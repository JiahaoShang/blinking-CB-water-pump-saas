# -*- coding: utf-8 -*-
"""T5 Telegram 首发联调（LangBot 4.10.11 传输栈）部署侧判别性测试。

覆盖（先红后绿；对应本轮四项最小改动）：
- deploy/langbot-compose.yaml：冻结镜像 digest、两个容器（b2b-langbot / b2b-langbot-plugin）、
  私有网络、核心仅 127.0.0.1:5300 回环、插件不发布宿主端口、b2b.langbot=1 标签、数据目录挂载、
  runtime 命令与共享控制令牌等环境变量契约；文件不含秘密字面值。（nginx 凭据路径回归锁在
  test_first_launch_t4_deploy.NginxConfigTests。）
- deploy/langbot_precheck.py + deploy/backup_snapshot.sh：opt-in 停写——仅当
  connection-runtime/langbot/managed 普通文件存在时：停写之前预检两个固定容器（缺失/被替换/
  标签不符/挂载不属于本数据目录/docker 不可用一律 fail-closed），只停原本运行的容器并在备份后
  经既有 STOPPED_BRIDGES 机制恢复，绝不起动原本未运行的容器；标记缺失时行为与原来完全一致。

隔离口径：沿用既有部署测试的 PATH 假件（systemctl/docker）驱动真实 deploy/backup_snapshot.sh
（真实 shell 逻辑 + 假外部命令）；不触网、不启动真实容器、不读任何真实凭据。真实 Linux/docker
与云端行为仍须在 pilot 受邀实例上验收（deploy/langbot-pilot.md）。
"""
from __future__ import annotations

import json
import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from test_first_launch_t4_deploy import DeployScriptTestBase, find_bundle_dirs  # noqa: E402

COMPOSE = ROOT / "deploy" / "langbot-compose.yaml"
DIGEST = "sha256:ed8c7f19d7e44cda8b59a803be78c4cbecd294e7de1e53d114517f2c034e6e40"
CORE = "b2b-langbot"
PLUGIN = "b2b-langbot-plugin"
DATA_MOUNT = "${B2B_DATA_DIR:-/var/lib/b2b/data}/connection-runtime/langbot"


def service_block(text: str, name: str) -> str:
    """取 services 下某个服务的 YAML 块（两空格缩进的服务键起，到下一个服务/顶层键前），没有则空串。"""
    match = re.search(r"(?ms)^  %s:\s*$\n(.*?)(?=^  \S+:\s*$|^networks:)" % re.escape(name), text)
    return match.group(1) if match else ""


class LangbotComposeTests(unittest.TestCase):
    """deploy/langbot-compose.yaml：冻结传输栈定义（只读文本契约；语法另经真实 `docker compose config` 核验）。"""

    def compose_text(self) -> str:
        self.assertTrue(COMPOSE.is_file(), "必须存在 deploy/langbot-compose.yaml（LangBot pilot 传输栈冻结定义）")
        return COMPOSE.read_text(encoding="utf-8")

    def test_image_frozen_by_digest_on_both_containers(self) -> None:
        text = self.compose_text()
        image_line = "image: rockchin/langbot@" + DIGEST
        lines = [line.strip() for line in text.splitlines() if line.strip().startswith("image:")]
        self.assertEqual(lines, [image_line, image_line], "两个容器必须逐字使用同一冻结 digest 镜像")
        self.assertNotIn(":latest", text, "不得使用可变标签 latest")

    def test_core_loopback_only_and_plugin_never_published(self) -> None:
        text = self.compose_text()
        core = service_block(text, CORE)
        plugin = service_block(text, PLUGIN)
        self.assertTrue(core and plugin, "必须同时定义 b2b-langbot 与 b2b-langbot-plugin 两个服务")
        self.assertIn("container_name: " + CORE, core)
        self.assertIn("container_name: " + PLUGIN, plugin)
        self.assertIn('- "127.0.0.1:5300:5300"', core, "核心端口只允许绑定 127.0.0.1:5300:5300")
        self.assertEqual(text.count("127.0.0.1:5300:5300"), 1, "全文件只允许一处宿主端口发布")
        self.assertNotIn("ports:", plugin, "插件运行时不发布任何宿主端口")
        self.assertNotIn("0.0.0.0", text, "不得出现 0.0.0.0 绑定")
        for label, block in (("core", core), ("plugin", plugin)):
            self.assertIn("b2b-langbot-net", block, f"{label} 必须在私有网络上")
        self.assertIn("networks:\n  b2b-langbot-net:\n    driver: bridge", text, "必须声明专用桥接网络")

    def test_labels_mounts_command_and_restart_policy(self) -> None:
        text = self.compose_text()
        self.assertEqual(text.count('b2b.langbot: "1"'), 2, "两个容器都必须带 b2b.langbot=1 标签（备份停写预检依赖）")
        self.assertIn(DATA_MOUNT + "/core:/app/data", text, "核心挂载必须来自本数据目录 connection-runtime/langbot/core")
        self.assertIn(DATA_MOUNT + "/plugin:/app/data/plugins", text,
                      "插件挂载必须来自本数据目录 connection-runtime/langbot/plugin")
        self.assertIn('command: ["uv", "run", "--no-sync", "-m", "langbot_plugin.cli.__init__", "rt"]',
                      service_block(text, PLUGIN), "插件运行时命令为镜像内 rt（默认控制端口 5400）")
        self.assertEqual(text.count("restart: unless-stopped"), 2)

    def test_env_contract_shared_secrets_without_literals(self) -> None:
        text = self.compose_text()
        core = service_block(text, CORE)
        self.assertIn("API__GLOBAL_API_KEY=${LANGBOT_API_KEY", core, "核心全局 API Key 取自 LANGBOT_API_KEY（与 b2b 应用同值）")
        self.assertIn("PLUGIN__RUNTIME_WS_URL=ws://b2b-langbot-plugin:5400/control/ws", core,
                      "控制连接指向插件运行时控制端口（冻结镜像内 rt 默认 5400；5401 为调试服务，鉴权头不同）")
        token_ref = "LANGBOT_PLUGIN_RUNTIME_CONTROL_TOKEN=${LANGBOT_PLUGIN_RUNTIME_CONTROL_TOKEN"
        self.assertEqual(text.count(token_ref), 2, "两端必须注入同一个控制令牌")
        self.assertIn("BOX__ENABLED=false", core)
        self.assertNotIn("/var/run/docker.sock", text, "不引入 Box/宿主 Docker socket")
        for pattern in (r"LANGBOT_API_KEY=(?!\$)", r"LANGBOT_PLUGIN_RUNTIME_CONTROL_TOKEN=(?!\$)"):
            self.assertIsNone(re.search(pattern, text), "不得出现秘密字面值：%s" % pattern)


