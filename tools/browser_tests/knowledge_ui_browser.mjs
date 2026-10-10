// 企业知识管理界面 JS 的“浏览器端”动态驱动（Node，无第三方依赖；vm + 迷你 DOM 桩）。
//
// 目的（review-task23 清单 1/2/3 的动态 DOM 复核）：用真实的 KB_JS 与折叠导航 JS 源码
// （由 Python 测试从模块常量写入 config.json，未经修改）在迷你 DOM 上驱动以下行为：
//   A 搜索清空：真实按钮；输入时即时显隐；点击后清空关键词、提交原筛选表单（保留筛选）、
//     写入焦点恢复标记；提交失败兜底使用 data-href。
//   B 编辑器未保存保护：三按钮对话框（无 prompt）；取消/Escape 不跳转；放弃修改跳转；
//     保存草稿并离开走表单提交（带 next），标题为空时明确阻止且不提交、不丢内容；焦点恢复。
//   C 导入未保存保护：保存离开走表单提交（携带文件），不落 location.href；未选文件时阻止。
//   D 折叠导航：服务端子视图默认展开；服务端活动子视图优先于会话里存过的收起（清单13）；
//     无活动子视图时显式存过的收起/展开优先；箭头只切换不跳转；主按钮与子项点击记录展开。
//   E 保存流程（清单9）+ 站内确认对话框（替代 window.confirm）：日常保存不弹确认；对外+授权发布先弹
//     站内对话框（取消 / Escape 不提交、焦点回到触发按钮；确认写入显式确认字段并重发提交、清除未保存标记）。
//   F 内容形态切换（清单14）：文章↔FAQ 切换只改可见块与隐藏 form 值，已输入内容与另一形态的输入都保留。
//   G 产品授权对话框：仅「已发布产品开启授权」时弹窗并写入 confirmed=1；关闭授权 / 无需确认不弹窗。
// 输出结构化事实到 result.json，由 Python 测试（test_enterprise_knowledge_review23.DynamicDomTests）
// 逐项断言；本驱动不代表真实浏览器视觉走查（浏览器 QA 由 Codex 后续执行）。
//
// 用法: node knowledge_ui_browser.mjs <config.json> <result.json>
import { readFileSync, writeFileSync } from 'node:fs';
import vm from 'node:vm';

const [configPath, resultPath] = process.argv.slice(2);
if (!configPath || !resultPath) {
  console.error('usage: node knowledge_ui_browser.mjs <config.json> <result.json>');
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
    this.checked = false;
    this.files = null;
    this.textContent = '';
    this.form = null; // 由调用方设置（input.form）
  }
  append(...items) { for (const item of items) { item.parent = this; this.children.push(item); } }
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
  click() {
    journal.push({ act: 'click', on: this.debugName });
    if (this.form) {
      this.form._lastSubmitter = this;
      journal.push({ act: 'form-submit', form: this.form.debugName, submitter: `${this.attrs.name || ''}=${this.attrs.value || ''}`, next: (this.form._nextInput && this.form._nextInput.value) || '' });
      return;
    }
    dispatch(this, 'click', {});
  }
  requestSubmit(submitter) {
    const who = submitter && submitter.attrs
      ? `${submitter.attrs.name || ''}=${submitter.attrs.value || ''}`
      : 'requestSubmit';
    journal.push({ act: 'form-submit', form: this.debugName, submitter: who, next: (this._nextInput && this._nextInput.value) || '' });
  }
  submit() {
    journal.push({ act: 'form-submit', form: this.debugName, submitter: 'submit', next: (this._nextInput && this._nextInput.value) || '' });
  }
  dispatchEvent(event) { return dispatch(this, event.type, event.detail || {}); }
  addEventListener(type, handler) { this.listeners[type] = handler; }
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

