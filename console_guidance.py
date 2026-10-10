"""按既有页面的业务任务降低首次使用负担，不改变表单、权限或数据。"""
import json

# 明确列出每个模块的次要区域；业务处理、风险提示与提交按钮不自动折叠。
GUIDES = {
    'admin': ('从待办开始', '先处理需要你行动的事项；没有待办时，从常用入口进入业务。', '查看待办 → 打开业务 → 完成处理', []),
    'products': ('把资料变成可信的产品信息', '选择一个型号，按页面提示补充资料、核准字段，再发布给客户。', '登记资料 → 核准事实 → 发布版本', ['品类（候选与核准）', '登记来源资料']),
    'knowledge': ('统一的企业知识入口', '文章、FAQ、产品与服务、文件资料都在这里。所有成员可读；AI 对外问答只使用已发布且获准的内容。', '浏览或搜索 → 打开详情 → 编辑或授权（需要权限）', ['*']),
    'sales': ('先处理需要跟进的客户', '用逾期、今日到期或未分配入口找到客户；打开后安排下一步。', '找到客户 → 核对需求 → 安排跟进', ['跟进节点', '已确认需求快照', '推荐快照', '问答与人工任务']),
    'rfqs': ('查看客户的采购请求', '打开询价核对联系信息和采购要求；后续跟进在销售工作台处理。', '打开询价 → 核对要求 → 跟进客户', ['已确认需求快照', 'RFQ 提交与修改记录（仅卖方可见）']),
    'tasks': ('解决客户需要人工判断的问题', '打开待处理任务，先看问题和工况，再选择追问或答复。', '阅读问题 → 补充条件 → 提交答复', []),
    'creative': ('为产品准备客户看到的页面', '按当前步骤选择参数、安排内容、预览效果，再提交或确认上线。', '选择参数 → 排列内容 → 预览与发布', []),
    'analytics': ('先看结果，再查原因', '先看询价结果，再按需要展开渠道、跟进或资料缺口。所有明细和导出保留。', '查看结果 → 定位原因 → 查看明细', ['入口渠道', '需求确认与推荐', '问答与人工升级', 'RFQ 完整性', '跟进时效', '资料缺口与高频问题', '产品页浏览与下载（复盘）', '资料补充任务']),
    'users': ('给团队成员开通合适的权限', '先找到成员；展开该成员的授权设置分配模块，也可以添加新成员。', '找到或添加成员 → 授权模块 → 成员开始工作', []),
    'sites': ('把官网咨询接入平台', '先登记官网来源生成网站标识，再展开接入说明交给网站开发人员。', '登记官网 → 获取标识 → 接入聊天', []),
    'settings': ('选择一项企业规则进行设置', '展开需要调整的项目，修改后单独保存；无需一次填写全部配置。', '选择设置 → 调整规则 → 保存生效', ['线索阶段', 'RFQ 询价表单字段', '按品类询价字段（可选）', '通知接收人']),
    'audit': ('查找谁在何时做了什么', '按对象类型或操作者筛选记录，再展开需要核对的变更详情。', '筛选记录 → 找到操作 → 核对详情', []),
    'inbox_web': ('从客户的最新消息开始', '打开会话查看上下文，再回复客户；网站聊天回复会立即出现在客户窗口。', '选择会话 → 阅读消息 → 回复客户', []),
}

STYLE = '''<style>
.console .ux-guide{margin:0 0 20px;padding:14px 18px;border-left:3px solid var(--accent);background:var(--surface-2);border-radius:0 var(--r-md) var(--r-md) 0}
.console .ux-guide strong{font-size:14px;color:var(--text-1)}.console .ux-guide p{margin:5px 0 0;font-size:13px;color:var(--text-2)}
.console .ux-guide .ux-path{font-size:12px;margin-top:9px;color:var(--text-3)}
.console .ux-section-nav{display:flex;gap:8px;flex-wrap:wrap;margin:0 0 20px}
.console .ux-section-nav a{font-size:12px;padding:6px 10px;border:1px solid var(--border);border-radius:var(--r-md);color:var(--text-2)}
.console .ux-fold{border:1px solid var(--border);border-radius:var(--r-lg);background:var(--surface);margin:0 0 16px;min-width:0}
.console .ux-fold>summary{cursor:pointer;padding:16px 18px;color:var(--text-1);font-weight:500;font-size:14px}
.console .ux-fold>section.card{margin:0!important;border:0;border-top:1px solid var(--border);box-shadow:none}
.console .ux-fold>section>.panel-title h2,.console .ux-fold>section>h2{display:none}
.console .ux-fold summary:focus-visible,.console .ux-section-nav a:focus-visible{outline:2px solid var(--accent);outline-offset:3px}
.console .ux-filter{margin:12px 0}.console .ux-filter>summary{cursor:pointer;color:var(--text-2);font-size:13px}
.console .ux-tools>summary{cursor:pointer;font-size:12px;color:var(--text-2)}.console .ux-tools-actions{display:flex;gap:6px;flex-wrap:wrap;padding-top:8px}.console .ux-guide+nav{margin-top:-6px}.console .ux-section-nav~section{scroll-margin-top:80px}
@media(max-width:720px){.console .ux-guide{padding:12px}.console .ux-section-nav{gap:6px}.console .ux-section-nav a{padding:6px 8px}}
</style>'''

