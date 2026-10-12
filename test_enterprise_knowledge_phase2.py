"""企业知识第二批 · 任务1：主题、关系、版本与资料索引（临时 SQLite）。

对应用户已授权技术计划
（docs/superpowers/plans/2026-10-12-enterprise-knowledge-phase2.md 任务1）：
先写有意义的失败测试、再最小实现。覆盖：
- 跨企业不可见、跨企业写与跨企业资料关联拒绝；无 products 权限拒写、可读；
- 同一资料关联多个主题（多对多）、重复关联幂等、取消关联幂等；
- 旧产品 / 文章 / FAQ 全部保留在「全部资料」，不自动分配主题；
- 主题草稿 / 发布 / 历史：保存草稿不换线上；发布需全部已核对（未核对段落拦截）；
- 逐段来源钉住确切版本：资料发布新版后旧主题出处不被静默换版（标记换取版）；
- 分组删除只解除分组；重开数据库后主题 / 分组 / 关联 / 正文仍在。

只用临时数据库；不触碰 data/ 下的真实演示数据与任何密钥。
"""
import json
import tempfile
import unittest
from pathlib import Path

from app import Database

TENANT = "demo-tenant"
OTHER = "other-tenant"


class KnowledgePhase2Task1Tests(unittest.TestCase):
    maxDiff = None

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "knowledge2.db"
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

    def publish_article(self, title="整机保修与售后响应政策", body="整机保修 24 个月。"):
        item_id = self.db.create_knowledge_item(
            TENANT, "article", title, self.admin["id"], content={"body": body})
        # B2-B03 起资料摘录必须钉住登记原件：夹具先登记一个手工来源并绑定草稿，再发布。
        source_id = self.db.create_knowledge_source(
            TENANT, item_id, self.admin["id"], kind="manual", source_location="测试手工登记来源")
        self.db.save_knowledge_draft(TENANT, item_id, self.admin["id"], source_id=source_id)
        self.db.publish_knowledge_draft(TENANT, item_id, self.admin["id"])
        return item_id

    def reopen(self):
        self.db.conn.close()
        self.db = Database(self.db_path)
        return self.db

    def article_source_id(self, item_id):
        version = self.db.get_published_knowledge_version(TENANT, item_id)
        return version["source_id"]

    def topic_paragraph(self, item_id, version_id, text, *, checked=True, source_id=None, location=None):
        return {
            "kind": "intro", "text": text, "origin": "from_material", "checked": checked,
            "source": {"kind": "knowledge", "item_id": item_id, "version_id": version_id,
                       "source_id": source_id or self.article_source_id(item_id),
                       "location": location or {"kind": "manual"}},
        }

    # ── 测试 ──────────────────────────────────────────────────────────────

    def test_topic_entity_tenant_isolation(self):
        """跨企业不可见；其他企业管理员不能读写本企业主题，也不能拿本企业资料建关联。"""
        topic_id = self.db.kb2_create_topic(TENANT, self.admin["id"], "智能会议设备A")
        self.assertEqual([t["id"] for t in self.db.kb2_list_topics(TENANT)], [topic_id])
        self.assertEqual(self.db.kb2_list_topics(OTHER), [])
        self.assertIsNone(self.db.kb2_get_topic(OTHER, topic_id))
        item_id = self.publish_article()
        # 对方企业里既没有这个主题也没有这条资料：关联请求拒绝（不允许跨企业读写）。
        with self.assertRaises(ValueError):
            self.db.kb2_set_relation(OTHER, self.other_admin_id, topic_id, {"kind": "knowledge", "id": item_id})
        with self.assertRaises(PermissionError):
            self.db.kb2_create_topic(TENANT, self.other_admin_id, "越权主题")
        with self.assertRaises(PermissionError):
            self.db.kb2_create_group(TENANT, self.other_admin_id, "越权分组")

    def test_cross_tenant_material_refused(self):
        """本企业主题不能关联其他企业的资料；不复制对方资料行。"""
        self.db.ensure_tenant(TENANT, "示例水泵企业（演示）")
        other_item = self.db.create_knowledge_item(
            OTHER, "article", "对方文章", self.other_admin_id, content={"body": "对方正文"})
        topic_id = self.db.kb2_create_topic(TENANT, self.admin["id"], "本企业主题")
        with self.assertRaises(ValueError):
            self.db.kb2_set_relation(TENANT, self.admin["id"], topic_id, {"kind": "knowledge", "id": other_item})
        with self.assertRaises(ValueError):
            self.db.kb2_set_relation(TENANT, self.admin["id"], topic_id, {"kind": "product", "id": "no-such-product"})
        self.assertEqual(self.db.kb2_list_relations(TENANT, topic_id), [])
        # 对方企业的资料行仍是它自己的企业归属。
        row = self.db.conn.execute(
            "SELECT tenant_id FROM knowledge_items WHERE id=?", (other_item,)).fetchone()
        self.assertEqual(row["tenant_id"], OTHER)

    def test_write_requires_products_permission_but_read_allowed(self):
        """本企业成员可读；写必须管理员或 products 模块授权；撤权立即生效。"""
        item_id = self.publish_article()
        with self.assertRaises(PermissionError):
            self.db.kb2_create_topic(TENANT, self.member["id"], "员工建主题")
        with self.assertRaises(PermissionError):
            self.db.kb2_create_group(TENANT, self.member["id"], "员工分组")
        # 授权后可以写。
        self.db.set_user_modules(TENANT, self.member["id"], ["products"], self.admin["email"])
        topic_id = self.db.kb2_create_topic(TENANT, self.member["id"], "员工授权主题")
        self.db.kb2_set_relation(TENANT, self.member["id"], topic_id, {"kind": "knowledge", "id": item_id})
        self.assertEqual(len(self.db.kb2_list_relations(TENANT, topic_id)), 1)
        # 撤权立即生效：删除关联被拒绝，关联保持原样；只读仍可看。
        self.db.set_user_modules(TENANT, self.member["id"], [], self.admin["email"])
        with self.assertRaises(PermissionError):
            self.db.kb2_set_relation(TENANT, self.member["id"], topic_id, {"kind": "knowledge", "id": item_id}, linked=False)
        self.assertEqual(len(self.db.kb2_list_relations(TENANT, topic_id)), 1)
        self.assertTrue(self.db.kb2_list_topics(TENANT))
        self.assertTrue(self.db.kb2_list_relations(TENANT, topic_id))

    def test_relation_idempotent_and_multi_topic(self):
        """一份资料可关联多个主题；重复关联 / 重复取消均为幂等；无效引用拒绝。"""
        item_id = self.publish_article()
        t1 = self.db.kb2_create_topic(TENANT, self.admin["id"], "主题一")
        t2 = self.db.kb2_create_topic(TENANT, self.admin["id"], "主题二")
        ref = {"kind": "knowledge", "id": item_id}
        self.db.kb2_set_relation(TENANT, self.admin["id"], t1, ref)
        self.db.kb2_set_relation(TENANT, self.admin["id"], t1, ref)  # 幂等：不重复建
        self.db.kb2_set_relation(TENANT, self.admin["id"], t2, ref)  # 同一资料第二个主题
        self.assertEqual(len(self.db.kb2_list_relations(TENANT, t1)), 1)
        self.assertEqual(len(self.db.kb2_list_relations(TENANT, t2)), 1)
        count = self.db.conn.execute("SELECT COUNT(*) FROM kb2_topic_relations").fetchone()[0]
        self.assertEqual(count, 2)
        self.db.kb2_set_relation(TENANT, self.admin["id"], t2, ref, linked=False)
        self.db.kb2_set_relation(TENANT, self.admin["id"], t2, ref, linked=False)  # 幂等取消
        self.assertEqual(self.db.kb2_list_relations(TENANT, t2), [])
        with self.assertRaises(ValueError):
            self.db.kb2_set_relation(TENANT, self.admin["id"], t1, {"kind": "knowledge", "id": "missing-item"})
        with self.assertRaises(ValueError):
            self.db.kb2_set_relation(TENANT, self.admin["id"], t1, {"kind": "unknown", "id": item_id})
        with self.assertRaises(ValueError):
            self.db.kb2_set_relation(TENANT, self.admin["id"], "missing-topic", ref)

    def test_relation_pins_version_snapshot(self):
        """关联时钉住资料的确切版本；产品只读适配、不复制产品行。"""
        item_id = self.publish_article(body="保修 24 个月。")
        version = self.db.get_published_knowledge_version(TENANT, item_id)
        product_id = self.db.create_product(TENANT, "KB2-P1", "测试泵", "冷却水循环")
        topic_id = self.db.kb2_create_topic(TENANT, self.admin["id"], "选型与保修")
        self.db.kb2_set_relation(TENANT, self.admin["id"], topic_id, {"kind": "knowledge", "id": item_id})
        self.db.kb2_set_relation(TENANT, self.admin["id"], topic_id, {"kind": "product", "id": product_id})
        relations = {r["material_kind"]: r for r in self.db.kb2_list_relations(TENANT, topic_id)}
        self.assertEqual(relations["knowledge"]["pinned_version_id"], version["id"])
        self.assertEqual(relations["knowledge"]["pinned_version_number"], version["version"])
        self.assertEqual(relations["product"]["material_id"], product_id)
        self.assertEqual(relations["product"]["title"], "测试泵")
        # 原产品行仍在且只有一行（只读适配，不复制）。
        products = self.db.conn.execute("SELECT COUNT(*) FROM products").fetchone()[0]
        self.assertEqual(products, 1)

    def test_legacy_knowledge_in_all_materials_without_topics(self):
        """旧文章与产品先在「全部资料」呈现；未确认前没有自动主题分配。"""
        item_id = self.publish_article()
        faq_id = self.db.create_knowledge_item(
            TENANT, "faq", "发货常见问题", self.admin["id"],
            content={"faq": [{"question": "几天到货？", "answer": "一般 3-5 天。"}]})
        product_id = self.db.create_product(TENANT, "KB2-P2", "循环泵", "冷却水循环")
        materials = self.db.kb2_list_materials(TENANT)
        by_key = {(m["kind"], m["id"]): m for m in materials}
        self.assertIn(("knowledge", item_id), by_key)
        self.assertIn(("knowledge", faq_id), by_key)
        self.assertIn(("product", product_id), by_key)
        for material in materials:
            self.assertEqual(material["topics"], [])
        self.assertEqual(self.db.kb2_list_topics(TENANT), [])
        # 关联后「全部资料」出现主题名；解绑后回到空。
        topic_id = self.db.kb2_create_topic(TENANT, self.admin["id"], "保修政策")
        self.db.kb2_set_relation(TENANT, self.admin["id"], topic_id, {"kind": "knowledge", "id": item_id})
        updated = {m["id"]: m for m in self.db.kb2_list_materials(TENANT) if m["kind"] == "knowledge"}
        self.assertEqual([t["title"] for t in updated[item_id]["topics"]], ["保修政策"])

    def test_topic_draft_publish_history_and_source_pin(self):
        """主题草稿 / 发布 / 历史与逐段来源：未核对不能发布；保存草稿不换线上；
        资料发布新版后旧出处钉住原版本并标记换取版。"""
        item_id = self.publish_article(body="整机保修 24 个月。")
        v1 = self.db.get_published_knowledge_version(TENANT, item_id)
        topic_id = self.db.kb2_create_topic(TENANT, self.admin["id"], "智能会议设备A")
        self.assertEqual(self.db.kb2_get_topic(TENANT, topic_id)["status"], "draft")
        # 未核对段落 → 发布被拦截。
        self.db.kb2_save_topic_draft(
            TENANT, self.admin["id"], topic_id,
            paragraphs=[self.topic_paragraph(item_id, v1["id"], "整机保修 24 个月。", checked=False)])
        with self.assertRaises(ValueError):
            self.db.kb2_publish_topic_draft(TENANT, self.admin["id"], topic_id)
        # 核对后发布 v1。
        self.db.kb2_save_topic_draft(
            TENANT, self.admin["id"], topic_id,
            paragraphs=[self.topic_paragraph(item_id, v1["id"], "整机保修 24 个月。")])
        self.assertEqual(self.db.kb2_publish_topic_draft(TENANT, self.admin["id"], topic_id), 1)
        topic = self.db.kb2_get_topic(TENANT, topic_id)
        self.assertEqual(topic["status"], "published")
        self.assertEqual(topic["published_version_number"], 1)
        # 保存草稿不换线上：草稿内容变化，线上版本仍是 v1 的文字。
        first_para = self.db.kb2_get_topic(TENANT, topic_id)["content"]["paragraphs"][0]
        edited = dict(first_para)
        edited["text"] = "整机保修 24 个月（含配件）。"
        self.db.kb2_save_topic_draft(TENANT, self.admin["id"], topic_id, paragraphs=[edited])
        topic = self.db.kb2_get_topic(TENANT, topic_id)
        self.assertEqual(topic["published_version_number"], 1)
        self.assertEqual(topic["draft_version_number"], 2)
        self.assertIn("含配件", topic["content"]["paragraphs"][0]["text"])  # 读取草稿（待发布修改）
        # 编辑撤销核对并标注人工改写：不能直接发布，显式再核对后才可发布。
        draft_para = topic["content"]["paragraphs"][0]
        self.assertFalse(draft_para["checked"])
        self.assertEqual(draft_para["origin"], "edited")
        with self.assertRaisesRegex(ValueError, "未核对"):
            self.db.kb2_publish_topic_draft(TENANT, self.admin["id"], topic_id)
        self.db.kb2_save_topic_draft(
            TENANT, self.admin["id"], topic_id,
            paragraphs=[{**draft_para, "checked": True}])
        versions = self.db.kb2_list_topic_versions(TENANT, topic_id)
        published = next(v for v in versions if v["version"] == 1)
        self.assertEqual(published["status"], "published")
        published_content = json.loads(published["content_json"])
        self.assertIn("整机保修 24 个月。", published_content["paragraphs"][0]["text"])
        # 发布 v2 → v1 转历史。
        self.assertEqual(self.db.kb2_publish_topic_draft(TENANT, self.admin["id"], topic_id), 2)
        history = [(v["version"], v["status"]) for v in self.db.kb2_list_topic_versions(TENANT, topic_id)]
        self.assertEqual(history, [(2, "published"), (1, "superseded")])
        # 资料发布新版：旧主题出处仍钉住 v1，并标记换取版（不静默指向新版本）。
        self.db.save_knowledge_draft(TENANT, item_id, self.admin["id"], content={"body": "整机保修 36 个月。"})
        self.db.publish_knowledge_draft(TENANT, item_id, self.admin["id"])
        v2 = self.db.get_published_knowledge_version(TENANT, item_id)
        self.assertNotEqual(v1["id"], v2["id"])
        topic = self.db.kb2_get_topic(TENANT, topic_id)
        para = topic["content"]["paragraphs"][0]
        self.assertEqual(para["source"]["version_id"], v1["id"])
        self.assertTrue(para["source_stale"])
        self.assertIn("整机保修与售后响应政策", para["source_label"])
        # 空正文主题不能发布。
        empty_topic = self.db.kb2_create_topic(TENANT, self.admin["id"], "空主题")
        self.db.kb2_save_topic_draft(TENANT, self.admin["id"], empty_topic, paragraphs=[])
        with self.assertRaises(ValueError):
            self.db.kb2_publish_topic_draft(TENANT, self.admin["id"], empty_topic)

    def test_topic_publish_external_ai_requires_confirm(self):
        """对外 + 授权 AI 的主题发布需要一次确认；未确认不发布。"""
        topic_id = self.db.kb2_create_topic(TENANT, self.admin["id"], "对外主题")
        self.db.kb2_save_topic_draft(
            TENANT, self.admin["id"], topic_id,
            paragraphs=[{"kind": "intro", "text": "手工撰写的介绍。", "checked": True, "origin": "manual"}],
            usage="external", ai_authorized=True)
        with self.assertRaises(ValueError):
            self.db.kb2_publish_topic_draft(TENANT, self.admin["id"], topic_id)
        self.db.kb2_publish_topic_draft(TENANT, self.admin["id"], topic_id, confirm_ai=True)
        topic = self.db.kb2_get_topic(TENANT, topic_id)
        self.assertEqual(topic["status"], "published")

    def test_group_management_and_delete_only_ungroups(self):
        """分组改名 / 移动主题 / 删除分组只解除分组，主题与关联保留。"""
        group_a = self.db.kb2_create_group(TENANT, self.admin["id"], "产品与交付")
        group_b = self.db.kb2_create_group(TENANT, self.admin["id"], "团队与文化")
        topic_id = self.db.kb2_create_topic(TENANT, self.admin["id"], "培训材料", group_id=group_a)
        self.db.kb2_move_topic(TENANT, self.admin["id"], topic_id, group_b)
        self.db.kb2_rename_group(TENANT, self.admin["id"], group_b, "团队与文化（新）")
        groups = {g["id"]: g["name"] for g in self.db.kb2_list_groups(TENANT)}
        self.assertEqual(groups, {group_a: "产品与交付", group_b: "团队与文化（新）"})
        self.assertEqual(self.db.kb2_get_topic(TENANT, topic_id)["group_id"], group_b)
        with self.assertRaises(ValueError):
            self.db.kb2_create_topic(TENANT, self.admin["id"], "错误分组主题", group_id="missing-group")
        self.db.kb2_delete_group(TENANT, self.admin["id"], group_b)
        topic = self.db.kb2_get_topic(TENANT, topic_id)
        self.assertIsNotNone(topic)
        self.assertIsNone(topic["group_id"])
        self.assertNotIn(group_b, {g["id"] for g in self.db.kb2_list_groups(TENANT)})

    def test_reopen_db_persists_topics_groups_relations(self):
        """重开数据库后：分组、主题、关联、主题正文与来源钉住全部还在。"""
        group_id = self.db.kb2_create_group(TENANT, self.admin["id"], "产品与交付")
        item_id = self.publish_article(body="持久正文。")
        v1 = self.db.get_published_knowledge_version(TENANT, item_id)
        topic_id = self.db.kb2_create_topic(TENANT, self.admin["id"], "持久主题", group_id=group_id)
        self.db.kb2_set_relation(TENANT, self.admin["id"], topic_id, {"kind": "knowledge", "id": item_id})
        self.db.kb2_save_topic_draft(
            TENANT, self.admin["id"], topic_id,
            paragraphs=[self.topic_paragraph(item_id, v1["id"], "持久正文。")])
        self.db.kb2_publish_topic_draft(TENANT, self.admin["id"], topic_id)
        self.reopen()
        topic = self.db.kb2_get_topic(TENANT, topic_id)
        self.assertEqual(topic["title"], "持久主题")
        self.assertEqual(topic["group_id"], group_id)
        self.assertEqual(topic["status"], "published")
        relations = self.db.kb2_list_relations(TENANT, topic_id)
        self.assertEqual(len(relations), 1)
        self.assertEqual(relations[0]["pinned_version_id"], v1["id"])
        para = topic["content"]["paragraphs"][0]
        self.assertEqual(para["text"], "持久正文。")
        self.assertEqual(para["source"]["version_id"], v1["id"])
        self.assertEqual(len(self.db.kb2_list_groups(TENANT)), 1)
        self.assertEqual(len(self.db.kb2_list_topics(TENANT)), 1)


if __name__ == "__main__":
    unittest.main()
