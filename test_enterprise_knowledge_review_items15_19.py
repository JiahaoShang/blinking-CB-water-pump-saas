"""企业知识第一批 · 独立只读审查第 15-19 项必修 + 站内确认对话框验收测试。

对照 /tmp/b2b-kb-phase1/review-task23.txt 第 15-19 项（先于实现编写）：
15 原产品客户视角字段必须同时满足发布快照「对外 + 已核准」与当前字段「对外 + 已核准」；
   v1 为「仅内部」的字段只在草稿改成对外，不能以 v1 回答。
16 发布值只引用发布快照来源 A 并检查 A 当前授权；草稿换 B 不能绕过 A 被禁止使用，
   也不能把 A 的值记成 B；模型响应前后都复核来源与撤销。
17 编辑标题必须随快照真实保存（此前 _kb_snapshot_from_form 丢标题）。
18 删除全部自定义要点必须显式保存 fields=[]；再次发布后客户证据不含旧要点。
19 第一批 UI：中文保存提示（无裸状态码 saved_draft）、负责人显示企业内人名或邮箱、
   属性/提示不使用「检索前排除 / 远程索引删除」等实现术语。
+ 发布 / 停用 / 重新启用 / 开启 AI 授权改用站内可访问确认对话框（替代 window.confirm）：
  Escape 与取消不提交、焦点回到触发按钮、确认后写入显式确认字段（服务端仍强制校验）；
  保存草稿等日常操作不弹确认。
只用临时数据库与临时上传目录；不触碰 data/ 与任何密钥。
"""
from __future__ import annotations

import json
import os
import tempfile
import threading
import unittest
from pathlib import Path

from app import Database

TENANT = "demo-tenant"


class Review1519Base(unittest.TestCase):
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
    def make_published_product(self, model="RV-15", internal_keys=()):
        product_id = self.db.create_product(TENANT, model, "复核测试泵", "冷却水循环")
        source_id = self.db.add_source(
            TENANT, product_id, f"{model}-manual.txt", "TXT", "第 2 页：规格参数表",
            actor="demo-admin", scope="exact_model")
        for key, value, unit in (("flow_range", "20-60", "m³/h"), ("head_range", "25-45", "m")):
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

    def authorize_product(self, product_id):
        self.db.set_knowledge_product_authorization(
            TENANT, product_id, self.admin["id"], authorized=True, confirmed=True)

    def create_article(self, title="保修政策", body="保修 24 个月。", **kw):
        return self.db.create_knowledge_item(
            TENANT, "article", title, self.admin["id"], content={"body": body}, **kw)

    def publish_prepared(self, item_id, **kw):
        self.db.publish_knowledge_draft(TENANT, item_id, self.admin["id"], **kw)
        version = self.db.get_published_knowledge_version(TENANT, item_id)
        self.db.prepare_knowledge_version(TENANT, version["id"])
        return version


class HttpBase(Review1519Base):
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


