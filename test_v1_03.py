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
    standard_template_draft,
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
        self.assertIn("看效果", home)
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


class PreviewTests(unittest.TestCase):
    """看效果 · 买家视角预览：草稿渲染（板块/参数/标题）、同源渲染、不产生公开数据、试看限制。"""

    maxDiff = None

    @classmethod
    def setUpClass(cls):
        cls.temp_dir = tempfile.TemporaryDirectory()
        cls.server = make_server(Path(cls.temp_dir.name) / "app.db", port=0)
        cls.db = cls.server.RequestHandlerClass.db
        cls.db.seed_demo_users()
        cls.db.ensure_tenant("other-tenant", "另一家企业")
        # 已发布产品（两份来源 + 两项核准字段）
        cls.published_id = cls.db.create_product("demo-tenant", "PV-100", "预览测试泵", "测试用途", "demo-admin@example.invalid")
        cls.source_id = cls.db.add_source("demo-tenant", cls.published_id, "PV-100-手册A.txt", "TXT", "第1页：参数表", "流量范围: 20-80 m³/h\n".encode(), "demo-admin@example.invalid", scope="exact_model")
        cls.db.add_source("demo-tenant", cls.published_id, "PV-100-手册B.txt", "TXT", "第1页：参数表", "扬程范围: 18-42 m\n".encode(), "demo-admin@example.invalid", scope="exact_model")
        cls.db.approve_field("demo-tenant", cls.published_id, "flow_range")
        cls.db.approve_field("demo-tenant", cls.published_id, "head_range")
        cls.db.finalize_product_review("demo-tenant", cls.published_id)
        cls.db.allow_public_source("demo-tenant", cls.published_id, cls.source_id, "demo-admin@example.invalid")
        cls.db.publish_product("demo-tenant", cls.published_id, "demo-admin@example.invalid")
        # 未发布产品（对照）
        cls.draft_id = cls.db.create_product("demo-tenant", "PV-200", "未发布泵", "测试用途", "demo-admin@example.invalid")
        # 模板：关闭 FAQ、改 specs 标题、禁用扬程参数、加自定义参数
        cls.template_id = cls.db.create_template("demo-tenant", "测试品类", "demo-admin@example.invalid")
        draft = json.loads(cls.db.get_template("demo-tenant", cls.template_id)["draft_json"])
        for block in draft["blocks"]:
            if block["key"] == "faq":
                block["enabled"] = False
            if block["key"] == "specs":
                block["title_zh"] = "核心规格"
                block["title_en"] = "Core specs"
        for param in draft["params"]:
            if param["key"] == "head_range":
                param["enabled"] = False
        draft["params"].append({"key": "custom_demo01", "label_zh": "防爆等级", "label_en": "Ex rating", "enabled": True})
        cls.db.save_template_draft("demo-tenant", cls.template_id, draft, "demo-admin@example.invalid")
        cls.db.create_user("demo-tenant", "v103s5-nomod@example.invalid", "无权员工", "employee", "pass-preview-1", "demo-admin@example.invalid")
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.admin = login(cls.server, "demo-admin@example.invalid", "demo-admin")
        cls.nomod = login(cls.server, "v103s5-nomod@example.invalid", "pass-preview-1")

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.db.conn.close()
        cls.thread.join()
        cls.temp_dir.cleanup()

    def test_preview_guards(self):
        # 未发布产品 → 400
        status, _, body = form_request(
            self.server, "GET", f"/admin/creative/{self.template_id}/preview?product_id={self.draft_id}", cookie=self.admin
        )
        self.assertEqual(status, 400)
        self.assertIn("还没有已发布版本", body)
        # 跨企业产品 id → 400（视为不可选）
        other = self.db.create_product("other-tenant", "OT-1", "别家产品", "测试", "setup")
        status, _, body = form_request(
            self.server, "GET", f"/admin/creative/{self.template_id}/preview?product_id={other}", cookie=self.admin
        )
        self.assertEqual(status, 400)
        self.assertIn("本企业", body)
        # 无权员工 → 403；展示页不存在 → 404
        status, _, _ = form_request(self.server, "GET", f"/admin/creative/{self.template_id}/preview", cookie=self.nomod)
        self.assertEqual(status, 403)
        status, _, _ = form_request(self.server, "GET", "/admin/creative/" + "b" * 24 + "/preview", cookie=self.admin)
        self.assertEqual(status, 404)

    def test_preview_renders_draft_and_does_not_log_events(self):
        before = self.db.conn.execute("SELECT COUNT(*) FROM product_events WHERE tenant_id='demo-tenant'").fetchone()[0]
        status, _, page = form_request(
            self.server, "GET", f"/admin/creative/{self.template_id}/preview?product_id={self.published_id}&lang=zh", cookie=self.admin
        )
        self.assertEqual(status, 200)
        self.assertIn("预览 · 未发布", page)          # 横幅
        self.assertIn("预览测试泵", page)
        self.assertIn("核心规格", page)                # 区块标题覆盖
        self.assertIn("防爆等级", page)                # 自定义参数（缺项行）
        self.assertIn("nolog=1", page)                 # 预览内资料/图片链接带 nolog（不产生事件）
        self.assertNotIn("扬程范围", page)             # 被禁用参数不再出现（含要点/规格/对比）
        self.assertNotIn("id='faq'", page)             # 被关闭板块不渲染
        self.assertNotIn("#faq", page)                 # 锚点与首屏按钮联动移除
        # 预览不产生公开数据：无事件、无询价表单、无聊天窗
        after = self.db.conn.execute("SELECT COUNT(*) FROM product_events WHERE tenant_id='demo-tenant'").fetchone()[0]
        self.assertEqual(before, after)
        self.assertNotIn("id='product-rfq'", page)
        self.assertNotIn('id="chat-launcher"', page)
        # 缺省产品（不带 product_id）→ 落到已发布产品
        status, _, default_page = form_request(self.server, "GET", f"/admin/creative/{self.template_id}/preview", cookie=self.admin)
        self.assertEqual(status, 200)
        self.assertIn("PV-100", default_page)
        # 英文覆盖
        status, _, en_page = form_request(
            self.server, "GET", f"/admin/creative/{self.template_id}/preview?product_id={self.published_id}&lang=en", cookie=self.admin
        )
        self.assertEqual(status, 200)
        self.assertIn("Core specs", en_page)
        self.assertIn("Ex rating", en_page)

    def test_source_route_nolog_flag(self):
        # 公开资料路由：正常访问记一条下载事件；带 nolog=1 不记录（预览链路专用）
        def count():
            return self.db.conn.execute("SELECT COUNT(*) FROM product_events WHERE tenant_id='demo-tenant'").fetchone()[0]
        before = count()
        status, _, _ = form_request(self.server, "GET", f"/products/pv-100/sources/{self.source_id}?nolog=1")
        self.assertEqual(status, 200)
        self.assertEqual(count(), before)
        status, _, _ = form_request(self.server, "GET", f"/products/pv-100/sources/{self.source_id}")
        self.assertEqual(status, 200)
        self.assertEqual(count(), before + 1)

    def test_preview_step_shows_controls(self):
        status, _, page = form_request(self.server, "GET", f"/admin/creative/{self.template_id}?step=preview", cookie=self.admin)
        self.assertEqual(status, 200)
        self.assertIn("看效果 · 买家视角", page)
        self.assertIn("打开买家视角预览", page)
        self.assertIn("满意，去应用", page)
        self.assertIn("PV-100", page)
        self.assertIn("fs on'>看效果", page)
        self.assertEqual(page.count("class='flowbar'"), 1)


