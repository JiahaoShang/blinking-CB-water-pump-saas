"""企业知识第一批 · 任务1：真实存储、生命周期与权限（临时 SQLite）。

对应用户已授权的技术方案与开发计划
（docs/superpowers/plans/2026-10-10-enterprise-knowledge-phase1.md 任务1）：
先写有意义的失败测试、再最小实现。覆盖：
- 跨企业不可见；无 products（产品管理）权限拒绝写但可读；管理员与获授权成员可写；
- 已发布条目保存草稿不改变线上版本；历史恢复创建新草稿、不覆盖历史；
- 停用 / 关闭 AI 授权 / 到期在检索前即时排除（内部资料永不进入客户视角）；
- 发布新版后旧版本不得继续用于这条回答；「准备完成」由真实处理状态绑定确切版本，不靠计时模拟；
- 重开数据库后内容与历史仍在。

只用临时数据库；不触碰 data/ 下的真实演示数据与任何密钥。
"""
import json
import tempfile
import unittest
from pathlib import Path

from app import Database

TENANT = "demo-tenant"
OTHER = "other-tenant"


class EnterpriseKnowledgeTask1Tests(unittest.TestCase):
    maxDiff = None

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "knowledge.db"
        self.db = Database(self.db_path)
        self.db.seed_demo_users()
        self.admin = self.db.authenticate_user("demo-admin@example.invalid", "demo-admin")
        self.member = self.db.authenticate_user("demo-sales@example.invalid", "demo-sales")  # 无 products 权限
        self.db.ensure_tenant(OTHER, "其他企业（测试）")
        self.other_admin_id = self.db.create_user(
            OTHER, "other-admin@example.invalid", "对方管理员", "admin", "other-admin", "test-setup")

    def tearDown(self):
        self.db.conn.close()
        self.tmp.cleanup()

    # ── 帮助方法 ──────────────────────────────────────────────────────────

    def publish_article(self, *, usage="external", ai_authorized=True, expires_at=None,
                        title="整机保修与售后响应政策", body="整机保修 24 个月。", confirm=True, user=None):
        user = user or self.admin
        item_id = self.db.create_knowledge_item(
            TENANT, "article", title, user["id"],
            content={"body": body}, usage=usage, ai_authorized=ai_authorized, expires_at=expires_at,
        )
        self.db.publish_knowledge_draft(TENANT, item_id, user["id"], confirm_ai=confirm)
        return item_id

    def mark_completed(self, item_id):
        version = self.db.get_published_knowledge_version(TENANT, item_id)
        self.db.set_knowledge_processing_result(TENANT, version["id"], "completed")
        return version

    def answerable_version_ids(self, item_id):
        return [row["version_id"] for row in self.db.list_ai_answerable_knowledge(TENANT, item_id)]

    # ── 测试 ──────────────────────────────────────────────────────────────

    def test_cross_tenant_isolation(self):
        """跨企业不可见；其他企业管理员不能读写本企业知识；各自企业内互不影响。"""
        item_id = self.publish_article()
        version = self.db.get_published_knowledge_version(TENANT, item_id)
        other_item = self.db.create_knowledge_item(
            OTHER, "article", "对方企业文章", self.other_admin_id, content={"body": "自家内容"})
        # 其他企业读不到本企业的知识。
        self.assertEqual([row["id"] for row in self.db.list_knowledge_items(OTHER)], [other_item])
        self.assertIsNone(self.db.get_knowledge_item(OTHER, item_id))
        self.assertEqual(self.db.list_knowledge_versions(OTHER, item_id), [])
        self.assertIsNone(self.db.get_knowledge_version(OTHER, version["id"]))
        self.assertEqual(self.db.list_ai_answerable_knowledge(OTHER), [])
        # 本企业读不到其他企业的知识。
        self.assertIsNone(self.db.get_knowledge_item(TENANT, other_item))
        # 其他企业管理员对本企业没有任何写入口。
        with self.assertRaises(PermissionError):
            self.db.create_knowledge_item(TENANT, "article", "越权文章", self.other_admin_id, content={"body": "x"})
        with self.assertRaises(PermissionError):
            self.db.publish_knowledge_draft(TENANT, item_id, self.other_admin_id)
        self.assertEqual([row["id"] for row in self.db.list_knowledge_items(TENANT)], [item_id])

    def test_member_without_products_cannot_write_but_can_read(self):
        """本企业成员默认可读；写操作必须由管理员或获 products 模块授权的成员执行（后端强制）。"""
        item_id = self.publish_article()
        # 可读。
        self.assertEqual([row["id"] for row in self.db.list_knowledge_items(TENANT)], [item_id])
        self.assertTrue(self.db.list_knowledge_versions(TENANT, item_id))
        # 拒绝写：创建 / 保存草稿 / 发布。
        with self.assertRaises(PermissionError):
            self.db.create_knowledge_item(TENANT, "article", "员工新建", self.member["id"], content={"body": "x"})
        with self.assertRaises(PermissionError):
            self.db.save_knowledge_draft(TENANT, item_id, self.member["id"], content={"body": "员工修改"})
        with self.assertRaises(PermissionError):
            self.db.publish_knowledge_draft(TENANT, item_id, self.member["id"], confirm_ai=True)
        # 授权 products 模块后即可写（P2 已确认：沿用产品管理权限）。
        self.db.set_user_modules(TENANT, self.member["id"], ["products"], self.admin["email"])
        new_id = self.db.create_knowledge_item(
            TENANT, "faq", "发货 FAQ", self.member["id"],
            content={"faq": [{"question": "几天到货？", "answer": "一般 3-5 天。"}]})
        self.assertIsNotNone(self.db.get_knowledge_item(TENANT, new_id))
        # 撤销授权立即生效：再次拒绝写。
        self.db.set_user_modules(TENANT, self.member["id"], [], self.admin["email"])
        with self.assertRaises(PermissionError):
            self.db.save_knowledge_draft(TENANT, item_id, self.member["id"], content={"body": "撤权后修改"})

    def test_saved_draft_keeps_published_version_online(self):
        """编辑已发布条目：保存草稿不改变线上版本，草稿只作待发布修改。"""
        item_id = self.publish_article(body="整机保修 24 个月。")
        v1 = self.mark_completed(item_id)
        self.db.save_knowledge_draft(TENANT, item_id, self.admin["id"], content={"body": "整机保修 36 个月。"})
        item = self.db.get_knowledge_item(TENANT, item_id)
        self.assertEqual(item["status"], "published")
        self.assertEqual(item["published_version_id"], v1["id"])
        self.assertIsNotNone(item["draft_version_id"])
        v1_after = self.db.get_knowledge_version(TENANT, v1["id"])
        self.assertEqual(v1_after["status"], "published")
        self.assertEqual(json.loads(v1_after["content_json"])["body"], "整机保修 24 个月。")
        draft = self.db.get_knowledge_draft_version(TENANT, item_id)
        self.assertEqual(json.loads(draft["content_json"])["body"], "整机保修 36 个月。")
        # 草稿期间客户视角仍绑定线上 v1（发布前 v1 仍可用）。
        self.assertEqual(self.answerable_version_ids(item_id), [v1["id"]])

    def test_restore_history_creates_new_draft(self):
        """恢复历史版本创建新草稿（注明恢复自 vX），不覆盖历史、不影响线上。"""
        item_id = self.publish_article(body="保修 24 个月。")
        v1 = self.mark_completed(item_id)
        self.db.save_knowledge_draft(TENANT, item_id, self.admin["id"], content={"body": "保修 36 个月。"})
        self.db.publish_knowledge_draft(TENANT, item_id, self.admin["id"], confirm_ai=True)
        v2 = self.mark_completed(item_id)
        self.assertEqual(self.answerable_version_ids(item_id), [v2["id"]])
        self.db.restore_knowledge_version(TENANT, item_id, 1, self.admin["id"])
        item = self.db.get_knowledge_item(TENANT, item_id)
        self.assertEqual(item["published_version_id"], v2["id"])
        draft = self.db.get_knowledge_draft_version(TENANT, item_id)
        self.assertEqual(draft["version"], 3)
        self.assertEqual(draft["restored_from_version"], 1)
        self.assertEqual(json.loads(draft["content_json"])["body"], "保修 24 个月。")
        self.assertEqual(self.answerable_version_ids(item_id), [v2["id"]])
        self.assertEqual([row["version"] for row in self.db.list_knowledge_versions(TENANT, item_id)], [3, 2, 1])
        self.assertEqual(json.loads(self.db.get_knowledge_version(TENANT, v1["id"])["content_json"])["body"], "保修 24 个月。")
        # 已有未发布草稿时再次恢复会被拒绝（不弄丢现有草稿）。
        with self.assertRaises(ValueError):
            self.db.restore_knowledge_version(TENANT, item_id, 1, self.admin["id"])
        # 发布恢复出的草稿：v3 上线，v2 转入历史但仍可查。
        self.db.publish_knowledge_draft(TENANT, item_id, self.admin["id"], confirm_ai=True)
        self.assertEqual(self.db.get_published_knowledge_version(TENANT, item_id)["version"], 3)
        self.assertEqual(self.db.get_knowledge_version(TENANT, v2["id"])["status"], "superseded")

    def test_publish_rebinds_to_new_version_after_real_processing(self):
        """发布新版后旧版本不再用于这条回答；「准备完成」来自真实处理状态，不靠计时模拟。"""
        item_id = self.publish_article(body="保修 24 个月。")
        v1 = self.db.get_published_knowledge_version(TENANT, item_id)
        # 新版本处理未完成前整条不可用（准备中），且不得退回使用旧版本。
        self.assertEqual(self.answerable_version_ids(item_id), [])
        self.db.set_knowledge_processing_result(TENANT, v1["id"], "completed")
        self.assertEqual(self.answerable_version_ids(item_id), [v1["id"]])
        # 发布 v2：v1 立即不再用于回答；绑定新版必须等真实处理完成。
        self.db.save_knowledge_draft(TENANT, item_id, self.admin["id"], content={"body": "保修 36 个月。"})
        self.db.publish_knowledge_draft(TENANT, item_id, self.admin["id"], confirm_ai=True)
        v2 = self.db.get_published_knowledge_version(TENANT, item_id)
        self.assertNotEqual(v2["id"], v1["id"])
        self.assertEqual(self.answerable_version_ids(item_id), [])
        self.db.set_knowledge_processing_result(TENANT, v2["id"], "completed")
        rows = self.db.list_ai_answerable_knowledge(TENANT, item_id)
        self.assertEqual([row["version_id"] for row in rows], [v2["id"]])
        self.assertEqual(json.loads(rows[0]["content_json"])["body"], "保修 36 个月。")
        # 处理失败不算完成，记录错误；重试（计数）后才能重新可用。
        self.db.save_knowledge_draft(TENANT, item_id, self.admin["id"], content={"body": "保修 48 个月。"})
        self.db.publish_knowledge_draft(TENANT, item_id, self.admin["id"], confirm_ai=True)
        v3 = self.db.get_published_knowledge_version(TENANT, item_id)
        self.db.set_knowledge_processing_result(TENANT, v3["id"], "failed", "索引构建失败")
        state = self.db.knowledge_processing_state(TENANT, v3["id"])
        self.assertEqual((state["status"], state["error"]), ("failed", "索引构建失败"))
        self.assertEqual(self.answerable_version_ids(item_id), [])
        self.db.retry_knowledge_processing(TENANT, v3["id"], self.admin["id"])
        state = self.db.knowledge_processing_state(TENANT, v3["id"])
        self.assertEqual((state["status"], state["attempts"], state["error"]), ("pending", 1, ""))
        self.assertEqual(self.answerable_version_ids(item_id), [])
        self.db.set_knowledge_processing_result(TENANT, v3["id"], "completed")
        self.assertEqual(self.answerable_version_ids(item_id), [v3["id"]])
        # 未失败的处理不能重试。
        with self.assertRaises(ValueError):
            self.db.retry_knowledge_processing(TENANT, v3["id"], self.admin["id"])

    def test_disable_and_authorization_take_effect_immediately(self):
        """停用 / 关闭授权在检索前即时排除；重新启用 / 重新授权即时恢复。"""
        item_id = self.publish_article()
        v1 = self.mark_completed(item_id)
        self.assertEqual(self.answerable_version_ids(item_id), [v1["id"]])
        # 停用必须确认；确认后立即排除，历史与版本保留。
        with self.assertRaises(ValueError):
            self.db.disable_knowledge_item(TENANT, item_id, self.admin["id"], confirmed=False)
        self.db.disable_knowledge_item(TENANT, item_id, self.admin["id"], confirmed=True)
        self.assertEqual(self.answerable_version_ids(item_id), [])
        self.assertEqual(self.db.get_knowledge_item(TENANT, item_id)["status"], "disabled")
        self.assertEqual(self.db.get_knowledge_version(TENANT, v1["id"])["status"], "published")
        self.assertTrue(self.db.list_knowledge_versions(TENANT, item_id))
        # 重新启用（对外+授权需确认）后立即恢复可用。
        with self.assertRaises(ValueError):
            self.db.enable_knowledge_item(TENANT, item_id, self.admin["id"], confirmed=False)
        self.db.enable_knowledge_item(TENANT, item_id, self.admin["id"], confirmed=True)
        self.assertEqual(self.answerable_version_ids(item_id), [v1["id"]])
        # 未发布过的条目不能停用。
        draft_only = self.db.create_knowledge_item(TENANT, "article", "未发布条目", self.admin["id"], content={"body": "x"})
        with self.assertRaises(ValueError):
            self.db.disable_knowledge_item(TENANT, draft_only, self.admin["id"], confirmed=True)
        # 关闭授权立即停止使用（无需确认）；重新开启（对外+已发布）需要确认。
        self.db.set_knowledge_authorization(TENANT, item_id, self.admin["id"], False)
        self.assertEqual(self.answerable_version_ids(item_id), [])
        with self.assertRaises(ValueError):
            self.db.set_knowledge_authorization(TENANT, item_id, self.admin["id"], True)
        self.db.set_knowledge_authorization(TENANT, item_id, self.admin["id"], True, confirmed=True)
        self.assertEqual(self.answerable_version_ids(item_id), [v1["id"]])

    def test_expired_and_internal_excluded_from_answer(self):
        """到期与内部资料在检索前排除；员工阅读与生命周期不受影响。"""
        # 内部资料：即使已发布、已授权、处理完成也不进入客户视角（管理员测试同样遵守）。
        internal_id = self.publish_article(usage="internal", title="内部培训材料")
        self.mark_completed(internal_id)
        self.assertEqual(self.answerable_version_ids(internal_id), [])
        self.assertIsNotNone(self.db.get_knowledge_item(TENANT, internal_id))
        # 到期：检索前排除；条目仍是已发布（不因到期删除或另建状态）。
        expired_id = self.publish_article(expires_at="2020-01-01", title="秋季展会报价政策")
        self.mark_completed(expired_id)
        self.assertEqual(self.answerable_version_ids(expired_id), [])
        self.assertEqual(self.db.get_knowledge_item(TENANT, expired_id)["status"], "published")
        # 未来到期：正常可用。
        future_id = self.publish_article(expires_at="2099-12-31", title="长期有效政策")
        future_version = self.mark_completed(future_id)
        self.assertEqual(self.answerable_version_ids(future_id), [future_version["id"]])

    def test_external_ai_publish_requires_confirmation(self):
        """对外且授权的发布需要一次确认；未确认时保持草稿、线上不受影响。"""
        item_id = self.db.create_knowledge_item(
            TENANT, "article", "对外政策", self.admin["id"], content={"body": "内容"},
            usage="external", ai_authorized=True)
        with self.assertRaises(ValueError):
            self.db.publish_knowledge_draft(TENANT, item_id, self.admin["id"], confirm_ai=False)
        self.assertEqual(self.db.get_knowledge_item(TENANT, item_id)["status"], "draft")
        self.db.publish_knowledge_draft(TENANT, item_id, self.admin["id"], confirm_ai=True)
        self.assertEqual(self.db.get_knowledge_item(TENANT, item_id)["status"], "published")

    def test_create_and_publish_validation(self):
        """形态 / 标题 / 使用范围 / 到期格式 / 正文必填的最小校验。"""
        with self.assertRaises(ValueError):
            self.db.create_knowledge_item(TENANT, "poster", "海报", self.admin["id"], content={})
        with self.assertRaises(ValueError):
            self.db.create_knowledge_item(TENANT, "article", "   ", self.admin["id"], content={})
        with self.assertRaises(ValueError):
            self.db.create_knowledge_item(TENANT, "article", "政策", self.admin["id"], content={}, usage="public")
        with self.assertRaises(ValueError):
            self.db.create_knowledge_item(TENANT, "article", "政策", self.admin["id"], content={}, expires_at="2026/01/01")
        empty_url = self.db.create_knowledge_item(TENANT, "article", "空正文", self.admin["id"], content={"body": ""})
        with self.assertRaises(ValueError):
            self.db.publish_knowledge_draft(TENANT, empty_url, self.admin["id"])
        with self.assertRaises(ValueError):
            self.db.save_knowledge_draft(TENANT, empty_url, self.admin["id"], content={"title": " "})
        faq_id = self.db.create_knowledge_item(
            TENANT, "faq", "发货 FAQ", self.admin["id"],
            content={"faq": [{"question": "几天到货？", "answer": ""}]})
        with self.assertRaises(ValueError):
            self.db.publish_knowledge_draft(TENANT, faq_id, self.admin["id"])

    def test_reopen_keeps_content_and_history(self):
        """重开同一数据库：初始化幂等，内容、历史、来源、处理状态与检索绑定全部保留。"""
        item_id = self.publish_article(body="保修 24 个月。")
        v1 = self.mark_completed(item_id)
        source_id = self.db.create_knowledge_source(TENANT, item_id, self.admin["id"], source_location="产品手册 Rev.B 第 3 页")
        self.db.save_knowledge_draft(TENANT, item_id, self.admin["id"], content={"body": "保修 36 个月。"}, source_id=source_id)
        self.db.publish_knowledge_draft(TENANT, item_id, self.admin["id"], confirm_ai=True)
        v2 = self.mark_completed(item_id)
        self.db.conn.close()
        self.db = Database(self.db_path)
        item = self.db.get_knowledge_item(TENANT, item_id)
        self.assertEqual((item["status"], item["published_version_id"]), ("published", v2["id"]))
        history = self.db.list_knowledge_versions(TENANT, item_id)
        self.assertEqual([(row["version"], row["status"]) for row in history], [(2, "published"), (1, "superseded")])
        self.assertEqual(json.loads(self.db.get_knowledge_version(TENANT, v1["id"])["content_json"])["body"], "保修 24 个月。")
        self.assertEqual(self.db.get_knowledge_version(TENANT, v2["id"])["source_id"], source_id)
        self.assertTrue(self.db.list_knowledge_sources(TENANT, item_id))
        self.assertEqual(self.db.knowledge_processing_state(TENANT, v2["id"])["status"], "completed")
        self.assertEqual(self.answerable_version_ids(item_id), [v2["id"]])
        audit_count = self.db.conn.execute(
            "SELECT COUNT(*) FROM audit_log WHERE tenant_id=? AND entity_type='knowledge'", (TENANT,)).fetchone()[0]
        self.assertGreaterEqual(audit_count, 4)


if __name__ == "__main__":
    unittest.main()
