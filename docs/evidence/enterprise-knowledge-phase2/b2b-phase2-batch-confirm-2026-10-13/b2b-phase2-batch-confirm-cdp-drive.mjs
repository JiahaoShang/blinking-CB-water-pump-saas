// 批次发布确认 E2E 驱动：真实 Chrome（headless=new）+ CDP，无第三方依赖。
// 用法:
//   node cdp_drive.mjs probe  --port 18991 --out <dir> --app http://127.0.0.1:18990
//   node cdp_drive.mjs prefix --port 18991 --out <dir> --app <origin> --cookie <cb_session> --ext <batch> --int <batch>
//   node cdp_drive.mjs postfix --port 18991 --out <dir> --app <origin> --cookie <cb_session> --ext <batch> --int <batch>
import { spawn } from 'node:child_process';
import { mkdirSync, writeFileSync } from 'node:fs';
import { setTimeout as sleep } from 'node:timers/promises';

const CHROME = '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome';
const argv = process.argv.slice(2);
const scenario = argv[0];
const opts = {};
for (let i = 1; i < argv.length; i += 2) { opts[argv[i].replace(/^--/, '')] = argv[i + 1]; }
const APP = opts.app || 'http://127.0.0.1:18990';
const PORT = Number(opts.port || 18991);
const OUT = opts.out || '/tmp';
const COOKIE = opts.cookie || '';
mkdirSync(OUT, { recursive: true });

const report = { scenario, steps: [], posts: [], dialogs: {} };
const note = (key, value) => { report.steps.push({ [key]: value }); console.log(key + ':', JSON.stringify(value)); };

class CDP {
  constructor(ws) {
    this.ws = ws; this.seq = 0; this.pending = new Map(); this.posts = [];
    ws.addEventListener('message', (ev) => {
      const msg = JSON.parse(ev.data);
      if (msg.id) {
        const p = this.pending.get(msg.id);
        if (p) { this.pending.delete(msg.id); msg.error ? p.reject(new Error(msg.error.message)) : p.resolve(msg.result); }
        return;
      }
      if (msg.method === 'Network.requestWillBeSent' && msg.params.request.method === 'POST') {
        this.posts.push({ url: msg.params.request.url, postData: msg.params.request.postData || '' });
      }
    });
  }
  send(method, params = {}) {
    const id = ++this.seq;
    return new Promise((resolve, reject) => {
      this.pending.set(id, { resolve, reject });
      this.ws.send(JSON.stringify({ id, method, params }));
    });
  }
}

async function launchChrome() {
  const userDir = `${OUT}/chrome-profile-${Date.now()}`;
  const child = spawn(CHROME, [
    '--headless=new', `--remote-debugging-port=${PORT}`, `--user-data-dir=${userDir}`,
    '--no-first-run', '--no-default-browser-check', '--disable-gpu', '--hide-scrollbars',
    '--window-size=1440,900', 'about:blank',
  ], { stdio: 'ignore', detached: false });
  let version = null;
  for (let i = 0; i < 50; i += 1) {
    await sleep(200);
    try {
      const res = await fetch(`http://127.0.0.1:${PORT}/json/version`);
      version = await res.json();
      break;
    } catch (error) { /* 等待端口就绪 */ }
  }
  if (!version) { child.kill('SIGKILL'); throw new Error('Chrome 未就绪'); }
  const target = await (await fetch(`http://127.0.0.1:${PORT}/json/new?about:blank`, { method: 'PUT' })).json();
  const ws = new WebSocket(target.webSocketDebuggerUrl);
  await new Promise((resolve, reject) => {
    ws.addEventListener('open', resolve);
    ws.addEventListener('error', reject);
    setTimeout(() => reject(new Error('ws 连接超时')), 8000);
  });
  const cdp = new CDP(ws);
  await cdp.send('Page.enable');
  await cdp.send('Network.enable');
  return { child, cdp };
}

