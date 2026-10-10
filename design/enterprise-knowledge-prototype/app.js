'use strict';
/* ════════════════════════════════════════════════════════════════
   企业知识管理 · 设计评审原型（纯前端演示，无依赖、无外部请求）
   ─ 数据全部在内存中，刷新即重置；不上传文件、不调用模型。
   ─ 生命周期仅：草稿 / 已发布 / 已停用。
   ─ “AI客服可用/不可用”由系统计算（发布＋对外＋授权＋有效＋
     当前版本准备完成），界面上不允许手动选择就绪状态。
   ════════════════════════════════════════════════════════════════ */

const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => Array.from(el.querySelectorAll(s));
const esc = (s) => String(s == null ? '' : s).replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const clone = (o) => JSON.parse(JSON.stringify(o));
const DEMO_TODAY = '2026-10-10';           // 演示固定“今天”，保证状态稳定
const MAX_IMPORT_BYTES = 1024 * 1024;      // 本地文本读取上限 1MB

const CATEGORIES = ['FAQ', '服务政策', '企业文化', '培训', '文件资料'];
const PRODUCT_CATEGORIES = ['产品', '专业服务'];
const TYPE_OF_CATEGORY = { 'FAQ': 'FAQ', '服务政策': '文章', '企业文化': '文章', '培训': '文章', '文件资料': '文件资料', '产品': '产品', '专业服务': '服务' };

const REASON_HINT = {
  '仅内部': '仅内部资料：不用于客户问答（企业成员均可查看）。',
  '未授权': '未获得「允许AI客服使用」授权。',
  '准备中': '当前版本仍在准备中，系统处理完成后自动生效。',
  '处理失败': '当前版本处理失败，可在下方重试。',
  '过期': '已超过有效期，不再用于问答。',
  '尚未发布': '草稿尚未发布。',
  '已停用': '已停用：不再出现在任何问答中。'
};

/* ─────────────── 演示数据（12 条，覆盖 6+ 类） ─────────────── */
function seedEntries() {
  return [
    {
      id: 'k-001', type: 'FAQ', category: 'FAQ', title: '发货与物流常见问题',
      summary: '发货时效、物流选择与运费口径。',
      usage: 'external', aiAuthorized: true, status: 'published',
      prepared: true, processState: 'idle', validUntil: null, needsReview: false,
      owner: '王倩', updatedAt: '2026-09-20 10:12', currentVersion: 'v2',
      body: '', fields: [], related: [],
      faq: [
        { q: '下单后多久发货？', a: '现货产品在订单确认后 3 个工作日内发出；定制型号以合同约定交期为准。' },
        { q: '可以指定物流公司吗？', a: '华东、华南地区默认顺丰或德邦；其他地区可与销售确认。' },
        { q: '运费怎么计算？', a: '按体积重与实际重量取大者计价；偏远地区可能加收附加费，下单前会先确认。' }
      ],
      sources: [{ id: 's1', kind: 'manual', label: '手动创建', detail: '由 王倩 在控制台创建并维护' }],
      versions: [
        { ver: 'v1', date: '2026-08-12', author: '王倩', summary: '创建并发布', snapshot: { body: '', faq: [{ q: '下单后多久发货？', a: '现货产品在订单确认后 3 个工作日内发出。' }, { q: '可以指定物流公司吗？', a: '华东、华南地区默认顺丰或德邦。' }] } },
        { ver: 'v2', date: '2026-09-20', author: '王倩', summary: '补充运费口径' }
      ],
      draft: null,
      qa: { keywords: ['发货', '物流', '运费', '快递', '几天', '到货'], answer: '现货订单一般在订单确认后 3 个工作日内发出；定制型号以合同约定交期为准。运费按体积重与实际重量取大者计价。' }
    },
    {
      id: 'k-002', type: '文章', category: '服务政策', title: '整机保修与售后响应政策',
      summary: '保修期、故障响应与不保修范围。',
      usage: 'external', aiAuthorized: true, status: 'published',
      prepared: true, processState: 'idle', validUntil: null, needsReview: false,
      owner: '王倩', updatedAt: '2026-09-28 16:40', currentVersion: 'v3',
      body: '整机保修期自客户验收之日起 12 个月。\n保修期内出现故障，我们承诺在 48 小时内给出处理方案。\n耗材、人为损坏及超出使用环境说明的情形不在保修范围内；具体判定以服务记录为准。',
      fields: [], related: ['3SV08NL007T 立式多级离心泵'], faq: [],
      sources: [{ id: 's1', kind: 'manual', label: '手动创建', detail: '由 王倩 在控制台创建并维护' }],
      versions: [
        { ver: 'v1', date: '2026-06-02', author: '王倩', summary: '创建并发布', snapshot: { body: '整机保修期自客户验收之日起 12 个月。', faq: [] } },
        { ver: 'v2', date: '2026-08-15', author: '王倩', summary: '增加响应时限', snapshot: { body: '整机保修期自客户验收之日起 12 个月。\n保修期内出现故障，我们承诺在 48 小时内给出处理方案。', faq: [] } },
        { ver: 'v3', date: '2026-09-28', author: '王倩', summary: '明确不保修范围' }
      ],
      draft: null,
      qa: { keywords: ['保修', '质保', '售后', '维修', '坏了', '故障'], answer: '整机保修期自验收之日起 12 个月；保修期内出现故障，我们在 48 小时内给出处理方案；耗材与人为损坏不在保修范围内。' }
    },
    {
      id: 'k-003', type: '文章', category: '服务政策', title: '2026 秋季展会接待与报价政策',
      summary: '展会期间下单优惠与接待安排。',
      usage: 'external', aiAuthorized: true, status: 'published',
      prepared: true, processState: 'idle', validUntil: '2026-10-05', needsReview: false,
      owner: '李帆', updatedAt: '2026-09-15 09:30', currentVersion: 'v1',
      body: '2026 秋季展会（9 月 18 日–10 月 5 日）期间：现场登记客户享受 3% 下单优惠；报价单有效期至 10 月 15 日。\n本政策有效期至 2026-10-05，过期后不再作为对外承诺。',
      fields: [], related: [], faq: [],
      sources: [{ id: 's1', kind: 'manual', label: '手动创建', detail: '由 李帆 在控制台创建' }],
      versions: [{ ver: 'v1', date: '2026-09-15', author: '李帆', summary: '创建并发布' }],
      draft: null,
      qa: { keywords: ['展会', '报价政策', '展会政策', '优惠'], answer: '' }
    },
    {
      id: 'k-004', type: '文章', category: '企业文化', title: '公司简介与品牌故事',
      summary: '对外展示的企业概况。',
      usage: 'external', aiAuthorized: true, status: 'published',
      prepared: true, processState: 'idle', validUntil: null, needsReview: false,
      owner: '赵敏', updatedAt: '2026-07-08 11:00', currentVersion: 'v2',
      body: '示例企业成立于 2009 年，专注工业供水设备的设计与制造，目前在华东与华南设有两个生产基地。\n我们坚持以可核验的产品事实与长期服务陪伴客户。（演示数据）',
      fields: [], related: [], faq: [],
      sources: [{ id: 's1', kind: 'manual', label: '手动创建', detail: '由 赵敏 在控制台创建并维护' }],
      versions: [
        { ver: 'v1', date: '2026-03-20', author: '赵敏', summary: '创建并发布', snapshot: { body: '示例企业成立于 2009 年，专注工业供水设备。（演示数据）', faq: [] } },
        { ver: 'v2', date: '2026-07-08', author: '赵敏', summary: '补充生产基地信息' }
      ],
      draft: null,
      qa: { keywords: ['公司', '简介', '历史', '成立', '品牌'], answer: '示例企业成立于 2009 年，专注工业供水设备的设计与制造，目前在华东与华南设有两个生产基地。' }
    },
    {
      id: 'k-005', type: '文章', category: '企业文化', title: '工厂参观接待说明（内部）',
      summary: '内部使用的参观预约与接待流程。',
      usage: 'internal', aiAuthorized: false, status: 'published',
      prepared: true, processState: 'idle', validUntil: null, needsReview: false,
      owner: '赵敏', updatedAt: '2026-05-30 14:20', currentVersion: 'v1',
      body: '参观需由销售或渠道伙伴提前 5 个工作日提交预约单。\n接待流程：前台登记 → 安全告知 → 车间参观（禁止拍摄未公开产品）→ 会议室交流。',
      fields: [], related: [], faq: [],
      sources: [{ id: 's1', kind: 'manual', label: '手动创建', detail: '由 赵敏 在控制台创建' }],
      versions: [{ ver: 'v1', date: '2026-05-30', author: '赵敏', summary: '创建并发布' }],
      draft: null,
      qa: { keywords: ['参观', '工厂参观', '预约参观'], answer: '' }
    },
    {
      id: 'k-006', type: '文章', category: '培训', title: '新员工产品培训 · 第一课',
      summary: '内部培训课件正文（节选）。',
      usage: 'internal', aiAuthorized: false, status: 'published',
      prepared: true, processState: 'idle', validUntil: null, needsReview: false,
      owner: '陈启', updatedAt: '2026-08-01 10:05', currentVersion: 'v1',
      body: '第一课：产品线总览与常见客户问题。\n1. 三类主力产品与适用场景；\n2. 报价与交期口径（内部）；\n3. 客户常问问题与标准回应。',
      fields: [], related: [], faq: [],
      sources: [{ id: 's1', kind: 'manual', label: '手动创建', detail: '由 陈启 在控制台创建' }],
      versions: [{ ver: 'v1', date: '2026-08-01', author: '陈启', summary: '创建并发布' }],
      draft: null
    },
    {
      id: 'k-007', type: '产品', category: '产品', title: '3SV08NL007T 立式多级离心泵',
      summary: '水泵产品详情（演示：该产品自己的字段，不是通用知识模板）。',
      usage: 'external', aiAuthorized: false, status: 'published',
      prepared: true, processState: 'idle', validUntil: null, needsReview: false,
      owner: '周航', updatedAt: '2026-09-12 15:10', currentVersion: 'v2',
      body: '楼宇供水增压用途的立式多级离心泵配置。字段由这类产品自己的模板维护；结构化的产品字段与文章正文是两种不同的知识形态。',
      fields: [
        { label: '目录流量点', value: '0–4.4', unit: 'm³/h', usage: 'external', state: 'approved', source: '系列目录 Rev.B' },
        { label: '对应扬程点', value: '60–27.5', unit: 'm', usage: 'external', state: 'approved', source: '系列目录 Rev.B' },
        { label: '额定功率', value: '0.75', unit: 'kW', usage: 'external', state: 'approved', source: '型号记录' },
        { label: '叶轮级数', value: '8', unit: '级', usage: 'external', state: 'approved', source: '型号记录' },
        { label: '过流部件材质', value: 'AISI 316（口径待核准）', unit: '', usage: 'external', state: 'pending', source: '待核准' },
        { label: '内部成本口径', value: '见采购台账（不对外）', unit: '', usage: 'internal', state: 'approved', source: '内部' }
      ],
      related: ['报价单 RFQ-2026-0912', '推荐组合：楼宇增压方案'], faq: [],
      sources: [
        { id: 's1', kind: 'manual', label: '手动创建', detail: '产品条目由 周航 维护' },
        { id: 's2', kind: 'reference', label: '来源说明', detail: '系列目录 Rev.B（企业自行保管，未附文件）' }
      ],
      versions: [
        { ver: 'v1', date: '2026-07-30', author: '周航', summary: '创建并发布', snapshot: { body: '楼宇供水增压用途的立式多级离心泵配置。', faq: [], fields: [{ label: '目录流量点', value: '0–4.4', unit: 'm³/h', usage: 'external', state: 'approved', source: '系列目录 Rev.B' }] } },
        { ver: 'v2', date: '2026-09-12', author: '周航', summary: '补充扬程点与功率字段' }
      ],
      draft: null,
      qa: { keywords: ['3sv08', '流量', '扬程', '型号', '泵'], answer: '3SV08NL007T 的目录流量点为 0–4.4 m³/h，对应扬程 60–27.5 m；额定功率 0.75 kW、8 级叶轮。答复只引用该产品「对外且已核准」的字段，内部字段不参与。' }
    },
    {
      id: 'k-008', type: '产品', category: '产品', title: 'GX-9 变频供水机组',
      summary: '机组产品条目（演示“处理失败 → 重试”）。',
      usage: 'external', aiAuthorized: true, status: 'published',
      prepared: false, processState: 'failed', validUntil: null, needsReview: false,
      owner: '周航', updatedAt: '2026-10-09 17:45', currentVersion: 'v1',
      body: '面向中小型楼宇的成套变频供水机组。',
      fields: [
        { label: '适配功率范围', value: '0.75–7.5', unit: 'kW', usage: 'external', state: 'approved', source: '产品手册' },
        { label: '控制方式', value: '变频恒压 / 手动', unit: '', usage: 'external', state: 'approved', source: '产品手册' }
      ],
      related: [], faq: [],
      sources: [{ id: 's1', kind: 'manual', label: '手动创建', detail: '由 周航 在控制台创建' }],
      versions: [{ ver: 'v1', date: '2026-10-09', author: '周航', summary: '创建并发布' }],
      draft: null
    },
    {
      id: 'k-009', type: '服务', category: '专业服务', title: '现场安装与调试服务',
      summary: '服务范围、响应时间与不含项。',
      usage: 'external', aiAuthorized: true, status: 'published',
      prepared: true, processState: 'idle', validUntil: null, needsReview: false,
      owner: '周航', updatedAt: '2026-09-25 13:00', currentVersion: 'v1',
      body: '提供设备就位、管路接管指导、通电调试与首次运行培训。',
      fields: [
        { label: '服务区域', value: '全国主要工业城市（偏远地区需评估）', unit: '', usage: 'external', state: 'approved', source: '服务价目表' },
        { label: '响应时间', value: '收到通知后 48 小时内联系排期', unit: '', usage: 'external', state: 'approved', source: '服务承诺' },
        { label: '服务内容', value: '就位、接管、调试、首次运行培训', unit: '', usage: 'external', state: 'approved', source: '服务承诺' },
        { label: '不包含项', value: '土建改造与基座施工', unit: '', usage: 'external', state: 'approved', source: '服务承诺' },
        { label: '内部排期备注', value: '旺季需提前 7 天（不对外）', unit: '', usage: 'internal', state: 'approved', source: '内部' }
      ],
      related: ['3SV08NL007T 立式多级离心泵'], faq: [],
      sources: [{ id: 's1', kind: 'manual', label: '手动创建', detail: '由 周航 在控制台创建' }],
      versions: [{ ver: 'v1', date: '2026-09-25', author: '周航', summary: '创建并发布' }],
      draft: null,
      qa: { keywords: ['安装', '调试', '上门', '现场', '服务区域'], answer: '我们提供上门安装与调试服务，覆盖全国主要工业城市；收到通知后 48 小时内联系排期。偏远地区需要单独评估。' }
    },
    {
      id: 'k-010', type: 'FAQ', category: 'FAQ', title: '安装常见问题整理（草稿）',
      summary: '安装相关问题整理中，需要核对。',
      usage: 'external', aiAuthorized: false, status: 'draft',
      prepared: false, processState: 'idle', validUntil: null, needsReview: true,
      owner: '王倩', updatedAt: '2026-10-08 16:22', currentVersion: '',
      body: '', fields: [], related: [], faq: [
        { q: '安装需要提前准备什么？', a: '（待补充：核对现场条件清单）' }
      ],
      sources: [{ id: 's1', kind: 'manual', label: '手动创建', detail: '由 王倩 在控制台创建' }],
      versions: [], draft: null
    },
    {
      id: 'k-011', type: '文件资料', category: '文件资料', title: '员工差旅与报销制度（导入整理中）',
      summary: '由 TXT 文件导入，正文整理中。',
      usage: 'internal', aiAuthorized: false, status: 'draft',
      prepared: false, processState: 'idle', validUntil: null, needsReview: true,
      owner: '陈启', updatedAt: '2026-10-09 10:40', currentVersion: '',
      body: '（导入节选）出差审批：3 天以内由部门负责人审批；3 天以上加签总经理。\n报销时限：行程结束后 15 个工作日内提交。\n（其余内容待人工整理后发布）',
      fields: [], related: [], faq: [],
      sources: [{ id: 's1', kind: 'file', label: '原文件登记', detail: '原始文件：员工差旅与报销制度.txt · 12.4 KB · 仅登记文件信息，演示环境不上传' }],
      versions: [], draft: null
    },
    {
      id: 'k-012', type: '文章', category: '服务政策', title: '经销商返利政策（待核对）',
      summary: '返利口径整理中，需要与财务核对。',
      usage: 'external', aiAuthorized: false, status: 'draft',
      prepared: false, processState: 'idle', validUntil: null, needsReview: true,
      owner: '李帆', updatedAt: '2026-10-07 09:15', currentVersion: '',
      body: '（草稿）年度返利按季度销售额分档计算，具体档位与结算口径待财务核对后定稿。',
      fields: [], related: [], faq: [],
      sources: [{ id: 's1', kind: 'manual', label: '手动创建', detail: '由 李帆 在控制台创建' }],
      versions: [], draft: null,
      qa: { keywords: ['返利', '经销商'], answer: '' }
    }
  ];
}

