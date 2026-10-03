const $ = (id) => document.getElementById(id);
const log = $('chat-log');
const notice = $('chat-notice');
let siteKey = new URLSearchParams(location.search).get('site_key') || localStorage.getItem('pump_reference_site_key') || '';
let conversationId = '';
let token = '';
let current = null;
let locale = 'en';
$('site-key').value = siteKey;

const words = {
  en: { connect: 'Connect', quote: 'Request a quote', refresh: 'Refresh replies', send: 'Send', needSite: 'Enter the website key from the seller workspace.', noSession: 'Connect before sending a message.', confirmed: 'Confirmed requirements', draft: 'Check these extracted values before matching', unknown: 'Unknown', confirm: 'Confirm requirements', find: 'Find products', noMatch: 'No reliable candidate. Ask an engineer to review your application.', ask: 'Ask a product question', requestEngineer: 'Ask an engineer to review', rfq: 'RFQ', edit: 'Edit this RFQ', save: 'Submit RFQ', saveEdit: 'Save changes', contact: 'Reply email or phone', confirmContact: 'I confirm this reply contact can be used for this request', quantity: 'Quantity', destination: 'Destination', notes: 'Requirements and notes', error: 'The request did not complete. Your entries remain here; review the message and retry.' },
  zh: { connect: '连接', quote: '发起询价', refresh: '刷新回复', send: '发送', needSite: '请输入卖家系统提供的网站标识。', noSession: '请先连接，再发送消息。', confirmed: '已确认工况', draft: '请核对提取的工况后再选型', unknown: '不确定', confirm: '确认工况', find: '查找产品', noMatch: '暂无可靠候选，可请求工程师协助核实。', ask: '提出产品问题', requestEngineer: '请求工程师核实', rfq: '询价', edit: '编辑原 RFQ', save: '提交询价', saveEdit: '保存修改', contact: '可回复邮箱或电话', confirmContact: '我确认销售可通过上述方式回复本次请求', quantity: '数量', destination: '目的地', notes: '需求说明', error: '操作未完成。输入内容仍保留，请查看提示后重试。' },
};
const t = (key) => words[locale][key];
function node(tag, className = '', value = '') { const n = document.createElement(tag); if (className) n.className = className; if (value) n.textContent = value; return n; }
function showNotice(message = '') { notice.textContent = message; }
function sessionKey() { return `pump_reference_session_${siteKey}`; }
function appendCard(title) { const card = node('section', 'card'); card.append(node('h3', '', title)); log.append(card); return card; }
function field(form, label, name, value = '', type = 'text') {
  const wrapper = node('label'); wrapper.append(node('span', '', label));
  const input = node('input'); input.name = name; input.type = type; input.value = value || ''; wrapper.append(input); form.append(wrapper); return input;
}
function action(buttonText, onClick, secondary = false) { const button = node('button', secondary ? 'secondary' : '', buttonText); button.type = 'button'; button.addEventListener('click', onClick); return button; }
async function api(path, method = 'GET', data = null, extra = {}) {
  const response = await fetch(`/api/v1/buyer${path}`, {
    method,
    headers: { 'Content-Type': 'application/json', 'X-Site-Key': siteKey, ...(token ? { Authorization: `Bearer ${token}` } : {}), ...extra },
    body: data === null ? undefined : JSON.stringify(data),
  });
  const body = await response.json();
  if (!response.ok) throw new Error(body.error || `HTTP ${response.status}`);
  return body;
}
async function connect() {
  siteKey = $('site-key').value.trim();
  if (!siteKey) { showNotice(t('needSite')); return; }
  localStorage.setItem('pump_reference_site_key', siteKey);
  const saved = JSON.parse(localStorage.getItem(sessionKey()) || 'null');
  if (saved?.conversationId && saved?.token) {
    conversationId = saved.conversationId;
    token = saved.token;
    try { await refresh(); return; } catch (error) { showNotice(error.message); return; }
  }
  try {
    const started = await api('/conversations', 'POST', { site_key: siteKey, locale });
    conversationId = started.conversation_id;
    token = started.access_token;
    localStorage.setItem(sessionKey(), JSON.stringify({ conversationId, token }));
    await refresh();
  } catch (error) { showNotice(error.message); }
}
async function refresh() {
  if (!conversationId) { showNotice(t('noSession')); return; }
  current = await api(`/conversations/${conversationId}`);
  showNotice('');
  render();
}
function render() {
  log.replaceChildren();
  if (!current) return;
  for (const message of current.messages || []) {
    const bubble = node('div', `bubble ${message.role === 'buyer' ? 'buyer' : 'assistant'}`, message.content);
    if (message.source === 'human-engineer') bubble.prepend(node('small', '', locale === 'en' ? 'Engineer reply' : '工程师回复'));
    if (message.source?.startsWith('engineer-followup:')) bubble.prepend(node('small', '', locale === 'en' ? 'Engineer question' : '工程师追问'));
    log.append(bubble);
  }
  renderRequirements(current.requirement);
  renderRecommendation(current.recommendation, current.requirement);
  renderQuestionForm(current.recommendation);
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
  form.addEventListener('submit', async (event) => {
    event.preventDefault();
    const values = structuredClone(requirement.values || {});
    for (const [key, input] of Object.entries(inputs)) {
      const unknown = form.elements[`${key}_unknown`].checked;
      values[key] = { value: unknown ? '' : input.value.trim(), unit: key === 'flow' ? 'm³/h' : key === 'head' ? 'm' : '', label: names[key] };
    }
    try { await api(`/conversations/${conversationId}/requirements/confirm`, 'POST', { base_revision_id: requirement.revision_id, values }); await refresh(); }
    catch (error) { showNotice(error.message); }
  });
  card.append(form);
}
function renderRecommendation(recommendation, requirement) {
  if (requirement?.status === 'confirmed' && !recommendation) {
    const card = appendCard(t('find'));
    card.append(action(t('find'), async () => {
      try { await api(`/conversations/${conversationId}/recommendations`, 'POST', { revision_id: requirement.revision_id }); await refresh(); }
      catch (error) { showNotice(error.message); }
    }));
  }
  if (!recommendation) return;
  const card = appendCard(`Product match · ${recommendation.status}`);
  if (!recommendation.results.length) card.append(node('p', 'warning', t('noMatch')));
  const list = node('div');
  const draw = () => {
    list.replaceChildren();
    for (const product of recommendation.results) {
      const item = node('p');
      item.append(node('strong', '', `${product.model} · ${product.status}`));
      const details = node('div', 'muted', (product.satisfies || []).join('; ') || (product.unknown || []).join('; ')); item.append(details);
      const link = node('a', '', 'Open product detail ↗'); link.href = product.detail_url.startsWith('/products/') ? `/s/${encodeURIComponent(siteKey)}${product.detail_url}` : product.detail_url; link.target = '_blank'; link.rel = 'noopener'; item.append(link);
      list.append(item);
    }
  };
  if (recommendation.status === 'needs_confirmation') card.append(action('View tentative candidates / 查看待核实候选', draw, true));
  else draw();
  card.append(list);
}
function renderAnswers(answers, tasks) {
  for (const answer of answers) {
    const card = appendCard(answer.status === 'grounded' ? t('ask') : t('requestEngineer'));
    card.append(node('p', 'muted', answer.text));
    if (answer.status === 'grounded') {
      for (const citation of answer.citations || []) card.append(node('small', '', `${citation.label || 'Source'} · ${citation.filename || ''} · ${citation.location || ''}`));
      continue;
    }
    const task = tasks.find((item) => item.question_message_id === answer.question_message_id);
    if (task) {
      card.append(node('span', 'pill', task.status));
      if (task.status === 'waiting_buyer') {
        card.append(node('p', '', task.followup_question || 'The engineer needs one more detail.'));
        const form = node('form');
        const reply = node('textarea'); reply.required = true; reply.rows = 2; reply.placeholder = locale === 'en' ? 'Your additional details' : '补充工况'; form.append(reply);
        const submit = node('button', '', locale === 'en' ? 'Reply to engineer' : '回复工程师'); submit.type = 'submit'; form.append(submit);
        form.addEventListener('submit', async (event) => {
          event.preventDefault();
          try { await api(`/conversations/${conversationId}/human-tasks/${task.id}/responses`, 'POST', { text: reply.value.trim() }); await refresh(); }
          catch (error) { showNotice(error.message); }
        });
        card.append(form);
      }
    }
    else card.append(action(t('requestEngineer'), async () => {
      try { await api(`/conversations/${conversationId}/human-requests`, 'POST', { answer_id: answer.id, reply_path: 'conversation' }); await refresh(); }
      catch (error) { showNotice(error.message); }
    }));
  }
}
function renderQuestionForm(recommendation) {
  if (!conversationId) return;
  const card = appendCard(t('ask'));
  const form = node('form');
  const select = node('select'); select.name = 'product_id';
  const empty = node('option', '', 'General question / 一般问题'); empty.value = ''; select.append(empty);
  for (const product of recommendation?.results || []) {
    const option = node('option', '', product.model); option.value = product.product_id; select.append(option);
  }
  const label = node('label'); label.append(node('span', '', 'Product context / 产品')); label.append(select); form.append(label);
  const question = node('textarea'); question.name = 'question'; question.required = true; question.rows = 2; question.placeholder = 'e.g. What is the flow range?'; form.append(question);
  const submit = node('button', '', t('ask')); submit.type = 'submit'; form.append(submit);
  form.addEventListener('submit', async (event) => {
    event.preventDefault();
    try { await api(`/conversations/${conversationId}/questions`, 'POST', { product_id: select.value || null, question: question.value.trim() }); await refresh(); }
    catch (error) { showNotice(error.message); }
  });
  card.append(form);
}
function renderRfqs(rfqs) {
  for (const rfq of rfqs) {
    const card = appendCard(`${t('rfq')} · ${rfq.id.slice(0, 8)} · v${rfq.current_revision}`);
    card.append(node('p', 'muted', `${rfq.quantity || '—'} · ${rfq.model || 'Model pending'} · ${rfq.status}`));
    card.append(action(t('edit'), () => showRfqForm(rfq), true));
  }
}
function showRfqForm(rfq = null) {
  if (!conversationId) { showNotice(t('noSession')); return; }
  const card = appendCard(rfq ? `${t('edit')} · ${rfq.id.slice(0, 8)}` : t('quote'));
  const form = node('form');
  const submissionKey = crypto.randomUUID();
  const email = field(form, 'Email', 'contact_email', rfq?.contact_email || '', 'email');
  const phone = field(form, 'Phone', 'contact_phone', rfq?.contact_phone || '', 'tel');
  const quantity = field(form, t('quantity'), 'quantity', rfq?.quantity || '');
  const destination = field(form, t('destination'), 'destination', rfq?.destination || '');
  const notes = field(form, t('notes'), 'notes', rfq?.notes || '');
  const confirmed = field(form, t('confirmContact'), 'contact_confirmed', '', 'checkbox');
  const submit = node('button', '', rfq ? t('saveEdit') : t('save')); submit.type = 'submit'; form.append(submit);
  form.addEventListener('submit', async (event) => {
    event.preventDefault();
    if (!confirmed.checked || (!email.value.trim() && !phone.value.trim())) { showNotice(t('contact')); return; }
    submit.disabled = true;
    try {
      if (rfq) await api(`/conversations/${conversationId}/rfqs/${rfq.id}/revisions`, 'POST', { expected_revision: rfq.current_revision, contact_confirmed: true, contact: { contact_email: email.value.trim(), contact_phone: phone.value.trim() }, quantity: quantity.value.trim(), destination: destination.value.trim(), notes: notes.value.trim() }, { 'Idempotency-Key': submissionKey });
      else await api(`/conversations/${conversationId}/rfqs`, 'POST', { entry_surface: 'chat', contact_confirmed: true, contact: { contact_email: email.value.trim(), contact_phone: phone.value.trim(), quantity: quantity.value.trim(), region: destination.value.trim() }, notes: notes.value.trim() }, { 'Idempotency-Key': submissionKey });
      await refresh();
    } catch (error) { submit.disabled = false; showNotice(error.message || t('error')); }
  });
  card.append(form); log.scrollTop = log.scrollHeight;
}

