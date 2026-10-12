"""企业知识第二批 · 并发阻断（B2-B01）修复回归：独占处理认领、排队入口与崩溃恢复。

背景（不覆盖原复现理由）：Codex 独立审查用双线程/双连接复现「同一来源两个处理入口并发」
（同步创建入口 + worker 扫描入口）造成 parse_calls=2、knowledge_items=2（数据完整性阻断）。
原复现脚本用双人 Barrier 同步两个解析者；修复后只允许一个执行者进入解析，
原脚本的 Barrier 会超时——这是预期的行为变化，不代表功能退化。本文件用事件阻塞
第一个解析者、让第二个入口尝试处理再释放，做确定性回归。

覆盖（全部实现在数据层与本进程认领表，非文档约定）：
- 同步入口与 worker 入口并发处理同一来源：解析只执行一次、知识条目只有一个；
- 活动 processing 不被另一线程抢执行（第二入口尝试处理时被独占认领挡住）；
- 排队入口 kb2_create_batch(process_now=False)：立即返回 batch_id、请求内不解析；
- 真实入库线程（start_knowledge_phase2_worker）处理排队批次；
- 接管规则：属主进程已退出或租约过期才可接管；属主存活且租约有效不抢执行；
- 崩溃（BaseException）恢复：来源保持 processing（认领信息在数据层），
  恢复入口立即接管完成，条目不重复、重试幂等；
- 排队路径不改变原行为：文字解析、明确失败、核对、建议确认与发布照常。

运行环境：系统 python3 与隔离环境 .venv-knowledge 均可（本文件不依赖 PDF/DOCX 解析器；
不用子进程处理真实文件，除「属主进程存活」判定用短命进程）。只用临时数据库与测试私有
上传目录；不触碰 data/ 下真实演示数据与任何密钥。
"""
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from app import Database
import knowledge_ingestion
from enterprise_knowledge_phase2 import start_knowledge_phase2_worker

TENANT = "demo-tenant"