/* ─────────────── 全局状态 ─────────────── */
const DB = { entries: seedEntries() };
const state = {
  role: 'admin',
  route: { name: 'list', view: 'all', id: null, tab: 'content' },
  filters: { q: '', category: 'all', status: 'all', usage: 'all', ai: 'all', more: false },
  editing: null,
  imp: null,
  qa: { messages: [{ role: 'center', text: '下面是“模拟客户”视角的问答测试：只有已发布、对外、已授权且准备完成的资料才会被引用。' }], pending: false },
  dlg: null,
  pendingLeave: null,
  focusComposer: false,
  /* 侧栏知识分类的展开状态：render 只读取它、不重置（用户第四次反馈） */
  navExpanded: false
};

function byId(id) { return DB.entries.find((e) => e.id === id); }
function nowTime() { const d = new Date(); return String(d.getHours()).padStart(2, '0') + ':' + String(d.getMinutes()).padStart(2, '0'); }

/* ─────────────── 可用性计算（唯一权威：系统算，用户不选） ─────────────── */
function availability(e) {
  if (e.status === 'draft') return { ok: false, label: '草稿', cls: 'draft', reason: '尚未发布' };
  if (e.status === 'disabled') return { ok: false, label: '已停用', cls: 'off', reason: '已停用' };
  let reason = null;
  if (e.usage === 'internal') reason = '仅内部';
  else if (!e.aiAuthorized) reason = '未授权';
  else if (e.processState === 'failed') reason = '处理失败';
  else if (e.processState === 'processing' || !e.prepared) reason = '准备中';
  else if (e.validUntil && e.validUntil < DEMO_TODAY) reason = '过期';
  /* 已发布两种标签按可用性区分配色：AI客服可用＝绿色（cls ok，保留原成功绿）、不可用＝中性蓝灰（cls pub）；
     不把「AI 不可用」染成黄/绿告警色，避免内部正常资料像出故障；文案与颜色共同区分，原因仍另行提示
     （2026-10-10 用户纠正：保留原已发布可用的绿色，只把不可用改为中性蓝灰） */
  if (reason) return { ok: false, label: '已发布（AI客服不可用）', cls: 'pub', reason };
  return { ok: true, label: '已发布（AI客服可用）', cls: 'ok', reason: null };
}
function pillHTML(e) {
  const a = availability(e);
  return '<span class="pill ' + a.cls + '">' + esc(a.label) + '</span>';
}
function typeTagHTML(e) { return '<span class="tag type">' + esc(e.type) + '</span>'; }
function usageText(u) { return u === 'external' ? '对外' : '对内'; }

/* ─────────────── 内容 ↔ 工作副本 ─────────────── */
function contentOf(e) {
  return {
    title: e.title, category: e.category, usage: e.usage, aiAuthorized: e.aiAuthorized,
    body: e.body || '', faq: clone(e.faq || []), fields: clone(e.fields || []),
    relatedText: (e.related || []).join('，')
  };
}
function applyWorking(e, w) {
  e.title = w.title.trim() || '未命名';
  e.category = w.category;
  e.usage = w.usage;
  e.aiAuthorized = w.usage === 'external' ? !!w.aiAuthorized : false;
  e.body = w.body;
  e.faq = clone(w.faq || []);
  e.fields = clone(w.fields || []);
  e.related = w.relatedText.split(/[，,]/).map((s) => s.trim()).filter(Boolean);
}
function emptyArticle(category) {
  return { title: '', category: category || 'FAQ', usage: 'external', aiAuthorized: false, body: '', faq: [{ q: '', a: '' }], fields: [], relatedText: '' };
}
function emptyProduct() {
  return { title: '', category: '产品', usage: 'external', aiAuthorized: false, body: '', faq: [], fields: [{ label: '', value: '', unit: '', usage: 'external', state: 'approved', source: '' }], relatedText: '' };
}

