"""远程问答授权边界回归：共享 Dify 未具备检索前隔离时使用本地获准知识。

保留回复解析的单元检查；渠道和网站不得将问题发送给未经授权约束的共享应用。
"""
import json
import os
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from app import _dify_retriever_resources, make_server, strip_reasoning_blocks  # noqa: E402
from test_langbot_bridge import _LangBotMock, form_request, json_http, login  # noqa: E402


class _DifyQAMock:
    """最小 Dify 问答服务模拟：按问题内容返回既定答复，记录调用。"""

    def __init__(self, app_key="app-test-key"):
        state = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length).decode("utf-8")
                body = json.loads(raw or "{}")
                state.requests.append({"path": self.path, "headers": dict(self.headers), "body": body})
                query = str(body.get("query") or "")
                auth_ok = self.headers.get("Authorization") == f"Bearer {app_key}"
                if "崩溃" in query:
                    payload, status = {"message": "boom"}, 500
                elif not auth_ok:
                    payload, status = {"message": "unauthorized"}, 401
                elif "无依据" in query:
                    payload, status = {
                        "answer": "<think>\n<!--dify-deepseek-reasoning-->no basis\n</think>[[NO_ANSWER]]",
                        "conversation_id": "c-nb",
                        "retriever_resources": [],
                    }, 200
                else:
                    payload, status = {
                        "answer": "<think>\n<!--dify-deepseek-reasoning-->reasoning here\n</think>SLICE-T1 的流量范围为 20-80 m³/h。",
                        "conversation_id": "c-1",
                        "retriever_resources": [{
                            "position": 1,
                            "dataset_id": "ds-1",
                            "dataset_name": "Alpsaga 已核准产品资料",
                            "document_id": "doc-1",
                            "document_name": "SLICE-T1 · 测试泵",
                            "segment_id": "seg-1",
                        }],
                    }, 200
                data = json.dumps(payload).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.requests = []
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self._server.server_address[1]}"
        threading.Thread(target=self._server.serve_forever, daemon=True).start()

    def stop(self):
        self._server.shutdown()
        self._server.server_close()


