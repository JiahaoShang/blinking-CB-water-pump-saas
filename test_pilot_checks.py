"""试点校验脚本的冒烟与负例测试：确保每个 check_* 脚本在真库与坏数据上行为正确。"""
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

from app import Database, make_server

ROOT = Path(__file__).resolve().parent
CHECKS = ROOT / "tools" / "pilot_checks"


def run_script(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, *args], capture_output=True, text=True, cwd=str(ROOT), timeout=120,
    )


class PilotCheckScriptTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.db_path = self.root / "app.db"
        Database(self.db_path)  # 建出完整库结构
        self.uploads = self.root / "uploads"
        self.uploads.mkdir()

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_migration_check_passes_on_fresh_database(self):
        result = run_script(str(CHECKS / "check_db_migrations.py"), "--db", str(self.db_path))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("PASS", result.stdout)

    def test_restore_drill_passes_and_counts_match(self):
        result = run_script(str(CHECKS / "restore_drill.py"), "--db", str(self.db_path), "--uploads", str(self.uploads))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("PASS", result.stdout)
        self.assertIn("全程耗时", result.stdout)

    def test_public_permission_check_detects_missing_storage_file(self):
        # 无发布产品：空库应通过（并给出提醒）
        smoke = run_script(str(CHECKS / "check_public_permissions.py"), "--db", str(self.db_path))
        self.assertEqual(smoke.returncode, 0, smoke.stdout + smoke.stderr)
        # 构造坏数据：发布快照指向不存在的存储文件 + 有许可记录 → 必须 FAIL
        conn = sqlite3.connect(str(self.db_path))
        conn.execute("INSERT INTO products(id, tenant_id, model, name, use_case, slug, status, created_at, updated_at) VALUES ('p1', 'demo-tenant', 'BAD-1', '坏样例', 'demo', 'bad-1', 'published', '2026-10-03', '2026-10-03')")
        conn.execute(
            "INSERT INTO product_versions(id, tenant_id, product_id, version, snapshot_json, status, created_by, created_at, published_at) "
            "VALUES ('v1', 'demo-tenant', 'p1', 1, ?, 'published', 'test', '2026-10-03', '2026-10-03')",
            ('{"product": {"model": "BAD-1"}, "fields": [], "public_sources": [{"id": "s1", "filename": "gone.png", "scope": "exact_model"}]}',),
        )
        conn.execute("INSERT INTO source_documents(id, tenant_id, product_id, filename, file_type, source_location, storage_path, scope, created_at) VALUES ('s1', 'demo-tenant', 'p1', 'gone.png', 'image', 'demo', 'data/uploads/definitely-missing.png', 'exact_model', '2026-10-03')")
        conn.execute("INSERT INTO public_source_permissions(source_id, tenant_id, product_id, approved_by, approved_at) VALUES ('s1', 'demo-tenant', 'p1', 'test', '2026-10-03')")
        conn.commit()
        conn.close()
        broken = run_script(str(CHECKS / "check_public_permissions.py"), "--db", str(self.db_path))
        self.assertEqual(broken.returncode, 1, broken.stdout + broken.stderr)
        self.assertIn("存储文件不存在", broken.stdout)

    def test_public_permission_check_detects_snapshot_scope_mismatch(self):
        """快照归属与当前归属不一致（旧版本仍按旧口径公开的残留）必须 FAIL。"""
        conn = sqlite3.connect(str(self.db_path))
        conn.execute("INSERT INTO products(id, tenant_id, model, name, use_case, slug, status, created_at, updated_at) VALUES ('p2', 'demo-tenant', 'STALE-1', '归属残留样例', 'demo', 'stale-1', 'published', '2026-10-03', '2026-10-03')")
        conn.execute(
            "INSERT INTO product_versions(id, tenant_id, product_id, version, snapshot_json, status, created_by, created_at, published_at) "
            "VALUES ('v21', 'demo-tenant', 'p2', 1, ?, 'published', 'test', '2026-10-03', '2026-10-03')",
            ('{"product": {"model": "STALE-1"}, "fields": [], "public_sources": [{"id": "s2", "filename": "stale.pdf", "scope": "exact_model"}]}',),
        )
        conn.execute("INSERT INTO source_documents(id, tenant_id, product_id, filename, file_type, source_location, storage_path, scope, created_at) VALUES ('s2', 'demo-tenant', 'p2', 'stale.pdf', 'pdf', 'demo', 'README.md', 'series', '2026-10-03')")
        conn.execute("INSERT INTO public_source_permissions(source_id, tenant_id, product_id, approved_by, approved_at) VALUES ('s2', 'demo-tenant', 'p2', 'test', '2026-10-03')")
        conn.commit()
        conn.close()
        result = run_script(str(CHECKS / "check_public_permissions.py"), "--db", str(self.db_path))
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("归属口径与发布快照不一致", result.stdout)

    def test_public_permission_check_detects_stale_attribution_revision(self):
        """归属文本相同但修订落后（修正后改回原值、未重新发布）必须 FAIL。"""
        conn = sqlite3.connect(str(self.db_path))
        conn.execute("INSERT INTO products(id, tenant_id, model, name, use_case, slug, status, created_at, updated_at) VALUES ('p3', 'demo-tenant', 'CYCLE-1', '归属回环样例', 'demo', 'cycle-1', 'published', '2026-10-03', '2026-10-03')")
        conn.execute(
            "INSERT INTO product_versions(id, tenant_id, product_id, version, snapshot_json, status, created_by, created_at, published_at) "
            "VALUES ('v31', 'demo-tenant', 'p3', 1, ?, 'published', 'test', '2026-10-03', '2026-10-03')",
            ('{"product": {"model": "CYCLE-1"}, "fields": [], "public_sources": [{"id": "s3", "filename": "cycle.png", "scope": "exact_model", "scope_revision": 0}]}',),
        )
        conn.execute("INSERT INTO source_documents(id, tenant_id, product_id, filename, file_type, source_location, storage_path, scope, scope_revision, created_at) VALUES ('s3', 'demo-tenant', 'p3', 'cycle.png', 'image', 'demo', 'README.md', 'exact_model', 2, '2026-10-03')")
        conn.execute("INSERT INTO public_source_permissions(source_id, tenant_id, product_id, approved_by, approved_at) VALUES ('s3', 'demo-tenant', 'p3', 'test', '2026-10-03')")
        conn.commit()
        conn.close()
        result = run_script(str(CHECKS / "check_public_permissions.py"), "--db", str(self.db_path))
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("归属修订与发布快照不一致", result.stdout)
        self.assertIn("快照 r0 / 当前 r2", result.stdout)

    def test_cors_check_against_live_server(self):
        server = make_server(self.db_path, port=0)
        server.RequestHandlerClass.db.register_buyer_site("demo-tenant", "site-a", "https://seller-a.example")
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            result = run_script(
                str(CHECKS / "check_cors.py"), "--base", f"http://127.0.0.1:{server.server_port}",
                "--site-key", "site-a", "--allowed-origin", "https://seller-a.example",
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("PASS", result.stdout)
            self.assertIn("交接交换接口拒绝陌生域名", result.stdout)
            self.assertIn("交接交换接口只在平台自身来源受理", result.stdout)
            # 白名单不匹配时必须 FAIL（把允许域名改错，检查脚本会拒绝放行）
            wrong = run_script(
                str(CHECKS / "check_cors.py"), "--base", f"http://127.0.0.1:{server.server_port}",
                "--site-key", "site-a", "--allowed-origin", "https://wrong.example",
            )
            self.assertEqual(wrong.returncode, 1, wrong.stdout + wrong.stderr)
        finally:
            server.shutdown()
            server.server_close()
            thread.join()


if __name__ == "__main__":
    unittest.main()
