"""V1-01 账号与模块权限：两账号类型（管理员/员工）、员工零权限开通、管理员审批授权、
服务端硬拦截、授权即时生效、跨租户隔离、旧角色迁移零落差。

用户口径（2026-10-04）：员工新账号"对任何功能都没有访问权限"；"必须由管理员账号进行审批
并授权后"才可访问 —— 本文件锁该契约。
"""
import http.client
import json
import tempfile
import threading
import unittest
from pathlib import Path
from urllib.parse import urlencode

from app import Database, GRANTABLE_MODULE_KEYS, make_server


def form_request(server, method, path, fields=None, cookie=None):
    conn = http.client.HTTPConnection("127.0.0.1", server.server_port)
    headers = {}
    body = None
    if fields is not None:
        body = urlencode(fields)
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    if cookie:
        headers["Cookie"] = cookie
    conn.request(method, path, body=body, headers=headers)
    response = conn.getresponse()
    raw = response.read().decode("utf-8")
    headers_out = dict(response.getheaders())
    status = response.status
    conn.close()
    return status, headers_out, raw


def login(server, email, password):
    status, headers, _ = form_request(server, "POST", "/login", {"email": email, "password": password})
    if status not in (302, 303) or "Set-Cookie" not in headers:
        raise AssertionError(f"登录失败: {email} -> {status}")
    return headers["Set-Cookie"].split(";", 1)[0]


class ModulePermissionTests(unittest.TestCase):
    maxDiff = None

    @classmethod
    def setUpClass(cls):
        cls.temp_dir = tempfile.TemporaryDirectory()
        cls.server = make_server(Path(cls.temp_dir.name) / "app.db", port=0)
        cls.db = cls.server.RequestHandlerClass.db
        cls.db.seed_demo_users()
        # 新员工账号：默认零权限（唯一开通路径 = 管理员在成员页执行模块授权）
        cls.employee_id = cls.db.create_user(
            "demo-tenant", "newbie@example.invalid", "新员工", "employee", "strong-pass", "demo-admin@example.invalid"
        )
        # 只有产品模块的员工（逐模块正反例）
        cls.product_only_id = cls.db.create_user(
            "demo-tenant", "prod-only@example.invalid", "产品同事", "employee", "strong-pass", "demo-admin@example.invalid"
        )
        cls.db.set_user_modules("demo-tenant", cls.product_only_id, ["products"], "demo-admin@example.invalid")
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.admin_cookie = login(cls.server, "demo-admin@example.invalid", "demo-admin")
        cls.employee_cookie = login(cls.server, "newbie@example.invalid", "strong-pass")
        cls.product_cookie = login(cls.server, "prod-only@example.invalid", "strong-pass")

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.db.conn.close()
        cls.thread.join()
        cls.temp_dir.cleanup()

    def _json(self, path, cookie):
        status, _, raw = form_request(self.server, "GET", path, cookie=cookie)
        return status, json.loads(raw)

    def test_admin_only_pages_are_never_grantable(self):
        # 授予员工"全部"模块后：成员管理仍 403（管理员专属，不参与授权）
        self.db.set_user_modules("demo-tenant", self.employee_id, list(GRANTABLE_MODULE_KEYS), "demo-admin@example.invalid")
        status, _, body = form_request(self.server, "GET", "/admin/users", cookie=self.employee_cookie)
        self.assertEqual(status, 403)
        self.assertIn("成员管理只对管理员开放", body)
        # 管理员账号本身不能被配置模块（全模块，防误操作）
        admin_id = self.db.conn.execute("SELECT id FROM users WHERE email='demo-admin@example.invalid'").fetchone()["id"]
        with self.assertRaises(ValueError):
            self.db.set_user_modules("demo-tenant", admin_id, ["products"], "demo-admin@example.invalid")
        # 收回全部模块 → 服务端硬拦截对写路径同样生效（不依赖界面隐藏）
        self.db.set_user_modules("demo-tenant", self.employee_id, [], "demo-admin@example.invalid")
        self.assertEqual(form_request(self.server, "POST", "/admin/products/seed", {}, self.employee_cookie)[0], 403)

    def test_cross_tenant_isolation_for_module_grants(self):
        self.db.ensure_tenant("other-tenant", "另一个企业")
        outsider_id = self.db.create_user("other-tenant", "outsider@example.invalid", "外部员工", "employee", "strong-pass", "setup")
        with self.assertRaises(ValueError):
            # 用 demo-tenant 管理员去改别家账号 → 视为不存在（租户隔离）
            self.db.set_user_modules("demo-tenant", outsider_id, ["products"], "demo-admin@example.invalid")

    def test_each_module_gates_its_own_pages(self):
        # 产品同事（仅 products）：产品可访问、侧栏有产品入口；其余四个模块页 403
        status, _, page = form_request(self.server, "GET", "/admin", cookie=self.product_cookie)
        self.assertEqual(status, 200)
        self.assertIn("data-nav-count='products'", page)
        self.assertNotIn("data-nav-count='tasks'", page)
        self.assertNotIn("data-nav-count='sales'", page)
        self.assertEqual(form_request(self.server, "GET", "/admin/products", cookie=self.product_cookie)[0], 200)
        for path in ("/admin/tasks", "/admin/rfqs", "/admin/sales", "/admin/sites"):
            self.assertEqual(form_request(self.server, "GET", path, cookie=self.product_cookie)[0], 403, path)

    def test_grant_takes_effect_immediately_and_revoke_cuts_off(self):
        audit_before = self.db.conn.execute("SELECT COALESCE(MAX(id),0) FROM audit_log").fetchone()[0]
        # 管理员授权产品模块 → 员工"无需重新登录"立刻可访问；未授权模块仍被拦
        status, _, _ = form_request(self.server, "POST", f"/admin/users/{self.employee_id}/modules", {"module_products": "1"}, self.admin_cookie)
        self.assertEqual(status, 303)
        status, _, page = form_request(self.server, "GET", "/admin/products", cookie=self.employee_cookie)
        self.assertEqual(status, 200)
        self.assertIn("data-nav-count='products'", page)
        self.assertEqual(form_request(self.server, "GET", "/admin/tasks", cookie=self.employee_cookie)[0], 403)
        # 收回（保存空勾选）→ 403 恢复
        status, _, _ = form_request(self.server, "POST", f"/admin/users/{self.employee_id}/modules", {}, self.admin_cookie)
        self.assertEqual(status, 303)
        self.assertEqual(form_request(self.server, "GET", "/admin/products", cookie=self.employee_cookie)[0], 403)
        # 授权变更全程留审计（before/after；只取本次操作新增的行，避免受其他用例影响）
        rows = self.db.conn.execute(
            "SELECT details FROM audit_log WHERE entity_id=? AND action='modules_updated' AND id>? ORDER BY id",
            (self.employee_id, audit_before),
        ).fetchall()
        self.assertEqual(len(rows), 2)
        self.assertIn("after=products", rows[0]["details"])
        self.assertIn("after=none", rows[1]["details"])

    def test_new_employee_starts_with_zero_access(self):
        # 零权限员工：概览可见（含"等待授权"提示），所有模块页 403，侧栏无任何模块入口
        status, _, page = form_request(self.server, "GET", "/admin", cookie=self.employee_cookie)
        self.assertEqual(status, 200)
        self.assertIn("账号尚未开通任何模块权限", page)
        for path in ("/admin/products", "/admin/tasks", "/admin/rfqs", "/admin/sales", "/admin/sites"):
            status, _, body = form_request(self.server, "GET", path, cookie=self.employee_cookie)
            self.assertEqual(status, 403, path)
            self.assertIn("所需权限", body, path)
        for marker in ("data-nav-count='products'", "data-nav-count='tasks'", "data-nav-count='rfqs'", "data-nav-count='sales'"):
            self.assertNotIn(marker, page, marker)
        # 轮询接口不返回任何数字
        status, body = self._json("/admin/nav-counts", self.employee_cookie)
        self.assertEqual(body, {"counts": {}})

    def test_admin_cannot_deactivate_self(self):
        admin_id = self.db.conn.execute("SELECT id FROM users WHERE email='demo-admin@example.invalid'").fetchone()["id"]
        status, _, body = form_request(self.server, "POST", f"/admin/users/{admin_id}/toggle", {}, self.admin_cookie)
        self.assertEqual(status, 400)  # ValueError → “操作未完成”页；账号必须仍然启用
        self.assertIn("不能停用当前登录账号", body)
        row = self.db.conn.execute("SELECT active FROM users WHERE id=?", (admin_id,)).fetchone()
        self.assertEqual(row["active"], 1)


