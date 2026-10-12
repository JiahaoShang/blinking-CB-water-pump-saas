'use strict';
/* ════════════════════════════════════════════════════════════════
   企业知识管理 · v0.3 主题与资料设计评审原型（主题浏览 + 统一入库）
   纯前端演示：不读取文件内容、不上传、不调用模型、无网络请求；
   所有数据只存在于内存，刷新即重置。识别结果只通过明确的
   「使用示例整理结果（演示）」操作填入，不使用任何计时器冒充处理；
   图片 / 视频 / 网页不会被伪装成识别成功，只登记原件。
   ════════════════════════════════════════════════════════════════ */

/* ---------- 基础工具 ---------- */
const qs = (s, r) => (r || document).querySelector(s);
const qsa = (s, r) => Array.from((r || document).querySelectorAll(s));
const esc = (v) => String(v == null ? '' : v).replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const uid = (p) => (p || 'id') + '-' + Math.random().toString(36).slice(2, 8);
function todayStr() { const d = new Date(); return d.getFullYear() + '-' + String(d.getMonth() + 1).padStart(2, '0') + '-' + String(d.getDate()).padStart(2, '0'); }
/* 到期时间（可选，北京时间 = UTC+8 固定偏移；留空 = 长期有效）。
   比较一律用精确时刻，不按天做词典式判断，也不把已写明的时刻延长到整天。 */
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

/* ---------- 标签与枚举 ---------- */
const FORMAT = {
  pdf: { label: 'PDF' }, docx: { label: 'DOCX' }, image: { label: '图片' },
  video: { label: '视频' }, web: { label: '网页 / 在线文章' }, manual: { label: '手动正文' },
  other: { label: '其他文件' }
};
const FORMAT_ORDER = ['pdf', 'docx', 'image', 'video', 'web', 'manual', 'other'];
const USAGE_LABEL = { internal: '仅内部使用', external: '对外使用' };

/* ---------- 演示数据：青禾企业（跨行业示例，两个独立维度：资料形式 × 内容主题） ---------- */
const DB = { groups: [], topics: [], materials: [] };

function seed() {
  DB.groups = [
    { id: 'g_product', name: '产品与交付' },
    { id: 'g_team', name: '团队与文化' }
  ];
  const snap = (m) => { m.versions[m.versions.length - 1].snapshot = clone({ name: m.name, paras: m.paras }); };
  DB.materials = [
    {
      id: 'm_spec', name: '会议设备A-产品规格书.pdf', format: 'pdf', size: '1.1 MB', url: '',
      lifecycle: 'published', usage: 'external', aiOn: true, expiry: '', aiIssue: '',
      recognition: 'recognized', failReason: '', updated: '2026-09-12', currentVersion: 1, updateDraft: null,
      paras: [
        { id: 'sp1', text: '智能会议设备A是一体化视频会议终端，面向 8–16 人的中小型会议室：内置摄像头、麦克风阵列与扬声器，接线后一键入会。', loc: '第 1 页', checked: true, modified: false },
        { id: 'sp2', text: '适用会议室 8–16 人；支持同时接入 2 路无线投屏；有线投屏不受此限。', loc: '第 2 页', checked: true, modified: false },
        { id: 'sp3', text: '随机配件：遥控器、电源适配器、壁挂支架（含安装螺丝包）。', loc: '第 3 页', checked: true, modified: false },
        { id: 'sp4', text: '整机保修 2 年；随附配件保修 6 个月。', loc: '第 3 页', checked: true, modified: false }
      ],
      fields: [{ id: 'spf1', name: '整机重量', type: 'number', unit: 'kg', value: '1.8', source: '产品规格书 · 第 2 页' }],
      topics: ['t_meeting'],
      versions: [{ v: 1, date: '2026-09-12', note: '首次导入' }]
    },
    {
      id: 'm_guide', name: '会议设备A-安装指南.docx', format: 'docx', size: '320 KB', url: '',
      lifecycle: 'published', usage: 'internal', aiOn: false, expiry: '', aiIssue: '',
      recognition: 'recognized', failReason: '', updated: '2026-09-20', currentVersion: 1, updateDraft: null,
      paras: [
        { id: 'gp1', text: '安装前：确认墙面或桌面承重，核对配件清单。', loc: '段落 1', checked: true, modified: false },
        { id: 'gp2', text: '壁挂安装：使用随机挂架，按孔位固定后锁紧安全扣。', loc: '段落 2', checked: true, modified: false },
        { id: 'gp3', text: '首次通电：指示灯常亮后按遥控器任意键，等待系统自检完成。', loc: '段落 3', checked: true, modified: true },
        { id: 'gp4', text: '培训要点：新同事需在指导下完成 2 次实际安装后独立操作。', loc: '段落 4', checked: true, modified: false }
      ],
      fields: [],
      topics: ['t_meeting', 't_onboarding'],
      versions: [{ v: 1, date: '2026-09-20', note: '首次导入；段落 3 为成员人工修改' }]
    },
    {
      id: 'm_photo', name: '设备A-整机照片.jpg', format: 'image', size: '2.4 MB', url: '',
      lifecycle: 'published', usage: 'internal', aiOn: false, expiry: '', aiIssue: '',
      recognition: 'registered', failReason: '', updated: '2026-09-25', currentVersion: 1, updateDraft: null,
      paras: [], fields: [], topics: ['t_meeting'],
      versions: [{ v: 1, date: '2026-09-25', note: '仅登记原件' }]
    },
    {
      id: 'm_video', name: '设备A-操作演示.mp4', format: 'video', size: '18 MB', url: '',
      lifecycle: 'published', usage: 'internal', aiOn: false, expiry: '', aiIssue: '',
      recognition: 'registered', failReason: '', updated: '2026-09-26', currentVersion: 1, updateDraft: null,
      paras: [], fields: [], topics: ['t_meeting'],
      versions: [{ v: 1, date: '2026-09-26', note: '仅登记原件（视频本版不生成正文）' }]
    },
    {
      id: 'm_web', name: '设备A 官网产品介绍', format: 'web', size: '—', url: 'https://example.com/meeting-device-a',
      lifecycle: 'published', usage: 'internal', aiOn: false, expiry: '', aiIssue: '',
      recognition: 'registered', failReason: '', updated: '2026-09-28', currentVersion: 1, updateDraft: null,
      paras: [], fields: [], topics: ['t_meeting'],
      versions: [{ v: 1, date: '2026-09-28', note: '登记网页地址（内容采集为后续候选，待评审）' }]
    },
    {
      id: 'm_manual', name: '客户常见问答（客服手动整理）', format: 'manual', size: '—', url: '',
      lifecycle: 'published', usage: 'external', aiOn: true, expiry: '', aiIssue: '',
      recognition: 'manual', failReason: '', updated: '2026-09-30', currentVersion: 1, updateDraft: null,
      paras: [
        { id: 'mp1', text: '常见问题：投屏没有画面时，先检查线缆与输入源；仍无法解决时联系售后。', loc: '人工撰写', checked: true, modified: false },
        { id: 'mp2', text: '设备重启：长按电源键 5 秒。', loc: '人工撰写', checked: true, modified: false }
      ],
      fields: [], topics: ['t_aftersale'],
      versions: [{ v: 1, date: '2026-09-30', note: '手动创建' }]
    },
    {
      id: 'm_doa', name: '售后服务标准-2025.pdf', format: 'pdf', size: '860 KB', url: '',
      lifecycle: 'published', usage: 'external', aiOn: true, expiry: '2027-06-30T23:59', aiIssue: '',
      recognition: 'recognized', failReason: '', updated: '2026-06-02', currentVersion: 1, updateDraft: null,
      paras: [
        { id: 'dp1', text: '一般咨询在 4 小时内响应；上门服务在确认后 8 小时内完成。', loc: '第 1 页', checked: true, modified: false },
        { id: 'dp2', text: '整机保修 2 年；人为损坏与耗材不在保修范围。', loc: '第 2 页', checked: true, modified: false }
      ],
      fields: [], topics: ['t_aftersale'],
      versions: [{ v: 1, date: '2026-06-02', note: '首次导入' }]
    },
    {
      id: 'm_scan', name: '维修巡检表（扫描件）.pdf', format: 'pdf', size: '3.6 MB', url: '',
      lifecycle: 'published', usage: 'internal', aiOn: false, expiry: '', aiIssue: '',
      recognition: 'failed', failReason: '无文本层：没有识别到可编辑文字，未能生成正文。原件已保留，可人工整理正文。',
      updated: '2026-08-18', currentVersion: 1, updateDraft: null,
      paras: [], fields: [], topics: ['t_aftersale'],
      versions: [{ v: 1, date: '2026-08-18', note: '仅登记原件' }]
    },
    {
      id: 'm_training', name: '入职培训手册.pdf', format: 'pdf', size: '540 KB', url: '',
      lifecycle: 'published', usage: 'internal', aiOn: false, expiry: '', aiIssue: '',
      recognition: 'recognized', failReason: '', updated: '2026-10-02', currentVersion: 1, updateDraft: null,
      paras: [
        { id: 'np1', text: '入职第一周：完成账号开通、设备领取与安全培训。', loc: '第 1 页', checked: true, modified: false },
        { id: 'np2', text: '工作时间：周一至周五 9:30–18:30，午休 12:30–13:30。', loc: '第 2 页', checked: true, modified: false }
      ],
      fields: [], topics: ['t_onboarding'],
      versions: [{ v: 1, date: '2026-10-02', note: '首次导入' }]
    },
    {
      id: 'm_story', name: '公司故事与团队手册.docx', format: 'docx', size: '210 KB', url: '',
      lifecycle: 'published', usage: 'external', aiOn: false, expiry: '', aiIssue: '',
      recognition: 'recognized', failReason: '', updated: '2026-07-15', currentVersion: 1, updateDraft: null,
      paras: [
        { id: 'yp1', text: '青禾企业成立于 2018 年，从 12 人的小团队起步。', loc: '段落 1', checked: true, modified: false },
        { id: 'yp2', text: '我们的服务承诺：响应快、交付稳、复盘准。', loc: '段落 2', checked: true, modified: false }
      ],
      fields: [], topics: ['t_culture'],
      versions: [{ v: 1, date: '2026-07-15', note: '首次导入' }]
    },
    {
      id: 'm_old', name: '2024 年产品目录（停用归档）', format: 'pdf', size: '4.2 MB', url: '',
      lifecycle: 'disabled', usage: 'internal', aiOn: false, expiry: '', aiIssue: '',
      recognition: 'registered', failReason: '', updated: '2026-01-10', currentVersion: 1, updateDraft: null,
      paras: [], fields: [], topics: [],
      versions: [{ v: 1, date: '2026-01-10', note: '仅登记原件' }]
    }
  ];
  DB.materials.forEach(snap);
  DB.topics = [
    {
      id: 't_meeting', name: '智能会议设备A', group: 'g_product', lifecycle: 'published',
      usage: 'external', aiOn: true, expiry: '', updated: '2026-10-08', draft: null,
      intro: [
        { id: 'ti1', text: '智能会议设备A是一体化视频会议终端，面向 8–16 人的中小型会议室：内置摄像头、麦克风阵列与扬声器，接线后一键入会。', source: { mid: 'm_spec', loc: '第 1 页' }, checked: true, modified: false, system: true },
        { id: 'ti2', text: '设备支持有线与无线投屏；常见故障的处理与巡检安排见「售后标准」主题。', source: { mid: 'm_spec', loc: '第 1 页' }, checked: true, modified: true, system: true }
      ],
      points: [
        { id: 'tp1', text: '适用会议室：8–16 人', source: { mid: 'm_spec', loc: '第 2 页' } },
        { id: 'tp2', text: '安装方式：桌面放置或壁挂，需接通电源与网络', source: { mid: 'm_guide', loc: '段落 1' } },
        { id: 'tp3', text: '随机配件：遥控器、电源适配器、挂架', source: { mid: 'm_spec', loc: '第 3 页' } }
      ],
      related: ['t_aftersale'],
      versions: [{ v: 1, date: '2026-10-08', note: '系统整理初稿经成员确认后发布' }]
    },
    {
      id: 't_aftersale', name: '售后标准', group: 'g_product', lifecycle: 'published',
      usage: 'external', aiOn: true, expiry: '', updated: '2026-10-05', draft: null,
      intro: [
        { id: 'ai1', text: '一般咨询在 4 小时内响应；上门服务在确认后 8 小时内完成。', source: { mid: 'm_doa', loc: '第 1 页' }, checked: true, modified: false, system: true }
      ],
      points: [
        { id: 'ap1', text: '整机保修 2 年；人为损坏与耗材不在保修范围', source: { mid: 'm_doa', loc: '第 2 页' } },
        { id: 'ap2', text: '投屏无画面、设备重启等常见处理见客户常见问答', source: { mid: 'm_manual', loc: '人工撰写' } }
      ],
      related: ['t_meeting'],
      versions: [{ v: 1, date: '2026-10-05', note: '系统整理初稿经成员确认后发布' }]
    },
    {
      id: 't_onboarding', name: '入职培训', group: 'g_team', lifecycle: 'published',
      usage: 'internal', aiOn: false, expiry: '', updated: '2026-10-02', draft: null,
      intro: [
        { id: 'oi1', text: '入职第一周：完成账号开通、设备领取与安全培训。', source: { mid: 'm_training', loc: '第 1 页' }, checked: true, modified: false, system: true }
      ],
      points: [
        { id: 'op1', text: '工作时间：周一至周五 9:30–18:30', source: { mid: 'm_training', loc: '第 2 页' } },
        { id: 'op2', text: '安装类岗位：按《会议设备A-安装指南.docx》完成 2 次带教安装', source: { mid: 'm_guide', loc: '段落 4' } }
      ],
      related: [],
      versions: [{ v: 1, date: '2026-10-02', note: '系统整理初稿经成员确认后发布' }]
    },
    {
      id: 't_culture', name: '企业文化', group: 'g_team', lifecycle: 'published',
      usage: 'external', aiOn: false, expiry: '', updated: '2026-07-20', draft: null,
      intro: [
        { id: 'ci1', text: '青禾企业成立于 2018 年，从 12 人的小团队起步。', source: { mid: 'm_story', loc: '段落 1' }, checked: true, modified: false, system: true }
      ],
      points: [
        { id: 'cp1', text: '服务承诺：响应快、交付稳、复盘准', source: { mid: 'm_story', loc: '段落 2' } }
      ],
      related: [],
      versions: [{ v: 1, date: '2026-07-20', note: '系统整理初稿经成员确认后发布' }]
    },
    {
      id: 't_quote', name: '报价与合同流程', group: null, lifecycle: 'draft',
      usage: 'internal', aiOn: false, expiry: '', updated: '2026-10-10', draft: null,
      intro: [], points: [], related: [], versions: []
    }
  ];
}

/* ---------- 状态 ---------- */
const state = {
  route: { name: 'topics', id: null, step: 1 },
  backStack: [],           // 轻量内部返回栈（不依赖浏览器历史）：每层记录来源页面与去向标签
  role: 'admin',
  filters: { q: '', format: '', topic: '', status: '' },
  topicQ: '',
  navGroups: {},           // gid -> 展开态（主题浏览树的主题分组；默认收起）
  navTopicFiles: {},       // tid -> 展开态（主题下的关联文件列表；默认收起，跨阅读/返回保持）
  ovFolds: {},             // gid -> 展开态（中央总览分组；默认展开；与 navGroups 独立，跨进入主题/返回保持）
  manageGroups: false,
  topicEdit: null,         // 主题编辑工作副本
  work: null, workDirty: false,     // 添加资料三步工作副本
  update: null, updateDirty: false, // 更新资料工作副本
  dlgActions: [], lastFocus: null, pendingNav: null
};

/* ---------- 读取与计算 ---------- */
function materialById(id) { return DB.materials.find((m) => m.id === id) || null; }
function topicById(id) { return DB.topics.find((t) => t.id === id) || null; }
function groupById(id) { return DB.groups.find((g) => g.id === id) || null; }
function topicsOfGroup(gid) { return DB.topics.filter((t) => t.group === gid); }
function ungroupedTopics() { return DB.topics.filter((t) => !t.group); }
function hasBody(m) { return m.paras.some((p) => String(p.text).trim() !== ''); }
function uncheckedCount(m) { return m.paras.filter((p) => !p.checked && String(p.text).trim() !== '').length; }
/* 材料级识别呈现：识别/人工正文/仅登记/未能识别，与生命周期、AI 可用是独立维度 */
function recogInfo(m) {
  if (m.recognition === 'manual') return { cls: 'done', label: uncheckedCount(m) ? '人工正文 · 待核对 ' + uncheckedCount(m) + ' 段' : '人工正文' };
  if (m.recognition === 'failed') return { cls: 'gone', label: '未能识别' };
  if (m.recognition === 'registered') return { cls: 'gone', label: '仅登记原件' };
  const un = uncheckedCount(m);
  if (!m.paras.length) return { cls: 'gone', label: '仅登记原件' };
  return un ? { cls: 'review', label: '待核对 ' + un + ' 段' } : { cls: 'done', label: '已识别 · 已核对' };
}
function notExpired(v) { const exp = expiryInstant(v); return !exp || Date.now() < exp.getTime(); }
/* AI 可用性（发布状态 + AI 可用分开）：已发布可用绿；已发布 AI 不可用蓝灰并给原因 */
function availability(m) {
  if (m.lifecycle === 'disabled') return { pill: 'off', label: '已停用', reason: '已停用：不用于客户回答；成员仍可阅读档案。' };
  if (m.lifecycle === 'draft') return { pill: 'draft', label: '草稿', reason: '草稿：尚未发布，不会用于 AI 客服。' };
  const noBody = !hasBody(m);
  if (!m.aiOn) return { pill: 'pub', label: '已发布（AI 客服不可用）', reason: noBody ? '仅登记原件：没有可核对正文，无法用于 AI 客服。' : '未授权用于 AI 客服（可在发布设置中开启）。' };
  if (noBody) return { pill: 'pub', label: '已发布（AI 客服不可用）', reason: '仅登记原件：没有可核对正文，无法用于 AI 客服。' };
  if (uncheckedCount(m)) return { pill: 'pub', label: '已发布（AI 客服不可用）', reason: '正文还有未核对段落：核对完成后才可用于 AI 客服。' };
  if (m.usage === 'internal') return { pill: 'pub', label: '已发布（AI 客服不可用）', reason: '用途为仅内部：不会用于对外 AI 回答。' };
  if (!notExpired(m.expiry)) return { pill: 'pub', label: '已发布（AI 客服不可用）', reason: '已到到期时间（按北京时间精确判断）：不再用于 AI 客服。' };
  return { pill: 'ok', label: '已发布（AI 客服可用）', reason: '' };
}
/* 主题正文里的来源是否具备对外 AI 条件：已发布 + 对外 + 已授权 + 未到期 + 有已核对正文 */
function sourceEligible(mid) {
  const m = materialById(mid);
  if (!m) return false;
  return m.lifecycle === 'published' && m.usage === 'external' && m.aiOn && notExpired(m.expiry) && hasBody(m) && !uncheckedCount(m);
}
function topicSourceParas(t) { return t.intro.concat(t.points); }
function topicExternalGaps(t) { return topicSourceParas(t).filter((p) => p.source && !sourceEligible(p.source.mid)); }
/* 主题的发布 / AI 双标签（与资料一致）：绿=已发布（AI 客服可用）；蓝灰=已发布（AI 客服不可用）并给原因；
   草稿保持生命周期标签。无 AI 授权不单用绿色暗示可用。 */
function topicAvailability(t) {
  if (t.lifecycle === 'draft') return { pill: 'draft', label: '草稿', reason: '', warn: '' };
  if (!t.aiOn) return { pill: 'pub', label: '已发布（AI 客服不可用）', reason: '未授权用于 AI 客服。', warn: '' };
  const paras = topicSourceParas(t);
  if (!paras.length) return { pill: 'pub', label: '已发布（AI 客服不可用）', reason: '暂无正文，没有可引用的内容。', warn: '' };
  if (!notExpired(t.expiry)) return { pill: 'pub', label: '已发布（AI 客服不可用）', reason: '已到到期时间（按北京时间判断）。', warn: '' };
  if (t.usage !== 'external') return { pill: 'pub', label: '已发布（AI 客服不可用）', reason: '用途为仅内部。', warn: '' };
  const gaps = topicExternalGaps(t);
  return { pill: 'ok', label: '已发布（AI 客服可用）', reason: '', warn: gaps.length ? gaps.length + ' 条内容的来源不符合对外条件（已标注），不会用于对外回答。' : '' };
}
function existingTopicsForSelect(excludeIds) {
  return DB.topics.filter((t) => !(excludeIds || []).includes(t.id));
}
function sourceLabel(para) {
  if (!para.source) return null;
  const m = materialById(para.source.mid);
  return '《' + (m ? m.name : '已移除资料') + '》 · ' + para.source.loc;
}

/* ---------- 左侧主导航（知识四项）＋ 工作区内部主题树 ---------- */
/* 侧栏主导航：知识四项（全部主题 / 全部资料 / 添加资料 / 问答测试）——与第六轮前一致；「添加资料」沿用编辑权限控制（仅管理员/获授权成员）。
   主题浏览树按已确认布局放在工作区内部左列，不放侧栏；顶部功能导航与平台其他模块占位已按用户纠正撤回（2026-10-12）。 */
