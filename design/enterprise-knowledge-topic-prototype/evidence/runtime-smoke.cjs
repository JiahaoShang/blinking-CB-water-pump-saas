'use strict';
/* 运行时冒烟检查（Hermes 编写，Codex 保存便于复跑）：最小 DOM stub 加载真实 app.js，
   覆盖第三轮修复（对话框派发 / 核对数量实时更新 / 紧凑折叠）与既往回归项；
   第九轮（2026-10-12）：总览内部主题 / 文件树、每主题折叠与状态同步、唯一文件入口、两种返回语义；
   第十轮（2026-10-12）：中央总览分组折叠（默认展开、收起不导航、与左侧树独立、跨返回保持、搜索临时展开 / 清空恢复、管理按钮不嵌套）。 */
const fs = require('fs');
const vm = require('vm');
const APP = require('path').join(__dirname, '..', 'app.js');

let fails = 0;
const ok = (c, l) => { if (c) console.log('PASS ' + l); else { fails++; console.log('FAIL ' + l); } };
const focusLog = [];

function makeEl(tag) {
  return {
    tagName: (tag || 'div').toUpperCase(),
    innerHTML: '', textContent: '', value: '', hidden: false, open: false, checked: false,
    attrs: {},
    classList: { _s: {}, add(c) { this._s[c] = 1; }, remove(c) { delete this._s[c]; }, toggle(c, f) { const on = f === undefined ? !this._s[c] : !!f; if (on) this._s[c] = 1; else delete this._s[c]; return on; }, contains(c) { return !!this._s[c]; } },
    addEventListener() {},
    setAttribute(k, v) { this.attrs[k] = String(v); },
    removeAttribute(k) { delete this.attrs[k]; },
    getAttribute(k) { return (k in this.attrs) ? this.attrs[k] : null; },
    focus() { focusLog.push(this._dbg || this.tagName); },
    appendChild() {}, remove() {}, insertAdjacentHTML() {},
    querySelector() { return null; }, querySelectorAll() { return []; },
    closest() { return null; }, scrollIntoView() {},
    showModal() { this.open = true; }, close() { this.open = false; }
  };
}
function loadApp() {
  const reg = new Map();
  const handlers = {};
  const sandbox = {
    document: {
      body: makeEl('body'), activeElement: null,
      createElement: (t) => makeEl(t),
      querySelector(sel) { if (!reg.has(sel)) { const e = makeEl('div'); e._dbg = sel; reg.set(sel, e); } return reg.get(sel); },
      querySelectorAll() { return []; },
      addEventListener(type, fn) { (handlers[type] = handlers[type] || []).push(fn); }
    },
    window: { scrollTo() {} },
    console,
    requestAnimationFrame: () => 0,
    setTimeout: () => 0
  };
  vm.createContext(sandbox);
  vm.runInContext(fs.readFileSync(APP, 'utf8'), sandbox, { filename: 'app.js' });
  return { sandbox, reg, handlers };
}
const inCtx = (sb, expr) => vm.runInContext(expr, sb);
/* stub 特性：只有被 app 以 qs 查询过的选择器才存在于注册表；q() 先查询再取回同一元素 */
function q(sb, reg, sel) { inCtx(sb, 'qs(' + JSON.stringify(sel) + ')'); return reg.get(sel); }

function elStub(attrs, tag) {
  const el = {
    tagName: tag || 'BUTTON',
    getAttribute(k) { return (k in attrs) ? String(attrs[k]) : null; },
    closest(sel) { return sel === '[data-act]' ? el : null; }
  };
  return el;
}
function clickAct(handlers, act, attrs) {
  const el = elStub(Object.assign({ 'data-act': act }, attrs || {}));
  (handlers.click || []).forEach((fn) => fn({ target: el }));
}
function dlgClick(handlers, idx) {
  const btn = { tagName: 'BUTTON', getAttribute: (k) => (k === 'data-dlg' ? String(idx) : null) };
  const el = { tagName: 'BUTTON', closest(sel) { return sel === '[data-dlg]' ? btn : null; } };
  (handlers.click || []).forEach((fn) => fn({ target: el }));
}
function changeAct(handlers, act, attrs, extra) {
  const el = elStub(Object.assign({ 'data-act': act }, attrs || {}), 'INPUT');
  Object.assign(el, extra || {});
  (handlers.change || []).forEach((fn) => fn({ target: el }));
}
function inputAct(handlers, act, attrs, value) {
  const el = elStub(Object.assign({ 'data-act': act }, attrs || {}), 'TEXTAREA');
  el.value = value;
  (handlers.input || []).forEach((fn) => fn({ target: el }));
}
function keydown(handlers, key) {
  (handlers.keydown || []).forEach((fn) => fn({ key: key }));
}

