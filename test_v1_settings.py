"""V1-01 步骤②③：企业设置（线索阶段 / RFQ 字段 / 通知接收人）+ 操作记录查看页。

用户口径（2026-10-04 验收点清单定稿）：
- RFQ 字段默认：邮箱/电话/数量/目的地=必填，备注=选填；邮箱与电话不可同时隐藏；
- 线索阶段首版仅改名/排序/终态（增删暂缓 F-15）；
- 通知接收人仅登记（渠道未接入、暂不发送）；
- 操作记录=管理员可查看的关键操作审计（时间/操作者/对象/动作）。
"""
import http.client
import json
import tempfile
import threading
import unittest
from pathlib import Path
from urllib.parse import urlencode

from app import Database, make_server


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


def api_request(server, method, path, payload=None, headers=None):
    conn = http.client.HTTPConnection("127.0.0.1", server.server_port)
    merged = {"Content-Type": "application/json", **(headers or {})}
    body = json.dumps(payload).encode("utf-8") if payload is not None else None
    conn.request(method, path, body=body, headers=merged)
    response = conn.getresponse()
    raw = response.read().decode("utf-8")
    status = response.status
    conn.close()
    try:
        return status, json.loads(raw)
    except json.JSONDecodeError:
        return status, raw


FULL_CONTACT = {"contact_email": "buyer@example.com", "contact_phone": "+49 30 555 0400", "quantity": "2", "region": "Germany"}


class SettingsBase(unittest.TestCase):
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
        # 零权限员工（V1-01：默认无任何模块）
        cls.employee_id = cls.db.create_user("demo-tenant", "v1-employee@example.invalid", "零权限员工", "employee", "strong-pass-1", "demo-admin@example.invalid")
        cls.employee = login(cls.server, "v1-employee@example.invalid", "strong-pass-1")

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.db.conn.close()
        cls.thread.join()
        cls.temp_dir.cleanup()


class SettingsPageAccessTests(SettingsBase):
    def test_settings_page_is_admin_only_and_in_nav(self):
        status, _, page = form_request(self.server, "GET", "/admin/settings", cookie=self.admin)
        self.assertEqual(status, 200)
        self.assertIn("企业设置", page)
        self.assertIn("href='/admin/settings'", page)
        self.assertIn("href='/admin/audit'", page)
        # 零权限员工：403 且提示只对管理员开放；导航不出现设置与治理分组
        status, _, page_emp = form_request(self.server, "GET", "/admin/settings", cookie=self.employee)
        self.assertEqual(status, 403)
        self.assertIn("企业设置会影响全企业买家", page_emp)
        _, _, dashboard = form_request(self.server, "GET", "/admin", cookie=self.employee)
        self.assertNotIn("href='/admin/settings'", dashboard)
        self.assertNotIn("href='/admin/audit'", dashboard)
        # 员工全部模块授权后仍不可见/不可进（管理员专属，不参与模块授权）
        self.db.set_user_modules("demo-tenant", self.employee_id, ["products", "tasks", "rfqs", "sales", "sites"], "demo-admin@example.invalid")
        _, _, dashboard2 = form_request(self.server, "GET", "/admin", cookie=self.employee)
        self.assertNotIn("href='/admin/settings'", dashboard2)
        self.assertEqual(form_request(self.server, "GET", "/admin/audit", cookie=self.employee)[0], 403)
        self.db.set_user_modules("demo-tenant", self.employee_id, [], "demo-admin@example.invalid")

    def test_defaults_render_on_settings_page(self):
        _, _, page = form_request(self.server, "GET", "/admin/settings", cookie=self.admin)
        for label in ("邮箱", "电话", "数量", "目的地", "备注"):
            self.assertIn(label, page)
        # 默认三态：四个必填 + 备注选填（select 选中项）
        self.assertEqual(page.count("<option value='required' selected>必填</option>"), 4)
        self.assertIn("<option value='optional' selected>选填</option>", page)
        self.assertIn("渠道未接入、暂不发送", page)


