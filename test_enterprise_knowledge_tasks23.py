"""企业知识第一批 · 任务2/3：导入、产品适配、有效期与真实管理界面。

- 任务2（导入 / 产品适配 / 来源版本）：真实保存 TXT/MD/CSV 字节、私有读取、空正文拒绝发布、
  HTML 在界面中转义、文件上限与格式错误；原产品按「已核准 + 对外」字段过滤，产品生命周期
  仍以产品模块为唯一权威（不删除重建、推荐/询价关联保留）；可选到期时间支持精确时间戳
  与 Asia/Shanghai 输入、按 UTC 归一，检索前排除（now >= 到期即排除）。
- 任务3（管理界面）：本企业成员可读、只读时无编辑入口；搜索清空保留筛选；折叠导航默认收起、
  子项 inert；发布/授权需确认；处理完成必须经过真实本地内容准备（投影/分块），不靠计时。
只用临时数据库与临时上传目录；不触碰 data/ 与任何密钥。
"""
import io
import json
import os
import tempfile
import threading
import unittest
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path

from app import Database

TENANT = "demo-tenant"


class Tasks23Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        # 导入文件写入测试私有目录，不碰仓库 data/uploads；HTTP 处理会按请求重建
        # Database 实例，环境变量在两处路径都生效（见 enterprise_knowledge._knowledge_storage_dir）。
        os.environ["B2B_KB_STORAGE_ROOT"] = str(Path(self.tmp.name) / "uploads")
        self.db = Database(Path(self.tmp.name) / "knowledge.db")
        self.db.seed_demo_users()
        self.admin = self.db.authenticate_user("demo-admin@example.invalid", "demo-admin")
        self.member = self.db.authenticate_user("demo-sales@example.invalid", "demo-sales")
        self.db.knowledge_storage_root = Path(self.tmp.name) / "uploads"

    def tearDown(self):
        self.db.conn.close()
        self.tmp.cleanup()
        os.environ.pop("B2B_KB_STORAGE_ROOT", None)

    def create_article(self, title="保修政策", body="保修 24 个月。", **kw):
        return self.db.create_knowledge_item(
            TENANT, "article", title, self.admin["id"], content={"body": body}, **kw)

    def publish_prepared(self, item_id, **kw):
        "发布并执行真实本地内容准备（模拟服务器在发布后立即准备）。"
        self.db.publish_knowledge_draft(TENANT, item_id, self.admin["id"], **kw)
        version = self.db.get_published_knowledge_version(TENANT, item_id)
        self.db.prepare_knowledge_version(TENANT, version["id"])
        return version


