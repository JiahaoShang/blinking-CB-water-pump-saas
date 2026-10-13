#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""企业专属 WhatsApp 个人版桥（WAHA）预部署工具（root）。

为单个企业在单台服务器上创建固定容器 b2b-enterprise-<租户哈希 24hex>：
- NOWEB 引擎、只监听 127.0.0.1 上的随机端口、关闭 Swagger（WHATSAPP_SWAGGER_ENABLED）与
  Dashboard（WAHA_DASHBOARD_ENABLED）；
- 登录会话与媒体卷位于 <库父目录>/connection-runtime/enterprise/<哈希>/{sessions,media}；
  容器内分别挂到 /app/.sessions 与 /app/.media，并以 WHATSAPP_FILES_FOLDER=/app/.media、
  WHATSAPP_FILES_LIFETIME=0 对齐 WAHA 实际文件目录（停止自动删除；保留时长规则待 T6 另行确认）；
- 每企业独立 API 密钥写入受限 env 文件（0600），以 --env-file 注入容器——密钥绝不进 CLI 参数或日志；
- 登记进私有注册表 <库父目录>/connection-runtime/enterprise-bridges.json
  （root:b2b 0640，原子写；应用经 WHATSAPP_PERSONAL_BRIDGES_FILE 只读）。
- 不自动创建/启动任何 WhatsApp 会话（扫码由企业在界面完成）；不设置内存硬配额，也不承诺账号容量。

幂等：重复运行不重建容器；复用前校验容器标签/挂载/镜像确认是本工具为该企业创建的桥。
备份恢复后重跑本工具即可按恢复的注册表重建容器，并将随机端口可能变化的地址更新回注册表。
备份停写预检用 --check-registered：核对登记容器属于本库数据目录（标签/卷）及运行状态，不符即失败。

用法（root）：
    python3 deploy/provision_enterprise_bridge.py --tenant <租户ID> --db /var/lib/b2b/data/app.db
    python3 deploy/provision_enterprise_bridge.py --tenant <租户ID> --db <库> --image <镜像或不可变 digest>
    python3 deploy/provision_enterprise_bridge.py --list-registered --db <库>      # 只读：容器名清单
    python3 deploy/provision_enterprise_bridge.py --check-registered --db <库>    # 只读：停写预检
