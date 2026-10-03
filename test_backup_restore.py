import hashlib
import sqlite3
import tempfile
import unittest
from pathlib import Path

from app import Database
from tools.backup_restore import backup, restore_and_verify


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
            db.create_rfq("demo-tenant", conversation_id, "first", {"contact_email": "buyer@example.invalid"}, "First RFQ")
            bundle = root / "snapshot"
            backup(root / "app.db", uploads, bundle)
            db.create_rfq("demo-tenant", conversation_id, "second", {"contact_email": "buyer@example.invalid"}, "Later RFQ")
            (uploads / filename).unlink()
            db.conn.close()

            restored = root / "restored"
            self.assertEqual(restore_and_verify(bundle, restored), (1, 1))
            self.assertEqual((restored / "uploads" / filename).read_bytes(), contents)
            with sqlite3.connect(str(restored / "app.db")) as conn:
                self.assertEqual(conn.execute("SELECT notes FROM rfqs").fetchone()[0], "First RFQ")
            with self.assertRaises(ValueError):
                restore_and_verify(bundle, restored)


if __name__ == "__main__":
    unittest.main()
