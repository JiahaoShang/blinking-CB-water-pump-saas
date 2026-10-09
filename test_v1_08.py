"""V1-08 增量（批次四 · 后段）测试。

切片5「待培育线索」：leads 表重建迁移（旧库幂等、子表保留、FK 回升）、
创建校验与审计、HTTP 全链路（登记/列表/详情/更新/节点/关闭）、模块门禁与分配。
"""
import http.client
import tempfile
import threading
import unittest
from pathlib import Path
from urllib.parse import urlencode

from app import Database, SETTING_RFQ_FIELDS, make_server


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


class LegacyLeadsMigrationTests(unittest.TestCase):
    """旧库 leads 重建迁移：kind 回填、子表（里程碑/回复）保留、FK 开关回升、幂等。"""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "legacy.db"

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_rebuild_migration_keeps_child_rows_and_reenables_fk(self):
        db = Database(self.db_path)
        db.seed_demo_users()
        db.ensure_tenant("demo-tenant", "示例水泵企业（演示）")
        db.seed_demo_workspace("demo-tenant")
        lead = db.conn.execute("SELECT id, rfq_id, conversation_id FROM leads LIMIT 1").fetchone()
        lead_id, rfq_id, conversation_id = lead["id"], lead["rfq_id"], lead["conversation_id"]
        # 回退为 V1-09 时期的旧 leads 结构（无 kind 等新列）
        db.conn.execute("DROP TABLE leads")
        db.conn.execute(
            """CREATE TABLE leads (
                id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL REFERENCES tenants(id),
                rfq_id TEXT NOT NULL REFERENCES rfqs(id), conversation_id TEXT NOT NULL REFERENCES conversations(id),
                stage TEXT NOT NULL DEFAULT 'new', owner TEXT, next_follow_up TEXT, note TEXT NOT NULL DEFAULT '',
                close_reason TEXT NOT NULL DEFAULT '', closed_at TEXT,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL, UNIQUE (tenant_id, rfq_id))"""
        )
        db.conn.execute(
            "INSERT INTO leads(id, tenant_id, rfq_id, conversation_id, stage, owner, next_follow_up, note, created_at, updated_at) "
            "VALUES (?, 'demo-tenant', ?, ?, 'technical_review', 'demo-sales@example.invalid', '2026-10-20', '旧备注', '2026-01-01', '2026-01-02')",
            (lead_id, rfq_id, conversation_id),
        )
        db.conn.execute(
            "INSERT INTO lead_milestones(tenant_id, lead_id, key, done_by, done_at) VALUES ('demo-tenant', ?, 'tech_confirmed', 'demo-sales@example.invalid', '2026-01-03')",
            (lead_id,),
        )
        db.conn.execute(
            "INSERT INTO lead_replies(id, tenant_id, lead_id, body, status, channel, created_by, created_at, updated_at) "
            "VALUES ('R1','demo-tenant',?, '旧草稿','draft','','demo-sales@example.invalid','2026-01-03','2026-01-03')",
            (lead_id,),
        )
        db.conn.commit()
        db.conn.close()

        migrated = Database(self.db_path)  # 重开触发迁移
        try:
            row = migrated.conn.execute("SELECT * FROM leads WHERE id=?", (lead_id,)).fetchone()
            self.assertEqual(row["kind"], "rfq")  # 旧线索回填为 RFQ 线索
            self.assertEqual(row["stage"], "technical_review")
            self.assertEqual(row["note"], "旧备注")
            self.assertIsNone(row["merged_into"])
            # 子表保留
            self.assertEqual(migrated.conn.execute("SELECT COUNT(*) FROM lead_milestones WHERE lead_id=?", (lead_id,)).fetchone()[0], 1)
            self.assertEqual(migrated.conn.execute("SELECT COUNT(*) FROM lead_replies WHERE lead_id=?", (lead_id,)).fetchone()[0], 1)
            # FK 在同一连接上回升，且无悬挂引用
            self.assertEqual(migrated.conn.execute("PRAGMA foreign_keys").fetchone()[0], 1)
            self.assertEqual(migrated.conn.execute("PRAGMA foreign_key_check").fetchall(), [])
        finally:
            migrated.conn.close()
        # 幂等：再开一次不重复迁移
        again = Database(self.db_path)
        try:
            self.assertEqual(again.conn.execute("SELECT COUNT(*) FROM leads").fetchone()[0], 1)
        finally:
            again.conn.close()


