'use strict';
/* ════════════════════════════════════════════════════════════════
   企业知识管理 · 第二批设计评审原型逻辑（文件入库 + 灵活字段）
   纯前端演示：不读取文件内容、不上传、不调用模型、无网络请求；
   所有数据只存在于内存，刷新即重置。识别/发布结果由「演示场景」
   控件直接切换，不使用任何计时器冒充处理过程。
   ════════════════════════════════════════════════════════════════ */

/* ---------- 基础工具 ---------- */
const qs = (s, r) => (r || document).querySelector(s);
const qsa = (s, r) => Array.from((r || document).querySelectorAll(s));
const esc = (v) => String(v == null ? '' : v).replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const uid = (p) => (p || 'id') + '-' + Math.random().toString(36).slice(2, 8);
function todayStr() { const d = new Date(); return d.getFullYear() + '-' + String(d.getMonth() + 1).padStart(2, '0') + '-' + String(d.getDate()).padStart(2, '0'); }
/* 到期时间（可选，北京时间 = UTC+8 固定偏移；留空 = 长期有效）。
   旧式「只有日期」的值只在进入输入框/比较前补为当日 23:59（不修改模板的日期字段）；
   比较一律用精确时刻（换算为 UTC），不按天做词典式判断，也不把已写明的时刻延长到整天。 */
function expiryInputValue(v) { const s = String(v == null ? '' : v).trim(); return /^\d{4}-\d{2}-\d{2}$/.test(s) ? (s + 'T23:59') : s; }
function expiryInstant(v) {
  const m = /^(\d{4})-(\d{2})-(\d{2})(?:T(\d{2}):(\d{2}))?$/.exec(String(v == null ? '' : v).trim());
  if (!m) return null;
  const hh = (m[4] == null) ? 23 : Number(m[4]);
  const mm = (m[5] == null) ? 59 : Number(m[5]);
  return new Date(Date.UTC(Number(m[1]), Number(m[2]) - 1, Number(m[3]), hh - 8, mm));
}
function fmtExpiry(v) { const s = String(v == null ? '' : v).trim(); if (!s) return '长期有效'; return s.replace('T', ' ') + '（北京时间）'; }
const clone = (o) => JSON.parse(JSON.stringify(o));
const TYPE_LABEL = { text: '文字', number: '数字 + 单位', options: '选项', date: '日期', yesno: '是 / 否' };
const SHAPE_LABEL = { product_service: '商品 / 服务资料', article: '文章 / 制度说明' };
const CATEGORY_LIST = ['服务政策', '服务说明', '培训资料', '企业制度', '产品资料', '常见问题'];

/* ---------- 演示数据：知识模板（可自定义录入表，示例不是行业硬规则） ---------- */
const DB = {
  templates: [
    {
      id: 'tpl_service', name: '服务方案模板', shape: 'product_service', updated: '2026-10-09', usedCount: 1,
      versions: [
        { v: 1, date: '2026-08-12', note: '初始版本',
          fields: [
            { key: 'f_service_time', label: '服务时效', type: 'number', unit: '小时', required: true, help: '从确认到服务完成的最长时间', options: [] },
            { key: 'f_service_area', label: '服务区域', type: 'options', options: ['市区', '近郊', '全国'], required: false, help: '', unit: '' },
            { key: 'f_service_mode', label: '服务方式', type: 'text', required: false, help: '例如上门、寄修或远程支持', unit: '', options: [] }
          ] },
        { v: 2, date: '2026-10-09', note: '新增「服务保障期」；「服务区域」改为必填；移除「服务方式」',
          fields: [
            { key: 'f_service_time', label: '服务时效', type: 'number', unit: '小时', required: true, help: '从确认到服务完成的最长时间', options: [] },
            { key: 'f_service_area', label: '服务区域', type: 'options', options: ['市区', '近郊', '全国'], required: true, help: '', unit: '' },
            { key: 'f_warranty', label: '服务保障期', type: 'number', unit: '月', required: false, help: '服务完成后的保障时长', unitNote: '单位可留空；数字字段不做公式或自动换算', options: [] }
          ] }
      ]
    },
    {
      id: 'tpl_product', name: '商品资料模板', shape: 'product_service', updated: '2026-09-02', usedCount: 0,
      versions: [
        { v: 1, date: '2026-09-02', note: '初始版本',
          fields: [
            { key: 'f_weight', label: '商品重量', type: 'number', unit: 'kg', required: false, help: '含包装的净重', options: [] },
            { key: 'f_storage', label: '存放条件', type: 'text', required: false, help: '', unit: '', options: [] },
            { key: 'f_custom', label: '支持定制', type: 'yesno', required: false, help: '是否支持按客户要求定制', unit: '', options: [] }
          ] }
      ]
    },
    {
      id: 'tpl_policy', name: '企业制度模板', shape: 'article', updated: '2026-06-18', usedCount: 2,
      versions: [
        { v: 1, date: '2026-06-18', note: '初始版本',
          fields: [
            { key: 'f_issue_date', label: '制度发布日期', type: 'date', required: true, help: '', unit: '', options: [] },
            { key: 'f_scope', label: '适用范围', type: 'options', options: ['全体员工', '部门负责人', '新入职员工'], required: false, help: '', unit: '' }
          ] }
      ]
    }
  ],
  entries: [],
  importRecords: [
    { id: 'ir_scan', file: '产品手册-扫描件.pdf', kind: '扫描版 PDF', progress: '无法识别', reason: '无文本层，无法自动提取文字；可人工整理正文', at: '2026-10-08 15:20' }
  ]
};

/* ---------- 演示数据：知识条目 ---------- */
function seedEntries() {
  DB.entries = [
    {
      id: 'k_policy', home: 'file', title: '客户服务政策', category: '服务政策',
      lifecycle: 'published', usage: 'external', aiOn: true, expiry: '2027-06-30T23:59', aiIssue: '',
      templateId: 'tpl_policy', templateVersion: 1, updated: '2026-05-20', currentVersion: 2, draft: null,
      source: { file: '客户服务政策-2025.pdf', kind: 'PDF · 文本可复制', pages: 2, at: '2026-05-20 11:02' },
      fields: [
        { key: 'f_issue_date', label: '制度发布日期', type: 'date', unit: '', options: [], value: '2026-05-20', checked: true, aiAllowed: true, sourceKind: '原文', page: 1, modified: false },
        { key: 'f_scope', label: '适用范围', type: 'options', unit: '', options: ['全体员工', '部门负责人', '新入职员工'], value: '全体员工', checked: true, aiAllowed: true, sourceKind: '原文', page: 1, modified: false }
      ],
      paragraphs: [
        { id: 'kp1', text: '客户服务政策（2025 修订版）：适用于所有自助下单客户。', included: true, checked: true, internal: false, needsReview: false, page: 1, modified: false },
        { id: 'kp2', text: '一般咨询在 4 小时内响应；上门服务在确认后 8 小时内完成。', included: true, checked: true, internal: false, needsReview: false, page: 1, modified: false },
        { id: 'kp3', text: '服务费用以工单确认为准；超出服务范围的费用需提前告知客户。', included: true, checked: true, internal: false, needsReview: false, page: 1, modified: true },
        { id: 'kp4', text: '服务期满后可续约：在到期前 30 天联系客户经理办理。', included: true, checked: true, internal: false, needsReview: false, page: 2, modified: false }
      ],
      versions: [
        { v: 1, date: '2025-03-11', note: '首次导入（登记原件）' },
        { v: 2, date: '2026-05-20', note: '替换文件：正文更新；费用段落为人工修改' }
      ]
    },
    {
      id: 'k_training', home: 'file', title: '新员工培训资料（内部）', category: '培训资料',
      lifecycle: 'draft', usage: 'internal', aiOn: false, expiry: '', aiIssue: '',
      templateId: 'tpl_policy', templateVersion: 1, updated: '2026-10-02', currentVersion: 0, draft: null,
      source: { file: '新员工培训-讲义.docx', kind: 'DOCX · 识别成功', pages: 1, locKind: 'para', at: '2026-10-02 09:41' },
      fields: [],
      paragraphs: [
        { id: 'kt1', text: '入职第一周：完成账号开通、设备领取与安全培训。', included: true, checked: false, internal: false, needsReview: false, loc: 1, modified: false },
        { id: 'kt2', text: '工作时间：周一至周五 9:30–18:30，午休 12:30–13:30。', included: true, checked: false, internal: false, needsReview: false, loc: 2, modified: false },
        { id: 'kt3', text: '以下内容仅供内部使用，请勿对外提供。', included: false, checked: false, internal: true, needsReview: false, loc: 3, modified: false }
      ],
      versions: []
    },
    {
      id: 'k_install', home: 'product_service', title: '上门安装服务方案', category: '服务说明',
      lifecycle: 'published', usage: 'external', aiOn: true, expiry: '', aiIssue: '',
      templateId: 'tpl_service', templateVersion: 1, updated: '2026-08-15', currentVersion: 1, draft: null,
      source: { file: '上门安装服务方案.pdf', kind: 'PDF · 文本可复制', pages: 1, at: '2026-08-15 14:20' },
      fields: [
        { key: 'f_service_time', label: '服务时效', type: 'number', unit: '小时', options: [], value: '4', checked: true, aiAllowed: true, sourceKind: '原文', page: 1, modified: false },
        { key: 'f_service_area', label: '服务区域', type: 'options', options: ['市区', '近郊', '全国'], value: '市区', checked: true, aiAllowed: true, sourceKind: '原文', page: 1, modified: false },
        { key: 'f_service_mode', label: '服务方式', type: 'text', options: [], unit: '', value: '上门安装（市区与近郊）', checked: true, aiAllowed: false, sourceKind: '原文', page: 1, modified: false }
      ],
      paragraphs: [
        { id: 'ki1', text: '上门安装服务：确认预约后 4 小时内完成（市区与近郊）。', included: true, checked: true, internal: false, needsReview: false, page: 1, modified: false },
        { id: 'ki2', text: '标准上门安装不额外收费；如需更换配件，按配件清单单独计费。', included: true, checked: true, internal: false, needsReview: false, page: 1, modified: false },
        { id: 'ki3', text: '内部成本与结算口径见财务手册，不对外提供。', included: false, checked: false, internal: true, needsReview: false, page: 1, modified: false }
      ],
      versions: [{ v: 1, date: '2026-08-15', note: '首次导入' }]
    },
    {
      id: 'k_faq', home: 'article', title: '退换货说明', category: '常见问题',
      lifecycle: 'published', usage: 'external', aiOn: false, expiry: '', aiIssue: '',
      templateId: null, templateVersion: 0, updated: '2026-07-03', currentVersion: 1, draft: null,
      source: { file: '', kind: '手动创建', pages: 0, at: '2026-07-03 10:10' },
      fields: [],
      paragraphs: [
        { id: 'kf1', text: '自签收之日起 7 天内，未使用的商品可申请无理由退换。', included: true, checked: true, internal: false, needsReview: false, page: 1, modified: false },
        { id: 'kf2', text: '定制类商品不支持无理由退换；质量问题按售后流程处理。', included: true, checked: true, internal: false, needsReview: false, page: 1, modified: false }
      ],
      versions: [{ v: 1, date: '2026-07-03', note: '手动创建' }]
    }
  ];
  // 为每个条目的最新版本生成发布快照（用于「恢复为草稿」演示）
  DB.entries.forEach((e) => {
    const latest = e.versions[e.versions.length - 1];
    if (latest) latest.snapshot = makeSnapshot(e);
  });
  // 客户服务政策 v1 的历史快照（演示更早版本内容）
  const kp = byId('k_policy');
  if (kp && kp.versions[0]) {
    kp.versions[0].snapshot = {
      title: '客户服务政策', usage: 'external', aiOn: true,
      fields: [
        { key: 'f_issue_date', label: '制度发布日期', type: 'date', unit: '', options: [], value: '2025-03-11', checked: true, aiAllowed: true, sourceKind: '原文', page: 1, modified: false },
        { key: 'f_scope', label: '适用范围', type: 'options', unit: '', options: ['全体员工', '部门负责人', '新入职员工'], value: '全体员工', checked: true, aiAllowed: true, sourceKind: '原文', page: 1, modified: false }
      ],
      paragraphs: [
        { id: 'kpv1a', text: '客户服务政策：本政策适用于全部门店与服务点。', included: true, checked: true, internal: false, needsReview: false, page: 1, modified: false },
        { id: 'kpv1b', text: '一般咨询在 24 小时内响应；上门服务需提前预约。', included: true, checked: true, internal: false, needsReview: false, page: 1, modified: false }
      ]
    };
  }
}

/* ---------- 演示样例：识别结果由演示场景直接给出（无计时器） ---------- */
const PDF_SAMPLE = {
  file: '客户服务政策-2026.pdf', kind: 'PDF · 文本可复制', pages: 2, locKind: 'page',
  paras: [
    { id: 'd1', text: '客户服务政策（2026 版）', loc: 1, internal: false, needsReview: false },
    { id: 'd2', text: '一般咨询在 2 小时内响应；上门服务在确认后 4 小时内完成，市区与近郊均可安排。', loc: 1, internal: false, needsReview: false },
    { id: 'd3', text: '服务费用：标准上门服务不额外收取上门费；如需更换配件，按配件清单单独计费。', loc: 1, internal: false, needsReview: true, reviewNote: '包含费用表述，请人工确认对外口径' },
    { id: 'd4', text: '内部备注：上门服务的成本按工时核算，报价下限为内部信息，不得对外提供。', loc: 1, internal: true, needsReview: false },
    { id: 'd5', text: '服务期满后的续约：可在到期前 30 天联系客户经理办理，续约后服务时效保持不变。', loc: 2, internal: false, needsReview: false },
    { id: 'd6', text: '附表：服务区域与响应时间｜市区 2 小时｜近郊 4 小时（识别结果可能包含表格断行，请人工确认）', loc: 2, internal: false, needsReview: true, reviewNote: '可能包含表格断行，请人工确认' }
  ]
};
const DOCX_SAMPLE = {
  file: '新员工培训-讲义.docx', kind: 'DOCX · 识别成功', pages: 1, locKind: 'para',
  paras: [
    { id: 'x1', text: '新员工培训讲义（内部）', loc: 1, internal: false, needsReview: false },
    { id: 'x2', text: '入职第一周：完成账号开通、设备领取与安全培训。', loc: 2, internal: false, needsReview: false },
    { id: 'x3', text: '工作时间：周一至周五 9:30–18:30，午休 12:30–13:30。', loc: 3, internal: false, needsReview: false },
    { id: 'x4', text: '以下内容仅供内部使用，请勿对外提供。', loc: 4, internal: true, needsReview: false }
  ]
};

/* 导入演示场景：结果由评审控件直接切换（不以倒计时冒充处理） */
const IMPORT_SCENARIOS = {
  ok: { label: '文字型 PDF · 识别成功', note: '用于演示正常识别流程（下方两个演示样例按钮均可用）' },
  scan: { label: '扫描版 PDF · 无文本层', file: '产品手册-扫描件.pdf', kind: '扫描版 PDF', reason: '没有识别到可编辑文字：扫描版 PDF 没有文本层。可阅读原件并人工整理正文（自动识别 OCR 属后续可选方向，本方案不承诺）。' },
  doc: { label: '旧版 DOC（.doc）· 需人工整理', file: '旧版说明.doc', kind: '旧版 DOC', reason: '旧版 .doc 文件暂不支持自动识别；可人工整理正文，或另存为 .docx 后重新导入。' },
  enc: { label: '加密 PDF · 无法打开', file: '受保护文档.pdf', kind: '加密 PDF', reason: '文件已加密，无法打开读取文字。请使用未加密的文件；原件登记可保留（不会要求提供密码）。' },
  corrupt: { label: '损坏或不支持的文件 · 无法识别', file: '损坏文件.pdf', kind: '损坏或不支持', reason: '文件无法解析：可能已损坏或格式不受支持。请重新选择文件，或改为人工整理正文。' }
};

/* ---------- 状态 ---------- */
const state = {
  route: { name: 'list', view: 'all', id: null, tab: 'content' },
  role: 'admin',
  navExpanded: false,
  filters: { q: '', type: '', status: '' },
  work: null,          // 核对/导入的工作副本
  workDirty: false,
  builder: null,       // 模板编辑器工作副本
  builderDirty: false,
  pendingNav: null,    // 未保存对话框要去的目标
  dlgActions: [],
  lastFocus: null
};

/* ---------- 读取与计算 ---------- */
function byId(id) { return DB.entries.find((e) => e.id === id); }
function templateById(id) { return DB.templates.find((t) => t.id === id); }
function latestVersion(tpl) { return tpl.versions[tpl.versions.length - 1]; }
function versionOf(tpl, v) { return tpl.versions.find((x) => x.v === v) || null; }
function makeSnapshot(e) {
  return clone({ title: e.title, usage: e.usage, aiOn: e.aiOn, fields: e.fields, paragraphs: e.paragraphs });
}
function availability(e) {
  if (e.lifecycle === 'disabled') return { pill: 'off', label: '已停用', reason: '已停用：不对外展示、不用于 AI 客服。' };
  if (e.lifecycle === 'draft') return { pill: 'draft', label: '草稿', reason: '草稿：未发布，不会用于 AI 客服。' };
  if (!e.aiOn) return { pill: 'pub', label: '已发布（AI 客服不可用）', reason: '未授权用于 AI 客服（可在编辑中开启）。' };
  if (e.aiIssue) return { pill: 'pub', label: '已发布（AI 客服不可用）', reason: e.aiIssue };
  const exp = expiryInstant(e.expiry);
  if (exp && Date.now() >= exp.getTime()) return { pill: 'pub', label: '已发布（AI 客服不可用）', reason: '已到到期时间（按北京时间精确判断）：不再用于 AI 客服回答；成员阅读与历史不受影响。' };
  return { pill: 'ok', label: '已发布（AI 客服可用）', reason: '' };
}
const HOME_LABEL = { file: '文件资料', product_service: '商品 / 服务资料', article: '文章 / FAQ' };
function typeTag(e) { return '<span class="tag type">' + esc(HOME_LABEL[e.home] || '文件资料') + '</span>'; }