function sideNavHTML() {
  const r = state.route;
  const isTopics = r.name === 'topics' || r.name === 'topic';
  const isMaterials = r.name === 'materials' || r.name === 'material';
  let h = '<div class="side-heading">知识</div>';
  h += '<button class="side-link' + (isTopics ? ' active' : '') + '" type="button" data-act="nav" data-view="topics"><svg class="ic"><use href="#i-book"/></svg>全部主题</button>';
  h += '<button class="side-link' + (isMaterials ? ' active' : '') + '" type="button" data-act="nav" data-view="materials"><svg class="ic"><use href="#i-file"/></svg>全部资料</button>';
  if (state.role === 'admin') {
    h += '<button class="side-link' + (r.name === 'capture' || r.name === 'update' ? ' active' : '') + '" type="button" data-act="nav" data-view="capture"><svg class="ic"><use href="#i-upload"/></svg>添加资料</button>';
  }
  h += '<button class="side-link' + (r.name === 'qa' ? ' active' : '') + '" type="button" data-act="nav" data-view="qa"><svg class="ic"><use href="#i-inbox"/></svg>问答测试</button>';
  return h;
}
/* 主题浏览树（工作区内部左列 + 窄屏折叠）：pfx 区分两套 DOM（'' 桌面列 / 'm' 窄屏 details），展开状态同一份。
   结构：分组 → 主题 → 主题下关联文件（数据仍为 DB.materials 的 topics 多对多：一份资料关联多个主题时
   是同一实体，只在各主题下分别列出，不复制归档；只关联真正相关的资料，不按资料形式限制主题）。
   主题行 = 主题名按钮（进入正文）+ 独立折叠按钮（带数量；只开合文件列表，不导航、不压返回层）；
   分组与主题文件默认收起，展开状态记在轻量 UI 状态上（navGroups / navTopicFiles）。 */
const FORMAT_ICON = { pdf: 'i-file', docx: 'i-file', image: 'i-image', video: 'i-video', web: 'i-globe', manual: 'i-pen', other: 'i-file' };
function topicTreeHTML(pfx, activeId, activeMid) {
  let h = '<div class="ts-head">主题</div>';
  DB.groups.forEach((g) => {
    const open = !!state.navGroups[g.id];
    const kids = topicsOfGroup(g.id);
    h += '<button class="ts-link ts-grp" type="button" id="' + pfx + 'nav-grpbtn-' + g.id + '" data-act="nav-toggle-group" data-gid="' + g.id + '" aria-expanded="' + (open ? 'true' : 'false') + '">' +
      '<svg class="ic"><use href="#i-folder"/></svg><span class="grow">' + esc(g.name) + '</span><span class="ts-count">' + kids.length + '</span>' +
      '<svg class="ic ts-caret"><use href="#i-chev"/></svg></button>';
    h += '<div class="ts-subs' + (open ? ' open' : '') + '" id="' + pfx + 'nav-sub-' + g.id + '"' + (open ? '' : ' inert') + '><div class="ts-subs-inner">';
    kids.forEach((t) => { h += topicTreeItemHTML(t, pfx, activeId, activeMid); });
    h += '</div></div>';
  });
  ungroupedTopics().forEach((t) => { h += topicTreeItemHTML(t, pfx, activeId, activeMid); });
  if (state.role === 'admin') {
    h += '<button class="ts-link ts-mini" type="button" data-act="topic-new-open"><svg class="ic"><use href="#i-plus"/></svg>新建主题</button>';
  }
  return h;
}
function topicTreeItemHTML(t, pfx, activeId, activeMid) {
  const on = activeId === t.id;
  const mats = DB.materials.filter((m) => m.topics.includes(t.id));
  const open = !!state.navTopicFiles[t.id];
  let h = '<div class="ts-topic-row">' +
    '<button class="ts-link ts-item' + (on ? ' active' : '') + '" type="button" data-act="nav-topic" data-id="' + t.id + '">' +
      '<span class="ts-dot"></span><span class="grow nav-topic-name" title="' + esc(t.name) + '">' + esc(t.name) + '</span>' + (t.lifecycle === 'draft' ? '<span class="draft-mini">草稿</span>' : '') + '</button>' +
    '<button class="ts-filesbtn" type="button" data-act="nav-topic-files" data-id="' + t.id + '" id="' + pfx + 'nav-filesbtn-' + t.id + '" aria-expanded="' + (open ? 'true' : 'false') + '" aria-controls="' + pfx + 'nav-files-' + t.id + '" aria-label="展开或收起「' + esc(t.name) + '」的关联文件（' + mats.length + '）">' +
      '<svg class="ic ts-caret"><use href="#i-chev"/></svg><span class="ts-filesbtn-n">' + mats.length + '</span></button></div>';
  h += '<div class="ts-files' + (open ? ' open' : '') + '" id="' + pfx + 'nav-files-' + t.id + '"' + (open ? '' : ' inert') + '><div class="ts-files-inner">';
  if (mats.length) {
    mats.forEach((m) => {
      const fileOn = activeMid === m.id;
      h += '<button class="ts-file' + (fileOn ? ' active' : '') + '" type="button" data-act="nav-file" data-id="' + m.id + '" title="' + esc(m.name) + '">' +
        '<svg class="ic"><use href="#' + (FORMAT_ICON[m.format] || 'i-file') + '"/></svg><span class="ts-file-name">' + esc(m.name) + '</span></button>';
    });
  } else {
    h += '<p class="ts-none">无关联资料</p>';
  }
  h += '</div></div>';
  return h;
}
/* 分组展开/收起的 DOM 同步（动画 200ms；收起时子项 inert、不可聚焦、读屏跳过）；桌面与窄屏两套树同步 */
function toggleNavGroup(gid) {
  state.navGroups[gid] = !state.navGroups[gid];
  const open = state.navGroups[gid];
  ['', 'm'].forEach((pfx) => {
    const btn = qs('#' + pfx + 'nav-grpbtn-' + gid);
    const sub = qs('#' + pfx + 'nav-sub-' + gid);
    if (btn) btn.setAttribute('aria-expanded', open ? 'true' : 'false');
    if (sub) {
      if (sub.classList) sub.classList.toggle('open', open);
      if (open) { if (sub.removeAttribute) sub.removeAttribute('inert'); }
      else { if (sub.setAttribute) sub.setAttribute('inert', ''); }
    }
  });
}
/* 主题文件列表的开合（动画 200ms）：只更新该主题的折叠按钮与文件列表，不整页重渲染；
   收起时子项 inert、不可聚焦、读屏跳过；桌面与窄屏两套树同步。状态在 state.navTopicFiles → 跨阅读/返回保持 */
function toggleNavTopicFiles(tid) {
  state.navTopicFiles[tid] = !state.navTopicFiles[tid];
  const open = state.navTopicFiles[tid];
  ['', 'm'].forEach((pfx) => {
    const btn = qs('#' + pfx + 'nav-filesbtn-' + tid);
    const reg = qs('#' + pfx + 'nav-files-' + tid);
    if (btn) btn.setAttribute('aria-expanded', open ? 'true' : 'false');
    if (reg) {
      if (reg.classList) reg.classList.toggle('open', open);
      if (open) { if (reg.removeAttribute) reg.removeAttribute('inert'); }
      else { if (reg.setAttribute) reg.setAttribute('inert', ''); }
    }
  });
}
function renderNav() { const el = qs('#side-nav'); if (el) el.innerHTML = sideNavHTML(); }
/* 窄屏「浏览主题」在选中主题或文件后收起（桌面无此元素或本就收起，调用安全） */
function closeMobileTopicBrowse() { const d = qs('#topic-browse-details'); if (d && d.open) d.open = false; }

/* ---------- 路由渲染 ---------- */
function render() {
  renderNav();
  const r = state.route;
  const app = qs('#app');
  let html = '';
  if (r.name === 'topics') html = topicsHTML();
  else if (r.name === 'topic') {
    const t = topicById(r.id);
    html = !t ? missingHTML('主题') : (topicEditFor(r.id) ? topicEditHTML(t) : topicHTML(r.id));
  }
  else if (r.name === 'materials') html = materialsHTML();
  else if (r.name === 'material') html = materialById(r.id) ? materialHTML(r.id) : missingHTML('资料');
  else if (r.name === 'capture') html = captureHTML();
  else if (r.name === 'update') html = updateHTML();
  else if (r.name === 'qa') html = qaHTML();
  else html = missingHTML('页面');
  /* 主题 / 文件浏览列（工作区内部左列）：从「全部主题」总览（route=topics）起即显示；
     主题详情 / 编辑页继续显示；由该树打开的资料详情（总览树或主题树，用既有 backStack 顶层判定）同样保留；
     经「全部资料」列表打开的资料与其他页不显示（不扩展）。
     返回上下文只用这份轻量 UI 状态与既有 backStack：总览树直开文件 → 返回实际总览（不假装曾进入主题正文）；
     主题树开文件 → 返回原主题，再返回总览。 */
  const bTop = state.backStack[state.backStack.length - 1];
  const bTopTopic = !!(bTop && bTop.name === 'topic' && topicById(bTop.id));
  const bTopOverview = !!(bTop && bTop.name === 'topics');
  const treeCtx = r.name === 'topics' || r.name === 'topic' || (r.name === 'material' && (bTopTopic || bTopOverview));
  if (treeCtx) {
    const activeId = r.name === 'topic' ? r.id : (bTopTopic ? bTop.id : null);
    const activeMid = r.name === 'material' ? r.id : null;
    html = '<div class="workspace-grid">' +
      '<aside class="topic-side" aria-label="主题浏览导航"><div class="topic-side-inner">' + topicTreeHTML('', activeId, activeMid) + '</div></aside>' +
      '<div class="ws-main">' +
        '<details class="topic-side-details" id="topic-browse-details"><summary>浏览主题</summary><div class="topic-side-inner">' + topicTreeHTML('m', activeId, activeMid) + '</div></details>' +
        html +
      '</div></div>';
  }
  app.innerHTML = html;
  const crumb = qs('#crumb');
  if (crumb) crumb.innerHTML = crumbHTML();
  if (window.scrollTo) window.scrollTo({ top: 0 });
}
function crumbHTML() {
  const r = state.route;
  if (r.name === 'topic') { const t = topicById(r.id); return '企业知识 · 主题 · <b>' + esc(t ? t.name : '') + '</b>'; }
  if (r.name === 'material') { const m = materialById(r.id); return '企业知识 · 资料 · <b>' + esc(m ? m.name : '') + '</b>'; }
  if (r.name === 'capture') return '企业知识 · <b>添加资料</b>';
  if (r.name === 'update') return '企业知识 · <b>更新资料</b>';
  if (r.name === 'materials') return '企业知识 · <b>全部资料</b>';
  if (r.name === 'qa') return '企业知识 · <b>问答测试</b>';
  return '企业知识 · <b>全部主题</b>';
}
function missingHTML(kind) {
  return '<div class="empty"><h2>找不到该' + esc(kind) + '</h2><p>它可能已被移除，或链接已过期（演示数据不持久，刷新后会重置）。</p>' +
    '<button class="button" type="button" data-act="nav" data-view="topics">返回全部主题</button></div>';
}
function qaHTML() {
  return '<div class="toolbar"><div><h1>问答测试</h1><p class="lead">沿用第一批的问答测试入口，本原型不重复实现。</p></div></div>' +
    '<div class="info-note"><svg class="ic"><use href="#i-alert"/></svg><span>真实问答质量以实现阶段验收为准，不在本设计评审范围内。</span></div>';
}

/* ---------- 全部主题（默认入口） ---------- */
/* 中央总览分组折叠（2026-10-12 第十轮，用户批准）：组头标题内为真实按钮（Enter/Space 原生），控制下方主题行；
   默认展开（保持原视觉）；折叠状态记在 state.ovFolds → 跨进入主题 / 返回保持；与左侧浏览树的 navGroups 互不联动；
   搜索时命中组临时展开、不改用户状态，清空关键词后恢复用户原折叠状态；收起时子区 inert（不可聚焦、读屏跳过），
   开合动画 200ms、reduced-motion 全局禁用；管理分组的改名 / 删除按钮在折叠按钮之外（不嵌套 button）。 */
function ovGroupDisplayOpen(gid) { return state.topicQ.trim() ? true : (state.ovFolds[gid] === undefined ? true : !!state.ovFolds[gid]); }
function groupMngHTML(gid) {
  return (state.role === 'admin' && state.manageGroups)
    ? '<span class="mng-controls"><button class="linkbtn" type="button" data-act="group-rename" data-gid="' + gid + '">改名</button><button class="linkbtn danger-link" type="button" data-act="group-delete" data-gid="' + gid + '">删除分组</button></span>'
    : '';
}
function ovSecHTML(gid, name, icon, count, mng, rowsHTML) {
  const open = ovGroupDisplayOpen(gid);
  return '<section class="topic-sec"><div class="topic-sec-head"><h2><button class="sec-fold" type="button" data-act="ov-fold" data-gid="' + gid + '" id="ov-fold-btn-' + gid + '" aria-expanded="' + (open ? 'true' : 'false') + '" aria-controls="ov-body-' + gid + '">' +
    (icon ? '<svg class="ic"><use href="#i-folder"/></svg>' : '') + esc(name) +
    '<span class="topic-count">' + count + ' 个主题</span><svg class="ic sec-caret"><use href="#i-chev"/></svg></button></h2>' + (mng || '') +
    '</div><div class="sec-body' + (open ? ' open' : '') + '" id="ov-body-' + gid + '"' + (open ? '' : ' inert') + '><div class="sec-body-inner"><div class="rows">' + rowsHTML + '</div></div></div></section>';
}
function toggleOverviewGroup(gid) {
  state.ovFolds[gid] = !ovGroupDisplayOpen(gid);   /* 只改折叠状态：不导航、不压返回层；搜索中显示态仍保持展开 */
  const open = ovGroupDisplayOpen(gid);
  const btn = qs('#ov-fold-btn-' + gid);
  const body = qs('#ov-body-' + gid);
  if (btn) btn.setAttribute('aria-expanded', open ? 'true' : 'false');
  if (body) {
    if (body.classList) body.classList.toggle('open', open);
    if (open) { if (body.removeAttribute) body.removeAttribute('inert'); }
    else { if (body.setAttribute) body.setAttribute('inert', ''); }
  }
}
function topicsHTML() {
  const q = state.topicQ.trim();
  const match = (t) => !q || t.name.indexOf(q) >= 0 || t.intro.some((p) => p.text.indexOf(q) >= 0);
  const rowsFor = (list) => list.filter(match).map(topicRowHTML).join('');
  const groups = DB.groups.map((g) => {
    const list = topicsOfGroup(g.id);
    const shown = list.filter(match);
    if (q && !shown.length) return '';
    return ovSecHTML(g.id, g.name, true, list.length, groupMngHTML(g.id), rowsFor(list));
  }).join('');
  const un = ungroupedTopics();
  const unShown = un.filter(match);
  const unHTML = (!q || unShown.length) && un.length ? ovSecHTML('ungrouped', '未分组', false, un.length, '', rowsFor(un)) : '';
  const total = DB.topics.length;
  const shownTotal = DB.topics.filter(match).length;
  let h = '<div class="toolbar"><div><h1>全部主题</h1><p class="lead">按主题查看企业知识。先看正文与要点，需要时再查看关联的原始资料。</p></div>';
  if (state.role === 'admin') {
    h += '<div class="actions">';
    if (state.manageGroups) h += '<button class="button secondary" type="button" data-act="group-new-open"><svg class="ic"><use href="#i-plus"/></svg>新建分组</button>';
    h += '<button class="button secondary" type="button" data-act="manage-groups-toggle">' + (state.manageGroups ? '完成分组管理' : '管理分组') + '</button><button class="button" type="button" data-act="topic-new-open"><svg class="ic"><use href="#i-plus"/></svg>新建主题</button><button class="button secondary" type="button" data-act="nav" data-view="capture"><svg class="ic"><use href="#i-upload"/></svg>添加资料</button></div>';
  }
  h += '</div>';
  h += '<div class="list-toolbar"><div class="search-wrap"><svg class="ic"><use href="#i-search"/></svg><input id="search-q" type="search" placeholder="搜索主题或正文关键词" value="' + esc(state.topicQ) + '" aria-label="搜索主题">' +
    '<button class="search-clear" id="search-clear" type="button" data-act="search-clear" aria-label="清空搜索关键词"' + (state.topicQ ? '' : ' hidden') + '><svg class="ic"><use href="#i-x"/></svg></button></div></div>';
  if (state.manageGroups && state.role === 'admin') {
    h += '<div class="info-note"><svg class="ic"><use href="#i-folder"/></svg><span>分组只用于导航；可新建分组、改名或删除；删除分组不会删除主题。</span></div>';
  }
  h += '<div id="topics-region">' + topicsRegionHTML() + '</div>';
  h += '<div class="list-count">共 ' + total + ' 个主题' + (q ? '，显示 ' + shownTotal + ' 个' : '') + '</div>';
  return h;
}
function topicsRegionHTML() {
  const q = state.topicQ.trim();
  const match = (t) => !q || t.name.indexOf(q) >= 0 || t.intro.some((p) => p.text.indexOf(q) >= 0);
  let h = '';
  DB.groups.forEach((g) => {
    const list = topicsOfGroup(g.id);
    const shown = list.filter(match);
    if (q && !shown.length) return;
    h += ovSecHTML(g.id, g.name, true, list.length, groupMngHTML(g.id), shown.map(topicRowHTML).join(''));
  });
  const un = ungroupedTopics().filter(match);
  if (un.length) h += ovSecHTML('ungrouped', '未分组', false, un.length, '', un.map(topicRowHTML).join(''));
  if (q && !DB.topics.filter(match).length) {
    h += '<div class="empty"><h2>没有找到匹配的主题</h2><p>按搜索框里的「×」清空关键词，或换个词再试。</p><button class="button secondary" type="button" data-act="search-clear">清空搜索</button></div>';
  }
  return h || '<div class="empty"><h2>还没有主题</h2><p>可以添加资料让系统建议主题，或直接新建主题。</p><button class="button" type="button" data-act="topic-new-open">新建主题</button></div>';
}
function topicRowHTML(t) {
  const mats = DB.materials.filter((m) => m.topics.includes(t.id));
  const reg = mats.filter((m) => !hasBody(m)).length;
  const ta2 = topicAvailability(t);
  let side = '<span class="pill ' + ta2.pill + '">' + esc(ta2.label) + '</span>';
  let h = '<div class="row"><button class="row-main" type="button" data-act="t-open" data-id="' + t.id + '">' +
    '<span class="row-title"><strong>' + esc(t.name) + '</strong></span>' +
    '<span class="row-meta"><span>相关资料 ' + mats.length + (reg ? '（其中仅登记 ' + reg + '）' : '') + '</span><span>更新于 ' + esc(t.updated) + '</span>' + (t.group ? '' : '<span>未分组</span>') + '</span></button>' +
    '<div class="row-side">' + side;
  if (state.role === 'admin' && state.manageGroups) {
    let opts = '<option value="">未分组</option>';
    DB.groups.forEach((g) => { opts += '<option value="' + g.id + '"' + (t.group === g.id ? ' selected' : '') + '>' + esc(g.name) + '</option>'; });
    h += '<select class="filter-select move-select" data-act="topic-move" data-id="' + t.id + '" aria-label="移动主题到分组">' + opts + '</select>';
  }
  h += '<svg class="ic row-chev"><use href="#i-chev-right"/></svg></div></div>';
  return h;
}
function refreshTopicsRegion() { const el = qs('#topics-region'); if (el) el.innerHTML = topicsRegionHTML(); const c = qs('.list-count'); if (c) c.innerHTML = '共 ' + DB.topics.length + ' 个主题'; }