/* ══ 1. 基础与既往回归（返回栈 / 双标签 / 折叠区 / 搜索 X） ══ */
{
  const { sandbox, reg, handlers } = loadApp();
  ok(inCtx(sandbox, 'state.route.name') === 'topics', '默认进入全部主题');
  ok(reg.get('#app').innerHTML.indexOf('按主题查看企业知识') >= 0, '主题页任务式引导语');
  ok(reg.get('#app').innerHTML.indexOf('两个维度') < 0, '主题页无理论说明');

  const ta = inCtx(sandbox, 'topicAvailability(topicById("t_meeting"))');
  ok(ta.pill === 'ok' && ta.label === '已发布（AI 客服可用）', 't_meeting 绿色可用');
  const ta2 = inCtx(sandbox, 'topicAvailability(topicById("t_onboarding"))');
  ok(ta2.pill === 'pub' && ta2.reason.indexOf('未授权') >= 0, 't_onboarding 蓝灰不可用 + 原因');
  ok(inCtx(sandbox, 'topicsRegionHTML()').indexOf('已发布（AI 客服不可用）') >= 0, '主题列表双标签');

  /* 布局归属（2026-10-12 侧栏恢复）+ 第九轮：知识四项在左侧主导航；内部浏览列从总览即显示 */
  const sideNav0 = reg.get('#side-nav').innerHTML;
  ok(sideNav0.indexOf('全部主题') >= 0 && sideNav0.indexOf('全部资料') >= 0 && sideNav0.indexOf('添加资料') >= 0 && sideNav0.indexOf('问答测试') >= 0, '左侧主导航含知识四项（管理员含添加资料）');
  ok(sideNav0.indexOf('工作台') < 0 && sideNav0.indexOf('收件箱') < 0 && sideNav0.indexOf('渠道账号') < 0 && sideNav0.indexOf('设置') < 0 && sideNav0.indexOf('aria-disabled') < 0, '主导航不含平台其他模块占位（已撤回）');
  ok(sideNav0.indexOf('data-act="nav-topic"') < 0 && sideNav0.indexOf('data-act="nav-file"') < 0, '主题 / 文件树不在左侧主导航');
  /* 第九轮（2026-10-12）：内部主题 / 文件浏览列从总览（route=topics）即显示，
     主题下按分组 → 主题 → 关联文件组织，文件默认收起（替代旧「总览不出现树」规则） */
  const ovNow = reg.get('#app').innerHTML;
  ok(ovNow.indexOf('aria-label="主题浏览导航"') >= 0 && ovNow.indexOf('id="topic-browse-details"') >= 0, '总览页出现主题 / 文件浏览列（桌面 + 窄屏两套）');
  ok(ovNow.indexOf('id="nav-filesbtn-t_meeting"') >= 0 && ovNow.indexOf('id="mnav-filesbtn-t_meeting"') >= 0, '每个主题行含独立折叠按钮（两套树）');
  ok(ovNow.indexOf('id="nav-files-t_meeting" inert') >= 0 && ovNow.indexOf('ts-files open') < 0, '各主题文件默认收起（inert、无 open）');
  ok(ovNow.indexOf('data-act="nav-file" data-id="m_spec"') >= 0, '主题下的文件列表由树渲染（唯一文件入口的载体）');

  inCtx(sandbox, 'state.filters.format = "pdf"; openTopic("t_meeting", null);');
  ok(reg.get('#app').innerHTML.indexOf('aria-label="主题浏览导航"') >= 0 && reg.get('#app').innerHTML.indexOf('浏览主题') >= 0, '主题页在工作区内部出现主题树（含窄屏折叠）');
  ok(reg.get('#side-nav').innerHTML.indexOf('data-act="nav-topic"') < 0, '主题树不在左侧主导航（在工作区内部）');
  const topicHtmlNow = reg.get('#app').innerHTML;
  ok(topicHtmlNow.indexOf('<span class="fold-title">关联资料</span>') >= 0, '主题页关联资料折叠（带数量）');
  /* 第九轮规则：出处为非交互文本；打开原始文件的唯一列表入口 = 主题浏览树的文件列表（关联资料不再重复按钮） */
  ok(topicHtmlNow.indexOf('data-act="m-open"') < 0, '主题页「关联资料」不再含文件打开按钮（不重复入口）');
  ok(topicHtmlNow.indexOf('data-act="nav-file"') >= 0 && topicHtmlNow.indexOf('data-act="t-detach"') >= 0 && topicHtmlNow.indexOf('关联已有资料') >= 0, '树文件列表为打开入口；「关联资料」保留非点击信息与原关联编辑 / 移除操作');
  ok(topicHtmlNow.indexOf('data-act="t-source"') < 0 && topicHtmlNow.indexOf('src-chip muted-src') >= 0 && topicHtmlNow.indexOf('· 第 2 页') >= 0, '出处为非交互文本且保留文件与位置');
  ok(topicHtmlNow.indexOf('来源为仅内部 / 未授权 · 不用于对外回答') >= 0 || inCtx(sandbox, 'topicHTML("t_onboarding")').indexOf('来源为仅内部 / 未授权 · 不用于对外回答') >= 0, '出处保留不符合对外 AI 条件的标记（含此类来源的主题）');
  ok(topicHtmlNow.indexOf('从主题浏览列表打开原始文件') >= 0, '主题页轻量提示指向主题浏览列表');
  clickAct(handlers, 'nav-file', { 'data-id': 'm_spec' });
  ok(inCtx(sandbox, 'state.route.name') === 'material', '经树文件列表打开资料');
  ok(inCtx(sandbox, 'state.backStack[state.backStack.length-1].name') === 'topic', '主题树开文件的返回上下文 = 原主题');
  clickAct(handlers, 'd-back');
  ok(inCtx(sandbox, 'state.route.name') === 'topic' && reg.get('#app').innerHTML.indexOf('section-fold topic-block" open') >= 0, '返回主题并恢复关联资料展开');
  clickAct(handlers, 'd-back');
  ok(inCtx(sandbox, 'state.route.name') === 'topics', '再返回回到最初入口');

  inCtx(sandbox, 'goMaterials(); state.filters.format = "pdf"; openTopic("t_meeting", null); openMaterial("m_spec", null); doBack(); doBack();');
  ok(inCtx(sandbox, 'state.route.name') === 'materials' && inCtx(sandbox, 'state.filters.format') === 'pdf', '多层返回回到带筛选的全部资料');

  ok(inCtx(sandbox, 'materialsHTML()').indexOf('aria-label="清空搜索关键词" hidden') >= 0, '搜索清除按钮常驻且默认hidden');
  inCtx(sandbox, 'state.filters.q = "x";');
  ok(inCtx(sandbox, 'materialsHTML()').indexOf('aria-label="清空搜索关键词" hidden') < 0, '有关键词时不带hidden');
  inCtx(sandbox, 'state.filters = { q:"", format:"", topic:"", status:"" };');
}

/* ══ 2. 对话框派发（本轮修复 1：data-dlg → state.dlgActions） ══ */
{
  const { sandbox, reg, handlers } = loadApp();
  inCtx(sandbox, 'startCapture("");');
  clickAct(handlers, 'c-load-sample');
  clickAct(handlers, 'c-next');
  clickAct(handlers, 'c-use-samples');   /* 需要先有整理结果，发布闸门才能通过 */
  const sugId = inCtx(sandbox, 'work().items.find(function(x){return x.format==="video";}).sugs[0].id');
  clickAct(handlers, 'sg-rename', { 'data-id': sugId });
  ok(reg.get('#dlg').open === true, '改名对话框打开');
  inCtx(sandbox, 'qs("#dlg-input")');   /* stub 特性：动作读取走无根 qs，先注册同一元素 */
  reg.get('#dlg-input').value = '';
  dlgClick(handlers, 1);
  ok(reg.get('#dlg').open === true && inCtx(sandbox, "findSug('" + sugId + "').name") === '设备A安装与配置', '校验失败：返回 keep，对话框保持打开、名称不变');
  reg.get('#dlg-input').value = '设备A包装参考';
  dlgClick(handlers, 1);
  ok(inCtx(sandbox, "findSug('" + sugId + "').name") === '设备A包装参考', '确认后名称已保存（data-dlg 派发生效）');
  ok(reg.get('#dlg').open === false, '确认后对话框关闭');
  const photoSug = inCtx(sandbox, 'work().items.find(function(x){return x.format==="image";}).sugs[0].id');
  clickAct(handlers, 'sg-merge', { 'data-id': photoSug });
  ok(reg.get('#dlg').open === true, '合并对话框打开');
  dlgClick(handlers, 0);
  ok(reg.get('#dlg').open === false, '取消：对话框关闭');
  ok(inCtx(sandbox, "findSug('" + photoSug + "').kind") === 'new' && inCtx(sandbox, "findSug('" + photoSug + "').active") === true, '取消不改动建议');
  /* 发布确认对话框的取消与确认也走同一派发 */
  clickAct(handlers, 'c-next2');
  inCtx(sandbox, 'work().usage = "external"; work().aiOn = true;');
  inCtx(sandbox, 'work().items.forEach(function(it){ it.paras.forEach(function(p){ p.checked = true; }); });');
  const mBefore = inCtx(sandbox, 'DB.materials.length');
  clickAct(handlers, 'c-publish');
  ok(reg.get('#dlg').open === true, '对外+AI 发布确认对话框打开');
  dlgClick(handlers, 0);
  ok(reg.get('#dlg').open === false && inCtx(sandbox, 'DB.materials.length') === mBefore, '取消发布：对话框关闭、未发布');
  clickAct(handlers, 'c-publish');
  dlgClick(handlers, 1);
  ok(inCtx(sandbox, 'DB.materials.length') === mBefore + 5, '确认发布：走同一派发完成批次提交');
}