class LeadStageSettingsTests(SettingsBase):
    def _stages_form(self, labels, terminals, move=""):
        form = {}
        for key, label in labels.items():
            form[f"label_{key}"] = label
        for key, checked in terminals.items():
            if checked:
                form[f"terminal_{key}"] = "1"
        if move:
            form["move"] = move
        return form

    def test_rename_reorder_terminal_roundtrip_and_sales_sync(self):
        # 准备一条线索：默认阶段 + 逾期
        conversation_id = self.db.create_conversation("demo-tenant", "settings-test", "en")
        rfq_id, _ = self.db.create_rfq("demo-tenant", conversation_id, "stage-rfq-1", dict(FULL_CONTACT), "stage test")
        # 默认：quote_ready 非终态 → 逾期计入
        self.db.update_lead("demo-tenant", rfq_id, "quote_ready", "2020-01-01", "", "demo-admin@example.invalid")
        summary = self.db.get_admin_summary("demo-tenant")
        self.assertEqual(summary["leads_overdue"], 1)
        labels = {"new": "新线索", "needs_info": "待补充", "technical_review": "技术确认中", "quote_ready": "可以报价了", "paused": "暂缓", "closed": "已关闭"}
        terminals = {"closed": True, "paused": True, "quote_ready": True}
        status, headers, _ = form_request(self.server, "POST", "/admin/settings/stages", self._stages_form(labels, terminals, move="quote_ready:up"), self.admin)
        self.assertEqual(status, 303)
        self.assertIn("notice=stages", headers["Location"])
        stages = self.db.get_lead_stages("demo-tenant")
        self.assertEqual([s["key"] for s in stages], ["new", "needs_info", "quote_ready", "technical_review", "paused", "closed"])
        self.assertEqual({s["key"] for s in stages if s["terminal"]}, {"quote_ready", "paused", "closed"})
        # 终态化后不再计入逾期；数字接口同步
        summary2 = self.db.get_admin_summary("demo-tenant")
        self.assertEqual(summary2["leads_overdue"], 0)
        status, _, body = form_request(self.server, "GET", "/admin/nav-counts", cookie=self.admin)
        self.assertEqual(json.loads(body)["counts"].get("sales", 0), 0)
        # 销售工作台显示改名后的标签；详情页下拉按新顺序
        self.db.set_user_modules("demo-tenant", self.employee_id, ["sales"], "demo-admin@example.invalid")
        sales = login(self.server, "v1-employee@example.invalid", "strong-pass-1")
        _, _, list_page = form_request(self.server, "GET", "/admin/sales", cookie=sales)
        self.assertIn("可以报价了", list_page)
        _, _, detail_page = form_request(self.server, "GET", f"/admin/sales/{rfq_id}", cookie=sales)
        self.assertIn("可以报价了", detail_page)
        self.assertLess(detail_page.index("可以报价了"), detail_page.index("技术确认中"))

    def test_stage_validation_rejects_empty_and_duplicate(self):
        before = [s["label"] for s in self.db.get_lead_stages("demo-tenant")]
        labels = {s["key"]: s["label"] for s in self.db.get_lead_stages("demo-tenant")}
        labels_bad = dict(labels, quote_ready="  ")
        status, headers, _ = form_request(self.server, "POST", "/admin/settings/stages", self._stages_form(labels_bad, {}), self.admin)
        self.assertEqual(status, 303)
        self.assertIn("notice=stages_invalid", headers["Location"])
        labels_dup = dict(labels, quote_ready="待补充")
        status, headers, _ = form_request(self.server, "POST", "/admin/settings/stages", self._stages_form(labels_dup, {}), self.admin)
        self.assertIn("notice=stages_invalid", headers["Location"])
        # 未被改动（快照对比：与测试开始时相同）
        self.assertEqual([s["label"] for s in self.db.get_lead_stages("demo-tenant")], before)


