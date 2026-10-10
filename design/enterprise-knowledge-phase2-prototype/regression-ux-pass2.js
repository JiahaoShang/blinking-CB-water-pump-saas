'use strict';
/*
 * 第二轮 UX 改版 DOM 回归（DOM 沙盒，无依赖；运行：node regression-ux-pass2.js）
 * 只覆盖本轮最容易回归、且直接来自改版验收目标的事（均为状态 / 渲染字符串断言，不测布局）：
 *  1) 详情「返回」回到实际进入的列表视图（文件资料 / 全部知识），搜索与筛选保留，只读视角同样可用；
 *  2) 第 1 步默认「通用文档」，不选模板即可继续；未选择文件不能继续；
 *  3) 用途 / AI 授权 / 到期时间只在第 3 步出现（第 1、2 步不再重复展示这些控件）；
 *  4) 「演示场景」与「原文对照」默认折叠；点段落「来源」自动展开并定位；
 *  5) 未保存保护仍生效（有改动时点导航先弹确认）。
 * 这里是原型函数级检查，不能替代真实浏览器走查（布局、键盘、对话框焦点由人工/Codex 执行）。
 */
const fs = require('fs');
const path = require('path');
const vm = require('vm');

function makeEl(tag) {
  return {
    tagName: (tag || 'div').toUpperCase(),
    innerHTML: '', textContent: '', value: '', hidden: false, open: false,
    attrs: {},
    classList: { add() {}, remove() {}, toggle() {}, contains() { return false; } },
    addEventListener() {},
    setAttribute(k, v) { this.attrs[k] = String(v); },
    removeAttribute(k) { delete this.attrs[k]; },
    getAttribute(k) { return (k in this.attrs) ? this.attrs[k] : null; },
    focus() {}, appendChild() {}, remove() {}, insertAdjacentHTML() {},
    querySelector() { return null; }, querySelectorAll() { return []; },
    closest() { return null; },
    scrollIntoView() {},
    showModal() { this.open = true; },
    close() { this.open = false; }
  };
}

function loadApp() {
  const reg = new Map();
  const handlers = {};
  const documentStub = {
    body: makeEl('body'),
    activeElement: null,
    createElement: (t) => makeEl(t),
    querySelector(sel) { if (!reg.has(sel)) reg.set(sel, makeEl('div')); return reg.get(sel); },
    querySelectorAll() { return []; },
    addEventListener(type, fn) { (handlers[type] = handlers[type] || []).push(fn); }
  };
  const sandbox = {
    document: documentStub,
    window: { scrollTo() {} },
    console,
    requestAnimationFrame: () => 0,
    setTimeout: () => 0
  };
  vm.createContext(sandbox);
  vm.runInContext(fs.readFileSync(path.join(__dirname, 'app.js'), 'utf8'), sandbox, { filename: 'app.js' });
  return { sandbox, reg, handlers };
}

/* app.js 里的 const 绑定（如 state）不是沙箱对象属性，用同上下文求值读取 */
function inCtx(sandbox, expr) { return vm.runInContext(expr, sandbox); }

function clickAct(handlers, act, attrs) {
  const map = Object.assign({ 'data-act': act }, attrs || {});
  const el = {
    tagName: 'BUTTON',
    getAttribute: (k) => ((k in map) ? String(map[k]) : null),
    closest: (sel) => (sel === '[data-act]' ? el : null)
  };
  const ev = { target: { closest: (sel) => (sel === '[data-act]' ? el : null) } };
  (handlers.click || []).forEach((fn) => fn(ev));
}

let failures = 0;
function ok(cond, label) {
  if (cond) console.log('PASS - ' + label);
  else { failures++; console.log('FAIL - ' + label); }
}
function eq(actual, expected, label) {
  ok(actual === expected, label + '（期望 ' + JSON.stringify(expected) + '，实际 ' + JSON.stringify(actual) + '）');
}
function count(hay, needle) { return hay.split(needle).length - 1; }

