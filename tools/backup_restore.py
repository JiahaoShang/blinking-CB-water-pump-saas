#!/usr/bin/env python3
"""Create a consistent SQLite backup and verify it with uploaded source files."""

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import tempfile
from datetime import datetime, timezone
from pathlib import Path


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def restrict_permissions(root: Path) -> None:
    """备份/恢复产物只允许属主访问：目录 0700、文件 0600，不随进程 umask 放宽。"""
    os.chmod(root, 0o700)
    for path in root.rglob("*"):
        os.chmod(path, 0o700 if path.is_dir() else 0o600)


def check_source_files(database: Path, uploads: Path) -> int:
    with sqlite3.connect(str(database)) as conn:
        rows = conn.execute("SELECT storage_path, sha256 FROM source_documents WHERE storage_path IS NOT NULL").fetchall()
    for storage_path, expected in rows:
        path = Path(storage_path)
        if path.parts[:2] != ("data", "uploads") or len(path.parts) != 3:
            raise ValueError(f"不支持的资料存储路径: {storage_path}")
        target = uploads / path.name
        if not target.is_file() or (expected and digest(target) != expected):
            raise ValueError(f"资料缺失或校验失败: {storage_path}")
    return len(rows)


def backup(database: Path, uploads: Path, destination: Path) -> None:
    if destination.exists():
        raise ValueError("备份目标已存在")
    if not database.is_file():
        raise ValueError("数据库不存在")
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=destination.parent) as temporary:
        bundle = Path(temporary) / "bundle"
        bundle.mkdir()
        with sqlite3.connect(str(database)) as source, sqlite3.connect(str(bundle / "app.db")) as target:
            source.backup(target)
        if uploads.exists():
            shutil.copytree(uploads, bundle / "uploads")
        else:
            (bundle / "uploads").mkdir()
        check_source_files(bundle / "app.db", bundle / "uploads")
        files = {str(path.relative_to(bundle)): digest(path) for path in bundle.rglob("*") if path.is_file()}
        manifest = {"created_at": datetime.now(timezone.utc).isoformat(), "files": files}
        (bundle / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        # 备份含业务库与全部原件：先把整棵产物收紧到属主可读，再移出临时目录。
        restrict_permissions(bundle)
        bundle.rename(destination)


def restore_and_verify(bundle: Path, destination: Path) -> tuple[int, int]:
    if destination.exists():
        raise ValueError("恢复目标已存在，必须使用隔离的空目录")
    manifest = json.loads((bundle / "manifest.json").read_text(encoding="utf-8"))
    actual_files = {str(path.relative_to(bundle)) for path in bundle.rglob("*") if path.is_file() and path.name != "manifest.json"}
    if actual_files != set(manifest["files"]):
        raise ValueError("备份文件清单与实际文件不一致")
    for name, expected in manifest["files"].items():
        path = bundle / name
        if path.resolve().is_relative_to(bundle.resolve()) is False or not path.is_file() or digest(path) != expected:
            raise ValueError(f"备份文件校验失败: {name}")
    shutil.copytree(bundle, destination)
    restrict_permissions(destination)
    with sqlite3.connect(str(destination / "app.db")) as conn:
        if conn.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise ValueError("恢复数据库完整性检查失败")
        records = conn.execute("SELECT COUNT(*) FROM rfqs").fetchone()[0]
    sources = check_source_files(destination / "app.db", destination / "uploads")
    return records, sources


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)
    create = sub.add_parser("backup")
    create.add_argument("--db", type=Path, required=True)
    create.add_argument("--uploads", type=Path, required=True)
    create.add_argument("--out", type=Path, required=True)
    recover = sub.add_parser("restore-check")
    recover.add_argument("--bundle", type=Path, required=True)
    recover.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    if args.action == "backup":
        backup(args.db, args.uploads, args.out)
        print(f"备份完成: {args.out}")
    else:
        records, sources = restore_and_verify(args.bundle, args.out)
        print(f"隔离恢复完成: RFQ {records} 条；有文件的资料 {sources} 份；路径 {args.out}")


if __name__ == "__main__":
    main()