/* ---------- 主题详情（阅读中心） ---------- */
function topicHTML(id) {
  const t = topicById(id);
  const isDraft = t.lifecycle === 'draft';
  const mats = DB.materials.filter((m) => m.topics.includes(id));
  const ta = topicAvailability(t);
  const back = currentBackLabel();
  const matsOpen = !!state.reopenMats; state.reopenMats = false;   /* 从来源/关联资料返回时恢复展开一次 */
  let h = '<div class="detail-top"><button class="back-link" type="button" data-act="d-back"><svg class="ic"><use href="#i-back"/></svg>' + esc(back) + '</button></div>';
  h += '<div class="detail-head"><div class="detail-title"><h1>' + esc(t.name) + '</h1>' +
    '<span class="pill ' + ta.pill + '">' + esc(ta.label) + '</span>' +
    (t.group && groupById(t.group) ? '<span class="tag type">' + esc(groupById(t.group).name) + '</span>' : '') + '</div>';
  if (state.role === 'admin') {
    h += '<div class="actions">';
    h += '<button class="button secondary" type="button" data-act="t-edit"><svg class="ic"><use href="#i-pen"/></svg>编辑正文</button>';
    if (isDraft || t.draft) h += '<button class="button" type="button" data-act="t-publish">发布主题</button>';
    h += '</div>';
  }
  h += '</div>';
  h += '<div class="detail-meta"><span>相关资料 ' + mats.length + ' 条</span><span>更新于 ' + esc(t.updated) + '</span></div>';
  const note = ta.warn || ta.reason;
  if (note) h += '<p class="hint-line' + (ta.warn ? ' warns' : '') + '">' + esc(note) + '</p>';
  if (t.draft && state.role === 'admin') {
    h += '<div class="draft-banner banner"><span><b>主题正文有未发布的修改（草稿）</b>：发布后才会替换当前版本，旧版本保留在版本记录中。</span>' +
      '<span class="actions"><button class="button secondary" type="button" data-act="t-edit">继续编辑</button><button class="button" type="button" data-act="t-publish">发布修改</button><button class="button ghost" type="button" data-act="t-discard">放弃修改</button></span></div>';
  }
  if (state.role === 'reader') h += '<div class="readonly-note"><svg class="ic"><use href="#i-alert"/></svg><span>只读视角：可以阅读、搜索与跳转。</span></div>';

  /* 介绍 */
  h += '<section class="topic-block"><div class="section-head"><h2>介绍</h2>' + (state.role === 'admin' ? '<button class="linkbtn" type="button" data-act="t-edit">编辑</button>' : '') + '</div>';
  if (t.intro.length) {
    t.intro.forEach((p) => { h += topicParaHTML(p, 'intro'); });
  } else {
    h += '<div class="empty empty-inline"><p>' + (isDraft ? '正文还没有整理。' : '正文为空。') + '</p>' + (state.role === 'admin' ? '<span class="actions">' + (mats.length ? '<button class="button secondary" type="button" data-act="t-organize">根据关联资料整理初稿（演示）</button>' : '') + '<button class="button secondary" type="button" data-act="t-edit">编辑正文</button></span>' : '') + '</div>';
  }
  h += '</section>';

  /* 要点 */
  h += '<section class="topic-block"><div class="section-head"><h2>要点</h2>' + (state.role === 'admin' ? '<button class="linkbtn" type="button" data-act="t-edit">编辑</button>' : '') + '</div>';
  if (t.points.length) {
    h += '<ul class="points-list">' + t.points.map((p) => '<li class="pt-row"><span class="pt-text">' + esc(p.text) + '</span>' + srcChipHTML(p) + '</li>').join('') + '</ul>';
  } else {
    h += '<p class="hint-line">还没有要点。' + (state.role === 'admin' ? '可在「编辑正文」中补充。' : '') + '</p>';
  }
  h += '</section>';

  /* 关联资料：默认折叠；只保留非点击资料信息与原关联编辑 / 移除 / 添加操作——
     打开原始文件统一从主题浏览树的文件列表（不在此重复文件打开按钮） */
  if (mats.length) h += '<p class="hint-line">从主题浏览列表打开原始文件。</p>';
  h += '<details class="section-fold topic-block"' + (matsOpen ? ' open' : '') + '><summary><span class="fold-title">关联资料</span><span class="fold-count">' + mats.length + ' 条</span></summary><div class="fold-body">';
  if (mats.length) {
    h += '<div class="rows">' + mats.map((m) => materialRowHTML(m, id, false)).join('') + '</div>';
  } else {
    h += '<div class="empty empty-inline"><p>还没有关联资料。</p>' + (state.role === 'admin' ? '<span class="actions"><button class="button secondary" type="button" data-act="t-add-mat">添加资料</button><button class="button secondary" type="button" data-act="t-attach-pick">关联已有资料</button></span>' : '') + '</div>';
  }
  if (state.role === 'admin') {
    h += '<div class="attach-bar"><button class="button secondary" type="button" data-act="t-add-mat"><svg class="ic"><use href="#i-upload"/></svg>添加资料到本主题</button><label class="small" for="t-attach-select">关联已有资料：</label><select id="t-attach-select" class="filter-select"><option value="">选择资料…</option>' +
      DB.materials.filter((m) => !m.topics.includes(id)).map((m) => '<option value="' + m.id + '">' + esc(m.name) + '（' + esc(FORMAT[m.format].label) + '）</option>').join('') +
      '</select><button class="button secondary" type="button" data-act="t-attach">关联</button></div>';
  }
  h += '</div></details>';

  /* 相关主题：默认折叠（按需展开） */
  const rel = t.related.map(topicById).filter(Boolean);
  h += '<details class="section-fold topic-block"><summary><span class="fold-title">相关主题</span><span class="fold-count">' + rel.length + ' 个</span></summary><div class="fold-body">';
  if (rel.length) {
    h += '<div class="chiprow">' + rel.map((rt) => '<span class="chip link-chip-wrap"><button class="chip-btn" type="button" data-act="t-rel-open" data-id="' + rt.id + '">' + esc(rt.name) + '<svg class="ic"><use href="#i-chev-right"/></svg></button>' + (state.role === 'admin' ? '<button class="chip-x" type="button" data-act="t-unrel" data-id="' + rt.id + '" aria-label="取消相关主题 ' + esc(rt.name) + '"><svg class="ic"><use href="#i-x"/></svg></button>' : '') + '</span>').join('') + '</div>';
  } else {
    h += '<p class="hint-line">没有相关的其他主题。</p>';
  }
  if (state.role === 'admin') {
    const others = DB.topics.filter((x) => x.id !== id && !t.related.includes(x.id));
    if (others.length) h += '<div class="attach-bar"><label class="small" for="t-rel-select">添加相关主题：</label><select id="t-rel-select" class="filter-select"><option value="">选择主题…</option>' + others.map((x) => '<option value="' + x.id + '">' + esc(x.name) + '</option>').join('') + '</select><button class="button secondary" type="button" data-act="t-rel">添加</button></div>';
  }
  h += '</div></details>';

  /* 关系视图（设计建议） */
  h += '<details class="opt-box"><summary><svg class="ic"><use href="#i-network"/></svg>关系视图<small>小型局部关系（设计建议）</small></summary><div class="opt-body">';
  h += relationTreeHTML(t, mats);
  h += '<p class="hint-line">只显示与本主题直接相连的关系。</p></div></details>';

  /* 版本 */
  h += '<details class="opt-box"><summary><svg class="ic"><use href="#i-history"/></svg>版本记录<small>' + t.versions.length + ' 个版本</small></summary><div class="opt-body">';
  if (t.versions.length) {
    h += '<ul class="ver-list">' + t.versions.map((v) => '<li><span class="vtag">v' + v.v + '</span><span>' + esc(v.note) + '</span><span class="muted">' + esc(v.date) + '</span></li>').join('') + '</ul>';
  } else {
    h += '<p class="hint-line">还没有发布记录。</p>';
  }
  h += '</div></details>';
  return h;
}
function topicParaHTML(p, kind) {
  const gap = p.source && !sourceEligible(p.source.mid);
  return '<div class="topic-para' + (p.modified ? ' edited' : '') + '"><p class="tp-text">' + esc(p.text) + '</p>' +
    '<div class="tp-foot">' + srcChipHTML(p) + (p.system ? (p.checked ? '<span class="tag done">系统整理 · 已确认</span>' : '<span class="tag review">系统整理 · 待核对</span>') : '') + (p.modified ? '<span class="tag edit">成员修改</span>' : '') + (gap ? '<span class="tag internal">来源为仅内部 / 未授权 · 不用于对外回答</span>' : '') + '</div></div>';
}
function srcChipHTML(p) {
  /* 出处为非交互文本（span）：保留文件名称、页码 / 段落与人工出处；打开原始文件统一走「关联资料」折叠列表，不另造入口 */
  const label = sourceLabel(p);
  return '<span class="src-chip muted-src">来源：' + esc(label || '成员人工撰写') + '</span>';
}
/* 资料行两个用法：openable !== false = 「全部资料」列表（跨主题检索，保留可点击打开）；
   openable === false = 主题「关联资料」（非点击信息 + 原关联编辑/移除操作；打开原始文件统一走主题浏览树，不重复按钮） */
function materialRowHTML(m, fromTopic, openable) {
  const a = availability(m);
  const ri = recogInfo(m);
  const tnames = m.topics.map((tid) => { const t = topicById(tid); return t ? t.name : ''; }).filter(Boolean);
  const info = '<span class="row-title"><span class="tag type">' + esc(FORMAT[m.format].label) + '</span><strong>' + esc(m.name) + '</strong>' +
    (m.updateDraft ? '<span class="tag review">有更新草稿</span>' : '') + '</span>' +
    '<span class="row-meta"><span>' + esc(ri.label) + '</span>' + (tnames.length ? '<span>主题：' + esc(tnames.join('、')) + '</span>' : '<span>未关联主题</span>') + '</span>';
  const main = openable === false
    ? '<div class="row-main row-info">' + info + '</div>'
    : '<button class="row-main" type="button" data-act="m-open" data-id="' + m.id + '">' + info + '</button>';
  return '<div class="row">' + main +
    '<div class="row-side"><span class="pill ' + a.pill + '">' + esc(a.label) + '</span>' + (state.role === 'admin' && fromTopic ? '<button class="icon-btn" type="button" data-act="t-detach" data-id="' + m.id + '" aria-label="从本主题移除关联 ' + esc(m.name) + '"><svg class="ic"><use href="#i-x"/></svg></button>' : '') + '</div></div>';
}
function relationTreeHTML(t, mats) {
  let h = '<ul class="rel-tree"><li class="rel-root">' + esc(t.name) + '</li>';
  mats.forEach((m) => {
    const others = m.topics.filter((x) => x !== t.id).map((tid) => { const tt = topicById(tid); return tt ? tt.name : ''; }).filter(Boolean);
    h += '<li class="rel-mat"><span class="rel-line">├─</span>《' + esc(m.name) + '》<span class="muted">（' + esc(FORMAT[m.format].label) + ' · ' + esc(recogInfo(m).label) + '）</span>' + (others.length ? '<span class="rel-also">也关联：' + esc(others.join('、')) + '</span>' : '') + '</li>';
  });
  if (!mats.length) h += '<li class="rel-mat muted">暂无可展示的关系</li>';
  const rel = t.related.map(topicById).filter(Boolean);
  if (rel.length) h += '<li class="rel-rel"><span class="rel-line">└─</span>相关主题：' + rel.map((x) => esc(x.name)).join('、') + '</li>';
  h += '</ul>';
  return h;
}

/* ---------- 全部资料（统一入口 + 独立筛选） ---------- */
function materialFiltered() {
  const f = state.filters;
  return DB.materials.filter((m) => {
    if (f.format && m.format !== f.format) return false;
    if (f.topic) { if (f.topic === '__none') { if (m.topics.length) return false; } else if (!m.topics.includes(f.topic)) return false; }
    if (f.status && m.lifecycle !== f.status) return false;
    if (f.q) {
      const hay = m.name + ' ' + m.paras.map((p) => p.text).join(' ') + ' ' + m.topics.map((tid) => { const t = topicById(tid); return t ? t.name : ''; }).join(' ');
      if (hay.indexOf(f.q) < 0) return false;
    }
    return true;
  });
}
function materialsHTML() {
  let h = '<div class="toolbar"><div><h1>全部资料</h1><p class="lead">统一查看全部资料；可按形式、主题与状态筛选。</p></div>';
  if (state.role === 'admin') h += '<div class="actions"><button class="button" type="button" data-act="nav" data-view="capture"><svg class="ic"><use href="#i-upload"/></svg>添加资料</button></div>';
  h += '</div>';
  h += '<div class="list-toolbar"><div class="search-wrap"><svg class="ic"><use href="#i-search"/></svg><input id="search-q" type="search" placeholder="搜索资料名称或正文" value="' + esc(state.filters.q) + '" aria-label="搜索资料">' +
    '<button class="search-clear" id="search-clear" type="button" data-act="search-clear" aria-label="清空搜索关键词"' + (state.filters.q ? '' : ' hidden') + '><svg class="ic"><use href="#i-x"/></svg></button></div>' +
    '<select class="filter-select" data-act="filter-format" aria-label="按资料形式筛选"><option value="">全部形式</option>' + FORMAT_ORDER.map((k) => '<option value="' + k + '"' + (state.filters.format === k ? ' selected' : '') + '>' + esc(FORMAT[k].label) + '</option>').join('') + '</select>' +
    '<select class="filter-select" data-act="filter-topic" aria-label="按主题筛选"><option value="">全部主题</option>' + DB.topics.map((t) => '<option value="' + t.id + '"' + (state.filters.topic === t.id ? ' selected' : '') + '>' + esc(t.name) + '</option>').join('') + '<option value="__none"' + (state.filters.topic === '__none' ? ' selected' : '') + '>未关联主题</option></select>' +
    '<select class="filter-select" data-act="filter-status" aria-label="按状态筛选"><option value="">全部状态</option><option value="draft"' + (state.filters.status === 'draft' ? ' selected' : '') + '>草稿</option><option value="published"' + (state.filters.status === 'published' ? ' selected' : '') + '>已发布</option><option value="disabled"' + (state.filters.status === 'disabled' ? ' selected' : '') + '>已停用</option></select>' +
    '</div>';
  h += '<p class="filter-note">搜索「×」只清关键词，筛选保留。</p>';
  h += '<div id="list-region">' + listRegionHTML() + '</div>';
  return h;
}
function listRegionHTML() {
  const list = materialFiltered();
  let h = '';
  if (list.length) {
    h += '<div class="rows">' + list.map((m) => materialRowHTML(m, null, true)).join('') + '</div>';
    h += '<div class="list-count">共 ' + DB.materials.length + ' 条资料，当前显示 ' + list.length + ' 条</div>';
  } else {
    const f = state.filters;
    const noFilter = !f.q && !f.format && !f.topic && !f.status;
    h += '<div class="empty"><h2>' + (noFilter ? '还没有资料' : '没有找到匹配的资料') + '</h2><p>' + (noFilter ? '添加第一份资料，或先载入演示数据看看效果。' : '按搜索框里的「×」清空关键词，或调整形式 / 主题 / 状态筛选。') + '</p>' +
      '<span class="actions"><button class="button secondary" type="button" data-act="filter-clear">清空筛选</button>' + (state.role === 'admin' ? '<button class="button" type="button" data-act="nav" data-view="capture">添加资料</button>' : '') + '</span></div>';
  }
  return h;
}
function refreshListRegion() { const el = qs('#list-region'); if (el) el.innerHTML = listRegionHTML(); }

/* ---------- 资料详情 ---------- */
function materialHTML(id) {
  const m = materialById(id);
  const a = availability(m);
  const ri = recogInfo(m);
  const back = currentBackLabel();
  let h = '<div class="detail-top"><button class="back-link" type="button" data-act="d-back"><svg class="ic"><use href="#i-back"/></svg>' + esc(back) + '</button></div>';
  h += '<div class="detail-head"><div class="detail-title"><span class="tag type">' + esc(FORMAT[m.format].label) + '</span><h1>' + esc(m.name) + '</h1><span class="pill ' + a.pill + '">' + esc(a.label) + '</span></div>';
  if (state.role === 'admin') {
    h += '<div class="actions">';
    if (id === 'm_guide') h += '<button class="button" type="button" data-act="m-update">更新资料</button>';
    h += '<details class="more-wrap"><summary class="button secondary"><svg class="ic"><use href="#i-more"/></svg>更多操作</summary><div class="more-menu">' +
      ((m.recognition === 'registered' || m.recognition === 'failed') ? '<button type="button" data-act="m-manual"><svg class="ic"><use href="#i-pen"/></svg><span><b>补充手动正文</b><small>由成员人工撰写正文，标注为「人工撰写」</small></span></button>' : '') +
      (m.recognition === 'failed' ? '<button type="button" data-act="m-retry"><svg class="ic"><use href="#i-history"/></svg><span><b>重试识别（演示）</b><small>记录重试请求；本原型不执行真实识别</small></span></button>' : '') +
      (m.lifecycle === 'disabled' ? '<button type="button" data-act="m-enable"><svg class="ic"><use href="#i-check"/></svg><span><b>重新启用</b><small>恢复为已发布；AI 可用性取决于原设置</small></span></button>' : '<button class="danger-item" type="button" data-act="m-disable"><svg class="ic"><use href="#i-alert"/></svg><span><b>停用</b><small>不再用于客户回答；历史与原件保留</small></span></button>') +
      '</div></details></div>';
  }
  h += '</div>';
  if (m.updateDraft && state.role === 'admin') {
    h += '<div class="draft-banner banner"><span><b>有更新草稿待发布</b>：v' + (m.currentVersion + 1) + ' 候选已保存，当前发布版本（v' + m.currentVersion + '）继续有效。</span>' +
      '<span class="actions"><button class="button secondary" type="button" data-act="m-update">继续编辑更新</button><button class="button" type="button" data-act="u-publish-now">发布新版</button><button class="button ghost" type="button" data-act="u-discard">放弃更新草稿</button></span></div>';
  }
  if (state.role === 'reader') h += '<div class="readonly-note"><svg class="ic"><use href="#i-alert"/></svg><span>只读视角：可以阅读资料、正文与来源。</span></div>';

  /* 状态与关系（三个维度分开：发布状态 / 识别状态 / AI 可用） */
  h += '<div class="card"><h2><svg class="ic"><use href="#i-layout"/></svg>状态与关系</h2><dl class="summary-list">' +
    '<div class="kv"><dt>发布状态</dt><dd><span class="pill ' + a.pill + '">' + esc(a.label) + '</span>' + (a.reason ? '<div class="hint-line">' + esc(a.reason) + '</div>' : '') + '</dd></div>' +
    '<div class="kv"><dt>识别状态</dt><dd><span class="tag ' + ri.cls + '">' + esc(ri.label) + '</span>' + (m.recognition === 'registered' ? '<div class="hint-line">只登记了原件：没有识别结果。</div>' : '') + (m.recognition === 'failed' ? '<div class="hint-line bad">' + esc(m.failReason) + '</div>' : '') + (m.recognition === 'manual' ? '<div class="hint-line">成员人工撰写，来源标注为「人工撰写」。</div>' : '') + '</dd></div>' +
    '<div class="kv"><dt>用途</dt><dd>' + esc(USAGE_LABEL[m.usage]) + '<div class="hint-line">用途只决定能否用于对外 AI 回答。</div></dd></div>' +
    '<div class="kv"><dt>AI 到期</dt><dd>' + esc(fmtExpiry(m.expiry)) + '<div class="hint-line">留空 = 长期有效；到期后停止用于 AI 回答。</div></dd></div>' +
    '<div class="kv"><dt>关联主题</dt><dd id="m-topics-region">' + materialTopicsHTML(m) + '</dd></div>' +
    '</dl></div>';

  /* 正文 */
  h += '<section class="topic-block"><div class="section-head"><h2>正文</h2>' + (m.paras.length ? '<span class="sec-note">' + (m.recognition === 'manual' ? '人工撰写内容' : '识别整理的正文') + '：引用时以这里的版本为准。</span>' : '') + '</div>';
  if (m.paras.length) {
    h += m.paras.map((p) => '<div class="body-para"><div class="bp-head"><span class="src-label">' + esc(p.loc) + '</span>' + (p.modified ? '<span class="tag edit">人工修改</span>' : '') + (p.checked ? '<span class="tag done">已核对</span>' : '<span class="tag review">待核对</span>') + '</div><p>' + esc(p.text) + '</p></div>').join('');
  } else {
    h += '<div class="empty empty-inline"><p>' + (m.recognition === 'failed' ? '未能识别出正文。' + esc(m.failReason) : '这份资料没有正文：仅登记了原件。') + '</p>' +
      '<span class="actions">' + (state.role === 'admin' ? '<button class="button secondary" type="button" data-act="m-manual">补充手动正文</button>' : '') + '<button class="button secondary" type="button" data-act="m-attach-focus">关联主题</button></span></div>';
  }
  h += '</section>';

  /* 可选字段 */
  h += '<details class="opt-box"><summary><svg class="ic"><use href="#i-sliders"/></svg>可选字段<small>' + m.fields.length + ' 个</small></summary><div class="opt-body">' +
    '<p class="hint-line">补充少量业务字段（文字 / 数字 + 单位），不强制使用模板；每个字段都要写出来源。</p>';
  if (m.fields.length) {
    h += '<div class="table-scroll"><table class="fields"><thead><tr><th>字段</th><th>值</th><th>来源</th>' + (state.role === 'admin' ? '<th></th>' : '') + '</tr></thead><tbody>' +
      m.fields.map((f) => '<tr><td>' + esc(f.name) + '<small class="muted">' + (f.type === 'number' ? '数字' + (f.unit ? ' · ' + esc(f.unit) : '') : '文字') + '</small></td><td>' + esc(f.value) + '</td><td>' + esc(f.source) + '</td>' +
        (state.role === 'admin' ? '<td><button class="icon-btn" type="button" data-act="m-field-del" data-id="' + f.id + '" aria-label="删除字段 ' + esc(f.name) + '"><svg class="ic"><use href="#i-x"/></svg></button></td>' : '') + '</tr>').join('') + '</tbody></table></div>';
  } else {
    h += '<p class="hint-line">还没有字段。</p>';
  }
  if (state.role === 'admin') {
    h += '<div class="field-add"><input id="mf-name" placeholder="字段名称，如 整机重量" aria-label="字段名称"><select id="mf-type" class="filter-select" aria-label="字段类型"><option value="text">文字</option><option value="number">数字 + 单位</option></select><input id="mf-unit" placeholder="单位（数字字段）" aria-label="单位"><input id="mf-value" placeholder="值" aria-label="字段值"><input id="mf-source" placeholder="来源，如 产品规格书 · 第 2 页" aria-label="字段来源"><button class="button secondary" type="button" data-act="m-field-add">添加字段</button></div>';
  }
  h += '</div></details>';

  /* 发布设置：用途 / AI / 到期唯一入口（资料级） */
  if (state.role === 'admin') {
    const aiBlocked = !hasBody(m) || m.lifecycle === 'disabled';
    h += '<details class="opt-box"><summary><svg class="ic"><use href="#i-sliders"/></svg>发布设置（用途 / AI / 到期）<small>修改后点「应用」</small></summary><div class="opt-body">' +
      '<fieldset class="settings-group"><legend>用途</legend><label class="check-inline"><input type="radio" name="ms-usage" value="internal"' + (m.usage === 'internal' ? ' checked' : '') + '>仅内部使用</label><label class="check-inline"><input type="radio" name="ms-usage" value="external"' + (m.usage === 'external' ? ' checked' : '') + '>对外使用</label></fieldset>' +
      '<fieldset class="settings-group"><legend>AI 客服</legend><div class="switch-row"><span class="switch-state">' + (aiBlocked ? '不可开启' : '允许 AI 客服使用') + '</span><label class="ios-switch"><input id="ms-ai" type="checkbox"' + (m.aiOn ? ' checked' : '') + (aiBlocked ? ' disabled' : '') + '><span class="ios-slider"></span></label></div>' +
      (aiBlocked ? '<p class="hint-line bad">' + (m.lifecycle === 'disabled' ? '已停用资料不能开启 AI 客服使用。' : '没有可核对正文：仅登记原件无法用于 AI 客服。') + '</p>' : '<p class="hint-line">对外 + AI 同时开启时，应用前会确认。</p>') + '</fieldset>' +
      '<fieldset class="settings-group"><legend>AI 到期时间（可选，北京时间）</legend><input type="datetime-local" id="ms-expiry" value="' + esc(expiryInputValue(m.expiry)) + '"><p class="hint-line">留空 = 长期有效；到期后停止用于 AI 回答。</p></fieldset>' +
      '<div class="actions"><button class="button" type="button" data-act="ms-apply">应用修改</button></div></div></details>';
  }

  /* 原件登记 */
  h += '<details class="opt-box"><summary><svg class="ic"><use href="#i-file"/></svg>原件登记<small>企业私有</small></summary><div class="opt-body">' +
    '<dl class="summary-list">' +
    '<div class="kv"><dt>' + (m.format === 'web' ? '登记地址' : m.format === 'manual' ? '来源' : '原件') + '</dt><dd class="wrap-anywhere">' + esc(m.format === 'web' ? m.url : m.format === 'manual' ? '手动创建' : (m.name + ' · ' + m.size)) + '</dd></div>' +
    '<div class="kv"><dt>更新于</dt><dd>' + esc(m.updated) + '</dd></div>' +
    '<div class="kv"><dt>可见范围</dt><dd>原件仅本企业成员可查看，不随「对外」公开。</dd></div>' +
    '</dl></div></details>';

  /* 版本 */
  h += '<details class="opt-box"><summary><svg class="ic"><use href="#i-history"/></svg>版本记录<small>' + m.versions.length + ' 个版本</small></summary><div class="opt-body">' +
    '<ul class="ver-list">' + m.versions.map((v) => '<li><span class="vtag">v' + v.v + '</span><span>' + esc(v.note) + '</span><span class="muted">' + esc(v.date) + '</span></li>').join('') + '</ul>' +
    '<p class="hint-line">旧发布版本不会被草稿覆盖；更新只影响新版本。</p></div></details>';
  return h;
}
function materialTopicsHTML(m) {
  let h = '<div class="chiprow">';
  if (m.topics.length) {
    h += m.topics.map((tid) => { const t = topicById(tid); return t ? '<span class="chip link-chip-wrap"><button class="chip-btn" type="button" data-act="t-open-from-mat" data-id="' + t.id + '">' + esc(t.name) + '</button>' + (state.role === 'admin' ? '<button class="chip-x" type="button" data-act="m-untopic" data-id="' + t.id + '" aria-label="取消关联 ' + esc(t.name) + '"><svg class="ic"><use href="#i-x"/></svg></button>' : '') + '</span>' : ''; }).join('');
  } else {
    h += '<span class="muted">未关联主题（仍可在全部资料中通过搜索找到）</span>';
  }
  h += '</div>';
  if (state.role === 'admin') {
    const others = DB.topics.filter((t) => !m.topics.includes(t.id));
    if (others.length) h += '<div class="attach-bar"><label class="small" for="m-attach-select">关联到主题：</label><select id="m-attach-select" class="filter-select"><option value="">选择主题…</option>' + others.map((t) => '<option value="' + t.id + '">' + esc(t.name) + '</option>').join('') + '</select><button class="button secondary" type="button" data-act="m-addtopic">关联</button></div>';
  }
  return h;
}
function refreshMTopics() { const el = qs('#m-topics-region'); if (el) el.innerHTML = materialTopicsHTML(materialById(state.route.id)); }

