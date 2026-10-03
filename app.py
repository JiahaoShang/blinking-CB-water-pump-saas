#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Small, dependency-free MVP application for product approval and publishing.

The first slice deliberately keeps the business modules in one process while
persisting all state in SQLite. It is intended for local development and demo
data, not production authentication or file storage.
"""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import mimetypes
import os
import re
import secrets
import sqlite3
import tempfile
import hmac
from datetime import datetime, timezone
from email.parser import BytesParser
from email.policy import default
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse


ROOT = Path(__file__).resolve().parent
DEFAULT_DB = ROOT / "data" / "app.db"
UPLOAD_DIR = ROOT / "data" / "uploads"
DEMO_SEED_FILE = ROOT / "fixtures" / "demo_seed.json"
LOWARA_SEED_FILE = ROOT / "fixtures" / "lowara_demo_seed.json"
REQUIRED_FIELDS = ("flow_range", "head_range", "media", "material")
MATCH_REQUIRED_FIELDS = ("flow", "head", "media")
REQUIREMENT_LABELS = {
    "category": "品类",
    "use_case": "用途",
    "flow": "流量",
    "head": "扬程",
    "media": "介质",
    "temperature": "介质温度",
    "quantity": "数量",
    "region": "目的地",
    "delivery": "期望交期",
}
FIELD_LABELS = {
    "flow_range": "流量范围",
    "head_range": "扬程范围",
    "media": "介质适用条件",
    "material": "材质",
    "power": "功率",
    "temperature": "介质温度",
    "performance_points": "性能曲线目录点",
}


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def esc(value: object) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def normalize_locale(value: str | None) -> str:
    return "zh" if (value or "").lower().startswith("zh") else "en"


def requirement_capture_reply(locale: str, missing: list[str]) -> str:
    if normalize_locale(locale) == "zh":
        if missing:
            return "我已记录你的需求。为了比较产品，请确认：" + "、".join(missing) + "。"
        return "我已提取出以上工况。请先检查并确认需求摘要，再开始匹配。"
    if missing:
        return "I captured your message. To compare products, please confirm: " + ", ".join(missing) + "."
    return "I extracted the conditions above. Please check and confirm the summary before matching."


def slugify(model: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", model.lower()).strip("-")
    return slug or secrets.token_hex(4)


def parse_float(value: str | None) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except ValueError:
        return None


class Database:
    def __init__(self, path: str | os.PathLike[str]):
        self.path = str(path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.init_schema()

    def init_schema(self) -> None:
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS tenants (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS users (
                id TEXT PRIMARY KEY,
                tenant_id TEXT NOT NULL REFERENCES tenants(id),
                email TEXT NOT NULL UNIQUE,
                display_name TEXT NOT NULL,
                role TEXT NOT NULL,
                password_hash TEXT NOT NULL,
                active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS sessions (
                id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL REFERENCES users(id),
                tenant_id TEXT NOT NULL REFERENCES tenants(id),
                expires_at TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS products (
                id TEXT PRIMARY KEY,
                tenant_id TEXT NOT NULL REFERENCES tenants(id),
                model TEXT NOT NULL,
                name TEXT NOT NULL,
                use_case TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'draft',
                slug TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE (tenant_id, model),
                UNIQUE (tenant_id, slug)
            );
            CREATE TABLE IF NOT EXISTS source_documents (
                id TEXT PRIMARY KEY,
                tenant_id TEXT NOT NULL REFERENCES tenants(id),
                product_id TEXT NOT NULL REFERENCES products(id),
                filename TEXT NOT NULL,
                file_type TEXT NOT NULL,
                source_location TEXT NOT NULL,
                sha256 TEXT,
                storage_path TEXT,
                processing_status TEXT NOT NULL DEFAULT 'registered',
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS product_fields (
                id TEXT PRIMARY KEY,
                tenant_id TEXT NOT NULL REFERENCES tenants(id),
                product_id TEXT NOT NULL REFERENCES products(id),
                field_key TEXT NOT NULL,
                label TEXT NOT NULL,
                value TEXT NOT NULL DEFAULT '',
                unit TEXT NOT NULL DEFAULT '',
                source_document_id TEXT REFERENCES source_documents(id),
                source_location TEXT NOT NULL DEFAULT '',
                state TEXT NOT NULL DEFAULT 'candidate',
                approved_by TEXT,
                approved_at TEXT,
                updated_at TEXT NOT NULL,
                UNIQUE (product_id, field_key)
            );
            CREATE TABLE IF NOT EXISTS product_versions (
                id TEXT PRIMARY KEY,
                tenant_id TEXT NOT NULL REFERENCES tenants(id),
                product_id TEXT NOT NULL REFERENCES products(id),
                version INTEGER NOT NULL,
                snapshot_json TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'draft',
                created_by TEXT NOT NULL,
                created_at TEXT NOT NULL,
                published_at TEXT,
                UNIQUE (product_id, version)
            );
            CREATE TABLE IF NOT EXISTS audit_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                tenant_id TEXT NOT NULL REFERENCES tenants(id),
                entity_type TEXT NOT NULL,
                entity_id TEXT NOT NULL,
                action TEXT NOT NULL,
                actor TEXT NOT NULL,
                details TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS conversations (
                id TEXT PRIMARY KEY,
                tenant_id TEXT NOT NULL REFERENCES tenants(id),
                source TEXT NOT NULL DEFAULT 'web',
                locale TEXT NOT NULL DEFAULT 'en',
                status TEXT NOT NULL DEFAULT 'active',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS messages (
                id TEXT PRIMARY KEY,
                tenant_id TEXT NOT NULL REFERENCES tenants(id),
                conversation_id TEXT NOT NULL REFERENCES conversations(id),
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                source TEXT NOT NULL DEFAULT 'buyer_web',
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS requirement_revisions (
                id TEXT PRIMARY KEY,
                tenant_id TEXT NOT NULL REFERENCES tenants(id),
                conversation_id TEXT NOT NULL REFERENCES conversations(id),
                revision INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'candidate',
                values_json TEXT NOT NULL,
                missing_json TEXT NOT NULL DEFAULT '[]',
                created_by TEXT NOT NULL,
                created_at TEXT NOT NULL,
                confirmed_at TEXT,
                UNIQUE (conversation_id, revision)
            );
            CREATE TABLE IF NOT EXISTS recommendations (
                id TEXT PRIMARY KEY,
                tenant_id TEXT NOT NULL REFERENCES tenants(id),
                conversation_id TEXT NOT NULL REFERENCES conversations(id),
                requirement_revision_id TEXT NOT NULL REFERENCES requirement_revisions(id),
                status TEXT NOT NULL,
                rule_version TEXT NOT NULL,
                results_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                invalidated_at TEXT
            );
            CREATE TABLE IF NOT EXISTS answers (
                id TEXT PRIMARY KEY,
                tenant_id TEXT NOT NULL REFERENCES tenants(id),
                conversation_id TEXT NOT NULL REFERENCES conversations(id),
                question_message_id TEXT NOT NULL REFERENCES messages(id),
                product_id TEXT REFERENCES products(id),
                product_version_id TEXT REFERENCES product_versions(id),
                answer_status TEXT NOT NULL,
                answer_text TEXT NOT NULL,
                citations_json TEXT NOT NULL DEFAULT '[]',
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS human_tasks (
                id TEXT PRIMARY KEY,
                tenant_id TEXT NOT NULL REFERENCES tenants(id),
                conversation_id TEXT NOT NULL REFERENCES conversations(id),
                question_message_id TEXT NOT NULL REFERENCES messages(id),
                requirement_revision_id TEXT REFERENCES requirement_revisions(id),
                recommendation_id TEXT REFERENCES recommendations(id),
                product_id TEXT REFERENCES products(id),
                product_version_id TEXT REFERENCES product_versions(id),
                question TEXT NOT NULL,
                context_json TEXT NOT NULL DEFAULT '{}',
                reason TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                assigned_to TEXT,
                response TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                resolved_at TEXT
            );
            CREATE TABLE IF NOT EXISTS rfqs (
                id TEXT PRIMARY KEY,
                tenant_id TEXT NOT NULL REFERENCES tenants(id),
                conversation_id TEXT NOT NULL REFERENCES conversations(id),
                requirement_revision_id TEXT REFERENCES requirement_revisions(id),
                recommendation_id TEXT REFERENCES recommendations(id),
                product_id TEXT REFERENCES products(id),
                product_version_id TEXT REFERENCES product_versions(id),
                submission_key TEXT NOT NULL,
                contact_name TEXT NOT NULL DEFAULT '',
                company TEXT NOT NULL DEFAULT '',
                contact_email TEXT NOT NULL DEFAULT '',
                contact_phone TEXT NOT NULL DEFAULT '',
                quantity TEXT NOT NULL DEFAULT '',
                destination TEXT NOT NULL DEFAULT '',
                requested_delivery TEXT NOT NULL DEFAULT '',
                notes TEXT NOT NULL DEFAULT '',
                missing_json TEXT NOT NULL DEFAULT '[]',
                status TEXT NOT NULL DEFAULT 'needs_info',
                snapshot_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE (tenant_id, submission_key)
            );
            CREATE TABLE IF NOT EXISTS leads (
                id TEXT PRIMARY KEY,
                tenant_id TEXT NOT NULL REFERENCES tenants(id),
                rfq_id TEXT NOT NULL REFERENCES rfqs(id),
                conversation_id TEXT NOT NULL REFERENCES conversations(id),
                stage TEXT NOT NULL DEFAULT 'new',
                owner TEXT,
                next_follow_up TEXT,
                note TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE (tenant_id, rfq_id)
            );
            """
        )
        self.conn.execute(
            "INSERT OR IGNORE INTO tenants(id, name, created_at) VALUES (?, ?, ?)",
            ("demo-tenant", "示例水泵企业（演示）", now_iso()),
        )
        self.seed_demo_users()
        self.conn.commit()

    def _audit(self, tenant_id: str, entity_type: str, entity_id: str, action: str, actor: str, details: str = "") -> None:
        self.conn.execute(
            "INSERT INTO audit_log(tenant_id, entity_type, entity_id, action, actor, details, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (tenant_id, entity_type, entity_id, action, actor, details, now_iso()),
        )

    def ensure_tenant(self, tenant_id: str, name: str | None = None) -> None:
        """Create a tenant only for local fixtures; production auth owns this boundary."""
        self.conn.execute(
            "INSERT OR IGNORE INTO tenants(id, name, created_at) VALUES (?, ?, ?)",
            (tenant_id, name or tenant_id, now_iso()),
        )
        self.conn.commit()

    @staticmethod
    def password_hash(password: str, salt: bytes | None = None) -> str:
        salt = salt or secrets.token_bytes(16)
        digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 120_000)
        return f"pbkdf2_sha256$120000${salt.hex()}${digest.hex()}"

    @staticmethod
    def verify_password(password: str, encoded: str) -> bool:
        try:
            algorithm, rounds, salt_hex, digest_hex = encoded.split("$", 3)
            if algorithm != "pbkdf2_sha256":
                return False
            digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), bytes.fromhex(salt_hex), int(rounds))
            return hmac.compare_digest(digest.hex(), digest_hex)
        except (ValueError, TypeError):
            return False

    def seed_demo_users(self) -> None:
        users = [
            ("demo-admin@example.invalid", "演示管理员", "admin", "demo-admin"),
            ("demo-sales@example.invalid", "演示销售", "sales", "demo-sales"),
            ("demo-engineer@example.invalid", "演示工程师", "engineer", "demo-engineer"),
        ]
        for email, display_name, role, password in users:
            self.conn.execute(
                "INSERT OR IGNORE INTO users(id, tenant_id, email, display_name, role, password_hash, active, created_at) VALUES (?, 'demo-tenant', ?, ?, ?, ?, 1, ?)",
                (secrets.token_hex(12), email, display_name, role, self.password_hash(password), now_iso()),
            )

    def authenticate_user(self, email: str, password: str) -> sqlite3.Row | None:
        user = self.conn.execute("SELECT * FROM users WHERE lower(email)=lower(?) AND active=1", (email.strip(),)).fetchone()
        if not user or not self.verify_password(password, user["password_hash"]):
            return None
        return user

    def list_users(self, tenant_id: str) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT id, tenant_id, email, display_name, role, active, created_at "
            "FROM users WHERE tenant_id=? ORDER BY active DESC, created_at, email",
            (tenant_id,),
        ).fetchall()

    def create_user(
        self,
        tenant_id: str,
        email: str,
        display_name: str,
        role: str,
        password: str,
        actor: str,
    ) -> str:
        email = email.strip().lower()
        display_name = display_name.strip()
        if not email or "@" not in email:
            raise ValueError("请输入有效邮箱")
        if not display_name:
            raise ValueError("姓名不能为空")
        if role not in {"admin", "sales", "engineer"}:
            raise ValueError("不支持的账号角色")
        if len(password) < 8:
            raise ValueError("密码至少需要 8 位")
        user_id = secrets.token_hex(12)
        try:
            self.conn.execute(
                "INSERT INTO users(id, tenant_id, email, display_name, role, password_hash, active, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, 1, ?)",
                (user_id, tenant_id, email, display_name, role, self.password_hash(password), now_iso()),
            )
        except sqlite3.IntegrityError as exc:
            raise ValueError("该邮箱已经存在") from exc
        self._audit(tenant_id, "user", user_id, "created", actor, f"email={email};role={role}")
        self.conn.commit()
        return user_id

    def set_user_active(self, tenant_id: str, user_id: str, active: bool, actor: str) -> None:
        user = self.conn.execute(
            "SELECT id, email, active FROM users WHERE tenant_id=? AND id=?",
            (tenant_id, user_id),
        ).fetchone()
        if not user:
            raise ValueError("账号不存在")
        if bool(user["active"]) == active:
            return
        self.conn.execute(
            "UPDATE users SET active=? WHERE tenant_id=? AND id=?",
            (1 if active else 0, tenant_id, user_id),
        )
        if not active:
            self.conn.execute("DELETE FROM sessions WHERE tenant_id=? AND user_id=?", (tenant_id, user_id))
        self._audit(tenant_id, "user", user_id, "activated" if active else "deactivated", actor, user["email"])
        self.conn.commit()

    def create_session(self, user: sqlite3.Row, hours: int = 12) -> str:
        session_id = secrets.token_urlsafe(32)
        expires_at = datetime.fromtimestamp(datetime.now(timezone.utc).timestamp() + hours * 3600, timezone.utc).replace(microsecond=0).isoformat()
        self.conn.execute(
            "INSERT INTO sessions(id, user_id, tenant_id, expires_at, created_at) VALUES (?, ?, ?, ?, ?)",
            (session_id, user["id"], user["tenant_id"], expires_at, now_iso()),
        )
        self.conn.commit()
        return session_id

    def get_session_user(self, session_id: str | None) -> sqlite3.Row | None:
        if not session_id:
            return None
        return self.conn.execute(
            "SELECT u.* FROM sessions s JOIN users u ON u.id=s.user_id WHERE s.id=? AND s.expires_at>? AND u.active=1",
            (session_id, now_iso()),
        ).fetchone()

    def delete_session(self, session_id: str | None) -> None:
        if session_id:
            self.conn.execute("DELETE FROM sessions WHERE id=?", (session_id,))
            self.conn.commit()

    def list_products(self, tenant_id: str) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT p.*, (SELECT COUNT(*) FROM source_documents s WHERE s.product_id=p.id) AS source_count, "
            "(SELECT COUNT(*) FROM product_fields f WHERE f.product_id=p.id AND f.state='approved') AS approved_count "
            "FROM products p WHERE p.tenant_id=? ORDER BY p.updated_at DESC",
            (tenant_id,),
        ).fetchall()

    def get_product(self, tenant_id: str, product_id: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM products WHERE tenant_id=? AND id=?", (tenant_id, product_id)
        ).fetchone()

    def get_product_by_slug(self, tenant_id: str, slug: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM products WHERE tenant_id=? AND slug=?", (tenant_id, slug)
        ).fetchone()

    def get_sources(self, tenant_id: str, product_id: str) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM source_documents WHERE tenant_id=? AND product_id=? ORDER BY created_at DESC",
            (tenant_id, product_id),
        ).fetchall()

    def get_fields(self, tenant_id: str, product_id: str) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT f.*, s.filename AS source_filename FROM product_fields f "
            "LEFT JOIN source_documents s ON s.id=f.source_document_id "
            "WHERE f.tenant_id=? AND f.product_id=? ORDER BY f.field_key",
            (tenant_id, product_id),
        ).fetchall()

    def get_published_version(self, tenant_id: str, product_id: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM product_versions WHERE tenant_id=? AND product_id=? AND status='published' "
            "ORDER BY version DESC LIMIT 1",
            (tenant_id, product_id),
        ).fetchone()

    def create_conversation(self, tenant_id: str, source: str = "web", locale: str = "en") -> str:
        self.ensure_tenant(tenant_id)
        conversation_id = secrets.token_hex(12)
        timestamp = now_iso()
        self.conn.execute(
            "INSERT INTO conversations(id, tenant_id, source, locale, status, created_at, updated_at) VALUES (?, ?, ?, ?, 'active', ?, ?)",
            (conversation_id, tenant_id, source, locale, timestamp, timestamp),
        )
        self._audit(tenant_id, "conversation", conversation_id, "created", "buyer-web", source)
        self.conn.commit()
        return conversation_id

    def get_conversation(self, tenant_id: str, conversation_id: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM conversations WHERE tenant_id=? AND id=?", (tenant_id, conversation_id)
        ).fetchone()

    def set_conversation_locale(self, tenant_id: str, conversation_id: str, locale: str) -> None:
        normalized = normalize_locale(locale)
        self.conn.execute(
            "UPDATE conversations SET locale=?, updated_at=? WHERE tenant_id=? AND id=?",
            (normalized, now_iso(), tenant_id, conversation_id),
        )
        self.conn.commit()

    def list_messages(self, tenant_id: str, conversation_id: str) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM messages WHERE tenant_id=? AND conversation_id=? ORDER BY created_at, id",
            (tenant_id, conversation_id),
        ).fetchall()

    def save_message(self, tenant_id: str, conversation_id: str, role: str, content: str, source: str = "buyer-web") -> str:
        if not self.get_conversation(tenant_id, conversation_id):
            raise ValueError("会话不存在")
        content = content.strip()
        if not content:
            raise ValueError("消息不能为空")
        message_id = secrets.token_hex(12)
        self.conn.execute(
            "INSERT INTO messages(id, tenant_id, conversation_id, role, content, source, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (message_id, tenant_id, conversation_id, role, content, source, now_iso()),
        )
        self.conn.execute("UPDATE conversations SET updated_at=? WHERE tenant_id=? AND id=?", (now_iso(), tenant_id, conversation_id))
        self.conn.commit()
        return message_id

    def get_revisions(self, tenant_id: str, conversation_id: str) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM requirement_revisions WHERE tenant_id=? AND conversation_id=? ORDER BY revision",
            (tenant_id, conversation_id),
        ).fetchall()

    def get_latest_revision(self, tenant_id: str, conversation_id: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM requirement_revisions WHERE tenant_id=? AND conversation_id=? ORDER BY revision DESC LIMIT 1",
            (tenant_id, conversation_id),
        ).fetchone()

    def create_requirement_revision(
        self,
        tenant_id: str,
        conversation_id: str,
        values: dict[str, dict[str, str]],
        status: str = "candidate",
        created_by: str = "buyer-web",
    ) -> str:
        if not self.get_conversation(tenant_id, conversation_id):
            raise ValueError("会话不存在")
        previous = self.get_latest_revision(tenant_id, conversation_id)
        revision = (previous["revision"] if previous else 0) + 1
        missing = [key for key in MATCH_REQUIRED_FIELDS if not values.get(key, {}).get("value", "").strip()]
        revision_id = secrets.token_hex(12)
        timestamp = now_iso()
        self.conn.execute(
            "INSERT INTO requirement_revisions(id, tenant_id, conversation_id, revision, status, values_json, missing_json, created_by, created_at, confirmed_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (revision_id, tenant_id, conversation_id, revision, status, json.dumps(values, ensure_ascii=False), json.dumps(missing, ensure_ascii=False), created_by, timestamp, timestamp if status == "confirmed" else None),
        )
        self.conn.execute("UPDATE conversations SET updated_at=? WHERE tenant_id=? AND id=?", (timestamp, tenant_id, conversation_id))
        self._audit(tenant_id, "requirement_revision", revision_id, "confirmed" if status == "confirmed" else "candidate_saved", created_by, f"conversation={conversation_id};revision={revision}")
        self.conn.commit()
        return revision_id

    def get_recommendations(self, tenant_id: str, conversation_id: str) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM recommendations WHERE tenant_id=? AND conversation_id=? ORDER BY created_at DESC",
            (tenant_id, conversation_id),
        ).fetchall()

    def save_recommendation(
        self,
        tenant_id: str,
        conversation_id: str,
        requirement_revision_id: str,
        status: str,
        results: list[dict[str, object]],
        rule_version: str = "pump-demo-v1",
    ) -> str:
        self.conn.execute(
            "UPDATE recommendations SET status='invalidated', invalidated_at=? WHERE tenant_id=? AND conversation_id=? AND status <> 'invalidated'",
            (now_iso(), tenant_id, conversation_id),
        )
        recommendation_id = secrets.token_hex(12)
        self.conn.execute(
            "INSERT INTO recommendations(id, tenant_id, conversation_id, requirement_revision_id, status, rule_version, results_json, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (recommendation_id, tenant_id, conversation_id, requirement_revision_id, status, rule_version, json.dumps(results, ensure_ascii=False), now_iso()),
        )
        self._audit(tenant_id, "recommendation", recommendation_id, "generated", "matching-rule", f"status={status};revision={requirement_revision_id}")
        self.conn.commit()
        return recommendation_id

    def latest_active_recommendation(self, tenant_id: str, conversation_id: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM recommendations WHERE tenant_id=? AND conversation_id=? AND status <> 'invalidated' ORDER BY created_at DESC LIMIT 1",
            (tenant_id, conversation_id),
        ).fetchone()

    def save_answer(
        self,
        tenant_id: str,
        conversation_id: str,
        question_message_id: str,
        product_id: str | None,
        product_version_id: str | None,
        answer_status: str,
        answer_text: str,
        citations: list[dict[str, str]],
    ) -> str:
        answer_id = secrets.token_hex(12)
        self.conn.execute(
            "INSERT INTO answers(id, tenant_id, conversation_id, question_message_id, product_id, product_version_id, answer_status, answer_text, citations_json, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (answer_id, tenant_id, conversation_id, question_message_id, product_id, product_version_id, answer_status, answer_text, json.dumps(citations, ensure_ascii=False), now_iso()),
        )
        self.conn.commit()
        return answer_id

    def list_answers(self, tenant_id: str, conversation_id: str) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT a.*, p.model FROM answers a LEFT JOIN products p ON p.id=a.product_id "
            "WHERE a.tenant_id=? AND a.conversation_id=? ORDER BY a.created_at",
            (tenant_id, conversation_id),
        ).fetchall()

    def get_answer_for_question(self, tenant_id: str, conversation_id: str, question_message_id: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT a.*, p.model FROM answers a LEFT JOIN products p ON p.id=a.product_id "
            "WHERE a.tenant_id=? AND a.conversation_id=? AND a.question_message_id=?",
            (tenant_id, conversation_id, question_message_id),
        ).fetchone()

    def get_message(self, tenant_id: str, conversation_id: str, message_id: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM messages WHERE tenant_id=? AND conversation_id=? AND id=?",
            (tenant_id, conversation_id, message_id),
        ).fetchone()

    def create_human_task(
        self,
        tenant_id: str,
        conversation_id: str,
        question_message_id: str,
        question: str,
        reason: str,
        context: dict[str, object],
        product_id: str | None = None,
        product_version_id: str | None = None,
        requirement_revision_id: str | None = None,
        recommendation_id: str | None = None,
    ) -> str:
        task_id = secrets.token_hex(12)
        timestamp = now_iso()
        self.conn.execute(
            "INSERT INTO human_tasks(id, tenant_id, conversation_id, question_message_id, requirement_revision_id, recommendation_id, product_id, product_version_id, question, context_json, reason, status, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)",
            (task_id, tenant_id, conversation_id, question_message_id, requirement_revision_id, recommendation_id, product_id, product_version_id, question, json.dumps(context, ensure_ascii=False), reason, timestamp, timestamp),
        )
        self._audit(tenant_id, "human_task", task_id, "created", "rule-based-answer", reason)
        self.conn.commit()
        return task_id

    def list_human_tasks(self, tenant_id: str, status: str | None = None) -> list[sqlite3.Row]:
        query = (
            "SELECT t.*, p.model FROM human_tasks t LEFT JOIN products p ON p.id=t.product_id "
            "WHERE t.tenant_id=?"
        )
        args: list[str] = [tenant_id]
        if status:
            query += " AND t.status=?"
            args.append(status)
        query += " ORDER BY CASE t.status WHEN 'pending' THEN 0 WHEN 'in_progress' THEN 1 ELSE 2 END, t.created_at DESC"
        return self.conn.execute(query, args).fetchall()

    def get_human_task(self, tenant_id: str, task_id: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT t.*, p.model FROM human_tasks t LEFT JOIN products p ON p.id=t.product_id WHERE t.tenant_id=? AND t.id=?",
            (tenant_id, task_id),
        ).fetchone()

    def get_human_task_for_question(self, tenant_id: str, conversation_id: str, question_message_id: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT t.*, p.model FROM human_tasks t LEFT JOIN products p ON p.id=t.product_id "
            "WHERE t.tenant_id=? AND t.conversation_id=? AND t.question_message_id=? "
            "ORDER BY t.created_at DESC LIMIT 1",
            (tenant_id, conversation_id, question_message_id),
        ).fetchone()

    def resolve_human_task(self, tenant_id: str, task_id: str, response: str, actor: str = "demo-engineer") -> None:
        task = self.get_human_task(tenant_id, task_id)
        if not task:
            raise ValueError("人工任务不存在")
        response = response.strip()
        if not response:
            raise ValueError("人工回复不能为空")
        timestamp = now_iso()
        self.conn.execute(
            "UPDATE human_tasks SET status='resolved', response=?, assigned_to=?, updated_at=?, resolved_at=? WHERE tenant_id=? AND id=?",
            (response, actor, timestamp, timestamp, tenant_id, task_id),
        )
        self.save_message(tenant_id, task["conversation_id"], "assistant", f"工程师回复：{response}", "human-engineer")
        self._audit(tenant_id, "human_task", task_id, "resolved", actor)
        self.conn.commit()

    def latest_confirmed_revision(self, tenant_id: str, conversation_id: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM requirement_revisions WHERE tenant_id=? AND conversation_id=? AND status='confirmed' ORDER BY revision DESC LIMIT 1",
            (tenant_id, conversation_id),
        ).fetchone()

    def get_rfq_by_submission_key(self, tenant_id: str, submission_key: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM rfqs WHERE tenant_id=? AND submission_key=?", (tenant_id, submission_key)
        ).fetchone()

    def get_rfq(self, tenant_id: str, rfq_id: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT r.*, p.model FROM rfqs r LEFT JOIN products p ON p.id=r.product_id WHERE r.tenant_id=? AND r.id=?",
            (tenant_id, rfq_id),
        ).fetchone()

    def list_rfqs(self, tenant_id: str) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT r.*, p.model FROM rfqs r LEFT JOIN products p ON p.id=r.product_id WHERE r.tenant_id=? ORDER BY r.created_at DESC",
            (tenant_id,),
        ).fetchall()

    def create_rfq(
        self,
        tenant_id: str,
        conversation_id: str,
        submission_key: str,
        contact: dict[str, str],
        notes: str,
        selected_product_id: str | None = None,
    ) -> tuple[str, bool]:
        existing = self.get_rfq_by_submission_key(tenant_id, submission_key)
        if existing:
            return existing["id"], False
        conversation = self.get_conversation(tenant_id, conversation_id)
        if not conversation:
            raise ValueError("会话不存在")
        revision = self.latest_confirmed_revision(tenant_id, conversation_id)
        recommendation = self.latest_active_recommendation(tenant_id, conversation_id)
        if not revision:
            raise ValueError("请先确认需求摘要，再提交询价")
        revision_values = json.loads(revision["values_json"])
        recommendation_results = json.loads(recommendation["results_json"]) if recommendation else []
        selected_result = None
        if selected_product_id:
            selected_result = next((result for result in recommendation_results if result.get("product_id") == selected_product_id), None)
        if not selected_result:
            selected_result = next((result for result in recommendation_results if result.get("status") in {"candidate", "needs_confirmation"}), None)
        product_id = selected_result.get("product_id") if selected_result else None
        product_version_id = selected_result.get("product_version_id") if selected_result else None
        missing = []
        for key, label in (("quantity", "数量"), ("region", "目的地"), ("delivery", "期望交期"), ("contact_email", "联系人邮箱")):
            value = contact.get(key, "")
            if not value.strip():
                missing.append(label)
        status = "submitted" if not missing else "needs_info"
        snapshot = {
            "requirement_revision_id": revision["id"],
            "requirements": revision_values,
            "recommendation_id": recommendation["id"] if recommendation else None,
            "recommendation": selected_result,
            "submitted_contact": {"name": contact.get("contact_name", ""), "company": contact.get("company", ""), "email": contact.get("contact_email", ""), "phone": contact.get("contact_phone", "")},
            "submitted_at": now_iso(),
        }
        rfq_id = secrets.token_hex(12)
        timestamp = now_iso()
        self.conn.execute(
            "INSERT INTO rfqs(id, tenant_id, conversation_id, requirement_revision_id, recommendation_id, product_id, product_version_id, submission_key, contact_name, company, contact_email, contact_phone, quantity, destination, requested_delivery, notes, missing_json, status, snapshot_json, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (rfq_id, tenant_id, conversation_id, revision["id"], recommendation["id"] if recommendation else None, product_id, product_version_id, submission_key, contact.get("contact_name", "").strip(), contact.get("company", "").strip(), contact.get("contact_email", "").strip(), contact.get("contact_phone", "").strip(), contact.get("quantity", "").strip(), contact.get("region", "").strip(), contact.get("delivery", "").strip(), notes.strip(), json.dumps(missing, ensure_ascii=False), status, json.dumps(snapshot, ensure_ascii=False), timestamp),
        )
        lead_id = secrets.token_hex(12)
        self.conn.execute(
            "INSERT INTO leads(id, tenant_id, rfq_id, conversation_id, stage, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (lead_id, tenant_id, rfq_id, conversation_id, "new" if status == "submitted" else "needs_info", timestamp, timestamp),
        )
        self._audit(tenant_id, "rfq", rfq_id, "submitted", "buyer-web", f"status={status};lead={lead_id}")
        self.conn.commit()
        return rfq_id, True

    def get_lead_for_rfq(self, tenant_id: str, rfq_id: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM leads WHERE tenant_id=? AND rfq_id=?", (tenant_id, rfq_id)
        ).fetchone()

    def list_sales_records(self, tenant_id: str) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT r.*, p.model, l.id AS lead_id, l.stage, l.owner, l.next_follow_up, l.note, l.updated_at AS lead_updated_at "
            "FROM rfqs r LEFT JOIN products p ON p.id=r.product_id LEFT JOIN leads l ON l.rfq_id=r.id AND l.tenant_id=r.tenant_id "
            "WHERE r.tenant_id=? ORDER BY CASE l.stage WHEN 'new' THEN 0 WHEN 'needs_info' THEN 1 WHEN 'technical_review' THEN 2 WHEN 'quote_ready' THEN 3 WHEN 'paused' THEN 4 ELSE 5 END, l.next_follow_up, r.created_at DESC",
            (tenant_id,),
        ).fetchall()

    def get_admin_summary(self, tenant_id: str) -> dict[str, int]:
        """Return small, tenant-scoped counters for the management overview."""
        product_counts = self.conn.execute(
            "SELECT "
            "COUNT(*) AS total, "
            "SUM(CASE WHEN status='published' THEN 1 ELSE 0 END) AS published, "
            "SUM(CASE WHEN status IN ('pending_review', 'draft') THEN 1 ELSE 0 END) AS needs_review "
            "FROM products WHERE tenant_id=?",
            (tenant_id,),
        ).fetchone()
        task_counts = self.conn.execute(
            "SELECT "
            "SUM(CASE WHEN status='pending' THEN 1 ELSE 0 END) AS pending, "
            "COUNT(*) AS total "
            "FROM human_tasks WHERE tenant_id=?",
            (tenant_id,),
        ).fetchone()
        rfq_counts = self.conn.execute(
            "SELECT "
            "COUNT(*) AS total, "
            "SUM(CASE WHEN status='needs_info' THEN 1 ELSE 0 END) AS needs_info "
            "FROM rfqs WHERE tenant_id=?",
            (tenant_id,),
        ).fetchone()
        today = datetime.now(timezone.utc).date().isoformat()
        lead_counts = self.conn.execute(
            "SELECT "
            "SUM(CASE WHEN stage NOT IN ('closed', 'paused') THEN 1 ELSE 0 END) AS active, "
            "SUM(CASE WHEN next_follow_up < ? AND stage NOT IN ('closed', 'paused') THEN 1 ELSE 0 END) AS overdue "
            "FROM leads WHERE tenant_id=?",
            (today, tenant_id),
        ).fetchone()
        return {
            "products_total": int(product_counts["total"] or 0),
            "products_published": int(product_counts["published"] or 0),
            "products_needs_review": int(product_counts["needs_review"] or 0),
            "tasks_pending": int(task_counts["pending"] or 0),
            "tasks_total": int(task_counts["total"] or 0),
            "rfqs_total": int(rfq_counts["total"] or 0),
            "rfqs_needs_info": int(rfq_counts["needs_info"] or 0),
            "leads_active": int(lead_counts["active"] or 0),
            "leads_overdue": int(lead_counts["overdue"] or 0),
        }

    def update_lead(
        self,
        tenant_id: str,
        rfq_id: str,
        stage: str,
        owner: str,
        next_follow_up: str,
        note: str,
        actor: str = "demo-sales",
    ) -> None:
        allowed = {"new", "needs_info", "technical_review", "quote_ready", "paused", "closed"}
        if stage not in allowed:
            raise ValueError("不支持的线索阶段")
        lead = self.get_lead_for_rfq(tenant_id, rfq_id)
        if not lead:
            raise ValueError("线索不存在")
        timestamp = now_iso()
        self.conn.execute(
            "UPDATE leads SET stage=?, owner=?, next_follow_up=?, note=?, updated_at=? WHERE tenant_id=? AND rfq_id=?",
            (stage, owner.strip() or None, next_follow_up.strip() or None, note.strip(), timestamp, tenant_id, rfq_id),
        )
        self._audit(tenant_id, "lead", lead["id"], "updated", actor, f"rfq={rfq_id};stage={stage};owner={owner};next_follow_up={next_follow_up}")
        self.conn.commit()

    def list_entity_audit(self, tenant_id: str, entity_ids: list[str]) -> list[sqlite3.Row]:
        if not entity_ids:
            return []
        placeholders = ",".join("?" for _ in entity_ids)
        return self.conn.execute(
            f"SELECT * FROM audit_log WHERE tenant_id=? AND entity_id IN ({placeholders}) ORDER BY created_at DESC",
            [tenant_id, *entity_ids],
        ).fetchall()

    def create_product(self, tenant_id: str, model: str, name: str, use_case: str, actor: str = "demo-admin") -> str:
        self.ensure_tenant(tenant_id)
        model = model.strip()
        name = name.strip() or model
        if not model:
            raise ValueError("型号不能为空")
        product_id = secrets.token_hex(12)
        slug = slugify(model)
        timestamp = now_iso()
        try:
            self.conn.execute(
                "INSERT INTO products(id, tenant_id, model, name, use_case, status, slug, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, 'draft', ?, ?, ?)",
                (product_id, tenant_id, model, name, use_case.strip(), slug, timestamp, timestamp),
            )
        except sqlite3.IntegrityError as exc:
            raise ValueError("该企业下已有相同型号") from exc
        self._audit(tenant_id, "product", product_id, "created", actor, model)
        self.conn.commit()
        return product_id

    def add_source(
        self,
        tenant_id: str,
        product_id: str,
        filename: str,
        file_type: str,
        source_location: str,
        content: bytes | None = None,
        actor: str = "demo-admin",
    ) -> str:
        if not self.get_product(tenant_id, product_id):
            raise ValueError("产品不存在")
        source_id = secrets.token_hex(12)
        sha256 = hashlib.sha256(content).hexdigest() if content else None
        storage_path = None
        status = "registered"
        if content:
            UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
            safe_name = re.sub(r"[^a-zA-Z0-9._-]+", "_", Path(filename).name) or "source.bin"
            target = UPLOAD_DIR / f"{source_id}-{safe_name}"
            target.write_bytes(content)
            storage_path = str(target.relative_to(ROOT))
            status = "stored"
        self.conn.execute(
            "INSERT INTO source_documents(id, tenant_id, product_id, filename, file_type, source_location, sha256, storage_path, processing_status, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (source_id, tenant_id, product_id, filename.strip(), file_type.strip() or "other", source_location.strip(), sha256, storage_path, status, now_iso()),
        )
        self._audit(tenant_id, "source_document", source_id, "registered", actor, filename)
        self.conn.commit()
        return source_id

    def save_field(
        self,
        tenant_id: str,
        product_id: str,
        field_key: str,
        value: str,
        unit: str,
        source_document_id: str | None,
        source_location: str,
        actor: str = "demo-admin",
    ) -> None:
        if field_key not in FIELD_LABELS:
            raise ValueError("不支持的字段")
        product = self.get_product(tenant_id, product_id)
        if not product:
            raise ValueError("产品不存在")
        if source_document_id:
            source = self.conn.execute(
                "SELECT id FROM source_documents WHERE id=? AND tenant_id=? AND product_id=?",
                (source_document_id, tenant_id, product_id),
            ).fetchone()
            if not source:
                raise ValueError("来源文件不属于当前产品")
        self.conn.execute(
            "INSERT INTO product_fields(id, tenant_id, product_id, field_key, label, value, unit, source_document_id, source_location, state, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'candidate', ?) "
            "ON CONFLICT(product_id, field_key) DO UPDATE SET value=excluded.value, unit=excluded.unit, source_document_id=excluded.source_document_id, source_location=excluded.source_location, state='candidate', approved_by=NULL, approved_at=NULL, updated_at=excluded.updated_at",
            (secrets.token_hex(12), tenant_id, product_id, field_key, FIELD_LABELS[field_key], value.strip(), unit.strip(), source_document_id or None, source_location.strip(), now_iso()),
        )
        self.conn.execute("UPDATE products SET status='pending_review', updated_at=? WHERE id=? AND tenant_id=?", (now_iso(), product_id, tenant_id))
        self._audit(tenant_id, "product_field", product_id, "updated_candidate", actor, field_key)
        self.conn.commit()

    def approve_product(self, tenant_id: str, product_id: str, actor: str = "demo-admin") -> None:
        product = self.get_product(tenant_id, product_id)
        if not product:
            raise ValueError("产品不存在")
        fields = {row["field_key"]: row for row in self.get_fields(tenant_id, product_id)}
        missing = [FIELD_LABELS[key] for key in REQUIRED_FIELDS if key not in fields or not fields[key]["value"].strip() or not fields[key]["source_document_id"]]
        if missing:
            raise ValueError("必须先补齐有来源的字段：" + "、".join(missing))
        self.conn.execute(
            "UPDATE product_fields SET state='approved', approved_by=?, approved_at=? WHERE tenant_id=? AND product_id=? AND value <> ''",
            (actor, now_iso(), tenant_id, product_id),
        )
        self.conn.execute("UPDATE products SET status='approved', updated_at=? WHERE tenant_id=? AND id=?", (now_iso(), tenant_id, product_id))
        self._audit(tenant_id, "product", product_id, "approved", actor)
        self.conn.commit()

    def publish_product(self, tenant_id: str, product_id: str, actor: str = "demo-admin") -> int:
        product = self.get_product(tenant_id, product_id)
        if not product:
            raise ValueError("产品不存在")
        if product["status"] not in ("approved", "published"):
            raise ValueError("只有已核准产品才能发布")
        fields = self.get_fields(tenant_id, product_id)
        missing = [FIELD_LABELS[key] for key in REQUIRED_FIELDS if not any(f["field_key"] == key and f["state"] == "approved" and f["value"].strip() for f in fields)]
        if missing:
            raise ValueError("还有未核准字段：" + "、".join(missing))
        previous = self.get_published_version(tenant_id, product_id)
        version = (previous["version"] if previous else 0) + 1
        snapshot = {
            "product": {"model": product["model"], "name": product["name"], "use_case": product["use_case"], "slug": product["slug"]},
            "fields": [dict(field) for field in fields if field["state"] == "approved"],
            "published_at": now_iso(),
        }
        if previous:
            self.conn.execute("UPDATE product_versions SET status='withdrawn' WHERE id=?", (previous["id"],))
        self.conn.execute(
            "INSERT INTO product_versions(id, tenant_id, product_id, version, snapshot_json, status, created_by, created_at, published_at) VALUES (?, ?, ?, ?, ?, 'published', ?, ?, ?)",
            (secrets.token_hex(12), tenant_id, product_id, version, json.dumps(snapshot, ensure_ascii=False), actor, now_iso(), snapshot["published_at"]),
        )
        self.conn.execute("UPDATE products SET status='published', updated_at=? WHERE tenant_id=? AND id=?", (now_iso(), tenant_id, product_id))
        self._audit(tenant_id, "product", product_id, "published", actor, f"version={version}")
        self.conn.commit()
        return version

    def withdraw_product(self, tenant_id: str, product_id: str, actor: str = "demo-admin") -> None:
        product = self.get_product(tenant_id, product_id)
        if not product:
            raise ValueError("产品不存在")
        self.conn.execute("UPDATE product_versions SET status='withdrawn' WHERE tenant_id=? AND product_id=? AND status='published'", (tenant_id, product_id))
        self.conn.execute("UPDATE products SET status='withdrawn', updated_at=? WHERE tenant_id=? AND id=?", (now_iso(), tenant_id, product_id))
        self._audit(tenant_id, "product", product_id, "withdrawn", actor)
        self.conn.commit()

    def seed_demo_products(self, tenant_id: str, actor: str = "demo-admin") -> int:
        samples = [
            {
                "model": "CB-80A",
                "name": "CB-80A 清水离心泵",
                "use_case": "清水循环、工厂冷却水和一般供水",
                "fields": {
                    "flow_range": ("20-80", "m³/h"),
                    "head_range": ("18-42", "m"),
                    "media": ("清水及低粘度、非腐蚀性液体", ""),
                    "material": ("铸铁泵体，不锈钢叶轮", ""),
                    "power": ("15", "kW"),
                },
            },
            {
                "model": "CB-120C",
                "name": "CB-120C 耐腐蚀化工泵",
                "use_case": "化工工艺输送和含弱腐蚀介质循环",
                "fields": {
                    "flow_range": ("40-120", "m³/h"),
                    "head_range": ("28-65", "m"),
                    "media": ("弱酸碱、低浓度化工液体；需工程确认", ""),
                    "material": ("316L 不锈钢过流部件", ""),
                    "temperature": ("-10 至 90", "°C"),
                },
            },
        ]
        created = 0
        for sample in samples:
            if self.conn.execute("SELECT 1 FROM products WHERE tenant_id=? AND model=?", (tenant_id, sample["model"])).fetchone():
                continue
            product_id = self.create_product(tenant_id, sample["model"], sample["name"], sample["use_case"], actor)
            source_id = self.add_source(tenant_id, product_id, f"{sample['model']}-模拟产品手册.pdf", "PDF", "第 1 页：规格参数表", actor=actor)
            for key, (value, unit) in sample["fields"].items():
                self.save_field(tenant_id, product_id, key, value, unit, source_id, "第 1 页：规格参数表", actor)
            created += 1
        return created

    def seed_demo_workspace(self, tenant_id: str, actor: str = "demo-admin") -> dict[str, int]:
        """Load the tracked fixture into a tenant without duplicating RFQs."""
        if not DEMO_SEED_FILE.is_file():
            raise ValueError("项目缺少 fixtures/demo_seed.json")
        self.seed_demo_products(tenant_id, actor)
        for product in self.list_products(tenant_id):
            if product["model"] in {"CB-80A", "CB-120C"} and product["status"] in {"draft", "pending_review", "approved"}:
                self.approve_product(tenant_id, product["id"], actor)
                self.publish_product(tenant_id, product["id"], actor)
        fixture = json.loads(DEMO_SEED_FILE.read_text(encoding="utf-8"))
        created_rfqs = 0
        created_tasks = 0
        for inquiry in fixture.get("inquiries", []):
            submission_key = str(inquiry["key"])
            if self.get_rfq_by_submission_key(tenant_id, submission_key):
                continue
            conversation_id = self.create_conversation(tenant_id, "demo-fixture", inquiry.get("locale", "en"))
            question_message_id = self.save_message(tenant_id, conversation_id, "buyer", inquiry["message"], "demo-fixture")
            values = {
                key: {
                    **item,
                    "state": "confirmed",
                    "source": "demo-fixture",
                }
                for key, item in inquiry.get("requirements", {}).items()
            }
            revision_id = self.create_requirement_revision(tenant_id, conversation_id, values, "confirmed", "demo-fixture")
            status, results, _missing = match_products(self, tenant_id, values)
            recommendation_id = self.save_recommendation(tenant_id, conversation_id, revision_id, status, results)
            rfq_id, created = self.create_rfq(
                tenant_id,
                conversation_id,
                submission_key,
                inquiry["contact"],
                inquiry.get("notes", ""),
            )
            if not created:
                continue
            created_rfqs += 1
            self.update_lead(
                tenant_id,
                rfq_id,
                inquiry.get("stage", "new"),
                inquiry.get("owner", ""),
                inquiry.get("next_follow_up", ""),
                inquiry.get("lead_note", ""),
                actor,
            )
            if created_tasks == 0 and fixture.get("human_task"):
                human_task = fixture["human_task"]
                task_message_id = self.save_message(tenant_id, conversation_id, "buyer", human_task["question"], "demo-fixture")
                self.create_human_task(
                    tenant_id,
                    conversation_id,
                    task_message_id,
                    human_task["question"],
                    human_task["reason"],
                    {
                        "confirmed_requirements": values,
                        "recommendation_id": recommendation_id,
                        "product_model": results[0]["model"] if results else None,
                        "answer_so_far": human_task.get("answer_so_far", ""),
                    },
                    results[0]["product_id"] if results else None,
                    results[0]["product_version_id"] if results else None,
                    revision_id,
                    recommendation_id,
                )
                created_tasks += 1
        return {"products": len(self.list_products(tenant_id)), "rfqs": created_rfqs, "tasks": created_tasks}

    def seed_lowara_workspace(self, tenant_id: str, actor: str = "demo-admin") -> dict[str, int]:
        """Load the user-provided Lowara research as an internal demo product.

        Only structured facts and source metadata are tracked in Git. The source
        PDFs/CAD files remain in the user's research folder because redistribution
        permission has not been confirmed.
        """
        if not LOWARA_SEED_FILE.is_file():
            raise ValueError("项目缺少 fixtures/lowara_demo_seed.json")
        fixture = json.loads(LOWARA_SEED_FILE.read_text(encoding="utf-8"))
        created = 0
        for item in fixture.get("products", []):
            model = item["model"]
            existing = self.conn.execute(
                "SELECT id FROM products WHERE tenant_id=? AND model=?", (tenant_id, model)
            ).fetchone()
            if existing:
                continue
            product_id = self.create_product(tenant_id, model, item["name"], item["use_case"], actor)
            source_ids = []
            for source in item.get("sources", []):
                source_ids.append(
                    self.add_source(
                        tenant_id,
                        product_id,
                        source["filename"],
                        source.get("file_type", "OTHER"),
                        source["source_location"],
                        actor=actor,
                    )
                )
            if not source_ids:
                raise ValueError(f"Lowara {model} 缺少来源资料")
            for field_key, field in item.get("fields", {}).items():
                self.save_field(
                    tenant_id,
                    product_id,
                    field_key,
                    field["value"],
                    field.get("unit", ""),
                    source_ids[field.get("source_index", 0)],
                    field["source_location"],
                    actor,
                )
            self.approve_product(tenant_id, product_id, actor)
            self.publish_product(tenant_id, product_id, actor)
            created += 1
        return {"products": created}