class ExpiryTests(Tasks23Base):
    def test_normalize_accepts_exact_timestamp_and_legacy_date(self):
        normalize = self.db.normalize_knowledge_expiry
        # Asia/Shanghai（北京时间，UTC+8）无时区标注的日期时间按东八区解释，存 UTC。
        self.assertEqual(normalize("2026-10-10T18:00"), "2026-10-10T10:00:00+00:00")
        self.assertEqual(normalize("2026-10-10T18:00:00+08:00"), "2026-10-10T10:00:00+00:00")
        self.assertEqual(normalize("2026-10-10T10:00:00Z"), "2026-10-10T10:00:00+00:00")
        # 旧日期格式兼容：按当日结束（UTC 23:59:59）处理，行为与旧版「当天仍有效」一致。
        self.assertEqual(normalize("2026-10-10"), "2026-10-10T23:59:59+00:00")
        self.assertIsNone(normalize(""))
        self.assertIsNone(normalize(None))
        for bad in ("2026/10/10", "10-10-2026", "2026-10-10T25:00"):
            with self.assertRaises(ValueError):
                normalize(bad)

    def test_exclude_at_exact_expiry_boundary(self):
        "now >= 到期时间即排除；到期前 1 秒仍可用。"
        moment = datetime(2026, 10, 10, 12, 0, 0, tzinfo=timezone.utc)
        item_id = self.create_article(
            title="展会报价", usage="external", ai_authorized=True, expires_at="2026-10-10T20:00")  # 北京 20:00 = UTC 12:00
        self.db.publish_knowledge_draft(TENANT, item_id, self.admin["id"], confirm_ai=True)
        version = self.db.get_published_knowledge_version(TENANT, item_id)
        self.db.prepare_knowledge_version(TENANT, version["id"])
        self.assertEqual([r["version_id"] for r in self.db.list_ai_answerable_knowledge(TENANT, item_id, now=moment)], [])
        before = datetime(2026, 10, 10, 11, 59, 59, tzinfo=timezone.utc)
        self.assertEqual([r["version_id"] for r in self.db.list_ai_answerable_knowledge(TENANT, item_id, now=before)], [version["id"]])
        # 存储层已归一为 UTC。
        self.assertEqual(self.db.get_knowledge_version(TENANT, version["id"])["expires_at"], "2026-10-10T12:00:00+00:00")

    def test_legacy_date_only_rows_still_filtered(self):
        expired = self.create_article(title="过期政策", usage="external", ai_authorized=True, expires_at="2020-01-01")
        self.publish_prepared(expired, confirm_ai=True)
        self.assertEqual(self.db.list_ai_answerable_knowledge(TENANT, expired), [])
        fresh = self.create_article(title="长期政策", usage="external", ai_authorized=True, expires_at="2099-12-31")
        fresh_v = self.publish_prepared(fresh, confirm_ai=True)
        self.assertEqual([r["version_id"] for r in self.db.list_ai_answerable_knowledge(TENANT, fresh)], [fresh_v["id"]])


class ImportTests(Tasks23Base):
    def test_text_import_persists_bytes_and_reviewable_text(self):
        content = "# 服务政策\n\n整机保修 24 个月，全国联保。\n".encode("utf-8")
        result = self.db.import_knowledge_file(
            TENANT, self.admin["id"], filename="服务政策.md", data=content, title="服务政策", usage="internal")
        item = self.db.get_knowledge_item(TENANT, result["item_id"])
        self.assertEqual(item["status"], "draft")
        draft = self.db.get_knowledge_draft_version(TENANT, result["item_id"])
        self.assertIn("整机保修 24 个月", json.loads(draft["content_json"])["body"])
        source = self.db.list_knowledge_sources(TENANT, result["item_id"])[0]
        self.assertEqual(source["kind"], "file")
        self.assertTrue(source["storage_path"])
        row, data = self.db.read_knowledge_source_file(TENANT, source["id"])
        self.assertEqual(data, content)
        # 重开数据库后原件仍在磁盘、来源仍可读（真实持久化）。
        self.db.conn.close()
        self.db = Database(Path(self.tmp.name) / "knowledge.db")
        self.db.knowledge_storage_root = Path(self.tmp.name) / "uploads"
        row, data = self.db.read_knowledge_source_file(TENANT, source["id"])
        self.assertEqual(data, content)

    def test_csv_is_text_material_without_field_inference(self):
        csv_bytes = "model,name\nCB-80A,清水泵\n".encode("utf-8")
        result = self.db.import_knowledge_file(TENANT, self.admin["id"], filename="清单.csv", data=csv_bytes)
        draft = self.db.get_knowledge_draft_version(TENANT, result["item_id"])
        snapshot = json.loads(draft["content_json"])
        self.assertIn("CB-80A", snapshot["body"])
        self.assertNotIn("fields", snapshot)  # CSV 不自动推断行业字段

    def test_rejects_bad_format_and_oversize(self):
        with self.assertRaises(ValueError):
            self.db.import_knowledge_file(TENANT, self.admin["id"], filename="病毒.exe", data=b"data")
        with self.assertRaises(ValueError):
            self.db.import_knowledge_file(TENANT, self.admin["id"], filename="超大.txt", data=b"a" * (1024 * 1024 + 1))
        with self.assertRaises(PermissionError):
            self.db.import_knowledge_file(TENANT, self.member["id"], filename="越权.txt", data=b"x")

    def test_pdf_registers_raw_without_auto_text_and_blocks_publish_until_manual_body(self):
        pdf = b"%PDF-1.4 fake"
        result = self.db.import_knowledge_file(TENANT, self.admin["id"], filename="手册.pdf", data=pdf, title="产品手册")
        self.assertIsNone(result["text"])
        draft = self.db.get_knowledge_draft_version(TENANT, result["item_id"])
        self.assertFalse(json.loads(draft["content_json"]).get("body", "").strip())
        with self.assertRaises(ValueError):
            self.db.publish_knowledge_draft(TENANT, result["item_id"], self.admin["id"])
        # 人工整理正文后可保存草稿。
        self.db.save_knowledge_draft(TENANT, result["item_id"], self.admin["id"], content={"body": "人工整理：保修 24 个月。"})
        self.db.publish_knowledge_draft(TENANT, result["item_id"], self.admin["id"])
        row, data = self.db.read_knowledge_source_file(TENANT, result["source_id"])
        self.assertEqual(data, pdf)

    def test_private_source_read_is_tenant_scoped(self):
        self.db.ensure_tenant("other-tenant", "其他企业")
        result = self.db.import_knowledge_file(TENANT, self.admin["id"], filename="机密.txt", data="内部内容".encode("utf-8"))
        with self.assertRaises(ValueError):
            self.db.read_knowledge_source_file("other-tenant", result["source_id"])


