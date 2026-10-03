const rfqSection = document.getElementById('product-rfq');
if (rfqSection) {
  const siteKey = rfqSection.dataset.siteKey;
  const productId = rfqSection.dataset.productId;
  const form = document.getElementById('product-rfq-form');
  const result = document.getElementById('product-rfq-result');
  const sessionStatus = document.getElementById('product-rfq-session');
  const sessionStorageKey = `pump_reference_session_${siteKey}`;
  // 失败交接的隔离状态只存 sessionStorage（标签页级），随标签页关闭而消失。
  const handoffFailedKey = `pump_product_handoff_failed_${siteKey}`;
  const isolatedSessionKey = `pump_product_handoff_session_${siteKey}`;
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

  async function startSession(storage, key) {
    const created = await request('/conversations', 'POST', { site_key: siteKey, locale: document.documentElement.lang === 'zh-CN' ? 'zh' : 'en' });
    const session = { conversationId: created.conversation_id, token: created.access_token };
    storage.setItem(key, JSON.stringify(session));
    return session;
  }

  // 无码直访行为不变：优先复用本浏览器已保存的会话，否则提交时新建并保存。
  async function persistedSession() {
    const saved = JSON.parse(localStorage.getItem(sessionStorageKey) || 'null');
    if (saved?.conversationId && saved?.token) {
      await request(`/conversations/${saved.conversationId}`, 'GET', null, saved.token);
      return saved;
    }
    return startSession(localStorage, sessionStorageKey);
  }

  // 失败交接后的独立会话：只读写本标签页 sessionStorage，绝不读取 localStorage 或
  // sessionStorage 中更早已保存的其他会话；并存标签页看不到它，也就不会把各自的
  // 询价误写进这个新会话。
  async function isolatedSession() {
    const saved = JSON.parse(sessionStorage.getItem(isolatedSessionKey) || 'null');
    if (saved?.conversationId && saved?.token) {
      await request(`/conversations/${saved.conversationId}`, 'GET', null, saved.token);
      return saved;
    }
    return startSession(sessionStorage, isolatedSessionKey);
  }

  let handoffState = 'direct'; // 'direct' | 'connected' | 'failed'
  function buyerSession() {
    return handoffState === 'failed' ? isolatedSession() : persistedSession();
  }

  function handoffFailureMessage(code) {
    const en = { handoff_not_found: 'the link is not recognised', handoff_already_used: 'the link has already been used', handoff_expired: 'the link has expired', handoff_binding_mismatch: 'the link does not match this product page', origin_not_allowed: 'the link is not valid at this site', site_not_found: 'the link is not valid at this site' };
    const zh = { handoff_not_found: '链接无效或不存在', handoff_already_used: '链接已被使用', handoff_expired: '链接已过期', handoff_binding_mismatch: '链接与当前产品页不匹配', origin_not_allowed: '链接在此站点无效', site_not_found: '链接在此站点无效' };
    return `This handoff link could not reconnect the original conversation (${en[code] || code}). Requests sent from this page will start a new, independent conversation. / 此交接链接无法接回原会话（${zh[code] || code}）。在本页提交的询价将建立独立的新会话。`;
  }

  // 跨站交接先于一切，三种情形（完整语义见 docs/MVP-数据与接口契约.md §3.1）：
  // - 成功：接回原会话——凭据写入 localStorage（同源部署与既有行为兼容），页内 RFQ 并入原会话；
  // - 有码但兑换失败：本标签页按独立新访客处理——不读取、不复用任何已存会话，提交时建立
  //   独立新会话（只存本标签页 sessionStorage）；失败标记只作用于本标签页，刷新不失效；
  // - 无码直访：保持既有行为（优先复用已保存的会话，否则提交时新建并保存到 localStorage）。
  // 交接码只经 URL fragment 传递（不发送给服务器、不进 Referer），读取后立即从地址栏清除；
  // 长期会话凭据只写入浏览器本站存储，绝不出现在 URL、日志或页面 HTML 中。
  const fragment = new URLSearchParams(location.hash.startsWith('#') ? location.hash.slice(1) : location.hash);
  const handoffCode = fragment.get('handoff') || '';
  if (handoffCode) history.replaceState(null, '', location.pathname + location.search);

  const ready = (async () => {
    if (handoffCode) {
      try {
        const exchanged = await request('/handoff/exchange', 'POST', { code: handoffCode, product_id: productId });
        localStorage.setItem(sessionStorageKey, JSON.stringify({ conversationId: exchanged.conversation_id, token: exchanged.access_token }));
        sessionStorage.removeItem(handoffFailedKey);
        handoffState = 'connected';
        status('Connected to your original conversation. / 已连接原会话。');
      } catch (error) {
        handoffState = 'failed';
        sessionStorage.setItem(handoffFailedKey, '1');
        status(handoffFailureMessage(error.message));
      }
      return;
    }
    if (sessionStorage.getItem(handoffFailedKey) === '1') {
      handoffState = 'failed';
      status('This tab continues as a new visitor: the earlier handoff link could not reconnect the original conversation. Requests sent from this page start a new, independent conversation. / 本标签页继续以新访客处理：此前的交接链接未能接回原会话，在本页提交的询价将建立独立的新会话。');
    }
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
      result.textContent = handoffState === 'failed'
        ? `RFQ ${saved.rfq_id.slice(0, 8)} received as a new, independent conversation. / 询价已提交（独立新会话）。`
        : `RFQ ${saved.rfq_id.slice(0, 8)} received. The seller can review it with your existing conversation. / 询价已提交。`;
    } catch (error) {
      result.textContent = `The RFQ was not confirmed: ${error.message}. Your entries remain here. / 提交未确认，请核对后重试。`;
      submit.disabled = false;
    }
  });
}
