"""侧栏模块磁吸放大（macOS Dock 手法竖向适配）：渲染/护栏契约 + 弹簧行为（Node 驱动，无第三方依赖）。

- 渲染契约（始终运行）：控制台页面携带 CONSOLE_DOCK_JS 与两道护栏标记；买家页不携带。
- 行为测试（需 Node >= 18；缺失或版本过低时整类跳过、不冒充已测）：用 vm 加载 app.py 导出的
  CONSOLE_DOCK_JS 原文，stub DOM + 虚拟时钟 + 手动步进的 requestAnimationFrame，验证磁吸目标
  分布、弹簧收敛（过阻尼无超调）、收敛后不空转、飞行中重新指向、释放弹回与两道护栏。
"""
import json
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from app import CONSOLE_DOCK_JS, console_layout, layout

ROOT = Path(__file__).resolve().parent
HARNESS = ROOT / "tools" / "browser_tests" / "console_nav_dock_browser.mjs"
NODE = shutil.which("node")


def node_major_version(path: str) -> int:
    try:
        output = subprocess.run([path, "--version"], capture_output=True, text=True, check=True, timeout=30).stdout.strip()
        match = re.match(r"v(\d+)", output)
        return int(match.group(1)) if match else 0
    except Exception:
        return 0


class ConsoleNavDockRenderTests(unittest.TestCase):
    """渲染与护栏契约（不需 Node）。"""

    def test_console_page_carries_dock_script_and_guards(self):
        page = console_layout("管理概览", "<p>x</p>", "admin", nav_counts={})
        for marker in (
            "磁吸放大",
            ".side-nav .side-link",
            "prefers-reduced-motion",
            "(hover: hover) and (pointer: fine)",
            "requestAnimationFrame",
            "DOCK_GROW = 12",
            "DOCK_RANGE = 120",
            "DOCK_ICON_GROW = 0.35",
            "DOCK_SPRING = { mass: 0.1, stiffness: 150, damping: 12 }",
        ):
            self.assertIn(marker, page, marker)
        # 行高逐帧驱动的前提：.side-link 的 transition 只保留配色属性（否则逐帧更新被过渡拖慢）
        self.assertIn("transition:background-color .13s ease,color .13s ease", page)
        # 放大不被吃掉的布局前提：导航子项不参与 flex 收缩（溢出交给滚动容器）
        self.assertIn(".console .side-nav>*{flex:none}", page)

    def test_buyer_page_does_not_carry_dock_script(self):
        page = layout("产品页", "<p>x</p>")
        self.assertNotIn("DOCK_SPRING", page)
        self.assertNotIn("磁吸放大", page)


@unittest.skipUnless(NODE and node_major_version(NODE) >= 18, "未找到 Node>=18：磁吸放大行为测试跳过（未验证）")
class ConsoleNavDockBehaviorTests(unittest.TestCase):
    maxDiff = None

    @classmethod
    def setUpClass(cls):
        cls.temp_dir = tempfile.TemporaryDirectory()
        script_path = Path(cls.temp_dir.name) / "dock.js"
        script_path.write_text(CONSOLE_DOCK_JS, encoding="utf-8")
        config_path = Path(cls.temp_dir.name) / "config.json"
        config_path.write_text(json.dumps({"dockScriptPath": str(script_path)}), encoding="utf-8")
        cls.result_path = Path(cls.temp_dir.name) / "result.json"
        completed = subprocess.run(
            [NODE, str(HARNESS), str(config_path), str(cls.result_path)],
            capture_output=True, text=True, timeout=120,
        )
        if completed.returncode != 0 or not cls.result_path.exists():
            raise AssertionError(
                f"磁吸放大驱动失败: rc={completed.returncode}\nstdout={completed.stdout}\nstderr={completed.stderr}"
            )
        cls.results = json.loads(cls.result_path.read_text(encoding="utf-8"))
        if not cls.results.get("ok"):
            raise AssertionError(f"磁吸放大驱动异常: {cls.results.get('error')}")

    @classmethod
    def tearDownClass(cls):
        cls.temp_dir.cleanup()

    def test_guards_skip_when_reduced_motion_or_coarse_pointer(self):
        results = self.results["results"]
        for key in ("reduce", "coarse"):
            scenario = results[key]
            self.assertIsNone(scenario["initialHeight"], key)  # 不做任何高度写入
            self.assertEqual(scenario["listenerTypes"], [], key)  # 不装监听
            self.assertEqual(scenario["queue"], 0, key)  # 不排帧

    def test_magnify_distribution_spring_settle_and_retarget(self):
        enabled = self.results["results"]["enabled"]
        self.assertNotIn("error", enabled)
        self.assertEqual(enabled["initialHeight"], "36px")
        # 目标分布：指针在 2 号条目中心（影响半径 120px 内线性衰减）
        settled = {int(key): value for key, value in enabled["settledHeights"].items()}
        expected = {0: 40.60, 1: 44.30, 2: 48.00, 3: 44.30, 4: 40.60, 5: 36.90, 6: 36.00, 7: 36.00}
        for index, want in expected.items():
            self.assertAlmostEqual(settled[index], want, delta=0.08, msg=f"条目{index}")
        # 半径外条目纹丝不动；收敛后不空转、无残留写入；图标与行高同步放大
        self.assertEqual(enabled["suppressedItem6"], "36px")
        self.assertEqual(enabled["stoppedQueue"], 0)
        self.assertLess(enabled["idleDrift"], 0.001)
        icon_scale = float(re.search(r"scale\(([\d.]+)\)", enabled["icon2Transform"]).group(1))
        self.assertAlmostEqual(icon_scale, 1.35, delta=0.01)
        # 弹簧曲线：从首帧起平滑单调上升（过阻尼参数 → 无超调）
        trajectory = [height for _, height in enabled["item2Trajectory"]]
        self.assertGreater(trajectory[0], 36.0)
        self.assertLessEqual(max(trajectory), 48.05)
        for before, after in zip(trajectory, trajectory[1:]):
            self.assertGreaterEqual(after, before - 0.001)
        # 飞行中重新指向 6 号条目：目标整体转移、数值始终合法
        retarget = {int(key): value for key, value in enabled["retargetHeights"].items()}
        self.assertAlmostEqual(retarget[6], 48.00, delta=0.08)
        self.assertAlmostEqual(retarget[2], 36.00, delta=0.08)
        self.assertAlmostEqual(retarget[7], 44.30, delta=0.08)
        self.assertEqual(enabled["retargetQueue"], 0)
        for value in retarget.values():
            self.assertGreaterEqual(value, 35.99)
            self.assertLessEqual(value, 48.05)

    def test_release_returns_to_base_and_stops(self):
        enabled = self.results["results"]["enabled"]
        released = {int(key): value for key, value in enabled["releasedHeights"].items()}
        for index, value in released.items():
            self.assertAlmostEqual(value, 36.0, delta=0.05, msg=f"条目{index}")
        self.assertEqual(enabled["releasedQueue"], 0)


if __name__ == "__main__":
    unittest.main()
