"""V1-04 外部渠道接入测试（LangBot 单轨 + WhatsApp 底座）：数据层 / 权限与导航 / 渠道接入页 / 模拟服务 / 收件箱 / 出站总线。

背景：旧 Telegram 直连已于 2026-10 按计划撤除（代码/工具/专属用例一并退役，入口统一为 LangBot 单轨）。
本文件保留的覆盖面：共享渠道数据层、控制台与权限、收件箱通用行为、出站总线与 F-22 状态机、
WhatsApp（Twilio）模拟器与公开 RFQ 填写页；业务全链路（需求确认/匹配、RFQ 链接往返、坐席回复直达）
改由 LangBot 传输轨道承载（test_langbot_bridge 提供模拟器）。

口径（原 grill-me 定稿不变）：
- 渠道消息与网页聊天共用同一批会话/消息对象；按（渠道, 外部消息ID）唯一去重；
- 渠道账户只存公开配置；机密凭证只从本机环境变量读取（页面不回显值）；
- 功能默认关闭；「会话收件箱」= 一个可授权模块、三个导航子模块（网站聊天 / WhatsApp / LangBot）；
- RFQ 填写链接只存摘要、可撤销；每会话「暂停 AI 自动回复」开关默认不暂停。
"""
import http.client
import json
import os
import re
import sqlite3
import subprocess
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
    AI_PAUSED_ANSWER,
    AUDIT_ACTION_LABELS,
    AUDIT_ENTITY_LABELS,
    CHANNEL_KEYS,
    DEFAULT_RFQ_FIELDS,
    GRANTABLE_MODULE_KEYS,
    Database,
    channel_secret_status,
    dispatch_outbound,
    enqueue_outbound,
    make_server,
    mark_outbound_delivered,
    retry_outbound_message,
)
import mock_whatsapp  # noqa: E402
from test_langbot_bridge import _LangBotMock  # noqa: E402
from app import _LANGBOT_PLATFORMS_CACHE  # noqa: E402


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


