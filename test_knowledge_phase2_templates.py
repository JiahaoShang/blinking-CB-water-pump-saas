"""企业知识第二批 · 任务4：文件更新（替换）与灵活字段/模板（判别性用例）。

对应用户 2026-10-12/13 对任务4的要求与计划 checklist：
- 五种简单类型（单/多行文字、数字＋单位、选项、日期、是/否）；0 与「否」是有效值，未选择与不同；
- 模板版本不可变；改类型按新字段身份（旧值不自动转换）；
- 升级先对照、明确确认，只作用草稿：保留旧字段历史与已有草稿，发布版本与线上内容不动；
- 新版必填只约束「采用新版模板后」的发布；
- 文件更新产生私有新原件 + 新草稿与位置/文本差异对照；旧线上版本在核对发布前保持不变；
- 同一知识 ID / 历史 / 既有引用（如主题来源）保留；连续更新、重复确认不重复版本；
- 发布失败不留半更新。
只用临时数据库与测试私有上传目录；不触碰 data/ 下真实演示数据与任何密钥。
"""
import json
import os
import tempfile
import unittest
from pathlib import Path

from app import Database

TENANT = "demo-tenant"


class TemplatesBase(unittest.TestCase):
    maxDiff = None

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.upload_dir = Path(self.tmp.name) / "uploads"
        os.environ["B2B_KB_STORAGE_ROOT"] = str(self.upload_dir)
        self.db = Database(Path(self.tmp.name) / "phase2-templates.db")
        self.db.knowledge_storage_root = self.upload_dir
        self.db.seed_demo_users()
        self.admin = self.db.authenticate_user("demo-admin@example.invalid", "demo-admin")

    def tearDown(self):
        self.db.conn.close()
        os.environ.pop("B2B_KB_STORAGE_ROOT", None)
        self.tmp.cleanup()

    # ── 夹具 ──────────────────────────────────────────────────────────────

    def ingest_txt(self, filename="warranty.txt", text="保修24个月。"):
        batch_id = self.db.kb2_create_batch(
            TENANT, self.admin["id"], [{"kind": "file", "filename": filename, "data": text.encode()}])
        return self.db.kb2_get_batch(TENANT, batch_id)["sources"][0]

    def check_all(self, source_id):
        for row in self.db.conn.execute(
                "SELECT id FROM kb2_paragraphs WHERE tenant_id=? AND source_id=?",
                (TENANT, source_id)).fetchall():
            self.db.kb2_update_paragraph(TENANT, self.admin["id"], row["id"], checked=True)

    def publish_source_batch(self, batch_id, *, usage="external", ai_authorized=True):
        result = self.db.kb2_publish_batch(
            TENANT, self.admin["id"], batch_id, usage=usage, ai_authorized=ai_authorized,
            confirm_ai=True)
        self.assertFalse(result["failed"], result["failed"])
        return result

    def save_template(self, name, fields, *, template_id=None):
        return self.db.kb2_save_template(TENANT, self.admin["id"], template_id, fields, name=name)

    def make_published_item(self, text="保修24个月。\n安装前断电。"):
        source = self.ingest_txt(text=text)
        self.check_all(source["id"])
        batch_id = self.db.conn.execute(
            "SELECT batch_id FROM kb2_sources WHERE id=?", (source["id"],)).fetchone()[0]
        self.publish_source_batch(batch_id)
        return source["item_id"]

    def template_field(self, label, type_, **kw):
        entry = {"label": label, "type": type_}
        entry.update(kw)
        return entry

    # ── 模板定义与版本 ────────────────────────────────────────────────────

