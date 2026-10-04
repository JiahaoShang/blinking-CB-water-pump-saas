// 侧栏模块磁吸放大（macOS Dock 手法竖向适配）回归驱动（Node，无第三方依赖）。
//
// 用 vm 加载未经修改的 CONSOLE_DOCK_JS（由 Python 测试从 app.py 导出到临时文件），
// 配上 stub DOM / 虚拟时钟 / 手动步进的 requestAnimationFrame，输出结构化事实，
// 由 Python 测试（test_console_nav_dock.py）逐项断言：
//   1. 护栏：系统「减少动态效果」或粗指针（无 hover）时不启用（不装监听、不做任何高度写入）；
//   2. 磁吸目标：指针在条目中心 → 该条目全量放大；影响半径内按距离线性衰减；半径外保持原尺寸；
//   3. 弹簧物理：行高从基值平滑单调收敛（参考参数为过阻尼 → 无超调），图标缩放同步，
//      收敛后 rAF 不再排队（无空转）；飞行中重新指向另一条目 → 目标整体转移且数值始终合法；
//   4. 释放：指针离开侧栏后全部弹回基值并停止。
//
// 用法: node console_nav_dock_browser.mjs <config.json> <result.json>
// config: { "dockScriptPath": "…" }
import { readFileSync, writeFileSync } from 'node:fs';
import vm from 'node:vm';

const [configPath, resultPath] = process.argv.slice(2);
if (!configPath || !resultPath) {
  console.error('usage: node console_nav_dock_browser.mjs <config.json> <result.json>');
  process.exit(2);
}
const config = JSON.parse(readFileSync(configPath, 'utf8'));

const ITEM_COUNT = 8;
const BASE_HEIGHT = 36;
const ITEM_STRIDE = 37; // 条目高 36 + 容器 gap 1
const LIST_TOP = 100;

function itemCenter(index) {
  return LIST_TOP + index * ITEM_STRIDE + BASE_HEIGHT / 2;
}

function makeLink(index) {
  const icon = { style: {} };
  return {
    link: {
      style: {},
      offsetHeight: BASE_HEIGHT,
      getBoundingClientRect() {
        return { top: LIST_TOP + index * ITEM_STRIDE, height: BASE_HEIGHT };
      },
      querySelector(selector) {
        return selector === '.ic' ? icon : null;
      },
    },
    icon,
  };
}

function buildEnv({ reduce, fine }) {
  const links = Array.from({ length: ITEM_COUNT }, (_, index) => makeLink(index));
  const sidebarListeners = {};
  const sidebar = {
    addEventListener(type, handler) {
      (sidebarListeners[type] = sidebarListeners[type] || []).push(handler);
    },
  };
  const scrollerListeners = {};
  sidebar.querySelector = (selector) =>
    selector === '.side-nav'
      ? {
          addEventListener(type, handler) {
            (scrollerListeners[type] = scrollerListeners[type] || []).push(handler);
          },
        }
      : null;

  const clock = { now: 1000 };
  const frameQueue = [];
  const sandbox = {
    document: {
      querySelectorAll: (selector) =>
        selector === '.side-nav .side-link' ? links.map((entry) => entry.link) : [],
      getElementById: (id) => (id === 'console-side' ? sidebar : null),
    },
    window: {
      matchMedia: (query) => {
        if (query.includes('prefers-reduced-motion')) return { matches: reduce };
        if (query.includes('hover')) return { matches: fine };
        return { matches: false };
      },
    },
    performance: { now: () => clock.now },
    requestAnimationFrame(callback) {
      frameQueue.push(callback);
      return frameQueue.length;
    },
  };

  const source = readFileSync(config.dockScriptPath, 'utf8');
  vm.runInNewContext(source, sandbox);

  const step = (ms) => {
    clock.now += ms;
    const due = frameQueue.splice(0, frameQueue.length);
    for (const callback of due) callback(clock.now);
  };
  const heightOf = (index) => {
    const raw = links[index].link.style.height;
    return raw === undefined ? null : parseFloat(raw);
  };

  return { links, sidebarListeners, sidebar, clock, frameQueue, step, heightOf };
}

function guardedScenario(flags) {
  const env = buildEnv(flags);
  return {
    initialHeight: env.links[0].link.style.height ?? null,
    listenerTypes: Object.keys(env.sidebarListeners),
    queue: env.frameQueue.length,
  };
}

function enabledScenario() {
  const env = buildEnv({ reduce: false, fine: true });
  const result = { initialHeight: env.links[0].link.style.height ?? null };

  if (!env.sidebarListeners.mousemove) {
    return { ...result, error: 'mousemove listener missing' };
  }
  env.sidebarListeners.mousemove[0]({ clientY: itemCenter(2) });

  const trajectory = [];
  const sampleAll = () => {
    const heights = {};
    for (let index = 0; index < ITEM_COUNT; index += 1) heights[String(index)] = env.heightOf(index);
    return heights;
  };

  for (let frame = 0; frame < 90; frame += 1) {
    env.step(16);
    trajectory.push([env.clock.now - 1000, env.heightOf(2)]);
  }
  result.item2Trajectory = trajectory;
  result.settledHeights = sampleAll();
  result.suppressedItem6 = env.links[6].link.style.height ?? null;
  result.icon2Transform = env.links[2].icon.style.transform ?? null;
  result.stoppedQueue = env.frameQueue.length;

  // 收敛后空步进：不应再有任何写入（高度零漂移）
  const before = env.heightOf(2);
  for (let frame = 0; frame < 5; frame += 1) env.step(16);
  result.idleDrift = Math.abs(env.heightOf(2) - before);

  // 飞行中重新指向 6 号条目：目标整体转移
  env.sidebarListeners.mousemove[0]({ clientY: itemCenter(6) });
  for (let frame = 0; frame < 90; frame += 1) env.step(16);
  result.retargetHeights = sampleAll();
  result.retargetQueue = env.frameQueue.length;

  // 释放：指针离开侧栏
  if (env.sidebarListeners.mouseleave) {
    env.sidebarListeners.mouseleave[0]();
    for (let frame = 0; frame < 90; frame += 1) env.step(16);
    result.releasedHeights = sampleAll();
    result.releasedQueue = env.frameQueue.length;
  }
  return result;
}

let payload;
try {
  payload = {
    ok: true,
    results: {
      reduce: guardedScenario({ reduce: true, fine: true }),
      coarse: guardedScenario({ reduce: false, fine: false }),
      enabled: enabledScenario(),
    },
  };
} catch (error) {
  payload = { ok: false, error: String(error && error.stack ? error.stack : error) };
}

writeFileSync(resultPath, JSON.stringify(payload, null, 2));
if (!payload.ok) process.exitCode = 1;
