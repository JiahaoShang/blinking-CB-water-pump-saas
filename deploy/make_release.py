#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""T4 部署发布包工具：从当前源码按白名单导出 + 生成 sha256 版本清单。

为什么不用 git clone：本仓库的工作内容含未提交修改，HEAD 不代表当前版本；
新安装/升级一律使用在受信任构建机（本机）上生成的发布包：
  1) 构建机运行本工具，产出 tar.gz + 校验文件；
  2) 经安全通道传到服务器（scp / 控制台文件上传 / Workbench）；
  3) 服务器上先核对 sha256，再解包到 /opt/b2b/releases/<label>（见 deploy/README.md 第 3 节）。

包内容 = 白名单：运行必需的应用代码与静态资源、部署模板（deploy/）、
备份/建号/校验工具（tools/）、知识解析依赖清单（requirements-knowledge.txt）+ 版本清单。
绝不包含：业务 data/、密钥、.git、docs（含 evidence 与历史资料）、设计/演示素材、测试文件。
新增应用模块或静态资源时，必须同步补进 INCLUDE_PATTERNS（缺文件会在安装导入时暴露）。

--patch 模式：只导出“本次任务修补包”（deploy/offsite_backup_oss.sh、backup_snapshot.sh、
新增 systemd 单元、含 ACME webroot 修正的 nginx 模板、deploy/README.md），供已部署服务器
做原地小更新。注意：修补包中的模板仅供 diff 参考，**不得**用它直接覆盖服务器上已人工修改的
运行配置（如 /etc/nginx/sites-available/b2b.conf）；应用步骤见 deploy/README.md 第 11 节。

用法：
  python3 deploy/make_release.py [--source 仓库根] [--out 输出.tar.gz] [--label 标签]
  python3 deploy/make_release.py --patch --label <标签>        # 单独修补包

产出：
  <out>                        发布包（顶层目录 b2b-release-<label>/）
  <out>.sha256                 发布包自身的 sha256（传输后先核对再解包）
  包内 RELEASE-MANIFEST.json   逐文件 sha256/大小 + 来源 HEAD/是否含未提交修改 + 排除策略
  包内 RELEASE-MANIFEST.sha256 包内全部文件（除本文件外）的 sha256sum 清单，解包后可：
                               cd <解包根> && sha256sum -c RELEASE-MANIFEST.sha256
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
import tarfile
import tempfile
from datetime import datetime, timezone
from pathlib import Path

# ── 白名单：只导出这些条目（glob；目录条目会被跳过，文件才进包）──────────────
INCLUDE_PATTERNS = (
    # 应用代码（app.py 及其本地模块）
    "app.py",
    "ai_client.py",
    "business_channel_ui.py",
    "business_channels.py",
    "channel_runtime.py",
    "console_guidance.py",
    "enterprise_knowledge.py",
    "enterprise_knowledge_phase2.py",
    "enterprise_knowledge_phase2_ui.py",
    "enterprise_knowledge_ui.py",
    "inbox_search.py",
    "knowledge_delivery.py",
    "knowledge_ingestion.py",
    "model_settings.py",
    # 知识解析依赖清单（部署时 pip install -r 用）与项目说明
    "requirements-knowledge.txt",
    "README.md",
    # 应用静态资源：页面壳、品牌标、第二批知识界面脚本
    "assets/*",
    "reference-client/*",
    "static/*",
    # 部署模板（systemd/nginx/env 样例/备份脚本/企业桥预部署工具）与运维工具
    "deploy/*",
    "deploy/systemd/*",
    "deploy/provision_enterprise_bridge.py",
    "tools/backup_restore.py",
    "tools/create_admin.py",
    # 正式运维/核准工具（精确清单）：测试程序与本地演练脚本一律不进发布包
    "tools/pilot_checks/check_db_migrations.py",
    "tools/pilot_checks/check_public_permissions.py",
    "tools/pilot_checks/generate_threshold_review.py",
    "tools/channels/outbound_worker.py",
)

# 每条至少命中一个文件（空 = 源码不完整，拒绝打包；fail-closed）
REQUIRED_GLOBS = (
    "app.py",
    "reference-client/*",
    "assets/*",
    "static/*",
    "deploy/*",
    "deploy/systemd/*",
    "deploy/provision_enterprise_bridge.py",
    "tools/pilot_checks/check_db_migrations.py",
    "tools/pilot_checks/check_public_permissions.py",
    "tools/pilot_checks/generate_threshold_review.py",
)

