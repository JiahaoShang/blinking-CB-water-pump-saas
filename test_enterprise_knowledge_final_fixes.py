"""企业知识第一批 · 复核修正验收测试（review-task23 清单 8-14；先于实现编写）。

覆盖（逐项对应清单编号）：
8 导航：侧栏主入口为「全部知识」（对齐最终原型）；不再有重复的独立「产品管理」主入口
  （旧路径 /admin/products 保留可达，产品模块上下文入口保留）；正文不再重复四个视图链接；
  列表标题用原型口语说明（不含长技术合取）。
9 保存阻断修复：合法提交（保存/发布/上传）清除未保存标记，浏览器原生 beforeunload 不再拦截
  这次真实请求；被取消的提交保留标记（Node 动态驱动 + 真实浏览器走查，见交付报告）。
  到期提示改为用户口径（北京时间 / 到期停止 AI 使用），不含 UTC 存储与检索实现术语。
10 原产品投影：客户视角 = 已发布版本快照值 ∩ 当前未被撤销；未发布编辑不进入客户证据，
  也不改变当前发布视图（绝不出现草稿候选值）。
11 原产品逐项 AI 客服授权 + 可选到期（侧车存储，不改产品行）；共享闸门在网页/渠道/测试页
  的模型调用之前与之后生效；迁移只覆盖既有「已发布 + 对外已核准字段」的产品，
  新建产品默认未授权（不做全局自动授权）。
12 手工产品/服务要点有显式 使用范围 / 核准 / 出处 控件；对外条目不外泄仅内部与未核对要点；
  缺少显式控件的旧式提交按最保守解释（仅内部 + 待核对），不被静默视为已核准。
13 会话里存过「收起」时，直接进入活动子视图仍展开（服务端活动子视图优先）。
14 分类视图新增入口提供该类的两种形态选择（文章/FAQ、产品/服务）；新建编辑器可切换形态
  且保留已输入内容；编辑既有条目形态固定。

只用临时数据库与临时上传目录；不触碰 data/ 与任何密钥；stub 模型不代表真实模型联调。
"""
import json
import os
import tempfile
import threading
import unittest
from datetime import datetime, timezone
from pathlib import Path

import ai_client
from ai_client import AIConfig
from app import Database, answer_knowledge_question, answer_question_prefer_dify, answer_question_with_mode, process_channel_message

TENANT = "demo-tenant"


class StubAI:
    """AIService 边界的受控替身：记录调用并按脚本返回选择（不联网、不接真实厂商）。"""

    def __init__(self, selection=None, on_call=None):
        self.mode = ai_client.MODE_DEEPSEEK
        self.config = AIConfig(mode=ai_client.MODE_DEEPSEEK, provider="stub", model="stub-model")
        self.selection = selection
        self.on_call = on_call
        self.calls = []

    def select_answer_field(self, question, allowed):
        self.calls.append({"kind": "field", "question": question, "allowed": [dict(item) for item in allowed]})
        if self.on_call:
            self.on_call(self)
        return self.selection, {"duration_ms": 5, "http_status": 200, "prompt_tokens": 9, "completion_tokens": 3}

    def select_knowledge_evidence(self, question, evidence):
        self.calls.append({"kind": "evidence", "question": question, "evidence": [dict(item) for item in evidence]})
        if self.on_call:
            self.on_call(self)
        return self.selection, {"duration_ms": 7, "http_status": 200, "prompt_tokens": 9, "completion_tokens": 3}


