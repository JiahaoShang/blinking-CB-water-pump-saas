"""企业知识第二批 · B2-B08 精确来源所有权（判别性回归）。

对应独立复现 `/tmp/b2b-phase2-old-batch-review.py` 与用户 2026-10-12 的修复要求：
v1 原件 220V 发布 → 替换原件 240V 发布 v2 → 采用模板产生 v3 草稿后，从历史批次发布
不得把 v3 正文改回 220V、也不得发布；历史来源不得刷新 / 编辑 / 核对 / 补人工正文；
拒绝必须发生在任何写入之前（当前草稿、当前线上版本、批次与历史版本保持不变，队列行为不变）；
当前来源（替换批次）的编辑、核对与发布保持可用；显式历史恢复（恢复为新草稿）保持可用；
字段页 / 资料页给出当前草稿的真实发布位置（不把用户带向被替换的历史批次）；
旧产品/旧资料 ID、原件与租户/权限检查保持。

判别力：本文件在修复前代码上会失败（旧行为：旧批次发布会把 v3 正文改写为 220V 并发布 v3；
旧来源编辑/核对/补人工正文会被接受并改动当前草稿）。
只用临时数据库与测试私有上传目录；不触碰 data/ 下真实演示数据与任何密钥；不接真实模型。
"""
import http.client
import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit

from app import Database, make_server

TENANT = "demo-tenant"
OTHER = "other-tenant"
BOUNDARY = "----kb2ownershipboundary"


def request(server, method, path, *, fields=None, files=None, cookie=None):
    conn = http.client.HTTPConnection("127.0.0.1", server.server_port)
    headers = {}
    body = None
    if files is not None:
        parts = []
        for name, value in (fields or {}).items():
            parts.append(
                f"--{BOUNDARY}\r\nContent-Disposition: form-data; name=\"{name}\"\r\n\r\n{value}\r\n".encode("utf-8"))
        for name, filename, content in files:
            parts.append(
                (f"--{BOUNDARY}\r\nContent-Disposition: form-data; name=\"{name}\"; filename=\"{filename}\"\r\n"
                 "Content-Type: application/octet-stream\r\n\r\n").encode("utf-8"))
            parts.append(content)
            parts.append(b"\r\n")
        parts.append(f"--{BOUNDARY}--\r\n".encode("utf-8"))
        body = b"".join(parts)
        headers["Content-Type"] = "multipart/form-data; boundary=" + BOUNDARY
    elif fields is not None:
        body = urlencode(fields).encode("utf-8")
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    if cookie:
        headers["Cookie"] = cookie
    conn.request(method, path, body=body, headers=headers)
    response = conn.getresponse()
    raw = response.read()
    status = response.status
    headers_out = dict(response.getheaders())
    conn.close()
    return status, headers_out, raw


def login(server, email, password):
    status, headers, _ = request(server, "POST", "/login", fields={"email": email, "password": password})
    if status not in (302, 303) or "Set-Cookie" not in headers:
        raise AssertionError(f"登录失败: {email} -> {status}")
    return headers["Set-Cookie"].split(";", 1)[0]


