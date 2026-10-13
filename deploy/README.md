# 首次邀请测试上线：最小 Linux 单机部署配置（T4）

日期：2026-10-13（修复轮）。制作：本地 Hermes；独立验收由 Codex 复跑。状态：**配置与工具就绪；服务器已由用户购买并核对到位；业务程序已于当日由用户按第 3 节在真机完成首装（用户回传：`b2b-app` active、HTTPS 200、续期 dry-run 通过；T4 尚未完成项见 `docs/开发状态与变更记录.md`）；本目录的 systemd/nginx/脚本未在真实 Linux 上执行过（制作环境为 macOS），首装必须在服务器（Workbench）上按第 3 节第 ⑩ 步逐项验收。本目录不构成上线放行。** 真机续期演练（2026-10-13，`certbot renew --dry-run --run-deploy-hooks`）**当日曾实际未通过**：CA 经 80 跳转访问 `https://pilot.alpsaga.tech/.well-known/acme-challenge/<token>` 返回 404；根因=443 最终站点缺 ACME webroot location；已在 443 模板修复（80 跳转保留）。**当日用户已在真机按等效步骤把该 location 应用到线上 443 站点并恢复默认站点，随后 `certbot renew --dry-run --run-deploy-hooks` 真机模拟续期全部成功（用户回传；见 `docs/开发状态与变更记录.md`「T4真实服务器安装与HTTPS续期回传」）**；仓库模板已同步并带回归锁（⑨ d / ⑩ 第 8 项）。重装/迁移后仍须重跑本项确认；本任务修补包内的模板**仅供 diff 核对，禁止覆盖线上配置**（第 11 节）。

实际资源（用户提供、以 Workbench 为准；Codex 本机 SSH 超时未独立复验）：

