"""企业知识第一批 · 任务4：客户视角真实问答（本地 stub 模型；不联网、不叫真实模型）。

边界与声明（对应本轮指令）：
- 模型调用一律经现有 AIService 边界；本文件用注入 stub 验证“检索→约束选择→服务端精确引用→
  版本引用→生成后复核”的链路本身。stub/厂商模拟不得当作真实模型验收——真实模型接通后另测。
- 模型选择之前校验：企业隔离、版本、权限、到期、逐字段核准与出处；
  模型响应之后再次校验（防并发停用 / 撤权 / 到期 / 换版 / 字段变更），失效则按资料不足。
- 回答文本与引用完全由服务端从存储内容生成（精确原文 + 真实版本）；模型只选择证据键。
- 资料不足提示建议转人工，但绝不自动创建人工任务（沿用买方主动申请协助规则）。
- 规则模式明确报告“模型未连接”，不生成任何模拟答复。
- 网页与已开启渠道共用同一过滤器与问答阶段（不能只在测试页生效）。
只用临时数据库与临时上传目录；不触碰 data/ 与任何密钥。
"""
import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import ai_client
from ai_client import AIConfig, AIUnavailableError
from app import Database, answer_knowledge_question, answer_question_prefer_dify

TENANT = "demo-tenant"
OTHER = "other-tenant"


class StubKnowledgeAI:
    """AIService 边界的受控替身：记录送入模型的证据，按脚本返回选择。"""

    def __init__(self, selection=None, error=None, on_call=None):
        self.mode = ai_client.MODE_DEEPSEEK
        self.config = AIConfig(mode=ai_client.MODE_DEEPSEEK, provider="stub", model="stub-model")
        self.selection = selection
        self.error = error
        self.on_call = on_call
        self.calls = []

    def select_knowledge_evidence(self, question, evidence):
        self.calls.append({"question": question, "evidence": [dict(item) for item in evidence]})
        if self.on_call is not None:
            self.on_call(self, evidence)
        if self.error is not None:
            raise self.error
        return self.selection, {"duration_ms": 7, "http_status": 200, "prompt_tokens": 9, "completion_tokens": 3}

    def select_answer_field(self, question, allowed):
        self.calls.append({"question": question, "allowed": list(allowed)})
        return self.selection, {"duration_ms": 5, "http_status": 200, "prompt_tokens": 9, "completion_tokens": 3}


def choose_first(ai, evidence):
    if evidence:
        ai.selection = evidence[0]["key"]


class Task4Base(unittest.TestCase):
    maxDiff = None

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["B2B_KB_STORAGE_ROOT"] = str(Path(self.tmp.name) / "uploads")
        self.db = Database(Path(self.tmp.name) / "knowledge.db")
        self.db.seed_demo_users()
        self.admin = self.db.authenticate_user("demo-admin@example.invalid", "demo-admin")
        self.member = self.db.authenticate_user("demo-sales@example.invalid", "demo-sales")
        self.db.knowledge_storage_root = Path(self.tmp.name) / "uploads"
        self.db.ensure_tenant(OTHER, "其他企业（测试）")
        self.other_admin_id = self.db.create_user(
            OTHER, "other-admin@example.invalid", "对方管理员", "admin", "other-admin", "test-setup")

    def tearDown(self):
        self.db.conn.close()
        self.tmp.cleanup()
        os.environ.pop("B2B_KB_STORAGE_ROOT", None)

    # ── 帮助 ─────────────────────────────────────────────────────────────
    def publish_prepared(self, title="发货与物流常见问题", body="发货：3-5 天到货。",
                         *, usage="external", ai_authorized=True, **kw):
        item_id = self.db.create_knowledge_item(
            TENANT, "article", title, self.admin["id"], content={"body": body},
            usage=usage, ai_authorized=ai_authorized, **kw)
        self.db.publish_knowledge_draft(TENANT, item_id, self.admin["id"], confirm_ai=True)
        version = self.db.get_published_knowledge_version(TENANT, item_id)
        self.db.prepare_knowledge_version(TENANT, version["id"])
        return item_id, version

    def publish_product(self, model="DS-50"):
        product_id = self.db.create_product(TENANT, model, "测试泵", "冷却水循环")
        source_id = self.db.add_source(TENANT, product_id, f"{model}-manual.txt", "TXT", "第 2 页：规格参数表")
        for key, value, unit in (("flow_range", "20-60", "m³/h"), ("head_range", "25-45", "m")):
            self.db.save_field(TENANT, product_id, key, value, unit, source_id, "第 2 页：规格参数表")
            self.db.approve_field(TENANT, product_id, key)
        self.db.finalize_product_review(TENANT, product_id)
        self.db.publish_product(TENANT, product_id)
        # 清单11：产品需逐项 AI 客服授权才进入客户问答；测试夹具保持「发布即可用」的原行为。
        self.db.set_knowledge_product_authorization(
            TENANT, product_id, self.admin["id"], authorized=True, confirmed=True)
        return product_id, source_id

    def corpus(self):
        return self.db.list_customer_qa_evidence(TENANT)


