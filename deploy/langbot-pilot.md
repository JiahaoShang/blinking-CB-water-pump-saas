# pilot 受邀实例：LangBot 4.10.11 传输栈（T5 Telegram 首发联调）

日期：2026-10-14。制作：本地 Hermes；独立验收：Codex。状态：**受邀云实例已完成启动、认证、回调日志保护、单 bot 真实收发、停写快照及隔离恢复文件核验**，详见 [T5 Telegram 续验](../docs/首次上线-T5Telegram续验.md)。恢复副本未启动真实机器人，不能据此宣称全部首发或完整恢复业务流程通过。本文不是上线放行单。

## 1. 目标形态

两个容器，同一冻结镜像 `rockchin/langbot@sha256:ed8c7f19d7e44cda8b59a803be78c4cbecd294e7de1e53d114517f2c034e6e40`（4.10.11）：

| 容器 | 角色 | 网络 | 宿主端口 | 数据挂载（容器内） |
| --- | --- | --- | --- | --- |
| `b2b-langbot` | 核心（面板/事件回推/API） | `b2b-langbot-net`（私有桥接） | 仅 `127.0.0.1:5300` | `<数据目录>/connection-runtime/langbot/core` → `/app/data` |
| `b2b-langbot-plugin` | 插件运行时（命令 `rt`；控制端口 5400，调试 5401 仅容器内） | 同上 | 不发布 | `<数据目录>/connection-runtime/langbot/plugin` → `/app/data/plugins` |

- Box 关闭（`BOX__ENABLED=false`；不挂宿主 Docker socket）；Dify 与本栈无关；b2b 用户不获得 Docker 权限，全部命令以 root（admin + sudo）执行。
- 数据流：Telegram ↔ LangBot 核心；核心把消息事件 Webhook 推回本平台 `https://pilot.alpsaga.tech/api/v1/channels/langbot/webhook/<token>`（经 nginx 443 → 应用回环 8000；该路径已单独关闭访问日志）；卖家回复由本平台调核心 `http://127.0.0.1:5300`（`X-API-Key`）转投 Telegram。
- 控制端口按镜像内 `rt` 的默认值固定为 **5400**（本机以同 digest 镜像实核：`WebSocket server started on 0.0.0.0:5400`；`5401` 是插件调试服务、鉴权头不同，勿把控制连接指向 5401）。如需改动必须同时给 `rt` 传 `--ws-control-port`，本仓未采用。
- 服务器自身经公网域名访问 443 在本实例已验证（T4 首装自检即走公网 NIC），核心容器回推 HTTPS 域名不受回环 NAT 限制。

## 2. 前置条件

- 服务器已按 [deploy/README.md](README.md) 完成首装（nginx 443、应用回环 8000、备份链）。需要 `docker compose` v2（先跑 `docker compose version` 核对）。
- 本文件、`deploy/langbot-compose.yaml`、`deploy/langbot_precheck.py` 随常规发布包（`make_release.py` 全量包的 `deploy/`）上线；`--patch` 修补包不含这三个新文件。
- Telegram 机器人：在 BotFather 建机器人取得 token；**token 只填平台业务账号连接页的密码输入框**，由平台验证身份并交给本机 LangBot 保存，不入业务库、Git、文档或聊天。

## 3. 首次部署（root；Workbench 逐条）

**① 拉取冻结镜像（按 digest，不用 latest）**

```sh
sudo docker pull rockchin/langbot@sha256:ed8c7f19d7e44cda8b59a803be78c4cbecd294e7de1e53d114517f2c034e6e40
```

**② 数据目录（root 私有；随 `connection-runtime/` 整体进备份）**

```sh
sudo install -d -m 0700 /var/lib/b2b/data/connection-runtime/langbot/core /var/lib/b2b/data/connection-runtime/langbot/plugin
```

**③ 秘密文件 `<数据目录>/connection-runtime/langbot/transport.env`（root 0600，父目录 0700；随停写备份保存；绝不入 Git）**

```sh
sudo install -m 0600 /dev/null /var/lib/b2b/data/connection-runtime/langbot/transport.env
sudo editor /var/lib/b2b/data/connection-runtime/langbot/transport.env
# 内容（示例；真实值自生成、不复制本文）：
#   LANGBOT_API_KEY=<openssl rand -hex 24 生成；与 /etc/b2b/b2b.env 中同名变量必须是同一值>
#   LANGBOT_PLUGIN_RUNTIME_CONTROL_TOKEN=<openssl rand -hex 32 生成；核心与插件运行时共用>
#   #B2B_DATA_DIR=/var/lib/b2b/data   # 仅当 b2b.env 自定义过数据目录时，这里同步写同一值
```