def number_from_text(value: str) -> float | None:
    match = re.search(r"-?\d+(?:[.,]\d+)?", value or "")
    if not match:
        return None
    return parse_float(match.group(0).replace(",", "."))


def range_from_text(value: str) -> tuple[float, float] | None:
    match = re.search(r"(-?\d+(?:[.,]\d+)?)\s*(?:-|~|至|到|to)\s*(-?\d+(?:[.,]\d+)?)", value or "", flags=re.IGNORECASE)
    if not match:
        return None
    first = parse_float(match.group(1).replace(",", "."))
    second = parse_float(match.group(2).replace(",", "."))
    if first is None or second is None:
        return None
    return (min(first, second), max(first, second))


def performance_points_from_text(value: str) -> list[tuple[float, float]]:
    """Parse `flow:head` pairs recorded from a published catalogue table."""
    points = []
    for flow, head in re.findall(r"(-?\d+(?:[.,]\d+)?)\s*:\s*(-?\d+(?:[.,]\d+)?)", value or ""):
        points.append((float(flow.replace(",", ".")), float(head.replace(",", "."))))
    return sorted(points)


def curve_head_at_flow(points: list[tuple[float, float]], flow: float) -> float | None:
    if not points or flow < points[0][0] or flow > points[-1][0]:
        return None
    for (left_flow, left_head), (right_flow, right_head) in zip(points, points[1:]):
        if left_flow <= flow <= right_flow:
            if right_flow == left_flow:
                return max(left_head, right_head)
            ratio = (flow - left_flow) / (right_flow - left_flow)
            return left_head + ratio * (right_head - left_head)
    return points[-1][1]


