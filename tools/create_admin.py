#!/usr/bin/env python3
"""首次部署：为普通数据库显式创建第一位管理员。

普通新库不预置任何账号（无默认口令、无演示用户）。生产/试点部署在启动前
用本命令创建一个管理员，再通过成员管理页创建其余账号。

用法:
  python3 tools/create_admin.py --db data/app.db --email admin@example.com --name "Site Admin"
      → 交互式输入两遍密码（不回显、不进入 shell 历史）

  python3 tools/create_admin.py --db data/app.db --email admin@example.com --name "Site Admin" \
      --password-stdin
      → 从标准输入读一行密码，供部署脚本/CI 使用（仍不会出现在命令行历史里）

可选:
  --tenant default        企业标识（同一数据库多企业时区分；默认 default）
  --tenant-name "名称"    企业显示名称（默认与标识相同）

说明:
  - 本命令与演示命令互不相干；本地演示环境请用 tools/demo/start_demo.py
    （它创建文档化的演示租户与 demo-admin/demo-sales/demo-engineer，仅限本地演示）。
  - 密码不得写入脚本、README 示例或默认参数；本工具不生成默认口令。
"""
from __future__ import annotations

import argparse
import getpass
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app import Database  # noqa: E402


def create_admin(db_path: str | Path, email: str, display_name: str, password: str,
                 tenant_id: str = "default", tenant_name: str | None = None) -> str:
    """Create the first administrator in an ordinary database; returns the user id."""
    if len(password) < 8:
        raise ValueError("密码至少需要 8 位")
    db = Database(db_path)
    try:
        db.ensure_tenant(tenant_id, tenant_name or tenant_id)
        user_id = db.create_user(tenant_id, email, display_name, "admin", password, actor="create-admin-cli")
    finally:
        db.conn.close()
    return user_id


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, required=True, help="目标数据库路径（不存在则新建结构）")
    parser.add_argument("--email", required=True, help="管理员登录邮箱")
    parser.add_argument("--name", required=True, help="管理员显示名称")
    parser.add_argument("--tenant", default="default", help="企业标识（默认 default）")
    parser.add_argument("--tenant-name", default=None, help="企业显示名称（默认与标识相同）")
    parser.add_argument("--password-stdin", action="store_true", help="从标准输入读取一行密码（供部署脚本使用）")
    args = parser.parse_args()

    if args.password_stdin:
        password = sys.stdin.readline().rstrip("\n")
        if not password:
            raise SystemExit("标准输入没有可用密码")
    else:
        password = getpass.getpass("管理员密码（至少 8 位）: ")
        confirm = getpass.getpass("再次输入密码: ")
        if password != confirm:
            raise SystemExit("两次输入的密码不一致，未创建账号")

    try:
        user_id = create_admin(args.db, args.email.strip(), args.name.strip(), password, args.tenant, args.tenant_name)
    except ValueError as exc:
        raise SystemExit(f"未创建账号：{exc}")
    print(f"已创建管理员账号：{args.email.strip()}（企业 {args.tenant}，ID {user_id}）")
    print("请从 /login 登录后创建其余成员；如需本地演示环境，请改用 tools/demo/start_demo.py。")


if __name__ == "__main__":
    main()
