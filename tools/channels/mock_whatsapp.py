#!/usr/bin/env python3
"""本地模拟 Twilio（WhatsApp 通道，开发与自动化测试共用，仅标准库）。

覆盖最小接口面：Messages.json 创建消息（发送）、状态回调模拟、入站消息模拟（带真实 Twilio 签名算法）。
控制面端点（仅本机开发使用）：
  GET  /_control/sent                查看已外发消息
  POST /_control/inbound  {"from":"whatsapp:+49...","body":"hi","webhook_url":"http://..."}  模拟买方入站（POST 到指定 webhook）
  POST /_control/status   {"sid":"SM...","status":"delivered"}  触发该消息的 StatusCallback 回调

签名算法与 Twilio 一致：HMAC-SHA1(auth_token, url + 排序后的参数拼接)，Base64。
用法（手动联调）：python3 tools/channels/mock_whatsapp.py --port 8152
用法（测试）：srv = MockTwilioServer(); srv.base_url() → http://127.0.0.1:<port>
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import threading
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def twilio_signature(auth_token: str, url: str, params: dict) -> str:
    """Twilio 请求签名（用于验签与模拟对端联调）。"""
    payload = url + "".join(f"{key}{params[key]}" for key in sorted(params))
    digest = hmac.new(auth_token.encode("utf-8"), payload.encode("utf-8"), hashlib.sha1).digest()
    return base64.b64encode(digest).decode("ascii")


class MockTwilioServer:
    """进程内模拟 Twilio Messages API（每实例独立状态，自动选端口）。"""

    def __init__(self, host: str = "127.0.0.1", port: int = 0, auth_token: str = "mock-auth-token", account_sid: str = "ACmock") -> None:
        self.host = host
        self.auth_token = auth_token
        self.account_sid = account_sid
        self._lock = threading.Lock()
        self._sent: list[dict] = []
        self._counter = 0
        state = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args) -> None:
                pass

            def _json(self, payload: dict, status: int = 200) -> None:
                data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def _read_body(self) -> tuple[dict, dict]:
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b""
                content_type = self.headers.get("Content-Type", "")
                if "application/json" in content_type:
                    try:
                        payload = json.loads(raw.decode("utf-8"))
                        return (payload if isinstance(payload, dict) else {}), {}
                    except json.JSONDecodeError:
                        return {}, {}
                parsed = urllib.parse.parse_qs(raw.decode("utf-8"), keep_blank_values=True)
                return {}, {key: values[0] for key, values in parsed.items()}

            def do_GET(self) -> None:
                state._handle(self)

            def do_POST(self) -> None:
                state._handle(self)

        self._server = ThreadingHTTPServer((host, port), Handler)
        self.port = self._server.server_address[1]
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    # ── 控制面（测试 / 手动联调）──────────────────────────────────────────
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"

    def sent_messages(self) -> list[dict]:
        with self._lock:
            return [dict(item) for item in self._sent]

    def inject_inbound(self, from_number: str, body: str, webhook_url: str, message_sid: str | None = None, to_number: str = "whatsapp:+14155238886") -> tuple[int, str]:
        """模拟 Twilio 把买方消息 POST 到卖家 webhook（带正确签名），返回 (状态码, 响应文本)。"""
        with self._lock:
            self._counter += 1
            sid = message_sid or f"SM{self._counter:032x}"
        params = {
            "AccountSid": self.account_sid,
            "From": from_number,
            "To": to_number,
            "Body": body,
            "MessageSid": sid,
            "NumMedia": "0",
        }
        data = urllib.parse.urlencode(params).encode("utf-8")
        request = urllib.request.Request(
            webhook_url,
            data=data,
            headers={"Content-Type": "application/x-www-form-urlencoded", "X-Twilio-Signature": twilio_signature(self.auth_token, webhook_url, params)},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return response.status, response.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as error:  # noqa: PERF203
            return error.code, error.read().decode("utf-8", errors="replace")

    def trigger_status(self, sid: str, status: str) -> bool:
        """触发该消息的状态回调（queued/sent/delivered/failed），无回调地址时返回 False。"""
        with self._lock:
            record = next((item for item in self._sent if item["sid"] == sid), None)
        callback = record.get("status_callback") if record else ""
        if not callback:
            return False
        params = {"MessageSid": sid, "MessageStatus": status, "AccountSid": self.account_sid, "To": record.get("to", ""), "From": record.get("from", "")}
        data = urllib.parse.urlencode(params).encode("utf-8")
        request = urllib.request.Request(
            callback,
            data=data,
            headers={"Content-Type": "application/x-www-form-urlencoded", "X-Twilio-Signature": twilio_signature(self.auth_token, callback, params)},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=10):
                return True
        except Exception:  # 回调失败不影响模拟主流程
            return False

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()

    # ── 请求处理 ─────────────────────────────────────────────────────────
    def _handle(self, handler: BaseHTTPRequestHandler) -> None:
        path = handler.path.split("?", 1)[0]
        if path == "/_control/sent" and handler.command == "GET":
            handler._json({"sent": self.sent_messages()})
            return
        if path == "/_control/inbound" and handler.command == "POST":
            payload, _form = handler._read_body()
            status, text = self.inject_inbound(
                str(payload.get("from", "whatsapp:+490000000")),
                str(payload.get("body", "")),
                str(payload.get("webhook_url", "")),
                message_sid=str(payload.get("message_sid", "")) or None,
            )
            handler._json({"ok": True, "status": status, "body": text})
            return
        if path == "/_control/status" and handler.command == "POST":
            payload, _form = handler._read_body()
            ok = self.trigger_status(str(payload.get("sid", "")), str(payload.get("status", "delivered")))
            handler._json({"ok": ok})
            return
        if handler.command == "POST" and path.startswith("/2010-04-01/Accounts/") and path.endswith("/Messages.json"):
            _payload, form = handler._read_body()
            with self._lock:
                self._counter += 1
                sid = f"SM{self._counter:032x}"
                record = {
                    "sid": sid,
                    "to": form.get("To", ""),
                    "from": form.get("From", ""),
                    "body": form.get("Body", ""),
                    "status_callback": form.get("StatusCallback", ""),
                    "authorization": handler.headers.get("Authorization", ""),
                    "status": "queued",
                }
                self._sent.append(record)
            handler._json(
                {
                    "sid": sid,
                    "status": "queued",
                    "to": record["to"],
                    "from": record["from"],
                    "body": record["body"],
                },
                201,
            )
            return
        handler._json({"message": "not found", "code": 20404}, 404)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8152)
    parser.add_argument("--auth-token", default="mock-auth-token")
    args = parser.parse_args()
    server = MockTwilioServer(args.host, args.port, auth_token=args.auth_token)
    print(f"模拟 Twilio 就绪：{server.base_url()}（Messages.json + /_control/*）")
    print("签名算法与 Twilio 一致；Ctrl+C 退出。")
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        pass
    finally:
        server.stop()


if __name__ == "__main__":
    main()