class FinalFixesBase(unittest.TestCase):
    maxDiff = None

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["B2B_KB_STORAGE_ROOT"] = str(Path(self.tmp.name) / "uploads")
        self.db = Database(Path(self.tmp.name) / "knowledge.db")
        self.db.seed_demo_users()
        self.admin = self.db.authenticate_user("demo-admin@example.invalid", "demo-admin")
        self.member = self.db.authenticate_user("demo-sales@example.invalid", "demo-sales")
        self.db.knowledge_storage_root = Path(self.tmp.name) / "uploads"

    def tearDown(self):
        self.db.conn.close()
        self.tmp.cleanup()
        os.environ.pop("B2B_KB_STORAGE_ROOT", None)

    # ── 帮助 ─────────────────────────────────────────────────────────────
    def create_article(self, title="保修政策", body="保修 24 个月。", **kw):
        return self.db.create_knowledge_item(
            TENANT, "article", title, self.admin["id"], content={"body": body}, **kw)

    def make_published_product(self, model="DS-50", extra_fields=(), internal_keys=()):
        product_id = self.db.create_product(TENANT, model, "测试泵", "冷却水循环")
        source_id = self.db.add_source(TENANT, product_id, f"{model}-manual.txt", "TXT", "第 2 页：规格参数表",
                                       actor="demo-admin", scope="exact_model")
        fields = [("flow_range", "20-60", "m³/h"), ("head_range", "25-45", "m")] + list(extra_fields)
        for key, value, unit in fields:
            self.db.save_field(TENANT, product_id, key, value, unit, source_id, "第 2 页：规格参数表")
            self.db.approve_field(TENANT, product_id, key)
        for key in internal_keys:
            self.db.conn.execute(
                "UPDATE product_fields SET audience='internal' WHERE tenant_id=? AND product_id=? AND field_key=?",
                (TENANT, product_id, key))
        self.db.conn.commit()
        self.db.finalize_product_review(TENANT, product_id)
        self.db.publish_product(TENANT, product_id)
        return product_id, source_id

    def authorize_product(self, product_id, *, authorized=True, expires_at=None, confirmed=True):
        self.db.set_knowledge_product_authorization(
            TENANT, product_id, self.admin["id"], authorized=authorized,
            expires_at=expires_at, confirmed=confirmed)


class HttpBase(FinalFixesBase):
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
        self.cookies = {
            self.admin["id"]: login(self.server, "demo-admin@example.invalid", "demo-admin"),
            self.member["id"]: login(self.server, "demo-sales@example.invalid", "demo-sales"),
        }

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        super().tearDown()

    def get(self, path, user=None):
        return self.form_request(self.server, "GET", path, None, self.cookies[(user or self.admin)["id"]])

    def post(self, path, fields, user=None):
        return self.form_request(self.server, "POST", path, fields or {}, self.cookies[(user or self.admin)["id"]])


class NavStructureTests(HttpBase):
    """清单 8：主入口「全部知识」；去重复入口；正文不再重复四视图；标题口语化。"""

    def test_main_nav_item_is_all_knowledge_without_duplicate_product_entry(self):
        _, _, page = self.get("/admin/knowledge")
        # 侧栏主入口（折叠导航主链接）文本为「全部知识」，与最终原型一致。
        self.assertRegex(page, r"data-kb-nav-main[^>]*>(?:.*?)全部知识</a>")
        # 不再保留重复的独立「产品管理」主入口。
        self.assertNotIn("<a class='side-link' href='/admin/products'>", page)
        self.assertNotIn("side-count' data-nav-count='products'", page)
        # 折叠导航仍在（主按钮 + 独立箭头 + 子项）。
        self.assertIn("data-nav-toggle", page)
        self.assertIn("nav-knowledge-subs", page)

    def test_legacy_products_url_kept_reachable_with_contextual_entry(self):
        product_id, _ = self.make_published_product()
        code, _, page = self.get("/admin/products")
        self.assertEqual(code, 200, "旧路径保留可用")
        _, _, page = self.get(f"/admin/knowledge/product/{product_id}")
        self.assertIn(f"href='/admin/products/{product_id}'", page, "产品模块编辑/复核入口在知识产品页上下文可达")

    def test_body_does_not_repeat_view_navigation(self):
        _, _, page = self.get("/admin/knowledge")
        for view in ("product_service", "article_faq", "files"):
            self.assertEqual(page.count(f"href='/admin/knowledge?view={view}'"), 1,
                             f"视图链接 {view} 只应在侧栏出现一次（正文不得重复四个导航）")
        self.assertEqual(page.count("href='/admin/knowledge/qa'"), 1)

    def test_view_headings_use_plain_prototype_language(self):
        _, _, page = self.get("/admin/knowledge")
        self.assertIn("<h1>全部知识</h1>", page)
        self.assertIn("企业知识统一入口：文章、FAQ、产品与服务、文件资料都在这里管理", page)
        self.assertNotIn("已发布 + 对外 + 已授权 + 未过期 + 当前版本准备完成", page, "标题描述不得再用长技术合取")
        _, _, page = self.get("/admin/knowledge?view=article_faq")
        self.assertIn("<h1>文章与 FAQ</h1>", page)
        _, _, page = self.get("/admin/knowledge?view=product_service")
        self.assertIn("<h1>产品与服务</h1>", page)
        _, _, page = self.get("/admin/knowledge?view=files")
        self.assertIn("<h1>文件资料</h1>", page)


