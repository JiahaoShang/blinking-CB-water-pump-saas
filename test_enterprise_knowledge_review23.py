"""企业知识第一批 · 任务2/3 复核修正验收测试（review-task23 七项清单）。

覆盖（先于实现编写，逐项对应清单编号）：
1 未保存保护改为真实三按钮可访问对话框（无 window.prompt；Escape 与焦点恢复；导入页
  「保存草稿并离开」提交表单携带文件，不得落到 location.href 丢文件；缺文件/缺标题明确阻止）。
2 搜索「×」是真实按钮：输入时即时显隐、点击只清关键词保留筛选并刷新真实结果、焦点回到输入框。
3 侧栏「企业知识」主按钮进入并展开、子视图默认展开、独立箭头只切换、活动态正确、默认收起与 inert。
4 详情默认显示当前生效（已发布）版本；存在草稿时给出明确提示与继续编辑入口；版本历史实际展示内容。
5 /new 与 /edit 的 action=publish 必须带显式表单确认字段（对外+授权）；缺少确认不得发布。
6 列表整行可键盘访问链接；每行一枚生命周期标签（准备中不再出现第二枚 pill，作为原因文字）。
7 无伪造角色切换、无伪处理计时；保留配色/添加菜单/搜索×/折叠动画/图标/键盘/移动端/未保存保护。
只用临时数据库与临时上传目录；不触碰 data/ 与任何密钥。
"""
import json
import os
import tempfile
import threading
import unittest
from pathlib import Path

from app import Database

TENANT = "demo-tenant"


class Review23Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["B2B_KB_STORAGE_ROOT"] = str(Path(self.tmp.name) / "uploads")
        self.db = Database(Path(self.tmp.name) / "knowledge.db")
        self.db.seed_demo_users()
        self.admin = self.db.authenticate_user("demo-admin@example.invalid", "demo-admin")
        self.member = self.db.authenticate_user("demo-sales@example.invalid", "demo-sales")
        self.db.knowledge_storage_root = Path(self.tmp.name) / "uploads"
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
        self.db.conn.close()
        self.tmp.cleanup()
        os.environ.pop("B2B_KB_STORAGE_ROOT", None)

    def get(self, path, user=None):
        return self.form_request(self.server, "GET", path, None, self.cookies[(user or self.admin)["id"]])

    def post(self, path, fields, user=None):
        return self.form_request(self.server, "POST", path, fields, self.cookies[(user or self.admin)["id"]])

    def post_multipart(self, path, fields, filename, content, user=None):
        import http.client
        boundary = "----kb-review-boundary"
        parts = []
        for name, value in (fields or {}).items():
            parts.append(
                ("--" + boundary + "\r\n").encode()
                + ('Content-Disposition: form-data; name="%s"\r\n\r\n' % name).encode()
                + str(value).encode("utf-8") + b"\r\n"
            )
        parts.append(
            ("--" + boundary + "\r\n").encode()
            + ('Content-Disposition: form-data; name="file"; filename="%s"\r\n' % filename).encode()
            + b"Content-Type: application/octet-stream\r\n\r\n" + content + b"\r\n"
        )
        parts.append(("--" + boundary + "--\r\n").encode())
        conn = http.client.HTTPConnection("127.0.0.1", self.server.server_port)
        conn.request("POST", path, body=b"".join(parts), headers={
            "Content-Type": "multipart/form-data; boundary=" + boundary,
            "Cookie": self.cookies[(user or self.admin)["id"]],
        })
        response = conn.getresponse()
        raw = response.read().decode("utf-8", errors="replace")
        conn.close()
        return response.status, raw

    def create_article(self, title="保修政策", body="保修 24 个月。", **kw):
        return self.db.create_knowledge_item(
            TENANT, "article", title, self.admin["id"], content={"body": body}, **kw)

    def publish_prepared(self, item_id, **kw):
        self.db.publish_knowledge_draft(TENANT, item_id, self.admin["id"], **kw)
        version = self.db.get_published_knowledge_version(TENANT, item_id)
        self.db.prepare_knowledge_version(TENANT, version["id"])
        return version