class EvidenceGatingTests(Task4Base):
    """模型之前的本地校验：企业隔离、版本、权限、到期、逐字段核准与出处。"""

    def test_corpus_only_includes_fully_eligible_items(self):
        good_id, good_version = self.publish_prepared(title="合格文章", body="合格正文：可被引用。")
        internal_id, _ = self.publish_prepared(title="内部文章", body="内部正文：不可引用。", usage="internal")
        unauthorized_id, _ = self.publish_prepared(title="未授权文章", body="未授权正文。", ai_authorized=False)
        disabled_id, _ = self.publish_prepared(title="停用文章", body="停用正文。")
        self.db.disable_knowledge_item(TENANT, disabled_id, self.admin["id"], confirmed=True)
        expired_id, _ = self.publish_prepared(title="过期文章", body="过期正文。", expires_at="2020-01-01")
        pending_id = self.db.create_knowledge_item(
            TENANT, "article", "未准备文章", self.admin["id"], content={"body": "未准备正文。"},
            usage="external", ai_authorized=True)
        self.db.publish_knowledge_draft(TENANT, pending_id, self.admin["id"], confirm_ai=True)  # 未执行 prepare
        other_item = self.db.create_knowledge_item(
            OTHER, "article", "对方文章", self.other_admin_id, content={"body": "对方正文。"})
        self.db.publish_knowledge_draft(OTHER, other_item, self.other_admin_id, confirm_ai=True)
        other_version = self.db.get_published_knowledge_version(OTHER, other_item)
        self.db.prepare_knowledge_version(OTHER, other_version["id"])

        item_ids = {unit["item_id"] for unit in self.corpus() if unit["source"] == "knowledge"}
        self.assertEqual(item_ids, {good_id})
        texts = [unit["text"] for unit in self.corpus()]
        for leaked in ("内部正文", "未授权正文", "停用正文", "过期正文", "未准备正文", "对方正文"):
            self.assertFalse(any(leaked in text for text in texts), f"{leaked} 不得进入候选证据")
        # 停用/撤权/换版后有真实时间性：候选基于当前库状态即时计算。
        self.db.disable_knowledge_item(TENANT, good_id, self.admin["id"], confirmed=True)
        self.assertEqual([u for u in self.corpus() if u["item_id"] == good_id], [])

    def test_product_facts_field_level_approval_and_source(self):
        product_id, source_id = self.publish_product()
        # 新增未核准字段与禁止使用的来源字段：都不得进入候选。
        self.db.save_field(TENANT, product_id, "material", "铸铁", "", source_id, "第 2 页：规格参数表")
        facts = [unit for unit in self.corpus() if unit["source"] == "product_field"]
        self.assertEqual({unit["field_key"] for unit in facts}, {"flow_range", "head_range"})
        self.db.set_source_usage(TENANT, product_id, source_id, "restricted", "demo-admin")
        self.assertEqual([unit for unit in self.corpus() if unit["source"] == "product_field"], [])


