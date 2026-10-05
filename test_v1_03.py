"""V1-03 切片1：创意中心（产品展示页）数据层与权限。

用户口径（收口稿）：新增可授权模块「创意中心」——授权入口仍在成员管理、未授权员工
服务端硬拦截、授权即时生效；每类产品一份展示页（同租户同品类唯一、跨租户隔离）；
标准模板默认组合与现状公开页等价（板块/参数冻结清单）；新建与保存改动全程审计。
本文件锁该契约。
"""
import http.client
import json
import re
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
        self.assertNotIn("side-heading'>创意中心", home_before)
        # 管理员在成员管理勾选授权 → 立即生效（免重登）
        status, _, _ = form_request(
            self.server, "POST", f"/admin/users/{self.employee_id}/modules", {"module_creative": "1"}, self.admin
        )
        self.assertEqual(status, 303)
        status, _, page = form_request(self.server, "GET", "/admin/creative", cookie=self.employee)
        self.assertEqual(status, 200)
        self.assertIn("产品展示页", page)
        _, _, home_after = form_request(self.server, "GET", "/admin", cookie=self.employee)
        self.assertIn("side-heading'>创意中心", home_after)
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


class CreativeHomeTests(unittest.TestCase):
    """首页（产品视角列表 / 新建卡 / 三态与按钮）与编辑区骨架（模块条、分步路由、404）。"""

    maxDiff = None

    @classmethod
    def setUpClass(cls):
        cls.temp_dir = tempfile.TemporaryDirectory()
        cls.server = make_server(Path(cls.temp_dir.name) / "app.db", port=0)
        cls.db = cls.server.RequestHandlerClass.db
        cls.db.seed_demo_users()
        # 只读企业：一个已核准品类产品、无展示页（「尚未设置」态断言；本类其它用例不依赖它）
        cls.db.ensure_tenant("readonly-tenant", "只读测试企业")
        readonly_user = cls.db.create_user("readonly-tenant", "ro-creator@example.invalid", "只读编辑", "employee", "ro-pass-1", "setup")
        cls.db.set_user_modules("readonly-tenant", readonly_user, ["creative"], "setup")
        ro_product = cls.db.create_product("readonly-tenant", "RO-1", "只读产品", "测试", "setup")
        cls.db.set_category_candidate("readonly-tenant", ro_product, "工业输送设备", "manual", "setup")
        cls.db.approve_category("readonly-tenant", ro_product, "setup")
        # 空企业：无产品（空态引导断言）
        cls.db.ensure_tenant("empty-tenant", "空测试企业")
        empty_user = cls.db.create_user("empty-tenant", "empty-creator@example.invalid", "空编辑", "employee", "empty-pass-1", "setup")
        cls.db.set_user_modules("empty-tenant", empty_user, ["creative"], "setup")
        # demo 企业：创建旅程用产品（p4 故意不核准品类）
        cls.p1 = cls.db.create_product("demo-tenant", "AX-500", "智能输送设备", "测试", "demo-admin@example.invalid")
        cls.p2 = cls.db.create_product("demo-tenant", "AX-300", "输送设备", "测试", "demo-admin@example.invalid")
        cls.p3 = cls.db.create_product("demo-tenant", "V-100", "灌溉阀门", "测试", "demo-admin@example.invalid")
        cls.p4 = cls.db.create_product("demo-tenant", "DM-200", "减速电机", "测试", "demo-admin@example.invalid")
        for pid, category in ((cls.p1, "工业输送设备"), (cls.p2, "工业输送设备"), (cls.p3, "灌溉阀门")):
            cls.db.set_category_candidate("demo-tenant", pid, category, "manual", "demo-admin@example.invalid")
            cls.db.approve_category("demo-tenant", pid, "demo-admin@example.invalid")
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.admin = login(cls.server, "demo-admin@example.invalid", "demo-admin")
        cls.ro_cookie = login(cls.server, "ro-creator@example.invalid", "ro-pass-1")
        cls.empty_cookie = login(cls.server, "empty-creator@example.invalid", "empty-pass-1")

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.db.conn.close()
        cls.thread.join()
        cls.temp_dir.cleanup()

    def test_home_fresh_state_readonly(self):
        status, _, page = form_request(self.server, "GET", "/admin/creative", cookie=self.ro_cookie)
        self.assertEqual(status, 200)
        self.assertIn("RO-1", page)
        self.assertIn("尚未设置", page)
        self.assertIn("开始设置", page)
        self.assertIn("＋ 新建展示页", page)
        self.assertIn("data-cat='工业输送设备'", page)
        # 唯一模块条 + 首页态淡化 + 连线递进
        self.assertEqual(page.count("class='flowbar"), 1)
        self.assertIn("flowbar ghost", page)
        self.assertIn("class='conn'", page)

    def test_home_empty_tenant_guides_to_products(self):
        status, _, page = form_request(self.server, "GET", "/admin/creative", cookie=self.empty_cookie)
        self.assertEqual(status, 200)
        self.assertIn("还没有可制作展示页的产品", page)
        self.assertIn("/admin/products", page)
        self.assertNotIn("id='creative-new'", page)

    def test_create_journey_and_editor(self):
        # 1) 选择产品创建 → 303 → 进入编辑区（notice=created）；审计 template_created
        status, headers, _ = form_request(self.server, "POST", "/admin/creative/create", {"product_id": self.p1}, self.admin)
        self.assertEqual(status, 303)
        match = re.fullmatch(r"/admin/creative/([a-f0-9]+)\?notice=created", headers.get("Location", ""))
        self.assertIsNotNone(match, headers.get("Location", ""))
        template_id = match.group(1)
        self.assertEqual(self.db.get_template_for_category("demo-tenant", "工业输送设备")["id"], template_id)
        actions = [
            row["action"]
            for row in self.db.conn.execute(
                "SELECT action FROM audit_log WHERE entity_type='template' AND entity_id=?", (template_id,)
            )
        ]
        self.assertEqual(actions, ["template_created"])
        # 2) 编辑区骨架：唯一模块条、选参数高亮、步骤链接、占位文案、无数字标识
        status, _, page = form_request(self.server, "GET", f"/admin/creative/{template_id}?notice=created", cookie=self.admin)
        self.assertEqual(status, 200)
        self.assertIn("工业输送设备 · 产品展示页", page)
        self.assertIn("展示页已创建", page)
        self.assertEqual(page.count("class='flowbar"), 1)
        self.assertIn("fs on'>选参数", page)
        self.assertIn(f"href='/admin/creative/{template_id}?step=blocks'", page)
        self.assertIn(f"href='/admin/creative/{template_id}?step=publish'", page)
        self.assertIn("页面要展示哪些参数？", page)  # 默认落在「选参数」编辑器（切片3）
        # 模块条范围内无数字标识（整页含 CSS 历史备注字符，只对链路本身断言）
        bar = page.split("class='flowbar", 1)[1].split("</div>", 1)[0]
        for digit in ("①", "②", "③", "④", "⑤"):
            self.assertNotIn(digit, bar)
        # 3) 同品类再建 → 直接打开现有页（不重复创建）
        status, headers, _ = form_request(self.server, "POST", "/admin/creative/create", {"product_id": self.p2}, self.admin)
        self.assertEqual(status, 303)
        self.assertEqual(headers.get("Location", ""), f"/admin/creative/{template_id}?notice=existing")
        self.assertEqual(len(self.db.list_templates("demo-tenant")), 1)
        # 4) 首页：有改动还没发布 + 继续编辑 + 覆盖产品链接 + 未核准品类脚注
        status, _, home = form_request(self.server, "GET", "/admin/creative", cookie=self.admin)
        self.assertEqual(status, 200)
        self.assertIn("工业输送设备 · 产品展示页", home)
        self.assertIn("有改动还没发布", home)
        self.assertIn("继续编辑", home)
        self.assertIn(f"/admin/products/{self.p1}", home)
        self.assertIn(f"/admin/products/{self.p2}", home)
        self.assertIn("另有 1 个产品还没有已核准的品类", home)
        # 5) 未核准品类的产品 / 库外 id → 400 + 明确提示（不落数据）
        status, _, body = form_request(self.server, "POST", "/admin/creative/create", {"product_id": self.p4}, self.admin)
        self.assertEqual(status, 400)
        self.assertIn("核准品类", body)
        status, _, body = form_request(self.server, "POST", "/admin/creative/create", {"product_id": "0" * 24}, self.admin)
        self.assertEqual(status, 400)
        self.assertIn("本企业产品库", body)
        self.assertEqual(len(self.db.list_templates("demo-tenant")), 1)
        # 6) 空白创建（另一品类）→ 初始内容仅保留固定首屏
        status, headers, _ = form_request(self.server, "POST", "/admin/creative/create", {"product_id": self.p3, "start": "blank"}, self.admin)
        self.assertEqual(status, 303)
        blank_id = re.fullmatch(r"/admin/creative/([a-f0-9]+)\?notice=created", headers.get("Location", "")).group(1)
        draft = json.loads(self.db.get_template("demo-tenant", blank_id)["draft_json"])
        self.assertEqual(draft["params"], [])
        self.assertEqual([b["key"] for b in draft["blocks"] if b["enabled"]], ["hero"])
        # 7) 跨企业 id / 不存在 id → 404（不泄露、不区分）
        foreign = self.db.create_template("readonly-tenant", "别家品类", "setup")
        status, _, body = form_request(self.server, "GET", f"/admin/creative/{foreign}", cookie=self.admin)
        self.assertEqual(status, 404)
        self.assertIn("展示页不存在", body)
        status, _, _ = form_request(self.server, "GET", "/admin/creative/" + "a" * 24, cookie=self.admin)
        self.assertEqual(status, 404)


