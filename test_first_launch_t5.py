# -*- coding: utf-8 -*-
"""T5 首发最小回归：已有原生发送渠道的 RFQ 入口 + 送达事实提示（仅这两项及其门控）。

对应 /tmp/b2b-t5-repro-notes.txt 的两项阻断与 Hermes 的最小修复：
1. 已有原生发送渠道（whatsapp_personal，经既有出站总线）的收件箱会话提供「发送询价单」
   入口，链接经本企业桥发送；仍保留 can_send_conversation 发送权限、停止主动联系、
   会话所属渠道/企业边界；网站聊天（无外发通道）保持没有该入口。
2. 非 web 会话发送成功提示改为客观事实「渠道已接受发送，送达尚未确认」；回复区提示不再
   宣称「直达买方」；「已送达」只在真实回执推进（mark_outbound_delivered 同一落点函数）
   后渲染。

隔离口径：临时数据库 + 本机 mock WAHA 桥（伪造网络端点）；不触网、不发真实消息、
不读任何真实凭据；企业桥注册表模式与 EnterpriseBridgeRoutingTests 相同。
"""
import json
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'tools' / 'channels'))
sys.path.insert(0, str(ROOT / 'deploy'))

import mock_waha  # noqa: E402
import channel_runtime  # noqa: E402
import provision_enterprise_bridge  # noqa: E402
from app import Database, make_server, mark_outbound_delivered  # noqa: E402
from test_langbot_bridge import form_request, json_http, login  # noqa: E402


class _T5BridgeFixture(unittest.TestCase):
    """公共夹具：临时库 + 单企业桥 mock（企业注册表模式）+ 真实 HTTP 服务 + 两种登录。"""

    BRIDGE_KEY = 't5-bridge-key-000000'
    ENV_NAMES = (
        'WHATSAPP_PERSONAL_TOKEN', 'WHATSAPP_PERSONAL_API_KEY', 'WHATSAPP_PERSONAL_BASE',
        'WHATSAPP_PERSONAL_HMAC_KEY', 'WHATSAPP_PERSONAL_PACING', 'WHATSAPP_PERSONAL_BRIDGES_FILE',
    )

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(prefix='t5-first-launch-')
        cls.root = Path(cls.tmp.name)
        cls.tenant = 'demo-tenant'
        cls.db = Database(cls.root / 'app.db')
        cls.db.seed_demo_users()
        cls.admin = cls.db.authenticate_user('demo-admin@example.invalid', 'demo-admin')
        cls.member_id = cls.db.create_user(
            cls.tenant, 't5-member@example.invalid', 'T5 员工', 'employee', 't5-member-pass-123', cls.admin['email'])
        cls.db.set_user_modules(cls.tenant, cls.member_id, ['inbox', 'channels'], cls.admin['email'])
        cls.account = cls.db.create_business_connection(
            cls.tenant, 'whatsapp_personal', 'WhatsApp', 'T5 原生桥账号', cls.admin['id'], cls.admin['email'])
        cls.mock = mock_waha.MockWahaServer(api_key=cls.BRIDGE_KEY, session_name='business-' + cls.account)
        cls._env_backup = {name: os.environ.get(name) for name in cls.ENV_NAMES}
        for name in cls.ENV_NAMES:
            os.environ.pop(name, None)
        os.environ.update({
            'WHATSAPP_PERSONAL_BRIDGES_FILE': str(cls.root / 'connection-runtime' / 'enterprise-bridges.json'),
            'WHATSAPP_PERSONAL_BASE': 'http://127.0.0.1:9',  # 平台全局假桥：不得被使用
            'WHATSAPP_PERSONAL_API_KEY': 'global-key-must-not-be-used',
            'WHATSAPP_PERSONAL_TOKEN': 'global-token-must-not-route',
            'WHATSAPP_PERSONAL_HMAC_KEY': '',
            'WHATSAPP_PERSONAL_PACING': '0',
        })
        provision_enterprise_bridge.write_registry_file(
            cls.root / 'connection-runtime' / 'enterprise-bridges.json',
            {'version': 1, 'bridges': {cls.tenant: {
                'container_name': channel_runtime.enterprise_container_name(cls.tenant),
                'api_base': cls.mock.base_url(), 'api_key': cls.BRIDGE_KEY}}})
        config = json.loads(cls.db.get_business_connection(cls.tenant, cls.account)['config_json'])
        config = channel_runtime.provision_personal_bridge(cls.db.path, cls.account, config, tenant_id=cls.tenant)
        cls.db.conn.execute('UPDATE channel_accounts SET config_json=? WHERE tenant_id=? AND id=?',
                            (json.dumps(config), cls.tenant, cls.account))
        cls.db.conn.commit()
        cls.config = config
        cls.server = make_server(cls.root / 'http-server.db', port=0)
        cls.server.RequestHandlerClass.db.conn.close()
        cls.server.RequestHandlerClass.db = cls.db
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.cookie_admin = login(cls.server, 'demo-admin@example.invalid', 'demo-admin')
        cls.cookie_member = login(cls.server, 't5-member@example.invalid', 't5-member-pass-123')

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()
        cls.mock.stop()
        cls.db.conn.close()
        cls.tmp.cleanup()
        for name, value in cls._env_backup.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value

    def setUp(self):
        import app as app_module
        app_module._whatsapp_personal_reset_state()

    # ── 工具 ──────────────────────────────────────────────────────────────
    def inbound(self, jid, text, message_id):
        event = {
            'event': 'message.any', 'session': 'business-' + self.account,
            'payload': {'id': {'_serialized': message_id}, 'from': jid, 'to': '8617555000000@c.us',
                        'fromMe': False, 'body': text, 'hasMedia': False, '_data': {'notifyName': 'T5 客户'}},
        }
        return json_http(self.server, 'POST',
                         '/api/v1/channels/whatsapp-personal/webhook/' + self.config['webhook_token'], event)

    def conversation_for(self, jid):
        row = self.db.conn.execute(
            "SELECT conversation_id FROM channel_identities WHERE tenant_id=? AND channel='whatsapp_personal' AND external_user_id=?",
            (self.tenant, jid)).fetchone()
        return row['conversation_id'] if row else None

    def request(self, method, path, cookie, fields=None):
        return form_request(self.server, method, path, fields, cookie)

    def token_count(self, conversation_id):
        return self.db.conn.execute(
            'SELECT COUNT(*) AS n FROM rfq_form_tokens WHERE tenant_id=? AND conversation_id=?',
            (self.tenant, conversation_id)).fetchone()['n']

    def last_message(self, conversation_id, source=None):
        sql = 'SELECT * FROM messages WHERE tenant_id=? AND conversation_id=?'
        args = [self.tenant, conversation_id]
        if source:
            sql += ' AND source=?'
            args.append(source)
        return self.db.conn.execute(sql + ' ORDER BY rowid DESC LIMIT 1', args).fetchone()


