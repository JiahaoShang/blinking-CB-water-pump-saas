# 首次上线 T1 安全审查（Hermes）：入口/对象授权矩阵与最小修复

日期：2026-10-13（含第二轮修复补充，文内以「第一轮/第二轮」区分）。执行者：Hermes（本地）。状态：第一轮审查与实际漏洞的最小修复完成；第二轮按独立验收（Codex）发现的缺口完成最小修复与「先失败复现、后修复验证」；独立验收另行复核、本文件不代表其结论；改动未提交、未推送（按本轮指令）。本文件是 T1 的执行交付与证据索引，不是上线放行单。

2026-10-13 独立验收方补充：上述状态保留为实现方交付时点。第三轮已修新文件助手短写，Codex 最终隔离 282 项全部通过（无跳过），实际浏览器也已复验；当前结论、执行隔离偏差和正式环境待验证条件以[独立验收记录](首次上线-T1独立验收.md)为准。后续 Git 保存由 Codex 按用户本轮授权完成，Hermes 的“未提交”不表示用户尚未授权。

## 0. 边界与基线

- 基线：工作树 `/Users/jiahaoshang/.codex/worktrees/196c/b2b 售前客户管理平台`，分支 `feat/mvp-foundation`，HEAD `6a97cc7`。另有两份未提交文档（`docs/企业知识管理-PRD与交互设计-v0.1.md`、`docs/开发状态与变更记录.md`）与 T0 文件 `docs/首次上线-T0范围与现状核对.md` **两轮均未改**。
- 指令约束执行情况（第一轮）：未 reset、未提交、未推送、未部署、未做任何外部账号操作或消息发送；测试只用**临时数据库、临时上传目录、假密钥**（模型一律 rules/桩模式），未读取 `data/` 业务库、未读取真实密钥、未调用外部服务（渠道出站在鉴权/门控处即被拦下）。
- 指令约束执行情况（第二轮）：同样未 reset/提交/推送/部署/外发。失败复现基线为隔离副本（`git archive HEAD` + 第一轮 diff，cmp 校验与当时工作树一致）；相关回归用临时库/临时目录并显式剔除 DIFY/模型/渠道真实凭据；测试隔离情况与**运行残留清理**记录见 `docs/evidence/first-launch-t1/round2-isolation-note.md`（含两个既有测试文件的非隔离写入观察与处理）。
- 审查方式（第一轮）：通读 `app.py`（审查基线 13459 行）全部 do_GET/do_POST 分派与三个子路由模块（`business_channel_ui.py`、`enterprise_knowledge_ui.py`、`enterprise_knowledge_phase2_ui.py`）的对象授权实现，再以真实 HTTP 请求（临时服务实例）逐组验证；不依赖既有测试数量。第二轮针对独立验收的 4 组缺口逐项复现、修复并回归。
- 本轮新增测试：`test_first_launch_t1_security.py`（41 项＝第一轮 27 项 + 第二轮 14 项；第一轮修复前 9 项失败复现，第二轮修复前 11 个用例（报告 15 行）失败复现，修复后全部通过）。

## 1. 检查矩阵：实际 HTTP 入口 → 鉴权 / 对象授权

结论列的含义：【通过】= 本轮以真实 HTTP 请求验证符合设计；【修复】= 本轮发现缺口并已修；【设计内】= 属既定产品设计，如实记录不作为缺口。

### 1.1 后台面（`/admin/*`，会话 Cookie 鉴权，未列路径一律拒绝）

