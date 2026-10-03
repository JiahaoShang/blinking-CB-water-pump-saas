import http.client
import json
import tempfile
import threading
import unittest
from pathlib import Path

from app import Database, Handler, make_server


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
        self.assertTrue(result["results"][0]["detail_url"].startswith("/products/"))
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