class PipelineTests(Task4Base):
    """检索 → 约束选择 → 服务端引用 → 生成后复核。"""

    def test_grounded_uses_exact_server_quote_and_true_version(self):
        item_id, version = self.publish_prepared()
        stub = StubKnowledgeAI(on_call=choose_first)
        outcome = answer_knowledge_question(self.db, stub, TENANT, "", "发货要几天？")
        self.assertEqual(outcome["status"], "grounded")
        chunk = self.db.list_knowledge_chunks(TENANT, version["id"])[0]
        self.assertEqual(outcome["answer"], chunk["text"], "回答必须是存储原文的精确引用")
        citation = outcome["citations"][0]
        self.assertEqual(citation["kind"], "knowledge")
        self.assertEqual(citation["version"], "v1")
        self.assertEqual(citation["version_id"], version["id"])
        self.assertEqual(citation["item_id"], item_id)
        self.assertEqual(citation["quote"], chunk["text"])
        self.assertNotIn("storage_path", json.dumps(citation))
        # 送入模型的证据带服务端键；模型只做选择，不含答案生成。
        sent = stub.calls[0]["evidence"]
        self.assertEqual(len(sent), 1)
        self.assertTrue(sent[0]["key"])
        ai_rows = [dict(row) for row in self.db.conn.execute("SELECT * FROM ai_call_log ORDER BY rowid")]
        self.assertEqual((ai_rows[0]["task"], ai_rows[0]["status"]), ("knowledge_evidence_selection", "ok"))

    def test_model_none_or_unknown_key_is_no_evidence_handoff(self):
        self.publish_prepared()
        for selection in (None, "e999"):
            stub = StubKnowledgeAI(selection=selection)
            outcome = answer_knowledge_question(self.db, stub, TENANT, "", "保修多久？")
            self.assertEqual(outcome["status"], "no_evidence", selection)
            self.assertIn("转人工", outcome["answer"])
            self.assertEqual(outcome["citations"], [])
        self.assertEqual(self.db.list_human_tasks(TENANT), [], "资料不足不得自动创建人工任务")

    def test_no_candidates_no_model_call(self):
        stub = StubKnowledgeAI(selection="e1")
        outcome = answer_knowledge_question(self.db, stub, TENANT, "", "发货要几天？")
        self.assertEqual(outcome["status"], "no_evidence")
        self.assertEqual(stub.calls, [], "没有候选证据时不得调用模型")
        self.assertEqual(outcome["candidate_count"], 0)
        self.assertIn("转人工", outcome["answer"])

    def test_internal_and_ineligible_items_never_sent_to_model(self):
        self.publish_prepared(title="合格文章", body="合格正文：可被引用。")
        self.publish_prepared(title="内部文章", body="内部绝密正文。", usage="internal")
        stub = StubKnowledgeAI(on_call=choose_first)
        answer_knowledge_question(self.db, stub, TENANT, "", "问题")
        payload = json.dumps(stub.calls[0]["evidence"], ensure_ascii=False)
        self.assertIn("合格正文", payload)
        self.assertNotIn("内部绝密正文", payload, "内部资料不得作为候选证据送入模型")

    def test_after_model_revalidation_blocks_concurrent_revocation(self):
        item_id, version = self.publish_prepared()

        def revoke(ai, evidence):
            ai.selection = evidence[0]["key"]
            self.db.set_knowledge_authorization(TENANT, item_id, self.admin["id"], False)

        stub = StubKnowledgeAI(on_call=revoke)
        outcome = answer_knowledge_question(self.db, stub, TENANT, "", "发货要几天？")
        self.assertEqual(outcome["status"], "no_evidence")
        self.assertIn("失效", outcome["reason"])
        self.assertEqual(outcome["citations"], [])
        # 停用同口径。
        item_id2, _ = self.publish_prepared(title="第二篇", body="第二篇正文。")
        stub2 = StubKnowledgeAI(on_call=lambda ai, ev: (setattr(ai, "selection", ev[0]["key"]),
                                                        self.db.disable_knowledge_item(TENANT, item_id2, self.admin["id"], confirmed=True)))
        outcome2 = answer_knowledge_question(self.db, stub2, TENANT, "", "问题")
        self.assertEqual(outcome2["status"], "no_evidence")

    def test_after_model_revalidation_blocks_product_field_change(self):
        product_id, _ = self.publish_product()

        def revoke_field(ai, evidence):
            ai.selection = evidence[0]["key"]
            self.db.set_source_usage(TENANT, product_id, self.db.get_fields(TENANT, product_id)[0]["source_document_id"], "restricted", "demo-admin")

        stub = StubKnowledgeAI(on_call=revoke_field)
        outcome = answer_knowledge_question(self.db, stub, TENANT, "", "流量范围是多少？")
        self.assertEqual(outcome["status"], "no_evidence")
        self.assertIn("失效", outcome["reason"])

    def test_model_output_invalid_shape_is_error_not_fake_answer(self):
        self.publish_prepared()
        stub = StubKnowledgeAI(error=AIUnavailableError(ai_client.STATUS_RATE_LIMITED, "rate limited"))
        outcome = answer_knowledge_question(self.db, stub, TENANT, "", "发货要几天？")
        self.assertEqual(outcome["status"], "error")
        self.assertEqual(outcome["error_kind"], ai_client.STATUS_RATE_LIMITED)
        self.assertEqual(outcome["answer"], "")
        self.assertEqual(self.db.list_human_tasks(TENANT), [])

    def test_rules_mode_reports_model_not_connected(self):
        self.publish_prepared()
        rules_service = ai_client.AIService(AIConfig(mode=ai_client.MODE_RULES))
        for ai in (None, rules_service):
            outcome = answer_knowledge_question(self.db, ai, TENANT, "", "发货要几天？")
            self.assertEqual(outcome["status"], "model_not_connected")
            self.assertEqual(outcome["answer"], "")
            self.assertIn("模型未连接", outcome["reason"])
        # 规则模式没有任何模型调用日志。
        self.assertEqual(self.db.conn.execute("SELECT COUNT(*) FROM ai_call_log").fetchone()[0], 0)