class SnapshotAudienceTests(Review1519Base):
    """清单 15：快照与当前字段双重「对外 + 已核准」。"""

    def test_v1_internal_field_edited_external_in_draft_is_never_v1_answerable(self):
        product_id, source_id = self.make_published_product(internal_keys=("head_range",))
        self.authorize_product(product_id)
        fields = {field["field_key"] for field in self.db.knowledge_product_fields(TENANT, product_id)}
        self.assertEqual(fields, {"flow_range"}, "v1 为「仅内部」的字段一开始就不进客户视角")
        # 草稿里把 head_range 改成对外并重新核准（没有发布 v2）。
        self.db.save_field(TENANT, product_id, "head_range", "25-45", "m", source_id, "第 2 页：规格参数表")
        self.db.approve_field(TENANT, product_id, "head_range")
        self.db.conn.execute(
            "UPDATE product_fields SET audience='external' WHERE tenant_id=? AND product_id=? AND field_key='head_range'",
            (TENANT, product_id))
        self.db.conn.commit()
        fields = {field["field_key"] for field in self.db.knowledge_product_fields(TENANT, product_id)}
        self.assertNotIn("head_range", fields, "v1 快照不是「对外」，草稿改动不能让它以 v1 回答")
        texts = "\n".join(unit["text"] for unit in self.db.list_customer_qa_evidence(TENANT))
        self.assertNotIn("扬程范围", texts)
        self.assertNotIn("25-45", texts)
        # 真实发布 v2（快照对外 + 已核准）后才进入客户视角。
        self.db.finalize_product_review(TENANT, product_id)
        self.db.publish_product(TENANT, product_id)
        fields = {field["field_key"]: field for field in self.db.knowledge_product_fields(TENANT, product_id)}
        self.assertIn("head_range", fields)
        self.assertEqual(fields["head_range"]["value"], "25-45")
        facts = self.db.list_ai_answerable_product_facts(TENANT, product_id)
        self.assertEqual(facts[0]["product_version"], 2)
        self.assertIn("head_range", {fact["field_key"] for fact in facts[0]["facts"]})

    def test_current_field_must_also_stay_external_and_approved(self):
        product_id, _ = self.make_published_product()
        self.authorize_product(product_id)
        self.db.conn.execute(
            "UPDATE product_fields SET audience='internal' WHERE tenant_id=? AND product_id=? AND field_key='flow_range'",
            (TENANT, product_id))
        self.db.conn.commit()
        fields = {field["field_key"] for field in self.db.knowledge_product_fields(TENANT, product_id)}
        self.assertNotIn("flow_range", fields, "当前字段改仅内部：立即排除（快照与当前必须同时对外）")


class SnapshotSourceTests(Review1519Base):
    """清单 16：发布值只引用发布快照来源 A；换 B 不能绕过 A 撤销；A 的值不能记成 B。"""

    def test_source_a_restricted_blocks_even_when_draft_switches_to_b(self):
        product_id, source_a = self.make_published_product()
        self.authorize_product(product_id)
        source_b = self.db.add_source(
            TENANT, product_id, "RV-15-补充.txt", "TXT", "附件：修订说明",
            actor="demo-admin", scope="exact_model")
        # 草稿：把字段指向来源 B 并重新核准（没有发布 v2）。
        self.db.save_field(TENANT, product_id, "flow_range", "20-60", "m³/h", source_b, "附件：修订说明")
        self.db.approve_field(TENANT, product_id, "flow_range")
        self.db.finalize_product_review(TENANT, product_id)
        # 来源 A 被标记禁止使用：v1 发布值立即撤销，草稿换 B 不能绕过。
        self.db.set_source_usage(TENANT, product_id, source_a, "restricted")
        fields = {field["field_key"] for field in self.db.knowledge_product_fields(TENANT, product_id)}
        self.assertNotIn("flow_range", fields, "发布来源 A 被禁止使用：换 B 不能绕过 A 的撤销")
        revoked = self.db.knowledge_product_field_revocations(TENANT, product_id)
        self.assertIn("禁止使用", revoked.get("flow_range", ""))
        texts = "\n".join(unit["text"] for unit in self.db.list_customer_qa_evidence(TENANT))
        self.assertNotIn("20-60", texts)
        # 恢复 A：字段回到客户视角，出处必须指向发布快照来源 A，而不是当前草稿指向的 B。
        self.db.set_source_usage(TENANT, product_id, source_a, "internal")
        fields = {field["field_key"]: field for field in self.db.knowledge_product_fields(TENANT, product_id)}
        self.assertIn("flow_range", fields)
        self.assertEqual(fields["flow_range"]["source_document_id"], source_a,
                         "发布值只能引用发布快照来源 A，不能记成 B")
        self.assertEqual(fields["flow_range"]["source_filename"], "RV-15-manual.txt")
        self.assertEqual(fields["flow_range"]["source_location"], "第 2 页：规格参数表")
        self.assertEqual(fields["flow_range"]["value"], "20-60")
        facts = self.db.list_ai_answerable_product_facts(TENANT, product_id)
        flow = next(fact for fact in facts[0]["facts"] if fact["field_key"] == "flow_range")
        self.assertEqual(flow["source"], "RV-15-manual.txt", "客户证据的出处也只能是 A")
        # 发布 v2（值来自 B）后，出处才切换到 B。
        self.db.finalize_product_review(TENANT, product_id)
        self.db.publish_product(TENANT, product_id)
        fields = {field["field_key"]: field for field in self.db.knowledge_product_fields(TENANT, product_id)}
        self.assertEqual(fields["flow_range"]["source_document_id"], source_b)
        self.assertEqual(fields["flow_range"]["source_filename"], "RV-15-补充.txt")

    def test_after_model_recheck_validates_snapshot_source_id_and_authorization(self):
        product_id, source_a = self.make_published_product()
        self.authorize_product(product_id)
        evidence = next(unit for unit in self.db.list_customer_qa_evidence(TENANT) if unit.get("field_key") == "flow_range")
        self.assertEqual(evidence["fact_source_id"], source_a, "客户证据必须携带发布快照来源 ID")
        ok, _ = self.db.knowledge_evidence_valid_after_model(TENANT, evidence)
        self.assertTrue(ok)
        # 生成期间：来源 A 被禁止使用 → 模型之前候选移除；模型之后的复核也必须拦截。
        self.db.set_source_usage(TENANT, product_id, source_a, "restricted")
        self.assertEqual(
            [unit for unit in self.db.list_customer_qa_evidence(TENANT) if unit.get("field_key") == "flow_range"], [],
            "模型之前：来源被禁止使用的字段立即移出候选")
        ok, reason = self.db.knowledge_evidence_valid_after_model(TENANT, evidence)
        self.assertFalse(ok, "来源授权变化后，旧证据不得继续输出")
        self.assertIn("变化", reason)
        # 恢复 A；草稿换来源 B（未发布）不改变复核依据，值仍归属 A。
        self.db.set_source_usage(TENANT, product_id, source_a, "internal")
        source_b = self.db.add_source(
            TENANT, product_id, "RV-15-补充.txt", "TXT", "附件：修订说明",
            actor="demo-admin", scope="exact_model")
        self.db.save_field(TENANT, product_id, "flow_range", "20-60", "m³/h", source_b, "附件：修订说明")
        self.db.approve_field(TENANT, product_id, "flow_range")
        ok, _ = self.db.knowledge_evidence_valid_after_model(TENANT, evidence)
        self.assertTrue(ok, "草稿换 B 不能悄悄改变已发布值的来源归属")
        # 发布 v2 换版后，旧证据必须失效。
        self.db.finalize_product_review(TENANT, product_id)
        self.db.publish_product(TENANT, product_id)
        ok, _ = self.db.knowledge_evidence_valid_after_model(TENANT, evidence)
        self.assertFalse(ok, "换版后旧证据不得继续输出")


