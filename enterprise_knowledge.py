"""通用企业知识模块：文章 / FAQ / 产品与服务（第一批任务1：真实存储、生命周期与权限）。

- 表：
  knowledge_items      一条知识的身份与生命周期（企业、形态、关联原产品、当前发布版本、草稿引用、负责人）
  knowledge_versions   版本快照：不可变正文/FAQ/字段内容 + 分类 / 用途 / AI 授权 / 可选到期 / 作者 / 来源
  knowledge_sources    私有原件登记或手工出处（原件字节保存属任务2）
  knowledge_processing 处理结果绑定确切版本（pending / completed / failed + 错误与重试计数）
  knowledge_product_settings 原产品的侧车 AI 客服授权与可选到期（清单11；不改动产品行本身）
- 业务规则来源：docs/企业知识管理-PRD与交互设计-v0.1.md（R6/R8/R9/R10/R11/R13/R16、P2/P3 补充确认）
  与 docs/superpowers/plans/2026-10-10-enterprise-knowledge-phase1.md 任务1。
- 权限（后端强制）：本企业所有成员可读；管理员或获「产品管理（products）」模块授权的成员可写。
- 检索排除（R11，不依赖远程索引删除）：停用 / 关闭授权 / 到期在检索前即排除；
  客户回答只绑定「已发布 + 对外 + 已授权 + 未过期 + 当前线上版本处理完成」的确切版本，
  发布新版后旧版本立即不再用于回答；「准备完成」来自真实处理状态，不用计时模拟。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

KNOWLEDGE_FORMS = ("article", "faq", "product", "service")
KNOWLEDGE_STATUSES = ("draft", "published", "disabled")
KNOWLEDGE_USAGES = ("internal", "external")
KNOWLEDGE_SOURCE_KINDS = ("manual", "file")
KNOWLEDGE_FORM_LABELS = {"article": "文章", "faq": "FAQ", "product": "产品", "service": "服务"}
PROCESSING_STATUSES = ("pending", "completed", "failed")

# 任务2 导入边界：TXT/MD/CSV 作为文本材料真实保存原件并读取正文；PDF/Word 只登记原件、人工整理正文。
KNOWLEDGE_TEXT_EXTENSIONS = (".txt", ".md", ".markdown", ".csv")
KNOWLEDGE_DOCUMENT_EXTENSIONS = (".pdf", ".doc", ".docx")
KNOWLEDGE_TEXT_FILE_LIMIT = 1024 * 1024          # 1 MiB
KNOWLEDGE_DOCUMENT_FILE_LIMIT = 10 * 1024 * 1024  # 10 MiB（仅登记原件）
# 精确到期时间的界面时区：Asia/Shanghai（中国全境无夏令时，固定 UTC+8）。
SHANGHAI_TZ = timezone(timedelta(hours=8))

DATE_ONLY_RE = re.compile(r"\d{4}-\d{2}-\d{2}")
EXACT_EXPIRY_RE = re.compile(r"(\d{4}-\d{2}-\d{2})[T ](\d{2}:\d{2}(?::\d{2})?)(Z|[+-]\d{2}:?\d{2})?")


def normalize_knowledge_expiry(value) -> str | None:
    """把可选到期时间归一为 UTC ISO 时间戳（秒精度）。

    - 空值 → None（长期有效）。
    - 旧日期格式 YYYY-MM-DD 兼容：按「当日结束」（UTC 23:59:59）处理，与旧版「当天仍有效」一致。
    - 精确时间：无时区标注按界面上标注的 Asia/Shanghai（UTC+8）解释；也接受显式偏移 / Z。
    """
    if value is None or str(value).strip() == "":
        return None
    text = str(value).strip()
    if DATE_ONLY_RE.fullmatch(text):
        day = datetime.strptime(text, "%Y-%m-%d")
        return day.replace(hour=23, minute=59, second=59, tzinfo=timezone.utc).isoformat()
    match = EXACT_EXPIRY_RE.fullmatch(text)
    if not match:
        raise ValueError("到期时间格式应为 YYYY-MM-DD 或 YYYY-MM-DDTHH:MM（Asia/Shanghai，UTC+8）")
    date_part, time_part, offset = match.groups()
    if len(time_part) == 5:
        time_part += ":00"
    moment = datetime.strptime(f"{date_part}T{time_part}", "%Y-%m-%dT%H:%M:%S")
    if offset is None:
        moment = moment.replace(tzinfo=SHANGHAI_TZ)
    elif offset in ("Z", "z"):
        moment = moment.replace(tzinfo=timezone.utc)
    else:
        off = offset.replace(":", "")
        sign = 1 if off[0] == "+" else -1
        moment = moment.replace(tzinfo=timezone(sign * timedelta(hours=int(off[1:3]), minutes=int(off[3:5]))))
    return moment.astimezone(timezone.utc).replace(microsecond=0).isoformat()


def knowledge_expiry_instant(text) -> datetime | None:
    """把存储里的到期时间解析为 UTC 时刻（兼容旧日期格式与无时区历史值）。"""
    if text is None or str(text).strip() == "":
        return None
    value = str(text).strip()
    if DATE_ONLY_RE.fullmatch(value):
        day = datetime.strptime(value, "%Y-%m-%d")
        return day.replace(hour=23, minute=59, second=59, tzinfo=timezone.utc)
    try:
        moment = datetime.fromisoformat(value)
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


def knowledge_content_projection(form: str, snapshot: dict) -> str:
    """版本内容的文本投影（供真实本地准备/分块；任务4 检索复用）。只输出对客户可见语义的正文。"""
    lines: list[str] = []
    title = str(snapshot.get("title") or "").strip()
    if title:
        lines.append(title)
    if form == "faq":
        for pair in snapshot.get("faq") or []:
            if not isinstance(pair, dict):
                continue
            question = str(pair.get("question") or "").strip()
            answer = str(pair.get("answer") or "").strip()
            if question or answer:
                lines.append(f"问：{question}")
                lines.append(f"答：{answer}")
    else:
        body = str(snapshot.get("body") or "").strip()
        if body:
            lines.append(body)
        if form in ("product", "service"):
            for field in snapshot.get("fields") or []:
                if not isinstance(field, dict):
                    continue
                # 清单12：只有「显式勾选已核对（state=approved）」且「使用范围=对外」的要点进入客户视角投影；
                # 「仅内部」与未标记核准的旧数据一律不进入——不静默把未知要点当作已核准。
                if field.get("state") != "approved" or (field.get("audience") or "external") == "internal":
                    continue
                label = str(field.get("label") or "").strip()
                value = str(field.get("value") or "").strip()
                unit = str(field.get("unit") or "").strip()
                if label and value:
                    lines.append(f"{label}：{value} {unit}".strip())
    return "\n".join(lines).strip()


def _split_projection_chunks(text: str, limit: int = 800) -> list[str]:
    """按段落聚合为不超过 limit 的文本块（真实本地准备结果；不是计时模拟）。"""
    chunks: list[str] = []
    current = ""
    for paragraph in [part for part in re.split(r"\n\s*\n", text) if part.strip()]:
        paragraph = paragraph.strip()
        while len(paragraph) > limit:
            head, paragraph = paragraph[:limit], paragraph[limit:]
            if current:
                chunks.append(current)
                current = ""
            chunks.append(head)
        if not current:
            current = paragraph
        elif len(current) + 1 + len(paragraph) <= limit:
            current += "\n" + paragraph
        else:
            chunks.append(current)
            current = paragraph
    if current:
        chunks.append(current)
    return chunks

# 可选字段的「未提供」哨兵：区分「不修改」与「清空（None）」。
_UNSET = object()


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _today() -> str:
    return datetime.now(timezone.utc).date().isoformat()


class EnterpriseKnowledge:
    """Database 的通用知识 mixin；所有读写由调用方携带企业 ID，服务端强制隔离。"""

    def init_enterprise_knowledge(self) -> None:
        # 清单11：知识视角对原产品的「逐产品 AI 客服授权 + 可选到期」侧车表。
        # 只有首次创建该表时执行一次迁移：把既有「已发布且存在对外已核准字段」的产品按原行为保持可用；
        # 之后新建产品一律默认未授权（不做全局自动授权）。
        settings_existed = self.conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='knowledge_product_settings'"
        ).fetchone() is not None
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS knowledge_items (
                id TEXT PRIMARY KEY,
                tenant_id TEXT NOT NULL REFERENCES tenants(id),
                form TEXT NOT NULL,
                product_id TEXT REFERENCES products(id),
                status TEXT NOT NULL DEFAULT 'draft',
                -- 当前发布版本 / 草稿引用：应用层保证指向本条的版本行；
                -- 不声明 REFERENCES 以避免与 knowledge_versions 循环外键影响后续表重建迁移。
                published_version_id TEXT,
                draft_version_id TEXT,
                owner_user_id TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_knowledge_items_tenant ON knowledge_items(tenant_id, status);
            CREATE TABLE IF NOT EXISTS knowledge_sources (
                id TEXT PRIMARY KEY,
                tenant_id TEXT NOT NULL REFERENCES tenants(id),
                item_id TEXT NOT NULL REFERENCES knowledge_items(id),
                kind TEXT NOT NULL DEFAULT 'manual',
                filename TEXT NOT NULL DEFAULT '',
                file_type TEXT NOT NULL DEFAULT '',
                source_location TEXT NOT NULL DEFAULT '',
                sha256 TEXT,
                storage_path TEXT,
                created_by TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_knowledge_sources_item ON knowledge_sources(item_id);
            CREATE TABLE IF NOT EXISTS knowledge_versions (
                id TEXT PRIMARY KEY,
                tenant_id TEXT NOT NULL REFERENCES tenants(id),
                item_id TEXT NOT NULL REFERENCES knowledge_items(id),
                version INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'draft',
                content_json TEXT NOT NULL DEFAULT '{}',
                category TEXT NOT NULL DEFAULT '',
                usage TEXT NOT NULL DEFAULT 'internal',
                ai_authorized INTEGER NOT NULL DEFAULT 0,
                expires_at TEXT,
                restored_from_version INTEGER,
                source_id TEXT REFERENCES knowledge_sources(id),
                created_by TEXT NOT NULL,
                created_at TEXT NOT NULL,
                published_at TEXT,
                UNIQUE (item_id, version)
            );
            CREATE TABLE IF NOT EXISTS knowledge_processing (
                version_id TEXT PRIMARY KEY REFERENCES knowledge_versions(id),
                tenant_id TEXT NOT NULL REFERENCES tenants(id),
                status TEXT NOT NULL DEFAULT 'pending',
                error TEXT NOT NULL DEFAULT '',
                attempts INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS knowledge_chunks (
                version_id TEXT NOT NULL REFERENCES knowledge_versions(id),
                tenant_id TEXT NOT NULL REFERENCES tenants(id),
                item_id TEXT NOT NULL,
                ordinal INTEGER NOT NULL,
                text TEXT NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY (version_id, ordinal)
            );
            CREATE INDEX IF NOT EXISTS idx_knowledge_chunks_tenant ON knowledge_chunks(tenant_id, item_id);
            CREATE TABLE IF NOT EXISTS knowledge_product_settings (
                product_id TEXT PRIMARY KEY REFERENCES products(id),
                tenant_id TEXT NOT NULL REFERENCES tenants(id),
                ai_authorized INTEGER NOT NULL DEFAULT 0,
                expires_at TEXT,
                updated_by TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL DEFAULT '',
                note TEXT NOT NULL DEFAULT ''
            );
            CREATE INDEX IF NOT EXISTS idx_knowledge_product_settings_tenant ON knowledge_product_settings(tenant_id, ai_authorized);
            """
        )
        self.conn.commit()
        if not settings_existed:
            self._migrate_product_ai_consent()

    # ── 读（本企业成员默认可读；全部按 tenant_id 过滤）──────────────────────

    def list_knowledge_items(self, tenant_id: str, *, form: str | None = None, status: str | None = None) -> list[sqlite3.Row]:
        query = (
            "SELECT i.*, pv.version AS published_version_number, dv.version AS draft_version_number "
            "FROM knowledge_items i "
            "LEFT JOIN knowledge_versions pv ON pv.id = i.published_version_id "
            "LEFT JOIN knowledge_versions dv ON dv.id = i.draft_version_id "
            "WHERE i.tenant_id=?"
        )
        params: list = [tenant_id]
        if form is not None:
            query += " AND i.form=?"
            params.append(form)
        if status is not None:
            query += " AND i.status=?"
            params.append(status)
        query += " ORDER BY i.updated_at DESC, i.id"
        return self.conn.execute(query, params).fetchall()

    def get_knowledge_item(self, tenant_id: str, item_id: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM knowledge_items WHERE tenant_id=? AND id=?", (tenant_id, item_id)
        ).fetchone()

    def list_knowledge_versions(self, tenant_id: str, item_id: str) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM knowledge_versions WHERE tenant_id=? AND item_id=? ORDER BY version DESC",
            (tenant_id, item_id),
        ).fetchall()

    def get_knowledge_version(self, tenant_id: str, version_id: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM knowledge_versions WHERE tenant_id=? AND id=?", (tenant_id, version_id)
        ).fetchone()

    def get_published_knowledge_version(self, tenant_id: str, item_id: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM knowledge_versions WHERE tenant_id=? AND item_id=? AND status='published' "
            "ORDER BY version DESC LIMIT 1",
            (tenant_id, item_id),
        ).fetchone()

    def get_knowledge_draft_version(self, tenant_id: str, item_id: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT v.* FROM knowledge_versions v JOIN knowledge_items i ON i.draft_version_id = v.id "
            "WHERE i.tenant_id=? AND i.id=?",
            (tenant_id, item_id),
        ).fetchone()

    def list_knowledge_sources(self, tenant_id: str, item_id: str) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM knowledge_sources WHERE tenant_id=? AND item_id=? ORDER BY created_at DESC, id",
            (tenant_id, item_id),
        ).fetchall()

    def list_ai_answerable_knowledge(self, tenant_id: str, item_id: str | None = None, *, now: datetime | None = None) -> list[sqlite3.Row]:
        """客户视角检索候选（任务4 共用过滤器的基础）：只含当前确切线上版本。

        条件：已发布 + 对外 + 已授权 + 未过期 + 当前线上版本处理完成。
        停用 / 关闭授权 / 到期 / 内部资料在检索前即排除；发布新版后旧版本立即不再出现。
        到期排除语义：now >= 到期时刻即排除（精确到秒，UTC 比较）；空 = 长期有效。
        """
        query = (
            "SELECT i.id AS item_id, i.form, i.owner_user_id, v.id AS version_id, v.version AS version_number, "
            "v.content_json, v.category, v.usage, v.ai_authorized, v.expires_at, v.published_at "
            "FROM knowledge_items i "
            "JOIN knowledge_versions v ON v.id = i.published_version_id "
            "JOIN knowledge_processing pr ON pr.version_id = v.id "
            "WHERE i.tenant_id=? AND i.status='published' AND v.usage='external' AND v.ai_authorized=1 "
            "AND pr.status='completed'"
        )
        params: list = [tenant_id]
        if item_id is not None:
            query += " AND i.id=?"
            params.append(item_id)
        query += " ORDER BY v.published_at DESC, i.id"
        moment = now or datetime.now(timezone.utc)
        rows = self.conn.execute(query, params).fetchall()
        return [
            row for row in rows
            if (expiry := knowledge_expiry_instant(row["expires_at"])) is None or moment < expiry
        ]

    def knowledge_item_ui_state(self, tenant_id: str, item_id: str, *, now: datetime | None = None) -> dict:
        """生命周期展示与可用性原因（系统计算；界面不手选）。原因按固定优先级取第一条。"""
        item = self.get_knowledge_item(tenant_id, item_id)
        if not item:
            raise ValueError("知识不存在")
        state = {"status": item["status"], "available": False, "reason": "", "title": item["status"]}
        if item["status"] == "draft":
            draft = self.get_knowledge_draft_version(tenant_id, item_id)
            return {"status": "draft", "available": False, "reason": "草稿尚未发布", "title": "草稿",
                    "has_draft": draft is not None}
        if item["status"] == "disabled":
            return {"status": "disabled", "available": False, "reason": "已停用", "title": "已停用", "has_draft": False}
        version = self.conn.execute(
            "SELECT * FROM knowledge_versions WHERE tenant_id=? AND id=?", (tenant_id, item["published_version_id"])
        ).fetchone()
        draft = self.get_knowledge_draft_version(tenant_id, item_id)
        has_draft = draft is not None
        if not version:
            return {"status": "published", "available": False, "reason": "没有当前线上版本", "title": "已发布（AI客服不可用）", "has_draft": has_draft}
        if version["usage"] != "external":
            reason = "仅内部"
        elif not version["ai_authorized"]:
            reason = "未授权"
        else:
            processing = self.knowledge_processing_state(tenant_id, version["id"])
            expiry = knowledge_expiry_instant(version["expires_at"])
            moment = now or datetime.now(timezone.utc)
            if processing is None or processing["status"] == "pending":
                reason = "准备中"
            elif processing["status"] == "failed":
                reason = "处理失败"
            elif expiry is not None and moment >= expiry:
                reason = "过期"
            else:
                return {"status": "published", "available": True, "reason": "", "title": "已发布（AI客服可用）", "has_draft": has_draft}
        return {"status": "published", "available": False, "reason": reason, "title": "已发布（AI客服不可用）", "has_draft": has_draft}

    # ── 写（管理员或获 products 模块授权的成员；后端强制）────────────────────

    def knowledge_can_manage(self, tenant_id: str, user_id: str) -> bool:
        """写权限查询：企业管理员，或获产品管理（products）模块授权的在职成员（P2 沿用产品权限）。"""
        return self._knowledge_editor(tenant_id, user_id) is not None

    def _knowledge_editor(self, tenant_id: str, user_id: str) -> sqlite3.Row | None:
        user = self.conn.execute(
            "SELECT id, tenant_id, email, role, active FROM users WHERE id=?", (user_id,)
        ).fetchone()
        if not user or not user["active"] or user["tenant_id"] != tenant_id:
            return None
        if user["role"] == "admin":
            return user
        modules = {
            row["module"]
            for row in self.conn.execute("SELECT module FROM user_modules WHERE user_id=?", (user_id,))
        }
        return user if "products" in modules else None

    def _require_knowledge_editor(self, tenant_id: str, user_id: str) -> sqlite3.Row:
        user = self._knowledge_editor(tenant_id, user_id)
        if not user:
            raise PermissionError("需要当前企业的管理员或产品管理模块权限")
        return user

    normalize_knowledge_expiry = staticmethod(normalize_knowledge_expiry)

    @staticmethod
    def _clean_expires_at(value):
        """任务1 起保存的可选到期；任务2 起支持精确时间戳（Asia/Shanghai 输入，UTC 归一）。"""
        return normalize_knowledge_expiry(value)

    @staticmethod
    def _dump_content(snapshot: dict) -> str:
        try:
            return json.dumps(snapshot, ensure_ascii=False)
        except (TypeError, ValueError) as exc:
            raise ValueError("知识内容无法序列化") from exc

    @staticmethod
    def _validate_publish_content(form: str, snapshot: dict) -> None:
        if not str(snapshot.get("title") or "").strip():
            raise ValueError("标题不能为空")
        if form in ("article", "product", "service") and not str(snapshot.get("body") or "").strip():
            # 手工建立的产品/服务为行业中立正文；原始产品仍走产品模块的字段核准与发布（不重复为真源）。
            raise ValueError("正文不能为空")
        if form == "faq":
            pairs = snapshot.get("faq") or []
            complete = [
                pair for pair in pairs
                if isinstance(pair, dict) and str(pair.get("question") or "").strip() and str(pair.get("answer") or "").strip()
            ]
            if not complete:
                raise ValueError("FAQ 至少需要一组完整问答")
        if form in ("product", "service"):
            fields = snapshot.get("fields")
            if fields is not None and not isinstance(fields, list):
                raise ValueError("自定义要点格式无效")
            for field in fields or []:
                if not isinstance(field, dict) or not str(field.get("label") or "").strip() or not str(field.get("value") or "").strip():
                    raise ValueError("自定义要点需要同时填写名称与内容")

    def create_knowledge_item(
        self,
        tenant_id: str,
        form: str,
        title: str,
        user_id: str,
        *,
        content: dict | None = None,
        category: str = "",
        usage: str = "internal",
        ai_authorized: bool = False,
        expires_at=None,
        owner_user_id: str = "",
        product_id: str | None = None,
    ) -> str:
        """创建知识条目并生成 v1 草稿（未发布）；默认对内、未授权。"""
        editor = self._require_knowledge_editor(tenant_id, user_id)
        form = (form or "").strip()
        if form not in KNOWLEDGE_FORMS:
            raise ValueError("不支持的知识形态")
        title = (title or "").strip()
        if not title:
            raise ValueError("标题不能为空")
        if usage not in KNOWLEDGE_USAGES:
            raise ValueError("使用范围无效")
        expires_at = self._clean_expires_at(expires_at)
        if content is not None and not isinstance(content, dict):
            raise ValueError("知识内容格式无效")
        if product_id and not self.get_product(tenant_id, product_id):
            raise ValueError("关联产品不存在")
        snapshot = dict(content or {})
        snapshot["title"] = title
        content_json = self._dump_content(snapshot)
        item_id = secrets.token_hex(12)
        version_id = secrets.token_hex(12)
        timestamp = _now()
        self.conn.execute(
            "INSERT INTO knowledge_items(id, tenant_id, form, product_id, status, published_version_id, draft_version_id, owner_user_id, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, 'draft', NULL, ?, ?, ?, ?)",
            (item_id, tenant_id, form, product_id or None, version_id, owner_user_id or user_id, timestamp, timestamp),
        )
        self.conn.execute(
            "INSERT INTO knowledge_versions(id, tenant_id, item_id, version, status, content_json, category, usage, ai_authorized, expires_at, created_by, created_at) "
            "VALUES (?, ?, ?, 1, 'draft', ?, ?, ?, ?, ?, ?, ?)",
            (
                version_id, tenant_id, item_id, content_json, (category or "").strip(), usage,
                1 if ai_authorized else 0, expires_at, editor["email"], timestamp,
            ),
        )
        self._audit(tenant_id, "knowledge", item_id, "created", editor["email"], f"form={form};title={title}")
        self.conn.commit()
        return item_id

    def save_knowledge_draft(
        self,
        tenant_id: str,
        item_id: str,
        user_id: str,
        *,
        content: dict | None = None,
        category=_UNSET,
        usage=_UNSET,
        ai_authorized=_UNSET,
        expires_at=_UNSET,
        source_id=_UNSET,
    ) -> str:
        """保存草稿；已发布条目的线上版本保持不变（R13/§13）。

        无草稿时基于线上版本新建 v(n+1) 草稿；已有草稿原地更新。返回草稿版本 id。
        """
        editor = self._require_knowledge_editor(tenant_id, user_id)
        item = self.get_knowledge_item(tenant_id, item_id)
        if not item:
            raise ValueError("知识不存在")
        if content is not None and not isinstance(content, dict):
            raise ValueError("知识内容格式无效")
        if usage is not _UNSET and usage not in KNOWLEDGE_USAGES:
            raise ValueError("使用范围无效")
        if expires_at is not _UNSET:
            expires_at = self._clean_expires_at(expires_at)
        if source_id is not _UNSET and source_id is not None:
            self._require_knowledge_source(tenant_id, item_id, source_id)
        draft = self.get_knowledge_draft_version(tenant_id, item_id)
        published = self.get_published_knowledge_version(tenant_id, item_id)
        reference = draft or published
        snapshot = json.loads(reference["content_json"] or "{}") if reference else {}
        if content:
            snapshot.update(content)
        if not str(snapshot.get("title") or "").strip():
            raise ValueError("标题不能为空")
        settings = {
            "category": reference["category"] if reference else "",
            "usage": reference["usage"] if reference else "internal",
            "ai_authorized": reference["ai_authorized"] if reference else 0,
            "expires_at": reference["expires_at"] if reference else None,
            "source_id": reference["source_id"] if reference else None,
        }
        if category is not _UNSET:
            settings["category"] = (category or "").strip()
        if usage is not _UNSET:
            settings["usage"] = usage
        if ai_authorized is not _UNSET:
            settings["ai_authorized"] = 1 if ai_authorized else 0
        if expires_at is not _UNSET:
            settings["expires_at"] = expires_at
        if source_id is not _UNSET:
            settings["source_id"] = source_id
        timestamp = _now()
        if draft:
            self.conn.execute(
                "UPDATE knowledge_versions SET content_json=?, category=?, usage=?, ai_authorized=?, expires_at=?, source_id=? "
                "WHERE tenant_id=? AND id=? AND status='draft'",
                (
                    self._dump_content(snapshot), settings["category"], settings["usage"],
                    settings["ai_authorized"], settings["expires_at"], settings["source_id"],
                    tenant_id, draft["id"],
                ),
            )
            version_id = draft["id"]
            action = "draft_updated"
        else:
            next_version = self.conn.execute(
                "SELECT COALESCE(MAX(version), 0) + 1 FROM knowledge_versions WHERE tenant_id=? AND item_id=?",
                (tenant_id, item_id),
            ).fetchone()[0]
            version_id = secrets.token_hex(12)
            self.conn.execute(
                "INSERT INTO knowledge_versions(id, tenant_id, item_id, version, status, content_json, category, usage, ai_authorized, expires_at, source_id, created_by, created_at) "
                "VALUES (?, ?, ?, ?, 'draft', ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    version_id, tenant_id, item_id, next_version, self._dump_content(snapshot),
                    settings["category"], settings["usage"], settings["ai_authorized"],
                    settings["expires_at"], settings["source_id"], editor["email"], timestamp,
                ),
            )
            self.conn.execute(
                "UPDATE knowledge_items SET draft_version_id=?, updated_at=? WHERE tenant_id=? AND id=?",
                (version_id, timestamp, tenant_id, item_id),
            )
            action = "draft_created"
        self.conn.execute(
            "UPDATE knowledge_items SET updated_at=? WHERE tenant_id=? AND id=?", (timestamp, tenant_id, item_id)
        )
        self._audit(tenant_id, "knowledge", item_id, action, editor["email"], f"version_row={version_id[:8]}")
        self.conn.commit()
        return version_id

    def publish_knowledge_draft(self, tenant_id: str, item_id: str, user_id: str, *, confirm_ai: bool = False) -> int:
        """发布当前草稿：切换线上版本，旧版本转入历史；处理状态绑定确切的新版本（R10）。

        对外且授权的发布需要一次确认（R16），未确认时保持草稿、线上不受影响。
        """
        editor = self._require_knowledge_editor(tenant_id, user_id)
        item = self.get_knowledge_item(tenant_id, item_id)
        if not item:
            raise ValueError("知识不存在")
        if item["status"] == "disabled":
            raise ValueError("已停用的知识请先重新启用")
        draft = self.get_knowledge_draft_version(tenant_id, item_id)
        if not draft:
            raise ValueError("没有待发布的草稿")
        snapshot = json.loads(draft["content_json"] or "{}")
        # 任务5 / B2-B09 钩子（第二批）：入库资料的核对 / 排除 / 来源身份 / 正文一致性在共享发布
        # 服务强制校验（旧入口与书签路径同样不能绕过）；第一批旧资料（无第二批来源）保持兼容。
        publish_gate = getattr(self, "_kb2_validate_publish_draft", None)
        if publish_gate is not None:
            publish_gate(tenant_id, item_id, draft)
        self._validate_publish_content(item["form"], snapshot)
        # 任务4 钩子（第二批模板字段）：采用模板的资料在发布前校验必填字段；
        # 返回带字段快照的完整内容，随发布一起提交；未采用模板时为 None（第一批行为不变）。
        fields_hook = getattr(self, "_kb2_prepare_publish_fields_content", None)
        published_snapshot = None
        if fields_hook is not None:
            published_snapshot = fields_hook(tenant_id, item_id, snapshot)
        if draft["usage"] == "external" and draft["ai_authorized"] and not confirm_ai:
            raise ValueError("对外且授权 AI 客服使用的知识发布需要确认")
        previous = self.get_published_knowledge_version(tenant_id, item_id)
        if previous:
            # 发布新版后旧版本不得继续用于回答：旧快照仅作历史保留。
            self.conn.execute("UPDATE knowledge_versions SET status='superseded' WHERE id=?", (previous["id"],))
        timestamp = _now()
        if published_snapshot is not None:
            self.conn.execute(
                "UPDATE knowledge_versions SET content_json=? WHERE tenant_id=? AND id=?",
                (self._dump_content(published_snapshot), tenant_id, draft["id"]),
            )
        self.conn.execute(
            "UPDATE knowledge_versions SET status='published', published_at=? WHERE id=?", (timestamp, draft["id"])
        )
        self.conn.execute(
            "UPDATE knowledge_items SET status='published', published_version_id=?, draft_version_id=NULL, updated_at=? "
            "WHERE tenant_id=? AND id=?",
            (draft["id"], timestamp, tenant_id, item_id),
        )
        # 新版本以「准备中」起步；完成/失败只能由真实处理管线回报（不靠计时模拟）。
        self._upsert_processing(tenant_id, draft["id"], "pending", "", reset_attempts=True)
        self._audit(
            tenant_id, "knowledge", item_id, "published", editor["email"],
            f"version={draft['version']};usage={draft['usage']};ai_authorized={draft['ai_authorized']}",
        )
        self.conn.commit()
        return draft["version"]

    def disable_knowledge_item(self, tenant_id: str, item_id: str, user_id: str, *, confirmed: bool = False) -> None:
        """停用已发布条目（需确认）：检索前即时排除；历史、来源与版本保留。"""
        editor = self._require_knowledge_editor(tenant_id, user_id)
        item = self.get_knowledge_item(tenant_id, item_id)
        if not item:
            raise ValueError("知识不存在")
        if item["status"] != "published":
            raise ValueError("只有已发布的知识才能停用")
        if not confirmed:
            raise ValueError("停用需要确认")
        timestamp = _now()
        self.conn.execute(
            "UPDATE knowledge_items SET status='disabled', updated_at=? WHERE tenant_id=? AND id=?",
            (timestamp, tenant_id, item_id),
        )
        self._audit(tenant_id, "knowledge", item_id, "disabled", editor["email"])
        self.conn.commit()

    def enable_knowledge_item(self, tenant_id: str, item_id: str, user_id: str, *, confirmed: bool = False) -> None:
        """重新启用（对外+授权需确认）：恢复已发布状态，沿用原线上版本。"""
        editor = self._require_knowledge_editor(tenant_id, user_id)
        item = self.get_knowledge_item(tenant_id, item_id)
        if not item:
            raise ValueError("知识不存在")
        if item["status"] != "disabled":
            raise ValueError("只有已停用的知识才能重新启用")
        published = self.get_published_knowledge_version(tenant_id, item_id)
        if not published:
            raise ValueError("没有可恢复的已发布版本")
        if published["usage"] == "external" and published["ai_authorized"] and not confirmed:
            raise ValueError("对外且授权 AI 客服使用的知识重新启用需要确认")
        timestamp = _now()
        self.conn.execute(
            "UPDATE knowledge_items SET status='published', updated_at=? WHERE tenant_id=? AND id=?",
            (timestamp, tenant_id, item_id),
        )
        self._audit(tenant_id, "knowledge", item_id, "enabled", editor["email"], f"version={published['version']}")
        self.conn.commit()

    def restore_knowledge_version(self, tenant_id: str, item_id: str, version: int, user_id: str) -> str:
        """恢复历史版本：创建新草稿（注明恢复自 vX），不覆盖历史、不影响线上（R13）。"""
        editor = self._require_knowledge_editor(tenant_id, user_id)
        if not self.get_knowledge_item(tenant_id, item_id):
            raise ValueError("知识不存在")
        source = self.conn.execute(
            "SELECT * FROM knowledge_versions WHERE tenant_id=? AND item_id=? AND version=?",
            (tenant_id, item_id, int(version)),
        ).fetchone()
        if not source:
            raise ValueError("历史版本不存在")
        if source["status"] == "draft":
            raise ValueError("当前草稿无需恢复")
        if self.get_knowledge_draft_version(tenant_id, item_id):
            raise ValueError("已有未发布草稿；请先发布或继续编辑现有草稿")
        next_version = self.conn.execute(
            "SELECT COALESCE(MAX(version), 0) + 1 FROM knowledge_versions WHERE tenant_id=? AND item_id=?",
            (tenant_id, item_id),
        ).fetchone()[0]
        version_id = secrets.token_hex(12)
        timestamp = _now()
        self.conn.execute(
            "INSERT INTO knowledge_versions(id, tenant_id, item_id, version, status, content_json, category, usage, ai_authorized, expires_at, restored_from_version, source_id, created_by, created_at) "
            "VALUES (?, ?, ?, ?, 'draft', ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                version_id, tenant_id, item_id, next_version, source["content_json"], source["category"],
                source["usage"], source["ai_authorized"], source["expires_at"], source["version"],
                source["source_id"], editor["email"], timestamp,
            ),
        )
        self.conn.execute(
            "UPDATE knowledge_items SET draft_version_id=?, updated_at=? WHERE tenant_id=? AND id=?",
            (version_id, timestamp, tenant_id, item_id),
        )
        self._audit(tenant_id, "knowledge", item_id, "restored", editor["email"], f"from_version={source['version']};new_version={next_version}")
        # 第二批钩子（B2-B11）：恢复即用恢复点字段快照初始化工作集（字段行）；
        # 之后发生的明确编辑进入新草稿的发布快照（发布按当前工作集冻结）。
        init_fields = getattr(self, "_kb2_init_fields_from_restore", None)
        if init_fields is not None:
            init_fields(tenant_id, item_id, version_id)
        self.conn.commit()
        return version_id

    def set_knowledge_authorization(self, tenant_id: str, item_id: str, user_id: str, authorized: bool, *, confirmed: bool = False) -> None:
        """AI 客服授权开关（R11）：调整立即生效（检索前直接生效，不等远程索引删除）。

        直接更新当前线上版本的授权标记；若存在未发布草稿，同步其授权值，避免发布草稿时意外回退。
        对已发布且对外的知识开启授权需要一次确认（R16）；关闭授权无需确认。
        """
        editor = self._require_knowledge_editor(tenant_id, user_id)
        item = self.get_knowledge_item(tenant_id, item_id)
        if not item:
            raise ValueError("知识不存在")
        published = self.get_published_knowledge_version(tenant_id, item_id)
        draft = self.get_knowledge_draft_version(tenant_id, item_id)
        if authorized and published and published["usage"] == "external" and not confirmed:
            raise ValueError("对已发布的对外的知识开启 AI 授权需要确认")
        targets = [row["id"] for row in (published, draft) if row]
        if not targets:
            raise ValueError("知识还没有可授权的版本")
        for version_id in targets:
            self.conn.execute("UPDATE knowledge_versions SET ai_authorized=? WHERE id=?", (1 if authorized else 0, version_id))
        self.conn.execute(
            "UPDATE knowledge_items SET updated_at=? WHERE tenant_id=? AND id=?", (_now(), tenant_id, item_id)
        )
        action = "ai_authorization_granted" if authorized else "ai_authorization_revoked"
        self._audit(tenant_id, "knowledge", item_id, action, editor["email"])
        self.conn.commit()

    # ── 来源（私有原件登记或手工出处；原件字节保存属任务2）──────────────────

    def create_knowledge_source(
        self,
        tenant_id: str,
        item_id: str,
        user_id: str,
        *,
        kind: str = "manual",
        filename: str = "",
        file_type: str = "",
        source_location: str = "",
        sha256: str | None = None,
        storage_path: str | None = None,
    ) -> str:
        editor = self._require_knowledge_editor(tenant_id, user_id)
        if not self.get_knowledge_item(tenant_id, item_id):
            raise ValueError("知识不存在")
        if kind not in KNOWLEDGE_SOURCE_KINDS:
            raise ValueError("来源类型无效")
        if not (filename or "").strip() and not (source_location or "").strip():
            raise ValueError("来源说明不能为空")
        source_id = secrets.token_hex(12)
        self.conn.execute(
            "INSERT INTO knowledge_sources(id, tenant_id, item_id, kind, filename, file_type, source_location, sha256, storage_path, created_by, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                source_id, tenant_id, item_id, kind, (filename or "").strip(), (file_type or "").strip(),
                (source_location or "").strip(), sha256, storage_path, editor["email"], _now(),
            ),
        )
        self._audit(tenant_id, "knowledge", item_id, "source_registered", editor["email"], f"source={source_id[:8]}")
        self.conn.commit()
        return source_id

    def _require_knowledge_source(self, tenant_id: str, item_id: str, source_id: str) -> sqlite3.Row:
        row = self.conn.execute(
            "SELECT id FROM knowledge_sources WHERE tenant_id=? AND item_id=? AND id=?",
            (tenant_id, item_id, source_id),
        ).fetchone()
        if not row:
            raise ValueError("来源不存在或不属于该知识")
        return row

    # ── 处理结果（绑定确切版本；失败记录错误与重试）──────────────────────────

    def knowledge_processing_state(self, tenant_id: str, version_id: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM knowledge_processing WHERE tenant_id=? AND version_id=?", (tenant_id, version_id)
        ).fetchone()

    def set_knowledge_processing_result(self, tenant_id: str, version_id: str, status: str, error: str = "") -> None:
        """处理管线回报真实结果（构建/索引）：结果绑定确切版本行；失败记录错误。"""
        if status not in PROCESSING_STATUSES:
            raise ValueError("处理状态无效")
        if not self.get_knowledge_version(tenant_id, version_id):
            raise ValueError("知识版本不存在")
        self._upsert_processing(tenant_id, version_id, status, error)
        self.conn.commit()

    def retry_knowledge_processing(self, tenant_id: str, version_id: str, user_id: str) -> None:
        """重试失败的处理（记录重试次数与清空错误）：回到「准备中」，完成后才恢复可用。"""
        editor = self._require_knowledge_editor(tenant_id, user_id)
        version = self.get_knowledge_version(tenant_id, version_id)
        if not version:
            raise ValueError("知识版本不存在")
        state = self.knowledge_processing_state(tenant_id, version_id)
        if not state or state["status"] != "failed":
            raise ValueError("只有处理失败的知识才能重试")
        self.conn.execute(
            "UPDATE knowledge_processing SET status='pending', error='', attempts=attempts+1, updated_at=? WHERE version_id=?",
            (_now(), version_id),
        )
        self._audit(tenant_id, "knowledge", version["item_id"], "processing_retried", editor["email"], f"attempts={int(state['attempts']) + 1}")
        self.conn.commit()

    def _upsert_processing(self, tenant_id: str, version_id: str, status: str, error: str = "", *, reset_attempts: bool = False) -> None:
        existing = self.conn.execute(
            "SELECT attempts FROM knowledge_processing WHERE version_id=?", (version_id,)
        ).fetchone()
        attempts = 0 if (existing is None or reset_attempts) else existing["attempts"]
        self.conn.execute(
            "INSERT INTO knowledge_processing(version_id, tenant_id, status, error, attempts, updated_at) VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(version_id) DO UPDATE SET status=excluded.status, error=excluded.error, attempts=excluded.attempts, updated_at=excluded.updated_at",
            (version_id, tenant_id, status, error or "", attempts, _now()),
        )

    # ── 任务2：导入（TXT/MD/CSV 真实保存原件并读取正文；PDF/Word 只登记原件）────────
    # 说明：知识导入的私有原件与产品来源资料共用既有私有上传目录约束；
    # 测试可注入 knowledge_storage_root 以隔离真实 data/uploads。

    def _knowledge_storage_dir(self) -> Path:
        """知识导入原件的私有目录。

        生产沿用既有私有上传目录（data/uploads，可经 B2B_UPLOAD_DIR 配置为持久目录）；
        测试/隔离环境可通过实例属性 knowledge_storage_root 或环境变量
        B2B_KB_STORAGE_ROOT 指向临时目录（HTTP 处理会按请求重建 Database 实例，
        环境变量在两种路径下都生效）。
        目录为本次新建时收紧为 0700；已存在的目录保持原权限不动（T4 复核既有目录）。
        """
        root = getattr(self, "knowledge_storage_root", None) or os.environ.get("B2B_KB_STORAGE_ROOT")
        if root is None:
            from app import upload_dir
            root = upload_dir()
        from app import ensure_private_dir
        return ensure_private_dir(Path(root))

    @staticmethod
    def _decode_knowledge_text(data: bytes) -> str:
        for encoding in ("utf-8-sig", "utf-8", "gb18030"):
            try:
                return data.decode(encoding)
            except UnicodeDecodeError:
                continue
        raise ValueError("文件内容不是可识别的文本编码（支持 UTF-8 / GB）")

    def import_knowledge_file(
        self,
        tenant_id: str,
        user_id: str,
        *,
        filename: str,
        data: bytes,
        title: str = "",
        category: str = "",
        usage: str = "internal",
        ai_authorized: bool = False,
        expires_at=None,
        owner_user_id: str = "",
        source_location: str = "",
    ) -> dict:
        """真实保存导入原件并创建条目草稿（不自动推断行业字段）。

        - TXT/MD/CSV：校验大小与扩展名、解码为可核对文本作为草稿正文；原件字节私有保存。
        - PDF/Word：只登记原件，正文留空由人工整理（后续批次才做自动解析）。
        返回 {item_id, source_id, text, kind, size}；text 为 None 表示文档登记。
        """
        editor = self._require_knowledge_editor(tenant_id, user_id)
        name = str(filename or "").strip()
        if not name:
            raise ValueError("缺少文件名")
        if not isinstance(data, (bytes, bytearray)):
            raise ValueError("文件内容无效")
        payload = bytes(data)
        extension = Path(name).suffix.lower()
        if extension in KNOWLEDGE_TEXT_EXTENSIONS:
            if len(payload) > KNOWLEDGE_TEXT_FILE_LIMIT:
                raise ValueError("文本文件不能超过 1MB；大文件请先拆分或改用手工摘录")
            if b"\x00" in payload:
                raise ValueError("文件不是纯文本（包含二进制内容）")
            text = self._decode_knowledge_text(payload)
            kind = "text"
        elif extension in KNOWLEDGE_DOCUMENT_EXTENSIONS:
            if len(payload) > KNOWLEDGE_DOCUMENT_FILE_LIMIT:
                raise ValueError("原件文件不能超过 10MB")
            text = None
            kind = "document"
        else:
            raise ValueError("暂不支持该格式：第一批支持 TXT / Markdown / CSV；PDF 与 Word 只登记原件")
        item_title = str(title or "").strip() or Path(name).stem.strip() or "未命名导入"
        item_id = self.create_knowledge_item(
            tenant_id, "article", item_title, user_id,
            content={"body": text or ""}, category=category, usage=usage,
            ai_authorized=ai_authorized, expires_at=expires_at, owner_user_id=owner_user_id,
        )
        draft = self.get_knowledge_draft_version(tenant_id, item_id)
        sha256 = hashlib.sha256(payload).hexdigest()
        storage_dir = self._knowledge_storage_dir()
        source_id = secrets.token_hex(12)
        safe_name = re.sub(r"[^a-zA-Z0-9._-]+", "_", Path(name).name).strip(".") or "source.bin"
        target = storage_dir / f"kb-{source_id}-{safe_name}"
        from app import write_private_bytes
        write_private_bytes(target, payload)
        try:
            location = (source_location or "").strip() or f"导入自文件：{Path(name).name}"
            self.conn.execute(
                "INSERT INTO knowledge_sources(id, tenant_id, item_id, kind, filename, file_type, source_location, sha256, storage_path, created_by, created_at) "
                "VALUES (?, ?, ?, 'file', ?, ?, ?, ?, ?, ?, ?)",
                (source_id, tenant_id, item_id, Path(name).name, extension.lstrip(".") or "other",
                 location, sha256, str(target), editor["email"], _now()),
            )
            if draft:
                self.conn.execute(
                    "UPDATE knowledge_versions SET source_id=? WHERE tenant_id=? AND id=?", (source_id, tenant_id, draft["id"])
                )
            self._audit(tenant_id, "knowledge", item_id, "file_imported", editor["email"],
                        f"file={Path(name).name};bytes={len(payload)};kind={kind}")
            self.conn.commit()
        except Exception:
            target.unlink(missing_ok=True)
            raise
        return {"item_id": item_id, "source_id": source_id, "text": text, "kind": kind, "size": len(payload)}

    def read_knowledge_source_file(self, tenant_id: str, source_id: str) -> tuple[sqlite3.Row, bytes]:
        """私有原件读取（仅本企业）：校验归属、磁盘路径与 sha256；跨企业不可读。"""
        row = self.conn.execute(
            "SELECT * FROM knowledge_sources WHERE tenant_id=? AND id=?", (tenant_id, source_id)
        ).fetchone()
        if not row or not row["storage_path"]:
            raise ValueError("原件不存在或未保存")
        target = Path(row["storage_path"])
        if not target.is_file():
            raise ValueError("原件文件缺失")
        data = target.read_bytes()
        if row["sha256"] and hashlib.sha256(data).hexdigest() != row["sha256"]:
            raise ValueError("原件校验失败（内容与登记不一致）")
        return row, data

    # ── 任务2：原产品适配（只读投影；生命周期与字段核准仍以产品模块为唯一权威）─────
    # 清单10：客户视角只投影「已发布版本快照」里的字段值，再与当前逐字段/来源撤销状态求交集；
    # 未发布的编辑既不进入客户证据，也不改变当前发布视图（绝不出现草稿候选值）。
    # 清单11：原产品在知识视角有独立侧车设置（AI 客服授权 + 可选到期），不改动产品行本身。

    def knowledge_product_settings(self, tenant_id: str, product_id: str) -> dict:
        """逐产品侧车设置：AI 客服授权与可选到期（默认未授权、长期有效）。"""
        row = self.conn.execute(
            "SELECT * FROM knowledge_product_settings WHERE tenant_id=? AND product_id=?",
            (tenant_id, product_id),
        ).fetchone()
        if not row:
            return {"product_id": product_id, "tenant_id": tenant_id, "ai_authorized": 0,
                    "expires_at": None, "updated_by": "", "updated_at": "", "note": ""}
        return dict(row)

    def _knowledge_snapshot_field_revocation(self, field: dict, sources: dict[str, sqlite3.Row]) -> str:
        """发布快照字段自身的撤销检查（清单15/16）：发布时未「对外 + 已核准」，或发布来源 A
        缺失 / 已删除 / 已被禁止使用——当前草稿的改动不能绕过。返回原因，空字符串表示通过。"""
        if not str(field.get("value") or "").strip():
            return "发布版本内容为空"
        if (field.get("audience") or "external") != "external":
            return "发布版本使用范围仅内部"
        if str(field.get("state") or "") != "approved":
            return "发布版本字段未核准"
        source_id = str(field.get("source_document_id") or "").strip()
        if not source_id:
            return "发布版本缺少来源记录"
        source = sources.get(source_id)
        if source is None:
            return "发布版本来源已不存在"
        if (source["usage"] or "internal") == "restricted":
            return "发布版本来源已被标记禁止使用"
        return ""

    def knowledge_product_field_revocations(self, tenant_id: str, product_id: str) -> dict[str, str]:
        """已发布快照字段 → 当前撤销原因（清单10/15/16：撤销立即生效；未撤销的字段不出现）。

        除当前字段自身的状态外，快照字段「发布时对外 + 已核准」与「发布来源 A 当前可用」
        也参与判定：草稿把内部字段改成对外、或把来源从 A 换成 B，都不能让 v1 快照
        继续沿用旧状态对外。
        """
        version = self.get_published_version(tenant_id, product_id)
        if not version:
            return {}
        try:
            snapshot = json.loads(version["snapshot_json"] or "{}")
        except ValueError:
            return {}
        live = {row["field_key"]: row for row in self.get_fields(tenant_id, product_id)}
        sources = {row["id"]: row for row in self.get_sources(tenant_id, product_id)}
        restricted = {
            source_id for source_id, source in sources.items()
            if (source["usage"] or "internal") == "restricted"
        }
        revoked: dict[str, str] = {}
        for field in snapshot.get("fields") or []:
            if not isinstance(field, dict):
                continue
            key = str(field.get("field_key") or "")
            if not key:
                continue
            row = live.get(key)
            if row is None:
                revoked[key] = "已从产品模块移除"
            elif row["state"] != "approved":
                revoked[key] = "已重新进入待核准（当前未核准）"
            elif (row["audience"] or "external") != "external":
                revoked[key] = "使用范围已改为仅内部"
            elif not str(field.get("value") or "").strip():
                revoked[key] = "发布版本内容为空"
            elif not row["source_document_id"] or not str(row["source_location"] or "").strip():
                revoked[key] = "出处不完整"
            elif row["source_document_id"] in restricted:
                revoked[key] = "来源文件已被标记禁止使用"
            else:
                reason = self._knowledge_snapshot_field_revocation(field, sources)
                if reason:
                    revoked[key] = reason
        return revoked

    def knowledge_product_fields(self, tenant_id: str, product_id: str) -> list[dict]:
        """客户视角的产品字段：已发布快照值 ∩ 当前未被撤销（R14 + 清单10/15/16）。

        - 值 / 单位一律取已发布版本快照；未发布的编辑不进入客户视角（也绝不出现草稿候选值）。
        - 快照字段必须按「对外 + 已核准」发布，且当前字段也仍是「对外 + 已核准」；
          草稿把内部字段改成对外、或重新核准某个字段，都不能让 v1 快照以旧状态回答。
        - 出处只引用发布快照来源 A（ID / 位置 / 文件名）；草稿换来源 B 不能绕过 A 被禁止使用
          的撤销，也不能把 A 的值记成 B。
        """
        version = self.get_published_version(tenant_id, product_id)
        if not version:
            return []
        try:
            snapshot = json.loads(version["snapshot_json"] or "{}")
        except ValueError:
            return []
        live = {row["field_key"]: row for row in self.get_fields(tenant_id, product_id)}
        sources = {row["id"]: row for row in self.get_sources(tenant_id, product_id)}
        revoked = self.knowledge_product_field_revocations(tenant_id, product_id)
        effective = []
        for field in snapshot.get("fields") or []:
            if not isinstance(field, dict):
                continue
            key = str(field.get("field_key") or "")
            row = live.get(key)
            if not key or row is None or key in revoked:
                continue
            if self._knowledge_snapshot_field_revocation(field, sources):
                continue
            source = sources.get(str(field.get("source_document_id") or "").strip())
            if source is None:
                continue
            effective.append({
                "id": row["id"],
                "field_key": row["field_key"],
                "label": row["label"],
                "value": str(field.get("value") or ""),  # 已发布快照值（不是当前候选/草稿值）
                "unit": str(field.get("unit") or ""),
                "source_document_id": source["id"],
                "source_location": str(field.get("source_location") or source["source_location"] or ""),
                "source_filename": source["filename"],
                "source": source["filename"] or str(field.get("source_location") or "") or "产品核准资料",
                "audience": "external",
            })
        return effective

    def knowledge_product_ai_gate(self, tenant_id: str, product_id: str, *, now: datetime | None = None) -> tuple[bool, str]:
        """清单11 的共享闸门：已发布 + 逐产品已授权 + 未到期 + 存在有效字段。

        网页问答、已开启渠道与问答测试页共用这一判定；模型调用之前与模型响应之后都要用它复核。
        """
        product = self.get_product(tenant_id, product_id)
        if not product:
            return False, "产品不存在"
        if not self.get_published_version(tenant_id, product_id):
            return False, "未发布"
        settings = self.knowledge_product_settings(tenant_id, product_id)
        if not settings["ai_authorized"]:
            return False, "未授权 AI 客服使用"
        expiry = knowledge_expiry_instant(settings["expires_at"])
        if expiry is not None and (now or datetime.now(timezone.utc)) >= expiry:
            return False, "AI 客服授权已到期"
        if not self.knowledge_product_fields(tenant_id, product_id):
            return False, "无对外已核准且有效的字段"
        return True, ""

    def set_knowledge_product_authorization(
        self, tenant_id: str, product_id: str, user_id: str, *,
        authorized: bool, expires_at=_UNSET, confirmed: bool = False,
    ) -> None:
        """逐产品 AI 客服授权 + 可选到期（只存侧车设置；产品生命周期与内容权威不变）。

        - 权限与知识编辑一致（管理员或产品管理模块授权）。
        - 对已发布产品「开启」授权需要一次显式确认（沿用 R16 口径）；关闭无需确认。
        - expires_at 留空 → 长期有效；无时区输入按界面标注的 Asia/Shanghai 解释并归一 UTC。
        """
        editor = self._require_knowledge_editor(tenant_id, user_id)
        product = self.get_product(tenant_id, product_id)
        if not product:
            raise ValueError("产品不存在")
        published = self.get_published_version(tenant_id, product_id)
        if authorized and published and not confirmed:
            raise ValueError("对已发布的产品开启 AI 客服授权需要确认")
        normalized = _UNSET if expires_at is _UNSET else normalize_knowledge_expiry(expires_at)
        timestamp = _now()
        existing = self.conn.execute(
            "SELECT * FROM knowledge_product_settings WHERE tenant_id=? AND product_id=?",
            (tenant_id, product_id),
        ).fetchone()
        if existing:
            if expires_at is _UNSET:
                self.conn.execute(
                    "UPDATE knowledge_product_settings SET ai_authorized=?, updated_by=?, updated_at=? "
                    "WHERE tenant_id=? AND product_id=?",
                    (1 if authorized else 0, editor["email"], timestamp, tenant_id, product_id),
                )
            else:
                self.conn.execute(
                    "UPDATE knowledge_product_settings SET ai_authorized=?, expires_at=?, updated_by=?, updated_at=? "
                    "WHERE tenant_id=? AND product_id=?",
                    (1 if authorized else 0, normalized, editor["email"], timestamp, tenant_id, product_id),
                )
        else:
            self.conn.execute(
                "INSERT INTO knowledge_product_settings(product_id, tenant_id, ai_authorized, expires_at, updated_by, updated_at, note) "
                "VALUES (?, ?, ?, ?, ?, ?, '')",
                (product_id, tenant_id, 1 if authorized else 0,
                 None if normalized is _UNSET else normalized, editor["email"], timestamp),
            )
        action = "ai_authorization_granted" if authorized else "ai_authorization_revoked"
        self._audit(tenant_id, "product", product_id, action, editor["email"],
                    "expires_at=" + ("unchanged" if normalized is _UNSET else str(normalized)))
        self.conn.commit()

    def _migrate_product_ai_consent(self) -> None:
        """一次性迁移（侧车表首次创建时）：已发布且存在对外已核准字段的产品按既有行为保持可用。

        只覆盖「迁移前已存在」的显式发布产品；此后新建产品不带自动授权（不做全局自动授权假设），
        必须在知识视图里由管理员显式开启。
        """
        rows = self.conn.execute(
            "SELECT p.tenant_id AS tenant_id, p.id AS product_id, v.snapshot_json AS snapshot_json "
            "FROM products p JOIN product_versions v ON v.product_id=p.id AND v.tenant_id=p.tenant_id AND v.status='published'"
        ).fetchall()
        timestamp = _now()
        migrated = 0
        for row in rows:
            try:
                fields = json.loads(row["snapshot_json"] or "{}").get("fields") or []
            except ValueError:
                fields = []
            eligible = [
                field for field in fields
                if isinstance(field, dict) and field.get("state") == "approved"
                and (field.get("audience") or "external") == "external"
                and str(field.get("value") or "").strip()
            ]
            if not eligible:
                continue
            cursor = self.conn.execute(
                "INSERT OR IGNORE INTO knowledge_product_settings(product_id, tenant_id, ai_authorized, expires_at, updated_by, updated_at, note) "
                "VALUES (?, ?, 1, NULL, 'migration', ?, '迁移：保留既有已发布产品的可用行为')",
                (row["product_id"], row["tenant_id"], timestamp),
            )
            migrated += cursor.rowcount
        if migrated:
            self.conn.commit()

    def list_knowledge_product_overview(self, tenant_id: str) -> list[dict]:
        """把原始产品适配为知识列表条目（只读投影，不写入、不删除、不重建产品）。"""
        overview = []
        for product in self.list_products(tenant_id):
            version = self.get_published_version(tenant_id, product["id"])
            available, reason = self.knowledge_product_ai_gate(tenant_id, product["id"])
            fields = self.knowledge_product_fields(tenant_id, product["id"])
            overview.append({
                "product_id": product["id"],
                "model": product["model"],
                "name": product["name"],
                "status": product["status"],
                "category": product["category"] or "",
                "published_version": version["version"] if version else None,
                "eligible_fields": len(fields),
                "available": available,
                "reason": "" if available else reason,
                "updated_at": product["updated_at"],
                "slug": product["slug"],
            })
        return overview

    def list_ai_answerable_product_facts(self, tenant_id: str, product_id: str | None = None) -> list[dict]:
        """产品/服务的客户视角候选（任务4 + 清单11）：只在共享闸门通过时给出；字段已按快照∩撤销过滤。"""
        facts = []
        for info in self.list_knowledge_product_overview(tenant_id):
            if product_id is not None and info["product_id"] != product_id:
                continue
            if not info["available"]:
                continue
            fields = self.knowledge_product_fields(tenant_id, info["product_id"])
            facts.append({
                "product_id": info["product_id"],
                "product_version": info["published_version"],
                "model": info["model"],
                "name": info["name"],
                "facts": [
                    {
                        "field_key": field["field_key"],
                        "label": field["label"],
                        "value": field["value"],
                        "unit": field["unit"],
                        "audience": "external",
                        "source": field["source"],
                        "source_document_id": field["source_document_id"],
                    }
                    for field in fields
                ],
            })
        return facts

    # ── 任务4：客户视角问答候选证据（模型之前的本地校验 + 模型之后的复核）────────────

    def list_customer_qa_evidence(self, tenant_id: str, *, now: datetime | None = None) -> list[dict]:
        """客户视角问答的候选证据：只含当前完全合格的资料（调用模型前本地校验）。

        - 文章 / FAQ / 产品与服务：已发布 + 对外 + 已授权 + 未过期 + 当前版本处理完成
          （复用 list_ai_answerable_knowledge）；当前版本的每个准备分块为一个证据单元（精确原文）。
        - 原产品字段：产品已发布且字段「已核准 + 对外 + 有值 + 出处可用」（复用
          list_ai_answerable_product_facts）；每个字段为一个证据单元。
        全部按 tenant_id 过滤（跨企业不可见）；到期排除语义与检索一致（now >= 到期即排除）。
        """
        evidence: list[dict] = []
        for row in self.list_ai_answerable_knowledge(tenant_id, now=now):
            snapshot = json.loads(row["content_json"] or "{}")
            title = str(snapshot.get("title") or "").strip()
            for chunk in self.list_knowledge_chunks(tenant_id, row["version_id"]):
                evidence.append({
                    "source": "knowledge",
                    "item_id": row["item_id"],
                    "form": row["form"],
                    "form_label": KNOWLEDGE_FORM_LABELS.get(row["form"], row["form"]),
                    "title": title,
                    "category": row["category"],
                    "version_id": row["version_id"],
                    "version_number": row["version_number"],
                    "published_at": row["published_at"],
                    "text": chunk["text"],
                })
        for bundle in self.list_ai_answerable_product_facts(tenant_id):
            for fact in bundle["facts"]:
                text = f"{fact['label']}：{fact['value']}"
                if fact["unit"]:
                    text += f" {fact['unit']}"
                evidence.append({
                    "source": "product_field",
                    "product_id": bundle["product_id"],
                    "model": bundle["model"],
                    "name": bundle["name"],
                    "version_number": bundle["product_version"],
                    "field_key": fact["field_key"],
                    "label": fact["label"],
                    "value": fact["value"],
                    "unit": fact["unit"],
                    "fact_source": fact["source"],
                    "fact_source_id": fact.get("source_document_id"),
                    "text": text,
                })
        # 第二批钩子（任务5）：主题逐段过滤 + 灵活字段闸门（均先过各自资格再进入候选）。
        topic_hook = getattr(self, "kb2_list_topic_qa_evidence", None)
        if topic_hook is not None:
            evidence.extend(topic_hook(tenant_id, now=now))
        field_hook = getattr(self, "kb2_list_item_field_qa_evidence", None)
        if field_hook is not None:
            evidence.extend(field_hook(tenant_id, now=now))
        return evidence

    def knowledge_evidence_valid_after_model(
        self, tenant_id: str, evidence: dict, *, now: datetime | None = None
    ) -> tuple[bool, str]:
        """模型响应后的复核：所选证据是否仍是当前完全合格的资料。

        防并发变化：生成期间停用 / 撤销授权 / 到期 / 发布新版（换版）/ 字段核准或出处变更，
        任何一项发生都返回 False，由调用方按资料不足处理（绝不输出已失效内容）。
        """
        if evidence.get("source") == "knowledge":
            rows = [
                row for row in self.list_ai_answerable_knowledge(tenant_id, evidence.get("item_id"), now=now)
                if row["version_id"] == evidence.get("version_id")
            ]
            if not rows:
                return False, "该知识已停用、撤销授权、到期或被新版本替换（生成期间发生变化）"
            texts = {chunk["text"] for chunk in self.list_knowledge_chunks(tenant_id, evidence["version_id"])}
            if evidence.get("text") not in texts:
                return False, "该知识的当前版本内容已变化"
            return True, ""
        if evidence.get("source") == "product_field":
            for bundle in self.list_ai_answerable_product_facts(tenant_id, evidence.get("product_id")):
                if bundle["product_version"] != evidence.get("version_number"):
                    continue
                for fact in bundle["facts"]:
                    if (fact["field_key"] == evidence.get("field_key")
                            and fact["value"] == evidence.get("value")
                            and fact["unit"] == evidence.get("unit")
                            and fact.get("source_document_id") == evidence.get("fact_source_id")):
                        return True, ""
            return False, "该产品字段的核准、使用范围、出处或版本已变化（生成期间发生变化）"
        if evidence.get("source") in ("topic", "item_field"):
            # 第二批钩子（任务5）：主题段 / 灵活字段的生成后复核。
            hook = getattr(self, "kb2_evidence_valid_after_model", None)
            if hook is not None:
                return hook(tenant_id, evidence, now=now)
            return False, "该证据类型未启用（生成期间发生变化）"
        return False, "未知证据来源"

    # ── 任务2/4：真实本地内容准备（投影 + 分块；不用计时、也不无条件标记完成）────────

    def prepare_knowledge_version(self, tenant_id: str, version_id: str) -> int:
        """对指定版本执行真实本地准备：生成文本投影与分块，完成后才把处理状态标为 completed。

        没有可投影内容时记为 failed 并抛出错误——处理状态永远不无条件完成。
        """
        version = self.get_knowledge_version(tenant_id, version_id)
        if not version:
            raise ValueError("知识版本不存在")
        item = self.get_knowledge_item(tenant_id, version["item_id"])
        if not item:
            raise ValueError("知识不存在")
        snapshot = json.loads(version["content_json"] or "{}")
        projection = knowledge_content_projection(item["form"], snapshot)
        title = str(snapshot.get("title") or "").strip()
        substantive = projection.strip()
        if title and substantive == title:
            substantive = ""  # 只有标题、没有可核对正文时不构成真实准备
        if not substantive:
            self._upsert_processing(tenant_id, version_id, "failed", "内容为空，无法生成知识投影")
            self.conn.commit()
            raise ValueError("内容为空，无法准备：请先补充正文")
        chunks = _split_projection_chunks(projection)
        self.conn.execute("DELETE FROM knowledge_chunks WHERE version_id=?", (version_id,))
        timestamp = _now()
        for ordinal, text in enumerate(chunks):
            self.conn.execute(
                "INSERT INTO knowledge_chunks(version_id, tenant_id, item_id, ordinal, text, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (version_id, tenant_id, item["id"], ordinal, text, timestamp),
            )
        self._upsert_processing(tenant_id, version_id, "completed", "")
        self.conn.commit()
        return len(chunks)

    def list_knowledge_chunks(self, tenant_id: str, version_id: str) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM knowledge_chunks WHERE tenant_id=? AND version_id=? ORDER BY ordinal",
            (tenant_id, version_id),
        ).fetchall()
