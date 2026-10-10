"""企业知识统一管理界面（第一批任务3）：列表 / 详情 / 编辑 / 导入 / 来源与版本。

- 只从真实后端数据渲染；没有演示管理员切换、没有预置问答、没有假处理计时。
- 权限（后端强制）：本企业成员可读；管理员或获 products 模块授权者才出现编辑/发布/授权入口，
  只读成员看到明确说明；写接口对无权限成员一律 403。
- 界面沿最终原型布局：统一列表（分视图）、详情（属性栏+来源与版本页签）、编辑器、
  导入（选文件→检查→保存/发布）、搜索清空保筛选、折叠导航由控制台外壳与 JS 提供。
- 到期时间：可选精确时间戳，界面明确标注 Asia/Shanghai（北京时间 UTC+8），服务端归一为 UTC。
"""
from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from urllib.parse import quote, urlencode

from enterprise_knowledge import SHANGHAI_TZ, knowledge_expiry_instant

FORM_LABELS = {"article": "文章", "faq": "FAQ", "product": "产品", "service": "服务"}
VIEWS = {
    "all": ("全部知识", None),
    "product_service": ("产品与服务", ("product", "service")),
    "article_faq": ("文章与 FAQ", ("article", "faq")),
    "files": ("文件资料", None),
}
STATUS_OPTIONS = (("", "全部状态"), ("draft", "草稿"), ("published", "已发布"), ("disabled", "已停用"))
USAGE_OPTIONS = (("", "全部用途"), ("internal", "对内"), ("external", "对外"))
AI_OPTIONS = (("", "全部可用性"), ("ready", "AI客服可用"), ("not_ready", "AI客服不可用"))
SOURCE_KIND_LABELS = {"manual": "手工出处", "file": "文件导入"}
# 通知码 → 用户口径中文文案（列表页与详情页共用；界面不直接显示状态码）。
KB_NOTICE_MESSAGES = {
    "saved_draft": "草稿已保存；已发布条目的线上版本不受影响。",
    "published": "已发布。",
    "disabled": "已停用；客户问答不再使用这条内容。",
    "enabled": "已重新启用。",
    "authorized": "授权已更新并立即生效。",
    "restored": "已创建恢复草稿（不覆盖历史版本）。",
    "needs_confirm": "该操作需要确认后才能执行（对外且授权的发布 / 停用 / 重新启用）。",
    "imported": "文件已真实保存为草稿来源，请核对识别内容后保存或发布。",
}

KB_CSS = """<style>
.kb-head{display:flex;justify-content:space-between;align-items:flex-start;gap:16px;margin-bottom:16px;flex-wrap:wrap}
.kb-head h1{margin:0 0 6px}.kb-head p{margin:0;color:var(--text-2);font-size:13px;max-width:720px}
.kb-actions{display:flex;gap:8px;flex-wrap:wrap;align-items:center}
.kb-addmenu{position:relative}.kb-addmenu>summary{list-style:none;display:inline-block;cursor:pointer;border:0;border-radius:var(--r-md);padding:9px 14px;background:var(--accent);color:#fff;font-weight:600;font-size:13px}
.kb-addmenu>summary::-webkit-details-marker{display:none}.kb-addmenu[open]>summary{background:var(--accent-hover)}
.kb-addmenu .kb-menu{position:absolute;right:0;top:calc(100% + 6px);z-index:30;min-width:200px;background:var(--surface);border:1px solid var(--border);border-radius:var(--r-lg);box-shadow:var(--sh-md);padding:6px}
.kb-addmenu .kb-menu a{display:block;padding:9px 10px;border-radius:var(--r-md);color:var(--text-1);font-size:13px}
.kb-addmenu .kb-menu a:hover{background:var(--surface-2);text-decoration:none}
.kb-filterbar{display:flex;gap:10px;flex-wrap:wrap;align-items:flex-end;margin:0 0 16px;padding:12px 14px;background:var(--surface);border:1px solid var(--border);border-radius:var(--r-lg)}
.kb-search{position:relative;flex:1;min-width:220px}
.kb-search input{width:100%;padding:9px 40px 9px 12px;border:1px solid var(--border-strong);border-radius:var(--r-md);background:var(--surface)}
/* 隐藏 WebKit 原生搜索清除「×」：它与可访问的 .kb-clear 按钮重复出双 ×；只在知识搜索内生效，自绘按钮的键盘、焦点与筛选保留行为不变 */
.kb-search input::-webkit-search-cancel-button{-webkit-appearance:none;appearance:none;display:none}
.kb-search .kb-clear{position:absolute;right:6px;top:50%;transform:translateY(-50%);display:grid;place-items:center;width:28px;height:28px;border-radius:var(--r-md);color:var(--text-2);border:0;background:none;padding:0}
.kb-search .kb-clear:hover{background:var(--surface-3);color:var(--text-1)}
.kb-search .kb-clear[hidden]{display:none}
.kb-filterbar label{display:block;font-size:12px;color:var(--text-2);margin:0 0 4px}
.kb-filterbar select,.kb-filterbar input[type=date]{padding:8px 10px;border:1px solid var(--border-strong);border-radius:var(--r-md);background:var(--surface)}
.kb-more{margin:0}
.kb-more>summary{cursor:pointer;font-size:13px;color:var(--text-2);padding:9px 0}
.kb-more .kb-more-body{display:flex;gap:10px;flex-wrap:wrap}
.kb-list{padding:4px 0}
.kb-row{display:flex;align-items:center;gap:14px;padding:14px 6px;border-bottom:1px solid var(--border-subtle);color:inherit;text-decoration:none;cursor:pointer}
.kb-row:hover{background:var(--row-hover)}
.kb-row:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
.kb-row .kb-name{color:var(--text-1);font-weight:500;font-size:14px;display:block;min-width:0;overflow-wrap:anywhere}
.kb-row:hover .kb-name{color:var(--accent)}
.kb-row .row-chev{width:14px;height:14px;color:var(--text-3);transform:rotate(-90deg);flex:none}
.kb-row .kb-grow{flex:1;min-width:0}
.kb-row .kb-meta{font-size:12px;color:var(--text-3);margin-top:3px;display:flex;gap:10px;flex-wrap:wrap}
.kb-tag{display:inline-flex;align-items:center;border-radius:var(--r-sm);padding:2px 8px;font-size:12px;background:#F1F3F6;border:1px solid #E1E5EA;color:#4A5160;flex:none}
.kb-pill{display:inline-flex;align-items:center;gap:6px;border-radius:var(--r-full);padding:3px 11px;font-size:12px;border:1px solid;flex:none;white-space:nowrap}
.kb-pill::before{content:"";width:7px;height:7px;flex:none;border-radius:50%;background:currentColor}
.kb-pill.ok{background:var(--ok-soft);border-color:var(--ok-border);color:var(--ok)}
.kb-pill.pub{background:#EEF2F7;border-color:#C9D4E0;color:#3E4C5E}
.kb-pill.draft{background:var(--surface-3);border-color:var(--border-strong);color:var(--text-2)}
.kb-pill.off{background:var(--danger-soft);border-color:var(--danger-border);color:var(--danger-text)}
.kb-reason{font-size:12px;color:var(--text-2);margin-top:3px}
.kb-empty{text-align:center;padding:44px 16px;color:var(--text-2)}
.kb-empty .button{margin-top:14px}
.kb-detail{display:grid;grid-template-columns:minmax(0,1fr) 280px;gap:22px;align-items:start}
.kb-title-row{display:flex;align-items:center;gap:10px;flex-wrap:wrap}
.kb-title-row h1{margin:0}
.kb-body{background:var(--surface);border:1px solid var(--border);border-radius:var(--r-lg);padding:20px 22px;min-width:0}
.kb-body .kb-text{white-space:pre-wrap;overflow-wrap:anywhere;font-size:14px;line-height:1.7;color:var(--text-1)}
.kb-faq{margin:12px 0;padding:12px 14px;border:1px solid var(--border-subtle);border-radius:var(--r-md);background:var(--surface-2)}
.kb-faq b{display:block;margin-bottom:6px}
.kb-side{display:flex;flex-direction:column;gap:14px}
.kb-side section{padding:16px;background:var(--surface);border:1px solid var(--border);border-radius:var(--r-lg)}
.kb-side h2{font-size:13px;margin:0 0 10px;color:var(--text-2)}
.kb-side dl{font-size:13px}.kb-side dt{color:var(--text-3);margin-top:8px}.kb-side dd{margin:2px 0 0}
.kb-tabs{display:flex;gap:8px;margin:18px 0 12px;flex-wrap:wrap}
.kb-tabs a{padding:7px 12px;border:1px solid var(--border);border-radius:var(--r-md);font-size:13px;color:var(--text-2)}
.kb-tabs a.active{background:var(--surface-3);color:var(--text-1);font-weight:600}
.kb-tabs a:hover{text-decoration:none}
.kb-table{width:100%;border-collapse:collapse;font-size:13px}
.kb-table th{text-align:left;font-weight:500;color:var(--text-3);font-size:12px;padding:8px 10px;border-bottom:1px solid var(--border)}
.kb-table td{padding:9px 10px;border-bottom:1px solid var(--border-subtle);vertical-align:top;overflow-wrap:anywhere}
.kb-field-internal{color:var(--text-3)}
.kb-form label{display:block;font-size:13px;color:var(--text-2);margin:14px 0 5px}
.kb-form input[type=text],.kb-form input[type=date],.kb-form input[type=time],.kb-form select,.kb-form textarea{width:100%;padding:9px 11px;border:1px solid var(--border-strong);border-radius:var(--r-md);background:var(--surface)}
.kb-form textarea{min-height:180px;resize:vertical;line-height:1.7}
.kb-form .kb-grid2{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:0 16px}
.kb-form .kb-grid3{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:0 16px}
.kb-form .kb-hint{font-size:12px;color:var(--text-3);margin:4px 0 0}
.kb-form .kb-actions{margin-top:20px}
.kb-switch{display:inline-flex;gap:6px;margin:0 0 4px}
.kb-switch button{border:1px solid var(--border-strong);background:var(--surface);color:var(--text-2);border-radius:var(--r-md);padding:6px 14px;font-size:13px;font-weight:500;box-shadow:none}
.kb-switch button:hover{background:var(--surface-2);transform:none}
.kb-switch button.active{background:var(--surface-3);color:var(--text-1);font-weight:600;border-color:var(--accent-border)}
.kb-field-row{padding:8px 0;border-bottom:1px dashed var(--border-subtle)}
.kb-field-row:last-child{border-bottom:0}
.kb-check{display:flex;align-items:center;gap:8px;margin-top:14px;font-size:13px;color:var(--text-1)}
.kb-check input{width:auto}
.kb-steps{display:flex;gap:20px;list-style:none;padding:0;margin:0 0 18px;color:var(--text-3);font-size:13px;flex-wrap:wrap}
.kb-steps [aria-current=step]{color:var(--text-1);font-weight:600}
.kb-note{font-size:12.5px;color:var(--text-2);background:var(--surface-2);border:1px solid var(--border-subtle);border-radius:var(--r-md);padding:10px 12px;margin:12px 0}
.kb-readonly{display:flex;align-items:center;gap:8px;background:var(--warn-soft);border:1px solid var(--warn-border);color:var(--warn);border-radius:var(--r-md);padding:10px 12px;font-size:13px;margin:0 0 14px}
.kb-upload{max-width:640px}
.kb-upload input[type=file]{margin-top:10px}
.kb-dlg{width:min(520px,calc(100vw - 40px));padding:24px;border:1px solid var(--border);border-radius:var(--r-lg);background:var(--surface);color:var(--text-1)}
.kb-dlg::backdrop{background:rgba(0,0,0,.45)}
.kb-dlg h2{font-size:15px;margin:0 0 10px}
.kb-dlg p{font-size:13px;color:var(--text-2);margin:0 0 6px}
.kb-dlg .kb-dlg-error{color:var(--danger-text);background:var(--danger-soft);border:1px solid var(--danger-border);border-radius:var(--r-md);padding:9px 11px;margin:10px 0 0}
.kb-dlg .kb-dlg-foot{display:flex;gap:8px;flex-wrap:wrap;justify-content:flex-end;margin-top:18px}
dialog.kb-confirm-dlg{width:min(520px,calc(100vw - 40px));padding:0;color:var(--text-1)}
dialog.kb-confirm-dlg::backdrop{background:rgba(10,16,26,.42)}
.kb-version{margin-top:6px}
.kb-version>summary{cursor:pointer;font-size:12.5px;color:var(--text-2)}
.kb-version-body{padding:10px 0 4px}
.kb-qa-layout{display:grid;grid-template-columns:minmax(0,1fr) 300px;gap:22px;align-items:start}
.kb-qa-shell{background:var(--surface);border:1px solid var(--border);border-radius:var(--r-lg);padding:18px;display:flex;flex-direction:column;gap:12px;min-width:0}
.kb-qa-head{display:flex;align-items:center;gap:10px;padding-bottom:10px;border-bottom:1px solid var(--border-subtle)}
.kb-qa-avatar{display:grid;place-items:center;width:34px;height:34px;flex:none;border-radius:50%;background:var(--surface-3);font-weight:600}
.kb-qa-head strong{display:block;font-size:14px}.kb-qa-head small{color:var(--text-3);font-size:12px}
.kb-qa-log{display:flex;flex-direction:column;gap:10px;min-height:120px}
.kb-qa-msg{display:flex}.kb-qa-msg.right{justify-content:flex-end}
.kb-qa-bubble{max-width:85%;padding:10px 13px;border-radius:var(--r-lg);font-size:13.5px;line-height:1.65;white-space:pre-wrap;overflow-wrap:anywhere;background:var(--surface-2);border:1px solid var(--border-subtle)}
.kb-qa-msg.right .kb-qa-bubble{background:var(--accent-soft);border-color:var(--accent-border)}
.kb-qa-status{border-radius:var(--r-md);padding:10px 12px;font-size:13px;margin:0;line-height:1.6}
.kb-qa-status.warn{background:var(--warn-soft);border:1px solid var(--warn-border);color:var(--warn)}
.kb-qa-status.error{background:var(--danger-soft);border:1px solid var(--danger-border);color:var(--danger-text)}
.kb-cite{display:flex;justify-content:space-between;align-items:center;gap:10px;border:1px solid var(--border);border-radius:var(--r-md);padding:9px 11px;background:var(--surface-2);flex-wrap:wrap}
.kb-cite small{color:var(--text-3)}
.kb-cite-view{margin-top:4px}
.kb-cite-view>summary{cursor:pointer;font-size:12.5px;color:var(--text-2)}
.kb-quote{white-space:pre-wrap;overflow-wrap:anywhere;background:var(--surface-2);border:1px solid var(--border-subtle);border-radius:var(--r-md);padding:10px 12px;font-size:13px;margin:8px 0}
.kb-qa-composer{display:flex;gap:8px;align-items:flex-end}
.kb-qa-composer textarea{flex:1;min-height:44px;max-height:140px;resize:vertical;padding:10px 11px;border:1px solid var(--border-strong);border-radius:var(--r-md);background:var(--surface)}
.kb-qa-rail{padding:16px;background:var(--surface);border:1px solid var(--border);border-radius:var(--r-lg)}
.kb-qa-rail h3{margin:0 0 8px}.kb-qa-rail dl{font-size:12.5px}.kb-qa-rail dt{font-weight:600;margin-top:10px}.kb-qa-rail dd{margin:2px 0 0;color:var(--text-2)}
.kb-sr{position:absolute;width:1px;height:1px;margin:-1px;overflow:hidden;clip:rect(0 0 0 0);white-space:nowrap}
@media(max-width:860px){.kb-detail,.kb-qa-layout{grid-template-columns:1fr}.kb-side,.kb-qa-rail{order:2}}
/* 移动端列表行：徽标行（类型 / 状态 / 箭头）一行、标题与说明整行占满下方一行；
   不再让状态徽标挤占标题行（旧布局把标题/说明压到约 120px 宽）；桌面布局与配色不变 */
@media(max-width:600px){.kb-row{display:grid;grid-template-columns:auto auto minmax(24px,1fr);gap:8px}.kb-row .kb-tag{grid-area:1/1}.kb-row .kb-pill{grid-area:1/2}.kb-row .row-chev{grid-area:1/3;justify-self:end}.kb-row .kb-grow{grid-area:2/1/3/4}.kb-form .kb-grid2,.kb-form .kb-grid3{grid-template-columns:1fr}.kb-filterbar{flex-direction:column;align-items:stretch}.kb-head h1{font-size:21px}}
@media (prefers-reduced-motion:reduce){.kb-*{transition:none!important}}
</style>"""

