"""批次发布确认 E2E 的临时服务：临时库 + 临时原件目录（不触 data/、密钥、真实模型）。"""
import os
import sys
from pathlib import Path

ROOT = Path("/Users/jiahaoshang/.codex/worktrees/196c/b2b 售前客户管理平台")
sys.path.insert(0, str(ROOT))

BASE = Path("/Users/jiahaoshang/.hermes/cache/scratch/b2b-kb2-batch-confirm-2026-10-13")
BASE.mkdir(parents=True, exist_ok=True)
UPLOADS = BASE / "uploads"
UPLOADS.mkdir(exist_ok=True)
DB = BASE / "kb2-e2e.db"
for stale in (DB, DB.with_suffix(".db-journal"), DB.with_suffix(".db-wal")):
    if stale.exists():
        stale.unlink()
os.environ["B2B_KB_STORAGE_ROOT"] = str(UPLOADS)

from app import make_server  # noqa: E402

TENANT = "demo-tenant"
PORT = int(os.environ.get("KB2_E2E_PORT", "18990"))

server = make_server(DB, port=PORT)
db = server.RequestHandlerClass.db
db.knowledge_storage_root = UPLOADS
db.seed_demo_users()
admin = db.authenticate_user("demo-admin@example.invalid", "demo-admin")


def make_batch(name, text, usage, ai):
    batch_id = db.kb2_create_batch(
        TENANT, admin["id"], [{"kind": "file", "filename": name, "data": text.encode("utf-8")}])
    db.kb2_process_pending_sources(TENANT)
    batch = db.kb2_get_batch(TENANT, batch_id)
    source = batch["sources"][0]
    for paragraph in source["paragraphs"]:
        db.kb2_update_paragraph(TENANT, admin["id"], paragraph["id"], text=paragraph["text"], checked=True)
    if usage == "external":
        db.kb2_save_batch_settings(TENANT, admin["id"], batch_id, usage="external", ai_authorized=ai)
    return batch_id


ext_batch = make_batch("保修条款.txt", "保修24个月。\n安装前断电。", "external", True)
int_batch = make_batch("内部资料.txt", "内部维护手册要点。", "internal", False)

print(f"EXT {ext_batch}", flush=True)
print(f"INT {int_batch}", flush=True)
print("READY", flush=True)
server.serve_forever()