class NurtureLeadTests(unittest.TestCase):
    maxDiff = None

    @classmethod
    def setUpClass(cls):
        cls.temp_dir = tempfile.TemporaryDirectory()
        cls.server = make_server(Path(cls.temp_dir.name) / "app.db", port=0)
        cls.db = cls.server.RequestHandlerClass.db
        cls.db.seed_demo_users()
        cls.db.ensure_tenant("demo-tenant", "示例水泵企业（演示）")
        cls.sales_id = cls.db.create_user(
            "demo-tenant", "n-sales@example.invalid", "培育销售", "employee", "strong-pass", "demo-admin"
        )
        cls.db.set_user_modules("demo-tenant", cls.sales_id, ["sales"], "demo-admin@example.invalid")
        cls.nobody_id = cls.db.create_user(
            "demo-tenant", "n-nobody@example.invalid", "无权限同事", "employee", "strong-pass", "demo-admin"
        )
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.admin_cookie = login(cls.server, "demo-admin@example.invalid", "demo-admin")
        cls.sales_cookie = login(cls.server, "n-sales@example.invalid", "strong-pass")
        cls.nobody_cookie = login(cls.server, "n-nobody@example.invalid", "strong-pass")

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.db.conn.close()
        cls.thread.join()
        cls.temp_dir.cleanup()

    def _create(self, **overrides):
        fields = {
            "contact_name": "王工", "company": "华东水务工程公司",
            "contact_email": "wang@huadong.example", "contact_phone": "",
            "source": "expo", "note": "展会接触，Q4 招标", "consent_confirmed": "1",
        }
        fields.update(overrides)
        return form_request(self.server, "POST", "/admin/sales/nurture", fields, self.sales_cookie)

    def test_create_nurture_validation_and_audit(self):
        # 校验：来源无效 / 无联系方式 / 无称呼公司
        with self.assertRaises(ValueError):
            self.db.create_nurture_lead("demo-tenant", "x", "王工", "公司", "a@b.c", "", "bad-source", "", consent_confirmed=True)
        with self.assertRaises(ValueError):
            self.db.create_nurture_lead("demo-tenant", "x", "王工", "公司", "", "", "expo", "", consent_confirmed=True)
        with self.assertRaises(ValueError):
            self.db.create_nurture_lead("demo-tenant", "x", "", "", "a@b.c", "", "expo", "", consent_confirmed=True)
        lead_id = self.db.create_nurture_lead(
            "demo-tenant", "n-sales@example.invalid", "李工", "北方泵业", "", "+86 138 0000 0000", "phone", "电话询价"
        , consent_confirmed=True)
        lead = self.db.get_lead("demo-tenant", lead_id)
        self.assertEqual((lead["kind"], lead["stage"], lead["owner"]), ("nurture", "new", "n-sales@example.invalid"))
        self.assertEqual(lead["rfq_id"], None)
        audit = self.db.conn.execute(
            "SELECT details FROM audit_log WHERE entity_id=? AND action='nurture_created'", (lead_id,)
        ).fetchone()
        self.assertIn("电话", audit["details"])
        # 只出现在待培育列表
        self.assertIn(lead_id, [row["id"] for row in self.db.list_nurture_leads("demo-tenant")])
        # 逾期计数纳入统计（摘要口径）
        self.db.update_lead_by_id("demo-tenant", lead_id, "new", "2020-01-01", "", "n-sales@example.invalid")
        self.assertGreaterEqual(self.db.get_admin_summary("demo-tenant")["leads_overdue"], 1)

    def test_http_create_list_detail_update_and_milestones(self):
        # 登记 → 跳详情
        status, headers, _ = self._create()
        self.assertEqual(status, 303)
        location = headers["Location"]
        self.assertIn("/admin/leads/", location)
        self.assertIn("notice=nurture-created", location)
        lead_id = location.split("/admin/leads/")[1].split("?")[0]
        # 详情页渲染
        _s, _, page = form_request(self.server, "GET", f"/admin/leads/{lead_id}", cookie=self.sales_cookie)
        self.assertIn("待培育", page)
        self.assertIn("华东水务工程公司", page)
        self.assertIn("来源渠道：", page)
        self.assertIn("操作历史", page)
        self.assertIn("展会", self.db.conn.execute(
            "SELECT details FROM audit_log WHERE entity_id=? AND action='nurture_created'", (lead_id,)
        ).fetchone()["details"])
        # 列表页出现该线索
        _s, _, list_page = form_request(self.server, "GET", "/admin/sales", cookie=self.sales_cookie)
        self.assertIn(f"/admin/leads/{lead_id}", list_page)
        # 更新（正常 / 关闭缺原因 400 / 关闭带原因）
        status, headers, _ = form_request(
            self.server, "POST", f"/admin/leads/{lead_id}/update",
            {"stage": "technical_review", "next_follow_up": "2026-11-02", "note": "已回电"}, self.sales_cookie,
        )
        self.assertEqual(status, 303)
        self.assertIn("notice=updated", headers["Location"])
        lead = self.db.get_lead("demo-tenant", lead_id)
        self.assertEqual((lead["stage"], lead["next_follow_up"], lead["note"]), ("technical_review", "2026-11-02", "已回电"))
        status, _, _ = form_request(
            self.server, "POST", f"/admin/leads/{lead_id}/update", {"stage": "closed"}, self.sales_cookie
        )
        self.assertEqual(status, 400)
        status, _, page = form_request(
            self.server, "POST", f"/admin/leads/{lead_id}/update",
            {"stage": "closed", "close_reason": "客户预算冻结"}, self.sales_cookie,
        )
        self.assertEqual(status, 303)
        lead = self.db.get_lead("demo-tenant", lead_id)
        self.assertEqual(lead["stage"], "closed")
        self.assertTrue(lead["closed_at"])
        _s, _, page = form_request(self.server, "GET", f"/admin/leads/{lead_id}", cookie=self.sales_cookie)
        self.assertIn("客户预算冻结", page)
        # 重新激活
        form_request(self.server, "POST", f"/admin/leads/{lead_id}/update", {"stage": "new"}, self.sales_cookie)
        self.assertIsNone(self.db.get_lead("demo-tenant", lead_id)["closed_at"])
        # 跟进节点
        status, headers, _ = form_request(
            self.server, "POST", f"/admin/leads/{lead_id}/milestones/tech_confirmed", {}, self.sales_cookie
        )
        self.assertEqual(status, 303)
        self.assertIn("notice=milestone_on", headers["Location"])
        self.assertEqual(list(self.db.list_lead_milestones("demo-tenant", lead_id)), ["tech_confirmed"])
        form_request(self.server, "POST", f"/admin/leads/{lead_id}/milestones/tech_confirmed", {}, self.sales_cookie)
        self.assertEqual(list(self.db.list_lead_milestones("demo-tenant", lead_id)), [])

    def test_module_gate_owner_rules_and_assign(self):
        # 零权限员工不能登记/更新
        status, _, _ = form_request(
            self.server, "POST", "/admin/sales/nurture",
            {"contact_name": "零权限", "company": "测试", "contact_email": "n@x.example", "contact_phone": "", "source": "expo", "note": ""},
            self.nobody_cookie,
        )
        self.assertEqual(status, 403)
        status, _, _ = form_request(
            self.server, "POST", "/admin/leads/deadbeef/update", {"stage": "new"}, self.nobody_cookie
        )
        self.assertEqual(status, 403)
        # 员工（非管理员）不能分配
        lead_id = self.db.create_nurture_lead("demo-tenant", "n-sales@example.invalid", "赵工", "南方装备", "z@s.example", "", "visit", "", consent_confirmed=True)
        status, _, _ = form_request(
            self.server, "POST", f"/admin/leads/{lead_id}/assign", {"owner_email": "n-sales@example.invalid"}, self.sales_cookie
        )
        self.assertEqual(status, 403)
        # 管理员可分配给持 sales 模块的员工
        status, headers, _ = form_request(
            self.server, "POST", f"/admin/leads/{lead_id}/assign", {"owner_email": "n-sales@example.invalid"}, self.admin_cookie
        )
        self.assertEqual(status, 303)
        self.assertEqual(self.db.get_lead("demo-tenant", lead_id)["owner"], "n-sales@example.invalid")
        # 不能分配给无 sales 模块的员工
        status, _, _ = form_request(
            self.server, "POST", f"/admin/leads/{lead_id}/assign", {"owner_email": "n-nobody@example.invalid"}, self.admin_cookie
        )
        self.assertEqual(status, 400)

    def test_rfq_lead_detail_redirect_and_merged_exclusion(self):
        # 非 nurture 线索访问 /admin/leads/{id} → 跳回 RFQ 线索详情
        self.db.seed_demo_workspace("demo-tenant")
        rfq_lead = self.db.conn.execute("SELECT id, rfq_id FROM leads WHERE kind='rfq' LIMIT 1").fetchone()
        status, headers, _ = form_request(
            self.server, "GET", f"/admin/leads/{rfq_lead['id']}", cookie=self.admin_cookie
        )
        self.assertEqual(status, 303)
        self.assertIn(f"/admin/sales/{rfq_lead['rfq_id']}", headers["Location"])
        # 已合并的待培育线索不出现在列表
        lead_id = self.db.create_nurture_lead("demo-tenant", "n-sales@example.invalid", "合并测试", "测试公司", "m@t.example", "", "other", "", consent_confirmed=True)
        self.db.conn.execute("UPDATE leads SET merged_into='primary-x', merged_at='2026-10-05' WHERE id=?", (lead_id,))
        self.db.conn.commit()
        self.assertNotIn(lead_id, [row["id"] for row in self.db.list_nurture_leads("demo-tenant")])