class LeaveGuardTests(Review23Base):
    """清单 1：真实三按钮对话框 + 导入不丢文件。"""

    def test_editor_uses_three_button_dialog_not_prompt(self):
        item_id = self.create_article()
        code, _, page = self.get(f"/admin/knowledge/item/{item_id}/edit")
        self.assertEqual(code, 200)
        self.assertNotIn("window.prompt", page)
        self.assertIn("id='kb-leave-dlg'", page)
        self.assertIn("<dialog", page)
        self.assertIn("data-kb-leave='save'", page)
        self.assertIn("data-kb-leave='discard'", page)
        self.assertIn("data-kb-leave='cancel'", page)
        self.assertIn("保存草稿并离开", page)
        self.assertIn("放弃修改", page)
        self.assertIn("继续编辑", page)
        self.assertIn("showModal", page)

    def test_import_save_leave_submits_form_and_keeps_file(self):
        code, _, page = self.get("/admin/knowledge/import")
        self.assertEqual(code, 200)
        self.assertNotIn("window.prompt", page)
        self.assertIn("id='kb-leave-dlg'", page)
        self.assertIn("enctype='multipart/form-data'", page)
        self.assertIn("name='next'", page)
        # 保存离开必须提交表单（携带文件），不得 location.href 跳走；缺文件时明确阻止。
        self.assertIn("requestSubmit", page)
        self.assertIn("files.length", page)
        # 服务端：导入 + next 直接落草稿并跳转目标
        code, raw = self.post_multipart(
            "/admin/knowledge/import",
            {"next": "/admin/knowledge?view=files"},
            "服务承诺.txt", "保修 24 个月。".encode("utf-8"),
        )
        self.assertEqual(code, 303)
        item = self.db.list_knowledge_items(TENANT)[0]
        source = self.db.list_knowledge_sources(TENANT, item["id"])[0]
        row, data = self.db.read_knowledge_source_file(TENANT, source["id"])
        self.assertEqual(data, "保修 24 个月。".encode("utf-8"))

    def test_import_next_redirect_location(self):
        import http.client
        boundary = "----kb-review-boundary"
        body = (
            ("--" + boundary + "\r\n").encode()
            + b'Content-Disposition: form-data; name="next"\r\n\r\n'
            + b"/admin/knowledge?view=files\r\n"
            + ("--" + boundary + "\r\n").encode()
            + b'Content-Disposition: form-data; name="file"; filename="a.txt"\r\n'
            + b"Content-Type: text/plain\r\n\r\n"
            + "内容".encode("utf-8") + b"\r\n"
            + ("--" + boundary + "--\r\n").encode()
        )
        conn = http.client.HTTPConnection("127.0.0.1", self.server.server_port)
        conn.request("POST", "/admin/knowledge/import", body=body, headers={
            "Content-Type": "multipart/form-data; boundary=" + boundary,
            "Cookie": self.cookies[self.admin["id"]],
        })
        response = conn.getresponse()
        location = response.getheader("Location")
        response.read()
        conn.close()
        self.assertEqual(response.status, 303)
        self.assertEqual(location, "/admin/knowledge?view=files")


class SearchClearTests(Review23Base):
    """清单 2：真实按钮、即时显隐、只清关键词、保留筛选。"""

    def test_clear_button_markup_and_hidden_state(self):
        self.publish_prepared(self.create_article(title="整机保修与售后响应政策"), confirm_ai=True)
        _, _, page = self.get("/admin/knowledge?q=%E4%BF%9D%E4%BF%AE&status=published")
        self.assertIn("<button type='button' class='kb-clear' data-kb-clear", page)
        self.assertIn("aria-label='清空搜索'", page)
        # 有词时不带 hidden；其他筛选保留在数据属性里（JS 兜底路径也保筛选）。
        self.assertRegex(page, r"data-kb-clear[^>]*data-href='[^']*status=published[^']*'[^>]*>")
        self.assertNotRegex(page, r"data-kb-clear[^>]*data-href='[^']*status=published[^']*' hidden")
        _, _, page = self.get("/admin/knowledge?status=published")
        self.assertRegex(page, r"data-kb-clear[^>]* hidden")
        # 输入框右侧留出按钮空间（文字不被 × 遮挡）。
        self.assertIn("padding:9px 40px 9px 12px", page)
        # 即时显隐 + 清词后焦点回到输入框（动态行为由 Node 驱动测试复核）。
        self.assertIn("kb-clear", page)
        self.assertIn("kb-focus-search", page)