class SharedGatingTests(Task4Base):
    """网页与已开启渠道共用同一过滤器与问答阶段；既有工程字段问题不被劫持。"""

    def test_prefer_dify_extends_website_with_knowledge(self):
        self.publish_prepared()
        stub = StubKnowledgeAI(on_call=choose_first)
        status, answer, product_id, version_id, citations, reason, source = answer_question_prefer_dify(
            self.db, stub, TENANT, "", "发货要几天？", None, "web:test")
        self.assertEqual(status, "grounded")
        self.assertEqual(source, "knowledge")
        self.assertEqual(answer, self.db.list_knowledge_chunks(TENANT, citations[0]["version_id"])[0]["text"])
        self.assertEqual(citations[0]["version"], "v1")
        self.assertIsNone(version_id, "知识版本不写入 answers.product_version_id 外键列")
        self.assertEqual(self.db.list_human_tasks(TENANT), [])

    def test_prefer_dify_rules_mode_unchanged(self):
        self.publish_prepared()
        status, answer, *_rest = answer_question_prefer_dify(self.db, None, TENANT, "", "What is the flow range?", None, "web:test")
        self.assertEqual(status, "needs_human")
        self.assertTrue(answer)
        self.assertEqual(self.db.list_human_tasks(TENANT), [])

    def test_existing_engineering_field_question_not_hijacked(self):
        product_id, _ = self.publish_product()
        self.publish_prepared(title="知识文章", body="知识文章正文。")
        stub = StubKnowledgeAI(selection="flow_range")
        status, answer, answered_product_id, version_id, citations, reason, source = answer_question_prefer_dify(
            self.db, stub, TENANT, "", "What is the flow range?", product_id, "web:test")
        self.assertEqual(status, "grounded")
        self.assertEqual(source, "local", "已发布字段问题仍由既有字段问答回答")
        self.assertIn("20-60", answer)
        self.assertEqual(len(stub.calls), 1, "第一阶段已答出：不得再进入知识阶段")

    def test_needs_human_without_knowledge_items_keeps_single_stage(self):
        product_id, _ = self.publish_product()
        stub = StubKnowledgeAI(selection=None)
        status, answer, *_rest = answer_question_prefer_dify(self.db, stub, TENANT, "", "What is the flow range?", product_id, "web:test")
        self.assertEqual(status, "needs_human")
        self.assertEqual(len(stub.calls), 1, "没有文章/FAQ 候选时不做多余的模型调用")

    def test_channel_ai_off_never_reaches_knowledge_stage(self):
        from app import process_channel_message
        self.publish_prepared()
        account_id = self.db.create_business_connection(TENANT, "langbot", "Telegram", "业务号", self.admin["id"], self.admin["email"])
        stub = StubKnowledgeAI(on_call=choose_first)
        result = process_channel_message(self.db, stub, TENANT, "langbot", "cust-1", "客户", "发货要几天？", "m-1", account_id)
        self.assertEqual(result["action"], "ai_disabled", "渠道默认 AI 关闭：不进入问答阶段")
        self.assertEqual(stub.calls, [])

    def test_enabled_channel_uses_shared_knowledge_stage(self):
        from app import process_channel_message
        self.publish_prepared()
        account_id = self.db.create_business_connection(TENANT, "langbot", "Telegram", "业务号", self.admin["id"], self.admin["email"])
        self.db.set_connection_ai(TENANT, account_id, True, self.admin["id"], confirmed=True)
        stub = StubKnowledgeAI(on_call=choose_first)
        with patch("app.dispatch_outbound", return_value=(True, "")):
            result = process_channel_message(self.db, stub, TENANT, "langbot", "cust-1", "客户", "发货要几天？", "m-1", account_id)
        self.assertEqual(result["action"], "answered")
        self.assertEqual(result["qa_source"], "knowledge")
        row = self.db.conn.execute(
            "SELECT * FROM answers WHERE tenant_id=? ORDER BY rowid DESC LIMIT 1", (TENANT,)).fetchone()
        self.assertEqual(row["answer_status"], "grounded")
        citations = json.loads(row["citations_json"])
        self.assertEqual(citations[0]["version"], "v1")
        self.assertEqual(self.db.list_human_tasks(TENANT), [])