/* ---------- 侧栏导航 ---------- */
const LIST_TITLES = { all: '全部知识', product_service: '产品与服务', article: '文章与 FAQ', file: '文件资料' };
const LIST_LEAD = {
  all: '企业内所有知识都由这里进入；「全部知识」保留通用新增，各分类视图给出上下文新增入口。',
  product_service: '产品与服务类知识：正文之外可携带模板业务字段（字段是录入方式，不代表新的权限）。',
  article: '文章与 FAQ 类知识：正文与问答结构保留，额外字段不替换原有正文。',
  file: '文件资料：文件识别结果、导入记录与已生成的知识都集中在这里；知识状态与导入任务进度分开显示。'
};
function subActive(view) { const r = state.route; return (r.name === 'list' && r.view === view); }
function sideNavHTML() {
  const r = state.route;
  const expanded = state.navExpanded;
  const allActive = r.name === 'list' && r.view === 'all';
  const tplActive = r.name === 'templates' || r.name === 'builder';
  const sub = (view, label, icon) => '<button class="side-link sub' + (subActive(view) ? ' active' : '') + '" data-act="nav" data-view="' + view + '"' + (subActive(view) ? ' aria-current="page"' : '') + '>' +
    '<svg class="ic"><use href="#i-' + icon + '"/></svg>' + label + '</button>';
  const mgr = state.role === 'admin';
  let html = '';
  html += '<button class="side-link" data-act="nav" data-view="__out"><svg class="ic"><use href="#i-home"/></svg>工作台</button>';
  html += '<button class="side-link" data-act="nav" data-view="__out"><svg class="ic"><use href="#i-inbox"/></svg>收件箱</button>';
  html += '<div class="side-heading">企业知识</div>';
  html += '<div class="nav-main-row">' +
    '<button class="side-link' + (allActive ? ' active' : '') + '" data-act="nav" data-view="all"' + (allActive ? ' aria-current="page"' : '') + '><svg class="ic"><use href="#i-book"/></svg>全部知识</button>' +
    '<button class="side-toggle" type="button" data-act="nav-toggle" aria-controls="nav-knowledge-subs" aria-expanded="' + (expanded ? 'true' : 'false') + '" aria-label="' + (expanded ? '收起知识分类' : '展开知识分类') + '"><svg class="ic"><use href="#i-chev"/></svg></button>' +
    '</div>';
  html += '<div class="nav-subs' + (expanded ? ' open' : '') + '" id="nav-knowledge-subs"' + (expanded ? '' : ' inert aria-hidden="true"') + '>' +
    '<div class="nav-subs-inner">' +
    sub('product_service', '产品与服务', 'box') +
    sub('article', '文章与 FAQ', 'article') +
    sub('file', '文件资料', 'file') +
    '</div></div>';
  /* 「知识模板」是管理入口（沿用商品/服务管理的权限），不是第四种知识分类；只读成员不显示 */
  if (mgr) html += '<button class="side-link' + (tplActive ? ' active' : '') + '" data-act="nav" data-view="__templates"' + (tplActive ? ' aria-current="page"' : '') + '><svg class="ic"><use href="#i-layout"/></svg>知识模板</button>';
  html += '<button class="side-link" data-act="nav" data-view="__qa"><svg class="ic"><use href="#i-globe"/></svg>模拟客户问答</button>';
  html += '<div class="side-heading">设置</div>';
  html += '<button class="side-link" data-act="nav" data-view="__out"><svg class="ic"><use href="#i-sliders"/></svg>渠道账号</button>';
  html += '<button class="side-link" data-act="nav" data-view="__out"><svg class="ic"><use href="#i-sliders"/></svg>设置</button>';
  return html;
}
function syncNavExpandedDOM() {
  const toggle = qs('[data-act="nav-toggle"]');
  const subs = qs('#nav-knowledge-subs');
  if (!toggle || !subs) return;
  const open = state.navExpanded;
  subs.classList.toggle('open', open);
  if (open) { subs.removeAttribute('inert'); subs.removeAttribute('aria-hidden'); }
  else { subs.setAttribute('inert', ''); subs.setAttribute('aria-hidden', 'true'); }
  toggle.setAttribute('aria-expanded', open ? 'true' : 'false');
  toggle.setAttribute('aria-label', open ? '收起知识分类' : '展开知识分类');
}
function crumbHTML() {
  const r = state.route;
  const link = '<b>';
  if (r.name === 'list') return '企业知识 / ' + link + esc(LIST_TITLES[r.view] || '全部知识') + '</b>';
  if (r.name === 'detail') { const e = byId(r.id); const view = e ? e.home : 'file'; return '企业知识 / ' + esc(LIST_TITLES[view] || '全部知识') + ' / ' + (e ? '<b>' + esc(e.title) + '</b>' : ''); }
  if (r.name === 'review') {
    const mode = state.work ? state.work.mode : 'import';
    const t = mode === 'import' ? '上传文件' : mode === 'replace' ? '更新文件' : mode === 'manual' ? '手动创建' : '核对内容';
    return '企业知识 / ' + link + esc(t) + '</b>';
  }
  if (r.name === 'templates') return '企业知识 / ' + link + '知识模板</b>';
  if (r.name === 'builder') return '企业知识 / 知识模板 / <b>' + esc(state.builder ? (state.builder.name || '新模板') : '新模板') + '</b>';
  if (r.name === 'qa') return '模拟客户问答';
  if (r.name === 'denied') return '企业知识';
  return '企业知识';
}

/* ---------- 路由渲染 ---------- */
function render() {
  qs('#side-nav').innerHTML = sideNavHTML();
  const r = state.route;
  let html = '';
  if (r.name === 'list') html = listHTML();
  else if (r.name === 'detail') html = detailExists(r.id) ? detailHTML(r.id) : missingHTML();
  else if (r.name === 'review') html = state.role === 'admin' ? (state.work ? reviewHTML() : missingHTML()) : deniedHTML();
  else if (r.name === 'templates') html = state.role === 'admin' ? templatesHTML() : deniedHTML();
  else if (r.name === 'builder') html = state.role === 'admin' && state.builder ? builderHTML() : deniedHTML();
  else if (r.name === 'qa') html = qaHTML();
  else if (r.name === 'denied') html = deniedHTML();
  else html = missingHTML();
  qs('#app').innerHTML = html;
  qs('#crumb').innerHTML = crumbHTML();
  syncNavExpandedDOM();
}
function detailExists(id) { return !!byId(id); }
function missingHTML() {
  return '<div class="empty"><h2>页面不存在（演示）</h2><p>这条演示数据可能已被移除或重置。刷新页面可以恢复初始演示数据。</p><p><button class="button secondary" data-act="nav" data-view="all">返回全部知识</button></p></div>';
}
function deniedHTML() {
  return '<div class="empty"><h2>没有管理模板的权限</h2><p>当前是「仅阅读成员」演示视角：可以阅读全部知识（含仅内部资料），但没有新增、编辑、发布或模板管理入口。真实权限由后端强制，本原型只演示界面差异。</p><p><button class="button secondary" data-act="nav" data-view="all">返回全部知识</button></p></div>';
}
function qaHTML() {
  return '<div class="empty"><h2>模拟客户问答不在第二批范围</h2><p>问答测试沿用第一批评审设计（见 enterprise-knowledge-prototype），本原型不重复实现；第二批只评审文件入库与灵活字段。</p><p><button class="button secondary" data-act="nav" data-view="file">去文件资料</button></p></div>';
}

/* ---------- 列表页 ---------- */
function entryMatchesView(e, view) { return view === 'all' ? true : e.home === view; }
function entryMatchesFilters(e) {
  const f = state.filters;
  if (f.type && e.home !== f.type) return false;
  if (f.status && e.lifecycle !== f.status) return false;
  if (f.q) {
    const q = f.q.trim().toLowerCase();
    const hay = (e.title + ' ' + e.category + ' ' + e.paragraphs.map((p) => p.text).join(' ')).toLowerCase();
    if (hay.indexOf(q) < 0) return false;
  }
  return true;
}
function visibleEntries() { return DB.entries.filter((e) => entryMatchesView(e, state.route.view) && entryMatchesFilters(e)); }
function visibleRecords() {
  const f = state.filters;
  if (f.status || (f.type && f.type !== 'file')) return [];
  const q = f.q.trim().toLowerCase();
  return DB.importRecords.filter((rec) => !q || rec.file.toLowerCase().indexOf(q) >= 0);
}
function listHTML() {
  const r = state.route;
  return '<div class="toolbar"><div><h1>' + esc(LIST_TITLES[r.view] || '全部知识') + '</h1><p class="lead">' + esc(LIST_LEAD[r.view] || '') + '</p></div><div class="actions">' + addHTML(r.view) + '</div></div>'
    + listToolbarHTML() + '<div id="list-region">' + listRegionHTML() + '</div>';
}
function addHTML(view) {
  if (state.role !== 'admin') return '';
  if (view === 'file') return '<button class="button" data-act="open-import"><svg class="ic"><use href="#i-upload"/></svg>上传文件</button>';
  if (view === 'product_service') return '<button class="button" data-act="open-manual" data-shape="product_service"><svg class="ic"><use href="#i-plus"/></svg>添加产品 / 服务</button>';
  if (view === 'article') return '<button class="button" data-act="open-manual" data-shape="article"><svg class="ic"><use href="#i-plus"/></svg>添加文章 / FAQ</button>';
  return '<details class="add-wrap"><summary class="button"><svg class="ic"><use href="#i-plus"/></svg>添加知识</summary><div class="add-menu">' +
    '<button data-act="open-import"><svg class="ic"><use href="#i-upload"/></svg><span>上传文件<small>先选模板，识别后逐段核对</small></span></button>' +
    '<button data-act="open-manual"><svg class="ic"><use href="#i-plus"/></svg><span>手动创建<small>同样先选择知识模板</small></span></button>' +
    '</div></details>';
}
function listToolbarHTML() {
  const f = state.filters;
  return '<div class="list-toolbar">' +
    '<div class="search-wrap"><svg class="ic"><use href="#i-search"/></svg>' +
    '<input id="search-q" type="search" placeholder="搜索标题与正文（不使用 AI）" value="' + esc(f.q) + '" aria-label="搜索知识">' +
    '<button class="search-clear" id="search-clear" type="button" data-act="search-clear" aria-label="清空搜索关键词" ' + (f.q ? '' : 'hidden') + '><svg class="ic"><use href="#i-x"/></svg></button></div>' +
    '<select class="filter-select" id="filter-type" data-act="filter-type" aria-label="按知识形态筛选">' +
    '<option value=""' + (f.type === '' ? ' selected' : '') + '>全部形态</option>' +
    '<option value="file"' + (f.type === 'file' ? ' selected' : '') + '>文件资料</option>' +
    '<option value="product_service"' + (f.type === 'product_service' ? ' selected' : '') + '>产品与服务</option>' +
    '<option value="article"' + (f.type === 'article' ? ' selected' : '') + '>文章与 FAQ</option>' +
    '</select>' +
    '<select class="filter-select" id="filter-status" data-act="filter-status" aria-label="按状态筛选">' +
    '<option value=""' + (f.status === '' ? ' selected' : '') + '>全部状态</option>' +
    '<option value="draft"' + (f.status === 'draft' ? ' selected' : '') + '>草稿</option>' +
    '<option value="published"' + (f.status === 'published' ? ' selected' : '') + '>已发布</option>' +
    '<option value="disabled"' + (f.status === 'disabled' ? ' selected' : '') + '>已停用</option>' +
    '</select></div>' +
    '<p class="filter-note">清空搜索的「×」只清关键词，形态与状态筛选会保留。全文搜索按标题与正文文字匹配，不使用 AI。</p>';
}
function listRegionHTML() {
  const list = visibleEntries();
  const records = visibleRecords();
  let html = '';
  if (!list.length) {
    html += '<div class="empty"><h2>' + (state.filters.q || state.filters.status || state.filters.type ? '没有符合筛选的知识' : '这个分类还没有知识') + '</h2><p>' + (state.filters.q ? '换个关键词，或清空搜索（清空只影响关键词，筛选保留）。' : '可以上传文件，或手动创建一条。') + '</p></div>';
  } else {
    html += '<div class="rows">' + list.map(rowHTML).join('') + '</div>';
  }
  html += '<p class="list-count">当前显示 ' + list.length + ' 条知识' + (records.length ? ' · 另有 ' + records.length + ' 条导入记录' : '') + '</p>';
  if (records.length) {
    html += '<div class="rec-box"><h2>导入记录（任务进度，不是知识状态）</h2>' + records.map(recRowHTML).join('') + '</div>';
  }
  return html;
}
function rowHTML(e) {
  const av = availability(e);
  const tpl = e.templateId ? templateById(e.templateId) : null;
  const tplText = tpl ? '模板：' + tpl.name + ' v' + e.templateVersion : '未套模板';
  return '<button class="row" data-act="row-open" data-id="' + e.id + '">' +
    '<span class="row-main"><span class="row-title"><strong>' + esc(e.title) + '</strong>' + typeTag(e) + '</span>' +
    '<span class="row-meta"><span>' + esc(e.category) + '</span><span>' + esc(tplText) + '</span><span>更新 ' + esc(e.updated) + '</span>' + (e.lifecycle === 'published' && e.aiOn && e.aiIssue ? '<span>处理失败：可重试</span>' : '') + '</span></span>' +
    '<span class="row-side"><span class="pill ' + av.pill + '">' + esc(av.label) + '</span>' + (e.draft ? '<span class="row-note">待发布草稿：' + esc(e.draft.label) + '</span>' : '') + '<svg class="ic row-chev"><use href="#i-chev"/></svg></span>' +
    '</button>';
}
function recRowHTML(rec) {
  const actions = state.role === 'admin'
    ? '<div class="actions"><button class="button secondary" data-act="rec-manual" data-id="' + rec.id + '">继续手动整理</button><button class="button ghost" data-act="rec-remove" data-id="' + rec.id + '">移除记录</button></div>'
    : '';
  return '<div class="rec-row"><div><strong>' + esc(rec.file) + '</strong>' +
    '<small>' + esc(rec.kind) + ' · ' + esc(rec.progress) + '：' + esc(rec.reason) + ' · ' + esc(rec.at) + '</small></div>' + actions + '</div>';
}
function refreshListRegion() {
  const region = qs('#list-region');
  if (region) region.innerHTML = listRegionHTML();
}