class NavTests(Review23Base):
    """清单 3：展开与导航、直接子视图默认展开、独立箭头、活动态。"""

    def test_default_collapsed_on_main_list(self):
        _, _, page = self.get("/admin/knowledge")
        self.assertIn("aria-expanded='false'", page)
        self.assertIn("inert", page)
        self.assertIn("data-nav-toggle", page)
        self.assertIn("<a class='side-link active' href='/admin/knowledge'", page)

    def test_direct_subview_defaults_expanded_and_active(self):
        _, _, page = self.get("/admin/knowledge?view=article_faq")
        self.assertIn("data-sub-active='1'", page)
        self.assertIn("aria-expanded='true'", page)
        self.assertNotRegex(page, r"id='nav-knowledge-subs'[^>]*inert")
        self.assertIn("<a class='side-link sub active' href='/admin/knowledge?view=article_faq'", page)
        # 子视图时主链接不是活动态（活动态只在子项上）。
        self.assertNotIn("<a class='side-link active' href='/admin/knowledge'", page)

    def test_main_link_expands_and_navigates_js(self):
        _, _, page = self.get("/admin/knowledge")
        self.assertIn("data-kb-nav-main", page)
        self.assertIn("kb-nav-expanded", page)
        self.assertIn("data-sub-active", page)


class DetailVersionTests(Review23Base):
    """清单 4：默认显示已发布生效版本；草稿提示与入口；版本内容实际可见。"""

    def test_detail_shows_published_version_not_draft(self):
        item_id = self.create_article(title="发货政策", body="已发布正文：3-5 天到货。")
        self.publish_prepared(item_id, confirm_ai=True)
        self.db.save_knowledge_draft(TENANT, item_id, self.admin["id"], content={"body": "草稿正文：改为 2-4 天到货。"})
        _, _, page = self.get(f"/admin/knowledge/item/{item_id}")
        self.assertIn("已发布正文：3-5 天到货。", page)
        self.assertNotIn("草稿正文：改为 2-4 天到货。", page)
        self.assertIn("有草稿修改待发布", page)
        self.assertIn(f"href='/admin/knowledge/item/{item_id}/edit'", page)
        self.assertIn("当前版本 v1", page)

    def test_versions_tab_shows_actual_content(self):
        item_id = self.create_article(title="保修政策", body="v1 正文：保修 24 个月。")
        self.publish_prepared(item_id, confirm_ai=True)
        self.db.save_knowledge_draft(TENANT, item_id, self.admin["id"], content={"body": "v2 草稿正文：保修 36 个月。"})
        _, _, page = self.get(f"/admin/knowledge/item/{item_id}?tab=versions")
        self.assertIn("v1 正文：保修 24 个月。", page)
        self.assertIn("v2 草稿正文：保修 36 个月。", page)
        self.assertIn("<details", page)
        self.assertIn("查看内容", page)


