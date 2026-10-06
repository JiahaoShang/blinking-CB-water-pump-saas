"""V1-04 外部渠道接入（切片1）测试：数据层 / 权限与导航 / 渠道接入页 / 双渠道模拟服务。

方案口径（grill-me 两轮定稿）：
- 渠道消息与网页聊天共用同一批会话/消息对象；按（渠道, 外部消息ID）唯一去重；
- 渠道账户只存公开配置；机密凭证只从本机环境变量读取（页面不回显值）；
- 功能默认关闭；「会话收件箱」= 一个可授权模块、三个导航子模块（网站聊天 / Telegram / WhatsApp）；
- RFQ 填写链接只存摘要、可撤销；每会话「暂停 AI 自动回复」开关默认不暂停。
"""
import http.client
import json
import os
import sqlite3
import sys
import tempfile
import threading
import unittest
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools" / "channels"))

from app import (  # noqa: E402
    AUDIT_ACTION_LABELS,
    AUDIT_ENTITY_LABELS,
    CHANNEL_KEYS,
    GRANTABLE_MODULE_KEYS,
    Database,
    channel_secret_status,
    make_server,
)
import mock_telegram  # noqa: E402
import mock_whatsapp  # noqa: E402


def form_request(server, method, path, fields=None, cookie=None):
    conn = http.client.HTTPConnection("127.0.0.1", server.server_port)
    headers = {}
    body = None
    if fields is not None:
        body = urllib.parse.urlencode(fields)
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    if cookie:
        headers["Cookie"] = cookie
    conn.request(method, path, body=body, headers=headers)
    response = conn.getresponse()
    raw = response.read().decode("utf-8", errors="replace")
    headers_out = dict(response.getheaders())
    status = response.status
    conn.close()
    return status, headers_out, raw


def login(server, email, password):
    status, headers, _ = form_request(server, "POST", "/login", {"email": email, "password": password})
    if status not in (302, 303) or "Set-Cookie" not in headers:
        raise AssertionError(f"登录失败: {email} -> {status}")
    return headers["Set-Cookie"].split(";", 1)[0]


def json_request(method, url, payload=None):
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"}, method=method)
    with urllib.request.urlopen(request, timeout=5) as response:
        return response.status, json.loads(response.read().decode("utf-8"))


class _CaptureServer:
    """最小回执接收器：记录收到的 POST（用于验证模拟 Twilio 的出站行为与签名）。"""

    def __init__(self):
        state = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length).decode("utf-8")
                state.requests.append({"path": self.path, "headers": dict(self.headers), "body": raw})
                self.send_response(200)
                self.send_header("Content-Length", "2")
                self.end_headers()
                self.wfile.write(b"ok")

        self.requests = []
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self._server.server_address[1]}"
        threading.Thread(target=self._server.serve_forever, daemon=True).start()

    def last(self, path):
        return next((item for item in reversed(self.requests) if item["path"] == path), None)

    def stop(self):
        self._server.shutdown()
        self._server.server_close()