class SaveProtectionUiTests(HttpBase):
    """清单 9：提交守卫钩子 + 到期提示用户口径（动态行为见 DynamicDomTests 与真实浏览器走查）。"""

    def test_editor_expiry_hint_plain_language(self):
        item_id = self.create_article()
        _, _, page = self.get(f"/admin/knowledge/item/{item_id}/edit")
        self.assertIn("到期时间按北京时间填写", page)
        self.assertIn("到期后该条不再用于 AI 客服回答", page)
        self.assertNotIn("归一为 UTC", page)
        self.assertNotIn("检索前即被排除", page)

    def test_saved_draft_notice_uses_plain_language(self):
        code, headers, _ = self.post("/admin/knowledge/new", {
            "form": "article", "title": "保存提示", "body": "正文。"})
        self.assertEqual(code, 303)
        item = self.db.list_knowledge_items(TENANT)[0]
        _, _, page = self.get(f"/admin/knowledge/item/{item['id']}?notice=saved_draft")
        self.assertIn("草稿已保存；已发布条目的线上版本不受影响。", page)
        self.assertNotIn("notice=saved_draft'>saved_draft", page)
        self.assertNotIn(">saved_draft</div>", page)

    def test_pages_install_submit_guard_hook(self):
        item_id = self.create_article()
        for path in (f"/admin/knowledge/item/{item_id}/edit", "/admin/knowledge/import"):
            _, _, page = self.get(path)
            self.assertIn("__kbUnsavedGuard", page, path)
            self.assertIn("guard.submitted()", page, path)


class ContextualCreateTests(HttpBase):
    """清单 14：分类视图两种形态选择；编辑器内切换保留内容；编辑既有条目形态固定。"""

    def test_category_views_offer_both_shapes_only(self):
        _, _, page = self.get("/admin/knowledge?view=article_faq")
        self.assertIn("data-kb-add='article_faq'", page)
        self.assertIn("/admin/knowledge/new?form=article", page)
        self.assertIn("/admin/knowledge/new?form=faq", page)
        self.assertNotIn("/admin/knowledge/new?form=product", page)
        self.assertNotIn("/admin/knowledge/new?form=service", page)
        self.assertNotIn("/admin/knowledge/import", page)
        _, _, page = self.get("/admin/knowledge?view=product_service")
        self.assertIn("data-kb-add='product_service'", page)
        self.assertIn("/admin/knowledge/new?form=product", page)
        self.assertIn("/admin/knowledge/new?form=service", page)
        self.assertNotIn("/admin/knowledge/new?form=article", page)
        self.assertNotIn("/admin/knowledge/new?form=faq", page)
        # 「全部知识」保留通用菜单（含上传文件）。
        _, _, page = self.get("/admin/knowledge")
        for token in ("/admin/knowledge/new?form=article", "/admin/knowledge/new?form=faq",
                      "/admin/knowledge/new?form=product", "/admin/knowledge/import"):
            self.assertIn(token, page)
        # 只读成员没有任何新增入口。
        _, _, page = self.get("/admin/knowledge?view=article_faq", self.member)
        self.assertNotIn("data-kb-add", page)

    def test_editor_shape_switch_markup(self):
        _, _, page = self.get("/admin/knowledge/new?form=article")
        self.assertIn("data-form-switch", page)
        self.assertIn("data-kb-form-switch='article'", page)
        self.assertIn("data-kb-form-switch='faq'", page)
        self.assertIn("data-kb-form-block='article'", page)
        self.assertIn("data-kb-form-block='faq'", page)
        _, _, page = self.get("/admin/knowledge/new?form=service")
        self.assertIn("data-kb-form-block='shared'", page)
        self.assertIn("data-kb-form-switch='product'", page)
        self.assertIn("data-kb-form-switch='service'", page)
        item_id = self.create_article()
        _, _, page = self.get(f"/admin/knowledge/item/{item_id}/edit")
        self.assertNotIn("data-form-switch", page, "编辑既有条目时内容形态固定")