/* ---------- 添加资料（三步：添加资料 → 检查整理结果 → 发布设置） ---------- */
function sampleKeyFor(name, format) {
  if (format === 'pdf' && /\.pdf$/i.test(name)) return 'pdf';
  if (format === 'docx' && /\.docx$/i.test(name)) return 'docx';
  return null;
}
const SAMPLE_PARAS = {
  pdf: [
    { text: '智能会议设备A是一体化视频会议终端，面向 8–16 人的中小型会议室。', loc: '第 1 页' },
    { text: '适用会议室 8–16 人；支持同时接入 2 路无线投屏。', loc: '第 2 页' },
    { text: '随机配件：遥控器、电源适配器、壁挂支架（含安装螺丝包）。', loc: '第 3 页' }
  ],
  docx: [
    { text: '安装前：确认墙面或桌面承重，核对配件清单。', loc: '段落 1' },
    { text: '壁挂安装：使用随机挂架，按孔位固定后锁紧安全扣。', loc: '段落 2' },
    { text: '安装验收：拍照记录走线并登记序列号。', loc: '段落 3' }
  ]
};
const MIX_BATCH = [
  { name: '会议设备A-产品规格书.pdf', format: 'pdf', size: '1.1 MB', src: 'file' },
  { name: '会议设备A-安装指南.docx', format: 'docx', size: '320 KB', src: 'file' },
  { name: '设备A-安装现场照片.jpg', format: 'image', size: '2.4 MB', src: 'file' },
  { name: '设备A-现场演示录像.mp4', format: 'video', size: '18 MB', src: 'file' },
  { name: 'https://example.com/meeting-device-a', format: 'web', size: '—', src: 'url' }
];
function newWorkItem(o) {
  const it = {
    id: uid('wi'), name: o.name, format: o.format, size: o.size || '—', url: o.url || '', src: o.src,
    recognition: 'pending', sampleKey: o.sampleKey || null, failReason: '', registeredNote: '',
    paras: [], fields: [], sugs: []
  };
  if (o.format === 'manual') { it.recognition = 'manual'; }   /* 手动文字是成员录入，不是识别结果：不得描绘成识别失败/仅登记 */
  else if (o.src === 'url') { it.recognition = 'registered'; it.registeredNote = '已登记网页地址（仅登记链接，不抓取网页内容）。'; }
  else if (o.format === 'image' || o.format === 'video') { it.recognition = 'registered'; it.registeredNote = '仅登记原件：本版不生成正文。'; }
  else if (!it.sampleKey) { it.recognition = 'failed'; it.failReason = '该格式暂不支持识别：仅登记原件，可人工整理正文。'; }
  return it;
}
function startCapture(prefillTopicId) {
  /* inputMode = 第 1 步当前显示的输入方式（file / url / text）：每次只显示一个输入卡片，默认文件；
     存在工作副本上 → 返回第 1 步保持本次选择，只有新添加流程才重置为文件 */
  state.work = { step: 1, items: [], usage: 'internal', aiOn: false, expiry: '', assocConfirmed: false, prefillTopic: prefillTopicId || '', committed: false, inputMode: 'file' };
  state.workDirty = false;
  state.backStack = [];
  state.route = { name: 'capture', id: null, step: 1 };
  render();
}
function work() { return state.work; }
function markWorkDirty() { state.workDirty = true; }
function step1Ready() { const w = work(); return !!(w && w.items.length); }
function addWorkItem(o) { const w = work(); if (!w) return; const it = newWorkItem(o); w.items.push(it); return it; }
function buildSuggestions() {
  const w = work(); if (!w) return;
  const existingGuide = { tid: 't_meeting', name: '智能会议设备A' };
  w.items.forEach((it) => {
    if (it.sugs.length) return;
    if (it.sampleKey || it.format === 'web') {
      it.sugs.push({ id: uid('sg'), kind: 'existing', tid: 't_meeting', name: existingGuide.name, active: true });
    }
    if (it.format === 'docx') it.sugs.push({ id: uid('sg'), kind: 'existing', tid: 't_onboarding', name: '入职培训', active: true });
    if (it.format === 'image') it.sugs.push({ id: uid('sg'), kind: 'new', tid: null, name: '设备外观与成套配件', active: true });
    if (it.format === 'video') it.sugs.push({ id: uid('sg'), kind: 'new', tid: null, name: '设备A安装与配置', active: true });
  });
  if (w.prefillTopic) {
    const t = topicById(w.prefillTopic);
    if (t) w.items.forEach((it) => {
      if (!it.sugs.some((s) => s.tid === t.id)) it.sugs.unshift({ id: uid('sg'), kind: 'existing', tid: t.id, name: t.name, active: true });
    });
  }
}
function captureHTML() {
  const w = work();
  if (!w) { startCapture(); return captureHTML(); }
  let h = '<div class="steps"><span class="step' + (w.step === 1 ? ' on' : '') + '"><span class="n">1</span>添加资料</span><span class="sep"></span><span class="step' + (w.step === 2 ? ' on' : '') + '"><span class="n">2</span>检查整理结果</span><span class="sep"></span><span class="step' + (w.step === 3 ? ' on' : '') + '"><span class="n">3</span>发布设置</span></div>';
  if (w.step === 1) h += captureStep1HTML();
  else if (w.step === 2) h += captureStep2HTML();
  else h += captureStep3HTML();
  return h;
}
/* 第 1 步的输入方式：file / url / text。每次只显示一个输入卡片（其余带 hidden），
   默认文件；三个切换按钮在卡片下方（aria-pressed 表达当前方式）。 */
