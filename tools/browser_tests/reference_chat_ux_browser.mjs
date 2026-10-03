// 参考聊天窗口交互回归驱动（Node，无第三方依赖）。
//
// 用 vm 加载未经修改的 reference-client/app.js，配上 DOM / localStorage / fetch 桩，
// 驱动四类场景并输出结构化事实，由 Python 测试（test_reference_client_chat_ux.py）断言：
//   1. 系统语言 en → 英文欢迎语（且无「提出产品问题」常驻表单）；
//   2. 系统语言 zh → 中文欢迎语；切换按钮可互换语言（欢迎语随之切换）；
//   3. 统一输入框「需求描述」→ POST /messages（真实 HTTP），渲染需求草稿卡；
//   4. 统一输入框「提问」→ POST /questions（自动绑定当前推荐型号），渲染有据回答。
//
// 用法: node reference_chat_ux_browser.mjs <config.json> <result.json>
import { readFileSync, writeFileSync } from 'node:fs';
import vm from 'node:vm';

const [configPath, resultPath] = process.argv.slice(2);
if (!configPath || !resultPath) {
  console.error('usage: node reference_chat_ux_browser.mjs <config.json> <result.json>');
  process.exit(2);
}
const config = JSON.parse(readFileSync(configPath, 'utf8'));
const source = readFileSync(new URL('../../reference-client/app.js', import.meta.url), 'utf8');
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

function makeEl(tag = 'div', id = '') {
  const el = {
    tagName: String(tag).toUpperCase(), id,
    children: [], listeners: {}, dataset: {}, style: {},
    className: '', textContent: '', value: '', placeholder: '', hidden: false,
    checked: false, disabled: false, type: '', name: '', href: '', target: '', rel: '',
    required: false, rows: 0, scrollTop: 0, scrollHeight: 0,
    setAttribute(name, value) { el[name] = value; },
    getAttribute(name) { return Object.prototype.hasOwnProperty.call(el, name) ? el[name] : null; },
    removeAttribute() {},
    addEventListener(type, handler) { (el.listeners[type] = el.listeners[type] || []).push(handler); },
    removeEventListener() {},
    append(...items) { el.children.push(...items); },
    appendChild(item) { el.children.push(item); return item; },
    prepend(...items) { el.children.unshift(...items); },
    replaceChildren(...items) { el.children = items.slice(); },
    remove() {},
    focus() {},
    querySelector() { return null; },
    querySelectorAll() { return []; },
    closest() { return null; },
    contains() { return false; },
    classList: { add() {}, remove() {}, toggle() {}, contains() { return false; } },
  };
  return el;
}

function textOf(el) {
  const parts = [];
  if (typeof el.textContent === 'string' && el.textContent) parts.push(el.textContent);
  for (const child of el.children || []) parts.push(textOf(child));
  return parts.filter(Boolean).join(' ');
}

function makeStorage(initial = {}) {
  const map = new Map(Object.entries(initial));
  return {
    getItem: (key) => (map.has(String(key)) ? map.get(String(key)) : null),
    setItem: (key, value) => { map.set(String(key), String(value)); },
    removeItem: (key) => { map.delete(String(key)); },
  };
}

const IDS = ['chat-log', 'chat-notice', 'chat-launcher', 'chat-panel', 'chat-close', 'chat-language',
  'connect', 'site-key', 'show-rfq', 'refresh', 'stop-contact', 'message-form', 'message', 'chat-title', 'chat-subtitle'];

