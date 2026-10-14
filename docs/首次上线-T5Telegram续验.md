# T5 续验：Telegram 测试机器人与收件人

日期：2026-10-14。本地 Hermes CLI 开发，Codex 独立审查、隔离回归、真实 Chrome 操作与云端数据核对。状态：**单企业、单 Telegram 测试机器人的下列真实流程通过；T5 全范围未通过，未公开上线。**

## 条件与实际环境

- 用户指定机器人 `@Growave_v2bot`，自己的 Telegram 账号为测试收件人，授权标明 T5 的测试收发，并配合断开后重新填写令牌。曾贴到聊天的令牌已由用户在 BotFather 重置；新令牌只填产品连接页，未输出或进入 Git。
- 受邀实例 `https://pilot.alpsaga.tech`，企业 `pilot-t4`；连接 `1b1332d6081e82010938ae4e`，会话 `85c4a936fab51b8efbdcbdcd`。用户身份仅在证据中保存哈希 `be0b6fa85620`，截图遮蔽客户身份和询价凭据。
- 云端原无 LangBot 服务。本轮部署既有传输层 4.10.11，冻结镜像 `rockchin/langbot@sha256:ed8c7f19d7e44cda8b59a803be78c4cbecd294e7de1e53d114517f2c034e6e40`；核心仅宿主回环 5300，插件不发布端口，内部控制 5400。Box 关闭，不授予 b2b 用户 Docker 权限。平台仍是问答与授权主脑，不恢复共享 Dify。
- 最终应用目录 `b2b-release-20261014-t5-r4`，源码及全文件清单核对通过、HTTPS 200；旧版本保留，无业务库迁移，不切换生产流量。

## 实际阻断与最小修复

**T5-B03：创建机器人未启动收发。** 用户通过真实页面授权后，界面显示已连接但没有入站。独立核对冻结版本源码及云端运行日志：LangBot 的创建接口只 `load_bot`，更新接口才调用 `run()`。`business_channel_ui.py` 在创建返回 UUID 后补一次 PUT 启用，成功后才标记连接；启用失败尽力删除新建上游 bot、清空本地外部身份，页面只显示安全错误提示。无 UI 改版。

初次连接在旧代码下由运维启动现有 bot 以继续真实联调；安装补修后，用户重新连接通过真实页面完成 POST→PUT 激活，并发送新入站。后者才是连接补修的实机通过证据，不能用运维启动代替产品流程。

**部署必要修补：** nginx 的 LangBot 回调令牌路径单独关闭访问日志；假令牌 HTTPS 请求 404，访问日志中无该标记。新增两个容器的标签/本数据目录挂载预检和备份停写：缺失、被替换、外部挂载或 Docker 无法核对时，停写前拒绝；只恢复原本运行的容器。runtime 内 root 私有的数据库、配置、传输密钥和内部运维密码随既有停写快照保存。

收口另以当前真实 bot/传输密钥核对日志，仅输出匹配布尔结果：两个 LangBot 容器日志、应用 journal 均无当前传输凭据，nginx 访问日志无实际 Telegram 回调凭据；`t5-telegram-active-secret-log-check.log` 全部为 false。

## 独立验收