| 入口 | 认证/授权机制 | 本轮验证 | 结论 |
| --- | --- | --- | --- |
| `GET /admin`、`GET /admin/nav-counts` | 登录即可；数字按模块过滤、仅返回计数（计数查询按登录租户过滤，代码核验） | 匿名 303 到 /login；零权限员工 200 | 通过 |
| `GET /admin/preferences`（模型设置页） | 登录即可查看状态；**配置表单/断开仅管理员渲染**（代码核验）；`POST /admin/preferences/model/*` 仅管理员（授权规则核验 + 既有 test_model_settings 回归） | 零权限员工 GET 200；页面不回显密钥（假密钥断言）；管理员控制项存在（对应用例在回归集内） | 通过（设计内） |
| `/admin/knowledge*`（主题/资料/原件下载/问答测试，含第二批路由） | 登录即可**阅读**（设计：同企业成员可读）；写作/核准/发布需管理员或 `products` 模块（`_kb_can_manage`/`_editor` 服务端拦截，非隐藏入口） | 零权限员工 POST `/admin/knowledge/new` → 403；跨企业原件 404（含 kb2 原件）；匿名 303 | 通过（设计内） |
| `GET/POST /admin/users*`（成员管理/停用/模块授权） | 仅管理员 | 零权限员工 403；管理员 200；停用/撤权即时生效（§1.4） | 通过 |
| `/admin/settings*`、`/admin/audit`、`/admin/analytics`、`/admin/material-tasks`(POST)、`/admin/export/{products,rfqs,leads}(.csv)` | 仅管理员 | 零权限员工全 403；管理员 200/CSV | 通过 |
| `/admin/customer-transfers` | 仅管理员；服务层 `transfer_customer` 再次校验管理员+双方有效性 | 员工 GET 403；管理员 POST 303（§1.4） | 通过 |
| `/admin/demo* /admin/lowara* /admin/products/seed*`（演示工具） | 部署级开关（默认关）+ 仅管理员；关闭时 403 说明页且不落数据 | 普通模式 POST → 403「演示工具未启用」且产品数不变；页面上无演示按钮 | 通过 |
| `/admin/products*`（详情/来源/字段/品类/发布/撤回） | `products` 模块；对象按租户过滤 | 零权限 403；单模块（sales）403；管理员 200；跨企业详情 404 | 通过 |
| `/admin/tasks*`、`/admin/rfqs*`、`/admin/sales*`、`/admin/leads*` | 对应模块（tasks/rfqs/sales）+ **对象级**：`can_view_business_record`（管理员=全部；员工=账号授权范围 ∪ 转交获得） | 单模块员工仅自己模块 200、其余 403；跨企业对象 404 且无写入 | 通过 |
| `/admin/inbox/{slug}*`（网站聊天/Telegram/WhatsApp/个人版） | `inbox` 模块 + 会话级 `can_view_conversation`；发送类动作（reply / followup / rfq-link / retry / send / ai-toggle）再查 `can_send_conversation`；出站总线**投递前**再复核来源模块+对象权限（第二轮，§2.6） | 单模块员工 403；跨企业会话 404、跨企业回写 400 兜底且消息数不变；转交后接替人可看不可发（§1.4） | 通过 |
| `/admin/channels*`、`/admin/channels/accounts/{id}*` | 管理员，或 `channels` 模块 + `can_manage_connection`（负责人/被授权成员）；账号页/状态/二维码同口径 | 单模块员工 403；跨企业账号 404；员工无法创建/管理非本人账号（服务层校验） | 通过 |
| `GET /admin/creative*`（含预览/发布/撤回 POST） | `creative` 模块；模板按租户过滤 | 零权限 403；管理员 200 | 通过 |

### 1.2 匿名入口（不要求后台会话）

| 入口 | 认证/授权机制 | 本轮验证 | 结论 |
| --- | --- | --- | --- |
| `GET /login`、`POST /login`、`GET/POST /logout`、`GET /` | 公开（设计内）。登录 Cookie 会话；`next` 目标消毒 | 普通新库 0 账号、演示口令不可登录；跨站 POST 被拒（§2.2/2.3） | 修复（Origin/文案） |
| `GET /inquiry`（旧询价页，创建会话） | 公开创建（设计内）；发凭据 Cookie 绑定该会话 | — | 设计内 |
| `GET/POST /inquiry/{id}/(message\|confirm\|ask\|human-request\|rfq\|rfq/{id}/revise)` | 会话 Cookie 凭据（`has_buyer_access`，token 只存哈希）+ 同租户 | 无凭据/错凭据 404；同源提交可用；跨站 POST 拒绝 | 修复（Origin） |
| `GET/POST /rfq/{token}`（RFQ 填写链接） | URL 令牌即凭据（`token_urlsafe(32)` 只存哈希、可撤销）；可重开/更新同单 | 代码核验（真实路径由渠道切片既有测试覆盖） | 通过 |
| `GET /products/{slug}`、`GET /s/{site}/products/{slug}(/sources/{id})` | 仅**已发布**版本可见；资料下载三重条件（当次快照 ∧ 许可有效 ∧ 快照归属=当前归属）+ sha256 校验；撤回即 404 | 代码核验 + 既有回归（test_app/test_v1_03 等 163 项） | 通过 |
| `GET /api/v1/public/products/{slug}` | `X-Site-Key` + 站点启用 + Origin 白名单（同 Host 平台页放行） | 站点键缺失/错误 404；跨站 Origin 403（§1.3） | 通过 |
| `GET/POST /api/v1/buyer/*`（会话/消息/需求/推荐/问答/人工/RFQ/联系偏好） | `X-Site-Key` + `Authorization: Bearer`（若访问既有会话）+ Origin 白名单 | 无 Bearer 401；错误站点键 404；跨站 Origin 403；正确凭据 200 | 通过 |
| `POST /api/v1/buyer/handoff/exchange` + `.../handoff-codes` | 平台页面来源（Origin 与 Host 同源才受理）+ 一次性**绑定码**（会话/站点/型号；存哈希、TTL、并发一次消费） | 绑定不符 403（不消费）；换取成功 200；重复换取 409；换得凭据可访问同一会话 | 通过 |
| `POST /api/v1/channels/whatsapp/webhook`（+`/status`、GET verify） | Twilio `X-Twilio-Signature` 验签 / Meta `X-Hub-Signature-256` 验签 / verify_token；未配置一律 fail-closed | 代码核验（签名比较用 `compare_digest`；未接真外部服务） | 通过（待外部复验，§5） |
| `POST /api/v1/channels/langbot/webhook/{token}`、`whatsapp-personal/webhook/{token}` | URL 令牌（存库比对，`compare_digest`）；个人版支持可选 HMAC；停用渠道 200 skipped | 代码核验 | 通过（待外部复验，§5） |
| `GET /reference-client*`、`GET /assets/brand.png` | 公开静态（文档化演示资产） | 200 | 设计内 |
| `GET /files/*` | 一律 404（不提供公开文件直链） | 404 | 通过 |

