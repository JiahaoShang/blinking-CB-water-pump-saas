# -*- coding: utf-8 -*-
"""T4 部署准备修复轮：备份停写脚本 / HTTPS 引导配置 / systemd 单元 / 发布包工具的判别性测试。

覆盖（先红后绿；见 docs/首次上线-T4执行方案.md 修复轮记录）：
- deploy/backup_snapshot.sh：备份以 root 运行、产物 root 私有；停止前保存原活跃状态，
  失败也经 trap 恢复；只停/启“本库 connection-runtime/ 登记账号”的受管桥容器，
  不触碰其他容器；保留策略 glob 与实际时间戳目录名匹配；运维配置 /etc/b2b/b2b.env 进包。
- deploy/systemd：备份单元读取 /etc/b2b/b2b.env；应用单元保持既有加固与非 root 运行。
- deploy/nginx-*.conf.example：HTTP-only 引导 → nginx -t → certbot → 443 最终配置；
  默认站点关闭连接（444）且不代理应用；精确 server_name 防空 Host 反射；
  访问日志不记录 query/凭据（b2b_nocreds 格式 + /rfq/ 与租户 WhatsApp（个人版）webhook 令牌路径关闭访问日志）。
- deploy/make_release.py：从当前源码白名单导出发布包 + 逐文件 sha256 清单；
  不包含业务 data/、密钥、.git、测试与历史资料原件。

边界（如实）：systemctl/docker 在测试中用 PATH 假件替换——只能验证脚本逻辑与状态恢复；
真实 Linux/systemd/docker 与 HTTPS 验收仍须在服务器（Workbench）按 deploy/README.md 执行。
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
SCRIPT = REPO / "deploy" / "backup_snapshot.sh"
FINAL_NGINX = REPO / "deploy" / "nginx-b2b.conf.example"
BOOTSTRAP_NGINX = REPO / "deploy" / "nginx-b2b-http-bootstrap.conf.example"

SYSTEMCTL_SHIM = """#!/bin/sh
# 测试假件：FAKE_STATE_DIR/units/<unit>.active 存在 == 服务活动。
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
      exit 0
    fi
    [ "$quiet" = "1" ] || echo inactive
    exit 3 ;;
  stop)
    echo "systemctl stop $unit" >> "$log"
    rm -f "$dir/$unit.active"
    exit 0 ;;
  start)
    echo "systemctl start $unit" >> "$log"
    : > "$dir/$unit.active"
    exit 0 ;;
  *) exit 0 ;;
esac
"""

DOCKER_SHIM = """#!/bin/sh
# 测试假件：FAKE_STATE_DIR/containers/<name>.exists / .running / .inspectfail / .stopfail /
# .labels_json / .sessions_source / .media_source / .image 表示容器状态与核对输入。
cmd="$1"
shift
dir="${FAKE_STATE_DIR:-/tmp}/containers"
mkdir -p "$dir"
log="${FAKE_LOG:-/dev/null}"
case "$cmd" in
  info) exit 0 ;;
  inspect)
    template="$2"
    name="$3"
    if [ -f "$dir/$name.inspectfail" ]; then
      echo "Error response from daemon: cannot inspect container $name" >&2
      exit 1
    fi
    if [ -f "$dir/$name.exists" ]; then
      case "$template" in
        *Config.Labels*)
          echo "docker inspect-labels $name" >> "$log"
          if [ -f "$dir/$name.labels_json" ]; then
            cat "$dir/$name.labels_json"
          else
            printf '%s' '{}'
          fi
          exit 0 ;;
        *Mounts*)
          sessions=""
          media=""
          if [ -f "$dir/$name.sessions_source" ]; then sessions="$(cat "$dir/$name.sessions_source")"; fi
          if [ -f "$dir/$name.media_source" ]; then media="$(cat "$dir/$name.media_source")"; fi
          printf '[{"Source":"%s","Destination":"/app/.sessions"},{"Source":"%s","Destination":"/app/.media"}]' "$sessions" "$media"
          exit 0 ;;
        *Config.Image*)
          if [ -f "$dir/$name.image" ]; then cat "$dir/$name.image"; fi
          exit 0 ;;
        *)
          if [ -f "$dir/$name.running" ]; then echo true; else echo false; fi
          exit 0 ;;
      esac
    fi
    echo "Error: No such object: $name" >&2
    exit 1 ;;
  stop)
    if [ -f "$dir/$1.stopfail" ]; then
      echo "Error response from daemon: cannot stop container: $1" >&2
      exit 1
    fi
    echo "docker stop $1" >> "$log"
    rm -f "$dir/$1.running"
    exit 0 ;;
  start)
    echo "docker start $1" >> "$log"
    : > "$dir/$1.running"
    exit 0 ;;
  *) exit 0 ;;
