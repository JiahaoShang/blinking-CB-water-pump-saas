# -*- coding: utf-8 -*-
"""T4 判别性测试：备份/恢复覆盖全部原件表、私有模型配置与持久状态；恢复映射与安全拒绝；上传目录持久配置。

约束（与 T4 任务一致）：
- 只使用临时数据库、临时上传目录、临时私有目录与**假密钥**；不读现有 data/、不调用真实模型、不连外部服务。
- 先有判别力的失败断言（在修补代码上跑红），再最小实现使其变绿。
- 新接口（resolve_stored_file / resolve_source_storage_path / app.upload_dir）在用例内部导入，
  以便在旧代码上运行红基线时其余用例仍可执行并给出各自失败原因。
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

import app as app_module
from app import Database
from model_settings import EnterpriseModels
from tools.backup_restore import backup, digest, restore_and_verify


def write_private(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    os.chmod(path, 0o600)


class T4TestBase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="t4-test-")
        self.root = Path(self._tmp.name)
        self._saved = (app_module.ROOT, app_module.UPLOAD_DIR)
        app_module.ROOT = self.root
        app_module.UPLOAD_DIR = self.root / "data" / "uploads"
        os.environ.pop("B2B_UPLOAD_DIR", None)
        os.environ.pop("B2B_KB_STORAGE_ROOT", None)
        self.uploads = app_module.UPLOAD_DIR
        self.db_path = self.root / "app.db"
        self.db = Database(self.db_path)
        self.db.ensure_tenant("t4-tenant", "T4 测试企业")
        self.admin_id = self.db.create_user(
            "t4-tenant", "t4-admin@example.invalid", "T4 Admin", "admin", "password-123", actor="t4-test"
        )

    def tearDown(self) -> None:
        self.db.conn.close()
        app_module.ROOT, app_module.UPLOAD_DIR = self._saved
        os.environ.pop("B2B_UPLOAD_DIR", None)
        os.environ.pop("B2B_KB_STORAGE_ROOT", None)
        self._tmp.cleanup()

    # ── 夹具 ──────────────────────────────────────────────────────────────
    def _product(self, model: str = "P-T4") -> str:
        return self.db.create_product("t4-tenant", model, f"Pump {model}", "test use")

    def _source_document(self, name: str = "manual.txt", data: bytes = b"source original") -> dict:
        """产品来源原件：真实文件 + source_documents 行（走 add_source + 行内 storage_path/hash 修正）。"""
        product_id = self._product(f"P-{hashlib.sha256(name.encode()).hexdigest()[:6]}")
        source_id = self.db.add_source("t4-tenant", product_id, name, "TXT", "p1")
        file_name = f"{source_id}-{Path(name).name}"
        target = self.uploads / file_name
        write_private(target, data)
        self.db.conn.execute(
            "UPDATE source_documents SET storage_path=?, sha256=?, processing_status='stored' WHERE id=?",
            (f"data/uploads/{file_name}", hashlib.sha256(data).hexdigest(), source_id),
        )
        self.db.conn.commit()
        return dict(self.db.conn.execute("SELECT * FROM source_documents WHERE id=?", (source_id,)).fetchone())

    def _knowledge_source(self, name: str = "doc.txt", data: bytes = b"knowledge original") -> dict:
        result = self.db.import_knowledge_file("t4-tenant", self.admin_id, filename=name, data=data)
        row = self.db.conn.execute("SELECT * FROM knowledge_sources WHERE id=?", (result["source_id"],)).fetchone()
        return dict(row)

    def _kb2_source(self, name: str = "batch.txt", data: bytes = b"batch original") -> dict:
        batch_id = self.db.kb2_create_batch(
            "t4-tenant", self.admin_id, [{"kind": "file", "filename": name, "data": data}], process_now=False
        )
        row = self.db.conn.execute(
            "SELECT * FROM kb2_sources WHERE tenant_id=? AND batch_id=?", ("t4-tenant", batch_id)
        ).fetchone()
        return dict(row)

    def _rfq(self, key: str = "t4-rfq-1") -> str:
        conversation_id = self.db.create_conversation("t4-tenant")
        rfq_id, _ = self.db.create_rfq(
            "t4-tenant", conversation_id, key,
            {"contact_email": "buyer@example.invalid", "contact_phone": "+49 30 555 0208", "quantity": "1", "region": "DE"},
            "T4 drill note",
        )
        return rfq_id

    def _backup(self, name: str = "bundle") -> Path:
        out = self.root / name
        backup(self.db_path, self.uploads, out)
        return out


class BackupCoverageTests(T4TestBase):
    """备份必须核验三类原件表；缺件/篡改/逃逸/符号链接必须失败。"""

    def test_backup_fails_when_knowledge_original_missing(self) -> None:
        row = self._knowledge_source()
        Path(row["storage_path"]).unlink()
        with self.assertRaises(ValueError):
            self._backup()

    def test_backup_fails_when_kb2_original_missing(self) -> None:
        row = self._kb2_source()
        Path(row["storage_path"]).unlink()
        with self.assertRaises(ValueError):
            self._backup()

    def test_backup_fails_when_knowledge_original_tampered(self) -> None:
        row = self._knowledge_source(data=b"original bytes")
        Path(row["storage_path"]).write_bytes(b"tampered bytes!")
        with self.assertRaises(ValueError):
            self._backup()

    def test_backup_fails_when_knowledge_original_escapes_uploads(self) -> None:
        row = self._knowledge_source()
        outside = self.root / "outside" / "escaped.txt"
        write_private(outside, Path(row["storage_path"]).read_bytes())
        Path(row["storage_path"]).unlink()
        self.db.conn.execute(
            "UPDATE knowledge_sources SET storage_path=? WHERE id=?", (str(outside), row["id"])
        )
        self.db.conn.commit()
        with self.assertRaises(ValueError):
            self._backup()

    def test_backup_fails_when_source_document_is_symlink(self) -> None:
        source = self._source_document(name="linked.txt", data=b"real bytes")
        target = self.uploads / Path(source["storage_path"]).name
        real = self.uploads / "real-target.bin"
        write_private(real, b"real bytes")
        target.unlink()
        target.symlink_to(real)
        with self.assertRaises(ValueError):
            self._backup()

    def test_backup_fails_when_knowledge_original_is_symlink(self) -> None:
        row = self._knowledge_source()
        target = Path(row["storage_path"])
        real = target.parent / "real-knowledge.bin"
        write_private(real, target.read_bytes())
        target.unlink()
        target.symlink_to(real)
        with self.assertRaises(ValueError):
            self._backup()

    def test_backup_accepts_nested_phase2_relative_source_path(self) -> None:
        """T0 登记的旧边界：source_documents 中 data/uploads/phase2/... 嵌套相对路径必须被受理。"""
        product_id = self._product("P-NESTED")
        source_id = self.db.add_source("t4-tenant", product_id, "nested.txt", "TXT", "p1")
        nested = self.uploads / "phase2" / "nested.txt"
        write_private(nested, b"nested original")
        self.db.conn.execute(
            "UPDATE source_documents SET storage_path=?, sha256=?, processing_status='stored' WHERE id=?",
            ("data/uploads/phase2/nested.txt", hashlib.sha256(b"nested original").hexdigest(), source_id),
        )
        self.db.conn.commit()
        bundle = self._backup()
        self.assertTrue((bundle / "uploads" / "phase2" / "nested.txt").is_file())
        restored = self.root / "restored"
        records, sources = restore_and_verify(bundle, restored)
        self.assertEqual(sources, 1)
        self.assertTrue((restored / "uploads" / "phase2" / "nested.txt").is_file())


class PersistentStateCoverageTests(T4TestBase):
    """私有模型配置与连接运行状态必须进入备份并在恢复目录中正确映射。"""

    def test_model_secrets_included_and_remapped_on_restore(self) -> None:
        secrets_dir = Path(str(self.db_path) + ".model-secrets")
        EnterpriseModels(self.db_path, None).save(
            "t4-tenant", "https://api.example.com/v1", "example-model", "sk-t4-fake-key-not-real"
        )
        self.assertTrue(secrets_dir.is_dir())
        self._rfq()
        bundle = self._backup()
        self.assertTrue((bundle / "model-secrets").is_dir())
        shutil.rmtree(secrets_dir)  # 模拟原机丢失私有配置
        restored = self.root / "restored"
        records, _sources = restore_and_verify(bundle, restored)
        self.assertEqual(records, 1)
        restored_secrets = restored / "app.db.model-secrets"
        self.assertTrue(restored_secrets.is_dir())
        self.assertEqual(oct(restored_secrets.stat().st_mode & 0o777), oct(0o700))
        for item in restored_secrets.rglob("*"):
            if item.is_file():
                self.assertEqual(oct(item.stat().st_mode & 0o777), oct(0o600))
        cfg = EnterpriseModels(restored / "app.db", None).read("t4-tenant")
        self.assertEqual(cfg["model"], "example-model")
        self.assertEqual(cfg["base_url"], "https://api.example.com/v1")

    def test_connection_runtime_included_and_restored(self) -> None:
        runtime = self.root / "connection-runtime" / "0123456789abcdef01234567" / "sessions"
        write_private(runtime / "state.json", b'{"logged_in": true}')
        bundle = self._backup()
        self.assertTrue((bundle / "connection-runtime").is_dir())
        shutil.rmtree(self.root / "connection-runtime")
        restored = self.root / "restored"
        restore_and_verify(bundle, restored)
        self.assertEqual(
            (restored / "connection-runtime" / "0123456789abcdef01234567" / "sessions" / "state.json").read_bytes(),
            b'{"logged_in": true}',
        )

    def test_roundtrip_counts_cover_all_source_tables(self) -> None:
        self._source_document()
        self._knowledge_source()
        self._kb2_source()
        self._rfq()
        bundle = self._backup()
        restored = self.root / "restored"
        records, sources = restore_and_verify(bundle, restored)
        self.assertEqual(records, 1)
        self.assertEqual(sources, 3)


class RestoreMappingTests(T4TestBase):
    """恢复后绝对路径必须重映射到恢复目录；旧相对路径保持不变；旧格式备份包尽力恢复。"""

    def test_restore_remaps_absolute_paths_and_keeps_legacy_relative(self) -> None:
        product_id = self._product("P-ABS")
        source_id = self.db.add_source("t4-tenant", product_id, "abs.bin", "BIN", "p1")
        absolute_target = self.uploads / "abs.bin"
        write_private(absolute_target, b"absolute source")
        legacy_target = self.uploads / "legacy.bin"
        write_private(legacy_target, b"legacy source")
        self.db.conn.execute(
            "UPDATE source_documents SET storage_path=?, sha256=?, processing_status='stored' WHERE id=?",
            (str(absolute_target), hashlib.sha256(b"absolute source").hexdigest(), source_id),
        )
        legacy_id = self.db.add_source("t4-tenant", product_id, "legacy.bin", "BIN", "p1")
        self.db.conn.execute(
            "UPDATE source_documents SET storage_path=?, sha256=?, processing_status='stored' WHERE id=?",
            ("data/uploads/legacy.bin", hashlib.sha256(b"legacy source").hexdigest(), legacy_id),
        )
        self.db.conn.commit()
        knowledge = self._knowledge_source()
        kb2 = self._kb2_source()
        self._rfq()
        bundle = self._backup()
        shutil.rmtree(self.uploads)  # 模拟原机原件全部丢失
        restored = self.root / "restored"
        records, sources = restore_and_verify(bundle, restored)
        self.assertEqual(records, 1)
        self.assertEqual(sources, 4)
        with sqlite3.connect(str(restored / "app.db")) as conn:
            conn.row_factory = sqlite3.Row
            abs_row = conn.execute("SELECT storage_path FROM source_documents WHERE id=?", (source_id,)).fetchone()
            legacy_row = conn.execute("SELECT storage_path FROM source_documents WHERE id=?", (legacy_id,)).fetchone()
            knowledge_row = conn.execute("SELECT storage_path FROM knowledge_sources WHERE id=?", (knowledge["id"],)).fetchone()
            kb2_row = conn.execute("SELECT storage_path FROM kb2_sources WHERE id=?", (kb2["id"],)).fetchone()
        self.assertEqual(abs_row["storage_path"], str(restored / "uploads" / "abs.bin"))
        self.assertEqual(legacy_row["storage_path"], "data/uploads/legacy.bin")
        self.assertEqual(knowledge_row["storage_path"], str(restored / "uploads" / Path(knowledge["storage_path"]).name))
        self.assertEqual(kb2_row["storage_path"], str(restored / "uploads" / "phase2" / Path(kb2["storage_path"]).name))
        self.assertTrue((restored / "uploads" / "legacy.bin").is_file())

    def test_restore_accepts_legacy_bundle_layout(self) -> None:
        """旧格式包（无 origin 记录）：按文件名 + 哈希尽力重建知识原件映射。"""
        knowledge = self._knowledge_source(name="legacy-knowledge.txt", data=b"legacy knowledge bytes")
        self._rfq()
        bundle = self.root / "legacy-bundle"
        bundle.mkdir()
        with sqlite3.connect(str(self.db_path)) as source, sqlite3.connect(str(bundle / "app.db")) as target:
            source.backup(target)
        shutil.copytree(self.uploads, bundle / "uploads")
        files = {str(p.relative_to(bundle)): digest(p) for p in bundle.rglob("*") if p.is_file()}
        (bundle / "manifest.json").write_text(
            json.dumps({"created_at": datetime.now(timezone.utc).isoformat(), "files": files}, ensure_ascii=False),
            encoding="utf-8",
        )
        Path(knowledge["storage_path"]).unlink()
        restored = self.root / "restored-legacy"
        records, sources = restore_and_verify(bundle, restored)
        self.assertEqual(records, 1)
        self.assertEqual(sources, 1)
        with sqlite3.connect(str(restored / "app.db")) as conn:
            row = conn.execute("SELECT storage_path FROM knowledge_sources WHERE id=?", (knowledge["id"],)).fetchone()
        self.assertEqual(row[0], str(restored / "uploads" / Path(knowledge["storage_path"]).name))

    def test_restore_rejects_bundle_containing_symlink(self) -> None:
        self._rfq()
        bundle = self.root / "symlink-bundle"
        bundle.mkdir()
        with sqlite3.connect(str(self.db_path)) as source, sqlite3.connect(str(bundle / "app.db")) as target:
            source.backup(target)
        (bundle / "uploads").mkdir()
        real = bundle / "uploads" / "real.bin"
        real.write_bytes(b"symlinked content")
        link = bundle / "uploads" / "sneaky.bin"
        link.symlink_to(real)
        files = {str(p.relative_to(bundle)): digest(p) for p in bundle.rglob("*") if p.is_file()}
        (bundle / "manifest.json").write_text(
            json.dumps({"created_at": datetime.now(timezone.utc).isoformat(), "files": files}, ensure_ascii=False),
            encoding="utf-8",
        )
        with self.assertRaises(ValueError):
            restore_and_verify(bundle, self.root / "restored-symlink")

    def test_restore_refuses_existing_destination(self) -> None:
        self._rfq()
        bundle = self._backup()
        destination = self.root / "restored-once"
        restore_and_verify(bundle, destination)
        with self.assertRaises(ValueError):
            restore_and_verify(bundle, destination)


class SourcePathEscapeHardeningTests(T4TestBase):
    """Codex 初审复现（docs/evidence/first-launch-t4/independent/path-escape-review.txt）：
    `data/uploads/../x`、`../x`、`<uploads>/../x` 三种形态曾可解析到上传目录外的文件。
    修复口径：拒绝 `..` 段 + 解析后必须仍在上传根内；上传根/备份包根为符号链接一律拒绝。
    `/etc/b2b/b2b.env` 等受限运维配置作为 `ops-config/` 随备份包并提供可恢复映射。"""

    def _outside_file(self, name: str = "outside-secret.txt", data: bytes = b"outside secret") -> Path:
        outside = self.root / name
        write_private(outside, data)
        return outside

    def test_resolve_stored_file_rejects_dotdot_forms(self) -> None:
        from tools.backup_restore import resolve_stored_file

        outside = self._outside_file()
        forms = (
            f"data/uploads/../{outside.name}",
            f"../{outside.name}",
            str(self.uploads / ".." / outside.name),
        )
        for stored in forms:
            with self.assertRaises(ValueError, msg=f"未拒绝逃逸路径: {stored}"):
                resolve_stored_file(stored, self.uploads)

    def test_backup_fails_when_source_row_escapes_with_dotdot(self) -> None:
        outside = self._outside_file("registry-esc.bin", b"registry escape bytes")
        product_id = self._product("P-ESC")
        source_id = self.db.add_source("t4-tenant", product_id, "esc.bin", "BIN", "p1")
        self.db.conn.execute(
            "UPDATE source_documents SET storage_path=?, sha256=?, processing_status='stored' WHERE id=?",
            (f"data/uploads/../{outside.name}", hashlib.sha256(b"registry escape bytes").hexdigest(), source_id),
        )
        self.db.conn.commit()
        with self.assertRaises(ValueError):
            self._backup()

    def test_backup_rejects_symlinked_uploads_root(self) -> None:
        real = self.root / "real-uploads"
        real.mkdir()
        link = self.root / "uploads-link"
        link.symlink_to(real, target_is_directory=True)
        with self.assertRaises(ValueError):
            backup(self.db_path, link, self.root / "bundle-symlink-root")

    def test_restore_rejects_symlinked_bundle_root(self) -> None:
        self._rfq()
        bundle = self._backup()
        link = self.root / "bundle-link"
        link.symlink_to(bundle, target_is_directory=True)
        with self.assertRaises(ValueError):
            restore_and_verify(link, self.root / "restored-link")

    def test_restore_fails_closed_on_dotdot_registered_path(self) -> None:
        outside = self._outside_file("restore-escape.bin", b"restore escape bytes")
        knowledge = self._knowledge_source(name="restore-kb.txt", data=b"kb normal bytes")
        self._rfq()
        bundle = self._backup()
        # 篡改备份包内数据库：登记路径改为 `..` 形态，且正好命中恢复目录之外的同名文件；
        # 恢复必须 fail-closed（旧实现会沿 `..` 读到恢复目录之外并“成功”通过核验）。
        conn = sqlite3.connect(str(bundle / "app.db"))
        conn.execute(
            "UPDATE knowledge_sources SET storage_path=?, sha256=? WHERE id=?",
            (
                f"data/uploads/../../{outside.name}",
                hashlib.sha256(b"restore escape bytes").hexdigest(),
                knowledge["id"],
            ),
        )
        conn.commit()
        conn.close()
        manifest_file = bundle / "manifest.json"
        manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
        manifest["files"]["app.db"] = digest(bundle / "app.db")
        manifest_file.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        with self.assertRaises(ValueError):
            restore_and_verify(bundle, self.root / "restored-escape")

    def test_ops_config_file_included_and_restored(self) -> None:
        ops = self.root / "b2b.env"
        write_private(ops, b"B2B_UPLOAD_DIR=/var/lib/b2b/data/uploads\nDEEPSEEK_API_KEY=fake-key-for-drill\n")
        self._rfq()
        bundle = self.root / "bundle-ops"
        backup(self.db_path, self.uploads, bundle, ops_config=ops)
        packaged = bundle / "ops-config" / "b2b.env"
        self.assertTrue(packaged.is_file())
        self.assertEqual(oct(packaged.stat().st_mode & 0o777), oct(0o600))
        manifest = json.loads((bundle / "manifest.json").read_text(encoding="utf-8"))
        self.assertIn("ops-config/b2b.env", manifest["files"])
        restored = self.root / "restored-ops"
        restore_and_verify(bundle, restored)
        self.assertEqual((restored / "ops-config" / "b2b.env").read_bytes(), ops.read_bytes())

    def test_ops_config_rejects_directory_or_symlink(self) -> None:
        directory = self.root / "cfg-dir"
        directory.mkdir()
        with self.assertRaises(ValueError):
            backup(self.db_path, self.uploads, self.root / "bundle-dir-cfg", ops_config=directory)
        real = self.root / "real-ops.env"
        write_private(real, b"X=1\n")
        link = self.root / "link-ops.env"
        link.symlink_to(real)
        with self.assertRaises(ValueError):
            backup(self.db_path, self.uploads, self.root / "bundle-link-cfg", ops_config=link)


class UploadDirConfigTests(T4TestBase):
    """上传目录持久配置：新写入与控制目录一致，旧相对路径迁移后按持久目录解析。"""

    def test_configured_upload_dir_receives_new_source_files(self) -> None:
        persist = self.root / "persist" / "uploads"
        os.environ["B2B_UPLOAD_DIR"] = str(persist)
        product_id = self._product("P-ENV")
        source_id = self.db.add_source("t4-tenant", product_id, "env.bin", "BIN", "p1", content=b"persisted bytes")
        row = self.db.conn.execute("SELECT * FROM source_documents WHERE id=?", (source_id,)).fetchone()
        stored = Path(row["storage_path"])
        self.assertTrue(stored.is_absolute(), msg=f"配置持久目录后应为绝对路径: {stored}")
        self.assertEqual(stored, persist / stored.name)
        self.assertEqual(stored.read_bytes(), b"persisted bytes")
        from app import resolve_source_storage_path

        self.assertEqual(resolve_source_storage_path(row["storage_path"]), stored)

    def test_legacy_relative_path_resolves_to_configured_upload_dir(self) -> None:
        from app import resolve_source_storage_path

        persist = self.root / "persist2" / "uploads"
        os.environ["B2B_UPLOAD_DIR"] = str(persist)
        target = persist / "legacy.bin"
        write_private(target, b"legacy persisted")
        self.assertEqual(
            resolve_source_storage_path("data/uploads/legacy.bin"),
            target,
        )

    def test_knowledge_import_lands_in_configured_upload_dir(self) -> None:
        persist = self.root / "persist3" / "uploads"
        os.environ["B2B_UPLOAD_DIR"] = str(persist)
        row = self._knowledge_source(name="env-knowledge.txt")
        stored = Path(row["storage_path"])
        self.assertEqual(stored.parent, persist)
        self.assertTrue(stored.is_file())

    def test_resolve_stored_file_rejects_escape_and_symlink(self) -> None:
        from tools.backup_restore import resolve_stored_file

        outside = self.root / "outside2" / "x.bin"
        write_private(outside, b"outside")
        with self.assertRaises(ValueError):
            resolve_stored_file(str(outside), self.uploads)
        real = self.uploads / "real2.bin"
        write_private(real, b"real")
        link = self.uploads / "link2.bin"
        link.symlink_to(real)
        with self.assertRaises(ValueError):
            resolve_stored_file("data/uploads/link2.bin", self.uploads)
        self.assertEqual(resolve_stored_file("data/uploads/real2.bin", self.uploads), real)


class CliRoundtripTests(T4TestBase):
    """命令行备份/恢复在“持久上传目录 + 绝对路径登记”的部署形态下可整体复跑（锁回归）。

    该用例在修补前代码上运行会失败（旧工具只接受 data/uploads 单层相对路径，遇到知识/
    批次的绝对路径登记直接拒绝），属对 T0 登记缺口的端到端判别。
    """

    def test_cli_backup_and_restore_roundtrip(self) -> None:
        repo_root = Path(__file__).resolve().parent
        persist = self.root / "persist-cli" / "uploads"
        os.environ["B2B_UPLOAD_DIR"] = str(persist)
        product_id = self._product("P-CLI")
        source_id = self.db.add_source("t4-tenant", product_id, "cli.bin", "BIN", "p1", content=b"cli source bytes")
        source_row = self.db.conn.execute("SELECT storage_path FROM source_documents WHERE id=?", (source_id,)).fetchone()
        self.assertTrue(Path(source_row["storage_path"]).is_absolute(), msg="配置持久目录后来源原件应登记绝对路径")
        self._knowledge_source()
        self._kb2_source()
        self._rfq()
        bundle = self.root / "cli-backup"
        restored = self.root / "cli-restored"
        backup_run = subprocess.run(
            [sys.executable, str(repo_root / "tools" / "backup_restore.py"), "backup",
             "--db", str(self.db_path), "--uploads", str(persist), "--out", str(bundle)],
            cwd=str(repo_root), capture_output=True, text=True, timeout=120,
        )
        self.assertEqual(backup_run.returncode, 0, msg=backup_run.stdout + backup_run.stderr)
        self.assertIn("原件已核验", backup_run.stdout)
        restore_run = subprocess.run(
            [sys.executable, str(repo_root / "tools" / "backup_restore.py"), "restore-check",
             "--bundle", str(bundle), "--out", str(restored)],
            cwd=str(repo_root), capture_output=True, text=True, timeout=120,
        )
        self.assertEqual(restore_run.returncode, 0, msg=restore_run.stdout + restore_run.stderr)
        self.assertTrue((restored / "uploads" / Path(source_row["storage_path"]).name).is_file())


if __name__ == "__main__":
    unittest.main()
