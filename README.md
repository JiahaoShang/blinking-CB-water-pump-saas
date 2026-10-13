# 工业品 B2B 售前平台（MVP 开发中）

本仓库当前有产品需求和一份从 GitHub 测试代码导入的 Python/SQLite 开发基线。**应用尚未通过当前 MVP 的 M1–M7 验收，也不能直接用于真实试点。**代码来源、已验证行为和差距见[现有代码核查与复用决策](docs/现有代码核查与复用决策.md)；开发范围从[文档入口](docs/README.md)开始阅读。

## 本地运行测试基线

在仓库根目录执行（已用本机 Python 3.9.6 验证，无第三方 Python 依赖）：

```sh
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest -q
python3 app.py --host 127.0.0.1 --port 8000 --db data/app.db
```

浏览器打开 `http://127.0.0.1:8000/login`。数据库会在指定位置创建，`data/` 已被 Git 忽略。

**首次使用（普通新库没有任何账号）**：应用初始化不再预置演示企业或演示账号，也不存在默认口令的管理员。启动前必须显式创建第一位管理员：

```sh
python3 tools/create_admin.py --db data/app.db --email you@example.com --name "Your Name"
# 交互式输入两遍密码（不回显）；部署脚本可用 --password-stdin 从标准输入读取
```

其余成员登录后在企业后台「成员管理」创建。不要将这份基线直接暴露到公网、输入真实买家信息或作为已完成的 API 接入方案。

**本地演示（含文档化演示账号）**：`python3 tools/demo/start_demo.py` 一条命令构建演示库并创建演示企业、演示账号（管理员 `demo-admin`、销售 `demo-sales`、工程师 `demo-engineer`，密码与账号相同）与参考买方入口，详见 [演示剧本](docs/演示剧本.md)。演示库包含两个运行范围不同的可比合成演示型号（SLICE-T1、SLICE-T2）与一个无依据对照型号；所有型号名、来源文件与字段出处均显式标注 DEMO 合成演示，不对应任何真实厂商规格。这些演示账号只存在于演示命令创建的本地演示库，不得用于任何真实部署；普通数据库初始化不会创建它们。

在管理员的“网站 API 接入”页登记本地来源（如 `http://127.0.0.1:8000`），将生成的网站标识填入 `http://127.0.0.1:8000/reference-client`。此页面是调用买方 API 的参考聊天窗口：可发送需求、确认工况、查看推荐、提问、请求人工，以及提交和编辑 RFQ。它只用于本地联调，不是可直接安装到客户官网的生产组件。

参考聊天推荐的产品页使用 `/s/{网站标识}/products/{型号路径}`。同一浏览器、同一站点下，详情页底部表单会复用原聊天会话并调用同一 RFQ API；直接打开该链接的买家会在提交时建立自己的会话。演示页面的技术资料、文案与视觉仍需按 M2 验收，不可把当前页面作为正式对外产品页。

## 本地备份与隔离恢复检查

在应用使用本地 SQLite 和 `data/uploads/` 时，可用下列命令制作数据库一致性快照，并将数据库与上传文件恢复到**新的隔离目录**检查。备份与恢复目标须是尚不存在的路径，并放在仓库外或已忽略的 `data/` 内。

```sh
python3 tools/backup_restore.py backup --db data/app.db --uploads data/uploads --out data/backup-001
python3 tools/backup_restore.py restore-check --bundle data/backup-001 --out data/restore-check-001
```

工具使用 SQLite 在线备份接口，核对文件哈希和恢复后数据库完整性，并报告可恢复的 RFQ 与有文件的资料数量。它是本地演练工具，**不是**自动定时备份、异地保存或真实试点部署方案；首次使用真实资料前，仍须选择托管存储和备份策略，实际演练恢复并记录耗时和最后可恢复记录。

## 启用 DeepSeek 模型辅助（可选；默认仍是本地规则）

默认不设置 `AI_MODE` 时使用本地规则引擎（当前演示默认），行为与既有演示一致，不需要任何密钥。要让买方自由文本需求识别与资料内问答走 DeepSeek 模型辅助（模型只做候选抽取与字段定位；数值、答案和引用仍由平台从已发布核准快照构造，模型输出不是核准事实）：

