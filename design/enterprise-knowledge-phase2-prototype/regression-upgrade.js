'use strict';
/*
 * 升级流程回归（DOM 沙盒，无依赖；运行：node regression-upgrade.js）
 * 只覆盖三件容易回归、且曾在真实浏览器里出问题的事：
 *  1) 采用新版模板确认后，向导直接停在第 2 步「检查内容」（不是回到第 1 步要求重新选文件）；
 *  2) 升级只生成/更新草稿：当前发布版本号与已发布内容保持不变；
 *  3) 已有草稿时基于草稿升级：草稿正文与字段值保留、被移除字段的旧值进入历史。
 * 这里用最小 DOM 桩在 Node 里加载 app.js 并触发对话框确认，只验证状态与渲染结果；
 * 真实浏览器走查（布局、键盘、对话框焦点）仍由人工/Codex 执行。
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

function clickDialogAction(handlers, index) {
  const btn = { getAttribute: (k) => (k === 'data-dlg' ? String(index) : null) };
  const ev = { target: { closest: (sel) => (sel === '[data-dlg]' ? btn : null) } };
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

/* ── 场景 1/2：没有草稿的已发布知识升级模板 ─────────────────────────── */
{
  const { sandbox, reg, handlers } = loadApp();
  const e = sandbox.byId('k_install');
  const publishedFieldsBefore = JSON.stringify(e.fields);
  const versionsBefore = e.versions.length;

  sandbox.openUpgradeDialog(e);
  const dlg = reg.get('#dlg').innerHTML;
  ok(dlg.indexOf('从 v1 升级到 v2') >= 0, '对话框按已发布模板版本比较（v1 → v2）');
  ok(dlg.indexOf('服务保障期') >= 0, '对话框列出新增字段（服务保障期）');
  ok(dlg.indexOf('服务方式') >= 0, '对话框列出移除字段（服务方式）');

  clickDialogAction(handlers, 1); // 「确认采用（生成升级草稿）」

  const w = sandbox.work();
  eq(w.step, 2, '确认后工作副本处于第 2 步');
  ok(reg.get('#app').innerHTML.indexOf('第 2 步：检查内容') >= 0, '确认后页面直接显示第 2 步检查（无需重新选择文件）');
  ok(reg.get('#app').innerHTML.indexOf('id="step1-next"') < 0, '确认后不再出现第 1 步「开始检查」按钮');
  eq(sandbox.crumbHTML(), '企业知识 / <b>核对内容</b>', '确认后路由停在核对向导（非新建文件流程）');

  eq(w.source && w.source.file, '上门安装服务方案.pdf', '升级保留原来源文件');
  eq(w.paragraphs.length, 3, '升级保留原始段落');
  const f = (k) => w.fields.find((x) => x.key === k);
  eq(f('f_service_time') && f('f_service_time').value, '4', '沿用字段保留已填写值（服务时效=4）');
  eq(f('f_service_time') && f('f_service_time').checked, true, '未变化字段保留「已核对」勾选');
  eq(f('f_service_area') && f('f_service_area').value, '市区', '沿用字段保留已填写值（服务区域=市区）');
  eq(f('f_warranty') && f('f_warranty').value, '', '新增字段留空待填（服务保障期）');
  eq(f('f_service_mode'), undefined, '移除字段不再出现在字段区（服务方式）');
  const h = w.historicFields.find((x) => x.key === 'f_service_mode');
  eq(h && h.value, '上门安装（市区与近郊）', '移除字段的旧值进入历史');

  ok(!!e.draft, '确认后生成升级草稿');
  eq(e.draft && e.draft.kind, 'upgrade', '草稿类型标记为模板升级');
  eq(e.draft.content.templateVersion, 2, '草稿已按新版模板 v2');

  eq(e.currentVersion, 1, '发布版本号不变（仍是 v1）');
  eq(e.templateVersion, 1, '已发布内容仍挂 v1 模板');
  eq(JSON.stringify(e.fields), publishedFieldsBefore, '已发布字段未被升级改动');
  eq(e.versions.length, versionsBefore, '版本历史未新增（尚未保存/发布）');
}

