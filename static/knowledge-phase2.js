/* ════════════════════════════════════════════════════════════════
   企业知识 · v0.3 主题与资料 · 正式界面交互（任务3）
   依据设计基准 cd5f3e9 的原型交互移植，无依赖（原生 JS）：
   - 分组 / 主题文件列表（含窄屏）折叠：aria-expanded + 收起 inert，状态记在 sessionStorage；
   - 中央总览分组折叠（搜索时命中组临时展开、清空恢复）；搜索「×」只清关键词并回焦点；
   - 添加资料：单一输入卡片三模式切换（aria-pressed，切换不丢已输入）；
   - 未保存保护：离开核对 / 编辑页面前可「保存草稿并离开 / 放弃修改 / 继续编辑」；
   - 危险与发布动作确认对话框（按表单绑定；表单内其他动作按钮可声明 data-kb2-confirm-skip 跳过确认；
     确认重发保留触发按钮字段（requestSubmit）；Escape 关闭、焦点恢复）。
   reduced-motion 由 CSS 统一禁用动画；所有可点击元素都是原生 button/summary/a。
   ════════════════════════════════════════════════════════════════ */
(function () {
  'use strict';
  var root = document.querySelector('.kb2');
  if (!root) { return; }
  var STORE = window.sessionStorage;

  function loadState(key) {
    try { return JSON.parse(STORE.getItem(key) || '{}'); } catch (error) { return {}; }
  }
  function saveState(key, value) {
    try { STORE.setItem(key, JSON.stringify(value)); } catch (error) { /* 隐私模式下退化为本次会话内存状态 */ }
  }
  function setOpen(element, open) {
    if (!element) { return; }
    element.classList.toggle('open', open);
    if (open) { element.removeAttribute('inert'); } else { element.setAttribute('inert', ''); }
  }
  function each(list, fn) { Array.prototype.forEach.call(list, fn); }

  /* ── 主题浏览树：分组与主题文件列表折叠（桌面 + 窄屏两套 DOM 同步）── */
  var navGroups = loadState('kb2.navGroups');
  var navFiles = loadState('kb2.navTopicFiles');

  function renderNavGroup(gid) {
    var open = !!navGroups[gid];
    each(root.querySelectorAll('[data-kb2="nav-group"][data-gid="' + gid + '"]'), function (btn) {
      btn.setAttribute('aria-expanded', open ? 'true' : 'false');
    });
    setOpen(document.getElementById('nav-sub-' + gid), open);
    setOpen(document.getElementById('mnav-sub-' + gid), open);
  }
  function renderNavFiles(tid) {
    var open = !!navFiles[tid];
    each(root.querySelectorAll('[data-kb2="nav-topic-files"][data-tid="' + tid + '"]'), function (btn) {
      btn.setAttribute('aria-expanded', open ? 'true' : 'false');
    });
    setOpen(document.getElementById('nav-files-' + tid), open);
    setOpen(document.getElementById('mnav-files-' + tid), open);
  }
  each(root.querySelectorAll('[data-kb2="nav-group"]'), function (btn) {
    if (navGroups[btn.getAttribute('data-gid')]) { renderNavGroup(btn.getAttribute('data-gid')); }
    btn.addEventListener('click', function () {
      var gid = btn.getAttribute('data-gid');
      navGroups[gid] = !navGroups[gid];
      saveState('kb2.navGroups', navGroups);
      renderNavGroup(gid);
    });
  });
  each(root.querySelectorAll('[data-kb2="nav-topic-files"]'), function (btn) {
    if (navFiles[btn.getAttribute('data-tid')]) { renderNavFiles(btn.getAttribute('data-tid')); }
    btn.addEventListener('click', function () {
      var tid = btn.getAttribute('data-tid');
      navFiles[tid] = !navFiles[tid];
      saveState('kb2.navTopicFiles', navFiles);
      renderNavFiles(tid);
    });
  });
  /* 窄屏「浏览主题」：点选主题或文件后收起（桌面无此元素或本就收起） */
  each(root.querySelectorAll('.ts-item, .ts-file'), function (link) {
    link.addEventListener('click', function () {
      var details = document.getElementById('topic-browse-details');
      if (details && details.open) { details.open = false; }
    });
  });

  /* ── 中央总览分组折叠（搜索时命中组临时展开；清空后恢复用户状态）── */
  var ovFolds = loadState('kb2.ovFolds');
  var searchInput = document.getElementById('kb2-search');
  var searching = !!(searchInput && searchInput.value.trim() !== '');

  function renderOvFold(gid) {
    var open = searching ? true : (ovFolds[gid] === undefined ? true : !!ovFolds[gid]);
    var btn = document.getElementById('ov-fold-btn-' + gid);
    if (btn) { btn.setAttribute('aria-expanded', open ? 'true' : 'false'); }
    setOpen(document.getElementById('ov-body-' + gid), open);
  }
  each(root.querySelectorAll('[data-kb2="ov-fold"]'), function (btn) {
    var gid = btn.getAttribute('data-gid');
    renderOvFold(gid);
    btn.addEventListener('click', function () {
      if (searching) { return; }  /* 搜索中显示态保持展开，用户折叠状态不被搜索结果改写 */
      ovFolds[gid] = !(ovFolds[gid] === undefined ? true : !!ovFolds[gid]);
      saveState('kb2.ovFolds', ovFolds);
      renderOvFold(gid);
    });
  });

  /* ── 搜索：「×」只清关键词（保留其他筛选）、清空后回焦点；输入即时显示 ── */
  var clearLink = document.getElementById('kb2-search-clear');
  if (searchInput && clearLink) {
    searchInput.addEventListener('input', function () {
      if (searchInput.value.trim()) { clearLink.removeAttribute('hidden'); }
      else { clearLink.setAttribute('hidden', ''); }
    });
    clearLink.addEventListener('click', function (event) {
      event.preventDefault();
      window.location.href = clearLink.getAttribute('href') + '#kb2-search';
    });
    var form = searchInput.form;
    if (form) {
      form.addEventListener('submit', function (event) {
        if (!searchInput.value.trim()) {
          event.preventDefault();
          window.location.href = form.getAttribute('action') + '#kb2-search';
        }
      });
    }
  }
  if (window.location.hash === '#kb2-search' && searchInput) {
    searchInput.focus();
    searchInput.select();
    if (window.history && window.history.replaceState) {
      window.history.replaceState(null, '', window.location.pathname + window.location.search);
    }
  }

  /* ── 添加资料：单一输入卡片三模式切换（切换不丢已输入，不重渲染）── */
  var modeButtons = root.querySelectorAll('[data-kb2-mode-btn]');
  var modePanels = root.querySelectorAll('[data-kb2-mode]');
  function applyMode(target) {
    each(modeButtons, function (button) {
      var active = button.getAttribute('data-kb2-mode-btn') === target;
      button.setAttribute('aria-pressed', active ? 'true' : 'false');
      button.classList.toggle('on', active);
    });
    each(modePanels, function (panel) {
      if (panel.getAttribute('data-kb2-mode') === target) { panel.removeAttribute('hidden'); }
      else { panel.setAttribute('hidden', ''); }
    });
  }
  each(modeButtons, function (button) {
    button.addEventListener('click', function () { applyMode(button.getAttribute('data-kb2-mode-btn')); });
  });
  each(root.querySelectorAll('[data-kb2-submit-any]'), function (button) {
    button.addEventListener('click', function () {
      /* 提交前把三种输入都提交：面板只是显示层，hidden 面板里的输入仍是表单字段 */
      var form = button.closest('form');
      if (form && !form.querySelector('input[type="file"]').files.length
          && !form.querySelector('[name="link_url"]').value.trim()
          && !form.querySelector('[name="manual_text"]').value.trim()) {
        var target = document.querySelector('.mode-card[hidden]');
        if (target) { applyMode(target.getAttribute('data-kb2-mode')); }
      }
    });
  });

  /* ── 对话框（确认 / 未保存保护）：Escape 关闭、焦点恢复 ── */
  function buildDialog(id) {
    var dialog = document.createElement('dialog');
    dialog.className = 'dlg';
    dialog.id = id;
    dialog.innerHTML =
      '<div class="dlg-head"><span class="dlg-icon info" aria-hidden="true">!</span><div>' +
      '<h2 class="dlg-title"></h2><p class="dlg-sub"></p></div></div>' +
      '<div class="dlg-body"><p class="dlg-message"></p></div>' +
      '<div class="dlg-foot"></div>';
    root.appendChild(dialog);
    return dialog;
  }
  var lastFocus = null;
  function openDialog(dialog, title, message, buttons) {
    lastFocus = document.activeElement;
    dialog.querySelector('.dlg-title').textContent = title;
    dialog.querySelector('.dlg-sub').textContent = '请在下面选择下一步。';
    dialog.querySelector('.dlg-message').textContent = message;
    var foot = dialog.querySelector('.dlg-foot');
    foot.innerHTML = '';
    buttons.forEach(function (spec, index) {
      var button = document.createElement('button');
      button.type = 'button';
      button.className = 'button' + (spec.secondary ? ' secondary' : '');
      button.textContent = spec.label;
      button.addEventListener('click', function () {
        dialog.close();
        spec.action();
      });
      foot.appendChild(button);
      if (index === 0) { window.setTimeout(function () { button.focus(); }, 0); }
    });
    if (typeof dialog.showModal === 'function') { dialog.showModal(); }
    else { dialog.setAttribute('open', ''); }
  }
  function restoreFocus() {
    if (lastFocus && typeof lastFocus.focus === 'function') { lastFocus.focus(); }
  }
  var confirmDialog = buildDialog('kb2-confirm-dialog');
  confirmDialog.addEventListener('close', function () { restoreFocus(); });
  var guardDialog = buildDialog('kb2-nav-dialog');
  guardDialog.addEventListener('close', function () { restoreFocus(); });

  /* ── 确认对话框：表单动作（删除 / 停用 / 发布等）── */
  /* 确认后重发必须保留触发按钮的字段：动作通常写在提交按钮上（name=action value=publish），
     而 form.submit() 会丢掉提交按钮的字段——动作会被降级成默认值（例如把「发布」变成
     「保存设置」）。优先用 requestSubmit(submitter) 原生重发；老浏览器回退为注入同名隐藏字段。 */
  function resubmit(form, submitter) {
    if (submitter && typeof form.requestSubmit === 'function') {
      try { form.requestSubmit(submitter); return; } catch (error) { /* 回退注入 */ }
    }
    if (submitter && submitter.name) {
      var hidden = document.createElement('input');
      hidden.type = 'hidden';
      hidden.name = submitter.name;
      hidden.value = submitter.value;
      form.appendChild(hidden);
    }
    form.submit();
  }
  function hookConfirmForms(scope) {
    each(scope.querySelectorAll('form[data-kb2-confirm]'), function (form) {
      if (form.dataset.kb2Bound === '1') { return; }
      form.dataset.kb2Bound = '1';
      form.addEventListener('submit', function (event) {
        if (form.dataset.kb2Confirmed === '1') { return; }
        var submitter = event.submitter || null;
        /* 同表单的其他动作按钮可带 data-kb2-confirm-skip：按自己的动作直接提交、不弹确认。 */
        if (submitter && submitter.hasAttribute('data-kb2-confirm-skip')) { return; }
        event.preventDefault();
        lastFocus = submitter || form.querySelector('button') || document.activeElement;
        var field = form.getAttribute('data-kb2-confirm-field');
        var value = form.getAttribute('data-kb2-confirm-value');
        var title = form.getAttribute('data-kb2-confirm-title') || '请确认';
        var message = form.getAttribute('data-kb2-confirm') || '继续执行该操作？';
        openDialog(confirmDialog, title, message, [
          { label: '继续', action: function () {
              if (field) {
                var target = form.querySelector('[name="' + field + '"]');
                if (target) { target.value = value || '1'; }
              }
              form.dataset.kb2Confirmed = '1';
              resubmit(form, submitter);
          } },
          { label: '取消', secondary: true, action: function () { restoreFocus(); } },
        ]);
      });
    });
  }
  hookConfirmForms(root);

  /* ── 未保存保护：核对 / 编辑表单有改动时，离开前给出选择 ── */
  function dirtyForms() {
    return Array.prototype.filter.call(
      root.querySelectorAll('form[data-kb2-save]'),
      function (form) { return form.dataset.dirty === '1'; });
  }
  each(root.querySelectorAll('form[data-kb2-save]'), function (form) {
    form.addEventListener('input', function () { form.dataset.dirty = '1'; });
    form.addEventListener('change', function () { form.dataset.dirty = '1'; });
    form.addEventListener('submit', function () { form.dataset.dirty = ''; });
  });
  window.addEventListener('beforeunload', function (event) {
    if (dirtyForms().length) {
      event.preventDefault();
      event.returnValue = '有未保存的修改';
      return event.returnValue;
    }
  });
  function firstDirtyField() {
    var forms = dirtyForms();
    return forms.length ? forms[0].querySelector('input, textarea, select') : null;
  }
  document.addEventListener('click', function (event) {
    var link = event.target.closest ? event.target.closest('a') : null;
    if (!link) { return; }
    var href = link.getAttribute('href') || '';
    if (!href || href.charAt(0) !== '/' || link.target === '_blank' || link.hasAttribute('download')
        || link.getAttribute('data-kb2-ignore') === '1') {
      return;
    }
    var forms = dirtyForms();
    if (!forms.length) { return; }
    event.preventDefault();
    lastFocus = link;
    openDialog(guardDialog, '有未保存的修改', '离开前可以保存草稿，或放弃本次修改。', [
      { label: '保存草稿并离开', action: function () {
          var chain = Promise.resolve(true);
          forms.forEach(function (form) {
            chain = chain.then(function (ok) {
              if (!ok) { return false; }
              var body = new URLSearchParams(new FormData(form));
              return fetch(form.getAttribute('action'), {
                method: 'POST', body: body,
                headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
                credentials: 'same-origin',
              }).then(function (response) { return response.ok; }).catch(function () { return false; });
            });
          });
          chain.then(function (ok) {
            if (ok) { window.location.href = href; }
            else { window.alert('保存失败：请检查网络后重试，或选择「继续编辑」手动保存。'); }
          });
      } },
      { label: '放弃修改', secondary: true, action: function () {
          forms.forEach(function (form) { form.dataset.dirty = ''; });
          window.location.href = href;
      } },
      { label: '继续编辑', secondary: true, action: function () {
          var field = firstDirtyField();
          if (field && field.focus) { field.focus(); }
      } },
    ]);
  }, true);
})();