$('chat-launcher').addEventListener('click', () => { $('chat-panel').hidden = false; $('chat-launcher').setAttribute('aria-expanded', 'true'); $('site-key').focus(); });
$('chat-close').addEventListener('click', () => { $('chat-panel').hidden = true; $('chat-launcher').setAttribute('aria-expanded', 'false'); });
$('connect').addEventListener('click', connect);
$('refresh').addEventListener('click', () => refresh().catch((error) => showNotice(error.message)));
$('show-rfq').addEventListener('click', () => showRfqForm());
$('message-form').addEventListener('submit', async (event) => {
  event.preventDefault();
  if (!conversationId) { showNotice(t('noSession')); return; }
  const value = $('message').value.trim(); if (!value) return;
  try { await api(`/conversations/${conversationId}/messages`, 'POST', { text: value }); $('message').value = ''; await refresh(); }
  catch (error) { showNotice(error.message); }
});
$('chat-language').addEventListener('click', () => { locale = locale === 'en' ? 'zh' : 'en'; $('chat-language').textContent = locale === 'en' ? '中文' : 'EN'; $('connect').textContent = t('connect'); $('show-rfq').textContent = t('quote'); $('refresh').textContent = t('refresh'); $('message-form').querySelector('button').textContent = t('send'); render(); });
