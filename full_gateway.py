#!/usr/bin/env python3
"""Password-gated reverse proxy for a temporary full-feature demo tunnel."""

from __future__ import annotations

import base64
import http.client
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


UPSTREAM_HOST = "127.0.0.1"
UPSTREAM_PORT = 8001
MAX_BODY_BYTES = 8 * 1024 * 1024


def expected_authorization() -> str:
    user = os.environ.get("GROWAVE_GATEWAY_USER", "growave-demo")
    password = os.environ.get("GROWAVE_GATEWAY_PASSWORD", "")
    token = base64.b64encode(f"{user}:{password}".encode("utf-8")).decode("ascii")
    return f"Basic {token}"


class FullGatewayHandler(BaseHTTPRequestHandler):
    server_version = "GrowaveFullGateway/1.0"

    def _authorized(self) -> bool:
        if os.environ.get("GROWAVE_GATEWAY_PASSWORD"):
            return self.headers.get("Authorization", "") == expected_authorization()
        return False

    def _require_auth(self) -> bool:
        if self._authorized():
            return True
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="Growave demo"')
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        return False

    def _forward(self) -> None:
        if not self._require_auth():
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self.send_error(400, "Invalid content length")
            return
        if length > MAX_BODY_BYTES:
            self.send_error(413, "Request body too large")
            return
        body = self.rfile.read(length) if length else None
        headers = {}
        for name in ("Accept", "Accept-Encoding", "Content-Type", "Cookie", "Referer", "User-Agent"):
            if self.headers.get(name):
                headers[name] = self.headers[name]
        if body is not None:
            headers["Content-Length"] = str(len(body))
        connection = http.client.HTTPConnection(UPSTREAM_HOST, UPSTREAM_PORT, timeout=10)
        try:
            connection.request(self.command, self.path, body=body, headers=headers)
            response = connection.getresponse()
            payload = response.read()
        except (OSError, http.client.HTTPException) as exc:
            self.send_error(502, f"Growave local service unavailable: {exc}")
            return
        finally:
            connection.close()
        self.send_response(response.status, response.reason)
        skip = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te", "trailers", "transfer-encoding", "upgrade", "server", "date"}
        for name, value in response.getheaders():
            if name.lower() not in skip:
                self.send_header(name, value)
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(payload)

    def do_GET(self) -> None:
        self._forward()

    def do_HEAD(self) -> None:
        self._forward()

    def do_POST(self) -> None:
        self._forward()

    def log_message(self, format: str, *args: object) -> None:
        print(f"[full-gateway] {format % args}")


def main() -> None:
    if not os.environ.get("GROWAVE_GATEWAY_PASSWORD"):
        raise SystemExit("GROWAVE_GATEWAY_PASSWORD must be set")
    server = ThreadingHTTPServer(("127.0.0.1", 8003), FullGatewayHandler)
    print("Growave full-feature gateway running at http://127.0.0.1:8003")
    server.serve_forever()


if __name__ == "__main__":
    main()
