"""V1-02 产品资料增强（批次四 · 切片1）：源文件「禁止使用」级 —— 附件三级可见性。

锁定的契约（2026-10-05）：
- 可见性层级：internal（默认「仅内部」）/ restricted（「禁止使用」）；「可公开」＝归属已确认
  并通过公开下载许可（既有机制）。
- restricted：自动撤销公开下载许可；不可再开放公开下载；公开下载门（含旧版本快照）不再提供
  该文件；新回答不再引用其字段（转人工核实）；下次发布排除其字段与公开文件清单；
  恢复「仅内部」不自动恢复许可，重新公开须重新确认许可并发布新版本（可见性修订号推进）。
- 每次可见性变更审计（usage_changed；撤销许可时另记 public_permission_revoked_on_usage_change）。
"""
import http.client
import tempfile
import threading
import unittest
from pathlib import Path
from urllib.parse import urlencode

from app import (
    Database,
    RESTRICTED_SOURCE_ANSWER_REASON,
    answer_from_approved_material,
    make_server,
)


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


class SourceUsageTests(unittest.TestCase):
    maxDiff = None

    @classmethod
    def setUpClass(cls):
        cls.temp_dir = tempfile.TemporaryDirectory()
        cls.server = make_server(Path(cls.temp_dir.name) / "app.db", port=0)
        cls.db = cls.server.RequestHandlerClass.db
        cls.db.seed_demo_users()
        cls.db.ensure_tenant("demo-tenant", "示例水泵企业（演示）")
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

    def _make_product(self, model, published=False):
        """产品 + 两份来源（A=流量、B=扬程）+ 逐项核准 + 完成核对（可选发布）。

        发布前不开放任何公开许可；需要公开链路的用例自行先 allow + publish。
        """
        pid = self.db.create_product("demo-tenant", model, f"{model} 测试泵", "测试用途", "demo-admin")
        sid_a = self.db.add_source(
            "demo-tenant", pid, f"{model}-手册A.txt", "TXT", "第1页：参数表",
            "流量范围: 20-80 m³/h\n".encode(), "demo-admin", scope="exact_model",
        )
        sid_b = self.db.add_source(
            "demo-tenant", pid, f"{model}-手册B.txt", "TXT", "第1页：参数表",
            "扬程范围: 18-42 m\n".encode(), "demo-admin", scope="exact_model",
        )
        self.db.approve_field("demo-tenant", pid, "flow_range")
        self.db.approve_field("demo-tenant", pid, "head_range")
        self.db.finalize_product_review("demo-tenant", pid)
        if published:
            self.db.allow_public_source("demo-tenant", pid, sid_a, "demo-admin")
            self.db.allow_public_source("demo-tenant", pid, sid_b, "demo-admin")
            self.db.publish_product("demo-tenant", pid)
        return pid, sid_a, sid_b

    def _usage_post(self, pid, sid, usage):
        return form_request(
            self.server, "POST", f"/admin/products/{pid}/sources/{sid}/usage",
            {"usage": usage}, self.admin_cookie,
        )

    def test_restrict_revokes_permission_and_blocks_public_download(self):
        pid, sid_a, sid_b = self._make_product("SLICE-U1", published=True)
        slug = self.db.get_product("demo-tenant", pid)["slug"]
        self.assertIn(sid_a, self.db.public_downloadable_ids("demo-tenant", pid))
        # 公开下载路由：受限前 200
        status, _, _ = form_request(self.server, "GET", f"/products/{slug}/sources/{sid_a}")
        self.assertEqual(status, 200)
        # HTTP 标记禁止使用 → 303 + 专属 notice（原公开许可自动撤销）
        status, headers, _ = self._usage_post(pid, sid_a, "restricted")
        self.assertEqual(status, 303)
        self.assertIn("notice=usage-revoked", headers["Location"])
        servable = self.db.public_downloadable_ids("demo-tenant", pid)
        self.assertNotIn(sid_a, servable)
        self.assertIn(sid_b, servable)
        # 公开下载路由：受限后 404（旧快照也不再提供）
        status, _, _ = form_request(self.server, "GET", f"/products/{slug}/sources/{sid_a}")
        self.assertEqual(status, 404)
        # 幂等：重复标记同一层级 → usage-unchanged，不新增审计
        before_rows = self.db.conn.execute(
            "SELECT COUNT(*) AS n FROM audit_log WHERE entity_id=? AND action='usage_changed'", (sid_a,)
        ).fetchone()["n"]
        status, headers, _ = self._usage_post(pid, sid_a, "restricted")
        self.assertEqual(status, 303)
        self.assertIn("notice=usage-unchanged", headers["Location"])
        after_rows = self.db.conn.execute(
            "SELECT COUNT(*) AS n FROM audit_log WHERE entity_id=? AND action='usage_changed'", (sid_a,)
        ).fetchone()["n"]
        self.assertEqual(before_rows, after_rows)
        # 受限文件不能开放公开下载
        status, _, body = form_request(
            self.server, "POST", f"/admin/products/{pid}/sources/{sid_a}/allow-public", {}, self.admin_cookie
        )
        self.assertEqual(status, 400)
        self.assertIn("禁止使用", body)

    def test_detail_page_renders_usage_controls_and_reference_counts(self):
        pid, sid_a, _sid_b = self._make_product("SLICE-U2")
        status, _, page = form_request(self.server, "GET", f"/admin/products/{pid}", cookie=self.admin_cookie)
        self.assertEqual(status, 200)
        self.assertIn("仅内部", page)
        self.assertIn("标记禁止使用", page)
        self.assertIn("引用字段 1", page)
        self._usage_post(pid, sid_a, "restricted")
        _status, _, page = form_request(self.server, "GET", f"/admin/products/{pid}", cookie=self.admin_cookie)
        self.assertIn("禁止使用", page)
        self.assertIn("恢复可用", page)
        self.assertIn("该文件当前为 1 个字段的来源（其中已核准 1 个）", page)

    def test_answers_stop_citing_restricted_source_then_resume_after_restore(self):
        pid, sid_a, _sid_b = self._make_product("SLICE-U3")
        self.db.allow_public_source("demo-tenant", pid, sid_a, "demo-admin")
        self.db.publish_product("demo-tenant", pid)
        status, _answer, _pid, _vid, citations, reason = answer_from_approved_material(
            self.db, "demo-tenant", "流量范围是多少？", pid
        )
        self.assertEqual(status, "grounded")
        self.assertEqual(citations[0]["filename"], "SLICE-U3-手册A.txt")
        # 标记禁止使用 → 新回答不引用、转人工核实
        self.db.set_source_usage("demo-tenant", pid, sid_a, "restricted", "demo-admin")
        status, _answer, _pid, _vid, citations, reason = answer_from_approved_material(
            self.db, "demo-tenant", "流量范围是多少？", pid
        )
        self.assertEqual(status, "needs_human")
        self.assertEqual(citations, [])
        self.assertEqual(reason, RESTRICTED_SOURCE_ANSWER_REASON)
        # 恢复「仅内部」→ 回答恢复（对外引用不再受该标记限制）
        self.db.set_source_usage("demo-tenant", pid, sid_a, "internal", "demo-admin")
        status, _answer, _pid, _vid, citations, _reason = answer_from_approved_material(
            self.db, "demo-tenant", "流量范围是多少？", pid
        )
        self.assertEqual(status, "grounded")
        self.assertEqual(citations[0]["filename"], "SLICE-U3-手册A.txt")

    def test_new_publish_excludes_restricted_fields_and_files_old_snapshot_kept(self):
        pid, sid_a, sid_b = self._make_product("SLICE-U4", published=True)
        version_1 = self.db.get_published_version("demo-tenant", pid)
        self.assertEqual(version_1["version"], 1)
        # 标记 A 禁止使用 → 发布 v2：字段与公开文件均不含 A
        self.db.set_source_usage("demo-tenant", pid, sid_a, "restricted", "demo-admin")
        version_2 = self.db.publish_product("demo-tenant", pid)
        self.assertEqual(version_2, 2)
        import json as _json
        snapshot = _json.loads(self.db.get_published_version("demo-tenant", pid)["snapshot_json"])
        field_keys = {field["field_key"] for field in snapshot["fields"]}
        self.assertEqual(field_keys, {"head_range"})
        public_ids = {entry["id"] for entry in snapshot["public_sources"]}
        self.assertNotIn(sid_a, public_ids)
        self.assertIn(sid_b, public_ids)
        audit = self.db.conn.execute(
            "SELECT details FROM audit_log WHERE entity_id=? AND action='published' ORDER BY id DESC LIMIT 1",
            (pid,),
        ).fetchone()
        self.assertIn("usage_blocked=1", audit["details"])
        # 旧版本快照原样保留（旧 RFQ 仍引用当时版本）
        old = self.db.conn.execute(
            "SELECT snapshot_json FROM product_versions WHERE product_id=? AND version=1", (pid,)
        ).fetchone()
        old_keys = {field["field_key"] for field in _json.loads(old["snapshot_json"])["fields"]}
        self.assertIn("flow_range", old_keys)
        # 两份来源都被禁止 → 发布被拒（HTTP 400 页面）
        self.db.set_source_usage("demo-tenant", pid, sid_b, "restricted", "demo-admin")
        status, _, body = form_request(
            self.server, "POST", f"/admin/products/{pid}/publish", {}, self.admin_cookie
        )
        self.assertEqual(status, 400)
        self.assertIn("禁止使用", body)

    def test_usage_revision_and_audit_trail(self):
        pid, sid_a, _sid_b = self._make_product("SLICE-U5")
        row = self.db.conn.execute("SELECT scope_revision FROM source_documents WHERE id=?", (sid_a,)).fetchone()
        self.assertEqual(row["scope_revision"], 0)
        self.db.set_source_usage("demo-tenant", pid, sid_a, "restricted", "demo-admin")
        row = self.db.conn.execute("SELECT scope_revision, usage FROM source_documents WHERE id=?", (sid_a,)).fetchone()
        self.assertEqual((row["usage"], row["scope_revision"]), ("restricted", 1))
        self.db.set_source_usage("demo-tenant", pid, sid_a, "internal", "demo-admin")
        row = self.db.conn.execute("SELECT scope_revision FROM source_documents WHERE id=?", (sid_a,)).fetchone()
        self.assertEqual(row["scope_revision"], 2)
        rows = self.db.conn.execute(
            "SELECT details FROM audit_log WHERE entity_id=? AND action='usage_changed' ORDER BY id", (sid_a,)
        ).fetchall()
        self.assertEqual(len(rows), 2)
        self.assertIn("internal -> restricted (可见性修订 r1)", rows[0]["details"])
        self.assertIn("restricted -> internal (可见性修订 r2)", rows[1]["details"])
        # 公开许可被自动撤销时另记一条审计
        pid2, sid2, _b2 = self._make_product("SLICE-U5B")
        self.db.allow_public_source("demo-tenant", pid2, sid2, "demo-admin")
        result = self.db.set_source_usage("demo-tenant", pid2, sid2, "restricted", "demo-admin")
        self.assertTrue(result["public_permission_revoked"])
        revoked = self.db.conn.execute(
            "SELECT COUNT(*) AS n FROM audit_log WHERE entity_id=? AND action='public_permission_revoked_on_usage_change'",
            (sid2,),
        ).fetchone()["n"]
        self.assertEqual(revoked, 1)

    def test_validation_cross_tenant_and_defaults(self):
        pid, sid_a, _sid_b = self._make_product("SLICE-U6")
        # 默认层级
        row = self.db.conn.execute("SELECT usage FROM source_documents WHERE id=?", (sid_a,)).fetchone()
        self.assertEqual(row["usage"], "internal")
        # 非法层级
        with self.assertRaises(ValueError):
            self.db.set_source_usage("demo-tenant", pid, sid_a, "public", "demo-admin")
        # 不存在的来源 / 跨租户
        with self.assertRaises(ValueError):
            self.db.set_source_usage("demo-tenant", pid, "deadbeefdeadbeef", "restricted", "demo-admin")
        self.db.ensure_tenant("other-tenant", "另一家企业")
        with self.assertRaises(ValueError):
            self.db.set_source_usage("other-tenant", pid, sid_a, "restricted", "setup")


