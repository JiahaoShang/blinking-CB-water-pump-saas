"""演示工具测试：种子构建/幂等/重建，以及一键启动的检查模式。"""
import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools" / "demo"))


class DemoToolingTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "demo.db"
        self.created_files: list[str] = []

    def tearDown(self):
        for storage_path in self.created_files:
            target = ROOT / storage_path
            if target.is_file():
                target.unlink()
        self.temp_dir.cleanup()

    def _collect_files(self, db_path: Path) -> None:
        if not db_path.exists():
            return
        conn = sqlite3.connect(str(db_path))
        try:
            self.created_files += [row[0] for row in conn.execute("SELECT storage_path FROM source_documents WHERE storage_path IS NOT NULL")]
        except sqlite3.Error:
            pass
        finally:
            conn.close()

    def test_seed_builds_reproducible_demo_state_and_reset(self):
        from seed_demo import seed

        result = seed(self.db_path)
        self.assertFalse(result["skipped"])
        self.created_files += list(result.get("files", []))
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        products = {row["model"]: row["status"] for row in conn.execute("SELECT model, status FROM products")}
        self.assertEqual(products, {"SLICE-T1": "published", "SLICE-NOBASIS": "published"})
        product_id = conn.execute("SELECT id FROM products WHERE model='SLICE-T1'").fetchone()["id"]
        versions = [(row["version"], row["status"]) for row in conn.execute("SELECT version, status FROM product_versions WHERE product_id=? ORDER BY version", (product_id,))]
        self.assertEqual(versions, [(1, "withdrawn"), (2, "published")])
        snapshot = json.loads(conn.execute("SELECT snapshot_json FROM product_versions WHERE product_id=? AND status='published'", (product_id,)).fetchone()["snapshot_json"])
        public_files = snapshot["public_sources"]
        self.assertEqual(len(public_files), 7)
        for item in public_files:
            row = conn.execute("SELECT storage_path, sha256 FROM source_documents WHERE id=?", (item["id"],)).fetchone()
            target = ROOT / row["storage_path"]
            self.assertTrue(target.is_file(), row["storage_path"])
            self.assertEqual(item["sha256"], row["sha256"])
        fields = {row["field_key"]: row["state"] for row in conn.execute("SELECT field_key, state FROM product_fields WHERE product_id=?", (product_id,))}
        self.assertEqual(fields["head_range"], "approved")
        self.assertEqual(fields["temperature"], "rejected")
        site = conn.execute("SELECT site_key, allowed_origin FROM buyer_sites").fetchone()
        self.assertEqual((site["site_key"], site["allowed_origin"]), ("demo-site-key", "http://127.0.0.1:8137"))
        # 演示账号只由演示命令创建，且保持文档化的账号/角色（密码即账号名）
        users = {row["email"]: row["role"] for row in conn.execute("SELECT email, role FROM users")}
        self.assertEqual(users, {
            "demo-admin@example.invalid": "admin",
            "demo-sales@example.invalid": "sales",
            "demo-engineer@example.invalid": "engineer",
        })
        conn.close()
        # 文档化口令可通过登录校验（保留内部演示登录）
        from app import Database as AppDatabase
        check_db = AppDatabase(self.db_path)
        for email, password in (
            ("demo-admin@example.invalid", "demo-admin"),
            ("demo-sales@example.invalid", "demo-sales"),
            ("demo-engineer@example.invalid", "demo-engineer"),
        ):
            self.assertIsNotNone(check_db.authenticate_user(email, password), email)
        check_db.conn.close()

        # 幂等：再次构建直接跳过
        again = seed(self.db_path)
        self.assertTrue(again["skipped"])
        # 重建：--reset 后状态与首次一致，旧文件被清理
        rebuilt = seed(self.db_path, reset=True)
        self.assertFalse(rebuilt["skipped"])
        rebuilt_files = set(rebuilt.get("files", []))
        self.assertEqual(len(rebuilt_files), 8)  # SLICE-T1 的 7 份 + SLICE-NOBASIS 的 1 份
        for old in self.created_files:
            if old not in rebuilt_files:
                self.assertFalse((ROOT / old).is_file(), f"旧演示文件未清理: {old}")
        self.created_files = [path for path in self.created_files if path in rebuilt_files]
        conn = sqlite3.connect(str(self.db_path))
        published = conn.execute("SELECT COUNT(*) FROM products WHERE status='published'").fetchone()[0]
        conn.close()
        self.assertEqual(published, 2)

    def test_start_demo_check_mode_prints_ready_banner(self):
        proc = subprocess.run(
            [sys.executable, str(ROOT / "tools" / "demo" / "start_demo.py"), "--check", "--db", str(self.db_path)],
            capture_output=True, text=True, timeout=120, cwd=str(ROOT),
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("演示环境就绪", proc.stdout)
        self.assertIn("买方入口", proc.stdout)
        self.assertIn("demo-site-key", proc.stdout)
        self.assertIn("演示账号", proc.stdout)
        self._collect_files(self.db_path)
        # 复跑复用演示库
        second = subprocess.run(
            [sys.executable, str(ROOT / "tools" / "demo" / "start_demo.py"), "--check", "--db", str(self.db_path)],
            capture_output=True, text=True, timeout=120, cwd=str(ROOT),
        )
        self.assertEqual(second.returncode, 0, second.stdout + second.stderr)
        self.assertIn("复用现有演示库", second.stdout)


if __name__ == "__main__":
    unittest.main()
