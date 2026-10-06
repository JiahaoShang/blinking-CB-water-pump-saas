#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Telegram 长轮询收信（本地开发 / 无公网环境；生产可改用 Webhook）。

用法：
    python3 tools/channels/telegram_poller.py --db data/demo.db            # 持续轮询
    python3 tools/channels/telegram_poller.py --db data/demo.db --once     # 拉取一轮后退出（演示/测试）

要求：
    - 本机环境变量 TELEGRAM_BOT_TOKEN 已设置（不要把令牌写进任何文件）；
    - 渠道已在「设置与治理 › 渠道接入」启用（企业由渠道账户自动定位，或 --tenant 指定）；
    - 模拟联调时用 TELEGRAM_API_BASE 指向 tools/channels/mock_telegram.py 的地址。

说明：处理逻辑与 Webhook 完全共用（app.handle_telegram_update）；按消息去重，重复拉取不会重复处理。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import app as app_module  # noqa: E402


def telegram_call(base_url: str, token: str, method: str, payload: dict, timeout: int) -> dict:
    data = app_module.channel_http_json(f"{base_url}{token}/{method}", payload, timeout=timeout)
    if not data.get("ok"):
        raise app_module.ChannelSendError(str(data.get("description") or "渠道接口返回失败"))
    return data


def resolve_tenant(db, explicit: str) -> str:
    if explicit:
        return explicit
    rows = db.conn.execute("SELECT tenant_id, config_json FROM channel_accounts WHERE channel='telegram'").fetchall()
    tenants = [row["tenant_id"] for row in rows]
    if len(tenants) == 1:
        return tenants[0]
    if not tenants:
        raise SystemExit("还没有配置任何 Telegram 渠道账户：请先在「设置与治理 › 渠道接入」保存配置。")
    raise SystemExit("存在多个企业的 Telegram 渠道账户；请用 --tenant 指定其一。")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=str(ROOT / "data" / "demo.db"), help="SQLite 数据库路径（默认演示库）")
    parser.add_argument("--tenant", default="", help="企业 ID（默认按 Telegram 渠道账户自动定位）")
    parser.add_argument("--api-base", default="", help="Bot API 基地址（默认读 TELEGRAM_API_BASE 或官方地址）")
    parser.add_argument("--once", action="store_true", help="拉取一轮后退出")
    parser.add_argument("--timeout", type=int, default=25, help="getUpdates 长轮询超时秒数")
    parser.add_argument("--interval", type=float, default=1.0, help="空轮询间隔秒数")
    args = parser.parse_args()

    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        raise SystemExit("缺少环境变量 TELEGRAM_BOT_TOKEN（请在本机环境中设置后再运行）。")
    base = (args.api_base or os.environ.get("TELEGRAM_API_BASE", "") or "https://api.telegram.org/bot").rstrip("/")

    db = app_module.Database(Path(args.db))
    ai = app_module.AIService(app_module.AIConfig.from_env())
    tenant_id = resolve_tenant(db, args.tenant)
    print(f"Telegram 轮询就绪：tenant={tenant_id} db={args.db} base={base}")

    def channel_enabled() -> bool:
        account = db.get_channel_account(tenant_id, "telegram")
        return bool(account and account["status"] == "enabled")

    offset = 0
    while True:
        try:
            if not channel_enabled():
                # 与 Webhook 口径一致：停用渠道不收任何消息（不消费、不落库）。
                print("渠道未启用：暂停收信；在「设置与治理 › 渠道接入」启用后自动继续。")
                if args.once:
                    return
                time.sleep(max(args.interval, 5.0))
                continue
            try:
                data = telegram_call(base, token, "getUpdates", {"offset": offset + 1 if offset else 0, "timeout": args.timeout}, timeout=max(args.timeout + 10, 30))
            except app_module.ChannelSendError as error:
                print(f"拉取失败：{error}")
                if args.once:
                    raise SystemExit(2)
                time.sleep(max(args.interval, 5.0))
                continue
            updates = data.get("result") or []
            for update in updates:
                try:
                    offset = max(offset, int(update.get("update_id") or 0))
                except (TypeError, ValueError):
                    pass
                try:
                    outcome = app_module.handle_telegram_update(db, ai, tenant_id, update)
                except Exception as error:  # 单条失败不阻塞后续
                    print(f"update {update.get('update_id')} 处理失败：{error}")
                    continue
                if outcome:
                    print(json.dumps({"update_id": update.get("update_id"), **outcome}, ensure_ascii=False))
            if args.once:
                print(f"本轮完成：处理 {len(updates)} 条更新。")
                return
            if not updates:
                time.sleep(args.interval)
        except Exception as error:
            # 兜底：任何未预期异常都不允许让轮询终止（现场曾出现进程意外退出且无留痕）
            print(f"轮询循环未预期异常（将自动重试）：{error!r}", flush=True)
            if args.once:
                raise
            time.sleep(max(args.interval, 5.0))


if __name__ == "__main__":
    main()
