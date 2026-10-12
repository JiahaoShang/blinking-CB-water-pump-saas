# 企业知识第二批（主题与资料）Implementation Plan

> **For agentic workers:** 用户已指定本地 Hermes CLI 实现，Codex 独立验收；按任务顺序执行，不使用子代理。本轮不再请求重复开工许可。

**Goal:** 将已确认的主题/资料原型变成真实可保存、可核对、可授权的企业知识流程，并完成文字PDF/DOCX入库和灵活字段。

**Architecture:** 保留现有 SQLite、EnterpriseKnowledge 与产品身份，在侧车表添加主题/关系/入库任务/来源位置/模板快照。新业务服务及界面独立文件，通过既有 KnowledgeUI/Database/路由接入；原有详情编辑与问答兼容。

**Tech Stack:** Python 3.9+、标准库 HTTP/SQLite、原生 HTML/CSS/JS；文字PDF使用pypdf 6.20.0，DOCX使用python-docx 1.2.0（PyPI元数据已核对Python >=3.9）；隔离虚拟环境，不改全局Python。

**Spec:** docs/企业知识管理-PRD与交互设计-v0.3-主题与资料评审稿.md（当前交互）；v0.2 §3、§6–9、§14的文件/模板业务规则保留。设计基准提交cd5f3e9。

## Global Constraints

- 用户2026-10-12明确批准当前设计并授权Hermes业务开发。旧记录“未授权”保留为历史，不阻止此次明确授权。
- 本企业所有成员可读；管理员或products模块授权成员可编辑/发布/授权/管理主题分组模板。后端每次写入强制权限和企业归属。
- 旧产品、文章、FAQ先出现在全部资料，不自动分配主题；成员确认后关联。旧ID/原件/版本/RFQ/推荐/授权不覆盖、不复制改写。
- 新资料/主题默认internal+AI off；新渠道AI仍关闭。已发布AI可用绿色#0E7A5F/#EAF4F0/#C2E0D5；不可用蓝灰#3E4C5E/#EEF2F7/#C9D4E0。
- 四入口在原侧栏，不移顶部；内部主题文件树在全部主题/主题详情/由树进入的文件页。中央分组独立折叠；文件打开在树，中央关联资料不重复文件按钮；全集检索保留。
- 分组18px/主题16px/说明13px+缩进；上传第1步单输入卡默认文件、底部切换，保持已录入数据。
- 搜索X只清关键词保留筛选/焦点；返回实际入口保留筛选和折叠；Enter/Space、aria、inert、动画/reduced-motion、未保存保护沿用原型。
- 真实私有存储与处理/任务恢复，不能保留原型模拟结果/载入示例/倒计时或内存当保存。登记链接不抓取；图片/视频/扫描件无OCR只登记并可人工正文；复杂表格/旧DOC不承诺自动理解。
- PDF页码；DOCX标题/段落/表格位置，不虚构页号。正文来源固定到资料身份+确切版本+位置；人工改写明确标记，不能冒充原文。
- 发布需有效已核对内容，编辑撤销对应核对；仅登记原件可归档但AI不可用；排除段不进入知识/AI。字段默认未核对/不供客户；0/否不是空。
- 模板改版不自动改变旧知识；采用新版只改草稿，保留正文/来源/字段与发布快照。可沿旧模板，不强迫升级。
- 处理和存储限制沿现有10MiB文档/1MiB文本；批次按既有产品上传最多20件的技术上限验证，超限明确提示。文字PDF最多200页作为处理保护，超出仍可原件归档/人工整理，界面如实展示。若依赖运行约束需调整则说明证据，不偷偷改变业务。
- 隔离环境验证，不读取/输出密钥、不改正在运行的data/演示库；不部署、不推送、不接外部消息。真实模型B5仍按既有用户决定待联调；无模型时不假成功。
- 系统建议关系/新主题与正文必须真实来自本企业资料；明确建议，成员确认后建立。初稿可采用来源摘录编排，标明待核对，不编造/不覆盖人工文本；不声称未实现的模型摘要。
- 只修阻断主流程、数据丢失、越权/泄漏、错误关键事实或必需验收失败；非阻断问题记录复现/影响/替代/暂缓原因后继续，不进行额外打磨。

## Review Focus

