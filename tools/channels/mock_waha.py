# -*- coding: utf-8 -*-
"""WAHA（WhatsApp HTTP API）形状的最小模拟服务（测试用）。

覆盖本平台用到的端点：
- POST sendText / sendSeen / startTyping / stopTyping；
- GET /api/sessions、GET /api/sessions/{name}（会话状态）；
- GET /api/{session}/auth/qr?format=image（二维码，仅扫码态返回图片）；
- POST /api/sessions/{name}/start、POST /api/sessions（启动会话）。
X-Api-Key 不匹配时按 WAHA 形状返回 401。发送记录可供断言。
"""
import json
import struct
import threading
import zlib
from http.server import BaseHTTPRequestHandler, HTTPServer


def _tiny_png() -> bytes:
    """1×1 PNG（合法文件即可，测试断言 PNG 魔数与 Content-Type）。"""

    def chunk(tag: bytes, payload: bytes) -> bytes:
        return struct.pack(">I", len(payload)) + tag + payload + struct.pack(">I", zlib.crc32(tag + payload) & 0xFFFFFFFF)

    ihdr = struct.pack(">IIBBBBB", 1, 1, 8, 6, 0, 0, 0)
    idat = zlib.compress(struct.pack(">B4B", 0, 255, 0, 0, 255))
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", idat) + chunk(b"IEND", b"")


class MockWahaServer:
    def __init__(self, api_key: str = "waha-test-key", status: str = "WORKING", session_name: str = "default"):
        self.api_key = api_key
        self.status = status
        self.session_name = session_name
        self.start_calls = 0
        self.restart_calls = 0
        self.stop_calls = 0
        self.deleted = 0
        self.logout_calls = 0
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

            def _send_png(self, data: bytes) -> None:
                self.send_response(200)
                self.send_header("Content-Type", "image/png")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def _parts(self) -> list:
                return [segment for segment in self.path.split("?", 1)[0].split("/") if segment]

            def do_GET(self):
                if not self._auth_ok():
                    self._send(401, {"message": "Unauthorized", "statusCode": 401})
                    return
                parts = self._parts()
                if parts == ["api", "sessions"]:
                    self._send(200, [mock.session_payload()])
                    return
                if len(parts) == 3 and parts[:2] == ["api", "sessions"]:
                    if parts[2] != mock.session_name:
                        self._send(404, {"message": "Session not found", "statusCode": 404})
                        return
                    self._send(200, mock.session_payload())
                    return
                if len(parts) == 4 and parts[0] == "api" and parts[2:] == ["auth", "qr"]:
                    if parts[1] != mock.session_name:
                        self._send(404, {"message": "Session not found", "statusCode": 404})
                        return
                    if mock.status != "SCAN_QR_CODE":
                        self._send(400, {"message": "Session is not in SCAN_QR_CODE status", "statusCode": 400})
                        return
                    self._send_png(_tiny_png())
                    return
                self._send(404, {"message": f"unsupported path {self.path}", "statusCode": 404})

            def do_DELETE(self):
                if not self._auth_ok():
                    self._send(401, {"message": "Unauthorized", "statusCode": 401})
                    return
                parts = self._parts()
                if len(parts) == 3 and parts[:2] == ["api", "sessions"]:
                    if parts[2] != mock.session_name:
                        self._send(404, {"message": "Session not found", "statusCode": 404})
                        return
                    with mock._lock:
                        mock.deleted += 1
                        mock.status = "STOPPED"
                    self._send(201, {"name": mock.session_name, "status": "STOPPED"})
                    return
                self._send(404, {"message": f"unsupported path {self.path}", "statusCode": 404})

            def do_POST(self):
                payload = self._read_json()
                if not self._auth_ok():
                    self._send(401, {"message": "Unauthorized", "statusCode": 401})
                    return
                path = self.path.split("?", 1)[0]
                parts = self._parts()
                if path == "/api/sendText":
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
                if path in ("/api/sendSeen", "/api/startTyping", "/api/stopTyping"):
                    with mock._lock:
                        mock._notifications.append({"path": path, "chatId": payload.get("chatId")})
                    self._send(201, {"ok": True})
                    return
                if len(parts) == 4 and parts[:2] == ["api", "sessions"] and parts[3] == "start":
                    if parts[2] != mock.session_name:
                        self._send(404, {"message": "Session not found", "statusCode": 404})
                        return
                    with mock._lock:
                        mock.start_calls += 1
                        mock.status = "SCAN_QR_CODE"
                    self._send(201, {"name": mock.session_name, "status": "STARTING"})
                    return
                if len(parts) == 4 and parts[:2] == ["api", "sessions"] and parts[3] == "logout":
                    if parts[2] != mock.session_name:
                        self._send(404, {"message": "Session not found", "statusCode": 404})
                        return
                    with mock._lock:
                        mock.logout_calls += 1
                        mock.status = "STOPPED"
                    self._send(201, {"name": mock.session_name, "status": "STOPPED"})
                    return
                if len(parts) == 4 and parts[:2] == ["api", "sessions"] and parts[3] == "restart":
                    if parts[2] != mock.session_name:
                        self._send(404, {"message": "Session not found", "statusCode": 404})
                        return
                    with mock._lock:
                        mock.restart_calls += 1
                        mock.status = "SCAN_QR_CODE"
                    self._send(201, {"name": mock.session_name, "status": "STARTING"})
                    return
                if len(parts) == 4 and parts[:2] == ["api", "sessions"] and parts[3] == "stop":
                    if parts[2] != mock.session_name:
                        self._send(404, {"message": "Session not found", "statusCode": 404})
                        return
                    with mock._lock:
                        mock.stop_calls += 1
                        mock.status = "STOPPED"
                    self._send(201, {"name": mock.session_name, "status": "STOPPED"})
                    return
                if parts == ["api", "sessions"]:
                    name = str(payload.get("name") or mock.session_name)
                    if name != mock.session_name:
                        self._send(409, {"message": "Session with this name already exists", "statusCode": 409})
                        return
                    with mock._lock:
                        mock.start_calls += 1
                        mock.status = "SCAN_QR_CODE"
                    self._send(201, {"name": mock.session_name, "status": "STARTING"})
                    return
                self._send(404, {"message": f"unsupported path {self.path}", "statusCode": 404})

        self._server = HTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    def session_payload(self) -> dict:
        me = {"id": "861700000999@c.us", "pushName": "MockUser"} if self.status == "WORKING" else None
        return {"name": self.session_name, "status": self.status, "me": me, "engine": {"engine": "NOWEB"}}

    def set_status(self, status: str) -> None:
        with self._lock:
            self.status = status

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
