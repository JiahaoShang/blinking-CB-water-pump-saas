# T4 修复轮复核（verify-round）— 制作方复跑记录

- 日期：2026-10-13（本地，UTC+8）
- 对象：对照 `/tmp/b2b-t4-extra-review.txt`（已归档 `docs/evidence/first-launch-t4/independent/path-escape-review.txt`）复核修复轮 `fix-round-*` 的全部修复项。
- 状态：**制作方复核通过；非独立验收——不标 T4 通过、不标独立验收通过**。服务器端（Linux systemd/nginx/HTTPS/发布包安装）验收未执行（见 `deploy/README.md` 第 3 节 ⑩，待 Workbench）。
- 隔离：全部使用系统临时目录/临时库；仓库 `data/` 复核前后未变（uploads 5147 文件、phase2 0）；未调用真实模型、未连外部账号、未读取密钥；未提交/未推送。

## 一、修复内容核对（复核轮未改代码与测试，关键文件哈希与 fix-round 记录一致）

1. `tools/backup_restore.py::resolve_stored_file`：拒绝 `..` 段（相对/绝对两类分支各自检查）＋ `relative_to` 后 `rel` 不得含 `..`/绝对 ＋ 最终 `resolve()` 仍须在 uploads 根内；上传根为符号链接拒绝。
2. 恢复端 `restore_and_verify`/`_reject_symlinks_in_tree`：备份包根为符号链接拒绝、包内符号链接拒绝、清单路径不得绝对/含 `..`；`_remap_absolute_paths` 同口径 fail-closed。
3. 兼容性：正常嵌套（`data/uploads/phase2/...`）与符号链接前缀字面差异（macOS 临时目录 `/var → /private/var` 同类场景）均受理。

## 二、复跑结果（全部退出码 0）

- ① 逃逸探针（本目录 `verify-round-probe.py`，复跑用）：
  `PYTHONDONTWRITEBYTECODE=1 python3 docs/evidence/first-launch-t4/verify-round-probe.py` → **PASS 22 / FAIL 0**（`verify-round-probe.log`）。
  要点：三种 `..` 形态（`data/uploads/../x`、`../x`、`<uploads>/../x`）全部拒绝；同探针以 `t4-changes.diff` 逐字提取的修复前逻辑复演，三种形态均能读到上传根外文件（证明复现真实、探针有判别力）；上传根/备份包根符号链接拒绝；`...` 合法文件名不误伤；符号链接前缀双向兼容；受理路径最终 resolve 均在根内。
- ② 聚焦套件：`PYTHONDONTWRITEBYTECODE=1 python3 -m unittest -v test_first_launch_t4 test_first_launch_t4_deploy test_backup_restore` → **Ran 49: OK**（`verify-round-tests.log`）。
- ③ 隔离副本全量回归（rsync 排除 `data/`、`.git`、`dist/`）：17 套件（test_first_launch_t4 / test_first_launch_t4_deploy / test_backup_restore / test_first_launch_t1_security / test_pilot_checks / test_app / test_buyer_api / test_enterprise_knowledge / test_enterprise_knowledge_task4 / test_knowledge_ingestion / test_knowledge_phase2_ai_gates / test_knowledge_phase2_http / test_knowledge_phase2_source_ownership / test_knowledge_phase2_topic_pin_fix / test_m1_review / test_productionization / test_v1_03）→ **Ran 329 in 79.8s: OK (skipped=22)**（`verify-round-targeted-regression.log`）。
- ④ 端到端演练：`python3 tools/pilot_checks/check_first_launch_t4.py --out docs/evidence/first-launch-t4/verify-round-drill-report.json` → **28 PASS / 0 FAIL / 0 REVIEW / 4 INFO**（`verify-round-drill.log`、`verify-round-drill-report.json`）。
- ⑤ 发布包工具实测审计（本目录 `verify-round-package-audit.py`，复跑用）：
  `PYTHONDONTWRITEBYTECODE=1 python3 docs/evidence/first-launch-t4/verify-round-package-audit.py` → **PASS 32 / FAIL 0**（`verify-round-package-audit.log`）。
  要点：真实仓库打包成功（57 成员、清单 46 文件、包外/包内哈希校验一致、`dirty=True` 已记录）；不含 `data/`（真实原件）、`.env/.pem/.key`、`.git`、`docs/design/fixtures`、`test_*`；`b2b.env.example` 未注释行无密钥样式赋值；伪造污染源（`deploy/service.env`、`deploy/server.key`、`deploy/linked.pem` 符号链接、`tools/pilot_checks/test_leak.py`）全部 `exit 2` 拒绝且不产出半成品包。

## 三、复跑速记

- 全部命令与结果见 `verify-round-metadata.json`；探针/审计脚本随本目录存放，可直接复跑。
- 基线：HEAD `7a79083` + 未提交修复改动（不提交/不推送）。

## 四、未完成/待外部

- 服务器端 Linux 验收（systemd 单元、nginx HTTPS 引导、`backup_snapshot.sh` 真跑、发布包安装）待 Workbench 执行；
- 独立验收（Codex/第三方）——本轮为制作方复核，不构成独立验收。