class T5NativeRfqEntryTests(_T5BridgeFixture):
    """① 已有原生发送渠道（whatsapp_personal）的 RFQ 入口及其门控。"""

    def test_native_rfq_link_entry_sends_via_existing_bus(self):
        status, _ = self.inbound('861700000101@c.us', '你好，想了解服务安排', '3EB0T5N01')
        self.assertEqual(status, 200)
        conv = self.conversation_for('861700000101@c.us')
        self.assertTrue(conv, 'webhook 入站应建立 whatsapp_personal 会话')
        # 收件箱详情提供「发送询价单」入口（既有渠道补齐，而不是只对 LangBot 平台轨开放）
        status, _, page = self.request('GET', f'/admin/inbox/whatsapp_personal/{conv}', self.cookie_admin)
        self.assertEqual(status, 200)
        self.assertIn(f"/admin/inbox/whatsapp_personal/{conv}/rfq-link", page)
        self.assertIn('发送询价单', page)
        # 点击后经既有出站总线发送：建令牌 + rfq-link 消息 sent + 经本企业 mock 桥真实发出
        tokens_before = self.token_count(conv)
        sends_before = len(self.mock.sent_texts())
        status, headers, _ = self.request(
            'POST', f'/admin/inbox/whatsapp_personal/{conv}/rfq-link', self.cookie_admin, {})
        self.assertEqual(status, 303, '原生渠道应受理询价链接动作')
        self.assertIn('notice=link_sent', headers.get('Location', ''))
        self.assertEqual(self.token_count(conv), tokens_before + 1)
        row = self.last_message(conv, 'rfq-link')
        self.assertIsNotNone(row)
        self.assertEqual(row['delivery_status'], 'sent')
        self.assertIn('/rfq/', row['content'])
        sent = self.mock.sent_texts()[sends_before:]
        self.assertEqual(len(sent), 1, '链接应经企业桥真实发出')
        self.assertEqual(sent[0]['session'], 'business-' + self.account)
        self.assertEqual(sent[0]['chatId'], '861700000101@c.us')
        self.assertIn('/rfq/', sent[0]['text'])

    def test_native_rfq_link_gates_unchanged(self):
        # 停止主动联系：拒绝且不建令牌、不发送
        status, _ = self.inbound('861700000102@c.us', '你好', '3EB0T5N02')
        self.assertEqual(status, 200)
        conv = self.conversation_for('861700000102@c.us')
        self.assertTrue(self.db.stop_proactive_contact(
            self.tenant, conv, '请不要再主动发消息', 'seller_record', self.admin['email']))
        tokens_before = self.token_count(conv)
        sends_before = len(self.mock.sent_texts())
        status, headers, _ = self.request(
            'POST', f'/admin/inbox/whatsapp_personal/{conv}/rfq-link', self.cookie_admin, {})
        self.assertEqual(status, 303)
        self.assertIn('notice=link_stopped', headers.get('Location', ''))
        self.assertEqual(self.token_count(conv), tokens_before)
        self.assertIsNone(self.last_message(conv, 'rfq-link'))
        self.assertEqual(len(self.mock.sent_texts()), sends_before)
        # 发送权限：转交后历史可看、不可代原账号发送（POST 被权限层拒绝）
        status, _ = self.inbound('861700000103@c.us', '你好', '3EB0T5N03')
        conv2 = self.conversation_for('861700000103@c.us')
        self.db.transfer_customer(self.tenant, conv2, self.admin['id'], self.member_id, self.admin['id'])
        status, _, page = self.request('GET', f'/admin/inbox/whatsapp_personal/{conv2}', self.cookie_member)
        self.assertEqual(status, 200)
        self.assertNotIn(f"/admin/inbox/whatsapp_personal/{conv2}/rfq-link", page, '无发送权限不显示入口')
        status, _, _ = self.request(
            'POST', f'/admin/inbox/whatsapp_personal/{conv2}/rfq-link', self.cookie_member, {})
        self.assertEqual(status, 403)
        self.assertEqual(self.token_count(conv2), 0)
        # 渠道边界：不属于该渠道的会话仍拒绝；网站聊天（无外发通道）保持拒绝
        status, _, _ = self.request('POST', f'/admin/inbox/telegram/{conv2}/rfq-link', self.cookie_admin, {})
        self.assertEqual(status, 400)
        web_conv = self.db.create_conversation(self.tenant, 'web', 'en')
        self.db.save_message(self.tenant, web_conv, 'buyer', '网站访客消息', 'website_api')
        status, _, page = self.request('POST', f'/admin/inbox/web/{web_conv}/rfq-link', self.cookie_admin, {})
        self.assertEqual(status, 400)
        self.assertIn('发送通道尚未开通', page)


