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
        self.assertEqual(products, {"SLICE-T1": "published", "SLICE-T2": "published", "SLICE-NOBASIS": "published"})
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
        # 第二个可比型号：也真实发布并有自己的详情页数据（1 份获准公开参数表）
        t2_id = conn.execute("SELECT id FROM products WHERE model='SLICE-T2'").fetchone()["id"]
        t2_versions = [(row["version"], row["status"]) for row in conn.execute("SELECT version, status FROM product_versions WHERE product_id=? ORDER BY version", (t2_id,))]
        self.assertEqual(t2_versions, [(1, "published")])
        t2_snapshot = json.loads(conn.execute("SELECT snapshot_json FROM product_versions WHERE product_id=? AND status='published'", (t2_id,)).fetchone()["snapshot_json"])
        self.assertEqual(len(t2_snapshot["public_sources"]), 1)
        self.assertEqual(t2_snapshot["public_sources"][0]["filename"], "DEMO-SLICE-T2-参数表.txt")
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
        self.assertEqual(len(rebuilt_files), 9)  # SLICE-T1 的 7 份 + SLICE-T2 的 1 份 + SLICE-NOBASIS 的 1 份
        for old in self.created_files:
            if old not in rebuilt_files:
                self.assertFalse((ROOT / old).is_file(), f"旧演示文件未清理: {old}")
        self.created_files = [path for path in self.created_files if path in rebuilt_files]
        conn = sqlite3.connect(str(self.db_path))
        published = conn.execute("SELECT COUNT(*) FROM products WHERE status='published'").fetchone()[0]
        conn.close()
        self.assertEqual(published, 3)

    def test_demo_sources_and_facts_are_explicitly_marked_demo(self):
        """演示资料不得被误当作真实厂商规格：文件名、出处、标题与文件内容都带 DEMO 标注。"""
        from seed_demo import seed

        result = seed(self.db_path)
        self.created_files += list(result.get("files", []))
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        sources = conn.execute("SELECT filename, source_location FROM source_documents").fetchall()
        self.assertTrue(sources)
        for source in sources:
            self.assertTrue(source["filename"].startswith("DEMO-"), source["filename"])
            self.assertIn("DEMO 合成演示", source["source_location"], source["filename"])
        fields = conn.execute(
            "SELECT f.field_key, f.source_location, s.filename FROM product_fields f "
            "JOIN source_documents s ON s.id=f.source_document_id WHERE f.state='approved'"
        ).fetchall()
        self.assertTrue(fields)
        for field in fields:
            self.assertIn("DEMO", field["source_location"], field["field_key"])
            self.assertTrue(field["filename"].startswith("DEMO-"), field["field_key"])
        names = {row["model"]: row["name"] for row in conn.execute("SELECT model, name FROM products")}
        for model, name in names.items():
            self.assertIn("DEMO", name, model)
        # 文件内容本身也带合成演示声明（防截图/转发后脱离上下文）
        for source in conn.execute("SELECT storage_path FROM source_documents WHERE storage_path LIKE '%.txt' OR storage_path LIKE '%.dxf' OR storage_path LIKE '%.step'"):
            body = (ROOT / source["storage_path"]).read_text(encoding="utf-8", errors="replace")
            self.assertIn("DEMO", body.upper(), source["storage_path"])
        conn.close()

    def test_demo_two_comparable_candidates_with_gap_scenarios_kept(self):
        """明确工况应得到两个并列可解释候选；条件不足与超范围场景保持可用。"""
        from seed_demo import seed
        from app import Database, buyer_visible_results, confirm_requirement_values, extract_requirement_values, match_products

        result = seed(self.db_path)
        self.created_files += list(result.get("files", []))
        db = Database(self.db_path)

        def confirmed(text):
            return confirm_requirement_values(extract_requirement_values(text, {}, None))

        # 明确工况：两个运行范围不同的型号并列可初步推荐，说明各自带产品范围，链接正确
        status, results, missing = match_products(db, "demo-tenant", confirmed("flow 50 m3/h, clean water, head 45"))
        self.assertEqual(status, "recommended")
        self.assertEqual(missing, [])
        visible = buyer_visible_results(results, include_tentative=False)
        self.assertEqual([item["model"] for item in visible], ["SLICE-T1", "SLICE-T2"])
        self.assertEqual({item["detail_url"] for item in visible}, {"/products/slice-t1", "/products/slice-t2"})
        clauses = {}
        for item in visible:
            self.assertEqual(item["status"], "candidate")
            self.assertTrue(item["satisfies"])
            clauses[item["model"]] = "；".join(item["satisfies"])
        self.assertIn("产品范围 30-90 m³/h", clauses["SLICE-T1"])
        self.assertIn("产品范围 20-60 m³/h", clauses["SLICE-T2"])
        self.assertNotEqual(clauses["SLICE-T1"], clauses["SLICE-T2"], "并列候选应说明各自的范围差异")
        # SLICE-NOBASIS（无水力依据）有记录但不进入任何买方可见候选
        no_basis = next(item for item in results if item["model"] == "SLICE-NOBASIS")
        self.assertEqual(no_basis["status"], "no_basis")
        self.assertNotIn("SLICE-NOBASIS", [item["model"] for item in visible])

        # 条件不足保持：缺扬程 → insufficient；默认不下发候选，买方主动查看时两型号逐项标注
        status, results, missing = match_products(db, "demo-tenant", confirmed("flow 50 m3/h, clean water"))
        self.assertEqual(status, "insufficient")
        self.assertEqual(missing, ["扬程"])
        self.assertEqual(results, [], "待核实候选默认不得下发")
        status, results, missing = match_products(db, "demo-tenant", confirmed("flow 50 m3/h, clean water"), include_tentative=True)
        self.assertEqual(status, "insufficient")
        self.assertEqual(missing, ["扬程"])
        tentative = buyer_visible_results(results, include_tentative=True)
        self.assertEqual([item["model"] for item in tentative], ["SLICE-T1", "SLICE-T2"])
        for item in tentative:
            self.assertEqual(item["status"], "needs_confirmation")
            self.assertIn("扬程", "；".join(item["unknown"]))

        # 超范围保持：50 m³/h + 300 m 全为硬冲突 → 无可靠候选，永不进买方可见结果
        status, results, missing = match_products(db, "demo-tenant", confirmed("flow 50 m3/h, clean water, head 300"))
        self.assertEqual(status, "no_match")
        self.assertEqual(buyer_visible_results(results, include_tentative=True), [])
        db.conn.close()

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
