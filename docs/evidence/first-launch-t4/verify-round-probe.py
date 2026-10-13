#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""T4 修复复核探针（制作方复跑；不构成独立验收）。

对照 /tmp/b2b-t4-extra-review.txt（= docs/evidence/first-launch-t4/independent/path-escape-review.txt）：
 1. 三种 `..` 形态（`data/uploads/../x`、`../x`、`<uploads>/../x`）在修复后代码上必须全部被拒；
 2. 用 t4-changes.diff 逐字提取的「修复前逻辑」复演：证明该复现真实、探针具判别力；
 3. 上传根 / 备份包根为符号链接一律拒绝；
 4. 正常嵌套、符号链接前缀造成的字面差异（macOS 临时目录 /var → /private/var 同类场景）、
    `...` 类合法文件名不得误伤；
 5. 所有被受理的路径，最终 resolve 必须仍在 uploads 根内。

只用系统临时目录与临时库；不读仓库 data/、不联网、不调用模型、不读密钥。
运行：cd <repo> && python3 docs/evidence/first-launch-t4/verify-round-probe.py
退出码：0=全部通过；1=存在 FAIL。
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO))

from tools.backup_restore import backup, resolve_stored_file, restore_and_verify  # noqa: E402

PASSED: list[str] = []
FAILED: list[str] = []


def record(name: str, ok: bool, detail: str = "") -> None:
    (PASSED if ok else FAILED).append(name)
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))


def expect_reject(name: str, fn, *args, **kwargs) -> None:
    try:
        got = fn(*args, **kwargs)
    except ValueError as exc:
        record(name, True, f"ValueError: {exc}")
        return
    except Exception as exc:  # noqa: BLE001 - 非 ValueError 也视为失败（须是显式拒绝）
        record(name, False, f"抛出非 ValueError 异常: {exc!r}")
        return
    record(name, False, f"未拒绝，返回: {got}")


def expect_ok(name: str, fn, *args, uploads_root=None, expected_bytes=None, **kwargs):
    try:
        got = fn(*args, **kwargs)
    except Exception as exc:  # noqa: BLE001
        record(name, False, f"意外拒绝: {exc!r}")
        return None
    if uploads_root is not None:
        try:
            got.resolve().relative_to(Path(uploads_root).resolve())
        except (ValueError, OSError) as exc:
            record(name, False, f"返回路径 resolve 后不在根内: {got} ({exc})")
            return None
    if expected_bytes is not None:
        try:
            data = got.read_bytes()
        except OSError as exc:
            record(name, False, f"返回路径不可读: {got} ({exc})")
            return None
        if data != expected_bytes:
            record(name, False, f"内容不符: {got}")
            return None
    record(name, True, f"-> {got}")
    return got


