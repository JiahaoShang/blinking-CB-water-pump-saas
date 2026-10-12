// 企业知识第二批 · 批次发布确认路径的“浏览器端”动态驱动（Node，无第三方依赖；vm + 迷你 DOM 桩）。
//
// 目的（阻断修复的判别性回归）：用真实的 static/knowledge-phase2.js 源码
// （由 Python 测试从磁盘读入写入 config.json，未经修改）在迷你 DOM 上驱动批次发布设置表单：
//   A 「发布」提交被拦截并打开站内确认对话框（标题=发布批次，消息=发布前会逐件校验…）；
//   B 取消 / Escape：关闭且不提交、不写 confirm_ai（保持默认 0）、焦点回到触发按钮；
//   C 「继续」：写入 confirm_ai=1，重发时保留触发按钮字段（action=publish 不丢失）。
//     背景：form.submit() 会丢掉提交按钮的字段（动作被降级为默认值，修复前的缺陷即是如此），
//     修复后优先用 requestSubmit(submitter) 原生重发；真实浏览器 requestSubmit 会再派发一次
//     submit（已确认 → 放行），脚本按同样顺序重放验证；无 requestSubmit 的老浏览器回退为
//     注入同名隐藏字段后 form.submit()（独立场景验证）。
//   D 「保存设置」按钮带 data-kb2-confirm-skip：不弹确认、按自身动作（action=save）直接提交；
//   E 无 submitter 的提交仍走确认（skip 只对声明按钮生效，不放宽到其他提交）。
// 更新行为：确认属性挂在表单上（form[data-kb2-confirm*]）；同表单其他动作按钮用 skip 声明。
// 输出结构化事实到 result.json，由 Python 测试（test_knowledge_phase2_batch_publish_confirm.py）
// 逐项断言；本驱动不代表真实浏览器视觉走查（浏览器 E2E 由本任务独立执行并归档）。
//
// 用法: node knowledge_phase2_confirm_browser.mjs <config.json> <result.json>
import { readFileSync, writeFileSync } from 'node:fs';
import vm from 'node:vm';

const [configPath, resultPath] = process.argv.slice(2);
if (!configPath || !resultPath) {
  console.error('usage: node knowledge_phase2_confirm_browser.mjs <config.json> <result.json>');
  process.exit(2);
}
const config = JSON.parse(readFileSync(configPath, 'utf8'));

function scriptBody(source) {
  const match = String(source).match(/<script>([\s\S]*)<\/script>\s*$/);
  return match ? match[1] : String(source);
}

