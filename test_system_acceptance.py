import http.client
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlencode

from app import ROOT, make_server


def request(server, method, path, payload=None, headers=None):
    conn = http.client.HTTPConnection("127.0.0.1", server.server_port)
    request_headers = {"Content-Type": "application/json", **(headers or {})}
    conn.request(method, path, json.dumps(payload).encode() if payload is not None else None, request_headers)
    response = conn.getresponse()
    data = response.read()
    result = response.status, dict(response.getheaders()), json.loads(data) if data else None
    conn.close()
    return result


def form_login(server, email, password):
    conn = http.client.HTTPConnection("127.0.0.1", server.server_port)
    conn.request("POST", "/login", urlencode({"email": email, "password": password}), {"Content-Type": "application/x-www-form-urlencoded"})
    response = conn.getresponse()
    cookie = (response.getheader("Set-Cookie") or "").split(";", 1)[0]
    response.read()
    conn.close()
    return cookie


def html_get(server, path, cookie=None):
    conn = http.client.HTTPConnection("127.0.0.1", server.server_port)
    headers = {"Cookie": cookie} if cookie else {}
    conn.request("GET", path, headers=headers)
    response = conn.getresponse()
    text = response.read().decode("utf-8", errors="replace")
    status = response.status
    conn.close()
    return status, text


def raw_status(server, path, cookie=None):
    conn = http.client.HTTPConnection("127.0.0.1", server.server_port)
    headers = {"Cookie": cookie} if cookie else {}
    conn.request("GET", path, headers=headers)
    response = conn.getresponse()
    status = response.status
    response.read()
    conn.close()
    return status