esac
"""

BACKUP_SHIM = '''# -*- coding: utf-8 -*-
"""测试假件：记录调用顺序；可注入失败；成功路径委托给真实 tools/backup_restore.py。"""
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


def find_bundle_dirs(root: Path) -> list[Path]:
    if not root.is_dir():
        return []
    return sorted(p for p in root.iterdir() if p.is_dir() and re.fullmatch(r"\d{8}-\d{6}", p.name))


def nginx_location_block(server_block: str, marker: str) -> str:
    """从 server 块中取出 marker 起至首个右花括号的 location 块（所涉 location 均无嵌套块）。"""
    start = server_block.index(marker)
    return server_block[start: server_block.index("}", start) + 1]


def nginx_directive_lines(location_block: str) -> list[str]:
    """规范取 location 块内的指令行：跳过 location 头、去注释（含行内）、去空行与收尾括号。"""
    lines = [line.split("#", 1)[0].strip() for line in location_block.splitlines()[1:]]
    return [line for line in lines if line and line != "}"]


class DeployScriptTestBase(unittest.TestCase):
    """以 PATH 假件驱动真实 deploy/backup_snapshot.sh（脚本逻辑真执行，外部命令假件）。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="t4-deploy-")
        self.root = Path(self._tmp.name)
        self.state = self.root / "state"
        (self.state / "units").mkdir(parents=True)
        (self.state / "containers").mkdir(parents=True)
        self.log = self.root / "calls.log"
        self.shims = self.root / "shims"
        self.shims.mkdir()
        self._write_shim("systemctl", SYSTEMCTL_SHIM)
        self._write_shim("docker", DOCKER_SHIM)
        self.app_dir = self.root / "app"
        (self.app_dir / "tools").mkdir(parents=True)
        (self.app_dir / "tools" / "backup_restore.py").write_text(BACKUP_SHIM, encoding="utf-8")
        # 脚本会调用真实企业桥注册表工具读取登记容器；沙箱里放一份当前源码副本（含依赖模块）。
        tool_source = REPO / "deploy" / "provision_enterprise_bridge.py"
        if tool_source.is_file():
            (self.app_dir / "deploy").mkdir(parents=True, exist_ok=True)
            shutil.copy2(tool_source, self.app_dir / "deploy" / "provision_enterprise_bridge.py")
            shutil.copy2(REPO / "channel_runtime.py", self.app_dir / "channel_runtime.py")
        self.data_dir = self.root / "data"
        self.uploads = self.data_dir / "uploads"
        self.uploads.mkdir(parents=True)
        self.db_path = self.data_dir / "app.db"
        sqlite3.connect(str(self.db_path)).close()
        self.backup_root = self.root / "backups"
        self.env_file = self.root / "b2b.env"
        self.env_file.write_text("B2B_UPLOAD_DIR=/var/lib/b2b/data/uploads\n", encoding="utf-8")
        os.chmod(self.env_file, 0o600)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    # ── 夹具 ──────────────────────────────────────────────────────────────
    def _write_shim(self, name: str, content: str) -> None:
        path = self.shims / name
        path.write_text(content, encoding="utf-8")
        os.chmod(path, 0o755)

    def activate_app(self) -> None:
        (self.state / "units" / "b2b-app.active").write_text("", encoding="utf-8")

    def add_container(self, name: str, running: bool) -> None:
        (self.state / "containers" / f"{name}.exists").write_text("", encoding="utf-8")
        if running:
            (self.state / "containers" / f"{name}.running").write_text("", encoding="utf-8")

    def add_bridge_account(self, account: str) -> str:
        (self.data_dir / "connection-runtime" / account).mkdir(parents=True, exist_ok=True)
        return f"b2b-account-{account}"

    def add_enterprise_bridge(self, tenant: str, running: bool = True, label: str = "1",
                              sessions_root=None, media_root=None, tenant_label=None) -> str:
        digest = hashlib.sha256(tenant.encode("utf-8")).hexdigest()[:24]
        name = f"b2b-enterprise-{digest}"
        self.add_container(name, running=running)
        containers = self.state / "containers"
        base = self.data_dir / "connection-runtime" / "enterprise" / digest
        (containers / f"{name}.labels_json").write_text(json.dumps({
            "b2b.enterprise.bridge": label,
            "b2b.enterprise.tenant": tenant if tenant_label is None else tenant_label,
        }), encoding="utf-8")
        (containers / f"{name}.sessions_source").write_text(
            str(sessions_root if sessions_root is not None else base / "sessions"), encoding="utf-8")
        (containers / f"{name}.media_source").write_text(
            str(media_root if media_root is not None else base / "media"), encoding="utf-8")
        (containers / f"{name}.image").write_text("devlikeapro/waha:noweb-2026.9.2", encoding="utf-8")
        return name

    def write_registry(self, tenants) -> Path:
        path = self.data_dir / "connection-runtime" / "enterprise-bridges.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        document = {"version": 1, "bridges": {}}
        for tenant in tenants:
            digest = hashlib.sha256(tenant.encode("utf-8")).hexdigest()[:24]
            document["bridges"][tenant] = {
                "container_name": f"b2b-enterprise-{digest}",
                "api_base": "http://127.0.0.1:41000",
                "api_key": "k" * 64,
            }
        path.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")
        return path

    def run_script(self, *, with_docker: bool = True, fail_backup: bool = False) -> subprocess.CompletedProcess:
        env = {
            "PATH": f"{self.shims}:/usr/bin:/bin",
            "FAKE_STATE_DIR": str(self.state),
            "FAKE_LOG": str(self.log),
            "T4_REAL_BACKUP_TOOL": str(REPO / "tools" / "backup_restore.py"),
            "B2B_APP_UNIT": "b2b-app",
            "B2B_APP_DIR": str(self.app_dir),
            "B2B_PY": sys.executable,
            "B2B_DATA_DIR": str(self.data_dir),
            "B2B_BACKUP_ROOT": str(self.backup_root),
            "B2B_BACKUP_KEEP_DAYS": "14",
            "B2B_ENV_FILE": str(self.env_file),
        }
        if not with_docker:
            (self.shims / "docker").unlink()
        if fail_backup:
            env["T4_FAKE_BACKUP_FAIL"] = "1"
        return subprocess.run(
            ["/bin/sh", str(SCRIPT)], env=env, capture_output=True, text=True, timeout=120,
        )

    def log_lines(self) -> list[str]:
        if not self.log.exists():
            return []
        return self.log.read_text(encoding="utf-8").splitlines()

    def assert_order(self, lines: list[str], expected: list[str]) -> None:
        positions = []
        for item in expected:
            self.assertIn(item, lines, msg=f"缺少调用记录: {item}（实际: {lines}）")
            positions.append(lines.index(item))
        self.assertEqual(positions, sorted(positions), msg=f"调用顺序不符: {lines}")


