#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""真实 DeepSeek 账户联调检查（需要真实 Key，由配置者在本机执行；不进入自动测试）。

用法（在仓库根目录）:
  export AI_MODE=deepseek
  export DEEPSEEK_API_KEY=sk-...      # 只在本机 shell 配置；不要写入仓库、文档、日志或截图
  python3 tools/pilot_checks/check_deepseek_live.py

行为：读取项目配置（AI_MODE / DEEPSEEK_API_KEY / DEEPSEEK_BASE_URL / DEEPSEEK_MODEL /
DEEPSEEK_TIMEOUT_SECONDS / DEEPSEEK_MAX_TOKENS），对标准 Chat Completions 端点发一次最小
需求抽取调用，并走一遍白名单/单位校验。只打印模式、模型、状态、耗时与用量——绝不打印
密钥，也不把密钥写入任何文件。成功后可作为试点核对表第 6 项「真实模型接入联调」的连接
证据；完整联调还需按该行要求重跑故障演练。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from ai_client import (  # noqa: E402
    AIConfig,
    AIConfigurationError,
    AIService,
    AIUnavailableError,
    STATUS_LABELS_ZH,
)

SAMPLE_TEXT = (
    "We need a clean-water pump for a plant room: flow 30 m³/h, head 25 m, "
    "delivery to Hamburg in about 8 weeks."
)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default=None, help="覆盖 DEEPSEEK_BASE_URL（默认读环境变量）")
    parser.add_argument("--model", default=None, help="覆盖 DEEPSEEK_MODEL（默认读环境变量）")
    args = parser.parse_args()

    try:
        config = AIConfig.from_env()
        if args.base_url:
            config.base_url = args.base_url.rstrip("/")
        if args.model:
            config.model = args.model
        if config.mode != "deepseek":
            print("FAIL：当前 AI_MODE 不是 deepseek。请先在本机配置 AI_MODE=deepseek 与 DEEPSEEK_API_KEY 后重试。")
            raise SystemExit(1)
        service = AIService(config)
        candidates, meta = service.extract_requirements(SAMPLE_TEXT)
    except AIConfigurationError as error:
        print(f"FAIL（配置）：{error}")
        raise SystemExit(1)
    except AIUnavailableError as error:
        label = STATUS_LABELS_ZH.get(error.kind, error.kind)
        print(f"FAIL（调用）：状态 {error.kind}（{label}）· 耗时 {error.duration_ms} ms")
        if error.action == "configure":
            print("建议：检查 DEEPSEEK_API_KEY 是否有效、账户余额是否充足，修正后重试本脚本。")
        elif error.action == "retry":
            print("建议：稍后重试本脚本；若持续失败，检查网络与 DEEPSEEK_BASE_URL 配置。")
        else:
            print("建议：核对模型名与请求参数，或查看应用内 ai_call_log 的同类记录。")
        raise SystemExit(1)

    print(f"PASS：连接成功 · 模式 {config.mode} · 模型 {config.model}")
    print(
        f"  状态 ok · 耗时 {meta.get('duration_ms')} ms · HTTP {meta.get('http_status')} · "
        f"tokens {meta.get('prompt_tokens')}+{meta.get('completion_tokens')}"
    )
    print(f"  抽取候选（经白名单/单位/结构校验后）：{candidates}")
    print("说明：本次输出只含状态与用量，不含密钥；真实联调结论还需按试点核对表第 6 项重跑故障演练并签署。")


if __name__ == "__main__":
    main()