/* ─────────────── 保存 / 发布 / 生命周期 ─────────────── */
function isDirty() {
  if (state.editing && JSON.stringify(state.editing.working) !== state.editing.baseJSON) return true;
  if (state.imp && state.imp.dirty) return true;
  return false;
}
function saveDraft(showErrors) {
  const ed = state.editing;
  if (!ed) return false;
  if (showErrors && !ed.working.title.trim()) { flashEditorError('请先填写标题，再保存草稿。'); return false; }
  if (ed.isNew) {
    const e = {
      id: 'k-' + String(Date.now()).slice(-6), type: TYPE_OF_CATEGORY[ed.working.category] || '文章',
      category: ed.working.category, title: ed.working.title.trim() || '未命名草稿',
      summary: '（新建草稿）', usage: ed.working.usage, aiAuthorized: ed.working.usage === 'external' ? !!ed.working.aiAuthorized : false,
      status: 'draft', prepared: false, processState: 'idle', validUntil: null, needsReview: true,
      owner: '当前用户（演示）', updatedAt: DEMO_TODAY + ' ' + nowTime(), currentVersion: '',
      body: '', fields: [], faq: [], related: [],
      sources: [{ id: 's1', kind: 'manual', label: '手动创建', detail: '由当前用户（演示）创建' }],
      versions: [], draft: null
    };
    applyWorking(e, ed.working);
    DB.entries.unshift(e);
    state.editing = null;
    setRoute({ name: 'entry', id: e.id, tab: 'content' });
    render();
    toast('已保存为草稿');
    return true;
  }
  const e = byId(ed.id);
  if (!e) { state.editing = null; return true; }
  if (e.status === 'published') {
    e.draft = { content: clone(ed.working), savedAt: DEMO_TODAY + ' ' + nowTime() };
    toast('草稿已保存；线上版本仍为 ' + (e.currentVersion || '当前版本') + '，发布后才会替换');
  } else {
    applyWorking(e, ed.working);
    e.updatedAt = DEMO_TODAY + ' ' + nowTime();
    toast('草稿已更新');
  }
  ed.baseJSON = JSON.stringify(ed.working);
  return true;
}
function flashEditorError(msg) {
  const box = $('#editor-alert');
  if (box) { box.style.display = 'flex'; box.textContent = msg; }
  $('#app').scrollIntoView({ block: 'start' });
}
function requestPublish(source) {
  // source: {entry, working} 或在编辑器里从 state.editing 取
  let entry, working;
  if (state.editing) { entry = state.editing.isNew ? null : byId(state.editing.id); working = state.editing.working; }
  else { entry = source && source.entry; working = source && source.working; }
  if (!working.title.trim()) { flashEditorError('请先填写标题，再发布。'); return; }
  const willBeExtAi = working.usage === 'external' && (working.usage === 'external' && !!working.aiAuthorized);
  const doIt = () => finishPublish(entry, working);
  if (willBeExtAi) {
    confirmDialog({
      title: '发布对外 AI 版本？',
      lines: [
        '这条知识为「对外 + 允许AI客服使用」，发布后客户问答将引用新版本。',
        '请确认内容与出处已核对：发布错误的关键事实会直接影响客户答复。'
      ],
      confirmLabel: '确认发布',
      onConfirm: doIt
    });
  } else {
    doIt();
  }
}
function finishPublish(entry, working) {
  const wasNew = !entry;
  if (wasNew) {
    entry = {
      id: 'k-' + String(Date.now()).slice(-6), type: TYPE_OF_CATEGORY[working.category] || '文章',
      category: working.category, summary: '（新建）', status: 'draft', prepared: false, processState: 'idle',
      validUntil: null, needsReview: false, owner: '当前用户（演示）', updatedAt: '', currentVersion: '',
      body: '', fields: [], faq: [], related: [],
      sources: [{ id: 's1', kind: 'manual', label: '手动创建', detail: '由当前用户（演示）创建' }],
      versions: [], draft: null
    };
    applyWorking(entry, working);
    DB.entries.unshift(entry);
  } else {
    if (working === null) { /* noop */ }
    if (state.editing && !state.editing.isNew) applyWorking(entry, state.editing.working);
    else if (!state.editing && entry.draft) applyWorking(entry, entry.draft.content);
  }
  const n = entry.versions.length + 1;
  entry.versions.push({ ver: 'v' + n, date: DEMO_TODAY, author: '当前用户（演示）', summary: (entry.versions.length ? '更新发布' : '创建并发布') });
  entry.currentVersion = 'v' + n;
  entry.status = 'published';
  entry.draft = null;
  entry.needsReview = false;
  entry.updatedAt = DEMO_TODAY + ' ' + nowTime();
  state.editing = null;
  setRoute({ name: 'entry', id: entry.id, tab: 'content' });
  if (entry.usage === 'external' && entry.aiAuthorized) {
    entry.prepared = false;
    entry.processState = 'processing';
    toast('已发布；当前版本准备中（演示模拟）…');
    window.setTimeout(() => {
      entry.prepared = true;
      entry.processState = 'idle';
      entry.updatedAt = DEMO_TODAY + ' ' + nowTime();
      if (!state.editing) render();
      toast('当前版本处理完成，AI 客服已可用（演示模拟）');
    }, 2400);
  } else {
    toast('已发布');
  }
  render();
}
function toggleAuth(e, checked) {
  if (e.usage === 'internal') return;
  if (!checked) {
    e.aiAuthorized = false;
    e.updatedAt = DEMO_TODAY + ' ' + nowTime();
    toast('已停止用于 AI 客服，立即生效（演示不等远程索引）');
    render();
    return;
  }
  if (e.status === 'published' && e.usage === 'external') {
    confirmDialog({
      title: '允许 AI 客服使用这条知识？',
      lines: [
        '开启后，客户问答将可以引用这条知识（发布 + 对外 + 授权共同决定可用性）。',
        '授权调整会立即生效，不需要等待远程索引删除。'
      ],
      confirmLabel: '开启授权',
      onConfirm: () => {
        e.aiAuthorized = true;
        e.prepared = false;
        e.processState = 'processing';
        e.updatedAt = DEMO_TODAY + ' ' + nowTime();
        toast('已授权；当前版本准备中（演示模拟）…');
        render();
        window.setTimeout(() => {
          e.prepared = true;
          e.processState = 'idle';
          if (!state.editing) render();
          toast('准备完成，AI 客服已可用（演示模拟）');
        }, 2200);
      }
    });
  } else {
    e.aiAuthorized = true;
    render();
  }
}
function disableEntry(e) {
  confirmDialog({
    icon: 'danger',
    title: '停用这条知识？',
    lines: [
      '停用后：列表与详情显示「已停用」；AI 客服立即停止引用（不等远程索引删除）。',
      '历史版本、来源与关联对象都会保留，可随时重新启用。'
    ],
    confirmLabel: '停用',
    danger: true,
    onConfirm: () => {
      e.status = 'disabled';
      e.updatedAt = DEMO_TODAY + ' ' + nowTime();
      toast('已停用');
      render();
    }
  });
}
function reenableEntry(e) {
  const doIt = () => {
    e.status = 'published';
    e.updatedAt = DEMO_TODAY + ' ' + nowTime();
    if (e.usage === 'external' && e.aiAuthorized) {
      e.prepared = false;
      e.processState = 'processing';
      window.setTimeout(() => {
        e.prepared = true;
        e.processState = 'idle';
        if (!state.editing) render();
        toast('重新启用完成（演示模拟）');
      }, 2000);
    }
    toast('已重新启用');
    render();
  };
  if (e.usage === 'external' && e.aiAuthorized) {
    confirmDialog({
      title: '重新启用并恢复对外 AI 使用？',
      lines: ['该知识为「对外 + 已授权」，重新启用后 AI 客服会再次引用它。'],
      confirmLabel: '重新启用',
      onConfirm: doIt
    });
  } else { doIt(); }
}
function retryProcessing(e) {
  e.processState = 'processing';
  e.prepared = false;
  toast('已开始重试（演示模拟）…');
  render();
  window.setTimeout(() => {
    e.prepared = true;
    e.processState = 'idle';
    e.updatedAt = DEMO_TODAY + ' ' + nowTime();
    if (!state.editing) render();
    toast('重试成功，AI 客服已可用（演示模拟）');
  }, 2400);
}
function restoreVersion(e, ver) {
  if (e.draft) { toast('已有待发布草稿，先发布或继续编辑它', true); return; }
  const v = e.versions.find((x) => x.ver === ver);
  if (!v || !v.snapshot) { toast('该版本没有保存正文快照（演示限制）', true); return; }
  const w = contentOf(e);
  if (v.snapshot.body !== undefined) w.body = v.snapshot.body;
  if (v.snapshot.faq) w.faq = clone(v.snapshot.faq);
  if (v.snapshot.fields) w.fields = clone(v.snapshot.fields);
  e.draft = { content: w, savedAt: DEMO_TODAY + ' ' + nowTime(), restoredFrom: ver };
  toast('已从 ' + ver + ' 创建新草稿（不会覆盖历史版本）');
  render();
}

/* ─────────────── 编辑 ─────────────── */
function startEdit(id) {
  const e = byId(id);
  if (!e) return;
  let working;
  if (e.draft) working = clone(e.draft.content);
  else working = contentOf(e);
  state.editing = { id: e.id, isNew: false, kind: e.type === '产品' || e.type === '服务' ? 'product' : 'article', working, baseJSON: JSON.stringify(e.draft ? clone(e.draft.content) : contentOf(e)) };
  if (e.draft) state.editing.baseJSON = JSON.stringify(working);
  setRoute({ name: 'entry', id: e.id, tab: 'content', mode: 'edit' });
  render();
}
function startNew(kind, category) {
  const working = kind === 'product' ? emptyProduct() : emptyArticle(category);
  state.editing = { id: null, isNew: true, kind, working, baseJSON: JSON.stringify(kind === 'product' ? emptyProduct() : emptyArticle(category)) };
  setRoute({ name: 'edit', id: null, tab: 'content' });
  render();
}
function cancelEdit() {
  if (isDirty()) {
    state.pendingLeave = () => { state.editing = null; render(); };
    leaveDialog();
    return;
  }
  state.editing = null;
  render();
}

/* ─────────────── 路由 ─────────────── */
function setRoute(r) { state.route = Object.assign({ name: 'list', view: 'all', id: null, tab: 'content' }, r); }
function go(next) {
  if (isDirty()) {
    state.pendingLeave = () => { state.editing = null; state.imp = null; setRoute(next); render(); };
    leaveDialog();
    return;
  }
  state.editing = null; state.imp = null;
  setRoute(next);
  render();
}

/* ─────────────── 列表筛选 ─────────────── */
function entryMatchesView(e, view) {
  if (view === 'products') return e.type === '产品' || e.type === '服务';
  if (view === 'articles') return e.type === '文章' || e.type === 'FAQ';
  if (view === 'files') return (e.sources || []).some((s) => s.kind === 'file');
  return true;
}
function entryMatchesFilters(e) {
  const f = state.filters;
  if (f.q.trim()) {
    const hay = [e.title, e.summary, e.category, e.body, (e.faq || []).map((x) => x.q + ' ' + x.a).join(' '), (e.fields || []).map((x) => x.label + ' ' + x.value).join(' ')].join(' ').toLowerCase();
    if (!hay.includes(f.q.trim().toLowerCase())) return false;
  }
  if (f.category !== 'all' && e.category !== f.category) return false;
  if (f.status !== 'all' && e.status !== f.status) return false;
  if (f.usage !== 'all' && e.usage !== f.usage) return false;
  if (f.ai !== 'all') {
    const a = availability(e);
    if (f.ai === 'ok' && !a.ok) return false;
    if (f.ai === 'no' && !(e.status === 'published' && !a.ok)) return false;
  }
  return true;
}
function visibleEntries() { return DB.entries.filter((e) => entryMatchesView(e, state.route.view) && entryMatchesFilters(e)); }

