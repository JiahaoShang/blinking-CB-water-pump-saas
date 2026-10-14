# -*- coding: utf-8 -*-
"""公开 RFQ 头部身份回归（正式化清单 F-6 / T5-N02）。

范围：/rfq/{token} 公开填写页的头部身份，只覆盖：
1. 有效令牌 = 该询价记录所属企业的当前名称（读 tenants 现名；不是请求上下文/匿名默认企业）；
2. 无效、已撤销、非渠道会话的令牌 = 中性 Growave，不泄露任何企业名称；
3. 头部不再出现「Industrial Pump Supplier · DEMO」，也不带指向不存在产品区块的导航
   （#specifications/#downloads/#product-rfq）；
4. 名称按 HTML 转义渲染；公开页保留 no-referrer；
5. 令牌形态不合法的 /rfq/ 路径（空/过短/超长/含令牌正则之外字符）与无效令牌同响应：
   404 + 中性 Growave，不落入通用 404（旧页头）；非 /rfq 路径与有效路由不受影响。
不含表单字段/提交/修订/权限语义（由既有测试覆盖）。

隔离口径：临时数据库 + 本机 HTTP 服务（make_server，rules 模式，无外部调用）；
不触网、不发外信、不读真实凭据、不写原树 data/。
"""
import re
import sys
import tempfile
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from ai_client import AIConfig  # noqa: E402
from app import make_server  # noqa: E402
from test_langbot_bridge import form_request  # noqa: E402

TENANT_A = "header-a-tenant"
TENANT_A_NAME = '北星机械 & <Alpha> "联合"'
TENANT_A_ESCAPED = "北星机械 &amp; &lt;Alpha&gt; &quot;联合&quot;"
TENANT_A_RENAMED = 'Polaris & <Beta> "Group"'
TENANT_A_RENAMED_ESCAPED = "Polaris &amp; &lt;Beta&gt; &quot;Group&quot;"
TENANT_B = "header-b-tenant"
TENANT_B_NAME = "Southgate & Sons <UK>"
TENANT_B_ESCAPED = "Southgate &amp; Sons &lt;UK&gt;"
DEMO_TENANT_NAME = "示例水泵企业（演示）"
OLD_DEMO_MARKERS = ("Industrial Pump Supplier", "· DEMO")
DEAD_ANCHORS = ("#specifications", "#downloads", "#product-rfq")
NO_REFERRER = "<meta name='referrer' content='no-referrer'>"


def clip(text, limit=220):
    """失败信息只保留页面片段：日志不落整页 HTML。"""
    text = str(text)
    return text if len(text) <= limit else text[:limit] + "…[HTML 截断]"