class ChannelDataTests(unittest.TestCase):
    """数据层：建表与旧库迁移、渠道账户、身份绑定、消息去重、RFQ 链接、AI 开关。"""

    maxDiff = None

    @classmethod
    def setUpClass(cls):
        cls.temp_dir = tempfile.TemporaryDirectory()
        cls.db = Database(Path(cls.temp_dir.name) / "channels.db")
        cls.db.ensure_tenant("demo-tenant", "演示企业")
        cls.db.ensure_tenant("other-tenant", "另一家企业")

    @classmethod
    def tearDownClass(cls):
        cls.temp_dir.cleanup()

    def test_schema_fresh_and_legacy_migration(self):
        expected_tables = {"channel_accounts", "channel_identities", "rfq_form_tokens"}
        tables = {row["name"] for row in self.db.conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertTrue(expected_tables <= tables)
        message_columns = {row["name"] for row in self.db.conn.execute("PRAGMA table_info(messages)")}
        self.assertTrue({"channel", "external_message_id", "delivery_status", "delivery_error", "actor_email"} <= message_columns)
        conversation_columns = {row["name"] for row in self.db.conn.execute("PRAGMA table_info(conversations)")}
        self.assertIn("ai_paused", conversation_columns)
        indexes = {row["name"] for row in self.db.conn.execute("SELECT name FROM sqlite_master WHERE type='index'")}
        self.assertIn("idx_messages_external", indexes)
        # 旧库迁移：先造一个只有旧列的最小库，再打开 → 新列与新索引自动补齐；重复打开幂等。
        legacy_path = Path(self.temp_dir.name) / "legacy.db"
        legacy = sqlite3.connect(legacy_path)
        legacy.executescript(
            """
            CREATE TABLE conversations (
                id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, source TEXT NOT NULL DEFAULT 'web',
                locale TEXT NOT NULL DEFAULT 'en', status TEXT NOT NULL DEFAULT 'active',
                stop_proactive_contact INTEGER NOT NULL DEFAULT 0, stop_reason TEXT NOT NULL DEFAULT '',
                stop_source TEXT NOT NULL DEFAULT '', stopped_at TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            CREATE TABLE messages (
                id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, conversation_id TEXT NOT NULL,
                role TEXT NOT NULL, content TEXT NOT NULL, source TEXT NOT NULL DEFAULT 'buyer_web', created_at TEXT NOT NULL
            );
            """
        )
        legacy.commit()
        legacy.close()
        migrated = Database(legacy_path)
        columns = {row["name"] for row in migrated.conn.execute("PRAGMA table_info(messages)")}
        self.assertTrue({"channel", "external_message_id", "delivery_status", "delivery_error", "actor_email"} <= columns)
        self.assertIn("ai_paused", {row["name"] for row in migrated.conn.execute("PRAGMA table_info(conversations)")})
        self.assertIn(
            "idx_messages_external",
            {row["name"] for row in migrated.conn.execute("SELECT name FROM sqlite_master WHERE type='index'")},
        )
        Database(legacy_path)  # 再次打开不报错（幂等）

    def test_channel_account_upsert_unique_isolation_and_audit(self):
        self.db.save_channel_account("demo-tenant", "telegram", {"bot_username": "@First"}, "demo-admin@example.invalid")
        self.db.save_channel_account("demo-tenant", "telegram", {"bot_username": "@Second"}, "demo-admin@example.invalid")
        accounts = self.db.list_channel_accounts("demo-tenant")
        self.assertEqual(len(accounts), 1)  # upsert：同租户同渠道只有一行
        self.assertEqual(json.loads(accounts["telegram"]["config_json"])["bot_username"], "@Second")
        self.assertEqual(self.db.get_channel_account("other-tenant", "telegram"), None)
        with self.assertRaises(ValueError):
            self.db.save_channel_account("demo-tenant", "carrier-pigeon", {}, "demo-admin@example.invalid")
        # 默认关闭 + 启停审计
        self.assertEqual(self.db.get_channel_account("demo-tenant", "telegram")["status"], "disabled")
        self.db.set_channel_enabled("demo-tenant", "telegram", True, "demo-admin@example.invalid")
        self.assertEqual(self.db.get_channel_account("demo-tenant", "telegram")["status"], "enabled")
        self.db.set_channel_enabled("demo-tenant", "telegram", False, "demo-admin@example.invalid")
        self.assertEqual(self.db.get_channel_account("demo-tenant", "telegram")["status"], "disabled")
        actions = [
            row["action"]
            for row in self.db.conn.execute(
                "SELECT action FROM audit_log WHERE tenant_id='demo-tenant' AND entity_type='channel' ORDER BY id"
            )
        ]
        self.assertEqual(actions, ["channel_configured", "channel_configured", "channel_enabled", "channel_disabled"])

    def test_channel_identity_binding_and_message_dedupe(self):
        conversation_id = self.db.create_conversation("demo-tenant", "telegram", "en")
        identity_id = self.db.bind_channel_identity("demo-tenant", "telegram", "88001", "Байер", conversation_id)
        self.assertTrue(identity_id)
        identity = self.db.get_channel_identity("demo-tenant", "telegram", "88001")
        self.assertEqual(identity["conversation_id"], conversation_id)
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.bind_channel_identity("demo-tenant", "telegram", "88001", "重复", conversation_id)
        self.assertEqual(self.db.get_channel_identity("other-tenant", "telegram", "88001"), None)
        # 渠道消息去重：同渠道同一外部消息 ID 只落一次；不同渠道互不影响
        self.db.save_message("demo-tenant", conversation_id, "buyer", "flow 50", channel="telegram", external_message_id="9001")
        self.assertIsNotNone(self.db.find_message_by_external("demo-tenant", "telegram", "9001"))
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.save_message("demo-tenant", conversation_id, "buyer", "重复投递", channel="telegram", external_message_id="9001")
        self.db.save_message("demo-tenant", conversation_id, "buyer", "同号不同渠道", channel="whatsapp", external_message_id="9001")
        row = self.db.find_message_by_external("demo-tenant", "telegram", "9001")
        self.assertEqual(row["content"], "flow 50")

    def test_rfq_form_token_and_ai_pause_switch(self):
        conversation_id = self.db.create_conversation("demo-tenant", "whatsapp", "en")
        token = self.db.create_rfq_form_token("demo-tenant", conversation_id, "demo-admin@example.invalid")
        row = self.db.get_rfq_form_token("demo-tenant", token)
        self.assertIsNotNone(row)
        self.assertNotEqual(row["token_hash"], token)  # 只存摘要
        self.assertEqual(len(row["token_hash"]), 64)
        self.assertIsNone(self.db.get_rfq_form_token("other-tenant", token))
        self.db.revoke_rfq_form_token("demo-tenant", row["id"], "demo-admin@example.invalid")
        self.assertIsNone(self.db.get_rfq_form_token("demo-tenant", token))
        # AI 暂停开关（默认不暂停；人工介入不自动关闭）
        self.assertEqual(self.db.get_conversation("demo-tenant", conversation_id)["ai_paused"], 0)
        self.db.set_conversation_ai_paused("demo-tenant", conversation_id, True, "demo-admin@example.invalid")
        self.assertEqual(self.db.get_conversation("demo-tenant", conversation_id)["ai_paused"], 1)
        self.db.set_conversation_ai_paused("demo-tenant", conversation_id, False, "demo-admin@example.invalid")
        self.assertEqual(self.db.get_conversation("demo-tenant", conversation_id)["ai_paused"], 0)
        actions = [
            row["action"]
            for row in self.db.conn.execute(
                "SELECT action FROM audit_log WHERE tenant_id='demo-tenant' AND entity_id=? AND entity_type='conversation' ORDER BY id",
                (conversation_id,),
            )
        ]
        filtered = [item for item in actions if item in ("rfq_form_created", "conversation_ai_switch")]
        self.assertEqual(filtered, ["rfq_form_created", "conversation_ai_switch", "conversation_ai_switch"])

    def test_audit_labels_cover_channel_actions(self):
        expected = {
            "channel_configured": "保存渠道配置",
            "channel_enabled": "启用渠道",
            "channel_disabled": "停用渠道",
            "conversation_ai_switch": "切换AI自动回复",
            "rfq_form_created": "生成询价表单链接",
        }
        for action, label in expected.items():
            self.assertEqual(AUDIT_ACTION_LABELS.get(action), label, action)
        self.assertEqual(AUDIT_ENTITY_LABELS.get("channel"), "渠道")
        self.assertIn("inbox", GRANTABLE_MODULE_KEYS)


class ChannelConsoleTests(unittest.TestCase):
    """控制台：导航子模块、权限门禁、渠道接入页、凭证状态不回显。"""

    maxDiff = None

    @classmethod
    def setUpClass(cls):
        cls.temp_dir = tempfile.TemporaryDirectory()
        cls.server = make_server(Path(cls.temp_dir.name) / "app.db", port=0)
        cls.db = cls.server.RequestHandlerClass.db
        cls.db.seed_demo_users()
        cls.db.ensure_tenant("other-tenant", "另一家企业")
        cls.db.create_user("demo-tenant", "v104-inbox@example.invalid", "收件箱员工", "employee", "pass-inbox-1", "demo-admin@example.invalid")
        inbox_row = cls.db.conn.execute("SELECT id FROM users WHERE email=?", ("v104-inbox@example.invalid",)).fetchone()
        cls.db.set_user_modules("demo-tenant", inbox_row["id"], ["inbox"], "demo-admin@example.invalid")
        cls.db.create_user("demo-tenant", "v104-nomod@example.invalid", "零权限员工", "employee", "pass-nomod-1", "demo-admin@example.invalid")
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.admin = login(cls.server, "demo-admin@example.invalid", "demo-admin")
        cls.inbox_emp = login(cls.server, "v104-inbox@example.invalid", "pass-inbox-1")
        cls.nomod = login(cls.server, "v104-nomod@example.invalid", "pass-nomod-1")

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.temp_dir.cleanup()

    def setUp(self):
        # 凭证环境变量在用例内受控：默认清空，避免本机残留影响断言。
        self._env_names = ("TELEGRAM_BOT_TOKEN", "TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_WHATSAPP_FROM")
        self._saved_env = {name: os.environ.pop(name, None) for name in self._env_names}

    def tearDown(self):
        for name, value in self._saved_env.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value

    def test_nav_modules_and_channels_entry(self):
        status, _, page = form_request(self.server, "GET", "/admin", cookie=self.admin)
        self.assertEqual(status, 200)
        for text in ("会话收件箱", "网站聊天", "Telegram", "WhatsApp", "渠道接入"):
            self.assertIn(text, page)
        status, _, page = form_request(self.server, "GET", "/admin", cookie=self.inbox_emp)
        self.assertEqual(status, 200)
        self.assertIn("会话收件箱", page)
        self.assertIn("/admin/inbox/telegram", page)
        self.assertNotIn("渠道接入", page)  # 渠道接入仅管理员可见
        status, _, page = form_request(self.server, "GET", "/admin", cookie=self.nomod)
        self.assertNotIn("会话收件箱", page)

    def test_inbox_pages_and_permission(self):
        # 渠道状态在用例间共享：先统一关到「未启用」，避免依赖执行顺序。
        self.db.set_channel_enabled("demo-tenant", "telegram", False, "setup")
        self.db.set_channel_enabled("demo-tenant", "whatsapp", False, "setup")
        for section, marker in (("web", "网站聊天"), ("telegram", "渠道未启用"), ("whatsapp", "渠道未启用")):
            status, _, page = form_request(self.server, "GET", f"/admin/inbox/{section}", cookie=self.inbox_emp)
            self.assertEqual(status, 200, section)
            self.assertIn(marker, page)
        status, _, body = form_request(self.server, "GET", "/admin/inbox/web", cookie=self.nomod)
        self.assertEqual(status, 403)
        self.assertIn("会话收件箱", body)
        # 管理员可见全部三个子模块
        for section in ("web", "telegram", "whatsapp"):
            status, _, _ = form_request(self.server, "GET", f"/admin/inbox/{section}", cookie=self.admin)
            self.assertEqual(status, 200, section)
        status, _, _ = form_request(self.server, "GET", "/admin/inbox/nonsense", cookie=self.admin)
        self.assertEqual(status, 404)

    def test_channels_page_admin_only_render_and_actions(self):
        status, _, _ = form_request(self.server, "GET", "/admin/channels", cookie=self.inbox_emp)
        self.assertEqual(status, 403)
        status, _, page = form_request(self.server, "GET", "/admin/channels", cookie=self.admin)
        self.assertEqual(status, 200)
        for text in ("Telegram", "WhatsApp", "凭证未配置", "TELEGRAM_BOT_TOKEN", "TWILIO_ACCOUNT_SID", "默认关闭", "渠道接入"):
            self.assertIn(text, page)
        # 保存配置（管理员）→ 审计 + 页面回显
        status, headers, _ = form_request(self.server, "POST", "/admin/channels/telegram", {"bot_username": "@SellerBot"}, self.admin)
        self.assertEqual(status, 303)
        self.assertIn("notice=saved", headers.get("Location", ""))
        status, _, page = form_request(self.server, "GET", "/admin/channels", cookie=self.admin)
        self.assertIn("@SellerBot", page)
        # 员工不能改
        status, _, _ = form_request(self.server, "POST", "/admin/channels/telegram", {"bot_username": "@Hack"}, self.inbox_emp)
        self.assertEqual(status, 403)
        # 启停 + 审计
        status, headers, _ = form_request(self.server, "POST", "/admin/channels/telegram/toggle", {}, self.admin)
        self.assertEqual(status, 303)
        self.assertIn("notice=enabled", headers.get("Location", ""))
        status, _, page = form_request(self.server, "GET", "/admin/channels", cookie=self.admin)
        self.assertIn("已启用", page)
        status, headers, _ = form_request(self.server, "POST", "/admin/channels/telegram/toggle", {}, self.admin)
        self.assertIn("notice=disabled", headers.get("Location", ""))
        actions = [
            row["action"]
            for row in self.db.conn.execute(
                "SELECT action FROM audit_log WHERE tenant_id='demo-tenant' AND entity_type='channel' ORDER BY id"
            )
        ]
        self.assertEqual(actions, ["channel_configured", "channel_enabled", "channel_disabled"])
        # 渠道启用后的收件箱状态联动（无凭证 → 如实提示缺少的变量名）
        self.db.set_channel_enabled("demo-tenant", "telegram", True, "demo-admin@example.invalid")
        status, _, page = form_request(self.server, "GET", "/admin/inbox/telegram", cookie=self.inbox_emp)
        self.assertIn("凭证未配置", page)
        self.assertIn("TELEGRAM_BOT_TOKEN", page)

    def test_secrets_status_without_leaking_values(self):
        os.environ["TELEGRAM_BOT_TOKEN"] = "sk-telegram-secret-xyz"
        status, _, page = form_request(self.server, "GET", "/admin/channels", cookie=self.admin)
        self.assertEqual(status, 200)
        self.assertIn("凭证已配置", page)
        self.assertNotIn("sk-telegram-secret-xyz", page)
        self.assertTrue(channel_secret_status("telegram")["configured"])
        whatsapp = channel_secret_status("whatsapp")
        self.assertFalse(whatsapp["configured"])
        self.assertEqual(set(whatsapp["missing"]), {"TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_WHATSAPP_FROM"})

    def test_member_management_module_checkbox(self):
        status, _, page = form_request(self.server, "GET", "/admin/users", cookie=self.admin)
        self.assertEqual(status, 200)
        self.assertIn("module_inbox", page)
        self.assertIn("会话收件箱", page)


class ChannelMockTests(unittest.TestCase):
    """模拟服务：Telegram 轮询/发送往返；Twilio 发送/入站签名/状态回调。"""

    def test_mock_telegram_roundtrip(self):
        server = mock_telegram.MockTelegramServer()
        try:
            base = server.base_url()
            token = "123:TEST"
            server.inject_message(77, "flow 50 m3/h", first_name="Maya")
            status, payload = json_request("POST", f"{base}{token}/getUpdates", {"offset": 0, "timeout": 0})
            self.assertEqual(status, 200)
            self.assertTrue(payload["ok"])
            self.assertEqual(payload["result"][0]["message"]["text"], "flow 50 m3/h")
            self.assertEqual(payload["result"][0]["message"]["from"]["first_name"], "Maya")
            status, payload = json_request("POST", f"{base}{token}/sendMessage", {"chat_id": 77, "text": "Hello from seller"})
            self.assertTrue(payload["ok"])
            self.assertTrue(payload["result"]["message_id"])
            sent = server.sent_messages()
            self.assertEqual(len(sent), 1)
            self.assertEqual(sent[0]["text"], "Hello from seller")
            status, payload = json_request("POST", f"{base}{token}/setWebhook", {"url": "https://example.test/tg"})
            self.assertTrue(payload["ok"])
            self.assertEqual(server.webhook_url(), "https://example.test/tg")
        finally:
            server.stop()

    def test_mock_twilio_send_inbound_signature_and_status(self):
        server = mock_whatsapp.MockTwilioServer(auth_token="unit-secret")
        receiver = _CaptureServer()
        try:
            form = urllib.parse.urlencode(
                {
                    "To": "whatsapp:+491234",
                    "From": "whatsapp:+14155238886",
                    "Body": "buyer hello",
                    "StatusCallback": f"{receiver.url}/status",
                }
            ).encode("utf-8")
            request = urllib.request.Request(
                f"{server.base_url()}/2010-04-01/Accounts/ACmock/Messages.json",
                data=form,
                headers={"Content-Type": "application/x-www-form-urlencoded", "Authorization": "Basic QUNtb2NrOnVuaXQtc2VjcmV0"},
                method="POST",
            )
            with urllib.request.urlopen(request, timeout=5) as response:
                self.assertEqual(response.status, 201)
                payload = json.loads(response.read().decode("utf-8"))
            sid = payload["sid"]
            self.assertTrue(sid.startswith("SM"))
            sent = server.sent_messages()
            self.assertEqual(sent[0]["body"], "buyer hello")
            # 入站消息：POST 到回执地址，签名可用同一算法验真
            status, _ = server.inject_inbound("whatsapp:+491234", "你好，有货吗", f"{receiver.url}/inbound")
            self.assertEqual(status, 200)
            captured = receiver.last("/inbound")
            self.assertIsNotNone(captured)
            params = {key: values[0] for key, values in urllib.parse.parse_qs(captured["body"]).items()}
            self.assertEqual(params["Body"], "你好，有货吗")
            self.assertEqual(params["From"], "whatsapp:+491234")
            expected = mock_whatsapp.twilio_signature("unit-secret", f"{receiver.url}/inbound", params)
            self.assertEqual(captured["headers"].get("X-Twilio-Signature"), expected)
            # 状态回调（发送回执）
            self.assertTrue(server.trigger_status(sid, "delivered"))
            delivered = receiver.last("/status")
            self.assertIsNotNone(delivered)
            status_params = {key: values[0] for key, values in urllib.parse.parse_qs(delivered["body"]).items()}
            self.assertEqual(status_params["MessageStatus"], "delivered")
            self.assertEqual(status_params["MessageSid"], sid)
        finally:
            receiver.stop()
            server.stop()


if __name__ == "__main__":
    unittest.main()