/* ══ 3. 核对数量实时更新 + 折叠不丢（本轮修复 2/3） ══ */
{
  const { sandbox, reg, handlers } = loadApp();
  inCtx(sandbox, 'startCapture("");');
  clickAct(handlers, 'c-load-sample');
  clickAct(handlers, 'c-next');
  clickAct(handlers, 'c-use-samples');
  const it0 = inCtx(sandbox, 'work().items[0].id');           /* 规格书：3 段 */
  const it1 = inCtx(sandbox, 'work().items[1].id');           /* 安装指南：3 段 */
  const it2 = inCtx(sandbox, 'work().items[2].id');           /* 图片：仅登记 */
  ok(inCtx(sandbox, 'work().items[0].foldBody') === true && inCtx(sandbox, 'work().items[1].foldBody') === false, '默认只展开第一件有正文资料');
  ok(inCtx(sandbox, 'work().items[2].foldBody') !== true, '仅登记资料无正文折叠');
  ok(reg.get('#app').innerHTML.indexOf('fold-head') >= 0 && reg.get('#app').innerHTML.indexOf('调整主题') >= 0, '紧凑折叠呈现在本批列表');
  ok(reg.get('#app').innerHTML.indexOf('id="it-body-' + it0 + '" hidden') < 0 && reg.get('#app').innerHTML.indexOf('id="it-body-' + it1 + '" hidden') >= 0, '折叠默认状态正确（首件展开、其余收起）');

  /* 展开第一件的「调整主题」并保持；核对不应使其折叠（无整页重渲染） */
  clickAct(handlers, 'c-fold-topic', { 'data-id': it0 });
  ok(q(sandbox, reg, '#it-topic-' + it0).hidden === false, '调整主题展开');
  const p0 = inCtx(sandbox, 'work().items[0].paras[0].id');
  const p1 = inCtx(sandbox, 'work().items[0].paras[1].id');
  const p2 = inCtx(sandbox, 'work().items[0].paras[2].id');
  changeAct(handlers, 'c-para-check', { 'data-id': p0 }, { checked: true });
  ok(q(sandbox, reg, '#it-status-' + it0).textContent === '待核对 2 段', '勾选 1 段后数量标签更新（3→2）');
  changeAct(handlers, 'c-para-check', { 'data-id': p1 }, { checked: true });
  changeAct(handlers, 'c-para-check', { 'data-id': p2 }, { checked: true });
  ok(q(sandbox, reg, '#it-status-' + it0).textContent === '已核对', '全部核对后显示「已核对」');
  ok(q(sandbox, reg, '#it-remain-' + it0).textContent === '已核对', '折叠标题剩余数量同步');
  ok(q(sandbox, reg, '#it-topic-' + it0).hidden === false, '核对过程无整页重渲染（已展开区域保留）');
  inputAct(handlers, 'c-para-text', { 'data-id': p1 }, '壁挂安装：使用随机挂架，按孔位固定（已编辑）。');
  ok(inCtx(sandbox, 'work().items[0].paras[1].checked') === false, '编辑正文撤销该段核对');
  ok(q(sandbox, reg, '#it-status-' + it0).textContent === '待核对 1 段', '编辑后数量标签回到 1');
  ok(q(sandbox, reg, '#it-src-' + it0).innerHTML.indexOf('待核对') >= 0, '出处列表同步更新');
  clickAct(handlers, 'c-fold-topic', { 'data-id': it0 });
  ok(q(sandbox, reg, '#it-topic-' + it0).hidden === true, '再次点击折叠收起');
  clickAct(handlers, 'c-fold-body', { 'data-id': it1 });
  ok(q(sandbox, reg, '#it-body-' + it1).hidden === false && inCtx(sandbox, 'work().items[1].foldBody') === true, '其它资料可单独展开检查正文');
  /* 折叠状态经整页渲染（如建议改名后）仍保留 */
  inCtx(sandbox, 'render();');
  ok(reg.get('#app').innerHTML.indexOf('id="it-body-' + it1 + '" hidden') < 0, '整页渲染后展开状态保留（状态在工作副本上）');
}

/* ══ 4. 整理初稿 / 同名合并 / 防重复（保持既有行为） ══ */
{
  const { sandbox } = loadApp();
  inCtx(sandbox, 'attachMaterialToTopic("m_spec", "t_quote")');
  ok(inCtx(sandbox, 'organizeIntoTopic(topicById("t_quote"), topicById("t_quote"))') === 1, '整理初稿只取已核对段落');
  ok(inCtx(sandbox, 'topicById("t_quote").intro[0].checked') === false, '初稿默认待核对');
  ok(inCtx(sandbox, 'organizeIntoTopic(topicById("t_quote"), topicById("t_quote"))') === 0, '重复整理不追加');
  inCtx(sandbox, `startCapture('');
    var itA = addWorkItem({ name:'a.pdf', format:'pdf', sampleKey:'pdf', src:'file', size:'1 KB' });
    var itB = addWorkItem({ name:'b.pdf', format:'pdf', sampleKey:'pdf', src:'file', size:'1 KB' });
    itA.paras = [{ id:'pa', text:'A 段。', loc:'第 1 页', source:'第 1 页', checked:true, modified:false }];
    itB.paras = [{ id:'pb', text:'B 段。', loc:'第 1 页', source:'第 1 页', checked:true, modified:false }];
    itA.sugs = [{ id:'sa', kind:'new', name:'新主题X', active:true }];
    itB.sugs = [{ id:'sb', kind:'new', name:'新主题X', active:true }];
    itA.confirmedTopics = []; itB.confirmedTopics = [];
    var before = DB.topics.length; confirmAssoc(); var after = DB.topics.length;`);
  ok(inCtx(sandbox, 'after - before') === 1, '同名新主题建议合并为一个主题');
  ok(inCtx(sandbox, 'work().items[0].sugs[0].createdId === work().items[1].sugs[0].createdId'), '两件资料关联到同一个新主题');
  const beforePub = inCtx(sandbox, 'DB.materials.length');
  inCtx(sandbox, 'commitPublishBatch(""); commitPublishBatch("");');
  ok(inCtx(sandbox, 'DB.materials.length') === beforePub + 2, '批次发布只生成一次（防重复）');
  ok(inCtx(sandbox, 'DB.topics[DB.topics.length-1].intro.every(function(p){return p.checked===false;})'), '新主题初稿全部待核对');
}

/* ══ 5. 只读视角 ══ */
{
  const { sandbox, reg } = loadApp();
  inCtx(sandbox, 'state.role = "reader"; render();');
  ok(inCtx(sandbox, 'state.route.name') === 'topics' && reg.get('#app').innerHTML.indexOf('data-act="t-edit"') < 0, '只读视角渲染正常、无编辑入口');
  ok(reg.get('#side-nav').innerHTML.indexOf('添加资料') < 0 && reg.get('#side-nav').innerHTML.indexOf('全部主题') >= 0, '只读视角：左侧主导航不出现添加资料（权限控制不变）');
}

