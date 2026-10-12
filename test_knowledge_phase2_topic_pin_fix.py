"""企业知识第二批 · B2-B03 阻断修复回归：主题出处钉住与核对闸门（判别性用例）。

对应独立复核（/tmp/b2b-phase2-topic-pin-review-evidence.md）指出的三类阻断与用户修复要求：
1. 资料摘录段落必须钉住「确切版本 + 登记原件 + 实际位置」：缺项、仅资料 ID、
   同资料错配来源/版本、不存在的页码/行号 一律拒绝（不能把不存在的证据写成出处）；
2. 编辑已有段落文字 → 撤销该段核对并标注「人工改写」：提交的旧 checked=true/origin=from_material
   不能绕过；显式再次核对后才可发布；
3. 人工撰写保持人工撰写：不能冒用资料出处；产品只读适配保持兼容（不影响既有产品身份）；
4. 出处一致性：整理初稿不把新草稿文字挂到旧发布版本、不把所有原件都挂到一个当前版本；
   每个原件按自己真实所属版本钉住；出处不同版本不并作重复；来源更新不静默改现有段落与关联钉住，
   由成员显式「重新核对来源 / 重新钉住」完成（human reconfirmation flow）。

只用临时数据库与测试私有上传目录；不触碰 data/ 下真实演示数据与任何密钥；不接真实模型。
运行环境：系统 python3 与 .venv-knowledge 均可（全部用 TXT 夹具）。
"""
import json
import os
import tempfile
import unittest
from pathlib import Path

from app import Database

TENANT = "demo-tenant"
OTHER = "other-tenant"


class TopicPinFixBase(unittest.TestCase):
    maxDiff = None

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.upload_dir = Path(self.tmp.name) / "uploads"
        os.environ["B2B_KB_STORAGE_ROOT"] = str(self.upload_dir)
        self.db = Database(Path(self.tmp.name) / "phase2-pin.db")
        self.db.knowledge_storage_root = self.upload_dir
        self.db.seed_demo_users()
        self.admin = self.db.authenticate_user("demo-admin@example.invalid", "demo-admin")

    def tearDown(self):
        self.db.conn.close()
        os.environ.pop("B2B_KB_STORAGE_ROOT", None)
        self.tmp.cleanup()

    # ── 夹具 ──────────────────────────────────────────────────────────────

    def ingest_txt(self, filename="warranty.txt", text="保修24个月。", *, check=True):
        batch_id = self.db.kb2_create_batch(
            TENANT, self.admin["id"], [{"kind": "file", "filename": filename, "data": text.encode()}])
        source = self.db.kb2_get_batch(TENANT, batch_id)["sources"][0]
        ws_id = self.db.conn.execute(
            "SELECT knowledge_source_id FROM kb2_sources WHERE id=?", (source["id"],)).fetchone()[0]
        if check:
            for row in self.db.conn.execute(
                    "SELECT id FROM kb2_paragraphs WHERE tenant_id=? AND source_id=? ORDER BY ordinal",
                    (TENANT, source["id"])).fetchall():
                self.db.kb2_update_paragraph(TENANT, self.admin["id"], row["id"], checked=True)
        version = self.db.get_knowledge_draft_version(TENANT, source["item_id"])
        return {"source": source, "ws_id": ws_id, "item_id": source["item_id"], "version_id": version["id"]}

    def full_pin(self, fixture, text="保修24个月。", *, location=None, checked=True, origin="from_material"):
        return {
            "id": "p1", "kind": "intro", "text": text, "origin": origin, "checked": checked,
            "source": {
                "kind": "knowledge", "item_id": fixture["item_id"], "version_id": fixture["version_id"],
                "source_id": fixture["ws_id"], "location": location or {"kind": "line", "line": 1},
            },
        }

    def try_save(self, paragraphs, topic_title="出处测试"):
        topic_id = self.db.kb2_create_topic(TENANT, self.admin["id"], topic_title)
        try:
            self.db.kb2_save_topic_draft(TENANT, self.admin["id"], topic_id, paragraphs=paragraphs)
            self.db.kb2_publish_topic_draft(TENANT, self.admin["id"], topic_id)
            return topic_id, None
        except (ValueError, PermissionError) as error:
            return topic_id, str(error)

    def stored_paragraph(self, topic_id):
        content = json.loads(self.db.conn.execute(
            "SELECT content_json FROM kb2_topic_versions WHERE topic_id=? AND status='draft'",
            (topic_id,)).fetchone()[0])
        return content["paragraphs"][0]


