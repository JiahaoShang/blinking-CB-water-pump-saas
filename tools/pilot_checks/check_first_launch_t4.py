#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""首次邀请测试上线 T4：最小部署与备份恢复端到端演练（本机、全离线、临时数据）。

覆盖 T4 验收（对照 docs/首次上线-T4执行方案.md）：
1. 新安装：显式创建首位管理员（普通新库无默认账号）→ 服务启动于 127.0.0.1。
2. 真实 HTTP：管理员登录（会话 Cookie）；登记官网来源；买方会话/消息/RFQ 提交。
3. 合成持久状态：人工任务、知识条目与原件、企业模型配置（假密钥）、桥登录状态目录、
   待处理入库作业（记录数据均写入临时目录）。
4. 重启（不恢复）：会话、RFQ、人工任务、知识保持；待处理作业在重启后继续处理完成。
5. 停写窗口备份：停止应用后运行 tools/backup_restore.py（覆盖三类原件 / 模型配置 / 桥状态 / 运维配置）。
6. 模拟原机丢失原件与私有配置 → 隔离恢复到空目录（路径重映射、目录名映射、私有权限）。
7. 恢复实例启动：登录会话/RFQ/任务/知识可用；模型配置可读；待处理作业继续处理；
   可创建新会话与 RFQ（业务可继续）。

约束：全部数据写入系统临时目录；不读不写仓库 data/；强制离线（清除模型/渠道/Dify 环境变量，
AI_MODE 缺省=本地规则）；不连外部账号、不发送任何外部消息；不读取密钥。
退出码：0=全部通过；1=存在 FAIL/REVIEW；2=用法/环境问题（未执行演练）。
本脚本是制作方自测工具；结果不自动构成独立验收。

用法：
  python3 tools/pilot_checks/check_first_launch_t4.py [--keep-workdir] [--out /tmp/t4-report.json]
