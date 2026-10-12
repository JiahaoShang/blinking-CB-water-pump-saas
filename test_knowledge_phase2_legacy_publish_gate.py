"""企业知识第二批 · B2-B09 共享发布服务闸门（判别性回归）。

对应独立复现 `/tmp/b2b-phase2-legacy-publish-review.py` 与用户 2026-10-12 的修复要求：
未核对的第二批段落经既有 `save_knowledge_draft` + `publish_knowledge_draft`（第一批旧入口 /
书签 HTTP 路径最终调用的同一服务）发布后，不能进入客户候选证据。

固定口径（在共享服务强制，不只靠批次页面）：
- 第二批资料（有条目来源）的草稿：正文出处必须确切（原件身份缺失/不匹配拒绝）；
- 全部收录段落必须已核对（同批次核对口径；未核对带段号拒绝）；
- 正文必须与出处来源当前段落一致（被编辑过的正文不能绕过核对来源漂移）；
- 第一批旧资料（无第二批来源）完全保持兼容；被拒绝时旧线上版本与证据不受影响
  （旧发布快照是已批准的不可变内容，不因新草稿被拦而失效）。
只用临时数据库与测试私有上传目录；不触碰 data/ 下真实演示数据与任何密钥；不接真实模型。
"""
import http.client
import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlencode, urlsplit

from app import Database, make_server

TENANT = "demo-tenant"
BOUNDARY = "----kb2legacypublishgate"


def request(server, method, path, *, fields=None, cookie=None):
    conn = http.client.HTTPConnection("127.0.0.1", server.server_port)
    headers = {}
    body = None
    if fields is not None:
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


class LegacyPublishGateBase(unittest.TestCase):
    maxDiff = None

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.upload_dir = Path(self.tmp.name) / "uploads"
        os.environ["B2B_KB_STORAGE_ROOT"] = str(self.upload_dir)
        self.db = Database(Path(self.tmp.name) / "kb2-legacy-gate.db")
        self.db.knowledge_storage_root = self.upload_dir
        self.db.seed_demo_users()
        self.admin = self.db.authenticate_user("demo-admin@example.invalid", "demo-admin")

    def tearDown(self):
        self.db.conn.close()
        os.environ.pop("B2B_KB_STORAGE_ROOT", None)
        self.tmp.cleanup()

    def ingest(self, filename, text):
        batch_id = self.db.kb2_create_batch(
            TENANT, self.admin["id"], [{"kind": "file", "filename": filename, "data": text.encode()}])
        return batch_id, self.db.kb2_get_batch(TENANT, batch_id)["sources"][0]

    def check_all(self, source_id):
        for row in self.db.conn.execute(
                "SELECT id FROM kb2_paragraphs WHERE tenant_id=? AND source_id=? ORDER BY ordinal",
                (TENANT, source_id)).fetchall():
            self.db.kb2_update_paragraph(TENANT, self.admin["id"], row["id"], checked=True)

    def first_paragraph(self, source_id):
        return self.db.conn.execute(
            "SELECT * FROM kb2_paragraphs WHERE tenant_id=? AND source_id=? ORDER BY ordinal LIMIT 1",
            (TENANT, source_id)).fetchone()

    def publish_external(self, item_id, source_id):
        """经批次路径发布为对外 + 授权 AI + 准备完成（证据链对齐第一批口径）。"""
        batch_id = self.db.conn.execute(
            "SELECT batch_id FROM kb2_sources WHERE id=?", (source_id,)).fetchone()[0]
        result = self.db.kb2_publish_batch(TENANT, self.admin["id"], batch_id,
                                           usage="external", ai_authorized=True, confirm_ai=True)
        self.assertFalse(result["failed"], result)
        self.db.prepare_knowledge_version(
            TENANT, self.db.get_published_knowledge_version(TENANT, item_id)["id"])

    def evidence_texts(self):
        return [unit.get("text") or "" for unit in self.db.list_customer_qa_evidence(TENANT)]

    def evidence_contains(self, needle):
        return any(needle in text for text in self.evidence_texts())


