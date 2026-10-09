"""第二波 · Dify 知识库同步测试（已核准资料 → 知识库；幂等 / 撤回删除 / 来源禁用不泄漏）。

契约：
- 已核准且已发布的产品资料整理成文档（跳过来源被禁用的字段）；
- 首次同步创建；内容未变跳过；force 时更新；产品撤回/不再发布 → 删除知识库文档；
- 未配置（缺 DATASET_ID / API_KEY）→ 静默跳过（configured=False），不发任何请求；
- 管理员在渠道页一键触发（POST /admin/channels/sync-dify-kb）。
"""
import json
import os
import sys
import tempfile
import threading
import unittest
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from app import collect_kb_documents, make_server, sync_kb_to_dify  # noqa: E402
from test_langbot_bridge import form_request, login  # noqa: E402


class _DifyKBMock:
    """最小 Dify 数据集服务模拟：create/update/delete 全记录。"""

    def __init__(self, api_key="kb-test-key"):
        state = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length).decode("utf-8")
                try:
                    body = json.loads(raw) if raw else None
                except json.JSONDecodeError:
                    body = {"raw": raw}
                state.calls.append({"method": "POST", "path": self.path, "body": body, "auth": self.headers.get("Authorization")})
                path = urllib.parse.urlparse(self.path).path
                if path.endswith("/document/create-by-text"):
                    state.created += 1
                    payload = {"document": {"id": f"doc-{state.created}"}, "batch": f"batch-{state.created}"}
                elif path.endswith("/update-by-text"):
                    payload = {"result": "success"}
                else:
                    payload = {"message": "not found"}
                data = json.dumps(payload).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_DELETE(self):
                state.calls.append({"method": "DELETE", "path": self.path, "body": None, "auth": self.headers.get("Authorization")})
                self.send_response(204)
                self.send_header("Content-Length", "0")
                self.end_headers()

        self.calls = []
        self.created = 0
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self._server.server_address[1]}"
        threading.Thread(target=self._server.serve_forever, daemon=True).start()

    def stop(self):
        self._server.shutdown()
        self._server.server_close()

    def create_calls(self):
        return [c for c in self.calls if c["method"] == "POST" and c["path"].endswith("create-by-text")]

    def update_calls(self):
        return [c for c in self.calls if c["method"] == "POST" and c["path"].endswith("update-by-text")]

    def delete_calls(self):
        return [c for c in self.calls if c["method"] == "DELETE"]


