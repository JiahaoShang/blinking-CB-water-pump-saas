"""企业知识第一批收尾回归（2026-10-10）：列表快照口径 + 已发布条目的停用可达 + 发布提示/规则模式诊断。

用户路径缺口（真实浏览器已复现）：
- 列表/搜索曾优先读草稿版本：已发布条目的标题/分类/用途/正文搜索显示未发布草稿内容；
- 详情操作曾用 if 草稿 ... elif 已发布，已发布条目一旦有草稿，「停用」入口消失。

本测试锁定修复后的口径（只用临时数据库、真实 HTTP 服务端渲染）：
1. 已发布 v1 + 草稿 v2：列表显示 v1 标题/分类与已发布状态（保留「有草稿修改待发布」），
   列表搜索按 v1 正文命中、对 v2 独有正文不可见；详情固定显示 v1；编辑器显示 v2。
2. 发布后切换：列表与详情切到 v2。
3. 已发布 + 草稿时「停用」保留且带确认；停用不要求先发布草稿：
   停用后线上版本与草稿/历史都保留，检索闸门即时生效（不再可用）；重新启用恢复原已发布状态。
4. 直接发布（POST publish）回详情页显示简单中文「已发布。」提示。
5. 规则模式（AI_MODE=rules）：问答测试页显示「模型未连接」并指引到企业设置连接模型；
   不触发任何模型调用、不生成模拟答复（复用既有 status/semantics，不改 API 语义与日志）。
"""
import http.client
import os
import tempfile
import threading
import unittest
from pathlib import Path
from urllib.parse import quote, urlencode

from app import make_server

TENANT = "demo-tenant"

V1_TITLE = "整机保修政策（线上 v1）"
V1_BODY = "v1 正文：整机保修 24 个月，报修 48 小时响应。"
V1_CATEGORY = "售后政策"
V2_TITLE = "整机保修政策（草稿 v2）"
V2_BODY = "v2 草稿正文：整机保修 36 个月，报修 24 小时响应。"
V2_CATEGORY = "售后规则"


def form_request(server, method, path, fields=None, cookie=None):
    conn = http.client.HTTPConnection("127.0.0.1", server.server_port)
    headers = {}
    body = None
    if fields is not None:
        body = urlencode(fields)
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    if cookie:
        headers["Cookie"] = cookie
    conn.request(method, path, body=body, headers=headers)
    response = conn.getresponse()
    raw = response.read().decode("utf-8")
    headers_out = dict(response.getheaders())
    status = response.status
    conn.close()
    return status, headers_out, raw


def login(server, email, password):
    status, headers, _ = form_request(server, "POST", "/login", {"email": email, "password": password})
    if status not in (302, 303) or "Set-Cookie" not in headers:
        raise AssertionError(f"登录失败: {email} -> {status}")
    return headers["Set-Cookie"].split(";", 1)[0]


