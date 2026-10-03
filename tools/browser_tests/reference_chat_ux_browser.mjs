// 参考聊天窗口交互回归驱动（Node，无第三方依赖）。
//
// 用 vm 加载未经修改的 reference-client/app.js，配上 DOM / localStorage / fetch 桩与真实定时器，
// 驱动六类场景并输出结构化事实，由 Python 测试（test_reference_client_chat_ux.py）断言：
//   1. 系统语言 en → 英文欢迎语（且无「提出产品问题」常驻表单）；
//   2. 系统语言 zh → 中文欢迎语；切换按钮可互换语言（欢迎语随之切换）；
//   3. 统一输入框「需求描述」→ POST /messages（真实 HTTP），渲染需求草稿卡；
//   4. 统一输入框「提问」→ POST /questions（自动绑定当前推荐型号），渲染有据回答；
//   5. 工程师追问待回复时：卡片只给提示（无独立表单），买家经下方输入框回复 →
//      POST /human-tasks/{id}/responses（工程师一侧走真实登录 + 真实后台表单端点）；
//   6. 实时同步：面板打开时工程师答复自动出现（无需手动刷新）；面板收起时启动按钮出现
//      未读角标；重新打开后角标清空、回复可见。
//   7. 产品详情页上下文（window.PUMP_CHAT_CONTEXT）：打开即自动连接；询价默认当前型号并
//      在提交时绑定；提问默认针对当前型号（有据回答）。
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

function walk(root, visit) {
  for (const child of root.children || []) {
    visit(child);
    walk(child, visit);
  }
}

function findButtons(root) {
  const found = [];
  walk(root, (el) => { if (el.tagName === 'BUTTON') found.push(el); });
  return found;
}

function countTags(root, tag) {
  let count = 0;
  walk(root, (el) => { if (el.tagName === tag) count += 1; });
  return count;
}

function collectTags(root, tag) {
  const found = [];
  walk(root, (el) => { if (el.tagName === tag) found.push(el); });
  return found;
}

function makeStorage(initial = {}) {
  const map = new Map(Object.entries(initial));
  return {
    getItem: (key) => (map.has(String(key)) ? map.get(String(key)) : null),
    setItem: (key, value) => { map.set(String(key), String(value)); },
    removeItem: (key) => { map.delete(String(key)); },
  };
}

const IDS = ['chat-log', 'chat-notice', 'chat-launcher', 'chat-launcher-label', 'chat-panel', 'chat-close', 'chat-language',
  'chat-badge', 'connect', 'site-key', 'show-rfq', 'stop-contact', 'message-form', 'message', 'chat-title', 'chat-subtitle'];

function openChat({ language, languages, session = {}, chatContext = null }) {
  const journal = [];
  const elements = {};
  const timers = new Set();
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
  // 真实定时器（unref：场景跑完后不阻止 Node 退出；stop() 主动清理）。
  const wrappedSetInterval = (fn, ms) => {
    const timer = setInterval(fn, ms);
    if (typeof timer.unref === 'function') timer.unref();
    timers.add(timer);
    return timer;
  };
  const context = vm.createContext({
    document: {
      createElement: (tag) => makeEl(tag),
      getElementById: (id) => elements[id] || null,
      addEventListener() {},
      hidden: false,
    },
    location: { search: `?site_key=${encodeURIComponent(config.siteKey)}`, hash: '', pathname: '/reference-client' },
    localStorage: makeStorage(session),
    fetch: fetchImpl,
    navigator: { language, languages },
    crypto: globalThis.crypto,
    URLSearchParams,
    setInterval: wrappedSetInterval,
    clearInterval: (timer) => { clearInterval(timer); timers.delete(timer); },
    ...(chatContext ? { window: { PUMP_CHAT_CONTEXT: chatContext } } : {}),
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
    async click(text) {
      const button = findButtons(elements['chat-log']).filter((item) => item.textContent === text).pop();
      if (!button) throw new Error(`未找到按钮「${text}」；当前聊天记录: ${textOf(elements['chat-log']).slice(0, 400)}`);
      for (const handler of button.listeners.click || []) await handler();
      await sleep(250);
    },
    async clickClose() {
      for (const handler of elements['chat-close'].listeners.click || []) await handler();
    },
    async clickLauncher() {
      for (const handler of elements['chat-launcher'].listeners.click || []) await handler();
      await sleep(150);
    },
    async clickShowRfq() {
      for (const handler of elements['show-rfq'].listeners.click || []) await handler();
      await sleep(50);
    },
    inputValue(name) {
      const input = collectTags(elements['chat-log'], 'INPUT').find((item) => item.name === name);
      return input ? input.value : null;
    },
    async fillAndSubmitRfq(email) {
      const inputs = collectTags(elements['chat-log'], 'INPUT');
      const byName = (name) => inputs.find((item) => item.name === name);
      const emailInput = byName('contact_email');
      if (!emailInput) throw new Error('未找到询价邮箱输入框');
      emailInput.value = email;
      const confirmed = byName('contact_confirmed');
      if (confirmed) confirmed.checked = true;
      const form = collectTags(elements['chat-log'], 'FORM')[0];
      if (!form) throw new Error('未找到询价表单');
      for (const handler of form.listeners.submit || []) await handler({ preventDefault() {} });
      await sleep(60);
    },
    badge() {
      return { hidden: elements['chat-badge'].hidden, text: elements['chat-badge'].textContent };
    },
    panelHidden: () => elements['chat-panel'].hidden,
    textareaCount: () => countTags(elements['chat-log'], 'TEXTAREA'),
    snapshot() {
      return { logText: this.logText(), languageLabel: this.languageLabel(), launcherLabel: elements['chat-launcher-label'].textContent, messageValue: elements['message'].value };
    },
    stop() {
      for (const timer of timers) clearInterval(timer);
      timers.clear();
    },
  };
}

// —— 工程师一侧：真实登录 + 真实后台表单端点（无需借道买方接口） ——
async function engineerLogin() {
  const response = await fetch(`${config.baseUrl}/login`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/x-www-form-urlencoded', Origin: config.origin },
    body: new URLSearchParams({ email: config.engineerEmail, password: config.engineerPassword, next: '/admin' }).toString(),
    redirect: 'manual',
  });
  const rawCookies = typeof response.headers.getSetCookie === 'function'
    ? response.headers.getSetCookie()
    : [response.headers.get('set-cookie')];
  const session = (rawCookies || []).map((value) => String(value || '').split(';')[0]).find((value) => value.startsWith('cb_session='));
  if (!session) throw new Error(`工程师登录失败: HTTP ${response.status}`);
  return session;
}