function matches(node, selector) {
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

function matchToken(node, token) {
  if (!token) return false;
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

function queryAll(root, selector) {
  const out = [];
  const walk = (node) => {
    for (const child of node.children) {
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

function makeEnv(elementsById, rootElement) {
  const document = {
    activeElement: null,
    listeners: {},
    getElementById(id) { return elementsById[id] || null; },
    querySelector(selector) { return rootElement.querySelector(selector); },
    querySelectorAll(selector) { return rootElement.querySelectorAll(selector); },
    addEventListener(type, handler) { this.listeners[type] = handler; },
    dispatch(type, extra) {
      const handler = this.listeners[type];
      if (handler) handler({ type, preventDefault() {}, ...extra });
    },
  };
  const storage = new Map();
  const sessionStorage = {
    getItem: (key) => (storage.has(String(key)) ? storage.get(String(key)) : null),
    setItem: (key, value) => { storage.set(String(key), String(value)); journal.push({ act: 'storage-set', key: String(key), value: String(value) }); },
    removeItem: (key) => { storage.delete(String(key)); journal.push({ act: 'storage-remove', key: String(key) }); },
  };
  const location = { href: '', assigned: null };
  Object.defineProperty(location, 'href', {
    get() { return this._href || ''; },
    set(value) { this._href = String(value); this.assigned = String(value); journal.push({ act: 'navigate', to: String(value) }); },
  });
  const windowListeners = {};
  const window = {
    addEventListener(type, handler) { windowListeners[type] = handler; },
    confirm() { return true; },
    matchMedia() { return { matches: false }; },
  };
  return { document, window, location, sessionStorage, windowListeners, storage, root: rootElement };
}

function runScript(env, source) {
  const context = vm.createContext({ document: env.document, window: env.window, location: env.location, sessionStorage: env.sessionStorage, console });
  vm.runInContext(scriptBody(source), context, { timeout: 5000 });
}

function sub(label) { return (e) => { e.debugName = label; return e; }; }

const results = { scenarios: {} };

/* ── A 搜索清空 ─────────────────────────────────────────── */
{
  const input = sub('kb-q')(new StubElement('input', { id: 'kb-q', name: 'q', type: 'search', value: '保修' }));
  const clearBtn = sub('kb-clear')(new StubElement('button', { class: 'kb-clear', 'data-kb-clear': '', 'data-href': '/admin/knowledge?status=published' }));
  clearBtn.setAttribute('hidden', '');
  const form = sub('filterbar')(new StubElement('form', { class: 'kb-filterbar' }));
  const body = sub('body')(new StubElement('div'));
  form.append(input, clearBtn);
  body.append(form);
  input.form = form;
  form._nextInput = null;
  const env = makeEnv({ 'kb-q': input }, body);
  for (const el of [input, clearBtn, form]) el.ownerDocument = env.document;
  runScript(env, config.kbJs);
  const facts = { initialHidden: clearBtn.hidden };
  input.value = '保修条例';
  dispatch(input, 'input', {});
  facts.visibleAfterTyping = !clearBtn.hidden;
  input.value = '';
  dispatch(input, 'input', {});
  facts.hiddenAfterDeleting = clearBtn.hidden;
  input.value = '保修';
  dispatch(input, 'input', {});
  journal.length = 0;
  dispatch(clearBtn, 'click', {});
  facts.valueAfterClear = input.value;
  facts.submitted = journal.some((entry) => entry.act === 'form-submit' && entry.form === 'filterbar');
  facts.focusMarker = env.sessionStorage.getItem('kb-focus-search');
  facts.navigated = env.location.assigned;
  results.scenarios.searchClear = facts;
}

/* ── B 编辑器未保存保护 ─────────────────────────────────── */
{
  const title = sub('title')(new StubElement('input', { name: 'title', value: '' }));
  const next = sub('next')(new StubElement('input', { name: 'next', value: '' }));
  const save = sub('save-draft')(new StubElement('button', { name: 'action', value: 'save_draft', type: 'submit' }));
  const form = sub('editor-form')(new StubElement('form', { 'data-unsaved': '' }));
  const dialog = sub('leave-dialog')(new StubElement('dialog', { id: 'kb-leave-dlg' }));
  const error = sub('leave-error')(new StubElement('p', { id: 'kb-leave-error' }));
  const cancelBtn = sub('cancel')(new StubElement('button', { 'data-kb-leave': 'cancel' }));
  const discardBtn = sub('discard')(new StubElement('button', { 'data-kb-leave': 'discard' }));
  const saveLeaveBtn = sub('save-leave')(new StubElement('button', { 'data-kb-leave': 'save' }));
  dialog.append(error, cancelBtn, discardBtn, saveLeaveBtn);
  dialog.open = false;
  dialog.showModal = () => { dialog.open = true; };
  dialog.close = () => { dialog.open = false; dispatch(dialog, 'close', {}); };
  const link = sub('nav-link')(new StubElement('a', { href: '/admin/knowledge' }));
  const body = sub('body')(new StubElement('div'));
  form.append(title, next, save);
  body.append(form, dialog, link);
  title.form = form; next.form = form; save.form = form;
  form._nextInput = next;
  const env = makeEnv({ 'kb-leave-dlg': dialog, 'kb-leave-error': error }, body);
  for (const el of [form, dialog, error, link, title, next, save, cancelBtn, discardBtn, saveLeaveBtn]) el.ownerDocument = env.document;
  runScript(env, config.kbJs);
  const facts = { listenerTypes: Object.keys(dialog.listeners).sort() };
  dispatch(form, 'input', {});
  link.focus();
  journal.length = 0;
  dispatch(link, 'click', {});
  facts.openedOnLinkClick = dialog.open;
  facts.navPrevented = journal.some((entry) => entry.act === 'event' && entry.type === 'click' && entry.prevented === true);
  // 继续编辑：关闭对话框、不跳转、无提示；焦点回到触发元素。
  dispatch(cancelBtn, 'click', {});
  facts.cancelClosed = !dialog.open;
  facts.cancelNavigated = env.location.assigned;
  facts.cancelFocusRestored = env.document.activeElement === link;
  // 放弃修改：跳转目标 = 链接 href。
  dispatch(link, 'click', {});
  dispatch(discardBtn, 'click', {});
  facts.discardNavigated = env.location.assigned;
  // 保存草稿并离开：标题为空 → 明确阻止、不提交、对话框保持打开、内容保留。
  dispatch(form, 'input', {});
  journal.length = 0;
  env.location.assigned = null;
  dispatch(link, 'click', {});
  dispatch(saveLeaveBtn, 'click', {});
  facts.emptyTitleBlocked = !dialog.open ? false : (error.hidden === false && String(error.textContent).length > 0);
  facts.emptyTitleSubmitted = journal.some((entry) => entry.act === 'form-submit');
  facts.emptyTitleNav = env.location.assigned;
  // 填标题后保存：表单提交（save_draft），next = 目标链接；不落 location.href。
  title.value = '保修政策';
  dispatch(form, 'input', {});
  journal.length = 0;
  env.location.assigned = null;
  dispatch(link, 'click', {});
  facts.openedAgain = dialog.open;
  dispatch(saveLeaveBtn, 'click', {});
  const submitEntry = journal.find((entry) => entry.act === 'form-submit');
  facts.savedSubmitted = !!submitEntry;
  facts.savedSubmitter = submitEntry ? submitEntry.submitter : '';
  facts.savedNext = submitEntry ? submitEntry.next : '';
  facts.savedNavigated = env.location.assigned;
  results.scenarios.editorLeave = facts;
}

/* ── C 导入未保存保护 ───────────────────────────────────── */
{
  const file = sub('file-input')(new StubElement('input', { type: 'file', name: 'file' }));
  file.files = [];
  const next = sub('next')(new StubElement('input', { name: 'next', value: '' }));
  const form = sub('import-form')(new StubElement('form', { 'data-unsaved': '', enctype: 'multipart/form-data' }));
  const dialog = sub('leave-dialog')(new StubElement('dialog', { id: 'kb-leave-dlg' }));
  const error = sub('leave-error')(new StubElement('p', { id: 'kb-leave-error' }));
  dialog.append(error);
  dialog.open = false;
  dialog.showModal = () => { dialog.open = true; };
  dialog.close = () => { dialog.open = false; dispatch(dialog, 'close', {}); };
  const link = sub('nav-link')(new StubElement('a', { href: '/admin/knowledge?view=files' }));
  const body = sub('body')(new StubElement('div'));
  const cancel = sub('cancel')(new StubElement('button', { 'data-kb-leave': 'cancel' }));
  const saveBtn = sub('save')(new StubElement('button', { 'data-kb-leave': 'save' }));
  dialog.append(cancel, saveBtn);
  form.append(file, next);
  body.append(form, dialog, link);
  file.form = form; next.form = form;
  form._nextInput = next;
  const env = makeEnv({ 'kb-leave-dlg': dialog, 'kb-leave-error': error }, body);
  for (const el of [form, dialog, error, link, file, next, cancel, saveBtn]) el.ownerDocument = env.document;
  runScript(env, config.kbJs);
  const facts = {};
  dispatch(form, 'input', {});
  dispatch(file, 'change', {});
  journal.length = 0;
  dispatch(link, 'click', {});
  facts.openedOnLinkClick = dialog.open;
  dispatch(saveBtn, 'click', {});
  facts.noFileBlocked = error.hidden === false && /选择要上传的文件/.test(String(error.textContent));
  facts.noFileSubmitted = journal.some((entry) => entry.act === 'form-submit');
  // 有文件后：提交表单（携带文件），不落 location.href。
  file.files = [{ name: '服务承诺.txt' }];
  dispatch(file, 'change', {});
  journal.length = 0;
  env.location.assigned = null;
  dispatch(link, 'click', {});
  dispatch(saveBtn, 'click', {});
  const submitEntry = journal.find((entry) => entry.act === 'form-submit');
  facts.savedSubmitted = !!submitEntry;
  facts.savedVia = submitEntry ? submitEntry.submitter : '';
  facts.savedNext = submitEntry ? submitEntry.next : '';
  facts.fileKept = file.files.length === 1;
  facts.savedNavigated = env.location.assigned;
  results.scenarios.importLeave = facts;
}

/* ── D 折叠导航 ─────────────────────────────────────────── */
function navScenario(stored, submitter) {
  const toggle = sub('toggle')(new StubElement('button', { class: 'side-toggle', 'data-nav-toggle': '', 'aria-expanded': 'false' }));
  const subs = sub('subs')(new StubElement('div', { id: 'nav-knowledge-subs', class: 'nav-subs', 'data-sub-active': submitter }));
  const mainWrap = sub('main-row')(new StubElement('div', { class: 'nav-main-row' }));
  const mainLink = sub('main-link')(new StubElement('a', { class: 'side-link', 'data-kb-nav-main': '', href: '/admin/knowledge' }));
  const subLink = sub('sub-link')(new StubElement('a', { class: 'side-link sub', href: '/admin/knowledge?view=article_faq' }));
  subs.append(subLink);
  mainWrap.append(mainLink);
  const body = sub('body')(new StubElement('div'));
  body.append(mainWrap, subs, toggle);
  const env = makeEnv({ 'nav-knowledge-subs': subs }, body);
  for (const el of [toggle, subs, mainWrap, mainLink, subLink]) el.ownerDocument = env.document;
  if (stored !== null) env.sessionStorage.setItem('kb-nav-expanded', stored);
  journal.length = 0;
  runScript(env, config.navJs);
  return { env, toggle, subs, mainLink, subLink };
}

{
  // 1) 直接子视图（服务端默认展开）且无存储：展开。
  const run1 = navScenario(null, '1');
  const facts = { serverSubActiveOpened: run1.subs.classList.contains('open') };
  facts.ariaExpanded = run1.toggle.getAttribute('aria-expanded');
  // 2) 会话里存过「收起」，但服务端有活动子视图：服务端获胜 → 展开（清单13）。
  const run2 = navScenario('0', '1');
  facts.serverWinsOverStoredClosed = run2.subs.classList.contains('open');
  // 2b) 会话里存过「收起」且无活动子视图：保持收起。
  const run2b = navScenario('0', '0');
  facts.explicitCollapsed = !run2b.subs.classList.contains('open');
  // 3) 显式展开优先于服务端默认。
  const run3 = navScenario('1', '0');
  facts.explicitOpened = run3.subs.classList.contains('open');
  // 4) 箭头独立切换：展开并记录；不写 location。
  const run4 = navScenario(null, '0');
  facts.toggleClosedByDefault = !run4.subs.classList.contains('open');
  dispatch(run4.toggle, 'click', {});
  facts.toggleOpened = run4.subs.classList.contains('open');
  facts.toggleStored = run4.env.sessionStorage.getItem('kb-nav-expanded');
  facts.toggleNavigated = run4.env.location.assigned;
  dispatch(run4.toggle, 'click', {});
  facts.toggleClosedAgain = !run4.subs.classList.contains('open');
  facts.toggleStoredAfterClose = run4.env.sessionStorage.getItem('kb-nav-expanded');
  // 5) 主按钮点击：记录展开并保持（页面跳转由链接默认行为完成）。
  const run5 = navScenario('0', '0');
  dispatch(run5.mainLink, 'click', {});
  facts.mainLinkOpened = run5.subs.classList.contains('open');
  facts.mainLinkStored = run5.env.sessionStorage.getItem('kb-nav-expanded');
  facts.mainLinkNavigated = run5.env.location.assigned;
  // 6) 子项点击：记录展开。
  const run6 = navScenario('0', '0');
  dispatch(run6.subLink, 'click', {});
  facts.subLinkStored = run6.env.sessionStorage.getItem('kb-nav-expanded');
  results.scenarios.navFold = facts;
}

/* ── E 保存流程（清单9）+ 站内确认对话框 ─────────────────── */
function unloadAttempt(env) {
  const event = { defaultPrevented: false, returnValue: '', preventDefault() { this.defaultPrevented = true; } };
  const handler = env.windowListeners['beforeunload'];
  if (handler) handler(event);
  return event;
}
function makeConfirmDialog(body) {
  const dlg = sub('confirm-dialog')(new StubElement('dialog', { id: 'kb-confirm-dlg', class: 'dlg kb-confirm-dlg' }));
  const title = sub('confirm-title')(new StubElement('h3', { id: 'kb-confirm-title' }));
  const message = sub('confirm-message')(new StubElement('p', { id: 'kb-confirm-message' }));
  const icon = sub('confirm-icon')(new StubElement('span', { id: 'kb-confirm-icon', class: 'dlg-icon warn' }));
  const ok = sub('confirm-ok')(new StubElement('button', { id: 'kb-confirm-ok', 'data-kb-confirm-act': 'ok' }));
  const cancel = sub('confirm-cancel')(new StubElement('button', { 'data-kb-confirm-act': 'cancel' }));
  dlg.append(icon, title, message, ok, cancel);
  dlg.open = false;
  dlg.showModal = () => { dlg.open = true; };
  dlg.close = () => { dlg.open = false; dispatch(dlg, 'close', {}); };
  body.append(dlg);
  return { dlg, title, message, icon, ok, cancel };
}
{
  const title = sub('title')(new StubElement('input', { name: 'title', value: '' }));
  const next = sub('next')(new StubElement('input', { name: 'next', value: '' }));
  const confirmAi = sub('confirm-ai')(new StubElement('input', { name: 'confirm_ai', value: '' }));
  const usage = sub('usage')(new StubElement('select', { name: 'usage', value: 'external' }));
  const auth = sub('auth')(new StubElement('input', { name: 'ai_authorized', type: 'checkbox' }));
  auth.checked = true;
  const save = sub('save-draft')(new StubElement('button', { name: 'action', value: 'save_draft', type: 'submit' }));
  const publish = sub('publish')(new StubElement('button', { name: 'action', value: 'publish', type: 'submit' }));
  const form = sub('save-form')(new StubElement('form', { 'data-unsaved': '' }));
  const body = sub('body')(new StubElement('div'));
  const confirmUi = makeConfirmDialog(body);
  form.append(title, next, confirmAi, usage, auth, save, publish);
  body.append(form);
  title.form = form; next.form = form; save.form = form; publish.form = form;
  form._nextInput = next;
  const env = makeEnv({
    'kb-confirm-dlg': confirmUi.dlg, 'kb-confirm-title': confirmUi.title, 'kb-confirm-message': confirmUi.message,
    'kb-confirm-icon': confirmUi.icon, 'kb-confirm-ok': confirmUi.ok,
  }, body);
  for (const el of [form, body, title, next, confirmAi, usage, auth, save, publish,
    confirmUi.dlg, confirmUi.title, confirmUi.message, confirmUi.icon, confirmUi.ok, confirmUi.cancel]) el.ownerDocument = env.document;
  runScript(env, config.kbJs);
  const facts = {};
  // 基线：输入后未保存标记生效，离开被拦截。
  dispatch(form, 'input', {});
  facts.dirtyBlocksUnload = unloadAttempt(env).defaultPrevented;
  // 合法「保存草稿」提交：不弹确认、未被阻止、标记清除 → 这次保存不会被 beforeunload 拦截。
  journal.length = 0;
  const saveEvent = dispatch(form, 'submit', { submitter: save });
  facts.saveSubmitAllowed = saveEvent.defaultPrevented === false;
  facts.saveDraftNoDialog = !confirmUi.dlg.open;
  facts.saveClearsDirty = !unloadAttempt(env).defaultPrevented;
  // 对外+授权发布：第一次提交被拦截并打开站内对话框（不再使用 window.confirm）。
  dispatch(form, 'input', {});
  publish.focus();
  journal.length = 0;
  const declined = dispatch(form, 'submit', { submitter: publish });
  facts.publishDialogOpened = confirmUi.dlg.open;
  facts.publishDialogPrevented = declined.defaultPrevented === true;
  facts.publishDialogTitle = String(confirmUi.title.textContent || '');
  facts.publishDialogMessage = String(confirmUi.message.textContent || '');
  facts.publishDialogLabel = String(confirmUi.ok.textContent || '');
  // 取消：关闭、不提交、保留未保存保护、confirm_ai 保持为空、焦点回触发按钮。
  dispatch(confirmUi.cancel, 'click', {});
  facts.declineDialogClosed = !confirmUi.dlg.open;
  facts.declineResubmitted = journal.some((entry) => entry.act === 'form-submit');
  facts.publishDeclinedPrevented = facts.publishDialogPrevented && !facts.declineResubmitted;
  facts.publishDeclinedKeepsDirty = unloadAttempt(env).defaultPrevented;
  facts.publishDeclinedConfirmField = confirmAi.value;
  facts.declineFocusRestored = env.document.activeElement === publish;
  // Escape = 取消：dialog 触发 cancel（原生随后关闭），不提交、焦点恢复。
  journal.length = 0;
  dispatch(form, 'submit', { submitter: publish });
  facts.escapeDialogOpened = confirmUi.dlg.open;
  dispatch(confirmUi.dlg, 'cancel', {});
  confirmUi.dlg.close();
  facts.escapeClosed = !confirmUi.dlg.open;
  facts.escapeResubmitted = journal.some((entry) => entry.act === 'form-submit');
  facts.escapeFocusRestored = env.document.activeElement === publish;
  // 确认：写入 confirm_ai=1、重发提交（保留 publish 按钮）、清除未保存标记。
  journal.length = 0;
  dispatch(form, 'submit', { submitter: publish });
  dispatch(confirmUi.ok, 'click', {});
  facts.publishDialogAcceptedClosed = !confirmUi.dlg.open;
  facts.publishAcceptedConfirmField = confirmAi.value;
  const resubmit = journal.find((entry) => entry.act === 'form-submit');
  facts.publishAcceptedAllowed = !!resubmit;
  facts.publishAcceptedSubmitter = resubmit ? resubmit.submitter : '';
  facts.publishAcceptedClearsDirty = !unloadAttempt(env).defaultPrevented;
  facts.publishAcceptedFocusRestored = env.document.activeElement === publish;
  // 真实浏览器：requestSubmit 会再派发一次 submit（已确认 → 直接放行）；stub 里补发同样的顺序。
  const replayed = dispatch(form, 'submit', { submitter: publish });
  facts.publishAcceptedSecondDialog = confirmUi.dlg.open;
  facts.publishResubmitNotPrevented = replayed.defaultPrevented === false;
  // 新的一次发布尝试：重新弹窗（确认标记已被消费，不会锁死）。
  dispatch(form, 'submit', { submitter: publish });
  facts.publishDialogReopensForNewAttempt = confirmUi.dlg.open;
  dispatch(confirmUi.cancel, 'click', {});
  results.scenarios.saveFlow = facts;
}

/* ── F 内容形态切换（清单14）────────────────────────────── */
{
  const fieldInput = sub('form-field')(new StubElement('input', { type: 'hidden', name: 'form', value: 'article' }));
  const titleLabel = sub('title-label')(new StubElement('label', { id: 'kb-title-label' }));
  titleLabel.textContent = '文章标题';
  const formTitle = sub('form-title')(new StubElement('h1', { id: 'kb-form-title' }));
  formTitle.textContent = '添加文章';
  const bodyArea = sub('body-textarea')(new StubElement('textarea', { name: 'body' }));
  const articleBlock = sub('block-article')(new StubElement('div', { 'data-kb-form-block': 'article' }));
  articleBlock.append(bodyArea);
  const faqBlock = sub('block-faq')(new StubElement('div', { 'data-kb-form-block': 'faq' }));
  faqBlock.setAttribute('hidden', '');
  const faqQuestion = sub('faq-q0')(new StubElement('input', { name: 'faq_q_0' }));
  faqBlock.append(faqQuestion);
  const switchArticle = sub('switch-article')(new StubElement('button', { type: 'button', 'data-kb-form-switch': 'article', 'data-kb-title-label': '文章标题', 'data-kb-form-title': '添加文章' }));
  const switchFaq = sub('switch-faq')(new StubElement('button', { type: 'button', 'data-kb-form-switch': 'faq', 'data-kb-title-label': 'FAQ 标题', 'data-kb-form-title': '添加 FAQ' }));
  const form = sub('switch-form')(new StubElement('form', { 'data-unsaved': '', 'data-form-switch': '' }));
  form.append(fieldInput, titleLabel, formTitle, articleBlock, faqBlock, switchArticle, switchFaq);
  const body = sub('body')(new StubElement('div'));
  body.append(form);
  const env = makeEnv({ 'kb-title-label': titleLabel, 'kb-form-title': formTitle }, body);
  for (const el of [form, body, fieldInput, titleLabel, formTitle, articleBlock, bodyArea, faqBlock, faqQuestion, switchArticle, switchFaq]) el.ownerDocument = env.document;
  runScript(env, config.kbJs);
  if (config.switchJs) runScript(env, config.switchJs);
  const facts = {};
  facts.initialForm = fieldInput.value;
  facts.initialArticleVisible = !articleBlock.hidden;
  facts.initialFaqHidden = faqBlock.hidden;
  bodyArea.value = '正文内容';
  dispatch(form, 'input', {});
  dispatch(switchFaq, 'click', {});
  facts.formAfterSwitch = fieldInput.value;
  facts.articleHiddenAfterSwitch = articleBlock.hidden;
  facts.faqVisibleAfterSwitch = !faqBlock.hidden;
  facts.bodyPreserved = bodyArea.value;
  facts.titleLabelAfterSwitch = titleLabel.textContent;
  facts.formTitleAfterSwitch = formTitle.textContent;
  facts.faqPressed = switchFaq.getAttribute('aria-pressed');
  facts.articlePressed = switchArticle.getAttribute('aria-pressed');
  dispatch(switchArticle, 'click', {});
  facts.formAfterSwitchBack = fieldInput.value;
  facts.articleVisibleAgain = !articleBlock.hidden;
  facts.faqHiddenAgain = faqBlock.hidden;
  facts.bodyPreservedAfterBack = bodyArea.value;
  facts.unsavedGuardInstalled = typeof env.window.__kbUnsavedGuard === 'object';
  results.scenarios.editorShapeSwitch = facts;
}

/* ── G 产品授权确认对话框 ───────────────────────────────── */
{
  const checkbox = sub('authorized')(new StubElement('input', { name: 'authorized', type: 'checkbox', value: '1' }));
  checkbox.checked = true;
  const confirmed = sub('confirmed')(new StubElement('input', { name: 'confirmed', value: '' }));
  const settings = sub('save-auth')(new StubElement('button', { type: 'submit' }));
  const form = sub('product-auth-form')(new StubElement('form', {
    'data-kb-product-auth': '', 'data-needs-confirm': '1',
    'data-kb-confirm-title': '允许 AI 客服使用该产品？',
    'data-kb-confirm-message': '开启后，该产品「对外且已核准」的字段会进入 AI 客服回答候选用途。',
    'data-kb-confirm-label': '开启授权',
  }));
  const body = sub('body')(new StubElement('div'));
  const confirmUi = makeConfirmDialog(body);
  form.append(checkbox, confirmed, settings);
  body.append(form);
  checkbox.form = form; confirmed.form = form; settings.form = form;
  const env = makeEnv({
    'kb-confirm-dlg': confirmUi.dlg, 'kb-confirm-title': confirmUi.title, 'kb-confirm-message': confirmUi.message,
    'kb-confirm-icon': confirmUi.icon, 'kb-confirm-ok': confirmUi.ok,
  }, body);
  for (const el of [form, body, checkbox, confirmed, settings,
    confirmUi.dlg, confirmUi.title, confirmUi.message, confirmUi.icon, confirmUi.ok, confirmUi.cancel]) el.ownerDocument = env.document;
  runScript(env, config.kbJs);
  const facts = {};
  // 已授权产品（needs-confirm=0）与关闭授权：都不需要确认，直接提交。
  form.setAttribute('data-needs-confirm', '0');
  journal.length = 0;
  let event = dispatch(form, 'submit', { submitter: settings });
  facts.noDialogWhenNotNeeded = !confirmUi.dlg.open && event.defaultPrevented === false;
  checkbox.checked = false;
  event = dispatch(form, 'submit', { submitter: settings });
  facts.noDialogWhenTurningOff = !confirmUi.dlg.open && event.defaultPrevented === false;
  checkbox.checked = true;
  // 已发布产品开启授权：站内对话框拦截。
  form.setAttribute('data-needs-confirm', '1');
  settings.focus();
  journal.length = 0;
  const opened = dispatch(form, 'submit', { submitter: settings });
  facts.dialogOpened = confirmUi.dlg.open;
  facts.dialogPrevented = opened.defaultPrevented === true;
  facts.dialogTitle = String(confirmUi.title.textContent || '');
  facts.dialogLabel = String(confirmUi.ok.textContent || '');
  // 取消：不提交、confirmed 保持为空、焦点恢复。
  dispatch(confirmUi.cancel, 'click', {});
  facts.declineClosed = !confirmUi.dlg.open;
  facts.declineClearsConfirmedField = confirmed.value === '';
  facts.declineResubmitted = journal.some((entry) => entry.act === 'form-submit');
  facts.declineFocusRestored = env.document.activeElement === settings;
  // 确认：写入 confirmed=1 并重发提交。
  journal.length = 0;
  dispatch(form, 'submit', { submitter: settings });
  dispatch(confirmUi.ok, 'click', {});
  facts.confirmFieldAfterAccept = confirmed.value;
  facts.resubmittedAfterAccept = journal.some((entry) => entry.act === 'form-submit');
  results.scenarios.productConfirm = facts;
}

writeFileSync(resultPath, JSON.stringify(results, null, 2));
console.log('knowledge_ui_browser harness ok');
