# -*- coding: utf-8 -*-
"""WAHA（WhatsApp HTTP API）形状的最小模拟服务（测试用）。

覆盖本平台用到的端点：sendText / sendSeen / startTyping / stopTyping；
X-Api-Key 不匹配时按 WAHA 形状返回 401。发送记录可供断言。
"""
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer


class MockWahaServer:
    def __init__(self, api_key: str = "waha-test-key"):
        self.api_key = api_key
        self._sent: list[dict] = []
        self._notifications: list[dict] = []
        self._lock = threading.Lock()
        mock = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):  # 静默
                pass

            def _auth_ok(self) -> bool:
                return self.headers.get("X-Api-Key", "") == mock.api_key

            def _read_json(self) -> dict:
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b""
                try:
                    return json.loads(raw.decode("utf-8"))
                except Exception:
                    return {}

            def _send(self, status: int, payload: dict) -> None:
                body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self):
                payload = self._read_json()
                if not self._auth_ok():
                    self._send(401, {"message": "Unauthorized", "statusCode": 401})
                    return
                if self.path == "/api/sendText":
                    with mock._lock:
                        n = len(mock._sent) + 1
                        record = {
                            "chatId": payload.get("chatId"),
                            "text": payload.get("text"),
                            "session": payload.get("session"),
                        }
                        mock._sent.append(record)
                    self._send(201, {
                        "key": {"remoteJid": payload.get("chatId"), "fromMe": True, "id": f"3EB0MOCK{n}"},
                        "status": "PENDING",
                        "timestamp": 1700000000,
                    })
                    return
                if self.path in ("/api/sendSeen", "/api/startTyping", "/api/stopTyping"):
                    with mock._lock:
                        mock._notifications.append({"path": self.path, "chatId": payload.get("chatId")})
                    self._send(201, {"ok": True})
                    return
                self._send(404, {"message": f"unsupported path {self.path}", "statusCode": 404})

        self._server = HTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    def base_url(self) -> str:
        return f"http://127.0.0.1:{self._server.server_port}"

    def sent_texts(self) -> list:
        with self._lock:
            return list(self._sent)

    def notifications(self) -> list:
        with self._lock:
            return list(self._notifications)

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()
