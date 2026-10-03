#!/usr/bin/env python3
"""构建/刷新演示数据库：两款已发布演示产品（含 7 份获准公开文件）+ 演示账号 + 网站标识。

用法:
  python3 tools/demo/seed_demo.py --db data/demo.db          # 首次构建；已构建则跳过
  python3 tools/demo/seed_demo.py --db data/demo.db --reset  # 删除并重建（旧演示文件一并清理）

演示状态由本脚本确定性重建（与 2026-10 走查现场一致）：
  SLICE-T1  v1=仅字段 → 授权 7 份文件 → v2=字段+图库/图纸/资料（含 1 份系列归属图片）
  SLICE-NOBASIS  仅材质事实（用于展示"无水力依据不进入候选"）
产物：数据库写入 --db；文件写入 data/uploads/（沿用平台固定资料目录）。
"""
from __future__ import annotations

import argparse
import sqlite3
import struct
import sys
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from app import Database  # noqa: E402

TENANT = "demo-tenant"
TENANT_NAME = "示例水泵企业（演示）"
SITE_KEY = "demo-site-key"
SITE_ORIGIN = "http://127.0.0.1:8137"

PARAMS_TXT = """SLICE-T1 立式离心泵 · 内部 Demo 资料（非厂商正式承诺）
流量范围: 30-90 m³/h
扬程范围: 25-60 m
介质: 清水与低粘度工业循环水
材质: 铸铁泵体，不锈钢叶轮
功率: 22 kW
介质温度: 5-80 °C
备注：以上为联调演示值，正式对外前必须由企业授权人员核对来源。
"""

NOBASIS_TXT = """SLICE-NOBASIS 材质说明（联调演示，无水力参数）
材质: 铸铁泵体，不锈钢叶轮
"""

DXF_BYTES = """0
SECTION
2
HEADER
0
ENDSEC
0
SECTION
2
ENTITIES
0
LINE
8
0
10
0.0
20
0.0
11
320.0
21
180.0
0
LINE
8
0
10
0.0
20
180.0
11
320.0
21
0.0
0
ENDSEC
0
EOF
""".encode("utf-8")

STEP_BYTES = """ISO-10303-21;
HEADER;
FILE_NAME('SLICE-T1 外形图（联调演示）');
ENDSEC;
DATA;
#1=MECHANICAL_CONTEXT('pump','demo');
#2=CARTESIAN_POINT('origin',(0.,0.,0.));
ENDSEC;
END-ISO-10303-21;
""".encode("utf-8")