class PublishFlowTests(unittest.TestCase):
    """产品页联动：提交/确认上线/撤回、产品发布冻结引用、公开页按引用渲染（回归锁）。"""

    maxDiff = None

    @classmethod
    def setUpClass(cls):
        cls.temp_dir = tempfile.TemporaryDirectory()
        cls.server = make_server(Path(cls.temp_dir.name) / "app.db", port=0)
        cls.db = cls.server.RequestHandlerClass.db
        cls.db.seed_demo_users()
        cls.db.ensure_tenant("other-tenant", "另一家企业")
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.admin = login(cls.server, "demo-admin@example.invalid", "demo-admin")
        cls.db.create_user("demo-tenant", "v103s6-emp@example.invalid", "联动员工", "employee", "pass-publish-1", "demo-admin@example.invalid")
        emp_row = cls.db.conn.execute("SELECT id FROM users WHERE email=?", ("v103s6-emp@example.invalid",)).fetchone()
        cls.db.set_user_modules("demo-tenant", emp_row["id"], ["creative"], "demo-admin@example.invalid")
        cls.emp = login(cls.server, "v103s6-emp@example.invalid", "pass-publish-1")

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.temp_dir.cleanup()

    # ── 工具 ──────────────────────────────────────────────────────────────
    def _make_product(self, model, category):
        pid = self.db.create_product("demo-tenant", model, f"{model} 测试机", "测试用途", "demo-admin@example.invalid")
        self.db.set_category_candidate("demo-tenant", pid, category, "manual", "demo-admin@example.invalid")
        self.db.approve_category("demo-tenant", pid, "demo-admin@example.invalid")
        self.db.add_source("demo-tenant", pid, f"{model}-手册.txt", "TXT", "第1页：参数表", "流量范围: 20-80 m³/h\n".encode(), "demo-admin@example.invalid", scope="exact_model")
        self.db.approve_field("demo-tenant", pid, "flow_range")
        self.db.finalize_product_review("demo-tenant", pid)
        return pid

    def _make_template(self, category, specs_title_zh="联动规格"):
        tid = self.db.create_template("demo-tenant", category, "demo-admin@example.invalid")
        draft = json.loads(self.db.get_template("demo-tenant", tid)["draft_json"])
        for block in draft["blocks"]:
            if block["key"] == "specs":
                block["title_zh"] = specs_title_zh
                block["title_en"] = "Linked specs"
            if block["key"] == "faq":
                block["enabled"] = False
        self.db.save_template_draft("demo-tenant", tid, draft, "demo-admin@example.invalid")
        return tid

    def _get_public(self, model, lang="zh"):
        status, _, page = form_request(self.server, "GET", f"/products/{model.lower()}?lang={lang}")
        self.assertEqual(status, 200)
        return page

    def _audit_actions(self):
        rows = self.db.conn.execute("SELECT action FROM audit_log WHERE tenant_id='demo-tenant'").fetchall()
        return [row["action"] for row in rows]

    # ── 用例 ──────────────────────────────────────────────────────────────
    def test_submit_confirm_withdraw_permissions(self):
        tid = self._make_template("权限品类")
        # 员工提交给管理员
        status, headers, _ = form_request(self.server, "POST", f"/admin/creative/{tid}/publish/submit", {}, self.emp)
        self.assertEqual(status, 303)
        self.assertIn("notice=submitted", headers.get("Location", ""))
        self.assertEqual(self.db.get_template("demo-tenant", tid)["status"], "submitted")
        # 员工不能确认上线 / 撤回
        status, _, body = form_request(self.server, "POST", f"/admin/creative/{tid}/publish/confirm", {}, self.emp)
        self.assertEqual(status, 403)
        self.assertIn("只对管理员开放", body)
        status, _, _ = form_request(self.server, "POST", f"/admin/creative/{tid}/publish/withdraw", {}, self.emp)
        self.assertEqual(status, 403)
        self.assertEqual(self.db.get_template("demo-tenant", tid)["published_version"] or 0, 0)
        # 管理员视角可见「已提交，待确认」
        status, _, page = form_request(self.server, "GET", f"/admin/creative/{tid}?step=publish", cookie=self.admin)
        self.assertIn("已提交，待确认", page)
        # 管理员确认上线 → v1 冻结
        status, headers, _ = form_request(self.server, "POST", f"/admin/creative/{tid}/publish/confirm", {}, self.admin)
        self.assertEqual(status, 303)
        self.assertIn("notice=published", headers.get("Location", ""))
        self.assertIn("v=1", headers.get("Location", ""))
        template = self.db.get_template("demo-tenant", tid)
        self.assertEqual(template["published_version"], 1)
        self.assertEqual(template["status"], "published")
        versions = self.db.list_template_versions("demo-tenant", tid)
        self.assertEqual(len(versions), 1)
        self.assertEqual(versions[0]["approved_by"], "demo-admin@example.invalid")
        # 管理员撤回
        status, headers, _ = form_request(self.server, "POST", f"/admin/creative/{tid}/publish/withdraw", {}, self.admin)
        self.assertEqual(status, 303)
        self.assertEqual(self.db.get_template("demo-tenant", tid)["status"], "withdrawn")
        # 三个动作都有审计
        actions = self._audit_actions()
        self.assertIn("template_submitted", actions)
        self.assertIn("template_published", actions)
        self.assertIn("template_withdrawn", actions)

    def test_withdraw_without_version_rejected(self):
        tid = self._make_template("空撤回品类")
        status, _, body = form_request(self.server, "POST", f"/admin/creative/{tid}/publish/withdraw", {}, self.admin)
        self.assertEqual(status, 400)
        self.assertIn("还没有上线版本", body)

    def test_publish_binds_version_and_frozen_render(self):
        pid = self._make_product("XJ-100", "联动品类")
        tid = self._make_template("联动品类", "联动规格")
        self.db.publish_template_version("demo-tenant", tid, "demo-admin@example.invalid")  # v1
        self.db.publish_product("demo-tenant", pid, "demo-admin@example.invalid")
        snapshot = json.loads(self.db.get_published_version("demo-tenant", pid)["snapshot_json"])
        self.assertEqual(snapshot["template"]["version"], 1)
        self.assertEqual(snapshot["template"]["template_id"], tid)
        page = self._get_public("XJ-100")
        self.assertIn("联动规格", page)
        self.assertNotIn("常见问题", page)  # FAQ 板块被关闭
        self.assertNotIn("首页 › 产品 › 水泵", page)  # 审定去行业词：面包屑
        self.assertIn("首页 › 产品", page)
        self.assertIn("| Product", page)  # 页面标题品牌后缀
        # 草稿改新标题并确认 v2 → 公开页仍显示 v1 内容（发布即冻结）
        draft = json.loads(self.db.get_template("demo-tenant", tid)["draft_json"])
        for block in draft["blocks"]:
            if block["key"] == "specs":
                block["title_zh"] = "第二版规格"
        self.db.save_template_draft("demo-tenant", tid, draft, "demo-admin@example.invalid")
        self.db.publish_template_version("demo-tenant", tid, "demo-admin@example.invalid")  # v2
        page = self._get_public("XJ-100")
        self.assertIn("联动规格", page)
        self.assertNotIn("第二版规格", page)
        # 产品重新发布 → 应用 v2
        self.db.publish_product("demo-tenant", pid, "demo-admin@example.invalid")
        page = self._get_public("XJ-100")
        self.assertIn("第二版规格", page)

    def test_withdraw_stops_new_references(self):
        pid1 = self._make_product("XJ-200", "撤回品类")
        tid = self._make_template("撤回品类", "撤回规格")
        self.db.publish_template_version("demo-tenant", tid, "demo-admin@example.invalid")
        self.db.publish_product("demo-tenant", pid1, "demo-admin@example.invalid")
        self.db.withdraw_template("demo-tenant", tid, "demo-admin@example.invalid")
        # 已发布产品维持之前发布的版本
        self.assertIn("撤回规格", self._get_public("XJ-200"))
        # 撤回后新产品发布不再引用 → 平台标准样式
        pid2 = self._make_product("XJ-201", "撤回品类")
        self.db.publish_product("demo-tenant", pid2, "demo-admin@example.invalid")
        snapshot2 = json.loads(self.db.get_published_version("demo-tenant", pid2)["snapshot_json"])
        self.assertNotIn("template", snapshot2)
        page2 = self._get_public("XJ-201")
        self.assertNotIn("撤回规格", page2)
        self.assertIn("技术参数", page2)
        # 老产品重新发布 → 也停止引用，回退标准样式
        self.db.publish_product("demo-tenant", pid1, "demo-admin@example.invalid")
        self.assertNotIn("撤回规格", self._get_public("XJ-200"))

    def test_publish_step_ui_and_relink_hint(self):
        pid = self._make_product("XJ-300", "提示品类")
        tid = self._make_template("提示品类", "提示规格")
        self.db.publish_product("demo-tenant", pid, "demo-admin@example.invalid")
        # 管理员视角：有确认按钮、无撤回按钮；关联产品等待上线
        status, _, page = form_request(self.server, "GET", f"/admin/creative/{tid}?step=publish", cookie=self.admin)
        self.assertEqual(status, 200)
        self.assertIn("publish/confirm", page)
        self.assertNotIn("publish/submit", page)
        self.assertNotIn("撤回（停止新引用）", page)
        self.assertIn("会用到这份展示页的产品", page)
        self.assertIn("XJ-300", page)
        self.assertIn("确认上线后，重新发布该产品即可应用", page)
        # 确认上线 v1 → 出现撤回按钮；产品提示「需要重新发布」
        self.db.publish_template_version("demo-tenant", tid, "demo-admin@example.invalid")
        status, _, page = form_request(self.server, "GET", f"/admin/creative/{tid}?step=publish", cookie=self.admin)
        self.assertIn("撤回（停止新引用）", page)
        self.assertIn("需要「重新发布」才生效", page)
        self.assertIn("最近上线：v1", page)
        # 产品重新发布 → 已应用 v1
        self.db.publish_product("demo-tenant", pid, "demo-admin@example.invalid")
        status, _, page = form_request(self.server, "GET", f"/admin/creative/{tid}?step=publish", cookie=self.admin)
        self.assertIn("已应用 v1", page)
        # 员工视角：只有提交按钮，没有确认/撤回
        status, _, page = form_request(self.server, "GET", f"/admin/creative/{tid}?step=publish", cookie=self.emp)
        self.assertEqual(status, 200)
        self.assertIn("publish/submit", page)
        self.assertNotIn("publish/confirm", page)
        self.assertNotIn("publish/withdraw", page)

    def test_unpublished_changes_badges(self):
        tid = self._make_template("徽标品类")
        self.db.publish_template_version("demo-tenant", tid, "demo-admin@example.invalid")
        status, _, page = form_request(self.server, "GET", f"/admin/creative/{tid}", cookie=self.admin)
        self.assertIn("已上线 v1", page)
        self.assertNotIn("有改动还没发布", page)
        # 改草稿 → 出现「有改动还没发布」
        draft = json.loads(self.db.get_template("demo-tenant", tid)["draft_json"])
        for block in draft["blocks"]:
            if block["key"] == "specs":
                block["title_zh"] = "改后又改"
        self.db.save_template_draft("demo-tenant", tid, draft, "demo-admin@example.invalid")
        status, _, page = form_request(self.server, "GET", f"/admin/creative/{tid}", cookie=self.admin)
        self.assertIn("已上线 v1", page)
        self.assertIn("有改动还没发布", page)
        # 再确认上线 → 徽标消失
        self.db.publish_template_version("demo-tenant", tid, "demo-admin@example.invalid")
        status, _, page = form_request(self.server, "GET", f"/admin/creative/{tid}", cookie=self.admin)
        self.assertIn("已上线 v2", page)
        self.assertNotIn("有改动还没发布", page)

    def test_publish_action_cross_tenant(self):
        foreign = self.db.create_template("other-tenant", "别家品类", "setup")
        status, _, body = form_request(self.server, "POST", f"/admin/creative/{foreign}/publish/confirm", {}, self.admin)
        self.assertEqual(status, 400)
        self.assertIn("展示页不存在", body)


