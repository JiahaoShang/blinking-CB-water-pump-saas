# -*- coding: utf-8 -*-
"""桥运行与桥预部署工具测试：既有单账号容器模式 + 企业专属桥注册表模式。

覆盖：
- 既有模式（未设 WHATSAPP_PERSONAL_BRIDGES_FILE）行为不变（多会话直连 / 单账号容器）；
- 企业注册表模式（channel_runtime）：按 tenant 解析、fail-closed、不调用 Docker、密钥不进 config；
- deploy/provision_enterprise_bridge.py：幂等创建/重建、新容器固定回环端口发布、标签与卷校验防误复用、注册表原子写与受限权限。
Docker 用进程内假件；本文件不构成真实渠道验收。
"""
import hashlib
import json
import os
import re
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from channel_runtime import provision_personal_bridge

REPO = Path(__file__).resolve().parent
TENANT = 'tenant-alpha'
ACCOUNT = 'a' * 24
ACCOUNT2 = 'b' * 24


def _bridge_tool():
    """延迟导入部署工具（deploy/ 不在包路径内，且部署工具未实现时不影响本文件其余用例）。"""
    sys.path.insert(0, str(REPO / 'deploy'))
    import provision_enterprise_bridge

    return provision_enterprise_bridge


def _tenant_hash(tenant):
    return hashlib.sha256(tenant.encode('utf-8')).hexdigest()[:24]


class RuntimeCheck(unittest.TestCase):
    def test_isolated_container_config_and_secret_permissions(self):
        with tempfile.TemporaryDirectory() as directory:
            calls = []

            def docker(args, **kwargs):
                calls.append(args)
                if args[1] == 'inspect':
                    return SimpleNamespace(returncode=1)
                if args[1] == 'port':
                    return SimpleNamespace(returncode=0, stdout='127.0.0.1:41234\n')
                return SimpleNamespace(returncode=0)

            with patch.dict(os.environ, {'WHATSAPP_PERSONAL_API_KEY': 'fixture-key', 'WHATSAPP_PERSONAL_PROXY': 'http://local-proxy:7897'}, clear=True), patch('channel_runtime.subprocess.run', side_effect=docker):
                config = provision_personal_bridge(Path(directory) / 'app.db', 'a' * 24, {})
            self.assertEqual(config['session'], 'default')
            self.assertEqual(config['api_base'], 'http://127.0.0.1:41234')
            file = Path(directory) / 'connection-runtime' / ('a' * 24) / 'bridge.env'
            self.assertEqual(file.stat().st_mode & 0o777, 0o600)
            self.assertIn('WHATSAPP_DEFAULT_ENGINE=NOWEB', file.read_text())
            self.assertIn('WHATSAPP_PROXY_SERVER=', file.read_text())
            command = next(c for c in calls if c[1] == 'run')
            self.assertIn('127.0.0.1::3000', command)
            self.assertNotIn('fixture-key', ' '.join(command))