class PublishConfirmationTests(Review23Base):
    """清单 5：publish 动作必须显式确认字段（对外+授权）；直接 POST 不得发布。"""

    def test_new_publish_requires_confirmation_field(self):
        code, _, _ = self.post("/admin/knowledge/new", {
            "form": "article", "title": "对外承诺", "body": "3-5 天到货。",
            "usage": "external", "ai_authorized": "1", "action": "publish",
        })
        item = self.db.list_knowledge_items(TENANT)[0]
        self.assertEqual(self.db.get_knowledge_item(TENANT, item["id"])["status"], "draft",
                         "缺少 confirm_ai 时不得发布")
        self.assertEqual(code, 303)
        item2_count = len(self.db.list_knowledge_items(TENANT))
        code, _, _ = self.post("/admin/knowledge/new", {
            "form": "article", "title": "对外承诺2", "body": "3-5 天到货。",
            "usage": "external", "ai_authorized": "1", "action": "publish", "confirm_ai": "1",
        })
        self.assertEqual(code, 303)
        self.assertEqual(len(self.db.list_knowledge_items(TENANT)), item2_count + 1)
        published = [i for i in self.db.list_knowledge_items(TENANT) if i["id"] != item["id"]][0]
        self.assertEqual(self.db.get_knowledge_item(TENANT, published["id"])["status"], "published")

    def test_edit_publish_requires_confirmation_field(self):
        item_id = self.create_article(title="对外草稿", body="初稿。")
        self.db.save_knowledge_draft(TENANT, item_id, self.admin["id"],
                                     content={"body": "更新稿。"}, usage="external", ai_authorized=True)
        # 表单字段与实际编辑器一致（对外+授权），但缺少显式确认字段：保存草稿但不发布，并给出原因。
        code, headers, _ = self.post(f"/admin/knowledge/item/{item_id}/edit",
                                     {"action": "publish", "body": "更新稿。", "usage": "external", "ai_authorized": "1"})
        self.assertEqual(code, 303)
        self.assertIn("error=", headers.get("Location", ""), "未确认的发布应回到编辑页说明原因")
        item = self.db.get_knowledge_item(TENANT, item_id)
        self.assertEqual(item["status"], "draft")
        draft = self.db.get_knowledge_draft_version(TENANT, item_id)
        self.assertIsNotNone(draft, "被拒绝的发布不得丢失草稿")
        # 有确认字段：发布。
        code, _, _ = self.post(f"/admin/knowledge/item/{item_id}/edit",
                               {"action": "publish", "body": "更新稿。", "usage": "external", "ai_authorized": "1", "confirm_ai": "1"})
        self.assertEqual(code, 303)
        self.assertEqual(self.db.get_knowledge_item(TENANT, item_id)["status"], "published")

    def test_internal_publish_needs_no_confirmation(self):
        item_id = self.create_article(title="对内资料", body="内部说明。")
        code, _, _ = self.post(f"/admin/knowledge/item/{item_id}/edit", {"action": "publish", "body": "内部说明。"})
        self.assertEqual(code, 303)
        self.assertEqual(self.db.get_knowledge_item(TENANT, item_id)["status"], "published")

    def test_editor_has_confirm_field_and_js_sets_it(self):
        item_id = self.create_article()
        _, _, page = self.get(f"/admin/knowledge/item/{item_id}/edit")
        self.assertIn("name='confirm_ai'", page)
        self.assertIn("confirm_ai", page)
        self.assertNotIn("window.prompt", page)


class RowAccessibilityTests(Review23Base):
    """清单 6：整行链接、一枚生命周期标签、准备中作为原因文字。"""

    def test_row_is_whole_link_with_single_pill(self):
        item_id = self.create_article(title="保修政策")
        self.publish_prepared(item_id, confirm_ai=True)
        _, _, page = self.get("/admin/knowledge")
        self.assertIn(f"<a class='kb-row' href='/admin/knowledge/item/{item_id}'", page)
        self.assertEqual(page.count("<span class='kb-pill"), 1, "每行只应有一枚生命周期标签")

    def test_pending_processing_is_reason_not_second_pill(self):
        item_id = self.create_article(title="准备中政策", body="正文。")
        self.db.publish_knowledge_draft(TENANT, item_id, self.admin["id"], confirm_ai=True)
        _, _, page = self.get(f"/admin/knowledge/item/{item_id}")
        self.assertEqual(page.count("<span class='kb-pill"), 1, "准备中不得再出一枚 pill")
        self.assertIn("准备中", page)
        self.assertNotIn("<span class='kb-pill pub'>准备中", page)