class SharedServiceGateTests(LegacyPublishGateBase):
    def test_unchecked_kb2_paragraph_cannot_publish_via_shared_service(self):
        """复现脚本场景：未核对段落经 legacy 保存 + 发布 → 拒绝；无版本、无证据。"""
        batch_id, source = self.ingest("unchecked.txt", "Unchecked warranty is 240 months.")
        item_id = source["item_id"]
        self.db.save_knowledge_draft(TENANT, item_id, self.admin["id"], usage="external", ai_authorized=True)
        with self.assertRaisesRegex(ValueError, "未核对"):
            self.db.publish_knowledge_draft(TENANT, item_id, self.admin["id"], confirm_ai=True)
        # 没有发布、没有新版本、没有客户候选证据；段落仍是未核对。
        self.assertIsNone(self.db.get_published_knowledge_version(TENANT, item_id))
        paragraph = self.first_paragraph(source["id"])
        self.assertFalse(paragraph["checked"])
        self.assertFalse(self.evidence_contains("Unchecked warranty is 240 months."))
        # 正对照：显式核对后，共享服务可以发布，证据按真实内容进入候选。
        self.db.kb2_update_paragraph(TENANT, self.admin["id"], paragraph["id"], checked=True)
        version = self.db.publish_knowledge_draft(TENANT, item_id, self.admin["id"], confirm_ai=True)
        self.assertEqual(version, 1)
        self.db.prepare_knowledge_version(
            TENANT, self.db.get_published_knowledge_version(TENANT, item_id)["id"])
        self.assertTrue(self.evidence_contains("Unchecked warranty is 240 months."))

    def test_edited_body_drift_rejected_and_old_published_snapshot_unaffected(self):
        """已发布资料：legacy 编辑正文（与已核对出处不一致）发布 → 拒绝；旧线上与证据不变。"""
        batch_id, source = self.ingest("warranty.txt", "保修24个月。")
        item_id = source["item_id"]
        self.check_all(source["id"])
        self.publish_external(item_id, source["id"])
        self.assertTrue(self.evidence_contains("保修24个月。"))
        published_before = self.db.get_published_knowledge_version(TENANT, item_id)

        self.db.save_knowledge_draft(TENANT, item_id, self.admin["id"], content={"body": "保修36个月。"})
        with self.assertRaisesRegex(ValueError, "不一致"):
            self.db.publish_knowledge_draft(TENANT, item_id, self.admin["id"], confirm_ai=True)

        after = self.db.get_published_knowledge_version(TENANT, item_id)
        self.assertEqual(after["id"], published_before["id"], "旧线上版本不受未发布草稿影响")
        self.assertEqual(after["content_json"], published_before["content_json"])
        self.assertTrue(self.evidence_contains("保修24个月。"), "旧发布快照（已批准内容）继续可用")

    def test_unchecked_new_draft_does_not_invalidate_old_published_version(self):
        """段落被编辑（撤销核对）产生新草稿：legacy 发布被拦；旧线上证据仍在。"""
        batch_id, source = self.ingest("policy.txt", "响应时间不超过两小时。")
        item_id = source["item_id"]
        self.check_all(source["id"])
        self.publish_external(item_id, source["id"])
        published_before = self.db.get_published_knowledge_version(TENANT, item_id)

        paragraph = self.first_paragraph(source["id"])
        self.db.kb2_update_paragraph(TENANT, self.admin["id"], paragraph["id"], text="响应时间不超过一小时。")
        draft = self.db.get_knowledge_draft_version(TENANT, item_id)
        self.assertIsNotNone(draft)
        with self.assertRaisesRegex(ValueError, "未核对"):
            self.db.publish_knowledge_draft(TENANT, item_id, self.admin["id"], confirm_ai=True)
        self.assertEqual(self.db.get_published_knowledge_version(TENANT, item_id)["id"],
                         published_before["id"])
        self.assertTrue(self.evidence_contains("响应时间不超过两小时。"))

    def test_first_batch_items_without_kb2_sources_stay_compatible(self):
        """第一批旧资料（无第二批来源）经共享服务编辑 + 发布：行为不变。"""
        item_id = self.db.create_knowledge_item(
            TENANT, "article", "旧资料兼容", self.admin["id"], content={"body": "旧正文。"})
        source_id = self.db.create_knowledge_source(
            TENANT, item_id, self.admin["id"], kind="manual", source_location="测试手工登记来源")
        self.db.save_knowledge_draft(TENANT, item_id, self.admin["id"], source_id=source_id)
        self.db.publish_knowledge_draft(TENANT, item_id, self.admin["id"])
        self.db.save_knowledge_draft(TENANT, item_id, self.admin["id"], content={"body": "旧正文（修订）。"})
        version = self.db.publish_knowledge_draft(TENANT, item_id, self.admin["id"])
        self.assertEqual(version, 2)
        content = json.loads(self.db.get_published_knowledge_version(TENANT, item_id)["content_json"])
        self.assertEqual(content["body"], "旧正文（修订）。")

    def test_draft_with_unverifiable_source_identity_rejected(self):
        """草稿正文出处指向非第二批原件（来源身份无法核实）→ 拒绝发布。"""
        batch_id, source = self.ingest("identity.txt", "身份校验正文。")
        item_id = source["item_id"]
        self.check_all(source["id"])
        other_source = self.db.create_knowledge_source(
            TENANT, item_id, self.admin["id"], kind="manual", source_location="另一登记来源")
        self.db.save_knowledge_draft(TENANT, item_id, self.admin["id"], source_id=other_source)
        with self.assertRaisesRegex(ValueError, "出处"):
            self.db.publish_knowledge_draft(TENANT, item_id, self.admin["id"])
        self.assertIsNone(self.db.get_published_knowledge_version(TENANT, item_id))


