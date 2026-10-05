"""V1-03 切片1：创意中心（产品展示页）数据层与权限。

用户口径（收口稿）：新增可授权模块「创意中心」——授权入口仍在成员管理、未授权员工
服务端硬拦截、授权即时生效；每类产品一份展示页（同租户同品类唯一、跨租户隔离）；
标准模板默认组合与现状公开页等价（板块/参数冻结清单）；新建与保存改动全程审计。
本文件锁该契约。
"""
import http.client
import json
import tempfile
import threading
import unittest
from pathlib import Path
from urllib.parse import urlencode

from app import (
    AUDIT_ACTION_LABELS,
    AUDIT_ENTITY_LABELS,
    Database,
    GRANTABLE_MODULE_KEYS,
    TEMPLATE_BLOCK_LIBRARY,
    make_server,
)


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
    raw = response.read().decode("utf-8", errors="replace")
    headers_out = dict(response.getheaders())
    status = response.status
    conn.close()
    return status, headers_out, raw


def login(server, email, password):
    status, headers, _ = form_request(server, "POST", "/login", {"email": email, "password": password})
    if status not in (302, 303) or "Set-Cookie" not in headers:
        raise AssertionError(f"登录失败: {email} -> {status}")
    return headers["Set-Cookie"].split(";", 1)[0]


class CreativePermissionTests(unittest.TestCase):
    """模块注册 / 成员管理勾选 / 服务端硬拦截 / 授权即时生效与收回。"""

    maxDiff = None

    @classmethod
    def setUpClass(cls):
        cls.temp_dir = tempfile.TemporaryDirectory()
        cls.server = make_server(Path(cls.temp_dir.name) / "app.db", port=0)
        cls.db = cls.server.RequestHandlerClass.db
        cls.db.seed_demo_users()
        cls.employee_id = cls.db.create_user(
            "demo-tenant", "v103-newbie@example.invalid", "新员工", "employee", "strong-pass-1", "demo-admin@example.invalid"
        )
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.admin = login(cls.server, "demo-admin@example.invalid", "demo-admin")
        cls.employee = login(cls.server, "v103-newbie@example.invalid", "strong-pass-1")

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.db.conn.close()
        cls.thread.join()
        cls.temp_dir.cleanup()

    def test_module_registered_and_member_checkbox(self):
        self.assertIn("creative", GRANTABLE_MODULE_KEYS)
        status, _, page = form_request(self.server, "GET", "/admin/users", cookie=self.admin)
        self.assertEqual(status, 200)
        self.assertIn("module_creative", page)
        self.assertIn("创意中心", page)

    def test_admin_sees_creative_page_with_breadcrumb(self):
        status, _, page = form_request(self.server, "GET", "/admin/creative", cookie=self.admin)
        self.assertEqual(status, 200)
        self.assertIn("产品展示页", page)
        self.assertIn("创意中心 /", page)
        self.assertIn("data-nav-count='creative'", page)

    def test_employee_blocked_until_granted_then_revoked(self):
        # 授权前：服务端硬拦截（提示所需权限），侧栏不渲染入口
        status, _, body = form_request(self.server, "GET", "/admin/creative", cookie=self.employee)
        self.assertEqual(status, 403)
        self.assertIn("创意中心", body)
        self.assertIn("所需权限", body)
        _, _, home_before = form_request(self.server, "GET", "/admin", cookie=self.employee)
        self.assertNotIn("创意中心", home_before)
        # 管理员在成员管理勾选授权 → 立即生效（免重登）
        status, _, _ = form_request(
            self.server, "POST", f"/admin/users/{self.employee_id}/modules", {"module_creative": "1"}, self.admin
        )
        self.assertEqual(status, 303)
        status, _, page = form_request(self.server, "GET", "/admin/creative", cookie=self.employee)
        self.assertEqual(status, 200)
        self.assertIn("产品展示页", page)
        _, _, home_after = form_request(self.server, "GET", "/admin", cookie=self.employee)
        self.assertIn("创意中心", home_after)
        self.assertIn("/admin/creative", home_after)
        # 仅此模块：其他模块仍被拦
        self.assertEqual(form_request(self.server, "GET", "/admin/products", cookie=self.employee)[0], 403)
        # 收回授权 → 立即失效
        status, _, _ = form_request(self.server, "POST", f"/admin/users/{self.employee_id}/modules", {}, self.admin)
        self.assertEqual(status, 303)
        self.assertEqual(form_request(self.server, "GET", "/admin/creative", cookie=self.employee)[0], 403)