function inputMode() { const w = work(); return (w && w.inputMode) || 'file'; }
function inputModeCardHTML(mode, cur, body) {
  return '<div class="drop mode-card" data-mode-card="' + mode + '"' + (mode === cur ? '' : ' hidden') + '>' + body + '</div>';
}
function inputSwitchHTML(cur) {
  const btn = (m, label) => '<button class="switch-btn' + (m === cur ? ' on' : '') + '" type="button" data-act="c-input-mode" data-mode="' + m + '" aria-pressed="' + (m === cur ? 'true' : 'false') + '">' + label + '</button>';
  return '<div class="input-switch" role="group" aria-label="资料输入方式">' + btn('file', '文件') + btn('url', '网页地址') + btn('text', '手动文字') + '</div>';
}
function captureStep1HTML() {
  const w = work();
  let h = '<div class="toolbar"><div><h1>第 1 步：添加资料</h1><p class="step-sub">把资料加进来：文件、网页地址或手动文字都可以。</p></div></div>';
  if (w.prefillTopic) { const t = topicById(w.prefillTopic); if (t) h += '<div class="info-note"><svg class="ic"><use href="#i-book"/></svg><span>本批会优先建议关联到「' + esc(t.name) + '」，确认后在检查步骤仍可调整。</span></div>'; }
  /* 每次只显示一个资料输入卡片；切换区（三个按钮）在卡片下方 */
  const cur = inputMode();
  h += '<div class="card"><div class="input-modes">' +
    inputModeCardHTML('file', cur,
      '<svg class="ic big"><use href="#i-upload"/></svg><strong>选择文件（可多选）</strong><p>PDF、DOCX、图片、视频等都可以；只登记名称与大小，不上传。</p>' +
      '<input type="file" id="file-input" class="sr-input" multiple aria-label="选择文件">' +
      '<div class="actions"><button class="button" type="button" data-act="pick-file">选择文件</button></div>') +
    inputModeCardHTML('url', cur,
      '<svg class="ic big"><use href="#i-globe"/></svg><strong>登记网页地址</strong><p>登记在线文章或产品页的链接。</p>' +
      '<div class="inline-row"><input id="c-url" placeholder="https://…" aria-label="网页地址"><button class="button secondary" type="button" data-act="c-add-url">登记地址</button></div>') +
    inputModeCardHTML('text', cur,
      '<svg class="ic big"><use href="#i-pen"/></svg><strong>手动文字</strong><p>直接写正文；来源标注为「人工撰写」。</p>' +
      '<div class="actions"><button class="button secondary" type="button" data-act="c-add-text">添加手动文字</button></div>') +
    '</div>' + inputSwitchHTML(cur) + '</div>';
  h += '<details class="scenario-box"><summary>载入混合示例<small>不用准备文件也能走通</small></summary><p class="sc-note">载入一组示例资料（PDF、DOCX、图片、视频、网页）；这只是演示数据，不代表真实识别。</p><div class="actions"><button class="button secondary" type="button" data-act="c-load-sample">载入混合示例（演示）</button></div></details>';
  h += '<div class="card"><h2>本批资料<small class="muted"> ' + w.items.length + ' 件</small></h2>';
  if (w.items.length) {
    h += w.items.map((it) => captureItemStep1HTML(it)).join('');
  } else {
    h += '<p class="hint-line">还没有添加资料。可以先「选择文件」「登记网页地址」「添加手动文字」，或载入混合示例。</p>';
  }
  h += '</div>';
  const ready = step1Ready();
  h += '<div class="review-footer"><span></span><div class="actions"><button class="button" type="button" data-act="c-next"' + (ready ? '' : ' disabled') + '>下一步：检查整理结果</button></div></div>';
  if (!ready) h += '<p class="hint-line" style="text-align:right">「下一步」暂不可用：请先添加至少一件资料。</p>';
  return h;
}
function captureItemStep1HTML(it) {
  const w = work();
  let h = '<div class="item-card"><div class="item-head"><span class="tag type">' + esc(FORMAT[it.format].label) + '</span>';
  if (it.format === 'manual') {
    h += '<input class="item-name-input" id="c-name-' + it.id + '" value="' + esc(it.name) + '" placeholder="手动材料名称（必填）" aria-label="手动材料名称" data-act="c-item-name" data-id="' + it.id + '">';
  } else {
    h += '<b class="item-name wrap-anywhere">' + esc(it.name) + '</b>';
  }
  h += '<span class="grow"></span><span class="muted small">' + esc(it.src === 'url' ? '网页地址' : it.src === 'text' ? '手动文字' : it.size) + '</span>';
  if (!w.assocConfirmed) h += '<button class="icon-btn" type="button" data-act="c-remove-item" data-id="' + it.id + '" aria-label="移除 ' + esc(it.name || '未命名资料') + '"><svg class="ic"><use href="#i-x"/></svg></button>';
  else h += '<span class="muted small">已确认关联后不可移除</span>';
  h += '</div>';
  if (it.format === 'manual') h += '<textarea data-act="c-item-text" data-id="' + it.id + '" rows="3" placeholder="手动正文…">' + esc(it.paras.length ? it.paras[0].text : '') + '</textarea>';
  return h + '</div>';
}
function captureStep2HTML() {
  const w = work();
  buildSuggestions();
  const bodyN = w.items.filter((it) => it.paras.length).length;
  const regN = w.items.length - bodyN;
  const pending = w.items.filter((it) => it.recognition === 'pending');
  let h = '<div class="toolbar"><div><h1>第 2 步：检查整理结果</h1><p class="step-sub">检查内容，确认它们属于哪些主题。逐段核对正文；系统建议的主题可以改名、合并或拒绝。</p></div></div>';
  h += '<div class="info-note"><svg class="ic"><use href="#i-check"/></svg><span>本批 ' + w.items.length + ' 件：' + bodyN + ' 件可核对正文，' + regN + ' 件仅登记原件。</span></div>';
  if (pending.length) {
    h += '<div class="scenario-inline"><span class="scenario-tag">演示控件</span><p class="sc-note">还有 ' + pending.length + ' 件文字型资料没有整理结果；用下面的按钮填入<b>示例</b>整理结果（图片 / 视频 / 网页不会被识别）。</p>' +
      '<button class="button" type="button" data-act="c-use-samples" id="use-samples-btn">使用示例整理结果（演示）</button></div>';
  }
  h += '<div class="batch-bar"><label class="small" for="c-common-topic">整批设置共同主题</label><select id="c-common-topic" class="filter-select"><option value="">选择主题…</option>' + DB.topics.map((t) => '<option value="' + t.id + '">' + esc(t.name) + '</option>').join('') + '</select><button class="button secondary" type="button" data-act="c-apply-common">应用到整批</button><p class="hint-line">只添加这一个主题；每件资料仍可单独调整。</p></div>';
  h += w.items.map((it) => captureItemStep2HTML(it)).join('');
  if (!w.assocConfirmed) {
    h += '<div class="card confirm-assoc"><h2>确认主题与关联</h2><p class="hint-line">确认后建议才会生效；未确认的建议不会出现在主题导航里。</p><div class="actions"><button class="button" type="button" data-act="c-confirm-assoc">确认主题与关联</button></div></div>';
  } else {
    h += '<div class="notice"><svg class="ic"><use href="#i-check"/></svg><span>主题与关联已确认：新主题已出现在左侧主题导航（草稿）。</span></div>';
  }
  h += '<div class="review-footer"><button class="button secondary" type="button" data-act="c-back">返回上一步</button><div class="actions"><button class="button" type="button" data-act="c-next2">下一步：发布设置</button></div></div>';
  return h;
}
function captureItemStep2HTML(it) {
  const w = work();
  const st = itemStatus(it);
  let h = '<article class="item-card" id="item-' + it.id + '"><div class="item-head"><span class="tag type">' + esc(FORMAT[it.format].label) + '</span><b class="item-name wrap-anywhere">' + esc(it.name || '（未命名）') + '</b><span class="tag ' + st.cls + '" id="it-status-' + it.id + '">' + esc(st.label) + '</span>' +
    (w.assocConfirmed ? '' : (it.recognition === 'pending' ? '<button class="linkbtn" type="button" data-act="c-remove-item" data-id="' + it.id + '">移除</button>' : '')) + '</div>';
  if (it.paras.length) {
    const remain = it.paras.filter((p) => !p.checked).length;
    h += '<button class="fold-head' + (it.foldBody ? ' open' : '') + '" type="button" id="it-body-btn-' + it.id + '" data-act="c-fold-body" data-id="' + it.id + '" aria-expanded="' + (it.foldBody ? 'true' : 'false') + '"><span class="fold-caret" aria-hidden="true"></span>检查正文<small id="it-remain-' + it.id + '">' + (remain ? '剩余 ' + remain + ' 段待核对' : '已核对') + '</small></button>';
    h += '<div class="fold-region" id="it-body-' + it.id + '"' + (it.foldBody ? '' : ' hidden') + '>';
    h += it.paras.map((p, i) => paraCardHTML(p, i, it)).join('');
    h += '<div class="actions"><button class="linkbtn" type="button" data-act="c-add-para" data-id="' + it.id + '">＋ 添加段落</button></div>';
    h += '<details class="opt-details"><summary>核对出处与修改记录（' + it.paras.length + ' 段）</summary><ul class="src-list" id="it-src-' + it.id + '">' + srcListHTML(it) + '</ul><p class="hint-line">出处指该资料自身的位置。</p></details>';
    h += '</div>';
  } else if (it.recognition === 'manual') {
    h += '<p class="hint-line">还没有填写正文：可返回上一步补充（发布前需要正文）。</p>';
  } else {
    h += '<p class="hint-line">' + esc(it.registeredNote || (it.recognition === 'failed' ? it.failReason : '暂时没有正文。')) + ' ' + (it.recognition === 'pending' ? '可在上方用示例整理结果补充正文。' : '仅登记原件，不产生正文。') + '</p>';
  }
  h += '<button class="fold-head' + (it.foldTopic ? ' open' : '') + '" type="button" id="it-topic-btn-' + it.id + '" data-act="c-fold-topic" data-id="' + it.id + '" aria-expanded="' + (it.foldTopic ? 'true' : 'false') + '"><span class="fold-caret" aria-hidden="true"></span>调整主题<small>' + esc(topicFoldSummary(it)) + '</small></button>';
  h += '<div class="fold-region" id="it-topic-' + it.id + '"' + (it.foldTopic ? '' : ' hidden') + '>';
  h += it.sugs.map((s) => suggRowHTML(s, it)).join('');
  if (!it.sugs.length) h += '<p class="hint-line">没有建议。资料可以暂不关联主题（只在全部资料中可见），也可以随时手动关联。</p>';
  if (!w.assocConfirmed) {
    const others = existingTopicsForSelect(it.sugs.filter((s) => s.active && s.kind === 'existing').map((s) => s.tid));
    h += '<div class="sugg-add"><label class="small" for="c-addtopic-' + it.id + '">追加关联到已有主题：</label><select id="c-addtopic-' + it.id + '" class="filter-select"><option value="">选择主题…</option>' + others.map((t) => '<option value="' + t.id + '">' + esc(t.name) + '</option>').join('') + '</select><button class="button secondary" type="button" data-act="c-addtopic" data-id="' + it.id + '">添加</button></div>';
  }
  h += '</div>';
  h += '<details class="opt-details"><summary>可选字段（补充信息）' + (it.fields.length ? ' · ' + it.fields.length + ' 个' : ' · 选填') + '</summary>' +
    '<p class="hint-line">需要复用的少量信息可以整理成字段（文字 / 数字 + 单位）；每个字段都要写出来源。</p>';
  if (it.fields.length) {
    h += '<ul class="field-list">' + it.fields.map((f, idx) => '<li><span class="tag type">' + (f.type === 'number' ? '数字' : '文字') + '</span><b>' + esc(f.name) + '</b><span>' + esc(f.value) + (f.unit ? ' ' + esc(f.unit) : '') + '</span><small>来源：' + esc(f.source) + '</small><button class="icon-btn" type="button" data-act="c-field-del" data-id="' + it.id + '" data-idx="' + idx + '" aria-label="删除字段"><svg class="ic"><use href="#i-x"/></svg></button></li>').join('') + '</ul>';
  }
  h += '<div class="field-add"><input id="cf-name-' + it.id + '" placeholder="字段名称" aria-label="字段名称"><select id="cf-type-' + it.id + '" class="filter-select" aria-label="字段类型"><option value="text">文字</option><option value="number">数字 + 单位</option></select><input id="cf-unit-' + it.id + '" placeholder="单位" aria-label="单位"><input id="cf-value-' + it.id + '" placeholder="值" aria-label="字段值"><input id="cf-source-' + it.id + '" placeholder="来源" aria-label="字段来源"><button class="button secondary" type="button" data-act="c-field-add" data-id="' + it.id + '">添加字段</button></div></details>';
  h += '</article>';
  return h;
}
function paraCardHTML(p, i, it) {
  const locs = [];
  it.paras.forEach((x) => { if (x.source && locs.indexOf(x.source) < 0) locs.push(x.source); });
  return '<div class="para-card"><div class="para-head"><span class="para-no">段落 ' + (i + 1) + '</span>' +
    (p.modified ? '<span class="tag edit">人工修改</span>' : '') +
    (p.source ? '<span class="src-label mono">来源 · ' + esc(p.source) + '</span>' : '<span class="tag review">缺少来源</span><select class="filter-select src-select" data-act="c-para-source" data-id="' + p.id + '" aria-label="段落来源"><option value="">选择来源…</option><option value="人工撰写">人工撰写</option>' + locs.map((l) => '<option value="' + esc(l) + '">' + esc(l) + '</option>').join('') + '</select>') +
    '<label class="check-inline" style="margin:0 0 0 auto"><input type="checkbox" data-act="c-para-check" data-id="' + p.id + '"' + (p.checked ? ' checked' : '') + '>已核对</label></div>' +
    '<textarea data-act="c-para-text" data-id="' + p.id + '" rows="2" aria-label="段落 ' + (i + 1) + ' 正文">' + esc(p.text) + '</textarea></div>';
}
function itemStatus(it) {
  if (it.recognition === 'pending') return { cls: 'gone', label: '待整理（未识别）' };
  if (it.recognition === 'registered') return { cls: 'gone', label: '仅登记原件' };
  if (it.recognition === 'failed') return { cls: 'gone', label: '未能识别' };
  const un = it.paras.filter((p) => !p.checked).length;
  if (it.recognition === 'manual') return un ? { cls: 'review', label: '人工正文 · 待核对 ' + un + ' 段' } : { cls: 'done', label: '人工正文' };
  if (!it.paras.length) return { cls: 'gone', label: '仅登记原件' };
  return un ? { cls: 'review', label: '待核对 ' + un + ' 段' } : { cls: 'done', label: '已核对' };
}
/* 出处核对列表（可整块重绘，不重建其余输入控件，避免丢焦点） */
function srcListHTML(it) {
  return it.paras.map((p, i) => '<li>段落 ' + (i + 1) + ' · 来源：' + esc(p.source || '（未选择）') + ' · ' + (p.checked ? '已核对' : '待核对') + (p.modified ? ' · 人工修改' : '') + '</li>').join('');
}
function itemIdOfPara(pid) {
  const w = work(); if (!w) return null;
  const it = w.items.find((x) => x.paras.some((p) => p.id === pid));
  return it ? it.id : null;
}
/* 核对状态实时更新：只改资料头的状态标签、折叠标题的剩余数量与出处列表，不整页重渲染 */
function refreshItemStatus(it) {
  if (!it) return;
  const st = itemStatus(it);
  const tag = qs('#it-status-' + it.id);
  if (tag) { if (tag.setAttribute) tag.setAttribute('class', 'tag ' + st.cls); tag.textContent = st.label; }
  const remain = it.paras.filter((p) => !p.checked).length;
  const rem = qs('#it-remain-' + it.id);
  if (rem) rem.textContent = remain ? '剩余 ' + remain + ' 段待核对' : '已核对';
  const src = qs('#it-src-' + it.id);
  if (src) src.innerHTML = srcListHTML(it);
}
/* 步骤 2 的折叠：检查正文（首件有正文资料默认展开）/ 调整主题，展开状态保存在工作副本上 */
function syncItemFold(it, kind) {
  if (!it) return;
  const open = kind === 'body' ? !!it.foldBody : !!it.foldTopic;
  const btn = qs('#it-' + kind + '-btn-' + it.id);
  const reg = qs('#it-' + kind + '-' + it.id);
  if (btn) { if (btn.classList) btn.classList.toggle('open', open); if (btn.setAttribute) btn.setAttribute('aria-expanded', open ? 'true' : 'false'); }
  if (reg) reg.hidden = !open;
}
function initStep2Folds() {
  const w = work(); if (!w) return;
  let opened = false;
  w.items.forEach((it) => {
    it.foldBody = false; it.foldTopic = false;
    if (!opened && it.paras.length) { it.foldBody = true; opened = true; }
  });
}
function topicFoldSummary(it) {
  const w = work();
  const names = it.sugs.filter((s) => s.active).map((s) => s.kind === 'new' ? s.name : (topicById(s.tid) ? topicById(s.tid).name : s.name));
  if (!names.length) return '暂无建议';
  return (w && w.assocConfirmed ? '已确认：' : '建议：') + names.join('、');
}
/* 打开第一处含指定问题的资料折叠（供「去处理」定位用） */
function openFirstFold(kind) {
  const w = work(); if (!w) return;
  for (const it of w.items) {
    if (!it.paras.length) continue;
    const bad = it.paras.some((p) => kind === 'para' ? !p.checked : (String(p.text).trim() !== '' && !p.source));
    if (bad) { it.foldBody = true; return; }
  }
}
function suggRowHTML(s, it) {
  const w = work();
  if (s.active) {
    const chip = s.kind === 'new' ? '<span class="sugg-chip new"><svg class="ic"><use href="#i-plus"/></svg>' + esc(s.name) + '<small>建议新建</small></span>' : '<span class="sugg-chip"><svg class="ic"><use href="#i-check"/></svg>' + esc(s.name) + '<small>已有主题</small></span>';
    let acts = '';
    if (!w.assocConfirmed) {
      if (s.kind === 'new') acts += '<button class="linkbtn" type="button" data-act="sg-rename" data-id="' + s.id + '">改名</button><button class="linkbtn" type="button" data-act="sg-merge" data-id="' + s.id + '">合并到已有主题…</button>';
      acts += '<button class="linkbtn danger-link" type="button" data-act="sg-reject" data-id="' + s.id + '">不要</button>';
    }
    return '<div class="sugg-row">' + chip + '<span class="grow"></span>' + acts + '</div>';
  }
  return '<div class="sugg-row rejected"><span class="tag gone">已拒绝：' + esc(s.name) + '</span><span class="grow"></span>' + (w.assocConfirmed ? '' : '<button class="linkbtn" type="button" data-act="sg-undo" data-id="' + s.id + '">撤销</button>') + '</div>';
}
function captureStep3HTML() {
  const w = work();
  const gates = captureGates(w);
  const bodyN = w.items.filter((it) => it.paras.length).length;
  const regN = w.items.length - bodyN;
  const withTopic = w.items.filter((it) => it.sugs.some((s) => s.active && (s.kind === 'existing' || s.kind === 'new'))).length;
  let h = '<div class="toolbar"><div><h1>第 3 步：发布设置</h1><p class="step-sub">设置用途、AI 与到期时间；对外又开启 AI 时发布前会确认。</p></div></div>';
  h += '<div class="card"><h2><svg class="ic"><use href="#i-layout"/></svg>本批摘要</h2><dl class="summary-list">' +
    '<div class="kv"><dt>资料</dt><dd>' + w.items.length + ' 件（' + bodyN + ' 件可核对正文 · ' + regN + ' 件仅登记原件）</dd></div>' +
    '<div class="kv"><dt>主题</dt><dd>' + (w.assocConfirmed ? '已确认关联 ' + withTopic + ' 件' + newTopicNames(w) : '还有 ' + (w.items.length - withTopic) + ' 件未确认主题建议（不确认不会建立新主题，也不影响发布）') + '</dd></div>' +
    '<div class="kv"><dt>未关联主题</dt><dd>' + (w.items.length - withTopic) + ' 件（发布后只在全部资料可见）。</dd></div>' +
    '</dl>';
  if (gates.length) {
    h += '<h3 class="sub-head">发布前还需要完成</h3><div class="gates">' + gates.map((g) => '<div class="gate gate-bad"><svg class="ic"><use href="#i-alert"/></svg><span>' + esc(g.label) + ' <button class="linkbtn" type="button" data-act="c-gate-go" data-go="' + g.go + '">去处理</button></span></div>').join('') + '</div>';
  } else {
    h += '<div class="gate gate-ok"><svg class="ic"><use href="#i-check"/></svg><span>核对项都已通过，可以发布。</span></div>';
  }
  h += '</div>';
  h += '<div class="card"><fieldset class="settings-group"><legend>用途</legend><label class="check-inline"><input type="radio" name="w-usage" value="internal" data-act="w-usage"' + (w.usage === 'internal' ? ' checked' : '') + '>仅内部使用</label><label class="check-inline"><input type="radio" name="w-usage" value="external" data-act="w-usage"' + (w.usage === 'external' ? ' checked' : '') + '>对外使用</label><p class="hint-line">新内容默认对内。</p></fieldset>' +
  '<fieldset class="settings-group"><legend>AI 客服</legend><div class="switch-row"><span class="switch-state">' + (w.aiOn ? '允许 AI 客服使用' : '暂不用于 AI 客服') + '</span><label class="ios-switch"><input id="w-ai" type="checkbox" data-act="w-ai"' + (w.aiOn ? ' checked' : '') + '><span class="ios-slider"></span></label></div><p class="hint-line">默认关闭；仅登记原件的资料即使开启也不会用于回答。</p></fieldset>' +
  '<fieldset class="settings-group"><legend>AI 到期时间（可选，北京时间）</legend><input type="datetime-local" id="w-expiry" data-act="w-expiry" value="' + esc(w.expiry) + '"><p class="hint-line">留空 = 长期有效；到期后停止用于 AI 回答。</p></fieldset></div>';
  h += '<div class="review-footer"><button class="button secondary" type="button" data-act="c-to-step2">返回上一步</button><div class="actions"><button class="button secondary" type="button" data-act="c-save-draft">保存草稿</button><button class="button" type="button" data-act="c-publish">发布</button></div></div>';
  h += '<p class="hint-line" style="text-align:right">保存草稿允许不完整。</p>';
  return h;
}
function newTopicNames(w) {
  const names = [];
  w.items.forEach((it) => it.sugs.forEach((s) => { if (s.active && s.kind === 'new' && names.indexOf(s.name) < 0) names.push(s.name); }));
  return names.length ? '（将新建：' + esc(names.join('、')) + '）' : '';
}
function captureGates(w) {
  const gates = [];
  const paras = [];
  w.items.forEach((it) => it.paras.forEach((p) => paras.push(p)));
  const unchecked = paras.filter((p) => !p.checked).length;
  if (unchecked) gates.push({ key: 'unchecked', label: '还有 ' + unchecked + ' 段正文未核对', go: 'paras' });
  const nosrc = paras.filter((p) => String(p.text).trim() !== '' && !p.source).length;
  if (nosrc) gates.push({ key: 'nosrc', label: '有 ' + nosrc + ' 段正文缺少来源标注', go: 'src' });
  const emptyText = paras.filter((p) => String(p.text).trim() === '').length;
  if (emptyText) gates.push({ key: 'empty', label: '有 ' + emptyText + ' 段正文为空（请补写或删除）', go: 'paras' });
  const pending = w.items.filter((it) => it.recognition === 'pending').length;
  if (pending) gates.push({ key: 'pending', label: '有 ' + pending + ' 件文字型资料还没有核对整理结果', go: 'pending' });
  const manualNoName = w.items.filter((it) => it.format === 'manual' && !String(it.name || '').trim()).length;
  if (manualNoName) gates.push({ key: 'name', label: '有手动材料还没有名称', go: 'items' });
  const manualNoBody = w.items.filter((it) => it.format === 'manual' && !it.paras.some((p) => String(p.text).trim() !== '')).length;
  if (manualNoBody) gates.push({ key: 'nobody', label: '有手动材料没有正文', go: 'items' });
  return gates;
}
/* ---------- 更新资料（版本演示） ---------- */
const UPDATE_SAMPLE = {
  file: '会议设备A-安装指南-2026修订.docx',
  paras: [
    { old: 'gp1', text: '安装前：确认墙面或桌面承重，核对配件清单。', loc: '段落 1', status: 'same' },
    { old: 'gp2', text: '壁挂安装：使用随机挂架；混凝土墙用膨胀螺栓，轻质墙需先加装衬板。', loc: '段落 2', status: 'changed' },
    { old: 'gp3', text: '首次通电：指示灯常亮后按遥控器任意键，等待系统自检完成。', loc: '段落 3', status: 'kept-manual', note: '保留成员人工修改；新资料对应段落措辞为「首次通电：指示灯常亮后按任意按键唤醒，等待自检完成」。' },
    { old: 'gp4', text: '培训要点：新同事需在指导下完成 2 次实际安装后独立操作。', loc: '段落 4', status: 'same' },
    { old: null, text: '安装验收：拍照记录走线并登记序列号。', loc: '段落 5', status: 'added' }
  ]
};
function startUpdate(mid) {
  const m = materialById(mid);
  if (!m) return;
  state.update = m.updateDraft ? clone(m.updateDraft) : null;
  state.updateDirty = false;
  state.route = { name: 'update', id: mid, step: 1 };
  render();
}
function loadUpdateSample() {
  const m = materialById(state.route.id);
  if (!m) return;
  state.update = { name: UPDATE_SAMPLE.file, loaded: true, paras: clone(UPDATE_SAMPLE.paras) };
  state.updateDirty = true;
  render();
}
function updateHTML() {
  const m = materialById(state.route.id);
  if (!m) return missingHTML('资料');
  const u = state.update;
  let h = '<div class="detail-top"><button class="back-link" type="button" data-act="u-back"><svg class="ic"><use href="#i-back"/></svg>返回资料</button></div>';
  h += '<div class="toolbar"><div><h1>更新资料：' + esc(m.name) + '</h1><p class="step-sub">用一份新版本替换文件：先对照，再保存草稿，确认发布后才会替换当前版本（v' + m.currentVersion + '）。</p></div></div>';
  h += '<div class="info-note"><svg class="ic"><use href="#i-alert"/></svg><span>更新对照规则为设计建议（待评审）：人工修改优先保留、差异只做文本对照，不自动判断新旧哪个“正确”。</span></div>';
  if (!u) {
    h += '<div class="card"><h2>第 1 步：选择新版本</h2><p class="hint-line">本原型只为《会议设备A-安装指南.docx》准备了一条更新演示样例；其他资料的更新流程在设计评审后随实现提供。</p>' +
      '<div class="actions"><button class="button" type="button" data-act="u-load-sample">载入更新示例（演示）</button><input type="file" id="u-file" class="sr-input" aria-label="选择新版本文件"><button class="button secondary" type="button" data-act="u-pick-file">选择文件</button></div></div>';
    return h;
  }
  h += '<div class="card"><h2>对照：旧发布版本 vs 新整理初稿</h2><p class="hint-line">新文件：' + esc(u.name) + '。以下对照为示例文本（不渲染真实文件）；「保留人工修改」的段落不会静默覆盖。</p>';
  h += '<div class="compare-wrap">' + u.paras.map((p, i) => {
    const oldM = m.paras.find((x) => x.id === p.old);
    const tag = p.status === 'changed' ? '<span class="tag review">文字有变化</span>' : p.status === 'added' ? '<span class="tag review">新增段落</span>' : p.status === 'kept-manual' ? '<span class="tag edit">保留人工修改</span>' : '<span class="tag done">无变化</span>';
    let row = '<div class="compare-row"><div class="cmp-cell"><span class="cmp-cap">旧发布 v' + m.currentVersion + '</span>' + (oldM ? esc(oldM.text) : '<span class="muted">（无对应段落）</span>') + '</div>';
    row += '<div class="cmp-cell"><span class="cmp-cap">新整理初稿' + (p.status === 'changed' || p.status === 'added' ? ' · <label class="check-inline" style="display:inline-flex;margin:0"><input type="checkbox" data-act="u-para-check" data-idx="' + i + '"' + (p.checked ? ' checked' : '') + '>已核对</label>' : '') + '</span>';
    if (p.status === 'same' || p.status === 'kept-manual') {
      row += esc(p.text) + (p.note ? '<div class="hint-line">' + esc(p.note) + '</div>' : '');
    } else {
      row += '<textarea data-act="u-para-text" data-idx="' + i + '" rows="2" aria-label="新版本段落 ' + (i + 1) + '">' + esc(p.text) + '</textarea>';
    }
    row += '</div><div class="cmp-mid">' + tag + ' · ' + esc(p.loc) + '</div></div>';
    return row;
  }).join('') + '</div>';
  h += '</div>';
  h += '<div class="review-footer"><button class="button secondary" type="button" data-act="u-cancel">取消更新</button><div class="actions"><button class="button secondary" type="button" data-act="u-save-draft">保存更新草稿</button><button class="button" type="button" data-act="u-publish">发布新版本 v' + (m.currentVersion + 1) + '</button></div></div>';
  return h;
}
function updateGates() {
  const u = state.update; if (!u) return [];
  const need = u.paras.filter((p) => (p.status === 'changed' || p.status === 'added') && !p.checked);
  return need.length ? [{ label: '还有 ' + need.length + ' 段新内容未核对' }] : [];
}

/* ---------- 主题编辑 ---------- */
function startTopicEdit(id) {
  const t = topicById(id);
  if (!t) return;
  const base = t.draft ? t.draft : t;
  state.topicEdit = { id: id, dirty: false, d: clone({ name: base.name, group: t.group, usage: base.usage, aiOn: base.aiOn, expiry: base.expiry, intro: base.intro.map(stripPara), points: base.points.map(stripPara) }) };
  render();
}
function stripPara(p) { return { id: p.id, text: p.text, source: p.source ? clone(p.source) : null, checked: p.checked, modified: !!p.modified, system: !!p.system }; }
function topicEditHTML(t) {
  const e = state.topicEdit;
  const d = e.d;
  let h = '<div class="detail-top"><button class="back-link" type="button" data-act="te-cancel"><svg class="ic"><use href="#i-back"/></svg>返回主题</button></div>';
  h += '<div class="toolbar"><div><h1>编辑主题：' + esc(d.name || t.name) + '</h1><p class="step-sub">修改正文、要点与发布设置。保存草稿不发布；发布后旧版本保留在版本记录中。</p></div></div>';
  h += '<div class="card"><label for="te-name">主题名称</label><input id="te-name" data-act="te-name" value="' + esc(d.name) + '">';
  h += '<label for="te-group">所属分组</label><select id="te-group" data-act="te-group"><option value="">不分组</option>' + DB.groups.map((g) => '<option value="' + g.id + '"' + (d.group === g.id ? ' selected' : '') + '>' + esc(g.name) + '</option>').join('') + '</select><p class="hint-line">分组只辅助导航，可以随时调整或不分组。</p></div>';
  h += '<div class="card"><h2>介绍</h2>' + d.intro.map((p, i) => topicEditParaHTML(p, i, 'intro')).join('') +
    '<div class="actions"><button class="button secondary" type="button" data-act="te-add-intro">＋ 添加介绍段落</button></div></div>';
  h += '<div class="card"><h2>要点</h2>' + d.points.map((p, i) => topicEditParaHTML(p, i, 'point')).join('') +
    '<div class="actions"><button class="button secondary" type="button" data-act="te-add-point">＋ 添加要点</button></div></div>';
  h += '<div class="card"><fieldset class="settings-group"><legend>用途</legend><label class="check-inline"><input type="radio" name="te-usage" value="internal" data-act="te-usage"' + (d.usage === 'internal' ? ' checked' : '') + '>仅内部使用</label><label class="check-inline"><input type="radio" name="te-usage" value="external" data-act="te-usage"' + (d.usage === 'external' ? ' checked' : '') + '>对外使用</label></fieldset>' +
    '<fieldset class="settings-group"><legend>AI 客服</legend><div class="switch-row"><span class="switch-state">' + (d.aiOn ? '允许 AI 客服使用' : '暂不用于 AI 客服') + '</span><label class="ios-switch"><input id="te-ai" type="checkbox" data-act="te-ai"' + (d.aiOn ? ' checked' : '') + '><span class="ios-slider"></span></label></div><p class="hint-line">关联与生成不会自动授权 AI；对外的段落还会检查来源是否满足对外条件。</p></fieldset>' +
    '<fieldset class="settings-group"><legend>AI 到期时间（可选，北京时间）</legend><input type="datetime-local" id="te-expiry" data-act="te-expiry" value="' + esc(expiryInputValue(d.expiry)) + '"></fieldset></div>';
  const un = d.intro.concat(d.points).filter((p) => String(p.text).trim() !== '' && !p.checked).length;
  if (un) h += '<p class="hint-line" style="text-align:right">发布前还需核对 ' + un + ' 段正文（勾选每段的「已核对」或删除）。</p>';
  h += '<div class="review-footer"><span class="actions"><button class="button secondary" type="button" data-act="te-organize">根据关联资料整理初稿（演示）</button></span><div class="actions"><button class="button secondary" type="button" data-act="te-cancel">取消</button><button class="button secondary" type="button" data-act="te-save">保存草稿</button><button class="button" type="button" data-act="te-publish">发布主题</button></div></div>';
  return h;
}
function topicEditParaHTML(p, i, kind) {
  const label = sourceLabel(p) || '成员人工撰写';
  return '<div class="para-card"><div class="para-head"><span class="para-no">' + (kind === 'intro' ? '介绍 ' : '要点 ') + (i + 1) + '</span>' + (p.modified ? '<span class="tag edit">已修改</span>' : '') + (p.system && !p.checked ? '<span class="tag review">系统初稿</span>' : '') + (p.system && p.checked ? '<span class="tag done">系统整理 · 已确认</span>' : '') + '<span class="src-label">来源：' + esc(label) + '</span><span class="grow"></span><label class="check-inline" style="margin:0"><input type="checkbox" data-act="te-para-check" data-kind="' + kind + '" data-idx="' + i + '"' + (p.checked ? ' checked' : '') + '>已核对</label><button class="icon-btn" type="button" data-act="te-del" data-kind="' + kind + '" data-idx="' + i + '" aria-label="删除这一段"><svg class="ic"><use href="#i-x"/></svg></button></div>' +
    '<textarea data-act="te-para-text" data-kind="' + kind + '" data-idx="' + i + '" rows="2" aria-label="' + (kind === 'intro' ? '介绍' : '要点') + ' ' + (i + 1) + '"' + '>' + esc(p.text) + '</textarea></div>';
}
function topicEditFor(id) { return state.topicEdit && state.topicEdit.id === id ? state.topicEdit : null; }