const q = (s) => JSON.stringify(s);
async function evalJS(cdp, expr, { retry = true } = {}) {
  try {
    const r = await cdp.send('Runtime.evaluate', { expression: expr, returnByValue: true, awaitPromise: true });
    if (r.exceptionDetails) { throw new Error('eval 异常: ' + JSON.stringify(r.exceptionDetails.exception && r.exceptionDetails.exception.description)); }
    return r.result ? r.result.value : undefined;
  } catch (error) {
    if (retry) { await sleep(150); return evalJS(cdp, expr, { retry: false }); }
    throw error;
  }
}
async function waitFor(cdp, expr, timeoutMs = 15000) {
  const deadline = Date.now() + timeoutMs;
  let last = null;
  while (Date.now() < deadline) {
    try { last = await evalJS(cdp, expr); if (last) return last; }
    catch (error) { last = `eval-error: ${error.message}`; }
    await sleep(200);
  }
  throw new Error(`等待超时: ${expr}（最后值 ${last}）`);
}
async function navigate(cdp, url) {
  await cdp.send('Page.navigate', { url });
  await waitFor(cdp, `document.readyState === 'complete' && !!document.body`, 20000);
}
async function clickSelector(cdp, selector) {
  const box = await evalJS(cdp, `(() => {
    const el = document.querySelector(${q(selector)});
    if (!el) return null;
    el.scrollIntoView({ block: 'center' });
    const r = el.getBoundingClientRect();
    return { x: r.left + r.width / 2, y: r.top + r.height / 2 };
  })()`);
  if (!box) throw new Error('未找到元素: ' + selector);
  await cdp.send('Input.dispatchMouseEvent', { type: 'mouseMoved', x: box.x, y: box.y });
  await cdp.send('Input.dispatchMouseEvent', { type: 'mousePressed', x: box.x, y: box.y, button: 'left', clickCount: 1 });
  await cdp.send('Input.dispatchMouseEvent', { type: 'mouseReleased', x: box.x, y: box.y, button: 'left', clickCount: 1 });
}
async function pressEscape(cdp) {
  const base = { key: 'Escape', code: 'Escape', windowsVirtualKeyCode: 27, nativeVirtualKeyCode: 53 };
  await cdp.send('Input.dispatchKeyEvent', { type: 'rawKeyDown', ...base });
  await cdp.send('Input.dispatchKeyEvent', { type: 'keyUp', ...base });
}
async function pressEnter(cdp) {
  const base = { key: 'Enter', code: 'Enter', windowsVirtualKeyCode: 13, nativeVirtualKeyCode: 36 };
  await cdp.send('Input.dispatchKeyEvent', { type: 'rawKeyDown', ...base });
  await cdp.send('Input.dispatchKeyEvent', { type: 'keyUp', ...base });
}
async function shot(cdp, name) {
  const r = await cdp.send('Page.captureScreenshot', { format: 'png' });
  const path = `${OUT}/${name}.png`;
  writeFileSync(path, Buffer.from(r.data, 'base64'));
  note(`screenshot_${name}`, path);
}
const dialogState = `(() => { const d = document.getElementById('kb2-confirm-dialog'); return JSON.stringify({ exists: !!d, open: !!(d && d.open) }); })()`;
async function currentUrl(cdp) { return evalJS(cdp, 'window.location.href'); }
async function setCookie(cdp) {
  await cdp.send('Network.setCookie', { name: 'cb_session', value: COOKIE, url: APP + '/', path: '/' });
}
function lastPost(cdp, needle = '/settings') {
  return [...cdp.posts].reverse().find((p) => p.url.includes(needle)) || null;
}

async function ensureOrigin(cdp) {
  await navigate(cdp, APP + '/login');
  await setCookie(cdp);
}