/* ---------- 核对向导：工作副本 ---------- */
function work() { return state.work; }
function markWorkDirty() { state.workDirty = true; }
function locOf(p) { return (p.loc != null ? p.loc : (p.page != null ? p.page : null)); }
function locLabel(w, n) {
  if (n == null) return '手动补充';
  return (w && w.locKind === 'para') ? ('第 ' + n + ' 段') : ('第 ' + n + ' 页');
}
function newWork(mode, opts) {
  opts = opts || {};
  return {
    mode: mode,
    home: opts.home || 'file',
    entryId: opts.entryId || null,
    step: 1,
    pending: opts.pending || null,
    templateId: ('templateId' in opts) ? opts.templateId : undefined,
    templateDecided: ('templateDecided' in opts) ? opts.templateDecided : false,
    templateVersion: opts.templateVersion || 0,
    scenario: 'ok',
    fromRecord: opts.fromRecord || null,
    title: opts.title || '', category: opts.category || '服务政策', usage: opts.usage || 'internal', aiOn: !!opts.aiOn, expiry: opts.expiry || '',
    source: null, fallback: null, pages: [], locKind: 'page', curPage: 1,
    paragraphs: [], fields: [], historicFields: [], compare: null
  };
}
function buildFieldInstances(templateId, version) {
  if (!templateId) return [];
  const tpl = templateById(templateId);
  const v = versionOf(tpl, version) || latestVersion(tpl);
  return v.fields.map((f) => ({ key: f.key, label: f.label, type: f.type, unit: f.unit || '', options: (f.options || []).slice(), required: !!f.required, help: f.help || '', value: '', checked: false, aiAllowed: false, sourceKind: '', loc: null, modified: false, historic: false }));
}
function parasFromSample(sample) {
  return sample.paras.map((p) => ({ id: p.id, text: p.text, loc: p.loc, included: !p.internal, checked: false, internal: !!p.internal, needsReview: !!p.needsReview, reviewNote: p.reviewNote || '', modified: false }));
}
function pagesFromParas(paras) {
  const by = {};
  paras.forEach((p) => { const n = locOf(p) || 1; (by[n] = by[n] || []).push(p.id); });
  return Object.keys(by).sort((a, b) => a - b).map((n) => ({ n: Number(n), ids: by[n] }));
}
function stripExt(name) { return String(name || '').replace(/\.[a-z0-9]+$/i, ''); }
function defaultCategoryFor(templateId) {
  if (templateId === 'tpl_service') return '服务说明';
  if (templateId === 'tpl_product') return '产品资料';
  if (templateId === 'tpl_policy') return '企业制度';
  return '服务政策';
}
function beginCheck() {
  const w = work();
  if (w.mode === 'import') {
    const sc = IMPORT_SCENARIOS[w.scenario];
    if (sc.label.indexOf('识别成功') >= 0) {
      const sample = (w.pending && w.pending.sample === 'docx') ? DOCX_SAMPLE : ((w.pending && w.pending.sample === 'pdf') ? PDF_SAMPLE : null);
      if (sample) {
        w.source = { file: sample.file, kind: sample.kind, pages: sample.pages, locKind: sample.locKind, at: '' };
        w.locKind = sample.locKind;
        w.paragraphs = parasFromSample(sample);
        w.pages = pagesFromParas(w.paragraphs);
        if (!w.title.trim()) w.title = stripExt(sample.file);
      } else {
        w.source = { file: (w.pending && w.pending.file) || '未命名文件', kind: '本地文件（演示未读取）', pages: 0, locKind: 'page', at: '' };
        w.fallback = { reason: '演示环境不读取本地文件的内容，因此不会显示识别结果；真实系统会在这里展示识别出的正文。可以点「演示样例：PDF / DOCX」体验完整核对流程，或直接手动添加正文。' };
        w.paragraphs = []; w.pages = [];
      }
    } else {
      w.source = { file: sc.file, kind: sc.kind, pages: 0, locKind: 'page', at: '' };
      w.fallback = { reason: sc.reason };
      w.paragraphs = []; w.pages = [];
    }
    if (!w.category || w.category === '服务政策') w.category = defaultCategoryFor(w.templateId);
  } else if (w.mode === 'replace') {
    const e = byId(w.entryId);
    const sc = IMPORT_SCENARIOS[w.scenario];
    if (sc.label.indexOf('识别成功') >= 0) {
      const sample = PDF_SAMPLE;
      w.source = { file: sample.file, kind: sample.kind, pages: sample.pages, locKind: sample.locKind, at: '' };
      w.locKind = sample.locKind;
      w.paragraphs = parasFromSample(sample);
      w.pages = pagesFromParas(w.paragraphs);
      w.compare = buildCompare(e.paragraphs, w.paragraphs);
      w.compareOldV = e.currentVersion;
    } else {
      w.source = { file: sc.file, kind: sc.kind, pages: 0, locKind: 'page', at: '' };
      w.fallback = { reason: sc.reason + '（新文件无法识别时，无法与当前版本对照；可以人工整理正文后再保存草稿。）' };
      w.paragraphs = []; w.pages = []; w.compare = null;
    }
  } else if (w.mode === 'manual') {
    w.source = { file: '', kind: '手动创建', pages: 0, locKind: 'page', at: '' };
    w.paragraphs = []; w.pages = [];
  }
  w.curPage = 1;
  w.step = 2;
  render();
  focusStepHeading();
}
function buildCompare(oldParas, newParas) {
  const rows = [];
  const n = Math.max(oldParas.length, newParas.length);
  for (let i = 0; i < n; i++) {
    const o = oldParas[i]; const np = newParas[i];
    if (o && np) rows.push({ kind: 'pair', label: (o.text === np.text) ? '文字相同' : '文字有变化', o: o.text, n: np.text, npId: np.id });
    else if (!o && np) rows.push({ kind: 'added', label: '新增段落', n: np.text, npId: np.id });
    else rows.push({ kind: 'gone', label: '旧版有此段，新文件未匹配到', o: o.text });
  }
  return rows;
}
function workFromEntry(e, mode) {
  const w = newWork(mode, { entryId: e.id, home: e.home, templateId: e.templateId, templateDecided: true, templateVersion: e.templateVersion, title: e.title, category: e.category, usage: e.usage, aiOn: e.aiOn, expiry: e.expiry });
  w.paragraphs = clone(e.paragraphs);
  w.fields = clone(e.fields);
  w.source = clone(e.source);
  w.locKind = (e.source && e.source.locKind) || 'page';
  w.pages = pagesFromParas(w.paragraphs);
  return w;
}
function workFromDraft(e) {
  const d = e.draft;
  const c = clone(d.content);
  const w = newWork(d.kind || 'edit', { entryId: e.id, home: e.home, templateId: c.templateId, templateDecided: true, templateVersion: c.templateVersion, title: c.title, category: c.category, usage: c.usage, aiOn: c.aiOn, expiry: c.expiry });
  w.paragraphs = c.paragraphs || [];
  w.fields = c.fields || [];
  w.historicFields = c.historicFields || [];
  w.source = c.source || { file: '', kind: '', pages: 0, locKind: 'page', at: '' };
  w.locKind = w.source.locKind || 'page';
  w.pages = pagesFromParas(w.paragraphs);
  w.step = 2;
  return w;
}
function focusStepHeading() {
  const h = qs('#step-region h1');
  if (h) { h.setAttribute('tabindex', '-1'); h.focus({ preventScroll: true }); }
}

/* ---------- 核对向导：渲染 ---------- */
function reviewHTML() {
  const w = work();
  const names = ['选择文件与模板', '核对来源与字段', '保存与发布'];
  const steps = names.map((nm, i) => {
    const n = i + 1;
    return '<div class="step' + (w.step === n ? ' on' : '') + '"' + (w.step === n ? ' aria-current="step"' : '') + '><span class="n">' + n + '</span>' + nm + '</div>';
  }).join('<span class="sep" aria-hidden="true"></span>');
  const modeNote = w.mode === 'replace'
    ? '<p class="info-note"><svg class="ic"><use href="#i-history"/></svg><span>更新文件：为新文件登记独立来源，核对后保存为 <b>新草稿</b>；当前发布版本继续可用，发布成功后才会切换。</span></p>'
    : w.mode === 'edit' ? '<p class="info-note"><svg class="ic"><use href="#i-history"/></svg><span>编辑草稿：保存或发布后才会更新线上版本；期间线上内容保持不变。</span></p>'
      : w.mode === 'manual' ? '<p class="info-note"><svg class="ic"><use href="#i-file"/></svg><span>手动创建：同样先选择知识模板；正文在核对页逐段添加。</span></p>' : '';
  return '<div class="steps" aria-label="步骤">' + steps + '</div>' + modeNote + '<div id="step-region">' + stepBodyHTML() + '</div>';
}
function stepBodyHTML() {
  const w = work();
  if (w.step === 1) return step1HTML();
  if (w.step === 2) return step2HTML();
  return step3HTML();
}
function renderStepRegion() { const el = qs('#step-region'); if (el) el.innerHTML = stepBodyHTML(); }

/* ---- 步骤 1 ---- */
function step1HTML() {
  const w = work();
  let html = '';
  if (w.mode === 'manual') {
    html += '<div class="card"><h1>手动创建：基本信息</h1>' +
      '<div class="form-grid"><div class="field" id="fld1-title"><label for="s1-title">标题</label><input id="s1-title" data-act="s1-title" value="' + esc(w.title) + '" placeholder="例如：上门安装服务说明"></div>' +
      '<div><label for="s1-category">分类</label><select id="s1-category" data-act="s1-category">' + CATEGORY_LIST.map((c) => '<option' + (w.category === c ? ' selected' : '') + '>' + esc(c) + '</option>').join('') + '</select></div></div>' +
      '<p class="hint-line">标题也可以稍后在核对页填写；保存草稿允许未完成。</p></div>';
  } else {
    const replacing = w.mode === 'replace';
    html += '<div class="card"><h1>' + (replacing ? '更新文件：选择新文件' : '上传文件 · 第 1 步：选择文件') + '</h1>' +
      '<div class="drop">' +
      '<svg class="ic big"><use href="#i-upload"/></svg>' +
      '<strong>选择本地文件</strong>' +
      '<p>演示说明：选择本地文件只会记录<b>文件名</b>，不会读取内容、不会上传。评审建议限制：单文件 ≤10MB、≤200 页；超出时登记原件并人工整理。一次处理一个文件。</p>' +
      '<input type="file" id="file-input" class="sr-input" aria-label="选择本地文件（演示：不读取内容、不上传）">' +
      '<button class="button secondary" type="button" data-act="pick-file"><svg class="ic"><use href="#i-file"/></svg>选择本地文件…</button>' +
      '<div class="sample-row"><button class="button secondary" type="button" data-act="sample-pdf">演示样例：PDF</button><button class="button secondary" type="button" data-act="sample-docx">演示样例：DOCX</button></div>' +
      '</div>' +
      '<div id="step1-status">' + step1StatusHTML() + '</div>' +
      (replacing ? '<p class="hint-line">新文件会登记为独立来源；原文件与旧版本不会被删除。</p>' : '') +
      '</div>';
    html += scenarioBoxHTML(w);
  }
  html += '<div class="card"><h2>知识模板</h2><div id="tmpl-region">' + tmplRegionHTML() + '</div>' +
    '<div class="actions" style="margin-top:12px">' +
    (w.mode === 'replace' ? '' : '<button class="button secondary" type="button" data-act="pick-template">选择知识模板…</button>') +
    (state.role === 'admin' ? '<button class="button ghost" type="button" data-act="manage-templates">管理知识模板</button>' : '') +
    '</div>' +
    '<p class="hint-line">模板决定这条知识在核对页要填写的业务字段；选择「通用文档」就只整理标题与正文，不强迫所有文档填业务字段。模板不是第四种知识分类，也不赋予任何权限。</p></div>';
  const nextLabel = w.mode === 'manual' ? '开始编写 →' : '开始核对 →';
  html += '<div class="review-footer"><div class="actions" style="flex:1 1 auto;justify-content:flex-start"><button class="button secondary" data-act="wizard-cancel">取消</button></div>' +
    '<div class="actions"><button class="button" id="step1-next" type="button" data-act="start-check"' + (step1Ready() ? '' : ' disabled') + '>' + nextLabel + '</button></div></div>';
  html += '<div id="step1-next-reason">' + step1ReasonHTML() + '</div>';
  return html;
}
function step1Ready() {
  const w = work();
  if (!w.templateDecided) return false;
  if (w.mode === 'manual') return true;
  return !!w.pending;
}
function step1ReasonHTML() {
  const w = work();
  const reasons = [];
  if (w.mode !== 'manual' && !w.pending) reasons.push('请先选择文件（或点「演示样例」）');
  if (!w.templateDecided) reasons.push('请先选择知识模板，或选择「通用文档」');
  if (!reasons.length) return '';
  return '<div class="btn-reason muted-reason"><svg class="ic"><use href="#i-alert"/></svg><span>还未开始：' + reasons.join('；') + '。</span></div>';
}
function step1StatusHTML() {
  const w = work();
  if (!w.pending) return '<p class="hint-line" style="margin-top:12px">尚未选择文件。可以直接点「演示样例」，查看识别与核对的完整流程。</p>';
  const p = w.pending;
  let small = '演示场景决定识别结果；点「开始核对」查看结果。';
  if (p.native) small = '演示：仅记录文件名，未读取任何内容、未上传；真实系统会在这里展示识别出的正文。';
  return '<div class="file-chip"><svg class="ic"><use href="#i-file"/></svg><span><b>' + esc(p.file) + '</b><small>' + (p.kind ? esc(p.kind) + ' · ' : '') + small + '</small></span></div>';
}
function scenarioBoxHTML(w) {
  const opts = Object.keys(IMPORT_SCENARIOS).map((k) =>
    '<label class="scenario-opt"><input type="radio" name="scenario-import" value="' + k + '" data-act="scenario-import"' + (w.scenario === k ? ' checked' : '') + '><span>' + esc(IMPORT_SCENARIOS[k].label) + '<small>' + esc(IMPORT_SCENARIOS[k].note || (IMPORT_SCENARIOS[k].reason || '').slice(0, 46) + '…') + '</small></span></label>').join('');
  return '<div class="scenario-box"><div class="sc-head"><span class="scenario-tag">演示场景 · 评审控件</span><span class="sc-note">不属于产品界面；用于直接切换演示结果（原型不读取真文件、不用倒计时模拟处理）。</span></div><div class="scenario-opts">' + opts + '</div></div>';
}
function tmplRegionHTML() {
  const w = work();
  if (w.mode === 'replace') {
    const e = byId(w.entryId);
    const tpl = e && e.templateId ? templateById(e.templateId) : null;
    return '<div class="tmpl-current"><b>沿用本知识模板：' + (tpl ? esc(tpl.name) + ' v' + e.templateVersion : '通用文档') + '</b><small>更新文件默认沿用已发布版本的模板；如需换模板，请在「知识模板」中为新建内容选择。</small></div>';
  }
  if (!w.templateDecided) return '<div class="tmpl-current"><b>尚未选择</b><small>请选择模板；或选择「通用文档（只整理标题与正文）」。</small></div>';
  if (!w.templateId) return '<div class="tmpl-current"><b>通用文档（只整理标题与正文）</b><small>不附加业务字段；之后仍可在核对页改选模板。</small></div>';
  const tpl = templateById(w.templateId);
  const v = versionOf(tpl, w.templateVersion) || latestVersion(tpl);
  return '<div class="tmpl-current"><b>' + esc(tpl.name) + ' v' + v.v + '</b><small>适用形态：' + esc(SHAPE_LABEL[tpl.shape]) + ' · 字段：' + esc(v.fields.map((f) => f.label + '（' + TYPE_LABEL[f.type] + (f.required ? '，必填' : '') + '）').join('、')) + '</small></div>';
}

