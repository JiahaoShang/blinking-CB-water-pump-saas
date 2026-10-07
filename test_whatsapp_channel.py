"""WhatsApp 原生通道（切片①：出站实装 + 状态回调）测试。

契约：
- 发送：经 Twilio 形状 Messages API（表单 + Basic 认证）；成功落 external_message_id（「外部用户ID:SID」）、delivery_status=sent；
- 凭证缺失 fail-closed（明确缺哪个变量）；渠道未启用 → 「渠道未启用」；网络失败 → 可重试（attempts+1）；
- 状态回调 /api/v1/channels/whatsapp/status：验签 fail-closed；delivered → 推进；undelivered/failed → 标记失败（含原因）；
  幂等、终态不降级、未知 SID 无害；未配置 TWILIO_AUTH_TOKEN → 403（无法验签即拒绝）。
- UI 语义不变：收件箱回复/重试走同一出站总线。
"""
import base64 as b64
import http.client
import json
import os
import sys
import tempfile
import threading
import unittest
import urllib.parse
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools" / "channels"))

from app import (  # noqa: E402
    enqueue_outbound,
    make_server,
    retry_outbound_message,
)
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


def raw_post(server, path, fields, signature=None):
    """原始表单 POST（可带自定义签名头），返回 (状态码, 响应体)。"""
    conn = http.client.HTTPConnection("127.0.0.1", server.server_port)
    body = urllib.parse.urlencode(fields)
    headers = {"Content-Type": "application/x-www-form-urlencoded"}
    if signature is not None:
        headers["X-Twilio-Signature"] = signature
    conn.request("POST", path, body=body, headers=headers)
    response = conn.getresponse()
    raw = response.read().decode("utf-8", errors="replace")
    status = response.status
    conn.close()
    return status, raw


