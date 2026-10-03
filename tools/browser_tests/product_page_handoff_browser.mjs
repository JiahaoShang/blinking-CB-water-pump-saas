// 产品页交接逻辑的“浏览器端”回归驱动（Node，无第三方依赖）。
//
// 用 vm 加载未经修改的 reference-client/product-page.js，配上 localStorage /
// sessionStorage / DOM / fetch 桩，对真实 HTTP 服务按时间顺序驱动八类场景：
//   1. 成功交接 → 页内 RFQ 并入原会话（凭据同时固定到本标签页）
//   2. 已有保存会话 + 无效交接码 → 失败绝不复用旧会话，提交建立独立新会话
//   3. 并存标签页（无码直访）→ 继续使用已保存会话，不写入上面的新会话
//   4. 失败标签页刷新（无码、带失败标记）→ 继续以独立新访客处理，仍用自己的新会话
//   5. 全新浏览器无码直访 → 提交时新建会话并保存（既有直访行为不变）
//   6. 并存有效交接（A 先开、B 后开）→ A 页提交仍归 A，两个标签页互不串单
//   7. 成功交接标签页刷新 → 恢复“已连接”，提交仍归原会话（共享存储被 B 覆盖也不变）
//   8. 失败交接序列 → 同标签页连续失败复用自身独立会话；新标签页/成功后再失败各自隔离
//
// 输出结构化事实（请求日志、存储快照、界面文案）到 result.json，由 Python 测试
// （test_handoff_browser_js.py）对真实数据库/API 逐项断言。
//
// 用法: node product_page_handoff_browser.mjs <config.json> <result.json>
import { readFileSync, writeFileSync } from 'node:fs';
import vm from 'node:vm';

const [configPath, resultPath] = process.argv.slice(2);
if (!configPath || !resultPath) {
  console.error('usage: node product_page_handoff_browser.mjs <config.json> <result.json>');
  process.exit(2);
}
const config = JSON.parse(readFileSync(configPath, 'utf8'));
const source = readFileSync(new URL('../../reference-client/product-page.js', import.meta.url), 'utf8');

function sleep(ms) { return new Promise((resolve) => setTimeout(resolve, ms)); }

function makeStorage() {
  const map = new Map();
  return {
    getItem: (key) => (map.has(String(key)) ? map.get(String(key)) : null),
    setItem: (key, value) => { map.set(String(key), String(value)); },
    removeItem: (key) => { map.delete(String(key)); },
    snapshot: () => Object.fromEntries(map),
  };
}

class SimBrowser {
  constructor() { this.local = makeStorage(); }

  openTab({ url, productId, session }) {
    const journal = [];
    const local = this.local;
    const sessionStorage = session || makeStorage();
    const parsed = new URL(url, config.baseUrl);
    const location = { hash: parsed.hash, pathname: parsed.pathname, search: parsed.search };
    const history = {
      replaceState(_state, _title, target) {
        const next = new URL(target, config.baseUrl);
        location.hash = next.hash;
        location.pathname = next.pathname;
        location.search = next.search;
      },
    };
    const sessionStatus = { textContent: '' };
    const resultStatus = { textContent: '' };
    const submitButton = { disabled: false };
    const form = {
      __values: {},
      _listeners: {},
      addEventListener(type, handler) { form._listeners[type] = handler; },
      querySelector(selector) { return selector === 'button[type="submit"]' ? submitButton : null; },
    };
    const elements = {
      'product-rfq': { dataset: { siteKey: config.siteKey, productId } },
      'product-rfq-form': form,
      'product-rfq-result': resultStatus,
      'product-rfq-session': sessionStatus,
    };
    const fetchImpl = async (input, init = {}) => {
      const raw = String(input);
      const target = raw.startsWith('http') ? raw : config.baseUrl + raw;
      const method = (init.method || 'GET').toUpperCase();
      const headers = { ...(init.headers || {}) };
      // 浏览器对同源 POST 会自动带 Origin；这里显式补上，让服务端来源校验按真实路径生效。
      if (method !== 'GET' && !headers.Origin) headers.Origin = config.baseUrl;
      const response = await fetch(target, { ...init, headers });
      const clone = response.clone();
      const record = { method, url: raw, status: response.status, idempotencyKey: headers['Idempotency-Key'] || null, body: null };
      if (method !== 'GET') {
        try { record.body = await clone.json(); } catch { /* 非 JSON 响应 */ }
      }
      journal.push(record);
      return response;
    };
    class HarnessFormData {
      constructor(target) { this._target = target; }
      get(name) { return name in this._target.__values ? this._target.__values[name] : null; }
      has(name) { return name in this._target.__values; }
    }
    const context = vm.createContext({
      document: { documentElement: { lang: 'en' }, getElementById: (id) => elements[id] || null },
      location,
      history,
      localStorage: this.local,
      sessionStorage,
      fetch: fetchImpl,
      FormData: HarnessFormData,
      crypto: globalThis.crypto,
      URLSearchParams,
      console,
    });
    vm.runInContext(source, context, { filename: 'reference-client/product-page.js' });

    const tab = {
      journal, sessionStorage, form, sessionStatus, resultStatus,
      async settle() {
        if (!url.includes('handoff=')) return; // 无码页没有兑换请求，载入即完成
        const deadline = Date.now() + 8000;
        while (Date.now() < deadline) {
          if (journal.some((item) => item.url.includes('/handoff/exchange')) && sessionStatus.textContent) {
            await sleep(25); // 让页面对响应体的处理（状态文案）跑完
            return;
          }
          await sleep(10);
        }
        throw new Error(`settle 超时：${url}`);
      },
      async submit(values, delayMs = 0) {
        await tab.settle();
        if (delayMs) await sleep(delayMs);
        // 与真实用户勾选“确认联系方式可用于本次请求”对应
        form.__values = { contact_confirmed: 'on', ...values };
        await form._listeners.submit({ preventDefault() {} });
        await sleep(15);
      },
      snapshot() {
        return {
          localStorage: local.snapshot(),
          sessionStorage: sessionStorage.snapshot(),
          statusText: sessionStatus.textContent,
          resultText: resultStatus.textContent,
          url: { hash: location.hash, pathname: location.pathname, search: location.search },
        };
      },
    };
    return tab;
  }
}

