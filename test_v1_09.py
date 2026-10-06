"""V1-09 销售工作台增量：线索筛选与提醒、跟进节点、关闭原因与重新激活、回复草稿（发送前检查停止联系）。

用户口径（V1 文档 V1-09）：负责人/阶段/下一步变更保存并可审计；到期与逾期可见；
未获授权的自动发送不发生，发送状态清楚区分；关闭线索需记录原因、后续可重新激活。
"""
import http.client
import tempfile
import threading
import unittest
from datetime import datetime, timezone
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


def login(server, email, password):
    status, headers, _ = form_request(server, "POST", "/login", {"email": email, "password": password})
    if status not in (302, 303) or "Set-Cookie" not in headers:
        raise AssertionError(f"登录失败: {email} -> {status}")
    return headers["Set-Cookie"].split(";", 1)[0]


def contact_for(name, company):
    return {"contact_name": name, "company": company, "contact_email": "buyer@example.com", "contact_phone": "+49 30 555 0600", "quantity": "2", "region": "Germany", "delivery": "8 weeks"}


class SalesBase(unittest.TestCase):
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
        cls.sales = login(cls.server, "demo-sales@example.invalid", "demo-sales")
        cls.outsider_id = cls.db.create_user("demo-tenant", "v109-outsider@example.invalid", "无模块员工", "employee", "strong-pass-1", "demo-admin@example.invalid")
        cls.outsider = login(cls.server, "v109-outsider@example.invalid", "strong-pass-1")

        tenant = "demo-tenant"
        today = datetime.now(timezone.utc).date().isoformat()

        def make_lead(key, company, stage, follow_up, owner):
            conversation = cls.db.create_conversation(tenant, "web", "en")
            rfq_id, _ = cls.db.create_rfq(tenant, conversation, key, contact_for("Buyer", company), "note")
            cls.db.update_lead(tenant, rfq_id, stage, follow_up, f"跟进备注 {company}", "demo-admin@example.invalid")
            if owner:
                cls.db.assign_conversation_leads(tenant, rfq_id, owner, "demo-admin@example.invalid")
            return rfq_id

        # rfq_overdue：销售负责、逾期；rfq_today：未分配、今日到期；rfq_plain：销售负责、未安排跟进
        cls.rfq_overdue = make_lead("v109-rfq-1", "Nordstern Cooling GmbH", "needs_info", "2020-01-01", "demo-sales@example.invalid")
        cls.rfq_today = make_lead("v109-rfq-2", "华东冷却设备有限公司", "quote_ready", today, None)
        cls.rfq_plain = make_lead("v109-rfq-3", "Blue Harbor Pumps Ltd", "new", "", "demo-sales@example.invalid")

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.db.conn.close()
        cls.thread.join()
        cls.temp_dir.cleanup()


class SalesListTests(SalesBase):
    def test_filters_and_reminder_strip(self):
        today = datetime.now(timezone.utc).date().isoformat()
        _, _, page = form_request(self.server, "GET", "/admin/sales", cookie=self.admin)
        self.assertIn("跟进提醒", page)
        self.assertIn("逾期 1", page)
        self.assertIn("今日到期 1", page)
        self.assertIn("未分配 1", page)
        for rfq_id in (self.rfq_overdue, self.rfq_today, self.rfq_plain):
            self.assertIn(rfq_id, page)
        # 时效筛选：逾期
        _, _, overdue_page = form_request(self.server, "GET", "/admin/sales?timing=overdue", cookie=self.admin)
        self.assertIn(self.rfq_overdue, overdue_page)
        self.assertNotIn(self.rfq_today, overdue_page)
        self.assertNotIn(self.rfq_plain, overdue_page)
        self.assertIn("已筛选 1 / 3 条", overdue_page)
        # 负责人=未分配
        _, _, unassigned_page = form_request(self.server, "GET", "/admin/sales?owner=unassigned", cookie=self.admin)
        self.assertIn(self.rfq_today, unassigned_page)
        self.assertNotIn(self.rfq_overdue, unassigned_page)
        # 阶段筛选（配置中的 quote_ready）
        _, _, stage_page = form_request(self.server, "GET", "/admin/sales?stage=quote_ready", cookie=self.admin)
        self.assertIn(self.rfq_today, stage_page)
        self.assertNotIn(self.rfq_overdue, stage_page)
        # 关键词（公司名）
        _, _, kw_page = form_request(self.server, "GET", "/admin/sales?q=Blue%20Harbor", cookie=self.admin)
        self.assertIn(self.rfq_plain, kw_page)
        self.assertNotIn(self.rfq_today, kw_page)
        # 组合：今日到期 + 未分配
        _, _, combo_page = form_request(self.server, "GET", f"/admin/sales?timing=today&owner=unassigned", cookie=self.admin)
        self.assertIn(self.rfq_today, combo_page)
        self.assertNotIn(self.rfq_overdue, combo_page)

    def test_detail_page_renders_new_sections(self):
        _, _, page = form_request(self.server, "GET", f"/admin/sales/{self.rfq_overdue}", cookie=self.admin)
        for marker in ("跟进节点", "技术确认", "样品准备", "报价准备", "回复草稿（跟进记录）", "关闭原因（阶段设为「已关闭」时必填）", "draft → queued → sent/failed", "保存草稿"):
            self.assertIn(marker, page)
        # 「发送渠道」「记录为已发送（线下）」「经渠道发送」只在存在未发送草稿时渲染（由回复流测试覆盖）