```sh
export AI_MODE=deepseek
export DEEPSEEK_API_KEY=sk-...      # 只在本机 shell 配置；不要写入仓库、文档、日志或截图
python3 app.py --host 127.0.0.1 --port 8000 --db data/app.db
# 也可用 --ai-mode deepseek 覆盖本次启动；start_demo.py 同样读取这些环境变量
```

- **官方通道固定参数（2026-10-13 用户明确）**：平台 DeepSeek 与企业自定义指向官方 `api.deepseek.com` 时，请求固定 `model=deepseek-flash`、`thinking={"type":"enabled"}`（思考开启，不关闭）、`reasoning_effort="low"`（官方支持 low/high/max，默认档为 high）；思考模式忽略 `temperature`，官方通道不再发送该参数。环境变量、直接构造、运行期修改与旧企业配置都不能切换模型或关闭/调整思考；公开状态与调用日志的模型与实际请求一致。企业自定义指向其他第三方兼容端点时保留原协议，不注入 DeepSeek 专用字段。未显式设置输出上限时，官方通道默认 1024 tokens（真实低思考在 512 下会 `finish_reason=length` 截断，2026-10-13），第三方兼容端点默认沿用 512；显式 `DEEPSEEK_MAX_TOKENS` 与直接参数一律尊重，不静默忽略费用上限；思考强度保持 low，不自动升档。历史纠正：此前实现的「显式关闭思考模式」（`thinking disabled`，自 `8368d0a` 起）系对需求的误解，本项按用户明确要求改为思考开启、强度 low。
- 可选配置：`DEEPSEEK_MODEL`（仅兼容读取：官方通道读取与保存时规范化为 `deepseek-flash`，不能经环境变量切换）、`DEEPSEEK_BASE_URL`（默认 `https://api.deepseek.com`，仅测试/代理场景覆盖）、`DEEPSEEK_TIMEOUT_SECONDS`（默认 20，上限 120）、`DEEPSEEK_MAX_TOKENS`（显式输出上限，1–8192；未设置时按通道取默认——官方通道 1024、第三方端点 512）。密钥只经 `DEEPSEEK_API_KEY` 环境变量读取；`.env` 已在 `.gitignore` 中。
- `AI_MODE=deepseek` 而未设置 `DEEPSEEK_API_KEY` 时应用会**明确拒绝启动**，不会落回规则结果（避免把未联调误认为已联调）。模型故障时买方只看到统一的「暂不可用、消息已保存、可重试」提示；内部状态（可重试/需配置/需排查）记录在管理概览「模型接入」卡片与 `ai_call_log` 表（只含提供方、模型、状态、耗时、用量，不含密钥与买家消息）。
- **真实账户联调（未验证，待配置者本机执行）**：在配置 Key 后运行 `python3 tools/pilot_checks/check_deepseek_live.py`，输出只含状态/耗时/用量。该步骤属于[试点就绪核对表](docs/试点就绪核对表.md)第 6 项；在此之前，代码适配由本地模拟服务测试覆盖，**不等于**真实 DeepSeek 账户联调完成。
- 自动测试不需要真实 Key，也不要为跑测试设置 `AI_MODE=deepseek`；模型边界行为由本地 HTTP 模拟服务测试（`test_ai_deepseek.py`）覆盖。

## 当前开发约束

- GitHub 旧代码是测试材料，原有单元测试通过不代表符合当前用户旅程；尤其是人工请求、RFQ、公开页面、API、匿名会话权限和销售记录仍需修正。
- 产品资料和规则按演示状态处理。技术事实经企业授权人员核准后才能对外发布；没有资料的字段不得编造。
- 业务操作最终须通过产品页面和标准 API 完成，开发聊天不是应用运行环节。
- 每个切片先按 [MVP 需求与验收](docs/MVP-开发需求文档.md)写清可验证结果，再更新[实际状态](docs/开发状态与变更记录.md)。