- 服务器：阿里云香港轻量应用服务器，Ubuntu 24.04.2 x86_64，2 核 / 4GB 内存 / 50GB 磁盘；admin 有 sudo；Python/Git/curl/venv/Docker 已验证；TCP 22/80/443 已开放。
- 域名：`pilot.alpsaga.tech` 已解析到 `47.243.192.209`；HTTPS 证书在部署时用 certbot 申请（见第 3 节 ⑨）。
- 跨境独立备份：**已按用户配置落地（OSS）**——新加坡 `alpsaga-pilot-backup-v1`（标准/LRS/私有/阻止公共访问；专用 RAM 仅 ListObjects 桶与 PutObject/GetObject 桶/*，无删除与管理权限）；ossutil 2.4.0（`/usr/local/bin/ossutil`，root 0600 配置 `/etc/b2b/ossutil.conf`）；服务器加密公钥 `/etc/b2b/backup-age.pub`（`age1c3y8afewj46rtg2g2jugghdkuw4skuwxq5s8wyeun6leg5cxwsvstsyyar`；私钥只在 Mac 与密码管理器，未上服务器）；`age` 已安装。人工加密上传 + 云下载校验 + Mac 端解密 restore-check 已通过（2026-10-13，详见 `docs/开发状态与变更记录.md`）；自动链（第 11 节）待真机验收。R2 方案保留为备选（[offsite-backup-r2-plan.md](offsite-backup-r2-plan.md)）。

适用形态（对照 [T0 清单](../docs/首次上线-T0范围与现状核对.md) 与 [T4 执行方案](../docs/首次上线-T4执行方案.md)）：Python 3 + SQLite 单体应用，单实例、本机回环端口 + 反向 HTTPS 代理；不引入微服务、数据库迁移框架或新界面。首次真实数据进入部署环境前，必须先完成一次备份/恢复演练（见第 6 节）。

异地自动备份链（2026-10-13 追加，待真机验收）：新 `deploy/offsite_backup_oss.sh` + `b2b-offsite-backup.service/timer`（每日 04:30，与本机 03:30 备份错开）——停写快照（复用现有脚本；`B2B_BACKUP_OUT` 把“本次快照”路径钉死）→ 确认应用已恢复（门禁）→ `tar | age -R` 公钥加密（不落明文 tar）→ 仅上传密文与校验文件到 OSS 私有桶 `backups/<时间戳>/`（云端预检拒绝覆盖 + `--ignore-existing`）→ 云下载到隔离 root 0700 目录复核本机 sha256 一致；失败分阶段非零、本地快照与密文保留、绝不把应用留在停机态；不启用云端删除（生命周期待用户确认）。`deploy/backup_snapshot.sh` 增加可选 `B2B_BACKUP_OUT`（默认行为不变；同名目标在停写前拒绝）。本机以假件命令做了判别性测试（26 项，`test_first_launch_t4_offsite.py`）；**真实 Linux/ossutil/age 与定时执行待服务器验收**（安装与验收步骤见第 11 节）。


修复轮变更（2026-10-13，Codex 初审实际阻断）：

- `deploy/backup_snapshot.sh` 重写：以 root 运行、产物 root 私有；停止前保存原活跃状态，**失败也经 trap 恢复**（只恢复原本活跃的应用/受管桥，不启动原本未运行的东西、不触碰其他容器）；受管桥按“本库 `connection-runtime/` 登记账号”自动停写；保留策略 glob 与实际时间戳目录名匹配；运维配置 `/etc/b2b/b2b.env` 进包。
- 首次 HTTPS 引导（HTTP-only 配置 → `nginx -t` → certbot webroot → 443 最终配置）；精确 `server_name` + 默认站点 444（防任意 Host 反射、默认站点不暴露程序）；nginx 日志不记录买方凭据/query（详见 ⑨）。
- 新增 `deploy/make_release.py`：从当前源码白名单导出发布包 + sha256 版本清单（新安装/升级不再 `git clone`）。
- `tools/backup_restore.py`：修复 `..` 路径逃逸（三种形态）与上传根/备份包根符号链接拒绝；`--ops-config` 支持。

续期阻断修复（2026-10-13，真机演练发现；待真机复验）：

- 现象与根因：真机 `certbot renew --dry-run --run-deploy-hooks` 实际未通过——80 全站 301 后 CA 跟随跳转到 HTTPS 取校验文件，443 最终站点缺 ACME webroot location，请求落入 `location /` 被代理进应用（应用无该路径）返回 404。
- 修复：443 站点新增 `location ^~ /.well-known/acme-challenge/ { root /var/www/b2b-acme; default_type text/plain; }`——`^~` 优先于 `location /`，直读 webroot、不代理；80 跳转保留；引导与最终两阶段共用 `/var/www/b2b-acme`。回归锁：`test_first_launch_t4_deploy.py`（先红后绿，锁定“443 保留 ACME webroot”）。
- 复验路径：服务器更新发布包后按 ⑨ c 重装 443 配置，跑 ⑨ d（`certbot renew --dry-run --run-deploy-hooks`）与 ⑩ 第 8 项；**通过前本阻断不视为关闭、不得作为放行依据**。

## 1. 目标布局

```text
/opt/b2b/
  app -> releases/b2b-release-<label>/   # 当前版本（软链切换，回滚=换回旧目录）
  releases/b2b-release-<label>/          # 发布包解包内容（不含 data/、密钥、.git；由 make_release.py 生成）
  venv/                                  # 必建：单元以 /opt/b2b/venv/bin/python3 启动
/var/lib/b2b/
  data/app.db                            # 业务库（--db 指向此处；库权限 0600）
  data/uploads/                          # 全部原件（B2B_UPLOAD_DIR 指向此处）
  data/app.db.model-secrets/             # 企业自定义模型私有配置（随库名；0700/0600）
  data/connection-runtime/<账号>/sessions/            # WhatsApp 个人版桥（单账号模式）登录会话卷
  data/connection-runtime/enterprise/<租户哈希>/{sessions,media,bridge.env}  # 企业专属桥卷（容器内 /app/.sessions、/app/.media）
  data/connection-runtime/enterprise-bridges.json      # 企业桥私有注册表（root:b2b 0640；含逐企业密钥）
/etc/b2b/b2b.env                         # 私有运行配置（root:b2b 0640；密钥只在这里）
/var/backups/b2b/<YYYYmmdd-HHMMSS>/      # 本机备份快照：root 私有（0700/0600），保留 14 天；异地见第 11 节
/var/backups/b2b/<时间戳>.export.tar.gz.age(.sha256)  # 异地链密文与校验文件（root 0600，保留 14 天）
/var/backups/b2b/oss-verify/<时间戳>/    # 云下载复核的隔离目录（root 0700；成功即清、失败留作证据）
```

全部持久状态都在 `/var/lib/b2b` 与 `/etc/b2b` 下，**不在代码目录内**；因此切换 `/opt/b2b/app` 指向的版本不会丢原件（这依赖 `B2B_UPLOAD_DIR`，见第 4 节）。

## 2. 持久映射表（按当前代码实测，不猜路径）

| 资产 | 实际位置（本部署下） | 代码依据 | 备份覆盖 |
| --- | --- | --- | --- |
| 业务数据库（企业/成员/会话/RFQ/人工任务/知识与批次/渠道账号与连接治理/审计/模型调用日志） | `/var/lib/b2b/data/app.db` | `app.py --db`；`Database.__init__` | 是（SQLite 在线备份快照） |
| 产品来源原件（source_documents.storage_path） | `data/uploads/`（配置后存绝对路径；历史库为相对路径） | `app.py add_source` / `resolve_source_storage_path` | 是（逐条核验存在与 sha256） |
| 知识原件（knowledge_sources） | `data/uploads/kb-*` | `enterprise_knowledge.py` | 是 |
| 批次原件（kb2_sources，含嵌套） | `data/uploads/phase2/kb2-*` | `enterprise_knowledge_phase2.py` | 是 |
| 企业模型私有配置 | `data/app.db.model-secrets/`（0700/0600） | `model_settings.py EnterpriseModels` | 是（备份目录 `model-secrets/`，恢复映射为恢复目录下 `<库名>.model-secrets`） |
| 渠道桥登录状态（WhatsApp 个人版 WAHA 会话卷） | `data/connection-runtime/<账号ID>/sessions/`（docker 卷挂载） | `channel_runtime.py provision_personal_bridge` | 是（目录 `connection-runtime/` 整体）。备份脚本自动停写“本库登记账号”的 `b2b-account-*` 与企业桥 `b2b-enterprise-*`（注册表登记 + 归属核对）；企业桥按第 12 节验收后再启用 |
| 企业专属桥注册表与运行目录（含逐企业密钥与媒体卷） | `data/connection-runtime/enterprise-bridges.json`（root:b2b 0640）与 `data/connection-runtime/enterprise/<租户哈希>/{sessions,media,bridge.env}` | `deploy/provision_enterprise_bridge.py`（root，第 12 节）；应用经 `WHATSAPP_PERSONAL_BRIDGES_FILE` 只读 | 是（随 `connection-runtime/` 整体；恢复后重跑部署工具按恢复的注册表重建：重新随机选取固定端口并更新注册表） |
| 受限运维配置（密钥） | `/etc/b2b/b2b.env`（root:b2b 0640） | 单元 `EnvironmentFile` | 是（`ops-config/b2b.env`；恢复时按原相对路径保留；**禁止明文上传云端**） |
| 后台作业状态（无需单独文件：全部在库表内） | `kb_sync_state`、`knowledge_processing`、`kb2_sources.status/processing_owner`、`messages.delivery_status='queued'` | `knowledge_delivery.py`、`enterprise_knowledge*.py`、`tools/channels/outbound_worker.py` | 是（随数据库快照） |
| 服务日志 | systemd journal（stdout/stderr）；诊断日志 `data/whatsapp_personal_events.log`、`data/whatsapp_personal_start_audit.log`（代码目录内、非业务状态） | `app.py _append_private_log` | 不纳入备份（诊断用途）；journald 按系统策略轮转；代码目录内写入的约束见第 10 节 |
| 演示工具开关 | 默认关闭；唯一开启入口 `tools/demo/start_demo.py`（部署单元不调用） | `app.py make_server(demo_tools=...)` | 不适用（见第 8 节门控） |

网络暴露面：应用只监听 `127.0.0.1:8000`，公网仅经 Nginx 的 443（HTTPS）。

## 3. 服务器首安装（精确步骤；在 Workbench 里逐条执行）

> 部署通道说明：Codex 本机 SSH 到服务器超时，实际执行经阿里云控制台 **Workbench**（admin 有 sudo）。以下命令可按段复制；`<label>` 用发布包标签（如 `2026-10-13-a`）替换。

**① 系统包**

```sh
sudo apt-get update
sudo apt-get install -y python3 python3-venv nginx certbot
```

**② 账号与目录**

```sh
sudo useradd --system --create-home --home-dir /var/lib/b2b --shell /usr/sbin/nologin b2b
sudo install -d -m 0700 -o b2b -g b2b /var/lib/b2b/data /var/lib/b2b/data/uploads
sudo install -d -m 0700 -o root -g b2b /etc/b2b /var/backups/b2b
sudo install -d -m 0755 -o root -g root /opt/b2b /opt/b2b/releases
```

**③ 构建发布包（在受信任的构建机上，不要用 git clone——工作内容含未提交修改，HEAD 不代表当前版本）**

```sh
cd <本仓库>
python3 deploy/make_release.py --label <label>
# 产出：dist/b2b-release-<label>.tar.gz 与 dist/b2b-release-<label>.tar.gz.sha256
sha256sum dist/b2b-release-<label>.tar.gz     # 记录到部署记录里
# 传到服务器（scp，或经控制台/Workbench 文件上传到 /tmp）：
scp dist/b2b-release-<label>.tar.gz dist/b2b-release-<label>.tar.gz.sha256 admin@47.243.192.209:/tmp/
```

**④ 服务器上校验并解包（先核对哈希，再解包）**

```sh
cd /tmp
sha256sum -c b2b-release-<label>.tar.gz.sha256          # 必须 OK
sudo tar -xzf b2b-release-<label>.tar.gz -C /opt/b2b/releases
cd /opt/b2b/releases/b2b-release-<label>
sudo sha256sum -c RELEASE-MANIFEST.sha256               # 包内逐文件哈希，必须全 OK
sudo ln -sfn /opt/b2b/releases/b2b-release-<label> /opt/b2b/app
```

**⑤ 运行配置（私有；密钥只在这里）**

```sh
sudo cp /opt/b2b/app/deploy/b2b.env.example /etc/b2b/b2b.env
sudo chown root:b2b /etc/b2b/b2b.env && sudo chmod 0640 /etc/b2b/b2b.env
sudo editor /etc/b2b/b2b.env       # 至少确认 B2B_UPLOAD_DIR；启用模型时再填 AI_MODE/DEEPSEEK_API_KEY
```

**⑥ 虚拟环境（必建；纯文本/手工整理可不装知识依赖，需要解析 PDF/Word 时执行 pip）**

```sh
sudo python3 -m venv /opt/b2b/venv
sudo /opt/b2b/venv/bin/pip install -r /opt/b2b/app/requirements-knowledge.txt   # 仅解析 PDF/Word 需要
```

**⑦ 创建首位管理员（普通新库没有默认账号；口令从标准输入，不出现在命令行）**

```sh
sudo -u b2b sh -c 'umask 077; /opt/b2b/venv/bin/python3 /opt/b2b/app/tools/create_admin.py \
  --db /var/lib/b2b/data/app.db --email admin@你的邮箱 --name "管理员" --password-stdin'
```

**⑧ systemd（应用 + 每日备份；先语法校验再启用）**

```sh
sudo cp /opt/b2b/app/deploy/systemd/b2b-app.service /etc/systemd/system/
sudo cp /opt/b2b/app/deploy/systemd/b2b-backup.service /opt/b2b/app/deploy/systemd/b2b-backup.timer /etc/systemd/system/
sudo systemd-analyze verify /etc/systemd/system/b2b-app.service /etc/systemd/system/b2b-backup.service
sudo systemctl daemon-reload
sudo systemctl enable --now b2b-app.service
sudo systemctl enable --now b2b-backup.timer
```

**⑨ 反向代理与证书（四步：HTTP-only 引导 → certbot 签证 → 443 配置 → 续期复验）**

```sh
# a) HTTP-only 引导配置（此时不引用未生成的证书，业务页面还不提供）
sudo install -d -m 0755 /var/www/b2b-acme
sudo cp /opt/b2b/app/deploy/nginx-b2b-http-bootstrap.conf.example /etc/nginx/sites-available/b2b.conf
sudo ln -sfn ../sites-available/b2b.conf /etc/nginx/sites-enabled/b2b.conf
sudo rm -f /etc/nginx/sites-enabled/default        # 发行版默认站点必须移除（本配置自带 444 默认站点）
sudo nginx -t && sudo systemctl reload nginx

# b) 首次签证（webroot；不修改 nginx 配置，可审计）
sudo certbot certonly --webroot -w /var/www/b2b-acme -d pilot.alpsaga.tech \
  --agree-tos -m <你的运维邮箱>

# b2) 续期部署钩子（装一次即生效：systemd timer 续期成功后自动 reload nginx）
sudo install -d -m 0755 /etc/letsencrypt/renewal-hooks/deploy
sudo tee /etc/letsencrypt/renewal-hooks/deploy/reload-nginx.sh >/dev/null <<'EOF'
#!/bin/sh
systemctl reload nginx
EOF
sudo chmod 0755 /etc/letsencrypt/renewal-hooks/deploy/reload-nginx.sh

# c) 换成 443 最终配置（HTTPS 入口；/rfq/ 不记访问日志；默认站点 444）
sudo cp /opt/b2b/app/deploy/nginx-b2b.conf.example /etc/nginx/sites-available/b2b.conf
sudo ln -sfn ../sites-available/b2b.conf /etc/nginx/sites-enabled/b2b.conf
sudo nginx -t && sudo systemctl reload nginx

# d) 续期复验（须在 c) 之后执行；dry-run 走 staging、不消耗正式速率，--run-deploy-hooks 连部署钩子一起验）
#    前提：80 全站 301 后 CA 跟随跳转经 HTTPS 取校验文件——443 配置必须保留 ACME webroot location
#    （本模板已含，勿删）；2026-10-13 真机演练曾实际失败（404），当日用户已在真机应用修正并经本条
#    命令复验通过（用户回传，见状态记录）；任何重装/迁移后须重新执行本命令确认。
sudo certbot renew --dry-run --run-deploy-hooks
```

日志口径（两套配置一致）：访问日志用 `b2b_nocreds` 格式——只记 `$uri`（不含 query），也不记 Referer（来源页 URL 可能带 query）；买方询价链接 `/rfq/<token>` 的 token 属凭据，该路径 `access_log off`；错误日志收敛到 `crit`（避免请求行/凭据路径随错误信息落盘），应用级诊断看 `journalctl -u b2b-app`。

**⑩ 首装验收（服务器上逐条跑；本机假件测试不能代替此步）**

```sh
# 1) 服务与定时器
systemctl is-active b2b-app b2b-backup.timer          # 两个都应为 active
# 2) 本机回环
curl -sf -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8000/login      # 200
# 3) 域名 HTTPS（浏览器打开 https://pilot.alpsaga.tech/login 用 ⑦ 账号登录，确认无演示账号痕迹）
curl -sS -o /dev/null -w '%{http_code}\n' https://pilot.alpsaga.tech/login # 200
# 4) 默认站点不暴露程序（期望连接被直接关闭或非 200；curl 报错/000 即符合预期）
curl -sS -o /dev/null -w '%{http_code}\n' --resolve nope.invalid:443:47.243.192.209 https://nope.invalid/login
curl -sS -o /dev/null -w '%{http_code}\n' --resolve nope.invalid:80:47.243.192.209 http://nope.invalid/
# 5) 日志口径（应无输出/为 0）
sudo grep -E '/rfq/|\?' /var/log/nginx/b2b-access.log || true
# 6) 备份真跑一次（停写窗口 + root 私有产物 + 运维配置进包）
sudo systemctl start b2b-backup.service && systemctl status b2b-backup.service
sudo ls -ld /var/backups/b2b/20*
sudo ls -l /var/backups/b2b/20*/ops-config/b2b.env     # 目录 0700、文件 0600
journalctl -u b2b-backup.service -n 50                 # 看“停止 → 快照 → 恢复”流程日志
# 7) 隔离恢复检查（不会覆盖任何现有数据）
sudo /opt/b2b/venv/bin/python3 /opt/b2b/app/tools/backup_restore.py restore-check \
  --bundle /var/backups/b2b/<时间戳> --out /var/lib/b2b/restore-check-<时间戳>
