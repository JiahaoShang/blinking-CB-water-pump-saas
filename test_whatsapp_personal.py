# -*- coding: utf-8 -*-
"""WhatsApp（个人版）渠道（WAHA 桥）测试。

覆盖：URL 令牌 fail-closed / 入站文本全链路（回复经桥送出）/ 群聊·广播·媒体·他人外发跳过 /
自聊放行与回声防护 / 去重 / 节奏层（拟人通知 + 限速阻断）/ HMAC 加固 / 注册表与标签。
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import sys
import http.client
import tempfile
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools" / "channels"))

import app as app_module  # noqa: E402
from app import CHANNEL_LABELS, channel_secret_status, inbox_sections, make_server  # noqa: E402
import mock_waha  # noqa: E402

class _MockDify:
    """最小 Dify 问答模拟（/chat-messages → 返回固定答案）。"""

    def __init__(self):
        import http.server
        import threading as _threading

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                self.rfile.read(length)
                body = json.dumps({
                    "answer": "DIFY-MOCK：现有产品包括 CB-120C 与 SLICE-T1。",
                    "metadata": {"retriever_resources": []},
                }).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self._server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        _threading.Thread(target=self._server.serve_forever, daemon=True).start()

    def base_url(self):
        return f"http://127.0.0.1:{self._server.server_port}/v1"

    def stop(self):
        self._server.shutdown()
        self._server.server_close()


def _http(server, method, path, fields=None, cookie=None):
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
    raw = response.read()
    headers_out = dict(response.getheaders())
    status = response.status
    conn.close()
    return status, headers_out, raw


def _login(server, email, password):
    status, headers, _ = _http(server, "POST", "/login", {"email": email, "password": password})
    if status not in (302, 303) or "Set-Cookie" not in headers:
        raise AssertionError(f"登录失败: {email} -> {status}")
    return headers["Set-Cookie"].split(";", 1)[0]


TOKEN = "personal-token-0123456789abcdef"
API_KEY = "waha-test-api-key"
HMAC_KEY = "waha-test-hmac-key"


class WhatsAppPersonalTests(unittest.TestCase):
    maxDiff = None

    @classmethod
    def setUpClass(cls):
        cls.temp_dir = tempfile.TemporaryDirectory()
        cls.mock = mock_waha.MockWahaServer(api_key=API_KEY)
        cls.env_names = (
            "WHATSAPP_PERSONAL_TOKEN", "WHATSAPP_PERSONAL_API_KEY", "WHATSAPP_PERSONAL_BASE",
            "WHATSAPP_PERSONAL_HMAC_KEY", "WHATSAPP_PERSONAL_PACING", "WHATSAPP_PERSONAL_MIN_GAP",
            "WHATSAPP_PERSONAL_MAX_PER_MIN", "WHATSAPP_PERSONAL_MAX_PER_HOUR",
            "WHATSAPP_PERSONAL_TYPING_BASE", "WHATSAPP_PERSONAL_TYPING_PER_CHAR", "WHATSAPP_PERSONAL_TYPING_MAX",
            "WHATSAPP_PROVIDER", "TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_WHATSAPP_FROM",
            "TWILIO_API_BASE", "META_WHATSAPP_TOKEN", "META_PHONE_NUMBER_ID", "META_APP_SECRET", "META_VERIFY_TOKEN",
            "DIFY_QA_URL", "DIFY_QA_APP_KEY", "DIFY_KB_DATASET_ID", "DIFY_KB_API_KEY", "DIFY_KB_URL",
        )
        cls.env_backup = {name: os.environ.get(name) for name in cls.env_names}
        for name in cls.env_names:
            os.environ.pop(name, None)
        cls.server = make_server(Path(cls.temp_dir.name) / "app.db", port=0)
        cls.db = cls.server.RequestHandlerClass.db
        cls.db.seed_demo_users()
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.server.server_port}"
        os.environ.update({
            "WHATSAPP_PERSONAL_TOKEN": TOKEN,
            "WHATSAPP_PERSONAL_API_KEY": API_KEY,
            "WHATSAPP_PERSONAL_BASE": cls.mock.base_url(),
            "WHATSAPP_PERSONAL_PACING": "0",  # 默认关（用例按需打开）
        })
        cls.db.save_channel_account("demo-tenant", "whatsapp_personal", {"display_name": "个人版"}, "demo-admin@example.invalid")
        cls.db.set_channel_enabled("demo-tenant", "whatsapp_personal", True, "demo-admin@example.invalid")
        cls.admin_cookie = _login(cls.server, "demo-admin@example.invalid", "demo-admin")

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

    def setUp(self):
        app_module._whatsapp_personal_reset_state()

    # ── 工具 ──
    def _event(self, text, msg_id, from_jid="861700000999@c.us", name="Shan", from_me=False, to_jid=None, has_media=False, source=None):
        payload = {
            "id": {"_serialized": msg_id},
            "from": from_jid,
            "to": to_jid if to_jid is not None else (from_jid if from_me else "8617555000000@c.us"),
            "fromMe": from_me,
            "body": text,
            "hasMedia": has_media,
            "_data": {"notifyName": name},
        }
        if source:
            payload["source"] = source
        return {"event": "message.any", "session": "default", "payload": payload}

    def _post(self, event, token=None, hmac_value=None, hmac_algorithm=None):
        raw = json.dumps(event).encode("utf-8")
        request = urllib.request.Request(
            f"{self.base}/api/v1/channels/whatsapp-personal/webhook/{token or TOKEN}",
            data=raw,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        if hmac_value is not None:
            request.add_header("X-Webhook-Hmac", hmac_value)
        if hmac_algorithm is not None:
            request.add_header("X-Webhook-Hmac-Algorithm", hmac_algorithm)
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                return response.status, response.read().decode("utf-8")
        except urllib.error.HTTPError as error:
            return error.code, error.read().decode("utf-8")

    # ── 用例 ──
    def test_token_fail_closed(self):
        status, _ = self._post(self._event("你好", "3EB0BAD1"), token="wrong-token-0000000000000000")
        self.assertEqual(status, 403)
        status2, _ = self._post(self._event("你好", "3EB0BAD2"), token="short")
        self.assertIn(status2, (403, 404))
        row = self.db.conn.execute("SELECT * FROM messages WHERE external_message_id IN ('3EB0BAD1','3EB0BAD2')").fetchone()
        self.assertIsNone(row)

    def test_inbound_text_flow_reply_via_bridge(self):
        sent_before = len(self.mock.sent_texts())
        notes_before = len(self.mock.notifications())
        status, body = self._post(self._event("需求：流量 10 m³/h、介质清水", "3EB0IN0001"))
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["outcome"]["action"], "extracted")
        identity = self.db.get_channel_identity("demo-tenant", "whatsapp_personal", "861700000999@c.us")
        self.assertIsNotNone(identity)
        self.assertEqual(identity["display_name"], "Shan")
        conv_id = identity["conversation_id"]
        buyer = self.db.conn.execute(
            "SELECT * FROM messages WHERE tenant_id='demo-tenant' AND external_message_id='3EB0IN0001'"
        ).fetchone()
        self.assertIsNotNone(buyer)
        self.assertEqual(buyer["role"], "buyer")
        self.assertEqual(buyer["channel"], "whatsapp_personal")
        sent = self.mock.sent_texts()[sent_before:]
        self.assertTrue(sent)
        self.assertEqual(sent[-1]["chatId"], "861700000999@c.us")
        self.assertEqual(sent[-1]["session"], "default")
        self.assertIn("已记录以下工况", sent[-1]["text"])
        reply = self.db.conn.execute(
            "SELECT * FROM messages WHERE conversation_id=? ORDER BY rowid DESC LIMIT 1", (conv_id,)
        ).fetchone()
        self.assertEqual(reply["delivery_status"], "sent")
        self.assertTrue(str(reply["external_message_id"]).startswith("861700000999@c.us:"))
        self.assertIn("3EB0MOCK", str(reply["external_message_id"]))
        # 节奏关闭 → 不产生已读/输入中通知
        self.assertEqual(len(self.mock.notifications()), notes_before)

    def test_group_broadcast_media_and_outgoing_ignored(self):
        status, body = self._post(self._event("群消息", "3EB0G1", from_jid="123456789-987654@g.us"))
        self.assertEqual(json.loads(body)["outcome"]["action"], "ignored_groupchat")
        status, body = self._post(self._event("广播", "3EB0B1", from_jid="status@broadcast"))
        self.assertEqual(json.loads(body)["outcome"]["action"], "ignored_groupchat")
        status, body = self._post(self._event("图", "3EB0M1", has_media=True))
        self.assertEqual(json.loads(body)["outcome"]["action"], "ignored_non_text")
        status, body = self._post(self._event("自己发给别人", "3EB0O1", from_me=True, to_jid="8617111000000@c.us"))
        self.assertEqual(json.loads(body)["outcome"]["action"], "ignored_outgoing")
        self.assertIsNone(self.db.get_channel_identity("demo-tenant", "whatsapp_personal", "123456789-987654@g.us"))

    def test_self_chat_allowed_and_echo_guard(self):
        # 自聊（from == to，fromMe）放行（便于自测）
        status, body = self._post(self._event("自测：流量 9 m³/h", "3EB0SELF1", from_me=True))
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["outcome"]["action"], "extracted")
        sent = self.mock.sent_texts()
        self.assertTrue(sent)
        reply_id = f"3EB0MOCK{len(sent)}"
        # 回声（桥自己发出的消息被回推）→ 防护拦截
        status, body = self._post(self._event("已记录以下工况（回声）", reply_id, from_me=True))
        self.assertEqual(json.loads(body)["outcome"]["action"], "ignored_echo")
        # API 来源（本桥发出）即便是新 id 也直接判回声
        status, body = self._post(self._event("由桥发出", "3EB0API9", from_me=True, source="api"))
        self.assertEqual(json.loads(body)["outcome"]["action"], "ignored_echo")

    def test_self_chat_lid_mismatch_detected(self):
        """自聊但 from/to 为不同格式（PN vs LID）→ 依据 SELF_IDS 集合判定为自聊并放行。"""
        os.environ["WHATSAPP_PERSONAL_SELF_IDS"] = "861700000999@c.us,999888777@lid"
        try:
            status, body = self._post(self._event("自测LID：流量 7 m³/h", "3EB0LID1", from_jid="999888777@lid", to_jid="861700000999@c.us", from_me=True))
            self.assertEqual(status, 200)
            self.assertEqual(json.loads(body)["outcome"]["action"], "extracted")
        finally:
            os.environ.pop("WHATSAPP_PERSONAL_SELF_IDS", None)

    def test_question_prefers_dify_on_personal_channel(self):
        """回归修复：个人版渠道的问题必须走 Dify 优先分流（此前遗漏在 tuple 外）。"""
        mock_dify = _MockDify()
        saved = {key: os.environ.get(key) for key in ("DIFY_QA_URL", "DIFY_QA_APP_KEY")}
        os.environ["DIFY_QA_URL"] = mock_dify.base_url()
        os.environ["DIFY_QA_APP_KEY"] = "mock-key"
        try:
            sent_before = len(self.mock.sent_texts())
            status, body = self._post(self._event("你们有哪些产品？", "3EB0DIFY1"))
            self.assertEqual(status, 200)
            self.assertEqual(json.loads(body)["outcome"].get("qa_source"), "dify")
            sent = self.mock.sent_texts()[sent_before:]
            self.assertTrue(sent)
            self.assertIn("DIFY-MOCK", sent[-1]["text"])
        finally:
            for key, value in saved.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
            mock_dify.stop()

    def test_self_chat_empty_to_parsed_from_serialized(self):
        """真机回归：app 发出的自聊消息 to 为空 → 用 _serialized 的 chatJid 判定自聊并放行。"""
        os.environ["WHATSAPP_PERSONAL_SELF_IDS"] = "861700000999@c.us,999888777@lid"
        try:
            event = self._event("自测空to：流量 6 m³/h", "true_999888777@lid_3EB0ZZ1", from_jid="999888777@lid", to_jid="", from_me=True)
            status, body = self._post(event)
            self.assertEqual(status, 200)
            self.assertEqual(json.loads(body)["outcome"]["action"], "extracted")
            # chatJid 指向别人（朋友）→ 仍应忽略
            event2 = self._event("发给朋友的消息", "true_8617111000000@c.us_3EB0ZZ2", from_jid="999888777@lid", to_jid="", from_me=True)
            status, body = self._post(event2)
            self.assertEqual(json.loads(body)["outcome"]["action"], "ignored_outgoing")
        finally:
            os.environ.pop("WHATSAPP_PERSONAL_SELF_IDS", None)

    def test_bind_area_rendered_on_channels_page(self):
        """渠道页「WhatsApp（个人版）」卡片渲染扫码绑定区（容器 + 脚本接线）。"""
        status, _, raw = _http(self.server, "GET", "/admin/channels", cookie=self.admin_cookie)
        self.assertEqual(status, 200)
        page = raw.decode("utf-8")
        self.assertIn("waha-bind", page)
        self.assertIn("waha-qr", page)
        self.assertIn("waha-start", page)
        self.assertIn("/admin/channels/whatsapp-personal/session.json", page)
        self.assertIn("/admin/channels/whatsapp-personal/qr.png", page)

    def test_bind_session_state_and_qr_endpoints(self):
        """扫码绑定区：状态 JSON（含登录名）/二维码图片；非扫码态 409；未登录重定向。"""
        status, headers, _ = _http(self.server, "GET", "/admin/channels/whatsapp-personal/session.json")
        self.assertIn(status, (302, 303))  # 未登录 → 登录页，不泄露内容

        self.mock.set_status("WORKING")
        status, headers, raw = _http(self.server, "GET", "/admin/channels/whatsapp-personal/session.json", cookie=self.admin_cookie)
        self.assertEqual(status, 200)
        data = json.loads(raw)
        self.assertTrue(data["ok"])
        self.assertEqual(data["status"], "WORKING")
        self.assertEqual(data["status_label"], "已连接")
        self.assertEqual(data["me_name"], "MockUser")
        self.assertTrue(data["channel_enabled"])

        # 已连接 → 二维码接口 409（不触碰 WAHA 会挂起的路径）
        status, headers, raw = _http(self.server, "GET", "/admin/channels/whatsapp-personal/qr.png", cookie=self.admin_cookie)
        self.assertEqual(status, 409)
        self.assertFalse(json.loads(raw)["ok"])

        # 扫码态 → 返回真实 PNG 字节
        self.mock.set_status("SCAN_QR_CODE")
        status, headers, raw = _http(self.server, "GET", "/admin/channels/whatsapp-personal/qr.png", cookie=self.admin_cookie)
        self.assertEqual(status, 200)
        self.assertEqual(headers.get("Content-Type"), "image/png")
        self.assertTrue(raw.startswith(b"\x89PNG"))
        status, headers, raw = _http(self.server, "GET", "/admin/channels/whatsapp-personal/session.json", cookie=self.admin_cookie)
        self.assertEqual(json.loads(raw)["status"], "SCAN_QR_CODE")

    def test_bind_start_session_button(self):
        """「启动会话并取码」：POST 启动 → mock 收到 start → 状态回到扫码态。"""
        self.mock.set_status("STOPPED")
        before = self.mock.start_calls
        status, headers, raw = _http(self.server, "POST", "/admin/channels/whatsapp-personal/session/start", fields={}, cookie=self.admin_cookie)
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(raw)["ok"])
        self.assertEqual(self.mock.start_calls, before + 1)
        self.assertEqual(self.mock.status, "SCAN_QR_CODE")

    def test_bind_bridge_down_is_reported_not_crashed(self):
        """桥不可达：状态 JSON → ok=false（HTTP 200）；二维码 → 502；均不抛栈。"""
        saved = os.environ.get("WHATSAPP_PERSONAL_BASE")
        os.environ["WHATSAPP_PERSONAL_BASE"] = "http://127.0.0.1:9"
        try:
            status, headers, raw = _http(self.server, "GET", "/admin/channels/whatsapp-personal/session.json", cookie=self.admin_cookie)
            self.assertEqual(status, 200)
            data = json.loads(raw)
            self.assertFalse(data["ok"])
            self.assertIn("桥", data["error"])
            status, headers, raw = _http(self.server, "GET", "/admin/channels/whatsapp-personal/qr.png", cookie=self.admin_cookie)
            self.assertEqual(status, 502)
        finally:
            if saved is None:
                os.environ.pop("WHATSAPP_PERSONAL_BASE", None)
            else:
                os.environ["WHATSAPP_PERSONAL_BASE"] = saved

    def test_bind_start_recovers_failed_session(self):
        """FAILED 会话点「重置会话并取码」→ 平台自动重启（不再对 FAILED 干刷 start）。"""
        self.mock.set_status("FAILED")
        before = self.mock.restart_calls
        status, headers, raw = _http(self.server, "POST", "/admin/channels/whatsapp-personal/session/start", fields={}, cookie=self.admin_cookie)
        self.assertEqual(status, 200)
        data = json.loads(raw)
        self.assertTrue(data["ok"])
        self.assertEqual(self.mock.restart_calls, before + 1)
        self.assertEqual(data["note"], "restarted")
        self.assertEqual(data["status"], "SCAN_QR_CODE")

    def test_bind_start_dedup_rapid_clicks(self):
        """连点防抖：2.5 秒内第二次启动请求直接返回 busy（不再打桥、不再刷爆）。"""
        self.mock.set_status("STOPPED")
        status, _, raw = _http(self.server, "POST", "/admin/channels/whatsapp-personal/session/start", fields={}, cookie=self.admin_cookie)
        first = json.loads(raw)
        status, _, raw = _http(self.server, "POST", "/admin/channels/whatsapp-personal/session/start", fields={}, cookie=self.admin_cookie)
        second = json.loads(raw)
        self.assertEqual(first["note"], "started")
        self.assertEqual(second["note"], "busy")
        self.assertEqual(self.mock.start_calls, 1)

    def test_inbox_hides_history_when_disconnected(self):
        """用户要求：未连接（未登录）时不显示历史聊天记录；连接后恢复显示。"""
        self.mock.set_status("WORKING")
        self._post(self._event("需求：流量 10 m³/h、介质清水", "3EB0INBOX1"))
        identity = self.db.get_channel_identity("demo-tenant", "whatsapp_personal", "861700000999@c.us")
        self.assertIsNotNone(identity)
        conversation_id = identity["conversation_id"]
        # 已连接 → 会话在列
        status, _, raw = _http(self.server, "GET", "/admin/inbox/whatsapp_personal", cookie=self.admin_cookie)
        self.assertEqual(status, 200)
        page = raw.decode("utf-8")
        self.assertIn("Shan", page)
        self.assertNotIn("账号未连接", page)
        # 退出登录（等待扫码）→ 不显示历史
        self.mock.set_status("SCAN_QR_CODE")
        status, _, raw = _http(self.server, "GET", "/admin/inbox/whatsapp_personal", cookie=self.admin_cookie)
        page = raw.decode("utf-8")
        self.assertIn("账号未连接", page)
        self.assertNotIn("Shan", page)
        # 详情页同样拦截
        status, _, raw = _http(self.server, "GET", f"/admin/inbox/whatsapp_personal/{conversation_id}", cookie=self.admin_cookie)
        page = raw.decode("utf-8")
        self.assertIn("账号未连接", page)
        self.assertNotIn("已记录以下工况", page)
        self.mock.set_status("WORKING")

    def _status_event(self, status, session="default"):
        return {"event": "session.status", "session": session, "payload": {"name": session, "status": status}}

    def test_scan_success_auto_enables_channel(self):
        """扫码成功（SCAN_QR_CODE → WORKING）→ 渠道自动启用；单独 WORKING 不打扰手动停用。"""
        try:
            self.db.set_channel_enabled("demo-tenant", "whatsapp_personal", False, "test")
            # ① 无扫码前置的 WORKING → 不自动启用（尊重手动停用）
            status, raw = self._post(self._status_event("WORKING"))
            self.assertEqual(status, 200)
            account = self.db.get_channel_account("demo-tenant", "whatsapp_personal")
            self.assertEqual(account["status"], "disabled")
            # ② 扫码流程：SCAN_QR_CODE → WORKING → 自动启用 + 审计
            status, raw = self._post(self._status_event("SCAN_QR_CODE"))
            self.assertEqual(status, 200)
            status, raw = self._post(self._status_event("WORKING"))
            self.assertEqual(status, 200)
            self.assertEqual(json.loads(raw)["outcome"]["action"], "auto_enabled")
            account = self.db.get_channel_account("demo-tenant", "whatsapp_personal")
            self.assertEqual(account["status"], "enabled")
            row = self.db.conn.execute(
                "SELECT action, actor FROM audit_log WHERE entity_id='whatsapp_personal' ORDER BY rowid DESC LIMIT 1"
            ).fetchone()
            self.assertEqual(row["action"], "channel_enabled")
            self.assertEqual(row["actor"], "system:scan-auto-enable")
        finally:
            self.db.set_channel_enabled("demo-tenant", "whatsapp_personal", True, "test-restore")

    def test_duplicate_dedup(self):
        self._post(self._event("需求：流量 11 m³/h", "3EB0DUP1"))
        identity = self.db.get_channel_identity("demo-tenant", "whatsapp_personal", "861700000999@c.us")
        conv_id = identity["conversation_id"]
        n_before = self.db.conn.execute("SELECT COUNT(*) AS n FROM messages WHERE conversation_id=?", (conv_id,)).fetchone()["n"]
        sent_before = len(self.mock.sent_texts())
        status, body = self._post(self._event("需求：流量 11 m³/h", "3EB0DUP1"))
        self.assertEqual(json.loads(body)["outcome"]["action"], "duplicate")
        n_after = self.db.conn.execute("SELECT COUNT(*) AS n FROM messages WHERE conversation_id=?", (conv_id,)).fetchone()["n"]
        self.assertEqual(n_before, n_after)
        self.assertEqual(len(self.mock.sent_texts()), sent_before)

    def test_pacing_notifications_and_caps(self):
        os.environ.update({
            "WHATSAPP_PERSONAL_PACING": "1",
            "WHATSAPP_PERSONAL_MIN_GAP": "0",
            "WHATSAPP_PERSONAL_MAX_PER_MIN": "8",
            "WHATSAPP_PERSONAL_MAX_PER_HOUR": "60",
            "WHATSAPP_PERSONAL_TYPING_BASE": "0",
            "WHATSAPP_PERSONAL_TYPING_PER_CHAR": "0",
            "WHATSAPP_PERSONAL_TYPING_MAX": "0",
        })
        try:
            notes_before = len(self.mock.notifications())
            self._post(self._event("需求：流量 12 m³/h、介质清水", "3EB0P1"))
            notes = self.mock.notifications()[notes_before:]
            paths = [n["path"] for n in notes]
            self.assertIn("/api/sendSeen", paths)
            self.assertIn("/api/startTyping", paths)
            self.assertIn("/api/stopTyping", paths)
            self.assertLess(paths.index("/api/startTyping"), paths.index("/api/stopTyping"))
            # 限速：每分钟上限 1 → 第二条被节奏层阻断（消息标记失败、原因可见）
            os.environ["WHATSAPP_PERSONAL_MAX_PER_MIN"] = "1"
            app_module._whatsapp_personal_reset_state()
            self._post(self._event("需求：流量 13 m³/h、介质清水", "3EB0P2"))
            status, body = self._post(self._event("需求：流量 14 m³/h、介质清水", "3EB0P3", from_jid="861700000888@c.us"))
            identity = self.db.get_channel_identity("demo-tenant", "whatsapp_personal", "861700000888@c.us")
            self.assertIsNotNone(identity)
            reply = self.db.conn.execute(
                "SELECT * FROM messages WHERE conversation_id=? ORDER BY rowid DESC LIMIT 1", (identity["conversation_id"],)
            ).fetchone()
            self.assertEqual(reply["delivery_status"], "failed")
            self.assertIn("节奏层", str(reply["delivery_error"]))
        finally:
            os.environ.update({
                "WHATSAPP_PERSONAL_PACING": "0",
                "WHATSAPP_PERSONAL_MIN_GAP": "3",
                "WHATSAPP_PERSONAL_MAX_PER_MIN": "8",
                "WHATSAPP_PERSONAL_MAX_PER_HOUR": "60",
                "WHATSAPP_PERSONAL_TYPING_BASE": "1.5",
                "WHATSAPP_PERSONAL_TYPING_PER_CHAR": "0.03",
                "WHATSAPP_PERSONAL_TYPING_MAX": "6",
            })
            app_module._whatsapp_personal_reset_state()

    def test_hmac_strict_when_header_present(self):
        os.environ["WHATSAPP_PERSONAL_HMAC_KEY"] = HMAC_KEY
        try:
            # 无签名头：仅凭 URL 令牌 → 放行（宽容模式）
            status, body = self._post(self._event("你好", "3EB0H1"))
            self.assertEqual(status, 200)
            # 带错误签名 → 403
            status, _ = self._post(self._event("你好", "3EB0H2"), hmac_value="0" * 128, hmac_algorithm="sha512")
            self.assertEqual(status, 403)
            # 带正确签名（WAHA 默认 sha512）→ 200
            event = self._event("你好", "3EB0H3")
            raw = json.dumps(event).encode("utf-8")
            good = hmac.new(HMAC_KEY.encode("utf-8"), raw, hashlib.sha512).hexdigest()
            status, _ = self._post(event, hmac_value=good, hmac_algorithm="sha512")
            self.assertEqual(status, 200)
            # 指定 sha256 算法也支持 → 200
            event2 = self._event("你好", "3EB0H4")
            raw2 = json.dumps(event2).encode("utf-8")
            good2 = hmac.new(HMAC_KEY.encode("utf-8"), raw2, hashlib.sha256).hexdigest()
            status, _ = self._post(event2, hmac_value=good2, hmac_algorithm="sha256")
            self.assertEqual(status, 200)
        finally:
            os.environ.pop("WHATSAPP_PERSONAL_HMAC_KEY", None)

    def test_registry_labels_and_secret_status(self):
        self.assertEqual(CHANNEL_LABELS["whatsapp_personal"], "WhatsApp（个人版）")
        sections = inbox_sections(self.db, "demo-tenant")
        slugs = [s["slug"] for s in sections]
        self.assertIn("whatsapp_personal", slugs)
        entry = next(s for s in sections if s["slug"] == "whatsapp_personal")
        self.assertEqual(entry["label"], "WhatsApp（个人版）")
        self.assertEqual(entry["kind"], "native")
        secret = channel_secret_status("whatsapp_personal")
        self.assertTrue(secret["configured"])


if __name__ == "__main__":
    unittest.main()