class EditedTitlePersistenceTests(HttpBase):
    """清单 17：编辑标题必须真实保存（保存与发布都不丢）。"""

    def test_edited_title_persists_on_save_and_publish(self):
        code, _, _ = self.post("/admin/knowledge/new", {
            "form": "article", "title": "初版标题", "body": "正文内容。", "usage": "internal",
            "action": "save_draft",
        })
        self.assertEqual(code, 303)
        item = self.db.list_knowledge_items(TENANT)[0]
        # 编辑标题并保存草稿：草稿快照必须带新标题。
        code, _, _ = self.post(f"/admin/knowledge/item/{item['id']}/edit", {
            "title": "修订后的标题", "body": "正文内容。", "usage": "internal", "action": "save_draft",
        })
        self.assertEqual(code, 303)
        draft = self.db.get_knowledge_draft_version(TENANT, item["id"])
        self.assertEqual(json.loads(draft["content_json"])["title"], "修订后的标题")
        _, _, page = self.get(f"/admin/knowledge/item/{item['id']}/edit?saved=1")
        self.assertIn("修订后的标题", page)
        _, _, page = self.get(f"/admin/knowledge/item/{item['id']}")
        self.assertIn("修订后的标题", page)
        # 再次编辑标题并发布：已发布版本与列表都同步新标题。
        code, _, _ = self.post(f"/admin/knowledge/item/{item['id']}/edit", {
            "title": "发布后的标题", "body": "正文内容。", "usage": "internal", "action": "publish",
        })
        self.assertEqual(code, 303)
        published = self.db.get_published_knowledge_version(TENANT, item["id"])
        self.assertEqual(json.loads(published["content_json"])["title"], "发布后的标题")
        _, _, page = self.get(f"/admin/knowledge/item/{item['id']}")
        self.assertIn("发布后的标题", page)
        # 2026-10-12 IA：旧「全部知识」列表保留在 ?view=all。
        _, _, page = self.get("/admin/knowledge?view=all")
        self.assertIn("发布后的标题", page)


