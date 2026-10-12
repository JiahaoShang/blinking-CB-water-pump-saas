"""企业知识第二批 · 批次发布设置确认路径（阻断修复的判别性测试）。

独立验收复现（`docs/evidence/enterprise-knowledge-phase2/b2b-phase2-b08b09b10-2026-10-12/
b2b-phase2-batch-confirm-current-observation.txt`）：批次发布设置里确认属性挂在「发布」按钮上，
而 `static/knowledge-phase2.js` 的确认对话框按表单（`form[data-kb2-confirm]` +
`form.getAttribute('data-kb2-confirm-field')`）绑定——真实浏览器点「发布」为直接提交；
批次设为对外 + 授权 AI 时后台正确拒绝（需要确认），用户无法经批次界面完成必需发布。

本文件固定修复后的契约（修复前运行应失败，属预期判别）：
1) 确认属性在表单上（不在按钮上）；默认 hidden `confirm_ai=0`；「发布」按钮保留 `action=publish`；
   同表单「保存设置」按钮带 `data-kb2-confirm-skip`（不弹确认、按自身动作提交）。
2) HTTP 契约：未确认的对外+AI 发布仍被后台拒绝（授权闸门不变）；确认后真实发布；内部常规
   批量发布保持可用；「保存设置」不受影响。
3) 组件行为（Node 迷你 DOM 驱动真实 `static/knowledge-phase2.js`）：发布→弹窗；取消/Escape
   不发布且不改默认值；「继续」写 `confirm_ai=1` 且重发保留触发按钮（`action=publish` 不丢失，
   修复前 `form.submit()` 会把它丢掉）；无 `requestSubmit` 回退注入同名隐藏字段。
只用临时数据库与测试私有上传目录；不触碰 data/ 与任何密钥；不接真实模型。
"""
import http.client
import json
import os
import shutil
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from urllib.parse import unquote, urlencode, urlsplit

from app import make_server

ROOT = Path(__file__).resolve().parent
TENANT = "demo-tenant"


def request(server, method, path, *, fields=None, files=None, cookie=None):
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


def page_text(raw):
    return raw.decode("utf-8", errors="replace")