/* ---- 步骤 2：核对 ---- */
function step2HTML() {
  const w = work();
  return '<h1>第 2 步：核对来源与字段</h1>' +
    (w.mode === 'replace' ? compareCardHTML(w) : '') +
    '<div class="review-grid">' +
    origPanelHTML(w) +
    '<div class="right-col"><div id="internal-banner">' + internalBannerHTML(w) + '</div>' + paraSectionHTML(w) + '</div>' +
    '</div>' +
    fieldsSectionHTML(w) +
    '<div class="review-footer"><div class="actions" style="flex:1 1 auto;justify-content:flex-start">' +
    '<button class="button secondary" data-act="step-back">← 上一步</button>' +
    '<button class="button secondary" data-act="wizard-cancel">取消</button></div>' +
    '<div class="actions"><span class="hint-line" style="margin:0 10px 0 0">发布前需要：包含段落已全部核对、必填字段已填写；保存草稿不要求完成。</span>' +
    '<button class="button secondary" data-act="save-draft">保存草稿</button>' +
    '<button class="button" data-act="to-step3">下一步：保存与发布 →</button></div></div>';
}
function compareCardHTML(w) {
  if (!w.compare) {
    return '<div class="card compare-wrap"><h2>与当前版本的对照（演示规则）</h2><p class="hint-line">新文件未能识别，暂时无法对照；可人工整理正文后保存草稿。对照只是文本比对，不判断新旧资料哪个真实。</p></div>';
  }
  const rows = w.compare.map((r) => {
    const tag = r.kind === 'pair' ? (r.label === '文字相同' ? '<span class="tag done">文字相同</span>' : '<span class="tag edit">文字有变化</span>')
      : r.kind === 'added' ? '<span class="tag review">新增段落</span>' : '<span class="tag gone">旧版有此段，新文件未匹配到</span>';
    return '<div class="compare-row">' +
      '<div class="cmp-cell' + (r.o ? '' : ' empty-cell') + '"><span class="cmp-cap">当前发布版 v' + esc(String(w.compareOldV || '')) + '</span>' + esc(r.o || '（无对应段落）') + '</div>' +
      '<div class="cmp-cell' + (r.n ? '' : ' empty-cell') + '"><span class="cmp-cap">新文件识别结果（演示）' + (r.npId ? ' · <button class="linkbtn" type="button" data-act="cmp-focus" data-id="' + r.npId + '">定位到该段</button>' : '') + '</span>' + esc(r.n || '（无对应段落）') + '</div>' +
      '<div class="cmp-mid">' + tag + ' · 对照按段落顺序简单对应，仅为评审演示；不做语义判断，也不声称存在冲突。</div>' +
      '</div>';
  }).join('');
  return '<div class="card compare-wrap"><div class="section-head"><h2>与当前版本的对照（演示规则）</h2><span class="sec-note">左右两列为文本/字段对照；请人工阅读判断是否需要修改整理结果。</span></div>' + rows + '</div>';
}
function origPanelHTML(w) {
  const isPdf = w.locKind !== 'para';
  const badge = '非真实 ' + (isPdf ? 'PDF' : 'Word') + ' 渲染 · 对照演示';
  let body = '';
  if (w.fallback) {
    body = '<div class="warn-note" style="margin:0"><svg class="ic"><use href="#i-alert"/></svg><span><b>无法自动识别。</b>' + esc(w.fallback.reason) + '</span></div>';
  } else if (!w.pages.length) {
    body = '<p class="orig-fallback">还没有正文内容。点右侧「添加段落」手动补充，或回到上一步换一个演示样例。</p>';
  } else {
    body = '<div id="orig-region">' + origPagesHTML(w) + '</div>';
  }
  const unit = isPara(w) ? '段' : '页';
  const ctrl = (w.pages.length > 1) ? '<div class="page-ctrl">' +
    '<button class="button secondary" type="button" data-act="page-prev"' + (w.curPage <= 1 ? ' disabled' : '') + '>← 上一' + unit + '</button>' +
    '<span class="pg-label">第 ' + w.curPage + ' / ' + w.pages.length + ' ' + unit + '</span>' +
    '<button class="button secondary" type="button" data-act="page-next"' + (w.curPage >= w.pages.length ? ' disabled' : '') + '>下一' + unit + ' →</button></div>' : '';
  return '<div class="card orig-panel"><div class="orig-head"><h2>原文示例</h2><span class="orig-badge">' + esc(badge) + '</span></div>' +
    '<p class="hint-line">用于对照来源位置的演示文本；点击右侧段落的「来源」会跳到对应页并高亮对应内容。</p>' + ctrl + body + '</div>';
}
function origPagesHTML(w) {
  return w.pages.map((pg) => {
    const blocks = pg.ids.map((id) => {
      const p = w.paragraphs.find((x) => x.id === id);
      if (!p) return '';
      return '<div class="orig-block" data-anchor="' + p.id + '" data-page="' + pg.n + '"><span class="ob-tag">' + esc(isPara(w) ? '第 ' + pg.n + ' 段' : '第 ' + pg.n + ' 页') + ' · 示例文本</span>' + esc(p.text) + '</div>';
    }).join('');
    return '<div class="orig-page' + (pg.n === w.curPage ? ' on' : '') + '" data-page="' + pg.n + '">' + blocks + '</div>';
  }).join('');
}
function isPara(w) { return w.locKind === 'para'; }
function refreshOrig() { const r = qs('#orig-region'); if (r) r.innerHTML = origPagesHTML(work()); }
function paraSectionHTML(w) {
  const paras = w.paragraphs;
  const inc = paras.filter((p) => p.included);
  const unchecked = inc.filter((p) => !p.checked);
  const emptyInc = inc.filter((p) => !p.text.trim());
  const manual = w.mode === 'manual' || !!w.fallback;
  let list = '';
  if (!paras.length) {
    list = '<p class="hint-line">' + (w.fallback ? '识别未产生正文：请手动添加段落（原件已登记为来源）。' : '还没有段落。') + '</p>';
  } else {
    list = paras.map((p, i) => paraCardHTML(p, i, w)).join('');
  }
  return '<div class="card"><div class="section-head"><h2>整理正文（逐段核对）</h2><span class="sec-note">识别结果只做文字提取，不校验内容是否正确；请以原文为准人工核对。</span></div>' +
    '<p class="list-count" id="para-count-line">' + paraCountText(w) + '</p>' +
    list +
    '<div class="actions" style="margin-top:4px"><button class="button secondary para-add" type="button" data-act="add-para"><svg class="ic"><use href="#i-plus"/></svg>' + (manual ? '添加段落' : '手动补充段落') + '</button></div>' +
    '<p class="hint-line">未包含的段落仍保留在来源记录中（私有），不会进入正文，也不会用于 AI 客服回答。标注「人工修改」的段落与原始文件不完全一致；对外引用以本条已发布内容为准，不宣称与原件逐字相同。</p>' +
    '</div>';
}
function paraCountText(w) {
  const paras = w.paragraphs;
  const inc = paras.filter((p) => p.included);
  const un = inc.filter((p) => !p.checked).length;
  let t = '共 ' + paras.length + ' 段；已包含 ' + inc.length + ' 段' + (un ? '，其中 ' + un + ' 段尚未勾选「确认已核对」' : '，均已核对');
  if (paras.some((p) => p.needsReview)) t += '；有段落在识别时标记「待核对」（如表格断行），请人工确认';
  if (isPara(w)) t += '；DOCX 按「段」定位来源，不虚构页码';
  return t + '。';
}
function paraCardHTML(p, i, w) {
  const tags = [];
  if (p.modified) tags.push('<span class="tag edit">人工修改</span>');
  if (p.internal) tags.push('<span class="tag internal">仅内部</span>');
  if (p.needsReview) tags.push('<span class="tag review" title="' + esc(p.reviewNote || '') + '">待核对' + (p.reviewNote ? ' · ' + esc(p.reviewNote) : '') + '</span>');
  return '<div class="para-card" data-id="' + p.id + '">' +
    '<div class="para-head"><span class="para-no">第 ' + (i + 1) + ' 段</span>' + tags.join('') +
    '<button class="linkbtn" type="button" data-act="para-source" data-id="' + p.id + '">来源 · ' + esc(locLabel(w, locOf(p))) + (p.needsReview ? '（待核对）' : '') + '</button>' +
    '<span style="margin-left:auto"><button class="icon-btn" type="button" data-act="para-del" data-id="' + p.id + '" aria-label="删除第 ' + (i + 1) + ' 段"><svg class="ic"><use href="#i-trash"/></svg></button></span></div>' +
    '<textarea data-act="para-text" data-id="' + p.id + '" aria-label="第 ' + (i + 1) + ' 段正文">' + esc(p.text) + '</textarea>' +
    '<div class="para-foot">' +
    '<label class="check-inline"><input type="checkbox" data-act="para-included" data-id="' + p.id + '"' + (p.included ? ' checked' : '') + '><span>包含该段</span></label>' +
    '<label class="check-inline"><input type="checkbox" data-act="para-checked" data-id="' + p.id + '"' + (p.checked ? ' checked' : '') + '><span>确认已核对</span></label>' +
    '<button class="linkbtn" type="button" data-act="use-as-field" data-id="' + p.id + '">用作字段值…</button>' +
    '</div></div>';
}
function internalBannerHTML(w) {
  const conflict = w.usage === 'external' && w.paragraphs.some((p) => p.included && p.internal);
  if (!conflict) return '';
  return '<div class="form-alert"><svg class="ic"><use href="#i-alert"/></svg><div><b>这份文件里混有「仅内部」内容，而本条知识是对外使用。</b>' +
    '<p style="margin:6px 0 0">整条知识只有一套使用范围，段落与字段不单独设权限。两种处理方式：① 取消包含内部段落（原内容仍保留为私有来源）；② 把整条知识改为内部使用（如内部内容也要给客户，请另外新建一条对外知识）。</p>' +
    '<div class="actions" style="margin-top:8px"><button class="button secondary" type="button" data-act="drop-internal-para">不包含内部段落</button><button class="button secondary" type="button" data-act="make-internal">整条改为内部</button></div></div></div>';
}
function refreshParaMeta() {
  const w = work();
  const cl = qs('#para-count-line');
  if (cl) cl.textContent = paraCountText(w);
  const ib = qs('#internal-banner');
  if (ib) ib.innerHTML = internalBannerHTML(w);
}

/* ---- 字段区 ---- */
function fieldsSectionHTML(w) {
  const tpl = w.templateId ? templateById(w.templateId) : null;
  const v = tpl ? (versionOf(tpl, w.templateVersion) || latestVersion(tpl)) : null;
  let html = '<div class="card"><h2 id="fields-h">基本字段（所有知识共用）</h2>' +
    '<div class="form-grid">' +
    '<div class="field"><label for="w-title">标题 <span class="req-star">*</span></label><input id="w-title" data-act="w-title" value="' + esc(w.title) + '" placeholder="例如：客户服务政策"></div>' +
    '<div><label for="w-category">分类</label><select id="w-category" data-act="w-category">' + CATEGORY_LIST.map((c) => '<option' + (w.category === c ? ' selected' : '') + '>' + esc(c) + '</option>').join('') + '</select></div>' +
    '</div>' +
    '<fieldset style="border:0;padding:0;margin:4px 0 0"><legend class="sr-input">使用范围</legend>' +
    '<label class="check-inline"><input type="radio" name="w-usage" value="internal" data-act="w-usage"' + (w.usage === 'internal' ? ' checked' : '') + '><span>内部使用（默认）</span></label>' +
    '<label class="check-inline"><input type="radio" name="w-usage" value="external" data-act="w-usage"' + (w.usage === 'external' ? ' checked' : '') + '><span>对外提供给客户</span></label></fieldset>' +
    '<p class="hint-line">整条知识只有一套使用范围；段落和字段不单独设置权限。</p>' +
    '<div class="switch-row" style="margin:12px 0 0"><span>允许 AI 客服使用（默认关闭）</span>' +
    '<label class="ios-switch"><input type="checkbox" data-act="w-ai"' + (w.aiOn ? ' checked' : '') + ' aria-label="允许 AI 客服使用"><span class="ios-slider"></span></label></div>' +
    '<p class="hint-line">开启且对外时，发布需要一次明确确认；内部使用或未授权时不额外确认。</p>' +
    '<label for="w-expiry">到期时间（可选）</label><input type="datetime-local" id="w-expiry" data-act="w-expiry" value="' + esc(expiryInputValue(w.expiry)) + '">' +
    '<p class="hint-line">按北京时间填写；留空 = 长期有效；到期后不再用于 AI 客服回答。与模板字段里的日期不是同一件事。</p>' +
    '</div>';
  html += '<div class="card"><h2>模板字段' + (tpl ? '（来自 ' + esc(tpl.name) + ' v' + (v ? v.v : 1) + '）' : '') + '</h2>' +
    '<p class="hint-line">若要从原文对应：先在左侧段落点「用作字段值…」手动填入。字段默认都按内部候选处理：需勾选「已核对」并允许客户问答后，才可能用于 AI 客服回答。必填只表示资料完整度，与 AI 授权无关；数字 0、选择「否」都算已填写。</p>' +
    '<div id="fields-region">' + fieldsRegionHTML(w) + '</div></div>';
  return html;
}
function fieldsRegionHTML(w) {
  if (!w.templateId) return '<p class="hint-line">本条知识使用「通用文档」：没有业务字段，只有标题与正文。</p>';
  let html = w.fields.map(fieldCardHTML).join('');
  if (w.historicFields && w.historicFields.length) {
    html += '<h3 style="margin-top:14px">旧版模板字段（仅存历史）</h3>' +
      w.historicFields.map((f) => '<div class="field-card historic"><div class="fc-head"><b>' + esc(f.label) + '</b><span class="tag gone">已从新版模板移除</span></div>' +
        '<p class="fc-help">旧值：' + esc(f.value || '（未填写）') + '。该字段在新版模板中已移除；旧值保留在历史与来源中，不会静默删除，也不进入新版本的字段区。</p></div>').join('');
  }
  return html;
}
function fieldSrcText(f, w) {
  if (!String(f.value).trim()) return '未填写';
  if (f.sourceKind === '原文') return '原文 · ' + locLabel(w, f.loc);
  if (f.sourceKind === '人工修改') return '人工修改' + (f.loc != null ? '（基于' + locLabel(w, f.loc) + '）' : '');
  if (f.sourceKind === '手动填写') return '手动填写';
  return '未标注来源（请核对）';
}
function fieldCardHTML(f) {
  if (f.historic) return '';
  const w = work();
  let ctrl = '';
  if (f.type === 'text') ctrl = '<input type="text" data-act="field-value" data-key="' + f.key + '" value="' + esc(f.value) + '" aria-label="' + esc(f.label) + '">';
  else if (f.type === 'number') ctrl = '<input type="text" inputmode="decimal" data-act="field-value" data-key="' + f.key + '" value="' + esc(f.value) + '" aria-label="' + esc(f.label) + '">' + (f.unit ? '<span class="unit">' + esc(f.unit) + '</span>' : '<span class="unit">（无单位）</span>');
  else if (f.type === 'options') ctrl = '<select data-act="field-value" data-key="' + f.key + '" aria-label="' + esc(f.label) + '"><option value=""' + (!f.value ? ' selected' : '') + '>未填写</option>' + f.options.map((o) => '<option' + (f.value === o ? ' selected' : '') + '>' + esc(o) + '</option>').join('') + '</select>';
  else if (f.type === 'date') ctrl = '<input type="date" data-act="field-value" data-key="' + f.key + '" value="' + esc(f.value) + '" aria-label="' + esc(f.label) + '">';
  else if (f.type === 'yesno') ctrl = '<select data-act="field-value" data-key="' + f.key + '" aria-label="' + esc(f.label) + '"><option value=""' + (!f.value ? ' selected' : '') + '>未选择</option><option' + (f.value === '是' ? ' selected' : '') + '>是</option><option' + (f.value === '否' ? ' selected' : '') + '>否</option></select>';
  return '<div class="field-card' + (f.modified ? ' is-modified' : '') + '" data-key="' + f.key + '">' +
    '<div class="fc-head"><b>' + esc(f.label) + '</b>' + (f.required ? '<span class="req-star">必填</span>' : '') + '<span class="tag type">' + esc(TYPE_LABEL[f.type]) + (f.unit ? ' · ' + esc(f.unit) : '') + '</span></div>' +
    (f.help ? '<p class="fc-help">' + esc(f.help) + '</p>' : '') +
    '<div class="fc-ctrl">' + ctrl + '</div>' +
    '<div class="fc-meta"><span class="src-label">来源：<b data-role="fsrc">' + esc(fieldSrcText(f, w)) + '</b>' + (f.modified ? ' <span class="tag edit">人工修改</span>' : '') + '</span>' +
    '<label class="check-inline"><input type="checkbox" data-act="field-checked" data-key="' + f.key + '"' + (f.checked ? ' checked' : '') + '><span>已核对</span></label>' +
    '<label class="check-inline"><input type="checkbox" data-act="field-ai" data-key="' + f.key + '"' + (f.aiAllowed ? ' checked' : '') + '><span>允许用于客户问答</span></label>' +
    '</div></div>';
}
function refreshFieldsRegion() { const r = qs('#fields-region'); if (r) r.innerHTML = fieldsRegionHTML(work()); }

/* ---- 步骤 3：保存与发布 ---- */
function publishGates(w) {
  const inc = w.paragraphs.filter((p) => p.included);
  const req = w.fields.filter((f) => f.required && !f.historic);
  return [
    { ok: !!w.title.trim(), text: '标题已填写' },
    { ok: inc.length >= 1, text: '至少包含 1 段正文' },
    { ok: inc.length >= 1 && inc.every((p) => p.text.trim() !== ''), text: '包含的段落内容不为空' },
    { ok: inc.length >= 1 && inc.every((p) => p.checked), text: '所有包含段落已勾选「确认已核对」' },
    { ok: !(w.usage === 'external' && inc.some((p) => p.internal)), text: '对外知识中没有包含「仅内部」段落' },
    { ok: req.every((f) => String(f.value).trim() !== ''), text: '模板必填字段已填写' + (req.length ? '（' + req.filter((f) => !String(f.value).trim()).map((f) => f.label).join('、') + '）' : '') }
  ];
}
function step3HTML() {
  const w = work();
  const gates = publishGates(w);
  const bad = gates.filter((g) => !g.ok);
  const inc = w.paragraphs.filter((p) => p.included);
  const aiFields = w.fields.filter((f) => !f.historic && f.checked && f.aiAllowed);
  const tpl = w.templateId ? templateById(w.templateId) : null;
  const kv = [
    ['标题', w.title || '（未填写）'],
    ['来源文件', w.source && w.source.file ? w.source.file + '（' + w.source.kind + '）' : '手动创建'],
    ['模板', tpl ? tpl.name + ' v' + w.templateVersion : '通用文档'],
    ['分类', w.category],
    ['使用范围', w.usage === 'external' ? '对外提供给客户' : '内部使用'],
    ['AI 客服授权', w.aiOn ? '已授权' : '未授权（默认）'],
    ['到期时间', fmtExpiry(w.expiry)],
    ['正文', '包含 ' + inc.length + ' 段 / 共 ' + w.paragraphs.length + ' 段'],
    ['字段', w.templateId ? (w.fields.filter((f) => !f.historic && String(f.value).trim() !== '').length + ' / ' + w.fields.filter((f) => !f.historic).length + ' 个已填写；其中 ' + aiFields.length + ' 个允许用于客户问答') : '无模板字段']
  ];
  return '<h1>第 3 步：保存与发布</h1>' +
    '<div class="card"><h2>将要保存的内容</h2><dl class="summary-list">' + kv.map((r) => '<div class="kv"><dt>' + esc(r[0]) + '</dt><dd>' + esc(r[1]) + '</dd></div>').join('') + '</dl></div>' +
    '<div class="card"><h2>发布检查</h2><div class="gates">' + gates.map((g) => '<div class="gate ' + (g.ok ? 'gate-ok' : 'gate-bad') + '"><svg class="ic"><use href="#i-' + (g.ok ? 'check' : 'alert') + '"/></svg><span>' + esc(g.text) + '</span></div>').join('') + '</div>' +
    '<p class="hint-line">发布成功只表示内容版本已发布；AI 客服是否能用还取决于使用范围、AI 授权、处理结果与到期时间。对外且已授权时，发布会先弹出一次确认。</p></div>' +
    '<div class="scenario-box"><div class="sc-head"><span class="scenario-tag">演示场景 · 评审控件</span><span class="sc-note">用于演示「发布后处理失败」的表现；真实处理结果由后端异步任务决定，原型不用计时器模拟。</span></div>' +
    '<div class="scenario-opts">' +
    '<label class="scenario-opt"><input type="radio" name="scenario-publish" value="ok" data-act="scenario-publish"' + (state.scenarioPublish !== 'fail' ? ' checked' : '') + '><span>发布后处理正常<small>发布后显示「已发布（AI 客服可用）」（未授权则为不可用）</small></span></label>' +
    '<label class="scenario-opt"><input type="radio" name="scenario-publish" value="fail" data-act="scenario-publish"' + (state.scenarioPublish === 'fail' ? ' checked' : '') + '><span>发布后处理失败<small>显示蓝灰「已发布（AI 客服不可用）」与处理失败原因，可重试；任务状态不新增知识状态</small></span></label>' +
    '</div></div>' +
    '<div class="review-footer"><div class="actions" style="flex:1 1 auto;justify-content:flex-start"><button class="button secondary" data-act="to-step2">← 返回核对</button><button class="button secondary" data-act="wizard-cancel">取消</button></div>' +
    '<div class="actions"><button class="button secondary" data-act="save-draft">保存草稿</button><button class="button" id="publish-btn" data-act="publish"' + (bad.length ? ' disabled' : '') + '>发布</button></div></div>' +
    (bad.length ? '<div class="btn-reason"><svg class="ic"><use href="#i-alert"/></svg><span>还不能发布：' + esc(bad.map((g) => g.text).join('；')) + '。可以先保存草稿，未完成的部分稍后继续。</span></div>' : '');
}