class TemplateDefinitionTests(TemplatesBase):
    def test_five_simple_field_types_and_valid_zero_false(self):
        """五种简单类型可保存；0/否有效且与未填不同；空选项/重复选项拒绝。"""
        version_id = self.save_template("服务方案", [
            self.template_field("简介", "text", key="intro"),
            self.template_field("说明", "textarea", key="desc"),
            self.template_field("响应时间", "number", unit="小时", key="resp"),
            self.template_field("服务区域", "select", options=["华东", "华南", "华南", ""], key="region"),
            self.template_field("生效日期", "date", key="effective"),
            self.template_field("支持定制", "bool", key="custom"),
        ])
        template = self.db.kb2_get_template(TENANT, self.db.kb2_list_templates(TENANT)[0]["id"])
        fields = template["versions"][0]["fields"]
        self.assertEqual([f["type"] for f in fields],
                         ["text", "textarea", "number", "select", "date", "bool"])
        self.assertEqual(fields[3]["options"], ["华东", "华南"], "重复/空选项不保存")
        with self.assertRaisesRegex(ValueError, "至少需要两个"):
            self.save_template("坏模板", [self.template_field("区域", "select", options=["只有一个"])])
        with self.assertRaisesRegex(ValueError, "名称不能重复"):
            self.save_template("坏模板2", [self.template_field("字段A", "text"), self.template_field("字段A", "text")])
        with self.assertRaisesRegex(ValueError, "类型无效"):
            self.save_template("坏模板3", [self.template_field("字段A", "whatever")])

    def test_template_versions_are_immutable(self):
        """旧模板版本字段定义不可变：新版本只追加，改类型仍是新版本内记录。"""
        item_source = self.ingest_txt()
        item_id = item_source["item_id"]
        v1 = self.save_template("服务方案", [self.template_field("响应时间", "number", unit="小时", key="resp")])
        template_id = self.db.kb2_list_templates(TENANT)[0]["id"]
        v2 = self.save_template("服务方案", [self.template_field("响应时间", "text", key="resp")],
                                template_id=template_id)
        template = self.db.kb2_get_template(TENANT, template_id)
        versions = {v["version"]: v for v in template["versions"]}
        self.assertEqual(versions[1]["fields"][0]["type"], "number")
        self.assertEqual(versions[2]["fields"][0]["type"], "text")
        self.assertNotEqual(v1, v2)