class OwnershipBase(unittest.TestCase):
    maxDiff = None

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.upload_dir = Path(self.tmp.name) / "uploads"
        os.environ["B2B_KB_STORAGE_ROOT"] = str(self.upload_dir)
        self.db = Database(Path(self.tmp.name) / "kb2-ownership.db")
        self.db.knowledge_storage_root = self.upload_dir
        self.db.seed_demo_users()
        self.admin = self.db.authenticate_user("demo-admin@example.invalid", "demo-admin")
        self.member = self.db.authenticate_user("demo-sales@example.invalid", "demo-sales")  # 无 products 权限
        self.db.ensure_tenant(OTHER, "其他企业（测试）")
        self.other_admin = self.db.create_user(
            OTHER, "other-admin@example.invalid", "对方管理员", "admin", "other-admin", "test-setup")

    def tearDown(self):
        self.db.conn.close()
        os.environ.pop("B2B_KB_STORAGE_ROOT", None)
        self.tmp.cleanup()

    # ── 夹具 ──────────────────────────────────────────────────────────────

    def ingest(self, filename, text):
        batch_id = self.db.kb2_create_batch(
            TENANT, self.admin["id"], [{"kind": "file", "filename": filename, "data": text.encode()}])
        return self.db.kb2_get_batch(TENANT, batch_id)["sources"][0]

    def batch_of(self, source_id):
        return self.db.conn.execute(
            "SELECT batch_id FROM kb2_sources WHERE id=?", (source_id,)).fetchone()[0]

    def check_all(self, source_id):
        for row in self.db.conn.execute(
                "SELECT id FROM kb2_paragraphs WHERE tenant_id=? AND source_id=?",
                (TENANT, source_id)).fetchall():
            self.db.kb2_update_paragraph(TENANT, self.admin["id"], row["id"], checked=True)

    def publish(self, batch_id):
        result = self.db.kb2_publish_batch(TENANT, self.admin["id"], batch_id)
        self.assertFalse(result["failed"], result["failed"])
        return result

    def paragraphs_of(self, source_id):
        return self.db.conn.execute(
            "SELECT * FROM kb2_paragraphs WHERE tenant_id=? AND source_id=? ORDER BY ordinal",
            (TENANT, source_id)).fetchall()

    def make_replaced_item(self):
        """复现 B2-B08 场景：v1(220V) 发布 → 替换(240V) 发布 v2 → 采用模板得到 v3 草稿。"""
        first = self.ingest("old.txt", "Power supply 220V")
        batch1 = self.batch_of(first["id"])
        self.check_all(first["id"])
        self.publish(batch1)
        item_id = self.db.kb2_get_batch(TENANT, batch1)["sources"][0]["item_id"]
        replacement = self.db.kb2_replace_file(
            TENANT, self.admin["id"], item_id, "new.txt", b"Power supply 240V")
        self.check_all(replacement["source_id"])
        self.publish(replacement["batch_id"])
        template_version = self.db.kb2_save_template(
            TENANT, self.admin["id"], fields=[{"key": "note", "label": "Note", "type": "text"}], name="QA")
        self.db.kb2_adopt_template(TENANT, self.admin["id"], item_id, template_version, confirmed=True)
        return {
            "item_id": item_id, "batch1": batch1, "old_source": first["id"],
            "new_source": replacement["source_id"], "batch2": replacement["batch_id"],
        }

    def draft_body(self, item_id):
        draft = self.db.get_knowledge_draft_version(TENANT, item_id)
        return json.loads(draft["content_json"])["body"] if draft else None

    def published_snapshot(self, item_id):
        version = self.db.get_published_knowledge_version(TENANT, item_id)
        return None if version is None else (version["id"], version["version"], version["content_json"])

    def history_snapshot(self, item_id):
        return [(row["id"], row["version"], row["status"], row["content_json"])
                for row in self.db.list_knowledge_versions(TENANT, item_id)]


