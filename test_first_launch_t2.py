"""首次上线 T2 最小阻断修复的失败回归（先于修复编写：修复前红、修复后绿）。

覆盖（对应本轮 T2 指令）：
1. 官网（买方 API）「只有主题知识」的企业 + 模型故障（timeout / 429 / invalid output）：
   必须走现有 503（service_unavailable）可重试路径、客户问题保留、不落假答案；
   同一幂等键在模型恢复后重试可继续生成，且不重复提问、不重复保存答案；
   模型调用失败只留一条日志（不重复日志）。
2. 渠道入口（LangBot webhook）同一故障：落入现有异常入口（500 processing_failed），
   不落假答案、不发送误导性回复；模型恢复后买家重新发送即可正常有据回答。
3. /admin/knowledge/qa 引用卡片：主题引用的「查看资料」指向真实主题详情页、灵活字段引用
   指向资料详情页，且链接 HTTP 可访问（200）。

隔离边界：只用临时数据库、临时上传目录与受控 stub；不读取 data/ 与任何密钥；
不触发真实模型调用；渠道出站一律 patch 为本地替身，不发送任何外部消息。
"""
from __future__ import annotations

import json
import os
import re
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import ai_client
from ai_client import AIConfig, AIUnavailableError

from app import Database, make_server
from test_langbot_bridge import form_request, json_http, login

TENANT = "demo-tenant"
QUESTION = "When is the response?"
PARAGRAPH = "Service inquiries are answered within two business days."


class StubKnowledgeAI:
    """AIService 边界受控替身：error / selection 可变（同一实例可先失败、后恢复）。"""

    def __init__(self, selection=None, error=None, on_call=None):
        self.mode = ai_client.MODE_DEEPSEEK
        self.config = AIConfig(mode=ai_client.MODE_DEEPSEEK, provider="stub", model="stub-model")
        self.selection = selection
        self.error = error
        self.on_call = on_call
        self.calls = []

    def select_knowledge_evidence(self, question, evidence):
        self.calls.append({"kind": "evidence", "question": question})
        if self.on_call is not None:
            self.on_call(self, evidence)
        if self.error is not None:
            raise self.error
        return self.selection, {"duration_ms": 7, "http_status": 200, "prompt_tokens": 9, "completion_tokens": 3}

    def select_answer_field(self, question, allowed):
        self.calls.append({"kind": "field", "question": question})
        return self.selection, {"duration_ms": 5, "http_status": 200, "prompt_tokens": 9, "completion_tokens": 3}


def choose_source(source_kind):
    """选择指定来源类型的证据（服务端键由送选清单分配）。"""

    def pick(ai, evidence):
        for item in evidence:
            if item["evidence"].get("source") == source_kind:
                ai.selection = item["key"]
                return
        ai.selection = None

    return pick


class T2Base(unittest.TestCase):
    maxDiff = None

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["B2B_KB_STORAGE_ROOT"] = str(Path(self.tmp.name) / "uploads")
        self.db = Database(Path(self.tmp.name) / "t2.db")
        self.db.seed_demo_users()
        self.admin = self.db.authenticate_user("demo-admin@example.invalid", "demo-admin")
        self.db.knowledge_storage_root = Path(self.tmp.name) / "uploads"

    def tearDown(self):
        self.db.conn.close()
        self.tmp.cleanup()
        os.environ.pop("B2B_KB_STORAGE_ROOT", None)

    # ── 夹具 ─────────────────────────────────────────────────────────────
    def publish_topic(self):
        """只有合格对外主题的企业（无任何已发布产品）。"""
        topic_id = self.db.kb2_create_topic(TENANT, self.admin["id"], "客服响应")
        self.db.kb2_save_topic_draft(
            TENANT, self.admin["id"], topic_id,
            paragraphs=[{"id": "p1", "text": PARAGRAPH, "origin": "manual", "checked": True,
                         "source": {"kind": "manual"}}],
            usage="external", ai_authorized=True)
        self.db.kb2_publish_topic_draft(TENANT, self.admin["id"], topic_id, confirm_ai=True)
        return topic_id

    def publish_item_with_field(self):
        """发布一条带灵活字段（已核对 + 允许客户引用 + 有来源）的资料。"""
        batch_id = self.db.kb2_create_batch(
            TENANT, self.admin["id"],
            [{"kind": "file", "filename": "service.txt", "data": "响应一小时内。\n支持定制。".encode()}])
        source = self.db.kb2_get_batch(TENANT, batch_id)["sources"][0]
        for row in self.db.conn.execute(
                "SELECT id FROM kb2_paragraphs WHERE tenant_id=? AND source_id=? ORDER BY ordinal",
                (TENANT, source["id"])).fetchall():
            self.db.kb2_update_paragraph(TENANT, self.admin["id"], row["id"], checked=True)
        item_id = source["item_id"]
        template_version = self.db.kb2_save_template(
            TENANT, self.admin["id"], None, [{"label": "支持定制", "type": "bool", "key": "custom"}],
            name="服务方案")
        self.db.kb2_adopt_template(TENANT, self.admin["id"], item_id, template_version, confirmed=True)
        self.db.kb2_set_item_field(TENANT, self.admin["id"], item_id, "custom", value=True,
                                   source={"kind": "manual"}, checked=True, allow_customer=True)
        result = self.db.kb2_publish_batch(TENANT, self.admin["id"], batch_id, usage="external",
                                           ai_authorized=True, confirm_ai=True)
        self.assertFalse(result["failed"], result["failed"])
        return item_id

    def ai_call_rows(self):
        return [dict(row) for row in self.db.conn.execute(
            "SELECT task, status FROM ai_call_log ORDER BY rowid").fetchall()]