function openChat({ language, languages, session = {} }) {
  const journal = [];
  const elements = {};
  for (const id of IDS) elements[id] = makeEl('div', id);
  const sendButton = makeEl('button');
  elements['message-form'].querySelector = (selector) => (selector === 'button' ? sendButton : null);
  const fetchImpl = async (input, init = {}) => {
    const raw = String(input);
    const target = raw.startsWith('http') ? raw : config.baseUrl + raw;
    const method = (init.method || 'GET').toUpperCase();
    const headers = { ...(init.headers || {}) };
    // 浏览器对非同源/跨站请求都会带 Origin；这里显式补上，让服务端来源校验按真实路径生效。
    if (method !== 'GET' && !headers.Origin) headers.Origin = config.origin;
    const response = await fetch(target, { ...init, headers });
    const clone = response.clone();
    let requestBody = null;
    if (typeof init.body === 'string' && init.body) {
      try { requestBody = JSON.parse(init.body); } catch { requestBody = init.body; }
    }
    const record = {
      method, url: raw, status: response.status,
      idempotencyKey: headers['Idempotency-Key'] || null,
      requestBody, responseBody: null,
    };
    if (method !== 'GET') {
      try { record.responseBody = await clone.json(); } catch { /* 非 JSON 响应 */ }
    }
    journal.push(record);
    return response;
  };
  const context = vm.createContext({
    document: { createElement: (tag) => makeEl(tag), getElementById: (id) => elements[id] || null },
    location: { search: `?site_key=${encodeURIComponent(config.siteKey)}`, hash: '', pathname: '/reference-client' },
    localStorage: makeStorage(session),
    fetch: fetchImpl,
    navigator: { language, languages },
    crypto: globalThis.crypto,
    URLSearchParams,
    console,
  });
  vm.runInContext(source, context, { filename: 'reference-client/app.js' });

  return {
    journal, elements,
    logText: () => textOf(elements['chat-log']),
    languageLabel: () => elements['chat-language'].textContent,
    async connect() {
      elements['site-key'].value = config.siteKey;
      for (const handler of elements['connect'].listeners.click || []) await handler();
    },
    async submitMessage(text) {
      elements['message'].value = text;
      for (const handler of elements['message-form'].listeners.submit || []) {
        await handler({ preventDefault() {} });
      }
      await sleep(30);
    },
    async clickLanguage() {
      for (const handler of elements['chat-language'].listeners.click || []) await handler();
    },
    snapshot() {
      return { logText: this.logText(), languageLabel: this.languageLabel(), messageValue: elements['message'].value };
    },
  };
}

const results = {};
try {
  // 场景 1：英文系统 → 英文欢迎语（无连接、无会话时的首屏）
  {
    const chat = openChat({ language: 'en-US', languages: ['en-US', 'en'] });
    results.greeting_en = chat.snapshot();
  }
  // 场景 2：中文系统 → 中文欢迎语；切到 EN 后欢迎语切换
  {
    const chat = openChat({ language: 'zh-CN', languages: ['zh-CN', 'zh'] });
    results.greeting_zh = chat.snapshot();
    await chat.clickLanguage();
    results.after_toggle = chat.snapshot();
  }
  // 场景 3：需求描述 → POST /messages（真实 HTTP），渲染需求草稿卡
  {
    const session = { [config.sessionKey]: JSON.stringify({ conversationId: config.requirementConversationId, token: config.requirementToken }) };
    const chat = openChat({ language: 'en-US', languages: ['en-US'], session });
    await chat.connect();
    await chat.submitMessage('flow 50 m3/h, head 30 m, clean water');
    results.requirement_route = { ...chat.snapshot(), requests: chat.journal };
  }
  // 场景 4：提问 → POST /questions（自动绑定推荐型号），渲染有据回答
  {
    const session = { [config.sessionKey]: JSON.stringify({ conversationId: config.questionConversationId, token: config.questionToken }) };
    const chat = openChat({ language: 'en-US', languages: ['en-US'], session });
    await chat.connect();
    await chat.submitMessage('What is the flow range?');
    results.question_route = { ...chat.snapshot(), requests: chat.journal };
  }
  writeFileSync(resultPath, JSON.stringify({ ok: true, results }, null, 2));
} catch (error) {
  writeFileSync(resultPath, JSON.stringify({ ok: false, error: String((error && error.stack) || error) }, null, 2));
  process.exitCode = 1;
}