# 8) 续期演练（dry-run + 部署钩子；2026-10-13 曾失败〔404〕，修正后当日用户真机复验通过；重装后重跑本项）
sudo certbot renew --dry-run --run-deploy-hooks
```

通过标准：以上全部符合预期；`curl` 对默认站点不是 200/301/404 页面；`/rfq/` 不出现于访问日志；备份产物权限为 root 私有；续期演练成功（模拟续期通过、部署钩子已执行、退出码 0）。

## 4. 上传目录持久配置（版本切换不丢原件）

- 应用默认把原件放在**代码目录**的 `data/uploads`（历史行为）。部署必须设置环境变量 **`B2B_UPLOAD_DIR=/var/lib/b2b/data/uploads`**（见 `b2b.env.example`），此后新写原件落在持久目录并记录绝对路径。
- 迁移旧部署：把旧代码目录的 `data/uploads`（含 `phase2/` 子目录）**整体复制**到 `B2B_UPLOAD_DIR` 指向的目录即可；旧库中的相对路径（`data/uploads/…`）在配置了该变量后按持久目录解析，无需改库。
- 产品来源、知识导入、批次原件共用该目录；知识导入另有测试/隔离专用变量 `B2B_KB_STORAGE_ROOT`（优先级更高，正式部署不用）。

## 5. 备份、恢复与停写边界

**一致性边界（不伪装原子快照）**：`tools/backup_restore.py` 的数据库快照是事务一致的；原件与私有目录在快照之后顺序复制、无全局锁。并发上传/处理中可能出现“库中行与文件不完全匹配”。要得到互相匹配的一整套（库 + 全部原件 + 模型配置 + 桥登录状态 + 运维配置），必须**先停止写入**——`deploy/backup_snapshot.sh` 即该停写流程：

- 以 root 运行（systemd `b2b-backup.service`，读取 `/etc/b2b/b2b.env`）；**产物 root 私有**（目录 0700/文件 0600）——包内含密钥材料，不进 b2b 用户可读范围。
- 先保存原活跃状态：只停止原本在运行的 `b2b-app`、本库登记账号的受管桥容器（`b2b-account-<24 位 ID>`）与注册表登记的企业桥容器（`b2b-enterprise-<租户哈希>`）；备份后只把原本活跃的恢复回去；**任何失败都经 trap 恢复原状**，不会出现“应用被停掉而备份没做成”。
- 企业桥停写预检（fail-closed）：脚本调用 `provision_enterprise_bridge.py --check-registered` 核对登记容器的标签与 sessions/media 卷**属于本库数据目录**、并区分运行状态。由其他数据根（含恢复副本）提供的同名容器、被替换的容器、docker inspect 失败等“无法确认”情形一律**失败退出（exit 3）**，不宣称一致性成功，也绝不误停另一数据根（生产）的桥；预检在停应用之前完成，失败不打断既有服务。
- 运维配置 `/etc/b2b/b2b.env` 随包 `ops-config/`（文件缺失时告警继续，不阻断业务数据备份）。
- 自动备份：`b2b-backup.timer` 每日 03:30（错过则开机补跑）；保留天数由 `B2B_BACKUP_KEEP_DAYS`（默认 14）控制，只清理本工具创建的 `YYYYmmdd-HHMMSS` 目录。
- 手工执行（root；同样先停应用）：

```sh
sudo systemctl stop b2b-app
sudo /opt/b2b/venv/bin/python3 /opt/b2b/app/tools/backup_restore.py backup \
  --db /var/lib/b2b/data/app.db --uploads /var/lib/b2b/data/uploads \
  --out /var/backups/b2b/manual-$(date +%Y%m%d-%H%M%S) --ops-config /etc/b2b/b2b.env
