"""企业知识第二批 · 文件历史恢复的发布快照（判别性回归）。

对应独立复现 `/tmp/b2b-phase2-file-history-review.py` 与用户 2026-10-12 的要求：
- 恢复历史版本产生的新草稿在发布时保留恢复点自带的不可变字段快照，
  不得被“最新全局字段行”静默覆盖（修复前：恢复 v2(2200)后发布 v4，字段变成 2400）；
- 普通草稿的既有口径不变：发布时把当前字段行冻结为发布快照。
只用临时数据库与测试私有上传目录；不触碰 data/ 下真实演示数据与任何密钥；不接真实模型。
"""
import json
import os
import tempfile
import unittest
from pathlib import Path

from app import Database

TENANT = "demo-tenant"


class HistoryRestorePublishTests(unittest.TestCase):
    maxDiff = None

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.upload_dir = Path(self.tmp.name) / "uploads"
        os.environ["B2B_KB_STORAGE_ROOT"] = str(self.upload_dir)
        self.db = Database(Path(self.tmp.name) / "kb2-history.db")
        self.db.knowledge_storage_root = self.upload_dir
        self.db.seed_demo_users()
        self.admin = self.db.authenticate_user("demo-admin@example.invalid", "demo-admin")

    def tearDown(self):
        self.db.conn.close()
        os.environ.pop("B2B_KB_STORAGE_ROOT", None)
        self.tmp.cleanup()

    # ── 夹具 ──────────────────────────────────────────────────────────────

    def publish_initial(self):
        batch = self.db.kb2_create_batch(
            TENANT, self.admin["id"],
            [{"kind": "file", "filename": "warranty.txt", "data": b"Power supply 220V"}])
        source = self.db.kb2_get_batch(TENANT, batch)["sources"][0]
        for row in self.db.conn.execute(
                "SELECT id FROM kb2_paragraphs WHERE tenant_id=? AND source_id=?",
                (TENANT, source["id"])).fetchall():
            self.db.kb2_update_paragraph(TENANT, self.admin["id"], row["id"], checked=True)
        self.db.kb2_publish_batch(TENANT, self.admin["id"], batch)
        return batch, source["item_id"]

    def fields_of(self, version):
        return json.loads(version["content_json"]).get("kb2_fields")

    # ── 用例 ──────────────────────────────────────────────────────────────

    def test_restored_draft_publish_keeps_restored_field_snapshot(self):
        """恢复点字段快照（2200/已核对）不被最新全局字段行（2400）静默覆盖。"""
        batch, item_id = self.publish_initial()
        template_version = self.db.kb2_save_template(
            TENANT, self.admin["id"],
            fields=[{"key": "voltage", "label": "Voltage", "type": "text"}], name="QA")
        self.db.kb2_adopt_template(TENANT, self.admin["id"], item_id, template_version, confirmed=True)
        self.db.kb2_set_item_field(TENANT, self.admin["id"], item_id, "voltage",
                                   value="2200", source={"kind": "manual"}, checked=True)
        self.db.kb2_publish_batch(TENANT, self.admin["id"], batch)
        v2 = self.db.get_published_knowledge_version(TENANT, item_id)
        self.assertEqual(self.fields_of(v2)[0]["value"], "2200")

        self.db.kb2_set_item_field(TENANT, self.admin["id"], item_id, "voltage", value="2400")
        self.db.publish_knowledge_draft(TENANT, item_id, self.admin["id"])
        self.assertEqual(
            self.fields_of(self.db.get_published_knowledge_version(TENANT, item_id))[0]["value"], "2400")

        self.db.restore_knowledge_version(TENANT, item_id, 2, self.admin["id"])
        draft = self.db.get_knowledge_draft_version(TENANT, item_id)
        self.assertEqual(self.fields_of(draft)[0]["value"], "2200")
        self.db.publish_knowledge_draft(TENANT, item_id, self.admin["id"])
        published = self.db.get_published_knowledge_version(TENANT, item_id)
        restored = self.fields_of(published)
        self.assertEqual(restored, self.fields_of(draft), "发布必须保留恢复点字段快照，不用最新全局字段行覆盖")
        self.assertEqual(restored[0]["value"], "2200")
        self.assertTrue(restored[0]["checked"])

    def test_normal_draft_publish_still_freezes_current_fields(self):
        """普通（非恢复）草稿：发布仍冻结当前字段行（既有口径不变）。"""
        batch, item_id = self.publish_initial()
        template_version = self.db.kb2_save_template(
            TENANT, self.admin["id"],
            fields=[{"key": "voltage", "label": "Voltage", "type": "text"}], name="QA2")
        self.db.kb2_adopt_template(TENANT, self.admin["id"], item_id, template_version, confirmed=True)
        self.db.kb2_set_item_field(TENANT, self.admin["id"], item_id, "voltage",
                                   value="2300", source={"kind": "manual"}, checked=True)
        self.db.publish_knowledge_draft(TENANT, item_id, self.admin["id"])
        published = self.db.get_published_knowledge_version(TENANT, item_id)
        fields = self.fields_of(published)
        self.assertEqual(fields[0]["value"], "2300")
        self.assertTrue(fields[0]["checked"])

    def test_restore_initializes_working_set_and_post_restore_edit_flows(self):
        """B2-B11：恢复历史版本即用恢复点快照初始化字段工作集；恢复后的明确编辑（改值+再核对）
        进入新草稿的发布快照（发布取编辑后的值）；恢复前的工作集漂移不混入；旧历史不可变。

        对应独立复现 `/tmp/b2b-phase2-restored-edit-review.py`（修复前：恢复 v1 后改为 3 再核对再发布，
        发布值仍为 1——恢复快照钩子忽略已保存的修改）。
        """
        item_id = self.db.create_knowledge_item(
            TENANT, "article", "Service", self.admin["id"], content={"body": "Service details"})
        template_version = self.db.kb2_save_template(
            TENANT, self.admin["id"],
            fields=[{"key": "price", "label": "Price", "type": "number"}], name="QA-restore")
        self.db.kb2_adopt_template(TENANT, self.admin["id"], item_id, template_version, confirmed=True)
        self.db.kb2_set_item_field(TENANT, self.admin["id"], item_id, "price",
                                   value=1, source={"kind": "manual"}, checked=True)
        self.db.publish_knowledge_draft(TENANT, item_id, self.admin["id"])
        self.db.kb2_set_item_field(TENANT, self.admin["id"], item_id, "price", value=2)
        self.db.kb2_set_item_field(TENANT, self.admin["id"], item_id, "price", checked=True)
        self.db.publish_knowledge_draft(TENANT, item_id, self.admin["id"])
        self.assertEqual(self.fields_of(self.db.get_published_knowledge_version(TENANT, item_id))[0]["value"], "2")

        self.db.restore_knowledge_version(TENANT, item_id, 1, self.admin["id"])
        draft = self.db.get_knowledge_draft_version(TENANT, item_id)
        self.assertEqual(draft["restored_from_version"], 1)
        # 恢复即初始化工作集（字段行）为恢复点值；旧历史与线上不变。
        rows = {field["field_key"]: field for field in self.db.kb2_list_item_fields(TENANT, item_id)["fields"]}
        self.assertEqual(rows["price"]["value"], "1", "恢复应初始化准确工作集（恢复点值）")
        self.assertTrue(rows["price"]["checked"])
        self.assertEqual(self.fields_of(self.db.get_published_knowledge_version(TENANT, item_id))[0]["value"], "2")
        # 恢复后的明确编辑：改值 3 并重新核对 → 进入新草稿快照。
        self.db.kb2_set_item_field(TENANT, self.admin["id"], item_id, "price", value=3)
        self.db.kb2_set_item_field(TENANT, self.admin["id"], item_id, "price", checked=True)
        self.db.publish_knowledge_draft(TENANT, item_id, self.admin["id"])
        restored_fields = self.fields_of(self.db.get_published_knowledge_version(TENANT, item_id))
        self.assertEqual(restored_fields[0]["value"], "3", "恢复后的明确编辑必须进入新草稿快照")
        self.assertTrue(restored_fields[0]["checked"])
        # 恢复前的工作集漂移（2）没有复现；历史版本列表保持完整。
        versions = [(row["version"], row["status"]) for row in self.db.list_knowledge_versions(TENANT, item_id)]
        self.assertIn((2, "superseded"), versions)
        self.assertEqual(self.fields_of(
            self.db.get_published_knowledge_version(TENANT, item_id))[0]["value"], "3")


if __name__ == "__main__":
    unittest.main()
