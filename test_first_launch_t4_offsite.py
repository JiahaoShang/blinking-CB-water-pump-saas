# -*- coding: utf-8 -*-
"""T4 异地自动备份（OSS）判别性测试：deploy/offsite_backup_oss.sh + 快照脚本 B2B_BACKUP_OUT 固定输出。

覆盖（先红后绿；任务要求逐项）：
- 快照失败：非零退出、不上传、应用经 trap 恢复；快照“报告成功但产物缺失”也被拒绝（不能靠时间猜包）。
- age 加密失败：不产生上传，不完整密文被清理，本地快照保留。
- 上传失败：非零退出，本地快照 + 密文 + 校验文件全部保留，应用不处于停机态。
- 云校验失败/成功：下载到隔离 root 0700 目录比对本地 sha256；失败保留证据目录，成功清理下载副本。
- 云端预检解析（官方 2.4.0 二进制实核）：默认表格输出空前缀（仅 "Object Number is: 0"）放行；非空（行含 oss://）拒绝；
  ls 失败 fail-closed 退出 14；`-s` 等不存在的选项按真二进制口径被拒绝——测试不再假装支持不存在的选项。
- 同名冲突：云端已有对象（真实表格行）时拒绝覆盖（只做 ls、不 cp）；本地目标已存在时在停写前拒绝。
- ops 含密钥不得明文上传：快照内含 /etc/b2b/b2b.env 标记串，上传物必须仅为 age 密文（标记不可出现）。
- 应用恢复门：快照后应用未恢复时先尝试补救；仍失败则拒绝加密上传；补救成功则继续。
- 与 b2b-backup.service 的守候：正在跑本机备份时等待/超时拒绝，避免两个停写窗口重叠。
- systemd 单元、deploy/README.md 安装说明、make_release.py --patch 修补包。

边界（如实）：systemctl/ossutil/age 在测试中用 PATH 假件替换，使用真实 tar/sha256sum/停写快照脚本与
真实 tools/backup_restore.py；ossutil 假件的输出排版与报错口径照官方 2.4.0 二进制实核结果
（docs/evidence/first-launch-t4/ossutil-240-ls-evidence/）；只能验证脚本逻辑与失败恢复语义，
不代表真实 Linux/ossutil/age 与云侧验收。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
import time
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent
OFFSITE = REPO / "deploy" / "offsite_backup_oss.sh"
INNER = REPO / "deploy" / "backup_snapshot.sh"
UNITS = REPO / "deploy" / "systemd"
README = REPO / "deploy" / "README.md"

SECRET_MARKER = b"T4OPS_SECRET_MARKER_20261013"

SYSTEMCTL_SHIM = """#!/bin/sh
# 测试假件：FAKE_STATE_DIR/units/<unit>.active 存在 == 服务活动（与既有部署测试同口径）。
# 注入：FAKE_FAIL_START_UNIT/计数文件 —— start 前 N 次失败；FAKE_CLEAR_ACTIVE_AFTER_FIRST —— is-active 命中后清标志。
cmd="$1"
shift
dir="${FAKE_STATE_DIR:-/tmp}/units"
mkdir -p "$dir"
unit=""
quiet=0
for a in "$@"; do
  case "$a" in
    --quiet) quiet=1 ;;
    -*) ;;
    *) unit="$a" ;;
  esac
done
log="${FAKE_LOG:-/dev/null}"
case "$cmd" in
  is-active)
    if [ -f "$dir/$unit.active" ]; then
      [ "$quiet" = "1" ] || echo active
      if [ -n "${FAKE_CLEAR_ACTIVE_AFTER_FIRST:-}" ] && [ "$unit" = "$FAKE_CLEAR_ACTIVE_AFTER_FIRST" ]; then
        rm -f "$dir/$unit.active"
      fi
      exit 0
    fi
    [ "$quiet" = "1" ] || echo inactive
    exit 3 ;;
  stop)
    echo "systemctl stop $unit" >> "$log"
    rm -f "$dir/$unit.active"
    exit 0 ;;
  start)
    if [ -n "${FAKE_FAIL_START_UNIT:-}" ] && [ "$unit" = "$FAKE_FAIL_START_UNIT" ]; then
      count_file="${FAKE_STATE_DIR:-/tmp}/start_fail_$unit"
      count="$(cat "$count_file" 2>/dev/null || echo 0)"
      if [ "${count:-0}" -gt 0 ] 2>/dev/null; then
        echo $((count - 1)) > "$count_file"
        echo "systemctl start $unit (注入失败)" >> "$log"
        exit 1
      fi
    fi
    echo "systemctl start $unit" >> "$log"
    : > "$dir/$unit.active"
    exit 0 ;;
  *) exit 0 ;;
esac
"""

OSSUTIL_SHIM = '''# -*- coding: utf-8 -*-
"""测试假件：模拟 ossutil 2.4.0 的 ls / cp（上传、下载、--ignore-existing 跳过）。

