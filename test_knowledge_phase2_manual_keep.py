"""企业知识第二批 · B2-B02 回归：成员人工段落在重试 / 自动处理 / 恢复中不被覆盖。

背景（不覆盖历史定级记录）：Codex 独立复验把「pending/failed 来源中的人工段落被自动重建删除」
从非阻断重新定级为阻断（数据丢失）：人工段落 origin=manual、checked/edited/excluded 均为 0，
旧的「人工修改不重建」判定不认 origin，重建的 DELETE 会把成员写的正文清掉；且保护一旦触发，
异常会中断整轮扫描（其他来源被跳过、来源永远停在 processing）。

本文件锁定修复后的行为（判别力：把保护判定换回旧口径，下列断言会失败）：
1. 排队（pending）来源补充人工段落后再处理：人工段落保留原 ID / 文字 / 顺序，文件解析正文正常补充；
2. 失败（failed）来源的人工段落：重试后仍然保留，条目不重复创建；
3. 崩溃重启（processing→恢复）：人工段落保留，恢复入口接管完成，不重复建条目；
4. 单个来源出现意外内部错误：不中断整轮扫描（其他来源照常处理）、来源进入明确失败而不是
   永远 processing，且可再次重试成功；
5. 字段来源（钉住人工段落）在重处理后仍指向同一段落，位置标签可读；
6. 审计写入真实落盘：人工段落审计在第二个连接上可直接读到（不把写事务留在连接上）。

运行环境：系统 python3 与隔离环境 .venv-knowledge 均可（不依赖 PDF/DOCX 解析器）。
只用临时数据库与测试私有上传目录；不触碰 data/ 下真实演示数据与任何密钥。
"""
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from app import Database

TENANT = "demo-tenant"


class ManualKeepTestBase(unittest.TestCase):
    maxDiff = None

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.upload_dir = Path(self.tmp.name) / "uploads"
        os.environ["B2B_KB_STORAGE_ROOT"] = str(self.upload_dir)
        self.db_path = Path(self.tmp.name) / "knowledge2-manual.db"
        self.db = Database(self.db_path)
        self.db.knowledge_storage_root = self.upload_dir
        self.db.seed_demo_users()
        self.admin = self.db.authenticate_user("demo-admin@example.invalid", "demo-admin")

    def tearDown(self):
        self.db.conn.close()
        os.environ.pop("B2B_KB_STORAGE_ROOT", None)
        self.tmp.cleanup()

    # ── 帮助方法 ──────────────────────────────────────────────────────────

    def connect(self):
        db = Database(self.db_path)
        db.knowledge_storage_root = self.upload_dir
        return db

    def file_entry(self, filename, data, title=""):
        return {"kind": "file", "filename": filename, "data": data, "title": title}

    def source_by(self, batch, filename):
        return next(source for source in batch["sources"] if source["filename"] == filename)

    def paragraph_rows(self, source_id):
        return self.db.conn.execute(
            "SELECT * FROM kb2_paragraphs WHERE tenant_id=? AND source_id=? ORDER BY ordinal",
            (TENANT, source_id),
        ).fetchall()

    def item_count(self):
        return self.db.conn.execute(
            "SELECT COUNT(*) FROM knowledge_items WHERE tenant_id=?", (TENANT,)).fetchone()[0]

    def draft_body(self, item_id):
        draft = self.db.get_knowledge_draft_version(TENANT, item_id)
        self.assertIsNotNone(draft, "资料草稿应存在")
        import json as _json
        return _json.loads(draft["content_json"]).get("body", "")