/* ─────────────── 侧栏 / 顶栏 ─────────────── */
function sideNavHTML() {
  const listActive = state.route.name === 'list' || state.route.name === 'entry' || state.route.name === 'edit';
  const qaActive = state.route.name === 'qa';
  const subActive = (view) => listActive && state.route.view === view;
  const allActive = listActive && state.route.name === 'list' && state.route.view === 'all';
  const sub = (view, label, icon) => '<button class="side-link sub' + (subActive(view) ? ' active' : '') + '" data-act="nav" data-view="' + view + '"' + (subActive(view) ? ' aria-current="page"' : '') + '>' +
    '<svg class="ic"><use href="#' + icon + '"/></svg>' + esc(label) + '</button>';
  const expanded = !!state.navExpanded;
  return '' +
    '<div class="side-heading">卖家控制台</div>' +
    '<button class="side-link" data-act="out-of-scope"><svg class="ic"><use href="#i-home"/></svg>工作台</button>' +
    '<button class="side-link" data-act="out-of-scope"><svg class="ic"><use href="#i-inbox"/></svg>收件箱</button>' +
    '<div class="side-heading">企业知识</div>' +
    /* 「全部知识」主按钮：进入全部列表并展开分类；右侧独立箭头只展开/收起、不跳转（两个并列 button，不嵌套） */
    '<div class="nav-main-row">' +
    '<button class="side-link' + (allActive ? ' active' : '') + '" data-act="nav" data-view="all"' + (allActive ? ' aria-current="page"' : '') + '><svg class="ic"><use href="#i-book"/></svg>全部知识</button>' +
    '<button class="side-toggle" type="button" data-act="nav-toggle" aria-controls="nav-knowledge-subs" aria-expanded="' + (expanded ? 'true' : 'false') + '" aria-label="' + (expanded ? '收起知识分类' : '展开知识分类') + '"><svg class="ic"><use href="#i-chev"/></svg></button>' +
    '</div>' +
    '<div class="nav-subs' + (expanded ? ' open' : '') + '" id="nav-knowledge-subs"' + (expanded ? '' : ' inert aria-hidden="true"') + '>' +
    '<div class="nav-subs-inner">' +
    sub('products', '产品与服务', 'i-box') +
    sub('articles', '文章与 FAQ', 'i-book') +
    sub('files', '文件资料', 'i-file') +
    '</div></div>' +
    '<button class="side-link' + (qaActive ? ' active' : '') + '" data-act="nav" data-view="qa" style="margin-top:6px"><svg class="ic"><use href="#i-chat"/></svg>模拟客户问答</button>' +
    '<div class="side-heading">其他</div>' +
    '<button class="side-link" data-act="out-of-scope"><svg class="ic"><use href="#i-globe"/></svg>渠道账号</button>' +
    '<button class="side-link" data-act="out-of-scope"><svg class="ic"><use href="#i-sliders"/></svg>设置</button>';
}
/* 只同步导航 DOM（class / inert / aria），不重建——保证展开收起动画能完整播放（用户第四次反馈） */
function syncNavExpandedDOM() {
  const subs = $('#nav-knowledge-subs');
  const toggle = $('.side-toggle');
  if (!subs || !toggle) return;
  const open = !!state.navExpanded;
  subs.classList.toggle('open', open);
  if (open) { subs.removeAttribute('inert'); subs.removeAttribute('aria-hidden'); }
  else { subs.setAttribute('inert', ''); subs.setAttribute('aria-hidden', 'true'); }
  toggle.setAttribute('aria-expanded', open ? 'true' : 'false');
  toggle.setAttribute('aria-label', open ? '收起知识分类' : '展开知识分类');
}
function crumbHTML() {
  const r = state.route;
  if (r.name === 'list') {
    const label = { all: '全部知识', products: '产品与服务', articles: '文章与 FAQ', files: '文件资料' }[r.view] || '全部知识';
    return '企业知识 / <b>' + esc(label) + '</b>';
  }
  if (r.name === 'qa') return '企业知识 / <b>模拟客户问答</b>';
  if (r.name === 'import') return '企业知识 / <b>添加 · 文件导入</b>';
  if (r.name === 'edit') return '企业知识 / <b>' + (state.editing && state.editing.isNew ? '添加知识' : '编辑') + '</b>';
  const e = byId(r.id);
  return '企业知识 / <b>' + esc(e ? e.title : '') + '</b>';
}

/* ─────────────── 渲染：总入口 ─────────────── */
function render() {
  $('#side-nav').innerHTML = sideNavHTML();
  $('#crumb').innerHTML = crumbHTML();
  const r = state.route;
  let html = '';
  if (r.name === 'list') html = viewListHTML();
  else if (r.name === 'entry') html = state.editing ? viewEditHTML() : viewEntryHTML(r.id);
  else if (r.name === 'edit') html = viewEditHTML();
  else if (r.name === 'import') html = viewImportHTML();
  else if (r.name === 'qa') html = viewQAHTML();
  $('#app').innerHTML = html;
  document.body.classList.remove('nav-open');
  $('#menu-btn').setAttribute('aria-expanded', 'false');
  if (state.focusComposer) {
    state.focusComposer = false;
    const ta = $('[data-qa-input]');
    if (ta) ta.focus();
    const log = $('.chat-log');
    if (log) log.scrollTop = log.scrollHeight;
  }
  const log2 = $('.chat-log');
  if (log2) log2.scrollTop = log2.scrollHeight;
}

/* ─────────────── 视图：列表 ─────────────── */
function addMenuHTML() {
  return '' +
    '<details class="add-wrap" id="add-wrap"><summary class="button" role="button">' +
    '<svg class="ic" style="stroke:#fff"><use href="#i-plus"/></svg>添加知识' +
    '<svg class="ic" style="stroke:#fff"><use href="#i-chev"/></svg></summary>' +
    '<div class="add-menu">' +
    '<button data-act="new-article"><svg class="ic"><use href="#i-book"/></svg><span>添加知识文章<small>FAQ、政策、企业文化、培训等</small></span></button>' +
    '<button data-act="new-product"><svg class="ic"><use href="#i-box"/></svg><span>添加产品 / 服务<small>结构化字段（该对象自己的字段）</small></span></button>' +
    '<button data-act="new-file"><svg class="ic"><use href="#i-file"/></svg><span>上传文件<small>登记原文件，人工核对后入库</small></span></button>' +
    '</div></details>';
}
/* 分视图新增入口：产品与服务 / 文章与 FAQ / 文件资料 直接显示对应动作按钮；
   只有「全部知识」保留通用「添加知识」下拉；只读视角不出现任何新增入口（用户第四次反馈） */
function viewAddHTML() {
  if (state.role !== 'admin') return '<span class="tag">仅阅读视角（演示）</span>';
  const view = state.route.view;
  if (view === 'products') return '<button class="button" data-act="new-product"><svg class="ic" style="stroke:#fff"><use href="#i-box"/></svg>添加产品 / 服务</button>';
  if (view === 'articles') return '<button class="button" data-act="new-article"><svg class="ic" style="stroke:#fff"><use href="#i-book"/></svg>添加文章 / FAQ</button>';
  if (view === 'files') return '<button class="button" data-act="new-file"><svg class="ic" style="stroke:#fff"><use href="#i-file"/></svg>上传文件</button>';
  return addMenuHTML();
}
function viewListHTML() {
  const titles = { all: '全部知识', products: '产品与服务', articles: '文章与 FAQ', files: '文件资料' };
  const r = state.route;
  return '' +
    '<div class="toolbar"><div><h1>' + esc(titles[r.view] || '全部知识') + '</h1>' +
    '<p class="lead">企业知识统一入口：文章、FAQ、产品与服务、文件资料都在这里管理；分类用筛选完成，不来回切换后台。' +
    (r.view === 'files' ? '「文件资料」视图只显示登记了原文件的知识；原文件默认不对外公开。' : '') + '</p></div>' +
    viewAddHTML() + '</div>' +
    listToolbarHTML() +
    '<section class="card" aria-label="知识列表"><div class="rows" id="list-rows">' + listRowsHTML() + '</div></section>';
}
function listToolbarHTML() {
  const f = state.filters;
  const opt = (v, t, cur) => '<option value="' + v + '"' + (cur === v ? ' selected' : '') + '>' + t + '</option>';
  return '' +
    '<div class="list-toolbar">' +
    '<div class="search-wrap"><svg class="ic"><use href="#i-search"/></svg>' +
    '<input id="list-search" data-scope="filters" data-key="q" value="' + esc(f.q) + '" placeholder="全文搜索：按标题与正文文字匹配（不使用 AI）" aria-label="全文搜索" autocomplete="off">' +
    '<button type="button" class="search-clear" data-act="clear-search" aria-label="清空搜索"' + (f.q ? '' : ' hidden') + '><svg class="ic"><use href="#i-x"/></svg></button>' +
    '</div>' +
    '<select class="filter-select" data-scope="filters" data-key="category" aria-label="按分类筛选">' + opt('all', '全部分类', f.category) + CATEGORIES.concat(PRODUCT_CATEGORIES).map((c) => opt(c, c, f.category)).join('') + '</select>' +
    '<select class="filter-select" data-scope="filters" data-key="status" aria-label="按状态筛选">' + opt('all', '全部状态', f.status) + opt('draft', '草稿', f.status) + opt('published', '已发布', f.status) + opt('disabled', '已停用', f.status) + '</select>' +
    '<button class="button secondary" data-act="toggle-more">' + (f.more ? '收起筛选' : '更多筛选') + '</button>' +
    '</div>' +
    '<div class="more-filters' + (f.more ? ' open' : '') + '">' +
    '<select class="filter-select" data-scope="filters" data-key="usage" aria-label="按用途筛选">' + opt('all', '全部用途', f.usage) + opt('external', '对外', f.usage) + opt('internal', '对内', f.usage) + '</select>' +
    '<select class="filter-select" data-scope="filters" data-key="ai" aria-label="按 AI 可用性筛选">' + opt('all', 'AI 可用性：全部', f.ai) + opt('ok', 'AI 可用', f.ai) + opt('no', 'AI 不可用', f.ai) + '</select>' +
    '</div>' +
    '<p class="filter-note" id="list-count">' + countText() + '</p>';
}
function countText() { return '当前显示 ' + visibleEntries().length + ' 条 · 全文搜索按标题与正文文字匹配（不使用 AI；与“模拟客户问答”不同）'; }
function listRowsHTML() {
  const rows = visibleEntries();
  if (!rows.length) {
    const emptyFile = state.route.view === 'files';
    return '<div class="empty"><h2>' + (emptyFile ? '还没有登记原文件的知识' : '没有匹配的知识') + '</h2>' +
      '<p>' + (emptyFile ? '通过「添加知识 → 上传文件」登记 TXT / Markdown 或先登记 PDF / Word 原件，再人工整理正文。' : '可以清空筛选，或从「添加知识」创建新条目。') + '</p>' +
      '<div class="actions" style="justify-content:center">' +
      (emptyFile ? (state.role === 'admin' ? '<button class="button" data-act="new-file">上传文件</button>' : '') : '<button class="button secondary" data-act="clear-filters">清空筛选</button>') +
      '</div></div>';
  }
  return rows.map((e) => {
    const a = availability(e);
    const notes = [];
    if (e.status === 'draft' && e.needsReview) notes.push('需要核对');
    if (e.draft) notes.push('有草稿修改待发布');
    return '<button class="row" data-act="open-entry" data-id="' + e.id + '">' +
      '<span class="row-main"><span class="row-title">' + typeTagHTML(e) + '<strong>' + esc(e.title) + '</strong>' +
      (a.reason && e.status === 'published' ? '<span class="tag" title="' + esc(REASON_HINT[a.reason] || '') + '">' + esc(a.reason) + '</span>' : '') +
      '</span>' +
      '<span class="row-meta"><span>' + esc(e.category) + '</span><span>' + esc(usageText(e.usage)) + '</span><span>负责人 ' + esc(e.owner) + '</span><span>更新 ' + esc(e.updatedAt) + '</span>' +
      (notes.length ? '<span class="row-note">' + notes.map(esc).join(' · ') + '</span>' : '') + '</span></span>' +
      '<span class="row-side">' + pillHTML(e) + '<svg class="ic row-chev"><use href="#i-chev"/></svg></span>' +
      '</button>';
  }).join('');
}
function refreshList() {
  const rows = $('#list-rows');
  if (rows) rows.innerHTML = listRowsHTML();
  const c = $('#list-count');
  if (c) c.textContent = countText();
  const more = $('.more-filters');
  if (more) more.classList.toggle('open', state.filters.more);
}
/* 清空按钮只随关键词显隐，不重建输入框本身，输入时焦点不丢（用户第四次反馈） */
function syncSearchClear() {
  const btn = $('.search-clear');
  if (!btn) return;
  if (state.filters.q) btn.removeAttribute('hidden'); else btn.setAttribute('hidden', '');
}