class MilestoneTests(SalesBase):
    def test_toggle_and_audit_and_invalid_key(self):
        path = f"/admin/sales/{self.rfq_overdue}/milestones/tech_confirmed"
        status, headers, _ = form_request(self.server, "POST", path, {}, self.sales)
        self.assertEqual(status, 303)
        self.assertIn("notice=milestone_on", headers["Location"])
        lead_id = self.db.get_lead_for_rfq("demo-tenant", self.rfq_overdue)["id"]
        row = self.db.conn.execute("SELECT * FROM lead_milestones WHERE lead_id=? AND key='tech_confirmed'", (lead_id,)).fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row["done_by"], "demo-sales@example.invalid")
        audit = self.db.conn.execute(
            "SELECT * FROM audit_log WHERE entity_id=? AND action='milestone_completed' ORDER BY id DESC LIMIT 1", (lead_id,)
        ).fetchone()
        self.assertIn("key=tech_confirmed", audit["details"])
        _, _, page = form_request(self.server, "GET", f"/admin/sales/{self.rfq_overdue}", cookie=self.admin)
        self.assertIn("已完成 · demo-sales@example.invalid", page)
        # 重新打开
        status, headers, _ = form_request(self.server, "POST", path, {}, self.sales)
        self.assertIn("notice=milestone_off", headers["Location"])
        self.assertIsNone(self.db.conn.execute("SELECT * FROM lead_milestones WHERE lead_id=? AND key='tech_confirmed'", (lead_id,)).fetchone())
        audit2 = self.db.conn.execute(
            "SELECT * FROM audit_log WHERE entity_id=? AND action='milestone_reopened' ORDER BY id DESC LIMIT 1", (lead_id,)
        ).fetchone()
        self.assertIsNotNone(audit2)
        # 不支持的节点
        status, headers, _ = form_request(self.server, "POST", f"/admin/sales/{self.rfq_overdue}/milestones/bogus", {}, self.sales)
        self.assertIn("notice=milestone_invalid", headers["Location"])


class CloseReasonTests(SalesBase):
    def _update(self, rfq_id, fields):
        return form_request(self.server, "POST", f"/admin/sales/{rfq_id}/update", fields, self.admin)

    def test_close_requires_reason_and_reactivation(self):
        # 缺原因 → 拒绝
        status, headers, _ = self._update(self.rfq_overdue, {"stage": "closed", "next_follow_up": "2020-01-01", "note": "x", "close_reason": ""})
        self.assertEqual(status, 303)
        self.assertIn("notice=close_reason", headers["Location"])
        lead = self.db.get_lead_for_rfq("demo-tenant", self.rfq_overdue)
        self.assertEqual(lead["stage"], "needs_info")
        self.assertIsNone(lead["closed_at"])
        # 带原因 → 关闭 + 落库
        status, headers, _ = self._update(self.rfq_overdue, {"stage": "closed", "next_follow_up": "2020-01-01", "note": "x", "close_reason": "客户取消项目，转用竞品"})
        self.assertIn("notice=updated", headers["Location"])
        lead = self.db.get_lead_for_rfq("demo-tenant", self.rfq_overdue)
        self.assertEqual(lead["stage"], "closed")
        self.assertEqual(lead["close_reason"], "客户取消项目，转用竞品")
        self.assertTrue(lead["closed_at"])
        audit = self.db.conn.execute(
            "SELECT * FROM audit_log WHERE entity_id=? AND action='updated' ORDER BY id DESC LIMIT 1", (lead["id"],)
        ).fetchone()
        self.assertIn("transition=closed", audit["details"])
        self.assertIn("close_reason=客户取消项目", audit["details"])
        _, _, page = form_request(self.server, "GET", f"/admin/sales/{self.rfq_overdue}", cookie=self.admin)
        self.assertIn("重新选择其他阶段即可重新激活", page)
        # 重新激活（换回非关闭阶段）→ closed_at 清空 + 审计 reopened
        status, headers, _ = self._update(self.rfq_overdue, {"stage": "technical_review", "next_follow_up": "2020-01-01", "note": "重新激活", "close_reason": ""})
        self.assertIn("notice=updated", headers["Location"])
        lead = self.db.get_lead_for_rfq("demo-tenant", self.rfq_overdue)
        self.assertEqual(lead["stage"], "technical_review")
        self.assertIsNone(lead["closed_at"])
        self.assertEqual(lead["close_reason"], "")
        audit2 = self.db.conn.execute(
            "SELECT * FROM audit_log WHERE entity_id=? AND action='updated' ORDER BY id DESC LIMIT 1", (lead["id"],)
        ).fetchone()
        self.assertIn("transition=reopened", audit2["details"])

    def test_stop_conflict_uses_friendly_notice(self):
        conversation_id = self.db.get_lead_for_rfq("demo-tenant", self.rfq_plain)["conversation_id"]
        self.db.stop_proactive_contact("demo-tenant", conversation_id, "请勿主动联系", "buyer_chat", "buyer")
        status, headers, _ = self._update(self.rfq_plain, {"stage": "needs_info", "next_follow_up": "2030-01-01", "note": "", "close_reason": ""})
        self.assertIn("notice=stop_conflict", headers["Location"])
        lead = self.db.get_lead_for_rfq("demo-tenant", self.rfq_plain)
        self.assertIsNone(lead["next_follow_up"])