class T2HttpBase(T2Base):
    def setUp(self):
        super().setUp()
        self.server = make_server(Path(self.tmp.name) / "http.db", port=0)
        self.server.RequestHandlerClass.db.conn.close()
        self.server.RequestHandlerClass.db = self.db
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.admin_cookie = login(self.server, "demo-admin@example.invalid", "demo-admin")

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        super().tearDown()

    def use_stub(self, stub):
        class StubModels:
            def __init__(self, ai):
                self.ai = ai

            def service(self, tenant):
                return self.ai

        self.server.RequestHandlerClass.models = StubModels(stub)


class T2WebsiteTopicFaultTests(T2HttpBase):
    """官网入口（买方 API）：主题知识企业 + 模型故障 → 503 / 输入保留 / 同键重试。"""

    def setUp(self):
        super().setUp()
        self.db.register_buyer_site(TENANT, "site-a", "https://seller-a.example")

    def new_conversation(self):
        status, created = json_http(self.server, "POST", "/api/v1/buyer/conversations",
                                    {"site_key": "site-a"})
        self.assertIn(status, (200, 201), created)
        headers = {"X-Site-Key": "site-a", "Authorization": f"Bearer {created['access_token']}"}
        return f"/api/v1/buyer/conversations/{created['conversation_id']}", headers

    def ask(self, path, headers, payload, key):
        return json_http(self.server, "POST", path + "/questions", payload,
                         {**headers, "Idempotency-Key": key})

    def conversation_view(self, path, headers):
        status, body = json_http(self.server, "GET", path, None, headers)
        self.assertEqual(status, 200, body)
        return body

    def test_topic_timeout_returns_503_preserves_input_and_same_key_retry_recovers(self):
        topic_id = self.publish_topic()
        stub = StubKnowledgeAI(error=AIUnavailableError(ai_client.STATUS_TIMEOUT, "timeout"))
        self.use_stub(stub)
        path, headers = self.new_conversation()
        payload = {"question": QUESTION}

        status, body = self.ask(path, headers, payload, "t2-q-timeout")
        view = self.conversation_view(path, headers)
        log_rows = self.ai_call_rows()
        facts = {"status": status, "body": body,
                 "message_contents": [item.get("content") for item in view["messages"]],
                 "answers": view["answers"], "human_tasks": view["human_tasks"], "ai_call_log": log_rows}
        self.assertEqual(status, 503,
                         "模型故障必须走现有 503 可重试路径（不得静默落「没有产品资料」假答案）："
                         + json.dumps(facts, ensure_ascii=False))
        self.assertEqual(body["error"], "service_unavailable")
        # 客户输入保留：问题已落库且不重复；不落假答案、不建人工任务。
        question_count = sum(1 for item in view["messages"] if item.get("content") == QUESTION)
        self.assertEqual(question_count, 1, "问题必须保留且只保留一条")
        self.assertEqual(view["answers"], [], "故障时不得保存任何答案")
        self.assertEqual(view["human_tasks"], [])
        self.assertEqual(log_rows, [{"task": "knowledge_evidence_selection", "status": "timeout"}],
                         "一次失败只记一条日志（不重复日志）")
        self.assertEqual(len(stub.calls), 1)

        # 同幂等键重试：模型恢复 → 复用已存问题继续生成，不重复提问、不重复落答案。
        stub.error = None
        stub.on_call = choose_source("topic")
        status, body = self.ask(path, headers, payload, "t2-q-timeout")
        self.assertEqual(status, 201, "同键重试（模型恢复）应继续并成功："
                         + json.dumps(body or {}, ensure_ascii=False))
        self.assertEqual(body["status"], "grounded")
        self.assertEqual(body["answer"].strip(), PARAGRAPH)
        self.assertEqual(body["citations"][0]["kind"], "topic")
        self.assertEqual(body["citations"][0]["topic_id"], topic_id)
        self.assertEqual(len(stub.calls), 2, "同键重试必须再次调用模型（不再命中假答案）")
        view = self.conversation_view(path, headers)
        question_count = sum(1 for item in view["messages"] if item.get("content") == QUESTION)
        self.assertEqual(question_count, 1, "同键重试不得重复提问")
        self.assertEqual(len(view["answers"]), 1, "同键重试只保存一份答案")

        # 再次同键重试：去重返回已存答案，不重复生成。
        status, body = self.ask(path, headers, payload, "t2-q-timeout")
        self.assertEqual(status, 200)
        self.assertTrue(body.get("duplicate"))
        self.assertEqual(len(stub.calls), 2, "已成功答案的同键重试不再调用模型")
        view = self.conversation_view(path, headers)
        self.assertEqual(len(view["answers"]), 1)

    def test_rate_limited_and_invalid_output_also_return_503_without_fake_answer(self):
        self.publish_topic()
        for index, kind in enumerate((ai_client.STATUS_RATE_LIMITED, ai_client.STATUS_INVALID_OUTPUT)):
            with self.subTest(kind=kind):
                self.use_stub(StubKnowledgeAI(error=AIUnavailableError(kind, kind)))
                path, headers = self.new_conversation()
                status, body = self.ask(path, headers, {"question": QUESTION}, f"t2-q-{index}-{kind}")
                self.assertEqual(status, 503, f"{kind}: " + json.dumps(body or {}, ensure_ascii=False))
                self.assertEqual(body["error"], "service_unavailable")
                view = self.conversation_view(path, headers)
                self.assertEqual(view["answers"], [], f"{kind}: 不得落假答案")
                question_count = sum(1 for item in view["messages"] if item.get("content") == QUESTION)
                self.assertEqual(question_count, 1, f"{kind}: 问题保留且不重复")
                self.assertEqual(self.db.latest_ai_call(TENANT)["status"], kind)