class ConcurrencyTestBase(unittest.TestCase):
    maxDiff = None

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.upload_dir = Path(self.tmp.name) / "uploads"
        os.environ["B2B_KB_STORAGE_ROOT"] = str(self.upload_dir)
        self.db_path = Path(self.tmp.name) / "knowledge2.db"
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
        """独立数据库连接（同一文件）：模拟 worker / HTTP 请求各自的连接。"""
        db = Database(self.db_path)
        db.knowledge_storage_root = self.upload_dir
        return db

    def file_entry(self, filename, data, title=""):
        return {"kind": "file", "filename": filename, "data": data, "title": title}

    def counts(self):
        return {
            "sources": self.db.conn.execute("SELECT COUNT(*) FROM kb2_sources").fetchone()[0],
            "items": self.db.conn.execute("SELECT COUNT(*) FROM knowledge_items").fetchone()[0],
        }

    def source_rows(self, batch_id):
        return self.db.conn.execute(
            "SELECT * FROM kb2_sources WHERE batch_id=? ORDER BY filename", (batch_id,)).fetchall()

    # ── 并发回归（原 B2-B01 场景的确定性版本）──────────────────────────────

    def test_concurrent_sync_and_worker_only_one_parser_and_single_item(self):
        """同步创建入口与 worker 入口并发处理同一来源：解析只一次、条目只一个、成功收尾、恢复幂等。"""
        entered = threading.Event()
        release = threading.Event()
        calls = []

        def blocking_extract(name, data):
            calls.append(name)
            entered.set()
            release.wait(15)
            return {
                "status": "extracted", "format": "txt",
                "paragraphs": [{"text": "并发验收资料", "location": {"kind": "line", "line": 1},
                                "source_kind": "text"}],
                "warnings": [], "error": "",
            }

        outcome = {}

        def sync_entry():
            db = self.connect()
            try:
                outcome["batch_id"] = db.kb2_create_batch(
                    TENANT, self.admin["id"],
                    [self.file_entry("并发验收.txt", "并发验收资料".encode())])
            except BaseException as error:  # 线程内异常回收，避免静默
                outcome["error"] = repr(error)
            finally:
                db.conn.close()

        with patch.object(knowledge_ingestion, "extract_document", blocking_extract):
            sync_thread = threading.Thread(target=sync_entry, name="kb2-sync-entry")
            sync_thread.start()
            try:
                self.assertTrue(entered.wait(15), "同步入口未能进入解析（事件等待超时）")
                # 第二个入口（worker 扫描路径，独立连接）在第一个解析进行中尝试处理同一来源。
                worker = self.connect()
                try:
                    result = worker.kb2_process_pending_sources(TENANT)
                finally:
                    worker.conn.close()
                # 独占认领：第二入口被挡住——没有第二次解析、没有第二个条目。
                self.assertEqual(result["processed"], 0)
                self.assertEqual(result["skipped"], 1)
                self.assertEqual(len(calls), 1)
                # 重试入口同样不能抢占活动 processing：明确拒绝而不是重新解析。
                source_id = self.db.conn.execute("SELECT id FROM kb2_sources").fetchone()[0]
                with self.assertRaisesRegex(ValueError, "正在处理中"):
                    self.db.kb2_retry_source(TENANT, self.admin["id"], source_id)
                self.assertEqual(len(calls), 1)
                self.assertEqual(
                    self.db.conn.execute("SELECT status FROM kb2_sources").fetchone()[0], "processing")
            finally:
                release.set()
                sync_thread.join(20)
        self.assertFalse(sync_thread.is_alive(), "同步入口未在释放后结束")
        self.assertNotIn("error", outcome)
        self.assertTrue(outcome.get("batch_id"))
        # 解析只执行一次；知识条目只有一个；来源成功、条目为草稿。
        self.assertEqual(len(calls), 1)
        self.assertEqual(self.counts(), {"sources": 1, "items": 1})
        source = self.db.conn.execute("SELECT * FROM kb2_sources").fetchone()
        self.assertEqual(source["status"], "extracted")
        self.assertEqual(source["attempts"], 1)  # 第二入口未认领（不重复解析、不抢执行）
        self.assertIsNone(source["processing_owner"])  # 完成后释放认领
        item = self.db.get_knowledge_item(TENANT, source["item_id"])
        self.assertEqual(item["status"], "draft")
        # 恢复仍幂等：再扫描不重复处理、不重复建。
        again = self.db.kb2_process_pending_sources(TENANT)
        self.assertEqual(again["processed"], 0)
        self.assertEqual(self.counts(), {"sources": 1, "items": 1})

    # ── 排队入口（HTTP 不阻塞）───────────────────────────────────────────

    def test_enqueue_mode_returns_immediately_and_worker_entry_processes(self):
        """process_now=False：创建批次立即返回 batch_id、请求内不解析；worker 入口真实处理。"""
        calls = []
        real_extract = knowledge_ingestion.extract_document

        def slow_extract(name, data):
            calls.append(name)
            time.sleep(0.8)  # 若入口同步解析，耗时必然 ≥0.8s
            return real_extract(name, data)

        with patch.object(knowledge_ingestion, "extract_document", slow_extract):
            started = time.time()
            batch_id = self.db.kb2_create_batch(
                TENANT, self.admin["id"],
                [self.file_entry("排队.txt", "排队处理的正文。".encode())],
                process_now=False)
            elapsed = time.time() - started
            self.assertLess(elapsed, 0.5, "排队入口在请求内执行了解析（不阻塞目标未达成）")
            self.assertEqual(calls, [])
            queued = self.db.kb2_get_batch(TENANT, batch_id)
            self.assertEqual(queued["sources"][0]["status"], "pending")
            self.assertIsNone(queued["sources"][0]["item_id"])
            # worker 入口（独立连接）真实处理排队批次。
            worker = self.connect()
            try:
                result = worker.kb2_process_pending_sources(TENANT)
            finally:
                worker.conn.close()
        self.assertEqual(result["processed"], 1)
        self.assertEqual(len(calls), 1)
        refreshed = self.db.kb2_get_batch(TENANT, batch_id)
        self.assertEqual(refreshed["sources"][0]["status"], "extracted")
        self.assertEqual(refreshed["sources"][0]["item_status"], "draft")
        self.assertEqual(self.counts(), {"sources": 1, "items": 1})

    def test_background_worker_thread_processes_enqueued_batch(self):
        """真实入库线程（start_knowledge_phase2_worker）处理排队批次。"""
        batch_id = self.db.kb2_create_batch(
            TENANT, self.admin["id"],
            [{"kind": "manual", "text": "后台 worker 处理的人工文字。", "title": "后台样例"}],
            process_now=False)
        self.assertEqual(self.source_rows(batch_id)[0]["status"], "pending")
        stop = start_knowledge_phase2_worker(self.db_path, interval=1)
        try:
            deadline = time.time() + 20
            status = "pending"
            while time.time() < deadline:
                status = self.source_rows(batch_id)[0]["status"]
                if status != "pending":
                    break
                time.sleep(0.1)
        finally:
            stop.set()
        self.assertEqual(status, "extracted")
        source = self.source_rows(batch_id)[0]
        self.assertTrue(source["item_id"])
        item = self.db.get_knowledge_item(TENANT, source["item_id"])
        self.assertEqual(item["status"], "draft")
        self.assertEqual(self.counts(), {"sources": 1, "items": 1})

    # ── 接管规则（安全崩溃恢复 / 不抢执行）───────────────────────────────

    def test_processing_claims_takeover_rules(self):
        """处理中行的接管判定（真实逻辑）：
        - 属主进程已退出（即使租约未过期）→ 可接管；
        - 属主进程存活且租约有效 → 不抢执行（保持原样、不解析、不建条目）；
        - 属主信息缺失（迁移前旧数据）→ 可接管；
        - 租约过期（无法用进程存活判断的属主）→ 可接管；租约未过期 → 不接管。"""
        batch_id = self.db.kb2_create_batch(
            TENANT, self.admin["id"], [
                self.file_entry("接管甲.txt", "甲文。".encode()),
                self.file_entry("接管乙.txt", "乙文。".encode()),
                self.file_entry("接管丙.txt", "丙文。".encode()),
            ], process_now=False)
        rows = {row["filename"]: row for row in self.source_rows(batch_id)}

        dead = subprocess.Popen([sys.executable, "-c", "pass"])
        dead.wait()  # 已退出、已被回收的进程：属主失活
        live = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        now = time.time()
        host = socket.gethostname()
        try:
            # 甲：属主进程已退出 + 租约未过期 → 应接管。
            self.db.conn.execute(
                "UPDATE kb2_sources SET status='processing', processing_owner=?, processing_lease_until=? WHERE id=?",
                (f"{host}:{dead.pid}:7:deadbeef", now + 3600, rows["接管甲.txt"]["id"]))
            # 乙：属主进程存活 + 租约未过期 → 不应接管。
            self.db.conn.execute(
                "UPDATE kb2_sources SET status='processing', processing_owner=?, processing_lease_until=? WHERE id=?",
                (f"{host}:{live.pid}:7:livebeef", now + 3600, rows["接管乙.txt"]["id"]))
            # 丙：迁移前旧数据（无属主/租约信息）→ 应接管。
            self.db.conn.execute(
                "UPDATE kb2_sources SET status='processing', processing_owner=NULL, processing_lease_until=NULL WHERE id=?",
                (rows["接管丙.txt"]["id"],))
            self.db.conn.commit()

            # 租约规则（直接判定真实函数；其他主机属主无法用进程存活判断）。
            self.assertTrue(self.db._kb2_stale_claim_takeover_allowed("other-host:12345:1:xx", now - 1, now))
            self.assertFalse(self.db._kb2_stale_claim_takeover_allowed("other-host:12345:1:xx", now + 999, now))

            result = self.db.kb2_process_pending_sources(TENANT)
            self.assertEqual(result["processed"], 2)   # 甲、丙被接管处理
            self.assertEqual(result["skipped"], 1)     # 乙被跳过（不抢执行）
            after = {row["filename"]: row for row in self.source_rows(batch_id)}
            self.assertEqual(after["接管甲.txt"]["status"], "extracted")
            self.assertEqual(after["接管甲.txt"]["attempts"], 1)
            self.assertEqual(after["接管丙.txt"]["status"], "extracted")
            self.assertEqual(after["接管丙.txt"]["attempts"], 1)
            # 乙保持原样：仍是 processing、未解析、未建条目、attempts 不变。
            self.assertEqual(after["接管乙.txt"]["status"], "processing")
            self.assertEqual(after["接管乙.txt"]["attempts"], 0)
            self.assertIsNone(after["接管乙.txt"]["item_id"])
            self.assertEqual(after["接管乙.txt"]["processing_owner"], f"{host}:{live.pid}:7:livebeef")

            # 属主退出后（用租约过期路径：其他主机属主）→ 接管完成。
            self.db.conn.execute(
                "UPDATE kb2_sources SET processing_owner='other-host:12345:1:deadbeef', "
                "processing_lease_until=? WHERE id=?",
                (now - 5, rows["接管乙.txt"]["id"]))
            self.db.conn.commit()
            result2 = self.db.kb2_process_pending_sources(TENANT)
            self.assertEqual(result2["processed"], 1)
            after2 = {row["filename"]: row for row in self.source_rows(batch_id)}
            self.assertEqual(after2["接管乙.txt"]["status"], "extracted")
        finally:
            live.kill()
            live.wait()
        # 每个来源一个知识条目：共 3 个，无重复。
        self.assertEqual(self.counts(), {"sources": 3, "items": 3})

    # ── 崩溃恢复（BaseException）──────────────────────────────────────────

    def test_crash_recovery_under_claim_is_immediate_and_idempotent(self):
        """崩溃中止解析后：来源保持 processing（认领信息在数据层），恢复入口立即接管完成；
        不重复创建条目，成功后重试被拒绝（幂等）。"""
        class Crash(BaseException):
            pass

        def crashing_extract(name, data):
            raise Crash("重启前模拟崩溃")

        outcome = {}

        def crashing_entry():
            db = self.connect()
            try:
                db.kb2_create_batch(
                    TENANT, self.admin["id"],
                    [self.file_entry("崩溃恢复.txt", "恢复内容。".encode())])
            except BaseException as error:
                outcome["error"] = error
            finally:
                db.conn.close()

        with patch.object(knowledge_ingestion, "extract_document", crashing_extract):
            crash_thread = threading.Thread(target=crashing_entry, name="kb2-crash-entry")
            crash_thread.start()
            crash_thread.join(20)
        self.assertFalse(crash_thread.is_alive())
        self.assertIsInstance(outcome.get("error"), Crash)
        stuck = self.db.conn.execute("SELECT * FROM kb2_sources").fetchone()
        self.assertEqual(stuck["status"], "processing")  # 崩溃后留在处理中（可安全恢复）
        self.assertTrue(stuck["processing_owner"])       # 认领信息在数据层，不靠进程内存
        self.assertEqual(stuck["attempts"], 1)
        # 同进程恢复：原执行线程已终止 → 立即接管（无需等租约），条目不重复。
        result = self.db.kb2_process_pending_sources(TENANT)
        self.assertGreaterEqual(result["processed"], 1)
        recovered = self.db.conn.execute("SELECT * FROM kb2_sources").fetchone()
        self.assertEqual(recovered["status"], "extracted")
        self.assertEqual(recovered["attempts"], 2)
        self.assertIsNone(recovered["processing_owner"])
        self.assertEqual(self.counts(), {"sources": 1, "items": 1})
        # 重试幂等：已成功处理的来源不可再重试；再扫描不重复处理。
        with self.assertRaises(ValueError):
            self.db.kb2_retry_source(TENANT, self.admin["id"], recovered["id"])
        self.assertEqual(self.db.kb2_process_pending_sources(TENANT)["processed"], 0)
        self.assertEqual(self.counts(), {"sources": 1, "items": 1})

    # ── 排队路径保留原有行为 ─────────────────────────────────────────────

    def test_enqueued_batch_preserves_parse_failure_check_suggest_publish(self):
        """排队路径不改变原行为：文字解析、明确失败、核对、建议确认与发布照常（单失败不阻断）。"""
        batch_id = self.db.kb2_create_batch(
            TENANT, self.admin["id"], [
                self.file_entry("排队条款.txt", "保修政策：整机保修 24 个月。\n第二条 响应 48 小时。".encode()),
                {"kind": "link", "url": "https://example.invalid/queued", "title": "排队链接"},
                self.file_entry("坏文件.txt", b"abc\x00def"),
            ], process_now=False)
        worker = self.connect()
        try:
            result = worker.kb2_process_pending_sources(TENANT)
        finally:
            worker.conn.close()
        self.assertEqual(result["processed"], 3)
        batch = self.db.kb2_get_batch(TENANT, batch_id)
        by_name = {source["filename"]: source for source in batch["sources"]}
        good = by_name["排队条款.txt"]
        self.assertEqual(good["status"], "extracted")
        self.assertEqual([p["text"] for p in good["paragraphs"]],
                         ["保修政策：整机保修 24 个月。", "第二条 响应 48 小时。"])
        self.assertEqual(by_name["https://example.invalid/queued"]["status"], "registered")
        bad = by_name["坏文件.txt"]
        self.assertEqual(bad["status"], "failed")
        self.assertTrue(bad["error"])
        self.assertIsNone(bad["item_id"])
        # 核对（未核对不能发布）。
        for paragraph in good["paragraphs"]:
            self.db.kb2_update_paragraph(TENANT, self.admin["id"], paragraph["id"], checked=True)
        # 建议：真实文字命中现有主题 + 成员输入新主题；确认前不建立。
        topic_id = self.db.kb2_create_topic(TENANT, self.admin["id"], "保修政策")
        suggestions = self.db.kb2_build_topic_suggestions(TENANT, self.admin["id"], batch_id)
        matches = [s for s in suggestions if s["status"] == "pending" and s["existing_topic_id"] == topic_id]
        self.assertEqual([s["source_ids"] for s in matches], [[good["id"]]])
        # 任务3 补齐（2026-10-12 用户要求）：新主题候选来自资料实际标题（basis=source_title），
        # 仍是候选、待人工确认；解析失败的来源（坏文件.txt）不产出候选。
        title_candidates = {s["name"]: s for s in suggestions if s["basis"] == "source_title"}
        self.assertEqual(set(title_candidates), {"排队条款", "排队链接"})
        for candidate in title_candidates.values():
            self.assertIsNone(candidate["existing_topic_id"])
            self.assertEqual(candidate["status"], "pending")
        self.db.kb2_add_topic_suggestion(TENANT, self.admin["id"], batch_id, "排队与响应")
        confirm = self.db.kb2_confirm_batch_topics(TENANT, self.admin["id"], batch_id)
        self.assertEqual({topic["title"] for topic in confirm["topics_created"]},
                         {"排队与响应", "排队条款", "排队链接"})
        # 发布：正文型发布、仅登记归档；失败来源不阻断其他资料。
        summary = self.db.kb2_publish_batch(TENANT, self.admin["id"], batch_id)
        self.assertEqual({entry["source_id"] for entry in summary["published"]}, {good["id"]})
        self.assertEqual({entry["source_id"] for entry in summary["archived"]},
                         {by_name["https://example.invalid/queued"]["id"]})
        self.assertEqual({entry["source_id"] for entry in summary["failed"]}, {bad["id"]})
        published = self.db.get_published_knowledge_version(TENANT, good["item_id"])
        self.assertIn("响应 48 小时。", json.loads(published["content_json"])["body"])
        # 重复发布幂等：不产生重复版本。
        before = len(self.db.list_knowledge_versions(TENANT, good["item_id"]))
        again = self.db.kb2_publish_batch(TENANT, self.admin["id"], batch_id)
        after = len(self.db.list_knowledge_versions(TENANT, good["item_id"]))
        self.assertEqual(before, after)
        self.assertTrue(again["skipped"])
        self.assertEqual(self.counts(), {"sources": 3, "items": 2})  # 失败来源不建条目：正文 1 + 仅登记 1


if __name__ == "__main__":
    unittest.main()
