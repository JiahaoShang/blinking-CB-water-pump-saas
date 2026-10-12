"""企业知识第二批 · 主题/关系/版本与真实统一入库服务（任务1 + 任务2 后端切片）。

- 设计依据：docs/企业知识管理-PRD与交互设计-v0.3-主题与资料评审稿.md（§2/§4/§6）、
  v0.2 保留的文件/模板规则，以及技术计划
  docs/superpowers/plans/2026-10-12-enterprise-knowledge-phase2.md（任务1/2；Interfaces 已同步说明）。
- 侧车表（不重构第一批知识表，不复制产品行）：
    kb2_groups / kb2_topics / kb2_topic_versions   主题、可选分组、正文版本（逐段来源钉住）
    kb2_topic_relations                            主题 ↔ 资料（文章/FAQ/产品只读适配）多对多关联
    kb2_batches / kb2_sources / kb2_paragraphs     统一入库任务、私有原件、逐段整理与核对
    kb2_source_fields / kb2_topic_suggestions      字段来源（真实 source/version/location）与主题建议
- 权限（服务层逐次强制）：本企业成员可读；写入（分组/主题/关联/批次/核对/发布）必须是
  企业管理员或获「products（产品管理）」模块授权的在职成员；全部读写带 tenant_id，跨企业拒绝。
- 规则要点：新资料/主题默认 internal + AI 关闭；完成识别只创建草稿，绝不自动发布；
  排除段不进入知识/AI；发布需有效已核对内容（未核对/空正文拦截）；仅登记原件可归档发布但
  AI 不可用、不算发布知识正文；来源版本钉住——资料更新不改旧主题正文出处（标记换取版）。
- 任务状态（批次/来源）与知识生命周期（草稿/已发布/已停用）分开存储，数据库是唯一事实来源；
  处理可恢复（崩溃后 kb2_process_pending_sources 从数据库继续）、失败重试幂等（不重复建条目）。
- 并发（B2-B01 修复）：处理采用「独占认领 + 租约」——同一来源同一时刻只有一个执行者
  （进程内活动认领表互斥 + 数据层条件更新兜底；处理中行仅在属主失活或租约过期时可接管）；
  排队入口 kb2_create_batch(process_now=False) 立即返回 batch_id，由入库线程真实处理。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import socket
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import knowledge_ingestion
from enterprise_knowledge import (
    KNOWLEDGE_DOCUMENT_EXTENSIONS,
    KNOWLEDGE_DOCUMENT_FILE_LIMIT,
    KNOWLEDGE_FORMS,
    KNOWLEDGE_FORM_LABELS,
    KNOWLEDGE_TEXT_EXTENSIONS,
    KNOWLEDGE_TEXT_FILE_LIMIT,
    KNOWLEDGE_USAGES,
    _UNSET,
    _now,
    knowledge_content_projection,
    knowledge_expiry_instant,
    normalize_knowledge_expiry,
)

KB2_MATERIAL_KINDS = ("knowledge", "product")
KB2_TOPIC_STATUSES = ("draft", "published", "disabled")
KB2_SUGGESTION_STATUSES = ("pending", "merged", "rejected", "confirmed")
KB2_SOURCE_STATUSES = ("pending", "processing", "extracted", "registered", "failed", "published")
# 段落 origin：manual=人工撰写；from_material=来源摘录（文字须与来源内容一致）；
# edited=人工改写（保留来源依据，文字可不同）；extracted=系统来源摘录（同 from_material 校验）。
KB2_PARAGRAPH_ORIGINS = ("manual", "from_material", "edited", "extracted")
KB2_MATERIAL_DERIVED_ORIGINS = ("from_material", "edited", "extracted")
# 批次件数沿用既有产品批量上传的技术上限（app.py MAX_BATCH_SOURCE_FILES = 20）。
KB2_MAX_BATCH_ENTRIES = 20
KB2_LINK_KINDS = ("file", "link", "manual")
KB2_SUPPORTED_ENTRY_EXTENSIONS = (
    knowledge_ingestion.TEXT_EXTENSIONS
    + knowledge_ingestion.PDF_EXTENSIONS
    + knowledge_ingestion.DOCX_EXTENSIONS
    + knowledge_ingestion.REGISTER_ONLY_EXTENSIONS
)

KB2_FORMAT_LABELS = {
    "pdf": "PDF", "docx": "DOCX", "doc": "DOC", "txt": "TXT", "md": "Markdown", "csv": "CSV",
    "image": "图片", "video": "视频", "web": "网页", "manual": "手动文字", "other": "其他",
}
# 灵活字段（任务4）：五种简单类型分组（单/多行文字、数字＋单位、选项、日期、是/否）。
KB2_FIELD_TYPES = ("text", "textarea", "number", "select", "date", "bool")
KB2_FIELD_TYPE_LABELS = {
    "text": "单行文字", "textarea": "多行文字", "number": "数字＋单位",
    "select": "选项", "date": "日期", "bool": "是/否",
}
KB2_IDENTIFICATION_LABELS = {
    "extracted": "已识别（可核对）", "manual": "人工正文", "registered": "仅登记原件",
    "failed": "未能识别", "processing": "处理中", "pending": "待处理",
}

# ── 并发处理认领（B2-B01 修复：同一来源同一时刻只有一个执行者）────────────────
# 场景：单一服务器进程内，「同步创建入口 + 后台入库线程 + 其他连接」可能并发处理同一来源。
# - 进程内互斥：模块级活动认领表（同一数据库真实路径 + 企业 + 来源），执行线程存活即视为持有；
# - 数据层兜底：kb2_sources 条件更新；处理中行仅在属主失活（线程终止 / 进程退出）或租约过期时接管；
# - 崩溃恢复：认领与结果都是数据库行状态，条目身份（item_id / knowledge_source_id）条件写入且唯一，
#   中断后由恢复入口接管，不重复解析、不重复创建知识条目。
KB2_PROCESSING_LEASE_SECONDS = 600  # 「处理中」租约（秒）：跨进程属主失活兜底的接管期限
_KB2_HOSTNAME = socket.gethostname()
_KB2_CLAIMS_LOCK = threading.RLock()
_KB2_ACTIVE_CLAIMS = {}  # (真实库路径, tenant_id, source_id) -> (认领令牌, 本进程执行线程)


def _kb2_new_claim_token() -> str:
    """认领令牌：主机:进程:线程:随机（用于属主存活判断，不承担其他语义）。"""
    return f"{_KB2_HOSTNAME}:{os.getpid()}:{threading.get_ident()}:{secrets.token_hex(8)}"


def _kb2_pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # 存在但无权限：视为存活（不能作为接管依据）
    except OSError:
        return False
    return True


def _json_load(text, fallback):
    try:
        value = json.loads(text or "")
    except (TypeError, ValueError):
        return fallback
    return value


class EnterpriseKnowledgePhase2:
    """Database 的第二批知识 mixin；依赖第一批 EnterpriseKnowledge 的权限与知识表。

    依赖的宿主方法（由 Database 的 EnterpriseKnowledge 基类提供）：
    _require_knowledge_editor / get_knowledge_item / get_knowledge_version / list_knowledge_items /
    list_knowledge_sources / read_knowledge_source_file / publish_knowledge_draft / _audit 等。
    """

    # ── 初始化 ────────────────────────────────────────────────────────────

    def init_knowledge_phase2(self) -> None:
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS kb2_groups (
                id TEXT PRIMARY KEY,
                tenant_id TEXT NOT NULL REFERENCES tenants(id),
                name TEXT NOT NULL,
                created_by TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_kb2_groups_tenant ON kb2_groups(tenant_id, name);

            CREATE TABLE IF NOT EXISTS kb2_topics (
                id TEXT PRIMARY KEY,
                tenant_id TEXT NOT NULL REFERENCES tenants(id),
                title TEXT NOT NULL,
                group_id TEXT,
                status TEXT NOT NULL DEFAULT 'draft',
                published_version_id TEXT,
                draft_version_id TEXT,
                owner_user_id TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_kb2_topics_tenant ON kb2_topics(tenant_id, status);

            CREATE TABLE IF NOT EXISTS kb2_topic_versions (
                id TEXT PRIMARY KEY,
                tenant_id TEXT NOT NULL REFERENCES tenants(id),
                topic_id TEXT NOT NULL REFERENCES kb2_topics(id),
                version INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'draft',
                content_json TEXT NOT NULL DEFAULT '{}',
                usage TEXT NOT NULL DEFAULT 'internal',
                ai_authorized INTEGER NOT NULL DEFAULT 0,
                expires_at TEXT,
                restored_from_version INTEGER,
                created_by TEXT NOT NULL,
                created_at TEXT NOT NULL,
                published_at TEXT,
                UNIQUE (topic_id, version)
            );

            CREATE TABLE IF NOT EXISTS kb2_topic_relations (
                id TEXT PRIMARY KEY,
                tenant_id TEXT NOT NULL REFERENCES tenants(id),
                topic_id TEXT NOT NULL REFERENCES kb2_topics(id),
                material_kind TEXT NOT NULL,
                material_id TEXT NOT NULL,
                pinned_version_id TEXT,
                pinned_source_id TEXT,
                pinned_version_number INTEGER,
                origin TEXT NOT NULL DEFAULT 'manual',
                linked_by TEXT NOT NULL DEFAULT '',
                linked_at TEXT NOT NULL,
                UNIQUE (tenant_id, topic_id, material_kind, material_id)
            );
            CREATE INDEX IF NOT EXISTS idx_kb2_relations_topic ON kb2_topic_relations(tenant_id, topic_id);
            CREATE INDEX IF NOT EXISTS idx_kb2_relations_material ON kb2_topic_relations(tenant_id, material_kind, material_id);

            CREATE TABLE IF NOT EXISTS kb2_batches (
                id TEXT PRIMARY KEY,
                tenant_id TEXT NOT NULL REFERENCES tenants(id),
                status TEXT NOT NULL DEFAULT 'draft',
                usage TEXT NOT NULL DEFAULT 'internal',
                ai_authorized INTEGER NOT NULL DEFAULT 0,
                expires_at TEXT,
                created_by TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                confirmed_at TEXT,
                published_at TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_kb2_batches_tenant ON kb2_batches(tenant_id, status);

            CREATE TABLE IF NOT EXISTS kb2_sources (
                id TEXT PRIMARY KEY,
                tenant_id TEXT NOT NULL REFERENCES tenants(id),
                batch_id TEXT NOT NULL REFERENCES kb2_batches(id),
                kind TEXT NOT NULL,
                filename TEXT NOT NULL DEFAULT '',
                title TEXT NOT NULL DEFAULT '',
                format TEXT NOT NULL DEFAULT '',
                size INTEGER NOT NULL DEFAULT 0,
                sha256 TEXT,
                storage_path TEXT,
                source_location TEXT NOT NULL DEFAULT '',
                payload_text TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'pending',
                error TEXT NOT NULL DEFAULT '',
                warnings_json TEXT NOT NULL DEFAULT '[]',
                attempts INTEGER NOT NULL DEFAULT 0,
                processing_owner TEXT,
                processing_lease_until REAL,
                item_id TEXT,
                knowledge_source_id TEXT,
                created_by TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_kb2_sources_batch ON kb2_sources(tenant_id, batch_id);
            CREATE INDEX IF NOT EXISTS idx_kb2_sources_status ON kb2_sources(tenant_id, status);

            CREATE TABLE IF NOT EXISTS kb2_paragraphs (
                id TEXT PRIMARY KEY,
                tenant_id TEXT NOT NULL REFERENCES tenants(id),
                source_id TEXT NOT NULL REFERENCES kb2_sources(id),
                item_id TEXT,
                ordinal INTEGER NOT NULL,
                text TEXT NOT NULL DEFAULT '',
                location_json TEXT NOT NULL DEFAULT '{}',
                source_kind TEXT NOT NULL DEFAULT '',
                origin TEXT NOT NULL DEFAULT 'extracted',
                edited INTEGER NOT NULL DEFAULT 0,
                checked INTEGER NOT NULL DEFAULT 0,
                excluded INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE (source_id, ordinal)
            );
            CREATE INDEX IF NOT EXISTS idx_kb2_paragraphs_source ON kb2_paragraphs(tenant_id, source_id);

            CREATE TABLE IF NOT EXISTS kb2_source_fields (
                id TEXT PRIMARY KEY,
                tenant_id TEXT NOT NULL REFERENCES tenants(id),
                source_id TEXT NOT NULL REFERENCES kb2_sources(id),
                item_id TEXT,
                label TEXT NOT NULL,
                value TEXT NOT NULL DEFAULT '',
                unit TEXT NOT NULL DEFAULT '',
                paragraph_id TEXT REFERENCES kb2_paragraphs(id),
                checked INTEGER NOT NULL DEFAULT 0,
                created_by TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_kb2_source_fields_source ON kb2_source_fields(tenant_id, source_id);

            CREATE TABLE IF NOT EXISTS kb2_topic_suggestions (
                id TEXT PRIMARY KEY,
                tenant_id TEXT NOT NULL REFERENCES tenants(id),
                batch_id TEXT NOT NULL REFERENCES kb2_batches(id),
                name TEXT NOT NULL,
                existing_topic_id TEXT,
                basis TEXT NOT NULL DEFAULT 'text_match',
                source_ids_json TEXT NOT NULL DEFAULT '[]',
                status TEXT NOT NULL DEFAULT 'pending',
                created_by TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_kb2_suggestions_batch ON kb2_topic_suggestions(tenant_id, batch_id);

            CREATE TABLE IF NOT EXISTS kb2_topic_links (
                id TEXT PRIMARY KEY,
                tenant_id TEXT NOT NULL REFERENCES tenants(id),
                topic_id TEXT NOT NULL REFERENCES kb2_topics(id),
                related_topic_id TEXT NOT NULL REFERENCES kb2_topics(id),
                created_by TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                UNIQUE (tenant_id, topic_id, related_topic_id)
            );
            CREATE INDEX IF NOT EXISTS idx_kb2_topic_links_topic ON kb2_topic_links(tenant_id, topic_id);

            CREATE TABLE IF NOT EXISTS kb2_templates (
                id TEXT PRIMARY KEY,
                tenant_id TEXT NOT NULL REFERENCES tenants(id),
                name TEXT NOT NULL,
                form TEXT NOT NULL DEFAULT '',
                created_by TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE (tenant_id, name)
            );

            CREATE TABLE IF NOT EXISTS kb2_template_versions (
                id TEXT PRIMARY KEY,
                tenant_id TEXT NOT NULL REFERENCES tenants(id),
                template_id TEXT NOT NULL REFERENCES kb2_templates(id),
                version INTEGER NOT NULL,
                fields_json TEXT NOT NULL DEFAULT '[]',
                note TEXT NOT NULL DEFAULT '',
                created_by TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                UNIQUE (template_id, version)
            );

            CREATE TABLE IF NOT EXISTS kb2_item_template (
                item_id TEXT PRIMARY KEY,
                tenant_id TEXT NOT NULL REFERENCES tenants(id),
                template_version_id TEXT NOT NULL REFERENCES kb2_template_versions(id),
                adopted_by TEXT NOT NULL DEFAULT '',
                adopted_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS kb2_item_fields (
                id TEXT PRIMARY KEY,
                tenant_id TEXT NOT NULL REFERENCES tenants(id),
                item_id TEXT NOT NULL,
                field_key TEXT NOT NULL,
                ordinal INTEGER NOT NULL DEFAULT 0,
                label TEXT NOT NULL,
                type TEXT NOT NULL,
                required INTEGER NOT NULL DEFAULT 0,
                hint TEXT NOT NULL DEFAULT '',
                unit TEXT NOT NULL DEFAULT '',
                options_json TEXT NOT NULL DEFAULT '[]',
                value TEXT,
                source_json TEXT,
                checked INTEGER NOT NULL DEFAULT 0,
                allow_customer INTEGER NOT NULL DEFAULT 0,
                removed INTEGER NOT NULL DEFAULT 0,
                template_version_id TEXT NOT NULL,
                created_by TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_kb2_item_fields_item ON kb2_item_fields(tenant_id, item_id);
            """
        )
        # 既有库补齐并发认领列（B2-B01 修复）：幂等迁移，旧库首次打开即生效。
        source_columns = {row["name"] for row in self.conn.execute("PRAGMA table_info(kb2_sources)")}
        if "processing_owner" not in source_columns:
            self.conn.execute("ALTER TABLE kb2_sources ADD COLUMN processing_owner TEXT")
        if "processing_lease_until" not in source_columns:
            self.conn.execute("ALTER TABLE kb2_sources ADD COLUMN processing_lease_until REAL")
        # 文件更新（任务4）：记录被替换前的线上版本，供差异对照；批次记录用途以便界面区分。
        if "replace_of_version_id" not in source_columns:
            self.conn.execute("ALTER TABLE kb2_sources ADD COLUMN replace_of_version_id TEXT")
        batch_columns = {row["name"] for row in self.conn.execute("PRAGMA table_info(kb2_batches)")}
        if "purpose" not in batch_columns:
            self.conn.execute("ALTER TABLE kb2_batches ADD COLUMN purpose TEXT NOT NULL DEFAULT 'upload'")
        self.conn.commit()

    # ── 内部查询 / 校验帮助 ───────────────────────────────────────────────

    def _kb2_topic_row(self, tenant_id: str, topic_id: str):
        return self.conn.execute(
            "SELECT * FROM kb2_topics WHERE tenant_id=? AND id=?", (tenant_id, topic_id)
        ).fetchone()

    def _kb2_require_topic(self, tenant_id: str, topic_id: str):
        row = self._kb2_topic_row(tenant_id, topic_id)
        if not row:
            raise ValueError("主题不存在或不属于本企业")
        return row

    def _kb2_group_row(self, tenant_id: str, group_id: str):
        return self.conn.execute(
            "SELECT * FROM kb2_groups WHERE tenant_id=? AND id=?", (tenant_id, group_id)
        ).fetchone()

    def _kb2_batch_row(self, tenant_id: str, batch_id: str):
        return self.conn.execute(
            "SELECT * FROM kb2_batches WHERE tenant_id=? AND id=?", (tenant_id, batch_id)
        ).fetchone()

    def _kb2_require_batch(self, tenant_id: str, batch_id: str):
        row = self._kb2_batch_row(tenant_id, batch_id)
        if not row:
            raise ValueError("批次不存在或不属于本企业")
        return row

    def _kb2_source_row(self, tenant_id: str, source_id: str):
        return self.conn.execute(
            "SELECT * FROM kb2_sources WHERE tenant_id=? AND id=?", (tenant_id, source_id)
        ).fetchone()

    def _kb2_require_source(self, tenant_id: str, source_id: str):
        row = self._kb2_source_row(tenant_id, source_id)
        if not row:
            raise ValueError("来源任务不存在或不属于本企业")
        return row

    def _kb2_topic_version_row(self, tenant_id: str, topic_id: str, *, draft=False, published=False):
        if draft:
            return self.conn.execute(
                "SELECT v.* FROM kb2_topic_versions v JOIN kb2_topics t ON t.draft_version_id = v.id "
                "WHERE t.tenant_id=? AND t.id=?",
                (tenant_id, topic_id),
            ).fetchone()
        if published:
            return self.conn.execute(
                "SELECT * FROM kb2_topic_versions WHERE tenant_id=? AND topic_id=? AND status='published' "
                "ORDER BY version DESC LIMIT 1",
                (tenant_id, topic_id),
            ).fetchone()
        return None

    def _kb2_snapshot_registered_only(self, item, content: dict) -> bool:
        """资料是否「仅登记原件」：没有可核对正文（含显式登记标记）。"""
        if content.get("kb2_registered_only"):
            return True
        projection = knowledge_content_projection(item["form"], content).strip()
        title = str(content.get("title") or "").strip()
        if title and projection == title:
            projection = ""
        return not projection

    def _kb2_material_snapshot(self, tenant_id: str, material_kind: str, material_id: str):
        """资料只读快照（不复制行）：标题、状态、是否符合登记、当前版本钉住信息。"""
        if material_kind == "knowledge":
            item = self.get_knowledge_item(tenant_id, material_id)
            if not item:
                return None
            draft = self.get_knowledge_draft_version(tenant_id, material_id)
            published = self.get_published_knowledge_version(tenant_id, material_id)
            reference = draft or published
            content = _json_load(reference["content_json"], {}) if reference else {}
            return {
                "kind": "knowledge",
                "id": material_id,
                "title": str(content.get("title") or "").strip() or "(无标题资料)",
                "form": item["form"],
                "form_label": KNOWLEDGE_FORM_LABELS.get(item["form"], item["form"]),
                "status": item["status"],
                "registered_only": self._kb2_snapshot_registered_only(item, content),
                "pinned_version_id": reference["id"] if reference else None,
                "pinned_version_number": reference["version"] if reference else None,
                "pinned_source_id": reference["source_id"] if reference else None,
            }
        if material_kind == "product":
            product = self.get_product(tenant_id, material_id)
            if not product:
                return None
            version = self.get_published_version(tenant_id, material_id)
            return {
                "kind": "product",
                "id": material_id,
                "title": product["name"] or product["model"],
                "form": "product",
                "form_label": "产品",
                "status": product["status"],
                "registered_only": False,
                "model": product["model"],
                "pinned_version_id": version["id"] if version else None,
                "pinned_version_number": version["version"] if version else None,
                "pinned_source_id": None,
            }
        return None

    @staticmethod
    def kb2_location_label(location) -> str:
        """位置显示文案（只表述可确定的位置；DOCX 不虚构页号）。"""
        if not isinstance(location, dict):
            return ""
        kind = location.get("kind")
        if kind == "page":
            return f"第 {location.get('page')} 页"
        if kind == "paragraph":
            label = f"第 {location.get('paragraph')} 段"
            section = str(location.get("section") or "").strip()
            if section:
                label += f"（{section}）"
            return label
        if kind == "table":
            return f"表格 {location.get('table')} 第 {location.get('row')} 行第 {location.get('col')} 列"
        if kind == "line":
            return f"第 {location.get('line')} 行"
        if kind == "manual":
            return "人工撰写"
        return ""

    def _kb2_source_display(self, tenant_id: str, source: dict) -> tuple[str, bool]:
        """主题正文某一段的来源显示标签与「来源资料已更新（换取版）」标记。"""
        if not source:
            return "", False
        kind = source.get("kind")
        location_label = self.kb2_location_label(source.get("location") or {})
        if kind == "knowledge":
            snapshot = self._kb2_material_snapshot(tenant_id, "knowledge", str(source.get("item_id") or ""))
            if snapshot is None:
                return "来源资料已不存在", True
            label = snapshot["title"]
            if location_label:
                label += f" · {location_label}"
            pinned = source.get("version_id")
            stale = bool(pinned) and snapshot["pinned_version_id"] != pinned
            if stale:
                label += "（来源资料已更新）"
            return label, stale
        if kind == "product":
            snapshot = self._kb2_material_snapshot(tenant_id, "product", str(source.get("product_id") or ""))
            if snapshot is None:
                return "来源产品已不存在", True
            label = snapshot["title"]
            version_number = source.get("version_number")
            stale = version_number is not None and snapshot["pinned_version_number"] != version_number
            if stale:
                label += "（产品已更新）"
            return label, stale
        if kind == "manual":
            return "人工撰写", False
        return "", False

    @staticmethod
    def _kb2_location_signature(location) -> tuple:
        """位置的可比对签名（只取可确定字段；忽略 section 等展示辅助信息）。"""
        if not isinstance(location, dict):
            return ()
        kind = str(location.get("kind") or "")
        try:
            if kind == "line":
                return ("line", int(location.get("line")))
            if kind == "page":
                return ("page", int(location.get("page")))
            if kind == "paragraph":
                return ("paragraph", int(location.get("paragraph")))
            if kind == "table":
                return ("table", int(location.get("table")), int(location.get("row")), int(location.get("col")))
            if kind == "manual":
                return ("manual",)
        except (TypeError, ValueError):
            return ("invalid", kind)
        return ("invalid", kind or "missing")

    def _kb2_validate_pin_location(self, tenant_id: str, version, ws, location, text: str,
                                   *, require_text_match: bool) -> None:
        """实际位置校验（B2-B03）：不许伪造页码/行号，文字必须与所指位置的实际内容一致。

        - 由第二批入库产生的来源：位置必须对应原件识别出的真实段落（逐段 location 精确匹配）；
          摘录类段落（from_material）的文字必须与该段落的实际文字一致。
        - 第一批适配来源（登记原件/手工登记，无第二轮识别结果）：文本型原件按版本正文行核对；
          手工登记只接受「人工撰写」位置；PDF/Word 等仅登记的原件没有可核对位置，明确拒绝，
          不猜测位置、也不允许把不存在的页码当作证据。
        """
        kind = str(location.get("kind") or "")
        if kind not in ("line", "page", "paragraph", "table", "manual"):
            raise ValueError("段落来源位置类型无效")
        if self._kb2_location_signature(location) == ("invalid", kind):
            raise ValueError("段落来源位置格式无效")
        kb2_source = self.conn.execute(
            "SELECT * FROM kb2_sources WHERE tenant_id=? AND knowledge_source_id=?", (tenant_id, ws["id"])
        ).fetchone()
        if kb2_source is not None:
            rows = self.conn.execute(
                "SELECT * FROM kb2_paragraphs WHERE tenant_id=? AND source_id=? ORDER BY ordinal",
                (tenant_id, kb2_source["id"]),
            ).fetchall()
            target = self._kb2_location_signature(location)
            matched = next(
                (row for row in rows
                 if self._kb2_location_signature(_json_load(row["location_json"], {})) == target), None)
            if matched is None:
                raise ValueError("段落来源位置不在该原件的识别结果中（不能使用不存在的页码/行号/段落）")
            if require_text_match and text.strip():
                actual = str(matched["text"] or "")
                if text.strip() != actual.strip():
                    raise ValueError(
                        "段落文字与来源位置的实际内容不一致：如为人工改写请把该段标记为「人工改写」，"
                        "或修正文字后重新核对")
            return
        # 第一批适配来源：按所钉住版本的正文核对（版本内容是该版本的材料记录）。
        source_kind = str(ws["kind"] or "")
        file_type = str(ws["file_type"] or "").strip().lower()
        body = str(_json_load(version["content_json"], {}).get("body") or "")
        if source_kind == "manual":
            if kind != "manual":
                raise ValueError("该来源是手工登记（没有原件文件）：位置只能是「人工撰写」")
            if require_text_match and text.strip() and text.strip() not in body:
                raise ValueError("段落文字不在该版本正文中：不能把这段当作该来源的摘录")
            return
        if source_kind != "file":
            raise ValueError("该来源没有可核对的位置信息：请改标人工撰写，或通过新的资料录入重新整理")
        if file_type in ("txt", "md", "markdown", "csv", "text"):
            if kind != "line":
                raise ValueError("文本原件的可核对位置是行号（页码/段落等位置无法在该原件中核对）")
            line_no = int(location["line"])
            lines = body.splitlines()
            if not (1 <= line_no <= len(lines)):
                raise ValueError("段落来源行号超出该版本正文范围（不能使用不存在的行号）")
            if require_text_match and text.strip() and text.strip() != lines[line_no - 1].strip():
                raise ValueError("段落文字与来源行号的实际内容不一致：如为人工改写请把该段标记为「人工改写」")
            return
        raise ValueError("该登记原件（PDF/Word 等）没有可核对的位置信息：请通过新的资料录入重新整理，"
                         "或将该段标记为人工撰写/人工改写")

    def _kb2_normalize_paragraph_source(self, tenant_id: str, origin: str, source, *,
                                        text: str = "", text_match: bool = True):
        """逐段来源校验与规范化（B2-B03 修复）。

        资料摘录/人工改写段落必须钉住「确切版本 + 登记原件 + 实际位置」：
        - 缺版本、缺原件、缺位置、仅给资料 ID、位置不存在、原件与版本错配、文字与位置内容不一致，
          一律拒绝保存（不能把不存在的证据写成出处）；
        - text_match=True 时（新段落，或出处被改动）摘录文字必须与所指位置实际内容一致；
        - 人工撰写段落保持人工撰写，不能冒用资料出处；产品沿既有只读适配（产品身份不被复制改写）。
        """
        material_derived = origin in KB2_MATERIAL_DERIVED_ORIGINS
        if source in (None, ""):
            if material_derived:
                raise ValueError("资料摘录段落必须标注来源：资料 + 确切版本 + 登记原件 + 实际位置")
            return None
        if not isinstance(source, dict):
            raise ValueError("段落来源格式无效")
        kind = str(source.get("kind") or "")
        if kind == "knowledge":
            if origin == "manual":
                raise ValueError("人工撰写段落不能冒用资料出处：请标记为资料摘录或人工改写")
            item_id = str(source.get("item_id") or "").strip()
            if not item_id:
                raise ValueError("来源缺少资料 ID")
            item = self.get_knowledge_item(tenant_id, item_id)
            if not item:
                raise ValueError("段落来源资料不存在或不属于本企业")
            if not material_derived:
                raise ValueError("段落来源类型无效")
            version_id = str(source.get("version_id") or "").strip()
            source_id = str(source.get("source_id") or "").strip()
            location = source.get("location")
            if not version_id:
                raise ValueError("段落来源缺少确切版本（资料摘录必须钉住版本）")
            if not source_id:
                raise ValueError("段落来源缺少登记原件（资料摘录必须钉住原件）")
            if not isinstance(location, dict) or not location.get("kind"):
                raise ValueError("段落来源缺少实际位置（资料摘录必须记录位置）")
            version = self.conn.execute(
                "SELECT * FROM knowledge_versions WHERE tenant_id=? AND item_id=? AND id=?",
                (tenant_id, item_id, version_id),
            ).fetchone()
            if not version:
                raise ValueError("段落来源版本不属于该资料")
            ws = self.conn.execute(
                "SELECT * FROM knowledge_sources WHERE tenant_id=? AND item_id=? AND id=?",
                (tenant_id, item_id, source_id),
            ).fetchone()
            if not ws:
                raise ValueError("段落来源原件不属于该资料")
            if str(version["source_id"] or "") != source_id:
                raise ValueError(
                    "段落来源原件与该版本不匹配（同一资料下也不允许错配的来源/版本）："
                    "请选择该版本实际关联的原件")
            location = dict(location)
            self._kb2_validate_pin_location(
                tenant_id, version, ws, location, text,
                require_text_match=(text_match and origin in ("from_material", "extracted")))
            snapshot = self._kb2_material_snapshot(tenant_id, "knowledge", item_id)
            return {
                "kind": "knowledge", "item_id": item_id, "version_id": version_id,
                "source_id": source_id, "location": location,
                "material_title": snapshot["title"] if snapshot else "",
            }
        if kind == "product":
            if origin == "manual":
                raise ValueError("人工撰写段落不能冒用产品出处：请标记为资料摘录或人工改写")
            product_id = str(source.get("product_id") or "").strip()
            if not product_id:
                raise ValueError("来源缺少产品 ID")
            product = self.get_product(tenant_id, product_id)
            if not product:
                raise ValueError("段落来源产品不存在或不属于本企业")
            normalized: dict = {"kind": "product", "product_id": product_id}
            if source.get("version_number") is not None:
                try:
                    normalized["version_number"] = int(source["version_number"])
                except (TypeError, ValueError):
                    raise ValueError("产品来源版本号格式无效") from None
            return normalized
        if kind == "manual":
            if material_derived:
                raise ValueError("资料摘录段落缺少资料出处：请选择资料与位置，或把该段标记为人工撰写")
            return {"kind": "manual"}
        raise ValueError("段落来源类型无效")

    # ── 分组（可选辅助导航；删除只解除分组）────────────────────────────────

    def kb2_create_group(self, tenant_id: str, user_id: str, name: str) -> str:
        editor = self._require_knowledge_editor(tenant_id, user_id)
        name = str(name or "").strip()
        if not name:
            raise ValueError("分组名称不能为空")
        group_id = secrets.token_hex(12)
        timestamp = _now()
        self.conn.execute(
            "INSERT INTO kb2_groups(id, tenant_id, name, created_by, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
            (group_id, tenant_id, name, editor["email"], timestamp, timestamp),
        )
        self._audit(tenant_id, "kb2", group_id, "group_created", editor["email"], f"name={name}")
        self.conn.commit()
        return group_id

    def kb2_rename_group(self, tenant_id: str, user_id: str, group_id: str, name: str) -> None:
        editor = self._require_knowledge_editor(tenant_id, user_id)
        if not self._kb2_group_row(tenant_id, group_id):
            raise ValueError("分组不存在或不属于本企业")
        name = str(name or "").strip()
        if not name:
            raise ValueError("分组名称不能为空")
        self.conn.execute(
            "UPDATE kb2_groups SET name=?, updated_at=? WHERE tenant_id=? AND id=?",
            (name, _now(), tenant_id, group_id),
        )
        self._audit(tenant_id, "kb2", group_id, "group_renamed", editor["email"], f"name={name}")
        self.conn.commit()

    def kb2_delete_group(self, tenant_id: str, user_id: str, group_id: str) -> None:
        """删除分组只解除分组：主题与关联全部保留（不给企业强制行业分类）。"""
        editor = self._require_knowledge_editor(tenant_id, user_id)
        if not self._kb2_group_row(tenant_id, group_id):
            raise ValueError("分组不存在或不属于本企业")
        timestamp = _now()
        self.conn.execute(
            "UPDATE kb2_topics SET group_id=NULL, updated_at=? WHERE tenant_id=? AND group_id=?",
            (timestamp, tenant_id, group_id),
        )
        self.conn.execute("DELETE FROM kb2_groups WHERE tenant_id=? AND id=?", (tenant_id, group_id))
        self._audit(tenant_id, "kb2", group_id, "group_deleted", editor["email"], "主题保留为未分组")
        self.conn.commit()

    def kb2_list_groups(self, tenant_id: str) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM kb2_groups WHERE tenant_id=? ORDER BY created_at, id", (tenant_id,)
        ).fetchall()
        return [
            {"id": row["id"], "name": row["name"], "created_by": row["created_by"],
             "created_at": row["created_at"], "updated_at": row["updated_at"]}
            for row in rows
        ]

    # ── 主题（草稿 / 发布 / 历史；正文逐段来源钉住）──────────────────────────

    def kb2_create_topic(self, tenant_id: str, user_id: str, title: str, group_id: str | None = None) -> str:
        editor = self._require_knowledge_editor(tenant_id, user_id)
        title = str(title or "").strip()
        if not title:
            raise ValueError("主题名称不能为空")
        if group_id:
            if not self._kb2_group_row(tenant_id, group_id):
                raise ValueError("分组不存在或不属于本企业")
        topic_id = secrets.token_hex(12)
        version_id = secrets.token_hex(12)
        timestamp = _now()
        self.conn.execute(
            "INSERT INTO kb2_topics(id, tenant_id, title, group_id, status, published_version_id, draft_version_id, owner_user_id, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, 'draft', NULL, ?, ?, ?, ?)",
            (topic_id, tenant_id, title, group_id, version_id, user_id, timestamp, timestamp),
        )
        content = json.dumps({"title": title, "paragraphs": []}, ensure_ascii=False)
        self.conn.execute(
            "INSERT INTO kb2_topic_versions(id, tenant_id, topic_id, version, status, content_json, usage, ai_authorized, created_by, created_at) "
            "VALUES (?, ?, ?, 1, 'draft', ?, 'internal', 0, ?, ?)",
            (version_id, tenant_id, topic_id, content, editor["email"], timestamp),
        )
        self._audit(tenant_id, "kb2_topic", topic_id, "topic_created", editor["email"], f"title={title}")
        self.conn.commit()
        return topic_id

    def kb2_move_topic(self, tenant_id: str, user_id: str, topic_id: str, group_id: str | None = None) -> None:
        editor = self._require_knowledge_editor(tenant_id, user_id)
        self._kb2_require_topic(tenant_id, topic_id)
        if group_id:
            if not self._kb2_group_row(tenant_id, group_id):
                raise ValueError("分组不存在或不属于本企业")
        self.conn.execute(
            "UPDATE kb2_topics SET group_id=?, updated_at=? WHERE tenant_id=? AND id=?",
            (group_id, _now(), tenant_id, topic_id),
        )
        self._audit(tenant_id, "kb2_topic", topic_id, "topic_moved", editor["email"], f"group={group_id or 'none'}")
        self.conn.commit()

    def kb2_list_topics(self, tenant_id: str) -> list[dict]:
        topics = self.conn.execute(
            "SELECT * FROM kb2_topics WHERE tenant_id=? ORDER BY created_at, id", (tenant_id,)
        ).fetchall()
        relations = self.conn.execute(
            "SELECT * FROM kb2_topic_relations WHERE tenant_id=?", (tenant_id,)
        ).fetchall()
        counts: dict[str, dict] = {}
        for relation in relations:
            bucket = counts.setdefault(relation["topic_id"], {"materials": 0, "registered_only": 0})
            bucket["materials"] += 1
            snapshot = self._kb2_material_snapshot(tenant_id, relation["material_kind"], relation["material_id"])
            if snapshot and snapshot["registered_only"]:
                bucket["registered_only"] += 1
        out = []
        for topic in topics:
            published = self._kb2_topic_version_row(tenant_id, topic["id"], published=True)
            draft = self._kb2_topic_version_row(tenant_id, topic["id"], draft=True)
            bucket = counts.get(topic["id"], {"materials": 0, "registered_only": 0})
            out.append({
                "id": topic["id"], "title": topic["title"], "group_id": topic["group_id"],
                "status": topic["status"], "owner_user_id": topic["owner_user_id"],
                "published_version_number": published["version"] if published else None,
                "draft_version_number": draft["version"] if draft else None,
                "has_draft": draft is not None,
                "materials_count": bucket["materials"],
                "registered_only_count": bucket["registered_only"],
                "created_at": topic["created_at"], "updated_at": topic["updated_at"],
            })
        return out

    def kb2_get_topic(self, tenant_id: str, topic_id: str) -> dict | None:
        topic = self._kb2_topic_row(tenant_id, topic_id)
        if not topic:
            return None
        published = self._kb2_topic_version_row(tenant_id, topic_id, published=True)
        draft = self._kb2_topic_version_row(tenant_id, topic_id, draft=True)
        reference = draft or published
        content = _json_load(reference["content_json"], {}) if reference else {"title": topic["title"], "paragraphs": []}
        content = {"title": content.get("title") or topic["title"], "paragraphs": list(content.get("paragraphs") or [])}
        decorated = []
        for index, paragraph in enumerate(content["paragraphs"], start=1):
            if not isinstance(paragraph, dict):
                continue
            item = dict(paragraph)
            item["label"] = f"第 {index} 段"
            source = item.get("source")
            source_label, stale = self._kb2_source_display(tenant_id, source if isinstance(source, dict) else None)
            if str(item.get("origin") or "") == "edited" and source_label:
                source_label += " · 人工改写"  # 编辑撤销后明确标注成员修改，不暗示与原文一致
            item["source_label"] = source_label
            item["source_stale"] = stale
            decorated.append(item)
        content["paragraphs"] = decorated
        return {
            "id": topic["id"], "title": topic["title"], "group_id": topic["group_id"],
            "status": topic["status"], "owner_user_id": topic["owner_user_id"],
            "published_version_id": published["id"] if published else None,
            "draft_version_id": draft["id"] if draft else None,
            "published_version_number": published["version"] if published else None,
            "draft_version_number": draft["version"] if draft else None,
            "usage": (reference["usage"] if reference else "internal"),
            "ai_authorized": bool(reference["ai_authorized"]) if reference else False,
            "expires_at": reference["expires_at"] if reference else None,
            "content": content,
            "relations": self.kb2_list_relations(tenant_id, topic_id),
            "related_topics": self.kb2_list_topic_links(tenant_id, topic_id),
            "versions": self.kb2_list_topic_versions(tenant_id, topic_id),
            "created_at": topic["created_at"], "updated_at": topic["updated_at"],
        }

    def kb2_list_topic_versions(self, tenant_id: str, topic_id: str) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM kb2_topic_versions WHERE tenant_id=? AND topic_id=? ORDER BY version DESC",
            (tenant_id, topic_id),
        ).fetchall()
        out = []
        for row in rows:
            content = _json_load(row["content_json"], {})
            out.append({
                "id": row["id"], "version": row["version"], "status": row["status"],
                "usage": row["usage"], "ai_authorized": bool(row["ai_authorized"]),
                "expires_at": row["expires_at"], "created_by": row["created_by"],
                "created_at": row["created_at"], "published_at": row["published_at"],
                "restored_from_version": row["restored_from_version"],
                "paragraph_count": len([p for p in content.get("paragraphs") or [] if isinstance(p, dict)]),
                "content_json": row["content_json"],
            })
        return out

    def _kb2_validate_topic_paragraph_sources(self, tenant_id: str, paragraphs: list[dict],
                                              *, existing: list[dict] | None = None) -> list[dict]:
        """逐段来源校验与规范化（B2-B03 修复）。

        编辑已有段落文字 → 撤销该段核对并标注人工改写：提交的旧 checked=true / origin=from_material
        不能绕过硬性撤销（先撤销、成员看过后显式再次核对才能发布）。文字未变的段落沿用已记录的
        origin（保存不再宣称新的出处）；出处被改动的段落按全新出处做完整核对（含文字一致性）。
        """
        existing_by_id: dict[str, dict] = {}
        for paragraph in existing or []:
            if isinstance(paragraph, dict) and paragraph.get("id"):
                existing_by_id[str(paragraph["id"])] = paragraph
        normalized = []
        for index, paragraph in enumerate(paragraphs, start=1):
            if not isinstance(paragraph, dict):
                raise ValueError(f"第 {index} 段格式无效")
            text = str(paragraph.get("text") or "")
            kind = str(paragraph.get("kind") or "intro")
            if kind not in ("intro", "point"):
                kind = "intro"
            origin = str(paragraph.get("origin") or "manual")
            if origin not in KB2_PARAGRAPH_ORIGINS:
                origin = "manual"
            stored = existing_by_id.get(str(paragraph.get("id") or ""))
            if stored is not None:
                stored_origin = str(stored.get("origin") or "manual")
                if stored_origin not in KB2_PARAGRAPH_ORIGINS:
                    stored_origin = "manual"
                stored_text = str(stored.get("text") or "")
                if text != stored_text:
                    # 编辑撤销核对：无论提交什么 checked/origin，都不能以旧核对状态发布改过的文字。
                    checked = False
                    origin = "edited" if stored_origin in KB2_MATERIAL_DERIVED_ORIGINS else stored_origin
                else:
                    # 文字未变：沿用已记录的 origin（编辑撤销过就保持人工改写），仅按提交更新核对状态。
                    checked = bool(paragraph.get("checked"))
                    origin = stored_origin
                text_match = not self._kb2_pin_equal(stored.get("source"), paragraph.get("source"))
            else:
                checked = bool(paragraph.get("checked"))
                text_match = True
            source = self._kb2_normalize_paragraph_source(
                tenant_id, origin, paragraph.get("source"), text=text, text_match=text_match)
            normalized.append({
                "id": str(paragraph.get("id") or secrets.token_hex(8)),
                "kind": kind,
                "text": text,
                "origin": origin,
                "checked": checked,
                "source": source,
            })
        return normalized

    @staticmethod
    def _kb2_pin_equal(stored, incoming) -> bool:
        """出处是否与已记录的一致（忽略展示字段；改出处必须重新做完整核对）。"""

        def canonical(pin):
            if not isinstance(pin, dict):
                return None
            kind = str(pin.get("kind") or "")
            if kind == "knowledge":
                return ("knowledge", str(pin.get("item_id") or ""), str(pin.get("version_id") or ""),
                        str(pin.get("source_id") or ""), json.dumps(pin.get("location") or {}, sort_keys=True))
            if kind == "product":
                return ("product", str(pin.get("product_id") or ""), pin.get("version_number"))
            return kind or None

        return canonical(stored) == canonical(incoming)

    def kb2_save_topic_draft(
        self,
        tenant_id: str,
        user_id: str,
        topic_id: str,
        *,
        paragraphs=_UNSET,
        usage=_UNSET,
        ai_authorized=_UNSET,
        expires_at=_UNSET,
    ) -> str:
        """保存主题正文草稿（不改变当前线上版本）。"""
        editor = self._require_knowledge_editor(tenant_id, user_id)
        topic = self._kb2_require_topic(tenant_id, topic_id)
        if usage is not _UNSET and usage not in KNOWLEDGE_USAGES:
            raise ValueError("使用范围无效")
        if expires_at is not _UNSET:
            expires_at = normalize_knowledge_expiry(expires_at)
        draft = self._kb2_topic_version_row(tenant_id, topic_id, draft=True)
        published = self._kb2_topic_version_row(tenant_id, topic_id, published=True)
        reference = draft or published
        snapshot = _json_load(reference["content_json"], {}) if reference else {}
        if paragraphs is not _UNSET:
            if not isinstance(paragraphs, list):
                raise ValueError("主题正文段落格式无效")
            existing = [p for p in (snapshot.get("paragraphs") or []) if isinstance(p, dict)]
            snapshot["paragraphs"] = self._kb2_validate_topic_paragraph_sources(
                tenant_id, paragraphs, existing=existing)
        snapshot["title"] = topic["title"]
        settings = {
            "usage": reference["usage"] if reference else "internal",
            "ai_authorized": reference["ai_authorized"] if reference else 0,
            "expires_at": reference["expires_at"] if reference else None,
        }
        if usage is not _UNSET:
            settings["usage"] = usage
        if ai_authorized is not _UNSET:
            settings["ai_authorized"] = 1 if ai_authorized else 0
        if expires_at is not _UNSET:
            settings["expires_at"] = expires_at
        content_json = json.dumps(snapshot, ensure_ascii=False)
        timestamp = _now()
        if draft:
            self.conn.execute(
                "UPDATE kb2_topic_versions SET content_json=?, usage=?, ai_authorized=?, expires_at=? "
                "WHERE tenant_id=? AND id=? AND status='draft'",
                (content_json, settings["usage"], settings["ai_authorized"], settings["expires_at"], tenant_id, draft["id"]),
            )
            version_id = draft["id"]
            action = "topic_draft_updated"
        else:
            next_version = self.conn.execute(
                "SELECT COALESCE(MAX(version), 0) + 1 FROM kb2_topic_versions WHERE tenant_id=? AND topic_id=?",
                (tenant_id, topic_id),
            ).fetchone()[0]
            version_id = secrets.token_hex(12)
            self.conn.execute(
                "INSERT INTO kb2_topic_versions(id, tenant_id, topic_id, version, status, content_json, usage, ai_authorized, expires_at, created_by, created_at) "
                "VALUES (?, ?, ?, ?, 'draft', ?, ?, ?, ?, ?, ?)",
                (version_id, tenant_id, topic_id, next_version, content_json, settings["usage"],
                 settings["ai_authorized"], settings["expires_at"], editor["email"], timestamp),
            )
            self.conn.execute(
                "UPDATE kb2_topics SET draft_version_id=?, updated_at=? WHERE tenant_id=? AND id=?",
                (version_id, timestamp, tenant_id, topic_id),
            )
            action = "topic_draft_created"
        self.conn.execute(
            "UPDATE kb2_topics SET updated_at=? WHERE tenant_id=? AND id=?", (timestamp, tenant_id, topic_id)
        )
        self._audit(tenant_id, "kb2_topic", topic_id, action, editor["email"], f"version_row={version_id[:8]}")
        self.conn.commit()
        return version_id

    def kb2_publish_topic_draft(self, tenant_id: str, user_id: str, topic_id: str, *, confirm_ai: bool = False) -> int:
        """发布主题草稿：正文须全部已核对；对外 + 授权 AI 需一次确认；旧线上转历史。"""
        editor = self._require_knowledge_editor(tenant_id, user_id)
        topic = self._kb2_require_topic(tenant_id, topic_id)
        draft = self._kb2_topic_version_row(tenant_id, topic_id, draft=True)
        if not draft:
            raise ValueError("没有待发布的主题草稿")
        snapshot = _json_load(draft["content_json"], {})
        paragraphs = [p for p in snapshot.get("paragraphs") or [] if isinstance(p, dict)]
        included = [p for p in paragraphs if str(p.get("text") or "").strip()]
        if not included:
            raise ValueError("主题正文不能为空：请先填写介绍或要点并核对")
        if not str(snapshot.get("title") or topic["title"] or "").strip():
            raise ValueError("主题名称不能为空")
        unchecked = [index for index, paragraph in enumerate(paragraphs, start=1)
                     if str(paragraph.get("text") or "").strip() and not paragraph.get("checked")]
        if unchecked:
            raise ValueError(
                "还有 %d 段未核对（第 %s 段）：逐段核对后才能发布"
                % (len(unchecked), "、".join(str(i) for i in unchecked)))
        # 发布前复核来源仍是本企业真实资料（换版不拦截，由来源标签如实标注）。
        self._kb2_validate_topic_paragraph_sources(
            tenant_id,
            [
                {"text": p.get("text"), "kind": p.get("kind"), "origin": p.get("origin"),
                 "checked": True, "source": p.get("source")}
                for p in paragraphs
            ],
        )
        if draft["usage"] == "external" and draft["ai_authorized"] and not confirm_ai:
            raise ValueError("对外且授权 AI 客服使用的主题发布需要确认")
        previous = self._kb2_topic_version_row(tenant_id, topic_id, published=True)
        timestamp = _now()
        if previous:
            self.conn.execute("UPDATE kb2_topic_versions SET status='superseded' WHERE id=?", (previous["id"],))
        self.conn.execute(
            "UPDATE kb2_topic_versions SET status='published', published_at=? WHERE id=?",
            (timestamp, draft["id"]),
        )
        self.conn.execute(
            "UPDATE kb2_topics SET status='published', published_version_id=?, draft_version_id=NULL, updated_at=? "
            "WHERE tenant_id=? AND id=?",
            (draft["id"], timestamp, tenant_id, topic_id),
        )
        self._audit(tenant_id, "kb2_topic", topic_id, "topic_published", editor["email"],
                    f"version={draft['version']};usage={draft['usage']};ai_authorized={draft['ai_authorized']}")
        self.conn.commit()
        return draft["version"]

    def _kb2_resolve_source_version(self, item, ws_id: str, text: str, *, current_only: bool = False):
        """为一段来源文字挑选真实包含它的资料版本（表达「确切出处」）。

        只在该版本明确关联这个原件（version.source_id == 原件）且正文真实包含这段文字时选中；
        顺序与全系统「当前版本 = 草稿（若有）否则线上」的口径一致：当前草稿版本优先（成员编辑
        的最新文字落在草稿里，与来源换取版提示 / 重新核对 / 关联快照同一基准），其次当前线上
        版本，再按版本倒序（历史版本仍可核对）。找不到归属版本时返回 None —— 不把新草稿文字挂
        到旧发布版本下，也不把多个原件都挂到同一版本；出处不被静默换版。
        current_only=True（成员重新核实时）：只在当前草稿/当前线上版本里找，不落到历史版本。
        """
        versions = self.conn.execute(
            "SELECT * FROM knowledge_versions WHERE tenant_id=? AND item_id=? AND source_id=? ORDER BY version DESC",
            (item["tenant_id"], item["id"], ws_id),
        ).fetchall()
        if not versions:
            return None
        ordered = []
        for version in versions:
            if version["id"] == item["draft_version_id"]:
                ordered.append(version)
        for version in versions:
            if version["id"] == item["published_version_id"] and version["id"] != item["draft_version_id"]:
                ordered.append(version)
        if not current_only:
            seen = {v["id"] for v in ordered}
            ordered.extend(v for v in versions if v["id"] not in seen)
        needle = text.strip()
        if not needle:
            return None
        for version in ordered:
            body = str(_json_load(version["content_json"], {}).get("body") or "")
            if needle in body:
                return version
        return None

    def kb2_build_topic_draft(self, tenant_id: str, user_id: str, topic_id: str) -> dict:
        """按关联资料整理主题正文初稿（来源摘录编排，只追加、不覆盖人工内容）。

        - 只使用本企业、该主题**已关联资料**中「已核对且未排除」的文字段落，逐段抄录原文与
          确切出处（资料 + 该原件实际所属版本 + 原件 + 实际位置）；每个原件按自己的归属版本
          钉住：不把新草稿文字挂到旧发布版本，也不把多个原件都挂到一个当前版本下；
        - 新段落一律「待核对」（checked=False、origin=from_material）：逐段核对后才能发布主题；
        - 不调用模型、不做摘要或改写（是来源摘录编排，不是模型生成）；扫描件/图片/视频等
          没有可核对文字的资料不产出段落，也不会被声称已读取；
        - 只追加：已有人工段落、已核对段落与其他既有内容原样保留；同一出处（含版本）同一文字
          不重复追加；现有段落与现有钉住不被静默改写（来源更新后由成员显式重新核对）。

        返回 {"added": n, "skipped": n, "total": n, "unattributed": n}。
        """
        self._require_knowledge_editor(tenant_id, user_id)
        self._kb2_require_topic(tenant_id, topic_id)
        draft = self._kb2_topic_version_row(tenant_id, topic_id, draft=True)
        published = self._kb2_topic_version_row(tenant_id, topic_id, published=True)
        reference = draft or published
        snapshot = _json_load(reference["content_json"], {}) if reference else {}
        current = [p for p in (snapshot.get("paragraphs") or []) if isinstance(p, dict)]

        def fingerprint(paragraph: dict):
            source = paragraph.get("source")
            if not isinstance(source, dict) or source.get("kind") != "knowledge":
                return None
            return (
                str(source.get("item_id") or ""),
                str(source.get("version_id") or ""),  # 出处不同版本不并作重复
                str(source.get("source_id") or ""),
                json.dumps(source.get("location") or {}, ensure_ascii=False, sort_keys=True),
                str(paragraph.get("text") or "").strip(),
            )

        existing = {fp for fp in (fingerprint(p) for p in current) if fp}
        added = 0
        skipped = 0
        unattributed = 0
        for relation in self.kb2_list_relations(tenant_id, topic_id):
            if relation["material_kind"] != "knowledge":
                continue  # 产品不产出正文摘录（产品字段有独立核对与授权闸门）
            item = self.get_knowledge_item(tenant_id, relation["material_id"])
            if not item:
                continue
            for source in self.conn.execute(
                "SELECT * FROM kb2_sources WHERE tenant_id=? AND item_id=? ORDER BY created_at, id",
                (tenant_id, item["id"]),
            ).fetchall():
                ws_id = str(source["knowledge_source_id"] or "")
                if not ws_id:
                    continue  # 没有登记原件：不能形成「确切版本+原件+位置」的出处
                for row in self.conn.execute(
                    "SELECT * FROM kb2_paragraphs WHERE tenant_id=? AND source_id=? ORDER BY ordinal",
                    (tenant_id, source["id"]),
                ).fetchall():
                    text = str(row["text"] or "").strip()
                    if not text or row["excluded"] or not row["checked"]:
                        continue  # 只用已核对、未排除的段落（待核对内容不进入初稿）
                    version = self._kb2_resolve_source_version(item, ws_id, text)
                    if version is None:
                        unattributed += 1  # 没有真实包含这段文字的归属版本：不产出出处
                        continue
                    location = _json_load(row["location_json"], {})
                    candidate = {
                        "id": secrets.token_hex(8), "kind": "point", "text": text,
                        "origin": "from_material", "checked": False,
                        "source": {
                            "kind": "knowledge", "item_id": item["id"],
                            "version_id": version["id"], "source_id": ws_id,
                        },
                    }
                    if location:
                        candidate["source"]["location"] = location
                    fp = fingerprint(candidate)
                    if fp and fp in existing:
                        skipped += 1
                        continue
                    if fp:
                        existing.add(fp)
                    current.append(candidate)
                    added += 1
        if added:
            self.kb2_save_topic_draft(tenant_id, user_id, topic_id, paragraphs=current)
        return {"added": added, "skipped": skipped, "total": len(current), "unattributed": unattributed}

    def kb2_renew_topic_paragraph_source(self, tenant_id: str, user_id: str, topic_id: str,
                                         paragraph_id: str) -> dict:
        """人类显式重新核对：把一段摘录的出处按当前资料内容重新钉住（不静默自动换版）。

        只在成员点「重新核对来源」时执行；只改草稿段落。仅当当前资料的真实段落里存在与
        该段文字一致的段落时才重新钉住（同原件同位置优先，其次同原件其他位置，再其次该资料
        的其他原件）；找不到一致位置时明确失败，提示改标人工改写，不猜测出处。
        """
        self._require_knowledge_editor(tenant_id, user_id)
        topic = self._kb2_require_topic(tenant_id, topic_id)
        draft = self._kb2_topic_version_row(tenant_id, topic_id, draft=True)
        published = self._kb2_topic_version_row(tenant_id, topic_id, published=True)
        reference = draft or published
        if not reference:
            raise ValueError("主题还没有正文：请先编辑正文并保存草稿")
        snapshot = _json_load(reference["content_json"], {})
        paragraphs = [p for p in (snapshot.get("paragraphs") or []) if isinstance(p, dict)]
        target = next((p for p in paragraphs if str(p.get("id") or "") == str(paragraph_id)), None)
        if target is None:
            raise ValueError("段落不存在于当前草稿")
        source = target.get("source")
        if not isinstance(source, dict) or source.get("kind") != "knowledge":
            raise ValueError("该段不是资料摘录：无需重新核对来源")
        item_id = str(source.get("item_id") or "")
        item = self.get_knowledge_item(tenant_id, item_id)
        if not item:
            raise ValueError("来源资料已不存在：请把该段改标人工改写")
        text = str(target.get("text") or "").strip()
        if not text:
            raise ValueError("空段落没有可核对的内容")
        old_source_id = str(source.get("source_id") or "")
        old_signature = self._kb2_location_signature(source.get("location") or {})
        candidates = []
        for kb2_source in self.conn.execute(
            "SELECT * FROM kb2_sources WHERE tenant_id=? AND item_id=? ORDER BY created_at, id",
            (tenant_id, item_id),
        ).fetchall():
            ws_id = str(kb2_source["knowledge_source_id"] or "")
            if not ws_id:
                continue
            for row in self.conn.execute(
                "SELECT * FROM kb2_paragraphs WHERE tenant_id=? AND source_id=? ORDER BY ordinal",
                (tenant_id, kb2_source["id"]),
            ).fetchall():
                row_text = str(row["text"] or "").strip()
                if not row_text or row_text != text:
                    continue
                location = _json_load(row["location_json"], {})
                same_source = 0 if ws_id == old_source_id else 1
                same_location = 0 if self._kb2_location_signature(location) == old_signature else 1
                candidates.append((same_source, same_location, row["ordinal"], ws_id, location))
        candidates.sort()
        for _, _, _, ws_id, location in candidates:
            version = self._kb2_resolve_source_version(item, ws_id, text, current_only=True)
            if version is None:
                continue
            new_source = {"kind": "knowledge", "item_id": item_id,
                          "version_id": version["id"], "source_id": ws_id, "location": location}
            target["source"] = new_source
            self.kb2_save_topic_draft(tenant_id, user_id, topic_id, paragraphs=paragraphs)
            self._audit(tenant_id, "kb2_topic", topic_id, "paragraph_source_renewed",
                        self._require_knowledge_editor(tenant_id, user_id)["email"],
                        f"paragraph={paragraph_id[:8]};version={version['version']}")
            self.conn.commit()
            return {"updated": True, "previous": dict(source), "source": dict(new_source),
                    "version_number": version["version"]}
        raise ValueError(
            "当前资料的发布内容中找不到与这段文字一致的位置：请把该段改标为「人工改写」后重新核对"
            "（文字会保留为成员修改），或先修正资料内容")

    def kb2_refresh_relation_pin(self, tenant_id: str, user_id: str, topic_id: str,
                                 material_ref: dict) -> dict:
        """人类显式重新钉住关联快照（来源更新后不静默换版；成员确认后更新钉住版本）。"""
        editor = self._require_knowledge_editor(tenant_id, user_id)
        self._kb2_require_topic(tenant_id, topic_id)
        ref = material_ref if isinstance(material_ref, dict) else {}
        kind = str(ref.get("kind") or "")
        material_id = str(ref.get("id") or "").strip()
        if kind not in KB2_MATERIAL_KINDS or not material_id:
            raise ValueError("资料类型无效（应为 knowledge 或 product）")
        row = self.conn.execute(
            "SELECT * FROM kb2_topic_relations WHERE tenant_id=? AND topic_id=? AND material_kind=? AND material_id=?",
            (tenant_id, topic_id, kind, material_id),
        ).fetchone()
        if not row:
            raise ValueError("还没有建立该资料的关联")
        snapshot = self._kb2_material_snapshot(tenant_id, kind, material_id)
        if snapshot is None:
            raise ValueError("资料不存在或不属于本企业：不能重新钉住")
        previous = {"pinned_version_id": row["pinned_version_id"],
                    "pinned_version_number": row["pinned_version_number"],
                    "pinned_source_id": row["pinned_source_id"]}
        if (previous["pinned_version_id"] == snapshot["pinned_version_id"]
                and previous["pinned_version_number"] == snapshot["pinned_version_number"]
                and previous["pinned_source_id"] == snapshot["pinned_source_id"]):
            return {"updated": False, "previous": previous, "current": previous}
        self.conn.execute(
            "UPDATE kb2_topic_relations SET pinned_version_id=?, pinned_source_id=?, pinned_version_number=?, linked_at=? "
            "WHERE tenant_id=? AND id=?",
            (snapshot["pinned_version_id"], snapshot["pinned_source_id"], snapshot["pinned_version_number"],
             _now(), tenant_id, row["id"]),
        )
        self._audit(tenant_id, "kb2_topic", topic_id, "relation_pin_refreshed", editor["email"],
                    f"{kind}:{material_id[:12]};version={snapshot['pinned_version_number']}")
        self.conn.commit()
        return {"updated": True, "previous": previous,
                "current": {"pinned_version_id": snapshot["pinned_version_id"],
                            "pinned_version_number": snapshot["pinned_version_number"],
                            "pinned_source_id": snapshot["pinned_source_id"]}}

    def kb2_set_topic_link(self, tenant_id: str, user_id: str, topic_id: str,
                           related_topic_id: str, linked: bool = True) -> None:
        """相关主题（主题↔主题基础关系，v0.3 §4）：双向可见、幂等；只存关系不扩图谱。

        关系是独立于内容与权限的链接：建立/移除不改变主题内容、发布状态与版本。
        """
        editor = self._require_knowledge_editor(tenant_id, user_id)
        self._kb2_require_topic(tenant_id, topic_id)
        related_topic_id = str(related_topic_id or "").strip()
        if not related_topic_id or related_topic_id == topic_id:
            raise ValueError("请选择另一个主题作为相关主题")
        self._kb2_require_topic(tenant_id, related_topic_id)
        if linked:
            cursor = self.conn.execute(
                "INSERT OR IGNORE INTO kb2_topic_links(id, tenant_id, topic_id, related_topic_id, created_by, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (secrets.token_hex(12), tenant_id, topic_id, related_topic_id, editor["email"], _now()),
            )
            if cursor.rowcount == 1:
                self._audit(tenant_id, "kb2_topic", topic_id, "topic_link_added", editor["email"],
                            f"related={related_topic_id[:12]}")
                self.conn.commit()
            return
        cursor = self.conn.execute(
            "DELETE FROM kb2_topic_links WHERE tenant_id=? AND topic_id=? AND related_topic_id=?",
            (tenant_id, topic_id, related_topic_id),
        )
        if cursor.rowcount == 0:
            # 反向删除同样有效（双向关系一样看待）。
            cursor = self.conn.execute(
                "DELETE FROM kb2_topic_links WHERE tenant_id=? AND topic_id=? AND related_topic_id=?",
                (tenant_id, related_topic_id, topic_id),
            )
        if cursor.rowcount:
            self._audit(tenant_id, "kb2_topic", topic_id, "topic_link_removed", editor["email"],
                        f"related={related_topic_id[:12]}")
            self.conn.commit()

    def kb2_list_topic_links(self, tenant_id: str, topic_id: str) -> list[dict]:
        """相关主题（双向合并去重）：返回 [{"id","title","status"}]。"""
        rows = self.conn.execute(
            "SELECT related_topic_id AS other FROM kb2_topic_links WHERE tenant_id=? AND topic_id=? "
            "UNION SELECT topic_id AS other FROM kb2_topic_links WHERE tenant_id=? AND related_topic_id=?",
            (tenant_id, topic_id, tenant_id, topic_id),
        ).fetchall()
        out = []
        for row in rows:
            topic = self._kb2_topic_row(tenant_id, row["other"])
            if topic:
                out.append({"id": topic["id"], "title": topic["title"], "status": topic["status"]})
        out.sort(key=lambda item: item["title"])
        return out

    def kb2_restore_topic_version(self, tenant_id: str, user_id: str, topic_id: str, version: int) -> str:
        """恢复历史版本为**新草稿**（v0.2 §8：回到历史点产生新草稿，不覆盖历史、不动线上）。"""
        editor = self._require_knowledge_editor(tenant_id, user_id)
        topic = self._kb2_require_topic(tenant_id, topic_id)
        if topic["draft_version_id"]:
            raise ValueError("已有未发布草稿：请先发布或继续编辑现有草稿，再恢复历史版本")
        source_version = None
        for row in self.conn.execute(
            "SELECT * FROM kb2_topic_versions WHERE tenant_id=? AND topic_id=? AND version=?",
            (tenant_id, topic_id, int(version)),
        ).fetchall():
            source_version = row
        if source_version is None:
            raise ValueError("历史版本不存在")
        if source_version["status"] == "draft":
            raise ValueError("当前草稿无需恢复")
        next_version = self.conn.execute(
            "SELECT COALESCE(MAX(version), 0) + 1 FROM kb2_topic_versions WHERE tenant_id=? AND topic_id=?",
            (tenant_id, topic_id),
        ).fetchone()[0]
        version_id = secrets.token_hex(12)
        timestamp = _now()
        self.conn.execute(
            "INSERT INTO kb2_topic_versions(id, tenant_id, topic_id, version, status, content_json, usage, "
            "ai_authorized, expires_at, restored_from_version, created_by, created_at) "
            "VALUES (?, ?, ?, ?, 'draft', ?, ?, ?, ?, ?, ?, ?)",
            (version_id, tenant_id, topic_id, next_version, source_version["content_json"],
             source_version["usage"], source_version["ai_authorized"], source_version["expires_at"],
             source_version["version"], editor["email"], timestamp),
        )
        self.conn.execute(
            "UPDATE kb2_topics SET draft_version_id=?, updated_at=? WHERE tenant_id=? AND id=?",
            (version_id, timestamp, tenant_id, topic_id),
        )
        self._audit(tenant_id, "kb2_topic", topic_id, "topic_version_restored", editor["email"],
                    f"from=v{source_version['version']};new_draft=v{next_version}")
        self.conn.commit()
        return version_id

    def kb2_set_topic_disabled(self, tenant_id: str, user_id: str, topic_id: str, disabled: bool = True) -> None:
        """停用 / 恢复主题（生命周期三态：草稿 / 已发布 / 已停用；发布历史与关联保留，不删除）。"""
        editor = self._require_knowledge_editor(tenant_id, user_id)
        topic = self._kb2_require_topic(tenant_id, topic_id)
        timestamp = _now()
        if disabled:
            if topic["status"] == "disabled":
                return  # 幂等
            if topic["status"] != "published":
                raise ValueError("只有已发布的主题才能停用")
            self.conn.execute(
                "UPDATE kb2_topics SET status='disabled', updated_at=? WHERE tenant_id=? AND id=?",
                (timestamp, tenant_id, topic_id),
            )
            action = "topic_disabled"
        else:
            if topic["status"] != "disabled":
                return  # 幂等
            restored = "published" if topic["published_version_id"] else "draft"
            self.conn.execute(
                "UPDATE kb2_topics SET status=?, updated_at=? WHERE tenant_id=? AND id=?",
                (restored, timestamp, tenant_id, topic_id),
            )
            action = "topic_enabled"
        self._audit(tenant_id, "kb2_topic", topic_id, action, editor["email"], "")
        self.conn.commit()

    # ── 主题 ↔ 资料关联（多对多；关联时钉住来源快照）────────────────────────

    def kb2_list_relations(self, tenant_id: str, topic_id: str) -> list[dict]:
        if not self._kb2_topic_row(tenant_id, topic_id):
            return []
        rows = self.conn.execute(
            "SELECT * FROM kb2_topic_relations WHERE tenant_id=? AND topic_id=? ORDER BY linked_at, id",
            (tenant_id, topic_id),
        ).fetchall()
        out = []
        for row in rows:
            snapshot = self._kb2_material_snapshot(tenant_id, row["material_kind"], row["material_id"])
            out.append({
                "id": row["id"], "topic_id": row["topic_id"],
                "material_kind": row["material_kind"], "material_id": row["material_id"],
                "title": snapshot["title"] if snapshot else "(资料已不存在)",
                "material_status": snapshot["status"] if snapshot else "",
                "form_label": snapshot["form_label"] if snapshot else "",
                "registered_only": snapshot["registered_only"] if snapshot else False,
                "missing": snapshot is None,
                "pinned_version_id": row["pinned_version_id"],
                "pinned_version_number": row["pinned_version_number"],
                "pinned_source_id": row["pinned_source_id"],
                "origin": row["origin"], "linked_by": row["linked_by"], "linked_at": row["linked_at"],
            })
        return out

    def _kb2_set_relation_internal(self, tenant_id: str, user_id: str, topic_id, material_ref, linked, origin) -> bool:
        editor = self._require_knowledge_editor(tenant_id, user_id)
        self._kb2_require_topic(tenant_id, topic_id)
        ref = material_ref if isinstance(material_ref, dict) else {}
        kind = str(ref.get("kind") or "")
        material_id = str(ref.get("id") or "").strip()
        if kind not in KB2_MATERIAL_KINDS:
            raise ValueError("资料类型无效（应为 knowledge 或 product）")
        if not material_id:
            raise ValueError("资料 ID 不能为空")
        existing = self.conn.execute(
            "SELECT id FROM kb2_topic_relations WHERE tenant_id=? AND topic_id=? AND material_kind=? AND material_id=?",
            (tenant_id, topic_id, kind, material_id),
        ).fetchone()
        timestamp = _now()
        if linked:
            if existing:
                return False  # 幂等：重复关联不改变已经钉住的快照
            snapshot = self._kb2_material_snapshot(tenant_id, kind, material_id)
            if snapshot is None:
                raise ValueError("资料不存在或不属于本企业")
            self.conn.execute(
                "INSERT INTO kb2_topic_relations(id, tenant_id, topic_id, material_kind, material_id, pinned_version_id, pinned_source_id, pinned_version_number, origin, linked_by, linked_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (secrets.token_hex(12), tenant_id, topic_id, kind, material_id,
                 snapshot["pinned_version_id"], snapshot["pinned_source_id"], snapshot["pinned_version_number"],
                 origin, editor["email"], timestamp),
            )
            self._audit(tenant_id, "kb2_topic", topic_id, "relation_linked", editor["email"],
                        f"{kind}:{material_id[:12]};origin={origin}")
            self.conn.commit()
            return True
        if existing:
            self.conn.execute("DELETE FROM kb2_topic_relations WHERE tenant_id=? AND id=?", (tenant_id, existing["id"]))
            self._audit(tenant_id, "kb2_topic", topic_id, "relation_unlinked", editor["email"],
                        f"{kind}:{material_id[:12]}")
            self.conn.commit()
            return True
        return False  # 幂等：重复取消不报错

    def kb2_set_relation(self, tenant_id: str, user_id: str, topic_id: str, material_ref: dict, linked: bool = True) -> None:
        """建立 / 取消主题与资料的关联；旧产品只读适配，不复制产品行。

        关联是独立于内容与权限的关系：不影响资料发布状态、不授权 AI。
        """
        self._kb2_set_relation_internal(tenant_id, user_id, topic_id, material_ref, linked, "manual")

    # ── 全部资料（跨主题全集；旧产品/文章/FAQ 不自动分配主题）─────────────

    def kb2_list_materials(self, tenant_id: str) -> list[dict]:
        topics_by_material: dict[tuple, list] = {}
        for row in self.conn.execute(
            "SELECT r.material_kind, r.material_id, t.id AS topic_id, t.title AS topic_title "
            "FROM kb2_topic_relations r JOIN kb2_topics t ON t.id = r.topic_id "
            "WHERE r.tenant_id=? ORDER BY t.title, t.id",
            (tenant_id,),
        ).fetchall():
            topics_by_material.setdefault((row["material_kind"], row["material_id"]), []).append(
                {"id": row["topic_id"], "title": row["topic_title"]})
        out: list[dict] = []
        for item in self.list_knowledge_items(tenant_id):
            snapshot = self._kb2_material_snapshot(tenant_id, "knowledge", item["id"])
            if snapshot is None:
                continue
            # 形式维度与主题正交（v0.3 §1）：由登记原件决定的资料形式优先（PDF/DOCX/图片/网页/手动文字…）；
            # 没有第二轮原件的旧资料保持知识形态显示（文章/FAQ…）。source_id 供「全部资料」直达统一资料详情。
            source_row = self.conn.execute(
                "SELECT id, format FROM kb2_sources WHERE tenant_id=? AND item_id=? ORDER BY created_at DESC, id DESC LIMIT 1",
                (tenant_id, item["id"]),
            ).fetchone()
            out.append({
                "kind": "knowledge", "id": item["id"], "title": snapshot["title"],
                "form": item["form"], "form_label": snapshot["form_label"],
                "display_form": KB2_FORMAT_LABELS.get(source_row["format"], source_row["format"])
                if source_row else snapshot["form_label"],
                "source_id": source_row["id"] if source_row else None,
                "status": item["status"], "registered_only": snapshot["registered_only"],
                "updated_at": item["updated_at"],
                "topics": topics_by_material.get(("knowledge", item["id"]), []),
            })
        for overview in self.list_knowledge_product_overview(tenant_id):
            out.append({
                "kind": "product", "id": overview["product_id"], "title": overview["name"],
                "form": "product", "form_label": "产品", "display_form": "产品",
                "source_id": None,
                "status": overview["status"],
                "registered_only": False, "model": overview["model"],
                "available": overview["available"], "reason": overview["reason"],
                "updated_at": overview["updated_at"],
                "topics": topics_by_material.get(("product", overview["product_id"]), []),
            })
        return out

    # ══════════════════════════════════════════════════════════════════════
    # 任务2：真实统一入库（批次 / 私有原件 / 逐段核对 / 建议确认 / 发布）
    # ══════════════════════════════════════════════════════════════════════

    def _kb2_storage_dir(self) -> Path:
        """批次原件的私有目录（token 命名；测试可注入 knowledge_storage_root 隔离）。"""
        root = self._knowledge_storage_dir() / "phase2"
        root.mkdir(parents=True, exist_ok=True)
        return root

    def _kb2_read_source_bytes(self, source) -> bytes:
        if not source["storage_path"]:
            raise ValueError("原件未保存")
        target = Path(source["storage_path"])
        if not target.is_file():
            raise ValueError("原件文件缺失：请重新导入或人工整理正文")
        data = target.read_bytes()
        if source["sha256"] and hashlib.sha256(data).hexdigest() != source["sha256"]:
            raise ValueError("原件校验失败（内容与登记不一致）")
        return data

    def kb2_create_batch(self, tenant_id: str, user_id: str, entries: list[dict], *,
                         process_now: bool = True) -> str:
        """统一入库：真实保存原件并处理（完成识别只创建草稿，绝不自动发布）。

        entries: [{"kind":"file","filename":...,"data":bytes,"title":可选},
                  {"kind":"link","url":...}, {"kind":"manual","text":...,"title":可选}]
        process_now=False（HTTP/排队入口）：只落库并立即返回 batch_id，请求内不做解析；
        由入库线程（start_knowledge_phase2_worker）或 kb2_process_pending_sources 独占认领后真实处理。
        """
        editor = self._require_knowledge_editor(tenant_id, user_id)
        if not isinstance(entries, list) or not entries:
            raise ValueError("请先添加至少一件资料")
        if len(entries) > KB2_MAX_BATCH_ENTRIES:
            raise ValueError(f"一次最多添加 {KB2_MAX_BATCH_ENTRIES} 件资料，请分批添加")
        normalized = []
        for entry in entries:
            if not isinstance(entry, dict):
                raise ValueError("资料条目格式无效")
            kind = str(entry.get("kind") or "")
            title = str(entry.get("title") or "").strip()
            if kind == "file":
                filename = str(entry.get("filename") or "").strip()
                data = entry.get("data")
                if not filename:
                    raise ValueError("缺少文件名")
                if not isinstance(data, (bytes, bytearray)) or not bytes(data):
                    raise ValueError(f"{filename}：文件内容为空或无效")
                payload = bytes(data)
                extension = Path(filename).suffix.lower()
                if extension in knowledge_ingestion.TEXT_EXTENSIONS:
                    if len(payload) > KNOWLEDGE_TEXT_FILE_LIMIT:
                        raise ValueError(f"{filename}：文本文件不能超过 1MB；大文件请先拆分或改用手工摘录")
                elif extension in KB2_SUPPORTED_ENTRY_EXTENSIONS:
                    if len(payload) > KNOWLEDGE_DOCUMENT_FILE_LIMIT:
                        raise ValueError(f"{filename}：文件不能超过 10MB")
                else:
                    raise ValueError(f"{filename}：暂不支持该格式（支持 TXT/Markdown/CSV/PDF/DOCX；图片、视频与旧版 DOC 可仅登记原件）")
                normalized.append({"kind": kind, "filename": filename, "data": payload, "title": title})
            elif kind == "link":
                url = str(entry.get("url") or "").strip()
                if not re.match(r"^https?://\S+$", url):
                    raise ValueError("请填写有效的 http(s) 网址（本批只登记地址，不抓取内容）")
                normalized.append({"kind": kind, "url": url, "title": title or url})
            elif kind == "manual":
                text = str(entry.get("text") or "")
                if not text.strip():
                    raise ValueError("手动文字不能为空")
                display = title or next((line.strip() for line in text.splitlines() if line.strip()), "手动文字")[:40]
                normalized.append({"kind": kind, "text": text, "title": display})
            else:
                raise ValueError("资料类型无效（file / link / manual）")
        batch_id = secrets.token_hex(12)
        timestamp = _now()
        self.conn.execute(
            "INSERT INTO kb2_batches(id, tenant_id, status, usage, ai_authorized, expires_at, created_by, created_at, updated_at) "
            "VALUES (?, ?, 'draft', 'internal', 0, NULL, ?, ?, ?)",
            (batch_id, tenant_id, editor["email"], timestamp, timestamp),
        )
        written: list[Path] = []
        try:
            for entry in normalized:
                source_id = secrets.token_hex(12)
                if entry["kind"] == "file":
                    extension = Path(entry["filename"]).suffix.lower()
                    target = self._kb2_storage_dir() / f"kb2-{secrets.token_hex(16)}{extension}"
                    target.write_bytes(entry["data"])
                    written.append(target)
                    self.conn.execute(
                        "INSERT INTO kb2_sources(id, tenant_id, batch_id, kind, filename, title, format, size, sha256, storage_path, source_location, payload_text, status, created_by, created_at, updated_at) "
                        "VALUES (?, ?, ?, 'file', ?, ?, ?, ?, ?, ?, ?, '', 'pending', ?, ?, ?)",
                        (source_id, tenant_id, batch_id, entry["filename"],
                         entry["title"] or Path(entry["filename"]).stem or entry["filename"],
                         knowledge_ingestion.format_for_filename(entry["filename"]), len(entry["data"]),
                         hashlib.sha256(entry["data"]).hexdigest(), str(target),
                         f"导入自文件：{Path(entry['filename']).name}", editor["email"], timestamp, timestamp),
                    )
                elif entry["kind"] == "link":
                    self.conn.execute(
                        "INSERT INTO kb2_sources(id, tenant_id, batch_id, kind, filename, title, format, source_location, payload_text, status, created_by, created_at, updated_at) "
                        "VALUES (?, ?, ?, 'link', ?, ?, 'web', ?, '', 'pending', ?, ?, ?)",
                        (source_id, tenant_id, batch_id, entry["url"], entry["title"], entry["url"], editor["email"], timestamp, timestamp),
                    )
                else:
                    self.conn.execute(
                        "INSERT INTO kb2_sources(id, tenant_id, batch_id, kind, filename, title, format, source_location, payload_text, status, created_by, created_at, updated_at) "
                        "VALUES (?, ?, ?, 'manual', ?, ?, 'manual', '人工撰写', ?, 'pending', ?, ?, ?)",
                        (source_id, tenant_id, batch_id, entry["title"], entry["title"], entry["text"], editor["email"], timestamp, timestamp),
                    )
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            for path in written:
                path.unlink(missing_ok=True)
            raise
        self._audit(tenant_id, "kb2_batch", batch_id, "batch_created", editor["email"],
                    f"entries={len(normalized)}")
        self.conn.commit()  # 排队入口同样要立即持久（不能把审计写入留在事务里占着写锁）
        if process_now:
            self.kb2_process_pending_sources(tenant_id=tenant_id)
        return batch_id

    # ── 独占处理认领 / 接管 / 释放（B2-B01 修复核心：实现在数据层与本进程认领表）──

    def _kb2_claim_scope(self) -> str:
        """认领表作用域：同一数据库文件（真实路径）在进程内共享同一份活动认领视图。"""
        return os.path.realpath(str(self.path))

    def _kb2_lease_seconds(self) -> float:
        return float(getattr(self, "kb2_processing_lease_seconds", KB2_PROCESSING_LEASE_SECONDS))

    def _kb2_stale_claim_takeover_allowed(self, owner, lease_until, now: float) -> bool:
        """处理中行能否被接管：属主已失活（同进程线程终止 / 进程已退出），或租约已过期。"""
        if owner:
            host, _, remainder = str(owner).partition(":")
            pid_text, _, _ = remainder.partition(":")
            try:
                pid = int(pid_text)
            except ValueError:
                pid = None
            if host == _KB2_HOSTNAME and pid is not None:
                if pid == os.getpid():
                    # 同进程：调用前已核对活动认领表，能到这里说明执行线程已终止（finally 已释放认领）。
                    return True
                if not _kb2_pid_alive(pid):
                    return True
        if lease_until is None:
            return True  # 旧数据没有认领/租约信息：按可恢复处理（迁移前的 processing 行）
        try:
            return float(lease_until) <= now
        except (TypeError, ValueError):
            return True

    def _kb2_claim_source(self, tenant_id: str, source_id: str):
        """原子认领来源：返回认领令牌；被他人持有或不满足接管条件时返回 None（跳过，不阻断其他来源）。"""
        key = (self._kb2_claim_scope(), tenant_id, source_id)
        with _KB2_CLAIMS_LOCK:
            active = _KB2_ACTIVE_CLAIMS.get(key)
            if active is not None:
                if active[1].is_alive():
                    return None  # 本进程已有执行者在处理：不抢执行
                _KB2_ACTIVE_CLAIMS.pop(key, None)
            row = self.conn.execute(
                "SELECT status, processing_owner, processing_lease_until FROM kb2_sources "
                "WHERE tenant_id=? AND id=?",
                (tenant_id, source_id),
            ).fetchone()
            if not row or row["status"] not in ("pending", "processing"):
                return None  # 不存在或已是终态（完成/失败/已发布）
            token = _kb2_new_claim_token()
            now = time.time()
            lease_until = now + self._kb2_lease_seconds()
            timestamp = _now()
            if row["status"] == "pending":
                cursor = self.conn.execute(
                    "UPDATE kb2_sources SET status='processing', processing_owner=?, processing_lease_until=?, "
                    "attempts=attempts+1, error='', updated_at=? WHERE tenant_id=? AND id=? AND status='pending'",
                    (token, lease_until, timestamp, tenant_id, source_id),
                )
            else:
                if not self._kb2_stale_claim_takeover_allowed(
                        row["processing_owner"], row["processing_lease_until"], now):
                    return None  # 属主存活且租约有效：不抢执行
                cursor = self.conn.execute(
                    "UPDATE kb2_sources SET status='processing', processing_owner=?, processing_lease_until=?, "
                    "attempts=attempts+1, updated_at=? WHERE tenant_id=? AND id=? AND status='processing' "
                    "AND processing_owner IS ? AND processing_lease_until IS ?",
                    (token, lease_until, timestamp, tenant_id, source_id,
                     row["processing_owner"], row["processing_lease_until"]),
                )
            if cursor.rowcount != 1:
                self.conn.rollback()  # 竞争失败：以数据库当前状态为准（不覆盖）
                return None
            self.conn.commit()
            _KB2_ACTIVE_CLAIMS[key] = (token, threading.current_thread())
            return token

    def _kb2_release_claim(self, tenant_id: str, source_id: str, token: str) -> None:
        """释放本进程认领表条目；数据行终态由处理结果写入，中断时保留认领交恢复路径接管。"""
        key = (self._kb2_claim_scope(), tenant_id, source_id)
        with _KB2_CLAIMS_LOCK:
            active = _KB2_ACTIVE_CLAIMS.get(key)
            if active is not None and active[0] == token:
                _KB2_ACTIVE_CLAIMS.pop(key, None)

    def _kb2_claim_held_elsewhere(self, tenant_id: str, source_id: str) -> bool:
        """来源是否被其他执行者持有（本进程活动认领，或租约内且属主可追踪的处理中行）。"""
        key = (self._kb2_claim_scope(), tenant_id, source_id)
        with _KB2_CLAIMS_LOCK:
            active = _KB2_ACTIVE_CLAIMS.get(key)
            if active is not None and active[1].is_alive():
                return True
            row = self.conn.execute(
                "SELECT status, processing_owner, processing_lease_until FROM kb2_sources "
                "WHERE tenant_id=? AND id=?",
                (tenant_id, source_id),
            ).fetchone()
            if not row or row["status"] != "processing":
                return False
            return not self._kb2_stale_claim_takeover_allowed(
                row["processing_owner"], row["processing_lease_until"], time.time())

    def kb2_process_pending_sources(self, tenant_id: str | None = None) -> dict:
        """从数据库继续处理待处理来源（崩溃恢复 / 工作进程入口；独占认领，幂等）。

        - 每个来源先原子认领：同一来源同一时刻只有一个执行者；被其他执行者持有
          （本进程活动认领 / 租约内属主存活）则跳过——活动 processing 不被另一线程抢执行；
        - 属主失活（同进程线程终止 / 进程已退出）或租约过期时接管继续（崩溃恢复路径）；
        - 已是终态（extracted/registered/failed/published）的来源不满足认领条件，自然跳过。
        返回 processed=本次实际处理数；skipped=本次跳过数；pending_left=扫描后未处理的来源数。
        """
        query = "SELECT id, tenant_id FROM kb2_sources WHERE status IN ('pending', 'processing')"
        params: list = []
        if tenant_id is not None:
            query += " AND tenant_id=?"
            params.append(tenant_id)
        query += " ORDER BY created_at, id"
        rows = self.conn.execute(query, params).fetchall()
        done = 0
        skipped = 0
        errored = 0
        for row in rows:
            token = self._kb2_claim_source(row["tenant_id"], row["id"])
            if token is None:
                skipped += 1
                continue
            try:
                self._kb2_run_source(row["tenant_id"], row["id"], token)
            except Exception as error:
                # B2-B02：单个来源的意外错误不中断整轮扫描（其他来源照常处理），也不让来源
                # 永远停在 processing——先丢弃半写，再写入明确失败（可重试），审计落盘。
                errored += 1
                self._kb2_mark_source_failed_after_error(
                    row["tenant_id"], row["id"], token, str(error) or error.__class__.__name__)
            else:
                done += 1
            finally:
                self._kb2_release_claim(row["tenant_id"], row["id"], token)
        return {"processed": done, "skipped": skipped, "errored": errored,
                "pending_left": len(rows) - done - errored}

    def _kb2_mark_source_failed_after_error(self, tenant_id: str, source_id: str, token: str, message: str) -> None:
        """意外内部错误后的收尾：仅当认领仍属本执行者时写入明确失败（不覆盖接管者结果）。"""
        self.conn.rollback()  # 丢弃本次处理的半写（认领信息在此前已单独提交）
        source = self._kb2_source_row(tenant_id, source_id)
        actor = source["created_by"] if source else ""
        error_text = f"处理中断（{message[:160]}）：请重试或人工整理正文"
        try:
            updated = self.conn.execute(
                "UPDATE kb2_sources SET status='failed', error=?, processing_owner=NULL, "
                "processing_lease_until=NULL, updated_at=? "
                "WHERE tenant_id=? AND id=? AND status='processing' AND processing_owner=?",
                (error_text, _now(), tenant_id, source_id, token),
            )
            if updated.rowcount == 1:
                self._audit(tenant_id, "kb2_source", source_id, "source_failed", actor,
                            f"unexpected={message[:120]}")
        except Exception:
            self.conn.rollback()
            return
        self.conn.commit()  # 失败结果与审计落盘（不留隐形事务）

    def _kb2_process_source(self, tenant_id: str, source_id: str) -> None:
        """处理单个来源：先原子认领（独占），再真实解析 → 创建资料草稿（幂等）。

        并发与恢复语义（实现在 _kb2_claim_source / _kb2_run_source，由并发回归测试验证）：
        - 同一来源同一时刻只有一个执行者；其他入口（同步创建 / 入库线程 / 重试）认领失败即跳过，
          不重复解析、不重复创建资料条目；
        - 崩溃（含 BaseException）后来源保持 processing；同进程恢复入口（kb2_process_pending_sources
          或 kb2_retry_source）立即接管（原执行线程已终止），跨进程按属主退出 / 租约过期接管；
        - 条目身份（item_id / knowledge_source_id）在数据层条件写入且唯一，恢复与重试不重复创建。
        """
        token = self._kb2_claim_source(tenant_id, source_id)
        if token is None:
            return  # 已有执行者或不满足接管条件：跳过（幂等）
        try:
            self._kb2_run_source(tenant_id, source_id, token)
        finally:
            self._kb2_release_claim(tenant_id, source_id, token)

    def _kb2_run_source(self, tenant_id: str, source_id: str, token: str) -> None:
        """认领后的实际处理：解析 → 建条目草稿 → 写段落 → 状态收尾；收尾写入都带认领校验。

        中断语义：processing 状态与认领信息先提交再解析；中断（含 BaseException）后由恢复入口
        接管继续；段落重建失败回滚、不产生半写；处理结果只在仍持有认领时落库（不覆盖接管者）。
        """
        source = self._kb2_source_row(tenant_id, source_id)
        if not source or source["processing_owner"] != token:
            return  # 认领已失效（行已变化或被接管）：不覆盖他人结果
        if source["kind"] == "link":
            result = {
                "status": "registered", "paragraphs": [], "error": "",
                "warnings": ["网页地址仅登记，不抓取内容（本批不做网页采集）：可人工整理正文。"],
            }
        elif source["kind"] == "manual":
            paragraphs = [
                {"text": line.strip(), "location": {"kind": "manual"}, "source_kind": "manual"}
                for line in (source["payload_text"] or "").splitlines() if line.strip()
            ]
            result = {"status": "extracted" if paragraphs else "registered",
                      "paragraphs": paragraphs, "error": "", "warnings": []}
        else:
            try:
                data = self._kb2_read_source_bytes(source)
                result = knowledge_ingestion.extract_document(source["filename"], data)
            except Exception as error:  # 原件缺失/校验失败：明确失败，可人工整理
                result = {"status": "failed", "paragraphs": [], "error": str(error), "warnings": []}
        warnings_json = json.dumps(result.get("warnings") or [], ensure_ascii=False)
        if result["status"] == "failed":
            error_text = result.get("error") or "未能识别：请重试或人工整理正文"
            updated = self.conn.execute(
                "UPDATE kb2_sources SET status='failed', error=?, warnings_json=?, processing_owner=NULL, "
                "processing_lease_until=NULL, updated_at=? WHERE tenant_id=? AND id=? AND processing_owner=?",
                (error_text, warnings_json, _now(), tenant_id, source_id, token),
            )
            self.conn.commit()
            if updated.rowcount == 1:
                self._audit(tenant_id, "kb2_source", source_id, "source_failed", source["created_by"],
                            (result.get("error") or "")[:120])
                self.conn.commit()  # 审计落盘（不把写事务留给后续操作/其他连接承担锁等待）
            return
        title = source["title"] or Path(source["filename"]).stem or source["filename"]
        item_id = self._kb2_create_material_item(tenant_id, source, title)
        paragraphs = [
            paragraph for paragraph in (result.get("paragraphs") or [])
            if isinstance(paragraph, dict) and str(paragraph.get("text") or "").strip()
        ]
        existing = self.conn.execute(
            "SELECT * FROM kb2_paragraphs WHERE tenant_id=? AND source_id=? ORDER BY ordinal",
            (tenant_id, source_id),
        ).fetchall()
        # B2-B02（数据保留）：受保护段落 = 成员人工撰写（origin=manual，含未核对）、
        # 已核对 / 已编辑 / 已排除，或被字段来源钉住的段落。自动重建不得删除或改写它们：
        # 原行（ID/文字/顺序）保持；只替换未受保护的旧提取段落；新识别正文追加在其后；
        # 不因此中断本轮处理，也不影响其他来源（见 kb2_process_pending_sources 的逐来源兜底）。
        pinned_ids = {
            row["paragraph_id"] for row in self.conn.execute(
                "SELECT paragraph_id FROM kb2_source_fields "
                "WHERE tenant_id=? AND source_id=? AND paragraph_id IS NOT NULL",
                (tenant_id, source_id),
            ).fetchall()
        }
        protected_ids = {
            row["id"] for row in existing
            if row["origin"] == "manual" or row["checked"] or row["edited"] or row["excluded"]
            or row["id"] in pinned_ids
        }
        try:
            if protected_ids:
                placeholders = ",".join("?" for _ in protected_ids)
                self.conn.execute(
                    f"DELETE FROM kb2_paragraphs WHERE tenant_id=? AND source_id=? AND id NOT IN ({placeholders})",
                    (tenant_id, source_id, *protected_ids),
                )
                next_ordinal = self.conn.execute(
                    "SELECT COALESCE(MAX(ordinal), -1) + 1 FROM kb2_paragraphs WHERE tenant_id=? AND source_id=?",
                    (tenant_id, source_id),
                ).fetchone()[0]
            else:
                self.conn.execute("DELETE FROM kb2_paragraphs WHERE tenant_id=? AND source_id=?", (tenant_id, source_id))
                next_ordinal = 0
            for ordinal, paragraph in enumerate(paragraphs, start=next_ordinal):
                self.conn.execute(
                    "INSERT INTO kb2_paragraphs(id, tenant_id, source_id, item_id, ordinal, text, location_json, source_kind, origin, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'extracted', ?, ?)",
                    (secrets.token_hex(12), tenant_id, source_id, item_id, ordinal, str(paragraph.get("text") or ""),
                     json.dumps(paragraph.get("location") or {}, ensure_ascii=False),
                     str(paragraph.get("source_kind") or ""), _now(), _now()),
                )
        except Exception:
            self.conn.rollback()  # 不留下半写段落；由重试/恢复重新处理
            raise
        if protected_ids:
            warning_list = list(result.get("warnings") or [])
            warning_list.append(
                "已有成员整理内容（人工段落 / 已核对 / 已编辑 / 已排除）：本次自动识别未覆盖这些段落，"
                "原件解析出的新正文已追加在其后，请继续逐段核对。")
            warnings_json = json.dumps(warning_list, ensure_ascii=False)
        status = "extracted" if paragraphs else "registered"
        updated = self.conn.execute(
            "UPDATE kb2_sources SET status=?, warnings_json=?, error='', processing_owner=NULL, "
            "processing_lease_until=NULL, updated_at=? WHERE tenant_id=? AND id=? AND processing_owner=?",
            (status, warnings_json, _now(), tenant_id, source_id, token),
        )
        if updated.rowcount != 1:
            self.conn.rollback()  # 认领已被接管：丢弃本次收尾，不覆盖接管者结果
            return
        self.conn.commit()
        fresh = self._kb2_source_row(tenant_id, source_id)
        self._kb2_refresh_item_draft_body(tenant_id, fresh)
        self.conn.commit()
        details = f"status={status};paragraphs={len(paragraphs)}"
        if protected_ids:
            details += f";kept={len(protected_ids)}"
        details += f";item={item_id[:8]}"
        self._audit(tenant_id, "kb2_source", source_id, "source_processed", source["created_by"], details)
        self.conn.commit()  # 审计落盘（处理结束不留下写事务/写锁）

    def _kb2_create_material_item(self, tenant_id: str, source, title: str) -> str:
        """创建（或恢复）资料条目草稿：条目身份（item_id / knowledge_source_id）条件写入且唯一。

        预生成 ID 先落库：重试、崩溃恢复与并发竞争都复用数据层已写入的 ID，
        保证「一个来源 ↔ 一个知识条目」不重复创建。
        """
        item_id = source["item_id"]
        if not item_id:
            candidate = secrets.token_hex(12)
            claimed = self.conn.execute(
                "UPDATE kb2_sources SET item_id=?, updated_at=? WHERE tenant_id=? AND id=? AND item_id IS NULL",
                (candidate, _now(), tenant_id, source["id"]),
            )
            if claimed.rowcount == 1:
                item_id = candidate
            else:
                fresh = self._kb2_source_row(tenant_id, source["id"])
                if not fresh or not fresh["item_id"]:
                    self.conn.rollback()
                    raise ValueError("来源与资料的唯一身份写入失败，请重试")
                item_id = fresh["item_id"]
        existing = self.get_knowledge_item(tenant_id, item_id)
        if existing is None:
            timestamp = _now()
            version_id = secrets.token_hex(12)
            content = json.dumps({"title": title, "body": "", "kb2_registered_only": True}, ensure_ascii=False)
            self.conn.execute(
                "INSERT INTO knowledge_items(id, tenant_id, form, product_id, status, published_version_id, draft_version_id, owner_user_id, created_at, updated_at) "
                "VALUES (?, ?, 'article', NULL, 'draft', NULL, ?, ?, ?, ?)",
                (item_id, tenant_id, version_id, source["created_by"], timestamp, timestamp),
            )
            self.conn.execute(
                "INSERT INTO knowledge_versions(id, tenant_id, item_id, version, status, content_json, category, usage, ai_authorized, expires_at, source_id, created_by, created_at) "
                "VALUES (?, ?, ?, 1, 'draft', ?, '', 'internal', 0, NULL, NULL, ?, ?)",
                (version_id, tenant_id, item_id, content, source["created_by"], timestamp),
            )
            self._audit(tenant_id, "kb2_source", source["id"], "item_created", source["created_by"],
                        f"item={item_id[:8]};title={title}")
        if not source["knowledge_source_id"]:
            ws_id = secrets.token_hex(12)
            claimed_ws = self.conn.execute(
                "UPDATE kb2_sources SET knowledge_source_id=?, updated_at=? "
                "WHERE tenant_id=? AND id=? AND knowledge_source_id IS NULL",
                (ws_id, _now(), tenant_id, source["id"]),
            )
            if claimed_ws.rowcount == 1:
                if source["kind"] == "file":
                    ws_kind, ws_filename, ws_type = "file", source["filename"], source["format"]
                    ws_location = source["source_location"]
                elif source["kind"] == "link":
                    ws_kind, ws_filename, ws_type = "manual", source["title"], "web"
                    ws_location = source["source_location"]
                else:
                    ws_kind, ws_filename, ws_type = "manual", source["title"], "manual"
                    ws_location = "人工撰写"
                self.conn.execute(
                    "INSERT INTO knowledge_sources(id, tenant_id, item_id, kind, filename, file_type, source_location, sha256, storage_path, created_by, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (ws_id, tenant_id, item_id, ws_kind, ws_filename, ws_type, ws_location,
                     source["sha256"], source["storage_path"], source["created_by"], _now()),
                )
                draft = self.get_knowledge_draft_version(tenant_id, item_id)
                if draft:
                    self.conn.execute(
                        "UPDATE knowledge_versions SET source_id=? WHERE tenant_id=? AND id=?",
                        (ws_id, tenant_id, draft["id"]),
                    )
        self.conn.commit()
        return item_id

    def _kb2_ensure_item_draft(self, tenant_id: str, item_id: str):
        """条目草稿：无草稿时基于线上版本创建 v(n+1)（线上版本保持不变）。"""
        item = self.get_knowledge_item(tenant_id, item_id)
        if not item:
            raise ValueError("资料条目缺失")
        draft = self.get_knowledge_draft_version(tenant_id, item_id)
        if draft:
            return draft
        published = self.get_published_knowledge_version(tenant_id, item_id)
        if not published:
            raise ValueError("资料条目没有可编辑版本")
        next_version = self.conn.execute(
            "SELECT COALESCE(MAX(version), 0) + 1 FROM knowledge_versions WHERE tenant_id=? AND item_id=?",
            (tenant_id, item_id),
        ).fetchone()[0]
        version_id = secrets.token_hex(12)
        timestamp = _now()
        self.conn.execute(
            "INSERT INTO knowledge_versions(id, tenant_id, item_id, version, status, content_json, category, usage, ai_authorized, expires_at, source_id, created_by, created_at) "
            "VALUES (?, ?, ?, ?, 'draft', ?, ?, ?, ?, ?, ?, ?, ?)",
            (version_id, tenant_id, item_id, next_version, published["content_json"], published["category"],
             published["usage"], published["ai_authorized"], published["expires_at"], published["source_id"],
             published["created_by"], timestamp),
        )
        self.conn.execute(
            "UPDATE knowledge_items SET draft_version_id=?, updated_at=? WHERE tenant_id=? AND id=?",
            (version_id, timestamp, tenant_id, item_id),
        )
        self.conn.commit()
        return self.get_knowledge_version(tenant_id, version_id)

    def _kb2_refresh_item_draft_body(self, tenant_id: str, source) -> None:
        """把来源的收录段落投影到资料草稿正文（排除段不进入；登记标记随实际内容更新）。"""
        if not source["item_id"]:
            return
        self._kb2_require_source_owns_current_draft(tenant_id, source, action="刷新草稿正文")
        item = self.get_knowledge_item(tenant_id, source["item_id"])
        if not item:
            return
        draft = self.get_knowledge_draft_version(tenant_id, source["item_id"])
        if not draft:
            return
        paragraphs = self.conn.execute(
            "SELECT * FROM kb2_paragraphs WHERE tenant_id=? AND source_id=? ORDER BY ordinal",
            (tenant_id, source["id"]),
        ).fetchall()
        body = "\n".join(row["text"].strip() for row in paragraphs
                         if not row["excluded"] and row["text"].strip())
        content = _json_load(draft["content_json"], {})
        content["title"] = content.get("title") or source["title"] or source["filename"]
        content["body"] = body
        if body:
            content.pop("kb2_registered_only", None)
        else:
            content["kb2_registered_only"] = True
        self.conn.execute(
            "UPDATE knowledge_versions SET content_json=? WHERE tenant_id=? AND id=?",
            (json.dumps(content, ensure_ascii=False), tenant_id, draft["id"]),
        )

    # ── B2-B08 精确来源所有权：当前草稿归属哪个原件 ───────────────────────

    def _kb2_item_current_original_source_id(self, tenant_id: str, item_id: str):
        """条目当前内容的原件归属（knowledge_sources id）：优先当前草稿，其次当前线上版本。"""
        draft = self.get_knowledge_draft_version(tenant_id, item_id)
        if draft:
            return draft["source_id"]
        published = self.get_published_knowledge_version(tenant_id, item_id)
        return published["source_id"] if published else None

    def _kb2_draft_owned_by_other_original(self, tenant_id: str, source) -> bool:
        """当前草稿是否属于其他原件（B2-B08）：历史来源只能读，不能改写/发布属于更新原件的草稿。

        仅当双方归属都可确定且不同才判定；无法证实归属（含旧视图缺字段）时不拦截。
        """
        if source is None:
            return False
        try:
            item_id = source["item_id"] or ""
            mine = source["knowledge_source_id"]
        except (KeyError, IndexError):
            return False
        if not item_id:
            return False
        owner = self._kb2_item_current_original_source_id(tenant_id, item_id)
        return bool(owner and mine and owner != mine)

    def _kb2_require_source_owns_current_draft(self, tenant_id: str, source, *, action: str) -> None:
        """历史来源不得刷新 / 编辑 / 核对 / 补人工正文或发布属于其他原件的当前草稿。

        拒绝发生在任何写入之前：当前草稿、当前线上版本、批次状态与历史版本保持不变；
        恢复历史内容必须显式进行（从版本记录恢复为新草稿），不使用旧批次静默改向新草稿。
        """
        if self._kb2_draft_owned_by_other_original(tenant_id, source):
            raise ValueError(
                "该来源属于历史原件：当前草稿来自更新版本（文件更新 / 采用模板后的新草稿），"
                f"不能{action}这份属于其他原件的草稿。如需这份历史内容，请从版本记录显式恢复为新草稿；"
                "本批次发布不会覆盖当前草稿。")

    def _kb2_require_batch_settings_ownership(self, tenant_id: str, batch_id: str) -> None:
        """保存发布设置前的全批次只读守卫（B2-B08 设置补充）。

        保存设置会写入「当前草稿」的用途 / AI / 到期：来源必须确切拥有其资料当前内容的原件身份
        （当前草稿优先、否则当前线上版本的 knowledge_sources）。历史来源或身份尚未确认的来源
        （识别失败 / 未认领原件）一律整批拒绝——拒绝发生在任何写入之前，不以空身份放行；
        混合批次不做部分写入。当前草稿所在批次保持可用。
        """
        for source in self.conn.execute(
            "SELECT * FROM kb2_sources WHERE tenant_id=? AND batch_id=? ORDER BY created_at, id",
            (tenant_id, batch_id),
        ).fetchall():
            if not source["item_id"]:
                continue
            owner = self._kb2_item_current_original_source_id(tenant_id, source["item_id"])
            if not owner or not source["knowledge_source_id"] or owner != source["knowledge_source_id"]:
                raise ValueError(
                    "不能保存该批次的发布设置：「%s」不是当前内容的原件（属于历史版本，或原件身份尚未确认），"
                    "保存设置会改写当前草稿的用途 / AI / 到期。请到当前草稿所在批次设置。"
                    % (source["title"] or source["filename"]))

    def _kb2_validate_publish_draft(self, tenant_id: str, item_id: str, draft) -> None:
        """共享发布服务（publish_knowledge_draft）的第二批闸门（B2-B09，不只在批次页面）。

        - 只对第二批入库资料生效：条目没有任何第二批来源时直接通过（第一批旧资料保持兼容）；
        - 草稿正文出处必须是本资料已登记的第二批原件（身份确切；缺身份 / 不匹配一律拒绝）；
        - 全部收录（未排除且有文字）段落必须已核对（与批次发布同一口径，带段号定位）；
        - 正文必须与出处来源的当前段落一致：被编辑过的正文不能带着漂移绕过核对来源；
        - 替换原件尚未识别成功（失败 / 处理中且未认领身份）时，草稿仍借用上一版正文，不能发布
          （旧线上版本保持不变；失败原件与草稿保留，可重试或补人工正文）。
        全部拒绝都发生在任何写入之前。
        """
        marker = self.conn.execute(
            "SELECT 1 FROM kb2_sources WHERE tenant_id=? AND item_id=? LIMIT 1", (tenant_id, item_id)
        ).fetchone()
        if not marker:
            return  # 非第二批资料（旧文章 / FAQ / 产品 / 旧手工资料）：维持第一批行为
        draft_source_id = str(draft["source_id"] or "")
        source = None
        if draft_source_id:
            source = self.conn.execute(
                "SELECT * FROM kb2_sources WHERE tenant_id=? AND item_id=? AND knowledge_source_id=?",
                (tenant_id, item_id, draft_source_id),
            ).fetchone()
        if source is None:
            raise ValueError(
                "不能发布：这份草稿的正文出处不是本资料已登记的第二批原件（来源身份缺失或不匹配）。"
                "请回到资料整理 / 核对页面重新核对并发布。")
        paragraphs = self.conn.execute(
            "SELECT * FROM kb2_paragraphs WHERE tenant_id=? AND source_id=? ORDER BY ordinal",
            (tenant_id, source["id"]),
        ).fetchall()
        unchecked = [index for index, paragraph in enumerate(paragraphs, start=1)
                     if not paragraph["excluded"] and paragraph["text"].strip() and not paragraph["checked"]]
        if unchecked:
            raise ValueError("不能发布：还有 %d 段未核对（第 %s 段）：逐段核对后才能发布"
                             % (len(unchecked), "、".join(str(index) for index in unchecked)))
        projection = "\n".join(paragraph["text"].strip() for paragraph in paragraphs
                               if not paragraph["excluded"] and paragraph["text"].strip())
        body = str(_json_load(draft["content_json"], {}).get("body") or "")
        if body != projection:
            raise ValueError(
                "不能发布：草稿正文与「已核对出处」不一致（正文被编辑过，不能带着漂移发布）。"
                "请回到资料整理 / 核对页面保存核对后的正文，再发布。")
        for unresolved in self.conn.execute(
            "SELECT * FROM kb2_sources WHERE tenant_id=? AND item_id=? AND replace_of_version_id IS NOT NULL "
            "AND knowledge_source_id IS NULL AND status IN ('failed', 'pending', 'processing')",
            (tenant_id, item_id),
        ).fetchall():
            replaced = self.get_knowledge_version(tenant_id, unresolved["replace_of_version_id"])
            if replaced and replaced["item_id"] == item_id and str(replaced["source_id"] or "") == draft_source_id:
                raise ValueError(
                    "不能发布：替换的原件尚未识别成功（失败或处理中）：这份草稿仍借用上一版本正文，"
                    "发布会把旧正文当作新原件的结果。请重试识别，或补充人工正文并逐段核对后再发布；旧线上版本不变。")

    def kb2_item_publish_context(self, tenant_id: str, item_id: str) -> dict | None:
        """条目当前草稿（无草稿时取当前线上版本）的发布位置：所属原件与批次。

        供字段页 / 资料页直达正确的发布设置（B2-B08：不把用户带向已被替换的历史批次）。
        资料没有第二批来源时返回 None。
        """
        if not self.get_knowledge_item(tenant_id, item_id):
            return None
        draft = self.get_knowledge_draft_version(tenant_id, item_id)
        reference = draft if draft is not None else self.get_published_knowledge_version(tenant_id, item_id)
        source = None
        if reference is not None and reference["source_id"]:
            source = self.conn.execute(
                "SELECT * FROM kb2_sources WHERE tenant_id=? AND item_id=? AND knowledge_source_id=? "
                "ORDER BY created_at DESC, id DESC LIMIT 1",
                (tenant_id, item_id, reference["source_id"]),
            ).fetchone()
        if source is None:
            source = self.conn.execute(
                "SELECT * FROM kb2_sources WHERE tenant_id=? AND item_id=? ORDER BY created_at DESC, id DESC LIMIT 1",
                (tenant_id, item_id),
            ).fetchone()
        if source is None:
            return None
        return {
            "item_id": item_id, "source_id": source["id"], "batch_id": source["batch_id"],
            "draft": draft is not None, "source_status": source["status"],
            "source_title": source["title"] or source["filename"],
        }

    def kb2_retry_source(self, tenant_id: str, user_id: str, source_id: str) -> None:
        """重试处理失败的来源任务：不重复创建资料条目 / 不覆盖人工修改。"""
        self._require_knowledge_editor(tenant_id, user_id)
        source = self._kb2_require_source(tenant_id, source_id)
        self._kb2_require_source_owns_current_draft(tenant_id, source, action="重新处理")
        if source["status"] not in ("failed", "pending", "processing"):
            raise ValueError("只有失败或待处理的来源才能重试")
        rows = self.conn.execute(
            "SELECT checked, edited, excluded FROM kb2_paragraphs WHERE tenant_id=? AND source_id=?",
            (tenant_id, source_id),
        ).fetchall()
        if any(row["checked"] or row["edited"] or row["excluded"] for row in rows):
            raise ValueError("该来源段落已有人工修改：不自动重建，请继续人工整理")
        if self._kb2_claim_held_elsewhere(tenant_id, source_id):
            raise ValueError("该来源正在处理中：无需重试，请等待本轮处理完成")
        self.conn.execute(
            "UPDATE kb2_sources SET status='pending', error='', updated_at=? WHERE tenant_id=? AND id=?",
            (_now(), tenant_id, source_id),
        )
        self.conn.commit()
        self._kb2_process_source(tenant_id, source_id)

    # ── 逐段核对 / 编辑 / 排除 / 人工正文 / 字段来源 ──────────────────────

    def _kb2_require_paragraph(self, tenant_id: str, paragraph_id: str):
        row = self.conn.execute(
            "SELECT * FROM kb2_paragraphs WHERE tenant_id=? AND id=?", (tenant_id, paragraph_id)
        ).fetchone()
        if not row:
            raise ValueError("段落不存在或不属于本企业")
        return row

    def kb2_update_paragraph(self, tenant_id: str, user_id: str, paragraph_id: str, *,
                             text=_UNSET, checked=_UNSET, excluded=_UNSET) -> None:
        """核对 / 编辑（撤销核对并记人工修改）/ 排除段落；编辑已发布资料转为新草稿。"""
        self._require_knowledge_editor(tenant_id, user_id)
        paragraph = self._kb2_require_paragraph(tenant_id, paragraph_id)
        source = self._kb2_source_row(tenant_id, paragraph["source_id"])
        if not source:
            raise ValueError("来源任务不存在或不属于本企业")
        item_id = source["item_id"] or paragraph["item_id"]
        if item_id:
            self._kb2_require_source_owns_current_draft(tenant_id, source, action="改写或核对段落")
            item = self.get_knowledge_item(tenant_id, item_id)
            if item and item["status"] == "published" and not item["draft_version_id"]:
                self._kb2_ensure_item_draft(tenant_id, item_id)
        changes = {}
        if text is not _UNSET:
            new_text = str(text)
            if new_text != paragraph["text"]:
                changes["text"] = new_text
                changes["origin"] = "edited"
                changes["edited"] = 1
                if checked is _UNSET:
                    checked = False  # 改了文字：该段回到待核对
        if checked is not _UNSET:
            changes["checked"] = 1 if checked else 0
        if excluded is not _UNSET:
            changes["excluded"] = 1 if excluded else 0
        if not changes:
            return
        changes["updated_at"] = _now()
        assignments = ", ".join(f"{key}=?" for key in changes)
        self.conn.execute(
            f"UPDATE kb2_paragraphs SET {assignments} WHERE tenant_id=? AND id=?",
            (*changes.values(), tenant_id, paragraph_id),
        )
        self.conn.commit()
        fresh = self._kb2_source_row(tenant_id, source["id"])
        self._kb2_refresh_item_draft_body(tenant_id, fresh)
        self.conn.commit()

    def kb2_add_manual_paragraph(self, tenant_id: str, user_id: str, source_id: str, text: str) -> str:
        """为来源补充人工正文（标注人工撰写；不冒充原件原文；发布前同样要核对）。"""
        self._require_knowledge_editor(tenant_id, user_id)
        source = self._kb2_require_source(tenant_id, source_id)
        text = str(text or "").strip()
        if not text:
            raise ValueError("人工正文不能为空")
        self._kb2_require_source_owns_current_draft(tenant_id, source, action="补充人工正文")
        if not source["item_id"] or not source["knowledge_source_id"]:
            # 补充人工正文即产生该来源自己的内容：补齐条目与原件身份（识别失败 / 中断的来源也认领），
            # 后续发布按确切身份走——不以空身份借用旧正文（B2-B08 失败换版）。
            self._kb2_create_material_item(tenant_id, source, source["title"] or source["filename"])
            source = self._kb2_source_row(tenant_id, source_id)
        self._kb2_ensure_item_draft(tenant_id, source["item_id"])
        next_ordinal = self.conn.execute(
            "SELECT COALESCE(MAX(ordinal), -1) + 1 FROM kb2_paragraphs WHERE tenant_id=? AND source_id=?",
            (tenant_id, source_id),
        ).fetchone()[0]
        paragraph_id = secrets.token_hex(12)
        timestamp = _now()
        self.conn.execute(
            "INSERT INTO kb2_paragraphs(id, tenant_id, source_id, item_id, ordinal, text, location_json, source_kind, origin, checked, edited, excluded, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, 'manual', 'manual', 0, 0, 0, ?, ?)",
            (paragraph_id, tenant_id, source_id, source["item_id"], next_ordinal, text,
             json.dumps({"kind": "manual"}, ensure_ascii=False), timestamp, timestamp),
        )
        self.conn.commit()
        fresh = self._kb2_source_row(tenant_id, source_id)
        self._kb2_refresh_item_draft_body(tenant_id, fresh)
        self.conn.commit()
        self._audit(tenant_id, "kb2_source", source_id, "manual_paragraph_added", source["created_by"], "")
        self.conn.commit()  # 审计落盘（不留隐形写事务在连接上）
        return paragraph_id

    def kb2_set_source_field(self, tenant_id: str, user_id: str, source_id: str, *, label: str,
                             value: str, unit: str = "", paragraph_id: str | None = None,
                             checked: bool = False, field_id: str | None = None) -> str:
        """补充字段：来源必须对应真实 source/version/location（可钉住具体段落）。"""
        editor = self._require_knowledge_editor(tenant_id, user_id)
        source = self._kb2_require_source(tenant_id, source_id)
        label = str(label or "").strip()
        value = str(value if value is not None else "")
        if not label:
            raise ValueError("字段名称不能为空")
        if value.strip() == "":
            raise ValueError("字段内容不能为空（0 与「否」都是有效值，请如实填写）")
        unit = str(unit or "").strip()
        if paragraph_id is not None:
            paragraph = self._kb2_require_paragraph(tenant_id, str(paragraph_id))
            if paragraph["source_id"] != source_id:
                raise ValueError("字段来源段落不属于该资料")
        timestamp = _now()
        if field_id:
            existing = self.conn.execute(
                "SELECT id FROM kb2_source_fields WHERE tenant_id=? AND id=? AND source_id=?",
                (tenant_id, field_id, source_id),
            ).fetchone()
            if not existing:
                raise ValueError("字段不存在或不属于该资料")
            self.conn.execute(
                "UPDATE kb2_source_fields SET label=?, value=?, unit=?, paragraph_id=?, checked=?, updated_at=? "
                "WHERE tenant_id=? AND id=?",
                (label, value, unit, paragraph_id, 1 if checked else 0, timestamp, tenant_id, field_id),
            )
        else:
            field_id = secrets.token_hex(12)
            self.conn.execute(
                "INSERT INTO kb2_source_fields(id, tenant_id, source_id, item_id, label, value, unit, paragraph_id, checked, created_by, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (field_id, tenant_id, source_id, source["item_id"], label, value, unit, paragraph_id,
                 1 if checked else 0, editor["email"], timestamp, timestamp),
            )
        self.conn.commit()
        return field_id

    # ── 批次读取（含逐段草稿、核对计数、字段、建议）─────────────────────────

    def _kb2_source_identification(self, source, paragraphs: list) -> tuple[str, str]:
        if source["status"] in ("pending", "processing"):
            code = "processing" if source["status"] == "processing" else "pending"
        elif source["status"] == "failed":
            code = "failed"
        else:
            real = [p for p in paragraphs if p["source_kind"] != "manual"]
            if real:
                code = "extracted"
            elif paragraphs:
                code = "manual"
            else:
                code = "registered"
        return code, KB2_IDENTIFICATION_LABELS[code]

    def kb2_get_batch(self, tenant_id: str, batch_id: str) -> dict | None:
        batch = self._kb2_batch_row(tenant_id, batch_id)
        if not batch:
            return None
        sources = self.conn.execute(
            "SELECT * FROM kb2_sources WHERE tenant_id=? AND batch_id=? ORDER BY created_at, id",
            (tenant_id, batch_id),
        ).fetchall()
        source_views = []
        summary = {"sources": len(sources), "extracted": 0, "registered": 0, "failed": 0, "pending": 0}
        for source in sources:
            paragraphs_rows = self.conn.execute(
                "SELECT * FROM kb2_paragraphs WHERE tenant_id=? AND source_id=? ORDER BY ordinal",
                (tenant_id, source["id"]),
            ).fetchall()
            paragraphs = []
            for row in paragraphs_rows:
                location = _json_load(row["location_json"], {})
                paragraphs.append({
                    "id": row["id"], "ordinal": row["ordinal"], "text": row["text"],
                    "location": location, "location_label": self.kb2_location_label(location),
                    "source_kind": row["source_kind"], "origin": row["origin"],
                    "edited": bool(row["edited"]), "checked": bool(row["checked"]),
                    "excluded": bool(row["excluded"]), "updated_at": row["updated_at"],
                })
            fields_rows = self.conn.execute(
                "SELECT * FROM kb2_source_fields WHERE tenant_id=? AND source_id=? ORDER BY created_at, id",
                (tenant_id, source["id"]),
            ).fetchall()
            fields = []
            for row in fields_rows:
                paragraph_location = None
                paragraph_label = ""
                if row["paragraph_id"]:
                    paragraph_row = self.conn.execute(
                        "SELECT location_json FROM kb2_paragraphs WHERE tenant_id=? AND id=?",
                        (tenant_id, row["paragraph_id"]),
                    ).fetchone()
                    if paragraph_row:
                        paragraph_location = _json_load(paragraph_row["location_json"], {})
                        paragraph_label = self.kb2_location_label(paragraph_location)
                fields.append({
                    "id": row["id"], "label": row["label"], "value": row["value"], "unit": row["unit"],
                    "paragraph_id": row["paragraph_id"], "paragraph_location": paragraph_location,
                    "paragraph_location_label": paragraph_label, "checked": bool(row["checked"]),
                    "source_id": row["source_id"], "created_at": row["created_at"],
                })
            identification, identification_label = self._kb2_source_identification(source, paragraphs)
            item = self.get_knowledge_item(tenant_id, source["item_id"]) if source["item_id"] else None
            summary_key = {"extracted": "extracted", "registered": "registered", "failed": "failed"}.get(source["status"])
            if summary_key:
                summary[summary_key] += 1
            elif source["status"] in ("pending", "processing"):
                summary["pending"] += 1
            source_views.append({
                "id": source["id"], "kind": source["kind"], "filename": source["filename"],
                "title": source["title"], "format": source["format"],
                "format_label": KB2_FORMAT_LABELS.get(source["format"], source["format"]),
                "size": source["size"], "sha256": source["sha256"],
                "source_location": source["source_location"], "status": source["status"],
                "identification": identification, "identification_label": identification_label,
                "error": source["error"], "warnings": _json_load(source["warnings_json"], []),
                "attempts": source["attempts"], "item_id": source["item_id"],
                "knowledge_source_id": source["knowledge_source_id"],
                "item_status": item["status"] if item else None,
                "paragraphs": paragraphs, "fields": fields,
                "counts": {
                    "paragraphs": len(paragraphs),
                    "pending_check": len([p for p in paragraphs if not p["checked"] and not p["excluded"]]),
                    "excluded": len([p for p in paragraphs if p["excluded"]]),
                    "manual": len([p for p in paragraphs if p["source_kind"] == "manual"]),
                },
                "created_at": source["created_at"], "updated_at": source["updated_at"],
            })
        suggestions = []
        for row in self.conn.execute(
            "SELECT * FROM kb2_topic_suggestions WHERE tenant_id=? AND batch_id=? ORDER BY created_at, id",
            (tenant_id, batch_id),
        ).fetchall():
            topic = self._kb2_topic_row(tenant_id, row["existing_topic_id"]) if row["existing_topic_id"] else None
            suggestions.append({
                "id": row["id"], "name": row["name"],
                "existing_topic_id": row["existing_topic_id"],
                "existing_topic_title": topic["title"] if topic else None,
                "basis": row["basis"], "status": row["status"],
                "source_ids": _json_load(row["source_ids_json"], []),
                "created_at": row["created_at"], "updated_at": row["updated_at"],
            })
        return {
            "id": batch["id"], "status": batch["status"], "purpose": batch["purpose"],
            "usage": batch["usage"], "ai_authorized": bool(batch["ai_authorized"]),
            "expires_at": batch["expires_at"], "created_by": batch["created_by"],
            "created_at": batch["created_at"], "updated_at": batch["updated_at"],
            "confirmed_at": batch["confirmed_at"], "published_at": batch["published_at"],
            "sources": source_views, "suggestions": suggestions, "summary": summary,
        }

    # ── 主题建议（真实文字命中 / 成员输入；确认前不建立）────────────────────

    def kb2_list_batches(self, tenant_id: str, limit: int = 30) -> list[dict]:
        """批次清单（正式界面找回已保存批次；只读，不改变任何状态）。

        只列「添加资料」批次；文件更新（替换）批次从资料页进入，不混入最近批次列表。
        """
        rows = self.conn.execute(
            "SELECT * FROM kb2_batches WHERE tenant_id=? AND purpose='upload' ORDER BY created_at DESC, id DESC LIMIT ?",
            (tenant_id, max(1, int(limit))),
        ).fetchall()
        out = []
        for batch in rows:
            counts = self.conn.execute(
                "SELECT COUNT(*) AS total, "
                "SUM(CASE WHEN status IN ('pending','processing') THEN 1 ELSE 0 END) AS running "
                "FROM kb2_sources WHERE tenant_id=? AND batch_id=?",
                (tenant_id, batch["id"]),
            ).fetchone()
            out.append({
                "id": batch["id"], "status": batch["status"],
                "usage": batch["usage"], "ai_authorized": bool(batch["ai_authorized"]),
                "created_by": batch["created_by"],
                "created_at": batch["created_at"], "updated_at": batch["updated_at"],
                "confirmed_at": batch["confirmed_at"], "published_at": batch["published_at"],
                "sources_total": counts["total"] or 0,
                "sources_pending": counts["running"] or 0,
            })
        return out

    def _kb2_list_suggestions(self, tenant_id: str, batch_id: str) -> list[dict]:
        return self.kb2_get_batch(tenant_id, batch_id)["suggestions"]

    def kb2_build_topic_suggestions(self, tenant_id: str, user_id: str, batch_id: str) -> list[dict]:
        """系统建议（真实本地信号；确认前不建立主题）：

        - 文字命中：现有主题名出现在某件资料正文文字里（basis=text_match）；
        - 新主题候选（任务3 补齐）：依据资料**实际标题**（basis=source_title）与
          **标题段落**（DOCX 标题段，basis=source_heading）生成，一律只是候选、待成员确认，
          不声称模型摘要，也不声称读取扫描件/图片（这类来源没有标题段落，不产出候选）；
        - 不改写成员输入与已拒绝的建议（拒绝不复活）；同批同名合并为一条。
        """
        self._require_knowledge_editor(tenant_id, user_id)
        self._kb2_require_batch(tenant_id, batch_id)
        topics = self.kb2_list_topics(tenant_id)
        source_rows = self.conn.execute(
            "SELECT * FROM kb2_sources WHERE tenant_id=? AND batch_id=? ORDER BY created_at, id",
            (tenant_id, batch_id),
        ).fetchall()
        for source in source_rows:
            paragraphs = self.conn.execute(
                "SELECT text, source_kind FROM kb2_paragraphs WHERE tenant_id=? AND source_id=? ORDER BY ordinal",
                (tenant_id, source["id"]),
            ).fetchall()
            blob = " ".join([source["title"]] + [row["text"] for row in paragraphs]).lower()
            for topic in topics:
                name = topic["title"].strip()
                if len(name) < 2 or name.lower() not in blob:
                    continue
                existing = self.conn.execute(
                    "SELECT * FROM kb2_topic_suggestions WHERE tenant_id=? AND batch_id=? AND existing_topic_id=? AND lower(name)=lower(?)",
                    (tenant_id, batch_id, topic["id"], name),
                ).fetchone()
                if existing:
                    if existing["status"] == "rejected":
                        continue  # 尊重成员拒绝：不复活
                    if existing["status"] != "pending":
                        continue
                    source_ids = _json_load(existing["source_ids_json"], [])
                    if source["id"] not in source_ids:
                        source_ids.append(source["id"])
                        self.conn.execute(
                            "UPDATE kb2_topic_suggestions SET source_ids_json=?, updated_at=? WHERE id=?",
                            (json.dumps(source_ids, ensure_ascii=False), _now(), existing["id"]),
                        )
                else:
                    self.conn.execute(
                        "INSERT INTO kb2_topic_suggestions(id, tenant_id, batch_id, name, existing_topic_id, basis, source_ids_json, status, created_by, created_at, updated_at) "
                        "VALUES (?, ?, ?, ?, ?, 'text_match', ?, 'pending', ?, ?, ?)",
                        (secrets.token_hex(12), tenant_id, batch_id, name, topic["id"],
                         json.dumps([source["id"]], ensure_ascii=False), user_id, _now(), _now()),
                    )
            if source["status"] == "failed":
                continue  # 没有可依据内容的失败资料不产出候选
            candidates = []
            title = str(source["title"] or "").strip()
            if title:
                candidates.append((title, "source_title"))
            for row in paragraphs:
                if row["source_kind"] == "docx_heading":
                    heading = str(row["text"] or "").strip()
                    if heading:
                        candidates.append((heading, "source_heading"))
            for raw_name, basis in candidates:
                name = re.sub(r"\s+", " ", raw_name).strip()
                if not (2 <= len(name) <= 40):
                    continue
                if any(name.lower() == topic["title"].strip().lower() for topic in topics):
                    continue  # 已有同名主题：走文字命中路径，不重复造候选
                existing = self.conn.execute(
                    "SELECT * FROM kb2_topic_suggestions WHERE tenant_id=? AND batch_id=? AND lower(name)=lower(?)",
                    (tenant_id, batch_id, name),
                ).fetchone()
                if existing:
                    if existing["status"] == "rejected":
                        continue  # 尊重成员拒绝：不复活
                    if existing["status"] != "pending":
                        continue  # 已确认 / 已合并的历史建议不再改写
                    source_ids = _json_load(existing["source_ids_json"], [])
                    if source["id"] not in source_ids:
                        source_ids.append(source["id"])
                        self.conn.execute(
                            "UPDATE kb2_topic_suggestions SET source_ids_json=?, updated_at=? WHERE id=?",
                            (json.dumps(source_ids, ensure_ascii=False), _now(), existing["id"]),
                        )
                else:
                    self.conn.execute(
                        "INSERT INTO kb2_topic_suggestions(id, tenant_id, batch_id, name, existing_topic_id, basis, source_ids_json, status, created_by, created_at, updated_at) "
                        "VALUES (?, ?, ?, ?, NULL, ?, ?, 'pending', ?, ?, ?)",
                        (secrets.token_hex(12), tenant_id, batch_id, name, basis,
                         json.dumps([source["id"]], ensure_ascii=False), user_id, _now(), _now()),
                    )
        self.conn.commit()
        return self._kb2_list_suggestions(tenant_id, batch_id)

    def _kb2_batch_source_ids(self, tenant_id: str, batch_id: str) -> set[str]:
        rows = self.conn.execute(
            "SELECT id FROM kb2_sources WHERE tenant_id=? AND batch_id=?", (tenant_id, batch_id)
        ).fetchall()
        return {row["id"] for row in rows}

    def _kb2_match_topic_by_name(self, tenant_id: str, name: str):
        return self.conn.execute(
            "SELECT * FROM kb2_topics WHERE tenant_id=? AND lower(title)=lower(?) ORDER BY created_at, id LIMIT 1",
            (tenant_id, name.strip()),
        ).fetchone()

    def kb2_add_topic_suggestion(self, tenant_id: str, user_id: str, batch_id: str,
                                 name: str, source_ids: list | None = None) -> str:
        """成员添加主题建议（含整批共同主题）；同名建议合并为一条；确认前不建立主题。"""
        editor = self._require_knowledge_editor(tenant_id, user_id)
        self._kb2_require_batch(tenant_id, batch_id)
        name = str(name or "").strip()
        if not name:
            raise ValueError("主题建议名称不能为空")
        valid_ids = self._kb2_batch_source_ids(tenant_id, batch_id)
        if source_ids is None:
            chosen = sorted(valid_ids)
        else:
            chosen = [str(sid) for sid in source_ids]
            invalid = [sid for sid in chosen if sid not in valid_ids]
            if invalid:
                raise ValueError("主题建议包含不属于该批次的资料")
            chosen = sorted(set(chosen))
        existing = self.conn.execute(
            "SELECT * FROM kb2_topic_suggestions WHERE tenant_id=? AND batch_id=? AND lower(name)=lower(?)",
            (tenant_id, batch_id, name),
        ).fetchone()
        match = self._kb2_match_topic_by_name(tenant_id, name)
        existing_topic_id = match["id"] if match else None
        timestamp = _now()
        if existing:
            merged_sources = sorted(set(_json_load(existing["source_ids_json"], [])) | set(chosen))
            status = "pending" if existing["status"] in ("rejected", "pending") else existing["status"]
            self.conn.execute(
                "UPDATE kb2_topic_suggestions SET source_ids_json=?, status=?, existing_topic_id=?, basis='member', updated_at=? WHERE id=?",
                (json.dumps(merged_sources, ensure_ascii=False), status, existing_topic_id, timestamp, existing["id"]),
            )
            self.conn.commit()
            return existing["id"]
        suggestion_id = secrets.token_hex(12)
        self.conn.execute(
            "INSERT INTO kb2_topic_suggestions(id, tenant_id, batch_id, name, existing_topic_id, basis, source_ids_json, status, created_by, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, 'member', ?, 'pending', ?, ?, ?)",
            (suggestion_id, tenant_id, batch_id, name, existing_topic_id,
             json.dumps(chosen, ensure_ascii=False), editor["email"], timestamp, timestamp),
        )
        self.conn.commit()
        return suggestion_id

    def _kb2_require_suggestion(self, tenant_id: str, suggestion_id: str):
        row = self.conn.execute(
            "SELECT * FROM kb2_topic_suggestions WHERE tenant_id=? AND id=?", (tenant_id, suggestion_id)
        ).fetchone()
        if not row:
            raise ValueError("主题建议不存在或不属于本企业")
        return row

    def kb2_rename_topic_suggestion(self, tenant_id: str, user_id: str, suggestion_id: str, name: str) -> None:
        editor = self._require_knowledge_editor(tenant_id, user_id)
        suggestion = self._kb2_require_suggestion(tenant_id, suggestion_id)
        if suggestion["status"] != "pending":
            raise ValueError("只有待确认的建议才能改名")
        name = str(name or "").strip()
        if not name:
            raise ValueError("主题建议名称不能为空")
        match = self._kb2_match_topic_by_name(tenant_id, name)
        self.conn.execute(
            "UPDATE kb2_topic_suggestions SET name=?, existing_topic_id=?, updated_at=? WHERE id=?",
            (name, match["id"] if match else None, _now(), suggestion_id),
        )
        self._audit(tenant_id, "kb2_batch", suggestion["batch_id"], "suggestion_renamed", editor["email"], f"name={name}")
        self.conn.commit()

    def kb2_merge_topic_suggestion(self, tenant_id: str, user_id: str, suggestion_id: str, topic_id: str) -> None:
        editor = self._require_knowledge_editor(tenant_id, user_id)
        suggestion = self._kb2_require_suggestion(tenant_id, suggestion_id)
        if suggestion["status"] != "pending":
            raise ValueError("只有待确认的建议才能合并")
        self._kb2_require_topic(tenant_id, topic_id)
        self.conn.execute(
            "UPDATE kb2_topic_suggestions SET existing_topic_id=?, status='pending', updated_at=? WHERE id=?",
            (topic_id, _now(), suggestion_id),
        )
        self._audit(tenant_id, "kb2_batch", suggestion["batch_id"], "suggestion_merged", editor["email"],
                    f"into={topic_id[:12]}")
        self.conn.commit()

    def kb2_reject_topic_suggestion(self, tenant_id: str, user_id: str, suggestion_id: str) -> None:
        editor = self._require_knowledge_editor(tenant_id, user_id)
        suggestion = self._kb2_require_suggestion(tenant_id, suggestion_id)
        if suggestion["status"] == "rejected":
            return  # 幂等
        if suggestion["status"] != "pending":
            raise ValueError("已确认的建议不能拒绝")
        self.conn.execute(
            "UPDATE kb2_topic_suggestions SET status='rejected', updated_at=? WHERE id=?",
            (_now(), suggestion_id),
        )
        self._audit(tenant_id, "kb2_batch", suggestion["batch_id"], "suggestion_rejected", editor["email"],
                    f"name={suggestion['name']}")
        self.conn.commit()

    def kb2_set_suggestion_sources(self, tenant_id: str, user_id: str, suggestion_id: str, source_ids: list) -> None:
        """调整建议覆盖的资料（可逐件取消某个建议，单项仍保留自己的选择）。"""
        self._require_knowledge_editor(tenant_id, user_id)
        suggestion = self._kb2_require_suggestion(tenant_id, suggestion_id)
        if suggestion["status"] != "pending":
            raise ValueError("只有待确认的建议才能调整资料")
        valid_ids = self._kb2_batch_source_ids(tenant_id, suggestion["batch_id"])
        chosen = sorted({str(sid) for sid in (source_ids or [])})
        invalid = [sid for sid in chosen if sid not in valid_ids]
        if invalid:
            raise ValueError("调整结果包含不属于该批次的资料")
        self.conn.execute(
            "UPDATE kb2_topic_suggestions SET source_ids_json=?, updated_at=? WHERE id=?",
            (json.dumps(chosen, ensure_ascii=False), _now(), suggestion_id),
        )
        self.conn.commit()

    def kb2_confirm_batch_topics(self, tenant_id: str, user_id: str, batch_id: str) -> dict:
        """确认主题与关联：确认前不建议中主题不存在；同批同名合并；重复确认幂等。"""
        self._require_knowledge_editor(tenant_id, user_id)
        batch = self._kb2_require_batch(tenant_id, batch_id)
        pending = self.conn.execute(
            "SELECT * FROM kb2_topic_suggestions WHERE tenant_id=? AND batch_id=? AND status='pending' ORDER BY created_at, id",
            (tenant_id, batch_id),
        ).fetchall()
        topics_created = []
        created_by_name: dict[str, str] = {}
        relations_created = 0
        skipped = []
        for suggestion in pending:
            name = suggestion["name"].strip()
            if not name:
                continue
            target = None
            if suggestion["existing_topic_id"]:
                target = self._kb2_topic_row(tenant_id, suggestion["existing_topic_id"])
            if target is None:
                target = self._kb2_match_topic_by_name(tenant_id, name)
            if target is None and name.lower() in created_by_name:
                target = self._kb2_topic_row(tenant_id, created_by_name[name.lower()])
            if target is None:
                topic_id = self.kb2_create_topic(tenant_id, user_id, name)
                created_by_name[name.lower()] = topic_id
                topics_created.append({"id": topic_id, "title": name})
                target = self._kb2_topic_row(tenant_id, topic_id)
            else:
                created_by_name.setdefault(name.lower(), target["id"])
            for source_id in _json_load(suggestion["source_ids_json"], []):
                source = self._kb2_source_row(tenant_id, str(source_id))
                if not source or not source["item_id"]:
                    skipped.append({"suggestion_id": suggestion["id"], "source_id": str(source_id),
                                    "reason": "资料还没有可关联的条目（未能识别或仍在处理）"})
                    continue
                if self._kb2_set_relation_internal(
                        tenant_id, user_id, target["id"],
                        {"kind": "knowledge", "id": source["item_id"]}, True, "batch_confirm"):
                    relations_created += 1
            self.conn.execute(
                "UPDATE kb2_topic_suggestions SET status='confirmed', updated_at=? WHERE id=?",
                (_now(), suggestion["id"]),
            )
        if batch["status"] == "draft":
            self.conn.execute(
                "UPDATE kb2_batches SET status='confirmed', confirmed_at=?, updated_at=? WHERE tenant_id=? AND id=?",
                (_now(), _now(), tenant_id, batch_id),
            )
        self.conn.commit()
        return {"batch_id": batch_id, "topics_created": topics_created,
                "relations_created": relations_created, "skipped": skipped}

    # ── 发布设置与批次发布（多文件继续；幂等；仅登记原件可归档）──────────────

    def kb2_save_batch_settings(self, tenant_id: str, user_id: str, batch_id: str, *,
                                usage: str = "internal", ai_authorized: bool = False,
                                expires_at=None) -> None:
        """保存发布设置到草稿（不发布；允许不完整草稿）。

        B2-B08 设置补充：先做全批次只读归属校验，拒绝发生在任何写入之前——历史来源 / 身份尚未
        确认的来源不能经保存设置改写属于其他原件的草稿；混合批次整批拒绝（不做部分写入）。
        """
        editor = self._require_knowledge_editor(tenant_id, user_id)
        self._kb2_require_batch(tenant_id, batch_id)
        if usage not in KNOWLEDGE_USAGES:
            raise ValueError("使用范围无效")
        self._kb2_require_batch_settings_ownership(tenant_id, batch_id)
        normalized = normalize_knowledge_expiry(expires_at)
        timestamp = _now()
        self.conn.execute(
            "UPDATE kb2_batches SET usage=?, ai_authorized=?, expires_at=?, updated_at=? WHERE tenant_id=? AND id=?",
            (usage, 1 if ai_authorized else 0, normalized, timestamp, tenant_id, batch_id),
        )
        for source in self.conn.execute(
            "SELECT * FROM kb2_sources WHERE tenant_id=? AND batch_id=?", (tenant_id, batch_id)
        ).fetchall():
            if not source["item_id"]:
                continue
            item = self.get_knowledge_item(tenant_id, source["item_id"])
            if item and item["draft_version_id"]:
                self.conn.execute(
                    "UPDATE knowledge_versions SET usage=?, ai_authorized=?, expires_at=? WHERE tenant_id=? AND id=?",
                    (usage, 1 if ai_authorized else 0, normalized, tenant_id, item["draft_version_id"]),
                )
        self._audit(tenant_id, "kb2_batch", batch_id, "settings_saved", editor["email"],
                    f"usage={usage};ai={1 if ai_authorized else 0}")
        self.conn.commit()

    def _kb2_publish_source(self, tenant_id: str, user_id: str, source, *, usage: str,
                            ai_authorized: bool, expires_at, confirm_ai: bool) -> dict:
        if source["status"] in ("pending", "processing"):
            raise ValueError("还在处理中：请稍后重试")
        if not source["knowledge_source_id"]:
            # B2-B08 失败换版：来源未成功认领原件身份（识别失败 / 中断）时，不得把旧正文当作
            # 新原件的结果发布或归档；拒绝在任何写入之前——失败原件与草稿保留（可重试 / 补人工
            # 正文），旧线上版本不变。
            raise ValueError(
                "该来源还没有可核对的原件身份（识别失败或未完成）：不能发布或归档；"
                "请先重试识别，或补充人工正文并逐段核对后再发布（失败的原件与草稿会保留，旧线上版本不变）。")
        if not source["item_id"]:
            raise ValueError(source["error"] or "未能识别：请先重试该来源或人工整理正文")
        item = self.get_knowledge_item(tenant_id, source["item_id"])
        if not item:
            raise ValueError("资料条目缺失")
        if item["status"] == "published" and not item["draft_version_id"]:
            return {"kind": "skipped", "source_id": source["id"], "item_id": item["id"], "reason": "已发布，跳过"}
        self._kb2_require_source_owns_current_draft(tenant_id, source, action="发布")
        paragraphs = self.conn.execute(
            "SELECT * FROM kb2_paragraphs WHERE tenant_id=? AND source_id=? ORDER BY ordinal",
            (tenant_id, source["id"]),
        ).fetchall()
        texts = [p for p in paragraphs if not p["excluded"] and p["text"].strip()]
        timestamp = _now()
        if texts:
            unchecked = [index for index, p in enumerate(paragraphs, start=1)
                         if not p["excluded"] and p["text"].strip() and not p["checked"]]
            if unchecked:
                raise ValueError(
                    "还有 %d 段未核对（第 %s 段）：逐段核对后才能发布"
                    % (len(unchecked), "、".join(str(i) for i in unchecked)))
            draft = self.get_knowledge_draft_version(tenant_id, source["item_id"])
            if not draft:
                raise ValueError("资料没有可发布的草稿")
            self.conn.execute(
                "UPDATE knowledge_versions SET usage=?, ai_authorized=?, expires_at=? WHERE tenant_id=? AND id=?",
                (usage, 1 if ai_authorized else 0, expires_at, tenant_id, draft["id"]),
            )
            self.conn.commit()
            fresh = self._kb2_source_row(tenant_id, source["id"])
            self._kb2_refresh_item_draft_body(tenant_id, fresh)
            self.conn.commit()
            version = self.publish_knowledge_draft(tenant_id, source["item_id"], user_id, confirm_ai=True)
            # 与第一批发布流程一致：发布后对新版本执行真实本地准备（处理状态不靠计时假完成）。
            self.prepare_knowledge_version(tenant_id, self.get_published_knowledge_version(
                tenant_id, source["item_id"])["id"])
            self.conn.execute(
                "UPDATE kb2_sources SET status='published', updated_at=? WHERE tenant_id=? AND id=?",
                (timestamp, tenant_id, source["id"]),
            )
            self.conn.commit()
            return {"kind": "published", "source_id": source["id"], "item_id": source["item_id"], "version": version}
        if any(p["text"].strip() for p in paragraphs):
            raise ValueError("已排除全部正文：没有可收录内容（排除段不进入知识）")
        # 仅登记原件：归档发布（AI 不可用；不算发布知识正文）。
        draft = self.get_knowledge_draft_version(tenant_id, source["item_id"])
        if not draft:
            return {"kind": "skipped", "source_id": source["id"], "item_id": source["item_id"], "reason": "已归档，跳过"}
        content = _json_load(draft["content_json"], {})
        content["kb2_registered_only"] = True
        content.setdefault("title", source["title"] or source["filename"])
        content.setdefault("body", "")
        self.conn.execute(
            "UPDATE knowledge_versions SET content_json=?, usage=?, ai_authorized=?, expires_at=? WHERE tenant_id=? AND id=?",
            (json.dumps(content, ensure_ascii=False), usage, 1 if ai_authorized else 0, expires_at, tenant_id, draft["id"]),
        )
        previous = self.get_published_knowledge_version(tenant_id, source["item_id"])
        if previous:
            self.conn.execute("UPDATE knowledge_versions SET status='superseded' WHERE id=?", (previous["id"],))
        self.conn.execute(
            "UPDATE knowledge_versions SET status='published', published_at=? WHERE id=?", (timestamp, draft["id"])
        )
        self.conn.execute(
            "UPDATE knowledge_items SET status='published', published_version_id=?, draft_version_id=NULL, updated_at=? "
            "WHERE tenant_id=? AND id=?",
            (draft["id"], timestamp, tenant_id, source["item_id"]),
        )
        self.conn.execute(
            "UPDATE kb2_sources SET status='published', updated_at=? WHERE tenant_id=? AND id=?",
            (timestamp, tenant_id, source["id"]),
        )
        self._audit(tenant_id, "kb2_source", source["id"], "registered_archived", source["created_by"],
                    f"item={source['item_id'][:8]};version={draft['version']}")
        self.conn.commit()
        return {"kind": "archived", "source_id": source["id"], "item_id": source["item_id"], "version": draft["version"]}

    def kb2_publish_batch(self, tenant_id: str, user_id: str, batch_id: str, *,
                          usage=_UNSET, ai_authorized=_UNSET, expires_at=_UNSET,
                          confirm_ai: bool = False) -> dict:
        """发布批次：逐个来源独立发布（单个失败不阻断其他）；重复发布幂等。

        - 文字型资料：需全部收录段落已核对（未核对 / 空正文拦截并定位）。
        - 仅登记原件（图片/视频/网页等）：归档发布，明确 AI 不可用、不算发布知识正文。
        - 对外 + 授权 AI 需要一次确认（在任何写入之前整体校验）。
        """
        self._require_knowledge_editor(tenant_id, user_id)
        batch = self._kb2_require_batch(tenant_id, batch_id)
        effective_usage = batch["usage"] if usage is _UNSET else usage
        effective_ai = bool(batch["ai_authorized"]) if ai_authorized is _UNSET else bool(ai_authorized)
        effective_expires = batch["expires_at"] if expires_at is _UNSET else normalize_knowledge_expiry(expires_at)
        if effective_usage not in KNOWLEDGE_USAGES:
            raise ValueError("使用范围无效")
        sources = self.conn.execute(
            "SELECT * FROM kb2_sources WHERE tenant_id=? AND batch_id=? ORDER BY created_at, id",
            (tenant_id, batch_id),
        ).fetchall()
        if not sources:
            raise ValueError("批次没有资料")
        has_content = False
        for source in sources:
            rows = self.conn.execute(
                "SELECT text, excluded FROM kb2_paragraphs WHERE tenant_id=? AND source_id=?",
                (tenant_id, source["id"]),
            ).fetchall()
            if any(not row["excluded"] and row["text"].strip() for row in rows):
                has_content = True
                break
        if effective_usage == "external" and effective_ai and has_content and not confirm_ai:
            raise ValueError("对外且授权 AI 客服使用的发布需要确认：请先确认再发布")
        results = {"published": [], "archived": [], "failed": [], "skipped": []}
        for source in sources:
            try:
                outcome = self._kb2_publish_source(
                    tenant_id, user_id, source, usage=effective_usage, ai_authorized=effective_ai,
                    expires_at=effective_expires, confirm_ai=confirm_ai)
            except Exception as error:  # 单个失败不阻断其他资料
                results["failed"].append({"source_id": source["id"], "error": str(error)})
            else:
                results[outcome["kind"]].append(outcome)
        timestamp = _now()
        self.conn.execute(
            "UPDATE kb2_batches SET usage=?, ai_authorized=?, expires_at=?, updated_at=? WHERE tenant_id=? AND id=?",
            (effective_usage, 1 if effective_ai else 0, effective_expires, timestamp, tenant_id, batch_id),
        )
        if results["published"] or results["archived"]:
            self.conn.execute(
                "UPDATE kb2_batches SET status='published', published_at=COALESCE(published_at, ?), updated_at=? "
                "WHERE tenant_id=? AND id=?",
                (timestamp, timestamp, tenant_id, batch_id),
            )
        self._audit(tenant_id, "kb2_batch", batch_id, "batch_published" if (results["published"] or results["archived"]) else "batch_publish_attempted",
                    batch["created_by"],
                    f"published={len(results['published'])};archived={len(results['archived'])};"
                    f"failed={len(results['failed'])};skipped={len(results['skipped'])}")
        self.conn.commit()
        return results


    # ══════════════════════════════════════════════════════════════════════
    # 任务4：灵活字段 / 知识模板（版本不可变）/ 文件更新（替换）
    # 设计依据：v0.2 §7–§8（字段五种简单类型；模板改版不改旧知识；采用新版只改草稿；
    # 替换产生新原件与新草稿、位置/文本差异对照、发布前旧版本继续有效）。
    # ══════════════════════════════════════════════════════════════════════

    # ── 模板定义（版本不可变：只追加新版本，绝不改写旧版本）────────────────

    @staticmethod
    def _kb2_normalize_template_fields(fields) -> list[dict]:
        """模板字段定义校验：类型 / 名称 / 身份（key）/ 顺序 / 必填 / 提示 / 单位 / 选项。"""
        if not isinstance(fields, list) or not fields:
            raise ValueError("模板至少需要一个字段")
        if len(fields) > 30:
            raise ValueError("模板字段不能超过 30 个")
        normalized = []
        seen_keys, seen_labels = set(), set()
        for index, field in enumerate(fields, start=1):
            if not isinstance(field, dict):
                raise ValueError(f"第 {index} 个字段格式无效")
            label = str(field.get("label") or field.get("name") or "").strip()
            if not label:
                raise ValueError(f"第 {index} 个字段缺少名称")
            if len(label) > 40:
                raise ValueError(f"字段名称过长（最多 40 字）：{label[:12]}…")
            if label.lower() in seen_labels:
                raise ValueError(f"字段名称不能重复：{label}")
            seen_labels.add(label.lower())
            field_type = str(field.get("type") or "text").strip()
            if field_type not in KB2_FIELD_TYPES:
                raise ValueError(f"字段「{label}」的类型无效（支持 { ' / '.join(KB2_FIELD_TYPES) }）")
            key = str(field.get("key") or "").strip()
            if not key or not re.fullmatch(r"[a-zA-Z0-9_-]{1,40}", key):
                key = "f_" + secrets.token_hex(6)
            if key in seen_keys:
                raise ValueError(f"字段身份（key）重复：{key}")
            seen_keys.add(key)
            entry = {
                "key": key, "label": label, "type": field_type,
                "required": bool(field.get("required")),
                "hint": str(field.get("hint") or "").strip()[:120],
                "unit": str(field.get("unit") or "").strip()[:20],
                "options": [],
            }
            if field_type == "select":
                options = [str(option or "").strip() for option in (field.get("options") or [])]
                options = [option for option in options if option]
                unique = list(dict.fromkeys(options))
                if len(unique) < 2:
                    raise ValueError(f"选项字段「{label}」至少需要两个不重复的选项（空选项不保存）")
                entry["options"] = unique
            normalized.append(entry)
        return normalized

    def _kb2_template_row(self, tenant_id: str, template_id: str):
        return self.conn.execute(
            "SELECT * FROM kb2_templates WHERE tenant_id=? AND id=?", (tenant_id, template_id)
        ).fetchone()

    def _kb2_template_version_row(self, tenant_id: str, template_version_id: str):
        return self.conn.execute(
            "SELECT * FROM kb2_template_versions WHERE tenant_id=? AND id=?",
            (tenant_id, template_version_id),
        ).fetchone()

    def kb2_save_template(self, tenant_id: str, user_id: str, template_id: str | None = None,
                          fields=None, *, name: str | None = None, form: str = "", note: str = "") -> str:
        """保存模板（新模板或新版本）：模板版本不可变——旧版本字段定义永不改写。

        返回新的模板版本 ID；模板 ID 不变、版本号递增。改字段类型时按新字段身份处理
        （采用新版时旧值不自动转换，由 kb2_adopt_template 处理）。
        """
        editor = self._require_knowledge_editor(tenant_id, user_id)
        normalized = self._kb2_normalize_template_fields(fields)
        timestamp = _now()
        if template_id:
            template = self._kb2_template_row(tenant_id, template_id)
            if not template:
                raise ValueError("模板不存在或不属于本企业")
            version = self.conn.execute(
                "SELECT COALESCE(MAX(version), 0) + 1 FROM kb2_template_versions WHERE tenant_id=? AND template_id=?",
                (tenant_id, template_id),
            ).fetchone()[0]
            if name is not None:
                candidate = str(name or "").strip()
                if not candidate:
                    raise ValueError("模板名称不能为空")
                clash = self.conn.execute(
                    "SELECT id FROM kb2_templates WHERE tenant_id=? AND lower(name)=lower(?) AND id<>?",
                    (tenant_id, candidate, template_id),
                ).fetchone()
                if clash:
                    raise ValueError("已有同名模板")
                self.conn.execute(
                    "UPDATE kb2_templates SET name=?, updated_at=? WHERE tenant_id=? AND id=?",
                    (candidate, timestamp, tenant_id, template_id),
                )
            if form is not None and str(form).strip():
                self.conn.execute(
                    "UPDATE kb2_templates SET form=?, updated_at=? WHERE tenant_id=? AND id=?",
                    (str(form).strip(), timestamp, tenant_id, template_id),
                )
        else:
            candidate = str(name or "").strip()
            if not candidate:
                raise ValueError("模板名称不能为空")
            clash = self.conn.execute(
                "SELECT id FROM kb2_templates WHERE tenant_id=? AND lower(name)=lower(?)",
                (tenant_id, candidate),
            ).fetchone()
            if clash:
                raise ValueError("已有同名模板")
            template_id = secrets.token_hex(12)
            version = 1
            self.conn.execute(
                "INSERT INTO kb2_templates(id, tenant_id, name, form, created_by, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (template_id, tenant_id, candidate, str(form or "").strip(), editor["email"], timestamp, timestamp),
            )
        version_id = secrets.token_hex(12)
        self.conn.execute(
            "INSERT INTO kb2_template_versions(id, tenant_id, template_id, version, fields_json, note, created_by, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (version_id, tenant_id, template_id, version,
             json.dumps(normalized, ensure_ascii=False), str(note or "").strip()[:200],
             editor["email"], timestamp),
        )
        self.conn.execute(
            "UPDATE kb2_templates SET updated_at=? WHERE tenant_id=? AND id=?", (timestamp, tenant_id, template_id)
        )
        self._audit(tenant_id, "kb2_template", template_id, "template_version_saved", editor["email"],
                    f"version={version};fields={len(normalized)}")
        self.conn.commit()
        return version_id

    def kb2_list_templates(self, tenant_id: str) -> list[dict]:
        out = []
        for template in self.conn.execute(
            "SELECT * FROM kb2_templates WHERE tenant_id=? ORDER BY updated_at DESC, id", (tenant_id,)
        ).fetchall():
            latest = self.conn.execute(
                "SELECT * FROM kb2_template_versions WHERE tenant_id=? AND template_id=? ORDER BY version DESC LIMIT 1",
                (tenant_id, template["id"]),
            ).fetchone()
            count = self.conn.execute(
                "SELECT COUNT(*) FROM kb2_template_versions WHERE tenant_id=? AND template_id=?",
                (tenant_id, template["id"]),
            ).fetchone()[0]
            fields = _json_load(latest["fields_json"], []) if latest else []
            out.append({
                "id": template["id"], "name": template["name"], "form": template["form"],
                "latest_version_id": latest["id"] if latest else None,
                "latest_version": latest["version"] if latest else None,
                "field_count": len(fields), "version_count": count,
                "created_at": template["created_at"], "updated_at": template["updated_at"],
            })
        return out

    def kb2_get_template(self, tenant_id: str, template_id: str) -> dict | None:
        template = self._kb2_template_row(tenant_id, template_id)
        if not template:
            return None
        versions = []
        for row in self.conn.execute(
            "SELECT * FROM kb2_template_versions WHERE tenant_id=? AND template_id=? ORDER BY version DESC",
            (tenant_id, template_id),
        ).fetchall():
            versions.append({
                "id": row["id"], "version": row["version"],
                "fields": _json_load(row["fields_json"], []),
                "note": row["note"], "created_by": row["created_by"], "created_at": row["created_at"],
            })
        return {"id": template["id"], "name": template["name"], "form": template["form"],
                "created_at": template["created_at"], "updated_at": template["updated_at"],
                "versions": versions}

    # ── 采用模板版本（对照 + 明确确认；只作用于草稿）──────────────────────

    def _kb2_item_binding(self, tenant_id: str, item_id: str):
        return self.conn.execute(
            "SELECT * FROM kb2_item_template WHERE tenant_id=? AND item_id=?", (tenant_id, item_id)
        ).fetchone()

    def _kb2_item_field_rows(self, tenant_id: str, item_id: str, *, include_removed: bool = False):
        query = "SELECT * FROM kb2_item_fields WHERE tenant_id=? AND item_id=?"
        if not include_removed:
            query += " AND removed=0"
        query += " ORDER BY ordinal, created_at, id"
        return self.conn.execute(query, (tenant_id, item_id)).fetchall()

    @staticmethod
    def _kb2_field_view(row) -> dict:
        return {
            "id": row["id"], "field_key": row["field_key"], "label": row["label"], "type": row["type"],
            "type_label": KB2_FIELD_TYPE_LABELS.get(row["type"], row["type"]),
            "required": bool(row["required"]), "hint": row["hint"], "unit": row["unit"],
            "options": _json_load(row["options_json"], []),
            "value": row["value"], "value_present": row["value"] is not None,
            "source": _json_load(row["source_json"], None),
            "checked": bool(row["checked"]), "allow_customer": bool(row["allow_customer"]),
            "removed": bool(row["removed"]), "template_version_id": row["template_version_id"],
            "ordinal": row["ordinal"], "updated_at": row["updated_at"],
        }

    def kb2_adopt_template(self, tenant_id: str, user_id: str, item_id: str,
                           template_version_id: str, *, confirmed: bool = False) -> dict:
        """采用模板版本：先给出对照（新增/移除/改类型/改名/必填缺项），明确确认后只改草稿。

        - 不确认（confirmed=False）只返回对照，不改任何数据；
        - 已采用相同版本 → 幂等；确认应用时只更新草稿关联与字段行：旧的值/来源/核对状态保留，
          被移除或改类型的旧字段行保留为历史（removed），不删除；发布版本与线上内容不动。
        """
        editor = self._require_knowledge_editor(tenant_id, user_id)
        item = self.get_knowledge_item(tenant_id, item_id)
        if not item:
            raise ValueError("资料不存在或不属于本企业")
        template_version = self._kb2_template_version_row(tenant_id, template_version_id)
        if not template_version:
            raise ValueError("模板版本不存在或不属于本企业")
        template = self._kb2_template_row(tenant_id, template_version["template_id"])
        new_fields = _json_load(template_version["fields_json"], [])
        if not isinstance(new_fields, list) or not new_fields:
            raise ValueError("模板版本没有字段定义")
        binding = self._kb2_item_binding(tenant_id, item_id)
        current_rows = {row["field_key"]: row for row in self._kb2_item_field_rows(tenant_id, item_id)}
        added, removed, type_changed, renamed = [], [], [], []
        kept = 0
        for field in new_fields:
            key = str(field.get("key") or "")
            row = current_rows.get(key)
            if row is None:
                added.append({"key": key, "label": field["label"], "type": field["type"]})
                continue
            if str(row["type"]) != str(field["type"]):
                type_changed.append({"key": key, "label": field["label"],
                                     "old_type": row["type"], "new_type": field["type"]})
                continue
            kept += 1
            if str(row["label"]) != str(field["label"]):
                renamed.append({"key": key, "old_label": row["label"], "new_label": field["label"]})
        new_keys = {str(field.get("key") or "") for field in new_fields}
        for key, row in current_rows.items():
            if key not in new_keys:
                removed.append({"key": key, "label": row["label"], "type": row["type"]})
        missing_required = []
        for field in new_fields:
            if not field.get("required"):
                continue
            key = str(field.get("key") or "")
            row = current_rows.get(key)
            if row is None or row["value"] is None:
                missing_required.append({"key": key, "label": field["label"]})
        diff = {
            "template_id": template["id"], "template_name": template["name"],
            "template_version": template_version["version"], "template_version_id": template_version["id"],
            "current_template_version_id": binding["template_version_id"] if binding else None,
            "added": added, "removed": removed, "type_changed": type_changed, "renamed": renamed,
            "kept": kept, "missing_required": missing_required,
        }
        if binding and binding["template_version_id"] == template_version_id:
            return {"applied": False, "already": True, "diff": diff}
        if not confirmed:
            return {"applied": False, "already": False, "diff": diff}
        # 只作用于草稿：已有草稿用现有草稿；已发布且无草稿时从线上版本新建草稿（线上不变）。
        draft = self.get_knowledge_draft_version(tenant_id, item_id)
        if draft is None:
            draft = self._kb2_ensure_item_draft(tenant_id, item_id)
        timestamp = _now()
        all_rows = self._kb2_item_field_rows(tenant_id, item_id, include_removed=True)
        active_by_key = {}
        for row in all_rows:
            if not row["removed"]:
                active_by_key.setdefault(row["field_key"], row)
        ordinals = {str(field.get("key") or ""): index for index, field in enumerate(new_fields)}
        for field in new_fields:
            key = str(field.get("key") or "")
            row = active_by_key.get(key)
            same_type = row is not None and str(row["type"]) == str(field["type"])
            if same_type:
                self.conn.execute(
                    "UPDATE kb2_item_fields SET label=?, required=?, hint=?, unit=?, options_json=?, "
                    "ordinal=?, template_version_id=?, updated_at=? WHERE tenant_id=? AND id=?",
                    (field["label"], 1 if field.get("required") else 0, field.get("hint") or "",
                     field.get("unit") or "", json.dumps(field.get("options") or [], ensure_ascii=False),
                     ordinals[key], template_version_id, timestamp, tenant_id, row["id"]),
                )
                continue
            if row is not None:
                # 改类型 / 同名新身份：旧行保留为历史，值不自动转换。
                self.conn.execute(
                    "UPDATE kb2_item_fields SET removed=1, updated_at=? WHERE tenant_id=? AND id=?",
                    (timestamp, tenant_id, row["id"]),
                )
            self.conn.execute(
                "INSERT INTO kb2_item_fields(id, tenant_id, item_id, field_key, ordinal, label, type, required, "
                "hint, unit, options_json, value, source_json, checked, allow_customer, removed, template_version_id, "
                "created_by, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, 0, 0, 0, ?, ?, ?, ?)",
                (secrets.token_hex(12), tenant_id, item_id, key, ordinals[key], field["label"], field["type"],
                 1 if field.get("required") else 0, field.get("hint") or "", field.get("unit") or "",
                 json.dumps(field.get("options") or [], ensure_ascii=False), template_version_id,
                 editor["email"], timestamp, timestamp),
            )
        for row in all_rows:
            if row["field_key"] not in new_keys and not row["removed"]:
                # 移除字段：新版表单不再显示；旧值与历史保留（不静默删历史数据）。
                self.conn.execute(
                    "UPDATE kb2_item_fields SET removed=1, updated_at=? WHERE tenant_id=? AND id=?",
                    (timestamp, tenant_id, row["id"]),
                )
        if binding:
            self.conn.execute(
                "UPDATE kb2_item_template SET template_version_id=?, adopted_by=?, adopted_at=? WHERE tenant_id=? AND item_id=?",
                (template_version_id, editor["email"], timestamp, tenant_id, item_id),
            )
        else:
            self.conn.execute(
                "INSERT INTO kb2_item_template(item_id, tenant_id, template_version_id, adopted_by, adopted_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (item_id, tenant_id, template_version_id, editor["email"], timestamp),
            )
        self._audit(tenant_id, "knowledge", item_id, "template_adopted", editor["email"],
                    f"template={template['id'][:12]};version={template_version['version']};"
                    f"added={len(added)};removed={len(removed)};type_changed={len(type_changed)}")
        self.conn.commit()
        return {"applied": True, "already": False, "diff": diff,
                "draft_version_id": draft["id"] if draft else None}

    # ── 字段值（五种类型；0/否是有效值，未填与0不同；每个值都要有来源）────────

    @staticmethod
    def _kb2_normalize_field_value(field_type: str, value):
        """类型校验与规范化；未填 → None（与 0 / 否 明确区分）。"""
        if value is None:
            return None
        if isinstance(value, bool):
            if field_type != "bool":
                raise ValueError("该字段不是「是/否」类型")
            return "true" if value else "false"
        text = str(value).strip()
        if text == "":
            return None
        if field_type in ("text", "textarea"):
            return str(value)
        if field_type == "number":
            try:
                float(text)
            except ValueError:
                raise ValueError("数字字段必须是有效数字（0 是有效值）") from None
            return text
        if field_type == "select":
            return text
        if field_type == "date":
            try:
                datetime.strptime(text, "%Y-%m-%d")
            except ValueError:
                raise ValueError("日期字段格式应为 YYYY-MM-DD") from None
            return text
        if field_type == "bool":
            lowered = text.lower()
            if lowered in ("1", "true", "yes", "是"):
                return "true"
            if lowered in ("0", "false", "no", "否"):
                return "false"
            raise ValueError("「是/否」字段只接受 是/否（0/否也是有效值）")
        raise ValueError("字段类型无效")

    def kb2_list_item_fields(self, tenant_id: str, item_id: str) -> dict:
        """资料当前字段视图（草稿工作集）：绑定模板、字段、可用来源段落。"""
        item = self.get_knowledge_item(tenant_id, item_id)
        if not item:
            raise ValueError("资料不存在或不属于本企业")
        binding = self._kb2_item_binding(tenant_id, item_id)
        template_view = None
        if binding:
            template_version = self._kb2_template_version_row(tenant_id, binding["template_version_id"])
            if template_version:
                template = self._kb2_template_row(tenant_id, template_version["template_id"])
                latest = self.conn.execute(
                    "SELECT * FROM kb2_template_versions WHERE tenant_id=? AND template_id=? ORDER BY version DESC LIMIT 1",
                    (tenant_id, template_version["template_id"]),
                ).fetchone()
                template_view = {
                    "id": template["id"] if template else "", "name": template["name"] if template else "",
                    "template_version_id": template_version["id"],
                    "template_version": template_version["version"],
                    "latest_version_id": latest["id"] if latest else None,
                    "latest_version": latest["version"] if latest else None,
                }
        fields = [self._kb2_field_view(row) for row in self._kb2_item_field_rows(tenant_id, item_id)]
        source_options = []
        for source in self.conn.execute(
            "SELECT * FROM kb2_sources WHERE tenant_id=? AND item_id=? ORDER BY created_at, id",
            (tenant_id, item_id),
        ).fetchall():
            for row in self.conn.execute(
                "SELECT * FROM kb2_paragraphs WHERE tenant_id=? AND source_id=? ORDER BY ordinal",
                (tenant_id, source["id"]),
            ).fetchall():
                location = _json_load(row["location_json"], {})
                source_options.append({
                    "paragraph_id": row["id"], "source_id": source["id"],
                    "source_title": source["title"] or source["filename"],
                    "location_label": self.kb2_location_label(location),
                    "text": str(row["text"] or "")[:120],
                })
        return {"item_id": item_id, "item_title": (self._kb2_material_snapshot(tenant_id, "knowledge", item_id) or {}).get("title", ""),
                "binding": binding["template_version_id"] if binding else None,
                "template": template_view, "fields": fields, "source_options": source_options}

    def kb2_set_item_field(self, tenant_id: str, user_id: str, item_id: str, field_key: str, *,
                           value=_UNSET, unit=_UNSET, checked=_UNSET, allow_customer=_UNSET,
                           source=_UNSET) -> dict:
        """填写/修改一个字段值：按类型校验（0 与「否」是有效值，未填与 0 不同）；
        每个值必须写出来源（来源段落或人工填写）；改值/改来源会撤销该字段的核对标记。"""
        editor = self._require_knowledge_editor(tenant_id, user_id)
        item = self.get_knowledge_item(tenant_id, item_id)
        if not item:
            raise ValueError("资料不存在或不属于本企业")
        binding = self._kb2_item_binding(tenant_id, item_id)
        if not binding:
            raise ValueError("资料还没有采用模板：请先在「采用模板」中选择版本")
        if item["status"] == "published" and not item["draft_version_id"]:
            self._kb2_ensure_item_draft(tenant_id, item_id)  # 字段修改进入草稿，线上不变
        row = next((candidate for candidate in self._kb2_item_field_rows(tenant_id, item_id)
                    if candidate["field_key"] == str(field_key)), None)
        if row is None:
            raise ValueError("字段不存在或不在当前模板中")
        field_type = str(row["type"])
        changes = {}
        value_changed = unit_changed = source_changed = False
        if value is not _UNSET:
            normalized = self._kb2_normalize_field_value(field_type, value)
            if normalized is not None and field_type == "select" and normalized not in _json_load(row["options_json"], []):
                raise ValueError("选项字段的值必须是模板定义的选项（不让 AI 猜选项）")
            if normalized != row["value"]:
                changes["value"] = normalized
                # 首次填写（原值未填）不算"改动"；已有值时改值/清空都算改动（撤销核对）。
                value_changed = row["value"] is not None
            elif normalized is None and row["value"] is not None:
                changes["value"] = None
                value_changed = True
        if unit is not _UNSET:
            new_unit = str(unit or "").strip()[:20]
            changes["unit"] = new_unit
            # 单位是数值字段声明的一部分：已有值的前提下改单位同样使核对失效。
            if new_unit != str(row["unit"] or "") and row["value"] is not None:
                unit_changed = True
        if source is not _UNSET:
            normalized_source = None
            if source is not None:
                if not isinstance(source, dict):
                    raise ValueError("字段来源格式无效")
                kind = str(source.get("kind") or "")
                if kind == "manual":
                    normalized_source = {"kind": "manual", "label": "人工填写"}
                elif kind == "source":
                    source_id = str(source.get("source_id") or "").strip()
                    paragraph_id = str(source.get("paragraph_id") or "").strip()
                    paragraph = self._kb2_require_paragraph(tenant_id, paragraph_id) if paragraph_id else None
                    if not paragraph or paragraph["item_id"] != item_id or source_id != (paragraph["source_id"] or ""):
                        raise ValueError("字段来源段落不属于该资料：请选择本资料的真实段落")
                    kb2_source = self._kb2_source_row(tenant_id, paragraph["source_id"])
                    location = _json_load(paragraph["location_json"], {})
                    normalized_source = {
                        "kind": "source", "source_id": paragraph["source_id"], "paragraph_id": paragraph["id"],
                        "location": location,
                        "label": f"{kb2_source['title'] or kb2_source['filename']} · {self.kb2_location_label(location) or '资料级'}",
                    }
                else:
                    raise ValueError("字段来源类型无效（人工填写或来源段落）")
            if json.dumps(normalized_source, ensure_ascii=False, sort_keys=True) != json.dumps(
                    _json_load(row["source_json"], None), ensure_ascii=False, sort_keys=True):
                changes["source_json"] = None if normalized_source is None else json.dumps(normalized_source, ensure_ascii=False)
                # 首次写出来源不算"改动"；已有来源被改动/清空才算（撤销核对）。
                source_changed = row["source_json"] is not None
        claim_changed = value_changed or unit_changed or source_changed
        if claim_changed:
            # 改值/改单位/改来源 → 核对强制失效：同一次提交携带的旧 checked=true 不豁免（须下次显式再核对）。
            changes["checked"] = 0
        elif checked is not _UNSET:
            changes["checked"] = 1 if checked else 0
        if allow_customer is not _UNSET:
            changes["allow_customer"] = 1 if allow_customer else 0
        # 「每个字段必须写出来源」：设置了值就不能没有来源。
        effective_value = changes.get("value", row["value"])
        effective_source = changes.get("source_json", row["source_json"])
        if effective_value is not None and not effective_source:
            raise ValueError("字段必须写出来源：选择来源段落或标记「人工填写」后再保存")
        if not changes:
            return self._kb2_field_view(row)
        changes["updated_at"] = _now()
        assignments = ", ".join(f"{key}=?" for key in changes)
        self.conn.execute(
            f"UPDATE kb2_item_fields SET {assignments} WHERE tenant_id=? AND id=?",
            (*changes.values(), tenant_id, row["id"]),
        )
        self.conn.commit()
        fresh = self.conn.execute("SELECT * FROM kb2_item_fields WHERE tenant_id=? AND id=?",
                                  (tenant_id, row["id"])).fetchone()
        return self._kb2_field_view(fresh)

    def _kb2_init_fields_from_restore(self, tenant_id: str, item_id: str, draft_version_id: str) -> None:
        """B2-B11：恢复历史版本时用恢复点字段快照初始化工作集（字段行）。

        - 只覆盖恢复点快照里出现的字段（值 / 单位 / 核对 / 客户引用 / 来源），其余字段行不动；
        - 初始化之后发生的明确编辑（改值 / 改单位 / 改来源 / 再核对）进入新草稿的发布快照；
        - 恢复前的行漂移在此被恢复点值重置，不会混入恢复发布；旧历史版本不可变。
        """
        draft = self.get_knowledge_version(tenant_id, draft_version_id)
        if not draft:
            return
        fields = _json_load(draft["content_json"], {}).get("kb2_fields")
        if not isinstance(fields, list) or not fields:
            return
        rows = {row["field_key"]: row for row in self._kb2_item_field_rows(tenant_id, item_id)}
        timestamp = _now()
        for field in fields:
            if not isinstance(field, dict):
                continue
            row = rows.get(str(field.get("field_key") or ""))
            if row is None:
                continue
            source = field.get("source")
            self.conn.execute(
                "UPDATE kb2_item_fields SET value=?, unit=?, checked=?, allow_customer=?, source_json=?, updated_at=? "
                "WHERE tenant_id=? AND id=?",
                (field.get("value"), str(field.get("unit") or ""), 1 if field.get("checked") else 0,
                 1 if field.get("allow_customer") else 0,
                 json.dumps(source, ensure_ascii=False) if isinstance(source, dict) else None,
                 timestamp, tenant_id, row["id"]),
            )

    def _kb2_prepare_publish_fields_content(self, tenant_id: str, item_id: str, snapshot: dict):
        """发布前的模板字段处理（挂入第一批 publish_knowledge_draft）。

        - 新版必填字段只约束「采用该版本模板后」的发布：必填缺项在发布前拦截；
        - 普通草稿：发布时把当前字段行冻结为快照（值/单位/核对/客户引用/来源）；
        - 恢复的草稿（restored_from_version）：恢复时已用恢复点快照初始化工作集（字段行），
          发布按当前工作集冻结——恢复后的明确编辑进入新草稿快照（B2-B11：独立验收要求
          「恢复应初始化准确工作集，之后明确编辑进入新草稿快照，旧历史不可变」）；
          恢复草稿跳过「新增必填」校验：不把历史已发布内容因新增必填判成无效（v0.2 §7.4）；
        - 无模板绑定返回 None；只读校验 + 返回新内容，不在此写入（发布路径统一在切换版本时一起提交，不留隐形事务）。
        """
        binding = self._kb2_item_binding(tenant_id, item_id)
        if not binding:
            return None
        template_version = self._kb2_template_version_row(tenant_id, binding["template_version_id"])
        if not template_version:
            raise ValueError("资料采用的模板版本已不存在：请重新采用模板后再发布")
        draft = self.get_knowledge_draft_version(tenant_id, item_id)
        restored = draft is not None and draft["restored_from_version"] is not None
        definitions = {str(field.get("key") or ""): field
                       for field in _json_load(template_version["fields_json"], []) if isinstance(field, dict)}
        rows = self._kb2_item_field_rows(tenant_id, item_id)
        if not restored:
            missing = []
            for row in rows:
                definition = definitions.get(row["field_key"])
                if definition and definition.get("required") and row["value"] is None:
                    missing.append(row["label"])
            if missing:
                raise ValueError("必填字段未填写（模板 v%d）：%s" % (
                    template_version["version"], "、".join(missing)))
        fields_snapshot = []
        for row in rows:
            view = self._kb2_field_view(row)
            fields_snapshot.append({
                "field_key": view["field_key"], "label": view["label"], "type": view["type"],
                "value": view["value"], "unit": view["unit"], "checked": view["checked"],
                "allow_customer": view["allow_customer"], "source": view["source"],
            })
        updated = dict(snapshot if isinstance(snapshot, dict) else {})
        updated["kb2_fields"] = fields_snapshot
        return updated

    # ── 文件更新（替换）：新原件 + 新草稿 + 位置/文本差异；线上版本在核对发布前不变 ──

    @staticmethod
    def _kb2_text_lines(text: str) -> list[str]:
        return [line.strip() for line in str(text or "").splitlines() if line.strip()]

    def kb2_source_replacement_diff(self, tenant_id: str, source_id: str) -> dict | None:
        """替换对照：被替换的线上版本正文 vs 新原件的识别段落（文本/位置；不判定谁更真实）。"""
        source = self._kb2_source_row(tenant_id, source_id)
        if not source or not source["replace_of_version_id"]:
            return None
        baseline = self.get_knowledge_version(tenant_id, source["replace_of_version_id"])
        if not baseline:
            return None
        old_lines = self._kb2_text_lines(_json_load(baseline["content_json"], {}).get("body") or "")
        paragraphs = []
        for row in self.conn.execute(
            "SELECT * FROM kb2_paragraphs WHERE tenant_id=? AND source_id=? ORDER BY ordinal",
            (tenant_id, source_id),
        ).fetchall():
            location = _json_load(row["location_json"], {})
            paragraphs.append({"text": str(row["text"] or "").strip(),
                               "location_label": self.kb2_location_label(location)})
        new_lines = [paragraph["text"] for paragraph in paragraphs]
        import difflib
        matcher = difflib.SequenceMatcher(a=old_lines, b=new_lines, autojunk=False)
        added, removed, unchanged = [], [], 0
        changed = []
        for tag, i1, i2, j1, j2 in matcher.get_opcodes():
            if tag == "equal":
                unchanged += i2 - i1
            elif tag == "insert":
                added.extend(paragraphs[j1:j2])
            elif tag == "delete":
                removed.extend(old_lines[i1:i2])
            else:  # replace：按位置配对成「变更」，多出的部分归入新增/移除
                paired = min(i2 - i1, j2 - j1)
                for offset in range(paired):
                    changed.append({"old": old_lines[i1 + offset], "new": new_lines[j1 + offset],
                                    "location_label": paragraphs[j1 + offset]["location_label"]})
                removed.extend(old_lines[i1 + paired:i2])
                added.extend(paragraphs[j1 + paired:j2])
        return {
            "baseline_version_id": baseline["id"], "baseline_version": baseline["version"],
            "added": added[:50], "removed": removed[:50], "changed": changed[:50],
            "counts": {"added": len(added), "removed": len(removed), "changed": len(changed),
                       "unchanged": unchanged},
        }

    def kb2_replace_file(self, tenant_id: str, user_id: str, item_id: str,
                         filename: str, data) -> dict:
        """文件更新：登记新原件（独立来源身份）→ 新草稿（沿用资料 ID）→ 差异对照。

        - 同一知识 ID 不变；旧线上版本在核对发布前继续有效（本函数不改线上内容/状态）；
        - 已有未发布草稿时拒绝（先发布或继续现有草稿），人工修改不被静默覆盖；
        - 原作者文件私有保存（token 命名 + sha256），处理后可查看逐段位置与文本差异；
        - 保留模板绑定与字段行（沿用已发布版本的模板作为编辑起点，不因替换丢失）。
        """
        editor = self._require_knowledge_editor(tenant_id, user_id)
        item = self.get_knowledge_item(tenant_id, item_id)
        if not item:
            raise ValueError("资料不存在或不属于本企业")
        if item["draft_version_id"]:
            raise ValueError("该资料已有未发布草稿：请先发布或继续整理现有草稿，再更新文件")
        published = self.get_published_knowledge_version(tenant_id, item_id)
        if not published:
            raise ValueError("该资料尚未发布：请直接编辑草稿，无需更新文件")
        name = str(filename or "").strip()
        if not name:
            raise ValueError("缺少文件名")
        if not isinstance(data, (bytes, bytearray)) or not bytes(data):
            raise ValueError(f"{name}：文件内容为空或无效")
        payload = bytes(data)
        extension = Path(name).suffix.lower()
        if extension in knowledge_ingestion.TEXT_EXTENSIONS:
            if len(payload) > KNOWLEDGE_TEXT_FILE_LIMIT:
                raise ValueError(f"{name}：文本文件不能超过 1MB；大文件请先拆分或改用手工摘录")
        elif extension in KB2_SUPPORTED_ENTRY_EXTENSIONS:
            if len(payload) > KNOWLEDGE_DOCUMENT_FILE_LIMIT:
                raise ValueError(f"{name}：文件不能超过 10MB")
        else:
            raise ValueError(f"{name}：暂不支持该格式（支持 TXT/Markdown/CSV/PDF/DOCX；图片、视频与旧版 DOC 可仅登记原件）")
        timestamp = _now()
        batch_id = secrets.token_hex(12)
        source_id = secrets.token_hex(12)
        self.conn.execute(
            "INSERT INTO kb2_batches(id, tenant_id, status, usage, ai_authorized, expires_at, created_by, created_at, updated_at, purpose) "
            "VALUES (?, ?, 'draft', 'internal', 0, NULL, ?, ?, ?, 'replace_file')",
            (batch_id, tenant_id, editor["email"], timestamp, timestamp),
        )
        target = self._kb2_storage_dir() / f"kb2-{secrets.token_hex(16)}{extension}"
        target.write_bytes(payload)
        try:
            self.conn.execute(
                "INSERT INTO kb2_sources(id, tenant_id, batch_id, kind, filename, title, format, size, sha256, "
                "storage_path, source_location, payload_text, status, item_id, replace_of_version_id, "
                "created_by, created_at, updated_at) "
                "VALUES (?, ?, ?, 'file', ?, ?, ?, ?, ?, ?, ?, '', 'pending', ?, ?, ?, ?, ?)",
                (source_id, tenant_id, batch_id, name, Path(name).stem or name,
                 knowledge_ingestion.format_for_filename(name), len(payload),
                 hashlib.sha256(payload).hexdigest(), str(target),
                 f"更新自文件：{Path(name).name}", item_id, published["id"],
                 editor["email"], timestamp, timestamp),
            )
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            target.unlink(missing_ok=True)
            raise
        # 新草稿（从线上版本复制；线上不变），随后同步处理新原件（独占认领、幂等）。
        draft = self._kb2_ensure_item_draft(tenant_id, item_id)
        self._kb2_process_source(tenant_id, source_id)
        fresh = self._kb2_source_row(tenant_id, source_id)
        if fresh["status"] == "published":  # 理论上不会发生（新来源从 pending 开始）
            raise ValueError("来源状态异常，请重试")
        ws_id = fresh["knowledge_source_id"]
        if ws_id:
            # 新草稿以新原件为出处（旧版本仍指向旧原件；不改旧版本）。
            self.conn.execute(
                "UPDATE knowledge_versions SET source_id=? WHERE tenant_id=? AND id=? AND status='draft'",
                (ws_id, tenant_id, draft["id"]),
            )
            self.conn.commit()
        diff = self.kb2_source_replacement_diff(tenant_id, source_id)
        self._audit(tenant_id, "kb2_source", source_id, "file_replaced", editor["email"],
                    f"item={item_id[:8]};baseline_v={published['version']};status={fresh['status']}")
        self.conn.commit()
        return {"item_id": item_id, "batch_id": batch_id, "source_id": source_id,
                "draft_id": draft["id"], "status": fresh["status"],
                "baseline_version": published["version"], "diff": diff}

    # ══════════════════════════════════════════════════════════════════════
    # 任务5：对外 AI 证据闸门（主题逐段过滤 / 字段闸门；模型前后双重校验）
    # ══════════════════════════════════════════════════════════════════════

    def _kb2_topic_reference_version(self, tenant_id: str, topic_id: str):
        """主题对外引用的版本：已发布主题用最新发布版本；草稿主题没有对外引用。"""
        topic = self._kb2_topic_row(tenant_id, topic_id)
        if not topic or topic["status"] != "published" or not topic["published_version_id"]:
            return None
        return self.conn.execute(
            "SELECT * FROM kb2_topic_versions WHERE tenant_id=? AND id=? AND status='published'",
            (tenant_id, topic["published_version_id"]),
        ).fetchone()

    def _kb2_paragraph_evidence_gate(self, tenant_id: str, paragraph: dict, *, now=None):
        """单段是否可用于对外回答（任务5）：来源授权的主题不能绕过来源自身限制。

        通过条件（全部满足）：已核对、有文字；人工撰写/人工改写的段落以成员核对与主题授权为准；
        资料摘录还需：来源资料当前已发布、对外、已授权、未过期、不是仅登记原件、来源仍可用，
        且所钉住版本是来源资料当前发布版本（换版/替换后不得静默换版——按不合格过滤）。
        返回 (bool, reason)。
        """
        text = str(paragraph.get("text") or "").strip()
        if not text:
            return False, "空段落"
        if not paragraph.get("checked"):
            return False, "该段尚未核对"
        source = paragraph.get("source")
        if not isinstance(source, dict) or source.get("kind") == "manual":
            # 人工撰写/改写：成员在核对时确认；主题的对外+AI 授权覆盖（来源为成员自己）。
            return True, ""
        if source.get("kind") == "product":
            return False, "产品来源段落暂不用于对外回答（产品字段有独立闸门）"
        if source.get("kind") != "knowledge":
            return False, "段落来源类型无效"
        item_id = str(source.get("item_id") or "")
        version_id = str(source.get("version_id") or "")
        source_id = str(source.get("source_id") or "")
        item = self.get_knowledge_item(tenant_id, item_id)
        if not item:
            return False, "来源资料已不存在"
        if item["status"] != "published":
            return False, "来源资料未发布或已停用"
        processing = self.knowledge_processing_state(tenant_id, version_id)
        if processing is None or processing["status"] != "completed":
            return False, "来源版本未准备完成"
        version = self.get_knowledge_version(tenant_id, version_id)
        if not version:
            return False, "来源版本已不存在"
        if item["published_version_id"] != version_id:
            # 换版（含草稿版本尚未发布 / 被替换 / 被新版取代）：不得静默换版，须成员重新核对。
            return False, "来源版本不是当前发布版本：请重新核对来源（不静默换版）"
        if version["status"] != "published":
            return False, "来源版本已不是发布版本"
        if str(version["source_id"] or "") != source_id:
            return False, "来源原件与该版本不匹配"
        if version["usage"] != "external":
            return False, "来源资料仅内部使用"
        if not version["ai_authorized"]:
            return False, "来源资料未授权 AI 客服使用"
        snapshot_item = self._kb2_material_snapshot(tenant_id, "knowledge", item_id)
        if snapshot_item and snapshot_item["registered_only"]:
            return False, "来源资料仅登记原件（没有可核对正文，不供 AI 使用）"
        expiry = knowledge_expiry_instant(version["expires_at"])
        moment = now or datetime.now(timezone.utc)
        if expiry is not None and moment >= expiry:
            return False, "来源资料已到期"
        return True, ""

    def kb2_topic_ai_state(self, tenant_id: str, topic_id: str, *, now=None) -> dict:
        """主题的 AI 可用状态（由真实资格计算，不按计时/不按开关单独判定）。"""
        topic = self._kb2_topic_row(tenant_id, topic_id)
        if not topic:
            return {"available": False, "reason": "主题不存在", "eligible_paragraphs": 0, "total_paragraphs": 0}
        if topic["status"] == "disabled":
            return {"available": False, "reason": "主题已停用", "eligible_paragraphs": 0, "total_paragraphs": 0}
        reference = self._kb2_topic_reference_version(tenant_id, topic_id)
        if reference is None:
            return {"available": False, "reason": "草稿尚未发布", "eligible_paragraphs": 0, "total_paragraphs": 0}
        if reference["usage"] != "external":
            return {"available": False, "reason": "仅内部使用", "eligible_paragraphs": 0,
                    "total_paragraphs": len(_json_load(reference["content_json"], {}).get("paragraphs") or [])}
        if not reference["ai_authorized"]:
            return {"available": False, "reason": "未授权 AI", "eligible_paragraphs": 0,
                    "total_paragraphs": len(_json_load(reference["content_json"], {}).get("paragraphs") or [])}
        from enterprise_knowledge import knowledge_expiry_instant
        expiry = knowledge_expiry_instant(reference["expires_at"])
        moment = now or datetime.now(timezone.utc)
        if expiry is not None and moment >= expiry:
            return {"available": False, "reason": "主题已到期", "eligible_paragraphs": 0,
                    "total_paragraphs": len(_json_load(reference["content_json"], {}).get("paragraphs") or [])}
        paragraphs = [p for p in (_json_load(reference["content_json"], {}).get("paragraphs") or [])
                      if isinstance(p, dict) and str(p.get("text") or "").strip()]
        eligible = 0
        reasons: list[str] = []
        for paragraph in paragraphs:
            ok, reason = self._kb2_paragraph_evidence_gate(tenant_id, paragraph, now=now)
            if ok:
                eligible += 1
            elif reason and reason not in reasons:
                reasons.append(reason)
        if not paragraphs:
            return {"available": False, "reason": "主题正文为空", "eligible_paragraphs": 0, "total_paragraphs": 0}
        if eligible == 0:
            return {"available": False, "reason": "无可用于对外回答的合格段落（" + ("；".join(reasons[:3]) or "来源不合格") + "）",
                    "eligible_paragraphs": 0, "total_paragraphs": len(paragraphs)}
        return {"available": True, "reason": "", "eligible_paragraphs": eligible, "total_paragraphs": len(paragraphs)}

    def kb2_list_topic_qa_evidence(self, tenant_id: str, *, now=None) -> list[dict]:
        """主题对外的候选证据（模型之前）：逐段过滤；混合主题只返回合格段。"""
        evidence = []
        for topic in self.conn.execute(
            "SELECT * FROM kb2_topics WHERE tenant_id=? AND status='published' ORDER BY updated_at DESC, id",
            (tenant_id,),
        ).fetchall():
            reference = self._kb2_topic_reference_version(tenant_id, topic["id"])
            if reference is None:
                continue
            if reference["usage"] != "external" or not reference["ai_authorized"]:
                continue
            from enterprise_knowledge import knowledge_expiry_instant
            expiry = knowledge_expiry_instant(reference["expires_at"])
            moment = now or datetime.now(timezone.utc)
            if expiry is not None and moment >= expiry:
                continue
            content = _json_load(reference["content_json"], {})
            paragraphs = [p for p in (content.get("paragraphs") or []) if isinstance(p, dict)]
            for index, paragraph in enumerate(paragraphs, start=1):
                text = str(paragraph.get("text") or "").strip()
                if not text:
                    continue
                ok, _reason = self._kb2_paragraph_evidence_gate(tenant_id, paragraph, now=now)
                if not ok:
                    continue
                source = paragraph.get("source") if isinstance(paragraph.get("source"), dict) else None
                source_label = ""
                if source and source.get("kind") == "knowledge":
                    snapshot = self._kb2_material_snapshot(tenant_id, "knowledge", str(source.get("item_id") or ""))
                    if snapshot is not None:
                        source_label = snapshot["title"]
                        location_label = self.kb2_location_label(source.get("location") or {})
                        if location_label:
                            source_label += f" · {location_label}"
                elif source and source.get("kind") == "manual":
                    source_label = "人工撰写"
                evidence.append({
                    "source": "topic",
                    "topic_id": topic["id"], "topic_title": topic["title"],
                    "topic_version_id": reference["id"], "topic_version_number": reference["version"],
                    "published_at": reference["published_at"],
                    "paragraph_id": str(paragraph.get("id") or ""), "paragraph_index": index,
                    "origin": str(paragraph.get("origin") or "manual"),
                    "source_label": source_label,
                    "text": text,
                })
        return evidence

    def kb2_list_item_field_qa_evidence(self, tenant_id: str, *, now=None) -> list[dict]:
        """灵活字段的对外候选证据（模型之前）：仅当前发布快照里的「已核对 + 允许客户引用 + 有值 + 有来源」。"""
        evidence = []
        for row in self.list_ai_answerable_knowledge(tenant_id, now=now):
            snapshot = _json_load(row["content_json"], {})
            for field in snapshot.get("kb2_fields") or []:
                if not isinstance(field, dict):
                    continue
                if not field.get("checked") or not field.get("allow_customer"):
                    continue
                value = field.get("value")
                if value is None or str(value) == "":
                    continue
                if not field.get("source"):
                    continue
                text = f"{field['label']}：{value}"
                if field.get("unit"):
                    text += f" {field['unit']}"
                evidence.append({
                    "source": "item_field",
                    "item_id": row["item_id"], "form": row["form"],
                    "form_label": KNOWLEDGE_FORM_LABELS.get(row["form"], row["form"]),
                    "title": str(snapshot.get("title") or "").strip(),
                    "version_id": row["version_id"], "version_number": row["version_number"],
                    "published_at": row["published_at"],
                    "field_key": field.get("field_key"), "label": field.get("label"),
                    "value": value, "unit": field.get("unit") or "",
                    "field_source": (field.get("source") or {}).get("label") if isinstance(field.get("source"), dict) else "",
                    "text": text,
                })
        return evidence

    def kb2_evidence_valid_after_model(self, tenant_id: str, evidence: dict, *, now=None):
        """模型之后复核（任务5）：主题段与字段证据在生成期间失效（撤权/停用/到期/换版/删改）即拒绝。"""
        if evidence.get("source") == "topic":
            topic = self._kb2_topic_row(tenant_id, str(evidence.get("topic_id") or ""))
            if not topic or topic["status"] != "published":
                return False, "该主题已停用或不存在（生成期间发生变化）"
            reference = self._kb2_topic_reference_version(tenant_id, topic["id"])
            if reference is None or reference["id"] != evidence.get("topic_version_id"):
                return False, "主题已发布新版本（生成期间发生变化）"
            if reference["usage"] != "external" or not reference["ai_authorized"]:
                return False, "主题已调整使用范围或撤销 AI 授权（生成期间发生变化）"
            expiry = knowledge_expiry_instant(reference["expires_at"])
            moment = now or datetime.now(timezone.utc)
            if expiry is not None and moment >= expiry:
                return False, "主题已到期（生成期间发生变化）"
            content = _json_load(reference["content_json"], {})
            paragraph = next((p for p in (content.get("paragraphs") or [])
                              if isinstance(p, dict) and str(p.get("id") or "") == str(evidence.get("paragraph_id") or "")), None)
            if paragraph is None or str(paragraph.get("text") or "").strip() != str(evidence.get("text") or "").strip():
                return False, "该段落内容已在生成期间变化"
            ok, reason = self._kb2_paragraph_evidence_gate(tenant_id, paragraph, now=now)
            if not ok:
                return False, "该段证据在生成期间已失效：" + reason
            return True, ""
        if evidence.get("source") == "item_field":
            item_id = str(evidence.get("item_id") or "")
            rows = [row for row in self.list_ai_answerable_knowledge(tenant_id, item_id, now=now)
                    if row["version_id"] == evidence.get("version_id")]
            if not rows:
                return False, "该资料已停用、撤销授权、到期或被新版本替换（生成期间发生变化）"
            snapshot = _json_load(rows[0]["content_json"], {})
            field = next((f for f in (snapshot.get("kb2_fields") or [])
                          if isinstance(f, dict) and f.get("field_key") == evidence.get("field_key")), None)
            if field is None:
                return False, "该字段已从当前版本移除（生成期间发生变化）"
            if (str(field.get("value")) != str(evidence.get("value"))
                    or str(field.get("unit") or "") != str(evidence.get("unit") or "")
                    or not field.get("checked") or not field.get("allow_customer")):
                return False, "该字段的取值、核对或允许范围已变化（生成期间发生变化）"
            return True, ""
        return False, "未知证据来源"


def start_knowledge_phase2_worker(db_path, interval: int = 15):
    """可恢复的入库工作线程（服务器可选接入）：独立连接，独占认领后处理待处理来源。

    与第一批知识同步 worker 相同的接入方式：随服务关闭而停止；未接模型、未接外部消息。
    排队入口（kb2_create_batch(process_now=False)）落库的批次由本线程真实处理；同一来源
    并发时由数据层认领互斥（认领失败即跳过，不抢执行；中断来源下一轮按接管规则继续）。
    """
    stop = threading.Event()

    def run():
        from app import Database

        db = Database(db_path, initialize=False)
        try:
            while not stop.wait(interval):
                try:
                    db.kb2_process_pending_sources()
                except Exception:
                    # 单个数据库暂时不可用时保留待处理状态，下一轮继续。
                    continue
        finally:
            db.conn.close()

    thread = threading.Thread(target=run, daemon=True, name="enterprise-knowledge-phase2-ingest")
    thread.start()
    return stop