class OldBatchOwnershipTests(OwnershipBase):
    """历史来源/批次不得改写或发布属于更新原件的当前草稿；拒绝保持一切不变。"""

    def test_old_batch_publish_rejected_and_everything_preserved(self):
        scenario = self.make_replaced_item()
        item_id = scenario["item_id"]
        draft_before = self.db.get_knowledge_draft_version(TENANT, item_id)
        self.assertIn("240V", json.loads(draft_before["content_json"])["body"])
        published_before = self.published_snapshot(item_id)
        history_before = self.history_snapshot(item_id)

        outcome = self.db.kb2_publish_batch(TENANT, self.admin["id"], scenario["batch1"])

        self.assertEqual(outcome["published"], [])
        self.assertEqual(outcome["archived"], [])
        self.assertEqual(len(outcome["failed"]), 1, outcome)
        self.assertIn("历史原件", outcome["failed"][0]["error"])
        # 当前草稿不变（正文仍是 240V，不是旧批次重写回的 220V）。
        draft_after = self.db.get_knowledge_draft_version(TENANT, item_id)
        self.assertEqual(draft_after["id"], draft_before["id"])
        self.assertEqual(draft_after["version"], draft_before["version"])
        self.assertEqual(draft_after["content_json"], draft_before["content_json"])
        # 当前线上版本与全部历史版本不变。
        self.assertEqual(self.published_snapshot(item_id), published_before)
        self.assertEqual(self.history_snapshot(item_id), history_before)
        superseded = {row["version"]: row for row in self.db.list_knowledge_versions(TENANT, item_id)}
        self.assertEqual(json.loads(superseded[1]["content_json"])["body"], "Power supply 220V")

    def test_old_source_edit_check_and_exclude_rejected_without_writes(self):
        scenario = self.make_replaced_item()
        item_id = scenario["item_id"]
        paragraph = self.paragraphs_of(scenario["old_source"])[0]
        draft_before = self.draft_body(item_id)
        with self.assertRaisesRegex(ValueError, "历史原件"):
            self.db.kb2_update_paragraph(TENANT, self.admin["id"], paragraph["id"], text="Power supply 110V")
        with self.assertRaisesRegex(ValueError, "历史原件"):
            self.db.kb2_update_paragraph(TENANT, self.admin["id"], paragraph["id"], checked=False)
        with self.assertRaisesRegex(ValueError, "历史原件"):
            self.db.kb2_update_paragraph(TENANT, self.admin["id"], paragraph["id"], excluded=True)
        fresh = self.db.conn.execute(
            "SELECT * FROM kb2_paragraphs WHERE id=?", (paragraph["id"],)).fetchone()
        self.assertEqual(fresh["text"], paragraph["text"])
        self.assertEqual(fresh["checked"], paragraph["checked"])
        self.assertEqual(fresh["excluded"], paragraph["excluded"])
        self.assertEqual(fresh["updated_at"], paragraph["updated_at"])
        self.assertEqual(self.draft_body(item_id), draft_before)

    def test_old_source_manual_paragraph_and_retry_rejected(self):
        scenario = self.make_replaced_item()
        item_id = scenario["item_id"]
        count_before = len(self.paragraphs_of(scenario["old_source"]))
        with self.assertRaisesRegex(ValueError, "历史原件"):
            self.db.kb2_add_manual_paragraph(TENANT, self.admin["id"], scenario["old_source"], "补一段人工正文")
        self.assertEqual(len(self.paragraphs_of(scenario["old_source"])), count_before)
        self.assertIn("240V", self.draft_body(item_id))
        with self.assertRaisesRegex(ValueError, "历史原件"):
            self.db.kb2_retry_source(TENANT, self.admin["id"], scenario["old_source"])
        # 来源状态未被改动（仍是已发布）。
        source = self.db._kb2_source_row(TENANT, scenario["old_source"])
        self.assertEqual(source["status"], "published")

    def test_worker_scan_does_not_touch_current_draft(self):
        scenario = self.make_replaced_item()
        item_id = scenario["item_id"]
        draft_before = self.draft_body(item_id)
        summary = self.db.kb2_process_pending_sources(TENANT)
        self.assertEqual(summary.get("errored"), 0, summary)
        self.assertEqual(self.draft_body(item_id), draft_before)

    def test_cross_tenant_checks_hold(self):
        scenario = self.make_replaced_item()
        with self.assertRaises((ValueError, PermissionError)):
            self.db.kb2_publish_batch(OTHER, self.other_admin, scenario["batch1"])
        with self.assertRaises((ValueError, PermissionError)):
            self.db.kb2_update_paragraph(
                OTHER, self.other_admin, self.paragraphs_of(scenario["old_source"])[0]["id"], checked=True)
        self.assertEqual(self.db.kb2_item_publish_context(OTHER, scenario["item_id"]), None)


