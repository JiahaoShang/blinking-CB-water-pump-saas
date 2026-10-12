"""企业知识第二批 · B2-B08 失败换版闸门（判别性回归）。

对应独立复现 `/tmp/b2b-phase2-failed-replacement-review.py` 与用户 2026-10-12 的修复要求：
损坏的新原件（处理 failed、未认领原件身份）不能把旧正文当作新原件的结果归档发布，
也不能替换线上版本；旧线上、草稿与新原件全部保留（可重试 / 人工整理）。

固定口径：
- 失败 / 未认领身份的来源，正式批次路径与共享 legacy 发布服务同口径拒绝（拒绝在任何写入之前）；
- 失败来源补充并经核对的人工正文可以发布（不冒用旧正文、不假装识别成功；不自动视为仅登记归档）；
- 成功「仅登记原件」（图片 / 链接 / 文本解析为空的原件）的归档发布保持可用；
- 未人工核对的人工正文不能发布（与批次同一核对口径）。

判别力：本文件在修复前代码上会失败（旧行为：失败换版发布 archived v2、正文沿用旧原件 220V、
线上 v1 被替换；共享服务同样放行借用旧正文的草稿）。
只用临时数据库与测试私有上传目录；不触碰 data/ 下真实演示数据与任何密钥；不接真实模型。
"""
import json
import os
import tempfile
import unittest
from pathlib import Path

from app import Database

TENANT = "demo-tenant"


class FailedReplacementBase(unittest.TestCase):
    maxDiff = None

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.upload_dir = Path(self.tmp.name) / "uploads"
        os.environ["B2B_KB_STORAGE_ROOT"] = str(self.upload_dir)
        self.db = Database(Path(self.tmp.name) / "kb2-failed-replacement.db")
        self.db.knowledge_storage_root = self.upload_dir
        self.db.seed_demo_users()
        self.admin = self.db.authenticate_user("demo-admin@example.invalid", "demo-admin")

    def tearDown(self):
        self.db.conn.close()
        os.environ.pop("B2B_KB_STORAGE_ROOT", None)
        self.tmp.cleanup()

    # ── 夹具 ──────────────────────────────────────────────────────────────

    def ingest(self, filename, data):
        batch_id = self.db.kb2_create_batch(
            TENANT, self.admin["id"], [{"kind": "file", "filename": filename, "data": data}])
        return batch_id, self.db.kb2_get_batch(TENANT, batch_id)["sources"][0]

    def check_all(self, source_id):
        for row in self.db.conn.execute(
                "SELECT id FROM kb2_paragraphs WHERE tenant_id=? AND source_id=?",
                (TENANT, source_id)).fetchall():
            self.db.kb2_update_paragraph(TENANT, self.admin["id"], row["id"], checked=True)

    def source_row(self, source_id):
        return self.db._kb2_source_row(TENANT, source_id)

    def published(self, item_id):
        return self.db.get_published_knowledge_version(TENANT, item_id)

    def published_snapshot(self, item_id):
        version = self.published(item_id)
        return None if version is None else (version["id"], version["version"], version["content_json"])

    def history_snapshot(self, item_id):
        return [(row["id"], row["version"], row["status"], row["content_json"])
                for row in self.db.list_knowledge_versions(TENANT, item_id)]

    def draft_snapshot(self, item_id):
        draft = self.db.get_knowledge_draft_version(TENANT, item_id)
        return None if draft is None else (draft["id"], draft["version"], draft["status"], draft["content_json"])

    def make_failed_replacement(self):
        """v1（220V）发布 → 替换损坏 PDF（failed）。返回场景信息。"""
        batch1, source1 = self.ingest("old.txt", b"Power supply 220V")
        self.check_all(source1["id"])
        result = self.db.kb2_publish_batch(TENANT, self.admin["id"], batch1)
        self.assertFalse(result["failed"], result)
        item_id = source1["item_id"]
        replacement = self.db.kb2_replace_file(
            TENANT, self.admin["id"], item_id, "damaged.pdf", b"not a valid PDF")
        self.assertEqual(replacement["status"], "failed")
        return {"batch1": batch1, "source1": source1["id"], "item_id": item_id, "replacement": replacement}


