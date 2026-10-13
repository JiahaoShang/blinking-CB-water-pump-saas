#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""首次上线 T2 修复验证隔离运行器：把源码复制到不含 data/.git/venv 的临时副本再运行测试。

用法（日志写入本目录；副本目录建在系统临时区）：
  python3 docs/evidence/first-launch-t2/t2-isolated-run.py <标签> <副本前缀> [测试目标 ...]
    <标签>      日志/元数据文件名前缀，如 repro-before-fix、after-fix、full-suite-after-fix
    <副本前缀>  临时副本目录名前缀，如 b2b-t2-red、b2b-t2-green
    测试目标    unittest 目标（模块名或模块.类）；省略 = 全量 discover（README 标准口径）

隔离：副本排除 .git / data / .venv* / __pycache__ / .DS_Store；
运行环境剔除已知凭据类变量；PYTHONDONTWRITEBYTECODE=1。
不访问真实业务库、任何密钥、外部服务与真实模型。
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

SOURCE = Path(__file__).resolve().parents[3]
EVIDENCE = Path(__file__).resolve().parent
TRACKED = ["app.py", "enterprise_knowledge_ui.py", "test_first_launch_t2.py"]
SCRUB_PREFIXES = ("DIFY_", "DEEPSEEK_", "OPENAI_", "LANGBOT_", "WHATSAPP_", "META_",
                  "TELEGRAM_", "TWILIO_", "B2B_KB_STORAGE_ROOT")


def main() -> int:
    if len(sys.argv) < 3:
        print(__doc__)
        return 2
    label, prefix, *targets = sys.argv[1:]
    target = Path(tempfile.mkdtemp(prefix=f"{prefix}-"))
    shutil.copytree(SOURCE, target, dirs_exist_ok=True,
                    ignore=shutil.ignore_patterns(".git", "data", ".venv*", "__pycache__", ".DS_Store"))
    env = {key: value for key, value in os.environ.items() if not key.startswith(SCRUB_PREFIXES)}
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    command = ([sys.executable, "-m", "unittest", "-v", *targets] if targets
               else [sys.executable, "-m", "unittest", "discover", "-q"])
    metadata = {
        "label": label,
        "python": sys.version,
        "interpreter": sys.executable,
        "source": str(SOURCE),
        "isolated_copy": str(target),
        "command": " ".join(command),
        "targets": targets or ["discover"],
        "excluded": [".git", "data", ".venv*", "__pycache__", ".DS_Store"],
        "env_scrubbed_prefixes": list(SCRUB_PREFIXES),
        "tracked_sha256": {name: hashlib.sha256((SOURCE / name).read_bytes()).hexdigest()
                           for name in TRACKED if (SOURCE / name).exists()},
    }
    log_path = EVIDENCE / f"{label}.log"
    with log_path.open("w", encoding="utf-8") as log:
        log.write(json.dumps(metadata, ensure_ascii=False, indent=2) + "\n\n")
        log.flush()
        result = subprocess.run(command, cwd=target, env=env, stdout=log, stderr=subprocess.STDOUT)
        log.write(f"\nexit={result.returncode}\n")
    metadata["exit_code"] = result.returncode
    (EVIDENCE / f"{label}-metadata.json").write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"label": label, "isolated_copy": str(target),
                      "exit_code": result.returncode, "log": str(log_path)}, ensure_ascii=False))
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