class CurrentSourceUsabilityTests(OwnershipBase):
    """当前合法来源保持可用；显式历史恢复产生的新草稿保持可用。"""

    def test_current_source_check_edit_and_batch_publication_work(self):
        scenario = self.make_replaced_item()
        item_id = scenario["item_id"]
        paragraph = self.paragraphs_of(scenario["new_source"])[0]
        # 当前来源：编辑 → 自动撤销核对；显式再核对后发布。
        self.db.kb2_update_paragraph(TENANT, self.admin["id"], paragraph["id"],
                                     text="Power supply 240V (rev A)")
        self.assertIn("240V (rev A)", self.draft_body(item_id))
        refreshed = self.db.conn.execute(
            "SELECT * FROM kb2_paragraphs WHERE id=?", (paragraph["id"],)).fetchone()
        self.assertFalse(refreshed["checked"])
        self.db.kb2_update_paragraph(TENANT, self.admin["id"], paragraph["id"], checked=True)
        result = self.publish(scenario["batch2"])
        self.assertEqual(len(result["published"]), 1, result)
        published = self.db.get_published_knowledge_version(TENANT, item_id)
        self.assertEqual(published["version"], 3)
        self.assertIn("Power supply 240V (rev A)", json.loads(published["content_json"])["body"])
        # 发布后再发旧批次：已发布且无草稿 → 幂等跳过，不动任何内容。
        again = self.db.kb2_publish_batch(TENANT, self.admin["id"], scenario["batch1"])
        self.assertEqual(again["published"], [])
        self.assertEqual(len(again["skipped"]), 1, again)
        self.assertIn("Power supply 240V (rev A)",
                      json.loads(self.db.get_published_knowledge_version(TENANT, item_id)["content_json"])["body"])

    def test_explicit_history_restore_remains_usable(self):
        scenario = self.make_replaced_item()
        item_id = scenario["item_id"]
        # 有未发布草稿时恢复被明确拒绝（先发布或继续现有草稿）。
        with self.assertRaisesRegex(ValueError, "已有未发布草稿"):
            self.db.restore_knowledge_version(TENANT, item_id, 1, self.admin["id"])
        # 发布当前草稿后，显式从版本记录恢复 v1 为新草稿。
        self.publish(scenario["batch2"])
        self.db.restore_knowledge_version(TENANT, item_id, 1, self.admin["id"])
        draft = self.db.get_knowledge_draft_version(TENANT, item_id)
        self.assertEqual(draft["restored_from_version"], 1)
        self.assertIn("Power supply 220V", json.loads(draft["content_json"])["body"])
        # 恢复后旧原件的编辑恢复可用（草稿归属回到该原件），发布走同一批次路径。
        paragraph = self.paragraphs_of(scenario["old_source"])[0]
        self.db.kb2_update_paragraph(TENANT, self.admin["id"], paragraph["id"],
                                     text="Power supply 220V (restored)")
        self.db.kb2_update_paragraph(TENANT, self.admin["id"], paragraph["id"], checked=True)
        result = self.publish(scenario["batch1"])
        self.assertEqual(len(result["published"]), 1, result)
        published = self.db.get_published_knowledge_version(TENANT, item_id)
        self.assertEqual(published["version"], 4)
        self.assertIn("Power supply 220V (restored)", json.loads(published["content_json"])["body"])

    def test_publish_context_points_to_current_original(self):
        scenario = self.make_replaced_item()
        context = self.db.kb2_item_publish_context(TENANT, scenario["item_id"])
        self.assertEqual(context["source_id"], scenario["new_source"])
        self.assertEqual(context["batch_id"], scenario["batch2"])
        self.assertTrue(context["draft"])