SCRIPT = '''<script>(function(){
var config=CONFIG;
function init(){
var main=document.querySelector('.console main');if(!main)return;
var guide=document.getElementById('ux-guide');var toolbar=main.querySelector('.toolbar');
if(toolbar)toolbar.after(guide);
var headings=Array.from(main.querySelectorAll('section.card h2')).filter(function(h){return h.closest('section.card').querySelector('h2')===h;});
var nav=document.createElement('nav');nav.className='ux-section-nav';nav.setAttribute('aria-label','本页操作入口');
headings.forEach(function(h,i){
var section=h.closest('section.card');var label=h.textContent.trim();
if(section.closest('.chat-shell'))return;
if(!section.id)section.id='ux-section-'+i;
var link=document.createElement('a');link.href='#'+section.id;link.textContent=label;nav.appendChild(link);
if(config.fold.includes(label)||config.fold.includes('*')){
var details=document.createElement('details');details.className='ux-fold';
var summary=document.createElement('summary');summary.textContent=label;
// 保存后的提示及服务端错误保持可见；不让用户寻找刚操作的区域。
var notice=(new URLSearchParams(location.search).get('notice')||'').split('_')[0];
var feedback={products:{source:'登记来源资料',category:'品类（候选与核准）'},settings:{stages:'线索阶段',fields:'RFQ 询价表单字段',notify:'通知接收人','rfq-category':'按品类询价字段（可选）'}};
details.open=!!(feedback[config.key]&&feedback[config.key][notice]===label)||(config.key==='knowledge'&&i===0);
section.before(details);details.append(summary,section);
}
});
if(headings.length>1&&headings.length<=5&&!['settings','knowledge'].includes(config.key))guide.after(nav);
// 筛选条件仍使用原 GET 表单，已筛选时默认展开，避免隐藏当前筛选口径。
if(config.key==='sales'){
var filter=main.querySelector('form.filter-bar');if(filter){var wrap=document.createElement('details');wrap.className='ux-filter';var summary=document.createElement('summary');summary.textContent='按阶段、负责人或关键词筛选';
wrap.open=Array.from(filter.querySelectorAll('input,select')).some(function(el){return !!el.value;});filter.before(wrap);wrap.append(summary,filter);}}
// 新建入口出现在页首；原表单仍在原位置，按钮只负责展开与定位。
var createLabels={products:'新建产品型号',users:'创建成员账号',sites:'登记新的官网来源'};
if(toolbar&&createLabels[config.key]){var disclosure=Array.from(main.querySelectorAll('details')).find(function(d){return d.querySelector('summary')&&d.querySelector('summary').textContent.trim()===createLabels[config.key];});
if(disclosure){var action=document.createElement('button');action.type='button';action.textContent=createLabels[config.key];toolbar.appendChild(action);
if(main.querySelector('.simple-item'))disclosure.open=false;
action.addEventListener('click',function(){disclosure.open=true;disclosure.scrollIntoView({block:'center'});var field=disclosure.querySelector('input');if(field)field.focus();});}}
// 核对页优先看仍待处理的字段，完整表格始终可以切回。
if(config.key==='products'){
var table=main.querySelector('#fields>table');if(table){var rows=Array.from(table.querySelectorAll('tbody>tr'));var pending=rows.filter(function(row){return !!row.querySelector('.badge.pending_review');});
if(pending.length&&pending.length<rows.length){var toggle=document.createElement('button');toggle.type='button';toggle.className='secondary';var onlyPending=!new URLSearchParams(location.search).has('notice');
function renderFields(){rows.forEach(function(row){row.hidden=onlyPending&&!pending.includes(row);});toggle.textContent=onlyPending?'查看全部 '+rows.length+' 个字段':'只看待核对的 '+pending.length+' 个字段';}
table.before(toggle);renderFields();toggle.addEventListener('click',function(){onlyPending=!onlyPending;renderFields();});}}}
if(config.key==='sales'){
var stop=main.querySelector('form[action$="/contact-stop"]');if(stop){var stopDetails=document.createElement('details');stopDetails.className='ux-filter';var summary=document.createElement('summary');summary.textContent='记录客户停止主动联系的要求';stop.before(stopDetails);stopDetails.append(summary,stop);}}
if(config.key==='audit')main.querySelectorAll('.simple-list details.detail-toggle>summary').forEach(function(summary){summary.textContent='查看变更详情';});
if(config.key==='creative'){
main.querySelectorAll('.pcard .tools').forEach(function(tools){var actions=Array.from(tools.children).filter(function(el){return !el.querySelector('.ptoggle')&&!el.classList.contains('lock');});
if(!actions.length)return;var more=document.createElement('details');more.className='ux-tools';var summary=document.createElement('summary');summary.textContent='编辑与排序';var panel=document.createElement('div');panel.className='ux-tools-actions';tools.appendChild(more);more.append(summary,panel);actions.forEach(function(el){panel.appendChild(el);});});
var path=guide.querySelector('.ux-path');if(path&&main.querySelector('.flowbar'))path.hidden=true;
var coach=main.querySelector('.coach');if(coach){var help=document.createElement('details');help.className='ux-filter';var summary=document.createElement('summary');summary.textContent='查看初次制作说明';coach.before(help);help.append(summary,coach);}
var pool=main.querySelector('.pchips');if(pool){var library=document.createElement('details');library.className='ux-filter';var summary=document.createElement('summary');summary.textContent='添加更多参数或内容板块';
var caption=pool.previousElementSibling;pool.before(library);library.appendChild(summary);if(caption&&caption.classList.contains('muted'))library.appendChild(caption);library.appendChild(pool);}}
if(config.key==='inbox_web'){var chips=main.querySelector('.chat-chips');if(chips){var context=document.createElement('details');context.className='ux-filter';var summary=document.createElement('summary');summary.textContent='查看客户资料与关联业务';chips.before(context);context.append(summary,chips);}}
function reveal(target){if(!target)return;for(var parent=target.parentElement;parent;parent=parent.parentElement){if(parent.tagName==='DETAILS')parent.open=true;}if(target.tagName==='DETAILS')target.open=true;}
function hashTarget(){var id;try{id=decodeURIComponent(location.hash.slice(1));}catch(e){return null;}return id?document.getElementById(id):null;}
reveal(hashTarget());window.addEventListener('hashchange',function(){reveal(hashTarget());});
nav.addEventListener('click',function(event){var link=event.target.closest('a');if(link){reveal(document.getElementById(link.hash.slice(1)));}});
// 浏览器校验隐藏的必填项时，展开对应区域，使原表单校验仍可操作。
main.addEventListener('invalid',function(event){reveal(event.target);},true);
}
if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',init);else init();
})();</script>'''