KB_JS = """<script>
// 未保存保护（编辑器/导入）：站内跳转弹真实三按钮对话框（保存草稿并离开 / 放弃修改 / 继续编辑），
// Escape = 继续编辑；dialog 原生焦点圈与关闭后焦点恢复；减少动画偏好下无额外动效。
// 导入页「保存草稿并离开」提交表单（multipart 携带文件字节），不落 location.href 丢文件；
// 缺文件或缺标题时明确阻止并保留当前内容；不用浏览器输入框式临时询问。
(function(){
  var form=document.querySelector('form[data-unsaved]');
  if(!form){return;}
  var dlg=document.getElementById('kb-leave-dlg');
  var dirty=false;
  var pending=null;
  var restoreFocus=null;
  var titleInput=form.querySelector('input[name=title]');
  var fileInput=form.querySelector('input[type=file]');
  var nextInput=form.querySelector('input[name=next]');
  // 清单9：提交真正送达服务端前清除未保存标记（由下方 submit 监听在确认逻辑之后调用）；
  // 被取消的提交保留脏标记。合法校验（required 等）不通过时浏览器不会触发 submit，内容继续保持保护。
  window.__kbUnsavedGuard={submitted:function(){dirty=false;}};
  function setError(message){
    var el=document.getElementById('kb-leave-error');
    if(!el){return;}
    if(message){el.textContent=message;el.hidden=false;}
    else{el.textContent='';el.setAttribute('hidden','');}
  }
  function openDialog(href){
    pending=href;
    restoreFocus=document.activeElement||null;
    setError('');
    if(dlg&&typeof dlg.showModal==='function'){dlg.showModal();}
  }
  function closeDialog(){
    if(dlg&&dlg.open&&typeof dlg.close==='function'){dlg.close();}
  }
  function restore(){
    var target=restoreFocus;
    restoreFocus=null;
    if(target&&typeof target.focus==='function'){target.focus();}
  }
  form.addEventListener('input',function(){dirty=true;setError('');});
  if(fileInput){fileInput.addEventListener('change',function(){dirty=true;setError('');});}
  window.addEventListener('beforeunload',function(event){if(dirty){event.preventDefault();event.returnValue='';}});
  document.addEventListener('click',function(event){
    var link=event.target&&event.target.closest?event.target.closest('a[href]'):null;
    if(!link||!dirty){return;}
    var href=link.getAttribute('href');
    if(!href||href.charAt(0)!=='/'||href.indexOf('/logout')===0){return;}
    event.preventDefault();
    openDialog(href);
  });
  if(dlg){
    dlg.addEventListener('cancel',function(){pending=null;setError('');}); // Escape：继续编辑
    dlg.addEventListener('close',function(){restore();});
    dlg.addEventListener('click',function(event){
      var button=event.target&&event.target.closest?event.target.closest('[data-kb-leave]'):null;
      if(!button){return;}
      var choice=button.getAttribute('data-kb-leave');
      if(choice==='cancel'){pending=null;setError('');closeDialog();return;}
      if(choice==='discard'){dirty=false;var target=pending;pending=null;closeDialog();if(target){location.href=target;}return;}
      if(choice!=='save'||!pending){return;}
      if(titleInput&&!String(titleInput.value||'').trim()){
        setError('请先填写标题，再保存离开。标题与正文仍保留在当前页面，不会丢失。');
        return;
      }
      if(fileInput&&!(fileInput.files&&fileInput.files.length)){
        setError('请先选择要上传的文件，再保存离开；当前选择不会丢失。');
        return;
      }
      if(!titleInput&&!fileInput){
        setError('当前页面无法自动保存草稿；请使用页面上的保存按钮，或继续编辑。');
        return;
      }
      var href=pending;
      pending=null;
      setError('');
      if(nextInput){nextInput.value=href;}
      var save=form.querySelector('button[name=action][value=save_draft]');
      // 编辑器：点击「保存草稿」按钮提交（带 action=save_draft 与 next）；导入：提交表单（multipart 携带文件）。
      // 提交失败时页面与内容保持原处，为下一次保存重试保留现场。
      if(save&&typeof save.click==='function'){save.click();}
      else if(typeof form.requestSubmit==='function'){form.requestSubmit();}
      else if(typeof form.submit==='function'){form.submit();}
    });
  }
})();
// 搜索清空（列表页）：真实按钮（键盘 Enter/Space 原生可用）；输入时即时显隐；
// 点击只清关键词——提交原筛选表单（其他筛选保留）刷新真实结果，焦点回到输入框（会话标记，一次生效）。
(function(){
  var input=document.getElementById('kb-q');
  if(!input){return;}
  var button=document.querySelector('.kb-clear[data-kb-clear]');
  function sync(){
    if(!button){return;}
    if(String(input.value||'').length){button.removeAttribute('hidden');}
    else{button.setAttribute('hidden','');}
  }
  input.addEventListener('input',sync);
  if(button){
    button.addEventListener('click',function(){
      input.value='';
      try{sessionStorage.setItem('kb-focus-search','1');}catch(error){}
      var form=input.form||(input.closest?input.closest('form'):null);
      var href=button.getAttribute('data-href');
      if(form&&typeof form.requestSubmit==='function'){form.requestSubmit();}
      else if(form&&typeof form.submit==='function'){form.submit();}
      else if(href){location.href=href;}
    });
  }
  try{
    if(sessionStorage.getItem('kb-focus-search')==='1'){
      sessionStorage.removeItem('kb-focus-search');
      input.focus();
    }
  }catch(error){}
})();
// 需要一次确认的操作（对外+授权发布 / 停用 / 重新启用 / 开启 AI 授权）统一走站内可访问确认对话框。
// 原因：浏览器原生 confirm 在部分独立 QA 环境会被直接拦截，且与设计稿弹窗外观不一致；
// 确认后由本脚本写入表单的显式确认字段（confirm_ai / confirmed），服务端仍会再次校验——
// 直接 POST 不带确认字段不会执行动作。取消与 Escape 均不提交，焦点回到触发按钮。
(function(){
  var dlg=document.getElementById('kb-confirm-dlg');
  var titleEl=document.getElementById('kb-confirm-title');
  var messageEl=document.getElementById('kb-confirm-message');
  var iconEl=document.getElementById('kb-confirm-icon');
  var okBtn=document.getElementById('kb-confirm-ok');
  var pending=null;
  var restoreFocus=null;
  var confirmedForm=null;
  function confirmSpec(form, submitter){
    if(form.getAttribute('data-kb-confirm')!==null){
      return {
        title: form.getAttribute('data-kb-confirm-title')||'需要确认',
        message: form.getAttribute('data-kb-confirm-message')||'',
        label: form.getAttribute('data-kb-confirm-label')||'确认',
        danger: form.getAttribute('data-kb-confirm-tone')==='danger',
        field: form.querySelector('[name=confirmed]')
      };
    }
    var submitterValue=submitter?String(submitter.value||''):'';
    if(submitterValue==='publish'){
      var usage=form.querySelector('[name=usage]');
      var auth=form.querySelector('[name=ai_authorized]');
      var confirmField=form.querySelector('[name=confirm_ai]');
      if(confirmField){confirmField.value='';}
      if(usage&&usage.value==='external'&&auth&&auth.checked){
        return {
          title:'发布对外 AI 版本？',
          message:'这条知识为「对外 + 允许 AI 客服使用」，发布后客户问答将引用新版本；请确认内容与出处已核对。',
          label:'确认发布',
          danger:false,
          field:confirmField
        };
      }
      return null;
    }
    if(form.getAttribute('data-kb-product-auth')!==null){
      var enableBox=form.querySelector('[name=authorized]');
      var confirmedField=form.querySelector('[name=confirmed]');
      if(confirmedField){confirmedField.value='';}
      if(form.getAttribute('data-needs-confirm')==='1'&&enableBox&&enableBox.checked){
        return {
          title: form.getAttribute('data-kb-confirm-title')||'允许 AI 客服使用该产品？',
          message: form.getAttribute('data-kb-confirm-message')||'',
          label: form.getAttribute('data-kb-confirm-label')||'开启授权',
          danger:false,
          field:confirmedField
        };
      }
    }
    return null;
  }
  function openDialog(spec, form, submitter){
    if(!dlg||typeof dlg.showModal!=='function'){return false;}
    pending={form:form,submitter:submitter||null,field:spec.field};
    var active=document.activeElement;
    restoreFocus=(active&&active!==document.body)?active:(submitter||null);
    if(pending.field){pending.field.value='';}
    if(titleEl){titleEl.textContent=spec.title;}
    if(messageEl){messageEl.textContent=spec.message;messageEl.hidden=!spec.message;}
    if(okBtn){okBtn.textContent=spec.label;okBtn.className=spec.danger?'button danger':'button';}
    if(iconEl){iconEl.className=spec.danger?'dlg-icon danger':'dlg-icon warn';}
    dlg.showModal();
    return true;
  }
  function closeDialog(){if(dlg&&dlg.open&&typeof dlg.close==='function'){dlg.close();}}
  function restore(){var target=restoreFocus;restoreFocus=null;if(target&&typeof target.focus==='function'){target.focus();}}
  function resubmit(form, submitter){
    if(typeof form.requestSubmit==='function'){
      try{form.requestSubmit(submitter||undefined);return;}catch(error){}
    }
    if(submitter&&submitter.name){
      var hidden=document.createElement('input');
      hidden.type='hidden';hidden.name=submitter.name;hidden.value=submitter.value;
      form.appendChild(hidden);
    }
    if(typeof form.submit==='function'){form.submit();}
  }
  if(dlg){
    dlg.addEventListener('cancel',function(){pending=null;}); // Escape = 取消
    dlg.addEventListener('close',function(){restore();});
    dlg.addEventListener('click',function(event){
      var button=event.target&&event.target.closest?event.target.closest('[data-kb-confirm-act]'):null;
      if(!button||!pending){return;}
      var act=button.getAttribute('data-kb-confirm-act');
      if(act==='cancel'){pending=null;closeDialog();return;}
      if(act!=='ok'){return;}
      var run=pending;
      pending=null;
      if(run.field){run.field.value='1';}
      var guard=window.__kbUnsavedGuard;
      if(guard){guard.submitted();}
      confirmedForm=run.form;
      closeDialog();
      resubmit(run.form, run.submitter);
    });
  }
  document.addEventListener('submit',function(event){
    var form=event.target;
    if(form===confirmedForm){
      confirmedForm=null; // 已确认的重发提交：不再弹窗（防止循环）
    }
    else{
      var spec=confirmSpec(form, event.submitter||null);
      if(spec&&openDialog(spec, form, event.submitter||null)){event.preventDefault();return;}
    }
    // 清单9：提交未被阻止 = 这次保存/发布请求会真正送达服务端 → 清除未保存标记，
    // beforeunload 不再拦截这次合法提交；被取消的提交（上方 return）保留脏标记。
    var guard=window.__kbUnsavedGuard;
    if(guard&&!event.defaultPrevented&&form.getAttribute('data-unsaved')!==null){guard.submitted();}
  });
})();
</script>"""