/* ── 迷你 DOM ─────────────────────────────────────────────── */
let nodeSeq = 0;
class StubElement {
  constructor(tag, attrs = {}, label = '') {
    this.tagName = String(tag).toUpperCase();
    this.attrs = {};
    this.children = [];
    this.parent = null;
    this.listeners = {};
    this.ownerDocument = null;
    this.debugName = label || `${tag}#${++nodeSeq}`;
    for (const [key, value] of Object.entries(attrs)) this.attrs[key] = String(value);
    this._className = this.attrs.class || '';
    this.value = this.attrs.value || '';
    this.name = this.attrs.name || '';
    this.checked = false;
    this.files = null;
    this.textContent = '';
    this.form = null; // 由调用方设置（input.form / button.form）
    this._dataset = null;
  }
  append(...items) { for (const item of items) { item.parent = this; this.children.push(item); } }
  appendChild(item) { item.parent = this; this.children.push(item); return item; }
  get className() { return this._className; }
  set className(value) { this._className = String(value); }
  get classList() {
    const self = this;
    const list = () => self._className.split(/\s+/).filter(Boolean);
    return {
      contains: (name) => list().includes(name),
      add: (name) => { const l = list(); if (!l.includes(name)) self._className = [...l, name].join(' '); },
      remove: (name) => { self._className = list().filter((c) => c !== name).join(' '); },
      toggle: (name, force) => {
        const has = list().includes(name);
        const want = force === undefined ? !has : !!force;
        if (want) { this.classList.add(name); } else { this.classList.remove(name); }
        return want;
      },
    };
  }
  get dataset() { if (!this._dataset) { this._dataset = {}; } return this._dataset; }
  set innerHTML(source) {
    this.children = [];
    for (const child of parseHtml(String(source))) {
      if (typeof child === 'string') { this.children.push(child); continue; }
      child.parent = this;
      this.children.push(child);
    }
  }
  setAttribute(name, value) {
    this.attrs[name] = String(value);
    if (name === 'class') this._className = String(value);
    if (name === 'value') this.value = String(value);
  }
  getAttribute(name) { return Object.prototype.hasOwnProperty.call(this.attrs, name) ? this.attrs[name] : null; }
  hasAttribute(name) { return Object.prototype.hasOwnProperty.call(this.attrs, name); }
  removeAttribute(name) { delete this.attrs[name]; }
  get hidden() { return Object.prototype.hasOwnProperty.call(this.attrs, 'hidden'); }
  set hidden(value) { if (value) { this.attrs.hidden = ''; } else { delete this.attrs.hidden; } }
  focus() { if (this.ownerDocument) this.ownerDocument.activeElement = this; journal.push({ act: 'focus', on: this.debugName }); }
  closest(selector) {
    let node = this;
    while (node) { if (matches(node, selector)) return node; node = node.parent; }
    return null;
  }
  querySelector(selector) { return queryAll(this, selector)[0] || null; }
  querySelectorAll(selector) { return queryAll(this, selector); }
  requestSubmit(submitter) {
    const who = submitter && submitter.attrs
      ? `${submitter.attrs.name || ''}=${submitter.attrs.value || ''}`
      : 'requestSubmit';
    journal.push({ act: 'form-submit', form: this.debugName, submitter: who });
  }
  submit() {
    journal.push({ act: 'form-submit', form: this.debugName, submitter: 'submit' });
  }
  addEventListener(type, handler) { this.listeners[type] = handler; }
}

/* 迷你 innerHTML 解析：只覆盖组件实际使用的简单标记（含 class/aria 属性、成对标签、文本）。 */
function parseHtml(source) {
  const top = [];
  const stack = [top];
  const tokenRe = /<\/?[a-zA-Z][^>]*>|[^<]+/g;
  let token;
  while ((token = tokenRe.exec(source)) !== null) {
    const text = token[0];
    if (text.startsWith('</')) { if (stack.length > 1) stack.pop(); continue; }
    if (!text.startsWith('<')) { stack[stack.length - 1].push(text); continue; }
    const match = /^<([a-zA-Z][\w-]*)\s*([^>]*?)\/?>$/.exec(text);
    if (!match) { continue; }
    const element = new StubElement(match[1]);
    const attrRe = /([\w-]+)="([^"]*)"/g;
    let attr;
    while ((attr = attrRe.exec(match[2] || '')) !== null) element.setAttribute(attr[1], attr[2]);
    stack[stack.length - 1].push(element);
    if (!/\/>$/.test(text)) stack.push(element.children);
  }
  return top;
}