1. 同源资料多主题/跨企业ID混用：身份不可复制，跨企业读写与原件访问均拒绝。
2. 解析失败/中途重启/批次部分失败：持久任务与草稿保留，可重试且不重复创建；其他文件可继续。
3. 原件/主题来源换版、停用、撤权、到期：旧线上在更新草稿期间保持；对外候选按段实时过滤，模型前后复核。
4. 模板升级/数字0/布尔否：旧版本和用户草稿保持，新必填只约束使用新模板后的发布。
5. 重复提交/快速导航/只读：不重复发布/关联；已输入内容保护；隐藏入口不替代后端权限。

---

### Task 1: 主题、关系、版本与资料索引

**Files:** Create enterprise_knowledge_phase2.py, test_enterprise_knowledge_phase2.py; Modify app.py Database初始化；必要的enterprise_knowledge.py兼容钩子。

**Interfaces:** 新EnterpriseKnowledgePhase2 mixin：init_knowledge_phase2(); kb2_create_topic(tenant_id,user_id,title,group_id=None)->str; kb2_list_topics(tenant_id)->list[dict]; kb2_set_relation(tenant_id,user_id,topic_id,material_ref,linked=True)->None。material_ref={kind:knowledge|product,id:str}，旧产品只适配读取，不复制产品行。组/模板/主题正文使用同样企业与编辑权限检查；资料快照钉住原始version_id/source_id。

> **2026-10-12 实现同步（Hermes，后端切片）**：计划列出的三个接口保持不变；为覆盖「主题草稿/发布/历史与逐段 source pin、分组管理、全部资料索引」验收项，同一 mixin 内补齐下列接口（写操作统一约定 `(tenant_id, user_id, ...)`，与既有 kb2_create_topic / kb2_set_relation 一致；读取只带 tenant_id）：
> - 分组：`kb2_create_group(tenant_id,user_id,name)->str`、`kb2_rename_group(tenant_id,user_id,group_id,name)->None`、`kb2_delete_group(tenant_id,user_id,group_id)->None`（只解除分组）、`kb2_list_groups(tenant_id)->list[dict]`、`kb2_move_topic(tenant_id,user_id,topic_id,group_id=None)->None`。
> - 主题读取与版本：`kb2_get_topic(tenant_id,topic_id)->dict|None`（含逐段 label / source_label / source_stale 与关系、版本历史）、`kb2_save_topic_draft(tenant_id,user_id,topic_id,*,paragraphs=_UNSET,usage=_UNSET,ai_authorized=_UNSET,expires_at=_UNSET)->str`（保存草稿不换线上；段落来源必须指向本企业真实资料）、`kb2_publish_topic_draft(tenant_id,user_id,topic_id,*,confirm_ai=False)->int`（正文须全部已核对；对外+AI 需确认；旧线上转 superseded）、`kb2_list_topic_versions(tenant_id,topic_id)->list[dict]`。
> - 关系与全集：`kb2_list_relations(tenant_id,topic_id)->list[dict]`（含钉住快照与 missing 标记）、`kb2_list_materials(tenant_id)->list[dict]`（知识条目 + 产品只读适配；未确认前不带主题）。
> - 原因：检查清单要求这些行为可被测试闭环与后续 UI 直接调用；仅凭原三个接口无法验证「保存草稿不换线上 / 发布拦截 / 分组删除只解除分组 / 旧资料未自动分配」，故在计划内补记而非另建模块。

- [x] 先写临时DB测试：企业隔离/无授权拒写/重复关联幂等/多主题/旧知识仍在全部资料且未自动分配/重开DB持久。
- [x] 跑失败测试后实现最小侧车schema和CRUD，不重构相邻模块。
- [x] 定义主题草稿/发布/历史与逐段source pin；保存草稿不换线上，管理分组删除只解除分组。
- [x] 跑Task1测试及受影响第一批知识测试；提供实际结果给Codex。

> 2026-10-12 后端切片完成（10 项新测试 + 受影响第一批 127 项回归通过；命令与证据见《docs/企业知识管理-第二批实施记录.md》）。界面接入属任务3。

### Task 2: 真实统一入库、识别与核对

