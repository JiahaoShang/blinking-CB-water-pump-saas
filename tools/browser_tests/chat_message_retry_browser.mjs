// 参考聊天窗消息重试逻辑的“浏览器端”回归驱动（Node，无第三方依赖）。
//
// 针对 B-08（Codex 独立复现）：消息提取失败返回 503 后，参考聊天窗把成功（2xx）一律
// 当作已处理并清空输入；而服务恢复后同键重试拿到的仍是「无草稿的成功响应」时，买家
// 无法依提示完成重试。本驱动用 vm 加载未经修改的 reference-client/app.js，配上
// localStorage / DOM / fetch 桩，对真实 HTTP 服务按时间顺序驱动两类场景：
//   1. 提取失败（服务端故障注入一次）→ 503：提示可见、输入保留、提交键保留；
//      服务恢复后同键重试 → 200 duplicate + 草稿：输入清空、界面刷新；两请求同键。
//   2. 旧版服务的无草稿成功响应（200 duplicate, requirement_draft=null，按 B-08
//      报告的原始载荷由 fetch 桩注入）：不得清空输入、显示提示、保留同一提交键；
//      再次提交命中真实服务 → 201 saved + 草稿 → 输入清空。
//
// 输出结构化事实（请求日志、输入/提示快照）到 result.json，由 Python 测试
// （test_chat_message_browser_js.py）对真实数据库/API 逐项断言。
//
// 用法: node chat_message_retry_browser.mjs <config.json> <result.json>
import { readFileSync, writeFileSync } from 'node:fs';
import { randomUUID } from 'node:crypto';
import vm from 'node:vm';

const [configPath, resultPath] = process.argv.slice(2);
if (!configPath || !resultPath) {
  console.error('usage: node chat_message_retry_browser.mjs <config.json> <result.json>');
  process.exit(2);
}
const config = JSON.parse(readFileSync(configPath, 'utf8'));
const source = readFileSync(new URL('../../reference-client/app.js', import.meta.url), 'utf8');

function sleep(ms) { return new Promise((resolve) => setTimeout(resolve, ms)); }

class StubNode {
  constructor(tag) {
    this.tagName = tag; this.children = []; this.listeners = {};
    this.className = ''; this.textContent = ''; this.value = '';
    this.hidden = false; this.checked = false; this.scrollTop = 0; this.scrollHeight = 0;
  }
  append(...items) { this.children.push(...items); }
  prepend(...items) { this.children.unshift(...items); }
  replaceChildren(...items) { this.children = [...items]; }
  addEventListener(type, handler) { this.listeners[type] = handler; }
  setAttribute(name, value) { this[name] = value; }
  querySelector(selector) { return this.querySelectorImpl ? this.querySelectorImpl(selector) : null; }
  remove() {}
  focus() {}
}

function makeStorage() {
  const map = new Map();
  return {
    getItem: (key) => (map.has(String(key)) ? map.get(String(key)) : null),
    setItem: (key, value) => { map.set(String(key), String(value)); },
    removeItem: (key) => { map.delete(String(key)); },
    snapshot: () => Object.fromEntries(map),
  };
}

const journal = [];
const mockQueue = [];
const runFailures = [];

const elements = {};
for (const id of ['chat-log', 'chat-notice', 'chat-panel', 'site-key', 'message', 'connect', 'refresh', 'show-rfq', 'stop-contact', 'chat-launcher', 'chat-launcher-label', 'chat-close', 'chat-language', 'chat-title', 'chat-subtitle']) {
  elements[id] = new StubNode(id === 'message' ? 'textarea' : 'div');
}
const messageForm = new StubNode('form');
const messageSubmitButton = new StubNode('button');
messageForm.querySelectorImpl = (selector) => (selector === 'button' ? messageSubmitButton : null);
elements['message-form'] = messageForm;

const storage = makeStorage();
storage.setItem(`pump_reference_session_${config.siteKey}`, JSON.stringify({ conversationId: config.conversationId, token: config.token }));