class DuplicateMergeTests(unittest.TestCase):
    """重复线索提示与人工合并：只提示不自动合并；合并保留全部记录与历史。"""

    maxDiff = None

    @classmethod
    def setUpClass(cls):
        cls.temp_dir = tempfile.TemporaryDirectory()
        cls.server = make_server(Path(cls.temp_dir.name) / "app.db", port=0)
        cls.db = cls.server.RequestHandlerClass.db
        cls.db.seed_demo_users()
        cls.db.ensure_tenant("demo-tenant", "示例水泵企业（演示）")
        cls.db.seed_demo_workspace("demo-tenant")
        cls.sales_id = cls.db.create_user(
            "demo-tenant", "m-sales@example.invalid", "合并销售", "employee", "strong-pass", "demo-admin"
        )
        cls.db.set_user_modules("demo-tenant", cls.sales_id, ["sales"], "demo-admin@example.invalid")
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.admin_cookie = login(cls.server, "demo-admin@example.invalid", "demo-admin")
        cls.sales_cookie = login(cls.server, "m-sales@example.invalid", "strong-pass")

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.db.conn.close()
        cls.thread.join()
        cls.temp_dir.cleanup()

    def _rfq_lead(self):
        return self.db.conn.execute("SELECT * FROM leads WHERE kind='rfq' LIMIT 1").fetchone()

    def test_hints_detection_exclusions_and_render(self):
        from app import duplicate_leads_section

        rfq_lead = self._rfq_lead()
        rfq = self.db.conn.execute("SELECT * FROM rfqs WHERE id=?", (rfq_lead["rfq_id"],)).fetchone()
        # 同邮箱的待培育线索 → 双向提示
        nurture_id = self.db.create_nurture_lead(
            "demo-tenant", "m-sales@example.invalid", "疑似同一买家", "新公司", rfq["contact_email"], "", "expo", ""
        , consent_confirmed=True)
        hints = self.db.find_duplicate_lead_hints("demo-tenant", nurture_id)
        self.assertEqual([(h["kind"], h["matched_by"]) for h in hints], [("rfq", ["同邮箱"])])
        reverse = self.db.find_duplicate_lead_hints("demo-tenant", rfq_lead["id"])
        self.assertTrue(any(h["id"] == nurture_id and h["matched_by"] == ["同邮箱"] for h in reverse))
        # 无重合 → 无提示；自身不出现
        solo_id = self.db.create_nurture_lead("demo-tenant", "m-sales@example.invalid", "路人", "无关公司", "solo@x.example", "", "other", "", consent_confirmed=True)
        self.assertEqual(self.db.find_duplicate_lead_hints("demo-tenant", solo_id), [])
        self.assertTrue(all(h["id"] != nurture_id for h in self.db.find_duplicate_lead_hints("demo-tenant", nurture_id)))
        # 区块渲染：管理员有合并表单；员工没有
        section_admin = duplicate_leads_section(self.db, "demo-tenant", self.db.get_lead("demo-tenant", nurture_id), True)
        self.assertIn("确认合并", section_admin)
        self.assertIn("同邮箱", section_admin)
        section_sales = duplicate_leads_section(self.db, "demo-tenant", self.db.get_lead("demo-tenant", nurture_id), False)
        self.assertNotIn("确认合并", section_sales)

    def test_merge_flow_validation_and_audit(self):
        primary = self.db.create_nurture_lead("demo-tenant", "m-sales@example.invalid", "主线索", "甲公司", "p@x.example", "", "expo", "", consent_confirmed=True)
        secondary = self.db.create_nurture_lead("demo-tenant", "owner-2@example.invalid", "次级线索", "甲公司", "s@x.example", "", "expo", "", consent_confirmed=True)
        # 归属转移分支：主线索清空负责人 → 合并后继承次级的负责人
        self.db.conn.execute("UPDATE leads SET owner='' WHERE id=?", (primary,))
        self.db.conn.commit()
        self.db.merge_leads("demo-tenant", primary, secondary, "demo-admin@example.invalid")
        sec = self.db.get_lead("demo-tenant", secondary)
        self.assertEqual(sec["merged_into"], primary)
        self.assertTrue(sec["merged_at"])
        self.assertEqual(self.db.get_lead("demo-tenant", primary)["owner"], "owner-2@example.invalid")
        # 审计双向
        audits = self.db.conn.execute(
            "SELECT entity_id, action, details FROM audit_log WHERE action IN ('merged','merged_in') AND entity_id IN (?,?) ORDER BY id",
            (primary, secondary),
        ).fetchall()
        by_action = {row["action"]: row for row in audits}
        self.assertEqual(by_action["merged"]["entity_id"], secondary)
        self.assertIn(f"into={primary}", by_action["merged"]["details"])
        self.assertIn(f"from={secondary}", by_action["merged_in"]["details"])
        self.assertIn("owner_transferred=owner-2@example.invalid", by_action["merged_in"]["details"])
        # 列表剔除、关联清单、提示剔除
        self.assertNotIn(secondary, [row["id"] for row in self.db.list_nurture_leads("demo-tenant")])
        self.assertEqual([row["id"] for row in self.db.merged_related_leads("demo-tenant", primary)], [secondary])
        # 边界：重复合并 / 自合并 / 同询价 / 跨租户
        with self.assertRaises(ValueError):
            self.db.merge_leads("demo-tenant", primary, secondary, "demo-admin@example.invalid")
        third = self.db.create_nurture_lead("demo-tenant", "m-sales@example.invalid", "第三条", "乙公司", "t@x.example", "", "other", "", consent_confirmed=True)
        with self.assertRaises(ValueError):
            self.db.merge_leads("demo-tenant", third, third, "demo-admin@example.invalid")
        rfq_lead = self._rfq_lead()
        with self.assertRaises(ValueError):
            self.db.merge_leads("demo-tenant", rfq_lead["id"], rfq_lead["id"], "demo-admin@example.invalid")
        self.db.ensure_tenant("other-tenant", "另一家企业")
        with self.assertRaises(ValueError):
            self.db.merge_leads("other-tenant", primary, third, "setup")

    def test_http_merge_admin_only_and_page_rendering(self):
        rfq_lead = self._rfq_lead()
        rfq = self.db.conn.execute("SELECT * FROM rfqs WHERE id=?", (rfq_lead["rfq_id"],)).fetchone()
        nurture_id = self.db.create_nurture_lead(
            "demo-tenant", "m-sales@example.invalid", "网页合并演示", "演示公司", rfq["contact_email"], "", "visit", ""
        , consent_confirmed=True)
        self.db.assign_conversation_leads("demo-tenant", rfq["id"], "m-sales@example.invalid", "demo-admin@example.invalid")
        # 员工可查看已分配业务，仍不能合并
        status, _, _ = form_request(
            self.server, "POST", f"/admin/leads/{rfq_lead['id']}/merge", {"secondary_lead_id": nurture_id}, self.sales_cookie
        )
        self.assertEqual(status, 403)
        # 管理员合并（RFQ 线索为主 → 跳回销售详情并显示合并过程）
        status, headers, _ = form_request(
            self.server, "POST", f"/admin/leads/{rfq_lead['id']}/merge", {"secondary_lead_id": nurture_id}, self.admin_cookie
        )
        self.assertEqual(status, 303)
        self.assertIn(f"/admin/sales/{rfq_lead['rfq_id']}?notice=merged", headers["Location"])
        _s, _, page = form_request(self.server, "GET", f"/admin/sales/{rfq_lead['rfq_id']}?notice=merged", cookie=self.admin_cookie)
        self.assertIn("已合并该线索", page)
        self.assertIn("重复线索提示", page)
        self.assertIn("已并入本线索", page)
        self.assertIn(f"/admin/leads/{nurture_id}", page)
        # 次级线索详情：显示并入横幅并可跳转主线索
        _s, _, page = form_request(self.server, "GET", f"/admin/leads/{nurture_id}", cookie=self.admin_cookie)
        self.assertIn("该线索已并入", page)
        self.assertIn(f"/admin/sales/{rfq_lead['rfq_id']}", page)