class SourceOwnershipHttpTests(unittest.TestCase):
    """UI/HTTP：字段页与资料页给出当前草稿的真实发布位置；过期批次有明确门槛提示。"""

    maxDiff = None

    @classmethod
    def setUpClass(cls):
        cls.temp_dir = tempfile.TemporaryDirectory()
        cls.upload_dir = Path(cls.temp_dir.name) / "uploads"
        os.environ["B2B_KB_STORAGE_ROOT"] = str(cls.upload_dir)
        cls.db_path = Path(cls.temp_dir.name) / "kb2-ownership-http.db"
        cls.server = make_server(cls.db_path, port=0)
        cls.db = cls.server.RequestHandlerClass.db
        cls.db.knowledge_storage_root = cls.upload_dir
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.db.seed_demo_users()
        cls.admin = cls.db.authenticate_user("demo-admin@example.invalid", "demo-admin")
        cls.admin_cookie = login(cls.server, "demo-admin@example.invalid", "demo-admin")
        cls.member_cookie = login(cls.server, "demo-sales@example.invalid", "demo-sales")
        cls.scenario = cls._build_scenario()

    @classmethod
    def tearDownClass(cls):
        cls.server.server_close()
        cls.db.conn.close()
        os.environ.pop("B2B_KB_STORAGE_ROOT", None)
        cls.temp_dir.cleanup()

    # ── 帮助 ──────────────────────────────────────────────────────────────

    @classmethod
    def _batch_of(cls, source_id):
        return cls.db.conn.execute(
            "SELECT batch_id FROM kb2_sources WHERE id=?", (source_id,)).fetchone()[0]

    @classmethod
    def _check(cls, batch_id, source_id):
        fields = {"source_id": source_id}
        source = next(row for row in cls.db.kb2_get_batch(TENANT, batch_id)["sources"] if row["id"] == source_id)
        for paragraph in source["paragraphs"]:
            fields[f"p_{paragraph['id']}_text"] = paragraph["text"]
            fields[f"p_{paragraph['id']}_checked"] = "1"
        status, _, _ = request(cls.server, "POST", f"/admin/knowledge/batch/{batch_id}/paragraphs",
                               fields=fields, cookie=cls.admin_cookie)
        assert status in (302, 303), status

    @classmethod
    def _publish(cls, batch_id):
        status, headers, _ = request(
            cls.server, "POST", f"/admin/knowledge/batch/{batch_id}/settings",
            fields={"usage": "internal", "action": "publish", "confirm_ai": "0"}, cookie=cls.admin_cookie)
        assert status in (302, 303), status
        return headers["Location"]

    @classmethod
    def _build_scenario(cls):
        status, headers, _ = request(
            cls.server, "POST", "/admin/knowledge/add",
            files=[("source_file", "old.txt", b"Power supply 220V")], cookie=cls.admin_cookie)
        assert status in (302, 303), status
        batch1 = urlsplit(headers["Location"]).path.rsplit("/", 1)[1]
        cls.db.kb2_process_pending_sources(TENANT)
        old_source = cls.db.kb2_get_batch(TENANT, batch1)["sources"][0]
        cls._check(batch1, old_source["id"])
        cls._publish(batch1)
        item_id = cls.db.kb2_get_batch(TENANT, batch1)["sources"][0]["item_id"]
        status, headers, _ = request(
            cls.server, "POST", f"/admin/knowledge/item/{item_id}/replace-file",
            files=[("replace_file", "new.txt", b"Power supply 240V")], cookie=cls.admin_cookie)
        assert status in (302, 303), status
        batch2 = urlsplit(headers["Location"]).path.split("/batch/")[1].split("?")[0]
        cls.db.kb2_process_pending_sources(TENANT)
        new_source = cls.db.kb2_get_batch(TENANT, batch2)["sources"][0]
        cls._check(batch2, new_source["id"])
        cls._publish(batch2)
        template_version = cls.db.kb2_save_template(
            TENANT, cls.admin["id"], fields=[{"key": "note", "label": "Note", "type": "text"}], name="QA-HTTP")
        status, headers, _ = request(
            cls.server, "POST", f"/admin/knowledge/fields/{item_id}/adopt",
            fields={"template_version_id": template_version, "stage": "confirm"}, cookie=cls.admin_cookie)
        assert status in (302, 303), status
        return {"item_id": item_id, "batch1": batch1, "old_source": old_source["id"],
                "new_source": new_source["id"], "batch2": batch2}

    def get(self, path, *, cookie=None):
        status, _, raw = request(self.server, "GET", path, cookie=cookie or self.admin_cookie)
        return status, raw.decode("utf-8", errors="replace")

    # ── 页面可达性 ────────────────────────────────────────────────────────

    def test_fields_page_links_current_batch_publication_and_template_management(self):
        status, page = self.get(f"/admin/knowledge/fields/{self.scenario['item_id']}")
        self.assertEqual(status, 200)
        self.assertIn(f"/admin/knowledge/batch/{self.scenario['batch2']}?step=publish", page)
        self.assertNotIn(f"/admin/knowledge/batch/{self.scenario['batch1']}?step=publish", page)
        self.assertIn("/admin/knowledge/templates", page, "已采用模板时仍可上下文进入模板管理")

    def test_material_pages_point_to_current_draft_batch(self):
        # 历史原件页：明确提示当前草稿来自更新版本，并给出当前批次发布入口。
        status, page = self.get(f"/admin/knowledge/material/{self.scenario['old_source']}")
        self.assertEqual(status, 200)
        self.assertIn("当前草稿来自更新版本", page)
        self.assertIn(f"/admin/knowledge/batch/{self.scenario['batch2']}?step=publish", page)
        # 当前原件页：直接给出发布入口。
        status, page = self.get(f"/admin/knowledge/material/{self.scenario['new_source']}")
        self.assertEqual(status, 200)
        self.assertIn(f"/admin/knowledge/batch/{self.scenario['batch2']}?step=publish", page)

    def test_stale_batch_publish_step_shows_superseded_gate(self):
        status, page = self.get(f"/admin/knowledge/batch/{self.scenario['batch1']}?step=publish")
        self.assertEqual(status, 200)
        self.assertIn("属于历史原件", page)
        status, page = self.get(f"/admin/knowledge/batch/{self.scenario['batch2']}?step=publish")
        self.assertEqual(status, 200)
        self.assertNotIn("属于历史原件", page)

    def test_legacy_item_page_has_contextual_fields_entry(self):
        item_id = self.db.create_knowledge_item(
            TENANT, "article", "旧文章（上下文入口）", self.admin["id"], content={"body": "旧正文。"})
        status, page = self.get(f"/admin/knowledge/item/{item_id}")
        self.assertEqual(status, 200)
        self.assertIn(f"/admin/knowledge/fields/{item_id}", page)
        # 只读成员没有该入口（字段页需要编辑权限）。
        status, page = self.get(f"/admin/knowledge/item/{item_id}", cookie=self.member_cookie)
        self.assertEqual(status, 200)
        self.assertNotIn("灵活字段（可选模板）", page)

    def test_old_batch_publish_over_http_rejected_with_message(self):
        before = self.db.get_knowledge_draft_version(TENANT, self.scenario["item_id"])["content_json"]
        status, headers, _ = request(
            self.server, "POST", f"/admin/knowledge/batch/{self.scenario['batch1']}/settings",
            fields={"usage": "internal", "action": "publish", "confirm_ai": "0"}, cookie=self.admin_cookie)
        self.assertIn(status, (302, 303))
        location = headers["Location"]
        self.assertIn("error=", location)
        from urllib.parse import unquote
        self.assertIn("历史原件", unquote(location))
        after = self.db.get_knowledge_draft_version(TENANT, self.scenario["item_id"])["content_json"]
        self.assertEqual(after, before)

    def test_z_current_batch_publication_over_http_works(self):
        status, headers, _ = request(
            self.server, "POST", f"/admin/knowledge/batch/{self.scenario['batch2']}/settings",
            fields={"usage": "internal", "action": "publish", "confirm_ai": "0"}, cookie=self.admin_cookie)
        self.assertIn(status, (302, 303))
        self.assertNotIn("error=", headers["Location"])
        published = self.db.get_published_knowledge_version(TENANT, self.scenario["item_id"])
        self.assertEqual(published["version"], 3)
        self.assertIn("Power supply 240V", json.loads(published["content_json"])["body"])
        self.assertIsNone(self.db.get_knowledge_draft_version(TENANT, self.scenario["item_id"]))