/* ══ 6. 主流程真实派发：c-next2 → 设置控件 → c-publish / 保存草稿 / 返回（本轮修复） ══ */
{
  const { sandbox, reg, handlers } = loadApp();
  clickAct(handlers, 'nav', { 'data-view': 'capture' });
  clickAct(handlers, 'c-load-sample');
  clickAct(handlers, 'c-next');
  clickAct(handlers, 'c-use-samples');
  const pids = inCtx(sandbox, 'work().items.reduce(function(a,it){return a.concat(it.paras.map(function(p){return p.id;}));},[])');
  pids.forEach((pid) => changeAct(handlers, 'c-para-check', { 'data-id': pid }, { checked: true }));
  clickAct(handlers, 'c-confirm-assoc');
  clickAct(handlers, 'c-next2');   /* 真实点击「下一步：发布设置」 */
  ok(inCtx(sandbox, 'work().step') === 3, 'c-next2 真实派发后到达第 3 步');
  const step3 = reg.get('#app').innerHTML;
  ok(step3.indexOf('第 3 步：发布设置') >= 0, 'c-next2 后渲染出第 3 步');
  ok(step3.indexOf('data-act="w-usage"') >= 0 && step3.indexOf('id="w-ai"') >= 0 && step3.indexOf('id="w-expiry"') >= 0 && step3.indexOf('settings-group') >= 0, '设置区完整渲染（用途 / AI / 到期同卡）');
  /* 内用默认 + AI 关：发布无确认对话框，直接提交 */
  const m0 = inCtx(sandbox, 'DB.materials.length');
  clickAct(handlers, 'c-publish');
  ok(reg.get('#dlg').open === false && inCtx(sandbox, 'DB.materials.length') === m0 + 5, '内用默认 AI 关：发布直接提交（无确认）');
  /* 保存草稿路径（新的批次，允许不完整） */
  clickAct(handlers, 'nav', { 'data-view': 'capture' });
  clickAct(handlers, 'c-load-sample');
  clickAct(handlers, 'c-next');
  clickAct(handlers, 'c-next2');
  const m1 = inCtx(sandbox, 'DB.materials.length');
  clickAct(handlers, 'c-save-draft');
  ok(inCtx(sandbox, 'DB.materials.length') === m1 + 5 && inCtx(sandbox, 'work() === null'), '保存草稿：创建 5 件草稿资料并离开流程');
  ok(inCtx(sandbox, 'DB.materials.filter(function(m){return m.lifecycle==="draft";}).length') >= 5, '草稿生命周期正确');
  /* 返回按钮可达（在同一批未提交时） */
  clickAct(handlers, 'nav', { 'data-view': 'capture' });
  clickAct(handlers, 'c-load-sample');
  clickAct(handlers, 'c-next');
  clickAct(handlers, 'c-next2');
  clickAct(handlers, 'c-to-step2');
  ok(inCtx(sandbox, 'work().step') === 2, 'c-to-step2 真实派发可达');
  clickAct(handlers, 'c-back');
  ok(inCtx(sandbox, 'work().step') === 1, 'c-back 真实派发可达');
}

/* ══ 7. 布局归属（2026-10-12 侧栏恢复）：平台抽屉 / 左侧主导航 / 内部主题树；重复点击当前主题不压栈 ══ */
{
  const { sandbox, reg, handlers } = loadApp();
  /* 平台抽屉含知识四项：点左侧导航入口（全部主题）后关闭 */
  inCtx(sandbox, 'document.body.classList.add("nav-open"); qs("#side-scrim").hidden = false;');
  clickAct(handlers, 'nav', { 'data-view': 'topics' });
  ok(inCtx(sandbox, 'document.body.classList.contains("nav-open")') === false, '点击左侧导航入口后关闭抽屉');
  /* 进入主题页：工作区内部主题树就位（桌面列 + 窄屏折叠两套 DOM） */
  clickAct(handlers, 't-open', { 'data-id': 't_meeting' });
  ok(inCtx(sandbox, 'state.route.name') === 'topic' && reg.get('#app').innerHTML.indexOf('aria-label="主题浏览导航"') >= 0, '主题树在工作区内部');
  ok(reg.get('#app').innerHTML.indexOf('id="topic-browse-details"') >= 0 && reg.get('#app').innerHTML.indexOf('id="mnav-sub-g_product"') >= 0, '窄屏「浏览主题」折叠与独立前缀同在工作区内');
  /* 分组开合：两套树同步（aria-expanded + inert） */
  ok(reg.get('#app').innerHTML.indexOf('id="nav-sub-g_product" inert') < 0, '主题页默认展开所在分组（桌面树）');
  clickAct(handlers, 'nav-toggle-group', { 'data-gid': 'g_product' });
  ok(q(sandbox, reg, '#nav-grpbtn-g_product').getAttribute('aria-expanded') === 'false' && q(sandbox, reg, '#nav-sub-g_product').getAttribute('inert') === '', '收起：aria 与 inert 同步（桌面树）');
  ok(q(sandbox, reg, '#mnav-grpbtn-g_product').getAttribute('aria-expanded') === 'false' && q(sandbox, reg, '#mnav-sub-g_product').getAttribute('inert') === '', '收起：窄屏树同步');
  clickAct(handlers, 'nav-toggle-group', { 'data-gid': 'g_product' });
  ok(q(sandbox, reg, '#nav-grpbtn-g_product').getAttribute('aria-expanded') === 'true' && q(sandbox, reg, '#nav-sub-g_product').getAttribute('inert') === null, '展开：aria 与 inert 恢复');
  /* 重复点击当前主题：只关抽屉并收起窄屏浏览树，不压返回层 */
  q(sandbox, reg, '#topic-browse-details').open = true;
  inCtx(sandbox, 'document.body.classList.add("nav-open");');
  const stackBefore = inCtx(sandbox, 'state.backStack.length');
  clickAct(handlers, 'nav-topic', { 'data-id': 't_meeting' });
  ok(inCtx(sandbox, 'document.body.classList.contains("nav-open")') === false, '重复点击当前主题：关闭平台抽屉');
  ok(q(sandbox, reg, '#topic-browse-details').open === false, '重复点击当前主题：收起窄屏「浏览主题」');
  ok(inCtx(sandbox, 'state.backStack.length') === stackBefore && inCtx(sandbox, 'state.route.id') === 't_meeting', '重复点击当前主题不压返回层（仍在原主题）');
  /* 点击其他主题：正常导航并只压一层；窄屏浏览树以收起状态呈现 */
  clickAct(handlers, 'nav-topic', { 'data-id': 't_aftersale' });
  ok(inCtx(sandbox, 'state.route.id') === 't_aftersale' && inCtx(sandbox, 'state.backStack.length') === stackBefore + 1, '点击其他主题正常打开并只压一层');
  ok(reg.get('#app').innerHTML.indexOf('id="topic-browse-details" open') < 0, '选中主题后窄屏浏览树收起（默认折叠）');
  clickAct(handlers, 'd-back');
  ok(inCtx(sandbox, 'state.route.id') === 't_meeting', '返回回到上一主题');
  /* 经主题树文件列表进入的资料详情保留内部主题树（既有 backStack 推导；不改返回栈） */
  clickAct(handlers, 'nav-file', { 'data-id': 'm_spec' });
  ok(inCtx(sandbox, 'state.route.name') === 'material' && reg.get('#app').innerHTML.indexOf('aria-label="主题浏览导航"') >= 0, '经主题树进入的资料详情保留内部主题树');
  clickAct(handlers, 'd-back');
  ok(inCtx(sandbox, 'state.route.name') === 'topic', '资料详情返回主题正常');
  /* Escape 与焦点行为保留（平台抽屉） */
  inCtx(sandbox, 'document.body.classList.add("nav-open"); qs("#side-scrim").hidden = false;');
  focusLog.length = 0;
  keydown(handlers, 'Escape');
  ok(inCtx(sandbox, 'document.body.classList.contains("nav-open")') === false, 'Escape 收起抽屉');
  ok(focusLog[focusLog.length - 1] === '#menu-btn', 'Escape 后焦点回到菜单按钮');
  /* 资料列表页：无内部主题树；工作区功能导航可进入 */
  clickAct(handlers, 'nav', { 'data-view': 'materials' });
  ok(inCtx(sandbox, 'state.route.name') === 'materials' && reg.get('#app').innerHTML.indexOf('aria-label="主题浏览导航"') < 0, '资料列表页无主题树');
}

