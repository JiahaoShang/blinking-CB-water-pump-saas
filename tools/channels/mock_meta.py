# -*- coding: utf-8 -*-
"""Meta Graph API 形状的最小模拟服务（测试用）：接收 /{version}/{phone_id}/messages 并记录发送。"""
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer


class MockMetaGraphServer:
    """模拟 graph.facebook.com 的发送端点；Bearer 令牌不匹配时按 Meta 形状回 401。"""

    def __init__(self, token: str = "meta-test-token"):
        self.token = token
        self._sent: list[dict] = []
        self._lock = threading.Lock()
        mock = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):  # 静默
                pass

            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b""
                auth = self.headers.get("Authorization", "")
                try:
                    payload = json.loads(raw.decode("utf-8"))
                except Exception:
                    payload = {}
                if auth != f"Bearer {mock.token}":
                    body = json.dumps({"error": {"message": "Invalid OAuth access token.", "code": 190}}).encode()
                    self.send_response(401)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                if "/messages" in self.path:
                    with mock._lock:
                        n = len(mock._sent) + 1
                        mock._sent.append({
                            "path": self.path,
                            "to": payload.get("to"),
                            "body": ((payload.get("text") or {}).get("body")),
                            "payload": payload,
                        })
                    body = json.dumps({
                        "messaging_product": "whatsapp",
                        "contacts": [{"input": payload.get("to"), "wa_id": payload.get("to")}],
                        "messages": [{"id": f"wamid.mock-{n}", "message_status": "accepted"}],
                    }).encode()
                else:
                    body = json.dumps({"error": {"message": f"unsupported path {self.path}", "code": 100}}).encode()
                    self.send_response(404)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self._server = HTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    def base_url(self) -> str:
        return f"http://127.0.0.1:{self._server.server_port}"

    def sent_messages(self) -> list:
        with self._lock:
            return list(self._sent)

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()
