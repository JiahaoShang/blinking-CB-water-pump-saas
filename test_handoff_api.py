"""跨站会话交接（一次性交接码）的 HTTP 测试。

覆盖验收要求：两个不同来源（不同端口）下由官网聊天打开产品页后，
详情页 RFQ 仍写入原会话；交接码一次性、短时、绑定站点/型号/企业；
来源边界拒绝且不返回跨域放行头；过期/重放/错绑不发放凭据；
直访者可建立自己的会话；全程不按邮箱关联旧会话。
"""
import http.client
import json
import tempfile
import threading
import unittest
from pathlib import Path

from app import make_server


def approve_all_fields(db, tenant_id, product_id):
    """Simulate an admin who individually approved every sourced candidate field."""
    for field in db.get_fields(tenant_id, product_id):
        if field["state"] == "candidate":
            db.approve_field(tenant_id, product_id, field["field_key"])
    db.finalize_product_review(tenant_id, product_id)


class HandoffApiTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "app.db"
        self.server = make_server(self.db_path, port=0)
        self.db = self.server.RequestHandlerClass.db
        self.db.seed_demo_users()
        self.db.ensure_tenant("second-tenant")
        self.db.register_buyer_site("demo-tenant", "site-a", "https://seller-a.example")
        self.db.register_buyer_site("second-tenant", "site-b", "https://seller-b.example")
        self.db.seed_demo_products("demo-tenant")
        self.products = self.db.list_products("demo-tenant")
        self.assertGreaterEqual(len(self.products), 2)
        for product in self.products:
            approve_all_fields(self.db, "demo-tenant", product["id"])
            self.db.publish_product("demo-tenant", product["id"])
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.server2 = None
        self.thread2 = None

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        if self.server2:
            self.server2.shutdown()
            self.server2.server_close()
            self.server2.RequestHandlerClass.db.conn.close()
        self.db.conn.close()
        self.thread.join()
        if self.thread2:
            self.thread2.join()
        self.temp_dir.cleanup()

    def request_via(self, server, method, path, payload=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", server.server_port)
        request_headers = {"Content-Type": "application/json", **(headers or {})}
        conn.request(method, path, json.dumps(payload).encode() if payload is not None else None, request_headers)
        response = conn.getresponse()
        data = response.read()
        result = response.status, dict(response.getheaders()), json.loads(data) if data else None
        conn.close()
        return result

    def request(self, method, path, payload=None, headers=None):
        return self.request_via(self.server, method, path, payload, headers)

    def start_second_server(self):
        self.server2 = make_server(self.db_path, port=0)
        self.thread2 = threading.Thread(target=self.server2.serve_forever, daemon=True)
        self.thread2.start()
        return self.server2

    def platform_origin(self, server=None):
        return f"http://127.0.0.1:{(server or self.server).server_port}"

    def create_conversation(self, server=None, site="site-a", origin="https://seller-a.example"):
        server = server or self.server
        status, _, created = self.request_via(server, "POST", "/api/v1/buyer/conversations", {"site_key": site}, {"Origin": origin})
        self.assertEqual(status, 201)
        return created["conversation_id"], created["access_token"]

    def mint_code(self, conversation_id, token, product_id, site="site-a", origin="https://seller-a.example", server=None):
        server = server or self.server
        status, _, minted = self.request_via(
            server, "POST", f"/api/v1/buyer/conversations/{conversation_id}/handoff-codes", {"product_id": product_id},
            {"X-Site-Key": site, "Authorization": f"Bearer {token}", "Origin": origin},
        )
        self.assertEqual(status, 201, minted)
        return minted["code"]

    def exchange(self, code, product_id, site="site-a", origin=None, server=None):
        server = server or self.server
        headers = {"X-Site-Key": site}
        if origin is not None:
            headers["Origin"] = origin
        return self.request_via(server, "POST", "/api/v1/buyer/handoff/exchange", {"code": code, "product_id": product_id}, headers)

    def conversation_get(self, conversation_id, token, server=None, site="site-a"):
        return self.request_via(server or self.server, "GET", f"/api/v1/buyer/conversations/{conversation_id}", None,
                                {"X-Site-Key": site, "Authorization": f"Bearer {token}"})

    def test_handoff_exchange_keeps_original_chat_and_page_rfq_joins_conversation(self):
        product = self.products[0]
        conversation_id, token1 = self.create_conversation()
        code = self.mint_code(conversation_id, token1, product["id"])

        status, _, exchanged = self.exchange(code, product["id"], origin=self.platform_origin())
        self.assertEqual(status, 200)
        self.assertEqual(exchanged["conversation_id"], conversation_id)
        self.assertNotEqual(exchanged["access_token"], token1)
        self.assertNotEqual(exchanged["access_token"], code)
        token2 = exchanged["access_token"]

        # 原聊天凭据不受影响，可继续读写原会话
        self.assertEqual(self.conversation_get(conversation_id, token1)[0], 200)
        # 换取的凭据同样有效，并可用于详情页 RFQ（entry_surface=product_page）
        self.assertEqual(self.conversation_get(conversation_id, token2)[0], 200)
        status, _, saved = self.request_via(
            self.server, "POST", f"/api/v1/buyer/conversations/{conversation_id}/rfqs",
            {"entry_surface": "product_page", "product_id": product["id"], "contact_confirmed": True,
             "contact": {"contact_email": "buyer@example.com", "quantity": "2", "region": "DE"}, "notes": "handoff"},
            {"X-Site-Key": "site-a", "Authorization": f"Bearer {token2}", "Idempotency-Key": "handoff-rfq-1"},
        )
        self.assertEqual(status, 201)
        # 详情页底部表单提交的 RFQ 必须绑定当页具体产品（型号随产品冻结进快照，销售侧据此识别）
        saved_row = self.db.get_rfq("demo-tenant", saved["rfq_id"])
        self.assertEqual(saved_row["product_id"], product["id"])
        self.assertEqual(json.loads(saved_row["snapshot_json"])["recommendation"]["model"], product["model"])
        self.assertEqual(json.loads(saved_row["snapshot_json"])["entry_surface"], "product_page")
        # 原聊天刷新后看到同一 RFQ
        _, _, current = self.conversation_get(conversation_id, token1)
        self.assertEqual([item["id"] for item in current["rfqs"]], [saved["rfq_id"]])
        # 无凭据仍读不到私有会话
        self.assertEqual(self.request("GET", f"/api/v1/buyer/conversations/{conversation_id}", headers={"X-Site-Key": "site-a"})[0], 401)

        # 产品页包含交接状态元素与 no-referrer 防护
        conn = http.client.HTTPConnection("127.0.0.1", self.server.server_port)
        conn.request("GET", f"/s/site-a/products/{product['slug']}")
        response = conn.getresponse()
        html = response.read().decode()
        conn.close()
        self.assertEqual(response.status, 200)
        self.assertIn("product-rfq-session", html)
        self.assertIn("no-referrer", html)

    def test_handoff_code_single_use_replay_and_expiry(self):
        product = self.products[0]
        conversation_id, token1 = self.create_conversation()
        code = self.mint_code(conversation_id, token1, product["id"])
        status, _, exchanged = self.exchange(code, product["id"], origin=self.platform_origin())
        self.assertEqual(status, 200)
        token2 = exchanged["access_token"]
        self.assertEqual(self.db.conn.execute(
            "SELECT COUNT(*) FROM buyer_conversation_tokens WHERE conversation_id=?", (conversation_id,)
        ).fetchone()[0], 1)

        # 重放：同一交接码再次交换被拒，且不影响已发凭据
        status, _, replay = self.exchange(code, product["id"], origin=self.platform_origin())
        self.assertEqual(status, 409)
        self.assertEqual(replay["error"], "handoff_already_used")
        self.assertNotIn("access_token", replay)
        self.assertEqual(self.db.conn.execute(
            "SELECT COUNT(*) FROM buyer_conversation_tokens WHERE conversation_id=?", (conversation_id,)
        ).fetchone()[0], 1)
        self.assertEqual(self.conversation_get(conversation_id, token1)[0], 200)
        self.assertEqual(self.conversation_get(conversation_id, token2)[0], 200)

        # 过期：过期的交接码被拒，不发凭据
        expired_code = self.mint_code(conversation_id, token1, product["id"])
        self.db.conn.execute("UPDATE buyer_handoff_codes SET expires_at='2000-01-01T00:00:00+00:00' WHERE code_hash IS NOT NULL AND used_at IS NULL")
        self.db.conn.commit()
        status, _, expired = self.exchange(expired_code, product["id"], origin=self.platform_origin())
        self.assertEqual(status, 410)
        self.assertEqual(expired["error"], "handoff_expired")
        self.assertNotIn("access_token", expired)
        self.assertEqual(self.db.conn.execute(
            "SELECT COUNT(*) FROM buyer_conversation_tokens WHERE conversation_id=?", (conversation_id,)
        ).fetchone()[0], 1)

        # 未知码
        status, _, unknown = self.exchange("not-a-real-code", product["id"], origin=self.platform_origin())
        self.assertEqual(status, 404)
        self.assertEqual(unknown["error"], "handoff_not_found")

    def test_handoff_binding_and_origin_boundaries(self):
        product, other_product = self.products[0], self.products[1]
        conversation_id, token1 = self.create_conversation()
        code = self.mint_code(conversation_id, token1, product["id"])

        # 陌生来源：交换被拒且不返回跨域放行头；且这次拒绝不消费交接码
        status, headers, denied = self.exchange(code, product["id"], origin="https://evil.example")
        self.assertEqual(status, 403)
        self.assertEqual(denied["error"], "origin_not_allowed")
        self.assertNotIn("Access-Control-Allow-Origin", headers)

        # 正确来源仍可交换（证明拒绝未消费）
        status, _, exchanged = self.exchange(code, product["id"], origin=self.platform_origin())
        self.assertEqual(status, 200)
        token2 = exchanged["access_token"]

        # 站点/企业不符：site-b 与 demo-tenant 的型号组合被拒
        code2 = self.mint_code(conversation_id, token1, product["id"])
        status, _, mismatched = self.exchange(code2, product["id"], site="site-b", origin=self.platform_origin())
        self.assertEqual(status, 403)
        self.assertEqual(mismatched["error"], "handoff_binding_mismatch")
        # 同企业不同型号（已发布）被拒
        status, _, wrong_product = self.exchange(code2, other_product["id"], origin=self.platform_origin())
        self.assertEqual(status, 403)
        self.assertEqual(wrong_product["error"], "handoff_binding_mismatch")
        # 未知型号被拒
        status, _, unknown_product = self.exchange(code2, "0" * 24, origin=self.platform_origin())
        self.assertEqual(status, 403)
        # 绑定不符不消费：正确交换仍然成功
        status, _, exchanged2 = self.exchange(code2, product["id"], origin=self.platform_origin())
        self.assertEqual(status, 200)
        self.assertEqual(self.db.conn.execute(
            "SELECT COUNT(*) FROM buyer_conversation_tokens WHERE conversation_id=?", (conversation_id,)
        ).fetchone()[0], 2)

        # 未经登记来源不能出具交接码（沿用网站来源校验）
        status, _, blocked = self.request(
            "POST", f"/api/v1/buyer/conversations/{conversation_id}/handoff-codes", {"product_id": product["id"]},
            {"X-Site-Key": "site-a", "Authorization": f"Bearer {token1}", "Origin": "https://evil.example"},
        )
        self.assertEqual(status, 403)
        self.assertEqual(blocked["error"], "origin_not_allowed")

        # 跨企业凭据不可读另一站点会话：site-b 的凭据/会话隔离保持
        second_conversation, second_token = self.create_conversation(site="site-b", origin="https://seller-b.example")
        self.assertEqual(self.request("GET", f"/api/v1/buyer/conversations/{second_conversation}", headers={"X-Site-Key": "site-b", "Authorization": f"Bearer {token2}"})[0], 401)

    def test_mint_requires_authorized_conversation_and_published_product(self):
        product = self.products[0]
        conversation_id, token1 = self.create_conversation()
        path = f"/api/v1/buyer/conversations/{conversation_id}/handoff-codes"
        # 无凭据
        self.assertEqual(self.request("POST", path, {"product_id": product["id"]}, {"X-Site-Key": "site-a"})[0], 401)
        # 缺少型号参数
        status, _, body = self.request("POST", path, {}, {"X-Site-Key": "site-a", "Authorization": f"Bearer {token1}"})
        self.assertEqual(status, 422)
        self.assertEqual(body["error"], "product_required")
        # 未发布型号
        draft_id = self.db.create_product("demo-tenant", "DRAFT-H1", "未发布样品泵", "联调")
        status, _, body = self.request("POST", path, {"product_id": draft_id}, {"X-Site-Key": "site-a", "Authorization": f"Bearer {token1}"})
        self.assertEqual(status, 404)
        self.assertEqual(body["error"], "product_not_found")
        # 未知型号
        self.assertEqual(self.request("POST", path, {"product_id": "f" * 24}, {"X-Site-Key": "site-a", "Authorization": f"Bearer {token1}"})[0], 404)
        # 会话不存在
        self.assertEqual(self.request("POST", "/api/v1/buyer/conversations/" + "f" * 24 + "/handoff-codes", {"product_id": product["id"]}, {"X-Site-Key": "site-a", "Authorization": f"Bearer {token1}"})[0], 404)

    def test_two_server_origins_share_conversation_via_handoff(self):
        """不同端口=不同浏览器来源：聊天在平台实例 A 建会话，产品页在实例 B 交接后提交 RFQ。"""
        product = self.products[0]
        platform = self.start_second_server()
        # 官网聊天（登记来源 seller-a）跨域调用平台实例
        conversation_id, token1 = self.create_conversation(server=platform)
        code = self.mint_code(conversation_id, token1, product["id"], server=platform)
        # 产品页在平台实例自身来源上交换
        status, _, exchanged = self.exchange(code, product["id"], origin=self.platform_origin(platform), server=platform)
        self.assertEqual(status, 200)
        self.assertEqual(exchanged["conversation_id"], conversation_id)
        token2 = exchanged["access_token"]
        # 另一实例（同库多进程/多来源部署）也接受两枚凭据
        self.assertEqual(self.conversation_get(conversation_id, token1, server=self.server)[0], 200)
        self.assertEqual(self.conversation_get(conversation_id, token2, server=self.server)[0], 200)
        # 详情页 RFQ 经实例 B 写入原会话；原聊天经实例 A 也能看到
        status, _, saved = self.request_via(
            platform, "POST", f"/api/v1/buyer/conversations/{conversation_id}/rfqs",
            {"entry_surface": "product_page", "product_id": product["id"], "contact_confirmed": True,
             "contact": {"contact_phone": "+4900000001"}, "notes": "cross-origin"},
            {"X-Site-Key": "site-a", "Authorization": f"Bearer {token2}", "Idempotency-Key": "xc-rfq-1"},
        )
        self.assertEqual(status, 201)
        _, _, current = self.conversation_get(conversation_id, token1)
        self.assertEqual([item["id"] for item in current["rfqs"]], [saved["rfq_id"]])

    def test_direct_visitor_and_same_email_never_reassociate(self):
        first, token1 = self.create_conversation()
        second, token2 = self.create_conversation()
        self.assertNotEqual(first, second)
        for conversation_id, token in ((first, token1), (second, token2)):
            status, _, _ = self.request(
                "POST", f"/api/v1/buyer/conversations/{conversation_id}/rfqs",
                {"entry_surface": "product_page", "contact_confirmed": True,
                 "contact": {"contact_email": "same@example.com", "quantity": "1", "region": "FR"}, "notes": "direct"},
                {"X-Site-Key": "site-a", "Authorization": f"Bearer {token}", "Idempotency-Key": f"direct-{conversation_id}"},
            )
            self.assertEqual(status, 201)
        # 同一邮箱两条独立 RFQ 分属两个会话，互不合并
        self.assertEqual(self.db.conn.execute("SELECT COUNT(*) FROM rfqs WHERE tenant_id='demo-tenant'").fetchone()[0], 2)
        # 交接码只回到码绑定会话（即使另一会话更晚创建）；跨会话凭据互不可用
        product = self.products[0]
        code = self.mint_code(first, token1, product["id"])
        status, _, exchanged = self.exchange(code, product["id"], origin=self.platform_origin())
        self.assertEqual(status, 200)
        self.assertEqual(exchanged["conversation_id"], first)
        self.assertEqual(self.request("GET", f"/api/v1/buyer/conversations/{second}", headers={"X-Site-Key": "site-a", "Authorization": f"Bearer {exchanged['access_token']}"})[0], 401)

    def test_inactive_site_cannot_mint_or_exchange(self):
        product = self.products[0]
        conversation_id, token1 = self.create_conversation()
        code = self.mint_code(conversation_id, token1, product["id"])
        self.db.conn.execute("UPDATE buyer_sites SET active=0 WHERE site_key='site-a'")
        self.db.conn.commit()
        status, _, body = self.exchange(code, product["id"], origin=self.platform_origin())
        self.assertEqual(status, 404)
        self.assertEqual(body["error"], "site_not_found")


if __name__ == "__main__":
    unittest.main()