class TemplateAdoptionTests(TemplatesBase):
    def test_adoption_preview_then_confirm_only_touches_draft(self):
        """不确认只对照；确认后只改草稿与字段工作集：线上内容不动、旧值保留。"""
        source = self.ingest_txt(text="保修24个月。")
        item_id = source["item_id"]
        self.check_all(source["id"])
        self.publish_source_batch(self.db.kb2_get_batch(TENANT, self.db.conn.execute(
            "SELECT batch_id FROM kb2_sources WHERE id=?", (source["id"],)).fetchone()[0])["id"])
        published = self.db.get_published_knowledge_version(TENANT, item_id)
        v1 = self.save_template("服务方案", [
            self.template_field("响应时间", "number", unit="小时", required=True, key="resp"),
            self.template_field("备注", "text", key="note"),
        ])
        preview = self.db.kb2_adopt_template(TENANT, self.admin["id"], item_id, v1)
        self.assertFalse(preview["applied"], "未确认不得改数据")
        self.assertEqual(self.db.conn.execute(
            "SELECT COUNT(*) FROM kb2_item_template").fetchone()[0], 0)
        self.assertEqual([f["key"] for f in preview["diff"]["added"]], ["resp", "note"])
        self.assertEqual([f["key"] for f in preview["diff"]["missing_required"]], ["resp"])
        result = self.db.kb2_adopt_template(TENANT, self.admin["id"], item_id, v1, confirmed=True)
        self.assertTrue(result["applied"])
        self.assertIsNotNone(result["draft_version_id"])
        # 线上版本内容与状态未变。
        fresh_published = self.db.get_published_knowledge_version(TENANT, item_id)
        self.assertEqual(fresh_published["id"], published["id"])
        self.assertEqual(fresh_published["content_json"], published["content_json"])
        # 重复确认同一版本 → 幂等，不产生变化。
        again = self.db.kb2_adopt_template(TENANT, self.admin["id"], item_id, v1, confirmed=True)
        self.assertFalse(again["applied"])
        self.assertTrue(again["already"])

    def test_type_change_is_new_identity_and_old_value_kept_as_history(self):
        """改类型按新字段身份：值不自动转换；旧行保留为历史（removed），新行未填。"""
        source = self.ingest_txt(text="响应时间为一小时。")
        item_id = source["item_id"]
        v1 = self.save_template("服务方案", [self.template_field("响应时间", "number", key="resp")])
        self.db.kb2_adopt_template(TENANT, self.admin["id"], item_id, v1, confirmed=True)
        value = self.db.kb2_set_item_field(
            TENANT, self.admin["id"], item_id, "resp", value="48", unit="小时",
            source={"kind": "manual"}, checked=True)
        self.assertEqual(value["value"], "48")
        template_id = self.db.kb2_list_templates(TENANT)[0]["id"]
        v2 = self.save_template("服务方案", [self.template_field("响应时间", "text", key="resp")],
                                template_id=template_id)
        diff = self.db.kb2_adopt_template(TENANT, self.admin["id"], item_id, v2)["diff"]
        self.assertEqual(len(diff["type_changed"]), 1)
        self.db.kb2_adopt_template(TENANT, self.admin["id"], item_id, v2, confirmed=True)
        rows = self.db.conn.execute(
            "SELECT field_key, type, value, removed FROM kb2_item_fields WHERE item_id=? ORDER BY created_at",
            (item_id,)).fetchall()
        self.assertEqual([(r["field_key"], r["type"], r["value"], r["removed"]) for r in rows],
                         [("resp", "number", "48", 1), ("resp", "text", None, 0)],
                         "旧值保留为历史，新字段未填（不自动转换）")
        # 移除字段：不再显示、旧值保留。
        v3 = self.save_template("服务方案", [self.template_field("另一字段", "text", key="other")],
                                template_id=template_id)
        diff3 = self.db.kb2_adopt_template(TENANT, self.admin["id"], item_id, v3)["diff"]
        self.assertEqual([f["key"] for f in diff3["removed"]], ["resp"])
        self.db.kb2_adopt_template(TENANT, self.admin["id"], item_id, v3, confirmed=True)
        active = [f["field_key"] for f in self.db.kb2_list_item_fields(TENANT, item_id)["fields"]]
        self.assertEqual(active, ["other"])
        history = self.db.conn.execute(
            "SELECT COUNT(*) FROM kb2_item_fields WHERE item_id=? AND field_key='resp'", (item_id,)).fetchone()[0]
        self.assertEqual(history, 2, "旧字段历史继续保留（不静默删）")

    def test_new_required_constrains_only_new_template_publications(self):
        """新增必填只约束采用新版模板后的发布：旧模板下先发布不受影响。"""
        source = self.ingest_txt(text="正文内容。")
        item_id = source["item_id"]
        v1 = self.save_template("服务方案", [self.template_field("备注", "text", key="note")])
        self.db.kb2_adopt_template(TENANT, self.admin["id"], item_id, v1, confirmed=True)
        template_id = self.db.kb2_list_templates(TENANT)[0]["id"]
        v2 = self.save_template("服务方案", [self.template_field("新必填", "text", required=True, key="mandatory")],
                                template_id=template_id)
        # 未采用新版：发布不受新必填影响。
        db = self.db
        self.check_all(source["id"])
        db._kb2_prepare_publish_fields_content(TENANT, item_id, {"title": "t", "body": "b"})
        # 采用新版后：新必填缺项阻止发布。
        db.kb2_adopt_template(TENANT, self.admin["id"], item_id, v2, confirmed=True)
        with self.assertRaisesRegex(ValueError, "必填字段未填写"):
            db._kb2_prepare_publish_fields_content(TENANT, item_id, {"title": "t", "body": "b"})
        db.kb2_set_item_field(TENANT, self.admin["id"], item_id, "mandatory", value="已填",
                              source={"kind": "manual"})
        snapshot = db._kb2_prepare_publish_fields_content(TENANT, item_id, {"title": "t", "body": "b"})
        self.assertEqual(snapshot["kb2_fields"][0]["field_key"], "mandatory")


