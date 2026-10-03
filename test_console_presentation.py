"""控制台线稿式展示的回归测试：焦点卡、待办清单、下一步按钮、板块顺序。"""
import http.client
import tempfile
import threading
import unittest
from pathlib import Path
from urllib.parse import urlencode

from app import make_server


def approve_all_fields(db, tenant_id, product_id):
    for field in db.get_fields(tenant_id, product_id):
        if field["state"] == "candidate":
            db.approve_field(tenant_id, product_id, field["field_key"])
    db.finalize_product_review(tenant_id, product_id)


class ConsolePresentationTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "app.db"
        self.server = make_server(self.db_path, port=0)
        self.db = self.server.RequestHandlerClass.db
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.db.conn.close()
        self.thread.join()
        self.temp_dir.cleanup()

    def request(self, method, path, body=None, cookie=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.server.server_port)
        headers = {"Content-Type": "application/x-www-form-urlencoded"}
        if cookie:
            headers["Cookie"] = cookie
        conn.request(method, path, body=body, headers=headers)
        response = conn.getresponse()
        result = (response.status, dict(response.getheaders()), response.read().decode())
        conn.close()
        return result

    def login(self) -> str:
        _, headers, _ = self.request("POST", "/login", urlencode({"email": "demo-admin@example.invalid", "password": "demo-admin"}))
        return headers["Set-Cookie"].split(";", 1)[0]

    def test_dashboard_focus_card_and_todo_list(self):
        cookie = self.login()
        _, _, page = self.request("GET", "/admin", cookie=cookie)
        self.assertIn("class='focus'", page)
        self.assertIn("没有需要立即处理的事项", page)
        self.assertIn("待处理事项", page)
        self.assertIn("常用入口", page)
        self.assertIn("查看运营数字", page)
        # 有待核产品时，焦点卡切换为“产品资料核对”，待办清单出现“去核对”
        self.db.seed_demo_products("demo-tenant")
        _, _, page2 = self.request("GET", "/admin", cookie=cookie)
        self.assertIn("当前待办 · 产品资料核对", page2)
        self.assertIn("去核对", page2)
        self.assertIn("simple-item", page2)

    def test_products_list_with_next_step_rows(self):
        self.db.seed_demo_products("demo-tenant")
        cookie = self.login()
        _, _, page = self.request("GET", "/admin/products", cookie=cookie)
        self.assertIn("simple-item", page)
        self.assertIn("继续核对资料", page)
        self.assertIn("新建产品型号", page)  # 折叠入口
        product = next(item for item in self.db.list_products("demo-tenant") if item["model"] == "CB-80A")
        approve_all_fields(self.db, "demo-tenant", product["id"])
        self.db.publish_product("demo-tenant", product["id"])
        _, _, page2 = self.request("GET", "/admin/products", cookie=cookie)
        self.assertIn("查看与管理", page2)
        self.assertIn("买方页 ↗", page2)

    def test_product_detail_focus_and_primary_first_order(self):
        self.db.seed_demo_products("demo-tenant")
        product = next(item for item in self.db.list_products("demo-tenant") if item["model"] == "CB-80A")
        cookie = self.login()
        _, _, page = self.request("GET", f"/admin/products/{product['id']}", cookie=cookie)
        self.assertIn("class='focus'", page)
        self.assertIn("项候选字段", page)  # 焦点卡显示待核对数量
        idx_fields = page.index("id='fields'")
        idx_publish = page.index("发布与版本")
        idx_sources = page.index("id='sources'")
        idx_info = page.index("查看产品基本信息")
        self.assertTrue(idx_fields < idx_publish < idx_sources < idx_info, "主任务（字段核对）应排在最前，基本信息默认折叠在最后")


if __name__ == "__main__":
    unittest.main()
