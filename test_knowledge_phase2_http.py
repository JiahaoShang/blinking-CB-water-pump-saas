"""企业知识第二批 · 任务3 HTTP 闭环（临时数据库 + 真实服务端渲染）。

覆盖用户 2026-10-12 对任务3的要求与计划 checklist：
- 匿名访问重定向登录；跨企业读/下载拒绝；只读成员可读不可写（后端 403，不靠隐藏入口）；
- 真实上传（multipart，queued 入口 kb2_create_batch(process_now=False)）→ 批次检查页 → 逐段核对
  → 生成系统建议（现有主题命中 + 资料标题/标题段落候选）→ 确认主题与关联 → 发布设置 → 发布；
- 受认证的企业内原件下载（真实字节 + sha256；跨企业 404；不在任何公开地址）；
- 保存后重新读取（发布结果出现在全部资料 / 全部主题）；GET 不改变任何数据；
- 侧栏四入口（全部主题 / 全部资料 / 添加资料 / 问答测试）：成员只读缺少「添加资料」，管理员可见；
- 单卡默认文件 + 三模式切换不丢已输入（标记断言，交互细节由原型移植 JS 承担）；
- 主题浏览树同页可见、唯一文件入口；中央「关联资料」不重复打开文件按钮；旧入口保持兼容。

运行环境：系统 python3 与隔离环境 .venv-knowledge 均可（全部用 TXT 夹具）。只用临时数据库与
测试私有上传目录；不触碰 data/ 下真实演示数据与任何密钥；不接真实模型（问答页规则模式原样）。
"""
import http.client
import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit

from app import make_server

TENANT = "demo-tenant"
BOUNDARY = "----kb2httptestboundary"


def request(server, method, path, *, fields=None, files=None, cookie=None, raw_body=None, content_type=None):
    conn = http.client.HTTPConnection("127.0.0.1", server.server_port)
    headers = {}
    body = raw_body
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
    elif content_type:
        headers["Content-Type"] = content_type
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


