"""桥接入运行时：单账号容器（既有模式）与企业专属桥注册表模式（运营预部署）。

企业专属桥（用户 2026-10-14 授权"每企业一个运营预部署的多会话容器"）：
- 运营以 root 运行 deploy/provision_enterprise_bridge.py 为每企业创建固定容器
  b2b-enterprise-<租户哈希 24hex>；登录会话与媒体卷位于
  <库父目录>/connection-runtime/enterprise/<哈希>/{sessions,media}（容器内挂到
  /app/.sessions 与 /app/.media）；
- 容器地址与逐企业独立密钥登记在私有注册表 <库父目录>/connection-runtime/enterprise-bridges.json
  （root:b2b 0640）；应用（无 Docker 权限）通过环境变量 WHATSAPP_PERSONAL_BRIDGES_FILE 只读；
- 应用侧只按账号配置里的企业引用解析桥：缺失/无效映射一律 fail-closed，不回退平台全局桥。
  密钥只在解析返回值中出现，不写入 config_json、页面或日志。
"""
import hashlib
import json
import os
import re
import subprocess
import sqlite3
from pathlib import Path

ENTERPRISE_CONTAINER_PREFIX = 'b2b-enterprise-'
ENTERPRISE_REGISTRY_FILENAME = 'enterprise-bridges.json'
ENTERPRISE_REGISTRY_ENV = 'WHATSAPP_PERSONAL_BRIDGES_FILE'
_BUSINESS_SESSION_PATTERN = re.compile(r'business-[0-9a-f]{24}')
_REGISTRY_API_BASE_PATTERN = re.compile(r'http://127\.0\.0\.1:\d{1,5}')
_ACCOUNT_ID_PATTERN = re.compile(r'[a-f0-9]{24}')


def enterprise_tenant_hash(tenant_id) -> str:
    """企业（租户）哈希：容器名与运行目录名共用（24 位十六进制）。"""
    return hashlib.sha256(str(tenant_id).encode('utf-8')).hexdigest()[:24]


def enterprise_container_name(tenant_id) -> str:
    return ENTERPRISE_CONTAINER_PREFIX + enterprise_tenant_hash(tenant_id)


def enterprise_runtime_dir(db_path) -> Path:
    return Path(db_path).resolve().parent / 'connection-runtime' / 'enterprise'


def enterprise_bridge_dir(db_path, tenant_id) -> Path:
    return enterprise_runtime_dir(db_path) / enterprise_tenant_hash(tenant_id)


def default_enterprise_registry_path(db_path) -> Path:
    return Path(db_path).resolve().parent / 'connection-runtime' / ENTERPRISE_REGISTRY_FILENAME


def enterprise_registry_path(db_path=None) -> Path:
    """注册表位置：环境变量 WHATSAPP_PERSONAL_BRIDGES_FILE 优先，其次 <库父目录>/connection-runtime/。"""
    configured = os.environ.get(ENTERPRISE_REGISTRY_ENV, '').strip()
    if configured:
        return Path(configured)
    if db_path is None:
        raise ValueError('未配置企业桥注册表路径')
    return default_enterprise_registry_path(db_path)