sudo systemctl start b2b-app
```

- 恢复到空目录核对（不会覆盖任何现有数据；恢复产物同样 root 私有）：

```sh
sudo /opt/b2b/venv/bin/python3 /opt/b2b/app/tools/backup_restore.py restore-check \
  --bundle /var/backups/b2b/<时间戳> --out /var/lib/b2b/restore-check-<时间戳>
```

恢复会把归档里的绝对原件路径重写到恢复目录、把 `model-secrets/` 映射为恢复库同名目录、把 `ops-config/` 按原相对路径保留，并校验全部原件哈希与数据库完整性；路径逃逸（含 `..`）、符号链接、缺件、篡改一律拒绝。把恢复目录投入使用前先跑第 6 节的演练脚本或至少人工登录核对；如需把恢复目录交给 b2b 用户使用，先 `sudo chown -R b2b:b2b <目录>`（本工具默认保持 root 私有）。

- **异地/独立备份**：本机备份不能防整机丢失；跨机备份必须先加密再上传（备份包含密钥，**禁止明文上传**）。自动链（OSS 私有桶 + age 客户端加密）见第 11 节；R2 方案保留为备选（[offsite-backup-r2-plan.md](offsite-backup-r2-plan.md)，未执行）。

## 6. 部署前后必做的恢复演练（可复跑）

```sh
# 本地（builder/开发机）端到端演练：新安装 → HTTP 业务 → 重启保存 → 备份 → 丢件 → 恢复 → 业务继续（全离线、临时数据）
python3 /opt/b2b/app/tools/pilot_checks/check_first_launch_t4.py
# 判别性测试（备份覆盖/安全拒绝/恢复映射/上传目录配置/原件路径逃逸）
python3 -m unittest -v test_first_launch_t4 test_first_launch_t4_deploy
```

通过标准：演练脚本 0 FAIL 且退出码 0；真实数据进入部署环境前，再按第 5 节对本部署的真实库做一次 `backup` + `restore-check` 并记录耗时与结果（在服务器上做，属第 3 节 ⑩ 的一部分）。

## 7. 版本切换、回滚与迁移前备份

1. **升级前必须先做一次停写窗口备份**（第 5 节）——这就是“迁移前备份”。
2. 新版本：构建机 `python3 deploy/make_release.py --label <新 label>` → 传包 → 校验哈希 → `sudo tar -xzf … -C /opt/b2b/releases` → `systemctl stop b2b-app` → `ln -sfn releases/b2b-release-<新 label> /opt/b2b/app` → `systemctl start b2b-app` → 按第 3 节 ⑩ 自检。
3. **回滚**：停止应用 → 把升级前备份 `restore-check` 到干净目录并替换 `data/`（或按备份包重建）→ 切回旧版本目录 → 启动核对。**不要**把旧版本代码直接指向已由新版本迁移过的库：应用的建表/加列是向前写的，仓库没有反向迁移脚本，也不承诺旧版本可读新 schema；回滚以“恢复升级前备份”为准。
4. 升级前一并核对迁移影响：`python3 tools/pilot_checks/check_db_migrations.py --db <库的副本>`（只操作副本，检查关键列/索引与重复数据风险）。

## 8. 正式演示门控

- 部署单元只运行 `app.py`（`make_server(demo_tools=False)` 默认）——演示按钮与演示写入口一律 403，登录页不出现演示账号文案。
- **不要在服务器上运行 `tools/demo/start_demo.py`**；演示环境与正式库分离。正式库只经 `tools/create_admin.py` 创建首位管理员，其余成员在成员管理中创建。
- 沿用 [正式化清单](../docs/正式化清单.md)：未处理项不得对外发布；本部署形态不引入演示数据。

## 9. 发布包工具（make_release.py）

- 从**当前源码**按白名单导出：应用代码与全部本地模块、静态资源（`reference-client/`、`static/`、`assets/`）、部署模板（`deploy/` 全套）、运维工具（`tools/backup_restore.py`、`tools/create_admin.py`、`tools/pilot_checks/`）、知识解析依赖清单（`requirements-knowledge.txt`）。
- 生成逐文件 sha256 清单 `RELEASE-MANIFEST.json` + 可校验的 `RELEASE-MANIFEST.sha256`；清单同时记录来源 HEAD 与“是否含未提交修改”（如实，不掩饰）。
- **绝不包含**：业务 `data/`、密钥、`.git`、`docs/`（含 evidence 与历史资料原件）、`design/`、演示素材（`fixtures/`、`B2B_Pump_Demo_Research/`）、测试文件。
- 新增应用模块或静态资源时，必须同步补进 `deploy/make_release.py` 的 `INCLUDE_PATTERNS`（缺文件会在安装导入或页面加载时暴露；发布包测试有导入校验）。
- `python3 deploy/make_release.py --patch --label <标签>` 只构建**修补包**（异地备份脚本与 systemd 单元、含 ACME webroot 修正的两个 nginx 模板、本 README），用于已部署服务器的原地小更新；模板仅供 diff 参考，**严禁**覆盖线上运行配置（第 11 节）。

## 10. 已知限制与未闭项（如实）

- **未在真实 Linux 执行过**：本目录的 systemd/nginx/脚本在本机（macOS）只做过语法与逻辑校验（含外部命令假件的脚本测试）+ 隔离演练；首装必须按第 3 节 ⑩ 在服务器上跑一遍并记录结果（`systemd-analyze verify`、`nginx -t`、`b2b-backup.service` 真跑、HTTPS 与默认站点检查）。
- **WhatsApp 个人版桥的接入形态**：应用以非 root `b2b` 用户运行，无 Docker socket 访问权限（也**不**默认加 docker 组、不放开 socket）——桥容器一律由运营以 root 预部署（pilot-t4：每企业一个 `b2b-enterprise-<租户哈希>` 容器，见第 12 节）；应用只经 HTTP 与私有注册表连接。`channel_runtime.py` 的单账号容器模式在服务器上因无 Docker 权限会失败（保留兼容，不用于本部署）。**第 12 节流程在本机只做过假件逻辑测试，未在真实 Linux/Docker/WAHA 上执行；须经独立审查与真机验收后才可对外启用。**
- **代码内诊断日志写入受加固限制**：`app.py` 把 `data/whatsapp_personal_events.log`、`data/whatsapp_personal_start_audit.log` 写在**代码目录**；单元加固（`ProtectSystem=strict` + `ReadWritePaths=/var/lib/b2b`）下代码目录只读，这些写入静默失败（调用处 try/except 吞掉）。不影响业务数据、审计与备份（都在 `/var/lib/b2b` 与库表内）；是否需要 `B2B_LOG_DIR` 之类改造留 T5 决定。
- 单实例 + 本地 SQLite：短时维护可接受（试点前提）；高可用/多实例/外部对象存储属后续架构决策（ADR-07/08），不因本目录新增。
- 桥容器（WhatsApp 个人版）需要 Docker 与足够内存（官方建议 ≥2C4G，容量按实际实测）；其登录会话恢复后如状态异常，按 `/admin/channels` 页面重新扫码（属运营动作，T5 专项验收）。
- 安全组 22/80/443 与 DNS 解析为用户提供信息，未独立复验（Codex 本机 SSH 超时）；以 Workbench 实际操作结果为准。
- 异地自动备份链未在真实 Linux 执行过（本机假件测试只验逻辑）：`systemd-analyze verify`、`ossutil`/`age` 真跑、定时器首次触发与云端对象核对，均须按第 11 节在服务器上验收；未验收前不得视为闭环。云端删除与生命周期未启用（待用户确认）。

## 11. 异地自动备份链：OSS + age（2026-10-13 追加）

**状态**：脚本与单元已实现，并在本机以假件命令（fake `systemctl`/`ossutil`/`age`，真实 `tar`/`sha256sum`/停写快照脚本/备份工具）做过判别性测试：快照失败不上传、age 失败清理不完整密文、上传失败保留本地快照与密文且应用不在停机态、云端同名对象拒绝覆盖、云下载 sha256 不一致保留证据、密钥材料绝不明文上传、与本机备份不并发。**真实 Linux/systemd/ossutil/age 与定时执行尚未验收**（清单见本节末；2026-10-13 真机首跑：预检曾因脚本使用 2.4.0 不存在的 `-s` 而 fail-closed 退出 14〔未上传、应用已恢复〕，本地已改用默认表格输出并重写解析，待真机复验——见 `docs/开发状态与变更记录.md`「T4 异地链预检真实阻断修复」）。

已就绪的外部条件（用户配置并回填，2026-10-13）：私有桶 `alpsaga-pilot-backup-v1`（新加坡/标准/LRS/阻止公共访问；专用 RAM 仅 ListObjects 桶与 PutObject/GetObject 桶/*，**无删除与管理权限**）；`ossutil` 2.4.0（`/usr/local/bin/ossutil`，配置 `/etc/b2b/ossutil.conf`，root 0600）；加密公钥 `/etc/b2b/backup-age.pub`（`age1c3y8afewj46rtg2g2jugghdkuw4skuwxq5s8wyeun6leg5cxwsvstsyyar`；**私钥只在 Mac 与密码管理器，禁止放服务器**）；`age` 已安装。

**链行为**（`deploy/offsite_backup_oss.sh`，root，由 `b2b-offsite-backup.service` 调用）：

1. 前置检查（公钥格式/工具/ossutil 配置/与 `b2b-backup.service` 不并发——正在跑则等待，超时或禁等直接拒绝）；任何失败都在**停写之前**结束（退出码 2）。
2. 调用现有停写快照 `deploy/backup_snapshot.sh`，以 `B2B_BACKUP_OUT` 把**本次**快照钉死为 `<BACKUP_ROOT>/<时间戳>`（不按时间猜、不会误选旧包；同名先拒绝）——快照失败即终止，不上传（10）。
3. 应用恢复门：确认应用已恢复（快照脚本 trap 自恢复 + 本脚本复查兜底）；未恢复则拒绝加密与上传（11）。
4. `tar -C <快照> -czf - | age -R /etc/b2b/backup-age.pub -o <密文>`：管道直传，**不落明文 tar**；失败清理不完整密文（12）。
5. 本地 sha256 校验文件（相对文件名，下载到任意目录可复核；13）→ 云端预检：`ossutil … ls oss://…/backups/<时间戳>/`（默认表格输出；2.4.0 无 `-s` 短标志）必须为空，否则拒绝覆盖（14；无法确认同样 fail-closed）。
6. 上传仅两件：`<时间戳>.export.tar.gz.age` 与 `.sha256`（`--ignore-existing`：任何情况下不覆盖已存在对象）；失败非零（15），本地快照/密文/校验全部保留。
7. 云下载到 `<BACKUP_ROOT>/oss-verify/<时间戳>/`（root 0700，隔离）复核与本地 sha256 一致；失败保留该目录作证据（16），成功清理下载副本。
8. 保留（仅本地、仅本工具命名）：快照目录与密文/校验文件、复核目录按 `B2B_BACKUP_KEEP_DAYS`（默认 14 天）；**云端不做任何删除、不配置生命周期**（待用户确认）。