def json_http(server, method, path, payload=None, headers=None):
    """对测试服务发 JSON 请求（渠道 Webhook / 买家 API 等）。"""
    conn = http.client.HTTPConnection("127.0.0.1", server.server_port)
    request_headers = {"Content-Type": "application/json", **(headers or {})}
    conn.request(method, path, json.dumps(payload).encode("utf-8") if payload is not None else None, request_headers)
    response = conn.getresponse()
    data = response.read()
    result = response.status, json.loads(data.decode("utf-8")) if data else None
    conn.close()
    return result


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
        self.db.save_channel_account("demo-tenant", "langbot", {"bot_uuid": "bot-first"}, "demo-admin@example.invalid")
        self.db.save_channel_account("demo-tenant", "langbot", {"bot_uuid": "bot-second"}, "demo-admin@example.invalid")
        accounts = self.db.list_channel_accounts("demo-tenant")
        self.assertEqual(len(accounts), 1)  # upsert：同租户同渠道只有一行
        self.assertEqual(json.loads(accounts["langbot"]["config_json"])["bot_uuid"], "bot-second")
        self.assertEqual(self.db.get_channel_account("other-tenant", "langbot"), None)
        with self.assertRaises(ValueError):
            self.db.save_channel_account("demo-tenant", "carrier-pigeon", {}, "demo-admin@example.invalid")
        # 默认关闭 + 启停审计
        self.assertEqual(self.db.get_channel_account("demo-tenant", "langbot")["status"], "disabled")
        self.db.set_channel_enabled("demo-tenant", "langbot", True, "demo-admin@example.invalid")
        self.assertEqual(self.db.get_channel_account("demo-tenant", "langbot")["status"], "enabled")
        self.db.set_channel_enabled("demo-tenant", "langbot", False, "demo-admin@example.invalid")
        self.assertEqual(self.db.get_channel_account("demo-tenant", "langbot")["status"], "disabled")
        actions = [
            row["action"]
            for row in self.db.conn.execute(
                "SELECT action FROM audit_log WHERE tenant_id='demo-tenant' AND entity_type='channel' ORDER BY id"
            )
        ]
        self.assertEqual(actions, ["channel_configured", "channel_configured", "channel_enabled", "channel_disabled"])

    def test_channel_identity_binding_and_message_dedupe(self):
        conversation_id = self.db.create_conversation("demo-tenant", "langbot", "en")
        identity_id = self.db.bind_channel_identity("demo-tenant", "langbot", "88001", "Байер", conversation_id)
        self.assertTrue(identity_id)
        identity = self.db.get_channel_identity("demo-tenant", "langbot", "88001")
        self.assertEqual(identity["conversation_id"], conversation_id)
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.bind_channel_identity("demo-tenant", "langbot", "88001", "重复", conversation_id)
        self.assertEqual(self.db.get_channel_identity("other-tenant", "langbot", "88001"), None)
        # 渠道消息去重：同渠道同一外部消息 ID 只落一次；不同渠道互不影响
        self.db.save_message("demo-tenant", conversation_id, "buyer", "flow 50", channel="langbot", external_message_id="9001")
        self.assertIsNotNone(self.db.find_message_by_external("demo-tenant", "langbot", "9001"))
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.save_message("demo-tenant", conversation_id, "buyer", "重复投递", channel="langbot", external_message_id="9001")
        self.db.save_message("demo-tenant", conversation_id, "buyer", "同号不同渠道", channel="whatsapp", external_message_id="9001")
        row = self.db.find_message_by_external("demo-tenant", "langbot", "9001")
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
        # 2026-10 撤除旧 Telegram 直连后：渠道集合收敛为 WhatsApp + LangBot 单轨
        self.assertEqual(CHANNEL_KEYS, ("whatsapp", "langbot"))


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
        self._env_names = ("LANGBOT_API_KEY", "TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_WHATSAPP_FROM")
        self._saved_env = {name: os.environ.pop(name, None) for name in self._env_names}
        _LANGBOT_PLATFORMS_CACHE["at"] = 0.0  # LangBot 平台接入缓存清零，保证断言确定性

    def tearDown(self):
        for name, value in self._saved_env.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value

    def test_nav_modules_and_channels_entry(self):
        status, _, page = form_request(self.server, "GET", "/admin", cookie=self.admin)
        self.assertEqual(status, 200)
        for text in ("会话收件箱", "网站聊天", "WhatsApp", "渠道接入"):
            self.assertIn(text, page)
        status, _, page = form_request(self.server, "GET", "/admin", cookie=self.inbox_emp)
        self.assertEqual(status, 200)
        self.assertIn("会话收件箱", page)
        self.assertIn("/admin/inbox/web", page)
        self.assertIn("/admin/inbox/whatsapp", page)  # 原生渠道模块常显（未接入 → 空态指引）
        self.assertNotIn("/admin/inbox/langbot", page)  # 路由节点不再是收件箱模块
        self.assertNotIn("渠道接入", page)  # 渠道接入仅管理员可见
        status, _, page = form_request(self.server, "GET", "/admin", cookie=self.nomod)
        self.assertNotIn("会话收件箱", page)

    def test_inbox_pages_and_permission(self):
        # 渠道状态在用例间共享：先统一关到「未启用」，避免依赖执行顺序。
        self.db.set_channel_enabled("demo-tenant", "langbot", False, "setup")
        self.db.set_channel_enabled("demo-tenant", "whatsapp", False, "setup")
        # 来源平台重构：旧「LangBot」模块 URL 已退役；WhatsApp 为原生模块（未启用 → 空态指引）
        status, _, _ = form_request(self.server, "GET", "/admin/inbox/langbot", cookie=self.inbox_emp)
        self.assertEqual(status, 404)
        for section, marker in (("web", "网站聊天"), ("whatsapp", "渠道未启用")):
            status, _, page = form_request(self.server, "GET", f"/admin/inbox/{section}", cookie=self.inbox_emp)
            self.assertEqual(status, 200, section)
            self.assertIn(marker, page)
        status, _, body = form_request(self.server, "GET", "/admin/inbox/web", cookie=self.nomod)
        self.assertEqual(status, 403)
        self.assertIn("会话收件箱", body)
        # 造一个 Telegram 平台会话：平台模块从消息里识别并出现在导航与列表
        tg_conv = self.db.create_conversation("demo-tenant", "langbot", "en")
        self.db.bind_channel_identity("demo-tenant", "langbot", "nav-tg-1", "Nav Buyer", tg_conv)
        self.db.update_channel_identity_meta("demo-tenant", "langbot", "nav-tg-1", {"adapter": "Telegram"})
        self.db.save_message("demo-tenant", tg_conv, "buyer", "hi from telegram", "buyer_web", channel="langbot", external_message_id="nav-tg-1")
        for section in ("web", "whatsapp", "telegram"):
            status, _, _ = form_request(self.server, "GET", f"/admin/inbox/{section}", cookie=self.admin)
            self.assertEqual(status, 200, section)
        status, _, page = form_request(self.server, "GET", "/admin", cookie=self.admin)
        self.assertIn("/admin/inbox/telegram", page)
        self.assertIn("i-plat-telegram", page)
        status, _, _ = form_request(self.server, "GET", "/admin/inbox/nonsense", cookie=self.admin)
        self.assertEqual(status, 404)

    def test_channels_page_admin_only_render_and_actions(self):
        status, _, _ = form_request(self.server, "GET", "/admin/channels", cookie=self.inbox_emp)
        self.assertEqual(status, 403)
        status, _, page = form_request(self.server, "GET", "/admin/channels", cookie=self.admin)
        self.assertEqual(status, 200)
        for text in ("LangBot", "WhatsApp", "凭证未配置", "LANGBOT_API_KEY", "TWILIO_ACCOUNT_SID", "默认关闭", "渠道接入"):
            self.assertIn(text, page)
        # 旧直连已撤除：不再有 Telegram 渠道卡与操作入口（历史说明文案除外）
        self.assertNotIn("/admin/channels/telegram", page)
        self.assertNotIn("TELEGRAM_BOT_TOKEN", page)
        # 保存配置（管理员）→ 审计 + 页面回显
        status, headers, _ = form_request(self.server, "POST", "/admin/channels/langbot", {"bot_uuid": "bot-test-1"}, self.admin)
        self.assertEqual(status, 303)
        self.assertIn("notice=saved", headers.get("Location", ""))
        status, _, page = form_request(self.server, "GET", "/admin/channels", cookie=self.admin)
        self.assertIn("bot-test-1", page)
        # 员工不能改
        status, _, _ = form_request(self.server, "POST", "/admin/channels/langbot", {"bot_uuid": "bot-hack"}, self.inbox_emp)
        self.assertEqual(status, 403)
        # 启停 + 审计
        status, headers, _ = form_request(self.server, "POST", "/admin/channels/langbot/toggle", {}, self.admin)
        self.assertEqual(status, 303)
        self.assertIn("notice=enabled", headers.get("Location", ""))
        status, _, page = form_request(self.server, "GET", "/admin/channels", cookie=self.admin)
        self.assertIn("已启用", page)
        status, headers, _ = form_request(self.server, "POST", "/admin/channels/langbot/toggle", {}, self.admin)
        self.assertIn("notice=disabled", headers.get("Location", ""))
        actions = [
            row["action"]
            for row in self.db.conn.execute(
                "SELECT action FROM audit_log WHERE tenant_id='demo-tenant' AND entity_type='channel' ORDER BY id"
            )
        ]
        self.assertEqual(actions, ["channel_configured", "channel_enabled", "channel_disabled"])
        # 渠道启用与来源平台模块解耦：LangBot 是接入通道，不再是收件箱模块；平台模块在出现第一个会话后自动出现。
        self.db.set_channel_enabled("demo-tenant", "langbot", True, "demo-admin@example.invalid")
        status, _, _ = form_request(self.server, "GET", "/admin/inbox/langbot", cookie=self.inbox_emp)
        self.assertEqual(status, 404)
        conv = self.db.create_conversation("demo-tenant", "langbot", "en")
        self.db.bind_channel_identity("demo-tenant", "langbot", "console-tg-1", "Console Buyer", conv)
        self.db.update_channel_identity_meta("demo-tenant", "langbot", "console-tg-1", {"adapter": "Telegram"})
        self.db.save_message("demo-tenant", conv, "buyer", "hello", "buyer_web", channel="langbot", external_message_id="console-tg-1")
        status, _, page = form_request(self.server, "GET", "/admin/inbox/telegram", cookie=self.inbox_emp)
        self.assertEqual(status, 200)
        self.assertIn("Console Buyer", page)

    def test_secrets_status_without_leaking_values(self):
        os.environ["LANGBOT_API_KEY"] = "«redacted:lbk-…»"
        status, _, page = form_request(self.server, "GET", "/admin/channels", cookie=self.admin)
        self.assertEqual(status, 200)
        self.assertIn("凭证已配置", page)
        self.assertNotIn("«redacted:lbk-…»", page)
        self.assertTrue(channel_secret_status("langbot")["configured"])
        whatsapp = channel_secret_status("whatsapp")
        self.assertFalse(whatsapp["configured"])
        self.assertEqual(set(whatsapp["missing"]), {"TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_WHATSAPP_FROM"})

    def test_member_management_module_checkbox(self):
        status, _, page = form_request(self.server, "GET", "/admin/users", cookie=self.admin)
        self.assertEqual(status, 200)
        self.assertIn("module_inbox", page)
        self.assertIn("会话收件箱", page)


