# 独立异地备份方案：Cloudflare R2 私有桶 + age 客户端加密（T4 准备；未执行）

日期：2026-10-13。状态：**方案就绪，未创建任何账号/凭据、未购买任何资源、未上传任何数据。** 执行方式：服务器首装完成后，由 Codex 带用户逐项执行（以本文件为准）；本仓库不预置任何云凭据。

> **状态更新（2026-10-13）**：实际执行采用**阿里云 OSS**（新加坡私有桶 `alpsaga-pilot-backup-v1` + ossutil + age 公钥加密），自动链已按此实现：见 [README.md](README.md) 第 11 节与 `docs/开发状态与变更记录.md`。**本文件保留为 R2 备选方案**（未执行），其中“绝不明文上传、凭据只在服务器 root 私有文件与用户密码管理器”等原则仍适用。

## 0. 原则（硬线）

- **桶必须私有**：不开公共访问、不绑自定义公开域名；凭据最小权限（只允许对该桶的 Object 读写）。
- **绝不明文上传**：备份包内含密钥材料（`/etc/b2b/b2b.env`、企业模型私有配置），任何异地副本必须先做客户端加密（age），云上只存密文。
- **凭据只在服务器 root 私有文件与用户密码管理器**：不进 Git、不进文档、不进聊天、不回显；泄露即轮换。
- 本机 `/var/backups/b2b` 仍是主恢复路径；异地副本只防“整机丢失/机房事故”。**下载 + 解密 + restore-check 演练通过后才算闭环。**

## 1. 选型与费用线索（执行时以官方页面为准）

- 目标形态：S3 兼容私有桶、客户端加密、保留期可设（生命周期规则）。
- Cloudflare R2 标准存储有免费额度（约 10GB-月量级），保留期与包大小按真实数据实测后定；阿里云 OSS 为备选（如用户阿里云侧结算更顺）。
- 备份包大小 ≈ 业务库 + 全部原件 + 私有配置；首装后实测一次（`du -sh /var/backups/b2b/<时间戳>`）。

## 2. 需要用户在 Cloudflare 侧完成（执行清单；Codex 陪同，逐项核对）

1. Cloudflare 账号 → R2 → 创建桶，命名 `b2b-backup`；**不开公共访问**。
2. 创建 R2 API 令牌：作用域仅该桶、权限 Object Read & Write；记录 Access Key ID / Secret Access Key 与 S3 端点 `https://<accountid>.r2.cloudflarestorage.com`（只存服务器与密码管理器）。
3. 桶设置：可加生命周期规则“对象 N 天后删除”（与保留期一致，建议先 30 天）；不需要版本控制/公共域名。

## 3. 服务器侧配置（root；待第 2 节完成后执行）

```sh
sudo apt-get install -y rclone age        # 或按 age 官方文档安装
# rclone 配置（root 0600）——示例，值在上传时由用户填入，勿写进仓库：
sudo install -m 0600 /dev/null /etc/b2b/rclone.conf
sudo tee /etc/b2b/rclone.conf >/dev/null <<'EOF'
[b2b-r2]
type = s3
provider = Cloudflare
access_key_id = <Access Key ID>
secret_access_key = <Secret Access Key>
endpoint = https://<accountid>.r2.cloudflarestorage.com
acl = private
EOF
# age 密钥对（恢复的前提；私钥另存用户密码管理器，服务器丢了还能解密云端副本）
sudo age-keygen -o /etc/b2b/age.key       # 0600 由 age 默认保证；输出的公钥记入部署记录
sudo age-keygen -y /etc/b2b/age.key | sudo tee /etc/b2b/age.pub   # 0600
```

## 4. 加密与上传口径（每日链：本机备份成功后执行）

```sh
# 打包 + 客户端加密（密文按天命名；密文可另算 sha256 供传输核对）
B=/var/backups/b2b/$(date +%Y%m%d-%H%M%S)
sudo -s <<'SH'
tar -C "$B" -czf - . | age -r "$(cat /etc/b2b/age.pub)" -o "$B.export.tar.gz.age"
sha256sum "$B.export.tar.gz.age" | tee "$B.export.tar.gz.age.sha256"
rclone copy "$B.export.tar.gz.age" b2b-r2:b2b-backup/latest/
rclone copy "$B.export.tar.gz.age.sha256" b2b-r2:b2b-backup/latest/
SH
```

上传器脚本化（systemd oneshot，排在本机 b2b-backup 之后）留待执行时落最小实现；**本轮不写、不配置真实令牌**。

## 5. 下载与恢复演练（每季度或换机时；验收闭环步骤）

```sh
rclone copy b2b-r2:b2b-backup/latest/<stamp>.tar.gz.age /tmp/
sha256sum -c /tmp/<stamp>.tar.gz.age.sha256
mkdir /tmp/restore && age -d -i /etc/b2b/age.key -o /tmp/restore/bundle.tar.gz /tmp/<stamp>.tar.gz.age
mkdir /tmp/restore/bundle && tar -C /tmp/restore/bundle -xzf /tmp/restore/bundle.tar.gz
sudo /opt/b2b/venv/bin/python3 /opt/b2b/app/tools/backup_restore.py restore-check \
  --bundle /tmp/restore/bundle --out /var/lib/b2b/restore-check-<stamp>
# 核对 ops-config/、模型配置、原件哈希全部通过，再考虑投入使用
```

## 6. 保留与命名

- 本机：14 天（`B2B_BACKUP_KEEP_DAYS`），只清理 `YYYYmmdd-HHMMSS` 目录。
- 异地：建议 30 天起步（成本实测后调）；对象名 `<stamp>.tar.gz.age`。
- 密钥轮换：R2 令牌泄露即删除重建；age 密钥轮换需先上传新公钥加密的副本，再退役旧私钥。

## 7. 明确不做（本轮与后续边界）

- 不由 Hermes 创建云账号、生成或保存真实凭据、购买任何资源、上传任何数据。
- 不把 `/etc/b2b/b2b.env` 或任何密钥明文放进云端、Git、文档或聊天。
- 异地上传器实现与真实令牌配置属部署执行（Codex 带用户逐项做）；本文件只定义口径与步骤。