def field_value(values: dict[str, dict[str, str]], key: str) -> str:
    return values.get(key, {}).get("value", "").strip()


def extract_requirement_values(text: str, explicit: dict[str, str], previous: dict[str, dict[str, str]] | None = None) -> dict[str, dict[str, str]]:
    """Keep buyer wording and structured candidates separate from product facts."""
    values = {key: dict(item) for key, item in (previous or {}).items()}
    for key, raw in explicit.items():
        value = (raw or "").strip()
        if key not in REQUIREMENT_LABELS or not value:
            continue
        values[key] = {
            "value": value,
            "unit": explicit.get(f"{key}_unit", ""),
            "state": "candidate",
            "source": "buyer_form",
            "label": REQUIREMENT_LABELS[key],
        }

    patterns = {
        "flow": r"(?:flow|流量)[^\d-]{0,18}(-?\d+(?:[.,]\d+)?)",
        "head": r"(?:head|扬程)[^\d-]{0,18}(-?\d+(?:[.,]\d+)?)",
        "temperature": r"(?:temperature|temp|温度)[^\d-]{0,18}(-?\d+(?:[.,]\d+)?)",
        "quantity": r"(?:quantity|qty|数量)[^\d-]{0,18}(\d+(?:[.,]\d+)?)",
    }
    for key, pattern in patterns.items():
        match = re.search(pattern, text or "", flags=re.IGNORECASE)
        if match:
            values[key] = {
                "value": match.group(1).replace(",", "."),
                "unit": {"flow": "m³/h", "head": "m", "temperature": "°C", "quantity": "units"}.get(key, ""),
                "state": "candidate",
                "source": "buyer_message",
                "label": REQUIREMENT_LABELS[key],
            }
    if re.search(r"pump|water pump|水泵|泵", text or "", flags=re.IGNORECASE):
        values.setdefault("category", {"value": "水泵", "unit": "", "state": "candidate", "source": "buyer_message", "label": REQUIREMENT_LABELS["category"]})
    if re.search(r"清水|净水|clean\s+water|fresh\s+water", text or "", flags=re.IGNORECASE):
        values["media"] = {"value": "清水", "unit": "", "state": "candidate", "source": "buyer_message", "label": REQUIREMENT_LABELS["media"]}
    elif re.search(r"酸|碱|化工|chemical|acid|alkali", text or "", flags=re.IGNORECASE):
        values["media"] = {"value": "化工介质", "unit": "", "state": "candidate", "source": "buyer_message", "label": REQUIREMENT_LABELS["media"]}
    return values


