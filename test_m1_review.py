import http.client
import json
import tempfile
import threading
import unittest
from pathlib import Path

from app import Database, Handler, UPLOAD_DIR, answer_from_approved_material, make_server


def cleanup_upload(case, *source_ids):
    for source_id in source_ids:
        for path in UPLOAD_DIR.glob(f"{source_id}-*"):
            case.addCleanup(path.unlink, missing_ok=True)


class FakeHandler:
    tenant_id = "demo-tenant"
    captured = None
    render_product_page = Handler.render_product_page

    def __init__(self, database):
        self.db = database

    def send_html(self, content, status=200):
        self.captured = (content, status)

    def send_bytes(self, *args, **kwargs):
        self.captured = ("file served", 200)


class ExtractionReviewTests(unittest.TestCase):
    """M1: content-based extraction must be reviewable, explicit and never guess."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.temp_dir.name) / "app.db")
        # 测试夹具企业需显式创建（普通新库不再预置）。
        self.db.ensure_tenant("demo-tenant", "示例水泵企业（演示）")

    def tearDown(self):
        self.db.conn.close()
        self.temp_dir.cleanup()

    def _product(self, model: str) -> str:
        return self.db.create_product("demo-tenant", model, f"{model} 测试泵", "清水循环")

    def test_text_content_extracts_candidates_with_line_sources(self):
        product_id = self._product("EX-1")
        content = "产品参数表\n流量范围: 20-80 m³/h\n扬程范围：18-42 m\n介质: 清水\n材质,铸铁泵体\n文件备注：普通文字\n".encode("utf-8")
        source_id = self.db.add_source("demo-tenant", product_id, "EX-1-参数.txt", "TXT", "第 1 节", content=content, scope="exact_model")
        cleanup_upload(self, source_id)
        source = next(item for item in self.db.get_sources("demo-tenant", product_id) if item["id"] == source_id)
        self.assertEqual(source["processing_status"], "extracted")
        items = json.loads(source["extraction_json"])
        self.assertEqual({item["field_key"] for item in items}, {"flow_range", "head_range", "media", "material"})
        flow = next(item for item in items if item["field_key"] == "flow_range")
        self.assertEqual((flow["value"], flow["unit"]), ("20-80", "m³/h"))
        self.assertEqual(flow["location"], "第 2 行")
        self.assertEqual(flow["review_status"], "new")
        fields = {field["field_key"]: field for field in self.db.get_fields("demo-tenant", product_id)}
        self.assertEqual(fields["flow_range"]["state"], "candidate")
        self.assertEqual(fields["flow_range"]["origin"], "extracted")
        self.assertEqual(fields["flow_range"]["source_document_id"], source_id)
        self.assertEqual(fields["flow_range"]["source_location"], "第 2 行")
        self.assertEqual(self.db.get_product("demo-tenant", product_id)["status"], "pending_review")

    def test_unsupported_and_unmatched_files_fail_clearly_and_keep_manual_entry(self):
        product_id = self._product("EX-2")
        tricky = self.db.add_source(
            "demo-tenant", product_id, "流量20-80参数.pdf", "PDF", "第 1 页",
            content=b"%PDF-1.4 fake \x00 binary payload", scope="exact_model",
        )
        unmatched = self.db.add_source(
            "demo-tenant", product_id, "notes.txt", "TXT", "全文",
            content=b"hello world, no labelled fields here", scope="exact_model",
        )
        cleanup_upload(self, tricky, unmatched)
        sources = {source["id"]: source for source in self.db.get_sources("demo-tenant", product_id)}
        self.assertEqual(sources[tricky]["processing_status"], "extraction_unsupported")
        self.assertIn("人工录入", sources[tricky]["extraction_note"])
        self.assertEqual(sources[unmatched]["processing_status"], "extraction_failed")
        self.assertIn("未识别到", sources[unmatched]["extraction_note"])
        # 文件名包含“流量20-80”也不得生成候选；不支持格式必须明确失败
        self.assertEqual(self.db.get_fields("demo-tenant", product_id), [])
        # 失败后保留手工录入路径，录入值仍须逐项核准
        self.db.save_field("demo-tenant", product_id, "flow_range", "20-80", "m³/h", tricky, "第 1 页：规格表（人工核对）")
        field = self.db.get_fields("demo-tenant", product_id)[0]
        self.assertEqual((field["state"], field["origin"]), ("candidate", "manual"))
        self.assertEqual(self.db.get_product("demo-tenant", product_id)["status"], "pending_review")

    def test_review_gate_blocks_unhandled_candidates_and_rejection_keeps_value(self):
        product_id = self._product("EX-3")
        content = "流量范围: 20-80 m³/h\n扬程范围: 18-42 m\n".encode()
        source_id = self.db.add_source("demo-tenant", product_id, "EX-3.txt", "TXT", "第 1 节", content=content, scope="exact_model")
        cleanup_upload(self, source_id)
        with self.assertRaises(ValueError):
            self.db.finalize_product_review("demo-tenant", product_id)
        with self.assertRaises(ValueError):
            self.db.publish_product("demo-tenant", product_id)
        self.db.approve_field("demo-tenant", product_id, "flow_range")
        self.db.reject_field("demo-tenant", product_id, "head_range", "范围需工程师确认")
        self.db.finalize_product_review("demo-tenant", product_id)
        self.db.publish_product("demo-tenant", product_id)
        snapshot = json.loads(self.db.get_published_version("demo-tenant", product_id)["snapshot_json"])
        self.assertEqual([item["field_key"] for item in snapshot["fields"]], ["flow_range"])
        rejected = next(field for field in self.db.get_fields("demo-tenant", product_id) if field["field_key"] == "head_range")
        self.assertEqual(rejected["state"], "rejected")
        self.assertEqual(rejected["value"], "18-42")  # 驳回保留原值
        self.assertEqual(rejected["review_note"], "范围需工程师确认")
        # 被驳回字段不进入有据问答
        status, _answer, *_rest = answer_from_approved_material(self.db, "demo-tenant", "What is the head range?", product_id)
        self.assertEqual(status, "needs_human")

    def test_reextraction_conflict_does_not_overwrite_approved_value(self):
        product_id = self._product("EX-4")
        first = "流量范围: 20-80 m³/h\n".encode()
        source_id = self.db.add_source("demo-tenant", product_id, "EX-4-v1.txt", "TXT", "第 1 节", content=first, scope="exact_model")
        cleanup_upload(self, source_id)
        self.db.approve_field("demo-tenant", product_id, "flow_range")
        self.db.finalize_product_review("demo-tenant", product_id)
        later = "流量范围: 25-85 m³/h\n".encode()
        second = self.db.add_source("demo-tenant", product_id, "EX-4-v2.txt", "TXT", "第 1 节（新版）", content=later, scope="exact_model")
        duplicate = self.db.add_source("demo-tenant", product_id, "EX-4-v1-copy.txt", "TXT", "第 1 节", content=first, scope="exact_model")
        cleanup_upload(self, second, duplicate)
        field = self.db.get_fields("demo-tenant", product_id)[0]
        self.assertEqual(field["value"], "20-80")  # 已核准值未被覆盖
        self.assertEqual(field["state"], "approved")
        self.assertEqual(self.db.get_product("demo-tenant", product_id)["status"], "approved")  # 冲突不降级
        second_row = next(source for source in self.db.get_sources("demo-tenant", product_id) if source["id"] == second)
        conflict = json.loads(second_row["extraction_json"])[0]
        self.assertEqual(conflict["review_status"], "conflict")
        self.assertEqual(conflict["existing_value"], "20-80")
        self.assertEqual(conflict["value"], "25-85")
        duplicate_row = next(source for source in self.db.get_sources("demo-tenant", product_id) if source["id"] == duplicate)
        self.assertEqual(json.loads(duplicate_row["extraction_json"])[0]["review_status"], "duplicate")

    def test_scope_and_permission_rules_for_public_files(self):
        product_id = self._product("EX-5")
        source_id = self.db.add_source("demo-tenant", product_id, "EX-5-manual.pdf", "PDF", "第 1 页", content=b"demo", scope="unknown")
        cleanup_upload(self, source_id)
        with self.assertRaises(ValueError):
            self.db.allow_public_source("demo-tenant", product_id, source_id, "demo-admin")  # 归属未核对
        with self.assertRaises(ValueError):
            self.db.set_source_scope("demo-tenant", product_id, source_id, "not-a-scope")
        self.db.set_source_scope("demo-tenant", product_id, source_id, "series")
        self.db.allow_public_source("demo-tenant", product_id, source_id, "demo-admin")
        with self.assertRaises(ValueError):
            self.db.set_source_scope("demo-tenant", product_id, source_id, "unknown")  # 已公开不能改回未核对

    def test_other_product_source_is_never_served(self):
        product_id = self._product("EX-7")
        other_id = self._product("EX-8")
        content = b"approved bytes"
        source_id = self.db.add_source("demo-tenant", product_id, "EX-7.txt", "TXT", "第 1 节", content=content, scope="exact_model")
        cleanup_upload(self, source_id)
        self.db.save_field("demo-tenant", product_id, "flow_range", "10-30", "m³/h", source_id, "第 1 节")
        self.db.approve_field("demo-tenant", product_id, "flow_range")
        self.db.allow_public_source("demo-tenant", product_id, source_id, "demo-admin")
        self.db.finalize_product_review("demo-tenant", product_id)
        self.db.publish_product("demo-tenant", product_id)
        other_source = self.db.add_source("demo-tenant", other_id, "EX-8.txt", "TXT", "第 1 节", content=b"other", scope="exact_model")
        cleanup_upload(self, other_source)
        self.db.save_field("demo-tenant", other_id, "flow_range", "40-80", "m³/h", other_source, "第 1 节")
        self.db.approve_field("demo-tenant", other_id, "flow_range")
        self.db.finalize_product_review("demo-tenant", other_id)
        self.db.publish_product("demo-tenant", other_id)
        fake = FakeHandler(self.db)
        Handler.public_source(fake, "ex-7", source_id)
        self.assertEqual(fake.captured[1], 200)
        # 同一企业的另一个产品路径不能读取该文件
        Handler.public_source(fake, "ex-8", source_id)
        self.assertEqual(fake.captured[1], 404)
        self.assertIsNone(self.db.get_product_by_slug("other-tenant", "ex-7"))

    def test_tampered_file_is_blocked_by_hash_verification(self):
        product_id = self._product("EX-9")
        source_id = self.db.add_source("demo-tenant", product_id, "EX-9.txt", "TXT", "第 1 节", content=b"original bytes", scope="exact_model")
        cleanup_upload(self, source_id)
        self.db.save_field("demo-tenant", product_id, "flow_range", "10-30", "m³/h", source_id, "第 1 节")
        self.db.approve_field("demo-tenant", product_id, "flow_range")
        self.db.allow_public_source("demo-tenant", product_id, source_id, "demo-admin")
        self.db.finalize_product_review("demo-tenant", product_id)
        self.db.publish_product("demo-tenant", product_id)
        fake = FakeHandler(self.db)
        Handler.public_source(fake, "ex-9", source_id)
        self.assertEqual(fake.captured[1], 200)
        stored = next(UPLOAD_DIR.glob(f"{source_id}-*"))
        stored.write_bytes(b"tampered bytes")
        Handler.public_source(fake, "ex-9", source_id)
        self.assertEqual(fake.captured[1], 409)
        self.assertIn("完整性校验未通过", fake.captured[0])
        audit = self.db.list_entity_audit("demo-tenant", [source_id])
        self.assertTrue(any(item["action"] == "public_download_blocked_hash_mismatch" for item in audit))


class PublishedPageFilesTests(unittest.TestCase):
    """M2: gallery, CAD entry and version-frozen file mapping on the buyer page."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.temp_dir.name) / "app.db")
        # 测试夹具企业需显式创建（普通新库不再预置）。
        self.db.ensure_tenant("demo-tenant", "示例水泵企业（演示）")

    def tearDown(self):
        self.db.conn.close()
        self.temp_dir.cleanup()

    def _png(self, name: str) -> bytes:
        return b"\x89PNG\r\n\x1a\n" + name.encode()

    def _publish(self, product_id: str, source_specs):
        source_ids = {}
        for key, (filename, file_type, content, scope) in source_specs.items():
            source_ids[key] = self.db.add_source("demo-tenant", product_id, filename, file_type, "第 1 节", content=content, scope=scope)
        self.db.save_field("demo-tenant", product_id, "flow_range", "20-80", "m³/h", source_ids["image-1"], "第 1 节")
        self.db.approve_field("demo-tenant", product_id, "flow_range")
        for key, source_id in source_ids.items():
            if key != "private-image":
                self.db.allow_public_source("demo-tenant", product_id, source_id, "demo-admin")
        self.db.finalize_product_review("demo-tenant", product_id)
        self.db.publish_product("demo-tenant", product_id)
        return source_ids

    def test_gallery_cad_and_document_sections_only_show_current_version_public_files(self):
        product_id = self.db.create_product("demo-tenant", "EX-GAL", "图库测试泵", "清水")
        other_id = self.db.create_product("demo-tenant", "EX-OTH", "其他型号泵", "清水")
        specs = {
            "image-1": ("EX-GAL-front.png", "PNG", self._png("front"), "exact_model"),
            "image-2": ("EX-GAL-side.png", "PNG", self._png("side"), "exact_model"),
            "series-image": ("EX-series-catalogue.png", "PNG", self._png("catalogue"), "series"),
            "private-image": ("EX-GAL-unapproved.png", "PNG", self._png("private"), "exact_model"),
            "model-step": ("EX-GAL.step", "STEP", b"ISO-10303-21;\nENDSEC;", "exact_model"),
            "series-dxf": ("EX-series.dxf", "DXF", b"0\nSECTION\nEOF\n", "series"),
        }
        source_ids = self._publish(product_id, specs)
        cleanup_upload(self, *source_ids.values())
        # 另一个型号自己的 CAD，绝不能出现在本型号页面上
        other_source = self.db.add_source("demo-tenant", other_id, "EX-OTH.step", "STEP", "第 1 节", content=b"ISO-10303-21;", scope="exact_model")
        cleanup_upload(self, other_source)
        self.db.save_field("demo-tenant", other_id, "flow_range", "1-2", "m³/h", other_source, "第 1 节")
        self.db.approve_field("demo-tenant", other_id, "flow_range")
        self.db.finalize_product_review("demo-tenant", other_id)
        self.db.publish_product("demo-tenant", other_id)

        fake = FakeHandler(self.db)
        Handler.public_product(fake, "ex-gal", {"lang": ["zh"]})
        self.assertEqual(fake.captured[1], 200)
        page = fake.captured[0]
        # 图库：两张本型号图有缩略图交互，系列图与未公开图不进图库
        self.assertIn("product-gallery", page)
        self.assertIn(f"/sources/{source_ids['image-1']}?preview=1", page)
        self.assertIn(f"/sources/{source_ids['image-2']}?preview=1", page)
        self.assertIn("glr-1", page)
        self.assertNotIn(f"/sources/{source_ids['private-image']}?preview=1", page)
        self.assertNotIn("EX-GAL-unapproved.png", page)
        # 系列图进入资料区并明确标注非本型号专属
        self.assertIn("EX-series-catalogue.png", page)
        self.assertIn("系列资料（非本型号专属）", page)
        # 2D/3D 区：本型号文件可下载，且如实说明浏览器不渲染
        self.assertIn("本型号工程文件", page)
        self.assertIn(f"/sources/{source_ids['model-step']}", page)
        self.assertIn("EX-GAL.step", page)
        self.assertIn("该格式不提供浏览器内渲染", page)
        self.assertIn("系列级参考文件", page)
        self.assertIn("EX-series.dxf", page)
        self.assertNotIn("EX-OTH.step", page)
        self.assertNotIn(f"/sources/{other_source}", page)
        # 对比表只含同企业已发布型号；缺项如实显示
        self.assertIn("EX-OTH", page)

        empty_id = self.db.create_product("demo-tenant", "EX-EMPTY", "无图泵", "清水")
        meta_source = self.db.add_source("demo-tenant", empty_id, "EX-EMPTY-手册.pdf", "PDF", "第 1 页")
        self.db.save_field("demo-tenant", empty_id, "flow_range", "5-6", "m³/h", meta_source, "第 1 页")
        self.db.approve_field("demo-tenant", empty_id, "flow_range")
        self.db.finalize_product_review("demo-tenant", empty_id)
        self.db.publish_product("demo-tenant", empty_id)
        Handler.public_product(fake, "ex-empty", {"lang": ["zh"]})
        empty_page = fake.captured[0]
        self.assertIn("已核准本型号图片待补充", empty_page)
        self.assertIn("当前版本没有本型号已获准公开的 2D/3D 文件", empty_page)