class LegacyHttpBookmarkGateTests(unittest.TestCase):
    """旧书签/HTTP 路径（第一批 /item/<id>/publish 表单）不能绕过共享闸门。"""

    maxDiff = None

    @classmethod
    def setUpClass(cls):
        cls.temp_dir = tempfile.TemporaryDirectory()
        cls.upload_dir = Path(cls.temp_dir.name) / "uploads"
        os.environ["B2B_KB_STORAGE_ROOT"] = str(cls.upload_dir)
        cls.db_path = Path(cls.temp_dir.name) / "kb2-legacy-http.db"
        cls.server = make_server(cls.db_path, port=0)
        cls.db = cls.server.RequestHandlerClass.db
        cls.db.knowledge_storage_root = cls.upload_dir
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.db.seed_demo_users()
        cls.admin = cls.db.authenticate_user("demo-admin@example.invalid", "demo-admin")
        cls.admin_cookie = login(cls.server, "demo-admin@example.invalid", "demo-admin")

    @classmethod
    def tearDownClass(cls):
        cls.server.server_close()
        cls.db.conn.close()
        os.environ.pop("B2B_KB_STORAGE_ROOT", None)
        cls.temp_dir.cleanup()

    def test_bookmarked_legacy_publish_rejected_over_http(self):
        batch_id = self.db.kb2_create_batch(
            TENANT, self.admin["id"],
            [{"kind": "file", "filename": "unchecked.txt", "data": b"Unchecked warranty is 240 months."}])
        source = self.db.kb2_get_batch(TENANT, batch_id)["sources"][0]
        item_id = source["item_id"]
        self.db.save_knowledge_draft(TENANT, item_id, self.admin["id"], usage="external", ai_authorized=True)
        # 旧资料页（书签）的发布按钮：POST /admin/knowledge/item/<id>/publish。
        status, headers, _ = request(self.server, "POST", f"/admin/knowledge/item/{item_id}/publish",
                                     fields={"confirmed": "1"}, cookie=self.admin_cookie)
        self.assertIn(status, (302, 303))
        location = headers.get("Location", "")
        self.assertIn("error=", location)
        self.assertIn("未核对", unquote(location))
        # 未发布、无证据（HTTP 层与共享服务同一事实）。
        self.assertIsNone(self.db.get_published_knowledge_version(TENANT, item_id))
        self.assertEqual(batch_id, self.db.conn.execute(
            "SELECT batch_id FROM kb2_sources WHERE id=?", (source["id"],)).fetchone()[0])


if __name__ == "__main__":
    unittest.main()