KB_FORM_SWITCH_JS = """<script>
// 内容形态切换（清单14）：文章↔FAQ 与 产品↔服务。两块内容都保留在 DOM 中，
// 切换只改可见块与隐藏 form 值，已输入内容原样保留；标题标签随形态更新。
// 仅新建编辑器提供切换（编辑既有条目时内容形态固定）。
(function(){
  var form=document.querySelector('form[data-form-switch]');
  if(!form){return;}
  var field=form.querySelector('input[name=form]');
  var blocks=form.querySelectorAll('[data-kb-form-block]');
  var buttons=form.querySelectorAll('button[data-kb-form-switch]');
  var titleLabel=document.getElementById('kb-title-label');
  var formTitle=document.getElementById('kb-form-title');
  function apply(target){
    if(field){field.value=target;}
    for(var i=0;i<blocks.length;i+=1){
      var block=blocks[i].getAttribute('data-kb-form-block');
      if(block===target||block==='shared'){blocks[i].removeAttribute('hidden');}
      else{blocks[i].setAttribute('hidden','');}
    }
    for(var j=0;j<buttons.length;j+=1){
      var active=buttons[j].getAttribute('data-kb-form-switch')===target;
      if(active){buttons[j].setAttribute('aria-pressed','true');buttons[j].classList.add('active');}
      else{buttons[j].setAttribute('aria-pressed','false');buttons[j].classList.remove('active');}
      var label=buttons[j].getAttribute('data-kb-title-label');
      if(active&&label&&titleLabel){titleLabel.textContent=label;}
      var form_name=buttons[j].getAttribute('data-kb-form-title');
      if(active&&form_name&&formTitle){formTitle.textContent=form_name;}
    }
  }
  for(var k=0;k<buttons.length;k+=1){(function(button){button.addEventListener('click',function(){apply(button.getAttribute('data-kb-form-switch'));});})(buttons[k]);}
})();
</script>"""

KB_QA_JS = """<script>
// 问答测试输入框：Enter 发送、Shift+Enter 换行；无 JavaScript 时表单按钮仍可用。
(function(){
  var box=document.getElementById('kb-qa-input');
  if(!box){return;}
  box.addEventListener('keydown',function(event){
    if(event.key==='Enter'&&!event.shiftKey){
      event.preventDefault();
      if(box.form&&typeof box.form.requestSubmit==='function'){box.form.requestSubmit();}
    }
  });
})();
</script>"""


def _fmt_beijing(value: str | None) -> str:
    """把存储的 UTC 到期时间显示为北京时间文案（界面明确标注时区）。"""
    moment = knowledge_expiry_instant(value)
    if moment is None:
        return "长期有效"
    return moment.astimezone(SHANGHAI_TZ).strftime("%Y-%m-%d %H:%M") + "（北京时间）"