# ── 修补包白名单（--patch）：只含本任务相关文件；全部必须存在（fail-closed）────────
PATCH_INCLUDE_PATTERNS = (
    "deploy/backup_snapshot.sh",
    "deploy/offsite_backup_oss.sh",
    "deploy/systemd/b2b-offsite-backup.service",
    "deploy/systemd/b2b-offsite-backup.timer",
    "deploy/nginx-b2b.conf.example",
    "deploy/nginx-b2b-http-bootstrap.conf.example",
    "deploy/README.md",
)

# ── 排除策略（防御性校验：白名单路径万一命中这些规则也拒绝打包）─────────────
EXCLUDED_ROOTS = (
    "data", ".git", ".venv-knowledge", "docs", "design", "fixtures",
    "B2B_Pump_Demo_Research", "dist", "__pycache__",
)
EXCLUDED_BASENAME_PREFIXES = ("test_",)
EXCLUDED_SUFFIXES = (".pyc", ".db", ".sqlite", ".sqlite3", ".log", ".env", ".pem", ".key", ".DS_Store")


class BuildError(Exception):
    """打包前校验失败（fail-closed，不产出半成品包）。"""


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def git_info(source: Path):
    """返回 (HEAD 哈希, 是否含未提交修改)；非 git 仓库或 git 不可用时返回 (None, None)。"""
    try:
        head = subprocess.run(
            ["git", "-C", str(source), "rev-parse", "HEAD"], capture_output=True, text=True, timeout=30
        )
        status = subprocess.run(
            ["git", "-C", str(source), "status", "--porcelain"], capture_output=True, text=True, timeout=60
        )
    except (OSError, subprocess.SubprocessError):
        return None, None
    if head.returncode != 0 or status.returncode != 0:
        return None, None
    return head.stdout.strip(), bool(status.stdout.strip())


def _validate_member(source: Path, path: Path) -> str:
    if path.is_symlink():
        raise BuildError(f"拒绝打包符号链接：{path}")
    rel = path.relative_to(source)
    parts = rel.parts
    for root in EXCLUDED_ROOTS:
        if root in parts:
            raise BuildError(f"白名单条目命中排除目录（{root}）：{rel}")
    name = parts[-1]
    if name.startswith(EXCLUDED_BASENAME_PREFIXES):
        raise BuildError(f"拒绝打包测试文件：{rel}")
    if name.endswith(EXCLUDED_SUFFIXES):
        raise BuildError(f"拒绝打包该类型文件：{rel}")
    return str(rel)


def collect_files(source: Path, patterns=INCLUDE_PATTERNS, required=REQUIRED_GLOBS) -> dict:
    """按白名单收集文件；返回 {相对路径: 绝对路径}。任何一项不完整/不合规都拒绝打包。"""
    files: dict = {}
    for pattern in patterns:
        matches = sorted(source.glob(pattern))
        if not matches:
            if pattern in required:
                raise BuildError(f"源码不完整：白名单条目没有任何文件：{pattern}（源码目录: {source}）")
            continue
        for path in matches:
            if path.is_dir():
                continue
            rel = _validate_member(source, path)
            files[rel] = path
    if not files:
        raise BuildError(f"白名单没有收集到任何文件：{source}")
    return files