ls 按官方 2.4.0 二进制实核行为建模（证据 docs/evidence/first-launch-t4/ossutil-240-ls-evidence/）：
- 不存在的短/长标志按真二进制口径报错并退出 1（含 -s：unknown shorthand flag）——不假装支持不存在的选项；
- 默认（long）输出为表格：表头 + 数据行（oss:// 在行中 ObjectName 列）+ "Object Number is: N"；空输出仅计数行；
- --short-format 为真 2.4.0 支持的长标志（短输出）；FAKE_OSS_LS_FAIL=1 注入服务端 403（退出码 2，同真二进制）。
"""
import os
import shutil
import sys

args = sys.argv[1:]
log_path = os.environ.get("FAKE_LOG", "")


def log(line):
    if log_path:
        with open(log_path, "a", encoding="utf-8") as handle:
            handle.write(line + "\\n")


log("ossutil " + " ".join(args))

cloud = None
if os.environ.get("FAKE_OSS_CLOUD"):
    cloud = os.path.join(os.environ["FAKE_OSS_CLOUD"])
    os.makedirs(cloud, exist_ok=True)

rest = list(args)
while rest and rest[0] in ("--config-file", "-c"):
    if len(rest) < 2:
        log("ossutil-bad-args")
        sys.exit(2)
    cfg = rest[1]
    rest = rest[2:]
    if not os.path.isfile(cfg):
        log("ossutil-config-missing")
        sys.exit(1)

if not rest:
    log("ossutil-no-subcommand")
    sys.exit(2)
cmd, rest = rest[0], rest[1:]


def split_key(url):
    body = url[len("oss://"):]
    bucket, _, key = body.partition("/")
    return bucket, key


def reject_unknown_flag(arg):
    """真 2.4.0（pflag）口径：未知标志报错并退出 1（实测 `ls -s` -> unknown shorthand flag: 's' in -s）。"""
    if arg.startswith("--"):
        sys.stderr.write("Error: unknown flag: {}\\n".format(arg.split("=")[0]))
    else:
        sys.stderr.write("Error: unknown shorthand flag: '{}' in {}\\n".format(arg[1:2], arg))
    sys.exit(1)


# 官方 2.4.0 默认 long 输出排版（照实核结果；仅 ObjectName 随对象变化）
LS_HEADER = "LastModifiedTime                   Size(B)     StorageClass   ETAG                                  ObjectName"
LS_ROW = "2026-10-13 19:08:29 +0800 CST        19186         Standard   0123456789abcdef0123456789abcdef      oss://{bucket}/{key}"


if cmd == "ls":
    urls = []
    short_format = False
    for a in rest:
        if a == "--short-format":
            short_format = True
        elif a.startswith("-") and a != "-":
            reject_unknown_flag(a)
        else:
            urls.append(a)
    if len(urls) != 1 or not urls[0].startswith("oss://"):
        log("ossutil-ls-bad-args")
        sys.exit(2)
    bucket, prefix = split_key(urls[0])
    entries = []
    base = os.path.join(cloud, prefix)
    if os.path.isdir(base):
        for dirpath, _dirnames, filenames in os.walk(base):
            for name in sorted(filenames):
                full = os.path.join(dirpath, name)
                entries.append(os.path.relpath(full, cloud))
    if os.environ.get("FAKE_OSS_PREEXISTING") == "1":
        entries.append(prefix + "stale-precheck-object")
    if os.environ.get("FAKE_OSS_LS_FAIL") == "1":
        sys.stderr.write(
            "Error: operation error ListObjectsV2: Error returned by Service. \\n"
            "Http Status Code: 403. \\n"
            "Error Code: AccessDenied. \\n")
        sys.exit(2)
    if entries:
        if short_format:
            for key in entries:
                print("oss://{}/{}".format(bucket, key))
        else:
            print(LS_HEADER)
            for key in entries:
                print(LS_ROW.format(bucket=bucket, key=key))
    if os.environ.get("FAKE_OSS_LS_FOOTER_COMPACT") == "1":
        print("Object Number is:{}".format(len(entries)))
    else:
        print("Object Number is: {}".format(len(entries)))
    print("")
    print("0.000000(s) elapsed")
    sys.exit(0)

if cmd == "cp":
    positional = [a for a in rest if not a.startswith("-")]
    flags = rest[len(positional):]
    if len(positional) != 2:
        log("ossutil-cp-bad-args")
        sys.exit(2)
    src, dst = positional
    ignore_existing = "--ignore-existing" in rest
    if src.startswith("oss://"):
        bucket, key = split_key(src)
        if os.environ.get("FAKE_OSS_FAIL_DOWNLOAD") == "1":
            log("ossutil-download-injected-fail")
            sys.exit(1)
        source = os.path.join(cloud, key)
        if not os.path.isfile(source):
            log("ossutil-download-missing")
            sys.exit(1)
        with open(source, "rb") as handle:
            data = handle.read()
        if os.environ.get("FAKE_OSS_CORRUPT_DOWNLOAD") == "1":
            data = b"FAKE-CORRUPTED\\n" + data
        if os.path.exists(dst) and ignore_existing:
            log("ossutil-skip-existing " + dst)
            sys.exit(0)
        os.makedirs(os.path.dirname(dst) or ".", exist_ok=True)
        with open(dst, "wb") as handle:
            handle.write(data)
        sys.exit(0)
    bucket, key = split_key(dst)
    target = os.path.join(cloud, key)
    if os.environ.get("FAKE_OSS_FAIL_UPLOAD") == "1":
        log("ossutil-upload-injected-fail")
        sys.exit(1)
    if os.path.exists(target) and ignore_existing:
        log("ossutil-skip-existing " + key)
        sys.exit(0)
    os.makedirs(os.path.dirname(target), exist_ok=True)
    shutil.copyfile(src, target)
    sys.exit(0)

log("ossutil-unknown-command")
sys.exit(2)
'''

AGE_SHIM = '''# -*- coding: utf-8 -*-
"""测试假件：age -R <recipient> -o <out>（stdin -> 密文）；可注入失败并留下不完整输出。"""
import hashlib
import os
import sys

args = sys.argv[1:]
log_path = os.environ.get("FAKE_LOG", "")


def log(line):
    if log_path:
        with open(log_path, "a", encoding="utf-8") as handle:
            handle.write(line + "\\n")


out = None
recipient = None
i = 0
while i < len(args):
    if args[i] in ("-R", "-r"):
        recipient = args[i + 1]
        i += 2
        continue
    if args[i] in ("-o", "--output"):
        out = args[i + 1]
        i += 2
        continue
    i += 1

data = sys.stdin.buffer.read()
log("age-call recipient={} bytes={}".format(recipient, len(data)))
if os.environ.get("FAKE_AGE_FAIL") == "1":
    if out:
        with open(out, "wb") as handle:
            handle.write(b"PARTIAL-CIPHERTEXT")
    log("age-fail")
    sys.exit(1)
if not out or not recipient or not os.path.isfile(recipient):
    log("age-bad-args")
    sys.exit(1)
digest = hashlib.sha256(data).hexdigest().encode()
with open(out, "wb") as handle:
    handle.write(b"FAKEAGE1\\n" + digest + b"\\n")
log("age-ok")
sys.exit(0)
'''

BACKUP_TOOL_SHIM = '''# -*- coding: utf-8 -*-
"""测试假件：记录调用；可注入失败/无声成功；成功路径委托给真实 tools/backup_restore.py。"""
import os
import runpy
import sys


def mark(line):
    path = os.environ.get("FAKE_LOG", "")
    if path:
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(line + "\\n")


if os.environ.get("T4_FAKE_BACKUP_FAIL") == "1":
    mark("backup-fail")
    sys.exit(7)
mark("backup-start")
if os.environ.get("T4_FAKE_BACKUP_NOOP") == "1":
    mark("backup-noop")
    sys.exit(0)
runpy.run_path(os.environ["T4_REAL_BACKUP_TOOL"], run_name="__main__")
mark("backup-ok")
'''


def set_mtime(path: Path, days_ago: float) -> None:
    stamp = time.time() - days_ago * 86400
    os.utime(path, (stamp, stamp))


def tar_read(tar: tarfile.TarFile, name: str) -> bytes:
    stream = tar.extractfile(name)
    assert stream is not None
    return stream.read()


def find_stamp_dirs(root: Path) -> "list[Path]":
    if not root.is_dir():
        return []
    return sorted(p for p in root.iterdir() if p.is_dir() and re.fullmatch(r"\d{8}-\d{6}", p.name))


class OffsiteBackupTestBase(unittest.TestCase):
    """以 PATH 假件驱动真实 offsite_backup_oss.sh（内部调用真实 backup_snapshot.sh）。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="t4-offsite-")
        self.root = Path(self._tmp.name)
        self.state = self.root / "state"
        (self.state / "units").mkdir(parents=True)
        self.log = self.root / "calls.log"
        self.shims = self.root / "shims"
        self.shims.mkdir()
        self._write_shim("systemctl", SYSTEMCTL_SHIM, executable=True)
        self._write_py_shim("ossutil", OSSUTIL_SHIM)
        self._write_py_shim("age", AGE_SHIM)

        self.app_dir = self.root / "app"
        (self.app_dir / "tools").mkdir(parents=True)
        (self.app_dir / "tools" / "backup_restore.py").write_text(BACKUP_TOOL_SHIM, encoding="utf-8")

        self.data_dir = self.root / "data"
        self.uploads = self.data_dir / "uploads"
        self.uploads.mkdir(parents=True)
        self.db_path = self.data_dir / "app.db"
        sqlite3.connect(str(self.db_path)).close()

        self.backup_root = self.root / "backups"
        self.verify_root = self.root / "oss-verify"
        self.env_file = self.root / "b2b.env"
        self.env_file.write_text(
            "B2B_UPLOAD_DIR=/var/lib/b2b/data/uploads\n"
            "T4OPS_SECRET_MARKER_20261013=must-not-appear-in-cloud\n",
            encoding="utf-8",
        )
        os.chmod(self.env_file, 0o600)

        self.recipient = self.root / "backup-age.pub"
        self.recipient.write_text("age1" + "q" * 58 + "\n", encoding="utf-8")

        self.oss_config = self.root / "ossutil.conf"
        self.oss_config.write_text("[Credentials]\nplaceholder = fake\n", encoding="utf-8")
        os.chmod(self.oss_config, 0o600)

        self.cloud = self.root / "cloud"
        (self.cloud / "backups").mkdir(parents=True)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    # ── 夹具 ──────────────────────────────────────────────────────────────
    def _write_shim(self, name: str, content: str, *, executable: bool = True) -> Path:
        path = self.shims / name
        path.write_text(content, encoding="utf-8")
        if executable:
            os.chmod(path, 0o755)
        return path

    def _write_py_shim(self, name: str, content: str) -> Path:
        path = self.shims / name
        path.write_text("#!" + sys.executable + "\n" + content, encoding="utf-8")
        os.chmod(path, 0o755)
        return path

    def activate_app(self) -> None:
        (self.state / "units" / "b2b-app.active").write_text("", encoding="utf-8")

    def app_is_active(self) -> bool:
        return (self.state / "units" / "b2b-app.active").exists()

    def backup_unit_is_active(self) -> bool:
        return (self.state / "units" / "b2b-backup.service.active").exists()

    def base_env(self) -> dict:
        return {
            "PATH": "{}:/usr/bin:/bin:/sbin".format(self.shims),
            "FAKE_STATE_DIR": str(self.state),
            "FAKE_LOG": str(self.log),
            "FAKE_OSS_CLOUD": str(self.cloud),
            "T4_REAL_BACKUP_TOOL": str(REPO / "tools" / "backup_restore.py"),
            "B2B_APP_UNIT": "b2b-app",
            "B2B_BACKUP_UNIT": "b2b-backup.service",
            "B2B_APP_DIR": str(self.app_dir),
            "B2B_PY": sys.executable,
            "B2B_DATA_DIR": str(self.data_dir),
            "B2B_BACKUP_ROOT": str(self.backup_root),
            "B2B_BACKUP_KEEP_DAYS": "14",
            "B2B_ENV_FILE": str(self.env_file),
            "B2B_AGE_RECIPIENT_FILE": str(self.recipient),
            "B2B_OSSUTIL": str(self.shims / "ossutil"),
            "B2B_OSSUTIL_CONFIG": str(self.oss_config),
            "B2B_OSS_BUCKET": "bucket-t4-oss",
            "B2B_OSS_PREFIX": "backups",
            "B2B_OSS_VERIFY_ROOT": str(self.verify_root),
        }

    def run_offsite(self, *, app_active: bool = True, env_extra: "dict | None" = None,
                    clear_log: bool = True) -> subprocess.CompletedProcess:
        if app_active:
            self.activate_app()
        if clear_log and self.log.exists():
            self.log.unlink()
        env = self.base_env()
        if env_extra:
            env.update(env_extra)
        return subprocess.run(
            ["/bin/bash", str(OFFSITE)], env=env, capture_output=True, text=True, timeout=180,
            cwd=str(REPO),
        )

    def run_inner(self, *, env_extra: "dict | None" = None, app_active: bool = True,
                  clear_log: bool = True) -> subprocess.CompletedProcess:
        if app_active:
            self.activate_app()
        if clear_log and self.log.exists():
            self.log.unlink()
        env = self.base_env()
        if env_extra:
            env.update(env_extra)
        return subprocess.run(
            ["/bin/sh", str(INNER)], env=env, capture_output=True, text=True, timeout=180,
            cwd=str(REPO),
        )

    # ── 断言辅助 ──────────────────────────────────────────────────────────
    def log_lines(self) -> "list[str]":
        if not self.log.exists():
            return []
        return self.log.read_text(encoding="utf-8").splitlines()

    def ossutil_calls(self) -> "list[str]":
        return [line for line in self.log_lines() if line.startswith("ossutil ")]

    def ossutil_upload_calls(self) -> "list[str]":
        result = []
        for line in self.ossutil_calls():
            tokens = line.split()
            if "cp" not in tokens:
                continue
            args = tokens[tokens.index("cp") + 1:]
            positional = [a for a in args if not a.startswith("-")]
            if positional and not positional[0].startswith("oss://"):
                result.append(line)
        return result

    def ossutil_download_calls(self) -> "list[str]":
        result = []
        for line in self.ossutil_calls():
            tokens = line.split()
            if "cp" not in tokens:
                continue
            args = tokens[tokens.index("cp") + 1:]
            positional = [a for a in args if not a.startswith("-")]
            if positional and positional[0].startswith("oss://"):
                result.append(line)
        return result

    def assert_order(self, expected_prefixes: "list[str]") -> None:
        lines = self.log_lines()
        positions = []
        for prefix in expected_prefixes:
            hit = next((i for i, line in enumerate(lines) if line.startswith(prefix)), None)
            self.assertIsNotNone(hit, msg="缺少调用记录: {}（实际: {}）".format(prefix, lines))
            positions.append(hit)
        self.assertEqual(positions, sorted(positions), msg="调用顺序不符: {}".format(lines))

    def stamp_dirs(self) -> "list[Path]":
        return find_stamp_dirs(self.backup_root)

    def single_stamp(self) -> Path:
        stamps = self.stamp_dirs()
        self.assertEqual(len(stamps), 1, msg="应恰好产生一个时间戳快照目录: {}".format(stamps))
        return stamps[0]

    def cipher_paths(self, stamp: str) -> "tuple[Path, Path]":
        name = stamp + ".export.tar.gz.age"
        return self.backup_root / name, self.backup_root / (name + ".sha256")

    def cloud_backups(self) -> "list[Path]":
        root = self.cloud / "backups"
        if not root.is_dir():
            return []
        return sorted(p for p in root.rglob("*") if p.is_file())


