"""企业知识列表页回归：原始产品条目与知识条目共用同一列表结构（category_value 修复）。

背景：演示数据里的「原产品」（products 表，未做知识迁移）经 list_knowledge_product_overview
适配进统一的 /admin/knowledge 列表。此前产品条目缺少 category_value 键，列表页构建分类筛选
（categories 推导）时抛 KeyError，整个列表页 500。本测试用临时数据库锁定修复：

- 播种 1 个原始已发布产品（沿用既有产品线：创建→来源→字段核准→品类核准→发布；ID 保持产品自身 ID）
  与 1 篇新文章（知识线）；
- 「全部知识」「产品与服务」视图 200，且各自只出现应出现的内容；
- 分类、搜索筛选 200 且结果正确；
- 列表中的产品链接（/admin/knowledge/product/<原 ID>）可直达产品详情页，原 ID / 发布版本快照保持。

只用临时数据库与临时上传目录；不读写 data/ 下的演示数据。
"""
import http.client
import os
import re
import tempfile
import threading
import unittest
from pathlib import Path
from urllib.parse import quote, urlencode

from app import make_server

TENANT = "demo-tenant"
PRODUCT_MODEL = "CB-FIX-80"
PRODUCT_NAME = "CB-FIX-80 清水离心泵（回归样例）"
PRODUCT_CATEGORY = "清水泵类"
ARTICLE_TITLE = "整机保修与售后响应政策"
ARTICLE_CATEGORY = "售后政策"
ARTICLE_BODY = "整机保修 24 个月；报修后 48 小时内响应。"


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


class KnowledgeLegacyListFixTests(unittest.TestCase):
    """回归：原产品进入统一列表不再因条目结构不一致而崩溃。"""

    maxDiff = None

    @classmethod
    def setUpClass(cls):
        cls.temp_dir = tempfile.TemporaryDirectory()
        cls.db_path = Path(cls.temp_dir.name) / "legacy-list.db"
        os.environ["B2B_KB_STORAGE_ROOT"] = str(Path(cls.temp_dir.name) / "uploads")
        cls.server = make_server(cls.db_path, port=0)
        cls.db = cls.server.RequestHandlerClass.db
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

        cls.db.seed_demo_users()
        cls.admin = cls.db.authenticate_user("demo-admin@example.invalid", "demo-admin")

        # 原始已发布产品：沿用既有产品线（创建 → 来源 → 字段核准 → 品类核准 → 发布）。
        cls.product_id = cls.db.create_product(
            TENANT, PRODUCT_MODEL, PRODUCT_NAME, "清水循环与工厂冷却水", "setup")
        source_id = cls.db.add_source(
            TENANT, cls.product_id, f"{PRODUCT_MODEL}-样本.pdf", "PDF", "第 1 页：规格参数表",
            actor="setup", scope="exact_model")
        for key, value, unit in (
            ("flow_range", "20-80", "m³/h"),
            ("head_range", "18-42", "m"),
            ("media", "清水及低粘度、非腐蚀性液体", ""),
            ("material", "铸铁泵体，不锈钢叶轮", ""),
            ("power", "15", "kW"),
        ):
            cls.db.save_field(
                TENANT, cls.product_id, key, value, unit, source_id, "第 1 页：规格参数表", "setup")
        cls.db.set_category_candidate(TENANT, cls.product_id, PRODUCT_CATEGORY, "manual", "setup")
        cls.db.approve_category(TENANT, cls.product_id, "setup")
        cls.db.approve_seeded_fields(TENANT, cls.product_id, "setup")
        cls.db.publish_product(TENANT, cls.product_id, "setup")

        # 新文章（知识线）。
        cls.article_id = cls.db.create_knowledge_item(
            TENANT, "article", ARTICLE_TITLE, cls.admin["id"],
            content={"body": ARTICLE_BODY}, category=ARTICLE_CATEGORY,
            usage="external", ai_authorized=True)
        cls.db.publish_knowledge_draft(TENANT, cls.article_id, cls.admin["id"], confirm_ai=True)

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

    def get_page(self, path):
        status, _, page = form_request(self.server, "GET", path, cookie=self.cookie)
        self.assertEqual(status, 200, path)
        return page

    @property
    def product_row_title(self):
        return f"{PRODUCT_MODEL} · {PRODUCT_NAME}"

    # ── 测试 ─────────────────────────────────────────────────────────────

    def test_all_view_lists_original_product_and_article(self):
        """「全部知识」：原产品与新文章同时出现（此前该请求因 KeyError category_value 直接 500）。"""
        page = self.get_page("/admin/knowledge?view=all")
        self.assertIn(self.product_row_title, page)
        self.assertIn(ARTICLE_TITLE, page)
        # 分类筛选的两个候选值都能渲染（categories 推导即此前崩溃点）。
        self.assertIn(f"<option value='{PRODUCT_CATEGORY}'>", page)
        self.assertIn(f"<option value='{ARTICLE_CATEGORY}'>", page)
        # 产品行链接指向原产品详情（保持原 ID）。
        self.assertIn(f"/admin/knowledge/product/{self.product_id}", page)

    def test_product_service_view_shows_product_not_article(self):
        page = self.get_page("/admin/knowledge?view=product_service")
        self.assertIn(self.product_row_title, page)
        self.assertNotIn(ARTICLE_TITLE, page)

    def test_category_filter_returns_correct_rows(self):
        product_page = self.get_page("/admin/knowledge?view=all&category=" + quote(PRODUCT_CATEGORY))
        self.assertIn(self.product_row_title, product_page)
        self.assertNotIn(ARTICLE_TITLE, product_page)
        article_page = self.get_page("/admin/knowledge?view=all&category=" + quote(ARTICLE_CATEGORY))
        self.assertIn(ARTICLE_TITLE, article_page)
        self.assertNotIn(self.product_row_title, article_page)

    def test_search_filter_returns_correct_rows(self):
        product_page = self.get_page("/admin/knowledge?view=all&q=" + quote(PRODUCT_MODEL))
        self.assertIn(self.product_row_title, product_page)
        self.assertNotIn(ARTICLE_TITLE, product_page)
        article_page = self.get_page("/admin/knowledge?view=all&q=" + quote("保修"))
        self.assertIn(ARTICLE_TITLE, article_page)
        self.assertNotIn(self.product_row_title, article_page)

    def test_product_detail_reachable_and_ids_snapshots_preserved(self):
        """列表中的产品链接可直达详情页；原产品 ID 与已发布版本快照保持（未迁移、未重建）。"""
        page = self.get_page("/admin/knowledge?view=all")
        match = re.search(r"/admin/knowledge/product/([a-f0-9]{8,64})", page)
        self.assertIsNotNone(match, "列表页应包含原产品详情链接")
        product_id = match.group(1)
        self.assertEqual(product_id, self.product_id, "原产品 ID 不得变化")
        detail = self.get_page(f"/admin/knowledge/product/{product_id}")
        self.assertIn(PRODUCT_NAME, detail)
        product = self.db.get_product(TENANT, product_id)
        self.assertIsNotNone(product)
        self.assertEqual(product["status"], "published")
        self.assertIsNotNone(self.db.get_published_version(TENANT, product_id), "已发布版本快照应保持")


if __name__ == "__main__":
    unittest.main()