def build(source: Path, out: Path, label: str, patch: bool = False) -> dict:
    source = source.resolve()
    if not source.is_dir():
        raise BuildError(f"源码目录不存在：{source}")
    if not label or "/" in label or label.startswith("."):
        raise BuildError(f"版本标签无效：{label!r}")
    if out.exists():
        raise BuildError(f"输出已存在（拒绝覆盖）：{out}")
    patterns = PATCH_INCLUDE_PATTERNS if patch else INCLUDE_PATTERNS
    required = PATCH_INCLUDE_PATTERNS if patch else REQUIRED_GLOBS
    files = collect_files(source, patterns, required)
    head, dirty = git_info(source)
    kind = "patch" if patch else "release"
    root_name = f"b2b-{kind}-{label}"
    out.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=str(out.parent)) as temporary:
        stage = Path(temporary) / root_name
        stage.mkdir()
        for rel, src in files.items():
            target = stage / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, target)
        manifest_files = {}
        for rel in sorted(files):
            target = stage / rel
            manifest_files[rel] = {"sha256": sha256_file(target), "size": target.stat().st_size}
        manifest = {
            "label": label,
            "kind": kind,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "generator": "deploy/make_release.py",
            "source_root": str(source),
            "source_head": head,
            "source_dirty": dirty,
            "python": sys.version.split()[0],
            "file_count": len(manifest_files),
            "files": manifest_files,
            "policy": {
                "include_patterns": list(patterns),
                "excluded_roots": list(EXCLUDED_ROOTS),
                "excluded_basename_prefixes": list(EXCLUDED_BASENAME_PREFIXES),
                "excluded_suffixes": list(EXCLUDED_SUFFIXES),
                "note": (
                    "修补包白名单导出（deploy/ 任务文件）；模板仅供 diff，不得覆盖服务器已人工修改的运行配置。"
                    if patch else
                    "白名单导出；不含业务 data/、密钥、.git、docs（evidence/历史资料）、演示与测试文件。"
                ),
            },
        }
        (stage / "RELEASE-MANIFEST.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        sums = []
        for path in sorted(stage.rglob("*")):
            if not path.is_file():
                continue
            rel = path.relative_to(stage)
            if rel.name == "RELEASE-MANIFEST.sha256":
                continue
            sums.append(f"{sha256_file(path)}  {rel}")
        (stage / "RELEASE-MANIFEST.sha256").write_text("\n".join(sums) + "\n", encoding="utf-8")
        with tarfile.open(out, "w:gz") as tar:
            tar.add(str(stage), arcname=root_name)
    digest = sha256_file(out)
    Path(str(out) + ".sha256").write_text(f"{digest}  {out.name}\n", encoding="utf-8")
    return {
        "out": out, "digest": digest, "file_count": len(files), "label": label, "kind": kind,
        "source_head": head, "source_dirty": dirty,
    }


def default_label(source: Path) -> str:
    head, _dirty = git_info(source)
    short = head[:7] if head else "nogit"
    return f"{datetime.now().strftime('%Y%m%d')}-{short}"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    default_source = Path(__file__).resolve().parent.parent
    parser.add_argument("--source", type=Path, default=default_source, help="仓库根目录（默认本脚本所在仓库）")
    parser.add_argument("--out", type=Path, default=None, help="输出 tar.gz 路径（默认 <source>/dist/<包名>.tar.gz）")
    parser.add_argument("--label", default=None, help="版本标签（默认 YYYYMMDD-<git 短哈希>）")
    parser.add_argument("--patch", action="store_true",
                        help="只构建修补包（deploy/ 任务文件：异地备份脚本/单元/nginx 模板/README）")
    args = parser.parse_args()

    source = args.source.resolve()
    label = args.label or default_label(source)
    kind = "patch" if args.patch else "release"
    out = args.out or (source / "dist" / f"b2b-{kind}-{label}.tar.gz")
    try:
        result = build(source, out, label, patch=args.patch)
    except BuildError as error:
        print(f"[拒绝打包] {error}", file=sys.stderr)
        raise SystemExit(2)
    print(f"{'修补包' if args.patch else '发布包'}已生成：{result['out']}")
    print(f"sha256：{result['digest']}（同时写入 {result['out']}.sha256）")
    print(
        f"文件数：{result['file_count']}；来源 HEAD：{result['source_head'] or '（非 git 仓库）'}；"
        f"含未提交修改：{'是（发布包内容以实际打包为准，清单已记录）' if result['source_dirty'] else '否'}"
    )
    print("服务器端校验：① sha256sum -c <包名>.sha256；② 解包后 cd <包根> && sha256sum -c RELEASE-MANIFEST.sha256")
    if args.patch:
        print("修补包应用：按 deploy/README.md 第 11 节逐项执行；模板仅供 diff 参考，"
              "不得覆盖服务器上已人工修改的 nginx 运行配置。")
    else:
        print("安装步骤见 deploy/README.md 第 3 节（使用发布包，不从 git clone）。")


if __name__ == "__main__":
    main()