def guided_console_body(body, path, active, lang='zh'):
    if active not in GUIDES or not path.startswith('/admin') or lang != 'zh':
        return body
    # 明确排除本轮要求保持原版的页面及所有权限/错误拦截页。
    if path.startswith('/admin/channels') or (path.startswith('/admin/inbox/') and not path.startswith('/admin/inbox/web')):
        return body
    if "<h1" not in body and 'chat-shell' not in body:
        return body
    title, hint, steps, fold = GUIDES[active]
    if path == '/admin/customer-transfers':
        title, hint, steps = '转交客户与相关事项', '选择原负责人、接替人和客户，再确认转交；原操作记录会保留。', '选择人员 → 选择客户 → 确认转交'
    elif path.startswith(('/admin/sales/', '/admin/leads/')):
        title, hint, steps = '按当前状态处理这位客户', '先核对客户要求与联系授权，再填写跟进状态或准备回复；业务历史可按需展开。', '核对客户 → 处理当前事项 → 保存下一步'
    elif path.startswith('/admin/inbox/web/'):
        title, hint, steps = '回复这位网站客户', '先阅读消息；需要时展开关联资料。点击发送后，回复会出现在客户的聊天窗口。', '阅读消息 → 核对上下文 → 发送回复'
    config = json.dumps({'key': active, 'fold': fold}, ensure_ascii=False).replace('<', '\\u003c')
    guide = f'<aside id="ux-guide" class="ux-guide" aria-label="操作引导"><strong>{title}</strong><p>{hint}</p><p class="ux-path">{steps}</p></aside>'
    return STYLE + guide + body + SCRIPT.replace('CONFIG', config)
