"""V1-10 基础分析、导出与资料补充任务：口径/可追溯/演示排除/CSV 导出与审计/事件埋点。

用户口径（V1 文档 V1-10）：指标只报告实际事件、不推断成交；统计口径可追溯；
导出遵循权限并记录操作者与时间；资料缺口关联原问题与产品、可形成可分派的修订任务。
"""
import http.client
import tempfile
import threading
import unittest
from pathlib import Path
from urllib.parse import urlencode

from app import make_server


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
    raw = response.read().decode("utf-8", errors="replace")
    headers_out = dict(response.getheaders())
    status = response.status
    conn.close()
    return status, headers_out, raw


def fetch_bytes(server, path):
    conn = http.client.HTTPConnection("127.0.0.1", server.server_port)
    conn.request("GET", path)
    response = conn.getresponse()
    raw = response.read()
    status = response.status
    headers = dict(response.getheaders())
    conn.close()
    return status, headers, raw


def login(server, email, password):
    status, headers, _ = form_request(server, "POST", "/login", {"email": email, "password": password})
    if status not in (302, 303) or "Set-Cookie" not in headers:
        raise AssertionError(f"登录失败: {email} -> {status}")
    return headers["Set-Cookie"].split(";", 1)[0]


FULL_CONTACT = {"contact_email": "buyer@example.com", "contact_phone": "+49 30 555 0500", "quantity": "2", "region": "Germany"}


def publish_product(db, tenant, model):
    product_id = db.create_product(tenant, model, f"{model} 分析样例泵", "测试用途", "setup")
    source_id = db.add_source(tenant, product_id, f"{model}-手册.txt", "TXT", "第 1 节", b"demo analytics content", "setup", scope="exact_model")
    db.save_field(tenant, product_id, "flow_range", "10-20", "m³/h", source_id, "第 1 节", "setup")
    db.approve_field(tenant, product_id, "flow_range")
    db.finalize_product_review(tenant, product_id)
    db.allow_public_source(tenant, product_id, source_id, "setup")
    db.publish_product(tenant, product_id, "setup")
    return product_id, source_id


class AnalyticsBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp_dir = tempfile.TemporaryDirectory()
        cls.server = make_server(Path(cls.temp_dir.name) / "app.db", port=0)
        cls.db = cls.server.RequestHandlerClass.db
        cls.db.seed_demo_users()
        cls.db.ensure_tenant("demo-tenant", "示例水泵企业（演示）")
        cls.db.ensure_tenant("other-tenant", "另一家企业")
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.admin = login(cls.server, "demo-admin@example.invalid", "demo-admin")
        cls.employee_id = cls.db.create_user("demo-tenant", "ana-employee@example.invalid", "零权限员工", "employee", "strong-pass-a", "demo-admin@example.invalid")
        cls.employee = login(cls.server, "ana-employee@example.invalid", "strong-pass-a")

        tenant = "demo-tenant"
        # 场景数据：web 会话 + 已确认工况 + 完整 RFQ（submitted）
        cls.web_conv = cls.db.create_conversation(tenant, "web", "en")
        values = {"flow": {"value": "50", "unit": "m3/h", "label": "流量", "state": "confirmed", "source": "test"}}
        cls.db.create_requirement_revision(tenant, cls.web_conv, values, "confirmed", "setup")
        cls.rfq_submitted, _ = cls.db.create_rfq(tenant, cls.web_conv, "ana-rfq-1", {**FULL_CONTACT, "delivery": "8 weeks"}, "complete")
        # API 会话 + 待补充 RFQ + 未解问题 + 人工任务 + 逾期线索（quote_ready）
        cls.api_conv = cls.db.create_conversation(tenant, "website_api", "en")
        cls.rfq_needs, _ = cls.db.create_rfq(tenant, cls.api_conv, "ana-rfq-2", dict(FULL_CONTACT), "needs")
        cls.db.update_lead(tenant, cls.rfq_needs, "quote_ready", "2020-01-01", "跟进备注", "demo-admin@example.invalid")
        cls.product_id, cls.source_id = publish_product(cls.db, tenant, "ANA-1")
        version = cls.db.get_published_version(tenant, cls.product_id)
        question_id = cls.db.save_message(tenant, cls.api_conv, "buyer", "Can you confirm the price and delivery time?", "buyer-web")
        cls.db.save_answer(tenant, cls.api_conv, question_id, cls.product_id, version["id"], "needs_human", "该问题需要人工确认。", [])
        cls.db.create_human_task(tenant, cls.api_conv, question_id, "Can you confirm the price and delivery time?", "资料外问题", {})
        cls.gap_answer_id = cls.db.conn.execute("SELECT id FROM answers WHERE conversation_id=?", (cls.api_conv,)).fetchone()["id"]
        # 演示夹具会话（用于排除开关）
        cls.demo_conv = cls.db.create_conversation(tenant, "demo-fixture", "zh")
        cls.db.create_requirement_revision(tenant, cls.demo_conv, values, "confirmed", "demo-fixture")
        cls.rfq_demo, _ = cls.db.create_rfq(tenant, cls.demo_conv, "ana-rfq-3", {**FULL_CONTACT, "delivery": "6 weeks"}, "demo")
        # 事件埋点（直接写两条，HTTP 路径另测）
        cls.db.log_product_event(tenant, cls.product_id, "page_view", site_key="site-a", detail="")
        cls.db.log_product_event(tenant, cls.product_id, "download", site_key="site-a", detail="ANA-1-手册.txt;attachment")
        # 站点登记
        cls.db.register_buyer_site(tenant, "site-a", "https://seller-a.example")

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.db.conn.close()
        cls.thread.join()
        cls.temp_dir.cleanup()