class FailedReplacementPublishTests(FailedReplacementBase):
    def test_failed_replacement_rejected_and_everything_preserved(self):
        scenario = self.make_failed_replacement()
        item_id = scenario["item_id"]
        published_before = self.published_snapshot(item_id)
        history_before = self.history_snapshot(item_id)
        draft_before = self.draft_snapshot(item_id)
        self.assertIsNotNone(draft_before, "替换产生的新草稿应保留")

        outcome = self.db.kb2_publish_batch(TENANT, self.admin["id"], scenario["replacement"]["batch_id"])

        self.assertEqual(outcome["published"], [], outcome)
        self.assertEqual(outcome["archived"], [], outcome)
        self.assertEqual(len(outcome["failed"]), 1, outcome)
        self.assertIn("原件身份", outcome["failed"][0]["error"])
        # 旧线上版本保持不变：仍是 v1、正文仍是旧原件内容、没有被标成「仅登记归档」。
        after = self.published(item_id)
        self.assertEqual(after["version"], 1)
        content = json.loads(after["content_json"])
        self.assertEqual(content["body"], "Power supply 220V")
        self.assertNotIn("kb2_registered_only", content)
        self.assertEqual(self.published_snapshot(item_id), published_before)
        self.assertEqual(self.history_snapshot(item_id), history_before)
        # 新草稿原样保留（可继续核对 / 重试 / 人工整理）。
        self.assertEqual(self.draft_snapshot(item_id), draft_before)
        # 失败的新原件保留（仍可重试）。
        fresh = self.source_row(scenario["replacement"]["source_id"])
        self.assertEqual(fresh["status"], "failed")
        self.assertIsNone(fresh["knowledge_source_id"], "失败来源未认领原件身份")

    def test_failed_replacement_shared_legacy_service_also_rejected(self):
        scenario = self.make_failed_replacement()
        item_id = scenario["item_id"]
        published_before = self.published_snapshot(item_id)
        draft_before = self.draft_snapshot(item_id)
        # 共享发布服务（第一批旧入口 / 书签路径最终调用的同一个服务）同口径拒绝：
        # 草稿仍借用上一版正文，不能把旧正文当作新原件的结果发布。
        with self.assertRaisesRegex(ValueError, "替换的原件"):
            self.db.publish_knowledge_draft(TENANT, item_id, self.admin["id"], confirm_ai=True)
        self.assertEqual(self.published_snapshot(item_id), published_before)
        self.assertEqual(self.draft_snapshot(item_id), draft_before)

    def test_retry_door_stays_open_after_failure(self):
        scenario = self.make_failed_replacement()
        item_id = scenario["item_id"]
        published_before = self.published_snapshot(item_id)
        self.db.kb2_retry_source(TENANT, self.admin["id"], scenario["replacement"]["source_id"])
        fresh = self.source_row(scenario["replacement"]["source_id"])
        self.assertEqual(fresh["status"], "failed", "损坏文件重试后仍明确失败，不假装成功")
        self.assertEqual(self.published_snapshot(item_id), published_before)

    def test_manual_content_after_failed_parser_can_publish(self):
        """失败后补充的人工正文：未核对不能发布；核对后以确切身份发布（正文只用人工内容）。"""
        scenario = self.make_failed_replacement()
        item_id = scenario["item_id"]
        paragraph_id = self.db.kb2_add_manual_paragraph(
            TENANT, self.admin["id"], scenario["replacement"]["source_id"],
            "人工正文：新原件损坏，参数以供应商补发文件为准。")
        # 补人工正文即认领该来源自己的原件身份（不再以空身份借用旧正文）。
        fresh = self.source_row(scenario["replacement"]["source_id"])
        self.assertIsNotNone(fresh["knowledge_source_id"])
        # 未核对：发布被拦。
        blocked = self.db.kb2_publish_batch(TENANT, self.admin["id"], scenario["replacement"]["batch_id"])
        self.assertEqual(blocked["published"], [])
        self.assertEqual(len(blocked["failed"]), 1)
        self.assertIn("未核对", blocked["failed"][0]["error"])
        self.assertEqual(self.published(item_id)["version"], 1)
        # 显式核对后发布：正文=人工内容，不含旧正文，也不标「仅登记」。
        self.db.kb2_update_paragraph(TENANT, self.admin["id"], paragraph_id, checked=True)
        outcome = self.db.kb2_publish_batch(TENANT, self.admin["id"], scenario["replacement"]["batch_id"])
        self.assertEqual(len(outcome["published"]), 1, outcome)
        published = self.published(item_id)
        self.assertEqual(published["version"], 2)
        content = json.loads(published["content_json"])
        self.assertEqual(content["body"], "人工正文：新原件损坏，参数以供应商补发文件为准。")
        self.assertNotIn("220V", content["body"])
        self.assertNotIn("kb2_registered_only", content)
        self.assertEqual(self.source_row(scenario["replacement"]["source_id"])["status"], "published")

    def test_manual_content_after_failed_parser_publish_via_shared_service(self):
        scenario = self.make_failed_replacement()
        item_id = scenario["item_id"]
        paragraph_id = self.db.kb2_add_manual_paragraph(
            TENANT, self.admin["id"], scenario["replacement"]["source_id"],
            "人工整理：损坏原件不可解析，以下为成员核实的信息。")
        self.db.kb2_update_paragraph(TENANT, self.admin["id"], paragraph_id, checked=True)
        version = self.db.publish_knowledge_draft(TENANT, item_id, self.admin["id"], confirm_ai=True)
        self.assertEqual(version, 2)
        content = json.loads(self.published(item_id)["content_json"])
        self.assertEqual(content["body"], "人工整理：损坏原件不可解析，以下为成员核实的信息。")
        self.assertNotIn("220V", content["body"])