class TemplateDataTests(unittest.TestCase):
    """模板表结构 / 每类一份唯一约束 / 跨租户隔离 / 标准与空白初始内容 / 保存与审计。"""

    maxDiff = None

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "tpl.db"
        self.db = Database(self.db_path)
        self.db.ensure_tenant("demo-tenant", "示例水泵企业（演示）")
        self.db.ensure_tenant("other-tenant", "另一家企业")

    def tearDown(self):
        self.db.conn.close()
        self.temp_dir.cleanup()

    def test_tables_created_and_reopen_is_idempotent(self):
        names = {row["name"] for row in self.db.conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertIn("templates", names)
        self.assertIn("template_versions", names)
        self.db.conn.close()
        reopened = Database(self.db_path)  # 重复打开即执行迁移：幂等、不报错
        self.db = reopened
        columns = {row["name"] for row in reopened.conn.execute("PRAGMA table_info(templates)")}
        self.assertLessEqual({"id", "tenant_id", "category", "draft_json", "published_version"}, columns)

    def test_create_template_unique_per_category_and_isolated_between_tenants(self):
        first = self.db.create_template("demo-tenant", "工业输送设备", "demo-admin@example.invalid")
        # 每类一份：同品类重复创建被拒
        with self.assertRaises(ValueError):
            self.db.create_template("demo-tenant", "工业输送设备", "demo-admin@example.invalid")
        # 空品类被拒
        with self.assertRaises(ValueError):
            self.db.create_template("demo-tenant", "   ", "demo-admin@example.invalid")
        self.assertEqual(len(self.db.list_templates("demo-tenant")), 1)
        self.assertEqual(self.db.get_template_for_category("demo-tenant", "工业输送设备")["id"], first)
        # 跨租户隔离：别家可建同品类；别家 id 在本租户查不到、也改不了
        other = self.db.create_template("other-tenant", "工业输送设备", "owner@other.example.invalid")
        self.assertIsNone(self.db.get_template("demo-tenant", other))
        with self.assertRaises(ValueError):
            self.db.save_template_draft("demo-tenant", other, {"params": [], "blocks": []}, "demo-admin@example.invalid")
        self.assertEqual(len(self.db.list_templates("other-tenant")), 1)

    def test_standard_draft_matches_frozen_defaults(self):
        tid = self.db.create_template("demo-tenant", "工业输送设备", "demo-admin@example.invalid")
        draft = json.loads(self.db.get_template("demo-tenant", tid)["draft_json"])
        # 板块：12 项、顺序 = 冻结库顺序；hero 固定开启、gallery 默认关、其余默认开
        self.assertEqual([b["key"] for b in draft["blocks"]], [b["key"] for b in TEMPLATE_BLOCK_LIBRARY])
        by_key = {b["key"]: b["enabled"] for b in draft["blocks"]}
        self.assertTrue(by_key["hero"])
        self.assertFalse(by_key["gallery"])
        self.assertTrue(by_key["specs"])
        # 参数：默认 = 现有 8 个可提取字段（现状公开页顺序）
        self.assertEqual(
            [p["key"] for p in draft["params"]],
            ["media", "temperature", "flow_range", "head_range", "performance_points", "power", "material", "dimension"],
        )
        self.assertTrue(all(p["enabled"] for p in draft["params"]))

    def test_blank_draft_only_keeps_fixed_hero(self):
        tid = self.db.create_template("demo-tenant", "灌溉阀门", "demo-admin@example.invalid", blank=True)
        draft = json.loads(self.db.get_template("demo-tenant", tid)["draft_json"])
        self.assertEqual(draft["params"], [])
        self.assertEqual([b["key"] for b in draft["blocks"] if b["enabled"]], ["hero"])

    def test_save_draft_persists_and_audits(self):
        tid = self.db.create_template("demo-tenant", "工业输送设备", "demo-admin@example.invalid")
        draft = json.loads(self.db.get_template("demo-tenant", tid)["draft_json"])
        draft["params"].append({"key": "custom_color", "label_zh": "颜色", "label_en": "Color", "enabled": True})
        self.db.save_template_draft("demo-tenant", tid, draft, "张工")
        row = self.db.get_template("demo-tenant", tid)
        self.assertEqual(json.loads(row["draft_json"])["params"][-1]["label_zh"], "颜色")
        self.assertEqual(row["draft_updated_by"], "张工")
        self.assertIsNotNone(row["draft_updated_at"])
        actions = [
            r["action"]
            for r in self.db.conn.execute(
                "SELECT action FROM audit_log WHERE entity_type='template' AND entity_id=? ORDER BY id", (tid,)
            )
        ]
        self.assertEqual(actions, ["template_created", "template_draft_saved"])
        # 审计中文标签已登记（操作记录页不裸露英文 key）
        self.assertIn("template_created", AUDIT_ACTION_LABELS)
        self.assertIn("template_draft_saved", AUDIT_ACTION_LABELS)
        self.assertIn("template", AUDIT_ENTITY_LABELS)


if __name__ == "__main__":
    unittest.main()
