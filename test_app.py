import json
import subprocess
import sys
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch
from pathlib import Path

from app import Database, Handler, answer_from_approved_material, confirm_requirement_values, curve_head_at_flow, extract_requirement_values, layout, match_products, performance_points_from_text


def approve_all_fields(db, tenant_id, product_id):
    """Simulate an admin who individually approved every sourced candidate field."""
    for field in db.get_fields(tenant_id, product_id):
        if field["state"] == "candidate":
            db.approve_field(tenant_id, product_id, field["field_key"])
    db.finalize_product_review(tenant_id, product_id)


class ProductApprovalTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "app.db"
        self.db = Database(self.db_path)
        # 测试使用的演示企业属于测试夹具：必须显式创建（普通新库不再预置）。
        self.db.ensure_tenant("demo-tenant", "示例水泵企业（演示）")

    def tearDown(self):
        self.db.conn.close()
        self.temp_dir.cleanup()

    def test_limited_page_can_publish_only_reviewed_sourced_facts_and_show_gaps(self):
        product_id = self.db.create_product("demo-tenant", "T-1", "测试泵", "清水")
        source_id = self.db.add_source("demo-tenant", product_id, "manual.pdf", "PDF", "第 2 页")
        with self.assertRaises(ValueError):
            self.db.finalize_product_review("demo-tenant", product_id)
        self.db.save_field("demo-tenant", product_id, "flow_range", "10-30", "m3/h", source_id, "第 2 页")
        self.db.save_field("demo-tenant", product_id, "material", "未核对材质", "", None, "")
        with self.assertRaises(ValueError):
            self.db.finalize_product_review("demo-tenant", product_id)  # 候选字段未逐项处理
        with self.assertRaises(ValueError):
            self.db.approve_field("demo-tenant", product_id, "material")  # 缺来源位置不能核准
        self.db.approve_field("demo-tenant", product_id, "flow_range")
        self.db.reject_field("demo-tenant", product_id, "material", "没有来源位置，暂不公开")
        self.db.finalize_product_review("demo-tenant", product_id)
        version = self.db.publish_product("demo-tenant", product_id)
        self.assertEqual(version, 1)
        product = self.db.get_product("demo-tenant", product_id)
        self.assertEqual(product["status"], "published")
        fields = {field["field_key"]: field for field in self.db.get_fields("demo-tenant", product_id)}
        self.assertEqual(fields["material"]["state"], "rejected")
        self.assertEqual(fields["material"]["review_note"], "没有来源位置，暂不公开")
        snapshot = json.loads(self.db.get_published_version("demo-tenant", product_id)["snapshot_json"])
        self.assertEqual([item["field_key"] for item in snapshot["fields"]], ["flow_range"])
        self.assertIn("head_range", snapshot["missing_fields"])
        self.assertIn("material", snapshot["missing_fields"])
        class FakeHandler:
            db = self.db
            tenant_id = "demo-tenant"
            captured = None

            def send_html(self, content, status=200):
                self.captured = (content, status)

        fake = FakeHandler()
        Handler.public_product(fake, product["slug"], {"lang": ["zh"]})
        self.assertEqual(fake.captured[1], 200)
        self.assertIn("待补充信息", fake.captured[0])
        self.assertNotIn("未核对材质", fake.captured[0])

    def test_editing_published_field_downgrades_candidate_and_preserves_version(self):
        product_id = self.db.create_product("demo-tenant", "T-2", "版本泵", "循环")
        source_id = self.db.add_source("demo-tenant", product_id, "manual.pdf", "PDF", "第 1 页")
        for key, value in [("flow_range", "10-30"), ("head_range", "20-40"), ("media", "清水"), ("material", "铸铁")]:
            self.db.save_field("demo-tenant", product_id, key, value, "", source_id, "第 1 页")
        approve_all_fields(self.db, "demo-tenant", product_id)
        self.db.publish_product("demo-tenant", product_id)
        self.db.save_field("demo-tenant", product_id, "flow_range", "12-32", "m3/h", source_id, "第 3 页")
        self.assertEqual(self.db.get_product("demo-tenant", product_id)["status"], "pending_review")
        old_version = self.db.get_published_version("demo-tenant", product_id)
        self.assertEqual(json.loads(old_version["snapshot_json"])["fields"][0]["value"], "10-30")
        class FakeHandler:
            db = self.db
            tenant_id = "demo-tenant"
            captured = None

            def send_html(self, content, status=200):
                self.captured = (content, status)

        fake = FakeHandler()
        Handler.public_product(fake, "t-2", {})
        self.assertEqual(fake.captured[1], 200)
        self.assertIn("10-30", fake.captured[0])
        self.assertNotIn("12-32", fake.captured[0])

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

    def test_source_file_is_not_public_without_explicit_file_permission(self):
        with tempfile.TemporaryDirectory() as temp:
            db = Database(Path(temp) / "app.db")
            db.ensure_tenant("demo-tenant", "示例水泵企业（演示）")
            product_id = db.create_product("demo-tenant", "T-PUBLIC", "测试泵", "清水")
            source_id = db.add_source("demo-tenant", product_id, "manual.pdf", "PDF", "p. 2")
            db.save_field("demo-tenant", product_id, "flow_range", "10-30", "m³/h", source_id, "p. 2")
            approve_all_fields(db, "demo-tenant", product_id)
            db.publish_product("demo-tenant", product_id)
            db.conn.execute("UPDATE source_documents SET storage_path='README.md' WHERE id=?", (source_id,))
            db.conn.commit()

            class FakeHandler:
                tenant_id = "demo-tenant"
                captured = None

                def __init__(self, database):
                    self.db = database

                def send_html(self, content, status=200):
                    self.captured = (content, status)

                def send_bytes(self, *args, **kwargs):
                    self.captured = ("file served", 200)

            fake = FakeHandler(db)
            Handler.public_product(fake, "t-public", {})
            self.assertNotIn(f"/sources/{source_id}'", fake.captured[0])
            Handler.public_source(fake, "t-public", source_id)
            self.assertEqual(fake.captured[1], 404)
            # 归属未确认的文件不能开放公开下载（新校验）
            with self.assertRaises(ValueError):
                db.allow_public_source("demo-tenant", product_id, source_id, "demo-admin")
            db.set_source_scope("demo-tenant", product_id, source_id, "exact_model")
            db.allow_public_source("demo-tenant", product_id, source_id, "demo-admin")
            # 发布版本冻结文件清单：发布后才开放的文件要等新版本才进入买方页
            Handler.public_product(fake, "t-public", {})
            self.assertNotIn(f"/sources/{source_id}'", fake.captured[0])
            Handler.public_source(fake, "t-public", source_id)
            self.assertEqual(fake.captured[1], 404)
            db.publish_product("demo-tenant", product_id)
            Handler.public_product(fake, "t-public", {})
            self.assertIn(f"/sources/{source_id}'", fake.captured[0])
            Handler.public_source(fake, "t-public", source_id)
            self.assertEqual(fake.captured[1], 200)
            db.revoke_public_source("demo-tenant", product_id, source_id, "demo-admin")
            Handler.public_source(fake, "t-public", source_id)
            self.assertEqual(fake.captured[1], 404)
            db.conn.close()


class RequirementMatchingTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.temp_dir.name) / "app.db")
        self.db.ensure_tenant("demo-tenant", "示例水泵企业（演示）")
        self.db.seed_demo_products("demo-tenant")
        for product in self.db.list_products("demo-tenant"):
            approve_all_fields(self.db, "demo-tenant", product["id"])
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
        self.db.ensure_tenant("demo-tenant", "示例水泵企业（演示）")
        self.db.seed_demo_products("demo-tenant")
        for product in self.db.list_products("demo-tenant"):
            approve_all_fields(self.db, "demo-tenant", product["id"])
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

    def test_engineer_reply_failure_keeps_task_pending_and_retry_delivers_once(self):
        conversation_id = self.db.create_conversation("demo-tenant")
        question_id = self.db.save_message("demo-tenant", conversation_id, "buyer", "Is this item in stock?")
        task_id = self.db.create_human_task("demo-tenant", conversation_id, question_id, "Is this item in stock?", "库存需要核实", {})
        with patch.object(self.db, "save_message", side_effect=OSError("message write failed")):
            with self.assertRaises(OSError):
                self.db.resolve_human_task("demo-tenant", task_id, "Please contact sales for current stock.")
        self.assertEqual(self.db.get_human_task("demo-tenant", task_id)["status"], "pending")
        self.assertEqual(len(self.db.list_messages("demo-tenant", conversation_id)), 1)

        self.db.resolve_human_task("demo-tenant", task_id, "Please contact sales for current stock.")
        self.assertEqual(self.db.get_human_task("demo-tenant", task_id)["status"], "resolved")
        self.assertEqual(len(self.db.list_messages("demo-tenant", conversation_id)), 2)
        with self.assertRaises(ValueError):
            self.db.resolve_human_task("demo-tenant", task_id, "duplicate")
        self.assertEqual(len(self.db.list_messages("demo-tenant", conversation_id)), 2)

    def test_engineer_followup_waits_for_buyer_on_same_task(self):
        conversation_id = self.db.create_conversation("demo-tenant")
        question_id = self.db.save_message("demo-tenant", conversation_id, "buyer", "Is this item in stock?")
        task_id = self.db.create_human_task("demo-tenant", conversation_id, question_id, "Is this item in stock?", "库存需要核实", {})
        self.db.ask_task_followup("demo-tenant", task_id, "What quantity do you need?")
        self.assertEqual(self.db.get_human_task("demo-tenant", task_id)["status"], "waiting_buyer")
        self.assertEqual(self.db.list_task_followups("demo-tenant", task_id)[0]["source"], f"engineer-followup:{task_id}")
        self.db.answer_task_followup("demo-tenant", conversation_id, task_id, "10 units")
        self.assertEqual(self.db.get_human_task("demo-tenant", task_id)["status"], "pending")
        self.assertEqual(len(self.db.list_task_followups("demo-tenant", task_id)), 2)
        self.db.resolve_human_task("demo-tenant", task_id, "We will check availability for 10 units.")
        self.assertEqual(self.db.get_human_task("demo-tenant", task_id)["status"], "resolved")
        self.assertEqual(len(self.db.list_messages("demo-tenant", conversation_id)), 4)


class RfqTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.temp_dir.name) / "app.db")
        self.db.ensure_tenant("demo-tenant", "示例水泵企业（演示）")
        self.db.seed_demo_products("demo-tenant")
        for product in self.db.list_products("demo-tenant"):
            approve_all_fields(self.db, "demo-tenant", product["id"])
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
        # V1-01：必填字段补齐后，软缺项只剩"期望交期"，照旧进入 needs_info 与"还缺"清单
        rfq_id, created = self.db.create_rfq("demo-tenant", conversation_id, "partial-token", {"contact_email": "buyer@example.com", "contact_phone": "+49 30 555 0201", "quantity": "2", "region": "Germany"}, "Need help", self.product["id"])
        rfq = self.db.get_rfq("demo-tenant", rfq_id)
        self.assertTrue(created)
        self.assertEqual(rfq["status"], "needs_info")
        self.assertIn("期望交期", json.loads(rfq["missing_json"]))
        self.assertNotIn("数量", json.loads(rfq["missing_json"]))

    def test_buyer_can_submit_before_requirement_confirmation_with_contact(self):
        conversation_id = self.db.create_conversation("demo-tenant")
        rfq_id, created = self.db.create_rfq(
            "demo-tenant", conversation_id, "early-rfq",
            {"contact_email": "buyer@example.com", "contact_phone": "+49 30 555 0202", "quantity": "1", "region": "Germany"}, "Please quote for my project",
        )
        self.assertTrue(created)
        rfq = self.db.get_rfq("demo-tenant", rfq_id)
        self.assertIsNone(rfq["requirement_revision_id"])
        self.assertEqual(rfq["status"], "needs_info")

    def test_rfq_without_reply_contact_creates_no_record(self):
        conversation_id = self.db.create_conversation("demo-tenant")
        # V1-01：默认口径下四个必填字段缺一即拒绝（报错列出全部缺项）
        with self.assertRaisesRegex(ValueError, "请填写：邮箱、电话、数量、目的地"):
            self.db.create_rfq("demo-tenant", conversation_id, "no-contact", {}, "Please quote")
        self.assertEqual(len(self.db.list_rfqs("demo-tenant")), 0)


class DemoFixtureTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.temp_dir.name) / "app.db")
        # 演示夹具需要演示企业、演示账号与线索负责人（分配校验会核对用户）。
        self.db.seed_demo_users()

    def tearDown(self):
        self.db.conn.close()
        self.temp_dir.cleanup()

    def test_demo_workspace_is_complete_and_idempotent(self):
        first = self.db.seed_demo_workspace("demo-tenant")
        self.assertEqual(first["products"], 2)
        self.assertEqual(first["rfqs"], 2)
        self.assertEqual(first["tasks"], 1)
        self.assertEqual(len(self.db.list_rfqs("demo-tenant")), 2)
        self.assertEqual(len(self.db.list_human_tasks("demo-tenant", "pending")), 1)
        self.assertEqual({item["stage"] for item in self.db.list_sales_records("demo-tenant")}, {"technical_review", "needs_info"})
        published = self.db.conn.execute("SELECT COUNT(*) FROM products WHERE tenant_id=? AND status='published'", ("demo-tenant",)).fetchone()[0]
        self.assertEqual(published, 2)

        second = self.db.seed_demo_workspace("demo-tenant")
        self.assertEqual(second["rfqs"], 0)
        self.assertEqual(second["tasks"], 0)
        self.assertEqual(len(self.db.list_rfqs("demo-tenant")), 2)
        self.assertEqual(len(self.db.list_human_tasks("demo-tenant", "pending")), 1)


class LowaraFixtureTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.temp_dir.name) / "app.db")
        self.db.ensure_tenant("demo-tenant", "示例水泵企业（演示）")

    def tearDown(self):
        self.db.conn.close()
        self.temp_dir.cleanup()

    def test_lowara_fixture_publishes_cited_product_and_is_idempotent(self):
        first = self.db.seed_lowara_workspace("demo-tenant")
        second = self.db.seed_lowara_workspace("demo-tenant")
        self.assertEqual(first["products"], 1)
        self.assertEqual(second["products"], 0)
        product = self.db.conn.execute(
            "SELECT * FROM products WHERE tenant_id=? AND model=?", ("demo-tenant", "3SV08NL007T")
        ).fetchone()
        self.assertEqual(product["status"], "published")
        fields = {row["field_key"]: row for row in self.db.get_fields("demo-tenant", product["id"])}
        self.assertEqual(fields["power"]["value"], "0.75")
        self.assertIn("Rev. B", fields["performance_points"]["source_location"])
        self.assertEqual(len(self.db.get_sources("demo-tenant", product["id"])), 3)

    def test_lowara_catalogue_curve_requires_engineering_confirmation(self):
        self.db.seed_lowara_workspace("demo-tenant")
        values = confirm_requirement_values({
            "flow": {"value": "3", "unit": "m³/h", "label": "流量"},
            "head": {"value": "48", "unit": "m", "label": "扬程"},
            "media": {"value": "清水", "unit": "", "label": "介质"},
        })
        status, results, missing = match_products(self.db, "demo-tenant", values)
        lowara = next(result for result in results if result["model"] == "3SV08NL007T")
        self.assertEqual(status, "needs_confirmation")
        self.assertEqual(missing, [])
        self.assertEqual(lowara["status"], "needs_confirmation")
        self.assertIn("工程师", "".join(lowara["unknown"]))

    def test_delivery_wording_is_handed_to_human(self):
        self.db.seed_lowara_workspace("demo-tenant")
        product = self.db.conn.execute(
            "SELECT id FROM products WHERE tenant_id=? AND model=?", ("demo-tenant", "3SV08NL007T")
        ).fetchone()
        status, answer, *_rest = answer_from_approved_material(self.db, "demo-tenant", "什么时候可以交货？", product["id"])
        self.assertEqual(status, "needs_human")
        self.assertIn("交期", answer)

    def test_rfq_uses_lowara_confirmation_candidate_when_product_is_not_selected(self):
        self.db.seed_lowara_workspace("demo-tenant")
        conversation_id = self.db.create_conversation("demo-tenant")
        values = confirm_requirement_values({
            "flow": {"value": "3", "unit": "m³/h", "label": "流量"},
            "head": {"value": "48", "unit": "m", "label": "扬程"},
            "media": {"value": "清水", "unit": "", "label": "介质"},
        })
        revision_id = self.db.create_requirement_revision("demo-tenant", conversation_id, values, "confirmed")
        status, results, _ = match_products(self.db, "demo-tenant", values)
        self.db.save_recommendation("demo-tenant", conversation_id, revision_id, status, results)
        rfq_id, created = self.db.create_rfq(
            "demo-tenant", conversation_id, "lowara-rfq-token",
            {"quantity": "1", "region": "上海", "delivery": "6 周", "contact_email": "buyer@example.com", "contact_phone": "+86 21 5555 0204"}, "演示询价"
        )
        self.assertTrue(created)
        rfq = self.db.get_rfq("demo-tenant", rfq_id)
        self.assertEqual(rfq["model"], "3SV08NL007T")
        self.assertEqual(rfq["status"], "needs_info")

    def test_catalogue_points_are_interpolated_only_for_demo_screening(self):
        points = performance_points_from_text("0:60;3:48.1;4.4:27.5")
        self.assertEqual(points, [(0.0, 60.0), (3.0, 48.1), (4.4, 27.5)])
        self.assertAlmostEqual(curve_head_at_flow(points, 3.7), 37.8, places=2)


class SalesWorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.temp_dir.name) / "app.db")
        # 领取/分配会校验本企业销售与管理员账号：显式种入演示账号。
        self.db.seed_demo_users()
        self.db.seed_demo_products("demo-tenant")
        for product in self.db.list_products("demo-tenant"):
            approve_all_fields(self.db, "demo-tenant", product["id"])
            self.db.publish_product("demo-tenant", product["id"])
        conversation_id = self.db.create_conversation("demo-tenant")
        values = confirm_requirement_values(extract_requirement_values("flow 50, head 30, clean water", {}, None))
        revision_id = self.db.create_requirement_revision("demo-tenant", conversation_id, values, "confirmed")
        status, results, _ = match_products(self.db, "demo-tenant", values)
        self.db.save_recommendation("demo-tenant", conversation_id, revision_id, status, results)
        self.rfq_id, _ = self.db.create_rfq("demo-tenant", conversation_id, "sales-token", {"contact_email": "buyer@example.com", "contact_phone": "+49 30 555 0205", "quantity": "2", "region": "Germany", "delivery": "8 weeks"}, "Sales handoff")

    def tearDown(self):
        self.db.conn.close()
        self.temp_dir.cleanup()

    def test_sales_update_persists_and_records_operator(self):
        self.db.update_lead("demo-tenant", self.rfq_id, "technical_review", "2026-10-01", "Ask engineer to confirm medium compatibility", "demo-sales")
        lead = self.db.get_lead_for_rfq("demo-tenant", self.rfq_id)
        self.assertEqual(lead["stage"], "technical_review")
        self.assertIsNone(lead["owner"])
        self.assertEqual(lead["next_follow_up"], "2026-10-01")
        self.assertIn("engineer", lead["note"])
        audit = self.db.list_entity_audit("demo-tenant", [lead["id"]])
        self.assertTrue(any(item["action"] == "updated" and item["actor"] == "demo-sales" for item in audit))

    def test_claim_and_assignment_keep_one_owner_across_conversation_rfqs(self):
        first = self.db.get_rfq("demo-tenant", self.rfq_id)
        other_rfq, _ = self.db.create_rfq("demo-tenant", first["conversation_id"], "second-purchase", {"contact_email": "buyer@example.com", "contact_phone": "+49 30 555 0206", "quantity": "1", "region": "Germany"}, "Another purchase")
        self.db.claim_conversation_leads("demo-tenant", self.rfq_id, "demo-sales@example.invalid")
        self.assertEqual(self.db.get_lead_for_rfq("demo-tenant", self.rfq_id)["owner"], "demo-sales@example.invalid")
        self.assertEqual(self.db.get_lead_for_rfq("demo-tenant", other_rfq)["owner"], "demo-sales@example.invalid")
        third_rfq, _ = self.db.create_rfq("demo-tenant", first["conversation_id"], "third-purchase", {"contact_email": "buyer@example.com", "contact_phone": "+49 30 555 0207", "quantity": "1", "region": "Germany"}, "Another model")
        self.assertEqual(self.db.get_lead_for_rfq("demo-tenant", third_rfq)["owner"], "demo-sales@example.invalid")
        with self.assertRaises(ValueError):
            self.db.claim_conversation_leads("demo-tenant", other_rfq, "demo-engineer@example.invalid")
        self.db.create_user("demo-tenant", "sales-two@example.invalid", "Second sales", "sales", "test-pass-123", "demo-admin@example.invalid")
        self.db.assign_conversation_leads("demo-tenant", self.rfq_id, "sales-two@example.invalid", "demo-admin@example.invalid")
        self.assertEqual({self.db.get_lead_for_rfq("demo-tenant", rfq_id)["owner"] for rfq_id in (self.rfq_id, other_rfq, third_rfq)}, {"sales-two@example.invalid"})
        with self.assertRaises(ValueError):
            self.db.assign_conversation_leads("demo-tenant", self.rfq_id, "demo-engineer@example.invalid", "demo-sales@example.invalid")

    def test_concurrent_claims_keep_one_conversation_owner(self):
        self.db.create_user("demo-tenant", "sales-two@example.invalid", "Second sales", "sales", "test-pass-123", "demo-admin@example.invalid")
        barrier = threading.Barrier(2)

        def claim(email):
            db = Database(self.db.path, initialize=False)
            try:
                barrier.wait(timeout=5)
                try:
                    db.claim_conversation_leads("demo-tenant", self.rfq_id, email)
                    return "claimed"
                except ValueError:
                    return "already_owned"
            finally:
                db.conn.close()

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(claim, ("demo-sales@example.invalid", "sales-two@example.invalid")))
        self.assertCountEqual(results, ["claimed", "already_owned"])
        self.assertIn(self.db.get_lead_for_rfq("demo-tenant", self.rfq_id)["owner"], ("demo-sales@example.invalid", "sales-two@example.invalid"))

    def test_stop_contact_preserves_rfq_and_clears_proactive_followup(self):
        rfq = self.db.get_rfq("demo-tenant", self.rfq_id)
        self.db.update_lead("demo-tenant", self.rfq_id, "technical_review", "2026-10-20", "Call buyer", "demo-sales@example.invalid")
        self.db.stop_proactive_contact("demo-tenant", rfq["conversation_id"], "Please do not contact me again.", "buyer_chat", "buyer")
        preference = self.db.get_contact_preference("demo-tenant", rfq["conversation_id"])
        self.assertEqual(preference["stop_proactive_contact"], 1)
        self.assertEqual(preference["stop_reason"], "Please do not contact me again.")
        self.assertIsNone(self.db.get_lead_for_rfq("demo-tenant", self.rfq_id)["next_follow_up"])
        self.assertIsNotNone(self.db.get_rfq("demo-tenant", self.rfq_id))
        with self.assertRaises(ValueError):
            self.db.update_lead("demo-tenant", self.rfq_id, "technical_review", "2026-10-21", "Call buyer", "demo-sales@example.invalid")

    def test_sales_records_are_tenant_scoped(self):
        self.db.ensure_tenant("other-tenant")
        self.assertEqual(len(self.db.list_sales_records("other-tenant")), 0)
        self.assertEqual(len(self.db.list_sales_records("demo-tenant")), 1)

    def test_admin_summary_counts_current_tenant_and_overdue_leads(self):
        self.db.update_lead("demo-tenant", self.rfq_id, "technical_review", "2020-01-01", "Follow up", "demo-sales")
        summary = self.db.get_admin_summary("demo-tenant")
        self.assertEqual(summary["rfqs_total"], 1)
        self.assertEqual(summary["leads_active"], 1)
        self.assertEqual(summary["leads_overdue"], 1)
        self.assertEqual(self.db.get_admin_summary("other-tenant")["rfqs_total"], 0)


class AuthTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.temp_dir.name) / "app.db")
        # 演示账号现在必须显式种入（普通新库不再预置任何账号）。
        self.db.seed_demo_users()

    def tearDown(self):
        self.db.conn.close()
        self.temp_dir.cleanup()

    def test_demo_roles_authenticate_and_session_keeps_tenant_context(self):
        admin = self.db.authenticate_user("demo-admin@example.invalid", "demo-admin")
        sales = self.db.authenticate_user("demo-sales@example.invalid", "demo-sales")
        engineer = self.db.authenticate_user("demo-engineer@example.invalid", "demo-engineer")
        self.assertEqual(admin["role"], "admin")
        self.assertEqual((sales["role"], engineer["role"]), ("employee", "employee"))
        self.assertEqual(set(self.db.get_user_modules(sales["id"])), {"rfqs", "sales"})
        self.assertEqual(set(self.db.get_user_modules(engineer["id"])), {"tasks", "rfqs", "sales"})
        self.assertIsNone(self.db.authenticate_user("demo-sales@example.invalid", "wrong-password"))
        session_id = self.db.create_session(sales)
        session_user = self.db.get_session_user(session_id)
        self.assertEqual(session_user["tenant_id"], "demo-tenant")
        self.assertEqual(session_user["email"], "demo-sales@example.invalid")
        self.db.delete_session(session_id)
        self.assertIsNone(self.db.get_session_user(session_id))

    def test_admin_can_create_and_deactivate_member_with_tenant_scope(self):
        user_id = self.db.create_user(
            "demo-tenant",
            "new-sales@example.invalid",
            "New Sales",
            "sales",
            "strong-pass",
            "demo-admin@example.invalid",
        )
        created = self.db.authenticate_user("new-sales@example.invalid", "strong-pass")
        self.assertEqual(created["id"], user_id)
        session_id = self.db.create_session(created)
        self.db.set_user_active("demo-tenant", user_id, False, "demo-admin@example.invalid")
        self.assertIsNone(self.db.authenticate_user("new-sales@example.invalid", "strong-pass"))
        self.assertIsNone(self.db.get_session_user(session_id))
        self.db.ensure_tenant("other-tenant")
        self.assertEqual(len(self.db.list_users("other-tenant")), 0)

    def test_member_password_and_role_are_validated(self):
        with self.assertRaises(ValueError):
            self.db.create_user("demo-tenant", "short@example.invalid", "Short", "sales", "short", "demo-admin")
        with self.assertRaises(ValueError):
            self.db.create_user("demo-tenant", "bad-role@example.invalid", "Bad role", "buyer", "long-pass", "demo-admin")