def confirm_requirement_values(values: dict[str, dict[str, str]]) -> dict[str, dict[str, str]]:
    confirmed = {}
    for key, item in values.items():
        copied = dict(item)
        copied["state"] = "confirmed" if copied.get("value", "").strip() else "unknown"
        copied["source"] = "buyer_confirmation" if copied["state"] == "confirmed" else copied.get("source", "buyer_confirmation")
        copied.setdefault("label", REQUIREMENT_LABELS.get(key, key))
        confirmed[key] = copied
    return confirmed


def match_products(db: Database, tenant_id: str, values: dict[str, dict[str, str]]) -> tuple[str, list[dict[str, object]], list[str]]:
    """Apply deterministic demo rules to approved, published product snapshots."""
    missing = [REQUIREMENT_LABELS[key] for key in MATCH_REQUIRED_FIELDS if not field_value(values, key)]
    if missing:
        return "insufficient", [], missing
    requirements = {key: number_from_text(field_value(values, key)) for key in ("flow", "head", "temperature")}
    requirement_media = field_value(values, "media").lower()
    rows = db.conn.execute(
        "SELECT p.id, p.model, p.slug, v.id AS version_id, v.version, v.snapshot_json FROM products p "
        "JOIN product_versions v ON v.product_id=p.id AND v.tenant_id=p.tenant_id AND v.status='published' "
        "WHERE p.tenant_id=? AND p.status='published' ORDER BY p.model",
        (tenant_id,),
    ).fetchall()
    results: list[dict[str, object]] = []
    for row in rows:
        snapshot = json.loads(row["snapshot_json"])
        product_fields = {item["field_key"]: item for item in snapshot.get("fields", [])}
        satisfies: list[str] = []
        conflicts: list[str] = []
        unknown: list[str] = []

        performance_points = performance_points_from_text(product_fields.get("performance_points", {}).get("value", ""))
        if performance_points:
            required_flow = requirements["flow"]
            required_head = requirements["head"]
            curve_head = curve_head_at_flow(performance_points, required_flow) if required_flow is not None else None
            if curve_head is None:
                conflicts.append("流量超出该型号目录性能点范围")
            elif required_head is None:
                unknown.append("扬程无法与目录性能曲线核对")
            elif required_head <= curve_head:
                satisfies.append(f"流量 {required_flow:g}m³/h 位于目录性能点范围")
                satisfies.append(f"扬程 {required_head:g}m 不高于该流量目录曲线值约 {curve_head:g}m")
                unknown.append("最终工作点仍需工程师按曲线和系统工况确认")
            else:
                conflicts.append(f"扬程 {required_head:g}m 高于该流量目录曲线值约 {curve_head:g}m")
        else:
            for requirement_key, product_key, label, unit in (("flow", "flow_range", "流量", "m³/h"), ("head", "head_range", "扬程", "m")):
                required_number = requirements[requirement_key]
                product_range = range_from_text(product_fields.get(product_key, {}).get("value", ""))
                if required_number is None or product_range is None:
                    unknown.append(f"{label}规则无法解析")
                elif product_range[0] <= required_number <= product_range[1]:
                    satisfies.append(f"{label} {required_number:g}{unit} 在产品范围内")
                else:
                    conflicts.append(f"{label} {required_number:g}{unit} 超出产品范围")

        product_media = product_fields.get("media", {}).get("value", "").lower()
        if "清水" in requirement_media or "water" in requirement_media:
            if "清水" in product_media or "water" in product_media:
                satisfies.append("介质为清水，产品资料标注可用")
            else:
                conflicts.append("产品资料未标注清水适用")
        elif any(token in requirement_media for token in ("酸", "碱", "化工", "chemical", "acid", "alkali")):
            if any(token in product_media for token in ("酸", "碱", "化工", "chemical", "acid", "alkali")):
                satisfies.append("介质属于化工场景，产品资料标注相关适用条件")
            else:
                conflicts.append("产品资料未标注该化工介质适用")
        else:
            unknown.append("介质类型尚未形成可解释规则")

        if requirements["temperature"] is not None:
            temperature_range = range_from_text(product_fields.get("temperature", {}).get("value", ""))
            if not temperature_range:
                unknown.append("产品资料没有可核对的介质温度范围")
            elif temperature_range[0] <= requirements["temperature"] <= temperature_range[1]:
                satisfies.append(f"介质温度 {requirements['temperature']:g}°C 在资料范围内")
            else:
                conflicts.append(f"介质温度 {requirements['temperature']:g}°C 超出资料范围")

        candidate_status = "candidate" if not conflicts and not unknown else "needs_confirmation" if not conflicts else "conflict"
        results.append({
            "product_id": row["id"],
            "model": row["model"],
            "product_version_id": row["version_id"],
            "product_version": row["version"],
            "detail_url": f"/products/{row['slug']}",
            "status": candidate_status,
            "satisfies": satisfies,
            "conflicts": conflicts,
            "unknown": unknown,
        })
    results.sort(key=lambda item: (0 if item["status"] == "candidate" else 1 if item["status"] == "needs_confirmation" else 2, item["model"]))
    if any(item["status"] == "candidate" for item in results):
        status = "recommended"
    elif any(item["status"] == "needs_confirmation" for item in results):
        status = "needs_confirmation"
    else:
        status = "no_match"
    return status, results, []


def answer_from_approved_material(
    db: Database,
    tenant_id: str,
    question: str,
    product_id: str | None,
    locale: str = "zh",
) -> tuple[str, str, str | None, str | None, list[dict[str, str]], str]:
    """Answer only from the selected published snapshot; otherwise hand off."""
    locale = normalize_locale(locale)
    is_zh = locale == "zh"
    question_lower = question.lower()
    forbidden = ("price", "cost", "quote", "price", "价格", "报价", "库存", "stock", "delivery", "lead time", "shipping", "交期", "交货", "到货", "认证", "certificate", "certification")
    if any(token in question_lower for token in forbidden):
        answer = (
            "这个问题涉及价格、库存、交期或认证等资料外信息，当前无法从核准产品资料确认。你可以明确请求工程师答复；在你提交请求前，我们不会创建人工任务。"
            if is_zh else
            "This question involves price, stock, delivery or certification information that is outside the approved materials. You can request an engineer reply; no human task is created until you submit that request."
        )
        return "needs_human", answer, product_id, None, [], "资料不包含商业承诺或认证判断"

    product = db.get_product(tenant_id, product_id) if product_id else None
    version = db.get_published_version(tenant_id, product_id) if product_id else None
    if not product or not version:
        answer = (
            "当前没有可引用的已发布产品版本，无法确认这个问题。你可以明确请求工程师答复；在你提交请求前，我们不会创建人工任务。"
            if is_zh else
            "There is no approved published product version to cite for this question. You can request an engineer reply; no human task is created until you submit that request."
        )
        return "needs_human", answer, product_id, None, [], "没有可引用的已发布产品版本"

    snapshot = json.loads(version["snapshot_json"])
    fields = {field["field_key"]: field for field in snapshot.get("fields", [])}
    field_map = [
        (("flow", "流量"), "flow_range", "该型号的流量范围是 {value}{unit}。" if is_zh else "The flow range for this model is {value}{unit}."),
        (("head", "扬程"), "head_range", "该型号的扬程范围是 {value}{unit}。" if is_zh else "The head range for this model is {value}{unit}."),
        (("media", "介质", "medium", "液体"), "media", "资料标注的介质适用条件是：{value}{unit}。" if is_zh else "The approved material describes the applicable medium as: {value}{unit}."),
        (("material", "材质", "材料"), "material", "该型号的材质是：{value}{unit}。" if is_zh else "The material for this model is: {value}{unit}."),
        (("power", "功率"), "power", "该型号的功率是 {value}{unit}。" if is_zh else "The rated power for this model is {value}{unit}."),
        (("temperature", "温度", "temperature"), "temperature", "资料标注的介质温度范围是 {value}{unit}。" if is_zh else "The approved material describes the medium temperature range as {value}{unit}."),
        (("性能", "曲线", "performance", "catalogue"), "performance_points", "资料中的目录性能点（流量:扬程）是：{value}{unit}。最终工作点需工程师确认。" if is_zh else "The catalogue performance points (flow:head) are {value}{unit}. An engineer must confirm the final duty point."),
    ]
    selected_key = None
    template = ""
    for tokens, key, candidate_template in field_map:
        if any(token in question_lower for token in tokens):
            selected_key = key
            template = candidate_template
            break
    if not selected_key or not fields.get(selected_key) or not fields[selected_key].get("value", "").strip():
        answer = (
            "核准资料中没有足够依据回答这个问题。我们不会猜测参数；如果你需要工程师结合本次工况核实，请点击“请求工程师答复”。"
            if is_zh else
            "The approved materials do not provide enough evidence to answer this question. We will not guess a parameter; click \"Request an engineer reply\" if you want a case-specific review."
        )
        return "needs_human", answer, product_id, version["id"], [], "资料缺少对应字段或问题需要工程判断"

    field = fields[selected_key]
    citation = {
        "filename": field.get("source_filename") or "产品核准资料",
        "location": field.get("source_location") or "字段核准记录",
        "product_version": str(version["version"]),
        "field": field.get("label", selected_key),
    }
    source = db.conn.execute(
        "SELECT filename, source_location FROM source_documents WHERE tenant_id=? AND id=?",
        (tenant_id, field.get("source_document_id")),
    ).fetchone()
    if source:
        citation["filename"] = source["filename"]
        citation["location"] = field.get("source_location") or source["source_location"]
    answer = template.format(value=field.get("value", ""), unit=field.get("unit", ""))
    return "grounded", answer, product_id, version["id"], [citation], ""


def layout(title: str, body: str, active: str = "产品管理", lang: str = "zh") -> str:
    return f"""<!doctype html>
<html lang='{ 'en' if lang == 'en' else 'zh-CN' }'>
<head>
<meta charset='utf-8'><meta name='viewport' content='width=device-width, initial-scale=1'>
<title>{esc(title)} · Growave</title>
<style>
:root{{--ink:#f7f4ff;--muted:#a8a2b8;--line:#29223c;--accent:#8b5cf6;--accent-strong:#a855f7;--bg:#08070f;--card:rgba(20,17,33,.9);--card-2:#151126;--warn:#f6c56a;--danger:#ff8fa3;--success:#67e8c5;}}
*{{box-sizing:border-box}}html{{background:var(--bg)}}body{{min-height:100vh;margin:0;background-color:var(--bg);background-image:linear-gradient(rgba(139,92,246,.045) 1px,transparent 1px),linear-gradient(90deg,rgba(139,92,246,.045) 1px,transparent 1px),radial-gradient(circle at 85% 0%,rgba(139,92,246,.15),transparent 28rem);background-size:48px 48px,48px 48px,100% 100%;color:var(--ink);font:15px/1.6 "Avenir Next","SF Pro Display","PingFang SC","Microsoft YaHei",sans-serif}}
a{{color:#c4b5fd;text-decoration:none}}a:hover{{color:#ede9fe;text-decoration:none}}header{{position:sticky;top:0;z-index:10;background:rgba(8,7,15,.88);backdrop-filter:blur(18px);border-bottom:1px solid rgba(139,92,246,.18);color:var(--ink);padding:17px 32px;display:flex;justify-content:space-between;align-items:center;gap:22px}}header strong{{font-size:18px;letter-spacing:.08em}}header nav{{display:flex;gap:4px;align-items:center;justify-content:flex-end;flex-wrap:wrap}}header nav a{{color:var(--muted);padding:7px 10px;border-radius:9px}}header nav a:hover{{background:rgba(139,92,246,.12);color:var(--ink)}}main{{max-width:1240px;margin:0 auto;padding:54px 28px 80px}}h1{{font-size:38px;line-height:1.15;letter-spacing:-.035em;margin:0 0 8px}}h2{{font-size:21px;letter-spacing:-.02em;margin:0 0 14px}}h3{{font-size:16px;margin:20px 0 8px}}.muted{{color:var(--muted)}}.notice{{padding:13px 15px;border-radius:12px;background:rgba(103,232,197,.1);border:1px solid rgba(103,232,197,.22);color:#9af2da;margin:0 0 20px}}.error{{padding:13px 15px;border-radius:12px;background:rgba(255,143,163,.1);border:1px solid rgba(255,143,163,.23);color:#ffb1c0;margin:0 0 20px}}.grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(250px,1fr));gap:18px}}.metric-grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(190px,1fr));gap:18px}}.metric{{font-size:38px;font-weight:750;line-height:1.1;margin:8px 0;color:#fff}}.action-card{{display:block;color:var(--ink);transition:transform .2s ease,border-color .2s ease,background .2s ease}}.action-card:hover{{text-decoration:none;border-color:rgba(168,85,247,.75);background:rgba(32,24,52,.95);transform:translateY(-2px)}}.card{{background:var(--card);border:1px solid var(--line);border-radius:18px;padding:22px;box-shadow:0 18px 45px rgba(0,0,0,.2);backdrop-filter:blur(12px)}}.toolbar{{display:flex;justify-content:space-between;align-items:center;gap:12px;flex-wrap:wrap;margin-bottom:26px}}button,.button{{border:0;border-radius:10px;padding:10px 15px;background:linear-gradient(135deg,var(--accent),var(--accent-strong));color:#fff;font-weight:700;cursor:pointer;display:inline-block;box-shadow:0 8px 24px rgba(139,92,246,.24)}}button:hover,.button:hover{{filter:brightness(1.08);transform:translateY(-1px)}}button.secondary,.button.secondary{{background:rgba(139,92,246,.1);border:1px solid rgba(139,92,246,.35);color:#ddd6fe;box-shadow:none}}button.warn,.button.warn{{background:rgba(246,197,106,.12);border:1px solid rgba(246,197,106,.35);color:var(--warn);box-shadow:none}}button.danger,.button.danger{{background:rgba(255,143,163,.1);border:1px solid rgba(255,143,163,.35);color:var(--danger);box-shadow:none}}form.inline{{display:inline}}label{{display:block;font-weight:700;margin:10px 0 5px;color:#ded8ed}}input,select,textarea{{width:100%;padding:10px 11px;border:1px solid #3a3152;border-radius:10px;font:inherit;color:var(--ink);background:#0f0d19}}input::placeholder,textarea::placeholder{{color:#716a85}}input:focus,select:focus,textarea:focus{{outline:2px solid rgba(168,85,247,.35);border-color:var(--accent)}}textarea{{min-height:76px;resize:vertical}}.form-grid{{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:4px 14px}}.full{{grid-column:1/-1}}table{{width:100%;border-collapse:collapse}}th,td{{text-align:left;padding:12px 8px;border-bottom:1px solid var(--line);vertical-align:top}}th{{font-size:12px;color:var(--muted);text-transform:uppercase;letter-spacing:.06em}}.badge{{display:inline-flex;align-items:center;border-radius:999px;padding:3px 10px;font-size:12px;font-weight:700;background:rgba(139,92,246,.13);border:1px solid rgba(139,92,246,.24);color:#d8ccff}}.badge.published,.badge.approved{{background:rgba(103,232,197,.1);border-color:rgba(103,232,197,.25);color:#9af2da}}.badge.pending_review{{background:rgba(246,197,106,.1);border-color:rgba(246,197,106,.25);color:#f7d991}}.badge.withdrawn{{background:rgba(255,143,163,.1);border-color:rgba(255,143,163,.25);color:#ffb1c0}}.field-state{{font-size:12px;color:var(--muted)}}.source{{display:flex;justify-content:space-between;gap:12px;padding:11px 0;border-bottom:1px solid var(--line)}}.product-hero{{background:linear-gradient(135deg,#17112d 0%,#31206a 52%,#5627a4 100%);color:white;border:1px solid rgba(196,181,253,.22);border-radius:22px;padding:36px;margin-bottom:20px;box-shadow:0 22px 60px rgba(69,35,132,.25)}}.product-hero .eyebrow{{text-transform:uppercase;letter-spacing:.14em;font-size:12px;opacity:.8}}.product-hero h1{{font-size:42px;margin:4px 0 8px}}.kv{{display:grid;grid-template-columns:repeat(auto-fit,minmax(190px,1fr));gap:12px;margin-top:24px}}.kv div{{background:rgba(8,7,15,.24);border:1px solid rgba(255,255,255,.14);padding:13px;border-radius:12px}}.kv b{{display:block;font-size:12px;opacity:.75}}.kv span{{font-size:19px;font-weight:700}}.footer-note{{margin-top:28px;padding:14px 16px;border-left:3px solid #a78bfa;background:rgba(167,139,250,.08);color:#c4b5fd;border-radius:0 10px 10px 0}}.brand-mark{{display:inline-block;width:10px;height:10px;margin-right:10px;border-radius:3px 8px 3px 8px;background:linear-gradient(135deg,#c4b5fd,#7c3aed);box-shadow:0 0 18px rgba(168,85,247,.75);transform:rotate(45deg)}}@media(max-width:700px){{header{{padding:15px 18px;align-items:flex-start;flex-direction:column}}header nav{{justify-content:flex-start;overflow:auto;flex-wrap:nowrap;width:100%;padding-bottom:2px}}main{{padding:34px 14px 60px}}.form-grid{{grid-template-columns:1fr}}.full{{grid-column:auto}}h1{{font-size:30px}}.product-hero h1{{font-size:34px}}table{{font-size:13px}}}}
</style></head><body>
<header><strong><span class='brand-mark' aria-hidden='true'></span>Growave</strong><nav><a href='/admin'>{'管理概览' if lang == 'zh' else 'Overview'}</a><a href='/admin/products'>{'产品管理' if lang == 'zh' else 'Products'}</a><a href='/admin/tasks'>{'人工任务' if lang == 'zh' else 'Tasks'}</a><a href='/admin/rfqs'>{'RFQ询价' if lang == 'zh' else 'RFQs'}</a><a href='/admin/sales'>{'销售工作台' if lang == 'zh' else 'Sales desk'}</a><a href='/inquiry'>{'买方询盘' if lang == 'zh' else 'Buyer inquiry'}</a><a href='/admin/products?lang={'en' if lang == 'zh' else 'zh'}'>{'English' if lang == 'zh' else '中文'}</a></nav></header>
<main>{body}</main></body></html>"""