class KnowledgePhase2HttpTests(unittest.TestCase):
    maxDiff = None

    @classmethod
    def setUpClass(cls):
        cls.temp_dir = tempfile.TemporaryDirectory()
        cls.upload_dir = Path(cls.temp_dir.name) / "uploads"
        os.environ["B2B_KB_STORAGE_ROOT"] = str(cls.upload_dir)
        cls.db_path = Path(cls.temp_dir.name) / "kb2-http.db"
        cls.server = make_server(cls.db_path, port=0)
        cls.db = cls.server.RequestHandlerClass.db
        cls.db.knowledge_storage_root = cls.upload_dir
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.db.seed_demo_users()
        cls.admin = cls.db.authenticate_user("demo-admin@example.invalid", "demo-admin")
        cls.member = cls.db.authenticate_user("demo-sales@example.invalid", "demo-sales")
        cls.admin_cookie = login(cls.server, "demo-admin@example.invalid", "demo-admin")
        cls.member_cookie = login(cls.server, "demo-sales@example.invalid", "demo-sales")
        cls.db.ensure_tenant("other-tenant", "其他企业（测试）")
        cls.other_admin_id = cls.db.create_user(
            "other-tenant", "other-admin@example.invalid", "对方管理员", "admin", "other-admin", "test-setup")
        cls.other_cookie = login(cls.server, "other-admin@example.invalid", "other-admin")

    @classmethod
    def tearDownClass(cls):
        cls.server.server_close()
        cls.db.conn.close()
        os.environ.pop("B2B_KB_STORAGE_ROOT", None)
        cls.temp_dir.cleanup()

    # ── 帮助 ──────────────────────────────────────────────────────────────

    def create_batch_with_file(self, filename, content: bytes, title=""):
        status, headers, _ = request(
            self.server, "POST", "/admin/knowledge/add",
            fields={"file_title": title} if title else {},
            files=[("source_file", filename, content)], cookie=self.admin_cookie)
        self.assertIn(status, (302, 303), "添加资料应重定向到检查整理结果")
        location = headers["Location"]
        self.assertTrue(location.startswith("/admin/knowledge/batch/"), location)
        batch_id = urlsplit(location).path.rsplit("/", 1)[1]
        return batch_id

    def batch_state(self, batch_id):
        return self.db.kb2_get_batch(TENANT, batch_id)

    def source_rows(self, batch_id):
        return self.db.conn.execute(
            "SELECT * FROM kb2_sources WHERE tenant_id=? AND batch_id=? ORDER BY created_at, id",
            (TENANT, batch_id)).fetchall()

    # ── 认证 / 权限 / 跨企业 ──────────────────────────────────────────────

    def test_anonymous_is_redirected_to_login(self):
        for path in ("/admin/knowledge", "/admin/knowledge?view=materials", "/admin/knowledge/add"):
            status, headers, _ = request(self.server, "GET", path)
            self.assertIn(status, (302, 303), path)
            self.assertIn("/login", headers.get("Location", ""))

    def test_cross_tenant_cannot_read_batch_topic_or_download(self):
        batch_id = self.create_batch_with_file("跨企业.txt", "跨企业内容。".encode())
        self.db.kb2_process_pending_sources(TENANT)
        status, _, _ = request(self.server, "GET", f"/admin/knowledge/batch/{batch_id}", cookie=self.other_cookie)
        self.assertEqual(status, 404)
        topic_id = self.db.kb2_create_topic(TENANT, self.admin["id"], "跨企业主题")
        status, _, _ = request(self.server, "GET", f"/admin/knowledge/topic/{topic_id}", cookie=self.other_cookie)
        self.assertEqual(status, 404)
        source = self.source_rows(batch_id)[0]
        status, _, _ = request(self.server, "GET", f"/admin/knowledge/source/{source['id']}/file",
                               cookie=self.other_cookie)
        self.assertEqual(status, 404, "跨企业下载必须拒绝")
        status, _, _ = request(self.server, "GET", f"/admin/knowledge/material/{source['id']}",
                               cookie=self.other_cookie)
        self.assertEqual(status, 404)

    def test_read_only_member_can_read_but_backend_denies_writes(self):
        batch_id = self.create_batch_with_file("只读.txt", "只读成员读取内容。".encode())
        # 可读：主题 / 资料 / 批次页面。
        for path in ("/admin/knowledge", "/admin/knowledge?view=materials", f"/admin/knowledge/batch/{batch_id}"):
            status, _, raw = request(self.server, "GET", path, cookie=self.member_cookie)
            self.assertEqual(status, 200, path)
            self.assertIn("只读", page_text(raw))
        # 侧栏入口差异：成员不可见「添加资料」，管理员可见。
        _, _, member_page = request(self.server, "GET", "/admin/knowledge", cookie=self.member_cookie)
        self.assertNotIn("href='/admin/knowledge/add'", page_text(member_page))
        _, _, admin_page = request(self.server, "GET", "/admin/knowledge", cookie=self.admin_cookie)
        self.assertIn("href='/admin/knowledge/add'", page_text(admin_page))
        # 后端拒绝写入（不靠隐藏入口）。
        status, _, raw = request(self.server, "POST", "/admin/knowledge/add",
                                 fields={"manual_text": "越权写入"}, cookie=self.member_cookie)
        self.assertEqual(status, 403)
        self.assertIn("无权限", page_text(raw))
        source = self.source_rows(batch_id)[0]
        status, _, _ = request(self.server, "POST", f"/admin/knowledge/batch/{batch_id}/paragraphs",
                               fields={"source_id": source["id"]}, cookie=self.member_cookie)
        self.assertEqual(status, 403)

    # ── 上传 → 三步闭环 → 发布结果可重新读取 ──────────────────────────────

    def test_upload_check_suggest_confirm_publish_roundtrip(self):
        batch_id = self.create_batch_with_file("售后条款.txt", "整机保修 24 个月。\n响应时间 48 小时。\n仅内部备注。".encode())
        # 排队入口：请求内不解析（来源仍是 pending），由入库线程入口处理。
        state = self.batch_state(batch_id)
        self.assertEqual(state["sources"][0]["status"], "pending")
        result = self.db.kb2_process_pending_sources(TENANT)
        self.assertGreaterEqual(result["processed"], 1)
        state = self.batch_state(batch_id)
        source = state["sources"][0]
        self.assertEqual(source["status"], "extracted")
        self.assertEqual(len(source["paragraphs"]), 3)
        # 检查整理结果页：包含真实段落与三步指示。
        _, _, raw = request(self.server, "GET", f"/admin/knowledge/batch/{batch_id}", cookie=self.admin_cookie)
        page = page_text(raw)
        self.assertIn("检查整理结果", page)
        self.assertIn("整机保修 24 个月。", page)
        # 逐段核对（前两段）＋排除第三段；保存后重新读取。
        first, second, third = source["paragraphs"]
        fields = {
            "source_id": source["id"],
            f"p_{first['id']}_text": first["text"], f"p_{first['id']}_checked": "1",
            f"p_{second['id']}_text": second["text"], f"p_{second['id']}_checked": "1",
            f"p_{third['id']}_text": third["text"], f"p_{third['id']}_excluded": "1",
        }
        status, headers, _ = request(self.server, "POST", f"/admin/knowledge/batch/{batch_id}/paragraphs",
                                     fields=fields, cookie=self.admin_cookie)
        self.assertIn(status, (302, 303))
        refreshed = self.batch_state(batch_id)["sources"][0]
        self.assertEqual(refreshed["counts"]["pending_check"], 0)
        self.assertEqual(refreshed["counts"]["excluded"], 1)
        # 生成系统建议：文字命中（无现成主题时只有候选）→ 添加成员建议 → 确认建立草稿主题。
        status, _, _ = request(self.server, "POST", f"/admin/knowledge/batch/{batch_id}/suggestions/build",
                               fields={}, cookie=self.admin_cookie)
        self.assertIn(status, (302, 303))
        status, _, _ = request(self.server, "POST", f"/admin/knowledge/batch/{batch_id}/suggestion-add",
                               fields={"name": "售后与保修"}, cookie=self.admin_cookie)
        self.assertIn(status, (302, 303))
        status, _, _ = request(self.server, "POST", f"/admin/knowledge/batch/{batch_id}/confirm-topics",
                               fields={}, cookie=self.admin_cookie)
        self.assertIn(status, (302, 303))
        topics = {topic["title"]: topic for topic in self.db.kb2_list_topics(TENANT)}
        self.assertIn("售后与保修", topics)
        topic_id = topics["售后与保修"]["id"]
        # 资料已关联主题、仍为草稿（确认不自动发布）。
        materials = {m["title"]: m for m in self.db.kb2_list_materials(TENANT) if m["kind"] == "knowledge"}
        self.assertIn("售后与保修", [t["title"] for t in materials["售后条款"]["topics"]])
        # 发布设置：发布后结果在全部资料 / 全部主题可重新读取。
        status, headers, _ = request(self.server, "POST", f"/admin/knowledge/batch/{batch_id}/settings",
                                     fields={"action": "publish", "usage": "internal", "confirm_ai": "0"},
                                     cookie=self.admin_cookie)
        self.assertIn(status, (302, 303))
        # 重定向携带的结果提示在页面上真实可见（不是只留在 URL 里）。
        _, _, result_page = request(self.server, "GET", headers["Location"], cookie=self.admin_cookie)
        self.assertIn("已发布：正文发布 1 件", page_text(result_page))
        state = self.batch_state(batch_id)
        self.assertEqual(state["sources"][0]["status"], "published")
        _, _, materials_page = request(self.server, "GET", "/admin/knowledge?view=materials", cookie=self.admin_cookie)
        self.assertIn("售后条款", page_text(materials_page))
        _, _, topics_page = request(self.server, "GET", "/admin/knowledge", cookie=self.admin_cookie)
        topics_html = page_text(topics_page)
        self.assertIn("售后与保修", topics_html)
        _, _, topic_page = request(self.server, "GET", f"/admin/knowledge/topic/{topic_id}", cookie=self.admin_cookie)
        topic_html = page_text(topic_page)
        self.assertIn("版本记录", topic_html)
        # 主题浏览树（同页即出现）：包含关联文件的唯一打开入口。
        self.assertIn(f"href='/admin/knowledge/material/", topics_html)
        self.assertIn("从左侧主题浏览列表", topic_html)
        # 由树打开的资料详情（受认证原件下载入口 + 同页浏览树 + 明确返回）。
        source_id = self.source_rows(batch_id)[0]["id"]
        _, _, material_page = request(
            self.server, "GET", f"/admin/knowledge/material/{source_id}?from=topics", cookie=self.admin_cookie)
        material_html = page_text(material_page)
        self.assertIn("下载原件（企业内认证）", material_html)
        self.assertIn("workspace-grid", material_html)
        self.assertIn(f"/admin/knowledge/source/{source_id}/file", material_html)
        # 中央「关联资料」不重复文件打开按钮（树以外不出现 material 链接）。
        relations_html = topic_html.split("id='kb2-relations'", 1)[1].split("</details>", 1)[0]
        self.assertNotIn("/admin/knowledge/material/", relations_html)

    def test_registered_only_download_and_publish_archive(self):
        batch_id = self.create_batch_with_file("示意图.png", b"\x89PNG\r\n\x1a\n" + b"0" * 64)
        self.db.kb2_process_pending_sources(TENANT)
        source = self.batch_state(batch_id)["sources"][0]
        self.assertEqual(source["status"], "registered")
        # 受认证下载：真实字节一致。
        status, headers, raw = request(self.server, "GET", f"/admin/knowledge/source/{source['id']}/file",
                                       cookie=self.admin_cookie)
        self.assertEqual(status, 200)
        self.assertEqual(raw, b"\x89PNG\r\n\x1a\n" + b"0" * 64)
        self.assertTrue(headers.get("Content-Disposition", "").startswith("inline"),
                        "图片原件按预览提供（仍需企业登录）")
        # 文档类原件按附件下载；字节与登记一致。
        status, headers, raw = request(
            self.server, "POST", "/admin/knowledge/add",
            files=[("source_file", "下载.txt", "下载字节校验。".encode())], cookie=self.admin_cookie)
        self.assertIn(status, (302, 303))
        txt_batch = urlsplit(headers["Location"]).path.rsplit("/", 1)[1]
        self.db.kb2_process_pending_sources(TENANT)
        txt_source = self.batch_state(txt_batch)["sources"][0]
        status, headers, raw = request(self.server, "GET", f"/admin/knowledge/source/{txt_source['id']}/file",
                                       cookie=self.admin_cookie)
        self.assertTrue(headers.get("Content-Disposition", "").startswith("attachment"))
        self.assertEqual(raw.decode("utf-8"), "下载字节校验。")
        # 归档发布后：全部资料显示「已发布（AI 客服不可用）」+ 仅登记原件原因。
        status, _, _ = request(self.server, "POST", f"/admin/knowledge/batch/{batch_id}/settings",
                               fields={"action": "publish", "usage": "internal"}, cookie=self.admin_cookie)
        self.assertIn(status, (302, 303))
        _, _, page = request(self.server, "GET", "/admin/knowledge?view=materials", cookie=self.admin_cookie)
        html = page_text(page)
        self.assertIn("已发布（AI 客服不可用）", html)
        self.assertIn("仅登记原件", html)

    def test_publish_blocked_until_checked_and_gate_links(self):
        batch_id = self.create_batch_with_file("未核对.txt", "等待核对的正文。".encode())
        self.db.kb2_process_pending_sources(TENANT)
        status, headers, _ = request(self.server, "POST", f"/admin/knowledge/batch/{batch_id}/settings",
                                     fields={"action": "publish", "usage": "internal"}, cookie=self.admin_cookie)
        self.assertIn(status, (302, 303))
        location = headers["Location"]
        self.assertIn("step=publish", location)
        self.assertIn("error=", location)
        # 发布前校验页可定位未通过项。
        _, _, raw = request(self.server, "GET", f"/admin/knowledge/batch/{batch_id}?step=publish",
                            cookie=self.admin_cookie)
        page = page_text(raw)
        self.assertIn("未核对", page)
        self.assertIn("去处理", page)
        # 未发布：来源与条目保持草稿。
        state = self.batch_state(batch_id)
        self.assertEqual(state["sources"][0]["status"], "extracted")
        self.assertEqual(state["sources"][0]["item_status"], "draft")

    def test_get_requests_do_not_change_data(self):
        batch_id = self.create_batch_with_file("只读访问.txt", "只读访问不应改变状态。".encode())
        before = self.db.conn.execute(
            "SELECT status, attempts, item_id FROM kb2_sources WHERE batch_id=?", (batch_id,)).fetchall()
        for path in ("/admin/knowledge", "/admin/knowledge?view=materials", f"/admin/knowledge/batch/{batch_id}",
                     f"/admin/knowledge/batch/{batch_id}?step=publish", "/admin/knowledge/add"):
            status, _, _ = request(self.server, "GET", path, cookie=self.admin_cookie)
            self.assertEqual(status, 200, path)
        after = self.db.conn.execute(
            "SELECT status, attempts, item_id FROM kb2_sources WHERE batch_id=?", (batch_id,)).fetchall()
        self.assertEqual([tuple(row) for row in before], [tuple(row) for row in after], "GET 不能变更数据")

    # ── 表单入口与兼容 ────────────────────────────────────────────────────

    def test_add_page_single_card_default_file(self):
        _, _, raw = request(self.server, "GET", "/admin/knowledge/add", cookie=self.admin_cookie)
        page = page_text(raw)
        self.assertIn("data-kb2-mode='file'", page)
        self.assertIn("data-kb2-mode-btn='file' aria-pressed='true'", page)
        file_panel = page.split("data-kb2-mode='file'", 1)[1].split("</div>", 3)[0]
        self.assertNotIn("hidden", file_panel.split(">", 1)[0], "文件卡片默认显示")
        self.assertIn("最近批次", page)

    def test_invalid_upload_returns_clear_error_without_batch(self):
        before = len(self.db.kb2_list_batches(TENANT))
        status, _, raw = request(self.server, "POST", "/admin/knowledge/add",
                                 files=[("source_file", "程序.exe", b"MZ\x00\x00")], cookie=self.admin_cookie)
        self.assertEqual(status, 200)  # 回到表单页并显示原因（不创建批次）
        self.assertIn("暂不支持该格式", page_text(raw))
        self.assertEqual(len(self.db.kb2_list_batches(TENANT)), before)

    def test_old_entrypoints_still_work(self):
        item_id = self.db.create_knowledge_item(
            TENANT, "article", "旧入口文章", self.admin["id"], content={"body": "旧入口正文。"})
        self.db.publish_knowledge_draft(TENANT, item_id, self.admin["id"])
        for path in (f"/admin/knowledge/item/{item_id}", "/admin/knowledge/qa",
                     "/admin/knowledge?view=product_service", "/admin/knowledge?view=article_faq",
                     "/admin/knowledge?view=files", "/admin/knowledge/new", "/admin/knowledge/import"):
            status, _, _ = request(self.server, "GET", path, cookie=self.admin_cookie)
            self.assertEqual(status, 200, path)
        # 旧文章出现在新的「全部资料」，且未自动分配主题。
        _, _, page = request(self.server, "GET", "/admin/knowledge?view=materials", cookie=self.admin_cookie)
        html = page_text(page)
        self.assertIn("旧入口文章", html)
        self.assertIn("未关联主题", html)

    def test_topic_tree_visible_on_overview_and_unique_file_entry(self):
        batch_id = self.create_batch_with_file("树入口.txt", "树入口正文。".encode())
        self.db.kb2_process_pending_sources(TENANT)
        source = self.batch_state(batch_id)["sources"][0]
        topic_id = self.db.kb2_create_topic(TENANT, self.admin["id"], "树入口主题")
        self.db.kb2_set_relation(TENANT, self.admin["id"], topic_id,
                                 {"kind": "knowledge", "id": source["item_id"]})
        _, _, raw = request(self.server, "GET", "/admin/knowledge", cookie=self.admin_cookie)
        page = page_text(raw)
        self.assertIn("workspace-grid", page)
        self.assertIn("nav-filesbtn-", page)
        self.assertIn(f"href='/admin/knowledge/material/{source['id']}?from=topic:{topic_id}'", page)
        self.assertIn("主题浏览导航", page)
        # 树文件入口是唯一打开位置：主题页中央不再出现 material 链接（已在 roundtrip 用例断言）。

    def test_worker_thread_wiring_on_make_server(self):
        server = make_server(Path(self.temp_dir.name) / "kb2-worker.db", port=0,
                             knowledge_worker=True)
        try:
            names = {thread.name for thread in threading.enumerate()}
            self.assertIn("enterprise-knowledge-phase2-ingest", names, "持久入库线程应随服务器启动")
        finally:
            server.server_close()

    # ── 其余写路由覆盖（主题生命周期 / 分组 / 建议子动作 / 字段与人工正文）────

    def test_topic_lifecycle_and_group_routes(self):
        # 新建主题 → 保存草稿（人工段落，默认待核对）→ 添加段落 → 建初稿 → 发布 → 停用/恢复。
        status, headers, _ = request(self.server, "POST", "/admin/knowledge/topic/new",
                                     fields={"title": "HTTP 主题", "group_id": ""}, cookie=self.admin_cookie)
        self.assertIn(status, (302, 303))
        topic_id = urlsplit(headers["Location"]).path.rsplit("/", 1)[1]
        status, _, _ = request(
            self.server, "POST", f"/admin/knowledge/topic/{topic_id}/save",
            fields={"p_0_text": "成员撰写的介绍。", "p_0_kind": "intro", "p_0_origin": "manual",
                    "p_0_checked": "1"}, cookie=self.admin_cookie)
        self.assertIn(status, (302, 303))
        topic = self.db.kb2_get_topic(TENANT, topic_id)
        self.assertEqual(topic["content"]["paragraphs"][0]["text"], "成员撰写的介绍。")
        status, _, _ = request(self.server, "POST", f"/admin/knowledge/topic/{topic_id}/paragraph-add",
                               fields={"text": "追加要点。", "kind": "point"}, cookie=self.admin_cookie)
        self.assertIn(status, (302, 303))
        status, _, _ = request(self.server, "POST", f"/admin/knowledge/topic/{topic_id}/build-draft",
                               fields={}, cookie=self.admin_cookie)
        self.assertIn(status, (302, 303))
        # 未核对段落仍在：发布被拦（重定向带 error）。
        status, headers, _ = request(self.server, "POST", f"/admin/knowledge/topic/{topic_id}/publish",
                                     fields={"confirm_ai": "0"}, cookie=self.admin_cookie)
        self.assertIn(status, (302, 303))
        self.assertIn("error=", headers["Location"])
        # 核对全部段落 → 发布成功。
        topic = self.db.kb2_get_topic(TENANT, topic_id)
        fields = {}
        for index, paragraph in enumerate(topic["content"]["paragraphs"]):
            fields[f"p_{index}_text"] = paragraph["text"]
            fields[f"p_{index}_id"] = paragraph["id"]
            fields[f"p_{index}_kind"] = paragraph["kind"]
            fields[f"p_{index}_origin"] = paragraph["origin"]
            fields[f"p_{index}_checked"] = "1"
            if paragraph.get("source"):
                fields[f"p_{index}_source"] = json.dumps(paragraph["source"], ensure_ascii=False)
        status, _, _ = request(self.server, "POST", f"/admin/knowledge/topic/{topic_id}/save",
                               fields=fields, cookie=self.admin_cookie)
        self.assertIn(status, (302, 303))
        status, headers, _ = request(self.server, "POST", f"/admin/knowledge/topic/{topic_id}/publish",
                                     fields={"confirm_ai": "0"}, cookie=self.admin_cookie)
        self.assertIn(status, (302, 303))
        self.assertEqual(self.db.kb2_get_topic(TENANT, topic_id)["status"], "published")
        _, _, published_page = request(self.server, "GET", headers["Location"], cookie=self.admin_cookie)
        self.assertIn("主题已发布（版本 v1）", page_text(published_page))
        status, _, _ = request(self.server, "POST", f"/admin/knowledge/topic/{topic_id}/disable",
                               fields={}, cookie=self.admin_cookie)
        self.assertIn(status, (302, 303))
        self.assertEqual(self.db.kb2_get_topic(TENANT, topic_id)["status"], "disabled")
        status, _, _ = request(self.server, "POST", f"/admin/knowledge/topic/{topic_id}/enable",
                               fields={}, cookie=self.admin_cookie)
        self.assertIn(status, (302, 303))
        self.assertEqual(self.db.kb2_get_topic(TENANT, topic_id)["status"], "published")
        # 分组：新建 → 主题移动 → 改名 → 删除（主题保留为未分组）。
        status, _, _ = request(self.server, "POST", "/admin/knowledge/group/new",
                               fields={"name": "HTTP 分组"}, cookie=self.admin_cookie)
        self.assertIn(status, (302, 303))
        group_id = self.db.kb2_list_groups(TENANT)[0]["id"]
        status, _, _ = request(self.server, "POST", f"/admin/knowledge/topic/{topic_id}/move",
                               fields={"group_id": group_id}, cookie=self.admin_cookie)
        self.assertIn(status, (302, 303))
        self.assertEqual(self.db.kb2_get_topic(TENANT, topic_id)["group_id"], group_id)
        status, _, _ = request(self.server, "POST", "/admin/knowledge/group/rename",
                               fields={"group_id": group_id, "name": "HTTP 分组（改名）"}, cookie=self.admin_cookie)
        self.assertIn(status, (302, 303))
        status, _, _ = request(self.server, "POST", "/admin/knowledge/group/delete",
                               fields={"group_id": group_id}, cookie=self.admin_cookie)
        self.assertIn(status, (302, 303))
        self.assertEqual(self.db.kb2_list_groups(TENANT), [])
        self.assertIsNone(self.db.kb2_get_topic(TENANT, topic_id)["group_id"])
        self.assertEqual(self.db.kb2_get_topic(TENANT, topic_id)["status"], "published", "删分组不影响主题")

    def test_batch_suggestion_field_and_manual_routes(self):
        batch_id = self.create_batch_with_file("路由覆盖.txt", "第一段内容。\n第二段内容。".encode())
        self.db.kb2_process_pending_sources(TENANT)
        source = self.batch_state(batch_id)["sources"][0]
        first = source["paragraphs"][0]
        # 人工正文 + 字段来源（钉住段落）。
        status, _, _ = request(self.server, "POST", f"/admin/knowledge/batch/{batch_id}/paragraph-add",
                               fields={"source_id": source["id"], "text": "补充人工正文。"}, cookie=self.admin_cookie)
        self.assertIn(status, (302, 303))
        status, _, _ = request(self.server, "POST", f"/admin/knowledge/batch/{batch_id}/field",
                               fields={"source_id": source["id"], "label": "响应时间", "value": "24",
                                       "unit": "小时", "paragraph_id": first["id"]}, cookie=self.admin_cookie)
        self.assertIn(status, (302, 303))
        refreshed = self.batch_state(batch_id)["sources"][0]
        self.assertEqual(refreshed["counts"]["manual"], 1)
        self.assertEqual(refreshed["fields"][0]["paragraph_id"], first["id"])
        # 建议子动作：生成 → 改名 → 合并 → 拒绝 → 资料范围。
        status, _, _ = request(self.server, "POST", f"/admin/knowledge/batch/{batch_id}/suggestions/build",
                               fields={}, cookie=self.admin_cookie)
        self.assertIn(status, (302, 303))
        status, _, _ = request(self.server, "POST", f"/admin/knowledge/batch/{batch_id}/suggestion-add",
                               fields={"name": "待改名"}, cookie=self.admin_cookie)
        self.assertIn(status, (302, 303))
        suggestions = {s["name"]: s for s in self.batch_state(batch_id)["suggestions"]}
        rename_id = suggestions["待改名"]["id"]
        status, _, _ = request(self.server, "POST",
                               f"/admin/knowledge/batch/{batch_id}/suggestion/{rename_id}/rename",
                               fields={"name": "改名后的建议"}, cookie=self.admin_cookie)
        self.assertIn(status, (302, 303))
        self.assertIn("改名后的建议", {s["name"] for s in self.batch_state(batch_id)["suggestions"]})
        status, _, _ = request(self.server, "POST",
                               f"/admin/knowledge/batch/{batch_id}/suggestion/{rename_id}/sources",
                               fields={"source_ids": source["id"]}, cookie=self.admin_cookie)
        self.assertIn(status, (302, 303))
        target_topic = self.db.kb2_create_topic(TENANT, self.admin["id"], "合并目标主题")
        status, _, _ = request(self.server, "POST",
                               f"/admin/knowledge/batch/{batch_id}/suggestion/{rename_id}/merge",
                               fields={"topic_id": target_topic}, cookie=self.admin_cookie)
        self.assertIn(status, (302, 303))
        merged = {s["name"]: s for s in self.batch_state(batch_id)["suggestions"]}["改名后的建议"]
        self.assertEqual(merged["existing_topic_id"], target_topic)
        status, _, _ = request(self.server, "POST",
                               f"/admin/knowledge/batch/{batch_id}/suggestion/{rename_id}/reject",
                               fields={}, cookie=self.admin_cookie)
        self.assertIn(status, (302, 303))
        rejected = {s["name"]: s for s in self.batch_state(batch_id)["suggestions"]}["改名后的建议"]
        self.assertEqual(rejected["status"], "rejected")
        # 重试入口：已成功来源重试被拒（重定向带 error，状态不变）。
        status, headers, _ = request(self.server, "POST",
                                     f"/admin/knowledge/batch/{batch_id}/source/{source['id']}/retry",
                                     fields={}, cookie=self.admin_cookie)
        self.assertIn(status, (302, 303))
        self.assertIn("error=", headers["Location"])
        self.assertEqual(self.batch_state(batch_id)["sources"][0]["status"], "extracted")


if __name__ == "__main__":
    unittest.main()