退出码：2 前置/拒绝；10 快照失败；11 应用未恢复/产物缺失；12 加密失败；13 本地校验失败；14 云端预检不通过；15 上传失败；16 云下载校验失败。日志：`journalctl -u b2b-offsite-backup.service`。

**可配项**（可选，写入 `/etc/b2b/b2b.env`；默认值即当前部署形态）：`B2B_OSS_BUCKET`（alpsaga-pilot-backup-v1）、`B2B_OSS_PREFIX`（backups）、`B2B_OSSUTIL`（/usr/local/bin/ossutil）、`B2B_OSSUTIL_CONFIG`（/etc/b2b/ossutil.conf）、`B2B_AGE_RECIPIENT_FILE`（/etc/b2b/backup-age.pub）、`B2B_OSS_VERIFY_ROOT`（<BACKUP_ROOT>/oss-verify）、`B2B_OFFSITE_WAIT_LOCAL_S`（600）、`B2B_BACKUP_KEEP_DAYS`（14）。⚠️ **不要把 `B2B_BACKUP_OUT` 写进 `b2b.env`**——该变量仅由异地链在内部对本次快照调用注入；全局设置会让每日本机备份固定到同一路径而在第二次运行时被“同名拒绝”。

**修补包构建（构建机=本仓库，一条命令）**：