def status_badge(status: str) -> str:
    labels = {"draft": "草稿", "pending_review": "待核准", "approved": "已核准", "published": "已发布", "withdrawn": "已撤回"}
    return f"<span class='badge {esc(status)}'>{esc(labels.get(status, status))}</span>"


def parse_multipart(handler: BaseHTTPRequestHandler) -> dict[str, str | bytes]:
    content_type = handler.headers.get("Content-Type", "")
    length = int(handler.headers.get("Content-Length", "0"))
    payload = handler.rfile.read(length)
    if not content_type.startswith("multipart/form-data"):
        values = parse_qs(payload.decode("utf-8"), keep_blank_values=True)
        return {key: vals[-1] for key, vals in values.items()}
    message = BytesParser(policy=default).parsebytes(b"Content-Type: " + content_type.encode() + b"\r\nMIME-Version: 1.0\r\n\r\n" + payload)
    result: dict[str, str | bytes] = {}
    for part in message.iter_parts():
        name = part.get_param("name", header="content-disposition")
        if not name:
            continue
        filename = part.get_filename()
        content = part.get_payload(decode=True) or b""
        result[name] = content if filename else content.decode(part.get_content_charset() or "utf-8", errors="replace")
        if filename:
            result[f"{name}__filename"] = filename
    return result


