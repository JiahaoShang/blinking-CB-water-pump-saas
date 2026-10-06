#!/usr/bin/env python3
"""本地模拟 Telegram Bot API（开发与自动化测试共用，仅标准库）。

覆盖最小接口面：getUpdates（轮询消费）、sendMessage（记录外发）、setWebhook/deleteWebhook（模式切换）、getMe。
另提供控制面端点（仅本机开发使用）：
  POST /_control/inject  {"chat_id":1,"text":"hi","first_name":"Buyer"}  注入一条买方消息
  GET  /_control/sent     查看已外发消息
  GET  /_control/updates  查看待消费更新队列

用法（手动联调）：python3 tools/channels/mock_telegram.py --port 8151
用法（测试）：srv = MockTelegramServer(); srv.base_url() → http://127.0.0.1:<port>/bot
"""
from __future__ import annotations

import argparse
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class MockTelegramServer:
    """进程内模拟 Telegram Bot API 服务（每实例独立状态，自动选端口）。"""

    def __init__(self, host: str = "127.0.0.1", port: int = 0) -> None:
        self.host = host
        self._lock = threading.Lock()
        self._updates: list[dict] = []
        self._sent: list[dict] = []
        self._next_update_id = 1
        self._next_message_id = 1000
        self._webhook_url = ""
        state = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args) -> None:  # 安静运行
                pass

            def _json(self, payload: dict, status: int = 200) -> None:
                data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def _read_json(self) -> dict:
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b""
                if not raw:
                    return {}
                try:
                    payload = json.loads(raw.decode("utf-8"))
                    return payload if isinstance(payload, dict) else {}
                except json.JSONDecodeError:
                    return {}

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
        return f"http://{self.host}:{self.port}/bot"

    def inject_message(self, chat_id: int, text: str, first_name: str = "Buyer", username: str = "", update_id: int | None = None) -> int:
        """模拟一条买方私聊消息进入更新队列。"""
        with self._lock:
            if update_id is None:
                update_id = self._next_update_id
                self._next_update_id += 1
            else:
                self._next_update_id = max(self._next_update_id, update_id + 1)
            self._updates.append(
                {
                    "update_id": update_id,
                    "message": {
                        "message_id": 5000 + update_id,
                        "date": 1759500000,
                        "chat": {"id": chat_id, "type": "private"},
                        "from": {"id": chat_id, "first_name": first_name, "username": username, "language_code": "en"},
                        "text": text,
                    },
                }
            )
            return update_id

    def sent_messages(self) -> list[dict]:
        with self._lock:
            return [dict(item) for item in self._sent]

    def webhook_url(self) -> str:
        with self._lock:
            return self._webhook_url

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()

    # ── 请求处理 ─────────────────────────────────────────────────────────
    def _handle(self, handler: BaseHTTPRequestHandler) -> None:
        path = handler.path.split("?", 1)[0]
        if path == "/_control/inject" and handler.command == "POST":
            payload = handler._read_json()
            self.inject_message(
                int(payload.get("chat_id", 1)),
                str(payload.get("text", "")),
                first_name=str(payload.get("first_name", "Buyer")),
                username=str(payload.get("username", "")),
            )
            handler._json({"ok": True})
            return
        if path == "/_control/sent" and handler.command == "GET":
            handler._json({"ok": True, "result": self.sent_messages()})
            return
        if path == "/_control/updates" and handler.command == "GET":
            with self._lock:
                handler._json({"ok": True, "result": list(self._updates)})
            return
        parts = path.split("/")
        if len(parts) == 3 and parts[1].startswith("bot"):
            method = parts[2]
            payload = handler._read_json()
            if method == "getMe":
                handler._json({"ok": True, "result": {"id": 999, "is_bot": True, "username": "mock_seller_bot"}})
                return
            if method == "getUpdates":
                offset = int(payload.get("offset") or 0)
                with self._lock:
                    pending = [item for item in self._updates if int(item["update_id"]) >= offset]
                handler._json({"ok": True, "result": pending})
                return
            if method == "sendMessage":
                with self._lock:
                    self._next_message_id += 1
                    record = {
                        "message_id": self._next_message_id,
                        "chat": {"id": payload.get("chat_id")},
                        "text": str(payload.get("text", "")),
                        "parse_mode": str(payload.get("parse_mode", "")),
                    }
                    self._sent.append(record)
                handler._json({"ok": True, "result": record})
                return
            if method in ("setWebhook", "deleteWebhook"):
                with self._lock:
                    self._webhook_url = str(payload.get("url", "")) if method == "setWebhook" else ""
                handler._json({"ok": True, "result": True})
                return
        handler._json({"ok": False, "description": "not found"}, 404)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8151)
    args = parser.parse_args()
    server = MockTelegramServer(args.host, args.port)
    print(f"模拟 Telegram Bot API 就绪：{server.base_url()}<令牌>/getUpdates|sendMessage")
    print("控制面：POST /_control/inject 注入消息；GET /_control/sent 查看外发；Ctrl+C 退出。")
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        pass
    finally:
        server.stop()


if __name__ == "__main__":
    main()