class DifyQALangBotTests(unittest.TestCase):
    """渠道和网站问答：未经隔离的远程应用不可用于对外生成。"""

    maxDiff = None

    @classmethod
    def setUpClass(cls):
        cls.temp_dir = tempfile.TemporaryDirectory()
        cls.db_path = Path(cls.temp_dir.name) / "app.db"
        cls.langbot = _LangBotMock()
        cls.dify = _DifyQAMock()
        cls.env_backup = {name: os.environ.get(name) for name in ("LANGBOT_API_KEY", "LANGBOT_API_BASE", "DIFY_QA_URL", "DIFY_QA_APP_KEY")}
        os.environ["LANGBOT_API_KEY"] = "lbk_test_key"
        os.environ["LANGBOT_API_BASE"] = cls.langbot.url
        os.environ["DIFY_QA_URL"] = cls.dify.url + "/v1"
        os.environ["DIFY_QA_APP_KEY"] = "app-test-key"
        cls.server = make_server(cls.db_path, port=0)
        cls.db = cls.server.RequestHandlerClass.db
        cls.db.seed_demo_users()
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.token = "tok_dify_qa_test_0001_abcdef"
        cls.db.save_channel_account("demo-tenant", "langbot", {"bot_uuid": "bot-0001", "webhook_token": cls.token}, "demo-admin@example.invalid")
        cls.db.set_channel_enabled("demo-tenant", "langbot", True, "demo-admin@example.invalid")
        cls.admin = login(cls.server, "demo-admin@example.invalid", "demo-admin")

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.langbot.stop()
        cls.dify.stop()
        cls.temp_dir.cleanup()
        for name, value in cls.env_backup.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value

    # ── 辅助 ──────────────────────────────────────────────────────────────
    def event_for(self, sender_id, text, uuid):
        return {
            "uuid": uuid,
            "event_type": "bot.person_message",
            "data": {
                "bot_uuid": "bot-0001",
                "adapter_name": "TelegramAdapter",
                "sender": {"id": sender_id, "name": "Ivan"},
                "message": [{"type": "Plain", "text": text}],
                "timestamp": 1759500000,
            },
        }

    def post_event(self, event):
        return json_http(self.server, "POST", f"/api/v1/channels/langbot/webhook/{self.token}", event)

    def last_answer(self, sender_id):
        return self.db.conn.execute(
            "SELECT a.* FROM answers a JOIN channel_identities i ON i.conversation_id = a.conversation_id "
            "WHERE i.tenant_id='demo-tenant' AND i.channel='langbot' AND i.external_user_id=? ORDER BY a.rowid DESC LIMIT 1",
            (str(sender_id),),
        ).fetchone()

    def sent_to(self, sender_id):
        return [item for item in self.langbot.sent_messages() if item["target_id"] == str(sender_id)]

    # ── 用例 ──────────────────────────────────────────────────────────────
    def test_unrestricted_remote_retrieval_is_not_used(self):
        """共享应用未提供检索前授权，即使配置密钥也不得发送企业问题。"""
        before = len(self.dify.requests)
        status, body = self.post_event(self.event_for(7401, "SLICE-T1 的流量范围是多少？", "ev-d1"))
        self.assertEqual(status, 200)
        self.assertEqual(body["qa_source"], "local")
        self.assertEqual(len(self.dify.requests), before)
        self.assertTrue(self.sent_to(7401))

    def test_no_answer_sentinel_hands_to_human(self):
        """哨兵：[[NO_ANSWER]] → 按资料外转人工口径回复 + needs_human 落库。"""
        status, body = self.post_event(self.event_for(7402, "无依据的天气问题？", "ev-d2"))
        self.assertEqual(status, 200)
        self.assertEqual(body["outcome"], "answer_unresolved")
        self.assertEqual(body["qa_source"], "local")
        sent = self.sent_to(7402)
        self.assertTrue(any("没有可引用的已发布产品资料" in item["text"] for item in sent))
        self.assertTrue(any("「人工」" in item["text"] for item in sent))
        answer = self.last_answer(7402)
        self.assertEqual(answer["answer_status"], "needs_human")

    def test_dify_down_falls_back_to_local(self):
        """Dify 5xx：静默回落本地问答，通道不中断。"""
        status, body = self.post_event(self.event_for(7403, "崩溃场景的问题？", "ev-d3"))
        self.assertEqual(status, 200)
        self.assertEqual(body["qa_source"], "local")
        self.assertEqual(body["outcome"], "answer_unresolved")  # 本地：无已发布绑定 → 转人工提示
        sent = self.sent_to(7403)
        self.assertTrue(any("「人工」" in item["text"] for item in sent))

    def test_qa_disabled_uses_local_path(self):
        """未配置 DIFY_QA_APP_KEY：与旧版完全一致（本地问答）。"""
        saved = os.environ.pop("DIFY_QA_APP_KEY", None)
        try:
            status, body = self.post_event(self.event_for(7404, "还有个问题？", "ev-d4"))
            self.assertEqual(status, 200)
            self.assertEqual(body["qa_source"], "local")
        finally:
            if saved is not None:
                os.environ["DIFY_QA_APP_KEY"] = saved

    def test_retriever_resources_shapes_unit(self):
        """引用提取：顶层 / metadata 嵌套 / 脏数据 三种形状。"""
        top = {"retriever_resources": [{"position": 1}, "bad", None]}
        nested = {"metadata": {"retriever_resources": [{"position": 2}]}}
        self.assertEqual(_dify_retriever_resources(top), [{"position": 1}])
        self.assertEqual(_dify_retriever_resources(nested)[0]["position"], 2)
        self.assertEqual(_dify_retriever_resources({}), [])
        self.assertEqual(_dify_retriever_resources({"retriever_resources": None}), [])

    def test_web_inquiry_blocks_unrestricted_dify(self):
        """网页入口也限制共享远程检索，缺少本地依据时进入核实路径。"""
        status, headers, _ = form_request(self.server, "GET", "/inquiry")
        self.assertEqual(status, 303)
        path = headers["Location"].split("?", 1)[0]
        cookie = headers["Set-Cookie"].split(";", 1)[0]
        conversation_id = path.rsplit("/", 1)[1]
        status, _headers, _ = form_request(
            self.server, "POST", path + "/ask",
            {"question": "SLICE-T1 的流量范围是多少？", "submission_key": "sk-web-01"}, cookie=cookie,
        )
        self.assertEqual(status, 303)
        row = self.db.conn.execute(
            "SELECT * FROM answers WHERE tenant_id='demo-tenant' AND conversation_id=? ORDER BY rowid DESC LIMIT 1",
            (conversation_id,),
        ).fetchone()
        self.assertEqual(row["answer_status"], "needs_human")
        self.assertIn("没有可引用的已发布产品资料", row["answer_text"])
        self.assertEqual(json.loads(row["citations_json"]), [])
        self.assertFalse(self.dify.requests)
        # 哨兵 → 转人工口径（与渠道一致）
        status, _headers, _ = form_request(
            self.server, "POST", path + "/ask",
            {"question": "无依据的问题？", "submission_key": "sk-web-02"}, cookie=cookie,
        )
        self.assertEqual(status, 303)
        row2 = self.db.conn.execute(
            "SELECT * FROM answers WHERE tenant_id='demo-tenant' AND conversation_id=? ORDER BY rowid DESC LIMIT 1",
            (conversation_id,),
        ).fetchone()
        self.assertEqual(row2["answer_status"], "needs_human")
        self.assertIn("没有可引用的已发布产品资料", row2["answer_text"])

    def test_strip_reasoning_blocks_unit(self):
        cases = [
            ("<think>x</think>答案", "答案"),
            ("<THINK>x</THINK>答案", "答案"),
            ("答案中有 <think>中间块</think> 注释", "答案中有  注释"),
            ("<!--dify-deepseek-reasoning-->答案", "答案"),
            ("<think>未闭合", "未闭合"),
            ("", ""),
        ]
        for raw, expected in cases:
            self.assertEqual(strip_reasoning_blocks(raw), expected, raw)


if __name__ == "__main__":
    unittest.main()