class ManualFieldScopeTests(HttpBase):
    """清单 12：手工产品/服务要点的 使用范围 / 核准 / 出处；对外条目不外泄内部与未核对要点。"""

    def test_product_editor_has_scope_approval_source_controls(self):
        _, _, page = self.get("/admin/knowledge/new?form=product")
        for token in ("name='field_audience_0'", "name='field_approved_0'", "name='field_source_0'",
                      "使用范围", "已核对（核准）", "出处（来源说明）"):
            self.assertIn(token, page)
        self.assertNotIn("单位 / 使用范围", page, "旧的合并输入框应已被拆分为显式控件")
        _, _, page = self.get("/admin/knowledge/new?form=service")
        self.assertIn("name='field_audience_0'", page)

    def test_external_manual_item_hides_internal_and_unapproved_fields(self):
        code, _, _ = self.post("/admin/knowledge/new", {
            "form": "product", "title": "安装服务包", "body": "现场安装说明正文。",
            "usage": "external", "ai_authorized": "1",
            "field_label_0": "标准工期", "field_value_0": "3 天", "field_unit_0": "工作日",
            "field_audience_0": "external", "field_approved_0": "1", "field_source_0": "报价单 Rev.C",
            "field_label_1": "内部成本", "field_value_1": "成本 999 元", "field_unit_1": "",
            "field_audience_1": "internal", "field_approved_1": "1", "field_source_1": "采购台账",
            "field_label_2": "试用装数量", "field_value_2": "5 套", "field_unit_2": "套",
            "field_audience_2": "external", "field_source_2": "口头说明",
        })
        self.assertEqual(code, 303)
        item = self.db.list_knowledge_items(TENANT)[0]
        draft = self.db.get_knowledge_draft_version(TENANT, item["id"])
        fields = {field["label"]: field for field in json.loads(draft["content_json"])["fields"]}
        self.assertEqual((fields["标准工期"]["audience"], fields["标准工期"]["state"], fields["标准工期"]["source"]),
                         ("external", "approved", "报价单 Rev.C"))
        self.assertEqual(fields["内部成本"]["audience"], "internal")
        self.assertEqual(fields["试用装数量"]["state"], "candidate")
        # 发布（对外 + 已授权需要显式确认字段）。
        code, _, _ = self.post(f"/admin/knowledge/item/{item['id']}/publish", {"confirmed": "1"})
        self.assertEqual(code, 303)
        version = self.db.get_published_knowledge_version(TENANT, item["id"])
        text = "\n".join(chunk["text"] for chunk in self.db.list_knowledge_chunks(TENANT, version["id"]))
        self.assertIn("标准工期：3 天", text)
        self.assertNotIn("内部成本", text, "对外条目不外泄仅内部要点")
        self.assertNotIn("999", text)
        self.assertNotIn("试用装数量", text, "未勾选核准的要点不被静默视为已核准")
        evidence_text = "\n".join(unit["text"] for unit in self.db.list_customer_qa_evidence(TENANT))
        self.assertNotIn("999", evidence_text)
        self.assertNotIn("试用装数量", evidence_text)
        # 详情页：使用范围 / 核准 / 出处都在内部可见。
        _, _, page = self.get(f"/admin/knowledge/item/{item['id']}")
        for token in ("仅内部", "待核对", "报价单 Rev.C", "采购台账"):
            self.assertIn(token, page)

    def test_legacy_style_post_without_field_controls_is_conservative(self):
        code, _, _ = self.post("/admin/knowledge/new", {
            "form": "service", "title": "旧表单服务", "body": "说明。",
            "field_label_0": "响应时间", "field_value_0": "4 小时",
        })
        self.assertEqual(code, 303)
        item = self.db.list_knowledge_items(TENANT)[0]
        draft = self.db.get_knowledge_draft_version(TENANT, item["id"])
        field = json.loads(draft["content_json"])["fields"][0]
        self.assertEqual(field["audience"], "internal", "缺少显式使用范围时按最保守处理")
        self.assertEqual(field["state"], "candidate", "缺少显式核准时不得被静默视为已核准")