class KnowledgeListSnapshotAndStopTests(unittest.TestCase):
    """已发布条目的列表快照口径与「停用」入口回归（每个用例自建条目，不依赖用例顺序）。"""

    maxDiff = None

    @classmethod
    def setUpClass(cls):
        cls.temp_dir = tempfile.TemporaryDirectory()
        cls.db_path = Path(cls.temp_dir.name) / "ui-final-flow.db"
        os.environ["B2B_KB_STORAGE_ROOT"] = str(Path(cls.temp_dir.name) / "uploads")
        cls.server = make_server(cls.db_path, port=0)
        cls.db = cls.server.RequestHandlerClass.db
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

        cls.db.seed_demo_users()
        cls.admin = cls.db.authenticate_user("demo-admin@example.invalid", "demo-admin")
        cls.cookie = login(cls.server, "demo-admin@example.invalid", "demo-admin")

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.db.conn.close()
        cls.thread.join()
        cls.temp_dir.cleanup()
        os.environ.pop("B2B_KB_STORAGE_ROOT", None)

    # ── 帮助 ─────────────────────────────────────────────────────────────

    def get(self, path):
        status, _, page = form_request(self.server, "GET", path, cookie=self.cookie)
        return status, page

    def get200(self, path):
        status, page = self.get(path)
        self.assertEqual(status, 200, path)
        return page

    def post(self, path, fields):
        return form_request(self.server, "POST", path, fields, self.cookie)

    def make_published_article(self, title, body, category, usage="external", ai_authorized=True):
        """已发布 v1 条目（对外 + 已授权）。标题由用例传入（各用例唯一，避免共享库里互相干扰）。"""
        item_id = self.db.create_knowledge_item(
            TENANT, "article", title, self.admin["id"],
            content={"body": body}, category=category, usage=usage, ai_authorized=ai_authorized)
        self.db.publish_knowledge_draft(TENANT, item_id, self.admin["id"], confirm_ai=ai_authorized and usage == "external")
        return item_id

    def save_draft(self, item_id, title, body, category, usage="internal"):
        self.db.save_knowledge_draft(
            TENANT, item_id, self.admin["id"],
            content={"title": title, "body": body}, category=category, usage=usage)
        return self.db.get_knowledge_draft_version(TENANT, item_id)

    def clear_draft(self, item_id):
        """清掉当前草稿（供需要精确版本序列的用例自行播种）。"""
        self.db.conn.execute("UPDATE knowledge_items SET draft_version_id=NULL WHERE tenant_id=? AND id=?", (TENANT, item_id))
        self.db.conn.commit()

    # ── 1. 已发布 + 草稿：列表 / 详情 / 编辑器各取正确快照 ───────────────

    def test_a01_list_shows_published_snapshot_with_draft_hint(self):
        v1_title, v2_title = "A1 列表快照 v1", "A1 列表快照 v2（草稿）"
        item_id = self.make_published_article(v1_title, V1_BODY, V1_CATEGORY)
        self.save_draft(item_id, v2_title, V2_BODY, V2_CATEGORY)
        page = self.get200("/admin/knowledge")
        self.assertIn(v1_title, page, "列表标题取已发布快照")
        self.assertNotIn(v2_title, page, "草稿标题不得顶替线上展示")
        self.assertIn("分类：" + V1_CATEGORY, page, "列表分类取已发布快照")
        self.assertNotIn("分类：" + V2_CATEGORY, page, "草稿分类不得出现在列表行（筛选下拉允许列历史分类）")
        self.assertIn("已发布（AI客服不可用）", page, "已发布状态保持（准备中不可用）")
        self.assertNotIn("草稿尚未发布", page)
        self.assertIn("有草稿修改待发布", page, "草稿提示保留")

    def test_a02_list_search_uses_published_body_and_category(self):
        v1_title, v2_title = "A2 搜索口径 v1", "A2 搜索口径 v2（草稿）"
        item_id = self.make_published_article(v1_title, V1_BODY, V1_CATEGORY)
        self.save_draft(item_id, v2_title, V2_BODY, V2_CATEGORY)
        v1_page = self.get200("/admin/knowledge?q=" + quote("保修 24 个月"))
        self.assertIn(v1_title, v1_page, "搜索命中已发布正文")
        v2_page = self.get200("/admin/knowledge?q=" + quote("36 个月"))
        self.assertNotIn(v1_title, v2_page, "草稿独有正文不得被搜索命中")
        cat_page = self.get200("/admin/knowledge?category=" + quote(V1_CATEGORY))
        self.assertIn(v1_title, cat_page)
        draft_cat_page = self.get200("/admin/knowledge?category=" + quote(V2_CATEGORY))
        self.assertNotIn(v1_title, draft_cat_page, "草稿分类不得成为筛选值")

    def test_a03_detail_shows_published_and_editor_shows_draft(self):
        v1_title, v2_title = "A3 详情快照 v1", "A3 详情快照 v2（草稿）"
        item_id = self.make_published_article(v1_title, V1_BODY, V1_CATEGORY)
        self.save_draft(item_id, v2_title, V2_BODY, V2_CATEGORY)
        detail = self.get200(f"/admin/knowledge/item/{item_id}")
        self.assertIn(v1_title, detail, "详情固定展示已发布版本")
        self.assertIn("当前版本 v1", detail)
        self.assertNotIn(V2_BODY, detail, "详情不得显示草稿正文")
        self.assertIn("有草稿修改待发布（草稿 v2）", detail)
        editor = self.get200(f"/admin/knowledge/item/{item_id}/edit")
        self.assertIn(v2_title, editor, "编辑器继续编辑草稿")
        self.assertIn(V2_BODY, editor)

    # ── 2. 发布后切换 ────────────────────────────────────────────────────

    def test_b01_publish_switches_list_detail_and_simple_notice(self):
        v1_title, v2_title = "B1 发布切换 v1", "B1 发布切换 v2"
        item_id = self.make_published_article(v1_title, V1_BODY, V1_CATEGORY)
        self.save_draft(item_id, v2_title, V2_BODY, V2_CATEGORY)
        code, headers, _ = self.post(f"/admin/knowledge/item/{item_id}/publish", {"confirmed": "1"})
        self.assertEqual(code, 303)
        notice_location = headers.get("Location", "")
        self.assertIn("notice=published", notice_location, "发布后使用简单通知码")
        item = self.db.get_knowledge_item(TENANT, item_id)
        self.assertEqual(item["status"], "published")
        self.assertEqual(item["draft_version_id"], None, "发布后草稿清空")
        detail = self.get200(notice_location)
        self.assertIn("已发布。", detail, "详情页显示简单中文「已发布。」")
        self.assertNotIn("已发布；处理状态按真实本地内容准备结果显示。", detail)
        self.assertIn(v2_title, detail)
        self.assertIn("当前版本 v2", detail)
        lst = self.get200("/admin/knowledge")
        self.assertIn(v2_title, lst, "发布后列表切到 v2")
        self.assertNotIn(v1_title, lst)

    # ── 3. 已发布 + 草稿：停用保留、闸门即时、历史保留、重新启用 ──────────

    def test_c01_published_item_with_draft_keeps_stop_and_publish(self):
        item_id = self.make_published_article("C1 停用入口 v1", V1_BODY, V1_CATEGORY)
        self.save_draft(item_id, "C1 停用入口 v2（草稿）", V2_BODY, V2_CATEGORY)
        page = self.get200(f"/admin/knowledge/item/{item_id}")
        self.assertIn(f"/admin/knowledge/item/{item_id}/disable", page, "已发布 + 有草稿时「停用」必须保留")
        self.assertIn("停用这条知识？", page, "停用保留确认对话框")
        self.assertIn("data-kb-confirm-tone='danger'", page)
        self.assertIn(f"/admin/knowledge/item/{item_id}/publish", page, "有草稿时「发布」同时保留")
        self.assertIn("编辑（存为草稿）", page)

    def test_c02_published_item_without_draft_keeps_simple_actions(self):
        item_id = self.make_published_article("C2 无草稿操作 v1", V1_BODY, V1_CATEGORY)
        page = self.get200(f"/admin/knowledge/item/{item_id}")
        self.assertIn(f"/admin/knowledge/item/{item_id}/disable", page)
        self.assertIn("编辑（存为草稿）", page)
        self.assertNotIn(f"/admin/knowledge/item/{item_id}/publish", page, "无草稿时没有可发布内容，不显示发布")

    def test_c03_stop_preserves_versions_and_blocks_immediately_then_enable(self):
        v1_title = "C3 停用保留 v1"
        item_id = self.make_published_article(v1_title, V1_BODY, V1_CATEGORY)
        self.clear_draft(item_id)
        draft = self.save_draft(item_id, "C3 停用保留 v2（草稿）", "停用前草稿正文。", V2_CATEGORY)
        published = self.db.get_published_knowledge_version(TENANT, item_id)
        self.assertEqual(self.db.get_knowledge_item(TENANT, item_id)["status"], "published",
                         "存草稿不改线上状态（停用前仍是已发布）")

        # 停用不要求先发布草稿：直接停用，线上版本与草稿/历史必须保留，闸门即时关闭。
        code, _, _ = self.post(f"/admin/knowledge/item/{item_id}/disable", {"confirmed": "1"})
        self.assertEqual(code, 303)
        item = self.db.get_knowledge_item(TENANT, item_id)
        self.assertEqual(item["status"], "disabled", "停用立即生效")
        self.assertEqual(item["published_version_id"], published["id"], "停用保留线上版本")
        self.assertEqual(item["draft_version_id"], draft["id"], "停用保留草稿")
        versions_all = self.db.list_knowledge_versions(TENANT, item_id)
        self.assertEqual(len(versions_all), 2, "历史版本保留（v1 已发布 + v2 草稿）")
        state = self.db.knowledge_item_ui_state(TENANT, item_id)
        self.assertFalse(state["available"], "停用后检索闸门即时关闭")
        detail = self.get200(f"/admin/knowledge/item/{item_id}")
        self.assertIn("已停用", detail, "已停用状态与重新启用入口可见")
        self.assertIn(f"/admin/knowledge/item/{item_id}/enable", detail)
        versions = self.get200(f"/admin/knowledge/item/{item_id}?tab=versions")
        self.assertIn("草稿（未发布）", versions, "草稿仍出现在版本历史")
        lst = self.get200("/admin/knowledge")
        self.assertIn("已停用", lst)

        # 重新启用：恢复原已发布状态、沿用原线上版本，草稿保持。
        code, _, _ = self.post(f"/admin/knowledge/item/{item_id}/enable", {"confirmed": "1"})
        self.assertEqual(code, 303)
        item = self.db.get_knowledge_item(TENANT, item_id)
        self.assertEqual(item["status"], "published", "重新启用恢复原已发布状态")
        self.assertEqual(item["published_version_id"], published["id"], "沿用原线上版本（不发布草稿）")
        self.assertEqual(item["draft_version_id"], draft["id"], "草稿在停用/启用往返中保持")
        page = self.get200(f"/admin/knowledge/item/{item_id}")
        self.assertIn("当前版本 v1", page)
        self.assertIn("有草稿修改待发布", page)