class ExactPinRequiredTests(TopicPinFixBase):
    def test_complete_pin_is_accepted_and_published(self):
        """对照组：完整出处（版本+原件+实际位置）可以保存并发布。"""
        fixture = self.ingest_txt()
        _, error = self.try_save([self.full_pin(fixture)])
        self.assertIsNone(error, error)

    def test_missing_version_source_location_or_item_rejected(self):
        """缺版本 / 缺原件 / 缺位置 / 只有资料 ID：一律拒绝（不能只写资料 ID 当出处）。"""
        fixture = self.ingest_txt()
        for missing in ("version_id", "source_id", "location"):
            paragraph = self.full_pin(fixture)
            paragraph["source"].pop(missing)
            _, error = self.try_save([paragraph], topic_title=f"缺{missing}")
            self.assertIsNotNone(error, f"缺少 {missing} 不应通过")
        paragraph = self.full_pin(fixture)
        paragraph["source"] = {"kind": "knowledge", "item_id": fixture["item_id"]}
        _, error = self.try_save([paragraph], topic_title="仅资料ID")
        self.assertIsNotNone(error)

    def test_mismatched_source_version_pair_rejected(self):
        """同一资料下，来源不是该版本关联的原件 → 拒绝（错配的来源/版本不能混用）。"""
        fixture = self.ingest_txt()
        other_source = self.db.create_knowledge_source(
            TENANT, fixture["item_id"], self.admin["id"], source_location="同条目另一登记")
        paragraph = self.full_pin(fixture)
        paragraph["source"]["source_id"] = other_source
        _, error = self.try_save([paragraph], topic_title="错配来源")
        self.assertIsNotNone(error)
        self.assertIn("不匹配", error)

    def test_foreign_item_and_tenant_pins_rejected(self):
        """其他资料 / 其他企业的版本与原件继续拒绝（保持 B2-B03 已验证边界）。"""
        fixture = self.ingest_txt()
        other_item = self.db.create_knowledge_item(
            TENANT, "article", "另一资料", self.admin["id"], content={"body": "另一正文"})
        other_version = self.db.get_knowledge_draft_version(TENANT, other_item)["id"]
        other_source = self.db.create_knowledge_source(
            TENANT, other_item, self.admin["id"], source_location="另一资料来源")
        for field, value in (("version_id", other_version), ("source_id", other_source)):
            paragraph = self.full_pin(fixture)
            paragraph["source"][field] = value
            _, error = self.try_save([paragraph], topic_title=f"其他资料{field}")
            self.assertIsNotNone(error)
        self.db.ensure_tenant(OTHER, "其他企业（测试）")
        foreign_admin = self.db.create_user(OTHER, "other-admin@example.invalid", "对方管理员", "admin", "other-admin", "test")
        foreign_item = self.db.create_knowledge_item(OTHER, "article", "对方资料", foreign_admin, content={"body": "对方正文"})
        foreign_version = self.db.get_knowledge_draft_version(OTHER, foreign_item)["id"]
        foreign_source = self.db.create_knowledge_source(OTHER, foreign_item, foreign_admin, source_location="对方来源")
        for field, value in (("version_id", foreign_version), ("source_id", foreign_source)):
            paragraph = self.full_pin(fixture)
            paragraph["source"][field] = value
            _, error = self.try_save([paragraph], topic_title=f"跨企业{field}")
            self.assertIsNotNone(error)

    def test_fabricated_locations_rejected(self):
        """TXT 不许出现不存在的页码/行号；真实行号通过（实际内容核对）。"""
        fixture = self.ingest_txt(text="第一行。\n第二行。")
        for location in ({"kind": "page", "page": 999}, {"kind": "line", "line": 99}, {"kind": "paragraph", "paragraph": 3}):
            paragraph = self.full_pin(fixture, text="第一行。", location=location)
            _, error = self.try_save([paragraph], topic_title="伪造位置")
            self.assertIsNotNone(error, f"位置 {location} 不应通过")
        paragraph = self.full_pin(fixture, text="第二行。", location={"kind": "line", "line": 2})
        _, error = self.try_save([paragraph], topic_title="真实行号")
        self.assertIsNone(error, error)

    def test_text_must_match_pinned_location(self):
        """新段落声明 from_material 时，文字必须与所指位置的实际内容一致。"""
        fixture = self.ingest_txt(text="保修24个月。")
        paragraph = self.full_pin(fixture, text="保修240个月。")
        _, error = self.try_save([paragraph], topic_title="文字不符")
        self.assertIsNotNone(error)
        self.assertIn("不一致", error)

    def test_manual_writing_keeps_manual_and_cannot_claim_provenance(self):
        """人工撰写保持人工撰写：不能挂资料/产品出处；正常人工段落照常可发布。"""
        fixture = self.ingest_txt()
        paragraph = self.full_pin(fixture, origin="manual")
        _, error = self.try_save([paragraph], topic_title="人工冒用")
        self.assertIsNotNone(error)
        clean = {"id": "m1", "kind": "intro", "text": "成员撰写。", "origin": "manual",
                 "checked": True, "source": {"kind": "manual"}}
        _, error = self.try_save([clean], topic_title="纯人工")
        self.assertIsNone(error, error)

    def test_edited_paragraph_may_differ_but_keeps_valid_pin(self):
        """人工改写：文字可不同，但出处仍须是真实、配对的位置。"""
        fixture = self.ingest_txt()
        paragraph = self.full_pin(fixture, text="保修期为两年（成员改写）。", origin="edited")
        _, error = self.try_save([paragraph], topic_title="人工改写")
        self.assertIsNone(error, error)
        broken = self.full_pin(fixture, text="改写。", origin="edited", location={"kind": "line", "line": 99})
        _, error = self.try_save([broken], topic_title="改写坏出处")
        self.assertIsNotNone(error)

    def test_legacy_adapter_file_source_pin_uses_version_lines(self):
        """第一批适配来源（登记原件）：文本原件按版本正文行核对；不存在的页码/行号拒绝。"""
        imported = self.db.import_knowledge_file(
            TENANT, self.admin["id"], filename="报价.txt", data="第一行。\n第二行。".encode())
        item_id, source_id = imported["item_id"], imported["source_id"]
        self.db.publish_knowledge_draft(TENANT, item_id, self.admin["id"])
        version = self.db.get_published_knowledge_version(TENANT, item_id)

        def pin(text, location):
            return {"id": "lp1", "kind": "intro", "text": text, "origin": "from_material", "checked": True,
                    "source": {"kind": "knowledge", "item_id": item_id, "version_id": version["id"],
                               "source_id": source_id, "location": location}}

        _, error = self.try_save([pin("第二行。", {"kind": "line", "line": 2})], topic_title="适配来源行2")
        self.assertIsNone(error, error)
        _, error = self.try_save([pin("第一行。", {"kind": "page", "page": 1})], topic_title="适配来源页码")
        self.assertIsNotNone(error, "文本原件没有页码，不能作为位置")
        _, error = self.try_save([pin("第一行。", {"kind": "line", "line": 9})], topic_title="适配来源行9")
        self.assertIsNotNone(error)
        _, error = self.try_save([pin("不存在的文字。", {"kind": "line", "line": 1})], topic_title="适配来源文字不符")
        self.assertIsNotNone(error)

    def test_product_reference_adapters_remain_compatible(self):
        """旧产品身份保持：主题可继续引用产品（只读适配，不复制产品行）；人工段不能冒用产品。"""
        product_id = self.db.create_product(TENANT, "KB2-P1", "测试泵", "冷却水循环")
        paragraph = {"id": "pp1", "kind": "intro", "text": "选型参考。", "origin": "from_material",
                     "checked": True, "source": {"kind": "product", "product_id": product_id, "version_number": 1}}
        _, error = self.try_save([paragraph], topic_title="产品适配")
        self.assertIsNone(error, error)
        count = self.db.conn.execute("SELECT COUNT(*) FROM products").fetchone()[0]
        self.assertEqual(count, 1, "只读适配不复制产品行")
        fake_manual = {"id": "pp2", "kind": "intro", "text": "人工文字。", "origin": "manual",
                       "checked": True, "source": {"kind": "product", "product_id": product_id}}
        _, error = self.try_save([fake_manual], topic_title="人工冒用产品")
        self.assertIsNotNone(error)


