"""企业知识第二批 · 任务5：对外 AI 证据闸门（主题逐段过滤 / 字段闸门；模型前后复核）。

对应用户 2026-10-12/13 对任务5的要求与计划 checklist：
- 主题授权不能绕过来源限制：来源为仅内部 / 未核对 / 撤权 / 到期 / 换版（替换）的段落
  即使主题「对外 + AI 开」也不进入候选；混合主题只返回合格段；
- 来源钉住不静默换版：资料发新版后旧出处段按不合格过滤，须成员显式重新核对；
- 灵活字段：已核对 + 允许客户引用 + 有值（0/否有效）+ 有来源 才进入候选；
- 模型之前本地过滤 + 模型之后复核（生成期间撤权/换版/删改 → 拒绝该证据）；
- 可控 stub 模型验证调用前后过滤与引用身份（服务端精确原文与真实版本引用）；
  真实模型联调（B5）仍待验证，规则模式明确未连接。
只用临时数据库与测试私有上传目录；不触碰 data/ 下真实演示数据与任何密钥；不接真实模型。
"""
import json
import os
import tempfile
import unittest
from pathlib import Path

import ai_client
from ai_client import AIConfig
from app import Database, answer_knowledge_question, answer_question_prefer_dify

TENANT = "demo-tenant"


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


def choose_first(ai, evidence):
    if evidence:
        ai.selection = evidence[0]["key"]


def choose_source(source_kind):
    """选择指定来源类型的证据（服务端键由送选清单分配）。"""

    def pick(ai, evidence):
        for item in evidence:
            if item["evidence"].get("source") == source_kind:
                ai.selection = item["key"]
                return
        ai.selection = None
    return pick


class AiGateBase(unittest.TestCase):
    maxDiff = None

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.upload_dir = Path(self.tmp.name) / "uploads"
        os.environ["B2B_KB_STORAGE_ROOT"] = str(self.upload_dir)
        self.db = Database(Path(self.tmp.name) / "phase2-aigates.db")
        self.db.knowledge_storage_root = self.upload_dir
        self.db.seed_demo_users()
        self.admin = self.db.authenticate_user("demo-admin@example.invalid", "demo-admin")

    def tearDown(self):
        self.db.conn.close()
        os.environ.pop("B2B_KB_STORAGE_ROOT", None)
        self.tmp.cleanup()

    # ── 夹具 ──────────────────────────────────────────────────────────────

    def ingest_txt(self, filename="file.txt", text="保修24个月。"):
        batch_id = self.db.kb2_create_batch(
            TENANT, self.admin["id"], [{"kind": "file", "filename": filename, "data": text.encode()}])
        return batch_id, self.db.kb2_get_batch(TENANT, batch_id)["sources"][0]

    def check_all(self, source_id):
        for row in self.db.conn.execute(
                "SELECT id FROM kb2_paragraphs WHERE tenant_id=? AND source_id=?",
                (TENANT, source_id)).fetchall():
            self.db.kb2_update_paragraph(TENANT, self.admin["id"], row["id"], checked=True)

    def publish_item(self, batch_id, *, usage="external", ai_authorized=True):
        result = self.db.kb2_publish_batch(TENANT, self.admin["id"], batch_id, usage=usage,
                                           ai_authorized=ai_authorized, confirm_ai=True)
        self.assertFalse(result["failed"], result["failed"])

    def build_topic_from(self, item_id, *, publish=True, usage="external", ai_authorized=True):
        topic_id = self.db.kb2_create_topic(TENANT, self.admin["id"], f"主题-{item_id[:6]}")
        self.db.kb2_set_relation(TENANT, self.admin["id"], topic_id, {"kind": "knowledge", "id": item_id})
        self.db.kb2_build_topic_draft(TENANT, self.admin["id"], topic_id)
        paragraphs = [dict(p) for p in self.db.kb2_get_topic(TENANT, topic_id)["content"]["paragraphs"]]
        for paragraph in paragraphs:
            paragraph["checked"] = True
        self.db.kb2_save_topic_draft(TENANT, self.admin["id"], topic_id, paragraphs=paragraphs,
                                     usage=usage, ai_authorized=ai_authorized)
        if publish:
            self.db.kb2_publish_topic_draft(TENANT, self.admin["id"], topic_id, confirm_ai=True)
        return topic_id, paragraphs

    def topic_evidence(self):
        return [unit for unit in self.db.list_customer_qa_evidence(TENANT) if unit["source"] == "topic"]

    def field_evidence(self):
        return [unit for unit in self.db.list_customer_qa_evidence(TENANT) if unit["source"] == "item_field"]

    # ── 主题逐段过滤 ──────────────────────────────────────────────────────