class CategoryApprovalTests(unittest.TestCase):
    """品类候选与核准：候选 ≠ 已核准；授权成员核准生效；未核准不进入对外内容与派生行为。"""

    maxDiff = None

    @classmethod
    def setUpClass(cls):
        cls.temp_dir = tempfile.TemporaryDirectory()
        cls.server = make_server(Path(cls.temp_dir.name) / "app.db", port=0)
        cls.db = cls.server.RequestHandlerClass.db
        cls.db.seed_demo_users()
        cls.db.ensure_tenant("demo-tenant", "示例水泵企业（演示）")
        # 授权成员（持 products 模块）与零权限员工
        cls.member_id = cls.db.create_user(
            "demo-tenant", "cat-member@example.invalid", "产品同事", "employee", "strong-pass", "demo-admin"
        )
        cls.db.set_user_modules("demo-tenant", cls.member_id, ["products"], "demo-admin@example.invalid")
        cls.nobody_id = cls.db.create_user(
            "demo-tenant", "cat-nobody@example.invalid", "无权限同事", "employee", "strong-pass", "demo-admin"
        )
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.admin_cookie = login(cls.server, "demo-admin@example.invalid", "demo-admin")
        cls.member_cookie = login(cls.server, "cat-member@example.invalid", "strong-pass")
        cls.nobody_cookie = login(cls.server, "cat-nobody@example.invalid", "strong-pass")

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.db.conn.close()
        cls.thread.join()
        cls.temp_dir.cleanup()

    def _make_product(self, model):
        pid = self.db.create_product("demo-tenant", model, f"{model} 测试泵", "测试用途", "demo-admin")
        self.db.add_source(
            "demo-tenant", pid, f"{model}-手册.txt", "TXT", "第1页：参数表",
            "流量范围: 20-80 m³/h\n".encode(), "demo-admin", scope="exact_model",
        )
        self.db.approve_field("demo-tenant", pid, "flow_range")
        self.db.finalize_product_review("demo-tenant", pid)
        return pid

    def test_candidate_approve_replace_flow_and_audit(self):
        pid = self._make_product("SLICE-C1")
        self.assertEqual(self.db.get_approved_category("demo-tenant", pid), "")
        # 候选不生效
        self.db.set_category_candidate("demo-tenant", pid, " 离心泵 ", "doc", "demo-admin")
        self.assertEqual(self.db.get_approved_category("demo-tenant", pid), "")
        row = self.db.conn.execute("SELECT category_candidate, category_candidate_source FROM products WHERE id=?", (pid,)).fetchone()
        self.assertEqual((row["category_candidate"], row["category_candidate_source"]), ("离心泵", "doc"))
        # 核准生效（授权成员）
        self.assertEqual(self.db.approve_category("demo-tenant", pid, "cat-member@example.invalid"), "离心泵")
        row = self.db.conn.execute("SELECT * FROM products WHERE id=?", (pid,)).fetchone()
        self.assertEqual(row["category"], "离心泵")
        self.assertEqual(row["category_approved_by"], "cat-member@example.invalid")
        self.assertTrue(row["category_approved_at"])
        self.assertEqual(row["category_candidate"], "")
        self.assertIsNone(row["category_candidate_at"])
        # 替换流程：新候选 → 再核准（审计 before→after）
        self.db.set_category_candidate("demo-tenant", pid, "混流泵", "manual", "demo-admin")
        self.assertEqual(self.db.approve_category("demo-tenant", pid, "demo-admin"), "混流泵")
        audits = self.db.conn.execute(
            "SELECT details FROM audit_log WHERE entity_id=? AND action='category_approved' ORDER BY id", (pid,)
        ).fetchall()
        self.assertEqual(len(audits), 2)
        self.assertIn("（未设置） -> 离心泵", audits[0]["details"])
        self.assertIn("离心泵 -> 混流泵", audits[1]["details"])
        # 无候选时核准被拒
        with self.assertRaises(ValueError):
            self.db.approve_category("demo-tenant", pid, "demo-admin")

    def test_candidate_never_leaks_to_snapshot_or_gate(self):
        pid = self._make_product("SLICE-C2")
        # 快照：无已核准品类 → 空串；仅候选 → 仍空串；核准后发布 → 生效
        self.db.publish_product("demo-tenant", pid)
        import json as _json
        snap = _json.loads(self.db.get_published_version("demo-tenant", pid)["snapshot_json"])
        self.assertEqual(snap["product"]["category"], "")
        self.db.set_category_candidate("demo-tenant", pid, "离心泵", "model", "demo-admin")
        self.db.publish_product("demo-tenant", pid)
        snap = _json.loads(self.db.get_published_version("demo-tenant", pid)["snapshot_json"])
        self.assertEqual(snap["product"]["category"], "")
        self.assertEqual(self.db.get_approved_category("demo-tenant", pid), "")
        self.db.approve_category("demo-tenant", pid, "demo-admin")
        self.db.publish_product("demo-tenant", pid)
        snap = _json.loads(self.db.get_published_version("demo-tenant", pid)["snapshot_json"])
        self.assertEqual(snap["product"]["category"], "离心泵")

    def test_validation_cross_tenant_and_clear(self):
        pid = self._make_product("SLICE-C3")
        with self.assertRaises(ValueError):
            self.db.set_category_candidate("demo-tenant", pid, "   ", "manual", "demo-admin")
        with self.assertRaises(ValueError):
            self.db.set_category_candidate("demo-tenant", pid, "泵" * 41, "manual", "demo-admin")
        with self.assertRaises(ValueError):
            self.db.set_category_candidate("demo-tenant", pid, "离心泵", "guess", "demo-admin")
        # 清除候选
        self.db.set_category_candidate("demo-tenant", pid, "离心泵", "manual", "demo-admin")
        self.db.clear_category_candidate("demo-tenant", pid, "demo-admin")
        row = self.db.conn.execute("SELECT category_candidate FROM products WHERE id=?", (pid,)).fetchone()
        self.assertEqual(row["category_candidate"], "")
        with self.assertRaises(ValueError):
            self.db.clear_category_candidate("demo-tenant", pid, "demo-admin")
        # 跨租户：视为不存在
        self.db.ensure_tenant("other-tenant", "另一家企业")
        with self.assertRaises(ValueError):
            self.db.set_category_candidate("other-tenant", pid, "离心泵", "manual", "setup")
        with self.assertRaises(ValueError):
            self.db.approve_category("other-tenant", pid, "setup")

    def test_http_flow_page_rendering_and_module_gate(self):
        pid = self._make_product("SLICE-C4")
        # 零权限员工 → 403（含所属模块提示）
        status, _, _ = form_request(
            self.server, "POST", f"/admin/products/{pid}/category/candidate",
            {"category": "离心泵", "source": "manual"}, self.nobody_cookie,
        )
        self.assertEqual(status, 403)
        # 授权成员登记候选 → 303 + notice；页面渲染候选与核准入口
        status, headers, _ = form_request(
            self.server, "POST", f"/admin/products/{pid}/category/candidate",
            {"category": "离心泵", "source": "manual"}, self.member_cookie,
        )
        self.assertEqual(status, 303)
        self.assertIn("notice=category-candidate", headers["Location"])
        _s, _, page = form_request(self.server, "GET", f"/admin/products/{pid}", cookie=self.member_cookie)
        self.assertIn("品类（候选与核准）", page)
        self.assertIn("当前候选：<b>离心泵</b>", page)
        self.assertIn("核准为正式品类", page)
        # 授权成员核准 → 303 + notice；页面显示已核准（操作者+时间）
        status, headers, _ = form_request(
            self.server, "POST", f"/admin/products/{pid}/category/approve", {}, self.member_cookie
        )
        self.assertEqual(status, 303)
        self.assertIn("notice=category-approve", headers["Location"])
        _s, _, page = form_request(self.server, "GET", f"/admin/products/{pid}", cookie=self.admin_cookie)
        self.assertIn("已核准：离心泵", page)
        self.assertIn("cat-member@example.invalid", page)
        # 无候选核准 → 400
        status, _, _ = form_request(
            self.server, "POST", f"/admin/products/{pid}/category/approve", {}, self.admin_cookie
        )
        self.assertEqual(status, 400)


if __name__ == "__main__":
    unittest.main()