class FakeDocker:
    """进程内假件：只模拟本工具用到的 docker CLI 子集，并保持容器状态。"""

    def __init__(self, port=41000):
        self.calls = []
        self.containers = {}
        self.port = port  # 未固定端口容器（修复前的动态映射）docker port 报告的回落值
        self.inspect_error = ''  # 非空时任何 inspect 都失败（模拟 Docker 守护进程不可用）

    def __call__(self, args, timeout=None):
        self.calls.append([str(item) for item in args])
        args = [str(item) for item in args]
        command = args[0]
        if command == 'inspect':
            if self.inspect_error:
                return SimpleNamespace(returncode=1, stdout='', stderr=self.inspect_error)
            template = args[args.index('--format') + 1] if '--format' in args else ''
            name = args[-1]
            state = self.containers.get(name)
            if state is None:
                return SimpleNamespace(returncode=1, stdout='', stderr=f'Error: No such object: {name}')
            if template == '{{.State.Running}}':
                return SimpleNamespace(returncode=0, stdout=('true\n' if state['running'] else 'false\n'), stderr='')
            if 'Config.Labels' in template:
                return SimpleNamespace(returncode=0, stdout=json.dumps(state['labels']), stderr='')
            if 'Mounts' in template:
                return SimpleNamespace(returncode=0, stdout=json.dumps(state['mounts']), stderr='')
            if 'Config.Image' in template:
                return SimpleNamespace(returncode=0, stdout=state['image'], stderr='')
            return SimpleNamespace(returncode=1, stdout='', stderr='unsupported template')
        if command == 'run':
            name = args[args.index('--name') + 1]
            mounts = []
            labels = {}
            host_port = None
            for index, item in enumerate(args):
                if item == '-v':
                    source, _, destination = args[index + 1].partition(':')
                    mounts.append({'Source': source, 'Destination': destination})
                if item == '--label':
                    key, _, value = args[index + 1].partition('=')
                    labels[key] = value
                if item == '-p':
                    mapping = re.fullmatch(r'127\.0\.0\.1:(\d{1,5}):3000', args[index + 1])
                    if mapping:
                        host_port = mapping.group(1)
            self.containers[name] = {'running': True, 'labels': labels, 'mounts': mounts,
                                     'image': args[-1], 'host_port': host_port}
            return SimpleNamespace(returncode=0, stdout='faked-container-id\n', stderr='')
        if command == 'start':
            state = self.containers.setdefault(args[1], {'labels': {}, 'mounts': [], 'image': '', 'running': False})
            state['running'] = True
            return SimpleNamespace(returncode=0, stdout=args[1] + '\n', stderr='')
        if command == 'port':
            state = self.containers.get(args[1])
            if not state or not state['running']:
                return SimpleNamespace(returncode=1, stdout='', stderr='container not running')
            # 固定发布的容器回报创建时写入的端口；未固定的回落 self.port（模拟修复前动态映射的实际值）
            host_port = state.get('host_port') or self.port
            return SimpleNamespace(returncode=0, stdout=f'127.0.0.1:{host_port}\n', stderr='')
        return SimpleNamespace(returncode=0, stdout='', stderr='')


def _minimal_db(root, tenants, accounts):
    """最小库：部署工具只需要 tenants 表，注册表模式只需要 channel_accounts 的归属列。"""
    database = Path(root) / 'app.db'
    connection = sqlite3.connect(database)
    connection.execute('CREATE TABLE tenants (id TEXT PRIMARY KEY, name TEXT, created_at TEXT)')
    connection.execute('CREATE TABLE channel_accounts (id TEXT PRIMARY KEY, tenant_id TEXT, channel TEXT, config_json TEXT)')
    for tenant in tenants:
        connection.execute('INSERT INTO tenants VALUES (?,?,?)', (tenant, tenant, ''))
    for account, tenant in accounts:
        connection.execute('INSERT INTO channel_accounts VALUES (?,?,?,?)', (account, tenant, 'whatsapp_personal', '{}'))
    connection.commit()
    connection.close()
    return database