class PublicRfqHeaderTests(unittest.TestCase):
    """公开 RFQ 填写/验证失败/提交/修订/无效链接的头部身份。"""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(prefix="public-rfq-header-")
        cls.server = make_server(Path(cls.tmp.name) / "app.db", port=0, ai_config=AIConfig())
        cls.db = cls.server.RequestHandlerClass.db
        cls.db.ensure_tenant(TENANT_A, TENANT_A_NAME)
        cls.db.ensure_tenant(TENANT_B, TENANT_B_NAME)
        cls.db.ensure_tenant("demo-tenant", DEMO_TENANT_NAME)  # 匿名请求的默认企业：不得被泄露到公开页
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()
        cls.db.conn.close()
        cls.tmp.cleanup()

    # ── 工具 ──────────────────────────────────────────────────────────────
    def link_for(self, tenant_id, source="langbot"):
        """按企业建会话并签发 RFQ 链接令牌（只存摘要，返回明文令牌）。"""
        conversation_id = self.db.create_conversation(tenant_id, source, "en")
        return conversation_id, self.db.create_rfq_form_token(tenant_id, conversation_id, "public-rfq-header-test")

    def header_of(self, page):
        match = re.search(r"<header>.*?</header>", page, re.S)
        self.assertTrue(match, "公开 RFQ 页应渲染 header 区")
        return match.group(0)

    def assert_header(self, page, expected_name_escaped, label):
        """头部 = 纯文本名称（转义后），无导航、无演示身份；公开页保留 no-referrer。"""
        expected = f"<header><strong>{expected_name_escaped}</strong></header>"
        header = self.header_of(page)
        if header != expected:
            self.fail(f"{label}头部应为 {expected!r}；实际：{clip(header)}")
        for marker in OLD_DEMO_MARKERS + DEAD_ANCHORS:
            at = page.find(marker)
            if at >= 0:
                self.fail(f"{label}不应再出现 {marker!r}；上下文：{clip(page[max(0, at - 60):at + len(marker) + 60])}")
        if NO_REFERRER not in page:
            self.fail(f"{label}应保留 no-referrer 元标记；页面片段：{clip(page)}")

    def assert_name_absent(self, page, name, label):
        at = page.find(name)
        if at >= 0:
            self.fail(f"{label}不应出现 {name!r}；上下文：{clip(page[max(0, at - 60):at + len(name) + 60])}")

    def assert_neutral_404(self, status, page, label):
        self.assertEqual(status, 404, f"{label}应返回 404")
        self.assert_header(page, "Growave", label)
        if "链接无效或已撤销" not in page:
            self.fail(f"{label}应显示链接无效说明；页面片段：{clip(page)}")
        for name in (TENANT_A_NAME, TENANT_A_ESCAPED, TENANT_B_NAME, TENANT_B_ESCAPED, DEMO_TENANT_NAME):
            self.assert_name_absent(page, name, label)

    # ── 用例 ──────────────────────────────────────────────────────────────
    def test_valid_links_show_own_tenant_current_name(self):
        """A/B 两条有效链接各自显示所属企业名称；改名后显示现名；请求参数不参与身份。"""
        _, token_a = self.link_for(TENANT_A)
        _, token_b = self.link_for(TENANT_B)
        status, _, page_a = form_request(self.server, "GET", f"/rfq/{token_a}")
        self.assertEqual(status, 200)
        self.assert_header(page_a, TENANT_A_ESCAPED, "A 链接填写页")
        self.assert_name_absent(page_a, TENANT_A_NAME, "A 链接填写页")   # 原文（含 HTML 特殊字符）不得直接出现
        self.assert_name_absent(page_a, TENANT_B_ESCAPED, "A 链接填写页")  # 不串企业
        self.assert_name_absent(page_a, DEMO_TENANT_NAME, "A 链接填写页")  # 不用匿名默认企业
        status, _, page_b = form_request(self.server, "GET", f"/rfq/{token_b}")
        self.assertEqual(status, 200)
        self.assert_header(page_b, TENANT_B_ESCAPED, "B 链接填写页")
        self.assert_name_absent(page_b, TENANT_A_ESCAPED, "B 链接填写页")
        self.assert_name_absent(page_b, DEMO_TENANT_NAME, "B 链接填写页")
        # 改名后同一链接显示当前名称（读 tenants 现名，不是签发时快照）
        try:
            self.db.conn.execute("UPDATE tenants SET name=? WHERE id=?", (TENANT_A_RENAMED, TENANT_A))
            self.db.conn.commit()
            status, _, page = form_request(self.server, "GET", f"/rfq/{token_a}")
            self.assertEqual(status, 200)
            self.assert_header(page, TENANT_A_RENAMED_ESCAPED, "A 改名后填写页")
            self.assert_name_absent(page, TENANT_A_ESCAPED, "A 改名后填写页")
        finally:
            self.db.conn.execute("UPDATE tenants SET name=? WHERE id=?", (TENANT_A_NAME, TENANT_A))
            self.db.conn.commit()
        # 请求参数不能参与身份（不能借参数指定企业名/品牌）
        status, _, page = form_request(self.server, "GET", f"/rfq/{token_a}?tenant_id={TENANT_B}&brand=EvilCorp")
        self.assertEqual(status, 200)
        self.assert_header(page, TENANT_A_ESCAPED, "A 带参数填写页")
        self.assert_name_absent(page, "EvilCorp", "A 带参数填写页")

    def test_form_validation_failure_keeps_tenant_header(self):
        """填写页：未勾选联系确认 / 缺必填字段的 400 页仍显示所属企业。"""
        _, token = self.link_for(TENANT_B)
        status, _, page = form_request(self.server, "GET", f"/rfq/{token}")
        self.assertEqual(status, 200)
        self.assert_header(page, TENANT_B_ESCAPED, "填写页")
        if "填写询价信息" not in page:
            self.fail(f"填写页应保留表单标题；页面片段：{clip(page)}")
        # 未勾选联系确认 → 400 保留填写内容
        status, _, page = form_request(self.server, "POST", f"/rfq/{token}", {
            "contact_email": "hdr-b@example.invalid", "contact_phone": "+49 30 000000",
            "quantity": "2", "destination": "Germany", "notes": ""})
        self.assertEqual(status, 400)
        if "请先勾选确认" not in page:
            self.fail(f"未勾选应给出确认提示；页面片段：{clip(page)}")
        self.assert_header(page, TENANT_B_ESCAPED, "验证失败页（未勾选）")
        # 已勾选但缺必填（数量、目的地）→ 400 列出缺项
        status, _, page = form_request(self.server, "POST", f"/rfq/{token}", {
            "contact_email": "hdr-b@example.invalid", "contact_phone": "+49 30 000000", "contact_confirmed": "1"})
        self.assertEqual(status, 400)
        if "请填写：数量" not in page:
            self.fail(f"缺字段应列出缺项；页面片段：{clip(page)}")
        self.assert_header(page, TENANT_B_ESCAPED, "验证失败页（缺字段）")

    def test_submit_and_revision_pages_show_tenant_header(self):
        """提交成功页、再次打开（修订入口）与修订成功页均显示所属企业。"""
        conversation_id, token = self.link_for(TENANT_A)
        status, _, page = form_request(self.server, "POST", f"/rfq/{token}", {
            "contact_email": "hdr-a@example.invalid", "contact_phone": "+49 30 111111",
            "quantity": "3", "destination": "France", "notes": "", "contact_confirmed": "1"})
        self.assertEqual(status, 200)
        if "已提交" not in page:
            self.fail(f"提交成功页应含已提交说明；页面片段：{clip(page)}")
        self.assert_header(page, TENANT_A_ESCAPED, "提交成功页")
        # 重新打开 → 回显既有内容（同一张单的修订入口）
        status, _, page = form_request(self.server, "GET", f"/rfq/{token}")
        self.assertEqual(status, 200)
        if "您已提交过询价" not in page:
            self.fail(f"重新打开应提示更新同一张单；页面片段：{clip(page)}")
        self.assert_header(page, TENANT_A_ESCAPED, "修订入口页")
        # 再次提交 → 更新同一张单（版本 +1），成功页仍显示本企业
        status, _, page = form_request(self.server, "POST", f"/rfq/{token}", {
            "contact_email": "hdr-a@example.invalid", "contact_phone": "+49 30 111111",
            "quantity": "5", "destination": "France", "notes": "数量更新", "contact_confirmed": "1"})
        self.assertEqual(status, 200)
        self.assert_header(page, TENANT_A_ESCAPED, "修订成功页")
        rfq = self.db.conn.execute(
            "SELECT * FROM rfqs WHERE tenant_id=? AND conversation_id=? AND entry_surface='langbot_link'",
            (TENANT_A, conversation_id)).fetchone()
        self.assertIsNotNone(rfq, "提交应建立本企业的询价单")
        self.assertEqual(rfq["current_revision"], 2, "再次提交应更新同一张单")

    def test_invalid_revoked_and_nonchannel_links_are_neutral(self):
        """无效/已撤销/非渠道令牌：GET 与 POST 均 404 + 中性品牌，不泄露企业名。"""
        fake = "A" * 43  # 形态合法但未签发的令牌
        status, _, page = form_request(self.server, "GET", f"/rfq/{fake}")
        self.assert_neutral_404(status, page, "无效链接 GET")
        status, _, page = form_request(self.server, "POST", f"/rfq/{fake}", {"contact_email": "x@example.invalid", "contact_confirmed": "1"})
        self.assert_neutral_404(status, page, "无效链接 POST")
        # 请求参数不能影响身份
        status, _, page = form_request(self.server, "GET", f"/rfq/{fake}?brand=EvilCorp&tenant_id={TENANT_A}")
        self.assert_neutral_404(status, page, "无效链接带参数 GET")
        self.assert_name_absent(page, "EvilCorp", "无效链接带参数 GET")
        # 已撤销令牌
        _, token = self.link_for(TENANT_A)
        row = self.db.get_rfq_form_token(TENANT_A, token)
        self.db.revoke_rfq_form_token(TENANT_A, row["id"])
        status, _, page = form_request(self.server, "GET", f"/rfq/{token}")
        self.assert_neutral_404(status, page, "已撤销链接 GET")
        # 非渠道会话（web）签发的令牌：同样中性，不泄露企业名
        _, token_web = self.link_for(TENANT_B, source="web")
        status, _, page = form_request(self.server, "GET", f"/rfq/{token_web}")
        self.assert_neutral_404(status, page, "非渠道链接 GET")

    def test_malformed_token_paths_return_same_neutral_404(self):
        """令牌形态不合法（空/过短/非法字符/过长）的 /rfq/ 路径：GET 与 POST 均与
        「形态合法但未签发」逐字同响应（404 + 中性 Growave），不落入通用 404。"""
        ref_status, _, ref_get = form_request(self.server, "GET", f"/rfq/{'A' * 43}")
        self.assert_neutral_404(ref_status, ref_get, "基准无效链接 GET")
        ref_status, _, ref_post = form_request(self.server, "POST", f"/rfq/{'A' * 43}", {
            "contact_email": "x@example.invalid", "contact_confirmed": "1"})
        self.assert_neutral_404(ref_status, ref_post, "基准无效链接 POST")
        malformed = (
            "/rfq/t5-invalid-link",        # 15 字符：独立浏览器实测的短令牌路径
            "/rfq/",                       # 空令牌（带尾斜杠）
            "/rfq",                        # 空令牌（无尾斜杠）
            "/rfq/" + "A" * 15 + "!",      # 长度达标但含令牌正则之外的字符
            "/rfq/" + "A" * 97,            # 超过 96 字符上限
        )
        for path in malformed:
            status, _, page = form_request(self.server, "GET", path)
            self.assert_neutral_404(status, page, f"形态不合法 GET {path}")
            if page != ref_get:
                self.fail(f"形态不合法 GET {path} 应与无效令牌响应逐字一致；头部：{clip(self.header_of(page))}")
            status, _, page = form_request(self.server, "POST", path, {
                "contact_email": "x@example.invalid", "contact_confirmed": "1"})
            self.assert_neutral_404(status, page, f"形态不合法 POST {path}")
            if page != ref_post:
                self.fail(f"形态不合法 POST {path} 应与无效令牌响应逐字一致；头部：{clip(self.header_of(page))}")
        # 范围外检查：非 /rfq 的未知路径保持通用 404（本修复不改变其他公开页与路由）
        status, _, page = form_request(self.server, "GET", "/no-such-page")
        self.assertEqual(status, 404)
        if "页面不存在" not in page:
            self.fail(f"非 /rfq 未知路径应保持通用 404 页面；页面片段：{clip(page)}")


if __name__ == "__main__":
    unittest.main()
