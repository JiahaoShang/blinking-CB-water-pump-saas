#!/usr/bin/env python3
"""试点前检查：买家 API 的跨域白名单只放行登记过的官网域名，并拒绝陌生域名。

用法:
  python3 tools/pilot_checks/check_cors.py --base http://127.0.0.1:8137 \\
      --site-key <演示库网站标识> --allowed-origin https://seller-a.example

说明：会创建 1-2 个空会话用于验证，不影响既有数据。
"""
from __future__ import annotations

import argparse
import json
import urllib.error
import urllib.request


def call(base: str, path: str, method: str, origin: str | None = None,
         payload: dict | None = None, extra_headers: dict | None = None) -> tuple[int, dict, bytes]:
    headers = {"Content-Type": "application/json", **(extra_headers or {})}
    if origin:
        headers["Origin"] = origin
    request = urllib.request.Request(
        base + path, method=method,
        data=json.dumps(payload).encode() if payload is not None else None,
        headers=headers,
    )
    try:
        response = urllib.request.urlopen(request)
        return response.status, {k.lower(): v for k, v in response.headers.items()}, response.read()
    except urllib.error.HTTPError as error:
        return error.code, {k.lower(): v for k, v in error.headers.items()}, error.read()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", default="http://127.0.0.1:8137")
    parser.add_argument("--site-key", required=True)
    parser.add_argument("--allowed-origin", required=True, help="卖家官网登记域名，如 https://seller-a.example")
    parser.add_argument("--evil-origin", default="https://not-registered.example")
    args = parser.parse_args()

    failed = 0

    def report(name: str, ok: bool, detail: str) -> None:
        nonlocal failed
        failed += 0 if ok else 1
        print(f"[{'PASS' if ok else 'FAIL'}] {name} — {detail}")

    status, headers, _ = call(args.base, "/api/v1/buyer/conversations", "POST",
                              origin=args.allowed_origin, payload={"site_key": args.site_key})
    report("已登记官网域名可创建会话且响应带跨域放行头",
           status == 201 and headers.get("access-control-allow-origin") == args.allowed_origin,
           f"status={status} ACAO={headers.get('access-control-allow-origin')}")

    status, headers, _ = call(args.base, "/api/v1/buyer/conversations", "POST",
                              origin=args.evil_origin, payload={"site_key": args.site_key})
    report("陌生域名被拒绝且响应不含跨域放行头（浏览器端会被拦下）",
           status == 403 and "access-control-allow-origin" not in headers,
           f"status={status} ACAO={headers.get('access-control-allow-origin')}")

    status, headers, _ = call(args.base, "/api/v1/buyer/conversations", "OPTIONS",
                              origin=args.allowed_origin, extra_headers={"Access-Control-Request-Method": "POST"})
    report("预检 OPTIONS 放行已登记域名并允许幂等键头",
           status == 204 and headers.get("access-control-allow-origin") == args.allowed_origin
           and "idempotency-key" in (headers.get("access-control-allow-headers") or "").lower(),
           f"status={status} allow-headers={headers.get('access-control-allow-headers')}")

    status, headers, _ = call(args.base, "/api/v1/buyer/conversations", "OPTIONS",
                              origin=args.evil_origin, extra_headers={"Access-Control-Request-Method": "POST"})
    report("预检 OPTIONS 拒绝陌生域名", status == 403 and "access-control-allow-origin" not in headers,
           f"status={status}")

    status, _, _ = call(args.base, "/api/v1/buyer/conversations", "POST", payload={"site_key": args.site_key})
    report("无 Origin 的非浏览器调用可用（同机集成/官网服务端转发场景）", status == 201, f"status={status}")

    status, headers, body = call(args.base, "/api/v1/buyer/handoff/exchange", "POST",
                                 origin=args.evil_origin, payload={"code": "unused-code", "product_id": "unused-product"},
                                 extra_headers={"X-Site-Key": args.site_key})
    report("交接交换接口拒绝陌生域名（不返回跨域放行头）",
           status == 403 and b'"origin_not_allowed"' in body and "access-control-allow-origin" not in headers,
           f"status={status} ACAO={headers.get('access-control-allow-origin')}")

    status, headers, body = call(args.base, "/api/v1/buyer/handoff/exchange", "POST",
                                 origin=args.base.rstrip("/"), payload={"code": "unused-code", "product_id": "unused-product"},
                                 extra_headers={"X-Site-Key": args.site_key})
    report("交接交换接口只在平台自身来源受理（垃圾参数被拒且不放行跨域头）",
           status == 403 and b'"handoff_binding_mismatch"' in body and "access-control-allow-origin" not in headers,
           f"status={status} ACAO={headers.get('access-control-allow-origin')}")

    print("结果：", "PASS — 跨域白名单放行/拒绝行为正确" if failed == 0 else f"FAIL — {failed} 项不通过")
    raise SystemExit(0 if failed == 0 else 1)


if __name__ == "__main__":
    main()
