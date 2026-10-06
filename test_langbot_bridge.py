"""V1-04 · LangBot 桥接（第一波·传输层）测试。

契约（grill-me 定稿方案）：
- LangBot 是纯传输层：Webhook 推事件进来（URL 令牌即凭据，fail-closed），发送走其 send_message API（X-API-Key）；
- 收到 person 消息一律返回 skip_pipeline=true（平台是唯一「主脑」，LangBot 流水线不介入）；
- 渠道路由与 Telegram 同口径（复用同一分流逻辑）；uuid 为事件去重键；
- adapter_name（如 telegram）落档 channel_identities.platform_meta，收件箱按平台展示。
"""
import http.client
import json
import os
import sys
import tempfile
import threading
import unittest
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from app import make_server  # noqa: E402


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


def json_http(server, method, path, payload=None, headers=None):
    conn = http.client.HTTPConnection("127.0.0.1", server.server_port)
    request_headers = {"Content-Type": "application/json", **(headers or {})}
    conn.request(method, path, json.dumps(payload).encode("utf-8") if payload is not None else None, request_headers)
    response = conn.getresponse()
    data = response.read()
    result = response.status, json.loads(data.decode("utf-8")) if data else None
    conn.close()
    return result


class _LangBotMock:
    """最小 LangBot 服务模拟：记录请求，send_message 返回 code=0。"""

    def __init__(self, api_key="lbk_test_key"):
        state = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length).decode("utf-8")
                state.requests.append({"path": self.path, "headers": dict(self.headers), "body": raw})
                if self.headers.get("X-API-Key") == api_key:
                    payload = {"code": 0, "msg": "ok", "data": {"sent": True}}
                    status = 200
                else:
                    payload = {"code": 401, "msg": "invalid api key", "data": None}
                    status = 401
                body = json.dumps(payload).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.requests = []
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self._server.server_address[1]}"
        threading.Thread(target=self._server.serve_forever, daemon=True).start()

    def sent_messages(self):
        """已收到的 send_message 调用（解析后的 message_chain 文本 + 目标）。"""
        items = []
        for record in self.requests:
            if record["path"].endswith("/send_message"):
                body = json.loads(record["body"])
                text = "".join(
                    str(segment.get("text") or "")
                    for segment in body.get("message_chain") or []
                    if isinstance(segment, dict) and segment.get("type") == "Plain"
                )
                items.append({"target_id": body.get("target_id"), "text": text, "headers": {str(key).lower(): value for key, value in record["headers"].items()}})
        return items

    def stop(self):
        self._server.shutdown()
        self._server.server_close()