/* ══ 8. 分组管理：新建分组入口（仅管理员、管理模式可见）并复用既有对话框/校验 ══ */
{
  const { sandbox, reg, handlers } = loadApp();
  ok(reg.get('#app').innerHTML.indexOf('data-act="group-new-open"') < 0, '未进入分组管理时不显示「新建分组」');
  clickAct(handlers, 'manage-groups-toggle');
  ok(reg.get('#app').innerHTML.indexOf('data-act="group-new-open"') >= 0, '管理员分组管理模式显示「新建分组」');
  clickAct(handlers, 'group-new-open');
  ok(reg.get('#dlg').open === true, '新建分组对话框打开（复用既有实现）');
  inCtx(sandbox, 'qs("#dlg-input")');
  reg.get('#dlg-input').value = '';
  dlgClick(handlers, 1);
  ok(reg.get('#dlg').open === true && inCtx(sandbox, 'DB.groups.length') === 2, '空名称被拦（keep），未新增分组');
  reg.get('#dlg-input').value = '客户与支持';
  dlgClick(handlers, 1);
  ok(inCtx(sandbox, 'DB.groups.length') === 3 && inCtx(sandbox, 'DB.groups[2].name') === '客户与支持', '确认后新增分组并出现在页面');
  ok(reg.get('#dlg').open === false && reg.get('#app').innerHTML.indexOf('客户与支持') >= 0, '对话框关闭且新分组已渲染');
  inCtx(sandbox, 'state.role = "reader"; render();');
  ok(reg.get('#app').innerHTML.indexOf('data-act="group-new-open"') < 0 && reg.get('#app').innerHTML.indexOf('data-act="manage-groups-toggle"') < 0, '只读成员不可见新建分组与分组管理');
}

/* ══ 9. 手动文字：识别标记为「人工正文」，不得显示「未能识别」；闸门保留 ══ */
{
  const { sandbox, reg, handlers } = loadApp();
  clickAct(handlers, 'nav', { 'data-view': 'capture' });
  clickAct(handlers, 'c-add-text');
  const mid = inCtx(sandbox, 'work().items[0].id');
  inputAct(handlers, 'c-item-name', { 'data-id': mid }, '会议纪要模板');
  inputAct(handlers, 'c-item-text', { 'data-id': mid }, '复盘要点：每周五整理一次行动项。');
  ok(inCtx(sandbox, 'work().items[0].recognition') === 'manual', '手动文字识别标记为 manual');
  ok(inCtx(sandbox, 'itemStatus(work().items[0]).label') === '人工正文', 'itemStatus 显示「人工正文」而非「未能识别」');
  ok(inCtx(sandbox, 'work().items[0].paras[0].source') === '人工撰写', '人工出处保留（人工撰写）');
  clickAct(handlers, 'c-next');
  const html = reg.get('#app').innerHTML;
  ok(html.indexOf('人工正文') >= 0 && html.indexOf('未能识别') < 0, '第 2 步资料头不再显示「未能识别」');
  const p = inCtx(sandbox, 'work().items[0].paras[0].id');
  changeAct(handlers, 'c-para-check', { 'data-id': p }, { checked: false });
  ok(inCtx(sandbox, 'itemStatus(work().items[0]).label') === '人工正文 · 待核对 1 段', '取消核对后显示「人工正文 · 待核对 1 段」');
  ok(q(sandbox, reg, '#it-status-' + mid).textContent === '人工正文 · 待核对 1 段', '资料头标签实时同步（manual 状态）');
}
{
  /* 发布闸门保留：无名称/无正文的手动材料仍被拦 */
  const { sandbox, reg, handlers } = loadApp();
  clickAct(handlers, 'nav', { 'data-view': 'capture' });
  clickAct(handlers, 'c-add-text');
  clickAct(handlers, 'c-next');
  clickAct(handlers, 'c-next2');
  ok(reg.get('#app').innerHTML.indexOf('有手动材料还没有名称') >= 0, '手动材料缺名称仍在发布闸门列出');
  clickAct(handlers, 'c-publish');
  ok(inCtx(sandbox, 'work() !== null && work().step === 3'), '发布被拦（未提交，闸门未放宽）');
}