class AnalyticsPageTests(AnalyticsBase):
    def test_admin_only_and_nav(self):
        status, _, page = form_request(self.server, "GET", "/admin/analytics", cookie=self.admin)
        self.assertEqual(status, 200)
        for marker in ("分析报表", "RFQ 漏斗", "入口渠道", "资料缺口与高频问题", "资料补充任务", "导出 RFQ"):
            self.assertIn(marker, page)
        self.assertIn("未接入订单数据", page)
        self.assertNotIn("成交率", page.replace("不显示推断成交率", ""))  # 除声明句外不出现成交率
        self.assertIn("href='/admin/analytics'", page)
        status, _, denied = form_request(self.server, "GET", "/admin/analytics", cookie=self.employee)
        self.assertEqual(status, 403)
        self.assertIn("分析报表汇总全企业买家数据", denied)
        _, _, dashboard = form_request(self.server, "GET", "/admin", cookie=self.employee)
        self.assertNotIn("href='/admin/analytics'", dashboard)

    def test_metrics_correct_and_traceable(self):
        report = self.db.analytics_snapshot("demo-tenant")
        self.assertEqual(report["conversations"]["total"], 3)
        self.assertEqual(report["rfqs"]["total"], 3)
        self.assertEqual(report["rfqs"]["submitted"], 2)   # ana-rfq-1 与 ana-rfq-3（完整）
        self.assertEqual(report["rfqs"]["needs_info"], 1)  # ana-rfq-2（缺期望交期）
        self.assertEqual(report["quote_ready"], 1)
        self.assertEqual(report["leads"]["overdue"], 1)
        self.assertEqual(report["answers"]["needs_human"], 1)
        self.assertEqual(report["tasks"]["total"], 1)
        self.assertEqual(report["events"]["page_view"], 1)
        self.assertEqual(report["events"]["download"], 1)
        # 缺项 Top 含「期望交期」
        labels = [label for label, _count in report["rfqs"]["missing_top"]]
        self.assertIn("期望交期", labels)
        # 页面可追溯到具体记录：明细里出现 RFQ 编号前缀与线索
        _, _, page = form_request(self.server, "GET", "/admin/analytics", cookie=self.admin)
        self.assertIn(self.rfq_submitted[:12], page)
        self.assertIn(self.rfq_needs[:12], page)
        self.assertIn("可报价（线索阶段「可进入报价」）", page)

    def test_exclude_demo_toggle(self):
        included = self.db.analytics_snapshot("demo-tenant")
        excluded = self.db.analytics_snapshot("demo-tenant", exclude_demo=True)
        self.assertEqual(included["rfqs"]["total"], 3)
        self.assertEqual(excluded["rfqs"]["total"], 2)  # demo-fixture 会话的 RFQ 被排除
        self.assertEqual(excluded["conversations"]["total"], 2)
        _, _, page = form_request(self.server, "GET", "/admin/analytics?exclude_demo=1", cookie=self.admin)
        self.assertIn("已排除演示夹具数据", page)
        self.assertIn("selected", page)  # 复选框回显

    def test_range_filter(self):
        report_all = self.db.analytics_snapshot("demo-tenant")
        self.db.conn.execute("UPDATE rfqs SET created_at='2020-01-01T00:00:00+00:00' WHERE id=?", (self.rfq_demo,))
        self.db.conn.commit()
        try:
            report_7d = self.db.analytics_snapshot("demo-tenant", days=7)
            self.assertEqual(report_7d["rfqs"]["total"], report_all["rfqs"]["total"] - 1)
            _, _, page = form_request(self.server, "GET", "/admin/analytics?days=7", cookie=self.admin)
            self.assertIn("口径：近 7 天", page)
        finally:
            self.db.conn.execute("UPDATE rfqs SET created_at=? WHERE id=?", ("2026-10-01T00:00:00+00:00", self.rfq_demo))
            self.db.conn.commit()