| 流程 | 实际结果 | 主要证据（`evidence/first-launch-t5-telegram/`） |
| --- | --- | --- |
| 连接、默认关闭与入站 | 新连接 AI 关闭；用户真实发送入站 1014，进入正确会话，仅 buyer 记录，无自动回答；原管理员负责人不变 | `t5-telegram-confirm-ingress-text.log`、`grounded-inbox.png` |
| 开启确认、取消 | 产品界面取消后仍关闭，勾选明确确认后仅开启此 Telegram 测试连接；两个 WhatsApp 连接始终关闭 | `ui-ai-activation.json`、`ai-confirm.png`、`ai-enabled.png` |
| 有据问答与真实收件人 | 使用已核准的合成文章 v1；真实 DeepSeek Flash 选择资料，HTTP 200、883ms、307/85 tokens；回答为周二至周四 10:00–16:00，用户回传实际收到的同一回答 | `t5-telegram-rfq-and-model-evidence.log`、`user-confirmations.json` |
| 人工暂停、原账号回复 | Chrome 操作暂停并发送“UI人工接管 1014”；用户暂停后入站被保存，没有新的 AI 问答；RFQ 提交回执属于业务动作，独立记录，不当成 AI 问答 | `manual-reply.png`、`ui-manual-rfq.json`、模型/消息元数据 |
| RFQ 发送、提交、修订 | 原机器人发 HTTPS 链接；匿名 Chrome 填写合成联系信息，数量 4→5；仍一张 RFQ `fc84ef49ea090479b99d4caf`、v2、同会话/客户、销售负责人 `t4-admin@example.invalid`；头部显示所属测试企业，用户确认收到链接 | `rfq-link-sent.png`、`rfq-revised.png`、`t5-telegram-real-reconnect-and-duplicate.log` |
| 断开、重连 | Chrome 正式断开→用户在同一页面重填令牌→新 bot 实例激活；外部机器人、连接、客户身份、原会话/RFQ/负责人保留，AI 关闭，新真实入站正确路由 | `disconnected.png`、`t5-telegram-real-reconnect-and-duplicate.log` |
| 重复回调 | 当前重连后的真实入站 UUID 经 HTTPS 重放两次，均 duplicate / skip_pipeline=true；会话、消息、RFQ、版本、线索和任务计数不变 | `t5-telegram-real-reconnect-and-duplicate.log` |
| 发送失败、恢复与重试 | 受控停止真实 LangBot 核心；UI 显示发送失败，原消息保留且可重试。恢复服务后 UI 重试成功，同一消息 `9b90e37fcf8f110f8182b98e` 尝试 2 次；再经 HTTPS 重试为 no-op，用户确认只收到一条 | `send-failed.png`、`retry-sent.png`、`ui-retry.json`、最终存储核对、`user-confirmations.json` |
| 备份停写与隔离恢复 | 实际停应用、企业 WhatsApp 桥和两个 LangBot 容器，快照后恢复。快照 10.026 秒，隔离恢复核验 4.354 秒；恢复 2 张 RFQ/1 个核验原件及 runtime 数据库、配置、密钥、managed 标记；文件哈希/0600、停用测试管理员一致 | `t5-telegram-final-cleanup-backup-restore.log` |
| 重启后实际收信 | 最后用户发送“备份后入站 1014”，原会话保存一次，AI 关闭，没有自动回答；原 RFQ v2/数量5保留，HTTPS 200、全清单核对通过 | `t5-telegram-post-backup-final-state.log` |

隔离恢复副本**没有启动真实机器人**，避免与原实例同时轮询。上述证明恢复包完整、原实例停写后可继续收信；不冒充“已从恢复副本运行 Telegram 完整业务流程”。新传输层的这一限制留给正式彩排/恢复方案评审。

回执边界：LangBot 发送 API 接受、平台 `sent` 与用户手机实际收到分别取证。未新增 Telegram provider message ID / read receipt 集成，存储没有被改成 `delivered`；不据 UI 文案宣称供应商已确认送达。

## 回归、工具限制与收口

- 部署/备份/nginx 相关独立副本 **36 项通过，45.757 秒，退出 0**；连接补修及共享渠道/桥接独立副本 **57 项通过，32.752 秒，退出 0**。制作方红绿日志保留，不替代这两次独立检查；不重跑无关全库。
- 首次启动等待不足后服务就绪，重试内部初始化成功；首次误用未安装的 `b2b-backup.service` 未执行停写，随后直接运行既有快照脚本完成真实核验；当前实际定时链为 `b2b-offsite-backup.timer`。若干诊断列名/路径及浏览器等待修正仅属验收工具问题，没有据此修改业务规则。重试记录内容为 JSON，早先 SQL 字面比较计数 0 不作结论，以原消息 ID、解码后唯一性和用户收件数量为准。
- 知识启用/停用需要站内确认，首次浏览器脚本未确认而未执行；补齐确认后完成。清理时连接 AI 已关闭，界面隐藏会话开关；不把定位超时当产品缺陷，也不声称已恢复局部暂停为 0。最终测试会话保留人工暂停为 1。
- 最终三个连接 enabled、AI 均关闭；合成文章停用；临时 QA 管理员停用且会话 0；测试 RFQ 链接撤销。标记测试消息、询价和审计历史保留。机器人及 root 私有运维配置保留供后续产品内测试；本地/云端临时 QA 凭据删除。

## 剩余条件与下一项

本轮只有一个真实测试企业、一个 Telegram bot。多企业/更多 Telegram bot 的实机路由不能用隔离回归代替；既有 WhatsApp 双账号实际证据仍按前一切片保留。官网聊天尚未真实嵌入 HTTPS 企业网站，第二企业实机连接仍缺条件；**不减少 T0 首发渠道，不放行全部 T5。**

T3 真实资料核准、T4 原未闭项、T5-N01/N03 及 T6 运营职责仍按原记录处理。T6 可在用户指定后启动不依赖这些渠道条件的运营准备；T7 需受影响的真实资料、部署与渠道前置条件具备；T8 仍须用户确认具体上线方案。连接服务内部 JWT 续期步骤见 [LangBot 运维说明](../deploy/langbot-pilot.md)，不靠开发聊天维持服务。