function pagePath(product, fragment = '') {
  return `/s/${config.siteKey}/products/${product.slug}${fragment}`;
}

const browser = new SimBrowser();
const scenarios = [];
async function scenario(name, run) {
  const record = { name };
  scenarios.push(record);
  await run(record);
}

try {
  // 1. 成功交接：兑换成功 → 凭据写入 localStorage，RFQ 并入原会话
  await scenario('success_handoff', async (rec) => {
    const tab = browser.openTab({ url: pagePath(config.products.t1, `#handoff=${config.handoffCode}`), productId: config.products.t1.id });
    await tab.settle();
    rec.afterLoad = tab.snapshot();
    await tab.submit({ email: 'buyer-one@example.invalid', phone: '', quantity: '2', destination: 'DE', notes: 'success handoff' });
    rec.afterSubmit = tab.snapshot();
    rec.requests = tab.journal;
  });

  // 2. 失败交接：已有保存会话（=成功路径留下的），无效码；提交必须建立独立新会话
  await scenario('failed_handoff_isolated', async (rec) => {
    const tab = browser.openTab({ url: pagePath(config.products.t2, `#handoff=${config.invalidCode}`), productId: config.products.t2.id });
    await tab.settle();
    rec.afterLoad = tab.snapshot();
    await tab.submit({ email: 'second-buyer@example.invalid', phone: '', quantity: '1', destination: 'FR', notes: 'failed handoff should isolate' });
    rec.afterSubmit = tab.snapshot();
    rec.requests = tab.journal;
    rec.tabSession = tab.sessionStorage;
  });

  // 3. 并存标签页（无码直访，同浏览器）：继续使用已保存会话，且不写入上面的新会话
  await scenario('coexisting_direct_tab', async (rec) => {
    const tab = browser.openTab({ url: pagePath(config.products.t1), productId: config.products.t1.id });
    await tab.settle();
    rec.afterLoad = tab.snapshot();
    await tab.submit({ email: 'buyer-one-again@example.invalid', phone: '', quantity: '1', destination: 'DE', notes: 'coexisting tab' });
    rec.afterSubmit = tab.snapshot();
    rec.requests = tab.journal;
  });

  // 4. 失败标签页刷新（无码，失败标记与独立会话仍在 sessionStorage）：继续独立处理
  await scenario('failed_tab_reload', async (rec) => {
    const tab = browser.openTab({ url: pagePath(config.products.t2), productId: config.products.t2.id, session: scenarios[1].tabSession });
    await tab.settle();
    rec.afterLoad = tab.snapshot();
    await tab.submit({ email: 'second-buyer-again@example.invalid', phone: '', quantity: '1', destination: 'FR', notes: 'failed tab reload' });
    rec.afterSubmit = tab.snapshot();
    rec.requests = tab.journal;
  });

  // 5. 全新浏览器无码直访：提交时新建会话并保存（既有直访行为）
  await scenario('fresh_direct_visit', async (rec) => {
    const fresh = new SimBrowser();
    const tab = fresh.openTab({ url: pagePath(config.products.t2), productId: config.products.t2.id });
    await tab.settle();
    rec.afterLoad = tab.snapshot();
    await tab.submit({ email: 'direct-visitor@example.invalid', phone: '', quantity: '3', destination: 'NL', notes: 'fresh direct visit' });
    rec.afterSubmit = tab.snapshot();
    rec.requests = tab.journal;
  });

  // 6. 并存有效交接：先开 A 页（T1）成功、再开 B 页（T2）成功；B 后开；
  //    随后在仍打开的 A 页提交仍须归 A（修复前 A 页重读共享 localStorage 会串到 B）。
  await scenario('coexisting_success_handoffs', async (rec) => {
    const tabA = browser.openTab({ url: pagePath(config.products.t1, `#handoff=${config.handoffCodeA2}`), productId: config.products.t1.id });
    await tabA.settle();
    rec.afterLoadA = tabA.snapshot();
    const tabB = browser.openTab({ url: pagePath(config.products.t2, `#handoff=${config.handoffCodeB}`), productId: config.products.t2.id });
    await tabB.settle();
    rec.afterLoadB = tabB.snapshot();
    await tabA.submit({ email: 'buyer-a@example.invalid', phone: '', quantity: '5', destination: 'DE', notes: 'A submits after B opened' });
    rec.afterSubmitA = tabA.snapshot();
    rec.requestsA = tabA.journal;
    await tabB.submit({ email: 'buyer-b@example.invalid', phone: '', quantity: '2', destination: 'FR', notes: 'B submits' });
    rec.afterSubmitB = tabB.snapshot();
    rec.requestsB = tabB.journal;
    rec.tabASession = tabA.sessionStorage;
  });

  // 7. 成功交接标签页刷新（URL 已无码）：恢复“已连接”，提交仍归原会话；
  //    此时浏览器级保存会话已被 B 覆盖，本页归属不得受影响。
  await scenario('connected_tab_reload', async (rec) => {
    const tab = browser.openTab({ url: pagePath(config.products.t1), productId: config.products.t1.id, session: scenarios[5].tabASession });
    await tab.settle();
    rec.afterLoad = tab.snapshot();
    await tab.submit({ email: 'buyer-a-reload@example.invalid', phone: '', quantity: '1', destination: 'DE', notes: 'reload of connected tab' });
    rec.afterSubmit = tab.snapshot();
    rec.requests = tab.journal;
  });

  // 8. 失败交接序列：同标签页连续失败复用自己建的独立会话；新标签页失败各自独立；
  //    “成功后再失败”作废旧固定凭据、按新访客处理 —— 均不得触达其他既有会话。
  await scenario('failed_handoff_sequences', async (rec) => {
    const tabF1 = browser.openTab({ url: pagePath(config.products.t2, `#handoff=${config.invalidCode}`), productId: config.products.t2.id });
    await tabF1.settle();
    await tabF1.submit({ email: 'visitor-f1@example.invalid', phone: '', quantity: '1', destination: 'NL', notes: 'first failed attempt' });
    rec.f1AfterSubmit = tabF1.snapshot();
    rec.f1Requests = tabF1.journal;
    const tabF1b = browser.openTab({ url: pagePath(config.products.t2, `#handoff=${config.invalidCode2}`), productId: config.products.t2.id, session: tabF1.sessionStorage });
    await tabF1b.settle();
    await tabF1b.submit({ email: 'visitor-f1@example.invalid', phone: '', quantity: '1', destination: 'NL', notes: 'second failed attempt, same tab' });
    rec.f1bAfterSubmit = tabF1b.snapshot();
    rec.f1bRequests = tabF1b.journal;
    const tabF2 = browser.openTab({ url: pagePath(config.products.t2, `#handoff=${config.invalidCode}`), productId: config.products.t2.id });
    await tabF2.settle();
    await tabF2.submit({ email: 'visitor-f2@example.invalid', phone: '', quantity: '2', destination: 'NL', notes: 'separate failed visitor' });
    rec.f2AfterSubmit = tabF2.snapshot();
    rec.f2Requests = tabF2.journal;
    const tabF3 = browser.openTab({ url: pagePath(config.products.t1, `#handoff=${config.invalidCode}`), productId: config.products.t1.id, session: scenarios[5].tabASession });
    await tabF3.settle();
    await tabF3.submit({ email: 'visitor-f3@example.invalid', phone: '', quantity: '1', destination: 'DE', notes: 'failed handoff after a previous success in this tab' });
    rec.f3AfterSubmit = tabF3.snapshot();
    rec.f3Requests = tabF3.journal;
  });

  writeFileSync(resultPath, JSON.stringify({ ok: true, node: process.version, baseUrl: config.baseUrl, scenarios }, null, 2));
  console.log(`ok: ${scenarios.length} scenarios against ${config.baseUrl}`);
} catch (error) {
  writeFileSync(resultPath, JSON.stringify({ ok: false, node: process.version, baseUrl: config.baseUrl, error: String(error && error.stack || error), scenarios }, null, 2));
  console.error(error && error.stack || error);
  process.exit(1);
}