class KnowledgeQaRulesModeDiagnosticsTests(unittest.TestCase):
    """规则模式用户诊断：模型未连接 + 企业设置连接指引；不触发模型调用、无模拟答复。"""

    maxDiff = None

    @classmethod
    def setUpClass(cls):
        cls.temp_dir = tempfile.TemporaryDirectory()
        cls.db_path = Path(cls.temp_dir.name) / "ui-final-flow-qa.db"
        os.environ["B2B_KB_STORAGE_ROOT"] = str(Path(cls.temp_dir.name) / "uploads")
        cls.server = make_server(cls.db_path, port=0)
        cls.db = cls.server.RequestHandlerClass.db
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.db.seed_demo_users()
        cls.admin = cls.db.authenticate_user("demo-admin@example.invalid", "demo-admin")
        cls.cookie = login(cls.server, "demo-admin@example.invalid", "demo-admin")

        # 已发布 + 对外 + 已授权 + 正文含有独特句子（规则模式下不得作为答复出现）。
        cls.item_id = cls.db.create_knowledge_item(
            TENANT, "article", "发货时效", cls.admin["id"],
            content={"body": "规则模式正文不得作为答复出现。"}, category="物流",
            usage="external", ai_authorized=True)
        cls.db.publish_knowledge_draft(TENANT, cls.item_id, cls.admin["id"], confirm_ai=True)

        # 注入「模型未连接」替身：任何模型调用都会立刻失败，证明规则模式不触碰模型。
        class AssertNoModelCalls:
            mode = "rules"

            def __getattr__(self, name):
                raise AssertionError(f"规则模式不得调用模型方法：{name}")

        class StubModels:
            def service(self, tenant):
                return AssertNoModelCalls()

        cls.server.RequestHandlerClass.models = StubModels()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.db.conn.close()
        cls.thread.join()
        cls.temp_dir.cleanup()
        os.environ.pop("B2B_KB_STORAGE_ROOT", None)

    def test_rules_mode_page_reports_model_not_connected_with_settings_hint(self):
        status, _, page = form_request(self.server, "GET", "/admin/knowledge/qa", None, self.cookie)
        self.assertEqual(status, 200)
        self.assertIn("模型未连接", page)
        self.assertIn("AI_MODE=rules", page)
        self.assertIn("请在企业设置连接模型", page, "规则模式必须给出连接模型指引")
        self.assertNotIn("模型已接通", page)
        logs_before = self.db.conn.execute("SELECT COUNT(*) FROM ai_call_log").fetchone()[0]
        status, _, page = form_request(
            self.server, "POST", "/admin/knowledge/qa", {"question": "发货要几天？"}, self.cookie)
        self.assertEqual(status, 200)
        self.assertIn("模型未连接", page, "提交后仍明确模型未连接")
        self.assertIn("请在企业设置连接模型", page)
        self.assertNotIn("规则模式正文不得作为答复出现。", page, "规则模式不得生成模拟答复")
        logs_after = self.db.conn.execute("SELECT COUNT(*) FROM ai_call_log").fetchone()[0]
        self.assertEqual(logs_before, logs_after, "规则模式不产生模型调用日志")
        self.assertEqual(self.db.list_human_tasks(TENANT), [], "不自动创建人工任务")


if __name__ == "__main__":
    unittest.main()