**④ 起栈**

```sh
sudo docker compose --project-name b2b-langbot --env-file /var/lib/b2b/data/connection-runtime/langbot/transport.env -f /opt/b2b/app/deploy/langbot-compose.yaml up -d
# 缺任一秘密变量时 compose 直接报错（${VAR:?}），不会静默用空值起容器
```

**⑤ 自检（不打印任何密钥）**

```sh
sudo docker compose --project-name b2b-langbot --env-file /var/lib/b2b/data/connection-runtime/langbot/transport.env -f /opt/b2b/app/deploy/langbot-compose.yaml ps   # 两容器 Up
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:5300/                                      # 200（核心入口）
sudo docker logs b2b-langbot 2>&1 | grep "Connected to instance-scoped plugin runtime"               # 核心↔运行时已连
sudo docker logs b2b-langbot-plugin 2>&1 | grep "WebSocket server started"                           # 0.0.0.0:5400 控制（5401 调试）
sudo sh -c 'set -a; . /var/lib/b2b/data/connection-runtime/langbot/transport.env; set +a; curl -s -o /dev/null -w "%{http_code}\n" \
  -H "X-API-Key: $LANGBOT_API_KEY" http://127.0.0.1:5300/api/v1/platform/bots'                       # 200：平台↔核心共享密钥通路
```

**⑥ nginx 凭据路径（事件回推走 HTTPS 且不记访问日志）**

模板 `deploy/nginx-b2b.conf.example` 已含 `location ^~ /api/v1/channels/langbot/webhook/`（`access_log off`，转发口径与 `location /` 逐行一致）。按既有口径**仅供 diff 核对、人工应用到线上配置，禁止整文件覆盖**：

```sh
diff -u /opt/b2b/app/deploy/nginx-b2b.conf.example /etc/nginx/sites-available/b2b.conf || true
# 人工应用新增 location 段后：
sudo nginx -t && sudo systemctl reload nginx
# 日志口径核对（触发一次回推后应输出 0）：
sudo grep -c 'langbot/webhook' /var/log/nginx/b2b-access.log || true
```

**⑦ 初始化私有连接服务与平台连接页**

- 运维仅从服务器回环调用 `/api/v1/user/init`（初次创建内部管理员）及 `/api/v1/user/auth`（登录）；随机密码保存在 runtime 下 root 0600 的 `operator.json`，响应令牌不打印。
- 平台 `/etc/b2b/b2b.env` 配置同一 `LANGBOT_API_KEY`、`LANGBOT_API_BASE=http://127.0.0.1:5300` 及 `LANGBOT_USER_TOKEN`，重启应用。用户令牌用于正式 Webhook 管理 API，不能用全局 API-Key 代替。
- 平台「渠道接入 → Telegram 业务账号」填写机器人授权令牌并连接。平台自动验证 bot 身份、登记企业回调、建立 bot 并保存 bot_uuid；不需手填标识或直接修改连接服务数据库。新连接 AI 默认关闭。
- 冻结版本用户 JWT 默认有效期 604800 秒（7 天）；到期前运维用上述私有管理员重新认证，仅更新本机 `LANGBOT_USER_TOKEN` 后重启应用。续期失败时不能称新连接/重连通过；已有 bot 的收发使用全局 API-Key，须分别核验。

**⑧ 备份停写 opt-in（确认平台可收信后执行；见第 5 节）**

```sh
sudo install -m 0600 /dev/null /var/lib/b2b/data/connection-runtime/langbot/managed
```

标记存在后：每日停写备份会先核对并停止两个 LangBot 容器（只停原本在运行的），备份后恢复；缺失/被替换/挂载不属于本数据目录时**失败退出（exit 3），不会误停**。

## 4. 凭据与轮换