/* ---------- 保存与发布 ---------- */
function itemToMaterial(it, lifecycle) {
  const body = it.paras.some((p) => String(p.text).trim() !== '');
  const recog = it.recognition === 'pending' ? 'registered' : it.recognition;
  const m = {
    id: uid('m'), name: String(it.name || '').trim() || '未命名资料', format: it.format, size: it.size, url: it.url,
    lifecycle: lifecycle, usage: state.work.usage, aiOn: !!state.work.aiOn && body, expiry: state.work.expiry, aiIssue: '',
    recognition: recog, failReason: it.failReason || '', updated: todayStr(), currentVersion: lifecycle === 'published' ? 1 : 0, updateDraft: null,
    paras: clone(it.paras.filter((p) => String(p.text).trim() !== '' || p.checked)).map((p) => ({ id: p.id, text: p.text, loc: p.source || '人工撰写', checked: p.checked, modified: !!p.modified })),
    fields: clone(it.fields || []),
    topics: (it.confirmedTopics || []),
    versions: [], registeredNote: it.registeredNote || ''
  };
  if (state.work.aiOn && !body) m.aiIssue = '仅登记原件：没有可核对正文。';
  if (lifecycle === 'published') {
    m.versions = [{ v: 1, date: todayStr(), note: '从添加资料批次发布' }];
    m.versions[0].snapshot = clone({ name: m.name, paras: m.paras });
  }
  return m;
}
function saveBatchDraft(navigate) {
  const w = work(); if (!w || w.committed) return false;
  const unnamed = w.items.find((it) => it.format === 'manual' && !String(it.name || '').trim());
  if (unnamed) { toast('手动材料需要先填写名称，再保存草稿。', true); focusFirst('[data-act="c-item-name"]'); return false; }
  w.committed = true;   /* 防重复：同一批次只提交一次，保存后工作副本不再可用 */
  const n = w.items.length;
  w.items.forEach((it) => { DB.materials.unshift(itemToMaterial(it, 'draft')); });
  finishCapture();
  if (navigate) { state.route = { name: 'materials', id: null, step: 1 }; state.filters = { q: '', format: '', topic: '', status: '' }; state.backStack = []; render(); }
  toast('已保存为草稿（' + n + ' 件）：允许不完整，随时可以在全部资料中继续（演示内存数据）。');
  return true;
}
function publishGatePass() { const w = work(); return w && !captureGates(w).length; }
function doPublishBatch() {
  const w = work(); if (!w) return;
  const gates = captureGates(w);
  if (gates.length) { toast('还有未完成项：' + gates[0].label, true); focusFirst('[data-act="c-gate-go"]'); return; }
  if (w.usage === 'external' && w.aiOn) { openPublishConfirm(); return; }
  commitPublishBatch('');
}
function openPublishConfirm() {
  const w = work();
  openDialog({
    title: '确认对外发布并开启 AI？', icon: 'alert',
    body: '<p>这批 ' + w.items.length + ' 件资料将以「对外使用 + 允许 AI 客服使用」发布：</p>' + kvRows([
      ['用途', '对外使用'],
      ['AI 客服', '允许（仅限有已核对正文的资料）'],
      ['AI 到期', fmtExpiry(w.expiry)],
      ['说明', '仅登记原件（图片、视频、网页等）不会进入 AI 回答；主题关联本身不代表授权。']
    ]),
    actions: [
      { label: '取消', cls: 'secondary' },
      { label: '确认发布', cls: '', onClick: () => { closeDialog(); commitPublishBatch('确认已记录：对外 + AI 已被明确同意。'); } }
    ]
  });
}
function commitPublishBatch(extra) {
  const w = work(); if (!w || w.committed) return;
  w.committed = true;   /* 防重复：同一批次只允许提交一次，发布与草稿不会叠加生成重复资料 */
  const createdTopics = [];
  w.items.forEach((it) => {
    (it.sugs || []).forEach((s) => { if (s.active && s.kind === 'new' && s.createdId && createdTopics.indexOf(s.createdId) < 0) createdTopics.push(s.createdId); });
  });
  const n = w.items.length;
  w.items.forEach((it) => { DB.materials.unshift(itemToMaterial(it, 'published')); });
  /* 新主题保持草稿：只附上「待核对」的系统整理初稿与确切出处；
     资料核对不等于主题正文核对——主题正文必须由成员在主题编辑页核对后才可发布 */
  createdTopics.forEach((tid) => {
    const t = topicById(tid); if (!t) return;
    organizeIntoTopic(t, t);
    t.updated = todayStr();
  });
  finishCapture();
  state.route = { name: 'materials', id: null, step: 1 };
  state.filters = { q: '', format: '', topic: '', status: '' };
  state.backStack = [];
  render();
  toast('已发布 ' + n + ' 件资料（演示内存数据；不代表后台已完成处理）。' + (createdTopics.length ? ' 新主题为草稿，正文初稿待核对后发布。' : '') + (extra ? ' ' + extra : ''));
}
function finishCapture() { state.work = null; state.workDirty = false; }

/* ---------- 主题与资料关系调整 ---------- */
function detachMaterialFromTopic(mid, tid) {
  const m = materialById(mid); if (!m) return;
  m.topics = m.topics.filter((x) => x !== tid);
}
function attachMaterialToTopic(mid, tid) {
  const m = materialById(mid); if (!m) return;
  if (!m.topics.includes(tid)) m.topics.push(tid);
}
/* 根据关联资料整理主题正文初稿（演示）：只用已核对的文字段落及其确切出处（第一段），
   图片 / 视频 / 仅登记资料没有正文，不会被推测；只追加不覆盖；初稿默认「待核对」。 */
function organizeIntoTopic(target, t) {
  let n = 0;
  DB.materials.filter((m) => m.topics.includes(t.id)).forEach((m) => {
    const p = m.paras.find((x) => x.checked && String(x.text).trim() !== '');
    if (!p) return;
    if (target.intro.some((x) => x.source && x.source.mid === m.id && x.source.loc === p.loc)) return;
    target.intro.push({ id: uid('ti'), text: p.text, source: { mid: m.id, loc: p.loc }, checked: false, modified: false, system: true });
    n++;
  });
  return n;
}

/* ---------- 返回与未保存保护 ---------- */
/* 轻量内部返回栈：逐层回到进入前的位置（不依赖浏览器历史）；列表筛选保存在 filters，返回后继续生效 */
function backLabelForCurrent() {
  const r = state.route;
  if (r.name === 'topic') { const t = topicById(r.id); return t ? '返回主题「' + t.name + '」' : '返回全部主题'; }
  if (r.name === 'material') { const m = materialById(r.id); return m ? '返回资料「' + m.name + '」' : '返回全部资料'; }
  if (r.name === 'materials') return '返回全部资料';
  return '返回全部主题';
}
function pushBack(viaLabel) {
  const r = state.route;
  const entry = (r.name === 'topic' || r.name === 'material' || r.name === 'materials') ? { name: r.name, id: r.id } : { name: 'topics', id: null };
  entry.label = viaLabel || backLabelForCurrent();
  state.backStack.push(entry);
}
function currentBackLabel() { const b = state.backStack[state.backStack.length - 1]; return b ? b.label : '返回全部主题'; }
function goTopics() { state.backStack = []; state.route = { name: 'topics', id: null, step: 1 }; state.topicQ = ''; state.manageGroups = false; render(); }
function goMaterials() { state.backStack = []; state.route = { name: 'materials', id: null, step: 1 }; render(); }
function openTopic(id, viaLabel) {
  const t = topicById(id); if (!t) return;
  pushBack(viaLabel);
  if (t.group) state.navGroups[t.group] = true;
  state.route = { name: 'topic', id: id, step: 1 };
  render();
}
function openMaterial(id, viaLabel) {
  const m = materialById(id); if (!m) return;
  const fromTopic = state.route.name === 'topic' && state.route.id !== id;
  pushBack(viaLabel);
  if (fromTopic) state.reopenMats = true;   /* 返回主题时恢复「关联资料」展开（一次性） */
  state.route = { name: 'material', id: id, step: 1 };
  render();
}
function doBack() {
  const b = state.backStack.pop() || { name: 'topics', id: null };
  if (b.name === 'topic' && topicById(b.id)) { state.route = { name: 'topic', id: b.id, step: 1 }; }
  else if (b.name === 'material' && materialById(b.id)) { state.route = { name: 'material', id: b.id, step: 1 }; }
  else if (b.name === 'materials') { state.route = { name: 'materials', id: null, step: 1 }; }
  else { state.route = { name: 'topics', id: null, step: 1 }; }
  render();
}
function isDirty() { return state.workDirty || state.updateDirty || (state.topicEdit && state.topicEdit.dirty); }
function requestLeave(go) {
  if (!isDirty()) { go(); return; }
  state.pendingNav = go;
  openUnsavedDialog();
}
function openUnsavedDialog() {
  const acts = [];
  if (state.workDirty) acts.push({ label: '保存草稿并离开', cls: '', onClick: () => { if (saveBatchDraft(false)) { closeDialog(); applyNav(); } else { dlgError('请先给手动材料填写名称，再保存离开。'); return 'keep'; } } });
  if (state.updateDirty) acts.push({ label: '保存更新草稿并离开', cls: '', onClick: () => { saveUpdateDraft(false); closeDialog(); applyNav(); } });
  if (state.topicEdit && state.topicEdit.dirty) acts.push({ label: '保存修改为草稿并离开', cls: '', onClick: () => { if (saveTopicEditQuiet()) { closeDialog(); applyNav(); } else { dlgError('主题名称不能为空。'); return 'keep'; } } });
  acts.push({ label: '放弃修改并离开', cls: 'danger', onClick: () => { resetFlows(); closeDialog(); applyNav(); } });
  acts.push({ label: '继续编辑', cls: 'secondary' });
  openDialog({ title: '未保存的修改', icon: 'alert', body: '<p>这里还有没有保存的内容。可以保存为草稿再离开、放弃本次未保存的改动，或继续编辑。取消不会丢内容。</p>', actions: acts });
}
function applyNav() { const g = state.pendingNav; state.pendingNav = null; if (g) g(); }
function resetFlows() { state.work = null; state.workDirty = false; state.update = null; state.updateDirty = false; state.topicEdit = null; }

/* ---------- 更新草稿与发布 ---------- */
function saveUpdateDraft(navigate) {
  const m = materialById(state.route.id); const u = state.update;
  if (!m || !u) return false;
  m.updateDraft = clone({ name: u.name, paras: u.paras, at: todayStr() });
  state.updateDirty = false;
  if (navigate) { state.route = { name: 'material', id: m.id, step: 1 }; render(); }
  toast('已保存更新草稿：当前发布版本 v' + m.currentVersion + ' 继续有效，发布成功后才会替换。');
  return true;
}
function doPublishUpdate() {
  const m = materialById(state.route.id); const u = state.update;
  if (!m || !u) return;
  const gates = updateGates();
  if (gates.length) { toast('还不能发布：' + gates[0].label, true); return; }
  const changed = u.paras.filter((p) => p.status !== 'same').length;
  openDialog({
    title: '发布新版本 v' + (m.currentVersion + 1) + '？', icon: 'alert',
    body: '<p>发布后本资料将使用新版本内容；旧版本 v' + m.currentVersion + ' 保留在版本记录中，不会被覆盖。</p>' + kvRows([
      ['新文件', u.name],
      ['变化', changed + ' 段（含保留人工修改）'],
      ['用途与 AI', '沿用当前设置（' + USAGE_LABEL[m.usage] + (m.aiOn ? ' · AI 客服允许' : ' · AI 关闭') + '）']
    ]),
    actions: [
      { label: '取消', cls: 'secondary' },
      { label: '确认发布 v' + (m.currentVersion + 1), cls: '', onClick: () => { closeDialog(); commitUpdatePublish(m, u); } }
    ]
  });
}
function commitUpdatePublish(m, u) {
  const nextV = m.currentVersion + 1;
  m.paras = u.paras.map((p) => ({ id: p.old || uid('p'), text: p.text, loc: p.loc, checked: true, modified: p.status === 'kept-manual' ? true : false }));
  m.updated = todayStr();
  m.currentVersion = nextV;
  m.updateDraft = null;
  m.versions.push({ v: nextV, date: todayStr(), note: '更新文件：' + u.name + '（保留人工修改段落）' });
  m.versions[m.versions.length - 1].snapshot = clone({ name: m.name, paras: m.paras });
  state.update = null; state.updateDirty = false;
  state.route = { name: 'material', id: m.id, step: 1 };
  render();
  toast('已发布新版本 v' + nextV + '（演示内存数据）；旧版本仍保留在版本记录中。');
}

/* ---------- 主题保存与发布 ---------- */
function applyTopicEdit(t, d) {
  t.name = String(d.name || '').trim() || t.name;
  t.group = d.group || null;
  t.usage = d.usage; t.aiOn = d.aiOn; t.expiry = d.expiry;
  t.intro = d.intro.filter((p) => String(p.text).trim() !== '').map(clone);
  t.points = d.points.filter((p) => String(p.text).trim() !== '').map(clone);
  t.updated = todayStr();
}
function saveTopicEditQuiet() {
  const e = state.topicEdit; if (!e) return true;
  if (!String(e.d.name || '').trim()) return false;
  const t = topicById(e.id);
  if (t.lifecycle === 'draft') applyTopicEdit(t, e.d);
  else t.draft = clone(e.d);
  state.topicEdit = null;
  return true;
}
function saveTopicEdit(asPublish) {
  const e = state.topicEdit; if (!e) return;
  if (!String(e.d.name || '').trim()) { toast('主题名称不能为空。', true); const f = qs('#te-name'); if (f) f.focus(); return; }
  const t = topicById(e.id);
  if (asPublish) { publishTopicNow(t, e.d); return; }
  if (t.lifecycle === 'draft') { applyTopicEdit(t, e.d); toast('已保存（仍是草稿）。'); }
  else { t.draft = clone(e.d); toast('已保存修改草稿；发布后才会替换当前版本。'); }
  state.topicEdit = null;
  render();
}
function topicPublishGates(t, d) {
  const gates = [];
  const paras = d.intro.concat(d.points).filter((p) => String(p.text).trim() !== '');
  if (!paras.length) gates.push({ label: '正文为空：请先补充介绍或要点' });
  const un = paras.filter((p) => !p.checked).length;
  if (un) gates.push({ label: '还有 ' + un + ' 段正文未核对（在编辑页勾选「已核对」）' });
  return gates;
}
function publishTopicNow(t, d) {
  const gates = topicPublishGates(t, d);
  if (gates.length) { toast('还不能发布：' + gates[0].label, true); return; }
  const doCommit = () => {
    closeDialog();
    applyTopicEdit(t, d);
    if (t.lifecycle !== 'published') t.lifecycle = 'published';
    t.draft = null;
    const v = (t.versions.length ? t.versions[t.versions.length - 1].v : 0) + 1;
    t.versions.push({ v: v, date: todayStr(), note: '主题正文更新' });
    state.topicEdit = null;
    render();
    toast('主题已发布（演示内存数据），v' + v + ' 已记录。');
  };
  if (d.usage === 'external' && d.aiOn) {
    openDialog({
      title: '确认对外发布并开启 AI？', icon: 'alert',
      body: '<p>主题正文将对外使用并允许 AI 客服引用：</p>' + kvRows([['用途', '对外使用'], ['AI 客服', '允许'], ['AI 到期', fmtExpiry(d.expiry)], ['说明', '来源不符合对外条件的段落仍不会进入对外回答。']]),
      actions: [{ label: '取消', cls: 'secondary' }, { label: '确认发布', cls: '', onClick: doCommit }]
    });
  } else doCommit();
}
function topicPublishClick() {
  const t = topicById(state.route.id); if (!t) return;
  const e = state.topicEdit && state.topicEdit.id === t.id ? state.topicEdit : null;
  if (e) { publishTopicNow(t, e.d); return; }
  const d = t.draft ? t.draft : clone({ name: t.name, group: t.group, usage: t.usage, aiOn: t.aiOn, expiry: t.expiry, intro: t.intro.map(stripPara), points: t.points.map(stripPara) });
  publishTopicNow(t, d);
}

/* ---------- 对话框 ---------- */
function openDialog(o) {
  state.lastFocus = document.activeElement;
  state.dlgActions = o.actions || [];
  const dlg = qs('#dlg');
  const icon = o.icon ? '<div class="dlg-icon ' + (o.iconCls || '') + '"><svg class="ic" style="width:18px;height:18px"><use href="#i-' + o.icon + '"/></svg></div>' : '';
  dlg.innerHTML = '<div class="dlg-head">' + icon + '<h2 class="dlg-title" id="dlg-title">' + esc(o.title) + '</h2></div>' +
    '<div class="dlg-body">' + o.body + '</div>' +
    '<div class="dlg-error" id="dlg-error"><svg class="ic"><use href="#i-alert"/></svg><span></span></div>' +
    '<div class="dlg-foot">' + state.dlgActions.map((a, i) => '<button type="button" class="button ' + (a.cls || 'secondary') + '" data-dlg="' + i + '">' + esc(a.label) + '</button>').join('') + '</div>';
  if (dlg.showModal) dlg.showModal();
  const f = o.focus ? qs(o.focus, dlg) : qs('.dlg-foot .button', dlg);
  if (f) f.focus();
}
function closeDialog() { const dlg = qs('#dlg'); if (dlg && dlg.open) dlg.close(); }
function dlgError(msg) {
  const el = qs('#dlg-error');
  if (!el) return;
  if (el.classList) el.classList.toggle('on', !!msg);
  const sp = qs('span', el); if (sp) sp.textContent = msg || '';
}
function kvRows(rows) { return '<dl class="summary-list">' + rows.map((r) => '<div class="kv"><dt>' + esc(r[0]) + '</dt><dd>' + esc(r[1]) + '</dd></div>').join('') + '</dl>'; }
function inputDialog(o) {
  openDialog({
    title: o.title, icon: o.icon || 'pen',
    body: '<label for="dlg-input">' + esc(o.label) + '</label>' + (o.textarea ? '<textarea id="dlg-input" rows="4" placeholder="' + esc(o.placeholder || '') + '">' + esc(o.value || '') + '</textarea>' : '<input id="dlg-input" value="' + esc(o.value || '') + '" placeholder="' + esc(o.placeholder || '') + '">') + (o.note ? '<p class="hint-line">' + esc(o.note) + '</p>' : ''),
    focus: '#dlg-input',
    actions: [
      { label: '取消', cls: 'secondary' },
      { label: o.okLabel || '确定', cls: '', onClick: () => { const inp = qs('#dlg-input'); const v = inp ? inp.value : ''; return o.onOk(v); } }
    ]
  });
}
function selectDialog(o) {
  openDialog({
    title: o.title, icon: o.icon || 'link',
    body: '<label for="dlg-select">' + esc(o.label) + '</label><select id="dlg-select">' + o.options.map((x) => '<option value="' + x[0] + '">' + esc(x[1]) + '</option>').join('') + '</select>',
    focus: '#dlg-select',
    actions: [
      { label: '取消', cls: 'secondary' },
      { label: o.okLabel || '确定', cls: '', onClick: () => { const s = qs('#dlg-select'); return o.onOk(s ? s.value : ''); } }
    ]
  });
}
function onDialogClosed() { if (state.lastFocus && state.lastFocus.focus) state.lastFocus.focus(); }