class ProductSnapshotTests(FinalFixesBase):
    """清单 10：客户视角 = 已发布快照值 ∩ 当前撤销状态；未发布编辑不进入。"""

    def test_unpublished_edits_never_reach_customer_view(self):
        product_id, source_id = self.make_published_product()
        self.authorize_product(product_id)
        # 编辑并重新核准 flow_range，但不发布 v2。
        self.db.save_field(TENANT, product_id, "flow_range", "88-99", "m³/h", source_id, "第 9 页")
        self.db.approve_field(TENANT, product_id, "flow_range")
        fields = {field["field_key"]: field for field in self.db.knowledge_product_fields(TENANT, product_id)}
        self.assertEqual(fields["flow_range"]["value"], "20-60", "未发布编辑不改变客户所见（仍是 v1 值）")
        facts = self.db.list_ai_answerable_product_facts(TENANT, product_id)
        flow = next(fact for fact in facts[0]["facts"] if fact["field_key"] == "flow_range")
        self.assertEqual(flow["value"], "20-60", "客户证据也必须来自已发布快照")
        # 新增并核准一个发布时不存在的新字段：不得进入客户视图。
        self.db.save_field(TENANT, product_id, "material", "铸铁", "", source_id, "第 2 页")
        self.db.approve_field(TENANT, product_id, "material")
        keys = {field["field_key"] for field in self.db.knowledge_product_fields(TENANT, product_id)}
        self.assertNotIn("material", keys, "未发布的新字段不能提前出现")
        # 重新发布 v2 后才切换到新值。
        self.db.finalize_product_review(TENANT, product_id)
        self.db.publish_product(TENANT, product_id)
        fields = {field["field_key"]: field for field in self.db.knowledge_product_fields(TENANT, product_id)}
        self.assertEqual(fields["flow_range"]["value"], "88-99")
        self.assertIn("material", fields)

    def test_revocations_intersect_published_snapshot(self):
        product_id, source_id = self.make_published_product(extra_fields=[("media", "清水", "")])
        self.authorize_product(product_id)
        fields = {field["field_key"] for field in self.db.knowledge_product_fields(TENANT, product_id)}
        self.assertEqual(fields, {"flow_range", "head_range", "media"})
        # 撤销①：head_range 改仅内部 → 立即排除。
        self.db.conn.execute(
            "UPDATE product_fields SET audience='internal' WHERE tenant_id=? AND product_id=? AND field_key='head_range'",
            (TENANT, product_id))
        self.db.conn.commit()
        # 撤销②：flow_range 重新编辑未核准（课程状态回到待核准）→ 立即排除。
        self.db.save_field(TENANT, product_id, "flow_range", "70-80", "m³/h", source_id, "第 3 页")
        # 撤销③：media 的来源被标记禁止使用 → 立即排除。
        self.db.set_source_usage(TENANT, product_id, source_id, "restricted", "demo-admin")
        fields = {field["field_key"] for field in self.db.knowledge_product_fields(TENANT, product_id)}
        self.assertEqual(fields, set(), "撤销（改内部 / 重新待核准 / 来源禁用）立即从未发布快照交集移除")
        revoked = self.db.knowledge_product_field_revocations(TENANT, product_id)
        self.assertIn("使用范围已改为仅内部", revoked.get("head_range", ""))
        self.assertIn("重新进入待核准", revoked.get("flow_range", ""))
        self.assertIn("禁止使用", revoked.get("media", ""))
        # 恢复：重新核准 flow_range（来源恢复正常）→ 回到 v1 快照值（不出现编辑中的新值）。
        self.db.set_source_usage(TENANT, product_id, source_id, "internal", "demo-admin")
        self.db.approve_field(TENANT, product_id, "flow_range")
        fields = {field["field_key"]: field for field in self.db.knowledge_product_fields(TENANT, product_id)}
        self.assertEqual(fields["flow_range"]["value"], "20-60")

    def test_facts_carry_true_published_version(self):
        product_id, _ = self.make_published_product()
        self.authorize_product(product_id)
        facts = self.db.list_ai_answerable_product_facts(TENANT, product_id)
        self.assertEqual(facts[0]["product_version"], 1)
        self.assertEqual({fact["value"] for fact in facts[0]["facts"]}, {"20-60", "25-45"})