const fetchImpl = async (input, init = {}) => {
  const raw = String(input);
  const target = raw.startsWith('http') ? raw : config.baseUrl + raw;
  const method = (init.method || 'GET').toUpperCase();
  const headers = { ...(init.headers || {}) };
  // 浏览器对同源 POST 会自动带 Origin；这里显式补上，让服务端来源校验按真实路径生效。
  if (method !== 'GET' && !headers.Origin) headers.Origin = config.baseUrl;
  const record = { method, url: raw, status: null, idempotencyKey: headers['Idempotency-Key'] || null, body: null };
  const mockIndex = mockQueue.findIndex((entry) => (entry.match ? entry.match(raw, method) : method === 'POST' && raw.includes('/messages')));
  if (mockIndex !== -1) {
    const entry = mockQueue.splice(mockIndex, 1)[0];
    record.status = entry.status;
    record.body = entry.body;
    record.mock = true;
    journal.push(record);
    return new Response(JSON.stringify(entry.body), { status: entry.status, headers: { 'Content-Type': 'application/json' } });
  }
  const response = await fetch(target, { ...init, headers });
  const clone = response.clone();
  record.status = response.status;
  if (method !== 'GET') {
    try { record.body = await clone.json(); } catch { /* 非 JSON 响应 */ }
  }
  journal.push(record);
  return response;
};

const context = vm.createContext({
  document: { getElementById: (id) => elements[id] || null, createElement: (tag) => new StubNode(tag), addEventListener() {}, hidden: false },
  location: { search: `?site_key=${config.siteKey}` },
  localStorage: storage,
  fetch: fetchImpl,
  crypto: { randomUUID },
  URLSearchParams,
  setInterval: () => 0,
  clearInterval: () => {},
  console,
});
vm.runInContext(source, context, { filename: 'reference-client/app.js' });

function state() {
  return { input: elements['message'].value, notice: elements['chat-notice'].textContent };
}

async function submitText(text) {
  elements['message'].value = text;
  await messageForm.listeners.submit({ preventDefault() {} });
  await sleep(15);
}

const scenarios = [];
async function scenario(name, run) {
  const record = { name };
  const from = journal.length;
  scenarios.push(record);
  try {
    await run(record);
  } catch (error) {
    record.error = String((error && error.stack) || error);
    runFailures.push(name);
  }
  record.requests = journal.slice(from);
}

try {
  // 连接（已保存会话）：点击 Connect 后完成 refresh，且不显示任何错误
  await elements['connect'].listeners.click();
  await sleep(50);
  const connectState = state();
  if (connectState.notice) throw new Error(`连接失败：${connectState.notice}`);
  if (!journal.some((entry) => entry.method === 'GET' && entry.url.includes('/conversations/'))) throw new Error('连接后未见会话刷新请求');

  // 1. 提取失败（服务端注入一次）→ 503；同键重试（服务恢复）→ 200 + 草稿
  await scenario('failure_then_retry_recovery', async (rec) => {
    await submitText(config.failureText);
    rec.afterFirstSubmit = state();
    await submitText(config.failureText);
    rec.afterRetrySubmit = state();
  });

  // 2. 旧版服务的无草稿成功响应：不清空输入、保留同一键；再次提交 → 201 + 草稿
  await scenario('legacy_no_draft_success_guard', async (rec) => {
    mockQueue.push({ status: 200, body: { message_id: 'legacy-buggy-server', status: 'duplicate', requirement_draft: null } });
    await submitText(config.mockText);
    rec.afterMockedSubmit = state();
    await submitText(config.mockText);
    rec.afterRecoveredSubmit = state();
  });

  writeFileSync(resultPath, JSON.stringify({ ok: runFailures.length === 0, connectState, scenarios, journal }, null, 2));
  if (runFailures.length) process.exit(1);
} catch (error) {
  writeFileSync(resultPath, JSON.stringify({ ok: false, error: String((error && error.stack) || error), scenarios, journal }, null, 2));
  process.exit(1);
}