class BatchPublishConfirmHttpTests(unittest.TestCase):
    maxDiff = None

    @classmethod
    def setUpClass(cls):
        cls.temp_dir = tempfile.TemporaryDirectory()
        cls.upload_dir = Path(cls.temp_dir.name) / "uploads"
        os.environ["B2B_KB_STORAGE_ROOT"] = str(cls.upload_dir)
        cls.db_path = Path(cls.temp_dir.name) / "kb2-batch-confirm.db"
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

    # ── 帮助 ──────────────────────────────────────────────────────────────

    def add_checked_batch(self):
        """上传一个 .txt 批次并逐段核对（完成识别只创建草稿；不自动发布）。"""
        conn = http.client.HTTPConnection("127.0.0.1", self.server.server_port)
        boundary = "----kb2batchconfirmboundary"
        content = "保修24个月。\n安装前断电。".encode("utf-8")
        parts = [
            (f"--{boundary}\r\nContent-Disposition: form-data; name=\"source_file\"; "
             "filename=\"保修.txt\"\r\nContent-Type: application/octet-stream\r\n\r\n").encode("utf-8"),
            content, b"\r\n", f"--{boundary}--\r\n".encode("utf-8"),
        ]
        conn.request("POST", "/admin/knowledge/add", body=b"".join(parts), headers={
            "Content-Type": "multipart/form-data; boundary=" + boundary, "Cookie": self.admin_cookie})
        response = conn.getresponse()
        status = response.status
        location = response.getheader("Location", "")
        response.read()
        conn.close()
        self.assertIn(status, (302, 303))
        batch_id = urlsplit(location).path.rsplit("/", 1)[1]
        self.db.kb2_process_pending_sources(TENANT)
        source = self.source_of(batch_id)
        fields = {"source_id": source["id"]}
        for paragraph in source["paragraphs"]:
            fields[f"p_{paragraph['id']}_text"] = paragraph["text"]
            fields[f"p_{paragraph['id']}_checked"] = "1"
        status, _, _ = request(self.server, "POST", f"/admin/knowledge/batch/{batch_id}/paragraphs",
                               fields=fields, cookie=self.admin_cookie)
        self.assertIn(status, (302, 303))
        return batch_id

    def make_external_ai_checked_batch(self):
        batch_id = self.add_checked_batch()
        status, _, _ = request(self.server, "POST", f"/admin/knowledge/batch/{batch_id}/settings",
                               fields={"usage": "external", "ai_authorized": "1", "action": "save"},
                               cookie=self.admin_cookie)
        self.assertIn(status, (302, 303))
        return batch_id

    def source_of(self, batch_id):
        return self.db.kb2_get_batch(TENANT, batch_id)["sources"][0]

    # ── 1. 标记：确认属性在表单上（不在按钮上）；默认未确认值保持 ──────────

    def test_publish_form_confirm_attributes_on_form_not_button(self):
        batch_id = self.make_external_ai_checked_batch()
        status, _, raw = request(self.server, "GET", f"/admin/knowledge/batch/{batch_id}?step=publish",
                                 cookie=self.admin_cookie)
        self.assertEqual(status, 200)
        page = page_text(raw)
        marker = "action='/admin/knowledge/batch/%s/settings'" % batch_id
        self.assertIn(marker, page)
        start = page.rindex("<form", 0, page.index(marker))
        form_tag = page[start:page.index(">", page.index(marker))]
        # 组件按表单属性绑定（form[data-kb2-confirm] + data-kb2-confirm-field/value）。
        for piece in ("data-kb2-confirm='", "data-kb2-confirm-title='发布批次'",
                      "data-kb2-confirm-field='confirm_ai'", "data-kb2-confirm-value='1'"):
            self.assertIn(piece, form_tag, "确认属性必须在表单上（组件按 form[data-kb2-confirm] 绑定）")
        # 默认 hidden confirm_ai=0：取消 / 未确认提交保持未确认（后台继续正确拒绝）。
        self.assertIn("name='confirm_ai' value='0'", page)
        # 「发布」按钮保留自身动作，但不再携带确认属性（修复前的缺陷形态即属性在按钮上）。
        self.assertIn("<button class='button' name='action' value='publish'>发布</button>", page)
        self.assertNotIn("value='publish' data-kb2-confirm", page)
        # 「保存设置」与发布同表单：声明 skip，按自身动作直接提交（不进入发布确认）。
        self.assertIn("name='action' value='save' data-kb2-confirm-skip", page)

    # ── 2. 授权闸门不变：未确认被拒；确认后（对话「继续」同口径）真实发布 ──

    def test_unconfirmed_external_ai_publish_rejected_then_confirm_succeeds(self):
        batch_id = self.make_external_ai_checked_batch()
        base = f"/admin/knowledge/batch/{batch_id}/settings"
        # 未经确认的发布（如对话框未接入时的直接提交）：后台保持拒绝且不发布。
        status, headers, _ = request(self.server, "POST", base,
                                     fields={"usage": "external", "ai_authorized": "1",
                                             "action": "publish", "confirm_ai": "0"},
                                     cookie=self.admin_cookie)
        self.assertIn(status, (302, 303))
        self.assertIn("error=", headers["Location"])
        self.assertIn("需要确认", unquote(headers["Location"]))
        self.assertEqual(self.source_of(batch_id)["status"], "extracted")
        # 对话框「继续」后送达的请求（action=publish + confirm_ai=1）：真实发布。
        status, headers, _ = request(self.server, "POST", base,
                                     fields={"usage": "external", "ai_authorized": "1",
                                             "action": "publish", "confirm_ai": "1"},
                                     cookie=self.admin_cookie)
        self.assertIn(status, (302, 303))
        self.assertNotIn("error=", headers["Location"])
        self.assertIn("已发布", unquote(headers["Location"]))
        self.assertEqual(self.source_of(batch_id)["status"], "published")

    # ── 3. 内部常规批量发布可用；「保存设置」不受影响 ─────────────────────

    def test_internal_batch_publish_available_and_save_unaffected(self):
        batch_id = self.add_checked_batch()
        base = f"/admin/knowledge/batch/{batch_id}/settings"
        status, headers, _ = request(self.server, "POST", base,
                                     fields={"usage": "internal", "action": "publish", "confirm_ai": "1"},
                                     cookie=self.admin_cookie)
        self.assertIn(status, (302, 303))
        self.assertNotIn("error=", headers["Location"])
        self.assertIn("已发布", unquote(headers["Location"]))
        self.assertEqual(self.source_of(batch_id)["status"], "published")
        # 「保存设置」原动作：保存后仍是草稿（可稍后发布），未发布。
        batch2 = self.add_checked_batch()
        status, headers, _ = request(self.server, "POST", f"/admin/knowledge/batch/{batch2}/settings",
                                     fields={"usage": "external", "ai_authorized": "1", "action": "save"},
                                     cookie=self.admin_cookie)
        self.assertIn(status, (302, 303))
        self.assertIn("已保存发布设置", unquote(headers["Location"]))
        self.assertEqual(self.source_of(batch2)["status"], "extracted")