### 1.3 匿名凭据与跨站交接（重点结论）

- 买方会话凭据：`secrets.token_urlsafe(32)`（256-bit），库中只存 `sha256` 摘要，比较用 `hmac.compare_digest`；交接码同规格 + 一次性消费（写事务 `BEGIN IMMEDIATE` + `used_at IS NULL` 条件更新）；两种凭据都不进 URL（Cookie / Authorization 头 / URL fragment）。
- 交接码经 URL **fragment** 传给产品页（不回传服务器、不进 Referer），公开页另设 `referrer no-referrer`。
- 买方 API 与交接入口的跨站防线为“站点键 + Origin 白名单 + Bearer”；本轮补上的是**会话 Cookie 路由**（/admin、/inquiry、/login）缺失的 Origin 校验（§2.2）。
- 网络暴露面：`app.py` 与 `tools/demo/start_demo.py` 默认绑定 `127.0.0.1`（仅 `--host` 显式传参才对外）；本轮未发现默认对外监听。正式暴露面即 1.2 的公开路径，须在 T4 反代层再次收口（HTTPS、备份目录、管理端口）；同源校验依赖反代保留外部 Host 并覆盖外来转发头（第二轮收紧，§2.7）——应用只应监听本机/内网。

### 1.4 停用 / 撤权 / 客户转交的即时性（重点结论）

- 停用：`get_session_user` 每请求 JOIN `users.active=1`，且 `set_user_active(False)` 同事务删除该用户全部会话 → 既有 Cookie 立即失效（303 去登录页）。已测。
- 模块撤权：模块集合每请求从 `user_modules` 读取（非登录时快照）→ 撤销后下一次请求立即 403。已测；队列中的外发消息同样在投递前被阻止（第二轮，§2.6）。
- 客户转交：管理员在 `/admin/customer-transfers` 提交；`transfer_customer` 检查管理员、原负责人未变化、原子更新会话归属 + 写入 `conversation_access` + 迁移关联线索；**接替人获得历史查看权但未获原账号连接/发送权**（`can_send_conversation` 要求账号负责人或 `connection_members`）——回复按钮被 403 拦下且不落消息。已测。
- 备注（如实记录，非缺口）：跨企业管理员访问他人对象时，GET 统一 404；个别写入口走 400 通用兜底页，两者都不产生读取或写入，属返回码口径差异。

## 2. 实际漏洞、失败复现与最小修复

第一轮修复前共 9 项红（= 失败复现）；对应 5 个根因，全部修复后转绿。第二轮（同日补充，§2.6–2.8）针对独立验收的 3 组技术缺口再做最小修复：复现基线中 11 个用例红（unittest 报告 15 行失败），修复后全部转绿。

### 2.1 登录页无条件公开演示口令（F-A）

- 位置：`app.py` `login_page()`（原 8287 行附近）。
- 根因：页脚注释无条件拼接演示说明“演示账号 demo-admin / demo-sales / demo-engineer，密码与账号相同”。它由 B-05 修复时作为“首次使用前提”写入，未接到 B-05/F-1~F-3 已建立的**部署级演示开关**（`demo_tools`）上。
- 实际风险：任何部署的登录页都公开演示账号名与口令规则。对普通库（不预置演示账号）不构成直接登录，但在以下真实场景直接可用：用 `tools/demo/start_demo.py` 生成的库被暴露/留存（演示库本来就希望可登录，但不应靠公开口令承载）、或运营把演示库当作试点环境。属“默认账号/演示入口”明确关注项。
- 失败复现：`test_login_page_hides_demo_credentials_in_normal_deployment`（修复前：登录页包含 `demo-admin` 与“密码与账号相同”）。
- 修复：演示句仅在 `self.demo_tools=True`（唯一开启入口=start_demo）时渲染；普通部署保留 `create_admin.py` 指引。演示模式下文案保持原样（演示环境照常用）。
- 修复后：普通 302/200 页面无任何演示账号痕迹；演示模式服务（demo_tools=True）页面保留说明（`test_demo_mode_login_page_keeps_documented_demo_accounts`）。