class MaterialTaskTests(AnalyticsBase):
    def test_gap_visible_and_task_flow(self):
        _, _, page = form_request(self.server, "GET", "/admin/analytics", cookie=self.admin)
        self.assertIn("Can you confirm the price and delivery time?", page)
        # 登记任务（分派给演示员工）
        status, headers, _ = form_request(
            self.server, "POST", "/admin/material-tasks",
            {"source_ref": self.gap_answer_id, "product_id": self.product_id, "assignee": "demo-sales@example.invalid"},
            self.admin,
        )
        self.assertEqual(status, 303)
        self.assertIn("notice=material_saved", headers["Location"])
        task = self.db.conn.execute("SELECT * FROM material_tasks WHERE source_ref=?", (self.gap_answer_id,)).fetchone()
        self.assertIsNotNone(task)
        self.assertEqual(task["status"], "open")
        self.assertEqual(task["assignee"], "demo-sales@example.invalid")
        self.assertIn("ANA-1", task["title"])
        audit = self.db.conn.execute(
            "SELECT * FROM audit_log WHERE entity_type='material_task' AND action='created' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        self.assertIn("assignee=demo-sales@example.invalid", audit["details"])
        # 页面：缺口行显示已登记 + 任务列表出现
        _, _, page2 = form_request(self.server, "GET", "/admin/analytics", cookie=self.admin)
        self.assertIn("已登记任务", page2)
        self.assertIn("资料补充 · ANA-1", page2)
        # 重复登记被拒
        status, headers, _ = form_request(
            self.server, "POST", "/admin/material-tasks",
            {"source_ref": self.gap_answer_id, "product_id": self.product_id, "assignee": ""},
            self.admin,
        )
        self.assertIn("notice=material_duplicate", headers["Location"])
        self.assertEqual(self.db.conn.execute("SELECT COUNT(*) FROM material_tasks").fetchone()[0], 1)
        # 无效接收人被拒
        status, headers, _ = form_request(
            self.server, "POST", "/admin/material-tasks",
            {"source_ref": "", "product_id": self.product_id, "assignee": "ghost@example.invalid"},
            self.admin,
        )
        self.assertIn("notice=material_invalid", headers["Location"])
        # 完成
        status, headers, _ = form_request(self.server, "POST", f"/admin/material-tasks/{task['id']}/complete", {}, self.admin)
        self.assertEqual(status, 303)
        self.assertIn("notice=material_done", headers["Location"])
        done = self.db.conn.execute("SELECT * FROM material_tasks WHERE id=?", (task["id"],)).fetchone()
        self.assertEqual(done["status"], "done")
        self.assertEqual(done["completed_by"], "demo-admin@example.invalid")
        audit_done = self.db.conn.execute(
            "SELECT * FROM audit_log WHERE entity_type='material_task' AND action='completed' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        self.assertIsNotNone(audit_done)
        # 完成后缺口行恢复可登记（无 open 任务）→ 不显示已登记徽标
        _, _, page3 = form_request(self.server, "GET", "/admin/analytics", cookie=self.admin)
        self.assertIn("登记资料补充任务", page3)


class GapWithoutProductTest(AnalyticsBase):
    def test_gap_without_product_shows_note_only(self):
        # 无产品关联的未解问题：只给说明，不提供登记表单（后端同样会拒绝空产品）
        msg_id = self.db.save_message("demo-tenant", self.web_conv, "buyer", "Do you deliver to Norway?", "buyer-web")
        self.db.save_answer("demo-tenant", self.web_conv, msg_id, None, None, "needs_human", "需要人工确认。", [])
        _, _, page = form_request(self.server, "GET", "/admin/analytics", cookie=self.admin)
        self.assertIn("Do you deliver to Norway?", page)
        self.assertIn("未关联产品，无法登记修订任务", page)


class ExportTests(AnalyticsBase):
    def test_csv_export_admin_only_and_audit(self):
        # RFQ 导出
        status, headers, raw = fetch_bytes(self.server, "/admin/export/rfqs.csv")
        # 未登录：被重定向到登录（非 200 文件）
        self.assertNotEqual(status, 200)
        status, headers, raw = self._fetch("/admin/export/rfqs.csv", self.admin)
        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Type"], "text/csv; charset=utf-8")
        self.assertIn("attachment", headers["Content-Disposition"])
        self.assertTrue(raw.startswith("\ufeff".encode("utf-8")))
        text = raw.decode("utf-8-sig")
        lines = [line for line in text.splitlines() if line.strip()]
        self.assertTrue(lines[0].startswith("rfq_id,"))
        self.assertEqual(len(lines), 3 + 1)  # 3 条 RFQ + 表头
        self.assertIn(self.rfq_submitted, text)
        # 导出留审计（操作者与时间）
        audit = self.db.conn.execute(
            "SELECT * FROM audit_log WHERE entity_type='export' AND entity_id='rfqs' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        self.assertEqual(audit["action"], "exported")
        self.assertEqual(audit["actor"], "demo-admin@example.invalid")
        self.assertIn("rows=3", audit["details"])
        self.assertTrue(audit["created_at"])
        # 另两个数据集
        for name, header in (("leads", "lead_id,"), ("products", "model,")):
            status, _headers, raw2 = self._fetch(f"/admin/export/{name}.csv", self.admin)
            self.assertEqual(status, 200)
            self.assertTrue(raw2.decode("utf-8-sig").startswith(header))
        # 员工被拒
        status, _, denied = form_request(self.server, "GET", "/admin/export/rfqs.csv", cookie=self.employee)
        self.assertEqual(status, 403)
        self.assertIn("数据导出包含买方联系信息", denied)

    def test_export_tenant_isolation(self):
        self.db.create_user("other-tenant", "other-admin@example.invalid", "另一企业管理员", "admin", "other-admin", "setup")
        other = login(self.server, "other-admin@example.invalid", "other-admin")
        status, headers, raw = self._fetch("/admin/export/rfqs.csv", other)
        self.assertEqual(status, 200)
        text = raw.decode("utf-8-sig")
        self.assertEqual(len([line for line in text.splitlines() if line.strip()]), 1)  # 仅表头
        self.assertNotIn(self.rfq_submitted, text)

    def _fetch(self, path, cookie):
        conn = http.client.HTTPConnection("127.0.0.1", self.server.server_port)
        conn.request("GET", path, headers={"Cookie": cookie})
        response = conn.getresponse()
        raw = response.read()
        status = response.status
        headers = dict(response.getheaders())
        conn.close()
        return status, headers, raw


class ProductEventTests(AnalyticsBase):
    def test_page_view_and_download_logged_from_http(self):
        before = self.db.conn.execute(
            "SELECT COUNT(*) FROM product_events WHERE product_id=? AND event='page_view'", (self.product_id,)
        ).fetchone()[0]
        status, _, page = form_request(self.server, "GET", "/s/site-a/products/ana-1")
        self.assertEqual(status, 200)
        self.assertIn("ANA-1", page)
        after = self.db.conn.execute(
            "SELECT COUNT(*) FROM product_events WHERE product_id=? AND event='page_view'", (self.product_id,)
        ).fetchone()[0]
        self.assertEqual(after, before + 1)
        # 下载事件
        before_dl = self.db.conn.execute(
            "SELECT COUNT(*) FROM product_events WHERE product_id=? AND event='download'", (self.product_id,)
        ).fetchone()[0]
        status, headers, raw = fetch_bytes(self.server, f"/s/site-a/products/ana-1/sources/{self.source_id}")
        self.assertEqual(status, 200)
        self.assertTrue(raw.startswith(b"demo analytics"))
        after_dl = self.db.conn.execute(
            "SELECT COUNT(*) FROM product_events WHERE product_id=? AND event='download'", (self.product_id,)
        ).fetchone()[0]
        self.assertEqual(after_dl, before_dl + 1)
        detail = self.db.conn.execute(
            "SELECT detail FROM product_events WHERE product_id=? AND event='download' ORDER BY id DESC LIMIT 1", (self.product_id,)
        ).fetchone()[0]
        self.assertIn("ANA-1-手册.txt", detail)
        self.assertIn("attachment", detail)

    def test_events_shown_on_analytics(self):
        _, _, page = form_request(self.server, "GET", "/admin/analytics", cookie=self.admin)
        self.assertIn("产品页浏览与下载", page)
        self.assertIn("ANA-1", page)


if __name__ == "__main__":
    unittest.main()