/* ---------- 详情页 ---------- */
function locLabelFor(kind, n) { if (n == null) return '未标注'; return kind === 'para' ? ('第 ' + n + ' 段') : ('第 ' + n + ' 页'); }
function detailHTML(id) {
  const e = byId(id);
  const av = availability(e);
  const r = state.route;
  const mgr = state.role === 'admin';
  const tpl = e.templateId ? templateById(e.templateId) : null;
  const newer = tpl && latestVersion(tpl).v > e.templateVersion ? latestVersion(tpl) : null;
  let actions = '';
  if (mgr) {
    if (e.lifecycle === 'draft') actions += '<button class="button" data-act="d-edit" data-id="' + e.id + '">继续编辑</button>';
    if (e.lifecycle === 'published') actions += '<button class="button secondary" data-act="d-edit" data-id="' + e.id + '">编辑</button><button class="button secondary" data-act="d-replace" data-id="' + e.id + '"><svg class="ic"><use href="#i-upload"/></svg>替换文件…</button><button class="button danger" data-act="d-disable" data-id="' + e.id + '">停用</button>';
    if (e.lifecycle === 'disabled') actions += '<button class="button secondary" data-act="d-enable" data-id="' + e.id + '">重新启用</button>';
  }
  let html = '<div class="detail-head"><div class="detail-title"><h1>' + esc(e.title) + '</h1><span class="pill ' + av.pill + '">' + esc(av.label) + '</span>' + (e.draft ? '<span class="tag review">待发布草稿</span>' : '') + '</div>' +
    (actions ? '<div class="actions">' + actions + '</div>' : '') + '</div>' +
    '<div class="detail-meta"><span>' + esc(e.category) + '</span><span>使用范围：' + (e.usage === 'external' ? '对外' : '内部') + '</span><span>AI 客服授权：' + (e.aiOn ? '已授权' : '未授权') + '</span>' + (e.expiry ? '<span>到期时间 ' + esc(fmtExpiry(e.expiry)) + '</span>' : '') + (tpl ? '<span>模板：' + esc(tpl.name) + ' v' + e.templateVersion + '</span>' : '') + '<span>更新 ' + esc(e.updated) + '</span></div>';
  if (state.role !== 'admin') html += '<p class="readonly-note"><svg class="ic"><use href="#i-book"/></svg><span>只读视角（演示）：可以阅读全部知识（含仅内部资料与字段），但没有编辑、发布、替换文件或模板管理入口。</span></p>';
  if (av.reason && e.lifecycle === 'published') html += '<div class="fail-note"><span><b>AI 客服不可用原因：</b>' + esc(av.reason) + '</span>' + (mgr && e.aiIssue ? '<button class="button secondary" data-act="d-retry" data-id="' + e.id + '">重试</button>' : '') + '</div>';
  if (e.draft && mgr) {
    html += '<div class="draft-banner banner"><span><b>' + esc(e.draft.label) + '</b> · ' + esc(e.draft.summary) + '（' + esc(e.draft.createdAt) + '）。当前发布版本保持不变，发布成功后才会切换。</span>' +
      '<span class="actions"><button class="button secondary" data-act="d-continue-draft" data-id="' + e.id + '">继续编辑草稿</button></span></div>';
  }
  const tabs = [['content', '内容'], ['fields', '字段'], ['versions', '来源与版本']];
  html += '<div class="tabs" role="tablist">' + tabs.map((t) => '<button class="tab' + (r.tab === t[0] ? ' on' : '') + '" role="tab" aria-selected="' + (r.tab === t[0]) + '" data-act="d-tab" data-tab="' + t[0] + '" data-id="' + e.id + '">' + t[1] + '</button>').join('') + '</div>';
  html += '<div class="detail-layout"><div>' + detailTabHTML(e, r.tab) + '</div>' + detailRailHTML(e, newer, mgr) + '</div>';
  return html;
}
function detailTabHTML(e, tab) {
  if (tab === 'fields') return detailFieldsHTML(e);
  if (tab === 'versions') return detailVersionsHTML(e);
  return detailContentHTML(e);
}
function detailContentHTML(e) {
  const inc = e.paragraphs.filter((p) => p.included);
  const excl = e.paragraphs.length - inc.length;
  const locKind = (e.source && e.source.locKind) || 'page';
  let html = '';
  if (!inc.length) html += '<p class="hint-line">这条知识还没有正文段落。</p>';
  html += inc.map((p, i) => '<div class="body-para"><div class="bp-head"><span class="para-no">第 ' + (i + 1) + ' 段</span>' +
    (p.modified ? '<span class="tag edit">人工修改</span>' : '') + (p.internal ? '<span class="tag internal">仅内部</span>' : '') +
    '<span class="src-label">来源 · ' + esc(locLabelFor(locKind, locOf(p))) + '</span></div><p>' + esc(p.text) + '</p></div>').join('');
  if (excl) html += '<p class="hint-line">另有 ' + excl + ' 段未包含（保留在私有来源中，不进入正文，也不用于 AI 客服回答）。</p>';
  if (inc.some((p) => p.modified)) html += '<p class="hint-line">标注「人工修改」的段落与原始文件不完全一致；对外引用以本条已发布内容为准，不宣称与原件逐字相同。</p>';
  return '<div class="card">' + html + '</div>';
}
function detailFieldsHTML(e) {
  const mgr = state.role === 'admin';
  const tpl = e.templateId ? templateById(e.templateId) : null;
  const newer = tpl && latestVersion(tpl).v > e.templateVersion ? latestVersion(tpl) : null;
  const locKind = (e.source && e.source.locKind) || 'page';
  let html = '';
  if (tpl) {
    html += '<div class="info-note"><svg class="ic"><use href="#i-layout"/></svg><span>正在使用 <b>' + esc(tpl.name) + ' v' + e.templateVersion + '</b>' + (newer ? '，已有新版 v' + newer.v + '。' : '（已是最新版本）。') +
      '字段是模板快照：模板改版不会自动改变这条知识的字段与历史。</span></div>';
    if (newer && mgr && e.lifecycle !== 'disabled') html += '<div class="actions" style="margin:0 0 14px"><button class="button secondary" data-act="d-upgrade" data-id="' + e.id + '">采用新版模板（v' + newer.v + '）…</button></div>';
  } else html += '<p class="hint-line">本条知识使用「通用文档」：没有业务字段。</p>';
  if (e.fields.length) {
    html += '<div class="table-scroll"><table class="fields"><thead><tr><th>字段</th><th>值</th><th>来源</th><th>已核对</th><th>允许客户问答</th></tr></thead><tbody>' +
      e.fields.map((f) => '<tr><td><b>' + esc(f.label) + '</b><span class="muted">' + esc(TYPE_LABEL[f.type] || '') + (f.unit ? ' · ' + esc(f.unit) : '') + '</span></td>' +
        '<td>' + esc(String(f.value).trim() === '' ? '（未填写）' : f.value) + '</td>' +
        '<td><span class="src-label">' + esc(f.sourceKind === '原文' ? '原文 · ' + locLabelFor(locKind, f.loc) : f.sourceKind === '人工修改' ? '人工修改' : (f.sourceKind || '未标注')) + '</span></td>' +
        '<td>' + (f.checked ? '<span class="state-line ok">已核对</span>' : '<span class="state-line warn">未核对</span>') + '</td>' +
        '<td>' + (f.aiAllowed ? '允许' : '关闭') + '</td></tr>').join('') + '</tbody></table></div>';
  }
  html += '<p class="hint-line">未勾选「已核对」或未允许客户问答的字段不会用于 AI 客服回答，也不冒充原文逐字引用；企业成员仍可阅读。</p>';
  return '<div class="card">' + html + '</div>';
}
function detailVersionsHTML(e) {
  const mgr = state.role === 'admin';
  let html = '<div class="card"><h2>来源文件</h2><div class="sources-list">' +
    (e.source && e.source.file ? '<div class="source-item"><div><strong>' + esc(e.source.file) + '</strong><small>' + esc(e.source.kind) + (e.source.pages ? ' · ' + e.source.pages + ' 页' : '') + ' · 登记于 ' + esc(e.source.at || '—') + ' · 原件仅本企业成员可读，不随「对外知识」公开</small></div></div>' :
      '<div class="source-item"><div><strong>手动创建</strong><small>没有原始文件；正文由成员编写。</small></div></div>') + '</div></div>';
  html += '<div class="card"><h2>版本历史</h2><p class="hint-line">「恢复」只会从历史版本创建一条新草稿，不覆盖历史、不直接替换线上版本；发布版本不会被草稿改动。</p><div class="versions-list">';
  if (e.draft) html += '<div class="version-item"><div><strong><span class="vtag">草稿</span>' + esc(e.draft.label) + '</strong><small>' + esc(e.draft.summary) + ' · ' + esc(e.draft.createdAt) + ' · 尚未发布</small></div>' + (mgr ? '<button class="button secondary" data-act="d-continue-draft" data-id="' + e.id + '">继续编辑</button>' : '') + '</div>';
  const vs = e.versions.slice().reverse();
  vs.forEach((v) => {
    const isCur = v.v === e.currentVersion;
    const hasSnap = !!v.snapshot;
    let btn = '';
    if (mgr) btn = hasSnap && !isCur && !e.draft ? '<button class="button secondary" data-act="d-restore" data-id="' + e.id + '" data-ver="' + v.v + '">恢复为草稿</button>' : (hasSnap ? '' : '<span class="muted small">无快照（演示限制）</span>');
    html += '<div class="version-item"><div><strong><span class="vtag">v' + v.v + '</span>' + esc(v.note) + '</strong><small>' + esc(v.date) + (isCur ? ' · 当前发布版本' : '') + '</small></div>' + btn + '</div>';
  });
  if (!e.versions.length) html += '<p class="hint-line">还没有发布版本（草稿）。</p>';
  html += '</div></div>';
  return html;
}
function detailRailHTML(e, newer, mgr) {
  const tpl = e.templateId ? templateById(e.templateId) : null;
  return '<aside class="rail"><h3>属性</h3>' +
    '<dl>' +
    '<div class="rail-item"><dt>分类</dt><dd>' + esc(e.category) + '</dd></div>' +
    '<div class="rail-item"><dt>使用范围</dt><dd>' + (e.usage === 'external' ? '对外提供给客户' : '内部使用') + '</dd></div>' +
    '<div class="rail-item"><dt>AI 客服授权</dt><dd>' + (e.aiOn ? '已授权' : '未授权') + '</dd></div>' +
    '<div class="rail-item"><dt>到期时间</dt><dd>' + esc(fmtExpiry(e.expiry)) + '</dd></div>' +
    '<div class="rail-item"><dt>模板</dt><dd>' + (tpl ? esc(tpl.name) + ' v' + e.templateVersion + (newer ? '<br><span class="muted small">已有新版 v' + newer.v + '</span>' : '') : '通用文档') + '</dd></div>' +
    '</dl></aside>';
}