class TopicEvidenceFilterTests(AiGateBase):
    def test_eligible_topic_paragraphs_only_after_item_and_topic_authorization(self):
        """主题与来源均对外+授权后，已核对段落进入候选；来源仅内部 / 主题未授权不进入。"""
        internal_batch, internal_source = self.ingest_txt("internal.txt", "内部内容。")
        self.check_all(internal_source["id"])
        self.publish_item(internal_batch, usage="internal", ai_authorized=True)
        internal_topic, _ = self.build_topic_from(internal_source["item_id"])
        state = self.db.kb2_topic_ai_state(TENANT, internal_topic)
        self.assertFalse(state["available"])
        self.assertIn("来源资料仅内部使用", state["reason"])
        self.assertEqual(self.topic_evidence(), [])
        # 对外+授权资料：段落进入候选。
        external_batch, external_source = self.ingest_txt("external.txt", "对外内容。")
        self.check_all(external_source["id"])
        self.publish_item(external_batch, usage="external", ai_authorized=True)
        topic_id, _ = self.build_topic_from(external_source["item_id"])
        evidence = self.topic_evidence()
        self.assertEqual([unit["text"] for unit in evidence], ["对外内容。"])
        self.assertEqual(evidence[0]["topic_id"], topic_id)
        self.assertEqual(evidence[0]["paragraph_index"], 1)
        self.assertTrue(evidence[0]["source_label"])
        # 主题未授权 AI：段落在但主题整体不进入候选。
        unauthorized_topic, _ = self.build_topic_from(external_source["item_id"], ai_authorized=False)
        self.assertNotIn(unauthorized_topic, {unit["topic_id"] for unit in self.topic_evidence()})
        self.assertIn("未授权 AI", self.db.kb2_topic_ai_state(TENANT, unauthorized_topic)["reason"])

    def test_unchecked_paragraph_and_internal_or_unauthorized_topic_are_excluded(self):
        """未核对段落不进入候选（发布闸门之外的防御性复核）；主题「仅内部」或未授权整条不进入。"""
        batch_id, source = self.ingest_txt()
        self.check_all(source["id"])
        self.publish_item(batch_id)
        item_id = source["item_id"]
        topic_id, _ = self.build_topic_from(item_id)
        self.assertEqual(len(self.topic_evidence()), 1)
        # 数据层模拟历史发布段落被就地标记未核对（防御性闸门验证）。
        reference = self.db._kb2_topic_reference_version(TENANT, topic_id)
        content = json.loads(reference["content_json"])
        content["paragraphs"][0]["checked"] = False
        self.db.conn.execute("UPDATE kb2_topic_versions SET content_json=? WHERE id=?",
                             (json.dumps(content, ensure_ascii=False), reference["id"]))
        self.db.conn.commit()
        self.assertEqual(self.topic_evidence(), [])
        state = self.db.kb2_topic_ai_state(TENANT, topic_id)
        self.assertIn("未核对", state["reason"])

    def test_mixed_topic_returns_only_eligible_paragraphs(self):
        """混合主题（合格段 + 来源不合格段）：只返回合格段。"""
        good_batch, good_source = self.ingest_txt("good.txt", "合格内容。")
        self.check_all(good_source["id"])
        self.publish_item(good_batch)
        # 主题混合：一段引用合格资料，一段为成员人工撰写，另一段引用仅内部资料。
        internal_batch, internal_source = self.ingest_txt("internal.txt", "内部内容。")
        self.check_all(internal_source["id"])
        self.publish_item(internal_batch, usage="internal", ai_authorized=True)
        item_id = good_source["item_id"]
        topic_id, _ = self.build_topic_from(item_id)
        # 追加两段：人工撰写 + 仅内部资料的摘录（构建时该资料已发布，但仅内部→候选过滤）。
        self.db.kb2_set_relation(TENANT, self.admin["id"], topic_id,
                                 {"kind": "knowledge", "id": internal_source["item_id"]})
        self.db.kb2_build_topic_draft(TENANT, self.admin["id"], topic_id)
        paragraphs = [dict(p) for p in self.db.kb2_get_topic(TENANT, topic_id)["content"]["paragraphs"]]
        self.assertEqual(len(paragraphs), 2)
        for paragraph in paragraphs:
            paragraph["checked"] = True
        paragraphs.append({"id": "manual-1", "kind": "point", "text": "成员撰写。", "origin": "manual",
                           "checked": True, "source": {"kind": "manual"}})
        self.db.kb2_save_topic_draft(TENANT, self.admin["id"], topic_id, paragraphs=paragraphs)
        self.db.kb2_publish_topic_draft(TENANT, self.admin["id"], topic_id, confirm_ai=True)
        evidence = self.topic_evidence()
        texts = sorted(unit["text"] for unit in evidence)
        self.assertEqual(texts, ["合格内容。", "成员撰写。"], "只保留合格段与人工撰写段")
        state = self.db.kb2_topic_ai_state(TENANT, topic_id)
        self.assertTrue(state["available"])
        self.assertEqual(state["eligible_paragraphs"], 2)
        self.assertEqual(state["total_paragraphs"], 3)

    def test_paragraph_pin_never_silently_upgrades_after_new_material_version(self):
        """来源资料发新版后：旧出处段按不合格过滤（不静默换版），成员显式重新核对后恢复候选。"""
        batch_id, source = self.ingest_txt(text="保修24个月。")
        self.check_all(source["id"])
        item_id = source["item_id"]
        self.publish_item(batch_id)
        topic_id, _ = self.build_topic_from(item_id)
        self.assertEqual(len(self.topic_evidence()), 1)
        # 资料编辑（新增一行，原文字仍在）并发布新版：旧出处的段落被过滤（不会静默指向新版本）。
        # B2-B09：通过整理路径补人工正文并核对后发布（正文与已核对出处保持一致）。
        added = self.db.kb2_add_manual_paragraph(TENANT, self.admin["id"], source["id"], "新增一行。")
        self.db.kb2_update_paragraph(TENANT, self.admin["id"], added, checked=True)
        self.db.publish_knowledge_draft(TENANT, item_id, self.admin["id"], confirm_ai=True)
        # 与第一批发布流程一致：发布后对新版本执行真实本地准备。
        self.db.prepare_knowledge_version(
            TENANT, self.db.get_published_knowledge_version(TENANT, item_id)["id"])
        self.assertEqual(self.topic_evidence(), [])
        state = self.db.kb2_topic_ai_state(TENANT, topic_id)
        self.assertFalse(state["available"])
        self.assertIn("重新核对", state["reason"])
        # 成员显式重新核对来源（文字仍在当前资料中）→ 出处重钉到当前发布版本；发布主题新版本后恢复候选。
        paragraph = self.db.kb2_get_topic(TENANT, topic_id)["content"]["paragraphs"][0]
        renewed = self.db.kb2_renew_topic_paragraph_source(TENANT, self.admin["id"], topic_id, paragraph["id"])
        self.assertEqual(renewed["source"]["version_id"],
                         self.db.get_published_knowledge_version(TENANT, item_id)["id"])
        self.db.kb2_publish_topic_draft(TENANT, self.admin["id"], topic_id, confirm_ai=True)
        evidence = self.topic_evidence()
        self.assertEqual([unit["text"] for unit in evidence], ["保修24个月。"])

    def test_renew_cannot_pin_to_history_or_missing_text(self):
        """重新核对只在当前内容里找：文字已从当前资料消失时明确失败（不猜测出处）。"""
        batch_id, source = self.ingest_txt(text="保修24个月。")
        self.check_all(source["id"])
        item_id = source["item_id"]
        self.publish_item(batch_id)
        topic_id, _ = self.build_topic_from(item_id)
        # 资料新版本完全替换正文：旧文字只存在于历史版本。
        # B2-B09：改写收录段落文字并显式再核对后发布（正文与已核对出处保持一致）。
        row = self.db.conn.execute(
            "SELECT id FROM kb2_paragraphs WHERE tenant_id=? AND source_id=? ORDER BY ordinal LIMIT 1",
            (TENANT, source["id"])).fetchone()
        self.db.kb2_update_paragraph(TENANT, self.admin["id"], row["id"], text="保修36个月。")
        self.db.kb2_update_paragraph(TENANT, self.admin["id"], row["id"], checked=True)
        self.db.publish_knowledge_draft(TENANT, item_id, self.admin["id"], confirm_ai=True)
        paragraph = self.db.kb2_get_topic(TENANT, topic_id)["content"]["paragraphs"][0]
        with self.assertRaisesRegex(ValueError, "找不到"):
            self.db.kb2_renew_topic_paragraph_source(TENANT, self.admin["id"], topic_id, paragraph["id"])

    def test_after_model_catches_topic_expiry(self):
        """B2-B07：模型后复核必须校验主题自身到期（生成期间到期 → 拒绝旧证据）。"""
        from datetime import datetime, timedelta, timezone
        now = datetime.now(timezone.utc)
        expiry = (now + timedelta(minutes=2)).replace(second=0, microsecond=0)
        topic_id = self.db.kb2_create_topic(TENANT, self.admin["id"], "到期主题")
        self.db.kb2_save_topic_draft(
            TENANT, self.admin["id"], topic_id,
            paragraphs=[{"id": "p1", "kind": "intro", "text": "对外事实。", "origin": "manual",
                         "checked": True, "source": {"kind": "manual"}}],
            usage="external", ai_authorized=True,
            expires_at=expiry.astimezone(timezone(timedelta(hours=8))).strftime("%Y-%m-%dT%H:%M"))
        self.db.kb2_publish_topic_draft(TENANT, self.admin["id"], topic_id, confirm_ai=True)
        evidence = self.db.kb2_list_topic_qa_evidence(TENANT, now=now)
        self.assertEqual(len(evidence), 1)
        ok, _ = self.db.knowledge_evidence_valid_after_model(TENANT, evidence[0], now=now)
        self.assertTrue(ok)
        after = expiry + timedelta(seconds=1)
        # 到期后候选列表为空；此前已生成的证据在模型后复核也必须被拒。
        self.assertEqual(self.db.kb2_list_topic_qa_evidence(TENANT, now=after), [])
        ok, reason = self.db.knowledge_evidence_valid_after_model(TENANT, evidence[0], now=after)
        self.assertFalse(ok)
        self.assertIn("到期", reason)

    def test_registered_only_source_never_available_for_ai(self):
        """仅登记原件（没有可核对正文）不供 AI：即使被手工钉住也按不合格过滤。"""
        batch_id, source = self.ingest_txt()
        self.check_all(source["id"])
        self.publish_item(batch_id)
        item_id = source["item_id"]
        topic_id, _ = self.build_topic_from(item_id)
        # 数据层模拟「仅登记原件」的发布资料：有正文的条目改为登记标记（防御性闸门验证）。
        version = self.db.get_published_knowledge_version(TENANT, item_id)
        content = json.loads(version["content_json"])
        content["kb2_registered_only"] = True
        self.db.conn.execute("UPDATE knowledge_versions SET content_json=? WHERE id=?",
                             (json.dumps(content, ensure_ascii=False), version["id"]))
        self.db.conn.commit()
        evidence = self.topic_evidence()
        self.assertEqual(evidence, [])
        state = self.db.kb2_topic_ai_state(TENANT, topic_id)
        self.assertIn("仅登记原件", state["reason"])


