#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""T4 复核：deploy/make_release.py 实测审计（制作方复跑；不构成独立验收）。

1) 对真实仓库打包 -> 审计产物：不得含业务 data/（真实原件）、密钥（.pem/.key）、
   环境文件（.env）、.git、docs/design/fixtures、测试文件、.db/.log 等；运行必需
   文件齐备；RELEASE-MANIFEST.json / RELEASE-MANIFEST.sha256 逐文件可校验；
   包外 .sha256 与包实际哈希一致；包内不得有符号链接成员。
2) 伪造最小源（含 data/ 原件、根 .env、docs/、test_ 文件）：打包成功但产物不含其内容。
3) 伪造污染源（deploy 内 .env / .key / 符号链接 / test_ 文件）：必须拒绝（exit 2、
   不产出半成品包、输出可见拒绝原因）。

不读真实密钥文件；只用临时目录。运行：cd <repo> && python3 docs/evidence/first-launch-t4/verify-round-package-audit.py
退出码：0=全部通过；1=存在 FAIL。
"""
from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
MAKE_RELEASE = REPO / "deploy" / "make_release.py"
PY = sys.executable
PASSED: list[str] = []
FAILED: list[str] = []

FORBIDDEN_SEGMENTS = ("data", ".git", "docs", "design", "fixtures", "B2B_Pump_Demo_Research", "__pycache__")
FORBIDDEN_SUFFIXES = (".pyc", ".db", ".sqlite", ".sqlite3", ".log", ".env", ".pem", ".key", ".DS_Store")


def record(name: str, ok: bool, detail: str = "") -> None:
    (PASSED if ok else FAILED).append(name)
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path) -> str:
    return sha256_bytes(Path(path).read_bytes())


def run_build(source: Path, out: Path, label: str):
    return subprocess.run(
        [PY, str(MAKE_RELEASE), "--source", str(source), "--out", str(out), "--label", label],
        capture_output=True, text=True, timeout=300, cwd=str(REPO),
    )


def audit_package(out: Path, expected_present=()) -> tuple[list[str], dict[str, bytes]]:
    with tarfile.open(out, "r:gz") as tar:
        members = tar.getmembers()
        names = [m.name for m in members]
        links = [m.name for m in members if m.issym() or m.islnk()]
        contents: dict[str, bytes] = {}
        for member in members:
            if member.isfile():
                handle = tar.extractfile(member)
                contents[member.name] = handle.read() if handle else b""
    record("包内无符号链接成员（成员级）", not links, f"{links[:3]}" if links else "")
    bad_segments, bad_suffixes, bad_tests = [], [], []
    for name in names:
        segments = name.split("/")
        for root in FORBIDDEN_SEGMENTS:
            if root in segments:
                bad_segments.append(name)
                break
        base = segments[-1]
        if base.startswith("test_"):
            bad_tests.append(name)
        if base.endswith(FORBIDDEN_SUFFIXES):
            bad_suffixes.append(name)
    record("不含 data//.git/docs/design/fixtures 等排除目录",
           not bad_segments, f"{bad_segments[:3]}" if bad_segments else f"{len(names)} 个成员")
    record("不含 .env/.pem/.key/.db/.log 等敏感后缀", not bad_suffixes, f"{bad_suffixes[:3]}" if bad_suffixes else "")
    record("不含 test_* 测试文件", not bad_tests, f"{bad_tests[:3]}" if bad_tests else "")
    root = names[0].split("/")[0] if names else ""
    for rel in expected_present:
        record(f"包含运行必需文件 {rel}", f"{root}/{rel}" in names)
    return names, contents


def make_fake_source(fake: Path) -> None:
    shutil.rmtree(fake, ignore_errors=True)
    files = {
        "app.py": "x = 1\n",
        "assets/logo.txt": "l\n",
        "reference-client/client.js": "// c\n",
        "static/app.css": "body{}\n",
        "deploy/b2b.env.example": "# example\n",
        "deploy/systemd/b2b-app.service": "[Unit]\n",
        "tools/pilot_checks/check.py": "print('ok')\n",
    }
    for rel, data in files.items():
        target = fake / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(data, encoding="utf-8")


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="t4-pkg-") as tmp_text:
        tmp = Path(tmp_text)

        print("== 1. 真实仓库打包与审计 ==")
        out1 = tmp / "b2b-release-verify.tar.gz"
        proc = run_build(REPO, out1, "verify-20261013")
        tail = (proc.stdout + proc.stderr).strip().splitlines()
        record("真实仓库打包成功（exit 0）", proc.returncode == 0, tail[-1] if tail else "")
        if proc.returncode == 0:
            names, contents = audit_package(out1, expected_present=(
                "app.py", "deploy/make_release.py", "deploy/backup_snapshot.sh", "deploy/b2b.env.example",
                "deploy/nginx-b2b.conf.example", "deploy/nginx-b2b-http-bootstrap.conf.example",
                "tools/backup_restore.py", "tools/pilot_checks/check_first_launch_t4.py",
                "RELEASE-MANIFEST.json", "RELEASE-MANIFEST.sha256",
            ))
            root = names[0].split("/")[0]
            checksum = Path(str(out1) + ".sha256")
            recorded = checksum.read_text(encoding="utf-8").split()[0] if checksum.is_file() else None
            record("包外 .sha256 与包实际哈希一致", recorded == sha256_file(out1), f"sha256={recorded}")
            manifest = json.loads(contents[f"{root}/RELEASE-MANIFEST.json"].decode("utf-8"))
            mismatched = [
                rel for rel, info in manifest["files"].items()
                if f"{root}/{rel}" not in contents or sha256_bytes(contents[f"{root}/{rel}"]) != info["sha256"]
            ]
            record("RELEASE-MANIFEST.json 逐文件哈希一致", not mismatched,
                   f"{mismatched[:3]}" if mismatched else f"{manifest.get('file_count')} 个文件")
            record("清单 file_count 与 files 数一致", manifest.get("file_count") == len(manifest["files"]))
            sums_entries: dict[str, str] = {}
            for line in contents[f"{root}/RELEASE-MANIFEST.sha256"].decode("utf-8").splitlines():
                if line.strip():
                    digest, rel = line.split("  ", 1)
                    sums_entries[rel] = digest
            bad_sums = [
                rel for rel, digest in sums_entries.items()
                if sha256_bytes(contents.get(f"{root}/{rel}", b"")) != digest
            ]
            record("RELEASE-MANIFEST.sha256 全部条目校验", not bad_sums,
                   f"{bad_sums[:3]}" if bad_sums else f"{len(sums_entries)} 条")
            example = contents.get(f"{root}/deploy/b2b.env.example", b"").decode("utf-8", "replace")
            secret_assign = [
                line for line in example.splitlines()
                if not line.strip().startswith("#")
                and re.match(r"^\s*[A-Z0-9_]*(KEY|TOKEN|SECRET|SID|PASSWORD)\s*=\s*\S+", line)
            ]
            record("b2b.env.example 未注释行无密钥样式赋值", not secret_assign, f"{secret_assign[:2]}" if secret_assign else "")
            record("来源未提交修改标记已记录（当前工作树应为 dirty=True）",
                   manifest.get("source_dirty") is True,
                   f"source_head={manifest.get('source_head')} dirty={manifest.get('source_dirty')}")

        print("== 2. 伪造源：data/原件、根 .env、docs/、测试文件不进包 ==")
        fake = tmp / "fake-src"
        make_fake_source(fake)
        (fake / "data" / "uploads").mkdir(parents=True)
        (fake / "data" / "uploads" / "real-original.pdf").write_bytes(b"real original bytes")
        (fake / "docs").mkdir()
        (fake / "docs" / "notes.md").write_text("x\n", encoding="utf-8")
        (fake / "local.env").write_text("DEEPSEEK_API_KEY=SHOULD-NEVER-SHIP\n", encoding="utf-8")
        (fake / "test_hidden.py").write_text("pass\n", encoding="utf-8")
        out2 = tmp / "b2b-release-fake.tar.gz"
        proc = run_build(fake, out2, "fake-clean")
        record("伪造干净源打包成功（敏感文件不进包、构建不受影响）", proc.returncode == 0,
               (proc.stdout + proc.stderr).strip().splitlines()[-1] if proc.returncode != 0 else "")
        if proc.returncode == 0:
            audit_package(out2, expected_present=("app.py", "deploy/systemd/b2b-app.service"))

        print("== 3. 伪造污染源：必须拒绝（env/密钥/符号链接/测试文件）==")
        poisons = (
            ("环境文件 .env", "deploy/service.env", "text", "拒绝打包该类型文件"),
            ("密钥 .key", "deploy/server.key", "text", "拒绝打包该类型文件"),
            ("符号链接", "deploy/linked.pem", "symlink", "拒绝打包符号链接"),
            ("test_ 前缀文件", "tools/pilot_checks/test_leak.py", "text", "拒绝打包测试文件"),
        )
        for index, (label, rel, kind, expected) in enumerate(poisons):
            make_fake_source(fake)
            target = fake / rel
            if kind == "symlink":
                target.symlink_to(fake / "app.py")
            else:
                target.write_text("REAL-VALUE-SHOULD-NEVER-SHIP\n", encoding="utf-8")
            out3 = tmp / f"b2b-release-poison-{index}.tar.gz"
            proc = run_build(fake, out3, f"poison-{index}")
            output = proc.stdout + proc.stderr
            ok = proc.returncode == 2 and expected in output and rel in output and not out3.exists()
            record(f"拒绝{label}（exit 2、无半成品包、原因可见）", ok,
                   output.strip().splitlines()[-1] if output.strip() else "无输出")

    print("\n== 汇总 ==")
    print(f"PASS {len(PASSED)} / FAIL {len(FAILED)}")
    if FAILED:
        print("失败项：")
        for name in FAILED:
            print(f"  - {name}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