/* ---------- 知识模板：列表与编辑器 ---------- */
function templateUseCount(id) { return DB.entries.filter((e) => e.templateId === id).length; }
function templateChips(tpl) {
  const v = latestVersion(tpl);
  return v.fields.slice(0, 4).map((f) => '<span class="chip">' + esc(f.label) + ' · ' + esc(TYPE_LABEL[f.type]) + (f.required ? ' · 必填' : '') + '</span>').join('') + (v.fields.length > 4 ? '<span class="chip">…</span>' : '');
}
function templatesHTML() {
  const cards = DB.templates.map((t) => {
    const v = latestVersion(t);
    return '<div class="tmpl-card"><h3>' + esc(t.name) + '</h3>' +
      '<div class="tc-meta">适用形态：' + esc(SHAPE_LABEL[t.shape]) + ' · 当前版本 v' + v.v + '（' + esc(v.date) + '）<br>使用中的知识：' + templateUseCount(t.id) + ' 条 · 共 ' + t.versions.length + ' 个版本</div>' +
      '<div class="chiprow">' + templateChips(t) + '</div>' +
      '<div class="tc-foot"><button class="button secondary" data-act="t-edit" data-id="' + t.id + '">编辑模板</button><span class="muted small">' + esc(v.note) + '</span></div></div>';
  }).join('');
  return '<div class="toolbar"><div><h1>知识模板</h1><p class="lead">模板是企业自己定义的录入表：固定字段（标题、分类、使用范围、到期时间、来源与版本）不可删除，企业只定义额外的业务字段。模板只决定“怎么录入”，不赋予任何权限，也不会自动改变已有知识。</p></div><div class="actions"><button class="button" data-act="tpl-new"><svg class="ic"><use href="#i-plus"/></svg>新建模板</button></div></div>' +
    '<p class="info-note"><svg class="ic"><use href="#i-layout"/></svg><span>权限（评审建议 P2-02）：管理模板沿用「管理员 / 获产品管理授权成员」，与第一批一致；只读成员可以阅读知识，但看不到本页与模板管理按钮。</span></p>' +
    '<div class="tmpl-cards">' + cards + '</div>';
}
function builderHTML() {
  const b = state.builder;
  const useCount = b.id ? templateUseCount(b.id) : 0;
  const rows = b.fields.map((f, i) => bfRowHTML(f, i)).join('');
  return '<div class="toolbar"><div><h1>' + esc(b.isNew ? '新建模板' : ('编辑模板：' + b.name)) + '</h1><p class="lead">' + (b.isNew ? '定义字段后保存为 v1。' : '修改会保存为<b>新版本</b>；已有知识继续使用原版本，不会自动搬家，也不会改变已发布快照与历史引用。') + '</p></div><div class="actions"><button class="button secondary" data-act="b-cancel">取消</button>' + (state.role === 'admin' ? '<button class="button" data-act="b-save">' + (b.isNew ? '保存模板（v1）' : '保存为新版本（v' + (b.baseVersion + 1) + '）') + '</button>' : '') + '</div></div>' +
    '<div id="bf-err"></div>' +
    '<div class="builder-grid"><div>' +
    '<div class="card"><h2>模板信息</h2><div class="form-grid">' +
    '<div><label for="bf-name">模板名称</label><input id="bf-name" data-act="bf-name" value="' + esc(b.name) + '" placeholder="例如：服务方案模板"></div>' +
    '<div><label for="bf-shape">适用知识形态</label><select id="bf-shape" data-act="bf-shape">' +
    '<option value="product_service"' + (b.shape === 'product_service' ? ' selected' : '') + '>商品 / 服务资料（产品、服务方案类字段）</option>' +
    '<option value="article"' + (b.shape === 'article' ? ' selected' : '') + '>文章 / 制度说明（FAQ、制度类字段）</option></select>' +
    '<p class="hint-line">适用形态只是提示这条模板更适合哪类知识，不限制选择，也不改变已有内容。</p></div>' +
    '</div>' +
    '<label>固定字段（系统保留，不可删除）</label><div class="fixed-fields"><span class="chip">标题</span><span class="chip">分类</span><span class="chip">使用范围（内部 / 对外）</span><span class="chip">到期时间</span><span class="chip">来源与版本</span><span class="chip">AI 授权</span></div>' +
    '</div>' +
    '<div class="card"><div class="section-head"><h2>业务字段</h2><span class="sec-note">支持：文字、数字+单位、选项、日期、是 / 否。不提供公式、重复组或字段间关联。</span></div>' +
    '<div>' + (rows || '<p class="hint-line">还没有字段。点下面「添加字段」开始。</p>') + '</div>' +
    '<div class="actions" style="margin-top:10px"><button class="button secondary" data-act="bf-add"><svg class="ic"><use href="#i-plus"/></svg>添加字段</button></div>' +
    '<p class="hint-line">规则：名称不能为空、同一模板内不能重名；选项类型至少 2 个选项；数字类型单位可留空。已填写的数据保留；改类型需要重新填写，历史保留。</p>' +
    '</div></div>' +
    '<div class="preview-panel" aria-label="录入表预览"><h3>预览（核对页中的样子）</h3><div id="bf-preview">' + bfPreviewHTML(b) + '</div>' +
    '<p class="hint-line">' + (b.id ? '已使用本模板的知识：' + useCount + ' 条' + (useCount ? '（继续使用原版本，不会自动升级）' : '') : '新模板保存后即可在使用中选择。') + '</p></div>' +
    '</div>';
}
function bfRowHTML(f, i) {
  const isNewField = !f.orig;
  const origLabel = f.orig ? f.orig.label : null;
  let extra = '';
  if (f.type === 'number') extra = '<div class="bf-extra"><span class="type-note">单位：</span><input data-act="bf-unit" data-key="' + f.key + '" value="' + esc(f.unit || '') + '" placeholder="可留空，例如 小时 / kg" aria-label="单位"><span class="type-note">数字字段不做公式或自动换算。</span></div>';
  if (f.type === 'options') extra = '<div class="bf-extra"><span class="type-note">选项（用「、」分隔，至少 2 个）：</span><input data-act="bf-options" data-key="' + f.key + '" value="' + esc((f.options || []).join('、')) + '"><span class="type-note">不接受空选项；不会自动猜选项。</span></div>';
  const typeChanged = f.orig && f.orig.type !== f.type;
  return '<div class="bf-row" data-key="' + f.key + '">' +
    '<input data-act="bf-label" data-key="' + f.key + '" value="' + esc(f.label) + '" placeholder="字段名称，例如：服务时效" aria-label="字段名称">' +
    '<select data-act="bf-type" data-key="' + f.key + '" aria-label="字段类型">' + Object.keys(TYPE_LABEL).map((t) => '<option value="' + t + '"' + (f.type === t ? ' selected' : '') + '>' + esc(TYPE_LABEL[t]) + '</option>').join('') + '</select>' +
    '<label class="bf-check"><input type="checkbox" data-act="bf-required" data-key="' + f.key + '"' + (f.required ? ' checked' : '') + '>必填</label>' +
    '<input data-act="bf-help" data-key="' + f.key + '" value="' + esc(f.help || '') + '" placeholder="填写提示（可留空）" aria-label="填写提示">' +
    '<div class="bf-actions"><button class="icon-btn up-btn" type="button" data-act="bf-up" data-key="' + f.key + '" aria-label="上移字段"' + (i === 0 ? ' disabled' : '') + '><svg class="ic"><use href="#i-up"/></svg></button>' +
    '<button class="icon-btn down-btn" type="button" data-act="bf-down" data-key="' + f.key + '" aria-label="下移字段"><svg class="ic"><use href="#i-down"/></svg></button></div>' +
    '<button class="icon-btn" type="button" data-act="bf-del" data-key="' + f.key + '" aria-label="删除字段" style="grid-column:5"><svg class="ic"><use href="#i-trash"/></svg></button>' +
    '<div class="bf-meta">' + (isNewField ? '<span>新字段</span>' : (origLabel !== f.label ? '<span>已改名：原「' + esc(origLabel) + '」，已填写的数据保留</span>' : '')) + (typeChanged ? '<span class="hint-line bad" style="margin:0">类型已更改：保存后需要重新填写该字段；历史与来源中的旧值保留</span>' : '') + '</div>' +
    extra + '</div>';
}
function bfPreviewHTML(b) {
  if (!b.fields.length) return '<p class="hint-line">还没有字段；添加后会在这里预览录入表。</p>';
  const sampleVal = (f) => {
    if (f.type === 'number') return '4' + (f.unit ? ' ' + f.unit : '');
    if (f.type === 'options') return (f.options && f.options[0]) || '（需至少 2 个选项）';
    if (f.type === 'date') return '2026-05-20';
    if (f.type === 'yesno') return '否（“否”不是空值）';
    return '示例文字';
  };
  return b.fields.map((f) => '<div class="pv-field"><b>' + esc(f.label || '（未命名）') + '</b>' + (f.required ? ' <span class="req-star">必填</span>' : '') +
    ' <span class="tag type">' + esc(TYPE_LABEL[f.type]) + '</span>' +
    '<span class="pv-sample">' + esc(sampleVal(f)) + '</span>' +
    (f.help ? '<small>' + esc(f.help) + '</small>' : '') + '</div>').join('');
}
function refreshPreview() { const p = qs('#bf-preview'); if (p && state.builder) p.innerHTML = bfPreviewHTML(state.builder); }
function builderErr(msg) {
  const el = qs('#bf-err');
  if (el) el.innerHTML = msg ? '<div class="form-alert"><svg class="ic"><use href="#i-alert"/></svg><span>' + esc(msg) + '</span></div>' : '';
}
function parseOptions(text) { return String(text || '').split(/[、,，;；\s]+/).map((s) => s.trim()).filter(Boolean); }
function diffTemplate(base, cur) {
  const items = [];
  if (base.name !== cur.name) items.push({ kind: '名称', label: base.name + ' → ' + cur.name, note: '模板名称变化只影响显示，不改变已有知识的字段与历史。' });
  if (base.shape !== cur.shape) items.push({ kind: '形态', label: SHAPE_LABEL[base.shape] + ' → ' + SHAPE_LABEL[cur.shape], note: '适用形态只是提示，不改变已有内容。' });
  cur.fields.forEach((nf) => {
    const bf = base.fields.find((x) => x.key === nf.key);
    if (!bf) { items.push({ kind: '新增', label: nf.label, note: '新版本中的新字段；采用新版的知识里留空待填。' }); return; }
    if (bf.type !== nf.type) items.push({ kind: '改类型', label: nf.label, note: '类型 ' + TYPE_LABEL[bf.type] + ' → ' + TYPE_LABEL[nf.type] + '：需要重新填写；旧值保留在历史与来源中。' });
    if (!!bf.required !== !!nf.required) items.push({ kind: '必填', label: nf.label, note: nf.required ? '变为必填：只约束采用新版模板之后的新发布。' : '改为选填。' });
    if (bf.label !== nf.label) items.push({ kind: '改名', label: bf.label + ' → ' + nf.label, note: '已填写的数据保留；已发布内容里的旧名称不变。' });
    if ((bf.unit || '') !== (nf.unit || '')) items.push({ kind: '单位', label: nf.label, note: '单位 ' + (bf.unit || '（无）') + ' → ' + (nf.unit || '（无）') + '。' });
    if ((bf.options || []).join('、') !== (nf.options || []).join('、')) items.push({ kind: '选项', label: nf.label, note: (bf.options || []).join('、') + ' → ' + (nf.options || []).join('、') + '。' });
  });
  base.fields.forEach((bf) => { if (!cur.fields.find((x) => x.key === bf.key)) items.push({ kind: '移除', label: bf.label, note: '新版本不再显示该字段；已发布内容与历史中的旧值保留，不会删除。' }); });
  return items;
}
function openBuilder(opts) {
  if (opts.id) {
    const tpl = templateById(opts.id);
    const v = latestVersion(tpl);
    state.builder = { id: tpl.id, isNew: false, name: tpl.name, shape: tpl.shape, baseVersion: v.v, fields: v.fields.map((f) => ({ key: f.key, label: f.label, type: f.type, unit: f.unit || '', options: (f.options || []).slice(), required: !!f.required, help: f.help || '', orig: { label: f.label, type: f.type } })) };
  } else {
    state.builder = { id: null, isNew: true, name: '', shape: 'product_service', baseVersion: 0, fields: [] };
  }
  state.builderDirty = false;
  state.route = { name: 'builder', view: state.route.view, id: null, tab: 'content' };
  render();
  focusStepHeading();
}
function validateBuilder() {
  const b = state.builder;
  if (!b.name.trim()) return '模板名称不能为空。';
  const names = b.fields.map((f) => f.label.trim());
  if (names.some((n) => !n)) return '有字段还没有名称：每个业务字段都需要名称。';
  const dup = names.find((n, i) => n && names.indexOf(n) !== i);
  if (dup) return '字段名称不能重复：「' + dup + '」出现了多次。';
  for (const f of b.fields) {
    if (f.type === 'options' && (f.options || []).filter(Boolean).length < 2) return '「' + f.label + '」是选项类型：至少需要 2 个选项，且不能留空。';
  }
  return '';
}
function commitBuilder() {
  const b = state.builder;
  if (b.isNew) {
    const tpl = { id: uid('tpl'), name: b.name.trim(), shape: b.shape, updated: todayStr(), usedCount: 0, versions: [{ v: 1, date: todayStr(), note: '初始版本', fields: b.fields.map((f) => ({ key: f.key, label: f.label.trim(), type: f.type, unit: f.unit || '', options: (f.options || []).slice(), required: !!f.required, help: f.help || '' })) }] };
    DB.templates.push(tpl);
    state.builder = null; state.builderDirty = false;
    state.route = { name: 'templates', view: state.route.view, id: null, tab: 'content' };
    render();
    toast('模板已保存为 v1（演示）；新建知识时即可选择。');
    return;
  }
  const tpl = templateById(b.id);
  const base = versionOf(tpl, b.baseVersion) || latestVersion(tpl);
  const diff = diffTemplate({ name: base === latestVersion(tpl) ? tpl.name : tpl.name, shape: tpl.shape, fields: base.fields }, b);
  if (!diff.length) { toast('没有修改，未生成新版本。', true); return; }
  const newV = tpl.versions[tpl.versions.length - 1].v + 1;
  const fields = b.fields.map((f) => {
    const bf = base.fields.find((x) => x.key === f.key);
    const typeChanged = bf && bf.type !== f.type;
    return { key: typeChanged ? (f.key + '_v' + newV) : f.key, label: f.label.trim(), type: f.type, unit: f.unit || '', options: (f.options || []).slice(), required: !!f.required, help: f.help || '' };
  });
  tpl.versions.push({ v: newV, date: todayStr(), note: '更新：' + diff.map((d) => d.kind + '「' + d.label + '」').slice(0, 4).join('、') + (diff.length > 4 ? ' 等' : ''), fields: fields });
  tpl.name = b.name.trim(); tpl.shape = b.shape; tpl.updated = todayStr();
  state.builder = null; state.builderDirty = false;
  state.route = { name: 'templates', view: state.route.view, id: null, tab: 'content' };
  render();
  const used = templateUseCount(tpl.id);
  toast('已保存为新版本 v' + newV + '（演示）' + (used ? '；' + used + ' 条知识继续使用旧版模板，不会自动迁移，可在各自详情页选择「采用新版模板」。' : '。'));
}

/* ---------- 补充状态 ---------- */
state.scenarioPublish = 'ok';
state.drawerPick = '__none';
state.mapPara = null;

/* ---------- Toast ---------- */
function toast(msg, warn) {
  const t = document.createElement('div');
  t.className = 'toast' + (warn ? ' warn' : '');
  t.textContent = msg;
  qs('#toasts').appendChild(t);
  requestAnimationFrame(() => t.classList.add('show'));
  setTimeout(() => { t.classList.remove('show'); setTimeout(() => t.remove(), 250); }, 3800);
}

/* ---------- 通用对话框（原生 dialog，Escape 取消、焦点返回） ---------- */
function openDialog(o) {
  state.lastFocus = document.activeElement;
  state.dlgActions = o.actions || [];
  const dlg = qs('#dlg');
  const icon = o.icon ? '<div class="dlg-icon ' + (o.iconCls || '') + '"><svg class="ic" style="width:18px;height:18px"><use href="#i-' + o.icon + '"/></svg></div>' : '';
  dlg.innerHTML = '<div class="dlg-head">' + icon + '<h2 class="dlg-title" id="dlg-title">' + esc(o.title) + '</h2></div>' +
    '<div class="dlg-body">' + o.body + '</div>' +
    '<div class="dlg-error" id="dlg-error"><svg class="ic"><use href="#i-alert"/></svg><span></span></div>' +
    '<div class="dlg-foot">' + state.dlgActions.map((a, i) => '<button type="button" class="button ' + (a.cls || 'secondary') + '" data-dlg="' + i + '">' + esc(a.label) + '</button>').join('') + '</div>';
  dlg.showModal();
  const f = o.focus ? qs(o.focus, dlg) : qs('.dlg-foot .button', dlg);
  if (f) f.focus();
}
function closeDialog() { const dlg = qs('#dlg'); if (dlg && dlg.open) dlg.close(); }
function dlgError(msg) {
  const el = qs('#dlg-error');
  if (!el) return;
  el.classList.toggle('on', !!msg);
  qs('span', el).textContent = msg || '';
}
function kvRows(rows) { return '<dl class="summary-list">' + rows.map((r) => '<div class="kv"><dt>' + esc(r[0]) + '</dt><dd>' + esc(r[1]) + '</dd></div>').join('') + '</dl>'; }
function changeList(items) {
  return '<ul class="dlg-list">' + items.map((it) => '<li><span class="tag ' + (it.kind === '新增' ? 'review' : it.kind === '移除' ? 'gone' : 'edit') + '">' + esc(it.kind) + '</span><span>' + esc(it.label) + '<small>' + esc(it.note) + '</small></span></li>').join('') + '</ul>';
}

/* ---------- 模板选择抽屉 ---------- */
const TPL_SAMPLES = {
  tpl_service: '示例：服务时效 4 小时；服务区域「市区 / 近郊 / 全国」',
  tpl_product: '示例：商品重量 2.5 kg；支持定制「是 / 否」',
  tpl_policy: '示例：制度发布日期 2026-05-20；适用范围「全体员工」'
};
function openDrawer() {
  state.lastFocus = document.activeElement;
  const w = work();
  state.drawerPick = w.templateDecided ? (w.templateId || '__none') : '__none';
  const d = qs('#drawer');
  d.innerHTML = '<form class="drawer-in" method="dialog" onsubmit="return false"><div class="drawer-head"><h2 id="drawer-title">选择知识模板</h2><button class="icon-btn" type="button" data-act="drawer-cancel" aria-label="关闭"><svg class="ic"><use href="#i-x"/></svg></button></div>' +
    '<div class="drawer-body">' +
    '<p class="hint-line" style="margin:0 0 12px">模板决定核对页需要填写的业务字段。示例只是起点，不是行业硬规则；保存前都可以改。</p>' +
    DB.templates.map((t) => drawerOptionHTML(t, state.drawerPick === t.id)).join('') +
    '<label class="tmpl-option' + (state.drawerPick === '__none' ? ' on' : '') + '"><input type="radio" name="tmpl-pick" value="__none" data-act="drawer-tmpl"' + (state.drawerPick === '__none' ? ' checked' : '') + '><span class="to-main"><b>通用文档（只整理标题与正文）</b><small>不附加业务字段；之后仍可在核对页改选模板。</small></span></label>' +
    '</div>' +
    '<div class="drawer-foot"><button class="button secondary" type="button" data-act="drawer-cancel">取消</button>' + (state.role === 'admin' ? '<button class="button secondary" type="button" data-act="manage-templates">管理知识模板</button>' : '') + '<button class="button" type="button" data-act="drawer-confirm">确认选择</button></div></form>';
  d.showModal();
  const f = qs('.drawer-body input', d);
  if (f) f.focus();
}
function drawerOptionHTML(t, on) {
  const v = latestVersion(t);
  return '<label class="tmpl-option' + (on ? ' on' : '') + '"><input type="radio" name="tmpl-pick" value="' + t.id + '" data-act="drawer-tmpl"' + (on ? ' checked' : '') + '>' +
    '<span class="to-main"><b>' + esc(t.name) + ' v' + v.v + '</b><small>适用形态：' + esc(SHAPE_LABEL[t.shape]) + ' · ' + esc(TPL_SAMPLES[t.id] || '') + '</small>' +
    '<span class="chiprow">' + v.fields.map((fld) => '<span class="chip">' + esc(fld.label) + ' · ' + esc(TYPE_LABEL[fld.type]) + (fld.required ? ' · 必填' : '') + '</span>').join('') + '</span></span></label>';
}
function closeDrawer() { const d = qs('#drawer'); if (d && d.open) d.close(); }

/* ---------- 导航与未保存保护 ---------- */
function resetFlow() { state.work = null; state.workDirty = false; state.builder = null; state.builderDirty = false; state.mapPara = null; }
function requestLeave(go) {
  const dirty = state.workDirty || state.builderDirty;
  if (!dirty) { go(); return; }
  state.pendingNav = go;
  if (state.workDirty) openUnsavedDialog(); else openUnsavedBuilderDialog();
}
function applyNav() { const g = state.pendingNav; state.pendingNav = null; if (g) g(); }
function openUnsavedDialog() {
  openDialog({
    title: '未保存的修改', icon: 'alert',
    body: '<p>核对内容还没有保存。可以直接保存为草稿、放弃本次未保存的改动，或继续编辑。取消不会丢内容。</p>',
    actions: [
      { label: '保存草稿并离开', cls: '', onClick: () => { if (saveDraftFromWork(false)) { closeDialog(); applyNav(); } else { dlgError('草稿也需要标题：请先填写标题，再保存离开。'); return 'keep'; } } },
      { label: '放弃修改并离开', cls: 'danger', onClick: () => { resetFlow(); closeDialog(); applyNav(); } },
      { label: '继续编辑', cls: 'secondary' }
    ]
  });
}
function openUnsavedBuilderDialog() {
  openDialog({
    title: '未保存的模板修改', icon: 'alert',
    body: '<p>模板编辑还没有保存为新版本。放弃会丢失这些修改；已保存的版本和已有知识不受影响。</p>',
    actions: [
      { label: '放弃修改并离开', cls: 'danger', onClick: () => { state.builder = null; state.builderDirty = false; closeDialog(); applyNav(); } },
      { label: '继续编辑', cls: 'secondary' }
    ]
  });
}