class BatchPublishConfirmComponentTests(unittest.TestCase):
    """用 Node 迷你 DOM 驱动真实 static/knowledge-phase2.js（确认组件）验证交互契约。"""

    maxDiff = None

    @classmethod
    def setUpClass(cls):
        cls.node = shutil.which("node")
        if cls.node is None:
            raise unittest.SkipTest("未找到 node：批次发布确认组件动态复核跳过（未验证）")
        version = subprocess.run([cls.node, "--version"], capture_output=True, text=True, timeout=30).stdout.strip()
        try:
            major = int(version.lstrip("v").split(".")[0])
        except ValueError:
            major = 0
        if major < 18:
            raise unittest.SkipTest(f"node 版本过低（{version}）：批次发布确认组件动态复核跳过（未验证）")

    def run_harness(self):
        config = {"phase2Js": (ROOT / "static" / "knowledge-phase2.js").read_text(encoding="utf-8")}
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.json"
            result_path = Path(tmp) / "result.json"
            config_path.write_text(json.dumps(config), encoding="utf-8")
            proc = subprocess.run(
                [self.node, str(ROOT / "tools" / "browser_tests" / "knowledge_phase2_confirm_browser.mjs"),
                 str(config_path), str(result_path)],
                capture_output=True, text=True, timeout=120)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            return json.loads(result_path.read_text(encoding="utf-8"))

    def test_publish_confirm_flow_preserves_action_and_skips_save(self):
        facts = self.run_harness()["scenarios"]["confirmFlow"]
        self.assertTrue(facts["confirmDialogBuilt"] and facts["guardDialogBuilt"])
        # 「保存设置」（skip）：不弹窗、不阻止、默认值不动 → 按自身动作直接提交。
        self.assertTrue(facts["saveNoDialog"], "保存设置不得进入发布确认对话框")
        self.assertTrue(facts["saveNotPrevented"], "保存设置提交不得被确认逻辑阻止")
        self.assertTrue(facts["saveConfirmFieldUntouched"])
        # 「发布」：拦截并弹窗（文案=发布批次）；确认前默认值保持 0。
        self.assertTrue(facts["publishPrevented"] and facts["publishDialogOpened"])
        self.assertEqual(facts["publishDialogTitle"], "发布批次")
        self.assertIn("继续发布", facts["publishDialogMessage"])
        self.assertEqual(facts["confirmFieldBeforeConfirm"], "0", "确认前不得改写 confirm_ai")
        # 取消 / Escape：关闭、不提交、默认值保持 0、焦点回触发按钮。
        self.assertTrue(facts["cancelClosed"] and facts["cancelNoSubmit"])
        self.assertEqual(facts["cancelConfirmField"], "0", "取消不得写入 confirm_ai")
        self.assertTrue(facts["cancelFocusRestored"])
        self.assertTrue(facts["escapeDialogReopened"] and facts["escapeClosed"] and facts["escapeNoSubmit"])
        self.assertEqual(facts["escapeConfirmField"], "0", "Escape 不得写入 confirm_ai")
        self.assertTrue(facts["escapeFocusRestored"])
        # 「继续」：写 confirm_ai=1；重发保留触发按钮（action=publish 不丢失——修复前会丢）。
        self.assertTrue(facts["acceptClosed"])
        self.assertEqual(facts["acceptConfirmField"], "1", "继续必须显式写 confirm_ai=1")
        self.assertTrue(facts["acceptResubmitted"])
        self.assertEqual(facts["acceptSubmitter"], "action=publish",
                         "确认后重发必须保留触发按钮字段（否则动作被降级为保存设置）")
        # 真实浏览器 requestSubmit 的二次 submit 重放：已确认 → 放行、不再弹窗。
        self.assertTrue(facts["replayNotPrevented"] and facts["replayNoSecondDialog"])
        # 无 submitter 的提交仍走确认（skip 只对声明按钮生效）。
        self.assertTrue(facts["nullSubmitterPrevented"] and facts["nullSubmitterDialogOpened"])

    def test_fallback_injects_submitter_field_without_request_submit(self):
        facts = self.run_harness()["scenarios"]["fallback"]
        self.assertTrue(facts["confirmDialogBuilt"])
        self.assertTrue(facts["publishDialogOpened"])
        self.assertTrue(facts["fallbackSubmitted"])
        self.assertEqual(facts["fallbackVia"], "submit")
        self.assertEqual(facts["fallbackConfirmField"], "1")
        self.assertTrue(facts["fallbackInjectedAction"],
                        "无 requestSubmit 时必须注入 action=publish 并提交（保留发布动作）")


if __name__ == "__main__":
    unittest.main()
