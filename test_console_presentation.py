"""控制台线稿式展示的回归测试：焦点卡、待办清单、下一步按钮、板块顺序。"""
import http.client
import json
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
        # 控制台走查需要演示账号与演示企业：显式种入（普通新库不再预置）。
        self.db.seed_demo_users()
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.db.conn.close()
        self.thread.join()
        self.temp_dir.cleanup()

    def request(self, method, path, body=None, cookie=None, json_body=None, extra_headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.server.server_port)
        headers = {"Content-Type": "application/json" if json_body is not None else "application/x-www-form-urlencoded"}
        if extra_headers:
            headers.update(extra_headers)
        if cookie:
            headers["Cookie"] = cookie
        payload = json.dumps(json_body) if json_body is not None else body
        conn.request(method, path, body=payload, headers=headers)
        response = conn.getresponse()
        result = (response.status, dict(response.getheaders()), response.read().decode())
        conn.close()
        return result

    def login(self, email="demo-admin@example.invalid", password="demo-admin") -> str:
        _, headers, _ = self.request("POST", "/login", urlencode({"email": email, "password": password}))
        return headers["Set-Cookie"].split(";", 1)[0]

    def create_rfq(self) -> str:
        """通过买方 API 走真实链路创建一份 RFQ，返回 RFQ 编号。"""
        self.db.register_buyer_site("demo-tenant", "site-a", "https://seller-a.example")
        _, _, created_text = self.request("POST", "/api/v1/buyer/conversations", json_body={"site_key": "site-a"})
        created = json.loads(created_text)
        conversation_id = created["conversation_id"]
        extra = {"X-Site-Key": "site-a", "Authorization": f"Bearer {created['access_token']}"}
        _, _, sent_text = self.request("POST", f"/api/v1/buyer/conversations/{conversation_id}/messages", json_body={"text": "flow 50 m3/h, clean water"}, extra_headers=extra)
        draft = json.loads(sent_text)["requirement_draft"]
        _, _, confirmed_text = self.request("POST", f"/api/v1/buyer/conversations/{conversation_id}/requirements/confirm", json_body={"base_revision_id": draft["revision_id"], "values": draft["values"]}, extra_headers=extra)
        confirmed = json.loads(confirmed_text)
        assert confirmed["revision_id"]
        _, _, rfq_text = self.request("POST", f"/api/v1/buyer/conversations/{conversation_id}/rfqs", json_body={"contact_confirmed": True, "contact": {"contact_email": "buyer@example.com", "quantity": "2"}}, extra_headers={**extra, "Idempotency-Key": "console-test-rfq"})
        return json.loads(rfq_text)["rfq_id"]

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

    def test_sales_and_rfq_next_step_presentation(self):
        rfq_id = self.create_rfq()
        admin = self.login()
        sales = self.login("demo-sales@example.invalid", "demo-sales")
        # 销售列表：条目行 + 打开线索按钮
        _, _, page = self.request("GET", "/admin/sales", cookie=admin)
        self.assertIn("打开线索 →", page)
        self.assertIn(rfq_id, page)
        # 未领取线索（销售视角）：焦点卡给出“领取”主操作
        _, _, detail_sales = self.request("GET", f"/admin/sales/{rfq_id}", cookie=sales)
        self.assertIn("class='focus'", detail_sales)
        self.assertIn("当前任务 · 领取", detail_sales)
        self.assertIn("领取这条会话", detail_sales)
        # 管理员视角：焦点卡给出“分配”，且页面含改派入口
        _, _, detail_admin = self.request("GET", f"/admin/sales/{rfq_id}", cookie=admin)
        self.assertIn("当前任务 · 分配", detail_admin)
        self.assertIn("分配或改派", detail_admin)
        # RFQ 详情：焦点卡显示当前状态
        _, _, rfq_page = self.request("GET", f"/admin/rfqs/{rfq_id}", cookie=admin)
        self.assertIn("class='focus'", rfq_page)
        self.assertIn("待补充", rfq_page)

    def test_team_and_sites_pages_use_wireframe_blocks(self):
        admin = self.login()
        _, _, users_page = self.request("GET", "/admin/users", cookie=admin)
        self.assertIn("class='simple-item'", users_page)
        self.assertIn("创建成员账号", users_page)
        self.assertNotIn("<table>", users_page)
        self.db.register_buyer_site("demo-tenant", "site-x", "https://shop.example.com")
        _, _, sites_page = self.request("GET", "/admin/sites", cookie=admin)
        self.assertIn("class='simple-item'", sites_page)
        self.assertIn("https://shop.example.com", sites_page)
        self.assertIn("登记新的官网来源", sites_page)
        self.assertNotIn("<table>", sites_page)

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