class BatchSettingsOwnershipTests(OwnershipBase):
    """B2-B08 设置补充：历史批次 / 身份未知来源保存设置不得改写当前草稿的用途/AI/到期。

    对应独立复现 `/tmp/b2b-phase2-old-settings-review.py`：oldbatch save_settings 曾把
    新草稿的 external/AI=true/到期 2030 改成 internal/关闭/空。守卫必须在任何写入之前拒绝：
    当前草稿、当前线上、历史版本与批次行全部保持不变；混合批次行为明确（整批拒绝）；
    当前草稿所在批次保存设置与发布保持可用。
    """

    def batch_row(self, batch_id):
        return tuple(self.db.conn.execute(
            "SELECT usage, ai_authorized, expires_at, updated_at FROM kb2_batches WHERE id=?",
            (batch_id,)).fetchone())

    def draft_fields(self, item_id):
        draft = self.db.get_knowledge_draft_version(TENANT, item_id)
        if draft is None:
            return None
        return (draft["id"], draft["version"], draft["status"], draft["usage"],
                draft["ai_authorized"], draft["expires_at"], draft["content_json"], draft["source_id"])

    def test_old_batch_settings_rejected_before_writes(self):
        scenario = self.make_replaced_item()  # v1(220V) 发布 → 替换(240V) 发布 v2 → v3 草稿
        item_id = scenario["item_id"]
        # 当前草稿所在批次（替换批次）保存设置：允许，并写入草稿（保持可用）。
        self.db.kb2_save_batch_settings(
            TENANT, self.admin["id"], scenario["batch2"], usage="external", ai_authorized=True,
            expires_at="2030-01-01")
        draft_before = self.draft_fields(item_id)
        self.assertEqual(draft_before[3], "external")
        self.assertTrue(draft_before[4])
        self.assertTrue(str(draft_before[5]).startswith("2030-01-01"))
        published_before = self.published_snapshot(item_id)
        history_before = self.history_snapshot(item_id)
        old_batch_row_before = self.batch_row(scenario["batch1"])

        with self.assertRaisesRegex(ValueError, "不是当前内容的原件"):
            self.db.kb2_save_batch_settings(
                TENANT, self.admin["id"], scenario["batch1"], usage="internal", ai_authorized=False)

        # 拒绝发生在任何写入之前：草稿设置、线上、历史与旧批次行全部不变。
        self.assertEqual(self.draft_fields(item_id), draft_before)
        self.assertEqual(self.published_snapshot(item_id), published_before)
        self.assertEqual(self.history_snapshot(item_id), history_before)
        self.assertEqual(self.batch_row(scenario["batch1"]), old_batch_row_before)

    def test_mixed_batch_settings_rejected_without_partial_writes(self):
        """混合批次（一件已换版、一件仍是当前原件）：整批拒绝，不做部分写入。"""
        batch_id = self.db.kb2_create_batch(
            TENANT, self.admin["id"],
            [{"kind": "file", "filename": "a.txt", "data": b"Power supply 220V"},
             {"kind": "file", "filename": "b.txt", "data": b"Cable kit 12m"}])
        sources = self.db.kb2_get_batch(TENANT, batch_id)["sources"]
        first = next(source for source in sources if source["filename"] == "a.txt")
        second = next(source for source in sources if source["filename"] == "b.txt")
        for source in (first, second):
            self.check_all(source["id"])
        self.publish(batch_id)
        # 第二件（仍为当前原件）产生一个草稿，用于观察部分写入。
        second_paragraph = self.paragraphs_of(second["id"])[0]
        self.db.kb2_update_paragraph(TENANT, self.admin["id"], second_paragraph["id"],
                                     text="Cable kit 12m (rev B)")
        self.db.kb2_update_paragraph(TENANT, self.admin["id"], second_paragraph["id"], checked=True)
        item_first = first["item_id"]
        item_second = second["item_id"]
        draft_second_before = self.draft_fields(item_second)
        self.assertIsNotNone(draft_second_before)
        # 第一件换版（新批次）并发布 → 原批次变成混合状态（一件历史、一件当前）。
        replacement = self.db.kb2_replace_file(
            TENANT, self.admin["id"], item_first, "a3.txt", b"Power supply 440V")
        self.check_all(replacement["source_id"])
        self.publish(replacement["batch_id"])
        row_before = self.batch_row(batch_id)
        published_first_before = self.published_snapshot(item_first)

        with self.assertRaisesRegex(ValueError, "不是当前内容的原件"):
            self.db.kb2_save_batch_settings(TENANT, self.admin["id"], batch_id,
                                            usage="external", ai_authorized=True)
        # 整批拒绝：当前原件一侧的草稿也没有被部分写入。
        self.assertEqual(self.draft_fields(item_second), draft_second_before)
        self.assertEqual(self.batch_row(batch_id), row_before)
        self.assertEqual(self.published_snapshot(item_first), published_first_before)

    def test_settings_after_failed_replacement_rejected_until_identity(self):
        """失败换版批次（新原件未认领身份）：保存设置先拒绝；补人工正文认领身份后可保存。"""
        first = self.ingest("source.txt", "Power supply 220V")
        item_id = first["item_id"]
        self.check_all(first["id"])
        self.publish(self.batch_of(first["id"]))
        replacement = self.db.kb2_replace_file(TENANT, self.admin["id"], item_id, "damaged.pdf", b"broken")
        self.assertEqual(replacement["status"], "failed")
        draft_before = self.draft_fields(item_id)
        with self.assertRaisesRegex(ValueError, "不是当前内容的原件"):
            self.db.kb2_save_batch_settings(TENANT, self.admin["id"], replacement["batch_id"],
                                            usage="external", ai_authorized=True)
        self.assertEqual(self.draft_fields(item_id), draft_before)
        # 补充人工正文认领该来源自己的原件身份后，保存设置恢复可用。
        self.db.kb2_add_manual_paragraph(TENANT, self.admin["id"], replacement["source_id"],
                                         "人工正文：待补发原件。")
        self.db.kb2_save_batch_settings(TENANT, self.admin["id"], replacement["batch_id"],
                                        usage="external", ai_authorized=True)
        draft_after = self.draft_fields(item_id)
        self.assertEqual(draft_after[3], "external")
        self.assertTrue(draft_after[4])


if __name__ == "__main__":
    unittest.main()