class TemplateAuditTests(unittest.TestCase):
    """切片7：审计与合规——六动作中文标签、恢复默认、操作记录可见可筛、术语与数字化断言。"""

    maxDiff = None

    @classmethod
    def setUpClass(cls):
        cls.temp_dir = tempfile.TemporaryDirectory()
        cls.server = make_server(Path(cls.temp_dir.name) / "app.db", port=0)
        cls.db = cls.server.RequestHandlerClass.db
        cls.db.seed_demo_users()
        cls.db.ensure_tenant("other-tenant", "另一家企业")
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.admin = login(cls.server, "demo-admin@example.invalid", "demo-admin")
        cls.db.create_user("demo-tenant", "v103s7-emp@example.invalid", "审计员工", "employee", "pass-audit-1", "demo-admin@example.invalid")
        emp_row = cls.db.conn.execute("SELECT id FROM users WHERE email=?", ("v103s7-emp@example.invalid",)).fetchone()
        cls.db.set_user_modules("demo-tenant", emp_row["id"], ["creative"], "demo-admin@example.invalid")
        cls.emp = login(cls.server, "v103s7-emp@example.invalid", "pass-audit-1")

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.temp_dir.cleanup()

    def _make_template(self, category):
        return self.db.create_template("demo-tenant", category, "demo-admin@example.invalid")

    def _template_audit_rows(self):
        return self.db.conn.execute(
            "SELECT action, entity_type, details, actor FROM audit_log WHERE tenant_id='demo-tenant' AND entity_type='template' ORDER BY id DESC"
        ).fetchall()

    def test_all_template_actions_have_chinese_labels(self):
        expected = {
            "template_created": "创建展示页",
            "template_draft_saved": "保存展示页改动",
            "template_submitted": "提交展示页给管理员",
            "template_published": "确认上线展示页",
            "template_withdrawn": "撤回展示页",
            "template_restored_defaults": "恢复默认设置",
        }
        for action, label in expected.items():
            self.assertEqual(AUDIT_ACTION_LABELS.get(action), label, action)
        self.assertEqual(AUDIT_ENTITY_LABELS.get("template"), "产品展示页")

    def test_six_actions_produce_audit_rows(self):
        tid = self._make_template("审计动作品类")
        draft = json.loads(self.db.get_template("demo-tenant", tid)["draft_json"])
        draft["params"] = draft["params"][:3]
        self.db.save_template_draft("demo-tenant", tid, draft, "v103s7-emp@example.invalid", detail="params.toggle")
        self.db.submit_template("demo-tenant", tid, "v103s7-emp@example.invalid")
        self.db.publish_template_version("demo-tenant", tid, "demo-admin@example.invalid")
        self.db.withdraw_template("demo-tenant", tid, "demo-admin@example.invalid")
        self.db.restore_template_defaults("demo-tenant", tid, "v103s7-emp@example.invalid")
        rows = [row for row in self._template_audit_rows()]
        actions = {row["action"] for row in rows}
        for name in ("template_created", "template_draft_saved", "template_submitted", "template_published", "template_withdrawn", "template_restored_defaults"):
            self.assertIn(name, actions)
        # 改动类审计带操作明细（可判读）；发布类带版本号
        saved = next(row for row in rows if row["action"] == "template_draft_saved")
        self.assertEqual(saved["details"], "params.toggle")
        published = next(row for row in rows if row["action"] == "template_published")
        self.assertEqual(published["details"], "version=1")
        # 操作者如实记录（员工与管理员）
        actors = {row["actor"] for row in rows}
        self.assertIn("v103s7-emp@example.invalid", actors)
        self.assertIn("demo-admin@example.invalid", actors)

    def test_restore_defaults_action_and_guard(self):
        tid = self._make_template("恢复品类")
        draft = json.loads(self.db.get_template("demo-tenant", tid)["draft_json"])
        for block in draft["blocks"]:
            if block["key"] == "faq":
                block["enabled"] = False
        draft["params"] = []
        self.db.save_template_draft("demo-tenant", tid, draft, "demo-admin@example.invalid")
        # 员工执行恢复（草稿动作，不需要管理员）
        status, headers, _ = form_request(self.server, "POST", f"/admin/creative/{tid}/restore_defaults", {}, self.emp)
        self.assertEqual(status, 303)
        self.assertIn("notice=restored", headers.get("Location", ""))
        restored = json.loads(self.db.get_template("demo-tenant", tid)["draft_json"])
        self.assertEqual(restored, standard_template_draft())
        # 已发布版本不受影响（恢复不触碰 template_versions）
        # 页面：恢复提示 + 进阶折叠中的按钮（用回跳地址取页，等价浏览器跟随跳转）
        status, _, page = form_request(self.server, "GET", headers.get("Location", f"/admin/creative/{tid}?step=blocks"), cookie=self.admin)
        self.assertEqual(status, 200)
        self.assertIn("已恢复为标准默认设置", page)
        self.assertIn("恢复默认设置（进阶）", page)
        self.assertIn(f"/admin/creative/{tid}/restore_defaults", page)
        # 审计
        actions = {row["action"] for row in self._template_audit_rows()}
        self.assertIn("template_restored_defaults", actions)
        # 跨租户拒绝
        foreign = self.db.create_template("other-tenant", "别家恢复品类", "setup")
        status, _, body = form_request(self.server, "POST", f"/admin/creative/{foreign}/restore_defaults", {}, self.admin)
        self.assertEqual(status, 400)
        self.assertIn("展示页不存在", body)

    def test_audit_page_visible_and_filterable(self):
        tid = self._make_template("操作记录品类")
        self.db.submit_template("demo-tenant", tid, "v103s7-emp@example.invalid")
        self.db.create_template("other-tenant", "别家记录品类", "setup")
        # 管理员访问：中文标签 + 可筛选项
        status, _, page = form_request(self.server, "GET", "/admin/audit", cookie=self.admin)
        self.assertEqual(status, 200)
        self.assertIn("提交展示页给管理员", page)
        self.assertIn("产品展示页", page)
        self.assertNotIn("category=别家记录品类", page)
        # 按对象筛选 type=template
        status, _, page = form_request(self.server, "GET", "/admin/audit?type=template", cookie=self.admin)
        self.assertEqual(status, 200)
        self.assertIn("提交展示页给管理员", page)
        self.assertIn("option value='template' selected", page)
        self.assertNotIn("category=别家记录品类", page)
        # 非管理员访问被拒
        status, _, _ = form_request(self.server, "GET", "/admin/audit", cookie=self.emp)
        self.assertEqual(status, 403)

    def test_editor_terminology_and_digit_assertions(self):
        tid = self._make_template("术语品类")
        expectations = {
            "params": "页面要展示哪些参数？",
            "blocks": "页面由哪些部分组成？",
            "preview": "看效果 · 买家视角",
            "publish": "产品页联动",
        }
        banned = ("字段", "区块", "提示词", "草稿")
        for step, phrase in expectations.items():
            status, _, page = form_request(self.server, "GET", f"/admin/creative/{tid}?step={step}", cookie=self.admin)
            self.assertEqual(status, 200)
            self.assertIn(phrase, page)
            stripped = re.sub(r"<style>.*?</style>|<script>.*?</script>", "", page, flags=re.S)
            for word in banned:
                self.assertNotIn(word, stripped, f"主路径出现禁用词「{word}」（step={step}）")
            self.assertEqual(page.count("class='flowbar"), 1)
            bar = re.search(r"<div class='flowbar.*?</div>", page, flags=re.S)
            self.assertIsNotNone(bar)
            bar_text = re.sub(r"href='[^']*'", "", bar.group(0))
            self.assertIsNone(re.search(r"[0-9]", bar_text), f"模块条不应含数字: step={step}")
        # 首页同样检查（「标准模板（推荐）」为 v0.3 定稿命名，不在禁用清单内）
        pid = self.db.create_product("demo-tenant", "S8-TERM-100", "术语测试机", "测试用途", "demo-admin@example.invalid")
        self.db.set_category_candidate("demo-tenant", pid, "术语准备品类", "manual", "demo-admin@example.invalid")
        self.db.approve_category("demo-tenant", pid, "demo-admin@example.invalid")
        status, _, home = form_request(self.server, "GET", "/admin/creative", cookie=self.admin)
        self.assertEqual(status, 200)
        home_stripped = re.sub(r"<style>.*?</style>|<script>.*?</script>", "", home, flags=re.S)
        for word in banned:
            self.assertNotIn(word, home_stripped, f"首页出现禁用词「{word}」")
        self.assertIn("标准模板（推荐）", home)
        # 管理员的编辑区带「操作记录」入口；员工没有
        status, _, page = form_request(self.server, "GET", f"/admin/creative/{tid}", cookie=self.admin)
        self.assertIn("/admin/audit?type=template", page)
        self.assertIn("操作记录", page)
        status, _, page = form_request(self.server, "GET", f"/admin/creative/{tid}", cookie=self.emp)
        self.assertNotIn("/admin/audit?type=template", page)


if __name__ == "__main__":
    unittest.main()