| 凭据 | 位置 | 轮换步骤 |
| --- | --- | --- |
| `LANGBOT_API_KEY`（平台↔核心 API 密钥） | `/etc/b2b/b2b.env` 与 `/var/lib/b2b/data/connection-runtime/langbot/transport.env`（必须同值） | 新值写入两文件 → `sudo systemctl restart b2b-app` → `sudo docker compose --project-name b2b-langbot --env-file /var/lib/b2b/data/connection-runtime/langbot/transport.env -f /opt/b2b/app/deploy/langbot-compose.yaml up -d` → 按 3-⑤ 复核 `X-API-Key` 返回 200 |
| `LANGBOT_PLUGIN_RUNTIME_CONTROL_TOKEN`（核心↔插件运行时） | 仅 `/var/lib/b2b/data/connection-runtime/langbot/transport.env`（经 compose 注入两端） | 新值（`openssl rand -hex 32`）→ 同一条 `up -d`（两端一起重建）→ 日志复核 3-⑤ 第 3 项 |
| Telegram bot token | LangBot 面板（存于其数据目录） | 面板内更换并重启机器人；本仓不存档 |

轮换会短暂中断传输；完成后按 3-⑤ 与平台渠道页状态复核。秘密绝不写入 Git、文档、日志或聊天。

## 5. 备份与恢复（含 LangBot 停写）

- `deploy/backup_snapshot.sh` 原样覆盖 `connection-runtime/` 整体：LangBot 的 `core/`、`plugin/` 数据与 `managed` 标记都随包，恢复后栈重新受管；无需为新容器新增备份路径。
- opt-in 停写（标记 = `<数据目录>/connection-runtime/langbot/managed` 普通文件）：停写**之前**用 `deploy/langbot_precheck.py` 核对两个固定容器——存在、带 `b2b.langbot=1` 标签、挂载来自本数据目录（解析后路径）且 docker 可核对；任一不符（缺失、被替换、另一数据根/恢复副本的同名容器、docker 不可用或 inspect 失败）一律失败退出（exit 3）、不产生快照。只停原本运行的容器，经既有 `STOPPED_BRIDGES` 机制在成功后恢复、失败也经 trap 恢复；绝不起动原本未运行的容器。**标记不存在时行为与原来完全一致。**
- 恢复演练口径：把恢复目录当作数据根时，预检会因“挂载不属于本数据目录”拒绝停写（同一 fail-closed 口径，不误停生产栈）；恢复后重建容器仍以本 compose 为准（挂载指向恢复目录仅为演练）。
- 卸载/迁移：先用上述 compose 命令 `down`（保留数据目录），再删除 `managed` 标记。不能只删标记：标记缺失会关闭 LangBot 停写预检，仍运行的容器可能写入半份快照。
- 异地加密链（OSS）复用同一停写快照；runtime 内 `transport.env`、`operator.json` 与 `/etc/b2b/b2b.env` 都属于秘密材料，仅经 age 加密后离机。恢复演练不连接真实 bot，不与原实例同时轮询同一 Telegram 机器人。

## 6. 本机已验证 / 尚未验证（如实）

- 已验证（本机，2026-10-14）：compose 语法与渲染经真实 `docker compose config`（端口/挂载/环境变量/标签逐项核对）；镜像 digest 与本机运行中的同版本镜像一致；核心↔运行时控制端口 5400 由同 digest 镜像实核；nginx 模板新增 location 与既有应用代理逐行一致且关闭访问日志（回归锁见 `test_first_launch_t4_deploy.NginxConfigTests`）；停写 opt-in 的缺失/标签不符/挂载不符/docker 不可用/只停运行中/失败恢复/无标记不变等判别性测试先红后绿（`test_first_launch_t5_telegram.py`；证据 `docs/evidence/first-launch-t5-telegram/`）。
- 后续独立云端验证：上述启动、自检、nginx、单 bot 真实收发及停写/恢复文件核验已通过；实际快照 10.026 秒、隔离恢复核验 4.354 秒，重启后真实入站保留。尚未执行恢复副本真实机器人收发、卸载/内部密钥轮换演练；新 bot 令牌由用户重置并经产品页使用，内部用户 JWT 续期仍按第 4 节执行。

## 7. 变更记录

- 2026-10-14 初版（Hermes）：新增 `deploy/langbot-compose.yaml`、`deploy/langbot_precheck.py`、本文件；`deploy/nginx-b2b.conf.example` 增加 LangBot webhook 凭据路径；`deploy/backup_snapshot.sh` 增加 opt-in 停写；回归 `test_first_launch_t5_telegram.py` 与 `test_first_launch_t4_deploy.py` 两处扩展。
- 2026-10-14 续验（Codex）：实机验证按 T5 记录收口；秘密文件放在 runtime 下随停写备份纳入，补齐内部账号/JWT 自助连接依赖，统一 compose 项目名。初次等待/错误单元名不计为通过；后续实际执行及恢复边界单独取证。