class FieldEvidenceGateTests(AiGateBase):
    def make_item_with_fields(self, fields, values, *, publish=True):
        batch_id, source = self.ingest_txt(text="响应一小时内。\n支持定制。")
        self.check_all(source["id"])
        item_id = source["item_id"]
        template_version = self.db.kb2_save_template(
            TENANT, self.admin["id"], None, fields, name="服务方案")
        self.db.kb2_adopt_template(TENANT, self.admin["id"], item_id, template_version, confirmed=True)
        for key, value, extra in values:
            self.db.kb2_set_item_field(TENANT, self.admin["id"], item_id, key, value=value,
                                       source={"kind": "manual"}, **extra)
        if publish:
            self.publish_item(batch_id)
        return item_id, batch_id

    def test_zero_and_false_are_valid_evidence_with_gates(self):
        """0 与「否」是有效值可进入候选；未核对 / 不允许客户引用 / 无来源 / 未填 被排除。"""
        item_id, _ = self.make_item_with_fields(
            [
                {"label": "响应时间", "type": "number", "unit": "小时", "key": "resp"},
                {"label": "支持定制", "type": "bool", "key": "custom"},
                {"label": "内部口径", "type": "text", "key": "internal"},
                {"label": "未核对字段", "type": "text", "key": "unchecked"},
                {"label": "未填字段", "type": "text", "key": "empty"},
            ],
            [
                ("resp", "0", {"checked": True, "allow_customer": True}),
                ("custom", False, {"checked": True, "allow_customer": True}),
                ("internal", "内部内容", {"checked": True, "allow_customer": False}),
                ("unchecked", "未核对内容", {"allow_customer": True}),
                ("empty", None, {"checked": True, "allow_customer": True}),
            ])
        evidence = self.field_evidence()
        by_key = {unit["field_key"]: unit for unit in evidence}
        self.assertEqual(sorted(by_key), ["custom", "resp"], "只保留核对+允许客户+有值+有来源的字段")
        self.assertEqual(by_key["resp"]["value"], "0")
        self.assertEqual(by_key["resp"]["text"], "响应时间：0 小时")
        self.assertEqual(by_key["custom"]["value"], "false")
        self.assertEqual(by_key["custom"]["text"], "支持定制：false")

    def test_field_change_after_publication_blocks_old_evidence(self):
        """生成期间/发布后字段值变化：旧快照证据在模型之后复核被拒。"""
        item_id, _ = self.make_item_with_fields(
            [{"label": "响应时间", "type": "number", "unit": "小时", "key": "resp"}],
            [("resp", "1", {"checked": True, "allow_customer": True})])
        evidence = self.field_evidence()
        self.assertEqual(len(evidence), 1)
        ok, _ = self.db.kb2_evidence_valid_after_model(TENANT, evidence[0])
        self.assertTrue(ok)
        # 编辑字段值并发布新版 → 旧证据失效。
        self.db.kb2_set_item_field(TENANT, self.admin["id"], item_id, "resp", value="2")
        self.db.kb2_set_item_field(TENANT, self.admin["id"], item_id, "resp", checked=True, allow_customer=True)
        self.db.publish_knowledge_draft(TENANT, item_id, self.admin["id"], confirm_ai=True)
        ok, reason = self.db.kb2_evidence_valid_after_model(TENANT, evidence[0])
        self.assertFalse(ok)
        self.assertIn("变化", reason)