```sh
python3 deploy/make_release.py --patch --label offsite-oss-<日期>
# 产出：dist/b2b-patch-<label>.tar.gz 与 dist/b2b-patch-<label>.tar.gz.sha256
# 含：offsite_backup_oss.sh、backup_snapshot.sh、b2b-offsite-backup.service/.timer、
#     两个 nginx 模板（含 ACME webroot 修正）、本 README；不含任何密钥与业务数据。
```

**服务器安装（Workbench 逐条；仅本任务相关）**：

```sh
# a) 上传到 /tmp 后：先核对包哈希，再解包核对包内逐文件哈希
cd /tmp && sha256sum -c b2b-patch-<label>.tar.gz.sha256        # 必须 OK
tar -xzf b2b-patch-<label>.tar.gz
cd b2b-patch-<label> && sha256sum -c RELEASE-MANIFEST.sha256   # 必须全 OK

# b) 脚本与单元就位（装进“当前版本”目录内；单元引用 /opt/b2b/app/deploy/…）
sudo install -o root -g root -m 0755 deploy/offsite_backup_oss.sh /opt/b2b/app/deploy/offsite_backup_oss.sh
sudo install -o root -g root -m 0755 deploy/backup_snapshot.sh    /opt/b2b/app/deploy/backup_snapshot.sh
sudo install -o root -g root -m 0644 deploy/systemd/b2b-offsite-backup.service deploy/systemd/b2b-offsite-backup.timer /etc/systemd/system/
sudo systemd-analyze verify /etc/systemd/system/b2b-offsite-backup.service
sudo systemctl daemon-reload

# c) nginx 模板：仅供 diff 参考——线上 /etc/nginx/sites-available/b2b.conf 已人工应用 ACME webroot
#    修正，禁止用本包模板直接覆盖。核对（可选，只读）：
diff -u deploy/nginx-b2b.conf.example /etc/nginx/sites-available/b2b.conf || true

# d) 首次手工运行（先跑通，再启用定时器）
sudo systemctl start b2b-offsite-backup.service
journalctl -u b2b-offsite-backup.service -n 80          # 应见 ①…⑧ 步骤与“异地备份完成”
sudo ls -ld /var/backups/b2b/20* /var/backups/b2b/20*.export.tar.gz.age*
sudo /usr/local/bin/ossutil --config-file /etc/b2b/ossutil.conf ls oss://alpsaga-pilot-backup-v1/backups/

# e) 通过后启用定时器（每日 04:30；与本机 03:30 备份错开）
sudo systemctl enable --now b2b-offsite-backup.timer
systemctl list-timers b2b-offsite-backup.timer
```