class LegacyMigrationTests(unittest.TestCase):
    """旧角色迁移：sales/engineer → 员工 + 对应模块授权（能力不变、幂等、可重复打开）。"""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "legacy.db"

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_legacy_roles_migrate_to_employee_with_default_modules_idempotently(self):
        seed = Database(self.db_path)
        seed.ensure_tenant("demo-tenant", "示例水泵企业（演示）")
        seed.conn.execute(
            "INSERT INTO users(id, tenant_id, email, display_name, role, password_hash, active, created_at) "
            "VALUES ('u1','demo-tenant','old-sales@example.invalid','旧销售','sales','x',1,'2026-01-01')"
        )
        seed.conn.execute(
            "INSERT INTO users(id, tenant_id, email, display_name, role, password_hash, active, created_at) "
            "VALUES ('u2','demo-tenant','old-eng@example.invalid','旧工程师','engineer','x',1,'2026-01-01')"
        )
        seed.conn.commit()
        seed.conn.close()

        migrated = Database(self.db_path)  # 重新打开即执行迁移
        try:
            roles = {row["id"]: row["role"] for row in migrated.conn.execute("SELECT id, role FROM users")}
            self.assertEqual(roles, {"u1": "employee", "u2": "employee"})
            self.assertEqual(set(migrated.get_user_modules("u1")), {"rfqs", "sales"})
            self.assertEqual(set(migrated.get_user_modules("u2")), {"tasks", "rfqs", "sales"})
        finally:
            migrated.conn.close()
        # 幂等：再次打开不重复授予
        again = Database(self.db_path)
        try:
            count = again.conn.execute("SELECT COUNT(*) FROM user_modules").fetchone()[0]
            self.assertEqual(count, 5)
        finally:
            again.conn.close()

    def test_legacy_role_names_still_accepted_by_create_user_for_compatibility(self):
        db = Database(self.db_path)
        try:
            db.ensure_tenant("demo-tenant")
            uid = db.create_user("demo-tenant", "compat@example.invalid", "兼容", "sales", "strong-pass", "setup")
            row = db.conn.execute("SELECT role FROM users WHERE id=?", (uid,)).fetchone()
            self.assertEqual(row["role"], "employee")
            self.assertEqual(set(db.get_user_modules(uid)), {"rfqs", "sales"})
            # 未知角色仍然拒绝
            with self.assertRaises(ValueError):
                db.create_user("demo-tenant", "weird@example.invalid", "怪", "buyer", "strong-pass", "setup")
            # 新员工账号默认零权限（不落任何模块行）
            new_id = db.create_user("demo-tenant", "fresh@example.invalid", "新同事", "employee", "strong-pass", "setup")
            self.assertEqual(db.get_user_modules(new_id), [])
        finally:
            db.conn.close()


if __name__ == "__main__":
    unittest.main()
