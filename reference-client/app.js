const $ = (id) => document.getElementById(id);
const log = $('chat-log');
const notice = $('chat-notice');
// 产品详情页上下文：由服务端注入 window.PUMP_CHAT_CONTEXT = {siteKey, productId, model}。
// 有上下文时：聊天窗与页面询价表单保持同一会话归属语义；发出即自动连接；询价默认针对当前型号。
const chatContext = (typeof window !== 'undefined' && window.PUMP_CHAT_CONTEXT) || null;
let siteKey = (chatContext && chatContext.siteKey) || new URLSearchParams(location.search).get('site_key') || localStorage.getItem('pump_reference_site_key') || '';
// 跨来源部署时，聊天页在企业官网来源、API/产品页在平台来源：用 ?api_base=https://platform.example
// 指定平台来源；不填则与页面同源（本地参考演示）。产品链接也指向该来源。
// 不落盘保存：api_base 属于页面级配置，避免一次跨来源演示影响后续同来源打开。
let apiBase = (new URLSearchParams(location.search).get('api_base') || '').trim().replace(/\/+$/, '');
if (apiBase && !/^https?:\/\/[^/\s]+$/i.test(apiBase)) apiBase = '';
let conversationId = '';
let token = '';
let current = null;
function systemPrefersChinese() {
  try {
    const languages = navigator.languages && navigator.languages.length ? navigator.languages : [navigator.language || 'en'];
    return languages.some((value) => String(value || '').toLowerCase().startsWith('zh'));
  } catch (error) { return false; }
}
// 首次打开按系统（浏览器）语言选择中英文；仍可用右上角按钮随时切换。
let locale = systemPrefersChinese() ? 'zh' : 'en';
let pendingMessage = null;
let pendingQuestion = null;
$('site-key').value = siteKey;
// 接管站点的聊天窗：隐藏「Website key / 连接」配置行（连接由页面上下文自动完成）。
if (chatContext) {
  const configRow = $('site-key').parentElement && $('site-key').parentElement.parentElement;
  if (configRow) configRow.hidden = true;
}