/* ── 场景 3：已有草稿时，基于草稿升级（草稿正文与字段值保留） ────────── */
{
  const { sandbox, reg, handlers } = loadApp();
  const tpl = sandbox.templateById('tpl_service');
  tpl.versions.push({ v: 3, date: '2026-10-11', note: '回归用：移除服务区域、新增支持渠道',
    fields: [
      { key: 'f_service_time', label: '服务时效', type: 'number', unit: '小时', required: true, help: '', options: [] },
      { key: 'f_warranty', label: '服务保障期', type: 'number', unit: '月', required: false, help: '', options: [] },
      { key: 'f_support', label: '支持渠道', type: 'text', required: false, help: '', options: [] }
    ] });
  const e = sandbox.byId('k_install');
  e.draft = {
    label: '编辑草稿', summary: '编辑内容', kind: 'edit', createdAt: '2026-10-11',
    content: {
      title: e.title, category: e.category, usage: e.usage, aiOn: true, expiry: '',
      templateId: 'tpl_service', templateVersion: 2,
      source: { file: '上门安装服务方案.pdf', kind: 'PDF · 文本可复制', pages: 1, locKind: 'page', at: '' },
      fields: [
        { key: 'f_service_time', label: '服务时效', type: 'number', unit: '小时', options: [], value: '6', checked: true, aiAllowed: true, sourceKind: '人工修改', loc: 1, modified: true, historic: false },
        { key: 'f_service_area', label: '服务区域', type: 'options', options: ['市区', '近郊', '全国'], unit: '', value: '近郊', checked: true, aiAllowed: false, sourceKind: '人工修改', loc: 1, modified: true, historic: false },
        { key: 'f_warranty', label: '服务保障期', type: 'number', unit: '月', options: [], value: '12', checked: false, aiAllowed: false, sourceKind: '', loc: null, modified: false, historic: false }
      ],
      historicFields: [{ key: 'f_service_mode', label: '服务方式', value: '上门安装（市区与近郊）', historic: true }],
      paragraphs: [
        { id: 'ki1', text: '上门安装服务：确认预约后 6 小时内完成（仅市区）。', included: true, checked: false, internal: false, needsReview: false, page: 1, modified: true },
        { id: 'ki2', text: '标准上门安装不额外收费；如需更换配件，按配件清单单独计费。', included: true, checked: true, internal: false, needsReview: false, page: 1, modified: false }
      ]
    }
  };

  sandbox.openUpgradeDialog(e);
  const dlg = reg.get('#dlg').innerHTML;
  ok(dlg.indexOf('从 v2 升级到 v3') >= 0, '有草稿时对话框按草稿的模板版本比较（v2 → v3）');
  ok(dlg.indexOf('基于当前草稿') >= 0, '对话框说明将基于当前草稿升级');

  clickDialogAction(handlers, 1);

  const w = sandbox.work();
  eq(w.step, 2, '基于草稿升级后直接进入第 2 步');
  ok(reg.get('#app').innerHTML.indexOf('第 2 步：检查内容') >= 0, '基于草稿升级后页面显示第 2 步');
  eq(w.paragraphs.length, 2, '保留草稿正文段数（不是已发布版本的 3 段）');
  ok(w.paragraphs.some((p) => p.text.indexOf('6 小时内') >= 0), '保留草稿中修改过的正文文字');
  const g = (k) => w.fields.find((x) => x.key === k);
  eq(g('f_service_time') && g('f_service_time').value, '6', '保留草稿字段值（服务时效=6，而非已发布的 4）');
  eq(g('f_service_time') && g('f_service_time').checked, true, '未变化字段保留草稿中的「已核对」勾选');
  eq(g('f_warranty') && g('f_warranty').value, '12', '上一轮新增字段保留草稿值（服务保障期=12）');
  eq(g('f_support') && g('f_support').value, '', '本轮新增字段留空待填（支持渠道）');
  eq(g('f_service_area'), undefined, '本轮移除字段不在字段区（服务区域）');
  const hArea = w.historicFields.find((x) => x.key === 'f_service_area');
  const hMode = w.historicFields.find((x) => x.key === 'f_service_mode');
  eq(hArea && hArea.value, '近郊', '本轮移除字段取草稿值进入历史（服务区域=近郊）');
  eq(hMode && hMode.value, '上门安装（市区与近郊）', '既有历史（服务方式）再次升级后仍保留');
  eq(e.draft && e.draft.content && e.draft.content.templateVersion, 3, '草稿升级到 v3');
  eq(e.currentVersion, 1, '发布版本号仍不变');
  eq(e.templateVersion, 1, '已发布内容仍挂 v1 模板');
  eq((e.fields.find((x) => x.key === 'f_service_mode') || {}).value, '上门安装（市区与近郊）', '已发布字段原样保留');
}

console.log(failures ? ('\n升级流程回归：' + failures + ' 项未通过') : '\n升级流程回归：全部通过');
process.exit(failures ? 1 : 0);