class T2ChannelFaultEntryTests(T2HttpBase):
    """渠道入口（LangBot webhook）：同一故障落入现有异常入口，不静默成假答复。"""

    def setUp(self):
        super().setUp()
        self.publish_topic()
        self.account_id = self.db.create_business_connection(
            TENANT, "langbot", "Telegram", "T2 业务号", self.admin["id"], self.admin["email"])
        self.db.set_connection_ai(TENANT, self.account_id, True, self.admin["id"], confirmed=True)
        account = self.db.get_business_connection(TENANT, self.account_id)
        self.token = json.loads(account["config_json"])["webhook_token"]

    def event(self, uuid, text):
        return {"uuid": uuid, "event_type": "bot.person_message",
                "data": {"adapter_name": "TelegramAdapter", "sender": {"id": "cust-t2", "name": "客户"},
                         "message": [{"type": "Plain", "text": text}], "timestamp": 1759500000}}

    def post_event(self, event):
        with patch("app.dispatch_outbound", return_value=(True, "")):
            return json_http(self.server, "POST", f"/api/v1/channels/langbot/webhook/{self.token}", event)

    def test_model_fault_goes_to_existing_error_entry_and_retry_recovers(self):
        stub = StubKnowledgeAI(error=AIUnavailableError(ai_client.STATUS_TIMEOUT, "timeout"))
        self.use_stub(stub)

        status, body = self.post_event(self.event("t2-ev-1", QUESTION))
        rows = [dict(row) for row in self.db.conn.execute(
            "SELECT role, content FROM messages WHERE tenant_id=? ORDER BY rowid", (TENANT,)).fetchall()]
        answer_count = self.db.conn.execute("SELECT COUNT(*) AS n FROM answers").fetchone()["n"]
        log_rows = self.ai_call_rows()
        facts = {"status": status, "body": body, "messages": rows, "answers": answer_count,
                 "ai_call_log": log_rows}
        self.assertEqual(status, 500,
                         "渠道现有异常入口必须收到该故障（不得静默成「没有产品资料」假答复）："
                         + json.dumps(facts, ensure_ascii=False))
        self.assertEqual(body["error"], "processing_failed")
        self.assertEqual(rows, [{"role": "buyer", "content": QUESTION}],
                         "客户消息保留、且没有误导性回复")
        self.assertEqual(answer_count, 0, "不得落假答案")
        self.assertEqual(log_rows, [{"task": "knowledge_evidence_selection", "status": "timeout"}],
                         "一次失败只记一条日志（不重复日志）")

        # 模型恢复后，买家重新发送（新消息）→ 正常有据回答。
        stub.error = None
        stub.on_call = choose_source("topic")
        status, body = self.post_event(self.event("t2-ev-2", QUESTION))
        self.assertEqual(status, 200, body)
        self.assertEqual(body.get("outcome"), "answered")
        self.assertEqual(body.get("qa_source"), "knowledge")
        answer = self.db.conn.execute(
            "SELECT answer_status, answer_text FROM answers WHERE tenant_id=? ORDER BY rowid DESC LIMIT 1",
            (TENANT,)).fetchone()
        self.assertEqual(answer["answer_status"], "grounded")
        self.assertEqual(str(answer["answer_text"]).strip(), PARAGRAPH)


