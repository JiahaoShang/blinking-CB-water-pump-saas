"""产品页交接逻辑的浏览器端回归测试（Node 加载真实 product-page.js + 真实 HTTP/数据库）。

针对 Codex 独立浏览器审计（docs/独立审计-2026-10-03.md「跨来源交接的独立浏览器复验」）
复现的阻断缺陷：平台来源已保存原会话时，带无效交接码的产品页在兑换失败后仍复用浏览器
已保存的旧会话，第二位买家的 RFQ 被并入旧会话。此处用 Node 直接加载未经修改的
reference-client/product-page.js（vm + localStorage/sessionStorage/DOM/fetch 桩）按时间
顺序驱动五类场景，并对照真实服务与数据库断言：

- 成功交接：凭据写入平台来源 localStorage（同源兼容），页内 RFQ 并入原会话；
- 失败交接：绝不复用已存会话，提交时建立独立新会话（只存本标签页 sessionStorage），
  且整个流程不存在任何触达旧会话的请求；
- 并存标签页（无码直访）：继续使用已保存会话，不会写入失败交接的新会话；
- 失败标签页刷新（失败标记仍在）：继续以独立新访客处理，仍使用自己已建立的新会话；
- 全新浏览器无码直访：保持既有行为（提交时新建会话并保存到 localStorage）。

场景由 tools/browser_tests/product_page_handoff_browser.mjs 驱动（同一测试类内一次运行、
五个测试方法分别断言）；依赖 Node >= 18（全局 fetch）。Node 不可用或版本过低时整体跳过，
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

from app import make_server

ROOT = Path(__file__).resolve().parent
HARNESS = ROOT / "tools" / "browser_tests" / "product_page_handoff_browser.mjs"
NODE = shutil.which("node")
SESSION_KEY = "pump_reference_session_site-a"
FAILED_KEY = "pump_product_handoff_failed_site-a"
ISOLATED_KEY = "pump_product_handoff_session_site-a"


def node_major_version(path: str) -> int:
    try:
        output = subprocess.run([path, "--version"], capture_output=True, text=True, check=True, timeout=30).stdout.strip()
        match = re.match(r"v(\d+)", output)
        return int(match.group(1)) if match else 0
    except Exception:
        return 0


def approve_all_fields(db, tenant_id, product_id):
    """Simulate an admin who individually approved every sourced candidate field."""
    for field in db.get_fields(tenant_id, product_id):
        if field["state"] == "candidate":
            db.approve_field(tenant_id, product_id, field["field_key"])
    db.finalize_product_review(tenant_id, product_id)


class ProductPageHandoffBrowserTests(unittest.TestCase):
    maxDiff = None

    @classmethod
    def setUpClass(cls):
        if NODE is None:
            raise unittest.SkipTest("未找到 node：产品页浏览器逻辑回归测试跳过（未验证）")
        if node_major_version(NODE) < 18:
            raise unittest.SkipTest("node 版本过低（需要 >= 18 的全局 fetch）：产品页浏览器逻辑回归测试跳过（未验证）")
        cls.temp_dir = tempfile.TemporaryDirectory()
        cls.db_path = Path(cls.temp_dir.name) / "app.db"
        cls.server = make_server(cls.db_path, port=0)
        cls.db = cls.server.RequestHandlerClass.db
        cls.db.seed_demo_users()
        cls.db.register_buyer_site("demo-tenant", "site-a", "https://seller-a.example")
        cls.db.seed_demo_products("demo-tenant")
        cls.products = cls.db.list_products("demo-tenant")
        if len(cls.products) < 2:
            raise AssertionError("演示种子不足两个产品，无法覆盖两个型号页面")
        for product in cls.products:
            approve_all_fields(cls.db, "demo-tenant", product["id"])
            cls.db.publish_product("demo-tenant", product["id"])
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base_url = f"http://127.0.0.1:{cls.server.server_port}"

        # 原会话与有效交接码（跨来源聊天的角色由 Python 侧 HTTP 直接扮演）
        status, _, created = cls.request_via(
            "POST", "/api/v1/buyer/conversations", {"site_key": "site-a"}, {"Origin": "https://seller-a.example"}
        )
        if status != 201:
            raise AssertionError(f"创建原会话失败: {status} {created}")
        cls.conversation_id = created["conversation_id"]
        cls.conversation_token = created["access_token"]
        cls.product_t1, cls.product_t2 = cls.products[0], cls.products[1]
        status, _, minted = cls.request_via(
            "POST", f"/api/v1/buyer/conversations/{cls.conversation_id}/handoff-codes", {"product_id": cls.product_t1["id"]},
            {"X-Site-Key": "site-a", "Authorization": f"Bearer {cls.conversation_token}", "Origin": "https://seller-a.example"},
        )
        if status != 201:
            raise AssertionError(f"出具交接码失败: {status} {minted}")
        cls.handoff_code = minted["code"]

        config = {
            "baseUrl": cls.base_url,
            "siteKey": "site-a",
            "products": {
                "t1": {"id": cls.product_t1["id"], "slug": cls.product_t1["slug"]},
                "t2": {"id": cls.product_t2["id"], "slug": cls.product_t2["slug"]},
            },
            "handoffCode": cls.handoff_code,
            "invalidCode": "invalid-audit-code",
        }
        config_path = Path(cls.temp_dir.name) / "config.json"
        result_path = Path(cls.temp_dir.name) / "result.json"
        config_path.write_text(json.dumps(config), encoding="utf-8")
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
            raise AssertionError(f"node 场景驱动未完成全部场景: {json.dumps(cls.result)[:4000]}")

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

    def rfq_posts(self, scenario):
        posts = []
        for entry in scenario["requests"]:
            match = re.fullmatch(r"/api/v1/buyer/conversations/([a-f0-9]+)/rfqs", entry["url"])
            if entry["method"] == "POST" and match:
                body = entry.get("body") or {}
                posts.append({"conversation_id": match.group(1), "rfq_id": body.get("rfq_id"), "idempotency_key": entry.get("idempotencyKey")})
        return posts

    def conversation_rfqs(self, conversation_id, token):
        status, _, body = self.request_via(
            "GET", f"/api/v1/buyer/conversations/{conversation_id}", None,
            {"X-Site-Key": "site-a", "Authorization": f"Bearer {token}"},
        )
        self.assertEqual(status, 200, body)
        return [item["id"] for item in body["rfqs"]]

    def isolated_session(self, scenario):
        return json.loads(scenario["afterSubmit"]["sessionStorage"][ISOLATED_KEY])

    # ---- 五个场景断言 -------------------------------------------------------

    def test_success_handoff_rejoins_original_conversation(self):
        """成功交接：凭据写入 localStorage，页内 RFQ 进入原会话，提示为已连接。"""
        s1 = self.scenario("success_handoff")
        self.assertIn("Connected to your original conversation", s1["afterLoad"]["statusText"])
        self.assertEqual(s1["afterLoad"]["url"]["hash"], "", "交接码应在读取后立即从地址栏清除")
        stored = json.loads(s1["afterLoad"]["localStorage"][SESSION_KEY])
        self.assertEqual(stored["conversationId"], self.conversation_id)
        posts = self.rfq_posts(s1)
        self.assertEqual(len(posts), 1)
        self.assertEqual(posts[0]["conversation_id"], self.conversation_id)
        self.assertTrue(posts[0]["rfq_id"])
        self.assertTrue(posts[0]["idempotency_key"], "RFQ 提交仍应携带幂等键")
        self.assertIn(posts[0]["rfq_id"], self.conversation_rfqs(self.conversation_id, self.conversation_token))
        self.assertIn("received", s1["afterSubmit"]["resultText"])
        self.assertNotIn("independent conversation", s1["afterSubmit"]["resultText"])

    def test_failed_handoff_never_reuses_saved_session(self):
        """失败交接：不读旧会话、建立独立新会话；提示如实、不承诺接回。"""
        s1 = self.scenario("success_handoff")
        s2 = self.scenario("failed_handoff_isolated")
        status_text = s2["afterLoad"]["statusText"]
        self.assertIn("could not reconnect the original conversation", status_text)
        self.assertIn("new, independent conversation", status_text)
        self.assertNotIn("Connected to your original conversation", status_text)
        # 旧实现曾显示“continuing with this browser's saved session”——该承诺必须消失
        self.assertNotIn("saved session", status_text)
        # 失败标记与独立会话只在本标签页 sessionStorage；localStorage 中原会话保持不变
        self.assertEqual(s2["afterLoad"]["sessionStorage"][FAILED_KEY], "1")
        self.assertEqual(s2["afterLoad"]["localStorage"], s1["afterSubmit"]["localStorage"])
        posts = self.rfq_posts(s2)
        self.assertEqual(len(posts), 1)
        self.assertNotEqual(posts[0]["conversation_id"], self.conversation_id)
        # 整个失败流程不存在任何触达旧会话的请求（绝不复用的直接证据）
        self.assertFalse([entry for entry in s2["requests"] if self.conversation_id in entry["url"]])
        exchange = [entry for entry in s2["requests"] if entry["url"].endswith("/handoff/exchange")]
        self.assertEqual([entry["status"] for entry in exchange], [404])
        isolated = self.isolated_session(s2)
        self.assertNotEqual(isolated["conversationId"], self.conversation_id)
        self.assertEqual(posts[0]["conversation_id"], isolated["conversationId"])
        # 失败交接的 RFQ 不在原会话（最终状态：原会话=成功路径+并存标签页写入），
        # 而是落在独立新会话中
        rfq2 = posts[0]["rfq_id"]
        self.assertNotIn(rfq2, self.conversation_rfqs(self.conversation_id, self.conversation_token))
        self.assertIn(rfq2, self.conversation_rfqs(isolated["conversationId"], isolated["token"]))
        self.assertIn("new, independent conversation", s2["afterSubmit"]["resultText"])
        self.assertNotIn("existing conversation", s2["afterSubmit"]["resultText"])

    def test_coexisting_tab_writes_to_its_own_session_not_new_one(self):
        """并存标签页（无码直访）继续使用已保存会话；不会写入失败交接的新会话。"""
        s1 = self.scenario("success_handoff")
        s2 = self.scenario("failed_handoff_isolated")
        s3 = self.scenario("coexisting_direct_tab")
        self.assertEqual(s3["afterLoad"]["statusText"], "", "无码直访不应显示失败交接提示")
        posts = self.rfq_posts(s3)
        self.assertEqual(len(posts), 1)
        self.assertEqual(posts[0]["conversation_id"], self.conversation_id)
        rfq1 = self.rfq_posts(s1)[0]["rfq_id"]
        rfq3 = posts[0]["rfq_id"]
        self.assertEqual(set(self.conversation_rfqs(self.conversation_id, self.conversation_token)), {rfq1, rfq3})
        isolated = self.isolated_session(s2)
        isolated_rfqs = self.conversation_rfqs(isolated["conversationId"], isolated["token"])
        self.assertNotIn(rfq3, isolated_rfqs, "并存标签页的 RFQ 不得写入失败交接的新会话")
        self.assertNotIn(rfq1, isolated_rfqs)

    def test_failed_tab_reload_continues_isolated(self):
        """失败标签页刷新后继续以独立新访客处理；仍写入自己的新会话，不碰原会话。"""
        s2 = self.scenario("failed_handoff_isolated")
        s4 = self.scenario("failed_tab_reload")
        self.assertIn("continues as a new visitor", s4["afterLoad"]["statusText"])
        posts = self.rfq_posts(s4)
        self.assertEqual(len(posts), 1)
        isolated = self.isolated_session(s2)
        self.assertEqual(posts[0]["conversation_id"], isolated["conversationId"])
        self.assertNotEqual(posts[0]["conversation_id"], self.conversation_id)
        self.assertFalse([entry for entry in s4["requests"] if self.conversation_id in entry["url"]])
        rfq4 = posts[0]["rfq_id"]
        self.assertIn(rfq4, self.conversation_rfqs(isolated["conversationId"], isolated["token"]))
        # 原会话保持成功路径与并存标签页的两份 RFQ，未被失败标签页写入
        self.assertNotIn(rfq4, self.conversation_rfqs(self.conversation_id, self.conversation_token))
        self.assertEqual(len(self.conversation_rfqs(self.conversation_id, self.conversation_token)), 2)

    def test_no_code_direct_visit_keeps_existing_behavior(self):
        """全新浏览器无码直访：提交时新建会话并保存到 localStorage（既有行为不变）。"""
        s2 = self.scenario("failed_handoff_isolated")
        s5 = self.scenario("fresh_direct_visit")
        self.assertEqual(s5["afterLoad"]["statusText"], "")
        self.assertEqual(s5["afterLoad"]["localStorage"], {}, "全新浏览器载入时不应凭空产生会话")
        posts = self.rfq_posts(s5)
        self.assertEqual(len(posts), 1)
        stored = json.loads(s5["afterSubmit"]["localStorage"][SESSION_KEY])
        self.assertEqual(stored["conversationId"], posts[0]["conversation_id"])
        self.assertNotEqual(stored["conversationId"], self.conversation_id)
        self.assertNotEqual(stored["conversationId"], self.isolated_session(s2)["conversationId"])
        self.assertEqual(self.conversation_rfqs(stored["conversationId"], stored["token"]), [posts[0]["rfq_id"]])


if __name__ == "__main__":
    unittest.main()