class ParamsEditorTests(unittest.TestCase):
    """选参数编辑器：推荐池/自定义、开关、排序、行内编辑、删除、自动保存（审计）、术语。"""

    maxDiff = None

    @classmethod
    def setUpClass(cls):
        cls.temp_dir = tempfile.TemporaryDirectory()
        cls.server = make_server(Path(cls.temp_dir.name) / "app.db", port=0)
        cls.db = cls.server.RequestHandlerClass.db
        cls.db.seed_demo_users()
        cls.db.ensure_tenant("other-tenant", "另一家企业")
        cls.template_id = cls.db.create_template("demo-tenant", "工业输送设备", "demo-admin@example.invalid")
        cls.db.create_user("demo-tenant", "v103s3-nomod@example.invalid", "无权员工", "employee", "pass-s3-long", "demo-admin@example.invalid")
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.admin = login(cls.server, "demo-admin@example.invalid", "demo-admin")
        cls.nomod = login(cls.server, "v103s3-nomod@example.invalid", "pass-s3-long")

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.db.conn.close()
        cls.thread.join()
        cls.temp_dir.cleanup()

    def _draft(self):
        return json.loads(self.db.get_template("demo-tenant", self.template_id)["draft_json"])

    def _keys(self):
        return [p["key"] for p in self._draft()["params"]]

    def _find(self, key):
        return next(p for p in self._draft()["params"] if p["key"] == key)

    def _post(self, action, fields):
        return form_request(self.server, "POST", f"/admin/creative/{self.template_id}/params/{action}", fields, self.admin)

    def test_cross_tenant_and_unauthorized(self):
        # 未授权员工：服务端硬拦截（模块门）
        status, _, _ = form_request(
            self.server, "POST", f"/admin/creative/{self.template_id}/params/toggle", {"param_key": "material"}, self.nomod
        )
        self.assertEqual(status, 403)
        # 跨租户模板 id：视为不存在（400 错误页，不泄露）
        foreign = self.db.create_template("other-tenant", "别家品类", "setup")
        status, _, body = form_request(
            self.server, "POST", f"/admin/creative/{foreign}/params/toggle", {"param_key": "material"}, self.admin
        )
        self.assertEqual(status, 400)
        self.assertIn("展示页不存在", body)

    def test_param_actions_journey(self):
        # 推荐池添加 → 自动保存 + 重定向回本步
        status, headers, _ = self._post("add", {"source": "pool", "pool_key": "voltage"})
        self.assertEqual(status, 303)
        self.assertEqual(headers.get("Location", ""), f"/admin/creative/{self.template_id}?step=params")
        self.assertEqual(self._keys()[-1], "voltage")
        self.assertTrue(self._find("voltage")["enabled"])
        self.assertEqual(self._find("voltage")["label_zh"], "电压")
        # 重复添加 → 400；未知推荐项 → 400
        status, _, body = self._post("add", {"source": "pool", "pool_key": "voltage"})
        self.assertEqual(status, 400)
        self.assertIn("已经在页面上", body)
        self.assertEqual(self._post("add", {"source": "pool", "pool_key": "nope"})[0], 400)
        # 自定义参数 → 生成 custom_ 键；空名称 → 400
        status, _, _ = self._post("add", {"source": "custom", "label_zh": "涂层", "label_en": "Coating", "unit": ""})
        self.assertEqual(status, 303)
        custom_key = self._keys()[-1]
        self.assertTrue(custom_key.startswith("custom_"))
        self.assertEqual(self._find(custom_key)["label_zh"], "涂层")
        self.assertEqual(self._post("add", {"source": "custom", "label_zh": "   "})[0], 400)
        # 开关（自动保存）
        self._post("toggle", {"param_key": "voltage"})
        self.assertFalse(self._find("voltage")["enabled"])
        self._post("toggle", {"param_key": "voltage"})
        self.assertTrue(self._find("voltage")["enabled"])
        # 排序：voltage 上移一格；首位上移 = 不变（不写坏顺序）
        before = self._keys()
        self._post("move", {"param_key": "voltage", "dir": "up"})
        after = self._keys()
        self.assertEqual(after.index("voltage"), before.index("voltage") - 1)
        self._post("move", {"param_key": after[0], "dir": "up"})
        self.assertEqual(self._keys(), after)
        # 行内编辑（旧字段也可改标签）；空名 → 400
        self._post("update", {"param_key": "voltage", "label_zh": "工作电压", "label_en": "Operating voltage", "unit": "V"})
        row = self._find("voltage")
        self.assertEqual((row["label_zh"], row["label_en"], row["unit"]), ("工作电压", "Operating voltage", "V"))
        self.assertEqual(self._post("update", {"param_key": "voltage", "label_zh": ""})[0], 400)
        # 删除自定义；未知 key → 400
        self._post("delete", {"param_key": custom_key})
        self.assertNotIn(custom_key, self._keys())
        status, _, body = self._post("delete", {"param_key": "ghost"})
        self.assertEqual(status, 400)
        self.assertIn("参数不存在", body)
        # 自动保存痕迹：页面头部显示「已自动保存」；审计累计（每次生效的改动一条）
        status, _, page = form_request(self.server, "GET", f"/admin/creative/{self.template_id}?step=params", cookie=self.admin)
        self.assertIn("已自动保存", page)
        saves = self.db.conn.execute(
            "SELECT COUNT(*) FROM audit_log WHERE entity_type='template' AND entity_id=? AND action='template_draft_saved'",
            (self.template_id,),
        ).fetchone()[0]
        self.assertGreaterEqual(saves, 7)

    def test_params_step_renders_cards_pool_and_no_jargon(self):
        status, _, page = form_request(self.server, "GET", f"/admin/creative/{self.template_id}?step=params", cookie=self.admin)
        self.assertEqual(status, 200)
        self.assertIn("页面要展示哪些参数？", page)
        self.assertIn("从推荐里加", page)
        self.assertIn("＋ 自定义参数…", page)
        self.assertIn("下一步 · 排内容", page)
        # 标准模板默认 8 个（现有字段）都在卡片里
        for label in ("材质", "功率", "安装尺寸", "流量范围", "扬程范围", "介质适用条件", "介质温度", "性能曲线目录点"):
            self.assertIn(label, page)
        # 未添加的推荐项可点添加；已添加项显示已加状态
        self.assertIn("name='pool_key' value='color'", page)
        self.assertIn("pchip added", page)
        # 唯一模块条 + 当前步高亮
        self.assertEqual(page.count("class='flowbar"), 1)
        self.assertIn("fs on'>选参数", page)
        # 术语：正文（去样式/脚本）不出现技术词「字段」；模块条无数字标识
        stripped = re.sub(r"<style>.*?</style>|<script>.*?</script>", "", page, flags=re.S)
        self.assertNotIn("字段", stripped)
        bar = page.split("class='flowbar", 1)[1].split("</div>", 1)[0]
        for digit in ("①", "②", "③", "④", "⑤"):
            self.assertNotIn(digit, bar)
        # 相邻步骤：排内容已是真实编辑器（切片 4），看效果仍为占位（切片 5）
        status, _, blocks_page = form_request(self.server, "GET", f"/admin/creative/{self.template_id}?step=blocks", cookie=self.admin)
        self.assertEqual(status, 200)
        self.assertIn("页面由哪些部分组成？", blocks_page)


