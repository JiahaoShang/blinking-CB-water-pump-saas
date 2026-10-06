# Dify 问答第二波 · 说明（LangBot 渠道资料问答接入 Dify 引擎）

- 日期：2026-10-07
- 对应提交：`a82b3b7`（代码与测试；本文档随其后的文档提交）
- 范围：**只换两层**（AI 问答层引入 Dify；渠道层沿用第一波 LangBot 桥接）——平台内核（收件箱/分流/转人工/出站总线/审计）原样保留，**平台仍是唯一主脑**。
- 前置：第一波「LangBot 桥接（传输层）」已完成（见 `docs/langbot-bridge-wave1-说明.md`）。

## 1. 目标与边界

本文档回答的验收口径：让 **LangBot 渠道的资料问答**由本机 **Dify 问答应用**承担——平台把问题交给 Dify、把答复落库并回给买方；资料（知识库）来自平台**已核准已发布**的产品快照，**发布新版本自动同步**、渠道页另提供**手动同步按钮**兜底。

明确不做（本波）：多租户/SaaS 化；Dify 源码进入产品线（Dify 仅本机运行、真实用户不可见，见 grill-me 决策 #13）；WhatsApp 切桥接（仍走官方双轨待定）；网页聊天与其他渠道的问答路径不变（仍走平台本地「资料问答」）。

## 2. 架构与数据流（本机 spike 形态）

```
Telegram（真实用户）
   │  ①消息
   ▼
LangBot（Docker，5300；纯传输层，不介入问答）
   │  ②Webhook 推送（URL 令牌即凭据，恒 skip_pipeline=true）
   ▼
平台（8137）── 分流 ⑤ 提问样文本
   │        ├─ 优先：POST http://127.0.0.1:8090/v1/chat-messages（Dify，blocking）
   │        │         └─ 知识库：「Alpsaga 已核准产品资料」（本机 Dify 数据集）
   │        ├─ 哨兵/空答 → 按「资料外」口径回复 + 转人工提示（needs_human 落库）
   │        └─ 引擎不可用 → 静默回落平台本地资料问答（通道不中断）
   │
   │  ③答复落库（answers；来源标注 qa_source=dify|local）
   ▼
出站总线（queued → sent/failed）
   │  ④POST /api/v1/platform/bots/<bot_uuid>/send_message（X-API-Key）
   ▼
LangBot ──⑤──▶ Telegram 用户
```

知识库同步（另有链路）：平台「已核准已发布快照」→ `kb_sync_state` 台账（内容哈希幂等）→ Dify 数据集服务 API（`create-by-text` / `update-by-text` / `DELETE`）。

## 3. 问答契约（平台 ↔ Dify）

- **调用**：`POST {DIFY_QA_URL}/chat-messages`，body `{inputs:{}, query, response_mode:"blocking", conversation_id:"", user:"langbot:<外部用户ID>"}`，头 `Authorization: Bearer <DIFY_QA_APP_KEY>`；超时 45s。
- **答复清理**：剥掉 `<think>…</think>` 推理块与 `<!--dify-deepseek-reasoning-->` 渠道标记（模型为推理型，原始答复会携带推理文本；清理后才是买方可见文本）。
- **哨兵**：守护提示词规定「资料外整条回复只输出 `[[NO_ANSWER]]`」；命中哨兵或清理后为空 → 平台按既有「资料外」口径回复 `NO_APPROVED_BASIS_ANSWER`（＋`CHANNEL_HUMAN_HINT`），`answers.answer_status='needs_human'` ——与本地路径完全同口径，后续「人工」转交、收件箱展示、审计链路不变。
- **引用**：`retriever_resources`（兼容顶层与 1.17 的 `metadata.retriever_resources`）→ `citations_json`（`{label:"知识库", filename:文档名, location:"检索片段 N", …}`）；收件箱与参考客户端按既有样式渲染（空 `product_version` 已容忍）。
- **回落**：网络错误/5xx/超时 → `DifyQAUnavailableError` → 静默回落本地 `answer_question_with_mode`（原路径）；**不产生假答案、不静默改变答复来源**——`webhook 响应`与内部结果均带 `qa_source`（`dify` / `local`）供核对。
- **开关**：仅当 `DIFY_QA_APP_KEY` 已配置且渠道为 `langbot` 时启用；未配置 = 与旧版完全一致（本地问答）。