/* ── probe：浏览器原生提交语义（form.submit() 丢按钮字段；requestSubmit 保留）── */
async function runProbe(cdp) {
  const page = 'data:text/html,' + encodeURIComponent(
    '<!doctype html><meta charset="utf-8"><form id="f" method="post" action="' + APP + '/login">' +
    '<input name="email" value="探针@example.invalid"><input type="password" name="password" value="x">' +
    '<button id="b" name="action" value="publish" type="submit">发布</button>' +
    '<input type="hidden" name="confirm_ai" value="0"></form>');
  await navigate(cdp, page);
  await evalJS(cdp, 'document.getElementById("f").submit()');
  await sleep(600);
  const viaSubmit = lastPost(cdp, '/login');
  note('programmaticSubmit(postData)', viaSubmit ? viaSubmit.postData : null);
  const parsedSubmit = new URLSearchParams(viaSubmit ? viaSubmit.postData : '');
  note('form.submit() 是否携带 action 字段', parsedSubmit.has('action'));

  await navigate(cdp, page);
  await evalJS(cdp, 'document.getElementById("f").requestSubmit(document.getElementById("b"))');
  await sleep(600);
  const viaRequest = lastPost(cdp, '/login');
  const parsedRequest = new URLSearchParams(viaRequest ? viaRequest.postData : '');
  note('form.requestSubmit(button) 是否携带 action 字段', parsedRequest.has('action'));
  note('requestSubmit action 值', parsedRequest.get('action'));

  await navigate(cdp, page);
  await evalJS(cdp, 'document.querySelector("input[name=email]").focus()');
  await pressEnter(cdp);
  await sleep(600);
  const viaEnter = lastPost(cdp, '/login');
  const parsedEnter = new URLSearchParams(viaEnter ? viaEnter.postData : '');
  note('Enter 隐式提交是否携带 action 字段', parsedEnter.has('action'));
  note('Enter 隐式提交 action 值', parsedEnter.get('action'));
}

/* ── 公共：打开批次发布设置页 ─────────────────────────────── */
async function openBatch(cdp, batch) {
  await navigate(cdp, `${APP}/admin/knowledge/batch/${batch}?step=publish`);
  await waitFor(cdp, 'document.readyState === "complete" && !!document.querySelector("form[action$=\'/settings\']")', 15000);
}
function formAttrsExpr(batch) {
  return `(() => {
    const form = document.querySelector("form[action='/admin/knowledge/batch/${batch}/settings']");
    if (!form) return JSON.stringify({ form: false });
    const btn = form.querySelector("button[value='publish']");
    const save = form.querySelector("button[value='save']");
    const hidden = form.querySelector("input[name='confirm_ai']");
    return JSON.stringify({
      form: true,
      formConfirm: form.hasAttribute('data-kb2-confirm'),
      formField: form.getAttribute('data-kb2-confirm-field'),
      formValue: form.getAttribute('data-kb2-confirm-value'),
      btnConfirm: btn ? btn.hasAttribute('data-kb2-confirm') : null,
      btnField: btn ? btn.getAttribute('data-kb2-confirm-field') : null,
      saveSkip: save ? save.hasAttribute('data-kb2-confirm-skip') : null,
      hiddenConfirm: hidden ? hidden.value : null,
    });
  })()`;
}