class SystemAcceptanceTests(unittest.TestCase):
    """横切系统级验收：失败回退、跨企业隔离、版本变化、重启恢复。"""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "app.db"
        self.servers = []

    def tearDown(self):
        while self.servers:
            self.stop_last()
        self.temp_dir.cleanup()

    def start(self):
        server = make_server(self.db_path, port=0)
        db = server.RequestHandlerClass.db
        # 演示企业与演示账号属于测试夹具：显式种入（普通新库不再预置）。
        db.seed_demo_users()
        db.ensure_tenant("second-tenant")
        if not db.get_buyer_site("site-a"):
            db.register_buyer_site("demo-tenant", "site-a", "https://seller-a.example")
        if not db.get_buyer_site("site-b"):
            db.register_buyer_site("second-tenant", "site-b", "https://seller-b.example")
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.servers.append((server, thread))
        return server

    def stop_last(self):
        server, thread = self.servers.pop()
        server.shutdown()
        server.server_close()
        server.RequestHandlerClass.db.conn.close()
        thread.join()

    def test_answer_service_failure_keeps_conversation_usable(self):
        server = self.start()
        _, _, created = request(server, "POST", "/api/v1/buyer/conversations", {"site_key": "site-a"})
        headers = {"X-Site-Key": "site-a", "Authorization": f"Bearer {created['access_token']}"}
        path = f"/api/v1/buyer/conversations/{created['conversation_id']}"
        with patch("app.answer_from_approved_material", side_effect=RuntimeError("model down")):
            status, _, body = request(server, "POST", path + "/questions", {"question": "What is the flow range?"}, headers)
        self.assertEqual(status, 503)
        self.assertEqual(body["error"], "service_unavailable")
        # 失败时不得落虚假答案、不得建任务；问题保留、会话可继续
        _, _, current = request(server, "GET", path, headers=headers)
        self.assertEqual(current["messages"][0]["content"], "What is the flow range?")
        self.assertEqual(current["answers"], [])
        self.assertEqual(current["human_tasks"], [])
        # 服务恢复后可重试
        status, _, body = request(server, "POST", path + "/questions", {"question": "What is the flow range?"}, headers)
        self.assertEqual(status, 201)
        self.assertEqual(body["status"], "needs_human")

    def test_cross_tenant_download_and_admin_records_are_blocked(self):
        server = self.start()
        db = server.RequestHandlerClass.db
        product_id = db.create_product("demo-tenant", "X-TEN", "跨租户泵", "清水")
        contents = b"\x89PNG\r\n\x1a\n" + b"cross-tenant"
        source_id = db.add_source("demo-tenant", product_id, "X-TEN.png", "PNG", "img", content=contents, scope="exact_model")
        self.addCleanup((ROOT / "data" / "uploads" / f"{source_id}-X-TEN.png").unlink, missing_ok=True)
        db.save_field("demo-tenant", product_id, "flow_range", "1-2", "m³/h", source_id, "img")
        db.approve_field("demo-tenant", product_id, "flow_range")
        db.allow_public_source("demo-tenant", product_id, source_id, "demo-admin")
        db.finalize_product_review("demo-tenant", product_id)
        db.publish_product("demo-tenant", product_id)
        # 文件通过另一企业的网站标识不可读取
        self.assertEqual(raw_status(server, f"/s/site-b/products/x-ten/sources/{source_id}"), 404)
        # 另一企业的 RFQ 对当前企业管理员不可见
        second_conv = db.create_conversation("second-tenant")
        second_rfq, _ = db.create_rfq("second-tenant", second_conv, "xten-rfq", {"contact_email": "other@example.invalid", "contact_phone": "+49 30 555 0227", "quantity": "1", "region": "DE"}, "other tenant")
        cookie = form_login(server, "demo-admin@example.invalid", "demo-admin")
        self.assertEqual(html_get(server, f"/admin/rfqs/{second_rfq}", cookie)[0], 404)
        self.assertEqual(html_get(server, f"/admin/sales/{second_rfq}", cookie)[0], 404)
        self.assertEqual(html_get(server, f"/admin/products", cookie)[0], 200)

    def test_rfq_snapshot_survives_product_republish_and_new_answers_cite_latest(self):
        server = self.start()
        db = server.RequestHandlerClass.db
        db.seed_demo_products("demo-tenant")
        product = next(item for item in db.list_products("demo-tenant") if item["model"] == "CB-80A")
        for field in db.get_fields("demo-tenant", product["id"]):
            db.approve_field("demo-tenant", product["id"], field["field_key"])
        db.finalize_product_review("demo-tenant", product["id"])
        db.publish_product("demo-tenant", product["id"])
        v1 = db.get_published_version("demo-tenant", product["id"])
        _, _, created = request(server, "POST", "/api/v1/buyer/conversations", {"site_key": "site-a"})
        headers = {"X-Site-Key": "site-a", "Authorization": f"Bearer {created['access_token']}"}
        path = f"/api/v1/buyer/conversations/{created['conversation_id']}"
        _, _, sent = request(server, "POST", path + "/messages", {"text": "flow 50 m3/h, head 30 m, clean water"}, headers)
        draft = sent["requirement_draft"]
        _, _, confirmed = request(server, "POST", path + "/requirements/confirm", {"base_revision_id": draft["revision_id"], "values": draft["values"]}, headers)
        request(server, "POST", path + "/recommendations", {"revision_id": confirmed["revision_id"]}, headers)
        _, _, rfq = request(server, "POST", path + "/rfqs", {"contact_confirmed": True, "contact": {"contact_email": "buyer@example.com", "contact_phone": "+49 30 555 0228", "quantity": "2", "region": "DE"}, "product_id": product["id"]}, {**headers, "Idempotency-Key": "ver-1"})
        # 更新并重新发布为 v2
        source_id = db.get_sources("demo-tenant", product["id"])[0]["id"]
        db.save_field("demo-tenant", product["id"], "flow_range", "21-81", "m³/h", source_id, "第 1 页：规格参数表（更新）")
        db.approve_field("demo-tenant", product["id"], "flow_range")
        db.finalize_product_review("demo-tenant", product["id"])
        self.assertEqual(db.publish_product("demo-tenant", product["id"]), 2)
        # 旧 RFQ 快照仍指向 v1 的事实与版本
        snapshot = json.loads(db.get_rfq("demo-tenant", rfq["rfq_id"])["snapshot_json"])
        self.assertEqual(snapshot["recommendation"]["product_version_id"], v1["id"])
        # 新问答引用最新版本的核准值
        _, _, answer = request(server, "POST", path + "/questions", {"question": "What is the flow range?", "product_id": product["id"]}, headers)
        self.assertEqual(answer["status"], "grounded")
        self.assertIn("21-81", answer["answer"])
        self.assertEqual(answer["citations"][0]["product_version"], "2")
        # 买方 RFQ 当前值不受产品更新影响
        _, _, current = request(server, "GET", path, headers=headers)
        self.assertEqual(current["rfqs"][0]["id"], rfq["rfq_id"])

    def test_state_survives_server_restart(self):
        server = self.start()
        db = server.RequestHandlerClass.db
        product_id = db.create_product("demo-tenant", "RESTART-1", "重启测试泵", "清水")
        source_id = db.add_source("demo-tenant", product_id, "RESTART-1.txt", "TXT", "第 1 节")
        db.save_field("demo-tenant", product_id, "flow_range", "10-20", "m³/h", source_id, "第 1 节")
        db.approve_field("demo-tenant", product_id, "flow_range")
        db.finalize_product_review("demo-tenant", product_id)
        db.publish_product("demo-tenant", product_id)
        _, _, created = request(server, "POST", "/api/v1/buyer/conversations", {"site_key": "site-a"})
        headers = {"X-Site-Key": "site-a", "Authorization": f"Bearer {created['access_token']}"}
        path = f"/api/v1/buyer/conversations/{created['conversation_id']}"
        request(server, "POST", path + "/messages", {"text": "flow 15 m3/h"}, headers)
        request(server, "POST", path + "/rfqs", {"contact_confirmed": True, "contact": {"contact_email": "buyer@example.com", "contact_phone": "+49 30 555 0229", "quantity": "2", "region": "DE"}}, {**headers, "Idempotency-Key": "restart-1"})
        # 模拟进程重启：关闭整个服务与数据库连接，在同一数据库上重新启动
        self.stop_last()
        server2 = self.start()
        status, _, current = request(server2, "GET", path, headers=headers)
        self.assertEqual(status, 200)
        self.assertEqual(current["messages"][0]["content"], "flow 15 m3/h")
        self.assertEqual(len(current["rfqs"]), 1)
        self.assertEqual(current["rfqs"][0]["status"], "needs_info")
        status, _, product_json = request(server2, "GET", "/api/v1/public/products/restart-1", headers={"X-Site-Key": "site-a"})
        self.assertEqual(status, 200)
        self.assertEqual(product_json["fields"][0]["value"], "10-20")
        cookie = form_login(server2, "demo-admin@example.invalid", "demo-admin")
        status, page = html_get(server2, f"/admin/rfqs/{current['rfqs'][0]['id']}", cookie)
        self.assertEqual(status, 200)
        self.assertIn("buyer@example.com", page)


if __name__ == "__main__":
    unittest.main()