class BackupScriptBehaviorTests(DeployScriptTestBase):
    def test_script_parses_in_sh(self) -> None:
        result = subprocess.run(["/bin/sh", "-n", str(SCRIPT)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_success_flow_saves_state_and_writes_root_private_bundle(self) -> None:
        self.activate_app()
        account_a = "0123456789abcdef01234567"
        name_a = self.add_bridge_account(account_a)
        self.add_container(name_a, running=True)
        name_b = self.add_bridge_account("111111111111111111111111")
        self.add_container(name_b, running=False)
        name_other_account = "b2b-account-222222222222222222222222"  # 未在本库登记
        self.add_container(name_other_account, running=True)
        self.add_container("unrelated-service", running=True)
        self.add_bridge_account("not-a-valid-account")  # 非法目录名：不得产生候选
        self.backup_root.mkdir(parents=True)
        os.chmod(self.backup_root, 0o755)  # 预置过宽权限：脚本必须收紧为 0700

        result = self.run_script()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(oct(self.backup_root.stat().st_mode & 0o777), oct(0o700))
        bundles = find_bundle_dirs(self.backup_root)
        self.assertEqual(len(bundles), 1, msg=f"应恰好产生一个时间戳备份目录: {bundles}")
        bundle = bundles[0]
        self.assertEqual(oct(bundle.stat().st_mode & 0o777), oct(0o700))
        self.assertEqual(oct((bundle / "manifest.json").stat().st_mode & 0o777), oct(0o600))
        self.assertTrue((bundle / "ops-config" / "b2b.env").is_file(), "运维配置必须进包")
        self.assert_order(self.log_lines(), [
            "systemctl stop b2b-app",
            f"docker stop {name_a}",
            "backup-start",
            "backup-ok",
            f"docker start {name_a}",
            "systemctl start b2b-app",
        ])
        lines = self.log_lines()
        self.assertNotIn(f"docker stop {name_b}", lines, "原本未运行的容器不得被停")
        self.assertNotIn(f"docker start {name_b}", lines, "不得启动原本未运行的容器")
        self.assertNotIn(f"docker stop {name_other_account}", lines, "未登记账号的容器不得触碰")
        self.assertNotIn("docker stop unrelated-service", lines, "无关容器不得触碰")
        # 状态恢复：应用回到活动、原本运行的桥回到运行
        self.assertTrue((self.state / "units" / "b2b-app.active").exists())
        self.assertTrue((self.state / "containers" / f"{name_a}.running").exists())

    def test_failed_backup_restores_original_state_and_skips_cleanup(self) -> None:
        self.activate_app()
        name_a = self.add_bridge_account("0123456789abcdef01234567")
        self.add_container(name_a, running=True)
        self.backup_root.mkdir(parents=True)
        aged = self.backup_root / "20200101-000000"
        aged.mkdir()
        set_mtime(aged, 30)

        result = self.run_script(fail_backup=True)
        self.assertNotEqual(result.returncode, 0, "注入的备份失败必须让脚本非零退出")
        lines = self.log_lines()
        self.assertIn("backup-fail", lines)
        self.assertIn(f"docker start {name_a}", lines)
        self.assertIn("systemctl start b2b-app", lines)
        self.assertTrue((self.state / "units" / "b2b-app.active").exists(), "应用必须恢复为活动")
        self.assertTrue((self.state / "containers" / f"{name_a}.running").exists(), "桥必须恢复运行")
        self.assertTrue(aged.exists(), "备份失败时不得执行保留清理")
        remaining = [p for p in find_bundle_dirs(self.backup_root) if p != aged]
        self.assertEqual(remaining, [], "失败时不得留下半成品时间戳目录")

    def test_success_with_inactive_app_keeps_it_stopped(self) -> None:
        result = self.run_script()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        lines = self.log_lines()
        self.assertNotIn("systemctl stop b2b-app", lines)
        self.assertNotIn("systemctl start b2b-app", lines)
        self.assertEqual(len(find_bundle_dirs(self.backup_root)), 1)

    def test_failed_backup_with_inactive_app_starts_nothing(self) -> None:
        result = self.run_script(fail_backup=True)
        self.assertNotEqual(result.returncode, 0)
        lines = self.log_lines()
        self.assertNotIn("systemctl start b2b-app", lines)
        self.assertNotIn("docker start", " ".join(lines))

    def test_docker_unavailable_still_backs_up_with_warning(self) -> None:
        self.activate_app()
        self.add_bridge_account("0123456789abcdef01234567")
        result = self.run_script(with_docker=False)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("Docker", result.stderr)
        lines = self.log_lines()
        self.assertFalse(any(line.startswith("docker ") for line in lines))
        self.assertEqual(len(find_bundle_dirs(self.backup_root)), 1)

    def test_missing_env_file_warns_and_backs_up_without_ops_config(self) -> None:
        self.activate_app()
        self.env_file.unlink()
        result = self.run_script()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        bundles = find_bundle_dirs(self.backup_root)
        self.assertEqual(len(bundles), 1)
        self.assertFalse((bundles[0] / "ops-config").exists())
        self.assertIn("b2b.env", result.stderr)

    def test_retention_prunes_only_aged_stamp_dirs(self) -> None:
        self.activate_app()
        self.backup_root.mkdir(parents=True)
        aged = self.backup_root / "20200101-000000"
        aged.mkdir()
        recent = self.backup_root / "20261013-000000"
        recent.mkdir()
        manual = self.backup_root / "manual-1"
        manual.mkdir()
        note = self.backup_root / "notes.txt"
        note.write_text("keep me", encoding="utf-8")
        set_mtime(aged, 30)
        set_mtime(recent, 0)
        set_mtime(manual, 30)
        set_mtime(note, 30)

        result = self.run_script()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse(aged.exists(), "超过保留天数的“YYYYmmdd-HHMMSS”目录必须被清理")
        self.assertTrue(recent.exists(), "未超过保留天数的目录必须保留")
        self.assertTrue(manual.exists(), "非本工具命名的目录不得清理")
        self.assertTrue(note.exists(), "非目录文件不得清理")
        self.assertEqual(len(find_bundle_dirs(self.backup_root)), 2)  # recent + 本次新备份


class EnterpriseBridgeBackupScriptTests(DeployScriptTestBase):
    """企业桥注册表参与停写：登记后必须能停写（fail-closed），兼容旧账号容器，不触碰无关容器。"""

    def test_registered_running_bridges_stop_once_and_restore(self) -> None:
        self.activate_app()
        account_container = self.add_bridge_account("0123456789abcdef01234567")
        self.add_container(account_container, running=True)
        alpha = self.add_enterprise_bridge("tenant-alpha", running=True)
        beta = self.add_enterprise_bridge("tenant-beta", running=True)
        gamma = self.add_enterprise_bridge("tenant-gamma", running=False)
        digest_delta = hashlib.sha256("tenant-delta".encode("utf-8")).hexdigest()[:24]
        delta = f"b2b-enterprise-{digest_delta}"  # 登记但容器不存在（尚未部署/已移除）
        self.write_registry(["tenant-alpha", "tenant-beta", "tenant-gamma", "tenant-delta"])
        self.add_container("unrelated-service", running=True)

        result = self.run_script()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        lines = self.log_lines()
        self.assert_order(lines, [
            "systemctl stop b2b-app",
            f"docker stop {account_container}",
            "backup-start",
            "backup-ok",
        ])
        for name in (alpha, beta):
            self.assert_order(lines, [f"docker inspect-labels {name}", f"docker stop {name}"])
            self.assertLess(lines.index(f"docker stop {name}"), lines.index("backup-start"),
                            "企业桥必须在快照之前停写")
            self.assertEqual(lines.count(f"docker stop {name}"), 1, "注册企业桥不得被重复停止")
        self.assertNotIn(f"docker stop {gamma}", lines, "未运行的注册桥无需停写")
        self.assertNotIn(f"docker start {gamma}", lines, "不得启动原本未运行的桥")
        self.assertNotIn(f"docker stop {delta}", lines, "不存在的注册容器不得被触碰")
        self.assertNotIn("docker stop unrelated-service", lines, "无关容器不得触碰")
        self.assertTrue((self.state / "containers" / f"{alpha}.running").exists(), "原本运行的桥必须被恢复")
        self.assertTrue((self.state / "containers" / f"{beta}.running").exists())
        self.assertTrue((self.state / "units" / "b2b-app.active").exists())
        bundles = find_bundle_dirs(self.backup_root)
        self.assertEqual(len(bundles), 1)
        registry_copy = bundles[0] / "connection-runtime" / "enterprise-bridges.json"
        self.assertTrue(registry_copy.is_file(), "企业桥注册表必须进入备份包（connection-runtime 整体纳入）")

    def test_restored_copy_never_stops_original_bridge(self) -> None:
        """恢复副本（另一数据根）登记的桥不得停原桥：挂载来源与当前数据目录不一致必须失败。"""
        self.activate_app()
        digest = hashlib.sha256("tenant-alpha".encode("utf-8")).hexdigest()[:24]
        original_enterprise = self.root / "original-data" / "connection-runtime" / "enterprise"
        # 同名容器，但会话/媒体卷来自另一数据根（原库）——恢复副本里可能出现这种注册
        alpha = self.add_enterprise_bridge(
            "tenant-alpha", running=True,
            sessions_root=original_enterprise / digest / "sessions",
            media_root=original_enterprise / digest / "media")
        self.write_registry(["tenant-alpha"])

        result = self.run_script()
        self.assertNotEqual(result.returncode, 0, "挂载不属于本数据目录时必须拒绝（fail-closed）")
        self.assertIn("企业桥", result.stderr)
        lines = self.log_lines()
        self.assertNotIn(f"docker stop {alpha}", lines, "恢复副本不得停原桥")
        self.assertNotIn("systemctl stop b2b-app", lines, "预检失败不得先停应用")
        self.assertTrue((self.state / "containers" / f"{alpha}.running").exists())
        self.assertEqual(find_bundle_dirs(self.backup_root), [])

    def test_unknown_container_state_fails_closed(self) -> None:
        """docker inspect 失败不能当作“不在运行”继续宣称已一致停写。"""
        self.activate_app()
        alpha = self.add_enterprise_bridge("tenant-alpha", running=True)
        self.write_registry(["tenant-alpha"])
        (self.state / "containers" / f"{alpha}.inspectfail").write_text("", encoding="utf-8")

        result = self.run_script()
        self.assertNotEqual(result.returncode, 0, "无法确认容器状态时必须失败")
        self.assertIn("企业桥", result.stderr)
        lines = self.log_lines()
        self.assertNotIn(f"docker stop {alpha}", lines)
        self.assertNotIn("systemctl stop b2b-app", lines, "预检失败不得先停应用")
        self.assertTrue((self.state / "containers" / f"{alpha}.running").exists())
        self.assertEqual(find_bundle_dirs(self.backup_root), [])

    def test_enterprise_stop_failure_fails_backup_and_restores_original_state(self) -> None:
        self.activate_app()
        alpha = self.add_enterprise_bridge("tenant-alpha", running=True)
        self.write_registry(["tenant-alpha"])
        (self.state / "containers" / f"{alpha}.stopfail").write_text("", encoding="utf-8")

        result = self.run_script()
        self.assertNotEqual(result.returncode, 0, "企业桥无法停写时必须失败，而不是宣称一致性成功")
        self.assertIn("企业桥", result.stderr)
        lines = self.log_lines()
        self.assertIn("systemctl start b2b-app", lines, "失败后必须恢复原活跃服务")
        self.assertTrue((self.state / "units" / "b2b-app.active").exists())
        self.assertEqual(find_bundle_dirs(self.backup_root), [], "停写失败不得留下备份产物")
        self.assertTrue((self.state / "containers" / f"{alpha}.running").exists(), "未能停止的容器应保持原状")

    def test_label_mismatch_refuses_to_stop_container(self) -> None:
        self.activate_app()
        alpha = self.add_enterprise_bridge("tenant-alpha", running=True, label="")
        self.write_registry(["tenant-alpha"])

        result = self.run_script()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("企业桥", result.stderr)
        lines = self.log_lines()
        self.assertNotIn(f"docker stop {alpha}", lines, "标签不符的容器必须拒绝停写")
        self.assertNotIn("systemctl stop b2b-app", lines, "预检失败不得先停应用")
        self.assertTrue((self.state / "units" / "b2b-app.active").exists())
        self.assertEqual(find_bundle_dirs(self.backup_root), [])

    def test_registry_with_docker_unavailable_fails_before_stopping_app(self) -> None:
        self.activate_app()
        self.add_enterprise_bridge("tenant-alpha", running=True)
        self.write_registry(["tenant-alpha"])

        result = self.run_script(with_docker=False)
        self.assertNotEqual(result.returncode, 0, "已登记企业桥时 Docker 不可用必须拒绝备份")
        self.assertIn("企业桥", result.stderr)
        self.assertNotIn("systemctl stop b2b-app", self.log_lines(), "预检失败不得先停应用")
        self.assertTrue((self.state / "units" / "b2b-app.active").exists())
        self.assertEqual(find_bundle_dirs(self.backup_root), [])

    def test_empty_registry_keeps_legacy_flow(self) -> None:
        self.activate_app()
        self.write_registry([])

        result = self.run_script()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        lines = self.log_lines()
        self.assertFalse(any(line.startswith("docker stop") for line in lines))
        self.assertEqual(len(find_bundle_dirs(self.backup_root)), 1)


class SystemdUnitTests(unittest.TestCase):
    def test_backup_service_reads_env_file_and_runs_script(self) -> None:
        unit = (REPO / "deploy" / "systemd" / "b2b-backup.service").read_text(encoding="utf-8")
        self.assertIn("EnvironmentFile=/etc/b2b/b2b.env", unit)
        self.assertIn("ExecStart=/opt/b2b/app/deploy/backup_snapshot.sh", unit)
        self.assertIn("User=root", unit)

    def test_app_service_keeps_hardening_and_loopback(self) -> None:
        unit = (REPO / "deploy" / "systemd" / "b2b-app.service").read_text(encoding="utf-8")
        self.assertIn("--host 127.0.0.1", unit)
        self.assertIn("ReadWritePaths=/var/lib/b2b", unit)
        self.assertNotIn("docker", unit.lower())


class NginxConfigTests(unittest.TestCase):
    def test_final_config_host_restriction_and_default_site(self) -> None:
        conf = FINAL_NGINX.read_text(encoding="utf-8")
        self.assertIn("server_name pilot.alpsaga.tech;", conf)
        self.assertIn("listen 80 default_server;", conf)
        self.assertIn("listen 443 ssl default_server;", conf)
        self.assertIn("ssl_reject_handshake on;", conf)
        blocks = conf.split("server {")
        self.assertGreaterEqual(len(blocks), 4, "应为 默认站点 + 80 跳转 + 443 站点 三段")
        default_block = blocks[1]
        self.assertIn("return 444;", default_block)
        self.assertNotIn("proxy_pass", default_block, "默认站点不得代理应用")
        named_block = blocks[-1]
        self.assertIn("proxy_pass http://127.0.0.1:8000;", named_block)
        self.assertIn("ssl_certificate", named_block)
        self.assertIn("client_max_body_size 12m;", named_block)

    def test_final_config_redirect_is_exact_host(self) -> None:
        conf = FINAL_NGINX.read_text(encoding="utf-8")
        self.assertIn("return 301 https://pilot.alpsaga.tech$request_uri;", conf)

    def test_final_config_keeps_acme_webroot_for_renewal(self) -> None:
        """真机续期阻断回归（2026-10-13）：80 全站 301 后 CA 跟随跳转到 HTTPS 取校验文件，
        443 站点必须保留 ACME webroot location 直读文件（^~ 优先于 location /；不得代理进应用）。
        缺它时校验请求落入 location / 被代理进应用返回 404，certbot renew 实际失败。"""
        conf = FINAL_NGINX.read_text(encoding="utf-8")
        named_block = conf.split("server {")[-1]  # 443 精确域名站点（与既有用例同取法）
        self.assertIn("listen 443 ssl;", named_block)
        self.assertIn("server_name pilot.alpsaga.tech;", named_block)
        marker = "location ^~ /.well-known/acme-challenge/"
        self.assertIn(marker, named_block, "443 最终站点必须保留 ACME webroot location（续期经 HTTPS 取校验文件）")
        location = named_block[named_block.index(marker):]
        location = location[: location.index("}") + 1]
        self.assertIn("root /var/www/b2b-acme;", location)
        self.assertIn("default_type text/plain;", location)
        self.assertNotIn("proxy_pass", location, "校验文件必须由 webroot 直读，不得代理进应用")
        bootstrap = BOOTSTRAP_NGINX.read_text(encoding="utf-8")
        self.assertIn("root /var/www/b2b-acme;", bootstrap, "引导与最终两阶段必须共用同一 webroot 目录")

    def test_final_config_access_log_omits_query_and_token_paths(self) -> None:
        conf = FINAL_NGINX.read_text(encoding="utf-8")
        log_lines = [line for line in conf.splitlines() if line.startswith("log_format")]
        self.assertEqual(len(log_lines), 1, "必须定义唯一的 b2b_nocreds 日志格式")
        log_line = log_lines[0]
        self.assertIn("$uri", log_line)
        self.assertNotIn("$request_uri", log_line)
        self.assertNotIn('"$request"', log_line)
        self.assertNotIn("referer", log_line, "Referer 可能带 query，不得记录")
        index = conf.index("location ^~ /rfq/")
        self.assertIn("access_log off;", conf[index:index + 400], "/rfq/ 凭据路径必须关闭访问日志")
        self.assertIn("error_log", conf)
        self.assertIn("crit", conf, "错误日志收敛到 crit，避免请求行（含 URI）落盘")

    def test_final_config_whatsapp_personal_webhook_suppresses_access_log_and_keeps_proxy(self) -> None:
        """租户 WhatsApp（个人版）webhook 令牌在路径里（2026-10-14 真机核对：令牌出现在访问日志）：
        `/api/v1/channels/whatsapp-personal/webhook/<token>` 与 b2b_nocreds 记录的 `$uri` 叠加
        ⇒ 令牌随每条事件写盘。修复=单独 `^~` location 关闭访问日志，且转发口径与应用代理
        （`location /`）逐行一致——请求仍进应用（app.py 实际路由：前缀 + 16~96 位 [A-Za-z0-9_-] 令牌，
        字符集不含“/”，前缀 location 恰好覆盖整条令牌路径）。
        真实生效以服务器 nginx -t + 部署后日志核对为准（deploy/README.md ⑨/⑩）。
        """
        conf = FINAL_NGINX.read_text(encoding="utf-8")
        named_block = conf.split("server {")[-1]  # 443 精确域名站点（与既有用例同取法）
        marker = "location ^~ /api/v1/channels/whatsapp-personal/webhook/"
        self.assertEqual(named_block.count(marker), 1, "443 站点必须有且仅有一处该 webhook location（^~ 前缀）")
        block = nginx_location_block(named_block, marker)
        body = nginx_directive_lines(block)
        self.assertIn("access_log off;", body, "webhook 令牌路径必须关闭访问日志（$uri 会把 /webhook/<token> 写盘）")
        app_proxy = nginx_directive_lines(nginx_location_block(named_block, "location / {"))
        self.assertEqual([line for line in body if line != "access_log off;"], app_proxy,
                         "除 access_log off 外，webhook location 必须与既有应用代理逐行一致（同 proxy_pass/头/超时）")
        app_source = (REPO / "app.py").read_text(encoding="utf-8")
        self.assertIn('r"/api/v1/channels/whatsapp-personal/webhook/([A-Za-z0-9_-]{16,96})"', app_source,
                      "app.py 实际路由变化：nginx 前缀 location 须同步复核")
        blocks = conf.split("server {")
        self.assertNotIn("whatsapp-personal", blocks[1], "默认站点不得涉及该路径（保持 444 口径）")
        self.assertNotIn("whatsapp-personal", blocks[2], "80 跳转站点不得涉及该路径（保持整站跳转口径）")

    def test_bootstrap_config_is_http_only_and_does_not_expose_app(self) -> None:
        conf = BOOTSTRAP_NGINX.read_text(encoding="utf-8")
        self.assertIn("server_name pilot.alpsaga.tech;", conf)
        self.assertIn("listen 80 default_server;", conf)
        self.assertIn("return 444;", conf)
        self.assertIn(".well-known/acme-challenge/", conf)
        self.assertIn("root /var/www/b2b-acme;", conf)
        self.assertIn("return 404;", conf)
        self.assertNotIn("listen 443", conf, "引导阶段只开 80（先签证再上 443）")
        self.assertNotIn("proxy_pass", conf, "引导阶段不得暴露应用")

    def test_readme_documents_bootstrap_sequence(self) -> None:
        readme = (REPO / "deploy" / "README.md").read_text(encoding="utf-8")
        for token in ("nginx-b2b-http-bootstrap.conf.example", "certbot", "pilot.alpsaga.tech",
                      "nginx-b2b.conf.example", "make_release.py"):
            self.assertIn(token, readme, msg=f"README 缺少步骤要素: {token}")


class ReleasePackageTests(unittest.TestCase):
    """发布包：从当前源码白名单导出 + sha256 清单；不含 data/密钥/.git/测试与历史资料。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls._tmp = tempfile.TemporaryDirectory(prefix="t4-release-")
        cls.out = Path(cls._tmp.name) / "b2b-release-test.tar.gz"
        cls.build = subprocess.run(
            [sys.executable, str(REPO / "deploy" / "make_release.py"),
             "--source", str(REPO), "--out", str(cls.out), "--label", "t4-test"],
            capture_output=True, text=True, timeout=300, cwd=str(REPO),
        )
        cls.package_root = "b2b-release-t4-test"
        cls.file_hashes: dict[str, str] = {}
        cls.names: list[str] = []
        if cls.out.is_file():
            with tarfile.open(cls.out, "r:gz") as tar:
                for member in tar.getmembers():
                    cls.names.append(member.name)
                    if member.isfile():
                        cls.file_hashes[member.name] = hashlib.sha256(tar_read(tar, member.name)).hexdigest()

    @classmethod
    def tearDownClass(cls) -> None:
        cls._tmp.cleanup()

    def test_build_succeeds_and_writes_package_checksum(self) -> None:
        self.assertEqual(self.build.returncode, 0, self.build.stdout + self.build.stderr)
        self.assertTrue(self.out.is_file())
        checksum_file = Path(str(self.out) + ".sha256")
        self.assertTrue(checksum_file.is_file())
        recorded = checksum_file.read_text(encoding="utf-8").split()
        actual = hashlib.sha256(self.out.read_bytes()).hexdigest()
        self.assertEqual(recorded[0], actual)

    def test_package_contains_runtime_assets_and_templates(self) -> None:
        required = [
            f"{self.package_root}/app.py",
            f"{self.package_root}/reference-client/app.js",
            f"{self.package_root}/static/knowledge-phase2.js",
            f"{self.package_root}/assets/brand.png",
            f"{self.package_root}/requirements-knowledge.txt",
            f"{self.package_root}/deploy/backup_snapshot.sh",
            f"{self.package_root}/deploy/make_release.py",
            f"{self.package_root}/deploy/provision_enterprise_bridge.py",
            f"{self.package_root}/deploy/nginx-b2b.conf.example",
            f"{self.package_root}/deploy/systemd/b2b-app.service",
            f"{self.package_root}/tools/backup_restore.py",
            f"{self.package_root}/tools/create_admin.py",
            f"{self.package_root}/tools/pilot_checks/check_first_launch_t4.py",
            f"{self.package_root}/RELEASE-MANIFEST.json",
            f"{self.package_root}/RELEASE-MANIFEST.sha256",
        ]
        for name in required:
            self.assertIn(name, self.names, msg=f"发布包缺少: {name}")

    def test_package_excludes_data_secrets_git_tests_and_history(self) -> None:
        for name in self.names:
            segments = name.split("/")
            basename = segments[-1]
            self.assertNotIn("data", segments, msg=f"不得打包业务数据: {name}")
            self.assertNotIn(".git", segments, msg=f"不得打包 .git: {name}")
            self.assertNotIn("docs", segments, msg=f"不得打包 docs（含 evidence/历史）：{name}")
            self.assertNotIn("design", segments)
            self.assertNotIn("fixtures", segments, msg=f"不得打包演示夹具: {name}")
            self.assertNotIn("B2B_Pump_Demo_Research", segments)
            self.assertNotIn("__pycache__", segments)
            self.assertFalse(basename.startswith("test_"), msg=f"不得打包测试文件: {name}")
            self.assertFalse(basename.endswith((".pyc", ".db", ".log", ".env")), msg=f"不得打包: {name}")

    def test_manifest_hashes_match_every_file(self) -> None:
        import json

        manifest_name = f"{self.package_root}/RELEASE-MANIFEST.json"
        with tarfile.open(self.out, "r:gz") as tar:
            manifest = json.loads(tar_read(tar, manifest_name).decode("utf-8"))
        files = manifest["files"]
        actual_files = {
            name[len(self.package_root) + 1:] for name in self.file_hashes
            if name.startswith(self.package_root + "/")
        }
        self.assertEqual(set(files), actual_files - {"RELEASE-MANIFEST.json", "RELEASE-MANIFEST.sha256"})
        for rel, info in files.items():
            self.assertEqual(
                info["sha256"], self.file_hashes[f"{self.package_root}/{rel}"],
                msg=f"清单哈希与实际内容不一致: {rel}",
            )
        self.assertEqual(manifest["label"], "t4-test")
        head = manifest["source_head"]
        self.assertTrue(head is None or re.fullmatch(r"[0-9a-f]{40}", head), msg=f"source_head 形态异常: {head!r}")
        self.assertIn(manifest["source_dirty"], (True, False, None))

    def test_manifest_sha256sum_file_verifies_all_files(self) -> None:
        sums_name = f"{self.package_root}/RELEASE-MANIFEST.sha256"
        with tarfile.open(self.out, "r:gz") as tar:
            content = tar_read(tar, sums_name).decode("utf-8")
        entries = {}
        for line in content.splitlines():
            line = line.strip()
            if not line:
                continue
            digest, path = line.split("  ", 1)
            entries[path] = digest
        expected = {
            name[len(self.package_root) + 1:] for name in self.file_hashes
            if name.startswith(self.package_root + "/")
        } - {"RELEASE-MANIFEST.sha256"}
        self.assertEqual(set(entries), expected)
        for rel, digest in entries.items():
            self.assertEqual(digest, self.file_hashes[f"{self.package_root}/{rel}"])

    def test_extracted_package_imports_and_deploy_script_parses(self) -> None:
        extract_root = Path(self._tmp.name) / "extract"
        with tarfile.open(self.out, "r:gz") as tar:
            tar.extractall(extract_root)
        package = extract_root / self.package_root
        env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONPATH": str(package),
        }
        result = subprocess.run(
            [sys.executable, "-c",
             "import app; import tools.backup_restore; import tools.create_admin; "
             "import channel_runtime; import enterprise_knowledge_phase2_ui; print('IMPORT-OK')"],
            cwd=str(package), env=env, capture_output=True, text=True, timeout=180,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("IMPORT-OK", result.stdout)
        syntax = subprocess.run(
            ["/bin/sh", "-n", str(package / "deploy" / "backup_snapshot.sh")],
            capture_output=True, text=True,
        )
        self.assertEqual(syntax.returncode, 0, syntax.stderr)

    def test_build_fails_closed_on_incomplete_source(self) -> None:
        empty = Path(self._tmp.name) / "empty-source"
        empty.mkdir(exist_ok=True)
        result = subprocess.run(
            [sys.executable, str(REPO / "deploy" / "make_release.py"),
             "--source", str(empty), "--out", str(Path(self._tmp.name) / "empty.tar.gz"), "--label", "empty"],
            capture_output=True, text=True, timeout=120, cwd=str(REPO),
        )
        self.assertNotEqual(result.returncode, 0)
        output = result.stdout + result.stderr
        self.assertIn("app.py", output)


if __name__ == "__main__":
    unittest.main()
