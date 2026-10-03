"""参考聊天窗消息重试逻辑的浏览器端回归测试（Node 加载真实 app.js + 真实 HTTP/数据库）。

针对 B-08（Codex 独立复现）：POST /messages 首次保存消息后提取失败返回 503；服务恢复后
同键同内容重试在旧实现中返回 200 status=duplicate 且 requirement_draft=null，聊天窗把
该成功响应当作已处理并清空输入，买家无法依提示完成重试。此处用 Node 直接加载未经修改的
reference-client/app.js（vm + localStorage/DOM/fetch 桩）驱动两类场景，并对照真实服务与
数据库断言：

- 提取失败（服务端故障注入一次）→ 503：提示可见、输入保留、提交键保留；服务恢复后同键
  重试 → 200 duplicate + 草稿：输入清空、界面刷新，两请求同键同消息，数据库只存一份；
- 旧版服务的无草稿成功响应（B-08 报告的原始载荷：200 duplicate、requirement_draft=null，
  由 fetch 桩注入）：不得清空输入、显示提示、保留同一提交键；随后同键提交命中真实服务
  → 201 saved + 草稿 → 输入清空。

场景由 tools/browser_tests/chat_message_retry_browser.mjs 驱动（同一测试类内一次运行、
逐项断言）；依赖 Node >= 18（全局 fetch/Response）。Node 不可用或版本过低时整体跳过，
不冒充已测。
"""
import http.client
import json
import re
import shutil
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from app import make_server

ROOT = Path(__file__).resolve().parent
HARNESS = ROOT / "tools" / "browser_tests" / "chat_message_retry_browser.mjs"
NODE = shutil.which("node")
FAILURE_TEXT = "Need pump flow 50 m3/h, head 30 m, clean water"
MOCK_TEXT = "please advise on a pump for the plant room"


def node_major_version(path: str) -> int:
    try:
        output = subprocess.run([path, "--version"], capture_output=True, text=True, check=True, timeout=30).stdout.strip()
        match = re.match(r"v(\d+)", output)
        return int(match.group(1)) if match else 0
    except Exception:
        return 0