class ProductAdapterTests(Tasks23Base):
    def make_product(self):
        product_id = self.db.create_product(TENANT, "3SV08NL007T", "立式多级离心泵", "高层供水", "demo-admin")
        source_id = self.db.add_source(TENANT, product_id, "系列目录.pdf", "PDF", "第 1 页", actor="demo-admin", scope="exact_model")
        self.db.save_field(TENANT, product_id, "flow_range", "4-28", "m³/h", source_id, "第 1 页", "demo-admin")
        self.db.save_field(TENANT, product_id, "head_range", "20-160", "m", source_id, "第 2 页", "demo-admin")
        self.db.save_field(TENANT, product_id, "power", "7.5", "kW", source_id, "第 2 页", "demo-admin")
        self.db.approve_field(TENANT, product_id, "flow_range", "demo-admin")
        self.db.approve_field(TENANT, product_id, "head_range", "demo-admin")
        self.db.reject_field(TENANT, product_id, "power", "未经工程确认", "demo-admin")
        # 仅内部字段：已核准也不进入客户视角。
        self.db.conn.execute("UPDATE product_fields SET audience='internal' WHERE tenant_id=? AND product_id=? AND field_key='head_range'", (TENANT, product_id))
        self.db.conn.commit()
        self.db.finalize_product_review(TENANT, product_id, "demo-admin")
        return product_id

    def test_field_level_filtering_and_lifecycle_projection(self):
        product_id = self.make_product()
        before = self.db.get_product(TENANT, product_id)
        # 清单10：未发布前没有客户视角投影（一切来自已发布快照）。
        self.assertEqual(self.db.knowledge_product_fields(TENANT, product_id), [])
        self.db.publish_product(TENANT, product_id, "demo-admin")
        eligible = {f["field_key"] for f in self.db.knowledge_product_fields(TENANT, product_id)}
        self.assertEqual(eligible, {"flow_range"})
        # 清单11：已发布产品默认还没有 AI 客服授权；显式授权后才进入客户问答候选。
        self.assertEqual(self.db.list_ai_answerable_product_facts(TENANT, product_id), [])
        self.assertEqual(self.db.knowledge_product_ai_gate(TENANT, product_id), (False, "未授权 AI 客服使用"))
        self.db.set_knowledge_product_authorization(
            TENANT, product_id, self.admin["id"], authorized=True, confirmed=True)
        facts = self.db.list_ai_answerable_product_facts(TENANT, product_id)
        self.assertEqual(len(facts), 1)
        self.assertEqual({f["label"] for f in facts[0]["facts"]}, {"流量范围"})
        self.assertTrue(all(f["audience"] == "external" for f in facts[0]["facts"]))
        # 撤回后立即不可用（产品生命周期仍以产品模块为准）。
        self.db.withdraw_product(TENANT, product_id, "demo-admin")
        self.assertEqual(self.db.list_ai_answerable_product_facts(TENANT, product_id), [])
        # 适配为只读投影：不删除、不重建、不修改原产品行；授权只写侧车表。
        after = self.db.get_product(TENANT, product_id)
        self.assertEqual((before["id"], after["id"]), (product_id, product_id))
        self.assertEqual(after["created_at"], before["created_at"])

    def test_product_publish_requires_valid_fields(self):
        product_id = self.db.create_product(TENANT, "GX-9", "变频供水机组", "恒压供水", "demo-admin")
        with self.assertRaises(ValueError):
            self.db.publish_product(TENANT, product_id, "demo-admin")
        source_id = self.db.add_source(TENANT, product_id, "手册.pdf", "PDF", "第 1 页", actor="demo-admin")
        self.db.save_field(TENANT, product_id, "flow_range", "", "", source_id, "第 1 页", "demo-admin")
        with self.assertRaises(ValueError):
            self.db.approve_field(TENANT, product_id, "flow_range", "demo-admin")

    def test_adaptation_keeps_recommendation_and_rfq_associations(self):
        product_id = self.make_product()
        self.db.publish_product(TENANT, product_id, "demo-admin")
        version = self.db.get_published_version(TENANT, product_id)
        conversation_id = self.db.create_conversation(TENANT)
        self.db.conn.execute(
            "INSERT INTO requirement_revisions(id,tenant_id,conversation_id,revision,values_json,created_by,created_at) "
            "VALUES ('rev-1',?,?,1,'{}','demo-admin','2026-01-01')",
            (TENANT, conversation_id))
        self.db.conn.execute(
            "INSERT INTO recommendations(id,tenant_id,conversation_id,requirement_revision_id,status,rule_version,results_json,created_at) "
            "VALUES ('rec-1',?,?, 'rev-1','served','v1',?, '2026-01-01')",
            (TENANT, conversation_id, json.dumps([{"product_id": product_id, "version_id": version["id"]}])))
        self.db.conn.execute(
            "INSERT INTO rfqs(id,tenant_id,conversation_id,product_id,product_version_id,submission_key,snapshot_json,created_at) "
            "VALUES ('rfq-1',?,?,?,?, 'sub-1','{}','2026-01-01')",
            (TENANT, conversation_id, product_id, version["id"]))
        self.db.conn.commit()
        # 适配投影发生后，推荐与询价关联仍指向原产品 ID。
        self.db.list_knowledge_product_overview(TENANT)
        row = self.db.conn.execute("SELECT product_id, product_version_id FROM rfqs WHERE id='rfq-1'").fetchone()
        self.assertEqual((row["product_id"], row["product_version_id"]), (product_id, version["id"]))
        rec = self.db.conn.execute("SELECT results_json FROM recommendations WHERE id='rec-1'").fetchone()
        self.assertEqual(json.loads(rec["results_json"])[0]["product_id"], product_id)

    def test_manual_service_item_is_industry_neutral(self):
        item_id = self.db.create_knowledge_item(
            TENANT, "service", "现场安装服务", self.admin["id"],
            content={"body": "提供现场安装与调试，按项目报价。"}, usage="external")
        self.publish_prepared(item_id)
        draft = self.db.get_published_knowledge_version(TENANT, item_id)
        snapshot = json.loads(draft["content_json"])
        self.assertNotIn("model", snapshot)  # 行业中立：不套用水泵字段模板