class FieldValueTests(TemplatesBase):
    def make_item_with_template(self):
        source = self.ingest_txt(text="响应时间一小时。\n保修24个月。")
        item_id = source["item_id"]
        version_id = self.save_template("服务方案", [
            self.template_field("响应时间", "number", unit="小时", key="resp"),
            self.template_field("服务区域", "select", options=["华东", "华南"], key="region"),
            self.template_field("生效日期", "date", key="effective"),
            self.template_field("支持定制", "bool", key="custom"),
            self.template_field("备注", "textarea", key="note"),
        ])
        self.db.kb2_adopt_template(TENANT, self.admin["id"], item_id, version_id, confirmed=True)
        return source, item_id

    def field(self, item_id, key):
        return next(f for f in self.db.kb2_list_item_fields(TENANT, item_id)["fields"] if f["field_key"] == key)

    def test_type_validation_and_zero_false_are_valid(self):
        """0 / 否 是有效值，未填与 0 不同；错误类型明确拒绝。"""
        source, item_id = self.make_item_with_template()
        self.db.kb2_set_item_field(TENANT, self.admin["id"], item_id, "resp",
                                   value="0", source={"kind": "manual"})
        self.assertEqual(self.field(item_id, "resp")["value"], "0")
        self.assertTrue(self.field(item_id, "resp")["value_present"])
        self.db.kb2_set_item_field(TENANT, self.admin["id"], item_id, "custom",
                                   value=False, source={"kind": "manual"})
        self.assertEqual(self.field(item_id, "custom")["value"], "false")
        self.assertTrue(self.field(item_id, "custom")["value_present"])
        # 未填 ≠ 0：清空后 value_present False。
        self.db.kb2_set_item_field(TENANT, self.admin["id"], item_id, "resp", value=None)
        self.assertFalse(self.field(item_id, "resp")["value_present"])
        with self.assertRaisesRegex(ValueError, "有效数字"):
            self.db.kb2_set_item_field(TENANT, self.admin["id"], item_id, "resp", value="不是数字")
        with self.assertRaisesRegex(ValueError, "选项"):
            self.db.kb2_set_item_field(TENANT, self.admin["id"], item_id, "region", value="华中")
        with self.assertRaisesRegex(ValueError, "YYYY-MM-DD"):
            self.db.kb2_set_item_field(TENANT, self.admin["id"], item_id, "effective", value="2026年10月")
        with self.assertRaisesRegex(ValueError, "是/否"):
            self.db.kb2_set_item_field(TENANT, self.admin["id"], item_id, "custom", value="也许")

    def test_every_value_requires_source_and_edit_revokes_checked(self):
        """每个字段必须写出来源（来源段落或人工填写）；改值撤销核对标记。"""
        source, item_id = self.make_item_with_template()
        with self.assertRaisesRegex(ValueError, "必须写出来源"):
            self.db.kb2_set_item_field(TENANT, self.admin["id"], item_id, "note", value="无来源内容")
        paragraph = self.db.conn.execute(
            "SELECT * FROM kb2_paragraphs WHERE tenant_id=? AND source_id=? ORDER BY ordinal",
            (TENANT, source["id"])).fetchone()
        field = self.db.kb2_set_item_field(
            TENANT, self.admin["id"], item_id, "note", value="响应为一小时。",
            source={"kind": "source", "source_id": source["id"], "paragraph_id": paragraph["id"]},
            checked=True, allow_customer=True)
        self.assertEqual(field["source"]["kind"], "source")
        self.assertIn("第 1 行", field["source"]["label"])
        self.assertTrue(field["checked"])
        updated = self.db.kb2_set_item_field(TENANT, self.admin["id"], item_id, "note", value="响应约一小时。")
        self.assertFalse(updated["checked"], "值变化 → 核对失效")
        # 其他资料的段落不能被引用为该字段来源。
        other = self.ingest_txt("other.txt", "另一份资料。")
        with self.assertRaisesRegex(ValueError, "不属于该资料"):
            self.db.kb2_set_item_field(
                TENANT, self.admin["id"], item_id, "note", value="x",
                source={"kind": "source", "source_id": other["id"],
                        "paragraph_id": self.db.conn.execute(
                            "SELECT id FROM kb2_paragraphs WHERE source_id=?", (other["id"],)).fetchone()[0]})

    def test_change_value_or_unit_revokes_checked_even_with_old_flag(self):
        """B2-B05：改值/改单位强制撤销核对——同一次提交携带的旧 checked=true 不豁免；随后显式再核对有效。"""
        source, item_id = self.make_item_with_template()
        self.db.kb2_set_item_field(TENANT, self.admin["id"], item_id, "resp",
                                   value="220", unit="V", source={"kind": "manual"})
        self.assertTrue(self.db.kb2_set_item_field(
            TENANT, self.admin["id"], item_id, "resp",
            checked=True, allow_customer=True)["checked"])
        changed = self.db.kb2_set_item_field(TENANT, self.admin["id"], item_id, "resp",
                                             value="2200", checked=True)
        self.assertFalse(changed["checked"], "改值后携带旧 checked=true 必须被撤销")
        self.assertEqual(changed["value"], "2200")
        # 值未变时显式再核对有效。
        self.assertTrue(self.db.kb2_set_item_field(
            TENANT, self.admin["id"], item_id, "resp", checked=True)["checked"])
        unit_changed = self.db.kb2_set_item_field(TENANT, self.admin["id"], item_id, "resp", unit="kV")
        self.assertFalse(unit_changed["checked"], "改单位同样使核对失效")
        # 清空值同样撤销（并入后续核对路径）。
        self.db.kb2_set_item_field(TENANT, self.admin["id"], item_id, "resp", checked=True)
        cleared = self.db.kb2_set_item_field(TENANT, self.admin["id"], item_id, "resp", value=None)
        self.assertFalse(cleared["checked"])