class PublicScopeCorrectionTests(unittest.TestCase):
    """公开文件归属被修正后，旧版本不得继续按旧口径公开；重新公开须重新核准+重新发布。

    覆盖页面、下载与公开 JSON 三个出口；exact_model→series 与 series→exact_model 两个方向；
    旧版本（修正后、重新发布前）与新版本（重新发布后）两种状态。
    """

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "app.db"
        self.server = make_server(self.db_path, port=0)
        self.db = self.server.RequestHandlerClass.db
        self.db.ensure_tenant("demo-tenant", "示例水泵企业（演示）")
        self.db.register_buyer_site("demo-tenant", "site-a", "https://seller-a.example")
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.db.conn.close()
        self.thread.join()
        self.temp_dir.cleanup()

    def _http_get(self, path, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.server.server_port)
        conn.request("GET", path, headers=headers or {})
        response = conn.getresponse()
        body = response.read()
        result = response.status, body
        conn.close()
        return result

    def _publish_with_file(self, model, scope):
        product_id = self.db.create_product("demo-tenant", model, f"{model} 归属测试泵", "清水循环")
        contents = b"\x89PNG\r\n\x1a\n" + f"scope-correction-{model}".encode()
        source_id = self.db.add_source("demo-tenant", product_id, f"{model}-drawing.png", "PNG", "第 1 节", content=contents, scope=scope)
        cleanup_upload(self, source_id)
        self.db.save_field("demo-tenant", product_id, "flow_range", "20-80", "m³/h", source_id, "第 1 节")
        self.db.approve_field("demo-tenant", product_id, "flow_range")
        self.db.allow_public_source("demo-tenant", product_id, source_id, "demo-admin")
        self.db.finalize_product_review("demo-tenant", product_id)
        self.db.publish_product("demo-tenant", product_id)
        product = self.db.get_product("demo-tenant", product_id)
        self.assertEqual(self.db.public_downloadable_ids("demo-tenant", product_id), {source_id})
        return product_id, product["slug"], source_id, contents

    def _surfaces(self, slug, source_id):
        page_status, page = self._http_get(f"/s/site-a/products/{slug}?lang=zh")
        download_status, download_body = self._http_get(f"/s/site-a/products/{slug}/sources/{source_id}")
        json_status, json_body = self._http_get(f"/api/v1/public/products/{slug}", headers={"X-Site-Key": "site-a"})
        return {
            "page_status": page_status, "page": page.decode("utf-8"),
            "download_status": download_status, "download_body": download_body,
            "json_status": json_status, "json": json.loads(json_body) if json_body else None,
        }

    def _assert_old_version_safe(self, slug, source_id):
        """修正后、重新发布前：三个出口都不得再出现该文件。"""
        surfaces = self._surfaces(slug, source_id)
        self.assertEqual(surfaces["page_status"], 200)
        self.assertNotIn(f"/sources/{source_id}", surfaces["page"], "旧版本页面仍在引用已修正归属的文件")
        self.assertEqual(surfaces["download_status"], 404, "旧版本下载仍然可用")
        self.assertEqual(surfaces["json_status"], 200)
        self.assertEqual(surfaces["json"]["public_sources"], [], "公开 JSON 仍在提供已修正归属的文件")

    def test_exact_model_to_series_correction_hides_old_version_and_republish_relabels(self):
        product_id, slug, source_id, contents = self._publish_with_file("SCP-EX2S", "exact_model")
        surfaces = self._surfaces(slug, source_id)
        self.assertEqual(surfaces["page_status"], 200)
        self.assertIn("SCP-EX2S-drawing.png", surfaces["page"])
        self.assertIn(f"<img src='/s/site-a/products/{slug}/sources/{source_id}?preview=1'", surfaces["page"])
        self.assertNotIn("系列资料（非本型号专属）", surfaces["page"])
        self.assertEqual(surfaces["download_status"], 200)
        self.assertEqual(surfaces["download_body"], contents)
        self.assertEqual(surfaces["json"]["page_version"], 1)
        self.assertEqual([item["scope"] for item in surfaces["json"]["public_sources"]], ["exact_model"])

        result = self.db.set_source_scope("demo-tenant", product_id, source_id, "series")
        self.assertEqual(result, {"changed": True, "public_permission_revoked": True})
        audit = self.db.list_entity_audit("demo-tenant", [source_id])
        revoked = [item for item in audit if item["action"] == "public_permission_revoked_on_scope_change"]
        self.assertEqual(len(revoked), 1)
        self.assertIn("exact_model -> series", revoked[0]["details"])
        updated = [item for item in audit if item["action"] == "scope_updated"]
        self.assertIn("exact_model -> series", updated[-1]["details"])

        # 旧版本（v1；修正后、重新发布前）：页面/下载/JSON 全部立即失效
        self._assert_old_version_safe(slug, source_id)

        # 仅重新核准（未重新发布）仍不足以恢复公开
        self.db.allow_public_source("demo-tenant", product_id, source_id, "demo-admin")
        self._assert_old_version_safe(slug, source_id)

        # 重新发布 v2：新版本按修正后的归属正确标注（系列资料，不再进本型号图库）
        self.assertEqual(self.db.publish_product("demo-tenant", product_id), 2)
        surfaces = self._surfaces(slug, source_id)
        self.assertIn("SCP-EX2S-drawing.png", surfaces["page"])
        self.assertIn("<span class='file-chip'>系列资料（非本型号专属）</span>", surfaces["page"])
        self.assertNotIn(f"<img src='/s/site-a/products/{slug}/sources/{source_id}?preview=1'", surfaces["page"])
        self.assertEqual(surfaces["download_status"], 200)
        self.assertEqual(surfaces["download_body"], contents)
        self.assertEqual(surfaces["json"]["page_version"], 2)
        self.assertEqual([item["scope"] for item in surfaces["json"]["public_sources"]], ["series"])
        # 发布历史保留：v1 快照仍记录旧口径，v2 记录修正后的口径
        versions = [(row["version"], row["status"], json.loads(row["snapshot_json"])["public_sources"][0]["scope"])
                    for row in self.db.conn.execute("SELECT * FROM product_versions WHERE product_id=? ORDER BY version", (product_id,))]
        self.assertEqual(versions, [(1, "withdrawn", "exact_model"), (2, "published", "series")])

    def test_series_to_exact_model_correction_hides_old_version_and_republish_relabels(self):
        product_id, slug, source_id, contents = self._publish_with_file("SCP-S2EX", "series")
        surfaces = self._surfaces(slug, source_id)
        self.assertIn("<span class='file-chip'>系列资料（非本型号专属）</span>", surfaces["page"])
        self.assertNotIn(f"<img src='/s/site-a/products/{slug}/sources/{source_id}?preview=1'", surfaces["page"])
        self.assertEqual(surfaces["download_status"], 200)
        self.assertEqual([item["scope"] for item in surfaces["json"]["public_sources"]], ["series"])

        result = self.db.set_source_scope("demo-tenant", product_id, source_id, "exact_model")
        self.assertEqual(result, {"changed": True, "public_permission_revoked": True})
        self._assert_old_version_safe(slug, source_id)

        self.db.allow_public_source("demo-tenant", product_id, source_id, "demo-admin")
        self.assertEqual(self.db.publish_product("demo-tenant", product_id), 2)
        surfaces = self._surfaces(slug, source_id)
        self.assertIn(f"<img src='/s/site-a/products/{slug}/sources/{source_id}?preview=1'", surfaces["page"])
        self.assertNotIn("系列资料（非本型号专属）", surfaces["page"])
        self.assertEqual(surfaces["download_status"], 200)
        self.assertEqual(surfaces["download_body"], contents)
        self.assertEqual(surfaces["json"]["page_version"], 2)
        self.assertEqual([item["scope"] for item in surfaces["json"]["public_sources"]], ["exact_model"])

    def test_same_scope_keeps_permission_and_rejects_unreviewed(self):
        product_id, slug, source_id, _contents = self._publish_with_file("SCP-NOOP", "exact_model")
        result = self.db.set_source_scope("demo-tenant", product_id, source_id, "exact_model")
        self.assertEqual(result, {"changed": False, "public_permission_revoked": False})
        self.assertEqual(self.db.public_downloadable_ids("demo-tenant", product_id), {source_id})
        revision = self.db.conn.execute("SELECT scope_revision FROM source_documents WHERE id=?", (source_id,)).fetchone()["scope_revision"]
        self.assertEqual(revision, 0, "同一归属的重复保存不得推进归属修订")
        with self.assertRaises(ValueError):
            self.db.set_source_scope("demo-tenant", product_id, source_id, "unknown")  # 已公开不能改回未核对

    def test_exact_series_exact_cycle_requires_fresh_publication(self):
        """残留缺陷复现：归属文本改回原值也不能让旧快照恢复公开。

        exact->series->exact 两步修正后，仅重新核准许可（未重新发布），旧版本
        仍不得恢复任何出口；重新发布 v2 后按新快照恢复。"""
        product_id, slug, source_id, contents = self._publish_with_file("SCP-CYCLE1", "exact_model")
        self.assertEqual(
            self.db.set_source_scope("demo-tenant", product_id, source_id, "series"),
            {"changed": True, "public_permission_revoked": True},
        )
        self._assert_old_version_safe(slug, source_id)
        self.assertEqual(
            self.db.set_source_scope("demo-tenant", product_id, source_id, "exact_model"),
            {"changed": True, "public_permission_revoked": False},
        )
        # 重新核准公开许可（文本与快照一致），但归属修订已经从 r0 前进到 r2
        self.db.allow_public_source("demo-tenant", product_id, source_id, "demo-admin")
        row = self.db.conn.execute("SELECT scope, scope_revision FROM source_documents WHERE id=?", (source_id,)).fetchone()
        self.assertEqual((row["scope"], row["scope_revision"]), ("exact_model", 2))
        version = self.db.get_published_version("demo-tenant", product_id)
        self.assertEqual(json.loads(version["snapshot_json"])["public_sources"][0]["scope_revision"], 0)
        self.assertEqual(self.db.public_downloadable_ids("demo-tenant", product_id), set())
        self._assert_old_version_safe(slug, source_id)

        # 重新发布 v2：新快照记录当前修订，按修正后状态恢复三个出口
        self.assertEqual(self.db.publish_product("demo-tenant", product_id), 2)
        surfaces = self._surfaces(slug, source_id)
        self.assertIn("SCP-CYCLE1-drawing.png", surfaces["page"])
        self.assertIn(f"<img src='/s/site-a/products/{slug}/sources/{source_id}?preview=1'", surfaces["page"])
        self.assertEqual(surfaces["download_status"], 200)
        self.assertEqual(surfaces["download_body"], contents)
        self.assertEqual(surfaces["json"]["page_version"], 2)
        self.assertEqual([item["scope"] for item in surfaces["json"]["public_sources"]], ["exact_model"])
        new_snapshot = json.loads(self.db.get_published_version("demo-tenant", product_id)["snapshot_json"])
        self.assertEqual(new_snapshot["public_sources"][0]["scope_revision"], 2)
        # 发布历史保留：v1 记录旧修订 r0，v2 记录修正后 r2
        versions = [
            (row["version"], row["status"],
             json.loads(row["snapshot_json"])["public_sources"][0]["scope"],
             json.loads(row["snapshot_json"])["public_sources"][0].get("scope_revision", 0))
            for row in self.db.conn.execute("SELECT * FROM product_versions WHERE product_id=? ORDER BY version", (product_id,))
        ]
        self.assertEqual(versions, [(1, "withdrawn", "exact_model", 0), (2, "published", "exact_model", 2)])
        audit = self.db.list_entity_audit("demo-tenant", [source_id])
        updates = [item["details"] for item in audit if item["action"] == "scope_updated"]
        self.assertEqual(len(updates), 2)
        self.assertTrue(updates[0].endswith("(归属修订 r1)"))
        self.assertTrue(updates[1].endswith("(归属修订 r2)"))

    def test_series_exact_series_cycle_republish_before_repermission_keeps_file_hidden(self):
        """series->exact->series 循环，并覆盖“重新核准前先发布”的顺序。

        未重新核准时发布的新版本不得收录该文件；发布后补核准也仍不公开，
        必须再次发布才恢复（系列归属，不进本型号图库）。"""
        product_id, slug, source_id, contents = self._publish_with_file("SCP-CYCLE2", "series")
        self.assertEqual(
            self.db.set_source_scope("demo-tenant", product_id, source_id, "exact_model"),
            {"changed": True, "public_permission_revoked": True},
        )
        self._assert_old_version_safe(slug, source_id)
        self.assertEqual(
            self.db.set_source_scope("demo-tenant", product_id, source_id, "series"),
            {"changed": True, "public_permission_revoked": False},
        )
        # 未重新核准即发布 v2：文件不在快照内，旧版本不得借此恢复
        self.assertEqual(self.db.publish_product("demo-tenant", product_id), 2)
        surfaces = self._surfaces(slug, source_id)
        self.assertEqual(surfaces["download_status"], 404)
        self.assertEqual(surfaces["json"]["page_version"], 2)
        self.assertEqual(surfaces["json"]["public_sources"], [])
        self.assertNotIn(f"/sources/{source_id}", surfaces["page"])
        # 发布之后补核准：仍不公开（快照不含该文件），需再次发布
        self.db.allow_public_source("demo-tenant", product_id, source_id, "demo-admin")
        self.assertEqual(self.db.public_downloadable_ids("demo-tenant", product_id), set())
        self._assert_old_version_safe(slug, source_id)
        self.assertEqual(self.db.publish_product("demo-tenant", product_id), 3)
        surfaces = self._surfaces(slug, source_id)
        self.assertIn("SCP-CYCLE2-drawing.png", surfaces["page"])
        self.assertIn("<span class='file-chip'>系列资料（非本型号专属）</span>", surfaces["page"])
        self.assertEqual(surfaces["download_status"], 200)
        self.assertEqual(surfaces["download_body"], contents)
        self.assertEqual(surfaces["json"]["page_version"], 3)
        self.assertEqual([item["scope"] for item in surfaces["json"]["public_sources"]], ["series"])
        versions = [
            (row["version"], row["status"])
            for row in self.db.conn.execute("SELECT version, status FROM product_versions WHERE product_id=? ORDER BY version", (product_id,))
        ]
        self.assertEqual(versions, [(1, "withdrawn"), (2, "withdrawn"), (3, "published")])


if __name__ == "__main__":
    unittest.main()
