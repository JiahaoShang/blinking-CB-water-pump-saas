# ossutil 2.4.0 预检修复：官方二进制实核证据（本目录）

本目录是“T4 异地链预检 `-s` 修复”的官方 CLI 实核材料。全部来自官方发布物与本机 mock 端点，
**未连接任何真实云服务、未使用任何真实密钥**（ossutil 配置为一次性 dummy 值 + 本机回环 endpoint）。

> **退役说明（2026-10-14）**：本目录的 `mock_oss_server2.py`、`case6-harness.sh`、`e2e-run.sh` 已按[历史一次性测试脚本清理记录](../../../历史一次性测试脚本清理记录.md)退役删除。下文命令与输出记录仅说明当轮证据如何产生，属历史留档，不作当前操作步骤；当前异地备份验证入口为 `test_first_launch_t4_offsite.py`（使用其自身夹具）。

## 官方下载与校验（官方源 2026-10-13 实下载）

- 下载页（官方）：https://help.aliyun.com/zh/oss/developer-reference/ossutil-overview/
  （表内含各平台包名与 SHA256；历史版本页含 2.4.0 全平台直链）
- macOS ARM64（本机运行用）：`https://gosspublic.alicdn.com/ossutil/v2/2.4.0/ossutil-2.4.0-mac-arm64.zip`
  实测 sha256 = `26e51080c6d6c9848fe3aea8a6b0cbd88453e65cd41c5a27573c59335a78a41d`（与官方公布一致）
- Linux x86_64（服务器使用）：`https://gosspublic.alicdn.com/ossutil/v2/2.4.0/ossutil-2.4.0-linux-amd64.zip`
  实测 sha256 = `85edf66b2fb7238f5c7e25cab820cf29312319fe4935b7c86a6b8485eb434f3c`（与官方公布一致）

服务器安装命令（Ubuntu；以官方文档口径）：

```sh
curl -o ossutil-2.4.0-linux-amd64.zip https://gosspublic.alicdn.com/ossutil/v2/2.4.0/ossutil-2.4.0-linux-amd64.zip
sha256sum ossutil-2.4.0-linux-amd64.zip   # 应等于 85edf66b2fb7238f5c7e25cab820cf29312319fe4935b7c86a6b8485eb434f3c
unzip ossutil-2.4.0-linux-amd64.zip
cd ossutil-2.4.0-linux-amd64
chmod 755 ossutil
sudo mv ossutil /usr/local/bin/ && sudo ln -s /usr/local/bin/ossutil /usr/bin/ossutil
ossutil version    # 期望输出 2.4.0
```

## 本目录文件

| 文件 | 说明 |
| --- | --- |
| version.txt | `ossutil version` → `2.4.0` |
| ls-help.txt | `ossutil ls --help` 全文（实核：无 `-s` 短标志；短输出为长标志 `--short-format`；默认 long/表格输出） |
| global-help.txt | `ossutil --help` 全文（全局参数：`--config-file`、`--endpoint`、`--language` 等） |
| ls-s-repro.txt | `ossutil --config-file <dummy> ls -s oss://…/backups/…/` 复现：`Error: unknown shorthand flag: 's' in -s`，退出码 1（与服务器 19:08 报错逐字一致） |
| ls-default-nonempty.txt | 默认表格输出（前缀下有对象）：表头 + 数据行（ObjectName 列含 `oss://`）+ `Object Number is: 2` |
| ls-default-empty.txt | 默认表格输出（空前缀）：仅 `Object Number is: 0` |
| ls-fail-403.txt | 列表失败（mock 返回 403）：`Error: operation error ListObjectsV2 … 403 … AccessDenied`，退出码 2 |
| ls-shortformat-nonempty.txt / ls-shortformat-empty.txt | `--short-format` 对照输出（行长逐条为 `oss://…`；空为同一计数行）——2.4.0 真实支持的短输出形态 |
| ls-language-ch-empty.txt | `--language CH` 对照：计数行与默认完全一致（该行不随语言变化） |
| mock_oss_server2.py | 本机 mock OSS 端点（仅 127.0.0.1；ListObjectsV2 + HEAD/GET/PUT），产生上表输出；不接触任何真实云 |
| case6-harness.sh | 用上述真输出驱动修复后脚本⑥预检块（eval 原文）的判定核对脚本（结果见上级目录 `ossutil-precheck-fix-real-outputs.log`） |
| e2e-run.sh | “真脚本 + 真 ossutil 2.4.0 + 真 age”本机端到端运行器（结果见上级目录 `ossutil-real-script-e2e.log`） |

## 关键结论（实核）

1. 2.4.0 `ls` 无 `-s`；不存在的短标志按 pflag 口径报 `unknown shorthand flag: 's' in -s` 并退出 1；未知长标志报 `unknown flag: …` 并退出 1。
2. 默认（long）输出为表格；对象行首为时间列，`oss://bucket/key` 出现在行中 ObjectName 列；空前缀输出仅计数行。
3. 计数行排版为 `Object Number is: N`（冒号后一个空格）；语法上兼容用户服务器回传的无空格形态 `Object Number is:0`（解析容忍 `[:space:]*`）。
4. 上述三种形态（非空/空/失败）的原始输出已按字节留存，供测试夹具与复跑核对。