class EditRevocationTests(TopicPinFixBase):
    def test_edit_revokes_checked_and_marks_rewrite_until_explicit_recheck(self):
        """已发布段落的文字被改：旧 checked=true/origin=from_material 不能绕过撤销；
        显式再次核对后才允许发布（随后再次编辑又会被撤销）。"""
        fixture = self.ingest_txt()
        topic_id, error = self.try_save([self.full_pin(fixture)])
        self.assertIsNone(error, error)
        # 改文字 + 原样提交 checked=true/origin=from_material（复核脚本的攻击载荷）。
        attack = self.full_pin(fixture, text="保修240个月。")
        self.db.kb2_save_topic_draft(TENANT, self.admin["id"], topic_id, paragraphs=[attack])
        stored = self.stored_paragraph(topic_id)
        self.assertEqual(stored["text"], "保修240个月。")
        self.assertFalse(stored["checked"], "编辑必须撤销该段核对")
        self.assertEqual(stored["origin"], "edited", "编辑必须标注人工改写")
        with self.assertRaisesRegex(ValueError, "未核对"):
            self.db.kb2_publish_topic_draft(TENANT, self.admin["id"], topic_id)
        # 显式再次核对（文字不再变化）→ 允许发布。
        self.db.kb2_save_topic_draft(TENANT, self.admin["id"], topic_id,
                                     paragraphs=[{**stored, "checked": True}])
        version = self.db.kb2_publish_topic_draft(TENANT, self.admin["id"], topic_id)
        self.assertEqual(version, 2)
        # 再编辑一次（同一草稿生命周期内）→ 又撤销。
        self.db.kb2_save_topic_draft(TENANT, self.admin["id"], topic_id,
                                     paragraphs=[{**stored, "text": "保修250个月。", "checked": True}])
        stored = self.stored_paragraph(topic_id)
        self.assertFalse(stored["checked"])
        self.assertEqual(stored["origin"], "edited")
        with self.assertRaisesRegex(ValueError, "未核对"):
            self.db.kb2_publish_topic_draft(TENANT, self.admin["id"], topic_id)

    def test_manual_paragraph_edit_revokes_checked_but_stays_manual(self):
        """人工段落改文字同样回到待核对，但不标成「改写自资料」。"""
        topic_id = self.db.kb2_create_topic(TENANT, self.admin["id"], "人工编辑")
        self.db.kb2_save_topic_draft(
            TENANT, self.admin["id"], topic_id,
            paragraphs=[{"id": "m1", "kind": "intro", "text": "初稿。", "origin": "manual",
                         "checked": True, "source": {"kind": "manual"}}])
        self.db.kb2_save_topic_draft(
            TENANT, self.admin["id"], topic_id,
            paragraphs=[{"id": "m1", "kind": "intro", "text": "修改后。", "origin": "manual",
                         "checked": True, "source": {"kind": "manual"}}])
        stored = self.stored_paragraph(topic_id)
        self.assertFalse(stored["checked"])
        self.assertEqual(stored["origin"], "manual")