### 2.2 会话 Cookie 跨站 POST 无服务端校验（F-B）

- 位置：`app.py` `do_POST`（/login、/logout、/admin/*、/inquiry/*）。
- 根因：这些写入口只依赖会话/买方 Cookie 的 `SameSite=Lax`（浏览器侧防线），服务端无第二道 Origin 校验。登录 POST 同缺。
- 实际风险（核实后的准确档位）：现代浏览器对跨站 POST 不发送 Lax Cookie，经典 CSRF 被浏览器拦下，**不是当前可直接利用的越权**；剩余面：老浏览器/内嵌 WebView 不执行 SameSite 时无服务端兜底；登录 CSRF（跨站表单替受害者登录攻击者账号）因“跨站响应设置 Cookie”仍可能成立；同时这是纵深防御的系统性缺口（单一防线）。按“概率与影响共同判断”，本轮按最小修复补上第二道闸。
- 失败复现（3 项）：
  - `test_admin_write_post_rejects_cross_site_origin`：带合法管理员 Cookie、`Origin: https://evil.example` 的 `POST /admin/users` 修复前返回 303 **并真实创建账号**；
  - `test_login_post_rejects_cross_site_origin`：跨站登录修复前 303 并**下发会话 Cookie**；
  - `test_inquiry_message_post_rejects_cross_site_origin`：跨站提交 `/inquiry/{id}/message` 修复前 303 并**写入消息**。
- 修复：`same_origin_browser_post()`——请求带 Origin 时要求其 scheme∈{http,https} 且 netloc 属于 {Host, X-Forwarded-Host}，否则 403「请求来源校验未通过」；**不带 Origin 的非浏览器调用（部署脚本/命令行）保持既有行为**（与 `api_site` 既有口径一致）。作用于 /login、/logout、/admin/*、/inquiry/* 的 POST。（第二轮收紧判定口径并加入 Sec-Fetch-Site 拒止，见 §2.7。）
- 修复后：三类跨站 POST 均 403 且零数据变化；同源 Origin 与无 Origin 调用照常（不误杀），回归全覆盖。

### 2.3 登录/买方 Cookie 无 Secure（F-C）

- 位置：`login_post()` 会话 Cookie、`logout()` 清除 Cookie、`do_GET /inquiry` 买方 Cookie（均带 `HttpOnly; SameSite=Lax`，缺 `Secure`）。
- 根因：Cookie 属性常量手写，没有随实际访问协议（反代 `X-Forwarded-Proto`）加 `Secure`；而生产形态是 https（T4 部署）。
- 实际风险：https 部署下若同一 Cookie 也出现在 http 明文请求（降级/中间人把 https 请求改写为 http 的场景），会话凭据可被截获；`Secure` 属性是标准最小防线。
- 失败复现：`test_session_cookie_is_secure_behind_https_proxy`、`test_buyer_cookie_is_secure_behind_https_proxy`（修复前 X-Forwarded-Proto: https 时 Set-Cookie 无 `; Secure`）。
- 修复：新增 `request_scheme()` / `cookie_security_suffix()`；三个 Set-Cookie 处在 `X-Forwarded-Proto=https` 时追加 `; Secure`；本地直连 http 不加（不破坏本地开发流程）。
- 修复后：https 声明下两处 Cookie 均带 `; Secure` 且保留 HttpOnly/SameSite=Lax；纯 http 下 Cookie 仍可用（`test_session_cookie_stays_usable_on_plain_http_local`）。
- 部署要点（写入 §5）：反向代理必须设置 `X-Forwarded-Proto: https`，否则拿不到 Secure；反代如改写 Host 亦应设置 `X-Forwarded-Host`。
- 位置：`app.py` `login_post()`、`logout()`、`do_GET` 的 `/inquiry` 分支。

### 2.4 备份/恢复产物权限随 umask（F-D）

- 位置：`tools/backup_restore.py`。
- 根因：包目录/产物文件用默认权限创建（目录经 `mkdir`、库文件经 sqlite、文件经 `copytree/copy2`），随后 `rename` 出临时目录；最终权限 = 进程 umask 演化结果（macOS/Linux 常见 umask 022 → **目录 0755、文件 0644**）。
- 实际风险：备份包包含完整业务库（成员、会话、消息、RFQ 联系方式等）与全部原件；同机其他本地用户可直接读取。属“备份敏感信息”明确关注项。
- 失败复现：`test_backup_output_permissions_are_owner_only`、`test_restore_check_output_permissions_are_owner_only`（修复前 umask=022 下 0755/0644，实测报错 `0o755`）。
- 修复：新增 `restrict_permissions()`（目录 0700、文件 0600，显式 chmod 不随 umask）；`backup()` 在把包移出临时目录前收紧，`restore_and_verify()` 在复制完成后同样收紧。
- 修复后：两测试断言全树 0700/0600 通过；`test_backup_restore.py` 既有功能回归通过。

### 2.5 诊断/审计日志权限随 umask（F-E）

- 位置：`app.py` 的 `_whatsapp_personal_debug()`（`data/whatsapp_personal_events.log`）与 `/admin/channels/whatsapp-personal/session/start` 的调用方审计（`data/whatsapp_personal_start_audit.log`）。
- 根因：`open(path, "a")` 默认权限随 umask（022 → 0644）。
- 实际风险：诊断日志含买方消息摘要（业务数据）、审计日志含调用方 IP/UA；同机其他本地用户可以读取。与 F-D 同类（日志敏感信息）。
- 失败复现：`test_debug_log_file_permissions_are_owner_only`（修复前 0644，实测报错 `420 != 384`）。
- 修复：新增 `_append_private_log()`（`os.open(..., 0o600)` + `os.fchmod` 收紧旧文件），两处写日志改用它。
- 修复后：日志 0600 断言通过；内容格式不变（`test_whatsapp_personal` 回归通过）。

### 2.6 撤销模块后排队消息仍可投递（F-F，第二轮）

- 位置：`app.py` `dispatch_outbound()`。
- 根因：投递前的句子级复核原先只有 `can_send_conversation`（对象权限），且仅当会话挂企业连接（`channel_account_id`）时执行；**模块授权不参与投递判定**。已复现：员工（inbox+channels）被清空模块后，其 `staff-reply` 排队消息仍被投递（发送器被调用、返回成功）。
- 失败复现（2 个用例、含 5 个来源子用例）：`test_dispatch_rechecks_module_grant_after_revoke`、`test_dispatch_rechecks_module_for_each_staff_send_source`（修复前红：撤权后仍投递）。
- 修复：新增 `outbound_required_module()` 与 `outbound_actor_block_reason()`——实际投递前再复核：发送人须仍在本企业且 `active=1`；管理员按全模块口径放行（**管理员重试/转交流程本身未改**）；员工按「来源模块（`staff-reply`/`rfq-link`→inbox；`lead-reply`→sales；`human-engineer` 与 `engineer-followup:*`→tasks）+ `can_send_conversation`」复核，任一不满足即阻止并落「发送失败」原因。覆盖全部现有员工发送来源（inbox/tasks/sales）。
- 修复后：撤模块、停用、连接成员被移除后，发送器一律不被调用（消息落 failed 可人工重试）；正常授权来源保持可用（来源用例的正向分支、无 actor 的系统消息、管理员路径、`retry_outbound_message` 状态机均验证通过）。错误文案示例：「发送人已无「会话收件箱」模块授权，消息未发出」。

### 2.7 同源判定过宽：任意转发 Host 与协议不比较（F-B 续，第二轮）

- 位置：`app.py` `same_origin_browser_post()`、`api_site()`、handoff 换取端点。
- 根因：第一轮实现的来源集合把**任意 `X-Forwarded-Host`** 当授权依据；且只比较 `netloc`、不比较协议。已复现：`POST /login` 带 `Origin: https://evil.invalid` + `X-Forwarded-Host: evil.invalid` 返回 303；HTTP 请求带 `Origin: https://127.0.0.1:<端口>` 也被当作同源。
- 失败复现（6 项）：`test_same_origin_gate_does_not_trust_forwarded_host`、`test_same_origin_gate_requires_matching_scheme`、`test_same_origin_gate_compares_effective_port`、`test_missing_origin_cross_site_fetch_metadata_is_rejected`、`test_handoff_exchange_scheme_mismatch_is_not_accepted`、`test_buyer_api_same_host_page_scheme_consistency`（修复前红：应 403 的返回 303/200；端口归一分支被误拒）。
- 修复：新增 `origin_matches_request_host()`——Origin 必须与请求同源，三项齐检：**协议**＝实际访问协议（反代经 `X-Forwarded-Proto` 声明；本地直连 http）、**主机**＝请求自身 `Host` 头（**不再信任 `X-Forwarded-Host` 等转发头**）、**有效端口**按协议归一后相等（http:80 / https:443）。`same_origin_browser_post` 在无 Origin 时若 `Sec-Fetch-Site: cross-site` 亦拒绝（脚本/CLI 无这两个头时保持既有行为）。`api_site` 的同主机放行与 handoff 换取改用同一判定；**登记企业网站 Origin 白名单仍按登记值精确匹配、照常受理**（HTTPS 白名单在 http 测试实例上亦正常，既有买方面测试全绿）。
- 修复后：伪造转发 Host、协议不符、端口不符均 403 且零副作用（交接码不被消费）；同源表单/白名单不误杀。
- 部署要件（T4 落地，写入部署手册）：反向代理必须**保留外部 Host、覆盖外来 `X-Forwarded-*` 头**（`X-Forwarded-Proto: https` 仍用于 Secure Cookie 与同源协议判定）；应用只监听本机/内网端口，客户端不得直达。

### 2.8 业务库与私有原件默认属主权限（F-D 续，第二轮）

- 位置：`Database.__init__`（新库落盘）、`add_source()`（产品来源原件）、`enterprise_knowledge.py` 知识导入原件、`enterprise_knowledge_phase2.py` 批次创建与替换文件两处写点。
- 根因：上述写到磁盘的位置均沿用进程 umask（022 → 库 0644、原件 0644、目录 0755）；业务库含连接配置与客户隐私。原 T1 范围仅覆盖备份工具，未覆盖应用自身写点。
- 失败复现（3 项）：`test_new_application_database_is_owner_only`、`test_new_upload_files_are_owner_only`、`test_new_knowledge_files_are_owner_only`（修复前实测 0644/0755）。
- 修复：新增 `write_private_bytes()`（显式 0600）与 `ensure_private_dir()`（新建目录 0700；已存在目录不动）；新库先以 0600 预创建再交给 sqlite 打开；四个原件写点全部改用私有写函数。**既有文件与既有目录不改权限**（不修改现有数据；原目录权限复核归 T4）。
- 修复后：新库 0600、新原件 0600、新建目录 0700；重新打开既有库不会变更其权限（已测「既有库保持 0644 不动」）；`.model-secrets`（既有 0700/0600）与日志类保持第一轮口径。

## 3. 测试命令与证据

### 3.1 命令

```bash
# ① 失败复现（修复前基线）：取修复前提交的干净副本，放入本测试文件运行
#    （等价、可复现；不依赖任何本地未提交状态）
tmp=$(mktemp -d) && git archive HEAD | tar -x -C "$tmp" \
  && cp test_first_launch_t1_security.py "$tmp/" && (cd "$tmp" && python3 -m unittest test_first_launch_t1_security -v)
# ② 修复后（当前工作树）
python3 -m unittest test_first_launch_t1_security -v
# ③ 相关模块回归（权限/渠道/备份/模型/演示门控/登录/买方/交接/knowledge 相邻面）
python3 -m unittest test_backup_restore test_productionization test_v1_permissions \
  test_business_channels test_model_settings test_buyer_access test_handoff_api \
  test_demo_tooling test_console_nav_counts test_app test_v1_channels \
  test_whatsapp_personal test_console_theme -q
# ④ 全量
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -q
```

第二轮（2026-10-13，全部命令先经 `env -u` 剔除 DIFY/模型/渠道真实凭据，使用临时目录与本地发送替身）：

```bash
# ⑤ 第二轮失败复现基线：HEAD + 第一轮 diff（t1-changes.diff）组成隔离副本，
#    放入含第二轮用例的测试文件运行（cmp 校验 app.py/backup_restore.py 与当时工作树一致）
tmp=/tmp/t1-round2-baseline && mkdir -p "$tmp" && (cd 工作树 && git archive HEAD | tar -x -C "$tmp") \
  && (cd "$tmp" && patch -p1 < 工作树/docs/evidence/first-launch-t1/t1-changes.diff \
      && cp 工作树/test_first_launch_t1_security.py . && python3 -m unittest -v test_first_launch_t1_security)
# ⑥ 第二轮修复后（工作树）
python3 -m unittest -v test_first_launch_t1_security
# ⑦ 第二轮相关模块回归（21 个文件：权限/发送/API/知识原件/演示门控等；不重跑 731 全量）
python3 -m unittest -q test_first_launch_t1_security test_business_channels test_v1_channels \
  test_whatsapp_channel test_whatsapp_personal test_meta_whatsapp test_langbot_bridge \
  test_handoff_api test_buyer_api test_buyer_access test_v1_permissions test_productionization \
  test_backup_restore test_model_settings test_app test_demo_tooling \
  test_reference_client_chat_ux test_console_nav_counts test_console_theme \
  test_enterprise_knowledge test_knowledge_ingestion
# ⑧ 补跑解析器相关用例（消除 ⑦ 的条件跳过）与浏览器 JS/买方 API 相邻面
.venv-knowledge/bin/python -m unittest -q test_knowledge_ingestion test_enterprise_knowledge
python3 -m unittest -q test_handoff_browser_js test_chat_message_browser_js test_ai_deepseek
```

### 3.2 结果与证据文件（`docs/evidence/first-launch-t1/`）

| 文件 | 内容 | 结果 |
| --- | --- | --- |
| `repro-before-fix.log` | ① 修复前基线（clean `git archive HEAD` 副本 + 本测试文件，含运行命令注释） | `Ran 27 tests`，**FAILED (failures=9)**，恰为 §2 五个根因的 9 个断言 |
| `after-fix.log` | ② 修复后运行 | `Ran 27 tests`，**OK** |
| `targeted-regression.log` | ③ 相关模块回归 | `Ran 163 tests`，**OK**（54.6s） |
| `full-suite.log` | ④ 全量发现式回归（含本文件与全部既有用例） | `Ran 731 tests in 283.000s`，**OK (skipped=23)**；23 项跳过全部来自既有的条件性跳过机制（Node 环境复核与 `.venv-knowledge` 解析器；条件满足的 shell 下对应用例会实际执行），与本次改动无关（新增 27 项无跳过、均实际执行） |
| `t1-changes.diff` | 第一轮代码改动 diff（`app.py`、`tools/backup_restore.py`） | 供独立复核 |
| `changed-files.txt` | 第一轮改动/新增文件与 `git status` 快照 | 供独立复核 |
| `round2-repro-before-fix.log` | ⑤ 第二轮失败复现基线（HEAD+第一轮 diff 副本 + 41 项测试文件） | `Ran 41 tests`，**FAILED (failures=15)**（= 11 个用例，其中来源覆盖用例含 5 个子用例），恰为 §2.6–2.8 的缺口，`exit=1` |
| `round2-after-fix.log` | ⑥ 第二轮修复后完整 T1 文件 | `Ran 41 tests`，**OK**，`exit=0` |
| `round2-targeted-regression.log` | ⑦ 相关模块回归 + ⑧ 补跑（三段） | ⑦ `Ran 306 tests`，**OK (skipped=22)**（22 项＝`test_knowledge_ingestion` 的解析器条件跳过，已由 ⑧ 补齐）；⑧ `Ran 32 tests` **OK** + `Ran 34 tests` **OK** |
| `round2-changes.diff` | 第二轮代码改动 diff（app.py / enterprise_knowledge.py / enterprise_knowledge_phase2.py；对照=第二轮修复前状态） | 供独立复核 |
| `round2-isolation-note.md` | 第二轮测试隔离说明与运行残留清理记录（含两个既有测试文件非隔离写入的观察与处理） | 供独立复核 |
| `round2-cleanup-record.json` | 本轮运行残留（9 个演示文件）清理清单与 sha256 | 供独立复核 |

说明：同目录下 `independent-baseline-red.log`、`independent-round2-red.log` 与 `ui-*.jpg` 为**独立验收（Codex）写入的产物**，不是 Hermes 生成；保留待独立验收方自行索引。Hermes 产出的两轮证据即上表十类 + 上述新增条目与 `hermes-artifact-hashes.txt`。

## 4. 改动清单（全部为最小修复，无 UI 设计变更）

| 文件 | 位置 | 内容 |
| --- | --- | --- |
| `app.py` | `login_page()` | 演示账号/口令说明改为仅演示模式渲染（F-A） |
| `app.py` | `Handler.request_scheme()` / `cookie_security_suffix()`（新增）；`login_post()`、`logout()`、`do_GET /inquiry` | https 下三处 Cookie 追加 `; Secure`（F-C） |
| `app.py` | `Handler.same_origin_browser_post()`（新增）；`do_POST` 顶部 | 会话 Cookie 路由（/login、/logout、/admin/*、/inquiry/*）跨站 POST 403（F-B） |
| `app.py` | `_append_private_log()`（新增）；`_whatsapp_personal_debug()`；session/start 审计写点 | 诊断/审计日志 0600（F-E） |
| `tools/backup_restore.py` | `restrict_permissions()`（新增）；`backup()`、`restore_and_verify()` | 备份/恢复产物 0700/0600（F-D） |
| `test_first_launch_t1_security.py` | 新文件 | 27 项 T1 测试（复现 + 修复验证 + 矩阵）；第二轮扩至 41 项 |

第二轮改动（2026-10-13 补充，全部为最小修复，无 UI 变更）：

| 文件 | 位置 | 内容 |
| --- | --- | --- |
| `app.py` | `Database.__init__` | 新库 0600 预创建（既有库打开不改权限）（§2.8） |
| `app.py` | `write_private_bytes()` / `ensure_private_dir()`（新增） | 私有原件 0600、新建私有目录 0700（§2.8） |
| `app.py` | `add_source()` | 产品来源原件改用私有写（§2.8） |
| `app.py` | `OUTBOUND_SOURCE_MODULES` / `outbound_required_module()` / `outbound_actor_block_reason()`（新增）；`dispatch_outbound()` | 投递前复核来源模块 + 对象权限（§2.6） |
| `app.py` | `Handler.origin_matches_request_host()`（新增）；`same_origin_browser_post()` | 同源按协议/主机/有效端口判定；不信任 X-Forwarded-Host；Sec-Fetch-Site: cross-site 拒绝（§2.7） |
| `app.py` | `api_site()`、handoff 换取端点 | 同主机放行改用同一判定（白名单照常）（§2.7） |
| `enterprise_knowledge.py` | `_knowledge_storage_dir()`、`import_knowledge_file()` | 知识原件私有写与目录收紧（§2.8） |
| `enterprise_knowledge_phase2.py` | `_kb2_storage_dir()`、`kb2_create_batch()`、替换文件写点 | 批次原件私有写与目录收紧（§2.8） |
| `test_first_launch_t1_security.py` | 追加 14 项 | 第二轮复现 + 修复验证 + 锁定用例（含各来源覆盖与隔离权限矩阵） |

未改动：两份既有脏文档、T0 文件、所有产品页面模板与样式；未新增/删除任何路由；未改管理员重试/转交的业务规则与状态机。第二轮另清理了本轮测试运行在 `data/` 的残留（见 `round2-isolation-note.md`）。

## 5. 仍未覆盖 / 待正式环境复验项（正式验收前必须完成）

1. **真实 HTTPS 链路**：`; Secure` 与同源协议判定在真反代下的行为未验——需在真实域名验证（T4）：反代设置 `X-Forwarded-Proto: https` 且保留外部 Host、覆盖外来转发头；本机测试只覆盖了声明式协议（含有效端口归一）。
2. **真实部署界面**：Codex 已在最终本地版本实际登录、提交成员表单并验证停用后的旧会话失效。T4/T5 仍须复验真实 HTTPS、正式官网交接与 CORS；T7 按确认的实际设备验证关键手机操作。本轮没有批准将 Chrome/Safari/iOS/Android 各一次扩大为额外必需验收。
3. **反向代理后的 Origin 判定（第二轮已收紧，部署侧固化）**：同源判定只认请求自身 Host 头——反代**必须保留外部 Host 及必要端口**（nginx 可用 `proxy_set_header Host $http_host`）并覆盖外来 `X-Forwarded-*`；若反代把 Host 改写成内部名，同源表单会被拒绝（fail-closed）。X-Forwarded-Host 不再作为授权依据。此条须写入 T4 部署手册。
4. **数据库与私有原件权限（第二轮已修应用写点）**：新创建的业务库默认 0600、新写入原件 0600、新建私有目录 0700（不随 umask；已测）。**既有**部署目录与文件（如现有 `data/uploads` 0755、既有 `*.db` 0644）保持原样未动——不修改现有数据；T4 部署时统一复核与收紧（部署脚本 `umask 077`/显式 chmod），恢复演练时一并核验。
5. **`.model-secrets/` 不入备份**：T0 §6 已登记（T4 覆盖补齐）；本轮确认其自身权限为 0700/0600 且密钥不回显（已测），备份覆盖问题保持 T4。
6. **渠道 webhook 真链路**：Twilio/Meta/LangBot/个人版桥的验签与路由只做了代码级核对与既有模拟测试回归，未接真实外部服务（符合本轮“不调用外部服务”边界）；T5 联调时复验签名拒绝路径。
7. **跨企业写入口的 400/404 口径**：信息性记录（无泄漏、无写入），如需统一为 404 属后续打磨项，本轮不改。
8. **独立验收**：本轮结论均出自 Hermes 本地执行；按 T0 安排由 Codex 独立复验（含对失败复现与修复的复核）。
9. **Git 提交**：按本轮指令**未提交、未推送**（指令明确要求不提交，不属“待裁定”事项）；改动与未跟踪的测试/证据文件保留在工作树，供独立验收复核后按流程交由对应责任人提交。
10. **测试套件两处既有非隔离写入（登记待办，非本轮修复范围）**：`test_demo_tooling` 每次运行向仓库 `data/uploads` 写入 9 个演示文件；`test_whatsapp_personal` 每次运行向 `data/whatsapp_personal_start_audit.log` 追加 4 行测试审计。均为既有测试行为（先前批次同样如此），本轮已清理本轮运行产生的部分并在隔离副本验证“在副本中运行即可零仓库残留”（见 `round2-isolation-note.md`）。建议后续小切片把这两个测试的写入目标改为临时目录；`data/uploads` 中累计的历史演示文件（1422 个）随 T4/清理统一处理。

## 6. 与既有文档的关系

- 本文件是 T0 §7 “T1 立即用隔离测试企业/账号检查”的**执行结果**，不改变 T0 的通过条件与 T1–T8 依赖。
- 受影响但按指令未改：`docs/开发状态与变更记录.md` 与 T0 文档——两轮均未动（该状态文档的补充由独立验收方 Codex 按分工追加，Hermes 不修改）。`docs/正式化清单.md` 的 F-1~F-5 与 F-A 相关（演示文案门控的延续），未改该文件，仅在本文件说明关联。
- `docs/首次上线-T1独立验收.md`（Codex）与本文件各自独立：其缺口清单是第二轮修复的输入；修复后的复核结论以该文件为准。
- 企业知识读权限（全员可读）与 `/admin/preferences` 只读可见为既定设计（见 `business_channels.py` 文案与 V1-01 记录），本轮按设计内行为记录，未调整。
