#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""构建“单脚本修补包”：仅 deploy/offsite_backup_oss.sh（T4 异地链预检修复：-s → 默认表格输出+重写解析）。

约定与 deploy/make_release.py 对齐：顶层 b2b-patch-<label>/、RELEASE-MANIFEST.json（逐文件 sha256）、
RELEASE-MANIFEST.sha256、包外 <out>.sha256。拒绝覆盖既有输出（fail-closed）。
单脚本包的目的：服务器上只替换这一个脚本，不含 nginx/systemd/其他脚本与模板，避免覆盖其他已验配置。

用法:
  python3 build-offsite-precheck-patch.py [--source 仓库根] [--out 输出.tar.gz] [--label 标签]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import tarfile
import tempfile
from datetime import datetime, timezone
from pathlib import Path

FILES = ("deploy/offsite_backup_oss.sh",)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def git_info(source: Path):
    try:
        head = subprocess.run(
            ["git", "-C", str(source), "rev-parse", "HEAD"], capture_output=True, text=True, timeout=30
        ).stdout.strip()
        dirty = bool(subprocess.run(
            ["git", "-C", str(source), "status", "--porcelain"], capture_output=True, text=True, timeout=60
        ).stdout.strip())
    except (OSError, subprocess.SubprocessError):
        return None, None
    return head or None, dirty


def main() -> None:
    here = Path(__file__).resolve()
    default_source = here.parents[3] if len(here.parents) >= 4 else Path.cwd()
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--source", type=Path, default=default_source, help="仓库根目录")
    parser.add_argument("--out", type=Path, default=None, help="输出 tar.gz（默认 <source>/dist/b2b-patch-<label>.tar.gz）")
    parser.add_argument("--label", default="offsite-precheck-20261013", help="版本标签")
    args = parser.parse_args()

    source = args.source.resolve()
    out = args.out or (source / "dist" / "b2b-patch-{}.tar.gz".format(args.label))
    if out.exists():
        print("[拒绝打包] 输出已存在，不覆盖：{}".format(out), file=sys.stderr)
        raise SystemExit(2)
    for rel in FILES:
        path = source / rel
        if not path.is_file() or path.is_symlink():
            print("[拒绝打包] 缺少或非法文件：{}".format(path), file=sys.stderr)
            raise SystemExit(2)

    head, dirty = git_info(source)
    root_name = "b2b-patch-{}".format(args.label)
    out.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=str(out.parent)) as tmp:
        stage = Path(tmp) / root_name
        for rel in FILES:
            target = stage / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes((source / rel).read_bytes())
        manifest = {
            "label": args.label,
            "kind": "patch",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "generator": "docs/evidence/first-launch-t4/build-offsite-precheck-patch.py",
            "source_root": str(source),
            "source_head": head,
            "source_dirty": dirty,
            "python": sys.version.split()[0],
            "file_count": len(FILES),
            "files": {
                rel: {"sha256": sha256_file(stage / rel), "size": (stage / rel).stat().st_size}
                for rel in FILES
            },
            "policy": {
                "note": ("单脚本修补包：仅含 deploy/offsite_backup_oss.sh，用于替换服务器上同名脚本；"
                         "不含 nginx/systemd/其他脚本与模板，避免覆盖其他已验配置。"),
            },
        }
        (stage / "RELEASE-MANIFEST.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        sums = []
        for path in sorted(stage.rglob("*")):
            if path.is_file() and path.name != "RELEASE-MANIFEST.sha256":
                sums.append("{}  {}".format(sha256_file(path), path.relative_to(stage)))
        (stage / "RELEASE-MANIFEST.sha256").write_text("\n".join(sums) + "\n", encoding="utf-8")
        with tarfile.open(out, "w:gz") as tar:
            tar.add(str(stage), arcname=root_name)

    digest = sha256_file(out)
    Path(str(out) + ".sha256").write_text("{}  {}\n".format(digest, out.name), encoding="utf-8")
    print("修补包已生成：{}".format(out))
    print("sha256：{}".format(digest))
    print("文件数：{}；来源 HEAD：{}；含未提交修改：{}".format(
        len(FILES), head, "是" if dirty else "否"))


if __name__ == "__main__":
    main()
