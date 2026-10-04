"""卖家侧栏待办数字：渲染口径、角色过滤、轮询接口的鉴权与跨租户隔离、封顶与动画钩子。

与概览页待办同源（同一计数函数）——本文件断言的数字必须与 `get_admin_summary` 一一对应。
"""
import http.client
import json
import tempfile
import threading
import unittest
from pathlib import Path
from urllib.parse import urlencode

from app import console_layout, make_server


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


def json_request(server, path, cookie=None):
    status, _, raw = form_request(server, "GET", path, cookie=cookie)
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        parsed = None
    return status, parsed


def login(server, email, password):
    status, headers, _ = form_request(server, "POST", "/login", {"email": email, "password": password})
    if status not in (302, 303) or "Set-Cookie" not in headers:
        raise AssertionError(f"登录失败: {email} -> {status}")
    return headers["Set-Cookie"].split(";", 1)[0]


class ConsoleNavCountTests(unittest.TestCase):
    maxDiff = None

    @classmethod
    def setUpClass(cls):
        cls.temp_dir = tempfile.TemporaryDirectory()
        db_path = Path(cls.temp_dir.name) / "app.db"
        cls.server = make_server(db_path, port=0)
        cls.db = cls.server.RequestHandlerClass.db
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

        cls.db.seed_demo_users()
        cls.db.ensure_tenant("second-tenant")
        cls.db.create_user("second-tenant", "second-admin@example.invalid", "第二租户管理员", "admin", "second-admin", "setup")

        # 演示租户场景：待核产品 1、待答任务 2、待补充 RFQ 1、逾期线索 1
        cls.db.create_product("demo-tenant", "NAV-DRAFT-1", "待核对样例泵", "测试用途", "setup")
        conversation_id = cls.db.create_conversation("demo-tenant", "nav-test", "en")
        for index in range(2):
            message_id = cls.db.save_message("demo-tenant", conversation_id, "buyer", f"nav question {index}")
            cls.db.create_human_task("demo-tenant", conversation_id, message_id, f"nav question {index}", "nav test", {})
        rfq_id, _ = cls.db.create_rfq("demo-tenant", conversation_id, "nav-rfq-1", {"contact_email": "buyer@example.com"}, "", None)
        cls.db.conn.execute("UPDATE leads SET next_follow_up='2020-01-01' WHERE rfq_id=?", (rfq_id,))
        cls.db.conn.commit()

        cls.admin_cookie = login(cls.server, "demo-admin@example.invalid", "demo-admin")
        cls.sales_cookie = login(cls.server, "demo-sales@example.invalid", "demo-sales")
        cls.engineer_cookie = login(cls.server, "demo-engineer@example.invalid", "demo-engineer")
        cls.second_cookie = login(cls.server, "second-admin@example.invalid", "second-admin")

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.db.conn.close()
        cls.thread.join()
        cls.temp_dir.cleanup()

    def test_admin_badges_render_with_db_counts(self):
        status, _, page = form_request(self.server, "GET", "/admin", cookie=self.admin_cookie)
        self.assertEqual(status, 200)
        # 四类数字与数据库现状一一对应
        for key, value in (("products", 1), ("tasks", 2), ("rfqs", 1), ("sales", 1)):
            self.assertIn(f"data-nav-count='{key}' data-count='{value}'", page, key)
            self.assertNotIn(f"data-nav-count='{key}' data-count='{value}' hidden", page, key)
        # 其余导航项与 0 值：保留占位但隐藏
        for key in ("admin", "users", "sites"):
            self.assertIn(f"data-nav-count='{key}' data-count='0' hidden", page, key)
        # 告急样式（参照好友参考稿的方法）：人工任务与逾期跟进红字加粗；产品与 RFQ 为普通数字
        self.assertIn("class='side-count hot' data-nav-count='tasks'", page)
        self.assertIn("class='side-count hot' data-nav-count='sales'", page)
        self.assertIn("class='side-count' data-nav-count='products'", page)
        self.assertIn("class='side-count' data-nav-count='rfqs'", page)

    def test_role_filter_rendering_and_endpoint(self):
        # 销售：只有 RFQ 与客户跟进两项数字；任务/产品保持隐藏
        _, _, sales_page = form_request(self.server, "GET", "/admin", cookie=self.sales_cookie)
        self.assertIn("data-nav-count='rfqs' data-count='1'", sales_page)
        self.assertIn("data-nav-count='sales' data-count='1'", sales_page)
        self.assertIn("class='side-count hot' data-nav-count='sales'", sales_page)
        self.assertIn("class='side-count' data-nav-count='rfqs'", sales_page)
        self.assertIn("data-nav-count='tasks' data-count='0' hidden", sales_page)
        self.assertIn("data-nav-count='products' data-count='0' hidden", sales_page)
        # 工程师：只有人工任务一项数字
        _, _, engineer_page = form_request(self.server, "GET", "/admin", cookie=self.engineer_cookie)
        self.assertIn("data-nav-count='tasks' data-count='2'", engineer_page)
        self.assertIn("class='side-count hot' data-nav-count='tasks'", engineer_page)
        self.assertIn("data-nav-count='rfqs' data-count='0' hidden", engineer_page)
        self.assertIn("data-nav-count='sales' data-count='0' hidden", engineer_page)
        self.assertIn("data-nav-count='products' data-count='0' hidden", engineer_page)
        # 轮询接口：角色过滤 + 只返回数字
        status, body = json_request(self.server, "/admin/nav-counts", cookie=self.admin_cookie)
        self.assertEqual(status, 200)
        self.assertEqual(body, {"counts": {"products": 1, "tasks": 2, "rfqs": 1, "sales": 1}})
        _, body = json_request(self.server, "/admin/nav-counts", cookie=self.sales_cookie)
        self.assertEqual(body, {"counts": {"rfqs": 1, "sales": 1}})
        _, body = json_request(self.server, "/admin/nav-counts", cookie=self.engineer_cookie)
        self.assertEqual(body, {"counts": {"tasks": 2}})
        # 未登录：不得返回 200 数字
        status, _ = json_request(self.server, "/admin/nav-counts")
        self.assertIn(status, (302, 303, 401))

    def test_second_tenant_isolation(self):
        _, _, page = form_request(self.server, "GET", "/admin", cookie=self.second_cookie)
        for key in ("products", "tasks", "rfqs", "sales"):
            self.assertIn(f"data-nav-count='{key}' data-count='0' hidden", page, key)
        status, body = json_request(self.server, "/admin/nav-counts", cookie=self.second_cookie)
        self.assertEqual(status, 200)
        self.assertEqual(body, {"counts": {"products": 0, "tasks": 0, "rfqs": 0, "sales": 0}})

    def test_cap_and_animation_hooks(self):
        # 99+ 封顶（保留原始数值用于比对）；告急样式同样生效
        capped = console_layout("t", "<p>x</p>", "admin", nav_counts={"tasks": 120})
        self.assertIn(">99+<", capped)
        self.assertIn("data-count='120'", capped)
        self.assertIn("class='side-count hot' data-nav-count='tasks'", capped)
        # 页面携带轮询脚本与翻牌钩子（含"减少动态效果"的降级样式）
        _, _, page = form_request(self.server, "GET", "/admin", cookie=self.admin_cookie)
        for marker in ("/admin/nav-counts", "setInterval(pollNavCounts", "prefers-reduced-motion", "flip-out", "flip-in", "side-count", "POLL_INTERVAL_MS = 5000", "HOT_KEYS", "side-count.hot"):
            self.assertIn(marker, page, marker)


if __name__ == "__main__":
    unittest.main()