class KnowledgeUI:
    """挂在 HTTP Handler 上的知识界面路由（由 business_channel_ui 分派，app.py 仅接入导航）。"""

    # ── 公共帮助 ─────────────────────────────────────────────────────────

    def _kb_can_manage(self) -> bool:
        return bool(getattr(self, "user_id", "")) and self.db.knowledge_can_manage(self.tenant_id, self.user_id)

    def _kb_forbidden(self) -> None:
        from app import esc
        self.send_html(self.console_page(
            "无权限", f"<div class='error'>{esc('当前账号没有编辑企业知识的权限：需要企业管理员或产品管理（products）模块授权。所有成员可阅读本企业知识。')}</div>", "knowledge"), 403)

    def _kb_not_read_only_note(self) -> str:
        if self._kb_can_manage():
            return ""
        return "<div class='kb-readonly'>只读：你不是本企业的知识管理员。内容对所有成员可读；编辑、发布、授权与处理操作需要管理员或产品管理模块权限。</div>"

    def _kb_items(self) -> list[dict]:
        from app import esc
        owner_labels = self._kb_owner_labels()
        entries = []
        for row in self.db.list_knowledge_items(self.tenant_id):
            state = self.db.knowledge_item_ui_state(self.tenant_id, row["id"])
            # 清单4（第一批收尾修复）：列表展示与搜索优先取已发布快照；草稿只在「尚无已发布版本」
            # 时顶替（纯草稿条目本就没有线上内容），已发布条目仍以草稿标记提示待发布修改。
            version = self.db.get_knowledge_version(
                self.tenant_id, row["published_version_id"] or row["draft_version_id"])
            snapshot = json.loads(version["content_json"]) if version else {}
            sources = self.db.list_knowledge_sources(self.tenant_id, row["id"])
            has_file = any(source["kind"] == "file" for source in sources)
            category = version["category"] if version else ""
            entries.append({
                "kind": "knowledge",
                "id": row["id"],
                "form": row["form"],
                "type_label": FORM_LABELS.get(row["form"], row["form"]),
                "title": snapshot.get("title") or "（无标题）",
                "category": category,
                "category_value": category,
                "usage": version["usage"] if version else "internal",
                "search_text": " ".join([
                    str(snapshot.get("title") or ""),
                    str(snapshot.get("body") or ""),
                    " ".join(str(pair.get("question", "")) + str(pair.get("answer", "")) for pair in snapshot.get("faq") or [] if isinstance(pair, dict)),
                    " ".join(source["filename"] for source in sources),
                ]).lower(),
                "updated_at": row["updated_at"],
                "owner": owner_labels.get(row["owner_user_id"], ""),
                "state": state,
                "has_draft": state.get("has_draft", False),
                "has_file": has_file,
                "href": "/admin/knowledge/item/" + row["id"],
            })
        for info in self.db.list_knowledge_product_overview(self.tenant_id):
            # 生命周期仅三态（草稿 / 已发布 / 已停用）：未发布的原产品沿用「草稿」标签，不另造「未发布」。
            state = {"status": "published" if info["published_version"] else "draft", "available": info["available"],
                     "reason": info["reason"], "title": ("已发布（AI客服可用）" if info["available"] else ("已发布（AI客服不可用）" if info["published_version"] else "草稿")),
                     "has_draft": False}
            entries.append({
                "kind": "product",
                "id": info["product_id"],
                "form": "product",
                "type_label": "产品",
                "title": f"{info['model']} · {info['name']}",
                "category": info["category"],
                # 原产品与知识条目共用列表结构：category_value 与 category 同值（列表页两个键都会读）。
                "category_value": info["category"],
                "search_text": f"{info['model']} {info['name']} {info['category']}".lower(),
                "updated_at": info["updated_at"],
                "owner": "",
                "state": state,
                "has_draft": False,
                "has_file": False,
                "href": "/admin/knowledge/product/" + info["product_id"],
                "usage": "external",
            })
        return entries

    def _kb_status_pill(self, state: dict) -> str:
        from app import esc
        label = state["title"]
        if state["status"] == "published":
            css = "ok" if state["available"] else "pub"
        elif state["status"] == "disabled":
            css = "off"
        else:
            css = "draft"
        return f"<span class='kb-pill {css}'>{esc(label)}</span>"

    @staticmethod
    def _kb_flat(value):
        if isinstance(value, (list, tuple)):
            return value[0] if value else ""
        return value

    def _kb_query_link(self, query: dict, **updates) -> str:
        params = {key: self._kb_flat(value) for key, value in query.items() if key not in ("notice", "error")}
        for key, value in updates.items():
            if value is None:
                params.pop(key, None)
            else:
                params[key] = value
        clean = urlencode({key: value for key, value in params.items() if value})
        return "/admin/knowledge" + ("?" + clean if clean else "")

    # ── GET 路由 ─────────────────────────────────────────────────────────

    def knowledge_get(self, path: str, query: dict) -> bool:
        if path == "/admin/knowledge":
            self.knowledge_list_page(query)
            return True
        if path == "/admin/knowledge/new":
            self.knowledge_editor_page(query, item_id=None)
            return True
        if path == "/admin/knowledge/import":
            self.knowledge_import_page(query)
            return True
        if path == "/admin/knowledge/qa":
            self.knowledge_qa_page(query)
            return True
        match = re.fullmatch(r"/admin/knowledge/item/([a-f0-9]{8,64})", path)
        if match:
            self.knowledge_detail_page(match.group(1), query)
            return True
        match = re.fullmatch(r"/admin/knowledge/item/([a-f0-9]{8,64})/edit", path)
        if match:
            self.knowledge_editor_page(query, item_id=match.group(1))
            return True
        match = re.fullmatch(r"/admin/knowledge/product/([a-f0-9]{8,64})", path)
        if match:
            self.knowledge_product_page(match.group(1), query)
            return True
        match = re.fullmatch(r"/admin/knowledge/files/([a-f0-9]{8,64})", path)
        if match:
            self.knowledge_source_download(match.group(1))
            return True
        return False

    # ── 列表页 ───────────────────────────────────────────────────────────

    def knowledge_list_page(self, query: dict) -> None:
        from app import esc
        view = (query.get("view", ["all"])[0] or "all")
        if view not in VIEWS:
            view = "all"
        q = (query.get("q", [""])[0] or "").strip().lower()
        status = query.get("status", [""])[0]
        category = (query.get("category", [""])[0] or "").strip()
        usage = query.get("usage", [""])[0]
        ai = query.get("ai", [""])[0]
        entries = self._kb_items()
        categories = sorted({entry["category_value"] or entry["category"] for entry in entries if (entry["category_value"] or entry["category"])})
        forms = VIEWS[view][1]
        filtered = []
        for entry in entries:
            if view == "files" and not entry["has_file"]:
                continue
            if forms and entry["form"] not in forms:
                continue
            if q and q not in entry["search_text"] and q not in entry["title"].lower():
                continue
            if status and entry["state"]["status"] != status:
                continue
            if category and (entry["category_value"] or entry["category"]) != category:
                continue
            if usage and entry["usage"] != usage:
                continue
            if ai == "ready" and not entry["state"]["available"]:
                continue
            if ai == "not_ready" and entry["state"]["available"]:
                continue
            filtered.append(entry)
        filtered.sort(key=lambda item: item["updated_at"] or "", reverse=True)

        add_html = self._kb_add_controls(view)
        notice = query.get("notice", [""])[0]
        notice_html = f"<div class='notice'>{esc(KB_NOTICE_MESSAGES.get(notice, notice))}</div>" if notice else ""
        error = query.get("error", [""])[0]
        error_html = f"<div class='error'>{esc(error)}</div>" if error else ""

        rows = []
        for entry in filtered:
            meta = []
            if entry["category_value"] or entry["category"]:
                meta.append("分类：" + esc(entry["category_value"] or entry["category"]))
            if entry["kind"] == "knowledge":
                meta.append("负责人：" + esc(entry["owner"] or "—"))
            meta.append("更新：" + esc((entry["updated_at"] or "")[:16]))
            draft_hint = "<span class='kb-reason'>有草稿修改待发布</span>" if entry["has_draft"] else ""
            reason = f"<div class='kb-reason'>不可用原因：{esc(entry['state']['reason'])}</div>" if (entry["state"]["status"] == "published" and not entry["state"]["available"] and entry["state"]["reason"]) else ""
            rows.append(
                f"<a class='kb-row' href='{entry['href']}'>"
                f"<span class='kb-tag'>{esc(entry['type_label'])}</span>"
                f"<span class='kb-grow'><span class='kb-name'>{esc(entry['title'])}</span>"
                f"<span class='kb-meta'>{' · '.join(meta)}</span>{draft_hint}{reason}</span>"
                f"{self._kb_status_pill(entry['state'])}"
                f"<svg class='ic row-chev' viewBox='0 0 24 24' aria-hidden='true'><path d='m6 9 6 6 6-6'/></svg></a>"
            )
        if not rows:
            if view == "files":
                empty_next = "<a class='button' href='/admin/knowledge/import'>上传文件</a>" if self._kb_can_manage() else ""
            else:
                empty_next = f"<a class='button secondary' href='{self._kb_query_link({})}'>清空筛选</a>"
            rows.append(f"<div class='kb-empty'>没有符合条件的内容。{empty_next}</div>")

        clear_href = self._kb_query_link(query, q=None)
        clear_btn = (
            "<button type='button' class='kb-clear' data-kb-clear aria-label='清空搜索' "
            f"data-href='{esc(clear_href)}' title='清空搜索'" + ("" if q else " hidden") + ">"
            "<svg class='ic' viewBox='0 0 24 24' aria-hidden='true'><path d='M18 6 6 18'/><path d='m6 6 12 12'/></svg>"
            "</button>"
        )
        category_options = "".join(f"<option value='{esc(value)}'{' selected' if value == category else ''}>{esc(value)}</option>" for value in ["", *categories])
        def select_options(options, current):
            return "".join(f"<option value='{value}'{' selected' if value == current else ''}>{label}</option>" for value, label in options)
        more_open = " open" if (usage or ai) else ""
        filter_html = f"""
<form class='kb-filterbar' method='get' action='/admin/knowledge'>
<input type='hidden' name='view' value='{esc(view)}'>
<div class='kb-search'><label for='kb-q'>搜索</label><input id='kb-q' type='search' name='q' value='{esc(q)}' placeholder='按标题与正文搜索（不使用 AI）'>{clear_btn}</div>
<div><label for='kb-status'>状态</label><select id='kb-status' name='status'>{select_options(STATUS_OPTIONS, status)}</select></div>
<div><label for='kb-category'>分类</label><select id='kb-category' name='category'>{category_options}</select></div>
<details class='kb-more'{more_open}><summary>更多筛选</summary><div class='kb-more-body'>
<div><label for='kb-usage'>用途</label><select id='kb-usage' name='usage'>{select_options(USAGE_OPTIONS, usage)}</select></div>
<div><label for='kb-ai'>AI 可用性</label><select id='kb-ai' name='ai'>{select_options(AI_OPTIONS, ai)}</select></div>
</div></details>
<button type='submit' class='button secondary'>筛选</button>
</form>"""
        view_titles = {"all": "全部知识", "product_service": "产品与服务", "article_faq": "文章与 FAQ", "files": "文件资料"}
        lead = "企业知识统一入口：文章、FAQ、产品与服务、文件资料都在这里管理；分类用筛选完成，不来回切换后台。"
        if view == "files":
            lead += "「文件资料」视图只显示登记了原文件的知识；原文件默认不对外公开。"
        body = f"""{KB_CSS}
<div class='kb-head'><div><h1>{esc(view_titles[view])}</h1><p>{esc(lead)}</p></div><div class='kb-actions'>{add_html}</div></div>
{self._kb_not_read_only_note()}{notice_html}{error_html}
{filter_html}
<p class='kb-hint' style='margin:-6px 0 10px'>搜索按标题与正文文字匹配（不使用 AI）；AI 客服回答只使用「已发布、对外、已授权、未过期」的资料。</p>
<div class='kb-list'>{''.join(rows)}</div>
{KB_JS}"""
        active = {"all": "knowledge", "product_service": "knowledge_products", "article_faq": "knowledge_articles", "files": "knowledge_files"}[view]
        self.send_html(self.console_page("企业知识", body, active))

    def _kb_add_controls(self, view: str) -> str:
        """分视图新增入口（清单14）：分类视图给出该类的两种形态选择；只有「全部知识」保留通用菜单。"""
        if not self._kb_can_manage():
            return ""
        if view == "product_service":
            return ("<details class='kb-addmenu' data-kb-add='product_service'><summary>添加产品 / 服务</summary>"
                    "<div class='kb-menu'><a href='/admin/knowledge/new?form=product'>添加产品</a>"
                    "<a href='/admin/knowledge/new?form=service'>添加服务</a></div></details>")
        if view == "article_faq":
            return ("<details class='kb-addmenu' data-kb-add='article_faq'><summary>添加文章 / FAQ</summary>"
                    "<div class='kb-menu'><a href='/admin/knowledge/new?form=article'>添加文章</a>"
                    "<a href='/admin/knowledge/new?form=faq'>添加 FAQ</a></div></details>")
        if view == "files":
            return "<a class='button' href='/admin/knowledge/import'>上传文件</a>"
        return ("<details class='kb-addmenu'><summary>添加知识</summary><div class='kb-menu'>"
                "<a href='/admin/knowledge/new?form=article'>添加知识文章</a>"
                "<a href='/admin/knowledge/new?form=faq'>添加 FAQ</a>"
                "<a href='/admin/knowledge/new?form=product'>添加产品 / 服务</a>"
                "<a href='/admin/knowledge/import'>上传文件</a></div></details>")

    def _kb_snapshot_html(self, form_name: str, snapshot: dict) -> str:
        """版本内容快照的只读渲染（详情正文与版本历史共用；无编辑控件）。"""
        from app import esc
        if form_name == "faq":
            pairs = [pair for pair in snapshot.get("faq") or [] if isinstance(pair, dict)]
            return "".join(
                f"<div class='kb-faq'><b>{esc(pair.get('question') or '')}</b><span class='kb-text'>{esc(pair.get('answer') or '')}</span></div>"
                for pair in pairs if str(pair.get("question") or "").strip() or str(pair.get("answer") or "").strip()
            ) or "<p class='kb-reason'>还没有问答内容。</p>"
        content = f"<div class='kb-text'>{esc(snapshot.get('body') or '（还没有正文）')}</div>"
        fields = [field for field in snapshot.get("fields") or [] if isinstance(field, dict)]
        if form_name in ("product", "service") and fields:
            def approval_label(field):
                return "已核准" if field.get("state") == "approved" else "待核对"
            rows = "".join(
                f"<tr><td>{esc(field.get('label') or '')}</td><td>{esc(field.get('value') or '')} {esc(field.get('unit') or '')}</td>"
                f"<td>{'对外' if field.get('audience') != 'internal' else '仅内部'}</td>"
                f"<td>{approval_label(field)}</td><td>{esc(field.get('source') or '—')}</td></tr>" for field in fields)
            content += (f"<table class='kb-table' style='margin-top:14px'><tr><th>要点</th><th>内容</th><th>使用范围</th><th>核准</th><th>出处</th></tr>{rows}</table>"
                        "<p class='kb-hint'>只有「已核准 + 对外」的要点会进入客户问答；待核对与仅内部要点仅在内部展示。</p>")
        return content

    def _kb_leave_dialog(self) -> str:
        """未保存保护对话框（编辑器与导入页共用）：三按钮 + Escape=继续编辑 + 错误说明位。"""
        return (
            "<dialog id='kb-leave-dlg' class='kb-dlg' aria-labelledby='kb-leave-title'>"
            "<h2 id='kb-leave-title'>有未保存的修改</h2>"
            "<p>离开前可以先把内容保存为草稿；也可以放弃这些修改。保存后仍可在详情页继续编辑。</p>"
            "<p class='kb-dlg-error' id='kb-leave-error' hidden></p>"
            "<div class='kb-dlg-foot'>"
            "<button type='button' class='button secondary' data-kb-leave='cancel'>继续编辑</button>"
            "<button type='button' class='button warn' data-kb-leave='discard'>放弃修改</button>"
            "<button type='button' data-kb-leave='save'>保存草稿并离开</button>"
            "</div></dialog>"
        )

    def _kb_owner_labels(self) -> dict[str, str]:
        """企业成员展示名（负责人显示可识别的人名或邮箱，不显示内部用户 ID）。"""
        labels: dict[str, str] = {}
        for row in self.db.conn.execute(
            "SELECT id, display_name, email FROM users WHERE tenant_id=?", (self.tenant_id,)
        ).fetchall():
            labels[row["id"]] = (row["display_name"] or "").strip() or (row["email"] or "").strip()
        return labels

    def _kb_confirm_dialog(self) -> str:
        """需要一次确认的操作对话框（发布 / 停用 / 重新启用 / 开启 AI 授权）。

        外观对齐设计稿的模态卡（.dlg 系列：图标 + 标题 + 说明 + 取消/确认）。
        取消与 Escape 不提交、焦点回到触发按钮；确认后由 KB_JS 写入表单的显式确认字段
        （confirm_ai / confirmed），服务端仍会再次校验——直接 POST 不带确认字段不会执行动作。
        """
        from app import svg_icon
        return (
            "<dialog id='kb-confirm-dlg' class='dlg kb-confirm-dlg' role='alertdialog'"
            " aria-labelledby='kb-confirm-title' aria-describedby='kb-confirm-message'>"
            "<div class='dlg-head'><span class='dlg-icon warn' id='kb-confirm-icon' aria-hidden='true'>"
            f"{svg_icon('alert')}</span>"
            "<div><h3 class='dlg-title' id='kb-confirm-title'></h3>"
            "<p class='dlg-sub' id='kb-confirm-message'></p></div></div>"
            "<div class='dlg-foot'>"
            "<button type='button' class='button secondary' data-kb-confirm-act='cancel'>取消</button>"
            "<button type='button' class='button' id='kb-confirm-ok' data-kb-confirm-act='ok'></button>"
            "</div></dialog>"
        )

    # ── 详情页 ───────────────────────────────────────────────────────────

    def knowledge_detail_page(self, item_id: str, query: dict) -> None:
        from app import esc
        item = self.db.get_knowledge_item(self.tenant_id, item_id)
        if not item:
            self.send_error(404)
            return
        state = self.db.knowledge_item_ui_state(self.tenant_id, item_id)
        published = self.db.get_published_knowledge_version(self.tenant_id, item_id)
        draft = self.db.get_knowledge_draft_version(self.tenant_id, item_id)
        # 清单4：默认展示当前生效的已发布版本；草稿只以提示与编辑入口出现，不顶替线上内容。
        version = published or draft
        snapshot = json.loads(version["content_json"]) if version else {}
        if published is not None:
            version_line = f"当前版本 v{published['version']}"
        elif draft is not None:
            version_line = f"草稿 v{draft['version']}（尚未发布）"
        else:
            version_line = "当前版本 —"
        tab = query.get("tab", ["content"])[0]
        notice = query.get("notice", [""])[0]
        # 通知码 → 用户口径中文文案（详情页既接收通知码也接收已编码的中文消息）。
        notice = KB_NOTICE_MESSAGES.get(notice, notice)
        error = query.get("error", [""])[0]
        notice_html = f"<div class='notice'>{esc(notice)}</div>" if notice else ""
        error_html = f"<div class='error'>{esc(error)}</div>" if error else ""
        content_html = self._kb_snapshot_html(item["form"], snapshot)

        reason_html = f"<div class='kb-reason'>不可用原因：{esc(state['reason'])}</div>" if (state['status'] == 'published' and not state['available'] and state['reason']) else ""
        draft_banner = ""
        if draft is not None and item["status"] == "published" and published is not None:
            draft_banner = (
                f"<div class='kb-note kb-draft-banner'>有草稿修改待发布（草稿 v{draft['version']}）：线上版本 v{published['version']} 继续对外服务，发布后才会替换。"
                f" <a href='/admin/knowledge/item/{item_id}/edit'>继续编辑</a></div>"
            )
        sources = self.db.list_knowledge_sources(self.tenant_id, item_id)
        if sources:
            file_count = sum(1 for source in sources if source["storage_path"])
            content_html += f"<p class='kb-hint' style='margin-top:14px'>来源：{len(sources)} 项" + (f"（其中 {file_count} 个原件已私有保存，可在「来源」页签下载核对）" if file_count else "") + "。</p>"
        processing = None
        if item["published_version_id"]:
            processing = self.db.knowledge_processing_state(self.tenant_id, item["published_version_id"])
        owner_label = self._kb_owner_labels().get(item["owner_user_id"] or "", "")
        actions = self._kb_detail_actions(item, state, processing, draft, published)
        attribute_bar = self._kb_attribute_bar(item, version)
        tabs_html = ""
        if tab == "versions":
            tabs_html = self._kb_versions_tab(item)
        elif tab == "sources":
            tabs_html = self._kb_sources_tab(item)
        body = f"""{KB_CSS}
<div class='kb-head'><div class='kb-title-row'><h1>{esc(snapshot.get('title') or '（无标题）')}</h1><span class='kb-tag'>{esc(FORM_LABELS.get(item['form'], item['form']))}</span>{self._kb_status_pill(state)}</div>
<p>{version_line} · 负责人 {esc(owner_label or '—')} · 更新 {esc(item['updated_at'][:16])}</p>{reason_html}{draft_banner}</div>
<div class='kb-actions'>{actions}</div></div>
{self._kb_not_read_only_note()}{notice_html}{error_html}
<div class='kb-detail'>
<div>
<section class='kb-body'>{content_html}</section>
<div class='kb-tabs'><a href='/admin/knowledge/item/{item_id}' class='{'active' if tab == 'content' else ''}'>内容</a>
<a href='/admin/knowledge/item/{item_id}?tab=sources' class='{'active' if tab == 'sources' else ''}'>来源</a>
<a href='/admin/knowledge/item/{item_id}?tab=versions' class='{'active' if tab == 'versions' else ''}'>版本历史</a></div>
{tabs_html}
</div>
{attribute_bar}
</div>
{self._kb_confirm_dialog()}
{KB_JS}"""
        self.send_html(self.console_page("知识详情", body, "knowledge"))

    @staticmethod
    def _kb_ai_confirm_attrs(version, title: str, message: str, label: str) -> str:
        """R16：只有「对外 + 已授权」的版本发布 / 重新启用才先弹站内确认；对内或未授权不弹额外确认。
        服务端仍强制校验显式确认字段（本函数只决定界面是否先给确认对话框，不放松后端校验）。"""
        if not (version and version["usage"] == "external" and version["ai_authorized"]):
            return ""
        return (f" data-kb-confirm data-kb-confirm-title='{title}'"
                f" data-kb-confirm-message='{message}'"
                f" data-kb-confirm-label='{label}'")

    def _kb_detail_actions(self, item, state, processing, draft, published) -> str:
        from app import esc
        if not self._kb_can_manage():
            return ""
        item_id = item["id"]
        actions = []
        has_draft = bool(item["draft_version_id"])
        # R16 确认范围（与后端条件一致）：发布按「待发布的草稿版本」判定、重新启用按「生效的已发布版本」判定，
        # 两者都只在「对外 + 已授权」时先弹确认；对内或未授权直接执行。停用始终确认（不受此影响）。
        publish_confirm = self._kb_ai_confirm_attrs(
            draft, "发布这条知识？",
            "发布后，对外且已授权的内容将进入客户问答候选用途；线上版本会被新版本替换。", "确认发布")
        enable_confirm = self._kb_ai_confirm_attrs(
            published, "重新启用这条知识？",
            "重新启用后恢复为已发布；若为对外且已授权，将立即进入客户问答候选用途。", "重新启用")
        if item["status"] == "published":
            # 清单4（第一批收尾修复）：已发布条目只要仍是已发布状态就必须保留「停用」，
            # 即使有草稿待发布也不得被草稿分支顶掉（停用不要求先发布草稿；草稿与历史照常保留）。
            actions.append(f"<a class='button secondary' href='/admin/knowledge/item/{item_id}/edit'>编辑（存为草稿）</a>")
            if has_draft:
                actions.append(
                    f"<form method='post' action='/admin/knowledge/item/{item_id}/publish'{publish_confirm}>"
                    "<input type='hidden' name='confirmed' value=''><button>发布</button></form>")
            actions.append(
                f"<form method='post' action='/admin/knowledge/item/{item_id}/disable'"
                " data-kb-confirm data-kb-confirm-tone='danger' data-kb-confirm-title='停用这条知识？'"
                " data-kb-confirm-message='停用后，该知识立即不再用于客户问答；历史与版本保留。'"
                " data-kb-confirm-label='停用'>"
                "<input type='hidden' name='confirmed' value=''><button class='warn'>停用</button></form>")
        elif item["status"] == "draft":
            actions.append(f"<a class='button' href='/admin/knowledge/item/{item_id}/edit'>编辑</a>")
            actions.append(
                f"<form method='post' action='/admin/knowledge/item/{item_id}/publish'{publish_confirm}>"
                "<input type='hidden' name='confirmed' value=''><button>发布</button></form>")
        if item["status"] == "disabled":
            actions.append(
                f"<form method='post' action='/admin/knowledge/item/{item_id}/enable'{enable_confirm}>"
                "<input type='hidden' name='confirmed' value=''><button>重新启用</button></form>")
        if processing and processing["status"] == "failed":
            actions.append(f"<form method='post' action='/admin/knowledge/item/{item_id}/retry'><button class='warn'>重试处理</button></form>")
        if processing and processing["status"] == "pending":
            actions.append("<span class='kb-reason'>准备中：等待真实本地内容准备完成；完成后才进入客户问答候选</span>")
        return "".join(actions)

    def _kb_attribute_bar(self, item, version) -> str:
        from app import esc
        item_id = item["id"]
        usage = version["usage"] if version else "internal"
        authorized = bool(version["ai_authorized"]) if version else False
        category = version["category"] if version else ""
        expiry = _fmt_beijing(version["expires_at"]) if version else "长期有效"
        relation = ""
        if item["product_id"]:
            product = self.db.get_product(self.tenant_id, item["product_id"])
            if product:
                relation = f"<dd><a href='/admin/knowledge/product/{product['id']}'>{esc(product['model'])} · {esc(product['name'])}</a></dd>"
        else:
            relation = "<dd>—（独立知识，不关联产品）</dd>"
        toggle = ""
        if self._kb_can_manage():
            if authorized:
                toggle = f"<form method='post' action='/admin/knowledge/item/{item_id}/authorize'><input type='hidden' name='authorized' value='0'><button class='warn'>关闭 AI 授权</button></form>"
            else:
                toggle = (
                    f"<form method='post' action='/admin/knowledge/item/{item_id}/authorize'"
                    " data-kb-confirm data-kb-confirm-title='允许 AI 客服使用？'"
                    " data-kb-confirm-message='开启后，对已发布且对外的内容将立即进入 AI 客服候选用途。'"
                    " data-kb-confirm-label='开启授权'>"
                    "<input type='hidden' name='authorized' value='1'>"
                    "<input type='hidden' name='confirmed' value=''><button>允许 AI 客服使用</button></form>")
        else:
            toggle = f"<span class='kb-reason'>AI 授权：{'已开启' if authorized else '未开启'}</span>"
        return f"""<aside class='kb-side'><section>
<h2>属性</h2><dl>
<dt>分类</dt><dd>{esc(category) or '—'}</dd>
<dt>用途</dt><dd>{'对外' if usage == 'external' else '对内'}</dd>
<dt>允许 AI 客服使用</dt><dd>{'已授权' if authorized else '未授权'}</dd>
<dt>到期时间</dt><dd>{esc(expiry)}</dd>
<dt>关联对象</dt>{relation}
</dl></section><section><h2>操作</h2>{toggle}<p class='kb-hint'>授权与停用立即生效，不影响历史版本与同事阅读。</p></section></aside>"""

    def _kb_versions_tab(self, item) -> str:
        from app import esc
        rows = []
        for version in self.db.list_knowledge_versions(self.tenant_id, item["id"]):
            is_current = version["id"] == item["published_version_id"]
            actions = ""
            if self._kb_can_manage() and not is_current and version["status"] != "draft" and not item["draft_version_id"]:
                actions = f"<form method='post' action='/admin/knowledge/item/{item['id']}/restore'><input type='hidden' name='version' value='{version['version']}'><button class='button secondary'>恢复为草稿</button></form>"
            snapshot = json.loads(version["content_json"] or "{}")
            state_label = {"draft": "草稿（未发布）", "published": "已发布", "superseded": "历史版本（已被替换）"}.get(version["status"], version["status"])
            meta_bits = [
                "分类：" + version["category"] if version["category"] else "",
                "对外" if version["usage"] == "external" else "对内",
                "已授权 AI 客服" if version["ai_authorized"] else "未授权 AI 客服",
                _fmt_beijing(version["expires_at"]),
                f"恢复自 v{version['restored_from_version']}" if version["restored_from_version"] else "",
            ]
            meta_line = " · ".join(bit for bit in meta_bits if bit)
            viewer = (
                f"<details class='kb-version'><summary>查看内容（v{version['version']}）</summary>"
                f"<div class='kb-version-body'>{self._kb_snapshot_html(item['form'], snapshot)}"
                f"<p class='kb-hint'>{esc(meta_line)}</p></div></details>"
            )
            rows.append(
                f"<tr><td>v{version['version']}</td><td>{esc(state_label)}</td><td>{esc(version['created_by'])}</td><td>{esc((version['created_at'] or '')[:16])}</td><td>{'当前线上版本' if is_current else ''}</td><td>{actions}</td></tr>"
                f"<tr class='kb-version-row'><td colspan='6'>{viewer}</td></tr>"
            )
        return f"<section class='kb-body'><h2>版本历史</h2><p class='kb-hint'>每行可展开查看该版本的完整内容快照；恢复历史版本会创建新草稿（注明恢复来源），不覆盖历史、不影响线上版本。</p><table class='kb-table'><tr><th>版本</th><th>状态</th><th>作者</th><th>时间</th><th></th><th></th></tr>{''.join(rows)}</table></section>"

    def _kb_sources_tab(self, item) -> str:
        from app import esc
        rows = []
        sources = self.db.list_knowledge_sources(self.tenant_id, item["id"])
        for source in sources:
            download = f"<a href='/admin/knowledge/files/{source['id']}'>下载原件</a>" if source["storage_path"] else "<span class='kb-reason'>未附文件</span>"
            rows.append(f"<tr><td>{esc(source['filename'] or '—')}</td><td>{esc(SOURCE_KIND_LABELS.get(source['kind'], source['kind']))}</td><td>{esc(source['source_location'])}</td><td>{esc((source['created_at'] or '')[:16]) if 'created_at' in source.keys() else ''}</td><td>{download}</td></tr>")
        if not rows:
            rows.append("<tr><td colspan='5' class='kb-reason'>还没有来源记录。导入文件或手工创建时会登记来源。</td></tr>")
        return f"<section class='kb-body'><h2>来源</h2><p class='kb-hint'>对外知识 ≠ 公开原文件下载：原件仅本企业成员可读。</p><table class='kb-table'><tr><th>文件 / 说明</th><th>类型</th><th>出处</th><th>登记时间</th><th></th></tr>{''.join(rows)}</table></section>"

    # ── 编辑器 ───────────────────────────────────────────────────────────

    def knowledge_editor_page(self, query: dict, item_id: str | None, *, error: str = "", values: dict | None = None) -> None:
        from app import esc
        if not self._kb_can_manage():
            body = f"{KB_CSS}<h1>编辑知识</h1>{self._kb_not_read_only_note()}<p class='kb-reason'>没有编辑权限；你可以阅读该内容，但保存、发布与授权需要管理员或产品管理模块权限。</p>"
            self.send_html(self.console_page("编辑知识", body, "knowledge"))
            return
        item = self.db.get_knowledge_item(self.tenant_id, item_id) if item_id else None
        if item_id and not item:
            self.send_error(404)
            return
        form = item["form"] if item else (query.get("form", ["article"])[0] or "article")
        if form not in ("article", "faq", "product", "service"):
            form = "article"
        version = None
        snapshot = {}
        if item:
            version = self.db.get_knowledge_version(self.tenant_id, item["draft_version_id"] or item["published_version_id"])
            snapshot = json.loads(version["content_json"]) if version else {}
        values = values or {}
        title = values.get("title", snapshot.get("title") or "")
        body_text = values.get("body", snapshot.get("body") or "")
        category = values.get("category", (version["category"] if version else ""))
        usage = values.get("usage", (version["usage"] if version else "internal"))
        authorized = values.get("ai_authorized", bool(version["ai_authorized"]) if version else False)
        expiry_value = version["expires_at"] if version else None
        beijing = knowledge_expiry_instant(expiry_value)
        expiry_date = values.get("expiry_date", beijing.astimezone(SHANGHAI_TZ).strftime("%Y-%m-%d") if beijing else "")
        expiry_time = values.get("expiry_time", beijing.astimezone(SHANGHAI_TZ).strftime("%H:%M") if beijing else "")
        categories = sorted({
            version_row["category"]
            for item_row in self.db.list_knowledge_items(self.tenant_id)
            for version_row in self.db.list_knowledge_versions(self.tenant_id, item_row["id"])
            if version_row["category"]
        })
        faq_pairs = snapshot.get("faq") or []
        faq_inputs = ""
        for index in range(max(4, len(faq_pairs))):
            pair = faq_pairs[index] if index < len(faq_pairs) and isinstance(faq_pairs[index], dict) else {}
            faq_inputs += (f"<div class='kb-grid2'><div><label>问题 {index + 1}</label><input type='text' name='faq_q_{index}' value='{esc(pair.get('question') or '')}'></div>"
                           f"<div><label>回答 {index + 1}</label><textarea name='faq_a_{index}' style='min-height:72px'>{esc(pair.get('answer') or '')}</textarea></div></div>")
        field_rows = ""
        if form in ("product", "service"):
            field_list = [field for field in snapshot.get("fields") or [] if isinstance(field, dict)]
            for index in range(max(2, len(field_list))):
                field = field_list[index] if index < len(field_list) else {}
                # 清单12：逐要点显式「使用范围 + 核准 + 出处」；新行默认 对外 + 已核对（可见控件，可改），
                # 既有行按已存状态回显（缺核准标记的旧数据不勾选，不会被静默视为已核准）。
                is_new_row = not field
                audience = "external" if is_new_row else (field.get("audience") if field.get("audience") in ("external", "internal") else "external")
                approved_checked = is_new_row or field.get("state") == "approved"
                source_value = "" if is_new_row else str(field.get("source") or "")
                field_rows += (
                    f"<div class='kb-field-row'><div class='kb-grid3'>"
                    f"<div><label>要点名称 {index + 1}</label><input type='text' name='field_label_{index}' value='{esc(field.get('label') or '')}'></div>"
                    f"<div><label>内容</label><input type='text' name='field_value_{index}' value='{esc(field.get('value') or '')}'></div>"
                    f"<div><label>单位（可选）</label><input type='text' name='field_unit_{index}' value='{esc(field.get('unit') or '')}'></div></div>"
                    f"<div class='kb-grid3'>"
                    f"<div><label>使用范围</label><select name='field_audience_{index}'>"
                    f"<option value='external'{' selected' if audience == 'external' else ''}>对外</option>"
                    f"<option value='internal'{' selected' if audience == 'internal' else ''}>仅内部</option></select></div>"
                    f"<div><label>出处（来源说明）</label><input type='text' name='field_source_{index}' value='{esc(source_value)}' placeholder='例如：人工录入 / 资料名称与位置'></div>"
                    f"<div><label class='kb-check' style='margin-top:22px'><input type='checkbox' name='field_approved_{index}' value='1'{' checked' if approved_checked else ''}>已核对（核准）</label></div>"
                    f"</div></div>"
                )
        body_label = "服务说明（自由文本）" if form == "service" else "说明 / 规格（自由文本，行业中立）"
        article_block = f"<label>{body_label}</label><textarea name='body'>{esc(body_text)}</textarea>"
        fields_hint = "<p class='kb-hint'>要点可选填：勾选「已核对」且「使用范围 = 对外」的要点才会进入 AI 客服回答；「仅内部」与未勾选的要点只在详情内供内部查看。</p>"
        form_switch = ""
        if item:
            content_inputs = f"<div data-kb-form-block='{form}'>{faq_inputs if form == 'faq' else article_block + (fields_hint + field_rows if form in ('product', 'service') else '')}</div>"
        else:
            # 新建（清单14）：两种形态的编辑区都保留在 DOM 中，切换只改可见块与隐藏 form 值，已输入内容不丢；
            # 产品 / 服务的字段模板相同，共用一个内容块，切换只影响隐藏 form 值与标题标签。
            pairs = {
                "article": ("文章", "文章标题", "添加文章"),
                "faq": ("FAQ", "FAQ 标题", "添加 FAQ"),
                "product": ("产品", "产品 / 服务名称", "添加产品"),
                "service": ("服务", "服务名称", "添加服务"),
            }
            if form in ("article", "faq"):
                options = ("article", "faq")
                block_article = f"<div data-kb-form-block='article'{'' if form == 'article' else ' hidden'}>{article_block}</div>"
                block_faq = f"<div data-kb-form-block='faq'{'' if form == 'faq' else ' hidden'}>{faq_inputs}</div>"
                content_inputs = block_article + block_faq
            else:
                options = ("product", "service")
                content_inputs = f"<div data-kb-form-block='shared'>{article_block}{fields_hint}{field_rows}</div>"
            buttons = []
            for option in options:
                short, title_text, form_title = pairs[option]
                active = option == form
                buttons.append(
                    f"<button type='button' class='{'active' if active else ''}' data-kb-form-switch='{option}' "
                    f"data-kb-title-label='{title_text}' data-kb-form-title='{form_title}' aria-pressed='{'true' if active else 'false'}'>{short}</button>")
            form_switch = "<div class='kb-switch' role='group' aria-label='内容形态'>" + "".join(buttons) + "</div>"
        title_label = {"article": "文章标题", "faq": "FAQ 标题", "product": "产品 / 服务名称", "service": "服务名称"}[form]
        saved_notice = "<div class='notice'>已保存草稿；已发布条目的线上版本保持不变。</div>" if query.get("saved") else ""
        imported_notice = ""
        if query.get("from") == ["import"]:
            imported_notice = "<div class='kb-note'>第 2 步 · 检查识别内容：核对标题与正文，然后保存草稿或发布（第 3 步）。文件原件已私有保存，可在详情「来源」页签下载核对。</div>"
        error_html = f"<div class='error'>{esc(error)}</div>" if error else ""
        action_path = f"/admin/knowledge/item/{item['id']}/edit" if item else "/admin/knowledge/new"
        meta_row = ""
        if item:
            state = self.db.knowledge_item_ui_state(self.tenant_id, item["id"])
            meta_row = f"<p class='kb-meta'>当前状态：{esc(state['title'])}{' · 不可用原因：' + esc(state['reason']) if state['reason'] and state['status'] == 'published' and not state['available'] else ''}</p>"
        steps = "<ol class='kb-steps'>" + ("<li aria-current='step'>1 选择文件</li><li aria-current='step'>2 检查识别内容</li><li>3 保存草稿或发布</li>" if item and query.get("from") == ["import"] else "") + "</ol>"
        body = f"""{KB_CSS}
<form class='kb-form' method='post' action='{action_path}' data-unsaved{'' if item else ' data-form-switch'}>
<input type='hidden' name='form' value='{esc(form)}'>
<input type='hidden' name='next' value=''>
<input type='hidden' name='confirm_ai' value=''>
<div class='kb-head'><div><h1 id='kb-form-title'>{'编辑' if item else '添加'}{esc(FORM_LABELS.get(form, '知识'))}</h1>{meta_row}</div></div>
{self._kb_not_read_only_note()}{error_html}{saved_notice}{imported_notice}{steps}
{form_switch}
<label for='kb-title' id='kb-title-label'>{title_label}</label><input id='kb-title' type='text' name='title' value='{esc(title)}' required>
{content_inputs}
<div class='kb-grid2'>
<div><label for='kb-category'>分类（可直接输入主题）</label><input id='kb-category' type='text' name='category' value='{esc(category)}' list='kb-categories' placeholder='例如：服务政策'>
<datalist id='kb-categories'>{''.join(f'<option value="{esc(value)}"></option>' for value in categories)}</datalist></div>
<div><label for='kb-usage'>用途</label><select id='kb-usage' name='usage'><option value='internal'{" selected" if usage == "internal" else ""}>对内</option><option value='external'{" selected" if usage == "external" else ""}>对外</option></select></div>
</div>
<div class='kb-grid2'>
<div><label for='kb-expiry-date'>到期日期（可选；留空 = 长期有效）</label><input id='kb-expiry-date' type='date' name='expiry_date' value='{esc(expiry_date)}'></div>
<div><label for='kb-expiry-time'>到期时间（可选，精确到分钟）</label><input id='kb-expiry-time' type='time' name='expiry_time' value='{esc(expiry_time)}'></div>
</div>
<p class='kb-hint'>到期时间按北京时间填写（可选；留空 = 长期有效）。到期后该条不再用于 AI 客服回答，同事阅读与历史版本不受影响。</p>
<label class='kb-check'><input type='checkbox' name='ai_authorized' value='1'{" checked" if authorized else ""}>允许 AI 客服使用（发布后生效；对已发布且对外的内容开启时会先确认）</label>
<div class='kb-actions'>
<button type='submit' name='action' value='save_draft' class='secondary'>保存草稿</button>
<button type='submit' name='action' value='publish'>发布</button>
<a class='button secondary' href='{f"/admin/knowledge/item/{item['id']}" if item else "/admin/knowledge"}'>取消</a>
</div>
</form>
{self._kb_leave_dialog()}
{self._kb_confirm_dialog()}
{KB_JS}
{KB_FORM_SWITCH_JS if not item else ''}"""
        self.send_html(self.console_page("编辑知识", body, "knowledge"))

    # ── 导入页（3 步）──────────────────────────────────────────────────────

    def knowledge_import_page(self, query: dict) -> None:
        from app import esc
        if not self._kb_can_manage():
            self.send_html(self.console_page("导入文件", f"{KB_CSS}<h1>导入文件</h1>{self._kb_not_read_only_note()}<p class='kb-reason'>没有导入权限：需要管理员或产品管理模块权限。</p>", "knowledge"))
            return
        error = query.get("error", [""])[0]
        error_html = f"<div class='error'>{esc(error)}</div>" if error else ""
        body = f"""{KB_CSS}
<div class='kb-head'><div><h1>上传文件</h1><p>真实保存原件并核对识别内容，然后保存草稿或发布。</p></div></div>
{self._kb_not_read_only_note()}{error_html}
<ol class='kb-steps'><li aria-current='step'>1 选择文件</li><li>2 检查识别内容</li><li>3 保存草稿或发布</li></ol>
<section class='kb-body kb-upload'>
<p class='kb-note'>TXT / Markdown / CSV（≤1MB）会读取为可核对文本；PDF 与 Word 只登记原件、由人工整理正文（自动解析属后续批次）。原件仅本企业成员可读；对外知识 ≠ 公开原文件下载。</p>
<form method='post' action='/admin/knowledge/import' enctype='multipart/form-data' data-unsaved>
<input type='hidden' name='next' value=''>
<label for='kb-file'>选择文件</label><input id='kb-file' type='file' name='file' accept='.txt,.md,.markdown,.csv,.pdf,.doc,.docx' required>
<p class='kb-hint'>选择文件后点击上传：下一页会显示识别内容供你核对修改。</p>
<div class='kb-actions'><button>上传并预览</button><a class='button secondary' href='/admin/knowledge?view=files'>取消</a></div>
</form>
</section>
{self._kb_leave_dialog()}
{KB_JS}"""
        self.send_html(self.console_page("导入文件", body, "knowledge_files"))

    # ── 问答测试（任务4：客户视角；真实状态、明确错误与精确版本引用查看器）────────

    def knowledge_qa_page(self, query: dict, *, result: dict | None = None, question: str = "") -> None:
        from app import esc
        ai = self.ai_for(self.tenant_id)
        if ai is not None and getattr(ai, "mode", "") == "deepseek":
            model_line = (
                f"<div class='notice' role='status'>模型已接通：{esc(ai.config.provider)} / {esc(ai.config.model)}。"
                "回答只引用「已发布 + 对外 + 已授权 + 未过期 + 当前版本准备完成」的资料原文；引用带真实版本。</div>"
            )
        else:
            model_line = (
                "<div class='kb-qa-status error' role='status'>模型未连接：当前部署为本地规则模式（AI_MODE=rules）；"
                "请在企业设置连接模型（AI_MODE=deepseek）。页面不会生成任何模拟答复："
                "接入真实模型前，客户视角问答测试保持未验收。</div>"
            )
        evidence = self.db.list_customer_qa_evidence(self.tenant_id)
        knowledge_count = sum(1 for unit in evidence if unit["source"] == "knowledge")
        corpus_line = (
            f"<p class='kb-meta'>当前可检索证据：{len(evidence)} 条（知识 {knowledge_count} · 产品字段 {len(evidence) - knowledge_count}）；"
            "与网页、已开启渠道共用同一过滤与生成后复核。</p>"
        )
        result_html = self._kb_qa_result_html(result, question) if result else ""
        placeholder_hint = "" if result else "<p class='kb-reason'>输入一条客户会问的问题，查看真实检索结果与引用；不使用预置答复。</p>"
        body = f"""{KB_CSS}
<div class='kb-head'><div><h1>问答测试 · 客户视角</h1><p>以客户身份验证企业知识：只有完全合格（已发布 + 对外 + 已授权 + 未过期 + 当前版本准备完成）的资料会被检索；产品字段再按「对外且已核准 + 出处可用」过滤。</p></div></div>
{self._kb_not_read_only_note()}
{model_line}
{corpus_line}
<div class='kb-qa-layout'>
<section class='kb-qa-shell'>
<div class='kb-qa-head'><span class='kb-qa-avatar' aria-hidden='true'>客</span><div><strong>客户（模拟）</strong><small>来源：企业官网 / 已开启渠道的客户问答；本页为内部测试页</small></div></div>
<div class='kb-qa-log' aria-live='polite'>{placeholder_hint}{result_html}</div>
<form class='kb-qa-composer' method='post' action='/admin/knowledge/qa'>
<label class='kb-sr' for='kb-qa-input'>以客户身份提问</label>
<textarea id='kb-qa-input' name='question' rows='1' placeholder='以客户身份提问（Enter 发送，Shift+Enter 换行）' required>{esc(question)}</textarea>
<button>发送</button>
</form>
</section>
<aside class='kb-qa-rail'><h3>规则说明</h3><dl>
<div><dt>检索范围</dt><dd>本企业 + 已发布 + 对外 + 已授权 + 未过期 + 当前版本准备完成；产品字段再按「对外且已核准且出处可用」过滤。模型选择后、输出前还会再复核一次当前状态。</dd></div>
<div><dt>资料不足时</dt><dd>提示建议转人工，不猜测、不编造。按现有买方流程，只有客户主动请求工程师核实才会创建人工任务；本页不自动建任务。</dd></div>
<div><dt>与全文搜索的区别</dt><dd>列表页搜索按文字匹配（不使用 AI）；本页展示客户问答的检索、引用与版本。</dd></div>
<div><dt>管理员的内部资料</dt><dd>不会出现在这里的任何回答中——模拟客户视角只使用对外授权知识。</dd></div>
</dl></aside>
</div>
{KB_QA_JS}"""
        self.send_html(self.console_page("问答测试", body, "knowledge_qa"))

    def _kb_qa_result_html(self, result: dict, question: str) -> str:
        from app import esc
        parts = [f"<div class='kb-qa-msg right'><div class='kb-qa-bubble'>{esc(question)}</div></div>"]
        status = result.get("status")
        if status == "grounded":
            citation = (result.get("citations") or [{}])[0]
            if citation.get("kind") == "product_field":
                link = f"/admin/knowledge/product/{esc(citation.get('product_id'))}"
                source_line = f"产品字段 · 出处：{esc(citation.get('filename') or '')}"
            else:
                link = f"/admin/knowledge/item/{esc(citation.get('item_id'))}"
                source_line = f"知识条目 · {esc(citation.get('form_label') or '')}"
            version_line = (
                "版本 " + esc(citation.get("version") or "")
                + " · 版本行 " + esc(citation.get("version_id") or "")
                + (" · 发布时间 " + esc(citation.get("published_at")) if citation.get("published_at") else "")
                + " · 生成后已复核：该版本仍为当前有效版本"
            )
            parts.append(
                f"<div class='kb-qa-msg left'><div class='kb-qa-bubble'>{esc(result.get('answer') or '')}</div>"
                f"<div class='kb-cite'><span><b>{esc(citation.get('title') or '')}</b> "
                f"<small>{source_line} · {esc(citation.get('version') or '')}</small></span>"
                f"<a class='button secondary' href='{link}'>查看资料</a></div>"
                f"<details class='kb-cite-view'><summary>引用原文与版本（{esc(citation.get('version') or '')}）</summary>"
                f"<p class='kb-quote'>{esc(citation.get('quote') or '')}</p>"
                f"<p class='kb-hint'>{version_line}</p></details></div>"
            )
        elif status == "model_not_connected":
            parts.append(f"<div class='kb-qa-status error' role='status'>模型未连接：{esc(result.get('reason') or '')}</div>")
        elif status == "error":
            if result.get("error_kind") in ("empty_question", "too_long"):
                parts.append(f"<div class='kb-qa-status error' role='status'>{esc(result.get('reason') or '')}</div>")
            else:
                hint = {"retry": "可稍后重试", "configure": "请管理员检查模型配置后重试", "investigate": "请联系平台排查"}.get(
                    result.get("error_action") or "", "可稍后重试")
                parts.append(
                    f"<div class='kb-qa-status error' role='status'>模型调用失败（{esc(result.get('error_kind') or '')}）：未生成答复，{hint}。</div>"
                )
        else:
            parts.append(
                "<div class='kb-qa-status warn' role='status'>资料不足：现有授权资料没有足够依据回答这个问题，建议转人工处理。</div>"
                f"<p class='kb-reason'>内部原因：{esc(result.get('reason') or '')} · 候选证据 {result.get('candidate_count', 0)} 条</p>"
                "<p class='kb-hint'>按现有买方流程，只有客户在会话中主动请求工程师核实时才会创建人工任务；本页只做提示，不自动建任务。</p>"
            )
        return "".join(parts)

    def knowledge_qa_post(self, form: dict) -> None:
        from app import answer_knowledge_question
        question = str(form.get("question", "")).strip()
        if not question:
            self.knowledge_qa_page({}, question="", result=self._kb_qa_error("empty_question", "问题不能为空"))
            return
        if len(question) > 2000:
            self.knowledge_qa_page({}, question=question[:2000], result=self._kb_qa_error("too_long", "问题过长（最多 2000 字）"))
            return
        result = answer_knowledge_question(self.db, self.ai_for(self.tenant_id), self.tenant_id, None, question)
        self.knowledge_qa_page({}, question=question, result=result)

    @staticmethod
    def _kb_qa_error(kind: str, reason: str) -> dict:
        return {"status": "error", "answer": "", "citations": [], "reason": reason,
                "error_kind": kind, "error_action": "", "candidate_count": 0, "selected": None}

    # ── 产品详情（只读投影）────────────────────────────────────────────────

    def knowledge_product_page(self, product_id: str, query: dict) -> None:
        """原产品适配页（清单10/11）：只读投影 + 逐产品 AI 客服授权与可选到期的侧车设置。

        - 客户视角字段 = 已发布快照值 ∩ 当前未被撤销；未发布编辑不进入（也不改写当前发布视图）。
        - 所有成员都可在此看到字段与来源（内部视图）；编辑字段、登记来源与发布仍走产品模块（旧路径）。
        - 管理员可在此开启/关闭「允许 AI 客服使用」并设置可选到期（开启在已发布产品上需要一次确认）。
        """
        from app import esc
        product = self.db.get_product(self.tenant_id, product_id)
        if not product:
            self.send_error(404)
            return
        version = self.db.get_published_version(self.tenant_id, product_id)
        overview = next((info for info in self.db.list_knowledge_product_overview(self.tenant_id) if info["product_id"] == product_id), None)
        settings = self.db.knowledge_product_settings(self.tenant_id, product_id)
        gate_ok, gate_reason = self.db.knowledge_product_ai_gate(self.tenant_id, product_id)
        effective = self.db.knowledge_product_fields(self.tenant_id, product_id)
        effective_keys = {field["field_key"] for field in effective}
        revoked = self.db.knowledge_product_field_revocations(self.tenant_id, product_id)
        snapshot_fields = []
        if version:
            try:
                for field in json.loads(version["snapshot_json"] or "{}").get("fields") or []:
                    if isinstance(field, dict) and str(field.get("field_key") or ""):
                        snapshot_fields.append(field)
            except ValueError:
                snapshot_fields = []
        snapshot_labels = {str(field.get("field_key")): str(field.get("label") or field.get("field_key")) for field in snapshot_fields}

        # 与列表同口径（状态同一枚）：未发布的原产品显示「草稿」，不另造「未发布」。
        state = {
            "status": "published" if version else "draft",
            "available": gate_ok,
            "title": ("已发布（AI客服可用）" if gate_ok else ("已发布（AI客服不可用）" if version else "草稿")),
            "reason": gate_reason if not gate_ok else "",
        }
        reason_html = f"<div class='kb-reason'>不可用原因：{esc(state['reason'])}</div>" if state["reason"] else ""
        notice = query.get("notice", [""])[0]
        error = query.get("error", [""])[0]
        notice_html = f"<div class='notice'>{esc(notice)}</div>" if notice else ""
        error_html = f"<div class='error'>{esc(error)}</div>" if error else ""

        # 1) 客户问答实际使用的字段：已发布快照 ∩ 当前未被撤销。
        effective_rows = "".join(
            f"<tr><td>{esc(field['label'])}</td><td>{esc(field['value'])} {esc(field['unit'])}</td><td>{esc(field['source'])}</td></tr>"
            for field in effective
        )
        revoked_rows = "".join(
            f"<tr class='kb-field-internal'><td>{esc(snapshot_labels.get(key, key))}</td><td colspan='2'>已撤销：{esc(reason)}</td></tr>"
            for key, reason in revoked.items()
        )
        if version:
            if effective_rows or revoked_rows:
                published_table = (
                    f"<table class='kb-table'><tr><th>字段</th><th>内容（已发布快照）</th><th>来源</th></tr>{effective_rows}{revoked_rows}</table>"
                    "<p class='kb-hint'>内容一律取已发布版本快照：产品模块里未发布的修改不会出现在这里，也不会进入客户回答；撤销（重新待核准 / 改仅内部 / 来源被禁用）立即生效。</p>"
                )
            else:
                published_table = "<p class='kb-reason'>已发布版本还没有可用的对外字段。</p>"
        else:
            published_table = "<p class='kb-reason'>尚未发布：客户问答不会使用任何字段（先在产品模块核准字段并发布）。</p>"

        # 2) 内部视图：产品模块当前字段与来源（所有成员可读）。
        live_rows = []
        for field in self.db.get_fields(self.tenant_id, product_id):
            scope = "对外" if (field["audience"] or "external") == "external" else "仅内部"
            state_label = {"candidate": "待核准", "approved": "已核准", "rejected": "已驳回"}.get(field["state"], field["state"])
            used = "✓ 进入客户问答" if field["field_key"] in effective_keys else "—"
            css = "" if field["field_key"] in effective_keys else " class='kb-field-internal'"
            source = field["source_filename"] or field["source_location"] or "—"
            live_rows.append(f"<tr{css}><td>{esc(field['label'])}</td><td>{esc(field['value'])} {esc(field['unit'])}</td><td>{esc(scope)}</td><td>{esc(state_label)}</td><td>{esc(source)}</td><td>{used}</td></tr>")
        if not live_rows:
            live_rows.append("<tr><td colspan='6' class='kb-reason'>还没有字段记录。</td></tr>")
        source_rows = []
        for source in self.db.get_sources(self.tenant_id, product_id):
            scope_label = {"exact_model": "本型号", "series": "系列", "unknown": "未确认"}.get(source["scope"], source["scope"])
            usage_label = {"internal": "仅内部", "restricted": "禁止使用"}.get(source["usage"] or "internal", source["usage"])
            source_rows.append(f"<tr><td>{esc(source['filename'])}</td><td>{esc(scope_label)}</td><td>{esc(usage_label)}</td><td>{esc((source['created_at'] or '')[:16])}</td></tr>")
        if not source_rows:
            source_rows.append("<tr><td colspan='4' class='kb-reason'>还没有来源文件。</td></tr>")

        # 3) 侧车设置：AI 客服授权 + 可选到期（管理员；读者只看到当前状态）。
        expiry_beijing = knowledge_expiry_instant(settings["expires_at"])
        expiry_line = _fmt_beijing(settings["expires_at"])
        current_line = (
            f"当前：{'已授权（AI 客服可使用）' if settings['ai_authorized'] else '未授权（AI 客服不可使用）'} · 到期：{esc(expiry_line)}"
            + (f"（由 {esc(settings['updated_by'] or '—')} 更新）" if settings["updated_by"] else "")
        )
        if self._kb_can_manage():
            expiry_date_value = expiry_beijing.astimezone(SHANGHAI_TZ).strftime("%Y-%m-%d") if expiry_beijing else ""
            expiry_time_value = expiry_beijing.astimezone(SHANGHAI_TZ).strftime("%H:%M") if expiry_beijing else ""
            needs_confirm = "1" if (version and not settings["ai_authorized"]) else "0"
            settings_html = f"""<form method='post' action='/admin/knowledge/product/{product_id}/authorize' data-kb-product-auth data-needs-confirm='{needs_confirm}' data-kb-confirm-title='允许 AI 客服使用该产品？' data-kb-confirm-message='开启后，该产品「对外且已核准」的字段会进入 AI 客服回答候选用途。' data-kb-confirm-label='开启授权'>
<input type='hidden' name='confirmed' value=''>
<label class='kb-check'><input type='checkbox' name='authorized' value='1'{' checked' if settings['ai_authorized'] else ''}>允许 AI 客服使用</label>
<div class='kb-grid2'>
<div><label for='kb-p-expiry-date'>到期日期（可选）</label><input id='kb-p-expiry-date' type='date' name='expiry_date' value='{esc(expiry_date_value)}'></div>
<div><label for='kb-p-expiry-time'>到期时间（可选）</label><input id='kb-p-expiry-time' type='time' name='expiry_time' value='{esc(expiry_time_value)}'></div>
</div>
<p class='kb-hint'>到期时间按北京时间填写；留空 = 长期有效。到期后立即停止用于客户回答（字段与历史不受影响）。</p>
<button class='secondary'>保存 AI 客服设置</button>
</form>"""
        else:
            settings_html = "<p class='kb-hint'>调整授权与到期需要管理员或产品管理模块权限。</p>"
        body = f"""{KB_CSS}
<div class='kb-head'><div class='kb-title-row'><h1>{esc(product['model'])} · {esc(product['name'])}</h1><span class='kb-tag'>产品</span>{self._kb_status_pill(state)}</div>
<p>{'当前发布版本 v' + str(version['version']) if version else '尚未发布'} · 分类 {esc(product['category'] or '—')} · 更新 {esc(product['updated_at'][:16])}</p>{reason_html}</div>
<div class='kb-actions'><a class='button secondary' href='/admin/products/{product_id}'>在产品模块中编辑字段 / 复核 / 发布（旧路径）</a></div>
{self._kb_not_read_only_note()}{notice_html}{error_html}
<p class='kb-note'>本页把原产品映射为知识视角：字段、核准与发布以产品模块为唯一权威；客户问答使用「已发布快照 ∩ 当前未被撤销」的字段，本页不修改任何产品数据。</p>
<div class='kb-detail'>
<div>
<section class='kb-body'><h2>客户问答使用的字段{('（已发布 v' + str(version['version']) + '）') if version else ''}</h2>{published_table}</section>
<section class='kb-body' style='margin-top:16px'><h2>字段与来源（产品模块当前状态）</h2>
<p class='kb-hint'>下方为内部核对视图（所有成员可读）：包含未发布修改与仅内部要点；「客户问答」列显示当前是否真正进入客户回答。</p>
<table class='kb-table'><tr><th>字段</th><th>内容</th><th>使用范围</th><th>核准</th><th>出处</th><th>客户问答</th></tr>{''.join(live_rows)}</table>
<h3 style='font-size:13px;color:var(--text-2);margin:14px 0 4px'>来源文件</h3>
<table class='kb-table'><tr><th>文件</th><th>归属</th><th>可见性</th><th>登记时间</th></tr>{''.join(source_rows)}</table>
</section>
</div>
<aside class='kb-side'><section><h2>AI 客服可用性</h2><p class='kb-reason'>{current_line}</p>{settings_html}</section></aside>
</div>
{self._kb_confirm_dialog()}
{KB_JS}"""
        self.send_html(self.console_page("产品知识", body, "knowledge_products"))

    # ── POST 路由 ────────────────────────────────────────────────────────

    def knowledge_post(self, path: str, form: dict) -> bool:
        if not re.match(r"^/admin/knowledge(/|$)", path):
            return False
        if not self._kb_can_manage():
            self._kb_forbidden()
            return True
        if path == "/admin/knowledge/new":
            self.knowledge_create_post(form)
            return True
        if path == "/admin/knowledge/import":
            self.knowledge_import_post(form)
            return True
        if path == "/admin/knowledge/qa":
            self.knowledge_qa_post(form)
            return True
        match = re.fullmatch(r"/admin/knowledge/item/([a-f0-9]{8,64})/(edit|publish|disable|enable|authorize|restore|retry)", path)
        if match:
            item_id, action = match.groups()
            item = self.db.get_knowledge_item(self.tenant_id, item_id)
            if not item:
                self.send_error(404)
                return True
            self.knowledge_item_action(item_id, action, form)
            return True
        match = re.fullmatch(r"/admin/knowledge/product/([a-f0-9]{8,64})/authorize", path)
        if match:
            product_id = match.group(1)
            if not self.db.get_product(self.tenant_id, product_id):
                self.send_error(404)
                return True
            self.knowledge_product_authorize_post(product_id, form)
            return True
        self.send_error(404)
        return True

    def knowledge_product_authorize_post(self, product_id: str, form: dict) -> None:
        """原产品的 AI 客服授权 / 到期设置（侧车保存；开启在已发布产品上需确认，服务端再次校验）。"""
        try:
            if str(form.get("expiry_date", "")).strip() == "" and str(form.get("expiry_time", "")).strip():
                raise ValueError("请先填写到期日期，再填写到期时间")
            self.db.set_knowledge_product_authorization(
                self.tenant_id, product_id, self.user_id,
                authorized=str(form.get("authorized", "")) == "1",
                expires_at=self._kb_expiry_from_form(form),
                confirmed=str(form.get("confirmed", "")) == "1",
            )
        except (ValueError, PermissionError) as exc:
            self.redirect(f"/admin/knowledge/product/{product_id}?error={quote(str(exc))}")
            return
        message = ("已开启 AI 客服使用：发布快照中「对外且已核准」的字段与到期设置立即生效。"
                   if str(form.get("authorized", "")) == "1"
                   else "已关闭 AI 客服使用：该产品立即退出客户回答候选（字段与历史不变）。")
        self.redirect(f"/admin/knowledge/product/{product_id}?notice={quote(message)}")

    def _kb_error_page(self, title: str, message: str, *, status: int = 400) -> None:
        from app import esc
        self.send_html(self.console_page(title, f"<div class='error'>{esc(message)}</div>", "knowledge"), status)

    def knowledge_create_post(self, form: dict) -> None:
        form_name = str(form.get("form", "article"))
        if form_name not in ("article", "faq", "product", "service"):
            form_name = "article"
        try:
            title = str(form.get("title", "")).strip()
            items = self.db.list_knowledge_items(self.tenant_id)
            item_id = self.db.create_knowledge_item(
                self.tenant_id, form_name, title, self.user_id,
                content=self._kb_snapshot_from_form(form_name, form),
                category=str(form.get("category", "")),
                usage=str(form.get("usage", "internal")),
                ai_authorized=bool(form.get("ai_authorized")),
                expires_at=self._kb_expiry_from_form(form),
            )
            version = self.db.get_knowledge_draft_version(self.tenant_id, item_id)
            publish = str(form.get("action", "")) == "publish"
            if publish:
                try:
                    self.db.publish_knowledge_draft(
                        self.tenant_id, item_id, self.user_id,
                        confirm_ai=str(form.get("confirm_ai", "")) == "1")
                except ValueError as exc:
                    # 直接 POST 未带确认字段：条目保持草稿，回到编辑页提示原因。
                    self.redirect(f"/admin/knowledge/item/{item_id}/edit?error={quote(str(exc))}")
                    return
                published = self.db.get_published_knowledge_version(self.tenant_id, item_id)
                try:
                    self.db.prepare_knowledge_version(self.tenant_id, published["id"])
                except ValueError:
                    pass
                self.redirect(f"/admin/knowledge/item/{item_id}?notice=published")
                return
            self.redirect(f"/admin/knowledge/item/{item_id}?notice=saved_draft")
            return
        except ValueError as exc:
            self.knowledge_editor_page({"form": [form_name]}, None, error=str(exc), values={
                "title": str(form.get("title", "")), "body": str(form.get("body", "")),
                "category": str(form.get("category", "")), "usage": str(form.get("usage", "internal")),
            })

    def _kb_snapshot_from_form(self, form_name: str, form: dict) -> dict:
        snapshot: dict = {}
        # 清单17：标题属于版本内容，编辑标题必须随快照保存（此前合并草稿时被丢弃）。
        # 表单未提交标题字段（旧式直接 POST）时保留参考版本标题（由 save_knowledge_draft 合并）；
        # 显式提交空标题仍按「标题不能为空」拒绝。
        if "title" in form:
            snapshot["title"] = str(form.get("title", "")).strip()
        if form_name == "faq":
            pairs = []
            index = 0
            while f"faq_q_{index}" in form or f"faq_a_{index}" in form:
                question = str(form.get(f"faq_q_{index}", "")).strip()
                answer = str(form.get(f"faq_a_{index}", "")).strip()
                if question or answer:
                    pairs.append({"question": question, "answer": answer})
                index += 1
            snapshot["faq"] = pairs
        else:
            snapshot["body"] = str(form.get("body", ""))
        if form_name in ("product", "service"):
            fields = []
            index = 0
            while (f"field_label_{index}" in form or f"field_value_{index}" in form or f"field_unit_{index}" in form
                   or f"field_audience_{index}" in form or f"field_approved_{index}" in form):
                label = str(form.get(f"field_label_{index}", "")).strip()
                value = str(form.get(f"field_value_{index}", "")).strip()
                unit = str(form.get(f"field_unit_{index}", "")).strip()
                if label or value:
                    # 清单12：使用范围 / 核准 / 出处都来自界面上的显式控件；表单里缺少这些字段
                    # （旧表单或直接 POST）时按最保守解释：仅内部 + 待核对——不静默把未知要点当作已核准。
                    audience = "external" if str(form.get(f"field_audience_{index}", "")) == "external" else "internal"
                    state = "approved" if str(form.get(f"field_approved_{index}", "")) == "1" else "candidate"
                    source = str(form.get(f"field_source_{index}", "")).strip() or "人工录入"
                    fields.append({"label": label, "value": value, "unit": unit,
                                   "audience": audience, "state": state, "source": source})
                index += 1
            # 清单18：即使要点被全部删除也要显式保存 fields=[]；
            # 省略字段会让草稿保存合并旧内容，导致已删除的要点在再次发布后复现。
            snapshot["fields"] = fields
        return snapshot

    def _kb_expiry_from_form(self, form: dict):
        date_value = str(form.get("expiry_date", "")).strip()
        time_value = str(form.get("expiry_time", "")).strip()
        if not date_value:
            return None
        return date_value + ("T" + time_value if time_value else "")

    def knowledge_import_post(self, form: dict) -> None:
        data = form.get("file")
        filename = str(form.get("file__filename", ""))
        if not data or not isinstance(data, (bytes,)):
            self.redirect("/admin/knowledge/import?error=" + quote("请选择要上传的文件"))
            return
        try:
            result = self.db.import_knowledge_file(
                self.tenant_id, self.user_id, filename=filename, data=data,
                title=Path(filename).stem, source_location="",
            )
        except ValueError as exc:
            self.redirect("/admin/knowledge/import?error=" + quote(str(exc)))
            return
        next_path = str(form.get("next", "")).strip()
        if next_path.startswith("/") and not next_path.startswith("//"):
            self.redirect(next_path)
            return
        self.redirect(f"/admin/knowledge/item/{result['item_id']}/edit?from=import")

    def knowledge_item_action(self, item_id: str, action: str, form: dict) -> None:
        from app import esc
        try:
            if action == "edit":
                self._kb_save_from_form(item_id, form)
                return
            if action == "publish":
                self.db.publish_knowledge_draft(self.tenant_id, item_id, self.user_id, confirm_ai=bool(form.get("confirmed")))
                published = self.db.get_published_knowledge_version(self.tenant_id, item_id)
                try:
                    self.db.prepare_knowledge_version(self.tenant_id, published["id"])
                except ValueError:
                    pass
                # 发布结果文案保持简单用户口径（准备状态由详情页的真实状态自行显示）。
                self.redirect(f"/admin/knowledge/item/{item_id}?notice=published")
                return
            if action == "disable":
                self.db.disable_knowledge_item(self.tenant_id, item_id, self.user_id, confirmed=bool(form.get("confirmed")))
                self.redirect(f"/admin/knowledge/item/{item_id}?notice={quote('已停用：客户问答不再使用该内容。')}")
                return
            if action == "enable":
                self.db.enable_knowledge_item(self.tenant_id, item_id, self.user_id, confirmed=bool(form.get("confirmed")))
                self.redirect(f"/admin/knowledge/item/{item_id}?notice={quote('已重新启用。')}")
                return
            if action == "authorize":
                authorized = str(form.get("authorized", "")) == "1"
                self.db.set_knowledge_authorization(self.tenant_id, item_id, self.user_id, authorized, confirmed=bool(form.get("confirmed")))
                message = "已授权 AI 客服使用（立即生效）。" if authorized else "已关闭 AI 授权（立即停止使用）。"
                self.redirect(f"/admin/knowledge/item/{item_id}?notice={quote(message)}")
                return
            if action == "restore":
                version = int(str(form.get("version", "0")))
                self.db.restore_knowledge_version(self.tenant_id, item_id, version, self.user_id)
                self.redirect(f"/admin/knowledge/item/{item_id}?notice={quote(f'已恢复 v{version} 为新草稿（不覆盖历史）。')}")
                return
            if action == "retry":
                version = self.db.get_published_knowledge_version(self.tenant_id, item_id)
                state = self.db.knowledge_processing_state(self.tenant_id, version["id"]) if version else None
                if state and state["status"] == "failed":
                    self.db.retry_knowledge_processing(self.tenant_id, version["id"], self.user_id)
                if version:
                    try:
                        self.db.prepare_knowledge_version(self.tenant_id, version["id"])
                    except ValueError:
                        pass
                self.redirect(f"/admin/knowledge/item/{item_id}?notice={quote('已重新处理；完成后才恢复可用。')}")
                return
            self.send_error(404)
        except (ValueError, PermissionError) as exc:
            self.redirect(f"/admin/knowledge/item/{item_id}?error={quote(str(exc))}")

    def _kb_save_from_form(self, item_id: str, form: dict) -> None:
        item = self.db.get_knowledge_item(self.tenant_id, item_id)
        form_name = item["form"]
        snapshot = self._kb_snapshot_from_form(form_name, form)
        self.db.save_knowledge_draft(
            self.tenant_id, item_id, self.user_id,
            content=snapshot,
            category=str(form.get("category", "")),
            usage=str(form.get("usage", "internal")),
            ai_authorized=bool(form.get("ai_authorized")),
            expires_at=self._kb_expiry_from_form(form),
        )
        if str(form.get("action", "")) == "publish":
            try:
                self.db.publish_knowledge_draft(
                    self.tenant_id, item_id, self.user_id,
                    confirm_ai=str(form.get("confirm_ai", "")) == "1")
            except ValueError as exc:
                # 对外+授权未确认：保持草稿，明确提示（不发布、不丢草稿）。
                self.redirect(f"/admin/knowledge/item/{item_id}/edit?error={quote(str(exc))}")
                return
            published = self.db.get_published_knowledge_version(self.tenant_id, item_id)
            try:
                self.db.prepare_knowledge_version(self.tenant_id, published["id"])
            except ValueError:
                pass
            self.redirect(f"/admin/knowledge/item/{item_id}?notice=published")
            return
        next_path = str(form.get("next", "")).strip()
        if next_path.startswith("/") and not next_path.startswith("//"):
            self.redirect(next_path)
            return
        self.redirect(f"/admin/knowledge/item/{item_id}/edit?saved=1")

    # ── 私有原件下载 ─────────────────────────────────────────────────────

    def knowledge_source_download(self, source_id: str) -> None:
        try:
            row, data = self.db.read_knowledge_source_file(self.tenant_id, source_id)
        except ValueError:
            self.send_error(404)
            return
        self.db._audit(self.tenant_id, "knowledge", row["item_id"], "file_downloaded", self.actor, f"source={source_id}")
        self.db.conn.commit()
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Disposition", "attachment; filename*=UTF-8''" + quote(row["filename"] or "knowledge.bin"))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)
