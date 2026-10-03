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
from datetime import datetime, timedelta, timezone
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
    "dimension": "安装尺寸",
}
FIELD_LABELS_EN = {
    "flow_range": "Flow range", "head_range": "Head range", "media": "Suitable medium",
    "material": "Material", "power": "Power", "temperature": "Liquid temperature",
    "performance_points": "Performance points", "dimension": "Installation dimensions",
}
PRODUCT_PAGE_GROUPS = (
    ("Basics and application", "基本与用途", ("media", "temperature")),
    ("Hydraulic performance", "水力性能", ("flow_range", "head_range", "performance_points")),
    ("Power and materials", "电气与材料", ("power", "material")),
    ("Installation and dimensions", "安装尺寸", ("dimension",)),
)
# File attribution scope for a source document. Only files explicitly attributed
# to this model (exact_model) or its series (series) may be approved for public
# download; unfiled documents never appear on a public page.
SOURCE_SCOPES = {"exact_model": "本型号", "series": "系列", "unknown": "归属待核对"}
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".avif"}
CAD_3D_EXTENSIONS = {".step", ".stp", ".stl", ".iges", ".igs", ".obj", ".3mf", ".sldprt", ".sldasm"}
CAD_2D_EXTENSIONS = {".dxf", ".dwg"}
# Binary or layout-heavy formats that the dependency-free local environment cannot
# parse reliably. They must fail extraction explicitly instead of guessing values.
UNSUPPORTED_EXTRACTION_EXTENSIONS = {
    ".pdf": "PDF 版式解析不在当前本地环境内；请打开文件后按页码人工录入字段",
    ".xlsx": "Excel 文件需要人工打开核对；请人工录入字段并标注单元格位置",
    ".xls": "Excel 文件需要人工打开核对；请人工录入字段并标注单元格位置",
    ".doc": "Office 文档需要人工打开核对；请人工录入字段",
    ".docx": "Office 文档需要人工打开核对；请人工录入字段",
    ".dwg": "CAD 二进制文件不做参数抽取；人工录入参数后可用作本型号文件",
    ".dxf": "二维图纸不做自动参数抽取；请人工录入参数并标注出处",
    ".step": "三维模型不做参数抽取；人工录入参数后可用作本型号文件",
    ".stp": "三维模型不做参数抽取；人工录入参数后可用作本型号文件",
    ".stl": "三维模型不做参数抽取；人工录入参数后可用作本型号文件",
    ".iges": "三维模型不做参数抽取；人工录入参数后可用作本型号文件",
    ".igs": "三维模型不做参数抽取；人工录入参数后可用作本型号文件",
    ".zip": "压缩包需要解压后逐份核对；请先解压再上传各文件",
    ".rar": "压缩包需要解压后逐份核对；请先解压再上传各文件",
}
# Label synonyms accepted by the content-based extractor. Matching is conservative:
# the label part of a line must equal one of these after whitespace/punctuation
# normalization, so ambiguous phrases like “最大流量” are never guessed as a field.
FIELD_SYNONYMS = {
    "flow_range": ("流量范围", "流量", "flow range", "flow", "flow rate", "rated flow", "delivery flow"),
    "head_range": ("扬程范围", "扬程", "head range", "head", "rated head", "delivery head"),
    "media": ("介质适用条件", "适用介质", "介质", "medium", "media", "suitable medium"),
    "material": ("材质", "材料", "material", "construction", "body material"),
    "power": ("功率", "电机功率", "power", "motor power", "rated power", "shaft power"),
    "temperature": ("介质温度", "液体温度", "温度", "temperature", "liquid temperature", "temp"),
    "performance_points": ("性能曲线目录点", "性能曲线", "性能点", "performance points", "performance curve", "curve points"),
    "dimension": ("安装尺寸", "外形尺寸", "尺寸", "dimensions", "installation dimensions", "overall dimensions"),
}
_NUMERIC_FIELDS = {"flow_range", "head_range", "power", "temperature", "dimension"}


def normalize_field_label(text: str) -> str:
    cleaned = re.sub(r"[（(【\[][^）)】\]]*[）)】\]]", "", text or "")
    cleaned = re.sub(r"[\s：:，,。.;；=＝\-_/\\]+", "", cleaned)
    return cleaned.lower()


def extract_candidate_items(content: bytes, filename: str) -> tuple[str, list[dict[str, str]], str]:
    """Extract field candidates from file content only, never from the filename.

    Returns (processing_status, items, note) where status is one of
    ``extracted`` / ``extraction_failed`` / ``extraction_unsupported``.
    """
    suffix = Path(filename or "").suffix.lower()
    if suffix in UNSUPPORTED_EXTRACTION_EXTENSIONS:
        return "extraction_unsupported", [], f"{suffix} 格式不支持自动抽取：{UNSUPPORTED_EXTRACTION_EXTENSIONS[suffix]}。"
    if not content:
        return "extraction_unsupported", [], "没有可解析的文件内容；请人工录入字段。"
    if b"\x00" in content[:8192]:
        return "extraction_unsupported", [], "文件包含二进制内容，当前环境不做自动抽取；请人工录入字段并标注出处。"
    text = None
    for encoding in ("utf-8-sig", "utf-8", "gb18030"):
        try:
            text = content.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    if text is None:
        return "extraction_unsupported", [], "文件无法按文本解码，当前环境不做自动抽取；请人工录入字段。"
    items: list[dict[str, str]] = []
    for index, raw_line in enumerate(text.splitlines(), start=1):
        line = raw_line.strip().strip("|").strip()
        if not line:
            continue
        label, value_text, unit_hint = _split_candidate_line(line)
        if not label:
            continue
        field_key = _match_field_key(label)
        if not field_key:
            continue
        value, unit = _split_value_unit(field_key, value_text)
        if unit_hint and not unit:
            unit = unit_hint
        if not value:
            continue
        items.append({
            "field_key": field_key,
            "label": FIELD_LABELS[field_key],
            "value": value,
            "unit": unit,
            "location": f"第 {index} 行",
            "raw": raw_line.strip(),
        })
    if not items:
        return "extraction_failed", [], "未识别到已知字段标签（支持“标签: 值 单位”或“标签,值,单位”，标签如流量范围/扬程范围/介质/材质/功率/介质温度/安装尺寸）。"
    return "extracted", items, ""


def _split_candidate_line(line: str) -> tuple[str, str, str]:
    for separator in ("\t", ",", "；", ";"):
        if separator in line:
            parts = [part.strip() for part in line.split(separator)]
            if len(parts) >= 2 and parts[0]:
                return parts[0], parts[1], parts[2] if len(parts) >= 3 else ""
    for separator in ("：", ":", "=", "＝"):
        if separator in line:
            label, _, rest = line.partition(separator)
            return label.strip(), rest.strip(), ""
    return "", "", ""


def _match_field_key(label: str) -> str | None:
    normalized = normalize_field_label(label)
    if not normalized:
        return None
    for field_key, synonyms in FIELD_SYNONYMS.items():
        if any(normalized == normalize_field_label(synonym) for synonym in synonyms):
            return field_key
    return None


def _split_value_unit(field_key: str, raw: str) -> tuple[str, str]:
    raw = (raw or "").strip().strip("：: ")
    if not raw:
        return "", ""
    if field_key not in _NUMERIC_FIELDS:
        return raw, ""
    match = re.match(
        r"^(-?\d+(?:[.,]\d+)?(?:\s*(?:-|~|至|到|to)\s*-?\d+(?:[.,]\d+)?)?)\s*(\S.*)?$",
        raw,
        flags=re.IGNORECASE,
    )
    if not match:
        return raw, ""
    value = match.group(1).strip()
    unit = (match.group(2) or "").strip()
    if len(unit) > 16:
        return raw, ""
    return value, unit