"""
from __future__ import annotations

import argparse
import grp
import json
import os
import re
import secrets
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from channel_runtime import (  # noqa: E402  （同仓库模块；发布包中 deploy/ 与 channel_runtime.py 同层）
    enterprise_bridge_dir,
    enterprise_container_name,
    enterprise_registry_path,
    read_enterprise_registry,
)

DEFAULT_IMAGE = 'devlikeapro/waha:noweb-2026.9.2'
LABEL_MARKER = 'b2b.enterprise.bridge'
LABEL_TENANT = 'b2b.enterprise.tenant'
SESSIONS_TARGET = '/app/.sessions'
MEDIA_TARGET = '/app/.media'
BRIDGE_GROUP = 'b2b'
ENV_KEY_LINE = 'WAHA_API_KEY='
_TENANT_PATTERN = re.compile(r'[^\s/\\\x00-\x1f]{1,190}')


class ProvisionError(Exception):
    """部署前置检查或 Docker 操作失败（fail-closed：不产出半成品登记）。"""


def run_docker(args, timeout=30):
    """所有 Docker 调用的唯一入口（测试可替换）；命令行只含路径与镜像，绝不含密钥。"""
    try:
        return subprocess.run(['docker', *args], capture_output=True, text=True, timeout=timeout)
    except OSError as error:
        raise ProvisionError(f'Docker 不可用（{error}）') from None


def _docker_ok(result, action):
    if result.returncode:
        detail = (result.stderr or result.stdout or '').strip().splitlines()
        tail = detail[-1] if detail else ''
        raise ProvisionError(f'{action}失败（docker 退出码 {result.returncode}）' + (f'：{tail[:200]}' if tail else ''))
    return result


def _inspect(name, template):
    """docker inspect 取单个字段。

    容器不存在（stderr 含 No such…）返回 None；其他失败（守护进程不可用等）抛
    ProvisionError——绝不能把“无法确认”当成“不存在/不在运行”。
    """
    result = run_docker(['inspect', '--format', template, name], timeout=15)
    if result.returncode:
        stderr = (result.stderr or '')
        if 'no such' in stderr.lower():
            return None
        raise ProvisionError(f'无法确认容器状态（docker inspect 失败）：{name}：{stderr.strip().splitlines()[-1][:160] if stderr.strip() else ""}')
    return result.stdout.strip()


def _container_state(name):
    """返回 {'running','labels','mounts','image'} 或 None（容器不存在）。

    容器存在但 inspect 读不完整（守护进程异常等）会抛 ProvisionError，不当成“不存在”。
    """
    running = _inspect(name, '{{.State.Running}}')
    if running is None:
        return None
    labels_raw = _inspect(name, '{{json .Config.Labels}}')
    mounts_raw = _inspect(name, '{{json .Mounts}}')
    image = _inspect(name, '{{.Config.Image}}')
    try:
        labels = json.loads(labels_raw or '{}') or {}
    except json.JSONDecodeError:
        raise ProvisionError(f'无法解析同名容器标签，拒绝复用：{name}') from None
    try:
        mounts = json.loads(mounts_raw or '[]') or []
    except json.JSONDecodeError:
        raise ProvisionError(f'无法解析同名容器挂载，拒绝复用：{name}') from None
    if not isinstance(labels, dict) or not isinstance(mounts, list):
        raise ProvisionError(f'同名容器信息异常，拒绝复用：{name}')
    return {'running': running == 'true', 'labels': labels, 'mounts': mounts, 'image': image or ''}


def _same_path(left, right):
    """路径同一性：两侧先解析符号链接（如 macOS /var→/private/var），再退化为字面比较。"""
    left = str(left or '')
    right = str(right or '')
    if not left or not right:
        return False
    try:
        if os.path.realpath(left) == os.path.realpath(right):
            return True
    except OSError:
        pass
    return os.path.normpath(left) == os.path.normpath(right)


def _verify_managed_container(name, tenant_id, workdir, state):
    """核对容器确为本库（本数据目录）该企业的企业桥：标签 + sessions/media 卷来源。

    用于复用前校验与备份停写预检；不符时拒绝——绝不把“另一数据根的桥/恢复副本/无关容器”
    当成可复用或可停写对象。
    """
    if state['labels'].get(LABEL_MARKER) != '1' or state['labels'].get(LABEL_TENANT) != tenant_id:
        raise ProvisionError(f'同名容器不是本工具为该企业创建的桥，拒绝复用或停写：{name}（确认废弃后：docker rm -f {name}）')
    expected = {
        SESSIONS_TARGET: str(workdir / 'sessions'),
        MEDIA_TARGET: str(workdir / 'media'),
    }
    actual = {str(mount.get('Destination')): str(mount.get('Source')) for mount in state['mounts'] if isinstance(mount, dict)}
    for destination, source in expected.items():
        if not _same_path(actual.get(destination, ''), source):
            raise ProvisionError(f'同名容器挂载与本库数据目录不一致，拒绝复用或停写：{name}（可能属于其他数据目录或恢复副本）')


def _read_env_key(path):
    """读取受限 env 文件里的桥密钥；文件不存在/无有效密钥返回 ''。"""
    try:
        text = Path(path).read_text(encoding='utf-8')
    except FileNotFoundError:
        return ''
    except OSError as error:
        raise ProvisionError(f'桥 env 文件无法读取：{path}') from error
    for line in text.splitlines():
        if line.startswith(ENV_KEY_LINE):
            value = line[len(ENV_KEY_LINE):].strip()
            if value and not any(ch.isspace() for ch in value) and len(value) <= 200:
                return value
    return ''


def _write_env_file(path, api_key):
    """写受限 env 文件（0600）：密钥 + NOWEB + 关闭 Swagger/Dashboard + 媒体目录持久化。

    变量名核对（Codex 独立审查 2026-10-14）：Swagger 禁用开关为 WHATSAPP_SWAGGER_ENABLED，
    Dashboard 为 WAHA_DASHBOARD_ENABLED（保留 WAHA_SWAGGER_ENABLED 以覆盖不同版本命名）；
    媒体文件目录 WHATSAPP_FILES_FOLDER 必须等于容器内挂载点（/app/.media），
    WHATSAPP_FILES_LIFETIME=0 关闭自动删除——企业媒体保留时长规则仍待 T6 确认，这里只停止自动删。
    """
    lines = [
        ENV_KEY_LINE + api_key,
        'WHATSAPP_DEFAULT_ENGINE=NOWEB',
        'WHATSAPP_SWAGGER_ENABLED=false',
        'WAHA_SWAGGER_ENABLED=false',
        'WAHA_DASHBOARD_ENABLED=false',
        'WHATSAPP_FILES_FOLDER=' + MEDIA_TARGET,
        'WHATSAPP_FILES_LIFETIME=0',
    ]
    proxy = os.environ.get('WHATSAPP_PERSONAL_PROXY', '').strip()
    if proxy and not any(ch in proxy for ch in '\r\n\x00'):
        lines.append('WHATSAPP_PROXY_SERVER=' + proxy)
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w', encoding='utf-8') as handle:
        handle.write('\n'.join(lines) + '\n')
    os.chmod(path, 0o600)


def _chown_bridge_group(path):
    """root 运行时把属主设为 root:b2b（应用以 b2b 身份读注册表）；非 root 或系统无该组时按需处理。"""
    if os.geteuid() != 0:
        return
    try:
        gid = grp.getgrnam(BRIDGE_GROUP).gr_gid
    except KeyError:
        raise ProvisionError(f'系统缺少 {BRIDGE_GROUP} 用户组，无法按受限权限交付：{path}') from None
    os.chown(path, 0, gid)


def _restricted_dir(path):
    """确保目录存在且不对“其他人”开放（保留属主/组的既有读写位，兼容既有账号目录使用）。"""
    path = Path(path)
    path.mkdir(parents=True, exist_ok=True)
    mode = path.stat().st_mode & 0o777
    restricted = mode & ~0o007
    if restricted != mode:
        os.chmod(path, restricted)
    if path.stat().st_uid == 0:
        _chown_bridge_group(path)


def _private_dir(path):
    path = Path(path)
    path.mkdir(parents=True, exist_ok=True)
    os.chmod(path, 0o700)
    if path.stat().st_uid == 0:
        _chown_bridge_group(path)


def _prepare_runtime_dirs(db_path, tenant_id):
    workdir = enterprise_bridge_dir(db_path, tenant_id)
    _restricted_dir(workdir.parent)  # connection-runtime/enterprise
    _private_dir(workdir)
    for name in ('sessions', 'media'):
        _private_dir(workdir / name)
    return workdir


def tenant_exists(db_path, tenant_id):
    database = Path(db_path)
    if not database.is_file():
        raise ProvisionError(f'数据库不存在：{database}')
    try:
        with sqlite3.connect(str(database), timeout=5) as connection:
            connection.execute('PRAGMA query_only = ON')
            row = connection.execute('SELECT 1 FROM tenants WHERE id=?', (tenant_id,)).fetchone()
    except sqlite3.DatabaseError as error:
        raise ProvisionError(f'数据库无法读取（{database}）：{error}') from None
    return bool(row)


def write_registry_file(path, document):
    """原子写注册表：同目录临时文件（0600）→ fsync → 收紧为 0640/root:b2b → 替换。

    父目录收紧为“不对其他人开放”；拒绝符号链接。
    """
    path = Path(path)
    if path.is_symlink():
        raise ProvisionError(f'注册表路径是符号链接，拒绝写入：{path}')
    parent = path.parent
    if parent.is_symlink():
        raise ProvisionError(f'注册表父目录是符号链接，拒绝写入：{parent}')
    _restricted_dir(parent)
    payload = json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + '\n'
    fd, temporary = tempfile.mkstemp(prefix=path.name + '.', suffix='.tmp', dir=str(parent))
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o640)
        if os.stat(temporary).st_uid == 0:
            _chown_bridge_group(temporary)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def registered_containers(db_path, registry_path=None):
    """注册表登记的受管容器名（去重排序）；供备份停写脚本只读使用。"""
    registry = Path(registry_path) if registry_path else enterprise_registry_path(db_path)
    try:
        bridges = read_enterprise_registry(registry)
    except ValueError as error:
        raise ProvisionError(str(error)) from None
    return sorted({entry['container_name'] for entry in bridges.values()})


def check_registered(db_path, registry_path=None):
    """备份停写预检：核对每个登记容器的归属（标签 + sessions/media 卷与本库数据目录一致）与运行状态。

    返回 [(name, state)]，state ∈ running / stopped / absent；任何无法核对的情形
    （标签/挂载不符、docker inspect 失败）抛出 ProvisionError——调用方必须 fail-closed，
    绝不能把“无法确认”当成“无需停写”。恢复副本里登记的“原库同名容器”会因挂载不符被拒绝，
    因此不会停到另一数据根（生产）的桥。
    """
    database = Path(db_path)
    registry = Path(registry_path) if registry_path else enterprise_registry_path(database)
    try:
        bridges = read_enterprise_registry(registry)
    except ValueError as error:
        raise ProvisionError(str(error)) from None
    results = []
    seen = set()
    for tenant_id in sorted(bridges):
        name = bridges[tenant_id]['container_name']
        if name in seen:  # 去重（正常注册表不会重复；防御）
            continue
        seen.add(name)
        state = _container_state(name)
        if state is None:
            results.append((name, 'absent'))
            continue
        _verify_managed_container(name, tenant_id, enterprise_bridge_dir(database, tenant_id), state)
        results.append((name, 'running' if state['running'] else 'stopped'))
    return results


def provision_tenant(tenant_id, db_path, image=DEFAULT_IMAGE, registry_path=None):
    """为企业创建/复用专属桥容器并登记私有注册表；返回摘要（绝不含密钥）。"""
    tenant_id = str(tenant_id or '').strip()
    if not _TENANT_PATTERN.fullmatch(tenant_id):
        raise ProvisionError('无效的企业标识（--tenant）：不得包含空白、斜杠或控制字符')
    image = str(image or '').strip()
    if not image or any(ch.isspace() for ch in image):
        raise ProvisionError('无效的镜像引用（--image）')
    database = Path(db_path)
    if not database.is_file():
        raise ProvisionError(f'数据库不存在：{database}')
    if not tenant_exists(database, tenant_id):
        raise ProvisionError(f'企业不存在：{tenant_id}（请先在平台创建该企业，避免误建桥）')
    registry = Path(registry_path) if registry_path else enterprise_registry_path(database)
    try:
        bridges = read_enterprise_registry(registry)
    except ValueError as error:
        raise ProvisionError(str(error)) from None
    name = enterprise_container_name(tenant_id)
    entry = bridges.get(tenant_id)
    if entry and entry['container_name'] != name:
        raise ProvisionError(f'注册表条目与该企业容器命名规则不一致：{tenant_id}')

    workdir = _prepare_runtime_dirs(database, tenant_id)
    env_file = workdir / 'bridge.env'
    state = _container_state(name)
    if state is None:
        existing_key = _read_env_key(env_file)
        api_key = existing_key or (entry['api_key'] if entry else '') or secrets.token_hex(32)
        if existing_key != api_key:
            _write_env_file(env_file, api_key)
        _docker_ok(run_docker([
            'run', '-d', '--name', name, '--restart', 'unless-stopped',
            '--add-host', 'host.docker.internal:host-gateway',
            '-p', '127.0.0.1::3000', '--env-file', str(env_file),
            '-v', str(workdir / 'sessions') + ':' + SESSIONS_TARGET,
            '-v', str(workdir / 'media') + ':' + MEDIA_TARGET,
            '--label', LABEL_MARKER + '=1',
            '--label', LABEL_TENANT + '=' + tenant_id,
            image,
        ]), '容器创建')
        action = 'created'
    else:
        _verify_managed_container(name, tenant_id, workdir, state)
        if state['image'] != image:
            raise ProvisionError(
                f'同名容器镜像为 {state["image"]}，与 --image {image} 不一致；如确需更换先 docker rm -f {name} 再重跑本工具')
        api_key = _read_env_key(env_file)
        if not api_key:
            raise ProvisionError(f'容器已存在但缺少可用的受限 env 文件（{env_file}），请人工核对')
        action = 'running'
        if not state['running']:
            _docker_ok(run_docker(['start', name], timeout=30), '容器启动')
            action = 'started'
    port_result = _docker_ok(run_docker(['port', name, '3000/tcp'], timeout=15), '端口查询')
    address = (port_result.stdout.strip().splitlines() or [''])[0]
    if not re.fullmatch(r'127\.0\.0\.1:\d{1,5}', address):
        raise ProvisionError(f'未能取得回环映射端口，请检查容器端口映射：{name}')
    api_base = 'http://' + address
    updated_entry = {'container_name': name, 'api_base': api_base, 'api_key': api_key}
    registry_changed = entry != updated_entry
    if registry_changed:
        write_registry_file(registry, {'version': 1, 'bridges': {**bridges, tenant_id: updated_entry}})
    return {
        'tenant': tenant_id, 'container': name, 'api_base': api_base, 'image': image,
        'action': action, 'registry': str(registry), 'registry_changed': registry_changed,
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--tenant', default=None, help='企业（租户）ID：为该企业创建/复用专属桥容器')
    parser.add_argument('--db', required=True, help='平台数据库路径（定位 connection-runtime；并校验企业存在）')
    parser.add_argument('--image', default=DEFAULT_IMAGE, help=f'WAHA 镜像（默认 {DEFAULT_IMAGE}；可传不可变 digest）')
    parser.add_argument('--list-registered', action='store_true', help='只读：列出注册表登记的受管容器名')
    parser.add_argument('--check-registered', action='store_true',
                        help='只读：核对登记容器的归属（标签/卷）与运行状态，输出「state name」（备份停写预检）')
    args = parser.parse_args(argv)
    try:
        if args.list_registered:
            for name in registered_containers(args.db):
                print(name)
            return 0
        if args.check_registered:
            for name, state in check_registered(args.db):
                print(f'{state} {name}')
            return 0
        if not args.tenant:
            parser.error('--tenant 与 --list-registered/--check-registered 必须二选一（如 --tenant <企业ID>）')
        if os.geteuid() != 0:
            print('警告：未以 root 运行——部署时应以 root 运行（属主/权限按 root:b2b 受限交付）。', file=sys.stderr)
        summary = provision_tenant(args.tenant, args.db, image=args.image)
        note = '；注册表已更新' if summary['registry_changed'] else ''
        print(f"企业桥就绪：tenant={summary['tenant']} container={summary['container']} "
              f"api_base={summary['api_base']} image={summary['image']} action={summary['action']}{note}")
        return 0
    except ProvisionError as error:
        print(f'错误：{error}', file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
