const rfqSection = document.getElementById('product-rfq');
if (rfqSection) {
  const siteKey = rfqSection.dataset.siteKey;
  const productId = rfqSection.dataset.productId;
  const form = document.getElementById('product-rfq-form');
  const result = document.getElementById('product-rfq-result');
  const sessionStatus = document.getElementById('product-rfq-session');
  const sessionStorageKey = `pump_reference_session_${siteKey}`;
  const submissionStorageKey = `pump_product_rfq_key_${siteKey}_${productId}`;

  async function request(path, method = 'GET', payload = null, token = '', key = '') {
    const response = await fetch(`/api/v1/buyer${path}`, {
      method,
      headers: {
        'Content-Type': 'application/json',
        'X-Site-Key': siteKey,
        ...(token ? { Authorization: `Bearer ${token}` } : {}),
        ...(key ? { 'Idempotency-Key': key } : {}),
      },
      body: payload === null ? undefined : JSON.stringify(payload),
    });
    const body = await response.json();
    if (!response.ok) throw new Error(body.error || `HTTP ${response.status}`);
    return body;
  }

  function status(message) {
    if (sessionStatus) sessionStatus.textContent = message;
  }

  // 官网聊天（另一来源）打开本页时，用一次性交接码接回原会话：交接码只经 URL
  // fragment 传递（不发送给服务器、不进 Referer），读取后立即从地址栏清除；
  // 再在平台自身来源上换取同一会话的凭据。长期会话凭据只写入本来源 localStorage，
  // 绝不出现在 URL、日志或页面 HTML 中。
  const fragment = new URLSearchParams(location.hash.startsWith('#') ? location.hash.slice(1) : location.hash);
  const handoffCode = fragment.get('handoff') || '';
  if (handoffCode) history.replaceState(null, '', location.pathname + location.search);

  async function buyerSession() {
    const saved = JSON.parse(localStorage.getItem(sessionStorageKey) || 'null');
    if (saved?.conversationId && saved?.token) {
      await request(`/conversations/${saved.conversationId}`, 'GET', null, saved.token);
      return saved;
    }
    const created = await request('/conversations', 'POST', { site_key: siteKey, locale: document.documentElement.lang === 'zh-CN' ? 'zh' : 'en' });
    const session = { conversationId: created.conversation_id, token: created.access_token };
    localStorage.setItem(sessionStorageKey, JSON.stringify(session));
    return session;
  }

  // 交接先于一切：成功即持原会话凭据；失败绝不按公开链接、会话 ID 或邮箱关联旧会话，
  // 只显示明确提示并按直访者处理（提交时建立自己的新会话）。
  const ready = (async () => {
    let handoffError = '';
    if (handoffCode) {
      try {
        const exchanged = await request('/handoff/exchange', 'POST', { code: handoffCode, product_id: productId });
        localStorage.setItem(sessionStorageKey, JSON.stringify({ conversationId: exchanged.conversation_id, token: exchanged.access_token }));
        status('Connected to your original conversation. / 已连接原会话。');
        return;
      } catch (error) {
        handoffError = error.message;
      }
    }
    const saved = JSON.parse(localStorage.getItem(sessionStorageKey) || 'null');
    if (saved?.conversationId) {
      if (handoffError) status(`The handoff link is unavailable (${handoffError}); continuing with this browser's saved session. / 交接链接不可用（${handoffError}），已使用本浏览器保存的会话。`);
      return;
    }
    if (handoffError) status(`This link could not reconnect the original conversation (${handoffError}). You can still send a request; the seller will see it as a new conversation. / 未能接回原会话（${handoffError}）；仍可提交询价，卖家将看到新会话。`);
  })();

  form.addEventListener('submit', async (event) => {
    event.preventDefault();
    const values = new FormData(form);
    const email = String(values.get('email') || '').trim();
    const phone = String(values.get('phone') || '').trim();
    if ((!email && !phone) || !values.has('contact_confirmed')) {
      result.textContent = 'Please provide and confirm an email or phone for the seller to reply. / 请提供并确认可回复的联系方式。';
      return;
    }
    const submit = form.querySelector('button[type="submit"]');
    submit.disabled = true;
    result.textContent = 'Submitting…';
    let key = sessionStorage.getItem(submissionStorageKey);
    if (!key) {
      key = crypto.randomUUID();
      sessionStorage.setItem(submissionStorageKey, key);
    }
    try {
      await ready;
      const session = await buyerSession();
      const saved = await request(`/conversations/${session.conversationId}/rfqs`, 'POST', {
        entry_surface: 'product_page',
        product_id: productId,
        contact_confirmed: true,
        contact: {
          contact_email: email,
          contact_phone: phone,
          quantity: String(values.get('quantity') || '').trim(),
          region: String(values.get('destination') || '').trim(),
        },
        notes: String(values.get('notes') || '').trim(),
      }, session.token, key);
      sessionStorage.removeItem(submissionStorageKey);
      result.textContent = `RFQ ${saved.rfq_id.slice(0, 8)} received. The seller can review it with your existing conversation. / 询价已提交。`;
    } catch (error) {
      result.textContent = `The RFQ was not confirmed: ${error.message}. Your entries remain here. / 提交未确认，请核对后重试。`;
      submit.disabled = false;
    }
  });
}
