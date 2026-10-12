# -*- coding: utf-8 -*-
"""设计对齐收尾（2026-10-10 · phase1 授权范围内）：移动端列表行 + 原产品未发布标签 + 详情页确认范围（R16）。

用户确认的最终小改动清单：
1) @media max600 下 kb-row 曾用 flex-wrap + 徽标 order 顶置，把标题/说明挤到约 120px（390 宽实拍）；
   改为「徽标行（类型 / 状态 / 箭头）一行 + kb-grow 标题/说明整行占满下方」的移动端网格；桌面布局、
   颜色、链接与焦点行为保持；320 宽可用。
2) 原产品未发布行 state.status 已是 draft，但 state.title 显示「未发布」：改为「草稿」，
   保持生命周期仅三态（草稿 / 已发布 / 已停用）；不改 state.status / ID。
3) PRD R16：必要确认仅「停用」与「对外 + 已授权」发布/重新启用。
   - 对内或未授权的发布：详情页不出现确认属性，且路由不带 confirmed 也能发布；
   - 对外 + 已授权发布：必须出现确认；路由不带 confirmed 被后端拒绝（显式确认校验保持）；
   - 重新启用：按「生效的已发布版本」同口径；停用：始终确认（界面 + 路由不带 confirmed 被拒）。

只用临时数据库 + 真实 HTTP 服务端渲染；不动 data/ 既有库、不跑全量、不新增依赖。
"""
import http.client
import os
import re
import tempfile
import threading
import unittest
from pathlib import Path
from urllib.parse import urlencode

from app import make_server

TENANT = "demo-tenant"


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