class OffsiteScriptBehaviorTests(OffsiteBackupTestBase):
    def test_script_parses_in_bash(self) -> None:
        result = subprocess.run(["/bin/bash", "-n", str(OFFSITE)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_preflight_rejects_secret_key_as_recipient(self) -> None:
        self.recipient.write_text("AGE-SECRET-KEY-1QQQQQQQQQQQQQQQQQQQQQQQQQQQQ\n", encoding="utf-8")
        result = self.run_offsite()
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("公钥", result.stderr)
        self.assertEqual(self.stamp_dirs(), [], "预检失败不得开始快照")
        self.assertEqual(self.ossutil_calls(), [])
        self.assertNotIn("systemctl stop b2b-app", self.log_lines())

    def test_preflight_rejects_missing_ossutil_binary(self) -> None:
        result = self.run_offsite(env_extra={"B2B_OSSUTIL": str(self.root / "nope-ossutil")})
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("ossutil", result.stderr)
        self.assertEqual(self.stamp_dirs(), [])

    def test_snapshot_failure_no_upload_and_app_restored(self) -> None:
        result = self.run_offsite(env_extra={"T4_FAKE_BACKUP_FAIL": "1"})
        self.assertEqual(result.returncode, 10, result.stdout + result.stderr)
        self.assertIn("快照失败", result.stderr)
        lines = self.log_lines()
        self.assertIn("backup-fail", lines)
        self.assertIn("systemctl start b2b-app", lines)
        self.assertTrue(self.app_is_active(), "快照失败后应用必须恢复活动")
        self.assertEqual(self.ossutil_calls(), [], "快照失败不得发生任何云调用")
        self.assertEqual(self.stamp_dirs(), [])
        self.assertEqual(self.cloud_backups(), [])

    def test_snapshot_success_but_missing_bundle_is_rejected(self) -> None:
        result = self.run_offsite(env_extra={"T4_FAKE_BACKUP_NOOP": "1"})
        self.assertEqual(result.returncode, 11, result.stdout + result.stderr)
        self.assertIn("产物", result.stderr)
        self.assertEqual(self.ossutil_calls(), [])
        self.assertIn("systemctl start b2b-app", self.log_lines())

    def test_age_failure_removes_partial_ciphertext_and_never_uploads(self) -> None:
        result = self.run_offsite(env_extra={"FAKE_AGE_FAIL": "1"})
        self.assertEqual(result.returncode, 12, result.stdout + result.stderr)
        self.assertIn("加密失败", result.stderr)
        stamp = self.single_stamp().name
        cipher, cipher_sha = self.cipher_paths(stamp)
        self.assertFalse(cipher.exists(), "不完整密文必须被清理")
        self.assertFalse(cipher_sha.exists(), "加密失败不得生成校验文件")
        self.assertTrue((self.single_stamp() / "manifest.json").is_file(), "本地快照必须保留")
        self.assertEqual(self.ossutil_calls(), [], "加密失败不得发生任何云调用")

    def test_upload_failure_nonzero_keeps_local_artifacts_and_app_running(self) -> None:
        result = self.run_offsite(env_extra={"FAKE_OSS_FAIL_UPLOAD": "1"})
        self.assertEqual(result.returncode, 15, result.stdout + result.stderr)
        self.assertIn("保留", result.stderr)
        stamp = self.single_stamp().name
        cipher, cipher_sha = self.cipher_paths(stamp)
        self.assertTrue(cipher.is_file(), "上传失败必须保留本地密文")
        self.assertTrue(cipher_sha.is_file(), "上传失败必须保留本地校验文件")
        self.assertTrue((self.single_stamp() / "manifest.json").is_file())
        self.assertTrue(self.app_is_active(), "上传失败不得让应用处于停机态")
        self.assertEqual(self.cloud_backups(), [])

    def test_existing_cloud_object_refused_without_upload(self) -> None:
        """云端已有对象（默认表格输出的真实行）→ 拒绝覆盖：只做一次 ls、不执行任何 cp、退出 14。"""
        result = self.run_offsite(env_extra={"FAKE_OSS_PREEXISTING": "1"})
        self.assertEqual(result.returncode, 14, result.stdout + result.stderr)
        self.assertIn("拒绝", result.stderr)
        self.assertIn("oss://", result.stderr, "拒绝时应回显检测到的对象行")
        calls = self.ossutil_calls()
        self.assertEqual(len(calls), 1, msg="只允许做一次 ls 预检：{}".format(calls))
        self.assertIn(" ls ", calls[0])
        self.assertNotIn("-s", calls[0], msg="预检不得使用 2.4.0 不存在的 -s：{}".format(calls[0]))
        self.assertEqual(self.ossutil_upload_calls(), [], "同名冲突时不得执行任何上传")
        stamp = self.single_stamp().name
        cipher, _ = self.cipher_paths(stamp)
        self.assertTrue(cipher.is_file(), "本地密文仍必须保留")
        self.assertEqual(self.cloud_backups(), [])

    def test_ls_failure_fail_closed_no_upload(self) -> None:
        """云端预检 ls 以非零退出（2.4.0 实核的 403 口径）→ fail-closed 退出 14；不执行任何上传。"""
        result = self.run_offsite(env_extra={"FAKE_OSS_LS_FAIL": "1"})
        self.assertEqual(result.returncode, 14, result.stdout + result.stderr)
        self.assertIn("fail-closed", result.stderr)
        self.assertIn("AccessDenied", result.stderr, "应回显 ls 的错误输出")
        calls = self.ossutil_calls()
        self.assertEqual(len(calls), 1, msg="只允许一次 ls 预检调用：{}".format(calls))
        self.assertEqual(self.ossutil_upload_calls(), [])
        self.assertEqual(self.cloud_backups(), [])
        stamp = self.single_stamp().name
        cipher, cipher_sha = self.cipher_paths(stamp)
        self.assertTrue(cipher.is_file(), "预检失败必须保留本地密文")
        self.assertTrue(cipher_sha.is_file(), "预检失败必须保留本地校验文件")
        self.assertTrue(self.app_is_active())

    def test_precheck_tolerates_user_reported_compact_footer(self) -> None:
        """服务器用户回传的空桶尾行为 `Object Number is:0`（无空格）；官方二进制实核为 `Object Number is: 0`。
        两种排版都必须被预检接受（否则会误 fail-closed），本项以紧凑排版驱动一次完整成功链。"""
        result = self.run_offsite(env_extra={"FAKE_OSS_LS_FOOTER_COMPACT": "1"})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue(self.ossutil_upload_calls(), "紧凑尾行不得被误判为无法确认而拒绝")

    def test_cloud_verify_mismatch_fails_and_keeps_evidence(self) -> None:
        result = self.run_offsite(env_extra={"FAKE_OSS_CORRUPT_DOWNLOAD": "1"})
        self.assertEqual(result.returncode, 16, result.stdout + result.stderr)
        self.assertIn("sha256", result.stderr)
        stamp = self.single_stamp().name
        verify_dir = self.verify_root / stamp
        self.assertTrue(verify_dir.is_dir(), "校验失败必须保留隔离证据目录")
        downloaded = verify_dir / (stamp + ".export.tar.gz.age")
        self.assertTrue(downloaded.is_file())
        cipher, cipher_sha = self.cipher_paths(stamp)
        self.assertTrue(cipher.is_file())
        self.assertTrue(cipher_sha.is_file())
        self.assertTrue(self.app_is_active())

    def test_success_flow_uploads_cipher_and_checksum_then_verifies(self) -> None:
        result = self.run_offsite()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        stamp_dir = self.single_stamp()
        stamp = stamp_dir.name
        cipher, cipher_sha = self.cipher_paths(stamp)
        self.assertTrue(cipher.is_file())
        self.assertTrue(cipher_sha.is_file())

        uploads = self.ossutil_upload_calls()
        downloads = self.ossutil_download_calls()
        self.assertEqual(len(uploads), 2, msg="应恰好上传密文与校验文件：{}".format(uploads))
        self.assertEqual(len(downloads), 2, msg="应恰好下载密文与校验文件复核：{}".format(downloads))
        for call in uploads:
            self.assertIn("--ignore-existing", call, "上传必须带 --ignore-existing 防覆盖")

        self.assert_order([
            "systemctl stop b2b-app",
            "backup-start",
            "backup-ok",
            "systemctl start b2b-app",
            "age-ok",
            "ossutil",
        ])
        self.assertTrue(self.app_is_active())

        # 云上只有这两个对象；字节与本地一致；下载校验目录用完即清
        cloud_files = self.cloud_backups()
        self.assertEqual(
            sorted(p.relative_to(self.cloud).as_posix() for p in cloud_files),
            sorted([
                "backups/{}/{}".format(stamp, cipher.name),
                "backups/{}/{}".format(stamp, cipher_sha.name),
            ]),
        )
        cloud_cipher = self.cloud / "backups" / stamp / cipher.name
        self.assertEqual(cloud_cipher.read_bytes(), cipher.read_bytes())
        self.assertFalse((self.verify_root / stamp).exists(), "校验成功后应清理隔离下载副本")
        self.assertIn("异地备份完成", result.stdout)

    def test_ops_secret_never_uploaded_in_plaintext(self) -> None:
        result = self.run_offsite()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        stamp = self.single_stamp().name
        cipher, _cipher_sha = self.cipher_paths(stamp)

        # 前提校验：密钥标记确实进了本地快照（否则“未上传”断言无判别力）
        ops_copy = self.single_stamp() / "ops-config" / "b2b.env"
        self.assertTrue(ops_copy.is_file())
        self.assertIn(SECRET_MARKER, ops_copy.read_bytes())

        cloud_files = self.cloud_backups()
        self.assertEqual(len(cloud_files), 2)
        for path in cloud_files:
            self.assertNotIn(SECRET_MARKER, path.read_bytes(),
                             "云端对象不得包含运维配置明文: {}".format(path))

        # 上传物必须是 age 密文本体（字节等价），且不是任何明文 tar
        cloud_cipher = self.cloud / "backups" / stamp / cipher.name
        self.assertEqual(cloud_cipher.read_bytes(), cipher.read_bytes())
        plaintext_tars = [p for p in self.backup_root.rglob("*.tar.gz")]
        self.assertEqual(plaintext_tars, [], "本地不得落明文 tar：{}".format(plaintext_tars))
        try:
            with tarfile.open(cipher) as _tar:  # 密文不是 tar（防“加密被当成无操作”）
                self.fail("密文不得是明文 tar")
        except tarfile.ReadError:
            pass

    def test_success_with_inactive_app_starts_nothing(self) -> None:
        result = self.run_offsite(app_active=False)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        lines = self.log_lines()
        self.assertNotIn("systemctl stop b2b-app", lines, "原本未运行的应用不得被停")
        self.assertNotIn("systemctl start b2b-app", lines, "原本未运行的应用不得被启动")
        self.assertEqual(len(self.stamp_dirs()), 1)
        self.assertTrue(self.ossutil_upload_calls(), "应用原本未运行时仍应完成异地备份")

    def test_retention_prunes_only_aged_export_and_verify_items(self) -> None:
        self.backup_root.mkdir(parents=True)
        aged_cipher = self.backup_root / "20200101-000000.export.tar.gz.age"
        aged_cipher.write_bytes(b"old")
        aged_sha = self.backup_root / "20200101-000000.export.tar.gz.age.sha256"
        aged_sha.write_bytes(b"old")
        recent_cipher = self.backup_root / "20261013-000000.export.tar.gz.age"
        recent_cipher.write_bytes(b"recent")
        unrelated = self.backup_root / "20200101-000000.tar.gz.age"
        unrelated.write_bytes(b"not-ours")
        notes = self.backup_root / "notes.txt"
        notes.write_text("keep me", encoding="utf-8")
        self.verify_root.mkdir(parents=True)
        aged_verify = self.verify_root / "20200101-000000"
        aged_verify.mkdir()
        manual_verify = self.verify_root / "manual-dir"
        manual_verify.mkdir()
        for path in (aged_cipher, aged_sha, unrelated, notes, aged_verify, manual_verify):
            set_mtime(path, 30)
        set_mtime(recent_cipher, 0)

        result = self.run_offsite()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse(aged_cipher.exists())
        self.assertFalse(aged_sha.exists())
        self.assertFalse(aged_verify.exists())
        self.assertTrue(recent_cipher.exists(), "未过保留期的密文必须保留")
        self.assertTrue(unrelated.exists(), "非本工具命名的文件不得清理")
        self.assertTrue(notes.exists())
        self.assertTrue(manual_verify.exists())
        stamp = self.single_stamp().name
        cipher, cipher_sha = self.cipher_paths(stamp)
        self.assertTrue(cipher.is_file())
        self.assertTrue(cipher_sha.is_file())

    def test_app_restore_retry_after_inner_failure_then_continues(self) -> None:
        (self.state / "start_fail_b2b-app").write_text("1", encoding="utf-8")
        result = self.run_offsite(env_extra={"FAKE_FAIL_START_UNIT": "b2b-app"})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        lines = self.log_lines()
        self.assertIn("systemctl start b2b-app (注入失败)", lines)
        self.assertIn("systemctl start b2b-app", lines)
        self.assertTrue(self.ossutil_upload_calls(), "应用补救恢复成功后应继续上传")
        self.assertTrue(self.app_is_active())
        positions = [i for i, line in enumerate(lines) if line.startswith("ossutil ")]
        start_pos = max(i for i, line in enumerate(lines) if line.startswith("systemctl start b2b-app"))
        self.assertLess(start_pos, positions[0], "恢复应用必须先于任何云调用")

    def test_app_restore_failure_blocks_encryption_and_upload(self) -> None:
        (self.state / "start_fail_b2b-app").write_text("2", encoding="utf-8")
        result = self.run_offsite(env_extra={"FAKE_FAIL_START_UNIT": "b2b-app"})
        self.assertEqual(result.returncode, 11, result.stdout + result.stderr)
        self.assertIn("恢复", result.stderr)
        self.assertEqual(self.ossutil_calls(), [], "应用未恢复时不得加密/上传")
        self.assertFalse(self.app_is_active())
        stamp = self.single_stamp().name
        cipher, _ = self.cipher_paths(stamp)
        self.assertFalse(cipher.exists(), "门禁失败不得生成密文")

    def test_wait_for_local_backup_timeout_refuses_to_start(self) -> None:
        (self.state / "units" / "b2b-backup.service.active").write_text("", encoding="utf-8")
        result = self.run_offsite(env_extra={"B2B_OFFSITE_WAIT_LOCAL_S": "0"})
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("拒绝", result.stderr)
        self.assertEqual(self.stamp_dirs(), [], "本机备份未结束前不得开始新的停写窗口")
        self.assertEqual(self.ossutil_calls(), [])

    def test_wait_for_local_backup_finishes_then_proceeds(self) -> None:
        (self.state / "units" / "b2b-backup.service.active").write_text("", encoding="utf-8")
        result = self.run_offsite(env_extra={
            "B2B_OFFSITE_WAIT_LOCAL_S": "30",
            "FAKE_CLEAR_ACTIVE_AFTER_FIRST": "b2b-backup.service",
        })
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(len(self.stamp_dirs()), 1)
        self.assertTrue(self.ossutil_upload_calls())


class SnapshotOutPinningTests(OffsiteBackupTestBase):
    """backup_snapshot.sh 的 B2B_BACKUP_OUT：把“本次生成的快照”路径钉死（异地链的可靠返回路径）。"""

    def test_pinned_out_is_created_exactly_at_given_path(self) -> None:
        pinned = self.backup_root / "20990101-000000"
        result = self.run_inner(env_extra={"B2B_BACKUP_OUT": str(pinned)})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue(pinned.is_dir())
        self.assertTrue((pinned / "manifest.json").is_file())
        self.assertIn("systemctl stop b2b-app", self.log_lines())
        self.assertIn("systemctl start b2b-app", self.log_lines())

    def test_existing_pinned_out_refused_before_stopping_app(self) -> None:
        pinned = self.backup_root / "20990101-000000"
        pinned.mkdir(parents=True)
        (pinned / "keep.txt").write_text("keep", encoding="utf-8")
        result = self.run_inner(env_extra={"B2B_BACKUP_OUT": str(pinned)})
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        lines = self.log_lines()
        self.assertNotIn("systemctl stop b2b-app", lines, "同名冲突必须在停写前拒绝")
        self.assertTrue((pinned / "keep.txt").is_file(), "已存在的目标不得被触碰")
        self.assertTrue(self.app_is_active())

    def test_relative_pinned_out_refused(self) -> None:
        result = self.run_inner(env_extra={"B2B_BACKUP_OUT": "relative/path"})
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertNotIn("systemctl stop b2b-app", self.log_lines())


class SystemdUnitAndDocsTests(unittest.TestCase):
    def test_offsite_service_unit_wiring(self) -> None:
        unit = (UNITS / "b2b-offsite-backup.service").read_text(encoding="utf-8")
        self.assertIn("Type=oneshot", unit)
        self.assertIn("User=root", unit)
        self.assertIn("EnvironmentFile=/etc/b2b/b2b.env", unit)
        self.assertIn("ExecStart=/opt/b2b/app/deploy/offsite_backup_oss.sh", unit)
        after_lines = [line for line in unit.splitlines() if line.startswith("After=")]
        self.assertTrue(any("b2b-backup.service" in line for line in after_lines),
                        "须声明 After=b2b-backup.service，避免两个停写窗口重叠")
        self.assertTrue(any("network-online.target" in line for line in after_lines))

    def test_offsite_timer_unit_wiring(self) -> None:
        unit = (UNITS / "b2b-offsite-backup.timer").read_text(encoding="utf-8")
        self.assertIn("OnCalendar=*-*-* 04:30:00", unit)
        self.assertIn("Persistent=true", unit)
        self.assertIn("RandomizedDelaySec=300", unit)
        self.assertIn("Unit=b2b-offsite-backup.service", unit)
        self.assertIn("WantedBy=timers.target", unit)

    def test_readme_documents_offsite_chain_install(self) -> None:
        readme = README.read_text(encoding="utf-8")
        for token in (
            "offsite_backup_oss.sh",
            "b2b-offsite-backup.service",
            "b2b-offsite-backup.timer",
            "backup-age.pub",
            "ossutil",
            "make_release.py --patch",
            "oss://",
            "--ignore-existing",
        ):
            self.assertIn(token, readme, msg="README 缺少要素: {}".format(token))


class PatchPackageTests(unittest.TestCase):
    """make_release.py --patch：服务器可上传的单独修补包（含修正的 ACME 模板与脚本）。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls._tmp = tempfile.TemporaryDirectory(prefix="t4-patch-")
        cls.out = Path(cls._tmp.name) / "b2b-patch-test.tar.gz"
        cls.build = subprocess.run(
            [sys.executable, str(REPO / "deploy" / "make_release.py"),
             "--source", str(REPO), "--out", str(cls.out), "--label", "offsite-test", "--patch"],
            capture_output=True, text=True, timeout=300, cwd=str(REPO),
        )
        cls.names: "list[str]" = []
        cls.file_hashes: "dict[str, str]" = {}
        cls.package_root = "b2b-patch-offsite-test"
        if cls.out.is_file():
            with tarfile.open(cls.out, "r:gz") as tar:
                for member in tar.getmembers():
                    cls.names.append(member.name)
                    if member.isfile():
                        stream = tar.extractfile(member.name)
                        cls.file_hashes[member.name] = hashlib.sha256(stream.read()).hexdigest()

    @classmethod
    def tearDownClass(cls) -> None:
        cls._tmp.cleanup()

    def test_patch_build_succeeds_with_checksum_file(self) -> None:
        self.assertEqual(self.build.returncode, 0, self.build.stdout + self.build.stderr)
        self.assertTrue(self.out.is_file())
        checksum_file = Path(str(self.out) + ".sha256")
        self.assertTrue(checksum_file.is_file())
        recorded = checksum_file.read_text(encoding="utf-8").split()
        self.assertEqual(recorded[0], hashlib.sha256(self.out.read_bytes()).hexdigest())

    def test_patch_contains_only_task_files(self) -> None:
        expected = {
            "deploy/backup_snapshot.sh",
            "deploy/offsite_backup_oss.sh",
            "deploy/systemd/b2b-offsite-backup.service",
            "deploy/systemd/b2b-offsite-backup.timer",
            "deploy/nginx-b2b.conf.example",
            "deploy/nginx-b2b-http-bootstrap.conf.example",
            "deploy/README.md",
        }
        actual = {
            name[len(self.package_root) + 1:] for name in self.file_hashes
            if name.startswith(self.package_root + "/")
        }
        self.assertEqual(actual, expected | {"RELEASE-MANIFEST.json", "RELEASE-MANIFEST.sha256"})
        for name in self.names:
            self.assertNotIn("data", name.split("/"))
            self.assertNotIn(".git", name.split("/"))
            self.assertFalse(name.split("/")[-1].endswith((".env", ".key", ".pem")))

    def test_patch_template_carries_acme_webroot_fix(self) -> None:
        with tarfile.open(self.out, "r:gz") as tar:
            conf = tar_read(tar, self.package_root + "/deploy/nginx-b2b.conf.example").decode("utf-8")
        self.assertIn("location ^~ /.well-known/acme-challenge/", conf)

    def test_patch_manifest_matches_files(self) -> None:
        with tarfile.open(self.out, "r:gz") as tar:
            manifest = json.loads(tar_read(tar, self.package_root + "/RELEASE-MANIFEST.json").decode("utf-8"))
        self.assertEqual(manifest["label"], "offsite-test")
        self.assertEqual(manifest.get("kind"), "patch")
        files = manifest["files"]
        for rel, info in files.items():
            self.assertEqual(
                info["sha256"], self.file_hashes["{}/{}".format(self.package_root, rel)],
                msg="清单哈希与实际内容不一致: {}".format(rel),
            )


if __name__ == "__main__":
    unittest.main()