class FileReplacementTests(TemplatesBase):
    def test_replacement_creates_new_original_and_draft_with_diff(self):
        """替换产生新原件/新草稿与差异对照；旧线上版本在核对发布前保持可用且不变。"""
        item_id = self.make_published_item()
        published = self.db.get_published_knowledge_version(TENANT, item_id)
        result = self.db.kb2_replace_file(
            TENANT, self.admin["id"], item_id, "warranty_v2.txt",
            "保修36个月。\n安装前断电。\n请保留合格证。".encode())
        self.assertEqual(result["status"], "extracted")
        self.assertNotEqual(result["draft_id"], published["id"])
        diff = result["diff"]
        self.assertEqual(diff["counts"]["changed"], 1)
        self.assertEqual(diff["counts"]["added"], 1)
        self.assertEqual(diff["counts"]["unchanged"], 1)
        self.assertEqual(diff["changed"][0]["old"], "保修24个月。")
        self.assertEqual(diff["changed"][0]["new"], "保修36个月。")
        # 旧线上版本不变（内容/状态/来源）。
        still = self.db.get_published_knowledge_version(TENANT, item_id)
        self.assertEqual(still["id"], published["id"])
        self.assertEqual(still["content_json"], published["content_json"])
        self.assertEqual(still["source_id"], published["source_id"])
        # 新草稿以新原件为出处；新原件私有保存且有 sha256。
        draft = self.db.get_knowledge_draft_version(TENANT, item_id)
        self.assertEqual(draft["id"], result["draft_id"])
        self.assertNotEqual(draft["source_id"], published["source_id"])
        kb2_source = self.db.conn.execute(
            "SELECT * FROM kb2_sources WHERE id=?", (result["source_id"],)).fetchone()
        self.assertEqual(kb2_source["replace_of_version_id"], published["id"])
        self.assertTrue(Path(kb2_source["storage_path"]).is_file())
        self.assertTrue(kb2_source["sha256"])
        # 同一知识 ID 与历史保留。
        self.assertEqual(self.db.get_knowledge_item(TENANT, item_id)["id"], item_id)
        versions = {v["version"]: v["status"] for v in self.db.list_knowledge_versions(TENANT, item_id)}
        self.assertEqual(versions[published["version"]], "published")
        # 未核对不能发布（半更新不发生）；核对后发布新版、旧版本转历史。
        publish = self.db.kb2_publish_batch(TENANT, self.admin["id"], result["batch_id"],
                                            usage="external", ai_authorized=True, confirm_ai=True)
        self.assertEqual(len(publish["failed"]), 1, "未核对段落必须先核对")
        self.assertEqual(self.db.get_published_knowledge_version(TENANT, item_id)["id"], published["id"])
        self.check_all(result["source_id"])
        publish = self.db.kb2_publish_batch(TENANT, self.admin["id"], result["batch_id"],
                                            usage="external", ai_authorized=True, confirm_ai=True)
        self.assertEqual(len(publish["published"]), 1, publish)
        new_published = self.db.get_published_knowledge_version(TENANT, item_id)
        self.assertNotEqual(new_published["id"], published["id"])
        self.assertIn("保修36个月。", json.loads(new_published["content_json"])["body"])
        versions = {v["version"]: v["status"] for v in self.db.list_knowledge_versions(TENANT, item_id)}
        self.assertEqual(versions[published["version"]], "superseded")

    def test_replacement_refused_with_existing_draft_and_no_half_update(self):
        """已有未发布草稿 → 拒绝（人工修改不被静默覆盖）；拒绝时不产生新的来源/版本/文件。"""
        item_id = self.make_published_item()
        self.db.save_knowledge_draft(TENANT, item_id, self.admin["id"], content={"body": "成员正在编辑的正文。"})
        before = {
            "sources": self.db.conn.execute("SELECT COUNT(*) FROM kb2_sources").fetchone()[0],
            "versions": self.db.conn.execute(
                "SELECT COUNT(*) FROM knowledge_versions WHERE item_id=?", (item_id,)).fetchone()[0],
        }
        with self.assertRaisesRegex(ValueError, "已有未发布草稿"):
            self.db.kb2_replace_file(TENANT, self.admin["id"], item_id, "x.txt", "新内容。".encode())
        self.assertEqual(self.db.conn.execute("SELECT COUNT(*) FROM kb2_sources").fetchone()[0], before["sources"])
        self.assertEqual(self.db.conn.execute(
            "SELECT COUNT(*) FROM knowledge_versions WHERE item_id=?", (item_id,)).fetchone()[0], before["versions"])
        draft = self.db.get_knowledge_draft_version(TENANT, item_id)
        self.assertIn("成员正在编辑", json.loads(draft["content_json"])["body"])

    def test_replacement_keeps_topic_citations_and_ids(self):
        """替换不改既有主题段落的出处（不静默换版），也不改资料 ID；成员显式重新核对才更新。"""
        item_id = self.make_published_item()
        topic_id = self.db.kb2_create_topic(TENANT, self.admin["id"], "保修主题")
        self.db.kb2_set_relation(TENANT, self.admin["id"], topic_id, {"kind": "knowledge", "id": item_id})
        self.db.kb2_build_topic_draft(TENANT, self.admin["id"], topic_id)
        old_para = self.db.kb2_get_topic(TENANT, topic_id)["content"]["paragraphs"][0]
        old_pin = dict(old_para["source"])
        self.db.kb2_replace_file(TENANT, self.admin["id"], item_id, "v2.txt", "保修24个月。\n安装前断电。".encode())
        unchanged = self.db.kb2_get_topic(TENANT, topic_id)["content"]["paragraphs"][0]
        self.assertEqual(unchanged["source"], old_pin, "替换不静默改既有主题出处")
        self.assertTrue(unchanged["source_stale"])
        self.assertEqual(self.db.get_knowledge_item(TENANT, item_id)["id"], item_id)