/* ── 场景 1：从「文件资料」进入详情，返回文件资料并保留筛选 ── */
{
  const { sandbox, reg, handlers } = loadApp();
  clickAct(handlers, 'nav', { 'data-view': 'file' });
  eq(inCtx(sandbox, 'state.route.name'), 'list', '导航到文件资料列表');
  eq(inCtx(sandbox, 'state.route.view'), 'file', '当前列表为文件资料');
  inCtx(sandbox, "state.filters.q = '客户'; state.filters.status = 'published';");
  clickAct(handlers, 'row-open', { 'data-id': 'k_policy' });
  eq(inCtx(sandbox, 'state.route.name'), 'detail', '打开知识详情');
  const app = reg.get('#app').innerHTML;
  ok(app.indexOf('返回文件资料') >= 0, '详情顶部显示「返回文件资料」');
  ok(app.indexOf('data-act="d-back"') >= 0, '返回是普通导航按钮（不依赖浏览器历史）');
  clickAct(handlers, 'd-back');
  eq(inCtx(sandbox, 'state.route.view'), 'file', '点返回回到实际进入的列表（文件资料）');
  eq(inCtx(sandbox, 'state.filters.q'), '客户', '返回后搜索关键词保留');
  eq(inCtx(sandbox, 'state.filters.status'), 'published', '返回后状态筛选保留');
  const list = reg.get('#app').innerHTML;
  ok(list.indexOf('id="search-q"') >= 0 && list.indexOf('value="客户"') >= 0, '返回后的列表保留搜索框与关键词');
  ok(list.indexOf('上传文件') >= 0, '返回后仍在文件资料视图（有「上传文件」入口）');
}

/* ── 场景 2：从「全部知识」进入，返回全部知识 ── */
{
  const { sandbox, reg, handlers } = loadApp();
  clickAct(handlers, 'nav', { 'data-view': 'all' });
  clickAct(handlers, 'row-open', { 'data-id': 'k_install' });
  ok(reg.get('#app').innerHTML.indexOf('返回全部知识') >= 0, '从全部知识进入时显示「返回全部知识」');
  clickAct(handlers, 'd-back');
  eq(inCtx(sandbox, 'state.route.name'), 'list', '返回列表');
  eq(inCtx(sandbox, 'state.route.view'), 'all', '返回视图为全部知识');
}

/* ── 场景 3：只读视角同样可用 ── */
{
  const { sandbox, reg, handlers } = loadApp();
  inCtx(sandbox, "state.role = 'reader';");
  clickAct(handlers, 'row-open', { 'data-id': 'k_policy' });
  const app = reg.get('#app').innerHTML;
  ok(app.indexOf('返回全部知识') >= 0, '只读视角详情也有返回入口');
  ok(app.indexOf('data-act="d-edit"') < 0, '只读视角没有编辑按钮');
  clickAct(handlers, 'd-back');
  eq(inCtx(sandbox, 'state.route.view'), 'all', '只读视角返回全部知识');
}

/* ── 场景 4：默认通用文档；未选文件不能继续，不选模板可继续 ── */
{
  const { sandbox, reg, handlers } = loadApp();
  sandbox.startImport();
  eq(inCtx(sandbox, 'work().templateId'), null, '第 1 步默认通用文档（未选模板）');
  eq(inCtx(sandbox, 'work().templateDecided'), true, '模板不阻挡继续（默认即通用文档）');
  eq(sandbox.step1Ready(), false, '未选择文件时不能继续');
  const html = reg.get('#app').innerHTML;
  ok(html.indexOf('补充信息（选填）') >= 0, '模板入口位于「补充信息（选填）」');
  ok(!/id="extra-box"[^>]*\sopen/.test(html), '「补充信息」默认折叠');
  ok(!/id="scenario-import-box"[^>]*\sopen/.test(html), '识别「演示场景」默认折叠');
  ok(html.indexOf('请先选择文件') >= 0, '禁用原因写明「请先选择文件」');
  clickAct(handlers, 'sample-pdf');
  eq(sandbox.step1Ready(), true, '选好文件后（未选模板）即可继续');
  clickAct(handlers, 'start-check');
  const step2 = reg.get('#app').innerHTML;
  ok(step2.indexOf('第 2 步：检查内容') >= 0, '进入第 2 步「检查内容」');
  ok(step2.indexOf('还需检查') >= 0, '第 2 步显示单一进度「还需检查 N 段」');
  ok(step2.indexOf('查看原文对照') >= 0, '第 2 步有原文对照入口');
  ok(!/id="orig-details"[^>]*\sopen/.test(step2), '原文对照默认折叠');
}