**真机验收清单（Codex 复跑）**：① `bash -n` 脚本 + `systemd-analyze verify` 单元（上 b）；② 首次手工运行退出码 0，journal 出现 ①–⑧ 与 sha256 一致行，产物为 root 私有（0700/0600）；③ 云侧 `backups/<时间戳>/` 恰好两个对象，服务器另目录下载 `sha256sum -c` 通过；④ 负例抽查：临时改名 ossutil 配置→预检拒绝（2）、断网→云端预检失败拒绝（14）；⑤ `list-timers` 显示下次触发时间。

**边界**：不启用任何云端删除；生命周期/保留策略待用户确认后另行处理；Mac 端解密（`age -d` 私钥）+ `restore-check` 仍是闭环演练的另一半；本链不代替第 5/6 节的恢复演练。

## 12. 企业专属 WhatsApp 桥（运营预部署，pilot-t4）

**状态**：代码与部署工具已实现，本机以假件做过判别性测试（注册表/密钥隔离/停写归属/恢复重建）；**未在真实 Linux/Docker/WAHA 上执行过、未部署**。可承载账号数按实测另定（不设内存硬配额、不承诺容量）。

- 每企业一个固定容器 `b2b-enterprise-<租户哈希 24hex>`（NOWEB 引擎），只监听 `127.0.0.1`：容器创建时随机选取一个空闲宿主端口并固定发布（`-p 127.0.0.1:<端口>:3000`），端口在容器生命周期内固定、重启不再重分配；Swagger（`WHATSAPP_SWAGGER_ENABLED=false`）与 Dashboard（`WAHA_DASHBOARD_ENABLED=false`）关闭。
- 运行目录：`/var/lib/b2b/data/connection-runtime/enterprise/<租户哈希>/{sessions,media,bridge.env}`；容器内挂载 `/app/.sessions` 与 `/app/.media`；`WHATSAPP_FILES_FOLDER=/app/.media`、`WHATSAPP_FILES_LIFETIME=0` 停止 WAHA 的媒体自动删除（**企业媒体保留时长规则仍待 T6 确认**；这里只停止自动删，不代表承诺长期保留）。
- 部署（root；`<租户ID>` 用平台内真实租户 ID 替换；企业须已存在，否则拒绝创建）：