class ReplyDraftTests(SalesBase):
    def test_full_reply_flow_statuses_and_audit(self):
        # 1) 新建草稿
        status, headers, _ = form_request(self.server, "POST", f"/admin/sales/{self.rfq_plain}/replies", {"body": "Dear buyer, thanks for your request."}, self.sales)
        self.assertEqual(status, 303)
        self.assertIn("notice=reply_saved", headers["Location"])
        lead_id = self.db.get_lead_for_rfq("demo-tenant", self.rfq_plain)["id"]
        reply = self.db.conn.execute("SELECT * FROM lead_replies WHERE lead_id=? ORDER BY created_at DESC LIMIT 1", (lead_id,)).fetchone()
        self.assertEqual(reply["status"], "draft")
        self.assertEqual(reply["created_by"], "demo-sales@example.invalid")
        # 页面显示三种状态所需的文案与操作
        _, _, page = form_request(self.server, "GET", f"/admin/sales/{self.rfq_plain}", cookie=self.sales)
        self.assertIn("未发送草稿", page)
        self.assertIn("记录为已发送（线下）", page)
        self.assertIn("废弃草稿", page)
        # 2) 编辑草稿
        status, headers, _ = form_request(self.server, "POST", f"/admin/sales/{self.rfq_plain}/replies/{reply['id']}", {"action": "save", "body": "Updated draft body."}, self.sales)
        self.assertIn("notice=reply_saved", headers["Location"])
        self.assertEqual(self.db.conn.execute("SELECT body FROM lead_replies WHERE id=?", (reply["id"],)).fetchone()["body"], "Updated draft body.")
        audit_update = self.db.conn.execute("SELECT * FROM audit_log WHERE entity_id=? AND action='updated'", (reply["id"],)).fetchone()
        self.assertEqual(audit_update["actor"], "demo-sales@example.invalid")
        # 3) 记录发送（带渠道；同时保存最新正文）
        status, headers, _ = form_request(self.server, "POST", f"/admin/sales/{self.rfq_plain}/replies/{reply['id']}", {"action": "sent", "channel": "email", "body": "Final sent text."}, self.sales)
        self.assertIn("notice=reply_sent", headers["Location"])
        sent = self.db.conn.execute("SELECT * FROM lead_replies WHERE id=?", (reply["id"],)).fetchone()
        self.assertEqual(sent["status"], "sent")
        self.assertEqual(sent["channel"], "email")
        self.assertEqual(sent["body"], "Final sent text.")
        self.assertEqual(sent["sent_by"], "demo-sales@example.invalid")
        self.assertTrue(sent["sent_at"])
        audit_sent = self.db.conn.execute("SELECT * FROM audit_log WHERE entity_id=? AND action='sent'", (reply["id"],)).fetchone()
        self.assertIn("channel=email", audit_sent["details"])
        # 4) 已发送的不可再改/再发
        status, headers, _ = form_request(self.server, "POST", f"/admin/sales/{self.rfq_plain}/replies/{reply['id']}", {"action": "save", "body": "nope"}, self.sales)
        self.assertIn("notice=reply_invalid", headers["Location"])
        # 5) 新建第二条草稿 → 废弃
        form_request(self.server, "POST", f"/admin/sales/{self.rfq_plain}/replies", {"body": "Second draft"}, self.sales)
        draft2 = self.db.conn.execute("SELECT * FROM lead_replies WHERE lead_id=? AND status='draft'", (lead_id,)).fetchone()
        status, headers, _ = form_request(self.server, "POST", f"/admin/sales/{self.rfq_plain}/replies/{draft2['id']}", {"action": "discard"}, self.sales)
        self.assertIn("notice=reply_discarded", headers["Location"])
        self.assertEqual(self.db.conn.execute("SELECT status FROM lead_replies WHERE id=?", (draft2["id"],)).fetchone()["status"], "discarded")
        # 6) 空内容被拒
        status, headers, _ = form_request(self.server, "POST", f"/admin/sales/{self.rfq_plain}/replies", {"body": "   "}, self.sales)
        self.assertIn("notice=reply_invalid", headers["Location"])
        # 7) 线索详情页展示发送状态（已记录发送（邮件））
        _, _, page2 = form_request(self.server, "GET", f"/admin/sales/{self.rfq_plain}", cookie=self.sales)
        self.assertIn("已记录发送（邮件）", page2)
        self.assertIn("Final sent text.", page2)

    def test_send_blocked_when_stop_requested(self):
        # 停止联系后：允许存草稿，但记录发送被阻止（发送前检查）
        conversation_id = self.db.get_lead_for_rfq("demo-tenant", self.rfq_today)["conversation_id"]
        self.db.stop_proactive_contact("demo-tenant", conversation_id, "请不要联系", "buyer_chat", "buyer")
        status, headers, _ = form_request(self.server, "POST", f"/admin/sales/{self.rfq_today}/replies", {"body": "Should not be sent"}, self.sales)
        self.assertIn("notice=reply_saved", headers["Location"])
        lead_id = self.db.get_lead_for_rfq("demo-tenant", self.rfq_today)["id"]
        draft = self.db.conn.execute("SELECT * FROM lead_replies WHERE lead_id=? ORDER BY created_at DESC LIMIT 1", (lead_id,)).fetchone()
        status, headers, _ = form_request(self.server, "POST", f"/admin/sales/{self.rfq_today}/replies/{draft['id']}", {"action": "sent", "channel": "email", "body": draft["body"]}, self.sales)
        self.assertIn("notice=reply_stop_blocked", headers["Location"])
        still = self.db.conn.execute("SELECT status FROM lead_replies WHERE id=?", (draft["id"],)).fetchone()
        self.assertEqual(still["status"], "draft")
        # 页面提示：仍是未发送草稿、没有「已记录发送（邮件）」状态
        _, _, page = form_request(self.server, "GET", f"/admin/sales/{self.rfq_today}", cookie=self.sales)
        self.assertIn("未发送草稿", page)
        self.assertNotIn("已记录发送（邮件）", page)
        # 清理：解除停止（供其它测试的计数不受影响——本测试在类内按字母序最先执行后的独立状态）
        with self.db.conn:
            self.db.conn.execute("UPDATE conversations SET stop_proactive_contact=0, stop_reason='', stop_source='', stopped_at=NULL WHERE tenant_id='demo-tenant' AND id=?", (conversation_id,))

    def test_module_and_tenant_isolation(self):
        # 无销售模块的员工：路由门禁 403
        status, _, denied = form_request(self.server, "POST", f"/admin/sales/{self.rfq_plain}/replies", {"body": "x"}, self.outsider)
        self.assertEqual(status, 403)
        self.assertIn("销售工作台", denied)
        # 另一企业：视为线索不存在
        self.db.create_user("other-tenant", "v109-other-admin@example.invalid", "另一企业管理员", "admin", "other-admin", "setup")
        other = login(self.server, "v109-other-admin@example.invalid", "other-admin")
        status, headers, _ = form_request(self.server, "POST", f"/admin/sales/{self.rfq_plain}/replies", {"body": "cross tenant"}, other)
        self.assertEqual(status, 303)
        self.assertIn("notice=reply_invalid", headers["Location"])
        status, headers, _ = form_request(self.server, "POST", f"/admin/sales/{self.rfq_plain}/milestones/tech_confirmed", {}, other)
        self.assertIn("notice=milestone_invalid", headers["Location"])


if __name__ == "__main__":
    unittest.main()
