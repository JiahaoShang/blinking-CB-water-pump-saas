"""DeepSeek 模型辅助接入的边界测试：真实 HTTP 协议模拟服务 + 真实应用进程。

约束与设计（对应 docs/MVP-开发需求文档.md M3/M5、docs/技术架构草案.md §10、ADR-04）：

- 不调用真实 DeepSeek 账户，也不使用真实 Key：所有场景由本地 ThreadingHTTPServer 模拟
  Chat Completions 协议，通过 DEEPSEEK_BASE_URL 等价配置（ai_config.base_url）指向模拟服务。
- 覆盖：成功抽取与候选门控、无效 JSON / 超时 / 429 / 401 / 5xx 的失败语义与重试恢复、
  错误字段/单位/含糊值丢弃、流量/扬程/温度缺单位不被补成确定单位（数量按计数语义）、
  明确提问字段与模型字段冲突时不答非所问（确定性路由或转待核实）、未核准字段不可被答出、
  同键重试（B-08 语义）、日志只记录提供方/模型/状态/耗时/用量（不含密钥与买家消息）、
  管理概览内部状态、以及一条完整接口旅程（消息→确认→推荐→问答）。
- 浏览器端参考聊天（reference-client/app.js）在 deepseek 模式下的 Node 旅程本轮未执行，
  见开发状态记录中的未完成项；本文件中的接口旅程覆盖同一批 API 的串联调用。
"""
import http.client
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlencode

import ai_client
from ai_client import AIConfig, AIConfigurationError
from app import explicit_answer_fields, guard_answer_field_selection, make_server

ROOT = Path(__file__).resolve().parent
FAKE_KEY = "sk-test-not-a-real-deepseek-key"
ORIGIN = "https://seller-a.example"


class MockDeepSeekServer:
    """真实 HTTP 协议模拟服务：按脚本返回 Chat Completions 响应，并记录收到的请求。"""

    def __init__(self, scripts):
        self.scripts = list(scripts)
        self.requests = []
        self.lock = threading.Lock()
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                length = int(self.headers.get("Content-Length", "0") or 0)
                raw = self.rfile.read(length) if length else b""
                try:
                    body = json.loads(raw.decode("utf-8"))
                except (ValueError, UnicodeDecodeError):
                    body = None
                with outer.lock:
                    outer.requests.append({"path": self.path, "headers": dict(self.headers.items()), "body": body})
                    script = outer.scripts.pop(0) if outer.scripts else {"status": 500, "json": {"error": "unscripted"}}
                delay = script.get("delay")
                if delay:
                    time.sleep(delay)
                status = script.get("status", 200)
                if "raw" in script:
                    payload = str(script["raw"]).encode("utf-8")
                else:
                    payload = json.dumps(script.get("json", {}), ensure_ascii=False).encode("utf-8")
                try:
                    self.send_response(status)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(payload)))
                    self.end_headers()
                    self.wfile.write(payload)
                except (BrokenPipeError, ConnectionResetError):
                    pass

            def log_message(self, format, *args):  # noqa: A002 - 基类签名
                return

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def base_url(self):
        return f"http://127.0.0.1:{self.server.server_port}"

    def stop(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()


def chat_completion(content, status=200, usage=(120, 30)):
    return {
        "status": status,
        "json": {
            "id": "chatcmpl-mock",
            "object": "chat.completion",
            "created": 0,
            "model": "deepseek-flash",
            "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": content}}],
            "usage": {"prompt_tokens": usage[0], "completion_tokens": usage[1], "total_tokens": usage[0] + usage[1]},
        },
    }


def extraction_success(values):
    return chat_completion(json.dumps({"values": values}, ensure_ascii=False))


def selection_success(field):
    return chat_completion(json.dumps({"field": field}, ensure_ascii=False))


def ai_config_for(mock, timeout_seconds=5.0, max_tokens=512):
    return AIConfig(
        mode="deepseek", api_key=FAKE_KEY, base_url=mock.base_url,
        timeout_seconds=timeout_seconds, max_tokens=max_tokens,
    )