class BlocksEditorTests(unittest.TestCase):
    """排内容编辑器：板块开关/排序/改标题、板块库、进阶折叠（AI 助手/分组名/版本记录）、自动保存。"""

    maxDiff = None

    @classmethod
    def setUpClass(cls):
        cls.temp_dir = tempfile.TemporaryDirectory()
        cls.server = make_server(Path(cls.temp_dir.name) / "app.db", port=0)
        cls.db = cls.server.RequestHandlerClass.db
        cls.db.seed_demo_users()
        cls.db.ensure_tenant("other-tenant", "另一家企业")
        cls.template_id = cls.db.create_template("demo-tenant", "工业输送设备", "demo-admin@example.invalid")
        cls.db.create_user("demo-tenant", "v103s4-nomod@example.invalid", "无权员工", "employee", "pass-blocks-1", "demo-admin@example.invalid")
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.admin = login(cls.server, "demo-admin@example.invalid", "demo-admin")
        cls.nomod = login(cls.server, "v103s4-nomod@example.invalid", "pass-blocks-1")

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.db.conn.close()
        cls.thread.join()
        cls.temp_dir.cleanup()

    def _draft(self):
        return json.loads(self.db.get_template("demo-tenant", self.template_id)["draft_json"])

    def _blocks(self):
        return self._draft()["blocks"]

    def _keys(self):
        return [b["key"] for b in self._blocks()]

    def _find(self, key):
        return next(b for b in self._blocks() if b["key"] == key)

    def _post(self, action, fields):
        return form_request(self.server, "POST", f"/admin/creative/{self.template_id}/blocks/{action}", fields, self.admin)

    def test_blocks_actions_journey(self):
        # 初始：12 板块、顺序=库顺序、hero 固定开启、图库默认关闭
        self.assertEqual(self._keys(), [item["key"] for item in TEMPLATE_BLOCK_LIBRARY])
        self.assertTrue(self._find("hero")["enabled"])
        self.assertFalse(self._find("gallery")["enabled"])
        # 开关：打开图库 → 关闭（回到初始）
        status, headers, _ = self._post("toggle", {"block_key": "gallery"})
        self.assertEqual(status, 303)
        self.assertEqual(headers.get("Location", ""), f"/admin/creative/{self.template_id}?step=blocks")
        self.assertTrue(self._find("gallery")["enabled"])
        self._post("toggle", {"block_key": "gallery"})
        self.assertFalse(self._find("gallery")["enabled"])
        # 首屏固定：不可关闭
        status, _, body = self._post("toggle", {"block_key": "hero"})
        self.assertEqual(status, 400)
        self.assertIn("固定", body)
        # 排序：specs 上移一格；首位上移 = 不变
        before = self._keys()
        self._post("move", {"block_key": "specs", "dir": "up"})
        after = self._keys()
        self.assertEqual(after.index("specs"), before.index("specs") - 1)
        self._post("move", {"block_key": after[0], "dir": "up"})
        self.assertEqual(self._keys(), after)
        # 改标题：设置 → 清空 → 再设置（供渲染用例断言）
        self._post("update", {"block_key": "specs", "title_zh": "核心规格", "title_en": "Core specs"})
        self.assertEqual(self._find("specs")["title_zh"], "核心规格")
        self._post("update", {"block_key": "specs", "title_zh": "", "title_en": ""})
        self.assertEqual(self._find("specs")["title_zh"], "")
        self._post("update", {"block_key": "specs", "title_zh": "核心规格", "title_en": "Core specs"})
        # 未知板块 → 400
        status, _, body = self._post("toggle", {"block_key": "ghost"})
        self.assertEqual(status, 400)
        self.assertIn("板块不存在", body)
        # 进阶：AI 助手偏好保存
        self._post("advanced", {"ai_extract": "优先提取性能相关参数", "ai_locate": ""})
        self.assertEqual(self._draft()["advanced"]["ai_prompts"]["extract"], "优先提取性能相关参数")
        # 进阶：分组名保存 / 空名回退默认
        self._post("group", {"group_zh": "性能参数", "group_en": "Performance"})
        self.assertEqual(self._draft()["groups"][0]["label_zh"], "性能参数")
        self._post("group", {"group_zh": "  ", "group_en": ""})
        self.assertEqual(self._draft()["groups"][0]["label_zh"], "规格参数")
        # 自动保存痕迹 + 审计累计（每次生效改动一条）
        status, _, page = form_request(self.server, "GET", f"/admin/creative/{self.template_id}?step=blocks", cookie=self.admin)
        self.assertIn("已自动保存", page)
        saves = self.db.conn.execute(
            "SELECT COUNT(*) FROM audit_log WHERE entity_type='template' AND entity_id=? AND action='template_draft_saved'",
            (self.template_id,),
        ).fetchone()[0]
        self.assertGreaterEqual(saves, 9)

    def test_blocks_cross_tenant_and_unauthorized(self):
        status, _, _ = form_request(
            self.server, "POST", f"/admin/creative/{self.template_id}/blocks/toggle", {"block_key": "gallery"}, self.nomod
        )
        self.assertEqual(status, 403)
        foreign = self.db.create_template("other-tenant", "别家品类", "setup")
        status, _, body = form_request(
            self.server, "POST", f"/admin/creative/{foreign}/blocks/toggle", {"block_key": "gallery"}, self.admin
        )
        self.assertEqual(status, 400)
        self.assertIn("展示页不存在", body)

    def test_blocks_step_renders_and_no_jargon(self):
        status, _, page = form_request(self.server, "GET", f"/admin/creative/{self.template_id}?step=blocks", cookie=self.admin)
        self.assertEqual(status, 200)
        self.assertIn("页面由哪些部分组成？", page)
        self.assertIn("从板块库添加", page)
        self.assertIn("下一步 · 看效果", page)
        self.assertIn("固定显示", page)
        self.assertIn("改标题", page)
        for item in TEMPLATE_BLOCK_LIBRARY:
            self.assertIn(item["label_zh"], page)
        # 关闭中的板块：注记 + 一键打开 chip；标题注记（旅程设置）
        self.assertIn("已关闭：买家看不到这部分", page)
        self.assertIn("＋ 图片与图库", page)
        self.assertIn("标题：核心规格", page)
        # 进阶折叠
        self.assertIn("AI 助手设置（进阶）", page)
        self.assertIn("分组名称（进阶）", page)
        self.assertIn("版本记录", page)
        self.assertIn("还没有发布历史", page)
        # 模块条：唯一 + 当前步高亮 + 相邻步骤可达
        self.assertEqual(page.count("class='flowbar"), 1)
        self.assertIn("fs on'>排内容", page)
        self.assertIn(f"href='/admin/creative/{self.template_id}?step=params'", page)
        self.assertIn(f"href='/admin/creative/{self.template_id}?step=preview'", page)
        # 术语：正文（去样式/脚本）不含「字段」
        stripped = re.sub(r"<style>.*?</style>|<script>.*?</script>", "", page, flags=re.S)
        self.assertNotIn("字段", stripped)


if __name__ == "__main__":
    unittest.main()