/* ── prefix：修复前复现（发布为直接提交 → 对外+AI 被拒）── */
async function runPrefix(cdp) {
  await ensureOrigin(cdp);
  await openBatch(cdp, opts.ext);
  note('ext.formAttrs(prefix)', JSON.parse(await evalJS(cdp, formAttrsExpr(opts.ext))));
  await shot(cdp, 'prefix-ext-publish-step');

  // 保存设置（修复前：表单未挂确认；发布按钮属性不生效，保存照常直接提交）。
  await clickSelector(cdp, "form[action$='/settings'] button[value='save']");
  await waitFor(cdp, `window.location.href.includes('notice=') || document.body.innerText.includes('已保存')`, 15000);
  const savePost = lastPost(cdp);
  note('prefix.save.postData', savePost ? savePost.postData : null);
  note('prefix.save.url', await currentUrl(cdp));

  await openBatch(cdp, opts.ext);
  await clickSelector(cdp, "form[action$='/settings'] button[value='publish']");
  await sleep(800);
  const dlg = JSON.parse(await evalJS(cdp, dialogState));
  note('prefix.publish.dialog', dlg);
  await waitFor(cdp, `window.location.href.includes('/batch/${opts.ext}') && document.readyState === 'complete'`, 15000);
  const pubPost = lastPost(cdp);
  note('prefix.publish.postData', pubPost ? pubPost.postData : null);
  const url = await currentUrl(cdp);
  note('prefix.publish.url', url);
  note('prefix.publish.errorShown', decodeURIComponent(url).includes('需要确认'));
  await shot(cdp, 'prefix-after-publish-click');
}

/* ── postfix：修复后全流程 ─────────────────────────────────── */
async function runPostfix(cdp) {
  await ensureOrigin(cdp);
  // 0) 保存设置：不弹确认、直接保存（skip 生效）。
  await openBatch(cdp, opts.ext);
  await evalJS(cdp, `(() => { window.__dlgOpened = false; window.__dlgObserver = new MutationObserver(() => { const d = document.getElementById('kb2-confirm-dialog'); if (d && d.open) { window.__dlgOpened = true; } }); window.__dlgObserver.observe(document.body, { childList: true, subtree: true, attributes: true }); return true; })()`);
  await clickSelector(cdp, "form[action$='/settings'] button[value='save']");
  await sleep(900);
  const saveNavigated = await evalJS(cdp, `document.body.innerText.includes('已保存发布设置')`);
  const saveDialogOpened = await evalJS(cdp, 'window.__dlgOpened === true');
  const savePost = lastPost(cdp);
  note('postfix.save.navigated', !!saveNavigated);
  note('postfix.save.dialogOpened', !!saveDialogOpened);
  note('postfix.save.postData', savePost ? savePost.postData : null);
  note('postfix.save.url', await currentUrl(cdp));

  // 1) 发布 → 弹窗（真实点击）。
  await openBatch(cdp, opts.ext);
  await clickSelector(cdp, "form[action$='/settings'] button[value='publish']");
  await sleep(400);
  note('postfix.publish.dialogAfterClick', JSON.parse(await evalJS(cdp, dialogState)));
  note('postfix.publish.urlAfterClick', await currentUrl(cdp));
  await shot(cdp, 'postfix-publish-dialog-open');

  // 2) 真实 Escape：关闭、不发布。
  await pressEscape(cdp);
  await sleep(400);
  note('postfix.escape.dialog', JSON.parse(await evalJS(cdp, dialogState)));
  note('postfix.escape.url', await currentUrl(cdp));
  note('postfix.escape.hiddenConfirm', await evalJS(cdp, `(() => { const h = document.querySelector("form[action$='/settings'] input[name='confirm_ai']"); return h ? h.value : null; })()`));

  // 3) 发布 → 弹窗 → 取消：关闭、不发布。
  await clickSelector(cdp, "form[action$='/settings'] button[value='publish']");
  await sleep(300);
  note('postfix.cancel.dialogOpened', JSON.parse(await evalJS(cdp, dialogState)));
  await clickSelector(cdp, "#kb2-confirm-dialog .dlg-foot button:last-child");
  await sleep(400);
  note('postfix.cancel.dialogAfter', JSON.parse(await evalJS(cdp, dialogState)));
  note('postfix.cancel.url', await currentUrl(cdp));

  // 4) 发布 → 弹窗 → 继续：真实发布（对外+AI 需 confirm_ai=1）。
  await clickSelector(cdp, "form[action$='/settings'] button[value='publish']");
  await sleep(300);
  note('postfix.accept.dialogOpened', JSON.parse(await evalJS(cdp, dialogState)));
  await shot(cdp, 'postfix-publish-dialog-before-continue');
  await clickSelector(cdp, "#kb2-confirm-dialog .dlg-foot button:first-child");
  await waitFor(cdp, `document.body.innerText.includes('已发布') || window.location.href.includes('error=') || window.location.href.includes('notice=')`, 20000);
  const pubPost = lastPost(cdp);
  note('postfix.accept.postData', pubPost ? pubPost.postData : null);
  const extUrl = await currentUrl(cdp);
  note('postfix.accept.url', extUrl);
  note('postfix.accept.notice', decodeURIComponent(extUrl));
  note('postfix.accept.publishedShown', await evalJS(cdp, `document.body.innerText.includes('已发布')`));
  await shot(cdp, 'postfix-ext-published');

  // 5) 内部批次：发布 → 弹窗 → 继续 → 发布（内部常规批量发布可用）。
  await openBatch(cdp, opts.int);
  await clickSelector(cdp, "form[action$='/settings'] button[value='publish']");
  await sleep(300);
  note('postfix.internal.dialogOpened', JSON.parse(await evalJS(cdp, dialogState)));
  await clickSelector(cdp, "#kb2-confirm-dialog .dlg-foot button:first-child");
  await waitFor(cdp, `document.body.innerText.includes('已发布') || window.location.href.includes('error=') || window.location.href.includes('notice=')`, 20000);
  const intPost = lastPost(cdp);
  note('postfix.internal.postData', intPost ? intPost.postData : null);
  const intUrl = await currentUrl(cdp);
  note('postfix.internal.url', intUrl);
  note('postfix.internal.publishedShown', await evalJS(cdp, `document.body.innerText.includes('已发布')`));
  await shot(cdp, 'postfix-int-published');
}

