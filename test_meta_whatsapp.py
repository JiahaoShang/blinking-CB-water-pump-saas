# -*- coding: utf-8 -*-
"""Meta 官方形态（Cloud API 形状）WhatsApp 通道测试。

覆盖：Webhook 验证握手 / 入站文本全链路（回复经 graph 送出）/ 签名 fail-closed /
状态回执（delivered·read·failed、终态不降级）/ 非文本跳过 / 凭据状态按承载方切换。
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

from app import channel_secret_status, make_server  # noqa: E402
import mock_meta  # noqa: E402

APP_SECRET = "meta-app-secret-xyz"
VERIFY_TOKEN = "verify-token-abc"
TOKEN = "meta-test-token"


class MetaWhatsAppTests(unittest.TestCase):
    """Meta 形态：通道承载方 = meta（WHATSAPP_PROVIDER=meta）。"""

    maxDiff = None

    @classmethod
    def setUpClass(cls):
        cls.temp_dir = tempfile.TemporaryDirectory()
        cls.mock = mock_meta.MockMetaGraphServer(token=TOKEN)
        cls.env_names = (
            "WHATSAPP_PROVIDER", "META_WHATSAPP_TOKEN", "META_PHONE_NUMBER_ID", "META_APP_SECRET",
            "META_VERIFY_TOKEN", "META_API_BASE", "META_API_VERSION",
            "TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_WHATSAPP_FROM", "TWILIO_API_BASE",
            "TWILIO_STATUS_CALLBACK", "TWILIO_PUBLIC_BASE",
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
            "WHATSAPP_PROVIDER": "meta",
            "META_WHATSAPP_TOKEN": TOKEN,
            "META_PHONE_NUMBER_ID": "123456789012345",
            "META_APP_SECRET": APP_SECRET,
            "META_VERIFY_TOKEN": VERIFY_TOKEN,
            "META_API_BASE": cls.mock.base_url(),
            "META_API_VERSION": "v-test",
        })
        cls.db.save_channel_account("demo-tenant", "whatsapp", {"display_name": "链路测试"}, "demo-admin@example.invalid")
        cls.db.set_channel_enabled("demo-tenant", "whatsapp", True, "demo-admin@example.invalid")

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

    # ── 工具 ──
    def _meta_post(self, payload: dict, secret: str | None = None):
        raw = json.dumps(payload).encode("utf-8")
        sig = "sha256=" + hmac.new((secret or APP_SECRET).encode("utf-8"), raw, hashlib.sha256).hexdigest()
        request = urllib.request.Request(
            f"{self.base}/api/v1/channels/whatsapp/webhook",
            data=raw,
            headers={"Content-Type": "application/json", "X-Hub-Signature-256": sig},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return response.status, response.read().decode("utf-8")
        except urllib.error.HTTPError as error:
            return error.code, error.read().decode("utf-8")

    def _inbound_payload(self, from_digits: str, text: str, msg_id: str, name: str = "Shan") -> dict:
        return {
            "object": "whatsapp_business_account",
            "entry": [{
                "id": "WABA-ID-1",
                "changes": [{
                    "field": "messages",
                    "value": {
                        "messaging_product": "whatsapp",
                        "metadata": {"display_phone_number": "17300000000", "phone_number_id": "123456789012345"},
                        "contacts": [{"profile": {"name": name}, "wa_id": from_digits}],
                        "messages": [{
                            "from": from_digits, "id": msg_id, "timestamp": "1700000000",
                            "type": "text", "text": {"body": text},
                        }],
                    },
                }],
            }],
        }

    def _status_payload(self, sid: str, status: str, errors: list | None = None) -> dict:
        item = {"id": sid, "status": status, "timestamp": "1700000010", "recipient_id": "861700000999"}
        if errors:
            item["errors"] = errors
        return {"object": "whatsapp_business_account", "entry": [{"id": "WABA-ID-1", "changes": [{"field": "messages", "value": {"messaging_product": "whatsapp", "statuses": [item]}}]}]}

    # ── 用例 ──
    def test_verify_handshake_ok_and_fail_closed(self):
        url = f"{self.base}/api/v1/channels/whatsapp/webhook?hub.mode=subscribe&hub.verify_token={VERIFY_TOKEN}&hub.challenge=challenge-123"
        with urllib.request.urlopen(url, timeout=5) as response:
            self.assertEqual(response.status, 200)
            self.assertEqual(response.read().decode("utf-8"), "challenge-123")
        bad = f"{self.base}/api/v1/channels/whatsapp/webhook?hub.mode=subscribe&hub.verify_token=wrong&hub.challenge=x"
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(bad, timeout=5)
        self.assertEqual(ctx.exception.code, 403)

    def test_inbound_text_flow_reply_via_meta(self):
        sent_before = len(self.mock.sent_messages())
        status, body = self._meta_post(self._inbound_payload("861700000999", "请报价：flow 15 m3/h, head 30 m, medium clean water", "wamid.in-1"))
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["outcome"]["messages"], 1)
        identity = self.db.get_channel_identity("demo-tenant", "whatsapp", "+861700000999")
        self.assertIsNotNone(identity)
        self.assertEqual(identity["display_name"], "Shan")
        conv_id = identity["conversation_id"]
        buyer = self.db.conn.execute(
            "SELECT * FROM messages WHERE tenant_id='demo-tenant' AND external_message_id='wamid.in-1'"
        ).fetchone()
        self.assertIsNotNone(buyer)
        self.assertEqual(buyer["role"], "buyer")
        # 回复经 graph 送出（to=纯数字；无 whatsapp: 前缀）
        sent = self.mock.sent_messages()[sent_before:]
        self.assertTrue(sent)
        self.assertEqual(sent[-1]["to"], "861700000999")
        self.assertIn("已记录以下工况", sent[-1]["body"])
        reply_row = self.db.conn.execute(
            "SELECT * FROM messages WHERE conversation_id=? ORDER BY rowid DESC LIMIT 1", (conv_id,)
        ).fetchone()
        self.assertEqual(reply_row["delivery_status"], "sent")
        self.assertTrue(str(reply_row["external_message_id"]).startswith("+861700000999:"))
        self.assertIn("wamid.mock-", str(reply_row["external_message_id"]))

    def test_signature_fail_closed(self):
        status, _ = self._meta_post(self._inbound_payload("861700000888", "你好", "wamid.bad-1"), secret="wrong-secret")
        self.assertEqual(status, 403)
        row = self.db.conn.execute("SELECT * FROM messages WHERE external_message_id='wamid.bad-1'").fetchone()
        self.assertIsNone(row)

    def test_statuses_delivered_read_no_downgrade_and_failed(self):
        # 第一条：入站 → 回复 sent → delivered 回执推进
        self._meta_post(self._inbound_payload("861700000777", "需要：流量 12 m3/h、介质清水", "wamid.in-st-1"))
        identity = self.db.get_channel_identity("demo-tenant", "whatsapp", "+861700000777")
        self.assertIsNotNone(identity)
        conv_id = identity["conversation_id"]
        reply = self.db.conn.execute(
            "SELECT * FROM messages WHERE conversation_id=? ORDER BY rowid DESC LIMIT 1", (conv_id,)
        ).fetchone()
        wamid = str(reply["external_message_id"]).split(":", 1)[1]
        status, body = self._meta_post(self._status_payload(wamid, "delivered"))
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["outcome"]["statuses"], 1)
        row = self.db.conn.execute("SELECT delivery_status FROM messages WHERE id=?", (reply["id"],)).fetchone()
        self.assertEqual(row["delivery_status"], "delivered")
        # read 不降级、failed 不降级（终态保护）
        self._meta_post(self._status_payload(wamid, "read"))
        self._meta_post(self._status_payload(wamid, "failed", errors=[{"code": 131047, "title": "Re-engagement message"}]))
        row = self.db.conn.execute("SELECT delivery_status FROM messages WHERE id=?", (reply["id"],)).fetchone()
        self.assertEqual(row["delivery_status"], "delivered")
        # 第二条：failed 回执 → 标记失败并带错误
        self._meta_post(self._inbound_payload("861700000666", "需要：流量 7 m3/h、介质清水", "wamid.in-st-2"))
        identity2 = self.db.get_channel_identity("demo-tenant", "whatsapp", "+861700000666")
        conv2 = identity2["conversation_id"]
        reply2 = self.db.conn.execute(
            "SELECT * FROM messages WHERE conversation_id=? ORDER BY rowid DESC LIMIT 1", (conv2,)
        ).fetchone()
        wamid2 = str(reply2["external_message_id"]).split(":", 1)[1]
        self._meta_post(self._status_payload(wamid2, "failed", errors=[{"code": 131047, "title": "Re-engagement message"}]))
        row2 = self.db.conn.execute("SELECT delivery_status, delivery_error FROM messages WHERE id=?", (reply2["id"],)).fetchone()
        self.assertEqual(row2["delivery_status"], "failed")
        self.assertIn("131047", str(row2["delivery_error"]))

    def test_non_text_ignored(self):
        payload = self._inbound_payload("861700000555", "ignored", "wamid.in-img-1")
        payload["entry"][0]["changes"][0]["value"]["messages"][0]["type"] = "image"
        payload["entry"][0]["changes"][0]["value"]["messages"][0].pop("text", None)
        status, body = self._meta_post(payload)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["outcome"]["ignored"], 1)
        identity = self.db.get_channel_identity("demo-tenant", "whatsapp", "+861700000555")
        self.assertIsNone(identity)

    def test_secret_status_provider_aware(self):
        status = channel_secret_status("whatsapp")
        self.assertTrue(status["configured"])  # meta 变量齐 → 已配置
        os.environ.pop("WHATSAPP_PROVIDER", None)
        try:
            default_view = channel_secret_status("whatsapp")
            self.assertIn("TWILIO_ACCOUNT_SID", default_view["missing"])  # 默认 twilio 视角
        finally:
            os.environ["WHATSAPP_PROVIDER"] = "meta"


if __name__ == "__main__":
    unittest.main()