class CategoryRfqFieldsTests(unittest.TestCase):
    """按品类询价字段：覆盖解析/校验、创建与修订执行、会话语境、产品页注入与设置页编辑。"""

    maxDiff = None

    @classmethod
    def setUpClass(cls):
        cls.temp_dir = tempfile.TemporaryDirectory()
        cls.server = make_server(Path(cls.temp_dir.name) / "app.db", port=0)
        cls.db = cls.server.RequestHandlerClass.db
        cls.db.seed_demo_users()
        cls.db.ensure_tenant("demo-tenant", "示例水泵企业（演示）")
        cls.db.register_buyer_site("demo-tenant", "demo-site-key", "http://127.0.0.1:8139")
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.admin_cookie = login(cls.server, "demo-admin@example.invalid", "demo-admin")

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.db.conn.close()
        cls.thread.join()
        cls.temp_dir.cleanup()

    def _make_product(self, model, category="", publish=True):
        pid = self.db.create_product("demo-tenant", model, f"{model} 泵", "测试", "demo-admin")
        sid = self.db.add_source(
            "demo-tenant", pid, f"{model}-a.txt", "TXT", "p1", "流量范围: 20-80 m³/h\n".encode(),
            "demo-admin", scope="exact_model",
        )
        self.db.approve_field("demo-tenant", pid, "flow_range")
        self.db.finalize_product_review("demo-tenant", pid)
        if category:
            self.db.set_category_candidate("demo-tenant", pid, category, "manual", "demo-admin")
            self.db.approve_category("demo-tenant", pid, "demo-admin")
        if publish:
            self.db.allow_public_source("demo-tenant", pid, sid, "demo-admin")
            self.db.publish_product("demo-tenant", pid)
        return pid

    def test_config_resolution_validation_and_clear(self):
        # 全局：目的地隐藏；品类「离心泵」覆盖：目的地必填、备注隐藏
        self.db.set_tenant_setting(
            "demo-tenant", SETTING_RFQ_FIELDS,
            {"email": "required", "phone": "required", "quantity": "required", "destination": "hidden", "notes": "optional"},
            "demo-admin",
        )
        self._make_product("CFG-1", category="离心泵")
        self.db.set_rfq_fields_for_category("demo-tenant", "离心泵", {"destination": "required", "notes": "hidden"}, "demo-admin")
        global_cfg = self.db.get_rfq_field_config("demo-tenant")
        self.assertEqual(global_cfg["destination"], "hidden")
        self.assertEqual(global_cfg["notes"], "optional")
        category_cfg = self.db.get_rfq_field_config("demo-tenant", "离心泵")
        self.assertEqual((category_cfg["destination"], category_cfg["notes"]), ("required", "hidden"))
        self.assertEqual(category_cfg["email"], "required")  # 未覆盖字段继承全局
        # 未核准品类：拒绝保存；读取时回退全局
        with self.assertRaises(ValueError):
            self.db.set_rfq_fields_for_category("demo-tenant", "未登记品类", {"notes": "hidden"}, "demo-admin")
        self.assertEqual(self.db.get_rfq_field_config("demo-tenant", "未登记品类")["destination"], "hidden")
        # 非法状态与双隐藏（含继承结果）
        with self.assertRaises(ValueError):
            self.db.set_rfq_fields_for_category("demo-tenant", "离心泵", {"notes": "maybe"}, "demo-admin")
        with self.assertRaises(ValueError):
            self.db.set_rfq_fields_for_category("demo-tenant", "离心泵", {"email": "hidden", "phone": "hidden"}, "demo-admin")
        # 清除覆盖 → 回到全局
        self.db.set_rfq_fields_for_category("demo-tenant", "离心泵", {}, "demo-admin")
        self.assertEqual(self.db.get_rfq_field_config("demo-tenant", "离心泵")["destination"], "hidden")

    def test_create_and_revise_enforcement_by_category(self):
        self.db.set_tenant_setting(
            "demo-tenant", SETTING_RFQ_FIELDS,
            {"email": "required", "phone": "required", "quantity": "required", "destination": "hidden", "notes": "optional"},
            "demo-admin",
        )
        pid = self._make_product("ENF-1", category="离心泵")
        pid2 = self._make_product("ENF-2", category="")
        self.db.set_rfq_fields_for_category("demo-tenant", "离心泵", {"destination": "required", "notes": "hidden"}, "demo-admin")
        conv = self.db.create_conversation("demo-tenant", "website_api", "en")
        contact = {"contact_name": "M", "company": "C", "contact_email": "m@x.example", "contact_phone": "+1", "quantity": "2", "region": "", "delivery": "8w"}
        # 品类产品缺目的地 → 拒（全局该字段为隐藏，说明按品类覆盖生效）
        with self.assertRaises(ValueError):
            self.db.create_rfq("demo-tenant", conv, "e1", contact, "", selected_product_id=pid)
        # 无品类产品同缺目的地 → 通过（继承全局隐藏）
        rfq_plain, created = self.db.create_rfq("demo-tenant", conv, "e2", contact, "", selected_product_id=pid2)
        self.assertTrue(created)
        # 品类产品带目的地 → 通过；备注（覆盖隐藏）不被收集
        rfq_cat, _ = self.db.create_rfq("demo-tenant", conv, "e3", dict(contact, region="Berlin"), "会被清空", selected_product_id=pid)
        row = self.db.get_rfq("demo-tenant", rfq_cat)
        self.assertEqual(row["destination"], "Berlin")
        self.assertEqual(row["notes"], "")
        # 修订：品类询价清空目的地 → 拒；非品类询价清空 → 通过（字段为隐藏态，不受必填约束）
        with self.assertRaises(ValueError):
            self.db.revise_rfq("demo-tenant", conv, rfq_cat, 1, {"destination": ""}, "r1")
        self.db.revise_rfq("demo-tenant", conv, rfq_plain, 1, {"destination": ""}, "r2")

    def test_conversation_hint_and_product_page_injection(self):
        self.db.set_tenant_setting(
            "demo-tenant", SETTING_RFQ_FIELDS,
            {"email": "required", "phone": "required", "quantity": "required", "destination": "hidden", "notes": "optional"},
            "demo-admin",
        )
        pid = self._make_product("HNT-1", category="离心泵")
        pid2 = self._make_product("HNT-2", category="")
        self.db.set_rfq_fields_for_category("demo-tenant", "离心泵", {"destination": "required"}, "demo-admin")
        # 会话推荐语境
        conv = self.db.create_conversation("demo-tenant", "website_api", "en")
        self.assertEqual(self.db.conversation_category_hint("demo-tenant", conv), "")
        revision = self.db.create_requirement_revision("demo-tenant", conv, {"flow": {"value": "30", "state": "confirmed"}}, status="confirmed")
        self.db.save_recommendation("demo-tenant", conv, revision, "candidate", [{"product_id": pid, "model": "HNT-1", "status": "candidate"}])
        self.assertEqual(self.db.conversation_category_hint("demo-tenant", conv), "离心泵")
        self.assertEqual(self.db.rfq_category_hint("demo-tenant", conv, None, "hnt-1"), "离心泵")
        self.assertEqual(self.db.rfq_category_hint("demo-tenant", "no-conv", "ghost", ""), "")
        # 产品页注入：品类页按覆盖（目的地必填）；无品类页按全局（隐藏）
        import json as _json
        import re as _re
        for model, slug_pid, expect in (("HNT-1", pid, "required"), ("HNT-2", pid2, "hidden")):
            slug = self.db.get_product("demo-tenant", slug_pid)["slug"]
            _s, _, page = form_request(self.server, "GET", f"/s/demo-site-key/products/{slug}")
            match = _re.search(r"window\.RFQ_FIELD_CONFIG=(\{.*?\});", page)
            self.assertIsNotNone(match, model)
            config = _json.loads(match.group(1))
            self.assertEqual(config["destination"], expect, model)

    def test_settings_category_editor_http(self):
        self._make_product("SET-1", category="螺杆泵")
        # 保存覆盖
        status, headers, _ = form_request(
            self.server, "POST", "/admin/settings/rfq-fields-category",
            {"category": "螺杆泵", "field_destination": "required", "field_notes": "hidden"}, self.admin_cookie,
        )
        self.assertEqual(status, 303)
        self.assertIn("notice=rfq-category", headers["Location"])
        config = self.db.get_rfq_field_config("demo-tenant", "螺杆泵")
        self.assertEqual((config["destination"], config["notes"]), ("required", "hidden"))
        _s, _, page = form_request(self.server, "GET", "/admin/settings", cookie=self.admin_cookie)
        self.assertIn("按品类询价字段", page)
        self.assertIn("螺杆泵", page)
        self.assertIn("目的地=必填", page)
        # 未核准品类 → 友好错误
        status, headers, _ = form_request(
            self.server, "POST", "/admin/settings/rfq-fields-category",
            {"category": "没这个品类", "field_destination": "required"}, self.admin_cookie,
        )
        self.assertEqual(status, 303)
        self.assertIn("notice=rfq_category_invalid", headers["Location"])
        # 清除覆盖
        status, headers, _ = form_request(
            self.server, "POST", "/admin/settings/rfq-fields-category",
            {"category": "螺杆泵", "clear": "1"}, self.admin_cookie,
        )
        self.assertEqual(status, 303)
        self.assertIn("notice=rfq-category", headers["Location"])
        # 清除后与该租户全局配置完全一致（回到继承）
        self.assertEqual(self.db.get_rfq_field_config("demo-tenant", "螺杆泵"), self.db.get_rfq_field_config("demo-tenant"))


