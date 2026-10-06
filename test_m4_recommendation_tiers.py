import tempfile
import unittest
from pathlib import Path

from app import (
    Database,
    buyer_visible_results,
    classify_products,
    confirm_requirement_values,
    match_products,
    overall_status,
    recommendation_flags,
)


def publish_product(db, model, fields, use_case="联调场景"):
    product_id = db.create_product("demo-tenant", model, f"{model} 测试泵", use_case)
    source_id = db.add_source("demo-tenant", product_id, f"{model}-manual.txt", "TXT", "第 1 节：参数表")
    for key, (value, unit) in fields.items():
        db.save_field("demo-tenant", product_id, key, value, unit, source_id, "第 1 节：参数表")
    for key in fields:
        db.approve_field("demo-tenant", product_id, key)
    db.finalize_product_review("demo-tenant", product_id)
    db.publish_product("demo-tenant", product_id)
    return product_id


def confirmed(**values):
    return confirm_requirement_values({
        key: {"value": value, "unit": unit, "label": key}
        for key, (value, unit) in values.items()
    })


class RecommendationTierTests(unittest.TestCase):
    """M4: classify models into candidate / tentative / conflict / no-basis tiers."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.temp_dir.name) / "app.db")
        # 测试夹具企业需显式创建（普通新库不再预置）。
        self.db.ensure_tenant("demo-tenant", "示例水泵企业（演示）")
        publish_product(self.db, "RNG-A", {
            "flow_range": ("30-90", "m³/h"),
            "head_range": ("24-58", "m"),
            "media": ("清水与低粘度工业循环水", ""),
        })
        publish_product(self.db, "CURVE-B", {
            "flow_range": ("0-4.4", "m³/h"),
            "head_range": ("27.5-60", "m"),
            "performance_points": ("0:60;3:48.1;4.4:27.5", ""),
            "media": ("清水", ""),
        })
        publish_product(self.db, "NOBASE-C", {
            "material": ("铸铁泵体，不锈钢叶轮", ""),
        })
        publish_product(self.db, "OVER-D", {
            "flow_range": ("1-2", "m³/h"),
            "head_range": ("10-20", "m"),
            "media": ("清水", ""),
        })

    def tearDown(self):
        self.db.conn.close()
        self.temp_dir.cleanup()

    def tiers(self, values):
        results, missing = classify_products(self.db, "demo-tenant", values)
        return {item["model"]: item for item in results}, missing

    def test_four_tier_classification_for_in_range_requirements(self):
        results, missing = self.tiers(confirmed(flow=("50", "m³/h"), head=("40", "m"), media=("清水", "")))
        self.assertEqual(missing, [])
        self.assertEqual(results["RNG-A"]["status"], "candidate")
        self.assertIn("流量", "".join(results["RNG-A"]["satisfies"]))
        self.assertEqual(results["CURVE-B"]["status"], "conflict")  # flow beyond catalog curve
        self.assertEqual(results["OVER-D"]["status"], "conflict")
        self.assertEqual(results["NOBASE-C"]["status"], "no_basis")
        self.assertEqual(overall_status(list(results.values()), []), "recommended")
        # 无相关性依据与硬冲突都不进入买方可见候选（含待核实视图）
        status, public_results, _ = match_products(self.db, "demo-tenant", confirmed(flow=("50", "m³/h"), head=("40", "m"), media=("清水", "")))
        self.assertEqual(status, "recommended")
        visible = buyer_visible_results(public_results, False)
        self.assertEqual([item["model"] for item in visible], ["RNG-A"])
        visible_tentative = buyer_visible_results(public_results, True)
        self.assertNotIn("NOBASE-C", [item["model"] for item in visible_tentative])
        self.assertNotIn("OVER-D", [item["model"] for item in visible_tentative])

    def test_curve_model_stays_tentative_and_never_candidate(self):
        values = confirmed(flow=("3", "m³/h"), head=("40", "m"), media=("清水", ""))
        results, _ = self.tiers(values)
        self.assertEqual(results["CURVE-B"]["status"], "needs_confirmation")
        self.assertIn("工程师", "".join(results["CURVE-B"]["unknown"]))
        self.assertEqual(results["RNG-A"]["status"], "conflict")
        self.assertEqual(overall_status(list(results.values()), []), "needs_confirmation")
        # 待核实候选默认不返回给买方，需明确请求后才可见
        _, full_results, _ = match_products(self.db, "demo-tenant", values)
        self.assertEqual([item["model"] for item in buyer_visible_results(full_results, False)], [])
        self.assertEqual([item["model"] for item in buyer_visible_results(full_results, True)], ["CURVE-B"])

    def test_insufficient_conditions_only_explore_when_requested(self):
        values = confirmed(flow=("50", "m³/h"))
        status, results, missing = match_products(self.db, "demo-tenant", values)
        self.assertEqual(status, "insufficient")
        self.assertEqual(results, [])
        self.assertIn("扬程", missing)
        self.assertIn("介质", missing)
        status, explore, missing = match_products(self.db, "demo-tenant", values, include_tentative=True)
        self.assertEqual(status, "insufficient")
        models = [item["model"] for item in explore]
        self.assertIn("RNG-A", models)
        self.assertNotIn("NOBASE-C", models)   # 无相关性依据
        self.assertNotIn("OVER-D", models)     # 已知硬冲突
        rng = next(item for item in explore if item["model"] == "RNG-A")
        unknown_text = "".join(rng["unknown"])
        self.assertIn("买方尚未确认扬程", unknown_text)
        self.assertIn("买方尚未确认介质", unknown_text)
        self.assertTrue(all(item["status"] == "needs_confirmation" for item in explore))
        flags = recommendation_flags([{"status": "needs_confirmation"}], "insufficient")
        self.assertTrue(flags["tentative_available"])
        self.assertTrue(flags["options"]["engineer_help"])

    def test_classification_is_deterministic(self):
        values = confirmed(flow=("50", "m³/h"), head=("40", "m"), media=("清水", ""))
        first, _ = classify_products(self.db, "demo-tenant", values)
        second, _ = classify_products(self.db, "demo-tenant", values)
        self.assertEqual(
            [(item["model"], item["status"]) for item in first],
            [(item["model"], item["status"]) for item in second],
        )
        # 每个层级内部按型号排序，层级顺序保持稳定
        for status in {item["status"] for item in first}:
            models = [item["model"] for item in first if item["status"] == status]
            self.assertEqual(models, sorted(models))
        tier_order = {"candidate": 0, "needs_confirmation": 1, "conflict": 2, "no_basis": 3}
        statuses = [tier_order[item["status"]] for item in first]
        self.assertEqual(statuses, sorted(statuses))


if __name__ == "__main__":
    unittest.main()