class ProductConsentGateTests(FinalFixesBase):
    """清单 11：逐产品 AI 授权 + 到期闸门；模型前后生效；迁移只覆盖既有产品。"""

    def test_default_unauthorized_blocks_model_calls_and_answers(self):
        product_id, _ = self.make_published_product()
        self.assertEqual(self.db.knowledge_product_ai_gate(TENANT, product_id), (False, "未授权 AI 客服使用"))
        self.assertEqual(self.db.list_ai_answerable_product_facts(TENANT, product_id), [])
        self.assertEqual([unit for unit in self.db.list_customer_qa_evidence(TENANT) if unit["source"] == "product_field"], [])
        # 字段问答：闸门在模型调用之前 → 不调模型、不出答案。
        stub = StubAI(selection="flow_range")
        status, _, _, _, citations, reason = answer_question_with_mode(self.db, stub, TENANT, "", "What is the flow range?", product_id)
        self.assertEqual(status, "needs_human")
        self.assertEqual(citations, [])
        self.assertEqual(stub.calls, [], "未授权产品不得触发模型调用")
        self.assertIn("未授权 AI 客服使用", reason)
        # 统一入口（网页/渠道共用）：字段阶段与知识阶段都不会使用未授权产品。
        stub2 = StubAI(selection="e1")
        status, *_rest = answer_question_prefer_dify(self.db, stub2, TENANT, "", "What is the flow range?", product_id, "web:test")
        self.assertEqual(status, "needs_human")
        self.assertEqual(stub2.calls, [])

    def test_confirm_expiry_boundary_and_after_model_revocation(self):
        product_id, _ = self.make_published_product()
        with self.assertRaises(ValueError):
            self.authorize_product(product_id, confirmed=False)  # 已发布产品开启授权需要确认
        self.assertEqual(self.db.knowledge_product_ai_gate(TENANT, product_id)[0], False)
        self.authorize_product(product_id)
        self.assertEqual(self.db.knowledge_product_ai_gate(TENANT, product_id)[0], True)
        # 模型调用成功且响应后仍有效 → 正常回答。
        stub = StubAI(selection="flow_range")
        status, answer, *_rest = answer_question_with_mode(self.db, stub, TENANT, "", "What is the flow range?", product_id)
        self.assertEqual(status, "grounded")
        self.assertIn("20-60", answer)
        # 生成期间撤销授权 → 模型响应后复核拦截（绝不输出）。
        stub2 = StubAI(selection="flow_range", on_call=lambda ai: self.authorize_product(product_id, authorized=False))
        status, answer, *_rest = answer_question_with_mode(self.db, stub2, TENANT, "", "What is the flow range?", product_id)
        self.assertEqual(status, "needs_human")
        self.assertNotIn("20-60", answer)
        # 到期边界：now >= 到期 → 排除；到期前 1 秒仍可用（北京时间输入，存 UTC）。
        self.authorize_product(product_id, expires_at="2026-10-10T20:00")  # 北京 20:00 = UTC 12:00
        at_expiry = datetime(2026, 10, 10, 12, 0, 0, tzinfo=timezone.utc)
        before = datetime(2026, 10, 10, 11, 59, 59, tzinfo=timezone.utc)
        self.assertEqual(self.db.knowledge_product_ai_gate(TENANT, product_id, now=at_expiry), (False, "AI 客服授权已到期"))
        self.assertEqual(self.db.knowledge_product_ai_gate(TENANT, product_id, now=before)[0], True)
        stub3 = StubAI(selection="flow_range")
        outcome = answer_knowledge_question(self.db, stub3, TENANT, "", "问题")
        self.assertEqual(outcome["status"], "no_evidence", "到期产品不进入通用问答候选")
        self.assertEqual(stub3.calls, [])

    def test_migration_backfills_existing_published_only(self):
        product_id, _ = self.make_published_product(model="OLD-1")
        internal_only, _ = self.make_published_product(model="OLD-2", internal_keys=("flow_range", "head_range"))
        # 模拟旧库升级：侧车表首次创建时执行一次性迁移。
        self.db.conn.execute("DROP TABLE knowledge_product_settings")
        self.db.conn.commit()
        self.db.init_enterprise_knowledge()
        settings = self.db.knowledge_product_settings(TENANT, product_id)
        self.assertEqual(settings["ai_authorized"], 1, "既有的已发布产品按原行为保持可用")
        self.assertEqual(settings["updated_by"], "migration")
        self.assertEqual(self.db.knowledge_product_settings(TENANT, internal_only)["ai_authorized"], 0,
                         "没有对外已核准字段的产品不被迁移授权")
        # 迁移之后新建并发布的产品：默认未授权（不做全局自动授权）。
        fresh, _ = self.make_published_product(model="NEW-1")
        self.assertEqual(self.db.knowledge_product_ai_gate(TENANT, fresh), (False, "未授权 AI 客服使用"))

    def test_sidecar_only_keeps_product_row_untouched(self):
        product_id, _ = self.make_published_product()
        before = self.db.get_product(TENANT, product_id)
        self.authorize_product(product_id)
        after = self.db.get_product(TENANT, product_id)
        self.assertEqual((after["id"], after["created_at"], after["updated_at"], after["status"]),
                         (before["id"], before["created_at"], before["updated_at"], before["status"]),
                         "授权只写侧车表，不改动产品行")
        count = self.db.conn.execute("SELECT COUNT(*) FROM products WHERE tenant_id=? AND model='DS-50'", (TENANT,)).fetchone()[0]
        self.assertEqual(count, 1, "不复制产品记录")

    def test_channel_shared_gate_blocks_unpublished_consent(self):
        from unittest.mock import patch
        from app import get_or_create_channel_conversation
        product_id, _ = self.make_published_product()
        account_id = self.db.create_business_connection(TENANT, "langbot", "Telegram", "业务号", self.admin["id"], self.admin["email"])
        self.db.set_connection_ai(TENANT, account_id, True, self.admin["id"], confirmed=True)
        version = self.db.get_published_version(TENANT, product_id)
        conversation_id = get_or_create_channel_conversation(self.db, TENANT, "langbot", "cust-gate", "客户", account_id=account_id)
        self.db.conn.execute(
            "INSERT INTO requirement_revisions(id,tenant_id,conversation_id,revision,values_json,created_by,created_at) "
            "VALUES ('rev-gate',?,?,1,'{}','demo-admin','2026-01-01')", (TENANT, conversation_id))
        self.db.conn.execute(
            "INSERT INTO recommendations(id,tenant_id,conversation_id,requirement_revision_id,status,rule_version,results_json,created_at) "
            "VALUES ('rec-gate',?,?,'rev-gate','served','v1',?,'2026-01-01')",
            (TENANT, conversation_id, json.dumps([{"product_id": product_id, "version_id": version["id"], "status": "candidate"}])))
        self.db.conn.commit()
        # 渠道侧绑定该产品提问：未授权 → 不出答复、不调模型（与网页同一闸门）。
        stub = StubAI(selection="flow_range")
        with patch("app.dispatch_outbound", return_value=(True, "")):
            result = process_channel_message(self.db, stub, TENANT, "langbot", "cust-gate", "客户", "What is the flow range?", "m-gate", account_id)
        self.assertEqual(stub.calls, [], "未授权产品不得在渠道侧触发模型调用")
        self.assertEqual(result["action"], "answer_unresolved")
        row = self.db.conn.execute(
            "SELECT * FROM answers WHERE tenant_id=? ORDER BY rowid DESC LIMIT 1", (TENANT,)).fetchone()
        self.assertEqual(row["answer_status"], "needs_human")
        self.assertNotIn("20-60", row["answer_text"])
        # 显式授权后，同一渠道问题才进入既有字段问答（客户视角一致）。
        self.authorize_product(product_id)
        stub2 = StubAI(selection="flow_range")
        with patch("app.dispatch_outbound", return_value=(True, "")):
            result2 = process_channel_message(self.db, stub2, TENANT, "langbot", "cust-gate", "客户", "What is the flow range again?", "m-gate-2", account_id)
        self.assertEqual(result2["action"], "answered")
        self.assertEqual(len(stub2.calls), 1)