class StubModelGateTests(AiGateBase):
    """可控 stub 模型：验证调用前后的过滤与引用身份（服务端生成原文与真实版本）。"""

    def test_stub_model_only_receives_eligible_evidence_and_citation_is_exact(self):
        batch_id, source = self.ingest_txt(text="保修24个月。")
        self.check_all(source["id"])
        item_id = source["item_id"]
        self.publish_item(batch_id)
        topic_id, _ = self.build_topic_from(item_id)
        # 附加一条仅内部来源的主题（不应送模型）。
        internal_batch, internal_source = self.ingest_txt("internal.txt", "内部内容。")
        self.check_all(internal_source["id"])
        self.publish_item(internal_batch, usage="internal", ai_authorized=True)
        internal_topic, _ = self.build_topic_from(internal_source["item_id"])
        stub = StubKnowledgeAI(on_call=choose_source("topic"))
        outcome = answer_knowledge_question(self.db, stub, TENANT, "", "保修多久？")
        self.assertEqual(outcome["status"], "grounded")
        payload = json.dumps(stub.calls[0]["evidence"], ensure_ascii=False)
        self.assertNotIn("内部内容", payload)
        self.assertIn("保修24个月。", payload)
        citation = outcome["citations"][0]
        self.assertEqual(citation["kind"], "topic")
        self.assertEqual(citation["quote"], "保修24个月。")
        self.assertEqual(citation["topic_id"], topic_id)
        self.assertTrue(citation["version"].startswith("主题 v"))
        self.assertNotEqual(internal_topic, citation["topic_id"])

    def test_after_model_revocation_blocks_topic_evidence(self):
        """模型等待期间撤权 → 旧证据被拦（按资料不足处理）。"""
        batch_id, source = self.ingest_txt()
        self.check_all(source["id"])
        item_id = source["item_id"]
        self.publish_item(batch_id)
        topic_id, _ = self.build_topic_from(item_id)

        def revoke(ai, evidence):
            choose_source("topic")(ai, evidence)
            self.db.disable_knowledge_item(TENANT, item_id, self.admin["id"], confirmed=True)

        stub = StubKnowledgeAI(on_call=revoke)
        outcome = answer_knowledge_question(self.db, stub, TENANT, "", "保修多久？")
        self.assertEqual(outcome["status"], "no_evidence")
        self.assertIn("失效", outcome["reason"])
        # 恢复启用（对外+授权需确认）后重新可用。
        self.db.enable_knowledge_item(TENANT, item_id, self.admin["id"], confirmed=True)
        stub2 = StubKnowledgeAI(on_call=choose_source("topic"))
        outcome2 = answer_knowledge_question(self.db, stub2, TENANT, "", "保修多久？")
        self.assertEqual(outcome2["status"], "grounded")

    def test_after_model_version_replacement_blocks_old_topic_evidence(self):
        """模型等待期间来源发新版 → 旧出处的证据被拦。"""
        batch_id, source = self.ingest_txt(text="保修24个月。")
        self.check_all(source["id"])
        item_id = source["item_id"]
        self.publish_item(batch_id)
        self.build_topic_from(item_id)

        def replace_version(ai, evidence):
            choose_source("topic")(ai, evidence)
            # B2-B09：改写收录段落并显式再核对后发布新版（正文与已核对出处保持一致）。
            row = self.db.conn.execute(
                "SELECT id FROM kb2_paragraphs WHERE tenant_id=? AND source_id=? ORDER BY ordinal LIMIT 1",
                (TENANT, source["id"])).fetchone()
            self.db.kb2_update_paragraph(TENANT, self.admin["id"], row["id"], text="保修36个月。")
            self.db.kb2_update_paragraph(TENANT, self.admin["id"], row["id"], checked=True)
            self.db.publish_knowledge_draft(TENANT, item_id, self.admin["id"], confirm_ai=True)

        stub = StubKnowledgeAI(on_call=replace_version)
        outcome = answer_knowledge_question(self.db, stub, TENANT, "", "保修多久？")
        self.assertEqual(outcome["status"], "no_evidence")
        self.assertIn("失效", outcome["reason"])

    def test_stub_model_field_citation_identity(self):
        """字段证据的引用身份：服务端原文 + 真实版本；0/否有效。"""
        item_id, _ = FieldEvidenceGateTests.make_item_with_fields(
            self, [{"label": "支持定制", "type": "bool", "key": "custom"}],
            [("custom", False, {"checked": True, "allow_customer": True})])
        stub = StubKnowledgeAI(on_call=choose_source("item_field"))
        outcome = answer_knowledge_question(self.db, stub, TENANT, "", "支持定制吗？")
        self.assertEqual(outcome["status"], "grounded")
        citation = outcome["citations"][0]
        self.assertEqual(citation["kind"], "item_field")
        self.assertEqual(citation["quote"], "支持定制：false")
        self.assertEqual(citation["item_id"], item_id)