class ProcessingTests(Tasks23Base):
    def test_not_ready_until_real_local_preparation(self):
        item_id = self.create_article(title="发货与物流", body="一般 3-5 天到货。", usage="external", ai_authorized=True)
        self.db.publish_knowledge_draft(TENANT, item_id, self.admin["id"], confirm_ai=True)
        version = self.db.get_published_knowledge_version(TENANT, item_id)
        # 发布后为「准备中」：没有真实内容准备前不得声称可用。
        self.assertEqual(self.db.knowledge_processing_state(TENANT, version["id"])["status"], "pending")
        self.assertEqual(self.db.list_ai_answerable_knowledge(TENANT, item_id), [])
        count = self.db.prepare_knowledge_version(TENANT, version["id"])
        self.assertGreater(count, 0)
        chunks = self.db.list_knowledge_chunks(TENANT, version["id"])
        self.assertTrue(any("3-5 天" in chunk["text"] for chunk in chunks))
        self.assertEqual(self.db.knowledge_processing_state(TENANT, version["id"])["status"], "completed")
        self.assertEqual([r["version_id"] for r in self.db.list_ai_answerable_knowledge(TENANT, item_id)], [version["id"]])

    def test_prepare_never_completes_empty_content(self):
        "没有真实可投影内容时不得标记完成（防「无条件完成」）。"
        item_id = self.db.create_knowledge_item(TENANT, "article", "空稿", self.admin["id"], content={"body": ""})
        draft = self.db.get_knowledge_draft_version(TENANT, item_id)
        with self.assertRaises(ValueError):
            self.db.prepare_knowledge_version(TENANT, draft["id"])
        state = self.db.knowledge_processing_state(TENANT, draft["id"])
        self.assertEqual(state["status"], "failed")
        self.assertTrue(state["error"])