/* ─────────────── 视图：详情 ─────────────── */
function viewEntryHTML(id) {
  const e = byId(id);
  if (!e) return '<div class="empty"><h2>条目不存在</h2><p>演示数据已重置时可能发生，请返回列表。</p><button class="button" data-act="back-list">返回列表</button></div>';
  const a = availability(e);
  const canEdit = state.role === 'admin';
  const canRestore = e.versions.length > 1;
  const parts = [];
  parts.push('<p><button class="button ghost" data-act="back-list">← 返回列表</button></p>');
  parts.push('<div class="detail-head"><div><div class="detail-title"><h1>' + esc(e.title) + '</h1>' + typeTagHTML(e) + pillHTML(e) + (state.role === 'member' ? '<span class="tag">只读（演示）</span>' : '') + '</div>' +
    '<div class="detail-meta"><span>负责人 ' + esc(e.owner) + '</span><span>更新 ' + esc(e.updatedAt) + '</span><span>' + esc(e.status === 'draft' ? '草稿（未发布）' : '当前版本 ' + (e.currentVersion || '—')) + '</span></div>' +
    (a.reason && e.status === 'published' ? '<p class="hint-line">AI 客服不可用原因：' + esc(REASON_HINT[a.reason] || a.reason) + '</p>' : '') +
    (e.status === 'draft' ? '<p class="hint-line">草稿状态：整理完成后发布；发布前不会出现在任何问答中。</p>' : '') +
    '</div><div class="actions">' + entryActionsHTML(e, canEdit) + '</div></div>');
  if (!canEdit) parts.push('<div class="readonly-note"><svg class="ic"><use href="#i-check"/></svg><span>你当前是「仅阅读成员」视角（演示）：本企业所有账户默认可读对内与对外知识；编辑、发布、授权与停用的具体角色分配是待评审建议（原型未验证后端权限隔离）。</span></div>');
  if (e.processState === 'failed') parts.push('<div class="warn-note"><svg class="ic"><use href="#i-alert"/></svg><span>当前版本处理失败（这就是 AI 客服不可用的原因）。主状态仍为「已发布」，可重试；重试只影响这条知识，其他问答不受影响。</span>' + (canEdit ? '<button class="button secondary" data-act="retry" style="margin-left:auto">重试</button>' : '') + '</div>');
  if (e.processState === 'processing') parts.push('<div class="demo-note"><span>当前版本准备中（演示模拟约 2 秒）…准备完成后自动恢复可用，无需人工操作。此条准备期间其他知识不受影响。</span></div>');
  if (e.draft) {
    const n = e.versions.length + 1;
    parts.push('<div class="draft-banner banner"><span>存在待发布草稿 v' + n + (e.draft.restoredFrom ? '（恢复自 ' + esc(e.draft.restoredFrom) + '）' : '') + '，保存于 ' + esc(e.draft.savedAt) + '。' + (e.currentVersion ? '线上版本 ' + esc(e.currentVersion) + ' 继续对外服务，发布后才会替换。' : '') + '</span>' +
      (canEdit ? '<span class="actions"><button class="button secondary" data-act="edit-draft">继续编辑</button><button class="button" data-act="publish-draft">发布草稿</button></span>' : '') + '</div>');
  }
  if (e.status === 'draft' && e.needsReview) {
    parts.push('<div class="warn-note"><svg class="ic"><use href="#i-alert"/></svg><span>需要核对：这条草稿还有待确认内容（辅助提醒，不是生命周期状态）。核对完成后可发布，或标记为已核对。</span>' +
      (canEdit ? '<button class="button secondary" data-act="mark-reviewed" style="margin-left:auto">标记核对完成</button>' : '') + '</div>');
  }
  const tab = state.route.tab || 'content';
  parts.push('<div class="tabs">' +
    '<button class="tab' + (tab === 'content' ? ' on' : '') + '" data-act="tab" data-tab="content">' + (e.type === '产品' || e.type === '服务' ? '结构化字段' : '正文') + '</button>' +
    '<button class="tab' + (tab === 'sources' ? ' on' : '') + '" data-act="tab" data-tab="sources">来源与版本</button>' +
    '</div>');
  parts.push('<div class="detail-layout"><section class="card">' + (tab === 'content' ? entryContentHTML(e) : entrySourcesHTML(e, canRestore)) + '</section>' + railHTML(e, canEdit) + '</div>');
  return parts.join('');
}
function entryActionsHTML(e, canEdit) {
  if (!canEdit) return '';
  if (e.status === 'draft') {
    return '<button class="button secondary" data-act="edit-entry" data-id="' + e.id + '">编辑</button>' +
      '<button class="button" data-act="publish-from-entry" data-id="' + e.id + '">发布</button>';
  }
  if (e.status === 'disabled') {
    return '<button class="button secondary" data-act="edit-entry" data-id="' + e.id + '">编辑</button>' +
      '<button class="button" data-act="reenable" data-id="' + e.id + '">重新启用</button>';
  }
  return '<button class="button secondary" data-act="edit-entry" data-id="' + e.id + '">编辑</button>' +
    '<button class="button danger" data-act="disable" data-id="' + e.id + '">停用</button>';
}
function entryContentHTML(e) {
  if (e.type === '产品' || e.type === '服务') {
    const rows = e.fields.map((f) => {
      const scope = f.usage === 'external' ? '<span class="field-scope ext">对外</span>' : '<span class="field-scope int">仅内部</span>';
      const st = f.state === 'approved' ? '<span class="state-line ok">已核准</span>' : '<span class="state-line warn">待核准</span>';
      return '<tr><td><strong>' + esc(f.label) + '</strong></td><td>' + esc(f.value) + (f.unit ? ' <span class="muted" style="display:inline">' + esc(f.unit) + '</span>' : '') + '</td><td>' + scope + '</td><td>' + st + '</td><td class="muted">' + esc(f.source || '—') + '</td></tr>';
    }).join('');
    return '<p class="lead" style="margin-bottom:14px">' + esc(e.body || '') + '</p>' +
      '<div class="table-scroll"><table class="fields"><thead><tr><th>字段</th><th>内容</th><th>使用范围</th><th>核准</th><th>出处</th></tr></thead><tbody>' + rows + '</tbody></table></div>' +
      '<p class="hint-line" style="margin-top:10px">字段级规则：即使整条知识「对外 + 已授权」，AI 也只引用「对外且已核准」的字段；仅内部字段（如内部成本口径）不参与客户回答。</p>';
  }
  if (e.type === 'FAQ') {
    return (e.faq || []).map((p) => '<div class="faq-pair"><p class="faq-q">问：' + esc(p.q) + '</p><p class="faq-a">答：' + esc(p.a) + '</p></div>').join('') || '<p class="muted">（还没有问答对）</p>';
  }
  return '<div class="entry-body">' + String(e.body || '').split(/\n+/).filter(Boolean).map((p) => '<p>' + esc(p) + '</p>').join('') + '</div>';
}
function entrySourcesHTML(e, canRestore) {
  const srcs = (e.sources || []).map((s) => {
    const kindText = { manual: '手动创建', file: '文件导入', reference: '引用说明' }[s.kind] || s.kind;
    return '<div class="source-item"><div><strong>' + esc(s.label) + '</strong><small>' + esc(s.detail) + '</small></div><span class="tag">' + esc(kindText) + '</span></div>';
  }).join('') || '<p class="muted">（暂无来源记录）</p>';
  const vers = e.versions.length ? e.versions.map((v) => {
    const isCurrent = e.status === 'published' && v.ver === e.currentVersion;
    let tail = '';
    if (isCurrent) tail = '';
    else if (v.snapshot) tail = '<button class="button secondary" data-act="restore" data-id="' + e.id + '" data-ver="' + esc(v.ver) + '">恢复为草稿</button>';
    else tail = '<span class="hint-line">（演示未保存该版本快照）</span>';
    return '<div class="version-item"><div><strong><span class="vtag">' + esc(v.ver) + '</span>' + esc(v.summary) + '</strong><small>' + esc(v.date) + ' · ' + esc(v.author) + ' · ' + (isCurrent ? '当前线上版本' : '历史版本') + '</small></div>' +
      tail + '</div>';
  }).join('') : '<p class="muted">（尚未发布过，没有历史版本）</p>';
  const draftRow = e.draft ? '<div class="version-item"><div><strong><span class="vtag">v' + (e.versions.length + 1) + '</span>草稿（编辑中）</strong><small>保存于 ' + esc(e.draft.savedAt) + (e.draft.restoredFrom ? ' · 恢复自 ' + esc(e.draft.restoredFrom) : '') + ' · 发布前不影响线上版本</small></div></div>' : '';
  return '<h2>来源</h2><div class="sources-list">' + srcs + '</div>' +
    '<p class="hint-line" style="margin:10px 0 18px">对外知识 ≠ 公开原文件下载：客户问答只使用整理后的正文/字段；原始文件默认不对外公开。</p>' +
    '<h2>版本历史</h2><div class="versions-list">' + draftRow + (e.versions.length ? vers : (e.draft ? '' : vers)) + '</div>' +
    (canRestore ? '<p class="hint-line" style="margin-top:10px">恢复历史版本会创建一个新草稿，不会覆盖历史记录。</p>' : '');
  void canRestore;
}
function railHTML(e, canEdit) {
  const related = (e.related || []).length ? e.related.map((r) => '<span class="chip">' + esc(r) + '</span>').join('') : '<span class="muted">无关联对象</span>';
  let authBlock;
  if (e.usage === 'internal') {
    authBlock = '<span class="switch-state">不适用（仅内部资料不参与客户问答）</span>';
  } else if (e.status === 'draft') {
    authBlock = '<span class="switch-state">' + (e.aiAuthorized ? '已授权（发布后生效）' : '未授权（发布后可开启）') + '</span>';
  } else if (!canEdit) {
    authBlock = '<span class="switch-state">' + (e.aiAuthorized ? '已允许' : '未允许') + '</span>';
  } else {
    authBlock = '<label class="ios-switch"><input type="checkbox" data-act="auth-toggle" data-id="' + e.id + '"' + (e.aiAuthorized ? ' checked' : '') + ' aria-label="允许 AI 客服使用"><span class="ios-slider"></span></label>' +
      '<span class="switch-state">' + (e.aiAuthorized ? '已允许' : '未允许') + '</span>';
  }
  return '<aside class="rail" aria-label="属性"><h3>属性</h3>' +
    '<dl>' +
    '<div class="rail-item"><dt>分类</dt><dd><span class="tag type">' + esc(e.category) + '</span></dd></div>' +
    '<div class="rail-item"><dt>用途（对内 / 对外）</dt><dd>' + (e.usage === 'external' ? '对外：整理后可参与客户问答' : '对内：仅企业内部使用') + '</dd></div>' +
    '<div class="rail-item"><dt>允许 AI 客服使用（授权设置）</dt><dd class="switch-row">' + authBlock + '</dd></div>' +
    '<div class="rail-item"><dt>关联对象</dt><dd class="chiprow">' + related + '</dd></div>' +
    '</dl>' +
    '</aside>';
}

