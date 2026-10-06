"""第二波 · Dify 问答引擎接线测试（LangBot 渠道优先走 Dify；平台仍是唯一主脑）。

契约：
- langbot 渠道配置了 DIFY_QA_APP_KEY 时，提问样文本先调 Dify（阻塞模式）；
- Dify 回答剥离 <think> 推理块；命中 [[NO_ANSWER]] 或空 → 按「资料外」转人工口径回复；
- Dify 不可用（网络/5xx）→ 静默回落本地资料问答，通道不中断；
- 未配置 Dify 时行为与旧版完全一致（本地问答）。
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
from test_langbot_bridge import _LangBotMock, login, json_http  # noqa: E402


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
    """LangBot 渠道资料问答：Dify 主路、哨兵转人工、不可用回落、未配置同旧版。"""

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
    def test_grounded_answer_via_dify(self):
        """有依据：Dify 回答（剥推理块）直达买方；引用落库；请求带正确密钥与用户标签。"""
        status, body = self.post_event(self.event_for(7401, "SLICE-T1 的流量范围是多少？", "ev-d1"))
        self.assertEqual(status, 200)
        self.assertEqual(body["outcome"], "answered")
        self.assertEqual(body["qa_source"], "dify")
        sent = self.sent_to(7401)
        self.assertTrue(sent)
        self.assertEqual(sent[-1]["text"], "SLICE-T1 的流量范围为 20-80 m³/h。")
        answer = self.last_answer(7401)
        self.assertEqual(answer["answer_status"], "grounded")
        citations = json.loads(answer["citations_json"])
        self.assertEqual(citations[0]["filename"], "SLICE-T1 · 测试泵")
        self.assertEqual(citations[0]["label"], "知识库")
        req = self.dify.requests[-1]
        self.assertEqual(req["headers"].get("Authorization"), "Bearer app-test-key")
        self.assertEqual(req["body"].get("user"), "langbot:7401")
        self.assertEqual(req["body"].get("response_mode"), "blocking")

    def test_no_answer_sentinel_hands_to_human(self):
        """哨兵：[[NO_ANSWER]] → 按资料外转人工口径回复 + needs_human 落库。"""
        status, body = self.post_event(self.event_for(7402, "无依据的天气问题？", "ev-d2"))
        self.assertEqual(status, 200)
        self.assertEqual(body["outcome"], "answer_unresolved")
        self.assertEqual(body["qa_source"], "dify")
        sent = self.sent_to(7402)
        self.assertTrue(any("现有核准资料没有足够依据" in item["text"] for item in sent))
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