class DatabaseBootstrapTests(unittest.TestCase):
    """普通新库不得预置演示租户/账号；演示与首位管理员都必须显式创建。"""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "fresh.db"

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_fresh_database_has_no_demo_tenant_or_predictable_users(self):
        db = Database(self.db_path)
        try:
            tenants = db.conn.execute("SELECT COUNT(*) FROM tenants").fetchone()[0]
            users = db.conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
            self.assertEqual((tenants, users), (0, 0))
            self.assertIsNone(db.authenticate_user("demo-admin@example.invalid", "demo-admin"))
            self.assertIsNone(db.authenticate_user("demo-sales@example.invalid", "demo-sales"))
        finally:
            db.conn.close()

    def test_explicit_demo_bootstrap_keeps_documented_logins(self):
        db = Database(self.db_path)
        try:
            db.seed_demo_users()
            admin = db.authenticate_user("demo-admin@example.invalid", "demo-admin")
            sales = db.authenticate_user("demo-sales@example.invalid", "demo-sales")
            engineer = db.authenticate_user("demo-engineer@example.invalid", "demo-engineer")
            self.assertEqual((admin["role"], sales["role"], engineer["role"]), ("admin", "employee", "employee"))
            self.assertEqual(set(db.get_user_modules(sales["id"])), {"rfqs", "sales"})
            self.assertEqual(admin["tenant_id"], "demo-tenant")
            db.seed_demo_users()  # 幂等：重复显式初始化不产生重复账号
            self.assertEqual(db.conn.execute("SELECT COUNT(*) FROM users").fetchone()[0], 3)
        finally:
            db.conn.close()

    def test_create_admin_cli_provisions_first_admin_without_default_password(self):
        root = Path(__file__).resolve().parent
        result = subprocess.run(
            [
                sys.executable, str(root / "tools" / "create_admin.py"),
                "--db", str(self.db_path), "--email", "first-admin@example.invalid",
                "--name", "首位管理员", "--password-stdin",
            ],
            input="first-admin-pass-1\n", capture_output=True, text=True, cwd=str(root), timeout=120,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        db = Database(self.db_path)
        try:
            user = db.authenticate_user("first-admin@example.invalid", "first-admin-pass-1")
            self.assertIsNotNone(user)
            self.assertEqual((user["role"], user["tenant_id"]), ("admin", "default"))
            # 只创建了这一个账号，且没有演示/默认口令账号
            self.assertEqual(db.conn.execute("SELECT COUNT(*) FROM users").fetchone()[0], 1)
            self.assertIsNone(db.authenticate_user("demo-admin@example.invalid", "demo-admin"))
        finally:
            db.conn.close()


class BrandingTests(unittest.TestCase):
    def test_layout_uses_growave_brand(self):
        page = layout("管理概览", "<p>content</p>")
        self.assertIn("Growave", page)
        self.assertNotIn("CB Water Pump SaaS", page)


if __name__ == "__main__":
    unittest.main()