class LangBotBridgeTests(unittest.TestCase):
    """LangBot 通道：收信落库与展示、去重、skip_pipeline、回复直达、失败可见与重试、fail-closed。"""

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
        cls.token = "tok_langbot_test_0001_abcdef"
        cls.db.save_channel_account("demo-tenant", "langbot", {"bot_uuid": "bot-0001", "webhook_token": cls.token}, "demo-admin@example.invalid")
        cls.db.set_channel_enabled("demo-tenant", "langbot", True, "demo-admin@example.invalid")

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
    def event_for(self, sender_id, text, uuid, adapter="telegram", name="Ivan"):
        return {
            "uuid": uuid,
            "event_type": "bot.person_message",
            "data": {
                "bot_uuid": "bot-0001",
                "adapter_name": adapter,
                "sender": {"id": sender_id, "name": name},
                "message": [{"type": "Plain", "text": text}],
                "timestamp": 1759500000,
            },
        }

    def post_event(self, event, token=None):
        return json_http(self.server, "POST", f"/api/v1/channels/langbot/webhook/{token or self.token}", event)

    def conversation_for(self, sender_id):
        row = self.db.conn.execute(
            "SELECT conversation_id FROM channel_identities WHERE tenant_id='demo-tenant' AND channel='langbot' AND external_user_id=?",
            (str(sender_id),),
        ).fetchone()
        return row["conversation_id"] if row else None

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
    def test_webhook_receives_stores_and_skips_pipeline(self):
        """收信：落库 + 平台字段落档 + 自动分流回复经 LangBot 发送 + skip_pipeline=true。"""
        status, body = self.post_event(self.event_for(7301, "flow 15 m3/h, head 30 m, medium clean water", "ev-0001"))
        self.assertEqual(status, 200)
        self.assertEqual(body["status"], "ok")
        self.assertIs(body["skip_pipeline"], True)
        conversation_id = self.conversation_for(7301)
        self.assertTrue(conversation_id)
        conversation = self.db.get_conversation("demo-tenant", conversation_id)
        self.assertEqual(conversation["source"], "langbot")
        buyer_row = self.last_message(conversation_id, "langbot")
        self.assertEqual(buyer_row["role"], "buyer")
        self.assertEqual(buyer_row["external_message_id"], "ev-0001")
        identity = self.db.conn.execute(
            "SELECT * FROM channel_identities WHERE tenant_id='demo-tenant' AND conversation_id=?", (conversation_id,)
        ).fetchone()
        self.assertEqual(identity["display_name"], "Ivan")
        self.assertEqual(json.loads(identity["platform_meta"])["adapter"], "telegram")
        # 自动分流：工况整理回复经 LangBot 发送（target = 平台侧用户 ID；X-API-Key 正确）
        sent = self.mock.sent_messages()
        self.assertTrue(sent)
        self.assertEqual(sent[-1]["target_id"], "7301")
        self.assertEqual(sent[-1]["headers"].get("x-api-key"), "lbk_test_key")
        self.assertIn("确认", sent[-1]["text"])
        reply_row = self.last_message(conversation_id, "channel-requirements")
        self.assertEqual(reply_row["delivery_status"], "sent")

    def test_question_asks_human_via_channel(self):
        """提问样文本（资料外）→ 转人工提示直达渠道。"""
        status, body = self.post_event(self.event_for(7302, "你们能出口到德国吗？", "ev-0002"))
        self.assertEqual(status, 200)
        self.assertEqual(body["outcome"], "answer_unresolved")
        sent = self.mock.sent_messages()
        self.assertTrue(any("「人工」" in item["text"] for item in sent if item["target_id"] == "7302"))

    def test_duplicate_event_processed_once(self):
        event = self.event_for(7303, "hello", "ev-0003")
        status, _body = self.post_event(event)
        self.assertEqual(status, 200)
        conversation_id = self.conversation_for(7303)
        count_before = self.db.conn.execute("SELECT COUNT(*) AS n FROM messages WHERE conversation_id=?", (conversation_id,)).fetchone()["n"]
        sent_before = len(self.mock.sent_messages())
        status, body = self.post_event(event)
        self.assertEqual(status, 200)
        self.assertEqual(body["outcome"], "duplicate")
        count_after = self.db.conn.execute("SELECT COUNT(*) AS n FROM messages WHERE conversation_id=?", (conversation_id,)).fetchone()["n"]
        self.assertEqual(count_before, count_after)
        self.assertEqual(sent_before, len(self.mock.sent_messages()))

    def test_wrong_token_and_disabled_channel_fail_closed(self):
        event = self.event_for(7304, "hello", "ev-0004")
        status, body = self.post_event(event, token="wrong_token_000000000000")
        self.assertEqual(status, 404)
        self.assertEqual(body.get("error"), "webhook_not_found")
        self.assertIsNone(self.conversation_for(7304))
        # 渠道停用 → 200 skipped，不处理
        self.db.set_channel_enabled("demo-tenant", "langbot", False, "demo-admin@example.invalid")
        try:
            status, body = self.post_event(event)
            self.assertEqual(status, 200)
            self.assertEqual(body.get("skipped"), "channel_disabled")
            self.assertIsNone(self.conversation_for(7304))
        finally:
            self.db.set_channel_enabled("demo-tenant", "langbot", True, "demo-admin@example.invalid")

    def test_staff_reply_failure_visible_and_retry(self):
        """卖家回复直达；发送失败可见（含原因）+ 一键重试成功。"""
        status, _body = self.post_event(self.event_for(7305, "hello", "ev-0005"))
        self.assertEqual(status, 200)
        conversation_id = self.conversation_for(7305)
        # 成功回复
        status, headers, _ = form_request(self.server, "POST", f"/admin/inbox/langbot/{conversation_id}/reply", {"body": "您好，已收到您的消息。"}, self.admin)
        self.assertEqual(status, 303)
        self.assertIn("notice=sent", headers.get("Location", ""))
        sent = self.mock.sent_messages()
        self.assertTrue(any(item["text"] == "您好，已收到您的消息。" and item["target_id"] == "7305" for item in sent))
        # 失败可见：指向不可达端口
        os.environ["LANGBOT_API_BASE"] = "http://127.0.0.1:9"
        try:
            status, headers, _ = form_request(self.server, "POST", f"/admin/inbox/langbot/{conversation_id}/reply", {"body": "这条会失败。"}, self.admin)
            self.assertEqual(status, 303)
            self.assertIn("notice=reply_failed", headers.get("Location", ""))
            failed_row = self.last_message(conversation_id, "staff-reply")
            self.assertEqual(failed_row["delivery_status"], "failed")
            self.assertIn("网络请求失败", failed_row["delivery_error"])
            status, _, page = form_request(self.server, "GET", f"/admin/inbox/langbot/{conversation_id}", cookie=self.admin)
            self.assertIn("发送失败：网络请求失败", page)
            self.assertIn("重试发送", page)
        finally:
            os.environ["LANGBOT_API_BASE"] = self.mock.url
        # 重试成功
        status, headers, _ = form_request(self.server, "POST", f"/admin/inbox/langbot/{conversation_id}/messages/{failed_row['id']}/retry", {}, self.admin)
        self.assertEqual(status, 303)
        self.assertIn("notice=retried_ok", headers.get("Location", ""))
        refreshed = self.db.conn.execute("SELECT * FROM messages WHERE id=?", (failed_row["id"],)).fetchone()
        self.assertEqual(refreshed["delivery_status"], "sent")

    def test_missing_api_key_fails_closed(self):
        backup = os.environ.pop("LANGBOT_API_KEY", None)
        try:
            status, _body = self.post_event(self.event_for(7306, "hello", "ev-0006"))
            self.assertEqual(status, 200)
            conversation_id = self.conversation_for(7306)
            status, headers, _ = form_request(self.server, "POST", f"/admin/inbox/langbot/{conversation_id}/reply", {"body": "测试。"}, self.admin)
            self.assertEqual(status, 303)
            self.assertIn("notice=reply_failed", headers.get("Location", ""))
            failed_row = self.last_message(conversation_id, "staff-reply")
            self.assertEqual(failed_row["delivery_status"], "failed")
            self.assertIn("缺少环境变量 LANGBOT_API_KEY", failed_row["delivery_error"])
        finally:
            if backup is not None:
                os.environ["LANGBOT_API_KEY"] = backup

    def test_inbox_and_channel_page_show_platform_fields(self):
        """收件箱展示（含平台/用户ID）；渠道接入页展示 LangBot 卡片与收信地址。"""
        status, _body = self.post_event(self.event_for(7307, "hello", "ev-0007", adapter="telegram", name="Maya L"))
        self.assertEqual(status, 200)
        conversation_id = self.conversation_for(7307)
        # 列表页
        status, _, page = form_request(self.server, "GET", "/admin/inbox/langbot", cookie=self.admin)
        self.assertEqual(status, 200)
        self.assertIn("Maya L", page)
        # 详情页：平台 + 用户ID
        status, _, page = form_request(self.server, "GET", f"/admin/inbox/langbot/{conversation_id}", cookie=self.admin)
        self.assertEqual(status, 200)
        self.assertIn("平台 telegram", page)
        self.assertIn("用户ID 7307", page)
        # 渠道接入页：LangBot 卡片
        status, _, page = form_request(self.server, "GET", "/admin/channels", cookie=self.admin)
        self.assertEqual(status, 200)
        for text in ("LangBot", "bot_uuid", "LANGBOT_API_KEY", "host.docker.internal"):
            self.assertIn(text, page)
        self.assertIn(self.token, page)
        # 导航入口
        status, _, page = form_request(self.server, "GET", "/admin", cookie=self.admin)
        self.assertIn("/admin/inbox/langbot", page)

    def test_channel_save_generates_stable_token(self):
        """保存配置：首次生成 webhook_token，再次保存保持稳定。"""
        status, headers, _ = form_request(self.server, "POST", "/admin/channels/langbot", {"bot_uuid": "bot-9999"}, self.admin)
        self.assertEqual(status, 303)
        account = self.db.get_channel_account("demo-tenant", "langbot")
        config = json.loads(account["config_json"])
        self.assertEqual(config["bot_uuid"], "bot-9999")
        token_first = config["webhook_token"]
        status, _headers, _ = form_request(self.server, "POST", "/admin/channels/langbot", {"bot_uuid": "bot-9999"}, self.admin)
        account = self.db.get_channel_account("demo-tenant", "langbot")
        config = json.loads(account["config_json"])
        self.assertEqual(config["webhook_token"], token_first)
        # 复原供后续用例使用
        self.db.save_channel_account("demo-tenant", "langbot", {"bot_uuid": "bot-0001", "webhook_token": self.token}, "demo-admin@example.invalid")


if __name__ == "__main__":
    unittest.main()