class EnterpriseBridgeToolTests(unittest.TestCase):
    """部署工具：容器创建 / 幂等重建 / 防误复用 / 私有注册表（docker 为进程内假件）。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix='enterprise-bridge-tool-')
        self.root = Path(self._tmp.name)
        self.tool = _bridge_tool()
        self.db = _minimal_db(self.root, (TENANT, 'tenant-beta'), ((ACCOUNT, TENANT), (ACCOUNT2, TENANT)))
        self.name = 'b2b-enterprise-' + _tenant_hash(TENANT)
        self.workdir = self.root / 'connection-runtime' / 'enterprise' / _tenant_hash(TENANT)
        self.registry = self.root / 'connection-runtime' / 'enterprise-bridges.json'

    def tearDown(self):
        self._tmp.cleanup()

    def provision(self, fake):
        with patch.object(self.tool, 'run_docker', fake):
            return self.tool.provision_tenant(TENANT, self.db)

    def published_port(self, fake, run_index=0):
        """取本工具第 run_index 次 docker run 的宿主端口映射，并断言其为固定数字回环形态。"""
        run = [call for call in fake.calls if call[0] == 'run'][run_index]
        mapping = run[run.index('-p') + 1]
        match = re.fullmatch(r'127\.0\.0\.1:(\d{1,5}):3000', mapping)
        self.assertIsNotNone(match, f'发布映射必须为固定数字回环端口（收到：{mapping!r}）')
        return match.group(1)

    def test_creates_isolated_container_env_file_and_registry(self):
        fake = FakeDocker(port=41001)
        summary = self.provision(fake)
        self.assertEqual(summary['container'], self.name)
        fixed_port = self.published_port(fake)
        self.assertEqual(summary['api_base'], 'http://127.0.0.1:' + fixed_port)
        self.assertIn('noweb-2026.9.2', summary['image'])
        run = next(call for call in fake.calls if call[0] == 'run')
        joined = ' '.join(run)
        self.assertIn(self.name, run)
        self.assertNotIn('127.0.0.1::3000', joined, '不得再使用动态宿主端口（停启会重分配）')
        volumes = [run[index + 1] for index, item in enumerate(run) if item == '-v']
        # 路径按真实形态比较（/var 与 /private/var 等符号链接前缀在不同环境下形态不同）
        sessions_volume = os.path.realpath(self.workdir / 'sessions') + ':/app/.sessions'
        media_volume = os.path.realpath(self.workdir / 'media') + ':/app/.media'
        self.assertIn(sessions_volume, volumes)
        self.assertIn(media_volume, volumes, '媒体卷必须挂到 WAHA 实际文件目录')
        self.assertIn('b2b.enterprise.bridge=1', run)
        self.assertIn('b2b.enterprise.tenant=' + TENANT, run)
        env_file = self.workdir / 'bridge.env'
        env_text = env_file.read_text()
        env = dict(line.split('=', 1) for line in env_text.splitlines() if '=' in line)
        self.assertEqual(env['WHATSAPP_DEFAULT_ENGINE'], 'NOWEB')
        self.assertEqual(env['WHATSAPP_SWAGGER_ENABLED'], 'false', '官方 Swagger 禁用开关')
        self.assertEqual(env['WAHA_SWAGGER_ENABLED'], 'false')
        self.assertEqual(env['WAHA_DASHBOARD_ENABLED'], 'false')
        self.assertEqual(env['WHATSAPP_FILES_FOLDER'], '/app/.media', '媒体目录环境变量必须是容器内挂载点')
        self.assertEqual(env['WHATSAPP_FILES_LIFETIME'], '0', '停止自动删除，保留策略另行确认')
        media_destination = next(volume.rpartition(':')[2] for volume in volumes
                                 if os.path.realpath(volume.rpartition(':')[0]) == os.path.realpath(self.workdir / 'media'))
        self.assertEqual(env['WHATSAPP_FILES_FOLDER'], media_destination, '环境变量与卷挂载目标必须一致')
        key = dict(line.split('=', 1) for line in env_text.splitlines() if '=' in line)['WAHA_API_KEY']
        self.assertTrue(key)
        self.assertEqual(env_file.stat().st_mode & 0o777, 0o600)
        self.assertNotIn(key, joined, '密钥不得出现在 docker 命令行参数里')
        self.assertNotIn(key, json.dumps(summary, ensure_ascii=False))
        self.assertTrue((self.workdir / 'sessions').is_dir())
        self.assertTrue((self.workdir / 'media').is_dir())
        document = json.loads(self.registry.read_text())
        self.assertEqual(document['version'], 1)
        self.assertEqual(document['bridges'][TENANT]['container_name'], self.name)
        self.assertEqual(document['bridges'][TENANT]['api_base'], 'http://127.0.0.1:' + fixed_port)
        self.assertEqual(document['bridges'][TENANT]['api_key'], key)
        self.assertEqual(self.registry.stat().st_mode & 0o777, 0o640)
        self.assertEqual((self.root / 'connection-runtime').stat().st_mode & 0o007, 0)
        self.assertEqual((self.root / 'connection-runtime' / 'enterprise').stat().st_mode & 0o007, 0)

    def test_new_container_publishes_fixed_host_port_and_registry_reflects_it(self):
        """修复回归锁：新容器固定发布数字宿主端口（旧的 127.0.0.1::3000 动态映射停启会重分配）。"""
        fake = FakeDocker(port=41001)
        summary = self.provision(fake)
        run = next(call for call in fake.calls if call[0] == 'run')
        mapping = run[run.index('-p') + 1]
        match = re.fullmatch(r'127\.0\.0\.1:(\d{1,5}):3000', mapping)
        self.assertIsNotNone(match, f'必须固定发布数字宿主端口（收到：{mapping!r}）')
        port = int(match.group(1))
        self.assertGreaterEqual(port, 1)
        self.assertLessEqual(port, 65535)
        self.assertNotIn('127.0.0.1::3000', ' '.join(run), '不得再使用空的动态宿主端口')
        self.assertEqual(summary['api_base'], f'http://127.0.0.1:{port}')
        document = json.loads(self.registry.read_text())
        self.assertEqual(document['bridges'][TENANT]['api_base'], f'http://127.0.0.1:{port}',
                         '注册表必须反映固定发布端口')
        # 同一容器停/启不重选端口：注册表地址稳定、不得重建容器
        fake.containers[self.name]['running'] = False
        second = self.provision(fake)
        self.assertEqual(second['action'], 'started')
        self.assertEqual(second['api_base'], f'http://127.0.0.1:{port}')
        self.assertFalse(second['registry_changed'], '端口未变化时不得改写注册表地址')
        self.assertEqual(len([call for call in fake.calls if call[0] == 'run']), 1, '不得自动重建容器')

    def test_second_run_is_idempotent_and_refreshes_address(self):
        fake = FakeDocker(port=41001)
        self.provision(fake)
        first_key = json.loads(self.registry.read_text())['bridges'][TENANT]['api_key']
        calls_after_first = len(fake.calls)
        # 模拟修复前创建的动态端口容器：停机重启后宿主端口实际变化，仅经 docker port 报告
        fake.containers[self.name].pop('host_port', None)
        fake.port = 41002
        fake.containers[self.name]['running'] = False
        second = self.provision(fake)
        self.assertEqual(second['action'], 'started')
        self.assertEqual(second['api_base'], 'http://127.0.0.1:41002')
        self.assertFalse(any(call[0] == 'run' for call in fake.calls[calls_after_first:]), '不得重复创建容器')
        self.assertTrue(any(call[0] == 'start' for call in fake.calls[calls_after_first:]))
        document = json.loads(self.registry.read_text())
        self.assertEqual(document['bridges'][TENANT]['api_base'], 'http://127.0.0.1:41002')
        self.assertEqual(document['bridges'][TENANT]['api_key'], first_key)
        self.assertEqual((self.workdir / 'bridge.env').read_text().count('WAHA_API_KEY='), 1)

    def test_rebuild_from_restored_registry_updates_random_address(self):
        # 模拟备份恢复：注册表与桥数据目录已恢复（旧地址/旧密钥），容器不存在 → 重建并更新端口
        (self.workdir / 'sessions').mkdir(parents=True)
        (self.workdir / 'sessions' / 'state.bin').write_bytes(b'login-state')
        (self.workdir / 'media').mkdir()
        restored_key = 'c' * 64
        (self.workdir / 'bridge.env').write_text('WAHA_API_KEY=' + restored_key + '\nWHATSAPP_DEFAULT_ENGINE=NOWEB\n')
        self.tool.write_registry_file(self.registry, {'version': 1, 'bridges': {TENANT: {
            'container_name': self.name, 'api_base': 'http://127.0.0.1:39999', 'api_key': restored_key}}})
        fake = FakeDocker(port=45123)
        summary = self.provision(fake)
        self.assertEqual(summary['action'], 'created')
        rebuild_port = self.published_port(fake)  # 重建时重新随机选取并固定发布
        self.assertEqual(summary['api_base'], 'http://127.0.0.1:' + rebuild_port)
        document = json.loads(self.registry.read_text())
        self.assertEqual(document['bridges'][TENANT]['api_base'], 'http://127.0.0.1:' + rebuild_port)
        self.assertEqual(document['bridges'][TENANT]['api_key'], restored_key, '恢复后重建不得轮换密钥')
        self.assertEqual((self.workdir / 'sessions' / 'state.bin').read_bytes(), b'login-state')

    def test_refuses_foreign_container_with_same_name(self):
        fake = FakeDocker(port=41001)
        self.provision(fake)
        calls_before = len(fake.calls)
        fake.containers[self.name]['labels'] = {}
        with self.assertRaises(self.tool.ProvisionError):
            self.provision(fake)
        fake.containers[self.name]['labels'] = {'b2b.enterprise.bridge': '1', 'b2b.enterprise.tenant': 'someone-else'}
        with self.assertRaises(self.tool.ProvisionError):
            self.provision(fake)
        fake.containers[self.name]['labels'] = {'b2b.enterprise.bridge': '1', 'b2b.enterprise.tenant': TENANT}
        fake.containers[self.name]['mounts'] = [{'Source': '/tmp/foreign', 'Destination': '/app/.sessions'}]
        with self.assertRaises(self.tool.ProvisionError):
            self.provision(fake)
        self.assertFalse(
            any(call[0] in ('run', 'start') for call in fake.calls[calls_before:]),
            '校验不通过时不得创建或启动容器',
        )

    def test_refuses_unknown_tenant_or_missing_database(self):
        with self.assertRaises(self.tool.ProvisionError):
            self.tool.provision_tenant('tenant-ghost', self.db)
        with self.assertRaises(self.tool.ProvisionError):
            self.tool.provision_tenant(TENANT, self.root / 'missing.db')
        self.assertFalse(self.registry.exists(), '被拒绝的调用不得留下注册表')

    def test_list_registered_returns_managed_names(self):
        fake = FakeDocker(port=41001)
        self.provision(fake)
        self.assertEqual(self.tool.registered_containers(self.db), [self.name])

    def check(self, fake):
        with patch.object(self.tool, 'run_docker', fake):
            return self.tool.check_registered(self.db)

    def test_check_registered_reports_verified_states(self):
        fake = FakeDocker(port=41001)
        self.provision(fake)
        self.assertEqual(self.check(fake), [(self.name, 'running')])
        fake.containers[self.name]['running'] = False
        self.assertEqual(self.check(fake), [(self.name, 'stopped')])
        fake.containers.pop(self.name)
        self.assertEqual(self.check(fake), [(self.name, 'absent')])

    def test_check_registered_fails_closed_on_foreign_mount_or_label_or_inspect_error(self):
        # 备份停写预检：登记容器的卷必须属于本库数据目录；否则拒绝（不得当成“无需停写”）
        fake = FakeDocker(port=41001)
        self.provision(fake)
        original_mounts = fake.containers[self.name]['mounts']
        fake.containers[self.name]['mounts'] = [
            {'Source': '/srv/other-data/connection-runtime/enterprise/x/sessions', 'Destination': '/app/.sessions'},
            {'Source': '/srv/other-data/connection-runtime/enterprise/x/media', 'Destination': '/app/.media'},
        ]
        with self.assertRaises(self.tool.ProvisionError):
            self.check(fake)
        fake.containers[self.name]['mounts'] = original_mounts
        # tenant 标签不符（同名容器但别的企业/来源）→ 拒绝
        fake.containers[self.name]['labels']['b2b.enterprise.tenant'] = 'tenant-other'
        with self.assertRaises(self.tool.ProvisionError):
            self.check(fake)
        fake.containers[self.name]['labels']['b2b.enterprise.tenant'] = TENANT
        self.assertEqual(self.check(fake), [(self.name, 'running')])
        # Docker inspect 失败不能当成“容器不在运行”
        fake.inspect_error = 'Cannot connect to the Docker daemon at unix:///var/run/docker.sock'
        with self.assertRaises(self.tool.ProvisionError):
            self.check(fake)

    def test_check_registered_cli_output_contract(self):
        import contextlib
        import io

        fake = FakeDocker(port=41001)
        self.provision(fake)
        buffer = io.StringIO()
        with patch.object(self.tool, 'run_docker', fake), contextlib.redirect_stdout(buffer):
            code = self.tool.main(['--check-registered', '--db', str(self.db)])
        self.assertEqual(code, 0)
        self.assertEqual(buffer.getvalue().strip(), f'running {self.name}')


class PersonalBridgeRegistryModeTests(unittest.TestCase):
    """channel_runtime 注册表模式：按企业解析、fail-closed、不调用 Docker、密钥不进 config。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix='enterprise-bridge-registry-')
        self.root = Path(self._tmp.name)
        self.tool = _bridge_tool()
        self.db = _minimal_db(self.root, (TENANT, 'tenant-beta'), ((ACCOUNT, TENANT), (ACCOUNT2, TENANT)))
        self.registry = self.root / 'connection-runtime' / 'enterprise-bridges.json'
        self.api_base = 'http://127.0.0.1:43210'
        self.bridge_key = 'tenant-alpha-key-0001'
        self.tool.write_registry_file(self.registry, {'version': 1, 'bridges': {TENANT: {
            'container_name': 'b2b-enterprise-' + _tenant_hash(TENANT),
            'api_base': self.api_base, 'api_key': self.bridge_key}}})
        self.env = patch.dict(os.environ, {
            'WHATSAPP_PERSONAL_BRIDGES_FILE': str(self.registry),
            'WHATSAPP_PERSONAL_API_KEY': 'global-key-must-not-be-used',
            'WHATSAPP_PERSONAL_BASE': 'http://127.0.0.1:9',
        })
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self._tmp.cleanup()

    def _forbid_docker(self):
        def _forbidden(*args, **kwargs):
            raise AssertionError('注册表模式下不得调用 Docker')

        return patch('channel_runtime.subprocess.run', side_effect=_forbidden)

    def test_resolves_current_tenant_entry_without_docker(self):
        with self._forbid_docker():
            config = provision_personal_bridge(self.db, ACCOUNT, {'session': 'business-' + ACCOUNT}, tenant_id=TENANT)
        self.assertEqual(config['api_base'], self.api_base)
        self.assertEqual(config['session'], 'business-' + ACCOUNT)
        self.assertEqual(config['bridge_tenant'], TENANT)
        self.assertNotIn('api_key', config)
        self.assertNotIn('managed_runtime', config)
        dumped = json.dumps(config)
        self.assertNotIn('global-key-must-not-be-used', dumped)
        self.assertNotIn('127.0.0.1:9', dumped)

    def test_fail_closed_without_valid_tenant_mapping(self):
        cases = (
            ({'session': 'business-' + ACCOUNT}, 'tenant-ghost'),  # 未注册企业
            ({'session': 'business-' + ACCOUNT}, ''),  # 缺 tenant
            ({'session': 'business-' + ACCOUNT2}, TENANT),  # 会话与账号不一致
            ({'managed_runtime': 'b2b-account-x', 'session': 'business-' + ACCOUNT}, 'tenant-ghost'),  # 不得走既有直通
        )
        for config, tenant_id in cases:
            with self.subTest(tenant=tenant_id, session=config.get('session')):
                with self._forbid_docker():
                    with self.assertRaises(ValueError):
                        provision_personal_bridge(self.db, ACCOUNT, dict(config), tenant_id=tenant_id)
        # 跨企业取钥：账号属于 tenant-alpha；即使用 tenant-beta（已注册）也必须拒绝
        document = json.loads(self.registry.read_text())
        document['bridges']['tenant-beta'] = {
            'container_name': 'b2b-enterprise-' + _tenant_hash('tenant-beta'),
            'api_base': 'http://127.0.0.1:43211', 'api_key': 'tenant-beta-key-0002'}
        self.tool.write_registry_file(self.registry, document)
        with self._forbid_docker():
            with self.assertRaises(ValueError):
                provision_personal_bridge(self.db, ACCOUNT, {'session': 'business-' + ACCOUNT}, tenant_id='tenant-beta')

    def test_fail_closed_when_registry_file_missing(self):
        self.registry.unlink()
        with self._forbid_docker():
            with self.assertRaises(ValueError):
                provision_personal_bridge(self.db, ACCOUNT, {'session': 'business-' + ACCOUNT}, tenant_id=TENANT)


class PersonalBridgeLegacyModeTests(unittest.TestCase):
    """未开启注册表（未设 WHATSAPP_PERSONAL_BRIDGES_FILE）时既有模式不变。"""

    def test_registry_file_present_but_not_configured_keeps_existing_modes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            tool = _bridge_tool()
            registry = root / 'connection-runtime' / 'enterprise-bridges.json'
            tool.write_registry_file(registry, {'version': 1, 'bridges': {TENANT: {
                'container_name': 'b2b-enterprise-' + _tenant_hash(TENANT),
                'api_base': 'http://127.0.0.1:43210', 'api_key': 'tenant-alpha-key-0001'}}})
            with patch.dict(os.environ, {'WHATSAPP_PERSONAL_MULTI_SESSION': '1'}, clear=True):
                config = provision_personal_bridge(root / 'app.db', ACCOUNT, {'k': 1})
            self.assertEqual(config, {'k': 1}, '未配置注册表时必须保持既有直通行为')


if __name__ == '__main__':
    unittest.main()