class ChannelMockTests(unittest.TestCase):
    """模拟服务：Twilio 发送/入站签名/状态回调（WhatsApp 底座）。"""

    def test_mock_twilio_send_inbound_signature_and_status(self):
        server = mock_whatsapp.MockTwilioServer(auth_token="unit-secret")
        receiver = _CaptureServer()
        try:
            form = urllib.parse.urlencode(
                {
                    "To": "whatsapp:+491234",
                    "From": "whatsapp:+141****8886",
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


class ConversationInboxTests(unittest.TestCase):
    """会话收件箱：列表分渠道、详情标注与回复人、直接回复、AI 开关、权限。"""

    maxDiff = None

    @classmethod
    def setUpClass(cls):
        cls.temp_dir = tempfile.TemporaryDirectory()
        cls.server = make_server(Path(cls.temp_dir.name) / "app.db", port=0)
        cls.db = cls.server.RequestHandlerClass.db
        cls.db.seed_demo_users()
        cls.db.ensure_tenant("other-tenant", "另一家企业")
        cls.db.register_buyer_site("demo-tenant", "site-a", "https://seller-a.example")
        cls.db.create_user("demo-tenant", "v104b-inbox@example.invalid", "收件箱员工", "employee", "pass-inbox-2", "demo-admin@example.invalid")
        inbox_row = cls.db.conn.execute("SELECT id FROM users WHERE email=?", ("v104b-inbox@example.invalid",)).fetchone()
        cls.db.set_user_modules("demo-tenant", inbox_row["id"], ["inbox"], "demo-admin@example.invalid")
        cls.db.create_user("demo-tenant", "v104b-nomod@example.invalid", "零权限员工", "employee", "pass-nomod-2", "demo-admin@example.invalid")
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.admin = login(cls.server, "demo-admin@example.invalid", "demo-admin")
        cls.inbox_emp = login(cls.server, "v104b-inbox@example.invalid", "pass-inbox-2")
        cls.nomod = login(cls.server, "v104b-nomod@example.invalid", "pass-nomod-2")
        # 会话主夹具：买方消息 + AI 有据回答 + 工程师回复（新：带回复人）+ 旧工程师回复（回退匹配任务）+ 转交播报
        cls.web_a = cls.db.create_conversation("demo-tenant", "website_api", "en")
        cls.db.save_message("demo-tenant", cls.web_a, "buyer", "flow 50 m3/h, clean water", "website_api")
        cls.db.save_message("demo-tenant", cls.web_a, "assistant", "流量范围 30-90 m³/h（依据：参数表 第 2 行）", "grounded-answer")
        cls.db.save_message("demo-tenant", cls.web_a, "assistant", "工程师回复：建议核对工况后选型。", "human-engineer", actor_email="eng-new@example.invalid")
        question_message_id = cls.db.save_message("demo-tenant", cls.web_a, "buyer", "还有样品吗？", "website_api")
        cls.db.save_message("demo-tenant", cls.web_a, "assistant", "工程师回复：样品需要确认。", "human-engineer")
        cls.db.conn.execute(
            "INSERT INTO human_tasks(id, tenant_id, conversation_id, question_message_id, question, reason, status, response, assigned_to, created_at, updated_at, resolved_at) "
            "VALUES (?, ?, ?, ?, ?, ?, 'resolved', ?, ?, ?, ?, ?)",
            ("task-old-1", "demo-tenant", cls.web_a, question_message_id, "还有样品吗？", "no_basis", "样品需要确认。", "eng-old@example.invalid", "2026-10-06T00:00:00+00:00", "2026-10-06T00:00:00+00:00", "2026-10-06T00:00:00+00:00"),
        )
        cls.db.conn.commit()
        cls.db.save_message("demo-tenant", cls.web_a, "assistant", "已转交工程师核实——答复会直接出现在这里。", "human-request:task-old-1")
        # 关联询价（列表联系方式与详情关联业务用）
        cls.rfq_id, _created = cls.db.create_rfq(
            "demo-tenant", cls.web_a, "sub-inbox-1",
            {"contact_email": "buyer@example.invalid", "contact_phone": "+491234", "quantity": "2", "region": "Germany"},
            "请报价",
        )
        # 第二个网站会话（列表排序用）与渠道会话、演示夹具会话（应被排除）
        cls.web_b = cls.db.create_conversation("demo-tenant", "web", "en")
        cls.db.save_message("demo-tenant", cls.web_b, "buyer", "hello from second visitor", "website_api")
        cls.langbot_conv = cls.db.create_conversation("demo-tenant", "langbot", "en")
        cls.db.bind_channel_identity("demo-tenant", "langbot", "88002", "Maya (LangBot)", cls.langbot_conv)
        cls.db.update_channel_identity_meta("demo-tenant", "langbot", "88002", {"adapter": "Telegram"})
        cls.db.save_message("demo-tenant", cls.langbot_conv, "buyer", "hi from langbot", "buyer_web", channel="langbot", external_message_id="lb-1")
        # 未识别来源（旧数据无平台元数据）→ 归入「其他来源」模块（迁移兜底）
        cls.other_conv = cls.db.create_conversation("demo-tenant", "langbot", "en")
        cls.db.bind_channel_identity("demo-tenant", "langbot", "88003", "Older Buyer", cls.other_conv)
        cls.db.save_message("demo-tenant", cls.other_conv, "buyer", "hi from older import", "buyer_web", channel="langbot", external_message_id="lb-2")
        _LANGBOT_PLATFORMS_CACHE["at"] = 0.0
        cls.fixture_conv = cls.db.create_conversation("demo-tenant", "demo-fixture", "en")

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.temp_dir.cleanup()

    @classmethod
    def _buyer_token(cls, conversation_id):
        cache = getattr(cls, "_token_cache", None)
        if cache is None:
            cache = cls._token_cache = {}
        if conversation_id not in cache:
            cache[conversation_id] = cls.db.issue_buyer_access("demo-tenant", conversation_id)
        return cache[conversation_id]

    def test_inbox_ai_toggle_and_question_suppression(self):
        # 暂停 AI
        status, headers, _ = form_request(self.server, "POST", f"/admin/inbox/web/{self.web_a}/ai-toggle", {}, self.inbox_emp)
        self.assertEqual(status, 303)
        self.assertIn("notice=ai_paused", headers.get("Location", ""))
        self.assertEqual(self.db.get_conversation("demo-tenant", self.web_a)["ai_paused"], 1)
        token = self._buyer_token(self.web_a)
        headers_api = {"X-Site-Key": "site-a", "Authorization": f"Bearer {token}", "Idempotency-Key": "q-paused-1"}
        status, body = json_http(self.server, "POST", f"/api/v1/buyer/conversations/{self.web_a}/questions", {"question": "what is the flow range?"}, headers_api)
        self.assertEqual(status, 201)
        self.assertEqual(body["status"], "needs_human")
        self.assertEqual(body["answer"], AI_PAUSED_ANSWER)
        # 恢复 AI：同样问题不再返回「已暂停」文案（无产品语境 → 走常规兜底）
        status, headers, _ = form_request(self.server, "POST", f"/admin/inbox/web/{self.web_a}/ai-toggle", {}, self.inbox_emp)
        self.assertIn("notice=ai_resumed", headers.get("Location", ""))
        self.assertEqual(self.db.get_conversation("demo-tenant", self.web_a)["ai_paused"], 0)
        status, body = json_http(self.server, "POST", f"/api/v1/buyer/conversations/{self.web_a}/questions", {"question": "what is the flow range?"}, {**headers_api, "Idempotency-Key": "q-resumed-1"})
        self.assertEqual(status, 201)
        self.assertNotEqual(body["answer"], AI_PAUSED_ANSWER)
        # 审计：两次切换可见
        switches = [
            row["details"]
            for row in self.db.conn.execute(
                "SELECT details FROM audit_log WHERE tenant_id='demo-tenant' AND action='conversation_ai_switch' AND entity_id=? ORDER BY id",
                (self.web_a,),
            )
        ]
        self.assertEqual(switches, ["paused=1", "paused=0"])

    def test_inbox_detail_labels_and_responders(self):
        status, _, page = form_request(self.server, "GET", f"/admin/inbox/web/{self.web_a}", cookie=self.inbox_emp)
        self.assertEqual(status, 200)
        self.assertIn("买方", page)
        self.assertIn("AI · 有据回答", page)
        self.assertIn("人工 · 工程师（eng-new@example.invalid）", page)
        self.assertIn("人工 · 工程师（eng-old@example.invalid）", page)  # 旧消息经任务回退还原回复人
        self.assertIn("系统 · 已转交工程师", page)
        self.assertNotIn("工程师回复：建议核对工况后选型", page)  # 前缀剥离，以标签+正文呈现
        self.assertIn("建议核对工况后选型。", page)
        # 新聊天界面：AI 开关为 iOS 滑块（开启态=checked）+ 聊天容器结构
        self.assertIn("AI 自动回复", page)
        self.assertIn("<input type='checkbox' checked", page)
        self.assertIn("class='chat-log'", page)
        self.assertIn("class='chat-composer'", page)
        # 关联业务：询价单链接
        self.assertIn(f"/admin/rfqs/{self.rfq_id}", page)
        self.assertIn("询价单", page)
        # 页面内的链接/表单必须用 URL 段 slug（web），不能把内部导航键拼进地址（走查抓到的缺陷）
        self.assertIn(f"/admin/inbox/web/{self.web_a}/reply", page)
        self.assertIn(f"/admin/inbox/web/{self.web_a}/ai-toggle", page)
        self.assertIn("href='/admin/inbox/web'", page)
        self.assertNotIn("/admin/inbox/inbox_web", page)  # 坏的 URL 形态（导航徽标 data-nav-count 不算）

    def test_inbox_lists_filter_and_preview(self):
        # 制造确定性排序：把 web_b 的“最后活动”推到未来时间（避免同秒并列按 id 兜底）
        self.db.save_message("demo-tenant", self.web_b, "buyer", "second visitor again", "website_api")
        self.db.conn.execute("UPDATE conversations SET updated_at='2099-01-01T00:00:00+00:00' WHERE id=?", (self.web_b,))
        self.db.conn.commit()
        status, _, page = form_request(self.server, "GET", "/admin/inbox/web", cookie=self.inbox_emp)
        self.assertEqual(status, 200)
        self.assertIn("网站访客", page)
        self.assertIn("buyer@example.invalid", page)  # 联系方式来自关联询价
        self.assertNotIn(self.langbot_conv[:6], page)  # 渠道会话不进网站聊天列表
        self.assertNotIn(self.fixture_conv[:6], page)  # 演示夹具不进收件箱
        self.assertIn(self.web_b[:6], page)
        self.assertIn(self.web_a[:6], page)
        self.assertTrue(page.find(self.web_b[:6]) < page.find(self.web_a[:6]))  # 最新在前
        self.assertIn(f"/admin/inbox/web/{self.web_b}", page)  # 列表链接用 URL 段 slug
        # Telegram 平台列表：显示绑定买方名，不含网站会话；图标为平台自身样式
        status, _, page = form_request(self.server, "GET", "/admin/inbox/telegram", cookie=self.inbox_emp)
        self.assertEqual(status, 200)
        self.assertIn("Maya (LangBot)", page)
        self.assertIn("i-plat-telegram", page)
        self.assertNotIn(self.web_a[:6], page)
        # 未识别来源 → 「其他来源」模块
        status, _, page = form_request(self.server, "GET", "/admin/inbox/other", cookie=self.inbox_emp)
        self.assertEqual(status, 200)
        self.assertIn("Older Buyer", page)
        self.assertIn("i-plat-generic", page)
        # WhatsApp：原生模块常显 → 未启用空态
        status, _, page = form_request(self.server, "GET", "/admin/inbox/whatsapp", cookie=self.inbox_emp)
        self.assertEqual(status, 200)
        self.assertIn("渠道未启用", page)

    def test_inbox_permissions_isolation(self):
        status, _, _ = form_request(self.server, "GET", "/admin/inbox/web", cookie=self.nomod)
        self.assertEqual(status, 403)
        status, _, _ = form_request(self.server, "GET", f"/admin/inbox/web/{self.web_a}", cookie=self.nomod)
        self.assertEqual(status, 403)
        status, _, _ = form_request(self.server, "POST", f"/admin/inbox/web/{self.web_a}/reply", {"body": "x"}, self.nomod)
        self.assertEqual(status, 403)
        status, _, _ = form_request(self.server, "POST", f"/admin/inbox/web/{self.web_a}/ai-toggle", {}, self.nomod)
        self.assertEqual(status, 403)
        # 跨租户 / 串模块：404
        other_conv = self.db.create_conversation("other-tenant", "website_api", "en")
        status, _, _ = form_request(self.server, "GET", f"/admin/inbox/web/{other_conv}", cookie=self.inbox_emp)
        self.assertEqual(status, 404)
        status, _, _ = form_request(self.server, "GET", f"/admin/inbox/web/{self.langbot_conv}", cookie=self.inbox_emp)
        self.assertEqual(status, 404)
        status, _, _ = form_request(self.server, "GET", f"/admin/inbox/telegram/{self.web_a}", cookie=self.inbox_emp)
        self.assertEqual(status, 404)  # 平台串模块：网站会话不属于 Telegram 模块
        status, _, _ = form_request(self.server, "GET", f"/admin/inbox/telegram/{self.other_conv}", cookie=self.inbox_emp)
        self.assertEqual(status, 404)  # 其他来源会话不属于 Telegram 模块

    def test_inbox_nav_platform_modules(self):
        """侧栏收件箱子模块=来源平台：网站聊天 + Telegram（有数据）+ 其他来源 + WhatsApp（原生常显）。"""
        status, _, page = form_request(self.server, "GET", "/admin", cookie=self.inbox_emp)
        self.assertEqual(status, 200)
        for marker in ("/admin/inbox/web", "/admin/inbox/telegram", "/admin/inbox/whatsapp", "/admin/inbox/other"):
            self.assertIn(marker, page)
        self.assertIn("i-plat-telegram", page)
        self.assertIn("i-plat-whatsapp", page)
        self.assertNotIn("/admin/inbox/langbot", page)

    def test_inbox_reply_channel_delivery_gated(self):
        # 渠道回复走真实发送：未启用渠道时消息仍落库并标记「发送失败」（不再直接拒绝）。
        before = self.db.conn.execute(
            "SELECT COUNT(*) FROM messages WHERE tenant_id='demo-tenant' AND conversation_id=?", (self.langbot_conv,)
        ).fetchone()[0]
        status, headers, _ = form_request(self.server, "POST", f"/admin/inbox/telegram/{self.langbot_conv}/reply", {"body": "hello"}, self.inbox_emp)
        self.assertEqual(status, 303)
        self.assertIn("notice=reply_failed", headers.get("Location", ""))
        after = self.db.conn.execute(
            "SELECT COUNT(*) FROM messages WHERE tenant_id='demo-tenant' AND conversation_id=?", (self.langbot_conv,)
        ).fetchone()[0]
        self.assertEqual(before + 1, after)
        row = self.db.conn.execute(
            "SELECT * FROM messages WHERE tenant_id='demo-tenant' AND conversation_id=? ORDER BY rowid DESC LIMIT 1", (self.langbot_conv,)
        ).fetchone()
        self.assertEqual(row["delivery_status"], "failed")
        self.assertIn("渠道未启用", row["delivery_error"])
        status, _, page = form_request(self.server, "GET", f"/admin/inbox/telegram/{self.langbot_conv}?notice=reply_failed", cookie=self.inbox_emp)
        self.assertIn("发送失败", page)
        self.assertIn("回复发送失败", page)
        # WhatsApp：启用渠道但发送通道未开通 → 同样落库并标记失败（UI 目前不提供入口，直连 POST 验证守卫）
        self.db.save_channel_account("demo-tenant", "whatsapp", {}, "demo-admin@example.invalid")
        self.db.set_channel_enabled("demo-tenant", "whatsapp", True, "demo-admin@example.invalid")
        wa_conv = self.db.create_conversation("demo-tenant", "whatsapp", "en")
        self.db.bind_channel_identity("demo-tenant", "whatsapp", "wa-1", "Wa Buyer", wa_conv)
        self.db.save_message("demo-tenant", wa_conv, "buyer", "hi", "whatsapp", channel="whatsapp", external_message_id="wa:1")
        status, headers, _ = form_request(self.server, "POST", f"/admin/inbox/whatsapp/{wa_conv}/reply", {"body": "hi"}, self.inbox_emp)
        self.assertEqual(status, 303)
        self.assertIn("notice=reply_failed", headers.get("Location", ""))
        row = self.db.conn.execute(
            "SELECT * FROM messages WHERE tenant_id='demo-tenant' AND conversation_id=? ORDER BY rowid DESC LIMIT 1", (wa_conv,)
        ).fetchone()
        self.assertIn("发送通道尚未开通", row["delivery_error"])

    def test_inbox_reply_web_and_buyer_visibility(self):
        status, headers, _ = form_request(
            self.server, "POST", f"/admin/inbox/web/{self.web_a}/reply",
            {"body": "我们已经安排工程师复查，明天答复您。"}, self.inbox_emp,
        )
        self.assertEqual(status, 303)
        self.assertIn("notice=sent", headers.get("Location", ""))
        # 落库：卖家回复消息（含回复人、前缀、来源）与审计
        row = self.db.conn.execute(
            "SELECT * FROM messages WHERE tenant_id='demo-tenant' AND conversation_id=? ORDER BY rowid DESC LIMIT 1",
            (self.web_a,),
        ).fetchone()
        self.assertEqual(row["role"], "staff")
        self.assertEqual(row["source"], "staff-reply")
        self.assertEqual(row["actor_email"], "v104b-inbox@example.invalid")
        self.assertEqual(row["delivery_status"], "sent")
        self.assertTrue(row["content"].startswith("卖家回复："))
        actions = [
            item["action"]
            for item in self.db.conn.execute(
                "SELECT action FROM audit_log WHERE tenant_id='demo-tenant' AND entity_id=? ORDER BY id", (self.web_a,)
            )
        ]
        self.assertIn("staff_replied", actions)
        # 买家侧可见（网站聊天轮询读取同一会话）：卖家回复出现在消息流里
        token = self._buyer_token(self.web_a)
        status, body = json_http(
            self.server, "GET", f"/api/v1/buyer/conversations/{self.web_a}",
            headers={"X-Site-Key": "site-a", "Authorization": f"Bearer {token}"},
        )
        self.assertEqual(status, 200)
        self.assertTrue(
            any(message.get("source") == "staff-reply" and str(message.get("content", "")).startswith("卖家回复：") for message in body["messages"])
        )
        # 详情页呈现：人工 · 卖家回复（回复人）+ 前缀剥离
        status, _, page = form_request(self.server, "GET", f"/admin/inbox/web/{self.web_a}?notice=sent", cookie=self.inbox_emp)
        self.assertIn("人工 · 卖家回复（v104b-inbox@example.invalid）", page)
        self.assertIn("我们已经安排工程师复查，明天答复您。", page)
        self.assertIn("回复已发送", page)
        self.assertIn("发送卖家回复", AUDIT_ACTION_LABELS.get("staff_replied", ""))  # 标签中文
        self.assertEqual(AUDIT_ACTION_LABELS["staff_replied"], "发送卖家回复")


class ChannelPipelineTests(unittest.TestCase):
    """业务全链路（LangBot 传输轨道）：需求确认与匹配、RFQ 链接往返、表单字段三态、坐席回复直达与失败可见。"""

    maxDiff = None

    @classmethod
    def setUpClass(cls):
        cls.temp_dir = tempfile.TemporaryDirectory()
        cls.db_path = Path(cls.temp_dir.name) / "app.db"
        cls.mock = _LangBotMock()
        cls.env_backup = {name: os.environ.get(name) for name in ("LANGBOT_API_KEY", "LANGBOT_API_BASE")}
        os.environ["LANGBOT_API_KEY"] = "lbk_test_key"
        os.environ["LANGBOT_API_BASE"] = cls.mock.url
        cls.server = make_server(cls.db_path, port=0)
        cls.db = cls.server.RequestHandlerClass.db
        cls.db.seed_demo_users()
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.admin = login(cls.server, "demo-admin@example.invalid", "demo-admin")
        cls.token = "tok_pipeline_test_0001_abcdef"
        cls.db.save_channel_account("demo-tenant", "langbot", {"bot_uuid": "bot-pipe-1", "webhook_token": cls.token}, "demo-admin@example.invalid")
        cls.db.set_channel_enabled("demo-tenant", "langbot", True, "demo-admin@example.invalid")
        # 已发布产品（有据问答 / 产品匹配用）
        cls.product_id = cls.db.create_product("demo-tenant", "TG-100", "渠道测试泵", "清水")
        source_id = cls.db.add_source("demo-tenant", cls.product_id, "manual.pdf", "PDF", "第 2 页")
        cls.db.save_field("demo-tenant", cls.product_id, "flow_range", "10-30", "m3/h", source_id, "第 2 页")
        cls.db.save_field("demo-tenant", cls.product_id, "head_range", "20-60", "m", source_id, "第 3 页")
        cls.db.save_field("demo-tenant", cls.product_id, "media", "清水", "", source_id, "第 4 页")
        for field_key in ("flow_range", "head_range", "media"):
            cls.db.approve_field("demo-tenant", cls.product_id, field_key)
        cls.db.finalize_product_review("demo-tenant", cls.product_id)
        cls.db.publish_product("demo-tenant", cls.product_id)

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.mock.stop()
        cls.temp_dir.cleanup()
        for name, value in cls.env_backup.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value

    # ── 辅助 ───────────────────────────────────────────────────────────────
    def post_event(self, sender_id, text, uuid):
        return json_http(
            self.server, "POST", f"/api/v1/channels/langbot/webhook/{self.token}",
            {
                "uuid": uuid,
                "event_type": "bot.person_message",
                "data": {
                    "bot_uuid": "bot-pipe-1",
                    "adapter_name": "TelegramAdapter",
                    "sender": {"id": sender_id, "name": "Maya"},
                    "message": [{"type": "Plain", "text": text}],
                    "timestamp": 1759500000,
                },
            },
        )

    def conversation_for(self, sender_id):
        row = self.db.conn.execute(
            "SELECT conversation_id FROM channel_identities WHERE tenant_id='demo-tenant' AND channel='langbot' AND external_user_id=?",
            (str(sender_id),),
        ).fetchone()
        return row["conversation_id"] if row else None

    def sent_texts(self, sender_id):
        return [item["text"] for item in self.mock.sent_messages() if item["target_id"] == str(sender_id)]

    def last_message(self, conversation_id, source=None):
        if source:
            return self.db.conn.execute(
                "SELECT * FROM messages WHERE tenant_id='demo-tenant' AND conversation_id=? AND source=? ORDER BY rowid DESC LIMIT 1",
                (conversation_id, source),
            ).fetchone()
        return self.db.conn.execute(
            "SELECT * FROM messages WHERE tenant_id='demo-tenant' AND conversation_id=? ORDER BY rowid DESC LIMIT 1",
            (conversation_id,),
        ).fetchone()

    # ── 用例 ───────────────────────────────────────────────────────────────
    def test_requirement_confirm_and_match(self):
        status, _body = self.post_event(4404, "请报价：flow 15 m3/h, head 30 m, medium clean water，用于冷却水系统", "lb-ev-0404")
        self.assertEqual(status, 200)
        conversation_id = self.conversation_for(4404)
        self.assertTrue(any("已记录以下工况" in text for text in self.sent_texts(4404)))
        self.assertTrue(any("「确认」" in text for text in self.sent_texts(4404)))
        # 「确认」→ 确认工况并按同口径匹配（TG-100 覆盖 15 m³/h、30 m、清水）
        status, _body = self.post_event(4404, "确认", "lb-ev-0405")
        self.assertEqual(status, 200)
        latest_revision = self.db.conn.execute(
            "SELECT * FROM requirement_revisions WHERE tenant_id='demo-tenant' AND conversation_id=? ORDER BY revision DESC LIMIT 1",
            (conversation_id,),
        ).fetchone()
        self.assertEqual(latest_revision["status"], "confirmed")
        recommendation = self.db.conn.execute(
            "SELECT * FROM recommendations WHERE tenant_id='demo-tenant' AND conversation_id=? AND status<>'invalidated'", (conversation_id,)
        ).fetchone()
        self.assertTrue(recommendation)
        self.assertTrue(any("TG-100" in text for text in self.sent_texts(4404)))
        self.assertTrue(any("根据已确认工况" in text for text in self.sent_texts(4404)))

    def test_rfq_link_and_form_roundtrip(self):
        status, _body = self.post_event(4405, "hello", "lb-ev-0406")
        self.assertEqual(status, 200)
        conversation_id = self.conversation_for(4405)
        self.assertTrue(conversation_id)
        # 后台坐席发送询价链接（消息直达渠道）
        status, headers, _ = form_request(self.server, "POST", f"/admin/inbox/telegram/{conversation_id}/rfq-link", {}, self.admin)
        self.assertEqual(status, 303)
        self.assertIn("notice=link_sent", headers.get("Location", ""))
        link_row = self.last_message(conversation_id, "rfq-link")
        self.assertEqual(link_row["delivery_status"], "sent")
        link_texts = [text for text in self.sent_texts(4405) if "/rfq/" in text]
        self.assertTrue(link_texts)
        token = re.search(r"/rfq/([A-Za-z0-9_-]+)", link_texts[-1]).group(1)
        # 公开填写页（无登录即可打开）
        status, _, page = form_request(self.server, "GET", f"/rfq/{token}")
        self.assertEqual(status, 200)
        self.assertIn("填写询价信息", page)
        self.assertIn("contact_confirmed", page)
        self.assertIn("邮箱 / Email", page)
        # 未勾选「联系确认」→ 拒绝且保留填写内容
        status, _, page = form_request(self.server, "POST", f"/rfq/{token}", {"contact_email": "tg@example.invalid", "contact_phone": "+49", "quantity": "2", "destination": "Germany", "notes": ""})
        self.assertEqual(status, 400)
        self.assertIn("请先勾选确认", page)
        # 提交成功 → 建单（入口=langbot_link、会话绑定、联系同意来源）+ 渠道回执 + 会话留痕
        status, _, page = form_request(self.server, "POST", f"/rfq/{token}", {"contact_email": "tg@example.invalid", "contact_phone": "+49", "quantity": "2", "destination": "Germany", "notes": "需要 CE", "contact_confirmed": "1"})
        self.assertEqual(status, 200)
        self.assertIn("已提交", page)
        rfq = self.db.conn.execute(
            "SELECT * FROM rfqs WHERE tenant_id='demo-tenant' AND conversation_id=? AND entry_surface='langbot_link'", (conversation_id,)
        ).fetchone()
        self.assertTrue(rfq)
        self.assertEqual(rfq["quantity"], "2")
        self.assertEqual(rfq["contact_consent_source"], "langbot_link")
        self.assertEqual(self.db.get_rfq("demo-tenant", rfq["id"])["current_revision"], 1)
        self.assertTrue(any("您的询价信息已收到" in text for text in self.sent_texts(4405)))
        self.assertTrue(self.last_message(conversation_id, "rfq-form-submitted"))
        # 重新打开：回显既有内容 + 提示更新同一张单；再次提交 → 版本 +1
        status, _, page = form_request(self.server, "GET", f"/rfq/{token}")
        self.assertIn("您已提交过询价", page)
        self.assertIn("value='2'", page)
        status, _, page = form_request(self.server, "POST", f"/rfq/{token}", {"contact_email": "tg@example.invalid", "contact_phone": "+49", "quantity": "5", "destination": "Germany", "notes": "数量更新", "contact_confirmed": "1"})
        self.assertEqual(status, 200)
        refreshed = self.db.get_rfq("demo-tenant", rfq["id"])
        self.assertEqual(refreshed["current_revision"], 2)
        self.assertEqual(refreshed["quantity"], "5")

    def test_rfq_form_field_states_follow_settings(self):
        self.db.set_tenant_setting("demo-tenant", "rfq_fields", {"email": "required", "phone": "hidden", "quantity": "required", "destination": "optional", "notes": "optional"}, "demo-admin@example.invalid")
        try:
            status, _body = self.post_event(4406, "hello", "lb-ev-0407")
            self.assertEqual(status, 200)
            conversation_id = self.conversation_for(4406)
            token = self.db.create_rfq_form_token("demo-tenant", conversation_id, "demo-admin@example.invalid")
            status, _, page = form_request(self.server, "GET", f"/rfq/{token}")
            self.assertEqual(status, 200)
            self.assertIn("邮箱 / Email", page)
            self.assertNotIn("电话 / Phone", page)  # 隐藏字段不渲染
            # 缺必填邮箱 → 400 且列出缺项
            status, _, page = form_request(self.server, "POST", f"/rfq/{token}", {"quantity": "1", "contact_confirmed": "1"})
            self.assertEqual(status, 400)
            self.assertIn("请填写：邮箱", page)
            # 补齐后成功；隐藏电话即使被提交也不落库
            status, _, page = form_request(self.server, "POST", f"/rfq/{token}", {"contact_email": "states@example.invalid", "contact_phone": "should-not-store", "quantity": "1", "contact_confirmed": "1"})
            self.assertEqual(status, 200)
            rfq = self.db.conn.execute(
                "SELECT * FROM rfqs WHERE tenant_id='demo-tenant' AND conversation_id=? AND entry_surface='langbot_link'", (conversation_id,)
            ).fetchone()
            self.assertEqual(rfq["contact_phone"], "")
        finally:
            self.db.set_tenant_setting("demo-tenant", "rfq_fields", dict(DEFAULT_RFQ_FIELDS), "demo-admin@example.invalid")

    def test_staff_reply_from_inbox_reaches_channel(self):
        status, _body = self.post_event(4407, "hello", "lb-ev-0408")
        self.assertEqual(status, 200)
        conversation_id = self.conversation_for(4407)
        status, headers, _ = form_request(self.server, "POST", f"/admin/inbox/telegram/{conversation_id}/reply", {"body": "您好，已收到您的消息。"}, self.admin)
        self.assertEqual(status, 303)
        self.assertIn("notice=sent", headers.get("Location", ""))
        self.assertIn("您好，已收到您的消息。", self.sent_texts(4407))
        row = self.last_message(conversation_id, "staff-reply")
        self.assertEqual(row["delivery_status"], "sent")
        self.assertEqual(row["actor_email"], "demo-admin@example.invalid")
        status, _, page = form_request(self.server, "GET", f"/admin/inbox/telegram/{conversation_id}?notice=sent", cookie=self.admin)
        self.assertIn("已发送", page)
        # 失败可见：临时把模拟地址指向不可达端口
        os.environ["LANGBOT_API_BASE"] = "http://127.0.0.1:9"
        try:
            status, headers, _ = form_request(self.server, "POST", f"/admin/inbox/telegram/{conversation_id}/reply", {"body": "这条会失败。"}, self.admin)
            self.assertEqual(status, 303)
            self.assertIn("notice=reply_failed", headers.get("Location", ""))
            row = self.last_message(conversation_id, "staff-reply")
            self.assertEqual(row["delivery_status"], "failed")
            self.assertIn("网络请求失败", row["delivery_error"])
            status, _, page = form_request(self.server, "GET", f"/admin/inbox/telegram/{conversation_id}?notice=reply_failed", cookie=self.admin)
            self.assertIn("发送失败：网络请求失败", page)
        finally:
            os.environ["LANGBOT_API_BASE"] = self.mock.url


class OutboundBusTests(unittest.TestCase):
    """出站总线与 F-22 状态机——重试幂等、回执、销售侧真发、补投工人、主动门控（LangBot 轨道）。"""

    maxDiff = None

    @classmethod
    def setUpClass(cls):
        cls.temp_dir = tempfile.TemporaryDirectory()
        cls.db_path = Path(cls.temp_dir.name) / "app.db"
        cls.mock = _LangBotMock()
        cls.env_backup = {name: os.environ.get(name) for name in ("LANGBOT_API_KEY", "LANGBOT_API_BASE")}
        os.environ["LANGBOT_API_KEY"] = "lbk_test_key"
        os.environ["LANGBOT_API_BASE"] = cls.mock.url
        cls.server = make_server(cls.db_path, port=0)
        cls.db = cls.server.RequestHandlerClass.db
        cls.db.seed_demo_users()
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.admin = login(cls.server, "demo-admin@example.invalid", "demo-admin")
        cls.db.save_channel_account("demo-tenant", "langbot", {"bot_uuid": "bot-bus-1", "webhook_token": "bus-token-1234567890"}, "demo-admin@example.invalid")
        cls.db.set_channel_enabled("demo-tenant", "langbot", True, "demo-admin@example.invalid")
        # 会话 A：总线重试/回执/收件箱重试用
        cls.conv_a = cls.db.create_conversation("demo-tenant", "langbot", "en")
        cls.db.bind_channel_identity("demo-tenant", "langbot", "71001", "Bus Buyer", cls.conv_a)
        # 会话 B：销售侧真发（RFQ→线索自动生成）
        cls.conv_b = cls.db.create_conversation("demo-tenant", "langbot", "en")
        cls.db.bind_channel_identity("demo-tenant", "langbot", "71002", "Sales Buyer", cls.conv_b)
        cls.rfq_id, _created = cls.db.create_rfq(
            "demo-tenant", cls.conv_b, "sub-bus-1",
            {"contact_email": "bus@example.invalid", "contact_phone": "+49", "quantity": "1", "region": "Germany"},
            "总线测试询价",
        )
        # 会话 C：主动门控（独立，避免串扰）
        cls.conv_c = cls.db.create_conversation("demo-tenant", "langbot", "en")
        cls.db.bind_channel_identity("demo-tenant", "langbot", "71003", "Gate Buyer", cls.conv_c)
        for ext in ("71001", "71002", "71003"):
            cls.db.update_channel_identity_meta("demo-tenant", "langbot", ext, {"adapter": "Telegram"})
        # WhatsApp 回执基座：Twilio 状态回调（sent → delivered）为 WhatsApp 落地预留，用合成行验证
        cls.wa_conv = cls.db.create_conversation("demo-tenant", "whatsapp", "en")
        cls.db.bind_channel_identity("demo-tenant", "whatsapp", "wa-bus", "Wa Bus", cls.wa_conv)

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.mock.stop()
        cls.temp_dir.cleanup()
        for name, value in cls.env_backup.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value

    def message_row(self, message_id):
        return self.db.conn.execute("SELECT * FROM messages WHERE id=?", (message_id,)).fetchone()

    def sent_texts(self, sender_id):
        return [item["text"] for item in self.mock.sent_messages() if item["target_id"] == str(sender_id)]

    def test_dispatch_retry_idempotent_and_delivered_receipt(self):
        conversation = self.db.get_conversation("demo-tenant", self.conv_a)
        os.environ["LANGBOT_API_BASE"] = "http://127.0.0.1:9"  # 先制造一次失败
        try:
            message_id, sent, error = enqueue_outbound(self.db, "demo-tenant", conversation, "assistant", "总线测试消息一", "channel-guidance")
        finally:
            os.environ["LANGBOT_API_BASE"] = self.mock.url
        self.assertFalse(sent)
        self.assertIn("网络请求失败", error)
        row = self.message_row(message_id)
        self.assertEqual(row["delivery_status"], "failed")
        self.assertEqual(row["delivery_attempts"], 1)
        # 修复后重试 → sent；尝试数 +1；审计留痕
        sent, error = retry_outbound_message(self.db, "demo-tenant", message_id, "demo-admin@example.invalid")
        self.assertTrue(sent, error)
        row = self.message_row(message_id)
        self.assertEqual(row["delivery_status"], "sent")
        self.assertEqual(row["delivery_attempts"], 2)
        self.assertIn("总线测试消息一", self.sent_texts(71001))
        audits = [item["action"] for item in self.db.conn.execute(
            "SELECT action FROM audit_log WHERE tenant_id='demo-tenant' AND entity_id=? ORDER BY id", (message_id,)
        )]
        self.assertIn("outbound_retried", audits)
        # 幂等：已发送再重试 → 不重发
        before = len(self.mock.sent_messages())
        sent, _ = retry_outbound_message(self.db, "demo-tenant", message_id, "demo-admin@example.invalid")
        self.assertTrue(sent)
        self.assertEqual(before, len(self.mock.sent_messages()))
        # 回执推进：sent → delivered（幂等；Twilio 状态回调的落点，用合成行验证）
        receipt_id = self.db.save_message(
            "demo-tenant", self.wa_conv, "assistant", "回执基座消息", "channel-guidance",
            channel="whatsapp", external_message_id="SM-synth-1", delivery_status="sent",
        )
        self.assertTrue(mark_outbound_delivered(self.db, "demo-tenant", "whatsapp", "SM-synth-1"))
        self.assertEqual(self.message_row(receipt_id)["delivery_status"], "delivered")
        self.assertFalse(mark_outbound_delivered(self.db, "demo-tenant", "whatsapp", "SM-synth-1"))
        # 详情页显示「已送达」，且不再提供重试
        status, _, page = form_request(self.server, "GET", f"/admin/inbox/whatsapp/{self.wa_conv}", cookie=self.admin)
        self.assertEqual(status, 200)
        self.assertIn("已送达", page)

    def test_inbox_retry_button_flow_and_list_badge(self):
        conversation = self.db.get_conversation("demo-tenant", self.conv_a)
        os.environ["LANGBOT_API_BASE"] = "http://127.0.0.1:9"
        try:
            message_id, sent, _error = enqueue_outbound(self.db, "demo-tenant", conversation, "assistant", "总线测试消息二", "channel-guidance")
        finally:
            os.environ["LANGBOT_API_BASE"] = self.mock.url
        self.assertFalse(sent)
        # 列表：显示「N 条发送失败」徽标
        status, _, page = form_request(self.server, "GET", "/admin/inbox/telegram", cookie=self.admin)
        self.assertIn("1 条发送失败", page)
        # 详情：失败原因 + 「重试发送」按钮
        status, _, page = form_request(self.server, "GET", f"/admin/inbox/telegram/{self.conv_a}", cookie=self.admin)
        self.assertIn("发送失败：网络请求失败", page)
        self.assertIn("重试发送", page)
        self.assertIn(f"/admin/inbox/telegram/{self.conv_a}/messages/{message_id}/retry", page)
        # 点重试（真实 UI 表单 POST）→ 成功公告 + 消息可送达
        status, headers, _ = form_request(self.server, "POST", f"/admin/inbox/telegram/{self.conv_a}/messages/{message_id}/retry", {}, self.admin)
        self.assertEqual(status, 303)
        self.assertIn("notice=retried_ok", headers.get("Location", ""))
        self.assertEqual(self.message_row(message_id)["delivery_status"], "sent")
        self.assertIn("总线测试消息二", self.sent_texts(71001))
        status, _, page = form_request(self.server, "GET", f"/admin/inbox/telegram/{self.conv_a}?notice=retried_ok", cookie=self.admin)
        self.assertIn("重试成功", page)
        # 已成功消息再点重试（直接 POST）→ 幂等提示，不重发
        before = len(self.mock.sent_messages())
        status, headers, _ = form_request(self.server, "POST", f"/admin/inbox/telegram/{self.conv_a}/messages/{message_id}/retry", {}, self.admin)
        self.assertIn("notice=retry_already", headers.get("Location", ""))
        self.assertEqual(before, len(self.mock.sent_messages()))

    def test_outbound_worker_flush_stuck_queue(self):
        # 模拟「进程中断」：消息已落队但从未投递
        stuck_id = self.db.save_message(
            "demo-tenant", self.conv_a, "assistant", "卡在队列里的消息", "channel-guidance",
            channel="langbot", delivery_status="queued",
        )
        env = os.environ.copy()
        result = subprocess.run(
            [sys.executable, "tools/channels/outbound_worker.py", "--db", str(self.db_path), "--once", "--min-age", "0"],
            cwd=str(ROOT), env=env, capture_output=True, text=True, timeout=60,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("已补投", result.stdout)
        self.assertEqual(self.message_row(stuck_id)["delivery_status"], "sent")
        self.assertIn("卡在队列里的消息", self.sent_texts(71001))

    def test_sales_reply_via_channel_success_failure_retry_and_gates(self):
        # ① 成功：草稿经渠道发送
        draft = self.db.create_reply_draft("demo-tenant", self.rfq_id, "经渠道发送的正式回复", "demo-admin@example.invalid")
        status, headers, _ = form_request(self.server, "POST", f"/admin/sales/{self.rfq_id}/replies/{draft}", {"action": "channel"}, self.admin)
        self.assertEqual(status, 303)
        self.assertIn("notice=reply_channel_sent", headers.get("Location", ""))
        reply = self.db.conn.execute("SELECT * FROM lead_replies WHERE id=?", (draft,)).fetchone()
        self.assertEqual(reply["status"], "sent")
        self.assertTrue(reply["message_id"])
        self.assertEqual(reply["channel"], "langbot")
        self.assertIn("经渠道发送的正式回复", self.sent_texts(71002))
        # ② 失败 + 重试：复用同一条消息（不重复建消息）
        os.environ["LANGBOT_API_BASE"] = "http://127.0.0.1:9"
        try:
            draft2 = self.db.create_reply_draft("demo-tenant", self.rfq_id, "先失败后重试的回复", "demo-admin@example.invalid")
            status, headers, _ = form_request(self.server, "POST", f"/admin/sales/{self.rfq_id}/replies/{draft2}", {"action": "channel"}, self.admin)
        finally:
            os.environ["LANGBOT_API_BASE"] = self.mock.url
        self.assertIn("notice=reply_channel_failed", headers.get("Location", ""))
        reply2 = self.db.conn.execute("SELECT * FROM lead_replies WHERE id=?", (draft2,)).fetchone()
        self.assertEqual(reply2["status"], "failed")
        first_message_id = reply2["message_id"]
        status, headers, _ = form_request(self.server, "POST", f"/admin/sales/{self.rfq_id}/replies/{draft2}", {"action": "channel"}, self.admin)
        self.assertIn("notice=reply_channel_sent", headers.get("Location", ""))
        reply2 = self.db.conn.execute("SELECT * FROM lead_replies WHERE id=?", (draft2,)).fetchone()
        self.assertEqual(reply2["status"], "sent")
        self.assertEqual(reply2["message_id"], first_message_id)  # 重试不换消息
        self.assertIn("先失败后重试的回复", self.sent_texts(71002))
        # ③ 线下记录仍可用（同一渠道会话也不强制走真发）
        draft3 = self.db.create_reply_draft("demo-tenant", self.rfq_id, "线下邮件记录的回复", "demo-admin@example.invalid")
        status, headers, _ = form_request(self.server, "POST", f"/admin/sales/{self.rfq_id}/replies/{draft3}", {"action": "sent", "channel": "email"}, self.admin)
        self.assertIn("notice=reply_sent", headers.get("Location", ""))
        self.assertEqual(self.db.conn.execute("SELECT * FROM lead_replies WHERE id=?", (draft3,)).fetchone()["message_id"], "")
        # ④ 停止主动联系门控：渠道真发被阻止（不建消息、草稿保持）
        self.db.stop_proactive_contact("demo-tenant", self.conv_b, "请不要再主动联系", "buyer_chat", "buyer")
        draft4 = self.db.create_reply_draft("demo-tenant", self.rfq_id, "被门控阻止的回复", "demo-admin@example.invalid")
        status, headers, _ = form_request(self.server, "POST", f"/admin/sales/{self.rfq_id}/replies/{draft4}", {"action": "channel"}, self.admin)
        self.assertEqual(status, 303)
        self.assertIn("notice=reply_stop_blocked", headers.get("Location", ""))
        reply4 = self.db.conn.execute("SELECT * FROM lead_replies WHERE id=?", (draft4,)).fetchone()
        self.assertEqual(reply4["status"], "draft")
        self.assertEqual(reply4["message_id"], "")

    def test_proactive_gate_in_bus_entry(self):
        # 停止主动联系后：主动消息被总线拒绝且不留下任何消息；回应式消息不受影响
        self.db.stop_proactive_contact("demo-tenant", self.conv_c, "不再接收推销", "buyer_chat", "buyer")
        conversation = self.db.get_conversation("demo-tenant", self.conv_c)
        before = self.db.conn.execute("SELECT COUNT(*) AS n FROM messages WHERE conversation_id=?", (self.conv_c,)).fetchone()["n"]
        with self.assertRaises(ValueError):
            enqueue_outbound(self.db, "demo-tenant", conversation, "assistant", "主动推销消息", "rfq-link", proactive=True)
        after = self.db.conn.execute("SELECT COUNT(*) AS n FROM messages WHERE conversation_id=?", (self.conv_c,)).fetchone()["n"]
        self.assertEqual(before, after)
        # 回应式不受门控（对买方消息的回复）
        _message_id, sent, _error = enqueue_outbound(self.db, "demo-tenant", conversation, "staff", "卖家回复：回应内容", "staff-reply", proactive=False)
        self.assertTrue(sent)
        self.assertIn("回应内容", self.sent_texts(71003))

    def test_dispatch_idempotent_for_sent_messages(self):
        # 直接对已发送消息调用投递 → 幂等返回且不重发（并发/手动重复调用防护）
        conversation = self.db.get_conversation("demo-tenant", self.conv_a)
        message_id, sent, _error = enqueue_outbound(self.db, "demo-tenant", conversation, "assistant", "幂等测试消息", "channel-guidance")
        self.assertTrue(sent)
        before = len(self.mock.sent_messages())
        sent, _error = dispatch_outbound(self.db, "demo-tenant", message_id)
        self.assertTrue(sent)
        self.assertEqual(before, len(self.mock.sent_messages()))


if __name__ == "__main__":
    unittest.main()
