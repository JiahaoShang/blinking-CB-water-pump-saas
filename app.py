#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Small, dependency-free MVP application for product approval and publishing.

The first slice deliberately keeps the business modules in one process while
persisting all state in SQLite. It is intended for local development and demo
data, not production authentication or file storage.
"""

from __future__ import annotations

import argparse
import base64
import csv
import hashlib
import html
import io
import json
import mimetypes
import os
import re
import secrets
import sqlite3
import threading
import time
import hmac
from datetime import datetime, timedelta, timezone
from email.parser import BytesParser
from email.policy import default
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlencode, urlparse
from urllib import error as urllib_error
from urllib import request as urllib_request

import ai_client
from ai_client import AIConfig, AIConfigurationError, AIService, AIUnavailableError


ROOT = Path(__file__).resolve().parent
DEFAULT_DB = ROOT / "data" / "app.db"
UPLOAD_DIR = ROOT / "data" / "uploads"
DEMO_SEED_FILE = ROOT / "fixtures" / "demo_seed.json"
LOWARA_SEED_FILE = ROOT / "fixtures" / "lowara_demo_seed.json"
REQUIRED_FIELDS = ("flow_range", "head_range", "media", "material")
MATCH_REQUIRED_FIELDS = ("flow", "head", "media")
# AI 调用日志里的任务名（与 ai_client 内部任务无关，用于管理概览展示与排错）。
AI_TASK_EXTRACTION = "requirement_extraction"
AI_TASK_ANSWER = "answer_field_selection"
# 商业承诺、认证与交期类问题在任何模式下都不进入模型问答，直接转为待核实记录。
COMMERCIAL_QUESTION_TOKENS = (
    "price", "cost", "quote", "价格", "报价", "库存", "stock", "delivery",
    "lead time", "shipping", "交期", "交货", "到货", "认证", "certificate", "certification",
)
COMMERCIAL_REFUSAL_ANSWER = "关于价格、库存、交期或认证，我手上的核准资料还无法确认。"
NO_PUBLISHED_PRODUCT_ANSWER = "目前没有可引用的已发布产品资料，我还不能确认这个问题。"
NO_APPROVED_BASIS_ANSWER = "现有核准资料没有足够依据回答这个问题，参数还需要核实。"
AI_PAUSED_ANSWER = "本会话的 AI 自动回复已被卖家暂停；你的问题已保存，稍后由人工核实后回复。"
HUMAN_REQUEST_ACK = "已转交工程师核实——答复会直接出现在这里。"
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
# V1-02 附件三级可见性：「可公开」＝归属已确认并通过公开下载许可（进入公开版本）；默认「仅内部」不上公开页；「禁止使用」的文件不得进入新买方页面、新回答引用与公开下载。
SOURCE_USAGE_LEVELS = {"internal": "仅内部", "restricted": "禁止使用"}
# V1-02 品类核准：品类候选可由资料/模型/人工提出，必须以授权成员核准为准；
# 未核准的候选不进入买方页、不用于任何按品类派生行为（get_approved_category 只返回已核准值）。
CATEGORY_SOURCE_OPTIONS = (("doc", "资料建议"), ("model", "模型建议"), ("manual", "人工录入"))
CATEGORY_SOURCE_LABELS = dict(CATEGORY_SOURCE_OPTIONS)
CATEGORY_MAX_LENGTH = 40
# V1-02 批量上传：一次请求最多登记的文件数（防止误选整个目录）。
MAX_BATCH_SOURCE_FILES = 20
# V1-08 待培育线索：未形成 RFQ 的手动登记线索（线下/展会等）；来源渠道口径固定为下列选项。
NURTURE_SOURCE_OPTIONS = (
    ("expo", "展会"),
    ("visit", "线下拜访"),
    ("phone", "电话"),
    ("email", "邮件"),
    ("referral", "转介绍"),
    ("other", "其他"),
)
NURTURE_SOURCE_LABELS = dict(NURTURE_SOURCE_OPTIONS)
# V1-08 联系同意来源（记录买方/销售在何处确认可按该联系方式联系）。
CONTACT_CONSENT_SOURCE_LABELS = {
    "chat": "官网聊天窗",
    "product_page": "产品详情页",
    "inquiry": "旧询价页",
    "telegram_link": "Telegram 询价链接",
    "whatsapp_link": "WhatsApp 询价链接",
    "langbot_link": "LangBot 询价链接",
    "offline": "线下登记（销售已确认）",
}
# 跨站会话交接（一次性交接码）：官网聊天（来源 A）与平台产品页（来源 B）不同来源时，
# 由持有会话凭据的聊天方出具短时、一次性、绑定站点与型号的交接码；产品页在平台自身
# 来源上用它换取同一会话的新凭据。码与凭据都只存摘要（SHA-256）；长期会话凭据绝不
# 进入 URL、日志、页面 HTML 或 Referer。
BUYER_HANDOFF_CODE_TTL_SECONDS = 600  # 交接码有效期：10 分钟
BUYER_HANDOFF_CODE_KEEP = 40  # 每会话保留的最近交接码行数（含已用/过期，防无上限增长）
BUYER_HANDOFF_TOKEN_KEEP = 20  # 每会话经交接换取、仍有效的凭据上限（原始凭据不受影响）
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
            CREATE TABLE IF NOT EXISTS user_modules (
                user_id TEXT NOT NULL REFERENCES users(id),
                module TEXT NOT NULL,
                granted_by TEXT NOT NULL DEFAULT '',
                granted_at TEXT NOT NULL,
                PRIMARY KEY (user_id, module)
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
                category TEXT NOT NULL DEFAULT '',
                category_candidate TEXT NOT NULL DEFAULT '',
                category_candidate_source TEXT NOT NULL DEFAULT '',
                category_candidate_at TEXT,
                category_approved_by TEXT NOT NULL DEFAULT '',
                category_approved_at TEXT,
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
                usage TEXT NOT NULL DEFAULT 'internal',
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
            CREATE INDEX IF NOT EXISTS idx_audit_tenant ON audit_log(tenant_id, id);
            CREATE TABLE IF NOT EXISTS tenant_settings (
                tenant_id TEXT NOT NULL REFERENCES tenants(id),
                key TEXT NOT NULL,
                value_json TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (tenant_id, key)
            );
            CREATE TABLE IF NOT EXISTS templates (
                id TEXT PRIMARY KEY,
                tenant_id TEXT NOT NULL REFERENCES tenants(id),
                category TEXT NOT NULL DEFAULT '',
                name TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'draft',
                draft_json TEXT NOT NULL DEFAULT '{}',
                draft_updated_by TEXT NOT NULL DEFAULT '',
                draft_updated_at TEXT,
                published_version INTEGER NOT NULL DEFAULT 0,
                created_by TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE (tenant_id, category)
            );
            CREATE TABLE IF NOT EXISTS template_versions (
                id TEXT PRIMARY KEY,
                tenant_id TEXT NOT NULL REFERENCES tenants(id),
                template_id TEXT NOT NULL REFERENCES templates(id),
                version INTEGER NOT NULL,
                content_json TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'published',
                approved_by TEXT NOT NULL DEFAULT '',
                approved_at TEXT,
                created_by TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                UNIQUE (template_id, version)
            );
            CREATE TABLE IF NOT EXISTS product_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                tenant_id TEXT NOT NULL REFERENCES tenants(id),
                product_id TEXT NOT NULL,
                event TEXT NOT NULL,
                site_key TEXT NOT NULL DEFAULT '',
                conversation_id TEXT,
                detail TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_product_events_tenant ON product_events(tenant_id, created_at);
            CREATE TABLE IF NOT EXISTS material_tasks (
                id TEXT PRIMARY KEY,
                tenant_id TEXT NOT NULL REFERENCES tenants(id),
                product_id TEXT NOT NULL,
                title TEXT NOT NULL,
                source_question TEXT NOT NULL DEFAULT '',
                source_ref TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'open',
                assignee TEXT NOT NULL DEFAULT '',
                created_by TEXT NOT NULL,
                created_at TEXT NOT NULL,
                completed_by TEXT NOT NULL DEFAULT '',
                completed_at TEXT
            );
            CREATE TABLE IF NOT EXISTS lead_milestones (
                tenant_id TEXT NOT NULL REFERENCES tenants(id),
                lead_id TEXT NOT NULL REFERENCES leads(id),
                key TEXT NOT NULL,
                done_by TEXT NOT NULL DEFAULT '',
                done_at TEXT NOT NULL DEFAULT '',
                note TEXT NOT NULL DEFAULT '',
                PRIMARY KEY (tenant_id, lead_id, key)
            );
            CREATE TABLE IF NOT EXISTS lead_replies (
                id TEXT PRIMARY KEY,
                tenant_id TEXT NOT NULL REFERENCES tenants(id),
                lead_id TEXT NOT NULL REFERENCES leads(id),
                body TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'draft',
                channel TEXT NOT NULL DEFAULT '',
                message_id TEXT NOT NULL DEFAULT '',
                created_by TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_by TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL,
                sent_by TEXT NOT NULL DEFAULT '',
                sent_at TEXT
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
            CREATE TABLE IF NOT EXISTS buyer_conversation_tokens (
                id TEXT PRIMARY KEY,
                conversation_id TEXT NOT NULL REFERENCES conversations(id),
                tenant_id TEXT NOT NULL REFERENCES tenants(id),
                token_hash TEXT NOT NULL,
                source TEXT NOT NULL DEFAULT 'handoff',
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_buyer_tokens_conversation ON buyer_conversation_tokens(tenant_id, conversation_id);
            CREATE TABLE IF NOT EXISTS buyer_handoff_codes (
                code_hash TEXT PRIMARY KEY,
                tenant_id TEXT NOT NULL REFERENCES tenants(id),
                site_key TEXT NOT NULL,
                conversation_id TEXT NOT NULL REFERENCES conversations(id),
                product_id TEXT NOT NULL REFERENCES products(id),
                created_at TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                used_at TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_handoff_codes_conversation ON buyer_handoff_codes(conversation_id);
            CREATE TABLE IF NOT EXISTS channel_accounts (
                id TEXT PRIMARY KEY,
                tenant_id TEXT NOT NULL REFERENCES tenants(id),
                channel TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'disabled',
                config_json TEXT NOT NULL DEFAULT '{}',
                created_by TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_by TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL,
                UNIQUE (tenant_id, channel)
            );
            CREATE TABLE IF NOT EXISTS channel_identities (
                id TEXT PRIMARY KEY,
                tenant_id TEXT NOT NULL REFERENCES tenants(id),
                channel TEXT NOT NULL,
                external_user_id TEXT NOT NULL,
                display_name TEXT NOT NULL DEFAULT '',
                conversation_id TEXT NOT NULL REFERENCES conversations(id),
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE (tenant_id, channel, external_user_id)
            );
            CREATE INDEX IF NOT EXISTS idx_channel_identities_conversation ON channel_identities(tenant_id, conversation_id);
            CREATE TABLE IF NOT EXISTS rfq_form_tokens (
                id TEXT PRIMARY KEY,
                tenant_id TEXT NOT NULL REFERENCES tenants(id),
                conversation_id TEXT NOT NULL REFERENCES conversations(id),
                token_hash TEXT NOT NULL,
                created_by TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                revoked_at TEXT
            );
            CREATE UNIQUE INDEX IF NOT EXISTS idx_rfq_form_tokens_hash ON rfq_form_tokens(token_hash);
            CREATE INDEX IF NOT EXISTS idx_rfq_form_tokens_conversation ON rfq_form_tokens(tenant_id, conversation_id);
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
                delivery_attempts INTEGER NOT NULL DEFAULT 0,
                delivery_updated_at TEXT,
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
                requested_model TEXT NOT NULL DEFAULT '',
                contact_consent_source TEXT NOT NULL DEFAULT '',
                contact_consent_at TEXT,
                entry_surface TEXT NOT NULL DEFAULT '',
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
                rfq_id TEXT REFERENCES rfqs(id),
                conversation_id TEXT REFERENCES conversations(id),
                kind TEXT NOT NULL DEFAULT 'rfq',
                source TEXT NOT NULL DEFAULT '',
                contact_name TEXT NOT NULL DEFAULT '',
                company TEXT NOT NULL DEFAULT '',
                contact_email TEXT NOT NULL DEFAULT '',
                contact_phone TEXT NOT NULL DEFAULT '',
                stage TEXT NOT NULL DEFAULT 'new',
                owner TEXT,
                next_follow_up TEXT,
                note TEXT NOT NULL DEFAULT '',
                close_reason TEXT NOT NULL DEFAULT '',
                closed_at TEXT,
                merged_into TEXT,
                merged_at TEXT,
                consent_source TEXT NOT NULL DEFAULT '',
                consent_at TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE (tenant_id, rfq_id)
            );
            CREATE TABLE IF NOT EXISTS ai_call_log (
                id TEXT PRIMARY KEY,
                tenant_id TEXT NOT NULL,
                conversation_id TEXT,
                task TEXT NOT NULL,
                provider TEXT NOT NULL,
                model TEXT NOT NULL,
                status TEXT NOT NULL,
                action TEXT NOT NULL DEFAULT 'none',
                http_status INTEGER,
                duration_ms INTEGER,
                prompt_tokens INTEGER,
                completion_tokens INTEGER,
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_ai_call_log_tenant ON ai_call_log(tenant_id, created_at);
            """
        )
        if "request_hash" not in {row["name"] for row in self.conn.execute("PRAGMA table_info(rfqs)")}:
            self.conn.execute("ALTER TABLE rfqs ADD COLUMN request_hash TEXT")
        if "current_revision" not in {row["name"] for row in self.conn.execute("PRAGMA table_info(rfqs)")}:
            self.conn.execute("ALTER TABLE rfqs ADD COLUMN current_revision INTEGER NOT NULL DEFAULT 1")
        if "requested_model" not in {row["name"] for row in self.conn.execute("PRAGMA table_info(rfqs)")}:
            self.conn.execute("ALTER TABLE rfqs ADD COLUMN requested_model TEXT NOT NULL DEFAULT ''")
        if "contact_consent_source" not in {row["name"] for row in self.conn.execute("PRAGMA table_info(rfqs)")}:
            self.conn.execute("ALTER TABLE rfqs ADD COLUMN contact_consent_source TEXT NOT NULL DEFAULT ''")
        if "contact_consent_at" not in {row["name"] for row in self.conn.execute("PRAGMA table_info(rfqs)")}:
            self.conn.execute("ALTER TABLE rfqs ADD COLUMN contact_consent_at TEXT")
        if "entry_surface" not in {row["name"] for row in self.conn.execute("PRAGMA table_info(rfqs)")}:
            self.conn.execute("ALTER TABLE rfqs ADD COLUMN entry_surface TEXT NOT NULL DEFAULT ''")
        # 旧库回填：入口信息此前只存在 snapshot_json 里；逐行回填一次（幂等）。
        for row in self.conn.execute("SELECT id, snapshot_json FROM rfqs WHERE entry_surface=''").fetchall():
            try:
                entry = str(json.loads(row["snapshot_json"] or "{}").get("entry_surface") or "")
            except json.JSONDecodeError:
                entry = ""
            if entry:
                self.conn.execute("UPDATE rfqs SET entry_surface=? WHERE id=?", (entry, row["id"]))
        self.conn.commit()
        lead_columns = {row["name"] for row in self.conn.execute("PRAGMA table_info(leads)")}
        for column, definition in (
            ("close_reason", "TEXT NOT NULL DEFAULT ''"),
            ("closed_at", "TEXT"),
            ("consent_source", "TEXT NOT NULL DEFAULT ''"),
            ("consent_at", "TEXT"),
        ):
            if column not in lead_columns:
                self.conn.execute(f"ALTER TABLE leads ADD COLUMN {column} {definition}")
        if "kind" not in {row["name"] for row in self.conn.execute("PRAGMA table_info(leads)")}:
            # V1-08：待培育线索（无 RFQ）要求 rfq_id/conversation_id 可空，并新增 kind/来源/联系/合并列。
            # 采用「新建→拷贝→删旧→改名」顺序重建（标准做法，避免 RENAME 反向改写其它表的引用）。
            # FK ON 时 DROP 父表会隐式 DELETE 并触发 lead_milestones/lead_replies 的约束，须临时关闭外键。
            self.conn.commit()
            self.conn.execute("PRAGMA foreign_keys = OFF")
            self.conn.execute(
                """CREATE TABLE leads_migrate_v2 (
                    id TEXT PRIMARY KEY,
                    tenant_id TEXT NOT NULL REFERENCES tenants(id),
                    rfq_id TEXT REFERENCES rfqs(id),
                    conversation_id TEXT REFERENCES conversations(id),
                    kind TEXT NOT NULL DEFAULT 'rfq',
                    source TEXT NOT NULL DEFAULT '',
                    contact_name TEXT NOT NULL DEFAULT '',
                    company TEXT NOT NULL DEFAULT '',
                    contact_email TEXT NOT NULL DEFAULT '',
                    contact_phone TEXT NOT NULL DEFAULT '',
                    stage TEXT NOT NULL DEFAULT 'new',
                    owner TEXT,
                    next_follow_up TEXT,
                    note TEXT NOT NULL DEFAULT '',
                    close_reason TEXT NOT NULL DEFAULT '',
                    closed_at TEXT,
                    merged_into TEXT,
                    merged_at TEXT,
                    consent_source TEXT NOT NULL DEFAULT '',
                    consent_at TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE (tenant_id, rfq_id)
                )"""
            )
            self.conn.execute(
                "INSERT INTO leads_migrate_v2 (id, tenant_id, rfq_id, conversation_id, kind, stage, owner, next_follow_up, note, close_reason, closed_at, created_at, updated_at) "
                "SELECT id, tenant_id, rfq_id, conversation_id, 'rfq', stage, owner, next_follow_up, note, close_reason, closed_at, created_at, updated_at FROM leads"
            )
            self.conn.execute("DROP TABLE leads")
            self.conn.execute("ALTER TABLE leads_migrate_v2 RENAME TO leads")
            # PRAGMA 在事务中会被静默忽略：先 commit 关掉隐式事务，再恢复外键开关。
            self.conn.commit()
            self.conn.execute("PRAGMA foreign_keys = ON")
            self.conn.commit()
        product_columns = {row["name"] for row in self.conn.execute("PRAGMA table_info(products)")}
        for column, definition in (
            ("category", "TEXT NOT NULL DEFAULT ''"),
            ("category_candidate", "TEXT NOT NULL DEFAULT ''"),
            ("category_candidate_source", "TEXT NOT NULL DEFAULT ''"),
            ("category_candidate_at", "TEXT"),
            ("category_approved_by", "TEXT NOT NULL DEFAULT ''"),
            ("category_approved_at", "TEXT"),
        ):
            if column not in product_columns:
                self.conn.execute(f"ALTER TABLE products ADD COLUMN {column} {definition}")
        source_columns = {row["name"] for row in self.conn.execute("PRAGMA table_info(source_documents)")}
        for column, definition in (
            ("scope", "TEXT NOT NULL DEFAULT 'unknown'"),
            ("scope_revision", "INTEGER NOT NULL DEFAULT 0"),
            ("usage", "TEXT NOT NULL DEFAULT 'internal'"),
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
            ("channel", "TEXT NOT NULL DEFAULT ''"),
            ("external_message_id", "TEXT"),
            ("delivery_status", "TEXT NOT NULL DEFAULT ''"),
            ("delivery_error", "TEXT NOT NULL DEFAULT ''"),
            ("delivery_attempts", "INTEGER NOT NULL DEFAULT 0"),
            ("delivery_updated_at", "TEXT"),
            ("actor_email", "TEXT NOT NULL DEFAULT ''"),
        ):
            if column not in message_columns:
                self.conn.execute(f"ALTER TABLE messages ADD COLUMN {column} {definition}")
        # 渠道消息去重：同一渠道同一条外部消息只落一次（列建成后再建索引，兼容旧库迁移顺序）。
        self.conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_messages_external ON messages(tenant_id, channel, external_message_id) WHERE external_message_id IS NOT NULL"
        )
        lead_reply_columns = {row["name"] for row in self.conn.execute("PRAGMA table_info(lead_replies)")}
        if "message_id" not in lead_reply_columns:
            self.conn.execute("ALTER TABLE lead_replies ADD COLUMN message_id TEXT NOT NULL DEFAULT ''")
        conversation_columns = {row["name"] for row in self.conn.execute("PRAGMA table_info(conversations)")}
        if "ai_paused" not in conversation_columns:
            self.conn.execute("ALTER TABLE conversations ADD COLUMN ai_paused INTEGER NOT NULL DEFAULT 0")
        identity_columns = {row["name"] for row in self.conn.execute("PRAGMA table_info(channel_identities)")}
        if "platform_meta" not in identity_columns:
            # LangBot 接入：记录消息来自哪个社交平台（adapter）与机器人标识，供收件箱按平台展示。
            self.conn.execute("ALTER TABLE channel_identities ADD COLUMN platform_meta TEXT NOT NULL DEFAULT ''")
        # 第二波（Dify）：知识库同步台账（产品 → 文档映射 + 内容哈希；幂等同步与失败重试的依据）。
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS kb_sync_state (tenant_id TEXT NOT NULL, product_id TEXT NOT NULL, "
            "document_id TEXT NOT NULL DEFAULT '', content_hash TEXT NOT NULL DEFAULT '', "
            "synced_at TEXT NOT NULL DEFAULT '', last_error TEXT NOT NULL DEFAULT '', "
            "PRIMARY KEY (tenant_id, product_id))"
        )
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
        # V1-01 账号模型迁移：旧角色 sales/engineer → 员工 + 对应模块授权（能力与迁移前一致；幂等）。
        for legacy in self.conn.execute("SELECT id, role FROM users WHERE role IN ('sales','engineer')").fetchall():
            self.conn.execute("UPDATE users SET role='employee' WHERE id=?", (legacy["id"],))
            for module_key in LEGACY_ROLE_MODULES.get(legacy["role"], ()):
                self.conn.execute(
                    "INSERT OR IGNORE INTO user_modules(user_id, module, granted_by, granted_at) VALUES (?, ?, 'migration', ?)",
                    (legacy["id"], module_key, now_iso()),
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
            ("demo-admin@example.invalid", "演示管理员", "admin", "demo-admin", ()),
            ("demo-sales@example.invalid", "演示员工（询价与销售）", "employee", "demo-sales", ("rfqs", "sales")),
            ("demo-engineer@example.invalid", "演示员工（工程任务）", "employee", "demo-engineer", ("tasks", "rfqs", "sales")),
        ]
        for email, display_name, role, password, modules in users:
            self.conn.execute(
                "INSERT OR IGNORE INTO users(id, tenant_id, email, display_name, role, password_hash, active, created_at) VALUES (?, 'demo-tenant', ?, ?, ?, ?, 1, ?)",
                (secrets.token_hex(12), email, display_name, role, self.password_hash(password), now_iso()),
            )
            if modules:
                user_row = self.conn.execute("SELECT id FROM users WHERE email=?", (email,)).fetchone()
                for module_key in modules:
                    self.conn.execute(
                        "INSERT OR IGNORE INTO user_modules(user_id, module, granted_by, granted_at) VALUES (?, ?, 'demo-seed', ?)",
                        (user_row["id"], module_key, now_iso()),
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
        role = role.strip().lower()
        grant_modules: tuple[str, ...] = ()
        if role in LEGACY_ROLE_MODULES:
            # 旧角色名兼容（旧库迁移路径与既有测试）：等价于"员工 + 对应模块授权"
            grant_modules = LEGACY_ROLE_MODULES[role]
            role = "employee"
        if not email or "@" not in email:
            raise ValueError("请输入有效邮箱")
        if not display_name:
            raise ValueError("姓名不能为空")
        if role not in {"admin", "employee"}:
            raise ValueError("不支持的账号类型")
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
        for module_key in grant_modules:
            self.conn.execute(
                "INSERT OR IGNORE INTO user_modules(user_id, module, granted_by, granted_at) VALUES (?, ?, ?, ?)",
                (user_id, module_key, actor, now_iso()),
            )
        self._audit(
            tenant_id, "user", user_id, "created", actor,
            f"email={email};role={role}" + (f";modules={','.join(grant_modules)}" if grant_modules else ""),
        )
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

    def get_user_modules(self, user_id: str) -> list[str]:
        """员工账号已被授权的模块（管理员不落表，按全模块判定）。"""
        return [
            row["module"]
            for row in self.conn.execute(
                "SELECT module FROM user_modules WHERE user_id=? ORDER BY module", (user_id,)
            ).fetchall()
        ]

    def set_user_modules(self, tenant_id: str, user_id: str, modules, actor: str) -> list[str]:
        """管理员对员工账号的模块授权（全量覆盖式保存）；返回规范化后的模块集合。

        新账号默认零权限："必须由管理员账号进行审批并授权后"员工才可访问对应模块。
        """
        user = self.conn.execute(
            "SELECT id, email, role FROM users WHERE tenant_id=? AND id=?", (tenant_id, user_id)
        ).fetchone()
        if not user:
            raise ValueError("账号不存在")
        if user["role"] == "admin":
            raise ValueError("管理员账号为全模块权限，无需配置")
        wanted = [key for key in GRANTABLE_MODULE_KEYS if key in set(modules)]
        previous = sorted(self.get_user_modules(user_id))
        self.conn.execute("DELETE FROM user_modules WHERE user_id=?", (user_id,))
        for module_key in wanted:
            self.conn.execute(
                "INSERT INTO user_modules(user_id, module, granted_by, granted_at) VALUES (?, ?, ?, ?)",
                (user_id, module_key, actor, now_iso()),
            )
        self._audit(
            tenant_id, "user", user_id, "modules_updated", actor,
            f"email={user['email']};before={','.join(previous) or 'none'};after={','.join(wanted) or 'none'}",
        )
        self.conn.commit()
        return wanted

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
            row["id"]: (row["scope"], row["scope_revision"], row["usage"])
            for row in self.conn.execute(
                "SELECT id, scope, scope_revision, usage FROM source_documents WHERE tenant_id=? AND product_id=?",
                (tenant_id, product_id),
            )
        }
        result = set()
        for source_id, snapshot in snapshot_state.items():
            live = live_state.get(source_id)
            if (
                source_id in allowed
                and live is not None
                and (live[0], live[1]) == snapshot
                and live[2] != "restricted"
            ):
                result.add(source_id)
        return result

    def allow_public_source(self, tenant_id: str, product_id: str, source_id: str, actor: str) -> None:
        source = self.conn.execute(
            "SELECT id, storage_path, scope, usage FROM source_documents WHERE tenant_id=? AND product_id=? AND id=?",
            (tenant_id, product_id, source_id),
        ).fetchone()
        if not source or not source["storage_path"]:
            raise ValueError("需要已保存的来源文件才能开放下载")
        if (source["usage"] or "internal") == "restricted":
            raise ValueError("该文件已被标记禁止使用；先恢复可用后才能开放公开下载")
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

    def list_product_versions(self, tenant_id: str, product_id: str) -> list[sqlite3.Row]:
        """版本历史（倒序：最新在前）；含已撤回/被替换版本，供版本对比与审计。"""
        return self.conn.execute(
            "SELECT * FROM product_versions WHERE tenant_id=? AND product_id=? ORDER BY version DESC",
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
        digest = hashlib.sha256(token.encode()).hexdigest()
        row = self.conn.execute(
            "SELECT token_hash FROM buyer_conversation_access WHERE tenant_id=? AND conversation_id=?",
            (tenant_id, conversation_id),
        ).fetchone()
        if row and hmac.compare_digest(row["token_hash"], digest):
            return True
        # 跨站交接换取的凭据（同会话可有多个来源的凭据；原始凭据始终保留）
        rows = self.conn.execute(
            "SELECT token_hash FROM buyer_conversation_tokens WHERE tenant_id=? AND conversation_id=?",
            (tenant_id, conversation_id),
        ).fetchall()
        return any(hmac.compare_digest(candidate["token_hash"], digest) for candidate in rows)

    def create_handoff_code(
        self,
        tenant_id: str,
        site_key: str,
        conversation_id: str,
        product_id: str,
        ttl_seconds: int = BUYER_HANDOFF_CODE_TTL_SECONDS,
    ) -> tuple[str, str]:
        """出具一次性交接码：只存摘要，绑定会话、站点与型号，到期或使用后即失效。"""
        if not self.get_conversation(tenant_id, conversation_id):
            raise ValueError("会话不存在")
        code = secrets.token_urlsafe(32)
        created_at = now_iso()
        expires_at = (datetime.now(timezone.utc).replace(microsecond=0) + timedelta(seconds=max(1, ttl_seconds))).isoformat()
        self.conn.execute(
            "INSERT INTO buyer_handoff_codes(code_hash, tenant_id, site_key, conversation_id, product_id, created_at, expires_at, used_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, NULL)",
            (hashlib.sha256(code.encode()).hexdigest(), tenant_id, site_key, conversation_id, product_id, created_at, expires_at),
        )
        # 只保留该会话最近若干行，防止重复点击导致无上限增长；历史行不参与任何放行。
        self.conn.execute(
            "DELETE FROM buyer_handoff_codes WHERE conversation_id=? AND code_hash NOT IN ("
            "SELECT code_hash FROM buyer_handoff_codes WHERE conversation_id=? ORDER BY rowid DESC LIMIT ?)",
            (conversation_id, conversation_id, BUYER_HANDOFF_CODE_KEEP),
        )
        self._audit(tenant_id, "conversation", conversation_id, "handoff_code_created", "buyer-web", f"site={site_key};product={product_id}")
        self.conn.commit()
        return code, expires_at

    def exchange_handoff_code(self, code: str, site_key: str, product_id: str, tenant_id: str) -> dict[str, object]:
        """用一次性交接码换取同一会话的新凭据。

        全部绑定与一次性检查通过后，在同一写事务内消费代码并出具新凭据：
        并发交换同一代码只有一次成功；原始凭据与其他已发凭据在交换前后都保持有效。
        """
        row = self.conn.execute(
            "SELECT * FROM buyer_handoff_codes WHERE code_hash=?", (hashlib.sha256(code.encode()).hexdigest(),)
        ).fetchone()
        if not row:
            return {"ok": False, "error": "handoff_not_found"}
        if row["site_key"] != site_key or row["product_id"] != product_id or row["tenant_id"] != tenant_id:
            return {"ok": False, "error": "handoff_binding_mismatch"}
        if row["used_at"]:
            return {"ok": False, "error": "handoff_already_used"}
        if row["expires_at"] <= now_iso():
            return {"ok": False, "error": "handoff_expired"}
        if not self.get_conversation(tenant_id, row["conversation_id"]):
            return {"ok": False, "error": "handoff_expired"}
        token = secrets.token_urlsafe(32)
        timestamp = now_iso()
        with self.conn:
            self.conn.execute("BEGIN IMMEDIATE")
            consumed = self.conn.execute(
                "UPDATE buyer_handoff_codes SET used_at=? WHERE code_hash=? AND used_at IS NULL",
                (timestamp, row["code_hash"]),
            )
            if consumed.rowcount != 1:
                return {"ok": False, "error": "handoff_already_used"}
            self.conn.execute(
                "INSERT INTO buyer_conversation_tokens(id, conversation_id, tenant_id, token_hash, source, created_at) VALUES (?, ?, ?, ?, 'handoff', ?)",
                (secrets.token_hex(12), row["conversation_id"], tenant_id, hashlib.sha256(token.encode()).hexdigest(), timestamp),
            )
            self.conn.execute(
                "DELETE FROM buyer_conversation_tokens WHERE conversation_id=? AND id NOT IN ("
                "SELECT id FROM buyer_conversation_tokens WHERE conversation_id=? ORDER BY rowid DESC LIMIT ?)",
                (row["conversation_id"], row["conversation_id"], BUYER_HANDOFF_TOKEN_KEEP),
            )
            self._audit(tenant_id, "conversation", row["conversation_id"], "handoff_code_exchanged", "buyer-web", f"site={site_key};product={product_id}")
        return {"ok": True, "conversation_id": row["conversation_id"], "access_token": token}

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
        channel: str = "",
        external_message_id: str | None = None,
        delivery_status: str = "",
        actor_email: str = "",
    ) -> str:
        if not self.get_conversation(tenant_id, conversation_id):
            raise ValueError("会话不存在")
        content = content.strip()
        if not content:
            raise ValueError("消息不能为空")
        message_id = secrets.token_hex(12)
        self.conn.execute(
            "INSERT INTO messages(id, tenant_id, conversation_id, role, content, source, idempotency_key, request_hash, channel, external_message_id, delivery_status, actor_email, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (message_id, tenant_id, conversation_id, role, content, source, idempotency_key, request_hash, channel, external_message_id, delivery_status, actor_email, now_iso()),
        )
        self.conn.execute("UPDATE conversations SET updated_at=? WHERE tenant_id=? AND id=?", (now_iso(), tenant_id, conversation_id))
        if commit:
            self.conn.commit()
        return message_id

    # ── V1-04 外部渠道接入：渠道账户 / 渠道身份 / RFQ 表单链接 / 会话 AI 开关 ──
    def list_channel_accounts(self, tenant_id: str) -> dict[str, sqlite3.Row]:
        rows = self.conn.execute("SELECT * FROM channel_accounts WHERE tenant_id=?", (tenant_id,)).fetchall()
        return {row["channel"]: row for row in rows}

    def get_channel_account(self, tenant_id: str, channel: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM channel_accounts WHERE tenant_id=? AND channel=?", (tenant_id, channel)
        ).fetchone()

    def save_channel_account(self, tenant_id: str, channel: str, config: dict, actor: str = "demo-admin") -> None:
        """保存渠道公开配置（幂等 upsert）；机密凭证不进这里（走环境变量）。"""
        if channel not in CHANNEL_KEYS:
            raise ValueError("未知渠道")
        now = now_iso()
        config_json = json.dumps(config, ensure_ascii=False)
        existing = self.get_channel_account(tenant_id, channel)
        if existing:
            self.conn.execute(
                "UPDATE channel_accounts SET config_json=?, updated_by=?, updated_at=? WHERE tenant_id=? AND id=?",
                (config_json, actor, now, tenant_id, existing["id"]),
            )
        else:
            self.conn.execute(
                "INSERT INTO channel_accounts(id, tenant_id, channel, status, config_json, created_by, created_at, updated_by, updated_at) VALUES (?, ?, ?, 'disabled', ?, ?, ?, ?, ?)",
                (secrets.token_hex(12), tenant_id, channel, config_json, actor, now, actor, now),
            )
        self._audit(tenant_id, "channel", channel, "channel_configured", actor, ",".join(sorted(config.keys())))
        self.conn.commit()

    def set_channel_enabled(self, tenant_id: str, channel: str, enabled: bool, actor: str = "demo-admin") -> None:
        """启用/停用渠道（默认关闭 = fail-closed；启用前无需强制凭证，缺失在页面与运行时如实提示）。"""
        if channel not in CHANNEL_KEYS:
            raise ValueError("未知渠道")
        if not self.get_channel_account(tenant_id, channel):
            self.save_channel_account(tenant_id, channel, {}, actor)
        account = self.get_channel_account(tenant_id, channel)
        self.conn.execute(
            "UPDATE channel_accounts SET status=?, updated_by=?, updated_at=? WHERE tenant_id=? AND id=?",
            ("enabled" if enabled else "disabled", actor, now_iso(), tenant_id, account["id"]),
        )
        self._audit(tenant_id, "channel", channel, "channel_enabled" if enabled else "channel_disabled", actor, "")
        self.conn.commit()

    def get_channel_identity(self, tenant_id: str, channel: str, external_user_id: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM channel_identities WHERE tenant_id=? AND channel=? AND external_user_id=?",
            (tenant_id, channel, external_user_id),
        ).fetchone()

    def bind_channel_identity(self, tenant_id: str, channel: str, external_user_id: str, display_name: str, conversation_id: str) -> str:
        """外部渠道用户 ↔ 会话绑定（一外部用户一持续会话）；重复绑定的去重由调用方按唯一约束处理。"""
        if channel not in CHANNEL_KEYS:
            raise ValueError("未知渠道")
        if not self.get_conversation(tenant_id, conversation_id):
            raise ValueError("会话不存在")
        identity_id = secrets.token_hex(12)
        now = now_iso()
        self.conn.execute(
            "INSERT INTO channel_identities(id, tenant_id, channel, external_user_id, display_name, conversation_id, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (identity_id, tenant_id, channel, external_user_id, display_name, conversation_id, now, now),
        )
        self.conn.commit()
        return identity_id

    def update_channel_identity_meta(self, tenant_id: str, channel: str, external_user_id: str, meta: dict) -> None:
        """渠道身份附加信息（平台名 / 机器人标识等，供收件箱展示）；身份不存在时静默跳过。"""
        self.conn.execute(
            "UPDATE channel_identities SET platform_meta=?, updated_at=? WHERE tenant_id=? AND channel=? AND external_user_id=?",
            (json.dumps(meta, ensure_ascii=False), now_iso(), tenant_id, channel, external_user_id),
        )
        self.conn.commit()

    def find_message_by_external(self, tenant_id: str, channel: str, external_message_id: str) -> sqlite3.Row | None:
        """渠道消息去重查询：同一渠道同一条外部消息只处理一次。"""
        return self.conn.execute(
            "SELECT * FROM messages WHERE tenant_id=? AND channel=? AND external_message_id=?",
            (tenant_id, channel, external_message_id),
        ).fetchone()

    def create_rfq_form_token(self, tenant_id: str, conversation_id: str, actor: str = "demo-admin") -> str:
        """为会话生成 RFQ 填写链接令牌（只存摘要；可重复打开、可撤销；表单页在渠道切片上线）。"""
        if not self.get_conversation(tenant_id, conversation_id):
            raise ValueError("会话不存在")
        token = secrets.token_urlsafe(32)
        self.conn.execute(
            "INSERT INTO rfq_form_tokens(id, tenant_id, conversation_id, token_hash, created_by, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (secrets.token_hex(12), tenant_id, conversation_id, hashlib.sha256(token.encode()).hexdigest(), actor, now_iso()),
        )
        self._audit(tenant_id, "conversation", conversation_id, "rfq_form_created", actor, "")
        self.conn.commit()
        return token

    def get_rfq_form_token(self, tenant_id: str, token: str) -> sqlite3.Row | None:
        digest = hashlib.sha256(token.encode()).hexdigest()
        return self.conn.execute(
            "SELECT * FROM rfq_form_tokens WHERE tenant_id=? AND token_hash=? AND revoked_at IS NULL",
            (tenant_id, digest),
        ).fetchone()

    def revoke_rfq_form_token(self, tenant_id: str, token_id: str, actor: str = "demo-admin") -> None:
        row = self.conn.execute(
            "SELECT * FROM rfq_form_tokens WHERE tenant_id=? AND id=?", (tenant_id, token_id)
        ).fetchone()
        if not row:
            raise ValueError("链接不存在")
        self.conn.execute(
            "UPDATE rfq_form_tokens SET revoked_at=? WHERE tenant_id=? AND id=?", (now_iso(), tenant_id, token_id)
        )
        self.conn.commit()

    def set_conversation_ai_paused(self, tenant_id: str, conversation_id: str, paused: bool, actor: str = "demo-admin") -> None:
        """每会话「暂停 AI 自动回复」开关（默认继续自动答；人工介入不自动关闭）。"""
        if not self.get_conversation(tenant_id, conversation_id):
            raise ValueError("会话不存在")
        self.conn.execute(
            "UPDATE conversations SET ai_paused=?, updated_at=? WHERE tenant_id=? AND id=?",
            (1 if paused else 0, now_iso(), tenant_id, conversation_id),
        )
        self._audit(tenant_id, "conversation", conversation_id, "conversation_ai_switch", actor, f"paused={1 if paused else 0}")
        self.conn.commit()

    def audit_staff_reply(self, tenant_id: str, conversation_id: str, message_id: str, actor: str) -> None:
        """卖家在收件箱里发出的回复留痕（消息本体由 save_message 落库）。"""
        self._audit(tenant_id, "conversation", conversation_id, "staff_replied", actor, f"message={message_id[:8]}")
        self.conn.commit()

    def get_channel_identity_by_conversation(self, tenant_id: str, channel: str, conversation_id: str) -> sqlite3.Row | None:
        """按会话反查渠道身份（出站发送需要外部用户 ID）。"""
        return self.conn.execute(
            "SELECT * FROM channel_identities WHERE tenant_id=? AND channel=? AND conversation_id=?",
            (tenant_id, channel, conversation_id),
        ).fetchone()

    def find_rfq_form_token(self, token: str) -> sqlite3.Row | None:
        """按令牌原文跨会话查找（只存摘要；公开 RFQ 填写页用）。"""
        digest = hashlib.sha256(token.encode()).hexdigest()
        return self.conn.execute(
            "SELECT * FROM rfq_form_tokens WHERE token_hash=? AND revoked_at IS NULL", (digest,)
        ).fetchone()

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
        task_id = self.create_human_task(
            tenant_id, conversation_id, answer["question_message_id"], question["content"],
            "资料未覆盖或需要工程判断", context, answer["product_id"], answer["product_version_id"],
            revision["id"] if revision else None, recommendation["id"] if recommendation else None,
        )
        # 向会话写一条「已转交」状态消息（消息流里播报一次即可；重复请求不再追加）。
        # 参考客户端按语言本地化显示；旧 /inquiry 页按原文显示。
        ack_source = f"human-request:{task_id}"
        if not self.conn.execute(
            "SELECT 1 FROM messages WHERE tenant_id=? AND conversation_id=? AND source=?",
            (tenant_id, conversation_id, ack_source),
        ).fetchone():
            self.save_message(tenant_id, conversation_id, "assistant", HUMAN_REQUEST_ACK, ack_source)
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
            self.save_message(tenant_id, task["conversation_id"], "engineer", question, f"engineer-followup:{task_id}", commit=False, actor_email=actor)
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
            self.save_message(tenant_id, task["conversation_id"], "assistant", f"工程师回复：{response}", "human-engineer", commit=False, actor_email=actor)
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
            "requested_model": row["requested_model"],
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
        # V1-01 步骤②：隐藏字段不接受买家改动（保留现值）；必填字段修改后不可为空。
        # V1-08：按品类配置——询价已绑定产品时按其「已核准品类」解析字段配置。
        revise_category = self.get_approved_category(tenant_id, rfq["product_id"]) if rfq["product_id"] else ""
        field_config = self.get_rfq_field_config(tenant_id, revise_category)
        hidden_content_keys = {RFQ_FIELD_CONTENT_KEYS[key] for key, state in field_config.items() if state == "hidden"}
        editable = ("contact_name", "company", "contact_email", "contact_phone", "quantity", "destination", "requested_delivery", "notes", "requested_model")
        for key in editable:
            if key in changes and key not in hidden_content_keys:
                current[key] = str(changes[key]).strip()
        if "product_id" in changes:
            current["product_id"] = changes["product_id"] or None
        missing_required = [
            RFQ_FIELD_LABELS[field_key]
            for field_key in RFQ_FIELD_ORDER
            if field_config[field_key] == "required" and not str(current[RFQ_FIELD_CONTENT_KEYS[field_key]] or "").strip()
        ]
        if missing_required:
            raise ValueError("请保留：" + "、".join(missing_required))
        product_id = current["product_id"]
        version = self.get_published_version(tenant_id, product_id) if product_id else None
        if product_id and not version:
            raise ValueError("所选产品未发布或不属于当前企业")
        business_missing_labels = {"数量", "目的地", "期望交期", "邮箱", "电话"}
        technical_missing = [item for item in json.loads(rfq["missing_json"]) if item not in business_missing_labels]
        if "product_id" in changes and product_id != rfq["product_id"] and "产品适用性待核实" not in technical_missing:
            technical_missing.append("产品适用性待核实")
        missing = technical_missing + [
            RFQ_FIELD_LABELS[field_key]
            for field_key in RFQ_FIELD_ORDER
            if field_key != "notes" and field_config[field_key] == "optional" and not str(current[RFQ_FIELD_CONTENT_KEYS[field_key]] or "").strip()
        ]
        if not str(current["requested_delivery"] or "").strip():
            missing.append("期望交期")
        status = "needs_info" if missing else "submitted"
        timestamp = now_iso()
        next_revision = expected_revision + 1
        with self.conn:
            updated = self.conn.execute(
                "UPDATE rfqs SET current_revision=?, product_id=?, product_version_id=?, contact_name=?, company=?, contact_email=?, contact_phone=?, quantity=?, destination=?, requested_delivery=?, notes=?, requested_model=?, missing_json=?, status=? WHERE tenant_id=? AND conversation_id=? AND id=? AND current_revision=?",
                (next_revision, product_id, version["id"] if version else None, current["contact_name"], current["company"], current["contact_email"], current["contact_phone"], current["quantity"], current["destination"], current["requested_delivery"], current["notes"], current["requested_model"], json.dumps(missing, ensure_ascii=False), status, tenant_id, conversation_id, rfq_id, expected_revision),
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
        requested_model: str = "",
    ) -> tuple[str, bool]:
        requested_model = str(requested_model or "").strip()
        # V1-01 步骤②：按企业设置归一化（隐藏=不收集；不修改调用方传参）。
        contact = dict(contact)
        # V1-08：按品类配置——先按明确产品/型号/会话语境解析品类，再取合并后的字段配置。
        field_config = self.get_rfq_field_config(
            tenant_id,
            self.rfq_category_hint(tenant_id, conversation_id, selected_product_id, requested_model),
        )
        for field_key, field_state in field_config.items():
            if field_key != "notes" and field_state == "hidden":
                contact[RFQ_FIELD_INPUT_KEYS[field_key]] = ""
        if field_config["notes"] == "hidden":
            notes = ""
        normalized_contact = {key: contact.get(key, "").strip() for key in ("contact_name", "company", "contact_email", "contact_phone", "quantity", "region", "delivery")}
        request_hash = hashlib.sha256(json.dumps({
            "conversation_id": conversation_id, "contact": normalized_contact, "notes": notes.strip(),
            "selected_product_id": selected_product_id or None, "entry_surface": entry_surface,
            "requested_model": requested_model,
        }, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        existing = self.get_rfq_by_submission_key(tenant_id, submission_key)
        if existing:
            if existing["request_hash"] and not hmac.compare_digest(existing["request_hash"], request_hash):
                raise ConflictError("同一提交标识不能用于不同询价内容")
            return existing["id"], False
        conversation = self.get_conversation(tenant_id, conversation_id)
        if not conversation:
            raise ValueError("会话不存在")
        # V1-01 步骤②：必填校验（三个买方入口同口径；重试已在幂等分支返回，不受配置变化影响）。
        missing_required = [
            RFQ_FIELD_LABELS[field_key]
            for field_key in RFQ_FIELD_ORDER
            if field_config[field_key] == "required"
            and not (str(notes or "").strip() if field_key == "notes" else str(contact.get(RFQ_FIELD_INPUT_KEYS[field_key], "")).strip())
        ]
        if missing_required:
            raise ValueError("请填写：" + "、".join(missing_required))
        revision = self.latest_confirmed_revision(tenant_id, conversation_id)
        recommendation = self.latest_active_recommendation(tenant_id, conversation_id)
        revision_values = json.loads(revision["values_json"]) if revision else {}
        recommendation_results = json.loads(recommendation["results_json"]) if recommendation else []
        # 型号选择优先级：① 明确指定的产品（推荐结果匹配 → 已发布产品直绑）；
        # ② 买家明确填写的型号文字（推荐结果 → 已发布产品按名称匹配；匹配不上就不绑定，尊重买家表述）；
        # ③ 两者都没有（旧客户端）：沿用原有行为，自动绑定当前推荐的首个候选。
        selected_result = None
        if selected_product_id:
            selected_result = next((result for result in recommendation_results if result.get("product_id") == selected_product_id), None)
            if not selected_result:
                product = self.get_product(tenant_id, selected_product_id)
                version = self.get_published_version(tenant_id, selected_product_id) if product else None
                if product is not None and version:
                    selected_result = {"product_id": selected_product_id, "product_version_id": version["id"], "model": product["model"], "status": "direct_view"}
        if not selected_result and requested_model:
            wanted = requested_model.lower()
            selected_result = next((result for result in recommendation_results if str(result.get("model") or "").strip().lower() == wanted), None)
            if not selected_result:
                for candidate_product in self.list_products(tenant_id):
                    if str(candidate_product["model"] or "").strip().lower() != wanted:
                        continue
                    version = self.get_published_version(tenant_id, candidate_product["id"])
                    if version:
                        selected_result = {"product_id": candidate_product["id"], "product_version_id": version["id"], "model": candidate_product["model"], "status": "direct_view"}
                    break
        if not selected_result and not requested_model:
            selected_result = next((result for result in recommendation_results if result.get("status") in {"candidate", "needs_confirmation"}), None)
        product_id = selected_result.get("product_id") if selected_result else None
        product_version_id = selected_result.get("product_version_id") if selected_result else None
        missing = []
        if not revision:
            missing.append("工况待确认")
        if selected_result and selected_result.get("status") == "needs_confirmation":
            missing.append("产品适用性待工程师核实")
        for field_key in RFQ_FIELD_ORDER:
            if field_key == "notes":
                continue
            if field_config[field_key] == "optional" and not str(contact.get(RFQ_FIELD_INPUT_KEYS[field_key], "")).strip():
                missing.append(RFQ_FIELD_LABELS[field_key])
        if not contact.get("delivery", "").strip():
            missing.append("期望交期")
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
            "INSERT INTO rfqs(id, tenant_id, conversation_id, requirement_revision_id, recommendation_id, product_id, product_version_id, submission_key, request_hash, contact_name, company, contact_email, contact_phone, quantity, destination, requested_delivery, notes, requested_model, missing_json, status, snapshot_json, created_at, contact_consent_source, contact_consent_at, entry_surface) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (rfq_id, tenant_id, conversation_id, revision["id"] if revision else None, recommendation["id"] if recommendation else None, product_id, product_version_id, submission_key, request_hash, normalized_contact["contact_name"], normalized_contact["company"], normalized_contact["contact_email"], normalized_contact["contact_phone"], normalized_contact["quantity"], normalized_contact["region"], normalized_contact["delivery"], notes.strip(), requested_model, json.dumps(missing, ensure_ascii=False), status, json.dumps(snapshot, ensure_ascii=False), timestamp, entry_surface or "chat", timestamp, entry_surface or "chat"),
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
        user = self.conn.execute("SELECT id, role, active FROM users WHERE tenant_id=? AND email=?", (tenant_id, actor_email)).fetchone()
        lead = self.get_lead_for_rfq(tenant_id, rfq_id)
        can_claim = (
            bool(user) and user["active"] and user["role"] != "admin"
            and "sales" in self.get_user_modules(user["id"])
        )
        if not can_claim or not lead:
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
        owner = self.conn.execute("SELECT id, role, active FROM users WHERE tenant_id=? AND email=?", (tenant_id, owner_email)).fetchone()
        lead = self.get_lead_for_rfq(tenant_id, rfq_id)
        owner_ok = (
            bool(owner) and owner["active"] and owner["role"] != "admin"
            and "sales" in self.get_user_modules(owner["id"])
        )
        if not actor or not actor["active"] or actor["role"] != "admin" or not owner_ok or not lead:
            raise ValueError("管理员只能分配给已授权「销售工作台」模块的员工")
        with self.conn:
            self.conn.execute("UPDATE leads SET owner=?, updated_at=? WHERE tenant_id=? AND conversation_id=?", (owner_email, now_iso(), tenant_id, lead["conversation_id"]))
            self._audit(tenant_id, "lead", lead["id"], "assigned", actor_email, f"conversation={lead['conversation_id']};owner={owner_email}")

    def list_sales_records(self, tenant_id: str) -> list[sqlite3.Row]:
        # 排序按企业设置的阶段顺序（V1-01 步骤②：可改名/排序；键来自固定常量，无注入面）。
        stage_keys = [stage["key"] for stage in self.get_lead_stages(tenant_id)]
        order_case = " ".join(f"WHEN '{key}' THEN {index}" for index, key in enumerate(stage_keys))
        return self.conn.execute(
            "SELECT r.*, p.model, l.id AS lead_id, l.stage, l.owner, l.next_follow_up, l.note, l.updated_at AS lead_updated_at, c.stop_proactive_contact, "
            "(SELECT COUNT(*) FROM rfq_revisions v WHERE v.rfq_id=r.id AND v.revision>1) AS buyer_revision_count, "
            "(SELECT MAX(v.created_at) FROM rfq_revisions v WHERE v.rfq_id=r.id) AS last_revision_at "
            "FROM rfqs r LEFT JOIN products p ON p.id=r.product_id LEFT JOIN leads l ON l.rfq_id=r.id AND l.tenant_id=r.tenant_id JOIN conversations c ON c.id=r.conversation_id AND c.tenant_id=r.tenant_id "
            f"WHERE r.tenant_id=? AND COALESCE(l.merged_into, '') = '' ORDER BY CASE l.stage {order_case} ELSE {len(stage_keys)} END, l.next_follow_up, r.created_at DESC",
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
        # 终态阶段（默认：暂缓/已关闭）不计入在跟与逾期；随企业设置的阶段终态标记变化。
        terminal_keys = [stage["key"] for stage in self.get_lead_stages(tenant_id) if stage["terminal"]]
        terminal_clause = f"stage NOT IN ({', '.join('?' for _ in terminal_keys)})" if terminal_keys else "1=1"
        lead_counts = self.conn.execute(
            f"SELECT "
            f"SUM(CASE WHEN {terminal_clause} THEN 1 ELSE 0 END) AS active, "
            f"SUM(CASE WHEN next_follow_up < ? AND {terminal_clause} THEN 1 ELSE 0 END) AS overdue "
            "FROM leads WHERE tenant_id=? AND COALESCE(merged_into, '') = ''",
            [*terminal_keys, today, *terminal_keys, tenant_id],
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

    def record_ai_call(
        self,
        tenant_id: str,
        conversation_id: str | None,
        task: str,
        provider: str,
        model: str,
        status: str,
        action: str,
        http_status: int | None = None,
        duration_ms: int | None = None,
        prompt_tokens: int | None = None,
        completion_tokens: int | None = None,
    ) -> None:
        """记录一次模型调用的内部状态；只存提供方/模型/状态/耗时/用量。

        不存密钥、提示词、买方消息或联系方式；排错沿 conversation_id 关联业务对象。
        """
        with self.conn:
            self.conn.execute(
                "INSERT INTO ai_call_log(id, tenant_id, conversation_id, task, provider, model, status, action, "
                "http_status, duration_ms, prompt_tokens, completion_tokens, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    secrets.token_hex(12), tenant_id, conversation_id, task, provider, model, status, action,
                    http_status, duration_ms, prompt_tokens, completion_tokens, now_iso(),
                ),
            )

    def latest_ai_call(self, tenant_id: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM ai_call_log WHERE tenant_id=? ORDER BY created_at DESC, rowid DESC LIMIT 1",
            (tenant_id,),
        ).fetchone()

    def update_lead(
        self,
        tenant_id: str,
        rfq_id: str,
        stage: str,
        next_follow_up: str,
        note: str,
        actor: str = "demo-sales",
        close_reason: str = "",
    ) -> None:
        allowed = {stage["key"] for stage in self.get_lead_stages(tenant_id)}
        if stage not in allowed:
            raise ValueError("不支持的线索阶段")
        lead = self.get_lead_for_rfq(tenant_id, rfq_id)
        if not lead:
            raise ValueError("线索不存在")
        # V1-09：关闭线索必须填写原因；重新选择其他阶段 = 重新激活（审计留痕）。
        closing = stage == "closed"
        reopening = bool(lead["stage"] == "closed" and not closing)
        close_reason = close_reason.strip()
        if closing and not close_reason:
            raise ValueError("关闭线索需要填写原因")
        with self.conn:
            self.conn.execute("BEGIN IMMEDIATE")
            preference = self.get_contact_preference(tenant_id, lead["conversation_id"])
            if preference and preference["stop_proactive_contact"] and next_follow_up.strip():
                raise ValueError("买方已要求停止主动联系，不能安排主动跟进日期")
            timestamp = now_iso()
            if closing:
                self.conn.execute(
                    "UPDATE leads SET stage=?, next_follow_up=?, note=?, close_reason=?, closed_at=?, updated_at=? WHERE tenant_id=? AND rfq_id=?",
                    (stage, next_follow_up.strip() or None, note.strip(), close_reason, timestamp, timestamp, tenant_id, rfq_id),
                )
            elif reopening:
                self.conn.execute(
                    "UPDATE leads SET stage=?, next_follow_up=?, note=?, close_reason='', closed_at=NULL, updated_at=? WHERE tenant_id=? AND rfq_id=?",
                    (stage, next_follow_up.strip() or None, note.strip(), timestamp, tenant_id, rfq_id),
                )
            else:
                self.conn.execute(
                    "UPDATE leads SET stage=?, next_follow_up=?, note=?, updated_at=? WHERE tenant_id=? AND rfq_id=?",
                    (stage, next_follow_up.strip() or None, note.strip(), timestamp, tenant_id, rfq_id),
                )
            details = f"rfq={rfq_id};stage={stage};next_follow_up={next_follow_up}"
            if closing:
                details += f";close_reason={close_reason};transition=closed"
            if reopening:
                details += ";transition=reopened"
            self._audit(tenant_id, "lead", lead["id"], "updated", actor, details)

    # ── V1-08：待培育线索（未形成 RFQ；手动登记线下/展会询盘）────────────────────
    def create_nurture_lead(
        self,
        tenant_id: str,
        actor: str,
        contact_name: str,
        company: str,
        contact_email: str,
        contact_phone: str,
        source: str,
        note: str,
        consent_confirmed: bool = False,
    ) -> str:
        if source not in NURTURE_SOURCE_LABELS:
            raise ValueError("来源渠道无效")
        if not consent_confirmed:
            raise ValueError("登记前需要确认对方同意后续联系（线下登记也不能跳过）")
        email = contact_email.strip()
        phone = contact_phone.strip()
        if not email and not phone:
            raise ValueError("需要至少一种可回复的联系方式（邮箱或电话）")
        if not (contact_name.strip() or company.strip()):
            raise ValueError("请至少填写联系人称呼或公司名称")
        lead_id = secrets.token_hex(12)
        timestamp = now_iso()
        with self.conn:
            self.conn.execute(
                "INSERT INTO leads(id, tenant_id, rfq_id, conversation_id, kind, source, contact_name, company, contact_email, contact_phone, "
                "stage, owner, next_follow_up, note, consent_source, consent_at, created_at, updated_at) "
                "VALUES (?, ?, NULL, NULL, 'nurture', ?, ?, ?, ?, ?, 'new', ?, NULL, ?, 'offline', ?, ?, ?)",
                (lead_id, tenant_id, source, contact_name.strip(), company.strip(), email, phone, actor, note.strip(), timestamp, timestamp, timestamp),
            )
            self._audit(
                tenant_id, "lead", lead_id, "nurture_created", actor,
                f"source={NURTURE_SOURCE_LABELS[source]};contact={contact_name.strip() or company.strip()}",
            )
        return lead_id

    def get_lead(self, tenant_id: str, lead_id: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM leads WHERE tenant_id=? AND id=?", (tenant_id, lead_id)
        ).fetchone()

    def list_nurture_leads(self, tenant_id: str) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM leads WHERE tenant_id=? AND kind='nurture' AND COALESCE(merged_into, '') = '' "
            "ORDER BY (next_follow_up IS NULL), next_follow_up, created_at DESC",
            (tenant_id,),
        ).fetchall()

    def update_lead_by_id(
        self,
        tenant_id: str,
        lead_id: str,
        stage: str,
        next_follow_up: str,
        note: str,
        actor: str = "demo-sales",
        close_reason: str = "",
    ) -> None:
        """按线索 id 更新（待培育线索；与 RFQ 线索同一套阶段/关闭/重开语义）。"""
        allowed = {stage["key"] for stage in self.get_lead_stages(tenant_id)}
        if stage not in allowed:
            raise ValueError("不支持的线索阶段")
        lead = self.get_lead(tenant_id, lead_id)
        if not lead:
            raise ValueError("线索不存在")
        closing = stage == "closed"
        reopening = bool(lead["stage"] == "closed" and not closing)
        close_reason = close_reason.strip()
        if closing and not close_reason:
            raise ValueError("关闭线索需要填写原因")
        with self.conn:
            self.conn.execute("BEGIN IMMEDIATE")
            timestamp = now_iso()
            if closing:
                self.conn.execute(
                    "UPDATE leads SET stage=?, next_follow_up=?, note=?, close_reason=?, closed_at=?, updated_at=? WHERE tenant_id=? AND id=?",
                    (stage, next_follow_up.strip() or None, note.strip(), close_reason, timestamp, timestamp, tenant_id, lead_id),
                )
            elif reopening:
                self.conn.execute(
                    "UPDATE leads SET stage=?, next_follow_up=?, note=?, close_reason='', closed_at=NULL, updated_at=? WHERE tenant_id=? AND id=?",
                    (stage, next_follow_up.strip() or None, note.strip(), timestamp, tenant_id, lead_id),
                )
            else:
                self.conn.execute(
                    "UPDATE leads SET stage=?, next_follow_up=?, note=?, updated_at=? WHERE tenant_id=? AND id=?",
                    (stage, next_follow_up.strip() or None, note.strip(), timestamp, tenant_id, lead_id),
                )
            details = f"stage={stage};next_follow_up={next_follow_up}"
            if closing:
                details += f";close_reason={close_reason};transition=closed"
            if reopening:
                details += ";transition=reopened"
            self._audit(tenant_id, "lead", lead_id, "updated", actor, details)

    def assign_nurture_lead(self, tenant_id: str, lead_id: str, owner_email: str, actor_email: str) -> None:
        actor = self.conn.execute("SELECT role, active FROM users WHERE tenant_id=? AND email=?", (tenant_id, actor_email)).fetchone()
        owner = self.conn.execute("SELECT id, role, active FROM users WHERE tenant_id=? AND email=?", (tenant_id, owner_email)).fetchone()
        lead = self.get_lead(tenant_id, lead_id)
        owner_ok = (
            bool(owner) and owner["active"] and owner["role"] != "admin"
            and "sales" in self.get_user_modules(owner["id"])
        )
        if not actor or not actor["active"] or actor["role"] != "admin" or not owner_ok or not lead or lead["kind"] != "nurture":
            raise ValueError("管理员只能把待培育线索分配给已授权「销售工作台」模块的员工")
        with self.conn:
            self.conn.execute(
                "UPDATE leads SET owner=?, updated_at=? WHERE tenant_id=? AND id=?",
                (owner_email, now_iso(), tenant_id, lead_id),
            )
            self._audit(tenant_id, "lead", lead_id, "assigned", actor_email, f"owner={owner_email}")

    # ── V1-08：重复线索提示与人工合并（只提示、绝不自动合并）──────────────────────
    def lead_contact_index(self, tenant_id: str) -> list[dict]:
        """未合并线索的联系方式索引（RFQ 线索取询价联系人；待培育线索取登记联系人）。"""
        rows = self.conn.execute(
            "SELECT l.id, l.kind, l.owner, l.stage, l.created_at, l.rfq_id, "
            "l.contact_email AS n_email, l.contact_phone AS n_phone, l.company AS n_company, l.contact_name AS n_name, "
            "r.contact_email AS r_email, r.contact_phone AS r_phone, r.company AS r_company, r.contact_name AS r_name "
            "FROM leads l LEFT JOIN rfqs r ON r.id = l.rfq_id AND r.tenant_id = l.tenant_id "
            "WHERE l.tenant_id=? AND COALESCE(l.merged_into, '') = ''",
            (tenant_id,),
        ).fetchall()
        index = []
        for row in rows:
            nurture = row["kind"] == "nurture"
            index.append(
                {
                    "id": row["id"], "kind": row["kind"], "owner": row["owner"], "stage": row["stage"],
                    "rfq_id": row["rfq_id"], "created_at": row["created_at"],
                    "email": (row["n_email"] if nurture else row["r_email"]) or "",
                    "phone": (row["n_phone"] if nurture else row["r_phone"]) or "",
                    "company": (row["n_company"] if nurture else row["r_company"]) or "",
                    "name": (row["n_name"] if nurture else row["r_name"]) or "",
                }
            )
        return index

    def find_duplicate_lead_hints(self, tenant_id: str, lead_id: str) -> list[dict]:
        """同邮箱或同公司名的其他未合并线索（仅提示，不自动合并；上限 5 条）。"""
        index = self.lead_contact_index(tenant_id)
        target = next((item for item in index if item["id"] == lead_id), None)
        if not target:
            return []
        email = target["email"].strip().lower()
        company = re.sub(r"\s+", "", target["company"].strip().lower())
        hints = []
        for item in index:
            if item["id"] == lead_id:
                continue
            matched = []
            if email and item["email"].strip().lower() == email:
                matched.append("同邮箱")
            item_company = re.sub(r"\s+", "", item["company"].strip().lower())
            if company and item_company == company:
                matched.append("同公司")
            if matched:
                entry = dict(item)
                entry["matched_by"] = matched
                hints.append(entry)
        return hints[:5]

    def merged_related_leads(self, tenant_id: str, lead_id: str) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM leads WHERE tenant_id=? AND merged_into=? ORDER BY merged_at",
            (tenant_id, lead_id),
        ).fetchall()

    def merge_leads(self, tenant_id: str, primary_id: str, secondary_id: str, actor: str) -> None:
        """人工合并（管理员）：次级线索标记并入并移出列表；全部记录与历史保留。"""
        if primary_id == secondary_id:
            raise ValueError("不能把线索合并到自身")
        primary = self.get_lead(tenant_id, primary_id)
        secondary = self.get_lead(tenant_id, secondary_id)
        if not primary or not secondary:
            raise ValueError("线索不存在")
        if (secondary["merged_into"] or "") or (primary["merged_into"] or ""):
            raise ValueError("线索已合并，不能重复操作")
        if secondary["rfq_id"] and secondary["rfq_id"] == primary["rfq_id"]:
            raise ValueError("两条线索属于同一询价记录")
        timestamp = now_iso()
        owner_transferred = ""
        with self.conn:
            self.conn.execute(
                "UPDATE leads SET merged_into=?, merged_at=?, updated_at=? WHERE tenant_id=? AND id=?",
                (primary_id, timestamp, timestamp, tenant_id, secondary_id),
            )
            if not (primary["owner"] or "").strip() and (secondary["owner"] or "").strip():
                self.conn.execute(
                    "UPDATE leads SET owner=?, updated_at=? WHERE tenant_id=? AND id=?",
                    (secondary["owner"], timestamp, tenant_id, primary_id),
                )
                owner_transferred = secondary["owner"]
            self._audit(tenant_id, "lead", secondary_id, "merged", actor, f"into={primary_id}")
            details = f"from={secondary_id}"
            if owner_transferred:
                details += f";owner_transferred={owner_transferred}"
            self._audit(tenant_id, "lead", primary_id, "merged_in", actor, details)

    # ── V1-09：跟进节点（技术确认/样品/报价准备）──────────────────────────────
    def list_lead_milestones(self, tenant_id: str, lead_id: str) -> dict[str, sqlite3.Row]:
        rows = self.conn.execute(
            "SELECT * FROM lead_milestones WHERE tenant_id=? AND lead_id=?", (tenant_id, lead_id)
        ).fetchall()
        return {row["key"]: row for row in rows}

    def toggle_lead_milestone(self, tenant_id: str, rfq_id: str, key: str, actor: str) -> bool:
        lead = self.get_lead_for_rfq(tenant_id, rfq_id)
        if not lead:
            raise ValueError("线索不存在")
        return self.toggle_lead_milestone_for_lead(tenant_id, lead["id"], key, actor)

    def toggle_lead_milestone_for_lead(self, tenant_id: str, lead_id: str, key: str, actor: str) -> bool:
        if key not in LEAD_MILESTONE_KEYS:
            raise ValueError("不支持的跟进节点")
        lead = self.get_lead(tenant_id, lead_id)
        if not lead:
            raise ValueError("线索不存在")
        existing = self.conn.execute(
            "SELECT * FROM lead_milestones WHERE tenant_id=? AND lead_id=? AND key=?", (tenant_id, lead["id"], key)
        ).fetchone()
        with self.conn:
            if existing:
                self.conn.execute(
                    "DELETE FROM lead_milestones WHERE tenant_id=? AND lead_id=? AND key=?", (tenant_id, lead["id"], key)
                )
                self._audit(tenant_id, "lead", lead["id"], "milestone_reopened", actor, f"key={key}")
                return False
            self.conn.execute(
                "INSERT INTO lead_milestones(tenant_id, lead_id, key, done_by, done_at) VALUES (?, ?, ?, ?, ?)",
                (tenant_id, lead["id"], key, actor, now_iso()),
            )
            label = dict(LEAD_MILESTONE_DEFS)[key]
            self._audit(tenant_id, "lead", lead["id"], "milestone_completed", actor, f"key={key};label={label}")
            return True

    # ── V1-09：回复草稿（渠道会话可经出站总线真发；其余线下发送后记录；发送前检查停止主动联系）──
    def list_lead_replies(self, tenant_id: str, lead_id: str) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM lead_replies WHERE tenant_id=? AND lead_id=? "
            "ORDER BY CASE status WHEN 'draft' THEN 0 WHEN 'failed' THEN 1 WHEN 'queued' THEN 2 WHEN 'sent' THEN 3 ELSE 4 END, created_at DESC",
            (tenant_id, lead_id),
        ).fetchall()

    def _get_lead_reply(self, tenant_id: str, reply_id: str) -> sqlite3.Row:
        row = self.conn.execute(
            "SELECT * FROM lead_replies WHERE tenant_id=? AND id=?", (tenant_id, reply_id)
        ).fetchone()
        if not row:
            raise ValueError("回复不存在")
        return row

    def create_reply_draft(self, tenant_id: str, rfq_id: str, body: str, actor: str) -> str:
        lead = self.get_lead_for_rfq(tenant_id, rfq_id)
        if not lead:
            raise ValueError("线索不存在")
        body = body.strip()
        if not body:
            raise ValueError("回复内容不能为空")
        reply_id = secrets.token_hex(12)
        timestamp = now_iso()
        with self.conn:
            self.conn.execute(
                "INSERT INTO lead_replies(id, tenant_id, lead_id, body, status, created_by, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, 'draft', ?, ?, ?)",
                (reply_id, tenant_id, lead["id"], body, actor, timestamp, timestamp),
            )
            self._audit(tenant_id, "reply", reply_id, "created", actor, f"lead={lead['id']};len={len(body)}")
        return reply_id

    def update_reply_draft(self, tenant_id: str, reply_id: str, body: str, actor: str) -> None:
        reply = self._get_lead_reply(tenant_id, reply_id)
        if reply["status"] != "draft":
            raise ValueError("只有未发送草稿可以修改")
        body = body.strip()
        if not body:
            raise ValueError("回复内容不能为空")
        with self.conn:
            self.conn.execute(
                "UPDATE lead_replies SET body=?, updated_by=?, updated_at=? WHERE tenant_id=? AND id=?",
                (body, actor, now_iso(), tenant_id, reply_id),
            )
            self._audit(tenant_id, "reply", reply_id, "updated", actor, f"len={len(body)}")

    def discard_reply_draft(self, tenant_id: str, reply_id: str, actor: str) -> None:
        reply = self._get_lead_reply(tenant_id, reply_id)
        if reply["status"] != "draft":
            raise ValueError("只有未发送草稿可以废弃")
        with self.conn:
            self.conn.execute(
                "UPDATE lead_replies SET status='discarded', updated_by=?, updated_at=? WHERE tenant_id=? AND id=?",
                (actor, now_iso(), tenant_id, reply_id),
            )
            self._audit(tenant_id, "reply", reply_id, "discarded", actor, "")

    def record_reply_sent(self, tenant_id: str, reply_id: str, channel: str, body: str, actor: str) -> None:
        reply = self._get_lead_reply(tenant_id, reply_id)
        if reply["status"] != "draft":
            raise ValueError("该回复不是未发送草稿")
        if channel not in REPLY_CHANNEL_LABELS:
            raise ValueError("不支持的发送渠道")
        lead = self.conn.execute(
            "SELECT * FROM leads WHERE tenant_id=? AND id=?", (tenant_id, reply["lead_id"])
        ).fetchone()
        if not lead:
            raise ValueError("线索不存在")
        preference = self.get_contact_preference(tenant_id, lead["conversation_id"])
        if preference and preference["stop_proactive_contact"]:
            raise ValueError("买方已要求停止主动联系，发送被阻止")
        body = body.strip() or reply["body"]
        timestamp = now_iso()
        with self.conn:
            self.conn.execute(
                "UPDATE lead_replies SET body=?, status='sent', channel=?, sent_by=?, sent_at=?, updated_by=?, updated_at=? WHERE tenant_id=? AND id=?",
                (body, channel, actor, timestamp, actor, timestamp, tenant_id, reply_id),
            )
            self._audit(tenant_id, "reply", reply_id, "sent", actor, f"lead={reply['lead_id']};channel={channel}")

    def send_reply_via_channel(self, tenant_id: str, reply_id: str, actor: str) -> tuple[bool, str]:
        """F-22：回复草稿经真实渠道发送（draft/failed/queued → queued → sent/failed）。

        - 只在「会话已接渠道」时可用；每次尝试都检查停止主动联系（被阻止抛 ValueError）；
        - 重试复用同一条会话消息（message_id 关联），绝不重复建消息、重复发送；
        - 审计 action=sent_via_channel（含渠道、结果与尝试次数）。
        """
        reply = self._get_lead_reply(tenant_id, reply_id)
        if reply["status"] == "sent":
            return True, ""
        lead = self.conn.execute("SELECT * FROM leads WHERE tenant_id=? AND id=?", (tenant_id, reply["lead_id"])).fetchone()
        if not lead:
            raise ValueError("线索不存在")
        conversation = self.get_conversation(tenant_id, lead["conversation_id"])
        channel = (conversation["source"] or "") if conversation else ""
        if not conversation or channel not in CHANNEL_KEYS:
            raise ValueError("该会话没有已接入的发送渠道")
        preference = self.get_contact_preference(tenant_id, conversation["id"])
        if preference and preference["stop_proactive_contact"]:
            raise ValueError("买方已要求停止主动联系，发送被阻止")
        message_id = reply["message_id"]
        if not message_id:
            message_id = self.save_message(
                tenant_id, conversation["id"], "staff", reply["body"], "lead-reply",
                channel=channel, delivery_status="queued", actor_email=actor,
            )
            self.conn.execute(
                "UPDATE lead_replies SET message_id=?, channel=?, updated_by=?, updated_at=? WHERE tenant_id=? AND id=?",
                (message_id, channel, actor, now_iso(), tenant_id, reply_id),
            )
            self.conn.commit()
        sent, error = dispatch_outbound(self, tenant_id, message_id)
        attempts_row = self.conn.execute("SELECT delivery_attempts FROM messages WHERE id=?", (message_id,)).fetchone()
        attempts = attempts_row["delivery_attempts"] if attempts_row else 0
        timestamp = now_iso()
        if sent:
            self.conn.execute(
                "UPDATE lead_replies SET status='sent', sent_by=?, sent_at=?, updated_by=?, updated_at=? WHERE tenant_id=? AND id=?",
                (actor, timestamp, actor, timestamp, tenant_id, reply_id),
            )
        else:
            self.conn.execute(
                "UPDATE lead_replies SET status='failed', updated_by=?, updated_at=? WHERE tenant_id=? AND id=?",
                (actor, timestamp, tenant_id, reply_id),
            )
        self._audit(tenant_id, "reply", reply_id, "sent_via_channel", actor, f"channel={channel};result={'sent' if sent else 'failed'};attempts={attempts}")
        self.conn.commit()
        return sent, error

    def list_entity_audit(self, tenant_id: str, entity_ids: list[str]) -> list[sqlite3.Row]:
        if not entity_ids:
            return []
        placeholders = ",".join("?" for _ in entity_ids)
        return self.conn.execute(
            f"SELECT * FROM audit_log WHERE tenant_id=? AND entity_id IN ({placeholders}) ORDER BY created_at DESC",
            [tenant_id, *entity_ids],
        ).fetchall()

    # ── 企业设置（V1-01 步骤②）：通用存取 + 校验回退 ─────────────────────────
    def get_tenant_setting(self, tenant_id: str, key: str) -> tuple[object, str]:
        """读取企业设置；返回 (解析值, 更新时间)。无记录返回 (None, "")；损坏 JSON 视为无值。"""
        row = self.conn.execute(
            "SELECT value_json, updated_at FROM tenant_settings WHERE tenant_id=? AND key=?",
            (tenant_id, key),
        ).fetchone()
        if not row:
            return None, ""
        try:
            return json.loads(row["value_json"]), row["updated_at"]
        except json.JSONDecodeError:
            return None, row["updated_at"]

    def set_tenant_setting(self, tenant_id: str, key: str, value: object, actor: str) -> None:
        """写入企业设置并留审计（before/after 全量）。"""
        before, _ = self.get_tenant_setting(tenant_id, key)
        timestamp = now_iso()
        with self.conn:
            self.conn.execute(
                "INSERT INTO tenant_settings(tenant_id, key, value_json, updated_at) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(tenant_id, key) DO UPDATE SET value_json=excluded.value_json, updated_at=excluded.updated_at",
                (tenant_id, key, json.dumps(value, ensure_ascii=False), timestamp),
            )
            self._audit(
                tenant_id, "settings", key, "updated", actor,
                f"before={json.dumps(before, ensure_ascii=False)};after={json.dumps(value, ensure_ascii=False)}",
            )

    def get_lead_stages(self, tenant_id: str) -> list[dict[str, object]]:
        """线索阶段清单（按保存顺序）；键集合固定，损坏数据回退默认。"""
        stored, _ = self.get_tenant_setting(tenant_id, SETTING_LEAD_STAGES)
        defaults = {stage["key"]: dict(stage) for stage in DEFAULT_LEAD_STAGES}
        stages: list[dict[str, object]] = []
        seen: set[str] = set()
        if isinstance(stored, list):
            for item in stored:
                if not isinstance(item, dict) or item.get("key") not in defaults or item["key"] in seen:
                    continue
                seen.add(item["key"])
                label = str(item.get("label") or "").strip() or str(defaults[item["key"]]["label"])
                stages.append({"key": item["key"], "label": label[:24], "terminal": bool(item.get("terminal", defaults[item["key"]]["terminal"]))})
        if len(stages) != len(LEAD_STAGE_KEYS):
            return [dict(stage) for stage in DEFAULT_LEAD_STAGES]
        return stages

    def get_rfq_field_config(self, tenant_id: str, category: str | None = None) -> dict[str, str]:
        """RFQ 字段三态配置；可按已核准品类叠加覆盖（未覆盖字段继承全局）。

        非法值回退默认；邮箱与电话不可同时隐藏（损坏数据兜底：电话回落选填）。
        品类为空或没有覆盖时，行为与纯全局配置完全一致。
        """
        stored, _ = self.get_tenant_setting(tenant_id, SETTING_RFQ_FIELDS)
        config: dict[str, str] = {}
        for key in RFQ_FIELD_ORDER:
            value = stored.get(key) if isinstance(stored, dict) else None
            config[key] = value if value in RFQ_FIELD_STATES else DEFAULT_RFQ_FIELDS[key]
        category = (category or "").strip()
        if category:
            by_category, _ = self.get_tenant_setting(tenant_id, SETTING_RFQ_FIELDS_BY_CATEGORY)
            overrides = by_category.get(category) if isinstance(by_category, dict) else None
            if isinstance(overrides, dict):
                for key in RFQ_FIELD_ORDER:
                    value = overrides.get(key)
                    if value in RFQ_FIELD_STATES:
                        config[key] = value
        if config["email"] == "hidden" and config["phone"] == "hidden":
            config["phone"] = "optional"
        return config

    def approved_categories(self, tenant_id: str) -> list[str]:
        """已核准品类清单（来自产品页核准动作；用于按品类配置与工作流）。"""
        rows = self.conn.execute(
            "SELECT DISTINCT category FROM products WHERE tenant_id=? AND TRIM(category) != '' ORDER BY category",
            (tenant_id,),
        ).fetchall()
        return [row["category"] for row in rows]

    def set_rfq_fields_for_category(self, tenant_id: str, category: str, overrides: dict[str, str], actor: str) -> None:
        """保存某品类的 RFQ 字段覆盖（空 dict = 清除覆盖）；只能用于已核准品类。"""
        category = category.strip()
        if category not in self.approved_categories(tenant_id):
            raise ValueError("只能为已核准的品类配置询价字段；请先在产品页核准品类")
        effective = self.get_rfq_field_config(tenant_id)
        cleaned: dict[str, str] = {}
        for key, state in (overrides or {}).items():
            if key not in RFQ_FIELD_ORDER or state not in RFQ_FIELD_STATES:
                raise ValueError("按品类询价字段配置无效")
            cleaned[key] = state
            effective[key] = state
        if effective["email"] == "hidden" and effective["phone"] == "hidden":
            raise ValueError("邮箱与电话不能同时隐藏（含继承全局后的结果）")
        stored, _ = self.get_tenant_setting(tenant_id, SETTING_RFQ_FIELDS_BY_CATEGORY)
        data = dict(stored) if isinstance(stored, dict) else {}
        if cleaned:
            data[category] = cleaned
        else:
            data.pop(category, None)
        self.set_tenant_setting(tenant_id, SETTING_RFQ_FIELDS_BY_CATEGORY, data, actor)

    def conversation_category_hint(self, tenant_id: str, conversation_id: str) -> str:
        """会话语境提示：取最新有效推荐中首个可绑定产品的「已核准品类」（否则空串）。"""
        recommendation = self.latest_active_recommendation(tenant_id, conversation_id)
        if not recommendation:
            return ""
        for result in json.loads(recommendation["results_json"]):
            product_id = result.get("product_id")
            if product_id:
                category = self.get_approved_category(tenant_id, product_id)
                if category:
                    return category
        return ""

    def rfq_category_hint(
        self, tenant_id: str, conversation_id: str, selected_product_id: str | None, requested_model: str
    ) -> str:
        """创建 RFQ 时解析按品类覆盖：明确产品 ＞ 明确型号文字 ＞ 会话推荐语境；都没有则空。"""
        if selected_product_id:
            category = self.get_approved_category(tenant_id, selected_product_id)
            if category:
                return category
        wanted = str(requested_model or "").strip().lower()
        if wanted:
            row = self.conn.execute(
                "SELECT id FROM products WHERE tenant_id=? AND LOWER(model)=? LIMIT 1", (tenant_id, wanted)
            ).fetchone()
            if row:
                category = self.get_approved_category(tenant_id, row["id"])
                if category:
                    return category
        if conversation_id:
            return self.conversation_category_hint(tenant_id, conversation_id)
        return ""

    def get_notify_recipients(self, tenant_id: str) -> list[str]:
        """通知接收人（仅登记；渠道未接入）。返回去重后的合法邮箱列表（最多 20 个）。"""
        stored, _ = self.get_tenant_setting(tenant_id, SETTING_NOTIFY_RECIPIENTS)
        if not isinstance(stored, list):
            return []
        recipients: list[str] = []
        for item in stored:
            address = str(item or "").strip()
            if address and EMAIL_PATTERN.match(address) and address not in recipients:
                recipients.append(address)
        return recipients[:20]

    def list_audit_records(self, tenant_id: str, page: int = 1, page_size: int = 50, entity_type: str = "", actor: str = "") -> tuple[int, list[sqlite3.Row]]:
        """操作记录分页查询（租户隔离；可按对象类型/操作者筛选）。"""
        clauses = ["tenant_id=?"]
        params: list[object] = [tenant_id]
        if entity_type:
            clauses.append("entity_type=?")
            params.append(entity_type)
        if actor:
            clauses.append("actor=?")
            params.append(actor)
        where = " AND ".join(clauses)
        total = int(self.conn.execute(f"SELECT COUNT(*) AS n FROM audit_log WHERE {where}", params).fetchone()["n"])
        rows = self.conn.execute(
            f"SELECT * FROM audit_log WHERE {where} ORDER BY id DESC LIMIT ? OFFSET ?",
            [*params, page_size, max(page - 1, 0) * page_size],
        ).fetchall()
        return total, rows

    def audit_filter_options(self, tenant_id: str) -> tuple[list[str], list[str]]:
        """操作记录筛选下拉选项：本租户出现过的对象类型与操作者。"""
        entity_types = [row["entity_type"] for row in self.conn.execute(
            "SELECT DISTINCT entity_type FROM audit_log WHERE tenant_id=? ORDER BY entity_type", (tenant_id,)
        )]
        actors = [row["actor"] for row in self.conn.execute(
            "SELECT DISTINCT actor FROM audit_log WHERE tenant_id=? ORDER BY actor", (tenant_id,)
        )]
        return entity_types, actors

    # ── V1-10 分析导出：产品页事件埋点 ────────────────────────────────────────
    def log_product_event(self, tenant_id: str, product_id: str, event: str, site_key: str = "", conversation_id: str | None = None, detail: str = "") -> None:
        """记录产品页事件（page_view / download）。匿名事件无会话身份；有上下文时带会话键。"""
        self.conn.execute(
            "INSERT INTO product_events(tenant_id, product_id, event, site_key, conversation_id, detail, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (tenant_id, product_id, event, site_key, conversation_id, detail, now_iso()),
        )
        self.conn.commit()

    # ── V1-10 分析导出：资料补充任务（资料缺口 → 可分派的修订任务）───────────
    def create_material_task(self, tenant_id: str, product_id: str, source_ref: str, assignee: str, actor: str) -> str:
        product = self.get_product(tenant_id, product_id)
        if not product:
            raise ValueError("产品不存在或不属于当前企业")
        if source_ref:
            existing = self.conn.execute(
                "SELECT id FROM material_tasks WHERE tenant_id=? AND source_ref=? AND status='open'",
                (tenant_id, source_ref),
            ).fetchone()
            if existing:
                raise ValueError("该问题已登记资料补充任务")
        assignee = assignee.strip()
        if assignee:
            user = self.conn.execute(
                "SELECT id FROM users WHERE tenant_id=? AND email=? AND active=1", (tenant_id, assignee)
            ).fetchone()
            if not user:
                raise ValueError("接收人无效")
        question = ""
        if source_ref:
            row = self.conn.execute(
                "SELECT m.content FROM answers a JOIN messages m ON m.id=a.question_message_id WHERE a.tenant_id=? AND a.id=?",
                (tenant_id, source_ref),
            ).fetchone()
            question = (row["content"] if row else "")[:200]
        task_id = secrets.token_hex(12)
        timestamp = now_iso()
        with self.conn:
            self.conn.execute(
                "INSERT INTO material_tasks(id, tenant_id, product_id, title, source_question, source_ref, status, assignee, created_by, created_at) VALUES (?, ?, ?, ?, ?, ?, 'open', ?, ?, ?)",
                (task_id, tenant_id, product_id, f"资料补充 · {product['model']}", question, source_ref, assignee, actor, timestamp),
            )
            self._audit(tenant_id, "material_task", task_id, "created", actor, f"product={product['model']};assignee={assignee or '待分派'};ref={source_ref}")
        return task_id

    def complete_material_task(self, tenant_id: str, task_id: str, actor: str) -> None:
        task = self.conn.execute(
            "SELECT * FROM material_tasks WHERE tenant_id=? AND id=?", (tenant_id, task_id)
        ).fetchone()
        if not task:
            raise ValueError("任务不存在")
        if task["status"] == "done":
            return
        with self.conn:
            self.conn.execute(
                "UPDATE material_tasks SET status='done', completed_by=?, completed_at=? WHERE tenant_id=? AND id=?",
                (actor, now_iso(), tenant_id, task_id),
            )
            self._audit(tenant_id, "material_task", task_id, "completed", actor, task["title"])

    def list_material_tasks(self, tenant_id: str) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT t.*, p.model FROM material_tasks t LEFT JOIN products p ON p.id=t.product_id "
            "WHERE t.tenant_id=? ORDER BY CASE t.status WHEN 'open' THEN 0 ELSE 1 END, t.created_at DESC",
            (tenant_id,),
        ).fetchall()

    # ── V1-10 分析导出：报表聚合（只统计实际事件；明细可追溯）────────────────
    def analytics_snapshot(self, tenant_id: str, days: int = 0, exclude_demo: bool = False) -> dict[str, object]:
        """分析报表数据快照。定义：days=0 全部时间；exclude_demo=排除会话来源为演示夹具（demo-fixture）的记录。"""
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat() if days else None
        demo_clause = " AND c.source <> 'demo-fixture'" if exclude_demo else ""

        def time_and(alias: str) -> str:
            return f" AND {alias}.created_at >= ?" if cutoff else ""

        def time_params() -> list[object]:
            return [cutoff] if cutoff else []

        stages = self.get_lead_stages(tenant_id)
        stage_labels = {stage["key"]: stage["label"] for stage in stages}
        terminal_keys = {stage["key"] for stage in stages if stage["terminal"]}
        today = datetime.now(timezone.utc).date().isoformat()

        # 会话与来源
        conv_rows = self.conn.execute(
            f"SELECT c.id, c.source, c.locale, c.created_at FROM conversations c WHERE c.tenant_id=?{time_and('c')}{demo_clause} ORDER BY c.created_at DESC",
            [tenant_id, *time_params()],
        ).fetchall()
        by_source: dict[str, int] = {}
        for row in conv_rows:
            by_source[row["source"]] = by_source.get(row["source"], 0) + 1

        # RFQ
        rfq_rows_raw = self.conn.execute(
            f"SELECT r.id, r.status, r.created_at, r.current_revision, r.quantity, r.destination, r.requested_model, r.missing_json, r.snapshot_json, p.model, c.source AS conv_source, l.stage AS lead_stage "
            f"FROM rfqs r LEFT JOIN products p ON p.id=r.product_id LEFT JOIN conversations c ON c.id=r.conversation_id LEFT JOIN leads l ON l.rfq_id=r.id AND l.tenant_id=r.tenant_id "
            f"WHERE r.tenant_id=?{time_and('r')}{demo_clause} ORDER BY r.created_at DESC",
            [tenant_id, *time_params()],
        ).fetchall()
        rfq_rows: list[dict[str, object]] = []
        rfq_by_entry: dict[str, int] = {}
        rfq_missing_counter: dict[str, int] = {}
        rfq_submitted = 0
        rfq_needs_info = 0
        for row in rfq_rows_raw:
            try:
                entry = str(json.loads(row["snapshot_json"]).get("entry_surface") or "chat")
            except (json.JSONDecodeError, TypeError):
                entry = "chat"
            rfq_by_entry[entry] = rfq_by_entry.get(entry, 0) + 1
            if row["status"] == "needs_info":
                rfq_needs_info += 1
            else:
                rfq_submitted += 1
            try:
                missing = json.loads(row["missing_json"])
            except (json.JSONDecodeError, TypeError):
                missing = []
            for label in missing:
                rfq_missing_counter[label] = rfq_missing_counter.get(label, 0) + 1
            rfq_rows.append({
                "id": row["id"], "status": row["status"], "created_at": row["created_at"],
                "model": row["model"], "requested_model": row["requested_model"],
                "quantity": row["quantity"], "destination": row["destination"],
                "entry_surface": entry, "stage_label": stage_labels.get(row["lead_stage"], "—"),
                "missing": missing,
            })

        # 需求确认与推荐
        def status_counts(table: str, column: str = "status") -> dict[str, int]:
            rows = self.conn.execute(
                f"SELECT x.{column} AS status_key, COUNT(*) AS n FROM {table} x JOIN conversations c ON c.id=x.conversation_id "
                f"WHERE x.tenant_id=?{time_and('x')}{demo_clause} GROUP BY x.{column}",
                [tenant_id, *time_params()],
            ).fetchall()
            return {row["status_key"]: row["n"] for row in rows}

        requirement_counts = status_counts("requirement_revisions")
        recommendation_counts = status_counts("recommendations")
        answer_counts = status_counts("answers", "answer_status")
        task_counts = status_counts("human_tasks")

        # 线索与跟进时效
        lead_rows_raw = self.conn.execute(
            f"SELECT l.id, l.rfq_id, l.stage, l.owner, l.next_follow_up, l.created_at, c.stop_proactive_contact "
            f"FROM leads l LEFT JOIN conversations c ON c.id=l.conversation_id "
            f"WHERE l.tenant_id=?{time_and('l')}{demo_clause} ORDER BY l.created_at DESC",
            [tenant_id, *time_params()],
        ).fetchall()
        lead_rows: list[dict[str, object]] = []
        leads_active = 0
        leads_overdue = 0
        leads_due_today = 0
        quote_ready = 0
        stage_counter: dict[str, int] = {}
        for row in lead_rows_raw:
            stage_counter[row["stage"]] = stage_counter.get(row["stage"], 0) + 1
            if row["stage"] == "quote_ready":
                quote_ready += 1
            follow_up = row["next_follow_up"] or ""
            stop = bool(row["stop_proactive_contact"])
            overdue = bool(follow_up and follow_up < today and row["stage"] not in terminal_keys and not stop)
            if row["stage"] not in terminal_keys:
                leads_active += 1
            if not stop and follow_up == today and row["stage"] not in terminal_keys:
                leads_due_today += 1
            if overdue:
                leads_overdue += 1
            lead_rows.append({
                "id": row["id"], "rfq_id": row["rfq_id"], "stage_label": stage_labels.get(row["stage"], row["stage"]),
                "owner": row["owner"], "next_follow_up": follow_up, "stop": stop, "overdue": overdue,
                "created_at": row["created_at"],
            })

        # 资料缺口（needs_human 回答）与任务登记情况
        material_open_refs = {
            row["source_ref"]: row["id"] for row in self.conn.execute(
                "SELECT id, source_ref FROM material_tasks WHERE tenant_id=? AND status='open' AND source_ref <> ''",
                (tenant_id,),
            )
        }
        gap_rows_raw = self.conn.execute(
            f"SELECT a.id AS answer_id, a.product_id, a.created_at, m.content AS question, p.model, t.id AS task_id, t.status AS task_status "
            f"FROM answers a JOIN messages m ON m.id=a.question_message_id JOIN conversations c ON c.id=a.conversation_id "
            f"LEFT JOIN products p ON p.id=a.product_id LEFT JOIN human_tasks t ON t.question_message_id=a.question_message_id AND t.tenant_id=a.tenant_id "
            f"WHERE a.tenant_id=? AND a.answer_status='needs_human'{time_and('a')}{demo_clause} ORDER BY a.created_at DESC",
            [tenant_id, *time_params()],
        ).fetchall()
        gap_rows: list[dict[str, object]] = []
        gap_by_product: dict[str, int] = {}
        for row in gap_rows_raw:
            model = row["model"] or "未关联产品"
            gap_by_product[model] = gap_by_product.get(model, 0) + 1
            gap_rows.append({
                "answer_id": row["answer_id"], "product_id": row["product_id"] or "",
                "question": row["question"] or "", "model": row["model"] or "",
                "created_at": row["created_at"], "task_id": row["task_id"], "task_status": row["task_status"],
                "material_task_id": material_open_refs.get(row["answer_id"], ""),
            })
        top_question_rows = self.conn.execute(
            f"SELECT p.model, COUNT(*) AS n FROM answers a JOIN conversations c ON c.id=a.conversation_id "
            f"LEFT JOIN products p ON p.id=a.product_id WHERE a.tenant_id=?{time_and('a')}{demo_clause} GROUP BY a.product_id ORDER BY n DESC LIMIT 5",
            [tenant_id, *time_params()],
        ).fetchall()

        # 产品页事件（匿名事件不受演示排除开关影响；口径见页面说明）
        event_rows_raw = self.conn.execute(
            f"SELECT e.product_id, e.event, e.detail, e.created_at, p.model FROM product_events e "
            f"LEFT JOIN products p ON p.id=e.product_id WHERE e.tenant_id=?{time_and('e')} ORDER BY e.created_at DESC",
            [tenant_id, *time_params()],
        ).fetchall()
        event_counter: dict[str, int] = {}
        event_by_product: dict[str, dict[str, int]] = {}
        event_rows: list[dict[str, object]] = []
        for row in event_rows_raw:
            event_counter[row["event"]] = event_counter.get(row["event"], 0) + 1
            bucket = event_by_product.setdefault(row["model"] or row["product_id"], {"page_view": 0, "download": 0})
            if row["event"] in bucket:
                bucket[row["event"]] += 1
            event_rows.append({
                "product_id": row["product_id"], "model": row["model"], "event": row["event"],
                "detail": row["detail"], "created_at": row["created_at"],
            })
        event_products = sorted(
            ((model, counts["page_view"], counts["download"]) for model, counts in event_by_product.items()),
            key=lambda item: (item[1] + item[2]), reverse=True,
        )

        material_tasks = self.list_material_tasks(tenant_id)
        return {
            "conversations": {"total": len(conv_rows), "by_source": sorted(by_source.items(), key=lambda item: item[1], reverse=True),
                              "rows": [dict(row) for row in conv_rows]},
            "rfqs": {"total": len(rfq_rows), "submitted": rfq_submitted, "needs_info": rfq_needs_info,
                     "by_entry": sorted(rfq_by_entry.items(), key=lambda item: item[1], reverse=True),
                     "missing_top": sorted(rfq_missing_counter.items(), key=lambda item: item[1], reverse=True)[:6],
                     "rows": rfq_rows},
            "requirements": {"confirmed": requirement_counts.get("confirmed", 0), "candidate": requirement_counts.get("candidate", 0)},
            "recommendations": {"total": sum(recommendation_counts.values()),
                                "by_status": sorted(recommendation_counts.items(), key=lambda item: item[1], reverse=True)},
            "answers": {"grounded": answer_counts.get("grounded", 0), "needs_human": answer_counts.get("needs_human", 0)},
            "tasks": {"total": sum(task_counts.values()),
                      "by_status": sorted(task_counts.items(), key=lambda item: item[1], reverse=True)},
            "leads": {"total": len(lead_rows), "active": leads_active, "overdue": leads_overdue, "due_today": leads_due_today,
                      "by_stage": [(stage["key"], stage["label"], stage_counter.get(stage["key"], 0)) for stage in stages],
                      "rows": lead_rows},
            "quote_ready": quote_ready,
            "quote_ready_label": stage_labels.get("quote_ready", "可进入报价"),
            "gaps": {"rows": gap_rows, "by_product": sorted(gap_by_product.items(), key=lambda item: item[1], reverse=True)},
            "top_questions": [(row["model"] or "未关联产品", row["n"]) for row in top_question_rows],
            "events": {"page_view": event_counter.get("page_view", 0), "download": event_counter.get("download", 0),
                       "total": len(event_rows), "by_product": event_products, "rows": event_rows},
            "material_tasks": {"open": [dict(row) for row in material_tasks if row["status"] == "open"],
                               "done": [dict(row) for row in material_tasks if row["status"] == "done"]},
        }

    # ── V1-10 分析导出：CSV 导出数据行 ────────────────────────────────────────
    def export_rows(self, tenant_id: str, dataset: str) -> tuple[list[str], list[list[str]]]:
        if dataset == "products":
            headers = ["model", "name", "status", "latest_version", "created_at", "updated_at"]
            rows = [
                [str(row["model"]), str(row["name"]), str(row["status"]), str(row["latest_version"] or ""), str(row["created_at"]), str(row["updated_at"])]
                for row in self.conn.execute(
                    "SELECT p.model, p.name, p.status, p.created_at, p.updated_at, (SELECT MAX(version) FROM product_versions v WHERE v.product_id=p.id) AS latest_version FROM products p WHERE p.tenant_id=? ORDER BY p.created_at",
                    (tenant_id,),
                )
            ]
            return headers, rows
        if dataset == "rfqs":
            headers = ["rfq_id", "created_at", "status", "revision", "model", "requested_model", "quantity", "destination", "requested_delivery", "contact_name", "company", "contact_email", "contact_phone", "notes", "lead_stage", "lead_owner", "next_follow_up", "conversation_source"]
            rows = [
                [str(row["id"]), str(row["created_at"]), str(row["status"]), str(row["current_revision"]), str(row["model"] or ""), str(row["requested_model"] or ""),
                 str(row["quantity"] or ""), str(row["destination"] or ""), str(row["requested_delivery"] or ""), str(row["contact_name"] or ""), str(row["company"] or ""),
                 str(row["contact_email"] or ""), str(row["contact_phone"] or ""), str(row["notes"] or ""), str(row["stage"] or ""), str(row["owner"] or ""),
                 str(row["next_follow_up"] or ""), str(row["conv_source"] or "")]
                for row in self.conn.execute(
                    "SELECT r.*, p.model, l.stage, l.owner, l.next_follow_up, c.source AS conv_source FROM rfqs r "
                    "LEFT JOIN products p ON p.id=r.product_id LEFT JOIN leads l ON l.rfq_id=r.id AND l.tenant_id=r.tenant_id "
                    "LEFT JOIN conversations c ON c.id=r.conversation_id WHERE r.tenant_id=? ORDER BY r.created_at",
                    (tenant_id,),
                )
            ]
            return headers, rows
        if dataset == "leads":
            headers = ["lead_id", "rfq_id", "created_at", "stage", "owner", "next_follow_up", "business_note", "overdue", "stop_proactive_contact", "conversation_source", "company", "contact_email"]
            stages = self.get_lead_stages(tenant_id)
            terminal_keys = {stage["key"] for stage in stages if stage["terminal"]}
            today = datetime.now(timezone.utc).date().isoformat()
            rows = []
            for row in self.conn.execute(
                "SELECT l.id, l.rfq_id, l.stage, l.owner, l.next_follow_up, l.note, l.created_at, c.source AS conv_source, c.stop_proactive_contact, r.company, r.contact_email "
                "FROM leads l LEFT JOIN conversations c ON c.id=l.conversation_id LEFT JOIN rfqs r ON r.id=l.rfq_id AND r.tenant_id=l.tenant_id "
                "WHERE l.tenant_id=? ORDER BY l.created_at",
                (tenant_id,),
            ):
                overdue = bool(row["next_follow_up"] and row["next_follow_up"] < today and row["stage"] not in terminal_keys and not row["stop_proactive_contact"])
                rows.append([str(row["id"]), str(row["rfq_id"]), str(row["created_at"]), str(row["stage"]), str(row["owner"] or ""),
                             str(row["next_follow_up"] or ""), str(row["note"] or ""), "1" if overdue else "0",
                             "1" if row["stop_proactive_contact"] else "0", str(row["conv_source"] or ""), str(row["company"] or ""), str(row["contact_email"] or "")])
            return headers, rows
        raise ValueError("不支持的数据集")

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

    def set_source_usage(self, tenant_id: str, product_id: str, source_id: str, usage: str, actor: str = "demo-admin") -> dict[str, bool]:
        """V1-02 附件三级可见性：在「仅内部（默认）」与「禁止使用」之间切换。

        与归属修订同理：任何真实变更都推进 ``scope_revision``，旧快照的捕获值因此失配，
        不会再对外提供该文件（重新公开需重新确认许可并发布新版本）。标记「禁止使用」
        时立即撤销公开下载许可；恢复「仅内部」不自动恢复许可。
        """
        if usage not in SOURCE_USAGE_LEVELS:
            raise ValueError("文件可见性层级无效")
        source = self.conn.execute(
            "SELECT * FROM source_documents WHERE tenant_id=? AND product_id=? AND id=?",
            (tenant_id, product_id, source_id),
        ).fetchone()
        if not source:
            raise ValueError("来源文件不属于当前产品")
        previous = source["usage"] or "internal"
        if usage == previous:
            return {"changed": False, "public_permission_revoked": False}
        public_ids = self.public_source_ids(tenant_id, product_id)
        public_permission_revoked = False
        if usage == "restricted" and source_id in public_ids:
            self.conn.execute(
                "DELETE FROM public_source_permissions WHERE tenant_id=? AND product_id=? AND source_id=?",
                (tenant_id, product_id, source_id),
            )
            public_permission_revoked = True
            self._audit(
                tenant_id, "source_document", source_id, "public_permission_revoked_on_usage_change",
                actor, f"{previous} -> {usage}",
            )
        new_revision = int(source["scope_revision"] or 0) + 1
        self.conn.execute(
            "UPDATE source_documents SET usage=?, scope_revision=? WHERE id=?",
            (usage, new_revision, source_id),
        )
        self._audit(tenant_id, "source_document", source_id, "usage_changed", actor, f"{previous} -> {usage} (可见性修订 r{new_revision})")
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

    def set_category_candidate(self, tenant_id: str, product_id: str, value: str, source: str, actor: str = "demo-admin") -> None:
        """登记/替换品类候选（资料建议 / 模型建议 / 人工录入）。候选不等于已核准品类。"""
        if not self.get_product(tenant_id, product_id):
            raise ValueError("产品不存在")
        value = value.strip()
        if not value:
            raise ValueError("候选品类不能为空")
        if len(value) > CATEGORY_MAX_LENGTH:
            raise ValueError(f"候选品类过长（最多 {CATEGORY_MAX_LENGTH} 字）")
        if source not in CATEGORY_SOURCE_LABELS:
            raise ValueError("候选来源无效")
        timestamp = now_iso()
        self.conn.execute(
            "UPDATE products SET category_candidate=?, category_candidate_source=?, category_candidate_at=?, updated_at=? WHERE tenant_id=? AND id=?",
            (value, source, timestamp, timestamp, tenant_id, product_id),
        )
        self._audit(tenant_id, "product", product_id, "category_candidate_set", actor, f"候选={value}；来源={CATEGORY_SOURCE_LABELS[source]}")
        self.conn.commit()

    def approve_category(self, tenant_id: str, product_id: str, actor: str = "demo-admin") -> str:
        """授权成员核准品类：候选 → 正式品类；审计记录 before→after，候选清空。"""
        product = self.get_product(tenant_id, product_id)
        if not product:
            raise ValueError("产品不存在")
        candidate = (product["category_candidate"] or "").strip()
        if not candidate:
            raise ValueError("没有待核准的品类候选")
        previous = product["category"] or ""
        timestamp = now_iso()
        self.conn.execute(
            "UPDATE products SET category=?, category_approved_by=?, category_approved_at=?, category_candidate='', category_candidate_source='', category_candidate_at=NULL, updated_at=? WHERE tenant_id=? AND id=?",
            (candidate, actor, timestamp, timestamp, tenant_id, product_id),
        )
        self._audit(tenant_id, "product", product_id, "category_approved", actor, f"{previous or '（未设置）'} -> {candidate}")
        self.conn.commit()
        return candidate

    def clear_category_candidate(self, tenant_id: str, product_id: str, actor: str = "demo-admin") -> None:
        product = self.get_product(tenant_id, product_id)
        if not product:
            raise ValueError("产品不存在")
        if not (product["category_candidate"] or "").strip():
            raise ValueError("没有待处理的品类候选")
        self.conn.execute(
            "UPDATE products SET category_candidate='', category_candidate_source='', category_candidate_at=NULL, updated_at=? WHERE tenant_id=? AND id=?",
            (now_iso(), tenant_id, product_id),
        )
        self._audit(tenant_id, "product", product_id, "category_candidate_cleared", actor, product["category_candidate"] or "")
        self.conn.commit()

    def get_approved_category(self, tenant_id: str, product_id: str) -> str:
        """只返回已核准品类（空串＝尚未核准）；候选值绝不从这里流出。"""
        product = self.get_product(tenant_id, product_id)
        return (product["category"] or "").strip() if product else ""

    # ── V1-03 创意中心 · 产品展示页（企业展示页模板） ─────────────────────
    def list_templates(self, tenant_id: str) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM templates WHERE tenant_id=? ORDER BY updated_at DESC, id", (tenant_id,)
        ).fetchall()

    def get_template(self, tenant_id: str, template_id: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM templates WHERE tenant_id=? AND id=?", (tenant_id, template_id)
        ).fetchone()

    def get_template_for_category(self, tenant_id: str, category: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM templates WHERE tenant_id=? AND category=?", (tenant_id, category)
        ).fetchone()

    def create_template(self, tenant_id: str, category: str, actor: str, *, blank: bool = False) -> str:
        """按品类创建展示页（每类一份）：同品类已存在时报错，由界面改为直接打开。"""
        category = category.strip()
        if not category:
            raise ValueError("请先选择产品并确认品类")
        if self.get_template_for_category(tenant_id, category):
            raise ValueError("该品类已有展示页")
        draft = blank_template_draft() if blank else standard_template_draft()
        template_id = secrets.token_hex(12)
        now = now_iso()
        try:
            self.conn.execute(
                "INSERT INTO templates(id, tenant_id, category, name, status, draft_json, draft_updated_by, draft_updated_at, created_by, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, 'draft', ?, ?, ?, ?, ?, ?)",
                (template_id, tenant_id, category, f"{category} · 产品展示页", json.dumps(draft, ensure_ascii=False), actor, now, actor, now, now),
            )
        except sqlite3.IntegrityError:
            self.conn.rollback()
            raise ValueError("该品类已有展示页")
        self._audit(tenant_id, "template", template_id, "template_created", actor, f"category={category}")
        self.conn.commit()
        return template_id

    def save_template_draft(self, tenant_id: str, template_id: str, draft: dict, actor: str, detail: str = "") -> None:
        """保存展示页改动（自动保存入口）；只影响草稿，不触碰线上。"""
        if not self.get_template(tenant_id, template_id):
            raise ValueError("展示页不存在")
        now = now_iso()
        self.conn.execute(
            "UPDATE templates SET draft_json=?, draft_updated_by=?, draft_updated_at=?, updated_at=? WHERE tenant_id=? AND id=?",
            (json.dumps(draft, ensure_ascii=False), actor, now, now, tenant_id, template_id),
        )
        self._audit(tenant_id, "template", template_id, "template_draft_saved", actor, detail)
        self.conn.commit()

    def restore_template_defaults(self, tenant_id: str, template_id: str, actor: str = "demo-admin") -> None:
        """恢复默认：草稿的参数与板块重置为平台标准推荐组合（已发布版本不受影响）。"""
        if not self.get_template(tenant_id, template_id):
            raise ValueError("展示页不存在")
        now = now_iso()
        draft = standard_template_draft()
        self.conn.execute(
            "UPDATE templates SET draft_json=?, draft_updated_by=?, draft_updated_at=?, updated_at=? WHERE tenant_id=? AND id=?",
            (json.dumps(draft, ensure_ascii=False), actor, now, now, tenant_id, template_id),
        )
        self._audit(tenant_id, "template", template_id, "template_restored_defaults", actor, "")
        self.conn.commit()

    def list_template_versions(self, tenant_id: str, template_id: str) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM template_versions WHERE tenant_id=? AND template_id=? ORDER BY version DESC", (tenant_id, template_id)
        ).fetchall()

    def get_template_version(self, tenant_id: str, version_id: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM template_versions WHERE tenant_id=? AND id=?", (tenant_id, version_id)
        ).fetchone()

    def template_has_unpublished_changes(self, tenant_id: str, template: sqlite3.Row) -> bool:
        """草稿与最近上线版本是否不一致（还没有上线版本＝有未发布内容）。"""
        if not template["published_version"]:
            return True
        row = self.conn.execute(
            "SELECT content_json FROM template_versions WHERE tenant_id=? AND template_id=? AND version=?",
            (tenant_id, template["id"], template["published_version"]),
        ).fetchone()
        if not row:
            return True
        return (template["draft_json"] or "") != (row["content_json"] or "")

    def submit_template(self, tenant_id: str, template_id: str, actor: str = "demo-admin") -> None:
        """员工提交给管理员确认（不改变任何对外内容）。"""
        if not self.get_template(tenant_id, template_id):
            raise ValueError("展示页不存在")
        self.conn.execute(
            "UPDATE templates SET status='submitted', updated_at=? WHERE tenant_id=? AND id=?",
            (now_iso(), tenant_id, template_id),
        )
        self._audit(tenant_id, "template", template_id, "template_submitted", actor, "")
        self.conn.commit()

    def publish_template_version(self, tenant_id: str, template_id: str, actor: str = "demo-admin") -> int:
        """确认上线：把当前草稿冻结为下一个版本（仅管理员；界面层已做权限拦截）。"""
        template = self.get_template(tenant_id, template_id)
        if not template:
            raise ValueError("展示页不存在")
        version = (template["published_version"] or 0) + 1
        now = now_iso()
        self.conn.execute(
            "INSERT INTO template_versions(id, tenant_id, template_id, version, content_json, status, approved_by, approved_at, created_by, created_at) "
            "VALUES (?, ?, ?, ?, ?, 'published', ?, ?, ?, ?)",
            (secrets.token_hex(12), tenant_id, template_id, version, template["draft_json"], actor, now, actor, now),
        )
        self.conn.execute(
            "UPDATE templates SET published_version=?, status='published', updated_at=? WHERE tenant_id=? AND id=?",
            (version, now, tenant_id, template_id),
        )
        self._audit(tenant_id, "template", template_id, "template_published", actor, f"version={version}")
        self.conn.commit()
        return version

    def withdraw_template(self, tenant_id: str, template_id: str, actor: str = "demo-admin") -> None:
        """撤回＝停止新引用：之后产品重新发布不再引用此展示页；已发布版本不受影响。"""
        if not self.get_template(tenant_id, template_id):
            raise ValueError("展示页不存在")
        self.conn.execute(
            "UPDATE templates SET status='withdrawn', updated_at=? WHERE tenant_id=? AND id=?",
            (now_iso(), tenant_id, template_id),
        )
        self._audit(tenant_id, "template", template_id, "template_withdrawn", actor, "")
        self.conn.commit()

    def resolve_template_for_category(self, tenant_id: str, category: str) -> sqlite3.Row | None:
        """该品类当前可被产品发布引用的展示页版本行（已上线且未撤回）；无则 None。"""
        category = (category or "").strip()
        if not category:
            return None
        template = self.get_template_for_category(tenant_id, category)
        if not template or template["status"] != "published" or not template["published_version"]:
            return None
        return self.conn.execute(
            "SELECT * FROM template_versions WHERE tenant_id=? AND template_id=? AND version=?",
            (tenant_id, template["id"], template["published_version"]),
        ).fetchone()

    def creative_home_rows(self, tenant_id: str) -> dict:
        """创意中心首页数据：按品类聚合（覆盖产品 + 对应展示页），并统计未核准品类的产品数。"""
        products = self.conn.execute(
            "SELECT id, model, name, category FROM products WHERE tenant_id=? ORDER BY model", (tenant_id,)
        ).fetchall()
        templates = {row["category"]: row for row in self.list_templates(tenant_id)}
        grouped: dict[str, list[sqlite3.Row]] = {}
        uncategorized = 0
        for product in products:
            category = (product["category"] or "").strip()
            if category:
                grouped.setdefault(category, []).append(product)
            else:
                uncategorized += 1
        rows = [
            {"category": category, "products": items, "template": templates.pop(category, None)}
            for category, items in grouped.items()
        ]
        # 极少数情况：展示页所属品类下已没有产品（如产品改类后）——仍然列出，避免出现“看不见的展示页”。
        for category, template in templates.items():
            rows.append({"category": category, "products": [], "template": template})
        with_tpl = sorted(
            (row for row in rows if row["template"]), key=lambda row: row["template"]["updated_at"] or "", reverse=True
        )
        without = sorted((row for row in rows if not row["template"]), key=lambda row: row["category"])
        return {"rows": with_tpl + without, "uncategorized": uncategorized, "total_products": len(products)}

    def publish_product(self, tenant_id: str, product_id: str, actor: str = "demo-admin") -> int:
        product = self.get_product(tenant_id, product_id)
        if not product:
            raise ValueError("产品不存在")
        if product["status"] not in ("approved", "published"):
            raise ValueError("只有已核准产品才能发布")
        fields = self.get_fields(tenant_id, product_id)
        approved_fields = [field for field in fields if field["state"] == "approved" and field["value"].strip() and field["source_document_id"] and field["source_location"].strip()]
        # V1-02：来源被标记「禁止使用」的已核准事实不得进入新发布版本（旧版本快照不受影响）。
        restricted_ids = {
            source["id"] for source in self.get_sources(tenant_id, product_id)
            if (source["usage"] or "internal") == "restricted"
        }
        usage_blocked = [field for field in approved_fields if field["source_document_id"] in restricted_ids]
        approved_fields = [field for field in approved_fields if field["source_document_id"] not in restricted_ids]
        if not approved_fields:
            raise ValueError("没有可公开的已核准产品事实" if not usage_blocked else "已核准字段的来源均被禁止使用，无法发布")
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
            and (source["usage"] or "internal") != "restricted"
        ]
        # V1-03：产品发布时按「已核准品类」引用当前已上线的展示页版本（发布即冻结；
        # 无引用＝平台标准模板；撤回＝停止新引用）。
        template_version = self.resolve_template_for_category(tenant_id, self.get_approved_category(tenant_id, product_id))
        snapshot = {
            "product": {"model": product["model"], "name": product["name"], "use_case": product["use_case"], "slug": product["slug"], "category": (product["category"] or "")},
            "fields": [dict(field) for field in approved_fields],
            "missing_fields": missing,
            "public_sources": public_files,
            "published_at": now_iso(),
        }
        if template_version:
            snapshot["template"] = {
                "template_id": template_version["template_id"],
                "template_version_id": template_version["id"],
                "version": template_version["version"],
            }
        if previous:
            self.conn.execute("UPDATE product_versions SET status='withdrawn' WHERE id=?", (previous["id"],))
        self.conn.execute(
            "INSERT INTO product_versions(id, tenant_id, product_id, version, snapshot_json, status, created_by, created_at, published_at) VALUES (?, ?, ?, ?, ?, 'published', ?, ?, ?)",
            (secrets.token_hex(12), tenant_id, product_id, version, json.dumps(snapshot, ensure_ascii=False), actor, now_iso(), snapshot["published_at"]),
        )
        self.conn.execute("UPDATE products SET status='published', updated_at=? WHERE tenant_id=? AND id=?", (now_iso(), tenant_id, product_id))
        self._audit(tenant_id, "product", product_id, "published", actor, f"version={version}; usage_blocked={len(usage_blocked)}")
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
                    product_field = product_fields.get(product_key, {})
                    product_range = range_from_text(product_field.get("value", ""))
                    product_value = str(product_field.get("value", "")).strip()
                    product_unit = str(product_field.get("unit", "")).strip()
                    # 候选说明带上该型号自己的范围，多候选并列时差异可直接比较。
                    product_note = f"（产品范围 {product_value}{(' ' + product_unit) if product_unit else ''}）"
                    if required_number is None:
                        unknown.append(f"买方尚未确认{label}")
                    elif product_range is None:
                        unknown.append(f"{label}规则无法解析")
                    elif product_range[0] <= required_number <= product_range[1]:
                        satisfies.append(f"{label} {required_number:g}{unit} 在产品范围内{product_note}")
                    else:
                        conflicts.append(f"{label} {required_number:g}{unit} 超出产品范围{product_note}")

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


ANSWER_FIELD_KEYS = ("flow_range", "head_range", "media", "material", "power", "temperature", "performance_points")

# 字段答案模板：两种问答模式共用；数值与单位始终取自当前已发布快照的该字段。
ANSWER_FIELD_TEMPLATES = {
    "flow_range": "该型号的流量范围是 {value}{unit}。",
    "head_range": "该型号的扬程范围是 {value}{unit}。",
    "media": "资料标注的介质适用条件是：{value}{unit}。",
    "material": "该型号的材质是：{value}{unit}。",
    "power": "该型号的功率是 {value}{unit}。",
    "temperature": "资料标注的介质温度范围是 {value}{unit}。",
    "performance_points": "资料中的目录性能点（流量:扬程）是：{value}{unit}。最终工作点需工程师确认。",
}
# 规则模式的提问关键词 → 字段；deepseek 模式下由模型做同样的判断，并用同一张表做
# 字段冲突防护（模型选出的字段与提问明确字段冲突时不按该字段作答，见 guard_answer_field_selection）。
ANSWER_QUESTION_TOKENS = (
    (("flow", "流量"), "flow_range"),
    (("head", "扬程"), "head_range"),
    (("media", "介质", "medium", "液体"), "media"),
    (("material", "材质", "材料"), "material"),
    (("power", "功率"), "power"),
    (("temperature", "温度", "temperature"), "temperature"),
    (("性能", "曲线", "performance", "catalogue"), "performance_points"),
)


def _answer_question_token_pattern(tokens: tuple) -> re.Pattern:
    parts = []
    for token in tokens:
        escaped = re.escape(token)
        # 英文关键词按词边界匹配（避免 "head" 误中 "overhead"/"header"）；中文无词边界，按子串匹配。
        parts.append(rf"\b{escaped}\b" if token.isascii() else escaped)
    return re.compile("|".join(parts))


_ANSWER_QUESTION_FIELD_PATTERNS = tuple(
    (key, _answer_question_token_pattern(tokens)) for tokens, key in ANSWER_QUESTION_TOKENS
)


def explicit_answer_fields(question: str) -> list[str]:
    """提问中明确可识别的泵参数字段（与规则模式同一张关键词表；英文按词边界）。

    只用于判断「模型选出的字段是否与买家明确问到的字段冲突」；没有明确字段的问题
    返回空列表（不做任何推断，维持模型选择）。
    """
    text = str(question or "").lower()
    return [key for key, pattern in _ANSWER_QUESTION_FIELD_PATTERNS if pattern.search(text)]


def guard_answer_field_selection(question: str, selected: str | None, available_keys: set) -> str | None:
    """最小防护：模型选出的字段与提问明确字段冲突时，不按该字段作答。

    - 模型给出 none：维持原有语义（转待核实），不据此追加回答；
    - 问题没有明确字段、或模型选择正是明确字段之一：维持模型选择；
    - 冲突时：仅当提问只含一个明确字段且该字段已发布时，确定性路由到该字段
      （与规则模式同一张表），其余情形转待核实——绝不答非所问。
    """
    if selected is None:
        return None
    explicit = explicit_answer_fields(question)
    if not explicit or selected in explicit:
        return selected
    if len(explicit) == 1 and explicit[0] in available_keys:
        return explicit[0]
    return None


RESTRICTED_SOURCE_ANSWER_REASON = "来源文件已被标记禁止使用；按现有规则不对外引用"


def diff_product_snapshots(old_snapshot: dict, new_snapshot: dict) -> dict:
    """逐项对比两份发布快照（V1-02 版本对比）。

    返回字段值/单位变化、公开文件增删、缺项增补/消除、品类变化；供版本历史展示，
    不改变任何数据。旧快照允许缺新字段（历史版本），缺失一律按空值参与比较。
    """
    old_fields = {field["field_key"]: field for field in (old_snapshot or {}).get("fields", [])}
    new_fields = {field["field_key"]: field for field in (new_snapshot or {}).get("fields", [])}
    changes = []
    for key in sorted(set(old_fields) | set(new_fields)):
        old_field = old_fields.get(key)
        new_field = new_fields.get(key)
        old_text = f"{old_field['value']} {old_field['unit']}".strip() if old_field else ""
        new_text = f"{new_field['value']} {new_field['unit']}".strip() if new_field else ""
        if old_text == new_text:
            continue
        if old_field is None:
            status = "added"
        elif new_field is None:
            status = "removed"
        else:
            status = "changed"
        changes.append(
            {
                "field_key": key,
                "label": (new_field or old_field).get("label", key),
                "status": status,
                "old": old_text,
                "new": new_text,
            }
        )
    old_files = {entry["filename"] for entry in (old_snapshot or {}).get("public_sources", [])}
    new_files = {entry["filename"] for entry in (new_snapshot or {}).get("public_sources", [])}
    old_missing = set((old_snapshot or {}).get("missing_fields", []))
    new_missing = set((new_snapshot or {}).get("missing_fields", []))
    old_category = ((old_snapshot or {}).get("product", {}) or {}).get("category", "")
    new_category = ((new_snapshot or {}).get("product", {}) or {}).get("category", "")
    return {
        "changes": changes,
        "files_added": sorted(new_files - old_files),
        "files_removed": sorted(old_files - new_files),
        "missing_added": sorted(new_missing - old_missing),
        "missing_resolved": sorted(old_missing - new_missing),
        "category_old": old_category,
        "category_new": new_category,
        "category_changed": old_category != new_category,
    }


def source_is_restricted(db: Database, tenant_id: str, field: dict) -> bool:
    """该字段的来源文件是否已被标记「禁止使用」（据此后新回答不得引用其内容）。"""
    source_id = field.get("source_document_id")
    if not source_id:
        return False
    row = db.conn.execute(
        "SELECT usage FROM source_documents WHERE tenant_id=? AND id=?", (tenant_id, source_id)
    ).fetchone()
    return bool(row) and (row["usage"] or "internal") == "restricted"


def build_record_answer(
    db: Database,
    tenant_id: str,
    field_key: str,
    field: dict,
    version: sqlite3.Row,
) -> tuple[str, dict[str, str]]:
    """从当前已发布快照的字段构造答案文本与引用（规则与模型两种模式共用）。

    模型只能选择字段；数值、单位、答案文本和引用都在这里生成，模型输出不进入答案。
    """
    citation = {
        "filename": field.get("source_filename") or "产品核准资料",
        "location": field.get("source_location") or "字段核准记录",
        "product_version": str(version["version"]),
        "field": field.get("label", field_key),
    }
    source = db.conn.execute(
        "SELECT filename, source_location FROM source_documents WHERE tenant_id=? AND id=?",
        (tenant_id, field.get("source_document_id")),
    ).fetchone()
    if source:
        citation["filename"] = source["filename"]
        citation["location"] = field.get("source_location") or source["source_location"]
    answer = ANSWER_FIELD_TEMPLATES[field_key].format(value=field.get("value", ""), unit=field.get("unit", ""))
    return answer, citation


def answer_from_approved_material(
    db: Database,
    tenant_id: str,
    question: str,
    product_id: str | None,
) -> tuple[str, str, str | None, str | None, list[dict[str, str]], str]:
    """Answer only from the selected published snapshot; otherwise hand off.（规则模式）"""
    question_lower = question.lower()
    if any(token in question_lower for token in COMMERCIAL_QUESTION_TOKENS):
        return "needs_human", COMMERCIAL_REFUSAL_ANSWER, product_id, None, [], "资料不包含商业承诺或认证判断"

    product = db.get_product(tenant_id, product_id) if product_id else None
    version = db.get_published_version(tenant_id, product_id) if product_id else None
    if not product or not version:
        return "needs_human", NO_PUBLISHED_PRODUCT_ANSWER, product_id, None, [], "没有可引用的已发布产品版本"

    snapshot = json.loads(version["snapshot_json"])
    fields = {field["field_key"]: field for field in snapshot.get("fields", [])}
    selected_key = None
    for tokens, key in ANSWER_QUESTION_TOKENS:
        if any(token in question_lower for token in tokens):
            selected_key = key
            break
    if not selected_key or not fields.get(selected_key) or not str(fields[selected_key].get("value", "")).strip():
        return "needs_human", NO_APPROVED_BASIS_ANSWER, product_id, version["id"], [], "资料缺少对应字段或问题需要工程判断"
    if source_is_restricted(db, tenant_id, fields[selected_key]):
        return "needs_human", NO_APPROVED_BASIS_ANSWER, product_id, version["id"], [], RESTRICTED_SOURCE_ANSWER_REASON

    answer, citation = build_record_answer(db, tenant_id, selected_key, fields[selected_key], version)
    return "grounded", answer, product_id, version["id"], [citation], ""


def log_ai_call(
    db: Database,
    tenant_id: str,
    conversation_id: str | None,
    task: str,
    ai,
    meta: dict | None = None,
    error: AIUnavailableError | None = None,
) -> None:
    """把一次模型调用结果写入内部日志（提供方/模型/状态/耗时/用量；失败给处理建议）。"""
    if ai is None or ai.mode != ai_client.MODE_DEEPSEEK:
        return
    if error is not None:
        status, action = error.kind, error.action
        http_status, duration_ms = error.http_status, error.duration_ms
        prompt_tokens = completion_tokens = None
    else:
        meta = meta or {}
        status, action = ai_client.STATUS_OK, ai_client.ACTION_NONE
        http_status, duration_ms = meta.get("http_status"), meta.get("duration_ms")
        prompt_tokens, completion_tokens = meta.get("prompt_tokens"), meta.get("completion_tokens")
    db.record_ai_call(
        tenant_id, conversation_id, task, ai.config.provider, ai.config.model,
        status, action, http_status, duration_ms, prompt_tokens, completion_tokens,
    )


def extract_requirement_candidates(
    db: Database,
    ai,
    tenant_id: str,
    conversation_id: str,
    text: str,
    explicit: dict[str, str] | None = None,
    previous: dict[str, dict[str, str]] | None = None,
) -> dict[str, dict[str, str]]:
    """M3 提取入口：规则模式保持本地规则可复现；deepseek 模式由模型给出候选值。

    模型候选经白名单字段、单位与结构校验后并入现有需求卡（state 仍为 candidate），
    是否采信由买方在卡片里逐项确认；模型失败（超时/限流/服务错误）向上抛出，由调用方
    返回可重试的失败状态——不静默落回规则结果，也不假装已识别。
    """
    if ai is None or ai.mode != ai_client.MODE_DEEPSEEK:
        return extract_requirement_values(text, explicit or {}, previous)
    # 表单显式填写的字段按原有规则保留；模型只补充/覆盖来自自由文本的候选。
    values = extract_requirement_values("", dict(explicit or {}), previous)
    try:
        candidates, meta = ai.extract_requirements(text)
    except AIUnavailableError as error:
        log_ai_call(db, tenant_id, conversation_id, AI_TASK_EXTRACTION, ai, error=error)
        raise
    log_ai_call(db, tenant_id, conversation_id, AI_TASK_EXTRACTION, ai, meta=meta)
    for key, item in candidates.items():
        values[key] = {
            "value": item["value"],
            "unit": item["unit"],
            "state": "candidate",
            "source": "model_candidate",
            "label": REQUIREMENT_LABELS[key],
        }
    return values


def answer_question_with_mode(
    db: Database,
    ai,
    tenant_id: str,
    conversation_id: str,
    question: str,
    product_id: str | None,
) -> tuple[str, str, str | None, str | None, list[dict[str, str]], str]:
    """M5 问答入口：规则模式沿用本地规则；deepseek 模式模型只选择字段。

    最终数值、答案文本与引用必须由后端从该产品的已发布快照构造（build_record_answer）；
    模型给出 none 或其选择不在已核准、已发布字段内时按「没有可用依据」转人工核实。
    模型字段与提问的明确字段冲突时（如买家问扬程、模型选流量），先经
    guard_answer_field_selection：只有一个明确字段且已发布则按该字段回答，否则转待核实，
    绝不把冲突字段作为已答输出。
    模型超时/限流等失败向上抛出：不保存假答案、不创建人工任务，重试后可继续。
    """
    if ai is None or ai.mode != ai_client.MODE_DEEPSEEK:
        return answer_from_approved_material(db, tenant_id, question, product_id)
    question_lower = question.lower()
    if any(token in question_lower for token in COMMERCIAL_QUESTION_TOKENS):
        return "needs_human", COMMERCIAL_REFUSAL_ANSWER, product_id, None, [], "资料不包含商业承诺或认证判断"
    product = db.get_product(tenant_id, product_id) if product_id else None
    version = db.get_published_version(tenant_id, product_id) if product_id else None
    if not product or not version:
        return "needs_human", NO_PUBLISHED_PRODUCT_ANSWER, product_id, None, [], "没有可引用的已发布产品版本"
    snapshot = json.loads(version["snapshot_json"])
    fields = {field["field_key"]: field for field in snapshot.get("fields", [])}
    allowed = [
        {"key": key, "label_zh": FIELD_LABELS.get(key, key), "label_en": FIELD_LABELS_EN.get(key, key)}
        for key in ANSWER_FIELD_KEYS
        if fields.get(key) and str(fields[key].get("value", "")).strip()
    ]
    try:
        selected, meta = ai.select_answer_field(question, allowed)
    except AIUnavailableError as error:
        log_ai_call(db, tenant_id, conversation_id, AI_TASK_ANSWER, ai, error=error)
        raise
    log_ai_call(db, tenant_id, conversation_id, AI_TASK_ANSWER, ai, meta=meta)
    selected = guard_answer_field_selection(question, selected, {item["key"] for item in allowed})
    if not selected or not fields.get(selected) or not str(fields[selected].get("value", "")).strip():
        return "needs_human", NO_APPROVED_BASIS_ANSWER, product_id, version["id"], [], "资料缺少对应字段或问题需要工程判断"
    if source_is_restricted(db, tenant_id, fields[selected]):
        return "needs_human", NO_APPROVED_BASIS_ANSWER, product_id, version["id"], [], RESTRICTED_SOURCE_ANSWER_REASON
    answer, citation = build_record_answer(db, tenant_id, selected, fields[selected], version)
    return "grounded", answer, product_id, version["id"], [citation], ""


def answer_question_prefer_dify(
    db: Database,
    ai,
    tenant_id: str,
    conversation_id: str,
    question: str,
    product_id: str | None,
    user_label: str,
) -> tuple[str, str, str | None, str | None, list[dict[str, str]], str, str]:
    """第二波统一问答入口：配置 Dify 时优先 Dify（哨兵转人工同口径；引擎故障静默回落本地）。

    返回 (status, answer_text, product_id, version_id, citations, reason, source)；source ∈ {dify, local}。
    """
    if dify_qa_config()[1]:
        try:
            outcome = dify_ask_question(question, user_label)
        except DifyQAUnavailableError:
            outcome = None
        if outcome is not None:
            if outcome["status"] == "grounded":
                return "grounded", outcome["answer"], None, None, outcome["citations"], "", "dify"
            return "needs_human", NO_APPROVED_BASIS_ANSWER, None, None, [], "dify-no-answer", "dify"
    status, answer_text, answer_product_id, version_id, citations, reason = answer_question_with_mode(
        db, ai, tenant_id, conversation_id, question, product_id
    )
    return status, answer_text, answer_product_id, version_id, citations, reason, "local"


AI_TASK_LABELS_ZH = {AI_TASK_EXTRACTION: "需求提取", AI_TASK_ANSWER: "资料问答"}


def ai_status_section(db: Database, ai, tenant_id: str) -> str:
    """管理概览「模型接入」卡片：显式模式 + 最近一次调用的内部状态（可重试/需配置/需排查）。"""
    if ai is None or ai.mode != ai_client.MODE_DEEPSEEK:
        return (
            "<section class='card' style='margin-top:18px'><div class='panel-title'><h2>模型接入</h2>"
            "<span class='muted'>AI 调用边界</span></div><div class='simple-item'><div>"
            "<strong>当前模式：本地规则（演示）</strong><small>买方需求提取与资料内问答由本地规则完成，未调用外部模型。"
            "需要模型辅助时，按 README「启用 DeepSeek 模型辅助」配置 AI_MODE=deepseek 后重启服务。</small></div></div></section>"
        )
    latest = db.latest_ai_call(tenant_id)
    rows = [
        "<div class='simple-item'><div><strong>当前模式：DeepSeek 模型辅助</strong>"
        f"<small>提供方 DeepSeek · 模型 {esc(ai.config.model)}。模型只做候选抽取与字段定位；"
        "数值、答案与引用由平台从已发布核准快照构造。</small></div></div>"
    ]
    if latest is None:
        rows.append(
            "<div class='simple-item'><div><strong>尚无模型调用记录</strong>"
            "<small>买方发送需求或对已发布产品提问后，这里会显示最近一次调用的内部状态。</small></div></div>"
        )
    else:
        task_label = AI_TASK_LABELS_ZH.get(latest["task"], latest["task"])
        status_label = ai_client.STATUS_LABELS_ZH.get(latest["status"], latest["status"])
        details = [f"最近一次调用：{task_label} · {status_label}", latest["created_at"]]
        if latest["duration_ms"] is not None:
            details.append(f"{latest['duration_ms']} ms")
        if latest["http_status"]:
            details.append(f"HTTP {latest['http_status']}")
        if latest["prompt_tokens"] is not None or latest["completion_tokens"] is not None:
            details.append(f"tokens {latest['prompt_tokens'] or 0}+{latest['completion_tokens'] or 0}")
        rows.append(
            "<div class='simple-item'><div><strong>" + esc(" · ".join(details)) + "</strong>"
            "<small>失败时买方只看到统一的「暂不可用、可重试」提示；本卡片与 ai_call_log 记录内部状态，"
            "不含密钥与买家消息。</small></div></div>"
        )
    return (
        "<section class='card' style='margin-top:18px'><div class='panel-title'><h2>模型接入</h2>"
        "<span class='muted'>AI 调用边界</span></div><div class='simple-list'>" + "".join(rows) + "</div></section>"
    )


BASE_CSS = """:root{--ink:#f7f4ff;--muted:#a8a2b8;--line:#29223c;--accent:#8b5cf6;--accent-strong:#a855f7;--bg:#08070f;--card:rgba(20,17,33,.9);--card-2:#151126;--warn:#f6c56a;--danger:#ff8fa3;--success:#67e8c5;}
*{box-sizing:border-box}html{background:var(--bg)}body{min-height:100vh;margin:0;background-color:var(--bg);background-image:linear-gradient(rgba(139,92,246,.045) 1px,transparent 1px),linear-gradient(90deg,rgba(139,92,246,.045) 1px,transparent 1px),radial-gradient(circle at 85% 0%,rgba(139,92,246,.15),transparent 28rem);background-size:48px 48px,48px 48px,100% 100%;color:var(--ink);font:15px/1.6 "Avenir Next","SF Pro Display","PingFang SC","Microsoft YaHei",sans-serif}
a{color:#c4b5fd;text-decoration:none}a:hover{color:#ede9fe;text-decoration:none}header{position:sticky;top:0;z-index:10;background:rgba(8,7,15,.88);backdrop-filter:blur(18px);border-bottom:1px solid rgba(139,92,246,.18);color:var(--ink);padding:17px 32px;display:flex;justify-content:space-between;align-items:center;gap:22px}header strong{font-size:18px;letter-spacing:.08em}header nav{display:flex;gap:4px;align-items:center;justify-content:flex-end;flex-wrap:wrap}header nav a{color:var(--muted);padding:7px 10px;border-radius:9px;white-space:nowrap;flex-shrink:0}header nav a:hover{background:rgba(139,92,246,.12);color:var(--ink)}main{max-width:1240px;margin:0 auto;padding:54px 28px 80px}h1{font-size:38px;line-height:1.15;letter-spacing:-.035em;margin:0 0 8px}h2{font-size:21px;letter-spacing:-.02em;margin:0 0 14px}h3{font-size:16px;margin:20px 0 8px}.muted{color:var(--muted)}.notice{padding:13px 15px;border-radius:12px;background:rgba(103,232,197,.1);border:1px solid rgba(103,232,197,.22);color:#9af2da;margin:0 0 20px}.error{padding:13px 15px;border-radius:12px;background:rgba(255,143,163,.1);border:1px solid rgba(255,143,163,.23);color:#ffb1c0;margin:0 0 20px}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(250px,1fr));gap:18px}.metric-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(190px,1fr));gap:18px}.metric{font-size:38px;font-weight:750;line-height:1.1;margin:8px 0;color:#fff}.action-card{display:block;color:var(--ink);transition:transform .2s ease,border-color .2s ease,background .2s ease}.action-card:hover{text-decoration:none;border-color:rgba(168,85,247,.75);background:rgba(32,24,52,.95);transform:translateY(-2px)}.card{background:var(--card);border:1px solid var(--line);border-radius:18px;padding:22px;box-shadow:0 18px 45px rgba(0,0,0,.2);backdrop-filter:blur(12px)}.toolbar{display:flex;justify-content:space-between;align-items:center;gap:12px;flex-wrap:wrap;margin-bottom:26px}button,.button{border:0;border-radius:10px;padding:10px 15px;background:linear-gradient(135deg,var(--accent),var(--accent-strong));color:#fff;font-weight:700;cursor:pointer;display:inline-block;box-shadow:0 8px 24px rgba(139,92,246,.24)}button:hover,.button:hover{filter:brightness(1.08);transform:translateY(-1px)}button.secondary,.button.secondary{background:rgba(139,92,246,.1);border:1px solid rgba(139,92,246,.35);color:#ddd6fe;box-shadow:none}button.warn,.button.warn{background:rgba(246,197,106,.12);border:1px solid rgba(246,197,106,.35);color:var(--warn);box-shadow:none}button.danger,.button.danger{background:rgba(255,143,163,.1);border:1px solid rgba(255,143,163,.35);color:var(--danger);box-shadow:none}form.inline{display:inline}label{display:block;font-weight:700;margin:10px 0 5px;color:#ded8ed}input,select,textarea{width:100%;padding:10px 11px;border:1px solid #3a3152;border-radius:10px;font:inherit;color:var(--ink);background:#0f0d19}input::placeholder,textarea::placeholder{color:#716a85}input:focus,select:focus,textarea:focus{outline:2px solid rgba(168,85,247,.35);border-color:var(--accent)}textarea{min-height:76px;resize:vertical}.form-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:4px 14px}.full{grid-column:1/-1}table{width:100%;border-collapse:collapse}th,td{text-align:left;padding:12px 8px;border-bottom:1px solid var(--line);vertical-align:top}th{font-size:12px;color:var(--muted);text-transform:uppercase;letter-spacing:.06em}.badge{display:inline-flex;align-items:center;border-radius:999px;padding:3px 10px;font-size:12px;font-weight:700;background:rgba(139,92,246,.13);border:1px solid rgba(139,92,246,.24);color:#d8ccff}.badge.published,.badge.approved{background:rgba(103,232,197,.1);border-color:rgba(103,232,197,.25);color:#9af2da}.badge.pending_review{background:rgba(246,197,106,.1);border-color:rgba(246,197,106,.25);color:#f7d991}.badge.withdrawn{background:rgba(255,143,163,.1);border-color:rgba(255,143,163,.25);color:#ffb1c0}.field-state{font-size:12px;color:var(--muted)}.source{display:flex;justify-content:space-between;gap:12px;padding:11px 0;border-bottom:1px solid var(--line)}.product-hero{background:linear-gradient(135deg,#17112d 0%,#31206a 52%,#5627a4 100%);color:white;border:1px solid rgba(196,181,253,.22);border-radius:22px;padding:36px;margin-bottom:20px;box-shadow:0 22px 60px rgba(69,35,132,.25)}.product-hero .eyebrow{text-transform:uppercase;letter-spacing:.14em;font-size:12px;opacity:.8}.product-hero h1{font-size:42px;margin:4px 0 8px}.kv{display:grid;grid-template-columns:repeat(auto-fit,minmax(190px,1fr));gap:12px;margin-top:24px}.kv div{background:rgba(8,7,15,.24);border:1px solid rgba(255,255,255,.14);padding:13px;border-radius:12px}.kv b{display:block;font-size:12px;opacity:.75}.kv span{font-size:19px;font-weight:700}.footer-note{margin-top:28px;padding:14px 16px;border-left:3px solid #a78bfa;background:rgba(167,139,250,.08);color:#c4b5fd;border-radius:0 10px 10px 0}.brand-mark{display:inline-block;width:10px;height:10px;margin-right:10px;border-radius:3px 8px 3px 8px;background:linear-gradient(135deg,#c4b5fd,#7c3aed);box-shadow:0 0 18px rgba(168,85,247,.75);transform:rotate(45deg)}.brand-logo{display:inline-block;width:22px;height:22px;padding:2px;margin-right:9px;background:#fff;border-radius:7px;object-fit:contain;vertical-align:-5px}@media(max-width:700px){header{padding:15px 18px;align-items:flex-start;flex-direction:column}header nav{justify-content:flex-start;overflow:auto;flex-wrap:nowrap;width:100%;padding-bottom:2px}main{padding:34px 14px 60px}.form-grid{grid-template-columns:1fr}.full{grid-column:auto}h1{font-size:30px}.product-hero h1{font-size:34px}table{font-size:13px}main table{display:block;overflow-x:auto;max-width:100%}}"""


def layout(title: str, body: str, active: str = "产品管理", lang: str = "zh", public: bool = False) -> str:
    header_html = "<header><strong>Industrial Pump Supplier <small>· DEMO</small></strong><nav><a href='#specifications'>Specifications / 参数</a><a href='#downloads'>Documents / 资料</a><a href='#product-rfq'>RFQ / 询价</a></nav></header>" if public else f"<header><strong><img class='brand-logo' src='/assets/brand.png' alt=''>Growave</strong><nav><a href='/admin'>{'管理概览' if lang == 'zh' else 'Overview'}</a><a href='/admin/products'>{'产品管理' if lang == 'zh' else 'Products'}</a><a href='/admin/tasks'>{'人工任务' if lang == 'zh' else 'Tasks'}</a><a href='/admin/rfqs'>{'RFQ询价' if lang == 'zh' else 'RFQs'}</a><a href='/admin/sales'>{'销售工作台' if lang == 'zh' else 'Sales desk'}</a><a href='/inquiry'>{'买方询盘' if lang == 'zh' else 'Buyer inquiry'}</a><a href='/admin/products?lang={'en' if lang == 'zh' else 'zh'}'>{'English' if lang == 'zh' else '中文'}</a></nav></header>"
    # 公开页禁止把完整 URL（含可能的查询参数）经 Referer 传给外部资源；交接码只走 fragment，
    # 本就不发送给服务器也不进 Referer，这里的策略是对公开页的额外防护。
    referrer_meta = "<meta name='referrer' content='no-referrer'>" if public else ""
    return f"""<!doctype html>
<html lang='{ 'en' if lang == 'en' else 'zh-CN' }'>
<head>
<meta charset='utf-8'><meta name='viewport' content='width=device-width, initial-scale=1'>{referrer_meta}
<title>{esc(title)} · Growave</title>
<style>
{BASE_CSS}
</style></head><body>
{header_html}
<main>{body}</main></body></html>"""


CONSOLE_CSS = """
/* ════════ 卖家控制台视觉系统 · 复刻自朋友设计稿「错误态」版 ════════
   ① 配色：浅色 SaaS 默认 + 深色 Linear-C（html[data-theme="dark"] 激活）
   ② 字体：Inter + JetBrains Mono（联网加载；断网自动退回系统字体）
   ③ 图标：线性 stroke 图标；导航/弹窗用 #i-* sprite，状态与报错用 CSS mask
   ④ 报错：行内字段错误(.field-msg) / 表单汇总条(.form-alert) / 权限弹窗(.dlg)
   ⑤ 深浅：theme-toggle（sun/moon）+ localStorage + 跟随系统 + 快捷键 D
   ══════════════════════════════════════════════════════════ */
:root{
  --bg-app:#F7F7F7;--surface:#FFFFFF;--surface-2:#F4F4F4;--surface-3:#EEEEEE;
  --border:#E6E6E6;--border-strong:#D6D6D6;--border-subtle:#EFEFEF;
  --text-1:#1C1C1C;--text-2:#5F5F5F;--text-3:#8B8B8B;--text-4:#ABABAB;
  --accent:#4A38F5;--accent-hover:#3A28D4;--accent-soft:#EEEBFE;--accent-border:#D6D1FC;
  --ok:#0E7A5F;--ok-soft:#EAF4F0;--ok-border:#C2E0D5;
  --warn:#8A6410;--warn-soft:#FBF3E4;--warn-border:#EADCB6;
  --danger:#B23B30;--danger-soft:#FBEDEB;--danger-border:#EDC8C4;--danger-text:#8F2A20;
  --nav-bg:#E8E8E8;--nav-hover:#E2E2E2;--nav-active:#DCDCDC;--nav-line:#E1E1E1;
  --nav-text:#5E5E5E;--nav-text-2:#8C8C8C;--nav-text-active:#1C1C1C;--nav-accent:#4A38F5;
  --r-xs:3px;--r-sm:5px;--r-md:7px;--r-lg:11px;--r-full:999px;
  --sh-xs:0 1px 2px rgba(16,24,40,.04);
  --sh-sm:0 1px 3px rgba(16,24,40,.07),0 1px 2px rgba(16,24,40,.04);
  --sh-md:0 8px 24px rgba(16,24,40,.10),0 2px 6px rgba(16,24,40,.05);
  --font-sans:"Inter","Inter var",-apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Hiragino Sans GB","Microsoft YaHei",sans-serif;
  --font-mono:"JetBrains Mono",ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;
  --row-hover:#F7F9FC;--callout-bg:#FCFCFB;--cat-bg:#F1F3F6;--cat-fg:#4A5160;--cat-bd:#E1E5EA;
  --sb-avatar-bg:#DEDEDE;--sb-avatar-fg:#4A4A4A;--sb-name:#2B2B2B;--sb-hover-fg:#1C1C1C;--sb-foot-fg:#3A3A3A;
  --topbar-bg:rgba(255,255,255,.88);--tenant-bg:rgba(0,0,0,.025);
  --sidebar-w:248px;--topbar-h:60px;
}
*{box-sizing:border-box}
html{background:var(--bg-app);-webkit-text-size-adjust:100%}
body{margin:0;min-height:100vh;background:var(--bg-app);background-image:none;color:var(--text-1);font:400 13px/1.55 var(--font-sans);-webkit-font-smoothing:antialiased}
button,input,textarea,select{font:inherit;color:inherit}
button{cursor:pointer;border:0;background:none}
:focus-visible{outline:2px solid var(--accent);outline-offset:2px;border-radius:var(--r-xs)}
h1,h2,h3,p,dl,dd,dt,ol,ul,figure,blockquote{margin:0}
ul,ol{padding:0;list-style:none}
a{color:var(--accent);text-decoration:none}
a:hover{color:var(--accent-hover);text-decoration:underline;text-underline-offset:2px}
h1{font-size:25px;font-weight:600;letter-spacing:-.022em;line-height:1.25;margin:8px 0 6px}
h2{font-size:14px;font-weight:600;letter-spacing:-.005em;margin:0 0 12px}
h3{font-size:12px;font-weight:600;color:var(--text-2);letter-spacing:.01em;margin:0 0 8px;line-height:1.5}
.ic{width:16px;height:16px;flex:none;fill:none;stroke:currentColor;stroke-width:1.6;stroke-linecap:round;stroke-linejoin:round}
code{font-family:var(--font-mono);font-size:12px;background:var(--surface-3);border:1px solid var(--border);border-radius:var(--r-xs);padding:1px 5px;letter-spacing:-.01em}
hr{border:0;border-top:1px solid var(--border-subtle);margin:16px 0}
/* ── 外壳：左工具架 + 右工作区 ── */
.console{display:grid;grid-template-columns:var(--sidebar-w) minmax(0,1fr);min-height:100vh}
.console .side{position:sticky;top:0;height:100vh;display:flex;flex-direction:column;background:var(--nav-bg);border-right:0;padding:16px 12px 12px;z-index:20}
.console .side-brand{display:flex;align-items:center;gap:10px;padding:0 6px 16px;border-bottom:1px solid var(--nav-line)}
.console .side-brand strong{display:block;font-size:13px;font-weight:600;color:var(--nav-text-active);letter-spacing:-.01em}
.console .side-brand small{display:block;font-size:10px;color:var(--nav-text-2);letter-spacing:.1em;margin-top:1px}
.brand-mark{display:block;width:30px;height:30px;flex:none;margin:0;border-radius:var(--r-lg);background:#fff;border:1px solid rgba(16,24,40,.06);box-shadow:0 1px 2px rgba(16,24,40,.08);object-fit:contain;transform:none}
.console .side-tenant{display:flex;align-items:center;gap:8px;margin:12px 0;padding:8px 9px;border:1px solid var(--nav-line);border-radius:var(--r-md);background:var(--tenant-bg);font-size:12px;color:var(--sb-name)}
.console .side-tenant .tenant-avatar{display:grid;place-items:center;width:22px;height:22px;flex:none;border-radius:var(--r-sm);background:var(--sb-avatar-bg);color:var(--sb-avatar-fg);font-size:11px;font-weight:600}
.console .side-nav{flex:1;overflow-y:auto;overflow-x:hidden;display:flex;flex-direction:column;gap:1px;padding-top:2px}
/* 磁吸放大的前提：导航项不参与 flex 收缩——否则放大的行高会被压回 min-content 地板（溢出交给滚动），效果被吃掉 */
.console .side-nav>*{flex:none}
.console .side-heading{font-size:10px;font-weight:600;letter-spacing:.1em;color:var(--nav-text-2);margin:16px 10px 6px}
.console .side-heading:first-child{margin-top:4px}
/* 侧栏模块磁吸放大（CONSOLE_DOCK_JS）逐帧驱动行高与图标，transition 只保留配色属性，避免拖慢逐帧更新 */
.console .side-link{position:relative;display:flex;align-items:center;gap:10px;padding:8px 10px;border-radius:var(--r-md);color:var(--nav-text);font-size:13px;transition:background-color .13s ease,color .13s ease}
.console .side-link .ic{opacity:.75}
.console .side-link:hover{background:var(--nav-hover);color:var(--sb-hover-fg);text-decoration:none}
.console .side-link:hover .ic{opacity:1}
.console .side-link.active{background:var(--nav-active);color:var(--nav-text-active);font-weight:500}
.console .side-link.active .ic{opacity:1;color:var(--nav-accent)}
.console .side-foot{margin-top:auto;padding:12px 8px 4px;border-top:1px solid var(--nav-line);font-size:11px;color:var(--nav-text-2);line-height:1.9;overflow-wrap:anywhere}
.console .side-foot a{font-size:11px}
.console .shell{min-width:0;display:flex;flex-direction:column}
.console .topbar{position:sticky;top:0;z-index:15;height:var(--topbar-h);display:flex;flex-direction:row;align-items:center;gap:10px;padding:0 24px;background:var(--topbar-bg);backdrop-filter:blur(10px);border-bottom:1px solid var(--border)}
.console .crumb{font-size:12px;color:var(--text-3);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.console .crumb b{color:var(--text-1);font-weight:600}
.console .topbar .grow{flex:1}
.console .topbar>a{font-size:13px;color:var(--text-2);padding:7px 10px;border-radius:var(--r-md);white-space:nowrap}
.console .topbar>a:hover{background:var(--surface-3);color:var(--text-1);text-decoration:none}
.console .menu-btn{display:none;place-items:center;width:32px;height:32px;padding:0;border:0;background:none;color:var(--text-2);border-radius:var(--r-md)}
.console .menu-btn:hover{background:var(--surface-3);color:var(--text-1)}
.console main{flex:1;max-width:1560px;width:100%;margin:0 auto;padding:28px 28px 64px}
/* ── 主题切换按钮 ── */
.theme-toggle{display:grid;place-items:center;width:32px;height:32px;flex:none;border:0;background:none;border-radius:var(--r-md);color:var(--text-2);position:relative;transition:.15s;padding:0}
.theme-toggle:hover{background:var(--surface-3);color:var(--text-1)}
.theme-toggle svg{width:17px;height:17px;fill:none;stroke:currentColor;stroke-width:1.8;stroke-linecap:round;stroke-linejoin:round;position:absolute;transition:opacity .18s ease,transform .28s cubic-bezier(.34,1.4,.64,1)}
.theme-toggle .tt-moon{opacity:1;transform:rotate(0) scale(1)}
.theme-toggle .tt-sun{opacity:0;transform:rotate(-70deg) scale(.5)}
html[data-theme="dark"] .theme-toggle .tt-moon{opacity:0;transform:rotate(70deg) scale(.5)}
html[data-theme="dark"] .theme-toggle .tt-sun{opacity:1;transform:rotate(0) scale(1)}
html.theme-anim,html.theme-anim *{transition:background-color .22s ease,border-color .22s ease,color .22s ease,box-shadow .22s ease!important}
/* ── 通用组件（覆盖深色基线样式） ── */
.muted{color:var(--text-3)}
.notice{display:flex;align-items:flex-start;gap:9px;padding:11px 13px;border-radius:var(--r-md);font-size:12.5px;line-height:1.6;background:var(--ok-soft);border:1px solid var(--ok-border);color:var(--ok);margin:0 0 14px}
.notice::before{content:"";width:15px;height:15px;flex:none;margin-top:1.5px;background:currentColor;-webkit-mask:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24' fill='none' stroke='black' stroke-width='2.2' stroke-linecap='round' stroke-linejoin='round'%3E%3Ccircle cx='12' cy='12' r='10'/%3E%3Cpath d='m16 9-5.5 5.5L8 12'/%3E%3C/svg%3E") center/contain no-repeat;mask:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24' fill='none' stroke='black' stroke-width='2.2' stroke-linecap='round' stroke-linejoin='round'%3E%3Ccircle cx='12' cy='12' r='10'/%3E%3Cpath d='m16 9-5.5 5.5L8 12'/%3E%3C/svg%3E") center/contain no-repeat}
.error{display:flex;align-items:flex-start;gap:9px;padding:11px 13px;border-radius:var(--r-md);font-size:12.5px;line-height:1.6;background:var(--danger-soft);border:1px solid var(--danger-border);color:var(--danger-text);margin:0 0 14px}
.error::before{content:"";width:15px;height:15px;flex:none;margin-top:1.5px;background:currentColor;-webkit-mask:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24' fill='none' stroke='black' stroke-width='2' stroke-linecap='round' stroke-linejoin='round'%3E%3Cpath d='m21.73 18-8-14a2 2 0 0 0-3.48 0l-8 14A2 2 0 0 0 4 21h16a2 2 0 0 0 1.73-3'/%3E%3Cpath d='M12 9v4'/%3E%3Cpath d='M12 17h.01'/%3E%3C/svg%3E") center/contain no-repeat;mask:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24' fill='none' stroke='black' stroke-width='2' stroke-linecap='round' stroke-linejoin='round'%3E%3Cpath d='m21.73 18-8-14a2 2 0 0 0-3.48 0l-8 14A2 2 0 0 0 4 21h16a2 2 0 0 0 1.73-3'/%3E%3Cpath d='M12 9v4'/%3E%3Cpath d='M12 17h.01'/%3E%3C/svg%3E") center/contain no-repeat}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(300px,1fr));gap:14px}
.metric-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:14px}
.metric{font-family:var(--font-mono);font-size:25px;font-weight:600;line-height:1.2;letter-spacing:-.02em;margin:6px 0 4px;color:var(--text-1);font-variant-numeric:tabular-nums}
.card{background:var(--surface);border:1px solid var(--border);border-radius:var(--r-lg);padding:18px 20px;box-shadow:var(--sh-xs)}
.toolbar{display:flex;justify-content:space-between;align-items:flex-start;gap:12px;flex-wrap:wrap;margin-bottom:16px}
.toolbar h1{margin:0 0 4px}
button,.button{border:1px solid var(--accent);border-radius:var(--r-md);padding:0 13px;height:34px;background:var(--accent);color:#fff;font-weight:500;font-size:13px;cursor:pointer;display:inline-flex;align-items:center;justify-content:center;gap:7px;box-shadow:0 1px 2px rgba(74,56,245,.22);transition:.13s;white-space:nowrap;line-height:1;text-decoration:none}
button:hover,.button:hover{background:var(--accent-hover);border-color:var(--accent-hover);color:#fff;filter:none;transform:none;text-decoration:none}
button.secondary,.button.secondary{background:var(--surface);border:1px solid var(--border-strong);color:var(--text-1);box-shadow:none}
button.secondary:hover,.button.secondary:hover{background:var(--surface-2);border-color:var(--border-strong);color:var(--text-1)}
button.danger,.button.danger{background:var(--danger-soft);border:1px solid var(--danger-border);color:var(--danger-text);box-shadow:none}
button.danger:hover,.button.danger:hover{border-color:var(--danger);color:var(--danger-text)}
button.warn,.button.warn{background:var(--warn-soft);border:1px solid var(--warn-border);color:var(--warn);box-shadow:none}
button.wide,.button.wide{width:100%}
form.inline{display:inline}
label{display:block;font-size:12px;font-weight:600;color:var(--text-2);margin:12px 0 7px}
input,select,textarea{width:100%;height:34px;padding:0 11px;border:1px solid var(--border);border-radius:var(--r-md);font:inherit;font-size:13px;color:var(--text-1);background:var(--surface);transition:.13s}
input[type=file]{height:auto;padding:8px 11px}
textarea{height:auto;min-height:76px;padding:9px 11px;line-height:1.6;resize:vertical}
input::placeholder,textarea::placeholder{color:var(--text-4)}
input:focus,select:focus,textarea:focus{outline:0;border-color:var(--accent);box-shadow:0 0 0 3px var(--accent-soft)}
select{appearance:none;padding-right:30px;background-image:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='12' height='12' viewBox='0 0 24 24' fill='none' stroke='%238B8B8B' stroke-width='2' stroke-linecap='round'%3E%3Cpath d='m6 9 6 6 6-6'/%3E%3C/svg%3E");background-repeat:no-repeat;background-position:right 10px center}
.form-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:2px 14px}
.console .check-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:6px 14px;margin:10px 0 4px}
.console label.check-inline{display:flex;align-items:center;gap:7px;font-size:13px;font-weight:500;color:var(--text-1);margin:0}
.console label.check-inline input{width:auto;margin:0}
.console .stage-row{display:flex;align-items:center;gap:10px;padding:7px 0;border-bottom:1px solid var(--border-subtle);flex-wrap:wrap}
.console .stage-row:last-of-type{border-bottom:none;padding-bottom:0}
.console .chip-key{font-family:var(--font-mono);font-size:11px;color:var(--text-3);background:var(--surface-2);border:1px solid var(--border);border-radius:3px;padding:1px 6px}
.console .stage-row input[name^="label_"]{flex:1;min-width:150px}
.console .stage-row button.mini{padding:2px 9px;font-size:12px;line-height:1.5}
.console .filter-bar{display:flex;align-items:center;gap:10px;flex-wrap:wrap;margin-bottom:12px}
.console .filter-bar label{margin:0;font-size:13px;color:var(--text-2)}
.console .pager{display:flex;align-items:center;gap:12px;margin-top:14px;justify-content:flex-end}
.console .pager .muted{font-size:12px}
.full{grid-column:1/-1}
table{width:100%;border-collapse:collapse;font-size:13px}
th,td{text-align:left;padding:12px 14px;border-bottom:1px solid var(--border-subtle);vertical-align:middle}
th{font-size:11px;font-weight:600;letter-spacing:.03em;color:var(--text-3);background:var(--surface-2);border-bottom:1px solid var(--border);text-transform:none;white-space:nowrap}
tbody tr:last-child td{border-bottom:0}
td .field-state{display:block;margin-top:2px}
.badge{display:inline-flex;align-items:center;gap:5px;border-radius:var(--r-sm);padding:3px 8px;font-size:11px;font-weight:500;background:var(--surface-3);border:1px solid var(--border);color:var(--text-2);vertical-align:middle;white-space:nowrap}
.badge.approved,.badge.published,.badge.ok{background:var(--ok-soft);border-color:var(--ok-border);color:var(--ok)}
.badge.pending_review,.badge.warn{background:var(--warn-soft);border-color:var(--warn-border);color:var(--warn)}
.badge.withdrawn,.badge.danger{background:var(--danger-soft);border-color:var(--danger-border);color:var(--danger-text)}
.badge.info{background:var(--cat-bg);border-color:var(--cat-bd);color:var(--cat-fg)}
.badge.approved::before,.badge.published::before,.badge.ok::before{content:"";width:12px;height:12px;flex:none;background:currentColor;-webkit-mask:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24' fill='none' stroke='black' stroke-width='2.2' stroke-linecap='round' stroke-linejoin='round'%3E%3Ccircle cx='12' cy='12' r='10'/%3E%3Cpath d='m16 9-5.5 5.5L8 12'/%3E%3C/svg%3E") center/contain no-repeat;mask:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24' fill='none' stroke='black' stroke-width='2.2' stroke-linecap='round' stroke-linejoin='round'%3E%3Ccircle cx='12' cy='12' r='10'/%3E%3Cpath d='m16 9-5.5 5.5L8 12'/%3E%3C/svg%3E") center/contain no-repeat}
.badge.pending_review::before,.badge.warn::before{content:"";width:12px;height:12px;flex:none;background:currentColor;-webkit-mask:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24' fill='none' stroke='black' stroke-width='2' stroke-linecap='round' stroke-linejoin='round'%3E%3Cpath d='m21.73 18-8-14a2 2 0 0 0-3.48 0l-8 14A2 2 0 0 0 4 21h16a2 2 0 0 0 1.73-3'/%3E%3Cpath d='M12 9v4'/%3E%3Cpath d='M12 17h.01'/%3E%3C/svg%3E") center/contain no-repeat;mask:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24' fill='none' stroke='black' stroke-width='2' stroke-linecap='round' stroke-linejoin='round'%3E%3Cpath d='m21.73 18-8-14a2 2 0 0 0-3.48 0l-8 14A2 2 0 0 0 4 21h16a2 2 0 0 0 1.73-3'/%3E%3Cpath d='M12 9v4'/%3E%3Cpath d='M12 17h.01'/%3E%3C/svg%3E") center/contain no-repeat}
.badge.withdrawn::before,.badge.danger::before{content:"";width:12px;height:12px;flex:none;background:currentColor;-webkit-mask:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24' fill='none' stroke='black' stroke-width='2' stroke-linecap='round' stroke-linejoin='round'%3E%3Ccircle cx='12' cy='12' r='10'/%3E%3Cpath d='M8.5 12h7'/%3E%3C/svg%3E") center/contain no-repeat;mask:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24' fill='none' stroke='black' stroke-width='2' stroke-linecap='round' stroke-linejoin='round'%3E%3Ccircle cx='12' cy='12' r='10'/%3E%3Cpath d='M8.5 12h7'/%3E%3C/svg%3E") center/contain no-repeat}
.field-state{font-size:11px;color:var(--text-3)}
.source{display:flex;justify-content:space-between;gap:12px;padding:12px 0;border-bottom:1px solid var(--border-subtle)}
.source:last-child{border-bottom:0}
.footer-note{margin-top:16px;padding:11px 13px;border-left:3px solid var(--accent-border);background:var(--surface-2);color:var(--text-2);border-radius:0 var(--r-md) var(--r-md) 0;font-size:12px;line-height:1.65}
/* ── 线稿式展示组件（保留原类名，换肤） ── */
.console .focus{display:flex;justify-content:space-between;gap:18px;align-items:center;background:var(--surface);border:1px solid var(--border);border-left:3px solid var(--accent);border-radius:var(--r-lg);padding:15px 18px;margin:0 0 16px;box-shadow:var(--sh-xs)}
.console .focus .focus-label{font-family:var(--font-mono);color:var(--accent);font-size:11px;font-weight:600;letter-spacing:.12em}
.console .focus strong{display:block;font-size:17px;font-weight:600;letter-spacing:-.015em;line-height:1.35;margin:6px 0 4px;color:var(--text-1)}
.console .focus p{font-size:12.5px;color:var(--text-2);margin:0;max-width:640px;line-height:1.6}
.console .focus .actions{display:flex;gap:8px;flex-wrap:wrap;align-items:center;justify-content:flex-end;flex-shrink:0}
.console .actions{display:flex;gap:8px;flex-wrap:wrap;align-items:center}
.console .panel-title{display:flex;justify-content:space-between;align-items:baseline;gap:10px;margin:0 0 12px}
.console .panel-title h2{margin:0}
.console .panel-title .muted{font-size:12px}
.console .simple-list{display:grid}
.console .simple-item{display:flex;gap:16px;justify-content:space-between;align-items:center;border-bottom:1px solid var(--border-subtle);padding:13px 8px;margin:0 -8px;transition:background .12s}
.console .simple-item:hover{background:var(--row-hover)}
.console .simple-item:first-child{padding-top:6px}
.console .simple-item:last-child{border-bottom:0;padding-bottom:6px}
.console .simple-item strong{display:block;font-size:13.5px;font-weight:500;color:var(--text-1)}
.console .simple-item small{display:block;color:var(--text-3);font-size:11px;margin-top:3px;line-height:1.6}
.console .simple-item .actions{flex-shrink:0;display:flex;gap:8px;align-items:center;flex-wrap:wrap;justify-content:flex-end}
.console .compact-facts{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:10px;margin:0 0 10px}
.console .compact-facts>div{padding:11px 13px;border:1px solid var(--border);border-radius:var(--r-md);background:var(--surface-2)}
.console .compact-facts small{display:block;color:var(--text-3);font-size:11px}
.console .compact-facts strong{font-size:13.5px;font-weight:500}
.console .summary-strip{display:flex;gap:8px;flex-wrap:wrap;margin:0 0 16px}
.console .summary-strip span{font-size:11.5px;background:var(--surface-2);border:1px solid var(--border);border-radius:var(--r-full);padding:4px 11px;color:var(--text-2)}
.console .muted-block{background:var(--surface-2);border:1px solid var(--border);border-radius:var(--r-md);padding:12px 14px;font-size:12.5px;color:var(--text-2);line-height:1.65}
.console .quote{border:1px solid var(--border);border-left:3px solid var(--border-strong);background:var(--surface-2);border-radius:var(--r-md);padding:10px 12px;font-size:12.5px;line-height:1.7;margin:0 0 12px;color:var(--text-2)}
.console .guide{font-size:11.5px;color:var(--text-3);margin-top:8px}
.console .detail-toggle{border-top:1px solid var(--border-subtle);margin-top:14px;padding-top:12px;font-size:13px}
.console .detail-toggle summary{cursor:pointer;color:var(--text-2);font-weight:600;font-size:12.5px;list-style:none;display:flex;align-items:center;justify-content:space-between;gap:10px}
.console .detail-toggle summary::-webkit-details-marker{display:none}
.console .detail-toggle summary::after{content:"";width:14px;height:14px;flex:none;background:var(--text-3);-webkit-mask:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24' fill='none' stroke='black' stroke-width='2' stroke-linecap='round' stroke-linejoin='round'%3E%3Cpath d='m6 9 6 6 6-6'/%3E%3C/svg%3E") center/contain no-repeat;mask:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24' fill='none' stroke='black' stroke-width='2' stroke-linecap='round' stroke-linejoin='round'%3E%3Cpath d='m6 9 6 6 6-6'/%3E%3C/svg%3E") center/contain no-repeat;transition:transform .18s ease}
.console .detail-toggle[open] summary::after{transform:rotate(180deg)}
.console .detail-toggle>div{padding-top:12px}
/* ── 报错方式 · 行内字段错误 / 表单汇总条 ── */
.field{display:flex;flex-direction:column}
.field-msg{display:none;align-items:flex-start;gap:5px;margin-top:6px;font-size:12.5px;line-height:1.55;color:var(--danger-text)}
.field.is-invalid .field-msg{display:flex}
.field-msg::before{content:"";width:13px;height:13px;flex:none;margin-top:2px;background:currentColor;-webkit-mask:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24' fill='none' stroke='black' stroke-width='2' stroke-linecap='round' stroke-linejoin='round'%3E%3Cpath d='m21.73 18-8-14a2 2 0 0 0-3.48 0l-8 14A2 2 0 0 0 4 21h16a2 2 0 0 0 1.73-3'/%3E%3Cpath d='M12 9v4'/%3E%3Cpath d='M12 17h.01'/%3E%3C/svg%3E") center/contain no-repeat;mask:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24' fill='none' stroke='black' stroke-width='2' stroke-linecap='round' stroke-linejoin='round'%3E%3Cpath d='m21.73 18-8-14a2 2 0 0 0-3.48 0l-8 14A2 2 0 0 0 4 21h16a2 2 0 0 0 1.73-3'/%3E%3Cpath d='M12 9v4'/%3E%3Cpath d='M12 17h.01'/%3E%3C/svg%3E") center/contain no-repeat}
.field.is-invalid input,.field.is-invalid select,.field.is-invalid textarea{border-color:var(--danger);background:var(--danger-soft)}
.form-alert{display:flex;align-items:flex-start;gap:9px;padding:11px 13px;border-radius:var(--r-md);font-size:12.5px;line-height:1.6;background:var(--danger-soft);border:1px solid var(--danger-border);color:var(--danger-text);margin:0 0 14px}
.form-alert::before{content:"";width:15px;height:15px;flex:none;margin-top:1.5px;background:currentColor;-webkit-mask:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24' fill='none' stroke='black' stroke-width='2' stroke-linecap='round' stroke-linejoin='round'%3E%3Cpath d='m21.73 18-8-14a2 2 0 0 0-3.48 0l-8 14A2 2 0 0 0 4 21h16a2 2 0 0 0 1.73-3'/%3E%3Cpath d='M12 9v4'/%3E%3Cpath d='M12 17h.01'/%3E%3C/svg%3E") center/contain no-repeat;mask:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24' fill='none' stroke='black' stroke-width='2' stroke-linecap='round' stroke-linejoin='round'%3E%3Cpath d='m21.73 18-8-14a2 2 0 0 0-3.48 0l-8 14A2 2 0 0 0 4 21h16a2 2 0 0 0 1.73-3'/%3E%3Cpath d='M12 9v4'/%3E%3Cpath d='M12 17h.01'/%3E%3C/svg%3E") center/contain no-repeat}
.form-alert b{font-weight:600;color:var(--danger-text)}
.form-alert ul{list-style:disc;margin:4px 0 0;padding-left:18px}
.form-alert a{color:var(--danger-text);text-decoration:underline;text-underline-offset:2px}
/* ── 报错方式 · 权限弹窗（模态卡，403 页使用） ── */
.perm-wrap{max-width:460px;margin:7vh auto 0;padding:0 16px}
.dlg{background:var(--surface);border:1px solid var(--border-strong);border-radius:var(--r-lg);box-shadow:var(--sh-md);overflow:hidden}
.dlg-head{display:flex;gap:12px;padding:18px 18px 0}
.dlg-icon{display:grid;place-items:center;flex:none;width:36px;height:36px;border-radius:var(--r-md);background:var(--danger-soft);color:var(--danger)}
.dlg-icon .ic{width:19px;height:19px;stroke-width:1.9}
.dlg-icon.warn{background:var(--warn-soft);color:var(--warn)}
.dlg-title{font-size:15px;font-weight:600;letter-spacing:-.01em;margin:0 0 4px;color:var(--text-1)}
.dlg-sub{font-size:12.5px;line-height:1.6;color:var(--text-2);margin:0}
.dlg-body{padding:14px 18px 0}
.dlg-reason{font-size:12.5px;line-height:1.65;color:var(--text-2);background:var(--surface-2);border:1px solid var(--border);border-radius:var(--r-md);padding:11px 13px}
.dlg-reason dl{margin:0;display:grid;grid-template-columns:auto 1fr;gap:6px 12px}
.dlg-reason dt{color:var(--text-3);white-space:nowrap}
.dlg-reason dd{margin:0;color:var(--text-2)}
.dlg-reason dd strong{color:var(--text-1);font-weight:600}
.dlg-foot{display:flex;gap:9px;padding:16px 18px 18px;justify-content:flex-end}
/* ── 登录页（独立外壳） ── */
.auth-body{display:flex;flex-direction:column;align-items:center;justify-content:center;min-height:100vh;padding:44px 16px}
.auth-card{width:100%;max-width:440px;background:var(--surface);border:1px solid var(--border);border-radius:var(--r-lg);box-shadow:var(--sh-sm);padding:26px}
.auth-brand{display:flex;align-items:center;gap:10px;padding-bottom:16px;margin-bottom:20px;border-bottom:1px solid var(--border-subtle)}
.auth-brand strong{display:block;font-size:13px;font-weight:600;color:var(--text-1);letter-spacing:-.01em}
.auth-brand small{display:block;font-size:10px;color:var(--text-3);letter-spacing:.1em;margin-top:1px}
.auth-card h1{font-size:21px;margin:0 0 8px;letter-spacing:-.02em}
.auth-sub{font-size:12.5px;color:var(--text-2);line-height:1.65;margin:0 0 14px}
.auth-submit{margin-top:18px}
.theme-toggle.fixed{position:fixed;top:18px;right:18px;z-index:5}
/* ── 深色主题（Linear 配色 C 方案，由脚本在 <html> 上添加 data-theme="dark" 激活） ── */
html[data-theme="dark"]{
  --bg-app:#0A0B0C;--surface:#121314;--surface-2:#171819;--surface-3:#1C1D1E;
  --border:#222426;--border-strong:#2E3134;--border-subtle:#1B1D1F;
  --text-1:#F7F8F8;--text-2:#D0D6E0;--text-3:#8A8F98;--text-4:#62666D;
  --accent:#5E6AD2;--accent-hover:#6D78D5;--accent-soft:#1B1E2E;--accent-border:#2E3550;
  --ok:#3FB98A;--ok-soft:#11221C;--ok-border:#1E3D31;
  --warn:#D9A54A;--warn-soft:#241E11;--warn-border:#3D331C;
  --danger:#E0685C;--danger-soft:#251514;--danger-border:#3E2220;--danger-text:#F0857A;
  --nav-bg:#101112;--nav-hover:#181A1B;--nav-active:#1F2123;--nav-line:#1E2022;
  --nav-text:#9CA0A8;--nav-text-2:#62666D;--nav-text-active:#F7F8F8;--nav-accent:#8790E8;
  --r-md:6px;--r-lg:10px;
  --sb-avatar-bg:#2F3A48;--sb-avatar-fg:#C7D2E0;--sb-name:#D5DCE4;--sb-hover-fg:#E3E9F0;--sb-foot-fg:#D5DCE4;
  --topbar-bg:rgba(10,11,12,.88);--tenant-bg:rgba(255,255,255,.02);--row-hover:#181A1C;--callout-bg:#16181A;
  --cat-bg:#1C1D1E;--cat-fg:#A8AEB8;--cat-bd:#2E3134;
}
/* ── 响应式 ── */
@media(max-width:850px){
.console{grid-template-columns:minmax(0,1fr)}
.console .side{position:fixed;left:0;top:0;width:268px;transform:translateX(-105%);transition:transform .22s cubic-bezier(.4,0,.2,1);z-index:40}
.console .side.open{transform:none;box-shadow:14px 0 44px rgba(10,16,26,.34)}
.console .menu-btn{display:grid}
.console .topbar{padding:0 14px}
.console main{padding:20px 16px 56px}
.console .focus{flex-direction:column;align-items:flex-start}
.console .focus .actions{justify-content:flex-start}
.console .simple-item{flex-direction:column;align-items:flex-start;gap:8px}
.console .simple-item .actions{justify-content:flex-start}
.console .compact-facts{grid-template-columns:1fr}
}
@media(max-width:600px){
.form-grid{grid-template-columns:1fr}
.full{grid-column:auto}
h1{font-size:21px}
.metric{font-size:21px}
}
/* ── 侧栏待办数字（行动待办量；参照好友参考稿：去底纯数字 + 告急"加粗放大红"，
      变化时机械表日历跳转式翻牌，脚本见 CONSOLE_NAV_JS） ── */
.side-count{margin-left:auto;font-family:var(--font-mono);font-size:12px;font-weight:600;color:var(--nav-text);background:none;border:0;padding:0;text-align:right;font-variant-numeric:tabular-nums;transition:color .13s;perspective:120px}
.side-count[hidden]{display:none}
.side-count.hot{font-size:13px;font-weight:700;color:var(--danger)}
.side-link:hover .side-count{color:var(--sb-hover-fg)}
.side-link.active .side-count{color:var(--nav-text-active)}
.side-link:hover .side-count.hot,.side-link.active .side-count.hot{color:var(--danger)}
.side-count .n{display:block;transform:rotateX(0deg);transition:transform .12s ease-in;backface-visibility:hidden}
.side-count .n.flip-out{transform:rotateX(90deg)}
.side-count .n.flip-in{transform:rotateX(-90deg);transition:none}
@media (prefers-reduced-motion:reduce){.side-count .n{transition:none}}
/* ── V1-03 创意中心 · 产品展示页：区域模块条（全区域唯一导航，无数字、连线递进）与新建卡 ── */
.console .flowbar{display:flex;align-items:center;gap:12px;flex-wrap:wrap;margin:0 0 16px}
.console .flowbar .fh,.console .flowbar .fs{display:inline-flex;align-items:center;padding:8px 15px;border:1px solid var(--border);border-radius:var(--r-md);background:var(--surface);color:var(--text-1);font-size:13px;cursor:pointer;text-decoration:none}
.console .flowbar a.fh:hover,.console .flowbar a.fs:hover{border-color:var(--accent-border);color:var(--accent);text-decoration:none}
.console .flowbar .fh.on{background:var(--accent);border-color:var(--accent);color:#fff}
.console .flowbar .fs.on{border-color:var(--accent);color:var(--accent);background:var(--accent-soft);font-weight:600}
.console .flowbar .fs.done{color:var(--text-2)}
.console .flowbar .chain{display:flex;align-items:center;flex-wrap:wrap}
.console .flowbar .conn{width:24px;height:2px;background:var(--border-strong);position:relative;margin:0 7px;flex:none}
.console .flowbar .conn::after{content:"";position:absolute;right:-1px;top:-3.5px;border-left:7px solid var(--border-strong);border-top:4.5px solid transparent;border-bottom:4.5px solid transparent}
.console .flowbar.ghost .fs,.console .flowbar.ghost .conn{opacity:.45}
.console .newcard{display:none;border:1px dashed var(--accent-border);background:var(--accent-soft);border-radius:var(--r-lg);padding:14px 16px;margin:0 0 16px}
.console .newcard.open{display:block}
.console .newcard .step-t{font-family:var(--font-mono);font-size:11px;letter-spacing:.1em;color:var(--accent);font-weight:600}
.console .newcard .line{display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin:10px 0 0}
.console .coach{border:1px solid var(--accent-border);background:var(--accent-soft);color:var(--text-2);border-radius:var(--r-md);padding:9px 13px;font-size:12.5px;margin:0 0 12px;line-height:1.6}
/* ── 选参数：参数卡片 / 推荐池 chips / 开关 ── */
.console .pcard{border:1px solid var(--border);border-radius:var(--r-md);padding:11px 14px;margin:0 0 8px;background:var(--surface)}
.console .pcard.disabled{opacity:.6}
.console .pcard .top{display:flex;justify-content:space-between;gap:10px;align-items:center}
.console .pcard .nm{font-weight:600;font-size:13px}
.console .pcard .nm small{color:var(--text-3);font-weight:400;margin-left:6px}
.console .pcard .unit{color:var(--text-3);font-size:12px;margin-left:6px}
.console .pcard .tools{display:flex;gap:6px;align-items:center;flex-wrap:wrap}
.console .pcard .editrow{display:none}
.console .pcard.editing .editrow{display:block;margin-top:10px}
.console .pcard .editrow form{display:flex;gap:8px;align-items:center;flex-wrap:wrap}
.console .pcard .editrow input{max-width:190px}
.console .pcard .mini{border:1px solid var(--border);background:var(--surface);color:var(--text-2);border-radius:var(--r-sm);min-width:26px;height:26px;padding:0 8px;font-size:12px;cursor:pointer;display:inline-flex;align-items:center;justify-content:center}
.console .pcard .mini:hover{border-color:var(--accent-border);color:var(--accent)}
.console .pcard .mini.link{border:0;background:none;color:var(--accent);padding:0 4px}
.console .pcard .mini.danger-link{border:0;background:none;color:var(--danger-text);padding:0 4px}
.console .pcard .mini.danger-link:hover{color:var(--danger)}
.console .pcard .ptoggle{width:34px;height:19px;border-radius:var(--r-full);background:var(--border-strong);position:relative;padding:0;border:0;cursor:pointer;transition:background .15s}
.console .pcard .ptoggle::after{content:"";position:absolute;top:2px;left:2px;width:15px;height:15px;border-radius:50%;background:#fff;transition:left .15s;box-shadow:0 1px 2px rgba(0,0,0,.18)}
.console .pcard .ptoggle.on{background:var(--accent)}
.console .pcard .ptoggle.on::after{left:17px}
.console .pchips{display:flex;gap:8px;flex-wrap:wrap;margin:8px 0 0}
.console .pchip{border:1px dashed var(--border-strong);background:var(--surface);color:var(--text-2);border-radius:var(--r-full);padding:5px 12px;font-size:12px;cursor:pointer;display:inline-block}
.console .pchip:hover{border-color:var(--accent);color:var(--accent)}
.console .pchip.added{border-style:solid;border-color:var(--border-subtle);color:var(--text-4);cursor:default}
.console .pnew{margin-top:10px}
.console .pnew summary{list-style:none;display:inline-block}
.console .pnew summary::-webkit-details-marker{display:none}
.console .pnew[open] summary{margin-bottom:8px}
.console .pnew form{display:flex;gap:8px;flex-wrap:wrap;align-items:center}
.console .pnew input{max-width:220px}
.console .step-cta{text-align:center;margin:18px 0 4px}
/* ── 排内容：板块卡补充 + 进阶折叠卡 ── */
.console .pcard .lock{font-size:11px;color:var(--text-3);border:1px solid var(--border);border-radius:var(--r-full);padding:2px 9px;white-space:nowrap}
.console .pcard .title-note{color:var(--text-3);font-size:12px;margin-left:6px}
.console .pcard .bnote{color:var(--text-3);font-size:12px;margin-top:6px}
.console .pcard .editrow input[name='title_zh']{max-width:240px}
.console details.card{padding:13px 16px}
.console details.card>summary{cursor:pointer;list-style:none;display:flex;justify-content:space-between;align-items:center;gap:10px;font-weight:600;font-size:13px;color:var(--text-1)}
.console details.card>summary::-webkit-details-marker{display:none}
.console details.card>summary::after{content:"＋";color:var(--text-3);font-weight:500}
.console details.card[open]>summary::after{content:"－"}
.console details.card .inner{padding-top:12px}
.console details.card .inner form{display:block}
.console details.card .inner label{margin-top:0}
.console details.card .inner textarea{margin-top:2px;min-height:56px}
.console details.card + details.card{margin-top:8px}
/* ── 看效果：试看表单 ── */
.console .step-form{display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin:8px 0 0}
.console .step-form label{display:flex;gap:6px;align-items:center;margin:0;font-weight:500;color:var(--text-2);font-size:12.5px}
.console .step-form select{max-width:340px;min-width:200px}
/* ── 会话详情：主流聊天界面（左=买方 / 右=AI·人工 / 中=系统）+ iOS 滑块开关 ── */
.console .card.chat-shell{padding:0;overflow:hidden}
.console .chat-head{display:flex;align-items:center;gap:12px;padding:14px 18px;border-bottom:1px solid var(--border);background:var(--surface-2)}
.console .chat-avatar{width:38px;height:38px;border-radius:50%;background:var(--accent);color:#fff;display:flex;align-items:center;justify-content:center;font-weight:600;font-size:15px;flex:none}
.console .chat-id{min-width:0;display:flex;flex-direction:column;gap:2px}
.console .chat-id strong{font-size:14.5px;color:var(--text-1);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.console .chat-id small{font-size:11.5px;color:var(--text-3);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.console .chat-tools{margin-left:auto;flex:none}
.console .chat-chips{display:flex;gap:8px;align-items:center;flex-wrap:wrap;padding:10px 18px;border-bottom:1px solid var(--border)}
.console .chip{display:inline-flex;align-items:center;gap:6px;padding:4px 10px;border:1px solid var(--border);border-radius:999px;font-size:12px;color:var(--text-2);background:var(--surface-2);text-decoration:none}
.console .chip:hover{border-color:var(--accent-border);color:var(--text-1)}
.console .chat-chips form.inline{margin:0}
.console .chat-log{display:flex;flex-direction:column;gap:12px;padding:18px;min-height:320px;max-height:56vh;overflow-y:auto}
.console .chat-msg{display:flex;flex-direction:column;max-width:76%}
.console .chat-msg.left{align-self:flex-start;align-items:flex-start}
.console .chat-msg.right{align-self:flex-end;align-items:flex-end}
.console .chat-msg.center{align-self:center;align-items:center;max-width:100%}
.console .chat-bubble{padding:9px 13px;border-radius:14px;background:var(--surface-2);border:1px solid var(--border);font-size:13px;line-height:1.65;color:var(--text-1);white-space:pre-wrap;word-break:break-word}
.console .chat-msg.left .chat-bubble{border-top-left-radius:4px}
.console .chat-msg.right .chat-bubble{border-top-right-radius:4px;background:var(--accent-soft);border-color:var(--accent-border)}
.console .chat-msg.right.human .chat-bubble{background:var(--surface-2);border-color:var(--ok-border)}
.console .chat-msg.center .chat-bubble{background:transparent;border:0;color:var(--text-3);font-size:12px;padding:0 8px;text-align:center}
.console .chat-meta{display:flex;gap:8px;align-items:baseline;font-size:11px;color:var(--text-3);padding:3px 2px 0;flex-wrap:wrap}
.console .chat-meta .who{font-weight:600;color:var(--text-2)}
.console .chat-composer{display:flex;gap:10px;align-items:flex-end;padding:12px 18px;border-top:1px solid var(--border)}
.console .chat-composer textarea{flex:1;min-height:44px;max-height:140px;margin:0;resize:vertical}
.console .chat-composer button{flex:none;height:40px;padding:0 22px}
.console .chat-foot{display:flex;align-items:center;gap:10px;flex-wrap:wrap;padding:0 18px 12px}
.console .chat-hint{margin:0;font-size:11.5px;color:var(--text-3)}
/* 输入区底部小气泡（用户口径：小气泡格放在输入框底部、不越出对话框边线） */
.console .qpill{height:28px;padding:0 12px;border-radius:999px;font-size:12px;font-weight:500;background:var(--surface-2);border:1px solid var(--border-strong);color:var(--text-2);display:inline-flex;align-items:center;gap:6px;box-shadow:none;cursor:pointer}
.console .qpill:hover{border-color:var(--accent-border);color:var(--accent);background:var(--accent-soft)}
.console .qpill .ic{width:14px;height:14px}
/* iOS 风格滑块开关（AI 自动回复） */
.console .ai-switch-form{display:flex;align-items:center;gap:8px;margin:0}
.console .ios-switch{display:inline-flex;align-items:center;gap:8px;cursor:pointer;margin:0;font-weight:500}
.console .ios-switch input{position:absolute;opacity:0;width:0;height:0}
.console .ios-slider{position:relative;width:46px;height:28px;border-radius:999px;background:var(--border);transition:background .18s ease;flex:none}
.console .ios-slider::after{content:"";position:absolute;top:2px;left:2px;width:24px;height:24px;border-radius:50%;background:#fff;box-shadow:0 1px 3px rgba(0,0,0,.28);transition:transform .18s ease}
.console .ios-switch input:checked + .ios-slider{background:var(--accent)}
.console .ios-switch input:checked + .ios-slider::after{transform:translateX(18px)}
.console .ios-switch input:focus-visible + .ios-slider{outline:2px solid var(--accent-border);outline-offset:2px}
.console .ios-text{font-size:12.5px;color:var(--text-2);white-space:nowrap}
.console .ios-state{color:var(--warn,#c2410c);font-weight:600}
"""


CONSOLE_ICONS = """<svg width="0" height="0" style="position:absolute" aria-hidden="true"><defs>
<symbol id="i-home" viewBox="0 0 24 24"><path d="M15 21v-8a1 1 0 0 0-1-1h-4a1 1 0 0 0-1 1v8"/><path d="M3 10a2 2 0 0 1 .709-1.528l7-6a2 2 0 0 1 2.582 0l7 6A2 2 0 0 1 21 10v9a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/></symbol>
<symbol id="i-inbox" viewBox="0 0 24 24"><polyline points="22 12 16 12 14 15 10 15 8 12 2 12"/><path d="M5.45 5.11 2 12v6a2 2 0 0 0 2 2h16a2 2 0 0 0 2-2v-6l-3.45-6.89A2 2 0 0 0 16.76 4H7.24a2 2 0 0 0-1.79 1.11z"/></symbol>
<symbol id="i-tool" viewBox="0 0 24 24"><path d="M14.7 6.3a1 1 0 0 0 0 1.4l1.6 1.6a1 1 0 0 0 1.4 0l3.106-3.105c.32-.322.863-.22.983.218a6 6 0 0 1-8.259 7.057l-7.91 7.91a1 1 0 0 1-2.999-3l7.91-7.91a6 6 0 0 1 7.057-8.259c.438.12.54.662.219.984z"/></symbol>
<symbol id="i-file" viewBox="0 0 24 24"><path d="M6 22a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h8a2.4 2.4 0 0 1 1.704.706l3.588 3.588A2.4 2.4 0 0 1 20 8v12a2 2 0 0 1-2 2z"/><path d="M14 2v5a1 1 0 0 0 1 1h5"/><path d="M10 9H8"/><path d="M16 13H8"/><path d="M16 17H8"/></symbol>
<symbol id="i-box" viewBox="0 0 24 24"><path d="M11 21.73a2 2 0 0 0 2 0l7-4A2 2 0 0 0 21 16V8a2 2 0 0 0-1-1.73l-7-4a2 2 0 0 0-2 0l-7 4A2 2 0 0 0 3 8v8a2 2 0 0 0 1 1.73z"/><path d="M12 22V12"/><polyline points="3.29 7 12 12 20.71 7"/><path d="m7.5 4.27 9 5.15"/></symbol>
<symbol id="i-shield" viewBox="0 0 24 24"><path d="M20 13c0 5-3.5 7.5-7.66 8.95a1 1 0 0 1-.67-.01C7.5 20.5 4 18 4 13V6a1 1 0 0 1 1-1c2 0 4.5-1.2 6.24-2.72a1.17 1.17 0 0 1 1.52 0C14.51 3.81 17 5 19 5a1 1 0 0 1 1 1z"/><path d="m9 12 2 2 4-4"/></symbol>
<symbol id="i-globe" viewBox="0 0 24 24"><circle cx="12" cy="12" r="10"/><path d="M12 2a14.5 14.5 0 0 0 0 20 14.5 14.5 0 0 0 0-20"/><path d="M2 12h20"/></symbol>
<symbol id="i-layers" viewBox="0 0 24 24"><path d="M12.83 2.18a2 2 0 0 0-1.66 0L2.6 6.08a1 1 0 0 0 0 1.83l8.58 3.91a2 2 0 0 0 1.66 0l8.58-3.9a1 1 0 0 0 0-1.83z"/><path d="M2 12a1 1 0 0 0 .58.91l8.6 3.91a2 2 0 0 0 1.65 0l8.58-3.9A1 1 0 0 0 22 12"/><path d="M2 17a1 1 0 0 0 .58.91l8.6 3.91a2 2 0 0 0 1.65 0l8.58-3.9A1 1 0 0 0 22 17"/></symbol>
<symbol id="i-users" viewBox="0 0 24 24"><path d="M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2"/><path d="M16 3.128a4 4 0 0 1 0 7.744"/><path d="M22 21v-2a4 4 0 0 0-3-3.87"/><circle cx="9" cy="7" r="4"/></symbol>
<symbol id="i-sliders" viewBox="0 0 24 24"><path d="M21 4h-7"/><path d="M10 4H3"/><path d="M21 12h-9"/><path d="M8 12H3"/><path d="M21 20h-5"/><path d="M12 20H3"/><path d="M14 2v4"/><path d="M8 10v4"/><path d="M16 18v4"/></symbol>
<symbol id="i-history" viewBox="0 0 24 24"><path d="M3 12a9 9 0 1 0 9-9 9.75 9.75 0 0 0-6.74 2.74L3 8"/><path d="M3 3v5h5"/><path d="M12 7v5l4 2"/></symbol>
<symbol id="i-chart" viewBox="0 0 24 24"><path d="M3 3v16a2 2 0 0 0 2 2h16"/><path d="M18 17V9"/><path d="M13 17V5"/><path d="M8 17v-3"/></symbol>
<symbol id="i-search" viewBox="0 0 24 24"><circle cx="11" cy="11" r="8"/><path d="m21 21-4.3-4.3"/></symbol>
<symbol id="i-menu" viewBox="0 0 24 24"><path d="M4 7h16"/><path d="M4 12h16"/><path d="M4 17h16"/></symbol>
<symbol id="i-check" viewBox="0 0 24 24"><circle cx="12" cy="12" r="10"/><path d="m16 9-5.5 5.5L8 12"/></symbol>
<symbol id="i-draft" viewBox="0 0 24 24"><path d="M10.1 2.182a10 10 0 0 1 3.8 0"/><path d="M13.9 21.818a10 10 0 0 1-3.8 0"/><path d="M17.609 3.721a10 10 0 0 1 2.69 2.7"/><path d="M2.182 13.9a10 10 0 0 1 0-3.8"/><path d="M20.279 17.609a10 10 0 0 1-2.7 2.69"/><path d="M21.818 10.1a10 10 0 0 1 0 3.8"/><path d="M3.721 6.391a10 10 0 0 1 2.7-2.69"/><path d="M6.391 20.279a10 10 0 0 1-2.69-2.7"/></symbol>
<symbol id="i-alert" viewBox="0 0 24 24"><path d="m21.73 18-8-14a2 2 0 0 0-3.48 0l-8 14A2 2 0 0 0 4 21h16a2 2 0 0 0 1.73-3"/><path d="M12 9v4"/><path d="M12 17h.01"/></symbol>
<symbol id="i-gap" viewBox="0 0 24 24"><path d="M10.1 2.182a10 10 0 0 1 3.8 0"/><path d="M13.9 21.818a10 10 0 0 1-3.8 0"/><path d="M17.609 3.721a10 10 0 0 1 2.69 2.7"/><path d="M2.182 13.9a10 10 0 0 1 0-3.8"/><path d="M20.279 17.609a10 10 0 0 1-2.7 2.69"/><path d="M21.818 10.1a10 10 0 0 1 0 3.8"/><path d="M3.721 6.391a10 10 0 0 1 2.7-2.69"/><path d="M6.391 20.279a10 10 0 0 1-2.69-2.7"/><path d="M8.5 12h7"/></symbol>
<symbol id="i-arrow-right" viewBox="0 0 24 24"><path d="M4 12h15"/><path d="M13 6l6 6-6 6"/></symbol>
<symbol id="i-plus" viewBox="0 0 24 24"><path d="M12 5v14"/><path d="M5 12h14"/></symbol>
<symbol id="i-folder" viewBox="0 0 24 24"><path d="M12 10v6"/><path d="M9 13h6"/><path d="M20 20a2 2 0 0 0 2-2V8a2 2 0 0 0-2-2h-7.9a2 2 0 0 1-1.69-.9L9.6 3.9A2 2 0 0 0 7.93 3H4a2 2 0 0 0-2 2v13a2 2 0 0 0 2 2z"/></symbol>
<symbol id="i-clock" viewBox="0 0 24 24"><circle cx="12" cy="12" r="10"/><path d="M12 6v6l4 2"/></symbol>
<symbol id="i-branch" viewBox="0 0 24 24"><circle cx="6" cy="19" r="3"/><path d="M9 19h8.5a3.5 3.5 0 0 0 0-7h-11a3.5 3.5 0 0 1 0-7H15"/><circle cx="18" cy="5" r="3"/></symbol>
<symbol id="i-palette" viewBox="0 0 24 24"><circle cx="13.5" cy="6.5" r=".5"/><circle cx="17.5" cy="10.5" r=".5"/><circle cx="8.5" cy="7.5" r=".5"/><circle cx="6.5" cy="12.5" r=".5"/><path d="M12 2C6.5 2 2 6.5 2 12s4.5 10 10 10c.926 0 1.648-.746 1.648-1.688 0-.437-.18-.835-.437-1.125-.29-.289-.438-.652-.438-1.125a1.64 1.64 0 0 1 1.668-1.668h1.996c3.051 0 5.555-2.503 5.555-5.554C21.965 6.012 17.461 2 12 2z"/></symbol>
<symbol id="i-chat" viewBox="0 0 24 24"><path d="M7.9 20A9 9 0 1 0 4 16.1L2 22Z"/></symbol>
<symbol id="i-send" viewBox="0 0 24 24"><path d="M14.536 21.686a.5.5 0 0 0 .937-.024l6.5-19a.496.496 0 0 0-.635-.635l-19 6.5a.5.5 0 0 0-.024.937l7.93 3.18a2 2 0 0 1 1.112 1.11z"/><path d="m21.854 2.147-10.94 10.939"/></symbol>
<symbol id="i-phone" viewBox="0 0 24 24"><path d="M13.832 16.568a1 1 0 0 0 1.213-.303l.355-.465A2 2 0 0 1 17 15h3a2 2 0 0 1 2 2v3a2 2 0 0 1-2 2A18 18 0 0 1 2 4a2 2 0 0 1 2-2h3a2 2 0 0 1 2 2v3a2 2 0 0 1-.8 1.6l-.468.351a1 1 0 0 0-.292 1.233 14 14 0 0 0 6.392 6.384"/></symbol>
<symbol id="i-plug" viewBox="0 0 24 24"><path d="M12 22v-5"/><path d="M9 8V2"/><path d="M15 8V2"/><path d="M18 8v5a4 4 0 0 1-4 4h-4a4 4 0 0 1-4-4V8Z"/></symbol>
<symbol id="i-plat-telegram" viewBox="0 0 24 24"><path d="m22 2-7 20-4-9-9-4Z"/><path d="M22 2 11 13"/></symbol>
<symbol id="i-plat-whatsapp" viewBox="0 0 24 24"><path d="M12 3a9 9 0 0 0-7.7 13.6L3 21l4.6-1.2A9 9 0 1 0 12 3z"/><path d="M9.1 7.9c-.5 1.3-.1 2.7.9 4 1 1.2 2.5 2 3.8 2.2.7.1 1.3-.1 1.7-.4l.4-.4a.6.6 0 0 0 0-.8l-1.3-1.2a.6.6 0 0 0-.8 0l-.4.4c-.7-.3-1.5-1-2-1.8l.4-.4a.6.6 0 0 0 0-.8L10.6 7.9a.6.6 0 0 0-.8 0z"/></symbol>
<symbol id="i-plat-wechat" viewBox="0 0 24 24"><path d="M9.3 3.8C5.8 3.8 3 6.2 3 9.2c0 1.7.9 3.1 2.3 4l-.6 2 2.4-1c.7.2 1.4.3 2.2.3"/><path d="M14.7 8.6c3.3 0 6 2.2 6 5s-2.7 5-6 5c-.5 0-1-.1-1.5-.2l-2.3.9.5-1.7c-1.6-.9-2.7-2.3-2.7-4 0-2.8 2.7-5 6-5z"/></symbol>
<symbol id="i-plat-wecom" viewBox="0 0 24 24"><path d="M21 11.5c0-3.6-4-6.5-9-6.5s-9 2.9-9 6.5c0 1.9 1.1 3.6 2.8 4.7L5 19.5l3.4-1.5c.8.2 1.7.3 2.6.3 5 0 9-2.9 9-6.8z"/><path d="m8.8 11.3 2.1 2.1 4.2-4.2"/></symbol>
<symbol id="i-plat-discord" viewBox="0 0 24 24"><path d="M7.5 5.9C9 5.4 10.5 5.2 12 5.2s3 .2 4.5.7c2 .8 3.3 3 3.8 6.3.2 1.2.2 2.3 0 3.1-1.4 1-2.8 1.7-4.2 2l-.8-1.5c-.9.2-1.8.3-2.8.3s-1.9-.1-2.8-.3l-.8 1.5c-1.4-.3-2.8-1-4.2-2-.2-.8-.2-1.9 0-3.1.5-3.3 1.8-5.5 3.8-6.3z"/><path d="M9.7 12h.01"/><path d="M14.3 12h.01"/></symbol>
<symbol id="i-plat-line" viewBox="0 0 24 24"><path d="M21 10.9c0 3.5-4 6.3-9 6.3-1 0-2-.1-2.9-.3L5 18.7l.9-2.9C4.1 14.7 3 12.9 3 10.9 3 7.4 7 4.6 12 4.6s9 2.8 9 6.3z"/><path d="M8.6 9v3.6h2.2"/><path d="M13.2 9v3.6"/></symbol>
<symbol id="i-plat-feishu" viewBox="0 0 24 24"><path d="M20 4c-8.5.5-14 5-15.5 12.5l-.5 3.5 3.3-.6C15 18 19 12.5 20 4z"/><path d="M6 17c3-4.5 7-8 12-10"/></symbol>
<symbol id="i-plat-generic" viewBox="0 0 24 24"><circle cx="12" cy="12" r="9"/><path d="M3.6 9.2h16.8"/><path d="M3.6 14.8h16.8"/><path d="M12 3a14 14 0 0 1 0 18"/><path d="M12 3a14 14 0 0 0 0 18"/></symbol>
</defs></svg>"""


CONSOLE_THEME_JS = """
(function(){
 var K='seller-workspace-theme';var r=document.documentElement;
 function apply(m){if(m==='dark'){r.setAttribute('data-theme','dark');}else{r.removeAttribute('data-theme');}}
 var saved=null;try{saved=localStorage.getItem(K);}catch(e){}
 var sysDark=false;try{sysDark=window.matchMedia('(prefers-color-scheme: dark)').matches;}catch(e){}
 apply(saved||(sysDark?'dark':'light'));
 document.addEventListener('click',function(e){
   var b=e.target.closest?e.target.closest('#themeToggle'):null;if(!b){return;}
   var nowDark=r.getAttribute('data-theme')==='dark';
   r.classList.add('theme-anim');apply(nowDark?'light':'dark');
   try{localStorage.setItem(K,nowDark?'light':'dark');}catch(err){}
   setTimeout(function(){r.classList.remove('theme-anim');},260);
 });
 document.addEventListener('keydown',function(e){
   if(e.key!=='d'&&e.key!=='D'){return;}
   var t=e.target;if(t&&(t.tagName==='INPUT'||t.tagName==='TEXTAREA'||t.isContentEditable)){return;}
   if(e.metaKey||e.ctrlKey||e.altKey){return;}
   var btn=document.getElementById('themeToggle');if(btn){btn.click();}
 });
})();
"""


CONSOLE_NAV_JS = """
// 侧栏待办数字：每 5 秒轮询一次 /admin/nav-counts，只在数字真的变化时更新
// （只动导航上的数字，不触碰页面正文）；变化时做机械表日历跳转式翻牌动画；
// 系统启用"减少动态效果"时直接切换，不播动画。
(function () {
  var POLL_INTERVAL_MS = 5000;
  var FLIP_MS = 120;
  // 告急数字（加粗 + 放大 + 红）：与 app.py 的 HOT_COUNT_KEYS 保持一致
  var HOT_KEYS = { tasks: true, sales: true };
  var prefersReduce = false;
  try { prefersReduce = window.matchMedia('(prefers-reduced-motion: reduce)').matches; } catch (error) {}
  function flipIn(num, text) {
    num.textContent = text;
    if (prefersReduce) { return; }
    num.classList.add('flip-in');
    void num.offsetWidth;
    num.classList.remove('flip-in');
  }
  function flipSwap(num, text) {
    if (prefersReduce) { num.textContent = text; return; }
    num.classList.add('flip-out');
    setTimeout(function () {
      num.textContent = text;
      num.classList.remove('flip-out');
      num.classList.add('flip-in');
      void num.offsetWidth;
      num.classList.remove('flip-in');
    }, FLIP_MS);
  }
  function applyCount(badge, value) {
    var num = badge.querySelector('.n');
    var prev = parseInt(badge.getAttribute('data-count') || '0', 10) || 0;
    var hotNow = value > 0 && HOT_KEYS[badge.getAttribute('data-nav-count')] === true;
    if (value === prev && (value > 0) === (!badge.hidden) && hotNow === badge.classList.contains('hot')) { return; }
    badge.setAttribute('data-count', String(value));
    badge.classList.toggle('hot', hotNow);
    if (value <= 0) {
      if (badge.hidden) { return; }
      if (prefersReduce) { num.textContent = ''; badge.hidden = true; return; }
      num.classList.add('flip-out');
      setTimeout(function () { num.textContent = ''; num.classList.remove('flip-out'); badge.hidden = true; }, FLIP_MS);
      return;
    }
    var text = value > 99 ? '99+' : String(value);
    var appearing = badge.hidden;
    badge.hidden = false;
    if (appearing) { flipIn(num, text); } else { flipSwap(num, text); }
  }
  function pollNavCounts() {
    fetch('/admin/nav-counts', { headers: { 'Accept': 'application/json' }, cache: 'no-store' })
      .then(function (response) { return response.ok ? response.json() : null; })
      .then(function (data) {
        if (!data || !data.counts) { return; }
        var badges = document.querySelectorAll('.side-count[data-nav-count]');
        for (var index = 0; index < badges.length; index += 1) {
          var badge = badges[index];
          var value = Number(data.counts[badge.getAttribute('data-nav-count')] || 0);
          if (!isFinite(value)) { value = 0; }
          applyCount(badge, value);
        }
      })
      .catch(function () { /* 静默：下一轮重试 */ });
  }
  setInterval(pollNavCounts, POLL_INTERVAL_MS);
  document.addEventListener('visibilitychange', function () { if (!document.hidden) { pollNavCounts(); } });
})();
"""

# 侧栏"告急"数字（加粗 + 放大 + 红）：人工任务=有人正在等答复；逾期跟进=已逾期未处理。
# 前端 CONSOLE_NAV_JS 的 HOT_KEYS 必须与本集合保持一致。
HOT_COUNT_KEYS = {"tasks", "sales"}


CONSOLE_DOCK_JS = """
// 侧栏模块磁吸放大（macOS Dock 手法竖向移植）：
// 指针靠近哪一项，哪一项平滑放大——行高增长 + 图标弹性放大，下方条目被自然顶开；
// 目标尺寸按指针到条目中心的距离线性衰减（与参考组件一致的磁吸手感），用弹簧物理平滑
// （参数沿用参考组件默认值）；指针离开侧栏整列弹回。
// 纯原生实现（本项目无 React/framer-motion）；触屏（无 hover 指针）与系统"减少动态效果"时不启用。
(function () {
  var links = Array.prototype.slice.call(document.querySelectorAll('.side-nav .side-link'));
  if (!links.length) { return; }
  var reduce = false, fine = false;
  try {
    reduce = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
    fine = window.matchMedia('(hover: hover) and (pointer: fine)').matches;
  } catch (error) { /* 无法判定媒体能力时不启用 */ }
  if (reduce || !fine) { return; }

  var DOCK_GROW = 12;   // 最近处行高增量（px）：36 → 48
  var DOCK_RANGE = 120; // 磁吸影响半径（px）：超出此距离的条目保持原尺寸
  var DOCK_ICON_GROW = 0.35; // 图标随放大系数增加的比例（1 → 1.35）
  var DOCK_SPRING = { mass: 0.1, stiffness: 150, damping: 12 }; // 参考组件默认弹簧参数

  var sidebar = document.getElementById('console-side');
  if (!sidebar) { return; }
  var items = links.map(function (link) {
    var base = link.offsetHeight;
    link.style.height = base + 'px';
    return { link: link, icon: link.querySelector('.ic'), base: base, x: 0, v: 0, t: 0 };
  });
  var pointerY = null;
  var raf = null;
  var last = 0;

  function computeTargets() {
    for (var i = 0; i < items.length; i += 1) {
      var item = items[i];
      var rect = item.link.getBoundingClientRect();
      var center = rect.top + rect.height / 2;
      var distance = pointerY === null ? Infinity : Math.abs(pointerY - center);
      // 与参考组件一致：半径内线性衰减，半径外为 0
      item.t = distance >= DOCK_RANGE ? 0 : 1 - distance / DOCK_RANGE;
    }
  }

  function frame(now) {
    var dt = Math.min(Math.max((now - last) / 1000, 0.001), 0.1);
    last = now;
    var steps = Math.max(1, Math.ceil(dt / (1 / 120)));
    var h = dt / steps;
    var busy = false;
    for (var i = 0; i < items.length; i += 1) {
      var item = items[i];
      for (var s = 0; s < steps; s += 1) {
        var accel = (-DOCK_SPRING.stiffness * (item.x - item.t) - DOCK_SPRING.damping * item.v) / DOCK_SPRING.mass;
        item.v += accel * h;
        item.x += item.v * h;
      }
      if (Math.abs(item.x - item.t) > 0.001 || Math.abs(item.v) > 0.001) { busy = true; }
      item.link.style.height = (item.base + DOCK_GROW * item.x) + 'px';
      if (item.icon) { item.icon.style.transform = 'scale(' + (1 + DOCK_ICON_GROW * item.x) + ')'; }
    }
    raf = busy ? requestAnimationFrame(frame) : null;
  }

  function kick() {
    if (raf !== null) { return; }
    last = performance.now();
    raf = requestAnimationFrame(frame);
  }

  sidebar.addEventListener('mousemove', function (event) {
    pointerY = event.clientY;
    computeTargets();
    kick();
  });
  sidebar.addEventListener('mouseleave', function () {
    pointerY = null;
    computeTargets();
    kick();
  });
  var scroller = sidebar.querySelector('.side-nav');
  if (scroller) {
    scroller.addEventListener('scroll', function () {
      if (pointerY === null) { return; }
      computeTargets();
      kick();
    });
  }
})();
"""


THEME_TOGGLE_SVG = (
    "<svg class='tt-sun' viewBox='0 0 24 24' aria-hidden='true'><circle cx='12' cy='12' r='4'/><path d='M12 2v2'/><path d='M12 20v2'/>"
    "<path d='m4.93 4.93 1.41 1.41'/><path d='m17.66 17.66 1.41 1.41'/><path d='M2 12h2'/><path d='M20 12h2'/>"
    "<path d='m6.34 17.66-1.41 1.41'/><path d='m19.07 4.93-1.41 1.41'/></svg>"
    "<svg class='tt-moon' viewBox='0 0 24 24' aria-hidden='true'><path d='M12 3a6 6 0 0 0 9 9 9 9 0 1 1-9-9'/></svg>"
)


def svg_icon(name: str) -> str:
    """引用图标 sprite 中的线性图标。"""
    return f"<svg class='ic' aria-hidden='true'><use href='#i-{name}'></use></svg>"


def admin_permission_area(path: str) -> tuple[str, str, str]:
    """按路径给出权限拦截提示的三要素：操作 / 所需权限 / 设计原因（V1-01：账号两类型 + 模块授权制）。"""
    module_why = "该模块未向你的账号授权；如需使用，请联系企业管理员在「成员管理」中审批并开通。"
    areas = (
        ("/admin/users", "访问「成员管理」", "管理员", "成员账号与模块授权关系到企业数据隔离，成员管理只对管理员开放。"),
        ("/admin/settings", "访问「企业设置」", "管理员", "企业设置会影响全企业买家的表单与跟进流程，只对管理员开放。"),
        ("/admin/channels", "访问「渠道接入」", "管理员", "渠道接入包含对外连接与凭证引用配置，只对管理员开放。"),
        ("/admin/audit", "访问「操作记录」", "管理员", "操作记录包含全企业范围的操作轨迹，只对管理员开放。"),
        ("/admin/analytics", "访问「分析报表」", "管理员", "分析报表汇总全企业买家数据并提供导出入口，只对管理员开放。"),
        ("/admin/export", "导出数据", "管理员", "数据导出包含买方联系信息，只对管理员开放。"),
        ("/admin/material-tasks", "资料补充任务", "管理员", "资料补充任务的登记、分派与完成只对管理员开放。"),
        ("/admin/sites", "访问「网站 API 接入」", "模块权限「网站 API 接入」", module_why),
        ("/admin/products", "访问「产品管理」", "模块权限「产品管理」", module_why),
        ("/admin/tasks", "访问「人工任务」", "模块权限「人工任务」", module_why),
        ("/admin/demo", "访问「演示工具」", "管理员", "演示工具为部署级开关，只对管理员开放。"),
        ("/admin/lowara", "访问「演示工具」", "管理员", "演示工具为部署级开关，只对管理员开放。"),
        ("/admin/sales", "访问「销售工作台」", "模块权限「销售工作台」", module_why),
        ("/admin/leads", "访问「销售工作台」", "模块权限「销售工作台」", module_why),
        ("/admin/rfqs", "访问「RFQ 询价」", "模块权限「RFQ 询价」", module_why),
        ("/admin/creative", "访问「产品展示页」", "模块权限「创意中心」", module_why),
        ("/admin/inbox", "访问「会话收件箱」", "模块权限「会话收件箱」", module_why),
    )
    for prefix, area, required, why in areas:
        if path.startswith(prefix):
            return area, required, why
    return "访问管理功能", "管理员", "该功能只对管理员开放。"


def duplicate_leads_section(db: Database, tenant_id: str, lead: sqlite3.Row, is_admin: bool) -> str:
    """重复线索提示区块（V1-08）：提示同邮箱/同公司线索；管理员可人工合并；列出已并入记录。"""
    hints = db.find_duplicate_lead_hints(tenant_id, lead["id"])
    related = db.merged_related_leads(tenant_id, lead["id"])
    if not hints and not related:
        return ""

    def link(item_id: str, kind: str, rfq_id: str | None, label: str) -> str:
        href = f"/admin/sales/{rfq_id}" if kind == "rfq" and rfq_id else f"/admin/leads/{item_id}"
        return f"<a href='{href}'>{esc(label)}</a>"

    parts = [
        "<section class='card' style='margin-top:18px' id='duplicates'><div class='panel-title'><h2>重复线索提示</h2>"
        "<span class='muted'>仅提示，系统不会自动合并</span></div>"
    ]
    if hints:
        parts.append("<div class='simple-list'>")
        for hint in hints:
            kind_label = "待培育" if hint["kind"] == "nurture" else "RFQ"
            parts.append(
                "<div class='source'><span>"
                + link(hint["id"], hint["kind"], hint["rfq_id"], f"{hint['name'] or hint['company'] or hint['id'][:8]}（{kind_label}）")
                + f"<br><span class='field-state'>匹配：{'、'.join(hint['matched_by'])} · 邮箱：{esc(hint['email'] or '—')} · 公司：{esc(hint['company'] or '—')}</span></span></div>"
            )
        parts.append("</div>")
        if is_admin:
            options = "".join(
                f"<option value='{hint['id']}'>{esc(hint['name'] or hint['company'] or hint['id'][:8])}（{'待培育' if hint['kind'] == 'nurture' else 'RFQ'} · {'、'.join(hint['matched_by'])}）</option>"
                for hint in hints
            )
            parts.append(
                f"<form method='post' action='/admin/leads/{lead['id']}/merge'><label>合并一条重复线索到本线索（管理员；原消息、RFQ、人工任务与操作历史全部保留）</label>"
                f"<select name='secondary_lead_id'>{options}</select><button class='danger'>确认合并</button></form>"
            )
    else:
        parts.append("<p class='muted'>未发现同邮箱或同公司名的其他线索。</p>")
    if related:
        parts.append("<p class='field-state' style='margin-top:10px'>已并入本线索：</p><div class='simple-list'>")
        for row in related:
            kind_label = "待培育" if row["kind"] == "nurture" else "RFQ"
            parts.append(
                "<div class='source'><span>"
                + link(row["id"], row["kind"], row["rfq_id"], row["contact_name"] or row["company"] or row["id"][:8])
                + f"<br><span class='field-state'>类型：{kind_label} · 并入时间：{esc(row['merged_at'] or '')}</span></span></div>"
            )
        parts.append("</div>")
    parts.append("</section>")
    return "".join(parts)


def merged_into_banner(db: Database, tenant_id: str, lead: sqlite3.Row) -> str:
    """线索已并入其他线索时的说明横幅（保留全部记录，仅展示层归并）。"""
    target_id = (lead["merged_into"] or "").strip()
    if not target_id:
        return ""
    target = db.get_lead(tenant_id, target_id)
    if not target:
        return "<div class='notice'>该线索已并入其他线索；全部记录与历史保留。</div>"
    href = f"/admin/sales/{target['rfq_id']}" if target["kind"] == "rfq" and target["rfq_id"] else f"/admin/leads/{target['id']}"
    label = target["contact_name"] or target["company"] or target["id"][:8]
    return (
        f"<div class='notice'>该线索已并入 <a href='{href}'>{esc(label)}</a>"
        f"（{'待培育' if target['kind'] == 'nurture' else 'RFQ'} 线索 · {esc(lead['merged_at'] or '')}）；全部记录与历史保留。</div>"
    )


def permission_notice(area: str, role_label: str, required: str, why: str) -> str:
    """权限错误卡：复刻朋友设计稿的权限弹窗（模态样式），说明"为什么被拦下"。"""
    return f"""<div class='perm-wrap'><section class='dlg' role='alertdialog' aria-labelledby='perm-title'>
<div class='dlg-head'><span class='dlg-icon' aria-hidden='true'>{svg_icon('shield')}</span><div><h3 class='dlg-title' id='perm-title'>你无法执行这个操作</h3><p class='dlg-sub'>这不是操作错误，而是权限范围限制：当前账号没有访问此管理功能的权限。系统已阻止本次操作，不会产生任何改动。</p></div></div>
<div class='dlg-body'><div class='dlg-reason'><dl>
<dt>操作</dt><dd>{esc(area)}</dd>
<dt>你的角色</dt><dd><strong>{esc(role_label)}</strong></dd>
<dt>所需权限</dt><dd><strong>{esc(required)}</strong></dd>
<dt>为什么这样设计</dt><dd>{esc(why)}</dd>
</dl></div></div>
<div class='dlg-foot'><a class='button secondary' href='/admin'>返回可访问页面</a><a class='button' href='/login'>切换账号</a></div>
</section></div>"""


def auth_layout(title: str, body: str) -> str:
    """登录等未登录页面的独立外壳：与卖家控制台同一视觉系统（不含侧栏）。"""
    return f"""<!doctype html>
<html lang='zh-CN'>
<head>
<meta charset='utf-8'><meta name='viewport' content='width=device-width, initial-scale=1'>
<title>{esc(title)} · Growave</title>
<link rel='preconnect' href='https://fonts.googleapis.com'><link rel='preconnect' href='https://fonts.gstatic.com' crossorigin><link href='https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500;600&display=swap' rel='stylesheet'>
<style>
{BASE_CSS}
{CONSOLE_CSS}
</style>
<script>{CONSOLE_THEME_JS}</script>
</head><body class='auth-body'>
{CONSOLE_ICONS}
{body}
</body></html>"""


def console_layout(title: str, body: str, active: str, *, lang: str = "zh", path: str = "/admin", actor: str = "", role: str = "", tenant: str = "", nav_counts: dict[str, int] | None = None, nav_keys: set[str] | None = None, inbox_items: list[tuple[str, str, str, str]] | None = None) -> str:
    """卖家控制台外壳：左侧按业务分组的工具架 + 右侧工作区（顶栏面包屑）。"""
    groups = (
        ("我的工作", (("admin", "/admin", "home", "管理概览"),)),
        ("客户跟进", (
            ("sales", "/admin/sales", "inbox", "销售工作台"),
            ("rfqs", "/admin/rfqs", "file", "RFQ 询价"),
            ("tasks", "/admin/tasks", "tool", "人工任务"),
        )),
        ("会话收件箱", tuple(inbox_items or ())),
        ("产品经营", (("products", "/admin/products", "box", "产品管理"),)),
        ("创意中心", (("creative", "/admin/creative", "palette", "产品展示页"),)),
        ("经营分析", (("analytics", "/admin/analytics", "chart", "分析报表"),)),
        ("团队与接入", (
            ("users", "/admin/users", "users", "成员管理"),
            ("sites", "/admin/sites", "globe", "网站 API 接入"),
        )),
        ("设置与治理", (
            ("settings", "/admin/settings", "sliders", "企业设置"),
            ("channels", "/admin/channels", "plug", "渠道接入"),
            ("audit", "/admin/audit", "history", "操作记录"),
        )),
    )
    group_of = {"admin": "我的工作", "sales": "客户跟进", "rfqs": "客户跟进", "tasks": "客户跟进", "products": "产品经营", "creative": "创意中心", "users": "团队与接入", "sites": "团队与接入", "settings": "设置与治理", "channels": "设置与治理", "audit": "设置与治理", "analytics": "经营分析"}
    role_label = "管理员" if role == "admin" else ("员工" if role else "未登录")
    lang_target = f"{esc(path)}?lang={'en' if lang == 'zh' else 'zh'}"
    counts = nav_counts or {}
    def side_count_html(key: str) -> str:
        """待办数字（参照好友参考稿方法）：纯数字随行文字色；告急类加粗放大变红；
        0 时隐藏但保留占位（轮询发现变化时原地翻牌出现）。"""
        value = counts.get(key, 0)
        hot = " hot" if value and key in HOT_COUNT_KEYS else ""
        hidden = "" if value else " hidden"
        text = "99+" if value > 99 else (str(value) if value else "")
        return f"<span class='side-count{hot}' data-nav-count='{key}' data-count='{value}'{hidden}><span class='n'>{text}</span></span>"
    sections = []
    for heading, items in groups:
        if heading == "会话收件箱":
            visible_items = tuple(items)  # 由 console_page 按权限与数据预过滤（含"已接入未收消息"平台空态）
        else:
            visible_items = tuple(item for item in items if nav_keys is None or item[0] in nav_keys)
        if not visible_items:
            continue
        links = "".join(
            f"<a class='side-link{' active' if key == active else ''}' href='{url}'>{svg_icon(icon)}{label}{side_count_html(key)}</a>"
            for key, url, icon, label in visible_items
        )
        sections.append(f"<div class='side-heading'>{heading}</div>{links}")
    crumb = f"{group_of.get(active, '会话收件箱' if str(active).startswith('inbox_') else '卖家系统')} / <b>{esc(title)}</b>"
    account = f"当前账号：{esc(actor)}（{esc(role_label)}）" if actor else "未登录"
    tenant_name = tenant or "当前企业"
    return f"""<!doctype html>
<html lang='{'en' if lang == 'en' else 'zh-CN'}'>
<head>
<meta charset='utf-8'><meta name='viewport' content='width=device-width, initial-scale=1'>
<title>{esc(title)} · Growave</title>
<link rel='preconnect' href='https://fonts.googleapis.com'><link rel='preconnect' href='https://fonts.gstatic.com' crossorigin><link href='https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500;600&display=swap' rel='stylesheet'>
<style>
{BASE_CSS}
{CONSOLE_CSS}
</style>
<script>{CONSOLE_THEME_JS}</script>
</head><body class='console-body'>
{CONSOLE_ICONS}
<div class='console'>
<aside class='side' id='console-side' aria-label='卖家系统导航'>
<div class='side-brand'><img class='brand-mark' src='/assets/brand.png' alt=''><div><strong>Growave</strong><small>SELLER CONSOLE</small></div></div>
<div class='side-tenant'><span class='tenant-avatar' aria-hidden='true'>{esc(tenant_name[:1])}</span><span>{esc(tenant_name)}</span></div>
<nav class='side-nav'>{''.join(sections)}</nav>
<div class='side-foot'>{account}<br><a href='/inquiry' target='_blank'>打开买方演示页 ↗</a></div>
</aside>
<div class='shell'>
<header class='topbar'><button class='menu-btn' id='console-menu' aria-label='打开导航'>{svg_icon('menu')}</button><span class='crumb'>{crumb}</span><span class='grow'></span><button class='theme-toggle' id='themeToggle' type='button' title='切换浅色 / 深色（快捷键 D）' aria-label='切换主题'>{THEME_TOGGLE_SVG}</button><a href='{lang_target}'>{'English' if lang == 'zh' else '中文'}</a><a href='/logout'>退出登录</a></header>
<main>{body}</main>
</div>
</div>
<script>document.getElementById('console-menu').addEventListener('click',function(){{document.getElementById('console-side').classList.toggle('open');}});</script>
<script>{CONSOLE_NAV_JS}</script>
<script>{CONSOLE_DOCK_JS}</script>
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
    all_files: list[tuple[str, str, bytes]] = []
    for part in message.iter_parts():
        name = part.get_param("name", header="content-disposition")
        if not name:
            continue
        filename = part.get_filename()
        content = part.get_payload(decode=True) or b""
        result[name] = content if filename else content.decode(part.get_content_charset() or "utf-8", errors="replace")
        if filename:
            result[f"{name}__filename"] = filename
            all_files.append((name, filename, content))
    # 批量上传：保留全部同名文件部件（单文件读取路径按既有语义取最后一个）。
    result["__files__"] = all_files
    return result


# 演示工具端点（正式化清单 F-1～F-3）：只在演示模式下可用（demo_tools=True，默认关闭）。
DEMO_TOOL_PATHS = {"/admin/products/seed", "/admin/products/seed-lowara", "/admin/demo/seed", "/admin/lowara/seed"}

# V1-01 账号与模块权限：账号只有两种（admin=管理员 / employee=普通员工）；员工的可访问模块由管理员逐项授权。
GRANTABLE_MODULES = (("products", "产品管理"), ("tasks", "人工任务"), ("rfqs", "RFQ 询价"), ("sales", "销售工作台"), ("sites", "网站 API 接入"), ("creative", "创意中心"), ("inbox", "会话收件箱"))
GRANTABLE_MODULE_KEYS = tuple(key for key, _label in GRANTABLE_MODULES)
MODULE_LABELS = dict(GRANTABLE_MODULES)
# 旧角色名兼容（旧库迁移与既有测试）：等价于"员工 + 对应模块授权"，能力与迁移前一致。
LEGACY_ROLE_MODULES = {"sales": ("rfqs", "sales"), "engineer": ("tasks", "rfqs", "sales")}

# ── V1-04 外部渠道接入（WhatsApp / LangBot）：渠道账户与会话收件箱 ──────────────
# 渠道消息与网页聊天共用同一批会话/消息对象；渠道账户只存公开配置，
# 机密凭证（bot 令牌 / Twilio 凭据）一律走本机环境变量读取，不入库、不落 Git、不回显。
CHANNEL_KEYS = ("whatsapp", "langbot", "whatsapp_personal")
CHANNEL_LABELS = {"whatsapp": "WhatsApp", "langbot": "LangBot", "whatsapp_personal": "WhatsApp（个人版）"}
# 会话收件箱的子模块（导航分别展示；web=网站聊天；langbot=LangBot 接入的社交平台）
# ── 收件箱来源平台（2026-10 信息架构调整）──
# 模块 = 真实来源平台：LangBot 仅作信息路由，平台侧按事件的 adapter（已归一）把会话归入对应平台模块；
# whatsapp 为平台原生通道（Twilio 直连）；registry 之外的来源自动生成通用模块；已接入但无消息的平台显示空态指引。
INBOX_PLATFORM_REGISTRY = (
    {"slug": "telegram", "label": "Telegram", "icon": "plat-telegram", "adapters": ("telegram",), "order": 10},
    {"slug": "whatsapp", "label": "WhatsApp", "icon": "phone", "adapters": (), "native": True, "order": 20},
    {"slug": "whatsapp_personal", "label": "WhatsApp（个人版）", "icon": "phone", "adapters": ("whatsapp_personal",), "native": True, "order": 25},
    {"slug": "wechat", "label": "微信", "icon": "plat-wechat", "adapters": ("微信", "openclaw-weixin", "openclaw", "wechatpad", "wechatpadpro"), "order": 30},
    {"slug": "wecom", "label": "企业微信", "icon": "plat-wecom", "adapters": ("企业微信", "wecom", "wecombot"), "order": 40},
    {"slug": "wecomcs", "label": "微信客服", "icon": "plat-wecom", "adapters": ("微信客服", "wecomcs"), "order": 45},
    {"slug": "discord", "label": "Discord", "icon": "plat-discord", "adapters": ("discord",), "order": 50},
    {"slug": "line", "label": "LINE", "icon": "plat-line", "adapters": ("line",), "order": 60},
    {"slug": "feishu", "label": "飞书", "icon": "plat-feishu", "adapters": ("飞书", "lark"), "order": 70},
)
_LANGBOT_PLATFORMS_CACHE: dict = {"at": 0.0, "items": []}


def _platform_key(raw: str) -> str:
    return str(raw or "").strip().lower()


def inbox_platform_entry(raw_adapter: str) -> dict:
    """adapter（原样或归一后）→ 平台注册表条目；未知来源自动生成通用条目（确定性 slug）。"""
    key = _platform_key(raw_adapter)
    if not key:
        return {"slug": "other", "label": "其他来源", "icon": "plat-generic", "adapters": ("",), "order": 999}
    for entry in INBOX_PLATFORM_REGISTRY:
        candidate = {_platform_key(a) for a in entry["adapters"]} | {entry["label"].lower(), entry["slug"]}
        if key in candidate:
            return entry
    label = normalize_platform_label(raw_adapter) or "其他来源"
    ascii_slug = re.sub(r"[^a-z0-9]+", "-", key).strip("-")
    slug = ascii_slug or "p-" + hashlib.sha1(key.encode("utf-8")).hexdigest()[:8]
    return {"slug": slug, "label": label, "icon": "plat-generic", "adapters": (key,), "order": 500}


def langbot_enabled_platforms(timeout: int = 2) -> list:
    """LangBot 已启用的机器人适配器（用于“已接入但无消息”平台模块）；30 秒缓存，失败静默为空。"""
    now = time.time()
    if now - _LANGBOT_PLATFORMS_CACHE["at"] < 30:
        return list(_LANGBOT_PLATFORMS_CACHE["items"])
    items: list = []
    api_key = os.environ.get("LANGBOT_API_KEY", "").strip()
    if api_key:
        try:
            base = channel_api_base("langbot", {})
            request = urllib_request.Request(base + "/api/v1/platform/bots", headers={"X-API-Key": api_key}, method="GET")
            with urllib_request.urlopen(request, timeout=timeout) as response:
                payload = json.loads(response.read().decode("utf-8"))
            for bot in ((payload.get("data") or {}).get("bots") or []):
                if isinstance(bot, dict) and bot.get("enable") and bot.get("adapter"):
                    items.append(str(bot["adapter"]))
        except Exception:
            items = []
    items = sorted(set(items))
    _LANGBOT_PLATFORMS_CACHE["at"] = now
    _LANGBOT_PLATFORMS_CACHE["items"] = items
    return list(items)


def inbox_sections(db, tenant_id: str) -> list:
    """收件箱模块列表：网站聊天 + 来源平台（有数据 ∪ LangBot 已接入 ∪ 原生渠道）；含过滤所需元数据。"""
    aggregate: dict = {}

    def register(raw_adapter, has_data: bool = False, last_at: str = "") -> None:
        entry = inbox_platform_entry(raw_adapter)
        item = aggregate.setdefault(entry["slug"], {"entry": entry, "has_data": False, "last_at": ""})
        item["has_data"] = item["has_data"] or has_data
        if last_at and last_at > (item["last_at"] or ""):
            item["last_at"] = last_at

    rows = db.conn.execute(
        "SELECT CASE WHEN json_valid(i.platform_meta) THEN json_extract(i.platform_meta,'$.adapter') END AS adapter, MAX(c.updated_at) AS last_at "
        "FROM channel_identities i JOIN conversations c ON c.id = i.conversation_id "
        "WHERE i.tenant_id=? AND c.source='langbot' GROUP BY 1",
        (tenant_id,),
    ).fetchall()
    for row in rows:
        register(row["adapter"], has_data=True, last_at=row["last_at"] or "")
    for adapter in langbot_enabled_platforms():
        register(adapter)
    for entry in INBOX_PLATFORM_REGISTRY:
        if not entry.get("native"):
            continue
        register(entry["label"])
        native_count = db.conn.execute(
            "SELECT COUNT(*) AS n FROM conversations WHERE tenant_id=? AND source=?", (tenant_id, entry["slug"])
        ).fetchone()
        if native_count and native_count["n"]:
            aggregate[entry["slug"]]["has_data"] = True

    with_data = [item for item in aggregate.values() if item["has_data"]]
    without_data = [item for item in aggregate.values() if not item["has_data"]]
    with_data.sort(key=lambda item: item["last_at"], reverse=True)
    without_data.sort(key=lambda item: item["entry"].get("order", 999))
    sections: list = [{"slug": "web", "label": "网站聊天", "icon": "chat", "kind": "web", "adapters": (), "native_channel": "", "has_data": True}]
    for item in with_data + without_data:
        entry = item["entry"]
        aliases = {_platform_key(a) for a in entry["adapters"]} | {entry["label"].lower(), entry["slug"].lower()}
        sections.append({
            "slug": entry["slug"],
            "label": entry["label"],
            "icon": entry["icon"],
            "kind": "native" if entry.get("native") else "platform",
            "native_channel": entry["slug"] if entry.get("native") else "",
            "adapters": tuple(sorted(aliases)),
            "has_data": item["has_data"],
        })
    return sections

TWILIO_SECRET_ENV = ("TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_WHATSAPP_FROM")
META_SECRET_ENV = ("META_WHATSAPP_TOKEN", "META_PHONE_NUMBER_ID", "META_APP_SECRET", "META_VERIFY_TOKEN")
WHATSAPP_PERSONAL_SECRET_ENV = ("WHATSAPP_PERSONAL_TOKEN", "WHATSAPP_PERSONAL_API_KEY")
CHANNEL_SECRET_ENV = {
    "whatsapp": TWILIO_SECRET_ENV,
    "langbot": ("LANGBOT_API_KEY",),
    "whatsapp_personal": WHATSAPP_PERSONAL_SECRET_ENV,
}


def whatsapp_provider() -> str:
    """WhatsApp 通道承载方：twilio（默认，切片① 形状）/ meta（Meta 官方 Cloud API 形状）。"""
    return os.environ.get("WHATSAPP_PROVIDER", "twilio").strip().lower() or "twilio"


def whatsapp_guide_text() -> str:
    """渠道接入页指引文案（按承载方切换）。"""
    if whatsapp_provider() == "meta":
        return (
            "走 Meta 官方形态：Meta 开发者控制台创建应用并添加 WhatsApp 产品 → 把本页「收信地址」与验证令牌"
            "（本机变量 META_VERIFY_TOKEN）填入 Webhook 配置并订阅 messages 字段 → 把长期令牌与测试号 ID 写进"
            "本机环境变量 META_WHATSAPP_TOKEN / META_PHONE_NUMBER_ID → 回到这里填写显示名称并启用。"
        )
    return (
        "走 Twilio：开通 WhatsApp 沙箱（手机发送加入码即可参与测试）→ 把 Account SID、Auth Token、（whatsapp: 前缀的）"
        "发送号码写进本机环境变量 TWILIO_ACCOUNT_SID / TWILIO_AUTH_TOKEN / TWILIO_WHATSAPP_FROM → 回到这里填写显示名称并启用。"
        "把下方「收信地址」「状态回调地址」登记到 Twilio 的 Sandbox 设置或发件人配置（地址需公网可达，联调时用临时隧道；"
        "隧道域名可设本机变量 TWILIO_PUBLIC_BASE，本页即显示公网地址）。"
    )
# 模拟/测试用基地址（生产不设置即用官方默认）；适配器在后续切片接入。
CHANNEL_API_BASE_ENV = {"whatsapp": "TWILIO_API_BASE", "langbot": "LANGBOT_API_BASE", "whatsapp_personal": "WHATSAPP_PERSONAL_BASE"}


def channel_secret_status(channel: str) -> dict:
    """渠道凭据状态：只看环境变量“在不在”，绝不回显值（供渠道接入页展示）。

    whatsapp 按当前承载方检查：twilio（默认）看 TWILIO_*；meta 看 META_*。
    """
    if channel == "whatsapp":
        names = META_SECRET_ENV if whatsapp_provider() == "meta" else TWILIO_SECRET_ENV
    else:
        names = CHANNEL_SECRET_ENV.get(channel, ())
    missing = [name for name in names if not os.environ.get(name, "").strip()]
    return {"configured": not missing, "missing": missing}


# ── V1-04 切片③④：渠道全链路（收信 / 自动问答 / 人工转交 / 回复直达 / RFQ 填写链接） ──
# 收信经各渠道入口进入（LangBot Webhook / WhatsApp 回调），统一走 process_channel_message；
# 出站：发送适配器把已保存的消息投递到渠道（delivery_status = queued → sent / failed，失败原因可见）；
# 分流：与网页聊天同口径——工程师追问优先、暂停 AI 不自动答复、「人工」转交、「确认」确认工况并匹配、
#       提问样文本走有据问答（资料外给转人工提示）、其余走需求识别与整理。
CHANNEL_OFFICIAL_API_BASE = {"whatsapp": "https://api.twilio.com", "langbot": "http://127.0.0.1:5300", "whatsapp_personal": "http://127.0.0.1:3100"}
# 渠道内提示文案（入库即不可变：只描述可用动作，不作状态承诺）
CHANNEL_HUMAN_HINT = "如需工程师人工核实，请回复「人工」。"
CHANNEL_HUMAN_GUIDE = "还没有待核实的问题。请先发送您想核实的具体问题，再回复「人工」转交工程师。"


# ── Dify 问答引擎（第二波）：资料问答由 Dify 承担，平台仍是唯一主脑 ──────────
# 环境变量：DIFY_QA_URL（默认 http://127.0.0.1:8090/v1）、DIFY_QA_APP_KEY（问答应用密钥）；
# 知识库同步：DIFY_KB_DATASET_ID、DIFY_KB_API_KEY（DIFY_KB_URL 可选，默认从 DIFY_QA_URL 推导）。
# 机密凭证一律只从本机环境变量读取：页面不显示、数据库不保存、不进 Git。
DIFY_NO_ANSWER_SENTINEL = "[[NO_ANSWER]]"
DIFY_REASONING_MARKER = "<!--dify-deepseek-reasoning-->"


class DifyQAUnavailableError(RuntimeError):
    """Dify 问答引擎不可用（网络/超时/服务错误）；由调用方决定回落策略。"""


def dify_qa_config() -> tuple[str, str]:
    """问答应用配置：URL 与密钥都来自环境变量；未配置密钥时返回空。"""
    base = os.environ.get("DIFY_QA_URL", "http://127.0.0.1:8090/v1").strip().rstrip("/")
    key = os.environ.get("DIFY_QA_APP_KEY", "").strip()
    return base, key


def strip_reasoning_blocks(text: str) -> str:
    """剥掉模型推理块与渠道标记（Dify 回答 → 买方可见文本）。"""
    cleaned = re.sub(r"<think\b[^>]*>.*?</think\s*>", "", str(text or ""), flags=re.S | re.I)
    cleaned = re.sub(r"</?think\b[^>]*>", "", cleaned, flags=re.I)
    return cleaned.replace(DIFY_REASONING_MARKER, "").strip()


def _dify_retriever_resources(data: dict) -> list:
    """Dify 响应中的检索引用：兼容顶层 retriever_resources 与 1.17 的 metadata.retriever_resources。"""
    resources = data.get("retriever_resources")
    if not resources:
        meta = data.get("metadata")
        if isinstance(meta, dict):
            resources = meta.get("retriever_resources")
    return [item for item in resources or [] if isinstance(item, dict)]


def dify_ask_question(question: str, user_label: str, timeout: int = 45) -> dict:
    """调用 Dify 问答应用（阻塞模式）；返回 {status, answer, citations}；不可用抛 DifyQAUnavailableError。"""
    base, key = dify_qa_config()
    if not key:
        raise DifyQAUnavailableError("未配置 DIFY_QA_APP_KEY")
    payload = {
        "inputs": {}, "query": str(question), "response_mode": "blocking",
        "conversation_id": "", "user": str(user_label)[:64] or "channel",
    }
    try:
        data = channel_http_json(f"{base}/chat-messages", payload, timeout=timeout, headers={"Authorization": f"Bearer {key}"})
    except ChannelSendError as error:
        raise DifyQAUnavailableError(str(error)) from error
    answer = strip_reasoning_blocks(str(data.get("answer") or ""))
    if not answer or DIFY_NO_ANSWER_SENTINEL in answer:
        return {"status": "no_answer", "answer": "", "citations": []}
    citations = []
    for item in _dify_retriever_resources(data):
        position = item.get("position")
        citations.append({
            "label": "知识库",
            "filename": str(item.get("document_name") or "知识库资料"),
            "location": f"检索片段 {position if position is not None else 1}",
            "product_version": "",
            "field": str(item.get("dataset_name") or ""),
        })
    return {"status": "grounded", "answer": answer, "citations": citations}


def dify_kb_config() -> tuple[str, str, str]:
    """知识库同步配置：基地址、数据集 ID、服务密钥（后两个未配置时同步整体跳过）。"""
    base = os.environ.get("DIFY_KB_URL", os.environ.get("DIFY_QA_URL", "http://127.0.0.1:8090/v1")).strip().rstrip("/")
    if base.endswith("/v1"):
        base = base[: -len("/v1")]
    return base, os.environ.get("DIFY_KB_DATASET_ID", "").strip(), os.environ.get("DIFY_KB_API_KEY", "").strip()


def _dify_kb_request(base: str, api_key: str, method: str, path: str, payload: dict | None = None, timeout: int = 30) -> dict | None:
    """知识库服务 API 调用（create/update/delete 共用）；失败转 ChannelSendError。"""
    request = urllib_request.Request(
        f"{base}{path}",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None,
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"},
        method=method,
    )
    try:
        with urllib_request.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8")
    except urllib_error.HTTPError as error:
        raise ChannelSendError(f"知识库接口返回 HTTP {error.code}") from error
    except (urllib_error.URLError, TimeoutError, OSError) as error:
        raise ChannelSendError(f"知识库接口网络请求失败：{error}") from error
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as error:
        raise ChannelSendError("知识库接口返回内容无法解析") from error
    return data if isinstance(data, dict) else None


def collect_kb_documents(db: Database, tenant_id: str) -> list[dict]:
    """把「已核准且已发布」的产品资料整理为知识库文档；来源被禁用的字段不进入知识库。"""
    documents: list[dict] = []
    for product in db.list_products(tenant_id):
        if product["status"] == "withdrawn":
            continue
        version = db.get_published_version(tenant_id, product["id"])
        if not version:
            continue
        snapshot = json.loads(version["snapshot_json"])
        lines: list[str] = []
        use_case = str(product["use_case"] or "").strip() if "use_case" in product.keys() else ""
        if use_case:
            lines.append(f"用途：{use_case}")
        field_count = 0
        for field in snapshot.get("fields", []):
            if not isinstance(field, dict):
                continue
            value = str(field.get("value", "")).strip()
            if not value or source_is_restricted(db, tenant_id, field):
                continue
            unit = str(field.get("unit", "")).strip()
            label = str(field.get("label") or field.get("field_key") or "")
            lines.append(f"· {label}：{value}{(' ' + unit) if unit else ''}")
            field_count += 1
        if not field_count:
            continue
        text = "\n".join([f"【产品】{product['model']} · {product['name']}", *lines, f"资料版本：v{version['version']}（已核准字段 {field_count} 项）"])
        documents.append({"product_id": product["id"], "name": f"{product['model']} · {product['name']}", "text": text})
    return documents


def sync_kb_to_dify(db: Database, tenant_id: str, actor: str = "demo-admin", force: bool = False) -> dict:
    """已核准资料 → Dify 知识库（新增/更新/删除 + 内容哈希跳过）。返回统计；未配置时 configured=False 直接返回。"""
    base, dataset_id, api_key = dify_kb_config()
    stats = {"configured": bool(dataset_id and api_key), "created": 0, "updated": 0, "deleted": 0, "skipped": 0, "failed": 0, "error": ""}
    if not stats["configured"]:
        return stats
    documents = collect_kb_documents(db, tenant_id)
    current_ids = {doc["product_id"] for doc in documents}
    state_rows = {
        row["product_id"]: row
        for row in db.conn.execute("SELECT * FROM kb_sync_state WHERE tenant_id=?", (tenant_id,)).fetchall()
    }

    def upsert_state(product_id: str, document_id: str, digest: str, error: str) -> None:
        db.conn.execute(
            "INSERT INTO kb_sync_state(tenant_id, product_id, document_id, content_hash, synced_at, last_error) VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(tenant_id, product_id) DO UPDATE SET document_id=excluded.document_id, content_hash=excluded.content_hash, synced_at=excluded.synced_at, last_error=excluded.last_error",
            (tenant_id, product_id, document_id, digest, now_iso(), error),
        )

    for doc in documents:
        digest = hashlib.sha256(doc["text"].encode("utf-8")).hexdigest()
        state = state_rows.get(doc["product_id"])
        try:
            if state and state["document_id"] and state["content_hash"] == digest and not force:
                stats["skipped"] += 1
                continue
            if state and state["document_id"]:
                _dify_kb_request(base, api_key, "POST", f"/v1/datasets/{dataset_id}/documents/{state['document_id']}/update-by-text",
                                 {"name": doc["name"], "text": doc["text"], "process_rule": {"mode": "automatic"}})
                upsert_state(doc["product_id"], state["document_id"], digest, "")
                stats["updated"] += 1
            else:
                created = _dify_kb_request(base, api_key, "POST", f"/v1/datasets/{dataset_id}/document/create-by-text",
                                           {"name": doc["name"], "text": doc["text"], "indexing_technique": "high_quality", "process_rule": {"mode": "automatic"}})
                document_id = str(((created or {}).get("document") or {}).get("id") or "")
                if not document_id:
                    raise ChannelSendError("创建知识库文档未返回 document.id")
                upsert_state(doc["product_id"], document_id, digest, "")
                stats["created"] += 1
        except ChannelSendError as error:
            stats["failed"] += 1
            stats["error"] = str(error)[:200]
            upsert_state(doc["product_id"], (state["document_id"] if state else ""), (state["content_hash"] if state else ""), str(error)[:200])
        db.conn.commit()

    for product_id, state in state_rows.items():
        if product_id in current_ids or not state["document_id"]:
            continue
        try:
            _dify_kb_request(base, api_key, "DELETE", f"/v1/datasets/{dataset_id}/documents/{state['document_id']}")
            db.conn.execute("DELETE FROM kb_sync_state WHERE tenant_id=? AND product_id=?", (tenant_id, product_id))
            stats["deleted"] += 1
        except ChannelSendError as error:
            stats["failed"] += 1
            stats["error"] = str(error)[:200]
            db.conn.execute("UPDATE kb_sync_state SET last_error=?, synced_at=? WHERE tenant_id=? AND product_id=?", (str(error)[:200], now_iso(), tenant_id, product_id))
        db.conn.commit()

    db._audit(tenant_id, "kb_sync", "dify", "synced", actor,
              json.dumps({key: stats[key] for key in ("created", "updated", "deleted", "skipped", "failed")}, ensure_ascii=False))
    db.conn.commit()
    return stats


def try_sync_kb_quietly(db: Database, tenant_id: str, actor: str) -> None:
    """发布/撤回后的知识库自动同步：任何失败都不阻断主流程（台账与操作记录留痕，可用手动按钮重试）。"""
    try:
        sync_kb_to_dify(db, tenant_id, actor=actor)
    except Exception:
        pass
CHANNEL_RFQ_ACK = "您的询价信息已收到；您可以随时重新打开链接更新内容。"
CHANNEL_CONFIRM_WORDS = ("确认", "确定", "confirm", "confirmed", "ok")
CHANNEL_HUMAN_WORDS = ("人工", "转人工", "工程师", "人工核实", "engineer", "human", "agent")
_CHANNEL_QUESTION_HEAD = re.compile(r"^(what|which|how|can|could|do|does|is|are|will|would|should|why|when|where|who|may)\b", re.IGNORECASE)
_CHANNEL_QUESTION_HINTS = ("请问", "问一下", "想问", "咨询一下", "是否", "能否", "能不能", "可不可以", "如何", "怎么", "多少", "多大", "什么型号", "哪种", "哪款", "哪一个")


def channel_looks_like_question(text: str) -> bool:
    """与参考客户端 looksLikeQuestion 同口径；规则保守——宁可多走需求识别，不误吞工况描述。"""
    value = str(text or "").strip()
    if not value:
        return False
    if "?" in value or "？" in value:
        return True
    if _CHANNEL_QUESTION_HEAD.match(value):
        return True
    return any(hint in value for hint in _CHANNEL_QUESTION_HINTS)


# LangBot 推送的适配器名为类名风格（如 TelegramAdapter）；统一归一成展示名（如 Telegram）。
_CHANNEL_PLATFORM_LABELS = {
    "telegram": "Telegram", "discord": "Discord", "line": "LINE", "slack": "Slack", "matrix": "Matrix",
    "lark": "飞书", "dingtalk": "钉钉", "kook": "KOOK", "qqofficial": "QQ",
    "wecom": "企业微信", "wecombot": "企业微信机器人", "wecomcs": "微信客服", "officialaccount": "微信公众号",
    "openclaw-weixin": "微信", "openclawweixin": "微信", "openclaw_weixin": "微信", "wechatpad": "微信", "wechatpadpro": "微信",
}


def normalize_platform_label(raw: str) -> str:
    """把 LangBot 适配器名（TelegramAdapter / telegram / …）归一成展示名；未知值去掉 Adapter 后缀原样保留。"""
    value = str(raw or "").strip()
    if value.endswith("Adapter"):
        value = value[: -len("Adapter")].strip()
    if not value:
        return ""
    return _CHANNEL_PLATFORM_LABELS.get(value.lower(), value)


class ChannelSendError(RuntimeError):
    """渠道发送失败（原因可直接展示给卖家界面）。"""


def channel_api_base(channel: str, config: dict) -> str:
    """渠道 API 基地址：环境变量 > 公开配置 > 官方默认。本地模拟用环境变量覆盖。"""
    env_base = os.environ.get(CHANNEL_API_BASE_ENV.get(channel, ""), "").strip()
    base = env_base or str(config.get("api_base") or "").strip() or CHANNEL_OFFICIAL_API_BASE.get(channel, "")
    return base.rstrip("/")


def channel_http_json(url: str, payload: dict, timeout: int = 10, headers: dict | None = None) -> dict:
    request = urllib_request.Request(
        url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json", **(headers or {})},
        method="POST",
    )
    try:
        with urllib_request.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8")
    except urllib_error.HTTPError as error:
        detail = ""
        try:
            payload = json.loads(error.read().decode("utf-8"))
            err_obj = payload.get("error") if isinstance(payload.get("error"), dict) else {}
            message = str((err_obj or {}).get("message") or payload.get("message") or "").strip()
            code = str((err_obj or {}).get("code") or payload.get("code") or "").strip()
            detail = message + (f"（code={code}）" if code else "")
        except Exception:
            detail = ""
        raise ChannelSendError(f"渠道接口拒绝：{detail or f'HTTP {error.code}'}") from error
    except (urllib_error.URLError, TimeoutError, OSError) as error:
        raise ChannelSendError(f"网络请求失败：{error}") from error
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as error:
        raise ChannelSendError("渠道接口返回内容无法解析") from error
    if not isinstance(data, dict):
        raise ChannelSendError("渠道接口返回格式异常")
    return data



def channel_form_post(url: str, params: dict, auth: tuple[str, str], timeout: int = 10) -> dict:
    """表单提交（application/x-www-form-urlencoded + Basic 认证）——Twilio 形状渠道专用。

    与 channel_http_json 的差异：错误体（Twilio 返回 code/message 诊断）会被解析进报错文案，
    便于在收件箱「发送失败」里看到「为什么送不出去」（如沙箱未加入、号码不可达）。
    """
    body = urlencode(params).encode("utf-8")
    credentials = base64.b64encode(f"{auth[0]}:{auth[1]}".encode("utf-8")).decode("ascii")
    request = urllib_request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded", "Authorization": f"Basic {credentials}"},
        method="POST",
    )
    try:
        with urllib_request.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8")
    except urllib_error.HTTPError as error:
        detail = ""
        try:
            payload = json.loads(error.read().decode("utf-8"))
            message = str(payload.get("message") or "").strip()
            code = str(payload.get("code") or "").strip()
            detail = message + (f"（code={code}）" if code else "")
        except Exception:
            detail = ""
        raise ChannelSendError(f"渠道接口拒绝：{detail or f'HTTP {error.code}'}") from error
    except (urllib_error.URLError, TimeoutError, OSError) as error:
        raise ChannelSendError(f"网络请求失败：{error}") from error
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as error:
        raise ChannelSendError("渠道接口返回内容无法解析") from error
    if not isinstance(data, dict):
        raise ChannelSendError("渠道接口返回格式异常")
    return data


def send_channel_text(channel: str, config: dict, external_user_id: str, text: str) -> str:
    """向渠道发送一条文本，返回外部消息 ID。凭证缺失/未开通时明确报错（fail-closed）。"""
    if channel == "langbot":
        # LangBot 纯传输层：经其发送接口把文本投递到原平台（target_id = 平台侧用户 ID）。
        api_key = os.environ.get("LANGBOT_API_KEY", "").strip()
        if not api_key:
            raise ChannelSendError("缺少环境变量 LANGBOT_API_KEY")
        bot_uuid = str(config.get("bot_uuid") or "").strip()
        if not bot_uuid:
            raise ChannelSendError("未配置 LangBot 机器人标识（bot_uuid）")
        data = channel_http_json(
            f"{channel_api_base(channel, config)}/api/v1/platform/bots/{bot_uuid}/send_message",
            {"target_type": "person", "target_id": str(external_user_id), "message_chain": [{"type": "Plain", "text": text}]},
            headers={"X-API-Key": api_key},
        )
        if data.get("code") != 0:
            raise ChannelSendError(str(data.get("msg") or "LangBot 接口返回失败"))
        return ""
    if channel == "whatsapp":
        if whatsapp_provider() == "meta":
            return _send_whatsapp_meta(config, external_user_id, text)
        return _send_whatsapp_twilio(config, external_user_id, text)
    if channel == "whatsapp_personal":
        return _send_whatsapp_personal(config, external_user_id, text)
    raise ChannelSendError("该渠道的发送通道尚未开通")


def _send_whatsapp_twilio(config: dict, external_user_id: str, text: str) -> str:
    """Twilio 形状：Messages API 表单提交 + Basic 认证；返回 SID。

    凭证一律环境变量（不回显）：TWILIO_ACCOUNT_SID / TWILIO_AUTH_TOKEN / TWILIO_WHATSAPP_FROM；
    可选 TWILIO_STATUS_CALLBACK=本平台 /api/v1/channels/whatsapp/status 的公网地址（接收送达/失败回执）。
    """
    account_sid = os.environ.get("TWILIO_ACCOUNT_SID", "").strip()
    auth_token = os.environ.get("TWILIO_AUTH_TOKEN", "").strip()
    sender = os.environ.get("TWILIO_WHATSAPP_FROM", "").strip()
    if not account_sid:
        raise ChannelSendError("缺少环境变量 TWILIO_ACCOUNT_SID")
    if not auth_token:
        raise ChannelSendError("缺少环境变量 TWILIO_AUTH_TOKEN")
    if not sender:
        raise ChannelSendError("缺少环境变量 TWILIO_WHATSAPP_FROM")
    to_number = str(external_user_id or "").strip()
    if not to_number:
        raise ChannelSendError("缺少买方号码，无法发送")
    params = {
        "From": sender if sender.startswith("whatsapp:") else f"whatsapp:{sender}",
        "To": to_number if to_number.startswith("whatsapp:") else f"whatsapp:{to_number}",
        "Body": text,
    }
    status_callback = os.environ.get("TWILIO_STATUS_CALLBACK", "").strip()
    if status_callback:
        params["StatusCallback"] = status_callback
    data = channel_form_post(
        f"{channel_api_base('whatsapp', config)}/2010-04-01/Accounts/{account_sid}/Messages.json",
        params,
        (account_sid, auth_token),
    )
    sid = str(data.get("sid") or "").strip()
    if not sid:
        raise ChannelSendError(str(data.get("message") or "渠道接口返回失败（无消息编号）"))
    return sid


def _send_whatsapp_meta(config: dict, external_user_id: str, text: str) -> str:
    """Meta Cloud API 形状：graph JSON + Bearer；返回 wamid。

    环境变量：META_WHATSAPP_TOKEN / META_PHONE_NUMBER_ID；可选 META_API_BASE / META_API_VERSION（默认 graph.facebook.com / v23.0）。
    """
    token = os.environ.get("META_WHATSAPP_TOKEN", "").strip()
    phone_id = os.environ.get("META_PHONE_NUMBER_ID", "").strip()
    if not token:
        raise ChannelSendError("缺少环境变量 META_WHATSAPP_TOKEN")
    if not phone_id:
        raise ChannelSendError("缺少环境变量 META_PHONE_NUMBER_ID")
    to_number = re.sub(r"\D", "", str(external_user_id or ""))
    if not to_number:
        raise ChannelSendError("缺少买方号码，无法发送")
    base = (os.environ.get("META_API_BASE", "").strip() or "https://graph.facebook.com").rstrip("/")
    version = os.environ.get("META_API_VERSION", "").strip() or "v23.0"
    data = channel_http_json(
        f"{base}/{version}/{phone_id}/messages",
        {"messaging_product": "whatsapp", "recipient_type": "individual", "to": to_number, "type": "text", "text": {"body": text}},
        headers={"Authorization": f"Bearer {token}"},
    )
    sid = ""
    try:
        sid = str(data["messages"][0]["id"])
    except (KeyError, IndexError, TypeError):
        sid = ""
    if not sid:
        raise ChannelSendError("渠道接口返回失败（无消息编号）")
    return sid


# ── WhatsApp 个人版（WAHA 桥）· 节奏层（防封） ──────────────────────────────
# 照抄 WAHA 官方《How to Avoid Blocking》指南：只回复、拟人四步（已读→输入中→随机等待→发送）、
# 全局限速。节奏层状态仅存内存（进程重启即重置，偏保守）。
_WHATSAPP_PERSONAL_PACING_LOCK = threading.Lock()
_WHATSAPP_PERSONAL_SENT_AT: list = []
_WHATSAPP_PERSONAL_SENT_IDS: list = []
_WHATSAPP_PERSONAL_SENT_TEXTS: list = []


def _whatsapp_personal_reset_state() -> None:
    """清空节奏层与回声防护状态（测试与运维用）。"""
    with _WHATSAPP_PERSONAL_PACING_LOCK:
        _WHATSAPP_PERSONAL_SENT_AT.clear()
        _WHATSAPP_PERSONAL_SENT_IDS.clear()
        _WHATSAPP_PERSONAL_SENT_TEXTS.clear()


def _whatsapp_personal_self_ids() -> set:
    raw = os.environ.get("WHATSAPP_PERSONAL_SELF_IDS", "")
    return {item.strip() for item in raw.split(",") if item.strip()}


def _whatsapp_personal_is_self_chat(from_jid: str, to_jid: str) -> bool:
    """是否"自聊"（发给自己）：to 指回自己。

    背景（真机实测）：账号迁移 LID 后，自聊消息的 from/to 可能一方是「8617…@c.us」、
    另一方是「…@lid」——不能只做字符串相等；以本机配置的自有 ID 集合（PN+LID 双格式）判定。
    未配置集合时退化为原样相等比较。
    """
    self_ids = _whatsapp_personal_self_ids()
    if not self_ids:
        return from_jid == to_jid
    return from_jid == to_jid or to_jid in self_ids


def _whatsapp_personal_debug(summary: dict) -> None:
    """诊断日志（WHATSAPP_PERSONAL_DEBUG=1 时启用）：逐条记录入站事件摘要与处理结果。"""
    if os.environ.get("WHATSAPP_PERSONAL_DEBUG", "").strip() != "1":
        return
    try:
        path = ROOT / "data" / "whatsapp_personal_events.log"
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(summary, ensure_ascii=False) + "\n")
    except Exception:
        pass


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, "").strip() or default)
    except ValueError:
        return default


def _whatsapp_personal_pace(text: str) -> float:
    """拟人节奏（防封）：最小间隔 + 每分钟/每小时上限；返回"输入中"应持续的秒数。

    超限时抛出 ChannelSendError（该条不发送，原因可见、可人工重试）。
    """
    if os.environ.get("WHATSAPP_PERSONAL_PACING", "1").strip().lower() in ("0", "off", "false"):
        return None
    min_gap = max(0.0, _env_float("WHATSAPP_PERSONAL_MIN_GAP", 3.0))
    max_per_min = max(1, int(_env_float("WHATSAPP_PERSONAL_MAX_PER_MIN", 8)))
    max_per_hour = max(1, int(_env_float("WHATSAPP_PERSONAL_MAX_PER_HOUR", 60)))
    typing_base = max(0.0, _env_float("WHATSAPP_PERSONAL_TYPING_BASE", 1.5))
    typing_per_char = max(0.0, _env_float("WHATSAPP_PERSONAL_TYPING_PER_CHAR", 0.03))
    typing_max = max(0.0, _env_float("WHATSAPP_PERSONAL_TYPING_MAX", 6.0))
    with _WHATSAPP_PERSONAL_PACING_LOCK:
        now = time.time()
        recent = [stamp for stamp in _WHATSAPP_PERSONAL_SENT_AT if stamp >= now - 3600]
        _WHATSAPP_PERSONAL_SENT_AT[:] = recent
        if sum(1 for stamp in recent if stamp >= now - 60) >= max_per_min:
            raise ChannelSendError(f"节奏层（防封）：已达每分钟上限 {max_per_min} 条，本条未发送，请稍后重试")
        if len(recent) >= max_per_hour:
            raise ChannelSendError(f"节奏层（防封）：已达每小时上限 {max_per_hour} 条，本条未发送，请稍后重试")
        last = recent[-1] if recent else 0.0
        gap_wait = max(0.0, min_gap - (now - last))
        if gap_wait > 0:
            time.sleep(gap_wait)
        _WHATSAPP_PERSONAL_SENT_AT.append(time.time())
    typing_delay = min(typing_max, typing_base + typing_per_char * max(0, len(text or "")))
    return typing_delay


def _whatsapp_personal_notify(base: str, api_key: str, session: str, chat_id: str, action: str) -> None:
    """拟人状态通知（已读 / 输入中）——尽力而为：失败静默，不影响发送本体。"""
    paths = {"seen": "/api/sendSeen", "typing_start": "/api/startTyping", "typing_stop": "/api/stopTyping"}
    path = paths.get(action)
    if not path:
        return
    try:
        channel_http_json(base + path, {"session": session, "chatId": chat_id}, timeout=5, headers={"X-Api-Key": api_key})
    except Exception:
        pass


def _whatsapp_personal_remember_sent(message_id: str, text: str) -> None:
    """记录本桥发出的消息 ID/文本（回声防护：自聊场景下避免"自己回自己"的循环）。"""
    with _WHATSAPP_PERSONAL_PACING_LOCK:
        if message_id:
            _WHATSAPP_PERSONAL_SENT_IDS.append(message_id)
            _WHATSAPP_PERSONAL_SENT_IDS[:] = _WHATSAPP_PERSONAL_SENT_IDS[-200:]
        if text:
            _WHATSAPP_PERSONAL_SENT_TEXTS.append(text)
            _WHATSAPP_PERSONAL_SENT_TEXTS[:] = _WHATSAPP_PERSONAL_SENT_TEXTS[-50:]


def _whatsapp_personal_was_sent(message_id: str, text: str) -> bool:
    with _WHATSAPP_PERSONAL_PACING_LOCK:
        return (bool(message_id) and message_id in _WHATSAPP_PERSONAL_SENT_IDS) or (bool(text) and text in _WHATSAPP_PERSONAL_SENT_TEXTS)


def _send_whatsapp_personal(config: dict, external_user_id: str, text: str) -> str:
    """WhatsApp 个人版（经本机 WAHA 桥）：节奏层 → 已读/输入中 → sendText；返回消息 ID。

    环境变量：WHATSAPP_PERSONAL_BASE（默认 http://127.0.0.1:3100）、WHATSAPP_PERSONAL_API_KEY；
    节奏参数：WHATSAPP_PERSONAL_PACING / _MIN_GAP / _MAX_PER_MIN / _MAX_PER_HOUR / _TYPING_*。
    """
    base = channel_api_base("whatsapp_personal", config)
    api_key = os.environ.get("WHATSAPP_PERSONAL_API_KEY", "").strip()
    if not api_key:
        raise ChannelSendError("缺少环境变量 WHATSAPP_PERSONAL_API_KEY")
    session = str(config.get("session") or "default").strip() or "default"
    jid = str(external_user_id or "").strip()
    if "@" not in jid:
        digits = re.sub(r"\D", "", jid)
        if not digits:
            raise ChannelSendError("缺少买方号码，无法发送")
        jid = f"{digits}@c.us"
    typing_delay = _whatsapp_personal_pace(text)
    if typing_delay is not None:
        _whatsapp_personal_notify(base, api_key, session, jid, "seen")
        _whatsapp_personal_notify(base, api_key, session, jid, "typing_start")
        if typing_delay > 0:
            time.sleep(typing_delay)
        _whatsapp_personal_notify(base, api_key, session, jid, "typing_stop")
    data = channel_http_json(
        f"{base}/api/sendText",
        {"session": session, "chatId": jid, "text": text},
        headers={"X-Api-Key": api_key},
    )
    sid = ""
    raw_id = data.get("id")
    if isinstance(raw_id, dict):
        sid = str(raw_id.get("_serialized") or raw_id.get("id") or "").strip()
    elif isinstance(raw_id, str):
        sid = raw_id.strip()
    _whatsapp_personal_remember_sent(sid, text)
    return sid


def channel_outbound_text(message: sqlite3.Row) -> str:
    """外发文案：去掉只为网页呈现保留的「xx回复：」前缀（标签由接收端呈现）。"""
    content = message["content"] or ""
    for prefix in ("卖家回复：", "工程师回复："):
        if content.startswith(prefix):
            return content[len(prefix):]
    return content


def _mark_outbound_failed(db: Database, message_id: str, error: str) -> tuple[bool, str]:
    """把一次投递失败落成可见状态（原因进聊天记录；可人工重试）。"""
    db.conn.execute(
        "UPDATE messages SET delivery_status='failed', delivery_error=?, delivery_updated_at=? WHERE id=?",
        (error, now_iso(), message_id),
    )
    db.conn.commit()
    return False, error


def dispatch_outbound(db: Database, tenant_id: str, message_id: str) -> tuple[bool, str]:
    """出站总线投递（发送与重试共用）：queued/failed → sent / failed。

    - 已成功（sent/delivered）的消息幂等返回，绝不重发；
    - 每次尝试先自增 attempts、刷新 delivery_updated_at，崩溃/失败都留痕；
    - 网站聊天会话没有外发通道：视为即时可见，直接成功。
    """
    message = db.conn.execute("SELECT * FROM messages WHERE id=? AND tenant_id=?", (message_id, tenant_id)).fetchone()
    if not message:
        return False, "消息不存在"
    conversation = db.get_conversation(tenant_id, message["conversation_id"])
    if not conversation:
        return False, "会话不存在"
    channel = conversation["source"] or ""
    if channel not in CHANNEL_KEYS:
        return True, ""
    if (message["delivery_status"] or "") in ("sent", "delivered"):
        return True, ""
    db.conn.execute(
        "UPDATE messages SET delivery_status='queued', delivery_error='', delivery_attempts=COALESCE(delivery_attempts,0)+1, delivery_updated_at=? WHERE id=?",
        (now_iso(), message_id),
    )
    db.conn.commit()
    account = db.get_channel_account(tenant_id, channel)
    if not account or account["status"] != "enabled":
        return _mark_outbound_failed(db, message_id, "渠道未启用")
    identity = db.get_channel_identity_by_conversation(tenant_id, channel, conversation["id"])
    if not identity:
        return _mark_outbound_failed(db, message_id, "没有绑定外部联系人")
    try:
        config = json.loads(account["config_json"] or "{}")
    except json.JSONDecodeError:
        config = {}
    try:
        external_id = send_channel_text(channel, config, identity["external_user_id"], channel_outbound_text(message))
    except ChannelSendError as error:
        return _mark_outbound_failed(db, message_id, str(error))
    except Exception as error:  # 防御：程序性异常也必须留痕（否则消息永远悬在 queued，静默失联）
        return _mark_outbound_failed(db, message_id, f"发送异常：{type(error).__name__}: {error}")
    # 无外部回执 ID（如 LangBot 发送接口不回传消息 ID）→ 置 NULL：
    # 空串会被唯一索引（idx_messages_external，条件 IS NOT NULL）计入，导致同渠道第二条无法投递。
    external_message_id = f"{identity['external_user_id']}:{external_id}" if external_id else None
    db.conn.execute(
        "UPDATE messages SET delivery_status='sent', external_message_id=?, delivery_error='', delivery_updated_at=? WHERE id=?",
        (external_message_id, now_iso(), message_id),
    )
    db.conn.commit()
    return True, ""


def enqueue_outbound(db: Database, tenant_id: str, conversation: sqlite3.Row, role: str, content: str, source: str, actor_email: str = "", proactive: bool = False) -> tuple[str, bool, str]:
    """出站总线入口：先落 queued 消息，再投递；返回（消息 ID, 是否送达, 失败原因）。

    主动消息（proactive=True，如跟进、询价链接）先过「停止主动联系」门控，被阻止时抛
    ValueError 且不创建任何消息（fail-closed）；对买方消息的回应（问答/回复）不设该门控。
    """
    channel = (conversation["source"] or "") if conversation["source"] else ""
    if channel not in CHANNEL_KEYS:
        channel = ""
    if channel and proactive:
        preference = db.get_contact_preference(tenant_id, conversation["id"])
        if preference and preference["stop_proactive_contact"]:
            raise ValueError("买方已要求停止主动联系，发送被阻止")
    message_id = db.save_message(
        tenant_id, conversation["id"], role, content, source,
        channel=channel, delivery_status="queued" if channel else "", actor_email=actor_email,
    )
    if not channel:
        return message_id, True, ""
    sent, error = dispatch_outbound(db, tenant_id, message_id)
    return message_id, sent, error


def retry_outbound_message(db: Database, tenant_id: str, message_id: str, actor: str = "") -> tuple[bool, str]:
    """人工重试失败消息：仅 failed/queued 可重试；原子状态校验防并发重复发送；审计留痕。"""
    message = db.conn.execute("SELECT * FROM messages WHERE id=? AND tenant_id=?", (message_id, tenant_id)).fetchone()
    if not message:
        return False, "消息不存在"
    status = message["delivery_status"] or ""
    if status in ("sent", "delivered"):
        return True, ""  # 幂等：已成功不重发
    if status not in ("failed", "queued"):
        return False, "该消息不是发送失败状态，不能重试"
    if status == "failed":
        cursor = db.conn.execute(
            "UPDATE messages SET delivery_status='queued', delivery_updated_at=? WHERE id=? AND tenant_id=? AND delivery_status='failed'",
            (now_iso(), message_id, tenant_id),
        )
        if cursor.rowcount != 1:
            # 并发下已被其他请求重试/成功：读取最新状态，已成功按幂等成功返回。
            latest = db.conn.execute("SELECT delivery_status FROM messages WHERE id=?", (message_id,)).fetchone()
            if latest and (latest["delivery_status"] or "") in ("sent", "delivered"):
                return True, ""
            return False, "消息状态已变化，请刷新后重试"
        db.conn.commit()
    sent, error = dispatch_outbound(db, tenant_id, message_id)
    attempts_row = db.conn.execute("SELECT delivery_attempts FROM messages WHERE id=?", (message_id,)).fetchone()
    attempts = attempts_row["delivery_attempts"] if attempts_row else 0
    db._audit(tenant_id, "message", message_id, "outbound_retried", actor or "system", f"result={'sent' if sent else 'failed'};attempts={attempts}")
    db.conn.commit()
    return sent, error


def mark_outbound_delivered(db: Database, tenant_id: str, channel: str, external_message_id: str) -> bool:
    """投递回执（如 Twilio 状态回调）：sent → delivered；幂等、只在自身记录上推进。"""
    if not channel or not external_message_id:
        return False
    cursor = db.conn.execute(
        "UPDATE messages SET delivery_status='delivered', delivery_updated_at=? "
        "WHERE tenant_id=? AND channel=? AND external_message_id=? AND delivery_status='sent'",
        (now_iso(), tenant_id, channel, external_message_id),
    )
    db.conn.commit()
    return cursor.rowcount > 0


def apply_whatsapp_delivery_status(db: Database, message_sid: str, status: str, error_text: str = "") -> bool:
    """Twilio 状态回调落库：sent/queued → delivered；sent/queued → failed（终态不降级、幂等）。

    查找方式：external_message_id 形如「外部用户ID:SID」，按「:SID」后缀或全值匹配（SID 全局唯一）。
    """
    if not message_sid:
        return False
    status = str(status or "").strip().lower()
    if status not in ("delivered", "failed", "undelivered"):
        return False
    row = db.conn.execute(
        "SELECT id, delivery_status FROM messages WHERE channel='whatsapp' "
        "AND (external_message_id = ? OR external_message_id LIKE '%:' || ?) ORDER BY rowid DESC LIMIT 1",
        (message_sid, message_sid),
    ).fetchone()
    if not row:
        return False
    current = row["delivery_status"] or ""
    if current not in ("sent", "queued"):
        return False
    if status == "delivered":
        db.conn.execute(
            "UPDATE messages SET delivery_status='delivered', delivery_updated_at=? WHERE id=?",
            (now_iso(), row["id"]),
        )
    else:
        db.conn.execute(
            "UPDATE messages SET delivery_status='failed', delivery_error=?, delivery_updated_at=? WHERE id=?",
            (error_text or f"投递失败（{status}）", now_iso(), row["id"]),
        )
    db.conn.commit()
    return True


def get_or_create_channel_conversation(db: Database, tenant_id: str, channel: str, external_user_id: str, display_name: str, locale: str = "en") -> str:
    """外部用户 ↔ 持续会话：一个外部用户对应一个长期会话；并发下靠唯一约束兜底。"""
    identity = db.get_channel_identity(tenant_id, channel, external_user_id)
    if identity:
        return identity["conversation_id"]
    conversation_id = db.create_conversation(tenant_id, channel, locale if locale in {"en", "zh"} else "en")
    try:
        db.bind_channel_identity(tenant_id, channel, external_user_id, display_name, conversation_id)
        return conversation_id
    except sqlite3.IntegrityError:
        db.conn.rollback()
        existing = db.get_channel_identity(tenant_id, channel, external_user_id)
        if not existing:
            raise
        # 本次新建的会话没被绑定（并发败者）：没有消息就清理，避免收件箱出现空会话。
        count = db.conn.execute("SELECT COUNT(*) AS n FROM messages WHERE conversation_id=?", (conversation_id,)).fetchone()
        if count and count["n"] == 0:
            db.conn.execute("DELETE FROM conversations WHERE tenant_id=? AND id=?", (tenant_id, conversation_id))
            db.conn.commit()
        return existing["conversation_id"]


def channel_send_stored(db: Database, tenant_id: str, conversation: sqlite3.Row, text: str, source: str, actor_email: str = "", proactive: bool = False) -> tuple[str, bool, str]:
    """兼容包装：经出站总线保存并投递一条对外消息（返回 消息 ID / 是否送达 / 失败原因）。"""
    return enqueue_outbound(db, tenant_id, conversation, "assistant", text, source, actor_email=actor_email, proactive=proactive)


def channel_requirement_summary(values: dict) -> str:
    lines = []
    for key, item in values.items():
        if not isinstance(item, dict):
            continue
        value = str(item.get("value", "")).strip()
        if not value:
            continue
        label = str(item.get("label") or REQUIREMENT_LABELS.get(key, key))
        unit = str(item.get("unit", "")).strip()
        lines.append(f"· {label}：{value}{(' ' + unit) if unit else ''}")
    return ("已记录以下工况：\n" + "\n".join(lines)) if lines else ""


def channel_requirement_snapshot(values: dict) -> str:
    """只比较取值与单位（判断本次消息是否带来新内容）。"""
    return json.dumps(
        {key: [str(item.get("value", "")).strip(), str(item.get("unit", "")).strip()] for key, item in values.items() if isinstance(item, dict)},
        sort_keys=True, ensure_ascii=False,
    )


def channel_recommendation_summary(results: list, missing: list) -> str:
    candidates = [item for item in results if item.get("status") == "candidate"]
    tentative = [item for item in results if item.get("status") == "needs_confirmation"]
    parts: list = []
    if candidates:
        parts.append("根据已确认工况，资料显示可以关注以下型号：")
        for item in candidates[:3]:
            refs = [str(value) for value in (item.get("satisfies") or [])[:2]]
            note = f"（{'；'.join(refs)}）" if refs else ""
            parts.append(f"· {item.get('model')}{note}")
    elif tentative:
        parts.append(f"有 {len(tentative)} 个型号与工况相关，但需要工程师核实后才能作为推荐。")
    else:
        parts.append("根据已确认工况，当前没有可直接推荐的型号。")
    if missing:
        parts.append("还需补充：" + "、".join(str(item) for item in missing))
        parts.append("您可以继续补充工况，或回复「人工」请求工程师协助。")
    else:
        parts.append("如需进一步确认适用性，回复「人工」可请求工程师协助。")
    return "\n".join(parts)


def process_channel_message(db: Database, ai, tenant_id: str, channel: str, external_user_id: str, display_name: str, text: str, external_message_id: str) -> dict:
    """渠道买家消息 → 落库 + 服务端分流（全部复用网页聊天的业务函数）。"""
    text = str(text or "").strip()
    if not text:
        return {"action": "empty"}
    existing = db.find_message_by_external(tenant_id, channel, external_message_id)
    if existing:
        return {"action": "duplicate", "conversation_id": existing["conversation_id"], "message_id": existing["id"]}
    conversation_id = get_or_create_channel_conversation(db, tenant_id, channel, external_user_id, display_name)
    message_id = db.save_message(tenant_id, conversation_id, "buyer", text, channel, channel=channel, external_message_id=external_message_id)
    conversation = db.get_conversation(tenant_id, conversation_id)
    if not conversation:
        return {"action": "error", "error": "conversation_missing"}
    result: dict = {"action": "recorded", "conversation_id": conversation_id, "message_id": message_id}
    # ① 工程师追问待回复：整条消息作为对追问的补充（与网页聊天同一语义）。
    waiting = db.conn.execute(
        "SELECT id FROM human_tasks WHERE tenant_id=? AND conversation_id=? AND status='waiting_buyer' ORDER BY updated_at DESC LIMIT 1",
        (tenant_id, conversation_id),
    ).fetchone()
    if waiting:
        db.answer_task_followup(tenant_id, conversation_id, waiting["id"], text)
        result["action"] = "human_followup"
        return result
    # ② 暂停 AI：消息已保存、不自动答复；问句补一条与网页可见性一致的「已暂停」文案。
    if conversation["ai_paused"]:
        if channel_looks_like_question(text):
            channel_send_stored(db, tenant_id, conversation, AI_PAUSED_ANSWER, "unresolved-question")
            result["action"] = "ai_paused"
        return result
    compact = text.casefold().replace(" ", "")
    # ③ 「人工」：把最近一条未解问题转交工程师（与网页「请求工程师核实」同一路径，不自动建任务）。
    if compact in {word.replace(" ", "") for word in CHANNEL_HUMAN_WORDS}:
        answer = db.conn.execute(
            "SELECT a.id FROM answers a WHERE a.tenant_id=? AND a.conversation_id=? AND a.answer_status='needs_human' "
            "AND NOT EXISTS (SELECT 1 FROM human_tasks t WHERE t.tenant_id=a.tenant_id AND t.conversation_id=a.conversation_id AND t.question_message_id=a.question_message_id) "
            "ORDER BY a.rowid DESC LIMIT 1",
            (tenant_id, conversation_id),
        ).fetchone()
        if answer:
            task_id = db.request_human_review(tenant_id, conversation_id, answer["id"])
            ack = db.conn.execute(
                "SELECT id FROM messages WHERE tenant_id=? AND conversation_id=? AND source=?",
                (tenant_id, conversation_id, f"human-request:{task_id}"),
            ).fetchone()
            if ack:
                dispatch_outbound(db, tenant_id, ack["id"])
            result["action"] = "human_requested"
            result["task_id"] = task_id
        else:
            channel_send_stored(db, tenant_id, conversation, CHANNEL_HUMAN_GUIDE, "channel-guidance")
            result["action"] = "human_guide"
        return result
    # ④ 「确认」：确认最新候选工况并按网页同口径匹配产品。
    latest = db.get_latest_revision(tenant_id, conversation_id)
    if compact in {word.replace(" ", "") for word in CHANNEL_CONFIRM_WORDS} and latest and latest["status"] == "candidate":
        values = json.loads(latest["values_json"])
        confirmed = confirm_requirement_values(values)
        revision_id = db.create_requirement_revision(tenant_id, conversation_id, confirmed, "confirmed", channel)
        try:
            full_results, missing = classify_products(db, tenant_id, confirmed)
            status = overall_status(full_results, missing)
            db.save_recommendation(tenant_id, conversation_id, revision_id, status, full_results)
            body_text = channel_recommendation_summary(full_results, missing)
        except Exception:
            body_text = "工况已确认，但产品匹配暂时不可用；请稍后重试，或回复「人工」请求工程师协助。"
        channel_send_stored(db, tenant_id, conversation, body_text, "channel-recommendation")
        result["action"] = "confirmed"
        return result
    # ⑤ 提问样文本：有据问答（与网页一致：绑定当前推荐首型号）；资料外附转人工提示。
    if channel_looks_like_question(text):
        product_id = None
        recommendation = db.latest_active_recommendation(tenant_id, conversation_id)
        if recommendation:
            try:
                results = json.loads(recommendation["results_json"])
            except json.JSONDecodeError:
                results = []
            first = next((item for item in results if item.get("status") in {"candidate", "needs_confirmation"}), None)
            if first:
                product_id = first.get("product_id")
        # 第二波：LangBot / WhatsApp 渠道的资料问答优先走 Dify 引擎（平台仍是唯一主脑：落库、转人工、出站都在平台）。
        if channel in ("langbot", "whatsapp", "whatsapp_personal"):
            status, answer_text, answer_product_id, version_id, citations, _reason, qa_source = answer_question_prefer_dify(
                db, ai, tenant_id, conversation_id, text, product_id, f"{channel}:{external_user_id}"
            )
        else:
            status, answer_text, answer_product_id, version_id, citations, _reason = answer_question_with_mode(db, ai, tenant_id, conversation_id, text, product_id)
            qa_source = "local"
        db.save_answer(tenant_id, conversation_id, message_id, answer_product_id, version_id, status, answer_text, citations)
        if status == "grounded":
            channel_send_stored(db, tenant_id, conversation, answer_text, "grounded-answer")
        else:
            channel_send_stored(db, tenant_id, conversation, f"{answer_text}\n\n{CHANNEL_HUMAN_HINT}", "unresolved-question")
        result["action"] = "answered" if status == "grounded" else "answer_unresolved"
        result["qa_source"] = qa_source
        return result
    # ⑥ 其余：需求识别与整理（没有新内容时不重复播报）。
    previous_values = json.loads(latest["values_json"]) if latest else {}
    try:
        values = extract_requirement_candidates(db, ai, tenant_id, conversation_id, text, None, previous_values)
    except Exception:
        channel_send_stored(db, tenant_id, conversation, "您的消息已收到，但需求识别暂时不可用；请稍后再试。", "channel-guidance")
        result["action"] = "extraction_failed"
        return result
    changed = channel_requirement_snapshot(values) != channel_requirement_snapshot(previous_values)
    if not changed and latest:
        result["action"] = "no_change"
        return result
    db.create_requirement_revision(tenant_id, conversation_id, values, "candidate", channel)
    summary = channel_requirement_summary(values)
    if summary:
        body_text = f"{summary}\n\n请核对：回复「确认」开始匹配产品，或直接补充、更正。"
        source = "channel-requirements"
    else:
        body_text = "没有识别到具体的工况参数。请描述关键信息（示例：流量 3 m³/h、扬程 45 m、介质清水），或直接提出产品问题。"
        source = "channel-guidance"
    channel_send_stored(db, tenant_id, conversation, body_text, source)
    result["action"] = "extracted"
    return result



def handle_langbot_event(db: Database, ai, tenant_id: str, event: dict) -> dict | None:
    """LangBot 事件 → 渠道消息处理（person 消息与 Telegram 同口径走同一分流）。

    事件由 LangBot 的 Webhook 推送；uuid 为事件唯一标识（消息去重键）；
    adapter_name 记录消息来自哪个社交平台（telegram / discord / …），落档供收件箱按平台展示。
    """
    if not isinstance(event, dict):
        return None
    if str(event.get("event_type") or "") != "bot.person_message":
        return None
    data = event.get("data") or {}
    if not isinstance(data, dict):
        return None
    sender = data.get("sender") or {}
    external_user_id = str(sender.get("id") or "").strip()
    external_message_id = str(event.get("uuid") or "").strip()
    if not external_user_id or not external_message_id:
        return None
    text_parts: list[str] = []
    chain = data.get("message") or []
    if isinstance(chain, list):
        for segment in chain:
            if isinstance(segment, dict) and str(segment.get("type") or "") == "Plain":
                part = str(segment.get("text") or "")
                if part:
                    text_parts.append(part)
    text = "\n".join(text_parts).strip()
    if not text:
        return {"action": "ignored_non_text"}
    platform_label = normalize_platform_label(str(data.get("adapter_name") or "")) or "LangBot"
    display_name = str(sender.get("name") or "").strip() or f"{platform_label} 用户 {external_user_id}"
    result = process_channel_message(db, ai, tenant_id, "langbot", external_user_id, display_name, text, external_message_id)
    if result.get("action") != "duplicate":
        meta = {"adapter": platform_label}
        bot_uuid = str(data.get("bot_uuid") or "").strip()
        if bot_uuid:
            meta["bot_uuid"] = bot_uuid
        db.update_channel_identity_meta(tenant_id, "langbot", external_user_id, meta)
    return result


def handle_whatsapp_inbound(db: Database, ai, tenant_id: str, params: dict) -> dict:
    """Twilio 入站消息（WhatsApp）→ 统一大脑 process_channel_message。

    - MessageSid 为消息去重键；From 去掉 `whatsapp:` 前缀后作为外部用户 ID；
    - 显示名取 WhatsApp ProfileName，缺省回退「WhatsApp 用户 <号码>」；
    - 非文本消息（图片/文件等）：首期跳过（ignored_non_text），不落库不回复。
    """
    if not isinstance(params, dict):
        return {"action": "ignored"}
    message_sid = str(params.get("MessageSid") or "").strip()
    from_raw = str(params.get("From") or "").strip()
    text = str(params.get("Body") or "").strip()
    profile = str(params.get("ProfileName") or "").strip()
    if not message_sid or not from_raw:
        return {"action": "ignored_malformed"}
    external_user_id = from_raw[len("whatsapp:"):] if from_raw.startswith("whatsapp:") else from_raw
    if not text:
        return {"action": "ignored_non_text"}
    display_name = profile or f"WhatsApp 用户 {external_user_id}"
    return process_channel_message(db, ai, tenant_id, "whatsapp", external_user_id, display_name, text, message_sid)


def handle_whatsapp_meta_event(db: Database, ai, tenant_id: str, payload: dict) -> dict:
    """Meta Cloud API webhook 事件：messages（入站）与 statuses（回执）分派。

    - 入站：id（wamid）= 去重键；from 归一化补「+」；type=text 以外的首期跳过；
    - 回执：delivered/read → 推进 delivered；failed → 标记失败（幂等、终态不降级，复用同款推进函数）。
    """
    actions = {"messages": 0, "statuses": 0, "ignored": 0, "duplicate": 0}
    if not isinstance(payload, dict):
        return {"action": "ignored"}
    for entry in payload.get("entry") or []:
        if not isinstance(entry, dict):
            continue
        for change in entry.get("changes") or []:
            value = (change.get("value") or {}) if isinstance(change, dict) else {}
            if not isinstance(value, dict):
                continue
            for message in value.get("messages") or []:
                if not isinstance(message, dict):
                    continue
                msg_id = str(message.get("id") or "").strip()
                from_raw = str(message.get("from") or "").strip()
                if not msg_id or not from_raw or str(message.get("type") or "") != "text":
                    actions["ignored"] += 1
                    continue
                text = str(((message.get("text") or {}).get("body")) or "").strip()
                if not text:
                    actions["ignored"] += 1
                    continue
                display_name = ""
                for contact in value.get("contacts") or []:
                    if isinstance(contact, dict):
                        display_name = str((contact.get("profile") or {}).get("name") or "").strip()
                        if display_name:
                            break
                external_user_id = "+" + from_raw.lstrip("+")
                if not display_name:
                    display_name = f"WhatsApp 用户 {external_user_id}"
                result = process_channel_message(db, ai, tenant_id, "whatsapp", external_user_id, display_name, text, msg_id)
                if result.get("action") == "duplicate":
                    actions["duplicate"] += 1
                else:
                    actions["messages"] += 1
            for status_item in value.get("statuses") or []:
                if not isinstance(status_item, dict):
                    continue
                sid = str(status_item.get("id") or "").strip()
                status = str(status_item.get("status") or "").strip().lower()
                if not sid or status not in ("delivered", "read", "failed"):
                    continue
                error_text = ""
                if status == "failed":
                    errors = status_item.get("errors") or []
                    if errors and isinstance(errors[0], dict):
                        item = errors[0]
                        error_text = f"投递失败：{str(item.get('title') or item.get('message') or 'failed')}" + (f"（code={item.get('code')}）" if item.get("code") else "")
                if apply_whatsapp_delivery_status(db, sid, "delivered" if status in ("delivered", "read") else "failed", error_text):
                    actions["statuses"] += 1
    return actions


def handle_whatsapp_personal_event(db: Database, ai, tenant_id: str, event: dict) -> dict:
    """WAHA 事件 → 渠道消息处理（WhatsApp 个人版桥）。

    - 只处理 event=="message" 的文本消息；群聊/状态广播/媒体跳过；
    - fromMe 消息默认跳过（自己发给别人的，绝不进平台）；唯一例外＝"自聊"（from==to，便于自测），
      且对自聊做了回声防护（本桥发出的消息 ID/文本不会再次进平台，避免自回环）；
    - 消息 ID（id._serialized）＝ 去重键；notifyName ＝ 显示名。
    """
    if not isinstance(event, dict):
        return {"action": "ignored"}
    event_type = str(event.get("event") or "")
    if event_type not in ("message", "message.any"):
        return {"action": "ignored_event", "event": event_type}
    payload = event.get("payload") or {}
    if not isinstance(payload, dict):
        return {"action": "ignored"}
    # 本桥经 API 发出的消息会以 message.any 形式回流（source=api）：直接判回声，不进平台。
    if str(payload.get("source") or "").strip().lower() == "api":
        return {"action": "ignored_echo"}
    from_jid = str(payload.get("from") or "").strip()
    to_jid = str(payload.get("to") or "").strip()
    if not from_jid:
        return {"action": "ignored_malformed"}
    if from_jid.endswith("@g.us") or from_jid == "status@broadcast":
        return {"action": "ignored_groupchat"}
    message_id = ""
    raw_id = payload.get("id")
    if isinstance(raw_id, dict):
        message_id = str(raw_id.get("_serialized") or raw_id.get("id") or "").strip()
    elif isinstance(raw_id, str):
        message_id = raw_id.strip()
    if not message_id:
        return {"action": "ignored_malformed"}
    text = str(payload.get("body") or "").strip()
    from_me = bool(payload.get("fromMe"))
    if from_me:
        if not _whatsapp_personal_is_self_chat(from_jid, to_jid):
            return {"action": "ignored_outgoing"}
        if _whatsapp_personal_was_sent(message_id, text):
            return {"action": "ignored_echo"}
    if not text or payload.get("hasMedia"):
        return {"action": "ignored_non_text"}
    data = payload.get("_data") or {}
    display_name = str(payload.get("notifyName") or (data.get("notifyName") if isinstance(data, dict) else "") or "").strip()
    if not display_name:
        display_name = f"WhatsApp（个人版）用户 {from_jid.split('@')[0]}"
    result = process_channel_message(db, ai, tenant_id, "whatsapp_personal", from_jid, display_name, text, message_id)
    return result


def render_rfq_link_form(field_config: dict, values: dict, error: str = "", existing_short: str = "") -> str:
    """公开的 RFQ 填写页表单主体（字段状态跟随企业设置：必填 / 选填 / 隐藏）。"""
    field_specs = (
        ("email", "邮箱 / Email", "contact_email", " type='email'", "you@example.com"),
        ("phone", "电话 / Phone", "contact_phone", "", "+49 …"),
        ("quantity", "数量 / Quantity", "quantity", "", "如 2"),
        ("destination", "目的地 / Destination", "destination", "", "如 Germany"),
    )
    inputs = []
    for key, label, name, input_type, placeholder in field_specs:
        state = field_config.get(key, "optional")
        if state == "hidden":
            continue
        star = " *" if state == "required" else ""
        required_attr = " required" if state == "required" else ""
        inputs.append(
            f"<div><label>{esc(label)}{star}</label>"
            f"<input name='{name}'{input_type} value='{esc(values.get(key, ''))}' placeholder='{esc(placeholder)}'{required_attr}></div>"
        )
    notes_state = field_config.get("notes", "optional")
    notes_input = ""
    if notes_state != "hidden":
        star = " *" if notes_state == "required" else ""
        required_attr = " required" if notes_state == "required" else ""
        notes_input = (
            f"<div class='full'><label>补充说明 / Notes{star}</label>"
            f"<textarea name='notes'{required_attr} placeholder='其它需要说明的信息'>{esc(values.get('notes', ''))}</textarea></div>"
        )
    revisit_note = ""
    if existing_short:
        revisit_note = f"<p class='muted'>您已提交过询价（编号 {esc(existing_short)}）；再次提交将更新同一张询价单。</p>"
    error_html = f"<div class='error'>{esc(error)}</div>" if error else ""
    return f"""
{error_html}
{revisit_note}
<form method='post'>
<div class='form-grid'>{''.join(inputs)}{notes_input}</div>
<label style='display:flex;gap:8px;align-items:flex-start;margin-top:12px'><input type='checkbox' name='contact_confirmed' value='1' style='width:auto;margin-top:3px'> 我确认销售可通过上述联系方式就本次询价与我联系 / I confirm this contact can be used for this request</label>
<p style='margin-top:12px'><button>提交询价 / Submit</button></p>
</form>
"""


# ── V1-03 创意中心 · 产品展示页：板块库与标准模板（首批冻结，见 docs/V1-03-收口稿.md） ──
# 板块库顺序 = 标准组合在现状公开页的自上而下顺序；fixed=不可关闭；default_enabled=标准模板默认开关。
TEMPLATE_BLOCK_LIBRARY = (
    {"key": "hero", "label_zh": "首屏", "label_en": "Hero", "fixed": True, "default_enabled": True},
    {"key": "highlights", "label_zh": "要点速览", "label_en": "Highlights", "fixed": False, "default_enabled": True},
    {"key": "specs", "label_zh": "参数规格", "label_en": "Specifications", "fixed": False, "default_enabled": True},
    {"key": "drawings", "label_zh": "图纸与尺寸", "label_en": "Drawings", "fixed": False, "default_enabled": True},
    {"key": "gallery", "label_zh": "图片与图库", "label_en": "Gallery", "fixed": False, "default_enabled": False},
    {"key": "comparison", "label_zh": "型号对比", "label_en": "Model comparison", "fixed": False, "default_enabled": True},
    {"key": "applications", "label_zh": "应用场景", "label_en": "Applications", "fixed": False, "default_enabled": True},
    {"key": "customization", "label_zh": "定制能力", "label_en": "Customization", "fixed": False, "default_enabled": True},
    {"key": "manufacturing", "label_zh": "制造与质检", "label_en": "Manufacturing & QC", "fixed": False, "default_enabled": True},
    {"key": "certifications", "label_zh": "认证与案例", "label_en": "Certifications & cases", "fixed": False, "default_enabled": True},
    {"key": "downloads", "label_zh": "资料下载", "label_en": "Documents", "fixed": False, "default_enabled": True},
    {"key": "faq", "label_zh": "常见问题", "label_en": "FAQ", "fixed": False, "default_enabled": True},
)
# 标准模板默认参数 = 现有 8 个可提取字段（按现状公开页展示顺序），保证默认渲染与现状等价。
STANDARD_TEMPLATE_PARAMS = tuple(key for _group_en, _group_zh, keys in PRODUCT_PAGE_GROUPS for key in keys)


def standard_template_draft() -> dict:
    """标准模板（推荐）初始内容：与现状公开页等价的板块与参数组合。"""
    return {
        "params": [
            {"key": key, "label_zh": FIELD_LABELS[key], "label_en": FIELD_LABELS_EN.get(key, key), "enabled": True}
            for key in STANDARD_TEMPLATE_PARAMS
        ],
        "blocks": [
            {"key": block["key"], "enabled": block["default_enabled"], "title_zh": "", "title_en": ""}
            for block in TEMPLATE_BLOCK_LIBRARY
        ],
    }


def blank_template_draft() -> dict:
    """空白创建初始内容：仅保留固定首屏，其余板块默认关闭、参数为空。"""
    return {
        "params": [],
        "blocks": [
            {"key": block["key"], "enabled": bool(block["fixed"]), "title_zh": "", "title_en": ""}
            for block in TEMPLATE_BLOCK_LIBRARY
        ],
    }


# 展示页编辑四步（顺序即递进关系；界面文案与收口稿冻结一致）。
CREATIVE_STEPS = (("params", "选参数"), ("blocks", "排内容"), ("preview", "看效果"), ("publish", "产品页联动"))


def creative_flowbar(active: str, template_id: str = "") -> str:
    """区域顶部唯一模块条：首页 + 四步递进链路（无数字、连线箭头、当前步高亮）。

    active ∈ {home, params, blocks, preview, publish}；首页态整条链路淡化预览（ghost）。
    """
    order = [key for key, _label in CREATIVE_STEPS]
    home = "<span class='fh on'>首页</span>" if active == "home" else "<a class='fh' href='/admin/creative'>首页</a>"
    parts = []
    for key, label in CREATIVE_STEPS:
        if active == key:
            parts.append(f"<span class='fs on'>{label}</span>")
        elif active == "home":
            parts.append(f"<span class='fs'>{label}</span>")
        else:
            css = "fs done" if order.index(key) < order.index(active) else "fs"
            parts.append(f"<a class='{css}' href='/admin/creative/{template_id}?step={key}'>{label}</a>")
    chain = "<span class='conn'></span>".join(parts)
    ghost = " ghost" if active == "home" else ""
    return f"<div class='flowbar{ghost}'>{home}<span class='chain'>{chain}</span></div>"


# 首页交互（新建卡点击展开 / 选择产品联动提示 /「开始设置」预选对应品类产品）。
CREATIVE_HOME_JS = """
(function(){
 var btn=document.getElementById('creative-new');
 var card=document.getElementById('newCard');
 var sel=document.getElementById('creative-product');
 var hint=document.getElementById('creative-hint');
 function sync(){
  if(!sel||!hint){return;}
  var opt=sel.options[sel.selectedIndex];
  var cat=opt?opt.getAttribute('data-cat'):'';
  if(!cat){hint.textContent='';return;}
  hint.textContent=opt.getAttribute('data-has')==='1'
   ?('将归入「'+cat+'」· 该品类已有展示页 → 将直接打开现有页')
   :('将归入「'+cat+'」· 将创建新展示页');
 }
 function openCard(){if(card){card.classList.add('open');sync();}}
 if(btn&&card){
  btn.addEventListener('click',function(){card.classList.toggle('open');if(card.classList.contains('open')){sync();}});
 }
 if(sel){sel.addEventListener('change',sync);sync();}
 var starters=document.querySelectorAll('[data-start-cat]');
 for(var i=0;i<starters.length;i++){
  starters[i].addEventListener('click',function(){
   if(!card||!sel){return;}
   var cat=this.getAttribute('data-start-cat');
   for(var j=0;j<sel.options.length;j++){
    if(sel.options[j].getAttribute('data-cat')===cat){sel.selectedIndex=j;break;}
   }
   openCard();
   card.scrollIntoView({block:'nearest'});
  });
 }
})();
"""


# 参数推荐池（首批 14 项，行业通用；键与现有提取字段同名者直接复用：material/power/dimension）。
CREATIVE_PARAM_POOL = (
    {"key": "model", "label_zh": "型号", "label_en": "Model", "unit": ""},
    {"key": "material", "label_zh": "材质", "label_en": "Material", "unit": ""},
    {"key": "dimension", "label_zh": "尺寸", "label_en": "Dimensions", "unit": "mm"},
    {"key": "weight", "label_zh": "重量", "label_en": "Weight", "unit": "kg"},
    {"key": "color", "label_zh": "颜色", "label_en": "Color", "unit": ""},
    {"key": "power", "label_zh": "功率", "label_en": "Power", "unit": "kW"},
    {"key": "voltage", "label_zh": "电压", "label_en": "Voltage", "unit": "V"},
    {"key": "capacity", "label_zh": "容量", "label_en": "Capacity", "unit": ""},
    {"key": "speed", "label_zh": "转速", "label_en": "Speed", "unit": "rpm"},
    {"key": "pressure", "label_zh": "压力", "label_en": "Pressure", "unit": "MPa"},
    {"key": "accuracy", "label_zh": "精度", "label_en": "Accuracy", "unit": ""},
    {"key": "operating_temperature", "label_zh": "工作温度", "label_en": "Operating temperature", "unit": "°C"},
    {"key": "ip_rating", "label_zh": "防护等级", "label_en": "IP rating", "unit": ""},
    {"key": "interface", "label_zh": "接口", "label_en": "Interface", "unit": ""},
)

# 选参数页交互（「编辑」行内展开；不参与后续自动保存——保存走表单提交后重定向）。
CREATIVE_PARAMS_JS = """
(function(){
 var buttons=document.querySelectorAll('[data-edit]');
 for(var i=0;i<buttons.length;i++){
  buttons[i].addEventListener('click',function(){
   var card=document.getElementById(this.getAttribute('data-edit'));
   if(card){card.classList.toggle('editing');}
  });
 }
})();
"""


def creative_params_step(template: sqlite3.Row) -> str:
    """「选参数」步骤：参数卡片（开关/排序/行内编辑/删除）+ 推荐池 + 自定义；全部自动保存。"""
    template_id = esc(template["id"])
    draft = json.loads(template["draft_json"])
    raw_params = draft.get("params")
    params = [p for p in raw_params if isinstance(p, dict)] if isinstance(raw_params, list) else []
    present = {str(p.get("key")) for p in params}
    cards = []
    for param in params:
        key = esc(str(param.get("key", "")))
        enabled = bool(param.get("enabled"))
        label_zh = esc(str(param.get("label_zh", "")))
        label_en = esc(str(param.get("label_en", "")))
        unit = esc(str(param.get("unit", "")))
        unit_html = f"<span class='unit'>{unit}</span>" if unit else ""
        card_id = f"pc-{key}"
        cards.append(f"""
<div class='pcard{' disabled' if not enabled else ''}' id='{card_id}'>
<div class='top'><span class='nm'>{label_zh} <small>{label_en}</small>{unit_html}</span>
<span class='tools'>
<form method='post' action='/admin/creative/{template_id}/params/toggle' class='inline'><input type='hidden' name='param_key' value='{key}'><button class='ptoggle{' on' if enabled else ''}' title='点击隐藏或显示这个参数' aria-label='显示或隐藏'></button></form>
<form method='post' action='/admin/creative/{template_id}/params/move' class='inline'><input type='hidden' name='param_key' value='{key}'><input type='hidden' name='dir' value='up'><button class='mini' title='上移'>↑</button></form>
<form method='post' action='/admin/creative/{template_id}/params/move' class='inline'><input type='hidden' name='param_key' value='{key}'><input type='hidden' name='dir' value='down'><button class='mini' title='下移'>↓</button></form>
<button class='mini link' type='button' data-edit='{card_id}'>编辑</button>
<form method='post' action='/admin/creative/{template_id}/params/delete' class='inline'><input type='hidden' name='param_key' value='{key}'><button class='mini danger-link'>删除</button></form>
</span></div>
<div class='editrow'><form method='post' action='/admin/creative/{template_id}/params/update'>
<input name='label_zh' value='{label_zh}' placeholder='参数名称' required>
<input name='label_en' value='{label_en}' placeholder='英文名（可选）'>
<input name='unit' value='{unit}' placeholder='单位（可选）'>
<input type='hidden' name='param_key' value='{key}'>
<button class='secondary'>完成</button>
</form></div>
</div>""")
    chips = []
    for item in CREATIVE_PARAM_POOL:
        if item["key"] in present:
            chips.append(f"<span class='pchip added'>✓ {esc(item['label_zh'])}</span>")
        else:
            chips.append(
                f"<form method='post' action='/admin/creative/{template_id}/params/add' class='inline'>"
                f"<input type='hidden' name='source' value='pool'><input type='hidden' name='pool_key' value='{esc(item['key'])}'>"
                f"<button class='pchip'>＋ {esc(item['label_zh'])}</button></form>"
            )
    return f"""
<section class='card'>
<div class='panel-title'><h2>页面要展示哪些参数？</h2><span class='muted'>数量不限，随时可以改</span></div>
<p class='muted' style='margin:0 0 12px'>这些是买家在页面上看到的规格。数值来自你上传的资料，你确认过才会展示——没确认的内容永远不会出现在页面上。</p>
{''.join(cards)}
<div class='muted' style='margin-top:12px'>从推荐里加（点一下加一个）：</div>
<div class='pchips'>{''.join(chips)}</div>
<details class='pnew'><summary class='pchip'>＋ 自定义参数…</summary>
<form method='post' action='/admin/creative/{template_id}/params/add'>
<input type='hidden' name='source' value='custom'>
<input name='label_zh' placeholder='参数名称（必填）' required>
<input name='label_en' placeholder='英文名（可选）'>
<input name='unit' placeholder='单位（可选）'>
<button>添加</button>
</form></details>
</section>
<div class='step-cta'><a class='button' href='/admin/creative/{template_id}?step=blocks'>下一步 · 排内容 →</a></div>
"""


def resolved_template_blocks(draft: dict) -> list[dict]:
    """按板块库补全 draft.blocks：保留既有顺序与设置，缺失项按库默认补齐，未知键剔除。"""
    raw = draft.get("blocks")
    items = [b for b in raw if isinstance(b, dict)] if isinstance(raw, list) else []
    known = {item["key"] for item in TEMPLATE_BLOCK_LIBRARY}
    result = [b for b in items if str(b.get("key")) in known]
    have = {str(b.get("key")) for b in result}
    for item in TEMPLATE_BLOCK_LIBRARY:
        if item["key"] not in have:
            result.append({"key": item["key"], "enabled": bool(item["fixed"]), "title_zh": "", "title_en": ""})
    return result


def template_status_badges(db: Database, template: sqlite3.Row) -> str:
    """展示页状态徽标（首页行与编辑区共用）：已上线 vN / 有改动还没发布 / 已撤回 / 已提交待确认。"""
    badges = []
    if template["status"] == "withdrawn":
        badges.append("<span class='badge withdrawn'>已撤回</span>")
    elif template["published_version"]:
        badges.append(f"<span class='badge approved'>已上线 v{template['published_version']}</span>")
        if db.template_has_unpublished_changes(template["tenant_id"], template):
            badges.append("<span class='badge pending_review'>有改动还没发布</span>")
    else:
        badges.append("<span class='badge pending_review'>有改动还没发布</span>")
    if template["status"] == "submitted":
        badges.append("<span class='badge info'>已提交，待确认</span>")
    return " ".join(badges)


def enabled_render_blocks(blocks: list | None) -> list[dict]:
    """渲染用：给定 blocks 配置返回启用项（按列表顺序）；缺省 = 标准模板默认组合。"""
    items = [b for b in blocks if isinstance(b, dict)] if isinstance(blocks, list) else []
    if not items:
        items = [
            {"key": item["key"], "enabled": item["default_enabled"], "title_zh": "", "title_en": ""}
            for item in TEMPLATE_BLOCK_LIBRARY
        ]
    return [b for b in resolved_template_blocks({"blocks": items}) if b.get("enabled")]


def creative_blocks_step(template: sqlite3.Row, versions: list[sqlite3.Row]) -> str:
    """「排内容」步骤：板块卡片（开关/排序/改标题）+ 板块库 + 进阶折叠；全部自动保存。"""
    template_id = esc(template["id"])
    draft = json.loads(template["draft_json"])
    blocks = resolved_template_blocks(draft)
    library = {item["key"]: item for item in TEMPLATE_BLOCK_LIBRARY}
    cards = []
    for block in blocks:
        key = esc(str(block.get("key")))
        meta = library[str(block.get("key"))]
        enabled = bool(block.get("enabled"))
        fixed = bool(meta["fixed"])
        title_zh = esc(str(block.get("title_zh", "")))
        title_en = esc(str(block.get("title_en", "")))
        title_note = f"<span class='title-note'>标题：{title_zh}</span>" if title_zh else ""
        card_id = f"bk-{key}"
        if fixed:
            tools = "<span class='lock'>固定显示</span>"
            note = "<div class='bnote'>产品大图 + 关键参数，固定显示。</div>"
        else:
            tools = (
                f"<form method='post' action='/admin/creative/{template_id}/blocks/toggle' class='inline'><input type='hidden' name='block_key' value='{key}'><button class='ptoggle{' on' if enabled else ''}' title='点击关闭或显示这个板块' aria-label='显示或隐藏'></button></form>"
                f"<form method='post' action='/admin/creative/{template_id}/blocks/move' class='inline'><input type='hidden' name='block_key' value='{key}'><input type='hidden' name='dir' value='up'><button class='mini' title='上移'>↑</button></form>"
                f"<form method='post' action='/admin/creative/{template_id}/blocks/move' class='inline'><input type='hidden' name='block_key' value='{key}'><input type='hidden' name='dir' value='down'><button class='mini' title='下移'>↓</button></form>"
                f"<button class='mini link' type='button' data-edit='{card_id}'>改标题</button>"
            )
            note = "" if enabled else "<div class='bnote'>已关闭：买家看不到这部分。</div>"
        edit_row = "" if fixed else f"""
<div class='editrow'><form method='post' action='/admin/creative/{template_id}/blocks/update'>
<input name='title_zh' value='{title_zh}' placeholder='标题（留空 = 默认：{esc(meta['label_zh'])}）'>
<input name='title_en' value='{title_en}' placeholder='英文标题（可选）'>
<input type='hidden' name='block_key' value='{key}'>
<button class='secondary'>完成</button>
</form></div>"""
        cards.append(
            f"""
<div class='pcard{' disabled' if not enabled and not fixed else ''}' id='{card_id}'>
<div class='top'><span class='nm'>{esc(meta['label_zh'])} <small>{esc(meta['label_en'])}</small>{title_note}</span>
<span class='tools'>{tools}</span></div>{note}{edit_row}
</div>"""
        )
    off_blocks = [b for b in blocks if not b.get("enabled") and not library[str(b.get("key"))]["fixed"]]
    chips = "".join(
        f"<form method='post' action='/admin/creative/{template_id}/blocks/toggle' class='inline'>"
        f"<input type='hidden' name='block_key' value='{esc(str(b.get('key')))}'>"
        f"<button class='pchip'>＋ {esc(library[str(b.get('key'))]['label_zh'])}</button></form>"
        for b in off_blocks
    )
    library_row = (
        f"<div class='muted' style='margin-top:12px'>从板块库添加（点一下打开）：</div><div class='pchips'>{chips}</div>"
        if chips
        else "<p class='guide'>板块库里的板块都已打开。</p>"
    )
    advanced = draft.get("advanced") if isinstance(draft.get("advanced"), dict) else {}
    ai_prompts = advanced.get("ai_prompts") if isinstance(advanced.get("ai_prompts"), dict) else {}
    groups = draft.get("groups") if isinstance(draft.get("groups"), list) else []
    group_zh = "规格参数"
    group_en = "Specifications"
    if groups and isinstance(groups[0], dict):
        group_zh = str(groups[0].get("label_zh") or "规格参数")
        group_en = str(groups[0].get("label_en") or "")
    if versions:
        version_rows = "".join(
            f"<div class='source'><span>v{row['version']} · {esc(row['approved_by'] or row['created_by'] or '')}</span>"
            f"<span class='muted'>{esc((row['approved_at'] or row['created_at'] or '')[:16].replace('T', ' '))}</span></div>"
            for row in versions
        )
    else:
        version_rows = "<p class='muted'>还没有发布历史——第一次「确认上线」后会在这里留档。</p>"
    return f"""
<section class='card'>
<div class='panel-title'><h2>页面由哪些部分组成？</h2><span class='muted'>开关决定显示，上下箭头调顺序</span></div>
<p class='muted' style='margin:0 0 12px'>不想要的可以关掉；想加的从下面「板块库」里挑。板块标题不想改就留空，系统会用默认文字。</p>
{''.join(cards)}
{library_row}
</section>
<details class='card'><summary>AI 助手设置（进阶）— 不改也完全能用</summary><div class='inner'>
<p class='muted' style='margin:0 0 8px'>只有当你的企业启用了 AI 助手时才用得上；设置会保存在这里，启用后生效。</p>
<form method='post' action='/admin/creative/{template_id}/blocks/advanced'>
<label>需求抽取偏好（留空 = 平台默认）<textarea name='ai_extract' placeholder='例如：优先提取性能相关参数'>{esc(str(ai_prompts.get('extract', '')))}</textarea></label>
<label>提问定位偏好（留空 = 平台默认）<textarea name='ai_locate' placeholder='例如：回答时优先引用手册第 3 章'>{esc(str(ai_prompts.get('locate', '')))}</textarea></label>
<p style='margin-top:10px'><button class='secondary'>保存设置</button></p>
</form></div></details>
<details class='card'><summary>分组名称（进阶）— 默认一个「规格参数」组</summary><div class='inner'>
<p class='muted' style='margin:0 0 8px'>给参数分组的名字；多数情况不需要改（进阶分组编辑将在后续版本提供）。</p>
<form method='post' action='/admin/creative/{template_id}/blocks/group'>
<label>分组名称<input name='group_zh' value='{esc(group_zh)}' placeholder='规格参数'></label>
<label>英文名称<input name='group_en' value='{esc(group_en)}' placeholder='Specifications'></label>
<p style='margin-top:10px'><button class='secondary'>保存</button></p>
</form></div></details>
<details class='card'><summary>版本记录 — 每次「确认上线」都会留档</summary><div class='inner'>{version_rows}</div></details>
<details class='card'><summary>恢复默认设置（进阶）— 回到平台推荐组合</summary><div class='inner'>
<p class='muted' style='margin:0 0 8px'>把参数与板块全部恢复为平台标准推荐（相当于重新开始排版）。已上线的版本不受影响；这个操作会留下操作记录。</p>
<form method='post' action='/admin/creative/{template_id}/restore_defaults'>
<p style='margin:0'><button class='warn'>恢复默认设置</button></p>
</form></div></details>
<div class='step-cta'><a class='button' href='/admin/creative/{template_id}?step=preview'>下一步 · 看效果 →</a></div>
"""


# ── V1-01 步骤② 企业设置：线索阶段 / RFQ 表单字段 / 通知接收人 ──────────────────
SETTING_LEAD_STAGES = "lead_stages"
SETTING_RFQ_FIELDS = "rfq_fields"
SETTING_RFQ_FIELDS_BY_CATEGORY = "rfq_fields_by_category"
SETTING_NOTIFY_RECIPIENTS = "notify_recipients"

# 阶段 KEY 集合首版固定（仅支持改名/排序/终态；增删已登记为暂缓项 F-15）。
DEFAULT_LEAD_STAGES = (
    {"key": "new", "label": "新线索", "terminal": False},
    {"key": "needs_info", "label": "待补充", "terminal": False},
    {"key": "technical_review", "label": "技术确认中", "terminal": False},
    {"key": "quote_ready", "label": "可进入报价", "terminal": False},
    {"key": "paused", "label": "暂缓", "terminal": True},
    {"key": "closed", "label": "已关闭", "terminal": True},
)
LEAD_STAGE_KEYS = tuple(stage["key"] for stage in DEFAULT_LEAD_STAGES)

# RFQ 基础字段三态：required=必填（缺一不可）/ optional=选填（可空，缺项进"还缺"清单）/ hidden=隐藏（不收集）。
# 默认值=用户拍板：邮箱/电话/数量/目的地必填、备注选填；邮箱与电话不可同时隐藏。
DEFAULT_RFQ_FIELDS = {"email": "required", "phone": "required", "quantity": "required", "destination": "required", "notes": "optional"}
RFQ_FIELD_ORDER = ("email", "phone", "quantity", "destination", "notes")
RFQ_FIELD_LABELS = {"email": "邮箱", "phone": "电话", "quantity": "数量", "destination": "目的地", "notes": "备注"}
RFQ_FIELD_STATES = ("required", "optional", "hidden")
# 字段 → 建询价入参键（create 用 region；text 来自表单/接口）与内容键（revise/落库 destination）。
RFQ_FIELD_INPUT_KEYS = {"email": "contact_email", "phone": "contact_phone", "quantity": "quantity", "destination": "region", "notes": "notes"}
RFQ_FIELD_CONTENT_KEYS = {"email": "contact_email", "phone": "contact_phone", "quantity": "quantity", "destination": "destination", "notes": "notes"}

# ── V1-09 销售工作台增量：跟进节点（首版固定三项；企业自定义暂缓）与回复渠道 ──
LEAD_MILESTONE_DEFS = (("tech_confirmed", "技术确认"), ("sample", "样品准备"), ("quote", "报价准备"))
LEAD_MILESTONE_KEYS = tuple(key for key, _label in LEAD_MILESTONE_DEFS)
# 平台尚未接入外发渠道：回复先以草稿存在，线下实际发送后回来记录（渠道仅作说明）。
REPLY_CHANNELS = (("email", "邮件"), ("phone", "电话"), ("whatsapp", "WhatsApp"), ("other", "其他"))
REPLY_CHANNEL_LABELS = dict(REPLY_CHANNELS)

EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

# 操作记录页的中文标签（未知值回退原值显示）。
AUDIT_ENTITY_LABELS = {
    "conversation": "会话", "requirement_revision": "工况修订", "recommendation": "推荐",
    "human_task": "人工任务", "rfq": "询价单", "lead": "线索", "product": "产品",
    "product_field": "产品字段", "source_document": "来源资料", "user": "成员账号", "settings": "企业设置",
    "material_task": "资料补充任务", "export": "数据导出", "reply": "回复草稿", "template": "产品展示页",
    "channel": "渠道", "message": "渠道消息",
}
AUDIT_ACTION_LABELS = {
    "created": "创建", "updated": "更新", "activated": "启用", "deactivated": "停用",
    "submitted": "提交", "buyer_revised": "买方修改", "claimed": "领取", "assigned": "分配",
    "stop_proactive_contact": "停止主动联系", "handoff_code_created": "生成交接码", "handoff_code_exchanged": "交接码换取凭据",
    "confirmed": "确认工况", "candidate_saved": "保存候选", "generated": "生成推荐",
    "asked_buyer": "向买方追问", "buyer_replied": "买方回复", "resolved": "完成答复",
    "registered": "登记", "scope_updated": "修订归属", "extraction_rerun": "重新提取", "extraction_completed": "完成提取",
    "updated_candidate": "更新候选", "field_approved": "核准字段", "field_rejected": "驳回字段", "field_reopened": "回到候选",
    "review_finalized": "完成核对", "published": "发布版本", "withdrawn": "下架",
    "allowed_public_download": "开放下载", "revoked_public_download": "撤销下载",
    "usage_changed": "更新文件可见性", "public_permission_revoked_on_usage_change": "可见性变更撤销公开",
    "category_candidate_set": "登记品类候选", "category_approved": "核准品类", "category_candidate_cleared": "清除品类候选",
    "nurture_created": "登记待培育线索", "merged": "线索被并入", "merged_in": "并入重复线索",
    "public_download_blocked_hash_mismatch": "拦截下载（哈希不符）", "modules_updated": "模块授权",
    "exported": "导出数据", "completed": "完成", "sent": "记录发送", "discarded": "废弃",
    "sent_via_channel": "经渠道发送回复", "outbound_retried": "重试渠道消息",
    "milestone_completed": "节点完成", "milestone_reopened": "节点重开",
    "template_created": "创建展示页", "template_draft_saved": "保存展示页改动",
    "template_submitted": "提交展示页给管理员", "template_published": "确认上线展示页", "template_withdrawn": "撤回展示页",
    "template_restored_defaults": "恢复默认设置",
    "channel_configured": "保存渠道配置", "channel_enabled": "启用渠道", "channel_disabled": "停用渠道",
    "conversation_ai_switch": "切换AI自动回复", "rfq_form_created": "生成询价表单链接",
    "staff_replied": "发送卖家回复",
}


class Handler(BaseHTTPRequestHandler):
    db: Database
    # AI 调用边界：由 make_server 按显式模式装配（rules 时 ai 为空操作对象，不发外部请求）。
    ai: AIService | None = None
    # 演示工具（页面按钮 + seed 端点）为部署级开关：默认关闭；唯一开启方式见 make_server / tools/demo/start_demo.py。
    demo_tools = False
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
        self.user_modules = (
            set() if user["role"] == "admin" else set(self.db.get_user_modules(user["id"]))
        )
        # 访问要求（V1-01 模块授权制）：None=登录即可；"admin"=仅管理员；"module:<key>"=需该模块授权。
        # 未列入的 /admin 子路径一律拒绝（fail-closed），与既往"else=空集"语义一致。
        requirement: str | None = "deny"
        if path in ("/admin", "/admin/nav-counts"):
            requirement = None
        elif path.startswith("/admin/demo") or path.startswith("/admin/lowara") or path.startswith("/admin/users") or path.startswith("/admin/settings") or path.startswith("/admin/channels") or path.startswith("/admin/audit") or path.startswith("/admin/analytics") or path.startswith("/admin/export") or path.startswith("/admin/material-tasks"):
            requirement = "admin"
        else:
            for prefix, module_key in (
                ("/admin/sites", "sites"),
                ("/admin/products", "products"),
                ("/admin/tasks", "tasks"),
                ("/admin/sales", "sales"),
                ("/admin/leads", "sales"),
                ("/admin/rfqs", "rfqs"),
                ("/admin/creative", "creative"),
                ("/admin/inbox", "inbox"),
            ):
                if path.startswith(prefix):
                    requirement = f"module:{module_key}"
                    break
        if requirement == "deny":
            permitted = False
        elif requirement == "admin":
            permitted = self.user_role == "admin"
        elif requirement and requirement.startswith("module:"):
            permitted = self.has_module(requirement.split(":", 1)[1])
        else:
            permitted = True
        if not permitted:
            area, required, why = admin_permission_area(path)
            role_label = "管理员" if self.user_role == "admin" else "员工"
            self.send_html(self.console_page("无权限", permission_notice(area, role_label, required, why), ""), 403)
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

    def send_twiml(self, body: str = "<Response/>", status: int = 200) -> None:
        """Twilio 消息 webhook 的标准应答：text/xml + 合法 TwiML。

        空 <Response/> 表示不通过 TwiML 附加动作（我们的回复一律走 REST API，平台是唯一主脑）。
        返回 JSON 会被 Twilio 判为 12300 Invalid Content-Type，故必须 XML。
        """
        data = f"<?xml version=\"1.0\" encoding=\"UTF-8\"?>{body}".encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/xml; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
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

    def nav_badge_counts(self) -> dict[str, int]:
        """侧栏待办数字：与概览页待办同一计数函数（get_admin_summary），值含 0。

        可见集合 = 账号已授权的模块（管理员=全模块；V1-01 模块授权制）；未登录返回空字典。
        仅返回数字本身，不含任何业务内容。
        """
        if not self.user_id:
            return {}
        summary = self.db.get_admin_summary(self.tenant_id)
        counts: dict[str, int] = {}
        if self.has_module("products"):
            counts["products"] = summary["products_needs_review"]
        if self.has_module("tasks"):
            counts["tasks"] = summary["tasks_pending"]
        if self.has_module("rfqs"):
            counts["rfqs"] = summary["rfqs_needs_info"]
        if self.has_module("sales"):
            counts["sales"] = summary["leads_overdue"]
        return counts

    def console_page(self, title: str, body: str, active: str, lang: str = "zh") -> str:
        tenant = self.db.conn.execute("SELECT name FROM tenants WHERE id=?", (self.tenant_id,)).fetchone()
        clean_path = urlparse(self.path).path.rstrip("/") or "/admin"
        return console_layout(
            title, body, active, lang=lang, path=clean_path,
            actor=self.actor if self.user_id else "",
            role=self.user_role if self.user_id else "",
            tenant=(tenant["name"] if tenant else self.tenant_id),
            nav_counts=self.nav_badge_counts(),
            nav_keys=self.console_nav_keys(),
            inbox_items=self.inbox_nav_items(),
        )

    def tenant_display_name(self) -> str:
        """控制台页眉与工具栏显示的当前企业名称（读 tenants 表）；无记录时回退租户标识。"""
        row = self.db.conn.execute("SELECT name FROM tenants WHERE id=?", (self.tenant_id,)).fetchone()
        return row["name"] if row and row["name"] else self.tenant_id

    def has_module(self, module: str) -> bool:
        """当前请求上下文的模块权限：管理员=全模块；员工=已授权模块集合（V1-01）。"""
        if self.user_role == "admin":
            return True
        return module in getattr(self, "user_modules", set())

    def inbox_nav_items(self) -> list[tuple[str, str, str, str]]:
        """侧栏「会话收件箱」子模块：网站聊天 + 来源平台（按数据与接入状态动态生成；无权限时为空）。"""
        if not self.has_module("inbox"):
            return []
        items = [("inbox_web", "/admin/inbox/web", "chat", "网站聊天")]
        for section in inbox_sections(self.db, self.tenant_id):
            if section["slug"] == "web":
                continue
            items.append((f"inbox_{section['slug']}", f"/admin/inbox/{section['slug']}", section["icon"], section["label"]))
        return items

    def console_nav_keys(self) -> set[str]:
        """侧栏可见集：概览人人可见；管理员另见成员管理/渠道接入等；员工按已授权模块。"""
        keys = {"admin"}
        if self.user_role == "admin":
            keys.add("users")
            keys.add("settings")
            keys.add("audit")
            keys.add("analytics")
            keys.add("channels")
        for module_key in GRANTABLE_MODULE_KEYS:
            if self.has_module(module_key):
                keys.add(module_key)
        # 会话收件箱子模块（网站聊天 + 来源平台）由 inbox_nav_items 按权限与数据动态生成，此处不宣告固定键。
        return keys

    def login_page(self, query: dict[str, list[str]], error: str = "") -> None:
        if self.current_user():
            self.redirect("/admin")
            return
        next_path = query.get("next", ["/admin/sales"])[0]
        # 报错方式（复刻设计稿）：表单汇总错误条 —— 顶部红条 + 图钉图标，字段照常保留。
        error_html = f"<div class='form-alert' role='alert'><div><b>无法登录。</b>{esc(error)}，请检查后重试。</div></div>" if error else ""
        body = f"""
<button class='theme-toggle fixed' id='themeToggle' type='button' title='切换浅色 / 深色（快捷键 D）' aria-label='切换主题'>{THEME_TOGGLE_SVG}</button>
<section class='auth-card'>
<div class='auth-brand'><img class='brand-mark' src='/assets/brand.png' alt=''><div><strong>Growave</strong><small>SELLER CONSOLE</small></div></div>
<h1>企业后台登录</h1>
<p class='auth-sub'>登录后只能访问当前账号所属企业的数据。买方公开页面不需要后台账号。</p>
{error_html}
<form method='post' action='/login'><input type='hidden' name='next' value='{esc(next_path)}'><label>邮箱</label><input name='email' type='email' autocomplete='username' required placeholder='name@company.example'><label>密码</label><input name='password' type='password' autocomplete='current-password' required><p class='auth-submit'><button class='wide'>登录</button></p></form>
<p class='footer-note'>新部署的数据库不预置任何账号：请先用 `python3 tools/create_admin.py` 创建管理员。本地演示环境由 `python3 tools/demo/start_demo.py` 创建（文档化的演示账号 demo-admin / demo-sales / demo-engineer，密码与账号相同，仅限本地演示）。</p>
</section>
"""
        self.send_html(auth_layout("企业后台登录", body), 200)

    def login_post(self, form: dict[str, str | bytes]) -> None:
        user = self.db.authenticate_user(str(form.get("email", "")), str(form.get("password", "")))
        if not user:
            self.login_page({"next": [str(form.get("next", "/admin/sales"))]}, "邮箱或密码不正确")
            return
        next_path = str(form.get("next", "/admin/sales"))
        if not next_path.startswith("/") or next_path.startswith("//") or next_path.startswith("/login") or next_path.startswith("/admin/login"):
            # 登录页自身或非法目标：回到后台首页，避免登录成功后跳进登录相关路径
            next_path = "/admin"
        if next_path == "/admin/sales" and user["role"] != "admin" and "sales" not in set(self.db.get_user_modules(user["id"])):
            # 默认落点自适应（V1-01 模块授权制）：没有「销售工作台」模块的员工落到概览，避免登录即见 403
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
            "/reference-client/chat.css": ("chat.css", "text/css; charset=utf-8"),
            "/reference-client/product-page.js": ("product-page.js", "text/javascript; charset=utf-8"),
            "/reference-client/product-page.css": ("product-page.css", "text/css; charset=utf-8"),
        }
        if path in reference_files:
            self.send_reference_file(*reference_files[path])
            return
        if path == "/api/v1/channels/whatsapp/webhook":
            self.handle_whatsapp_webhook_verify()
            return
        if path == "/assets/brand.png":
            asset = ROOT / "assets" / "brand.png"
            if not asset.exists():
                self.send_error(404)
                return
            data = asset.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        rfq_link_match = re.fullmatch(r"/rfq/([A-Za-z0-9_-]{16,96})", path)
        if rfq_link_match:
            self.public_rfq_form(rfq_link_match.group(1))
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
                {"id": row["id"], "question_message_id": row["question_message_id"], "product_id": row["product_id"], "model": row["model"], "status": row["answer_status"], "text": row["answer_text"], "citations": json.loads(row["citations_json"])}
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
                {"conversation_id": conversation["id"], "status": conversation["status"], "locale": conversation["locale"], "contact_preference": {"stop_proactive_contact": bool(conversation["stop_proactive_contact"]), "reason": conversation["stop_reason"], "source": conversation["stop_source"], "stopped_at": conversation["stopped_at"]}, "messages": messages, "requirement": requirement, "recommendation": recommendation, "answers": answers, "human_tasks": human_tasks, "rfqs": rfqs, "rfq_fields": self.db.get_rfq_field_config(site["tenant_id"], self.db.conversation_category_hint(site["tenant_id"], conversation["id"]))},
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
        if path == "/admin/nav-counts":
            # 侧栏待办数字轮询接口：登录 + 角色过滤 + 租户隔离；只返回数字，不含任何内容。
            self.send_json({"counts": self.nav_badge_counts()})
            return
        if path == "/admin":
            self.admin_dashboard(query)
            return
        if path == "/admin/users":
            self.admin_users(query)
            return
        if path == "/admin/settings":
            self.admin_settings(query)
            return
        if path == "/admin/audit":
            self.admin_audit(query)
            return
        match = re.fullmatch(r"/admin/inbox/([a-z0-9_-]{1,40})", path)
        if match:
            self.admin_inbox(match.group(1), query)
            return
        match = re.fullmatch(r"/admin/inbox/([a-z0-9_-]{1,40})/([a-f0-9]+)", path)
        if match:
            self.admin_inbox_conversation(match.group(1), match.group(2), query)
            return
        if path == "/admin/channels":
            self.admin_channels(query)
            return
        if path == "/admin/analytics":
            self.admin_analytics(query)
            return
        if path == "/admin/creative":
            self.admin_creative(query)
            return
        match = re.fullmatch(r"/admin/creative/([a-f0-9]+)/?", path)
        if match:
            self.admin_creative_editor(match.group(1), query)
            return
        match = re.fullmatch(r"/admin/creative/([a-f0-9]+)/preview", path)
        if match:
            self.admin_creative_preview(match.group(1), query)
            return
        if re.fullmatch(r"/admin/export/(products|rfqs|leads)(\.csv)?", path):
            self.admin_export(path.rsplit("/", 1)[1].split(".")[0])
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
        match = re.fullmatch(r"/admin/leads/([a-f0-9]+)/?", path)
        if match:
            self.admin_nurture_lead(match.group(1), query)
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
            self.public_source(
                match.group(1), match.group(2),
                preview=query.get("preview", [""])[0] == "1",
                log_events=query.get("nolog", [""])[0] != "1",
            )
            return
        match = re.fullmatch(r"/s/([A-Za-z0-9_-]+)/products/([a-zA-Z0-9-]+)/sources/([a-f0-9]+)", path)
        if match:
            site = self.db.get_buyer_site(match.group(1))
            if not site:
                self.send_html(layout("资料不可用", "<div class='error'>网站入口不可用。</div>", public=True), 404)
                return
            self.public_source(
                match.group(2), match.group(3), site,
                preview=query.get("preview", [""])[0] == "1",
                log_events=query.get("nolog", [""])[0] != "1",
            )
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
        if path == "/api/v1/buyer/handoff/exchange":
            # 跨站会话交接：产品页（平台自身来源）用一次性交接码换取同一会话的新凭据。
            # 只在平台页面来源上受理——请求带 Origin 且与请求 Host 不同源时直接拒绝，
            # 且本端点永不返回跨域放行头；交接码本身是唯一秘密，长期会话凭据不进 URL。
            payload = self.read_json()
            if payload is None:
                return
            origin = self.headers.get("Origin")
            same_host_page = bool(origin) and urlparse(origin).netloc == self.headers.get("Host") and urlparse(origin).scheme in {"http", "https"}
            if origin and not same_host_page:
                self.send_json({"error": "origin_not_allowed"}, 403)
                return
            site = self.db.get_buyer_site(self.headers.get("X-Site-Key", "").strip())
            if not site:
                self.send_json({"error": "site_not_found"}, 404)
                return
            code = payload.get("code")
            product_id = payload.get("product_id")
            if not isinstance(code, str) or not code.strip() or not isinstance(product_id, str) or not product_id.strip():
                self.send_json({"error": "handoff_parameters_required"}, 422)
                return
            product = self.db.get_product(site["tenant_id"], product_id.strip())
            if not product or product["status"] == "withdrawn" or not self.db.get_published_version(site["tenant_id"], product["id"]):
                # 该站点企业下没有这个可公开型号（含跨企业、未发布、已撤回）：视为绑定不符，不发凭据
                self.send_json({"error": "handoff_binding_mismatch"}, 403)
                return
            result = self.db.exchange_handoff_code(code.strip(), site["site_key"], product["id"], site["tenant_id"])
            if not result["ok"]:
                statuses = {"handoff_not_found": 404, "handoff_expired": 410, "handoff_already_used": 409, "handoff_binding_mismatch": 403}
                self.send_json({"error": str(result["error"])}, statuses.get(str(result["error"]), 400))
                return
            self.send_json({"conversation_id": result["conversation_id"], "access_token": result["access_token"]}, 200)
            return
        api_match = re.fullmatch(r"/api/v1/buyer/conversations/([a-f0-9]+)/handoff-codes", path)
        if api_match:
            # 持有会话凭据的原官网聊天（已登记来源）为产品链接出具一次性交接码
            access = self.api_conversation(api_match.group(1))
            if not access:
                return
            payload = self.read_json()
            if payload is None:
                return
            site, conversation = access
            origin = site["allowed_origin"] if self.headers.get("Origin") else None
            product_id = payload.get("product_id")
            if not isinstance(product_id, str) or not product_id.strip():
                self.send_json({"error": "product_required"}, 422, origin)
                return
            product = self.db.get_product(site["tenant_id"], product_id.strip())
            if not product or product["status"] == "withdrawn" or not self.db.get_published_version(site["tenant_id"], product["id"]):
                self.send_json({"error": "product_not_found"}, 404, origin)
                return
            code, expires_at = self.db.create_handoff_code(site["tenant_id"], site["site_key"], conversation["id"], product["id"])
            self.send_json({"code": code, "expires_at": expires_at, "product_id": product["id"]}, 201, origin)
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
                    "SELECT id, content, request_hash, result_revision_id FROM messages WHERE tenant_id=? AND conversation_id=? AND idempotency_key=?",
                    (site["tenant_id"], conversation["id"], idempotency_key),
                ).fetchone()

            def lookup_keyed_revision() -> sqlite3.Row | None:
                return self.db.conn.execute(
                    "SELECT id, values_json FROM requirement_revisions WHERE tenant_id=? AND conversation_id=? AND idempotency_key=?",
                    (site["tenant_id"], conversation["id"], idempotency_key),
                ).fetchone()

            def draft_payload(values: dict[str, dict[str, str]], revision_id: str) -> dict[str, object]:
                missing = [key for key in MATCH_REQUIRED_FIELDS if not values.get(key, {}).get("value", "").strip()]
                askable = [key for key in missing if values.get(key, {}).get("state") != "unknown"]
                return {"revision_id": revision_id, "values": values, "missing": missing, "next_question": askable[0] if askable else None}

            def attached_draft(existing: sqlite3.Row) -> dict[str, object] | None:
                """返回消息已附加且仍存在的修订草稿；消息从未生成草稿（首次提取失败）时为 None。"""
                if not existing["result_revision_id"]:
                    return None
                stored = self.db.conn.execute(
                    "SELECT * FROM requirement_revisions WHERE id=? AND tenant_id=?",
                    (existing["result_revision_id"], site["tenant_id"]),
                ).fetchone()
                return draft_payload(json.loads(stored["values_json"]), stored["id"]) if stored else None

            def hash_conflict(existing: sqlite3.Row) -> bool:
                return bool(existing["request_hash"]) and not hmac.compare_digest(existing["request_hash"], request_hash)

            message_id = None
            retrying_saved = False  # 复用首次尝试已存下的消息补生成草稿（当时提取失败）
            if idempotency_key:
                existing = lookup_keyed()
                if existing:
                    if hash_conflict(existing):
                        self.send_json({"error": "idempotency_conflict"}, 409, origin)
                        return
                    draft = attached_draft(existing)
                    if draft:
                        self.send_json({"message_id": existing["id"], "status": "duplicate", "requirement_draft": draft}, 200, origin)
                        return
                    # 首次尝试只落了消息、未生成草稿（如提取失败）：复用已存消息补提取，不重复存消息。
                    message_id = existing["id"]
                    text_value = existing["content"]
                    retrying_saved = True
            if message_id is None:
                try:
                    message_id = self.db.save_message(
                        site["tenant_id"], conversation["id"], "buyer", text_value, "website_api",
                        idempotency_key=idempotency_key or None, request_hash=request_hash if idempotency_key else None,
                    )
                except sqlite3.IntegrityError:
                    # 并发同键：另一请求已写入；回滚本次事务后按已存消息继续（含补提取）。
                    self.db.conn.rollback()
                    existing = lookup_keyed()
                    if not existing:
                        raise
                    if hash_conflict(existing):
                        self.send_json({"error": "idempotency_conflict"}, 409, origin)
                        return
                    draft = attached_draft(existing)
                    if draft:
                        self.send_json({"message_id": existing["id"], "status": "duplicate", "requirement_draft": draft}, 200, origin)
                        return
                    message_id = existing["id"]
                    text_value = existing["content"]
                    retrying_saved = True
            try:
                previous = self.db.get_latest_revision(site["tenant_id"], conversation["id"])
                previous_values = json.loads(previous["values_json"]) if previous else {}
                values = extract_requirement_candidates(
                    self.db, self.ai, site["tenant_id"], conversation["id"], text_value, None, previous_values,
                )
                revision_id = self.db.create_requirement_revision(
                    site["tenant_id"], conversation["id"], values, "candidate", "website_api",
                    idempotency_key=idempotency_key or None, request_hash=request_hash if idempotency_key else None,
                )
            except sqlite3.IntegrityError:
                # 同键并发：另一请求已生成该键的修订；回滚并复用已存修订，不产生第二个草稿。
                self.db.conn.rollback()
                stored = lookup_keyed_revision() if idempotency_key else None
                if stored:
                    revision_id = stored["id"]
                    values = json.loads(stored["values_json"])
                else:
                    # 其他完整性问题按提取失败处理：消息已保存、可重试，不假装成功。
                    self.send_json({"error": "extraction_unavailable", "message": "需求提取暂不可用；您的消息已保存，可稍后重试。"}, 503, origin)
                    return
            except Exception:
                # 提取失败时消息已保存；不得假装已识别，返回可继续的失败状态供重试。
                self.send_json({"error": "extraction_unavailable", "message": "需求提取暂不可用；您的消息已保存，可稍后重试。"}, 503, origin)
                return
            if idempotency_key:
                self.db.conn.execute("UPDATE messages SET result_revision_id=? WHERE id=?", (revision_id, message_id))
                self.db.conn.commit()
            draft = draft_payload(values, revision_id)
            if retrying_saved:
                self.send_json({"message_id": message_id, "status": "duplicate", "requirement_draft": draft}, 200, origin)
                return
            self.send_json({"message_id": message_id, "status": "saved", "requirement_draft": draft}, 201, origin)
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
                if conversation["ai_paused"]:
                    # 收件箱「暂停 AI」开关：暂停自动答复（问题已保存、按待人工处理；不自动建任务）。
                    status, answer_text, answered_product_id, version_id, citations, _reason = ("needs_human", AI_PAUSED_ANSWER, None, None, [], "ai_paused")
                else:
                    # 第二波：网页聊天入口同样优先走 Dify（与渠道口径一致；引擎不可用时回落本地）。
                    status, answer_text, answered_product_id, version_id, citations, _reason, _qa_source = answer_question_prefer_dify(
                        self.db, self.ai, site["tenant_id"], conversation["id"], question,
                        product_id if isinstance(product_id, str) and product_id else None,
                        f"web:{conversation['id']}",
                    )
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
            entry_surface = str(payload.get("entry_surface", "chat"))
            if not isinstance(contact, dict) or not all(isinstance(value, str) for value in contact.values()) or payload.get("contact_confirmed") is not True or not submission_key or entry_surface not in {"chat", "product_page"}:
                self.send_json({"error": "confirmed_contact_and_submission_key_required"}, 422)
                return
            # V1-01 步骤②：必填校验在 create_rfq 内按企业设置统一执行（三个买方入口同口径）。
            notes = payload.get("notes", "")
            product_id = payload.get("product_id")
            requested_model = payload.get("requested_model", "")
            if not isinstance(notes, str) or (product_id is not None and not isinstance(product_id, str)) or not isinstance(requested_model, str):
                self.send_json({"error": "invalid_rfq"}, 422)
                return
            if product_id and not self.db.get_published_version(site["tenant_id"], product_id):
                self.send_json({"error": "product_not_found"}, 404)
                return
            try:
                rfq_id, created = self.db.create_rfq(site["tenant_id"], conversation["id"], submission_key, contact, notes, product_id, entry_surface, requested_model)
            except ConflictError:
                self.send_json({"error": "idempotency_conflict"}, 409)
                return
            except ValueError as error:
                message = str(error)
                code = "rfq_required_fields_missing" if message.startswith("请填写") else "invalid_rfq_request"
                self.send_json({"error": code, "message": message}, 422)
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
            allowed = {"quantity", "destination", "requested_delivery", "notes", "product_id", "requested_model"}
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
        langbot_hook_match = re.fullmatch(r"/api/v1/channels/langbot/webhook/([A-Za-z0-9_-]{16,96})", path)
        if langbot_hook_match:
            self.handle_langbot_webhook(langbot_hook_match.group(1))
            return
        personal_hook_match = re.fullmatch(r"/api/v1/channels/whatsapp-personal/webhook/([A-Za-z0-9_-]{16,96})", path)
        if personal_hook_match:
            self.handle_whatsapp_personal_webhook(personal_hook_match.group(1))
            return
        if path == "/api/v1/channels/whatsapp/webhook":
            self.handle_whatsapp_webhook()
            return
        if path == "/api/v1/channels/whatsapp/status":
            self.handle_whatsapp_status()
            return
        rfq_submit_match = re.fullmatch(r"/rfq/([A-Za-z0-9_-]{16,96})", path)
        if rfq_submit_match:
            self.public_rfq_submit(rfq_submit_match.group(1), parse_multipart(self))
            return
        if path.startswith("/admin/") and not self.authorize_admin(path, "POST"):
            return
        if path in DEMO_TOOL_PATHS and (not self.demo_tools or self.user_role != "admin"):
            # 正式化清单 F-1～F-3 + V1-01：演示工具=部署级开关且只对管理员开放；
            # 员工即使持有产品模块，也不得看到或触发演示数据重建。
            reason = (
                "演示工具未启用：这是部署级的演示开关，只有通过 tools/demo/start_demo.py 启动的演示环境才可使用。"
                if not self.demo_tools
                else "演示工具只对管理员开放；演示数据的加载与重建请联系管理员操作。"
            )
            self.send_html(self.console_page("演示工具不可用", f"<div class='error'>{reason}</div>", ""), 403)
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
                    str(form.get("role", "employee")),
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
                try:
                    values = extract_requirement_candidates(
                        self.db, self.ai, self.tenant_id, conversation_id, message, explicit, previous_values,
                    )
                except AIUnavailableError:
                    # 模型故障：消息已保存，明确提示可重试；不落草稿、不假装已识别。
                    self.redirect(f"/inquiry/{conversation_id}?notice=extract_unavailable")
                    return
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
                try:
                    paused_conversation = self.db.get_conversation(self.tenant_id, conversation_id)
                    if paused_conversation and paused_conversation["ai_paused"]:
                        # 收件箱「暂停 AI」开关（旧询价页同口径）。
                        answer_status, answer_text, answered_product_id, product_version_id, citations, _reason = ("needs_human", AI_PAUSED_ANSWER, None, None, [], "ai_paused")
                    else:
                        # 第二波：旧询价页同口径优先走 Dify（引擎不可用时回落本地）。
                        answer_status, answer_text, answered_product_id, product_version_id, citations, _reason, _qa_source = answer_question_prefer_dify(
                            self.db, self.ai, self.tenant_id, conversation_id, question,
                            selected_product_id if isinstance(selected_product_id, str) and selected_product_id else None,
                            f"web:{conversation_id}",
                        )
                except AIUnavailableError:
                    # 模型故障：问题已保存，明确提示可重试；不落假答案、不建人工任务。
                    self.redirect(f"/inquiry/{conversation_id}?notice=answer_unavailable")
                    return
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
                try:
                    rfq_id, created = self.db.create_rfq(self.tenant_id, conversation_id, submission_key, contact, str(form.get("notes", "")), str(form.get("product_id", "")) or None)
                except ConflictError:
                    self.redirect(f"/inquiry/{conversation_id}?notice=rfq_conflict")
                    return
                except ValueError as error:
                    message = str(error)
                    notice = "rfq_required" if ("请填写" in message or "请保留" in message) else "rfq_missing"
                    self.redirect(f"/inquiry/{conversation_id}?notice={notice}")
                    return
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
                    message = str(error)
                    notice = "rfq_required" if ("请填写" in message or "请保留" in message) else "rfq_missing"
                    self.redirect(f"/inquiry/{conversation_id}?notice={notice}&rfq_id={rfq_id}")
                    return
                self.redirect(f"/inquiry/{conversation_id}?notice=rfq&rfq_id={rfq_id}&created=0&revised=1")
                return
            match = re.fullmatch(r"/admin/tasks/([a-f0-9]+)/reply", path)
            if match:
                self.db.resolve_human_task(self.tenant_id, match.group(1), str(form.get("response", "")), self.actor)
                self.deliver_latest_task_message(match.group(1), "human-engineer")
                self.redirect(f"/admin/tasks/{match.group(1)}?notice=resolved")
                return
            match = re.fullmatch(r"/admin/tasks/([a-f0-9]+)/followup", path)
            if match:
                self.db.ask_task_followup(self.tenant_id, match.group(1), str(form.get("question", "")), self.actor)
                self.deliver_latest_task_message(match.group(1), f"engineer-followup:{match.group(1)}")
                self.redirect(f"/admin/tasks/{match.group(1)}?notice=followup")
                return
            match = re.fullmatch(r"/admin/sales/([a-f0-9]+)/update", path)
            if match:
                rfq_id = match.group(1)
                try:
                    self.db.update_lead(
                        self.tenant_id, rfq_id,
                        str(form.get("stage", "new")), str(form.get("next_follow_up", "")), str(form.get("note", "")),
                        self.actor, close_reason=str(form.get("close_reason", "")),
                    )
                except ValueError as error:
                    message = str(error)
                    notice = "close_reason" if "关闭线索需要填写原因" in message else "stop_conflict" if "停止主动联系" in message else "update_invalid"
                    self.redirect(f"/admin/sales/{rfq_id}?notice={notice}#lead-update")
                    return
                self.redirect(f"/admin/sales/{rfq_id}?notice=updated")
                return
            match = re.fullmatch(r"/admin/sales/([a-f0-9]+)/milestones/([a-z_]+)", path)
            if match:
                rfq_id, milestone_key = match.groups()
                try:
                    activated = self.db.toggle_lead_milestone(self.tenant_id, rfq_id, milestone_key, self.actor)
                except ValueError:
                    self.redirect(f"/admin/sales/{rfq_id}?notice=milestone_invalid#milestones")
                    return
                self.redirect(f"/admin/sales/{rfq_id}?notice={'milestone_on' if activated else 'milestone_off'}#milestones")
                return
            match = re.fullmatch(r"/admin/sales/([a-f0-9]+)/replies", path)
            if match:
                try:
                    self.db.create_reply_draft(self.tenant_id, match.group(1), str(form.get("body", "")), self.actor)
                except ValueError:
                    self.redirect(f"/admin/sales/{match.group(1)}?notice=reply_invalid#replies")
                    return
                self.redirect(f"/admin/sales/{match.group(1)}?notice=reply_saved#replies")
                return
            match = re.fullmatch(r"/admin/sales/([a-f0-9]+)/replies/([a-f0-9]+)", path)
            if match:
                rfq_id, reply_id = match.groups()
                action = str(form.get("action", "save"))
                try:
                    if action == "sent":
                        self.db.record_reply_sent(self.tenant_id, reply_id, str(form.get("channel", "")), str(form.get("body", "")), self.actor)
                        notice_code = "reply_sent"
                    elif action == "channel":
                        # F-22：经出站总线真发（draft/queued/failed → queued → sent/failed）
                        sent, _error = self.db.send_reply_via_channel(self.tenant_id, reply_id, self.actor)
                        notice_code = "reply_channel_sent" if sent else "reply_channel_failed"
                    elif action == "discard":
                        self.db.discard_reply_draft(self.tenant_id, reply_id, self.actor)
                        notice_code = "reply_discarded"
                    else:
                        self.db.update_reply_draft(self.tenant_id, reply_id, str(form.get("body", "")), self.actor)
                        notice_code = "reply_saved"
                except ValueError as error:
                    message = str(error)
                    notice = "reply_stop_blocked" if "停止主动联系" in message else "reply_invalid"
                    self.redirect(f"/admin/sales/{rfq_id}?notice={notice}#replies")
                    return
                self.redirect(f"/admin/sales/{rfq_id}?notice={notice_code}#replies")
                return
            match = re.fullmatch(r"/admin/sales/([a-f0-9]+)/(claim|assign)", path)
            if match:
                if match.group(2) == "claim":
                    if self.user_role == "admin" or not self.has_module("sales"):
                        self.send_html(self.console_page("无权操作", permission_notice("领取销售线索", "管理员" if self.user_role == "admin" else "员工", "模块权限「销售工作台」", "「领取」由获得销售工作台模块授权的员工执行；管理员可在同一页面直接分配。"), "sales"), 403)
                        return
                    self.db.claim_conversation_leads(self.tenant_id, match.group(1), self.actor)
                else:
                    if self.user_role != "admin":
                        self.send_html(self.console_page("无权操作", permission_notice("分配销售线索", "员工", "管理员", "分配或改派线索只对管理员开放。"), "sales"), 403)
                        return
                    self.db.assign_conversation_leads(self.tenant_id, match.group(1), str(form.get("owner_email", "")), self.actor)
                self.redirect(f"/admin/sales/{match.group(1)}?notice=assigned")
                return
            match = re.fullmatch(r"/admin/sales/([a-f0-9]+)/contact-stop", path)
            if match:
                if not self.has_module("sales"):
                    role_label = "管理员" if self.user_role == "admin" else "员工"
                    self.send_html(self.console_page("无权操作", permission_notice("记录买方联系要求", role_label, "模块权限「销售工作台」", "记录买方联系要求需要销售工作台模块权限。"), "sales"), 403)
                    return
                rfq = self.db.get_rfq(self.tenant_id, match.group(1))
                if not rfq:
                    raise ValueError("询价不存在")
                self.db.stop_proactive_contact(self.tenant_id, rfq["conversation_id"], str(form.get("reason", "")), "seller_record", self.actor)
                self.redirect(f"/admin/sales/{match.group(1)}?notice=contact-stop")
                return
            if path == "/admin/sales/nurture":
                if not self.has_module("sales"):
                    role_label = "管理员" if self.user_role == "admin" else "员工"
                    self.send_html(self.console_page("无权操作", permission_notice("登记待培育线索", role_label, "模块权限「销售工作台」", "登记待培育线索需要销售工作台模块权限。"), "sales"), 403)
                    return
                lead_id = self.db.create_nurture_lead(
                    self.tenant_id, self.actor,
                    str(form.get("contact_name", "")), str(form.get("company", "")),
                    str(form.get("contact_email", "")), str(form.get("contact_phone", "")),
                    str(form.get("source", "")), str(form.get("note", "")),
                    consent_confirmed=str(form.get("consent_confirmed", "")) == "1",
                )
                self.redirect(f"/admin/leads/{lead_id}?notice=nurture-created")
                return
            match = re.fullmatch(r"/admin/leads/([a-f0-9]+)/update", path)
            if match:
                if not self.has_module("sales"):
                    role_label = "管理员" if self.user_role == "admin" else "员工"
                    self.send_html(self.console_page("无权操作", permission_notice("更新待培育线索", role_label, "模块权限「销售工作台」", "更新线索需要销售工作台模块权限。"), "sales"), 403)
                    return
                self.db.update_lead_by_id(
                    self.tenant_id, match.group(1), str(form.get("stage", "")), str(form.get("next_follow_up", "")),
                    str(form.get("note", "")), self.actor, close_reason=str(form.get("close_reason", "")),
                )
                self.redirect(f"/admin/leads/{match.group(1)}?notice=updated#lead-update")
                return
            match = re.fullmatch(r"/admin/leads/([a-f0-9]+)/assign", path)
            if match:
                if self.user_role != "admin":
                    self.send_html(self.console_page("无权操作", permission_notice("分配待培育线索", "员工", "管理员", "分配或改派线索只对管理员开放。"), "sales"), 403)
                    return
                self.db.assign_nurture_lead(self.tenant_id, match.group(1), str(form.get("owner_email", "")), self.actor)
                self.redirect(f"/admin/leads/{match.group(1)}?notice=assigned")
                return
            match = re.fullmatch(r"/admin/leads/([a-f0-9]+)/milestones/([a-z_]+)", path)
            if match:
                if not self.has_module("sales"):
                    role_label = "管理员" if self.user_role == "admin" else "员工"
                    self.send_html(self.console_page("无权操作", permission_notice("更新跟进节点", role_label, "模块权限「销售工作台」", "更新跟进节点需要销售工作台模块权限。"), "sales"), 403)
                    return
                try:
                    activated = self.db.toggle_lead_milestone_for_lead(self.tenant_id, match.group(1), match.group(2), self.actor)
                    notice = "milestone_on" if activated else "milestone_off"
                except ValueError:
                    notice = "milestone_invalid"
                self.redirect(f"/admin/leads/{match.group(1)}?notice={notice}#milestones")
                return
            match = re.fullmatch(r"/admin/leads/([a-f0-9]+)/merge", path)
            if match:
                if self.user_role != "admin":
                    self.send_html(self.console_page("无权操作", permission_notice("合并重复线索", "员工", "管理员", "合并线索会改变展示与归属，只对管理员开放。"), "sales"), 403)
                    return
                primary_id = match.group(1)
                self.db.merge_leads(self.tenant_id, primary_id, str(form.get("secondary_lead_id", "")), self.actor)
                primary = self.db.get_lead(self.tenant_id, primary_id)
                if primary and primary["kind"] != "nurture" and primary["rfq_id"]:
                    self.redirect(f"/admin/sales/{primary['rfq_id']}?notice=merged")
                else:
                    self.redirect(f"/admin/leads/{primary_id}?notice=merged")
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
            match = re.fullmatch(r"/admin/users/([a-f0-9]+)/modules", path)
            if match:
                # V1-01：管理员对员工账号的模块授权（"审批并授权"的唯一开通路径；保存即生效）。
                wanted = [key for key, _label in GRANTABLE_MODULES if str(form.get(f"module_{key}", "")).strip()]
                self.db.set_user_modules(self.tenant_id, match.group(1), wanted, self.actor)
                self.redirect("/admin/users?notice=modules")
                return
            if path == "/admin/creative/create":
                # V1-03：创建展示页——必须先选择本企业的产品；同品类已有展示页则直接打开（每类一份）。
                product = self.db.get_product(self.tenant_id, str(form.get("product_id", "")))
                if not product:
                    raise ValueError("请选择本企业产品库中的产品")
                category = self.db.get_approved_category(self.tenant_id, product["id"])
                if not category:
                    raise ValueError(f"「{product['model']}」还没有已核准的品类；请先在「产品管理」里核准品类")
                existing = self.db.get_template_for_category(self.tenant_id, category)
                if existing:
                    self.redirect(f"/admin/creative/{existing['id']}?notice=existing")
                    return
                blank = str(form.get("start", "standard")).strip() == "blank"
                try:
                    template_id = self.db.create_template(self.tenant_id, category, self.actor, blank=blank)
                except ValueError:
                    existing = self.db.get_template_for_category(self.tenant_id, category)
                    if not existing:
                        raise
                    self.redirect(f"/admin/creative/{existing['id']}?notice=existing")
                    return
                self.redirect(f"/admin/creative/{template_id}?notice=created")
                return
            match = re.fullmatch(r"/admin/creative/([a-f0-9]+)/params/(add|toggle|move|update|delete)", path)
            if match:
                self.admin_creative_params_action(match.group(1), match.group(2), form)
                return
            match = re.fullmatch(r"/admin/creative/([a-f0-9]+)/blocks/(toggle|move|update|advanced|group)", path)
            if match:
                self.admin_creative_blocks_action(match.group(1), match.group(2), form)
                return
            match = re.fullmatch(r"/admin/creative/([a-f0-9]+)/publish/(submit|confirm|withdraw)", path)
            if match:
                self.admin_creative_publish_action(match.group(1), match.group(2))
                return
            match = re.fullmatch(r"/admin/creative/([a-f0-9]+)/restore_defaults", path)
            if match:
                self.admin_creative_restore_defaults(match.group(1))
                return
            match = re.fullmatch(r"/admin/channels/(whatsapp|langbot)", path)
            if match:
                channel = match.group(1)
                if channel == "langbot":
                    existing = self.db.get_channel_account(self.tenant_id, channel)
                    try:
                        existing_config = json.loads(existing["config_json"]) if existing else {}
                    except (json.JSONDecodeError, TypeError):
                        existing_config = {}
                    config = {
                        "bot_uuid": str(form.get("bot_uuid", "")).strip()[:64],
                        # LangBot 不签名回调：URL 令牌即收信凭据；首次保存时生成、此后保持稳定。
                        "webhook_token": str(existing_config.get("webhook_token") or secrets.token_hex(16)),
                    }
                else:
                    config = {"display_name": str(form.get("display_name", "")).strip()[:64]}
                self.db.save_channel_account(self.tenant_id, channel, config, self.actor)
                self.redirect("/admin/channels?notice=saved")
                return
            match = re.fullmatch(r"/admin/channels/(whatsapp|langbot|whatsapp_personal)/toggle", path)
            if match:
                channel = match.group(1)
                account = self.db.get_channel_account(self.tenant_id, channel)
                enabled = not (account and account["status"] == "enabled")
                self.db.set_channel_enabled(self.tenant_id, channel, enabled, self.actor)
                self.redirect(f"/admin/channels?notice={'enabled' if enabled else 'disabled'}")
                return
            if path == "/admin/channels/sync-dify-kb":
                if self.user_role != "admin":
                    self.send_html(self.console_page("无权操作", permission_notice("同步知识库", "员工", "管理员", "知识库同步会改动对外知识库内容，只对管理员开放。"), "channels"), 403)
                    return
                stats = sync_kb_to_dify(self.db, self.tenant_id, actor=self.actor)
                if not stats.get("configured"):
                    self.redirect("/admin/channels?notice=kb_not_configured")
                else:
                    self.redirect(
                        "/admin/channels?notice=kb_synced"
                        f"&created={stats['created']}&updated={stats['updated']}&deleted={stats['deleted']}&skipped={stats['skipped']}&failed={stats['failed']}"
                    )
                return
            match = re.fullmatch(r"/admin/inbox/([a-z0-9_-]{1,40})/([a-f0-9]+)/(reply|ai-toggle)", path)
            if match:
                if match.group(3) == "reply":
                    self.admin_inbox_reply(match.group(1), match.group(2), form)
                else:
                    self.admin_inbox_ai_toggle(match.group(1), match.group(2))
                return
            match = re.fullmatch(r"/admin/inbox/([a-z0-9_-]{1,40})/([a-f0-9]+)/rfq-link", path)
            if match:
                self.admin_inbox_rfq_link(match.group(1), match.group(2))
                return
            match = re.fullmatch(r"/admin/inbox/([a-z0-9_-]{1,40})/([a-f0-9]+)/messages/([a-f0-9]+)/retry", path)
            if match:
                self.admin_inbox_message_retry(match.group(1), match.group(2), match.group(3))
                return
            if path == "/admin/settings/stages":
                self.admin_settings_save_stages(form)
                return
            if path == "/admin/settings/rfq-fields":
                self.admin_settings_save_rfq_fields(form)
                return
            if path == "/admin/settings/rfq-fields-category":
                self.admin_settings_save_rfq_fields_category(form)
                return
            if path == "/admin/settings/notify":
                self.admin_settings_save_notify(form)
                return
            if path == "/admin/material-tasks":
                self.admin_material_task_create(form)
                return
            match = re.fullmatch(r"/admin/material-tasks/([a-f0-9]+)/complete", path)
            if match:
                self.admin_material_task_complete(match.group(1))
                return
            match = re.fullmatch(r"/admin/products/([a-f0-9]+)/sources", path)
            if match:
                product_id = match.group(1)
                scope = str(form.get("scope", "unknown") or "unknown")
                source_location = str(form.get("source_location", ""))
                file_type = str(form.get("file_type", "other"))
                all_files = form.get("__files__")
                batch_files = [
                    entry
                    for entry in (all_files if isinstance(all_files, list) else [])
                    if entry[0] == "source_file" and entry[2]
                ]
                if len(batch_files) > MAX_BATCH_SOURCE_FILES:
                    raise ValueError(f"一次最多上传 {MAX_BATCH_SOURCE_FILES} 个文件，请分批上传")
                if batch_files:
                    # 批量（或单文件）上传：每个文件独立登记为来源并分别按内容提取。
                    for _field_name, file_name, file_content in batch_files:
                        self.db.add_source(
                            self.tenant_id, product_id, file_name or "未命名资料",
                            file_type, source_location, file_content, self.actor, scope=scope,
                        )
                    self.redirect(f"/admin/products/{product_id}?notice=source&count={len(batch_files)}")
                    return
                raw_file = form.get("source_file")
                content = raw_file if isinstance(raw_file, bytes) else None
                filename = str(form.get("source_file__filename", "")) or str(form.get("filename", ""))
                self.db.add_source(
                    self.tenant_id, product_id, filename or "未命名资料",
                    file_type, source_location, content, self.actor, scope=scope,
                )
                self.redirect(f"/admin/products/{product_id}?notice=source")
                return
            match = re.fullmatch(r"/admin/products/([a-f0-9]+)/sources/([a-f0-9]+)/(allow-public|revoke-public|scope|extract|usage)", path)
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
                elif action == "usage":
                    usage_result = self.db.set_source_usage(self.tenant_id, product_id, source_id, str(form.get("usage", "")), self.actor)
                    if usage_result.get("public_permission_revoked"):
                        action = "usage-revoked"
                    elif not usage_result.get("changed"):
                        action = "usage-unchanged"
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
            match = re.fullmatch(r"/admin/products/([a-f0-9]+)/category/(candidate|approve|clear)", path)
            if match:
                product_id, action = match.groups()
                if action == "candidate":
                    self.db.set_category_candidate(
                        self.tenant_id, product_id, str(form.get("category", "")), str(form.get("source", "manual")), self.actor
                    )
                elif action == "approve":
                    self.db.approve_category(self.tenant_id, product_id, self.actor)
                else:
                    self.db.clear_category_candidate(self.tenant_id, product_id, self.actor)
                self.redirect(f"/admin/products/{product_id}?notice=category-{action}")
                return
            match = re.fullmatch(r"/admin/products/([a-f0-9]+)/(approve|finalize-review|publish|withdraw)", path)
            if match:
                product_id, action = match.groups()
                if action in ("approve", "finalize-review"):
                    self.db.finalize_product_review(self.tenant_id, product_id, self.actor)
                elif action == "publish":
                    self.db.publish_product(self.tenant_id, product_id, self.actor)
                    # 第二波：发布新版本 → 知识库自动同步（未配置时静默跳过；失败不阻断发布，可在渠道页手动重试）。
                    try_sync_kb_quietly(self.db, self.tenant_id, f"{self.actor}@publish")
                else:
                    self.db.withdraw_product(self.tenant_id, product_id, self.actor)
                    try_sync_kb_quietly(self.db, self.tenant_id, f"{self.actor}@withdraw")
                notice = "approve" if action == "finalize-review" else action
                self.redirect(f"/admin/products/{product_id}?notice={notice}")
                return
        except (ValueError, sqlite3.Error) as exc:
            if path.startswith("/inquiry/"):
                return_path = path.rsplit("/", 1)[0]
                self.send_html(layout("操作未完成", f"<div class='error'>{esc(exc)}</div><p><a href='{esc(return_path)}'>返回并修改</a></p>"), 400)
                return
            product_match = re.match(r"(/admin/products/[a-f0-9]+)(/|$)", path)
            if product_match:
                return_path = product_match.group(1)
            elif path.startswith("/admin/creative"):
                return_path = "/admin/creative"
            else:
                return_path = "/admin/products"
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
        notice_html = ""
        if self.demo_tools and notice == "demo":
            notice_html = "<div class='notice'>示例资料已登记为候选值，请逐个核对后核准，再发布到买方产品页。</div>"
        if self.demo_tools and notice == "lowara":
            notice_html = f"<div class='notice'>Lowara 资料已加载：新增 {esc(query.get('created', ['0'])[0])} 个内部演示型号。来源文件只登记元数据，外部再发布授权仍待确认。</div>"
        demo_actions = (
            "<div><form method='post' action='/admin/products/seed' class='inline'><button class='secondary'>导入两款示例水泵</button></form>"
            " <form method='post' action='/admin/products/seed-lowara' class='inline'><button class='secondary'>加载 Lowara 资料</button></form></div>"
            if self.demo_tools and self.user_role == "admin"
            else ""
        )
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
<div class='toolbar'><div><h1>{'产品资料与发布' if lang == 'zh' else 'Products & publishing'}</h1><div class='muted'>企业：{esc(self.tenant_display_name())} · 当前操作员：管理员</div></div>{demo_actions}</div>
{notice_html}
<section class='card'><div class='panel-title'><h2>产品列表</h2><span class='muted'>选择产品后完成核对与发布</span></div><div class='simple-list'>{items_html}</div>
<details class='detail-toggle'><summary>新建产品型号</summary><div><form method='post' action='/admin/products'><label>型号 *</label><input name='model' placeholder='例如 CB-80A' required><label>产品名称</label><input name='name' placeholder='买方页面显示名称'><label>主要用途</label><textarea name='use_case' placeholder='例如清水循环、化工介质输送'></textarea><p><button>创建草稿</button></p></form></div></details>
<details class='detail-toggle'><summary>查看流程说明</summary><div class='muted-block'>产品资料登记 → 来源文件 → 字段候选值 → 人工核准 → 版本发布 → 固定买方页面。未核准字段不会进入公开页；重新编辑已发布字段会回到待核准状态。对外发布前须确认资料归属与再发布授权；未确认授权的内容不会进入公开页。</div></details></section>
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
            "usage": "文件可见性已更新。",
            "usage-revoked": "文件已标记禁止使用；原公开许可已自动撤销。新回答不再引用该文件，下次发布也不会包含其字段。",
            "usage-unchanged": "文件可见性未变化。",
            "category-candidate": "品类候选已登记；候选不等于已核准，需由授权成员核准后才能生效。",
            "category-approve": "品类已核准。",
            "category-clear": "品类候选已清除。",
            "extract": "文件已按内容重新解析，结果在来源区显示。",
            "field": "字段已保存为候选值，需逐项核准或驳回后才能完成核对。",
            "field-approve": "字段已核准。",
            "field-reject": "字段已驳回；未核准内容不会进入匹配、问答或公开页。",
            "field-reopen": "字段已退回待核；请重新逐项处理。",
            "approve": "核对完成：全部候选字段已逐项处理，可发布新版本。",
            "publish": "产品版本已发布。",
            "withdraw": "公开版本已撤回。",
        }.get(notice, "")
        if notice == "source" and query.get("count"):
            notice_text = f"已保存 {esc(query.get('count', [''])[0])} 份来源文件。上传的文件已按内容尝试抽取；请核对下方提取结果后逐项处理。"
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
            source_usage = source["usage"] or "internal"
            if source_usage == "restricted":
                extract_button = ""
                perm_html = (
                    "<span class='field-state'>已标记禁止使用：不进入买方页、不参与新回答引用"
                    + ("；当前公开版本中的该文件已停止提供" if source["id"] in published_source_ids else "")
                    + "</span>"
                )
            elif source["storage_path"]:
                extract_button = f"<form method='post' action='/admin/products/{product_id}/sources/{source['id']}/extract' class='inline'><button class='secondary'>按内容重新解析</button></form>"
                if source["id"] in public_source_ids:
                    if source["id"] in servable_source_ids:
                        inclusion_html = " <span class='field-state'>已纳入当前公开版本</span>"
                    elif source["id"] in published_source_ids:
                        inclusion_html = " <span class='field-state'>当前版本快照已失效（文件归属或可见性修正过）；重新公开需发布新版本</span>"
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
            ref_rows = self.db.conn.execute(
                "SELECT COUNT(*) AS total, SUM(CASE WHEN state='approved' THEN 1 ELSE 0 END) AS approved FROM product_fields WHERE tenant_id=? AND product_id=? AND source_document_id=?",
                (self.tenant_id, product_id, source["id"]),
            ).fetchone()
            ref_total = int(ref_rows["total"] or 0)
            ref_approved = int(ref_rows["approved"] or 0)
            if source_usage == "restricted":
                usage_badge = "<span class='badge danger'>禁止使用</span>"
            elif source["id"] in public_source_ids:
                usage_badge = "<span class='badge approved'>可公开</span>"
            else:
                usage_badge = "<span class='badge'>仅内部</span>"
            if source_usage == "restricted":
                usage_form = f"<form method='post' action='/admin/products/{product_id}/sources/{source['id']}/usage' class='inline'><input type='hidden' name='usage' value='internal'><button class='secondary'>恢复可用</button></form>"
                restrict_hint = (
                    f"<p class='field-state' style='margin:6px 0 0'>该文件当前为 {ref_total} 个字段的来源（其中已核准 {ref_approved} 个）；标记禁止使用后，新回答不再引用，下次发布不会包含这些字段。</p>"
                    if ref_total else ""
                )
            else:
                usage_form = f"<form method='post' action='/admin/products/{product_id}/sources/{source['id']}/usage' class='inline'><input type='hidden' name='usage' value='restricted'><button class='danger'>标记禁止使用</button></form>"
                restrict_hint = ""
            source_blocks.append(
                "<div class='source' style='display:block'><div style='display:flex;justify-content:space-between;gap:12px;flex-wrap:wrap'>"
                f"<span><b>{esc(source['filename'])}</b> {usage_badge}<br><span class='muted'>{esc(source['file_type'])} · {esc(source['source_location'] or '位置待补充')}</span></span>"
                f"<span class='field-state'>{esc(processing_labels.get(source['processing_status'], source['processing_status']))} · 归属：{esc(SOURCE_SCOPES.get(source['scope'], source['scope']))}"
                + (f" · 归属修订 r{source['scope_revision']}" if source["scope_revision"] else "")
                + (f" · 引用字段 {ref_total}" if ref_total else "")
                + (f" · 校验码 {esc((source['sha256'] or '')[:10])}…" if source["sha256"] else "")
                + "</span></div>"
                + (f"<p class='field-state' style='margin:6px 0 0'>{esc(source['extraction_note'])}</p>" if source["extraction_note"] else "")
                + extraction_html
                + restrict_hint
                + f"<div style='margin-top:8px;display:flex;gap:8px;flex-wrap:wrap;align-items:center'>{scope_form}{usage_form}{extract_button}{perm_html}</div>"
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
        version_rows = self.db.list_product_versions(self.tenant_id, product_id)
        version_blocks = []
        for index, version_row in enumerate(version_rows):
            snapshot = json.loads(version_row["snapshot_json"])
            version_status_label = "当前公开" if version_row["status"] == "published" else "已下线（被替换或撤回，旧询价仍引用该版）"
            header = f"v{version_row['version']} · {version_status_label} · {esc(version_row['created_by'] or '')} · {esc(version_row['created_at'] or '')}"
            if index + 1 < len(version_rows):
                previous_snapshot = json.loads(version_rows[index + 1]["snapshot_json"])
                diff = diff_product_snapshots(previous_snapshot, snapshot)
                summary_bits = []
                if diff["changes"]:
                    summary_bits.append(f"字段变化 {len(diff['changes'])} 项")
                if diff["files_added"] or diff["files_removed"]:
                    summary_bits.append(f"公开文件 +{len(diff['files_added'])} / -{len(diff['files_removed'])}")
                if diff["missing_added"] or diff["missing_resolved"]:
                    summary_bits.append(f"缺项 +{len(diff['missing_added'])} / -{len(diff['missing_resolved'])}")
                if diff["category_changed"]:
                    summary_bits.append("品类变化")
                summary = "；".join(summary_bits) or "与上一版无可见变化"
                detail_items = "".join(
                    f"<li>{esc(change['label'])}：{esc(change['old'] or '（无）')} → {esc(change['new'] or '（移除）')}</li>"
                    for change in diff["changes"]
                )
                if diff["category_changed"]:
                    detail_items += f"<li>品类：{esc(diff['category_old'] or '（无）')} → {esc(diff['category_new'] or '（无）')}</li>"
                for filename in diff["files_added"]:
                    detail_items += f"<li>公开文件新增：{esc(filename)}</li>"
                for filename in diff["files_removed"]:
                    detail_items += f"<li>公开文件移除：{esc(filename)}</li>"
                for label in diff["missing_added"]:
                    detail_items += f"<li>缺项新增：{esc(FIELD_LABELS.get(label, label))}</li>"
                for label in diff["missing_resolved"]:
                    detail_items += f"<li>缺项补齐：{esc(FIELD_LABELS.get(label, label))}</li>"
                detail = f"<details><summary>查看逐项变化</summary><ul style='margin:6px 0 0 18px;padding:0'>{detail_items}</ul></details>" if detail_items else ""
                version_blocks.append(
                    f"<div class='source' style='display:block'><b>{header}</b><br><span class='field-state'>与上一版对比：{summary}</span>{detail}</div>"
                )
            else:
                version_blocks.append(
                    f"<div class='source' style='display:block'><b>{header}</b><br>"
                    f"<span class='field-state'>首个公开版本 · 字段 {len(snapshot.get('fields', []))} 项 · 公开文件 {len(snapshot.get('public_sources', []))} 份</span></div>"
                )
        version_history_html = "".join(version_blocks) or "<p class='muted'>尚无发布记录。</p>"
        version_section = (
            "<details class='detail-toggle' style='margin-top:18px'><summary>版本历史与对比（每次发布相对上一版的字段、文件、缺项与品类变化）</summary>"
            f"<div class='muted-block'>{version_history_html}</div></details>"
        )
        category = (product["category"] or "").strip()
        category_candidate = (product["category_candidate"] or "").strip()
        category_source_label = CATEGORY_SOURCE_LABELS.get(product["category_candidate_source"], "")
        if category:
            category_status_span = (
                f"已核准：{esc(category)}（{esc(product['category_approved_by'] or '')} · {esc(product['category_approved_at'] or '')}）"
            )
        else:
            category_status_span = "尚未核准品类"
        candidate_html = (
            f"<p class='field-state'>当前候选：<b>{esc(category_candidate)}</b>（{esc(category_source_label)} · {esc(product['category_candidate_at'] or '')}）——候选不等于已核准。</p>"
            if category_candidate else "<p class='field-state'>暂无待核准候选。</p>"
        )
        category_source_options = "".join(
            f"<option value='{key}'>{label}</option>" for key, label in CATEGORY_SOURCE_OPTIONS
        )
        category_actions = (
            f"<form method='post' action='/admin/products/{product_id}/category/candidate' class='inline'>"
            f"<input name='category' placeholder='例如 离心泵' style='width:160px'><select name='source'>{category_source_options}</select> "
            f"<button class='secondary'>登记候选</button></form>"
        )
        if category_candidate:
            category_actions += (
                f"<form method='post' action='/admin/products/{product_id}/category/approve' class='inline'><button>核准为正式品类</button></form>"
                f"<form method='post' action='/admin/products/{product_id}/category/clear' class='inline'><button class='danger'>清除候选</button></form>"
            )
        category_section = (
            f"<section class='card' style='margin-top:18px' id='category'><div class='panel-title'><h2>品类（候选与核准）</h2>"
            f"<span class='muted'>{category_status_span}</span></div>{candidate_html}"
            "<p class='muted'>品类可由资料、模型或人工提出候选，但必须由授权成员核准后才生效；未核准的候选不进入买方页、不用于按品类配置与工作流。核准动作全程审计。</p>"
            f"<div style='display:flex;gap:8px;flex-wrap:wrap;align-items:center'>{category_actions}</div></section>"
        )
        body = f"""
<div class='toolbar'><div><a href='/admin/products'>← 返回产品列表</a><h1>{esc(product['model'])}</h1><div class='muted'>{esc(product['name'])} · {status_badge(product['status'])}</div></div><div>{view_page_link}</div></div>
{notice_banner}
{focus_html}
{summary_strip}
{missing_html}
{category_section}
<section class='card' id='fields'><div class='panel-title'><h2>字段核对（逐项）</h2><span class='muted'>待处理 {pending_count} 项</span></div>{pending_html}<p class='muted'>候选字段需逐项核准或驳回；驳回保留原值并记录原因。已核准关键字段被修改后会回到待核对，需重新核准并发布新版本；缺项不会被推测值填充。</p><table><thead><tr><th>字段</th><th>值</th><th>来源与状态</th><th>操作</th></tr></thead><tbody>{field_table}</tbody></table><details class='detail-toggle'><summary>手动补充 / 修改字段值</summary><div><form method='post' action='/admin/products/{product_id}/fields'><div class='form-grid'><div><label>字段</label><select name='field_key'>{field_options}</select></div><div><label>值 *</label><input name='value' required placeholder='例如 20-80'></div><div><label>单位</label><input name='unit' placeholder='m³/h、m、°C'></div><div><label>来源</label><select name='source_document_id'>{source_options}</select></div><div class='full'><label>原文位置 *</label><input name='source_location' required placeholder='第 1 页：规格参数表'></div></div><p><button>保存为候选值</button></p></form></div></details></section>
<section class='card' style='margin-top:18px'><div class='panel-title'><h2>发布与版本</h2><span class='muted'>{version_line}</span></div><div class='actions' style='display:flex;gap:8px;flex-wrap:wrap'>{action_buttons}</div><p class='muted'>发布将冻结当前已核准事实与已开放公开文件清单；撤回后买方页不可访问，已提交的询价保留原版本快照。</p></section>
{version_section}
<section class='card' style='margin-top:18px' id='sources'><h2>登记来源资料</h2>{source_rows}<p class='muted'>文件默认仅内部使用、不对外下载；可见性分「仅内部 / 禁止使用」，禁止使用的文件不进入买方页、不参与新回答引用。上传后的文本/CSV 会按内容识别已知字段标签；PDF、图片、CAD 等不支持的格式会明确失败，请人工录入。开放公开下载前必须确认文件归属。支持多选批量上传：同批共用所选归属与原文位置，每个文件独立登记并分别提取（一次最多 20 个）。</p><hr><form method='post' action='/admin/products/{product_id}/sources' enctype='multipart/form-data'><label>文件（可选，可多选批量上传）</label><input type='file' name='source_file' multiple><label>或资料文件名</label><input name='filename' placeholder='产品手册.pdf'><div class='form-grid'><div><label>类型</label><select name='file_type'><option>PDF</option><option>XLSX</option><option>MARKDOWN</option><option>TXT</option><option>IMAGE</option><option>DXF</option><option>STEP</option><option>OTHER</option></select></div><div><label>文件归属 *</label><select name='scope' required><option value=''>请选择文件归属</option><option value='exact_model'>本型号专属文件</option><option value='series'>系列级资料（非本型号专属）</option><option value='unknown'>归属待核对（不上公开页）</option></select></div><div class='full'><label>原文位置（可选，批量时同批共用）</label><input name='source_location' placeholder='第 1 页：规格参数表；留空显示「位置待补充」'></div></div><p><button class='secondary'>保存来源</button></p></form></section>
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
            "extract_unavailable": "<div class='error'>消息已保存，但需求提取暂不可用（服务故障）；请再次提交同一内容重试。</div>",
            "answer_unavailable": "<div class='error'>问题已保存，但回答服务暂不可用；请稍后重新提交同一问题重试，或请求工程师核实。</div>",
            "rfq_stale": "<div class='error'>这张询价已在别处更新，请刷新页面后再修改（避免覆盖较新的版本）。</div>",
            "rfq_required": "<div class='error'>操作未完成：有必填信息未填写（请检查带 * 的必填字段），补全后重试。</div>",
            "rfq_conflict": "<div class='error'>提交未完成：同一提交标识对应了不同内容，请刷新页面后重新提交。</div>",
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
            citation_html = "".join(
                "<div class='field-state'>引用：" + " · ".join(
                    part for part in (
                        esc(citation.get("filename") or ""),
                        esc(citation.get("location") or ""),
                        f"产品版本 {esc(citation.get('product_version'))}" if citation.get("product_version") else "",
                    ) if part
                ) + "</div>"
                for citation in citations
            )
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
        # V1-01 步骤②：RFQ 字段三态（必填/选填/隐藏）——三个买方入口与服务端校验同口径。
        rfq_field_config = self.db.get_rfq_field_config(self.tenant_id, self.db.conversation_category_hint(self.tenant_id, conversation_id))

        def rfq_visible(field_key: str) -> bool:
            return rfq_field_config[field_key] != "hidden"

        def rfq_star(field_key: str) -> str:
            return " *" if rfq_field_config[field_key] == "required" else ""

        def rfq_required_attr(field_key: str) -> str:
            return " required" if rfq_field_config[field_key] == "required" else ""

        rfq_required_labels = [RFQ_FIELD_LABELS[key] for key in RFQ_FIELD_ORDER if rfq_field_config[key] == "required"]
        rfq_intro = "您可以随时提交询价，无须先完成选型。"
        if rfq_required_labels:
            rfq_intro += f"请填写：{'、'.join(rfq_required_labels)}。"
        rfq_intro += "已确认的工况会随询价保存，缺少的技术或商务信息会标为待补充。"

        create_form_fields = ""
        create_field_specs = (
            ("email", "Email / 邮箱", "contact_email", " type='email'", "you@example.com", ""),
            ("phone", "Phone / 电话", "contact_phone", "", "+49 ...", ""),
            ("quantity", "Quantity / 数量", "quantity", "", "e.g. 2", esc(prefill["quantity"])),
            ("destination", "Destination / 目的地", "region", "", "e.g. Germany", esc(prefill["region"])),
        )
        for field_key, field_label, field_name, field_type, field_placeholder, field_value in create_field_specs:
            if not rfq_visible(field_key):
                continue
            create_form_fields += (
                f"<div><label>{field_label}{rfq_star(field_key)}</label>"
                f"<input name='{field_name}'{field_type}{rfq_required_attr(field_key)} value='{field_value}' placeholder='{field_placeholder}'></div>"
            )
        create_form_fields += f"<div><label>Expected delivery / 期望交期</label><input name='delivery' value='{esc(prefill['delivery'])}' placeholder='e.g. 8 weeks'></div>"
        if rfq_visible("notes"):
            create_form_fields += f"<div class='full'><label>Notes / 补充说明{rfq_star('notes')}</label><textarea name='notes'{rfq_required_attr('notes')} placeholder='Additional operating conditions or questions'></textarea></div>"
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
            revise_field_specs = (
                ("email", "Email / 邮箱", "contact_email", " type='email'", content["contact_email"] or ""),
                ("phone", "Phone / 电话", "contact_phone", "", content["contact_phone"] or ""),
                ("quantity", "Quantity / 数量", "quantity", "", content["quantity"] or ""),
                ("destination", "Destination / 目的地", "region", "", content["destination"] or ""),
            )
            revise_fields = f"<div><label>Contact name / 联系人</label><input name='contact_name' value='{esc(content['contact_name'] or '')}'></div>"
            revise_fields += f"<div><label>Company / 公司</label><input name='company' value='{esc(content['company'] or '')}'></div>"
            for field_key, field_label, field_name, field_type, field_value in revise_field_specs:
                if not rfq_visible(field_key):
                    continue
                revise_fields += (
                    f"<div><label>{field_label}{rfq_star(field_key)}</label>"
                    f"<input name='{field_name}'{field_type}{rfq_required_attr(field_key)} value='{esc(field_value)}'></div>"
                )
            revise_fields += f"<div><label>Expected delivery / 期望交期</label><input name='delivery' value='{esc(content['requested_delivery'] or '')}'></div>"
            if rfq_visible("notes"):
                revise_fields += f"<div class='full'><label>Notes / 补充说明{rfq_star('notes')}</label><textarea name='notes'{rfq_required_attr('notes')}>{esc(content['notes'] or '')}</textarea></div>"
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
                f"<div class='form-grid'>{revise_fields}</div><p><button>Save changes / 保存修改</button></p></form></details></div>"
            )
        rfq_cards_html = "".join(rfq_card_parts) or "<p class='muted'>本会话还没有询价记录。</p>"

        body = f"""
<div class='toolbar'><div><h1>Buyer inquiry</h1><div class='muted'>Conversation {esc(conversation_id)} · 原始消息和每次需求修订都会保留</div></div><div><a class='button secondary' href='/inquiry?lang={'zh' if lang == 'en' else 'en'}'>New inquiry / 新询盘</a></div></div>
{notice_html}
<div class='grid'><section class='card'><h2>Describe your application</h2><p class='muted'>Tell us the pump use, flow, head and medium in your own words. Structured values are candidates until you confirm them.</p><form method='post' action='/inquiry/{conversation_id}/message'><label>Your message / 需求原话 *</label><textarea name='message' required placeholder='Example: I need a water pump for cooling water, flow 50 m³/h and head 30 m.'></textarea><p><button>Save message and extract fields</button></p></form><hr><h3>Conversation</h3>{message_rows}</section>
<section class='card'><h2>Requirement summary</h2>{missing_html}<form method='post' action='/inquiry/{conversation_id}/confirm'><input type='hidden' name='submission_key' value='{esc(confirm_key)}'><div class='form-grid'>{input_fields}</div><p><button>Confirm summary and match</button></p></form><table><thead><tr><th>Field</th><th>Value</th><th>State</th></tr></thead><tbody>{summary_rows}</tbody></table></section></div>
<section class='card' style='margin-top:18px'><h2>Recommendations</h2>{recommendation_html}</section>
<section class='card' style='margin-top:18px'><h2>Ask about the recommended product</h2><p class='muted'>资料内问题显示出处。资料外问题先保存为待核实；只有您点击“请求工程师核实”后才会交给工程师，在此对话查看回复无需另留联系方式。</p><form method='post' action='/inquiry/{conversation_id}/ask'><input type='hidden' name='submission_key' value='{esc(ask_key)}'><label>Product context</label><select name='product_id'>{candidate_options}</select><label>Your question / 技术问题 *</label><textarea name='question' required placeholder='Example: What is the flow range? Or ask about price, stock, certification to test handoff.'></textarea><p><button>Ask and save answer</button></p></form><h3>Answers and handoff</h3>{answer_html}{task_html}</section>
<section class='card' style='margin-top:18px'><h2>Request for quotation (RFQ)</h2><p class='muted'>{rfq_intro}</p><form method='post' action='/inquiry/{conversation_id}/rfq'><input type='hidden' name='submission_key' value='{esc(submission_key)}'><div class='form-grid'><div><label>Product / 产品</label><select name='product_id'>{candidate_options}</select></div><div><label>Contact name / 联系人</label><input name='contact_name' placeholder='Your name'></div><div><label>Company / 公司</label><input name='company' placeholder='Company name'></div>{create_form_fields}</div><p><button>Save RFQ / 提交询价</button></p></form><h3>Your RFQs / 我的询价</h3>{rfq_cards_html}</section>
<div class='footer-note'>产品页面、推荐和后续 chatbot 都只读取已核准并已发布的产品版本。当前规则是透明的水泵演示规则，正式阈值需由企业工程师确认。</div>
"""
        self.send_html(layout("Buyer inquiry", body, lang=lang), 200)

    def admin_dashboard(self, query: dict[str, list[str]]) -> None:
        summary = self.db.get_admin_summary(self.tenant_id)
        role_label = "管理员" if self.user_role == "admin" else "员工"
        demo_notice = ""
        if self.demo_tools and query.get("notice", [""])[0] == "demo":
            demo_notice = f"<div class='notice'>演示数据已加载：新增 RFQ {esc(query.get('rfqs', ['0'])[0])} 条、人工任务 {esc(query.get('tasks', ['0'])[0])} 条；重复加载不会复制已有记录。</div>"
        elif self.demo_tools and query.get("notice", [""])[0] == "lowara":
            demo_notice = f"<div class='notice'>Lowara 资料已加载：新增 {esc(query.get('products', ['0'])[0])} 个内部演示型号；来源文件只登记元数据，外部再发布授权仍待确认。</div>"
        demo_actions = (
            "<div><form method='post' action='/admin/demo/seed' class='inline'><button class='secondary'>加载演示数据</button></form>"
            " <form method='post' action='/admin/lowara/seed' class='inline'><button class='secondary'>加载 Lowara 资料</button></form></div>"
            if self.demo_tools and self.user_role == "admin"
            else ""
        )
        cards = "".join(
            f"<div class='card'><div class='muted'>{label}</div><div class='metric'>{value}</div><div class='field-state'>{detail}</div></div>"
            for label, value, detail in (
                ("产品总数", summary["products_total"], f"已发布 {summary['products_published']} · 待核准 {summary['products_needs_review']}"),
                ("待处理人工任务", summary["tasks_pending"], f"累计任务 {summary['tasks_total']}"),
                ("RFQ 询价", summary["rfqs_total"], f"待补充 {summary['rfqs_needs_info']}"),
                ("进行中线索", summary["leads_active"], f"逾期跟进 {summary['leads_overdue']}"),
            )
        )
        todos: list[tuple[str, str, str, str]] = []
        if self.has_module("products") and summary["products_needs_review"]:
            todos.append(("产品资料核对", f"{summary['products_needs_review']} 个产品有候选字段待逐项处理；未核准内容不会进入公开页与推荐", "/admin/products", "去核对"))
        if self.has_module("tasks") and summary["tasks_pending"]:
            todos.append(("待答技术问题", f"{summary['tasks_pending']} 条买家问题在等待人工答复；答复会回到买家的原会话", "/admin/tasks?status=pending", "去处理"))
        if self.has_module("rfqs") and summary["rfqs_needs_info"]:
            todos.append(("待补充询价", f"{summary['rfqs_needs_info']} 份 RFQ 缺少数量/目的地/交期等商务信息", "/admin/rfqs", "去查看"))
        if self.has_module("sales") and summary["leads_overdue"]:
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
        quick_links: list[tuple[str, str, str]] = []
        if self.has_module("sales"):
            quick_links.append(("/admin/sales", "打开销售工作台", "查看待跟进线索、负责人和完整上下文"))
        if self.has_module("rfqs"):
            quick_links.append(("/admin/rfqs", "查看 RFQ", "检查买方提交的询价和待补充字段"))
        if self.has_module("tasks"):
            quick_links.append(("/admin/tasks?status=pending", "处理人工任务", "回复资料外问题并保留当前会话上下文"))
        if self.has_module("products"):
            quick_links.append(("/admin/products", "维护产品资料", "核准字段、发布版本或撤回公开页"))
        if self.has_module("sites"):
            quick_links.append(("/admin/sites", "网站 API 接入", "登记企业官网来源并获取公开的网站标识"))
        if self.user_role == "admin":
            quick_links.append(("/admin/users", "管理成员账号", "创建账号、审批并授权模块权限、启停账号"))
        quick_html = "".join(
            f"<div class='simple-item'><div><strong>{esc(title)}</strong><small>{esc(description)}</small></div>"
            f"<div class='actions'><a class='button secondary' href='{href}'>进入</a></div></div>"
            for href, title, description in quick_links
        )
        ai_section = ai_status_section(self.db, self.ai, self.tenant_id)
        # V1-01 零权限员工：登录后只看到"等待授权"说明，不展示任何运营数字或内部状态。
        no_module_access = self.user_role != "admin" and not any(self.has_module(key) for key in GRANTABLE_MODULE_KEYS)
        if no_module_access:
            focus_html = (
                "<div class='focus'><div><span class='focus-label'>当前状态</span><strong>账号尚未开通任何模块权限</strong>"
                "<p>请联系企业管理员在「成员管理」中审批并授权需要使用的模块；授权保存后立即生效，无需重新登录。</p></div></div>"
            )
            todo_html = "<div class='muted-block'>开通模块权限后，这里会显示与你相关的待处理事项。</div>"
            quick_html = "<div class='muted-block'>目前没有可用入口。</div>"
            ai_section = ""
            cards_section = ""
        else:
            cards_section = f"<section class='card' style='margin-top:18px'><details class='detail-toggle' style='border-top:0;margin-top:0;padding-top:0'><summary>查看运营数字</summary><div><section class='metric-grid'>{cards}</section></div></details></section>"
        body = f"""
<div class='toolbar'><div><h1>管理概览</h1><div class='muted'>当前企业：{esc(self.tenant_display_name())} · 当前账号：{esc(self.actor)}（{role_label}）</div></div>{demo_actions}</div>
{demo_notice}
{focus_html}
<div class='grid'><section class='card'><div class='panel-title'><h2>待处理事项</h2><span class='muted'>只显示需要你行动的事项（跟随账号的模块授权）</span></div><div class='simple-list'>{todo_html}</div></section>
<section class='card'><div class='panel-title'><h2>常用入口</h2></div><div class='simple-list'>{quick_html}</div></section></div>
{ai_section}
{cards_section}
<div class='footer-note'>概览只读取当前登录账号所属企业的数据；具体修改仍需进入对应模块，并按账号的模块授权执行。</div>
"""
        self.send_html(self.console_page("管理概览", body, "admin"), 200)

    def admin_settings(self, query: dict[str, list[str]]) -> None:
        stages = self.db.get_lead_stages(self.tenant_id)
        field_config = self.db.get_rfq_field_config(self.tenant_id)
        recipients = self.db.get_notify_recipients(self.tenant_id)
        _, stages_updated = self.db.get_tenant_setting(self.tenant_id, SETTING_LEAD_STAGES)
        _, fields_updated = self.db.get_tenant_setting(self.tenant_id, SETTING_RFQ_FIELDS)
        _, notify_updated = self.db.get_tenant_setting(self.tenant_id, SETTING_NOTIFY_RECIPIENTS)
        notice = query.get("notice", [""])[0]
        notice_html = {
            "stages": "<div class='notice'>线索阶段已保存：改名、排序与终态标记已生效（销售工作台与「在跟 / 逾期」数字同步）。</div>",
            "fields": "<div class='notice'>RFQ 字段设置已保存：三个买方入口的表单与服务端校验同步生效。</div>",
            "notify": "<div class='notice'>通知接收人已保存。（渠道未接入、暂不发送，仅登记）</div>",
            "stages_invalid": "<div class='error'>阶段设置未保存：每个阶段都需要名称，且名称不能重复。</div>",
            "fields_invalid": "<div class='error'>字段设置未保存：存在无效的收集方式，请重新选择。</div>",
            "fields_both_hidden": "<div class='error'>字段设置未保存：邮箱与电话不能同时设为「隐藏」（至少保留一种可回复方式）。</div>",
            "notify_invalid": "<div class='error'>接收人未保存：请检查邮箱格式（每行一个或用逗号分隔，最多 20 个）。</div>",
            "rfq-category": "<div class='notice'>按品类询价字段已保存：仅对已核准品类的产品生效。</div>",
            "rfq_category_invalid": "<div class='error'>按品类字段未保存：品类必须已核准，且邮箱与电话不能同时隐藏。</div>",
        }.get(notice, "")
        stage_rows = "".join(
            f"<div class='stage-row'><code class='chip-key'>{esc(stage['key'])}</code>"
            f"<input name='label_{esc(stage['key'])}' value='{esc(stage['label'])}' maxlength='24' aria-label='阶段名称'>"
            f"<label class='check-inline'><input type='checkbox' name='terminal_{esc(stage['key'])}' value='1' {'checked' if stage['terminal'] else ''}>终态</label>"
            f"<button class='secondary mini' name='move' value='{esc(stage['key'])}:up' title='上移'>↑</button>"
            f"<button class='secondary mini' name='move' value='{esc(stage['key'])}:down' title='下移'>↓</button></div>"
            for stage in stages
        )
        state_labels = {"required": "必填", "optional": "选填", "hidden": "隐藏"}
        field_hints = {
            "email": "买家邮箱；与电话不能同时隐藏。",
            "phone": "买家电话；与邮箱不能同时隐藏。",
            "quantity": "采购数量。",
            "destination": "货物目的地。",
            "notes": "补充说明。",
        }

        def field_select(field_key: str) -> str:
            options = "".join(
                f"<option value='{esc(state)}'{' selected' if field_config[field_key] == state else ''}>{esc(state_labels[state])}</option>"
                for state in RFQ_FIELD_STATES
            )
            return f"<select name='state_{esc(field_key)}'>{options}</select>"

        field_rows = "".join(
            f"<tr><td><b>{esc(RFQ_FIELD_LABELS[field_key])}</b><br><span class='field-state'>{esc(field_hints[field_key])}</span></td><td>{field_select(field_key)}</td></tr>"
            for field_key in RFQ_FIELD_ORDER
        )
        approved_categories = self.db.approved_categories(self.tenant_id)
        by_category_raw, _ = self.db.get_tenant_setting(self.tenant_id, SETTING_RFQ_FIELDS_BY_CATEGORY)
        by_category = by_category_raw if isinstance(by_category_raw, dict) else {}
        category_blocks = []
        for category in approved_categories:
            overrides = by_category.get(category) if isinstance(by_category.get(category), dict) else {}
            summary_bits = [
                f"{RFQ_FIELD_LABELS[field_key]}={state_labels[overrides[field_key]]}"
                for field_key in RFQ_FIELD_ORDER
                if overrides.get(field_key) in RFQ_FIELD_STATES
            ]
            summary = "；".join(summary_bits) or "未设置覆盖（全部继承全局）"

            def category_select(field_key: str, overrides: dict = overrides) -> str:
                parts = [
                    f"<option value=''{'' if overrides.get(field_key) else ' selected'}>继承全局（{state_labels[field_config[field_key]]}）</option>"
                ]
                for state in RFQ_FIELD_STATES:
                    parts.append(f"<option value='{state}'{' selected' if overrides.get(field_key) == state else ''}>{state_labels[state]}</option>")
                return f"<select name='field_{esc(field_key)}'>" + "".join(parts) + "</select>"

            rows = "".join(
                f"<div><label>{esc(RFQ_FIELD_LABELS[field_key])}</label>{category_select(field_key)}</div>"
                for field_key in RFQ_FIELD_ORDER
            )
            category_blocks.append(
                f"<details class='detail-toggle'><summary>{esc(category)} · {esc(summary)}</summary><div>"
                f"<form method='post' action='/admin/settings/rfq-fields-category'><input type='hidden' name='category' value='{esc(category)}'>"
                f"<div class='form-grid'>{rows}</div><p><button>保存该品类覆盖</button></p></form>"
                f"<form method='post' action='/admin/settings/rfq-fields-category'><input type='hidden' name='category' value='{esc(category)}'>"
                f"<input type='hidden' name='clear' value='1'><button class='secondary'>清除该品类覆盖</button></form>"
                f"</div></details>"
            )
        category_section = (
            "<section class='card'><div class='panel-title'><h2>按品类询价字段（可选）</h2><span class='field-state'>只对已核准品类生效</span></div>"
            "<p class='muted'>为不同品类覆盖 RFQ 字段的收集方式；未覆盖的字段继承上方全局设置。品类需先在产品页核准；未核准的品类不会应用任何覆盖，产品页与会话表单按解析到的已核准品类取值。</p>"
            + ("".join(category_blocks) if approved_categories else "<div class='muted-block'>还没有已核准的品类。先到「产品管理」核准产品品类后，可在这里按品类细分询价字段。</div>")
            + "</section>"
        )
        recipients_value = esc("\n".join(recipients))
        body = f"""
<div class='toolbar'><div><h1>企业设置</h1><div class='muted'>影响全企业买家表单与跟进流程的配置；保存后立即生效。</div></div></div>
{notice_html}
<section class='card'><div class='panel-title'><h2>线索阶段</h2><span class='field-state'>上次修改：{esc(stages_updated or '未修改（当前为默认）')}</span></div>
<p class='muted'>首版支持改名、排序与终态标记（增删阶段暂未开放，已登记待办）。「终态」= 该阶段不计入「在跟」与「逾期」统计；保存后销售工作台与其数字同步。</p>
<form method='post' action='/admin/settings/stages'>{stage_rows}<p><button>保存阶段设置</button></p></form>
</section>
<section class='card'><div class='panel-title'><h2>RFQ 询价表单字段</h2><span class='field-state'>上次修改：{esc(fields_updated or '未修改（当前为默认）')}</span></div>
<p class='muted'>三个买方入口（聊天窗 / 产品详情页 / 旧询价页）与后台校验同口径。隐藏只影响买家表单展示与新提交，历史数据保留；邮箱与电话不可同时隐藏。</p>
<form method='post' action='/admin/settings/rfq-fields'><table><thead><tr><th>字段</th><th>收集方式</th></tr></thead><tbody>{field_rows}</tbody></table><p><button>保存字段设置</button></p></form>
</section>
{category_section}
<section class='card'><div class='panel-title'><h2>通知接收人</h2><span class='field-state'>上次修改：{esc(notify_updated or '未登记')}</span></div>
<p class='muted'>渠道未接入、暂不发送：当前版本还没有邮件/短信等通知通道；此处仅登记接收人名单，功能接入后才会按此发送。</p>
<form method='post' action='/admin/settings/notify'><label>接收人邮箱（每行一个）</label><textarea name='recipients' rows='4' placeholder='ops@company.example'>{recipients_value}</textarea><p><button>保存接收人</button></p></form>
</section>
"""
        self.send_html(self.console_page("企业设置", body, "settings"), 200)

    def admin_settings_save_stages(self, form: dict[str, str | bytes]) -> None:
        stages = self.db.get_lead_stages(self.tenant_id)
        labels: dict[str, str] = {}
        for stage in stages:
            label = str(form.get(f"label_{stage['key']}", "")).strip()
            if not label:
                self.redirect("/admin/settings?notice=stages_invalid")
                return
            labels[stage["key"]] = label[:24]
        if len(set(labels.values())) != len(labels):
            self.redirect("/admin/settings?notice=stages_invalid")
            return
        terminals = {stage["key"]: bool(str(form.get(f"terminal_{stage['key']}", "")).strip()) for stage in stages}
        ordered = [stage["key"] for stage in stages]
        move = str(form.get("move", "")).strip()
        if move:
            moved_key, _, direction = move.partition(":")
            if moved_key in ordered:
                index = ordered.index(moved_key)
                target = index - 1 if direction == "up" else index + 1
                if 0 <= target < len(ordered):
                    ordered[index], ordered[target] = ordered[target], ordered[index]
        self.db.set_tenant_setting(
            self.tenant_id, SETTING_LEAD_STAGES,
            [{"key": key, "label": labels[key], "terminal": terminals[key]} for key in ordered],
            self.actor,
        )
        self.redirect("/admin/settings?notice=stages")

    def admin_settings_save_rfq_fields(self, form: dict[str, str | bytes]) -> None:
        wanted: dict[str, str] = {}
        for field_key in RFQ_FIELD_ORDER:
            state = str(form.get(f"state_{field_key}", "")).strip()
            if state not in RFQ_FIELD_STATES:
                self.redirect("/admin/settings?notice=fields_invalid")
                return
            wanted[field_key] = state
        if wanted["email"] == "hidden" and wanted["phone"] == "hidden":
            self.redirect("/admin/settings?notice=fields_both_hidden")
            return
        self.db.set_tenant_setting(self.tenant_id, SETTING_RFQ_FIELDS, wanted, self.actor)
        self.redirect("/admin/settings?notice=fields")

    def admin_settings_save_rfq_fields_category(self, form: dict[str, str | bytes]) -> None:
        category = str(form.get("category", "")).strip()
        try:
            if str(form.get("clear", "")).strip() == "1":
                overrides: dict[str, str] = {}
            else:
                overrides = {}
                for field_key in RFQ_FIELD_ORDER:
                    state = str(form.get(f"field_{field_key}", "")).strip()
                    if state:
                        overrides[field_key] = state
            self.db.set_rfq_fields_for_category(self.tenant_id, category, overrides, self.actor)
        except ValueError:
            self.redirect("/admin/settings?notice=rfq_category_invalid")
            return
        self.redirect("/admin/settings?notice=rfq-category")

    def admin_settings_save_notify(self, form: dict[str, str | bytes]) -> None:
        raw = str(form.get("recipients", ""))
        candidates = [item.strip() for item in re.split(r"[,\n;，；]+", raw) if item.strip()]
        if len(candidates) > 20 or any(not EMAIL_PATTERN.match(item) for item in candidates):
            self.redirect("/admin/settings?notice=notify_invalid")
            return
        recipients = list(dict.fromkeys(candidates))
        self.db.set_tenant_setting(self.tenant_id, SETTING_NOTIFY_RECIPIENTS, recipients, self.actor)
        self.redirect("/admin/settings?notice=notify")

    # ── V1-04 外部渠道接入：会话收件箱（列表 / 详情 / 回复 / AI 开关） ──────────
    def inbox_section(self, slug: str) -> dict | None:
        """按 URL 段 slug 解析收件箱模块（web / 来源平台 slug）；未知返回 None。"""
        slug = str(slug or "").strip()
        for section in inbox_sections(self.db, self.tenant_id):
            if section["slug"] == slug:
                return section
        return None

    def _inbox_conversation_in_section(self, section: dict, conversation: sqlite3.Row) -> bool:
        """会话是否属于该收件箱模块：网站聊天=web+website_api；原生=对应 source；平台=LangBot 轨按 adapter 归属。"""
        source = conversation["source"] or ""
        kind = section.get("kind")
        if kind == "web":
            return source in ("web", "website_api")
        if kind == "native":
            return source == (section.get("native_channel") or "")
        if source != "langbot":
            return False
        identity = self.db.get_channel_identity_by_conversation(self.tenant_id, "langbot", conversation["id"])
        adapter = ""
        if identity:
            try:
                adapter = str((json.loads(identity["platform_meta"] or "{}") or {}).get("adapter") or "")
            except json.JSONDecodeError:
                adapter = ""
        return inbox_platform_entry(adapter)["slug"] == section["slug"]

    def _inbox_message_view(self, message: sqlite3.Row) -> tuple[str, str, str]:
        """单条消息 →（标注、正文、样式类）。AI 与人工分开标注；人工显示回复人。"""
        role = message["role"] or ""
        source = message["source"] or ""
        actor = message["actor_email"] or ""
        content = message["content"] or ""
        if source == "rfq-form-submitted":
            return "买方 · 询价链接提交", content, "buyer"
        if role == "buyer" or source.startswith("buyer-followup"):
            return "买方", content, "buyer"
        if source == "human-engineer":
            body = content.removeprefix("工程师回复：")
            suffix = f"（{actor}）" if actor else self._engineer_actor_suffix(message["conversation_id"], body)
            return f"人工 · 工程师{suffix}", body, "human"
        if source.startswith("engineer-followup"):
            suffix = f"（{actor}）" if actor else ""
            return f"人工 · 工程师追问{suffix}", content, "human"
        if source == "staff-reply":
            body = content.removeprefix("卖家回复：")
            return (f"人工 · 卖家回复（{actor}）" if actor else "人工 · 卖家回复"), body, "human"
        if source == "rfq-link":
            return (f"人工 · 询价链接（{actor}）" if actor else "人工 · 询价链接"), content, "human"
        if source == "channel-requirements":
            return "AI · 工况整理", content, "ai"
        if source == "channel-recommendation":
            return "AI · 匹配结果", content, "ai"
        if source == "channel-guidance":
            return "AI · 提示", content, "ai"
        if source == "rfq-ack":
            return "系统 · 询价回执", content, "system"
        if source == "grounded-answer":
            return "AI · 有据回答", content, "ai"
        if source == "unresolved-question":
            return "AI · 未能确认（待人工）", content, "ai"
        if source.startswith("human-request"):
            return "系统 · 已转交工程师", content, "system"
        return ("AI 回复" if role == "assistant" else "系统消息"), content, "ai"

    def _engineer_actor_suffix(self, conversation_id: str, body: str) -> str:
        """旧消息未记回复人时的尽力还原：按任务答复原文匹配已处理任务。"""
        row = self.db.conn.execute(
            "SELECT assigned_to FROM human_tasks WHERE tenant_id=? AND conversation_id=? AND response=? AND assigned_to IS NOT NULL ORDER BY resolved_at DESC LIMIT 1",
            (self.tenant_id, conversation_id, body),
        ).fetchone()
        return f"（{row['assigned_to']}）" if row and row["assigned_to"] else ""

    # ── V1-04 切片③④：渠道收信入口（LangBot Webhook）/ RFQ 填写链接公开页 / 任务回复投递 ──

    def _twilio_verified_params(self) -> dict | None:
        """读取并验签 Twilio 表单回调（fail-closed）。

        返回参数字典；未配置 TWILIO_AUTH_TOKEN、缺签名或签名不符时，已发送 403 并返回 None。
        验签 URL 重建：优先 TWILIO_PUBLIC_BASE（隧道场景可设）；否则 X-Forwarded-Proto + Host。
        """
        auth_token = os.environ.get("TWILIO_AUTH_TOKEN", "").strip()
        if not auth_token:
            self.send_json({"error": "channel_not_configured"}, 403)
            return None
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length).decode("utf-8") if length else ""
        params = {key: values[0] for key, values in parse_qs(raw, keep_blank_values=True).items()}
        provided = self.headers.get("X-Twilio-Signature", "")
        public_base = os.environ.get("TWILIO_PUBLIC_BASE", "").strip().rstrip("/")
        if public_base:
            url = public_base + urlparse(self.path).path
        else:
            scheme = (self.headers.get("X-Forwarded-Proto", "http").split(",")[0].strip() or "http")
            host = self.headers.get("Host", "")
            url = f"{scheme}://{host}{urlparse(self.path).path}"
        signing_payload = url + "".join(f"{key}{params[key]}" for key in sorted(params))
        expected = base64.b64encode(
            hmac.new(auth_token.encode("utf-8"), signing_payload.encode("utf-8"), hashlib.sha1).digest()
        ).decode("ascii")
        if not provided or not hmac.compare_digest(expected, provided):
            self.send_json({"error": "invalid_signature"}, 403)
            return None
        return params

    def handle_whatsapp_status(self) -> None:
        """Twilio 状态回调（WhatsApp）：验签（fail-closed）→ 推进投递状态（delivered / failed）。"""
        params = self._twilio_verified_params()
        if params is None:
            return
        message_sid = str(params.get("MessageSid", "")).strip()
        status = str(params.get("MessageStatus", "")).strip().lower()
        error_text = ""
        if status in ("failed", "undelivered"):
            code = str(params.get("ErrorCode", "")).strip()
            message = str(params.get("ErrorMessage", "")).strip()
            error_text = f"投递失败：{message or status}" + (f"（code={code}）" if code else "")
        changed = apply_whatsapp_delivery_status(self.db, message_sid, status, error_text)
        self.send_json({"ok": True, "changed": changed})

    def handle_whatsapp_webhook(self) -> None:
        """WhatsApp 入站 webhook（按签名头分流双形状，均 fail-closed）。

        - Meta Cloud API（X-Hub-Signature-256）→ handle_whatsapp_webhook_meta；
        - Twilio（X-Twilio-Signature）→ 验签 → 会话落库 → 统一大脑分流（AI / 人工接管）。
        单租户假设：取启用的 whatsapp 渠道账户（多租户路由＝F-25，产品化时再做）。
        """
        if self.headers.get("X-Hub-Signature-256"):
            self.handle_whatsapp_webhook_meta()
            return
        params = self._twilio_verified_params()
        if params is None:
            return
        account = self.db.conn.execute(
            "SELECT * FROM channel_accounts WHERE channel='whatsapp' AND status='enabled' ORDER BY rowid LIMIT 1"
        ).fetchone()
        if not account:
            self.send_twiml()
            return
        # 试用期应答载体（TWILIO_TRIAL_TWIML_REPLY=1，默认关）：
        # Twilio 试用禁自由文本 API（21654），探针：把刚生成且投递失败的回复文本随 webhook 应答带回。
        trial_carrier = os.environ.get("TWILIO_TRIAL_TWIML_REPLY", "").strip() == "1"
        before_rowid = 0
        if trial_carrier:
            snapshot = self.db.conn.execute(
                "SELECT COALESCE(MAX(rowid), 0) AS n FROM messages WHERE tenant_id=? AND role='assistant'",
                (account["tenant_id"],),
            ).fetchone()
            before_rowid = snapshot["n"] if snapshot else 0
        try:
            outcome = handle_whatsapp_inbound(self.db, self.ai, account["tenant_id"], params)
        except Exception:
            # 500 让 Twilio 重试（消息去重兜底）；4xx/5xx 无需 TwiML 体。
            self.send_json({"ok": False, "error": "processing_failed"}, 500)
            return
        if trial_carrier:
            fresh = self.db.conn.execute(
                "SELECT content, delivery_status FROM messages "
                "WHERE tenant_id=? AND role='assistant' AND rowid>? ORDER BY rowid DESC LIMIT 1",
                (account["tenant_id"], before_rowid),
            ).fetchone()
            if fresh and (fresh["delivery_status"] or "") == "failed" and str(fresh["content"] or "").strip():
                self.send_twiml(f"<Response><Message>{esc(str(fresh['content']))}</Message></Response>")
                return
        self.send_twiml()


    def handle_whatsapp_webhook_verify(self) -> None:
        """Meta Webhook 验证握手（GET）：校验 verify_token 后原样回 hub.challenge（fail-closed）。"""
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        mode = str((query.get("hub.mode") or [""])[0])
        token = str((query.get("hub.verify_token") or [""])[0])
        challenge = str((query.get("hub.challenge") or [""])[0])
        expected = os.environ.get("META_VERIFY_TOKEN", "").strip()
        if not expected or mode != "subscribe" or not hmac.compare_digest(token, expected):
            self.send_json({"error": "verification_failed"}, 403)
            return
        data = challenge.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def handle_whatsapp_webhook_meta(self) -> None:
        """Meta Cloud API 入站消息 / 状态回执：X-Hub-Signature-256 验签（fail-closed）→ 统一大脑。

        响应 JSON 200（Meta 只看状态码，无 TwiML 要求）。
        """
        app_secret = os.environ.get("META_APP_SECRET", "").strip()
        if not app_secret:
            self.send_json({"error": "channel_not_configured"}, 403)
            return
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        provided = self.headers.get("X-Hub-Signature-256", "")
        expected = "sha256=" + hmac.new(app_secret.encode("utf-8"), raw, hashlib.sha256).hexdigest()
        if not provided or not hmac.compare_digest(expected, provided):
            self.send_json({"error": "invalid_signature"}, 403)
            return
        try:
            payload = json.loads(raw.decode("utf-8"))
        except Exception:
            self.send_json({"ok": True, "skipped": "malformed"}, 200)
            return
        account = self.db.conn.execute(
            "SELECT * FROM channel_accounts WHERE channel='whatsapp' AND status='enabled' ORDER BY rowid LIMIT 1"
        ).fetchone()
        if not account:
            self.send_json({"ok": True, "skipped": "channel_disabled"}, 200)
            return
        try:
            outcome = handle_whatsapp_meta_event(self.db, self.ai, account["tenant_id"], payload)
        except Exception:
            self.send_json({"ok": False, "error": "processing_failed"}, 500)
            return
        self.send_json({"ok": True, "outcome": outcome}, 200)

    def handle_whatsapp_personal_webhook(self, token: str) -> None:
        """WhatsApp 个人版（WAHA 桥）事件入口：URL 令牌 fail-closed → 统一大脑。

        HMAC 加固：若配置 WHATSAPP_PERSONAL_HMAC_KEY 且请求带 X-Webhook-Hmac，必须验签通过；
        未带签名头时仅凭 URL 令牌（与既有桥同口径，后续可升级为强制签名）。
        """
        expected = os.environ.get("WHATSAPP_PERSONAL_TOKEN", "").strip()
        if not expected or not hmac.compare_digest(str(token), expected):
            self.send_json({"error": "invalid_token"}, 403)
            return
        hmac_key = os.environ.get("WHATSAPP_PERSONAL_HMAC_KEY", "").strip()
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        provided = self.headers.get("X-Webhook-Hmac", "")
        if hmac_key and provided:
            # WAHA 头的算法在 X-Webhook-Hmac-Algorithm（默认 sha512）；兼容 hex / base64 两种编码表达。
            algorithm = (self.headers.get("X-Webhook-Hmac-Algorithm", "") or "sha512").strip().lower()
            if algorithm not in hashlib.algorithms_available:
                algorithm = "sha512"
            mac = hmac.new(hmac_key.encode("utf-8"), raw, algorithm)
            candidates = {mac.hexdigest().lower(), base64.b64encode(mac.digest()).decode("ascii").lower()}
            provided_clean = provided.strip().lower()
            if provided_clean.startswith("sha256=") or provided_clean.startswith("sha512="):
                provided_clean = provided_clean.split("=", 1)[1]
            if not any(hmac.compare_digest(candidate, provided_clean) for candidate in candidates):
                self.send_json({"error": "invalid_signature"}, 403)
                return
        try:
            event = json.loads(raw.decode("utf-8"))
        except Exception:
            self.send_json({"ok": True, "skipped": "malformed"}, 200)
            return
        account = self.db.conn.execute(
            "SELECT * FROM channel_accounts WHERE channel='whatsapp_personal' AND status='enabled' ORDER BY rowid LIMIT 1"
        ).fetchone()
        if not account:
            self.send_json({"ok": True, "skipped": "channel_disabled"}, 200)
            return
        try:
            outcome = handle_whatsapp_personal_event(self.db, self.ai, account["tenant_id"], event)
        except Exception:
            self.send_json({"ok": False, "error": "processing_failed"}, 500)
            return
        _payload = event.get("payload") if isinstance(event, dict) else {}
        _payload = _payload if isinstance(_payload, dict) else {}
        _raw_id = _payload.get("id")
        _debug_id = str(_raw_id.get("_serialized") if isinstance(_raw_id, dict) else _raw_id or "")
        _whatsapp_personal_debug({
            "at": now_iso(),
            "event": str(event.get("event") or "") if isinstance(event, dict) else "",
            "from": str(_payload.get("from") or ""),
            "to": str(_payload.get("to") or ""),
            "fromMe": bool(_payload.get("fromMe")),
            "source": str(_payload.get("source") or ""),
            "id": _debug_id[:80],
            "hasMedia": bool(_payload.get("hasMedia")),
            "body": str(_payload.get("body") or "")[:80],
            "outcome": outcome.get("action") if isinstance(outcome, dict) else str(outcome),
        })
        self.send_json({"ok": True, "outcome": outcome}, 200)

    def handle_langbot_webhook(self, token: str) -> None:
        """LangBot Webhook 收信：URL 令牌即凭据（LangBot 不签名，fail-closed）。

        - 令牌不匹配 → 404；渠道停用 → 200 skipped（不处理任何消息）；
        - 一律返回 skip_pipeline=true：平台是唯一「主脑」，LangBot 自身流水线对这批消息不介入。
        """
        account = None
        for row in self.db.conn.execute("SELECT * FROM channel_accounts WHERE channel='langbot'").fetchall():
            try:
                config = json.loads(row["config_json"] or "{}")
            except json.JSONDecodeError:
                config = {}
            stored = str(config.get("webhook_token") or "")
            if stored and hmac.compare_digest(stored, token):
                account = row
                break
        if not account:
            self.send_json({"status": "error", "error": "webhook_not_found"}, 404)
            return
        if account["status"] != "enabled":
            self.send_json({"status": "ok", "skipped": "channel_disabled", "skip_pipeline": True}, 200)
            return
        payload = self.read_json()
        if payload is None:
            return
        try:
            outcome = handle_langbot_event(self.db, self.ai, account["tenant_id"], payload)
        except Exception:
            self.send_json({"status": "error", "error": "processing_failed"}, 500)
            return
        self.send_json({"status": "ok", "outcome": (outcome or {}).get("action", "ignored"), "qa_source": (outcome or {}).get("qa_source", ""), "skip_pipeline": True}, 200)

    def _rfq_link_context(self, token: str) -> tuple[sqlite3.Row | None, sqlite3.Row | None, str]:
        """RFQ 链接公共解析：令牌 →（会话, 渠道 slug）。无效返回 (None, None, \"\")。"""
        record = self.db.find_rfq_form_token(token)
        conversation = None
        if record:
            conversation = self.db.get_conversation(record["tenant_id"], record["conversation_id"])
        channel = (conversation["source"] or "") if conversation else ""
        if not record or not conversation or channel not in CHANNEL_KEYS:
            return None, None, ""
        return record, conversation, channel

    def public_rfq_form(self, token: str, values: dict | None = None, error: str = "", status: int = 200) -> None:
        """公开 RFQ 填写页（令牌即凭据；可重复打开，再次提交更新同一张询价单）。"""
        record, conversation, channel = self._rfq_link_context(token)
        if not record or not conversation:
            self.send_html(layout("链接不可用", "<div class='error'>链接无效或已撤销；请联系与您沟通的卖家重新获取。</div>", public=True), 404)
            return
        tenant_id = record["tenant_id"]
        conversation_id = conversation["id"]
        field_config = self.db.get_rfq_field_config(tenant_id, self.db.rfq_category_hint(tenant_id, conversation_id, None, ""))
        existing = self.db.conn.execute(
            "SELECT * FROM rfqs WHERE tenant_id=? AND conversation_id=? AND entry_surface=? ORDER BY created_at DESC LIMIT 1",
            (tenant_id, conversation_id, f"{channel}_link"),
        ).fetchone()
        if values is None:
            values = {"email": "", "phone": "", "quantity": "", "destination": "", "notes": ""}
            if existing:
                content = self.db.rfq_current_content(existing)
                values = {
                    "email": content.get("contact_email") or "", "phone": content.get("contact_phone") or "",
                    "quantity": content.get("quantity") or "", "destination": content.get("destination") or "",
                    "notes": content.get("notes") or "",
                }
        existing_short = existing["id"][:8] if existing else ""
        body = f"""
<div class='toolbar'><div><h1>填写询价信息</h1><div class='muted'>提交后进入卖家的销售工作台；本链接可重复打开、随时修改。</div></div></div>
<section class='card'>{render_rfq_link_form(field_config, values, error, existing_short)}</section>
"""
        self.send_html(layout("填写询价信息", body, public=True), status)

    def public_rfq_submit(self, token: str, form: dict) -> None:
        """公开 RFQ 填写页提交：首次提交建单；再次提交更新同一张单（版本 +1）。"""
        record, conversation, channel = self._rfq_link_context(token)
        if not record or not conversation:
            self.send_html(layout("链接不可用", "<div class='error'>链接无效或已撤销；请联系与您沟通的卖家重新获取。</div>", public=True), 404)
            return
        tenant_id = record["tenant_id"]
        conversation_id = conversation["id"]
        field_config = self.db.get_rfq_field_config(tenant_id, self.db.rfq_category_hint(tenant_id, conversation_id, None, ""))
        raw = {key: str(form.get(name, "")).strip() for key, name in (("email", "contact_email"), ("phone", "contact_phone"), ("quantity", "quantity"), ("destination", "destination"), ("notes", "notes"))}
        for key in ("email", "phone", "quantity", "destination", "notes"):
            if field_config.get(key) == "hidden":
                raw[key] = ""
        if str(form.get("contact_confirmed", "")) != "1":
            self.public_rfq_form(token, values=raw, error="请先勾选确认：销售可通过上述联系方式就本次询价与您联系。", status=400)
            return
        missing = [RFQ_FIELD_LABELS[key] for key in RFQ_FIELD_ORDER if field_config.get(key) == "required" and not raw.get(key, "")]
        if missing:
            self.public_rfq_form(token, values=raw, error="请填写：" + "、".join(missing), status=400)
            return
        contact = {"contact_email": raw["email"], "contact_phone": raw["phone"], "quantity": raw["quantity"], "region": raw["destination"]}
        entry = f"{channel}_link"
        existing = self.db.conn.execute(
            "SELECT * FROM rfqs WHERE tenant_id=? AND conversation_id=? AND entry_surface=? ORDER BY created_at DESC LIMIT 1",
            (tenant_id, conversation_id, entry),
        ).fetchone()
        try:
            if existing:
                self.db.revise_rfq(
                    tenant_id, conversation_id, existing["id"], existing["current_revision"],
                    {"contact_email": raw["email"], "contact_phone": raw["phone"], "quantity": raw["quantity"], "destination": raw["destination"], "notes": raw["notes"]},
                    secrets.token_hex(12),
                )
                rfq_id = existing["id"]
                action_text = f"通过询价链接更新了询价单 {rfq_id[:8]}。"
            else:
                rfq_id, _created = self.db.create_rfq(tenant_id, conversation_id, secrets.token_hex(12), contact, raw["notes"], None, entry, "")
                action_text = f"通过询价链接提交了询价单 {rfq_id[:8]}。"
        except ConflictError as error:
            self.public_rfq_form(token, values=raw, error=f"提交未完成：{error}。请刷新后重试。", status=409)
            return
        except ValueError as error:
            self.public_rfq_form(token, values=raw, error=f"提交未完成：{error}", status=400)
            return
        self.db.save_message(tenant_id, conversation_id, "buyer", action_text, "rfq-form-submitted", channel=channel)
        channel_send_stored(self.db, tenant_id, conversation, CHANNEL_RFQ_ACK, "rfq-ack")
        body = f"""
<div class='toolbar'><div><h1>已提交 / Submitted</h1></div></div>
<section class='card'><p>您的询价信息已提交（编号 {esc(rfq_id[:8])}）。</p>
<p class='muted'>您可以随时重新打开本链接更新内容；更新会保存到同一张询价单。</p></section>
"""
        self.send_html(layout("已提交", body, public=True), 200)

    def deliver_latest_task_message(self, task_id: str, source: str) -> None:
        """任务操作后把最新一条消息投递到渠道（网站聊天会话无外发通道，直接忽略）。"""
        task = self.db.get_human_task(self.tenant_id, task_id)
        if not task:
            return
        conversation = self.db.get_conversation(self.tenant_id, task["conversation_id"])
        if not conversation or (conversation["source"] or "") not in CHANNEL_KEYS:
            return
        row = self.db.conn.execute(
            "SELECT id FROM messages WHERE tenant_id=? AND conversation_id=? AND source=? ORDER BY rowid DESC LIMIT 1",
            (self.tenant_id, task["conversation_id"], source),
        ).fetchone()
        if row:
            dispatch_outbound(self.db, self.tenant_id, row["id"])

    def admin_inbox_rfq_link(self, slug: str, conversation_id: str) -> None:
        """给平台会话主动发送「RFQ 填写链接」消息（停止主动联系时拒绝）。"""
        section = self.inbox_section(slug)
        conversation = self.db.get_conversation(self.tenant_id, conversation_id)
        if not section or not conversation or not self._inbox_conversation_in_section(section, conversation):
            raise ValueError("会话不存在或不属于这个渠道")
        if section["kind"] != "platform":
            raise ValueError("该渠道的发送通道尚未开通")
        if conversation["stop_proactive_contact"]:
            self.redirect(f"/admin/inbox/{slug}/{conversation_id}?notice=link_stopped")
            return
        token = self.db.create_rfq_form_token(self.tenant_id, conversation_id, self.actor)
        host = self.headers.get("Host", "127.0.0.1:8137")
        scheme = "https" if self.headers.get("X-Forwarded-Proto") == "https" else "http"
        text = f"您可以打开以下链接填写询价信息（可重复打开、随时修改）：\n{scheme}://{host}/rfq/{token}"
        try:
            _message_id, sent, _error = channel_send_stored(self.db, self.tenant_id, conversation, text, "rfq-link", actor_email=self.actor, proactive=True)
        except ValueError:
            # 主动发送门控（停止主动联系）在总线入口兜底拦截
            self.redirect(f"/admin/inbox/{slug}/{conversation_id}?notice=link_stopped")
            return
        self.redirect(f"/admin/inbox/{slug}/{conversation_id}?notice={'link_sent' if sent else 'link_failed'}")

    def admin_inbox(self, slug: str, query: dict[str, list[str]]) -> None:
        """会话收件箱模块列表：按来源平台分开（平台从消息里识别；已接入未收消息的平台显示空态）。"""
        section = self.inbox_section(slug)
        if not section:
            self.send_html(self.console_page("未找到", "<div class='error'>页面不存在。</div>", ""), 404)
            return
        label = section["label"]
        if section["kind"] == "web":
            source_where, params = "c.source IN ('web','website_api')", ()
        elif section["kind"] == "native":
            source_where, params = "c.source=?", (section["native_channel"],)
        else:
            aliases = list(section["adapters"])
            placeholders = ",".join("?" for _ in aliases)
            source_where = (
                "c.source='langbot' AND EXISTS (SELECT 1 FROM channel_identities i "
                "WHERE i.tenant_id=c.tenant_id AND i.conversation_id=c.id "
                f"AND CASE WHEN json_valid(i.platform_meta) THEN lower(COALESCE(json_extract(i.platform_meta,'$.adapter'),'')) ELSE '' END IN ({placeholders}))"
            )
            params = tuple(aliases)
        rows = self.db.conn.execute(
            f"""SELECT c.*,
                (SELECT COUNT(*) FROM messages m WHERE m.tenant_id=c.tenant_id AND m.conversation_id=c.id) AS message_count,
                (SELECT m.content FROM messages m WHERE m.conversation_id=c.id ORDER BY m.rowid DESC LIMIT 1) AS last_content,
                (SELECT r.contact_email FROM rfqs r WHERE r.tenant_id=c.tenant_id AND r.conversation_id=c.id AND r.contact_email<>'' ORDER BY r.created_at DESC LIMIT 1) AS rfq_email,
                (SELECT l.contact_email FROM leads l WHERE l.tenant_id=c.tenant_id AND l.conversation_id=c.id AND l.contact_email<>'' ORDER BY l.created_at DESC LIMIT 1) AS lead_email,
                (SELECT i.display_name FROM channel_identities i WHERE i.tenant_id=c.tenant_id AND i.conversation_id=c.id LIMIT 1) AS ext_name,
                (SELECT i.external_user_id FROM channel_identities i WHERE i.tenant_id=c.tenant_id AND i.conversation_id=c.id LIMIT 1) AS ext_id,
                (SELECT COUNT(*) FROM messages m WHERE m.tenant_id=c.tenant_id AND m.conversation_id=c.id AND m.delivery_status='failed') AS failed_count
            FROM conversations c WHERE c.tenant_id=? AND {source_where}
            ORDER BY c.updated_at DESC, c.id DESC LIMIT 50""",
            (self.tenant_id, *params),
        ).fetchall()
        items = []
        for row in rows:
            if section["kind"] == "web":
                title = f"网站访客 · {row['id'][:6]}"
            else:
                title = row["ext_name"] or row["ext_id"] or f"平台访客 · {row['id'][:6]}"
            contact = row["rfq_email"] or row["lead_email"] or ""
            meta_parts = [f"消息 {row['message_count']} 条", f"最后活动 {esc((row['updated_at'] or '')[:16].replace('T', ' '))}"]
            if contact:
                meta_parts.insert(0, f"联系方式 {esc(contact)}")
            if row["ai_paused"]:
                meta_parts.append("<span class='badge pending_review'>AI 已暂停</span>")
            if row["failed_count"]:
                meta_parts.append(f"<span class='badge withdrawn'>{row['failed_count']} 条发送失败</span>")
            if row["stop_proactive_contact"]:
                meta_parts.append("<span class='badge withdrawn'>停止主动联系</span>")
            preview = (row["last_content"] or "（暂无消息）").strip().splitlines()[0][:60]
            items.append(
                f"<div class='simple-item'><div><strong>{esc(title)}</strong>"
                f"<small>最后一条：{esc(preview)}</small><small>{' · '.join(meta_parts)}</small></div>"
                f"<div class='actions'><a class='button secondary' href='/admin/inbox/{slug}/{esc(row['id'])}'>查看会话</a></div></div>"
            )
        if section["kind"] == "web":
            empty = "还没有网站聊天会话；买方在官网窗口发起咨询后，会在这里统一查看与回复。"
        elif section["kind"] == "native":
            channel = section["native_channel"]
            account = self.db.get_channel_account(self.tenant_id, channel)
            secret = channel_secret_status(channel)
            if not account or account["status"] != "enabled":
                empty = "该渠道未启用——管理员可在「设置与治理 › 渠道接入」完成配置并启用。"
            elif not secret["configured"]:
                empty = f"渠道已启用，但本机凭证未配置（缺少 {'、'.join(secret['missing'])}）；凭证就绪前不会收发任何消息。"
            else:
                empty = "渠道已启用且凭证就绪；买方发来的消息会出现在这里。"
        elif section["has_data"]:
            empty = "暂时没有会话。"
        else:
            empty = f"{label} 已接入（LangBot），还没有消息；确认 LangBot 中对应平台的机器人已启用、并把平台收信地址登记为 Webhook。"
        list_html = "".join(items) or f"<div class='muted-block'>{esc(empty)}</div>"
        body = f"""
<div class='toolbar'><div><h1>{esc(label)}</h1><div class='muted'>会话收件箱 · 按来源平台分开；消息经 LangBot 或原生通道进入平台后自动归入对应平台模块。</div></div></div>
<section class='card'><div class='panel-title'><h2>最近会话</h2><span class='muted'>按最后活动倒序 · 最多 50 条</span></div>
<div class='simple-list'>{list_html}</div></section>"""
        self.send_html(self.console_page(f"{label} · 会话", body, f"inbox_{slug}"))

    def admin_inbox_conversation(self, slug: str, conversation_id: str, query: dict[str, list[str]]) -> None:
        """会话详情（聊天视图）：左买方 / 右 AI·人工 / 中系统；顶部 AI 滑块开关 + 底部输入栏。"""
        section = self.inbox_section(slug)
        conversation = self.db.get_conversation(self.tenant_id, conversation_id)
        if not section or not conversation or not self._inbox_conversation_in_section(section, conversation):
            self.send_html(self.console_page("未找到", "<div class='error'>会话不存在或不属于这个平台。</div>", ""), 404)
            return
        label = section["label"]
        is_web = section["kind"] == "web"
        sent_html = (
            "<div class='notice'>回复已发送——买方在网站聊天窗口里可见。</div>"
            if is_web
            else f"<div class='notice'>回复已发送——已经由 {esc(label)} 直达买方。</div>"
        )
        notices = {
            "sent": sent_html,
            "reply_failed": "<div class='error'>回复发送失败——消息已保存并标为「发送失败」，原因见聊天记录。</div>",
            "ai_paused": "<div class='notice'>已暂停本会话的 AI 自动回复；买方提问将按「待人工」处理。</div>",
            "ai_resumed": "<div class='notice'>已恢复本会话的 AI 自动回复。</div>",
            "link_sent": f"<div class='notice'>询价链接已发送——买方在 {esc(label)} 里点开即可填写。</div>",
            "link_failed": "<div class='error'>询价链接发送失败——请检查渠道凭证与网络后重试。</div>",
            "link_stopped": "<div class='error'>该会话已标记「停止主动联系」，不能主动发送询价链接。</div>",
            "retried_ok": "<div class='notice'>重试成功——消息已发出。</div>",
            "retried_fail": "<div class='error'>重试仍未成功——原因见对应消息，可修复渠道后再试。</div>",
            "retry_already": "<div class='notice'>该消息已经发出，无需重试。</div>",
        }
        notice = notices.get(query.get("notice", [""])[0], "")
        messages = self.db.list_messages(self.tenant_id, conversation_id)
        message_rows = []
        for message in messages:
            who, body_text, css = self._inbox_message_view(message)
            when = esc((message["created_at"] or "")[:16].replace("T", " "))
            delivery_html = ""
            delivery_status = message["delivery_status"] or ""
            if not is_web and delivery_status:
                attempts = message["delivery_attempts"] or 0
                if delivery_status == "failed":
                    delivery_label = f"发送失败：{message['delivery_error'] or '原因未知'}"
                    if attempts > 1:
                        delivery_label += f"（已尝试 {attempts} 次）"
                else:
                    delivery_label = {"queued": "排队中", "sent": "已发送", "delivered": "已送达"}.get(delivery_status, delivery_status)
                delivery_html = f"<span class='when'>{esc(delivery_label)}</span>"
                if delivery_status == "failed":
                    delivery_html += (
                        f"<form method='post' action='/admin/inbox/{slug}/{esc(conversation_id)}/messages/{esc(message['id'])}/retry' class='inline' style='margin-left:8px'>"
                        f"<button class='secondary'>重试发送</button></form>"
                    )
            side = {"buyer": "left", "system": "center"}.get(css, "right")
            message_rows.append(
                f"<div class='chat-msg {side} {css}'><div class='chat-bubble'>{esc(body_text)}</div>"
                f"<div class='chat-meta'><span class='who'>{esc(who)}</span><span class='when'>{when}</span>{delivery_html}</div></div>"
            )
        transcript = "".join(message_rows) or "<div class='muted-block'>这个会话还没有消息。</div>"
        rfq_rows = self.db.conn.execute(
            "SELECT id, status, quantity, created_at FROM rfqs WHERE tenant_id=? AND conversation_id=? ORDER BY created_at",
            (self.tenant_id, conversation_id),
        ).fetchall()
        lead_row = self.db.conn.execute(
            "SELECT id FROM leads WHERE tenant_id=? AND conversation_id=? AND merged_into IS NULL ORDER BY created_at DESC LIMIT 1",
            (self.tenant_id, conversation_id),
        ).fetchone()
        chips = []
        for rfq_row in rfq_rows:
            chips.append(f"<a class='chip' href='/admin/rfqs/{esc(rfq_row['id'])}'>询价单 {esc(rfq_row['id'][:8])} · {esc(rfq_row['status'])}</a>")
        if lead_row:
            chips.append(f"<a class='chip' href='/admin/leads/{esc(lead_row['id'])}'>关联线索</a>")
        rfq_pill_html = ""
        if section["kind"] == "platform":
            rfq_pill_html = (
                f"<form class='inline' method='post' action='/admin/inbox/{slug}/{esc(conversation_id)}/rfq-link'>"
                f"<button class='qpill' title='给买方发送询价填写链接'>{svg_icon('file')}发送询价单</button></form>"
            )
        chips_html = "".join(chips)
        chips_row = f"<div class='chat-chips'>{chips_html}</div>" if chips_html else ""
        stop_badge = "<span class='badge withdrawn'>停止主动联系</span>" if conversation["stop_proactive_contact"] else ""
        identity = None
        if not is_web:
            channel_key = "langbot" if section["kind"] == "platform" else section["native_channel"]
            identity = self.db.get_channel_identity_by_conversation(self.tenant_id, channel_key, conversation_id)
        ext_id = identity["external_user_id"] if identity else ""
        title_name = identity["display_name"] if identity and identity["display_name"] else (ext_id or f"会话 {conversation_id[:10]}")
        meta_bits = [f"{esc(label)}"]
        if ext_id:
            meta_bits.append(f"用户ID {esc(ext_id)}")
        meta_bits.append(f"消息 {len(messages)} 条")
        meta_bits.append(f"开始 {esc((conversation['created_at'] or '')[:16].replace('T', ' '))}")
        meta_line = " · ".join(meta_bits)
        initial = (title_name.strip()[:1] or "对").upper()
        paused = bool(conversation["ai_paused"])
        paused_note = "<span class='ios-state'>已暂停</span>" if paused else ""
        ai_switch = (
            f"<form method='post' action='/admin/inbox/{slug}/{esc(conversation_id)}/ai-toggle' class='ai-switch-form'>"
            f"<label class='ios-switch'><input type='checkbox'{'' if paused else ' checked'} onchange='this.form.submit()'>"
            f"<span class='ios-slider' aria-hidden='true'></span></label>"
            f"<span class='ios-text'>AI 自动回复 {paused_note}</span></form>"
        )
        if is_web:
            hint = "回复将立即出现在买方网站聊天窗口。"
        elif section["kind"] == "native":
            hint = f"回复将经 {esc(label)} 发送；发送通道未就绪时消息会标记「发送失败」并支持重试。"
        else:
            hint = f"回复将经 {esc(label)} 直达买方；发送失败会标记并可重试。"
        body = f"""
<div class='toolbar'><div><a class='button secondary' href='/admin/inbox/{slug}'>← 返回{esc(label)}列表</a></div><div class='actions'>{stop_badge}</div></div>
{notice}
<section class='card chat-shell'>
<header class='chat-head'>
<div class='chat-avatar' aria-hidden='true'>{esc(initial)}</div>
<div class='chat-id'><strong>{esc(title_name)}</strong><small>{meta_line}</small></div>
<div class='chat-tools'>{ai_switch}</div>
</header>
{chips_row}
<div class='chat-log'>{transcript}</div>
<form class='chat-composer' method='post' action='/admin/inbox/{slug}/{esc(conversation_id)}/reply'>
<textarea name='body' required maxlength='4000' placeholder='输入回复内容…'></textarea>
<button>发送</button>
</form>
<div class='chat-foot'>{rfq_pill_html}<span class='chat-hint'>{hint}</span></div>
</section>"""
        self.send_html(self.console_page(f"{label} · 会话", body, f"inbox_{slug}"))

    def admin_inbox_reply(self, slug: str, conversation_id: str, form: dict) -> None:
        """收件箱直接回复：网站聊天即时可见；渠道会话经发送器直达买方（失败可见）。"""
        section = self.inbox_section(slug)
        conversation = self.db.get_conversation(self.tenant_id, conversation_id)
        if not section or not conversation or not self._inbox_conversation_in_section(section, conversation):
            raise ValueError("会话不存在或不属于这个渠道")
        body_text = str(form.get("body", "")).strip()
        if not body_text:
            raise ValueError("回复内容不能为空")
        if section["kind"] == "web":
            message_id = self.db.save_message(
                self.tenant_id, conversation_id, "staff", f"卖家回复：{body_text}", "staff-reply",
                actor_email=self.actor, delivery_status="sent",
            )
            self.db.audit_staff_reply(self.tenant_id, conversation_id, message_id, self.actor)
            self.redirect(f"/admin/inbox/{slug}/{conversation_id}?notice=sent")
            return
        message_id, sent, _error = enqueue_outbound(
            self.db, self.tenant_id, conversation, "staff", f"卖家回复：{body_text}", "staff-reply",
            actor_email=self.actor, proactive=False,
        )
        self.db.audit_staff_reply(self.tenant_id, conversation_id, message_id, self.actor)
        self.redirect(f"/admin/inbox/{slug}/{conversation_id}?notice={'sent' if sent else 'reply_failed'}")

    def admin_inbox_ai_toggle(self, slug: str, conversation_id: str) -> None:
        """每会话「暂停/恢复 AI 自动回复」开关。"""
        section = self.inbox_section(slug)
        conversation = self.db.get_conversation(self.tenant_id, conversation_id)
        if not section or not conversation or not self._inbox_conversation_in_section(section, conversation):
            raise ValueError("会话不存在或不属于这个渠道")
        paused = not conversation["ai_paused"]
        self.db.set_conversation_ai_paused(self.tenant_id, conversation_id, paused, self.actor)
        self.redirect(f"/admin/inbox/{slug}/{conversation_id}?notice={'ai_paused' if paused else 'ai_resumed'}")

    def admin_inbox_message_retry(self, slug: str, conversation_id: str, message_id: str) -> None:
        """收件箱内重试一条发送失败的消息（出站总线幂等：已成功不重发）。"""
        section = self.inbox_section(slug)
        conversation = self.db.get_conversation(self.tenant_id, conversation_id)
        if not section or not conversation or not self._inbox_conversation_in_section(section, conversation):
            raise ValueError("会话不存在或不属于这个渠道")
        if section["kind"] == "web":
            raise ValueError("网站聊天消息无需重试")
        message = self.db.conn.execute(
            "SELECT * FROM messages WHERE tenant_id=? AND id=? AND conversation_id=?",
            (self.tenant_id, message_id, conversation_id),
        ).fetchone()
        if not message:
            raise ValueError("消息不存在")
        status_before = message["delivery_status"] or ""
        if status_before in ("sent", "delivered"):
            notice = "retry_already"
        else:
            sent, _error = retry_outbound_message(self.db, self.tenant_id, message_id, self.actor)
            notice = "retried_ok" if sent else "retried_fail"
        self.redirect(f"/admin/inbox/{slug}/{conversation_id}?notice={notice}")

    def admin_channels(self, query: dict[str, list[str]]) -> None:
        """渠道接入页（管理员）：配置与启停 WhatsApp / LangBot；凭证只读环境变量状态、不回显值。"""
        accounts = self.db.list_channel_accounts(self.tenant_id)
        notices = {
            "saved": "<div class='notice'>渠道配置已保存。</div>",
            "enabled": "<div class='notice'>渠道已启用。凭证就绪后，收信与发送将在对应切片上线时开始工作。</div>",
            "disabled": "<div class='notice'>渠道已停用，不会收发任何消息。</div>",
        }
        notice_code = query.get("notice", [""])[0]
        notice = notices.get(notice_code, "")
        if notice_code == "kb_synced":
            notice = (
                "<div class='notice'>知识库同步完成：新增 "
                f"{esc(query.get('created', ['0'])[0])} · 更新 {esc(query.get('updated', ['0'])[0])} · 删除 {esc(query.get('deleted', ['0'])[0])}"
                f" · 跳过 {esc(query.get('skipped', ['0'])[0])} · 失败 {esc(query.get('failed', ['0'])[0])}。</div>"
            )
        elif notice_code == "kb_not_configured":
            notice = "<div class='notice'>知识库同步尚未配置（需本机变量 DIFY_KB_DATASET_ID 与 DIFY_KB_API_KEY）。</div>"
        guides = {
            "whatsapp": whatsapp_guide_text(),
            "langbot": "试验通道（本机）：LangBot 负责对接各社交平台并把消息推送进来，平台处理后的回复经 LangBot 发送接口直达买方。步骤：① 本机环境变量设置 LANGBOT_API_KEY（LangBot 面板「API Keys」生成）② 在 LangBot 面板把上方收信地址登记为 Webhook 并启用 ③ 回到这里填写 bot_uuid 并启用渠道。",
            "whatsapp_personal": "本机桥（WAHA）：把你的 WhatsApp 个人号接入平台。步骤：① 本机 WAHA 桥已就绪（端口 3100）② 手机 WhatsApp → 设置 → 已连接的设备 → 扫码（二维码由 WAHA 控制台或助手生成）③ 回到这里填写显示名称并启用。之后消息自动进「会话收件箱 → WhatsApp（个人版）」，回复经同一账号发出（含拟人节奏与限速保护）。",
        }
        host = self.headers.get("Host", "127.0.0.1:8137")
        cards = []
        for channel in CHANNEL_KEYS:
            account = accounts.get(channel)
            status = account["status"] if account else "disabled"
            config = json.loads(account["config_json"]) if account else {}
            secret = channel_secret_status(channel)
            enabled = status == "enabled"
            badge = "<span class='badge approved'>已启用</span>" if enabled else "<span class='badge'>已停用</span>"
            if secret["configured"]:
                secret_badge = "<span class='badge approved'>凭证已配置</span>"
            else:
                secret_badge = f"<span class='badge pending_review'>凭证未配置：{esc('、'.join(secret['missing']))}</span>"
            if channel == "langbot":
                field = f"<label>机器人标识 bot_uuid（在 LangBot 面板复制）<input name='bot_uuid' value='{esc(str(config.get('bot_uuid', '')))}' placeholder='例如 78099cc7-…'></label>"
            else:
                field = f"<label>显示名称（公开显示）<input name='display_name' value='{esc(str(config.get('display_name', '')))}' placeholder='例如 星海泵业'></label>"
            toggle_label = "停用" if enabled else "启用"
            toggle_class = "warn" if enabled else ""
            receive_block = ""
            kb_block = ""
            if channel == "langbot":
                token = str(config.get("webhook_token", ""))
                api_ok = bool(os.environ.get("LANGBOT_API_KEY", "").strip())
                port = host.split(":")[-1] if ":" in host else "8137"
                hook_url = f"http://host.docker.internal:{port}/api/v1/channels/langbot/webhook/{token}" if token else "（保存配置后生成）"
                api_label = "已设置" if api_ok else "未设置（需本机变量 LANGBOT_API_KEY）"
                receive_block = (
                    f"<div class='field-state' style='margin-top:8px'><b>链路状态（试验）</b>：把下面的地址登记到 LangBot「API 集成 › Webhooks」并启用，消息即进入本平台收件箱（本机 LangBot 在 Docker 内，故用 host.docker.internal；其它部署换成可达地址）。<br>"
                    f"收信地址 <code>{esc(hook_url)}</code>；发送 API Key 状态：{esc(api_label)}。<br><b>已支持</b>：自动问答、转人工、卖家回复直达、询价链接。</div>"
                )
                qa_ok = bool(os.environ.get("DIFY_QA_APP_KEY", "").strip())
                kb_ok = bool(os.environ.get("DIFY_KB_DATASET_ID", "").strip() and os.environ.get("DIFY_KB_API_KEY", "").strip())
                synced_docs = self.db.conn.execute(
                    "SELECT COUNT(*) AS n FROM kb_sync_state WHERE tenant_id=? AND document_id <> ''", (self.tenant_id,)
                ).fetchone()["n"]
                last_sync = self.db.conn.execute(
                    "SELECT MAX(synced_at) AS t FROM kb_sync_state WHERE tenant_id=?", (self.tenant_id,)
                ).fetchone()["t"] or "（尚未同步）"
                sync_control = (
                    "<form method='post' action='/admin/channels/sync-dify-kb' style='margin-top:8px'><button class='secondary'>同步已核准资料到知识库</button></form>"
                    if kb_ok
                    else "<p class='field-state'>知识库同步未配置（需本机变量 DIFY_KB_DATASET_ID / DIFY_KB_API_KEY）。</p>"
                )
                kb_block = (
                    f"<div class='field-state' style='margin-top:8px'><b>问答引擎（Dify）</b>：{'已接通' if qa_ok else '未配置（需本机变量 DIFY_QA_APP_KEY）'}；"
                    f"知识库已同步 {synced_docs} 篇 · 最近同步 {esc(last_sync)}。</div>{sync_control}"
                )
            elif channel == "whatsapp":
                public_base = (
                    os.environ.get("WHATSAPP_PUBLIC_BASE", "").strip()
                    or os.environ.get("TWILIO_PUBLIC_BASE", "").strip()
                ).rstrip("/")
                base = public_base or f"http://{host}"
                hook_url = f"{base}/api/v1/channels/whatsapp/webhook"
                if whatsapp_provider() == "meta":
                    receive_block = (
                        f"<div class='field-state' style='margin-top:8px'><b>收发链路（Meta 官方形态）</b>："
                        f"回调地址 <code>{esc(hook_url)}</code>（订阅字段 <code>messages</code>；验证令牌＝本机变量 META_VERIFY_TOKEN）。<br>"
                        f"地址需公网可达（联调时用临时隧道；隧道域名可设本机变量 WHATSAPP_PUBLIC_BASE，本页即显示公网地址）。</div>"
                    )
                else:
                    callback_url = f"{base}/api/v1/channels/whatsapp/status"
                    receive_block = (
                        f"<div class='field-state' style='margin-top:8px'><b>收发链路</b>：把下面两个地址填进 Twilio 的 Sandbox 设置——"
                        f"「WHEN A MESSAGE COMES IN」<code>{esc(hook_url)}</code>；"
                        f"「STATUS CALLBACK URL」<code>{esc(callback_url)}</code>。<br>"
                        f"地址需公网可达（联调时用临时隧道）；隧道域名可设本机变量 TWILIO_PUBLIC_BASE，本页即显示公网地址。</div>"
                    )
            elif channel == "whatsapp_personal":
                receive_block = (
                    f"<div class='field-state' style='margin-top:8px'><b>桥接状态</b>：本机 WAHA 桥（端口 3100）→ 平台收信 "
                    f"<code>http://127.0.0.1:8137/api/v1/channels/whatsapp-personal/webhook/•••</code>（URL 令牌即凭据，已配置、不回显）。<br>"
                    f"扫码与连接状态：<a href='http://127.0.0.1:3100/dashboard' target='_blank'>WAHA 控制台（127.0.0.1:3100/dashboard）</a>，或让助手生成二维码图片直接扫。</div>"
                )
            cards.append(f"""<section class='card' style='margin-top:16px'>
<div class='panel-title'><h2>{esc(CHANNEL_LABELS[channel])} {badge} {secret_badge}</h2></div>
<form method='post' action='/admin/channels/{channel}'>
{field}
<p style='margin-top:10px'><button class='secondary'>保存配置</button></p>
</form>
<form method='post' action='/admin/channels/{channel}/toggle' style='margin-top:8px'>
<p style='margin:0'><button class='{toggle_class}'>{toggle_label}渠道</button></p>
</form>
{receive_block}
{kb_block}
<p class='guide'>{esc(guides[channel])}</p>
<p class='field-state'>机密凭证只从本机环境变量读取：页面不显示、数据库不保存、不进 Git；启用/停用与配置变更全部写入操作记录。</p>
</section>""")
        body = f"""
<div class='toolbar'><div><h1>渠道接入</h1><div class='muted'>把社交平台（经 LangBot）与 WhatsApp 变成买方入口：消息进「会话收件箱」，回复从平台发出。</div></div></div>
{notice}
<section class='card'><div class='panel-title'><h2>如何工作</h2></div>
<p class='muted' style='margin:0'>启用并配置凭证后：买方在渠道里的消息会进入「会话收件箱」，与网站聊天走同一批处理（需求确认、有据问答、人工接管、RFQ 填写链接）；卖家在平台里的回复会经渠道直达买方。对客入口已统一为 LangBot 单轨（试验，资料问答由本机 Dify 引擎承担；旧 Telegram 直连代码已按计划撤除）；WhatsApp 接入进行中，微信系渠道随后。新渠道默认关闭，启用与凭证变更全部留痕。</p></section>
{''.join(cards)}"""
        self.send_html(self.console_page("渠道接入", body, "channels"))

    def admin_audit(self, query: dict[str, list[str]]) -> None:
        try:
            page = max(int(query.get("page", ["1"])[0]), 1)
        except ValueError:
            page = 1
        entity_type = query.get("type", [""])[0].strip()
        actor = query.get("actor", [""])[0].strip()
        page_size = 50
        total, rows = self.db.list_audit_records(self.tenant_id, page=page, page_size=page_size, entity_type=entity_type, actor=actor)
        page_count = max((total + page_size - 1) // page_size, 1)

        def select_options(values: list[str], selected: str, empty_label: str, label_map: dict[str, str] | None = None) -> str:
            options = f"<option value=''>{esc(empty_label)}</option>"
            for value in values:
                label = (label_map or {}).get(value, value)
                options += f"<option value='{esc(value)}'{' selected' if value == selected else ''}>{esc(label)}</option>"
            return options

        entity_types, actors = self.db.audit_filter_options(self.tenant_id)
        type_options = select_options(entity_types, entity_type, "全部对象", AUDIT_ENTITY_LABELS)
        actor_options = select_options(actors, actor, "全部操作者")
        items = []
        for row in rows:
            type_label = AUDIT_ENTITY_LABELS.get(row["entity_type"], row["entity_type"])
            action_label = AUDIT_ACTION_LABELS.get(row["action"], row["action"])
            details = row["details"] or ""
            if len(details) > 90:
                details_html = f"<details class='detail-toggle'><summary>{esc(details[:90])}…</summary><div class='field-state'>{esc(details)}</div></details>"
            elif details:
                details_html = f"<small>{esc(details)}</small>"
            else:
                details_html = ""
            items.append(
                f"<div class='simple-item'><div><strong>{esc(action_label)} · {esc(type_label)} <code class='chip-key'>{esc(row['entity_id'][:14])}</code></strong>"
                f"<small>{esc(row['created_at'])} · 操作者：{esc(row['actor'])}</small>{details_html}</div></div>"
            )
        items_html = "".join(items) or "<div class='muted-block'>暂无操作记录。</div>"
        query_parts = []
        if entity_type:
            query_parts.append("type=" + quote(entity_type))
        if actor:
            query_parts.append("actor=" + quote(actor))

        def page_link(target: int) -> str:
            return "/admin/audit?" + "&".join([*query_parts, f"page={target}"])

        pager = "<div class='pager'>"
        if page > 1:
            pager += f"<a class='button secondary' href='{page_link(page - 1)}'>← 上一页</a>"
        pager += f"<span class='muted'>第 {page} / {page_count} 页 · 共 {total} 条</span>"
        if page < page_count:
            pager += f"<a class='button secondary' href='{page_link(page + 1)}'>下一页 →</a>"
        pager += "</div>"
        body = f"""
<div class='toolbar'><div><h1>操作记录</h1><div class='muted'>本企业关键操作的审计轨迹（时间 / 操作者 / 对象 / 动作）；只读，按最新在前。</div></div></div>
<section class='card'>
<form method='get' action='/admin/audit' class='filter-bar'><label>对象</label><select name='type'>{type_options}</select><label>操作者</label><select name='actor'>{actor_options}</select><button class='secondary'>筛选</button></form>
<div class='simple-list'>{items_html}</div>
{pager}
</section>
"""
        self.send_html(self.console_page("操作记录", body, "audit"), 200)

    def material_assignee_options(self) -> str:
        options = ["<option value=''>未分派（先登记）</option>"]
        for user in self.db.list_users(self.tenant_id):
            if user["active"]:
                options.append(f"<option value='{esc(user['email'])}'>{esc(user['display_name'])} · {esc(user['email'])}</option>")
        return "".join(options)

    def admin_analytics(self, query: dict[str, list[str]]) -> None:
        try:
            days = int(query.get("days", ["0"])[0])
        except ValueError:
            days = 0
        if days not in (0, 7, 30, 90):
            days = 0
        exclude_demo = query.get("exclude_demo", [""])[0] == "1"
        report = self.db.analytics_snapshot(self.tenant_id, days=days, exclude_demo=exclude_demo)
        notice = query.get("notice", [""])[0]
        notice_html = {
            "material_saved": "<div class='notice'>资料补充任务已登记（见下方「资料补充任务」）。</div>",
            "material_done": "<div class='notice'>资料补充任务已完成。</div>",
            "material_invalid": "<div class='error'>任务未登记：请检查产品与接收人是否有效。</div>",
            "material_duplicate": "<div class='error'>该问题已登记过资料补充任务，无需重复登记。</div>",
        }.get(notice, "")
        range_labels = {0: "全部时间", 7: "近 7 天", 30: "近 30 天", 90: "近 90 天"}
        range_options = "".join(
            f"<option value='{value}'{' selected' if days == value else ''}>{label}</option>"
            for value, label in range_labels.items()
        )
        conversations = report["conversations"]
        rfqs = report["rfqs"]
        leads = report["leads"]
        answers = report["answers"]
        tasks = report["tasks"]
        events = report["events"]
        gaps = report["gaps"]
        material = report["material_tasks"]
        assignee_options = self.material_assignee_options()

        def toggle(title: str, total: int, rows_html: str) -> str:
            note = f"共 {total} 条，显示前 50 条" if total > 50 else f"共 {total} 条"
            return f"<details class='detail-toggle'><summary>{esc(title)}（{note}）</summary><div class='simple-list'>{rows_html}</div></details>"

        conv_rows = "".join(
            f"<div class='simple-item'><div><strong>{esc(row['id'][:12])}</strong>"
            f"<small>{esc(row['created_at'])} · 来源：{esc(row['source'])} · 语言：{esc(row['locale'])}</small></div></div>"
            for row in conversations["rows"][:50]
        ) or "<div class='muted-block'>无会话。</div>"
        by_source_html = "".join(f"<span>来源 {esc(source)}：{count}</span>" for source, count in conversations["by_source"]) or "<span>无会话</span>"

        rfq_rows = "".join(
            f"<div class='simple-item'><div><strong>{esc(row['id'][:12])} · {esc(row['model'] or row['requested_model'] or '未指定型号')}</strong>"
            f"<small>{esc(row['created_at'])} · {esc(row['status'])} · 入口：{esc(row['entry_surface'])} · 线索阶段：{esc(row['stage_label'])}"
            + (f" · 还缺：{esc('、'.join(row['missing']))}" if row["missing"] else "")
            + "</small></div></div>"
            for row in rfqs["rows"][:50]
        ) or "<div class='muted-block'>暂无 RFQ。</div>"
        entry_html = "".join(f"<span>入口 {esc(entry)}：{count}</span>" for entry, count in rfqs["by_entry"]) or "<span>无数据</span>"
        missing_html = "".join(f"<span>{esc(label)}：{count}</span>" for label, count in rfqs["missing_top"]) or "<span>无缺项</span>"

        lead_rows = "".join(
            f"<div class='simple-item'><div><strong>{esc(row['rfq_id'][:12])} · {esc(row['stage_label'])}</strong>"
            f"<small>负责人：{esc(row['owner'] or '未分配')} · 跟进：{esc(row['next_follow_up'] or '未安排')}"
            + (" · 逾期" if row["overdue"] else "")
            + (" · 已停止主动联系" if row["stop"] else "")
            + "</small></div></div>"
            for row in leads["rows"][:50]
        ) or "<div class='muted-block'>暂无线索。</div>"
        stage_html = "".join(f"<span>{esc(label)}：{count}</span>" for key, label, count in leads["by_stage"])

        task_status_html = "".join(f"<span>任务状态 {esc(status)}：{count}</span>" for status, count in tasks["by_status"]) or "<span>暂无人工任务</span>"
        rec_status_html = "".join(f"<span>推荐状态 {esc(status)}：{count}</span>" for status, count in report["recommendations"]["by_status"]) or "<span>暂无推荐</span>"

        gap_parts = []
        for row in gaps["rows"][:50]:
            if row["material_task_id"]:
                actions = "<span class='badge'>已登记任务</span>"
            elif row["product_id"]:
                actions = (
                    "<form method='post' action='/admin/material-tasks' class='inline'>"
                    f"<input type='hidden' name='source_ref' value='{esc(row['answer_id'])}'>"
                    f"<input type='hidden' name='product_id' value='{esc(row['product_id'])}'>"
                    f"<select name='assignee'>{assignee_options}</select>"
                    "<button class='secondary'>登记资料补充任务</button></form>"
                )
            else:
                actions = "<span class='field-state'>未关联产品，无法登记修订任务</span>"
            gap_parts.append(
                f"<div class='simple-item'><div><strong>{esc(row['question'][:80])}</strong>"
                f"<small>{esc(row['model'] or '未关联产品')} · {esc(row['created_at'])} · 人工任务：{esc(row['task_status'] or '无')}</small></div>"
                f"<div class='actions'>{actions}</div></div>"
            )
        gap_total = len(gaps["rows"])
        gap_note = f"<p class='field-state'>未解问题共 {gap_total} 条，显示前 50 条。</p>" if gap_total > 50 else ""
        gap_list_html = "".join(gap_parts) or "<div class='muted-block'>暂无未解问题。</div>"
        gap_products_html = "".join(f"<span>{esc(model)}：{count}</span>" for model, count in gaps["by_product"]) or "<span>无</span>"
        top_q_html = "".join(f"<span>{esc(model)}：{count}</span>" for model, count in report["top_questions"]) or "<span>无</span>"

        event_rows = "".join(
            f"<div class='simple-item'><div><strong>{esc({'page_view': '浏览', 'download': '下载'}.get(row['event'], row['event']))} · {esc(row['model'] or row['product_id'][:10])}</strong>"
            f"<small>{esc(row['created_at'])} · {esc(row['detail'])}</small></div></div>"
            for row in events["rows"][:50]
        ) or "<div class='muted-block'>暂无事件。</div>"
        event_products_html = "".join(
            f"<tr><td>{esc(model)}</td><td>{views}</td><td>{downloads}</td></tr>" for model, views, downloads in events["by_product"]
        ) or "<tr><td colspan='3' class='muted'>无数据</td></tr>"

        open_tasks = "".join(
            f"<div class='simple-item'><div><strong>{esc(task['title'])}</strong>"
            f"<small>{esc(task['model'] or '')} · 来源问题：{esc((task['source_question'] or '—')[:60])} · 接收人：{esc(task['assignee'] or '待分派')} · {esc(task['created_at'])}</small></div>"
            f"<div class='actions'><form method='post' action='/admin/material-tasks/{esc(task['id'])}/complete' class='inline'><button class='secondary'>标记完成</button></form></div></div>"
            for task in material["open"]
        ) or "<div class='muted-block'>暂无进行中的资料补充任务。</div>"
        done_tasks = "".join(
            f"<div class='simple-item'><div><strong>{esc(task['title'])}</strong><small>{esc(task['completed_at'] or '')} · 完成人：{esc(task['completed_by'] or '')}</small></div></div>"
            for task in material["done"][:20]
        ) or "<div class='muted-block'>暂无已完成记录。</div>"

        body = f"""
<div class='toolbar'><div><h1>分析报表</h1><div class='muted'>口径：{range_labels[days]}{'；已排除演示夹具数据' if exclude_demo else '；包含全部数据'}。指标只统计实际发生的事件，不自动推断成交贡献。</div></div>
<div><a class='button secondary' href='/admin/export/rfqs.csv'>导出 RFQ</a> <a class='button secondary' href='/admin/export/leads.csv'>导出线索</a> <a class='button secondary' href='/admin/export/products.csv'>导出产品</a> <span class='field-state'>（CSV 全量；导出记录操作者与时间）</span></div></div>
{notice_html}
<form method='get' action='/admin/analytics' class='filter-bar'><label>时间范围</label><select name='days'>{range_options}</select><label class='check-inline'><input type='checkbox' name='exclude_demo' value='1' {'checked' if exclude_demo else ''}>排除演示数据（会话来源 demo-fixture）</label><button class='secondary'>应用</button></form>
<section class='card'><div class='panel-title'><h2>RFQ 漏斗（明确区分，不推断成交）</h2></div>
<div class='compact-facts'><div><small>已提交 RFQ（含待补充）</small><strong>{rfqs['total']}</strong></div><div><small>可报价（线索阶段「{esc(report['quote_ready_label'])}」）</small><strong>{report['quote_ready']}</strong></div><div><small>已成交</small><strong>未接入订单数据</strong></div></div>
<p class='field-state'>「已成交」需要平台接入真实订单数据；当前不统计、不显示推断成交率。RFQ 完整性见下方板块。</p>
{toggle('已提交 RFQ 明细', rfqs['total'], rfq_rows)}
</section>
<section class='card'><div class='panel-title'><h2>入口渠道</h2></div>
<div class='summary-strip'>{by_source_html}</div>
<div class='summary-strip'><span class='muted'>RFQ 入口面：</span>{entry_html}</div>
{toggle('会话明细', conversations['total'], conv_rows)}
</section>
<section class='card'><div class='panel-title'><h2>需求确认与推荐</h2></div>
<div class='compact-facts'><div><small>已确认工况修订</small><strong>{report['requirements']['confirmed']}</strong></div><div><small>候选修订（未确认）</small><strong>{report['requirements']['candidate']}</strong></div><div><small>推荐次数</small><strong>{report['recommendations']['total']}</strong></div></div>
<div class='summary-strip'>{rec_status_html}</div>
</section>
<section class='card'><div class='panel-title'><h2>问答与人工升级</h2></div>
<div class='compact-facts'><div><small>有据回答</small><strong>{answers['grounded']}</strong></div><div><small>待核实（资料外）</small><strong>{answers['needs_human']}</strong></div><div><small>人工任务</small><strong>{tasks['total']}</strong></div></div>
<div class='summary-strip'>{task_status_html}</div>
</section>
<section class='card'><div class='panel-title'><h2>RFQ 完整性</h2></div>
<div class='compact-facts'><div><small>字段完整</small><strong>{rfqs['submitted']}</strong></div><div><small>待补充（缺项）</small><strong>{rfqs['needs_info']}</strong></div></div>
<div class='summary-strip'><span class='muted'>缺项 Top：</span>{missing_html}</div>
</section>
<section class='card'><div class='panel-title'><h2>跟进时效</h2></div>
<div class='compact-facts'><div><small>在跟线索</small><strong>{leads['active']}</strong></div><div><small>逾期</small><strong>{leads['overdue']}</strong></div><div><small>今日到期</small><strong>{leads['due_today']}</strong></div></div>
<div class='summary-strip'>{stage_html}</div>
{toggle('线索明细', leads['total'], lead_rows)}
</section>
<section class='card' id='gaps'><div class='panel-title'><h2>资料缺口与高频问题</h2></div>
<p class='muted'>未解问题 = 资料不足以回答、已转待核实的问题（按产品聚合）；「提问最多的产品」按全部提问统计。可将缺口登记为「资料补充任务」并分派给成员（完成动作在下方板块）。</p>
<div class='summary-strip'><span class='muted'>未解问题按产品：</span>{gap_products_html}</div>
<div class='summary-strip'><span class='muted'>提问最多的产品：</span>{top_q_html}</div>
{gap_list_html}{gap_note}
</section>
<section class='card'><div class='panel-title'><h2>产品页浏览与下载（复盘）</h2></div>
<div class='compact-facts'><div><small>产品页浏览</small><strong>{events['page_view']}</strong></div><div><small>资料下载 / 预览</small><strong>{events['download']}</strong></div></div>
<table><thead><tr><th>产品</th><th>浏览</th><th>下载</th></tr></thead><tbody>{event_products_html}</tbody></table>
{toggle('事件明细', events['total'], event_rows)}
<p class='field-state'>页面浏览为匿名事件（访客无会话身份时不带会话键）；问答、RFQ 与跟进均带会话/线索关联键，可在各自明细中追溯。事件不受「排除演示数据」开关影响。</p>
</section>
<section class='card' id='material-tasks'><div class='panel-title'><h2>资料补充任务</h2><span class='field-state'>进行中 {len(material['open'])} 项</span></div>
{open_tasks}
<details class='detail-toggle'><summary>已完成任务</summary><div class='simple-list'>{done_tasks}</div></details>
</section>
"""
        self.send_html(self.console_page("分析报表", body, "analytics"), 200)

    def admin_export(self, dataset: str) -> None:
        try:
            headers, rows = self.db.export_rows(self.tenant_id, dataset)
        except ValueError:
            self.send_html(self.console_page("导出不可用", "<div class='error'>不支持的数据集。</div>", "analytics"), 404)
            return
        buffer = io.StringIO()
        writer = csv.writer(buffer)
        writer.writerow(headers)
        writer.writerows(rows)
        data = ("\ufeff" + buffer.getvalue()).encode("utf-8")
        stamp = datetime.now(timezone.utc).date().isoformat()
        self.db._audit(self.tenant_id, "export", dataset, "exported", self.actor, f"rows={len(rows)};format=csv")
        self.db.conn.commit()
        self.send_bytes(data, "text/csv; charset=utf-8", f"growave-{dataset}-{stamp}.csv", inline=False)

    def admin_material_task_create(self, form: dict[str, str | bytes]) -> None:
        product_id = str(form.get("product_id", "")).strip()
        source_ref = str(form.get("source_ref", "")).strip()
        assignee = str(form.get("assignee", "")).strip()
        try:
            self.db.create_material_task(self.tenant_id, product_id, source_ref, assignee, self.actor)
        except ValueError as error:
            message = str(error)
            notice = "material_duplicate" if "已登记" in message else "material_invalid"
            self.redirect(f"/admin/analytics?notice={notice}#gaps")
            return
        self.redirect("/admin/analytics?notice=material_saved#material-tasks")

    def admin_material_task_complete(self, task_id: str) -> None:
        try:
            self.db.complete_material_task(self.tenant_id, task_id, self.actor)
        except ValueError:
            self.redirect("/admin/analytics?notice=material_invalid#material-tasks")
            return
        self.redirect("/admin/analytics?notice=material_done#material-tasks")

    def admin_creative(self, query: dict[str, list[str]]) -> None:
        """V1-03 创意中心 · 产品展示页首页：产品视角列表（每类产品一份）+ 选产品创建。"""
        data = self.db.creative_home_rows(self.tenant_id)
        rows, uncategorized, total_products = data["rows"], data["uncategorized"], data["total_products"]
        has_template = {row["category"]: bool(row["template"]) for row in rows}
        selectable = sorted(
            (product for row in rows for product in row["products"]),
            key=lambda product: product["model"],
        )
        items = []
        for row in rows:
            template = row["template"]
            category = esc(row["category"])
            covered = " · ".join(
                f"<a href='/admin/products/{esc(product['id'])}'>{esc(product['model'])}</a>"
                for product in row["products"]
            ) or "（暂无产品）"
            if template:
                badge = template_status_badges(self.db, template)
                updated = (template["draft_updated_at"] or template["created_at"])[:16].replace("T", " ")
                meta = f"更新：{esc(template['draft_updated_by'] or template['created_by'])} · {esc(updated)}"
                actions = (
                    f"<a class='button secondary' href='/admin/creative/{esc(template['id'])}/preview'>看效果</a>"
                    f"<a class='button secondary' href='/admin/creative/{esc(template['id'])}'>继续编辑</a>"
                )
            else:
                badge = "<span class='badge'>尚未设置</span>"
                meta = "买家目前看到平台标准样式"
                actions = f"<button class='secondary' type='button' data-start-cat='{category}'>开始设置</button>"
            items.append(
                f"<div class='simple-item'><div><strong>{category} · 产品展示页 {badge}</strong>"
                f"<small>覆盖产品（{len(row['products'])}）：{covered}</small><small>{meta}</small></div>"
                f"<div class='actions'>{actions}</div></div>"
            )
        if rows:
            footnote = (
                f"<p class='guide'>另有 {uncategorized} 个产品还没有已核准的品类，未列入以上分组——请先在产品管理里核准品类。</p>"
                if uncategorized
                else ""
            )
            section = (
                f"<section class='card'><div class='panel-title'><h2>产品 × 展示页</h2>"
                f"<span class='muted'>每类产品一份 · 同类下所有产品共用</span></div>"
                f"<div class='simple-list'>{''.join(items)}</div>{footnote}</section>"
            )
        else:
            hint = (
                "还没有可制作展示页的产品。先去 <a href='/admin/products'>产品管理</a> 添加产品，再回来为每一类产品做一份展示页。"
                if total_products == 0
                else f"现有的 {total_products} 个产品都没有已核准的品类——先到 <a href='/admin/products'>产品管理</a> 里为产品核准品类（品类决定展示页分组），再回来制作。"
            )
            section = f"<section class='card'><div class='panel-title'><h2>产品 × 展示页</h2></div><div class='muted-block'>{hint}</div></section>"
        create_html = ""
        if selectable:
            options = "".join(
                f"<option value='{esc(product['id'])}' data-cat='{esc((product['category'] or '').strip())}' data-has='{'1' if has_template.get((product['category'] or '').strip()) else '0'}'>"
                f"{esc(product['model'])} · {esc(product['name'])}（{esc((product['category'] or '').strip())}）</option>"
                for product in selectable
            )
            create_html = f"""
<div class='newcard' id='newCard'>
<span class='step-t'>新建展示页 · 第一步：选择产品</span>
<form method='post' action='/admin/creative/create'><div class='line'>
<select name='product_id' id='creative-product'>{options}</select>
<label class='muted'>起点 <select name='start'><option value='standard'>标准模板（推荐）</option><option value='blank'>空白创建</option></select></label>
<button>继续 →</button>
</div>
<p class='guide' id='creative-hint'></p>
<p class='guide'>只能为本企业产品库中的产品建页；产品列表里没有你要的产品？先去「产品经营 › 产品管理」添加。</p>
</form>
</div>
"""
        toolbar_action = "<button id='creative-new' type='button'>＋ 新建展示页</button>" if selectable else ""
        body = f"""
<div class='toolbar'><div><h1>产品展示页</h1><div class='muted'>买家点开产品后看到的页面，都在这里管理。改完发布后，买家才会看到新版本。</div></div>
<div class='actions'>{toolbar_action}</div></div>
{creative_flowbar("home")}
<div class='coach'>第一次设置？每一类产品做一份：选参数 → 排内容 → 看效果，大约 5 分钟。</div>
{create_html}
{section}
<script>{CREATIVE_HOME_JS}</script>
"""
        self.send_html(self.console_page("产品展示页", body, "creative"))

    def admin_creative_editor(self, template_id: str, query: dict[str, list[str]]) -> None:
        """V1-03 展示页编辑区分步骨架（模块条 + 当前步骤占位；编辑器随后续切片上线）。"""
        template = self.db.get_template(self.tenant_id, template_id)
        if not template:
            self.send_html(self.console_page("未找到", "<div class='error'>展示页不存在</div>", "creative"), 404)
            return
        step = query.get("step", ["params"])[0]
        if step not in {key for key, _label in CREATIVE_STEPS}:
            step = "params"
        badge = template_status_badges(self.db, template)
        if template["status"] == "withdrawn":
            line = "已停止新引用；重新确认上线后，产品重新发布时会再次应用这一版。"
        elif template["published_version"]:
            line = f"线上版本 v{template['published_version']}；改动只在确认上线并由产品重新发布后才对买家生效。"
        else:
            line = "这个展示页还没有上线；买家目前看到平台标准样式。"
        notices = {
            "created": "<div class='notice'>展示页已创建。建议顺序：选参数 → 排内容 → 看效果。</div>",
            "existing": "<div class='notice'>该品类已有展示页，已直接打开现有页。</div>",
            "submitted": "<div class='notice'>已提交给管理员，等待确认上线。</div>",
            "restored": "<div class='notice'>已恢复为标准默认设置——参数与板块都回到推荐组合；已上线的版本不受影响。</div>",
        }
        notice_key = query.get("notice", [""])[0]
        notice_html = notices.get(notice_key, "")
        if notice_key == "published":
            notice_html = f"<div class='notice'>已确认上线 v{esc(query.get('v', [''])[0])}。到对应产品上「重新发布」后，买家就能看到这一版。</div>"
        elif notice_key == "withdrawn":
            notice_html = "<div class='notice'>已撤回：之后重新发布的产品会回到平台标准样式；已发布产品维持之前发布时的版本。</div>"
        saved_line = ""
        if template["draft_updated_at"]:
            saved_line = f" · 已自动保存 {template['draft_updated_at'][5:16].replace('T', ' ')}"
        if step == "params":
            content = creative_params_step(template)
        elif step == "blocks":
            content = creative_blocks_step(template, self.db.list_template_versions(self.tenant_id, template_id))
        elif step == "preview":
            content = self.creative_preview_step(template_id, template)
        else:
            content = self.creative_publish_step(template)
        audit_link = "<a class='button secondary' href='/admin/audit?type=template'>操作记录</a>" if self.user_role == "admin" else ""
        body = f"""
<div class='toolbar'><div><h1>{esc(template['category'])} · 产品展示页 {badge}</h1>
<div class='muted'>{line}{saved_line}</div></div><div class='actions'>{audit_link}</div></div>
{notice_html}
{creative_flowbar(step, template_id=template_id)}
{content}
<script>{CREATIVE_PARAMS_JS}</script>
"""
        self.send_html(self.console_page("产品展示页", body, "creative"))

    def creative_preview_step(self, template_id: str, template: sqlite3.Row) -> str:
        """「看效果」步骤：试看产品与语言选择 + 打开买家视角预览。"""
        published = [
            item for item in self.db.list_products(self.tenant_id)
            if item["status"] != "withdrawn" and self.db.get_published_version(self.tenant_id, item["id"])
        ]
        preferred = [item for item in published if self.db.get_approved_category(self.tenant_id, item["id"]) == template["category"]]
        ordered = preferred + [item for item in published if item not in preferred]
        head = """
<div class='panel-title'><h2>看效果 · 买家视角</h2><span class='muted'>用「未发布」的内容 + 已确认的资料渲染</span></div>"""
        if ordered:
            options = "".join(
                f"<option value='{esc(item['id'])}'>{esc(item['model'])} · {esc(item['name'])}</option>" for item in ordered
            )
            content = f"""<section class='card'>{head}
<p class='muted' style='margin:0 0 10px'>预览只有你自己看得到：不对外、不产生任何数据。选一个已发布的产品试看效果；满意就到「产品页联动」去发布。</p>
<form class='step-form' method='get' action='/admin/creative/{esc(template_id)}/preview'>
<label>试看产品 <select name='product_id'>{options}</select></label>
<label>语言 <select name='lang'><option value='zh'>中文</option><option value='en'>English</option></select></label>
<button>打开买家视角预览 →</button>
</form></section>"""
        else:
            content = f"""<section class='card'>{head}
<div class='muted-block'>还没有已发布的产品可以试看——先到「产品管理」完成核对并发布一个产品，再回来预览。</div></section>"""
        return content + f"""
<div class='step-cta'><a class='button' href='/admin/creative/{esc(template_id)}?step=publish'>满意，去应用 →</a></div>"""

    def admin_creative_preview(self, template_id: str, query: dict[str, list[str]]) -> None:
        """V1-03：买家视角预览——草稿内容 + 已核准资料渲染；不对外、不产生公开数据。"""
        template = self.db.get_template(self.tenant_id, template_id)
        if not template:
            self.send_html(self.console_page("未找到", "<div class='error'>展示页不存在</div>", "creative"), 404)
            return
        published = [
            item for item in self.db.list_products(self.tenant_id)
            if item["status"] != "withdrawn" and self.db.get_published_version(self.tenant_id, item["id"])
        ]
        product_id = query.get("product_id", [""])[0].strip()
        product = None
        if product_id:
            product = next((item for item in published if item["id"] == product_id), None)
            if not product:
                if self.db.get_product(self.tenant_id, product_id):
                    self.send_html(self.console_page("预览不可用", "<div class='error'>该产品还没有已发布版本；请先完成核对并发布，或改选其他产品。</div>", "creative"), 400)
                else:
                    self.send_html(self.console_page("预览不可用", "<div class='error'>请选择本企业的已发布产品进行试看。</div>", "creative"), 400)
                return
        else:
            preferred = [item for item in published if self.db.get_approved_category(self.tenant_id, item["id"]) == template["category"]]
            product = (preferred or published or [None])[0]
        if not product:
            self.send_html(self.console_page("预览不可用", "<div class='error'>还没有已发布的产品可以试看——先在「产品管理」完成核对并发布一个产品。</div>", "creative"), 400)
            return
        version = self.db.get_published_version(self.tenant_id, product["id"])
        lang = query.get("lang", ["zh"])[0]
        if lang not in ("zh", "en"):
            lang = "zh"
        draft = json.loads(template["draft_json"])
        page = self.render_product_page(
            self.tenant_id,
            product,
            version,
            {"lang": [lang]},
            None,
            blocks=draft.get("blocks") if isinstance(draft.get("blocks"), list) else None,
            params=draft.get("params") if isinstance(draft.get("params"), list) else None,
            preview=True,
        )
        options = "".join(
            f"<option value='{esc(item['id'])}'{' selected' if item['id'] == product['id'] else ''}>{esc(item['model'])} · {esc(item['name'])}</option>"
            for item in published
        )
        toolbar = f"""<style>.tpl-preview-bar{{position:sticky;top:0;z-index:60;background:#FFF7DF;border-bottom:1px solid #EBD98A;color:#6B5407;padding:9px 16px;display:flex;gap:12px;align-items:center;flex-wrap:wrap;font:600 12.5px/1.5 "Inter","PingFang SC","Microsoft YaHei",system-ui,sans-serif}}.tpl-preview-bar .grow{{flex:1}}.tpl-preview-bar select{{border:1px solid #EBD98A;background:#fff;border-radius:8px;padding:5px 8px;font:inherit;font-weight:500;color:#6B5407;max-width:280px}}.tpl-preview-bar a{{color:#6B5407;text-decoration:underline;text-underline-offset:2px}}</style>
<div class="tpl-preview-bar"><span>👀 预览 · 未发布 —— 买家还看不到，也不产生任何数据</span><span class="grow"></span>
<form method="get" action="/admin/creative/{esc(template_id)}/preview">
<select name="product_id">{options}</select>
<select name="lang"><option value="zh"{' selected' if lang == 'zh' else ''}>中文</option><option value="en"{' selected' if lang == 'en' else ''}>English</option></select>
</form>
<a href="/admin/creative/{esc(template_id)}?step=preview">返回编辑区</a>
<a href="/admin/creative/{esc(template_id)}?step=publish">去应用 →</a>
</div>
<script>document.querySelectorAll('.tpl-preview-bar select').forEach(function (sel) {{ sel.addEventListener('change', function () {{ sel.form.submit(); }}); }});</script>"""
        body_tag = page.find("<body")
        if body_tag != -1:
            insert_at = page.find(">", body_tag) + 1
            page = page[:insert_at] + toolbar + page[insert_at:]
        else:
            page = toolbar + page
        self.send_html(page, 200)

    def creative_publish_step(self, template: sqlite3.Row) -> str:
        """「产品页联动」步骤：提交/确认上线/撤回 + 关联产品与重新发布提示。"""
        template_id = template["id"]
        is_admin = self.user_role == "admin"
        published_version = template["published_version"] or 0
        is_withdrawn = template["status"] == "withdrawn"
        dirty = self.db.template_has_unpublished_changes(self.tenant_id, template)
        head = """
<div class='panel-title'><h2>产品页联动</h2><span class='muted'>这一版如何发布、哪些产品会用到</span></div>"""
        if is_withdrawn:
            state = "<span class='badge withdrawn'>已撤回</span>"
            state_text = "已停止新引用：之后重新发布的产品会回到平台标准样式；已发布产品维持之前发布时的版本，重新确认上线后可再次应用。"
        elif published_version and not dirty:
            state = f"<span class='badge approved'>已上线 v{published_version}</span>"
            state_text = "当前编辑内容和线上版本一致，买家看到的就是这一版。"
        elif published_version:
            state = f"<span class='badge approved'>已上线 v{published_version}</span> <span class='badge pending_review'>有改动还没发布</span>"
            state_text = "最近有改动还没上线；确认上线并在产品上重新发布后，买家才能看到新版本。"
        else:
            state = "<span class='badge pending_review'>尚未上线</span>"
            state_text = "买家目前看到平台标准样式。确认上线后，再把产品重新发布一次即可应用。"
        if template["status"] == "submitted":
            state += " <span class='badge info'>已提交，待确认</span>"
        versions = self.db.list_template_versions(self.tenant_id, template_id)
        latest = versions[0] if versions else None
        version_line = (
            f"最近上线：v{latest['version']} · {esc(latest['approved_by'] or latest['created_by'])} · {esc((latest['approved_at'] or latest['created_at'] or '')[:16].replace('T', ' '))}"
            if latest else "还没有确认上线的版本——确认上线后会在这里留下版本记录。"
        )
        if is_admin:
            actions = (
                f"<form class='inline' method='post' action='/admin/creative/{esc(template_id)}/publish/confirm'>"
                f"<button>确认上线 · 发布为 v{published_version + 1}</button></form>"
            )
            if published_version and not is_withdrawn:
                actions += (
                    f"<form class='inline' method='post' action='/admin/creative/{esc(template_id)}/publish/withdraw'>"
                    f"<button class='warn'>撤回（停止新引用）</button></form>"
                )
            guide = "你是管理员：确认上线后，还需要到对应产品上「重新发布」，买家才会看到新版本；在此之前线上完全不受影响。"
        else:
            actions = (
                f"<form class='inline' method='post' action='/admin/creative/{esc(template_id)}/publish/submit'>"
                f"<button>提交给管理员</button></form>"
            )
            guide = "你提交后由管理员确认上线；确认后需要到产品上重新发布才会生效。"
        products = [
            item for item in self.db.list_products(self.tenant_id)
            if self.db.get_approved_category(self.tenant_id, item["id"]) == template["category"]
        ]
        rows = []
        for item in products:
            version_row = self.db.get_published_version(self.tenant_id, item["id"])
            ref_version = None
            if version_row:
                snapshot = json.loads(version_row["snapshot_json"])
                ref = snapshot.get("template") if isinstance(snapshot, dict) else None
                if isinstance(ref, dict) and ref.get("template_id") == template_id:
                    ref_version = ref.get("version")
            if not version_row:
                state_cell = "<span class='badge pending_review'>尚未发布</span> 发布该产品时会自动用上最新已上线版本"
            elif is_withdrawn:
                state_cell = (
                    f"<span class='badge withdrawn'>仍沿用 v{ref_version}</span> 撤回只停止新引用" if ref_version
                    else "未引用（展示页已撤回）"
                )
            elif not published_version:
                state_cell = "确认上线后，重新发布该产品即可应用"
            elif ref_version == published_version:
                state_cell = f"<span class='badge approved'>已应用 v{ref_version}</span>"
            else:
                state_cell = "<span class='badge pending_review'>需要「重新发布」才生效</span>"
            rows.append(
                f"<div class='simple-item'><div><strong>{esc(item['model'])} · {esc(item['name'])}</strong>"
                f"<small>{state_cell}</small></div>"
                f"<div class='actions'><a class='button secondary' href='/admin/products/{esc(item['id'])}'>去产品页</a></div></div>"
            )
        products_html = "".join(rows) or "<div class='muted-block'>这个品类下暂时没有产品——先到「产品管理」添加。</div>"
        return f"""<section class='card'>{head}
<p style='margin:0 0 10px'>{state} {state_text}</p>
<p class='muted' style='margin:0 0 14px'>{version_line}</p>
<div class='actions' style='display:flex;gap:10px;flex-wrap:wrap'>{actions}</div>
<p class='guide'>{guide}</p>
<p class='guide'>上线后的版本会被冻结保存：以后继续修改，不会影响买家看到的版本。</p>
</section>
<section class='card' style='margin-top:16px'><div class='panel-title'><h2>会用到这份展示页的产品</h2><span class='muted'>发布或重新发布后自动应用</span></div>
<div class='simple-list'>{products_html}</div></section>"""

    def admin_creative_publish_action(self, template_id: str, action: str) -> None:
        """产品页联动动作：提交给管理员（员工）/ 确认上线、撤回（仅管理员）。"""
        template = self.db.get_template(self.tenant_id, template_id)
        if not template:
            raise ValueError("展示页不存在")
        if action == "submit":
            self.db.submit_template(self.tenant_id, template_id, self.actor)
            self.redirect(f"/admin/creative/{template_id}?step=publish&notice=submitted")
            return
        if self.user_role != "admin":
            self.send_html(self.console_page("无权操作", permission_notice("确认上线或撤回产品展示页", "员工", "管理员", "确认上线或撤回展示页只对管理员开放；员工可以先「提交给管理员」。"), "creative"), 403)
            return
        if action == "confirm":
            version = self.db.publish_template_version(self.tenant_id, template_id, self.actor)
            self.redirect(f"/admin/creative/{template_id}?step=publish&notice=published&v={version}")
            return
        if action == "withdraw":
            if not (template["published_version"] or 0):
                raise ValueError("还没有上线版本，无法撤回")
            self.db.withdraw_template(self.tenant_id, template_id, self.actor)
            self.redirect(f"/admin/creative/{template_id}?step=publish&notice=withdrawn")
            return
        raise ValueError("未知操作")

    def admin_creative_restore_defaults(self, template_id: str) -> None:
        """恢复默认（草稿动作）：参数与板块重置为平台标准推荐；已发布版本不受影响。"""
        self.db.restore_template_defaults(self.tenant_id, template_id, self.actor)
        self.redirect(f"/admin/creative/{template_id}?step=blocks&notice=restored")

    def admin_creative_params_action(self, template_id: str, action: str, form: dict) -> None:
        """选参数改动（自动保存）：推荐池/自定义新增、开关、排序、行内编辑、删除。"""
        template = self.db.get_template(self.tenant_id, template_id)
        if not template:
            raise ValueError("展示页不存在")
        draft = json.loads(template["draft_json"])
        raw_params = draft.get("params")
        params = [p for p in raw_params if isinstance(p, dict)] if isinstance(raw_params, list) else []
        key = str(form.get("param_key", "")).strip()
        index = next((i for i, p in enumerate(params) if str(p.get("key")) == key), -1)
        if action == "add":
            if str(form.get("source", "pool")).strip() == "custom":
                label_zh = str(form.get("label_zh", "")).strip()
                if not label_zh:
                    raise ValueError("请填写参数名称")
                new_key = ""
                while not new_key or any(str(p.get("key")) == new_key for p in params):
                    new_key = f"custom_{secrets.token_hex(4)}"
                params.append({
                    "key": new_key, "label_zh": label_zh[:40],
                    "label_en": str(form.get("label_en", "")).strip()[:60],
                    "unit": str(form.get("unit", "")).strip()[:20], "enabled": True,
                })
            else:
                pool_key = str(form.get("pool_key", "")).strip()
                pool_item = next((item for item in CREATIVE_PARAM_POOL if item["key"] == pool_key), None)
                if not pool_item:
                    raise ValueError("不支持的推荐参数")
                if any(str(p.get("key")) == pool_key for p in params):
                    raise ValueError("这个参数已经在页面上了")
                params.append({
                    "key": pool_item["key"], "label_zh": pool_item["label_zh"],
                    "label_en": pool_item["label_en"], "unit": pool_item["unit"], "enabled": True,
                })
        elif index < 0:
            raise ValueError("参数不存在")
        elif action == "toggle":
            params[index]["enabled"] = not bool(params[index].get("enabled"))
        elif action == "move":
            target = index - 1 if str(form.get("dir", "")) == "up" else index + 1
            if not (0 <= target < len(params)):
                self.redirect(f"/admin/creative/{template_id}?step=params")
                return
            params[index], params[target] = params[target], params[index]
        elif action == "update":
            label_zh = str(form.get("label_zh", "")).strip()
            if not label_zh:
                raise ValueError("请填写参数名称")
            params[index]["label_zh"] = label_zh[:40]
            params[index]["label_en"] = str(form.get("label_en", "")).strip()[:60]
            params[index]["unit"] = str(form.get("unit", "")).strip()[:20]
        elif action == "delete":
            params.pop(index)
        draft["params"] = params
        self.db.save_template_draft(self.tenant_id, template_id, draft, self.actor, detail=f"params.{action}")
        self.redirect(f"/admin/creative/{template_id}?step=params")

    def admin_creative_blocks_action(self, template_id: str, action: str, form: dict) -> None:
        """排内容改动（自动保存）：板块开关/排序/改标题 + 进阶设置（AI 助手偏好 / 分组名）。"""
        template = self.db.get_template(self.tenant_id, template_id)
        if not template:
            raise ValueError("展示页不存在")
        draft = json.loads(template["draft_json"])
        if action == "advanced":
            advanced = draft.get("advanced") if isinstance(draft.get("advanced"), dict) else {}
            ai_prompts = advanced.get("ai_prompts") if isinstance(advanced.get("ai_prompts"), dict) else {}
            ai_prompts["extract"] = str(form.get("ai_extract", "")).strip()[:500]
            ai_prompts["locate"] = str(form.get("ai_locate", "")).strip()[:500]
            advanced["ai_prompts"] = ai_prompts
            draft["advanced"] = advanced
            self.db.save_template_draft(self.tenant_id, template_id, draft, self.actor, detail="blocks.advanced")
            self.redirect(f"/admin/creative/{template_id}?step=blocks")
            return
        if action == "group":
            group_zh = str(form.get("group_zh", "")).strip()[:40] or "规格参数"
            group_en = str(form.get("group_en", "")).strip()[:60]
            draft["groups"] = [{"key": "default", "label_zh": group_zh, "label_en": group_en}]
            self.db.save_template_draft(self.tenant_id, template_id, draft, self.actor, detail="blocks.group")
            self.redirect(f"/admin/creative/{template_id}?step=blocks")
            return
        blocks = resolved_template_blocks(draft)
        key = str(form.get("block_key", "")).strip()
        index = next((i for i, b in enumerate(blocks) if str(b.get("key")) == key), -1)
        if index < 0:
            raise ValueError("板块不存在")
        meta = next((item for item in TEMPLATE_BLOCK_LIBRARY if item["key"] == key), None)
        if action == "toggle":
            if meta and meta["fixed"]:
                raise ValueError("首屏固定显示，不能关闭")
            blocks[index]["enabled"] = not bool(blocks[index].get("enabled"))
        elif action == "move":
            target = index - 1 if str(form.get("dir", "")) == "up" else index + 1
            if not (0 <= target < len(blocks)):
                self.redirect(f"/admin/creative/{template_id}?step=blocks")
                return
            blocks[index], blocks[target] = blocks[target], blocks[index]
        elif action == "update":
            blocks[index]["title_zh"] = str(form.get("title_zh", "")).strip()[:40]
            blocks[index]["title_en"] = str(form.get("title_en", "")).strip()[:60]
        draft["blocks"] = blocks
        self.db.save_template_draft(self.tenant_id, template_id, draft, self.actor, detail=f"blocks.{action}")
        self.redirect(f"/admin/creative/{template_id}?step=blocks")

    def admin_users(self, query: dict[str, list[str]]) -> None:
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
            if user["role"] == "admin":
                type_badge = "<span class='badge'>管理员</span>"
                modules_html = "<small>全模块权限（管理员账号无需逐项授权）</small>"
            else:
                granted = self.db.get_user_modules(user["id"])
                summary = "、".join(MODULE_LABELS[key] for key in granted) or "未授权（无法访问任何模块）"
                checkboxes = "".join(
                    f"<label class='check-inline'><input type='checkbox' name='module_{key}' value='1' {'checked' if key in granted else ''}> {esc(label)}</label>"
                    for key, label in GRANTABLE_MODULES
                )
                type_badge = "<span class='badge'>员工</span>"
                modules_html = (
                    f"<small>模块权限：{esc(summary)}</small>"
                    f"<details class='detail-toggle'><summary>授权模块（审批并开通）</summary>"
                    f"<form method='post' action='/admin/users/{esc(user['id'])}/modules'><div class='check-grid'>{checkboxes}</div>"
                    f"<p><button>保存授权</button></p></form></details>"
                )
            items.append(
                f"<div class='simple-item'><div><strong>{esc(user['display_name'])} {type_badge} "
                f"<span class='badge {'approved' if active else 'withdrawn'}'>{status}</span></strong>"
                f"<small>{esc(user['email'])} · 创建于 {esc(user['created_at'])}</small>{modules_html}</div>"
                f"<div class='actions'>{action_html}</div></div>"
            )
        items_html = "".join(items) or "<div class='muted-block'>暂无成员。</div>"
        notice = {
            "created": "<div class='notice'>成员账号已创建。员工账号默认没有任何模块权限——请在成员列表中执行「授权模块」，保存后该员工才能访问对应功能。</div>",
            "updated": "<div class='notice'>成员账号状态已更新；停用账号的现有会话已失效。</div>",
            "modules": "<div class='notice'>模块授权已保存并立即生效；无需该员工重新登录。</div>",
        }.get(query.get("notice", [""])[0], "")
        body = f"""
<div class='toolbar'><div><h1>成员管理</h1><div class='muted'>账号只有两种：管理员（全模块）与普通员工（按模块授权）；员工新账号默认零权限，须由管理员审批并授权。</div></div><a class='button secondary' href='/admin'>返回管理概览</a></div>
{notice}
<section class='card'><div class='panel-title'><h2>成员列表</h2><span class='muted'>停用后不能登录，历史记录不受影响</span></div><div class='simple-list'>{items_html}</div>
<details class='detail-toggle'><summary>创建成员账号</summary><div><p class='muted'>密码只在创建时使用，不会在后台列表显示。员工账号创建后没有任何模块权限；如需访问，请在上方名单中为其「授权模块」。生产环境应接入正式身份提供商。</p><form method='post' action='/admin/users'><div class='form-grid'><div><label>姓名 *</label><input name='display_name' required placeholder='例如 Alex Chen'></div><div><label>邮箱 *</label><input name='email' type='email' required placeholder='alex@example.com'></div><div><label>账号类型 *</label><select name='role'><option value='employee'>普通员工（默认零权限）</option><option value='admin'>管理员（全模块）</option></select></div><div><label>初始密码 *</label><input name='password' type='password' minlength='8' required placeholder='至少 8 位'></div></div><p><button>创建账号</button></p></form></div></details>
<details class='detail-toggle'><summary>查看账号类型说明</summary><div class='muted-block'><p><b>管理员：</b>全模块权限，另可管理成员、审批并授权员工模块。</p><p><b>普通员工：</b>创建后默认零权限；管理员逐项授权以下模块后才可访问——产品管理 / 人工任务 / RFQ 询价 / 销售工作台 / 网站 API 接入。授权保存后立即生效。</p><p>成员管理只对管理员开放；账号属于当前企业；停用后不能继续登录，也不会改变历史 RFQ、任务或操作记录。</p></div></details></section>
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
        def model_label(row: sqlite3.Row) -> str:
            base = row["model"] or ""
            if row["requested_model"] and row["requested_model"] != base:
                base = f"{base}（买家填写：{row['requested_model']}）" if base else f"买家填写：{row['requested_model']}"
            return base or "未指定型号"
        items = "".join(
            f"<div class='simple-item'><div><strong>{esc(rfq['id'])} · {esc(model_label(rfq))}</strong>"
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
        model_display = rfq["model"] or ""
        if rfq["requested_model"] and rfq["requested_model"] != model_display:
            model_display = f"{model_display}（买家填写：{rfq['requested_model']}）" if model_display else f"买家填写：{rfq['requested_model']}"
        model_display = model_display or "未指定"
        revision_rows = []
        previous_data = None
        diff_labels = (("product_id", "型号"), ("requested_model", "填写型号"), ("contact_email", "邮箱"), ("contact_phone", "电话"), ("quantity", "数量"), ("destination", "目的地"), ("requested_delivery", "期望交期"), ("notes", "说明"))
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
                f"<td>{esc(history_product['model'] if history_product else (data.get('requested_model') or '未指定'))}</td><td>{esc(data['contact_email'] or data['contact_phone'] or '未提供')}</td><td>{esc(data['quantity'] or '待补充')}</td>"
                f"<td>{esc(data['destination'] or '待补充')}</td><td>{esc(data['notes'] or '无')}</td></tr>"
            )
            previous_data = data
        revision_rows = "".join(revision_rows)
        body = f"""
<div class='toolbar'><div><a href='/admin/rfqs'>← 返回 RFQ 列表</a><h1>RFQ {esc(rfq['id'])}</h1><div class='muted'>状态：{esc(rfq['status'])} · 会话：{esc(rfq['conversation_id'])} · {esc(rfq['created_at'])}</div></div></div>
{focus_html}
<div class='grid'><section class='card'><h2>联系人（后台可见）</h2><p><b>姓名：</b>{esc(rfq['contact_name']) or '未提供'}</p><p><b>公司：</b>{esc(rfq['company']) or '未提供'}</p><p><b>邮箱：</b>{esc(rfq['contact_email']) or '未提供'}</p><p><b>电话：</b>{esc(rfq['contact_phone']) or '未提供'}</p></section><section class='card'><h2>询价信息</h2><p><b>型号：</b>{esc(model_display)}</p><p><b>数量：</b>{esc(rfq['quantity']) or '待补充'}</p><p><b>目的地：</b>{esc(rfq['destination']) or '待补充'}</p><p><b>期望交期：</b>{esc(rfq['requested_delivery']) or '待补充'}</p><p><b>补充说明：</b>{esc(rfq['notes']) or '无'}</p></section></div>
<section class='card' style='margin-top:18px'><h2>已确认需求快照</h2><table><thead><tr><th>字段</th><th>值</th><th>状态</th></tr></thead><tbody>{requirement_rows}</tbody></table></section>
<section class='card' style='margin-top:18px'><h2>RFQ 提交与修改记录（仅卖方可见）</h2><table><thead><tr><th>版本</th><th>时间</th><th>修改者</th><th>变更内容（相对上一版）</th><th>型号</th><th>联系</th><th>数量</th><th>目的地</th><th>说明</th></tr></thead><tbody>{revision_rows}</tbody></table></section>
<div class='footer-note'>该 RFQ 保存了提交时的需求、推荐和产品版本快照；后续产品更新不会静默改写这条历史询价。</div>
"""
        self.send_html(self.console_page("RFQ 详情", body, "rfqs"), 200)

    def admin_sales(self, query: dict[str, list[str]]) -> None:
        records = self.db.list_sales_records(self.tenant_id)
        today = datetime.now(timezone.utc).date().isoformat()
        soon_cutoff = (datetime.now(timezone.utc).date() + timedelta(days=3)).isoformat()
        stages = self.db.get_lead_stages(self.tenant_id)
        stage_labels = {stage["key"]: stage["label"] for stage in stages}
        terminal_keys = {stage["key"] for stage in stages if stage["terminal"]}

        def follow_up_state(record) -> str:
            if record["stop_proactive_contact"] or record["stage"] in terminal_keys:
                return "inactive"
            value = record["next_follow_up"] or ""
            if not value:
                return "unset"
            if value < today:
                return "overdue"
            if value == today:
                return "today"
            if value <= soon_cutoff:
                return "soon"
            return "later"

        enriched = [(record, follow_up_state(record)) for record in records]
        counts = {
            "overdue": sum(1 for _record, state in enriched if state == "overdue"),
            "today": sum(1 for _record, state in enriched if state == "today"),
            "soon": sum(1 for _record, state in enriched if state == "soon"),
            "unassigned": sum(1 for record, _state in enriched if not record["owner"]),
        }
        stage_filter = query.get("stage", [""])[0].strip()
        owner_filter = query.get("owner", [""])[0].strip()
        timing_filter = query.get("timing", [""])[0].strip()
        keyword_raw = query.get("q", [""])[0].strip()
        keyword = keyword_raw.lower()

        def matches(record, state: str) -> bool:
            if stage_filter and record["stage"] != stage_filter:
                return False
            if owner_filter == "unassigned" and record["owner"]:
                return False
            if owner_filter and owner_filter != "unassigned" and record["owner"] != owner_filter:
                return False
            if timing_filter and state != timing_filter:
                return False
            if keyword:
                haystack = " ".join(str(record[key] or "") for key in ("id", "model", "company", "contact_email", "owner")).lower()
                if keyword not in haystack:
                    return False
            return True

        visible = [(record, state) for record, state in enriched if matches(record, state)]
        items = []
        for record, state in visible:
            if state == "overdue":
                follow_up = f"<span class='badge withdrawn'>逾期 {esc(record['next_follow_up'])}</span>"
            elif state == "today":
                follow_up = "<span class='badge pending_review'>今日到期</span>"
            elif state == "soon":
                follow_up = f"<span class='badge pending_review'>{esc(record['next_follow_up'])} · 3 天内</span>"
            else:
                follow_up = esc(record["next_follow_up"] or "未安排")
            if record["stop_proactive_contact"]:
                follow_up = "<span class='badge withdrawn'>停止主动联系</span>"
            changed = (f"<span class='badge pending_review'>买方已修改 {record['buyer_revision_count']} 次</span>" if record["buyer_revision_count"] else "")
            items.append(
                f"<div class='simple-item'><div><strong>{esc(record['id'])} · {esc(record['model'] or '未指定型号')} <span class='badge'>{esc(stage_labels.get(record['stage'], record['stage'] or '未分配'))}</span></strong>"
                f"<small>{esc(record['company'] or record['contact_email'] or '未提供联系人')} · 负责人：{esc(record['owner'] or '未分配')} · 跟进：{follow_up} · RFQ：{esc(record['status'])} {changed}</small></div>"
                f"<div class='actions'><a class='button' href='/admin/sales/{record['id']}'>打开线索 →</a></div></div>"
            )
        filtered_note = "" if len(visible) == len(records) else f" · 已筛选 {len(visible)} / {len(records)} 条"
        items_html = "".join(items) or "<div class='muted-block'>没有符合筛选条件的线索。</div>"

        stage_options_html = "<option value=''>全部阶段</option>" + "".join(
            f"<option value='{esc(stage['key'])}'{' selected' if stage_filter == stage['key'] else ''}>{esc(stage['label'])}</option>"
            for stage in stages
        )
        owners = sorted({record["owner"] for record in records if record["owner"]})
        owner_options_html = "<option value=''>全部负责人</option>" + f"<option value='unassigned'{' selected' if owner_filter == 'unassigned' else ''}>未分配</option>" + "".join(
            f"<option value='{esc(owner)}'{' selected' if owner_filter == owner else ''}>{esc(owner)}</option>" for owner in owners
        )
        timing_labels = (("overdue", "逾期"), ("today", "今日到期"), ("soon", "3 天内到期"), ("unset", "未安排"), ("later", "稍后跟进"), ("inactive", "终态/已停止"))
        timing_options_html = "<option value=''>全部时效</option>" + "".join(
            f"<option value='{key}'{' selected' if timing_filter == key else ''}>{label}</option>" for key, label in timing_labels
        )
        reminder_strip = (
            "<div class='summary-strip'><span class='muted'>跟进提醒：</span>"
            f"<a href='/admin/sales?timing=overdue'>逾期 {counts['overdue']}</a>"
            f"<a href='/admin/sales?timing=today'>今日到期 {counts['today']}</a>"
            f"<a href='/admin/sales?timing=soon'>3 天内到期 {counts['soon']}</a>"
            f"<a href='/admin/sales?owner=unassigned'>未分配 {counts['unassigned']}</a></div>"
        )
        nurture_leads = self.db.list_nurture_leads(self.tenant_id)
        nurture_items = []
        for nurture in nurture_leads:
            follow = esc(nurture["next_follow_up"] or "未安排")
            if nurture["next_follow_up"] and nurture["next_follow_up"] < today and nurture["stage"] not in terminal_keys:
                follow = f"<span class='badge withdrawn'>逾期 {esc(nurture['next_follow_up'])}</span>"
            contact_bits = esc(nurture["contact_email"] or nurture["contact_phone"] or "")
            company_bit = f" · {esc(nurture['company'])}" if nurture["company"] else ""
            nurture_items.append(
                f"<div class='simple-item'><div><strong>{esc(nurture['contact_name'] or nurture['company'] or '未命名线索')}"
                f" <span class='badge pending_review'>待培育</span> <span class='badge'>{esc(stage_labels.get(nurture['stage'], nurture['stage'] or '新线索'))}</span></strong>"
                f"<small>{contact_bits}{company_bit} · 来源：{esc(NURTURE_SOURCE_LABELS.get(nurture['source'], nurture['source'] or '未填'))} · 负责人：{esc(nurture['owner'] or '未分配')} · 跟进：{follow}</small></div>"
                f"<div class='actions'><a class='button' href='/admin/leads/{nurture['id']}'>打开线索 →</a></div></div>"
            )
        nurture_items_html = "".join(nurture_items) or "<div class='muted-block'>还没有待培育线索。线下询盘、展会名片等未形成 RFQ 的线索可登记在这里。</div>"
        nurture_source_options = "".join(f"<option value='{key}'>{label}</option>" for key, label in NURTURE_SOURCE_OPTIONS)
        nurture_section = (
            "<section class='card' style='margin-top:18px' id='nurture'><div class='panel-title'><h2>待培育线索（未形成 RFQ）</h2>"
            "<span class='muted'>手动登记，不受上方筛选影响</span></div>"
            f"<div class='simple-list'>{nurture_items_html}</div>"
            "<details class='detail-toggle'><summary>登记待培育线索</summary><div><form method='post' action='/admin/sales/nurture'><div class='form-grid'>"
            "<div><label>联系人称呼</label><input name='contact_name' placeholder='例如 王工'></div>"
            "<div><label>公司 / 单位</label><input name='company' placeholder='例如 华东水务工程公司'></div>"
            "<div><label>邮箱</label><input type='email' name='contact_email' placeholder='name@company.example'></div>"
            "<div><label>电话</label><input name='contact_phone' placeholder='+86 …'></div>"
            f"<div><label>来源渠道 *</label><select name='source' required>{nurture_source_options}</select></div>"
            "<div class='full'><label>需求摘要 / 备注</label><textarea name='note' placeholder='例如：展会接触，正在做新建项目选型，预计 Q4 招标'></textarea></div>"
            "<label class='full'><input type='checkbox' name='consent_confirmed' value='1' required style='width:auto'> 已确认对方同意后续联系（线下实际已获得同意）</label>"
            "</div><p class='muted'>邮箱与电话至少填写一种；同意确认是登记的必选前提；登记后负责人默认为登记人，可在线索详情中调整。</p>"
            "<p><button>登记待培育线索</button></p></form></div></details></section>"
        )
        body = f"""
<div class='toolbar'><div><h1>销售工作台</h1><div class='muted'>统一查看 RFQ、需求、推荐、问答和人工任务；当前企业：{esc(self.tenant_display_name())}</div></div><a class='button secondary' href='/admin/rfqs'>RFQ 原始记录</a></div>
<section class='card'><div class='panel-title'><h2>线索列表</h2><span class='muted'>领取或接手的线索从这里打开处理{filtered_note}</span></div>
{reminder_strip}
<form method='get' action='/admin/sales' class='filter-bar'><label>阶段</label><select name='stage'>{stage_options_html}</select><label>负责人</label><select name='owner'>{owner_options_html}</select><label>时效</label><select name='timing'>{timing_options_html}</select><label>关键词</label><input name='q' value='{esc(keyword_raw)}' placeholder='公司 / 型号 / 邮箱 / 编号'><button class='secondary'>筛选</button></form>
<div class='simple-list'>{items_html}</div></section>
{nurture_section}
"""
        self.send_html(self.console_page("销售工作台", body, "sales"), 200)

    def admin_nurture_lead(self, lead_id: str, query: dict[str, list[str]]) -> None:
        """待培育线索详情（V1-08）：跟进状态、节点、操作历史与分配；无 RFQ/会话。"""
        lead = self.db.get_lead(self.tenant_id, lead_id)
        if not lead:
            self.send_html(self.console_page("线索不存在", "<div class='error'>线索不存在或不属于当前企业。</div>", "sales"), 404)
            return
        if lead["kind"] != "nurture":
            self.redirect(f"/admin/sales/{lead['rfq_id']}")
            return
        notice = query.get("notice", [""])[0]
        notice_text = {
            "updated": "跟进状态已保存。",
            "assigned": "负责人已更新。",
            "nurture-created": "待培育线索已登记；负责人默认为登记人，可在下方调整阶段与下一步。",
            "milestone_on": "跟进节点已标记完成。",
            "milestone_off": "跟进节点已重新打开。",
            "milestone_invalid": "该跟进节点不可用。",
            "merged": "已合并该线索；原记录与历史全部保留。",
        }.get(notice, "")
        notice_banner = f"<div class='notice'>{esc(notice_text)}</div>" if notice_text else ""
        stages = self.db.get_lead_stages(self.tenant_id)
        stage_labels = {stage["key"]: stage["label"] for stage in stages}
        today = datetime.now(timezone.utc).date().isoformat()
        stage_options = "".join(
            f"<option value='{esc(stage['key'])}' {'selected' if lead['stage'] == stage['key'] else ''}>{esc(stage['label'])}</option>"
            for stage in stages
        )
        closed_info = (
            f"<p class='field-state'>已于 {esc(lead['closed_at'])} 关闭（原因：{esc(lead['close_reason'])}）；重新选择其他阶段即可重新激活。</p>"
            if lead["stage"] == "closed" and lead["closed_at"] else ""
        )
        assign_action = ""
        if self.user_role == "admin":
            sales_options = "".join(
                f"<option value='{esc(user['email'])}' {'selected' if lead['owner'] == user['email'] else ''}>{esc(user['display_name'])} · {esc(user['email'])}</option>"
                for user in self.db.list_users(self.tenant_id)
                if user["role"] == "employee" and user["active"] and "sales" in self.db.get_user_modules(user["id"])
            )
            if sales_options:
                assign_action = (
                    f"<form method='post' action='/admin/leads/{lead_id}/assign'><label>负责人（分配或改派）</label>"
                    f"<select name='owner_email'>{sales_options}</select><button>分配或改派</button></form>"
                )
        if lead["stage"] == "closed":
            focus_label, focus_title, focus_text = "当前状态 · 已关闭", "这条待培育线索已关闭", "如客户重启需求，可改回其他阶段重新激活；历史记录保留。"
        elif lead["next_follow_up"] and lead["next_follow_up"] < today:
            focus_label, focus_title, focus_text = "当前任务 · 逾期跟进", f"跟进日期 {lead['next_follow_up']} 已过", "建议尽快联系，并在下方更新下一步或阶段。"
        else:
            focus_label, focus_title, focus_text = "当前任务 · 培育推进", "安排下一步跟进或推进阶段", "记录技术确认、样品与报价准备节点；客户在官网提交询价后可人工核对并合并线索。"
        focus_html = (
            f"<div class='focus'><div><span class='focus-label'>{esc(focus_label)}</span><strong>{esc(focus_title)}</strong>"
            f"<p>{esc(focus_text)}</p></div><div class='actions'><a class='button' href='#lead-update'>更新跟进状态 →</a></div></div>"
        )
        milestones = self.db.list_lead_milestones(self.tenant_id, lead_id)
        milestone_rows = []
        for key, label in LEAD_MILESTONE_DEFS:
            done = milestones.get(key)
            state = f"已完成 · {esc(done['done_by'])} · {esc(done['done_at'])}" if done else "未完成"
            action_label = "重新打开" if done else "标记完成"
            milestone_rows.append(
                f"<div class='simple-item'><div><strong>{esc(label)}</strong><small>{state}</small></div>"
                f"<div class='actions'><form method='post' action='/admin/leads/{lead_id}/milestones/{key}' class='inline'><button class='secondary'>{action_label}</button></form></div></div>"
            )
        timeline_rows = self.db.conn.execute(
            "SELECT action, actor, details, created_at FROM audit_log WHERE tenant_id=? AND entity_type='lead' AND entity_id=? ORDER BY id DESC LIMIT 20",
            (self.tenant_id, lead_id),
        ).fetchall()
        timeline_html = "".join(
            f"<div class='source'><span><b>{esc(AUDIT_ACTION_LABELS.get(row['action'], row['action']))}</b> · {esc(row['actor'])}"
            f"<br><span class='field-state'>{esc(row['created_at'])}</span></span><span class='field-state'>{esc(row['details'])}</span></div>"
            for row in timeline_rows
        ) or "<p class='muted'>暂无操作记录。</p>"
        source_label = NURTURE_SOURCE_LABELS.get(lead["source"], lead["source"] or "未填")
        merged_banner = merged_into_banner(self.db, self.tenant_id, lead)
        duplicates_section = duplicate_leads_section(self.db, self.tenant_id, lead, self.user_role == "admin")
        body = f"""
<div class='toolbar'><div><a href='/admin/sales'>← 返回销售工作台</a><h1>{esc(lead['contact_name'] or lead['company'] or '待培育线索')}</h1>
<div class='muted'>{esc(lead['company'])} <span class='badge pending_review'>待培育</span> <span class='badge'>{esc(stage_labels.get(lead['stage'], lead['stage'] or '新线索'))}</span></div></div></div>
{notice_banner}
{merged_banner}
{focus_html}
<div class='summary-strip'><span>来源：{esc(source_label)}</span><span>负责人：{esc(lead['owner'] or '未分配')}</span><span>跟进：{esc(lead['next_follow_up'] or '未安排')}</span><span>登记：{esc(lead['created_at'])}</span></div>
{duplicates_section}
<div class='grid'><section class='card' id='lead-update'><h2>线索处理</h2>{closed_info}{assign_action}<form method='post' action='/admin/leads/{lead_id}/update'><label>阶段</label><select name='stage'>{stage_options}</select><label>下一步跟进日期</label><input type='date' name='next_follow_up' value='{esc(lead['next_follow_up'] or '')}'><label>销售备注</label><textarea name='note'>{esc(lead['note'])}</textarea><label>关闭原因（阶段设为「已关闭」时必填）</label><input name='close_reason' value='{esc(lead['close_reason'] or '')}' placeholder='例如：客户取消项目 / 已转向其他方案'><p><button>保存跟进状态</button></p></form></section>
<section class='card'><h2>联系人与来源</h2><p><b>联系人：</b>{esc(lead['contact_name']) or '未提供'}</p><p><b>公司：</b>{esc(lead['company']) or '未提供'}</p><p><b>邮箱：</b>{esc(lead['contact_email']) or '未提供'}</p><p><b>电话：</b>{esc(lead['contact_phone']) or '未提供'}</p><p><b>来源渠道：</b>{esc(source_label)}</p><p><b>联系同意：</b>{esc(CONTACT_CONSENT_SOURCE_LABELS.get(lead['consent_source'], '未记录'))}· {esc(lead['consent_at'] or '')}</p><p><b>需求摘要：</b>{esc(lead['note']) or '无'}</p><p class='field-state'>待培育线索尚未形成 RFQ；客户在官网提交询价后，销售可核对是否为同一采购需求（人工合并，系统不会自动合并）。</p></section></div>
<section class='card' style='margin-top:18px' id='milestones'><div class='panel-title'><h2>跟进节点</h2><span class='muted'>技术确认 / 样品准备 / 报价准备</span></div><div class='simple-list'>{"".join(milestone_rows)}</div></section>
<details class='detail-toggle' style='margin-top:18px'><summary>操作历史（最近 20 条）</summary><div class='muted-block'>{timeline_html}</div></details>
"""
        self.send_html(self.console_page(f"{lead['contact_name'] or lead['company'] or '待培育线索'} · 待培育线索", body, "sales"), 200)

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
        stop_form = f"<form method='post' action='/admin/sales/{rfq_id}/contact-stop'><label>记录买方在其他渠道提出的停止主动联系原话</label><textarea name='reason' required></textarea><button class='danger'>记录并停止主动跟进</button></form>" if not preference["stop_proactive_contact"] and self.has_module("sales") else ""
        followup_disabled = "disabled" if preference["stop_proactive_contact"] else ""
        closed_info = (
            f"<p class='field-state'>已于 {esc(lead['closed_at'])} 关闭（原因：{esc(lead['close_reason'])}）；重新选择其他阶段即可重新激活。</p>"
            if lead["stage"] == "closed" and lead["closed_at"] else ""
        )
        milestones = self.db.list_lead_milestones(self.tenant_id, lead["id"])
        replies = self.db.list_lead_replies(self.tenant_id, lead["id"])

        def milestone_row(key: str, label: str) -> str:
            done = milestones.get(key)
            state = f"已完成 · {esc(done['done_by'])} · {esc(done['done_at'])}" if done else "未完成"
            action_label = "重新打开" if done else "标记完成"
            return (
                f"<div class='simple-item'><div><strong>{esc(label)}</strong><small>{state}</small></div>"
                f"<div class='actions'><form method='post' action='/admin/sales/{rfq_id}/milestones/{key}' class='inline'><button class='secondary'>{action_label}</button></form></div></div>"
            )

        milestone_rows = "".join(milestone_row(key, label) for key, label in LEAD_MILESTONE_DEFS)
        channel_options = "".join(f"<option value='{key}'>{label}</option>" for key, label in REPLY_CHANNELS)
        # F-22：渠道会话的草稿可经出站总线真发（发送结果与重试都显示在卡片上）
        lead_conversation = self.db.get_conversation(self.tenant_id, lead["conversation_id"])
        lead_channel = (lead_conversation["source"] or "") if lead_conversation else ""
        channel_sendable = lead_channel in CHANNEL_KEYS
        channel_label = CHANNEL_LABELS.get(lead_channel, lead_channel)
        reply_parts = []
        for reply in replies:
            reply_delivery = None
            if reply["message_id"]:
                reply_delivery = self.db.conn.execute(
                    "SELECT delivery_status, delivery_error, delivery_attempts FROM messages WHERE id=?", (reply["message_id"],)
                ).fetchone()

            def channel_retry_form(label_text: str) -> str:
                return (
                    f"<form method='post' action='/admin/sales/{rfq_id}/replies/{reply['id']}' class='inline'>"
                    f"<button name='action' value='channel'>{esc(label_text)}</button></form>"
                )

            if reply["status"] == "draft":
                channel_button = channel_retry_form(f"经 {channel_label} 发送") if channel_sendable else ""
                reply_parts.append(
                    f"<div class='source' style='flex-direction:column;align-items:stretch;gap:8px'>"
                    f"<span><span class='badge pending_review'>未发送草稿</span> <span class='field-state'>最近更新 {esc(reply['updated_at'])} · {esc(reply['updated_by'] or reply['created_by'])}</span></span>"
                    f"<form method='post' action='/admin/sales/{rfq_id}/replies/{reply['id']}'>"
                    f"<textarea name='body' rows='3' required>{esc(reply['body'])}</textarea>"
                    f"<div class='actions' style='margin-top:8px'><label class='check-inline' style='margin:0'>发送渠道 <select name='channel'>{channel_options}</select></label>"
                    f"<button name='action' value='save' class='secondary'>保存修改</button>"
                    f"{channel_button}"
                    f"<button name='action' value='sent' class='secondary'>记录为已发送（线下）</button></div></form>"
                    f"<div class='actions'><form method='post' action='/admin/sales/{rfq_id}/replies/{reply['id']}' class='inline'><button name='action' value='discard' class='secondary'>废弃草稿</button></form></div>"
                    f"</div>"
                )
            elif reply["status"] in ("failed", "queued"):
                error_text = (reply_delivery["delivery_error"] if reply_delivery else "") or "发送失败"
                attempts = (reply_delivery["delivery_attempts"] if reply_delivery else 0) or 0
                if reply["status"] == "queued":
                    badge = "<span class='badge pending_review'>排队中</span>"
                    hint = "消息已进入出站队列；可点「立即重发」或等待后台补投。"
                else:
                    badge = "<span class='badge withdrawn'>发送失败</span>"
                    hint = f"原因：{esc(error_text)} · 已尝试 {attempts} 次"
                retry_form = channel_retry_form(f"重试发送（{channel_label}）") if channel_sendable else ""
                reply_parts.append(
                    f"<div class='source' style='flex-direction:column;align-items:stretch;gap:8px'>"
                    f"<span>{badge} <span class='field-state'>{hint}</span></span>"
                    f"<span>{esc(reply['body'])}</span>"
                    f"<div class='actions'>{retry_form}</div>"
                    f"</div>"
                )
            elif reply["status"] == "sent":
                if reply["message_id"]:
                    delivery_state = {
                        "sent": "已发送", "delivered": "已送达",
                        "failed": "发送失败", "queued": "排队中",
                    }.get(reply_delivery["delivery_status"] if reply_delivery else "", "已发送")
                    sent_channel_label = CHANNEL_LABELS.get(reply["channel"], reply["channel"])
                    reply_parts.append(
                        f"<div class='source'><span><span class='badge published'>经 {esc(sent_channel_label)} 发送 · {esc(delivery_state)}</span><br>{esc(reply['body'])}</span>"
                        f"<span class='field-state'>{esc(reply['sent_at'])} · {esc(reply['sent_by'])}</span></div>"
                    )
                else:
                    channel_label_offline = REPLY_CHANNEL_LABELS.get(reply["channel"], reply["channel"])
                    reply_parts.append(
                        f"<div class='source'><span><span class='badge published'>已记录发送（{esc(channel_label_offline)}）</span><br>{esc(reply['body'])}</span>"
                        f"<span class='field-state'>{esc(reply['sent_at'])} · {esc(reply['sent_by'])}</span></div>"
                    )
            else:
                reply_parts.append(
                    f"<div class='source'><span><span class='badge withdrawn'>已废弃</span> <span class='field-state'>{esc(reply['body'][:60])}</span></span>"
                    f"<span class='field-state'>{esc(reply['updated_at'])}</span></div>"
                )
        replies_new_form = (
            f"<form method='post' action='/admin/sales/{rfq_id}/replies'><label>起草回复（保存为未发送草稿）</label>"
            f"<textarea name='body' rows='3' required placeholder='写给买方的回复内容'></textarea>"
            f"<p><button>保存草稿</button></p></form>"
            f"<p class='field-state'>渠道会话可直接「经渠道发送」：draft → queued → sent/failed，失败可重试；其他情况线下发送后回来「记录为已发送」。两种方式发送前都会检查买方「停止主动联系」状态（已停止则阻止发送）。</p>"
        )
        replies_html = replies_new_form + ("".join(reply_parts) or "<p class='muted'>还没有回复记录。</p>")
        followup_cards = (
            f"<section class='card' style='margin-top:18px' id='milestones'><h2>跟进节点</h2>{milestone_rows}</section>"
            f"<section class='card' style='margin-top:18px' id='replies'><h2>回复草稿（跟进记录）</h2>{replies_html}</section>"
        )
        requirements = snapshot.get("requirements", {})
        recommendation = snapshot.get("recommendation") or {}
        requirement_rows = "".join(f"<tr><td>{esc(item.get('label', key))}</td><td>{esc(item.get('value', ''))} {esc(item.get('unit', ''))}</td><td>{esc(item.get('state', ''))}</td></tr>" for key, item in requirements.items()) or "<tr><td colspan='3' class='muted'>暂无需求快照</td></tr>"
        answers = self.db.list_answers(self.tenant_id, rfq["conversation_id"])
        answer_rows = "".join(f"<div class='source'><span><b>{'有据回答' if answer['answer_status'] == 'grounded' else '人工接管'}</b><br>{esc(answer['answer_text'])}</span><span class='field-state'>{esc(answer['created_at'])}</span></div>" for answer in answers) or "<p class='muted'>暂无问答记录。</p>"
        tasks = [task for task in self.db.list_human_tasks(self.tenant_id) if task["conversation_id"] == rfq["conversation_id"]]
        task_rows = "".join(f"<div class='source'><span><b>{esc(task['status'])}</b><br>{esc(task['question'])}<br><span class='field-state'>{esc(task['reason'])}</span></span><a href='/admin/tasks/{task['id']}'>处理</a></div>" for task in tasks) or "<p class='muted'>暂无人工任务。</p>"
        audit_rows = self.db.list_entity_audit(self.tenant_id, [rfq["id"], lead["id"], *[reply["id"] for reply in replies]])
        audit_html = "".join(f"<div class='field-state'>{esc(item['created_at'])} · {esc(item['action'])} · {esc(item['actor'])} · {esc(item['details'])}</div>" for item in audit_rows) or "<p class='muted'>暂无操作记录。</p>"
        stage_options = "".join(f"<option value='{esc(stage['key'])}' {'selected' if lead['stage'] == stage['key'] else ''}>{esc(stage['label'])}</option>" for stage in self.db.get_lead_stages(self.tenant_id))
        notice_code = query.get("notice", [""])[0]
        notice = {
            "updated": "<div class='notice'>销售状态已保存，刷新后仍会保留。</div>",
            "assigned": "<div class='notice'>负责人已更新，同会话的 RFQ 使用同一负责人。</div>",
            "close_reason": "<div class='error'>未保存：将线索设为「已关闭」时必须填写关闭原因。</div>",
            "stop_conflict": "<div class='error'>未保存：买方已要求停止主动联系，不能安排主动跟进日期。</div>",
            "update_invalid": "<div class='error'>未保存：请检查阶段与跟进信息。</div>",
            "milestone_on": "<div class='notice'>跟进节点已标记完成。</div>",
            "milestone_off": "<div class='notice'>跟进节点已重新打开。</div>",
            "milestone_invalid": "<div class='error'>节点操作未完成：不支持的节点。</div>",
            "reply_saved": "<div class='notice'>回复草稿已保存（未发送）。</div>",
            "reply_sent": "<div class='notice'>已记录为「线下发送」——平台不会自动外发；发送状态见下方草稿卡片。</div>",
            "reply_channel_sent": "<div class='notice'>已通过渠道发送——买方在对应渠道里可见；送达状态与重试都在下方卡片。</div>",
            "reply_channel_failed": "<div class='error'>渠道发送未成功——消息已存为「发送失败」，原因在下方卡片上，可点「重试发送」。</div>",
            "reply_discarded": "<div class='notice'>草稿已废弃。</div>",
            "reply_stop_blocked": "<div class='error'>发送被阻止：买方已要求停止主动联系；请仅在买方主动发起沟通后回应。</div>",
            "reply_invalid": "<div class='error'>回复操作未完成：请检查内容与操作。</div>",
            "merged": "<div class='notice'>已合并该线索；原记录与历史全部保留。</div>",
        }.get(notice_code, "")
        claim_action = f"<form method='post' action='/admin/sales/{rfq_id}/claim'><button>领取这条会话</button></form>" if not lead["owner"] and self.user_role != "admin" and self.has_module("sales") else ""
        sales_options = "".join(f"<option value='{esc(user['email'])}' {'selected' if lead['owner'] == user['email'] else ''}>{esc(user['display_name'])} · {esc(user['email'])}</option>" for user in self.db.list_users(self.tenant_id) if user["role"] == "employee" and user["active"] and "sales" in self.db.get_user_modules(user["id"]))
        assign_action = f"<form method='post' action='/admin/sales/{rfq_id}/assign'><label>分配给销售</label><select name='owner_email'>{sales_options}</select><button>分配或改派</button></form>" if self.user_role == "admin" and sales_options else ""
        if recommendation:
            recommendation_html = f"<p><b>推荐型号：</b>{esc(recommendation.get('model'))}</p><p><b>产品版本：</b>v{esc(recommendation.get('product_version'))}</p><p><b>满足项：</b>{esc('；'.join(recommendation.get('satisfies', [])) or '—')}</p><p><b>冲突项：</b>{esc('；'.join(recommendation.get('conflicts', [])) or '—')}</p><a href='{esc(recommendation.get('detail_url', '#'))}' target='_blank'>打开产品页</a>"
        else:
            recommendation_html = "<p class='muted'>暂无推荐快照。</p>"
        if preference["stop_proactive_contact"]:
            focus_label, focus_title, focus_text = "注意 · 已停止主动联系", "买方要求不再被主动联系", f"原话：{preference['stop_reason']}；买方主动发问时仍可回应，不得安排主动跟进。"
            focus_actions = ""
        elif not lead["owner"] and self.user_role != "admin" and self.has_module("sales"):
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
        merged_banner = merged_into_banner(self.db, self.tenant_id, lead)
        duplicates_section = duplicate_leads_section(self.db, self.tenant_id, lead, self.user_role == "admin")
        body = f"""
<div class='toolbar'><div><a href='/admin/sales'>← 返回销售工作台</a><h1>销售线索 {esc(rfq['id'])}</h1><div class='muted'>RFQ 状态：{esc(rfq['status'])} · 会话：{esc(rfq['conversation_id'])} · 当前 v{esc(rfq['current_revision'])}</div></div></div>
{notice}
{stop_banner}
{merged_banner}
{change_banner}
{duplicates_section}
{focus_html}
<div class='grid'><section class='card' id='lead-update'><h2>线索处理</h2><p><b>会话负责人：</b>{esc(lead['owner'] or '未领取')}</p>{closed_info}{assign_action if lead['owner'] else ''}<form method='post' action='/admin/sales/{rfq_id}/update'><label>阶段</label><select name='stage'>{stage_options}</select><label>下一步跟进日期</label><input type='date' name='next_follow_up' value='{esc(lead['next_follow_up'] or '')}' {followup_disabled}><label>销售备注</label><textarea name='note' placeholder='记录下一步和客户背景'>{esc(lead['note'])}</textarea><label>关闭原因（阶段设为「已关闭」时必填）</label><input name='close_reason' value='{esc(lead['close_reason'] or '')}' placeholder='例如：客户取消项目 / 已转向其他方案'><p><button>保存跟进状态</button></p></form></section><section class='card'><h2>联系人与询价</h2><p><b>联系人：</b>{esc(rfq['contact_name']) or '未提供'}</p><p><b>公司：</b>{esc(rfq['company']) or '未提供'}</p><p><b>邮箱：</b>{esc(rfq['contact_email']) or '未提供'}</p><p><b>电话：</b>{esc(rfq['contact_phone']) or '未提供'}</p><p><b>型号：</b>{esc(rfq['model'] or '未指定')}</p><p><b>数量：</b>{esc(rfq['quantity']) or '待补充'} · <b>目的地：</b>{esc(rfq['destination']) or '待补充'} · <b>交期：</b>{esc(rfq['requested_delivery']) or '待补充'}</p><p><b>备注：</b>{esc(rfq['notes']) or '无'}</p><p><b>联系同意：</b>买方通过「{esc(CONTACT_CONSENT_SOURCE_LABELS.get(rfq['contact_consent_source'], '未记录'))}」提交询价，同意就本次询价联系 · {esc(rfq['contact_consent_at'] or '')}</p><p><a href='/admin/rfqs/{rfq_id}'>查看初次提交与历次修改（当前 v{rfq['current_revision']}）</a></p></section></div>
{stop_form}
{followup_cards}
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
        self.send_html(self.render_product_page(tenant_id, product, version, query, site), 200)

    def render_product_page(
        self,
        tenant_id: str,
        product: sqlite3.Row,
        version: sqlite3.Row,
        query: dict[str, list[str]],
        site: sqlite3.Row | None = None,
        *,
        blocks: list | None = None,
        params: list | None = None,
        preview: bool = False,
    ) -> str:
        """公开产品页渲染（V1-03 参数化）。

        blocks/params = 展示页模板配置（缺省 = 标准模板，输出与现状逐字节一致）；
        preview = 买家视角预览：不记录公开事件（且 site=None 时天然不含询价表单与聊天窗）。
        """
        snapshot = json.loads(version["snapshot_json"])
        # V1-03：产品发布时冻结的展示页引用（无引用＝平台标准模板，输出与现状一致）。
        if blocks is None and params is None:
            template_ref = snapshot.get("template") if isinstance(snapshot, dict) else None
            if isinstance(template_ref, dict) and template_ref.get("template_version_id"):
                ref_row = self.db.get_template_version(tenant_id, str(template_ref["template_version_id"]))
                if ref_row:
                    ref_content = json.loads(ref_row["content_json"])
                    if isinstance(ref_content.get("blocks"), list):
                        blocks = ref_content["blocks"]
                    if isinstance(ref_content.get("params"), list):
                        params = ref_content["params"]
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
        # V1-03：模板板块与参数（缺省=标准模板；标准组合的输出与现状逐字节一致）。
        render_blocks = enabled_render_blocks(blocks)
        enabled_keys = [block["key"] for block in render_blocks]
        enabled_set = set(enabled_keys)
        block_conf = {str(block.get("key")): block for block in render_blocks}
        def block_title(key: str, default_en: str, default_zh: str) -> str:
            conf = block_conf.get(key) or {}
            title_zh = str(conf.get("title_zh") or "").strip()
            title_en = str(conf.get("title_en") or "").strip()
            custom = (title_en or title_zh) if lang == "en" else (title_zh or title_en)
            return esc(custom) if custom else t(default_en, default_zh)
        if isinstance(params, list):
            enabled_params = [item for item in params if isinstance(item, dict) and item.get("enabled")]
            legacy_param_keys = [str(item.get("key")) for item in enabled_params if str(item.get("key")) in FIELD_LABELS]
            custom_params = [item for item in enabled_params if str(item.get("key")) not in FIELD_LABELS]
        else:
            legacy_param_keys = [key for _en, _zh, keys in PRODUCT_PAGE_GROUPS for key in keys]
            custom_params = []
        legacy_enabled = set(legacy_param_keys)
        hero_keys = ("flow_range", "head_range", "media", "power", "material", "temperature", "performance_points", "dimension")
        def field_label(key: str) -> str:
            return FIELD_LABELS_EN.get(key, "Installation dimensions") if lang == "en" else FIELD_LABELS.get(key, "安装尺寸")
        hero_facts = "".join(f"<div><b>{esc(field_label(key))}</b>{fact(key)}</div>" for key in hero_keys if key in legacy_enabled)
        highlight_keys = [key for key in ("flow_range", "head_range") if key in legacy_enabled]
        if highlight_keys:
            highlight_fact_pair = "<div class='page-fact-pair'>" + "".join(f"<div><b>{esc(field_label(key))}</b>{fact(key)}</div>" for key in highlight_keys) + "</div>"
        else:
            highlight_fact_pair = f"<p class='page-missing'>{t('Flow and head values are not available in this published record.', '当前发布记录没有流量、扬程数据。')}</p>"
        group_parts = []
        for group_en, group_zh, keys in PRODUCT_PAGE_GROUPS:
            visible_keys = [key for key in keys if key in legacy_enabled]
            if not visible_keys:
                continue
            group_parts.append(
                f"<section class='page-group'><h3>{t(group_en, group_zh)}</h3><dl>" + "".join(f"<div><dt>{esc(field_label(key))}</dt><dd>{fact(key)}</dd></div>" for key in visible_keys) + "</dl></section>"
            )
        if custom_params:
            custom_rows = "".join(
                f"<div><dt>{esc(str(item.get('label_en') or '').strip() or str(item.get('label_zh') or '').strip()) if lang == 'en' else esc(str(item.get('label_zh') or '').strip() or str(item.get('label_en') or '').strip())}</dt>"
                f"<dd><span class='page-missing'>{t('Not provided', '资料未提供')}</span></dd></div>"
                for item in custom_params
            )
            group_parts.append(f"<section class='page-group'><h3>{t('Specifications', '规格参数')}</h3><dl>{custom_rows}</dl></section>")
        group_html = "".join(group_parts)
        group_html += f"<section class='page-group'><h3>{t('Purchasing and delivery', '采购交付')}</h3><p class='page-missing'>{t('MOQ, lead time and sample availability require seller confirmation.', '起订量、交期和样品状态待卖方确认。')}</p></section>"
        missing_labels = [FIELD_LABELS[key] for key in snapshot.get("missing_fields", []) if key in FIELD_LABELS]
        missing_html = "<p class='muted'>" + ("Pending information: " if lang == "en" else "待补充信息：") + esc("、".join(missing_labels)) + "</p>" if missing_labels else ""
        source_prefix = f"/s/{quote(site['site_key'])}" if site else ""
        def source_url(entry: dict[str, object], inline: bool = False) -> str:
            """公开资料 URL；inline=浏览器内预览；预览页自动带 nolog=1（不产生公开事件）。"""
            base = f"{source_prefix}/products/{esc(product['slug'])}/sources/{esc(entry['id'])}"
            params = []
            if inline:
                params.append("preview=1")
            if preview:
                params.append("nolog=1")
            return f"{base}?{'&'.join(params)}" if params else base
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
                f"<figure class='gallery-slide' data-index='{idx}'><img src='{source_url(entry, inline=True)}' alt='{esc(product['model'])} · {esc(entry['filename'])}'><figcaption>{esc(entry['filename'])} · {t('this model', '本型号已核准图片')}</figcaption></figure>"
                for idx, entry in enumerate(gallery_entries)
            )
            gallery_thumbs = "".join(
                f"<label for='glr-{idx}'><img src='{source_url(entry, inline=True)}' alt='' loading='lazy'><span>{idx + 1}</span></label>"
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
                f" · <a href='{source_url(entry, inline=True)}' target='_blank' rel='noopener'>{t('Preview', '预览')}</a>"
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
        drawings_action = f"<a class='button secondary' href='#drawings'>{t('2D/3D files', '图纸与模型')}</a>" if (exact_cad or series_cad) and "drawings" in enabled_set else ""
        comparison_keys = tuple(key for key in ("flow_range", "head_range", "power", "material", "media") if key in legacy_enabled)
        comparison_products = [product] + [item for item in self.db.list_products(tenant_id) if item["id"] != product["id"] and item["status"] == "published"][:2]
        comparison_versions = [(item, self.db.get_published_version(tenant_id, item["id"])) for item in comparison_products]
        comparison_versions = [(item, item_version) for item, item_version in comparison_versions if item_version]
        comparison_snapshots = [(item, {field["field_key"]: field for field in json.loads(item_version["snapshot_json"])["fields"]}) for item, item_version in comparison_versions]
        comparison_header = "".join(f"<th>{esc(item['model'])}</th>" for item, _ in comparison_snapshots)
        comparison_rows = "".join(f"<tr><th>{esc(field_label(key))}</th>" + "".join(f"<td>{esc(fields[key]['value'])} {esc(fields[key]['unit'])}</td>" if fields.get(key) else f"<td class='page-missing'>{t('Not provided', '资料未提供')}</td>" for _, fields in comparison_snapshots) + "</tr>" for key in comparison_keys)
        comparison_links = "".join(f"<li><a href='{source_prefix}/products/{esc(item['slug'])}'>{esc(item['model'])}</a></li>" for item, _ in comparison_snapshots)
        title = f"{product['model']} | Product"
        inquiry_link = "<a class='button' href='#product-rfq'>Request a quote / 发起询价</a>" if site else "<p class='muted'>Open the seller's website link to request a quote.</p>"
        chat_html = ""
        if site:
            context_json = json.dumps({"siteKey": site["site_key"], "productId": product["id"], "model": product["model"]}, ensure_ascii=False).replace("</", "<\\/")
            chat_html = f"""<script>window.PUMP_CHAT_CONTEXT={context_json};</script>
<button id="chat-launcher" class="chat-launcher" aria-controls="chat-panel" aria-expanded="false"><span id="chat-launcher-label">Ask me about products</span><span id="chat-badge" class="chat-badge" hidden></span></button>
<aside id="chat-panel" class="chat-panel" aria-label="Pump product conversation" hidden>
<div class="chat-header"><div><strong id="chat-title">Pump product assistant</strong><small id="chat-subtitle">Technical information with sources</small></div><button id="chat-language" type="button" title="Switch language">中文</button><button id="chat-close" type="button" aria-label="Close chat">−</button></div>
<div class="site-config"><label for="site-key">Website key</label><div><input id="site-key" autocomplete="off" placeholder="Paste key from seller workspace"><button id="connect" type="button">Connect</button></div><small>This page connects to the seller chat automatically.</small></div>
<div id="chat-log" class="chat-log" role="log" aria-live="polite"></div>
<div id="chat-notice" class="chat-notice" role="status"></div>
<div id="chat-actions" class="chat-actions"><button id="show-rfq" type="button">Request a quote</button><button id="stop-contact" type="button" class="secondary">Stop proactive contact</button></div>
<form id="message-form" class="message-form"><label for="message">Your message</label><div><textarea id="message" rows="2"></textarea><button type="submit">Send</button></div></form>
</aside>
<script src='/reference-client/app.js' defer></script>"""
        product_field_config = self.db.get_rfq_field_config(tenant_id, self.db.get_approved_category(tenant_id, product["id"]))

        def product_input(field_key: str, name: str, label_html: str, attrs: str = "") -> str:
            if product_field_config[field_key] == "hidden":
                return ""
            star = " *" if product_field_config[field_key] == "required" else ""
            required_attr = " required" if product_field_config[field_key] == "required" else ""
            return f"<label>{label_html}{star} <input name='{name}'{attrs}{required_attr}></label>"

        product_rfq_fields = (
            product_input("email", "email", "Email", " type='email'")
            + product_input("phone", "phone", "Phone", " type='tel'")
            + product_input("quantity", "quantity", "Quantity")
            + product_input("destination", "destination", "Destination")
        )
        if product_field_config["notes"] != "hidden":
            notes_star = " *" if product_field_config["notes"] == "required" else ""
            notes_required = " required" if product_field_config["notes"] == "required" else ""
            product_rfq_fields += f"<label class='full'>Requirements{notes_star} <textarea name='notes'{notes_required}></textarea></label>"
        product_field_json = json.dumps(product_field_config, ensure_ascii=False)
        rfq_html = f"""<section class='card' id='product-rfq' data-site-key='{esc(site['site_key'])}' data-product-id='{esc(product['id'])}' style='margin-top:18px'>
<h2>Request for a quotation / 发起询价</h2><p class='muted'>You may send a request at any time; fields marked * are required. / 可随时提交询价，带 * 为必填项。</p>
<p id='product-rfq-session' class='muted' role='status'></p>
<form id='product-rfq-form'><div class='form-grid'>{product_rfq_fields}</div><label><input name='contact_confirmed' type='checkbox' style='width:auto'> I confirm this reply contact may be used for this request.</label><button type='submit'>Submit RFQ</button></form><p id='product-rfq-result' role='status'></p></section>
<script>window.RFQ_FIELD_CONFIG={product_field_json};</script>
<script src='/reference-client/product-page.js' defer></script>""" if site else ""
        public_path = f"/s/{quote(site['site_key'])}/products/{esc(product['slug'])}" if site else f"/products/{esc(product['slug'])}"
        mobile_cta_html = f"<a class='page-mobile-cta' href='#product-rfq'>{t('Request a quote', '发起询价')}</a>" if site else ""
        breadcrumb_html = f"<nav class='page-breadcrumb' aria-label='Breadcrumb'>{t('Home › Products', '首页 › 产品')} › {esc(product['model'])}</nav>"
        hero_action_parts = [inquiry_link]
        if "downloads" in enabled_set:
            hero_action_parts.append(f"<a class='button secondary' href='#downloads'>{t('View documents', '查看资料')}</a>")
        hero_action_parts.append(drawings_action)
        if "faq" in enabled_set:
            hero_action_parts.append(f"<a class='button secondary' href='#faq'>{t('Read FAQ', '查看常见问题')}</a>")
        hero_actions = "<div class='page-actions'>" + "".join(hero_action_parts) + "</div>"
        hero_section = (
            f"<section class='page-hero' id='overview' data-section='hero'><div class='page-visual' role='img' aria-label='{t('Approved product images', '已核准产品图片') if gallery_entries else t('Product image pending approval', '产品图片待核准')}'>{hero_visual}</div><div><p class='page-eyebrow'>{t('Published product version', '已发布产品版本')} {version['version']} · {esc(version['published_at'])}</p><h1>{esc(product['model'])}</h1><p class='page-model'>{esc(product['name'])}</p><p class='muted'>{t('Check the sourced flow and head data before asking for a final duty-point review.', '请先核对有出处的流量与扬程，再请工程师确认最终工况。')}</p>{hero_actions}<div class='page-quick'>{hero_facts}</div>{missing_html}<p class='page-missing'>{t('MOQ, lead time and sample availability: ask the seller.', '起订量、交期、样品状态：请向卖方核实。')}</p></div></section>"
        )
        anchors_items = []
        for block_key, href, label_en, label_zh in (
            ("hero", "#overview", "Overview", "概览"),
            ("specs", "#specifications", "Specifications", "参数"),
            ("applications", "#applications", "Applications", "应用"),
            ("customization", "#customization", "Customization", "定制"),
            ("downloads", "#downloads", "Documents", "资料"),
            ("faq", "#faq", "FAQ", "FAQ"),
        ):
            if block_key not in enabled_set:
                continue
            anchors_items.append(f"<a href='{href}'>{'FAQ' if block_key == 'faq' else t(label_en, label_zh)}</a>")
        anchors_html = f"<nav class='page-anchors' aria-label='Product sections'>{''.join(anchors_items)}</nav>"
        if gallery_entries:
            gallery_grid = "".join(
                f"<figure><img src='{source_url(entry, inline=True)}' alt='{esc(product['model'])} · {esc(entry['filename'])}' loading='lazy'><figcaption>{esc(entry['filename'])}</figcaption></figure>"
                for entry in gallery_entries
            )
        else:
            gallery_grid = f"<p class='page-missing'>{t('No approved public image for this model yet.', '当前版本没有本型号已核准公开图片。')}</p>"
        sections = {
            "hero": hero_section,
            "highlights": f"<section class='page-section' data-section='advantages'><h2>{block_title('highlights', 'What the approved record supports', '已核准资料能说明什么')}</h2><p>{t('Flow and head values below are shown with their source. Final suitability depends on the buyer’s operating point and conditions.', '下列流量、扬程值附有出处；最终适用性仍取决于买方工况。')}</p>{highlight_fact_pair}</section>",
            "specs": f"<section class='page-section' id='specifications' data-section='specifications'><h2>{block_title('specs', 'Technical specifications', '技术参数')}</h2><div class='page-groups'>{group_html}</div></section>",
            "drawings": f"<section class='page-section' id='drawings' data-section='drawings'><h2>{block_title('drawings', '2D drawings and 3D models', '二维图纸与三维模型')}</h2><p class='muted'>{t('Only approved originals are offered here. STEP/DXF files are not rendered in the browser; download and open them in CAD software.', '这里只提供已核准原件；STEP/DXF 等不在浏览器内渲染，请下载后用 CAD 软件打开。')}</p>{drawings_html}</section>",
            "gallery": f"<section class='page-section' id='gallery' data-section='gallery'><h2>{block_title('gallery', 'Product images', '产品图片')}</h2><div class='page-gallery-grid'>{gallery_grid}</div></section>",
            "comparison": f"<section class='page-section' data-section='comparison'><h2>{block_title('comparison', 'Compare published models', '对比已发布型号')}</h2><p class='muted'>{t('Only approved published models from this seller appear here. Missing data stays visible.', '仅比较本企业已核准发布的型号；缺项如实显示。')}</p><div class='page-table-scroll'><table><thead><tr><th>{t('Parameter', '参数')}</th>{comparison_header}</tr></thead><tbody>{comparison_rows}</tbody></table></div><ul>{comparison_links}</ul></section>",
            "applications": f"<section class='page-section' id='applications' data-section='applications'><h2>{block_title('applications', 'Applications', '应用场景')}</h2><p class='page-missing'>{t('No application claim has been approved for this product version. Describe your operating conditions in the original website chat or RFQ.', '当前版本没有已核准的应用场景结论。请在原官网聊天窗口或询价中描述工况。')}</p></section>",
            "customization": f"<section class='page-section' id='customization' data-section='customization'><h2>{block_title('customization', 'Customization', '定制能力')}</h2><p class='page-missing'>{t('Available changes and turnaround times have not been confirmed by the seller.', '可定制项目及周期尚未由卖方确认。')}</p></section>",
            "manufacturing": f"<section class='page-section' data-section='manufacturing'><h2>{block_title('manufacturing', 'Manufacturing and quality control', '制造与质量控制')}</h2><p class='page-missing'>{t('Factory and inspection evidence has not been provided for publication.', '尚无获准公开的工厂与质检证据。')}</p></section>",
            "certifications": f"<section class='page-section' data-section='certification'><h2>{block_title('certifications', 'Certifications and cases', '认证与案例')}</h2><p class='page-missing'>{t('No verified certificates or customer cases are available in this published record.', '当前已发布记录没有可核验的认证或客户案例。')}</p></section>",
            "downloads": f"<section class='page-section' id='downloads' data-section='downloads'><h2>{block_title('downloads', 'Approved documents', '已获准公开的资料')}</h2><ul class='file-list'>{source_links}</ul><p class='muted'>{t('Documents without publication permission, outside the current version, or revoked are not downloadable.', '未获准公开、未纳入当前版本或已撤销许可的文件不提供下载。')}</p></section>",
            "faq": f"<section class='page-section' id='faq' data-section='faq'><h2>{block_title('faq', 'Frequently asked questions', '常见问题')}</h2><details><summary>{t('Can this model meet my exact duty point?', '这个型号能满足我的具体工况吗？')}</summary><p>{t('Compare the sourced flow and head information above, then ask an engineer to check your actual conditions.', '先核对上方有出处的流量、扬程数据，再请工程师检查实际工况。')}</p></details><details><summary>{t('What are the MOQ and lead time?', '起订量和交期是多少？')}</summary><p>{t('The published record does not confirm these commercial terms. Include your quantity and destination in an RFQ.', '当前已发布资料没有确认这些商务条件。请在询价中提供数量和目的地。')}</p></details></section>",
        }
        footer_html = f"<footer class='page-footer'>{t('Initial technical display; not a quotation or final engineering approval.', '初步技术展示；并非报价或最终工程适配结论。')} · <a href='{public_path}?lang={'zh' if lang == 'en' else 'en'}'>{'切换中文' if lang == 'en' else 'Switch to English'}</a></footer>"
        body = "\n".join(
            ["", breadcrumb_html, hero_section, anchors_html]
            + [sections[key] for key in enabled_keys if key != "hero"]
            + [rfq_html, chat_html, mobile_cta_html, footer_html, ""]
        )
        page = layout(title, body, lang=lang, public=True)
        json_ld = json.dumps({'@context': 'https://schema.org', '@type': 'Product', 'name': product['name'], 'model': product['model']}, ensure_ascii=False).replace("</", "<\\/")
        head_extra = f"<meta name='description' content='{esc(product['name'])} - approved industrial pump specifications'><script type='application/ld+json'>{json_ld}</script><link rel='stylesheet' href='/reference-client/product-page.css'><link rel='stylesheet' href='/reference-client/chat.css'><style>@media(max-width:760px){{.chat-launcher{{bottom:74px}}.chat-panel{{bottom:64px;max-height:calc(100vh - 80px)}}}}</style>"
        if "gallery" in enabled_set:
            head_extra += "<style>.page-gallery-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(180px,1fr));gap:12px;margin-top:6px}.page-gallery-grid figure{margin:0}.page-gallery-grid img{width:100%;border-radius:10px;display:block}</style>"
        page = page.replace("</head>", head_extra + "</head>")
        if not preview:
            self.db.log_product_event(tenant_id, product["id"], "page_view", site_key=(site["site_key"] if site else ""), detail="")
        return page

    def public_source(self, slug: str, source_id: str, site: sqlite3.Row | None = None, preview: bool = False, log_events: bool = True) -> None:
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
        inline_mode = preview and previewable
        if log_events:
            self.db.log_product_event(tenant_id, product["id"], "download", site_key=(site["site_key"] if site else ""), detail=f"{source['filename']};{'inline' if inline_mode else 'attachment'}")
        self.send_bytes(content, content_type, source["filename"], inline=inline_mode)


def make_server(
    db_path: str | os.PathLike[str],
    host: str = "127.0.0.1",
    port: int = 8000,
    ai_config: AIConfig | None = None,
    demo_tools: bool = False,
) -> ThreadingHTTPServer:
    database = Database(db_path)
    # 模式显式：不传配置时读取环境变量（默认 rules）；deepseek 缺 Key 在这里明确失败。
    config = ai_config if ai_config is not None else AIConfig.from_env()
    ai_service = AIService(config)
    # 演示工具（页面按钮 + seed 端点）为部署级开关：默认关闭；唯一开启方式 = tools/demo/start_demo.py 传入 demo_tools=True。
    demo_enabled = bool(demo_tools)

    class AppHandler(Handler):
        db = database
        ai = ai_service
        demo_tools = demo_enabled

    return ThreadingHTTPServer((host, port), AppHandler)


def main() -> None:
    parser = argparse.ArgumentParser(description="Growave MVP")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--ai-mode", choices=list(ai_client.MODES), default=None, help="覆盖 AI_MODE 环境变量（默认 rules）")
    args = parser.parse_args()
    try:
        ai_config = AIConfig.from_env(mode_override=args.ai_mode)
        server = make_server(args.db, args.host, args.port, ai_config=ai_config)
    except AIConfigurationError as error:
        # deepseek 配置不完整时拒绝启动：不允许静默落回规则结果。
        print(f"启动失败：{error}")
        raise SystemExit(2)
    print(f"Growave running at http://{args.host}:{args.port}（AI 模式：{ai_config.mode}）")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
