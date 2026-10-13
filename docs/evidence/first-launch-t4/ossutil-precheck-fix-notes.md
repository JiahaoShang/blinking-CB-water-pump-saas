# T4 异地链预检修复（`-s` → 默认表格输出 + 重写解析）：证据与操作说明

日期：2026-10-13（本地 Hermes；承接真机 19:08 首跑失败与 19:13 用户手工修正记录）。
范围：只修 `deploy/offsite_backup_oss.sh` 的 `ls -s`（2.4.0 不存在）与伴随预检解析；测试同步；`deploy/README.md` 两处命令同步。
约束：不用子代理、不提交/推送、不读业务 data/真实密钥与服务、不连真实云；基线 HEAD `7a79083`（工作树为准）。

## 结论

- 根因：官方 ossutil 2.4.0 `ls` 无 `-s` 短标志（实核 `ls --help`；短输出为长标志 `--short-format`）。原脚本以 `ls -s` 预检 → 报 `unknown shorthand flag: 's' in -s`，fail-closed 退出 14（未上传、应用恢复 active）。
- 修复：预检改用默认表格输出 `ls oss://…/backups/<时间戳>/`；解析双重判定（均 fail-closed）：`Object Number is: N` 计数行（容忍 `:` 后空格差异，兼容 `is:0` 紧凑排版）+ 含 `oss://` 对象行；计数取不到或 ls 非零都退出 14；同名冲突照旧拒绝。
- 测试：先红（Ran 29 → 11 failures）后绿（Ran 29 OK）；聚焦回归（offsite + deploy）Ran 52 OK。
- 交付：`dist/b2b-patch-offsite-precheck-20261013.tar.gz`（单脚本 + 清单；不覆盖其他已验配置）。

## 本目录证据索引（docs/evidence/first-launch-t4/）

| 文件 | 内容 |
| --- | --- |
| `ossutil-240-ls-evidence/` | 官方 2.4.0 二进制实核：下载与 sha256 核对、`ls --help`/`version`/`-s` 复现、空/非空/失败原始输出字节、mock 端点与复跑脚本 |
| `ossutil-precheck-fix-red.log` | 先失败：Ran 29 → 11 failures（假件按真二进制口径拒绝 `-s`，脚本 fail-closed 14） |
| `ossutil-precheck-fix-green.log` | 修复后：Ran 29 OK |
| `ossutil-precheck-fix-real-outputs.log` | 用真输出驱动修复后⑥预检块（eval 原文）：放行 / 拒绝 14 / fail-closed 14 |
| `ossutil-real-script-e2e.log` | 真脚本 + 真 ossutil + 真 age 端到端（mock 云）：stale→14、fail-list→14、success→0 且云端与本地字节一致 |
| `ossutil-precheck-fix-regression.log` | 聚焦回归：offsite + deploy，Ran 52 OK |
| `build-offsite-precheck-patch.py` | 单脚本修补包构建脚本（与 make_release.py 清单约定一致） |

## 修补包与哈希

- 包：`dist/b2b-patch-offsite-precheck-20261013.tar.gz`
  sha256 = `eeb166f53b223302ea6057cc88d11d091e7cfcd3dbab75b0542630ddfabbf318`（包外 `.sha256` 同值）
- 包内 `deploy/offsite_backup_oss.sh` sha256 = `6fa90e2b25758bc2147b428b138ab1ccbc312f792cdcad4270b07f55c5c0acc0`（14566 字节）
- 包内另有 `RELEASE-MANIFEST.json` / `RELEASE-MANIFEST.sha256`；来源 HEAD `7a79083`、含未提交修改（以工作树为准）。
- 注：`make_release.py --patch` 的 7 文件修补包流程未改动；本单脚本包是给本次修复的专门产物。

## 服务器应用与复验（待办）

```sh
# 1) 上传到 /tmp 后双重校验
cd /tmp && sha256sum -c b2b-patch-offsite-precheck-20261013.tar.gz.sha256
tar -xzf b2b-patch-offsite-precheck-20261013.tar.gz
cd b2b-patch-offsite-precheck-20261013 && sha256sum -c RELEASE-MANIFEST.sha256

# 2) 只替换这一个脚本（不动 nginx/systemd/其他脚本）
sudo install -o root -g root -m 0755 deploy/offsite_backup_oss.sh /opt/b2b/app/deploy/offsite_backup_oss.sh

# 3) 手工触发并按 deploy/README.md §11 d 步核对
sudo systemctl start b2b-offsite-backup.service
journalctl -u b2b-offsite-backup.service -n 80      # 应见 ①…⑧ 与“异地备份完成”
sudo /usr/local/bin/ossutil --config-file /etc/b2b/ossutil.conf ls oss://alpsaga-pilot-backup-v1/backups/
#    期望：backups/<新时间戳>/ 恰 2 个对象

# 4) 复验通过后再启用定时器（如未启用）
sudo systemctl enable --now b2b-offsite-backup.timer
```

复验要点：新链 run 退出码 0；云侧恰两对象；负例抽查（断网 → 预检失败 14；目标前缀已存在对象 → 拒绝 14 且不 cp）。

## 本机复跑（可选）

- 单测：`python3 -m unittest -v test_first_launch_t4_offsite`
- 真输出判定：`bash docs/evidence/first-launch-t4/ossutil-240-ls-evidence/case6-harness.sh`
- 端到端/格式取证（需 scratch 环境，脚本内为绝对路径）：`ossutil-240-ls-evidence/e2e-run.sh success|stale|fail-list`

## 备注

- macOS 自带 bash 3.2 在 UTF-8 locale（如 C.UTF-8）下会误解析“`$var` 紧贴中文”的表达式；单测子进程为净化环境（C locale）不受影响，服务器 Linux bash 5 不受影响。在本机直接手跑部署脚本时建议前缀 `LC_ALL=C`。
- `--short-format` 是 2.4.0 真实支持的短输出替代形态（对照输出已留档）；本次按任务给定的“默认表格输出”形态实现；即使将来切换该形态，解析逻辑同样成立（行含 `oss://` + 同一计数行）。