class WhatsAppOutboundTests(unittest.TestCase):
    """切片①：出站实装（Twilio 形状）+ 状态回调。"""

    maxDiff = None

    @classmethod
    def setUpClass(cls):
        cls.temp_dir = tempfile.TemporaryDirectory()
        cls.db_path = Path(cls.temp_dir.name) / "app.db"
        cls.mock = mock_whatsapp.MockTwilioServer(auth_token="twilio-test-token")
        cls.env_names = ("TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_WHATSAPP_FROM", "TWILIO_API_BASE", "TWILIO_STATUS_CALLBACK", "TWILIO_PUBLIC_BASE")
        cls.env_backup = {name: os.environ.get(name) for name in cls.env_names}
        for name in cls.env_names:
            os.environ.pop(name, None)
        cls.server = make_server(cls.db_path, port=0)
        cls.db = cls.server.RequestHandlerClass.db
        cls.db.seed_demo_users()
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.admin = login(cls.server, "demo-admin@example.invalid", "demo-admin")
        cls.db.save_channel_account("demo-tenant", "whatsapp", {"display_name": "测试"}, "demo-admin@example.invalid")
        cls.db.set_channel_enabled("demo-tenant", "whatsapp", True, "demo-admin@example.invalid")
        cls.conv = cls.db.create_conversation("demo-tenant", "whatsapp", "en")
        cls.db.bind_channel_identity("demo-tenant", "whatsapp", "whatsapp:+491234", "Wa Buyer", cls.conv)
        cls.db.save_message("demo-tenant", cls.conv, "buyer", "hi there", "whatsapp", channel="whatsapp", external_message_id="wa:seed-1")

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

    # ── 辅助 ──
    def _set_env(self):
        os.environ["TWILIO_ACCOUNT_SID"] = "ACtest00000000000000000000000000"
        os.environ["TWILIO_AUTH_TOKEN"] = "twilio-test-token"
        os.environ["TWILIO_WHATSAPP_FROM"] = "whatsapp:+14155238886"
        os.environ["TWILIO_API_BASE"] = self.mock.base_url()
        os.environ["TWILIO_STATUS_CALLBACK"] = f"http://127.0.0.1:{self.server.server_port}/api/v1/channels/whatsapp/status"

    def _clear_env(self):
        for name in ("TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_WHATSAPP_FROM", "TWILIO_API_BASE", "TWILIO_STATUS_CALLBACK"):
            os.environ.pop(name, None)

    def _conversation(self):
        return self.db.get_conversation("demo-tenant", self.conv)

    def _last_message(self):
        return self.db.conn.execute(
            "SELECT * FROM messages WHERE tenant_id='demo-tenant' AND conversation_id=? ORDER BY rowid DESC LIMIT 1", (self.conv,)
        ).fetchone()

    def _send(self, text):
        return enqueue_outbound(self.db, "demo-tenant", self._conversation(), "assistant", text, "staff-reply")

    def _status_url(self):
        return f"http://127.0.0.1:{self.server.server_port}/api/v1/channels/whatsapp/status"

    # ── 用例 ──
    def test_inbox_reply_ui_path_sends_via_bus(self):
        self._set_env()
        status, headers, _ = form_request(self.server, "POST", f"/admin/inbox/whatsapp/{self.conv}/reply", {"body": "您好，这是坐席回复。"}, self.admin)
        self.assertEqual(status, 303)
        self.assertIn("notice=sent", headers.get("Location", ""))
        row = self._last_message()
        self.assertEqual(row["delivery_status"], "sent")
        sid = self.mock.sent_messages()[-1]["sid"]
        self.assertTrue(self.mock.trigger_status(sid, "delivered"))
        row = self.db.conn.execute("SELECT * FROM messages WHERE id=?", (row["id"],)).fetchone()
        self.assertEqual(row["delivery_status"], "delivered")

    def test_missing_credentials_fail_closed_and_disabled_channel(self):
        self._clear_env()
        self.db.set_channel_enabled("demo-tenant", "whatsapp", False, "demo-admin@example.invalid")
        _mid, sent, error = self._send("未启用测试")
        self.assertFalse(sent)
        self.assertIn("渠道未启用", error)
        self.db.set_channel_enabled("demo-tenant", "whatsapp", True, "demo-admin@example.invalid")
        _mid, sent, error = self._send("缺凭证测试")
        self.assertFalse(sent)
        self.assertIn("缺少环境变量 TWILIO_ACCOUNT_SID", error)
        row = self._last_message()
        self.assertEqual(row["delivery_status"], "failed")
        self.assertIn("缺少环境变量 TWILIO_ACCOUNT_SID", row["delivery_error"])

    def test_network_failure_then_retry(self):
        self._set_env()
        os.environ["TWILIO_API_BASE"] = "http://127.0.0.1:9"
        message_id, sent, error = self._send("先失败后重试")
        self.assertFalse(sent)
        self.assertIn("网络请求失败", error)
        row = self.db.conn.execute("SELECT * FROM messages WHERE id=?", (message_id,)).fetchone()
        self.assertEqual(row["delivery_status"], "failed")
        self.assertEqual(row["delivery_attempts"], 1)
        os.environ["TWILIO_API_BASE"] = self.mock.base_url()
        ok, retry_error = retry_outbound_message(self.db, "demo-tenant", message_id, "demo-admin@example.invalid")
        self.assertTrue(ok, retry_error)
        row = self.db.conn.execute("SELECT * FROM messages WHERE id=?", (message_id,)).fetchone()
        self.assertEqual(row["delivery_status"], "sent")
        self.assertEqual(row["delivery_attempts"], 2)
        self.assertEqual(self.mock.sent_messages()[-1]["body"], "先失败后重试")

    def test_send_success_and_delivered_receipt(self):
        self._set_env()
        message_id, sent, error = self._send("您好，这是平台发出的第一条测试消息。")
        self.assertTrue(sent, error)
        row = self.db.conn.execute("SELECT * FROM messages WHERE id=?", (message_id,)).fetchone()
        self.assertEqual(row["delivery_status"], "sent")
        self.assertTrue(row["external_message_id"].startswith("whatsapp:+491234:"))
        self.assertIn(":SM", row["external_message_id"])
        record = self.mock.sent_messages()[-1]
        sid = record["sid"]
        self.assertEqual(record["to"], "whatsapp:+491234")
        self.assertEqual(record["body"], "您好，这是平台发出的第一条测试消息。")
        expected_auth = "Basic " + b64.b64encode(b"ACtest00000000000000000000000000:twilio-test-token").decode()
        self.assertEqual(record["authorization"], expected_auth)
        self.assertTrue(record["status_callback"].endswith("/api/v1/channels/whatsapp/status"))
        # delivered 推进 + 幂等
        self.assertTrue(self.mock.trigger_status(sid, "delivered"))
        row = self.db.conn.execute("SELECT * FROM messages WHERE id=?", (message_id,)).fetchone()
        self.assertEqual(row["delivery_status"], "delivered")
        self.assertTrue(self.mock.trigger_status(sid, "delivered"))
        row = self.db.conn.execute("SELECT * FROM messages WHERE id=?", (message_id,)).fetchone()
        self.assertEqual(row["delivery_status"], "delivered")

    def test_status_route_requires_token_configured(self):
        self._clear_env()
        status, body = raw_post(self.server, "/api/v1/channels/whatsapp/status", {"MessageSid": "SMxx", "MessageStatus": "delivered"}, signature="x")
        self.assertEqual(status, 403)
        self.assertIn("channel_not_configured", body)

    def test_status_signature_fail_closed_and_failed_status(self):
        self._set_env()
        message_id, sent, _error = self._send("回执与失败状态测试")
        self.assertTrue(sent)
        sid = self.mock.sent_messages()[-1]["sid"]
        path = "/api/v1/channels/whatsapp/status"
        url = self._status_url()
        # 假签名 → 403，状态不动
        bad_status, _ = raw_post(self.server, path, {"MessageSid": sid, "MessageStatus": "delivered"}, signature="ZmFrZQ==")
        self.assertEqual(bad_status, 403)
        row = self.db.conn.execute("SELECT delivery_status FROM messages WHERE id=?", (message_id,)).fetchone()
        self.assertEqual(row["delivery_status"], "sent")
        # 缺签名 → 403
        no_sig_status, _ = raw_post(self.server, path, {"MessageSid": sid, "MessageStatus": "delivered"}, signature=None)
        self.assertEqual(no_sig_status, 403)
        # 真实签名：undelivered（带错误码）→ 标记失败并记录原因
        fail_params = {
            "MessageSid": sid,
            "MessageStatus": "undelivered",
            "AccountSid": "ACtest00000000000000000000000000",
            "ErrorCode": "63015",
            "ErrorMessage": "User has not joined the sandbox",
        }
        good_sig = mock_whatsapp.twilio_signature("twilio-test-token", url, fail_params)
        ok_status, body = raw_post(self.server, path, fail_params, signature=good_sig)
        self.assertEqual(ok_status, 200)
        self.assertTrue(json.loads(body)["changed"])
        row = self.db.conn.execute("SELECT * FROM messages WHERE id=?", (message_id,)).fetchone()
        self.assertEqual(row["delivery_status"], "failed")
        self.assertIn("User has not joined the sandbox", row["delivery_error"])
        self.assertIn("63015", row["delivery_error"])
        # 失败后重试成功 → 新 SID；旧回调再来成为无害 no-op（终态不降级）
        ok, retry_error = retry_outbound_message(self.db, "demo-tenant", message_id, "demo-admin@example.invalid")
        self.assertTrue(ok, retry_error)
        row = self.db.conn.execute("SELECT * FROM messages WHERE id=?", (message_id,)).fetchone()
        self.assertEqual(row["delivery_status"], "sent")
        late_status, late_body = raw_post(self.server, path, fail_params, signature=good_sig)
        self.assertEqual(late_status, 200)
        self.assertFalse(json.loads(late_body)["changed"])
        row = self.db.conn.execute("SELECT * FROM messages WHERE id=?", (message_id,)).fetchone()
        self.assertEqual(row["delivery_status"], "sent")
