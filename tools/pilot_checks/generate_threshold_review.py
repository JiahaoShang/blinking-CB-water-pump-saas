#!/usr/bin/env python3
"""生成选型规则核准单：已发布产品的匹配依据 + 样例工况判定，供企业工程师逐行签字。

用法:
  python3 tools/pilot_checks/generate_threshold_review.py --db data/slice-m1m2.db \\
      --out data/pilot/threshold-review-sheet.csv

说明：试点前由企业工程师核对第一段“产品事实”与第二段“样例工况判定”的每一行，
在“工程师结论”列填写 同意/不同意/修改为…，签字后本表是阈值生效的书面依据。
"""

from __future__ import annotations
import argparse
import csv
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from app import Database, classify_products  # noqa: E402  （复用平台的匹配规则本体）

TENANT = "demo-tenant"
SAMPLES = [
    ("A 完整工况（流量/扬程/介质/温度齐全）", {"flow": {"value": "50", "unit": "m³/h"}, "head": {"value": "45", "unit": "m"}, "media": {"value": "clean water", "unit": ""}, "temperature": {"value": "60", "unit": "°C"}}),
    ("B 缺扬程", {"flow": {"value": "50", "unit": "m³/h"}, "media": {"value": "clean water", "unit": ""}}),
    ("C 扬程远超目录曲线", {"flow": {"value": "50", "unit": "m³/h"}, "head": {"value": "300", "unit": "m"}, "media": {"value": "clean water", "unit": ""}}),
    ("D 温度较高工况", {"flow": {"value": "50", "unit": "m³/h"}, "media": {"value": "clean water", "unit": ""}, "temperature": {"value": "100", "unit": "°C"}}),
]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--tenant", default=TENANT)
    args = parser.parse_args()

    db = Database(args.db, initialize=False)
    rows: list[list[str]] = []

    for product in db.conn.execute(
        "SELECT p.id, p.model, v.version, v.snapshot_json FROM products p "
        "JOIN product_versions v ON v.product_id = p.id AND v.status = 'published' "
        "WHERE p.tenant_id = ? AND p.status = 'published' ORDER BY p.model",
        (args.tenant,),
    ):
        snapshot = json.loads(product["snapshot_json"])
        for field in snapshot.get("fields", []):
            rows.append([
                "产品事实", f"{product['model']} v{product['version']}",
                field.get("label", field.get("field_key", "")),
                f"{field.get('value', '')} {field.get('unit', '')}".strip(),
                field.get("source", ""), "", "",
            ])

    for label, values in SAMPLES:
        results, missing = classify_products(db, args.tenant, values)
        verdict = "；".join(f"{item['model']}={item['status']}" for item in results) or "（无候选）"
        rows.append([
            "样例工况判定", label, verdict,
            f"缺项：{'、'.join(missing)}" if missing else "",
            "平台演示规则", "", "",
        ])

    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.writer(handle)
        writer.writerow(["段落", "型号/工况", "字段/判定", "值/说明", "出处", "工程师结论（同意/不同意/改为）", "工程师备注"])
        writer.writerows(rows)
    print(f"已生成核准单：{args.out}（{len(rows)} 行）")
    print("下一步：企业工程师逐行核对第 6 列并签字；结论与修改须回填系统后重新发布产品。")


if __name__ == "__main__":
    main()
