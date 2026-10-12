"""企业知识第二批 · 任务4/5 HTTP 闭环：模板管理 / 灵活字段 / 文件更新 / 重新核对来源。

覆盖用户 2026-10-12/13 的要求：界面与 HTTP 必须真实承载这些操作（不是假处理器）：
- 知识模板：仅编辑权限可见与可用（只读成员 403）；创建模板、保存新版本（不可变）；
- 灵活字段：采用模板需先看对照（未确认不改数据）、确认只改草稿；字段按类型保存、来源与核对写入；
- 文件更新：真实上传新原件 → 新草稿 + 差异对照页；核对后发布新版；旧线上在发布前不变；
- 主题重新核对来源：编辑页提供按钮（真实动作），POST 后出处重钉到当前版本；
- GET 不改变任何数据；全部写入先过后端权限检查。
只用临时数据库与测试私有上传目录；不触碰 data/ 下真实演示数据与任何密钥；不接真实模型。
"""
import html
import http.client
import json
import os
import re
import tempfile
import threading
import unittest
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlencode, urlsplit

from app import make_server

TENANT = "demo-tenant"
BOUNDARY = "----kb2httptask45boundary"


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


def page_text(raw):
    return raw.decode("utf-8", errors="replace")


class Phase2Task45HttpTests(unittest.TestCase):
    maxDiff = None

    @classmethod
    def setUpClass(cls):
        cls.temp_dir = tempfile.TemporaryDirectory()
        cls.upload_dir = Path(cls.temp_dir.name) / "uploads"
        os.environ["B2B_KB_STORAGE_ROOT"] = str(cls.upload_dir)
        cls.db_path = Path(cls.temp_dir.name) / "kb2-http2.db"
        cls.server = make_server(cls.db_path, port=0)
        cls.db = cls.server.RequestHandlerClass.db
        cls.db.knowledge_storage_root = cls.upload_dir
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.db.seed_demo_users()
        cls.admin = cls.db.authenticate_user("demo-admin@example.invalid", "demo-admin")
        cls.admin_cookie = login(cls.server, "demo-admin@example.invalid", "demo-admin")
        cls.member_cookie = login(cls.server, "demo-sales@example.invalid", "demo-sales")

    @classmethod
    def tearDownClass(cls):
        cls.server.server_close()
        cls.db.conn.close()
        os.environ.pop("B2B_KB_STORAGE_ROOT", None)
        cls.temp_dir.cleanup()

    # ── 帮助 ──────────────────────────────────────────────────────────────

    def add_file(self, filename, content, *, cookie=None):
        status, headers, _ = request(
            self.server, "POST", "/admin/knowledge/add",
            files=[("source_file", filename, content)], cookie=cookie or self.admin_cookie)
        self.assertIn(status, (302, 303))
        batch_id = urlsplit(headers["Location"]).path.rsplit("/", 1)[1]
        # 排队入口：由入库线程入口显式处理（与正式入口一致的数据路径；不依赖后台线程时序）。
        self.db.kb2_process_pending_sources(TENANT)
        return batch_id

    def source_of(self, batch_id, index=0):
        return self.db.kb2_get_batch(TENANT, batch_id)["sources"][index]

    def check_paragraphs(self, batch_id, source):
        fields = {"source_id": source["id"]}
        for paragraph in source["paragraphs"]:
            fields[f"p_{paragraph['id']}_text"] = paragraph["text"]
            fields[f"p_{paragraph['id']}_checked"] = "1"
        status, _, _ = request(self.server, "POST", f"/admin/knowledge/batch/{batch_id}/paragraphs",
                               fields=fields, cookie=self.admin_cookie)
        self.assertIn(status, (302, 303))

    def publish_batch(self, batch_id, *, usage="external", ai="1"):
        status, headers, _ = request(
            self.server, "POST", f"/admin/knowledge/batch/{batch_id}/settings",
            fields={"usage": usage, "ai_authorized": ai, "action": "publish", "confirm_ai": "1"},
            cookie=self.admin_cookie)
        self.assertIn(status, (302, 303))
        return headers["Location"]

    def make_published_item(self, text="保修24个月。\n安装前断电。"):
        batch_id = self.add_file("warranty.txt", text.encode())
        source = self.source_of(batch_id)
        self.check_paragraphs(batch_id, source)
        self.publish_batch(batch_id)
        return source["item_id"], batch_id, source

    # ── 模板管理（仅编辑权限；版本不可变）────────────────────────────────

    def test_template_pages_require_editor_and_save_new_versions(self):
        # 只读成员：GET 403，POST 403。
        status, _, raw = request(self.server, "GET", "/admin/knowledge/templates", cookie=self.member_cookie)
        self.assertEqual(status, 403)
        status, _, _ = request(self.server, "POST", "/admin/knowledge/templates/new",
                               fields={"name": "成员越权模板"}, cookie=self.member_cookie)
        self.assertEqual(status, 403)
        # 管理员：列表页与新建。
        status, _, raw = request(self.server, "GET", "/admin/knowledge/templates", cookie=self.admin_cookie)
        self.assertEqual(status, 200)
        fields = {
            "name": "服务方案A",
            "f_0_key": "", "f_0_label": "响应时间", "f_0_type": "number", "f_0_unit": "小时",
            "f_0_hint": "", "f_0_options": "",
            "f_1_key": "", "f_1_label": "服务区域", "f_1_type": "select", "f_1_options": "华东|华南",
        }
        status, headers, _ = request(self.server, "POST", "/admin/knowledge/templates/new",
                                     fields=fields, cookie=self.admin_cookie)
        self.assertIn(status, (302, 303))
        location = headers["Location"]
        self.assertTrue(location.startswith("/admin/knowledge/templates/"), location)
        template_id = urlsplit(location).path.rsplit("/", 1)[1]
        template = self.db.kb2_get_template(TENANT, template_id)
        self.assertEqual([f["label"] for f in template["versions"][0]["fields"]], ["响应时间", "服务区域"])
        # 空选项的重名/坏选项被拒绝（回到列表页报错，不落库）。
        status, headers, raw = request(
            self.server, "POST", "/admin/knowledge/templates/new",
            fields={"name": "坏模板", "f_0_label": "区域", "f_0_type": "select", "f_0_options": "只有一个"},
            cookie=self.admin_cookie)
        self.assertIn(status, (302, 303))
        self.assertIn("error", headers["Location"])
        self.assertNotIn("坏模板", [t["name"] for t in self.db.kb2_list_templates(TENANT)])
        # 保存新版本：字段定义追加，旧版本不变。
        keys = [f["key"] for f in template["versions"][0]["fields"]]
        fields = {"name": "服务方案A",
                  "f_0_key": keys[0], "f_0_label": "响应时间", "f_0_type": "number", "f_0_unit": "小时",
                  "f_0_hint": "", "f_0_options": "",
                  "f_1_key": keys[1], "f_1_label": "服务区域", "f_1_type": "select", "f_1_options": "华东|华南|华北"}
        status, headers, _ = request(self.server, "POST", f"/admin/knowledge/templates/{template_id}/save",
                                     fields=fields, cookie=self.admin_cookie)
        self.assertIn(status, (302, 303))
        refreshed = self.db.kb2_get_template(TENANT, template_id)
        self.assertEqual([v["version"] for v in refreshed["versions"]], [2, 1])
        self.assertEqual(refreshed["versions"][0]["fields"][1]["options"], ["华东", "华南", "华北"])
        self.assertEqual(refreshed["versions"][1]["fields"][1]["options"], ["华东", "华南"], "旧版本不可变")

    # ── 灵活字段：采用对照 / 填写 ─────────────────────────────────────────

    def test_fields_adopt_preview_then_confirm_and_field_save(self):
        item_id, _, _ = self.make_published_item()
        # 建模板（走 HTTP）。
        status, headers, _ = request(
            self.server, "POST", "/admin/knowledge/templates/new",
            fields={"name": "服务方案B",
                    "f_0_key": "resp", "f_0_label": "响应时间", "f_0_type": "number", "f_0_unit": "小时",
                    "f_1_key": "custom", "f_1_label": "支持定制", "f_1_type": "bool"},
            cookie=self.admin_cookie)
        template_id = urlsplit(headers["Location"]).path.rsplit("/", 1)[1]
        template_version_id = self.db.kb2_list_templates(TENANT)[0]["latest_version_id"]
        # 只读成员访问字段页 403。
        status, _, _ = request(self.server, "GET", f"/admin/knowledge/fields/{item_id}", cookie=self.member_cookie)
        self.assertEqual(status, 403)
        # 预览：不改数据；页面显示对照。
        status, headers, _ = request(
            self.server, "POST", f"/admin/knowledge/fields/{item_id}/adopt",
            fields={"template_version_id": template_version_id, "stage": "preview"}, cookie=self.admin_cookie)
        self.assertIn(status, (302, 303))
        self.assertIn(f"preview={template_version_id}", headers["Location"])
        self.assertIsNone(self.db._kb2_item_binding(TENANT, item_id), "预览不得写入采用记录")
        status, _, raw = request(self.server, "GET",
                                 f"/admin/knowledge/fields/{item_id}?preview={template_version_id}",
                                 cookie=self.admin_cookie)
        self.assertEqual(status, 200)
        self.assertIn("采用模板对照", page_text(raw))
        # 确认：采用只改草稿；字段出现。
        status, headers, _ = request(
            self.server, "POST", f"/admin/knowledge/fields/{item_id}/adopt",
            fields={"template_version_id": template_version_id, "stage": "confirm"}, cookie=self.admin_cookie)
        self.assertIn(status, (302, 303))
        self.assertIsNotNone(self.db._kb2_item_binding(TENANT, item_id))
        view = self.db.kb2_list_item_fields(TENANT, item_id)
        self.assertEqual([f["field_key"] for f in view["fields"]], ["resp", "custom"])
        # 保存字段值（0 是有效值）。
        status, headers, _ = request(
            self.server, "POST", f"/admin/knowledge/fields/{item_id}/field",
            fields={"field_key": "resp", "value": "0", "unit": "小时", "source_kind": "manual",
                    "checked": "1", "allow_customer": "1"}, cookie=self.admin_cookie)
        self.assertIn(status, (302, 303))
        field = next(f for f in self.db.kb2_list_item_fields(TENANT, item_id)["fields"] if f["field_key"] == "resp")
        self.assertEqual(field["value"], "0")
        self.assertTrue(field["value_present"])
        self.assertTrue(field["checked"])
        self.assertTrue(field["allow_customer"])
        self.assertEqual(field["source"]["kind"], "manual")
        # 值变化 → 核对撤销（服务端强制）。
        status, headers, _ = request(
            self.server, "POST", f"/admin/knowledge/fields/{item_id}/field",
            fields={"field_key": "resp", "value": "1", "unit": "小时", "source_kind": "manual"},
            cookie=self.admin_cookie)
        field = next(f for f in self.db.kb2_list_item_fields(TENANT, item_id)["fields"] if f["field_key"] == "resp")
        self.assertFalse(field["checked"], "改值后核对失效")

    # ── 文件更新（真实上传 → 对照 → 核对 → 发布）──────────────────────────

    def test_replace_file_flow_over_http(self):
        item_id, _, _ = self.make_published_item()
        published_before = self.db.get_published_knowledge_version(TENANT, item_id)
        status, headers, _ = request(
            self.server, "POST", f"/admin/knowledge/item/{item_id}/replace-file",
            files=[("replace_file", "warranty_v2.txt", "保修36个月。\n安装前断电。\n请保留合格证。".encode())],
            cookie=self.admin_cookie)
        self.assertIn(status, (302, 303))
        location = headers["Location"]
        self.assertIn("/batch/", location)
        batch_id = urlsplit(location).path.rsplit("/", 1)[1]
        # 旧线上版本未变；批次页显示差异对照。
        self.assertEqual(self.db.get_published_knowledge_version(TENANT, item_id)["id"], published_before["id"])
        status, _, raw = request(self.server, "GET", f"/admin/knowledge/batch/{batch_id}", cookie=self.admin_cookie)
        self.assertEqual(status, 200)
        page = page_text(raw)
        self.assertIn("文件更新对照", page)
        self.assertIn("保修24个月。", page)
        self.assertIn("保修36个月。", page)
        # 未核对发布被拦；核对后发布新版。
        source = self.source_of(batch_id)
        request(self.server, "POST", f"/admin/knowledge/batch/{batch_id}/settings",
                fields={"usage": "external", "ai_authorized": "1", "action": "publish", "confirm_ai": "1"},
                cookie=self.admin_cookie)
        self.assertEqual(self.db.get_published_knowledge_version(TENANT, item_id)["id"], published_before["id"])
        self.check_paragraphs(batch_id, source)
        self.publish_batch(batch_id)
        after = self.db.get_published_knowledge_version(TENANT, item_id)
        self.assertNotEqual(after["id"], published_before["id"])
        self.assertIn("保修36个月。", json.loads(after["content_json"])["body"])
        # 资料页：资料 ID 不变、历史可看。
        status, _, raw = request(self.server, "GET", f"/admin/knowledge/material/{source['id']}",
                                 cookie=self.admin_cookie)
        self.assertEqual(status, 200)
        self.assertIn("保修36个月。", page_text(raw))
        versions = self.db.list_knowledge_versions(TENANT, item_id)
        self.assertEqual(sorted(v["version"] for v in versions), [1, 2])
        # 重复更新被拒（已有草稿？不——已发布，无草稿；但已是最新内容。这里验证：再次更新会继续生成草稿）。
        status, headers, _ = request(
            self.server, "POST", f"/admin/knowledge/item/{item_id}/replace-file",
            files=[("replace_file", "warranty_v3.txt", "第三版。".encode())], cookie=self.admin_cookie)
        self.assertIn(status, (302, 303))
        self.assertIn("/batch/", headers["Location"])

    # ── 主题重新核对来源（按钮真实动作）──────────────────────────────────

    def test_topic_edit_renew_source_over_http(self):
        item_id, _, _ = self.make_published_item(text="保修24个月。")
        topic_id = self.db.kb2_create_topic(TENANT, self.admin["id"], "保修主题")
        self.db.kb2_set_relation(TENANT, self.admin["id"], topic_id, {"kind": "knowledge", "id": item_id})
        self.db.kb2_build_topic_draft(TENANT, self.admin["id"], topic_id)
        paragraph = self.db.kb2_get_topic(TENANT, topic_id)["content"]["paragraphs"][0]
        self.db.kb2_save_topic_draft(TENANT, self.admin["id"], topic_id,
                                     paragraphs=[{**paragraph, "checked": True}])
        self.db.kb2_publish_topic_draft(TENANT, self.admin["id"], topic_id)
        # 资料发新版（新增一行，原文字仍在）→ 段落来源换取版。
        # 走资料整理路径补一段人工正文并核对（B2-B09：正文必须与已核对出处一致才能发布）。
        kb2_source_id = self.db.conn.execute(
            "SELECT id FROM kb2_sources WHERE tenant_id=? AND item_id=?", (TENANT, item_id)).fetchone()[0]
        added = self.db.kb2_add_manual_paragraph(TENANT, self.admin["id"], kb2_source_id, "新增一行。")
        self.db.kb2_update_paragraph(TENANT, self.admin["id"], added, checked=True)
        self.db.publish_knowledge_draft(TENANT, item_id, self.admin["id"], confirm_ai=True)
        self.db.prepare_knowledge_version(
            TENANT, self.db.get_published_knowledge_version(TENANT, item_id)["id"])
        # 编辑页出现「重新核对来源」按钮（真实表单动作）。
        status, _, raw = request(self.server, "GET", f"/admin/knowledge/topic/{topic_id}/edit",
                                 cookie=self.admin_cookie)
        self.assertEqual(status, 200)
        page = page_text(raw)
        self.assertIn("重新核对来源", page)
        self.assertIn(f"value='{paragraph['id']}'", page)
        # 提交保存 + 重新核对：出处重钉到当前发布版本。
        refreshed = self.db.kb2_get_topic(TENANT, topic_id)
        fields = {
            "next": f"/admin/knowledge/topic/{topic_id}/edit",
            "p_0_id": refreshed["content"]["paragraphs"][0]["id"],
            "p_0_kind": "intro",
            "p_0_text": refreshed["content"]["paragraphs"][0]["text"],
            "p_0_origin": refreshed["content"]["paragraphs"][0]["origin"],
            "p_0_source": json.dumps(refreshed["content"]["paragraphs"][0]["source"], ensure_ascii=False),
            "p_0_checked": "1",
            "renew_paragraph": paragraph["id"],
        }
        status, headers, _ = request(self.server, "POST", f"/admin/knowledge/topic/{topic_id}/save",
                                     fields=fields, cookie=self.admin_cookie)
        self.assertIn(status, (303, 302))
        self.assertIn("notice", headers["Location"])
        draft = self.db.conn.execute(
            "SELECT content_json FROM kb2_topic_versions WHERE tenant_id=? AND topic_id=? AND status='draft'",
            (TENANT, topic_id)).fetchone()
        current_version = self.db.get_published_knowledge_version(TENANT, item_id)
        pinned = json.loads(draft["content_json"])["paragraphs"][0]["source"]["version_id"]
        self.assertEqual(pinned, current_version["id"], "重新核对后出处应钉住当前发布版本")

    # ── 全部资料：实际形式 + 页面身份（B2-B04 / B2-B06）────────────────────

    def test_materials_page_keeps_actual_form_links_and_page_identity(self):
        item_id, batch_id, source = self.make_published_item()
        status, _, raw = request(self.server, "GET", "/admin/knowledge?view=materials",
                                 cookie=self.admin_cookie)
        self.assertEqual(status, 200)
        page = page_text(raw)
        # B2-B04：形式维度显示真实原件形式（TXT），不是"文章"；行内直达统一资料详情。
        self.assertIn("TXT", page)
        self.assertIn(f"/admin/knowledge/material/{source['id']}", page)
        # B2-B06：GET 表单携带页面身份（view=materials），提交筛选不跳回全部主题。
        self.assertGreaterEqual(page.count("value='materials'"), 2, "搜索与筛选表单都要带页面身份")
        status, _, raw = request(
            self.server, "GET",
            "/admin/knowledge?view=materials&status=published&form=TXT&q=warranty",
            cookie=self.admin_cookie)
        page = page_text(raw)
        self.assertIn("清空搜索关键词", page)
        # 清空链接保留形式/状态筛选（只清关键词）。
        self.assertIn("form=TXT", page)
        self.assertIn("status=published", page)
        self.assertIn("view=materials", page)

    # ── 主题：发布设置 / 相关主题 / 历史恢复（范围补齐）────────────────────

    def test_topic_settings_related_links_and_restore_over_http(self):
        topic_id = self.db.kb2_create_topic(TENANT, self.admin["id"], "交互主题")
        other_id = self.db.kb2_create_topic(TENANT, self.admin["id"], "另一主题")
        # 编辑页包含用途/AI/到期设置与相关主题区。
        status, _, raw = request(self.server, "GET", f"/admin/knowledge/topic/{topic_id}/edit",
                                 cookie=self.admin_cookie)
        self.assertEqual(status, 200)
        page = page_text(raw)
        self.assertIn("用途、AI 与到期", page)
        self.assertIn("name='usage'", page)
        self.assertIn("name='ai_authorized'", page)
        # 保存设置（对外+授权+到期）。
        status, headers, _ = request(
            self.server, "POST", f"/admin/knowledge/topic/{topic_id}/save",
            fields={"next": f"/admin/knowledge/topic/{topic_id}/edit",
                    "p_0_id": "p1", "p_0_kind": "intro", "p_0_text": "对外正文。",
                    "p_0_origin": "manual", "p_0_source": json.dumps({"kind": "manual"}),
                    "p_0_checked": "1",
                    "usage": "external", "ai_authorized": "1",
                    "expiry_date": "2030-01-01", "expiry_time": "12:00"},
            cookie=self.admin_cookie)
        self.assertIn(status, (302, 303))
        topic = self.db.kb2_get_topic(TENANT, topic_id)
        self.assertEqual(topic["usage"], "external")
        self.assertTrue(topic["ai_authorized"])
        self.assertTrue(str(topic["expires_at"]).startswith("2030-01-01"))
        # 对外+AI 未经确认不能发布；确认后发布 v1。
        status, headers, _ = request(self.server, "POST", f"/admin/knowledge/topic/{topic_id}/publish",
                                     fields={"next": f"/admin/knowledge/topic/{topic_id}"},
                                     cookie=self.admin_cookie)
        self.assertIn("error", headers["Location"])
        status, headers, _ = request(self.server, "POST", f"/admin/knowledge/topic/{topic_id}/publish",
                                     fields={"next": f"/admin/knowledge/topic/{topic_id}", "confirm_ai": "1"},
                                     cookie=self.admin_cookie)
        self.assertIn(status, (302, 303))
        # 相关主题：详情页添加 → 双向可见 → 移除。
        status, headers, _ = request(self.server, "POST", f"/admin/knowledge/topic/{topic_id}/link-add",
                                     fields={"next": f"/admin/knowledge/topic/{topic_id}",
                                             "related_topic_id": other_id}, cookie=self.admin_cookie)
        self.assertIn(status, (302, 303))
        self.assertEqual([item["id"] for item in self.db.kb2_list_topic_links(TENANT, other_id)], [topic_id])
        status, _, raw = request(self.server, "GET", f"/admin/knowledge/topic/{topic_id}",
                                 cookie=self.admin_cookie)
        self.assertIn("相关主题", page_text(raw))
        status, headers, _ = request(self.server, "POST", f"/admin/knowledge/topic/{topic_id}/link-remove",
                                     fields={"next": f"/admin/knowledge/topic/{topic_id}",
                                             "related_topic_id": other_id}, cookie=self.admin_cookie)
        self.assertEqual(self.db.kb2_list_topic_links(TENANT, topic_id), [])
        # 历史恢复：发布 v2 后把 v1 恢复为草稿（线上保持 v2）。
        paragraph = self.db.kb2_get_topic(TENANT, topic_id)["content"]["paragraphs"][0]
        self.db.kb2_save_topic_draft(TENANT, self.admin["id"], topic_id,
                                     paragraphs=[{**paragraph, "text": "第二版正文。"}])
        self.db.kb2_save_topic_draft(
            TENANT, self.admin["id"], topic_id,
            paragraphs=[{**self.db.kb2_get_topic(TENANT, topic_id)["content"]["paragraphs"][0], "checked": True}])
        self.db.kb2_publish_topic_draft(TENANT, self.admin["id"], topic_id, confirm_ai=True)
        status, headers, _ = request(self.server, "POST", f"/admin/knowledge/topic/{topic_id}/version-restore",
                                     fields={"next": f"/admin/knowledge/topic/{topic_id}", "version": "1"},
                                     cookie=self.admin_cookie)
        self.assertIn(status, (302, 303))
        restored = self.db.kb2_get_topic(TENANT, topic_id)
        self.assertEqual(restored["published_version_number"], 2, "恢复不影响线上")
        self.assertEqual(restored["content"]["paragraphs"][0]["text"], "对外正文。")
        # 已有草稿时恢复被拒（错误提示，不产生第二个草稿）。
        status, headers, _ = request(self.server, "POST", f"/admin/knowledge/topic/{topic_id}/version-restore",
                                     fields={"next": f"/admin/knowledge/topic/{topic_id}", "version": "1"},
                                     cookie=self.admin_cookie)
        self.assertIn("error", headers["Location"])

    # ── GET 不变更数据 ────────────────────────────────────────────────────

    def test_get_pages_do_not_change_data(self):
        item_id, _, _ = self.make_published_item()
        status, headers, _ = request(
            self.server, "POST", "/admin/knowledge/templates/new",
            fields={"name": "只读检查", "f_0_label": "备注", "f_0_type": "text", "f_0_key": ""},
            cookie=self.admin_cookie)
        template_id = urlsplit(headers["Location"]).path.rsplit("/", 1)[1]
        template_version_id = self.db.kb2_list_templates(TENANT)[0]["latest_version_id"]
        self.db.kb2_adopt_template(TENANT, self.admin["id"], item_id, template_version_id, confirmed=True)
        before = {
            "templates": self.db.conn.execute("SELECT COUNT(*) FROM kb2_templates").fetchone()[0],
            "fields": self.db.conn.execute("SELECT COUNT(*) FROM kb2_item_fields").fetchone()[0],
            "versions": self.db.conn.execute("SELECT COUNT(*) FROM knowledge_versions").fetchone()[0],
            "template_versions": self.db.conn.execute("SELECT COUNT(*) FROM kb2_template_versions").fetchone()[0],
        }
        for path in (f"/admin/knowledge/templates", f"/admin/knowledge/templates/{template_id}",
                     f"/admin/knowledge/fields/{item_id}", f"/admin/knowledge/fields/{item_id}?preview={template_version_id}"):
            status, _, _ = request(self.server, "GET", path, cookie=self.admin_cookie)
            self.assertEqual(status, 200, path)
        after = {
            "templates": self.db.conn.execute("SELECT COUNT(*) FROM kb2_templates").fetchone()[0],
            "fields": self.db.conn.execute("SELECT COUNT(*) FROM kb2_item_fields").fetchone()[0],
            "versions": self.db.conn.execute("SELECT COUNT(*) FROM knowledge_versions").fetchone()[0],
            "template_versions": self.db.conn.execute("SELECT COUNT(*) FROM kb2_template_versions").fetchone()[0],
        }
        self.assertEqual(before, after, "GET 页面不得变更数据")

    # ── B2-B10 确认传递 / 实际入口返回 + 筛选 / 新界面到期口径 ──────────────

    def make_external_topic_draft(self, title="对外发布主题"):
        topic_id = self.db.kb2_create_topic(TENANT, self.admin["id"], title)
        self.db.kb2_save_topic_draft(
            TENANT, self.admin["id"], topic_id,
            paragraphs=[{"id": "p1", "kind": "intro", "text": "对外正文。", "origin": "manual",
                         "checked": True, "source": {"kind": "manual"}}],
            usage="external", ai_authorized=True)
        return topic_id

    def test_topic_publish_form_wires_confirm_field_for_dialog(self):
        """B2-B10：主题发布表单必须携带确认组件读取的属性（否则弹窗「继续」不传 confirm_ai）。"""
        topic_id = self.make_external_topic_draft()
        status, _, raw = request(self.server, "GET", f"/admin/knowledge/topic/{topic_id}",
                                 cookie=self.admin_cookie)
        self.assertEqual(status, 200)
        page = page_text(raw)
        self.assertIn(f"action='/admin/knowledge/topic/{topic_id}/publish'", page)
        # static/knowledge-phase2.js 从表单读 data-kb2-confirm-field/value 并注入目标输入框。
        self.assertIn("data-kb2-confirm-field='confirm_ai'", page)
        self.assertIn("data-kb2-confirm-value='1'", page)
        # 默认 hidden confirm_ai=0：取消 / 未确认提交保持未确认（后台继续正确拒绝）。
        self.assertIn("name='confirm_ai' value='0'", page)
        status, headers, _ = request(self.server, "POST", f"/admin/knowledge/topic/{topic_id}/publish",
                                     fields={"next": f"/admin/knowledge/topic/{topic_id}"},
                                     cookie=self.admin_cookie)
        self.assertIn("error", headers["Location"])
        self.assertEqual(self.db.kb2_get_topic(TENANT, topic_id)["status"], "draft")
        # 弹窗「继续」注入 confirm_ai=1 后送达后台：真实发布（与批量确认同口径）。
        status, headers, _ = request(self.server, "POST", f"/admin/knowledge/topic/{topic_id}/publish",
                                     fields={"next": f"/admin/knowledge/topic/{topic_id}", "confirm_ai": "1"},
                                     cookie=self.admin_cookie)
        self.assertIn(status, (302, 303))
        self.assertNotIn("error=", headers["Location"])
        self.assertEqual(self.db.kb2_get_topic(TENANT, topic_id)["status"], "published")

    def test_materials_rows_carry_validated_return_context(self):
        """实际入口返回：全集（含筛选）→ 资料详情「返回」回到原 URL 与筛选；目标只接受站内路径。"""
        item_id, batch_id, source = self.make_published_item()
        listed = "/admin/knowledge?view=materials&q=warranty&form=TXT&status=published"
        status, _, raw = request(self.server, "GET", listed, cookie=self.admin_cookie)
        self.assertEqual(status, 200)
        page = page_text(raw)
        match = re.search(
            r"href='(/admin/knowledge/material/%s\?from=materials&back=[^']+)'" % re.escape(source["id"]), page)
        self.assertIsNotNone(match, "全部资料行内链接必须携带实际入口返回目标")
        href = html.unescape(match.group(1))
        back = parse_qs(urlsplit(href).query)["back"][0]
        self.assertEqual(back, listed, "返回目标应保留全集页面身份与全部筛选")
        # 打开该资料详情：返回链接指向携带筛选的原 URL（服务端验证过的站内目标）。
        status, _, raw = request(self.server, "GET", href, cookie=self.admin_cookie)
        self.assertEqual(status, 200)
        page = page_text(raw)
        self.assertIn("href='%s'" % html.escape(back, quote=True), page)
        # 旧的无效返回目标不会被采用（校验本地路径，防跳外部）。
        status, _, raw = request(
            self.server, "GET",
            f"/admin/knowledge/material/{source['id']}?from=materials&back=https://evil.example/x",
            cookie=self.admin_cookie)
        page = page_text(raw)
        self.assertNotIn("evil.example", page)
        self.assertIn("href='/admin/knowledge'", page)
        # 主题树入口返回行为保持原样（默认回全部主题）。
        status, _, raw = request(
            self.server, "GET", f"/admin/knowledge/material/{source['id']}?from=topics",
            cookie=self.admin_cookie)
        page = page_text(raw)
        self.assertIn("href='/admin/knowledge'", page)

    def test_batch_settings_expiry_contract_is_beijing(self):
        """新界面的到期口径：只填日期=北京时间当日 23:59:59；日期+时间=北京时间；留空=长期有效；
        只填时间无日期=明确报错（不静默长期有效），且都不得改动已存值。"""
        batch_id = self.add_file("expiry.txt", "到期口径。".encode())
        base = f"/admin/knowledge/batch/{batch_id}/settings"

        def batch_expires():
            return self.db.conn.execute(
                "SELECT expires_at FROM kb2_batches WHERE id=?", (batch_id,)).fetchone()[0]

        # 只填日期 → 北京时间当日 23:59:59（UTC 15:59:59），不是旧的 UTC 日末。
        status, headers, _ = request(
            self.server, "POST", base,
            fields={"usage": "internal", "action": "save", "expiry_date": "2030-06-01"},
            cookie=self.admin_cookie)
        self.assertIn(status, (302, 303))
        self.assertEqual(batch_expires(), "2030-06-01T15:59:59+00:00")
        draft = self.db.get_knowledge_draft_version(TENANT, self.source_of(batch_id)["item_id"])
        self.assertEqual(draft["expires_at"], "2030-06-01T15:59:59+00:00")
        # 发布设置页回填为北京时间日期/时间（日期 2030-06-01、时间 23:59）。
        status, _, raw = request(self.server, "GET", f"/admin/knowledge/batch/{batch_id}?step=publish",
                                 cookie=self.admin_cookie)
        self.assertIn("name='expiry_date' value='2030-06-01'", page_text(raw))
        self.assertIn("name='expiry_time' value='23:59'", page_text(raw))
        # 日期+时间 → 按北京时间换算。
        status, headers, _ = request(
            self.server, "POST", base,
            fields={"usage": "internal", "action": "save", "expiry_date": "2030-06-01", "expiry_time": "12:30"},
            cookie=self.admin_cookie)
        self.assertEqual(batch_expires(), "2030-06-01T04:30:00+00:00")
        # 留空 → 长期有效（None）。
        status, headers, _ = request(
            self.server, "POST", base, fields={"usage": "internal", "action": "save"},
            cookie=self.admin_cookie)
        self.assertIsNone(batch_expires())
        # 只填时间无日期 → 明确报错；值保持不变（不能静默当长期有效）。
        status, headers, _ = request(
            self.server, "POST", base,
            fields={"usage": "internal", "action": "save", "expiry_time": "12:00"},
            cookie=self.admin_cookie)
        self.assertIn(status, (302, 303))
        self.assertIn("error=", headers["Location"])
        self.assertIn("日期", unquote(headers["Location"]))
        self.assertIsNone(batch_expires())

    def test_restore_initializes_fields_working_set_over_http(self):
        """B2-B11 界面路径：恢复后字段页显示恢复点值（初始化工作集）；
        页面改值 3 并再核对后发布 → 发布快照取 3（明确编辑进入新草稿快照）。"""
        item_id = self.db.create_knowledge_item(
            TENANT, "article", "恢复字段资料", self.admin["id"], content={"body": "恢复字段正文。"})
        status, headers, _ = request(
            self.server, "POST", "/admin/knowledge/templates/new",
            fields={"name": "恢复字段模板", "f_0_key": "price", "f_0_label": "Price", "f_0_type": "number"},
            cookie=self.admin_cookie)
        self.assertIn(status, (302, 303))
        template_version_id = next(
            template["latest_version_id"] for template in self.db.kb2_list_templates(TENANT)
            if template["name"] == "恢复字段模板")
        status, _, _ = request(
            self.server, "POST", f"/admin/knowledge/fields/{item_id}/adopt",
            fields={"template_version_id": template_version_id, "stage": "confirm"}, cookie=self.admin_cookie)
        self.assertIn(status, (302, 303))

        def save_field(value, *, checked):
            fields = {"field_key": "price", "value": value, "source_kind": "manual"}
            if checked:
                fields["checked"] = "1"
            status, _, _ = request(self.server, "POST", f"/admin/knowledge/fields/{item_id}/field",
                                   fields=fields, cookie=self.admin_cookie)
            self.assertIn(status, (302, 303))

        def publish_item():
            status, _, _ = request(self.server, "POST", f"/admin/knowledge/item/{item_id}/publish",
                                   fields={"confirmed": "1"}, cookie=self.admin_cookie)
            self.assertIn(status, (302, 303))

        save_field("1", checked=True)
        publish_item()
        save_field("2", checked=False)
        save_field("2", checked=True)
        publish_item()
        self.assertEqual(self.db.get_published_knowledge_version(TENANT, item_id)["version"], 2)

        status, headers, _ = request(self.server, "POST", f"/admin/knowledge/item/{item_id}/restore",
                                     fields={"version": "1"}, cookie=self.admin_cookie)
        self.assertIn(status, (302, 303))
        # 恢复即初始化工作集：字段页显示恢复点值 1、已核对。
        status, _, raw = request(self.server, "GET", f"/admin/knowledge/fields/{item_id}",
                                 cookie=self.admin_cookie)
        self.assertEqual(status, 200)
        page = page_text(raw)
        self.assertIn("name='value' inputmode='decimal' value='1'", page)
        self.assertIn("已核对", page)
        # 页面改值 3 → 再核对 → 发布：新草稿快照取 3。
        save_field("3", checked=False)
        save_field("3", checked=True)
        publish_item()
        published = self.db.get_published_knowledge_version(TENANT, item_id)
        self.assertEqual(published["version"], 3)
        fields = json.loads(published["content_json"])["kb2_fields"]
        self.assertEqual(fields[0]["value"], "3")
        self.assertTrue(fields[0]["checked"])


if __name__ == "__main__":
    unittest.main()