class UnifiedEntryCandidateTests(AiGateBase):
    """B2-B12：网页/渠道统一问答入口按合格主题 / 知识 / 字段判断候选（不能只看旧知识列表）。

    对应独立复现 `/tmp/b2b-phase2-unified-qa-review.py`：只有合格对外主题的企业，
    直接知识问答 grounded，但共享入口（answer_question_prefer_dify）提前返回 needs_human、模型调用 0。
    """

    def test_unified_entry_reads_topic_only_tenant(self):
        topic_id = self.db.kb2_create_topic(TENANT, self.admin["id"], "Customer service")
        self.db.kb2_save_topic_draft(
            TENANT, self.admin["id"], topic_id,
            paragraphs=[{"id": "p1", "text": "Service inquiries are answered within two business days.",
                         "origin": "manual", "checked": True, "source": {"kind": "manual"}}],
            usage="external", ai_authorized=True)
        self.db.kb2_publish_topic_draft(TENANT, self.admin["id"], topic_id, confirm_ai=True)
        self.assertEqual(len(self.db.kb2_list_topic_qa_evidence(TENANT)), 1)
        self.assertEqual(len(self.db.list_ai_answerable_knowledge(TENANT)), 0)
        stub = StubKnowledgeAI(on_call=choose_source("topic"))
        status, answer, product_id, version_id, citations, reason, source = answer_question_prefer_dify(
            self.db, stub, TENANT, "", "When is the response?", None, "QA")
        self.assertEqual(status, "grounded", reason)
        self.assertEqual(source, "knowledge")
        self.assertEqual(len(stub.calls), 1, "主题候选必须交给模型选择（不能提前判定无候选）")
        self.assertEqual(citations[0]["kind"], "topic")
        self.assertEqual(citations[0]["topic_id"], topic_id)

    def test_unified_entry_without_any_candidate_keeps_local_and_skips_model(self):
        """没有任何合格候选时保持既有行为：不进入知识问答、不做多余模型调用。"""
        stub = StubKnowledgeAI()
        status, answer, product_id, version_id, citations, reason, source = answer_question_prefer_dify(
            self.db, stub, TENANT, "", "Any answer?", None, "QA")
        self.assertEqual(len(stub.calls), 0, "无候选时不应调用模型")
        self.assertEqual(source, "local")


if __name__ == "__main__":
    unittest.main()