/* ---------- 保存与发布 ---------- */
function contentFromWork(w) {
  return { title: w.title, category: w.category, usage: w.usage, aiOn: w.aiOn, expiry: w.expiry, templateId: w.templateId || null, templateVersion: w.templateVersion || 0, source: clone(w.source), fields: clone(w.fields.filter((f) => !f.historic)), historicFields: clone(w.historicFields || []), paragraphs: clone(w.paragraphs) };
}
function draftFromWork(w, kind, label) {
  return { label: label, summary: kind === 'replace' ? '替换文件：新版本候选' : kind === 'upgrade' ? '模板升级：旧值保留、字段按新版' : '编辑内容', kind: kind, createdAt: todayStr(), content: contentFromWork(w) };
}
function saveDraftFromWork(navigate) {
  const w = work();
  if (!w || !w.title.trim()) {
    const f = qs('#w-title') || qs('#s1-title');
    if (f) { f.focus(); f.closest('.field') && f.closest('.field').classList.add('is-invalid'); }
    toast('保存草稿也需要标题：请先填写标题。', true);
    return false;
  }
  if (w.mode === 'import' || w.mode === 'manual') {
    const e = {
      id: uid('k'), home: w.home, title: w.title.trim(), category: w.category, lifecycle: 'draft', usage: w.usage, aiOn: w.aiOn, expiry: w.expiry, aiIssue: '',
      templateId: w.templateId || null, templateVersion: w.templateVersion || 0, updated: todayStr(), currentVersion: 0, draft: null,
      source: clone(w.source), fields: clone(w.fields.filter((f) => !f.historic)), paragraphs: clone(w.paragraphs), versions: []
    };
    DB.entries.unshift(e);
    if (w.fromRecord) DB.importRecords = DB.importRecords.filter((r) => r.id !== w.fromRecord);
    resetFlow();
    if (navigate) { state.route = { name: 'detail', view: e.home, id: e.id, tab: 'content' }; render(); }
    toast('已保存为草稿（未发布）。核对与字段都保留，随时可以继续。');
    return true;
  }
  const e = byId(w.entryId);
  e.draft = draftFromWork(w, w.mode === 'replace' ? 'replace' : 'edit', w.mode === 'replace' ? '替换文件草稿（新版本候选）' : '编辑草稿');
  resetFlow();
  if (navigate) { state.route = { name: 'detail', view: e.home, id: e.id, tab: 'content' }; render(); }
  toast('已保存草稿；当前发布版本继续可用，发布成功后才会替换。');
  return true;
}
function performPublish(w) {
  const fail = state.scenarioPublish === 'fail';
  const failMsg = '处理失败：新版本处理未完成，AI 客服暂不可用。可重试（演示）。';
  if (w.mode === 'import' || w.mode === 'manual') {
    const e = {
      id: uid('k'), home: w.home, title: w.title.trim(), category: w.category, lifecycle: 'published', usage: w.usage, aiOn: w.aiOn, expiry: w.expiry,
      aiIssue: (w.aiOn && fail) ? failMsg : '',
      templateId: w.templateId || null, templateVersion: w.templateVersion || 0, updated: todayStr(), currentVersion: 1, draft: null,
      source: clone(w.source), fields: clone(w.fields.filter((f) => !f.historic)), paragraphs: clone(w.paragraphs), versions: [{ v: 1, date: todayStr(), note: w.mode === 'manual' ? '手动创建' : '从文件导入' }]
    };
    e.versions[0].snapshot = makeSnapshot(e);
    DB.entries.unshift(e);
    if (w.fromRecord) DB.importRecords = DB.importRecords.filter((r) => r.id !== w.fromRecord);
    resetFlow();
    state.route = { name: 'detail', view: e.home, id: e.id, tab: 'content' };
    render();
    toast(!w.aiOn ? '已发布（演示）：AI 客服为未授权状态，不会进入客户回答。' : (fail ? '已发布；处理未完成：显示「已发布（AI 客服不可用）」（演示）' : '已发布（演示）：AI 客服已可用。'));
    return;
  }
  const e = byId(w.entryId);
  e.title = w.title.trim(); e.category = w.category; e.usage = w.usage; e.aiOn = w.aiOn; e.expiry = w.expiry;
  e.templateId = w.templateId || null; e.templateVersion = w.templateVersion || e.templateVersion;
  e.fields = clone(w.fields.filter((f) => !f.historic)); e.paragraphs = clone(w.paragraphs);
  if (w.mode === 'replace' && w.source) e.source = Object.assign(clone(w.source), { at: todayStr() });
  const wasDraft = (e.currentVersion || 0) === 0;
  const v = (e.currentVersion || 0) + 1;
  e.currentVersion = v;
  e.versions.push({ v: v, date: todayStr(), note: w.mode === 'replace' ? '替换文件：正文与字段更新' : (wasDraft ? '发布草稿' : '编辑更新') });
  e.versions[e.versions.length - 1].snapshot = makeSnapshot(e);
  e.lifecycle = 'published'; e.draft = null; e.updated = todayStr();
  e.aiIssue = (e.aiOn && fail) ? failMsg : '';
  resetFlow();
  state.route = { name: 'detail', view: e.home, id: e.id, tab: 'content' };
  render();
  toast('已发布 v' + v + (fail && e.aiOn ? '；处理未完成：显示「已发布（AI 客服不可用）」，可重试（演示）' : '（演示）'));
}
function publishGatesHTMLList(gates) { return gates.map((g) => '<div class="gate ' + (g.ok ? 'gate-ok' : 'gate-bad') + '"><svg class="ic"><use href="#i-' + (g.ok ? 'check' : 'alert') + '"/></svg><span>' + esc(g.text) + '</span></div>').join(''); }
function openPublishConfirm(w) {
  const inc = w.paragraphs.filter((p) => p.included);
  const aiFields = w.fields.filter((f) => !f.historic && f.checked && f.aiAllowed);
  openDialog({
    title: '对外发布确认', icon: 'check', iconCls: 'info',
    body: '<p>这条知识将对外提供给客户，并且已授权 AI 客服使用。确认后立即发布并替换当前线上版本。</p>' +
      kvRows([
        ['用途', '对外提供给客户'],
        ['AI 使用范围', '正文 ' + inc.length + ' 段 + 允许的字段 ' + aiFields.length + ' 个（' + (aiFields.map((f) => f.label).join('、') || '无') + '）'],
        ['有效期', fmtExpiry(w.expiry)],
        ['不会使用', '仅内部段落、未勾选「已核对」或未允许客户问答的字段、未包含的段落']
      ]),
    actions: [
      { label: '取消', cls: 'secondary' },
      { label: '确认发布', cls: '', onClick: () => { closeDialog(); performPublish(w); } }
    ]
  });
}
function doPublish() {
  const w = work();
  const gates = publishGates(w);
  if (gates.some((g) => !g.ok)) { toast('还有检查项未完成，不能发布；可以先保存草稿。', true); renderStepRegion(); return; }
  if (w.usage === 'external' && w.aiOn) openPublishConfirm(w); else performPublish(w);
}

/* ---------- 字段映射对话框（手动，不自动推断） ---------- */
function openMapDialog(p) {
  const w = work();
  const usable = w.fields.filter((f) => !f.historic);
  if (!w.templateId || !usable.length) { toast('这条知识没有模板字段：先在「知识模板」中选择模板，再回核对页。', true); return; }
  state.mapPara = p;
  const body = '<p>手动映射：由你选择来源段落并填写字段值；系统不会自动推断数值，也不会替你把段落当成参数。</p>' +
    '<fieldset style="border:0;padding:0;margin:0"><legend class="sr-input">选择目标字段</legend>' +
    usable.map((f, i) => '<label class="check-inline" style="margin:6px 0"><input type="radio" name="map-field" value="' + f.key + '"' + (i === 0 ? ' checked' : '') + '><span>' + esc(f.label) + ' <span class="tag type">' + esc(TYPE_LABEL[f.type]) + (f.unit ? ' · ' + esc(f.unit) : '') + '</span>' + (f.required ? ' <span class="req-star">必填</span>' : '') + '</span></label>').join('') +
    '</fieldset>' +
    '<label for="map-value">要使用的值（可修改）</label><input id="map-value" data-map="value" value="' + esc(p.text) + '">' +
    '<label for="map-unit">单位（数字字段；可留空）</label><input id="map-unit" data-map="unit" placeholder="例如 小时">' +
    '<label for="map-loc">来源位置（' + (isPara(w) ? '段' : '页') + '）</label><input id="map-loc" data-map="loc" type="number" min="1" value="' + (locOf(p) || 1) + '">' +
    '<p class="hint-line">填入后该字段标记为「原文」或「人工修改」（取决于你是否改过值），需要重新勾选「已核对」；「允许用于客户问答」默认关闭。</p>';
  openDialog({
    title: '用作字段值（手动映射）', icon: 'link', body: body, focus: '#map-value',
    actions: [
      { label: '取消', cls: 'secondary' },
      { label: '填入该字段', cls: '', onClick: () => {
          const dlg = qs('#dlg');
          const key = (qs('input[name="map-field"]:checked', dlg) || {}).value;
          const f = w.fields.find((x) => x.key === key) || usable[0];
          const value = qs('#map-value', dlg).value;
          const unit = qs('#map-unit', dlg).value.trim();
          const loc = Number(qs('#map-loc', dlg).value) || locOf(p) || 1;
          f.value = value;
          if (f.type === 'number') f.unit = unit || f.unit;
          f.loc = loc;
          const changed = value !== p.text;
          f.sourceKind = changed ? '人工修改' : '原文';
          f.modified = changed;
          f.checked = false;
          refreshFieldsRegion();
          toast('已填入字段「' + f.label + '」：请核对数值与单位，再勾选「已核对」。');
          closeDialog();
        } }
    ]
  });
}

/* ---------- 模板升级 / 恢复 ---------- */
function openUpgradeDialog(e) {
  const tpl = templateById(e.templateId);
  const hasDraft = !!e.draft;
  const baseVer = hasDraft ? ((e.draft.content && e.draft.content.templateVersion) || 0) : e.templateVersion;
  const from = versionOf(tpl, baseVer) || latestVersion(tpl);
  const to = latestVersion(tpl);
  const adds = to.fields.filter((f) => !from.fields.find((x) => x.key === f.key));
  const rems = from.fields.filter((f) => !to.fields.find((x) => x.key === f.key));
  const reqs = to.fields.filter((f) => { const o = from.fields.find((x) => x.key === f.key); return o && (!!o.required !== !!f.required); });
  let items = '';
  if (adds.length) items += '<p><b>新增字段：</b>' + adds.map((f) => esc(f.label) + '（' + esc(TYPE_LABEL[f.type]) + (f.required ? ' · 必填' : '') + '）').join('、') + '。新草稿中留空待填。</p>';
  if (rems.length) items += '<p><b>移除字段：</b>' + rems.map((f) => esc(f.label)).join('、') + '。旧值保留在历史与来源中，不会静默删除；已发布内容不变。</p>';
  if (reqs.length) items += '<p><b>必填变化：</b>' + reqs.map((f) => esc(f.label) + '（' + (f.required ? '变为必填' : '改为选填') + '）').join('、') + '。只约束采用新版之后的发布。</p>';
  const lead = hasDraft
    ? '<p>这条知识已有未发布草稿（' + esc(e.draft.label) + '）。确认后基于当前草稿从 v' + from.v + ' 升级到 v' + to.v + '：草稿中的正文与已填写字段值保留，升级结果仍是草稿；发布版本不变，取消不改任何值。也可以继续使用旧模板，不强制升级。</p>'
    : '<p>从 v' + from.v + ' 升级到 v' + to.v + '：只更新草稿，发布版本不变；取消不改任何值。也可以继续使用旧模板，不强制升级。</p>';
  openDialog({
    title: '采用新版模板：' + tpl.name + ' v' + to.v, icon: 'layout', body: lead + items +
      '<p class="hint-line">已填写过的字段保留原值；改过类型的字段需要重新填写（旧值保留在历史与来源中）。</p>',
    actions: [
      { label: '取消', cls: 'secondary' },
      { label: '确认采用（生成升级草稿）', cls: '', onClick: () => {
          const w = hasDraft ? workFromDraft(e) : workFromEntry(e, 'edit');
          const baseFields = w.fields || [];
          const historicPrev = clone(w.historicFields || []);
          w.fields = to.fields.map((f) => {
            const old = baseFields.find((x) => x.key === f.key);
            const sameType = !!old && old.type === f.type;
            return { key: f.key, label: f.label, type: f.type, unit: f.unit || '', options: (f.options || []).slice(), required: !!f.required, help: f.help || '', value: old ? old.value : '', checked: sameType ? !!old.checked : false, aiAllowed: sameType ? !!old.aiAllowed : false, sourceKind: old ? (old.sourceKind || '') : '', loc: (old && old.loc != null) ? old.loc : null, modified: old ? !!old.modified : false, historic: false };
          });
          const seen = {};
          w.historicFields = rems.map((f) => { const old = baseFields.find((x) => x.key === f.key); return { key: f.key, label: f.label, value: old ? old.value : '', historic: true }; })
            .concat(historicPrev)
            .filter((x) => (seen[x.key] ? false : (seen[x.key] = true)));
          w.templateVersion = to.v;
          w.step = 2;
          e.draft = draftFromWork(w, 'upgrade', '采用新版模板（v' + to.v + '）升级');
          state.work = w; state.workDirty = false;
          state.route = { name: 'review', view: e.home, id: e.id, tab: 'content' };
          render(); focusStepHeading();
          toast(hasDraft ? '已基于当前草稿生成升级草稿：草稿正文与已填字段保留、新字段留空待填；发布前线上版本保持原样。' : '已生成升级草稿：旧值已保留、新字段留空待填；发布前线上版本保持原样。');
        } }
    ]
  });
}
function openRestoreDialog(e, ver) {
  const v = e.versions.find((x) => x.v === ver);
  openDialog({
    title: '从 v' + ver + ' 创建新草稿', icon: 'history',
    body: '<p>「恢复」只会从历史版本创建一条新草稿，不会覆盖历史、不会直接替换线上版本；发布成功后才切换。</p>' +
      kvRows([['历史版本', 'v' + ver + '（' + v.date + '）'], ['内容摘要', v.note], ['快照', v.snapshot ? (v.snapshot.paragraphs ? v.snapshot.paragraphs.length : 0) + ' 段正文 · ' + ((v.snapshot.fields || []).length) + ' 个字段' : '无']]),
    actions: [
      { label: '取消', cls: 'secondary' },
      { label: '创建新草稿', cls: '', onClick: () => {
          const snap = clone(v.snapshot);
          e.draft = { label: '恢复草稿', summary: '从 v' + ver + ' 恢复（不覆盖历史）', kind: 'edit', createdAt: todayStr(),
            content: { title: snap.title || e.title, category: e.category, usage: snap.usage || e.usage, aiOn: snap.aiOn != null ? snap.aiOn : e.aiOn, expiry: e.expiry, templateId: e.templateId, templateVersion: e.templateVersion, source: clone(e.source), fields: snap.fields || [], historicFields: [], paragraphs: snap.paragraphs || [] } };
          render();
          toast('已从 v' + ver + ' 创建新草稿；当前发布版本未变化。');
        } }
    ]
  });
}
function openDisableDialog(e) {
  openDialog({
    title: '停用这条知识', icon: 'alert', iconCls: 'danger',
    body: '<p>停用后：不再对外展示、不再用于 AI 客服回答；企业成员仍可阅读。随时可以重新启用。</p>',
    actions: [
      { label: '取消', cls: 'secondary' },
      { label: '停用', cls: 'danger', onClick: () => { e.lifecycle = 'disabled'; render(); toast('已停用（演示）。'); } }
    ]
  });
}

/* ---------- 启动核对 / 编辑 / 替换 ---------- */
function startImport() { state.work = newWork('import', {}); state.workDirty = false; state.route = { name: 'review', view: 'file', id: null, tab: 'content' }; render(); focusStepHeading(); }
function startManual(shape, view) {
  const home = shape || 'article';
  const w = newWork('manual', { home: home, templateDecided: false });
  w.category = home === 'product_service' ? '服务说明' : '常见问题';
  state.work = w; state.workDirty = false;
  state.route = { name: 'review', view: view || (home === 'product_service' ? 'product_service' : 'article'), id: null, tab: 'content' };
  render(); focusStepHeading();
}
function startReplace(id) {
  const e = byId(id);
  const w = newWork('replace', { entryId: e.id, home: e.home, title: e.title, category: e.category, usage: e.usage, aiOn: e.aiOn, expiry: e.expiry, templateId: e.templateId, templateDecided: true, templateVersion: e.templateVersion });
  state.work = w; state.workDirty = false;
  state.route = { name: 'review', view: e.home, id: e.id, tab: 'content' };
  render(); focusStepHeading();
}
function startEdit(id) {
  const e = byId(id);
  const w = workFromEntry(e, e.lifecycle === 'draft' ? 'edit' : 'edit');
  state.work = w; state.workDirty = false;
  if (w.source && w.source.file) { w.pages = pagesFromParas(w.paragraphs); }
  state.route = { name: 'review', view: e.home, id: e.id, tab: 'content' };
  w.step = 2; render(); focusStepHeading();
}
function continueDraft(id) {
  const e = byId(id);
  state.work = workFromDraft(e); state.workDirty = false;
  state.route = { name: 'review', view: e.home, id: e.id, tab: 'content' };
  render(); focusStepHeading();
}

/* ---------- 局部刷新 ---------- */
function refreshStep1Status() { const el = qs('#step1-status'); if (el) el.innerHTML = step1StatusHTML(); refreshStep1Next(); }
function refreshStep1Next() {
  const btn = qs('#step1-next'); if (btn) btn.disabled = !step1Ready();
  const r = qs('#step1-next-reason'); if (r) r.innerHTML = step1ReasonHTML();
}
function refreshTmplRegion() { const el = qs('#tmpl-region'); if (el) el.innerHTML = tmplRegionHTML(); }
function updateFieldCardMicro(f) {
  const card = qs('.field-card[data-key="' + f.key + '"]');
  if (!card) return;
  const src = qs('[data-role="fsrc"]', card);
  if (src) src.textContent = fieldSrcText(f, work());
  const cb = qs('[data-act="field-checked"]', card);
  if (cb) cb.checked = f.checked;
  const label = qs('.src-label', card);
  if (f.modified && label && !qs('.tag.edit', label)) label.insertAdjacentHTML('beforeend', ' <span class="tag edit">人工修改</span>');
}
function setFieldValue(f, el) {
  if (String(f.value) === String(el.value)) return;
  f.value = el.value;
  f.modified = true;
  f.checked = false;
  f.sourceKind = (f.sourceKind === '原文') ? '人工修改' : (f.sourceKind || '手动填写');
  markWorkDirty();
  updateFieldCardMicro(f);
}
function focusAnchor(pid) {
  const w = work(); if (!w) return;
  const p = w.paragraphs.find((x) => x.id === pid); if (!p) return;
  const n = locOf(p);
  if (n != null && w.pages.length) w.curPage = Math.min(Math.max(1, n), w.pages.length);
  refreshOrig();
  qsa('.orig-block').forEach((b) => b.classList.remove('hl'));
  const b = qs('.orig-block[data-anchor="' + pid + '"]');
  if (b) { b.classList.add('hl'); b.scrollIntoView({ block: 'nearest' }); }
}
function pageNav(d) {
  const w = work(); if (!w || !w.pages.length) return;
  w.curPage = Math.min(Math.max(1, w.curPage + d), w.pages.length);
  refreshOrig();
  const prev = qs('[data-act="page-prev"]'); const next = qs('[data-act="page-next"]'); const lab = qs('.pg-label');
  if (prev) prev.disabled = w.curPage <= 1;
  if (next) next.disabled = w.curPage >= w.pages.length;
  if (lab) lab.textContent = '第 ' + w.curPage + ' / ' + w.pages.length + ' ' + (isPara(w) ? '段' : '页');
}
function wizardCancel() {
  const w = work(); if (!w) return;
  const go = () => {
    const mode = w.mode; const entryId = w.entryId;
    resetFlow();
    if ((mode === 'replace' || mode === 'edit') && entryId && byId(entryId)) {
      state.route = { name: 'detail', view: byId(entryId).home, id: entryId, tab: 'content' };
    } else {
      state.route = { name: 'list', view: 'file', id: null, tab: 'content' };
    }
    render();
  };
  if (state.workDirty) requestLeave(go); else go();
}
function retryEntry(e) {
  if (state.scenarioPublish === 'fail') {
    toast('当前演示场景为「发布后处理失败」：重试仍会显示失败原因。把核对页第 3 步的演示场景切到「处理正常」后，再点重试即可看到恢复（演示，不用计时器）。', true);
    return;
  }
  e.aiIssue = '';
  render();
  toast('重试完成（演示）：AI 客服已可用。');
}