class DesignAlignFixesTests(unittest.TestCase):
    """每个用例自建数据、不依赖用例顺序；共享临时库仅作夹具。"""

    maxDiff = None

    @classmethod
    def setUpClass(cls):
        cls.temp_dir = tempfile.TemporaryDirectory()
        cls.db_path = Path(cls.temp_dir.name) / "design-align.db"
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

    # ── 帮助 ────────────────────────────────────────────────────────────

    def get200(self, path):
        status, _, page = form_request(self.server, "GET", path, cookie=self.cookie)
        self.assertEqual(status, 200, path)
        return page

    def post(self, path, fields=None):
        return form_request(self.server, "POST", path, fields or {}, self.cookie)

    def make_item(self, title, usage="internal", ai_authorized=False, publish=False):
        item_id = self.db.create_knowledge_item(
            TENANT, "article", title, self.admin["id"],
            content={"body": title + " 正文"}, usage=usage, ai_authorized=ai_authorized)
        if publish:
            self.db.publish_knowledge_draft(
                TENANT, item_id, self.admin["id"],
                confirm_ai=bool(usage == "external" and ai_authorized))
        return item_id

    def item_form_tag(self, page, item_id, action):
        """取详情页某个动作表单的开标签（含确认属性），用于精确判定是否弹确认。"""
        match = re.search(
            r"<form [^>]*action='/admin/knowledge/item/%s/%s'[^>]*>" % (re.escape(item_id), action), page)
        return match.group(0) if match else ""

    # ── 1. 移动端列表行：徽标行在上、标题/说明整行在下 ────────────────────

    def test_a01_mobile_row_layout_guard_and_structure_preserved(self):
        item_id = self.make_item("A01 布局回归条目")
        page = self.get200("/admin/knowledge?view=all")
        # 旧的挤压写法（flex-wrap + 徽标 order 顶置）必须消失
        self.assertNotIn("order:-1", page, "不再用 order 顶置徽标挤压标题行")
        # 移动端两行网格：徽标行（类型/状态/箭头）+ 标题/说明整行
        self.assertIn(".kb-row{display:grid;grid-template-columns:auto auto minmax(24px,1fr)", page)
        self.assertIn(".kb-row .kb-tag{grid-area:1/1}", page)
        self.assertIn(".kb-row .kb-pill{grid-area:1/2}", page)
        self.assertIn(".kb-row .row-chev{grid-area:1/3;justify-self:end}", page)
        self.assertIn(".kb-row .kb-grow{grid-area:2/1/3/4}", page)
        # 600px 块内其他既有规则保留
        self.assertIn(".kb-form .kb-grid2,.kb-form .kb-grid3{grid-template-columns:1fr}", page)
        self.assertIn(".kb-filterbar{flex-direction:column;align-items:stretch}", page)
        # 桌面布局、链接与可访问行为保持
        self.assertIn(".kb-row{display:flex;align-items:center;gap:14px", page, "桌面布局不变")
        self.assertIn(".kb-row:focus-visible{outline:2px solid var(--accent);outline-offset:2px}", page)
        # 行仍是链接行，DOM 结构保持原顺序（类型 / 标题说明 / 状态 / 箭头）
        match = re.search(
            r"<a class='kb-row' href='/admin/knowledge/item/%s'>.*?</a>" % re.escape(item_id), page, re.S)
        self.assertIsNotNone(match, "列表行仍是可点击链接")
        row = match.group(0)
        positions = [row.find("kb-tag"), row.find("kb-grow"), row.find("kb-pill"), row.find("row-chev")]
        self.assertEqual(positions, sorted(positions), "行结构保持原 DOM 顺序（只改 CSS 布局）")

    # ── 2. 原产品未发布：行与详情同显示「草稿」 ──────────────────────────

    def test_a02_product_unpublished_shows_draft_label_in_row_and_detail(self):
        product_id = self.db.create_product(TENANT, "ALIGN-390", "对齐测试泵", "冷却水循环")
        page = self.get200("/admin/knowledge?view=all")
        match = re.search(
            r"<a class='kb-row' href='/admin/knowledge/product/%s'>.*?</a>" % re.escape(product_id), page, re.S)
        self.assertIsNotNone(match, "原产品应出现在知识列表")
        row = match.group(0)
        self.assertIn("ALIGN-390 · 对齐测试泵", row)
        self.assertIn("<span class='kb-pill draft'>草稿</span>", row, "未发布原产品行显示「草稿」")
        self.assertNotIn("未发布", row, "行内不出现「未发布」标签")
        detail = self.get200(f"/admin/knowledge/product/{product_id}")
        self.assertIn("<span class='kb-pill draft'>草稿</span>", detail, "详情页同一枚状态标签：草稿")
        # 未动 state.status / ID：产品仍以草稿状态出现在筛选中（状态筛选草稿能命中）
        filtered = self.get200("/admin/knowledge?view=all&status=draft")
        self.assertIn(f"/admin/knowledge/product/{product_id}", filtered)

    # ── 3. 详情页确认范围与 R16 对齐 ─────────────────────────────────────

    def test_b01_internal_draft_publish_no_confirm_and_route_accepts_without_confirmed(self):
        item_id = self.make_item("B01 对内草稿")
        page = self.get200(f"/admin/knowledge/item/{item_id}")
        publish_tag = self.item_form_tag(page, item_id, "publish")
        self.assertTrue(publish_tag, "草稿详情应有发布表单")
        self.assertNotIn("data-kb-confirm", publish_tag, "对内（未授权）草稿发布不弹额外确认")
        self.assertIn("name='confirmed' value=''", page, "显式确认字段保留（对外+已授权时由对话框写入）")
        code, headers, _ = self.post(f"/admin/knowledge/item/{item_id}/publish", {})
        self.assertEqual(code, 303)
        self.assertIn("notice=published", headers.get("Location", ""), "不带 confirmed 也能发布对内草稿")
        self.assertEqual(self.db.get_knowledge_item(TENANT, item_id)["status"], "published")

    def test_b02_external_ai_draft_publish_confirm_required_on_dialog_and_route(self):
        item_id = self.make_item("B02 对外授权草稿", usage="external", ai_authorized=True)
        page = self.get200(f"/admin/knowledge/item/{item_id}")
        publish_tag = self.item_form_tag(page, item_id, "publish")
        self.assertIn("data-kb-confirm", publish_tag, "对外+已授权发布必须先确认")
        self.assertIn("data-kb-confirm-label='确认发布'", publish_tag)
        code, headers, _ = self.post(f"/admin/knowledge/item/{item_id}/publish", {})
        self.assertEqual(code, 303)
        self.assertIn("error=", headers.get("Location", ""), "不带确认字段：后端显式确认校验保持")
        self.assertEqual(self.db.get_knowledge_item(TENANT, item_id)["status"], "draft", "未确认不发布")
        code, headers, _ = self.post(f"/admin/knowledge/item/{item_id}/publish", {"confirmed": "1"})
        self.assertEqual(code, 303)
        self.assertIn("notice=published", headers.get("Location", ""))
        self.assertEqual(self.db.get_knowledge_item(TENANT, item_id)["status"], "published")

    def test_b03_publish_confirm_follows_pending_draft_not_published_version(self):
        # 已发布（对内）+ 对外已授权草稿 → 按「待发布草稿」判定：需要确认
        item_a = self.make_item("B03a 已发布对内", usage="internal", publish=True)
        self.db.save_knowledge_draft(
            TENANT, item_a, self.admin["id"], content={"body": "对外草稿正文"},
            usage="external", ai_authorized=True)
        page_a = self.get200(f"/admin/knowledge/item/{item_a}")
        self.assertIn("data-kb-confirm", self.item_form_tag(page_a, item_a, "publish"),
                      "草稿为对外+已授权：待发布动作需要确认（与线上版本状态无关）")
        # 已发布（对外+已授权）+ 对内草稿 → 按「待发布草稿」判定：不弹确认，且路由可直接发布
        item_b = self.make_item("B03b 已发布对外", usage="external", ai_authorized=True, publish=True)
        self.db.save_knowledge_draft(
            TENANT, item_b, self.admin["id"], content={"body": "对内草稿正文"},
            usage="internal", ai_authorized=False)
        page_b = self.get200(f"/admin/knowledge/item/{item_b}")
        self.assertNotIn("data-kb-confirm", self.item_form_tag(page_b, item_b, "publish"),
                         "草稿仅对内：不弹额外确认（即使线上版本对外+已授权）")
        code, headers, _ = self.post(f"/admin/knowledge/item/{item_b}/publish", {})
        self.assertEqual(code, 303)
        self.assertIn("notice=published", headers.get("Location", ""), "不带 confirmed 可直接发布对内草稿")
        self.assertEqual(self.db.get_knowledge_item(TENANT, item_b)["draft_version_id"], None)

    def test_b04_enable_confirm_follows_effective_published_version(self):
        # 对外+已授权：停用后重新启用必须先确认；不带 confirmed 被拒
        item_a = self.make_item("B04a 对外授权", usage="external", ai_authorized=True, publish=True)
        code, _, _ = self.post(f"/admin/knowledge/item/{item_a}/disable", {"confirmed": "1"})
        self.assertEqual(code, 303)
        page_a = self.get200(f"/admin/knowledge/item/{item_a}")
        enable_tag = self.item_form_tag(page_a, item_a, "enable")
        self.assertIn("data-kb-confirm", enable_tag, "对外+已授权重新启用必须先确认")
        self.assertIn("data-kb-confirm-label='重新启用'", enable_tag)
        code, headers, _ = self.post(f"/admin/knowledge/item/{item_a}/enable", {})
        self.assertEqual(code, 303)
        self.assertIn("error=", headers.get("Location", ""), "不带确认字段：后端显式确认校验保持")
        self.assertEqual(self.db.get_knowledge_item(TENANT, item_a)["status"], "disabled")
        code, _, _ = self.post(f"/admin/knowledge/item/{item_a}/enable", {"confirmed": "1"})
        self.assertEqual(code, 303)
        self.assertEqual(self.db.get_knowledge_item(TENANT, item_a)["status"], "published")
        # 对内：重新启用不弹确认；不带 confirmed 直接恢复
        item_b = self.make_item("B04b 对内", usage="internal", publish=True)
        self.post(f"/admin/knowledge/item/{item_b}/disable", {"confirmed": "1"})
        page_b = self.get200(f"/admin/knowledge/item/{item_b}")
        self.assertNotIn("data-kb-confirm", self.item_form_tag(page_b, item_b, "enable"),
                         "对内条目重新启用不弹额外确认")
        code, headers, _ = self.post(f"/admin/knowledge/item/{item_b}/enable", {})
        self.assertEqual(code, 303)
        self.assertIn("notice=", headers.get("Location", ""), "不带 confirmed 可直接重新启用对内条目")
        self.assertEqual(self.db.get_knowledge_item(TENANT, item_b)["status"], "published")

    def test_b05_stop_always_confirms_even_for_internal_item(self):
        item_id = self.make_item("B05 对内发布", usage="internal", publish=True)
        page = self.get200(f"/admin/knowledge/item/{item_id}")
        disable_tag = self.item_form_tag(page, item_id, "disable")
        self.assertIn("data-kb-confirm", disable_tag, "停用始终确认（即使条目仅对内）")
        self.assertIn("data-kb-confirm-tone='danger'", disable_tag)
        self.assertIn("data-kb-confirm-label='停用'", disable_tag)
        code, headers, _ = self.post(f"/admin/knowledge/item/{item_id}/disable", {})
        self.assertEqual(code, 303)
        self.assertIn("error=", headers.get("Location", ""), "停用：不带确认字段被拒（后端校验保持）")
        self.assertEqual(self.db.get_knowledge_item(TENANT, item_id)["status"], "published")
        self.post(f"/admin/knowledge/item/{item_id}/disable", {"confirmed": "1"})
        self.assertEqual(self.db.get_knowledge_item(TENANT, item_id)["status"], "disabled")


if __name__ == "__main__":
    unittest.main()