async function engineerFormPost(path, fields, cookie) {
  const response = await fetch(`${config.baseUrl}${path}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/x-www-form-urlencoded', Origin: config.origin, Cookie: cookie },
    body: new URLSearchParams(fields).toString(),
    redirect: 'manual',
  });
  if (response.status >= 400) throw new Error(`工程师提交 ${path} 失败: HTTP ${response.status}`);
  return response.status;
}

async function buyerConversation(conversationId, token) {
  const response = await fetch(`${config.baseUrl}/api/v1/buyer/conversations/${conversationId}`, {
    headers: { 'X-Site-Key': config.siteKey, Authorization: `Bearer ${token}` },
  });
  if (!response.ok) throw new Error(`读取会话失败: HTTP ${response.status}`);
  return response.json();
}

const results = {};
try {
  // 场景 1：英文系统 → 英文欢迎语（无连接、无会话时的首屏）
  {
    const chat = openChat({ language: 'en-US', languages: ['en-US', 'en'] });
    results.greeting_en = chat.snapshot();
    chat.stop();
  }
  // 场景 2：中文系统 → 中文欢迎语；切到 EN 后欢迎语切换
  {
    const chat = openChat({ language: 'zh-CN', languages: ['zh-CN', 'zh'] });
    results.greeting_zh = chat.snapshot();
    await chat.clickLanguage();
    results.after_toggle = chat.snapshot();
    chat.stop();
  }
  // 场景 3：需求描述 → POST /messages（真实 HTTP），渲染需求草稿卡
  {
    const session = { [config.sessionKey]: JSON.stringify({ conversationId: config.requirementConversationId, token: config.requirementToken }) };
    const chat = openChat({ language: 'en-US', languages: ['en-US'], session });
    await chat.connect();
    await chat.submitMessage('flow 50 m3/h, head 30 m, clean water');
    results.requirement_route = { ...chat.snapshot(), requests: chat.journal.slice() };
    chat.stop();
  }
  // 场景 4：提问 → POST /questions（自动绑定推荐型号），渲染有据回答
  {
    const session = { [config.sessionKey]: JSON.stringify({ conversationId: config.questionConversationId, token: config.questionToken }) };
    const chat = openChat({ language: 'en-US', languages: ['en-US'], session });
    await chat.connect();
    await chat.submitMessage('What is the flow range?');
    results.question_route = { ...chat.snapshot(), requests: chat.journal.slice() };
    chat.stop();
  }
  // 场景 5：工程师追问 → 买家经统一输入框回复（真实链路：请求工程师 → 工程师追问 → 买家回复）
  const engineerSession = await engineerLogin();
  let taskOne = null;
  {
    const session = { [config.sessionKey]: JSON.stringify({ conversationId: config.taskConversationId, token: config.taskToken }) };
    const chat = openChat({ language: 'en-US', languages: ['en-US'], session });
    await chat.connect();
    // 无产品上下文提问 → 需人工回答卡 → 请求工程师（创建人工任务）
    await chat.submitMessage('Can this handle my application?');
    const afterQuestionText = chat.logText();
    await chat.click('Ask an engineer to review');
    const requestedStateText = chat.logText();
    const data = await buyerConversation(config.taskConversationId, config.taskToken);
    taskOne = (data.human_tasks || []).find((task) => task.status === 'pending');
    if (!taskOne) throw new Error(`未找到待处理人工任务: ${JSON.stringify(data.human_tasks)}`);
    // 工程师在后台追问
    await engineerFormPost(`/admin/tasks/${taskOne.id}/followup`, { question: 'Please confirm the flow and head (m³/h, m).' }, engineerSession);
    await sleep(4300); // 3 秒轮询应已把追问自动同步进聊天（无需手动刷新）
    const afterFollowupText = chat.logText();
    const textareaCount = chat.textareaCount();
    const beforeReply = chat.journal.length;
    // 买家经下方输入框回复 → 应命中人工任务回复端点
    await chat.submitMessage('flow 50 m3/h, head 30 m');
    results.waiting_task_reply = {
      taskId: taskOne.id,
      afterQuestionText, requestedStateText,
      afterFollowupText, textareaCount,
      afterReplyText: chat.logText(),
      requests: chat.journal.slice(beforeReply),
    };
    chat.stop();
  }
  // 场景 6：实时同步与未读角标（面板打开自动出现；收起后角标；重新打开清空）
  {
    const session = { [config.sessionKey]: JSON.stringify({ conversationId: config.taskConversationId, token: config.taskToken }) };
    const chat = openChat({ language: 'en-US', languages: ['en-US'], session });
    await chat.connect();
    // 工程师答复任务 1 → 面板开着，应自动出现（无需任何手动刷新）
    await engineerFormPost(`/admin/tasks/${taskOne.id}/reply`, { response: 'Confirmed: flow 50 m3/h at head 30 m is acceptable. — Engineer' }, engineerSession);
    const beforeWait = chat.journal.length;
    await sleep(4300);
    const autoReplyText = chat.logText();
    // 第二个问题（无产品上下文）→ 请求工程师 → 任务 2
    await chat.submitMessage('Will it work with my existing pipework?');
    await chat.click('Ask an engineer to review');
    const data = await buyerConversation(config.taskConversationId, config.taskToken);
    const taskTwo = (data.human_tasks || []).filter((task) => task.status === 'pending').pop();
    if (!taskTwo) throw new Error(`未找到第二个待处理人工任务: ${JSON.stringify(data.human_tasks)}`);
    // 收起聊天窗（页面只剩启动按钮）
    await chat.clickClose();
    // 工程师答复任务 2 → 收起状态下轮询应累计未读角标
    await engineerFormPost(`/admin/tasks/${taskTwo.id}/reply`, { response: 'Lead time for two units: 6-8 weeks. Price on written quote. — Engineer' }, engineerSession);
    await sleep(4300);
    const badgeWhileClosed = { ...chat.badge(), panelHidden: chat.panelHidden() };
    // 重新打开 → 角标清空、回复可见
    await chat.clickLauncher();
    results.auto_sync_and_badge = {
      autoReplyVisible: autoReplyText.includes('is acceptable'),
      engineerLabelVisible: autoReplyText.includes('Engineer reply'),
      autoSyncRequests: chat.journal.slice(beforeWait),
      badgeWhileClosed,
      badgeAfterOpen: chat.badge(),
      reopenedText: chat.logText(),
    };
    chat.stop();
  }
  // 场景 7：产品详情页上下文（window.PUMP_CHAT_CONTEXT）——打开即自动连接；
  // 询价表单默认当前型号且提交绑定；提问默认针对当前型号并得到有据回答
  {
    const chatContext = { siteKey: config.siteKey, productId: config.contextProductId, model: config.contextProductModel };
    const chat = openChat({ language: 'en-US', languages: ['en-US'], session: {}, chatContext });
    await chat.clickLauncher();
    await sleep(1200); // 自动连接（打开即连）
    const connected = chat.journal.some((item) => item.method === 'POST' && item.url.endsWith('/conversations'));
    await chat.clickShowRfq();
    const modelPrefill = chat.inputValue('requested_model');
    const rfqSlice = chat.journal.length;
    await chat.fillAndSubmitRfq('buyer@example.com');
    const rfqRequests = chat.journal.slice(rfqSlice);
    const questionSlice = chat.journal.length;
    await chat.submitMessage('What is the flow range?');
    const questionRequests = chat.journal.slice(questionSlice);
    results.product_page_context = { connected, modelPrefill, rfqRequests, questionRequests };
    chat.stop();
  }
  writeFileSync(resultPath, JSON.stringify({ ok: true, results }, null, 2));
} catch (error) {
  writeFileSync(resultPath, JSON.stringify({ ok: false, error: String((error && error.stack) || error) }, null, 2));
  process.exitCode = 1;
}