```sh
sudo /opt/b2b/venv/bin/python3 /opt/b2b/app/deploy/provision_enterprise_bridge.py \
  --tenant <租户ID> --db /var/lib/b2b/data/app.db
# 可选：--image 传不可变 digest（默认 devlikeapro/waha:noweb-2026.9.2）
```

- 工具生成逐企业独立密钥（只进 `bridge.env`，0600，环境变量方式注入容器、不进命令行）并原子更新私有注册表 `enterprise-bridges.json`（root:b2b 0640；先写临时文件再替换）；**不自动创建/启动任何 WhatsApp 会话**（扫码由企业在渠道页完成）。幂等：重复运行不重建容器；核对容器标签/卷/镜像不符时拒绝复用。
- 应用侧：在 `/etc/b2b/b2b.env` 配置 `WHATSAPP_PERSONAL_BRIDGES_FILE=/var/lib/b2b/data/connection-runtime/enterprise-bridges.json` 后，发送与扫码只按“账号配置里的企业引用（tenant ID）”解析该注册表；缺失/无效映射一律拒绝、不回退平台全局桥。密钥不写入数据库配置、页面或日志。
- 备份/恢复：备份脚本自动停写注册表登记且**归属本数据目录**的企业桥（停不下来或无法核对即失败退出）；`connection-runtime/`（注册表、env、sessions、media）随备份包整体恢复。恢复后按恢复的注册表重建容器（重建时重新随机选取固定端口，注册表会被更新）：

```sh
sudo /opt/b2b/venv/bin/python3 /opt/b2b/app/deploy/provision_enterprise_bridge.py \
  --tenant <租户ID> --db <恢复目录>/app.db
```

- **安全演练边界（必须遵守）**：禁止同一 WhatsApp 账号同时在源桥与恢复副本上登录。在恢复环境验证时：先停原桥容器（`sudo docker stop b2b-enterprise-<租户哈希>`），在同一主机演练时还须将原容器改为明确的暂存名称（保留原卷，演练后还原），才能用原名称创建挂载恢复目录的新容器；仅停止而保留同名原容器会因挂载不符被拒绝。创建/复用时 `--image` 必须沿用原桥的镜像引用或 digest。再验证恢复桥；或只做只读核对（`--check-registered` / `restore-check`）而不启动任何会话——本工具与备份脚本都不自动创建/启动会话。在恢复副本上直接跑 `backup_snapshot.sh` 会因登记容器“卷不属于本数据目录”而被拒绝停写（fail-closed），不会误停生产桥。
- 恢复后的会话启动（2026-10-14实机核对）：活动会话列表可能为空；运营核对须用 `GET /api/sessions?all=true` 查找已保存的 `STOPPED` 会话，再启动这些已有会话。也可在已指向恢复库、上传根和注册表的产品界面点击“开始连接”。本次两个账号均直接恢复原身份，无需重新扫码；不要用删除会话或退出登录替代恢复。鉴权只从恢复后的私有注册表读取，不在命令参数或日志中回显。
- 真机验收（独立执行；未通过前不得视为已上线）：`--check-registered` 与 docker 实际一致；企业扫码后收发链路可用；两企业互不串桥；备份停写与恢复重建各跑一轮；网络暴露面复查（仅 `127.0.0.1`）。