/* ══ 10. 单一资料输入卡片（默认文件 / 切换保留工作批次与模式 / 返回第 1 步保持、新流程重置）
   + 全部主题列表层级的字号作用域（本轮新增；其余断言原样保留） ══ */
{
  const path = require('path');
  const { sandbox, reg, handlers } = loadApp();
  const CSS = fs.readFileSync(path.join(__dirname, '..', 'style.css'), 'utf8');
  const IDX = fs.readFileSync(path.join(__dirname, '..', 'index.html'), 'utf8');

  clickAct(handlers, 'nav', { 'data-view': 'capture' });
  ok(inCtx(sandbox, 'work().inputMode') === 'file', '第 1 步默认输入方式 = 文件');
  const s1 = inCtx(sandbox, 'captureStep1HTML()');
  ok((s1.match(/data-mode-card="/g) || []).length === 3 && (s1.match(/data-mode-card="(url|text)" hidden/g) || []).length === 2 && s1.indexOf('data-mode-card="file" hidden') < 0, '单模式显示：只显示文件输入卡片（另外两种隐藏）');
  ok((s1.match(/aria-pressed="true"/g) || []).length === 1 && s1.indexOf('data-act="c-input-mode"') > s1.indexOf('data-mode-card="text"'), '三个切换按钮在卡片下方且恰有一个 pressed（文件 / 网页地址 / 手动文字）');

  /* 切换不丢工作批次（已添加列表），并就地更新显示（不整页重渲染） */
  clickAct(handlers, 'c-load-sample');
  const items0 = inCtx(sandbox, 'work().items.length');
  clickAct(handlers, 'c-input-mode', { 'data-mode': 'url' });
  ok(inCtx(sandbox, 'work().inputMode') === 'url' && inCtx(sandbox, 'work().items.length') === items0, '切换到网页地址：工作批次保留（' + items0 + ' 件）');
  ok(q(sandbox, reg, '[data-mode-card="url"]').hidden === false && q(sandbox, reg, '[data-mode-card="file"]').hidden === true, '切换后就地只显示所选卡片（无整页重渲染）');
  inCtx(sandbox, 'render();');
  const s2 = inCtx(sandbox, 'captureStep1HTML()');
  ok(s2.indexOf('data-mode-card="url" hidden') < 0 && s2.indexOf('data-mode-card="file" hidden') >= 0 && inCtx(sandbox, 'work().inputMode') === 'url', '重复 render 后模式保持（存在工作副本上）');

  /* 列表中的手动正文编辑不因切换丢失（内容随输入写入工作副本） */
  clickAct(handlers, 'c-input-mode', { 'data-mode': 'text' });
  clickAct(handlers, 'c-add-text');
  const mid2 = inCtx(sandbox, 'work().items[work().items.length-1].id');
  inputAct(handlers, 'c-item-name', { 'data-id': mid2 }, '会议纪要模板');
  inputAct(handlers, 'c-item-text', { 'data-id': mid2 }, '复盘要点：每周五整理一次行动项。');
  clickAct(handlers, 'c-input-mode', { 'data-mode': 'file' });
  ok(inCtx(sandbox, 'work().items[work().items.length-1].name') === '会议纪要模板' && inCtx(sandbox, 'work().items[work().items.length-1].paras[0].text').indexOf('每周五') >= 0, '切换不丢列表中的手动正文与名称');

  /* 返回第 1 步保持本次选择；新添加流程才重置为文件 */
  clickAct(handlers, 'c-input-mode', { 'data-mode': 'text' });
  clickAct(handlers, 'c-next');
  clickAct(handlers, 'c-back');
  ok(inCtx(sandbox, 'work().step') === 1 && inCtx(sandbox, 'work().inputMode') === 'text', '返回第 1 步保持本次选择（手动文字）');
  clickAct(handlers, 'nav', { 'data-view': 'capture' });   /* 有未保存内容 → 未保存对话框 */
  dlgClick(handlers, 1);                                   /* 放弃修改并离开 → 新添加流程 */
  ok(inCtx(sandbox, 'work() !== null && work().inputMode') === 'file' && inCtx(sandbox, 'work().items.length') === 0, '新添加流程重置默认文件（空批次）');

  /* 全部主题列表层级：字号 / 缩进只在 .topic-sec 作用域内；h1 仍最大；不越过到全部资料 / 主题详情 */
  const sz = (re) => Number((re.exec(CSS) || [])[1]) || 0;
  const sH1 = sz(/h1\{font-size:(\d+)px/);
  const sGroup = sz(/\.topic-sec-head h2\{[^}]*font-size:(\d+)px/);
  const sRow = sz(/\.topic-sec \.row-title strong\{font-size:(\d+)px/);
  const sMeta = sz(/\.topic-sec \.row-meta\{font-size:(\d+)px/);
  ok(sGroup === 18 && /\.topic-sec-head h2\{[^}]*font-weight:600/.test(CSS), '分组标题 18px/600（未分组章节同规则）');
  ok(sRow === 16 && /\.topic-sec \.row-title strong\{font-size:16px;font-weight:600\}/.test(CSS), '主题名 16px/600 且作用域限定 .topic-sec');
  ok(sMeta === 13 && /\.topic-sec \.row-meta\{font-size:13px;font-weight:400;color:var\(--text-3\)\}/.test(CSS), '说明 13px / 常规 / 较浅 且作用域限定 .topic-sec');
  ok(sH1 > sGroup && sGroup > sRow && sRow > sMeta, '层级：h1 ' + sH1 + ' > 分组 ' + sGroup + ' > 主题名 ' + sRow + ' > 说明 ' + sMeta);
  ok(/\.topic-sec \.rows \.row\{padding-left:30px\}/.test(CSS) && CSS.indexOf('.choose-grid') < 0, '主题行相对分组缩进约 20px（30px 内边距 − 10px 负外边距）；旧三栏同排样式已移除');
  const m600 = CSS.indexOf('@media(max-width:600px){');
  const m480 = CSS.indexOf('@media(max-width:480px){');
  ok(m600 >= 0 && m480 > m600 && CSS.slice(m600, m480).indexOf('.topic-sec .rows .row{padding-left:22px}') >= 0, '窄屏（≤600px，含 320/390）缩进收窄为 12px');
  ok(CSS.indexOf('.mode-card[hidden]{display:none}') >= 0, '单卡片样式：非当前方式 hidden 即不显示');
  ok(/\.row-title strong\{font-size:15px;font-weight:550;color:var\(--text-1\)/.test(CSS), '全局行规则保持原样（全部资料 / 主题详情不受影响）');
  ok(IDX.indexOf('style.css?v=20261012-overview-fold') >= 0 && IDX.indexOf('app.js?v=20261012-overview-fold') >= 0, 'index.html 资源版本 = 20261012-overview-fold');
}

/* ══ 11. 第九轮（2026-10-12）：总览内部主题 / 文件树；折叠不导航；唯一文件入口；两种返回语义 ══ */
{
  const path = require('path');
  const { sandbox, reg, handlers } = loadApp();
  /* 总览默认状态：树即显示、每主题下文件列表默认收起、文件行 = 名称 + 形式线稿图标 + title、空主题明确无资料 */
  const ov = reg.get('#app').innerHTML;
  ok(ov.indexOf('data-act="nav-file" data-id="m_spec"') >= 0 && ov.indexOf('title="会议设备A-产品规格书.pdf"') >= 0 && ov.indexOf('ts-file-name') >= 0, '总览树文件行：名称 + title（长名安全省略）');
  ok((ov.match(/data-act="nav-file" data-id="m_guide"/g) || []).length === 2 * inCtx(sandbox, 'materialById("m_guide").topics.length'), '同一资料关联多个主题：两套树下各主题分别列出（同一实体 id，不复制归档）');
  ok(inCtx(sandbox, 'DB.materials.filter(function(m){return m.id==="m_guide";}).length') === 1 && inCtx(sandbox, 'materialById("m_guide").topics.join(",")') === 't_meeting,t_onboarding', '多主题关联仍是同一资料实体（DB 多对多）');
  ok(ov.indexOf('id="nav-files-t_quote" inert') >= 0 && ov.indexOf('无关联资料') >= 0, '空主题明确无资料');
  ok(ov.indexOf('<use href="#i-image"/>') >= 0 && ov.indexOf('<use href="#i-video"/>') >= 0 && ov.indexOf('<use href="#i-globe"/>') >= 0 && ov.indexOf('<use href="#i-pen"/>') >= 0, '文件行含形式线稿图标（图片 / 视频 / 网页 / 手动正文）');

  /* 折叠按钮：只开合文件列表，不导航、不压返回层；两套树同步；跨渲染保持 */
  const rt0 = inCtx(sandbox, 'state.route.name');
  const st0 = inCtx(sandbox, 'state.backStack.length');
  clickAct(handlers, 'nav-topic-files', { 'data-id': 't_meeting' });
  ok(inCtx(sandbox, 'state.route.name') === rt0 && inCtx(sandbox, 'state.backStack.length') === st0, '折叠按钮只开合文件列表（不导航、不压返回层）');
  ok(q(sandbox, reg, '#nav-filesbtn-t_meeting').getAttribute('aria-expanded') === 'true' && q(sandbox, reg, '#nav-files-t_meeting').getAttribute('inert') === null && q(sandbox, reg, '#nav-files-t_meeting').classList.contains('open') === true, '展开同步：aria-expanded / inert / 动画类（桌面树）');
  ok(q(sandbox, reg, '#mnav-filesbtn-t_meeting').getAttribute('aria-expanded') === 'true' && q(sandbox, reg, '#mnav-files-t_meeting').getAttribute('inert') === null, '展开同步：窄屏树');
  ok(ov.indexOf('aria-controls="nav-files-t_meeting"') >= 0 && ov.indexOf('aria-controls="mnav-files-t_meeting"') >= 0, '折叠按钮 aria-controls 指向对应文件列表（两套树）');
  inCtx(sandbox, 'render();');
  ok(q(sandbox, reg, '#nav-filesbtn-t_meeting').getAttribute('aria-expanded') === 'true', '展开状态跨渲染保持（轻量 UI 状态）');
  clickAct(handlers, 'nav-topic-files', { 'data-id': 't_meeting' });
  ok(inCtx(sandbox, 'state.navTopicFiles.t_meeting') === false && q(sandbox, reg, '#nav-files-t_meeting').getAttribute('inert') === '' && q(sandbox, reg, '#mnav-files-t_meeting').getAttribute('inert') === '', '再次点击收起：状态与 inert 同步（两套树）');

  /* 总览树直开文件 → 返回实际总览（不假装曾进入主题正文） */
  clickAct(handlers, 'nav-file', { 'data-id': 'm_spec' });
  ok(inCtx(sandbox, 'state.route.name') === 'material' && inCtx(sandbox, 'state.route.id') === 'm_spec', '总览树文件列表直开资料');
  ok(inCtx(sandbox, 'state.backStack[state.backStack.length-1].name') === 'topics', '返回点 = 总览（未经过主题正文）');
  ok(reg.get('#app').innerHTML.indexOf('aria-label="主题浏览导航"') >= 0 && reg.get('#app').innerHTML.indexOf('ts-file active') >= 0, '资料详情保留浏览列 + 当前文件选中态');
  const stDup = inCtx(sandbox, 'state.backStack.length');
  clickAct(handlers, 'nav-file', { 'data-id': 'm_spec' });
  ok(inCtx(sandbox, 'state.route.id') === 'm_spec' && inCtx(sandbox, 'state.backStack.length') === stDup, '重复点击当前文件不压返回层');
  clickAct(handlers, 'd-back');
  ok(inCtx(sandbox, 'state.route.name') === 'topics', '返回实际总览（不是主题正文）');

  /* 主题树开文件 → 返回原主题（展开状态保持）→ 再返回总览；主题页唯一文件入口 */
  clickAct(handlers, 'nav-topic-files', { 'data-id': 't_meeting' });
  clickAct(handlers, 'nav-topic', { 'data-id': 't_meeting' });
  ok(inCtx(sandbox, 'state.route.name') === 'topic' && inCtx(sandbox, 'state.route.id') === 't_meeting', '从树进入主题正文');
  const tp = reg.get('#app').innerHTML;
  ok(tp.indexOf('data-act="m-open"') < 0 && tp.indexOf('data-act="nav-file"') >= 0, '主题页唯一文件入口 = 树文件列表（关联资料不重复按钮）');
  ok(tp.indexOf('从主题浏览列表打开原始文件') >= 0, '主题页提示指向主题浏览列表');
  ok(tp.indexOf('class="row-main row-info"') >= 0 && tp.indexOf('data-act="t-detach"') >= 0 && tp.indexOf('data-act="t-attach"') >= 0, '「关联资料」非点击信息 + 原关联移除 / 添加操作保留');
  clickAct(handlers, 'nav-file', { 'data-id': 'm_spec' });
  ok(inCtx(sandbox, 'state.backStack[state.backStack.length-1].name') === 'topic' && inCtx(sandbox, 'state.backStack[state.backStack.length-1].id') === 't_meeting', '返回上下文 = 原主题（backStack 顶层）');
  clickAct(handlers, 'd-back');
  ok(inCtx(sandbox, 'state.route.name') === 'topic' && inCtx(sandbox, 'state.route.id') === 't_meeting', '返回原主题');
  ok(q(sandbox, reg, '#nav-filesbtn-t_meeting').getAttribute('aria-expanded') === 'true', '文件列表展开状态跨阅读 / 返回保持');
  clickAct(handlers, 'd-back');
  ok(inCtx(sandbox, 'state.route.name') === 'topics', '再返回总览');

  /* 全部资料全集与经列表打开的资料不受影响（不扩展浏览列） */
  clickAct(handlers, 'nav', { 'data-view': 'materials' });
  ok(inCtx(sandbox, 'state.route.name') === 'materials' && reg.get('#app').innerHTML.indexOf('aria-label="主题浏览导航"') < 0, '全部资料页不扩展浏览列');
  ok(reg.get('#app').innerHTML.indexOf('data-act="m-open"') >= 0, '全部资料列表仍可打开资料（全集检索保留）');
  clickAct(handlers, 'm-open', { 'data-id': 'm_spec' });
  ok(inCtx(sandbox, 'state.route.name') === 'material' && reg.get('#app').innerHTML.indexOf('aria-label="主题浏览导航"') < 0, '经全部资料打开的资料详情不显示浏览列（不扩展）');
  clickAct(handlers, 'd-back');
  ok(inCtx(sandbox, 'state.route.name') === 'materials', '返回全部资料');

  /* 样式与资源版本（本轮直接相关） */
  const CSS = fs.readFileSync(path.join(__dirname, '..', 'style.css'), 'utf8');
  const IDX = fs.readFileSync(path.join(__dirname, '..', 'index.html'), 'utf8');
  ok(/\.ts-files\{display:grid;grid-template-rows:0fr/.test(CSS) && /\.ts-files\.open\{grid-template-rows:1fr\}/.test(CSS), '文件列表折叠样式（0fr → 1fr，与分组一致）');
  ok(/\.ts-file-name\{overflow:hidden;text-overflow:ellipsis;white-space:nowrap/.test(CSS) && CSS.indexOf('.ts-filesbtn[aria-expanded="true"] .ts-caret{transform:rotate(180deg)}') >= 0, '长名省略 + 折叠箭头旋转样式');
  ok(CSS.indexOf('@media (prefers-reduced-motion:reduce)') >= 0 && /\.ts-file\.active\{background:var\(--accent-soft\)/.test(CSS), '当前文件选中态样式 + reduced-motion 规则保留');
  ok(IDX.indexOf('style.css?v=20261012-overview-fold') >= 0, 'index.html 资源版本 = 20261012-overview-fold（避免缓存）');
}

/* ══ 12. 第十轮（2026-10-12）：中央总览分组折叠——默认展开 / 收起不导航 / 两侧独立 / 返回保持 / 搜索 / 管理按钮 ══ */
{
  const path = require('path');
  const { sandbox, reg, handlers } = loadApp();

  /* A. 默认展开：三个组头（产品与交付 / 团队与文化 / 未分组）为真实 button，aria-expanded=true，子区无 inert */
  const ov = reg.get('#app').innerHTML;
  ok((ov.match(/class="sec-fold"/g) || []).length === 3, '三个组头均为真实折叠按钮（产品与交付 / 团队与文化 / 未分组）');
  ok(ov.indexOf('id="ov-fold-btn-g_product" aria-expanded="true"') >= 0 && ov.indexOf('aria-controls="ov-body-g_product"') >= 0 && ov.indexOf('id="ov-fold-btn-g_team" aria-expanded="true"') >= 0 && ov.indexOf('id="ov-fold-btn-ungrouped" aria-expanded="true"') >= 0, '组头按钮 aria-expanded=true + aria-controls（三组一致）');
  ok(ov.indexOf('id="ov-body-g_product" inert') < 0 && ov.indexOf('id="ov-body-g_team" inert') < 0 && ov.indexOf('id="ov-body-ungrouped" inert') < 0, '初始展开：三个子区均无 inert（保持原视觉）');
  ok(ov.indexOf('class="sec-body open" id="ov-body-g_product"') >= 0 && ov.indexOf('class="sec-body open" id="ov-body-ungrouped"') >= 0, '子区默认带 open（0fr→1fr 动画展开态）');
  ok(ov.indexOf('data-act="t-open" data-id="t_meeting"') >= 0 && ov.indexOf('data-act="t-open" data-id="t_quote"') >= 0, '主题名仍为进入正文的按钮（分组行与未分组行均保留）');

  /* B. 收起不导航：只改折叠状态与 DOM，不动路由 / 返回栈 */
  const rt0 = inCtx(sandbox, 'state.route.name');
  const stk0 = inCtx(sandbox, 'state.backStack.length');
  clickAct(handlers, 'ov-fold', { 'data-gid': 'g_team' });
  ok(inCtx(sandbox, 'state.route.name') === rt0 && inCtx(sandbox, 'state.backStack.length') === stk0, '收起分组不导航、不压返回层');
  ok(q(sandbox, reg, '#ov-fold-btn-g_team').getAttribute('aria-expanded') === 'false' && q(sandbox, reg, '#ov-body-g_team').getAttribute('inert') === '' && q(sandbox, reg, '#ov-body-g_team').classList.contains('open') === false, '收起同步：aria-expanded / inert / 动画类（团队与文化）');

  /* C. 跨整页渲染保持（轻量 UI 状态）；未操作分组不受影响 */
  inCtx(sandbox, 'render();');
  const ovR = reg.get('#app').innerHTML;
  ok(ovR.indexOf('id="ov-fold-btn-g_team" aria-expanded="false"') >= 0 && ovR.indexOf('id="ov-body-g_team" inert') >= 0, '收起状态跨整页渲染保持');
  ok(ovR.indexOf('id="ov-fold-btn-g_product" aria-expanded="true"') >= 0 && ovR.indexOf('id="ov-fold-btn-ungrouped" aria-expanded="true"') >= 0, '未操作的分组保持展开');

  /* D. 两侧折叠状态独立：左侧树开合不联动中央，中央开合不联动左侧 */
  clickAct(handlers, 'nav-toggle-group', { 'data-gid': 'g_product' });
  ok(inCtx(sandbox, 'state.navGroups.g_product') === true && inCtx(sandbox, 'state.ovFolds.g_product') === undefined, '左侧树展开不改变中央折叠状态');
  inCtx(sandbox, 'render();');
  const ovD = reg.get('#app').innerHTML;
  ok(ovD.indexOf('data-gid="g_product" aria-expanded="true"') >= 0 && ovD.indexOf('id="ov-fold-btn-g_product" aria-expanded="true"') >= 0, '同屏：左侧展开 / 中央仍默认展开（互不联动）');
  clickAct(handlers, 'ov-fold', { 'data-gid': 'g_product' });
  ok(inCtx(sandbox, 'state.ovFolds.g_product') === false && inCtx(sandbox, 'state.navGroups.g_product') === true, '中央收起不影响左侧展开状态');
  inCtx(sandbox, 'render();');
  const ovD2 = reg.get('#app').innerHTML;
  ok(ovD2.indexOf('data-gid="g_product" aria-expanded="true"') >= 0 && ovD2.indexOf('id="ov-fold-btn-g_product" aria-expanded="false"') >= 0, '同屏：左侧展开 / 中央收起（两套状态各自保持）');

  /* E. 跨进入主题返回保持 */
  clickAct(handlers, 't-open', { 'data-id': 't_meeting' });
  ok(inCtx(sandbox, 'state.route.name') === 'topic', '进入主题正文');
  clickAct(handlers, 'd-back');
  ok(inCtx(sandbox, 'state.route.name') === 'topics', '返回总览');
  const ovE = reg.get('#app').innerHTML;
  ok(ovE.indexOf('id="ov-fold-btn-g_product" aria-expanded="false"') >= 0 && ovE.indexOf('id="ov-body-g_product" inert') >= 0, '返回后用户收起的分组仍收起');
  ok(ovE.indexOf('id="ov-fold-btn-g_team" aria-expanded="false"') >= 0 && ovE.indexOf('id="ov-fold-btn-ungrouped" aria-expanded="true"') >= 0, '返回后其他分组状态不变（团队与文化收起 / 未分组展开）');

  /* F. 搜索：命中组临时展开、不改用户状态；清空恢复原状态；命中结果不被折叠藏住 */
  const el2 = { id: 'search-q', value: '智能', closest: () => null };
  (handlers.input || []).forEach((fn) => fn({ target: el2 }));
  ok(inCtx(sandbox, 'state.topicQ') === '智能', '搜索输入进入状态（真实 input 派发）');
  const region = reg.get('#topics-region').innerHTML;
  ok(region.indexOf('id="ov-fold-btn-g_product" aria-expanded="true"') >= 0 && region.indexOf('id="ov-body-g_product" inert') < 0 && region.indexOf('ov-fold-btn-g_team') < 0, '搜索时命中组临时展开展示、无命中分组不渲染（折叠不会藏住命中）');
  ok(inCtx(sandbox, 'state.ovFolds.g_product') === false && inCtx(sandbox, 'state.ovFolds.g_team') === false, '搜索不改用户折叠状态');
  ok(q(sandbox, reg, '#search-clear').hidden === false, '有关键词时清除按钮出现');
  clickAct(handlers, 'search-clear');
  const region2 = reg.get('#topics-region').innerHTML;
  ok(inCtx(sandbox, 'state.topicQ') === '' && region2.indexOf('id="ov-fold-btn-g_product" aria-expanded="false"') >= 0 && region2.indexOf('id="ov-body-g_product" inert') >= 0, '清空搜索后恢复用户原折叠状态');
  ok(q(sandbox, reg, '#search-clear').hidden === true, '清空后清除按钮隐藏');

  /* G. 管理分组：改名 / 删除 / 移动仍可操作，且不嵌套在折叠按钮内 */
  clickAct(handlers, 'manage-groups-toggle');
  const mg = reg.get('#app').innerHTML;
  ok((mg.match(/data-act="group-rename"/g) || []).length === 2 && (mg.match(/data-act="group-delete"/g) || []).length === 2, '管理模式：两个分组的改名 / 删除按钮仍在组头');
  ok(mg.indexOf('data-act="group-new-open"') >= 0 && (mg.match(/data-act="topic-move"/g) || []).length >= 1, '管理模式：新建分组与主题移动控件保留');
  const iBtn = mg.indexOf('id="ov-fold-btn-g_product"');
  const iClose = mg.indexOf('</button>', iBtn);
  ok(iBtn >= 0 && iClose > iBtn && mg.slice(iBtn, iClose).indexOf('group-rename') < 0 && mg.indexOf('data-act="group-rename"') > iClose, '改名 / 删除位于折叠按钮之外（不嵌套 button）');
  clickAct(handlers, 'group-rename', { 'data-gid': 'g_product' });
  ok(reg.get('#dlg').open === true, '改名对话框仍可打开（管理操作保持）');
  dlgClick(handlers, 0);
  ok(reg.get('#dlg').open === false, '取消改名：对话框关闭（未改动分组）');
  clickAct(handlers, 'ov-fold', { 'data-gid': 'g_team' });
  ok(q(sandbox, reg, '#ov-fold-btn-g_team').getAttribute('aria-expanded') === 'true', '管理模式中分组折叠仍可操作（展开回调）');

  /* H. 样式与资源版本（本轮直接相关） */
  const CSS = fs.readFileSync(path.join(__dirname, '..', 'style.css'), 'utf8');
  const IDX = fs.readFileSync(path.join(__dirname, '..', 'index.html'), 'utf8');
  ok(/\.sec-body\{display:grid;grid-template-rows:0fr;transition:grid-template-rows \.2s ease\}/.test(CSS) && /\.sec-body\.open\{grid-template-rows:1fr\}/.test(CSS), '组区折叠样式（0fr → 1fr，与既有折叠一致）');
  ok(/\.sec-fold\{display:flex;align-items:center;gap:7px/.test(CSS) && CSS.indexOf('.sec-fold[aria-expanded="true"] .sec-caret{transform:rotate(180deg)}') >= 0, '组头按钮与箭头旋转样式');
  ok(CSS.indexOf('@media (prefers-reduced-motion:reduce)') >= 0 && CSS.indexOf('*{transition:none!important}') >= 0, 'reduced-motion 全局禁用保留（覆盖新开合动画）');
  ok(IDX.indexOf('style.css?v=20261012-overview-fold') >= 0 && IDX.indexOf('app.js?v=20261012-overview-fold') >= 0, 'index.html 资源版本 = 20261012-overview-fold（避免缓存）');
}

console.log(fails ? ('\n冒烟检查未通过：' + fails + ' 项') : '\n冒烟检查全部通过');
process.exit(fails ? 1 : 0);