class QATestPageTests(Task4Base):
    """测试页：真实状态、明确错误、精确版本引用查看器；规则模式明确“模型未连接”。"""

    def setUp(self):
        super().setUp()
        from app import make_server
        from test_langbot_bridge import form_request, login
        self.form_request = form_request
        self.server = make_server(Path(self.tmp.name) / "http.db", port=0)
        self.server.RequestHandlerClass.db.conn.close()
        self.server.RequestHandlerClass.db = self.db
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.cookie = login(self.server, "demo-admin@example.invalid", "demo-admin")

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        super().tearDown()

    def get(self, path):
        return self.form_request(self.server, "GET", path, None, self.cookie)

    def post(self, path, fields):
        return self.form_request(self.server, "POST", path, fields, self.cookie)

    def use_stub(self, stub):
        class StubModels:
            def __init__(self, ai):
                self.ai = ai

            def service(self, tenant):
                return self.ai

        self.server.RequestHandlerClass.models = StubModels(stub)

    def test_rules_mode_explicit_model_not_connected(self):
        self.publish_prepared(body="规则模式正文不得作为答复出现。")
        code, _, page = self.get("/admin/knowledge/qa")
        self.assertEqual(code, 200)
        self.assertIn("问答测试", page)
        self.assertIn("模型未连接", page)
        self.assertIn("AI_MODE=rules", page)
        self.assertIn("请在企业设置连接模型", page, "规则模式给出连接模型指引（第一批收尾修复）")
        self.assertIn("规则说明", page)
        code, _, page = self.post("/admin/knowledge/qa", {"question": "发货要几天？"})
        self.assertEqual(code, 200)
        self.assertIn("模型未连接", page)
        self.assertNotIn("规则模式正文不得作为答复出现。", page, "规则模式不得生成模拟答复")
        self.assertEqual(self.db.list_human_tasks(TENANT), [])

    def test_grounded_page_shows_quote_and_exact_version_viewer(self):
        item_id, version = self.publish_prepared()
        stub = StubKnowledgeAI(on_call=choose_first)
        self.use_stub(stub)
        code, _, page = self.get("/admin/knowledge/qa")
        self.assertIn("模型已接通", page)
        code, _, page = self.post("/admin/knowledge/qa", {"question": "发货要几天？"})
        self.assertEqual(code, 200)
        chunk = self.db.list_knowledge_chunks(TENANT, version["id"])[0]["text"]
        self.assertIn(chunk, page, "回答区展示服务端精确引用")
        self.assertIn("引用原文与版本", page)
        self.assertIn(version["id"], page, "引用查看器包含精确版本行")
        self.assertIn("v1", page)
        self.assertIn(f"/admin/knowledge/item/{item_id}", page)
        self.assertNotIn("/admin/knowledge/files/", page, "测试页不得暴露原件下载")

    def test_no_evidence_and_error_states_are_explicit(self):
        stub = StubKnowledgeAI(selection=None)
        self.use_stub(stub)
        code, _, page = self.post("/admin/knowledge/qa", {"question": "发货要几天？"})
        self.assertIn("建议转人工", page)
        self.assertIn("候选证据 0 条", page)
        self.assertEqual(self.db.list_human_tasks(TENANT), [])
        # 有候选后才可能进入模型调用；模型失败必须是明确错误，不落假答案。
        self.publish_prepared()
        stub2 = StubKnowledgeAI(error=AIUnavailableError(ai_client.STATUS_TIMEOUT, "timeout"))
        self.use_stub(stub2)
        code, _, page = self.post("/admin/knowledge/qa", {"question": "发货要几天？"})
        self.assertIn("模型调用失败", page)
        self.assertIn(ai_client.STATUS_TIMEOUT, page)
        self.assertEqual(self.db.list_human_tasks(TENANT), [])

    def test_member_can_open_test_page_readonly(self):
        from test_langbot_bridge import login as login_member
        member_cookie = login_member(self.server, "demo-sales@example.invalid", "demo-sales")
        status, _, page = self.form_request(self.server, "GET", "/admin/knowledge/qa", None, member_cookie)
        self.assertEqual(status, 200)
        self.assertIn("问答测试", page)


if __name__ == "__main__":
    unittest.main()