class UITests(Tasks23Base):
    def setUp(self):
        super().setUp()
        from app import make_server
        from test_langbot_bridge import form_request, login
        self.form_request = form_request
        self.server = make_server(Path(self.tmp.name) / "http.db", port=0)
        self.server.RequestHandlerClass.db.conn.close()
        self.server.RequestHandlerClass.db = self.db
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.cookies = {
            self.admin["id"]: login(self.server, "demo-admin@example.invalid", "demo-admin"),
            self.member["id"]: login(self.server, "demo-sales@example.invalid", "demo-sales"),
        }

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        super().tearDown()

    def get(self, path, user=None):
        return self.form_request(self.server, "GET", path, None, self.cookies[(user or self.admin)["id"]])

    def post(self, path, fields, user=None):
        return self.form_request(self.server, "POST", path, fields, self.cookies[(user or self.admin)["id"]])

    def post_file(self, path, filename, content, user=None):
        import http.client
        boundary = "----kb-boundary-42"
        body = (
            ("--" + boundary + "\r\n")
            + ('Content-Disposition: form-data; name="file"; filename="%s"\r\n' % filename)
            + "Content-Type: application/octet-stream\r\n\r\n"
        ).encode("utf-8") + content + ("\r\n--" + boundary + "--\r\n").encode()
        conn = http.client.HTTPConnection("127.0.0.1", self.server.server_port)
        conn.request("POST", path, body=body, headers={
            "Content-Type": "multipart/form-data; boundary=" + boundary,
            "Cookie": self.cookies[(user or self.admin)["id"]],
        })
        response = conn.getresponse()
        raw = response.read().decode("utf-8", errors="replace")
        conn.close()
        return response.status, raw

    def test_member_read_only_no_edit_entry(self):
        item_id = self.create_article()
        code, _, page = self.get("/admin/knowledge", self.member)
        self.assertEqual(code, 200)
        self.assertIn("企业知识", page)
        self.assertIn("只读", page)
        self.assertNotIn("添加知识", page)
        code, _, page = self.get("/admin/knowledge/item/" + item_id + "/edit", self.member)
        self.assertEqual(code, 200)
        self.assertIn("只读", page)
        code, _, page = self.post("/admin/knowledge/item/" + item_id + "/disable", {"confirmed": "1"}, self.member)
        self.assertEqual(code, 403)

    def test_nav_collapsed_default_with_sub_items(self):
        code, _, page = self.get("/admin/knowledge")
        self.assertEqual(code, 200)
        self.assertIn("aria-controls='nav-knowledge-subs'", page)
        self.assertIn("aria-expanded='false'", page)
        self.assertIn("nav-subs", page)
        # 2026-10-12 知识四项导航（v0.3 §5）：全部主题（主链接）与子项。
        self.assertIn("全部资料", page)
        self.assertIn("添加资料", page)
        self.assertIn("问答测试", page)
        self.assertIn("prefers-reduced-motion", page)

    def test_search_clear_keeps_filters_and_hides_without_query(self):
        self.publish_prepared(self.create_article(title="整机保修与售后响应政策"), confirm_ai=True)
        # 旧「全部知识」列表（?view=all）保留旧版搜索清空标记。
        _, _, page = self.get("/admin/knowledge?view=all&q=%E4%BF%9D%E4%BF%AE&status=published")
        self.assertIn("整机保修", page)
        self.assertIn("aria-label='清空搜索'", page)
        self.assertIn("status=published", page)  # 清空搜索的链接保留其他筛选
        _, _, page = self.get("/admin/knowledge?view=all&status=published")
        # 清单2 修订：清空按钮始终在 DOM（JS 输入时即时显隐），无关键词时带 hidden。
        self.assertRegex(page, r"data-kb-clear[^>]* hidden")

    def test_publish_flow_badges_and_disable_confirm(self):
        code, _, _ = self.post("/admin/knowledge/new", {"form": "article", "title": "发货 FAQ", "body": "3-5 天到货。", "usage": "external", "ai_authorized": "1"})
        self.assertEqual(code, 303)
        item = self.db.list_knowledge_items(TENANT)[0]
        # 发布对外+授权需确认：未确认时保持草稿。
        code, _, _ = self.post(f"/admin/knowledge/item/{item['id']}/publish", {})
        self.assertEqual(code, 303)
        self.assertEqual(self.db.get_knowledge_item(TENANT, item["id"])["status"], "draft")
        code, _, _ = self.post(f"/admin/knowledge/item/{item['id']}/publish", {"confirmed": "1"})
        self.assertEqual(code, 303)
        self.assertEqual(self.db.get_knowledge_item(TENANT, item["id"])["status"], "published")
        _, _, page = self.get("/admin/knowledge/item/" + item["id"])
        self.assertIn("已发布（AI客服可用）", page)
        # 停用需确认；停用后立即显示已停用。
        self.post(f"/admin/knowledge/item/{item['id']}/disable", {})
        self.assertEqual(self.db.get_knowledge_item(TENANT, item["id"])["status"], "published")
        self.post(f"/admin/knowledge/item/{item['id']}/disable", {"confirmed": "1"})
        _, _, page = self.get("/admin/knowledge/item/" + item["id"])
        self.assertIn("已停用", page)
        # 不可用（未授权）为中性蓝灰标签，可用为绿色（类名区分）。
        other = self.create_article(title="内训材料", usage="internal")
        self.publish_prepared(other)
        _, _, page = self.get("/admin/knowledge/item/" + other)
        self.assertIn("已发布（AI客服不可用）", page)
        self.assertIn("仅内部", page)

    def test_import_flow_and_private_download(self):
        code, page = self.post_file("/admin/knowledge/import", "服务承诺.txt", "保修 24 个月。".encode("utf-8"))
        self.assertEqual(code, 303)
        item = [i for i in self.db.list_knowledge_items(TENANT)][0]
        _, _, page = self.get("/admin/knowledge/item/" + item["id"])
        self.assertIn("原件", page)
        source = self.db.list_knowledge_sources(TENANT, item["id"])[0]
        code, _, body = self.get("/admin/knowledge/files/" + source["id"])
        self.assertIn(code, (200, 302, 303))
        # 恶意 HTML 在服务器渲染时被转义，不做原样输出。
        item2 = self.db.import_knowledge_file(TENANT, self.admin["id"], filename="危险.txt", data=b"<script>alert(1)</script>")["item_id"]
        _, _, page = self.get("/admin/knowledge/item/" + item2)
        self.assertNotIn("<script>alert(1)</script>", page)
        self.assertIn("&lt;script&gt;", page)


if __name__ == "__main__":
    unittest.main()
