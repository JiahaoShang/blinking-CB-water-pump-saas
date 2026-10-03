const rfqSection = document.getElementById('product-rfq');
if (rfqSection) {
  const siteKey = rfqSection.dataset.siteKey;
  const productId = rfqSection.dataset.productId;
  const form = document.getElementById('product-rfq-form');
  const result = document.getElementById('product-rfq-result');
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