def read_enterprise_registry(registry_path):
    """读取企业桥私有注册表；文件不存在返回 {}；任何无效内容抛 ValueError（fail-closed）。

    校验不变量：容器名必须是该租户的派生名；地址必须是本机回环；密钥非空且无空白字符。
    报错信息只含租户/路径，不含密钥。
    """
    path = Path(registry_path)
    if path.is_symlink():
        raise ValueError(f'企业桥注册表是符号链接，拒绝读取：{path}')
    try:
        text = path.read_text(encoding='utf-8')
    except FileNotFoundError:
        return {}
    except OSError as error:
        raise ValueError(f'企业桥注册表无法读取：{path}') from error
    try:
        document = json.loads(text)
    except json.JSONDecodeError as error:
        raise ValueError(f'企业桥注册表内容无法解析：{path}') from error
    if not isinstance(document, dict) or document.get('version') != 1:
        raise ValueError(f'企业桥注册表版本不受支持：{path}')
    bridges = document.get('bridges')
    if not isinstance(bridges, dict):
        raise ValueError(f'企业桥注册表缺少 bridges 映射：{path}')
    result = {}
    for tenant_id, entry in bridges.items():
        if not isinstance(tenant_id, str) or not tenant_id or not isinstance(entry, dict):
            raise ValueError(f'企业桥注册表条目格式无效：{path}')
        container_name = entry.get('container_name')
        api_base = entry.get('api_base')
        api_key = entry.get('api_key')
        if container_name != enterprise_container_name(tenant_id):
            raise ValueError(f'企业桥注册表容器名与租户不一致：{tenant_id}')
        if not isinstance(api_base, str) or not _REGISTRY_API_BASE_PATTERN.fullmatch(api_base):
            raise ValueError(f'企业桥注册表地址无效：{tenant_id}')
        if not isinstance(api_key, str) or not api_key or len(api_key) > 200 or any(ch.isspace() for ch in api_key):
            raise ValueError(f'企业桥注册表密钥无效：{tenant_id}')
        result[tenant_id] = {'container_name': container_name, 'api_base': api_base, 'api_key': api_key}
    return result


def resolve_enterprise_bridge(tenant_ref, session, db_path=None, registry_path=None):
    """按企业引用解析专属桥：返回 {container_name, api_base, api_key} 或 None。

    应用侧发送 / 扫码 / 回调的统一入口：只认“账号配置里的租户引用 + business-<账号> 会话”，
    引用为空、未注册、条目无效或会话形态不符一律 None——调用方必须 fail-closed，
    不得回退平台全局桥（WHATSAPP_PERSONAL_BASE / WHATSAPP_PERSONAL_API_KEY）。
    不接受调用方传入注册表内容或自由地址；除本函数返回值外，密钥不应进入配置、页面或日志。
    """
    tenant_ref = str(tenant_ref or '').strip()
    session = str(session or '').strip()
    if not tenant_ref or not _BUSINESS_SESSION_PATTERN.fullmatch(session):
        return None
    if registry_path is None:
        try:
            registry_path = enterprise_registry_path(db_path)
        except ValueError:
            return None
    try:
        bridges = read_enterprise_registry(registry_path)
    except ValueError:
        return None
    entry = bridges.get(tenant_ref)
    return dict(entry) if entry else None


