"""Codex T1 independent verification: source copy only, never the workspace data directory."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

source = Path(__file__).resolve().parents[3]
evidence = Path(__file__).resolve().parent
target = Path(tempfile.mkdtemp(prefix="b2b-t1-final-independent-"))
shutil.copytree(source, target, dirs_exist_ok=True,
                ignore=shutil.ignore_patterns(".git", "data", ".venv*", "__pycache__"))
environment = {key: value for key, value in os.environ.items()
               if not key.startswith(("DIFY_", "DEEPSEEK_", "OPENAI_", "LANGBOT_", "WHATSAPP_",
                                      "META_", "TELEGRAM_", "TWILIO_", "B2B_KB_STORAGE_ROOT"))}
environment["AI_MODE"] = "rules"
names = [
    "test_first_launch_t1_security", "test_v1_permissions", "test_business_channels",
    "test_model_settings", "test_buyer_access", "test_buyer_api", "test_handoff_api",
    "test_productionization", "test_demo_tooling", "test_backup_restore", "test_channel_runtime",
    "test_knowledge_phase2_http", "test_knowledge_phase2_source_ownership",
    "test_knowledge_phase2_ai_gates", "test_enterprise_knowledge", "test_enterprise_knowledge_phase2",
    "test_knowledge_ingestion", "test_system_acceptance", "test_v1_02", "test_v1_analytics",
    "test_whatsapp_personal",
]
files = ["app.py", "enterprise_knowledge.py", "enterprise_knowledge_phase2.py",
         "tools/backup_restore.py", "test_first_launch_t1_security.py"]
metadata = {"python": sys.version, "interpreter": sys.executable, "source": str(source),
            "isolated_copy": str(target), "test_modules": names, "ai_mode": "rules",
            "source_sha256": {name: hashlib.sha256((source / name).read_bytes()).hexdigest()
                              for name in files}}
with (evidence / "independent-final-tests.log").open("w", encoding="utf-8") as log:
    log.write(json.dumps(metadata, ensure_ascii=False, indent=2) + "\n")
    log.flush()
    result = subprocess.run([sys.executable, "-m", "unittest", "-v", *names],
                            cwd=target, env=environment, stdout=log, stderr=subprocess.STDOUT)
    log.write(f"\nexit={result.returncode}\n")
metadata["exit_code"] = result.returncode
(evidence / "independent-final-metadata.json").write_text(
    json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
print(json.dumps({"isolated_copy": str(target), "exit_code": result.returncode}, ensure_ascii=False))
raise SystemExit(result.returncode)