class PrototypeAlignmentTests(Review23Base):
    """清单 7：无伪角色/伪计时；保留配色、添加菜单、动画、图标与移动端。"""

    def kb_js_block(self, page):
        start = page.find("// 未保存保护")
        end = page.find("</script>", start)
        return page[start:end]

    def test_no_fake_role_or_timer_on_knowledge_pages(self):
        item_id = self.create_article()
        for path in ("/admin/knowledge", f"/admin/knowledge/item/{item_id}",
                     f"/admin/knowledge/item/{item_id}/edit", "/admin/knowledge/import"):
            _, _, page = self.get(path)
            self.assertNotIn("role-select", page, path)
            self.assertNotIn("权限视角", page, path)
            self.assertNotIn("2 秒", page, path)
            self.assertNotIn("模拟约", page, path)
            self.assertNotIn("setTimeout", self.kb_js_block(page), path)

    def test_preserved_ux_tokens(self):
        _, _, page = self.get("/admin/knowledge")
        self.assertIn("kb-addmenu", page)                    # 添加菜单
        self.assertIn("--ok-soft", page)                     # 绿色可用配色
        self.assertIn("grid-template-rows", page)            # 折叠动画
        self.assertIn("prefers-reduced-motion", page)
        self.assertIn("@media(max-width:600px)", page)       # 移动端