/* ---------- Toast ---------- */
function toast(msg, warn) {
  const t = document.createElement('div');
  t.className = 'toast' + (warn ? ' warn' : '');
  t.textContent = msg;
  const host = qs('#toasts'); if (host) host.appendChild(t);
  if (requestAnimationFrame) requestAnimationFrame(() => t.classList.add('show'));
  setTimeout(() => { t.classList.remove('show'); setTimeout(() => t.remove(), 250); }, 4200);
}
function focusFirst(sel) { const el = qs(sel); if (el && el.focus) el.focus(); }

/* ---------- 事件：点击 ---------- */
function onClick(e) {
  const t = e.target;
  if (t.tagName === 'INPUT' || t.tagName === 'SELECT' || t.tagName === 'TEXTAREA') return;
  const el = t.closest ? t.closest('[data-act]') : null;
  if (!el) return;
  const act = el.getAttribute('data-act');
  const w = work();
  switch (act) {
    case 'nav': {
      const v = el.getAttribute('data-view');
      if (state.role !== 'admin' && (v === 'capture' || v === 'update')) { toast('只读成员不能添加或更新资料（演示视角差异）。', true); return; }
      closeMobileNav();   /* 明确点击侧栏导航目标后收起移动抽屉（桌面无影响） */
      requestLeave(() => { resetFlows(); if (v === 'topics') goTopics(); else if (v === 'materials') goMaterials(); else if (v === 'capture') startCapture(''); else { state.route = { name: 'qa', id: null, step: 1 }; render(); } });
      return;
    }
    case 'nav-topic': {
      const id = el.getAttribute('data-id');
      if (state.route.name === 'topic' && state.route.id === id) { closeMobileNav(); closeMobileTopicBrowse(); return; }   /* 重复点击当前主题：只关抽屉/收起窄屏浏览树，不压返回层 */
      closeMobileNav();
      closeMobileTopicBrowse();   /* 窄屏选中主题后收起内部浏览导航（桌面无此折叠） */
      requestLeave(() => { resetFlows(); openTopic(id, null); });
      return;
    }
    case 'nav-toggle-group': toggleNavGroup(el.getAttribute('data-gid')); return;
    case 'ov-fold': { toggleOverviewGroup(el.getAttribute('data-gid')); return; }   /* 中央总览分组开合：只折叠、不导航、不压返回层 */
    case 'nav-topic-files': { toggleNavTopicFiles(el.getAttribute('data-id')); return; }   /* 只开合主题下的文件列表：不导航、不压返回层 */
    case 'nav-file': {
      const id = el.getAttribute('data-id');
      if (!materialById(id)) return;
      if (state.route.name === 'material' && state.route.id === id) { closeMobileNav(); closeMobileTopicBrowse(); return; }   /* 重复点击当前文件：只关抽屉/收起窄屏浏览树，不压返回层 */
      closeMobileNav();
      closeMobileTopicBrowse();   /* 窄屏选中文件后收起内部浏览树（桌面无此折叠） */
      requestLeave(() => { resetFlows(); openMaterial(id, null); });   /* 返回上下文交给既有 backStack：总览树开文件→实际总览；主题树开文件→原主题 */
      return;
    }
    case 't-open': { const id = el.getAttribute('data-id'); requestLeave(() => { resetFlows(); openTopic(id, null); }); return; }
    case 't-open-from-mat': { const id = el.getAttribute('data-id'); const m = materialById(state.route.id); requestLeave(() => { resetFlows(); openTopic(id, m ? '返回资料「' + m.name + '」' : null); }); return; }
    case 'm-open': { const id = el.getAttribute('data-id'); requestLeave(() => { resetFlows(); openMaterial(id, null); }); return; }
    case 't-rel-open': { const id = el.getAttribute('data-id'); const t2 = topicById(state.route.id); requestLeave(() => { resetFlows(); openTopic(id, t2 ? '返回主题「' + t2.name + '」' : null); }); return; }
    case 'd-back': { requestLeave(() => { resetFlows(); doBack(); }); return; }
    case 'search-clear': {
      if (state.route.name === 'materials') {
        state.filters.q = ''; refreshListRegion();
        const inp = qs('#search-q'); if (inp) { inp.value = ''; inp.focus(); }
        const c = qs('#search-clear'); if (c) c.hidden = true;
        toast('已清空关键词；其他筛选保留。');
      } else if (state.route.name === 'topics') {
        state.topicQ = ''; refreshTopicsRegion();
        const inp = qs('#search-q'); if (inp) { inp.value = ''; inp.focus(); }
        const c = qs('#search-clear'); if (c) c.hidden = true;
      }
      return;
    }
    case 'filter-clear': { state.filters = { q: '', format: '', topic: '', status: '' }; render(); return; }
    case 'manage-groups-toggle': { state.manageGroups = !state.manageGroups; render(); return; }
    case 'topic-new-open': openTopicNewDialog(); return;
    case 'group-new-open': openGroupNewDialog(); return;
    case 'group-rename': { const gid = el.getAttribute('data-gid'); const g = groupById(gid); if (g) inputDialog({ title: '重命名分组', label: '分组名称', value: g.name, okLabel: '保存', onOk: (v) => { const name = String(v || '').trim(); if (!name) { dlgError('分组名称不能为空。'); return 'keep'; } g.name = name; closeDialog(); render(); toast('分组已改名。'); } }); return; }
    case 'group-delete': { const gid = el.getAttribute('data-gid'); const g = groupById(gid); if (!g) return; const n = topicsOfGroup(gid).length; openDialog({ title: '删除分组「' + g.name + '」？', icon: 'alert', body: '<p>删除分组不会删除主题：组里的 ' + n + ' 个主题会变成「未分组」，资料关联与正文都不受影响。</p>', actions: [{ label: '取消', cls: 'secondary' }, { label: '删除分组', cls: 'danger', onClick: () => { DB.groups = DB.groups.filter((x) => x.id !== gid); DB.topics.forEach((t) => { if (t.group === gid) t.group = null; }); closeDialog(); render(); toast('分组已删除，主题保留为未分组。'); } }] }); return; }
    case 't-edit': { startTopicEdit(el.getAttribute('data-id') || state.route.id); return; }
    case 'te-cancel': { const go = () => { state.topicEdit = null; state.route = { name: 'topic', id: state.route.id, step: 1 }; render(); }; if (state.topicEdit && state.topicEdit.dirty) requestLeave(go); else go(); return; }
    case 'te-save': saveTopicEdit(false); return;
    case 'te-publish': saveTopicEdit(true); return;
    case 'te-add-intro': if (state.topicEdit) { state.topicEdit.d.intro.push({ id: uid('ti'), text: '', source: null, checked: false, modified: false }); state.topicEdit.dirty = true; render(); focusLastPara('intro'); } return;
    case 'te-add-point': if (state.topicEdit) { state.topicEdit.d.points.push({ id: uid('tp'), text: '', source: null, checked: false, modified: false }); state.topicEdit.dirty = true; render(); focusLastPara('point'); } return;
    case 't-organize': {
      const t = topicById(state.route.id); if (!t) return;
      if (t.lifecycle === 'draft') {
        const n = organizeIntoTopic(t, t);
        render();
        toast(n ? ('已加入 ' + n + ' 段系统整理初稿（待核对）；核对后才可发布主题。') : '没有可整理的已核对正文（仅登记或未核对资料不参与）。');
      } else {
        startTopicEdit(t.id);
        const e = state.topicEdit; if (!e) return;
        const n = organizeIntoTopic(e.d, t);
        if (n) e.dirty = true;
        render();
        toast(n ? ('已加入 ' + n + ' 段系统整理初稿（待核对）；保存草稿后发布。') : '没有可整理的已核对正文。');
      }
      return;
    }
    case 'te-organize': {
      const e = state.topicEdit; const t = e && topicById(e.id); if (!e || !t) return;
      const n = organizeIntoTopic(e.d, t);
      if (n) e.dirty = true;
      render();
      toast(n ? ('已加入 ' + n + ' 段系统整理初稿（待核对）；不会覆盖已有内容。') : '没有可整理的已核对正文。');
      return;
    }
    case 'te-del': {
      const e2 = state.topicEdit; if (!e2) return;
      const kind = el.getAttribute('data-kind'); const idx = Number(el.getAttribute('data-idx'));
      const list = kind === 'intro' ? e2.d.intro : e2.d.points;
      list.splice(idx, 1); e2.dirty = true; render(); return;
    }
    case 't-publish': topicPublishClick(); return;
    case 't-discard': { const t2 = topicById(state.route.id); if (!t2 || !t2.draft) return; openDialog({ title: '放弃未发布的主题修改？', icon: 'alert', body: '<p>放弃后主题回到已发布版本的内容；当前发布的正文不受影响。</p>', actions: [{ label: '取消', cls: 'secondary' }, { label: '放弃修改', cls: 'danger', onClick: () => { t2.draft = null; closeDialog(); render(); toast('已放弃未发布的主题修改。'); } }] }); return; }
    case 't-add-mat': { const id = state.route.id; requestLeave(() => { resetFlows(); startCapture(id); }); return; }
    case 't-attach-pick': return;
    case 't-attach': {
      const sel = qs('#t-attach-select'); const mid = sel ? sel.value : '';
      if (!mid) { toast('请先选择要关联的资料。', true); return; }
      attachMaterialToTopic(mid, state.route.id);
      render(); toast('已关联《' + materialById(mid).name + '》到本主题。');
      return;
    }
    case 't-attach-pick': { const sel = qs('#t-attach-select'); if (sel && sel.focus) sel.focus(); return; }
    case 't-detach': {
      const mid = el.getAttribute('data-id'); const m = materialById(mid); const tid = state.route.id;
      openDialog({ title: '移除关联？', icon: 'alert', body: '<p>把《' + esc(m ? m.name : '') + '》从本主题移除：资料本身不会被删除，它在其他主题的关联不受影响。</p>', actions: [{ label: '取消', cls: 'secondary' }, { label: '移除关联', cls: 'danger', onClick: () => { detachMaterialFromTopic(mid, tid); closeDialog(); render(); toast('已移除关联（资料保留在全部资料中）。'); } }] });
      return;
    }
    case 't-rel': {
      const sel = qs('#t-rel-select'); const rid = sel ? sel.value : ''; const t2 = topicById(state.route.id);
      if (!rid || !t2) { toast('请先选择相关主题。', true); return; }
      t2.related.push(rid); render(); toast('已添加相关主题。');
      return;
    }
    case 't-unrel': { const t2 = topicById(state.route.id); const rid = el.getAttribute('data-id'); if (t2) { t2.related = t2.related.filter((x) => x !== rid); render(); toast('已取消相关主题（主题本身保留）。'); } return; }
    case 'm-update': { startUpdate(el.getAttribute('data-id') || state.route.id); return; }
    case 'm-manual': {
      const m = materialById(state.route.id);
      if (!m) return;
      inputDialog({ title: '补充手动正文', label: '正文内容（将标注为「人工撰写」）', textarea: true, placeholder: '例如：设备照片对应的安装位置说明…', note: '成员人工撰写的内容与原件并列登记；不会把图片、视频等原件描述成自动识别结果。', okLabel: '保存手动正文', onOk: (v) => { const text = String(v || '').trim(); if (!text) { dlgError('正文不能为空。'); return 'keep'; } m.recognition = 'manual'; m.paras.push({ id: uid('p'), text: text, loc: '人工撰写', checked: true, modified: false }); closeDialog(); render(); toast('已补充手动正文（人工撰写）。'); } });
      return;
    }
    case 'm-retry': { toast('已记录重试请求（演示）：本原型不执行真实识别，状态不会改变；真实系统为后端异步任务。'); return; }
    case 'm-disable': { const m = materialById(state.route.id); if (!m) return; openDialog({ title: '停用这份资料？', icon: 'alert', body: '<p>停用后不再用于客户与 AI 回答；企业成员仍可阅读，历史与原件保留。可随时重新启用。</p>', actions: [{ label: '取消', cls: 'secondary' }, { label: '停用', cls: 'danger', onClick: () => { m.lifecycle = 'disabled'; closeDialog(); render(); toast('已停用（演示）。'); } }] }); return; }
    case 'm-enable': { const m = materialById(state.route.id); if (!m) return; m.lifecycle = 'published'; render(); toast('已重新启用（演示）：AI 可用性取决于用途、授权与到期设置。'); return; }
    case 'm-untopic': { const tid = el.getAttribute('data-id'); const m = materialById(state.route.id); if (m) { detachMaterialFromTopic(m.id, tid); refreshMTopics(); toast('已取消关联（主题本身保留）。'); } return; }
    case 'm-addtopic': { const sel = qs('#m-attach-select'); const tid = sel ? sel.value : ''; const m = materialById(state.route.id); if (!m) return; if (!tid) { toast('请先选择主题。', true); return; } attachMaterialToTopic(m.id, tid); refreshMTopics(); toast('已关联到「' + (topicById(tid) ? topicById(tid).name : '') + '」。'); return; }
    case 'm-attach-focus': { const sel = qs('#m-attach-select'); if (sel) sel.focus(); else toast('在「关联主题」一栏可以选择要关联的主题。'); return; }
    case 'm-field-add': {
      const m = materialById(state.route.id); if (!m) return;
      const name = qs('#mf-name') ? qs('#mf-name').value.trim() : '';
      const type = qs('#mf-type') ? qs('#mf-type').value : 'text';
      const unit = qs('#mf-unit') ? qs('#mf-unit').value.trim() : '';
      const value = qs('#mf-value') ? qs('#mf-value').value.trim() : '';
      const source = qs('#mf-source') ? qs('#mf-source').value.trim() : '';
      if (!name || !value || !source) { toast('字段需要名称、值和来源。', true); return; }
      m.fields.push({ id: uid('f'), name: name, type: type, unit: type === 'number' ? unit : '', value: value, source: source });
      render(); toast('已添加字段（含来源）。');
      return;
    }
    case 'm-field-del': { const m = materialById(state.route.id); const fid = el.getAttribute('data-id'); if (m) { m.fields = m.fields.filter((f) => f.id !== fid); render(); toast('已删除字段。'); } return; }
    case 'ms-apply': {
      const m = materialById(state.route.id); if (!m) return;
      const usageEl = qs('input[name="ms-usage"]:checked');
      const usage = usageEl ? usageEl.value : m.usage;
      const aiEl = qs('#ms-ai');
      const aiOn = aiEl ? aiEl.checked : m.aiOn;
      const expEl = qs('#ms-expiry');
      const expiry = expEl ? expEl.value : m.expiry;
      if (aiOn && !hasBody(m)) { toast('没有可核对正文：仅登记原件无法开启 AI 客服。', true); return; }
      if (aiOn && m.lifecycle === 'disabled') { toast('已停用资料不能开启 AI 客服使用。', true); return; }
      const commit = (extra) => { m.usage = usage; m.aiOn = aiOn; m.expiry = expiry; render(); toast('发布设置已更新（演示）。' + (extra || '')); };
      if (usage === 'external' && aiOn) {
        openDialog({ title: '确认对外 + AI？', icon: 'alert', body: '<p>这份资料将对外使用并允许 AI 客服引用：</p>' + kvRows([['用途', '对外使用'], ['AI 客服', '允许'], ['AI 到期', fmtExpiry(expiry)]]), actions: [{ label: '取消', cls: 'secondary' }, { label: '确认', cls: '', onClick: () => { closeDialog(); commit(''); } }] });
      } else commit('');
      return;
    }
    default: return;
  }
}
function focusLastPara(kind) {
  const list = qsa('[data-act="te-para-text"][data-kind="' + kind + '"]');
  if (list.length) { const el = list[list.length - 1]; if (el.scrollIntoView) el.scrollIntoView({ block: 'center' }); if (el.focus) el.focus(); }
}

