import http.client
from urllib.parse import urlencode
import tempfile
import threading
import unittest
from pathlib import Path

from app import make_server


class BuyerConversationAccessTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.server = make_server(Path(self.temp_dir.name) / "app.db", port=0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.server.RequestHandlerClass.db.conn.close()
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

    def test_private_conversation_requires_issued_cookie_for_read_and_write(self):
        status, headers, _ = self.request("GET", "/inquiry")
        self.assertEqual(status, 303)
        path = headers["Location"]
        cookie = headers["Set-Cookie"].split(";", 1)[0]
        self.assertIn("HttpOnly", headers["Set-Cookie"])

        self.assertEqual(self.request("GET", path)[0], 404)
        self.assertEqual(self.request("POST", path.split("?")[0] + "/message", "message=secret")[0], 404)
        self.assertEqual(self.request("GET", path, cookie=cookie)[0], 200)
        self.assertEqual(self.request("POST", path.split("?")[0] + "/message", "message=valid", cookie=cookie)[0], 303)
        self.assertIn("valid", self.request("GET", path, cookie=cookie)[2])

        other_status, other_headers, _ = self.request("GET", "/inquiry")
        self.assertEqual(other_status, 303)
        other_cookie = other_headers["Set-Cookie"].split(";", 1)[0]
        self.assertEqual(self.request("GET", path, cookie=other_cookie)[0], 404)

    def test_early_rfq_requires_reply_contact_and_is_saved_once(self):
        _, headers, _ = self.request("GET", "/inquiry")
        path = headers["Location"].split("?", 1)[0]
        cookie = headers["Set-Cookie"].split(";", 1)[0]
        no_contact = urlencode({"submission_key": "same-early", "notes": "Need a quote"})
        self.assertEqual(self.request("POST", path + "/rfq", no_contact, cookie)[0], 400)
        self.assertEqual(len(self.server.RequestHandlerClass.db.list_rfqs("demo-tenant")), 0)
        with_contact = urlencode({"submission_key": "same-early", "notes": "Need a quote", "contact_email": "buyer@example.com"})
        self.assertEqual(self.request("POST", path + "/rfq", with_contact, cookie)[0], 303)
        self.assertEqual(self.request("POST", path + "/rfq", with_contact, cookie)[0], 303)
        self.assertEqual(len(self.server.RequestHandlerClass.db.list_rfqs("demo-tenant")), 1)

    def test_unresolved_question_creates_task_only_after_buyer_requests_it(self):
        _, headers, _ = self.request("GET", "/inquiry")
        path = headers["Location"].split("?", 1)[0]
        cookie = headers["Set-Cookie"].split(";", 1)[0]
        self.assertEqual(self.request("POST", path + "/ask", urlencode({"question": "Can you confirm the price?"}), cookie)[0], 303)
        db = self.server.RequestHandlerClass.db
        conversation_id = path.rsplit("/", 1)[1]
        answer = db.list_answers("demo-tenant", conversation_id)[0]
        self.assertEqual(answer["answer_status"], "needs_human")
        self.assertEqual(len(db.list_human_tasks("demo-tenant")), 0)
        self.assertIn("Ask an engineer", self.request("GET", path, cookie=cookie)[2])

        body = urlencode({"answer_id": answer["id"]})
        self.assertEqual(self.request("POST", path + "/human-request", body, cookie)[0], 303)
        self.assertEqual(self.request("POST", path + "/human-request", body, cookie)[0], 303)
        self.assertEqual(len(db.list_human_tasks("demo-tenant")), 1)
        self.assertIn("pending", self.request("GET", path, cookie=cookie)[2])

    def test_legacy_inquiry_gates_tentative_and_never_claims_auto_task(self):
        """旧版 /inquiry 页与新口径一致：待核实需主动查看；资料外问题不冒充已安排人工。"""
        db = self.server.RequestHandlerClass.db
        db.seed_demo_products("demo-tenant")
        for product in db.list_products("demo-tenant"):
            for field in db.get_fields("demo-tenant", product["id"]):
                if field["state"] == "candidate":
                    db.approve_field("demo-tenant", product["id"], field["field_key"])
            db.finalize_product_review("demo-tenant", product["id"])
            db.publish_product("demo-tenant", product["id"])
        _, headers, _ = self.request("GET", "/inquiry")
        path = headers["Location"].split("?", 1)[0]
        cookie = headers["Set-Cookie"].split(";", 1)[0]
        self.assertEqual(self.request("POST", path + "/message", urlencode({"message": "Need pump flow 50 m3/h, head 30 m, clean water, temperature 100"}), cookie)[0], 303)
        confirm = urlencode({"flow": "50", "head": "30", "media": "clean water", "temperature": "100", "quantity": "2"})
        self.assertEqual(self.request("POST", path + "/confirm", confirm, cookie)[0], 303)
        page = self.request("GET", path, cookie=cookie)[2]
        self.assertIn("匹配结果", page)
        self.assertIn("查看待核实候选", page)
        self.assertNotIn("CB-80A", page)  # 待核实候选未泄漏（正文与下拉选项均不下发）
        shown = self.request("GET", path + "?show_tentative=1", cookie=cookie)[2]
        self.assertIn("CB-80A", shown)
        self.assertIn("待核实", shown)
        self.assertEqual(self.request("POST", path + "/ask", urlencode({"question": "What is the price?"}), cookie)[0], 303)
        self.assertEqual(len(db.list_human_tasks("demo-tenant")), 0)
        asked = self.request("GET", path + "?notice=answer", cookie=cookie)[2]
        self.assertIn("Ask an engineer", asked)
        self.assertNotIn("后台已生成待处理人工任务", asked)
        self.assertIn("不会自动安排", asked)

    def test_legacy_confirm_and_ask_dedupe_submission_key(self):
        """旧页双击/重发同一提交键：不产生重复确认修订与重复问答。"""
        db = self.server.RequestHandlerClass.db
        _, headers, _ = self.request("GET", "/inquiry")
        path = headers["Location"].split("?", 1)[0]
        cookie = headers["Set-Cookie"].split(";", 1)[0]
        conversation_id = path.rsplit("/", 1)[1]
        self.assertEqual(self.request("POST", path + "/message", urlencode({"message": "flow 50 m3/h, head 30 m, clean water"}), cookie)[0], 303)
        confirm = urlencode({"submission_key": "legacy-confirm-1", "flow": "50", "head": "30", "media": "clean water", "temperature": "60"})
        self.assertEqual(self.request("POST", path + "/confirm", confirm, cookie)[0], 303)
        self.assertEqual(self.request("POST", path + "/confirm", confirm, cookie)[0], 303)
        revisions = db.get_revisions("demo-tenant", conversation_id)
        self.assertEqual(len([row for row in revisions if row["status"] == "confirmed"]), 1)
        ask = urlencode({"submission_key": "legacy-ask-1", "question": "Can you confirm the price?"})
        self.assertEqual(self.request("POST", path + "/ask", ask, cookie)[0], 303)
        self.assertEqual(self.request("POST", path + "/ask", ask, cookie)[0], 303)
        self.assertEqual(len(db.list_answers("demo-tenant", conversation_id)), 1)


if __name__ == "__main__":
    unittest.main()
