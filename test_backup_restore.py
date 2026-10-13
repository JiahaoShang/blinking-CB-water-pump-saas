import hashlib
import json
import shutil
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

from app import Database
from tools.backup_restore import backup, restore_and_verify

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / 'deploy'))

import provision_enterprise_bridge  # noqa: E402


class BackupRestoreTests(unittest.TestCase):
    def test_backup_restores_database_and_source_file_in_isolated_directory(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            uploads = root / "uploads"
            uploads.mkdir()
            db = Database(root / "app.db")
            db.ensure_tenant("demo-tenant", "示例水泵企业（演示）")
            product_id = db.create_product("demo-tenant", "P-1", "Test pump", "Clean water")
            source_id = db.add_source("demo-tenant", product_id, "manual.pdf", "PDF", "Page 1")
            contents = b"approved demo source"
            filename = f"{source_id}-manual.pdf"
            (uploads / filename).write_bytes(contents)
            db.conn.execute(
                "UPDATE source_documents SET storage_path=?, sha256=? WHERE id=?",
                (f"data/uploads/{filename}", hashlib.sha256(contents).hexdigest(), source_id),
            )
            db.conn.commit()
            conversation_id = db.create_conversation("demo-tenant")
            db.create_rfq("demo-tenant", conversation_id, "first", {"contact_email": "buyer@example.invalid", "contact_phone": "+49 30 555 0208", "quantity": "1", "region": "Germany"}, "First RFQ")
            bundle = root / "snapshot"
            backup(root / "app.db", uploads, bundle)
            db.create_rfq("demo-tenant", conversation_id, "second", {"contact_email": "buyer@example.invalid", "contact_phone": "+49 30 555 0209", "quantity": "1", "region": "Germany"}, "Later RFQ")
            (uploads / filename).unlink()
            db.conn.close()

            restored = root / "restored"
            self.assertEqual(restore_and_verify(bundle, restored), (1, 1))
            self.assertEqual((restored / "uploads" / filename).read_bytes(), contents)
            with sqlite3.connect(str(restored / "app.db")) as conn:
                self.assertEqual(conn.execute("SELECT notes FROM rfqs").fetchone()[0], "First RFQ")
            with self.assertRaises(ValueError):
                restore_and_verify(bundle, restored)


class EnterpriseBridgeBackupRestoreTests(unittest.TestCase):
    """企业专属桥随备份恢复：私有注册表、受限 env、sessions/media 全部还原，且注册表仍可被应用解析。"""

    def test_enterprise_bridge_registry_env_sessions_media_restored(self):
        from channel_runtime import read_enterprise_registry

        tenant = "tenant-alpha"
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            uploads = root / "uploads"
            uploads.mkdir()
            db = Database(root / "app.db")
            db.ensure_tenant(tenant, "甲企业")
            db.conn.close()

            digest = hashlib.sha256(tenant.encode("utf-8")).hexdigest()[:24]
            workdir = root / "connection-runtime" / "enterprise" / digest
            (workdir / "sessions").mkdir(parents=True)
            (workdir / "sessions" / "state.bin").write_bytes(b"whatsapp-login-state")
            (workdir / "media").mkdir()
            (workdir / "media" / "blob.bin").write_bytes(b"media-bytes")
            api_key = "restored-env-key-" + "0" * 32
            env_text = "WAHA_API_KEY=" + api_key + "\nWHATSAPP_DEFAULT_ENGINE=NOWEB\n"
            (workdir / "bridge.env").write_text(env_text, encoding="utf-8")
            registry = root / "connection-runtime" / "enterprise-bridges.json"
            document = {"version": 1, "bridges": {tenant: {
                "container_name": "b2b-enterprise-" + digest,
                "api_base": "http://127.0.0.1:39999", "api_key": api_key}}}
            provision_enterprise_bridge.write_registry_file(registry, document)

            bundle = root / "snapshot"
            backup(root / "app.db", uploads, bundle)
            shutil.rmtree(root / "connection-runtime")

            restored = root / "restored"
            restore_and_verify(bundle, restored)
            runtime = restored / "connection-runtime"
            self.assertTrue((runtime / "enterprise-bridges.json").is_file(), "注册表必须随备份恢复")
            self.assertEqual(json.loads((runtime / "enterprise-bridges.json").read_text(encoding="utf-8")), document)
            self.assertEqual((runtime / "enterprise" / digest / "bridge.env").read_text(encoding="utf-8"), env_text)
            self.assertEqual((runtime / "enterprise" / digest / "sessions" / "state.bin").read_bytes(), b"whatsapp-login-state")
            self.assertEqual((runtime / "enterprise" / digest / "media" / "blob.bin").read_bytes(), b"media-bytes")
            # 恢复后的注册表必须仍能被应用解析（容器名/地址/密钥不变量通过校验）
            self.assertEqual(read_enterprise_registry(runtime / "enterprise-bridges.json"), {
                tenant: {"container_name": "b2b-enterprise-" + digest,
                         "api_base": "http://127.0.0.1:39999", "api_key": api_key}})


if __name__ == "__main__":
    unittest.main()
