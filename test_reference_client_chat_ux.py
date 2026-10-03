"""参考聊天窗口交互回归：系统语言欢迎语、统一输入框分流、任务回复、实时同步与未读角标。

- 源码契约测试（始终运行）：欢迎语/语言判定/分流函数存在；常驻问答表单与「刷新回复」按钮已移除；
  轮询同步与未读角标机制存在。
- 浏览器行为测试（需 Node >= 18；缺失或版本过低时整类跳过、不冒充已测）：用 vm 加载
  未经修改的 reference-client/app.js，接通真实 HTTP 服务驱动六个场景（欢迎语 en/zh、需求分流、
  提问绑定、工程师追问→输入框回复、实时同步+未读角标），把请求日志、渲染文本与真实响应体输出为
  result.json，由本文件逐项断言。工程师一侧走真实登录（demo-engineer 演示账号）与真实后台表单
  端点，不用买方接口代演。
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

from app import make_server

ROOT = Path(__file__).resolve().parent
HARNESS = ROOT / "tools" / "browser_tests" / "reference_chat_ux_browser.mjs"
NODE = shutil.which("node")


def node_major_version(path: str) -> int:
    try:
        output = subprocess.run([path, "--version"], capture_output=True, text=True, check=True, timeout=30).stdout.strip()
        match = re.match(r"v(\d+)", output)
        return int(match.group(1)) if match else 0
    except Exception:
        return 0


def request_via(server, method, path, payload=None, headers=None):
    conn = http.client.HTTPConnection("127.0.0.1", server.server_port)
    body = json.dumps(payload).encode("utf-8") if payload is not None else None
    merged = {"Content-Type": "application/json", **(headers or {})}
    conn.request(method, path, body=body, headers=merged)
    response = conn.getresponse()
    raw = response.read().decode("utf-8")
    conn.close()
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        parsed = raw
    return response.status, dict(response.getheaders()), parsed


def approve_all_fields(db, tenant_id, product_id):
    """Simulate an admin who individually approved every sourced candidate field."""
    for field in db.get_fields(tenant_id, product_id):
        if field["state"] == "candidate":
            db.approve_field(tenant_id, product_id, field["field_key"])
    db.finalize_product_review(tenant_id, product_id)


class ReferenceChatSourceContractTests(unittest.TestCase):
    """不需 Node 的源码契约：即使浏览器驱动不可用，也拦住「常驻问答卡」的回归。"""

    def test_unified_input_and_greeting_contract(self):
        source = (ROOT / "reference-client" / "app.js").read_text(encoding="utf-8")
        self.assertIn("greeting", source)
        self.assertIn("systemPrefersChinese", source)
        self.assertIn("navigator.languages", source)
        self.assertIn("looksLikeQuestion", source)
        self.assertNotIn("renderQuestionForm", source)
        self.assertNotIn("提出产品问题", source)
        self.assertNotIn("Ask a product question", source)

    def test_realtime_sync_and_task_reply_contract(self):
        source = (ROOT / "reference-client" / "app.js").read_text(encoding="utf-8")
        # 实时同步（轮询 + 未读角标）；手动「刷新回复」按钮已移除
        self.assertIn("POLL_INTERVAL_MS", source)
        self.assertIn("chat-badge", source)
        self.assertNotIn("Refresh replies", source)
        self.assertNotIn("刷新回复", source)
        # 工程师追问的回复走底部输入框（不再有独立表单）
        self.assertIn("replyHint", source)
        self.assertNotIn("Reply to engineer", source)
        self.assertNotIn("Your additional details", source)
        html = (ROOT / "reference-client" / "index.html").read_text(encoding="utf-8")
        self.assertNotIn('id="refresh"', html)
        self.assertIn('id="chat-badge"', html)
        # 有据回答消息不再作为气泡重复卡片内容；待核实回答保留气泡陈述（见浏览器断言）
        self.assertIn("grounded-answer", source)
        # 转交确认以消息流形式播报（客户端按语言本地化）；悬浮按钮文案本地化
        self.assertIn("human-request", source)
        self.assertIn("launcher", source)
        self.assertIn('id="chat-launcher-label"', (ROOT / "reference-client" / "index.html").read_text(encoding="utf-8"))
        # 兜底文案不再写死邀请语（动向与入口只由界面按真实状态给出）
        app_source = (ROOT / "app.py").read_text(encoding="utf-8")
        self.assertNotIn("可请求工程师查看", app_source)
        self.assertNotIn("我可以在您点击请求后请工程师查看", app_source)
        self.assertNotIn("若您希望工程师核实，可点击下方按钮", app_source)
        self.assertIn("HUMAN_REQUEST_ACK", app_source)


class ReferenceChatBrowserTests(unittest.TestCase):
    """真实链路的浏览器行为驱动（Node + 真实 HTTP/数据库）。"""

    maxDiff = None

    @classmethod
    def setUpClass(cls):
        if NODE is None:
            raise unittest.SkipTest("未找到 node：参考聊天浏览器行为测试跳过（未验证）")
        if node_major_version(NODE) < 18:
            raise unittest.SkipTest("node 版本过低（需要 >= 18 的全局 fetch）：参考聊天浏览器行为测试跳过（未验证）")
        cls.temp_dir = tempfile.TemporaryDirectory()
        db_path = Path(cls.temp_dir.name) / "app.db"
        cls.server = make_server(db_path, port=0)
        cls.db = cls.server.RequestHandlerClass.db
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base_url = f"http://127.0.0.1:{cls.server.server_port}"
        cls.db.seed_demo_users()
        cls.db.register_buyer_site("demo-tenant", "site-a", cls.base_url)
        cls.db.seed_demo_products("demo-tenant")
        for product in cls.db.list_products("demo-tenant"):
            approve_all_fields(cls.db, "demo-tenant", product["id"])
            cls.db.publish_product("demo-tenant", product["id"])

        # 会话 A：仅用于「需求描述 → 需求识别」分流
        status, _, created = request_via(cls.server, "POST", "/api/v1/buyer/conversations", {"site_key": "site-a"})
        if status != 201:
            raise AssertionError(f"创建会话 A 失败: {status} {created}")
        cls.requirement_conversation = created

        # 会话 B：预置「确认工况 + 推荐」链路，用于「提问 → 有据问答」与型号绑定
        status, _, created_b = request_via(cls.server, "POST", "/api/v1/buyer/conversations", {"site_key": "site-a"})
        if status != 201:
            raise AssertionError(f"创建会话 B 失败: {status} {created_b}")
        cls.question_conversation = created_b
        path = f"/api/v1/buyer/conversations/{created_b['conversation_id']}"
        headers = {"X-Site-Key": "site-a", "Authorization": f"Bearer {created_b['access_token']}"}
        status, _, sent = request_via(cls.server, "POST", path + "/messages", {"text": "Need pump flow 50 m3/h, head 30 m, clean water"}, headers)
        if status != 201:
            raise AssertionError(f"会话 B 消息失败: {status} {sent}")
        draft = sent["requirement_draft"]
        status, _, confirmed = request_via(cls.server, "POST", path + "/requirements/confirm", {"base_revision_id": draft["revision_id"], "values": draft["values"]}, headers)
        if status != 201:
            raise AssertionError(f"会话 B 确认失败: {status} {confirmed}")
        status, _, recommendation = request_via(cls.server, "POST", path + "/recommendations", {"revision_id": confirmed["revision_id"]}, headers)
        if status != 201 or not recommendation.get("results"):
            raise AssertionError(f"会话 B 推荐失败: {status} {recommendation}")
        cls.expected_product_id = recommendation["results"][0]["product_id"]
        cls.expected_model = recommendation["results"][0]["model"]

        # 会话 C：用于「工程师追问 → 买家经输入框回复」与「实时同步/未读角标」——真实工程师链路
        status, _, created_c = request_via(cls.server, "POST", "/api/v1/buyer/conversations", {"site_key": "site-a"})
        if status != 201:
            raise AssertionError(f"创建会话 C 失败: {status} {created_c}")
        cls.task_conversation = created_c

        cls.result_path = Path(cls.temp_dir.name) / "reference_chat_ux_result.json"
        config_path = Path(cls.temp_dir.name) / "reference_chat_ux_config.json"
        config_path.write_text(json.dumps({
            "baseUrl": cls.base_url,
            "origin": cls.base_url,
            "siteKey": "site-a",
            "sessionKey": "pump_reference_session_site-a",
            "requirementConversationId": created["conversation_id"],
            "requirementToken": created["access_token"],
            "questionConversationId": created_b["conversation_id"],
            "questionToken": created_b["access_token"],
            "taskConversationId": created_c["conversation_id"],
            "taskToken": created_c["access_token"],
            "contextProductId": cls.expected_product_id,
            "contextProductModel": cls.expected_model,
            "engineerEmail": "demo-engineer@example.invalid",
            "engineerPassword": "demo-engineer",
        }, ensure_ascii=False), encoding="utf-8")
        completed = subprocess.run([NODE, str(HARNESS), str(config_path), str(cls.result_path)], capture_output=True, text=True, timeout=180)
        if completed.returncode != 0 or not cls.result_path.exists():
            detail = ""
            if cls.result_path.exists():
                try:
                    detail = cls.result_path.read_text(encoding="utf-8")[:2000]
                except OSError:
                    detail = ""
            raise AssertionError(f"浏览器驱动失败: rc={completed.returncode}\nresult={detail}\nstdout={completed.stdout}\nstderr={completed.stderr}")
        cls.results = json.loads(cls.result_path.read_text(encoding="utf-8"))
        if not cls.results.get("ok"):
            raise AssertionError(f"浏览器驱动异常: {cls.results.get('error')}")

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.db.conn.close()
        cls.thread.join()
        cls.temp_dir.cleanup()

    def test_greeting_follows_system_language_and_no_persistent_form(self):
        results = self.results["results"]
        self.assertIn("consulting assistant", results["greeting_en"]["logText"])
        self.assertNotIn("咨询助手", results["greeting_en"]["logText"])
        self.assertIn("咨询助手", results["greeting_zh"]["logText"])
        self.assertEqual(results["greeting_en"]["languageLabel"], "中文")
        self.assertEqual(results["greeting_zh"]["languageLabel"], "EN")
        # 手动切换语言后欢迎语随之切换
        self.assertIn("consulting assistant", results["after_toggle"]["logText"])
        # 悬浮启动按钮文案：按系统语言本地化，切换后随之切换
        self.assertEqual(results["greeting_en"]["launcherLabel"], "Ask me about products")
        self.assertEqual(results["greeting_zh"]["launcherLabel"], "向我提问产品问题")
        self.assertEqual(results["after_toggle"]["launcherLabel"], "Ask me about products")
        for key in ("greeting_en", "greeting_zh"):
            self.assertNotIn("Ask a product question", results[key]["logText"])
            self.assertNotIn("提出产品问题", results[key]["logText"])

    def test_unified_input_routes_requirement_and_question(self):
        results = self.results["results"]
        requirement = results["requirement_route"]
        messages_calls = [item for item in requirement["requests"] if item["url"].endswith("/messages")]
        question_calls = [item for item in requirement["requests"] if item["url"].endswith("/questions")]
        self.assertEqual(len(messages_calls), 1, requirement["requests"])
        self.assertEqual(len(question_calls), 0, requirement["requests"])
        self.assertEqual(messages_calls[0]["status"], 201)
        self.assertTrue(messages_calls[0]["responseBody"].get("requirement_draft"))
        self.assertIn("Check these extracted values", requirement["logText"])

        question = results["question_route"]
        question_calls = [item for item in question["requests"] if item["url"].endswith("/questions")]
        messages_calls = [item for item in question["requests"] if item["url"].endswith("/messages")]
        self.assertEqual(len(question_calls), 1, question["requests"])
        self.assertEqual(len(messages_calls), 0, question["requests"])
        self.assertEqual(question_calls[0]["status"], 201)
        # 提问自动绑定当前推荐型号
        self.assertEqual(question_calls[0]["requestBody"]["product_id"], self.expected_product_id)
        self.assertEqual(question_calls[0]["responseBody"]["status"], "grounded")
        self.assertTrue(question_calls[0]["responseBody"]["citations"])
        answer = question_calls[0]["responseBody"]["answer"]
        self.assertIn(answer[:20], question["logText"])
        # 回答卡点明对应型号（多候选时不混淆）
        self.assertIn(f"{self.expected_model} · Product answer", question["logText"])

    def test_waiting_task_reply_uses_main_input(self):
        results = self.results["results"]
        item = results["waiting_task_reply"]
        # 工程师追问经轮询自动同步进聊天（无需手动刷新），带「工程师追问」标签与输入框提示
        self.assertIn("Engineer question", item["afterFollowupText"])
        self.assertIn("Please confirm the flow and head", item["afterFollowupText"])
        self.assertIn("Reply using the message box below", item["afterFollowupText"])
        # 旧的独立回复表单已整体移除：聊天区内不再有任何输入框/按钮组件
        self.assertEqual(item["textareaCount"], 0, item["afterFollowupText"])
        self.assertNotIn("Reply to engineer", item["afterFollowupText"])
        # 买家经下方输入框回复 → 命中人工任务回复端点（而非 /messages 或 /questions）
        posts = [record for record in item["requests"] if record["method"] == "POST"]
        task_calls = [record for record in posts if f"/human-tasks/{item['taskId']}/responses" in record["url"]]
        self.assertEqual(len(task_calls), 1, item["requests"])
        self.assertEqual(task_calls[0]["status"], 201)
        self.assertEqual(task_calls[0]["requestBody"]["text"], "flow 50 m3/h, head 30 m")
        self.assertEqual([record for record in posts if record["url"].endswith("/messages")], [])
        self.assertEqual([record for record in posts if record["url"].endswith("/questions")], [])
        # 回复以买家气泡进入聊天，任务回到待处理（卡片状态文案同步更新）
        self.assertIn("flow 50 m3/h, head 30 m", item["afterReplyText"])
        self.assertIn("Engineer review requested", item["afterReplyText"])

    def test_needs_human_card_states_and_no_duplication(self):
        results = self.results["results"]
        item = results["waiting_task_reply"]
        # ① 不能回答时给出的是明确的选择入口：问句卡片 + 按钮；尚不显示「已转交」文案，也没有原始状态徽标
        self.assertIn("Would you like an engineer to verify this?", item["afterQuestionText"])
        self.assertIn("Ask an engineer to review", item["afterQuestionText"])
        self.assertNotIn("Engineer review requested", item["afterQuestionText"])
        self.assertNotIn("pending", item["afterQuestionText"])
        # 正文只出现一次（气泡陈述；选择卡不重复正文）
        self.assertEqual(item["afterQuestionText"].count("我还不能确认"), 1, item["afterQuestionText"])
        # ② 点击请求后：卡片整体消失，消息流出现一次「已转交」状态；请求按钮不再出现
        self.assertIn("Engineer review requested", item["requestedStateText"])
        self.assertEqual(item["requestedStateText"].count("Engineer review requested"), 1, item["requestedStateText"])
        self.assertNotIn("Ask an engineer to review", item["requestedStateText"])
        self.assertNotIn("Would you like an engineer to verify this?", item["requestedStateText"])
        # ③ 工程师追问只出现一次（消息气泡，含输入框提示）；卡片不再参与，追问文案不重复
        self.assertEqual(item["afterFollowupText"].count("Please confirm the flow and head"), 1, item["afterFollowupText"])
        self.assertIn("Reply using the message box below", item["afterFollowupText"])
        self.assertEqual(item["afterFollowupText"].count("Reply using the message box below"), 1, item["afterFollowupText"])
        self.assertNotIn("The engineer needs more details", item["afterFollowupText"])
        # 转交后产品问答卡不再显示（有据回答卡不受影响，见场景 4 断言）
        self.assertNotIn("Product answer", item["afterFollowupText"])
        # ④ 回答正文只出现一次：待核实回答不再以气泡重复卡片内容；有据回答同验
        self.assertEqual(item["afterFollowupText"].count("我还不能确认"), 1, item["afterFollowupText"])
        question = results["question_route"]
        answer = [record for record in question["requests"] if record["url"].endswith("/questions")][0]["responseBody"]["answer"]
        self.assertEqual(question["logText"].count(answer), 1, question["logText"])

    def test_realtime_sync_and_unread_badge(self):
        results = self.results["results"]
        item = results["auto_sync_and_badge"]
        # 面板打开时工程师答复自动出现（全程仅等待、无任何手动刷新操作）
        self.assertTrue(item["autoReplyVisible"], item["reopenedText"])
        self.assertTrue(item["engineerLabelVisible"], item["reopenedText"])
        # 期间确有轮询 GET 在跑（自动同步机制）
        self.assertTrue(any(record["method"] == "GET" and "/conversations/" in record["url"] for record in item["autoSyncRequests"]), item["autoSyncRequests"])
        # 面板收起时出现未读角标（数字=新进消息数），面板本身仍是收起状态
        badge = item["badgeWhileClosed"]
        self.assertFalse(badge["hidden"])
        self.assertEqual(badge["text"], "1")
        self.assertTrue(badge["panelHidden"])
        # 重新打开：角标清空、回复内容可见（标签「Engineer reply」呈现，正文不再带中文前缀）
        self.assertTrue(item["badgeAfterOpen"]["hidden"])
        self.assertIn("6-8 weeks", item["reopenedText"])
        self.assertNotIn("工程师回复：", item["reopenedText"])
        self.assertIn("Engineer reply", item["reopenedText"])

    def test_product_page_context_defaults_model_and_connects(self):
        results = self.results["results"]
        item = results["product_page_context"]
        # 打开聊天窗即自动连接（无需手动点连接）
        self.assertTrue(item["connected"], item)
        # 询价表单默认带入当前详情页型号
        self.assertEqual(item["modelPrefill"], self.expected_model)
        # 提交的询价绑定该型号（product_id + 型号文字同行提交）
        rfq_posts = [record for record in item["rfqRequests"] if record["method"] == "POST" and record["url"].endswith("/rfqs")]
        self.assertEqual(len(rfq_posts), 1, item["rfqRequests"])
        self.assertEqual(rfq_posts[0]["requestBody"]["product_id"], self.expected_product_id)
        self.assertEqual(rfq_posts[0]["requestBody"]["requested_model"], self.expected_model)
        # 提问同样默认绑定当前详情页型号，并得到有据回答
        question_posts = [record for record in item["questionRequests"] if record["method"] == "POST" and record["url"].endswith("/questions")]
        self.assertEqual(len(question_posts), 1, item["questionRequests"])
        self.assertEqual(question_posts[0]["requestBody"]["product_id"], self.expected_product_id)
        self.assertEqual(question_posts[0]["responseBody"]["status"], "grounded")

    def test_product_page_carries_chat_widget(self):
        product = self.db.list_products("demo-tenant")[0]
        status, _, page = request_via(self.server, "GET", f"/s/site-a/products/{product['slug']}")
        self.assertEqual(status, 200)
        for marker in ("chat-launcher", "chat-launcher-label", "PUMP_CHAT_CONTEXT", product["id"], product["model"], "/reference-client/app.js", "/reference-client/chat.css", "chat-panel", "/reference-client/product-page.js"):
            self.assertIn(marker, page)
        # 静态资源路由必须真的能服务聊天样式（历史坑：白名单漏文件 → 404 → 样式全丢）
        for asset in ("/reference-client/chat.css", "/reference-client/style.css", "/reference-client/app.js", "/reference-client/product-page.js", "/reference-client/product-page.css"):
            asset_status, _, _ = request_via(self.server, "GET", asset)
            self.assertEqual(asset_status, 200, asset)


if __name__ == "__main__":
    unittest.main()