def provision_personal_bridge(db_path, account_id, config, tenant_id=None):
    """为业务账号准备个人版桥接入配置。

    注册表模式（已设 WHATSAPP_PERSONAL_BRIDGES_FILE）：按当前企业从私有注册表解析运营预部署桥，
    只回填租户引用 / 独立会话（business-<账号ID>）/ 非秘密路由地址；不调用 Docker；
    缺失、无效或跨企业映射一律拒绝（fail-closed）。其余情况保持既有模式不变。
    """
    if os.environ.get(ENTERPRISE_REGISTRY_ENV, '').strip():
        return _provision_enterprise_bridge(db_path, account_id, config, tenant_id)
    if config.get('managed_runtime'):
        return config
    if os.environ.get('WHATSAPP_PERSONAL_MULTI_SESSION') == '1':
        # 使用支持多会话的服务时，复用平台配置并采用账号专属会话。
        return config
    if not _ACCOUNT_ID_PATTERN.fullmatch(str(account_id or '')):
        raise ValueError('连接标识无效')
    api_key = os.environ.get('WHATSAPP_PERSONAL_API_KEY', '').strip()
    if not api_key or '\n' in api_key or '\r' in api_key:
        raise ValueError('连接服务未准备好，请联系平台运营方')
    name = 'b2b-account-' + account_id
    root = Path(db_path).resolve().parent / 'connection-runtime' / account_id
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        exists = subprocess.run(['docker', 'inspect', name], capture_output=True, timeout=10)
        if exists.returncode:
            env_file = root / 'bridge.env'
            env_data = 'WAHA_API_KEY=' + api_key + '\nWHATSAPP_DEFAULT_ENGINE=NOWEB\nWAHA_SWAGGER_ENABLED=false\nWAHA_DASHBOARD_ENABLED=false\n'
            proxy = os.environ.get('WHATSAPP_PERSONAL_PROXY', '').strip()
            if proxy and '\n' not in proxy and '\r' not in proxy:
                env_data += 'WHATSAPP_PROXY_SERVER=' + proxy + '\n'
            fd = os.open(env_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, 'w') as file:
                file.write(env_data)
            result = subprocess.run([
                'docker', 'run', '-d', '--name', name, '--restart', 'unless-stopped',
                '--add-host', 'host.docker.internal:host-gateway',
                '-p', '127.0.0.1::3000', '--env-file', str(env_file),
                '-v', str(root / 'sessions') + ':/app/.sessions',
                os.environ.get('WHATSAPP_PERSONAL_IMAGE', 'devlikeapro/waha:noweb-arm-2026.9.2'),
            ], capture_output=True, timeout=30)
            if result.returncode:
                raise ValueError('连接服务启动失败，请联系平台运营方')
        else:
            subprocess.run(['docker', 'start', name], capture_output=True, timeout=15, check=True)
        result = subprocess.run(['docker', 'port', name, '3000/tcp'], capture_output=True, text=True, timeout=10, check=True)
        address = result.stdout.strip().splitlines()[0]
        if not re.fullmatch(r'127\.0\.0\.1:\d+', address):
            raise ValueError('连接服务地址无效')
    except (OSError, subprocess.SubprocessError, IndexError):
        raise ValueError('连接服务暂不可用，请联系平台运营方') from None
    return {**config, 'api_base': 'http://' + address, 'session': 'default', 'managed_runtime': name}


def _provision_enterprise_bridge(db_path, account_id, config, tenant_id):
    """注册表模式：解析本企业条目并回填业务配置（不调用 Docker，密钥不进配置）。"""
    account_id = str(account_id or '')
    if not _ACCOUNT_ID_PATTERN.fullmatch(account_id):
        raise ValueError('连接标识无效')
    tenant = str(tenant_id or '').strip()
    if not tenant or len(tenant) > 190 or any(ch in tenant for ch in '\r\n\x00'):
        raise ValueError('连接服务未完成企业分配，请联系平台运营方')
    try:
        bridges = read_enterprise_registry(enterprise_registry_path(db_path))
    except ValueError:
        raise ValueError('连接服务配置异常，请联系平台运营方') from None
    entry = bridges.get(tenant)
    if not entry:
        raise ValueError('本企业尚未部署专属连接桥，请联系平台运营方')
    # 账号归属校验：只允许把账号绑定到其所在企业的桥，防止用其他企业引用取钥（跨企业隔离）。
    database = Path(db_path)
    if not database.is_file():
        raise ValueError('连接服务配置异常，请联系平台运营方')
    try:
        with sqlite3.connect(str(database)) as connection:
            connection.execute('PRAGMA query_only = ON')
            row = connection.execute('SELECT tenant_id FROM channel_accounts WHERE id=?', (account_id,)).fetchone()
    except sqlite3.DatabaseError:
        raise ValueError('连接服务配置异常，请联系平台运营方') from None
    if not row or str(row[0]) != tenant:
        raise ValueError('账号与企业连接配置不一致，请联系平台运营方')
    session = 'business-' + account_id
    existing = str(config.get('session') or '').strip()
    if existing and existing != session:
        raise ValueError('账号与企业连接配置不一致，请联系平台运营方')
    result = {**config, 'session': session, 'api_base': entry['api_base'], 'bridge_tenant': tenant}
    result.pop('managed_runtime', None)
    result.pop('api_key', None)
    return result
