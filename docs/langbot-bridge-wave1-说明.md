# LangBot 桥接 · 第一波（传输层）说明 — 2026-10-06

> 定位：LangBot 作为「渠道传输层」（收 / 发），平台保持「主脑」（治理 / 分流 / 任务 / RFQ / 审计）。
> 状态：spike 第一波跑通（合成事件 + 容器真实网络路径 + 全量测试 321 OK）；真实 Telegram 收尾待机器人接入。

## 架构

```
社交平台(Telegram/Discord/…)
   → LangBot（平台适配器）
   → Webhook 推送 → 平台: POST /api/v1/channels/langbot/webhook/<token>
平台出站总线（queued → sent / failed）
   → LangBot: POST /api/v1/platform/bots/<bot_uuid>/send_message
   → 原平台
```

## 收信契约（对齐 LangBot webhook_pusher.py 源码）

- `POST /api/v1/channels/langbot/webhook/<token>`；token = 渠道账户 `config.webhook_token`（URL 即凭据；不符 404，fail-closed）
- Body：`{"uuid", "event_type":"bot.person_message", "data":{"bot_uuid","adapter_name","sender":{"id","name"},"message":[{"type":"Plain","text"}],"timestamp"}}`
- 响应：`{"status":"ok","outcome":…,"skip_pipeline":true}` —— **恒 true：平台是唯一主脑，LangBot 自身流水线不介入**
- 去重键 = `uuid`；`adapter_name` 落档 `channel_identities.platform_meta`（收件箱显示「平台 / 用户ID」）
- 渠道停用 → 200 `skipped`；非 person 消息暂不处理

## 发送契约

- `POST {LANGBOT_API_BASE:-http://127.0.0.1:5300}/api/v1/platform/bots/{bot_uuid}/send_message`
- Header：`X-API-Key: $LANGBOT_API_KEY`（LangBot `config.yaml` 的 `api.global_api_key`；本机值存 `~/.alpsaga/langbot_spike.env`，不回显）
- Body：`{"target_type":"person","target_id":<平台用户ID>,"message_chain":[{"type":"Plain","text":…}]}` → `{"code":0}`
- 注意：不回传消息 ID → 出站总线 `external_message_id` 置 **NULL**（空串会撞唯一索引 `idx_messages_external`）

## 本机运行（Colima + Docker）

- 服务目录：`~/services/LangBot`（v4.10.11，commit 273b883，LICENSE=Apache-2.0）；compose 含 `langbot` + `langbot_plugin_runtime`
- 宿主访问 LangBot：`http://127.0.0.1:5300`；容器访问宿主平台：`http://host.docker.internal:8137`（已实测通）
- 网络 quirks：Mac 国际网络需代理（系统代理 `127.0.0.1:7897`）——终端/脚本需显式注入 `HTTPS_PROXY`/`HTTP_PROXY`（`NO_PROXY=127.0.0.1,localhost`）；colima VM 内 Docker Hub 直连可用
- LangBot「API 集成 › Webhooks」管理路由不认 API-Key（仅用户令牌）→ 本波直接写 `data/langbot.db` 的 `webhooks` 表
- 平台进程（8137）所需环境变量：`LANGBOT_API_KEY`；渠道账户 config：`{bot_uuid, webhook_token}`

## 未做 / 下一步

- 收尾：真实机器人接入（BotFather 新 bot → 令牌 → 经 LangBot API 建 bot → 回填 bot_uuid → 真人 E2E 取证）
- 第二波：Dify 接入（问答引擎 + 知识库同步）——另行授权后执行
- 迁移期：企微 / 微信客服 / 公众号 / Discord / LINE / 飞书逐平台切换；WhatsApp 走官方 API 路线