**Files:** Create knowledge_ingestion.py, requirements-knowledge.txt, test_knowledge_ingestion.py; extend phase2服务；必要接入服务器可恢复worker。

**Interfaces:** extract_document(filename:str,data:bytes)->dict(status,paragraphs:[{text,location,source_kind}],warnings,error); kb2_create_batch(tenant_id,user_id,entries:list[dict])->str; kb2_get_batch(tenant_id,batch_id)->dict; kb2_retry_source(tenant_id,user_id,source_id)->None。原件私有token命名+sha256，不使用用户路径。

> **2026-10-12 实现同步（Hermes，后端切片）**：计划列出的四个接口保持原名与语义；为覆盖「核对/编辑/排除、字段来源、建议改名/合并/拒绝/整批共同主题、确认前不建立、批次确认/发布幂等、可恢复处理」验收项，同一 mixin/模块内补齐：
> - 核对与字段：`kb2_update_paragraph(tenant_id,user_id,paragraph_id,*,text=_UNSET,checked=_UNSET,excluded=_UNSET)->None`（改文字→撤销该段核对并记人工修改）、`kb2_add_manual_paragraph(tenant_id,user_id,source_id,text)->str`（人工正文，发布前同样要核对）、`kb2_set_source_field(tenant_id,user_id,source_id,*,label,value,unit='',paragraph_id=None,checked=False,field_id=None)->str`（字段来源必须对应真实 source/version/location；0/否为有效值）。
> - 主题建议：`kb2_build_topic_suggestions(tenant_id,user_id,batch_id)->list[dict]`（真实本地信号：现有主题名出现在资料正文文字里才建议；不凭文件名猜新主题）、`kb2_add_topic_suggestion(...,name,source_ids=None)->str`（成员输入；整批共同主题=全选来源；同批同名合并为一条）、`kb2_rename_topic_suggestion`、`kb2_merge_topic_suggestion(...,topic_id)`、`kb2_reject_topic_suggestion`、`kb2_set_suggestion_sources(...,source_ids)`（逐件调整）；以上均不建立主题，`kb2_confirm_batch_topics(tenant_id,user_id,batch_id)->dict` 才创建主题草稿与关联（幂等；被拒绝的建议不复活）。
> - 发布：`kb2_save_batch_settings(tenant_id,user_id,batch_id,*,usage,ai_authorized,expires_at=None)->None`（保存草稿允许不完整）、`kb2_publish_batch(tenant_id,user_id,batch_id,*,usage=_UNSET,ai_authorized=_UNSET,expires_at=_UNSET,confirm_ai=False)->dict`（逐来源发布，单失败不阻断；重复发布幂等返回 skipped；对外+AI 在任何写入前整体要求确认）。
> - 恢复：`kb2_process_pending_sources(tenant_id=None)->dict` 与 `start_knowledge_phase2_worker(db_path, interval=15)`（服务器可选接入；从数据库继续待处理来源，未接模型/外部消息）。
> - 并发与排队（B2-B01 修复同步，2026-10-12）：`kb2_create_batch(..., process_now=False)` 为排队入口（HTTP 用）：只落库立即返回 batch_id，由入库线程独占认领后真实处理（默认 True 保持 CLI/测试同步行为）；处理改为原子认领（`kb2_sources.processing_owner / processing_lease_until`，属主失活或租约过期才接管；同进程多连接另有活动认领表互斥），同一来源并发只解析一次、只建一个条目；`kb2_retry_source` 对活动中来源明确拒绝。原因：修复 Codex 复现的并发重复创建阻断（详见实施记录《B2-B01 修复补记》）。
> - extract_document 额外返回 `format/title_hint`（显示辅助，不代表已识别）与 PDF 的 `page_count`；status 语义不变（extracted/registered/failed，「支持登记不等于识别」）。
> - 仅登记原件归档：批次发布对无正文来源走专门归档路径（内容带 `kb2_registered_only` 标记、AI 不可用、不算发布知识正文，对应 v0.3 §6.2）；常规发布（有正文）仍走第一批既有发布闸门（未核对/空正文拦截），不修改第一批校验代码。
> - 状态词汇：批次 draft→confirmed→published；来源 pending/processing/extracted/registered/failed/published；识别与生命周期分开展示（identification_label）。
> - 原因：checklist 各项需可测闭环；同时保持与第一批权限/存储/发布复用的口径，接口只落在新模块与新侧车表。