class RemovedFieldsEvidenceTests(HttpBase):
    """清单 18：删除全部自定义要点必须显式 fields=[]；再次发布后客户证据不含旧要点。"""

    def test_removing_all_fields_then_publish_leaves_no_old_evidence(self):
        code, _, _ = self.post("/admin/knowledge/new", {
            "form": "product", "title": "安装服务包", "body": "现场安装说明正文。",
            "usage": "external", "ai_authorized": "1",
            "field_label_0": "标准工期", "field_value_0": "3 天", "field_unit_0": "工作日",
            "field_audience_0": "external", "field_approved_0": "1", "field_source_0": "报价单 Rev.C",
            "field_label_1": "备用要点", "field_value_1": "999", "field_unit_1": "",
            "field_audience_1": "external", "field_approved_1": "1", "field_source_1": "内部估算",
            "action": "publish", "confirm_ai": "1",
        })
        self.assertEqual(code, 303)
        item = self.db.list_knowledge_items(TENANT)[0]
        version = self.db.get_published_knowledge_version(TENANT, item["id"])
        self.assertEqual(self.db.knowledge_processing_state(TENANT, version["id"])["status"], "completed")
        texts = "\n".join(unit["text"] for unit in self.db.list_customer_qa_evidence(TENANT))
        self.assertIn("标准工期", texts)
        # 编辑：删除全部自定义要点（编辑器里要点行全部留空后保存草稿，没有发布）。
        code, _, _ = self.post(f"/admin/knowledge/item/{item['id']}/edit", {
            "title": "安装服务包", "body": "现场安装说明正文。", "usage": "external", "ai_authorized": "1",
            "field_label_0": "", "field_value_0": "", "field_unit_0": "",
            "field_audience_0": "external", "field_approved_0": "1", "field_source_0": "",
            "field_label_1": "", "field_value_1": "", "field_unit_1": "",
            "field_audience_1": "external", "field_approved_1": "1", "field_source_1": "",
            "action": "save_draft",
        })
        self.assertEqual(code, 303)
        draft = self.db.get_knowledge_draft_version(TENANT, item["id"])
        snapshot = json.loads(draft["content_json"])
        self.assertIn("fields", snapshot, "删除全部要点必须显式保存 fields=[]（不能省略，否则会合并保留旧要点）")
        self.assertEqual(snapshot["fields"], [])
        # 再次发布 + 真实准备：客户证据不含旧要点，正文继续可用。
        code, _, _ = self.post(f"/admin/knowledge/item/{item['id']}/edit", {
            "title": "安装服务包", "body": "现场安装说明正文。", "usage": "external", "ai_authorized": "1",
            "field_label_0": "", "field_value_0": "",
            "action": "publish", "confirm_ai": "1",
        })
        self.assertEqual(code, 303)
        version2 = self.db.get_published_knowledge_version(TENANT, item["id"])
        self.assertNotEqual(version2["id"], version["id"])
        self.assertEqual(self.db.knowledge_processing_state(TENANT, version2["id"])["status"], "completed")
        self.assertEqual(json.loads(version2["content_json"])["fields"], [])
        texts = "\n".join(unit["text"] for unit in self.db.list_customer_qa_evidence(TENANT))
        self.assertNotIn("标准工期", texts, "再次发布后客户证据不得包含旧要点")
        self.assertNotIn("3 天", texts)
        self.assertNotIn("999", texts)
        self.assertIn("现场安装说明正文", texts)