class DifyKBSyncTests(unittest.TestCase):
    maxDiff = None

    @classmethod
    def setUpClass(cls):
        cls.temp_dir = tempfile.TemporaryDirectory()
        cls.db_path = Path(cls.temp_dir.name) / "app.db"
        cls.kb = _DifyKBMock()
        cls.env_backup = {name: os.environ.get(name) for name in ("DIFY_KB_URL", "DIFY_KB_DATASET_ID", "DIFY_KB_API_KEY")}
        os.environ["DIFY_KB_URL"] = cls.kb.url
        os.environ["DIFY_KB_DATASET_ID"] = "ds-777"
        os.environ["DIFY_KB_API_KEY"] = "kb-test-key"
        cls.server = make_server(cls.db_path, port=0)
        cls.db = cls.server.RequestHandlerClass.db
        cls.db.seed_demo_users()
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.admin = login(cls.server, "demo-admin@example.invalid", "demo-admin")

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.kb.stop()
        cls.temp_dir.cleanup()
        for name, value in cls.env_backup.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value

    # ── 辅助 ──────────────────────────────────────────────────────────────
    def _make_product(self, model):
        pid = self.db.create_product("demo-tenant", model, f"{model} 泵", "测试用途", "demo-admin")
        sid = self.db.add_source(
            "demo-tenant", pid, f"{model}-a.txt", "TXT", "p1", "流量范围: 20-80 m³/h\n".encode(), "demo-admin",
            scope="exact_model",
        )
        self.db.approve_field("demo-tenant", pid, "flow_range")
        self.db.finalize_product_review("demo-tenant", pid)
        self.db.allow_public_source("demo-tenant", pid, sid, "demo-admin")
        self.db.publish_product("demo-tenant", pid)
        return pid, sid

    # ── 用例 ──────────────────────────────────────────────────────────────
    def test_admin_route_and_page(self):
        """渠道页往返：一键同步按钮 + 状态行存在；POST 触发同步并回带统计。"""
        status, _, page = form_request(self.server, "GET", "/admin/channels", cookie=self.admin)
        self.assertEqual(status, 200)
        self.assertNotIn("同步已核准资料到知识库", page)
        self.assertNotIn("Dify", page)
        status, _, page = form_request(self.server, "GET", "/admin/knowledge", cookie=self.admin)
        self.assertEqual(status, 200)
        self.assertIn("企业知识", page)
        status, headers, _ = form_request(self.server, "POST", "/admin/channels/sync-dify-kb", fields={}, cookie=self.admin)
        self.assertEqual(status, 303)
        self.assertIn("/admin/channels?notice=kb_synced", headers.get("Location", ""))

    def test_create_then_skip_then_force_update(self):
        """首同步创建；内容未变跳过（零请求）；force 走 update-by-text。"""
        pid, _sid = self._make_product("KB-T1")
        stats = sync_kb_to_dify(self.db, "demo-tenant", actor="tester")
        self.assertTrue(stats["configured"])
        self.assertEqual(stats["created"], 1)
        create = self.kb.create_calls()[-1]
        self.assertEqual(create["auth"], "Bearer kb-test-key")
        self.assertEqual(create["body"]["indexing_technique"], "high_quality")
        self.assertTrue(create["body"]["text"].startswith("【产品】KB-T1"))
        self.assertIn("流量范围", create["body"]["text"])
        self.assertIn("20-80", create["body"]["text"])
        state = self.db.conn.execute(
            "SELECT * FROM kb_sync_state WHERE tenant_id='demo-tenant' AND product_id=?", (pid,)
        ).fetchone()
        self.assertTrue(state["document_id"])
        # 第二次：无变化 → 跳过（不发请求）
        before = len(self.kb.calls)
        stats2 = sync_kb_to_dify(self.db, "demo-tenant", actor="tester")
        self.assertEqual(stats2["skipped"], 1)
        self.assertEqual(stats2["created"], 0)
        self.assertEqual(stats2["updated"], 0)
        self.assertEqual(len(self.kb.calls), before)
        # force → 更新
        stats3 = sync_kb_to_dify(self.db, "demo-tenant", actor="tester", force=True)
        self.assertEqual(stats3["updated"], 1)
        upd = self.kb.update_calls()[-1]
        self.assertIn(f"/datasets/ds-777/documents/{state['document_id']}/update-by-text", upd["path"])

    def test_not_configured_noop(self):
        """未配置：静默跳过、零请求。"""
        saved = {key: os.environ.pop(key, None) for key in ("DIFY_KB_DATASET_ID", "DIFY_KB_API_KEY")}
        try:
            before = len(self.kb.calls)
            stats = sync_kb_to_dify(self.db, "demo-tenant")
            self.assertFalse(stats["configured"])
            self.assertEqual(len(self.kb.calls), before)
        finally:
            for key, value in saved.items():
                if value is not None:
                    os.environ[key] = value

    def test_restricted_source_excluded_and_deleted(self):
        """来源禁用：字段不进入知识库文档；标记后同步删除其文档并清台账。"""
        pid, sid = self._make_product("KB-T2")
        sync_kb_to_dify(self.db, "demo-tenant")
        docs = collect_kb_documents(self.db, "demo-tenant")
        self.assertTrue(any(doc["product_id"] == pid for doc in docs))
        self.db.conn.execute("UPDATE source_documents SET usage='restricted' WHERE tenant_id='demo-tenant' AND id=?", (sid,))
        self.db.conn.commit()
        docs_after = collect_kb_documents(self.db, "demo-tenant")
        self.assertFalse(any(doc["product_id"] == pid for doc in docs_after))
        stats = sync_kb_to_dify(self.db, "demo-tenant")
        self.assertGreaterEqual(stats["deleted"], 1)
        self.assertTrue(self.kb.delete_calls())
        state = self.db.conn.execute(
            "SELECT 1 FROM kb_sync_state WHERE tenant_id='demo-tenant' AND product_id=?", (pid,)
        ).fetchone()
        self.assertIsNone(state)

    def test_withdraw_deletes_document(self):
        """产品撤回：知识库文档删除、台账清空。"""
        pid, _sid = self._make_product("KB-T3")
        sync_kb_to_dify(self.db, "demo-tenant")
        before_deletes = len(self.kb.delete_calls())
        self.db.withdraw_product("demo-tenant", pid)
        stats = sync_kb_to_dify(self.db, "demo-tenant")
        self.assertGreaterEqual(stats["deleted"], 1)
        self.assertGreater(len(self.kb.delete_calls()), before_deletes)
        state = self.db.conn.execute(
            "SELECT 1 FROM kb_sync_state WHERE tenant_id='demo-tenant' AND product_id=?", (pid,)
        ).fetchone()
        self.assertIsNone(state)


if __name__ == "__main__":
    unittest.main()
