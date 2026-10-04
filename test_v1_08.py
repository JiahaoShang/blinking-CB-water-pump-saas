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
            "source": "expo", "note": "展会接触，Q4 招标",
        }
        fields.update(overrides)
        return form_request(self.server, "POST", "/admin/sales/nurture", fields, self.sales_cookie)

    def test_create_nurture_validation_and_audit(self):
        # 校验：来源无效 / 无联系方式 / 无称呼公司
        with self.assertRaises(ValueError):
            self.db.create_nurture_lead("demo-tenant", "x", "王工", "公司", "a@b.c", "", "bad-source", "")
        with self.assertRaises(ValueError):
            self.db.create_nurture_lead("demo-tenant", "x", "王工", "公司", "", "", "expo", "")
        with self.assertRaises(ValueError):
            self.db.create_nurture_lead("demo-tenant", "x", "", "", "a@b.c", "", "expo", "")
        lead_id = self.db.create_nurture_lead(
            "demo-tenant", "n-sales@example.invalid", "李工", "北方泵业", "", "+86 138 0000 0000", "phone", "电话询价"
        )
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
        lead_id = self.db.create_nurture_lead("demo-tenant", "n-sales@example.invalid", "赵工", "南方装备", "z@s.example", "", "visit", "")
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
        lead_id = self.db.create_nurture_lead("demo-tenant", "n-sales@example.invalid", "合并测试", "测试公司", "m@t.example", "", "other", "")
        self.db.conn.execute("UPDATE leads SET merged_into='primary-x', merged_at='2026-10-05' WHERE id=?", (lead_id,))
        self.db.conn.commit()
        self.assertNotIn(lead_id, [row["id"] for row in self.db.list_nurture_leads("demo-tenant")])


if __name__ == "__main__":
    unittest.main()
