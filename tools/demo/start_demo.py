#!/usr/bin/env python3
"""一键启动演示环境：必要时构建演示库 → 打印入口与账号 → 启动服务。

用法:
  python3 tools/demo/start_demo.py              # 默认 127.0.0.1:8137，数据 data/demo.db
  python3 tools/demo/start_demo.py --reset      # 删除并重建演示库（清掉上次演示产生的数据）
  python3 tools/demo/start_demo.py --check      # 只准备并打印就绪信息，不启动服务
  python3 tools/demo/start_demo.py --no-open    # 不自动打开浏览器

演示剧本见 docs/演示剧本.md；停止用 Ctrl+C。
"""
from __future__ import annotations

import argparse
import sys
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from app import make_server  # noqa: E402
from seed_demo import SITE_KEY, seed  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8137)
    parser.add_argument("--db", default="data/demo.db")
    parser.add_argument("--reset", action="store_true", help="删除并重建演示库")
    parser.add_argument("--check", action="store_true", help="只准备并打印，不启动服务")
    parser.add_argument("--no-open", action="store_true", help="不自动打开浏览器")
    args = parser.parse_args()

    db_path = Path(args.db)
    if not db_path.is_absolute():
        db_path = ROOT / db_path
    result = seed(db_path, reset=args.reset)
    site_key = str(result.get("site_key", SITE_KEY))
    buyer_url = f"http://{args.host}:{args.port}/reference-client?site_key={site_key}"
    admin_url = f"http://{args.host}:{args.port}/login"

    print("=" * 66)
    print("演示环境就绪" + ("（复用现有演示库；状态乱了加 --reset 重建）" if result.get("skipped") else "（演示库已构建）"))
    print(f"  演示库　: {db_path}")
    print(f"  买方入口: {buyer_url}")
    print(f"  卖家系统: {admin_url}")
    print("  演示账号: demo-admin@example.invalid / demo-admin（管理员，全模块）")
    print("            demo-sales@example.invalid / demo-sales（员工：RFQ 询价+销售工作台）")
    print("            demo-engineer@example.invalid / demo-engineer（员工：人工任务+RFQ 询价+销售工作台）")
    print("  演示型号: SLICE-T1 / SLICE-T2（可比合成演示数据，来源标注 DEMO）· SLICE-NOBASIS（无依据对照）")
    print("  演示剧本: docs/演示剧本.md")
    print("=" * 66)
    if args.check:
        return

    # 演示工具（页面按钮与 seed 端点）唯一的开启入口：只有本脚本以演示模式启动服务。
    server = make_server(db_path, host=args.host, port=args.port, demo_tools=True)
    if not args.no_open:
        webbrowser.open(admin_url)
        webbrowser.open(buyer_url)
    print(f"服务运行中: http://{args.host}:{args.port}（Ctrl+C 停止）")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止；演示数据保留在演示库中。")


if __name__ == "__main__":
    main()