class ChatMessageRetryBrowserTests(unittest.TestCase):
    maxDiff = None

    @classmethod
    def setUpClass(cls):
        if NODE is None:
            raise unittest.SkipTest("未找到 node：聊天窗消息重试浏览器端回归测试跳过（未验证）")
        if node_major_version(NODE) < 18:
            raise unittest.SkipTest("node 版本过低（需要 >= 18 的全局 fetch/Response）：浏览器端回归测试跳过（未验证）")
        cls.temp_dir = tempfile.TemporaryDirectory()
        cls.server = make_server(Path(cls.temp_dir.name) / "app.db", port=0)
        cls.db = cls.server.RequestHandlerClass.db
        cls.db.seed_demo_users()
        cls.db.register_buyer_site("demo-tenant", "site-a", "https://seller-a.example")
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base_url = f"http://127.0.0.1:{cls.server.server_port}"

        status, _, created = cls.request_via(
            "POST", "/api/v1/buyer/conversations", {"site_key": "site-a"}, {"Origin": "https://seller-a.example"}
        )
        if status != 201:
            raise AssertionError(f"创建会话失败: {status} {created}")
        cls.conversation_id = created["conversation_id"]
        cls.conversation_token = created["access_token"]

        config = {
            "baseUrl": cls.base_url,
            "siteKey": "site-a",
            "conversationId": cls.conversation_id,
            "token": cls.conversation_token,
            "failureText": FAILURE_TEXT,
            "mockText": MOCK_TEXT,
        }
        config_path = Path(cls.temp_dir.name) / "config.json"
        result_path = Path(cls.temp_dir.name) / "result.json"
        config_path.write_text(json.dumps(config), encoding="utf-8")

        # 故障注入：仅第一次提取调用失败（第一次发送拿到 503；同键重试时服务已“恢复”）。
        import app as app_module

        state = {"calls": 0}
        original_extract = app_module.extract_requirement_values
        counter = threading.Lock()

        def flaky_extract(text, explicit, previous=None):
            with counter:
                state["calls"] += 1
                call_number = state["calls"]
            if call_number == 1:
                raise RuntimeError("注入：模型提取服务暂不可用")
            return original_extract(text, explicit, previous)

        with patch("app.extract_requirement_values", side_effect=flaky_extract):
            completed = subprocess.run(
                [NODE, str(HARNESS), str(config_path), str(result_path)],
                capture_output=True, text=True, timeout=180,
            )
        cls.harness_stdout = completed.stdout
        cls.harness_stderr = completed.stderr
        if completed.returncode != 0 or not result_path.exists():
            raise AssertionError(
                f"node 场景驱动失败（exit {completed.returncode}）\nstdout: {completed.stdout}\nstderr: {completed.stderr}"
            )
        cls.result = json.loads(result_path.read_text(encoding="utf-8"))
        if not cls.result.get("ok"):
            raise AssertionError(f"node 场景驱动未完成全部场景: {json.dumps(cls.result, ensure_ascii=False)[:4000]}")

    @classmethod
    def tearDownClass(cls):
        if getattr(cls, "server", None):
            cls.server.shutdown()
            cls.server.server_close()
            cls.db.conn.close()
        if getattr(cls, "thread", None):
            cls.thread.join()
        if getattr(cls, "temp_dir", None):
            cls.temp_dir.cleanup()

    @classmethod
    def request_via(cls, method, path, payload=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", cls.server.server_port)
        request_headers = {"Content-Type": "application/json", **(headers or {})}
        conn.request(method, path, json.dumps(payload).encode() if payload is not None else None, request_headers)
        response = conn.getresponse()
        data = response.read()
        result = response.status, dict(response.getheaders()), json.loads(data) if data else None
        conn.close()
        return result

    # ---- 场景结果的读取辅助 -------------------------------------------------

    def scenario(self, name):
        for item in self.result["scenarios"]:
            if item["name"] == name:
                return item
        raise AssertionError(f"node 驱动未输出场景 {name}：{json.dumps(self.result['scenarios'], ensure_ascii=False)[:2000]}")

    def message_posts(self, requests):
        posts = []
        for entry in requests:
            if entry["method"] == "POST" and re.fullmatch(r"/api/v1/buyer/conversations/[a-f0-9]+/messages", entry["url"]):
                posts.append(entry)
        return posts

    def stored_messages(self, content):
        return [row for row in self.db.list_messages("demo-tenant", self.conversation_id) if row["content"] == content]

    # ---- 两个场景断言 -------------------------------------------------------

    def test_failed_extraction_keeps_input_and_same_key_retry_recovers_draft(self):
        """提取失败 → 503 明确失败且输入保留；服务恢复后同键重试补出草稿并只存一份。"""
        s1 = self.scenario("failure_then_retry_recovery")
        posts = self.message_posts(s1["requests"])
        self.assertEqual(len(posts), 2, "本场景应恰有两次消息请求（首次失败 + 同键重试）")
        first, retry = posts
        # 首次失败：明确 503，不是假成功
        self.assertEqual(first["status"], 503)
        self.assertEqual((first["body"] or {}).get("error"), "extraction_unavailable")
        # 客户端没有清空输入，并显示提示
        self.assertEqual(s1["afterFirstSubmit"]["input"], FAILURE_TEXT)
        self.assertTrue(s1["afterFirstSubmit"]["notice"])
        # 重试与首次同键同内容，服务恢复后返回可用 revision（duplicate + 草稿）
        self.assertTrue(first["idempotencyKey"], "消息请求必须携带幂等键")
        self.assertEqual(retry["idempotencyKey"], first["idempotencyKey"], "失败后的重试必须沿用同一提交键")
        self.assertEqual(retry["status"], 200)
        self.assertEqual((retry["body"] or {}).get("status"), "duplicate")
        draft = (retry["body"] or {}).get("requirement_draft") or {}
        self.assertTrue(draft.get("revision_id"), "恢复后的重试必须返回可用需求草稿")
        self.assertTrue(draft.get("values"))
        # 成功后输入清空、界面刷新（提示被刷新清空）
        self.assertEqual(s1["afterRetrySubmit"]["input"], "")
        self.assertEqual(s1["afterRetrySubmit"]["notice"], "")
        # 数据库：消息只存一次；恰有一个修订且与返回的 revision 一致，并已附加到该消息
        messages = self.stored_messages(FAILURE_TEXT)
        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0]["result_revision_id"], draft["revision_id"])
        revisions = [row for row in self.db.get_revisions("demo-tenant", self.conversation_id) if row["id"] == draft["revision_id"]]
        self.assertEqual(len(revisions), 1)
        self.assertEqual(revisions[0]["status"], "candidate")
        total_messages_for_failure_text = len(self.stored_messages(FAILURE_TEXT))
        self.assertEqual(total_messages_for_failure_text, 1, "同键重试不得重复保存消息")

    def test_success_without_draft_does_not_clear_input_and_keeps_key(self):
        """旧版服务的无草稿成功响应：不得悄悄清空输入；保留同一键，再次提交补全草稿。"""
        s2 = self.scenario("legacy_no_draft_success_guard")
        posts = self.message_posts(s2["requests"])
        self.assertEqual(len(posts), 2, "本场景应恰有两次消息请求（被桩截获的坏响应 + 真实提交）")
        mocked, recovered = posts
        # 注入的正是 B-08 报告的旧版载荷：200 duplicate 且没有草稿
        self.assertTrue(mocked.get("mock"), "第一次消息请求应由 fetch 桩返回旧版坏载荷")
        self.assertEqual(mocked["status"], 200)
        self.assertIsNone((mocked["body"] or {}).get("requirement_draft"))
        # 客户端不得把它当作已处理：输入保留、提示可见
        self.assertEqual(s2["afterMockedSubmit"]["input"], MOCK_TEXT, "无草稿的成功响应不得清空输入")
        self.assertTrue(s2["afterMockedSubmit"]["notice"], "无草稿的成功响应必须给出可见提示")
        # 再次提交仍用同一提交键（提交键未被丢弃）；命中真实服务后 201 + 草稿，随后输入清空
        self.assertEqual(recovered["idempotencyKey"], mocked["idempotencyKey"])
        self.assertEqual(recovered["status"], 201)
        self.assertEqual((recovered["body"] or {}).get("status"), "saved")
        self.assertTrue(((recovered["body"] or {}).get("requirement_draft") or {}).get("revision_id"))
        self.assertEqual(s2["afterRecoveredSubmit"]["input"], "")
        self.assertEqual(s2["afterRecoveredSubmit"]["notice"], "")
        # 数据库：该文本只存了一条消息
        self.assertEqual(len(self.stored_messages(MOCK_TEXT)), 1)


if __name__ == "__main__":
    unittest.main()