class Handler(BaseHTTPRequestHandler):
    db: Database
    tenant_id = "demo-tenant"
    actor = "demo-admin"
    user_role = "admin"
    user_id = ""

    def log_message(self, format: str, *args: object) -> None:
        return

    def redirect(self, location: str, cookie: str | None = None) -> None:
        self.send_response(HTTPStatus.SEE_OTHER)
        self.send_header("Location", location)
        if cookie:
            self.send_header("Set-Cookie", cookie)
        self.end_headers()

    def cookie_value(self, name: str) -> str | None:
        raw = self.headers.get("Cookie", "")
        for part in raw.split(";"):
            key, _, value = part.strip().partition("=")
            if key == name:
                return value
        return None

    def current_user(self) -> sqlite3.Row | None:
        return self.db.get_session_user(self.cookie_value("cb_session"))

    def authorize_admin(self, path: str, method: str) -> bool:
        user = self.current_user()
        if not user:
            self.redirect("/login?next=" + quote(path, safe="/"))
            return False
        self.tenant_id = user["tenant_id"]
        self.actor = user["email"]
        self.user_role = user["role"]
        self.user_id = user["id"]
        if path == "/admin":
            allowed = {"admin", "sales", "engineer"}
        elif path.startswith("/admin/demo"):
            allowed = {"admin"}
        elif path.startswith("/admin/users"):
            allowed = {"admin"}
        elif path.startswith("/admin/products"):
            allowed = {"admin"}
        elif path.startswith("/admin/tasks"):
            allowed = {"admin", "engineer"}
        elif path.startswith("/admin/sales") or path.startswith("/admin/rfqs"):
            allowed = {"admin", "sales", "engineer"}
        else:
            allowed = set()
        if user["role"] not in allowed:
            self.send_html(layout("无权限", "<div class='error'>当前账号没有访问此管理功能的权限。</div><p><a href='/login'>切换账号</a></p>"), 403)
            return False
        return True

    def send_html(self, content: str, status: int = 200) -> None:
        data = content.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def send_bytes(self, content: bytes, content_type: str, filename: str, status: int = 200) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Disposition", f"attachment; filename*=UTF-8''{quote(filename)}")
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    def login_page(self, query: dict[str, list[str]], error: str = "") -> None:
        next_path = query.get("next", ["/admin/sales"])[0]
        error_html = f"<div class='error'>{esc(error)}</div>" if error else ""
        body = f"""
<div style='max-width:520px;margin:70px auto'><section class='card'><h1>企业后台登录</h1><p class='muted'>登录后只能访问当前账号所属企业的数据。买方公开页面不需要后台账号。</p>{error_html}<form method='post' action='/login'><input type='hidden' name='next' value='{esc(next_path)}'><label>邮箱</label><input name='email' type='email' autocomplete='username' required placeholder='demo-sales@example.invalid'><label>密码</label><input name='password' type='password' autocomplete='current-password' required><p><button>登录</button></p></form><div class='footer-note'>本地演示账号：管理员 `demo-admin`、销售 `demo-sales`、工程师 `demo-engineer`，密码与账号相同。仅用于本地演示，不能用于生产。</div></section></div>
"""
        self.send_html(layout("企业后台登录", body), 200)

    def login_post(self, form: dict[str, str | bytes]) -> None:
        user = self.db.authenticate_user(str(form.get("email", "")), str(form.get("password", "")))
        if not user:
            self.login_page({"next": [str(form.get("next", "/admin/sales"))]}, "邮箱或密码不正确")
            return
        next_path = str(form.get("next", "/admin/sales"))
        if not next_path.startswith("/") or next_path.startswith("//"):
            next_path = "/admin/sales"
        session_id = self.db.create_session(user)
        self.redirect(next_path, f"cb_session={session_id}; Path=/; HttpOnly; SameSite=Lax; Max-Age=43200")

    def logout(self) -> None:
        self.db.delete_session(self.cookie_value("cb_session"))
        self.redirect("/login", "cb_session=; Path=/; HttpOnly; SameSite=Lax; Max-Age=0")

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        query = parse_qs(parsed.query)
        if path == "/login":
            self.login_page(query)
            return
        if path == "/logout":
            self.logout()
            return
        if path == "/":
            self.redirect("/admin")
            return
        if (path == "/admin" or path.startswith("/admin/")) and not self.authorize_admin(path, "GET"):
            return
        if path == "/admin":
            self.admin_dashboard(query)
            return
        if path == "/admin/users":
            self.admin_users(query)
            return
        if path == "/admin/products":
            self.admin_products(query)
            return
        if path == "/admin/tasks":
            self.admin_tasks(query)
            return
        if path == "/admin/rfqs":
            self.admin_rfqs(query)
            return
        if path == "/admin/sales":
            self.admin_sales(query)
            return
        if path == "/inquiry":
            locale = normalize_locale(query.get("lang", ["en"])[0])
            conversation_id = self.db.create_conversation(self.tenant_id, "web", locale)
            self.redirect(f"/inquiry/{conversation_id}?lang={locale}")
            return
        match = re.fullmatch(r"/admin/products/([a-f0-9]+)/?", path)
        if match:
            self.admin_product(match.group(1), query)
            return
        match = re.fullmatch(r"/inquiry/([a-f0-9]+)/?", path)
        if match:
            self.buyer_inquiry(match.group(1), query)
            return
        match = re.fullmatch(r"/admin/tasks/([a-f0-9]+)/?", path)
        if match:
            self.admin_task(match.group(1), query)
            return
        match = re.fullmatch(r"/admin/rfqs/([a-f0-9]+)/?", path)
        if match:
            self.admin_rfq(match.group(1), query)
            return
        match = re.fullmatch(r"/admin/sales/([a-f0-9]+)/?", path)
        if match:
            self.admin_sales_detail(match.group(1), query)
            return
        match = re.fullmatch(r"/products/([a-zA-Z0-9-]+)/?", path)
        if match:
            self.public_product(match.group(1), query)
            return
        match = re.fullmatch(r"/products/([a-zA-Z0-9-]+)/sources/([a-f0-9]+)/?", path)
        if match:
            self.public_source(match.group(1), match.group(2))
            return
        if path.startswith("/files/"):
            self.send_html(layout("文件下载", "<div class='error'>演示环境不提供公开文件直链，请从企业后台下载或补充对象存储权限。</div>"), 404)
            return
        self.send_html(layout("未找到", "<div class='error'>页面不存在</div>"), 404)

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/")
        if path == "/login":
            self.login_post(parse_multipart(self))
            return
        if path == "/logout":
            self.logout()
            return
        if path.startswith("/admin/") and not self.authorize_admin(path, "POST"):
            return
        try:
            form = parse_multipart(self)
            if path == "/admin/products":
                self.db.create_product(self.tenant_id, str(form.get("model", "")), str(form.get("name", "")), str(form.get("use_case", "")), self.actor)
                self.redirect("/admin/products")
                return
            if path == "/admin/products/seed":
                self.db.seed_demo_products(self.tenant_id, self.actor)
                self.redirect("/admin/products?notice=demo")
                return
            if path == "/admin/products/seed-lowara":
                summary = self.db.seed_lowara_workspace(self.tenant_id, self.actor)
                self.redirect(f"/admin/products?notice=lowara&created={summary['products']}")
                return
            if path == "/admin/users":
                self.db.create_user(
                    self.tenant_id,
                    str(form.get("email", "")),
                    str(form.get("display_name", "")),
                    str(form.get("role", "sales")),
                    str(form.get("password", "")),
                    self.actor,
                )
                self.redirect("/admin/users?notice=created")
                return
            if path == "/admin/demo/seed":
                summary = self.db.seed_demo_workspace(self.tenant_id, self.actor)
                self.redirect(f"/admin?notice=demo&rfqs={summary['rfqs']}&tasks={summary['tasks']}")
                return
            if path == "/admin/lowara/seed":
                summary = self.db.seed_lowara_workspace(self.tenant_id, self.actor)
                self.redirect(f"/admin?notice=lowara&products={summary['products']}")
                return
            match = re.fullmatch(r"/inquiry/([a-f0-9]+)/message", path)
            if match:
                conversation_id = match.group(1)
                message = str(form.get("message", ""))
                conversation = self.db.get_conversation(self.tenant_id, conversation_id)
                if not conversation:
                    raise ValueError("询盘不存在")
                locale = normalize_locale(conversation["locale"])
                self.db.save_message(self.tenant_id, conversation_id, "buyer", message, "buyer_web")
                previous = self.db.get_latest_revision(self.tenant_id, conversation_id)
                previous_values = json.loads(previous["values_json"]) if previous else {}
                explicit = {key: str(form.get(key, "")) for key in REQUIREMENT_LABELS}
                for key in REQUIREMENT_LABELS:
                    explicit[f"{key}_unit"] = str(form.get(f"{key}_unit", ""))
                values = extract_requirement_values(message, explicit, previous_values)
                self.db.create_requirement_revision(self.tenant_id, conversation_id, values, "candidate", "buyer-web")
                missing = [REQUIREMENT_LABELS[key] for key in MATCH_REQUIRED_FIELDS if not values.get(key, {}).get("value", "").strip()]
                reply = requirement_capture_reply(locale, missing)
                self.db.save_message(self.tenant_id, conversation_id, "assistant", reply, "rule-based-assistant")
                self.redirect(f"/inquiry/{conversation_id}?lang={locale}")
                return
            match = re.fullmatch(r"/inquiry/([a-f0-9]+)/confirm", path)
            if match:
                conversation_id = match.group(1)
                previous = self.db.get_latest_revision(self.tenant_id, conversation_id)
                if not previous:
                    raise ValueError("请先发送一条需求消息")
                values = json.loads(previous["values_json"])
                for key in REQUIREMENT_LABELS:
                    submitted = str(form.get(key, "")).strip()
                    if submitted:
                        values[key] = {
                            "value": submitted,
                            "unit": str(form.get(f"{key}_unit", values.get(key, {}).get("unit", ""))),
                            "state": "candidate",
                            "source": "buyer_confirmation_form",
                            "label": REQUIREMENT_LABELS[key],
                        }
                    elif key in values:
                        values[key]["value"] = values[key].get("value", "").strip()
                confirmed_values = confirm_requirement_values(values)
                revision_id = self.db.create_requirement_revision(self.tenant_id, conversation_id, confirmed_values, "confirmed", "buyer-web")
                status, results, _missing = match_products(self.db, self.tenant_id, confirmed_values)
                self.db.save_recommendation(self.tenant_id, conversation_id, revision_id, status, results)
                conversation = self.db.get_conversation(self.tenant_id, conversation_id)
                locale = normalize_locale(conversation["locale"] if conversation else "en")
                self.redirect(f"/inquiry/{conversation_id}?lang={locale}&notice=recommendation")
                return
            match = re.fullmatch(r"/inquiry/([a-f0-9]+)/ask", path)
            if match:
                conversation_id = match.group(1)
                question = str(form.get("question", "")).strip()
                if not question:
                    raise ValueError("问题不能为空")
                question_message_id = self.db.save_message(self.tenant_id, conversation_id, "buyer", question, "buyer_web")
                selected_product_id = str(form.get("product_id", "")).strip() or None
                recommendation = self.db.latest_active_recommendation(self.tenant_id, conversation_id)
                latest_revision = self.db.get_latest_revision(self.tenant_id, conversation_id)
                result_for_product = None
                if recommendation:
                    recommendation_results = json.loads(recommendation["results_json"])
                    if selected_product_id:
                        result_for_product = next((result for result in recommendation_results if result.get("product_id") == selected_product_id), None)
                    if not result_for_product:
                        result_for_product = next((result for result in recommendation_results if result.get("status") in {"candidate", "needs_confirmation"}), None)
                    selected_product_id = (result_for_product or {}).get("product_id") or selected_product_id
                conversation = self.db.get_conversation(self.tenant_id, conversation_id)
                locale = normalize_locale(conversation["locale"] if conversation else "en")
                answer_status, answer_text, answered_product_id, product_version_id, citations, reason = answer_from_approved_material(self.db, self.tenant_id, question, selected_product_id, locale)
                self.db.save_message(self.tenant_id, conversation_id, "assistant", answer_text, "grounded-answer" if answer_status == "grounded" else "human-handoff")
                self.db.save_answer(self.tenant_id, conversation_id, question_message_id, answered_product_id, product_version_id, answer_status, answer_text, citations)
                self.redirect(f"/inquiry/{conversation_id}?lang={locale}&notice=answer")
                return
            match = re.fullmatch(r"/inquiry/([a-f0-9]+)/request-help", path)
            if match:
                conversation_id = match.group(1)
                question_message_id = str(form.get("question_message_id", "")).strip()
                answer = self.db.get_answer_for_question(self.tenant_id, conversation_id, question_message_id)
                question_message = self.db.get_message(self.tenant_id, conversation_id, question_message_id)
                if not answer or answer["answer_status"] != "needs_human" or not question_message:
                    raise ValueError("找不到可请求人工答复的问题")
                conversation = self.db.get_conversation(self.tenant_id, conversation_id)
                locale = normalize_locale(conversation["locale"] if conversation else "en")
                existing_task = self.db.get_human_task_for_question(self.tenant_id, conversation_id, question_message_id)
                if not existing_task:
                    recommendation = self.db.latest_active_recommendation(self.tenant_id, conversation_id)
                    latest_revision = self.db.get_latest_revision(self.tenant_id, conversation_id)
                    _, _, _, product_version_id, _, reason = answer_from_approved_material(
                        self.db, self.tenant_id, question_message["content"], answer["product_id"], locale
                    )
                    context = {
                        "requirement_revision_id": latest_revision["id"] if latest_revision else None,
                        "confirmed_requirements": json.loads(latest_revision["values_json"]) if latest_revision else {},
                        "recommendation_id": recommendation["id"] if recommendation else None,
                        "product_model": answer["model"],
                        "product_version_id": product_version_id or answer["product_version_id"],
                        "answer_so_far": answer["answer_text"],
                    }
                    self.db.create_human_task(
                        self.tenant_id,
                        conversation_id,
                        question_message_id,
                        question_message["content"],
                        reason,
                        context,
                        answer["product_id"],
                        product_version_id or answer["product_version_id"],
                        latest_revision["id"] if latest_revision else None,
                        recommendation["id"] if recommendation else None,
                    )
                self.redirect(f"/inquiry/{conversation_id}?lang={locale}&notice=handoff")
                return
            match = re.fullmatch(r"/inquiry/([a-f0-9]+)/rfq", path)
            if match:
                conversation_id = match.group(1)
                submission_key = str(form.get("submission_key", "")).strip()
                if not submission_key:
                    raise ValueError("询价提交标识缺失，请刷新页面后重试")
                contact = {key: str(form.get(key, "")) for key in ("contact_name", "company", "contact_email", "contact_phone", "quantity", "region", "delivery")}
                rfq_id, created = self.db.create_rfq(self.tenant_id, conversation_id, submission_key, contact, str(form.get("notes", "")), str(form.get("product_id", "")) or None)
                conversation = self.db.get_conversation(self.tenant_id, conversation_id)
                locale = normalize_locale(conversation["locale"] if conversation else "en")
                self.redirect(f"/inquiry/{conversation_id}?lang={locale}&notice=rfq&rfq_id={rfq_id}&created={'1' if created else '0'}")
                return
            match = re.fullmatch(r"/admin/tasks/([a-f0-9]+)/reply", path)
            if match:
                self.db.resolve_human_task(self.tenant_id, match.group(1), str(form.get("response", "")), self.actor)
                self.redirect(f"/admin/tasks/{match.group(1)}?notice=resolved")
                return
            match = re.fullmatch(r"/admin/sales/([a-f0-9]+)/update", path)
            if match:
                self.db.update_lead(self.tenant_id, match.group(1), str(form.get("stage", "new")), str(form.get("owner", "")), str(form.get("next_follow_up", "")), str(form.get("note", "")), self.actor)
                self.redirect(f"/admin/sales/{match.group(1)}?notice=updated")
                return
            match = re.fullmatch(r"/admin/users/([a-f0-9]+)/toggle", path)
            if match:
                user_id = match.group(1)
                if user_id == self.user_id:
                    raise ValueError("不能停用当前登录账号")
                target = self.db.conn.execute(
                    "SELECT active FROM users WHERE tenant_id=? AND id=?",
                    (self.tenant_id, user_id),
                ).fetchone()
                if not target:
                    raise ValueError("账号不存在")
                self.db.set_user_active(self.tenant_id, user_id, not bool(target["active"]), self.actor)
                self.redirect("/admin/users?notice=updated")
                return
            match = re.fullmatch(r"/admin/products/([a-f0-9]+)/sources", path)
            if match:
                product_id = match.group(1)
                content = form.get("source_file") if isinstance(form.get("source_file"), bytes) else None
                filename = str(form.get("source_file__filename", "")) or str(form.get("filename", ""))
                self.db.add_source(self.tenant_id, product_id, filename or "未命名资料", str(form.get("file_type", "other")), str(form.get("source_location", "")), content, self.actor)
                self.redirect(f"/admin/products/{product_id}?notice=source")
                return
            match = re.fullmatch(r"/admin/products/([a-f0-9]+)/fields", path)
            if match:
                product_id = match.group(1)
                self.db.save_field(self.tenant_id, product_id, str(form.get("field_key", "")), str(form.get("value", "")), str(form.get("unit", "")), str(form.get("source_document_id", "")) or None, str(form.get("source_location", "")), self.actor)
                self.redirect(f"/admin/products/{product_id}?notice=field")
                return
            match = re.fullmatch(r"/admin/products/([a-f0-9]+)/(approve|publish|withdraw)", path)
            if match:
                product_id, action = match.groups()
                if action == "approve":
                    self.db.approve_product(self.tenant_id, product_id, self.actor)
                elif action == "publish":
                    self.db.publish_product(self.tenant_id, product_id, self.actor)
                else:
                    self.db.withdraw_product(self.tenant_id, product_id, self.actor)
                self.redirect(f"/admin/products/{product_id}?notice={action}")
                return
        except (ValueError, sqlite3.Error) as exc:
            self.send_html(layout("操作未完成", f"<div class='error'>{esc(exc)}</div><p><a href='/admin/products'>返回产品列表</a></p>"), 400)
            return
        self.send_html(layout("未找到", "<div class='error'>操作不存在</div>"), 404)

    def admin_products(self, query: dict[str, list[str]]) -> None:
        lang = query.get("lang", ["zh"])[0]
        products = self.db.list_products(self.tenant_id)
        notice = query.get("notice", [""])[0]
        notice_html = "<div class='notice'>示例资料已登记为候选值，请逐个核对后核准，再发布到买方产品页。</div>" if notice == "demo" else ""
        if notice == "lowara":
            notice_html = f"<div class='notice'>Lowara 资料已加载：新增 {esc(query.get('created', ['0'])[0])} 个内部演示型号。来源文件只登记元数据，外部再发布授权仍待确认。</div>"
        row_parts = []
        for product in products:
            public_link = (
                f"<a class='button secondary' href='/products/{esc(product['slug'])}' target='_blank'>打开买方页</a>"
                if product["status"] == "published"
                else "<span class='muted'>未公开</span>"
            )
            row_parts.append(
                f"<tr><td><a href='/admin/products/{product['id']}'>{esc(product['model'])}</a><br><span class='muted'>{esc(product['name'])}</span></td>"
                f"<td>{status_badge(product['status'])}</td><td>{product['approved_count']} 个字段已核准 / {product['source_count']} 份来源</td>"
                f"<td>{public_link}</td></tr>"
            )
        rows = "".join(row_parts) or "<tr><td colspan='4' class='muted'>还没有产品，请先导入示例或新建型号。</td></tr>"
        body = f"""
<div class='toolbar'><div><h1>{'产品资料与发布' if lang == 'zh' else 'Products & publishing'}</h1><div class='muted'>企业：示例水泵企业（演示） · 当前操作员：管理员</div></div><div><form method='post' action='/admin/products/seed' class='inline'><button class='secondary'>导入两款示例水泵</button></form> <form method='post' action='/admin/products/seed-lowara' class='inline'><button class='secondary'>加载 Lowara 资料</button></form></div></div>
{notice_html}
<div class='grid'>
<section class='card'><h2>新建产品型号</h2><form method='post' action='/admin/products'><label>型号 *</label><input name='model' placeholder='例如 CB-80A' required><label>产品名称</label><input name='name' placeholder='买方页面显示名称'><label>主要用途</label><textarea name='use_case' placeholder='例如清水循环、化工介质输送'></textarea><p><button>创建草稿</button></p></form></section>
<section class='card'><h2>当前切片</h2><p>产品资料登记 → 来源文件 → 字段候选值 → 人工核准 → 版本发布 → 固定买方页面。</p><p class='muted'>未核准字段不会进入公开页；重新编辑已发布字段会回到待核准状态。Lowara 数据仅用于内部 Demo，外部再发布授权待确认。</p></section>
</div>
<section class='card' style='margin-top:18px'><h2>产品列表</h2><table><thead><tr><th>型号</th><th>状态</th><th>资料完整度</th><th>买方页面</th></tr></thead><tbody>{rows}</tbody></table></section>
"""
        self.send_html(layout("产品资料与发布", body, lang=lang), 200)

    def admin_product(self, product_id: str, query: dict[str, list[str]]) -> None:
        product = self.db.get_product(self.tenant_id, product_id)
        if not product:
            self.send_html(layout("未找到", "<div class='error'>产品不存在或不属于当前企业。</div>"), 404)
            return
        sources = self.db.get_sources(self.tenant_id, product_id)
        fields = self.db.get_fields(self.tenant_id, product_id)
        notice = query.get("notice", [""])[0]
        notice_text = {"source": "来源资料已保存。", "field": "字段已保存为候选值，需重新核准。", "approve": "产品字段已核准。", "publish": "产品版本已发布。", "withdraw": "公开版本已撤回。"}.get(notice, "")
        field_rows = "".join(
            f"<tr><td><b>{esc(field['label'])}</b><br><span class='field-state'>{esc(field['field_key'])}</span></td><td>{esc(field['value'])} {esc(field['unit'])}</td><td>{status_badge(field['state'])}<br><span class='field-state'>{esc(field['source_filename'] or '未关联来源')} · {esc(field['source_location'])}</span></td></tr>"
            for field in fields
        ) or "<tr><td colspan='3' class='muted'>尚未登记字段。</td></tr>"
        source_options = "<option value=''>请选择来源</option>" + "".join(f"<option value='{esc(source['id'])}'>{esc(source['filename'])} · {esc(source['source_location'])}</option>" for source in sources)
        source_rows = "".join(f"<div class='source'><span><b>{esc(source['filename'])}</b><br><span class='muted'>{esc(source['file_type'])} · {esc(source['source_location'])}</span></span><span class='field-state'>{esc(source['processing_status'])}</span></div>" for source in sources) or "<p class='muted'>还没有来源资料。</p>"
        field_options = "".join(f"<option value='{key}'>{esc(label)}</option>" for key, label in FIELD_LABELS.items())
        action_buttons = f"<form method='post' action='/admin/products/{product_id}/approve' class='inline'><button>核准字段</button></form> <form method='post' action='/admin/products/{product_id}/publish' class='inline'><button class='secondary'>发布新版本</button></form>" + (f" <form method='post' action='/admin/products/{product_id}/withdraw' class='inline'><button class='danger'>撤回公开页</button></form>" if product['status'] == 'published' else "")
        notice_banner = f"<div class='notice'>{esc(notice_text)}</div>" if notice_text else ""
        body = f"""
<div class='toolbar'><div><a href='/admin/products'>← 返回产品列表</a><h1>{esc(product['model'])}</h1><div class='muted'>{esc(product['name'])} · {status_badge(product['status'])}</div></div><div>{action_buttons}</div></div>
{notice_banner}
<div class='grid'><section class='card'><h2>产品基本信息</h2><p><b>主要用途：</b>{esc(product['use_case']) or '未填写'}</p><p><b>固定买方链接：</b>{('/products/' + esc(product['slug'])) if product['status'] == 'published' else '发布后生成'}</p><p class='muted'>型号是产品通用事实；买方本次工况将在后续会话中单独保存，不写回这里。</p></section><section class='card'><h2>登记来源资料</h2>{source_rows}<hr><form method='post' action='/admin/products/{product_id}/sources' enctype='multipart/form-data'><label>文件（可选）</label><input type='file' name='source_file'><label>或资料文件名</label><input name='filename' placeholder='产品手册.pdf'><div class='form-grid'><div><label>类型</label><select name='file_type'><option>PDF</option><option>XLSX</option><option>IMAGE</option><option>CAD</option><option>OTHER</option></select></div><div><label>原文位置</label><input name='source_location' placeholder='第 1 页：规格参数表' required></div></div><p><button class='secondary'>保存来源</button></p></form></section></div>
<section class='card' style='margin-top:18px'><h2>字段核对</h2><p class='muted'>关键字段必须有来源且核准后，才能进入产品版本和公开页面。</p><table><thead><tr><th>字段</th><th>值</th><th>来源与状态</th></tr></thead><tbody>{field_rows}</tbody></table><hr><form method='post' action='/admin/products/{product_id}/fields'><div class='form-grid'><div><label>字段</label><select name='field_key'>{field_options}</select></div><div><label>值 *</label><input name='value' required placeholder='例如 20-80'></div><div><label>单位</label><input name='unit' placeholder='m³/h、m、°C'></div><div><label>来源</label><select name='source_document_id'>{source_options}</select></div><div class='full'><label>原文位置</label><input name='source_location' required placeholder='第 1 页：规格参数表'></div></div><p><button>保存为候选值</button></p></form></section>
"""
        self.send_html(layout(f"{product['model']} · 字段核对", body), 200)

    def buyer_inquiry(self, conversation_id: str, query: dict[str, list[str]]) -> None:
        conversation = self.db.get_conversation(self.tenant_id, conversation_id)
        if not conversation:
            self.send_html(layout("Inquiry not found", "<div class='error'>This inquiry does not exist or is not available.</div>", lang="en"), 404)
            return
        lang = normalize_locale(query.get("lang", [conversation["locale"] or "en"])[0])
        self.db.set_conversation_locale(self.tenant_id, conversation_id, lang)
        messages = self.db.list_messages(self.tenant_id, conversation_id)
        revisions = self.db.get_revisions(self.tenant_id, conversation_id)
        latest = revisions[-1] if revisions else None
        values = json.loads(latest["values_json"]) if latest else {}
        missing = json.loads(latest["missing_json"]) if latest else list(MATCH_REQUIRED_FIELDS)
        recommendations = [row for row in self.db.get_recommendations(self.tenant_id, conversation_id) if row["status"] != "invalidated"]
        current_recommendation = recommendations[0] if recommendations else None
        answers = self.db.list_answers(self.tenant_id, conversation_id)
        tasks = [task for task in self.db.list_human_tasks(self.tenant_id) if task["conversation_id"] == conversation_id]
        notice = query.get("notice", [""])[0]
        rfq_id = query.get("rfq_id", [""])[0]
        current_rfq = self.db.get_rfq(self.tenant_id, rfq_id) if rfq_id else None
        notice_html = {
            "recommendation": "<div class='notice'>需求已保存，推荐结果会基于当前已确认摘要重新计算。</div>" if lang == "zh" else "<div class='notice'>Requirements saved. Recommendations are recalculated from the confirmed summary.</div>",
            "answer": "<div class='notice'>问题已保存。资料不足时，请在对应回答下明确请求工程师答复。</div>" if lang == "zh" else "<div class='notice'>Question saved. If the materials are insufficient, request an engineer reply below.</div>",
            "handoff": "<div class='notice'>已收到人工答复请求，工程师会在本会话中处理；刷新页面可查看最新状态。</div>" if lang == "zh" else "<div class='notice'>Your engineer-reply request was received. Refresh this conversation to see its status.</div>",
        }.get(notice, "")
        if notice == "rfq" and current_rfq:
            missing_labels = json.loads(current_rfq["missing_json"])
            if current_rfq["status"] == "submitted":
                notice_html = f"<div class='notice'><b>询价已提交：</b>{esc(current_rfq['id'])}。销售会根据已保存的需求和产品版本继续处理。</div>"
            else:
                notice_html = f"<div class='error'><b>询价已保存，但仍待补充：</b>{esc('、'.join(missing_labels))}。记录编号：{esc(current_rfq['id'])}。</div>"

        message_rows = "".join(
            f"<div class='source'><span><b>{'You' if message['role'] == 'buyer' else 'Assistant'}</b><br>{esc(message['content'])}</span><span class='field-state'>{esc(message['created_at'])}</span></div>"
            for message in messages
        ) or "<p class='muted'>Describe your pump application to start.</p>"
        summary_rows = "".join(
            f"<tr><td>{esc(item.get('label', REQUIREMENT_LABELS.get(key, key)))}</td><td>{esc(item.get('value', '')) or '—'} {esc(item.get('unit', ''))}</td><td><span class='badge'>{'Confirmed' if item.get('state') == 'confirmed' else 'Needs confirmation' if item.get('state') == 'candidate' else 'Unknown'}</span><br><span class='field-state'>{esc(item.get('source', ''))}</span></td></tr>"
            for key, item in values.items()
        ) or "<tr><td colspan='3' class='muted'>No structured requirements yet.</td></tr>"
        missing_html = "" if not missing else "<div class='error'>Before matching, please confirm: " + ", ".join(esc(REQUIREMENT_LABELS.get(key, key)) for key in missing) + "</div>"
        input_fields = "".join(
            f"<div><label>{esc(REQUIREMENT_LABELS[key])}{' *' if key in MATCH_REQUIRED_FIELDS else ''}</label><input name='{key}' value='{esc(values.get(key, {}).get('value', ''))}' placeholder='{esc({'flow': 'e.g. 50', 'head': 'e.g. 30', 'media': 'e.g. clean water', 'temperature': 'e.g. 60', 'quantity': 'e.g. 2', 'region': 'e.g. Germany', 'delivery': 'e.g. 8 weeks', 'category': 'water pump', 'use_case': 'cooling water'} .get(key, ''))}'></div>"
            for key in ("flow", "head", "media", "temperature", "quantity", "region", "delivery")
        )

        recommendation_html = "<p class='muted'>Send a message and confirm the structured summary to see a recommendation.</p>"
        if current_recommendation:
            result_status = current_recommendation["status"]
            result_label = {"recommended": "可初步推荐 / Initial recommendation", "needs_confirmation": "需工程确认 / Engineering confirmation needed", "insufficient": "条件不足 / More information needed", "no_match": "无可靠候选 / No reliable candidate"}.get(result_status, result_status)
            results = json.loads(current_recommendation["results_json"])
            cards = []
            for result in results:
                card_class = "notice" if result["status"] in {"candidate", "needs_confirmation"} else "error"
                result_badge = {"candidate": "Candidate", "needs_confirmation": "需工程确认 / Confirm", "conflict": "Conflict"}.get(result["status"], result["status"])
                cards.append(
                    f"<div class='card' style='margin-top:12px'><h3>{esc(result['model'])} <span class='badge'>{result_badge}</span></h3>"
                    f"<p><b>满足项 / Meets:</b> {esc('；'.join(result['satisfies']) or '—')}</p>"
                    f"<p><b>冲突项 / Conflicts:</b> {esc('；'.join(result['conflicts']) or '—')}</p>"
                    f"<p><b>未确认 / Unknown:</b> {esc('；'.join(result['unknown']) or '—')}</p>"
                    f"<a class='button secondary' href='{esc(result['detail_url'])}' target='_blank'>查看产品页 / Product page</a></div>"
                )
            if result_status == "insufficient":
                cards.append(f"<div class='error'>缺少影响匹配的条件：{esc(', '.join(REQUIREMENT_LABELS.get(key, key) for key in missing))}。系统不会把未知当成满足。</div>")
            if result_status == "no_match":
                cards.append("<div class='error'>当前已发布产品没有满足全部硬条件的可靠候选，请转人工确认；系统没有把冲突产品标为适用。</div>")
            if result_status == "needs_confirmation":
                cards.append("<div class='error'>当前结果包含目录曲线或不可解析条件，系统不会把它当成最终适配；请让工程师确认工作点后再报价。</div>")
            recommendation_html = f"<div class='notice'><b>匹配结果：</b>{esc(result_label)} · 规则版本 {esc(current_recommendation['rule_version'])}（演示规则，不能替代正式工程选型）</div>" + "".join(cards)

        answer_rows = []
        for answer in answers:
            citations = json.loads(answer["citations_json"])
            citation_html = "".join(f"<div class='field-state'>引用：{esc(citation.get('filename'))} · {esc(citation.get('location'))} · 产品版本 {esc(citation.get('product_version'))}</div>" for citation in citations)
            if answer["answer_status"] == "grounded":
                state_label = "有据回答"
                action_html = ""
            else:
                state_label = "待请求人工确认"
                task = next((item for item in tasks if item["question_message_id"] == answer["question_message_id"]), None)
                if task:
                    action_html = f"<div class='field-state'>人工任务：{esc(task['status'])}。请返回本会话查看工程师回复。</div>"
                else:
                    action_html = f"<form method='post' action='/inquiry/{conversation_id}/request-help' class='inline'><input type='hidden' name='question_message_id' value='{esc(answer['question_message_id'])}'><button class='secondary'>请求工程师答复</button></form>"
            answer_rows.append(f"<div class='source'><span><b>{state_label}</b><br>{esc(answer['answer_text'])}{citation_html}{action_html}</span><span class='field-state'>{esc(answer['created_at'])}</span></div>")
        answer_html = "".join(answer_rows) or "<p class='muted'>还没有问答记录。</p>"
        task_html = "".join(f"<div class='source'><span><b>人工任务</b> · {esc(task['status'])}<br>{esc(task['question'])}</span><span class='field-state'>状态会在本会话中更新</span></div>" for task in tasks)
        candidate_options = "<option value=''>自动选择当前推荐型号</option>"
        if current_recommendation:
            for result in json.loads(current_recommendation["results_json"]):
                if result.get("status") in {"candidate", "needs_confirmation"}:
                    candidate_options += f"<option value='{esc(result['product_id'])}'>{esc(result['model'])} · v{esc(result['product_version'])}</option>"
        prefill = {key: values.get(key, {}).get("value", "") for key in ("quantity", "region", "delivery")}
        submission_key = secrets.token_urlsafe(18)

        body = f"""
<div class='toolbar'><div><h1>Buyer inquiry</h1><div class='muted'>Conversation {esc(conversation_id)} · 原始消息和每次需求修订都会保留</div></div><div><a class='button secondary' href='/inquiry?lang={'zh' if lang == 'en' else 'en'}'>New inquiry / 新询盘</a></div></div>
{notice_html}
<div class='grid'><section class='card'><h2>Describe your application</h2><p class='muted'>Tell us the pump use, flow, head and medium in your own words. Structured values are candidates until you confirm them.</p><form method='post' action='/inquiry/{conversation_id}/message'><label>Your message / 需求原话 *</label><textarea name='message' required placeholder='Example: I need a water pump for cooling water, flow 50 m³/h and head 30 m.'></textarea><p><button>Save message and extract fields</button></p></form><hr><h3>Conversation</h3>{message_rows}</section>
<section class='card'><h2>Requirement summary</h2>{missing_html}<form method='post' action='/inquiry/{conversation_id}/confirm'><div class='form-grid'>{input_fields}</div><p><button>Confirm summary and match</button></p></form><table><thead><tr><th>Field</th><th>Value</th><th>State</th></tr></thead><tbody>{summary_rows}</tbody></table></section></div>
<section class='card' style='margin-top:18px'><h2>Recommendations</h2>{recommendation_html}</section>
<section class='card' style='margin-top:18px'><h2>Ask about the recommended product</h2><p class='muted'>资料内问题会显示引用；价格、交期、认证或资料外问题不会被猜测。只有你点击“请求工程师答复”后，后台才会生成人工任务。</p><form method='post' action='/inquiry/{conversation_id}/ask'><label>Product context</label><select name='product_id'>{candidate_options}</select><label>Your question / 技术问题 *</label><textarea name='question' required placeholder='Example: What is the flow range? Or ask about price, stock, certification to test handoff.'></textarea><p><button>Ask and save answer</button></p></form><h3>Answers and handoff</h3>{answer_html}{task_html}</section>
<section class='card' style='margin-top:18px'><h2>Request for quotation (RFQ)</h2><p class='muted'>已确认工况和推荐产品会保存为本次询价快照；联系方式只供企业后台使用。缺少数量、目的地、期望交期或邮箱时，记录会标记为“待补充”，不会标记为可报价。</p><form method='post' action='/inquiry/{conversation_id}/rfq'><input type='hidden' name='submission_key' value='{esc(submission_key)}'><div class='form-grid'><div><label>Product / 产品</label><select name='product_id'>{candidate_options}</select></div><div><label>Contact name / 联系人</label><input name='contact_name' placeholder='Your name'></div><div><label>Company / 公司</label><input name='company' placeholder='Company name'></div><div><label>Email / 邮箱</label><input name='contact_email' type='email' placeholder='you@example.com'></div><div><label>Phone / 电话</label><input name='contact_phone' placeholder='+49 ...'></div><div><label>Quantity / 数量</label><input name='quantity' value='{esc(prefill['quantity'])}' placeholder='e.g. 2'></div><div><label>Destination / 目的地</label><input name='region' value='{esc(prefill['region'])}' placeholder='e.g. Germany'></div><div><label>Expected delivery / 期望交期</label><input name='delivery' value='{esc(prefill['delivery'])}' placeholder='e.g. 8 weeks'></div><div class='full'><label>Notes / 补充说明</label><textarea name='notes' placeholder='Additional operating conditions or questions'></textarea></div></div><p><button>Save RFQ / 提交询价</button></p></form></section>
<div class='footer-note'>产品页面、推荐和后续 chatbot 都只读取已核准并已发布的产品版本。当前规则是透明的水泵演示规则，正式阈值需由企业工程师确认。</div>
"""
        self.send_html(layout("Buyer inquiry", body, lang=lang), 200)

    def admin_dashboard(self, query: dict[str, list[str]]) -> None:
        summary = self.db.get_admin_summary(self.tenant_id)
        role_labels = {"admin": "管理员", "sales": "销售", "engineer": "工程师"}
        role_label = role_labels.get(self.user_role, self.user_role)
        demo_notice = ""
        if query.get("notice", [""])[0] == "demo":
            demo_notice = f"<div class='notice'>演示数据已加载：新增 RFQ {esc(query.get('rfqs', ['0'])[0])} 条、人工任务 {esc(query.get('tasks', ['0'])[0])} 条；重复加载不会复制已有记录。</div>"
        elif query.get("notice", [""])[0] == "lowara":
            demo_notice = f"<div class='notice'>Lowara 资料已加载：新增 {esc(query.get('products', ['0'])[0])} 个内部演示型号；来源文件只登记元数据，外部再发布授权仍待确认。</div>"
        cards = "".join(
            f"<div class='card'><div class='muted'>{label}</div><div class='metric'>{value}</div><div class='field-state'>{detail}</div></div>"
            for label, value, detail in (
                ("产品总数", summary["products_total"], f"已发布 {summary['products_published']} · 待核准 {summary['products_needs_review']}"),
                ("待处理人工任务", summary["tasks_pending"], f"累计任务 {summary['tasks_total']}"),
                ("RFQ 询价", summary["rfqs_total"], f"待补充 {summary['rfqs_needs_info']}"),
                ("进行中线索", summary["leads_active"], f"逾期跟进 {summary['leads_overdue']}"),
            )
        )
        quick_links = [
            ("/admin/sales", "打开销售工作台", "查看待跟进线索、负责人和完整上下文"),
            ("/admin/rfqs", "查看 RFQ", "检查买方提交的询价和待补充字段"),
        ]
        if self.user_role in {"admin", "engineer"}:
            quick_links.append(("/admin/tasks?status=pending", "处理人工任务", "回复资料外问题并保留当前会话上下文"))
        if self.user_role == "admin":
            quick_links.append(("/admin/products", "维护产品资料", "核准字段、发布版本或撤回公开页"))
            quick_links.append(("/admin/users", "管理成员账号", "创建销售/工程师账号并控制后台访问"))
        quick_html = "".join(f"<a class='card action-card' href='{href}'><h2>{title}</h2><p class='muted'>{description}</p></a>" for href, title, description in quick_links)
        pending_tasks = self.db.list_human_tasks(self.tenant_id, "pending")[:5]
        task_rows = "".join(
            f"<tr><td><a href='/admin/tasks/{task['id']}'>{esc(task['question'])}</a></td><td>{esc(task['model'] or '未指定型号')}</td><td>{esc(task['created_at'])}</td></tr>"
            for task in pending_tasks
        ) or "<tr><td colspan='3' class='muted'>暂无待处理人工任务。</td></tr>"
        records = self.db.list_sales_records(self.tenant_id)[:5]
        stage_labels = {"new": "新线索", "needs_info": "待补充", "technical_review": "技术确认中", "quote_ready": "可进入报价", "paused": "暂缓", "closed": "已关闭"}
        lead_rows = "".join(
            f"<tr><td><a href='/admin/sales/{record['id']}'>{esc(record['id'])}</a></td><td>{esc(stage_labels.get(record['stage'], record['stage'] or '未分配'))}</td><td>{esc(record['owner'] or '未分配')}</td><td>{esc(record['next_follow_up'] or '未安排')}</td></tr>"
            for record in records
        ) or "<tr><td colspan='4' class='muted'>暂无销售线索。</td></tr>"
        body = f"""
<div class='toolbar'><div><h1>管理概览</h1><div class='muted'>当前企业：示例水泵企业（演示） · 当前账号：{esc(self.actor)}（{role_label}）</div></div><div><form method='post' action='/admin/demo/seed' class='inline'><button class='secondary'>加载演示数据</button></form> <form method='post' action='/admin/lowara/seed' class='inline'><button class='secondary'>加载 Lowara 资料</button></form> <a class='button secondary' href='/logout'>退出登录</a></div></div>
{demo_notice}
<section class='metric-grid'>{cards}</section>
<section class='grid' style='margin-top:18px'>{quick_html}</section>
<div class='grid' style='margin-top:18px'><section class='card'><h2>待处理人工任务</h2><table><thead><tr><th>问题</th><th>型号</th><th>创建时间</th></tr></thead><tbody>{task_rows}</tbody></table></section><section class='card'><h2>最近销售线索</h2><table><thead><tr><th>记录</th><th>阶段</th><th>负责人</th><th>跟进日期</th></tr></thead><tbody>{lead_rows}</tbody></table></section></div>
<div class='footer-note'>概览只读取当前登录账号所属企业的数据；具体修改仍需进入对应模块，并按角色权限执行。</div>
"""
        self.send_html(layout("管理概览", body), 200)

    def admin_users(self, query: dict[str, list[str]]) -> None:
        role_labels = {"admin": "管理员", "sales": "销售", "engineer": "工程师"}
        users = self.db.list_users(self.tenant_id)
        rows = []
        for user in users:
            active = bool(user["active"])
            status = "启用" if active else "已停用"
            action = "停用" if active else "重新启用"
            action_html = (
                "<span class='muted'>当前账号</span>"
                if user["id"] == self.user_id
                else f"<form method='post' action='/admin/users/{esc(user['id'])}/toggle' class='inline'><button class='{'danger' if active else 'secondary'}'>{action}</button></form>"
            )
            rows.append(
                f"<tr><td><b>{esc(user['display_name'])}</b><br><span class='muted'>{esc(user['email'])}</span></td>"
                f"<td>{esc(role_labels.get(user['role'], user['role']))}</td><td><span class='badge {'approved' if active else 'withdrawn'}'>{status}</span></td>"
                f"<td>{esc(user['created_at'])}</td><td>{action_html}</td></tr>"
            )
        notice = {
            "created": "<div class='notice'>成员账号已创建，可以使用新邮箱和密码登录。</div>",
            "updated": "<div class='notice'>成员账号状态已更新；停用账号的现有会话已失效。</div>",
        }.get(query.get("notice", [""])[0], "")
        body = f"""
<div class='toolbar'><div><h1>成员管理</h1><div class='muted'>管理当前企业的后台账号和最小角色权限。</div></div><a class='button secondary' href='/admin'>返回管理概览</a></div>
{notice}
<div class='grid'><section class='card'><h2>创建成员账号</h2><p class='muted'>密码只在创建时使用，不会在后台列表显示。生产环境应接入正式身份提供商。</p><form method='post' action='/admin/users'><label>姓名 *</label><input name='display_name' required placeholder='例如 Alex Chen'><label>邮箱 *</label><input name='email' type='email' required placeholder='alex@example.com'><label>角色 *</label><select name='role'><option value='sales'>销售</option><option value='engineer'>工程师</option><option value='admin'>管理员</option></select><label>初始密码 *</label><input name='password' type='password' minlength='8' required placeholder='至少 8 位'><p><button>创建账号</button></p></form></section><section class='card'><h2>角色说明</h2><p><b>管理员：</b>产品管理、成员管理、人工任务、RFQ 和销售工作台。</p><p><b>销售：</b>RFQ 和销售工作台。</p><p><b>工程师：</b>人工任务、RFQ 和销售工作台。</p><p class='footer-note'>账号属于当前企业；停用后不能继续登录，也不会改变历史 RFQ、任务或操作记录。</p></section></div>
<section class='card' style='margin-top:18px'><h2>当前成员</h2><table><thead><tr><th>成员</th><th>角色</th><th>状态</th><th>创建时间</th><th>操作</th></tr></thead><tbody>{''.join(rows) or "<tr><td colspan='5' class='muted'>暂无成员。</td></tr>"}</tbody></table></section>
"""
        self.send_html(layout("成员管理", body), 200)

    def admin_tasks(self, query: dict[str, list[str]]) -> None:
        tasks = self.db.list_human_tasks(self.tenant_id, query.get("status", [""])[0] or None)
        rows = "".join(
            f"<tr><td><a href='/admin/tasks/{task['id']}'>{esc(task['question'])}</a></td><td>{esc(task['model'] or '未指定型号')}</td><td><span class='badge'>{esc(task['status'])}</span></td><td>{esc(task['created_at'])}</td></tr>"
            for task in tasks
        ) or "<tr><td colspan='4' class='muted'>暂无人工任务。资料外问题或无法确认的问题会出现在这里。</td></tr>"
        body = f"""
<div class='toolbar'><div><h1>人工任务</h1><div class='muted'>只处理当前演示企业的会话问题；工程师回复不会自动写入产品通用知识。</div></div><div><a class='button secondary' href='/admin/tasks?status=pending'>待处理</a> <a class='button secondary' href='/admin/tasks'>全部</a></div></div>
<section class='card'><table><thead><tr><th>问题</th><th>型号</th><th>状态</th><th>创建时间</th></tr></thead><tbody>{rows}</tbody></table></section>
"""
        self.send_html(layout("人工任务", body), 200)

    def admin_task(self, task_id: str, query: dict[str, list[str]]) -> None:
        task = self.db.get_human_task(self.tenant_id, task_id)
        if not task:
            self.send_html(layout("任务不存在", "<div class='error'>人工任务不存在或不属于当前企业。</div>"), 404)
            return
        context = json.loads(task["context_json"])
        requirements = context.get("confirmed_requirements", {})
        requirement_rows = "".join(f"<tr><td>{esc(item.get('label', key))}</td><td>{esc(item.get('value', ''))} {esc(item.get('unit', ''))}</td><td>{esc(item.get('state', ''))}</td></tr>" for key, item in requirements.items()) or "<tr><td colspan='3' class='muted'>暂无已确认工况</td></tr>"
        notice = "<div class='notice'>人工任务已回复，买方会话中已保存工程师回复。</div>" if query.get("notice", [""])[0] == "resolved" else ""
        response_area = f"<p><b>当前回复：</b>{esc(task['response'])}</p>" if task["response"] else f"<form method='post' action='/admin/tasks/{task_id}/reply'><label>工程师回复 *</label><textarea name='response' required placeholder='只回答本次询盘，并说明依据或仍需确认的边界。'></textarea><p><button>提交回复并关闭任务</button></p></form>"
        body = f"""
<div class='toolbar'><div><a href='/admin/tasks'>← 返回人工任务</a><h1>人工接管任务</h1><div class='muted'>状态：{esc(task['status'])} · 会话：{esc(task['conversation_id'])}</div></div></div>
{notice}
<div class='grid'><section class='card'><h2>问题上下文</h2><p><b>原问题：</b>{esc(task['question'])}</p><p><b>型号：</b>{esc(task['model'] or '未指定')}</p><p><b>转人工原因：</b>{esc(task['reason'])}</p><p><b>已有回答：</b>{esc(context.get('answer_so_far', ''))}</p></section><section class='card'><h2>已确认工况</h2><table><thead><tr><th>字段</th><th>值</th><th>状态</th></tr></thead><tbody>{requirement_rows}</tbody></table></section></div>
<section class='card' style='margin-top:18px'><h2>工程师处理</h2>{response_area}<p class='muted'>该回复只属于当前会话；如果要成为通用资料，必须回到产品资料核准流程。</p></section>
"""
        self.send_html(layout("人工接管任务", body), 200)

    def admin_rfqs(self, query: dict[str, list[str]]) -> None:
        rfqs = self.db.list_rfqs(self.tenant_id)
        rows = "".join(
            f"<tr><td><a href='/admin/rfqs/{rfq['id']}'>{esc(rfq['id'])}</a></td><td>{esc(rfq['model'] or '未指定型号')}</td><td>{esc(rfq['contact_email'] or '未提供')}</td><td><span class='badge'>{esc(rfq['status'])}</span></td><td>{esc(rfq['created_at'])}</td></tr>"
            for rfq in rfqs
        ) or "<tr><td colspan='5' class='muted'>暂无 RFQ。买方提交询价后会出现在这里。</td></tr>"
        body = f"""
<div class='toolbar'><div><h1>RFQ 询价记录</h1><div class='muted'>当前仅展示所属企业记录；完整负责人、阶段和跟进操作将在销售工作台切片中加入。</div></div></div>
<section class='card'><table><thead><tr><th>记录编号</th><th>型号</th><th>联系人</th><th>状态</th><th>提交时间</th></tr></thead><tbody>{rows}</tbody></table></section>
"""
        self.send_html(layout("RFQ 询价记录", body), 200)

    def admin_rfq(self, rfq_id: str, query: dict[str, list[str]]) -> None:
        rfq = self.db.get_rfq(self.tenant_id, rfq_id)
        if not rfq:
            self.send_html(layout("RFQ 不存在", "<div class='error'>RFQ 不存在或不属于当前企业。</div>"), 404)
            return
        snapshot = json.loads(rfq["snapshot_json"])
        requirements = snapshot.get("requirements", {})
        requirement_rows = "".join(f"<tr><td>{esc(item.get('label', key))}</td><td>{esc(item.get('value', ''))} {esc(item.get('unit', ''))}</td><td>{esc(item.get('state', ''))}</td></tr>" for key, item in requirements.items()) or "<tr><td colspan='3' class='muted'>暂无需求快照</td></tr>"
        missing = json.loads(rfq["missing_json"])
        missing_html = f"<div class='error'>待补充：{esc('、'.join(missing))}</div>" if missing else "<div class='notice'>提交字段完整，仍不等于系统自动承诺可报价。</div>"
        body = f"""
<div class='toolbar'><div><a href='/admin/rfqs'>← 返回 RFQ 列表</a><h1>RFQ {esc(rfq['id'])}</h1><div class='muted'>状态：{esc(rfq['status'])} · 会话：{esc(rfq['conversation_id'])}</div></div></div>
{missing_html}
<div class='grid'><section class='card'><h2>联系人（后台可见）</h2><p><b>姓名：</b>{esc(rfq['contact_name']) or '未提供'}</p><p><b>公司：</b>{esc(rfq['company']) or '未提供'}</p><p><b>邮箱：</b>{esc(rfq['contact_email']) or '未提供'}</p><p><b>电话：</b>{esc(rfq['contact_phone']) or '未提供'}</p></section><section class='card'><h2>询价信息</h2><p><b>型号：</b>{esc(rfq['model'] or '未指定')}</p><p><b>数量：</b>{esc(rfq['quantity']) or '待补充'}</p><p><b>目的地：</b>{esc(rfq['destination']) or '待补充'}</p><p><b>期望交期：</b>{esc(rfq['requested_delivery']) or '待补充'}</p><p><b>补充说明：</b>{esc(rfq['notes']) or '无'}</p></section></div>
<section class='card' style='margin-top:18px'><h2>已确认需求快照</h2><table><thead><tr><th>字段</th><th>值</th><th>状态</th></tr></thead><tbody>{requirement_rows}</tbody></table></section>
<div class='footer-note'>该 RFQ 保存了提交时的需求、推荐和产品版本快照；后续产品更新不会静默改写这条历史询价。</div>
"""
        self.send_html(layout("RFQ 详情", body), 200)

    def admin_sales(self, query: dict[str, list[str]]) -> None:
        records = self.db.list_sales_records(self.tenant_id)
        today = datetime.now(timezone.utc).date().isoformat()
        stage_labels = {"new": "新线索", "needs_info": "待补充", "technical_review": "技术确认中", "quote_ready": "可进入报价", "paused": "暂缓", "closed": "已关闭"}
        rows = []
        for record in records:
            overdue = bool(record["next_follow_up"] and record["next_follow_up"] < today and record["stage"] not in ("closed", "paused"))
            follow_up = f"<span class='badge withdrawn'>逾期 {esc(record['next_follow_up'])}</span>" if overdue else esc(record["next_follow_up"] or "未安排")
            rows.append(
                f"<tr><td><a href='/admin/sales/{record['id']}'>{esc(record['id'])}</a><br><span class='muted'>{esc(record['company'] or record['contact_email'] or '未提供联系人')}</span></td>"
                f"<td>{esc(record['model'] or '未指定型号')}</td><td><span class='badge'>{esc(stage_labels.get(record['stage'], record['stage'] or '未分配'))}</span></td>"
                f"<td>{esc(record['owner'] or '未分配')}</td><td>{follow_up}</td><td>{esc(record['status'])}</td></tr>"
            )
        table = "".join(rows) or "<tr><td colspan='6' class='muted'>暂无销售线索。买方提交 RFQ 后会进入这里。</td></tr>"
        body = f"""
<div class='toolbar'><div><h1>销售工作台</h1><div class='muted'>统一查看 RFQ、需求、推荐、问答和人工任务；当前企业：示例水泵企业（演示）</div></div><a class='button secondary' href='/admin/rfqs'>RFQ 原始记录</a></div>
<section class='card'><table><thead><tr><th>线索 / 联系人</th><th>型号</th><th>阶段</th><th>负责人</th><th>跟进日期</th><th>RFQ 状态</th></tr></thead><tbody>{table}</tbody></table></section>
"""
        self.send_html(layout("销售工作台", body), 200)

    def admin_sales_detail(self, rfq_id: str, query: dict[str, list[str]]) -> None:
        rfq = self.db.get_rfq(self.tenant_id, rfq_id)
        lead = self.db.get_lead_for_rfq(self.tenant_id, rfq_id)
        if not rfq or not lead:
            self.send_html(layout("线索不存在", "<div class='error'>线索不存在或不属于当前企业。</div>"), 404)
            return
        snapshot = json.loads(rfq["snapshot_json"])
        requirements = snapshot.get("requirements", {})
        recommendation = snapshot.get("recommendation") or {}
        requirement_rows = "".join(f"<tr><td>{esc(item.get('label', key))}</td><td>{esc(item.get('value', ''))} {esc(item.get('unit', ''))}</td><td>{esc(item.get('state', ''))}</td></tr>" for key, item in requirements.items()) or "<tr><td colspan='3' class='muted'>暂无需求快照</td></tr>"
        answers = self.db.list_answers(self.tenant_id, rfq["conversation_id"])
        answer_rows = "".join(f"<div class='source'><span><b>{'有据回答' if answer['answer_status'] == 'grounded' else '人工接管'}</b><br>{esc(answer['answer_text'])}</span><span class='field-state'>{esc(answer['created_at'])}</span></div>" for answer in answers) or "<p class='muted'>暂无问答记录。</p>"
        tasks = [task for task in self.db.list_human_tasks(self.tenant_id) if task["conversation_id"] == rfq["conversation_id"]]
        task_rows = "".join(f"<div class='source'><span><b>{esc(task['status'])}</b><br>{esc(task['question'])}<br><span class='field-state'>{esc(task['reason'])}</span></span><a href='/admin/tasks/{task['id']}'>处理</a></div>" for task in tasks) or "<p class='muted'>暂无人工任务。</p>"
        audit_rows = self.db.list_entity_audit(self.tenant_id, [rfq["id"], lead["id"]])
        audit_html = "".join(f"<div class='field-state'>{esc(item['created_at'])} · {esc(item['action'])} · {esc(item['actor'])} · {esc(item['details'])}</div>" for item in audit_rows) or "<p class='muted'>暂无操作记录。</p>"
        stage_options = "".join(f"<option value='{key}' {'selected' if lead['stage'] == key else ''}>{label}</option>" for key, label in {"new": "新线索", "needs_info": "待补充", "technical_review": "技术确认中", "quote_ready": "可进入报价", "paused": "暂缓", "closed": "已关闭"}.items())
        notice = "<div class='notice'>销售状态已保存，刷新后仍会保留。</div>" if query.get("notice", [""])[0] == "updated" else ""
        if recommendation:
            recommendation_html = f"<p><b>推荐型号：</b>{esc(recommendation.get('model'))}</p><p><b>产品版本：</b>v{esc(recommendation.get('product_version'))}</p><p><b>满足项：</b>{esc('；'.join(recommendation.get('satisfies', [])) or '—')}</p><p><b>冲突项：</b>{esc('；'.join(recommendation.get('conflicts', [])) or '—')}</p><a href='{esc(recommendation.get('detail_url', '#'))}' target='_blank'>打开产品页</a>"
        else:
            recommendation_html = "<p class='muted'>暂无推荐快照。</p>"
        body = f"""
<div class='toolbar'><div><a href='/admin/sales'>← 返回销售工作台</a><h1>销售线索 {esc(rfq['id'])}</h1><div class='muted'>RFQ 状态：{esc(rfq['status'])} · 会话：{esc(rfq['conversation_id'])}</div></div></div>
{notice}
<div class='grid'><section class='card'><h2>线索处理</h2><form method='post' action='/admin/sales/{rfq_id}/update'><label>阶段</label><select name='stage'>{stage_options}</select><label>负责人</label><input name='owner' value='{esc(lead['owner'] or '')}' placeholder='例如 Alex / 售前工程师'><label>下一步跟进日期</label><input type='date' name='next_follow_up' value='{esc(lead['next_follow_up'] or '')}'><label>销售备注</label><textarea name='note' placeholder='记录下一步和客户背景'>{esc(lead['note'])}</textarea><p><button>保存跟进状态</button></p></form></section><section class='card'><h2>联系人与询价</h2><p><b>联系人：</b>{esc(rfq['contact_name']) or '未提供'}</p><p><b>公司：</b>{esc(rfq['company']) or '未提供'}</p><p><b>邮箱：</b>{esc(rfq['contact_email']) or '未提供'}</p><p><b>电话：</b>{esc(rfq['contact_phone']) or '未提供'}</p><p><b>型号：</b>{esc(rfq['model'] or '未指定')}</p><p><b>数量：</b>{esc(rfq['quantity']) or '待补充'} · <b>目的地：</b>{esc(rfq['destination']) or '待补充'} · <b>交期：</b>{esc(rfq['requested_delivery']) or '待补充'}</p><p><b>备注：</b>{esc(rfq['notes']) or '无'}</p></section></div>
<section class='card' style='margin-top:18px'><h2>已确认需求快照</h2><table><thead><tr><th>字段</th><th>值</th><th>状态</th></tr></thead><tbody>{requirement_rows}</tbody></table></section>
<div class='grid' style='margin-top:18px'><section class='card'><h2>推荐快照</h2>{recommendation_html}</section><section class='card'><h2>问答与人工任务</h2>{answer_rows}{task_rows}</section></div>
<section class='card' style='margin-top:18px'><h2>操作记录</h2>{audit_html}</section>
"""
        self.send_html(layout("销售线索详情", body), 200)

    def public_product(self, slug: str, query: dict[str, list[str]]) -> None:
        product = self.db.get_product_by_slug(self.tenant_id, slug)
        if not product or product["status"] != "published":
            self.send_html(layout("产品不可用", "<div class='error'>该产品页尚未发布，或已被撤回。</div><p><a href='/admin/products'>返回企业后台</a></p>"), 404)
            return
        version = self.db.get_published_version(self.tenant_id, product["id"])
        if not version:
            self.send_html(layout("产品不可用", "<div class='error'>找不到已发布版本。</div>"), 404)
            return
        snapshot = json.loads(version["snapshot_json"])
        sources = self.db.get_sources(self.tenant_id, product["id"])
        lang = query.get("lang", ["en"])[0]
        field_cards = "".join(f"<div><b>{esc(field['label'])}</b><span>{esc(field['value'])} {esc(field['unit'])}</span></div>" for field in snapshot["fields"])
        source_links = "".join(
            f"<li>{esc(source['filename'])} · {esc(source['source_location'])} "
            + (f"<a href='/products/{esc(product['slug'])}/sources/{esc(source['id'])}'>下载资料</a>" if source["storage_path"] else "<span class='muted'>仅登记元数据</span>")
            + "</li>"
            for source in sources
        ) or "<li class='muted'>暂无公开资料附件</li>"
        title = f"{product['model']} | Industrial Pump"
        body = f"""
<div class='product-hero'><div class='eyebrow'>Published product version {version['version']}</div><h1>{esc(product['model'])}</h1><p>{esc(product['name'])}</p><div class='kv'>{field_cards}</div></div>
<div class='grid'><section class='card'><h2>{'Application' if lang == 'en' else '适用场景'}</h2><p>{esc(product['use_case'])}</p><p class='muted'>{'The values above come from the approved product record and its cited source. Please confirm final sizing with an engineer.' if lang == 'en' else '以上参数来自已核准产品版本及其引用资料。正式选型前请与工程师确认。'}</p></section><section class='card'><h2>{'Source documents' if lang == 'en' else '来源资料'}</h2><ul>{source_links}</ul></section><section class='card'><h2>{'Next step' if lang == 'en' else '继续沟通'}</h2><p>{'Need help checking flow, head or medium compatibility?' if lang == 'en' else '需要核对流量、扬程或介质适用条件？'}</p><a class='button' href='mailto:sales@example.invalid?subject={quote(product['model'])}'>Ask sales / 询价</a></section></div>
<div class='footer-note'>{'This is an initial technical display, not a price, stock, delivery or engineering-fit commitment.' if lang == 'en' else '这是初步技术展示，不代表价格、库存、交期或正式工程适配承诺。'} · <a href='/products/{esc(product['slug'])}?lang={'zh' if lang == 'en' else 'en'}'>{'切换中文' if lang == 'en' else 'Switch to English'}</a></div>
"""
        page = layout(title, body, lang=lang)
        json_ld = json.dumps({'@context': 'https://schema.org', '@type': 'Product', 'name': product['name'], 'model': product['model']}, ensure_ascii=False).replace("</", "<\\/")
        page = page.replace("</head>", f"<meta name='description' content='{esc(product['name'])} - approved industrial pump specifications'><script type='application/ld+json'>{json_ld}</script></head>")
        self.send_html(page, 200)

    def public_source(self, slug: str, source_id: str) -> None:
        product = self.db.get_product_by_slug(self.tenant_id, slug)
        if not product or product["status"] != "published":
            self.send_html(layout("资料不可用", "<div class='error'>产品未公开。</div>"), 404)
            return
        source = self.db.conn.execute(
            "SELECT * FROM source_documents WHERE tenant_id=? AND product_id=? AND id=?",
            (self.tenant_id, product["id"], source_id),
        ).fetchone()
        if not source or not source["storage_path"]:
            self.send_html(layout("资料不可用", "<div class='error'>该来源资料没有可下载文件。</div>"), 404)
            return
        target = ROOT / source["storage_path"]
        if not target.is_file():
            self.send_html(layout("资料不可用", "<div class='error'>文件存储不可用，请联系管理员。</div>"), 404)
            return
        content_type = mimetypes.guess_type(source["filename"])[0] or "application/octet-stream"
        self.send_bytes(target.read_bytes(), content_type, source["filename"])


def make_server(db_path: str | os.PathLike[str], host: str = "127.0.0.1", port: int = 8000) -> ThreadingHTTPServer:
    database = Database(db_path)

    class AppHandler(Handler):
        db = database

    return ThreadingHTTPServer((host, port), AppHandler)


def main() -> None:
    parser = argparse.ArgumentParser(description="Growave MVP")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--db", default=str(DEFAULT_DB))
    args = parser.parse_args()
    server = make_server(args.db, args.host, args.port)
    print(f"Growave running at http://{args.host}:{args.port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