class DynamicDomTests(unittest.TestCase):
    """清单 1/2/3 动态 DOM 复核：用 Node + 迷你 DOM 驱动真实 KB_JS 与折叠导航 JS。

    本驱动加载的是应用实际发布的脚本源码（模块常量），不是重新实现的近似逻辑；
    它验证行为（即时显隐、表单提交、跳转与焦点恢复、展开状态），不代表真实浏览器视觉走查。
    """

    @classmethod
    def setUpClass(cls):
        import shutil
        import subprocess as _subprocess
        cls.node = shutil.which("node")
        if cls.node is None:
            raise unittest.SkipTest("未找到 node：动态 DOM 复核跳过（未验证）")
        version = _subprocess.run([cls.node, "--version"], capture_output=True, text=True, timeout=30).stdout.strip()
        try:
            major = int(version.lstrip("v").split(".")[0])
        except ValueError:
            major = 0
        if major < 18:
            raise unittest.SkipTest(f"node 版本过低（{version}）：动态 DOM 复核跳过（未验证）")

    def run_harness(self):
        import json
        import subprocess as _subprocess
        import tempfile
        from app import CONSOLE_KNOWLEDGE_NAV_JS
        from enterprise_knowledge_ui import KB_FORM_SWITCH_JS, KB_JS
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.json"
            result_path = Path(tmp) / "result.json"
            config_path.write_text(json.dumps({
                "kbJs": KB_JS, "navJs": CONSOLE_KNOWLEDGE_NAV_JS, "switchJs": KB_FORM_SWITCH_JS,
            }), encoding="utf-8")
            proc = _subprocess.run(
                [self.node, "tools/browser_tests/knowledge_ui_browser.mjs", str(config_path), str(result_path)],
                capture_output=True, text=True, timeout=120,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            return json.loads(result_path.read_text(encoding="utf-8"))

    def test_search_clear_dynamic_behavior(self):
        facts = self.run_harness()["scenarios"]["searchClear"]
        self.assertTrue(facts["initialHidden"], "无关键词时清空按钮为隐藏态")
        self.assertTrue(facts["visibleAfterTyping"], "输入时即时显示清空按钮")
        self.assertTrue(facts["hiddenAfterDeleting"], "删空关键词后即时隐藏")
        self.assertEqual(facts["valueAfterClear"], "", "点击只清关键词")
        self.assertTrue(facts["submitted"], "点击后提交原筛选表单（保留其他筛选并刷新真实结果）")
        self.assertEqual(facts["focusMarker"], "1", "清空后设置焦点恢复标记")
        self.assertIn(facts["navigated"], (None, ""), "正常路径不依赖 location.href 兜底")

    def test_editor_leave_dialog_dynamic_behavior(self):
        facts = self.run_harness()["scenarios"]["editorLeave"]
        self.assertIn("cancel", facts["listenerTypes"], "Escape 通过 dialog cancel 处理")
        self.assertTrue(facts["openedOnLinkClick"], "有未保存内容时点击站内链接弹出对话框")
        self.assertTrue(facts["navPrevented"], "弹出对话框时阻止直接跳转")
        self.assertTrue(facts["cancelClosed"], "继续编辑关闭对话框")
        self.assertIn(facts["cancelNavigated"], (None, ""), "继续编辑不跳转")
        self.assertEqual(facts["discardNavigated"], "/admin/knowledge", "放弃修改跳转到目标")
        self.assertTrue(facts["emptyTitleBlocked"], "标题为空时保存离开明确阻止并说明原因")
        self.assertFalse(facts["emptyTitleSubmitted"], "被阻止时不提交表单")
        self.assertIn(facts["emptyTitleNav"], (None, ""), "被阻止时不跳转、不丢内容")
        self.assertTrue(facts["savedSubmitted"], "保存草稿并离开通过表单提交")
        self.assertEqual(facts["savedSubmitter"], "action=save_draft", "编辑器提交使用保存草稿动作")
        self.assertEqual(facts["savedNext"], "/admin/knowledge", "提交携带离开目标（服务端重定向）")
        self.assertIn(facts["savedNavigated"], (None, ""), "保存路径不落 location.href")
        self.assertTrue(facts["cancelFocusRestored"], "关闭对话框后焦点回到触发元素")

    def test_import_leave_dialog_dynamic_behavior(self):
        facts = self.run_harness()["scenarios"]["importLeave"]
        self.assertTrue(facts["openedOnLinkClick"], "导入页有已选文件时弹出未保存对话框")
        self.assertTrue(facts["noFileBlocked"], "未选文件时保存离开明确阻止")
        self.assertFalse(facts["noFileSubmitted"], "未选文件时不提交")
        self.assertTrue(facts["savedSubmitted"], "有文件时保存离开提交表单（携带文件字节）")
        self.assertEqual(facts["savedVia"], "requestSubmit", "导入提交走表单提交而非 location.href")
        self.assertEqual(facts["savedNext"], "/admin/knowledge?view=files", "提交携带离开目标")
        self.assertTrue(facts["fileKept"], "文件选择在保存路径中保留")
        self.assertIn(facts["savedNavigated"], (None, ""), "保存路径不落 location.href 丢文件")

    def test_nav_fold_dynamic_behavior(self):
        facts = self.run_harness()["scenarios"]["navFold"]
        self.assertTrue(facts["serverSubActiveOpened"], "直接子视图默认展开（服务端标记）")
        self.assertEqual(facts["ariaExpanded"], "true")
        self.assertTrue(facts["serverWinsOverStoredClosed"],
                        "清单13：会话存过收起时，直接进入活动子视图仍展开（服务端优先）")
        self.assertTrue(facts["explicitCollapsed"], "无活动子视图时会话存储的收起仍然生效")
        self.assertTrue(facts["explicitOpened"], "显式展开优先")
        self.assertTrue(facts["toggleClosedByDefault"], "无服务端标记且无存储时默认收起")
        self.assertTrue(facts["toggleOpened"], "箭头独立展开")
        self.assertEqual(facts["toggleStored"], "1", "箭头点击记录展开状态")
        self.assertIn(facts["toggleNavigated"], (None, ""), "箭头只切换不跳转")
        self.assertTrue(facts["toggleClosedAgain"], "箭头再次点击收起")
        self.assertEqual(facts["toggleStoredAfterClose"], "0")
        self.assertTrue(facts["mainLinkOpened"], "主按钮点击展开")
        self.assertEqual(facts["mainLinkStored"], "1", "主按钮进入并保持展开状态")
        self.assertIn(facts["mainLinkNavigated"], (None, ""), "主按钮跳转由链接默认行为完成")
        self.assertEqual(facts["subLinkStored"], "1", "子项点击保持展开")


if __name__ == "__main__":
    unittest.main()