class ConflictError(ValueError):
    pass


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def esc(value: object) -> str:
    return html.escape("" if value is None else str(value), quote=True)


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
    def __init__(self, path: str | os.PathLike[str], *, initialize: bool = True):
        self.path = str(path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.execute("PRAGMA busy_timeout = 5000")
        if initialize:
            self.init_schema()

    def init_schema(self) -> None:
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS tenants (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS buyer_sites (
                site_key TEXT PRIMARY KEY,
                tenant_id TEXT NOT NULL REFERENCES tenants(id),
                allowed_origin TEXT NOT NULL,
                active INTEGER NOT NULL DEFAULT 1,
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
                scope TEXT NOT NULL DEFAULT 'unknown',
                scope_revision INTEGER NOT NULL DEFAULT 0,
                extraction_json TEXT NOT NULL DEFAULT '[]',
                extraction_note TEXT NOT NULL DEFAULT '',
                processed_at TEXT,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS public_source_permissions (
                source_id TEXT PRIMARY KEY REFERENCES source_documents(id),
                tenant_id TEXT NOT NULL REFERENCES tenants(id),
                product_id TEXT NOT NULL REFERENCES products(id),
                approved_by TEXT NOT NULL,
                approved_at TEXT NOT NULL
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
                origin TEXT NOT NULL DEFAULT 'manual',
                review_note TEXT NOT NULL DEFAULT '',
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
                stop_proactive_contact INTEGER NOT NULL DEFAULT 0,
                stop_reason TEXT NOT NULL DEFAULT '',
                stop_source TEXT NOT NULL DEFAULT '',
                stopped_at TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS buyer_conversation_access (
                conversation_id TEXT PRIMARY KEY REFERENCES conversations(id),
                tenant_id TEXT NOT NULL REFERENCES tenants(id),
                token_hash TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS messages (
                id TEXT PRIMARY KEY,
                tenant_id TEXT NOT NULL REFERENCES tenants(id),
                conversation_id TEXT NOT NULL REFERENCES conversations(id),
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                source TEXT NOT NULL DEFAULT 'buyer_web',
                idempotency_key TEXT,
                request_hash TEXT,
                result_revision_id TEXT,
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
                idempotency_key TEXT,
                request_hash TEXT,
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
                tentative_viewed INTEGER NOT NULL DEFAULT 0,
                idempotency_key TEXT,
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
                request_hash TEXT,
                current_revision INTEGER NOT NULL DEFAULT 1,
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
            CREATE TABLE IF NOT EXISTS rfq_revisions (
                id TEXT PRIMARY KEY,
                tenant_id TEXT NOT NULL REFERENCES tenants(id),
                rfq_id TEXT NOT NULL REFERENCES rfqs(id),
                revision INTEGER NOT NULL,
                content_json TEXT NOT NULL,
                actor TEXT NOT NULL,
                idempotency_key TEXT,
                request_hash TEXT,
                created_at TEXT NOT NULL,
                UNIQUE (rfq_id, revision),
                UNIQUE (rfq_id, idempotency_key)
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
        if "request_hash" not in {row["name"] for row in self.conn.execute("PRAGMA table_info(rfqs)")}:
            self.conn.execute("ALTER TABLE rfqs ADD COLUMN request_hash TEXT")
        if "current_revision" not in {row["name"] for row in self.conn.execute("PRAGMA table_info(rfqs)")}:
            self.conn.execute("ALTER TABLE rfqs ADD COLUMN current_revision INTEGER NOT NULL DEFAULT 1")
        source_columns = {row["name"] for row in self.conn.execute("PRAGMA table_info(source_documents)")}
        for column, definition in (
            ("scope", "TEXT NOT NULL DEFAULT 'unknown'"),
            ("scope_revision", "INTEGER NOT NULL DEFAULT 0"),
            ("extraction_json", "TEXT NOT NULL DEFAULT '[]'"),
            ("extraction_note", "TEXT NOT NULL DEFAULT ''"),
            ("processed_at", "TEXT"),
        ):
            if column not in source_columns:
                self.conn.execute(f"ALTER TABLE source_documents ADD COLUMN {column} {definition}")
        field_columns = {row["name"] for row in self.conn.execute("PRAGMA table_info(product_fields)")}
        for column, definition in (
            ("origin", "TEXT NOT NULL DEFAULT 'manual'"),
            ("review_note", "TEXT NOT NULL DEFAULT ''"),
        ):
            if column not in field_columns:
                self.conn.execute(f"ALTER TABLE product_fields ADD COLUMN {column} {definition}")
        message_columns = {row["name"] for row in self.conn.execute("PRAGMA table_info(messages)")}
        for column, definition in (
            ("idempotency_key", "TEXT"),
            ("request_hash", "TEXT"),
            ("result_revision_id", "TEXT"),
        ):
            if column not in message_columns:
                self.conn.execute(f"ALTER TABLE messages ADD COLUMN {column} {definition}")
        recommendation_columns = {row["name"] for row in self.conn.execute("PRAGMA table_info(recommendations)")}
        if "tentative_viewed" not in recommendation_columns:
            self.conn.execute("ALTER TABLE recommendations ADD COLUMN tentative_viewed INTEGER NOT NULL DEFAULT 0")
        if "idempotency_key" not in recommendation_columns:
            self.conn.execute("ALTER TABLE recommendations ADD COLUMN idempotency_key TEXT")
        revision_columns = {row["name"] for row in self.conn.execute("PRAGMA table_info(requirement_revisions)")}
        for column in ("idempotency_key", "request_hash"):
            if column not in revision_columns:
                self.conn.execute(f"ALTER TABLE requirement_revisions ADD COLUMN {column} TEXT")
        self.conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_revisions_idempotency ON requirement_revisions(conversation_id, idempotency_key) WHERE idempotency_key IS NOT NULL"
        )
        self.conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_recommendations_idempotency ON recommendations(conversation_id, idempotency_key) WHERE idempotency_key IS NOT NULL"
        )
        try:
            self.conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_answers_question ON answers(question_message_id)")
        except sqlite3.Error:
            # 旧库可能存在历史重复行；代码路径仍先查后写，索引仅为并发兜底。
            pass
        self.conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_messages_idempotency ON messages(conversation_id, idempotency_key) WHERE idempotency_key IS NOT NULL"
        )
        try:
            # 同一未解问题只允许一个对外人工任务；并发请求由索引兜底，重复由 create_human_task 返回既有任务。
            self.conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_human_tasks_question ON human_tasks(question_message_id)")
        except sqlite3.IntegrityError:
            pass  # 旧库若已有重复任务则保留原样；并发防护不再由索引兜底，需在迁移时人工核查
        conversation_columns = {row["name"] for row in self.conn.execute("PRAGMA table_info(conversations)")}
        for column, definition in (
            ("stop_proactive_contact", "INTEGER NOT NULL DEFAULT 0"),
            ("stop_reason", "TEXT NOT NULL DEFAULT ''"),
            ("stop_source", "TEXT NOT NULL DEFAULT ''"),
            ("stopped_at", "TEXT"),
        ):
            if column not in conversation_columns:
                self.conn.execute(f"ALTER TABLE conversations ADD COLUMN {column} {definition}")
        for rfq in self.conn.execute("SELECT * FROM rfqs WHERE NOT EXISTS (SELECT 1 FROM rfq_revisions WHERE rfq_id=rfqs.id)").fetchall():
            self.conn.execute(
                "INSERT INTO rfq_revisions(id, tenant_id, rfq_id, revision, content_json, actor, created_at) VALUES (?, ?, ?, 1, ?, 'legacy-import', ?)",
                (secrets.token_hex(12), rfq["tenant_id"], rfq["id"], json.dumps(self.rfq_current_content(rfq), ensure_ascii=False), rfq["created_at"]),
            )
        # Ordinary initialization must not create any tenant, let alone predictable
        # users. Demo state comes only from the explicit demo command
        # (tools/demo/seed_demo.py -> seed_demo_users); a production database
        # provisions its first administrator via tools/create_admin.py.
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

    def register_buyer_site(self, tenant_id: str, site_key: str, allowed_origin: str) -> None:
        parsed = urlparse(allowed_origin.strip())
        local_http = parsed.scheme == "http" and parsed.hostname in {"localhost", "127.0.0.1"}
        if not site_key.strip() or not parsed.hostname or (parsed.scheme != "https" and not local_http) or parsed.username or parsed.password or parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
            raise ValueError("需要网站标识和允许的 HTTPS 来源")
        self.ensure_tenant(tenant_id)
        self.conn.execute(
            "INSERT INTO buyer_sites(site_key, tenant_id, allowed_origin, created_at) VALUES (?, ?, ?, ?)",
            (site_key.strip(), tenant_id, allowed_origin.rstrip("/"), now_iso()),
        )
        self.conn.commit()

    def create_buyer_site(self, tenant_id: str, allowed_origin: str) -> str:
        site_key = secrets.token_urlsafe(18)
        self.register_buyer_site(tenant_id, site_key, allowed_origin)
        return site_key

    def list_buyer_sites(self, tenant_id: str) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM buyer_sites WHERE tenant_id=? ORDER BY created_at DESC, site_key", (tenant_id,)
        ).fetchall()

    def get_buyer_site(self, site_key: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM buyer_sites WHERE site_key=? AND active=1", (site_key,)
        ).fetchone()

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
        """Explicit demo-only bootstrap: the demo tenant plus three documented logins.

        Never called from ``init_schema`` — an ordinary new database must not contain
        predictable accounts. The only production-like callers are the demo command
        (``tools/demo/seed_demo.py``, used by ``start_demo.py``) and explicit test
        setup; a real deployment provisions its first administrator through
        ``tools/create_admin.py`` instead.
        """
        self.ensure_tenant("demo-tenant", "示例水泵企业（演示）")
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
        self.conn.commit()

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

    def public_source_ids(self, tenant_id: str, product_id: str) -> set[str]:
        return {row["source_id"] for row in self.conn.execute(
            "SELECT source_id FROM public_source_permissions WHERE tenant_id=? AND product_id=?",
            (tenant_id, product_id),
        )}

    def public_downloadable_ids(self, tenant_id: str, product_id: str) -> set[str]:
        """Source ids the current published version may actually expose to the public.

        Three conditions must hold: the file is listed in the current published
        snapshot, it still has a live public-download permission, and its live
        attribution state matches what that snapshot captured at publish time —
        both the scope label (“本型号/系列”) and the monotonic ``scope_revision``
        that every real scope change advances. Comparing the revision as well means
        a file whose scope was corrected and later edited back to the same words
        can never resume service from the stale snapshot (the text matches but the
        revision does not). Renewed public display therefore requires a fresh
        permission approval plus a new published version (see set_source_scope).
        """
        version = self.get_published_version(tenant_id, product_id)
        if not version:
            return set()
        # Snapshots published before revision capture default to revision 0: any
        # later scope change bumps the live revision, so those old versions are
        # still invalidated by the comparison below.
        snapshot_state = {
            entry["id"]: (entry.get("scope"), entry.get("scope_revision", 0))
            for entry in json.loads(version["snapshot_json"]).get("public_sources", [])
        }
        allowed = self.public_source_ids(tenant_id, product_id)
        live_state = {
            row["id"]: (row["scope"], row["scope_revision"])
            for row in self.conn.execute(
                "SELECT id, scope, scope_revision FROM source_documents WHERE tenant_id=? AND product_id=?",
                (tenant_id, product_id),
            )
        }
        return {
            source_id
            for source_id, snapshot in snapshot_state.items()
            if source_id in allowed and live_state.get(source_id) == snapshot
        }

    def allow_public_source(self, tenant_id: str, product_id: str, source_id: str, actor: str) -> None:
        source = self.conn.execute(
            "SELECT id, storage_path, scope FROM source_documents WHERE tenant_id=? AND product_id=? AND id=?",
            (tenant_id, product_id, source_id),
        ).fetchone()
        if not source or not source["storage_path"]:
            raise ValueError("需要已保存的来源文件才能开放下载")
        if source["scope"] == "unknown":
            raise ValueError("请先确认文件归属（本型号或系列），再开放公开下载；归属未核对的文件不上公开页")
        self.conn.execute(
            "INSERT OR REPLACE INTO public_source_permissions(source_id, tenant_id, product_id, approved_by, approved_at) VALUES (?, ?, ?, ?, ?)",
            (source_id, tenant_id, product_id, actor, now_iso()),
        )
        self._audit(tenant_id, "source_document", source_id, "allowed_public_download", actor)
        self.conn.commit()

    def revoke_public_source(self, tenant_id: str, product_id: str, source_id: str, actor: str) -> None:
        self.conn.execute(
            "DELETE FROM public_source_permissions WHERE tenant_id=? AND product_id=? AND source_id=?",
            (tenant_id, product_id, source_id),
        )
        self._audit(tenant_id, "source_document", source_id, "revoked_public_download", actor)
        self.conn.commit()

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

    def get_contact_preference(self, tenant_id: str, conversation_id: str) -> sqlite3.Row | None:
        return self.get_conversation(tenant_id, conversation_id)

    def stop_proactive_contact(self, tenant_id: str, conversation_id: str, reason: str, source: str, actor: str) -> bool:
        if not reason.strip() or source not in {"buyer_chat", "seller_record"}:
            raise ValueError("需要有效会话和买方停止联系的原话")
        timestamp = now_iso()
        with self.conn:
            self.conn.execute("BEGIN IMMEDIATE")
            conversation = self.get_conversation(tenant_id, conversation_id)
            if not conversation:
                raise ValueError("需要有效会话和买方停止联系的原话")
            if conversation["stop_proactive_contact"]:
                return False
            self.conn.execute(
                "UPDATE conversations SET stop_proactive_contact=1, stop_reason=?, stop_source=?, stopped_at=?, updated_at=? WHERE tenant_id=? AND id=?",
                (reason.strip(), source, timestamp, timestamp, tenant_id, conversation_id),
            )
            self.conn.execute("UPDATE leads SET next_follow_up=NULL, updated_at=? WHERE tenant_id=? AND conversation_id=?", (timestamp, tenant_id, conversation_id))
            if source == "buyer_chat":
                self.save_message(tenant_id, conversation_id, "buyer", reason, "contact-stop", commit=False)
            self._audit(tenant_id, "conversation", conversation_id, "stop_proactive_contact", actor, f"source={source};reason={reason.strip()}")
        return True

    def issue_buyer_access(self, tenant_id: str, conversation_id: str) -> str:
        if not self.get_conversation(tenant_id, conversation_id):
            raise ValueError("会话不存在")
        token = secrets.token_urlsafe(32)
        self.conn.execute(
            "INSERT INTO buyer_conversation_access(conversation_id, tenant_id, token_hash, created_at) VALUES (?, ?, ?, ?)",
            (conversation_id, tenant_id, hashlib.sha256(token.encode()).hexdigest(), now_iso()),
        )
        self.conn.commit()
        return token

    def has_buyer_access(self, tenant_id: str, conversation_id: str, token: str | None) -> bool:
        if not token:
            return False
        row = self.conn.execute(
            "SELECT token_hash FROM buyer_conversation_access WHERE tenant_id=? AND conversation_id=?",
            (tenant_id, conversation_id),
        ).fetchone()
        return bool(row and hmac.compare_digest(row["token_hash"], hashlib.sha256(token.encode()).hexdigest()))

    def list_messages(self, tenant_id: str, conversation_id: str) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM messages WHERE tenant_id=? AND conversation_id=? ORDER BY rowid",
            (tenant_id, conversation_id),
        ).fetchall()

    def save_message(
        self,
        tenant_id: str,
        conversation_id: str,
        role: str,
        content: str,
        source: str = "buyer-web",
        commit: bool = True,
        idempotency_key: str | None = None,
        request_hash: str | None = None,
    ) -> str:
        if not self.get_conversation(tenant_id, conversation_id):
            raise ValueError("会话不存在")
        content = content.strip()
        if not content:
            raise ValueError("消息不能为空")
        message_id = secrets.token_hex(12)
        self.conn.execute(
            "INSERT INTO messages(id, tenant_id, conversation_id, role, content, source, idempotency_key, request_hash, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (message_id, tenant_id, conversation_id, role, content, source, idempotency_key, request_hash, now_iso()),
        )
        self.conn.execute("UPDATE conversations SET updated_at=? WHERE tenant_id=? AND id=?", (now_iso(), tenant_id, conversation_id))
        if commit:
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
        idempotency_key: str | None = None,
        request_hash: str | None = None,
    ) -> str:
        if not self.get_conversation(tenant_id, conversation_id):
            raise ValueError("会话不存在")
        previous = self.get_latest_revision(tenant_id, conversation_id)
        revision = (previous["revision"] if previous else 0) + 1
        missing = [key for key in MATCH_REQUIRED_FIELDS if not values.get(key, {}).get("value", "").strip()]
        revision_id = secrets.token_hex(12)
        timestamp = now_iso()
        self.conn.execute(
            "UPDATE recommendations SET status='invalidated', invalidated_at=? WHERE tenant_id=? AND conversation_id=? AND status <> 'invalidated'",
            (timestamp, tenant_id, conversation_id),
        )
        self.conn.execute(
            "INSERT INTO requirement_revisions(id, tenant_id, conversation_id, revision, status, values_json, missing_json, created_by, idempotency_key, request_hash, created_at, confirmed_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (revision_id, tenant_id, conversation_id, revision, status, json.dumps(values, ensure_ascii=False), json.dumps(missing, ensure_ascii=False), created_by, idempotency_key, request_hash, timestamp, timestamp if status == "confirmed" else None),
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
        tentative_viewed: bool = False,
        idempotency_key: str | None = None,
    ) -> str:
        self.conn.execute(
            "UPDATE recommendations SET status='invalidated', invalidated_at=? WHERE tenant_id=? AND conversation_id=? AND status <> 'invalidated'",
            (now_iso(), tenant_id, conversation_id),
        )
        recommendation_id = secrets.token_hex(12)
        self.conn.execute(
            "INSERT INTO recommendations(id, tenant_id, conversation_id, requirement_revision_id, status, rule_version, results_json, tentative_viewed, idempotency_key, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (recommendation_id, tenant_id, conversation_id, requirement_revision_id, status, rule_version, json.dumps(results, ensure_ascii=False), 1 if tentative_viewed else 0, idempotency_key, now_iso()),
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
        existing = self.conn.execute(
            "SELECT id FROM human_tasks WHERE tenant_id=? AND conversation_id=? AND question_message_id=?",
            (tenant_id, conversation_id, question_message_id),
        ).fetchone()
        if existing:
            return existing["id"]
        task_id = secrets.token_hex(12)
        timestamp = now_iso()
        try:
            self.conn.execute(
                "INSERT INTO human_tasks(id, tenant_id, conversation_id, question_message_id, requirement_revision_id, recommendation_id, product_id, product_version_id, question, context_json, reason, status, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)",
                (task_id, tenant_id, conversation_id, question_message_id, requirement_revision_id, recommendation_id, product_id, product_version_id, question, json.dumps(context, ensure_ascii=False), reason, timestamp, timestamp),
            )
        except sqlite3.IntegrityError:
            # 并发重复请求：另一请求已创建同一问题的任务，返回既有任务即可。
            winner = self.conn.execute(
                "SELECT id FROM human_tasks WHERE conversation_id=? AND question_message_id=?",
                (conversation_id, question_message_id),
            ).fetchone()
            if winner:
                return winner["id"]
            raise
        self._audit(tenant_id, "human_task", task_id, "created", "rule-based-answer", reason)
        self.conn.commit()
        return task_id

    def request_human_review(self, tenant_id: str, conversation_id: str, answer_id: str) -> str:
        answer = self.conn.execute(
            "SELECT * FROM answers WHERE tenant_id=? AND conversation_id=? AND id=? AND answer_status='needs_human'",
            (tenant_id, conversation_id, answer_id),
        ).fetchone()
        if not answer:
            raise ValueError("没有可请求人工核实的未解问题")
        question = self.conn.execute(
            "SELECT content FROM messages WHERE tenant_id=? AND conversation_id=? AND id=? AND role='buyer'",
            (tenant_id, conversation_id, answer["question_message_id"]),
        ).fetchone()
        if not question:
            raise ValueError("原始问题不存在")
        revision = self.get_latest_revision(tenant_id, conversation_id)
        recommendation = self.latest_active_recommendation(tenant_id, conversation_id)
        context = {
            "requirement_revision_id": revision["id"] if revision else None,
            "confirmed_requirements": json.loads(revision["values_json"]) if revision and revision["status"] == "confirmed" else {},
            "recommendation_id": recommendation["id"] if recommendation else None,
            "product_version_id": answer["product_version_id"],
            "answer_so_far": answer["answer_text"],
        }
        return self.create_human_task(
            tenant_id, conversation_id, answer["question_message_id"], question["content"],
            "资料未覆盖或需要工程判断", context, answer["product_id"], answer["product_version_id"],
            revision["id"] if revision else None, recommendation["id"] if recommendation else None,
        )

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

    def list_task_followups(self, tenant_id: str, task_id: str) -> list[sqlite3.Row]:
        task = self.get_human_task(tenant_id, task_id)
        if not task:
            return []
        return self.conn.execute(
            "SELECT * FROM messages WHERE tenant_id=? AND conversation_id=? AND source IN (?, ?) ORDER BY rowid",
            (tenant_id, task["conversation_id"], f"engineer-followup:{task_id}", f"buyer-followup:{task_id}"),
        ).fetchall()

    def ask_task_followup(self, tenant_id: str, task_id: str, question: str, actor: str = "demo-engineer") -> None:
        task = self.get_human_task(tenant_id, task_id)
        if not task or task["status"] != "pending" or not question.strip():
            raise ValueError("任务需处于待处理状态，且追问不能为空")
        with self.conn:
            updated = self.conn.execute(
                "UPDATE human_tasks SET status='waiting_buyer', assigned_to=?, updated_at=? WHERE tenant_id=? AND id=? AND status='pending'",
                (actor, now_iso(), tenant_id, task_id),
            )
            if updated.rowcount != 1:
                raise ValueError("任务状态已变化")
            self.save_message(tenant_id, task["conversation_id"], "engineer", question, f"engineer-followup:{task_id}", commit=False)
            self._audit(tenant_id, "human_task", task_id, "asked_buyer", actor)

    def answer_task_followup(self, tenant_id: str, conversation_id: str, task_id: str, answer: str) -> None:
        task = self.get_human_task(tenant_id, task_id)
        if not task or task["conversation_id"] != conversation_id or task["status"] != "waiting_buyer" or not answer.strip():
            raise ValueError("没有待补充的工程师追问")
        with self.conn:
            updated = self.conn.execute(
                "UPDATE human_tasks SET status='pending', updated_at=? WHERE tenant_id=? AND id=? AND conversation_id=? AND status='waiting_buyer'",
                (now_iso(), tenant_id, task_id, conversation_id),
            )
            if updated.rowcount != 1:
                raise ValueError("任务状态已变化")
            self.save_message(tenant_id, conversation_id, "buyer", answer, f"buyer-followup:{task_id}", commit=False)
            self._audit(tenant_id, "human_task", task_id, "buyer_replied", "buyer")

    def resolve_human_task(self, tenant_id: str, task_id: str, response: str, actor: str = "demo-engineer") -> None:
        task = self.get_human_task(tenant_id, task_id)
        if not task:
            raise ValueError("人工任务不存在")
        if task["status"] != "pending":
            raise ValueError("人工任务已处理")
        response = response.strip()
        if not response:
            raise ValueError("人工回复不能为空")
        timestamp = now_iso()
        with self.conn:
            updated = self.conn.execute(
                "UPDATE human_tasks SET status='resolved', response=?, assigned_to=?, updated_at=?, resolved_at=? WHERE tenant_id=? AND id=? AND status='pending'",
                (response, actor, timestamp, timestamp, tenant_id, task_id),
            )
            if updated.rowcount != 1:
                raise ValueError("人工任务已处理")
            self.save_message(tenant_id, task["conversation_id"], "assistant", f"工程师回复：{response}", "human-engineer", commit=False)
            self._audit(tenant_id, "human_task", task_id, "resolved", actor)

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

    @staticmethod
    def rfq_current_content(row: sqlite3.Row) -> dict[str, object]:
        return {
            "product_id": row["product_id"], "contact_name": row["contact_name"], "company": row["company"],
            "contact_email": row["contact_email"], "contact_phone": row["contact_phone"],
            "quantity": row["quantity"], "destination": row["destination"],
            "requested_delivery": row["requested_delivery"], "notes": row["notes"],
            "status": row["status"], "missing": json.loads(row["missing_json"]),
        }

    def list_rfq_revisions(self, tenant_id: str, rfq_id: str) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM rfq_revisions WHERE tenant_id=? AND rfq_id=? ORDER BY revision",
            (tenant_id, rfq_id),
        ).fetchall()

    def list_rfqs(self, tenant_id: str) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT r.*, p.model, p.slug FROM rfqs r LEFT JOIN products p ON p.id=r.product_id WHERE r.tenant_id=? ORDER BY r.created_at DESC",
            (tenant_id,),
        ).fetchall()

    def related_rfqs_in_conversation(self, tenant_id: str, conversation_id: str, product_id: str | None, exclude_rfq_id: str, hours: int = 24) -> list[sqlite3.Row]:
        """Same-conversation, same-model RFQs within the window. A hint to check for related
        purchase requests only — records are never merged or split automatically."""
        if not product_id:
            return []
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).replace(microsecond=0).isoformat()
        return self.conn.execute(
            "SELECT id, current_revision, status, created_at FROM rfqs "
            "WHERE tenant_id=? AND conversation_id=? AND product_id=? AND id<>? AND created_at>=? ORDER BY created_at",
            (tenant_id, conversation_id, product_id, exclude_rfq_id, cutoff),
        ).fetchall()

    def revise_rfq(self, tenant_id: str, conversation_id: str, rfq_id: str, expected_revision: int, changes: dict[str, object], idempotency_key: str) -> tuple[int, bool]:
        rfq = self.get_rfq(tenant_id, rfq_id)
        if not rfq or rfq["conversation_id"] != conversation_id:
            raise ValueError("询价不存在")
        request_hash = hashlib.sha256(json.dumps(changes, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        prior = self.conn.execute(
            "SELECT revision, request_hash FROM rfq_revisions WHERE tenant_id=? AND rfq_id=? AND idempotency_key=?",
            (tenant_id, rfq_id, idempotency_key),
        ).fetchone()
        if prior:
            if not hmac.compare_digest(prior["request_hash"], request_hash):
                raise ConflictError("同一修改标识不能用于不同内容")
            return prior["revision"], False
        if rfq["current_revision"] != expected_revision:
            raise ConflictError("询价已更新，请刷新后再修改")
        current = self.rfq_current_content(rfq)
        editable = ("contact_name", "company", "contact_email", "contact_phone", "quantity", "destination", "requested_delivery", "notes")
        for key in editable:
            if key in changes:
                current[key] = str(changes[key]).strip()
        if "product_id" in changes:
            current["product_id"] = changes["product_id"] or None
        if not (current["contact_email"] or current["contact_phone"]):
            raise ValueError("请保留至少一种可回复联系方式")
        product_id = current["product_id"]
        version = self.get_published_version(tenant_id, product_id) if product_id else None
        if product_id and not version:
            raise ValueError("所选产品未发布或不属于当前企业")
        technical_missing = [item for item in json.loads(rfq["missing_json"]) if item not in {"数量", "目的地", "期望交期"}]
        if "product_id" in changes and product_id != rfq["product_id"] and "产品适用性待核实" not in technical_missing:
            technical_missing.append("产品适用性待核实")
        missing = technical_missing + [label for key, label in (("quantity", "数量"), ("destination", "目的地"), ("requested_delivery", "期望交期")) if not current[key]]
        status = "needs_info" if missing else "submitted"
        timestamp = now_iso()
        next_revision = expected_revision + 1
        with self.conn:
            updated = self.conn.execute(
                "UPDATE rfqs SET current_revision=?, product_id=?, product_version_id=?, contact_name=?, company=?, contact_email=?, contact_phone=?, quantity=?, destination=?, requested_delivery=?, notes=?, missing_json=?, status=? WHERE tenant_id=? AND conversation_id=? AND id=? AND current_revision=?",
                (next_revision, product_id, version["id"] if version else None, current["contact_name"], current["company"], current["contact_email"], current["contact_phone"], current["quantity"], current["destination"], current["requested_delivery"], current["notes"], json.dumps(missing, ensure_ascii=False), status, tenant_id, conversation_id, rfq_id, expected_revision),
            )
            if updated.rowcount != 1:
                raise ConflictError("询价已更新，请刷新后再修改")
            revised = self.get_rfq(tenant_id, rfq_id)
            self.conn.execute(
                "INSERT INTO rfq_revisions(id, tenant_id, rfq_id, revision, content_json, actor, idempotency_key, request_hash, created_at) VALUES (?, ?, ?, ?, ?, 'buyer', ?, ?, ?)",
                (secrets.token_hex(12), tenant_id, rfq_id, next_revision, json.dumps(self.rfq_current_content(revised), ensure_ascii=False), idempotency_key, request_hash, timestamp),
            )
            self.conn.execute("UPDATE leads SET updated_at=? WHERE tenant_id=? AND rfq_id=?", (timestamp, tenant_id, rfq_id))
            self._audit(tenant_id, "rfq", rfq_id, "buyer_revised", "buyer", f"revision={next_revision}")
        return next_revision, True

    def create_rfq(
        self,
        tenant_id: str,
        conversation_id: str,
        submission_key: str,
        contact: dict[str, str],
        notes: str,
        selected_product_id: str | None = None,
        entry_surface: str = "chat",
    ) -> tuple[str, bool]:
        normalized_contact = {key: contact.get(key, "").strip() for key in ("contact_name", "company", "contact_email", "contact_phone", "quantity", "region", "delivery")}
        request_hash = hashlib.sha256(json.dumps({
            "conversation_id": conversation_id, "contact": normalized_contact, "notes": notes.strip(),
            "selected_product_id": selected_product_id or None, "entry_surface": entry_surface,
        }, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        existing = self.get_rfq_by_submission_key(tenant_id, submission_key)
        if existing:
            if existing["request_hash"] and not hmac.compare_digest(existing["request_hash"], request_hash):
                raise ConflictError("同一提交标识不能用于不同询价内容")
            return existing["id"], False
        conversation = self.get_conversation(tenant_id, conversation_id)
        if not conversation:
            raise ValueError("会话不存在")
        if not (contact.get("contact_email", "").strip() or contact.get("contact_phone", "").strip()):
            raise ValueError("请填写可回复的邮箱或电话等联系方式")
        revision = self.latest_confirmed_revision(tenant_id, conversation_id)
        recommendation = self.latest_active_recommendation(tenant_id, conversation_id)
        revision_values = json.loads(revision["values_json"]) if revision else {}
        recommendation_results = json.loads(recommendation["results_json"]) if recommendation else []
        selected_result = None
        if selected_product_id:
            selected_result = next((result for result in recommendation_results if result.get("product_id") == selected_product_id), None)
        if not selected_result:
            selected_result = next((result for result in recommendation_results if result.get("status") in {"candidate", "needs_confirmation"}), None)
        if not selected_result and selected_product_id:
            product = self.get_product(tenant_id, selected_product_id)
            version = self.get_published_version(tenant_id, selected_product_id) if product else None
            if version:
                selected_result = {"product_id": selected_product_id, "product_version_id": version["id"], "model": product["model"], "status": "direct_view"}
        product_id = selected_result.get("product_id") if selected_result else None
        product_version_id = selected_result.get("product_version_id") if selected_result else None
        missing = []
        if not revision:
            missing.append("工况待确认")
        if selected_result and selected_result.get("status") == "needs_confirmation":
            missing.append("产品适用性待工程师核实")
        for key, label in (("quantity", "数量"), ("region", "目的地"), ("delivery", "期望交期")):
            value = contact.get(key, "")
            if not value.strip():
                missing.append(label)
        status = "submitted" if not missing else "needs_info"
        snapshot = {
            "requirement_revision_id": revision["id"] if revision else None,
            "requirements": revision_values,
            "recommendation_id": recommendation["id"] if recommendation else None,
            "recommendation": selected_result,
            "submitted_contact": {"name": contact.get("contact_name", ""), "company": contact.get("company", ""), "email": contact.get("contact_email", ""), "phone": contact.get("contact_phone", "")},
            "submitted_at": now_iso(),
            "entry_surface": entry_surface,
        }
        rfq_id = secrets.token_hex(12)
        timestamp = now_iso()
        self.conn.execute(
            "INSERT INTO rfqs(id, tenant_id, conversation_id, requirement_revision_id, recommendation_id, product_id, product_version_id, submission_key, request_hash, contact_name, company, contact_email, contact_phone, quantity, destination, requested_delivery, notes, missing_json, status, snapshot_json, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (rfq_id, tenant_id, conversation_id, revision["id"] if revision else None, recommendation["id"] if recommendation else None, product_id, product_version_id, submission_key, request_hash, normalized_contact["contact_name"], normalized_contact["company"], normalized_contact["contact_email"], normalized_contact["contact_phone"], normalized_contact["quantity"], normalized_contact["region"], normalized_contact["delivery"], notes.strip(), json.dumps(missing, ensure_ascii=False), status, json.dumps(snapshot, ensure_ascii=False), timestamp),
        )
        initial = self.get_rfq(tenant_id, rfq_id)
        self.conn.execute(
            "INSERT INTO rfq_revisions(id, tenant_id, rfq_id, revision, content_json, actor, created_at) VALUES (?, ?, ?, 1, ?, 'buyer', ?)",
            (secrets.token_hex(12), tenant_id, rfq_id, json.dumps(self.rfq_current_content(initial), ensure_ascii=False), timestamp),
        )
        lead_id = secrets.token_hex(12)
        existing_owner = self.conn.execute(
            "SELECT owner FROM leads WHERE tenant_id=? AND conversation_id=? AND owner IS NOT NULL LIMIT 1",
            (tenant_id, conversation_id),
        ).fetchone()
        self.conn.execute(
            "INSERT INTO leads(id, tenant_id, rfq_id, conversation_id, stage, owner, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (lead_id, tenant_id, rfq_id, conversation_id, "new" if status == "submitted" else "needs_info", existing_owner["owner"] if existing_owner else None, timestamp, timestamp),
        )
        self._audit(tenant_id, "rfq", rfq_id, "submitted", "buyer-web", f"status={status};lead={lead_id}")
        self.conn.commit()
        return rfq_id, True

    def get_lead_for_rfq(self, tenant_id: str, rfq_id: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM leads WHERE tenant_id=? AND rfq_id=?", (tenant_id, rfq_id)
        ).fetchone()

    def claim_conversation_leads(self, tenant_id: str, rfq_id: str, actor_email: str) -> None:
        user = self.conn.execute("SELECT role, active FROM users WHERE tenant_id=? AND email=?", (tenant_id, actor_email)).fetchone()
        lead = self.get_lead_for_rfq(tenant_id, rfq_id)
        if not user or not user["active"] or user["role"] != "sales" or not lead:
            raise ValueError("没有可领取的线索")
        with self.conn:
            self.conn.execute("BEGIN IMMEDIATE")
            owners = {row["owner"] for row in self.conn.execute("SELECT owner FROM leads WHERE tenant_id=? AND conversation_id=? AND owner IS NOT NULL", (tenant_id, lead["conversation_id"]))}
            if owners:
                raise ValueError("该会话已由销售负责")
            self.conn.execute("UPDATE leads SET owner=?, updated_at=? WHERE tenant_id=? AND conversation_id=? AND owner IS NULL", (actor_email, now_iso(), tenant_id, lead["conversation_id"]))
            self._audit(tenant_id, "lead", lead["id"], "claimed", actor_email, f"conversation={lead['conversation_id']}")

    def assign_conversation_leads(self, tenant_id: str, rfq_id: str, owner_email: str, actor_email: str) -> None:
        actor = self.conn.execute("SELECT role, active FROM users WHERE tenant_id=? AND email=?", (tenant_id, actor_email)).fetchone()
        owner = self.conn.execute("SELECT role, active FROM users WHERE tenant_id=? AND email=?", (tenant_id, owner_email)).fetchone()
        lead = self.get_lead_for_rfq(tenant_id, rfq_id)
        if not actor or not actor["active"] or actor["role"] != "admin" or not owner or not owner["active"] or owner["role"] != "sales" or not lead:
            raise ValueError("管理员只能分配给本企业的有效销售成员")
        with self.conn:
            self.conn.execute("UPDATE leads SET owner=?, updated_at=? WHERE tenant_id=? AND conversation_id=?", (owner_email, now_iso(), tenant_id, lead["conversation_id"]))
            self._audit(tenant_id, "lead", lead["id"], "assigned", actor_email, f"conversation={lead['conversation_id']};owner={owner_email}")

    def list_sales_records(self, tenant_id: str) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT r.*, p.model, l.id AS lead_id, l.stage, l.owner, l.next_follow_up, l.note, l.updated_at AS lead_updated_at, c.stop_proactive_contact, "
            "(SELECT COUNT(*) FROM rfq_revisions v WHERE v.rfq_id=r.id AND v.revision>1) AS buyer_revision_count, "
            "(SELECT MAX(v.created_at) FROM rfq_revisions v WHERE v.rfq_id=r.id) AS last_revision_at "
            "FROM rfqs r LEFT JOIN products p ON p.id=r.product_id LEFT JOIN leads l ON l.rfq_id=r.id AND l.tenant_id=r.tenant_id JOIN conversations c ON c.id=r.conversation_id AND c.tenant_id=r.tenant_id "
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
        with self.conn:
            self.conn.execute("BEGIN IMMEDIATE")
            preference = self.get_contact_preference(tenant_id, lead["conversation_id"])
            if preference and preference["stop_proactive_contact"] and next_follow_up.strip():
                raise ValueError("买方已要求停止主动联系，不能安排主动跟进日期")
            timestamp = now_iso()
            self.conn.execute(
                "UPDATE leads SET stage=?, next_follow_up=?, note=?, updated_at=? WHERE tenant_id=? AND rfq_id=?",
                (stage, next_follow_up.strip() or None, note.strip(), timestamp, tenant_id, rfq_id),
            )
            self._audit(tenant_id, "lead", lead["id"], "updated", actor, f"rfq={rfq_id};stage={stage};next_follow_up={next_follow_up}")

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
        scope: str = "unknown",
    ) -> str:
        if not self.get_product(tenant_id, product_id):
            raise ValueError("产品不存在")
        if scope not in SOURCE_SCOPES:
            raise ValueError("文件归属范围无效")
        source_id = secrets.token_hex(12)
        sha256 = hashlib.sha256(content).hexdigest() if content else None
        storage_path = None
        status = "registered"
        if content:
            UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
            safe_name = re.sub(r"[^a-zA-Z0-9._-]+", "_", Path(filename).name).strip(".") or "source.bin"
            target = UPLOAD_DIR / f"{source_id}-{safe_name}"
            target.write_bytes(content)
            storage_path = str(target.relative_to(ROOT))
            status = "stored"
        self.conn.execute(
            "INSERT INTO source_documents(id, tenant_id, product_id, filename, file_type, source_location, sha256, storage_path, processing_status, scope, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (source_id, tenant_id, product_id, filename.strip(), file_type.strip() or "other", source_location.strip(), sha256, storage_path, status, scope, now_iso()),
        )
        if content:
            self._apply_source_extraction(tenant_id, product_id, source_id, content, filename, actor)
        self._audit(tenant_id, "source_document", source_id, "registered", actor, filename)
        self.conn.commit()
        return source_id

    def set_source_scope(self, tenant_id: str, product_id: str, source_id: str, scope: str, actor: str = "demo-admin") -> dict[str, bool]:
        """Correct a source file's attribution scope.

        A scope correction changes what the published snapshot's scope label
        (“本型号/系列”) means for this file, so every real change also advances a
        monotonic per-file ``scope_revision``. When the file currently carries a
        public-download permission, that permission is revoked immediately, so the
        old version stops serving the file on the public page, the download route
        and the public JSON (all consult public_downloadable_ids, which compares the
        snapshot's captured scope *and* revision). Even if the scope text is later
        changed back to its earlier value, the advanced revision no longer matches
        the stale snapshot — renewed display still requires a fresh permission
        approval plus a new published version that records the corrected state.
        """
        if scope not in SOURCE_SCOPES:
            raise ValueError("文件归属范围无效")
        source = self.conn.execute(
            "SELECT * FROM source_documents WHERE tenant_id=? AND product_id=? AND id=?",
            (tenant_id, product_id, source_id),
        ).fetchone()
        if not source:
            raise ValueError("来源文件不属于当前产品")
        previous_scope = source["scope"]
        if scope == previous_scope:
            return {"changed": False, "public_permission_revoked": False}
        public_ids = self.public_source_ids(tenant_id, product_id)
        if scope == "unknown" and source_id in public_ids:
            raise ValueError("该文件已开放公开下载；请先撤销公开许可，再标记为归属未核对")
        public_permission_revoked = source_id in public_ids
        new_revision = int(source["scope_revision"] or 0) + 1
        if public_permission_revoked:
            # 归属口径已变，旧快照不得继续以旧口径公开该文件；重新公开须走
            # 重新核准 + 重新发布（见 public_downloadable_ids 的三重条件）。
            self.conn.execute(
                "DELETE FROM public_source_permissions WHERE tenant_id=? AND product_id=? AND source_id=?",
                (tenant_id, product_id, source_id),
            )
            self._audit(
                tenant_id, "source_document", source_id, "public_permission_revoked_on_scope_change",
                actor, f"{previous_scope} -> {scope} (归属修订 r{new_revision})",
            )
        self.conn.execute(
            "UPDATE source_documents SET scope=?, scope_revision=? WHERE id=?",
            (scope, new_revision, source_id),
        )
        self._audit(tenant_id, "source_document", source_id, "scope_updated", actor, f"{previous_scope} -> {scope} (归属修订 r{new_revision})")
        self.conn.commit()
        return {"changed": True, "public_permission_revoked": public_permission_revoked}

    def rerun_source_extraction(self, tenant_id: str, product_id: str, source_id: str, actor: str = "demo-admin") -> None:
        source = self.conn.execute(
            "SELECT * FROM source_documents WHERE tenant_id=? AND product_id=? AND id=?",
            (tenant_id, product_id, source_id),
        ).fetchone()
        if not source or not source["storage_path"]:
            raise ValueError("该来源没有已保存文件，无法重新解析")
        target = ROOT / source["storage_path"]
        if not target.is_file():
            raise ValueError("文件存储不可用，无法重新解析")
        self._apply_source_extraction(tenant_id, product_id, source_id, target.read_bytes(), source["filename"], actor)
        self._audit(tenant_id, "source_document", source_id, "extraction_rerun", actor, source["filename"])
        self.conn.commit()

    def _apply_source_extraction(self, tenant_id: str, product_id: str, source_id: str, content: bytes, filename: str, actor: str) -> None:
        """Run content-based extraction and store reviewable candidates.

        Extraction never invents values: unsupported formats fail explicitly, and a
        candidate that differs from an already approved fact is recorded as a
        conflict instead of silently overwriting the approved value.
        """
        status, items, note = extract_candidate_items(content, filename)
        applied: list[dict[str, str]] = []
        field_changed = False
        for item in items:
            existing = self.conn.execute(
                "SELECT * FROM product_fields WHERE tenant_id=? AND product_id=? AND field_key=?",
                (tenant_id, product_id, item["field_key"]),
            ).fetchone()
            entry = dict(item)
            if existing is None:
                self.conn.execute(
                    "INSERT INTO product_fields(id, tenant_id, product_id, field_key, label, value, unit, source_document_id, source_location, state, origin, review_note, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'candidate', 'extracted', '', ?)",
                    (secrets.token_hex(12), tenant_id, product_id, item["field_key"], FIELD_LABELS[item["field_key"]], item["value"], item["unit"], source_id, item["location"], now_iso()),
                )
                entry["review_status"] = "new"
                field_changed = True
            elif existing["state"] == "approved":
                if existing["value"].strip() == item["value"] and existing["unit"].strip() == item["unit"]:
                    entry["review_status"] = "duplicate"
                else:
                    entry["review_status"] = "conflict"
                    entry["existing_value"] = existing["value"]
                    entry["existing_unit"] = existing["unit"]
            elif existing["state"] == "rejected":
                entry["review_status"] = "rejected_kept"
                entry["existing_value"] = existing["value"]
            elif existing["origin"] == "manual" and existing["value"].strip():
                entry["review_status"] = "kept_manual"
                entry["existing_value"] = existing["value"]
            else:
                self.conn.execute(
                    "UPDATE product_fields SET value=?, unit=?, source_document_id=?, source_location=?, origin='extracted', review_note='', updated_at=? WHERE id=?",
                    (item["value"], item["unit"], source_id, item["location"], now_iso(), existing["id"]),
                )
                entry["review_status"] = "updated"
                field_changed = True
            applied.append(entry)
        self.conn.execute(
            "UPDATE source_documents SET processing_status=?, extraction_json=?, extraction_note=?, processed_at=? WHERE tenant_id=? AND id=?",
            (status, json.dumps(applied, ensure_ascii=False), note, now_iso(), tenant_id, source_id),
        )
        if field_changed:
            self.conn.execute(
                "UPDATE products SET status='pending_review', updated_at=? WHERE tenant_id=? AND id=? AND status IN ('draft','approved','published')",
                (now_iso(), tenant_id, product_id),
            )
        self._audit(tenant_id, "source_document", source_id, "extraction_completed", actor, f"{status}; items={len(applied)}")

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
        origin: str = "manual",
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
            "INSERT INTO product_fields(id, tenant_id, product_id, field_key, label, value, unit, source_document_id, source_location, state, origin, review_note, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'candidate', ?, '', ?) "
            "ON CONFLICT(product_id, field_key) DO UPDATE SET value=excluded.value, unit=excluded.unit, source_document_id=excluded.source_document_id, source_location=excluded.source_location, state='candidate', origin=excluded.origin, review_note='', approved_by=NULL, approved_at=NULL, updated_at=excluded.updated_at",
            (secrets.token_hex(12), tenant_id, product_id, field_key, FIELD_LABELS[field_key], value.strip(), unit.strip(), source_document_id or None, source_location.strip(), origin, now_iso()),
        )
        self.conn.execute("UPDATE products SET status='pending_review', updated_at=? WHERE id=? AND tenant_id=?", (now_iso(), product_id, tenant_id))
        self._audit(tenant_id, "product_field", product_id, "updated_candidate", actor, field_key)
        self.conn.commit()

    def approve_field(self, tenant_id: str, product_id: str, field_key: str, actor: str = "demo-admin") -> None:
        if field_key not in FIELD_LABELS:
            raise ValueError("不支持的字段")
        field = self.conn.execute(
            "SELECT * FROM product_fields WHERE tenant_id=? AND product_id=? AND field_key=?",
            (tenant_id, product_id, field_key),
        ).fetchone()
        if not field:
            raise ValueError("字段不存在")
        if not field["value"].strip():
            raise ValueError("字段没有值，不能核准；请先保存候选值")
        if not field["source_document_id"] or not field["source_location"].strip():
            raise ValueError("字段缺少来源文件或原文位置，不能核准；请先补充出处")
        source = self.conn.execute(
            "SELECT id FROM source_documents WHERE id=? AND tenant_id=? AND product_id=?",
            (field["source_document_id"], tenant_id, product_id),
        ).fetchone()
        if not source:
            raise ValueError("来源文件不属于当前产品，不能核准")
        self.conn.execute(
            "UPDATE product_fields SET state='approved', review_note='', approved_by=?, approved_at=?, updated_at=? WHERE id=?",
            (actor, now_iso(), now_iso(), field["id"]),
        )
        self._audit(tenant_id, "product_field", product_id, "field_approved", actor, f"{field_key}={field['value'].strip()}")
        self.conn.commit()

    def reject_field(self, tenant_id: str, product_id: str, field_key: str, reason: str = "", actor: str = "demo-admin") -> None:
        if field_key not in FIELD_LABELS:
            raise ValueError("不支持的字段")
        field = self.conn.execute(
            "SELECT id FROM product_fields WHERE tenant_id=? AND product_id=? AND field_key=?",
            (tenant_id, product_id, field_key),
        ).fetchone()
        if not field:
            raise ValueError("字段不存在")
        self.conn.execute(
            "UPDATE product_fields SET state='rejected', review_note=?, approved_by=NULL, approved_at=NULL, updated_at=? WHERE id=?",
            (reason.strip(), now_iso(), field["id"]),
        )
        self.conn.execute(
            "UPDATE products SET status='pending_review', updated_at=? WHERE tenant_id=? AND id=? AND status IN ('approved','published')",
            (now_iso(), tenant_id, product_id),
        )
        self._audit(tenant_id, "product_field", product_id, "field_rejected", actor, f"{field_key}; {reason.strip()}")
        self.conn.commit()

    def reopen_field(self, tenant_id: str, product_id: str, field_key: str, actor: str = "demo-admin") -> None:
        if field_key not in FIELD_LABELS:
            raise ValueError("不支持的字段")
        field = self.conn.execute(
            "SELECT id FROM product_fields WHERE tenant_id=? AND product_id=? AND field_key=?",
            (tenant_id, product_id, field_key),
        ).fetchone()
        if not field:
            raise ValueError("字段不存在")
        self.conn.execute(
            "UPDATE product_fields SET state='candidate', review_note='', approved_by=NULL, approved_at=NULL, updated_at=? WHERE id=?",
            (now_iso(), field["id"]),
        )
        self.conn.execute(
            "UPDATE products SET status='pending_review', updated_at=? WHERE tenant_id=? AND id=? AND status IN ('approved','published')",
            (now_iso(), tenant_id, product_id),
        )
        self._audit(tenant_id, "product_field", product_id, "field_reopened", actor, field_key)
        self.conn.commit()

    def finalize_product_review(self, tenant_id: str, product_id: str, actor: str = "demo-admin") -> None:
        """Finish the review gate: every candidate must be individually approved or rejected."""
        product = self.get_product(tenant_id, product_id)
        if not product:
            raise ValueError("产品不存在")
        fields = self.get_fields(tenant_id, product_id)
        pending = [field["label"] for field in fields if field["state"] == "candidate"]
        if pending:
            raise ValueError("还有候选字段未逐项处理（核准或驳回）：" + "、".join(pending) + "。请逐项处理后再完成核对。")
        approved = [
            field for field in fields
            if field["state"] == "approved" and field["value"].strip() and field["source_document_id"] and field["source_location"].strip()
        ]
        if not approved:
            raise ValueError("至少需要一项带来源位置的产品事实被逐项核准后才能完成核对")
        self.conn.execute("UPDATE products SET status='approved', updated_at=? WHERE tenant_id=? AND id=?", (now_iso(), tenant_id, product_id))
        self._audit(tenant_id, "product", product_id, "review_finalized", actor, f"approved_fields={len(approved)}")
        self.conn.commit()

    def approve_seeded_fields(self, tenant_id: str, product_id: str, actor: str = "demo-admin") -> None:
        """Demo-fixture helper only: approve every sourced candidate for tracked demo data.

        Values from fixtures/demo seed files are pre-reviewed for the local demo. This
        bypass must not become a seller UI action; the product page exposes per-field
        approve/reject instead.
        """
        for field in self.get_fields(tenant_id, product_id):
            if field["state"] == "candidate" and field["value"].strip() and field["source_document_id"] and field["source_location"].strip():
                self.approve_field(tenant_id, product_id, field["field_key"], actor)
        self.finalize_product_review(tenant_id, product_id, actor)

    def publish_product(self, tenant_id: str, product_id: str, actor: str = "demo-admin") -> int:
        product = self.get_product(tenant_id, product_id)
        if not product:
            raise ValueError("产品不存在")
        if product["status"] not in ("approved", "published"):
            raise ValueError("只有已核准产品才能发布")
        fields = self.get_fields(tenant_id, product_id)
        approved_fields = [field for field in fields if field["state"] == "approved" and field["value"].strip() and field["source_document_id"] and field["source_location"].strip()]
        if not approved_fields:
            raise ValueError("没有可公开的已核准产品事实")
        missing = [key for key in REQUIRED_FIELDS if not any(field["field_key"] == key for field in approved_fields)]
        previous = self.get_published_version(tenant_id, product_id)
        version = (previous["version"] if previous else 0) + 1
        public_ids = self.public_source_ids(tenant_id, product_id)
        public_files = [
            {
                "id": source["id"],
                "filename": source["filename"],
                "file_type": source["file_type"],
                "scope": source["scope"],
                "scope_revision": source["scope_revision"],
                "sha256": source["sha256"],
                "source_location": source["source_location"],
                "created_at": source["created_at"],
            }
            for source in self.get_sources(tenant_id, product_id)
            if source["id"] in public_ids and source["storage_path"] and source["scope"] in ("exact_model", "series")
        ]
        snapshot = {
            "product": {"model": product["model"], "name": product["name"], "use_case": product["use_case"], "slug": product["slug"]},
            "fields": [dict(field) for field in approved_fields],
            "missing_fields": missing,
            "public_sources": public_files,
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
            source_id = self.add_source(tenant_id, product_id, f"{sample['model']}-模拟产品手册.pdf", "PDF", "第 1 页：规格参数表", actor=actor, scope="exact_model")
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
                self.approve_seeded_fields(tenant_id, product["id"], actor)
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
                inquiry.get("next_follow_up", ""),
                inquiry.get("lead_note", ""),
                actor,
            )
            if inquiry.get("owner") and tenant_id == "demo-tenant":
                self.assign_conversation_leads(tenant_id, rfq_id, "demo-sales@example.invalid", "demo-admin@example.invalid")
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
                        scope=source.get("scope", "unknown"),
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
            self.approve_seeded_fields(tenant_id, product_id, actor)
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


def classify_products(db: Database, tenant_id: str, values: dict[str, dict[str, str]]) -> tuple[list[dict[str, object]], list[str]]:
    """Classify every published model against the confirmed requirements.

    Per-model tiers: ``candidate`` (可初步推荐), ``needs_confirmation`` (待核实候选),
    ``conflict`` (已知硬冲突，永不作为候选展示) and ``no_basis``（没有支持相关性的
    已核准技术依据，不进入匹配结果）。Returns (results, missing requirement labels);
    callers decide which tiers may reach the buyer.
    """
    missing = [REQUIREMENT_LABELS[key] for key in MATCH_REQUIRED_FIELDS if not field_value(values, key)]
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
        has_basis = any(product_fields.get(key) for key in ("flow_range", "head_range", "performance_points"))

        if has_basis:
            performance_points = performance_points_from_text(product_fields.get("performance_points", {}).get("value", ""))
            if performance_points:
                required_flow = requirements["flow"]
                required_head = requirements["head"]
                curve_head = curve_head_at_flow(performance_points, required_flow) if required_flow is not None else None
                if required_flow is None:
                    unknown.append("买方尚未确认流量，无法与目录性能曲线核对")
                elif curve_head is None:
                    conflicts.append("流量超出该型号目录性能点范围")
                elif required_head is None:
                    unknown.append("买方尚未确认扬程，无法与目录性能曲线核对")
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
                    if required_number is None:
                        unknown.append(f"买方尚未确认{label}")
                    elif product_range is None:
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
            elif requirement_media:
                unknown.append("介质类型尚未形成可解释规则")
            else:
                unknown.append("买方尚未确认介质")

            if requirements["temperature"] is not None:
                temperature_range = range_from_text(product_fields.get("temperature", {}).get("value", ""))
                if not temperature_range:
                    unknown.append("产品资料没有可核对的介质温度范围")
                elif temperature_range[0] <= requirements["temperature"] <= temperature_range[1]:
                    satisfies.append(f"介质温度 {requirements['temperature']:g}°C 在资料范围内")
                else:
                    conflicts.append(f"介质温度 {requirements['temperature']:g}°C 超出资料范围")

        if not has_basis:
            candidate_status = "no_basis"
        elif conflicts:
            candidate_status = "conflict"
        elif unknown or missing:
            candidate_status = "needs_confirmation"
        else:
            candidate_status = "candidate"
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
    results.sort(key=lambda item: (0 if item["status"] == "candidate" else 1 if item["status"] == "needs_confirmation" else 2 if item["status"] == "conflict" else 3, item["model"]))
    return results, missing


def overall_status(results: list[dict[str, object]], missing: list[str]) -> str:
    if missing:
        return "insufficient"
    if any(item["status"] == "candidate" for item in results):
        return "recommended"
    if any(item["status"] == "needs_confirmation" for item in results):
        return "needs_confirmation"
    return "no_match"


def match_products(
    db: Database,
    tenant_id: str,
    values: dict[str, dict[str, str]],
    include_tentative: bool = False,
) -> tuple[str, list[dict[str, object]], list[str]]:
    """Public helper: classify and apply the buyer-facing tier policy (used by tests)."""
    results, missing = classify_products(db, tenant_id, values)
    if missing:
        if include_tentative:
            return "insufficient", [item for item in results if item["status"] == "needs_confirmation"], missing
        return "insufficient", [], missing
    return overall_status(results, missing), results, []


def buyer_visible_results(results: list[dict[str, object]], include_tentative: bool) -> list[dict[str, object]]:
    """Which result tiers may reach the buyer for this request.

    Tentative candidates are withheld until the buyer explicitly asks to view them;
    ``conflict`` and ``no_basis`` models are never returned to the buyer.
    """
    allowed = {"candidate", "needs_confirmation"} if include_tentative else {"candidate"}
    return [result for result in results if result.get("status") in allowed]


def recommendation_flags(results: list[dict[str, object]], status: str) -> dict[str, object]:
    tentative_available = any(result.get("status") == "needs_confirmation" for result in results)
    return {
        "tentative_available": tentative_available,
        "options": {"engineer_help": status != "recommended", "tentative_available": tentative_available},
    }


def answer_from_approved_material(
    db: Database,
    tenant_id: str,
    question: str,
    product_id: str | None,
) -> tuple[str, str, str | None, str | None, list[dict[str, str]], str]:
    """Answer only from the selected published snapshot; otherwise hand off."""
    question_lower = question.lower()
    forbidden = ("price", "cost", "quote", "price", "价格", "报价", "库存", "stock", "delivery", "lead time", "shipping", "交期", "交货", "到货", "认证", "certificate", "certification")
    if any(token in question_lower for token in forbidden):
        return "needs_human", "关于价格、库存、交期或认证，我手上的核准资料还无法确认。若您希望工程师核实，可点击下方按钮；提交后可回到本对话查看回复。", product_id, None, [], "资料不包含商业承诺或认证判断"

    product = db.get_product(tenant_id, product_id) if product_id else None
    version = db.get_published_version(tenant_id, product_id) if product_id else None
    if not product or not version:
        return "needs_human", "目前没有可引用的已发布产品资料，我还不能确认这个问题。若您希望继续核实，可请求工程师查看。", product_id, None, [], "没有可引用的已发布产品版本"

    snapshot = json.loads(version["snapshot_json"])
    fields = {field["field_key"]: field for field in snapshot.get("fields", [])}
    field_map = [
        (("flow", "流量"), "flow_range", "该型号的流量范围是 {value}{unit}。"),
        (("head", "扬程"), "head_range", "该型号的扬程范围是 {value}{unit}。"),
        (("media", "介质", "medium", "液体"), "media", "资料标注的介质适用条件是：{value}{unit}。"),
        (("material", "材质", "材料"), "material", "该型号的材质是：{value}{unit}。"),
        (("power", "功率"), "power", "该型号的功率是 {value}{unit}。"),
        (("temperature", "温度", "temperature"), "temperature", "资料标注的介质温度范围是 {value}{unit}。"),
        (("性能", "曲线", "performance", "catalogue"), "performance_points", "资料中的目录性能点（流量:扬程）是：{value}{unit}。最终工作点需工程师确认。"),
    ]
    selected_key = None
    template = ""
    for tokens, key, candidate_template in field_map:
        if any(token in question_lower for token in tokens):
            selected_key = key
            template = candidate_template
            break
    if not selected_key or not fields.get(selected_key) or not fields[selected_key].get("value", "").strip():
        return "needs_human", "现有核准资料没有足够依据回答这个问题，参数还需要核实。若您愿意，我可以在您点击请求后请工程师查看。", product_id, version["id"], [], "资料缺少对应字段或问题需要工程判断"

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


BASE_CSS = """:root{--ink:#f7f4ff;--muted:#a8a2b8;--line:#29223c;--accent:#8b5cf6;--accent-strong:#a855f7;--bg:#08070f;--card:rgba(20,17,33,.9);--card-2:#151126;--warn:#f6c56a;--danger:#ff8fa3;--success:#67e8c5;}
*{box-sizing:border-box}html{background:var(--bg)}body{min-height:100vh;margin:0;background-color:var(--bg);background-image:linear-gradient(rgba(139,92,246,.045) 1px,transparent 1px),linear-gradient(90deg,rgba(139,92,246,.045) 1px,transparent 1px),radial-gradient(circle at 85% 0%,rgba(139,92,246,.15),transparent 28rem);background-size:48px 48px,48px 48px,100% 100%;color:var(--ink);font:15px/1.6 "Avenir Next","SF Pro Display","PingFang SC","Microsoft YaHei",sans-serif}
a{color:#c4b5fd;text-decoration:none}a:hover{color:#ede9fe;text-decoration:none}header{position:sticky;top:0;z-index:10;background:rgba(8,7,15,.88);backdrop-filter:blur(18px);border-bottom:1px solid rgba(139,92,246,.18);color:var(--ink);padding:17px 32px;display:flex;justify-content:space-between;align-items:center;gap:22px}header strong{font-size:18px;letter-spacing:.08em}header nav{display:flex;gap:4px;align-items:center;justify-content:flex-end;flex-wrap:wrap}header nav a{color:var(--muted);padding:7px 10px;border-radius:9px;white-space:nowrap;flex-shrink:0}header nav a:hover{background:rgba(139,92,246,.12);color:var(--ink)}main{max-width:1240px;margin:0 auto;padding:54px 28px 80px}h1{font-size:38px;line-height:1.15;letter-spacing:-.035em;margin:0 0 8px}h2{font-size:21px;letter-spacing:-.02em;margin:0 0 14px}h3{font-size:16px;margin:20px 0 8px}.muted{color:var(--muted)}.notice{padding:13px 15px;border-radius:12px;background:rgba(103,232,197,.1);border:1px solid rgba(103,232,197,.22);color:#9af2da;margin:0 0 20px}.error{padding:13px 15px;border-radius:12px;background:rgba(255,143,163,.1);border:1px solid rgba(255,143,163,.23);color:#ffb1c0;margin:0 0 20px}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(250px,1fr));gap:18px}.metric-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(190px,1fr));gap:18px}.metric{font-size:38px;font-weight:750;line-height:1.1;margin:8px 0;color:#fff}.action-card{display:block;color:var(--ink);transition:transform .2s ease,border-color .2s ease,background .2s ease}.action-card:hover{text-decoration:none;border-color:rgba(168,85,247,.75);background:rgba(32,24,52,.95);transform:translateY(-2px)}.card{background:var(--card);border:1px solid var(--line);border-radius:18px;padding:22px;box-shadow:0 18px 45px rgba(0,0,0,.2);backdrop-filter:blur(12px)}.toolbar{display:flex;justify-content:space-between;align-items:center;gap:12px;flex-wrap:wrap;margin-bottom:26px}button,.button{border:0;border-radius:10px;padding:10px 15px;background:linear-gradient(135deg,var(--accent),var(--accent-strong));color:#fff;font-weight:700;cursor:pointer;display:inline-block;box-shadow:0 8px 24px rgba(139,92,246,.24)}button:hover,.button:hover{filter:brightness(1.08);transform:translateY(-1px)}button.secondary,.button.secondary{background:rgba(139,92,246,.1);border:1px solid rgba(139,92,246,.35);color:#ddd6fe;box-shadow:none}button.warn,.button.warn{background:rgba(246,197,106,.12);border:1px solid rgba(246,197,106,.35);color:var(--warn);box-shadow:none}button.danger,.button.danger{background:rgba(255,143,163,.1);border:1px solid rgba(255,143,163,.35);color:var(--danger);box-shadow:none}form.inline{display:inline}label{display:block;font-weight:700;margin:10px 0 5px;color:#ded8ed}input,select,textarea{width:100%;padding:10px 11px;border:1px solid #3a3152;border-radius:10px;font:inherit;color:var(--ink);background:#0f0d19}input::placeholder,textarea::placeholder{color:#716a85}input:focus,select:focus,textarea:focus{outline:2px solid rgba(168,85,247,.35);border-color:var(--accent)}textarea{min-height:76px;resize:vertical}.form-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:4px 14px}.full{grid-column:1/-1}table{width:100%;border-collapse:collapse}th,td{text-align:left;padding:12px 8px;border-bottom:1px solid var(--line);vertical-align:top}th{font-size:12px;color:var(--muted);text-transform:uppercase;letter-spacing:.06em}.badge{display:inline-flex;align-items:center;border-radius:999px;padding:3px 10px;font-size:12px;font-weight:700;background:rgba(139,92,246,.13);border:1px solid rgba(139,92,246,.24);color:#d8ccff}.badge.published,.badge.approved{background:rgba(103,232,197,.1);border-color:rgba(103,232,197,.25);color:#9af2da}.badge.pending_review{background:rgba(246,197,106,.1);border-color:rgba(246,197,106,.25);color:#f7d991}.badge.withdrawn{background:rgba(255,143,163,.1);border-color:rgba(255,143,163,.25);color:#ffb1c0}.field-state{font-size:12px;color:var(--muted)}.source{display:flex;justify-content:space-between;gap:12px;padding:11px 0;border-bottom:1px solid var(--line)}.product-hero{background:linear-gradient(135deg,#17112d 0%,#31206a 52%,#5627a4 100%);color:white;border:1px solid rgba(196,181,253,.22);border-radius:22px;padding:36px;margin-bottom:20px;box-shadow:0 22px 60px rgba(69,35,132,.25)}.product-hero .eyebrow{text-transform:uppercase;letter-spacing:.14em;font-size:12px;opacity:.8}.product-hero h1{font-size:42px;margin:4px 0 8px}.kv{display:grid;grid-template-columns:repeat(auto-fit,minmax(190px,1fr));gap:12px;margin-top:24px}.kv div{background:rgba(8,7,15,.24);border:1px solid rgba(255,255,255,.14);padding:13px;border-radius:12px}.kv b{display:block;font-size:12px;opacity:.75}.kv span{font-size:19px;font-weight:700}.footer-note{margin-top:28px;padding:14px 16px;border-left:3px solid #a78bfa;background:rgba(167,139,250,.08);color:#c4b5fd;border-radius:0 10px 10px 0}.brand-mark{display:inline-block;width:10px;height:10px;margin-right:10px;border-radius:3px 8px 3px 8px;background:linear-gradient(135deg,#c4b5fd,#7c3aed);box-shadow:0 0 18px rgba(168,85,247,.75);transform:rotate(45deg)}@media(max-width:700px){header{padding:15px 18px;align-items:flex-start;flex-direction:column}header nav{justify-content:flex-start;overflow:auto;flex-wrap:nowrap;width:100%;padding-bottom:2px}main{padding:34px 14px 60px}.form-grid{grid-template-columns:1fr}.full{grid-column:auto}h1{font-size:30px}.product-hero h1{font-size:34px}table{font-size:13px}main table{display:block;overflow-x:auto;max-width:100%}}"""


def layout(title: str, body: str, active: str = "产品管理", lang: str = "zh", public: bool = False) -> str:
    header_html = "<header><strong>Industrial Pump Supplier <small>· DEMO</small></strong><nav><a href='#specifications'>Specifications / 参数</a><a href='#downloads'>Documents / 资料</a><a href='#product-rfq'>RFQ / 询价</a></nav></header>" if public else f"<header><strong><span class='brand-mark' aria-hidden='true'></span>Growave</strong><nav><a href='/admin'>{'管理概览' if lang == 'zh' else 'Overview'}</a><a href='/admin/products'>{'产品管理' if lang == 'zh' else 'Products'}</a><a href='/admin/tasks'>{'人工任务' if lang == 'zh' else 'Tasks'}</a><a href='/admin/rfqs'>{'RFQ询价' if lang == 'zh' else 'RFQs'}</a><a href='/admin/sales'>{'销售工作台' if lang == 'zh' else 'Sales desk'}</a><a href='/inquiry'>{'买方询盘' if lang == 'zh' else 'Buyer inquiry'}</a><a href='/admin/products?lang={'en' if lang == 'zh' else 'zh'}'>{'English' if lang == 'zh' else '中文'}</a></nav></header>"
    return f"""<!doctype html>
<html lang='{ 'en' if lang == 'en' else 'zh-CN' }'>
<head>
<meta charset='utf-8'><meta name='viewport' content='width=device-width, initial-scale=1'>
<title>{esc(title)} · Growave</title>
<style>
{BASE_CSS}
</style></head><body>
{header_html}
<main>{body}</main></body></html>"""


CONSOLE_CSS = """
.console{display:grid;grid-template-columns:232px minmax(0,1fr);min-height:100vh}
.console .side{position:sticky;top:0;height:100vh;display:flex;flex-direction:column;background:#0b0916;border-right:1px solid var(--line);padding:16px 10px 10px;z-index:20}
.console .side-brand{display:flex;align-items:center;gap:10px;padding:2px 8px 14px;border-bottom:1px solid var(--line)}
.console .side-brand strong{font-size:16px;letter-spacing:.06em;display:block}
.console .side-brand small{display:block;font-size:10px;color:var(--muted);letter-spacing:.14em}
.console .side-tenant{margin:12px 6px;padding:9px 11px;border:1px solid var(--line);border-radius:10px;font-size:12px;color:var(--muted)}
.console .side-nav{flex:1;overflow:auto;display:flex;flex-direction:column;gap:2px;padding-top:4px}
.console .side-heading{font-size:11px;font-weight:700;letter-spacing:.12em;color:#7f77a0;margin:16px 10px 4px}
.console .side-link{display:flex;align-items:center;gap:10px;padding:10px 11px;border-radius:10px;color:var(--muted);font-size:14px;border:1px solid transparent}
.console .side-link:hover{background:rgba(139,92,246,.10);color:var(--ink)}
.console .side-link.active{background:rgba(139,92,246,.16);border-color:rgba(139,92,246,.30);color:#fff;font-weight:700}
.console .side-link .sym{width:20px;text-align:center;color:#a78bfa;font-size:15px}
.console .side-foot{border-top:1px solid var(--line);padding:12px 8px 2px;font-size:11px;color:var(--muted);line-height:1.8}
.console .side-foot a{color:#c4b5fd}
.console .shell{min-width:0;display:flex;flex-direction:column}
.console .topbar{position:sticky;top:0;z-index:10;display:flex;align-items:center;gap:10px;min-height:58px;padding:8px 26px;background:rgba(8,7,15,.9);backdrop-filter:blur(18px);border-bottom:1px solid rgba(139,92,246,.18)}
.console .crumb{font-size:12px;color:var(--muted);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.console .crumb b{color:var(--ink);font-size:13px}
.console .topbar .grow{flex:1}
.console .topbar a{font-size:13px;padding:7px 10px;border-radius:9px}
.console .topbar a:hover{background:rgba(139,92,246,.12)}
.console .menu-btn{display:none;border:1px solid var(--line);background:rgba(139,92,246,.08);color:var(--ink);border-radius:9px;padding:6px 11px;font-size:16px;line-height:1;box-shadow:none}
.console main{max-width:1160px;width:100%;margin:0 auto;padding:34px 30px 70px}
/* 线稿式展示组件：主任务在前，证据与历史按需展开 */
.console .focus{display:flex;justify-content:space-between;gap:18px;align-items:center;background:rgba(139,92,246,.08);border:1px solid rgba(139,92,246,.32);border-left:4px solid var(--accent);border-radius:14px;padding:18px 20px;margin:0 0 20px}
.console .focus .focus-label{color:#c4b5fd;font-size:12px;font-weight:700;letter-spacing:.08em}
.console .focus strong{display:block;font-size:19px;line-height:1.4;margin:6px 0 4px;color:#fff}
.console .focus p{font-size:13px;color:var(--muted);margin:0;max-width:640px}
.console .focus .actions{display:flex;gap:8px;flex-wrap:wrap;align-items:center;justify-content:flex-end;flex-shrink:0}
.console .panel-title{display:flex;justify-content:space-between;align-items:baseline;gap:10px;margin:0 0 12px}
.console .panel-title h2{margin:0}
.console .panel-title .muted{font-size:12px}
.console .simple-list{display:grid}
.console .simple-item{display:flex;gap:16px;justify-content:space-between;align-items:center;border-bottom:1px solid var(--line);padding:14px 0}
.console .simple-item:first-child{padding-top:2px}
.console .simple-item:last-child{border-bottom:0;padding-bottom:2px}
.console .simple-item strong{display:block;font-size:14px}
.console .simple-item small{display:block;color:var(--muted);font-size:12px;margin-top:3px}
.console .simple-item .actions{flex-shrink:0;display:flex;gap:8px;align-items:center;flex-wrap:wrap}
.console .compact-facts{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:10px;margin:0 0 10px}
.console .compact-facts>div{padding:12px 14px;border:1px solid var(--line);border-radius:10px;background:rgba(20,17,33,.55)}
.console .compact-facts small{display:block;color:var(--muted);font-size:12px}
.console .compact-facts strong{font-size:15px}
.console .summary-strip{display:flex;gap:8px;flex-wrap:wrap;margin:0 0 16px}
.console .summary-strip span{font-size:12px;background:rgba(139,92,246,.08);border:1px solid var(--line);border-radius:999px;padding:5px 11px;color:#cfc7e6}
.console .muted-block{background:rgba(20,17,33,.6);border:1px solid var(--line);border-radius:10px;padding:13px 15px;font-size:13px;color:var(--muted)}
.console .quote{border:1px solid var(--line);border-left:3px solid var(--accent);border-radius:0 10px 10px 0;background:rgba(139,92,246,.06);padding:12px 14px;font-size:14px;line-height:1.7;margin:0 0 12px}
.console .guide{font-size:12px;color:var(--muted);margin-top:8px}
.console .detail-toggle{border-top:1px solid var(--line);margin-top:14px;padding-top:12px;font-size:13px}
.console .detail-toggle summary{cursor:pointer;color:#c4b5fd;font-weight:700;list-style:none}
.console .detail-toggle summary::-webkit-details-marker{display:none}
.console .detail-toggle summary:after{content:'⌄';float:right;color:var(--muted)}
.console .detail-toggle[open] summary:after{content:'⌃'}
.console .detail-toggle>div{padding-top:12px}
@media(max-width:850px){
.console{grid-template-columns:1fr}
.console .side{position:fixed;left:0;top:0;width:242px;transform:translateX(-105%);transition:transform .18s ease;box-shadow:none}
.console .side.open{transform:none;box-shadow:10px 0 34px rgba(0,0,0,.55)}
.console .menu-btn{display:inline-block}
.console .topbar{padding:8px 14px}
.console main{padding:24px 16px 60px}
.console .focus{flex-direction:column;align-items:flex-start}
.console .focus .actions{justify-content:flex-start}
.console .simple-item{flex-direction:column;align-items:flex-start;gap:8px}
.console .compact-facts{grid-template-columns:1fr}
}
"""


def console_layout(title: str, body: str, active: str, *, lang: str = "zh", path: str = "/admin", actor: str = "", role: str = "", tenant: str = "") -> str:
    """卖家控制台外壳：左侧按业务分组的工具架 + 右侧工作区（顶栏面包屑）。"""
    groups = (
        ("我的工作", (("admin", "/admin", "⌂", "管理概览"),)),
        ("客户跟进", (
            ("sales", "/admin/sales", "◎", "销售工作台"),
            ("rfqs", "/admin/rfqs", "▦", "RFQ 询价"),
            ("tasks", "/admin/tasks", "◇", "人工任务"),
        )),
        ("产品经营", (("products", "/admin/products", "▧", "产品管理"),)),
        ("团队与接入", (
            ("users", "/admin/users", "◧", "成员管理"),
            ("sites", "/admin/sites", "◫", "网站 API 接入"),
        )),
    )
    group_of = {"admin": "我的工作", "sales": "客户跟进", "rfqs": "客户跟进", "tasks": "客户跟进", "products": "产品经营", "users": "团队与接入", "sites": "团队与接入"}
    role_label = {"admin": "管理员", "sales": "销售", "engineer": "工程师"}.get(role, role or "未登录")
    lang_target = f"{esc(path)}?lang={'en' if lang == 'zh' else 'zh'}"
    sections = []
    for heading, items in groups:
        links = "".join(
            f"<a class='side-link{' active' if key == active else ''}' href='{url}'><span class='sym'>{sym}</span>{label}</a>"
            for key, url, sym, label in items
        )
        sections.append(f"<div class='side-heading'>{heading}</div>{links}")
    crumb = f"{group_of.get(active, '卖家系统')} / <b>{esc(title)}</b>"
    account = f"当前账号：{esc(actor)}（{esc(role_label)}）" if actor else "未登录"
    return f"""<!doctype html>
<html lang='{'en' if lang == 'en' else 'zh-CN'}'>
<head>
<meta charset='utf-8'><meta name='viewport' content='width=device-width, initial-scale=1'>
<title>{esc(title)} · Growave</title>
<style>
{BASE_CSS}
{CONSOLE_CSS}
</style></head><body>
<div class='console'>
<aside class='side' id='console-side' aria-label='卖家系统导航'>
<div class='side-brand'><span class='brand-mark' aria-hidden='true'></span><div><strong>Growave</strong><small>SELLER CONSOLE</small></div></div>
<div class='side-tenant'>◧ {esc(tenant or '当前企业')}</div>
<nav class='side-nav'>{''.join(sections)}</nav>
<div class='side-foot'>{account}<br><a href='/inquiry' target='_blank'>打开买方演示页 ↗</a></div>
</aside>
<div class='shell'>
<header class='topbar'><button class='menu-btn' id='console-menu' aria-label='打开导航'>&#9776;</button><span class='crumb'>{crumb}</span><span class='grow'></span><a href='{lang_target}'>{'English' if lang == 'zh' else '中文'}</a><a href='/logout'>退出登录</a></header>
<main>{body}</main>
</div>
</div>
<script>document.getElementById('console-menu').addEventListener('click',function(){{document.getElementById('console-side').classList.toggle('open');}});</script>
</body></html>"""


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

    def setup(self) -> None:
        super().setup()
        self.db = Database(self.db.path, initialize=False)

    def finish(self) -> None:
        try:
            super().finish()
        finally:
            self.db.conn.close()

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

    def authorize_buyer_conversation(self, conversation_id: str) -> bool:
        token = self.cookie_value(f"cb_buyer_{conversation_id}")
        if self.db.has_buyer_access(self.tenant_id, conversation_id, token):
            return True
        self.send_html(layout("Inquiry not found", "<div class='error'>This inquiry is not available in this browser.</div>", lang="en"), 404)
        return False

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
        elif path.startswith("/admin/sites"):
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
            self.send_html(self.console_page("无权限", "<div class='error'>当前账号没有访问此管理功能的权限。</div><p><a href='/login'>切换账号</a></p>", ""), 403)
            return False
        return True

    def send_html(self, content: str, status: int = 200) -> None:
        data = content.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def send_json(self, payload: dict[str, object], status: int = 200, allowed_origin: str | None = None) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        if allowed_origin:
            self.send_header("Access-Control-Allow-Origin", allowed_origin)
            self.send_header("Vary", "Origin")
        self.end_headers()
        self.wfile.write(data)

    def send_reference_file(self, filename: str, content_type: str) -> None:
        data = (ROOT / "reference-client" / filename).read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def read_json(self) -> dict[str, object] | None:
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length < 1 or length > 65536:
                raise ValueError
            data = json.loads(self.rfile.read(length))
            if not isinstance(data, dict):
                raise ValueError
            return data
        except (ValueError, json.JSONDecodeError):
            self.send_json({"error": "invalid_json"}, 422)
            return None

    def api_site(self, site_key: str) -> sqlite3.Row | None:
        site = self.db.get_buyer_site(site_key)
        if not site:
            self.send_json({"error": "site_not_found"}, 404)
            return None
        origin = self.headers.get("Origin")
        same_host_page = origin and urlparse(origin).netloc == self.headers.get("Host") and urlparse(origin).scheme in {"http", "https"}
        if origin and origin != site["allowed_origin"] and not same_host_page:
            self.send_json({"error": "origin_not_allowed"}, 403)
            return None
        return site

    def api_conversation(self, conversation_id: str) -> tuple[sqlite3.Row, sqlite3.Row] | None:
        site = self.api_site(self.headers.get("X-Site-Key", ""))
        if not site:
            return None
        conversation = self.db.get_conversation(site["tenant_id"], conversation_id)
        if not conversation:
            self.send_json({"error": "conversation_not_found"}, 404)
            return None
        auth = self.headers.get("Authorization", "")
        token = auth[7:] if auth.startswith("Bearer ") else None
        if not self.db.has_buyer_access(site["tenant_id"], conversation_id, token):
            self.send_json({"error": "access_denied"}, 401)
            return None
        return site, conversation

    def do_OPTIONS(self) -> None:
        if not urlparse(self.path).path.startswith("/api/v1/buyer/"):
            self.send_error(404)
            return
        origin = self.headers.get("Origin", "")
        allowed = self.db.conn.execute(
            "SELECT 1 FROM buyer_sites WHERE allowed_origin=? AND active=1", (origin,)
        ).fetchone()
        if not allowed:
            self.send_json({"error": "origin_not_allowed"}, 403)
            return
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", origin)
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Authorization, Content-Type, X-Site-Key, Idempotency-Key")
        self.send_header("Vary", "Origin")
        self.end_headers()

    def send_bytes(self, content: bytes, content_type: str, filename: str, status: int = 200, inline: bool = False) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Disposition", f"{'inline' if inline else 'attachment'}; filename*=UTF-8''{quote(filename)}")
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    def console_page(self, title: str, body: str, active: str, lang: str = "zh") -> str:
        tenant = self.db.conn.execute("SELECT name FROM tenants WHERE id=?", (self.tenant_id,)).fetchone()
        clean_path = urlparse(self.path).path.rstrip("/") or "/admin"
        return console_layout(
            title, body, active, lang=lang, path=clean_path,
            actor=self.actor if self.user_id else "",
            role=self.user_role if self.user_id else "",
            tenant=(tenant["name"] if tenant else self.tenant_id),
        )

    def login_page(self, query: dict[str, list[str]], error: str = "") -> None:
        if self.current_user():
            self.redirect("/admin")
            return
        next_path = query.get("next", ["/admin/sales"])[0]
        error_html = f"<div class='error'>{esc(error)}</div>" if error else ""
        body = f"""
<div style='max-width:520px;margin:70px auto'><section class='card'><h1>企业后台登录</h1><p class='muted'>登录后只能访问当前账号所属企业的数据。买方公开页面不需要后台账号。</p>{error_html}<form method='post' action='/login'><input type='hidden' name='next' value='{esc(next_path)}'><label>邮箱</label><input name='email' type='email' autocomplete='username' required placeholder='name@company.example'><label>密码</label><input name='password' type='password' autocomplete='current-password' required><p><button>登录</button></p></form><div class='footer-note'>新部署的数据库不预置任何账号：请先用 `python3 tools/create_admin.py` 创建管理员。本地演示环境由 `python3 tools/demo/start_demo.py` 创建（文档化的演示账号 demo-admin / demo-sales / demo-engineer，密码与账号相同，仅限本地演示）。</div></section></div>
"""
        self.send_html(layout("企业后台登录", body), 200)

    def login_post(self, form: dict[str, str | bytes]) -> None:
        user = self.db.authenticate_user(str(form.get("email", "")), str(form.get("password", "")))
        if not user:
            self.login_page({"next": [str(form.get("next", "/admin/sales"))]}, "邮箱或密码不正确")
            return
        next_path = str(form.get("next", "/admin/sales"))
        if not next_path.startswith("/") or next_path.startswith("//") or next_path.startswith("/login") or next_path.startswith("/admin/login"):
            # 登录页自身或非法目标：回到后台首页，避免登录成功后跳进登录相关路径
            next_path = "/admin"
        session_id = self.db.create_session(user)
        self.redirect(next_path, f"cb_session={session_id}; Path=/; HttpOnly; SameSite=Lax; Max-Age=43200")

    def logout(self) -> None:
        self.db.delete_session(self.cookie_value("cb_session"))
        self.redirect("/login", "cb_session=; Path=/; HttpOnly; SameSite=Lax; Max-Age=0")

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        query = parse_qs(parsed.query)
        if path == "/admin/login":
            # /admin/login 不是真实页面：统一引导到登录页，登录后回后台首页（避免登录成功后被弹回本路径造成 403）
            self.redirect("/login?next=/admin")
            return
        reference_files = {
            "/reference-client": ("index.html", "text/html; charset=utf-8"),
            "/reference-client/app.js": ("app.js", "text/javascript; charset=utf-8"),
            "/reference-client/style.css": ("style.css", "text/css; charset=utf-8"),
            "/reference-client/product-page.js": ("product-page.js", "text/javascript; charset=utf-8"),
            "/reference-client/product-page.css": ("product-page.css", "text/css; charset=utf-8"),
        }
        if path in reference_files:
            self.send_reference_file(*reference_files[path])
            return
        public_product_match = re.fullmatch(r"/api/v1/public/products/([a-zA-Z0-9-]+)", path)
        if public_product_match:
            site = self.api_site(self.headers.get("X-Site-Key", ""))
            if not site:
                return
            tenant_id = site["tenant_id"]
            origin = site["allowed_origin"] if self.headers.get("Origin") else None
            product = self.db.get_product_by_slug(tenant_id, public_product_match.group(1))
            version = self.db.get_published_version(tenant_id, product["id"]) if product else None
            if not product or product["status"] == "withdrawn" or not version:
                self.send_json({"error": "product_not_found"}, 404, origin)
                return
            snapshot = json.loads(version["snapshot_json"])
            allowed_source_ids = self.db.public_downloadable_ids(tenant_id, product["id"])
            db_source_rows = {row["id"]: row for row in self.db.get_sources(tenant_id, product["id"])}
            public_files = []
            for entry in snapshot.get("public_sources", []):
                row = db_source_rows.get(entry["id"])
                if not row or not row["storage_path"] or entry["id"] not in allowed_source_ids:
                    continue
                suffix = Path(str(entry["filename"])).suffix.lower()
                base_url = f"/s/{quote(site['site_key'])}/products/{quote(product['slug'])}/sources/{entry['id']}"
                public_files.append({
                    "id": entry["id"], "filename": entry["filename"], "file_type": entry["file_type"], "scope": entry["scope"],
                    "download_url": base_url,
                    "preview_url": f"{base_url}?preview=1" if suffix in (IMAGE_EXTENSIONS | {".pdf"}) else None,
                })
            fields = [
                {
                    "field_key": field["field_key"], "label": field.get("label", ""), "value": field["value"], "unit": field.get("unit", ""),
                    "source_filename": field.get("source_filename") or None, "source_location": field.get("source_location") or None,
                }
                for field in snapshot["fields"]
            ]
            missing = [{"key": key, "label": FIELD_LABELS.get(key, key)} for key in snapshot.get("missing_fields", [])]
            self.send_json(
                {
                    "product": {"model": product["model"], "name": product["name"], "slug": product["slug"]},
                    "page_version": version["version"], "published_at": version["published_at"],
                    "fields": fields, "missing_fields": missing, "public_sources": public_files,
                    "disclaimer": "Initial technical display; not a quotation or final engineering approval.",
                },
                200, origin,
            )
            return
        api_match = re.fullmatch(r"/api/v1/buyer/conversations/([a-f0-9]+)", path)
        if api_match:
            access = self.api_conversation(api_match.group(1))
            if not access:
                return
            site, conversation = access
            messages = [
                {"id": row["id"], "role": row["role"], "source": row["source"], "content": row["content"], "created_at": row["created_at"]}
                for row in self.db.list_messages(site["tenant_id"], conversation["id"])
            ]
            answers = [
                {"id": row["id"], "question_message_id": row["question_message_id"], "status": row["answer_status"], "text": row["answer_text"], "citations": json.loads(row["citations_json"])}
                for row in self.db.list_answers(site["tenant_id"], conversation["id"])
            ]
            human_tasks = [
                {"id": row["id"], "question_message_id": row["question_message_id"], "status": row["status"], "question": row["question"],
                 "followup_question": next((message["content"] for message in reversed(self.db.list_task_followups(site["tenant_id"], row["id"])) if message["source"] == f"engineer-followup:{row['id']}"), None)}
                for row in self.db.list_human_tasks(site["tenant_id"]) if row["conversation_id"] == conversation["id"]
            ]
            rfqs = [
                {"id": row["id"], "current_revision": row["current_revision"], "model": row["model"], "created_at": row["created_at"], **self.db.rfq_current_content(row)}
                for row in self.db.list_rfqs(site["tenant_id"]) if row["conversation_id"] == conversation["id"]
            ]
            latest_revision = self.db.get_latest_revision(site["tenant_id"], conversation["id"])
            requirement = {
                "revision_id": latest_revision["id"], "status": latest_revision["status"],
                "values": json.loads(latest_revision["values_json"]), "missing": json.loads(latest_revision["missing_json"]),
            } if latest_revision else None
            latest_recommendation = self.db.latest_active_recommendation(site["tenant_id"], conversation["id"])
            recommendation = None
            if latest_recommendation:
                stored_results = json.loads(latest_recommendation["results_json"])
                visible_results = []
                for result in buyer_visible_results(stored_results, bool(latest_recommendation["tentative_viewed"])):
                    item = dict(result)
                    url = str(item.get("detail_url") or "")
                    if not url.startswith("/s/"):
                        item["detail_url"] = f"/s/{quote(site['site_key'])}" + url
                    visible_results.append(item)
                recommendation = {
                    "recommendation_id": latest_recommendation["id"], "status": latest_recommendation["status"],
                    "results": visible_results, "rule_version": latest_recommendation["rule_version"],
                    **recommendation_flags(stored_results, latest_recommendation["status"]),
                }
            self.send_json(
                {"conversation_id": conversation["id"], "status": conversation["status"], "locale": conversation["locale"], "contact_preference": {"stop_proactive_contact": bool(conversation["stop_proactive_contact"]), "reason": conversation["stop_reason"], "source": conversation["stop_source"], "stopped_at": conversation["stopped_at"]}, "messages": messages, "requirement": requirement, "recommendation": recommendation, "answers": answers, "human_tasks": human_tasks, "rfqs": rfqs},
                allowed_origin=site["allowed_origin"] if self.headers.get("Origin") else None,
            )
            return
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
        if path == "/admin/sites":
            self.admin_sites(query)
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
            conversation_id = self.db.create_conversation(self.tenant_id, "web", query.get("lang", ["en"])[0])
            token = self.db.issue_buyer_access(self.tenant_id, conversation_id)
            self.redirect(
                f"/inquiry/{conversation_id}?lang={query.get('lang', ['en'])[0]}",
                f"cb_buyer_{conversation_id}={token}; Path=/inquiry/{conversation_id}; HttpOnly; SameSite=Lax",
            )
            return
        match = re.fullmatch(r"/admin/products/([a-f0-9]+)/?", path)
        if match:
            self.admin_product(match.group(1), query)
            return
        match = re.fullmatch(r"/inquiry/([a-f0-9]+)/?", path)
        if match:
            if not self.authorize_buyer_conversation(match.group(1)):
                return
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
        match = re.fullmatch(r"/s/([A-Za-z0-9_-]+)/products/([a-zA-Z0-9-]+)", path)
        if match:
            site = self.db.get_buyer_site(match.group(1))
            if not site:
                self.send_html(layout("产品不可用", "<div class='error'>网站入口不可用。</div>"), 404)
                return
            self.public_product(match.group(2), query, site)
            return
        match = re.fullmatch(r"/products/([a-zA-Z0-9-]+)/sources/([a-f0-9]+)/?", path)
        if match:
            self.public_source(match.group(1), match.group(2), preview=query.get("preview", [""])[0] == "1")
            return
        match = re.fullmatch(r"/s/([A-Za-z0-9_-]+)/products/([a-zA-Z0-9-]+)/sources/([a-f0-9]+)", path)
        if match:
            site = self.db.get_buyer_site(match.group(1))
            if not site:
                self.send_html(layout("资料不可用", "<div class='error'>网站入口不可用。</div>", public=True), 404)
                return
            self.public_source(match.group(2), match.group(3), site, preview=query.get("preview", [""])[0] == "1")
            return
        if path.startswith("/files/"):
            self.send_html(layout("文件下载", "<div class='error'>演示环境不提供公开文件直链，请从企业后台下载或补充对象存储权限。</div>"), 404)
            return
        self.send_html(layout("未找到", "<div class='error'>页面不存在</div>"), 404)

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/")
        if path == "/api/v1/buyer/conversations":
            payload = self.read_json()
            if payload is None:
                return
            site = self.api_site(str(payload.get("site_key", "")))
            if not site:
                return
            locale = str(payload.get("locale", "en"))
            if locale not in {"en", "zh"}:
                self.send_json({"error": "invalid_locale"}, 422)
                return
            conversation_id = self.db.create_conversation(site["tenant_id"], "website_api", locale)
            token = self.db.issue_buyer_access(site["tenant_id"], conversation_id)
            self.send_json(
                {"conversation_id": conversation_id, "access_token": token, "status": "active"}, 201,
                site["allowed_origin"] if self.headers.get("Origin") else None,
            )
            return
        api_match = re.fullmatch(r"/api/v1/buyer/conversations/([a-f0-9]+)/messages", path)
        if api_match:
            access = self.api_conversation(api_match.group(1))
            if not access:
                return
            payload = self.read_json()
            if payload is None:
                return
            text_value = payload.get("text")
            if not isinstance(text_value, str) or not text_value.strip():
                self.send_json({"error": "text_required"}, 422)
                return
            site, conversation = access
            origin = site["allowed_origin"] if self.headers.get("Origin") else None
            idempotency_key = self.headers.get("Idempotency-Key", "").strip()
            request_hash = hashlib.sha256(text_value.strip().encode("utf-8")).hexdigest()

            def lookup_keyed() -> sqlite3.Row | None:
                return self.db.conn.execute(
                    "SELECT id, request_hash, result_revision_id FROM messages WHERE tenant_id=? AND conversation_id=? AND idempotency_key=?",
                    (site["tenant_id"], conversation["id"], idempotency_key),
                ).fetchone()

            def send_duplicate(existing: sqlite3.Row) -> None:
                draft = None
                if existing["result_revision_id"]:
                    stored = self.db.conn.execute(
                        "SELECT * FROM requirement_revisions WHERE id=? AND tenant_id=?",
                        (existing["result_revision_id"], site["tenant_id"]),
                    ).fetchone()
                    if stored:
                        values = json.loads(stored["values_json"])
                        missing = [key for key in MATCH_REQUIRED_FIELDS if not values.get(key, {}).get("value", "").strip()]
                        askable = [key for key in missing if values.get(key, {}).get("state") != "unknown"]
                        draft = {"revision_id": stored["id"], "values": values, "missing": missing, "next_question": askable[0] if askable else None}
                self.send_json({"message_id": existing["id"], "status": "duplicate", "requirement_draft": draft}, 200, origin)

            if idempotency_key:
                existing = lookup_keyed()
                if existing:
                    if existing["request_hash"] and not hmac.compare_digest(existing["request_hash"], request_hash):
                        self.send_json({"error": "idempotency_conflict"}, 409, origin)
                        return
                    send_duplicate(existing)
                    return
            try:
                message_id = self.db.save_message(
                    site["tenant_id"], conversation["id"], "buyer", text_value, "website_api",
                    idempotency_key=idempotency_key or None, request_hash=request_hash if idempotency_key else None,
                )
            except sqlite3.IntegrityError:
                # 并发同键：另一请求已写入，回滚本次事务并返回已存结果。
                self.db.conn.rollback()
                existing = lookup_keyed()
                if not existing:
                    raise
                if existing["request_hash"] and not hmac.compare_digest(existing["request_hash"], request_hash):
                    self.send_json({"error": "idempotency_conflict"}, 409, origin)
                    return
                send_duplicate(existing)
                return
            try:
                previous = self.db.get_latest_revision(site["tenant_id"], conversation["id"])
                previous_values = json.loads(previous["values_json"]) if previous else {}
                values = extract_requirement_values(text_value, {}, previous_values)
                revision_id = self.db.create_requirement_revision(site["tenant_id"], conversation["id"], values, "candidate", "website_api")
            except Exception:
                # 提取失败时消息已保存；不得假装已识别，返回可继续的失败状态供重试。
                self.send_json({"error": "extraction_unavailable", "message": "需求提取暂不可用；您的消息已保存，可稍后重试。"}, 503, origin)
                return
            if idempotency_key:
                self.db.conn.execute("UPDATE messages SET result_revision_id=? WHERE id=?", (revision_id, message_id))
                self.db.conn.commit()
            missing = [key for key in MATCH_REQUIRED_FIELDS if not values.get(key, {}).get("value", "").strip()]
            askable = [key for key in missing if values.get(key, {}).get("state") != "unknown"]
            self.send_json(
                {"message_id": message_id, "status": "saved", "requirement_draft": {"revision_id": revision_id, "values": values, "missing": missing, "next_question": askable[0] if askable else None}}, 201,
                origin,
            )
            return
        api_match = re.fullmatch(r"/api/v1/buyer/conversations/([a-f0-9]+)/requirements/confirm", path)
        if api_match:
            access = self.api_conversation(api_match.group(1))
            if not access:
                return
            payload = self.read_json()
            if payload is None:
                return
            site, conversation = access
            origin = site["allowed_origin"] if self.headers.get("Origin") else None
            idempotency_key = self.headers.get("Idempotency-Key", "").strip()
            request_hash = hashlib.sha256(
                json.dumps({"base_revision_id": payload.get("base_revision_id"), "values": payload.get("values")}, sort_keys=True, ensure_ascii=False).encode("utf-8")
            ).hexdigest()

            def lookup_keyed_revision() -> sqlite3.Row | None:
                return self.db.conn.execute(
                    "SELECT id, request_hash, values_json, status FROM requirement_revisions WHERE tenant_id=? AND conversation_id=? AND idempotency_key=?",
                    (site["tenant_id"], conversation["id"], idempotency_key),
                ).fetchone()

            def send_duplicate(existing: sqlite3.Row) -> None:
                stored_values = json.loads(existing["values_json"])
                missing = [key for key in MATCH_REQUIRED_FIELDS if not stored_values.get(key, {}).get("value", "").strip()]
                self.send_json({"revision_id": existing["id"], "values": stored_values, "missing": missing, "duplicate": True}, 200, origin)

            # 幂等重试先于基准版本检查：成功确认后基准版本已前进，重试仍应返回原始结果。
            if idempotency_key:
                existing_revision = lookup_keyed_revision()
                if existing_revision:
                    if existing_revision["request_hash"] and not hmac.compare_digest(existing_revision["request_hash"], request_hash):
                        self.send_json({"error": "idempotency_conflict"}, 409, origin)
                        return
                    send_duplicate(existing_revision)
                    return
            latest = self.db.get_latest_revision(site["tenant_id"], conversation["id"])
            if not latest or payload.get("base_revision_id") != latest["id"]:
                self.send_json({"error": "stale_requirement_revision"}, 409)
                return
            raw_values = payload.get("values")
            if not isinstance(raw_values, dict) or any(
                key not in REQUIREMENT_LABELS or not isinstance(item, dict) or not isinstance(item.get("value", ""), str) or not isinstance(item.get("unit", ""), str)
                for key, item in raw_values.items()
            ):
                self.send_json({"error": "invalid_requirement_values"}, 422)
                return
            values = confirm_requirement_values({
                key: {"value": item.get("value", "").strip(), "unit": item.get("unit", ""), "label": REQUIREMENT_LABELS[key]}
                for key, item in raw_values.items()
            })
            try:
                revision_id = self.db.create_requirement_revision(
                    site["tenant_id"], conversation["id"], values, "confirmed", "website_api",
                    idempotency_key=idempotency_key or None, request_hash=request_hash if idempotency_key else None,
                )
            except sqlite3.IntegrityError:
                # 并发同键：另一请求已写入，回滚本次事务并返回已存结果。
                self.db.conn.rollback()
                existing_revision = lookup_keyed_revision()
                if not existing_revision:
                    raise
                if existing_revision["request_hash"] and not hmac.compare_digest(existing_revision["request_hash"], request_hash):
                    self.send_json({"error": "idempotency_conflict"}, 409, origin)
                    return
                send_duplicate(existing_revision)
                return
            missing = [key for key in MATCH_REQUIRED_FIELDS if not values.get(key, {}).get("value", "").strip()]
            self.send_json(
                {"revision_id": revision_id, "values": values, "missing": missing}, 201,
                origin,
            )
            return
        api_match = re.fullmatch(r"/api/v1/buyer/conversations/([a-f0-9]+)/recommendations", path)
        if api_match:
            access = self.api_conversation(api_match.group(1))
            if not access:
                return
            payload = self.read_json()
            if payload is None:
                return
            include_tentative = payload.get("include_tentative", False)
            if not isinstance(include_tentative, bool):
                self.send_json({"error": "invalid_include_tentative"}, 422)
                return
            site, conversation = access
            origin = site["allowed_origin"] if self.headers.get("Origin") else None
            idempotency_key = self.headers.get("Idempotency-Key", "").strip()
            revision = self.db.get_latest_revision(site["tenant_id"], conversation["id"])
            if not revision or revision["status"] != "confirmed" or payload.get("revision_id") != revision["id"]:
                self.send_json({"error": "confirmed_revision_required"}, 409)
                return

            def with_site_url(result: dict[str, object]) -> dict[str, object]:
                item = dict(result)
                url = str(item.get("detail_url") or "")
                if not url.startswith("/s/"):
                    item["detail_url"] = f"/s/{quote(site['site_key'])}" + url
                return item

            def serve_recommendation(row: sqlite3.Row, code: int, duplicate: bool = False) -> None:
                stored = json.loads(row["results_json"])
                if include_tentative and not row["tentative_viewed"]:
                    self.db.conn.execute("UPDATE recommendations SET tentative_viewed=1 WHERE id=?", (row["id"],))
                    self.db.conn.commit()
                serve_tentative = include_tentative or bool(row["tentative_viewed"])
                results = [with_site_url(result) for result in buyer_visible_results(stored, serve_tentative)]
                missing_keys = json.loads(revision["missing_json"])
                missing = [REQUIREMENT_LABELS.get(key, key) for key in missing_keys]
                response = {"recommendation_id": row["id"], "status": row["status"], "results": results, "missing": missing, **recommendation_flags(stored, row["status"])}
                if duplicate:
                    response["duplicate"] = True
                self.send_json(response, code, origin)

            if idempotency_key:
                keyed = self.db.conn.execute(
                    "SELECT * FROM recommendations WHERE tenant_id=? AND conversation_id=? AND idempotency_key=?",
                    (site["tenant_id"], conversation["id"], idempotency_key),
                ).fetchone()
                if keyed:
                    if keyed["requirement_revision_id"] != revision["id"]:
                        self.send_json({"error": "idempotency_conflict"}, 409, origin)
                        return
                    serve_recommendation(keyed, 200, duplicate=True)
                    return
            existing = self.db.latest_active_recommendation(site["tenant_id"], conversation["id"])
            if existing and existing["requirement_revision_id"] == revision["id"]:
                serve_recommendation(existing, 200)
                return
            values = json.loads(revision["values_json"])
            try:
                full_results, missing = classify_products(self.db, site["tenant_id"], values)
                status = overall_status(full_results, missing)
                recommendation_id = self.db.save_recommendation(
                    site["tenant_id"], conversation["id"], revision["id"], status, full_results,
                    tentative_viewed=include_tentative, idempotency_key=idempotency_key or None,
                )
            except sqlite3.IntegrityError:
                # 并发同键：另一请求已写入，回滚本次事务并返回已存结果。
                self.db.conn.rollback()
                keyed = self.db.conn.execute(
                    "SELECT * FROM recommendations WHERE tenant_id=? AND conversation_id=? AND idempotency_key=?",
                    (site["tenant_id"], conversation["id"], idempotency_key),
                ).fetchone()
                if not keyed:
                    raise
                serve_recommendation(keyed, 200, duplicate=True)
                return
            except Exception:
                self.send_json({"error": "matching_unavailable", "message": "匹配服务暂不可用；已确认工况仍保留，可稍后重试或请求工程师协助。"}, 503, origin)
                return
            results = [with_site_url(result) for result in buyer_visible_results(full_results, include_tentative)]
            self.send_json(
                {"recommendation_id": recommendation_id, "status": status, "results": results, "missing": missing, **recommendation_flags(full_results, status)}, 201,
                origin,
            )
            return
        api_match = re.fullmatch(r"/api/v1/buyer/conversations/([a-f0-9]+)/questions", path)
        if api_match:
            access = self.api_conversation(api_match.group(1))
            if not access:
                return
            payload = self.read_json()
            if payload is None:
                return
            question = payload.get("question")
            if not isinstance(question, str) or not question.strip():
                self.send_json({"error": "question_required"}, 422)
                return
            site, conversation = access
            origin = site["allowed_origin"] if self.headers.get("Origin") else None
            product_id = payload.get("product_id")
            if product_id and (not isinstance(product_id, str) or not self.db.get_published_version(site["tenant_id"], product_id)):
                self.send_json({"error": "product_not_found"}, 404)
                return
            idempotency_key = self.headers.get("Idempotency-Key", "").strip()
            request_hash = hashlib.sha256(json.dumps({"question": question.strip(), "product_id": product_id or None}, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()

            def lookup_keyed_question() -> sqlite3.Row | None:
                return self.db.conn.execute(
                    "SELECT id, request_hash FROM messages WHERE tenant_id=? AND conversation_id=? AND idempotency_key=?",
                    (site["tenant_id"], conversation["id"], idempotency_key),
                ).fetchone()

            def saved_answer_for(question_id: str) -> sqlite3.Row | None:
                return self.db.conn.execute(
                    "SELECT * FROM answers WHERE tenant_id=? AND conversation_id=? AND question_message_id=?",
                    (site["tenant_id"], conversation["id"], question_id),
                ).fetchone()

            def send_answer(answer: sqlite3.Row, code: int, duplicate: bool = False) -> None:
                response = {"answer_id": answer["id"], "status": answer["answer_status"], "answer": answer["answer_text"], "citations": json.loads(answer["citations_json"])}
                if duplicate:
                    response["duplicate"] = True
                self.send_json(response, code, origin)

            question_message_id = None
            if idempotency_key:
                existing = lookup_keyed_question()
                if existing:
                    if existing["request_hash"] and not hmac.compare_digest(existing["request_hash"], request_hash):
                        self.send_json({"error": "idempotency_conflict"}, 409, origin)
                        return
                    existing_answer = saved_answer_for(existing["id"])
                    if existing_answer:
                        send_answer(existing_answer, 200, duplicate=True)
                        return
                    # 首次尝试只落了问题、未落答案（如生成失败）：复用已存问题继续生成，不重复存消息。
                    question_message_id = existing["id"]
            if question_message_id is None:
                try:
                    question_message_id = self.db.save_message(
                        site["tenant_id"], conversation["id"], "buyer", question, "website_api",
                        idempotency_key=idempotency_key or None, request_hash=request_hash if idempotency_key else None,
                    )
                except sqlite3.IntegrityError:
                    # 并发同键：另一请求已写入问题，沿用该问题继续生成。
                    self.db.conn.rollback()
                    existing = lookup_keyed_question()
                    if not existing:
                        raise
                    if existing["request_hash"] and not hmac.compare_digest(existing["request_hash"], request_hash):
                        self.send_json({"error": "idempotency_conflict"}, 409, origin)
                        return
                    question_message_id = existing["id"]
            try:
                status, answer_text, answered_product_id, version_id, citations, _reason = answer_from_approved_material(self.db, site["tenant_id"], question, product_id or None)
                self.db.save_message(site["tenant_id"], conversation["id"], "assistant", answer_text, "grounded-answer" if status == "grounded" else "unresolved-question")
                try:
                    answer_id = self.db.save_answer(site["tenant_id"], conversation["id"], question_message_id, answered_product_id, version_id, status, answer_text, citations)
                except sqlite3.IntegrityError:
                    # 并发同键：另一请求已保存答案，回滚本次事务并返回已存答案。
                    self.db.conn.rollback()
                    stored = saved_answer_for(question_message_id)
                    if not stored:
                        raise
                    send_answer(stored, 200, duplicate=True)
                    return
            except Exception:
                # 生成失败时不落任何答案、不建任务；问题已保留，会话可继续。
                self.send_json({"error": "service_unavailable", "message": "答案服务暂不可用；您的问题已保存，可稍后重试或请求工程师答复。"}, 503, origin)
                return
            self.send_json(
                {"answer_id": answer_id, "status": status, "answer": answer_text, "citations": citations}, 201,
                origin,
            )
            return
        api_match = re.fullmatch(r"/api/v1/buyer/conversations/([a-f0-9]+)/human-requests", path)
        if api_match:
            access = self.api_conversation(api_match.group(1))
            if not access:
                return
            payload = self.read_json()
            if payload is None:
                return
            if payload.get("reply_path") != "conversation" or not isinstance(payload.get("answer_id"), str):
                self.send_json({"error": "invalid_human_request"}, 422)
                return
            site, conversation = access
            answer = self.db.conn.execute(
                "SELECT question_message_id FROM answers WHERE tenant_id=? AND conversation_id=? AND id=? AND answer_status='needs_human'",
                (site["tenant_id"], conversation["id"], payload["answer_id"]),
            ).fetchone()
            if not answer:
                self.send_json({"error": "unresolved_question_not_found"}, 404)
                return
            existing = self.db.conn.execute(
                "SELECT id FROM human_tasks WHERE tenant_id=? AND conversation_id=? AND question_message_id=?",
                (site["tenant_id"], conversation["id"], answer["question_message_id"]),
            ).fetchone()
            task_id = self.db.request_human_review(site["tenant_id"], conversation["id"], payload["answer_id"])
            self.send_json(
                {"task_id": task_id, "status": "pending"}, 200 if existing else 201,
                site["allowed_origin"] if self.headers.get("Origin") else None,
            )
            return
        api_match = re.fullmatch(r"/api/v1/buyer/conversations/([a-f0-9]+)/human-tasks/([a-f0-9]+)/responses", path)
        if api_match:
            access = self.api_conversation(api_match.group(1))
            if not access:
                return
            payload = self.read_json()
            if payload is None:
                return
            answer_text = payload.get("text")
            if not isinstance(answer_text, str) or not answer_text.strip():
                self.send_json({"error": "text_required"}, 422)
                return
            site, conversation = access
            try:
                self.db.answer_task_followup(site["tenant_id"], conversation["id"], api_match.group(2), answer_text)
            except ValueError:
                self.send_json({"error": "followup_not_pending"}, 409)
                return
            self.send_json({"task_id": api_match.group(2), "status": "pending"}, 201, site["allowed_origin"] if self.headers.get("Origin") else None)
            return
        api_match = re.fullmatch(r"/api/v1/buyer/conversations/([a-f0-9]+)/contact-preference", path)
        if api_match:
            access = self.api_conversation(api_match.group(1))
            if not access:
                return
            payload = self.read_json()
            if payload is None:
                return
            reason = payload.get("reason")
            if payload.get("stop_proactive_contact") is not True or not isinstance(reason, str) or not reason.strip():
                self.send_json({"error": "stop_reason_required"}, 422)
                return
            site, conversation = access
            created = self.db.stop_proactive_contact(site["tenant_id"], conversation["id"], reason, "buyer_chat", "buyer")
            self.send_json({"stop_proactive_contact": True}, 201 if created else 200, site["allowed_origin"] if self.headers.get("Origin") else None)
            return
        api_match = re.fullmatch(r"/api/v1/buyer/conversations/([a-f0-9]+)/rfqs", path)
        if api_match:
            access = self.api_conversation(api_match.group(1))
            if not access:
                return
            payload = self.read_json()
            if payload is None:
                return
            site, conversation = access
            contact = payload.get("contact")
            submission_key = self.headers.get("Idempotency-Key", "").strip()
            entry_surface = payload.get("entry_surface", "chat")
            if not isinstance(contact, dict) or not all(isinstance(value, str) for value in contact.values()) or payload.get("contact_confirmed") is not True or not submission_key or entry_surface not in {"chat", "product_page"}:
                self.send_json({"error": "confirmed_contact_and_submission_key_required"}, 422)
                return
            if not (str(contact.get("contact_email", "")).strip() or str(contact.get("contact_phone", "")).strip()):
                self.send_json({"error": "reply_contact_required"}, 422)
                return
            notes = payload.get("notes", "")
            product_id = payload.get("product_id")
            if not isinstance(notes, str) or (product_id is not None and not isinstance(product_id, str)):
                self.send_json({"error": "invalid_rfq"}, 422)
                return
            if product_id and not self.db.get_published_version(site["tenant_id"], product_id):
                self.send_json({"error": "product_not_found"}, 404)
                return
            try:
                rfq_id, created = self.db.create_rfq(site["tenant_id"], conversation["id"], submission_key, contact, notes, product_id, entry_surface)
            except ConflictError:
                self.send_json({"error": "idempotency_conflict"}, 409)
                return
            row = self.db.get_rfq(site["tenant_id"], rfq_id)
            related = self.db.related_rfqs_in_conversation(site["tenant_id"], conversation["id"], row["product_id"], rfq_id)
            self.send_json(
                {
                    "rfq_id": rfq_id, "status": row["status"], "missing": json.loads(row["missing_json"]),
                    "related_rfq_hint": [
                        {"rfq_id": item["id"], "current_revision": item["current_revision"], "created_at": item["created_at"]}
                        for item in related
                    ],
                }, 201 if created else 200,
                site["allowed_origin"] if self.headers.get("Origin") else None,
            )
            return
        api_match = re.fullmatch(r"/api/v1/buyer/conversations/([a-f0-9]+)/rfqs/([a-f0-9]+)/revisions", path)
        if api_match:
            access = self.api_conversation(api_match.group(1))
            if not access:
                return
            payload = self.read_json()
            if payload is None:
                return
            site, conversation = access
            rfq_id = api_match.group(2)
            rfq = self.db.get_rfq(site["tenant_id"], rfq_id)
            if not rfq or rfq["conversation_id"] != conversation["id"]:
                self.send_json({"error": "rfq_not_found"}, 404)
                return
            key = self.headers.get("Idempotency-Key", "").strip()
            expected = payload.get("expected_revision")
            contact = payload.get("contact", {})
            if payload.get("contact_confirmed") is not True or not key or type(expected) is not int or not isinstance(contact, dict) or not all(isinstance(value, str) for value in contact.values()):
                self.send_json({"error": "invalid_rfq_revision"}, 422)
                return
            allowed = {"quantity", "destination", "requested_delivery", "notes", "product_id"}
            contact_allowed = {"contact_name", "company", "contact_email", "contact_phone"}
            if any(field not in allowed for field in payload if field not in {"expected_revision", "contact_confirmed", "contact"}) or any(field not in contact_allowed for field in contact):
                self.send_json({"error": "invalid_rfq_fields"}, 422)
                return
            changes = {field: value for field, value in payload.items() if field in allowed}
            changes.update(contact)
            if not all((value is None or isinstance(value, str)) if field == "product_id" else isinstance(value, str) for field, value in changes.items()):
                self.send_json({"error": "invalid_rfq_fields"}, 422)
                return
            try:
                revision, created = self.db.revise_rfq(site["tenant_id"], conversation["id"], rfq_id, expected, changes, key)
            except ConflictError:
                self.send_json({"error": "revision_conflict"}, 409)
                return
            except ValueError as exc:
                self.send_json({"error": str(exc)}, 422)
                return
            current = self.db.get_rfq(site["tenant_id"], rfq_id)
            self.send_json(
                {"rfq_id": rfq_id, "current_revision": revision, "status": current["status"], "missing": json.loads(current["missing_json"])},
                201 if created else 200, site["allowed_origin"] if self.headers.get("Origin") else None,
            )
            return
        if path == "/login":
            self.login_post(parse_multipart(self))
            return
        if path == "/logout":
            self.logout()
            return
        if path.startswith("/admin/") and not self.authorize_admin(path, "POST"):
            return
        buyer_match = re.fullmatch(r"/inquiry/([a-f0-9]+)/(message|confirm|ask|human-request|rfq|rfq/[a-f0-9]+/revise)", path)
        if buyer_match and not self.authorize_buyer_conversation(buyer_match.group(1)):
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
            if path == "/admin/sites":
                self.db.create_buyer_site(self.tenant_id, str(form.get("allowed_origin", "")))
                self.redirect("/admin/sites?notice=created")
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
                self.db.save_message(self.tenant_id, conversation_id, "buyer", message, "buyer_web")
                previous = self.db.get_latest_revision(self.tenant_id, conversation_id)
                previous_values = json.loads(previous["values_json"]) if previous else {}
                explicit = {key: str(form.get(key, "")) for key in REQUIREMENT_LABELS}
                for key in REQUIREMENT_LABELS:
                    explicit[f"{key}_unit"] = str(form.get(f"{key}_unit", ""))
                values = extract_requirement_values(message, explicit, previous_values)
                self.db.create_requirement_revision(self.tenant_id, conversation_id, values, "candidate", "buyer-web")
                missing = [REQUIREMENT_LABELS[key] for key in MATCH_REQUIRED_FIELDS if not values.get(key, {}).get("value", "").strip()]
                if missing:
                    reply = "I captured your message. To compare products, please confirm: " + ", ".join(missing) + "."
                else:
                    reply = "I extracted the conditions above. Please check and confirm the summary before matching."
                self.db.save_message(self.tenant_id, conversation_id, "assistant", reply, "rule-based-assistant")
                self.redirect(f"/inquiry/{conversation_id}")
                return
            match = re.fullmatch(r"/inquiry/([a-f0-9]+)/confirm", path)
            if match:
                conversation_id = match.group(1)
                submission_key = str(form.get("submission_key", "")).strip()
                if submission_key and self.db.conn.execute(
                    "SELECT id FROM requirement_revisions WHERE tenant_id=? AND conversation_id=? AND idempotency_key=?",
                    (self.tenant_id, conversation_id, submission_key),
                ).fetchone():
                    # 同一页面渲染内的重复提交（双击/重试）：沿用首次结果，不新建修订。
                    self.redirect(f"/inquiry/{conversation_id}?notice=recommendation")
                    return
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
                revision_id = self.db.create_requirement_revision(self.tenant_id, conversation_id, confirmed_values, "confirmed", "buyer-web", idempotency_key=submission_key or None)
                status, results, _missing = match_products(self.db, self.tenant_id, confirmed_values)
                self.db.save_recommendation(self.tenant_id, conversation_id, revision_id, status, results)
                self.redirect(f"/inquiry/{conversation_id}?notice=recommendation")
                return
            match = re.fullmatch(r"/inquiry/([a-f0-9]+)/ask", path)
            if match:
                conversation_id = match.group(1)
                question = str(form.get("question", "")).strip()
                if not question:
                    raise ValueError("问题不能为空")
                submission_key = str(form.get("submission_key", "")).strip()
                if submission_key and self.db.conn.execute(
                    "SELECT id FROM messages WHERE tenant_id=? AND conversation_id=? AND idempotency_key=?",
                    (self.tenant_id, conversation_id, submission_key),
                ).fetchone():
                    # 同一页面渲染内的重复提交：沿用首次问答，不重复提问。
                    self.redirect(f"/inquiry/{conversation_id}?notice=answer")
                    return
                try:
                    question_message_id = self.db.save_message(self.tenant_id, conversation_id, "buyer", question, "buyer_web", idempotency_key=submission_key or None)
                except sqlite3.IntegrityError:
                    self.db.conn.rollback()
                    self.redirect(f"/inquiry/{conversation_id}?notice=answer")
                    return
                selected_product_id = str(form.get("product_id", "")).strip() or None
                recommendation = self.db.latest_active_recommendation(self.tenant_id, conversation_id)
                result_for_product = None
                if recommendation:
                    recommendation_results = json.loads(recommendation["results_json"])
                    if selected_product_id:
                        result_for_product = next((result for result in recommendation_results if result.get("product_id") == selected_product_id), None)
                    if not result_for_product:
                        result_for_product = next((result for result in recommendation_results if result.get("status") == "candidate"), None)
                    selected_product_id = (result_for_product or {}).get("product_id") or selected_product_id
                answer_status, answer_text, answered_product_id, product_version_id, citations, _reason = answer_from_approved_material(self.db, self.tenant_id, question, selected_product_id)
                self.db.save_message(self.tenant_id, conversation_id, "assistant", answer_text, "grounded-answer" if answer_status == "grounded" else "unresolved-question")
                self.db.save_answer(self.tenant_id, conversation_id, question_message_id, answered_product_id, product_version_id, answer_status, answer_text, citations)
                self.redirect(f"/inquiry/{conversation_id}?notice=answer")
                return
            match = re.fullmatch(r"/inquiry/([a-f0-9]+)/human-request", path)
            if match:
                conversation_id = match.group(1)
                self.db.request_human_review(self.tenant_id, conversation_id, str(form.get("answer_id", "")))
                self.redirect(f"/inquiry/{conversation_id}?notice=human-request")
                return
            match = re.fullmatch(r"/inquiry/([a-f0-9]+)/rfq", path)
            if match:
                conversation_id = match.group(1)
                submission_key = str(form.get("submission_key", "")).strip()
                if not submission_key:
                    raise ValueError("询价提交标识缺失，请刷新页面后重试")
                contact = {key: str(form.get(key, "")) for key in ("contact_name", "company", "contact_email", "contact_phone", "quantity", "region", "delivery")}
                rfq_id, created = self.db.create_rfq(self.tenant_id, conversation_id, submission_key, contact, str(form.get("notes", "")), str(form.get("product_id", "")) or None)
                self.redirect(f"/inquiry/{conversation_id}?notice=rfq&rfq_id={rfq_id}&created={'1' if created else '0'}")
                return
            match = re.fullmatch(r"/inquiry/([a-f0-9]+)/rfq/([a-f0-9]+)/revise", path)
            if match:
                conversation_id, rfq_id = match.group(1), match.group(2)
                submission_key = str(form.get("submission_key", "")).strip()
                if not submission_key:
                    raise ValueError("修改标识缺失，请刷新页面后重试")
                try:
                    expected_revision = int(str(form.get("expected_revision", "")).strip())
                except ValueError:
                    raise ValueError("修改基准版本无效，请刷新页面后重试")
                changes = {
                    "contact_name": str(form.get("contact_name", "")),
                    "company": str(form.get("company", "")),
                    "contact_email": str(form.get("contact_email", "")),
                    "contact_phone": str(form.get("contact_phone", "")),
                    "quantity": str(form.get("quantity", "")),
                    "destination": str(form.get("region", "")),
                    "requested_delivery": str(form.get("delivery", "")),
                    "notes": str(form.get("notes", "")),
                }
                try:
                    self.db.revise_rfq(self.tenant_id, conversation_id, rfq_id, expected_revision, changes, submission_key)
                except ConflictError:
                    self.redirect(f"/inquiry/{conversation_id}?notice=rfq_stale&rfq_id={rfq_id}")
                    return
                except ValueError as error:
                    notice = "rfq_contact" if "联系方式" in str(error) else "rfq_missing"
                    self.redirect(f"/inquiry/{conversation_id}?notice={notice}&rfq_id={rfq_id}")
                    return
                self.redirect(f"/inquiry/{conversation_id}?notice=rfq&rfq_id={rfq_id}&created=0&revised=1")
                return
            match = re.fullmatch(r"/admin/tasks/([a-f0-9]+)/reply", path)
            if match:
                self.db.resolve_human_task(self.tenant_id, match.group(1), str(form.get("response", "")), self.actor)
                self.redirect(f"/admin/tasks/{match.group(1)}?notice=resolved")
                return
            match = re.fullmatch(r"/admin/tasks/([a-f0-9]+)/followup", path)
            if match:
                self.db.ask_task_followup(self.tenant_id, match.group(1), str(form.get("question", "")), self.actor)
                self.redirect(f"/admin/tasks/{match.group(1)}?notice=followup")
                return
            match = re.fullmatch(r"/admin/sales/([a-f0-9]+)/update", path)
            if match:
                self.db.update_lead(self.tenant_id, match.group(1), str(form.get("stage", "new")), str(form.get("next_follow_up", "")), str(form.get("note", "")), self.actor)
                self.redirect(f"/admin/sales/{match.group(1)}?notice=updated")
                return
            match = re.fullmatch(r"/admin/sales/([a-f0-9]+)/(claim|assign)", path)
            if match:
                if match.group(2) == "claim":
                    self.db.claim_conversation_leads(self.tenant_id, match.group(1), self.actor)
                else:
                    self.db.assign_conversation_leads(self.tenant_id, match.group(1), str(form.get("owner_email", "")), self.actor)
                self.redirect(f"/admin/sales/{match.group(1)}?notice=assigned")
                return
            match = re.fullmatch(r"/admin/sales/([a-f0-9]+)/contact-stop", path)
            if match:
                if self.user_role not in {"admin", "sales"}:
                    self.send_html(self.console_page("无权操作", "<div class='error'>只有销售或管理员可记录买方联系要求。</div>", "sales"), 403)
                    return
                rfq = self.db.get_rfq(self.tenant_id, match.group(1))
                if not rfq:
                    raise ValueError("询价不存在")
                self.db.stop_proactive_contact(self.tenant_id, rfq["conversation_id"], str(form.get("reason", "")), "seller_record", self.actor)
                self.redirect(f"/admin/sales/{match.group(1)}?notice=contact-stop")
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
                raw_file = form.get("source_file")
                content = raw_file if isinstance(raw_file, bytes) else None
                filename = str(form.get("source_file__filename", "")) or str(form.get("filename", ""))
                self.db.add_source(
                    self.tenant_id, product_id, filename or "未命名资料",
                    str(form.get("file_type", "other")), str(form.get("source_location", "")),
                    content, self.actor, scope=str(form.get("scope", "unknown") or "unknown"),
                )
                self.redirect(f"/admin/products/{product_id}?notice=source")
                return
            match = re.fullmatch(r"/admin/products/([a-f0-9]+)/sources/([a-f0-9]+)/(allow-public|revoke-public|scope|extract)", path)
            if match:
                product_id, source_id, action = match.groups()
                if action == "allow-public":
                    self.db.allow_public_source(self.tenant_id, product_id, source_id, self.actor)
                elif action == "revoke-public":
                    self.db.revoke_public_source(self.tenant_id, product_id, source_id, self.actor)
                elif action == "scope":
                    scope_result = self.db.set_source_scope(self.tenant_id, product_id, source_id, str(form.get("scope", "")), self.actor)
                    if scope_result.get("public_permission_revoked"):
                        action = "scope-revoked"
                else:
                    self.db.rerun_source_extraction(self.tenant_id, product_id, source_id, self.actor)
                self.redirect(f"/admin/products/{product_id}?notice={action}")
                return
            match = re.fullmatch(r"/admin/products/([a-f0-9]+)/fields", path)
            if match:
                product_id = match.group(1)
                self.db.save_field(self.tenant_id, product_id, str(form.get("field_key", "")), str(form.get("value", "")), str(form.get("unit", "")), str(form.get("source_document_id", "")) or None, str(form.get("source_location", "")), self.actor)
                self.redirect(f"/admin/products/{product_id}?notice=field")
                return
            match = re.fullmatch(r"/admin/products/([a-f0-9]+)/fields/([a-z_]+)/(approve|reject|reopen)", path)
            if match:
                product_id, field_key, action = match.groups()
                if action == "approve":
                    self.db.approve_field(self.tenant_id, product_id, field_key, self.actor)
                elif action == "reject":
                    self.db.reject_field(self.tenant_id, product_id, field_key, str(form.get("reason", "")), self.actor)
                else:
                    self.db.reopen_field(self.tenant_id, product_id, field_key, self.actor)
                self.redirect(f"/admin/products/{product_id}?notice=field-{action}")
                return
            match = re.fullmatch(r"/admin/products/([a-f0-9]+)/(approve|finalize-review|publish|withdraw)", path)
            if match:
                product_id, action = match.groups()
                if action in ("approve", "finalize-review"):
                    self.db.finalize_product_review(self.tenant_id, product_id, self.actor)
                elif action == "publish":
                    self.db.publish_product(self.tenant_id, product_id, self.actor)
                else:
                    self.db.withdraw_product(self.tenant_id, product_id, self.actor)
                notice = "approve" if action == "finalize-review" else action
                self.redirect(f"/admin/products/{product_id}?notice={notice}")
                return
        except (ValueError, sqlite3.Error) as exc:
            if path.startswith("/inquiry/"):
                return_path = path.rsplit("/", 1)[0]
                self.send_html(layout("操作未完成", f"<div class='error'>{esc(exc)}</div><p><a href='{esc(return_path)}'>返回并修改</a></p>"), 400)
                return
            product_match = re.match(r"(/admin/products/[a-f0-9]+)(/|$)", path)
            return_path = product_match.group(1) if product_match else "/admin/products"
            self.send_html(self.console_page("操作未完成", f"<div class='error'>{esc(exc)}</div><p><a href='{esc(return_path)}'>返回并修改</a></p>", ""), 400)
            return
        if path.startswith("/inquiry/"):
            self.send_html(layout("未找到", "<div class='error'>操作不存在</div>"), 404)
            return
        self.send_html(self.console_page("未找到", "<div class='error'>操作不存在</div>", ""), 404)

    def admin_products(self, query: dict[str, list[str]]) -> None:
        lang = query.get("lang", ["zh"])[0]
        products = self.db.list_products(self.tenant_id)
        notice = query.get("notice", [""])[0]
        notice_html = "<div class='notice'>示例资料已登记为候选值，请逐个核对后核准，再发布到买方产品页。</div>" if notice == "demo" else ""
        if notice == "lowara":
            notice_html = f"<div class='notice'>Lowara 资料已加载：新增 {esc(query.get('created', ['0'])[0])} 个内部演示型号。来源文件只登记元数据，外部再发布授权仍待确认。</div>"
        item_parts = []
        for product in products:
            published = self.db.get_published_version(self.tenant_id, product["id"])
            if product["status"] == "pending_review":
                next_label, hint = "继续核对资料", "有候选字段待逐项处理；未核准内容不会进入公开页"
            elif product["status"] == "approved":
                next_label, hint = "检查并发布", "字段已核准，可核对并发布公开版本"
            elif product["status"] == "published":
                next_label, hint = "查看与管理", f"已发布 v{published['version']}；修改字段后会回到待核对" if published else "已发布"
            elif product["status"] == "withdrawn":
                next_label, hint = "查看与管理", "公开页已撤回；处理字段后可重新发布"
            else:
                next_label, hint = "继续完善", "登记来源并逐项核准字段"
            public_link = (
                f"<a class='button secondary' href='/products/{esc(product['slug'])}' target='_blank'>买方页 ↗</a>"
                if product["status"] != "withdrawn" and published
                else ""
            )
            item_parts.append(
                f"<div class='simple-item'><div><strong>{esc(product['model'])} · {esc(product['name'])} {status_badge(product['status'])}</strong>"
                f"<small>{esc(hint)} · {product['approved_count']} 个字段已核准 · {product['source_count']} 份来源</small></div>"
                f"<div class='actions'>{public_link}<a class='button' href='/admin/products/{product['id']}'>{esc(next_label)}</a></div></div>"
            )
        items_html = "".join(item_parts) or "<div class='muted-block'>还没有产品，请先导入示例或新建型号。</div>"
        body = f"""
<div class='toolbar'><div><h1>{'产品资料与发布' if lang == 'zh' else 'Products & publishing'}</h1><div class='muted'>企业：示例水泵企业（演示） · 当前操作员：管理员</div></div><div><form method='post' action='/admin/products/seed' class='inline'><button class='secondary'>导入两款示例水泵</button></form> <form method='post' action='/admin/products/seed-lowara' class='inline'><button class='secondary'>加载 Lowara 资料</button></form></div></div>
{notice_html}
<section class='card'><div class='panel-title'><h2>产品列表</h2><span class='muted'>选择产品后完成核对与发布</span></div><div class='simple-list'>{items_html}</div>
<details class='detail-toggle'><summary>新建产品型号</summary><div><form method='post' action='/admin/products'><label>型号 *</label><input name='model' placeholder='例如 CB-80A' required><label>产品名称</label><input name='name' placeholder='买方页面显示名称'><label>主要用途</label><textarea name='use_case' placeholder='例如清水循环、化工介质输送'></textarea><p><button>创建草稿</button></p></form></div></details>
<details class='detail-toggle'><summary>查看流程说明</summary><div class='muted-block'>产品资料登记 → 来源文件 → 字段候选值 → 人工核准 → 版本发布 → 固定买方页面。未核准字段不会进入公开页；重新编辑已发布字段会回到待核准状态。Lowara 数据仅用于内部 Demo，外部再发布授权待确认。</div></details></section>
"""
        self.send_html(self.console_page("产品资料与发布", body, "products", lang=lang), 200)

    def admin_product(self, product_id: str, query: dict[str, list[str]]) -> None:
        product = self.db.get_product(self.tenant_id, product_id)
        if not product:
            self.send_html(self.console_page("未找到", "<div class='error'>产品不存在或不属于当前企业。</div>", "products"), 404)
            return
        sources = self.db.get_sources(self.tenant_id, product_id)
        fields = self.db.get_fields(self.tenant_id, product_id)
        published_version = self.db.get_published_version(self.tenant_id, product_id)
        published_snapshot = json.loads(published_version["snapshot_json"]) if published_version else None
        published_source_ids = {entry["id"] for entry in (published_snapshot or {}).get("public_sources", [])}
        notice = query.get("notice", [""])[0]
        notice_text = {
            "source": "来源已保存。已上传的文件已按内容尝试抽取；请核对下方提取结果后逐项处理。",
            "source-permission": "文件公开权限已更新；新文件在下次发布后才会进入买方页。",
            "scope": "文件归属已更新。",
            "scope-revoked": "文件归属已更新；原公开许可已自动撤销，旧版本不再展示该文件。如需重新公开，请重新确认许可并发布新版本。",
            "extract": "文件已按内容重新解析，结果在来源区显示。",
            "field": "字段已保存为候选值，需逐项核准或驳回后才能完成核对。",
            "field-approve": "字段已核准。",
            "field-reject": "字段已驳回；未核准内容不会进入匹配、问答或公开页。",
            "field-reopen": "字段已退回待核；请重新逐项处理。",
            "approve": "核对完成：全部候选字段已逐项处理，可发布新版本。",
            "publish": "产品版本已发布。",
            "withdraw": "公开版本已撤回。",
        }.get(notice, "")
        processing_labels = {
            "registered": "仅登记元数据（无文件内容）",
            "stored": "已保存文件",
            "extracted": "已提取待核对",
            "extraction_failed": "提取失败：未识别到字段",
            "extraction_unsupported": "不支持自动抽取",
        }
        review_labels = {
            "new": "新候选",
            "updated": "更新候选",
            "duplicate": "与已核准值一致",
            "conflict": "与已核准值冲突，未覆盖",
            "kept_manual": "保留手工值",
            "rejected_kept": "字段已驳回，未变更",
        }
        review_css = {"duplicate": "approved"}
        origin_labels = {"manual": "手工录入", "extracted": "文件提取", "seed": "示例数据"}
        approved_keys = {field["field_key"] for field in fields if field["state"] == "approved"}
        missing_keys = [key for key in REQUIRED_FIELDS if key not in approved_keys]
        missing_html = (
            "<div class='error'>关键字段缺项：" + esc("、".join(FIELD_LABELS[key] for key in missing_keys))
            + "。有限信息页仍可发布；缺项会在买方页标为待补充，不会填写推测值。</div>"
            if missing_keys else "<div class='notice'>关键字段已齐全；买方页展示当前版本已核准内容。</div>"
        )
        pending_fields = [field for field in fields if field["state"] == "candidate"]
        pending_html = (
            f"<div class='error'>还有 {len(pending_fields)} 个候选字段未逐项处理（核准或驳回），全部处理后才能完成核对与发布。</div>"
            if pending_fields else "<p class='muted'>候选字段已全部逐项处理。</p>"
        )

        def field_state_badge(state: str) -> str:
            label = {"candidate": "待核对", "approved": "已核准", "rejected": "已驳回"}.get(state, state)
            css = {"candidate": "pending_review", "approved": "approved", "rejected": "withdrawn"}.get(state, "")
            return f"<span class='badge {css}'>{label}</span>"

        field_rows = []
        for field in fields:
            review_bits = []
            if field["state"] == "approved":
                review_bits.append(f"核准：{esc(field['approved_by'] or '')} · {esc(field['approved_at'] or '')}")
            if field["state"] == "rejected" and field["review_note"]:
                review_bits.append(f"驳回原因：{esc(field['review_note'])}")
            if field["state"] == "candidate":
                actions = (
                    f"<form method='post' action='/admin/products/{product_id}/fields/{esc(field['field_key'])}/approve' class='inline'><button class='secondary'>核准此字段</button></form> "
                    f"<form method='post' action='/admin/products/{product_id}/fields/{esc(field['field_key'])}/reject' class='inline'><input name='reason' placeholder='驳回原因（可选）' style='width:160px'><button class='danger'>驳回</button></form>"
                )
            elif field["state"] == "approved":
                actions = f"<form method='post' action='/admin/products/{product_id}/fields/{esc(field['field_key'])}/reopen' class='inline'><button class='secondary'>退回待核</button></form>"
            else:
                actions = f"<form method='post' action='/admin/products/{product_id}/fields/{esc(field['field_key'])}/approve' class='inline'><button class='secondary'>重新核准</button></form>"
            field_rows.append(
                f"<tr><td><b>{esc(field['label'])}</b><br><span class='field-state'>{esc(field['field_key'])} · {esc(origin_labels.get(field['origin'], field['origin']))}</span></td>"
                f"<td>{esc(field['value'])} {esc(field['unit'])}</td>"
                f"<td>{field_state_badge(field['state'])}<br><span class='field-state'>{esc(field['source_filename'] or '未关联来源')} · {esc(field['source_location'])}</span>"
                + ("<br><span class='field-state'>" + "<br>".join(review_bits) + "</span>" if review_bits else "")
                + f"</td><td>{actions}</td></tr>"
            )
        field_table = "".join(field_rows) or "<tr><td colspan='4' class='muted'>尚未登记字段。</td></tr>"
        source_options = "<option value=''>请选择来源</option>" + "".join(f"<option value='{esc(source['id'])}'>{esc(source['filename'])} · {esc(source['source_location'])}</option>" for source in sources)
        public_source_ids = self.db.public_source_ids(self.tenant_id, product_id)
        # Files that the current published version can actually serve right now
        # (snapshot membership + live permission + matching scope/revision). A
        # correction loses the third condition until the next publish, so the
        # labels below distinguish "live in this version" from "republish needed".
        servable_source_ids = self.db.public_downloadable_ids(self.tenant_id, product_id)

        source_blocks = []
        for source in sources:
            extraction_items = json.loads(source["extraction_json"] or "[]")
            item_html = "".join(
                f"<li><b>{esc(item.get('label', item.get('field_key', '')))}</b>：{esc(item.get('value', ''))} {esc(item.get('unit', ''))}"
                f" <span class='field-state'>{esc(item.get('location', ''))}</span> "
                f"<span class='badge {review_css.get(item.get('review_status', ''), 'pending_review' if item.get('review_status') in ('new', 'updated') else 'withdrawn')}'>{esc(review_labels.get(item.get('review_status', ''), item.get('review_status', '')))}</span>"
                + (f" <span class='field-state'>原值：{esc(item.get('existing_value', ''))} {esc(item.get('existing_unit', ''))}</span>" if item.get("existing_value") else "")
                + "</li>"
                for item in extraction_items
            )
            extraction_html = f"<ul style='margin:8px 0 0 18px;padding:0'>{item_html}</ul>" if item_html else ""
            scope_options = "".join(
                f"<option value='{key}' {'selected' if source['scope'] == key else ''}>{label}</option>"
                for key, label in SOURCE_SCOPES.items()
            )
            scope_form = (
                f"<form method='post' action='/admin/products/{product_id}/sources/{source['id']}/scope' class='inline'>"
                f"<select name='scope'>{scope_options}</select> <button class='secondary'>保存归属</button></form>"
            )
            if source["storage_path"]:
                extract_button = f"<form method='post' action='/admin/products/{product_id}/sources/{source['id']}/extract' class='inline'><button class='secondary'>按内容重新解析</button></form>"
                if source["id"] in public_source_ids:
                    if source["id"] in servable_source_ids:
                        inclusion_html = " <span class='field-state'>已纳入当前公开版本</span>"
                    elif source["id"] in published_source_ids:
                        inclusion_html = " <span class='field-state'>当前版本快照已失效（归属修正过）；重新公开需发布新版本</span>"
                    else:
                        inclusion_html = " <span class='field-state'>将在下次发布后收录</span>"
                    perm_html = (
                        f"<form method='post' action='/admin/products/{product_id}/sources/{source['id']}/revoke-public' class='inline'><button class='danger'>撤销公开下载</button></form>"
                        + inclusion_html
                    )
                elif source["scope"] != "unknown":
                    perm_html = (
                        f"<form method='post' action='/admin/products/{product_id}/sources/{source['id']}/allow-public' class='inline'><button class='secondary'>确认许可并开放下载</button></form>"
                        " <span class='field-state'>开放后需发布新版本才进入买方页</span>"
                    )
                else:
                    perm_html = "<span class='field-state'>先保存文件归属（本型号或系列），才能开放公开下载</span>"
            else:
                extract_button = ""
                perm_html = "<span class='field-state'>无文件内容，仅登记元数据；参数请人工录入并标注位置</span>"
            source_blocks.append(
                "<div class='source' style='display:block'><div style='display:flex;justify-content:space-between;gap:12px;flex-wrap:wrap'>"
                f"<span><b>{esc(source['filename'])}</b><br><span class='muted'>{esc(source['file_type'])} · {esc(source['source_location'])}</span></span>"
                f"<span class='field-state'>{esc(processing_labels.get(source['processing_status'], source['processing_status']))} · 归属：{esc(SOURCE_SCOPES.get(source['scope'], source['scope']))}"
                + (f" · 归属修订 r{source['scope_revision']}" if source["scope_revision"] else "")
                + (f" · 校验码 {esc((source['sha256'] or '')[:10])}…" if source["sha256"] else "")
                + "</span></div>"
                + (f"<p class='field-state' style='margin:6px 0 0'>{esc(source['extraction_note'])}</p>" if source["extraction_note"] else "")
                + extraction_html
                + f"<div style='margin-top:8px;display:flex;gap:8px;flex-wrap:wrap;align-items:center'>{scope_form}{extract_button}{perm_html}</div>"
                + "</div>"
            )
        source_rows = "".join(source_blocks) or "<p class='muted'>还没有来源资料。</p>"
        field_options = "".join(f"<option value='{key}'>{esc(label)}</option>" for key, label in FIELD_LABELS.items())
        action_buttons = (
            f"<form method='post' action='/admin/products/{product_id}/finalize-review' class='inline'><button>完成字段核对</button></form> "
            f"<form method='post' action='/admin/products/{product_id}/publish' class='inline'><button class='secondary'>发布新版本</button></form>"
            + (f" <form method='post' action='/admin/products/{product_id}/withdraw' class='inline'><button class='danger'>撤回公开页</button></form>" if published_version else "")
        )
        notice_banner = f"<div class='notice'>{esc(notice_text)}</div>" if notice_text else ""
        version_line = (
            f"当前已发布版本 v{esc(published_version['version'])} · 公开文件 {len(published_source_ids)} 份（文件清单随版本冻结）"
            if published_version else "尚未发布；完成核对后可发布有限信息页"
        )
        pending_count = len(pending_fields)
        if not sources:
            focus_label, focus_title, focus_text = "当前任务 · 登记来源", "先登记这个型号的来源资料", "上传参数表等文件后，系统按内容抽取候选字段；不支持的格式会明确失败并保留手工录入。"
            focus_actions = "<a class='button' href='#sources'>去登记来源 →</a>"
        elif pending_count:
            focus_label, focus_title, focus_text = "当前任务 · 字段核对", f"核对 {pending_count} 项候选字段", "逐项核准或驳回；未核准内容不会进入公开页、匹配或问答。"
            focus_actions = "<a class='button' href='#fields'>去逐项核对 →</a>"
        elif product["status"] == "pending_review":
            focus_label, focus_title, focus_text = "当前任务 · 完成核对", "候选字段已全部处理", "完成字段核对后即可发布公开版本。"
            focus_actions = f"<form method='post' action='/admin/products/{product_id}/finalize-review' class='inline'><button>完成字段核对</button></form>"
        elif product["status"] == "approved":
            focus_label, focus_title, focus_text = "当前任务 · 发布", "内容已就绪，可发布公开新版本", "发布将冻结当前已核准事实与已开放文件清单；旧询价保留原版本快照。"
            focus_actions = f"<form method='post' action='/admin/products/{product_id}/publish' class='inline'><button>发布新版本</button></form>"
        elif product["status"] == "published" and published_version:
            focus_label, focus_title, focus_text = "当前状态 · 已发布", f"公开版本 v{published_version['version']} 正在运行", "修改字段后会回到待核对；重新发布生成新版本。"
            focus_actions = f"<a class='button secondary' href='/products/{esc(product['slug'])}' target='_blank'>查看买方页 ↗</a>"
        else:
            focus_label, focus_title, focus_text = "当前状态", "继续完善资料", "登记来源并逐项核准后才能发布。"
            focus_actions = "<a class='button' href='#sources'>去登记来源 →</a>"
        focus_html = f"<div class='focus'><div><span class='focus-label'>{esc(focus_label)}</span><strong>{esc(focus_title)}</strong><p>{esc(focus_text)}</p></div><div class='actions'>{focus_actions}</div></div>"
        summary_strip = (
            "<div class='summary-strip'>"
            f"<span>状态：{status_badge(product['status'])}</span>"
            f"<span>已核准字段：{sum(1 for field in fields if field['state'] == 'approved')} 项</span>"
            f"<span>来源文件：{len(sources)} 份</span>"
            f"<span>公开版本：{'v' + str(published_version['version']) if published_version else '未发布'}</span>"
            "</div>"
        )
        view_page_link = (
            f"<a class='button secondary' href='/products/{esc(product['slug'])}' target='_blank'>查看买方页 ↗</a>"
            if published_version and product["status"] != "withdrawn" else ""
        )
        body = f"""
<div class='toolbar'><div><a href='/admin/products'>← 返回产品列表</a><h1>{esc(product['model'])}</h1><div class='muted'>{esc(product['name'])} · {status_badge(product['status'])}</div></div><div>{view_page_link}</div></div>
{notice_banner}
{focus_html}
{summary_strip}
{missing_html}
<section class='card' id='fields'><div class='panel-title'><h2>字段核对（逐项）</h2><span class='muted'>待处理 {pending_count} 项</span></div>{pending_html}<p class='muted'>候选字段需逐项核准或驳回；驳回保留原值并记录原因。已核准关键字段被修改后会回到待核对，需重新核准并发布新版本；缺项不会被推测值填充。</p><table><thead><tr><th>字段</th><th>值</th><th>来源与状态</th><th>操作</th></tr></thead><tbody>{field_table}</tbody></table><details class='detail-toggle'><summary>手动补充 / 修改字段值</summary><div><form method='post' action='/admin/products/{product_id}/fields'><div class='form-grid'><div><label>字段</label><select name='field_key'>{field_options}</select></div><div><label>值 *</label><input name='value' required placeholder='例如 20-80'></div><div><label>单位</label><input name='unit' placeholder='m³/h、m、°C'></div><div><label>来源</label><select name='source_document_id'>{source_options}</select></div><div class='full'><label>原文位置 *</label><input name='source_location' required placeholder='第 1 页：规格参数表'></div></div><p><button>保存为候选值</button></p></form></div></details></section>
<section class='card' style='margin-top:18px'><div class='panel-title'><h2>发布与版本</h2><span class='muted'>{version_line}</span></div><div class='actions' style='display:flex;gap:8px;flex-wrap:wrap'>{action_buttons}</div><p class='muted'>发布将冻结当前已核准事实与已开放公开文件清单；撤回后买方页不可访问，已提交的询价保留原版本快照。</p></section>
<section class='card' style='margin-top:18px' id='sources'><h2>登记来源资料</h2>{source_rows}<p class='muted'>文件默认不对外下载。上传后的文本/CSV 会按内容识别已知字段标签；PDF、图片、CAD 等不支持的格式会明确失败，请人工录入。开放公开下载前必须确认文件归属。</p><hr><form method='post' action='/admin/products/{product_id}/sources' enctype='multipart/form-data'><label>文件（可选）</label><input type='file' name='source_file'><label>或资料文件名</label><input name='filename' placeholder='产品手册.pdf'><div class='form-grid'><div><label>类型</label><select name='file_type'><option>PDF</option><option>XLSX</option><option>MARKDOWN</option><option>TXT</option><option>IMAGE</option><option>DXF</option><option>STEP</option><option>OTHER</option></select></div><div><label>文件归属 *</label><select name='scope' required><option value=''>请选择文件归属</option><option value='exact_model'>本型号专属文件</option><option value='series'>系列级资料（非本型号专属）</option><option value='unknown'>归属待核对（不上公开页）</option></select></div><div class='full'><label>原文位置 *</label><input name='source_location' placeholder='第 1 页：规格参数表' required></div></div><p><button class='secondary'>保存来源</button></p></form></section>
<details class='detail-toggle' style='margin-top:18px'><summary>查看产品基本信息</summary><div class='muted-block'><p><b>主要用途：</b>{esc(product['use_case']) or '未填写'}</p><p><b>固定买方链接：</b>{('/products/' + esc(product['slug'])) if published_version else '发布后生成'}</p><p>型号是产品通用事实；买方本次工况将在后续会话中单独保存，不写回这里。</p><p>核准动作按字段逐项执行；未被逐项核准或已被驳回的内容不会进入匹配、问答和公开页。</p></div></details>
"""
        self.send_html(self.console_page(f"{product['model']} · 字段核对", body, "products"), 200)

    def buyer_inquiry(self, conversation_id: str, query: dict[str, list[str]]) -> None:
        conversation = self.db.get_conversation(self.tenant_id, conversation_id)
        if not conversation:
            self.send_html(layout("Inquiry not found", "<div class='error'>This inquiry does not exist or is not available.</div>", lang="en"), 404)
            return
        lang = query.get("lang", [conversation["locale"] or "en"])[0]
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
            "recommendation": "<div class='notice'>需求已保存，推荐结果会基于当前已确认摘要重新计算。</div>",
            "answer": "<div class='notice'>问题已保存。资料不足的问题会保留为待核实记录；只有您点击「请求工程师核实」后才会创建人工任务，不会自动安排。</div>",
            "rfq_stale": "<div class='error'>这张询价已在别处更新，请刷新页面后再修改（避免覆盖较新的版本）。</div>",
            "rfq_contact": "<div class='error'>请保留至少一种可回复联系方式（邮箱或电话）。</div>",
            "rfq_missing": "<div class='error'>没有找到要修改的询价，请刷新页面后重试。</div>",
        }.get(notice, "")
        if notice == "rfq" and current_rfq:
            missing_labels = json.loads(current_rfq["missing_json"])
            if query.get("revised", [""])[0] == "1":
                notice_html = f"<div class='notice'><b>已保存修改（v{current_rfq['current_revision']}）：</b>销售侧会看到本次变更；本页仅显示最新内容。</div>"
            elif current_rfq["status"] == "submitted":
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

        show_tentative = query.get("show_tentative", [""])[0] == "1"
        visible_results: list[dict[str, object]] = []
        recommendation_html = "<p class='muted'>Send a message and confirm the structured summary to see a recommendation.</p>"
        if current_recommendation:
            result_status = current_recommendation["status"]
            result_label = {"recommended": "可初步推荐 / Initial recommendation", "needs_confirmation": "需工程确认 / Engineering confirmation needed", "insufficient": "条件不足 / More information needed", "no_match": "无可靠候选 / No reliable candidate"}.get(result_status, result_status)
            all_results = json.loads(current_recommendation["results_json"])
            visible_results = buyer_visible_results(all_results, include_tentative=show_tentative)
            has_tentative = any(item.get("status") == "needs_confirmation" for item in all_results)
            cards = []
            for result in visible_results:
                result_badge = {"candidate": "可初步推荐 / Candidate", "needs_confirmation": "待核实（未确认适用）/ Tentative"}.get(result["status"], result["status"])
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
            tentative_choice = ""
            if has_tentative and not show_tentative:
                tentative_choice = (
                    f"<p><a class='button secondary' href='?show_tentative=1&lang={esc(lang)}'>查看待核实候选 / View tentative candidates</a> "
                    "<span class='field-state'>待核实候选（未确认适用）仅在您主动查看后展示，不构成适用结论。</span></p>"
                )
            elif has_tentative and show_tentative:
                tentative_choice = "<p class='field-state'>以上标记「待核实」的候选未确认适用，需工程师核对工作点后才能作为选型依据。</p>"
            recommendation_html = f"<div class='notice'><b>匹配结果：</b>{esc(result_label)} · 规则版本 {esc(current_recommendation['rule_version'])}（演示规则，不能替代正式工程选型）</div>{tentative_choice}" + "".join(cards)

        answer_rows = []
        tasks_by_question = {task["question_message_id"]: task for task in tasks}
        for answer in answers:
            citations = json.loads(answer["citations_json"])
            citation_html = "".join(f"<div class='field-state'>引用：{esc(citation.get('filename'))} · {esc(citation.get('location'))} · 产品版本 {esc(citation.get('product_version'))}</div>" for citation in citations)
            task = tasks_by_question.get(answer["question_message_id"])
            state_label = "有据回答" if answer["answer_status"] == "grounded" else "待核实" if not task else "已请求工程师"
            request_button = f"<form method='post' action='/inquiry/{conversation_id}/human-request'><input type='hidden' name='answer_id' value='{esc(answer['id'])}'><button class='secondary'>Ask an engineer / 请求工程师核实</button></form>" if answer["answer_status"] == "needs_human" and not task else ""
            answer_rows.append(f"<div class='source'><span><b>{state_label}</b><br>{esc(answer['answer_text'])}{citation_html}{request_button}</span><span class='field-state'>{esc(answer['created_at'])}</span></div>")
        answer_html = "".join(answer_rows) or "<p class='muted'>还没有问答记录。</p>"
        task_html = "".join(f"<div class='source'><span><b>工程师请求</b> · {esc(task['status'])}<br>{esc(task['question'])}</span></div>" for task in tasks)
        candidate_options = "<option value=''>自动选择当前推荐型号</option>"
        if current_recommendation:
            for result in visible_results:
                if result.get("status") in {"candidate", "needs_confirmation"}:
                    candidate_options += f"<option value='{esc(result['product_id'])}'>{esc(result['model'])} · v{esc(result['product_version'])}</option>"
        prefill = {key: values.get(key, {}).get("value", "") for key in ("quantity", "region", "delivery")}
        submission_key = secrets.token_urlsafe(18)
        confirm_key = secrets.token_urlsafe(18)
        ask_key = secrets.token_urlsafe(18)

        rfq_card_parts = []
        for rfq_row in self.db.conn.execute(
            "SELECT * FROM rfqs WHERE tenant_id=? AND conversation_id=? ORDER BY created_at",
            (self.tenant_id, conversation_id),
        ):
            content = self.db.rfq_current_content(rfq_row)
            edit_key = secrets.token_urlsafe(18)
            rfq_card_parts.append(
                f"<div class='source' style='flex-direction:column;align-items:stretch;gap:6px'>"
                f"<span><b>RFQ {esc(rfq_row['id'][:8])} · v{rfq_row['current_revision']} · {esc(rfq_row['status'])}</b> "
                f"<span class='field-state'>本页仅显示当前版本；历次修改由销售侧核对</span></span>"
                f"<span class='field-state'>联系人：{esc(content['contact_name'] or '—')} · 邮箱：{esc(content['contact_email'] or '—')} · 电话：{esc(content['contact_phone'] or '—')}</span>"
                f"<span class='field-state'>数量：{esc(content['quantity'] or '—')} · 目的地：{esc(content['destination'] or '—')} · 期望交期：{esc(content['requested_delivery'] or '—')}</span>"
                f"<details><summary>编辑询价 / Edit this RFQ</summary>"
                f"<form method='post' action='/inquiry/{conversation_id}/rfq/{rfq_row['id']}/revise'>"
                f"<input type='hidden' name='submission_key' value='{esc(edit_key)}'>"
                f"<input type='hidden' name='expected_revision' value='{rfq_row['current_revision']}'>"
                f"<div class='form-grid'>"
                f"<div><label>Contact name / 联系人</label><input name='contact_name' value='{esc(content['contact_name'] or '')}'></div>"
                f"<div><label>Company / 公司</label><input name='company' value='{esc(content['company'] or '')}'></div>"
                f"<div><label>Email / 邮箱</label><input name='contact_email' type='email' value='{esc(content['contact_email'] or '')}'></div>"
                f"<div><label>Phone / 电话</label><input name='contact_phone' value='{esc(content['contact_phone'] or '')}'></div>"
                f"<div><label>Quantity / 数量</label><input name='quantity' value='{esc(content['quantity'] or '')}'></div>"
                f"<div><label>Destination / 目的地</label><input name='region' value='{esc(content['destination'] or '')}'></div>"
                f"<div><label>Expected delivery / 期望交期</label><input name='delivery' value='{esc(content['requested_delivery'] or '')}'></div>"
                f"<div class='full'><label>Notes / 补充说明</label><textarea name='notes'>{esc(content['notes'] or '')}</textarea></div>"
                f"</div><p><button>Save changes / 保存修改</button></p></form></details></div>"
            )
        rfq_cards_html = "".join(rfq_card_parts) or "<p class='muted'>本会话还没有询价记录。</p>"

        body = f"""
<div class='toolbar'><div><h1>Buyer inquiry</h1><div class='muted'>Conversation {esc(conversation_id)} · 原始消息和每次需求修订都会保留</div></div><div><a class='button secondary' href='/inquiry?lang={'zh' if lang == 'en' else 'en'}'>New inquiry / 新询盘</a></div></div>
{notice_html}
<div class='grid'><section class='card'><h2>Describe your application</h2><p class='muted'>Tell us the pump use, flow, head and medium in your own words. Structured values are candidates until you confirm them.</p><form method='post' action='/inquiry/{conversation_id}/message'><label>Your message / 需求原话 *</label><textarea name='message' required placeholder='Example: I need a water pump for cooling water, flow 50 m³/h and head 30 m.'></textarea><p><button>Save message and extract fields</button></p></form><hr><h3>Conversation</h3>{message_rows}</section>
<section class='card'><h2>Requirement summary</h2>{missing_html}<form method='post' action='/inquiry/{conversation_id}/confirm'><input type='hidden' name='submission_key' value='{esc(confirm_key)}'><div class='form-grid'>{input_fields}</div><p><button>Confirm summary and match</button></p></form><table><thead><tr><th>Field</th><th>Value</th><th>State</th></tr></thead><tbody>{summary_rows}</tbody></table></section></div>
<section class='card' style='margin-top:18px'><h2>Recommendations</h2>{recommendation_html}</section>
<section class='card' style='margin-top:18px'><h2>Ask about the recommended product</h2><p class='muted'>资料内问题显示出处。资料外问题先保存为待核实；只有您点击“请求工程师核实”后才会交给工程师，在此对话查看回复无需另留联系方式。</p><form method='post' action='/inquiry/{conversation_id}/ask'><input type='hidden' name='submission_key' value='{esc(ask_key)}'><label>Product context</label><select name='product_id'>{candidate_options}</select><label>Your question / 技术问题 *</label><textarea name='question' required placeholder='Example: What is the flow range? Or ask about price, stock, certification to test handoff.'></textarea><p><button>Ask and save answer</button></p></form><h3>Answers and handoff</h3>{answer_html}{task_html}</section>
<section class='card' style='margin-top:18px'><h2>Request for quotation (RFQ)</h2><p class='muted'>您可以随时提交询价，无须先完成选型。请至少提供邮箱或电话，方便销售回复；已确认的工况会随询价保存，缺少的技术或商务信息会标为待补充。</p><form method='post' action='/inquiry/{conversation_id}/rfq'><input type='hidden' name='submission_key' value='{esc(submission_key)}'><div class='form-grid'><div><label>Product / 产品</label><select name='product_id'>{candidate_options}</select></div><div><label>Contact name / 联系人</label><input name='contact_name' placeholder='Your name'></div><div><label>Company / 公司</label><input name='company' placeholder='Company name'></div><div><label>Email / 邮箱</label><input name='contact_email' type='email' placeholder='you@example.com'></div><div><label>Phone / 电话</label><input name='contact_phone' placeholder='+49 ...'></div><div><label>Quantity / 数量</label><input name='quantity' value='{esc(prefill['quantity'])}' placeholder='e.g. 2'></div><div><label>Destination / 目的地</label><input name='region' value='{esc(prefill['region'])}' placeholder='e.g. Germany'></div><div><label>Expected delivery / 期望交期</label><input name='delivery' value='{esc(prefill['delivery'])}' placeholder='e.g. 8 weeks'></div><div class='full'><label>Notes / 补充说明</label><textarea name='notes' placeholder='Additional operating conditions or questions'></textarea></div></div><p><button>Save RFQ / 提交询价</button></p></form><h3>Your RFQs / 我的询价</h3>{rfq_cards_html}</section>
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
        role = self.user_role
        todos: list[tuple[str, str, str, str]] = []
        if role == "admin" and summary["products_needs_review"]:
            todos.append(("产品资料核对", f"{summary['products_needs_review']} 个产品有候选字段待逐项处理；未核准内容不会进入公开页与推荐", "/admin/products", "去核对"))
        if role in {"admin", "engineer"} and summary["tasks_pending"]:
            todos.append(("待答技术问题", f"{summary['tasks_pending']} 条买家问题在等待人工答复；答复会回到买家的原会话", "/admin/tasks?status=pending", "去处理"))
        if role in {"admin", "sales"} and summary["rfqs_needs_info"]:
            todos.append(("待补充询价", f"{summary['rfqs_needs_info']} 份 RFQ 缺少数量/目的地/交期等商务信息", "/admin/rfqs", "去查看"))
        if role in {"admin", "sales"} and summary["leads_overdue"]:
            todos.append(("逾期跟进线索", f"{summary['leads_overdue']} 条线索的跟进日期已过，建议尽快处理", "/admin/sales", "去跟进"))
        if todos:
            head_title, head_meta, head_href, head_action = todos[0]
            focus_html = (
                f"<div class='focus'><div><span class='focus-label'>当前待办 · {esc(head_title)}</span><strong>{esc(head_meta)}</strong>"
                f"<p>处理完这一项后，可继续查看下方其余待办或左侧导航。</p></div>"
                f"<div class='actions'><a class='button' href='{head_href}'>{esc(head_action)} →</a></div></div>"
            )
        else:
            focus_html = (
                "<div class='focus'><div><span class='focus-label'>当前状态</span><strong>没有需要立即处理的事项</strong>"
                "<p>可打开左侧导航或下方常用入口继续查看；自助会话不会仅因产生对话就进入销售队列。</p></div>"
                "<div class='actions'><a class='button secondary' href='/admin/sales'>查看销售工作台</a></div></div>"
            )
        todo_html = "".join(
            f"<div class='simple-item'><div><strong>{esc(title)}</strong><small>{esc(meta)}</small></div>"
            f"<div class='actions'><a class='button secondary' href='{href}'>{esc(action)}</a></div></div>"
            for title, meta, href, action in todos
        ) or "<div class='muted-block'>当前岗位没有待办。待办只显示需要采取行动的事项，一般记录请从左侧导航进入对应模块。</div>"
        quick_links = [
            ("/admin/sales", "打开销售工作台", "查看待跟进线索、负责人和完整上下文"),
            ("/admin/rfqs", "查看 RFQ", "检查买方提交的询价和待补充字段"),
        ]
        if self.user_role in {"admin", "engineer"}:
            quick_links.append(("/admin/tasks?status=pending", "处理人工任务", "回复资料外问题并保留当前会话上下文"))
        if self.user_role == "admin":
            quick_links.append(("/admin/products", "维护产品资料", "核准字段、发布版本或撤回公开页"))
            quick_links.append(("/admin/users", "管理成员账号", "创建销售/工程师账号并控制后台访问"))
            quick_links.append(("/admin/sites", "网站 API 接入", "登记企业官网来源并获取公开的网站标识"))
        quick_html = "".join(
            f"<div class='simple-item'><div><strong>{esc(title)}</strong><small>{esc(description)}</small></div>"
            f"<div class='actions'><a class='button secondary' href='{href}'>进入</a></div></div>"
            for href, title, description in quick_links
        )
        body = f"""
<div class='toolbar'><div><h1>管理概览</h1><div class='muted'>当前企业：示例水泵企业（演示） · 当前账号：{esc(self.actor)}（{role_label}）</div></div><div><form method='post' action='/admin/demo/seed' class='inline'><button class='secondary'>加载演示数据</button></form> <form method='post' action='/admin/lowara/seed' class='inline'><button class='secondary'>加载 Lowara 资料</button></form></div></div>
{demo_notice}
{focus_html}
<div class='grid'><section class='card'><div class='panel-title'><h2>待处理事项</h2><span class='muted'>只显示当前岗位需要行动的事项</span></div><div class='simple-list'>{todo_html}</div></section>
<section class='card'><div class='panel-title'><h2>常用入口</h2></div><div class='simple-list'>{quick_html}</div></section></div>
<section class='card' style='margin-top:18px'><details class='detail-toggle' style='border-top:0;margin-top:0;padding-top:0'><summary>查看运营数字</summary><div><section class='metric-grid'>{cards}</section></div></details></section>
<div class='footer-note'>概览只读取当前登录账号所属企业的数据；具体修改仍需进入对应模块，并按角色权限执行。</div>
"""
        self.send_html(self.console_page("管理概览", body, "admin"), 200)

    def admin_users(self, query: dict[str, list[str]]) -> None:
        role_labels = {"admin": "管理员", "sales": "销售", "engineer": "工程师"}
        users = self.db.list_users(self.tenant_id)
        items = []
        for user in users:
            active = bool(user["active"])
            status = "启用" if active else "已停用"
            action = "停用" if active else "重新启用"
            action_html = (
                "<span class='muted'>当前账号</span>"
                if user["id"] == self.user_id
                else f"<form method='post' action='/admin/users/{esc(user['id'])}/toggle' class='inline'><button class='{'danger' if active else 'secondary'}'>{action}</button></form>"
            )
            items.append(
                f"<div class='simple-item'><div><strong>{esc(user['display_name'])} <span class='badge'>"
                f"{esc(role_labels.get(user['role'], user['role']))}</span> "
                f"<span class='badge {'approved' if active else 'withdrawn'}'>{status}</span></strong>"
                f"<small>{esc(user['email'])} · 创建于 {esc(user['created_at'])}</small></div>"
                f"<div class='actions'>{action_html}</div></div>"
            )
        items_html = "".join(items) or "<div class='muted-block'>暂无成员。</div>"
        notice = {
            "created": "<div class='notice'>成员账号已创建，可以使用新邮箱和密码登录。</div>",
            "updated": "<div class='notice'>成员账号状态已更新；停用账号的现有会话已失效。</div>",
        }.get(query.get("notice", [""])[0], "")
        body = f"""
<div class='toolbar'><div><h1>成员管理</h1><div class='muted'>管理当前企业的后台账号和最小角色权限。</div></div><a class='button secondary' href='/admin'>返回管理概览</a></div>
{notice}
<section class='card'><div class='panel-title'><h2>成员列表</h2><span class='muted'>停用后不能登录，历史记录不受影响</span></div><div class='simple-list'>{items_html}</div>
<details class='detail-toggle'><summary>创建成员账号</summary><div><p class='muted'>密码只在创建时使用，不会在后台列表显示。生产环境应接入正式身份提供商。</p><form method='post' action='/admin/users'><div class='form-grid'><div><label>姓名 *</label><input name='display_name' required placeholder='例如 Alex Chen'></div><div><label>邮箱 *</label><input name='email' type='email' required placeholder='alex@example.com'></div><div><label>角色 *</label><select name='role'><option value='sales'>销售</option><option value='engineer'>工程师</option><option value='admin'>管理员</option></select></div><div><label>初始密码 *</label><input name='password' type='password' minlength='8' required placeholder='至少 8 位'></div></div><p><button>创建账号</button></p></form></div></details>
<details class='detail-toggle'><summary>查看角色说明</summary><div class='muted-block'><p><b>管理员：</b>产品管理、成员管理、人工任务、RFQ 和销售工作台。</p><p><b>销售：</b>RFQ 和销售工作台。</p><p><b>工程师：</b>人工任务、RFQ 和销售工作台。</p><p>账号属于当前企业；停用后不能继续登录，也不会改变历史 RFQ、任务或操作记录。</p></div></details></section>
"""
        self.send_html(self.console_page("成员管理", body, "users"), 200)

    def admin_sites(self, query: dict[str, list[str]]) -> None:
        sites = self.db.list_buyer_sites(self.tenant_id)
        items = "".join(
            f"<div class='simple-item'><div><strong>{esc(site['allowed_origin'])} "
            f"<span class='badge {'approved' if site['active'] else 'withdrawn'}'>{'启用' if site['active'] else '停用'}</span></strong>"
            f"<small>网站标识：<code>{esc(site['site_key'])}</code> · 登记于 {esc(site['created_at'])}</small></div></div>"
            for site in sites
        ) or "<div class='muted-block'>尚未登记官网来源。登记后官网聊天窗口才能与平台建立会话。</div>"
        notice = "<div class='notice'>官网来源已登记。网站标识可用于买方 API；它不是后台密钥。</div>" if query.get("notice", [""])[0] == "created" else ""
        body = f"""
<div class='toolbar'><div><a href='/admin'>← 返回管理概览</a><h1>网站 API 接入</h1><p class='muted'>为企业自有官网聊天窗口登记允许的浏览器来源。会话私有数据仍需要单独的买方访问凭据。</p></div></div>
{notice}
<section class='card'><div class='panel-title'><h2>已登记网站</h2><span class='muted'>只有登记过的来源才能创建买方会话</span></div><div class='simple-list'>{items}</div>
<details class='detail-toggle' open><summary>登记新的官网来源</summary><div><form method='post' action='/admin/sites'><label>官网来源 *</label><input name='allowed_origin' type='url' required placeholder='https://www.example.com'><p class='muted'>只填写协议、域名和可选端口；正式网站使用 HTTPS。本地测试可用 localhost。</p><button>生成网站标识</button></form></div></details>
<details class='detail-toggle'><summary>查看接入说明</summary><div class='muted-block'><p>把网站标识放在创建会话请求的 <code>site_key</code> 字段。后续请求使用 <code>X-Site-Key</code> 和该会话的 <code>Authorization</code> 凭据。</p><p><a href='/reference-client' target='_blank'>打开参考网页聊天窗口</a>。本地演示时，先把此网页的来源登记为允许来源。</p><p>这里是基础登记界面；完整接入文档、密钥轮换和自助安装尚未验收。</p></div></details></section>
"""
        self.send_html(self.console_page("网站 API 接入", body, "sites"), 200)

    def admin_tasks(self, query: dict[str, list[str]]) -> None:
        tasks = self.db.list_human_tasks(self.tenant_id, query.get("status", [""])[0] or None)
        items = "".join(
            f"<div class='simple-item'><div><strong>{esc(task['question'])}</strong>"
            f"<small>{esc(task['model'] or '未指定型号')} · <span class='badge'>{esc(task['status'])}</span> · {esc(task['created_at'])}</small></div>"
            f"<div class='actions'><a class='button{'' if task['status'] == 'pending' else ' secondary'}' href='/admin/tasks/{task['id']}'>{'处理 →' if task['status'] == 'pending' else '查看 →'}</a></div></div>"
            for task in tasks
        ) or "<div class='muted-block'>暂无人工任务。资料外问题或无法确认的问题会出现在这里。</div>"
        body = f"""
<div class='toolbar'><div><h1>人工任务</h1><div class='muted'>只处理当前演示企业的会话问题；工程师回复不会自动写入产品通用知识。</div></div><div><a class='button secondary' href='/admin/tasks?status=pending'>待处理</a> <a class='button secondary' href='/admin/tasks'>全部</a></div></div>
<section class='card'><div class='panel-title'><h2>任务列表</h2><span class='muted'>先处理"待处理"的任务</span></div><div class='simple-list'>{items}</div></section>
"""
        self.send_html(self.console_page("人工任务", body, "tasks"), 200)

    def admin_task(self, task_id: str, query: dict[str, list[str]]) -> None:
        task = self.db.get_human_task(self.tenant_id, task_id)
        if not task:
            self.send_html(self.console_page("任务不存在", "<div class='error'>人工任务不存在或不属于当前企业。</div>", "tasks"), 404)
            return
        context = json.loads(task["context_json"])
        requirements = context.get("confirmed_requirements", {})
        requirement_rows = "".join(f"<tr><td>{esc(item.get('label', key))}</td><td>{esc(item.get('value', ''))} {esc(item.get('unit', ''))}</td><td>{esc(item.get('state', ''))}</td></tr>" for key, item in requirements.items()) or "<tr><td colspan='3' class='muted'>暂无已确认工况</td></tr>"
        notice_kind = query.get("notice", [""])[0]
        notice = "<div class='notice'>人工任务已回复，买方会话中已保存工程师回复。</div>" if notice_kind == "resolved" and task["status"] == "resolved" else "<div class='notice'>追问已保存到买方原会话，等待买方补充。</div>" if notice_kind == "followup" and task["status"] == "waiting_buyer" else ""
        followup_rows = "".join(f"<div class='source'><span><b>{'工程师追问' if message['role'] == 'engineer' else '买方补充'}</b><br>{esc(message['content'])}</span></div>" for message in self.db.list_task_followups(self.tenant_id, task_id))
        if task["response"]:
            response_area = f"<p><b>当前回复：</b>{esc(task['response'])}</p>"
        elif task["status"] == "waiting_buyer":
            response_area = "<p class='muted'>等待买方在原会话补充；收到后可继续答复或再次追问。</p>"
        else:
            response_area = f"<form method='post' action='/admin/tasks/{task_id}/followup'><label>向买方追问工况</label><textarea name='question' required placeholder='仅询问处理此任务所需的条件'></textarea><p><button class='secondary'>发送追问并等待买方</button></p></form><hr><form method='post' action='/admin/tasks/{task_id}/reply'><label>工程师最终答复 *</label><textarea name='response' required placeholder='只回答本次询盘，并说明依据或仍需确认的边界。'></textarea><p><button>提交回复并关闭任务</button></p></form>"
        if task["status"] == "resolved":
            focus_label, focus_title, focus_text = "任务已完成", "人工答复已回到买家的原会话", "如该答复需要成为通用资料，必须回到产品资料核准流程；任务无需重复创建。"
            focus_actions = ""
        elif task["status"] == "waiting_buyer":
            focus_label, focus_title, focus_text = "当前等待 · 买家补充", "追问已发送到原会话，等待买家回复", "买家补充后会继续处理同一个任务。"
            focus_actions = ""
        else:
            focus_label, focus_title, focus_text = "当前任务 · 待答复", "回答买家的实际问题", "先阅读原问题与工况，再决定追问或直接答复；答复会回到买家的原会话。"
            focus_actions = "<a class='button' href='#handle'>去处理 →</a>"
        focus_html = f"<div class='focus'><div><span class='focus-label'>{esc(focus_label)}</span><strong>{esc(focus_title)}</strong><p>{esc(focus_text)}</p></div><div class='actions'>{focus_actions}</div></div>"
        body = f"""
<div class='toolbar'><div><a href='/admin/tasks'>← 返回人工任务</a><h1>人工接管任务</h1><div class='muted'>状态：{esc(task['status'])} · 会话：{esc(task['conversation_id'])} · {esc(task['created_at'])}</div></div></div>
{notice}
{focus_html}
<div class='grid'><section class='card'><h2>买家要解决的问题</h2><div class='quote'>“{esc(task['question'])}”</div><div class='compact-facts'><div><small>型号</small><strong>{esc(task['model'] or '未指定')}</strong></div><div><small>转人工原因</small><strong>{esc(task['reason'])}</strong></div></div><p class='muted'><b>已有回答：</b>{esc(context.get('answer_so_far', ''))}</p><details class='detail-toggle'><summary>查看已确认工况</summary><div><table><thead><tr><th>字段</th><th>值</th><th>状态</th></tr></thead><tbody>{requirement_rows}</tbody></table></div></details></section>
<section class='card' id='handle'><h2>工程师处理</h2>{followup_rows}{response_area}<p class='muted'>该回复只属于当前会话；如果要成为通用资料，必须回到产品资料核准流程。</p></section></div>
"""
        self.send_html(self.console_page("人工接管任务", body, "tasks"), 200)

    def admin_rfqs(self, query: dict[str, list[str]]) -> None:
        rfqs = self.db.list_rfqs(self.tenant_id)
        items = "".join(
            f"<div class='simple-item'><div><strong>{esc(rfq['id'])} · {esc(rfq['model'] or '未指定型号')}</strong>"
            f"<small>{esc(rfq['contact_email'] or '未提供联系人')} · <span class='badge'>{esc(rfq['status'])}</span> · {esc(rfq['created_at'])}</small></div>"
            f"<div class='actions'><a class='button secondary' href='/admin/rfqs/{rfq['id']}'>查看 →</a></div></div>"
            for rfq in rfqs
        ) or "<div class='muted-block'>暂无 RFQ。买方提交询价后会出现在这里。</div>"
        body = f"""
<div class='toolbar'><div><h1>RFQ 询价记录</h1><div class='muted'>买方提交的询价原记录；负责人、阶段与跟进操作在销售工作台处理。</div></div></div>
<section class='card'><div class='panel-title'><h2>询价列表</h2><span class='muted'>按提交时间倒序</span></div><div class='simple-list'>{items}</div></section>
"""
        self.send_html(self.console_page("RFQ 询价记录", body, "rfqs"), 200)

    def admin_rfq(self, rfq_id: str, query: dict[str, list[str]]) -> None:
        rfq = self.db.get_rfq(self.tenant_id, rfq_id)
        if not rfq:
            self.send_html(self.console_page("RFQ 不存在", "<div class='error'>RFQ 不存在或不属于当前企业。</div>", "rfqs"), 404)
            return
        snapshot = json.loads(rfq["snapshot_json"])
        requirements = snapshot.get("requirements", {})
        requirement_rows = "".join(f"<tr><td>{esc(item.get('label', key))}</td><td>{esc(item.get('value', ''))} {esc(item.get('unit', ''))}</td><td>{esc(item.get('state', ''))}</td></tr>" for key, item in requirements.items()) or "<tr><td colspan='3' class='muted'>暂无需求快照</td></tr>"
        missing = json.loads(rfq["missing_json"])
        missing_html = f"<div class='error'>待补充：{esc('、'.join(missing))}</div>" if missing else "<div class='notice'>提交字段完整，仍不等于系统自动承诺可报价。</div>"
        if missing:
            focus_label, focus_title, focus_text = "当前状态 · 待补充", f"这份询价还缺：{'、'.join(missing)}", "买方在原页面补充后会生成新版本；逐次变更见下方记录。"
        else:
            focus_label, focus_title, focus_text = "当前状态 · 字段完整", "提交字段完整，仍不等于自动承诺可报价", "如需调整商务信息，由买家在原页面修改同一张询价。"
        focus_html = f"<div class='focus'><div><span class='focus-label'>{esc(focus_label)}</span><strong>{esc(focus_title)}</strong><p>{esc(focus_text)}</p></div><div class='actions'></div></div>"
        revision_rows = []
        previous_data = None
        diff_labels = (("product_id", "型号"), ("contact_email", "邮箱"), ("contact_phone", "电话"), ("quantity", "数量"), ("destination", "目的地"), ("requested_delivery", "期望交期"), ("notes", "说明"))
        for item in self.db.list_rfq_revisions(self.tenant_id, rfq_id):
            data = json.loads(item["content_json"])
            history_product = self.db.get_product(self.tenant_id, data["product_id"]) if data["product_id"] else None
            if previous_data is None:
                diff_text = "初次提交"
            else:
                pieces = []
                for key, label in diff_labels:
                    old_value = previous_data.get(key)
                    new_value = data.get(key)
                    if key == "product_id":
                        old_product = self.db.get_product(self.tenant_id, old_value) if old_value else None
                        new_product = self.db.get_product(self.tenant_id, new_value) if new_value else None
                        old_value = old_product["model"] if old_product else ""
                        new_value = new_product["model"] if new_product else ""
                    old_text = str(old_value or "").strip()
                    new_text = str(new_value or "").strip()
                    if old_text != new_text:
                        pieces.append(f"{label}：{esc(old_text or '未提供')} → {esc(new_text or '未提供')}")
                diff_text = "；".join(pieces) or "无字段变化"
            revision_rows.append(
                f"<tr><td>v{item['revision']}</td><td>{esc(item['created_at'])}</td><td>{esc(item['actor'])}</td><td>{diff_text}</td>"
                f"<td>{esc(history_product['model'] if history_product else '未指定')}</td><td>{esc(data['contact_email'] or data['contact_phone'] or '未提供')}</td><td>{esc(data['quantity'] or '待补充')}</td>"
                f"<td>{esc(data['destination'] or '待补充')}</td><td>{esc(data['notes'] or '无')}</td></tr>"
            )
            previous_data = data
        revision_rows = "".join(revision_rows)
        body = f"""
<div class='toolbar'><div><a href='/admin/rfqs'>← 返回 RFQ 列表</a><h1>RFQ {esc(rfq['id'])}</h1><div class='muted'>状态：{esc(rfq['status'])} · 会话：{esc(rfq['conversation_id'])} · {esc(rfq['created_at'])}</div></div></div>
{focus_html}
<div class='grid'><section class='card'><h2>联系人（后台可见）</h2><p><b>姓名：</b>{esc(rfq['contact_name']) or '未提供'}</p><p><b>公司：</b>{esc(rfq['company']) or '未提供'}</p><p><b>邮箱：</b>{esc(rfq['contact_email']) or '未提供'}</p><p><b>电话：</b>{esc(rfq['contact_phone']) or '未提供'}</p></section><section class='card'><h2>询价信息</h2><p><b>型号：</b>{esc(rfq['model'] or '未指定')}</p><p><b>数量：</b>{esc(rfq['quantity']) or '待补充'}</p><p><b>目的地：</b>{esc(rfq['destination']) or '待补充'}</p><p><b>期望交期：</b>{esc(rfq['requested_delivery']) or '待补充'}</p><p><b>补充说明：</b>{esc(rfq['notes']) or '无'}</p></section></div>
<section class='card' style='margin-top:18px'><h2>已确认需求快照</h2><table><thead><tr><th>字段</th><th>值</th><th>状态</th></tr></thead><tbody>{requirement_rows}</tbody></table></section>
<section class='card' style='margin-top:18px'><h2>RFQ 提交与修改记录（仅卖方可见）</h2><table><thead><tr><th>版本</th><th>时间</th><th>修改者</th><th>变更内容（相对上一版）</th><th>型号</th><th>联系</th><th>数量</th><th>目的地</th><th>说明</th></tr></thead><tbody>{revision_rows}</tbody></table></section>
<div class='footer-note'>该 RFQ 保存了提交时的需求、推荐和产品版本快照；后续产品更新不会静默改写这条历史询价。</div>
"""
        self.send_html(self.console_page("RFQ 详情", body, "rfqs"), 200)

    def admin_sales(self, query: dict[str, list[str]]) -> None:
        records = self.db.list_sales_records(self.tenant_id)
        today = datetime.now(timezone.utc).date().isoformat()
        stage_labels = {"new": "新线索", "needs_info": "待补充", "technical_review": "技术确认中", "quote_ready": "可进入报价", "paused": "暂缓", "closed": "已关闭"}
        items = []
        for record in records:
            overdue = bool(record["next_follow_up"] and record["next_follow_up"] < today and record["stage"] not in ("closed", "paused") and not record["stop_proactive_contact"])
            follow_up = f"<span class='badge withdrawn'>逾期 {esc(record['next_follow_up'])}</span>" if overdue else esc(record["next_follow_up"] or "未安排")
            if record["stop_proactive_contact"]:
                follow_up = "<span class='badge withdrawn'>停止主动联系</span>"
            changed = (f"<span class='badge pending_review'>买方已修改 {record['buyer_revision_count']} 次</span>" if record["buyer_revision_count"] else "")
            items.append(
                f"<div class='simple-item'><div><strong>{esc(record['id'])} · {esc(record['model'] or '未指定型号')} <span class='badge'>{esc(stage_labels.get(record['stage'], record['stage'] or '未分配'))}</span></strong>"
                f"<small>{esc(record['company'] or record['contact_email'] or '未提供联系人')} · 负责人：{esc(record['owner'] or '未分配')} · 跟进：{follow_up} · RFQ：{esc(record['status'])} {changed}</small></div>"
                f"<div class='actions'><a class='button' href='/admin/sales/{record['id']}'>打开线索 →</a></div></div>"
            )
        items_html = "".join(items) or "<div class='muted-block'>暂无销售线索。买方提交 RFQ 后会进入这里。</div>"
        body = f"""
<div class='toolbar'><div><h1>销售工作台</h1><div class='muted'>统一查看 RFQ、需求、推荐、问答和人工任务；当前企业：示例水泵企业（演示）</div></div><a class='button secondary' href='/admin/rfqs'>RFQ 原始记录</a></div>
<section class='card'><div class='panel-title'><h2>线索列表</h2><span class='muted'>领取或接手的线索从这里打开处理</span></div><div class='simple-list'>{items_html}</div></section>
"""
        self.send_html(self.console_page("销售工作台", body, "sales"), 200)

    def admin_sales_detail(self, rfq_id: str, query: dict[str, list[str]]) -> None:
        rfq = self.db.get_rfq(self.tenant_id, rfq_id)
        lead = self.db.get_lead_for_rfq(self.tenant_id, rfq_id)
        if not rfq or not lead:
            self.send_html(self.console_page("线索不存在", "<div class='error'>线索不存在或不属于当前企业。</div>", "sales"), 404)
            return
        snapshot = json.loads(rfq["snapshot_json"])
        preference = self.db.get_contact_preference(self.tenant_id, rfq["conversation_id"])
        stop_banner = f"<div class='error'><b>停止主动联系</b> · 买方要求：{esc(preference['stop_reason'])} · {esc(preference['stopped_at'])}<br>不得安排主动跟进；买方主动发问仍可回应。</div>" if preference["stop_proactive_contact"] else ""
        detail_revisions = self.db.list_rfq_revisions(self.tenant_id, rfq_id)
        buyer_revision_count = max(len(detail_revisions) - 1, 0)
        change_banner = (
            f"<div class='notice'><b>买方已修改此询价 {buyer_revision_count} 次</b>（最近 {esc(detail_revisions[-1]['created_at'])}）。按最新内容处理即可；"
            f"<a href='/admin/rfqs/{rfq_id}'>查看初次提交与逐次变更对比</a>。</div>"
        ) if buyer_revision_count else ""
        stop_form = f"<form method='post' action='/admin/sales/{rfq_id}/contact-stop'><label>记录买方在其他渠道提出的停止主动联系原话</label><textarea name='reason' required></textarea><button class='danger'>记录并停止主动跟进</button></form>" if not preference["stop_proactive_contact"] and self.user_role in {"sales", "admin"} else ""
        followup_disabled = "disabled" if preference["stop_proactive_contact"] else ""
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
        notice = "<div class='notice'>销售状态已保存，刷新后仍会保留。</div>" if query.get("notice", [""])[0] == "updated" else "<div class='notice'>负责人已更新，同会话的 RFQ 使用同一负责人。</div>" if query.get("notice", [""])[0] == "assigned" else ""
        claim_action = f"<form method='post' action='/admin/sales/{rfq_id}/claim'><button>领取这条会话</button></form>" if not lead["owner"] and self.user_role == "sales" else ""
        sales_options = "".join(f"<option value='{esc(user['email'])}' {'selected' if lead['owner'] == user['email'] else ''}>{esc(user['display_name'])} · {esc(user['email'])}</option>" for user in self.db.list_users(self.tenant_id) if user["role"] == "sales" and user["active"])
        assign_action = f"<form method='post' action='/admin/sales/{rfq_id}/assign'><label>分配给销售</label><select name='owner_email'>{sales_options}</select><button>分配或改派</button></form>" if self.user_role == "admin" and sales_options else ""
        if recommendation:
            recommendation_html = f"<p><b>推荐型号：</b>{esc(recommendation.get('model'))}</p><p><b>产品版本：</b>v{esc(recommendation.get('product_version'))}</p><p><b>满足项：</b>{esc('；'.join(recommendation.get('satisfies', [])) or '—')}</p><p><b>冲突项：</b>{esc('；'.join(recommendation.get('conflicts', [])) or '—')}</p><a href='{esc(recommendation.get('detail_url', '#'))}' target='_blank'>打开产品页</a>"
        else:
            recommendation_html = "<p class='muted'>暂无推荐快照。</p>"
        if preference["stop_proactive_contact"]:
            focus_label, focus_title, focus_text = "注意 · 已停止主动联系", "买方要求不再被主动联系", f"原话：{preference['stop_reason']}；买方主动发问时仍可回应，不得安排主动跟进。"
            focus_actions = ""
        elif not lead["owner"] and self.user_role == "sales":
            focus_label, focus_title, focus_text = "当前任务 · 领取", "先认领这位买家的线索", "同一段咨询由一位销售负责；关联的询价单各自处理。"
            focus_actions = claim_action
        elif not lead["owner"]:
            focus_label, focus_title, focus_text = "当前任务 · 分配", "把这条线索分配给一位销售", "同一段咨询由一位销售负责；关联的询价单各自处理。"
            focus_actions = assign_action
        elif buyer_revision_count:
            focus_label, focus_title, focus_text = "当前任务 · 处理变更", f"买方已修改此询价 {buyer_revision_count} 次", "按最新内容处理即可；逐次变更对比在 RFQ 详情页。"
            focus_actions = f"<a class='button secondary' href='/admin/rfqs/{rfq_id}'>查看变更 →</a>"
        else:
            focus_label, focus_title, focus_text = "当前任务 · 跟进", "处理这份采购请求", "核对阶段与跟进日期；联系买家前注意其主动联系授权。"
            focus_actions = ""
        focus_html = f"<div class='focus'><div><span class='focus-label'>{esc(focus_label)}</span><strong>{esc(focus_title)}</strong><p>{esc(focus_text)}</p></div><div class='actions'>{focus_actions}</div></div>"
        body = f"""
<div class='toolbar'><div><a href='/admin/sales'>← 返回销售工作台</a><h1>销售线索 {esc(rfq['id'])}</h1><div class='muted'>RFQ 状态：{esc(rfq['status'])} · 会话：{esc(rfq['conversation_id'])} · 当前 v{esc(rfq['current_revision'])}</div></div></div>
{notice}
{stop_banner}
{change_banner}
{focus_html}
<div class='grid'><section class='card'><h2>线索处理</h2><p><b>会话负责人：</b>{esc(lead['owner'] or '未领取')}</p>{assign_action if lead['owner'] else ''}<form method='post' action='/admin/sales/{rfq_id}/update'><label>阶段</label><select name='stage'>{stage_options}</select><label>下一步跟进日期</label><input type='date' name='next_follow_up' value='{esc(lead['next_follow_up'] or '')}' {followup_disabled}><label>销售备注</label><textarea name='note' placeholder='记录下一步和客户背景'>{esc(lead['note'])}</textarea><p><button>保存跟进状态</button></p></form></section><section class='card'><h2>联系人与询价</h2><p><b>联系人：</b>{esc(rfq['contact_name']) or '未提供'}</p><p><b>公司：</b>{esc(rfq['company']) or '未提供'}</p><p><b>邮箱：</b>{esc(rfq['contact_email']) or '未提供'}</p><p><b>电话：</b>{esc(rfq['contact_phone']) or '未提供'}</p><p><b>型号：</b>{esc(rfq['model'] or '未指定')}</p><p><b>数量：</b>{esc(rfq['quantity']) or '待补充'} · <b>目的地：</b>{esc(rfq['destination']) or '待补充'} · <b>交期：</b>{esc(rfq['requested_delivery']) or '待补充'}</p><p><b>备注：</b>{esc(rfq['notes']) or '无'}</p><p><a href='/admin/rfqs/{rfq_id}'>查看初次提交与历次修改（当前 v{rfq['current_revision']}）</a></p></section></div>
{stop_form}
<section class='card' style='margin-top:18px'><h2>已确认需求快照</h2><table><thead><tr><th>字段</th><th>值</th><th>状态</th></tr></thead><tbody>{requirement_rows}</tbody></table></section>
<div class='grid' style='margin-top:18px'><section class='card'><h2>推荐快照</h2>{recommendation_html}</section><section class='card'><h2>问答与人工任务</h2>{answer_rows}{task_rows}</section></div>
<details class='detail-toggle' style='margin-top:18px'><summary>查看操作记录</summary><div>{audit_html}</div></details>
"""
        self.send_html(self.console_page("销售线索详情", body, "sales"), 200)

    def public_product(self, slug: str, query: dict[str, list[str]], site: sqlite3.Row | None = None) -> None:
        tenant_id = site["tenant_id"] if site else self.tenant_id
        product = self.db.get_product_by_slug(tenant_id, slug)
        if not product or product["status"] == "withdrawn":
            self.send_html(layout("产品不可用", "<div class='error'>该产品页尚未发布，或已被撤回。</div><p><a href='/admin/products'>返回企业后台</a></p>"), 404)
            return
        version = self.db.get_published_version(tenant_id, product["id"])
        if not version:
            self.send_html(self.console_page("产品不可用", "<div class='error'>找不到已发布版本。</div>", "products"), 404)
            return
        snapshot = json.loads(version["snapshot_json"])
        sources = self.db.get_sources(tenant_id, product["id"])
        allowed_source_ids = self.db.public_downloadable_ids(tenant_id, product["id"])
        db_source_rows = {source["id"]: source for source in sources}
        # The published version freezes the model-file mapping: files approved for
        # public download after this publication only appear after the next release.
        # Live permission revocation still hides a file immediately for safety, and
        # a scope correction invalidates this version's mapping until republish
        # (public_downloadable_ids requires snapshot scope == live scope).
        published_sources = []
        for entry in snapshot.get("public_sources", []):
            row = db_source_rows.get(entry["id"])
            if not row or not row["storage_path"] or entry["id"] not in allowed_source_ids:
                continue
            published_sources.append(entry)
        lang = query.get("lang", ["en"])[0]
        approved_fields = {field["field_key"]: field for field in snapshot["fields"]}
        def t(en: str, zh: str) -> str:
            return en if lang == "en" else zh
        def fact(key: str) -> str:
            field = approved_fields.get(key)
            if not field:
                return f"<span class='page-missing'>{t('Not provided', '资料未提供')}</span>"
            return f"<strong>{esc(field['value'])} {esc(field['unit'])}</strong><small>{esc(field['source_filename'] or '')} · {esc(field['source_location'])}</small>"
        hero_keys = ("flow_range", "head_range", "media", "power", "material", "temperature", "performance_points", "dimension")
        def field_label(key: str) -> str:
            return FIELD_LABELS_EN.get(key, "Installation dimensions") if lang == "en" else FIELD_LABELS.get(key, "安装尺寸")
        hero_facts = "".join(f"<div><b>{esc(field_label(key))}</b>{fact(key)}</div>" for key in hero_keys)
        group_html = "".join(
            f"<section class='page-group'><h3>{t(en, zh)}</h3><dl>" + "".join(f"<div><dt>{esc(field_label(key))}</dt><dd>{fact(key)}</dd></div>" for key in keys) + "</dl></section>"
            for en, zh, keys in PRODUCT_PAGE_GROUPS
        )
        group_html += f"<section class='page-group'><h3>{t('Purchasing and delivery', '采购交付')}</h3><p class='page-missing'>{t('MOQ, lead time and sample availability require seller confirmation.', '起订量、交期和样品状态待卖方确认。')}</p></section>"
        missing_labels = [FIELD_LABELS[key] for key in snapshot.get("missing_fields", []) if key in FIELD_LABELS]
        missing_html = "<p class='muted'>" + ("Pending information: " if lang == "en" else "待补充信息：") + esc("、".join(missing_labels)) + "</p>" if missing_labels else ""
        source_prefix = f"/s/{quote(site['site_key'])}" if site else ""
        def source_url(entry: dict[str, object]) -> str:
            return f"{source_prefix}/products/{esc(product['slug'])}/sources/{esc(entry['id'])}"
        def scope_chip(entry: dict[str, object]) -> str:
            return t('This model', '本型号') if entry["scope"] == "exact_model" else t('Series reference (not model-specific)', '系列资料（非本型号专属）')
        image_entries = [entry for entry in published_sources if Path(str(entry["filename"])).suffix.lower() in IMAGE_EXTENSIONS]
        gallery_entries = [entry for entry in image_entries if entry["scope"] == "exact_model"][:8]
        cad_entries = [entry for entry in published_sources if Path(str(entry["filename"])).suffix.lower() in (CAD_3D_EXTENSIONS | CAD_2D_EXTENSIONS)]
        exact_cad = [entry for entry in cad_entries if entry["scope"] == "exact_model"]
        series_cad = [entry for entry in cad_entries if entry["scope"] == "series"]
        if gallery_entries:
            gallery_rules = "".join(
                f"#glr-{idx}:checked~.gallery-main .gallery-slide[data-index='{idx}']{{display:block}}"
                f"#glr-{idx}:checked~.gallery-thumbs label[for='glr-{idx}']{{outline:2px solid #0c688c}}"
                for idx in range(len(gallery_entries))
            )
            gallery_inputs = "".join(
                f"<input class='gallery-input' type='radio' name='product-gallery' id='glr-{idx}' {'checked' if idx == 0 else ''} aria-label='{t('View approved image', '查看已核准图片')} {idx + 1}'>"
                for idx in range(len(gallery_entries))
            )
            gallery_slides = "".join(
                f"<figure class='gallery-slide' data-index='{idx}'><img src='{source_url(entry)}?preview=1' alt='{esc(product['model'])} · {esc(entry['filename'])}'><figcaption>{esc(entry['filename'])} · {t('this model', '本型号已核准图片')}</figcaption></figure>"
                for idx, entry in enumerate(gallery_entries)
            )
            gallery_thumbs = "".join(
                f"<label for='glr-{idx}'><img src='{source_url(entry)}?preview=1' alt='' loading='lazy'><span>{idx + 1}</span></label>"
                for idx, entry in enumerate(gallery_entries)
            ) if len(gallery_entries) > 1 else ""
            hero_visual = f"<style>.product-gallery .gallery-slide{{display:none}}{gallery_rules}</style><div class='product-gallery'>{gallery_inputs}<div class='gallery-main'>{gallery_slides}</div><div class='gallery-thumbs'>{gallery_thumbs}</div></div>"
        else:
            hero_visual = f"<span>{t('Approved product image pending', '已核准本型号图片待补充')}</span>"
        gallery_ids = {entry["id"] for entry in gallery_entries}
        cad_ids = {entry["id"] for entry in cad_entries}
        doc_entries = [entry for entry in published_sources if entry["id"] not in gallery_ids and entry["id"] not in cad_ids]
        def doc_item(entry: dict[str, object]) -> str:
            suffix = Path(str(entry["filename"])).suffix.lower()
            preview = (
                f" · <a href='{source_url(entry)}?preview=1' target='_blank' rel='noopener'>{t('Preview', '预览')}</a>"
                if suffix in (IMAGE_EXTENSIONS | {".pdf"}) else ""
            )
            return (
                f"<li><b>{esc(entry['filename'])}</b> <span class='file-chip'>{esc(scope_chip(entry))}</span> · {esc(entry['source_location'])}"
                f" <a href='{source_url(entry)}'>{t('Download', '下载')}</a>{preview}</li>"
            )
        source_links = "".join(doc_item(entry) for entry in doc_entries) or f"<li class='muted'>{t('No approved public document', '暂无公开资料附件')}</li>"
        def cad_block(title: str, entries: list[dict[str, object]]) -> str:
            items = "".join(
                f"<li><b>{esc(entry['filename'])}</b> <span class='file-chip'>{'3D' if Path(str(entry['filename'])).suffix.lower() in CAD_3D_EXTENSIONS else '2D'} · {esc(Path(str(entry['filename'])).suffix.lstrip('.').upper())}</span>"
                f" <a href='{source_url(entry)}'>{t('Download approved original', '下载获准原件')}</a>"
                f"<small>{t('This format is not rendered in the browser; open the downloaded original in CAD software.', '该格式不提供浏览器内渲染；请下载后在 CAD 软件中打开原件。')}</small></li>"
                for entry in entries
            )
            return f"<h3>{title}</h3><ul class='file-list'>{items}</ul>"
        if exact_cad or series_cad:
            drawing_parts = []
            if exact_cad:
                drawing_parts.append(cad_block(t('This model’s engineering files', '本型号工程文件'), exact_cad))
            if series_cad:
                drawing_parts.append(
                    f"<p class='page-missing'>{t('The files below belong to the series, not to this model; do not use them as this model’s engineering file.', '下列文件为系列级资料，非本型号专属；不得作为本型号工程依据。')}</p>"
                    + cad_block(t('Series-level files', '系列级参考文件'), series_cad)
                )
            drawings_html = "".join(drawing_parts)
        else:
            drawings_html = f"<p class='page-missing'>{t('No approved public 2D/3D file for this model yet. Ask the seller whether a drawing is available.', '当前版本没有本型号已获准公开的 2D/3D 文件；可向卖方询问。')}</p>"
        drawings_action = f"<a class='button secondary' href='#drawings'>{t('2D/3D files', '图纸与模型')}</a>" if (exact_cad or series_cad) else ""
        comparison_keys = ("flow_range", "head_range", "power", "material", "media")
        comparison_products = [product] + [item for item in self.db.list_products(tenant_id) if item["id"] != product["id"] and item["status"] == "published"][:2]
        comparison_versions = [(item, self.db.get_published_version(tenant_id, item["id"])) for item in comparison_products]
        comparison_versions = [(item, item_version) for item, item_version in comparison_versions if item_version]
        comparison_snapshots = [(item, {field["field_key"]: field for field in json.loads(item_version["snapshot_json"])["fields"]}) for item, item_version in comparison_versions]
        comparison_header = "".join(f"<th>{esc(item['model'])}</th>" for item, _ in comparison_snapshots)
        comparison_rows = "".join(f"<tr><th>{esc(field_label(key))}</th>" + "".join(f"<td>{esc(fields[key]['value'])} {esc(fields[key]['unit'])}</td>" if fields.get(key) else f"<td class='page-missing'>{t('Not provided', '资料未提供')}</td>" for _, fields in comparison_snapshots) + "</tr>" for key in comparison_keys)
        comparison_links = "".join(f"<li><a href='{source_prefix}/products/{esc(item['slug'])}'>{esc(item['model'])}</a></li>" for item, _ in comparison_snapshots)
        title = f"{product['model']} | Industrial Pump"
        inquiry_link = "<a class='button' href='#product-rfq'>Request a quote / 发起询价</a>" if site else "<p class='muted'>Open the seller's website link to request a quote.</p>"
        rfq_html = f"""<section class='card' id='product-rfq' data-site-key='{esc(site['site_key'])}' data-product-id='{esc(product['id'])}' style='margin-top:18px'>
<h2>Request a quotation / 发起询价</h2><p class='muted'>You may send a request at any time. Please confirm an email or phone so the seller can reply.</p>
<form id='product-rfq-form'><div class='form-grid'><label>Email <input name='email' type='email'></label><label>Phone <input name='phone' type='tel'></label><label>Quantity <input name='quantity'></label><label>Destination <input name='destination'></label><label class='full'>Requirements <textarea name='notes'></textarea></label></div><label><input name='contact_confirmed' type='checkbox' style='width:auto'> I confirm this reply contact may be used for this request.</label><button type='submit'>Submit RFQ</button></form><p id='product-rfq-result' role='status'></p></section>
<script src='/reference-client/product-page.js' defer></script>""" if site else ""
        public_path = f"/s/{quote(site['site_key'])}/products/{esc(product['slug'])}" if site else f"/products/{esc(product['slug'])}"
        mobile_cta_html = f"<a class='page-mobile-cta' href='#product-rfq'>{t('Request a quote', '发起询价')}</a>" if site else ""
        body = f"""
<nav class='page-breadcrumb' aria-label='Breadcrumb'>{t('Home › Products › Pump', '首页 › 产品 › 水泵')} › {esc(product['model'])}</nav>
<section class='page-hero' id='overview' data-section='hero'><div class='page-visual' role='img' aria-label='{t('Approved product images', '已核准产品图片') if gallery_entries else t('Product image pending approval', '产品图片待核准')}'>{hero_visual}</div><div><p class='page-eyebrow'>{t('Published product version', '已发布产品版本')} {version['version']} · {esc(version['published_at'])}</p><h1>{esc(product['model'])}</h1><p class='page-model'>{esc(product['name'])}</p><p class='muted'>{t('Check the sourced flow and head data before asking for a final duty-point review.', '请先核对有出处的流量与扬程，再请工程师确认最终工况。')}</p><div class='page-actions'>{inquiry_link}<a class='button secondary' href='#downloads'>{t('View documents', '查看资料')}</a>{drawings_action}<a class='button secondary' href='#faq'>{t('Read FAQ', '查看常见问题')}</a></div><div class='page-quick'>{hero_facts}</div>{missing_html}<p class='page-missing'>{t('MOQ, lead time and sample availability: ask the seller.', '起订量、交期、样品状态：请向卖方核实。')}</p></div></section>
<nav class='page-anchors' aria-label='Product sections'><a href='#overview'>{t('Overview', '概览')}</a><a href='#specifications'>{t('Specifications', '参数')}</a><a href='#applications'>{t('Applications', '应用')}</a><a href='#customization'>{t('Customization', '定制')}</a><a href='#downloads'>{t('Documents', '资料')}</a><a href='#faq'>FAQ</a></nav>
<section class='page-section' data-section='advantages'><h2>{t('What the approved record supports', '已核准资料能说明什么')}</h2><p>{t('Flow and head values below are shown with their source. Final suitability depends on the buyer’s operating point and conditions.', '下列流量、扬程值附有出处；最终适用性仍取决于买方工况。')}</p><div class='page-fact-pair'><div><b>{esc(field_label('flow_range'))}</b>{fact('flow_range')}</div><div><b>{esc(field_label('head_range'))}</b>{fact('head_range')}</div></div></section>
<section class='page-section' id='specifications' data-section='specifications'><h2>{t('Technical specifications', '技术参数')}</h2><div class='page-groups'>{group_html}</div></section>
<section class='page-section' id='drawings' data-section='drawings'><h2>{t('2D drawings and 3D models', '二维图纸与三维模型')}</h2><p class='muted'>{t('Only approved originals are offered here. STEP/DXF files are not rendered in the browser; download and open them in CAD software.', '这里只提供已核准原件；STEP/DXF 等不在浏览器内渲染，请下载后用 CAD 软件打开。')}</p>{drawings_html}</section>
<section class='page-section' data-section='comparison'><h2>{t('Compare published models', '对比已发布型号')}</h2><p class='muted'>{t('Only approved published models from this seller appear here. Missing data stays visible.', '仅比较本企业已核准发布的型号；缺项如实显示。')}</p><div class='page-table-scroll'><table><thead><tr><th>{t('Parameter', '参数')}</th>{comparison_header}</tr></thead><tbody>{comparison_rows}</tbody></table></div><ul>{comparison_links}</ul></section>
<section class='page-section' id='applications' data-section='applications'><h2>{t('Applications', '应用场景')}</h2><p class='page-missing'>{t('No application claim has been approved for this product version. Describe your operating conditions in the original website chat or RFQ.', '当前版本没有已核准的应用场景结论。请在原官网聊天窗口或询价中描述工况。')}</p></section>
<section class='page-section' id='customization' data-section='customization'><h2>{t('Customization', '定制能力')}</h2><p class='page-missing'>{t('Available changes and turnaround times have not been confirmed by the seller.', '可定制项目及周期尚未由卖方确认。')}</p></section>
<section class='page-section' data-section='manufacturing'><h2>{t('Manufacturing and quality control', '制造与质量控制')}</h2><p class='page-missing'>{t('Factory and inspection evidence has not been provided for publication.', '尚无获准公开的工厂与质检证据。')}</p></section>
<section class='page-section' data-section='certification'><h2>{t('Certifications and cases', '认证与案例')}</h2><p class='page-missing'>{t('No verified certificates or customer cases are available in this published record.', '当前已发布记录没有可核验的认证或客户案例。')}</p></section>
<section class='page-section' id='downloads' data-section='downloads'><h2>{t('Approved documents', '已获准公开的资料')}</h2><ul class='file-list'>{source_links}</ul><p class='muted'>{t('Documents without publication permission, outside the current version, or revoked are not downloadable.', '未获准公开、未纳入当前版本或已撤销许可的文件不提供下载。')}</p></section>
<section class='page-section' id='faq' data-section='faq'><h2>{t('Frequently asked questions', '常见问题')}</h2><details><summary>{t('Can this model meet my exact duty point?', '这个型号能满足我的具体工况吗？')}</summary><p>{t('Compare the sourced flow and head information above, then ask an engineer to check your actual conditions.', '先核对上方有出处的流量、扬程数据，再请工程师检查实际工况。')}</p></details><details><summary>{t('What are the MOQ and lead time?', '起订量和交期是多少？')}</summary><p>{t('The published record does not confirm these commercial terms. Include your quantity and destination in an RFQ.', '当前已发布资料没有确认这些商务条件。请在询价中提供数量和目的地。')}</p></details></section>
{rfq_html}
{mobile_cta_html}
<footer class='page-footer'>{t('Initial technical display; not a quotation or final engineering approval.', '初步技术展示；并非报价或最终工程适配结论。')} · <a href='{public_path}?lang={'zh' if lang == 'en' else 'en'}'>{'切换中文' if lang == 'en' else 'Switch to English'}</a></footer>
"""
        page = layout(title, body, lang=lang, public=True)
        json_ld = json.dumps({'@context': 'https://schema.org', '@type': 'Product', 'name': product['name'], 'model': product['model']}, ensure_ascii=False).replace("</", "<\\/")
        page = page.replace("</head>", f"<meta name='description' content='{esc(product['name'])} - approved industrial pump specifications'><script type='application/ld+json'>{json_ld}</script><link rel='stylesheet' href='/reference-client/product-page.css'></head>")
        self.send_html(page, 200)

    def public_source(self, slug: str, source_id: str, site: sqlite3.Row | None = None, preview: bool = False) -> None:
        tenant_id = site["tenant_id"] if site else self.tenant_id
        product = self.db.get_product_by_slug(tenant_id, slug)
        if not product or product["status"] == "withdrawn" or not self.db.get_published_version(tenant_id, product["id"]):
            self.send_html(layout("资料不可用", "<div class='error'>产品未公开。</div>", public=True), 404)
            return
        source = self.db.conn.execute(
            "SELECT * FROM source_documents WHERE tenant_id=? AND product_id=? AND id=?",
            (tenant_id, product["id"], source_id),
        ).fetchone()
        # 许可、快照收录与当前归属三者一致才可服务；归属修正会立即撤销许可，
        # 旧版本在重新核准并发布新版本之前不再出现该文件（见 set_source_scope）。
        if not source or not source["storage_path"] or source_id not in self.db.public_downloadable_ids(tenant_id, product["id"]):
            self.send_html(layout("资料不可用", "<div class='error'>该来源资料没有可下载文件、未纳入当前公开版本，或归属已修正待重新发布。</div>"), 404)
            return
        target = ROOT / source["storage_path"]
        if not target.is_file():
            self.send_html(layout("资料不可用", "<div class='error'>文件存储不可用，请联系管理员。</div>", public=True), 404)
            return
        content = target.read_bytes()
        if source["sha256"] and hashlib.sha256(content).hexdigest() != source["sha256"]:
            self.db._audit(tenant_id, "source_document", source_id, "public_download_blocked_hash_mismatch", "public")
            self.db.conn.commit()
            self.send_html(layout("资料不可用", "<div class='error'>文件完整性校验未通过，已阻止下载；请联系管理员重新上传该资料。</div>", public=True), 409)
            return
        content_type = mimetypes.guess_type(source["filename"])[0] or "application/octet-stream"
        previewable = content_type in {"image/png", "image/jpeg", "image/webp", "image/avif", "application/pdf"}
        self.send_bytes(content, content_type, source["filename"], inline=preview and previewable)


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