/* ─────────────── 视图：编辑 ─────────────── */
function viewEditHTML() {
  const ed = state.editing;
  if (!ed) return '<div class="empty"><h2>没有正在编辑的内容</h2><button class="button" data-act="back-list">返回列表</button></div>';
  const w = ed.working;
  const isProduct = ed.kind === 'product';
  const isFAQ = !isProduct && w.category === 'FAQ';
  const base = ed.isNew ? null : byId(ed.id);
  const headTitle = ed.isNew ? (isProduct ? '添加产品 / 服务' : '添加知识文章') : '编辑：' + (base ? base.title : '');
  const parts = [];
  parts.push('<div class="edit-head"><div><h1>' + esc(headTitle) + '</h1>' +
    '<p class="hint-line" id="dirty-indicator">' + (JSON.stringify(w) !== ed.baseJSON ? '有未保存的修改' : '暂无未保存修改') + '</p></div>' +
    '<div class="edit-actions">' +
    '<button class="button secondary" data-act="cancel-edit">取消</button>' +
    '<button class="button secondary" data-act="save-draft">保存草稿</button>' +
    '<button class="button" data-act="publish-editor">发布</button></div></div>');
  parts.push('<div class="form-alert" id="editor-alert" style="display:none"></div>');
  if (base && base.status === 'published') {
    parts.push('<div class="demo-note"><span>这条知识当前已发布（' + esc(base.currentVersion || '') + '）。保存草稿不会影响线上可用版本；发布新版本后，在准备完成前该条知识会暂时显示为「已发布（AI客服不可用）」，原因提示为“准备中”（辅助说明，不是额外状态）；其他知识不受影响。</span></div>');
  }
  if (base && base.draft) {
    parts.push('<div class="demo-note"><span>你正在编辑的是待发布草稿（' + esc(base.draft.restoredFrom ? '恢复自 ' + base.draft.restoredFrom : '保存于 ' + base.draft.savedAt) + '）。</span></div>');
  }
  parts.push('<section class="card"><h2>基本信息</h2><div class="form-grid">' +
    '<div class="field full"><label for="f-title">标题 *</label><input id="f-title" data-scope="work" data-key="title" value="' + esc(w.title) + '" placeholder="' + (isProduct ? '例如：某型号产品 / 某项服务' : '例如：退换货政策') + '"><div class="field-msg">标题不能为空</div></div>' +
    '<div><label for="f-cat">分类</label><select id="f-cat" data-scope="work" data-key="category">' +
    (isProduct ? PRODUCT_CATEGORIES : CATEGORIES).map((c) => '<option value="' + esc(c) + '"' + (w.category === c ? ' selected' : '') + '>' + esc(c) + '</option>').join('') +
    '</select><p class="editor-note">分类只用于筛选与组织，不代表门槛：任何分类都可以是草稿、已发布或已停用。</p></div>' +
    '<div><label for="f-usage">用途</label><select id="f-usage" data-scope="work" data-key="usage">' +
    '<option value="external"' + (w.usage === 'external' ? ' selected' : '') + '>对外（可参与客户问答）</option>' +
    '<option value="internal"' + (w.usage === 'internal' ? ' selected' : '') + '>对内（仅企业内部）</option></select>' +
    '<p class="editor-note">用途决定资料对内的说明范围；「是否允许 AI 客服使用」是另一个授权开关。</p></div>' +
    '<div class="full"><label>允许 AI 客服使用（授权设置，发布后生效）</label>' +
    (w.usage === 'internal'
      ? '<p class="editor-note">仅内部资料不参与客户问答，无需也無法开启该授权。</p>'
      : '<label class="check-inline"><input type="checkbox" data-scope="work" data-key="aiAuthorized"' + (w.aiAuthorized ? ' checked' : '') + '><span>发布后可用于 AI 客服的客户问答 <span class="hint">（随时可在详情页调整，调整立即生效）</span></span></label>') +
    '</div>' +
    (isProduct ? '' : '<div class="full"><label for="f-related">关联对象</label><input id="f-related" data-scope="work" data-key="relatedText" value="' + esc(w.relatedText) + '" placeholder="用逗号分隔，例如：3SV08NL007T，报价单 RFQ-0912"></div>') +
    (isProduct ? '<div class="full"><label for="f-related2">关联对象</label><input id="f-related2" data-scope="work" data-key="relatedText" value="' + esc(w.relatedText) + '" placeholder="例如：报价单 RFQ-0912，推荐组合"></div>' : '') +
    '</div></section>');
  if (isProduct) {
    parts.push('<section class="card"><h2>' + esc(w.category) + '的结构化字段</h2>' +
      '<p class="lead" style="margin-bottom:12px">每个字段可以有自己的使用范围（对外 / 仅内部）与核准状态。AI 只会引用「对外且已核准」的字段；这里不是通用知识字段，而是该产品/服务自己的字段模板。</p>' +
      '<div class="table-scroll"><div class="field-head"><span>字段名</span><span>内容</span><span>单位</span><span>使用范围</span><span>核准状态</span><span></span></div><div class="field-rows">' +
      w.fields.map((f, i) => '<div class="field-row">' +
        '<input data-scope="field" data-i="' + i + '" data-key="label" value="' + esc(f.label) + '" placeholder="字段名" aria-label="字段名">' +
        '<input data-scope="field" data-i="' + i + '" data-key="value" value="' + esc(f.value) + '" placeholder="内容" aria-label="内容">' +
        '<input data-scope="field" data-i="' + i + '" data-key="unit" value="' + esc(f.unit) + '" placeholder="单位" aria-label="单位">' +
        '<select data-scope="field" data-i="' + i + '" data-key="usage" aria-label="使用范围"><option value="external"' + (f.usage === 'external' ? ' selected' : '') + '>对外</option><option value="internal"' + (f.usage === 'internal' ? ' selected' : '') + '>仅内部</option></select>' +
        '<select data-scope="field" data-i="' + i + '" data-key="state" aria-label="核准状态"><option value="approved"' + (f.state === 'approved' ? ' selected' : '') + '>已核准</option><option value="pending"' + (f.state === 'pending' ? ' selected' : '') + '>待核准</option></select>' +
        '<button class="del" data-act="del-field" data-i="' + i + '" title="删除字段" aria-label="删除字段">✕</button>' +
        '</div>').join('') +
      '</div></div>' +
      '<div class="actions" style="margin-top:12px"><button class="button secondary" data-act="add-field">＋ 添加字段</button></div>' +
      '<p class="editor-note">字段的「出处」在详情页展示；自定义字段设计器（跨产品的字段模板配置）不在第一批范围，这里保持简单默认模板。</p></section>');
    parts.push('<section class="card"><h2>简介</h2><textarea data-scope="work" data-key="body" rows="4" placeholder="一句话说明这个产品/服务是什么、给谁用">' + esc(w.body) + '</textarea></section>');
  } else if (isFAQ) {
    parts.push('<section class="card faq-editor"><h2>问答内容</h2>' +
      w.faq.map((p, i) => '<div class="faq-row"><div class="row-head"><b>问答 ' + (i + 1) + '</b>' + (w.faq.length > 1 ? '<button class="button ghost" data-act="del-faq" data-i="' + i + '">删除</button>' : '') + '</div>' +
        '<label>问</label><input data-scope="faq" data-i="' + i + '" data-key="q" value="' + esc(p.q) + '" placeholder="客户会怎么问？">' +
        '<label>答</label><textarea data-scope="faq" data-i="' + i + '" data-key="a" rows="3" placeholder="按已核准的口径回答">' + esc(p.a) + '</textarea></div>').join('') +
      '<div class="actions"><button class="button secondary" data-act="add-faq">＋ 添加问答</button></div></section>');
  } else {
    parts.push('<section class="card"><h2>正文</h2><textarea data-scope="work" data-key="body" rows="12" placeholder="直接填写正文；段落之间空一行">' + esc(w.body) + '</textarea>' +
      '<p class="editor-note">正文可以随时编辑；从文件导入的内容在这个位置人工整理。</p></section>');
  }
  parts.push('<div class="actions" style="justify-content:flex-end"><button class="button secondary" data-act="cancel-edit">取消</button><button class="button secondary" data-act="save-draft">保存草稿</button><button class="button" data-act="publish-editor">发布</button></div>');
  return parts.join('');
}

/* ─────────────── 视图：导入 ─────────────── */
function viewImportHTML() {
  if (!state.imp) state.imp = { step: 1, fileName: '', fileSize: 0, fileType: '', read: 'none', title: '', body: '', category: '文件资料', usage: 'internal', authWanted: false, error: '', dirty: false };
  const imp = state.imp;
  const stepOn = (n) => (imp.step === n ? ' on' : '');
  const steps = '<div class="steps" aria-label="导入步骤">' +
    '<span class="step' + stepOn(1) + '"><span class="n">1</span>选文件</span><span class="sep"></span>' +
    '<span class="step' + stepOn(2) + '"><span class="n">2</span>检查识别内容</span><span class="sep"></span>' +
    '<span class="step' + stepOn(3) + '"><span class="n">3</span>保存草稿或发布</span></div>';
  const parts = [];
  parts.push('<p><button class="button ghost" data-act="back-list">← 返回列表</button></p>');
  parts.push('<div class="toolbar"><div><h1>文件导入</h1><p class="lead">文件不会上传到任何服务器。当前批次：TXT / Markdown / CSV 可在本地读取文本预览；PDF / Word 需要登记原件后人工整理正文（自动解析属于后续批次，本原型不模拟）。</p></div></div>');
  parts.push('<div class="demo-note"><span><strong>演示，不上传：</strong>文件只在当前浏览器本地读取预览，关闭或刷新页面即消失；任何文件都不会离开这台设备。</span></div>');
  parts.push(steps);
  if (imp.error) parts.push('<div class="form-alert" style="display:flex">' + esc(imp.error) + '</div>');
  if (imp.step === 1) {
    parts.push('<section class="card"><div class="drop" id="drop" data-act="pick-file" role="button" tabindex="0" aria-label="选择文件">' +
      '<svg class="ic"><use href="#i-file"/></svg><strong>选择文件（TXT / MD / CSV 可预览；PDF / Word 登记原件）</strong>' +
      '<p>本地读取上限 1MB。超过上限或二进制格式不会读取内容，可登记文件名后手工整理正文。</p></div>' +
      '<input type="file" id="file-input" accept=".txt,.md,.csv,.pdf,.doc,.docx" style="display:none">' +
      '<p class="editor-note">提示：原文件默认不对外公开；如需对外使用，请整理出一份可授权内容后再发布。</p></section>');
  }
  if (imp.step === 2) {
    const readNote = imp.read === 'text' ? '已本地读取文本预览（仅浏览器内存）' : '未解析内容：该格式在后续批次才支持自动解析，请在下方人工整理正文';
    parts.push('<section class="card"><h2>检查识别内容（可编辑）</h2>' +
      '<div class="file-register" style="margin-bottom:12px"><b>原文件登记</b>' + esc(imp.fileName || '（未选择）') + ' · ' + esc(imp.fileType || '—') + ' · ' + esc(imp.fileSize ? (imp.fileSize / 1024).toFixed(1) + ' KB' : '—') + '<br><span class="muted">' + esc(readNote) + '；文件信息只保存在当前浏览器会话。</span></div>' +
      '<div class="form-grid">' +
      '<div class="field full"><label for="imp-title">标题 *</label><input id="imp-title" data-scope="imp" data-key="title" value="' + esc(imp.title) + '"><div class="field-msg">请填写标题</div></div>' +
      '<div><label for="imp-cat">分类</label><select id="imp-cat" data-scope="imp" data-key="category">' + CATEGORIES.map((c) => '<option value="' + esc(c) + '"' + (imp.category === c ? ' selected' : '') + '>' + esc(c) + '</option>').join('') + '</select></div>' +
      '<div><label for="imp-usage">用途</label><select id="imp-usage" data-scope="imp" data-key="usage">' +
      '<option value="internal"' + (imp.usage === 'internal' ? ' selected' : '') + '>对内（仅企业内部）</option>' +
      '<option value="external"' + (imp.usage === 'external' ? ' selected' : '') + '>对外（可参与客户问答）</option></select></div>' +
      '</div>' +
      '<label for="imp-body">正文（人工核对与整理）</label><textarea id="imp-body" data-scope="imp" data-key="body" rows="10">' + esc(imp.body) + '</textarea>' +
      '<p class="editor-note">混合内容的文件不承诺自动分出对内 / 对外：请在这里人工整理成可授权的内容；整份原文件默认不公开。</p>' +
      '<div class="actions" style="margin-top:12px"><button class="button secondary" data-act="import-back">上一步</button><button class="button" data-act="import-next">下一步：确认</button></div></section>');
  }
  if (imp.step === 3) {
    parts.push('<section class="card"><h2>确认并保存</h2>' +
      '<div class="file-register" style="margin-bottom:12px"><b>即将保存</b>' +
      '标题：' + esc(imp.title || '（未填）') + '<br>分类：' + esc(imp.category) + ' · 用途：' + esc(usageText(imp.usage)) + '<br>' +
      '正文长度：' + imp.body.length + ' 字 · 原文件：' + esc(imp.fileName || '（未选择）') + '</div>' +
      (imp.usage === 'external' ? '<label class="check-inline"><input type="checkbox" data-scope="imp" data-key="authWanted"' + (imp.authWanted ? ' checked' : '') + '><span>同时允许 AI 客服使用 <span class="hint">（发布后生效；发布时将再次确认）</span></span></label>' : '') +
      '<p class="editor-note">保存草稿后仍可在详情页继续编辑；直接发布则需要通过「对外 AI 发布确认」（仅当对外且授权时）。</p>' +
      '<div class="actions" style="margin-top:12px"><button class="button secondary" data-act="import-back">上一步</button>' +
      '<button class="button secondary" data-act="import-save">保存草稿</button>' +
      '<button class="button" data-act="import-publish">保存并发布</button></div></section>');
  }
  return parts.join('');
}
function buildImportEntry() {
  const imp = state.imp;
  return {
    id: 'k-' + String(Date.now()).slice(-6), type: '文件资料', category: imp.category, title: imp.title.trim(),
    summary: imp.fileName ? '由文件导入：' + imp.fileName : '（手动整理）',
    usage: imp.usage, aiAuthorized: imp.usage === 'external' ? !!imp.authWanted : false, status: 'draft',
    prepared: false, processState: 'idle', validUntil: null, needsReview: true, owner: '当前用户（演示）',
    updatedAt: DEMO_TODAY + ' ' + nowTime(), currentVersion: '', body: imp.body, fields: [], faq: [], related: [],
    sources: [{ id: 's1', kind: imp.fileName ? 'file' : 'manual', label: imp.fileName ? '原文件登记' : '手动创建', detail: imp.fileName ? '原始文件：' + imp.fileName + ' · ' + (imp.fileSize / 1024).toFixed(1) + ' KB · 仅登记文件信息，演示环境不上传' : '由当前用户（演示）创建' }],
    versions: [], draft: null
  };
}
function saveImportDraftOnLeave() {
  // 「保存草稿并离开」在导入场景的实现：把导入内容转为草稿条目，不静默丢弃；
  // 标题为空时明确阻止离开并给出原因（不丢内容），而不是无反应。
  const imp = state.imp;
  if (!imp || !imp.dirty) return { ok: true };
  if (!imp.title.trim()) return { ok: false, msg: '导入内容还没有标题：请先填写标题，再使用「保存草稿并离开」。标题、正文与文件信息仍保留在当前页面，不会丢失。' };
  const e = buildImportEntry();
  DB.entries.unshift(e);
  state.imp = null;
  toast('导入已保存为草稿（原文件未上传）');
  return { ok: true };
}
function importFinish(publish) {
  const imp = state.imp;
  if (!imp) return;
  if (!imp.title.trim()) { imp.error = '请先填写标题。'; render(); return; }
  const e = buildImportEntry();
  DB.entries.unshift(e);
  state.imp = null;
  if (publish) {
    const w = { title: e.title, category: e.category, usage: e.usage, aiAuthorized: e.aiAuthorized, body: e.body, faq: [], fields: [], relatedText: '' };
    const go2 = () => finishPublish(e, w);
    if (e.usage === 'external' && e.aiAuthorized) {
      confirmDialog({ title: '发布对外 AI 版本？', lines: ['这条知识为「对外 + 允许AI客服使用」，发布后客户问答将引用它。', '请确认内容与出处已核对。'], confirmLabel: '确认发布', onConfirm: go2 });
    } else { go2(); }
  } else {
    setRoute({ name: 'entry', id: e.id, tab: 'content' });
    toast('已保存为草稿（原文件未上传）');
    render();
  }
}

