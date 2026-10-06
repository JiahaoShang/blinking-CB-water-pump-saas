#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""出站总线补投工人：把卡在「排队中」的外发消息补投出去（进程中断恢复）。

正常发送是「先排队再立即投递」；工人只处理异常残留（进程在投递前中断）。
补投失败的会转为「发送失败」并记录原因，等待人工在收件箱/销售台重试；绝不重发已成功消息。

用法：
    python3 tools/channels/outbound_worker.py --db data/demo.db --once     # 补投一轮后退出（演示/测试）
    python3 tools/channels/outbound_worker.py --db data/demo.db             # 持续补投（默认 5 秒间隔）

默认只处理创建超过 20 秒仍处于「排队中」的消息（避免抢正在投递的消息）；测试可用 --min-age 0。
"""
from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import app as app_module  # noqa: E402


def resolve_tenant(db, explicit: str) -> str:
    if explicit:
        return explicit
    rows = db.conn.execute("SELECT tenant_id FROM channel_accounts").fetchall()
    tenants = sorted({row["tenant_id"] for row in rows})
    if len(tenants) == 1:
        return tenants[0]
    if not tenants:
        raise SystemExit("还没有配置任何渠道账户：请先在「设置与治理 › 渠道接入」保存配置。")
    raise SystemExit("多个企业配置了渠道账户；请用 --tenant 指定其一。")


def flush_once(db, tenant_id: str, min_age: int) -> int:
    cutoff = datetime.now(timezone.utc) - timedelta(seconds=min_age)
    rows = db.conn.execute(
        "SELECT id, created_at FROM messages WHERE tenant_id=? AND delivery_status='queued' AND channel<>'' ORDER BY rowid",
        (tenant_id,),
    ).fetchall()
    processed = 0
    for row in rows:
        try:
            created = datetime.fromisoformat(str(row["created_at"]))
        except ValueError:
            created = None
        if created is not None and created > cutoff:
            continue
        sent, error = app_module.dispatch_outbound(db, tenant_id, row["id"])
        processed += 1
        print(f"{'已补投' if sent else '仍失败'}：{row['id']}{(' · ' + error) if error else ''}")
    return processed


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=str(ROOT / "data" / "demo.db"), help="SQLite 数据库路径（默认演示库）")
    parser.add_argument("--tenant", default="", help="企业 ID（默认按渠道账户自动定位）")
    parser.add_argument("--once", action="store_true", help="补投一轮后退出")
    parser.add_argument("--interval", type=float, default=5.0, help="持续模式间隔秒数")
    parser.add_argument("--min-age", type=int, default=20, help="只补投创建超过该秒数仍排队的消息")
    args = parser.parse_args()

    db = app_module.Database(Path(args.db))
    tenant_id = resolve_tenant(db, args.tenant)
    print(f"出站补投就绪：tenant={tenant_id} db={args.db} min_age={args.min_age}s")
    if args.once:
        processed = flush_once(db, tenant_id, args.min_age)
        print(f"本轮完成：补投 {processed} 条。")
        return
    while True:
        processed = flush_once(db, tenant_id, args.min_age)
        if processed:
            print(f"补投 {processed} 条。")
        time.sleep(args.interval)


if __name__ == "__main__":
    main()