class T5DeliveryNoticeTests(_T5BridgeFixture):
    """② 送达事实提示：客观口径 + 「已送达」仅由真实回执状态渲染。"""

    def test_nonweb_sent_notice_objective_and_delivered_only_after_receipt(self):
        status, _ = self.inbound('861700000201@c.us', '你好', '3EB0T5W01')
        self.assertEqual(status, 200)
        conv = self.conversation_for('861700000201@c.us')
        status, headers, _ = self.request(
            'POST', f'/admin/inbox/whatsapp_personal/{conv}/reply', self.cookie_admin,
            {'body': 'T4/T5测试服务时间为周二至周四 10:00-16:00。'})
        self.assertEqual(status, 303)
        self.assertIn('notice=sent', headers.get('Location', ''))
        status, _, page = self.request(
            'GET', f'/admin/inbox/whatsapp_personal/{conv}?notice=sent', self.cookie_admin)
        self.assertEqual(status, 200)
        self.assertIn('渠道已接受发送，送达尚未确认', page)  # 客观口径：受理≠送达
        self.assertNotIn('直达买方', page)                  # 发送成功不宣称直送买方
        self.assertIn('已发送', page)
        self.assertNotIn('已送达', page)                    # 无回执不出现已送达
        row = self.last_message(conv, 'staff-reply')
        self.assertEqual(row['delivery_status'], 'sent')
        self.assertTrue(row['external_message_id'])
        # 回执边界：真实回执推进函数（Twilio/Meta 回调同一落点）执行后才渲染「已送达」
        self.assertTrue(mark_outbound_delivered(
            self.db, self.tenant, 'whatsapp_personal', row['external_message_id']))
        status, _, page = self.request('GET', f'/admin/inbox/whatsapp_personal/{conv}', self.cookie_admin)
        self.assertIn('已送达', page)

    def test_platform_reply_area_hint_objective(self):
        conv = self.db.create_conversation(self.tenant, 'langbot', 'en')
        self.db.bind_channel_identity(self.tenant, 'langbot', '88021', 'Telegram 客户', conv)
        self.db.update_channel_identity_meta(self.tenant, 'langbot', '88021', {'adapter': 'Telegram'})
        self.db.save_message(self.tenant, conv, 'buyer', 'hi', 'buyer_web',
                             channel='langbot', external_message_id='lb-t5-1')
        status, _, page = self.request('GET', f'/admin/inbox/telegram/{conv}', self.cookie_admin)
        self.assertEqual(status, 200)
        self.assertNotIn('直达买方', page)                   # 平台轨回复区不再宣称直送买方
        self.assertIn('发送失败会标记并可重试', page)


if __name__ == '__main__':
    unittest.main(verbosity=2)