class ProductDetailUiTests(HttpBase):
    """清单 11 UI：授权设置、撤销展示、来源内部可见、旧路径入口。"""

    def test_product_page_settings_revocation_sources_and_readers(self):
        product_id, source_id = self.make_published_product()
        _, _, page = self.get(f"/admin/knowledge/product/{product_id}")
        self.assertIn("允许 AI 客服使用", page)
        self.assertIn(f"action='/admin/knowledge/product/{product_id}/authorize'", page)
        self.assertIn(f"href='/admin/products/{product_id}'", page)
        self.assertIn("未授权", page)
        self.assertIn("来源文件", page)
        self.assertIn("DS-50-manual.txt", page)
        # 开启授权（已发布需要确认）：不带确认字段不得开启。
        code, headers, _ = self.post(f"/admin/knowledge/product/{product_id}/authorize", {"authorized": "1"})
        self.assertEqual(code, 303)
        self.assertIn("error=", headers.get("Location", ""))
        self.assertEqual(self.db.knowledge_product_settings(TENANT, product_id)["ai_authorized"], 0)
        code, _, _ = self.post(f"/admin/knowledge/product/{product_id}/authorize",
                               {"authorized": "1", "confirmed": "1", "expiry_date": "2099-12-31"})
        self.assertEqual(code, 303)
        settings = self.db.knowledge_product_settings(TENANT, product_id)
        self.assertEqual(settings["ai_authorized"], 1)
        self.assertEqual(settings["expires_at"], "2099-12-31T23:59:59+00:00")
        _, _, page = self.get(f"/admin/knowledge/product/{product_id}")
        self.assertIn("已授权", page)
        # 撤销（来源被禁用）在页面明确显示，字段不再标记进入客户问答。
        self.db.set_source_usage(TENANT, product_id, source_id, "restricted", "demo-admin")
        _, _, page = self.get(f"/admin/knowledge/product/{product_id}")
        self.assertIn("已撤销：来源文件已被标记禁止使用", page)
        self.assertIn("无对外已核准且有效的字段", page)
        # 只读成员：字段与来源内部可见；没有设置表单。
        _, _, page = self.get(f"/admin/knowledge/product/{product_id}", self.member)
        self.assertIn("DS-50-manual.txt", page)
        self.assertIn("来源文件", page)
        self.assertNotIn("保存 AI 客服设置", page, "只读成员没有设置表单")
        self.assertIn("调整授权与到期需要管理员", page)