class RegisteredOnlyStillWorksTests(FailedReplacementBase):
    def test_image_and_link_archive_publish_still_works(self):
        png = b"\x89PNG\r\n\x1a\n" + b"0" * 64
        batch_img, source_img = self.ingest("示意图.png", png)
        self.assertEqual(source_img["status"], "registered")
        outcome = self.db.kb2_publish_batch(TENANT, self.admin["id"], batch_img)
        self.assertEqual(len(outcome["archived"]), 1, outcome)
        published = self.published(source_img["item_id"])
        content = json.loads(published["content_json"])
        self.assertTrue(content.get("kb2_registered_only"))
        self.assertEqual(content.get("body", ""), "")

        batch_link = self.db.kb2_create_batch(
            TENANT, self.admin["id"], [{"kind": "link", "url": "https://example.com/spec"}])
        source_link = self.db.kb2_get_batch(TENANT, batch_link)["sources"][0]
        self.assertEqual(source_link["status"], "registered")
        outcome = self.db.kb2_publish_batch(TENANT, self.admin["id"], batch_link)
        self.assertEqual(len(outcome["archived"]), 1, outcome)

    def test_empty_text_replacement_archives_without_borrowed_body(self):
        """成功的「仅登记」替换（文本解析为空）可以归档，但不得留下旧正文。"""
        batch1, source1 = self.ingest("old2.txt", b"Power supply 220V")
        self.check_all(source1["id"])
        self.db.kb2_publish_batch(TENANT, self.admin["id"], batch1)
        item2 = source1["item_id"]
        replacement = self.db.kb2_replace_file(TENANT, self.admin["id"], item2, "empty.txt", b"   \n  ")
        self.assertEqual(replacement["status"], "registered")
        outcome = self.db.kb2_publish_batch(TENANT, self.admin["id"], replacement["batch_id"])
        self.assertEqual(len(outcome["archived"]), 1, outcome)
        published = self.published(item2)
        content = json.loads(published["content_json"])
        self.assertEqual(published["version"], 2)
        self.assertEqual(content.get("body", ""), "")
        self.assertNotIn("220V", content.get("body", ""))
        self.assertTrue(content.get("kb2_registered_only"))


if __name__ == "__main__":
    unittest.main()