def make_png(width: int, height: int, top: tuple[int, int, int], bottom: tuple[int, int, int]) -> bytes:
    """生成一张竖直渐变感的演示图片（纯标准库：zlib + struct）。"""
    def chunk(tag: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    raw = bytearray()
    for y in range(height):
        ratio = y / max(1, height - 1)
        band = tuple(int(top[i] + (bottom[i] - top[i]) * ratio) for i in range(3))
        raw += b"\x00" + bytes(band) * width
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", zlib.compress(bytes(raw), 6)) + chunk(b"IEND", b"")


def make_pdf(lines: list[str]) -> bytes:
    """生成一页最小 PDF（Helvetica 文本行，ASCII），用于公开页预览与下载。"""
    stream = ("BT /F1 18 Tf 72 780 Td 26 TL\n" + "".join(f"({line}) Tj T*\n" for line in lines) + "ET").encode("latin-1", "replace")
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets: list[int] = []
    for index, obj in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{index} 0 obj\n".encode() + obj + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n".encode() + b"0000000000 65535 f \n"
    for offset in offsets:
        out += f"{offset:010d} 00000 n \n".encode()
    out += f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return bytes(out)


def _seed_slice_t1(db: Database) -> None:
    product_id = db.create_product(TENANT, "SLICE-T1", "SLICE-T1 联调离心泵", "联调演示：清水循环")
    params_id = db.add_source(TENANT, product_id, "SLICE-T1-参数表.txt", "TXT", "演示资料：参数表", content=PARAMS_TXT.encode("utf-8"), scope="exact_model")
    # 与真实走查一致：人工把扬程口径从提取值修正为 24-58 并标注
    db.save_field(TENANT, product_id, "head_range", "24-58", "m", params_id, "第 3 行（人工修正：口径调整为 24-58）", origin="manual")
    for key in ("flow_range", "head_range", "media", "material", "power"):
        db.approve_field(TENANT, product_id, key)
    db.reject_field(TENANT, product_id, "temperature", "演示资料不承诺介质温度上限；正式资料需企业确认后再公开")
    db.finalize_product_review(TENANT, product_id)
    db.publish_product(TENANT, product_id)  # v1：仅已核准字段
    db.allow_public_source(TENANT, product_id, params_id, "demo-admin")

    files = [
        ("SLICE-T1-产品手册.pdf", "PDF", "演示资料：产品手册", "exact_model",
         make_pdf(["SLICE-T1 Pump Series - Demo Manual", "Flow range: 30-90 m3/h", "Head range: 24-58 m", "Demo document for integration testing only."])),
        ("SLICE-T1-front.png", "IMAGE", "演示资料：产品正面图", "exact_model",
         make_png(520, 340, (96, 165, 250), (23, 32, 74))),
        ("SLICE-T1-side.png", "IMAGE", "演示资料：产品侧视图", "exact_model",
         make_png(520, 340, (94, 234, 212), (16, 58, 64))),
        ("SLICE-T1-系列样本页.png", "IMAGE", "演示资料：系列样本页（系列归属示例）", "series",
         make_png(680, 460, (167, 139, 250), (40, 24, 84))),
        ("SLICE-T1-外形图.step", "STEP", "演示资料：三维外形图", "exact_model", STEP_BYTES),
        ("SLICE-T1-装配图.dxf", "DXF", "演示资料：二维装配图", "exact_model", DXF_BYTES),
    ]
    for filename, file_type, location, scope, content in files:
        source_id = db.add_source(TENANT, product_id, filename, file_type, location, content=content, scope=scope)
        db.allow_public_source(TENANT, product_id, source_id, "demo-admin")
    db.publish_product(TENANT, product_id)  # v2：v1 + 7 份获准公开文件


def _seed_nobasis(db: Database) -> None:
    product_id = db.create_product(TENANT, "SLICE-NOBASIS", "SLICE-NOBASIS 说明用泵", "联调：无水力依据（验证无依据不进入候选）")
    db.add_source(TENANT, product_id, "SLICE-NOBASIS-材质说明.txt", "TXT", "演示资料：材质说明", content=NOBASIS_TXT.encode("utf-8"), scope="exact_model")
    db.approve_field(TENANT, product_id, "material")
    db.finalize_product_review(TENANT, product_id)
    db.publish_product(TENANT, product_id)


def seed(db_path: Path, reset: bool = False) -> dict[str, object]:
    db_path = Path(db_path)
    if db_path.exists() and not reset:
        try:
            conn = sqlite3.connect(str(db_path))
            count = conn.execute("SELECT COUNT(*) FROM products").fetchone()[0]
            conn.close()
        except sqlite3.Error:
            count = 0
        if count:
            return {"skipped": True, "products": count, "site_key": SITE_KEY, "db": str(db_path)}
    if db_path.exists():
        # 重建前清理旧演示文件（仅删除旧演示库自己登记过的文件）
        try:
            old = sqlite3.connect(str(db_path))
            old_rows = old.execute("SELECT storage_path FROM source_documents WHERE storage_path IS NOT NULL").fetchall()
            old.close()
            for (storage_path,) in old_rows:
                target = ROOT / storage_path
                if target.is_file():
                    target.unlink()
        except sqlite3.Error:
            pass
        db_path.unlink()
    db = Database(db_path)
    db.ensure_tenant(TENANT, TENANT_NAME)
    # 演示账号只在本命令（显式演示环境）里创建；普通数据库初始化不预置任何账号。
    db.seed_demo_users()
    try:
        db.register_buyer_site(TENANT, SITE_KEY, SITE_ORIGIN)
    except sqlite3.IntegrityError:
        pass
    _seed_slice_t1(db)
    _seed_nobasis(db)
    files = [row["storage_path"] for row in db.conn.execute("SELECT storage_path FROM source_documents WHERE tenant_id=? AND storage_path IS NOT NULL", (TENANT,))]
    db.conn.close()
    return {"skipped": False, "site_key": SITE_KEY, "db": str(db_path), "files": files}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=ROOT / "data" / "demo.db")
    parser.add_argument("--reset", action="store_true", help="删除并重建演示库（旧上传文件保留但不被引用）")
    args = parser.parse_args()
    result = seed(args.db, reset=args.reset)
    if result.get("skipped"):
        print(f"演示库已存在（{result['products']} 个产品），跳过构建：{result['db']}")
    else:
        print(f"演示库已构建：{result['db']}（网站标识 {result['site_key']}）")


if __name__ == "__main__":
    main()