/* ─────────────── 视图：模拟客户问答 ─────────────── */
function viewQAHTML() {
  const msgs = state.qa.messages.map((m) => {
    if (m.role === 'center') return '<div class="chat-msg center"><div class="chat-bubble">' + esc(m.text) + '</div></div>';
    if (m.role === 'user') return '<div class="chat-msg right"><div class="chat-bubble">' + esc(m.text) + '</div></div>';
    let cite = '';
    if (m.cite) {
      const ce = byId(m.cite.id);
      const a = ce ? availability(ce) : null;
      cite = '<div class="cite-card"><span><b>《' + esc(m.cite.title) + '》</b> <small>' + esc(m.cite.ver) + (a ? ' · ' + esc(a.label) : '') + '</small></span>' +
        '<button class="button secondary" data-act="open-cite" data-id="' + esc(m.cite.id) + '">查看资料</button></div>';
    }
    const off = m.offline ? '<div class="offline-note">' + esc(m.offline) + '</div>' : '';
    const note = m.note ? '<div class="chat-msg center" style="margin-top:2px"><div class="chat-bubble">' + esc(m.note) + '</div></div>' : '';
    return '<div class="chat-msg left"><div class="chat-bubble">' + esc(m.text) + '</div>' + cite + off + '</div>' + note;
  }).join('');
  const chips = ['发货要几天？', '整机保修多久？', '你们公司成立多久了？', '3SV08NL007T 的流量和扬程是多少？', '可以上门安装吗？覆盖哪些地区？', '工厂参观怎么申请？', '展会报价政策还有效吗？'];
  const pendingBubble = state.qa.pending ? '<div class="chat-msg center"><div class="chat-bubble">正在检索企业已授权的对外资料…（演示）</div></div>' : '';
  return '' +
    '<div class="toolbar"><div><h1>模拟客户 AI 问答</h1>' +
    '<p class="lead">从“客户视角”测试企业知识：只有已发布 + 对外 + 已授权 + 有效 + 版本准备完成的资料才可能被引用；<b>即使管理员测试，内部资料也不会用于这里的回答</b>。</p></div></div>' +
    '<div class="demo-note"><span><strong>演示，不调用模型：</strong>以下答复由页面内置规则从演示资料生成，用于说明「授权范围」与「引用样式」；真实产品中由企业接通的模型服务生成，平台当前未接通，本原型不代表模型能力。</span></div>' +
    '<div class="qa-layout">' +
    '<section class="chat-shell"><div class="chat-head"><span class="chat-avatar">客</span><div class="chat-id"><strong>客户（模拟）</strong><small>来源：企业官网 / WhatsApp 等渠道的客户问答演示</small></div></div>' +
    '<div class="chat-log" id="chat-log">' + msgs + pendingBubble + '</div>' +
    '<div class="qpill-row">' + chips.map((c) => '<button class="qpill" data-act="qa-chip" data-q="' + esc(c) + '">' + esc(c) + '</button>').join('') + '</div>' +
    '<div class="chat-composer"><textarea data-qa-input rows="1" placeholder="以客户身份提问（Enter 发送，Shift+Enter 换行）" aria-label="提问输入"></textarea>' +
    '<button class="button" data-act="qa-send">发送</button></div></section>' +
    '<aside class="rail"><h3>规则说明</h3>' +
    '<dl><div class="rail-item"><dt>检索范围</dt><dd>本企业 + 已发布 + 对外 + 已授权 + 有效 + 当前版本准备完成；字段级再按「对外且已核准」过滤。</dd></div>' +
    '<div class="rail-item"><dt>资料不足时</dt><dd>答复降级为「建议转人工」，不猜测、不编造。</dd></div>' +
    '<div class="rail-item"><dt>与全文搜索的区别</dt><dd>列表页的搜索是「按文字精确匹配」（不使用 AI）；这里演示的是客户问答的回答与引用样式。</dd></div>' +
    '<div class="rail-item"><dt>管理员的内部资料</dt><dd>不会出现在这里的任何回答中——模拟客户视角只使用对外授权知识。</dd></div></dl></aside>' +
    '</div>';
}
function answerQuestion(q) {
  const t = q.trim();
  if (!t) return null;
  const norm = t.toLowerCase();
  const avail = DB.entries.filter((e) => e.qa && availability(e).ok);
  let best = null, bestHits = 0;
  for (const e of avail) {
    const hits = e.qa.keywords.filter((k) => norm.includes(k)).length;
    if (hits > bestHits) { best = e; bestHits = hits; }
  }
  if (best) return { text: best.qa.answer, cite: { id: best.id, ver: best.currentVersion || '', title: best.title }, note: '演示答复：由内置规则匹配预设资料生成，不调用模型。' };
  let alt = null, altHits = 0;
  for (const e of DB.entries) {
    if (!e.qa) continue;
    const hits = e.qa.keywords.filter((k) => norm.includes(k)).length;
    if (hits > altHits) { alt = e; altHits = hits; }
  }
  if (alt) {
    const a = availability(alt);
    return { text: '现有授权资料中没有足够依据回答这个问题。建议转人工处理。', offline: '相关主题的资料当前对 AI 客服不可用（原因：' + (REASON_HINT[a.reason] || a.reason) + '），按规则不用于客户回答。', cite: null, note: '演示答复：资料不足即转人工。' };
  }
  return { text: '现有授权资料中没有足够依据回答这个问题。建议转人工处理。', offline: '没有匹配到演示资料。可以点击上方建议问题体验“引用可用资料”与“资料不足转人工”两种结果。', cite: null, note: '演示答复：资料不足即转人工。' };
}
function sendQuestion(q) {
  if (state.qa.pending) return;
  const t = (q || '').trim();
  if (!t) return;
  state.qa.messages.push({ role: 'user', text: t });
  state.qa.pending = true;
  state.focusComposer = true;
  render();
  window.setTimeout(() => {
    const ans = answerQuestion(t);
    state.qa.pending = false;
    if (ans) state.qa.messages.push({ role: 'bot', text: ans.text, cite: ans.cite, offline: ans.offline, note: ans.note });
    state.focusComposer = true;
    render();
  }, 520);
}

