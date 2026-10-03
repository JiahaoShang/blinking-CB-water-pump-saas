import http.client
import hashlib
import json
import tempfile
import threading
import unittest
from pathlib import Path
from urllib.parse import urlencode

from app import Database, Handler, ROOT, make_server


class BuyerApiTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.server = make_server(Path(self.temp_dir.name) / "app.db", port=0)
        self.db = self.server.RequestHandlerClass.db
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

    def test_public_image_preview_and_download_require_current_permission(self):
        product_id = self.db.create_product("demo-tenant", "IMG-1", "Pump with approved image", "unreviewed use case")
        source_id = self.db.add_source("demo-tenant", product_id, "pump.png", "PNG", "Image 1")
        contents = b"\x89PNG\r\n\x1a\n" + b"demo-image"
        upload = ROOT / "data" / "uploads" / f"{source_id}-pump.png"
        upload.parent.mkdir(parents=True, exist_ok=True)
        upload.write_bytes(contents)
        self.addCleanup(upload.unlink, missing_ok=True)
        self.db.conn.execute("UPDATE source_documents SET storage_path=?, sha256=? WHERE id=?", (f"data/uploads/{upload.name}", hashlib.sha256(contents).hexdigest(), source_id))
        self.db.conn.commit()
        self.db.save_field("demo-tenant", product_id, "flow_range", "10-30", "m³/h", source_id, "Image 1")
        self.db.approve_product("demo-tenant", product_id)
        self.db.publish_product("demo-tenant", product_id)
        path = f"/s/site-a/products/img-1/sources/{source_id}"
        self.assertEqual(self.raw_get(path + "?preview=1")[0], 404)
        self.db.allow_public_source("demo-tenant", product_id, source_id, "demo-admin")
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
        self.db.approve_product("demo-tenant", product["id"])
        self.db.publish_product("demo-tenant", product["id"])
        second = next(item for item in self.db.list_products("demo-tenant") if item["id"] != product["id"])
        self.db.approve_product("demo-tenant", second["id"])
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
        sections = ["hero", "advantages", "specifications", "comparison", "applications", "customization", "manufacturing", "certification", "downloads", "faq"]
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
            self.db.approve_product("demo-tenant", product["id"])
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
        self.db.approve_product("demo-tenant", product["id"])
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
        payload = {"entry_surface": "chat", "contact": {"contact_email": "buyer@example.com"}, "notes": "Need price", "contact_confirmed": True}
        self.assertEqual(self.request("POST", path + "/rfqs", {**payload, "contact_confirmed": False}, headers)[0], 422)
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
        _, _, submitted = self.request("POST", path + "/rfqs", {"contact_confirmed": True, "contact": {"contact_email": "buyer@example.test"}}, headers)
        rfq_id = submitted["rfq_id"]
        _, sales_headers, _ = self.form_request("POST", "/login", {"email": "demo-sales@example.invalid", "password": "demo-sales"})
        sales_cookie = sales_headers["Set-Cookie"].split(";", 1)[0]
        _, _, sales_page = self.form_request("GET", f"/admin/sales/{rfq_id}", cookie=sales_cookie)
        self.assertIn("领取这条会话", sales_page)
        self.assertNotIn("分配或改派", sales_page)
        self.assertEqual(self.form_request("POST", f"/admin/sales/{rfq_id}/claim", {}, sales_cookie)[0], 303)
        self.assertEqual(self.db.get_lead_for_rfq("demo-tenant", rfq_id)["owner"], "demo-sales@example.invalid")
        self.assertEqual(self.form_request("POST", f"/admin/sales/{rfq_id}/assign", {"owner_email": "demo-sales@example.invalid"}, sales_cookie)[0], 400)
        _, admin_headers, _ = self.form_request("POST", "/login", {"email": "demo-admin@example.invalid", "password": "demo-admin"})
        admin_cookie = admin_headers["Set-Cookie"].split(";", 1)[0]
        _, _, admin_page = self.form_request("GET", f"/admin/sales/{rfq_id}", cookie=admin_cookie)
        self.assertIn("分配或改派", admin_page)
        self.assertEqual(self.form_request("POST", f"/admin/sales/{rfq_id}/assign", {"owner_email": "demo-sales@example.invalid"}, admin_cookie)[0], 303)

    def test_buyer_stop_contact_is_visible_to_sales_and_blocks_followup_date(self):
        _, _, created = self.request("POST", "/api/v1/buyer/conversations", {"site_key": "site-a"})
        path = f"/api/v1/buyer/conversations/{created['conversation_id']}"
        headers = {"X-Site-Key": "site-a", "Authorization": f"Bearer {created['access_token']}"}
        _, _, submitted = self.request("POST", path + "/rfqs", {"contact_confirmed": True, "contact": {"contact_email": "buyer@example.test"}}, {**headers, "Idempotency-Key": "stop-rfq"})
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
        self.assertEqual(self.form_request("POST", f"/admin/sales/{rfq_id}/update", {"stage": "technical_review", "next_follow_up": "2026-10-21"}, cookie)[0], 400)

    def test_buyer_edits_same_rfq_with_seller_history_and_stale_revision_conflict(self):
        self.db.seed_demo_products("demo-tenant")
        product = self.db.list_products("demo-tenant")[0]
        self.db.approve_product("demo-tenant", product["id"])
        self.db.publish_product("demo-tenant", product["id"])
        _, _, created = self.request("POST", "/api/v1/buyer/conversations", {"site_key": "site-a"})
        path = f"/api/v1/buyer/conversations/{created['conversation_id']}"
        headers = {"X-Site-Key": "site-a", "Authorization": f"Bearer {created['access_token']}"}
        rfq_headers = {**headers, "Idempotency-Key": "initial-rfq"}
        payload = {"entry_surface": "chat", "contact": {"contact_email": "buyer@example.com"}, "contact_confirmed": True, "notes": "First request"}
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
            html = ""

            def send_html(self, content, status=200):
                self.html = content

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
