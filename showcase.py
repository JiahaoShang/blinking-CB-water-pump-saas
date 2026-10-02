#!/usr/bin/env python3
"""Read-only public showcase gateway for local Growave demos.

This intentionally exposes only published product pages. Administrative pages,
login, inquiry writes, RFQs, cookies, and request bodies never pass through.
"""

from __future__ import annotations

import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.error import URLError
from urllib.request import Request, urlopen
from urllib.parse import urlparse


UPSTREAM = "http://127.0.0.1:8001"
ALLOWED_PRODUCTS = {"3sv08nl007t", "cb-80a", "cb-120c"}


def landing_page() -> bytes:
    return """<!doctype html><html lang='zh-CN'><meta charset='utf-8'>
<meta name='viewport' content='width=device-width,initial-scale=1'>
<title>Growave | Industrial pump showcase</title>
<style>
:root{color-scheme:dark;font-family:ui-sans-serif,system-ui,sans-serif;background:#08070f;color:#f7f4ff}
body{max-width:980px;margin:0 auto;padding:48px 22px;background:radial-gradient(circle at 80% 0,#32176b 0,transparent 42%),#08070f}
.eyebrow{color:#c4b5fd;letter-spacing:.12em;text-transform:uppercase;font-size:12px}.hero{padding:34px 0 28px}
h1{font-size:clamp(38px,7vw,72px);line-height:1;margin:10px 0 16px}.muted{color:#aaa2bb;max-width:700px;line-height:1.7}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:16px;margin-top:28px}.card{display:block;padding:22px;border:1px solid #30254a;border-radius:18px;background:rgba(20,17,33,.88);color:inherit;text-decoration:none}
.card:hover{border-color:#a855f7;transform:translateY(-2px)}.tag{display:inline-block;color:#9af2da;font-size:12px;margin-bottom:8px}
</style><body><div class='hero'><div class='eyebrow'>Growave · private demo showcase</div>
<h1>Industrial pump intelligence.</h1><p class='muted'>这是 Growave 的只读产品展示入口。产品事实来自已核准资料；最终选型、价格、库存和交期需要销售与工程师确认。</p></div>
<div class='grid'><a class='card' href='/products/3sv08nl007t?lang=zh'><span class='tag'>LOWARA · INTERNAL DEMO</span><h2>3SV08NL007T</h2><p class='muted'>Lowara e-SV 立式多级离心泵 · AISI 316 · 0.75 kW</p></a>
<a class='card' href='/products/cb-80a?lang=zh'><span class='tag'>GROWAVE DEMO</span><h2>CB-80A</h2><p class='muted'>清水循环和一般供水演示型号</p></a>
<a class='card' href='/products/cb-120c?lang=zh'><span class='tag'>GROWAVE DEMO</span><h2>CB-120C</h2><p class='muted'>弱腐蚀介质场景演示型号</p></a></div></body></html>""".encode("utf-8")


class ShowcaseHandler(BaseHTTPRequestHandler):
    server_version = "GrowaveShowcase/1.0"

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path in {"", "/"}:
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            body = landing_page()
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        match = re.fullmatch(r"/products/([a-z0-9-]+)/?", parsed.path)
        if not match or match.group(1) not in ALLOWED_PRODUCTS:
            self.send_error(404, "Showcase route not available")
            return
        try:
            request = Request(UPSTREAM + parsed.path + (f"?{parsed.query}" if parsed.query else ""), method="GET")
            with urlopen(request, timeout=5) as response:
                body = response.read()
                content_type = response.headers.get("Content-Type", "text/html; charset=utf-8")
        except (URLError, TimeoutError) as exc:
            self.send_error(502, f"Local Growave service unavailable: {exc}")
            return
        if content_type.startswith("text/html"):
            text = body.decode("utf-8", errors="replace")
            text = re.sub(r"<header>.*?</header>", "", text, flags=re.DOTALL | re.IGNORECASE)
            body = text.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:
        self.send_error(405, "Read-only showcase")

    def log_message(self, format: str, *args: object) -> None:
        print(f"[showcase] {format % args}")


def main() -> None:
    server = ThreadingHTTPServer(("127.0.0.1", 8002), ShowcaseHandler)
    print("Growave showcase running at http://127.0.0.1:8002")
    server.serve_forever()


if __name__ == "__main__":
    main()
