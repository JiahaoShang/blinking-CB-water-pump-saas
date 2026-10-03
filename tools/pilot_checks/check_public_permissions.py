#!/usr/bin/env python3
"""试点前检查：已发布版本中的每个公开文件是否都有许可记录、哈希与可读文件。

用法:
  python3 tools/pilot_checks/check_public_permissions.py --db data/slice-m1m2.db
  python3 tools/pilot_checks/check_public_permissions.py --db data/slice-m1m2.db --register path/to/register.json

register.json（可选，试点时必填）:
  {"products": {"SLICE-T1": ["文件A.png", "手册.pdf"]}}
  说明：厂方书面授权清单的机器可读版；提供后本脚本会核对“平台要公开的每个文件
  都在厂方清单里”，防止平台公开了厂方没同意的文件。
"""

from __future__ import annotations
import argparse
import hashlib
import json
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--register", type=Path, help="厂方书面授权清单（JSON），试点前必填")
    args = parser.parse_args()

    conn = sqlite3.connect(str(args.db))
    conn.row_factory = sqlite3.Row
    register = json.loads(args.register.read_text(encoding="utf-8")) if args.register else None
    gaps: list[str] = []
    checked = 0

    products = conn.execute(
        "SELECT p.id, p.model, v.version, v.snapshot_json FROM products p "
        "JOIN product_versions v ON v.product_id = p.id AND v.status = 'published' "
        "WHERE p.status = 'published' ORDER BY p.model"
    ).fetchall()
    if not products:
        print("警告：没有任何已发布产品（试点前至少应有 1 个已核准并发布的产品）")

    for product in products:
        snapshot = json.loads(product["snapshot_json"])
        files = snapshot.get("public_sources", [])
        print(f"- {product['model']} v{product['version']}：公开文件 {len(files)} 份")
        for item in files:
            checked += 1
            label = f"{product['model']}/{item.get('filename')}"
            permission = conn.execute(
                "SELECT approved_by, approved_at FROM public_source_permissions WHERE source_id=? AND product_id=?",
                (item["id"], product["id"]),
            ).fetchone()
            if not permission:
                gaps.append(f"{label}: 在发布快照中但没有许可记录")
                continue
            if item.get("scope") not in ("exact_model", "series"):
                gaps.append(f"{label}: 归属 scope={item.get('scope')} 不在可公开范围")
            source = conn.execute("SELECT sha256, storage_path, scope FROM source_documents WHERE id=?", (item["id"],)).fetchone()
            if not source or not source["storage_path"]:
                gaps.append(f"{label}: 缺少本地存储路径")
                continue
            if source["scope"] != item.get("scope"):
                gaps.append(
                    f"{label}: 归属口径与发布快照不一致（快照 {item.get('scope')} / 当前 {source['scope']}）；"
                    "该文件不应再按旧版本公开，需重新核准并发布新版本"
                )
            file_path = ROOT / source["storage_path"]
            if not file_path.is_file():
                gaps.append(f"{label}: 存储文件不存在（{source['storage_path']}）")
                continue
            if source["sha256"] and digest(file_path) != source["sha256"]:
                gaps.append(f"{label}: 文件哈希与登记不一致（可能被替换）")
        if register is not None:
            authorized = set(register.get("products", {}).get(product["model"], []))
            published = {item.get("filename") for item in files}
            for extra in sorted(published - authorized):
                gaps.append(f"{product['model']}/{extra}: 平台要公开但不在厂方授权清单里")
            print(f"  厂方授权清单核对：授权 {len(authorized)} 份 / 平台公开 {len(published)} 份")

    orphan = conn.execute(
        "SELECT COUNT(*) FROM public_source_permissions WHERE source_id NOT IN (SELECT id FROM source_documents)"
    ).fetchone()[0]
    if orphan:
        gaps.append(f"存在 {orphan} 条指向缺失文件的许可记录")
    pending = conn.execute(
        "SELECT COUNT(*) FROM source_documents s WHERE s.storage_path IS NOT NULL "
        "AND s.id NOT IN (SELECT source_id FROM public_source_permissions)"
    ).fetchone()[0]
    print(f"已保存但未开放的文件（仅内部使用，正常）：{pending} 份")

    if register is None:
        print("提示：未提供厂方授权清单（--register）。机器校验通过也仅代表平台内部一致，")
        print("      试点前必须补充厂方书面授权清单并重新运行本脚本。")
    print("结果：", "PASS — 公开文件全部有许可、哈希、可读文件，且归属与快照一致" if not gaps else f"FAIL — {len(gaps)} 处缺口")
    for gap in gaps:
        print("  缺口:", gap)
    raise SystemExit(0 if not gaps else 1)


if __name__ == "__main__":
    main()