守护提示词（Dify 应用，运营修改点）：仅据知识库回答；资料外只回 `[[NO_ANSWER]]`；跟随提问语言；**纯文本输出（无 Markdown 标记）**；不提及知识库/资料/检索等字样；不承诺未收录信息（价格/交期/售后）。

## 4. 知识库同步契约

- **收集**（`collect_kb_documents`）：遍历本企业产品 → 仅取**已发布版本快照**（`get_published_version`）；跳过撤回机型；文档 = 产品题头（型号·名称＋用途）＋各核准字段（标签：值 单位）＋版本行；**来源被标记「禁止使用」的字段不进入文档**（治理口径与问答路径一致）。
- **台账**（新表 `kb_sync_state`，主键 `tenant_id+product_id`）：`document_id`、`content_hash`（sha256）、`synced_at`、`last_error`。
- **幂等**：内容哈希未变且已有文档 → 跳过（零请求）；变化 → `update-by-text`；新建 → `create-by-text` 后记录文档 ID；`force=True` 强制更新。
- **删除语义**：产品撤回、或不再有已发布版本（不再收编）→ 从知识库删除其文档并清台账——**知识库与「已核准已发布」集合保持一致**。
- **触发**：① 发布/撤回产品后自动同步（`try_sync_kb_quietly`：任何失败不阻断发布，错误留在台账 `last_error`，可用手动按钮重试）；② 渠道页 LangBot 卡片「同步已核准资料到知识库」按钮（**仅管理员**；POST `/admin/channels/sync-dify-kb`，回带新增/更新/删除/跳过/失败计数）；每次同步写审计（`kb_sync / synced`）。
- **未配置**（缺数据集 ID 或密钥）：整体跳过（`configured=false`），页面显示「未配置」提示，按钮不渲染。

## 5. 配置（本机）

| 环境变量 | 用途 | 本机来源文件（600，不入库不进 Git） |
|---|---|---|
| `DIFY_QA_URL` | 问答服务基地址（默认 `http://127.0.0.1:8090/v1`） | 启动命令显式指定 |
| `DIFY_QA_APP_KEY` | 问答应用 API 密钥 | `~/.alpsaga/dify_app_key.env`（`DIFY_APP_API_KEY`） |
| `DIFY_KB_URL` | 知识库服务基地址（可省，默认从 `DIFY_QA_URL` 推导） | 同上 |
| `DIFY_KB_DATASET_ID` | 知识库数据集 ID | `~/.alpsaga/dify_dataset.env`（`DIFY_DATASET_ID`） |
| `DIFY_KB_API_KEY` | 知识库服务密钥（限定到该数据集） | `~/.alpsaga/dify_dataset_key.env`（`DIFY_DATASET_API_KEY`） |

启动示例（本机 8137，含第一波变量）：

```bash
cd "<worktree>" && set -a \
 && . ~/.alpsaga/langbot_spike.env \
 && . ~/.alpsaga/dify_app_key.env \
 && . ~/.alpsaga/dify_dataset.env \
 && . ~/.alpsaga/dify_dataset_key.env && set +a \
 && DIFY_QA_URL=http://127.0.0.1:8090/v1 \
    DIFY_KB_DATASET_ID="$DIFY_DATASET_ID" DIFY_KB_API_KEY="$DIFY_DATASET_API_KEY" DIFY_QA_APP_KEY="$DIFY_APP_API_KEY" \
    python3 tools/demo/start_demo.py --no-open
```

## 6. 本机部署组件（第二波新增）

