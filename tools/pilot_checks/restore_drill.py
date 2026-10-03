#!/usr/bin/env python3
"""恢复彩排（干跑）：备份 → 隔离恢复 → 数据对照，记录耗时。

用法:
  python3 tools/pilot_checks/restore_drill.py --db data/slice-m1m2.db --uploads data/uploads

说明：本脚本在本机演练完整流程并核对恢复后计数与源库一致；托管环境的真实彩排
（真删真恢复、双人见证）在试点前另行执行，本脚本即其自动化核对步骤。
"""

from __future__ import annotations
import argparse
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
COUNTS = {
    "会话": "SELECT COUNT(*) FROM conversations",
    "消息": "SELECT COUNT(*) FROM messages",
    "需求修订": "SELECT COUNT(*) FROM requirement_revisions",
    "推荐记录": "SELECT COUNT(*) FROM recommendations",
    "RFQ": "SELECT COUNT(*) FROM rfqs",
    "已发布版本": "SELECT COUNT(*) FROM product_versions WHERE status='published'",
    "已开放文件许可": "SELECT COUNT(*) FROM public_source_permissions",
}


def counts(db_path: Path) -> dict[str, int]:
    conn = sqlite3.connect(str(db_path))
    result = {name: conn.execute(sql).fetchone()[0] for name, sql in COUNTS.items()}
    conn.close()
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--uploads", type=Path, required=True)
    parser.add_argument("--keep", type=Path, help="可选的保留目录：把备份与恢复产物复制到此处供人工复核")
    args = parser.parse_args()

    started = time.monotonic()
    before = counts(args.db)
    with tempfile.TemporaryDirectory(prefix="restore-drill-") as temporary:
        bundle = Path(temporary) / "bundle"
        restored = Path(temporary) / "restored"
        subprocess.run([sys.executable, str(ROOT / "tools/backup_restore.py"), "backup",
                        "--db", str(args.db), "--uploads", str(args.uploads), "--out", str(bundle)], check=True)
        subprocess.run([sys.executable, str(ROOT / "tools/backup_restore.py"), "restore-check",
                        "--bundle", str(bundle), "--out", str(restored)], check=True)
        after = counts(restored / "app.db")
        if args.keep:
            args.keep.mkdir(parents=True, exist_ok=True)
            shutil.copytree(bundle, args.keep / "bundle", dirs_exist_ok=True)
            shutil.copytree(restored, args.keep / "restored", dirs_exist_ok=True)
    elapsed = time.monotonic() - started

    ok = before == after
    for name in before:
        marker = "" if before[name] == after[name] else "  <= 不一致"
        print(f"{name}: 源 {before[name]} / 恢复 {after[name]}{marker}")
    print(f"全程耗时: {elapsed:.1f}s")
    print("结果：", "PASS — 备份可恢复，且恢复后数据与源库逐项一致" if ok else "FAIL — 恢复后计数不一致，需排查")
    raise SystemExit(0 if ok else 1)


if __name__ == "__main__":
    main()
