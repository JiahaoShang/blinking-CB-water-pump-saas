#!/usr/bin/env python3
"""旧库迁移核查：在数据库副本上运行迁移，检查关键列/索引齐备，并检测会阻塞唯一索引的历史重复数据。

用法:
  python3 tools/pilot_checks/check_db_migrations.py --db data/slice-m1m2.db
  python3 tools/pilot_checks/check_db_migrations.py --db data/m2-browser.db

说明：脚本只操作副本，不会改动原库；迁移在副本上真实执行后逐项核对。
"""

from __future__ import annotations
import argparse
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from app import Database  # noqa: E402

EXPECTED_COLUMNS = {
    "messages": ["idempotency_key", "request_hash", "result_revision_id"],
    "recommendations": ["tentative_viewed", "idempotency_key"],
    "requirement_revisions": ["idempotency_key", "request_hash"],
    "source_documents": ["scope", "extraction_json", "extraction_note", "processed_at"],
    "product_fields": ["state", "origin", "review_note"],
    "human_tasks": ["question_message_id"],
}
EXPECTED_INDEXES = [
    "idx_messages_idempotency",
    "idx_revisions_idempotency",
    "idx_recommendations_idempotency",
    "idx_answers_question",
    "idx_human_tasks_question",
]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, required=True)
    args = parser.parse_args()

    failures: list[str] = []
    with tempfile.TemporaryDirectory(prefix="migration-check-") as temporary:
        copy = Path(temporary) / "copy.db"
        shutil.copy2(args.db, copy)
        Database(copy)  # 真跑一遍迁移
        conn = sqlite3.connect(str(copy))
        for table, columns in EXPECTED_COLUMNS.items():
            actual = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
            if not actual:
                failures.append(f"表缺失:{table}")
                print(f"[FAIL] 表 {table} 不存在")
                continue
            for column in columns:
                ok = column in actual
                print(f"[{'PASS' if ok else 'FAIL'}] {table}.{column}")
                if not ok:
                    failures.append(f"{table}.{column}")
        indexes = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")}
        for index in EXPECTED_INDEXES:
            ok = index in indexes
            print(f"[{'PASS' if ok else 'FAIL'}] 索引 {index}")
            if not ok:
                failures.append(f"index:{index}")
        duplicate_tasks = conn.execute(
            "SELECT COUNT(*) FROM (SELECT question_message_id FROM human_tasks GROUP BY question_message_id HAVING COUNT(*) > 1)"
        ).fetchone()[0]
        duplicate_answers = conn.execute(
            "SELECT COUNT(*) FROM (SELECT question_message_id FROM answers GROUP BY question_message_id HAVING COUNT(*) > 1)"
        ).fetchone()[0]
        print(f"历史重复数据：同问题重复人工任务 {duplicate_tasks} 组；同问题重复答案 {duplicate_answers} 组")
        if duplicate_tasks or duplicate_answers:
            print("提示：存在重复时对应唯一索引可能被跳过（保证运行不中断），需人工合并重复记录后重跑本脚本。")
        conn.close()

    print("结果：", "PASS — 迁移后关键结构与唯一索引齐备" if not failures else f"FAIL — {len(failures)} 项缺失")
    raise SystemExit(0 if not failures else 1)


if __name__ == "__main__":
    main()
