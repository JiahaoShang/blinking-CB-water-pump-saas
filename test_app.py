import json
import tempfile
import unittest
from pathlib import Path

from app import Database, Handler, answer_from_approved_material, confirm_requirement_values, extract_requirement_values, match_products


class ProductApprovalTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "app.db"
        self.db = Database(self.db_path)

    def tearDown(self):
        self.db.conn.close()
        self.temp_dir.cleanup()

    def test_product_requires_sources_and_approval_before_publish(self):
        product_id = self.db.create_product("demo-tenant", "T-1", "测试泵", "清水")
        source_id = self.db.add_source("demo-tenant", product_id, "manual.pdf", "PDF", "第 2 页")
        self.db.save_field("demo-tenant", product_id, "flow_range", "10-30", "m3/h", source_id, "第 2 页")
        with self.assertRaises(ValueError):
            self.db.publish_product("demo-tenant", product_id)
        for key, value, unit in [
            ("head_range", "20-40", "m"),
            ("media", "清水", ""),
            ("material", "铸铁", ""),
        ]:
            self.db.save_field("demo-tenant", product_id, key, value, unit, source_id, "第 2 页")
        self.db.approve_product("demo-tenant", product_id)
        version = self.db.publish_product("demo-tenant", product_id)
        self.assertEqual(version, 1)
        product = self.db.get_product("demo-tenant", product_id)
        self.assertEqual(product["status"], "published")
        self.assertIsNotNone(self.db.get_published_version("demo-tenant", product_id))

    def test_editing_published_field_downgrades_candidate_and_preserves_version(self):
        product_id = self.db.create_product("demo-tenant", "T-2", "版本泵", "循环")
        source_id = self.db.add_source("demo-tenant", product_id, "manual.pdf", "PDF", "第 1 页")
        for key, value in [("flow_range", "10-30"), ("head_range", "20-40"), ("media", "清水"), ("material", "铸铁")]:
            self.db.save_field("demo-tenant", product_id, key, value, "", source_id, "第 1 页")
        self.db.approve_product("demo-tenant", product_id)
        self.db.publish_product("demo-tenant", product_id)
        self.db.save_field("demo-tenant", product_id, "flow_range", "12-32", "m3/h", source_id, "第 3 页")
        self.assertEqual(self.db.get_product("demo-tenant", product_id)["status"], "pending_review")
        old_version = self.db.get_published_version("demo-tenant", product_id)
        self.assertEqual(json.loads(old_version["snapshot_json"])["fields"][0]["value"], "10-30")

    def test_public_page_never_exposes_other_tenant_product(self):
        self.db.ensure_tenant("other-tenant")
        product_id = self.db.create_product("other-tenant", "T-3", "隔离泵", "内部")
        self.assertIsNone(self.db.get_product_by_slug("demo-tenant", "t-3"))
        self.assertIsNotNone(self.db.get_product("other-tenant", product_id))


class PublicPageTests(unittest.TestCase):
    def test_unpublished_page_returns_404(self):
        with tempfile.TemporaryDirectory() as temp:
            class FakeHandler:
                db = Database(Path(temp) / "app.db")
                tenant_id = "demo-tenant"
                captured = None

                def send_html(self, content, status=200):
                    self.captured = (content, status)

            fake = FakeHandler()
            Handler.public_product(fake, "not-published", {})
            self.assertEqual(fake.captured[1], 404)
            self.assertIn("尚未发布", fake.captured[0])
            fake.db.conn.close()


class RequirementMatchingTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.temp_dir.name) / "app.db")
        self.db.seed_demo_products("demo-tenant")
        for product in self.db.list_products("demo-tenant"):
            self.db.approve_product("demo-tenant", product["id"])
            self.db.publish_product("demo-tenant", product["id"])

    def tearDown(self):
        self.db.conn.close()
        self.temp_dir.cleanup()

    def test_confirmed_requirements_return_explainable_candidate(self):
        conversation_id = self.db.create_conversation("demo-tenant")
        message = "Need a water pump, flow 50 m3/h, head 30 m, clean water"
        self.db.save_message("demo-tenant", conversation_id, "buyer", message)
        values = extract_requirement_values(message, {}, None)
        candidate_id = self.db.create_requirement_revision("demo-tenant", conversation_id, values)
        self.assertTrue(candidate_id)
        confirmed = confirm_requirement_values(values)
        revision_id = self.db.create_requirement_revision("demo-tenant", conversation_id, confirmed, "confirmed")
        status, results, missing = match_products(self.db, "demo-tenant", confirmed)
        recommendation_id = self.db.save_recommendation("demo-tenant", conversation_id, revision_id, status, results)
        self.assertEqual(status, "recommended")
        self.assertEqual(missing, [])
        self.assertEqual(results[0]["model"], "CB-80A")
        self.assertEqual(results[0]["status"], "candidate")
        self.assertIn("流量", "".join(results[0]["satisfies"]))
        self.assertIn("CB-120C", [result["model"] for result in results])
        self.assertTrue(recommendation_id)

    def test_missing_condition_does_not_recommend(self):
        values = confirm_requirement_values({"flow": {"value": "50", "unit": "m3/h", "label": "流量"}})
        status, results, missing = match_products(self.db, "demo-tenant", values)
        self.assertEqual(status, "insufficient")
        self.assertEqual(results, [])
        self.assertIn("扬程", missing)
        self.assertIn("介质", missing)

    def test_new_requirement_revision_invalidates_old_recommendation(self):
        conversation_id = self.db.create_conversation("demo-tenant")
        first = confirm_requirement_values(extract_requirement_values("flow 50, head 30, clean water", {}, None))
        first_revision = self.db.create_requirement_revision("demo-tenant", conversation_id, first, "confirmed")
        first_status, first_results, _ = match_products(self.db, "demo-tenant", first)
        self.db.save_recommendation("demo-tenant", conversation_id, first_revision, first_status, first_results)

        changed = dict(first)
        changed["flow"] = {"value": "100", "unit": "m3/h", "state": "confirmed", "source": "buyer_confirmation", "label": "流量"}
        changed_revision = self.db.create_requirement_revision("demo-tenant", conversation_id, changed, "confirmed")
        changed_status, changed_results, _ = match_products(self.db, "demo-tenant", changed)
        self.db.save_recommendation("demo-tenant", conversation_id, changed_revision, changed_status, changed_results)

        revisions = self.db.get_revisions("demo-tenant", conversation_id)
        recommendations = self.db.get_recommendations("demo-tenant", conversation_id)
        self.assertEqual([revision["revision"] for revision in revisions], [1, 2])
        self.assertEqual(sum(item["status"] == "invalidated" for item in recommendations), 1)
        self.assertEqual(sum(item["status"] == changed_status for item in recommendations), 1)


class GroundedAnswerTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.temp_dir.name) / "app.db")
        self.db.seed_demo_products("demo-tenant")
        for product in self.db.list_products("demo-tenant"):
            self.db.approve_product("demo-tenant", product["id"])
            self.db.publish_product("demo-tenant", product["id"])
        self.product = self.db.conn.execute("SELECT * FROM products WHERE tenant_id=? AND model=?", ("demo-tenant", "CB-80A")).fetchone()

    def tearDown(self):
        self.db.conn.close()
        self.temp_dir.cleanup()

    def test_known_question_returns_answer_and_citation(self):
        result = answer_from_approved_material(self.db, "demo-tenant", "What is the flow range?", self.product["id"])
        status, answer, product_id, version_id, citations, reason = result
        self.assertEqual(status, "grounded")
        self.assertIn("20-80", answer)
        self.assertEqual(product_id, self.product["id"])
        self.assertTrue(version_id)
        self.assertEqual(citations[0]["filename"], "CB-80A-模拟产品手册.pdf")
        self.assertEqual(reason, "")

    def test_unknown_question_creates_resolvable_task_without_changing_product_facts(self):
        conversation_id = self.db.create_conversation("demo-tenant")
        question_id = self.db.save_message("demo-tenant", conversation_id, "buyer", "What is the price and delivery time?")
        status, answer, product_id, version_id, citations, reason = answer_from_approved_material(self.db, "demo-tenant", "What is the price and delivery time?", self.product["id"])
        self.assertEqual(status, "needs_human")
        self.assertIn("无法", answer)
        task_id = self.db.create_human_task("demo-tenant", conversation_id, question_id, "What is the price and delivery time?", reason, {"answer_so_far": answer}, product_id, version_id)
        self.db.resolve_human_task("demo-tenant", task_id, "工程师需要根据当前项目条件确认价格和交期。")
        task = self.db.get_human_task("demo-tenant", task_id)
        self.assertEqual(task["status"], "resolved")
        self.assertIn("工程师", task["response"])
        field = self.db.get_fields("demo-tenant", self.product["id"])[0]
        self.assertNotEqual(field["value"], "工程师需要根据当前项目条件确认价格和交期。")


class RfqTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.temp_dir.name) / "app.db")
        self.db.seed_demo_products("demo-tenant")
        for product in self.db.list_products("demo-tenant"):
            self.db.approve_product("demo-tenant", product["id"])
            self.db.publish_product("demo-tenant", product["id"])
        self.product = self.db.conn.execute("SELECT * FROM products WHERE tenant_id=? AND model=?", ("demo-tenant", "CB-80A")).fetchone()

    def tearDown(self):
        self.db.conn.close()
        self.temp_dir.cleanup()

    def _conversation_with_recommendation(self):
        conversation_id = self.db.create_conversation("demo-tenant")
        values = confirm_requirement_values(extract_requirement_values("flow 50, head 30, clean water", {}, None))
        revision_id = self.db.create_requirement_revision("demo-tenant", conversation_id, values, "confirmed")
        status, results, _ = match_products(self.db, "demo-tenant", values)
        recommendation_id = self.db.save_recommendation("demo-tenant", conversation_id, revision_id, status, results)
        return conversation_id, revision_id, recommendation_id

    def test_rfq_saves_confirmed_snapshot_and_duplicate_submission_is_idempotent(self):
        conversation_id, _revision_id, _recommendation_id = self._conversation_with_recommendation()
        contact = {
            "contact_name": "Buyer",
            "company": "Example GmbH",
            "contact_email": "buyer@example.com",
            "contact_phone": "+49 1",
            "quantity": "2",
            "region": "Germany",
            "delivery": "8 weeks",
        }
        rfq_id, created = self.db.create_rfq("demo-tenant", conversation_id, "same-token", contact, "Cooling water project", self.product["id"])
        duplicate_id, duplicate_created = self.db.create_rfq("demo-tenant", conversation_id, "same-token", contact, "Cooling water project", self.product["id"])
        self.assertTrue(created)
        self.assertFalse(duplicate_created)
        self.assertEqual(rfq_id, duplicate_id)
        self.assertEqual(len(self.db.list_rfqs("demo-tenant")), 1)
        rfq = self.db.get_rfq("demo-tenant", rfq_id)
        self.assertEqual(rfq["status"], "submitted")
        snapshot = json.loads(rfq["snapshot_json"])
        self.assertEqual(snapshot["recommendation"]["model"], "CB-80A")
        self.assertEqual(snapshot["requirements"]["flow"]["value"], "50")

    def test_incomplete_rfq_is_saved_as_needs_info(self):
        conversation_id, _revision_id, _recommendation_id = self._conversation_with_recommendation()
        rfq_id, created = self.db.create_rfq("demo-tenant", conversation_id, "partial-token", {"contact_email": "buyer@example.com"}, "Need help", self.product["id"])
        rfq = self.db.get_rfq("demo-tenant", rfq_id)
        self.assertTrue(created)
        self.assertEqual(rfq["status"], "needs_info")
        self.assertIn("数量", json.loads(rfq["missing_json"]))
        self.assertIn("目的地", json.loads(rfq["missing_json"]))


if __name__ == "__main__":
    unittest.main()