class SourceDraftConsistencyTests(TopicPinFixBase):
    def publish_item(self, item_id):
        self.db.publish_knowledge_draft(TENANT, item_id, self.admin["id"])
        return self.db.get_published_knowledge_version(TENANT, item_id)

    def test_build_draft_pins_sources_own_version_not_old_published(self):
        """资料编辑产生新草稿后：新文字挂新草稿版本，旧发布版本下的旧段落不被静默改写。"""
        fixture = self.ingest_txt(text="第一行文字。\n第二行文字。")
        item_id = fixture["item_id"]
        v1 = self.publish_item(item_id)
        self.assertEqual(fixture["version_id"], v1["id"])
        topic_id = self.db.kb2_create_topic(TENANT, self.admin["id"], "一致性")
        self.db.kb2_set_relation(TENANT, self.admin["id"], topic_id, {"kind": "knowledge", "id": item_id})
        first = self.db.kb2_build_topic_draft(TENANT, self.admin["id"], topic_id)
        self.assertEqual(first["added"], 2)
        for paragraph in self.db.kb2_get_topic(TENANT, topic_id)["content"]["paragraphs"]:
            self.assertEqual(paragraph["source"]["version_id"], v1["id"], "发布后初稿钉住发布版本")
        # 成员编辑第一行（编辑撤销核对）→ 重新核对 → 产生新草稿版本 v2，正文更新。
        rows = self.db.conn.execute(
            "SELECT * FROM kb2_paragraphs WHERE tenant_id=? AND source_id=? ORDER BY ordinal",
            (TENANT, fixture["source"]["id"])).fetchall()
        self.db.kb2_update_paragraph(TENANT, self.admin["id"], rows[0]["id"], text="第一行文字（更新）。")
        self.db.kb2_update_paragraph(TENANT, self.admin["id"], rows[0]["id"], checked=True)
        v2 = self.db.get_knowledge_draft_version(TENANT, item_id)
        self.assertNotEqual(v2["id"], v1["id"])
        # 重新整理：新文字 → 新草稿版本；旧段落原样保留（旧版本出处不被静默换成新版本）。
        # 出处（含版本）不同的候选按新的精确出处追加：同位置文字仍在的段落也以其当前归属版本出现。
        again = self.db.kb2_build_topic_draft(TENANT, self.admin["id"], topic_id)
        self.assertEqual(again["added"], 2)
        paragraphs = self.db.kb2_get_topic(TENANT, topic_id)["content"]["paragraphs"]
        texts = {p["text"]: p for p in paragraphs}
        self.assertIn("第一行文字。", texts)
        self.assertEqual(texts["第一行文字。"]["source"]["version_id"], v1["id"], "旧文字保持旧版本出处")
        self.assertIn("第一行文字（更新）。", texts)
        self.assertEqual(texts["第一行文字（更新）。"]["source"]["version_id"], v2["id"], "新文字挂新草稿版本")
        revised_v2 = [p for p in paragraphs if p["text"] == "第二行文字。"]
        self.assertEqual(len(revised_v2), 2, "同文字不同版本出处是两条精确出处（不并入一条）")
        self.assertEqual({p["source"]["version_id"] for p in revised_v2}, {v1["id"], v2["id"]})
        # 再整理：出处（含版本）相同不重复追加。
        third = self.db.kb2_build_topic_draft(TENANT, self.admin["id"], topic_id)
        self.assertEqual(third["added"], 0)
        self.assertEqual(third["skipped"], 2)

    def test_build_draft_counts_unattributable_rows(self):
        """没有归属版本（真实内容里找不到这段文字）的行不产出出处，计入 unattributed。"""
        fixture = self.ingest_txt(text="真实文字。")
        item_id = fixture["item_id"]
        self.publish_item(item_id)
        # 发布后数据层直接改段落文字、不刷新版本正文：模拟「找不到归属版本」的漂移状态。
        self.db.conn.execute(
            "UPDATE kb2_paragraphs SET text='漂移文字。' WHERE tenant_id=? AND source_id=?",
            (TENANT, fixture["source"]["id"]))
        self.db.conn.commit()
        topic_id = self.db.kb2_create_topic(TENANT, self.admin["id"], "漂移")
        self.db.kb2_set_relation(TENANT, self.admin["id"], topic_id, {"kind": "knowledge", "id": item_id})
        result = self.db.kb2_build_topic_draft(TENANT, self.admin["id"], topic_id)
        self.assertEqual(result["added"], 0)
        self.assertEqual(result["unattributed"], 1)

    def test_existing_pins_and_relation_not_retargeted_until_human_action(self):
        """资料出现新版本：现有段落出处与关联钉住不变；显式「重新核对/重新钉住」才更新。"""
        fixture = self.ingest_txt(text="第一行文字。")
        item_id = fixture["item_id"]
        v1 = self.publish_item(item_id)
        topic_id = self.db.kb2_create_topic(TENANT, self.admin["id"], "显式确认")
        self.db.kb2_set_relation(TENANT, self.admin["id"], topic_id, {"kind": "knowledge", "id": item_id})
        self.db.kb2_build_topic_draft(TENANT, self.admin["id"], topic_id)
        paragraph = self.db.kb2_get_topic(TENANT, topic_id)["content"]["paragraphs"][0]
        self.assertTrue(paragraph["source_stale"] is False)
        # 资料出现新草稿版本（内容不变）：不静默换版。
        self.db.save_knowledge_draft(TENANT, item_id, self.admin["id"], content={"body": "第一行文字。"})
        v2 = self.db.get_knowledge_draft_version(TENANT, item_id)
        self.assertNotEqual(v1["id"], v2["id"])
        unchanged = self.db.kb2_get_topic(TENANT, topic_id)["content"]["paragraphs"][0]
        self.assertEqual(unchanged["source"]["version_id"], v1["id"], "不静默改现有段落的出处")
        relation = self.db.kb2_list_relations(TENANT, topic_id)[0]
        self.assertEqual(relation["pinned_version_id"], v1["id"], "不静默改关联钉住")
        # 成员显式重新核对来源：把这一段重新钉到当前（草稿）版本。
        renewed = self.db.kb2_renew_topic_paragraph_source(TENANT, self.admin["id"], topic_id, unchanged["id"])
        self.assertTrue(renewed["updated"])
        self.assertEqual(renewed["source"]["version_id"], v2["id"])
        refreshed_topic = self.db.kb2_get_topic(TENANT, topic_id)
        self.assertEqual(refreshed_topic["content"]["paragraphs"][0]["source"]["version_id"], v2["id"])
        # 成员显式重新钉住关联。
        result = self.db.kb2_refresh_relation_pin(TENANT, self.admin["id"], topic_id,
                                                  {"kind": "knowledge", "id": item_id})
        self.assertTrue(result["updated"])
        self.assertEqual(result["current"]["pinned_version_id"], v2["id"])
        relation = self.db.kb2_list_relations(TENANT, topic_id)[0]
        self.assertEqual(relation["pinned_version_id"], v2["id"])
        # 幂等：再次重新钉住不产生变化。
        self.assertFalse(self.db.kb2_refresh_relation_pin(
            TENANT, self.admin["id"], topic_id, {"kind": "knowledge", "id": item_id})["updated"])

    def test_renew_fails_when_text_no_longer_exists(self):
        """重新核对只能在当前资料真实包含这段文字时成功，不猜测出处。"""
        fixture = self.ingest_txt(text="原始文字。")
        item_id = fixture["item_id"]
        self.publish_item(item_id)
        topic_id = self.db.kb2_create_topic(TENANT, self.admin["id"], "无法核对")
        self.db.kb2_set_relation(TENANT, self.admin["id"], topic_id, {"kind": "knowledge", "id": item_id})
        self.db.kb2_build_topic_draft(TENANT, self.admin["id"], topic_id)
        paragraph = self.db.kb2_get_topic(TENANT, topic_id)["content"]["paragraphs"][0]
        rows = self.db.conn.execute(
            "SELECT * FROM kb2_paragraphs WHERE tenant_id=? AND source_id=? ORDER BY ordinal",
            (TENANT, fixture["source"]["id"])).fetchall()
        self.db.kb2_update_paragraph(TENANT, self.admin["id"], rows[0]["id"], text="完全不同的新文字。")
        self.db.kb2_update_paragraph(TENANT, self.admin["id"], rows[0]["id"], checked=True)
        with self.assertRaisesRegex(ValueError, "找不到"):
            self.db.kb2_renew_topic_paragraph_source(TENANT, self.admin["id"], topic_id, paragraph["id"])

    def test_publish_revalidates_material_text_drift(self):
        """保存后资料内容漂移：发布时按实际内容复核，拦截并提示改标人工改写。"""
        fixture = self.ingest_txt(text="第一行文字。")
        item_id = fixture["item_id"]
        v1 = self.publish_item(item_id)
        topic_id = self.db.kb2_create_topic(TENANT, self.admin["id"], "漂移发布")
        self.db.kb2_set_relation(TENANT, self.admin["id"], topic_id, {"kind": "knowledge", "id": item_id})
        self.db.kb2_build_topic_draft(TENANT, self.admin["id"], topic_id)
        paragraph = self.db.kb2_get_topic(TENANT, topic_id)["content"]["paragraphs"][0]
        # 模拟位置内容变化：数据层把该段文字改成别的内容（版本正文刷新）。
        rows = self.db.conn.execute(
            "SELECT * FROM kb2_paragraphs WHERE tenant_id=? AND source_id=? ORDER BY ordinal",
            (TENANT, fixture["source"]["id"])).fetchall()
        self.db.conn.execute("UPDATE kb2_paragraphs SET text='内容已变化。' WHERE id=?", (rows[0]["id"],))
        self.db.conn.commit()
        self.db.kb2_save_topic_draft(TENANT, self.admin["id"], topic_id,
                                     paragraphs=[{**paragraph, "checked": True}])
        with self.assertRaisesRegex(ValueError, "不一致"):
            self.db.kb2_publish_topic_draft(TENANT, self.admin["id"], topic_id)

    def test_edited_origin_source_label_marks_rewrite(self):
        """人工改写段落的展示标注：来源标签注明人工改写，不暗示原文一致。"""
        fixture = self.ingest_txt()
        topic_id, error = self.try_save([self.full_pin(fixture)])
        self.assertIsNone(error, error)
        attack = self.full_pin(fixture, text="改写文字。")
        self.db.kb2_save_topic_draft(TENANT, self.admin["id"], topic_id, paragraphs=[attack])
        paragraph = self.db.kb2_get_topic(TENANT, topic_id)["content"]["paragraphs"][0]
        self.assertEqual(paragraph["origin"], "edited")
        self.assertIn("人工改写", paragraph["source_label"])


if __name__ == "__main__":
    unittest.main()