- [x] 测试真实文字PDF/DOCX/TXT，多文件与链接/人工、扫描/加密/损坏明确失败、不自动发布、任务可恢复/重试幂等。
- [x] 用真实解析生成逐段草稿，原件位置可读；任务状态单独于知识生命周期，数据库是事实来源。
- [x] 核对/编辑/排除与段落来源保存；字段来源对应真实source/version/location。权限、大小/格式与ZIP解压资源限制后端验证。
- [x] 建议已有/新主题，支持改名/合并/拒绝/整批共同主题，确认前不建立；同批同名新主题合并，批次确认/发布幂等。
- [x] 验证真实文件持久、重开恢复与错误回退；不把支持登记说成解析成功。

> 2026-10-12 后端切片完成（22 项新测试，全部真实文件解析；命令与证据见《docs/企业知识管理-第二批实施记录.md》）。界面三步流 / 版本更新 / AI 证据过滤属任务3–5。

> **2026-10-12/13 实现同步（Hermes，Task3 所需后端补齐；含 B2-B02 数据保留修复）**：
> - **B2-B02（数据保留，阻断修复）**：`_kb2_run_source` 的自动重建改为「受保护段落不动、只替换未受保护段落、新识别正文追加」——受保护 = `origin=manual`（含未核对）/ 已核对 / 已编辑 / 已排除 / 被字段来源钉住的段落；人工段落的 ID、文字、顺序、字段来源与草稿正文在重试、自动处理、崩溃恢复中均不覆盖。`kb2_process_pending_sources` 增加逐来源兜底：单个来源的意外错误不再中断整轮扫描，来源进入明确失败（可重试）而不是永远 processing；新增返回键 `errored`。审计写入补提交（如 `kb2_add_manual_paragraph`），不留隐形事务。
> - **主题初稿（新）**：`kb2_build_topic_draft(tenant_id,user_id,topic_id)->dict`——只用该主题**已关联资料**中「已核对且未排除」的段落做来源摘录编排；逐段钉住资料身份 + 当前可见版本 + 原件 + 位置；新段落一律「待核对」（origin=from_material）；只追加、不覆盖人工内容、同出处同文字不重复追加；不调用模型（不是模型摘要，也不读取扫描件/图片）。
> - **新主题候选（扩展）**：`kb2_build_topic_suggestions` 在原有「现有主题名文字命中」（basis=text_match）之外，新增依据**资料实际标题**（basis=source_title）与**标题段落**（DOCX 标题段，basis=source_heading）的候选；一律待成员在检查页确认，确认前不建立主题；拒绝不复活；解析失败的资料不产出候选。成员输入仍为 basis=member——文档与界面均不得把成员输入说成系统已建议的新主题。
> - **其他（新）**：`kb2_set_topic_disabled(tenant_id,user_id,topic_id,disabled=True)`（主题停用/恢复，历史与关联保留）与 `kb2_list_batches(tenant_id,limit=30)`（正式界面找回已保存批次）。基础主题关联与发布历史沿用原实现，不扩图谱、不做主题↔主题关系。
> - 判别力与回归证据（B2-B02 五场景先失败后通过、8 项新后端测试、受影响测试断言同步）见《企业知识管理-第二批实施记录》。

### Task 3: 正式界面与HTTP闭环

**Files:** Create enterprise_knowledge_phase2_ui.py, static/knowledge-phase2.js, static/knowledge-phase2.css, test_knowledge_phase2_http.py; Modify KnowledgeUI路由和app.py知识菜单/静态资源接入。

**Interfaces:** 新模块get(handler,path,query)->bool / post(handler,path,form)->bool，采用现有认证和企业作用域；JSON/表单接口调用Task1/2。GET不能变更数据。旧详情/产品/问答入口保持兼容，默认/admin/knowledge为全部主题，全集通过view=materials。