"""

from __future__ import annotations

import argparse
import hashlib
import http.client
import json
import os
import re
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

REQUIRED_ENV_PREFIXES = ("DEEPSEEK", "DIFY", "TWILIO", "META", "LANGBOT", "WHATSAPP", "AI_MODE")
ADMIN_EMAIL = "t4-drill-admin@example.invalid"
ADMIN_PASSWORD = "t4-drill-password-123"
TENANT = "pilot-t4"
TASK_QUESTION = "T4 drill manual question about pump head"
KNOWLEDGE_TITLE = "T4 drill knowledge note"
JOB1_FILE = "t4-drill-job1.txt"
JOB2_FILE = "t4-drill-job2.txt"
BRIDGE_ACCOUNT = "0123456789abcdef01234567"


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class Report:
    """逐项状态 + 摘要；状态：PASS / FAIL / REVIEW / INFO / SKIP。"""

    ALLOWED = ("PASS", "FAIL", "REVIEW", "INFO", "SKIP")

    def __init__(self) -> None:
        self.items: list[dict] = []
        self.started = now_iso()

    def add(self, section: str, name: str, status: str, detail: str = "", **extra) -> None:
        if status not in self.ALLOWED:
            raise ValueError(f"未知状态 {status}")
        item = {"section": section, "name": name, "status": status, "detail": str(detail), **extra}
        self.items.append(item)
        print(f"[{status}] {section}: {name}" + (f" — {detail}" if detail else ""))

    def check(self, section: str, name: str, condition: bool, detail: str = "") -> bool:
        self.add(section, name, "PASS" if condition else "FAIL", detail)
        return bool(condition)

    def counts(self) -> dict:
        result = {status: 0 for status in self.ALLOWED}
        for item in self.items:
            result[item["status"]] += 1
        return result

    def payload(self) -> dict:
        return {"started_at": self.started, "finished_at": now_iso(), "tool_sha256": sha256_file(Path(__file__)),
                "python": sys.version.split()[0], "items": self.items, "counts": self.counts()}


def scrubbed_env(uploads_dir: Path) -> dict:
    """服务子进程环境：清除模型/渠道/Dify 变量并强制上传目录；保持完全离线。"""
    env = {}
    for key, value in os.environ.items():
        if key == "AI_MODE" or key.startswith(REQUIRED_ENV_PREFIXES):
            continue
        env[key] = value
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["PYTHONUNBUFFERED"] = "1"
    env["B2B_UPLOAD_DIR"] = str(uploads_dir)
    return env


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


class Server:
    """本机应用实例（子进程 app.py）；stop() 即演练中的“停写窗口”动作。"""

    def __init__(self, code_root: Path, db_path: Path, uploads_dir: Path, port: int, log_path: Path):
        self.code_root = code_root
        self.db_path = db_path
        self.uploads_dir = uploads_dir
        self.port = port
        self.log_path = log_path
        self.proc: subprocess.Popen | None = None
        self._log = None

    def start(self) -> None:
        self._log = self.log_path.open("ab")
        self.proc = subprocess.Popen(
            [sys.executable, "app.py", "--host", "127.0.0.1", "--port", str(self.port), "--db", str(self.db_path)],
            cwd=str(self.code_root), env=scrubbed_env(self.uploads_dir),
            stdout=self._log, stderr=subprocess.STDOUT,
        )
        deadline = time.time() + 30
        while time.time() < deadline:
            if self.proc.poll() is not None:
                raise RuntimeError(f"服务启动失败（退出码 {self.proc.returncode}）：见 {self.log_path}")
            try:
                with socket.create_connection(("127.0.0.1", self.port), timeout=0.5):
                    status, _headers, _body = http_request(self.port, "GET", "/login")
                    if status == 200:
                        return
            except OSError:
                time.sleep(0.15)
        raise RuntimeError("服务未在 30 秒内就绪")

    def stop(self) -> None:
        if self.proc is None:
            return
        self.proc.terminate()
        try:
            self.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait(timeout=5)
        finally:
            if self._log is not None:
                self._log.close()
            self.proc = None


def http_request(port: int, method: str, path: str, body: bytes | None = None, headers: dict | None = None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=20)
    try:
        conn.request(method, path, body=body, headers=headers or {})
        response = conn.getresponse()
        return response.status, dict(response.getheaders()), response.read()
    finally:
        conn.close()


def form_body(fields: dict) -> bytes:
    return urllib.parse.urlencode(fields).encode("utf-8")


def json_body(payload: dict) -> bytes:
    return json.dumps(payload, ensure_ascii=False).encode("utf-8")


def open_db(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path), timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def poll_kb2_status(db_path: Path, filename: str, timeout: float = 75.0) -> tuple[str, str]:
    """轮询待处理作业状态直到离开 pending/processing；返回 (status, error)。"""
    deadline = time.time() + timeout
    status, error = "pending", ""
    while time.time() < deadline:
        with open_db(db_path) as conn:
            row = conn.execute(
                "SELECT status, error FROM kb2_sources WHERE tenant_id=? AND filename=? ORDER BY created_at DESC LIMIT 1",
                (TENANT, filename),
            ).fetchone()
        if row:
            status, error = str(row["status"]), str(row["error"] or "")
            if status not in ("pending", "processing"):
                return status, error
        time.sleep(2)
    return status, error


def run_cli(code_root: Path, args: list[str], timeout: int = 120) -> tuple[int, str]:
    result = subprocess.run(
        [sys.executable, str(code_root / "tools" / "backup_restore.py"), *args],
        cwd=str(code_root), capture_output=True, text=True, timeout=timeout,
        env=scrubbed_env(code_root / "data" / "uploads"),
    )
    return result.returncode, (result.stdout or "") + (result.stderr or "")


def run_drill(report: Report, workdir: Path, code_root: Path, port: int) -> None:
    sys.path.insert(0, str(code_root))
    from app import Database  # noqa: E402  运行所在代码根（默认本仓库）
    from model_settings import EnterpriseModels  # noqa: E402

    data_dir = workdir / "data"
    uploads = data_dir / "uploads"
    db_path = data_dir / "app.db"
    data_dir.mkdir(parents=True, exist_ok=True)
    # 本进程内的产品函数写入（知识导入/批次文件）也必须落到临时上传目录：
    # 与服务器子进程的 B2B_UPLOAD_DIR 保持一致，绝不触碰仓库 data/uploads。
    os.environ["B2B_UPLOAD_DIR"] = str(uploads)

    # ── 1. 新安装：先创建管理员（无默认账号），再启动服务 ──────────────────
    created = subprocess.run(
        [sys.executable, "tools/create_admin.py", "--db", str(db_path), "--email", ADMIN_EMAIL,
         "--name", "T4 Drill Admin", "--tenant", TENANT, "--tenant-name", "T4 演练企业", "--password-stdin"],
        cwd=str(code_root), input=ADMIN_PASSWORD + "\n", text=True, capture_output=True, timeout=60,
    )
    report.check("1 新安装", "首位管理员创建（普通新库）", created.returncode == 0,
                 (created.stdout or created.stderr).strip().splitlines()[-1] if (created.stdout or created.stderr) else "")

    server = Server(code_root, db_path, uploads, port, workdir / "server-1.log")
    server.start()
    report.add("1 新安装", "应用启动（127.0.0.1）", "PASS", f"port={port}，库与上传目录均在临时目录")

    # ── 2. 真实 HTTP：登录、登记官网、买方会话/RFQ ─────────────────────────
    status, headers, _body = http_request(port, "POST", "/login", form_body({"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD, "next": "/admin"}), {"Content-Type": "application/x-www-form-urlencoded"})
    cookie = ""
    match = re.search(r"cb_session=([^;]+)", headers.get("Set-Cookie", ""))
    if match:
        cookie = match.group(1)
    report.check("2 HTTP 业务", "管理员登录（会话 Cookie）", status in (302, 303) and bool(cookie), f"status={status}")
    auth_headers = {"Cookie": f"cb_session={cookie}"}
    status, _headers, body = http_request(port, "GET", "/admin", headers=auth_headers)
    report.check("2 HTTP 业务", "后台页面可访问", status == 200, f"GET /admin → {status}")

    origin = f"http://127.0.0.1:{port}"
    status, _headers, _body = http_request(port, "POST", "/admin/sites", form_body({"allowed_origin": origin}),
                                           {**auth_headers, "Content-Type": "application/x-www-form-urlencoded"})
    status2, _headers, sites_body = http_request(port, "GET", "/admin/sites", headers=auth_headers)
    site_match = re.search(r"网站标识：<code>([^<]+)</code>", sites_body.decode("utf-8", "replace"))
    site_key = site_match.group(1) if site_match else ""
    report.check("2 HTTP 业务", "登记官网来源并取得网站标识", bool(site_key), f"origin={origin}")

    buyer_headers = {"X-Site-Key": site_key, "Origin": origin, "Content-Type": "application/json"}
    status, _headers, body = http_request(port, "POST", "/api/v1/buyer/conversations",
                                          json_body({"site_key": site_key, "locale": "en"}), buyer_headers)
    created_ok = status == 201
    payload = json.loads(body.decode("utf-8")) if created_ok else {}
    conversation_id = payload.get("conversation_id", "")
    access_token = payload.get("access_token", "")
    report.check("2 HTTP 业务", "买方会话创建", created_ok and bool(conversation_id), f"status={status}")

    buyer_auth = {**buyer_headers, "Authorization": f"Bearer {access_token}"}
    status, _headers, body = http_request(
        port, "POST", f"/api/v1/buyer/conversations/{conversation_id}/messages",
        json_body({"text": "We need a clean-water pump for a plant room: flow 30 m3/h, head 25 m."}),
        {**buyer_auth, "Idempotency-Key": "t4-drill-message-1"},
    )
    message_id = ""
    if status in (200, 201):
        message_id = json.loads(body.decode("utf-8")).get("message_id", "")
    report.check("2 HTTP 业务", "买方消息提交（含需求抽取）", status in (200, 201) and bool(message_id), f"status={status}")

    status, _headers, body = http_request(
        port, "POST", f"/api/v1/buyer/conversations/{conversation_id}/rfqs",
        json_body({
            "contact": {"contact_name": "Drill Buyer", "company": "Drill Co", "contact_email": "drill-buyer@example.invalid",
                        "contact_phone": "+49 30 555 0208", "quantity": "1", "region": "Germany"},
            "contact_confirmed": True, "notes": "T4 drill rfq", "entry_surface": "chat",
        }),
        {**buyer_auth, "Idempotency-Key": "t4-drill-rfq-1"},
    )
    rfq_id = json.loads(body.decode("utf-8")).get("rfq_id", "") if status in (200, 201) else ""
    report.check("2 HTTP 业务", "买方 RFQ 提交", status in (200, 201) and bool(rfq_id), f"status={status} rfq={rfq_id}")

    # ── 3. 合成持久状态：人工任务 / 知识 / 模型配置 / 桥登录状态 ────────────
    with open_db(db_path) as conn:
        admin_row = conn.execute("SELECT id FROM users WHERE email=?", (ADMIN_EMAIL,)).fetchone()
    admin_id = admin_row["id"]
    db = Database(db_path)
    try:
        db.create_human_task(TENANT, conversation_id, message_id, TASK_QUESTION, "T4 演练：需人工核实",
                             {"confirmed_requirements": {}})
        knowledge = db.import_knowledge_file(TENANT, admin_id, filename="t4-drill-knowledge.txt",
                                             data=b"T4 drill knowledge body: lead time depends on configuration.",
                                             title=KNOWLEDGE_TITLE)
        knowledge_row = db.conn.execute(
            "SELECT storage_path FROM knowledge_sources WHERE id=?", (knowledge["source_id"],)).fetchone()
        job1_batch = db.kb2_create_batch(TENANT, admin_id, [{"kind": "file", "filename": JOB1_FILE,
                                                             "data": b"Job one paragraph about pumps.\n\nJob one detail."}],
                                         process_now=False)
    finally:
        db.conn.close()
    EnterpriseModels(db_path, None).save(TENANT, "https://api.example.com/v1", "t4-drill-model", "sk-t4-drill-fake-not-real")
    runtime_state = data_dir / "connection-runtime" / BRIDGE_ACCOUNT / "sessions" / "state.json"
    runtime_state.parent.mkdir(parents=True, exist_ok=True)
    runtime_state.write_bytes(b'{"logged_in": true, "drill": true}')
    os.chmod(runtime_state, 0o600)
    report.check("3 合成状态", "人工任务 / 知识原件 / 企业模型配置 / 桥登录状态写入",
                 Path(knowledge_row["storage_path"]).is_file() and runtime_state.is_file(),
                 f"knowledge={Path(knowledge_row['storage_path']).name}")

    new_job1_status, _ = poll_kb2_status(db_path, JOB1_FILE, timeout=1.0)
    server.stop()  # 停写窗口：重启演练开始（作业 1 已在库中保持 pending）

    # ── 4. 重启（不恢复）：会话与业务记录保存；待处理作业继续处理 ───────────
    server = Server(code_root, db_path, uploads, port, workdir / "server-2.log")
    server.start()
    status, _headers, body = http_request(port, "GET", "/admin", headers=auth_headers)
    report.check("4 重启保存", "重启后原登录会话仍有效", status == 200, f"GET /admin → {status}")
    status, _headers, rfqs_body = http_request(port, "GET", "/admin/rfqs", headers=auth_headers)
    report.check("4 重启保存", "RFQ 记录仍在", status == 200 and rfq_id in rfqs_body.decode("utf-8", "replace"))
    status, _headers, tasks_body = http_request(port, "GET", "/admin/tasks", headers=auth_headers)
    report.check("4 重启保存", "人工任务仍在", status == 200 and TASK_QUESTION in tasks_body.decode("utf-8", "replace"))
    status, _headers, item_body = http_request(port, "GET", f"/admin/knowledge/item/{knowledge['item_id']}", headers=auth_headers)
    report.check("4 重启保存", "知识条目仍在", status == 200 and KNOWLEDGE_TITLE in item_body.decode("utf-8", "replace"), f"item={knowledge['item_id']}")
    status, _headers, conv_body = http_request(port, "GET", f"/api/v1/buyer/conversations/{conversation_id}", headers=buyer_auth)
    report.check("4 重启保存", "买方访问凭据仍有效", status == 200, f"GET buyer conversation → {status}")

    job1_status, job1_error = poll_kb2_status(db_path, JOB1_FILE)
    report.check("4 重启保存", "待处理作业在重启后继续处理完成", job1_status in ("extracted", "registered"),
                 f"status={job1_status} error={job1_error[:60]}")
    server.stop()

    # ── 5. 停写窗口内备份（含作业 2 的待处理状态） ─────────────────────────
    db = Database(db_path)
    try:
        db.kb2_create_batch(TENANT, admin_id, [{"kind": "file", "filename": JOB2_FILE,
                                                "data": b"Job two paragraph about valves.\n\nJob two detail."}],
                            process_now=False)
    finally:
        db.conn.close()
    job2_pending, _ = poll_kb2_status(db_path, JOB2_FILE, timeout=1.0)
    ops_env = workdir / "b2b.env"
    ops_env.write_text("B2B_UPLOAD_DIR=/var/lib/b2b/data/uploads\nDEEPSEEK_API_KEY=drill-fake-key\n", encoding="utf-8")
    os.chmod(ops_env, 0o600)
    backup_out = workdir / "backup-1"
    code, output = run_cli(code_root, ["backup", "--db", str(db_path), "--uploads", str(uploads),
                                       "--out", str(backup_out), "--ops-config", str(ops_env)])
    bundle_ok = (
        code == 0
        and (backup_out / "uploads" / "phase2").is_dir()
        and (backup_out / "model-secrets").is_dir()
        and (backup_out / "connection-runtime" / BRIDGE_ACCOUNT / "sessions" / "state.json").is_file()
        and (backup_out / "ops-config" / "b2b.env").is_file()
        and (backup_out / "manifest.json").is_file()
    )
    report.check("5 备份", "停写窗口备份覆盖原件/模型配置/桥状态/运维配置", bundle_ok, output.strip().replace("\n", " | ")[:220])
    report.add("5 备份", "备份时作业 2 状态", "INFO", f"pending={job2_pending}")

    # ── 6. 模拟丢失 + 隔离恢复 ─────────────────────────────────────────────
    shutil.rmtree(uploads, ignore_errors=True)
    shutil.rmtree(Path(str(db_path) + ".model-secrets"), ignore_errors=True)
    shutil.rmtree(data_dir / "connection-runtime", ignore_errors=True)
    restored = workdir / "restored"
    code, output = run_cli(code_root, ["restore-check", "--bundle", str(backup_out), "--out", str(restored)])
    restored_ok = code == 0
    report.check("6 恢复", "隔离恢复到空目录", restored_ok, output.strip().replace("\n", " | ")[:220])
    report.check("6 恢复", "模型配置目录按恢复后库名映射", (restored / "app.db.model-secrets").is_dir())
    report.check("6 恢复", "桥登录状态恢复", (restored / "connection-runtime" / BRIDGE_ACCOUNT / "sessions" / "state.json").is_file())
    report.check("6 恢复", "运维配置按原相对路径恢复", (restored / "ops-config" / "b2b.env").is_file())
    secrets_file = next((p for p in (restored / "app.db.model-secrets").rglob("*") if p.is_file()), None)
    if secrets_file:
        report.check("6 恢复", "恢复产物私有权限（0600 文件 / 0700 目录）",
                     oct(secrets_file.stat().st_mode & 0o777) == oct(0o600)
                     and oct((restored / "app.db.model-secrets").stat().st_mode & 0o777) == oct(0o700))

    # ── 7. 恢复实例：业务可继续 ────────────────────────────────────────────
    restored_uploads = restored / "uploads"
    server = Server(code_root, restored / "app.db", restored_uploads, port, workdir / "server-3.log")
    server.start()
    status, _headers, body = http_request(port, "GET", "/admin", headers=auth_headers)
    report.check("7 恢复后", "登录会话在恢复后仍有效", status == 200, f"GET /admin → {status}")
    status, _headers, rfqs_body = http_request(port, "GET", "/admin/rfqs", headers=auth_headers)
    report.check("7 恢复后", "RFQ 可查看", status == 200 and rfq_id in rfqs_body.decode("utf-8", "replace"))
    status, _headers, tasks_body = http_request(port, "GET", "/admin/tasks", headers=auth_headers)
    report.check("7 恢复后", "人工任务可查看", status == 200 and TASK_QUESTION in tasks_body.decode("utf-8", "replace"))
    status, _headers, item_body = http_request(port, "GET", f"/admin/knowledge/item/{knowledge['item_id']}", headers=auth_headers)
    report.check("7 恢复后", "知识条目可查看", status == 200 and KNOWLEDGE_TITLE in item_body.decode("utf-8", "replace"))

    restored_models = EnterpriseModels(restored / "app.db", None).read(TENANT)
    report.check("7 恢复后", "企业模型配置可读（映射正确）",
                 bool(restored_models) and restored_models.get("model") == "t4-drill-model"
                 and restored_models.get("base_url") == "https://api.example.com/v1")

    job2_status, job2_error = poll_kb2_status(restored / "app.db", JOB2_FILE)
    report.check("7 恢复后", "待处理作业在恢复后继续处理完成", job2_status in ("extracted", "registered"),
                 f"status={job2_status} error={job2_error[:60]}")

    status, _headers, body = http_request(
        port, "POST", "/api/v1/buyer/conversations",
        json_body({"site_key": site_key, "locale": "en"}), buyer_headers)
    new_conversation = json.loads(body.decode("utf-8")).get("conversation_id", "") if status == 201 else ""
    new_token = json.loads(body.decode("utf-8")).get("access_token", "") if status == 201 else ""
    status2, _headers, body2 = http_request(
        port, "POST", f"/api/v1/buyer/conversations/{new_conversation}/rfqs",
        json_body({
            "contact": {"contact_name": "Drill Buyer 2", "company": "Drill Co", "contact_email": "drill-buyer2@example.invalid",
                        "contact_phone": "+49 30 555 0209", "quantity": "2", "region": "France"},
            "contact_confirmed": True, "notes": "T4 drill rfq after restore", "entry_surface": "chat",
        }),
        {**buyer_headers, "Authorization": f"Bearer {new_token}", "Idempotency-Key": "t4-drill-rfq-2"},
    )
    new_rfq = json.loads(body2.decode("utf-8")).get("rfq_id", "") if status2 in (200, 201) else ""
    report.check("7 恢复后", "恢复后可创建新会话与新 RFQ（业务继续）",
                 status == 201 and status2 in (200, 201) and bool(new_rfq), f"conversation={new_conversation} rfq={new_rfq}")
    server.stop()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--keep-workdir", action="store_true", help="保留临时工作目录供检查")
    parser.add_argument("--out", type=Path, default=None, help="报告 JSON 输出路径（可选）")
    parser.add_argument("--port", type=int, default=0, help="固定端口（默认自动选择空闲端口）")
    parser.add_argument("--code-root", type=Path, default=ROOT, help="被测代码根目录（默认本脚本所在仓库）")
    args = parser.parse_args()

    code_root = args.code_root.resolve()
    for required in ("app.py", "tools/backup_restore.py", "tools/create_admin.py"):
        if not (code_root / required).is_file():
            print(f"[用法错误] 代码根缺少 {required}: {code_root}")
            raise SystemExit(2)

    report = Report()
    workdir = Path(tempfile.mkdtemp(prefix="b2b-t4-drill-"))
    report.add("0 边界", "临时工作目录", "INFO", str(workdir))
    report.add("0 边界", "被测代码根", "INFO", str(code_root))
    report.add("0 边界", "离线说明", "INFO", "清除模型/渠道/Dify 环境变量；AI_MODE 缺省=本地规则；不读取仓库 data/、不连外部服务")
    port = args.port or free_port()
    failure: str | None = None
    saved_upload_env = os.environ.get("B2B_UPLOAD_DIR")
    try:
        run_drill(report, workdir, code_root, port)
    except Exception as error:  # 演练中断也给出可读报告
        import traceback
        traceback.print_exc()
        failure = f"{type(error).__name__}: {error}"
        report.add("X 中断", "演练中断", "FAIL", failure[:220])
    finally:
        if saved_upload_env is None:
            os.environ.pop("B2B_UPLOAD_DIR", None)
        else:
            os.environ["B2B_UPLOAD_DIR"] = saved_upload_env

    counts = report.counts()
    print("\n===== T4 演练摘要 =====")
    for status in ("PASS", "FAIL", "REVIEW", "INFO", "SKIP"):
        print(f"{status}: {counts[status]}")
    if failure:
        print(f"中断原因: {failure}")
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(report.payload(), ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"报告: {args.out}")
    if not args.keep_workdir:
        shutil.rmtree(workdir, ignore_errors=True)
    else:
        print(f"工作目录保留: {workdir}")
    if counts["FAIL"] or counts["REVIEW"]:
        raise SystemExit(1)
    raise SystemExit(0)


if __name__ == "__main__":
    main()
