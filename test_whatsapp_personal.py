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
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools" / "channels"))

import app as app_module  # noqa: E402
from app import CHANNEL_LABELS, channel_secret_status, inbox_sections, make_server  # noqa: E402
import mock_waha  # noqa: E402

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
    def _event(self, text, msg_id, from_jid="861700000999@c.us", name="Shan", from_me=False, to_jid=None, has_media=False):
        payload = {
            "id": {"_serialized": msg_id},
            "from": from_jid,
            "to": to_jid if to_jid is not None else (from_jid if from_me else "8617555000000@c.us"),
            "fromMe": from_me,
            "body": text,
            "hasMedia": has_media,
            "_data": {"notifyName": name},
        }
        return {"event": "message", "session": "default", "payload": payload}

    def _post(self, event, token=None, hmac_value=None):
        raw = json.dumps(event).encode("utf-8")
        request = urllib.request.Request(
            f"{self.base}/api/v1/channels/whatsapp-personal/webhook/{token or TOKEN}",
            data=raw,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        if hmac_value is not None:
            request.add_header("X-Webhook-Hmac", hmac_value)
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
        self.assertIn("_MOCK", str(reply["external_message_id"]))
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
        reply_id = f"true_861700000999@c.us_MOCK{len(sent)}"
        # 回声（桥自己发出的消息被回推）→ 防护拦截
        status, body = self._post(self._event("已记录以下工况（回声）", reply_id, from_me=True))
        self.assertEqual(json.loads(body)["outcome"]["action"], "ignored_echo")

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
            status, _ = self._post(self._event("你好", "3EB0H2"), hmac_value="sha256=" + "0" * 64)
            self.assertEqual(status, 403)
            # 带正确签名 → 200
            event = self._event("你好", "3EB0H3")
            raw = json.dumps(event).encode("utf-8")
            good = "sha256=" + hmac.new(HMAC_KEY.encode("utf-8"), raw, hashlib.sha256).hexdigest()
            status, _ = self._post(event, hmac_value=good)
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