class RfqFieldSettingsTests(SettingsBase):
    def _save_fields(self, states):
        return form_request(self.server, "POST", "/admin/settings/rfq-fields", {f"state_{k}": v for k, v in states.items()}, self.admin)

    def test_both_hidden_rejected_and_saved_state_renders(self):
        self.assertEqual(self._save_fields(dict(email="required", phone="required", quantity="required", destination="required", notes="optional"))[0], 303)
        status, headers, _ = self._save_fields(dict(email="hidden", phone="hidden", quantity="required", destination="required", notes="optional"))
        self.assertEqual(status, 303)
        self.assertIn("notice=fields_both_hidden", headers["Location"])
        self.assertEqual(self.db.get_rfq_field_config("demo-tenant")["phone"], "required")
        # 合法保存（电话隐藏）
        status, headers, _ = self._save_fields(dict(email="required", phone="hidden", quantity="optional", destination="required", notes="hidden"))
        self.assertIn("notice=fields", headers["Location"])
        config = self.db.get_rfq_field_config("demo-tenant")
        self.assertEqual(config["phone"], "hidden")
        self.assertEqual(config["quantity"], "optional")
        self.assertEqual(config["notes"], "hidden")
        _, _, page = form_request(self.server, "GET", "/admin/settings", cookie=self.admin)
        self.assertIn("<option value='hidden' selected>隐藏</option>", page)

    def test_settings_change_writes_audit_and_audit_page_shows_it(self):
        self._save_fields(dict(email="required", phone="required", quantity="required", destination="required", notes="optional"))
        rows = self.db.conn.execute(
            "SELECT details, actor FROM audit_log WHERE tenant_id='demo-tenant' AND entity_type='settings' AND entity_id='rfq_fields' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        self.assertIn("before=", rows["details"])
        self.assertIn("after=", rows["details"])
        self.assertEqual(rows["actor"], "demo-admin@example.invalid")
        _, _, page = form_request(self.server, "GET", "/admin/audit?type=settings", cookie=self.admin)
        self.assertIn("企业设置", page)
        self.assertIn("更新", page)


class NotifyRecipientsTests(SettingsBase):
    def test_save_valid_recipients_and_reject_invalid(self):
        status, headers, _ = form_request(self.server, "POST", "/admin/settings/notify", {"recipients": "ops@example.com\nsales@example.com,ops@example.com"}, self.admin)
        self.assertEqual(status, 303)
        self.assertIn("notice=notify", headers["Location"])
        self.assertEqual(self.db.get_notify_recipients("demo-tenant"), ["ops@example.com", "sales@example.com"])
        status, headers, _ = form_request(self.server, "POST", "/admin/settings/notify", {"recipients": "not-an-email"}, self.admin)
        self.assertIn("notice=notify_invalid", headers["Location"])
        self.assertEqual(self.db.get_notify_recipients("demo-tenant"), ["ops@example.com", "sales@example.com"])
        _, _, page = form_request(self.server, "GET", "/admin/settings", cookie=self.admin)
        self.assertIn("ops@example.com", page)
        self.assertIn("渠道未接入、暂不发送", page)


class RfqFieldEnforcementTests(SettingsBase):
    """三买方入口同口径：API / 旧页 / 产品页注入（产品页在本文件另测）。"""

    def _ensure_site(self):
        if not self.db.get_buyer_site("site-a"):
            self.db.register_buyer_site("demo-tenant", "site-a", "https://seller-a.example")

    def _new_conversation(self):
        self._ensure_site()
        status, body = api_request(self.server, "POST", "/api/v1/buyer/conversations", {"site_key": "site-a"})
        self.assertEqual(status, 201)
        return body["conversation_id"], body["access_token"]

    def test_api_required_fields_and_hidden_and_optional(self):
        self._reset_fields()
        conversation_id, token = self._new_conversation()
        headers = {"X-Site-Key": "site-a", "Authorization": f"Bearer {token}", "Idempotency-Key": "enf-1"}
        path = f"/api/v1/buyer/conversations/{conversation_id}/rfqs"
        # 1) 默认必填：缺项 → 422 + 清单，不落记录
        status, body = api_request(self.server, "POST", path, {"contact_confirmed": True, "contact": {"contact_email": "a@b.co"}}, headers)
        self.assertEqual(status, 422)
        self.assertEqual(body["error"], "rfq_required_fields_missing")
        self.assertIn("电话", body["message"])
        self.assertIn("目的地", body["message"])
        self.assertEqual(len(self.db.list_rfqs("demo-tenant")), 0)
        # 2) 齐全 → 201
        status, body = api_request(self.server, "POST", path, {"contact_confirmed": True, "contact": dict(FULL_CONTACT)}, headers)
        self.assertEqual(status, 201)
        row = self.db.get_rfq("demo-tenant", body["rfq_id"])
        self.assertEqual(row["contact_phone"], "+49 30 555 0400")
        # 3) 隐藏电话：新提交不带电话可过；提交里带了电话也会被丢弃
        self._save_fields(dict(email="required", phone="hidden", quantity="required", destination="required", notes="optional"))
        headers2 = {**headers, "Idempotency-Key": "enf-2"}
        status, body2 = api_request(self.server, "POST", path, {"contact_confirmed": True, "contact": {"contact_email": "c@d.co", "contact_phone": "+49 111", "quantity": "1", "region": "FR"}}, headers2)
        self.assertEqual(status, 201)
        row2 = self.db.get_rfq("demo-tenant", body2["rfq_id"])
        self.assertEqual(row2["contact_phone"], "")
        # 4) 数量改选填：缺失 → needs_info + 还缺 数量
        self._save_fields(dict(email="required", phone="optional", quantity="optional", destination="required", notes="optional"))
        headers3 = {**headers, "Idempotency-Key": "enf-3"}
        status, body3 = api_request(self.server, "POST", path, {"contact_confirmed": True, "contact": {"contact_email": "e@f.co", "region": "NL"}}, headers3)
        self.assertEqual(status, 201)
        missing = body3["missing"]
        self.assertIn("数量", missing)
        self.assertIn("电话", missing)
        self.assertNotIn("目的地", missing)
        self.assertEqual(body3["status"], "needs_info")
        # 5) 会话载荷包含 rfq_fields（聊天窗表单配置来源）
        status, session = api_request(self.server, "GET", f"/api/v1/buyer/conversations/{conversation_id}", headers={"X-Site-Key": "site-a", "Authorization": f"Bearer {token}"})
        self.assertEqual(session["rfq_fields"]["quantity"], "optional")
        self.assertEqual(session["rfq_fields"]["phone"], "optional")

    def _save_fields(self, states):
        return form_request(self.server, "POST", "/admin/settings/rfq-fields", {f"state_{k}": v for k, v in states.items()}, self.admin)

    def _reset_fields(self):
        self._save_fields(dict(email="required", phone="required", quantity="required", destination="required", notes="optional"))

    def test_revise_respects_required_and_hidden(self):
        self._reset_fields()
        conversation_id, token = self._new_conversation()
        headers = {"X-Site-Key": "site-a", "Authorization": f"Bearer {token}"}
        status, body = api_request(self.server, "POST", f"/api/v1/buyer/conversations/{conversation_id}/rfqs", {"contact_confirmed": True, "contact": dict(FULL_CONTACT)}, {**headers, "Idempotency-Key": "rev-1"})
        self.assertEqual(status, 201)
        rfq_id = body["rfq_id"]
        rev_path = f"/api/v1/buyer/conversations/{conversation_id}/rfqs/{rfq_id}/revisions"
        # 清空必填（邮箱）→ 422
        status, body = api_request(self.server, "POST", rev_path, {"expected_revision": 1, "contact_confirmed": True, "contact": {"contact_email": ""}}, {**headers, "Idempotency-Key": "rev-2"})
        self.assertEqual(status, 422)
        self.assertIn("邮箱", body["error"])
        # 隐藏电话后：电话改动被忽略，数量改动生效
        self._save_fields(dict(email="required", phone="hidden", quantity="required", destination="required", notes="optional"))
        status, body = api_request(self.server, "POST", rev_path, {"expected_revision": 1, "contact_confirmed": True, "contact": {"contact_phone": "SHOULD-BE-IGNORED"}, "quantity": "9"}, {**headers, "Idempotency-Key": "rev-3"})
        self.assertEqual(status, 201)
        row = self.db.get_rfq("demo-tenant", rfq_id)
        self.assertEqual(row["contact_phone"], "+49 30 555 0400")
        self.assertEqual(row["quantity"], "9")

    def test_legacy_page_renders_config_and_notice(self):
        self._reset_fields()
        # 默认：页面引导含必填清单；隐藏电话后表单不再渲染该字段
        status, headers, _ = form_request(self.server, "GET", "/inquiry")
        self.assertEqual(status, 303)
        path = headers["Location"].split("?")[0]
        cookie = headers["Set-Cookie"].split(";", 1)[0]
        page = form_request(self.server, "GET", path, cookie=cookie)[2]
        self.assertIn("请填写：邮箱、电话、数量、目的地。", page)
        self.assertIn("name='contact_phone'", page)
        self.assertIn("Email / 邮箱 *", page)
        self._save_fields(dict(email="required", phone="hidden", quantity="required", destination="required", notes="optional"))
        page2 = form_request(self.server, "GET", path, cookie=cookie)[2]
        self.assertNotIn("name='contact_phone'", page2)
        # 缺必填提交 → 303 + notice=rfq_required
        status, headers2, _ = form_request(self.server, "POST", path + "/rfq", {"submission_key": "legacy-req-1", "notes": "x"}, cookie)
        self.assertEqual(status, 303)
        self.assertIn("notice=rfq_required", headers2["Location"])
        # 恢复默认
        self._save_fields(dict(email="required", phone="required", quantity="required", destination="required", notes="optional"))

    def test_product_page_injects_field_config(self):
        self._reset_fields()
        self.db.seed_demo_products("demo-tenant")
        for product in self.db.list_products("demo-tenant"):
            for field in self.db.get_fields("demo-tenant", product["id"]):
                if field["state"] == "candidate":
                    self.db.approve_field("demo-tenant", product["id"], field["field_key"])
            self.db.finalize_product_review("demo-tenant", product["id"])
            self.db.publish_product("demo-tenant", product["id"])
        self._ensure_site()
        product = self.db.list_products("demo-tenant")[0]
        url = f"/s/site-a/products/{product['slug']}"
        _, _, page = form_request(self.server, "GET", url)
        self.assertIn("window.RFQ_FIELD_CONFIG=", page)
        self.assertIn('"phone": "required"', page)
        self.assertIn("name='phone'", page)
        # 隐藏电话后：字段不渲染、注入配置变化
        self._save_fields(dict(email="required", phone="hidden", quantity="required", destination="required", notes="optional"))
        _, _, page2 = form_request(self.server, "GET", url)
        self.assertIn('"phone": "hidden"', page2)
        self.assertNotIn("name='phone'", page2)
        self._save_fields(dict(email="required", phone="required", quantity="required", destination="required", notes="optional"))


class AuditViewTests(SettingsBase):
    def test_admin_view_lists_labeled_rows_and_employee_denied(self):
        # 制造一条有代表性的真实审计：创建产品
        self.db.create_product("demo-tenant", "AUDIT-1", "审计样例泵", "测试", "demo-admin@example.invalid")
        _, _, page = form_request(self.server, "GET", "/admin/audit", cookie=self.admin)
        self.assertIn("操作记录", page)
        self.assertIn("创建 · 产品", page)
        self.assertIn("demo-admin@example.invalid", page)
        self.assertIn("AUDIT-1", page)
        status, _, denied = form_request(self.server, "GET", "/admin/audit", cookie=self.employee)
        self.assertEqual(status, 403)
        self.assertIn("操作记录包含全企业范围", denied)

    def test_filters_and_pagination_and_tenant_isolation(self):
        # 先制造一行 settings 审计（对象筛选的素材）
        form_request(self.server, "POST", "/admin/settings/notify", {"recipients": "audit-check@example.com"}, self.admin)
        total_before = self.db.conn.execute("SELECT COUNT(*) FROM audit_log WHERE tenant_id='demo-tenant'").fetchone()[0]
        with self.db.conn:
            for index in range(55):
                self.db.conn.execute(
                    "INSERT INTO audit_log(tenant_id, entity_type, entity_id, action, actor, details, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    ("demo-tenant", "product", f"bulk-{index:03d}", "created", "bulk-writer", "", "2026-01-01T00:00:00+00:00"),
                )
            self.db.conn.execute(
                "INSERT INTO audit_log(tenant_id, entity_type, entity_id, action, actor, details, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                ("other-tenant", "product", "OTHER-TENANT-ROW", "created", "other-actor", "", "2026-01-01T00:00:00+00:00"),
            )
        total = total_before + 55
        pages = (total + 49) // 50
        _, _, page1 = form_request(self.server, "GET", "/admin/audit", cookie=self.admin)
        self.assertIn(f"共 {total} 条", page1)
        self.assertIn(f"第 1 / {pages} 页", page1)
        self.assertNotIn("OTHER-TENANT-ROW", page1)
        if pages >= 2:
            _, _, last_page = form_request(self.server, "GET", f"/admin/audit?page={pages}", cookie=self.admin)
            self.assertIn(f"第 {pages} / {pages} 页", last_page)
            self.assertIn("bulk-000", last_page)  # 最旧一批在末页
        # 对象筛选：只看 settings
        _, _, settings_page = form_request(self.server, "GET", "/admin/audit?type=settings", cookie=self.admin)
        self.assertIn("企业设置", settings_page)
        self.assertIn("更新", settings_page)
        self.assertNotIn("bulk-0", settings_page)  # 行内容不含批量行（下拉里的 bulk-writer 是操作者选项，不算）
        # 操作者筛选
        _, _, actor_page = form_request(self.server, "GET", "/admin/audit?actor=bulk-writer", cookie=self.admin)
        self.assertIn("共 55 条", actor_page)
        self.assertIn("bulk-054", actor_page)
        # 租户隔离：另一租户只看到自己的行
        other_admin = login(self.server, "second-admin@example.invalid", "second-admin") if self.db.authenticate_user("second-admin@example.invalid", "second-admin") else None
        # 若无 second-admin，直接建一个
        if other_admin is None:
            self.db.create_user("other-tenant", "second-admin@example.invalid", "另一租户管理员", "admin", "second-admin", "setup")
            other_admin = login(self.server, "second-admin@example.invalid", "second-admin")
        _, _, other_page = form_request(self.server, "GET", "/admin/audit", cookie=other_admin)
        self.assertIn("OTHER-TENANT", other_page)  # 列表里 entity_id 截断展示，取前缀断言
        self.assertNotIn("bulk-", other_page)


if __name__ == "__main__":
    unittest.main()