/* ── 场景 5：用途 / AI / 到期只在第 3 步（唯一可编辑位置） ── */
{
  const { sandbox, reg, handlers } = loadApp();
  sandbox.startImport();
  clickAct(handlers, 'sample-pdf');
  const step1 = reg.get('#app').innerHTML;
  eq(count(step1, 'data-act="w-usage"'), 0, '第 1 步没有用途控件');
  eq(count(step1, 'data-act="w-expiry"'), 0, '第 1 步没有到期控件');
  clickAct(handlers, 'start-check');
  const step2 = reg.get('#app').innerHTML;
  eq(count(step2, 'data-act="w-usage"'), 0, '第 2 步没有用途控件（不重复展示）');
  eq(count(step2, 'data-act="w-ai"'), 0, '第 2 步没有 AI 授权开关');
  eq(count(step2, 'data-act="w-expiry"'), 0, '第 2 步没有到期时间控件');
  eq(inCtx(sandbox, 'work().usage'), 'internal', '新知识默认内部使用');
  eq(inCtx(sandbox, 'work().aiOn'), false, '新知识默认 AI 关闭');
  clickAct(handlers, 'to-step3');
  const step3 = reg.get('#app').innerHTML;
  eq(inCtx(sandbox, 'work().step'), 3, '进入第 3 步发布设置');
  eq(count(step3, 'data-act="w-usage"'), 2, '第 3 步有一组用途单选（内部 / 对外）');
  eq(count(step3, 'data-act="w-ai"'), 1, '第 3 步只有一处 AI 授权开关');
  eq(count(step3, 'data-act="w-expiry"'), 1, '第 3 步只有一处到期时间输入');
  ok(step3.indexOf('发布前还需要完成') >= 0, '未通过项集中列出');
  ok(!/id="scenario-publish-box"[^>]*\sopen/.test(step3), '发布「演示场景」默认折叠');
  ok(step3.indexOf('标题已填写') < 0, '已通过项不铺满清单');
  eq(count(step3, 'data-act="gate-go"'), 1, '只列出未通过项（当前仅「正文未核对」1 项）');
  clickAct(handlers, 'gate-go', { 'data-go': 'paras' });
  eq(inCtx(sandbox, 'work().step'), 2, '点未通过项返回第 2 步对应区域');
}

/* ── 场景 6：点「来源」自动展开原文对照并定位到对应页 ── */
{
  const { sandbox, reg, handlers } = loadApp();
  sandbox.startImport();
  clickAct(handlers, 'sample-pdf');
  clickAct(handlers, 'start-check');
  clickAct(handlers, 'para-source', { 'data-id': 'd5' });   // d5 位于第 2 页
  eq(reg.get('#orig-details').open, true, '点「来源」自动展开原文对照');
  eq(inCtx(sandbox, 'work().curPage'), 2, '定位到来源所在页（第 2 页）');
}

/* ── 场景 7：未保存保护仍生效 ── */
{
  const { sandbox, reg, handlers } = loadApp();
  sandbox.startImport();
  clickAct(handlers, 'sample-pdf');
  clickAct(handlers, 'start-check');
  inCtx(sandbox, 'markWorkDirty()');
  clickAct(handlers, 'nav', { 'data-view': 'all' });
  eq(inCtx(sandbox, 'state.route.name'), 'review', '有未保存修改时不直接离开');
  ok(reg.get('#dlg').innerHTML.indexOf('未保存的修改') >= 0, '弹出未保存确认（保存草稿 / 放弃 / 继续编辑）');
}

console.log(failures ? ('\n第二轮改版回归：' + failures + ' 项未通过') : '\n第二轮改版回归：全部通过');
process.exit(failures ? 1 : 0);