class DeepSeekBoundaryTests(unittest.TestCase):
    maxDiff = None

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "app.db"
        self.mock = None
        self.server = None
        self.thread = None

    def tearDown(self):
        if self.server is not None:
            self.server.shutdown()
            self.server.server_close()
            self.server.RequestHandlerClass.db.conn.close()
        if self.thread is not None:
            self.thread.join()
        if self.mock is not None:
            self.mock.stop()
        self.temp_dir.cleanup()

    # ---- 基础设施 ----------------------------------------------------------

    def start_mock(self, scripts):
        self.mock = MockDeepSeekServer(scripts)
        return self.mock

    def start_app(self, mock, timeout_seconds=5.0):
        self.server = make_server(self.db_path, port=0, ai_config=ai_config_for(mock, timeout_seconds=timeout_seconds))
        db = self.server.RequestHandlerClass.db
        db.seed_demo_users()
        db.register_buyer_site("demo-tenant", "site-a", ORIGIN)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        return self.server

    def request(self, method, path, payload=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.server.server_port)
        request_headers = {"Content-Type": "application/json", **(headers or {})}
        conn.request(method, path, json.dumps(payload).encode() if payload is not None else None, request_headers)
        response = conn.getresponse()
        data = response.read()
        result = response.status, dict(response.getheaders()), json.loads(data) if data else None
        conn.close()
        return result

    def open_conversation(self):
        status, _, created = self.request("POST", "/api/v1/buyer/conversations", {"site_key": "site-a"}, {"Origin": ORIGIN})
        self.assertEqual(status, 201)
        headers = {"X-Site-Key": "site-a", "Authorization": f"Bearer {created['access_token']}", "Origin": ORIGIN}
        return created["conversation_id"], headers

    def send_message(self, path, headers, text, key=None):
        extra = dict(headers)
        if key:
            extra["Idempotency-Key"] = key
        return self.request("POST", path + "/messages", {"text": text}, extra)

    def publish_product(self, model="DS-50", reject_material=False):
        db = self.server.RequestHandlerClass.db
        product_id = db.create_product("demo-tenant", model, "跨接测试泵", "冷却水循环")
        source_id = db.add_source("demo-tenant", product_id, f"{model}-manual.txt", "TXT", "第 2 页：规格参数表")
        for key, value, unit in (
            ("flow_range", "20-60", "m³/h"), ("head_range", "25-45", "m"), ("media", "清水", ""),
            ("temperature", "0-60", "°C"), ("power", "7.5", "kW"),
        ):
            db.save_field("demo-tenant", product_id, key, value, unit, source_id, "第 2 页：规格参数表")
            db.approve_field("demo-tenant", product_id, key)
        if reject_material:
            db.save_field("demo-tenant", product_id, "material", "铸铁", "", source_id, "第 2 页：规格参数表")
            db.reject_field("demo-tenant", product_id, "material", "演示：拒驳后不上公开页")
        db.finalize_product_review("demo-tenant", product_id)
        db.publish_product("demo-tenant", product_id)
        return product_id, source_id

    def ai_rows(self):
        db = self.server.RequestHandlerClass.db
        return [dict(row) for row in db.conn.execute("SELECT * FROM ai_call_log ORDER BY rowid").fetchall()]

    def ai_rows_json(self):
        return json.dumps(self.ai_rows(), ensure_ascii=False)

    def login_admin(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.server.server_port)
        conn.request("POST", "/login", urlencode({"email": "demo-admin@example.invalid", "password": "demo-admin"}), {"Content-Type": "application/x-www-form-urlencoded"})
        response = conn.getresponse()
        headers = dict(response.getheaders())
        response.read()
        conn.close()
        return headers["Set-Cookie"].split(";", 1)[0]

    def admin_get(self, path, cookie):
        conn = http.client.HTTPConnection("127.0.0.1", self.server.server_port)
        conn.request("GET", path, headers={"Cookie": cookie})
        response = conn.getresponse()
        text = response.read().decode("utf-8", errors="replace")
        status = response.status
        conn.close()
        return status, text

    # ---- 规则模式与配置显式性 ----------------------------------------------

    def test_rules_mode_is_default_reproducible_and_makes_no_model_calls(self):
        with patch.dict(os.environ, {}, clear=True):
            self.server = make_server(self.db_path, port=0)
        db = self.server.RequestHandlerClass.db
        db.seed_demo_users()
        db.register_buyer_site("demo-tenant", "site-a", ORIGIN)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.assertEqual(self.server.RequestHandlerClass.ai.mode, "rules")
        conversation_id, headers = self.open_conversation()
        path = f"/api/v1/buyer/conversations/{conversation_id}"
        status, _, body = self.send_message(path, headers, "flow 50 m3/h and clean water", key="rules-1")
        self.assertEqual(status, 201)
        values = body["requirement_draft"]["values"]
        self.assertEqual(values["flow"]["value"], "50")
        self.assertEqual(values["flow"]["source"], "buyer_message")
        count = db.conn.execute("SELECT COUNT(*) FROM ai_call_log").fetchone()[0]
        self.assertEqual(count, 0, "规则模式不得产生模型调用日志")

    def test_deepseek_configuration_failures_are_explicit(self):
        with patch.dict(os.environ, {"AI_MODE": "deepseek"}, clear=True):
            with self.assertRaises(AIConfigurationError) as ctx:
                AIConfig.from_env()
            self.assertIn("DEEPSEEK_API_KEY", str(ctx.exception))
            with self.assertRaises(AIConfigurationError):
                make_server(self.db_path, port=0)  # 缺 Key 拒绝启动，不静默落回规则
        with patch.dict(os.environ, {"AI_MODE": "gpt5"}, clear=True):
            with self.assertRaises(AIConfigurationError):
                AIConfig.from_env()
        with patch.dict(os.environ, {"AI_MODE": "deepseek", "DEEPSEEK_API_KEY": "sk-unit-secret", "DEEPSEEK_TIMEOUT_SECONDS": "abc"}, clear=True):
            with self.assertRaises(AIConfigurationError):
                AIConfig.from_env()
        config = AIConfig(mode="deepseek", api_key="sk-unit-secret")
        self.assertNotIn("sk-unit-secret", repr(config))
        self.assertEqual(ai_client.classify_http_status(401), "auth_failed")
        self.assertEqual(ai_client.classify_http_status(402), "insufficient_balance")
        self.assertEqual(ai_client.classify_http_status(429), "rate_limited")
        self.assertEqual(ai_client.classify_http_status(500), "server_error")
        self.assertEqual(ai_client.classify_http_status(400), "invalid_request")
        self.assertEqual(ai_client.STATUS_ACTIONS["auth_failed"], "configure")
        self.assertEqual(ai_client.STATUS_ACTIONS["rate_limited"], "retry")

    # ---- M3：抽取成功、候选门控、日志卫生 -----------------------------------

    def test_extraction_success_feeds_candidate_card_and_keeps_confirmation_gate(self):
        mock = self.start_mock([extraction_success({
            "flow": {"value": "50", "unit": "m³/h"},
            "head": {"value": "30", "unit": "m"},
            "media": {"value": "clean water", "unit": ""},
            "category": {"value": "centrifugal pump", "unit": ""},
            "use_case": {"value": "plant room cooling", "unit": ""},
        })])
        self.start_app(mock)
        conversation_id, headers = self.open_conversation()
        path = f"/api/v1/buyer/conversations/{conversation_id}"
        text = "We need a pump for the plant room, flow 50 m3/h at 30 m head, clean water."
        status, _, body = self.send_message(path, headers, text, key="m3-1")
        self.assertEqual(status, 201)
        draft = body["requirement_draft"]
        values = draft["values"]
        self.assertEqual((values["flow"]["value"], values["flow"]["unit"]), ("50", "m³/h"))
        self.assertEqual((values["head"]["value"], values["head"]["unit"]), ("30", "m"))
        self.assertEqual(values["media"]["value"], "清水")
        self.assertEqual(values["category"]["value"], "水泵")
        self.assertEqual(values["use_case"]["value"], "plant room cooling")
        for key in ("flow", "head", "media"):
            self.assertEqual(values[key]["state"], "candidate", "模型输出只能是候选，不能创造确认状态")
            self.assertEqual(values[key]["source"], "model_candidate")
        # 上游请求符合 DeepSeek Chat Completions 协议约定
        sent = mock.requests[0]
        self.assertEqual(sent["path"], "/chat/completions")
        self.assertEqual(sent["headers"].get("Authorization"), f"Bearer {FAKE_KEY}")
        self.assertEqual(sent["body"]["model"], "deepseek-flash")
        self.assertEqual(sent["body"]["response_format"], {"type": "json_object"})
        self.assertEqual(sent["body"]["thinking"], {"type": "disabled"})
        self.assertEqual(sent["body"]["stream"], False)
        self.assertEqual(sent["body"]["max_tokens"], 512)
        self.assertIn(text, sent["body"]["messages"][1]["content"])
        # 未确认前不得取得匹配结果
        gate = self.request("POST", path + "/recommendations", {"revision_id": draft["revision_id"]}, headers)
        self.assertEqual(gate[0], 409)
        self.assertEqual(gate[2]["error"], "confirmed_revision_required")
        # 日志：提供方/模型/状态/耗时/用量；不含密钥与买家消息
        rows = self.ai_rows()
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual((row["provider"], row["model"], row["status"], row["action"]), ("deepseek", "deepseek-flash", "ok", "none"))
        self.assertEqual((row["http_status"], row["prompt_tokens"], row["completion_tokens"]), (200, 120, 30))
        self.assertIsInstance(row["duration_ms"], int)
        serialized = self.ai_rows_json()
        self.assertNotIn(FAKE_KEY, serialized)
        self.assertNotIn("plant room", serialized)

    def test_extraction_drops_invalid_units_unknown_fields_and_ambiguous_values(self):
        mock = self.start_mock([extraction_success({
            "flow": {"value": "50", "unit": "gpm"},          # 单位不认识：不换算、丢弃
            "head": {"value": "abc", "unit": "m"},           # 非数字：丢弃
            "voltage": {"value": "380", "unit": "V"},        # 白名单之外：丢弃
            "quantity": {"value": "-3", "unit": "units"},    # 非法数量：丢弃
            "media": {"value": "seawater", "unit": ""},      # 无法归入可解释介质：丢弃
            "temperature": {"value": "50", "unit": "°C"},
            "region": {"value": "Germany", "unit": ""},
            "delivery": {"value": "8 weeks", "unit": ""},
        })])
        self.start_app(mock)
        conversation_id, headers = self.open_conversation()
        path = f"/api/v1/buyer/conversations/{conversation_id}"
        status, _, body = self.send_message(path, headers, "some pending details", key="m3-drop")
        self.assertEqual(status, 201)
        values = body["requirement_draft"]["values"]
        self.assertEqual(set(values.keys()), {"temperature", "region", "delivery"})
        self.assertEqual((values["temperature"]["value"], values["temperature"]["unit"]), ("50", "°C"))
        self.assertNotIn("seawater", json.dumps(values, ensure_ascii=False))
        self.assertEqual(body["requirement_draft"]["next_question"], "flow")

    # ---- 缺口 A：空单位不得补成确定单位（2026-10-04 审计初审修复） -----------------

    def test_normalize_extraction_rejects_empty_unit_for_flow_head_temperature(self):
        result = ai_client.normalize_extraction_payload({"values": {
            "flow": {"value": "50", "unit": ""},
            "head": {"value": "30", "unit": ""},
            "temperature": {"value": "60", "unit": ""},
            "quantity": {"value": "3", "unit": ""},  # 计数（台/套）：空单位不构成技术单位猜测
            "media": {"value": "clean water", "unit": ""},
        }})
        self.assertEqual(set(result), {"quantity", "media"},
                         "缺单位的流量/扬程/温度必须丢弃；不得用标准单位补成确定参数")
        self.assertEqual(result["quantity"], {"value": "3", "unit": "units"})

    def test_normalize_extraction_rejects_unknown_units_and_normalizes_supported_forms(self):
        dropped = ai_client.normalize_extraction_payload({"values": {
            "flow": {"value": "50", "unit": "L/min"},
            "head": {"value": "30", "unit": "ft"},
            "temperature": {"value": "60", "unit": "°F"},
            "quantity": {"value": "3", "unit": "kg"},
        }})
        self.assertEqual(dropped, {}, "不认识的单位不猜测、不换算，直接丢弃")
        kept = ai_client.normalize_extraction_payload({"values": {
            "flow": {"value": "50", "unit": "m3/h"},
            "head": {"value": "30", "unit": "meters"},
            "temperature": {"value": "-5", "unit": "celsius"},
            "quantity": {"value": "3", "unit": "pcs"},
        }})
        self.assertEqual(kept["flow"], {"value": "50", "unit": "m³/h"})
        self.assertEqual(kept["head"], {"value": "30", "unit": "m"})
        self.assertEqual(kept["temperature"], {"value": "-5", "unit": "°C"})
        self.assertEqual(kept["quantity"], {"value": "3", "unit": "units"})

    def test_extraction_empty_unit_flow_stays_unknown_and_buyer_can_fill_it(self):
        mock = self.start_mock([extraction_success({
            "flow": {"value": "50", "unit": ""},  # 缺单位：不得补成 50 m³/h
            "head": {"value": "30", "unit": "m"},
        })])
        self.start_app(mock)
        conversation_id, headers = self.open_conversation()
        path = f"/api/v1/buyer/conversations/{conversation_id}"
        status, _, body = self.send_message(path, headers, "We need a pump: flow 50, head 30 m.", key="empty-unit-1")
        self.assertEqual(status, 201)
        draft = body["requirement_draft"]
        values = draft["values"]
        self.assertNotIn("flow", values, "买家原话未给单位时，模型输出不得被补成确定参数")
        self.assertEqual((values["head"]["value"], values["head"]["unit"]), ("30", "m"))
        self.assertEqual(draft["next_question"], "flow", "缺单位字段回到待确认，买家可手填或标未知")
        # 买家标未知：空值提交后状态为 unknown
        status, _, marked = self.request(
            "POST", path + "/requirements/confirm",
            {"base_revision_id": draft["revision_id"], "values": {**values, "flow": {"value": "", "unit": ""}}},
            {**headers, "Idempotency-Key": "empty-unit-2"},
        )
        self.assertEqual(status, 201)
        self.assertEqual(marked["values"]["flow"]["state"], "unknown")
        # 买家手填：补上 55 m³/h 后成为已确认值
        status, _, filled = self.request(
            "POST", path + "/requirements/confirm",
            {"base_revision_id": marked["revision_id"], "values": {**marked["values"], "flow": {"value": "55", "unit": "m³/h"}}},
            {**headers, "Idempotency-Key": "empty-unit-3"},
        )
        self.assertEqual(status, 201)
        self.assertEqual((filled["values"]["flow"]["value"], filled["values"]["flow"]["state"]), ("55", "confirmed"))

    # ---- M3：故障语义（同键恢复重试按 B-08）----------------------------------

    def test_extraction_invalid_json_is_503_then_same_key_retry_recovers(self):
        mock = self.start_mock([
            {"status": 200, "raw": "Sorry, I cannot extract that."},
            extraction_success({"flow": {"value": "50", "unit": "m³/h"}}),
        ])
        self.start_app(mock)
        db = self.server.RequestHandlerClass.db
        conversation_id, headers = self.open_conversation()
        path = f"/api/v1/buyer/conversations/{conversation_id}"
        status, _, failed = self.send_message(path, headers, "flow 50", key="retry-1")
        self.assertEqual(status, 503)
        self.assertEqual(failed["error"], "extraction_unavailable")
        self.assertEqual(len(db.list_messages("demo-tenant", conversation_id)), 1, "失败时消息必须已保存")
        self.assertIsNone(db.get_latest_revision("demo-tenant", conversation_id))
        self.assertEqual(self.ai_rows()[0]["status"], "invalid_output")
        self.assertEqual(self.ai_rows()[0]["action"], "retry")
        status, _, retried = self.send_message(path, headers, "flow 50", key="retry-1")
        self.assertEqual(status, 200)
        self.assertEqual(retried["status"], "duplicate")
        self.assertIsNotNone(retried["requirement_draft"]["revision_id"])
        self.assertEqual(len(db.list_messages("demo-tenant", conversation_id)), 1, "同键重试不得重复存消息")
        self.assertEqual(len(db.get_revisions("demo-tenant", conversation_id)), 1, "同键重试只产生一个草稿")
        self.assertEqual([row["status"] for row in self.ai_rows()], ["invalid_output", "ok"])
        self.assertEqual(len(mock.requests), 2)

    def test_extraction_timeout_is_503_then_retry_recovers(self):
        mock = self.start_mock([
            {"delay": 3.0, **extraction_success({"flow": {"value": "50", "unit": "m³/h"}})},
            extraction_success({"flow": {"value": "50", "unit": "m³/h"}}),
        ])
        self.start_app(mock, timeout_seconds=0.6)
        conversation_id, headers = self.open_conversation()
        path = f"/api/v1/buyer/conversations/{conversation_id}"
        status, _, _ = self.send_message(path, headers, "flow 50", key="timeout-1")
        self.assertEqual(status, 503)
        self.assertEqual(self.ai_rows()[0]["status"], "timeout")
        self.assertEqual(self.ai_rows()[0]["action"], "retry")
        status, _, retried = self.send_message(path, headers, "flow 50", key="timeout-1")
        self.assertEqual(status, 200)
        self.assertTrue(retried["requirement_draft"]["revision_id"])

    def test_extraction_rate_limited_and_server_error_are_retryable_statuses(self):
        mock = self.start_mock([
            {"status": 429, "json": {"error": {"message": "rate limit"}}},
            {"status": 500, "json": {"error": {"message": "server exploded"}}},
        ])
        self.start_app(mock)
        conversation_id, headers = self.open_conversation()
        path = f"/api/v1/buyer/conversations/{conversation_id}"
        self.assertEqual(self.send_message(path, headers, "first", key="rl-1")[0], 503)
        self.assertEqual(self.send_message(path, headers, "second", key="rl-2")[0], 503)
        rows = self.ai_rows()
        self.assertEqual([(row["status"], row["action"], row["http_status"]) for row in rows],
                         [("rate_limited", "retry", 429), ("server_error", "retry", 500)])

    def test_extraction_auth_failed_marks_configuration_and_hides_provider(self):
        mock = self.start_mock([{"status": 401, "json": {"error": {"message": "Authentication Fails"}}}])
        self.start_app(mock)
        db = self.server.RequestHandlerClass.db
        conversation_id, headers = self.open_conversation()
        path = f"/api/v1/buyer/conversations/{conversation_id}"
        status, _, body = self.send_message(path, headers, "flow 50", key="auth-1")
        self.assertEqual(status, 503)
        body_text = json.dumps(body, ensure_ascii=False).lower()
        self.assertNotIn("deepseek", body_text)
        self.assertNotIn("401", body_text)
        self.assertNotIn("api key", body_text)
        rows = self.ai_rows()
        self.assertEqual((rows[0]["status"], rows[0]["action"], rows[0]["http_status"]), ("auth_failed", "configure", 401))
        self.assertEqual(len(db.list_messages("demo-tenant", conversation_id)), 1)

    # ---- M5：只从已发布快照构造答案 -----------------------------------------

    def test_question_selection_answers_from_published_snapshot_only(self):
        mock = self.start_mock([selection_success("flow_range")])
        self.start_app(mock)
        product_id, _ = self.publish_product()
        conversation_id, headers = self.open_conversation()
        path = f"/api/v1/buyer/conversations/{conversation_id}"
        status, _, body = self.request(
            "POST", path + "/questions",
            {"question": "What is the flow range of this model?", "product_id": product_id}, headers,
        )
        self.assertEqual(status, 201)
        self.assertEqual(body["status"], "grounded")
        self.assertIn("20-60", body["answer"])
        self.assertIn("m³/h", body["answer"])
        citation = body["citations"][0]
        self.assertEqual(citation["filename"], "DS-50-manual.txt")
        self.assertEqual(citation["product_version"], "1")
        # 模型只收到字段清单与问题；答案数值不可能来自模型
        sent_content = mock.requests[0]["body"]["messages"][1]["content"]
        self.assertIn("flow_range", sent_content)
        self.assertIn("What is the flow range", sent_content)
        self.assertNotIn("20-60", sent_content)
        rows = self.ai_rows()
        self.assertEqual((rows[0]["task"], rows[0]["status"]), ("answer_field_selection", "ok"))

    def test_question_never_answers_unapproved_field(self):
        mock = self.start_mock([selection_success("material")])
        self.start_app(mock)
        product_id, _ = self.publish_product(reject_material=True)
        conversation_id, headers = self.open_conversation()
        path = f"/api/v1/buyer/conversations/{conversation_id}"
        status, _, body = self.request(
            "POST", path + "/questions",
            {"question": "What material is the pump casing?", "product_id": product_id}, headers,
        )
        self.assertEqual(status, 201)
        self.assertEqual(body["status"], "needs_human", "未核准字段不能被答出")
        self.assertNotIn("铸铁", body["answer"])
        sent_content = mock.requests[0]["body"]["messages"][1]["content"]
        self.assertNotIn("铸铁", sent_content)
        self.assertNotIn("- material:", sent_content, "未核准字段不得进入模型可选清单")
        self.assertIn("- temperature:", sent_content)

    # ---- 缺口 B：模型字段与提问明确字段冲突时不答非所问（2026-10-04 审计初审修复）----

    def test_question_wrong_field_selection_is_answered_from_asked_field(self):
        # 审计复现场景：买家明确问 head，模型误选 flow_range（虽在已发布白名单）——
        # 不得把流量作为已答输出；提问只含一个明确字段且已发布，确定性路由到 head_range。
        mock = self.start_mock([selection_success("flow_range")])
        self.start_app(mock)
        product_id, _ = self.publish_product()
        conversation_id, headers = self.open_conversation()
        path = f"/api/v1/buyer/conversations/{conversation_id}"
        status, _, body = self.request(
            "POST", path + "/questions",
            {"question": "What is the head range?", "product_id": product_id}, headers,
        )
        self.assertEqual(status, 201)
        self.assertEqual(body["status"], "grounded")
        self.assertIn("25-45", body["answer"], "应按提问字段（扬程）作答")
        self.assertNotIn("20-60", body["answer"], "不得把模型误选的流量字段作为已答输出")
        self.assertEqual(body["citations"][0]["field"], "扬程范围")

    def test_question_correct_field_selection_answers_asked_field(self):
        mock = self.start_mock([selection_success("head_range")])
        self.start_app(mock)
        product_id, _ = self.publish_product()
        conversation_id, headers = self.open_conversation()
        path = f"/api/v1/buyer/conversations/{conversation_id}"
        status, _, body = self.request(
            "POST", path + "/questions",
            {"question": "What is the maximum head of this model?", "product_id": product_id}, headers,
        )
        self.assertEqual(status, 201)
        self.assertEqual(body["status"], "grounded", "正确字段不得被防护误拦")
        self.assertIn("25-45", body["answer"])
        self.assertNotIn("20-60", body["answer"])

    def test_question_conflict_with_multiple_asked_fields_is_held_for_review(self):
        # 提问同时明确问 flow 与 head，模型却选 temperature：目标字段不唯一，转待核实，
        # 不输出任何字段的数值（含模型所选字段），也不自动创建人工任务。
        mock = self.start_mock([selection_success("temperature")])
        self.start_app(mock)
        product_id, _ = self.publish_product()
        conversation_id, headers = self.open_conversation()
        path = f"/api/v1/buyer/conversations/{conversation_id}"
        status, _, body = self.request(
            "POST", path + "/questions",
            {"question": "What are the flow and head ranges?", "product_id": product_id}, headers,
        )
        self.assertEqual(status, 201)
        self.assertEqual(body["status"], "needs_human")
        for leaked in ("20-60", "25-45", "0-60"):
            self.assertNotIn(leaked, body["answer"], f"转待核实时不得输出字段值 {leaked}")
        self.assertEqual(self.server.RequestHandlerClass.db.list_human_tasks("demo-tenant"), [],
                         "转待核实不自动创建人工任务")

    def test_question_conflict_routing_blocked_when_asked_field_not_published(self):
        # 提问明确问 material（未核准、不在已发布字段内），模型选 temperature：
        # 不得拿温度顶替；也不存在可路由的已发布字段，转待核实。
        mock = self.start_mock([selection_success("temperature")])
        self.start_app(mock)
        product_id, _ = self.publish_product(reject_material=True)
        conversation_id, headers = self.open_conversation()
        path = f"/api/v1/buyer/conversations/{conversation_id}"
        status, _, body = self.request(
            "POST", path + "/questions",
            {"question": "What material is used for the pump casing?", "product_id": product_id}, headers,
        )
        self.assertEqual(status, 201)
        self.assertEqual(body["status"], "needs_human")
        self.assertNotIn("0-60", body["answer"], "不得拿温度字段顶替未发布的材质字段")

    def test_question_field_detection_boundaries_and_guard_rules(self):
        # 检测：与规则模式同一张关键词表；英文按词边界（overhead 不误中 head），中文按子串。
        self.assertEqual(explicit_answer_fields("What is the head range?"), ["head_range"])
        self.assertEqual(explicit_answer_fields("你能告诉我这台泵的流量范围吗"), ["flow_range"])
        self.assertEqual(explicit_answer_fields("What are the flow and head ranges?"), ["flow_range", "head_range"])
        self.assertEqual(explicit_answer_fields("Can it pump from an overhead tank?"), [],
                         "英文关键词按词边界匹配，overhead 不误中 head")
        self.assertEqual(explicit_answer_fields("Please send me the price"), [], "商业问题不在字段表内")

        keys = {"flow_range", "head_range", "temperature"}
        self.assertEqual(guard_answer_field_selection("What is the head range?", "flow_range", keys), "head_range",
                         "单一明确字段且已发布：确定性路由")
        self.assertIsNone(guard_answer_field_selection("What is the power rating?", "temperature", keys),
                          "提问字段未发布：不得用其他已发布字段顶替")
        self.assertEqual(guard_answer_field_selection("What are the flow and head ranges?", "head_range", keys),
                         "head_range", "模型选择是明确字段之一：维持（回答仍在提问范围内）")
        self.assertIsNone(guard_answer_field_selection("What are the flow and head ranges?", "temperature", keys),
                          "多个明确字段且模型选择不在其中：转待核实")
        self.assertEqual(guard_answer_field_selection("How should I choose a pump?", "flow_range", keys), "flow_range",
                         "无明确字段：维持模型选择（防护不做推断）")
        self.assertIsNone(guard_answer_field_selection("What is the head range?", None, keys),
                          "模型给出 none：维持转待核实语义")

    def test_question_model_failure_keeps_question_and_creates_no_fake_answer_or_task(self):
        mock = self.start_mock([
            {"status": 429, "json": {"error": {"message": "rate limit"}}},
            selection_success("none"),
        ])
        self.start_app(mock)
        product_id, _ = self.publish_product()
        conversation_id, headers = self.open_conversation()
        path = f"/api/v1/buyer/conversations/{conversation_id}"
        question = {"question": "What is the temperature range for the liquid?", "product_id": product_id}
        status, _, failed = self.request("POST", path + "/questions", question, {**headers, "Idempotency-Key": "q-1"})
        self.assertEqual(status, 503)
        self.assertNotIn("deepseek", json.dumps(failed, ensure_ascii=False).lower())
        _, _, current = self.request("GET", path, headers=headers)
        self.assertEqual(current["messages"][0]["content"], question["question"], "失败时问题必须已保存")
        self.assertEqual(current["answers"], [], "失败时不得保存假答案")
        self.assertEqual(current["human_tasks"], [], "失败时不得声称已安排人工")
        self.assertEqual(self.ai_rows()[0]["status"], "rate_limited")
        # 同键重试可继续（模型恢复）
        status, _, retried = self.request("POST", path + "/questions", question, {**headers, "Idempotency-Key": "q-1"})
        self.assertEqual(status, 201)
        self.assertEqual(retried["status"], "needs_human")
        self.assertEqual(len(self.ai_rows()), 2)

    def test_commercial_question_never_calls_model(self):
        mock = self.start_mock([])
        self.start_app(mock)
        product_id, _ = self.publish_product()
        conversation_id, headers = self.open_conversation()
        path = f"/api/v1/buyer/conversations/{conversation_id}"
        status, _, body = self.request(
            "POST", path + "/questions",
            {"question": "Please give me the price for 10 units.", "product_id": product_id}, headers,
        )
        self.assertEqual(status, 201)
        self.assertEqual(body["status"], "needs_human")
        self.assertEqual(len(mock.requests), 0, "商业承诺类问题不得调用模型")
        self.assertEqual(self.ai_rows(), [])
        self.assertEqual(self.server.RequestHandlerClass.db.list_human_tasks("demo-tenant"), [])

    # ---- 管理概览内部状态与接口旅程 ------------------------------------------

    def test_model_diagnostics_are_in_settings_and_not_dashboard(self):
        mock = self.start_mock([extraction_success({"flow": {"value": "50", "unit": "m³/h"}})])
        self.start_app(mock)
        conversation_id, headers = self.open_conversation()
        path = f"/api/v1/buyer/conversations/{conversation_id}"
        self.assertEqual(self.send_message(path, headers, "flow 50", key="dash-1")[0], 201)
        cookie = self.login_admin()
        status, page = self.admin_get("/admin", cookie)
        self.assertEqual(status, 200)
        self.assertNotIn("模型接入", page)
        self.assertNotIn("DeepSeek 模型辅助", page)
        status, page = self.admin_get("/admin/preferences", cookie)
        self.assertEqual(status, 200)
        self.assertIn("运行诊断", page)
        self.assertIn("deepseek-flash", page)
        self.assertIn("需求提取", page)
        self.assertIn("成功", page)
        self.assertNotIn(FAKE_KEY, page)

    def test_interface_journey_message_confirm_recommend_question_in_deepseek_mode(self):
        mock = self.start_mock([
            extraction_success({
                "flow": {"value": "50", "unit": "m³/h"},
                "head": {"value": "30", "unit": "m"},
                "media": {"value": "clean water", "unit": ""},
            }),
            selection_success("temperature"),
        ])
        self.start_app(mock)
        product_id, _ = self.publish_product()
        conversation_id, headers = self.open_conversation()
        path = f"/api/v1/buyer/conversations/{conversation_id}"
        # 1) 买方原话 → 模型候选草稿
        status, _, sent = self.send_message(path, headers, "Pump needed: flow 50 m3/h, head 30 m, clean water.", key="j-1")
        self.assertEqual(status, 201)
        draft = sent["requirement_draft"]
        # 2) 买方确认（沿用现有闸口与修订语义）
        status, _, confirmed = self.request(
            "POST", path + "/requirements/confirm",
            {"base_revision_id": draft["revision_id"], "values": draft["values"]}, {**headers, "Idempotency-Key": "j-2"},
        )
        self.assertEqual(status, 201)
        # 3) 匹配：唯一候选由已核准字段规则筛出
        status, _, recommendation = self.request(
            "POST", path + "/recommendations", {"revision_id": confirmed["revision_id"]}, {**headers, "Idempotency-Key": "j-3"},
        )
        self.assertEqual(status, 201)
        self.assertEqual(recommendation["status"], "recommended")
        self.assertEqual([item["model"] for item in recommendation["results"]], ["DS-50"])
        self.assertIn("/s/site-a/products/", recommendation["results"][0]["detail_url"])
        # 4) 资料内提问：模型选字段，答案与引用由后端从发布快照构造
        status, _, answer = self.request(
            "POST", path + "/questions",
            {"question": "What is the liquid temperature range?", "product_id": product_id}, {**headers, "Idempotency-Key": "j-4"},
        )
        self.assertEqual(status, 201)
        self.assertEqual(answer["status"], "grounded")
        self.assertIn("0-60", answer["answer"])
        self.assertEqual(answer["citations"][0]["filename"], "DS-50-manual.txt")
        # 日志恰好两条（抽取 + 字段定位），均为成功
        self.assertEqual([(row["task"], row["status"]) for row in self.ai_rows()],
                         [("requirement_extraction", "ok"), ("answer_field_selection", "ok")])
        self.assertEqual(len(mock.requests), 2)

    # ---- 试点脚本（真实账户联调用）在模拟服务下的行为锁定 ---------------------

    def test_pilot_live_check_script_against_mock(self):
        script = ROOT / "tools" / "pilot_checks" / "check_deepseek_live.py"
        mock = MockDeepSeekServer([extraction_success({"flow": {"value": "30", "unit": "m³/h"}})])
        self.addCleanup(mock.stop)
        env = {**os.environ, "AI_MODE": "deepseek", "DEEPSEEK_API_KEY": FAKE_KEY, "DEEPSEEK_BASE_URL": mock.base_url}
        completed = subprocess.run([sys.executable, str(script)], capture_output=True, text=True, env=env, cwd=str(ROOT), timeout=90)
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        self.assertIn("PASS", completed.stdout)
        self.assertNotIn(FAKE_KEY, completed.stdout)
        mock2 = MockDeepSeekServer([{"status": 401, "json": {"error": {"message": "Authentication Fails"}}}])
        self.addCleanup(mock2.stop)
        env2 = {**env, "DEEPSEEK_BASE_URL": mock2.base_url}
        failed = subprocess.run([sys.executable, str(script)], capture_output=True, text=True, env=env2, cwd=str(ROOT), timeout=90)
        self.assertEqual(failed.returncode, 1)
        self.assertIn("auth_failed", failed.stdout)
        self.assertNotIn(FAKE_KEY, failed.stdout)


if __name__ == "__main__":
    unittest.main()
