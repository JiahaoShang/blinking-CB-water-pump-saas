import http.client
import hashlib
import json
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlencode

from app import Database, Handler, ROOT, console_layout, make_server


def approve_all_fields(db, tenant_id, product_id):
    """Simulate an admin who individually approved every sourced candidate field."""
    for field in db.get_fields(tenant_id, product_id):
        if field["state"] == "candidate":
            db.approve_field(tenant_id, product_id, field["field_key"])
    db.finalize_product_review(tenant_id, product_id)


class BuyerApiTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.server = make_server(Path(self.temp_dir.name) / "app.db", port=0)
        self.db = self.server.RequestHandlerClass.db
        # 演示企业与其后台账号属于测试夹具：显式种入（普通新库不再预置）。
        self.db.seed_demo_users()
        self.db.ensure_tenant("second-tenant")
        self.db.register_buyer_site("demo-tenant", "site-a", "https://seller-a.example")
        self.db.register_buyer_site("second-tenant", "site-b", "https://seller-b.example")
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.db.conn.close()
        self.thread.join()
        self.temp_dir.cleanup()

    def request(self, method, path, payload=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.server.server_port)
        request_headers = {"Content-Type": "application/json", **(headers or {})}
        conn.request(method, path, json.dumps(payload).encode() if payload is not None else None, request_headers)
        response = conn.getresponse()
        data = response.read()
        result = response.status, dict(response.getheaders()), json.loads(data) if data else None
        conn.close()
        return result

    def form_request(self, method, path, form=None, cookie=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.server.server_port)
        headers = {"Content-Type": "application/x-www-form-urlencoded"}
        if cookie:
            headers["Cookie"] = cookie
        conn.request(method, path, urlencode(form or {}) if form is not None else None, headers)
        response = conn.getresponse()
        result = response.status, dict(response.getheaders()), response.read().decode()
        conn.close()
        return result

    def html_get(self, path):
        conn = http.client.HTTPConnection("127.0.0.1", self.server.server_port)
        conn.request("GET", path)
        response = conn.getresponse()
        result = response.status, response.read().decode()
        conn.close()
        return result

    def raw_get(self, path):
        conn = http.client.HTTPConnection("127.0.0.1", self.server.server_port)
        conn.request("GET", path)
        response = conn.getresponse()
        result = response.status, dict(response.getheaders()), response.read()
        conn.close()
        return result

    def multipart_upload(self, path, cookie, filename, content, fields=None):
        boundary = "----growave-test-boundary"
        chunks = []
        for key, value in (fields or {}).items():
            chunks.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n{value}\r\n'.encode("utf-8"))
        chunks.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="source_file"; filename="{filename}"\r\nContent-Type: text/plain\r\n\r\n'.encode("utf-8")
            + content + b"\r\n"
        )
        chunks.append(f"--{boundary}--\r\n".encode("utf-8"))
        body = b"".join(chunks)
        conn = http.client.HTTPConnection("127.0.0.1", self.server.server_port)
        conn.request("POST", path, body, {
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            "Cookie": cookie,
            "Content-Length": str(len(body)),
        })
        response = conn.getresponse()
        status = response.status
        response.read()
        conn.close()
        return status

    def test_admin_upload_extract_review_and_publish_flow(self):
        _, admin_headers, _ = self.form_request("POST", "/login", {"email": "demo-admin@example.invalid", "password": "demo-admin"})
        admin_cookie = admin_headers["Set-Cookie"].split(";", 1)[0]
        self.assertEqual(self.form_request("POST", "/admin/products", {"model": "UI-1", "name": "UI 联调泵", "use_case": "联调"}, admin_cookie)[0], 303)
        product = next(item for item in self.db.list_products("demo-tenant") if item["model"] == "UI-1")
        content = "流量范围: 20-80 m³/h\n扬程范围: 18-42 m\n介质: 清水\n".encode("utf-8")
        status = self.multipart_upload(
            f"/admin/products/{product['id']}/sources", admin_cookie, "UI-1-参数.txt", content,
            {"filename": "UI-1-参数.txt", "file_type": "TXT", "scope": "exact_model", "source_location": "第 1 节：参数表"},
        )
        self.assertEqual(status, 303)
        source = self.db.get_sources("demo-tenant", product["id"])[0]
        self.addCleanup(Path(source["storage_path"]).unlink, missing_ok=True)
        self.assertEqual(source["processing_status"], "extracted")
        fields = {field["field_key"]: field for field in self.db.get_fields("demo-tenant", product["id"])}
        self.assertEqual(fields["flow_range"]["value"], "20-80")
        self.assertEqual(fields["flow_range"]["origin"], "extracted")
        # 还有候选未处理时，完成核对被拒绝
        self.assertEqual(self.form_request("POST", f"/admin/products/{product['id']}/finalize-review", {}, admin_cookie)[0], 400)
        # 逐项核准/驳回
        self.assertEqual(self.form_request("POST", f"/admin/products/{product['id']}/fields/flow_range/approve", {}, admin_cookie)[0], 303)
        self.assertEqual(self.form_request("POST", f"/admin/products/{product['id']}/fields/head_range/reject", {"reason": "范围需工程师确认"}, admin_cookie)[0], 303)
        self.assertEqual(self.form_request("POST", f"/admin/products/{product['id']}/fields/media/approve", {}, admin_cookie)[0], 303)
        self.assertEqual(self.form_request("POST", f"/admin/products/{product['id']}/finalize-review", {}, admin_cookie)[0], 303)
        self.assertEqual(self.db.get_product("demo-tenant", product["id"])["status"], "approved")
        self.assertEqual(self.form_request("POST", f"/admin/products/{product['id']}/publish", {}, admin_cookie)[0], 303)
        _, page = self.html_get(f"/products/{product['slug']}")
        self.assertIn("20-80", page)
        self.assertIn("清水", page)
        self.assertNotIn("18-42", page)  # 驳回字段不公开
        _, _, admin_page = self.form_request("GET", f"/admin/products/{product['id']}", cookie=admin_cookie)
        self.assertIn("驳回原因：范围需工程师确认", admin_page)
        self.assertIn("已驳回", admin_page)

    def test_public_image_preview_and_download_require_current_permission(self):
        product_id = self.db.create_product("demo-tenant", "IMG-1", "Pump with approved image", "unreviewed use case")
        contents = b"\x89PNG\r\n\x1a\n" + b"demo-image"
        source_id = self.db.add_source("demo-tenant", product_id, "pump.png", "PNG", "Image 1", content=contents, scope="exact_model")
        upload = ROOT / "data" / "uploads" / f"{source_id}-pump.png"
        self.addCleanup(upload.unlink, missing_ok=True)
        self.assertTrue(upload.is_file())
        self.db.save_field("demo-tenant", product_id, "flow_range", "10-30", "m³/h", source_id, "Image 1")
        approve_all_fields(self.db, "demo-tenant", product_id)
        self.db.publish_product("demo-tenant", product_id)
        path = f"/s/site-a/products/img-1/sources/{source_id}"
        self.assertEqual(self.raw_get(path + "?preview=1")[0], 404)
        self.db.allow_public_source("demo-tenant", product_id, source_id, "demo-admin")
        # 发布版本冻结文件清单：刚开放的文件还没有进入 v1 买方页
        self.assertNotIn(path + "?preview=1", self.html_get("/s/site-a/products/img-1")[1])
        self.db.publish_product("demo-tenant", product_id)
        page = self.html_get("/s/site-a/products/img-1")[1]
        self.assertIn(path + "?preview=1", page)
        status, headers, body = self.raw_get(path + "?preview=1")
        self.assertEqual((status, headers["Content-Type"], body), (200, "image/png", contents))
        self.assertTrue(headers["Content-Disposition"].startswith("inline;"))
        self.assertTrue(self.raw_get(path)[1]["Content-Disposition"].startswith("attachment;"))
        self.db.revoke_public_source("demo-tenant", product_id, source_id, "demo-admin")
        self.assertEqual(self.raw_get(path + "?preview=1")[0], 404)

    def test_site_product_page_is_tenant_scoped_and_has_api_rfq_entry(self):
        self.db.seed_demo_products("demo-tenant")
        product = self.db.list_products("demo-tenant")[0]
        approve_all_fields(self.db, "demo-tenant", product["id"])
        self.db.publish_product("demo-tenant", product["id"])
        second = next(item for item in self.db.list_products("demo-tenant") if item["id"] != product["id"])
        approve_all_fields(self.db, "demo-tenant", second["id"])
        self.db.publish_product("demo-tenant", second["id"])
        page_path = f"/s/site-a/products/{product['slug']}"
        status, page = self.html_get(page_path)
        self.assertEqual(status, 200)
        self.assertIn("id='product-rfq'", page)
        self.assertIn("data-site-key='site-a'", page)
        self.assertIn("data-product-id='" + product["id"] + "'", page)
        self.assertNotIn("href='/admin'", page)
        self.assertNotIn("mailto:sales@example.invalid", page)
        self.assertIn("product-page.css", page)
        sections = ["hero", "advantages", "specifications", "drawings", "comparison", "applications", "customization", "manufacturing", "certification", "downloads", "faq"]
        self.assertEqual([page.index(f"data-section='{name}'") for name in sections], sorted(page.index(f"data-section='{name}'") for name in sections))
        self.assertIn(second["model"], page)
        self.assertIn("MOQ, lead time and sample availability", page)
        self.assertNotIn(product["use_case"], page)
        self.assertEqual(self.html_get(f"/s/site-b/products/{product['slug']}")[0], 404)
        self.assertEqual(self.html_get("/reference-client/product-page.js")[0], 200)
        self.assertEqual(self.html_get("/reference-client/product-page.css")[0], 200)

    def test_site_scoped_conversation_requires_private_token(self):
        status, _, created = self.request("POST", "/api/v1/buyer/conversations", {"site_key": "site-a", "locale": "en"})
        self.assertEqual(status, 201)
        conversation_id = created["conversation_id"]
        token = created["access_token"]
        self.assertTrue(token)
        self.assertEqual(self.db.get_conversation("demo-tenant", conversation_id)["locale"], "en")
        self.assertIsNone(self.db.get_conversation("second-tenant", conversation_id))

        path = f"/api/v1/buyer/conversations/{conversation_id}"
        self.assertEqual(self.request("GET", path, headers={"X-Site-Key": "site-a"})[0], 401)
        self.assertEqual(self.request("GET", path, headers={"X-Site-Key": "site-b", "Authorization": f"Bearer {token}"})[0], 404)
        status, _, body = self.request("GET", path, headers={"X-Site-Key": "site-a", "Authorization": f"Bearer {token}"})
        self.assertEqual(status, 200)
        self.assertEqual(body["messages"], [])

        headers = {"X-Site-Key": "site-a", "Authorization": f"Bearer {token}"}
        status, _, saved = self.request("POST", path + "/messages", {"text": "Flow 3 m3/h, head 45 m"}, headers)
        self.assertEqual(status, 201)
        self.assertTrue(saved["message_id"])
        status, _, body = self.request("GET", path, headers=headers)
        self.assertEqual(body["messages"][0]["content"], "Flow 3 m3/h, head 45 m")

    def test_admin_registers_site_in_ui_and_sales_cannot_change_it(self):
        status, headers, _ = self.form_request("POST", "/login", {"email": "demo-admin@example.invalid", "password": "demo-admin"})
        self.assertEqual(status, 303)
        admin_cookie = headers["Set-Cookie"].split(";", 1)[0]
        self.assertEqual(self.form_request("GET", "/admin/sites", cookie=admin_cookie)[0], 200)
        status, _, _ = self.form_request("POST", "/admin/sites", {"allowed_origin": "https://factory.example"}, admin_cookie)
        self.assertEqual(status, 303)
        _, _, page = self.form_request("GET", "/admin/sites", cookie=admin_cookie)
        self.assertIn("https://factory.example", page)
        self.assertEqual(len(self.db.list_buyer_sites("demo-tenant")), 2)
        new_site = next(site for site in self.db.list_buyer_sites("demo-tenant") if site["allowed_origin"] == "https://factory.example")
        self.assertEqual(self.request("POST", "/api/v1/buyer/conversations", {"site_key": new_site["site_key"]}, {"Origin": "https://factory.example"})[0], 201)

        _, headers, _ = self.form_request("POST", "/login", {"email": "demo-sales@example.invalid", "password": "demo-sales"})
        sales_cookie = headers["Set-Cookie"].split(";", 1)[0]
        self.assertEqual(self.form_request("POST", "/admin/sites", {"allowed_origin": "https://wrong.example"}, sales_cookie)[0], 403)
        self.assertEqual(len(self.db.list_buyer_sites("demo-tenant")), 2)

    def test_registered_origin_is_required_for_browser_calls(self):
        status, _, _ = self.request(
            "POST", "/api/v1/buyer/conversations", {"site_key": "site-a"},
            {"Origin": "https://unrelated.example"},
        )
        self.assertEqual(status, 403)
        status, headers, _ = self.request(
            "POST", "/api/v1/buyer/conversations", {"site_key": "site-a"},
            {"Origin": "https://seller-a.example"},
        )
        self.assertEqual(status, 201)
        self.assertEqual(headers["Access-Control-Allow-Origin"], "https://seller-a.example")
        status, headers, _ = self.request(
            "OPTIONS", "/api/v1/buyer/conversations", headers={
                "Origin": "https://seller-a.example",
                "Access-Control-Request-Method": "POST",
            },
        )
        self.assertEqual(status, 204)
        self.assertIn("Authorization", headers["Access-Control-Allow-Headers"])

    def test_message_draft_confirmation_and_correction_invalidate_old_match(self):
        _, _, created = self.request("POST", "/api/v1/buyer/conversations", {"site_key": "site-a"})
        path = f"/api/v1/buyer/conversations/{created['conversation_id']}"
        headers = {"X-Site-Key": "site-a", "Authorization": f"Bearer {created['access_token']}"}
        status, _, sent = self.request("POST", path + "/messages", {"text": "Need pump flow 3 m3/h, head 45 m, clean water"}, headers)
        self.assertEqual(status, 201)
        draft = sent["requirement_draft"]
        self.assertEqual(draft["values"]["flow"]["value"], "3")
        self.assertEqual(draft["values"]["head"]["state"], "candidate")

        values = draft["values"]
        values["head"]["value"] = "48"
        status, _, confirmed = self.request("POST", path + "/requirements/confirm", {"base_revision_id": draft["revision_id"], "values": values}, headers)
        self.assertEqual(status, 201)
        self.assertEqual(confirmed["values"]["head"]["state"], "confirmed")
        self.db.save_recommendation("demo-tenant", created["conversation_id"], confirmed["revision_id"], "recommended", [])
        self.assertIsNotNone(self.db.latest_active_recommendation("demo-tenant", created["conversation_id"]))

        status, _, changed = self.request("POST", path + "/messages", {"text": "Actually head 60 m"}, headers)
        self.assertEqual(status, 201)
        self.assertIsNone(self.db.latest_active_recommendation("demo-tenant", created["conversation_id"]))
        self.assertEqual(changed["requirement_draft"]["values"]["head"]["value"], "60")

    def test_recommendation_only_uses_confirmed_revision_and_published_tenant_products(self):
        self.db.seed_demo_products("demo-tenant")
        for product in self.db.list_products("demo-tenant"):
            approve_all_fields(self.db, "demo-tenant", product["id"])
            self.db.publish_product("demo-tenant", product["id"])
        _, _, created = self.request("POST", "/api/v1/buyer/conversations", {"site_key": "site-a"})
        path = f"/api/v1/buyer/conversations/{created['conversation_id']}"
        headers = {"X-Site-Key": "site-a", "Authorization": f"Bearer {created['access_token']}"}
        _, _, sent = self.request("POST", path + "/messages", {"text": "Need pump flow 50 m3/h, head 30 m, clean water"}, headers)
        draft = sent["requirement_draft"]
        self.assertEqual(self.request("POST", path + "/recommendations", {"revision_id": draft["revision_id"]}, headers)[0], 409)
        _, _, confirmed = self.request("POST", path + "/requirements/confirm", {"base_revision_id": draft["revision_id"], "values": draft["values"]}, headers)
        status, _, result = self.request("POST", path + "/recommendations", {"revision_id": confirmed["revision_id"]}, headers)
        self.assertEqual(status, 201)
        self.assertEqual(result["status"], "recommended")
        self.assertEqual(result["results"][0]["model"], "CB-80A")
        self.assertTrue(result["results"][0]["detail_url"].startswith("/s/site-a/products/"))
        _, _, current = self.request("GET", path, headers=headers)
        self.assertEqual(current["requirement"]["status"], "confirmed")
        self.assertEqual(current["recommendation"]["recommendation_id"], result["recommendation_id"])

    def test_grounded_answer_and_explicit_engineer_request_share_conversation(self):
        self.db.seed_demo_products("demo-tenant")
        product = self.db.list_products("demo-tenant")[0]
        approve_all_fields(self.db, "demo-tenant", product["id"])
        self.db.publish_product("demo-tenant", product["id"])
        _, _, created = self.request("POST", "/api/v1/buyer/conversations", {"site_key": "site-a"})
        path = f"/api/v1/buyer/conversations/{created['conversation_id']}"
        headers = {"X-Site-Key": "site-a", "Authorization": f"Bearer {created['access_token']}"}
        status, _, grounded = self.request("POST", path + "/questions", {"question": "What is the flow range?", "product_id": product["id"]}, headers)
        self.assertEqual(status, 201)
        self.assertEqual(grounded["status"], "grounded")
        self.assertTrue(grounded["citations"])
        status, _, unresolved = self.request("POST", path + "/questions", {"question": "What is the price?", "product_id": product["id"]}, headers)
        self.assertEqual(status, 201)
        self.assertEqual(unresolved["status"], "needs_human")
        self.assertEqual(len(self.db.list_human_tasks("demo-tenant")), 0)

        body = {"answer_id": unresolved["answer_id"], "reply_path": "conversation"}
        self.assertEqual(self.request("POST", path + "/human-requests", body, headers)[0], 201)
        self.assertEqual(self.request("POST", path + "/human-requests", body, headers)[0], 200)
        self.assertEqual(len(self.db.list_human_tasks("demo-tenant")), 1)
        _, _, current = self.request("GET", path, headers=headers)
        self.assertEqual(current["human_tasks"][0]["status"], "pending")
        self.assertFalse(any(message["source"] == "human-engineer" for message in current["messages"]))

        status, login_headers, _ = self.form_request("POST", "/login", {"email": "demo-engineer@example.invalid", "password": "demo-engineer"})
        self.assertEqual(status, 303)
        engineer_cookie = login_headers["Set-Cookie"].split(";", 1)[0]
        task_id = current["human_tasks"][0]["id"]
        self.assertEqual(self.form_request("POST", f"/admin/tasks/{task_id}/reply", {"response": "Please confirm the requested quantity before a price is issued."}, engineer_cookie)[0], 303)
        _, _, current = self.request("GET", path, headers=headers)
        self.assertEqual(current["human_tasks"][0]["status"], "resolved")
        self.assertEqual(current["messages"][-1]["source"], "human-engineer")
        self.assertIn("requested quantity", current["messages"][-1]["content"])

    def test_engineer_followup_and_buyer_reply_keep_original_task(self):
        _, _, created = self.request("POST", "/api/v1/buyer/conversations", {"site_key": "site-a"})
        path = f"/api/v1/buyer/conversations/{created['conversation_id']}"
        headers = {"X-Site-Key": "site-a", "Authorization": f"Bearer {created['access_token']}"}
        _, _, unresolved = self.request("POST", path + "/questions", {"question": "Can this handle my application?"}, headers)
        _, _, requested = self.request("POST", path + "/human-requests", {"answer_id": unresolved["answer_id"], "reply_path": "conversation"}, headers)
        task_id = requested["task_id"]
        _, login_headers, _ = self.form_request("POST", "/login", {"email": "demo-engineer@example.invalid", "password": "demo-engineer"})
        cookie = login_headers["Set-Cookie"].split(";", 1)[0]
        self.assertEqual(self.form_request("POST", f"/admin/tasks/{task_id}/followup", {"question": "What is the medium temperature?"}, cookie)[0], 303)
        _, _, current = self.request("GET", path, headers=headers)
        self.assertEqual(current["human_tasks"][0]["status"], "waiting_buyer")
        self.assertEqual(current["human_tasks"][0]["followup_question"], "What is the medium temperature?")
        self.assertEqual(self.request("POST", path + f"/human-tasks/{task_id}/responses", {"text": "40 °C"}, headers)[0], 201)
        _, _, current = self.request("GET", path, headers=headers)
        self.assertEqual(current["human_tasks"][0]["status"], "pending")
        self.assertEqual(current["messages"][-1]["source"], f"buyer-followup:{task_id}")
        self.assertEqual(len(self.db.list_human_tasks("demo-tenant")), 1)

    def test_rfq_from_api_requires_confirmed_contact_and_rejects_changed_retry(self):
        _, _, created = self.request("POST", "/api/v1/buyer/conversations", {"site_key": "site-a"})
        path = f"/api/v1/buyer/conversations/{created['conversation_id']}"
        headers = {"X-Site-Key": "site-a", "Authorization": f"Bearer {created['access_token']}", "Idempotency-Key": "rfq-1"}
        payload = {"entry_surface": "chat", "contact": {"contact_email": "buyer@example.com", "contact_phone": "+49 30 555 0212", "quantity": "2", "region": "Germany"}, "notes": "Need price", "contact_confirmed": True}
        self.assertEqual(self.request("POST", path + "/rfqs", {**payload, "contact_confirmed": False}, headers)[0], 422)
        # V1-01：默认口径缺必填 → 422 且回传缺项清单（不落记录、不占用幂等键）
        status_missing, _, missing_body = self.request("POST", path + "/rfqs", {**payload, "contact": {"contact_email": "buyer@example.com"}}, headers)
        self.assertEqual(status_missing, 422)
        self.assertEqual(missing_body["error"], "rfq_required_fields_missing")
        self.assertIn("电话", missing_body["message"])
        self.assertEqual(len(self.db.list_rfqs("demo-tenant")), 0)
        status, _, first = self.request("POST", path + "/rfqs", payload, headers)
        self.assertEqual(status, 201)
        self.assertEqual(first["status"], "needs_info")
        status, _, retry = self.request("POST", path + "/rfqs", payload, headers)
        self.assertEqual(status, 200)
        self.assertEqual(retry["rfq_id"], first["rfq_id"])
        self.assertEqual(self.request("POST", path + "/rfqs", {**payload, "notes": "Different order"}, headers)[0], 409)
        self.assertEqual(len(self.db.list_rfqs("demo-tenant")), 1)
        _, _, current = self.request("GET", path, headers=headers)
        self.assertEqual(current["rfqs"][0]["id"], first["rfq_id"])

    def test_sales_claim_and_admin_assignment_have_separate_permissions(self):
        _, _, created = self.request("POST", "/api/v1/buyer/conversations", {"site_key": "site-a"})
        path = f"/api/v1/buyer/conversations/{created['conversation_id']}"
        headers = {"X-Site-Key": "site-a", "Authorization": f"Bearer {created['access_token']}", "Idempotency-Key": "sales-claim-rfq"}
        _, _, submitted = self.request("POST", path + "/rfqs", {"contact_confirmed": True, "contact": {"contact_email": "buyer@example.test", "contact_phone": "+49 30 555 0213", "quantity": "2", "region": "DE"}}, headers)
        rfq_id = submitted["rfq_id"]
        _, sales_headers, _ = self.form_request("POST", "/login", {"email": "demo-sales@example.invalid", "password": "demo-sales"})
        sales_cookie = sales_headers["Set-Cookie"].split(";", 1)[0]
        _, _, sales_page = self.form_request("GET", f"/admin/sales/{rfq_id}", cookie=sales_cookie)
        self.assertIn("领取这条会话", sales_page)
        self.assertNotIn("分配或改派", sales_page)
        self.assertEqual(self.form_request("POST", f"/admin/sales/{rfq_id}/claim", {}, sales_cookie)[0], 303)
        self.assertEqual(self.db.get_lead_for_rfq("demo-tenant", rfq_id)["owner"], "demo-sales@example.invalid")
        # V1-01：非管理员分配请求在处理器入口即被权限拦截（403 权限拦截页），不再走数据库层 ValueError(400)
        self.assertEqual(self.form_request("POST", f"/admin/sales/{rfq_id}/assign", {"owner_email": "demo-sales@example.invalid"}, sales_cookie)[0], 403)
        _, admin_headers, _ = self.form_request("POST", "/login", {"email": "demo-admin@example.invalid", "password": "demo-admin"})
        admin_cookie = admin_headers["Set-Cookie"].split(";", 1)[0]
        _, _, admin_page = self.form_request("GET", f"/admin/sales/{rfq_id}", cookie=admin_cookie)
        self.assertIn("分配或改派", admin_page)
        self.assertEqual(self.form_request("POST", f"/admin/sales/{rfq_id}/assign", {"owner_email": "demo-sales@example.invalid"}, admin_cookie)[0], 303)

    def test_buyer_stop_contact_is_visible_to_sales_and_blocks_followup_date(self):
        _, _, created = self.request("POST", "/api/v1/buyer/conversations", {"site_key": "site-a"})
        path = f"/api/v1/buyer/conversations/{created['conversation_id']}"
        headers = {"X-Site-Key": "site-a", "Authorization": f"Bearer {created['access_token']}"}
        _, _, submitted = self.request("POST", path + "/rfqs", {"contact_confirmed": True, "contact": {"contact_email": "buyer@example.test", "contact_phone": "+49 30 555 0214", "quantity": "2", "region": "DE"}}, {**headers, "Idempotency-Key": "stop-rfq"})
        rfq_id = submitted["rfq_id"]
        _, sales_headers, _ = self.form_request("POST", "/login", {"email": "demo-sales@example.invalid", "password": "demo-sales"})
        cookie = sales_headers["Set-Cookie"].split(";", 1)[0]
        self.assertEqual(self.form_request("POST", f"/admin/sales/{rfq_id}/update", {"stage": "technical_review", "next_follow_up": "2026-10-20"}, cookie)[0], 303)
        stop = {"stop_proactive_contact": True, "reason": "Please stop contacting me about this inquiry."}
        self.assertEqual(self.request("POST", path + "/contact-preference", stop, headers)[0], 201)
        self.assertEqual(self.request("POST", path + "/contact-preference", stop, headers)[0], 200)
        _, _, current = self.request("GET", path, headers=headers)
        self.assertTrue(current["contact_preference"]["stop_proactive_contact"])
        self.assertEqual(sum(message["source"] == "contact-stop" for message in current["messages"]), 1)
        self.assertIsNone(self.db.get_lead_for_rfq("demo-tenant", rfq_id)["next_follow_up"])
        _, _, seller_page = self.form_request("GET", f"/admin/sales/{rfq_id}", cookie=cookie)
        self.assertIn("停止主动联系", seller_page)
        # V1-09：冲突改为友好提示（303 + notice=stop_conflict），跟进日期照旧不被写入
        stop_status, stop_headers, _ = self.form_request("POST", f"/admin/sales/{rfq_id}/update", {"stage": "technical_review", "next_follow_up": "2026-10-21"}, cookie)
        self.assertEqual(stop_status, 303)
        self.assertIn("notice=stop_conflict", stop_headers["Location"])
        self.assertIsNone(self.db.get_lead_for_rfq("demo-tenant", rfq_id)["next_follow_up"])

    def test_recommendation_withholds_tentative_until_requested(self):
        self.db.seed_demo_products("demo-tenant")
        for product in self.db.list_products("demo-tenant"):
            approve_all_fields(self.db, "demo-tenant", product["id"])
            self.db.publish_product("demo-tenant", product["id"])
        _, _, created = self.request("POST", "/api/v1/buyer/conversations", {"site_key": "site-a"})
        path = f"/api/v1/buyer/conversations/{created['conversation_id']}"
        headers = {"X-Site-Key": "site-a", "Authorization": f"Bearer {created['access_token']}"}
        _, _, sent = self.request("POST", path + "/messages", {"text": "Need pump flow 50 m3/h, head 30 m, clean water, temperature 100"}, headers)
        draft = sent["requirement_draft"]
        _, _, confirmed = self.request("POST", path + "/requirements/confirm", {"base_revision_id": draft["revision_id"], "values": draft["values"]}, headers)
        status, _, first = self.request("POST", path + "/recommendations", {"revision_id": confirmed["revision_id"]}, headers)
        self.assertEqual(status, 201)
        self.assertEqual(first["status"], "needs_confirmation")
        self.assertEqual(first["results"], [])  # 待核实候选默认不下发
        self.assertTrue(first["tentative_available"])
        self.assertTrue(first["options"]["engineer_help"])
        # 买方明确选择查看后才返回待核实候选，并逐款标注未确认条件
        status, _, explore = self.request("POST", path + "/recommendations", {"revision_id": confirmed["revision_id"], "include_tentative": True}, headers)
        self.assertEqual(status, 200)
        models = [item["model"] for item in explore["results"]]
        self.assertEqual(models, ["CB-80A"])
        self.assertTrue(explore["results"][0]["detail_url"].startswith("/s/site-a/products/"))
        self.assertIn("温度", "".join(explore["results"][0]["unknown"]))
        self.assertNotIn("CB-120C", models)  # 已知硬冲突不进入候选
        # 买方已明确选择查看：刷新或重进会话后保持待核实视图（选择被记住）
        _, _, current = self.request("GET", path, headers=headers)
        self.assertEqual([item["model"] for item in current["recommendation"]["results"]], ["CB-80A"])
        self.assertTrue(current["recommendation"]["tentative_available"])
        # 新一轮确认后的新推荐不沿用旧选择：默认仍只下发可初步推荐层，需再次主动查看
        adjusted = dict(confirmed["values"])
        adjusted["quantity"] = {"value": "3", "unit": ""}
        _, _, reconfirmed = self.request("POST", path + "/requirements/confirm", {"base_revision_id": confirmed["revision_id"], "values": adjusted}, headers)
        _, _, fresh = self.request("POST", path + "/recommendations", {"revision_id": reconfirmed["revision_id"]}, headers)
        self.assertEqual(fresh["status"], "needs_confirmation")
        self.assertEqual(fresh["results"], [])
        self.assertTrue(fresh["tentative_available"])

    def test_recommendation_insufficient_offers_options_and_explore_lists_missing(self):
        self.db.seed_demo_products("demo-tenant")
        for product in self.db.list_products("demo-tenant"):
            approve_all_fields(self.db, "demo-tenant", product["id"])
            self.db.publish_product("demo-tenant", product["id"])
        _, _, created = self.request("POST", "/api/v1/buyer/conversations", {"site_key": "site-a"})
        path = f"/api/v1/buyer/conversations/{created['conversation_id']}"
        headers = {"X-Site-Key": "site-a", "Authorization": f"Bearer {created['access_token']}"}
        _, _, sent = self.request("POST", path + "/messages", {"text": "Need pump flow 50 m3/h"}, headers)
        draft = sent["requirement_draft"]
        self.assertIsNotNone(draft["next_question"])
        _, _, confirmed = self.request("POST", path + "/requirements/confirm", {"base_revision_id": draft["revision_id"], "values": draft["values"]}, headers)
        _, _, first = self.request("POST", path + "/recommendations", {"revision_id": confirmed["revision_id"]}, headers)
        self.assertEqual(first["status"], "insufficient")
        self.assertEqual(first["results"], [])
        self.assertIn("扬程", first["missing"])
        self.assertIn("介质", first["missing"])
        self.assertTrue(first["options"]["engineer_help"])
        self.assertTrue(first["options"]["tentative_available"])
        _, _, explore = self.request("POST", path + "/recommendations", {"revision_id": confirmed["revision_id"], "include_tentative": True}, headers)
        self.assertEqual(explore["status"], "insufficient")
        self.assertEqual(sorted(item["model"] for item in explore["results"]), ["CB-120C", "CB-80A"])
        self.assertTrue(all(item["status"] == "needs_confirmation" for item in explore["results"]))
        unknown_text = "".join("".join(item["unknown"]) for item in explore["results"])
        self.assertIn("买方尚未确认扬程", unknown_text)
        # 已知硬冲突导致无可靠候选：不再提供待核实列表
        changed = dict(confirmed["values"])
        changed["head"] = {"value": "300", "unit": "m", "label": "扬程"}
        changed["media"] = {"value": "清水", "unit": "", "label": "介质"}
        _, _, re_confirmed = self.request("POST", path + "/requirements/confirm", {"base_revision_id": confirmed["revision_id"], "values": changed}, headers)
        _, _, no_match = self.request("POST", path + "/recommendations", {"revision_id": re_confirmed["revision_id"]}, headers)
        self.assertEqual(no_match["status"], "no_match")
        self.assertEqual(no_match["results"], [])
        self.assertFalse(no_match["tentative_available"])
        self.assertTrue(no_match["options"]["engineer_help"])

    def test_confirm_idempotency_dedupes_retry_and_rejects_changed_retry(self):
        _, _, created = self.request("POST", "/api/v1/buyer/conversations", {"site_key": "site-a"})
        path = f"/api/v1/buyer/conversations/{created['conversation_id']}"
        headers = {"X-Site-Key": "site-a", "Authorization": f"Bearer {created['access_token']}"}
        _, _, sent = self.request("POST", path + "/messages", {"text": "flow 50 m3/h, head 30 m"}, headers)
        draft = sent["requirement_draft"]
        keyed = {**headers, "Idempotency-Key": "confirm-key-1"}
        payload = {"base_revision_id": draft["revision_id"], "values": draft["values"]}
        status, _, first = self.request("POST", path + "/requirements/confirm", payload, keyed)
        self.assertEqual(status, 201)
        # 同键同内容重试（双击/断网重发）：返回原始修订，不新建
        status, _, retry = self.request("POST", path + "/requirements/confirm", payload, keyed)
        self.assertEqual(status, 200)
        self.assertEqual(retry["revision_id"], first["revision_id"])
        self.assertTrue(retry["duplicate"])
        revisions = self.db.get_revisions("demo-tenant", created["conversation_id"])
        self.assertEqual(len([row for row in revisions if row["status"] == "confirmed"]), 1)
        # 成功后又产生新修订时，旧键重试仍返回原始结果：不报“基准过期”、不重复确认
        _, _, sent2 = self.request("POST", path + "/messages", {"text": "quantity 2"}, headers)
        status, _, late_retry = self.request("POST", path + "/requirements/confirm", payload, keyed)
        self.assertEqual(status, 200)
        self.assertEqual(late_retry["revision_id"], first["revision_id"])
        # 同键改内容：409，不静默覆盖也不假装成功
        changed = json.loads(json.dumps(payload))
        changed["values"]["quantity"] = {"value": "9", "unit": ""}
        self.assertEqual(self.request("POST", path + "/requirements/confirm", changed, keyed)[0], 409)
        # 新键代表真实的新动作：允许再次确认并为新修订
        draft2 = sent2["requirement_draft"]
        status, _, again = self.request("POST", path + "/requirements/confirm", {"base_revision_id": draft2["revision_id"], "values": draft2["values"]}, {**headers, "Idempotency-Key": "confirm-key-2"})
        self.assertEqual(status, 201)
        self.assertNotEqual(again["revision_id"], first["revision_id"])
        revisions = self.db.get_revisions("demo-tenant", created["conversation_id"])
        self.assertEqual(len([row for row in revisions if row["status"] == "confirmed"]), 2)

    def test_question_idempotency_dedupes_retry_and_rejects_changed_retry(self):
        _, _, created = self.request("POST", "/api/v1/buyer/conversations", {"site_key": "site-a"})
        path = f"/api/v1/buyer/conversations/{created['conversation_id']}"
        headers = {"X-Site-Key": "site-a", "Authorization": f"Bearer {created['access_token']}"}
        keyed = {**headers, "Idempotency-Key": "question-key-1"}
        payload = {"question": "Can you confirm the price?"}
        status, _, first = self.request("POST", path + "/questions", payload, keyed)
        self.assertEqual(status, 201)
        # 同键同内容重试：返回原始答案，不重复提问、不重复保存答案
        status, _, retry = self.request("POST", path + "/questions", payload, keyed)
        self.assertEqual(status, 200)
        self.assertEqual(retry["answer_id"], first["answer_id"])
        self.assertTrue(retry["duplicate"])
        self.assertEqual(len(self.db.list_answers("demo-tenant", created["conversation_id"])), 1)
        question_messages = [row for row in self.db.list_messages("demo-tenant", created["conversation_id"]) if row["content"] == "Can you confirm the price?"]
        self.assertEqual(len(question_messages), 1)
        # 同键改内容：409
        self.assertEqual(self.request("POST", path + "/questions", {"question": "What is the lead time?"}, keyed)[0], 409)
        # 新键同内容（买方真实再次提问）：允许新记录
        status, _, second = self.request("POST", path + "/questions", payload, {**headers, "Idempotency-Key": "question-key-2"})
        self.assertEqual(status, 201)
        self.assertNotEqual(second["answer_id"], first["answer_id"])

    def test_recommendation_idempotency_dedupes_double_click(self):
        self.db.seed_demo_products("demo-tenant")
        for product in self.db.list_products("demo-tenant"):
            approve_all_fields(self.db, "demo-tenant", product["id"])
            self.db.publish_product("demo-tenant", product["id"])
        _, _, created = self.request("POST", "/api/v1/buyer/conversations", {"site_key": "site-a"})
        path = f"/api/v1/buyer/conversations/{created['conversation_id']}"
        headers = {"X-Site-Key": "site-a", "Authorization": f"Bearer {created['access_token']}"}
        _, _, sent = self.request("POST", path + "/messages", {"text": "Need pump flow 50 m3/h, head 30 m, clean water"}, headers)
        draft = sent["requirement_draft"]
        _, _, confirmed = self.request("POST", path + "/requirements/confirm", {"base_revision_id": draft["revision_id"], "values": draft["values"]}, headers)
        keyed = {**headers, "Idempotency-Key": "find-key-1"}
        payload = {"revision_id": confirmed["revision_id"]}
        status, _, first = self.request("POST", path + "/recommendations", payload, keyed)
        self.assertEqual(status, 201)
        status, _, retry = self.request("POST", path + "/recommendations", payload, keyed)
        self.assertEqual(status, 200)
        self.assertEqual(retry["recommendation_id"], first["recommendation_id"])
        self.assertTrue(retry["duplicate"])
        self.assertEqual(len(self.db.get_recommendations("demo-tenant", created["conversation_id"])), 1)
        # 并发双击（同键两路并发）：唯一索引兜底，仍只有一条推荐记录、两个请求拿到同一结果
        _, _, created2 = self.request("POST", "/api/v1/buyer/conversations", {"site_key": "site-a"})
        path2 = f"/api/v1/buyer/conversations/{created2['conversation_id']}"
        headers2 = {"X-Site-Key": "site-a", "Authorization": f"Bearer {created2['access_token']}"}
        _, _, sent2 = self.request("POST", path2 + "/messages", {"text": "Need pump flow 50 m3/h, head 30 m, clean water"}, headers2)
        draft2 = sent2["requirement_draft"]
        _, _, confirmed2 = self.request("POST", path2 + "/requirements/confirm", {"base_revision_id": draft2["revision_id"], "values": draft2["values"]}, headers2)
        concurrent_headers = {**headers2, "Idempotency-Key": "find-race-1"}
        payload2 = {"revision_id": confirmed2["revision_id"]}
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: self.request("POST", path2 + "/recommendations", payload2, concurrent_headers), range(2)))
        self.assertEqual(sorted(status for status, _, _ in results), [200, 201])
        self.assertEqual(len({body["recommendation_id"] for _, _, body in results}), 1)
        self.assertEqual(len(self.db.get_recommendations("demo-tenant", created2["conversation_id"])), 1)

    def test_public_product_json_serves_only_published_approved_content(self):
        product_id = self.db.create_product("demo-tenant", "PUB-JSON", "公开 JSON 泵", "联调")
        contents = b"\x89PNG\r\n\x1a\n" + b"public-json-image"
        source_id = self.db.add_source("demo-tenant", product_id, "PUB-JSON.png", "PNG", "Image 1", content=contents, scope="exact_model")
        self.addCleanup((ROOT / "data" / "uploads" / f"{source_id}-PUB-JSON.png").unlink, missing_ok=True)
        self.db.save_field("demo-tenant", product_id, "flow_range", "12-34", "m³/h", source_id, "Image 1")
        approve_all_fields(self.db, "demo-tenant", product_id)
        self.db.allow_public_source("demo-tenant", product_id, source_id, "demo-admin")
        self.db.publish_product("demo-tenant", product_id)
        status, _, body = self.request("GET", "/api/v1/public/products/pub-json", headers={"X-Site-Key": "site-a"})
        self.assertEqual(status, 200)
        self.assertEqual(body["product"]["model"], "PUB-JSON")
        self.assertEqual(body["page_version"], 1)
        self.assertEqual([field["field_key"] for field in body["fields"]], ["flow_range"])
        self.assertEqual(body["fields"][0]["value"], "12-34")
        self.assertEqual(len(body["public_sources"]), 1)
        self.assertTrue(body["public_sources"][0]["download_url"].startswith("/s/site-a/products/pub-json/sources/"))
        self.assertTrue(body["public_sources"][0]["preview_url"].endswith("?preview=1"))
        serialized = json.dumps(body)
        self.assertNotIn("approved_by", serialized)
        self.assertNotIn("tenant_id", serialized)
        # 跨企业网站标识不可见
        self.assertEqual(self.request("GET", "/api/v1/public/products/pub-json", headers={"X-Site-Key": "site-b"})[0], 404)
        # 撤销文件许可后 JSON 中不再出现该文件
        self.db.revoke_public_source("demo-tenant", product_id, source_id, "demo-admin")
        _, _, after_revoke = self.request("GET", "/api/v1/public/products/pub-json", headers={"X-Site-Key": "site-a"})
        self.assertEqual(after_revoke["public_sources"], [])
        # 撤回产品后不可读取
        self.db.withdraw_product("demo-tenant", product_id, "demo-admin")
        self.assertEqual(self.request("GET", "/api/v1/public/products/pub-json", headers={"X-Site-Key": "site-a"})[0], 404)

    def test_message_idempotency_dedupes_retries_and_rejects_changed_retry(self):
        _, _, created = self.request("POST", "/api/v1/buyer/conversations", {"site_key": "site-a"})
        conversation_id = created["conversation_id"]
        path = f"/api/v1/buyer/conversations/{conversation_id}"
        headers = {"X-Site-Key": "site-a", "Authorization": f"Bearer {created['access_token']}", "Idempotency-Key": "msg-1"}
        status, _, first = self.request("POST", path + "/messages", {"text": "flow 3 m3/h, head 45 m"}, headers)
        self.assertEqual(status, 201)
        self.assertEqual(first["status"], "saved")
        message_id = first["message_id"]
        self.assertEqual(len(self.db.list_messages("demo-tenant", conversation_id)), 1)
        self.assertEqual(len(self.db.get_revisions("demo-tenant", conversation_id)), 1)
        # 同键同内容重试：返回原消息与原始草稿，不新增记录
        status, _, retry = self.request("POST", path + "/messages", {"text": "flow 3 m3/h, head 45 m"}, headers)
        self.assertEqual(status, 200)
        self.assertEqual(retry["status"], "duplicate")
        self.assertEqual(retry["message_id"], message_id)
        self.assertEqual(retry["requirement_draft"]["revision_id"], first["requirement_draft"]["revision_id"])
        self.assertEqual(len(self.db.list_messages("demo-tenant", conversation_id)), 1)
        self.assertEqual(len(self.db.get_revisions("demo-tenant", conversation_id)), 1)
        # 同键不同内容：冲突，不写入
        self.assertEqual(self.request("POST", path + "/messages", {"text": "flow 9 m3/h, head 45 m"}, headers)[0], 409)
        self.assertEqual(len(self.db.list_messages("demo-tenant", conversation_id)), 1)
        # 新键视为新消息
        status, _, second = self.request("POST", path + "/messages", {"text": "flow 9 m3/h, head 45 m"}, {**headers, "Idempotency-Key": "msg-2"})
        self.assertEqual(status, 201)
        self.assertNotEqual(second["message_id"], message_id)
        self.assertEqual(len(self.db.list_messages("demo-tenant", conversation_id)), 2)

    def test_message_retry_after_extraction_failure_recovers_draft_once(self):
        """B-08：提取失败（消息已存、无草稿）后同键重试必须补出草稿；持续故障保持明确失败。"""
        _, _, created = self.request("POST", "/api/v1/buyer/conversations", {"site_key": "site-a"})
        conversation_id = created["conversation_id"]
        path = f"/api/v1/buyer/conversations/{conversation_id}"
        headers = {"X-Site-Key": "site-a", "Authorization": f"Bearer {created['access_token']}", "Idempotency-Key": "msg-retry-1"}
        text = "flow 3 m3/h, head 45 m"
        # 提取持续故障：首次与重试都明确 503，不得返回假成功
        with patch("app.extract_requirement_values", side_effect=RuntimeError("提取服务暂不可用")):
            status, _, first_failed = self.request("POST", path + "/messages", {"text": text}, headers)
            self.assertEqual(status, 503)
            self.assertEqual(first_failed["error"], "extraction_unavailable")
            status, _, retry_failed = self.request("POST", path + "/messages", {"text": text}, headers)
            self.assertEqual(status, 503, "提取仍不可用时重试必须继续明确失败")
            self.assertEqual(retry_failed["error"], "extraction_unavailable")
        self.assertEqual(len(self.db.list_messages("demo-tenant", conversation_id)), 1)
        self.assertEqual(len(self.db.get_revisions("demo-tenant", conversation_id)), 0)
        # 服务恢复后同键同内容重试：复用已存消息补提取，返回可用 revision
        status, _, recovered = self.request("POST", path + "/messages", {"text": text}, headers)
        self.assertEqual(status, 200)
        self.assertEqual(recovered["status"], "duplicate")
        draft = recovered["requirement_draft"]
        self.assertTrue(draft and draft["revision_id"])
        self.assertEqual(len(self.db.list_messages("demo-tenant", conversation_id)), 1)
        revisions = self.db.get_revisions("demo-tenant", conversation_id)
        self.assertEqual(len(revisions), 1)
        self.assertEqual(revisions[0]["id"], draft["revision_id"])
        self.assertEqual(self.db.list_messages("demo-tenant", conversation_id)[0]["result_revision_id"], draft["revision_id"])
        # 再次重试返回同一消息与同一草稿；不新增消息或修订
        status, _, again = self.request("POST", path + "/messages", {"text": text}, headers)
        self.assertEqual(status, 200)
        self.assertEqual(again["message_id"], recovered["message_id"])
        self.assertEqual(again["requirement_draft"]["revision_id"], draft["revision_id"])
        self.assertEqual(len(self.db.list_messages("demo-tenant", conversation_id)), 1)
        self.assertEqual(len(self.db.get_revisions("demo-tenant", conversation_id)), 1)
        # 同键改内容仍是 409，不写入
        self.assertEqual(self.request("POST", path + "/messages", {"text": "flow 9 m3/h, head 45 m"}, headers)[0], 409)
        self.assertEqual(len(self.db.list_messages("demo-tenant", conversation_id)), 1)
        self.assertEqual(len(self.db.get_revisions("demo-tenant", conversation_id)), 1)

    def test_message_concurrent_retry_after_failure_creates_single_draft(self):
        """B-08：故障后两路同键并发重试只产生一个草稿；两路返回同一消息与 revision。"""
        _, _, created = self.request("POST", "/api/v1/buyer/conversations", {"site_key": "site-a"})
        conversation_id = created["conversation_id"]
        path = f"/api/v1/buyer/conversations/{conversation_id}"
        headers = {"X-Site-Key": "site-a", "Authorization": f"Bearer {created['access_token']}", "Idempotency-Key": "msg-concurrent-retry"}
        text = "flow 5 m3/h, head 40 m"
        with patch("app.extract_requirement_values", side_effect=RuntimeError("提取服务暂不可用")):
            status, _, _ = self.request("POST", path + "/messages", {"text": text}, headers)
            self.assertEqual(status, 503)
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: self.request("POST", path + "/messages", {"text": text}, headers), range(2)))
        self.assertEqual(sorted(status for status, _, _ in results), [200, 200])
        revision_ids = {body["requirement_draft"]["revision_id"] for _, _, body in results}
        message_ids = {body["message_id"] for _, _, body in results}
        self.assertEqual(len(revision_ids), 1, "并发重试必须复用同一修订")
        self.assertEqual(len(message_ids), 1)
        self.assertEqual(len(self.db.list_messages("demo-tenant", conversation_id)), 1)
        revisions = self.db.get_revisions("demo-tenant", conversation_id)
        self.assertEqual(len(revisions), 1)
        self.assertEqual(revisions[0]["id"], revision_ids.pop())

    def test_message_retry_reuses_keyed_revision_when_attach_was_lost(self):
        """B-08 恢复边界：首次尝试已提交修订但尚未附加到消息（中断窗口）时，重试复用同一修订。"""
        _, _, created = self.request("POST", "/api/v1/buyer/conversations", {"site_key": "site-a"})
        conversation_id = created["conversation_id"]
        path = f"/api/v1/buyer/conversations/{conversation_id}"
        headers = {"X-Site-Key": "site-a", "Authorization": f"Bearer {created['access_token']}", "Idempotency-Key": "msg-attach-lost"}
        text = "flow 12 m3/h, head 40 m"
        request_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
        self.db.save_message(
            "demo-tenant", conversation_id, "buyer", text, "website_api",
            idempotency_key="msg-attach-lost", request_hash=request_hash,
        )
        existing_revision = self.db.create_requirement_revision(
            "demo-tenant", conversation_id, {"flow": {"value": "12", "unit": "m³/h"}}, "candidate", "website_api",
            idempotency_key="msg-attach-lost", request_hash=request_hash,
        )
        status, _, retry = self.request("POST", path + "/messages", {"text": text}, headers)
        self.assertEqual(status, 200)
        self.assertEqual(retry["status"], "duplicate")
        self.assertEqual(retry["requirement_draft"]["revision_id"], existing_revision)
        self.assertEqual(len(self.db.get_revisions("demo-tenant", conversation_id)), 1)
        self.assertEqual(len(self.db.list_messages("demo-tenant", conversation_id)), 1)
        self.assertEqual(self.db.list_messages("demo-tenant", conversation_id)[0]["result_revision_id"], existing_revision)

    def test_next_question_skips_buyer_marked_unknown(self):
        _, _, created = self.request("POST", "/api/v1/buyer/conversations", {"site_key": "site-a"})
        path = f"/api/v1/buyer/conversations/{created['conversation_id']}"
        headers = {"X-Site-Key": "site-a", "Authorization": f"Bearer {created['access_token']}"}
        _, _, sent = self.request("POST", path + "/messages", {"text": "flow 50"}, headers)
        draft = sent["requirement_draft"]
        self.assertEqual(draft["next_question"], "head")
        values = {"flow": {"value": "50", "unit": "m³/h", "label": "流量"}, "head": {"value": "", "unit": "", "label": "扬程"}, "media": {"value": "", "unit": "", "label": "介质"}}
        _, _, confirmed = self.request("POST", path + "/requirements/confirm", {"base_revision_id": draft["revision_id"], "values": values}, headers)
        self.assertEqual(confirmed["values"]["head"]["state"], "unknown")
        self.assertEqual(confirmed["values"]["media"]["state"], "unknown")
        # 买方明确标记“不确定”的项不再被反复追问
        _, _, swept = self.request("POST", path + "/messages", {"text": "no further details for now"}, headers)
        self.assertIsNone(swept["requirement_draft"]["next_question"])
        self.assertIn("head", swept["requirement_draft"]["missing"])
        self.assertIn("media", swept["requirement_draft"]["missing"])

    def test_human_requests_concurrent_dedupe_creates_single_task(self):
        _, _, created = self.request("POST", "/api/v1/buyer/conversations", {"site_key": "site-a"})
        path = f"/api/v1/buyer/conversations/{created['conversation_id']}"
        headers = {"X-Site-Key": "site-a", "Authorization": f"Bearer {created['access_token']}"}
        _, _, unresolved = self.request("POST", path + "/questions", {"question": "Can this handle my application?"}, headers)
        body = {"answer_id": unresolved["answer_id"], "reply_path": "conversation"}
        barrier = threading.Barrier(2)

        def request_human(_):
            barrier.wait(timeout=5)
            return self.request("POST", path + "/human-requests", body, headers)

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(request_human, range(2)))
        statuses = [status for status, _, _ in results]
        self.assertTrue(all(status in (200, 201) for status in statuses), statuses)
        task_ids = {payload["task_id"] for _, _, payload in results}
        self.assertEqual(len(task_ids), 1)
        self.assertEqual(len(self.db.list_human_tasks("demo-tenant")), 1)

    def test_concurrent_rfq_revisions_keep_single_next_revision(self):
        _, _, created = self.request("POST", "/api/v1/buyer/conversations", {"site_key": "site-a"})
        path = f"/api/v1/buyer/conversations/{created['conversation_id']}"
        auth = {"X-Site-Key": "site-a", "Authorization": f"Bearer {created['access_token']}"}
        _, _, first = self.request("POST", path + "/rfqs", {"contact_confirmed": True, "contact": {"contact_email": "buyer@example.com", "contact_phone": "+49 30 555 0215", "quantity": "2", "region": "DE"}}, {**auth, "Idempotency-Key": "init-conc"})
        rfq_id = first["rfq_id"]
        revision = {"expected_revision": 1, "contact_confirmed": True, "contact": {"contact_email": "buyer@example.com"}, "quantity": "3"}
        barrier = threading.Barrier(2)

        def revise(key):
            barrier.wait(timeout=5)
            return self.request("POST", path + f"/rfqs/{rfq_id}/revisions", revision, {**auth, "Idempotency-Key": key})

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(revise, ("conc-a", "conc-b")))
        statuses = sorted(status for status, _, _ in results)
        self.assertEqual(statuses, [201, 409])
        self.assertEqual(self.db.get_rfq("demo-tenant", rfq_id)["current_revision"], 2)
        self.assertEqual(len(self.db.list_rfq_revisions("demo-tenant", rfq_id)), 2)

    def test_rfq_requested_model_text_binds_only_when_matching(self):
        self.db.seed_demo_products("demo-tenant")
        product = self.db.list_products("demo-tenant")[0]
        approve_all_fields(self.db, "demo-tenant", product["id"])
        self.db.publish_product("demo-tenant", product["id"])
        _, _, created = self.request("POST", "/api/v1/buyer/conversations", {"site_key": "site-a"})
        path = f"/api/v1/buyer/conversations/{created['conversation_id']}"
        headers = {"X-Site-Key": "site-a", "Authorization": f"Bearer {created['access_token']}"}
        # 买方手填的型号文字匹配已发布产品 → 绑定该产品
        _, _, matched = self.request("POST", path + "/rfqs", {"contact_confirmed": True, "contact": {"contact_email": "buyer@example.com", "contact_phone": "+49 30 555 0216", "quantity": "2", "region": "DE"}, "requested_model": product["model"]}, {**headers, "Idempotency-Key": "model-1"})
        row = self.db.get_rfq("demo-tenant", matched["rfq_id"])
        self.assertEqual(row["product_id"], product["id"])
        self.assertEqual(row["requested_model"], product["model"])
        # 手填型号无匹配 → 只记录文字、不绑定任何产品（尊重买方表述）
        _, _, custom = self.request("POST", path + "/rfqs", {"contact_confirmed": True, "contact": {"contact_email": "buyer@example.com", "contact_phone": "+49 30 555 0217", "quantity": "2", "region": "DE"}, "requested_model": "ZZ-999（定制款）"}, {**headers, "Idempotency-Key": "model-2"})
        row = self.db.get_rfq("demo-tenant", custom["rfq_id"])
        self.assertIsNone(row["product_id"])
        self.assertEqual(row["requested_model"], "ZZ-999（定制款）")
        # 买方侧会话载荷可见该型号文字（询价卡展示用）
        _, _, current = self.request("GET", path, headers=headers)
        payload = next(item for item in current["rfqs"] if item["id"] == custom["rfq_id"])
        self.assertEqual(payload["requested_model"], "ZZ-999（定制款）")

    def test_same_model_rfq_hint_within_24h_same_conversation(self):
        self.db.seed_demo_products("demo-tenant")
        product = self.db.list_products("demo-tenant")[0]
        approve_all_fields(self.db, "demo-tenant", product["id"])
        self.db.publish_product("demo-tenant", product["id"])
        _, _, created = self.request("POST", "/api/v1/buyer/conversations", {"site_key": "site-a"})
        path = f"/api/v1/buyer/conversations/{created['conversation_id']}"
        headers = {"X-Site-Key": "site-a", "Authorization": f"Bearer {created['access_token']}"}
        payload = {"contact_confirmed": True, "contact": {"contact_email": "buyer@example.com", "contact_phone": "+49 30 555 0218", "quantity": "2", "region": "DE"}, "product_id": product["id"]}
        _, _, first = self.request("POST", path + "/rfqs", payload, {**headers, "Idempotency-Key": "hint-1"})
        self.assertEqual(first["related_rfq_hint"], [])
        _, _, second = self.request("POST", path + "/rfqs", {**payload, "notes": "second request"}, {**headers, "Idempotency-Key": "hint-2"})
        self.assertEqual(len(second["related_rfq_hint"]), 1)
        self.assertEqual(second["related_rfq_hint"][0]["rfq_id"], first["rfq_id"])
        # 独立采购请求仍建立独立编号，不自动合并
        self.assertNotEqual(second["rfq_id"], first["rfq_id"])
        self.assertEqual(len(self.db.list_rfqs("demo-tenant")), 2)
        # 未选型号的 RFQ 不产生同型号提示
        _, _, third = self.request("POST", path + "/rfqs", {"contact_confirmed": True, "contact": {"contact_email": "buyer@example.com", "contact_phone": "+49 30 555 0219", "quantity": "2", "region": "DE"}, "notes": "no model"}, {**headers, "Idempotency-Key": "hint-3"})
        self.assertEqual(third["related_rfq_hint"], [])
        # 跨会话不提示（提示只检查同一会话）
        _, _, other = self.request("POST", "/api/v1/buyer/conversations", {"site_key": "site-a"})
        _, _, cross = self.request(
            "POST", f"/api/v1/buyer/conversations/{other['conversation_id']}/rfqs", payload,
            {"X-Site-Key": "site-a", "Authorization": f"Bearer {other['access_token']}", "Idempotency-Key": "hint-4"},
        )
        self.assertEqual(cross["related_rfq_hint"], [])

    def test_sales_sees_change_alert_and_revision_diffs(self):
        _, _, created = self.request("POST", "/api/v1/buyer/conversations", {"site_key": "site-a"})
        path = f"/api/v1/buyer/conversations/{created['conversation_id']}"
        headers = {"X-Site-Key": "site-a", "Authorization": f"Bearer {created['access_token']}"}
        _, _, first = self.request("POST", path + "/rfqs", {"contact_confirmed": True, "contact": {"contact_email": "buyer@example.com", "contact_phone": "+49 30 555 0220", "quantity": "2", "region": "DE"}, "notes": "First"}, {**headers, "Idempotency-Key": "alert-1"})
        rfq_id = first["rfq_id"]
        _, admin_headers, _ = self.form_request("POST", "/login", {"email": "demo-admin@example.invalid", "password": "demo-admin"})
        cookie = admin_headers["Set-Cookie"].split(";", 1)[0]
        _, _, list_before = self.form_request("GET", "/admin/sales", cookie=cookie)
        self.assertNotIn("买方已修改", list_before)
        _, _, updated = self.request("POST", path + f"/rfqs/{rfq_id}/revisions", {"expected_revision": 1, "contact_confirmed": True, "contact": {"contact_email": "buyer@example.com"}, "quantity": "3", "notes": "Updated"}, {**headers, "Idempotency-Key": "alert-2"})
        self.assertEqual(updated["current_revision"], 2)
        _, _, list_after = self.form_request("GET", "/admin/sales", cookie=cookie)
        self.assertIn("买方已修改 1 次", list_after)
        _, _, detail = self.form_request("GET", f"/admin/sales/{rfq_id}", cookie=cookie)
        self.assertIn("买方已修改此询价 1 次", detail)
        _, _, rfq_page = self.form_request("GET", f"/admin/rfqs/{rfq_id}", cookie=cookie)
        self.assertIn("初次提交", rfq_page)
        self.assertIn("数量：2 → 3", rfq_page)
        self.assertIn("说明：First → Updated", rfq_page)
        # 买方只读取当前内容，不暴露卖方历史对比
        _, _, conv = self.request("GET", path, headers=headers)
        rfq_view = conv["rfqs"][0]
        self.assertEqual(rfq_view["quantity"], "3")
        self.assertEqual(rfq_view["notes"], "Updated")
        self.assertNotIn("revisions", rfq_view)

    def test_buyer_edits_same_rfq_with_seller_history_and_stale_revision_conflict(self):
        self.db.seed_demo_products("demo-tenant")
        product = self.db.list_products("demo-tenant")[0]
        approve_all_fields(self.db, "demo-tenant", product["id"])
        self.db.publish_product("demo-tenant", product["id"])
        _, _, created = self.request("POST", "/api/v1/buyer/conversations", {"site_key": "site-a"})
        path = f"/api/v1/buyer/conversations/{created['conversation_id']}"
        headers = {"X-Site-Key": "site-a", "Authorization": f"Bearer {created['access_token']}"}
        rfq_headers = {**headers, "Idempotency-Key": "initial-rfq"}
        payload = {"entry_surface": "chat", "contact": {"contact_email": "buyer@example.com", "contact_phone": "+49 30 555 0221", "quantity": "2", "region": "DE"}, "contact_confirmed": True, "notes": "First request"}
        _, _, first = self.request("POST", path + "/rfqs", payload, rfq_headers)
        rfq_id = first["rfq_id"]
        revision_path = path + f"/rfqs/{rfq_id}/revisions"
        revision = {"expected_revision": 1, "contact_confirmed": True, "contact": {"contact_email": "buyer@example.com"}, "product_id": product["id"], "quantity": "3", "notes": "Updated request"}
        status, _, updated = self.request("POST", revision_path, revision, {**headers, "Idempotency-Key": "edit-1"})
        self.assertEqual(status, 201)
        self.assertEqual(updated["rfq_id"], rfq_id)
        self.assertEqual(updated["current_revision"], 2)
        self.assertEqual(self.request("POST", revision_path, revision, {**headers, "Idempotency-Key": "edit-2"})[0], 409)
        _, _, current = self.request("GET", path, headers=headers)
        self.assertEqual(current["rfqs"][0]["quantity"], "3")
        self.assertNotIn("revisions", current["rfqs"][0])
        self.assertEqual(len(self.db.list_rfq_revisions("demo-tenant", rfq_id)), 2)

        class SellerView:
            db = self.db
            tenant_id = "demo-tenant"
            user_role = "sales"
            actor = "demo-sales@example.invalid"
            user_id = "test-user"
            path = "/admin/rfqs"
            html = ""
            user_modules = {"rfqs", "sales"}

            def has_module(self, module):
                return module in self.user_modules

            def send_html(self, content, status=200):
                self.html = content

            def console_page(self, title, body, active, lang="zh"):
                return console_layout(title, body, active, lang=lang, path="/admin", actor=self.actor, role=self.user_role, tenant="示例水泵企业（演示）")

        seller = SellerView()
        Handler.admin_rfq(seller, rfq_id, {})
        self.assertIn("First request", seller.html)
        self.assertIn("Updated request", seller.html)
        self.assertGreaterEqual(seller.html.count(product["model"]), 2)
        Handler.admin_sales_detail(seller, rfq_id, {})
        self.assertIn(f"/admin/rfqs/{rfq_id}", seller.html)
        reopened = Database(Path(self.temp_dir.name) / "app.db")
        self.assertEqual(reopened.get_rfq("demo-tenant", rfq_id)["current_revision"], 2)
        self.assertEqual(len(reopened.list_rfq_revisions("demo-tenant", rfq_id)), 2)
        reopened.conn.close()


if __name__ == "__main__":
    unittest.main()