class LangbotBackupScriptTests(DeployScriptTestBase):
    """LangBot 停写 opt-in：标记存在才生效；预检 fail-closed；只停原本运行的容器并恢复原状。"""

    def install_marker(self) -> Path:
        marker = self.data_dir / "connection-runtime" / "langbot" / "managed"
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text("", encoding="utf-8")
        for part in ("core", "plugin"):
            (marker.parent / part).mkdir(exist_ok=True)
        return marker

    def langbot_source(self, part: str) -> Path:
        return self.data_dir / "connection-runtime" / "langbot" / part

    def add_langbot_container(self, name: str, running: bool, *, label: str = "1", source=None) -> str:
        """登记容器假件：b2b.langbot 标签 + 单个 bind 挂载（默认指向本数据目录的正确子目录）。"""
        self.add_container(name, running=running)
        containers = self.state / "containers"
        (containers / f"{name}.labels_json").write_text(json.dumps({"b2b.langbot": label}), encoding="utf-8")
        if source is None:
            source = self.langbot_source("core" if name == CORE else "plugin")
        target = "/app/data" if name == CORE else "/app/data/plugins"
        (containers / f"{name}.mounts_json").write_text(
            json.dumps([{"Type": "bind", "Source": str(source), "Destination": target}]), encoding="utf-8")
        return name

    def assert_no_stops_and_no_bundle(self, result) -> None:
        self.assertNotEqual(result.returncode, 0, "标记存在而预检不通过时必须失败")
        self.assertIn("LangBot", result.stderr)
        lines = self.log_lines()
        self.assertNotIn("systemctl stop b2b-app", lines, "预检失败不得先停应用")
        self.assertFalse(any(line.startswith("docker stop") for line in lines), "预检失败不得停任何容器")
        self.assertTrue((self.state / "units" / "b2b-app.active").exists())
        self.assertEqual(find_bundle_dirs(self.backup_root), [], "预检失败不得产生任何备份产物")

    def test_marker_stops_running_containers_before_snapshot_and_restores(self) -> None:
        self.activate_app()
        self.install_marker()
        self.add_langbot_container(CORE, running=True)
        self.add_langbot_container(PLUGIN, running=True)

        result = self.run_script()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        lines = self.log_lines()
        self.assert_order(lines, [
            f"docker inspect-labels {CORE}",
            f"docker inspect-labels {PLUGIN}",
            "systemctl stop b2b-app",
            f"docker stop {CORE}",
            f"docker stop {PLUGIN}",
            "backup-start",
            "backup-ok",
            f"docker start {CORE}",
            f"docker start {PLUGIN}",
            "systemctl start b2b-app",
        ])
        self.assertEqual(lines.count(f"docker stop {CORE}"), 1, "不得重复停止")
        self.assertEqual(lines.count(f"docker stop {PLUGIN}"), 1, "不得重复停止")
        self.assertTrue((self.state / "containers" / f"{CORE}.running").exists(), "原本运行的容器必须恢复运行")
        self.assertTrue((self.state / "containers" / f"{PLUGIN}.running").exists())
        self.assertTrue((self.state / "units" / "b2b-app.active").exists())
        bundles = find_bundle_dirs(self.backup_root)
        self.assertEqual(len(bundles), 1)
        self.assertTrue((bundles[0] / "connection-runtime" / "langbot" / "managed").is_file(),
                        "标记与 LangBot 数据目录随 connection-runtime 进备份包")

    def test_marker_stops_only_running_and_keeps_inactive_untouched(self) -> None:
        self.activate_app()
        self.install_marker()
        self.add_langbot_container(CORE, running=True)
        self.add_langbot_container(PLUGIN, running=False)

        result = self.run_script()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        lines = self.log_lines()
        self.assertIn(f"docker stop {CORE}", lines)
        self.assertNotIn(f"docker stop {PLUGIN}", lines, "原本未运行的容器不得被停")
        self.assertNotIn(f"docker start {PLUGIN}", lines, "不得启动原本未运行的容器")
        self.assertIn(f"docker start {CORE}", lines)
        self.assertTrue((self.state / "containers" / f"{CORE}.running").exists())

    def test_marker_with_missing_container_fails_before_stop(self) -> None:
        self.activate_app()
        self.install_marker()
        self.add_langbot_container(CORE, running=True)  # 插件容器缺失：标记存在即应部署，拒绝停写
        self.assert_no_stops_and_no_bundle(self.run_script())

    def test_marker_with_label_mismatch_fails_before_stop(self) -> None:
        self.activate_app()
        self.install_marker()
        self.add_langbot_container(CORE, running=True, label="")
        self.add_langbot_container(PLUGIN, running=True)
        self.assert_no_stops_and_no_bundle(self.run_script())

    def test_marker_with_foreign_mount_source_fails_before_stop(self) -> None:
        """恢复副本/另一数据根的同名容器：挂载不属于本数据目录，必须拒绝（绝不误停生产容器）。"""
        self.activate_app()
        self.install_marker()
        foreign = self.root / "original-data" / "connection-runtime" / "langbot" / "core"
        self.add_langbot_container(CORE, running=True, source=foreign)
        self.add_langbot_container(PLUGIN, running=True)
        self.assert_no_stops_and_no_bundle(self.run_script())

    def test_marker_with_inspect_failure_fails_before_stop(self) -> None:
        """docker inspect 失败不能当作“不在运行”继续宣称已一致停写。"""
        self.activate_app()
        self.install_marker()
        self.add_langbot_container(CORE, running=True)
        self.add_langbot_container(PLUGIN, running=True)
        (self.state / "containers" / f"{PLUGIN}.inspectfail").write_text("", encoding="utf-8")
        self.assert_no_stops_and_no_bundle(self.run_script())

    def test_marker_with_docker_unavailable_fails_before_stop(self) -> None:
        self.activate_app()
        self.install_marker()
        self.add_langbot_container(CORE, running=True)
        self.add_langbot_container(PLUGIN, running=True)
        self.assert_no_stops_and_no_bundle(self.run_script(with_docker=False))

    def test_backup_failure_restores_langbot_and_app(self) -> None:
        self.activate_app()
        self.install_marker()
        self.add_langbot_container(CORE, running=True)
        self.add_langbot_container(PLUGIN, running=True)

        result = self.run_script(fail_backup=True)
        self.assertNotEqual(result.returncode, 0, "注入的备份失败必须让脚本非零退出")
        lines = self.log_lines()
        self.assertIn(f"docker stop {CORE}", lines)
        self.assertIn(f"docker stop {PLUGIN}", lines)
        self.assertIn("backup-fail", lines)
        self.assertIn(f"docker start {CORE}", lines, "失败后必须恢复原本运行的容器")
        self.assertIn(f"docker start {PLUGIN}", lines)
        self.assertIn("systemctl start b2b-app", lines)
        self.assertTrue((self.state / "containers" / f"{CORE}.running").exists())
        self.assertTrue((self.state / "units" / "b2b-app.active").exists())
        self.assertEqual(find_bundle_dirs(self.backup_root), [], "失败不得留下备份产物")

    def test_without_marker_langbot_is_never_touched(self) -> None:
        self.activate_app()
        self.add_langbot_container(CORE, running=True)
        self.add_langbot_container(PLUGIN, running=True)

        result = self.run_script()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        lines = self.log_lines()
        self.assertFalse(any("b2b-langbot" in line for line in lines),
                         "无标记时不得核对/停止/启动 LangBot 容器（行为与原来完全一致）")
        self.assertTrue((self.state / "units" / "b2b-app.active").exists())
        self.assertEqual(len(find_bundle_dirs(self.backup_root)), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