/* ─────────────── 对话框 / Toast ─────────────── */
function confirmDialog(opts) {
  const dlg = $('#dlg');
  if (dlg.open) dlg.close();
  dlg.innerHTML = '<div class="dlg-head"><span class="dlg-icon' + (opts.danger ? ' danger' : '') + '"><svg class="ic"><use href="#i-alert"/></svg></span>' +
    '<h2 class="dlg-title">' + esc(opts.title) + '</h2></div>' +
    '<div class="dlg-body">' + (opts.lines || []).map((l) => '<p>' + esc(l) + '</p>').join('') + '</div>' +
    '<div class="dlg-foot"><button class="button secondary" data-act="dlg-cancel">取消</button>' +
    '<button class="button' + (opts.danger ? ' danger' : '') + '" data-act="dlg-ok">' + esc(opts.confirmLabel || '确认') + '</button></div>';
  state.dlg = { ok: opts.onConfirm || null, skip: null };
  dlg.showModal();
}
function dialogError(msg) {
  const dlg = $('#dlg');
  const old = dlg.querySelector('#dlg-err');
  if (old) old.remove();
  const body = dlg.querySelector('.dlg-body');
  if (body) body.insertAdjacentHTML('beforeend', '<p id="dlg-err" style="color:var(--danger-text)">' + esc(msg) + '</p>');
}
function leaveDialog() {
  const dlg = $('#dlg');
  if (dlg.open) dlg.close();
  dlg.innerHTML = '<div class="dlg-head"><span class="dlg-icon"><svg class="ic"><use href="#i-alert"/></svg></span>' +
    '<h2 class="dlg-title">有未保存的修改</h2></div>' +
    '<div class="dlg-body"><p>离开前可以先把内容保存为草稿；也可以放弃这些修改。</p></div>' +
    '<div class="dlg-foot"><button class="button secondary" data-act="dlg-cancel">继续编辑</button>' +
    '<button class="button danger" data-act="dlg-skip">放弃修改</button>' +
    '<button class="button" data-act="dlg-ok">保存草稿并离开</button></div>';
  state.dlg = {
    ok: () => {
      // 编辑器场景：保存编辑草稿后完成离开。
      if (state.editing) {
        if (saveDraft(false)) { const nx = state.pendingLeave; state.pendingLeave = null; if (nx) nx(); }
        else { dialogError('请先填写标题，再保存离开。'); dlg.showModal(); }
        return;
      }
      // 导入场景：保存导入草稿；标题为空时明确阻止（内容不丢），对话框保持打开并说明原因。
      const r = saveImportDraftOnLeave();
      if (!r.ok) { dialogError(r.msg); dlg.showModal(); return; }
      const nx = state.pendingLeave; state.pendingLeave = null; if (nx) nx();
    },
    skip: () => { state.editing = null; state.imp = null; const nx = state.pendingLeave; state.pendingLeave = null; if (nx) nx(); }
  };
  dlg.showModal();
}
function toast(msg, warn) {
  const box = $('#toasts');
  const el = document.createElement('div');
  el.className = 'toast' + (warn ? ' warn' : '');
  el.textContent = msg;
  box.appendChild(el);
  window.setTimeout(() => el.classList.add('show'), 10);
  window.setTimeout(() => { el.classList.remove('show'); window.setTimeout(() => el.remove(), 250); }, 3400);
}

/* ─────────────── 事件 ─────────────── */
function updateDirtyIndicator() {
  const ind = $('#dirty-indicator');
  if (ind && state.editing) ind.textContent = JSON.stringify(state.editing.working) !== state.editing.baseJSON ? '有未保存的修改' : '暂无未保存修改';
}
function closeAddMenu() { const d = $('#add-wrap'); if (d) d.open = false; }

document.addEventListener('click', (ev) => {
  const t = ev.target.closest('[data-act]');
  if (!t) {
    if (!ev.target.closest('.add-wrap')) closeAddMenu();
    return;
  }
  const act = t.dataset.act;
  const id = t.dataset.id;
  const dlg = t.closest('dialog');
  switch (act) {
    case 'out-of-scope': toast('工作台、收件箱、渠道账号与设置在本次评审范围之外（原型仅覆盖企业知识）。'); break;
    case 'nav': {
      closeAddMenu();
      if (t.dataset.view === 'qa') { go({ name: 'qa' }); break; }
      /* 主按钮与子项都会展开分类：主按钮进入全部列表并展开；进入子视图后保持展开。
         展开只同步导航 DOM，页面跳转仍走 go()，不绕过未保存保护。 */
      state.navExpanded = true;
      syncNavExpandedDOM();
      go({ name: 'list', view: t.dataset.view });
      break;
    }
    case 'nav-toggle': state.navExpanded = !state.navExpanded; syncNavExpandedDOM(); break;
    case 'clear-search': {
      state.filters.q = '';
      const inp = $('#list-search');
      if (inp) inp.value = '';
      if (state.route.name === 'list') refreshList();
      syncSearchClear();
      if (inp) inp.focus();
      break;
    }
    case 'back-list': closeAddMenu(); go({ name: 'list', view: 'all' }); break;
    case 'clear-filters': state.filters = { q: '', category: 'all', status: 'all', usage: 'all', ai: 'all', more: false }; render(); break;
    case 'toggle-more': state.filters.more = !state.filters.more; refreshList(); break;
    case 'open-entry': go({ name: 'entry', id, tab: 'content' }); break;
    case 'tab': setRoute({ name: 'entry', id: state.route.id, tab: t.dataset.tab }); render(); break;
    case 'new-article': closeAddMenu(); startNew('article', 'FAQ'); break;
    case 'new-product': closeAddMenu(); startNew('product', '产品'); break;
    case 'new-file': closeAddMenu(); go({ name: 'import' }); break;
    case 'edit-entry': startEdit(id); break;
    case 'edit-draft': { const e = byId(id || state.route.id); if (e) startEdit(e.id); break; }
    case 'save-draft': saveDraft(true); updateDirtyIndicator(); break;
    case 'publish-editor': requestPublish(null); break;
    case 'cancel-edit': cancelEdit(); break;
    case 'publish-from-entry': { const e = byId(id); if (e) requestPublish({ entry: e, working: contentOf(e) }); break; }
    case 'publish-draft': { const e = byId(id || state.route.id); if (e && e.draft) requestPublish({ entry: e, working: e.draft.content }); break; }
    case 'mark-reviewed': { const e = byId(id || state.route.id); if (e) { e.needsReview = false; toast('已移除“需要核对”提醒；发布时仍会再次检查。'); render(); } break; }
    case 'retry': { const e = byId(id || state.route.id); if (e) retryProcessing(e); break; }
    case 'disable': { const e = byId(id); if (e) disableEntry(e); break; }
    case 'reenable': { const e = byId(id); if (e) reenableEntry(e); break; }
    case 'restore': { const e = byId(id); if (e) restoreVersion(e, t.dataset.ver); break; }
    case 'add-faq': state.editing.working.faq.push({ q: '', a: '' }); render(); break;
    case 'del-faq': state.editing.working.faq.splice(Number(t.dataset.i), 1); render(); break;
    case 'add-field': state.editing.working.fields.push({ label: '', value: '', unit: '', usage: 'external', state: 'approved', source: '' }); render(); break;
    case 'del-field': state.editing.working.fields.splice(Number(t.dataset.i), 1); render(); break;
    case 'pick-file': { const fi = $('#file-input'); if (fi) fi.click(); break; }
    case 'import-back': if (state.imp && state.imp.step > 1) state.imp.step -= 1; render(); break;
    case 'import-next': {
      const imp = state.imp;
      if (!imp.title.trim()) { imp.error = '请先确认标题。'; render(); return; }
      imp.error = ''; imp.step = 3; render(); break;
    }
    case 'import-save': importFinish(false); break;
    case 'import-publish': importFinish(true); break;
    case 'qa-chip': sendQuestion(t.dataset.q); break;
    case 'qa-send': { const ta = $('[data-qa-input]'); sendQuestion(ta ? ta.value : ''); break; }
    case 'open-cite': { setRoute({ name: 'entry', id: t.dataset.id, tab: 'content' }); state.editing = null; render(); break; }
    case 'dlg-ok': { const h = state.dlg && state.dlg.ok; $('#dlg').close(); if (h) h(); else state.pendingLeave = null; break; }
    case 'dlg-skip': { const h = state.dlg && state.dlg.skip; $('#dlg').close(); if (h) h(); else state.pendingLeave = null; break; }
    case 'dlg-cancel': { $('#dlg').close(); if (dlg) { /* leave dialog cancel keeps editing */ } state.pendingLeave = null; break; }
    default: break;
  }
});

document.addEventListener('input', (ev) => {
  const el = ev.target;
  if (!el || !el.dataset || !el.dataset.scope) return;
  const scope = el.dataset.scope, key = el.dataset.key, i = Number(el.dataset.i);
  if (scope === 'filters') {
    state.filters[key] = el.value;
    if (key === 'q') syncSearchClear();
    if (state.route.name === 'list') refreshList();
    return;
  }
  if (scope === 'work' && state.editing) {
    state.editing.working[key] = el.value;
    updateDirtyIndicator();
    return;
  }
  if (scope === 'faq' && state.editing) { state.editing.working.faq[i][key] = el.value; updateDirtyIndicator(); return; }
  if (scope === 'field' && state.editing) { state.editing.working.fields[i][key] = el.value; updateDirtyIndicator(); return; }
  if (scope === 'imp' && state.imp) { state.imp[key] = el.value; state.imp.dirty = true; return; }
});
document.addEventListener('change', (ev) => {
  const el = ev.target;
  if (!el || !el.dataset) return;
  if (el.id === 'role-select') {
    state.role = el.value;
    if (state.role === 'member' && state.editing) { state.editing = null; toast('已切换到仅阅读视角，未保存的修改未生效（演示）。'); }
    render();
    return;
  }
  if (el.dataset.scope === 'filters') { state.filters[el.dataset.key] = el.value; if (state.route.name === 'list') refreshList(); return; }
  if (el.dataset.scope === 'work' && state.editing) {
    state.editing.working[el.dataset.key] = el.type === 'checkbox' ? el.checked : el.value;
    updateDirtyIndicator();
    render();
    return;
  }
  if (el.dataset.scope === 'imp' && state.imp) {
    state.imp[el.dataset.key] = el.type === 'checkbox' ? el.checked : el.value;
    state.imp.dirty = true;
    render();
    return;
  }
  if (el.dataset.act === 'auth-toggle') {
    const e = byId(el.dataset.id);
    if (e) toggleAuth(e, el.checked);
    return;
  }
  if (el.id === 'file-input') {
    const f = el.files && el.files[0];
    if (!f) return;
    state.imp = state.imp || { step: 1, fileName: '', fileSize: 0, fileType: '', read: 'none', title: '', body: '', category: '文件资料', usage: 'internal', authWanted: false, error: '' };
    const imp = state.imp;
    imp.fileName = f.name; imp.fileSize = f.size; imp.fileType = f.type || (f.name.split('.').pop() || '').toUpperCase();
    imp.title = f.name.replace(/\.[^.]+$/, '');
    imp.error = ''; imp.dirty = true; imp.read = 'none'; imp.body = '';
    if (f.size > MAX_IMPORT_BYTES) {
      imp.error = '文件超过 1MB，演示环境不读取内容；可把标题与关键信息手工整理到正文，或先登记文件名。';
      imp.step = 2; render(); return;
    }
    const isText = /\.(txt|md|csv)$/i.test(f.name);
    if (isText) {
      const r = new FileReader();
      r.onload = () => { imp.body = String(r.result || '').slice(0, 20000); imp.read = 'text'; imp.step = 2; render(); };
      r.onerror = () => { imp.error = '本地读取失败：可以在下一步手工填写正文。'; imp.read = 'unsupported'; imp.step = 2; render(); };
      r.readAsText(f, 'utf-8');
    } else {
      imp.read = 'unsupported';
      imp.step = 2;
      render();
    }
    return;
  }
});
document.addEventListener('keydown', (ev) => {
  if (ev.target && ev.target.matches && ev.target.matches('[data-qa-input]') && ev.key === 'Enter' && !ev.shiftKey) {
    ev.preventDefault();
    sendQuestion(ev.target.value);
  }
});
window.addEventListener('beforeunload', (ev) => {
  if (isDirty()) { ev.preventDefault(); ev.returnValue = ''; }
});

/* ─────────────── 初始化 ─────────────── */
(function init() {
  const menuBtn = $('#menu-btn');
  const scrim = $('#side-scrim');
  menuBtn.addEventListener('click', () => {
    const open = document.body.classList.toggle('nav-open');
    menuBtn.setAttribute('aria-expanded', open ? 'true' : 'false');
    scrim.hidden = !open;
  });
  scrim.addEventListener('click', () => { document.body.classList.remove('nav-open'); scrim.hidden = true; });
  $('#role-select').value = state.role;
  render();
})();