/* ---------- 事件 ---------- */
function onClick(e) {
  const dlgBtn = e.target.closest('[data-dlg]');
  if (dlgBtn) {
    const i = Number(dlgBtn.getAttribute('data-dlg'));
    const a = state.dlgActions[i];
    if (a) {
      if (a.onClick) { const r = a.onClick(); if (r !== 'keep') closeDialog(); }
      else closeDialog();
    }
    return;
  }
  const el = e.target.closest('[data-act]');
  if (!el) return;
  const tag = el.tagName;
  if (tag === 'INPUT' || tag === 'SELECT' || tag === 'TEXTAREA') return;
  const act = el.getAttribute('data-act');
  const w = work();
  switch (act) {
    case 'nav': {
      const v = el.getAttribute('data-view');
      if (v === '__out') { toast('该入口不在第二批评审范围（演示占位，不跳转）。', true); return; }
      closeMobileNav();
      if (v === '__templates') { requestLeave(() => { resetFlow(); state.route = { name: 'templates', view: 'all', id: null, tab: 'content' }; render(); }); return; }
      if (v === '__qa') { requestLeave(() => { resetFlow(); state.route = { name: 'qa', view: 'all', id: null, tab: 'content' }; render(); }); return; }
      requestLeave(() => { resetFlow(); state.route = { name: 'list', view: v, id: null, tab: 'content' }; render(); });
      return;
    }
    case 'nav-toggle': state.navExpanded = !state.navExpanded; syncNavExpandedDOM(); return;
    case 'row-open': state.route = { name: 'detail', view: state.route.view, id: el.getAttribute('data-id'), tab: 'content' }; render(); window.scrollTo({ top: 0 }); return;
    case 'rec-manual': {
      const rec = DB.importRecords.find((r) => r.id === el.getAttribute('data-id')); if (!rec) return;
      state.work = newWork('import', { fromRecord: rec.id, pending: { file: rec.file, native: false, kind: rec.kind, sample: null } });
      state.work.scenario = 'scan';
      state.workDirty = false;
      state.route = { name: 'review', view: 'file', id: null, tab: 'content' };
      render(); focusStepHeading();
      toast('已把该文件带到上传流程：选择模板后点「开始核对」，可继续人工整理。');
      return;
    }
    case 'rec-remove': {
      DB.importRecords = DB.importRecords.filter((r) => r.id !== el.getAttribute('data-id'));
      refreshListRegion();
      toast('已移除导入记录（演示）。');
      return;
    }
    case 'open-import': {
      const det = el.closest('details'); if (det) det.open = false;
      requestLeave(() => { resetFlow(); startImport(); });
      return;
    }
    case 'open-manual': {
      const det = el.closest('details'); if (det) det.open = false;
      const shape = el.getAttribute('data-shape') || (state.route.view === 'product_service' ? 'product_service' : 'article');
      requestLeave(() => { resetFlow(); startManual(shape, state.route.view === 'all' ? 'all' : state.route.view); });
      return;
    }
    case 'pick-file': { const fi = qs('#file-input'); if (fi) fi.click(); return; }
    case 'sample-pdf': if (w) { w.pending = { file: PDF_SAMPLE.file, kind: PDF_SAMPLE.kind, native: false, sample: 'pdf' }; refreshStep1Status(); } return;
    case 'sample-docx': if (w) { w.pending = { file: DOCX_SAMPLE.file, kind: DOCX_SAMPLE.kind, native: false, sample: 'docx' }; refreshStep1Status(); } return;
    case 'pick-template': openDrawer(); return;
    case 'manage-templates': {
      if (qs('#drawer') && qs('#drawer').open) closeDrawer();
      requestLeave(() => { resetFlow(); state.route = { name: 'templates', view: 'all', id: null, tab: 'content' }; render(); });
      return;
    }
    case 'drawer-cancel': closeDrawer(); return;
    case 'drawer-confirm': {
      if (!w) return;
      const pick = state.drawerPick || '__none';
      const had = w.fields.some((f) => String(f.value).trim() !== '' || f.checked || f.aiAllowed);
      if (pick === '__none') { w.templateId = null; w.templateDecided = true; w.templateVersion = 0; }
      else { const tpl = templateById(pick); w.templateId = pick; w.templateDecided = true; w.templateVersion = latestVersion(tpl).v; }
      w.fields = buildFieldInstances(w.templateId, w.templateVersion);
      closeDrawer();
      if (had) toast('已更换模板：字段按新模板重新生成（演示不迁移已填写内容）。', true);
      refreshTmplRegion(); refreshStep1Next();
      return;
    }
    case 'start-check': beginCheck(); return;
    case 'step-back': if (w) { w.step = 1; render(); focusStepHeading(); } return;
    case 'to-step3': if (w) { w.step = 3; render(); focusStepHeading(); } return;
    case 'to-step2': if (w) { w.step = 2; render(); focusStepHeading(); } return;
    case 'save-draft': saveDraftFromWork(true); return;
    case 'publish': doPublish(); return;
    case 'wizard-cancel': wizardCancel(); return;
    case 'para-source': focusAnchor(el.getAttribute('data-id')); return;
    case 'cmp-focus': focusAnchor(el.getAttribute('data-id')); return;
    case 'page-prev': pageNav(-1); return;
    case 'page-next': pageNav(1); return;
    case 'para-del': {
      if (!w) return;
      w.paragraphs = w.paragraphs.filter((p) => p.id !== el.getAttribute('data-id'));
      markWorkDirty(); renderStepRegion();
      return;
    }
    case 'add-para': {
      if (!w) return;
      w.paragraphs.push({ id: uid('p'), text: '', loc: null, included: true, checked: false, internal: false, needsReview: false, reviewNote: '', modified: true });
      markWorkDirty(); renderStepRegion();
      const tas = qsa('[data-act="para-text"]'); if (tas.length) tas[tas.length - 1].focus();
      return;
    }
    case 'use-as-field': {
      const p = w && w.paragraphs.find((x) => x.id === el.getAttribute('data-id'));
      if (p) openMapDialog(p);
      return;
    }
    case 'drop-internal-para': {
      if (!w) return;
      w.paragraphs.forEach((p) => { if (p.internal) p.included = false; });
      markWorkDirty(); renderStepRegion();
      toast('已取消包含内部段落；内容保留在私有来源中，不进入正文与 AI 回答。');
      return;
    }
    case 'make-internal': {
      if (!w) return;
      w.usage = 'internal';
      markWorkDirty(); renderStepRegion();
      toast('已把整条知识改为内部使用；如需对客户提供，请另外新建对外知识（演示）。');
      return;
    }
    case 'd-tab': state.route = { name: 'detail', view: state.route.view, id: el.getAttribute('data-id'), tab: el.getAttribute('data-tab') }; render(); return;
    case 'd-edit': startEdit(el.getAttribute('data-id')); return;
    case 'd-replace': startReplace(el.getAttribute('data-id')); return;
    case 'd-disable': { const e0 = byId(el.getAttribute('data-id')); if (e0) openDisableDialog(e0); return; }
    case 'd-enable': { const e0 = byId(el.getAttribute('data-id')); if (e0) { e0.lifecycle = 'published'; render(); toast('已重新启用（演示）：AI 客服状态取决于授权与处理结果。'); } return; }
    case 'd-retry': { const e0 = byId(el.getAttribute('data-id')); if (e0) retryEntry(e0); return; }
    case 'd-upgrade': { const e0 = byId(el.getAttribute('data-id')); if (e0) openUpgradeDialog(e0); return; }
    case 'd-continue-draft': continueDraft(el.getAttribute('data-id')); return;
    case 'd-restore': { const e0 = byId(el.getAttribute('data-id')); if (e0) openRestoreDialog(e0, Number(el.getAttribute('data-ver'))); return; }
    case 't-edit': openBuilder({ id: el.getAttribute('data-id') }); return;
    case 'tpl-new': openBuilder({}); return;
    case 'b-cancel': {
      if (state.builderDirty) { state.pendingNav = () => { state.builder = null; state.builderDirty = false; state.route = { name: 'templates', view: 'all', id: null, tab: 'content' }; render(); }; openUnsavedBuilderDialog(); }
      else { state.builder = null; state.route = { name: 'templates', view: 'all', id: null, tab: 'content' }; render(); }
      return;
    }
    case 'b-save': {
      const err = validateBuilder();
      if (err) { builderErr(err); toast(err, true); return; }
      builderErr('');
      const b = state.builder;
      if (b.isNew) { commitBuilder(); return; }
      const tpl = templateById(b.id); const base = latestVersion(tpl);
      const diff = diffTemplate({ name: tpl.name, shape: tpl.shape, fields: base.fields }, b);
      if (!diff.length) { toast('没有修改，未生成新版本。', true); return; }
      openDialog({
        title: '保存为新版本 v' + (base.v + 1), icon: 'layout',
        body: '<p>以下变化会写入新版本；已有知识继续使用原版本，不会自动迁移，也不会改变已发布快照与历史引用。</p>' + changeList(diff),
        actions: [{ label: '取消', cls: 'secondary' }, { label: '保存为新版本 v' + (base.v + 1), cls: '', onClick: () => { closeDialog(); commitBuilder(); } }]
      });
      return;
    }
    case 'bf-add': {
      const b = state.builder; if (!b) return;
      b.fields.push({ key: uid('f'), label: '', type: 'text', unit: '', options: [], required: false, help: '', orig: null });
      state.builderDirty = true; render();
      const inputs = qsa('[data-act="bf-label"]'); if (inputs.length) inputs[inputs.length - 1].focus();
      return;
    }
    case 'bf-up': case 'bf-down': {
      const b = state.builder; if (!b) return;
      const key = el.getAttribute('data-key');
      const i = b.fields.findIndex((f) => f.key === key); if (i < 0) return;
      const j = act === 'bf-up' ? i - 1 : i + 1;
      if (j < 0 || j >= b.fields.length) return;
      const tmp = b.fields[i]; b.fields[i] = b.fields[j]; b.fields[j] = tmp;
      state.builderDirty = true; render();
      const again = qs('[data-act="' + act + '"][data-key="' + key + '"]'); if (again) again.focus();
      return;
    }
    case 'bf-del': {
      const b = state.builder; if (!b) return;
      b.fields = b.fields.filter((f) => f.key !== el.getAttribute('data-key'));
      state.builderDirty = true; render();
      return;
    }
    case 'search-clear': {
      state.filters.q = '';
      const input = qs('#search-q'); if (input) { input.value = ''; input.focus(); }
      const c = qs('#search-clear'); if (c) c.hidden = true;
      refreshListRegion();
      return;
    }
    default: return;
  }
}
function onInput(e) {
  if (e.target.id === 'search-q') {
    state.filters.q = e.target.value;
    const c = qs('#search-clear'); if (c) c.hidden = !state.filters.q;
    refreshListRegion();
    return;
  }
  const el = e.target.closest('[data-act]');
  if (!el) return;
  const act = el.getAttribute('data-act');
  const w = work();
  switch (act) {
    case 'para-text': {
      if (!w) return;
      const p = w.paragraphs.find((x) => x.id === el.getAttribute('data-id')); if (!p) return;
      if (p.text === el.value) return;
      p.text = el.value; p.modified = true; p.checked = false;
      markWorkDirty();
      const card = el.closest('.para-card');
      if (card) {
        const cb = qs('[data-act="para-checked"]', card); if (cb) cb.checked = false;
        const head = qs('.para-head', card);
        if (head && !qs('.tag.edit', head)) {
          const srcBtn = qs('[data-act="para-source"]', head);
          if (srcBtn) srcBtn.insertAdjacentHTML('beforebegin', '<span class="tag edit">人工修改</span>');
        }
      }
      refreshParaMeta();
      return;
    }
    case 'field-value': {
      if (!w) return;
      const f = w.fields.find((x) => x.key === el.getAttribute('data-key')); if (!f) return;
      setFieldValue(f, el);
      return;
    }
    case 'w-title': if (w) { w.title = el.value; markWorkDirty(); if (el.closest('.field')) el.closest('.field').classList.remove('is-invalid'); } return;
    case 's1-title': if (w) { w.title = el.value; markWorkDirty(); } return;
    case 'bf-name': {
      const b = state.builder; if (!b) return;
      b.name = el.value; state.builderDirty = true;
      return;
    }
    case 'bf-label': case 'bf-help': case 'bf-unit': case 'bf-options': {
      const b = state.builder; if (!b) return;
      const f = b.fields.find((x) => x.key === el.getAttribute('data-key')); if (!f) return;
      if (act === 'bf-label') f.label = el.value;
      if (act === 'bf-help') f.help = el.value;
      if (act === 'bf-unit') f.unit = el.value;
      if (act === 'bf-options') f.options = parseOptions(el.value);
      state.builderDirty = true;
      refreshPreview();
      return;
    }
    default: return;
  }
}
function onChange(e) {
  const el = e.target.closest('[data-act]');
  if (e.target.id === 'file-input') {
    const fi = e.target;
    const name = (fi.files && fi.files[0]) ? fi.files[0].name : '未命名文件';
    const w = work(); if (!w) return;
    w.pending = { file: name, native: true, kind: '本地文件', sample: null };
    refreshStep1Status();
    return;
  }
  if (!el) return;
  const act = el.getAttribute('data-act');
  const w = work();
  switch (act) {
    case 'para-included': { if (!w) return; const p = w.paragraphs.find((x) => x.id === el.getAttribute('data-id')); if (p) { p.included = el.checked; markWorkDirty(); refreshParaMeta(); } return; }
    case 'para-checked': { if (!w) return; const p = w.paragraphs.find((x) => x.id === el.getAttribute('data-id')); if (p) { p.checked = el.checked; markWorkDirty(); refreshParaMeta(); } return; }
    case 'field-checked': { if (!w) return; const f = w.fields.find((x) => x.key === el.getAttribute('data-key')); if (f) { f.checked = el.checked; markWorkDirty(); } return; }
    case 'field-ai': { if (!w) return; const f = w.fields.find((x) => x.key === el.getAttribute('data-key')); if (f) { f.aiAllowed = el.checked; markWorkDirty(); } return; }
    case 'field-value': { if (!w) return; const f = w.fields.find((x) => x.key === el.getAttribute('data-key')); if (f) setFieldValue(f, el); return; }
    case 'scenario-import': if (w) w.scenario = el.value; return;
    case 'scenario-publish': state.scenarioPublish = el.value; return;
    case 'w-usage': if (w) { w.usage = el.value; markWorkDirty(); if (w.step === 2) refreshParaMeta(); else renderStepRegion(); } return;
    case 'w-ai': if (w) { w.aiOn = el.checked; markWorkDirty(); } return;
    case 'w-expiry': if (w) { w.expiry = el.value; markWorkDirty(); } return;
    case 'w-category': if (w) { w.category = el.value; markWorkDirty(); } return;
    case 's1-category': if (w) { w.category = el.value; markWorkDirty(); } return;
    case 'bf-type': { const b = state.builder; if (!b) return; const f = b.fields.find((x) => x.key === el.getAttribute('data-key')); if (f) { f.type = el.value; state.builderDirty = true; render(); } return; }
    case 'bf-required': { const b = state.builder; if (!b) return; const f = b.fields.find((x) => x.key === el.getAttribute('data-key')); if (f) { f.required = el.checked; state.builderDirty = true; refreshPreview(); } return; }
    case 'bf-shape': { const b = state.builder; if (!b) return; b.shape = el.value; state.builderDirty = true; return; }
    case 'drawer-tmpl': {
      state.drawerPick = el.value;
      qsa('#drawer .tmpl-option').forEach((opt) => { const r = qs('input', opt); opt.classList.toggle('on', r && r.value === state.drawerPick); });
      return;
    }
    case 'filter-type': state.filters.type = el.value; refreshListRegion(); return;
    case 'filter-status': state.filters.status = el.value; refreshListRegion(); return;
    default: return;
  }
}
function closeMobileNav() { document.body.classList.remove('nav-open'); const b = qs('#menu-btn'); if (b) b.setAttribute('aria-expanded', 'false'); const s = qs('#side-scrim'); if (s) s.hidden = true; }

/* ---------- 初始化 ---------- */
function init() {
  seedEntries();
  render();
  document.addEventListener('click', onClick);
  document.addEventListener('input', onInput);
  document.addEventListener('change', onChange);
  const dlg = qs('#dlg');
  dlg.addEventListener('close', () => { if (state.lastFocus && state.lastFocus.focus) state.lastFocus.focus(); });
  const drawer = qs('#drawer');
  drawer.addEventListener('close', () => { if (state.lastFocus && state.lastFocus.focus) state.lastFocus.focus(); });
  const roleSel = qs('#role-select');
  roleSel.addEventListener('change', () => { state.role = roleSel.value; render(); if (state.role === 'reader') toast('已切换为「仅阅读成员」演示视角：编辑与模板入口隐藏（演示界面差异，不代表后端权限）。'); });
  const menuBtn = qs('#menu-btn');
  menuBtn.addEventListener('click', () => {
    const open = !document.body.classList.contains('nav-open');
    document.body.classList.toggle('nav-open', open);
    menuBtn.setAttribute('aria-expanded', open ? 'true' : 'false');
    const s = qs('#side-scrim'); if (s) s.hidden = !open;
  });
  const scrim = qs('#side-scrim');
  scrim.addEventListener('click', closeMobileNav);
  document.addEventListener('keydown', (e) => { if (e.key === 'Escape') closeMobileNav(); });
}
init();