/* ── topic：主题发布（共享确认组件的另一挂接点）真实浏览器回归 ── */
async function runTopic(cdp) {
  await ensureOrigin(cdp);
  await navigate(cdp, `${APP}/admin/knowledge/topic/${opts.topic}`);
  await waitFor(cdp, `document.readyState === "complete" && !!document.querySelector("form[action$='/publish']")`, 15000);
  await clickSelector(cdp, "form[action$='/publish'] button");
  await sleep(300);
  note('topic.firstClick.dialog', JSON.parse(await evalJS(cdp, dialogState)));
  await shot(cdp, 'postfix-topic-dialog-open');
  await clickSelector(cdp, "#kb2-confirm-dialog .dlg-foot button:first-child");
  await waitFor(cdp, `document.body.innerText.includes('已发布') || window.location.href.includes('notice=') || window.location.href.includes('error=')`, 20000);
  const url = await currentUrl(cdp);
  note('topic.url', url);
  note('topic.publishedShown', await evalJS(cdp, `document.body.innerText.includes('已发布') || document.body.innerText.includes('AI 客服可用')`));
  const post = [...cdp.posts].reverse().find((p) => p.url.endsWith('/publish'));
  note('topic.postData', post ? post.postData : null);
  await shot(cdp, 'postfix-topic-published');
}

const { child, cdp } = await launchChrome();
try {
  if (scenario === 'probe') { await runProbe(cdp); }
  else if (scenario === 'prefix') { await runPrefix(cdp); }
  else if (scenario === 'postfix') { await runPostfix(cdp); }
  else if (scenario === 'topic') { await runTopic(cdp); }
  else { throw new Error('未知场景: ' + scenario); }
  report.posts = cdp.posts.map((p) => ({ url: p.url, postData: p.postData }));
  writeFileSync(`${OUT}/${scenario}-report.json`, JSON.stringify(report, null, 2));
  console.log('REPORT_SAVED', `${OUT}/${scenario}-report.json`);
} finally {
  child.kill('SIGTERM');
}