class ContinuousUpdateTests(TemplatesBase):
    def test_consecutive_replacements_and_repeat_confirmation_do_not_duplicate_versions(self):
        """连续更新：每次都产生独立来源与草稿；重复模板确认不重复版本。"""
        item_id = self.make_published_item(text="第一版内容。")
        first = self.db.kb2_replace_file(TENANT, self.admin["id"], item_id, "v2.txt", "第二版内容。".encode())
        self.check_all(first["source_id"])
        self.db.kb2_publish_batch(TENANT, self.admin["id"], first["batch_id"],
                                  usage="external", ai_authorized=True, confirm_ai=True)
        second = self.db.kb2_replace_file(TENANT, self.admin["id"], item_id, "v3.txt", "第三版内容。".encode())
        self.assertNotEqual(first["source_id"], second["source_id"])
        versions = self.db.list_knowledge_versions(TENANT, item_id)
        self.assertEqual(sorted(v["version"] for v in versions), [1, 2, 3], "连续更新产生有序版本")
        statuses = {v["version"]: v["status"] for v in versions}
        self.assertEqual(statuses[2], "published")
        self.assertEqual(statuses[3], "draft")
        # 重复采用同一模板版本不重复生成字段。
        template_version = self.save_template("服务方案", [self.template_field("备注", "text", key="note")])
        self.db.kb2_adopt_template(TENANT, self.admin["id"], item_id, template_version, confirmed=True)
        count_before = self.db.conn.execute(
            "SELECT COUNT(*) FROM kb2_item_fields WHERE item_id=?", (item_id,)).fetchone()[0]
        self.db.kb2_adopt_template(TENANT, self.admin["id"], item_id, template_version, confirmed=True)
        self.assertEqual(self.db.conn.execute(
            "SELECT COUNT(*) FROM kb2_item_fields WHERE item_id=?", (item_id,)).fetchone()[0], count_before)


if __name__ == "__main__":
    unittest.main()