def load_prefix_version_function():
    """从 t4-changes.diff 逐字提取 T4 初审版（修复前）resolve_stored_file；失败返回 None。"""
    diff_path = REPO / "docs" / "evidence" / "first-launch-t4" / "t4-changes.diff"
    try:
        text = diff_path.read_text(encoding="utf-8")
        start = text.index("+def resolve_stored_file")
        end = text.index("\n+def _stored_rows", start)
    except (OSError, ValueError):
        return None
    lines = []
    for line in text[start:end].splitlines():
        if line.startswith("+"):
            lines.append(line[1:])
        elif line == "":
            lines.append("")
        else:
            return None
    namespace = {"Path": Path}
    exec("\n".join(lines), namespace)  # noqa: S102 - 证据复演专用（固定来源文本）
    return namespace.get("resolve_stored_file")


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="t4-verify-") as tmp:
        root = Path(tmp)
        uploads = root / "uploads"
        (uploads / "phase2" / "deep").mkdir(parents=True)
        plain = uploads / "plain.bin"
        plain.write_bytes(b"plain")
        nested = uploads / "phase2" / "deep" / "f.bin"
        nested.write_bytes(b"nested")
        weird = uploads / "...weird.txt"
        weird.write_bytes(b"dots")
        outside = root / "outside.txt"
        outside.write_bytes(b"outside secret")
        outside2 = root / "outside2.bin"
        outside2.write_bytes(b"outside2")

        print("== 0. 环境信息 ==")
        prefix_differs = str(uploads) != str(uploads.resolve())
        print(f"[INFO] 上传根字面 {uploads}")
        print(f"[INFO] 上传根解析 {uploads.resolve()}")
        print(f"[INFO] 临时目录符号链接前缀差异存在: {prefix_differs}")

        forms = (
            ("A 旧相对前缀+..：data/uploads/../outside.txt", f"data/uploads/../{outside.name}"),
            ("B 纯相对 ..：../outside.txt", f"../{outside.name}"),
            ("C 绝对根+..：<uploads>/../outside.txt", str(uploads / ".." / outside.name)),
        )

        print("\n== 1. 修复后：三种复现形态必须拒绝 ==")
        for label, stored in forms:
            expect_reject(f"拒绝 {label}", resolve_stored_file, stored, uploads)

        print("\n== 2. 修复前逻辑复演（证明复现真实、探针具判别力）==")
        old = load_prefix_version_function()
        if old is None:
            record("提取修复前逻辑（来自 t4-changes.diff）", False, "提取失败")
        else:
            for label, stored in forms:
                try:
                    got = old(stored, uploads)
                except ValueError as exc:
                    record(f"旧逻辑仍可复现逃逸 {label}", False, f"旧逻辑竟拒绝: {exc}")
                    continue
                resolved = Path(got).resolve()
                readable = resolved == outside.resolve() and resolved.read_bytes() == b"outside secret"
                record(
                    f"旧逻辑仍可复现逃逸 {label}",
                    readable,
                    f"旧逻辑返回 {got} -> 解析为 {resolved}（可读目录外文件）" if readable else f"未读到根外文件: {resolved}",
                )

        print("\n== 3. 符号链接根拒绝 ==")
        link_uploads = root / "uploads-link"
        link_uploads.symlink_to(uploads, target_is_directory=True)
        expect_reject("上传根为符号链接：resolve_stored_file 拒绝", resolve_stored_file, "data/uploads/plain.bin", link_uploads)
        dummy_db = root / "dummy.db"
        dummy_db.write_bytes(b"")
        expect_reject("上传根为符号链接：backup 入口拒绝", backup, dummy_db, link_uploads, root / "b-dummy")
        real_bundle = root / "bundle-real"
        real_bundle.mkdir()
        link_bundle = root / "bundle-link"
        link_bundle.symlink_to(real_bundle, target_is_directory=True)
        expect_reject("备份包根为符号链接：restore_and_verify 入口拒绝", restore_and_verify, link_bundle, root / "restored-x")

        print("\n== 4. 正常形态与误伤防护（受理且 resolve 仍在根内）==")
        expect_ok("正常相对 data/uploads/plain.bin", resolve_stored_file, "data/uploads/plain.bin", uploads,
                  uploads_root=uploads, expected_bytes=b"plain")
        expect_ok("正常嵌套 data/uploads/phase2/deep/f.bin", resolve_stored_file, "data/uploads/phase2/deep/f.bin", uploads,
                  uploads_root=uploads, expected_bytes=b"nested")
        expect_ok("绝对形态（配置持久目录后的登记格式）", resolve_stored_file, str(plain), uploads,
                  uploads_root=uploads, expected_bytes=b"plain")
        expect_ok("'...' 合法文件名不误伤", resolve_stored_file, "data/uploads/...weird.txt", uploads,
                  uploads_root=uploads, expected_bytes=b"dots")

        print("\n== 5. 符号链接前缀（macOS 临时目录同类场景）字面差异兼容 ==")
        prefix_real = root / "prefix-real"
        (prefix_real / "uploads").mkdir(parents=True)
        pf = prefix_real / "uploads" / "pf.bin"
        pf.write_bytes(b"pf")
        prefix_link = root / "prefix-link"
        prefix_link.symlink_to(prefix_real, target_is_directory=True)
        via_link = prefix_link / "uploads"
        expect_ok("存储=实际前缀、根=链接前缀", resolve_stored_file, str(pf), via_link,
                  uploads_root=via_link, expected_bytes=b"pf")
        expect_ok("存储=链接前缀、根=实际前缀", resolve_stored_file, str(via_link / "pf.bin"), via_link.resolve(),
                  uploads_root=via_link.resolve(), expected_bytes=b"pf")
        expect_ok("链接前缀下的正常相对形态", resolve_stored_file, "data/uploads/pf.bin", via_link,
                  uploads_root=via_link, expected_bytes=b"pf")
        expect_reject("链接前缀不豁免根外绝对路径", resolve_stored_file, str(outside2), via_link)
        expect_reject("链接前缀不豁免 .. 形态", resolve_stored_file, f"data/uploads/../{outside2.name}", via_link)

        print("\n== 6. 其余防护 ==")
        expect_reject("根外绝对路径拒绝", resolve_stored_file, str(outside2), uploads)
        sneaky = uploads / "sneaky.bin"
        sneaky.symlink_to(outside)
        expect_reject("uploads 内符号链接段拒绝", resolve_stored_file, "data/uploads/sneaky.bin", uploads)
        expect_reject("空存储路径拒绝", resolve_stored_file, "", uploads)
        expect_reject("含 NUL 的存储路径拒绝", resolve_stored_file, "data/uploads/x\x00.bin", uploads)

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
