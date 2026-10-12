# -*- coding: utf-8 -*-
"""B2-B10 GUI 验证用隔离实例：临时库 + 对外/AI 主题草稿（已核对人工段落）。

端口 18974；数据目录 /tmp/b2b-phase2-gui-b10（临时库 + 私有上传目录）；
不接真实模型、不触真实库与密钥（DIFY/模型密钥变量在子进程移除）。
"""
import os
import sys
from pathlib import Path

ROOT = Path("/Users/jiahaoshang/.codex/worktrees/196c/b2b 售前客户管理平台")
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)
for key in tuple(os.environ):
    if key.startswith("DIFY_") or key in ("DEEPSEEK_API_KEY", "OPENAI_API_KEY"):
        os.environ.pop(key, None)
base = Path("/tmp/b2b-phase2-gui-b10")
(base / "uploads").mkdir(parents=True, exist_ok=True)
os.environ["B2B_KB_STORAGE_ROOT"] = str(base / "uploads")

from app import make_server  # noqa: E402

db_path = base / "db.sqlite"
server = make_server(db_path, port=18974)
db = server.RequestHandlerClass.db
TENANT = "demo-tenant"
if not db.conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]:
    db.seed_demo_users()
admin = db.authenticate_user("demo-admin@example.invalid", "demo-admin")
topic_id = None
for row in db.conn.execute(
        "SELECT id, status FROM kb2_topics WHERE tenant_id=? AND title='GUI 对外发布主题'", (TENANT,)):
    if row["status"] == "draft":
        topic_id = row["id"]
if topic_id is None:
    topic_id = db.kb2_create_topic(TENANT, admin["id"], "GUI 对外发布主题")
    db.kb2_save_topic_draft(
        TENANT, admin["id"], topic_id,
        paragraphs=[{"id": "p1", "kind": "intro", "text": "对外正文（GUI 验证）。", "origin": "manual",
                     "checked": True, "source": {"kind": "manual"}}],
        usage="external", ai_authorized=True)

# 材料（返回行为验证用）：TXT 已发布（最新资料在筛选 form=TXT&status=published 下可见）。
existing = db.conn.execute(
    "SELECT item_id FROM kb2_sources WHERE tenant_id=? AND filename='guide.txt' AND item_id IS NOT NULL",
    (TENANT,)).fetchone()
if existing is None:
    batch = db.kb2_create_batch(
        TENANT, admin["id"], [{"kind": "file", "filename": "guide.txt", "data": "安装指南正文。".encode()}])
    src = db.kb2_get_batch(TENANT, batch)["sources"][0]
    for row in db.conn.execute(
            "SELECT id FROM kb2_paragraphs WHERE tenant_id=? AND source_id=?", (TENANT, src["id"])).fetchall():
        db.kb2_update_paragraph(TENANT, admin["id"], row["id"], checked=True)
    db.kb2_publish_batch(TENANT, admin["id"], batch)
for row in db.conn.execute(
        "SELECT id FROM kb2_sources WHERE tenant_id=? AND filename='guide.txt' ORDER BY created_at DESC LIMIT 1",
        (TENANT,)):
    print("MATERIAL_SOURCE", row["id"], flush=True)
print("TOPIC_ID", topic_id, flush=True)
print("READY http://127.0.0.1:18974", flush=True)
server.serve_forever()
