# -*- coding: utf-8 -*-
"""T5 首发阻断修复：企业界面「连接机器人」必须真正启动 LangBot 收发轮询。

真实原因（对冻结传输栈 rockchin/langbot@sha256:ed8c7f19…（4.10.11）逐源核对）：
- POST /api/v1/platform/bots（create_bot）只执行 load_bot，不 run()——机器人被载入但长轮询未启动；
- PUT /api/v1/platform/bots/{uuid}（update_bot）才会 remove_bot → load_bot → enable 时 run()；
- 真实 Telegram 用户已在企业界面完成授权，但云端机器人在 enable=true 下没有任何运行/事件日志——
  「界面显示已连接却永远收不到消息」是首发阻断。

本测试锁定 business_channel_ui.py connect 分支的最小修复：
1. 创建返回有效 uuid 后，必须先 PUT {'enable': True} 启用，全部成功才把 bot_uuid/status 落库；
   启用失败不得算已连接（不产生「已连接」状态或页面）。
2. 启用失败：尝试 DELETE 清理刚创建的上游机器人（清理失败也不谎报）；清空本地 remote_account_id；
   界面只显示统一安全提示——不出现令牌、上游地址或上游错误原文。
3. 默认值/权限/断开语义不变：AI 默认关闭、成员与跨企业权限照旧、断开仍 DELETE 并复位状态。

隔离口径：临时 SQLite + 本机 mock LangBot 端点（记录有序请求、故障注入、小状态机）+
拦截 api.telegram.org 的 getMe 假件；不触外网、不发真实消息、不读任何真实凭据。
真实 Telegram 链路的运行期收发证据由独立验收（Codex）在真实实例上提供，不在本文件范围。
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import unittest
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from app import Database, make_server  # noqa: E402
from test_langbot_bridge import form_request, login  # noqa: E402

CREDENTIAL = '8880939599:AAFakeTokenForTests_abc123'
POISON = 'upstream-error-LEAK-' + CREDENTIAL
BOT_UUID_1 = 'bot-uuid-0001'
BOT_UUID_2 = 'bot-uuid-0002'

_REAL_URLOPEN = urllib.request.urlopen


class _FakeResponse:
    """最小的 urlopen 响应假件（支持 with / json.load）。"""

    def __init__(self, payload):
        self._raw = json.dumps(payload).encode('utf-8')

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self, *args):
        return self._raw


class _FakeTelegramIdentity:
    """api.telegram.org/bot<token>/getMe 假件：记录被请求的令牌，其余请求交给真实 urlopen（只到本机 mock）。"""

    def __init__(self, bot_id=8880939599, username='growave_test_bot'):
        self.bot_id = bot_id
        self.username = username
        self.calls = []

    def open(self, request, timeout=None, **kwargs):
        url = getattr(request, 'full_url', str(request))
        if url.startswith('https://api.telegram.org/'):
            self.calls.append(url)
            return _FakeResponse({'ok': True, 'result': {
                'id': self.bot_id, 'username': self.username, 'first_name': 'Growave'}})
        return _REAL_URLOPEN(request, timeout=timeout, **kwargs)


class _MockLangBot:
    """最小 LangBot 端点：有序记录请求；机器人小状态机（创建/启用/删除）与故障注入。

    与冻结镜像一致：创建只是登记（load_bot），启用（PUT）才代表进入运行；删除清理残留。
    """

    def __init__(self, api_key='lbk_test_key', user_token='tok_test_user'):
        self.api_key = api_key
        self.user_token = user_token
        self.requests = []
        self.bots = set()
        self._next = 1
        self.fail_activate = False
        self.fail_delete = False
        state = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _json(self, status, payload):
                body = json.dumps(payload).encode('utf-8')
                self.send_response(status)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _record(self):
                length = int(self.headers.get('Content-Length') or 0)
                raw = self.rfile.read(length).decode('utf-8')
                state.requests.append({
                    'method': self.command, 'path': self.path,
                    'api_key': self.headers.get('X-API-Key') or '',
                    'authorization': self.headers.get('Authorization') or '',
                    'body': json.loads(raw) if raw else None,
                })

            def _bot_uuid(self):
                return self.path.rstrip('/').rsplit('/', 1)[-1]

            def do_POST(self):
                self._record()
                if self.path == '/api/v1/webhooks':
                    if self.headers.get('Authorization') != 'Bearer ' + state.user_token:
                        return self._json(401, {'code': 401, 'msg': 'bad user token', 'data': None})
                    return self._json(200, {'code': 0, 'msg': 'ok', 'data': {'uuid': 'webhook-1'}})
                if self.path == '/api/v1/platform/bots':
                    if self.headers.get('X-API-Key') != state.api_key:
                        return self._json(401, {'code': 401, 'msg': 'bad api key', 'data': None})
                    uuid = 'bot-uuid-%04d' % state._next
                    state._next += 1
                    state.bots.add(uuid)
                    return self._json(200, {'code': 0, 'msg': 'ok', 'data': {'uuid': uuid}})
                return self._json(404, {'code': 404, 'msg': 'not found', 'data': None})

            def do_PUT(self):
                self._record()
                if self.headers.get('X-API-Key') != state.api_key:
                    return self._json(401, {'code': 401, 'msg': 'bad api key', 'data': None})
                if state.fail_activate:
                    return self._json(500, {'code': 500, 'msg': POISON, 'data': None})
                if self._bot_uuid() not in state.bots:
                    return self._json(404, {'code': 404, 'msg': 'bot not found', 'data': None})
                return self._json(200, {'code': 0, 'msg': 'ok', 'data': None})

            def do_DELETE(self):
                self._record()
                if self.headers.get('X-API-Key') != state.api_key:
                    return self._json(401, {'code': 401, 'msg': 'bad api key', 'data': None})
                if state.fail_delete:
                    return self._json(500, {'code': 500, 'msg': POISON, 'data': None})
                if self._bot_uuid() not in state.bots:
                    return self._json(404, {'code': 404, 'msg': 'bot not found', 'data': None})
                state.bots.discard(self._bot_uuid())
                return self._json(200, {'code': 0, 'msg': 'ok', 'data': None})

        self._server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.url = 'http://127.0.0.1:%d' % self._server.server_address[1]
        threading.Thread(target=self._server.serve_forever, daemon=True).start()

    def methods(self):
        return ['%s %s' % (r['method'], r['path']) for r in self.requests]

    def stop(self):
        self._server.shutdown()
        self._server.server_close()


class TelegramConnectActivationTests(unittest.TestCase):
    """企业界面 Telegram 连接：创建→启用（PUT）→落库；失败安全、可重试、权限与断开语义不变。"""

    ENV_NAMES = ('LANGBOT_API_KEY', 'LANGBOT_API_BASE', 'LANGBOT_USER_TOKEN')

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='t5-telegram-start-')
        self.db = Database(Path(self.tmp.name) / 'app.db')
        self.db.seed_demo_users()
        self.tenant = 'demo-tenant'
        self.admin, self.a, self.b = [self.db.authenticate_user(email, password) for email, password in (
            ('demo-admin@example.invalid', 'demo-admin'),
            ('demo-sales@example.invalid', 'demo-sales'),
            ('demo-engineer@example.invalid', 'demo-engineer'),
        )]
        for user in (self.a, self.b):
            self.db.set_user_modules(self.tenant, user['id'], ['inbox', 'channels', 'sales', 'rfqs'], self.admin['email'])
        self.db.ensure_tenant('tenant-b', '企业 B')
        self.db.create_user('tenant-b', 'admin-b@example.invalid', 'B 管理员', 'admin', 'b-admin-pass-123', actor='system:test')
        self.mock = _MockLangBot()
        self.telegram = _FakeTelegramIdentity()
        self._env_backup = {name: os.environ.get(name) for name in self.ENV_NAMES}
        os.environ.update({'LANGBOT_API_KEY': self.mock.api_key, 'LANGBOT_API_BASE': self.mock.url,
                           'LANGBOT_USER_TOKEN': self.mock.user_token})
        self.server = make_server(Path(self.tmp.name) / 'http.db', port=0)
        self.server.RequestHandlerClass.db.conn.close()
        self.server.RequestHandlerClass.db = self.db
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.cookie_admin = login(self.server, 'demo-admin@example.invalid', 'demo-admin')
        self.cookie_a = login(self.server, 'demo-sales@example.invalid', 'demo-sales')
        self.cookie_b = login(self.server, 'demo-engineer@example.invalid', 'demo-engineer')
        self.cookie_b_admin = login(self.server, 'admin-b@example.invalid', 'b-admin-pass-123')

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.mock.stop()
        self.db.conn.close()
        self.tmp.cleanup()
        for name, value in self._env_backup.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value

    # ── 辅助 ──────────────────────────────────────────────────────────────
    def connection(self, owner=None, channel='langbot', platform='Telegram'):
        owner = owner or self.a
        return self.db.create_business_connection(self.tenant, channel, platform, 'T5 测试账号', owner['id'], self.admin['email'])

    def connect(self, account_id, cookie, credential=CREDENTIAL):
        """经真实路由 POST connect 分支；只拦截 telegram getMe，其余出站到本机 mock。"""
        with patch('urllib.request.urlopen', side_effect=self.telegram.open):
            return form_request(self.server, 'POST', '/admin/channels/accounts/' + account_id + '/connect',
                                {'credential': credential}, cookie)

    def stored(self, account_id):
        row = self.db.get_business_connection(self.tenant, account_id)
        return row, json.loads(row['config_json'])

    def account_page(self, account_id, cookie):
        code, _, page = form_request(self.server, 'GET', '/admin/channels/accounts/' + account_id, None, cookie)
        self.assertEqual(code, 200)
        return page

    def assert_credential_absent_everywhere(self):
        """令牌不得以任何形式落库（连接配置、审计明细等一律检查）。"""
        for table in ('channel_accounts', 'audit_log'):
            rows = self.db.conn.execute('SELECT * FROM %s' % table).fetchall()
            blob = json.dumps([dict(row) for row in rows], ensure_ascii=False, default=str)
            self.assertNotIn(CREDENTIAL, blob, '令牌不得写入 %s' % table)

    # ── 用例 ──────────────────────────────────────────────────────────────
    def test_connect_creates_then_activates_before_saving_connected(self):
        """成功路径：webhook → 创建 → 启用（PUT {'enable': True}）之后才落库为已连接。"""
        account = self.connection()
        code, headers, _ = self.connect(account, self.cookie_a)
        self.assertEqual(code, 303)
        self.assertIn('?notice=saved', headers.get('Location', ''))
        self.assertEqual(len(self.telegram.calls), 1, '应经真实授权校验一次 getMe（假件拦截）')
        self.assertEqual(self.mock.methods(), [
            'POST /api/v1/webhooks',
            'POST /api/v1/platform/bots',
            'PUT /api/v1/platform/bots/' + BOT_UUID_1,
        ], '必须先创建、再启用；保存已连接之前不得跳过启用调用')
        self.assertEqual(self.mock.requests[1]['body'].get('adapter'), 'telegram')
        self.assertNotEqual(self.mock.requests[1]['api_key'], '', '创建机器人必须带连接服务 API Key')
        self.assertEqual(self.mock.requests[2]['body'], {'enable': True},
                         '启用调用必须且只是 PUT {"enable": True}（update_bot 据此 load_bot+run）')
        row, config = self.stored(account)
        self.assertEqual(config.get('bot_uuid'), BOT_UUID_1)
        self.assertEqual(config.get('account_name'), '@growave_test_bot')
        self.assertEqual(row['remote_account_id'], str(self.telegram.bot_id))
        self.assertEqual(row['status'], 'enabled')
        self.assertEqual(row['ai_enabled'], 0, '连接成功不得顺带开启 AI（默认关闭）')
        audit = self.db.conn.execute(
            "SELECT action FROM audit_log WHERE tenant_id=? AND entity_id=? ORDER BY rowid DESC LIMIT 1",
            (self.tenant, account)).fetchone()
        self.assertEqual(audit['action'], 'connection_connected')
        self.assert_credential_absent_everywhere()
        page = self.account_page(account, self.cookie_a)
        self.assertIn('已连接 · @growave_test_bot', page, '启用成功后才显示已连接')

    def test_activation_failure_not_connected_cleanup_and_safe_error(self):
        """启用失败：不算已连接、清理上游残留、清空本地身份，界面只有统一安全提示。"""
        account = self.connection()
        self.mock.fail_activate = True
        code, _, page = self.connect(account, self.cookie_a)
        self.assertEqual(code, 400)
        self.assertIn('账号连接未完成', page)
        for leaked in (CREDENTIAL, 'api.telegram.org', POISON):
            self.assertNotIn(leaked, page, '界面不得出现令牌、上游地址或上游错误原文：%s' % leaked)
        self.assertEqual(self.mock.methods(), [
            'POST /api/v1/webhooks',
            'POST /api/v1/platform/bots',
            'PUT /api/v1/platform/bots/' + BOT_UUID_1,
            'DELETE /api/v1/platform/bots/' + BOT_UUID_1,
        ], '启用失败后必须尝试删除刚创建的上游机器人')
        self.assertEqual(self.mock.bots, set(), '上游残留应已被清理')
        row, config = self.stored(account)
        self.assertNotIn('bot_uuid', config, '启用失败不得保存已连接状态')
        self.assertEqual(row['remote_account_id'], '', '本地外部身份必须清空（可重新授权）')
        self.assertEqual(row['ai_enabled'], 0)
        self.assertIsNone(self.db.conn.execute(
            "SELECT 1 FROM audit_log WHERE entity_id=? AND action='connection_connected'", (account,)).fetchone())
        self.assert_credential_absent_everywhere()
        page = self.account_page(account, self.cookie_a)
        self.assertNotIn('已连接 ·', page, '失败后页面不得显示已连接')
        self.assertIn('连接机器人', page)

    def test_retry_after_failed_activation_connects_with_new_bot(self):
        """失败清理后本连接可重试；第二次创建的新机器人启用成功才算已连接。"""
        account = self.connection()
        self.mock.fail_activate = True
        self.assertEqual(self.connect(account, self.cookie_a)[0], 400)
        self.assertEqual(self.mock.bots, set())
        self.mock.fail_activate = False
        code, _, _ = self.connect(account, self.cookie_a)
        self.assertEqual(code, 303)
        self.assertEqual(self.mock.methods()[-1], 'PUT /api/v1/platform/bots/' + BOT_UUID_2)
        row, config = self.stored(account)
        self.assertEqual(config.get('bot_uuid'), BOT_UUID_2)
        self.assertEqual(row['remote_account_id'], str(self.telegram.bot_id), '重试应重新写入外部身份')

    def test_cleanup_failure_still_safe_and_not_connected(self):
        """上游清理也失败时：界面仍安全、仍不算已连接；残留如实保留（不谎报已清理）。"""
        account = self.connection()
        self.mock.fail_activate = True
        self.mock.fail_delete = True
        code, _, page = self.connect(account, self.cookie_a)
        self.assertEqual(code, 400)
        self.assertIn('账号连接未完成', page)
        for leaked in (CREDENTIAL, 'api.telegram.org', POISON):
            self.assertNotIn(leaked, page)
        self.assertEqual(self.mock.methods()[-1], 'DELETE /api/v1/platform/bots/' + BOT_UUID_1)
        self.assertEqual(len(self.mock.bots), 1, '清理失败的残留必须如实在上游存在')
        row, config = self.stored(account)
        self.assertNotIn('bot_uuid', config)
        self.assertEqual(row['remote_account_id'], '')
        self.assert_credential_absent_everywhere()

    def test_permissions_cross_tenant_and_non_telegram_unchanged(self):
        """既有权限语义不变：非负责人 403、跨企业 403、非 Telegram 平台原提示；均不触达连接服务。"""
        account_a = self.connection(self.a)
        self.assertEqual(self.connect(account_a, self.cookie_b)[0], 403)
        self.assertEqual(self.connect(account_a, self.cookie_b_admin)[0], 403)
        self.assertEqual(self.mock.requests, [], '未授权请求不得触达连接服务')
        self.assertEqual(self.telegram.calls, [], '未授权请求不得触达 Telegram 授权接口')
        other = self.connection(self.admin, channel='whatsapp_personal', platform='WhatsApp')
        code, _, page = self.connect(other, self.cookie_admin)
        self.assertEqual(code, 400)
        self.assertIn('当前平台暂未提供此授权方式', page)
        self.assertEqual(self.mock.requests, [])
        row, _ = self.stored(account_a)
        self.assertEqual(row['ai_enabled'], 0)
        self.assertEqual(row['status'], 'enabled', '未连接账号保持创建默认状态')

    def test_disconnect_still_deletes_upstream_bot_and_resets_state(self):
        """断开语义不变：连接成功后断开仍 DELETE 上游机器人并复位本地状态。"""
        account = self.connection()
        self.assertEqual(self.connect(account, self.cookie_a)[0], 303)
        with patch('urllib.request.urlopen', side_effect=self.telegram.open):
            code, _, _ = form_request(self.server, 'POST', '/admin/channels/accounts/' + account + '/disconnect',
                                      {}, self.cookie_a)
        self.assertEqual(code, 303)
        self.assertEqual(self.mock.methods()[-1], 'DELETE /api/v1/platform/bots/' + BOT_UUID_1)
        self.assertEqual(self.mock.bots, set())
        row, config = self.stored(account)
        self.assertNotIn('bot_uuid', config)
        self.assertEqual(row['status'], 'disabled')
        self.assertEqual(row['ai_enabled'], 0)
        self.assertEqual(row['remote_account_id'], '')
        self.assertIn('连接机器人', self.account_page(account, self.cookie_a), '断开后页面回到连接向导')


if __name__ == '__main__':
    unittest.main(verbosity=2)