const words = {
  en: { connect: 'Connect', assistantTitle: 'Pump product assistant', assistantSubtitle: 'Technical information with sources', stopContact: 'Stop proactive contact', quote: 'Request a quote', send: 'Send', needSite: 'Enter the website key from the seller workspace.', noSession: 'Connect before sending a message.', confirmed: 'Confirmed requirements', draft: 'Check these extracted values before matching', unknown: 'Unknown', confirm: 'Confirm requirements', find: 'Find products', noMatch: 'No reliable candidate. Ask an engineer to review your application.', ask: 'Product answer', greeting: "Hello! I'm your consulting assistant — feel free to ask me anything about our products.", placeholder: 'Describe your requirements (e.g. flow 3 m³/h, head 45 m) — or just ask a question', requestEngineer: 'Ask an engineer to review', askEngineer: 'Would you like an engineer to verify this?', engineerRequested: 'Engineer review requested — the reply will appear right here.', replyHint: 'Reply using the message box below.', launcher: 'Ask me about products', rfq: 'RFQ', edit: 'Edit this RFQ', save: 'Submit RFQ', saveEdit: 'Save changes', contact: 'Reply email or phone', confirmContact: 'I confirm this reply contact can be used for this request', quantity: 'Quantity', model: 'Product model', destination: 'Destination', notes: 'Requirements and notes', error: 'The request did not complete. Your entries remain here; review the message and retry.', noDraft: 'Your message was saved, but the requirements were not extracted yet. Send again to retry — your text is kept.' },
  zh: { connect: '连接', assistantTitle: '水泵产品助手', assistantSubtitle: '带出处的技术信息', stopContact: '停止主动联系', quote: '发起询价', send: '发送', needSite: '请输入卖家系统提供的网站标识。', noSession: '请先连接，再发送消息。', confirmed: '已确认工况', draft: '请核对提取的工况后再选型', unknown: '不确定', confirm: '确认工况', find: '查找产品', noMatch: '暂无可靠候选，可请求工程师协助核实。', ask: '产品问答', greeting: '你好，我是您的咨询助手——您可以随时向我提出任何关于产品的问题。', placeholder: '描述工况（例如：流量 3 m³/h，扬程 45 m），或直接提问', requestEngineer: '请求工程师核实', askEngineer: '需要工程师帮您核实吗？', engineerRequested: '已转交工程师核实——答复会直接出现在这里。', replyHint: '请在下方输入框直接回复', launcher: '向我提问产品问题', rfq: '询价', edit: '编辑原 RFQ', save: '提交询价', saveEdit: '保存修改', contact: '可回复邮箱或电话', confirmContact: '我确认销售可通过上述方式回复本次请求', quantity: '数量', model: '产品型号', destination: '目的地', notes: '需求说明', error: '操作未完成。输入内容仍保留，请查看提示后重试。', noDraft: '消息已保存，但需求提取尚未完成；请再次发送重试，输入内容已保留。' },
};
const t = (key) => words[locale][key];
function node(tag, className = '', value = '') { const n = document.createElement(tag); if (className) n.className = className; if (value) n.textContent = value; return n; }
function showNotice(message = '') { notice.textContent = message; }
function sessionKey() { return `pump_reference_session_${siteKey}`; }
function readStored(store, key) {
  try { return store ? JSON.parse(store.getItem(key) || 'null') : null; } catch (error) { return null; }
}
function tabStorage() {
  try { return typeof sessionStorage === 'undefined' ? null : sessionStorage; } catch (error) { return null; }
}
// 产品页上下文下的会话归属（与 product-page.js 同语义）：交接成功 → 本标签页固定凭据；
// 交接失败 → 本标签页独立会话（不回落共享存储）；否则用浏览器共享存储。
function preferredSession() {
  if (chatContext && chatContext.siteKey) {
    const key = chatContext.siteKey;
    const connected = readStored(tabStorage(), `pump_product_handoff_connected_${key}`);
    if (connected && connected.conversationId && connected.token) return { session: connected, store: null, storeKey: '' };
    if (tabStorage() && tabStorage().getItem(`pump_product_handoff_failed_${key}`) === '1') {
      return { session: readStored(tabStorage(), `pump_product_handoff_session_${key}`), store: tabStorage(), storeKey: `pump_product_handoff_session_${key}` };
    }
  }
  return { session: readStored(localStorage, sessionKey()), store: localStorage, storeKey: sessionKey() };
}
function appendCard(title) { const card = node('section', 'card'); card.append(node('h3', '', title)); log.append(card); return card; }
function field(form, label, name, value = '', type = 'text') {
  const wrapper = node('label'); wrapper.append(node('span', '', label));
  const input = node('input'); input.name = name; input.type = type; input.value = value || ''; wrapper.append(input); form.append(wrapper); return input;
}
function action(buttonText, onClick, secondary = false) { const button = node('button', secondary ? 'secondary' : '', buttonText); button.type = 'button'; button.addEventListener('click', onClick); return button; }
async function api(path, method = 'GET', data = null, extra = {}) {
  const response = await fetch(`${apiBase}/api/v1/buyer${path}`, {
    method,
    headers: { 'Content-Type': 'application/json', 'X-Site-Key': siteKey, ...(token ? { Authorization: `Bearer ${token}` } : {}), ...extra },
    body: data === null ? undefined : JSON.stringify(data),
  });
  const body = await response.json();
  if (!response.ok) throw new Error(body.message || body.error || `HTTP ${response.status}`);
  return body;
}
async function connect() {
  siteKey = $('site-key').value.trim();
  if (!siteKey) { showNotice(t('needSite')); return; }
  localStorage.setItem('pump_reference_site_key', siteKey);
  const preferred = preferredSession();
  const saved = preferred.session;
  if (saved?.conversationId && saved?.token) {
    conversationId = saved.conversationId;
    token = saved.token;
    try { await refresh(); startPolling(); return; } catch (error) { showNotice(error.message); return; }
  }
  try {
    const started = await api('/conversations', 'POST', { site_key: siteKey, locale });
    conversationId = started.conversation_id;
    token = started.access_token;
    (preferred.store || localStorage).setItem(preferred.storeKey || sessionKey(), JSON.stringify({ conversationId, token }));
    await refresh();
    startPolling();
  } catch (error) { showNotice(error.message); }
}
async function refresh() {
  if (!conversationId) { showNotice(t('noSession')); return; }
  current = await api(`/conversations/${conversationId}`);
  for (const message of current.messages || []) seenMessageIds.add(message.id);
  lastFingerprint = conversationFingerprint(current);
  showNotice('');
  render();
}
// 自动同步：像正常聊天一样，新回复在数秒内自动出现（无需手动刷新）。
// 参考实现用轻量轮询（3 秒；真实站点可换 SSE/WebSocket）。窗口收起时改为在启动按钮上显示未读角标。
const POLL_INTERVAL_MS = 3000;
let pollTimer = null;
let lastFingerprint = '';
let unreadCount = 0;
let seenMessageIds = new Set();
let taskReplyInFlight = false;
function conversationFingerprint(data) {
  return JSON.stringify([
    (data.messages || []).map((m) => m.id).join(','),
    (data.answers || []).map((a) => `${a.id}:${a.status}`).join(','),
    (data.human_tasks || []).map((t) => `${t.id}:${t.status}`).join(','),
    (data.rfqs || []).map((r) => `${r.id}:${r.current_revision}`).join(','),
    data.requirement?.revision_id || '',
    data.recommendation?.recommendation_id || '',
  ]);
}
function setBadge(count) {
  unreadCount = count;
  const badge = $('chat-badge');
  badge.hidden = count <= 0;
  badge.textContent = count > 9 ? '9+' : String(count);
}
function renderIfIdle() {
  // 聊天区内有未提交的输入（如询价表单）时先不改动界面，下一轮轮询再试，避免清掉正在填写的内容。
  const busy = [...log.querySelectorAll('input,textarea')].some((el) => String(el.value || '').trim());
  if (busy) return false;
  render();
  return true;
}
async function poll() {
  if (!conversationId) return;
  try {
    const data = await api(`/conversations/${conversationId}`);
    const fingerprint = conversationFingerprint(data);
    if (fingerprint === lastFingerprint) return;
    const fresh = (data.messages || []).filter((message) => !seenMessageIds.has(message.id));
    for (const message of data.messages || []) seenMessageIds.add(message.id);
    const incoming = fresh.filter((message) => message.role !== 'buyer').length;
    current = data;
    if ($('chat-panel').hidden) {
      lastFingerprint = fingerprint;
      if (incoming) setBadge(unreadCount + incoming);
    } else if (renderIfIdle()) {
      lastFingerprint = fingerprint;
    }
  } catch (error) { /* 轮询失败静默重试；显式操作仍会给出错误提示。 */ }
}
function startPolling() {
  // 极简测试桩可能不提供定时器：缺省时跳过轮询，不影响其余功能。
  if (!pollTimer && typeof setInterval === 'function') pollTimer = setInterval(poll, POLL_INTERVAL_MS);
}
// 打开窗口即见的欢迎语：永远作为对话的第一条消息（语言跟随系统判定，可切换）。
function appendGreeting() {
  log.append(node('div', 'bubble assistant', t('greeting')));
}
// 统一输入框的分流：带问号或典型疑问词的消息按「产品提问」（有据问答）处理，
// 其余按「需求描述」交给需求识别。规则刻意保守——宁可多走需求识别，也不误吞工况描述。
function looksLikeQuestion(text) {
  const value = String(text).trim();
  if (!value) return false;
  if (/[?？]/.test(value)) return true;
  if (/^(what|which|how|can|could|do|does|is|are|will|would|should|why|when|where|who|may)\b/i.test(value)) return true;
  if (/(请问|问一下|想问|咨询一下|是否|能否|能不能|可不可以|如何|怎么|多少|多大|什么型号|哪种|哪款|哪一个)/.test(value)) return true;
  return false;
}
function applyLocale() {
  $('chat-language').textContent = locale === 'en' ? '中文' : 'EN';
  $('connect').textContent = t('connect');
  $('show-rfq').textContent = t('quote');
  $('stop-contact').textContent = t('stopContact');
  $('chat-title').textContent = t('assistantTitle');
  $('chat-subtitle').textContent = t('assistantSubtitle');
  $('chat-launcher-label').textContent = t('launcher');
  $('message-form').querySelector('button').textContent = t('send');
  $('message').placeholder = t('placeholder');
}
function render() {
  log.replaceChildren();
  appendGreeting();
  if (!current) return;
  $('stop-contact').hidden = Boolean(current.contact_preference?.stop_proactive_contact);
  if (current.contact_preference?.stop_proactive_contact) {
    const banner = appendCard(locale === 'en' ? 'Proactive contact stopped' : '已停止主动联系');
    banner.append(node('p', 'muted', current.contact_preference.reason || ''));
  }
  for (const message of current.messages || []) {
    // 有据回答由下方答案卡完整呈现（含出处），不再以气泡重复；
    // 待核实回答保留气泡（一句「不能确认」的陈述），「请求工程师」入口只在尚无任务时出现一次。
    if (message.source === 'grounded-answer') continue;
    let localized = message.source?.startsWith('human-request') ? t('engineerRequested') : message.content;
    // 工程师回复消息自带「工程师回复：」前缀（供旧页面直接消费）；参考窗已用标签呈现，去掉前缀避免重复。
    if (message.source === 'human-engineer') localized = localized.replace(/^工程师回复：/, '');
    // 卖家回复消息自带「卖家回复：」前缀（供旧页面直接消费）；参考窗已用标签呈现，去掉前缀避免重复。
    if (message.source === 'staff-reply') localized = localized.replace(/^卖家回复：/, '');
    const bubble = node('div', `bubble ${message.role === 'buyer' ? 'buyer' : 'assistant'}`, localized);
    if (message.source === 'human-engineer') bubble.prepend(node('small', '', locale === 'en' ? 'Engineer reply' : '工程师回复'));
    if (message.source === 'staff-reply') bubble.prepend(node('small', '', locale === 'en' ? 'Seller reply' : '卖家回复'));
    if (message.source?.startsWith('engineer-followup:')) {
      bubble.prepend(node('small', '', locale === 'en' ? 'Engineer question' : '工程师追问'));
      const task = (current.human_tasks || []).find((item) => item.id === message.source.split(':')[1]);
      // 仅当仍在等待买家补充时，在追问气泡下方提示用输入框回复；回复后提示自动消失。
      if (task && task.status === 'waiting_buyer') bubble.append(node('small', '', t('replyHint')));
    }
    log.append(bubble);
  }
  renderRequirements(current.requirement);
  renderRecommendation(current.recommendation, current.requirement);
  renderAnswers(current.answers || [], current.human_tasks || []);
  renderRfqs(current.rfqs || []);
  log.scrollTop = log.scrollHeight;
}
function renderRequirements(requirement) {
  if (!requirement) return;
  const card = appendCard(requirement.status === 'confirmed' ? t('confirmed') : t('draft'));
  const form = node('form');
  const names = { flow: 'Flow (m³/h) / 流量', head: 'Head (m) / 扬程', media: 'Medium / 介质' };
  const inputs = {};
  for (const [key, label] of Object.entries(names)) {
    const row = node('div', 'grid');
    inputs[key] = field(row, label, key, requirement.values[key]?.value || '');
    const unknown = field(row, t('unknown'), `${key}_unknown`, '', 'checkbox');
    unknown.checked = requirement.values[key]?.state === 'unknown';
    unknown.addEventListener('change', () => { if (unknown.checked) inputs[key].value = ''; });
    form.append(row);
  }
  const submit = node('button', '', t('confirm')); submit.type = 'submit'; form.append(submit);
  let pendingConfirm = null;
  form.addEventListener('submit', async (event) => {
    event.preventDefault();
    const values = structuredClone(requirement.values || {});
    for (const [key, input] of Object.entries(inputs)) {
      const unknown = form.elements[`${key}_unknown`].checked;
      values[key] = { value: unknown ? '' : input.value.trim(), unit: key === 'flow' ? 'm³/h' : key === 'head' ? 'm' : '', label: names[key] };
    }
    const payload = { base_revision_id: requirement.revision_id, values };
    const signature = JSON.stringify(payload);
    if (!pendingConfirm || pendingConfirm.signature !== signature) pendingConfirm = { key: crypto.randomUUID(), signature };
    try { await api(`/conversations/${conversationId}/requirements/confirm`, 'POST', payload, { 'Idempotency-Key': pendingConfirm.key }); pendingConfirm = null; await refresh(); }
    catch (error) { showNotice(error.message); }
  });
  card.append(form);
}
function renderRecommendation(recommendation, requirement) {
  if (requirement?.status === 'confirmed' && !recommendation) {
    const card = appendCard(t('find'));
    let findRequestKey = null;
    card.append(action(t('find'), async () => {
      findRequestKey = findRequestKey || crypto.randomUUID();
      try { await api(`/conversations/${conversationId}/recommendations`, 'POST', { revision_id: requirement.revision_id }, { 'Idempotency-Key': findRequestKey }); findRequestKey = null; await refresh(); }
      catch (error) { showNotice(error.message); }
    }));
  }
  if (!recommendation) return;
  const statusLabels = {
    recommended: '可初步推荐 / Recommended',
    needs_confirmation: '条件不足或需核实 / Needs review',
    insufficient: '条件不足 / More information needed',
    no_match: '无可靠候选 / No reliable candidate',
  };
  const card = appendCard(`Product match · ${statusLabels[recommendation.status] || recommendation.status}`);
  const list = node('div');
  const productLabel = (status) => status === 'candidate'
    ? (locale === 'zh' ? '可初步推荐（有依据）' : 'Suggested (with evidence)')
    : (locale === 'zh' ? '待核实候选（未确认适用）' : 'Tentative candidate — suitability not confirmed');
  const drawProducts = (items) => {
    list.replaceChildren();
    if (!items.length) {
      list.append(node('p', 'warning', recommendation.status === 'insufficient'
        ? (locale === 'zh' ? '条件不足：请补充工况，或请求工程师协助，或查看待核实候选。' : 'Not enough confirmed conditions yet: add details, ask an engineer, or view tentative candidates.')
        : t('noMatch')));
      return;
    }
    for (const product of items) {
      const item = node('p');
      item.append(node('strong', '', `${product.model} · ${productLabel(product.status)}`));
      if (product.satisfies?.length) item.append(node('div', 'muted', (product.satisfies || []).join('; ')));
      if (product.unknown?.length) item.append(node('div', 'muted', `${locale === 'zh' ? '未确认' : 'Unconfirmed'}: ${product.unknown.join('; ')}`));
      const link = node('a', '', 'Open product detail ↗');
      link.href = productUrl(product.detail_url);
      link.target = '_blank'; link.rel = 'noopener';
      link.addEventListener('click', (event) => openProductDetail(event, link, product));
      item.append(link);
      list.append(item);
    }
    if (recommendation.status !== 'recommended') {
      list.append(node('p', 'muted', locale === 'zh'
        ? '以上仅为待核实候选，不表示适用；最终工作点需工程师确认。'
        : 'These are tentative candidates only, not a suitability claim; an engineer must confirm the duty point.'));
    }
  };
  drawProducts(recommendation.results || []);
  if (recommendation.status !== 'recommended') {
    const tentativeShown = (recommendation.results || []).some((item) => item.status === 'needs_confirmation');
    let exploreRequestKey = null;
    const exploreButton = recommendation.options?.tentative_available && !tentativeShown
      ? action(locale === 'zh' ? '查看待核实候选' : 'View tentative candidates', async () => {
          exploreRequestKey = exploreRequestKey || crypto.randomUUID();
          try {
            const body = await api(`/conversations/${conversationId}/recommendations`, 'POST', { revision_id: requirement?.revision_id, include_tentative: true }, { 'Idempotency-Key': exploreRequestKey });
            exploreRequestKey = null;
            recommendation.results = body.results || [];
            drawProducts(recommendation.results);
            exploreButton.remove();
          } catch (error) { showNotice(error.message); }
        }, true)
      : null;
    if (exploreButton) card.append(exploreButton);
    if (recommendation.options?.engineer_help) {
      card.append(action(locale === 'zh' ? '请求工程师协助选型' : 'Ask an engineer to help select', async (event) => {
        event.target.disabled = true;
        try {
          const values = current?.requirement?.values || {};
          const conditionText = Object.entries(values).filter(([, value]) => value && value.value).map(([key, value]) => `${key}=${value.value}${value.unit || ''}`).join(', ') || 'conditions pending';
          const asked = await api(`/conversations/${conversationId}/questions`, 'POST', { product_id: null, question: `Request selection help / 请求工程师协助选型：${conditionText}` });
          await api(`/conversations/${conversationId}/human-requests`, 'POST', { answer_id: asked.answer_id, reply_path: 'conversation' });
          await refresh();
        } catch (error) { event.target.disabled = false; showNotice(error.message); }
      }));
    }
  }
  card.append(list);
}
function productUrl(path) {
  const routed = path.startsWith('/products/') ? `/s/${encodeURIComponent(siteKey)}${path}` : path;
  return `${apiBase}${routed}`;
}
// 点击产品链接：先向平台领取一次性交接码（短时、单次使用、绑定站点与型号），
// 再新标签打开「产品页#handoff=码」；产品页在平台来源上用码换取同一会话的凭据。
// 交接码只进 URL fragment（不发给服务器、不进 Referer）；长期会话凭据不出现在 URL。
// 新标签必须在点击手势内同步打开，等码返回后再跳转；取码失败按普通直访链接打开并提示。
async function openProductDetail(event, link, product) {
  if (!conversationId || !token) return;
  if (event.defaultPrevented || event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
  event.preventDefault();
  const popup = window.open('about:blank', '_blank');
  if (popup) popup.opener = null;
  let target = link.href;
  try {
    const minted = await api(`/conversations/${conversationId}/handoff-codes`, 'POST', { product_id: product.product_id });
    target = `${link.href}#handoff=${encodeURIComponent(minted.code)}`;
  } catch (error) {
    showNotice(locale === 'zh'
      ? `产品链接未能附带会话交接（${error.message}）；已按新访客方式打开产品页，提交询价将新建会话。`
      : `The product link could not carry the conversation handoff (${error.message}); the page opens as a new visitor and a request starts a new conversation.`);
  }
  if (popup) { popup.location.replace(target); return; }
  const fallback = window.open(target, '_blank', 'noopener');
  if (!fallback) showNotice((locale === 'zh' ? '浏览器阻止了新标签页，请手动打开：' : 'The browser blocked the new tab; please open: ') + target);
}
function renderAnswers(answers, tasks) {
  for (const answer of answers) {
    if (answer.status === 'grounded') {
      // 多候选场景下点明回答对应的型号，避免买家把另一个型号的数据当成答案。
      const card = appendCard(answer.model ? `${answer.model} · ${t('ask')}` : t('ask'));
      card.append(node('p', 'muted', answer.text));
      for (const citation of answer.citations || []) card.append(node('small', '', `${citation.label || 'Source'} · ${citation.filename || ''} · ${citation.location || ''}`));
      continue;
    }
    // 待核实回答：正文已在气泡中；卡片只在「还没有任何动作」时出现一次（选择入口）。
    // 一旦转交（或已有任务），卡片不再显示——后续动向由消息流播报（已转交状态、工程师追问与答复）。
    const task = tasks.find((item) => item.question_message_id === answer.question_message_id);
    if (task) continue;
    const card = appendCard(t('askEngineer'));
    card.append(action(t('requestEngineer'), async () => {
      try { await api(`/conversations/${conversationId}/human-requests`, 'POST', { answer_id: answer.id, reply_path: 'conversation' }); await refresh(); }
      catch (error) { showNotice(error.message); }
    }));
  }
}
function renderRfqs(rfqs) {
  for (const rfq of rfqs) {
    const card = appendCard(`${t('rfq')} · ${rfq.id.slice(0, 8)} · v${rfq.current_revision}`);
    card.append(node('p', 'muted', `${rfq.quantity || '—'} · ${rfq.model || rfq.requested_model || 'Model pending'} · ${rfq.status}`));
    card.append(action(t('edit'), () => showRfqForm(rfq), true));
  }
}
function showRfqForm(rfq = null) {
  if (!conversationId) { showNotice(t('noSession')); return; }
  const card = appendCard(rfq ? `${t('edit')} · ${rfq.id.slice(0, 8)}` : t('quote'));
  const form = node('form');
  const submissionKey = crypto.randomUUID();
  // 产品型号：产品详情页进入时默认当前型号；其他入口由买家手动填写（带可选项建议）。
  const contextModel = (chatContext && chatContext.model) || '';
  const candidates = current?.recommendation?.results || [];
  const defaultModel = rfq
    ? (rfq.requested_model || rfq.model || contextModel || (candidates[0]?.model || ''))
    : (contextModel || candidates[0]?.model || '');
  const modelField = field(form, t('model'), 'requested_model', defaultModel);
  modelField.placeholder = locale === 'en' ? 'e.g. CB-80A' : '例如：CB-80A';
  const suggestions = [...new Set([contextModel, ...candidates.map((item) => item.model)].filter(Boolean))];
  if (suggestions.length) {
    const modelList = node('datalist');
    modelList.id = `rfq-models-${submissionKey.slice(0, 8)}`;
    for (const value of suggestions) { const option = node('option'); option.value = value; modelList.append(option); }
    modelField.setAttribute('list', modelList.id);
    form.append(modelList);
  }
  // V1-01 企业设置：字段三态（必填/选填/隐藏）随会话载荷 rfq_fields 下发；缺失时全部可见，交由服务端校验兜底。
  const rfqFields = (current && current.rfq_fields) || { email: 'optional', phone: 'optional', quantity: 'optional', destination: 'optional', notes: 'optional' };
  const rfqField = (key, label, name, value, type) => {
    if (rfqFields[key] === 'hidden') return null;
    return field(form, `${label}${rfqFields[key] === 'required' ? ' *' : ''}`, name, value, type);
  };
  const email = rfqField('email', 'Email', 'contact_email', rfq?.contact_email || '', 'email');
  const phone = rfqField('phone', 'Phone', 'contact_phone', rfq?.contact_phone || '', 'tel');
  const quantity = rfqField('quantity', t('quantity'), 'quantity', rfq?.quantity || '');
  const destination = rfqField('destination', t('destination'), 'destination', rfq?.destination || '');
  const notes = rfqField('notes', t('notes'), 'notes', rfq?.notes || '');
  const confirmed = field(form, t('confirmContact'), 'contact_confirmed', '', 'checkbox');
  const submit = node('button', '', rfq ? t('saveEdit') : t('save')); submit.type = 'submit'; form.append(submit);
  form.addEventListener('submit', async (event) => {
    event.preventDefault();
    const missingFields = [['email', email, 'Email'], ['phone', phone, 'Phone'], ['quantity', quantity, t('quantity')], ['destination', destination, t('destination')], ['notes', notes, t('notes')]]
      .filter(([key, input]) => rfqFields[key] === 'required' && input && !input.value.trim())
      .map(([, , label]) => label);
    if (missingFields.length) { showNotice((locale === 'zh' ? '请填写：' : 'Please fill: ') + missingFields.join(locale === 'zh' ? '、' : ', ')); return; }
    if (!confirmed.checked) { showNotice(t('contact')); return; }
    submit.disabled = true;
    try {
      // 型号解析：与页面上下文 / 当前推荐候选 / 原绑定型号匹配则绑定 product_id；否则只记文字。
      const modelText = modelField.value.trim();
      const normalizedModel = modelText.toLowerCase();
      let boundProductId = null;
      if (modelText) {
        if (chatContext && chatContext.productId && String(chatContext.model || '').trim().toLowerCase() === normalizedModel) boundProductId = chatContext.productId;
        if (!boundProductId) {
          const hit = candidates.find((item) => String(item.model || '').trim().toLowerCase() === normalizedModel);
          if (hit) boundProductId = hit.product_id;
        }
        if (!boundProductId && rfq && rfq.product_id && String(rfq.model || '').trim().toLowerCase() === normalizedModel) boundProductId = rfq.product_id;
      }
      const saved = rfq
        ? await api(`/conversations/${conversationId}/rfqs/${rfq.id}/revisions`, 'POST', { expected_revision: rfq.current_revision, contact_confirmed: true, contact: { contact_email: email ? email.value.trim() : (rfq.contact_email || ''), contact_phone: phone ? phone.value.trim() : (rfq.contact_phone || '') }, quantity: quantity ? quantity.value.trim() : (rfq.quantity || ''), destination: destination ? destination.value.trim() : (rfq.destination || ''), notes: notes ? notes.value.trim() : (rfq.notes || ''), product_id: boundProductId, requested_model: modelText }, { 'Idempotency-Key': submissionKey })
        : await api(`/conversations/${conversationId}/rfqs`, 'POST', { entry_surface: 'chat', contact_confirmed: true, product_id: boundProductId, requested_model: modelText, contact: { contact_email: email ? email.value.trim() : '', contact_phone: phone ? phone.value.trim() : '', quantity: quantity ? quantity.value.trim() : '', region: destination ? destination.value.trim() : '' }, notes: notes ? notes.value.trim() : '' }, { 'Idempotency-Key': submissionKey });
      await refresh();
      if (!rfq && saved?.related_rfq_hint?.length) {
        showNotice((locale === 'zh' ? '本会话 24 小时内已有同型号询价（不会自动合并，请检查是否相关）：' : 'A same-model RFQ from this conversation exists within 24h (no auto-merge; please check if related): ')
          + saved.related_rfq_hint.map((item) => `${item.rfq_id.slice(0, 8)} · v${item.current_revision}`).join('；'));
      }
    } catch (error) { submit.disabled = false; showNotice(error.message || t('error')); }
  });
  card.append(form); log.scrollTop = log.scrollHeight;
}

$('chat-launcher').addEventListener('click', () => {
  $('chat-panel').hidden = false;
  $('chat-launcher').setAttribute('aria-expanded', 'true');
  setBadge(0);
  // 产品详情页上下文：打开即自动连接（共享存储已有会话时直接续接）。
  if (conversationId) refresh().catch((error) => showNotice(error.message));
  else if (chatContext) connect();
  else $('site-key').focus();
});
$('chat-close').addEventListener('click', () => { $('chat-panel').hidden = true; $('chat-launcher').setAttribute('aria-expanded', 'false'); });
$('connect').addEventListener('click', connect);
$('show-rfq').addEventListener('click', () => showRfqForm());
$('stop-contact').addEventListener('click', () => {
  if (!conversationId) { showNotice(t('noSession')); return; }
  const card = appendCard(locale === 'en' ? 'Stop proactive contact' : '停止主动联系');
  const form = node('form');
  const reason = node('textarea'); reason.required = true; reason.rows = 2; reason.value = locale === 'en' ? 'Please do not contact me proactively about this inquiry.' : '请不要再主动联系我讨论本次询价。'; form.append(reason);
  const submit = node('button', '', locale === 'en' ? 'Confirm stop request' : '确认停止主动联系'); submit.type = 'submit'; form.append(submit);
  form.addEventListener('submit', async (event) => {
    event.preventDefault();
    try { await api(`/conversations/${conversationId}/contact-preference`, 'POST', { stop_proactive_contact: true, reason: reason.value.trim() }); await refresh(); }
    catch (error) { showNotice(error.message); }
  });
  card.append(form); log.scrollTop = log.scrollHeight;
});
$('message-form').addEventListener('submit', async (event) => {
  event.preventDefault();
  if (!conversationId) { showNotice(t('noSession')); return; }
  const value = $('message').value.trim(); if (!value) return;
  // 统一输入框分流（优先级从高到低）：
  // ① 有工程师追问待回复 → 作为对工程师的回复（保持同一条对话线，避免买家补充内容误入其他管线）；
  // ② 像提问 → 「有据问答」（自动绑定当前推荐型号）；③ 其余 → 「需求识别」。
  const waitingTask = (current?.human_tasks || []).find((task) => task.status === 'waiting_buyer');
  if (waitingTask) {
    if (taskReplyInFlight) return;
    taskReplyInFlight = true;
    try {
      await api(`/conversations/${conversationId}/human-tasks/${waitingTask.id}/responses`, 'POST', { text: value });
      $('message').value = ''; await refresh();
    } catch (error) { showNotice(error.message); }
    finally { taskReplyInFlight = false; }
    return;
  }
  if (looksLikeQuestion(value)) {
    // 提问的产品归属：产品详情页上下文优先（页面即语境）；否则绑定当前推荐的首个型号。
    const productId = (chatContext && chatContext.productId) || current?.recommendation?.results?.[0]?.product_id || null;
    const payload = { product_id: productId, question: value };
    const signature = JSON.stringify(payload);
    if (!pendingQuestion || pendingQuestion.signature !== signature) pendingQuestion = { key: crypto.randomUUID(), signature };
    try {
      await api(`/conversations/${conversationId}/questions`, 'POST', payload, { 'Idempotency-Key': pendingQuestion.key });
      pendingQuestion = null; $('message').value = ''; await refresh();
    } catch (error) { showNotice(error.message); }
    return;
  }
  if (!pendingMessage || pendingMessage.text !== value) pendingMessage = { key: crypto.randomUUID(), text: value };
  try {
    const saved = await api(`/conversations/${conversationId}/messages`, 'POST', { text: value }, { 'Idempotency-Key': pendingMessage.key });
    // 成功但没有草稿（如旧版服务在提取失败后返回 duplicate/null）：不能当作已处理清空输入，
    // 保留文本与同一提交键，让买家可依提示重试补生成需求草稿。
    if (!saved.requirement_draft) { showNotice(t('noDraft')); return; }
    pendingMessage = null; $('message').value = ''; await refresh();
  }
  catch (error) { showNotice(error.message); }
});
$('chat-language').addEventListener('click', () => { locale = locale === 'en' ? 'zh' : 'en'; applyLocale(); render(); });
// 回到本页时立即同步一次（后台标签页定时器会被浏览器降频，回来时补一次）。
document.addEventListener('visibilitychange', () => { if (!document.hidden && conversationId) poll(); });
// 初次渲染：放置欢迎语，并按系统语言落地界面文案。
applyLocale();
render();