> **2026-10-12/13 实现同步（Hermes，Task3 界面与 HTTP）**：计划列出的 get/post 两个入口保持签名；实际落地的路由与变化：
> - GET：`/admin/knowledge`（全部主题总览；`view=materials` 全部资料；第一批旧列表视图 `view=all|product_service|article_faq|files` 原样保留交回旧代码）；`/admin/knowledge/topic/<id>`、`/topic/<id>/edit`、`/topic/new`；`/admin/knowledge/batch/<id>`（检查整理结果，`?step=publish` 为发布设置）；`/admin/knowledge/material/<id>`（由主题树打开的资料详情，`from=topics|topic:<id>` 决定是否带内部浏览树与返回位置）；`/admin/knowledge/source/<id>/file`（受认证企业下载/预览，sha256 校验 + 审计落盘）；`/admin/knowledge/assets/knowledge-phase2.(css|js)`（静态资源，no-cache）。
> - POST：`/admin/knowledge/add`（multipart，真实保存原件，`kb2_create_batch(process_now=False)` 排队入口）；`/batch/<id>/paragraphs|paragraph-add|field|suggestions/build|suggestion-add|suggestion/<sid>/(rename|merge|reject|sources)|confirm-topics|settings|source/<sid>/retry`；`/topic/new`、`/topic/<id>/(save|paragraph-add|build-draft|publish|disable|enable|relation-add|relation-remove|move)`；`/group/(new|rename|delete)`。全部写入先经后端「管理员或 products 授权」检查（不靠界面隐藏）。
> - 界面由服务端渲染 + 原型静态资源（CSS 限定在 `.kb2` 容器内；JS 负责折叠/搜索清空/单卡切换/未保存保护/确认对话框），没有复制原型内存数据库、示例数据、模拟按钮、假识别或假问答；问答测试入口仍指向第一批真实页面（规则模式如实提示未连接模型）。
> - 侧栏（app.py）：知识四项 = 全部主题（主链接）/ 全部资料 / 添加资料（仅管理员或 products 授权可见，后端仍强制）/ 问答测试；默认入口从旧「全部知识」列表改为全部主题，旧列表保留在 `?view=all`。
> - 该轮同时把侧栏与默认入口的既有第一批测试断言更新到本设计（涉及 7 个测试文件的最小改动，原因与范围见《企业知识管理-第二批实施记录》）。

- [x] HTTP临时库测试匿名/跨企业/仅阅读/授权成员、导航入口、真实上传及下载、保存后重新读取（test_knowledge_phase2_http，14 项；含 GET 不变更数据、受认证下载、旧入口兼容与全部写路由覆盖）。
- [x] 按cd5f3e9原型移植静态样式/交互并替换演示数据为真实接口；禁止复制原型内存DB/模拟按钮/假识别/假问答（static/knowledge-phase2.css 由原型 style.css 机器转换并限定 .kb2；交互 JS 重写为真实表单/接口）。
- [x] 全部主题与内部主题文件树+中央独立分组折叠；只在规定页面出现；完整返回与状态恢复（服务端渲染 + sessionStorage 折叠状态；material 页 `from=` 返回实际入口）。
- [x] 单卡输入三模式与真实批次三步、缺项定位、发布确认、未保存保护、只读入口差异；已保存批次离开后可从正式界面找回（添加资料页「最近批次」= kb2_list_batches）。
- [x] 旧资料在全部资料不自动关联；主题树唯一文件入口，原件是受认证企业下载/预览，不公开地址（下载路由企业归属校验 + sha256；中央「关联资料」不出文件打开按钮）。
- [x] Codex实测浏览器主路径与320/390；非阻断布局小缺陷登记，不重复打磨。证据见《第二批验收记录》及其截图。

### Task 4: 文件更新与灵活字段/模板

**Files:** Extend phase2服务/UI；Create test_knowledge_phase2_versions_templates.py；界面参考v0.2模板原型，不新增主侧栏分类。

**Interfaces:** kb2_save_template(tenant_id,user_id,template_id,fields)->version_id; kb2_adopt_template(tenant_id,user_id,item_id,template_version_id,confirmed=False)->dict; kb2_replace_file(tenant_id,user_id,item_id,filename,data)->draft_id。