function parseToken(token) {
  const match = token.match(/^([a-zA-Z]*)((?:[#.][\w-]+|\[[^\]]+\])*)$/);
  if (!match) return null;
  const parts = [];
  const rest = match[2] || '';
  const re = /([#.])([\w-]+)|\[([^\]]+)\]/g;
  let m;
  while ((m = re.exec(rest))) {
    if (m[1] === '#') parts.push({ kind: 'id', value: m[2] });
    else if (m[1] === '.') parts.push({ kind: 'class', value: m[2] });
    else {
      const attr = m[3];
      const eq = attr.indexOf('=');
      if (eq === -1) parts.push({ kind: 'attr', name: attr.trim(), value: null });
      else {
        let value = attr.slice(eq + 1).trim();
        if ((value.startsWith('"') && value.endsWith('"')) || (value.startsWith("'") && value.endsWith("'"))) value = value.slice(1, -1);
        parts.push({ kind: 'attr', name: attr.slice(0, eq).trim(), value });
      }
    }
  }
  return { tag: match[1] ? match[1].toUpperCase() : null, parts };
}

function matchToken(node, token) {
  if (!token || !node || typeof node !== 'object') return false;
  if (token.tag && node.tagName !== token.tag) return false;
  for (const part of token.parts) {
    if (part.kind === 'id' && node.attrs.id !== part.value) return false;
    if (part.kind === 'class' && !node.classList.contains(part.value)) return false;
    if (part.kind === 'attr') {
      const value = node.attrs[part.name];
      if (value === undefined) return false;
      if (part.value !== null && String(value) !== part.value) return false;
    }
  }
  return true;
}

function matches(node, selector) {
  if (!node || typeof node !== 'object') return false;
  const chain = String(selector).trim().split(/\s+(?![^\[]*\])/);
  const last = parseToken(chain[chain.length - 1]);
  if (!last) return false;
  if (!matchToken(node, last)) return false;
  let current = node.parent;
  for (let i = chain.length - 2; i >= 0; i -= 1) {
    const token = parseToken(chain[i]);
    let found = false;
    while (current) {
      if (matchToken(current, token)) { found = true; current = current.parent; break; }
      current = current.parent;
    }
    if (!found) return false;
  }
  return true;
}

function queryAll(root, selector) {
  const out = [];
  const walk = (node) => {
    for (const child of node.children) {
      if (typeof child !== 'object') continue;
      if (matches(child, selector)) out.push(child);
      walk(child);
    }
  };
  walk(root);
  return out;
}

function dispatch(target, type, extra = {}) {
  const event = {
    type,
    target,
    defaultPrevented: false,
    preventDefault() { this.defaultPrevented = true; },
    ...extra,
  };
  const record = { act: 'event', type, on: target.debugName, prevented: false };
  journal.push(record);
  // 冒泡：目标 → 祖先 → document（与真实浏览器一致的监听顺序，简化版）。
  let node = target;
  while (node) {
    const handler = node.listeners[type];
    if (handler) handler(event);
    node = node.parent;
  }
  const doc = target.ownerDocument;
  if (doc && doc.listeners[type]) doc.listeners[type](event);
  record.prevented = event.defaultPrevented;
  return event;
}

const journal = [];

function makeEnv(rootElement) {
  const document = {
    activeElement: null,
    listeners: {},
    createElement(tag) {
      const element = new StubElement(tag);
      element.ownerDocument = document;
      if (String(tag).toLowerCase() === 'dialog') {
        element.open = false;
        element.showModal = function () { this.open = true; };
        element.close = function () { this.open = false; dispatch(this, 'close', {}); };
      }
      return element;
    },
    getElementById() { return null; },
    querySelector(selector) { return matches(rootElement, selector) ? rootElement : rootElement.querySelector(selector); },
    querySelectorAll(selector) { return rootElement.querySelectorAll(selector); },
    addEventListener(type, handler) { this.listeners[type] = handler; },
  };
  const storage = new Map();
  const sessionStorage = {
    getItem: (key) => (storage.has(String(key)) ? storage.get(String(key)) : null),
    setItem: (key, value) => { storage.set(String(key), String(value)); },
    removeItem: (key) => { storage.delete(String(key)); },
  };
  const location = { href: '', hash: '', assigned: null };
  const windowListeners = {};
  const window = {
    addEventListener(type, handler) { windowListeners[type] = handler; },
    setTimeout(fn) { if (typeof fn === 'function') { fn(); } return 0; },
    sessionStorage,
    location,
    matchMedia() { return { matches: false }; },
  };
  return { document, window, location, sessionStorage, windowListeners, root: rootElement };
}

function runScript(env, source) {
  const context = vm.createContext({ document: env.document, window: env.window, location: env.location, sessionStorage: env.sessionStorage, console });
  vm.runInContext(scriptBody(source), context, { timeout: 5000 });
}

function sub(label) { return (e) => { e.debugName = label; return e; }; }

/* ── 批次发布设置表单（与服务器渲染一致：确认属性在表单上；保存按钮带 skip）── */
function buildBatchForm() {
  const root = sub('kb2-root')(new StubElement('div', { class: 'kb2' }));
  const form = sub('batch-settings-form')(new StubElement('form', {
    method: 'post',
    action: '/admin/knowledge/batch/b1/settings',
    'data-kb2-confirm': '发布前会逐件校验正文核对情况；对外且授权 AI 需要确认。继续发布？',
    'data-kb2-confirm-title': '发布批次',
    'data-kb2-confirm-field': 'confirm_ai',
    'data-kb2-confirm-value': '1',
  }));
  const usageInternal = sub('usage-internal')(new StubElement('input', { type: 'radio', name: 'usage', value: 'internal' }));
  const usageExternal = sub('usage-external')(new StubElement('input', { type: 'radio', name: 'usage', value: 'external' }));
  usageExternal.checked = true;
  const aiAuth = sub('ai-authorized')(new StubElement('input', { type: 'checkbox', name: 'ai_authorized', value: '1' }));
  aiAuth.checked = true;
  const saveBtn = sub('save-settings')(new StubElement('button', { class: 'button secondary', name: 'action', value: 'save', 'data-kb2-confirm-skip': '' }));
  const publishBtn = sub('publish-batch')(new StubElement('button', { class: 'button', name: 'action', value: 'publish' }));
  const hiddenConfirm = sub('hidden-confirm-ai')(new StubElement('input', { type: 'hidden', name: 'confirm_ai', value: '0' }));
  const card = sub('card')(new StubElement('div', { class: 'card' }));
  form.append(usageInternal, usageExternal, aiAuth, saveBtn, publishBtn, hiddenConfirm);
  card.append(form);
  root.append(card);
  saveBtn.form = form;
  publishBtn.form = form;
  return { root, form, saveBtn, publishBtn, hiddenConfirm };
}

function attachDocument(env, elements) {
  for (const element of elements) element.ownerDocument = env.document;
}

function findDialog(root, id) {
  return root.children.filter((child) => typeof child === 'object' && child.tagName === 'DIALOG')
    .find((dialog) => dialog.id === id) || null;
}

const results = { scenarios: {} };

/* ── 主场景：确认对话框全流程 ─────────────────────────────── */
{
  const parts = buildBatchForm();
  const env = makeEnv(parts.root);
  attachDocument(env, [parts.root, parts.form, parts.hiddenConfirm, parts.saveBtn, parts.publishBtn,
    ...parts.form.children.filter((c) => typeof c === 'object')]);
  journal.length = 0;
  runScript(env, config.phase2Js);

  const confirmDlg = findDialog(parts.root, 'kb2-confirm-dialog');
  const guardDlg = findDialog(parts.root, 'kb2-nav-dialog');
  const facts = { confirmDialogBuilt: !!confirmDlg, guardDialogBuilt: !!guardDlg };
  const titleOf = () => String((confirmDlg.querySelector('.dlg-title') || {}).textContent || '');
  const messageOf = () => String((confirmDlg.querySelector('.dlg-message') || {}).textContent || '');
  const footButtons = () => (confirmDlg.querySelector('.dlg-foot') || { children: [] }).children;
  const recordedSubmit = () => journal.find((entry) => entry.act === 'form-submit');

  // D) 「保存设置」（skip）：不弹窗、不阻止、按自身动作直接提交。
  journal.length = 0;
  let event = dispatch(parts.form, 'submit', { submitter: parts.saveBtn });
  facts.saveNoDialog = !confirmDlg.open;
  facts.saveNotPrevented = event.defaultPrevented === false;
  facts.saveConfirmFieldUntouched = parts.hiddenConfirm.value === '0';

  // A) 「发布」：拦截并弹窗；文案指向发布批次。
  parts.publishBtn.focus();
  journal.length = 0;
  event = dispatch(parts.form, 'submit', { submitter: parts.publishBtn });
  facts.publishPrevented = event.defaultPrevented === true;
  facts.publishDialogOpened = confirmDlg.open;
  facts.publishDialogTitle = titleOf();
  facts.publishDialogMessage = messageOf();
  facts.confirmFieldBeforeConfirm = parts.hiddenConfirm.value;

  // B1) 取消：关闭、不提交、默认值保持 0、焦点回触发按钮。
  dispatch(footButtons()[1], 'click', {});
  facts.cancelClosed = !confirmDlg.open;
  facts.cancelNoSubmit = !recordedSubmit();
  facts.cancelConfirmField = parts.hiddenConfirm.value;
  facts.cancelFocusRestored = env.document.activeElement === parts.publishBtn;

  // B2) Escape（cancel + close）：关闭、不提交、默认值保持 0、焦点回触发按钮。
  journal.length = 0;
  dispatch(parts.form, 'submit', { submitter: parts.publishBtn });
  facts.escapeDialogReopened = confirmDlg.open;
  dispatch(confirmDlg, 'cancel', {});
  confirmDlg.close();
  facts.escapeClosed = !confirmDlg.open;
  facts.escapeNoSubmit = !recordedSubmit();
  facts.escapeConfirmField = parts.hiddenConfirm.value;
  facts.escapeFocusRestored = env.document.activeElement === parts.publishBtn;

  // E) 无 submitter 的提交（键盘/程序化）：仍走确认，不被 skip 放宽。
  //    （放在「继续」之前：确认标记在同页只消费一次属既有生命周期，真实浏览器确认后会导航。）
  journal.length = 0;
  event = dispatch(parts.form, 'submit', {});
  facts.nullSubmitterPrevented = event.defaultPrevented === true;
  facts.nullSubmitterDialogOpened = confirmDlg.open;
  dispatch(footButtons()[1], 'click', {});

  // C) 继续：写 confirm_ai=1；重发保留触发按钮（action=publish）。
  journal.length = 0;
  dispatch(parts.form, 'submit', { submitter: parts.publishBtn });
  dispatch(footButtons()[0], 'click', {});
  facts.acceptClosed = !confirmDlg.open;
  facts.acceptConfirmField = parts.hiddenConfirm.value;
  const acceptEntry = recordedSubmit();
  facts.acceptResubmitted = !!acceptEntry;
  facts.acceptSubmitter = acceptEntry ? acceptEntry.submitter : '';
  // 真实浏览器：requestSubmit 再派发一次 submit（已确认 → 放行，不再弹窗）。
  const replay = dispatch(parts.form, 'submit', { submitter: parts.publishBtn });
  facts.replayNotPrevented = replay.defaultPrevented === false;
  facts.replayNoSecondDialog = !confirmDlg.open;

  results.scenarios.confirmFlow = facts;
}

/* ── 回退场景：无 requestSubmit 时注入同名隐藏字段 ────────── */
{
  const parts = buildBatchForm();
  parts.form.requestSubmit = null; // 老浏览器：没有 requestSubmit
  const env = makeEnv(parts.root);
  attachDocument(env, [parts.root, parts.form, parts.hiddenConfirm, parts.saveBtn, parts.publishBtn,
    ...parts.form.children.filter((c) => typeof c === 'object')]);
  journal.length = 0;
  runScript(env, config.phase2Js);

  const confirmDlg = findDialog(parts.root, 'kb2-confirm-dialog');
  const facts = { confirmDialogBuilt: !!confirmDlg };
  const footButtons = () => (confirmDlg.querySelector('.dlg-foot') || { children: [] }).children;

  parts.publishBtn.focus();
  journal.length = 0;
  dispatch(parts.form, 'submit', { submitter: parts.publishBtn });
  facts.publishDialogOpened = confirmDlg.open;
  dispatch(footButtons()[0], 'click', {});
  const entry = journal.find((item) => item.act === 'form-submit');
  facts.fallbackSubmitted = !!entry;
  facts.fallbackVia = entry ? entry.submitter : '';
  facts.fallbackConfirmField = parts.hiddenConfirm.value;
  facts.fallbackInjectedAction = parts.form.children.some(
    (child) => typeof child === 'object' && child.tagName === 'INPUT'
      && child.name === 'action' && child.value === 'publish');

  results.scenarios.fallback = facts;
}

writeFileSync(resultPath, JSON.stringify(results, null, 2));