class UserFacingLanguageTests(HttpBase):
    """清单 19：中文保存提示、负责人人名/邮箱、无实现术语。"""

    def test_saved_notice_is_chinese_and_owner_is_recognizable(self):
        code, headers, _ = self.post("/admin/knowledge/new", {
            "form": "article", "title": "售后政策", "body": "保修 24 个月。",
            "usage": "internal", "action": "save_draft",
        })
        self.assertEqual(code, 303)
        location = headers.get("Location", "")
        self.assertIn("notice=saved_draft", location)
        item = self.db.list_knowledge_items(TENANT)[0]
        _, _, page = self.get(location)
        self.assertNotIn("saved_draft", page, "详情不得直接显示英文状态代码")
        self.assertIn("草稿已保存", page)
        self.assertIn("演示管理员", page, "负责人显示企业内可识别的人名或邮箱")
        self.assertNotIn(item["owner_user_id"], page, "不得显示内部用户 ID")
        _, _, page = self.get("/admin/knowledge?view=all")
        self.assertIn("负责人：演示管理员", page)

    def test_no_developer_jargon_in_first_batch_ui(self):
        item_id = self.create_article()
        self.publish_prepared(item_id, confirm_ai=True)
        for path in ("/admin/knowledge", f"/admin/knowledge/item/{item_id}"):
            _, _, page = self.get(path)
            for token in ("检索前排除", "远程索引删除"):
                self.assertNotIn(token, page, path)
        code, headers, _ = self.post(f"/admin/knowledge/item/{item_id}/disable", {"confirmed": "1"})
        self.assertEqual(code, 303)
        _, _, page = self.get(headers["Location"])
        self.assertIn("已停用", page)
        self.assertIn("客户问答", page)
        for token in ("检索前排除", "远程索引删除", "从客户问答与检索中排除"):
            self.assertNotIn(token, page)


class ConfirmDialogMarkupTests(HttpBase):
    """站内确认对话框与显式确认字段：无原生 confirm；字段初始为空（由确认动作写入）；
    服务端校验保持强制（不带确认字段的直接 POST 不执行动作）。"""

    def test_pages_have_in_page_confirm_dialog_and_no_native_confirm(self):
        # 编辑器（发布按钮）与草稿详情（发布动作）。
        draft_id = self.create_article(usage="external", ai_authorized=True)
        for path in ("/admin/knowledge/new?form=article", f"/admin/knowledge/item/{draft_id}"):
            _, _, page = self.get(path)
            self.assertIn("id='kb-confirm-dlg'", page, path)
            self.assertIn("id='kb-confirm-title'", page)
            self.assertIn("data-kb-confirm-act='cancel'", page)
            self.assertIn("data-kb-confirm-act='ok'", page)
            self.assertNotIn("window.confirm", page)
            self.assertNotIn("data-confirm=", page)
        _, _, page = self.get(f"/admin/knowledge/item/{draft_id}")
        self.assertIn("data-kb-confirm-label='确认发布'", page)
        self.assertIn("name='confirmed' value=''", page, "确认字段初始为空：直接 POST 不带确认字段不会发布")
        self.assertNotIn("name='confirmed' value='1'", page)
        # 已发布条目：停用走 danger 对话框。
        published_id = self.create_article(title="对外政策", usage="external", ai_authorized=True)
        self.publish_prepared(published_id, confirm_ai=True)
        _, _, page = self.get(f"/admin/knowledge/item/{published_id}")
        self.assertIn("data-kb-confirm-tone='danger'", page)
        self.assertIn("data-kb-confirm-label='停用'", page)
        self.assertIn("name='confirmed' value=''", page)
        # 产品页（开启 AI 授权）。
        product_id, _ = self.make_published_product()
        _, _, page = self.get(f"/admin/knowledge/product/{product_id}")
        self.assertIn("id='kb-confirm-dlg'", page)
        self.assertIn("data-kb-product-auth", page)
        self.assertIn("data-kb-confirm-label='开启授权'", page)
        self.assertNotIn("window.confirm", page)

    def test_item_authorization_requires_explicit_confirmation_field(self):
        item_id = self.create_article(usage="external")
        self.publish_prepared(item_id, confirm_ai=True)
        # 已发布且对外的条目开启授权：不带确认字段的直接 POST 不生效（服务端强制）。
        code, headers, _ = self.post(f"/admin/knowledge/item/{item_id}/authorize", {"authorized": "1"})
        self.assertEqual(code, 303)
        self.assertIn("error=", headers.get("Location", ""))
        self.assertEqual(self.db.get_published_knowledge_version(TENANT, item_id)["ai_authorized"], 0)
        # 站内对话框确认后由 JS 写入的显式字段：授权生效。
        code, _, _ = self.post(f"/admin/knowledge/item/{item_id}/authorize",
                               {"authorized": "1", "confirmed": "1"})
        self.assertEqual(code, 303)
        self.assertEqual(self.db.get_published_knowledge_version(TENANT, item_id)["ai_authorized"], 1)


