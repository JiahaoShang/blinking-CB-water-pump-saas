#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Telegram 真机联调自检：验证机器人令牌可用（getMe），可选给指定 chat 发送一条测试消息。

用法：
    export TELEGRAM_BOT_TOKEN='<BotFather 给的令牌>'    # 只放本机环境变量，不要写在文件里
    python3 tools/channels/check_telegram_live.py                      # 仅验证令牌（getMe）
    python3 tools/channels/check_telegram_live.py --chat-id 123456789  # 再发一条测试消息

备注：令牌与返回值里的敏感字段不会打印；联调时如走模拟服务，用 --api-base 指向 mock_telegram.py 的地址。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request


def api_call(base: str, token: str, method: str, payload: dict) -> dict:
    request = urllib.request.Request(
        f"{base}{token}/{method}",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        raw = error.read().decode("utf-8", errors="replace")
        return {"ok": False, "description": f"HTTP {error.code}: {raw[:200]}"}
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        return {"ok": False, "description": f"网络失败：{error}"}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api-base", default="", help="Bot API 基地址（默认读 TELEGRAM_API_BASE 或官方地址）")
    parser.add_argument("--chat-id", default="", help="测试消息的接收 chat id（可选）")
    parser.add_argument("--text", default="Alpsaga 渠道联调测试消息：收到即说明发送通道已通。", help="测试消息内容")
    args = parser.parse_args()

    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        print("缺少环境变量 TELEGRAM_BOT_TOKEN。")
        raise SystemExit(2)
    base = (args.api_base or os.environ.get("TELEGRAM_API_BASE", "") or "https://api.telegram.org/bot").rstrip("/")

    me = api_call(base, token, "getMe", {})
    if not me.get("ok"):
        print(f"getMe 失败：{me.get('description', '未知错误')}")
        raise SystemExit(3)
    result = me.get("result") or {}
    print(f"getMe 成功：机器人 id={result.get('id')} username=@{result.get('username', '未知')}")

    if args.chat_id:
        sent = api_call(base, token, "sendMessage", {"chat_id": args.chat_id, "text": args.text})
        if not sent.get("ok"):
            print(f"sendMessage 失败：{sent.get('description', '未知错误')}")
            raise SystemExit(3)
        message_id = (sent.get("result") or {}).get("message_id")
        print(f"sendMessage 成功：消息 id={message_id}")
    print("自检通过。")


if __name__ == "__main__":
    main()