- **Dify 1.17.1 全套 Docker**（`~/services/dify/docker`，web/nginx 于 **8090**）：本机 colima（CPU 4 / 内存 8GB / 磁盘 60）；数据库/缓存/存储/插件数据全部改用**命名卷**（绕开 9P 挂载 chown 限制）；`.env` 注入宿主代理（`host.docker.internal:7897`，`NO_PROXY` 保内部）；容器外网经宿主代理。
- **账号与插件**：管理员 `admin@alpsaga.tech`（凭据 `~/.alpsaga/dify_admin.env`，600）；插件 `langgenius/deepseek`（v0.0.24）与 `langgenius/siliconflow`（v0.0.61）；默认嵌入模型 `BAAI/bge-m3`（硅基流动，免费）；默认对话模型 `deepseek-flash`（DeepSeek 直连）。
- **问答应用**「产品问答引擎（内部）」：`~/.alpsaga/dify_app.env`（应用 ID）＋`dify_app_key.env`（密钥）；挂载知识库「Alpsaga 已核准产品资料」（`dify_dataset.env` / `dify_dataset_key.env`）。
- 备注（排障留档）：本版 Dify 的「密码加密」实为 Base64 混淆（源码注释自证），登录载荷 `password=base64(明文)`、令牌走 Cookie（`access_token`+`csrf_token`）；库内口令散列 = `base64(hex(pbkdf2_hmac(sha256, 明文, salt, 10000)))`——重置口令时按此式写库。

## 7. 验证与证据

- **测试**：新增 `test_dify_qa.py`（6 项：有据直达/哨兵转人工/引擎故障回落/未配置同旧版/引用两代形状/推理块剥离）与 `test_dify_kb_sync.py`（5 项：渠道页与路由/首建-跳过-force 更新/未配置静默/来源受限排除与删除/撤回删除）；全量 **333 项通过**（基线 322）。
- **真机 E2E**（@Growave_v2bot，真实 Telegram 账号）：
  - 问「SLICE-T1 的流量范围是多少？」→ 平台应答 `qa_source=dify`，答复「SLICE-T1 的流量范围为 30–90 m³/h。」；**与平台发布快照逐值核对一致**（快照 `flow_range=30-90 m³/h`，来源 `SLICE-T1-参数表.txt`）；出站 `delivery_status=sent`；LangBot 日志 `send_message 200`。
  - 问「CB-80A 的扬程范围是多少？」→ 答复「CB-80A 的扬程范围为 18–42 m。」＋**4 条引用落库**（文档名=「CB-80A · CB-80A 清水离心泵」等）；同上送达。
  - 提示词加入「纯文本输出」后直连复验：无 Markdown 标记。
- **同步**：首次同步创建 4 篇（SLICE-T1、SLICE-NOBASIS、CB-80A、CB-120C，全部索引 completed）；复跑 `created=0 updated=0 deleted=0 skipped=4`（幂等）；渠道页显示「问答引擎（Dify）：已接通；知识库已同步 4 篇」。
- 证据目录：`data/evidence/dify-qa-wave2-2026-10-07/`（evidence.txt 原始摘录；本地留存，`data/` 不入 Git）。

## 8. 已知观察项（不影响本波验收）

1. 检索默认 top_k=4：小知识库下引用可列出全部文档（如第二条答复 4 条引用）；后续可按需收敛阈值或条数。
2. LangBot 推送层丢昵称（第一波已记录）：收件箱回退「<平台> 用户 <ID>」，源头修复需自建镜像，仍未做。
3. WhatsApp 双轨（官方对客 / 桥接试验）未开始；个人微信等渠道按决策排最后。
4. Dify 开源版许可边界（禁未授权多租户运营）：本波单公司自用无碍；**产品化阶段需重新评估**（决策 #13 已锁定 Dify 不进产品线）。
5. 实时性：blocking 调用约为数秒级（含推理）；演示环境单进程足够，后续可按需异步化。

## 9. 回滚

- **停用 Dify 问答**：不带 `DIFY_QA_APP_KEY` 重启平台 → langbot 渠道立即回到本地资料问答（前端无感）。
- **停用自动同步**：不带 `DIFY_KB_DATASET_ID`/`DIFY_KB_API_KEY` 重启 → 同步整体跳过（按钮显示未配置）；已同步的文档可手动删除（或恢复配置后按「收回产品」自动清理）。
- 平台与 LangBot 侧无 schema 破坏性变更（仅新增 `kb_sync_state` 表）；Dify 为独立服务，停容器不影响平台其余功能。