class ConfirmDialogDynamicTests(unittest.TestCase):
    """确认对话框动态行为：Node + 迷你 DOM 驱动真实 KB_JS（Escape 取消、焦点恢复、
    确认后写入显式字段并重发提交；保存草稿不弹确认；产品授权仅在需要时弹窗）。"""

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

    def test_publish_confirm_dialog_flow(self):
        facts = self.run_harness()["scenarios"]["saveFlow"]
        self.assertTrue(facts["saveDraftNoDialog"], "日常保存草稿不弹确认")
        self.assertTrue(facts["publishDialogOpened"], "对外+授权发布先弹站内对话框")
        self.assertTrue(facts["publishDialogPrevented"], "弹窗时阻止原提交，等待确认")
        self.assertIn("发布", facts["publishDialogTitle"])
        self.assertIn("AI", facts["publishDialogMessage"])
        self.assertIn("确认", facts["publishDialogLabel"])
        # 取消：关闭、不提交、保留未保存保护、焦点回触发按钮、确认字段为空。
        self.assertTrue(facts["declineDialogClosed"])
        self.assertFalse(facts["declineResubmitted"], "取消不提交")
        self.assertEqual(facts["publishDeclinedConfirmField"], "", "取消不得写入 confirm_ai")
        self.assertTrue(facts["publishDeclinedKeepsDirty"], "取消后未保存标记保留（内容仍受保护）")
        self.assertTrue(facts["declineFocusRestored"], "取消后焦点回到触发按钮")
        # Escape = 取消。
        self.assertTrue(facts["escapeDialogOpened"])
        self.assertTrue(facts["escapeClosed"])
        self.assertFalse(facts["escapeResubmitted"], "Escape 取消不提交")
        self.assertTrue(facts["escapeFocusRestored"])
        # 确认：写入 confirm_ai=1、重发提交带原 button、清除未保存标记。
        self.assertTrue(facts["publishDialogAcceptedClosed"])
        self.assertEqual(facts["publishAcceptedConfirmField"], "1", "确认发布写入显式确认字段")
        self.assertEqual(facts["publishAcceptedSubmitter"], "action=publish", "重发提交保留发布按钮")
        self.assertTrue(facts["publishAcceptedClearsDirty"], "确认发布路径同样清除未保存标记")
        self.assertFalse(facts["publishAcceptedSecondDialog"], "已确认的重发提交不再弹窗（不循环）")
        self.assertTrue(facts["publishResubmitNotPrevented"])
        self.assertTrue(facts["publishDialogReopensForNewAttempt"], "下一次发布尝试重新弹窗")

    def test_product_auth_confirm_dialog_flow(self):
        facts = self.run_harness()["scenarios"]["productConfirm"]
        self.assertTrue(facts["noDialogWhenNotNeeded"], "无需确认的保存不弹窗")
        self.assertTrue(facts["noDialogWhenTurningOff"], "关闭授权无需确认")
        self.assertTrue(facts["dialogOpened"], "已发布产品开启授权先弹站内对话框")
        self.assertIn("AI", facts["dialogTitle"])
        self.assertTrue(facts["declineClosed"])
        self.assertTrue(facts["declineClearsConfirmedField"], "取消不得写入 confirmed")
        self.assertTrue(facts["declineResubmitted"] is False, "取消不提交")
        self.assertTrue(facts["declineFocusRestored"], "取消后焦点回到触发按钮")
        self.assertEqual(facts["confirmFieldAfterAccept"], "1", "确认开启写入显式确认字段")
        self.assertTrue(facts["resubmittedAfterAccept"], "确认后重发提交")


if __name__ == "__main__":
    unittest.main()
