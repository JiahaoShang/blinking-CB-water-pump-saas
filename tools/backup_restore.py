#!/usr/bin/env python3
"""Create a consistent SQLite backup and verify it with uploaded source files.

覆盖范围（T4 清单实测口径）：
- 业务数据库 app.db：SQLite 在线备份接口（事务一致快照）；
- 全部登记原件：source_documents（产品来源）、knowledge_sources（第一批知识）、
  kb2_sources（第二批批次资料，含 uploads/phase2/ 嵌套目录），逐条核验存在与 sha256；
- 私有企业模型配置 <db>.model-secrets/（model_settings.EnterpriseModels 读取的目录）；
- 渠道桥登录状态 <db 父目录>/connection-runtime/（channel_runtime 中个人版桥的会话卷）；
- 受限运维配置（可选 `--ops-config`，如 /etc/b2b/b2b.env）：随包 `ops-config/` 目录，
  恢复时按原相对路径保留；含密钥，因此产物保持 0600、禁止明文上传到云端（见 deploy/offsite-backup-r2-plan.md）。

一致性边界（不伪装原子快照）：
数据库快照是一致的；原件与私有目录在快照之后顺序复制、无全局锁——并发写入
（正在上传/处理中）时可能出现“库中行与文件不完全匹配”。要得到互相匹配的一整套，
须先停止应用与渠道桥（停止写入与后台 worker）再运行本工具；
deploy/backup_snapshot.sh 即该停写流程（systemd stop → 备份 → start）。

恢复：只写入尚不存在的空目录；归档内记录的原件绝对路径会重写为恢复目录
（旧相对路径 `data/uploads/…` 保持原样，由运行环境的上传目录配置解析）；
`<db>.model-secrets` 按恢复后的库文件名映射（app.db → app.db.model-secrets）。
安全校验：路径逃逸（含 `..` 段与解析出根的路径）、符号链接（原件、上传根/备份包根、归档内均拒绝）、
缺件、内容篡改、私有权限（0700/0600）。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import tempfile
from datetime import datetime, timezone
from pathlib import Path

SOURCE_TABLES = ("source_documents", "knowledge_sources", "kb2_sources")


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def restrict_permissions(root: Path) -> None:
    """备份/恢复产物只允许属主访问：目录 0700、文件 0600，不随进程 umask 放宽。"""
    os.chmod(root, 0o700)
    for path in root.rglob("*"):
        if path.is_symlink():
            raise ValueError(f"产物包含符号链接，拒绝收紧权限: {path}")
        os.chmod(path, 0o700 if path.is_dir() else 0o600)


def _tables(conn: sqlite3.Connection) -> set[str]:
    return {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def resolve_stored_file(stored, uploads) -> Path:
    """把数据库记录的 storage_path 解析为 uploads 目录内的路径。

    记录格式（按实际代码）：
    1. 绝对路径：knowledge_sources / kb2_sources 的常规格式，以及配置了
       B2B_UPLOAD_DIR 之后 source_documents 的写入格式；必须落在 uploads 根内。
    2. 旧相对路径 `data/uploads/<…>`：source_documents 的历史格式（相对仓库根），
       去掉前缀后在 uploads 根内解析。
    3. 其他相对路径：按 uploads 根解析（保守兼容）。
    安全口径（T4 初审修复）：任何含 `..` 段的路径、上传根本身是符号链接、路径任一段
    是符号链接、或最终解析（resolve）后逃逸 uploads 根的路径一律拒绝；
    临时目录等符号链接前缀造成的字面差异，通过“解析后仍在根内”的比较兼容。
    """
    text = str(stored)
    if not text or "\x00" in text:
        raise ValueError(f"原件存储路径无效: {stored!r}")
    uploads = Path(uploads)
    if uploads.is_symlink():
        raise ValueError(f"上传目录是符号链接，拒绝处理: {uploads}")
    raw = Path(text)
    if not raw.is_absolute():
        parts = raw.parts
        if parts[:2] == ("data", "uploads"):
            parts = parts[2:]
        if ".." in parts:
            raise ValueError(f"原件路径包含上级目录引用（..），拒绝处理: {stored}")
        raw = uploads.joinpath(*parts)
    elif ".." in raw.parts:
        raise ValueError(f"原件路径包含上级目录引用（..），拒绝处理: {stored}")
    try:
        rel = raw.relative_to(uploads)
        walk_base = uploads
    except ValueError:
        # 字面形式不在根内：再按解析后比较（临时目录等前缀可能含符号链接），仍须在根内。
        try:
            rel = raw.resolve().relative_to(uploads.resolve())
        except (ValueError, OSError):
            raise ValueError(f"原件路径逃逸上传目录，拒绝处理: {stored}") from None
        walk_base = uploads.resolve()
        raw = walk_base / rel
    if rel.is_absolute() or ".." in rel.parts:
        raise ValueError(f"原件路径逃逸上传目录，拒绝处理: {stored}")
    current = walk_base
    for part in rel.parts:
        current = current / part
        if current.is_symlink():
            raise ValueError(f"原件路径包含符号链接，拒绝处理: {stored}")
    # 解析后仍须落在上传根内（词法 + 解析双重防线；两侧同样先解析再比较）。
    try:
        raw.resolve().relative_to(uploads.resolve())
    except (ValueError, OSError):
        raise ValueError(f"原件路径逃逸上传目录，拒绝处理: {stored}") from None
    return raw


def _stored_rows(database: Path):
    """遍历三类表中已登记原件的行；旧库缺表时按实际存在的表核验。"""
    with sqlite3.connect(str(database)) as conn:
        present = _tables(conn)
        for table in SOURCE_TABLES:
            if table not in present:
                continue
            rows = conn.execute(
                f"SELECT id, storage_path, sha256 FROM {table} WHERE storage_path IS NOT NULL AND storage_path<>''"
            ).fetchall()
            for row in rows:
                yield {"table": table, "id": row[0], "stored": str(row[1]), "sha256": str(row[2] or "")}


def _verified_relative_files(database: Path, uploads: Path) -> dict[str, str]:
    """核验全部登记原件存在且哈希一致；返回 {相对 uploads 根的路径: sha256（可能为空）}。"""
    uploads = Path(uploads)
    required: dict[str, str] = {}
    for row in _stored_rows(database):
        target = resolve_stored_file(row["stored"], uploads)
        if not target.is_file():
            raise ValueError(f"原件缺失或校验失败（{row['table']}）: {row['stored']}")
        expected = row["sha256"]
        if expected and digest(target) != expected:
            raise ValueError(f"原件校验失败（内容与登记不一致，{row['table']}）: {row['stored']}")
        try:
            rel = target.relative_to(uploads)
        except ValueError:
            rel = target.relative_to(uploads.resolve())
        rel_text = str(rel)
        previous = required.get(rel_text)
        if previous is not None and expected and previous and previous != expected:
            raise ValueError(f"同一原件路径登记了不同哈希，拒绝处理: {row['stored']}")
        required[rel_text] = expected
    return required


def check_source_files(database: Path, uploads: Path) -> int:
    """核验三类原件表中登记的（含嵌套目录）文件存在与哈希，返回通过核验的条数总和。"""
    return len(_verified_relative_files(database, uploads))


def _sqlite_counts(database: Path) -> dict[str, int]:
    with sqlite3.connect(str(database)) as conn:
        present = _tables(conn)
        counts = {}
        for table in SOURCE_TABLES:
            counts[table] = (
                conn.execute(
                    f"SELECT COUNT(*) FROM {table} WHERE storage_path IS NOT NULL AND storage_path<>''"
                ).fetchone()[0]
                if table in present
                else 0
            )
        counts["rfqs"] = conn.execute("SELECT COUNT(*) FROM rfqs").fetchone()[0] if "rfqs" in present else 0
    return counts


def _copy_tree(source: Path, destination: Path) -> None:
    """复制私有目录树；符号链接与非普通文件一律拒绝（宁可见失败，不做静默跳过）。"""
    source = Path(source)
    destination = Path(destination)
    if source.is_symlink():
        raise ValueError(f"目录是符号链接，拒绝复制: {source}")
    for dirpath, dirnames, filenames in os.walk(source, followlinks=False):
        current = Path(dirpath)
        rel = current.relative_to(source)
        target_dir = destination / rel
        target_dir.mkdir(parents=True, exist_ok=True)
        for name in dirnames + filenames:
            entry = current / name
            if entry.is_symlink():
                raise ValueError(f"目录包含符号链接，拒绝复制: {entry}")
        for name in filenames:
            entry = current / name
            if not entry.is_file():
                raise ValueError(f"目录包含非普通文件，拒绝复制: {entry}")
            shutil.copy2(entry, target_dir / name)


def _select_aux_dir(explicit, default: Path, label: str):
    """选择随库的私有目录：显式指定时必须存在；自动探测时存在才纳入。"""
    target = Path(explicit) if explicit is not None else Path(default)
    if target.is_symlink():
        raise ValueError(f"{label}是符号链接，拒绝处理: {target}")
    if target.exists():
        if not target.is_dir():
            raise ValueError(f"{label}存在但不是目录: {target}")
        return target
    if explicit is not None:
        raise ValueError(f"指定的{label}不存在: {target}")
    return None


def backup(
    database: Path,
    uploads: Path,
    destination: Path,
    *,
    model_secrets=None,
    connection_runtime=None,
    ops_config=None,
) -> int:
    """制作一致性快照 + 全部登记原件 + 私有模型配置 + 连接运行状态（+ 可选运维配置）的备份包。

    目录布局：app.db / uploads/ / model-secrets/（可选）/ connection-runtime/（可选）/
    ops-config/（可选，`--ops-config` 指定的受限运维配置，如 /etc/b2b/b2b.env）/ manifest.json。
    停写边界见模块说明；本函数不把“先库后文件”称为原子快照。
    返回通过核验并复制进包的**去重原件文件数**（登记行数可能更多：多行可指向同一文件）。
    """
    database = Path(database)
    uploads = Path(uploads)
    destination = Path(destination)
    if destination.exists():
        raise ValueError("备份目标已存在")
    if not database.is_file():
        raise ValueError("数据库不存在")
    if uploads.is_symlink():
        raise ValueError(f"上传目录是符号链接，拒绝备份: {uploads}")
    if ops_config is not None:
        ops_config = Path(ops_config)
        if ops_config.is_symlink() or not ops_config.is_file():
            raise ValueError(f"运维配置必须是普通文件（拒绝符号链接/目录/缺失）: {ops_config}")
    secrets_dir = _select_aux_dir(model_secrets, Path(str(database) + ".model-secrets"), "模型私有配置目录")
    runtime_dir = _select_aux_dir(
        connection_runtime, database.resolve().parent / "connection-runtime", "连接运行状态目录"
    )
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=destination.parent) as temporary:
        bundle = Path(temporary) / "bundle"
        bundle.mkdir()
        with sqlite3.connect(str(database)) as source, sqlite3.connect(str(bundle / "app.db")) as target:
            source.backup(target)
        # 以一致快照为准核验全部登记原件与真实上传目录的对应关系（在复制前做全量核对）。
        required = _verified_relative_files(bundle / "app.db", uploads)
        if uploads.is_dir():
            _copy_tree(uploads, bundle / "uploads")
        else:
            (bundle / "uploads").mkdir()
        for rel, expected in required.items():
            copied = bundle / "uploads" / rel
            if not copied.is_file():
                raise ValueError(f"备份复制缺失原件: {rel}")
            if expected and digest(copied) != expected:
                raise ValueError(f"备份复制损坏（内容与登记不一致）: {rel}")
        if secrets_dir is not None:
            _copy_tree(secrets_dir, bundle / "model-secrets")
        if runtime_dir is not None:
            _copy_tree(runtime_dir, bundle / "connection-runtime")
        if ops_config is not None:
            ops_dir = bundle / "ops-config"
            ops_dir.mkdir()
            shutil.copy2(ops_config, ops_dir / ops_config.name)
        files = {str(path.relative_to(bundle)): digest(path) for path in bundle.rglob("*") if path.is_file()}
        counts = _sqlite_counts(bundle / "app.db")
        manifest = {
            "created_at": datetime.now(timezone.utc).isoformat(),
            "format": 2,
            "files": files,
            "origin": {
                "database": str(database),
                "uploads": str(uploads),
                "uploads_resolved": str(uploads.resolve()),
                "model_secrets": str(secrets_dir) if secrets_dir is not None else None,
                "connection_runtime": str(runtime_dir) if runtime_dir is not None else None,
            },
            "counts": counts,
        }
        (bundle / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        # 备份含业务库与全部私有材料：先把整棵产物收紧到属主可读，再移出临时目录。
        restrict_permissions(bundle)
        bundle.rename(destination)
    return len(required)


def _reject_symlinks_in_tree(root: Path) -> None:
    if root.is_symlink():
        raise ValueError(f"目录是符号链接，拒绝处理: {root}")
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        for name in dirnames + filenames:
            entry = Path(dirpath) / name
            if entry.is_symlink():
                raise ValueError(f"备份包包含符号链接，拒绝恢复: {entry.relative_to(root)}")


def _legacy_original_match(uploads_dest: Path, raw: Path, sha256: str) -> Path | None:
    """旧格式包（无来源路径记录）尽力重建：按文件名 + 哈希唯一匹配恢复目录内原件。"""
    if not sha256:
        return None
    matches = [p for p in uploads_dest.rglob(raw.name) if p.is_file() and digest(p) == sha256]
    if len(matches) == 1:
        return matches[0].relative_to(uploads_dest)
    return None


def _remap_absolute_paths(conn: sqlite3.Connection, destination: Path, origin, uploads_dest: Path) -> int:
    """把恢复库中指向原机的绝对原件路径重写为恢复目录下的对应路径。"""
    origin = origin if isinstance(origin, dict) else {}
    bases = [str(origin[key]) for key in ("uploads", "uploads_resolved") if origin.get(key)]
    uploads_dest = Path(uploads_dest)
    present = _tables(conn)
    remapped = 0
    for table in SOURCE_TABLES:
        if table not in present:
            continue
        rows = conn.execute(
            f"SELECT id, storage_path, sha256 FROM {table} WHERE storage_path IS NOT NULL AND storage_path<>''"
        ).fetchall()
        for row in rows:
            stored = str(row[1])
            raw = Path(stored)
            if not raw.is_absolute():
                continue  # 旧相对路径保持原样（由运行环境上传目录配置解析）
            rel = None
            for base in bases:
                try:
                    rel = raw.relative_to(base)
                    break
                except ValueError:
                    pass
            if rel is None:
                for base in bases:
                    try:
                        rel = raw.resolve().relative_to(Path(base).resolve())
                        break
                    except (ValueError, OSError):
                        pass
            if rel is None and not bases:
                rel = _legacy_original_match(uploads_dest, raw, str(row[2] or ""))
            if rel is None:
                continue
            if rel.is_absolute() or ".." in rel.parts:
                raise ValueError(f"备份记录的原件路径异常（逃逸上传目录），拒绝恢复: {stored}")
            new_path = str(uploads_dest / rel)
            if new_path != stored:
                conn.execute(f"UPDATE {table} SET storage_path=? WHERE id=?", (new_path, row[0]))
                remapped += 1
    return remapped


def restore_and_verify(bundle: Path, destination: Path) -> tuple[int, int]:
    """把备份包恢复到空目录并核验；返回（可恢复的 RFQ 条数，通过核验的原件条数）。

    旧格式包（仅 app.db + uploads/ + manifest.files）仍可恢复：绝对路径按
    “文件名 + 哈希”唯一匹配尽力重建，无法重建的行会让校验失败（fail-closed）。
    """
    bundle = Path(bundle)
    destination = Path(destination)
    if bundle.is_symlink():
        raise ValueError(f"备份包目录是符号链接，拒绝处理: {bundle}")
    if destination.exists():
        raise ValueError("恢复目标已存在，必须使用隔离的空目录")
    manifest_file = bundle / "manifest.json"
    if not manifest_file.is_file():
        raise ValueError("备份包缺少 manifest.json")
    if not (bundle / "app.db").is_file():
        raise ValueError("备份包缺少数据库文件")
    _reject_symlinks_in_tree(bundle)
    manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
    files = manifest.get("files")
    if not isinstance(files, dict):
        raise ValueError("备份清单格式无效")
    for name, expected in files.items():
        if not isinstance(name, str) or not isinstance(expected, str) or not name:
            raise ValueError(f"备份清单包含非法路径项: {name!r}")
        candidate = Path(name)
        if candidate.is_absolute() or ".." in candidate.parts:
            raise ValueError(f"备份清单包含非法路径项: {name!r}")
    actual_files = {
        str(path.relative_to(bundle)) for path in bundle.rglob("*") if path.is_file() and path.name != "manifest.json"
    }
    if actual_files != set(files):
        raise ValueError("备份文件清单与实际文件不一致")
    for name, expected in files.items():
        path = bundle / name
        if path.is_symlink() or not path.is_file() or digest(path) != expected:
            raise ValueError(f"备份文件校验失败: {name}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.mkdir()
    shutil.copy2(bundle / "app.db", destination / "app.db")
    if (bundle / "uploads").is_dir():
        _copy_tree(bundle / "uploads", destination / "uploads")
    else:
        (destination / "uploads").mkdir()
    model_secrets = bundle / "model-secrets"
    if model_secrets.is_dir():
        # 目录映射：恢复后库名为 app.db，私有模型配置目录必须同名可查（EnterpriseModels 约定）。
        _copy_tree(model_secrets, destination / "app.db.model-secrets")
    elif model_secrets.exists():
        raise ValueError("备份内的模型私有配置不是目录")
    runtime = bundle / "connection-runtime"
    if runtime.is_dir():
        _copy_tree(runtime, destination / "connection-runtime")
    elif runtime.exists():
        raise ValueError("备份内的连接运行状态不是目录")
    for name in files:
        first = name.split("/", 1)[0]
        if first not in {"app.db", "uploads", "model-secrets", "connection-runtime"}:
            # 旧包若含清单登记的其他顶层文件：按原相对路径保留（不使用旧格式不存在的布局）。
            target = destination / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(bundle / name, target)
    shutil.copy2(manifest_file, destination / "manifest.json")
    restrict_permissions(destination)
    with sqlite3.connect(str(destination / "app.db")) as conn:
        if conn.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise ValueError("恢复数据库完整性检查失败")
        records = conn.execute("SELECT COUNT(*) FROM rfqs").fetchone()[0]
        _remap_absolute_paths(conn, destination, manifest.get("origin"), destination / "uploads")
        conn.commit()
    sources = check_source_files(destination / "app.db", destination / "uploads")
    return records, sources


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="action", required=True)
    create = sub.add_parser("backup")
    create.add_argument("--db", type=Path, required=True)
    create.add_argument("--uploads", type=Path, required=True)
    create.add_argument("--out", type=Path, required=True)
    create.add_argument("--secrets", type=Path, default=None, help="私有模型配置目录（默认 <db>.model-secrets，存在才纳入）")
    create.add_argument("--runtime", type=Path, default=None, help="连接运行状态目录（默认 <db 父目录>/connection-runtime，存在才纳入）")
    create.add_argument("--ops-config", type=Path, default=None, help="受限运维配置（如 /etc/b2b/b2b.env）：随包 ops-config/，恢复时按原相对路径保留")
    recover = sub.add_parser("restore-check")
    recover.add_argument("--bundle", type=Path, required=True)
    recover.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    if args.action == "backup":
        verified = backup(args.db, args.uploads, args.out, model_secrets=args.secrets,
                          connection_runtime=args.runtime, ops_config=args.ops_config)
        counts = _sqlite_counts(args.out / "app.db")
        secrets_note = "模型配置有" if (args.out / "model-secrets").is_dir() else "模型配置无"
        runtime_note = "连接运行状态有" if (args.out / "connection-runtime").is_dir() else "连接运行状态无"
        ops_note = "运维配置有" if (args.out / "ops-config").is_dir() else "运维配置无"
        print(f"备份完成: {args.out}")
        print(
            f"覆盖：RFQ {counts['rfqs']} 条；原件已核验 {verified} 个文件"
            f"（登记行：产品 {counts['source_documents']} / 知识 {counts['knowledge_sources']} / 批次 {counts['kb2_sources']}）；"
            f"{secrets_note}；{runtime_note}；{ops_note}"
        )
        print("一致性边界：数据库为事务一致快照；原件与私有目录在快照后顺序复制。要得到互相匹配的一整套，请先停止应用与渠道桥（见 deploy/backup_snapshot.sh）。")
    else:
        records, sources = restore_and_verify(args.bundle, args.out)
        counts = _sqlite_counts(args.out / "app.db")
        print(
            f"隔离恢复完成: RFQ {records} 条；原件校验 {sources} 个文件"
            f"（登记行：产品 {counts['source_documents']} / 知识 {counts['knowledge_sources']} / 批次 {counts['kb2_sources']}）；"
            f"路径 {args.out}"
        )
        if (args.out / "app.db.model-secrets").is_dir():
            print("私有模型配置已按恢复后库名映射（app.db.model-secrets）。")
        if (args.out / "connection-runtime").is_dir():
            print("连接运行状态已恢复到恢复目录（connection-runtime）。")
        if (args.out / "ops-config").is_dir():
            print("运维配置已恢复到 ops-config/（含密钥材料，按运维规程保管、禁止明文上传云端）。")


if __name__ == "__main__":
    main()