class QATestPageGateTests(HttpBase):
    """清单 11：问答测试页与共享闸门一致（候选证据不包含未授权产品）。"""

    def test_corpus_count_excludes_unauthorized_product(self):
        product_id, _ = self.make_published_product()
        _, _, page = self.get("/admin/knowledge/qa")
        self.assertIn("产品字段 0", page)
        self.authorize_product(product_id)
        _, _, page = self.get("/admin/knowledge/qa")
        self.assertIn("产品字段 2", page)


class DynamicDomTests(unittest.TestCase):
    """清单 9/13/14 动态 DOM 复核：用 Node + 迷你 DOM 驱动真实 KB_JS 与折叠导航 JS。"""

    @classmethod
    def setUpClass(cls):
        import shutil
        import subprocess as _subprocess
        cls.node = shutil.which("node")
        if cls.node is None:
            raise unittest.SkipTest("未找到 node：动态 DOM 复核跳过（未验证）")
        version = _subprocess.run([cls.node, "--version"], capture_output=True, text=True, timeout=30).stdout.strip()
        try:
            major = int(version.lstrip("v").split(".")[0])
        except ValueError:
            major = 0
        if major < 18:
            raise unittest.SkipTest(f"node 版本过低（{version}）：动态 DOM 复核跳过（未验证）")

    def run_harness(self):
        import subprocess as _subprocess
        from app import CONSOLE_KNOWLEDGE_NAV_JS
        from enterprise_knowledge_ui import KB_FORM_SWITCH_JS, KB_JS
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.json"
            result_path = Path(tmp) / "result.json"
            config_path.write_text(json.dumps({
                "kbJs": KB_JS, "navJs": CONSOLE_KNOWLEDGE_NAV_JS, "switchJs": KB_FORM_SWITCH_JS,
            }), encoding="utf-8")
            proc = _subprocess.run(
                [self.node, "tools/browser_tests/knowledge_ui_browser.mjs", str(config_path), str(result_path)],
                capture_output=True, text=True, timeout=120,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            return json.loads(result_path.read_text(encoding="utf-8"))

    def test_save_submit_clears_unload_guard(self):
        facts = self.run_harness()["scenarios"]["saveFlow"]
        self.assertTrue(facts["dirtyBlocksUnload"], "有未保存内容时 beforeunload 拦截离开")
        self.assertTrue(facts["saveSubmitAllowed"], "合法保存草稿提交不被确认逻辑阻止")
        self.assertTrue(facts["saveClearsDirty"], "合法提交清除未保存标记：beforeunload 不再拦截这次保存")
        self.assertTrue(facts["publishDeclinedPrevented"], "确认框取消时阻止提交")
        self.assertTrue(facts["publishDeclinedKeepsDirty"], "取消后未保存标记保留（内容仍受保护）")
        self.assertEqual(facts["publishDeclinedConfirmField"], "", "取消时不得写入 confirm_ai")
        self.assertTrue(facts["publishAcceptedAllowed"])
        self.assertEqual(facts["publishAcceptedConfirmField"], "1", "确认发布写入显式确认字段")
        self.assertTrue(facts["publishAcceptedClearsDirty"], "确认发布路径同样清除未保存标记")

    def test_nav_server_active_subview_wins_stored_closed(self):
        facts = self.run_harness()["scenarios"]["navFold"]
        self.assertTrue(facts["serverWinsOverStoredClosed"], "清单13：会话存过收起时，直接进入子视图仍展开")
        self.assertTrue(facts["explicitCollapsed"], "无活动子视图时会话存储的收起仍生效")
        self.assertTrue(facts["toggleClosedByDefault"])

    def test_editor_shape_switch_preserves_content(self):
        facts = self.run_harness()["scenarios"]["editorShapeSwitch"]
        self.assertEqual(facts["initialForm"], "article")
        self.assertTrue(facts["initialFaqHidden"])
        self.assertEqual(facts["formAfterSwitch"], "faq", "切换更新隐藏 form 值")
        self.assertTrue(facts["articleHiddenAfterSwitch"])
        self.assertTrue(facts["faqVisibleAfterSwitch"])
        self.assertEqual(facts["bodyPreserved"], "正文内容", "切换不丢已输入内容")
        self.assertEqual(facts["titleLabelAfterSwitch"], "FAQ 标题")
        self.assertEqual(facts["formTitleAfterSwitch"], "添加 FAQ")
        self.assertEqual(facts["faqPressed"], "true")
        self.assertEqual(facts["articlePressed"], "false")
        self.assertEqual(facts["formAfterSwitchBack"], "article")
        self.assertTrue(facts["bodyPreservedAfterBack"])
        self.assertTrue(facts["unsavedGuardInstalled"])


if __name__ == "__main__":
    unittest.main()