> **2026-10-13 实现同步（Hermes，任务4）**：计划列出的三个接口保持原名与语义；实际落地的接口与边界：
> - 模板：`kb2_save_template(tenant_id,user_id,template_id=None,fields,name=None)->version_id`（新模板 v1；已有模板追加新版本，版本行不可变）；`kb2_list_templates` / `kb2_get_template`（含全部历史版本）。字段类型 `text/textarea/number/select/date/bool`（= 单/多行文字、数字＋单位、选项、日期、是/否）；字段身份=key（改类型/改 key 按新身份，旧值不自动转换）；选项字段至少两个不重复选项。
> - 采用：`kb2_adopt_template(...,confirmed=False)` 只返回对照（新增/移除/改类型/改名/必填缺项，不改数据）；`confirmed=True` 才应用且只改草稿（绑定+字段行；被移除/改类型的旧行保留为历史 removed=1 不删除；发布版本与原件不动；重复确认幂等 `already=True`）。
> - 字段值：`kb2_set_item_field(tenant_id,user_id,item_id,field_key,value/unit/checked/allow_customer/source)->dict` 与 `kb2_list_item_fields`；0 与「否」是有效值、未填≠0（NULL 未填与 '0'/'false' 区分）；每个值必须有来源（来源段落=本资料真实段落，或「人工填写」）；改值/改来源撤销该字段核对；发布时 `_kb2_prepare_publish_fields_content`（挂入第一批 `publish_knowledge_draft`）校验新版必填并写入字段快照（老资料不采用新模板不受影响）。
> - 文件更新：`kb2_replace_file(...)->{item_id,batch_id,source_id,draft_id,status,baseline_version,diff}`：新原件私有保存（token+sha256）+ 新草稿（同一资料 ID）+ `kb2_source_replacement_diff` 位置/文本对照（SequenceMatcher：新增/变更/移除/未变）；旧线上版本在核对发布前保持；已有未发布草稿时拒绝（人工修改不被静默覆盖）；核对后经批次发布路径发布新版，旧版本转历史。替换批次 `purpose='replace_file'` 不进「最近批次」列表。
> - 界面：`/admin/knowledge/templates`（列表+新建）、`/templates/<id>`（编辑保存为新版本+版本记录）、`/fields/<item_id>`（模板绑定/采用对照预览+确认/逐字段按类型编辑）；资料页「更新文件」（仅已发布且无草稿）与批次页「文件更新对照」区。
> - 判别力测试：`test_knowledge_phase2_templates.py` 11 项（含升级取消不改值、旧线上与原件保留、发布失败不半更新、连续更新/重复确认不重复版本）；HTTP 往返 `test_knowledge_phase2_http_round2.py`。原因与证据见《企业知识管理-第二批实施记录》。

- [x] 模板五种简单类型（单/多行文字、数字单位、选项、日期、是/否），字段身份/顺序/必填/提示/来源/用途/核对；0/否有效，未选择不同。
- [x] 模板版本不可变；改类型新字段身份；升级对照且确认后只作用草稿，保留旧字段历史及已有草稿，不触线上。
- [x] 文件更新新原件/新草稿，位置/文本差异对照；人工修改不静默覆盖；成员核对后发布新版，历史可看（旧版本转历史、版本记录保留）。
- [x] 测试模板升级取消、旧线上与原件保留、发布失败不半更新、连续更新/重复确认不重复版本。

### Task 5: 真实知识准备与对外AI证据

**Files:** Modify enterprise_knowledge.py相关projection/evidence钩子与phase2服务；test_knowledge_phase2_ai_gates.py。既有模型服务复用，不申请新密钥。

**Interfaces:** 复用list_customer_qa_evidence与knowledge_evidence_valid_after_model；增加按段source pin资格过滤（tenant/current-published/source版本/checked/usage/AI/expiry）及主题引用的复核。