class ContactConsentTests(unittest.TestCase):
    """联系同意与来源：RFQ 入口来源/时间落库并展示；待培育登记必须确认同意。"""

    maxDiff = None

    @classmethod
    def setUpClass(cls):
        cls.temp_dir = tempfile.TemporaryDirectory()
        cls.server = make_server(Path(cls.temp_dir.name) / "app.db", port=0)
        cls.db = cls.server.RequestHandlerClass.db
        cls.db.seed_demo_users()
        cls.db.ensure_tenant("demo-tenant", "示例水泵企业（演示）")
        cls.sales_id = cls.db.create_user(
            "demo-tenant", "c-sales@example.invalid", "同意销售", "employee", "strong-pass", "demo-admin"
        )
        cls.db.set_user_modules("demo-tenant", cls.sales_id, ["sales"], "demo-admin@example.invalid")
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.admin_cookie = login(cls.server, "demo-admin@example.invalid", "demo-admin")
        cls.sales_cookie = login(cls.server, "c-sales@example.invalid", "strong-pass")

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.db.conn.close()
        cls.thread.join()
        cls.temp_dir.cleanup()

    def test_rfq_consent_recorded_and_displayed(self):
        conv = self.db.create_conversation("demo-tenant", "website_api", "en")
        contact = {"contact_name": "M", "company": "C", "contact_email": "m@x.example", "contact_phone": "+1", "quantity": "2", "region": "Berlin", "delivery": "8w"}
        rfq_chat, _ = self.db.create_rfq("demo-tenant", conv, "cc1", contact, "", entry_surface="chat")
        rfq_pp, _ = self.db.create_rfq("demo-tenant", conv, "cc2", contact, "", entry_surface="product_page")
        row = self.db.get_rfq("demo-tenant", rfq_chat)
        self.assertEqual(row["contact_consent_source"], "chat")
        self.assertTrue(row["contact_consent_at"])
        self.assertEqual(self.db.get_rfq("demo-tenant", rfq_pp)["contact_consent_source"], "product_page")
        _s, _, page = form_request(self.server, "GET", f"/admin/sales/{rfq_chat}", cookie=self.admin_cookie)
        self.assertIn("联系同意：", page)
        self.assertIn("买方通过「官网聊天窗」提交询价", page)
        _s, _, page = form_request(self.server, "GET", f"/admin/sales/{rfq_pp}", cookie=self.admin_cookie)
        self.assertIn("买方通过「产品详情页」提交询价", page)

    def test_nurture_consent_required_http_and_display(self):
        base = {
            "contact_name": "同意测试", "company": "测试公司", "contact_email": "consent@x.example",
            "contact_phone": "", "source": "expo", "note": "",
        }
        # 未勾选同意 → 400 且不落任何数据
        status, _, body = form_request(self.server, "POST", "/admin/sales/nurture", base, self.sales_cookie)
        self.assertEqual(status, 400)
        self.assertIn("确认对方同意", body)
        self.assertEqual(
            self.db.conn.execute("SELECT COUNT(*) FROM leads WHERE contact_email='consent@x.example'").fetchone()[0], 0
        )
        # 勾选后登记成功，落 offline 来源与时间
        status, headers, _ = form_request(
            self.server, "POST", "/admin/sales/nurture", dict(base, consent_confirmed="1"), self.sales_cookie
        )
        self.assertEqual(status, 303)
        lead_id = headers["Location"].split("/admin/leads/")[1].split("?")[0]
        lead = self.db.get_lead("demo-tenant", lead_id)
        self.assertEqual(lead["consent_source"], "offline")
        self.assertTrue(lead["consent_at"])
        _s, _, page = form_request(self.server, "GET", f"/admin/leads/{lead_id}", cookie=self.sales_cookie)
        self.assertIn("联系同意：", page)
        self.assertIn("线下登记（销售已确认）", page)


if __name__ == "__main__":
    unittest.main()