class ManualParagraphProtectionTests(ManualKeepTestBase):
    """判别力核心：人工段落在各类自动处理路径下保持原样。"""

    def test_pending_manual_paragraph_survives_processing_and_queue_continues(self):
        """排队来源补充人工正文后再处理：人工段落保留、文件正文补充、其他来源继续处理。"""
        batch_id = self.db.kb2_create_batch(
            TENANT, self.admin["id"], [
                self.file_entry("保护.txt", "文件第一段。\n文件第二段。".encode()),
                self.file_entry("普通.txt", "普通文件正文。".encode()),
            ], process_now=False)
        batch = self.db.kb2_get_batch(TENANT, batch_id)
        protected = self.source_by(batch, "保护.txt")
        self.assertEqual(protected["status"], "pending")

        manual_id = self.db.kb2_add_manual_paragraph(
            TENANT, self.admin["id"], protected["id"], "成员提前补写的说明（不得丢失）。")
        field_id = self.db.kb2_set_source_field(
            TENANT, self.admin["id"], protected["id"],
            label="补充说明", value="成员填写", paragraph_id=manual_id)

        # 处理：不能抛异常、不能断整轮（另一来源也必须处理）；两个来源都到达终态。
        result = self.db.kb2_process_pending_sources(TENANT)
        self.assertEqual(result["processed"], 2)
        self.assertEqual(result["skipped"], 0)
        self.assertEqual(result.get("errored", 0), 0)

        rows = self.paragraph_rows(protected["id"])
        by_id = {row["id"]: row for row in rows}
        self.assertIn(manual_id, by_id, "人工段落被自动处理删除（数据丢失）")
        manual = by_id[manual_id]
        self.assertEqual(manual["text"], "成员提前补写的说明（不得丢失）。")
        self.assertEqual(manual["origin"], "manual")
        self.assertEqual(manual["source_kind"], "manual")
        # 文件正文正常补充（与人工段落并存）。
        texts = [row["text"] for row in rows]
        self.assertIn("文件第一段。", texts)
        self.assertIn("文件第二段。", texts)
        self.assertEqual(by_id[manual_id]["ordinal"], 0)  # 原段落顺序不被改写

        batch = self.db.kb2_get_batch(TENANT, batch_id)
        protected = self.source_by(batch, "保护.txt")
        self.assertEqual(protected["status"], "extracted")
        self.assertEqual(protected["identification"], "extracted")
        self.assertEqual(protected["counts"]["manual"], 1)
        # 字段来源仍钉住同一人工段落，位置标签可读。
        field = next(item for item in protected["fields"] if item["id"] == field_id)
        self.assertEqual(field["paragraph_id"], manual_id)
        self.assertEqual(field["paragraph_location_label"], "人工撰写")
        # 草稿正文同时含人工文字与文件正文（人工文字不被覆盖）。
        body = self.draft_body(protected["item_id"])
        self.assertIn("成员提前补写的说明（不得丢失）。", body)
        self.assertIn("文件第一段。", body)
        # 保护提示如实告诉成员：自动识别没有覆盖人工内容。
        self.assertTrue(any("人工" in warning or "成员" in warning for warning in protected["warnings"]),
                        f"应有保护提示，实际 warnings={protected['warnings']}")
        self.assertEqual(self.item_count(), 2)
        # 再扫一轮：没有来源卡在 processing，也没有重复处理。
        again = self.db.kb2_process_pending_sources(TENANT)
        self.assertEqual(again["processed"], 0)
        stuck = self.db.conn.execute(
            "SELECT COUNT(*) FROM kb2_sources WHERE tenant_id=? AND status='processing'", (TENANT,)).fetchone()[0]
        self.assertEqual(stuck, 0)

    def test_failed_source_retry_keeps_manual_and_single_item(self):
        """failed 来源补充人工正文后重试：人工段落保留、条目不重复、成功后终态。"""
        batch_id = self.db.kb2_create_batch(
            TENANT, self.admin["id"], [self.file_entry("重试保护.txt", "重试后的文件正文。".encode())],
            process_now=False)
        source = self.source_by(self.db.kb2_get_batch(TENANT, batch_id), "重试保护.txt")
        raw = self.db.conn.execute(
            "SELECT * FROM kb2_sources WHERE tenant_id=? AND id=?", (TENANT, source["id"])).fetchone()
        stored = Path(raw["storage_path"])
        payload = stored.read_bytes()
        stored.unlink()  # 模拟原件暂不可用：处理会明确失败

        self.db.kb2_process_pending_sources(TENANT)
        batch = self.db.kb2_get_batch(TENANT, batch_id)
        failed = self.source_by(batch, "重试保护.txt")
        self.assertEqual(failed["status"], "failed")
        self.assertIsNone(failed["item_id"])

        manual_id = self.db.kb2_add_manual_paragraph(
            TENANT, self.admin["id"], failed["id"], "失败期间成员补写的人工正文。")
        field_id = self.db.kb2_set_source_field(
            TENANT, self.admin["id"], failed["id"],
            label="备注", value="保持", paragraph_id=manual_id, checked=False)

        stored.write_bytes(payload)  # 原件恢复后重试
        self.db.kb2_retry_source(TENANT, self.admin["id"], failed["id"])

        batch = self.db.kb2_get_batch(TENANT, batch_id)
        retried = self.source_by(batch, "重试保护.txt")
        self.assertEqual(retried["status"], "extracted")
        rows = {row["id"]: row for row in self.paragraph_rows(retried["id"])}
        self.assertIn(manual_id, rows, "重试清掉了人工段落（数据丢失）")
        self.assertEqual(rows[manual_id]["text"], "失败期间成员补写的人工正文。")
        self.assertIn("重试后的文件正文。", [row["text"] for row in rows.values()])
        field = next(item for item in retried["fields"] if item["id"] == field_id)
        self.assertEqual(field["paragraph_id"], manual_id)
        body = self.draft_body(retried["item_id"])
        self.assertIn("失败期间成员补写的人工正文。", body)
        self.assertIn("重试后的文件正文。", body)
        self.assertEqual(self.item_count(), 1)
        with self.assertRaises(ValueError):
            self.db.kb2_retry_source(TENANT, self.admin["id"], retried["id"])

    def test_crash_restart_recovery_keeps_manual(self):
        """崩溃重启（processing 恢复）：人工段落保留，恢复入口接管完成，条目不重复。"""
        class Crash(BaseException):
            pass

        batch_id = self.db.kb2_create_batch(
            TENANT, self.admin["id"], [self.file_entry("崩溃保护.txt", "崩溃恢复的文件正文。".encode())],
            process_now=False)
        source = self.source_by(self.db.kb2_get_batch(TENANT, batch_id), "崩溃保护.txt")
        manual_id = self.db.kb2_add_manual_paragraph(
            TENANT, self.admin["id"], source["id"], "崩溃重启后必须保留的人工正文。")

        def crashing_extract(name, data):
            raise Crash("重启前模拟崩溃")

        with patch.object(__import__("knowledge_ingestion"), "extract_document", crashing_extract):
            with self.assertRaises(Crash):
                self.db.kb2_process_pending_sources(TENANT)
        stuck = self.db.conn.execute(
            "SELECT * FROM kb2_sources WHERE tenant_id=? AND id=?", (TENANT, source["id"])).fetchone()
        self.assertEqual(stuck["status"], "processing")
        self.assertIn(manual_id, {row["id"] for row in self.paragraph_rows(source["id"])},
                      "崩溃中断不得丢失已提交的人工段落")

        result = self.db.kb2_process_pending_sources(TENANT)  # 恢复入口接管
        self.assertEqual(result["processed"], 1)
        recovered = self.db.conn.execute(
            "SELECT * FROM kb2_sources WHERE tenant_id=? AND id=?", (TENANT, source["id"])).fetchone()
        self.assertEqual(recovered["status"], "extracted")
        rows = {row["id"]: row for row in self.paragraph_rows(source["id"])}
        self.assertIn(manual_id, rows)
        self.assertEqual(rows[manual_id]["text"], "崩溃重启后必须保留的人工正文。")
        self.assertIn("崩溃恢复的文件正文。", [row["text"] for row in rows.values()])
        body = self.draft_body(recovered["item_id"])
        self.assertIn("崩溃重启后必须保留的人工正文。", body)
        self.assertEqual(self.item_count(), 1)

    def test_unexpected_error_does_not_break_queue_or_wedge_source(self):
        """单个来源意外内部错误：不中断整轮扫描、不永远 processing、可再次重试成功。"""
        batch_id = self.db.kb2_create_batch(
            TENANT, self.admin["id"], [
                self.file_entry("坏甲.txt", "甲文件正文。".encode()),
                self.file_entry("好乙.txt", "乙文件正文。".encode()),
            ], process_now=False)
        rows = {row["filename"]: row for row in self.db.conn.execute(
            "SELECT * FROM kb2_sources WHERE tenant_id=? AND batch_id=?", (TENANT, batch_id)).fetchall()}
        # 固定扫描顺序：坏甲排在前（ORDER BY created_at, id），确保它先被处理。
        self.db.conn.execute(
            "UPDATE kb2_sources SET created_at='2020-01-01T00:00:00+00:00' WHERE id=?", (rows["坏甲.txt"]["id"],))
        self.db.conn.commit()

        real_create = self.db._kb2_create_material_item

        def failing_create(tenant_id, source, title):
            if source["filename"] == "坏甲.txt":
                raise RuntimeError("模拟内部错误（非解析失败）")
            return real_create(tenant_id, source, title)

        self.db._kb2_create_material_item = failing_create
        try:
            result = self.db.kb2_process_pending_sources(TENANT)
        finally:
            self.db._kb2_create_material_item = real_create

        self.assertEqual(result["processed"], 1, "其他来源必须继续处理")
        self.assertEqual(result.get("errored"), 1)
        batch = self.db.kb2_get_batch(TENANT, batch_id)
        self.assertEqual(self.source_by(batch, "好乙.txt")["status"], "extracted")
        bad = self.source_by(batch, "坏甲.txt")
        self.assertEqual(bad["status"], "failed", "意外错误来源应进入明确失败而不是永远 processing")
        self.assertIn("内部错误", bad["error"])
        stuck = self.db.conn.execute(
            "SELECT COUNT(*) FROM kb2_sources WHERE tenant_id=? AND status='processing'", (TENANT,)).fetchone()[0]
        self.assertEqual(stuck, 0)
        # 失败后仍可再次重试成功（不是一次意外就永久失效）。
        self.db.kb2_retry_source(TENANT, self.admin["id"], bad["id"])
        batch = self.db.kb2_get_batch(TENANT, batch_id)
        self.assertEqual(self.source_by(batch, "坏甲.txt")["status"], "extracted")
        self.assertEqual(self.item_count(), 2)

    def test_manual_audit_committed_without_hidden_transaction(self):
        """审计真实落盘：人工段落审计在第二个连接上可直接读到；处理审计同样可见。"""
        batch_id = self.db.kb2_create_batch(
            TENANT, self.admin["id"], [self.file_entry("审计.txt", "审计校验正文。".encode())],
            process_now=False)
        source = self.source_by(self.db.kb2_get_batch(TENANT, batch_id), "审计.txt")
        self.db.kb2_add_manual_paragraph(TENANT, self.admin["id"], source["id"], "审计校验人工正文。")

        other = self.connect()
        try:
            manual_audit = other.conn.execute(
                "SELECT COUNT(*) FROM audit_log WHERE tenant_id=? AND action='manual_paragraph_added'",
                (TENANT,)).fetchone()[0]
            self.assertEqual(manual_audit, 1, "人工段落审计未提交落盘（写事务被留在连接上）")
        finally:
            other.conn.close()

        self.db.kb2_process_pending_sources(TENANT)
        other = self.connect()
        try:
            processed_audit = other.conn.execute(
                "SELECT details FROM audit_log WHERE tenant_id=? AND action='source_processed'",
                (TENANT,)).fetchall()
            self.assertTrue(processed_audit, "处理审计未落盘")
            self.assertTrue(any("kept" in row["details"] or "manual" in row["details"] for row in processed_audit),
                            f"保留人工段落的处理应在审计中可查，实际={[row['details'] for row in processed_audit]}")
        finally:
            other.conn.close()


if __name__ == "__main__":
    unittest.main()