> **2026-10-13 实现同步（Hermes，任务5）**：
> - 扩展（不新建平行体系）：`list_customer_qa_evidence` 追加 `kb2_list_topic_qa_evidence`（已发布主题=对外+授权+未到期 → 逐段过滤）与 `kb2_list_item_field_qa_evidence`（字段固定 = 已核对+允许客户引用+有值（0/否有效）+有来源）；`knowledge_evidence_valid_after_model` 增加 `topic`/`item_field` 分支（`kb2_evidence_valid_after_model`）；app 证据格式扩展主题/字段标签与引用（服务端原文+真实版本）。
> - 逐段资格（`_kb2_paragraph_evidence_gate`）：主题授权不绕过来源限制——来源资料须已发布+对外+授权+未过期+非仅登记原件+处理完成，且所钉版本=当前发布版本（换版/替换后按不合格过滤，不静默换版；成员显式「重新核对来源」后才恢复）。混合主题只返回合格段（`kb2_topic_ai_state` 同时给出合格/总段落数与原因，界面标签由此计算）。
> - 模型前后：发送前本地过滤（不合格段不送模型）；返回后复核（主题版本/授权/段文字/来源资格、字段快照值），生成期间撤权、停用、到期、换版、字段变更 → 拒绝旧证据（按资料不足处理）。
> - 测试（stub 模型，不联网）：`test_knowledge_phase2_ai_gates.py` 12 项（合格段/仅内部/未授权主题/混合主题/换版/仅登记/0与否/字段变更/生成期间撤权与换版/服务端引用身份）；真实模型联调仍按待验证（B5）保留，规则模式明确未连接。

- [x] 测试主题授权不能绕过内用/未核对/撤权/到期/换版来源；混合段落只返回合格段；字段0/否及来源闸门。
- [x] 真实本地分块准备到确切发布版本（第二批发布路径已随发布执行 `prepare_knowledge_version`），AI标签由真实资格计算，不按计时（主题双标签由 `kb2_topic_ai_state` 逐段资格计算）。
- [x] 更新草稿不改变旧线上；新版发布后旧主题来源不静默换版，明确重新核对（`kb2_renew_topic_paragraph_source` / `kb2_refresh_relation_pin` + 编辑页/关联区按钮）；模型等待期间撤权/换版能拦旧证据。
- [x] 使用可控模型stub验证调用前后过滤与引用身份，报告为程序验收；真实模型联调仍待验证，规则模式明确未连接。

### Task 6: 联合验收、缺陷登记与交付

**Files:** docs/企业知识管理-第二批验收记录.md；docs/开发状态与变更记录.md；docs/README.md及v0.3当前状态；保持原型历史可用。

- [x] 必要自动测试：新数据/解析/HTTP/版本模板/AI闸门 + 受影响第一批/产品/权限回归；临时工作副本完整 unittest discover 704 项通过，日志见《第二批验收记录》。
- [x] 独立隔离服务 18874（临时DB/私有存储），Codex实点新建主题/真实混合导入/核对/主题确认/发布/源码定位/替换/模板升级/只读与返回。
- [x] 重大阻断修复后复验受影响流程；其他小bug按 B2-N01–N06 登记暂缓。真实模型 B5 依用户决定待联调。
- [x] 已验证完整切片由Codex做Git提交；Hermes不自行推送/部署/提交，未完成不能标完成。
- [x] 最终交付主要改动文件、真实验证结果、未完成/模型B5与小bug、正式界面地址和提交号。

## 执行顺序与范围确认

用户已指定Hermes开工并确认旧资料先保留全集、人工主题关联。先Task1→2→3，随后4→5→6；常规实现可由实现者决定。保持本地规则实现为真实来源摘录/候选，不冒充LLM能力；真实模型缺口如实保留。OCR/视频理解/网页采集/全局图谱/新Agent/全量PIM与公开部署不进入本批。

解析参照：[pypdf官方提取说明](https://github.com/py-pdf/pypdf/blob/main/docs/user/extract-text.md)、[python-docx官方接口](https://python-docx.readthedocs.io/en/latest/user/api-concepts.html)。扫描图像不能被文字提取当成已识别；复杂表格留待成员核对。

> **2026-10-12 B2-B08 修复同步（Hermes）**：Task4/5 交付后，独立验收复现「历史批次发布覆盖更新原件当前草稿」（B2-B08）与「恢复历史后发布被最新全局字段覆盖」。本轮以**精确来源所有权**修复（当前草稿归属校验；守卫接入发布/改段/核对/补人工/重试/刷新兜底，拒绝在任何写入前），并从字段页/资料页给出当前草稿的真实发布位置（`kb2_item_publish_context`）；恢复草稿保留恢复点不可变字段快照。判别力回归、影子运行与命令见《企业知识管理-第二批实施记录》新增节「B2-B08 精确来源所有权修复与 UI 收口」。Task 6 checklist 仍不勾（独立复验未完成、第二批未完成）。