/* ---------- 事件：添加资料流程的点击 ---------- */
function captureClick(act, el) {
  const w = work();
  switch (act) {
    case 'pick-file': { const fi = qs('#file-input'); if (fi) fi.click(); return; }
    case 'c-input-mode': {
      /* 切换第 1 步的输入方式：只换显示哪一个卡片（就地更新，不整页重渲染）——
         已添加列表 / 工作批次、预选主题、列表里未提交的编辑内容都不受影响 */
      const m = el.getAttribute('data-mode');
      if (!w || (m !== 'file' && m !== 'url' && m !== 'text')) return;
      w.inputMode = m;
      ['file', 'url', 'text'].forEach((k) => {
        const card = qs('[data-mode-card="' + k + '"]');
        if (card) card.hidden = (k !== m);
        const btn = qs('[data-act="c-input-mode"][data-mode="' + k + '"]');
        if (btn) {
          btn.setAttribute('aria-pressed', k === m ? 'true' : 'false');
          if (btn.classList) btn.classList.toggle('on', k === m);
        }
      });
      return;
    }
    case 'c-add-url': {
      const inp = qs('#c-url'); const url = inp ? inp.value.trim() : '';
      if (!url || !/^https?:\/\//.test(url)) { toast('请输入以 http(s):// 开头的网页地址。', true); return; }
      addWorkItem({ name: url, format: 'web', src: 'url', url: url });
      markWorkDirty(); render(); toast('已登记网页地址（只登记链接，不抓取内容）。');
      return;
    }
    case 'c-add-text': {
      addWorkItem({ name: '', format: 'manual', src: 'text' });
      markWorkDirty(); render(); focusFirst('[data-act="c-item-name"]');
      return;
    }
    case 'c-load-sample': {
      MIX_BATCH.forEach((s) => {
        const it = addWorkItem({ name: s.name, format: s.format, size: s.size, src: s.src, sampleKey: sampleKeyFor(s.name, s.format) });
      });
      markWorkDirty(); render(); toast('已载入混合示例：PDF、DOCX、图片、视频、网页各 1 件（演示数据）。');
      return;
    }
    case 'c-remove-item': { if (w && !w.assocConfirmed) { w.items = w.items.filter((x) => x.id !== el.getAttribute('data-id')); markWorkDirty(); render(); } return; }
    case 'c-next': { if (!step1Ready()) { toast('请先添加至少一件资料。', true); return; } if (w) { w.step = 2; } buildSuggestions(); initStep2Folds(); render(); focusStepHeading(); return; }
    case 'c-back': { if (w) { w.step = 1; } render(); focusStepHeading(); return; }
    case 'c-to-step2': { if (w) { w.step = 2; } render(); focusStepHeading(); return; }
    case 'c-next2': { if (w) { w.step = 3; } render(); focusStepHeading(); return; }
    case 'c-fold-body': { const it = w && w.items.find((x) => x.id === el.getAttribute('data-id')); if (it) { it.foldBody = !it.foldBody; syncItemFold(it, 'body'); } return; }
    case 'c-fold-topic': { const it = w && w.items.find((x) => x.id === el.getAttribute('data-id')); if (it) { it.foldTopic = !it.foldTopic; syncItemFold(it, 'topic'); } return; }
    case 'c-use-samples': {
      if (!w) return;
      w.items.forEach((it) => {
        if (it.sampleKey && !it.paras.length) {
          it.recognition = 'recognized';
          it.paras = SAMPLE_PARAS[it.sampleKey].map((p) => ({ id: uid('p'), text: p.text, loc: p.loc, source: p.loc, checked: false, modified: false, added: false }));
        }
      });
      initStep2Folds();   /* 填入后默认展开第一件有正文的资料 */
      markWorkDirty(); render();
      toast('已填入示例整理结果：请逐段检查与核对；图片 / 视频 / 网页仍为「仅登记」。');
      return;
    }
    case 'c-para-check': return;
    case 'c-add-para': {
      if (!w) return;
      const it = w.items.find((x) => x.id === el.getAttribute('data-id'));
      if (!it) return;
      it.paras.push({ id: uid('p'), text: '', loc: '', source: null, checked: false, modified: true, added: true });
      markWorkDirty(); render();
      return;
    }
    case 'c-field-add': {
      const it = w && w.items.find((x) => x.id === el.getAttribute('data-id'));
      if (!it) return;
      const name = qs('#cf-name-' + it.id) ? qs('#cf-name-' + it.id).value.trim() : '';
      const type = qs('#cf-type-' + it.id) ? qs('#cf-type-' + it.id).value : 'text';
      const unit = qs('#cf-unit-' + it.id) ? qs('#cf-unit-' + it.id).value.trim() : '';
      const value = qs('#cf-value-' + it.id) ? qs('#cf-value-' + it.id).value.trim() : '';
      const source = qs('#cf-source-' + it.id) ? qs('#cf-source-' + it.id).value.trim() : '';
      if (!name || !value || !source) { toast('字段需要名称、值和来源。', true); return; }
      it.fields.push({ id: uid('f'), name: name, type: type, unit: type === 'number' ? unit : '', value: value, source: source });
      markWorkDirty(); render(); toast('已添加字段（含来源），发布后可在资料详情查看。');
      return;
    }
    case 'c-field-del': {
      const it = w && w.items.find((x) => x.id === el.getAttribute('data-id'));
      if (!it) return;
      it.fields.splice(Number(el.getAttribute('data-idx')), 1);
      markWorkDirty(); render();
      return;
    }
    case 'sg-reject': { if (w && w.assocConfirmed) return; const s = findSug(el.getAttribute('data-id')); if (s) { s.active = false; markWorkDirty(); render(); } return; }
    case 'sg-undo': { if (w && w.assocConfirmed) return; const s = findSug(el.getAttribute('data-id')); if (s) { s.active = true; markWorkDirty(); render(); } return; }
    case 'sg-rename': {
      if (w && w.assocConfirmed) return;
      const s = findSug(el.getAttribute('data-id')); if (!s) return;
      inputDialog({ title: '给建议的新主题改名', label: '主题名称', value: s.name, okLabel: '保存名称', onOk: (v) => { const name = String(v || '').trim(); if (!name) { dlgError('名称不能为空。'); return 'keep'; } s.name = name; markWorkDirty(); closeDialog(); render(); } });
      return;
    }
    case 'sg-merge': {
      if (w && w.assocConfirmed) return;
      const s = findSug(el.getAttribute('data-id')); if (!s) return;
      const opts = existingTopicsForSelect([]).map((t) => [t.id, t.name]);
      selectDialog({ title: '合并到已有主题', label: '选择要合并到的主题', options: opts, okLabel: '合并', onOk: (tid) => {
        const t = topicById(tid); if (!t) return;
        const it = w.items.find((x) => x.sugs.includes(s));
        if (it) {
          it.sugs = it.sugs.filter((x) => x !== s);
          if (!it.sugs.some((x) => x.active && x.kind === 'existing' && x.tid === tid)) it.sugs.push({ id: uid('sg'), kind: 'existing', tid: tid, name: t.name, active: true });
        }
        markWorkDirty(); closeDialog(); render(); toast('已合并到「' + t.name + '」。');
      } });
      return;
    }
    case 'c-addtopic': {
      if (w && w.assocConfirmed) return;
      const it = w && w.items.find((x) => x.id === el.getAttribute('data-id'));
      if (!it) return;
      const sel = qs('#c-addtopic-' + it.id); const tid = sel ? sel.value : '';
      if (!tid) { toast('请先选择要追加的主题。', true); return; }
      const t = topicById(tid);
      if (!it.sugs.some((x) => x.active && x.kind === 'existing' && x.tid === tid)) it.sugs.push({ id: uid('sg'), kind: 'existing', tid: tid, name: t ? t.name : '', active: true });
      markWorkDirty(); render();
      return;
    }
    case 'c-apply-common': {
      if (!w || w.assocConfirmed) return;
      const sel = qs('#c-common-topic'); const tid = sel ? sel.value : '';
      if (!tid) { toast('请先选择要作为共同主题的主题。', true); return; }
      const t = topicById(tid);
      w.items.forEach((it) => { if (!it.sugs.some((x) => x.active && x.kind === 'existing' && x.tid === tid)) it.sugs.unshift({ id: uid('sg'), kind: 'existing', tid: tid, name: t ? t.name : '', active: true }); });
      markWorkDirty(); render(); toast('已为整批添加共同主题「' + (t ? t.name : '') + '」；每件仍可单独调整。');
      return;
    }
    case 'c-confirm-assoc': { confirmAssoc(); return; }
    case 'c-gate-go': {
      const go = el.getAttribute('data-go');
      if (!w) return;
      if (go === 'items' || go === 'nobody') { w.step = 1; render(); focusStepHeading(); focusFirst(go === 'items' ? '[data-act="c-item-name"]' : '[data-act="c-item-text"]'); return; }
      w.step = 2;
      if (go === 'paras') openFirstFold('para');
      else if (go === 'src') openFirstFold('src');
      render(); focusStepHeading();
      if (go === 'pending') focusFirst('#use-samples-btn');
      else if (go === 'src') focusFirst('[data-act="c-para-source"]');
      else focusFirst('[data-act="c-para-check"]');
      return;
    }
    case 'c-save-draft': { saveBatchDraft(true); return; }
    case 'c-publish': { doPublishBatch(); return; }
  }
}
function findSug(sid) {
  const w = work(); if (!w) return null;
  for (const it of w.items) { const s = it.sugs.find((x) => x.id === sid); if (s) return Object.assign(s, { __item: it }); }
  return null;
}
function confirmAssoc() {
  const w = work(); if (!w) return;
  if (w.assocConfirmed) { toast('主题与关联已经确认过了。'); return; }
  /* 校验新主题建议：名称非空、不与已有主题重名；同一批次里同名的新主题建议合并为一个主题 */
  const news = [];
  w.items.forEach((it) => it.sugs.forEach((s) => { if (s.active && s.kind === 'new') news.push(s); }));
  for (const s of news) {
    const name = String(s.name || '').trim();
    if (!name) { toast('新的主题建议还没有名称：请先改名或删除。', true); return; }
    if (DB.topics.find((t) => t.name === name)) { toast('已有同名主题「' + name + '」：请用「合并到已有主题」或改名。', true); return; }
  }
  /* 同一名称只建立一个主题（草稿），涉及该名称的每件资料分别关联到它；单项仍可自行取消 */
  const byName = {};
  news.forEach((s) => {
    const name = String(s.name).trim();
    if (!byName[name]) {
      const t = { id: uid('t'), name: name, group: null, lifecycle: 'draft', usage: 'internal', aiOn: false, expiry: '', updated: todayStr(), draft: null, intro: [], points: [], related: [], versions: [] };
      DB.topics.push(t); byName[name] = t.id;
    }
    s.createdId = byName[name];
  });
  w.items.forEach((it) => {
    it.confirmedTopics = [];
    it.sugs.forEach((s) => {
      if (!s.active) return;
      if (s.kind === 'existing') it.confirmedTopics.push(s.tid);
      else if (s.kind === 'new' && s.createdId) it.confirmedTopics.push(s.createdId);
    });
  });
  w.assocConfirmed = true;
  render();
  toast('已确认：新主题以草稿建立，关联已生效；继续到发布设置。');
}
function focusStepHeading() {
  const h = qs('#app h1');
  if (h) { if (h.setAttribute) h.setAttribute('tabindex', '-1'); if (h.focus) h.focus({ preventScroll: true }); }
}

/* ---------- 事件：输入与选择 ---------- */
function onInput(e) {
  const t = e.target;
  if (t.id === 'search-q') {
    if (state.route.name === 'materials') { state.filters.q = t.value; refreshListRegion(); const c = qs('#search-clear'); if (c) c.hidden = !state.filters.q; }
    else if (state.route.name === 'topics') { state.topicQ = t.value; refreshTopicsRegion(); const c = qs('#search-clear'); if (c) c.hidden = !state.topicQ; }
    return;
  }
  const el = t.closest ? t.closest('[data-act]') : null;
  if (!el) return;
  const act = el.getAttribute('data-act');
  const w = work();
  switch (act) {
    /* 添加资料：步骤 1 */
    case 'c-item-name': { const it = w && w.items.find((x) => x.id === el.getAttribute('data-id')); if (it) { it.name = el.value; markWorkDirty(); } return; }
    case 'c-item-text': {
      const it = w && w.items.find((x) => x.id === el.getAttribute('data-id'));
      if (!it) return;
      if (!it.paras.length) it.paras.push({ id: uid('p'), text: '', loc: '人工撰写', source: '人工撰写', checked: true, modified: false });
      it.paras[0].text = el.value;
      markWorkDirty();
      return;
    }
    /* 添加资料：步骤 2 */
    case 'c-para-text': {
      const p = findWorkPara(el.getAttribute('data-id'));
      if (!p) return;
      if (p.text === el.value) return;
      p.text = el.value; p.modified = true; p.checked = false;
      markWorkDirty();
      syncParaCard(el);
      const iid = itemIdOfPara(p.id);
      refreshItemStatus(iid && work() ? work().items.find((x) => x.id === iid) : null);   /* 编辑撤销核对后同步数量 */
      return;
    }
    case 'c-para-source': { const p = findWorkPara(el.getAttribute('data-id')); if (p) { p.source = el.value || null; markWorkDirty(); } return; }
    /* 更新资料对照 */
    case 'u-para-text': {
      const u = state.update; if (!u) return;
      const p = u.paras[Number(el.getAttribute('data-idx'))]; if (!p) return;
      p.text = el.value; p.checked = false; state.updateDirty = true;
      return;
    }
    /* 主题编辑 */
    case 'te-name': if (state.topicEdit) { state.topicEdit.d.name = el.value; state.topicEdit.dirty = true; } return;
    case 'te-para-text': {
      const e2 = state.topicEdit; if (!e2) return;
      const kind = el.getAttribute('data-kind'); const idx = Number(el.getAttribute('data-idx'));
      const list = kind === 'intro' ? e2.d.intro : e2.d.points;
      const p = list[idx]; if (!p) return;
      if (p.text === el.value) return;
      /* 编辑即视为需要重新核对：正文段落要在编辑页勾选「已核对」后才允许发布 */
      p.text = el.value; p.modified = true; p.checked = false; e2.dirty = true;
      return;
    }
    default: return;
  }
}
function findWorkPara(pid) {
  const w = work(); if (!w) return null;
  for (const it of w.items) { const p = it.paras.find((x) => x.id === pid); if (p) return p; }
  return null;
}
/* 段落被修改后：勾选取消 + 卡片上补「人工修改」提示（真实浏览器中的就近反馈） */
function syncParaCard(el) {
  const card = el.closest ? el.closest('.para-card') : null;
  if (!card) return;
  const cb = card.querySelector ? card.querySelector('[data-act="c-para-check"]') : null;
  if (cb) cb.checked = false;
  const head = card.querySelector ? card.querySelector('.para-head') : null;
  if (head && head.insertAdjacentHTML && !(head.querySelector && head.querySelector('.tag.edit'))) {
    head.insertAdjacentHTML('afterbegin', '<span class="tag edit">人工修改</span>');
  }
}
function onChange(e) {
  const t = e.target;
  if (t.id === 'file-input') {
    const w = work(); if (!w) return;
    const files = t.files || [];
    for (let i = 0; i < files.length; i++) {
      const f = files[i];
      const name = f.name || '未命名文件';
      const ext = (name.match(/\.([a-z0-9]+)$/i) || [])[1] || '';
      const low = ext.toLowerCase();
      const format = low === 'pdf' ? 'pdf' : (low === 'docx' || low === 'doc' ? 'docx' : (['jpg', 'jpeg', 'png', 'gif', 'webp'].includes(low) ? 'image' : (['mp4', 'mov', 'webm'].includes(low) ? 'video' : 'other')));
      const size = f.size != null ? (f.size > 1048576 ? (Math.round(f.size / 104857.6) / 10) + ' MB' : Math.max(1, Math.round(f.size / 1024)) + ' KB') : '—';
      addWorkItem({ name: name, format: format, size: size, src: 'file', sampleKey: sampleKeyFor(name, format) });
    }
    markWorkDirty(); render();
    toast('已登记 ' + files.length + ' 个文件（只读取名称与大小，不读取内容、不上传）。');
    return;
  }
  if (t.id === 'u-file') {
    const m = materialById(state.route.id); if (!m) return;
    const f = (t.files && t.files[0]) || null;
    const name = f ? f.name : '未命名新版本';
    /* 其他资料没有预置对照示例：只登记新文件名，不表演对照结果 */
    state.update = { name: name, loaded: true, paras: clone(UPDATE_SAMPLE.paras), sampleFor: m.id };
    state.updateDirty = true;
    render();
    toast('已登记新版本文件（只读取名称）；对照文本仍为演示样例，请在真实实现中由后端整理。');
    return;
  }
  const el = t.closest ? t.closest('[data-act]') : null;
  if (!el) return;
  const act = el.getAttribute('data-act');
  const w = work();
  switch (act) {
    case 'filter-format': state.filters.format = t.value; refreshListRegion(); return;
    case 'filter-topic': state.filters.topic = t.value; refreshListRegion(); return;
    case 'filter-status': state.filters.status = t.value; refreshListRegion(); return;
    case 'topic-move': { const t2 = topicById(el.getAttribute('data-id')); if (t2) { t2.group = t.value || null; render(); toast('已移动主题分组。'); } return; }
    case 'c-para-check': {
      const p = findWorkPara(el.getAttribute('data-id'));
      if (p) {
        p.checked = t.checked; markWorkDirty();
        const iid = itemIdOfPara(p.id);
        refreshItemStatus(iid ? w.items.find((x) => x.id === iid) : null);   /* 数量标签实时更新，无整页重渲染 */
      }
      return;
    }
    case 'c-para-source': { const p = findWorkPara(el.getAttribute('data-id')); if (p) { p.source = t.value || null; markWorkDirty(); const iid = itemIdOfPara(p.id); refreshItemStatus(iid ? w.items.find((x) => x.id === iid) : null); } return; }
    case 'w-usage': if (w) { w.usage = t.value; markWorkDirty(); } return;
    case 'w-ai': if (w) { w.aiOn = t.checked; markWorkDirty(); render(); } return;
    case 'w-expiry': if (w) { w.expiry = t.value; markWorkDirty(); } return;
    case 'te-group': if (state.topicEdit) { state.topicEdit.d.group = t.value || null; state.topicEdit.dirty = true; } return;
    case 'te-usage': if (state.topicEdit) { state.topicEdit.d.usage = t.value; state.topicEdit.dirty = true; } return;
    case 'te-ai': if (state.topicEdit) { state.topicEdit.d.aiOn = t.checked; state.topicEdit.dirty = true; } return;
    case 'te-expiry': if (state.topicEdit) { state.topicEdit.d.expiry = t.value; state.topicEdit.dirty = true; } return;
    case 'te-para-check': { const e2 = state.topicEdit; if (e2) { const list = el.getAttribute('data-kind') === 'intro' ? e2.d.intro : e2.d.points; const p = list[Number(el.getAttribute('data-idx'))]; if (p) { p.checked = t.checked; e2.dirty = true; } } return; }
    case 'u-para-check': { const u = state.update; if (u) { const p = u.paras[Number(el.getAttribute('data-idx'))]; if (p) { p.checked = t.checked; state.updateDirty = true; } } return; }
    default: return;
  }
}

/* ---------- 更新与主题点击的补充接续 ---------- */
function updateClick(act, el) {
  switch (act) {
    case 'u-back': { const go = () => { state.update = null; state.updateDirty = false; state.route = { name: 'material', id: state.route.id, step: 1 }; render(); }; if (state.updateDirty) requestLeave(go); else go(); return; }
    case 'u-cancel': { const go = () => { state.update = null; state.updateDirty = false; state.route = { name: 'material', id: state.route.id, step: 1 }; render(); }; if (state.updateDirty) requestLeave(go); else go(); return; }
    case 'u-load-sample': loadUpdateSample(); return;
    case 'u-pick-file': { const fi = qs('#u-file'); if (fi) fi.click(); return; }
    case 'u-save-draft': saveUpdateDraft(true); return;
    case 'u-publish': doPublishUpdate(); return;
    case 'u-publish-now': {
      const m = materialById(state.route.id);
      if (m && m.updateDraft) {
        state.update = clone(m.updateDraft);
        state.route = { name: 'update', id: m.id, step: 1 };
        render();
        doPublishUpdate();
      }
      return;
    }
    case 'u-discard': {
      const m = materialById(state.route.id);
      if (!m || !m.updateDraft) return;
      openDialog({ title: '放弃更新草稿？', icon: 'alert', body: '<p>放弃后这份资料的更新草稿会被删除；当前发布版本不受影响。</p>', actions: [{ label: '取消', cls: 'secondary' }, { label: '放弃草稿', cls: 'danger', onClick: () => { m.updateDraft = null; closeDialog(); render(); toast('已放弃更新草稿（当前发布版本不变）。'); } }] });
      return;
    }
  }
}

/* ---------- 初始化 ---------- */
function onActClick(e) {
  const t = e.target;
  if (t.tagName === 'INPUT' || t.tagName === 'SELECT' || t.tagName === 'TEXTAREA') return;
  /* 对话框按钮统一在这里派发：data-dlg 指向 state.dlgActions 的下标；
     动作返回 'keep' 保留对话框（如校验失败），其它情况（含未定义回调的「取消」）执行后关闭。
     动作内部已自行 closeDialog 的也安全（closeDialog 只关闭仍开启的对话框）。 */
  const dlgBtn = t.closest ? t.closest('[data-dlg]') : null;
  if (dlgBtn) {
    const idx = Number(dlgBtn.getAttribute('data-dlg'));
    const a = state.dlgActions && state.dlgActions[idx];
    if (a) {
      const r = a.onClick ? a.onClick() : undefined;
      if (r !== 'keep') closeDialog();
    }
    return;
  }
  const el = t.closest ? t.closest('[data-act]') : null;
  if (!el) return;
  const act = el.getAttribute('data-act');
  if (act === 'u-back' || act === 'u-cancel' || act === 'u-load-sample' || act === 'u-pick-file' || act === 'u-save-draft' || act === 'u-publish' || act === 'u-publish-now' || act === 'u-discard') { updateClick(act, el); return; }
  if (state.route.name === 'capture') {
    if (act === 'pick-file' || act.indexOf('c-') === 0 || act.indexOf('sg-') === 0) {
      /* 收集栏优先（step1 的选择文件等）；否则走 captureClick */
      captureClick(act, el);
      return;
    }
  }
  onClick(e);
}
function openTopicNewDialog() {
  const groupOpts = DB.groups.map((g) => [g.id, g.name]);
  openDialog({
    title: '新建主题', icon: 'plus',
    body: '<label for="dlg-input">主题名称</label><input id="dlg-input" placeholder="例如：设备保养规范"><label for="dlg-select">所属分组（可选）</label><select id="dlg-select"><option value="">不分组</option>' + groupOpts.map((x) => '<option value="' + x[0] + '">' + esc(x[1]) + '</option>').join('') + '</select><p class="hint-line">新主题先保存为草稿；可以直接开始编辑正文。</p>',
    focus: '#dlg-input',
    actions: [
      { label: '取消', cls: 'secondary' },
      { label: '建立主题', cls: '', onClick: () => {
        const inp = qs('#dlg-input'); const name = inp ? inp.value.trim() : '';
        if (!name) { dlgError('请填写主题名称。'); return 'keep'; }
        const sel = qs('#dlg-select'); const gid = sel ? sel.value : '';
        const t = { id: uid('t'), name: name, group: gid || null, lifecycle: 'draft', usage: 'internal', aiOn: false, expiry: '', updated: todayStr(), draft: null, intro: [], points: [], related: [], versions: [] };
        DB.topics.push(t);
        closeDialog();
        openTopic(t.id, null);
        toast('主题已建立（草稿）：可以编辑正文，发布前需要至少一段正文。');
      } }
    ]
  });
}
function openGroupNewDialog() {
  inputDialog({ title: '新建分组', label: '分组名称', placeholder: '例如：客户与支持', okLabel: '建立分组', onOk: (v) => { const name = String(v || '').trim(); if (!name) { dlgError('分组名称不能为空。'); return 'keep'; } DB.groups.push({ id: uid('g'), name: name }); closeDialog(); render(); toast('分组已建立：把主题移动进去即可。'); } });
}
function init() {
  seed();
  render();
  document.addEventListener('click', onActClick);
  document.addEventListener('input', onInput);
  document.addEventListener('change', onChange);
  const dlg = qs('#dlg');
  if (dlg) dlg.addEventListener('close', onDialogClosed);
  const roleSel = qs('#role-select');
  if (roleSel) roleSel.addEventListener('change', () => {
    state.role = roleSel.value;
    /* 只读视角不能停留在编辑相关页面 */
    if (state.role === 'reader') {
      resetFlows();
      if (state.route.name === 'capture' || state.route.name === 'update') { state.route = { name: 'topics', id: null, step: 1 }; }
      if (state.route.name === 'topic') { const t = topicById(state.route.id); if (t) { state.backStack = []; } }
    }
    render();
    if (state.role === 'reader') toast('已切换为「仅阅读成员」演示视角：编辑、发布与添加入口隐藏（界面差异演示，不代表后端权限）。');
  });
  const menuBtn = qs('#menu-btn');
  if (menuBtn) menuBtn.addEventListener('click', () => {
    const open = !document.body.classList.contains('nav-open');
    document.body.classList.toggle('nav-open', open);
    menuBtn.setAttribute('aria-expanded', open ? 'true' : 'false');
    const s = qs('#side-scrim'); if (s) s.hidden = !open;
  });
  const scrim = qs('#side-scrim');
  if (scrim) scrim.addEventListener('click', () => closeMobileNav(false));
  document.addEventListener('keydown', (e) => {
    /* 抽屉打开时 Escape 收起并把焦点还给菜单按钮；对话框的 Escape 由原生 dialog 处理 */
    if (e.key === 'Escape' && document.body.classList.contains('nav-open')) closeMobileNav(true);
  });
  const crumb = qs('#crumb');
  if (crumb) crumb.innerHTML = crumbHTML();
}
function closeMobileNav(focusMenu) {
  /* 仅影响移动抽屉：桌面无 nav-open，全部为无副作用操作；Escape 收起时把焦点还给菜单按钮 */
  const wasOpen = document.body.classList.contains('nav-open');
  document.body.classList.remove('nav-open');
  const b = qs('#menu-btn'); if (b) b.setAttribute('aria-expanded', 'false');
  const s = qs('#side-scrim'); if (s) s.hidden = true;
  if (wasOpen && focusMenu) { const mb = qs('#menu-btn'); if (mb && mb.focus) mb.focus(); }
}
init();