class T2QACitationLinkTests(T2HttpBase):
    """测试页引用卡片：主题 / 灵活字段的「查看资料」指向真实详情页且 HTTP 可访问。"""

    def ask(self, question):
        status, _, page = form_request(self.server, "POST", "/admin/knowledge/qa",
                                       {"question": question}, self.admin_cookie)
        self.assertEqual(status, 200)
        return page

    def citation_card(self, page):
        start = page.find("<div class='kb-cite'>")
        self.assertNotEqual(start, -1, "回答卡必须包含引用卡")
        end = page.find("</div>", start)
        return page[start:end + len("</div>")]

    def test_topic_citation_links_to_topic_detail_page_and_is_reachable(self):
        topic_id = self.publish_topic()
        self.use_stub(StubKnowledgeAI(on_call=choose_source("topic")))
        page = self.ask(QUESTION)
        card = self.citation_card(page)
        match = re.search(r"href='(/admin/knowledge/topic/([a-f0-9]{8,64}))'", card)
        self.assertIsNotNone(
            match, "主题引用的「查看资料」必须指向主题详情页（不得再是 /admin/knowledge/item/）：" + card)
        self.assertEqual(match.group(2), topic_id)
        self.assertIn("主题段落", card, "主题引用的来源标签必须标明主题：" + card)
        self.assertNotIn("知识条目", card)
        status, _, detail = form_request(self.server, "GET", match.group(1), None, self.admin_cookie)
        self.assertEqual(status, 200, "主题详情链接必须 HTTP 可访问")
        self.assertIn("客服响应", detail)

    def test_item_field_citation_links_to_item_detail_page_and_is_reachable(self):
        item_id = self.publish_item_with_field()
        self.use_stub(StubKnowledgeAI(on_call=choose_source("item_field")))
        page = self.ask("支持定制吗？")
        card = self.citation_card(page)
        self.assertIn("资料字段", card, "灵活字段引用的来源标签应为「资料字段」：" + card)
        self.assertNotIn("知识条目", card)
        match = re.search(r"href='(/admin/knowledge/item/([a-f0-9]{8,64}))'", card)
        self.assertIsNotNone(match, "灵活字段引用必须给出资料详情链接：" + card)
        self.assertEqual(match.group(2), item_id)
        status, _, detail = form_request(self.server, "GET", match.group(1), None, self.admin_cookie)
        self.assertEqual(status, 200, "资料详情链接必须 HTTP 可访问")


if __name__ == "__main__":
    unittest.main()
