"""企业知识第二批 · 正式界面与 HTTP 接入（任务3）。

设计依据：docs/企业知识管理-PRD与交互设计-v0.3-主题与资料评审稿.md（§5 导航与页面、§6 三个主流程、
§10 生命周期与 AI 可用分离）与设计基准提交 cd5f3e9 的可点击原型
`design/enterprise-knowledge-topic-prototype/`（静态样式与交互按原型移植，演示数据全部替换为
任务1/2 的真实服务接口；不复制原型的内存数据库、模拟按钮、假识别或假问答）。

约定：
- 本模块只处理新的第二批路径（/admin/knowledge 全部主题、?view=materials 全部资料、
  /admin/knowledge/add 添加资料、/admin/knowledge/topic/*、/admin/knowledge/batch/*、
  /admin/knowledge/material/*）；第一批的详情 / 产品 / 问答 / 旧列表视图保持兼容，
  遇到这些路径返回 False 交给既有 KnowledgeUI。
- GET 一律不变更数据；所有写入走 POST，并逐次经服务层检查「管理员或 products 模块授权」，
  跨企业一律拒绝（服务层强制，界面隐藏入口不作为权限）。
- 原件下载为受认证的企业内下载（带企业归属校验与 sha256 校验、审计落盘），不提供公开地址。
- 静态资源 static/knowledge-phase2.css、static/knowledge-phase2.js 由本模块
  /admin/knowledge/assets/* 提供（no-cache），页面只引用这两个文件。
"""
from __future__ import annotations

import json
import re
from urllib.parse import quote, urlencode

KB2_UI_VERSION = "20261013-task3b"
KB2_UI_PREFIX = "/admin/knowledge"
KB2_OLD_LIST_VIEWS = {"all", "product_service", "article_faq", "files"}
KB2_STATUS_PILL = {
    "draft": ("draft", "草稿"),
    "disabled": ("off", "已停用"),
}

# 附加线稿图标（原型使用；与既有 sprite 的 i-* 前缀区分，避免冲突）
KB2_ICONS = """<svg width="0" height="0" style="position:absolute" aria-hidden="true"><defs>
<symbol id="kb2-chev" viewBox="0 0 24 24"><path d="m6 9 6 6 6-6"/></symbol>
<symbol id="kb2-x" viewBox="0 0 24 24"><path d="M18 6 6 18"/><path d="M6 6l12 12"/></symbol>
<symbol id="kb2-back" viewBox="0 0 24 24"><path d="M19 12H5"/><path d="m12 19-7-7 7-7"/></symbol>
<symbol id="kb2-image" viewBox="0 0 24 24"><rect x="3" y="3" width="18" height="18" rx="2"/><circle cx="9" cy="9" r="2"/><path d="m21 15-3.09-3.09a2 2 0 0 0-2.82 0L6 21"/></symbol>
<symbol id="kb2-video" viewBox="0 0 24 24"><path d="m16 13 5.24 3.5a.5.5 0 0 0 .76-.43V7.93a.5.5 0 0 0-.76-.43L16 11"/><rect x="2" y="6" width="14" height="12" rx="2"/></symbol>
<symbol id="kb2-pen" viewBox="0 0 24 24"><path d="M12 20h9"/><path d="M16.5 3.5a2.12 2.12 0 0 1 3 3L7 19l-4 1 1-4Z"/></symbol>
<symbol id="kb2-upload" viewBox="0 0 24 24"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="17 8 12 3 7 8"/><path d="M12 3v12"/></symbol>
<symbol id="kb2-link" viewBox="0 0 24 24"><path d="M10 13a5 5 0 0 0 7.54.54l3-3a5 5 0 0 0-7.07-7.07l-1.72 1.71"/><path d="M14 11a5 5 0 0 0-7.54-.54l-3 3a5 5 0 0 0 7.07 7.07l1.71-1.71"/></symbol>
<symbol id="kb2-folder" viewBox="0 0 24 24"><path d="M20 20a2 2 0 0 0 2-2V8a2 2 0 0 0-2-2h-7.9a2 2 0 0 1-1.69-.9L9.6 3.9A2 2 0 0 0 7.93 3H4a2 2 0 0 0-2 2v13a2 2 0 0 0 2 2Z"/></symbol>
<symbol id="kb2-check" viewBox="0 0 24 24"><circle cx="12" cy="12" r="10"/><path d="m16 9-5.5 5.5L8 12"/></symbol>
<symbol id="kb2-alert" viewBox="0 0 24 24"><path d="m21.73 18-8-14a2 2 0 0 0-3.48 0l-8 14A2 2 0 0 0 4 21h16a2 2 0 0 0 1.73-3"/><path d="M12 9v4"/><path d="M12 17h.01"/></symbol>
</defs></svg>"""

_FORMAT_ICON = {
    "pdf": "i-file", "docx": "i-file", "doc": "i-file", "txt": "i-file", "md": "i-file",
    "csv": "i-file", "image": "kb2-image", "video": "kb2-video", "web": "i-globe",
    "manual": "kb2-pen", "other": "i-file",
}
_FORMAT_LABELS = {
    "pdf": "PDF", "docx": "DOCX", "doc": "DOC", "txt": "TXT", "md": "Markdown", "csv": "CSV",
    "image": "图片", "video": "视频", "web": "网页", "manual": "手动文字", "other": "其他",
}
_SUGGESTION_BASIS_LABELS = {
    "text_match": "现有主题文字命中",
    "source_title": "依据资料标题",
    "source_heading": "依据资料标题段落",
    "member": "成员添加",
}
_IDENTIFICATION_PILL_CSS = {
    "extracted": "draft", "manual": "draft", "registered": "pub",
    "failed": "off", "processing": "draft", "pending": "draft",
}


# ── 通用帮助 ─────────────────────────────────────────────────────────────

def _esc(value) -> str:
    from app import esc
    return esc(value)


def _ic(name: str) -> str:
    return f"<svg class='ic' aria-hidden='true'><use href='#{name}'></use></svg>"


def _q1(query, key, default=""):
    """query / form 的取值帮助（二者形态不同：query 为 list，form 为标量）。"""
    if key not in query:
        return default
    value = query[key]
    if isinstance(value, list):
        value = value[-1] if value else default
    if isinstance(value, bytes):
        value = value.decode("utf-8", errors="replace")
    return value


def _assets() -> str:
    return (
        f"<link rel='stylesheet' href='{KB2_UI_PREFIX}/assets/knowledge-phase2.css?v={KB2_UI_VERSION}'>"
        f"<script src='{KB2_UI_PREFIX}/assets/knowledge-phase2.js?v={KB2_UI_VERSION}' defer></script>"
        f"{KB2_ICONS}"
    )


def _safe_next(value, default: str) -> str:
    """返回地址只接受站内 /admin/knowledge 路径（不依赖浏览器历史）。"""
    target = str(value or "").strip()
    if target.startswith(KB2_UI_PREFIX) and not target.startswith("//"):
        return target
    return default


def _qs(base: str, **params) -> str:
    """在 base 路径上叠加/覆盖查询参数；None 移除参数（保留其余筛选，供返回与清空使用）。"""
    from urllib.parse import parse_qsl, urlsplit, urlunsplit
    parts = urlsplit(base)
    merged = dict(parse_qsl(parts.query, keep_blank_values=False))
    for key, value in params.items():
        if value is None:
            merged.pop(key, None)
        else:
            merged[key] = value
    query = urlencode(merged)
    return urlunsplit(("", "", parts.path, query, ""))


def _notice_html(query) -> str:
    notice = str(_q1(query, "notice") or "")
    error = str(_q1(query, "error") or "")
    out = ""
    if notice:
        out += f"<div class='notice' role='status'>{_ic('kb2-check')}<span>{_esc(notice)}</span></div>"
    if error:
        out += f"<div class='form-alert' role='alert'>{_ic('kb2-alert')}<span>{_esc(error)}</span></div>"
    return out


def _redirect(handler, path: str, notice: str = "", error: str = "") -> None:
    params = {}
    if notice:
        params["notice"] = notice
    if error:
        params["error"] = error
    target = path if not params else _qs(path, **params)
    handler.redirect(target)


def _ok(handler, body: str, active: str, title: str) -> None:
    handler.send_html(handler.console_page(title, _assets() + f"<div class='kb2'>{body}</div>", active))


def _editor(handler):
    """当前请求的编辑身份（管理员或获 products 授权的成员）；否则 None。"""
    return handler.db._knowledge_editor(handler.tenant_id, handler.user_id)


# ── 主题 / 文件浏览树（工作区内部左列；桌面与窄屏共用一份数据）──────────────

def _tree_data(handler):
    db = handler.db
    tenant = handler.tenant_id
    groups = db.kb2_list_groups(tenant)
    topics = db.kb2_list_topics(tenant)
    materials = {(m["kind"], m["id"]): m for m in db.kb2_list_materials(tenant)}
    formats = {}
    source_by_item = {}
    for row in db.conn.execute(
        "SELECT id, item_id, format FROM kb2_sources WHERE tenant_id=? AND item_id IS NOT NULL "
        "ORDER BY created_at, id", (tenant,),
    ):
        formats.setdefault(row["item_id"], row["format"])
        source_by_item.setdefault(row["item_id"], row["id"])
    files_by_topic: dict[str, list] = {}
    for relation in db.conn.execute(
        "SELECT * FROM kb2_topic_relations WHERE tenant_id=? ORDER BY linked_at, id", (tenant,),
    ):
        material = materials.get((relation["material_kind"], relation["material_id"]))
        if material is None or relation["material_kind"] != "knowledge":
            continue
        files_by_topic.setdefault(relation["topic_id"], []).append({
            "item_id": relation["material_id"],
            "title": material["title"],
            "format": formats.get(relation["material_id"], ""),
            "source_id": source_by_item.get(relation["material_id"]),
        })
    return {"groups": groups, "topics": topics, "files_by_topic": files_by_topic}


def _tree_html(tree, *, active_topic="", active_source="", pfx="") -> str:
    """分组 → 主题 → 关联文件；每个主题一个独立折叠按钮（带数量、只开合文件列表，不导航）。"""
    out = ["<div class='ts-head'>主题</div>"]

    def topic_item(topic):
        name = topic["title"]
        files = tree["files_by_topic"].get(topic["id"], [])
        active = " active" if topic["id"] == active_topic else ""
        draft_badge = "<span class='draft-mini'>草稿</span>" if topic["status"] == "draft" else (
            "<span class='draft-mini'>已停用</span>" if topic["status"] == "disabled" else "")
        item = [
            "<div class='ts-topic-row'>",
            f"<a class='ts-link ts-item{active}' href='{KB2_UI_PREFIX}/topic/{topic['id']}'>",
            "<span class='ts-dot'></span>",
            f"<span class='grow nav-topic-name' title='{_esc(name)}'>{_esc(name)}</span>{draft_badge}</a>",
            f"<button class='ts-filesbtn' type='button' data-kb2='nav-topic-files' data-tid='{topic['id']}' "
            f"id='{pfx}nav-filesbtn-{topic['id']}' aria-expanded='false' aria-controls='{pfx}nav-files-{topic['id']}' "
            f"aria-label='展开或收起「{_esc(name)}」的关联文件（{len(files)}）'>"
            f"{_ic('kb2-chev')}<span class='ts-filesbtn-n'>{len(files)}</span></button></div>",
            f"<div class='ts-files' id='{pfx}nav-files-{topic['id']}' inert><div class='ts-files-inner'>",
        ]
        if files:
            for entry in files:
                href = (f"{KB2_UI_PREFIX}/material/{entry['source_id']}?from=topic:{topic['id']}"
                        if entry["source_id"] else f"{KB2_UI_PREFIX}/item/{entry['item_id']}")
                selected = " active" if entry["source_id"] and entry["source_id"] == active_source else ""
                icon = _FORMAT_ICON.get(entry["format"], "i-file")
                item.append(
                    f"<a class='ts-file{selected}' href='{href}' title='{_esc(entry['title'])}'>"
                    f"{_ic(icon)}<span class='ts-file-name'>{_esc(entry['title'])}</span></a>")
        else:
            item.append("<p class='ts-none'>无关联资料</p>")
        item.append("</div></div>")
        return "".join(item)

    ungrouped = [topic for topic in tree["topics"] if not topic["group_id"]]
    for group in tree["groups"]:
        kids = [topic for topic in tree["topics"] if topic["group_id"] == group["id"]]
        out.append(
            f"<button class='ts-link ts-grp' type='button' id='{pfx}nav-grpbtn-{group['id']}' "
            f"data-kb2='nav-group' data-gid='{group['id']}' aria-expanded='false' aria-controls='{pfx}nav-sub-{group['id']}'>"
            f"{_ic('i-folder')}<span class='grow'>{_esc(group['name'])}</span>"
            f"<span class='ts-count'>{len(kids)}</span>{_ic('kb2-chev')}</button>"
            f"<div class='ts-subs' id='{pfx}nav-sub-{group['id']}' inert><div class='ts-subs-inner'>"
            + "".join(topic_item(topic) for topic in kids) + "</div></div>")
    for topic in ungrouped:
        out.append(topic_item(topic))
    return "".join(out)


def _workspace(body: str, tree, *, active_topic="", active_source="", wide=False) -> str:
    """工作区：内部主题/文件浏览列（左，窄约 216px）+ 中央内容；窄屏折叠为「浏览主题」。"""
    return (
        "<div class='workspace-grid'>"
        f"<aside class='topic-side' aria-label='主题浏览导航'><div class='topic-side-inner'>{_tree_html(tree, active_topic=active_topic, active_source=active_source, pfx='')}</div></aside>"
        "<div class='ws-main'>"
        "<details class='topic-side-details' id='topic-browse-details'><summary>浏览主题</summary>"
        f"<div class='topic-side-inner'>{_tree_html(tree, active_topic=active_topic, active_source=active_source, pfx='m')}</div></details>"
        f"{body}</div></div>"
    )


# ── 全部主题（默认入口）───────────────────────────────────────────────────

def _topic_versions_map(handler) -> dict:
    out: dict[str, dict] = {}
    for row in handler.db.conn.execute(
        "SELECT * FROM kb2_topic_versions WHERE tenant_id=? ORDER BY version", (handler.tenant_id,),
    ):
        entry = out.setdefault(row["topic_id"], {})
        entry[row["status"]] = row
    return out


def _topic_state(handler, topic: dict) -> dict:
    """主题双标签状态：已发布（AI 客服可用）绿 / 已发布（AI 客服不可用）蓝灰 + 原因。

    任务5：AI 可用性由真实资格计算（主题自身 + 逐段来源过滤），不只看开关，也不按计时。
    """
    if topic.get("status") == "disabled":
        return {"css": "off", "label": "已停用", "reason": "历史版本与关联保留", "eligible": 0, "total": 0}
    if topic.get("status") != "published":
        return {"css": "draft", "label": "草稿", "reason": "", "eligible": 0, "total": 0}
    state = handler.db.kb2_topic_ai_state(handler.tenant_id, topic["id"])
    eligible = state.get("eligible_paragraphs", 0)
    total = state.get("total_paragraphs", 0)
    if state.get("available"):
        return {"css": "ok", "label": "已发布（AI 客服可用）",
                "reason": (f"合格段落 {eligible}/{total}" if total and eligible < total else ""),
                "eligible": eligible, "total": total}
    return {"css": "pub", "label": "已发布（AI 客服不可用）", "reason": state.get("reason", ""),
            "eligible": eligible, "total": total}


def _topics_search_blob(handler, topic_id: str) -> str:
    parts = []
    for row in handler.db.conn.execute(
        "SELECT content_json FROM kb2_topic_versions WHERE tenant_id=? AND topic_id=?",
        (handler.tenant_id, topic_id),
    ):
        try:
            content = json.loads(row["content_json"] or "{}")
        except ValueError:
            content = {}
        parts.append(str(content.get("title") or ""))
        for paragraph in content.get("paragraphs") or []:
            if isinstance(paragraph, dict):
                parts.append(str(paragraph.get("text") or ""))
    return " ".join(parts).lower()


def topics_page(handler, query) -> None:
    db = handler.db
    tenant = handler.tenant_id
    editor = _editor(handler) is not None
    tree = _tree_data(handler)
    q = str(_q1(query, "q") or "").strip()
    manage = str(_q1(query, "manage") or "") == "1" and editor

    def match(topic):
        if not q:
            return True
        query_text = q.lower()
        if query_text in topic["title"].lower():
            return True
        return query_text in _topics_search_blob(handler, topic["id"])

    def topic_row_html(topic):
        state = _topic_state(handler, topic)
        meta = [f"相关资料 {topic['materials_count']}"]
        if topic["registered_only_count"]:
            meta.append(f"其中仅登记 {topic['registered_only_count']}")
        meta.append(str(topic["updated_at"])[:10])
        side = f"<span class='pill {state['css']}'>{_esc(state['label'])}</span>"
        if state["reason"]:
            side += f"<span class='muted'>{_esc(state['reason'])}</span>"
        move = ""
        if manage:
            options = ["<option value=''>未分组</option>"] + [
                f"<option value='{group['id']}'{' selected' if topic['group_id'] == group['id'] else ''}>{_esc(group['name'])}</option>"
                for group in tree["groups"]]
            move = (
                f"<form method='post' action='{KB2_UI_PREFIX}/topic/{topic['id']}/move' class='mng-controls'>"
                f"<input type='hidden' name='next' value='{_esc(_qs(KB2_UI_PREFIX, manage=1, q=q or None))}'>"
                f"<select class='move-select' name='group_id' aria-label='把主题移动到分组'>{''.join(options)}</select>"
                f"<button class='linkbtn'>移动</button></form>")
        return (
            f"<div class='row'><a class='row-main' href='{KB2_UI_PREFIX}/topic/{topic['id']}'>"
            f"<span class='row-title'><strong>{_esc(topic['title'])}</strong></span>"
            f"<span class='row-meta'>{_esc(' · '.join(meta))}</span></a>"
            f"<div class='row-side'>{side}{move}</div></div>")

    def section_html(gid, name, topics, *, folder=False, group_row=None):
        shown = [topic for topic in topics if match(topic)]
        if q and not shown:
            return ""
        rows = "".join(topic_row_html(topic) for topic in shown)
        controls = ""
        if manage and group_row is not None:
            controls = (
                "<span class='mng-controls'>"
                f"<details class='more-wrap'><summary class='linkbtn'>改名</summary>"
                f"<div class='more-menu'><form method='post' action='{KB2_UI_PREFIX}/group/rename'>"
                f"<input type='hidden' name='group_id' value='{group_row['id']}'>"
                f"<input type='hidden' name='next' value='{_esc(_qs(KB2_UI_PREFIX, manage=1, q=q or None))}'>"
                f"<input name='name' value='{_esc(group_row['name'])}' aria-label='分组名称' required>"
                "<p><button class='button secondary'>保存分组名</button></p></form></div></details>"
                f"<form method='post' action='{KB2_UI_PREFIX}/group/delete' data-kb2-confirm='删除分组「{_esc(group_row['name'])}」？分组只会被删除，主题与关联保留为未分组。' data-kb2-confirm-title='删除分组'>"
                f"<input type='hidden' name='group_id' value='{group_row['id']}'>"
                f"<input type='hidden' name='next' value='{_esc(_qs(KB2_UI_PREFIX, manage=1, q=q or None))}'>"
                "<button class='linkbtn danger-link'>删除分组</button></form></span>")
        icon = _ic("i-folder") if folder else ""
        empty_rows = "<p class='muted'>（空）</p>"
        return (
            f"<section class='topic-sec'><div class='topic-sec-head'>"
            f"<h2><button class='sec-fold' type='button' data-kb2='ov-fold' data-gid='{gid}' id='ov-fold-btn-{gid}' "
            f"aria-expanded='true' aria-controls='ov-body-{gid}'>{icon}{_esc(name)}"
            f"<span class='topic-count'>{len(topics)} 个主题</span>{_ic('kb2-chev')}</button></h2>{controls}</div>"
            f"<div class='sec-body open' id='ov-body-{gid}'><div class='sec-body-inner'><div class='rows'>"
            f"{rows or empty_rows}</div></div></div></section>")

    sections = []
    for group in tree["groups"]:
        topics = [topic for topic in tree["topics"] if topic["group_id"] == group["id"]]
        sections.append(section_html(f"g-{group['id']}", group["name"], topics, folder=True, group_row=group))
    ungrouped = [topic for topic in tree["topics"] if not topic["group_id"]]
    if ungrouped:
        sections.append(section_html("ungrouped", "未分组", ungrouped))
    body_region = "".join(sections)
    if q and not any(match(topic) for topic in tree["topics"]):
        body_region = (
            "<div class='empty'><h2>没有找到匹配的主题</h2>"
            "<p>按搜索框里的「×」清空关键词，或换个词再试。</p>"
            f"<p><a class='button secondary' href='{_esc(_qs(KB2_UI_PREFIX, q=None, manage=(1 if manage else None)))}' data-kb2='search-clear-link'>清空搜索</a></p></div>")
    if not tree["topics"]:
        body_region = (
            "<div class='empty'><h2>还没有主题</h2>"
            "<p>可以添加资料让系统建议主题，或直接新建主题。</p>"
            "<p class='actions'>"
            + (f"<a class='button' href='{KB2_UI_PREFIX}/add'>{_ic('kb2-upload')}添加资料</a>" if editor else "")
            + f"<a class='button secondary' href='{KB2_UI_PREFIX}/topic/new'>{_ic('i-plus')}新建主题</a></p></div>")

    actions = ""
    if editor:
        manage_href = _qs(KB2_UI_PREFIX, manage=(None if manage else 1), q=q or None)
        actions = (
            "<div class='actions'>"
            f"<a class='button secondary' href='{_esc(_qs(KB2_UI_PREFIX, manage=1, q=q or None)) if not manage else _esc(manage_href)}'>"
            f"{'完成分组管理' if manage else '管理分组'}</a>"
            f"<a class='button' href='{KB2_UI_PREFIX}/topic/new'>{_ic('i-plus')}新建主题</a>"
            f"<a class='button secondary' href='{KB2_UI_PREFIX}/add'>{_ic('kb2-upload')}添加资料</a></div>")
    toolbar = (
        "<div class='toolbar'><div><h1>全部主题</h1>"
        "<p class='lead'>按主题查看企业知识。先看正文与要点，需要时再从主题浏览列表打开原始资料。</p></div>"
        f"{actions}</div>")
    if not editor:
        toolbar += (
            "<div class='readonly-note'>" + _ic("kb2-alert") +
            "<span>只读：你可以阅读、搜索、返回与跳转；新建主题、分组、关联与发布需要管理员或产品管理模块权限。</span></div>")
    search = (
        "<div class='list-toolbar'><form method='get' action='"
        + _esc(_qs(KB2_UI_PREFIX, manage=(1 if manage else None)))
        + "' class='search-wrap'>"
        f"{_ic('i-search')}<input id='kb2-search' type='search' name='q' value='{_esc(q)}' "
        f"placeholder='搜索主题或正文关键词' aria-label='搜索主题'>"
        f"<a class='search-clear' id='kb2-search-clear' href='{_esc(_qs(KB2_UI_PREFIX, q=None, manage=(1 if manage else None)))}' "
        f"aria-label='清空搜索关键词' data-kb2='search-clear'"
        + ("" if q else " hidden") + f">{_ic('kb2-x')}</a></form></div>")
    if manage:
        search += (
            f"<div class='info-note'>{_ic('i-folder')}<span>分组只用于导航；可新建分组、改名或删除；"
            "删除分组不会删除主题。移动主题用主题行上的「移动…」。</span></div>"
            f"<div class='batch-bar'><form method='post' action='{KB2_UI_PREFIX}/group/new'>"
            f"<input type='hidden' name='next' value='{_esc(_qs(KB2_UI_PREFIX, manage=1, q=q or None))}'>"
            "<label for='kb2-group-name'>新建分组</label>"
            "<input id='kb2-group-name' name='name' placeholder='例如：售后与安装' required>"
            "<button class='button secondary'>新建分组</button></form></div>")
    total, shown_total = len(tree["topics"]), len([topic for topic in tree["topics"] if match(topic)])
    count = f"<div class='list-count'>共 {total} 个主题" + (f"，显示 {shown_total} 个" if q else "") + "</div>"
    _ok(handler, _workspace(_notice_html(query) + toolbar + search + body_region + count, tree), "knowledge", "全部主题")


# ── 全部资料（跨主题全集；旧产品/文章/FAQ 不自动分配主题）────────────────────

def _material_pill(handler, material) -> str:
    """生命周期双标签（与第一批口径一致）：已发布可用=绿、不可用=蓝灰+原因；草稿/停用用生命周期标签。"""
    if material["kind"] != "knowledge":
        available = bool(material.get("available"))
        css = "ok" if available else "pub"
        label = "已发布（AI 客服可用）" if available else "已发布（AI 客服不可用）"
        reason = material.get("reason") or ""
        extra = f"<span class='muted'>{_esc(reason)}</span>" if reason and not available else ""
        return f"<span class='pill {css}'>{_esc(label)}</span>{extra}"
    try:
        state = handler.db.knowledge_item_ui_state(handler.tenant_id, material["id"])
    except ValueError:
        return ""
    if state["status"] == "published":
        available = state["available"]
        reason = state["reason"] or ""
        if material.get("registered_only"):
            available, reason = False, "仅登记原件（没有可核对正文，不供 AI 使用）"
        css = "ok" if available else "pub"
        label = "已发布（AI 客服可用）" if available else "已发布（AI 客服不可用）"
        extra = f"<span class='muted'>{_esc(reason)}</span>" if not available and reason else (
            "<span class='muted'>有草稿修改待发布</span>" if state.get("has_draft") else "")
        return f"<span class='pill {css}'>{_esc(label)}</span>{extra}"
    css, label = KB2_STATUS_PILL.get(state["status"], ("draft", "草稿"))
    return f"<span class='pill {css}'>{_esc(label)}</span>"


def materials_page(handler, query) -> None:
    db = handler.db
    materials = db.kb2_list_materials(handler.tenant_id)
    topics = {topic["id"]: topic for topic in db.kb2_list_topics(handler.tenant_id)}
    q = str(_q1(query, "q") or "").strip().lower()
    form_filter = str(_q1(query, "form") or "")
    topic_filter = str(_q1(query, "topic") or "")
    status_filter = str(_q1(query, "status") or "")
    # 实际入口返回（必需返回行为）：行内链接携带「当前全集 URL（含筛选）」作为受校验的返回目标。
    list_url = _qs(KB2_UI_PREFIX, view="materials", q=str(_q1(query, "q") or "").strip() or None,
                   form=form_filter or None, topic=topic_filter or None, status=status_filter or None)

    def matches(material):
        if form_filter and (material.get("display_form") or material["form_label"]) != form_filter:
            return False
        if topic_filter == "none":
            if material["topics"]:
                return False
        elif topic_filter and not any(topic["id"] == topic_filter for topic in material["topics"]):
            return False
        state = material["status"]
        if status_filter:
            if status_filter == "published" and state != "published":
                return False
            if status_filter == "draft" and state != "draft":
                return False
            if status_filter == "disabled" and state != "disabled":
                return False
        if q:
            names = " ".join(topic["title"] for topic in material["topics"])
            haystack = f"{material['title']} {material.get('model') or ''} {names}".lower()
            if material["kind"] == "knowledge":
                item = db.get_knowledge_item(handler.tenant_id, material["id"])
                if item:
                    version = db.get_knowledge_version(
                        handler.tenant_id, item["published_version_id"] or item["draft_version_id"])
                    if version:
                        haystack += " " + str(version["content_json"]).lower()
            if q not in haystack:
                return False
        return True

    rows = []
    for material in sorted(materials, key=lambda m: (m["kind"] != "knowledge", str(m["updated_at"]))):
        if not matches(material):
            continue
        topic_chips = "".join(
            f"<a class='tag type' href='{KB2_UI_PREFIX}/topic/{topic['id']}'>{_esc(topic['title'])}</a>"
            for topic in material["topics"]
        ) or "<span class='muted'>未关联主题</span>"
        # 统一资料详情：有登记原件（第二批入库/替换）的资料进入统一资料页；旧资料保持旧地址兼容。
        if material["kind"] == "product":
            href = f"{KB2_UI_PREFIX}/product/{material['id']}"
        elif material.get("source_id"):
            # from=materials + back=当前全集 URL：详情页「返回」恢复实际入口与全部筛选。
            href = (f"{KB2_UI_PREFIX}/material/{material['source_id']}?"
                    + urlencode({"from": "materials", "back": list_url}))
        else:
            href = f"{KB2_UI_PREFIX}/item/{material['id']}"
        registered_tag = ("<span class='tag edit'>仅登记原件</span>"
                          if material.get("registered_only") else "")
        rows.append(
            f"<div class='row'><a class='row-main' href='{href}'>"
            f"<span class='row-title'><strong>{_esc(material['title'])}</strong>"
            f"<span class='tag type'>{_esc(material.get('display_form') or material['form_label'])}</span>"
            f"{registered_tag}</span>"
            f"<span class='row-meta'>{topic_chips} · 更新 {_esc(str(material['updated_at'])[:10])}</span></a>"
            f"<div class='row-side'>{_material_pill(handler, material)}</div></div>")
    form_options = sorted({(material.get("display_form") or material["form_label"]) for material in materials})
    topic_options = "".join(
        f"<option value='{topic['id']}'{' selected' if topic_filter == topic['id'] else ''}>{_esc(topic['title'])}</option>"
        for topic in sorted(topics.values(), key=lambda t: t["title"]))
    base = _qs(KB2_UI_PREFIX, view="materials")
    readonly = "" if _editor(handler) is not None else (
        "<div class='readonly-note'>" + _ic("kb2-alert") +
        "<span>只读：你可以查找、打开与返回；核对、编辑与发布需要管理员或产品管理模块权限。</span></div>")
    # 页面身份与筛选保留（B2-B06）：GET 表单必须携带 view=materials；清空关键词只清 q、保留筛选。
    clear_link = _qs(KB2_UI_PREFIX, view="materials", form=form_filter or None,
                     topic=topic_filter or None, status=status_filter or None)
    body = (
        _notice_html(query)
        + "<div class='toolbar'><div><h1>全部资料</h1>"
        "<p class='lead'>统一入口找资料：形式、主题、状态各自独立筛选。登记了原件 ≠ 识别出正文 ≠ 可用于 AI。</p></div>"
        f"<div class='actions'><a class='button secondary' href='{KB2_UI_PREFIX}'>全部主题</a></div></div>"
        + readonly
        + f"<div class='list-toolbar'><form method='get' action='{_esc(KB2_UI_PREFIX)}' class='search-wrap'>"
        f"<input type='hidden' name='view' value='materials'>"
        + (f"<input type='hidden' name='form' value='{_esc(form_filter)}'>" if form_filter else "")
        + (f"<input type='hidden' name='topic' value='{_esc(topic_filter)}'>" if topic_filter else "")
        + (f"<input type='hidden' name='status' value='{_esc(status_filter)}'>" if status_filter else "")
        + f"{_ic('i-search')}<input id='kb2-search' type='search' name='q' value='{_esc(str(_q1(query, 'q') or ''))}' placeholder='搜索名称、正文或主题' aria-label='搜索资料'>"
        f"<a class='search-clear' id='kb2-search-clear' href='{_esc(clear_link)}' aria-label='清空搜索关键词' data-kb2='search-clear'"
        + ("" if str(_q1(query, 'q') or '') else " hidden") + f">{_ic('kb2-x')}</a></form>"
        f"<form method='get' action='{_esc(KB2_UI_PREFIX)}' class='list-toolbar'>"
        f"<input type='hidden' name='view' value='materials'>"
        f"<input type='hidden' name='q' value='{_esc(str(_q1(query, 'q') or ''))}'>"
        f"<select class='filter-select' name='form' aria-label='按形式筛选'><option value=''>全部形式</option>"
        + "".join(f"<option value='{_esc(label)}'{' selected' if form_filter == label else ''}>{_esc(label)}</option>" for label in form_options)
        + "</select>"
        f"<select class='filter-select' name='topic' aria-label='按主题筛选'><option value=''>全部主题</option>"
        f"<option value='none'{' selected' if topic_filter == 'none' else ''}>未关联主题</option>"
        f"{topic_options}</select>"
        f"<select class='filter-select' name='status' aria-label='按状态筛选'><option value=''>全部状态</option>"
        + "".join(f"<option value='{value}'{' selected' if status_filter == value else ''}>{label}</option>"
                  for value, label in (("draft", "草稿"), ("published", "已发布"), ("disabled", "已停用")))
        + "</select><button class='button secondary'>筛选</button></form></div>"
        + ("<div class='rows'>" + "".join(rows) + "</div>" if rows else
           "<div class='empty'><h2>没有符合条件的资料</h2><p>试试清空搜索关键词或放宽筛选。</p></div>")
        + f"<div class='list-count'>共 {len(materials)} 件资料</div>")
    _ok(handler, body, "knowledge_materials", "全部资料")


# ── 主题详情 / 编辑正文（来源出处为非交互文本；文件统一从浏览树打开）──────────

def _paragraph_source_chip(paragraph) -> str:
    label = paragraph.get("source_label") or "人工撰写"
    stale = paragraph.get("source_stale")
    cls = "src-chip muted-src"
    text = f"{_esc(label)}" if label else "未标来源"
    # 与独立验收登记的既有行为一致（重复后缀暂缓修复，不静默去重）。
    if stale:
        text += "（来源资料已更新）"
    return f"<span class='{cls}' title='出处说明（打开原始文件请用左侧主题浏览列表）'>{text}</span>"


def _topic_paragraph_html(paragraph, index, *, allow_edit=False) -> str:
    text = str(paragraph.get("text") or "")
    if not text.strip():
        return ""
    tags = []
    if paragraph.get("checked"):
        tags.append("<span class='tag done'>已核对</span>")
    elif paragraph.get("origin") == "from_material":
        tags.append("<span class='tag review'>系统初稿（来源摘录，待核对）</span>")
    else:
        tags.append("<span class='tag review'>待核对</span>")
    if paragraph.get("origin") == "manual":
        tags.append("<span class='tag edit'>人工撰写</span>")
    if paragraph.get("source_stale"):
        tags.append("<span class='tag edit'>来源更换版本</span>")
    return (
        f"<div class='topic-para' id='para-{index}'><div class='tp-text'>{_esc(text)}</div>"
        f"<div class='tp-foot'>{''.join(tags)}{_paragraph_source_chip(paragraph)}</div></div>")


def _topic_relations_html(handler, topic_id, *, editor: bool, next_url: str) -> str:
    db = handler.db
    relations = db.kb2_list_relations(handler.tenant_id, topic_id)
    rows = []
    for relation in relations:
        snapshot = db._kb2_material_snapshot(handler.tenant_id, relation["material_kind"], relation["material_id"])
        stale = bool(
            snapshot and relation["pinned_version_id"]
            and snapshot["pinned_version_id"] != relation["pinned_version_id"])
        refresh_form = ""
        if stale and editor:
            refresh_form = (
                f"<form method='post' action='{KB2_UI_PREFIX}/topic/{topic_id}/relation-refresh' class='sugg-add'>"
                f"<input type='hidden' name='next' value='{_esc(next_url)}'>"
                f"<input type='hidden' name='material_kind' value='{relation['material_kind']}'>"
                f"<input type='hidden' name='material_id' value='{relation['material_id']}'>"
                "<button class='linkbtn' title='资料已有新版本：确认后把关联快照重新钉到当前版本（不会静默换版）'>"
                "重新钉住当前版本</button></form>")
        info = (
            f"<div class='row'><div class='row-main'><span class='row-title'>"
            f"<strong>{_esc(relation['title'])}</strong>"
            f"<span class='tag type'>{_esc(relation['form_label'] or relation['material_kind'])}</span>"
            + ("<span class='tag edit'>仅登记原件</span>" if relation["registered_only"] else "")
            + ("<span class='tag gone'>资料已不存在</span>" if relation["missing"] else "")
            + ("<span class='tag review'>资料已有新版本</span>" if stale else "")
            + "</span><span class='row-meta'>"
            + (f"钉住版本 v{relation['pinned_version_number']} · " if relation["pinned_version_number"] else "")
            + (f"<span class='muted'>（资料更新后旧出处不许静默换版）</span>" if False else "")
            + "从左侧主题浏览列表打开原始文件</span></div>"
            + "<div class='row-side'>" + refresh_form
            + ("<form method='post' action='%s/topic/%s/relation-remove'>" % (KB2_UI_PREFIX, topic_id) +
               f"<input type='hidden' name='next' value='{_esc(next_url)}'>"
               f"<input type='hidden' name='material_kind' value='{relation['material_kind']}'>"
               f"<input type='hidden' name='material_id' value='{relation['material_id']}'>"
               "<button class='linkbtn danger-link'>移除关联</button></form>" if editor else "")
            + "</div></div>")
        rows.append(info)
    add_form = ""
    if editor:
        materials = db.kb2_list_materials(handler.tenant_id)
        linked = {(relation["material_kind"], relation["material_id"]) for relation in relations}
        options = ["<option value=''>选择要关联的资料…</option>"]
        for material in materials:
            if (material["kind"], material["id"]) in linked:
                continue
            label = material["title"]
            options.append(
                f"<option value='{material['kind']}::{material['id']}'>{_esc(label)}（{_esc(material['form_label'])}）</option>")
        add_form = (
            f"<div class='attach-bar'><form method='post' action='{KB2_UI_PREFIX}/topic/{topic_id}/relation-add' class='attach-bar'>"
            f"<input type='hidden' name='next' value='{_esc(next_url)}'>"
            f"<label for='kb2-rel-add'>关联资料</label>"
            f"<select id='kb2-rel-add' name='material' required>{''.join(options)}</select>"
            "<button class='button secondary'>添加关联</button></form></div>")
    return (
        f"<details class='section-fold' id='kb2-relations'><summary>关联资料"
        f"<span class='fold-count'>{len(relations)}</span></summary><div class='fold-body'>"
        + ("<div class='rows'>" + "".join(rows) + "</div>" if rows else
           "<p class='muted'>还没有关联资料：可以从下面的「关联资料」添加；关联不会改变资料本身、不改变发布状态、不授权 AI。</p>")
        + add_form + "</div></details>")


def _topic_versions_html(versions: list, *, topic_id: str = "", editor: bool = False) -> str:
    rows = []
    for version in versions:
        status_label = {"draft": "草稿", "published": "已发布", "superseded": "历史版本"}.get(version["status"], version["status"])
        restore = ""
        if editor and version["status"] != "draft":
            restore = (
                f"<form method='post' action='{KB2_UI_PREFIX}/topic/{topic_id}/version-restore' class='sugg-add'>"
                f"<input type='hidden' name='version' value='{version['version']}'>"
                "<button class='linkbtn' title='产生新草稿，不覆盖历史、不影响线上版本'>恢复为草稿</button></form>")
        rows.append(
            f"<li><span class='vtag'>v{version['version']}</span><span>{_esc(status_label)}</span>"
            f"<span class='muted'>{_esc(version['created_by'])} · {_esc(str(version['created_at'])[:10])}"
            + (f" · 发布于 {_esc(str(version['published_at'])[:10])}" if version.get("published_at") else "")
            + (f" · 恢复自 v{version['restored_from_version']}" if version.get("restored_from_version") else "")
            + f" · {version['paragraph_count']} 段</span>{restore}</li>")
    return (
        f"<details class='section-fold'><summary>版本记录<span class='fold-count'>{len(rows)}</span></summary>"
        f"<div class='fold-body'><ul class='ver-list'>{''.join(rows)}</ul>"
        "<p class='hint-line'>发布新版本不会删除历史；恢复历史版本产生新草稿（注明恢复来源），不覆盖历史、不影响线上。</p></div></details>")


def _topic_related_html(handler, topic, *, editor: bool, next_url: str) -> str:
    """相关主题（主题↔主题基础关系，双向可见；非全局图谱）。"""
    related = topic.get("related_topics") or []
    chips = "".join(
        f"<span class='chiprow-inline'><a class='tag type' href='{KB2_UI_PREFIX}/topic/{item['id']}'>{_esc(item['title'])}</a>"
        + (f"<form method='post' action='{KB2_UI_PREFIX}/topic/{topic['id']}/link-remove' class='sugg-add'>"
           f"<input type='hidden' name='next' value='{_esc(next_url)}'>"
           f"<input type='hidden' name='related_topic_id' value='{item['id']}'>"
           "<button class='linkbtn danger-link'>移除</button></form>" if editor else "")
        + "</span>"
        for item in related)
    add_form = ""
    if editor:
        options = "".join(
            f"<option value='{other['id']}'>{_esc(other['title'])}</option>"
            for other in handler.db.kb2_list_topics(handler.tenant_id)
            if other["id"] != topic["id"] and other["id"] not in {item["id"] for item in related})
        if options:
            add_form = (
                f"<form method='post' action='{KB2_UI_PREFIX}/topic/{topic['id']}/link-add' class='attach-bar'>"
                f"<input type='hidden' name='next' value='{_esc(next_url)}'>"
                "<label for='kb2-link-add'>相关主题</label>"
                f"<select id='kb2-link-add' name='related_topic_id' required><option value=''>选择主题…</option>{options}</select>"
                "<button class='button secondary'>添加相关主题</button></form>")
    empty_note = "<span class='muted'>还没有相关主题</span>"
    return (
        f"<details class='section-fold'><summary>相关主题<span class='fold-count'>{len(related)}</span></summary>"
        f"<div class='fold-body'><div class='chiprow'>{chips or empty_note}</div>"
        "<p class='hint-line'>相关主题只是互跳的关系（双向），不改变正文、发布状态与授权；不做全局图谱。</p>"
        + add_form + "</div></details>")


def topic_detail_page(handler, query, topic_id: str) -> None:
    db = handler.db
    topic = db.kb2_get_topic(handler.tenant_id, topic_id)
    if not topic:
        handler.send_html(handler.console_page(
            "未找到", f"{_assets()}<div class='kb2'><div class='error'>主题不存在或不属于本企业。</div>"
            f"<p><a href='{KB2_UI_PREFIX}'>返回全部主题</a></p></div>", "knowledge"), 404)
        return
    tree = _tree_data(handler)
    editor = _editor(handler) is not None
    state = _topic_state(handler, topic)
    manage = str(_q1(query, "manage") or "") == "1" and editor
    next_url = _qs(f"{KB2_UI_PREFIX}/topic/{topic_id}", manage=(1 if manage else None),
                   notice=_q1(query, "notice") or None, error=_q1(query, "error") or None)
    paragraphs = topic["content"]["paragraphs"]
    body_html = "".join(_topic_paragraph_html(paragraph, index) for index, paragraph in enumerate(paragraphs, start=1))
    if not body_html.strip():
        body_html = (
            "<div class='empty empty-inline'><h2>这个主题还没有正文</h2>"
            + ("<p>可以编辑正文：手动撰写，或让系统按已关联资料中<strong>已核对</strong>的段落整理初稿（来源摘录，默认待核对）。</p>"
               if editor else "<p>等待有编辑权限的成员补充主题正文。</p>")
            + (f"<p><a class='button' href='{KB2_UI_PREFIX}/topic/{topic_id}/edit'>编辑正文</a></p>" if editor else "")
            + "</div>")
    banners = []
    if topic["status"] == "draft":
        edit_link = (f"<a class='button' href='{KB2_UI_PREFIX}/topic/{topic_id}/edit'>编辑并发布</a>"
                     if editor else "")
        banners.append("<div class='draft-banner'><span>主题还是草稿：逐段核对后发布，成员确认前不会出现在任何对外内容里。</span>"
                       + edit_link + "</div>")
    elif topic["status"] == "disabled":
        banners.append("<div class='draft-banner'><span>主题已停用：历史版本与关联保留；恢复后可继续维护。</span></div>")
    elif state["css"] == "pub":
        banners.append(f"<div class='info-note'>{_ic('kb2-alert')}<span>主题已发布，但 AI 客服不可用：{_esc(state['reason'])}。"
                       "主题的对外授权不能绕过来源限制（来源仅内部 / 未核对 / 未授权的段落不会用于对外回答）。</span></div>")
    actions = []
    if editor:
        actions.append(f"<a class='button secondary' href='{KB2_UI_PREFIX}/topic/{topic_id}/edit'>{_ic('kb2-pen')}编辑正文</a>")
        if topic["status"] in ("draft",) or (topic["draft_version_id"] and topic["status"] == "published"):
            actions.append(
                f"<form method='post' action='{KB2_UI_PREFIX}/topic/{topic_id}/publish' data-kb2-confirm='发布前会再次校验：正文必须全部已核对；对外且授权 AI 需要一次确认。继续发布？' data-kb2-confirm-title='发布主题' data-kb2-confirm-field='confirm_ai' data-kb2-confirm-value='1'>"
                f"<input type='hidden' name='next' value='{_esc(next_url)}'>"
                "<input type='hidden' name='confirm_ai' value='0'>"
                "<button class='button'>发布主题</button></form>")
        if topic["status"] == "published":
            actions.append(
                f"<form method='post' action='{KB2_UI_PREFIX}/topic/{topic_id}/disable' data-kb2-confirm='停用后主题退出当前导航的高亮使用；历史版本与关联全部保留。停用？' data-kb2-confirm-title='停用主题'>"
                f"<input type='hidden' name='next' value='{_esc(next_url)}'>"
                "<button class='button secondary'>停用主题</button></form>")
        elif topic["status"] == "disabled":
            actions.append(
                f"<form method='post' action='{KB2_UI_PREFIX}/topic/{topic_id}/enable'>"
                f"<input type='hidden' name='next' value='{_esc(next_url)}'>"
                "<button class='button secondary'>恢复主题</button></form>")
    readonly = "" if editor else (
        "<div class='readonly-note'>" + _ic("kb2-alert") +
        "<span>只读：你不是本企业的知识管理员。可以阅读、搜索、返回与跳转；编辑、发布、授权与处理操作需要管理员或产品管理模块权限。</span></div>")
    hint = (f"<p class='hint-line'>打开原始文件统一从左侧「主题浏览列表」进入；"
            "本页的「关联资料」不重复放打开文件的按钮。</p>")
    settings_bits = ["用途：内部" if topic["usage"] == "internal" else "用途：对外",
                     "AI：已授权" if topic["ai_authorized"] else "AI：未授权"]
    if topic.get("expires_at"):
        settings_bits.append(f"到期：{_esc(str(topic['expires_at'])[:16])}")
    detail = (
        _notice_html(query)
        + "<div class='detail-top'><a class='back-link' href='"
        + _esc(_safe_next(_q1(query, "back"), KB2_UI_PREFIX)) + "'>"
        + _ic("kb2-back") + "返回全部主题</a></div>"
        f"<div class='detail-head'><div class='detail-title'><h1>{_esc(topic['title'])}</h1>"
        f"<span class='pill {state['css']}'>{_esc(state['label'])}</span></div>"
        f"<div class='actions'>{''.join(actions)}</div></div>"
        f"<div class='detail-meta'><span>相关资料 {len(topic['relations'])}</span>"
        f"<span>更新 {_esc(str(topic['updated_at'])[:10])}</span>"
        + (f"<span>当前线上版本 v{topic['published_version_number']}</span>" if topic["published_version_number"] else "")
        + "".join(f"<span>{bit}</span>" for bit in settings_bits)
        + "</div>"
        + "".join(banners) + readonly
        + "<div class='topic-block'><div class='section-head'><h2>正文</h2>"
        + "<span class='sec-note'>段落来源为文本说明（非交互）；系统初稿只使用已核对的来源段落，默认待核对。</span></div>"
        + body_html + hint + "</div>"
        + _topic_relations_html(handler, topic_id, editor=editor, next_url=next_url)
        + _topic_related_html(handler, topic, editor=editor, next_url=next_url)
        + _topic_versions_html(topic["versions"], topic_id=topic_id, editor=editor))
    _ok(handler, _workspace(detail, tree, active_topic=topic_id), "knowledge", topic["title"])


def _expiry_parts(value) -> tuple[str, str]:
    """把已保存的到期时刻（UTC ISO）换算为北京时间日期/时间，用于表单回填。"""
    if not value:
        return "", ""
    from datetime import datetime as _dt, timedelta as _td, timezone as _tz
    try:
        moment = _dt.fromisoformat(str(value))
    except ValueError:
        return str(value)[:10], ""
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=_tz.utc)
    local = moment.astimezone(_tz(_td(hours=8)))
    return local.strftime("%Y-%m-%d"), local.strftime("%H:%M")


def _topic_settings_fields(topic) -> str:
    """主题发布设置（用途 / AI / 到期）：与第一批口径一致，唯一位置；默认对内、AI 关。"""
    usage = topic.get("usage") or "internal"
    ai = " checked" if topic.get("ai_authorized") else ""
    date_value, time_value = _expiry_parts(topic.get("expires_at"))
    return (
        "<div class='card'><h2>用途、AI 与到期</h2>"
        "<fieldset class='settings-group'><legend>用途</legend>"
        f"<label class='check-inline'><input type='radio' name='usage' value='internal'{' checked' if usage == 'internal' else ''}> <span>仅内部使用（默认）</span></label>"
        f"<label class='check-inline'><input type='radio' name='usage' value='external'{' checked' if usage == 'external' else ''}> <span>对外使用（客户可见的 AI 回答）</span></label></fieldset>"
        "<fieldset class='settings-group'><legend>AI 客服使用</legend>"
        f"<label class='check-inline'><input type='checkbox' name='ai_authorized' value='1'{ai}> <span>授权 AI 客服使用（默认关闭）</span></label>"
        "<p class='hint-line'>对外且授权 AI 时，发布前需要一次确认；来源限制优先——主题授权不能绕过来源（仅内部 / 未核对 / 未授权 / 已换版的段落不会用于对外回答）。</p></fieldset>"
        "<fieldset class='settings-group'><legend>到期（可选）</legend>"
        f"<div class='inline-row'><input type='date' name='expiry_date' value='{_esc(date_value)}' aria-label='到期日期'>"
        f"<input type='time' name='expiry_time' value='{_esc(time_value)}' aria-label='到期时间'></div>"
        "<p class='hint-line'>留空 = 长期有效；到期时间按北京时间保存（与第一批一致）。</p></fieldset></div>")


def topic_edit_page(handler, query, topic_id: str) -> None:
    db = handler.db
    topic = db.kb2_get_topic(handler.tenant_id, topic_id)
    if not topic:
        handler.send_html(handler.console_page(
            "未找到", f"{_assets()}<div class='kb2'><div class='error'>主题不存在或不属于本企业。</div></div>",
            "knowledge"), 404)
        return
    if _editor(handler) is None:
        handler._kb_forbidden()
        return
    tree = _tree_data(handler)
    next_url = _qs(f"{KB2_UI_PREFIX}/topic/{topic_id}/edit")
    paragraphs = topic["content"]["paragraphs"]
    cards = []
    for index, paragraph in enumerate(paragraphs):
        source_label = paragraph.get("source_label") or "人工撰写"
        kind = str(paragraph.get("kind") or "intro")
        pin = paragraph.get("source") if isinstance(paragraph.get("source"), dict) else None
        renew_button = ""
        if paragraph.get("source_stale") and pin and pin.get("kind") == "knowledge":
            renew_button = (
                f"<button class='linkbtn' name='renew_paragraph' value='{_esc(paragraph.get('id') or '')}' "
                "title='按当前资料内容重新钉住这一段的确切出处（不会静默换版）'>"
                "重新核对来源（按当前资料）</button>")
        edits_marker = ("<span class='tag edit'>人工改写（核对后发布）</span>"
                        if paragraph.get("origin") == "edited" else "")
        cards.append(
            f"<div class='para-card'><div class='para-head'><span class='para-no'>第 {index + 1} 段</span>"
            f"<span class='src-label'>{_esc(source_label)}</span>"
            f"<span class='tag type'>{'介绍' if kind == 'intro' else '要点'}</span>"
            + ("<span class='tag edit'>人工撰写</span>" if paragraph.get("origin") == "manual" else "")
            + edits_marker
            + "</div>"
            f"<label for='kb2-p-{index}'>段落文字</label>"
            f"<textarea id='kb2-p-{index}' name='p_{index}_text'>{_esc(paragraph.get('text') or '')}</textarea>"
            f"<input type='hidden' name='p_{index}_id' value='{_esc(paragraph.get('id') or '')}'>"
            f"<input type='hidden' name='p_{index}_origin' value='{_esc(paragraph.get('origin') or 'manual')}'>"
            f"<input type='hidden' name='p_{index}_kind' value='{_esc(kind)}'>"
            f"<input type='hidden' name='p_{index}_source' value='{_esc(json.dumps(pin, ensure_ascii=False)) if pin else ''}'>"
            "<div class='chiprow'>"
            f"<label class='check-inline'><input type='checkbox' name='p_{index}_checked' value='1'{' checked' if paragraph.get('checked') else ''}> <span>已核对（核对后才能发布）</span></label>"
            f"<label class='check-inline'><input type='checkbox' name='p_{index}_remove' value='1'> <span>删除这一段</span></label>"
            + renew_button
            + "</div></div>")
    editor_body = (
        _notice_html(query)
        + "<div class='detail-top'><a class='back-link' href='"
        + _esc(_qs(f"{KB2_UI_PREFIX}/topic/{topic_id}")) + "'>" + _ic("kb2-back") + "返回主题</a></div>"
        f"<div class='toolbar'><div><h1>编辑正文 · {_esc(topic['title'])}</h1>"
        "<p class='lead'>修改会保存为草稿，不影响已发布版本；系统初稿只追加、不覆盖人工内容。</p></div>"
        f"<div class='actions'>"
        f"<form method='post' action='{KB2_UI_PREFIX}/topic/{topic_id}/build-draft' data-kb2-confirm='将使用已关联资料中「已核对且未排除」的段落，按来源摘录追加为待核对初稿（不调用模型、不覆盖人工内容）。继续？' data-kb2-confirm-title='根据关联资料整理初稿'>"
        f"<input type='hidden' name='next' value='{_esc(next_url)}'>"
        f"<button class='button secondary'>{_ic('i-layers')}根据关联资料整理初稿</button></form>"
        "</div></div>"
        f"<form method='post' action='{KB2_UI_PREFIX}/topic/{topic_id}/save' data-kb2-save data-kb2-leave-ok='1'>"
        f"<input type='hidden' name='next' value='{_esc(next_url)}'>"
        + ("".join(cards) if cards else "<p class='muted'>还没有段落：可以手动添加，或先用「根据关联资料整理初稿」。</p>")
        + _topic_settings_fields(topic)
        + "<div class='review-footer'><span class='hint-line'>未核对段落不会被发布；保存允许不完整。</span>"
        "<span class='actions'><button class='button secondary'>保存草稿</button></span></div></form>"
        + f"<div class='card'><h2>{_ic('kb2-pen')}添加段落（人工撰写）</h2>"
        f"<form method='post' action='{KB2_UI_PREFIX}/topic/{topic_id}/paragraph-add'>"
        f"<input type='hidden' name='next' value='{_esc(next_url)}'>"
        "<label for='kb2-new-para'>段落文字</label>"
        "<textarea id='kb2-new-para' name='text' placeholder='例如：安装前必须确认断电。'></textarea>"
        "<div class='inline-row'><select name='kind' aria-label='段落类型'>"
        "<option value='intro'>介绍</option><option value='point' selected>要点</option></select>"
        "<button class='button secondary'>添加为待核对段落</button></div>"
        "<p class='hint-line'>人工撰写的段落会明确标注，不冒充原件原文；发布前同样需要核对。</p></form></div>")
    _ok(handler, _workspace(editor_body, tree, active_topic=topic_id), "knowledge", topic["title"])


# ── 添加资料（第 1 步：单一输入卡片；默认文件，切换保留已输入）──────────────

KB2_ADD_LIMITS = (
    "支持 TXT / Markdown / CSV（≤1MB）、文字 PDF、DOCX（≤10MB）；图片、视频与旧版 DOC 可仅登记原件；"
    "一次最多 20 件；文字 PDF 最多处理 200 页（超出仍可原件归档并人工整理）；网页地址只登记、不抓取。"
)


def add_page(handler, query, *, error: str = "", values=None) -> None:
    editor = _editor(handler) is not None
    if not editor:
        handler._kb_forbidden()
        return
    values = values or {}
    tab = str(_q1(query, "tab") or values.get("tab") or "file")
    if tab not in ("file", "link", "manual"):
        tab = "file"

    def panel(name, inner):
        hidden = "" if name == tab else " hidden"
        return f"<div class='mode-card'{hidden} data-kb2-mode='{name}'>{inner}</div>"

    file_value = _esc(values.get("filename") or "")
    link_value = _esc(values.get("url") or "")
    manual_value = _esc(values.get("text") or "")
    switch = (
        "<div class='input-switch' role='group' aria-label='资料输入方式'>"
        f"<button class='switch-btn{' on' if tab == 'file' else ''}' type='button' data-kb2-mode-btn='file' aria-pressed='{'true' if tab == 'file' else 'false'}'>文件</button>"
        f"<button class='switch-btn{' on' if tab == 'link' else ''}' type='button' data-kb2-mode-btn='link' aria-pressed='{'true' if tab == 'link' else 'false'}'>网页地址</button>"
        f"<button class='switch-btn{' on' if tab == 'manual' else ''}' type='button' data-kb2-mode-btn='manual' aria-pressed='{'true' if tab == 'manual' else 'false'}'>手动文字</button>"
        "</div>"
        "<p class='hint-line'>切换只换输入卡片：已填写的文件、地址与文字不会丢失；三种可以一起提交，形成同一批资料。</p>")
    card = (
        "<div class='card'><h2>添加资料</h2>"
        f"<form method='post' action='{KB2_UI_PREFIX}/add' enctype='multipart/form-data' data-kb2-unsaved>"
        + panel("file", (
            "<div class='drop'><strong>选择文件</strong>"
            "<p>" + _esc(KB2_ADDLIMITS()) + "</p>"
            f"<input type='file' name='source_file' multiple data-kb2-file-input aria-label='选择要添加的文件'>"
            f"<div class='inline-row' style='margin-top:10px'><input name='file_title' placeholder='资料标题（可选，默认用文件名）' value='{_esc(values.get('file_title') or '')}'></div>"
            "</div>"))
        + panel("link", (
            "<div class='drop'><strong>登记网页地址</strong>"
            "<p>本批只登记地址、不抓取内容；需要内容时请人工整理正文。</p>"
            f"<div class='inline-row'><input name='link_url' type='url' placeholder='https://example.com/article' value='{link_value}'>"
            f"<input name='link_title' placeholder='标题（可选）' value='{_esc(values.get('link_title') or '')}'></div></div>"))
        + panel("manual", (
            "<div class='drop'><strong>手动文字</strong>"
            "<p>直接写正文（保存为人工撰写，发布前需要核对）。</p>"
            f"<textarea name='manual_text' placeholder='粘贴或输入文字内容…'>{manual_value}</textarea>"
            f"<div class='inline-row' style='margin-top:10px'><input name='manual_title' placeholder='标题（可选，默认用第一行）' value='{_esc(values.get('manual_title') or '')}'></div></div>"))
        + switch
        + "<div class='review-footer'><span class='hint-line'>提交后进入「检查整理结果」：原件在后台私有保存并处理，不会在请求里等待解析。</span>"
        "<span class='actions'><button class='button' data-kb2-submit-any>添加并检查</button></span></div>"
        "</form></div>")
    batches = handler.db.kb2_list_batches(handler.tenant_id, limit=12)
    rows = []
    for batch in batches:
        status_label = {"draft": "草稿", "confirmed": "已确认主题", "published": "已发布"}.get(batch["status"], batch["status"])
        running = f" · 处理中 {batch['sources_pending']}" if batch["sources_pending"] else ""
        rows.append(
            f"<div class='row'><a class='row-main' href='{KB2_UI_PREFIX}/batch/{batch['id']}'>"
            f"<span class='row-title'><strong>{_esc(str(batch['created_at'])[:16].replace('T', ' '))}</strong>"
            f"<span class='tag type'>{len(batch['id']) and (str(batch['sources_total']) + ' 件资料')}</span></span>"
            f"<span class='row-meta'>{_esc(status_label)}{running} · 由 {_esc(batch['created_by'])} 添加</span></a>"
            f"<div class='row-side'><a class='linkbtn' href='{KB2_UI_PREFIX}/batch/{batch['id']}'>继续处理</a></div></div>")
    recent = (
        "<div class='card'><h2>最近批次</h2>"
        + ("<div class='rows'>" + "".join(rows) + "</div>" if rows
           else "<p class='muted'>还没有批次：提交第一件资料后会出现在这里，离开页面也能找回。</p>")
        + "</div>")
    body = (
        "<div class='steps' aria-label='添加资料步骤'>"
        "<span class='step on'><span class='n'>1</span>添加资料</span><span class='sep'></span>"
        "<span class='step'><span class='n'>2</span>检查整理结果</span><span class='sep'></span>"
        "<span class='step'><span class='n'>3</span>发布设置</span></div>"
        "<div class='toolbar'><div><h1>添加资料</h1>"
        "<p class='lead'>一次只显示一个输入卡片（默认文件）；完成后进入检查整理结果，核对正文并确认主题。</p></div>"
        f"<div class='actions'><a class='button secondary' href='{KB2_UI_PREFIX}/templates'>知识模板（可选）</a></div></div>"
        + (f"<div class='form-alert' role='alert'>{_ic('kb2-alert')}<span>{_esc(error)}</span></div>" if error else "")
        + card + recent)
    _ok(handler, body, "knowledge_add", "添加资料")


def KB2_ADDLIMITS() -> str:
    return KB2_ADD_LIMITS


# ── 检查整理结果（第 2 步）：逐段核对 / 编辑 / 排除 / 字段 / 主题建议确认 ──────

def _source_status_pill(source) -> str:
    css = _IDENTIFICATION_PILL_CSS.get(source["identification"], "draft")
    return f"<span class='pill {'pub' if css == 'pub' else css}'>{_esc(source['identification_label'])}</span>"


def _paragraph_form(handler, batch, source, *, locked: bool) -> str:
    paragraphs = source["paragraphs"]
    cards = []
    for index, paragraph in enumerate(paragraphs, start=1):
        tags = []
        if paragraph["source_kind"] == "manual":
            tags.append("<span class='tag edit'>人工撰写</span>")
        if paragraph["origin"] == "edited":
            tags.append("<span class='tag edit'>人工修改</span>")
        if paragraph["excluded"]:
            tags.append("<span class='tag gone'>已排除（不进入正文/AI）</span>")
        if paragraph["checked"]:
            tags.append("<span class='tag done'>已核对</span>")
        cards.append(
            f"<div class='para-card'><div class='para-head'><span class='para-no'>第 {index} 段</span>"
            f"<span class='src-label'>{_esc(paragraph['location_label'] or '人工撰写')}</span>{''.join(tags)}</div>"
            f"<label for='kb2p-{paragraph['id']}'>段落文字（编辑后需要重新核对）</label>"
            f"<textarea id='kb2p-{paragraph['id']}' name='p_{paragraph['id']}_text'>{_esc(paragraph['text'])}</textarea>"
            "<div class='chiprow'>"
            f"<label class='check-inline'><input type='checkbox' name='p_{paragraph['id']}_checked' value='1'{' checked' if paragraph['checked'] else ''}> <span>已核对</span></label>"
            f"<label class='check-inline'><input type='checkbox' name='p_{paragraph['id']}_excluded' value='1'{' checked' if paragraph['excluded'] else ''}> <span>排除（不进入正文与 AI）</span></label>"
            "</div></div>")
    if not cards:
        cards.append("<p class='muted'>这件资料还没有正文段落（可能仅登记原件）；可以补充人工正文。</p>")
    pending = source["counts"]["pending_check"]
    disabled = " disabled" if locked else ""
    button = ("<button class='button secondary' disabled>保存修改</button>" if locked
              else "<button class='button secondary'>保存修改</button>")
    return (
        f"<form method='post' action='{KB2_UI_PREFIX}/batch/{batch['id']}/paragraphs' data-kb2-save>"
        f"<input type='hidden' name='source_id' value='{source['id']}'>"
        f"<div class='fold-region' data-kb2-fold-region>" + "".join(cards) + "</div>"
        f"<div class='review-footer'><span class='hint-line'>剩余待核对 {pending} 段"
        + ("；只读成员不能修改。" if locked else "；编辑文字会撤销该段已核对。") + "</span>"
        f"<span class='actions'>{button}</span></div></form>")


def _fields_html(handler, batch, source, *, locked: bool) -> str:
    rows = []
    for field in source["fields"]:
        location = field["paragraph_location_label"] or "资料级"
        checked = "<span class='tag done'>已核对</span>" if field["checked"] else "<span class='tag review'>未核对</span>"
        rows.append(
            f"<li><span><strong>{_esc(field['label'])}</strong>：{_esc(field['value'])}{_esc(field['unit'])}</span>"
            f"<small>来源：{_esc(location)}</small>{checked}</li>")
    add = ""
    if not locked:
        paragraph_options = "".join(
            f"<option value='{paragraph['id']}'>{_esc(paragraph['location_label'] or '人工撰写')}（第 {index} 段）</option>"
            for index, paragraph in enumerate(source["paragraphs"], start=1))
        add = (
            f"<form method='post' action='{KB2_UI_PREFIX}/batch/{batch['id']}/field' class='field-add'>"
            f"<input type='hidden' name='source_id' value='{source['id']}'>"
            "<input name='label' placeholder='字段名称（例如：响应时间）' required>"
            "<input name='value' placeholder='值（0 与「否」也是有效值）' required>"
            "<input name='unit' placeholder='单位（可选）'>"
            f"<select name='paragraph_id' aria-label='字段来源段落'><option value=''>资料级（不钉住段落）</option>{paragraph_options}</select>"
            "<button class='button secondary'>保存字段</button></form>")
    return (
        "<div class='opt-details-open'><p class='sub-head'>可选字段（每个字段都要求写出来源）</p>"
        + (f"<ul class='field-list'>{''.join(rows)}</ul>" if rows else "<p class='muted'>还没有字段。</p>")
        + add + "</div>")


def _suggestions_html(handler, batch, *, locked: bool) -> str:
    suggestions = batch["suggestions"]
    rows = []
    for suggestion in suggestions:
        basis = _SUGGESTION_BASIS_LABELS.get(suggestion["basis"], suggestion["basis"])
        existing = (" → 合并到："
                    + f"<a href='{KB2_UI_PREFIX}/topic/{suggestion['existing_topic_id']}'>{_esc(suggestion['existing_topic_title'] or '')}</a>"
                    if suggestion["existing_topic_id"] else "")
        if suggestion["status"] == "rejected":
            rows.append(
                f"<div class='sugg-row rejected'><span class='sugg-chip new'><span>{_esc(suggestion['name'])}</span>"
                f"<small>已拒绝 · {_esc(basis)}</small></span>"
                + ("" if locked else
                   f"<form method='post' action='{KB2_UI_PREFIX}/batch/{batch['id']}/suggestion-add' class='sugg-add'>"
                   f"<input type='hidden' name='name' value='{_esc(suggestion['name'])}'>"
                   "<button class='linkbtn'>重新考虑（作为成员建议）</button></form>")
                + "</div>")
            continue
        if suggestion["status"] == "confirmed":
            rows.append(
                f"<div class='sugg-row'><span class='sugg-chip'><span>{_esc(suggestion['name'])}</span>"
                f"<small>已确认 · {_esc(basis)}</small></span>{existing}</div>")
            continue
        name_form = ""
        merge_form = ""
        sources_form = ""
        reject_form = ""
        if not locked:
            name_form = (
                f"<form method='post' action='{KB2_UI_PREFIX}/batch/{batch['id']}/suggestion/{suggestion['id']}/rename' class='sugg-add'>"
                f"<input name='name' value='{_esc(suggestion['name'])}' aria-label='建议名称'>"
                "<button class='linkbtn'>改名</button></form>")
            topic_options = "".join(
                f"<option value='{topic['id']}'>{_esc(topic['title'])}</option>"
                for topic in handler.db.kb2_list_topics(handler.tenant_id))
            merge_form = (
                f"<form method='post' action='{KB2_UI_PREFIX}/batch/{batch['id']}/suggestion/{suggestion['id']}/merge' class='sugg-add'>"
                f"<select name='topic_id' aria-label='合并到已有主题' required><option value=''>合并到已有主题…</option>{topic_options}</select>"
                "<button class='linkbtn'>合并</button></form>")
            source_checks = "".join(
                f"<label class='check-inline'><input type='checkbox' name='source_ids' value='{source['id']}'"
                + (" checked" if source["id"] in suggestion["source_ids"] else "") + ">"
                f" <span>{_esc(source['title'] or source['filename'])}</span></label>"
                for source in batch["sources"])
            sources_form = (
                f"<details class='more-wrap'><summary class='linkbtn'>调整资料范围（{len(suggestion['source_ids'])}）</summary>"
                f"<div class='more-menu'><form method='post' action='{KB2_UI_PREFIX}/batch/{batch['id']}/suggestion/{suggestion['id']}/sources'>"
                + source_checks + "<p><button class='button secondary'>保存资料范围</button></p></form></div></details>")
            reject_form = (
                f"<form method='post' action='{KB2_UI_PREFIX}/batch/{batch['id']}/suggestion/{suggestion['id']}/reject'>"
                "<button class='linkbtn danger-link'>拒绝</button></form>")
        rows.append(
            f"<div class='sugg-row'><span class='sugg-chip{' new' if suggestion['basis'] in ('source_title', 'source_heading') else ''}'>"
            f"<span>{_esc(suggestion['name'])}</span><small>{_esc(basis)} · 待确认</small></span>{existing}"
            f"{name_form}{merge_form}{sources_form}{reject_form}</div>")
    build = ""
    if not locked:
        build = (
            f"<form method='post' action='{KB2_UI_PREFIX}/batch/{batch['id']}/suggestions/build'>"
            "<button class='button secondary'>生成系统建议（现有主题文字命中 + 依据资料标题/标题段落的新主题候选）</button></form>")
    add = ""
    confirm = ""
    if not locked:
        add = (
            f"<form method='post' action='{KB2_UI_PREFIX}/batch/{batch['id']}/suggestion-add' class='sugg-add'>"
            "<label for='kb2-sugg-name'>添加/整批共同主题</label>"
            "<input id='kb2-sugg-name' name='name' placeholder='例如：售后与安装'>"
            "<label class='check-inline'><input type='checkbox' name='all_sources' value='1' checked> <span>应用到全部资料</span></label>"
            "<button class='button secondary'>添加建议</button></form>")
        confirm = (
            f"<div class='confirm-assoc'><p class='hint-line'>确认后才建立主题与关联（草稿状态）；"
            "新主题候选必须经此确认，不会自动生效。</p>"
            f"<form method='post' action='{KB2_UI_PREFIX}/batch/{batch['id']}/confirm-topics'>"
            "<button class='button'>确认主题与关联</button></form></div>")
    return (
        "<div class='sugg-block'><p class='sub-head'>系统建议的主题（确认前不进入主题导航）</p>"
        + ("".join(rows) if rows else "<p class='muted'>还没有建议：生成系统建议，或直接添加。</p>")
        + build + add + confirm + "</div>")


def _gates(handler, batch) -> list:
    gates = []
    for source in batch["sources"]:
        label = source["title"] or source["filename"]
        if handler.db._kb2_draft_owned_by_other_original(handler.tenant_id, source):
            # B2-B08：该来源属于历史原件，当前草稿已被更新版本接管；本批次发布不会覆盖它。
            gates.append(("bad", f"{label}：属于历史原件，当前草稿来自更新版本；本批次发布不会覆盖它"
                                 "（请到当前草稿所在批次发布）", source["id"]))
            continue
        if source["status"] in ("pending", "processing"):
            gates.append(("bad", f"{label}：仍在后台处理中（可先检查其他资料，稍后刷新）", source["id"]))
            continue
        if source["status"] == "failed":
            gates.append(("bad", f"{label}：未能识别（{source['error'] or '原因未知'}）——可重试或补充人工正文", source["id"]))
            continue
        if source["status"] == "published":
            continue
        pending = source["counts"]["pending_check"]
        has_content = any(not paragraph["excluded"] and paragraph["text"].strip()
                          for paragraph in source["paragraphs"])
        if has_content and pending:
            gates.append(("bad", f"{label}：还有 {pending} 段未核对", source["id"]))
        elif not has_content:
            gates.append(("ok", f"{label}：仅登记原件，将按归档发布（AI 不可用，不算发布知识正文）", source["id"]))
        else:
            gates.append(("ok", f"{label}：正文已全部核对，可发布", source["id"]))
    return gates


def batch_page(handler, query, batch_id: str, *, step: int) -> None:
    db = handler.db
    batch = db.kb2_get_batch(handler.tenant_id, batch_id)
    if not batch:
        handler.send_html(handler.console_page(
            "未找到", f"{_assets()}<div class='kb2'><div class='error'>批次不存在或不属于本企业。</div></div>",
            "knowledge_add"), 404)
        return
    locked = _editor(handler) is None
    base = f"{KB2_UI_PREFIX}/batch/{batch_id}"
    steps_html = (
        "<div class='steps' aria-label='添加资料步骤'>"
        f"<a class='step' href='{KB2_UI_PREFIX}/add'><span class='n'>1</span>添加资料</a><span class='sep'></span>"
        f"<a class='step{' on' if step == 2 else ''}' href='{base}'><span class='n'>2</span>检查整理结果</a><span class='sep'></span>"
        f"<a class='step{' on' if step == 3 else ''}' href='{base}?step=publish'><span class='n'>3</span>发布设置</a></div>")
    summary = batch["summary"]
    head = (
        f"<div class='toolbar'><div><h1>检查整理结果</h1>"
        f"<p class='lead'>本批 {summary['sources']} 件：已识别 {summary['extracted']} · 仅登记 {summary['registered']} · "
        f"未能识别 {summary['failed']} · 处理中 {summary['pending']}。完成识别只创建草稿，绝不自动发布。</p></div>"
        f"<div class='actions'><a class='button secondary' href='{base}'>{_ic('i-history')}刷新状态</a></div></div>")
    if summary["pending"]:
        head += (f"<div class='info-note'>{_ic('i-clock')}<span>还有资料在后台处理中：可以离开本页，稍后从「添加资料 → 最近批次」回到这里。</span></div>")
    if locked:
        head += ("<div class='readonly-note'>" + _ic("kb2-alert") +
                 "<span>只读：你不是本企业的知识管理员。可以查看整理结果；核对、编辑、排除与发布需要管理员或产品管理模块权限。</span></div>")

    if step == 3:
        gates = _gates(handler, batch)
        gate_rows = "".join(
            f"<div class='gate gate-{'ok' if kind == 'ok' else 'bad'}'>{_ic('kb2-check' if kind == 'ok' else 'kb2-alert')}"
            f"<span>{_esc(text)} · <a href='{base}#source-{source_id}'>去处理</a></span></div>"
            for kind, text, source_id in gates)
        usage = batch["usage"]
        ai = " checked" if batch["ai_authorized"] else ""
        expiry_date_value, expiry_time_value = _expiry_parts(batch["expires_at"])
        form = ""
        if not locked:
            # 确认属性必须挂在表单上：static/knowledge-phase2.js 的确认对话框按 form[data-kb2-confirm]
            # 绑定（修复前误挂在「发布」按钮上，组件不读按钮属性——点发布直接提交，对外+AI 发布经
            # 界面无法完成）。「发布」保留自身动作字段（action=publish），确认后由组件保留提交按钮
            # 重发；「保存设置」声明 data-kb2-confirm-skip，按自身动作直接提交、不进确认。
            form = (
                f"<form method='post' action='{base}/settings' data-kb2-confirm='发布前会逐件校验正文核对情况；对外且授权 AI 需要确认。继续发布？' data-kb2-confirm-title='发布批次' data-kb2-confirm-field='confirm_ai' data-kb2-confirm-value='1'><fieldset class='settings-group'>"
                "<legend>用途</legend>"
                f"<label class='check-inline'><input type='radio' name='usage' value='internal'{' checked' if usage == 'internal' else ''}> <span>仅内部使用（默认）</span></label>"
                f"<label class='check-inline'><input type='radio' name='usage' value='external'{' checked' if usage == 'external' else ''}> <span>对外使用（客户可见的 AI 回答）</span></label></fieldset>"
                "<fieldset class='settings-group'><legend>AI 客服使用</legend>"
                f"<label class='check-inline'><input type='checkbox' name='ai_authorized' value='1'{ai} data-kb2-ai-toggle> <span>授权 AI 客服使用（默认关闭）</span></label>"
                "<p class='hint-line' id='kb2-ai-confirm-hint'>对外且授权 AI 时，发布前需要一次确认；仅登记原件即使授权 AI 也不可用。</p></fieldset>"
                "<fieldset class='settings-group'><legend>到期（可选）</legend>"
                f"<div class='inline-row'><input type='date' name='expiry_date' value='{_esc(expiry_date_value)}' aria-label='到期日期'>"
                f"<input type='time' name='expiry_time' value='{_esc(expiry_time_value)}' aria-label='到期时间'></div>"
                "<p class='hint-line'>留空 = 长期有效；只填日期 = 北京时间当日 23:59:59；到期时间按北京时间保存。</p></fieldset>"
                "<div class='review-footer'><span class='hint-line'>保存设置允许不完整；发布后仍可修改设置（生成新草稿）。</span>"
                "<span class='actions'>"
                f"<button class='button secondary' name='action' value='save' data-kb2-confirm-skip>保存设置</button>"
                f"<button class='button' name='action' value='publish'>发布</button>"
                "</span></div><input type='hidden' name='confirm_ai' value='0'></form>")
        body = (_notice_html(query) + steps_html + head
                + "<div class='card'><h2>发布校验</h2><div class='gates'>" + gate_rows + "</div></div>"
                + ("<div class='card'><h2>发布设置</h2>" + form + "</div>" if form else ""))
        _ok(handler, body, "knowledge_add", "发布设置")
        return

    sources_html = []
    first_content_source = next(
        (source for source in batch["sources"]
         if source["paragraphs"] and any(p["text"].strip() for p in source["paragraphs"])), None)
    for source in batch["sources"]:
        title = source["title"] or source["filename"]
        open_default = source is first_content_source
        warnings = "".join(f"<p class='hint-line warns'>{_esc(warning)}</p>" for warning in source["warnings"])
        error = (f"<p class='hint-line bad'>{_esc(source['error'])}</p>" if source["error"] else "")
        retry = ""
        if not locked and source["status"] == "failed":
            retry = (
                f"<form method='post' action='{KB2_UI_PREFIX}/batch/{batch_id}/source/{source['id']}/retry'>"
                "<button class='button secondary'>重试识别</button></form>")
        manual = ""
        if not locked and source["status"] != "published":
            manual = (
                f"<details class='opt-details'><summary>补充人工正文（发布前同样要核对）</summary>"
                f"<form method='post' action='{KB2_UI_PREFIX}/batch/{batch_id}/paragraph-add'>"
                f"<input type='hidden' name='source_id' value='{source['id']}'>"
                "<textarea name='text' placeholder='例如：某页的表格内容，按行写下关键参数…'></textarea>"
                "<p><button class='button secondary'>添加为待核对段落</button></p></form></details>")
        sources_html.append(
            f"<section class='item-card' id='source-{source['id']}'>"
            f"<div class='item-head'><span class='item-name'>{_esc(title)}</span>"
            f"<span class='tag type'>{_esc(source['format_label'])}</span>{_source_status_pill(source)}"
            f"<span class='muted'>{(str(source['size']) + ' B · ') if source['size'] else ''}{_esc(source['source_location'])}</span>"
            f"<span class='grow'></span>{retry}</div>"
            + warnings + error
            + ("<details class='fold-head-wrap' open>" if open_default else "<details class='fold-head-wrap'>"))
        sources_html.append(
            f"<summary class='fold-head{' open' if open_default else ''}' data-kb2-fold>检查正文"
            f"<small>（剩余待核对 {source['counts']['pending_check']} 段）</small></summary>"
            + _paragraph_form(handler, batch, source, locked=locked)
            + _fields_html(handler, batch, source, locked=locked)
            + manual + "</details></section>")
    check_body = (_notice_html(query) + steps_html + head
                  + _replacement_diff_html(handler, batch_id, batch)
                  + "<div class='card'><h2>逐件核对正文</h2>" + "".join(sources_html) + "</div>"
                  + "<div class='card'><h2>确认主题与关联</h2>" + _suggestions_html(handler, batch, locked=locked) + "</div>"
                  + f"<p class='hint-line'>完成后进入 <a href='{base}?step=publish'>发布设置 →</a></p>")
    _ok(handler, check_body, "knowledge_add", "检查整理结果")


def _replacement_diff_html(handler, batch_id: str, batch) -> str:
    """文件更新的差异对照区（位置/文本比较；不判定新旧资料哪个更真实）。"""
    db = handler.db
    batch_row = db._kb2_batch_row(handler.tenant_id, batch_id)
    if not batch_row or batch_row["purpose"] != "replace_file":
        return ""
    cards = []
    for source in batch["sources"]:
        diff = db.kb2_source_replacement_diff(handler.tenant_id, source["id"])
        if not diff:
            continue
        counts = diff["counts"]
        lines = [
            f"<div class='item-head'><span class='item-name'>{_esc(source['title'] or source['filename'])}</span>"
            f"<span class='tag type'>{_esc(source['format_label'])}</span>"
            f"<span class='muted'>对照基准：发布版本 v{diff['baseline_version']}（旧线上版本在核对发布前继续有效）</span></div>",
            f"<p class='hint-line'>差异对照：新增 {counts['added']} · 变更 {counts['changed']} · "
            f"移除 {counts['removed']} · 未变 {counts['unchanged']}。是文本比较，不代表自动判断新旧资料哪个真实。</p>",
        ]
        for change in diff["changed"][:20]:
            lines.append(f"<div class='diff-row'><span class='muted'>旧</span><span class='diff-old'>{_esc(change['old'])}</span></div>"
                         f"<div class='diff-row'><span class='muted'>新</span><span>{_esc(change['new'])}</span>"
                         f"<small>{_esc(change['location_label'])}</small></div>")
        for added in diff["added"][:20]:
            lines.append(f"<div class='diff-row'><span class='muted'>新增</span><span>{_esc(added['text'])}</span>"
                         f"<small>{_esc(added['location_label'])}</small></div>")
        for removed_text in diff["removed"][:20]:
            lines.append(f"<div class='diff-row'><span class='muted'>移除</span><span class='diff-old'>{_esc(removed_text)}</span></div>")
        cards.append("<div class='item-card'>" + "".join(lines) + "</div>")
    if not cards:
        return ""
    return ("<div class='card'><h2>文件更新对照（旧发布 vs 新原件）</h2>"
            "<p class='hint-line'>新原件已私有保存并生成新草稿；请逐段核对，核对完成后发布新版（旧版本转历史，不删除）。</p>"
            + "".join(cards) + "</div>")


# ── 由主题浏览树打开的资料详情（受认证下载；不公开地址）─────────────────────

def material_page(handler, query, source_id: str) -> None:
    db = handler.db
    raw = db.conn.execute(
        "SELECT * FROM kb2_sources WHERE tenant_id=? AND id=?", (handler.tenant_id, source_id)).fetchone()
    if not raw:
        handler.send_html(handler.console_page(
            "未找到", f"{_assets()}<div class='kb2'><div class='error'>资料不存在或不属于本企业。</div></div>",
            "knowledge_materials"), 404)
        return
    source = next(
        (item for item in (db.kb2_get_batch(handler.tenant_id, raw["batch_id"]) or {"sources": []})["sources"]
         if item["id"] == source_id), None)
    from_ref = _q1(query, "from")
    show_tree = bool(from_ref)
    topic_id = ""
    if isinstance(from_ref, str) and from_ref.startswith("topic:"):
        topic_id = from_ref.split(":", 1)[1]
    elif from_ref == "topics":
        topic_id = ""
    item = db.get_knowledge_item(handler.tenant_id, raw["item_id"]) if raw["item_id"] else None
    paragraphs = []
    fields = []
    identification_label = "处理中"
    status = "processing"
    if source:
        paragraphs = source["paragraphs"]
        fields = source["fields"]
        identification_label = source["identification_label"]
        status = source["status"]
    body_blocks = []
    for index, paragraph in enumerate(paragraphs, start=1):
        if not paragraph["text"].strip():
            continue
        tags = []
        if paragraph["source_kind"] == "manual":
            tags.append("<span class='tag edit'>人工撰写</span>")
        if paragraph["origin"] == "edited":
            tags.append("<span class='tag edit'>人工修改</span>")
        if paragraph["excluded"]:
            tags.append("<span class='tag gone'>已排除</span>")
        elif paragraph["checked"]:
            tags.append("<span class='tag done'>已核对</span>")
        else:
            tags.append("<span class='tag review'>待核对</span>")
        body_blocks.append(
            f"<div class='body-para' id='p-{index}'><div class='bp-head'><span class='para-no'>第 {index} 段</span>"
            f"<span class='src-label'>{_esc(paragraph['location_label'] or '人工撰写')}</span>{''.join(tags)}</div>"
            f"<p>{_esc(paragraph['text'])}</p></div>")
    if not body_blocks:
        body_blocks.append(
            "<div class='empty empty-inline'><h2>没有可读取的正文</h2>"
            "<p>这件资料可能仅是登记了原件（图片 / 视频 / 网页 / 未能识别）；可以回到所在批次补充人工正文。</p></div>")
    field_items = []
    for field in fields:
        checked_tag = "<span class='tag done'>已核对</span>" if field["checked"] else ""
        field_items.append(
            f"<li><span><strong>{_esc(field['label'])}</strong>：{_esc(field['value'])}{_esc(field['unit'])}</span>"
            f"<small>来源：{_esc(field['paragraph_location_label'] or '资料级')}</small>{checked_tag}</li>")
    fields_rows = "".join(field_items)
    relations = []
    if raw["item_id"]:
        for relation in db.conn.execute(
            "SELECT * FROM kb2_topic_relations WHERE tenant_id=? AND material_kind='knowledge' AND material_id=? "
            "ORDER BY linked_at, id", (handler.tenant_id, raw["item_id"]),
        ):
            topic = db.kb2_get_topic(handler.tenant_id, relation["topic_id"])
            if topic:
                relations.append(f"<a class='tag type' href='{KB2_UI_PREFIX}/topic/{topic['id']}'>{_esc(topic['title'])}</a>")
    item_status = item["status"] if item else None
    item_pill = {"draft": "<span class='pill draft'>草稿</span>",
                 "published": "<span class='pill pub'>已发布</span>",
                 "disabled": "<span class='pill off'>已停用</span>"}.get(item_status, "")
    download = (f"<a class='button secondary' href='{KB2_UI_PREFIX}/source/{source_id}/file'>"
                + _ic("i-file") + "下载原件（企业内认证）</a>" if raw["storage_path"] else
                "<span class='muted'>该资料没有原件文件（手动文字 / 网页地址）</span>")
    back_target = _safe_next(_q1(query, "back"), KB2_UI_PREFIX)
    # 字段页上下文入口（实际可达，不猜 URL）：返回目标只传递经校验的站内目标（无效值不外泄）。
    fields_back = _qs(f"{KB2_UI_PREFIX}/material/{source_id}",
                      **{"from": from_ref or None,
                         "back": back_target if back_target != KB2_UI_PREFIX else None})
    header = (
        f"<div class='detail-top'><a class='back-link' href='{_esc(back_target)}'>{_ic('kb2-back')}返回</a></div>"
        f"<div class='detail-head'><div class='detail-title'><h1>{_esc(raw['title'] or raw['filename'])}</h1>"
        f"<span class='tag type'>{_esc(_FORMAT_LABELS.get(raw['format'], raw['format']))}</span>"
        f"<span class='pill draft'>{_esc(identification_label)}</span>{item_pill}</div>"
        f"<div class='actions'>{download}</div></div>"
        f"<div class='detail-meta'><span>识别与发布分开：登记了原件 ≠ 识别出正文 ≠ 可用于 AI</span>"
        + (f"<span>原件校验 sha256 {_esc(raw['sha256'][:12])}…</span>" if raw["sha256"] else "")
        + "</div>")
    batch_link = ""
    publish_link = ""
    if raw["batch_id"]:
        editor = _editor(handler) is not None
        purpose_row = db._kb2_batch_row(handler.tenant_id, raw["batch_id"])
        purpose_note = "（文件更新）" if purpose_row and purpose_row["purpose"] == "replace_file" else ""
        batch_link = (f"<p class='hint-line'>这件资料属于一个添加批次{purpose_note}："
                      + (f"<a href='{KB2_UI_PREFIX}/batch/{raw['batch_id']}'>回到批次核对 / 发布</a>"
                         if editor else "由有权限的成员在批次中核对与发布。")
                      + "</p>")
        # 当前草稿的发布位置（B2-B08）：历史原件打开的页面不把成员引向会拒绝发布的旧批次。
        if editor and raw["item_id"]:
            context = db.kb2_item_publish_context(handler.tenant_id, raw["item_id"])
            if context and context["draft"]:
                if context["source_id"] == source_id:
                    publish_link = (f"<p class='hint-line'>当前草稿尚未发布："
                                    f"<a href='{KB2_UI_PREFIX}/batch/{context['batch_id']}?step=publish'>去发布设置</a></p>")
                else:
                    publish_link = (
                        f"<p class='hint-line'>当前草稿来自更新版本（{_esc(context['source_title'])}）："
                        f"<a href='{KB2_UI_PREFIX}/batch/{context['batch_id']}?step=publish'>去当前批次的发布设置</a>；"
                        "历史批次发布不会覆盖当前草稿。</p>")
    related = ("<p class='sub-head'>关联主题</p><div class='chiprow'>"
               + ("".join(relations) if relations else "<span class='muted'>未关联主题</span>") + "</div>")
    editor = _editor(handler) is not None
    flexible = ""
    replace_form = ""
    if raw["item_id"]:
        binding = db._kb2_item_binding(handler.tenant_id, raw["item_id"])
        field_count = db.conn.execute(
            "SELECT COUNT(*) FROM kb2_item_fields WHERE tenant_id=? AND item_id=? AND removed=0",
            (handler.tenant_id, raw["item_id"])).fetchone()[0]
        flexible = (
            "<div class='topic-block'><div class='section-head'><h2>灵活字段</h2>"
            "<span class='sec-note'>"
            + ("按模板录入的业务字段（可核对、可允许客户问答引用）" if binding else "尚未采用模板：模板是可选的录入表")
            + "</span></div>"
            + (f"<p class='hint-line'>已采用模板 · {field_count} 个字段</p>" if binding else "")
            + (f"<p><a class='button secondary' href='{KB2_UI_PREFIX}/fields/{raw['item_id']}?"
               + urlencode({"back": fields_back}) + "'>管理灵活字段</a>"
               f"<a class='linkbtn' href='{KB2_UI_PREFIX}/templates'>知识模板</a></p>"
               if editor else "<p class='muted'>字段由有权限的成员管理。</p>")
            + "</div>")
        if editor and item is not None:
            if item["draft_version_id"]:
                replace_form = ("<div class='card'><h2>更新文件</h2>"
                                "<p class='muted'>该资料已有未发布草稿：请先发布或继续整理现有草稿，再更新文件（避免人工修改被静默覆盖）。</p></div>")
            elif item["status"] == "published":
                replace_form = (
                    "<div class='card'><h2>更新文件</h2>"
                    "<p class='hint-line'>上传新文件会登记为新原件并生成新草稿：旧线上版本在成员核对并发布前继续有效；"
                    "资料 ID、模板字段与历史保留；发布前可查看位置与文本差异对照。</p>"
                    f"<form method='post' enctype='multipart/form-data' action='{KB2_UI_PREFIX}/item/{raw['item_id']}/replace-file'>"
                    "<label for='kb2-replace-file'>选择新文件（TXT / Markdown / CSV / PDF / DOCX，≤10MB）</label>"
                    "<input id='kb2-replace-file' type='file' name='replace_file' required>"
                    "<button class='button secondary'>登记新原件并生成新草稿</button></form></div>")
    detail = (_notice_html(query) + header
              + "<div class='topic-block'><div class='section-head'><h2>正文（整理结果）</h2></div>"
              + "".join(body_blocks) + batch_link + publish_link + "</div>"
              + (("<div class='topic-block'><p class='sub-head'>可选字段</p><ul class='field-list'>" + fields_rows + "</ul></div>")
                 if fields_rows else "")
              + flexible
              + "<div class='topic-block'>" + related + "</div>"
              + replace_form)
    if show_tree:
        tree = _tree_data(handler)
        detail = _workspace(detail, tree, active_topic=topic_id, active_source=source_id)
    _ok(handler, detail, "knowledge_materials", raw["title"] or raw["filename"])


# ── 原件下载（受认证的企业下载）/ 静态资源 ─────────────────────────────────

def download_source(handler, source_id: str) -> None:
    db = handler.db
    source = db.conn.execute(
        "SELECT * FROM kb2_sources WHERE tenant_id=? AND id=?", (handler.tenant_id, source_id)).fetchone()
    if not source or not source["storage_path"]:
        handler.send_error(404)
        return
    import hashlib
    from pathlib import Path as _Path
    target = _Path(source["storage_path"])
    if not target.is_file():
        handler.send_error(404)
        return
    content = target.read_bytes()
    if source["sha256"] and hashlib.sha256(content).hexdigest() != source["sha256"]:
        handler.send_error(409)
        return
    db._audit(handler.tenant_id, "kb2_source", source_id, "kb2_source_file_read", handler.actor, "")
    db.conn.commit()
    content_type = {
        "pdf": "application/pdf", "image": "image/png", "web": "application/octet-stream",
    }.get(source["format"], "application/octet-stream")
    inline = source["format"] in ("pdf", "image")
    handler.send_response(200)
    handler.send_header("Content-Type", content_type)
    handler.send_header("Content-Disposition",
                        ("inline" if inline else "attachment") + "; filename*=UTF-8''" + quote(source["filename"] or "file"))
    handler.send_header("Cache-Control", "no-store")
    handler.send_header("Content-Length", str(len(content)))
    handler.end_headers()
    handler.wfile.write(content)


def serve_asset(handler, filename: str) -> None:
    from app import ROOT
    if filename not in ("knowledge-phase2.css", "knowledge-phase2.js"):
        handler.send_error(404)
        return
    target = ROOT / "static" / filename
    if not target.is_file():
        handler.send_error(404)
        return
    content = target.read_bytes()
    content_type = "text/css; charset=utf-8" if filename.endswith(".css") else "text/javascript; charset=utf-8"
    handler.send_response(200)
    handler.send_header("Content-Type", content_type)
    handler.send_header("Cache-Control", "no-cache")
    handler.send_header("Content-Length", str(len(content)))
    handler.end_headers()
    handler.wfile.write(content)


# ── 路由：get / post（由既有 KnowledgeUI 先调用；旧路径返回 False 保持兼容）────

def _topic_new_page(handler, query) -> None:
    if _editor(handler) is None:
        handler._kb_forbidden()
        return
    groups = handler.db.kb2_list_groups(handler.tenant_id)
    options = "".join(f"<option value='{group['id']}'>{_esc(group['name'])}</option>" for group in groups)
    body = (
        f"<div class='detail-top'><a class='back-link' href='{KB2_UI_PREFIX}'>{_ic('kb2-back')}返回全部主题</a></div>"
        "<div class='toolbar'><div><h1>新建主题</h1>"
        "<p class='lead'>建立后是草稿：可以手动写正文，或关联资料后按已核对段落整理初稿；核对后发布。</p></div></div>"
        f"<div class='card'><form method='post' action='{KB2_UI_PREFIX}/topic/new'>"
        "<label for='kb2-topic-title'>主题名称</label>"
        "<input id='kb2-topic-title' name='title' placeholder='例如：安装与调试' required>"
        "<label for='kb2-topic-group'>分组（可选）</label>"
        f"<select id='kb2-topic-group' name='group_id'><option value=''>不分组</option>{options}</select>"
        "<div class='review-footer'><span class='hint-line'>分组只辅助导航。</span>"
        "<span class='actions'><button class='button'>建立主题</button></span></div></form></div>")
    _ok(handler, body, "knowledge", "新建主题")


# ── 知识模板管理（任务4）：字段定义 / 版本不可变 / 采用对照（仅编辑权限）────────
# 设计依据 v0.2 §7：模板 = 企业自定义录入表；旧知识继续用原模板版本，升级须人工确认且只改草稿。

_KB2_FIELD_TYPE_OPTS = (
    ("text", "单行文字"), ("textarea", "多行文字"), ("number", "数字＋单位"),
    ("select", "选项"), ("date", "日期"), ("bool", "是/否"),
)


def _template_form_fields(form) -> list[dict]:
    """从模板编辑表单解析字段行（空行跳过；已有身份用 hidden key 保持）。"""
    fields = []
    index = 0
    while f"f_{index}_label" in form:
        if f"f_{index}_remove" in form:
            index += 1
            continue
        label = _form_text(form, f"f_{index}_label").strip()
        key = _form_text(form, f"f_{index}_key").strip()
        if not label and not key:
            index += 1
            continue
        fields.append({
            "key": key or None,
            "label": label,
            "type": _form_text(form, f"f_{index}_type") or "text",
            "required": f"f_{index}_required" in form,
            "hint": _form_text(form, f"f_{index}_hint"),
            "unit": _form_text(form, f"f_{index}_unit"),
            "options": [part.strip() for part in _form_text(form, f"f_{index}_options").split("|")],
        })
        index += 1
    return fields


def _template_field_rows_html(fields: list[dict], start: int = 0) -> str:
    rows = []
    for offset, field in enumerate(fields):
        index = start + offset
        options = "|".join(field.get("options") or [])
        type_options = "".join(
            f"<option value='{value}'{' selected' if field.get('type') == value else ''}>{label}</option>"
            for value, label in _KB2_FIELD_TYPE_OPTS)
        rows.append(
            f"<div class='tpl-row'>"
            f"<input type='hidden' name='f_{index}_key' value='{_esc(field.get('key') or '')}'>"
            f"<input name='f_{index}_label' value='{_esc(field.get('label') or '')}' placeholder='字段名称' aria-label='字段名称'>"
            f"<select name='f_{index}_type' aria-label='字段类型'>{type_options}</select>"
            f"<input name='f_{index}_unit' value='{_esc(field.get('unit') or '')}' placeholder='单位（数字字段）' aria-label='单位'>"
            f"<input name='f_{index}_hint' value='{_esc(field.get('hint') or '')}' placeholder='填写提示' aria-label='填写提示'>"
            f"<input name='f_{index}_options' value='{_esc(options)}' placeholder='选项用 | 分隔（选项字段至少两个）' aria-label='选项'>"
            f"<label class='check-inline'><input type='checkbox' name='f_{index}_required' value='1'{' checked' if field.get('required') else ''}> <span>必填</span></label>"
            f"<label class='check-inline'><input type='checkbox' name='f_{index}_remove' value='1'> <span>移除</span></label>"
            "</div>")
    return "".join(rows)


def _blank_template_rows(start: int, count: int = 6) -> str:
    rows = []
    for index in range(start, start + count):
        rows.append(
            f"<div class='tpl-row'><input type='hidden' name='f_{index}_key' value=''>"
            f"<input name='f_{index}_label' placeholder='字段名称' aria-label='字段名称'>"
            f"<select name='f_{index}_type' aria-label='字段类型'>"
            + "".join(f"<option value='{value}'>{label}</option>" for value, label in _KB2_FIELD_TYPE_OPTS)
            + "</select>"
            f"<input name='f_{index}_unit' placeholder='单位（数字字段）' aria-label='单位'>"
            f"<input name='f_{index}_hint' placeholder='填写提示' aria-label='填写提示'>"
            f"<input name='f_{index}_options' placeholder='选项用 | 分隔（选项字段至少两个）' aria-label='选项'>"
            "<label class='check-inline'><input type='checkbox' name='f_{index}_required' value='1'> <span>必填</span></label>"
            "</div>")
    return "".join(rows)


def templates_page(handler, query) -> None:
    if _editor(handler) is None:
        handler._kb_forbidden()
        return
    db = handler.db
    templates = db.kb2_list_templates(handler.tenant_id)
    rows = []
    for template in templates:
        rows.append(
            f"<div class='row'><a class='row-main' href='{KB2_UI_PREFIX}/templates/{template['id']}'>"
            f"<span class='row-title'><strong>{_esc(template['name'])}</strong></span>"
            f"<span class='row-meta'>最新版本 v{template['latest_version']} · {template['field_count']} 个字段 · "
            f"{template['version_count']} 个版本</span></a>"
            f"<div class='row-side'><a class='linkbtn' href='{KB2_UI_PREFIX}/templates/{template['id']}'>编辑（保存为新版本）</a></div></div>")
    body = (
        _notice_html(query)
        + "<div class='toolbar'><div><h1>知识模板</h1>"
        "<p class='lead'>模板是企业自己的录入表：定义额外业务字段（文字 / 数字＋单位 / 选项 / 日期 / 是/否）。"
        "修改模板会保存为新版本；已有资料继续使用原版本，升级需要人工确认并只作用于草稿。</p></div></div>"
        + "<div class='card'><h2>模板列表</h2>"
        + (f"<div class='rows'>{''.join(rows)}</div>" if rows else "<p class='muted'>还没有模板。新建一个模板，上传资料时即可采用。</p>")
        + "</div>"
        + "<div class='card'><h2>新建模板</h2>"
        f"<form method='post' action='{KB2_UI_PREFIX}/templates/new'>"
        "<label for='kb2-tpl-name'>模板名称</label>"
        "<input id='kb2-tpl-name' name='name' placeholder='例如：服务方案' required>"
        "<p class='sub-head'>字段（最多 30 个；选项字段用 | 分隔至少两个选项）</p>"
        f"<div class='tpl-editor' data-kb2-fold-region>{_blank_template_rows(0)}</div>"
        "<div class='review-footer'><span class='hint-line'>保存即成为模板 v1；之后每次保存都会追加新版本，旧版本不会被改写。</span>"
        "<span class='actions'><button class='button'>保存模板</button></span></div></form></div>")
    _ok(handler, body, "knowledge_templates", "知识模板")


def template_edit_page(handler, query, template_id: str) -> None:
    if _editor(handler) is None:
        handler._kb_forbidden()
        return
    db = handler.db
    template = db.kb2_get_template(handler.tenant_id, template_id)
    if not template:
        handler.send_html(handler.console_page(
            "未找到", f"{_assets()}<div class='kb2'><div class='error'>模板不存在或不属于本企业。</div>"
            f"<p><a href='{KB2_UI_PREFIX}/templates'>返回知识模板</a></p></div>", "knowledge_templates"), 404)
        return
    latest = template["versions"][0] if template["versions"] else {"fields": [], "version": 0}
    versions_html = "".join(
        f"<li><span class='vtag'>v{version['version']}</span><span>{len(version['fields'])} 个字段</span>"
        f"<span class='muted'>{_esc(version['created_by'])} · {_esc(str(version['created_at'])[:10])}</span></li>"
        for version in template["versions"])
    body = (
        _notice_html(query)
        + f"<div class='detail-top'><a class='back-link' href='{KB2_UI_PREFIX}/templates'>{_ic('kb2-back')}返回知识模板</a></div>"
        + f"<div class='toolbar'><div><h1>{_esc(template['name'])}</h1>"
        f"<p class='lead'>当前最新版本 v{latest['version']}。保存会追加新版本（旧版本不可变）；"
        "资料仍沿用自己采用的版本，升级由资料页的人工确认完成。</p></div></div>"
        + "<div class='card'><h2>字段定义（保存为新版本）</h2>"
        f"<form method='post' action='{KB2_UI_PREFIX}/templates/{template_id}/save'>"
        "<label for='kb2-tpl-name'>模板名称</label>"
        f"<input id='kb2-tpl-name' name='name' value='{_esc(template['name'])}' required>"
        "<p class='sub-head'>字段（已有字段保留 hidden key 以保持身份；改类型将按新字段处理，旧值不自动转换）</p>"
        f"<div class='tpl-editor' data-kb2-fold-region>{_template_field_rows_html(latest['fields'])}{_blank_template_rows(len(latest['fields']))}</div>"
        "<div class='review-footer'><span class='hint-line'>保存后成为 v" + str(latest['version'] + 1) + "；已有资料不会自动改变。</span>"
        "<span class='actions'><button class='button'>保存为新版本</button></span></div></form></div>"
        + "<details class='section-fold'><summary>版本记录<span class='fold-count'>" + str(len(template['versions'])) + "</span></summary>"
        "<div class='fold-body'><ul class='ver-list'>" + versions_html + "</ul>"
        "<p class='hint-line'>模板版本不可变：每次保存都追加，不覆盖历史。</p></div></details>")
    _ok(handler, body, "knowledge_templates", template["name"])


# ── 资料灵活字段（任务4）：采用模板 / 填写字段 / 采用新版对照（人工确认）────────

def _fields_adopt_form(handler, item_id: str, *, template_version_id: str = "", stage: str = "preview",
                       label: str = "采用模板") -> str:
    return (
        f"<form method='post' action='{KB2_UI_PREFIX}/fields/{item_id}/adopt'>"
        f"<input type='hidden' name='template_version_id' value='{_esc(template_version_id)}'>"
        f"<input type='hidden' name='stage' value='{stage}'>"
        f"<button class='button secondary'>{_esc(label)}</button></form>")


def fields_page(handler, query, item_id: str) -> None:
    if _editor(handler) is None:
        handler._kb_forbidden()
        return
    db = handler.db
    try:
        view = db.kb2_list_item_fields(handler.tenant_id, item_id)
    except ValueError:
        handler.send_html(handler.console_page(
            "未找到", f"{_assets()}<div class='kb2'><div class='error'>资料不存在或不属于本企业。</div></div>",
            "knowledge_materials"), 404)
        return
    preview_version_id = str(_q1(query, "preview") or "")
    default_back = f"{KB2_UI_PREFIX}?view=materials"
    back_target = _safe_next(_q1(query, "back"), default_back)
    back_label = "返回全部资料" if back_target == default_back else "返回"
    parts = [
        _notice_html(query),
        f"<div class='detail-top'><a class='back-link' href='{_esc(back_target)}'>{_ic('kb2-back')}{back_label}</a></div>",
        f"<div class='toolbar'><div><h1>灵活字段 · {_esc(view['item_title'] or '资料')}</h1>"
        "<p class='lead'>采用模板后可以按类型填写字段；每个字段必须写出来源（来源段落或人工填写）。"
        "0 与「否」是有效值；未填写与 0 不同。修改字段只影响草稿，发布时冻结为发布快照。</p></div></div>",
    ]
    template_view = view["template"]
    # 采用 / 升级对照区
    templates = db.kb2_list_templates(handler.tenant_id)
    adopt_forms = []
    if template_view is None:
        if templates:
            options = "".join(
                f"<option value='{template['latest_version_id']}'>{_esc(template['name'])}（v{template['latest_version']}）</option>"
                for template in templates)
            adopt_forms.append(
                f"<div class='attach-bar'><form method='post' action='{KB2_UI_PREFIX}/fields/{item_id}/adopt'>"
                f"<input type='hidden' name='stage' value='preview'>"
                f"<label for='kb2-adopt'>采用模板</label>"
                f"<select id='kb2-adopt' name='template_version_id' required>"
                f"<option value=''>选择模板…</option>{options}</select>"
                "<button class='button secondary'>查看对照并采用</button></form></div>")
        else:
            adopt_forms.append(
                f"<p class='muted'>还没有模板：先到 <a href='{KB2_UI_PREFIX}/templates'>知识模板</a> 建立字段定义。</p>")
    else:
        current = f"正在使用模板「{_esc(template_view['name'])}」v{template_view['template_version']}"
        if template_view["latest_version_id"] != template_view["template_version_id"]:
            current += f"；已有新版 v{template_view['latest_version']}"
            adopt_forms.append(_fields_adopt_form(
                handler, item_id, template_version_id=template_view["latest_version_id"],
                stage="preview", label=f"采用新版 v{template_view['latest_version']}（先看对照）"))
        else:
            current += "（已是最新版本）"
        adopt_forms.insert(0, f"<p class='hint-line'>{current}。升级只改草稿并保留旧值历史，不强迫升级。</p>")
    if preview_version_id:
        diff = db.kb2_adopt_template(handler.tenant_id, handler.user_id, item_id,
                                     preview_version_id)["diff"]
        lines = [f"<p class='sub-head'>采用对照（{_esc(diff['template_name'])} v{diff['template_version']}）："
                 f"新增 {len(diff['added'])} · 移除 {len(diff['removed'])} · 改类型 {len(diff['type_changed'])} · "
                 f"改名 {len(diff['renamed'])} · 保留 {diff['kept']}</p>"]
        if diff["added"]:
            lines.append("<p class='muted'>新增字段：" + "、".join(_esc(f["label"]) for f in diff["added"]) + "</p>")
        if diff["removed"]:
            lines.append("<p class='muted'>移除字段（旧值保留在历史）：" + "、".join(_esc(f["label"]) for f in diff["removed"]) + "</p>")
        if diff["type_changed"]:
            lines.append("<p class='muted'>改类型（按新字段处理，旧值不自动转换）："
                         + "、".join(f"{_esc(f['label'])}（{_esc(f['old_type'])} → {_esc(f['new_type'])}）"
                                     for f in diff["type_changed"]) + "</p>")
        if diff["missing_required"]:
            lines.append("<p class='muted'>待补齐的必填项（只在发布时校验）："
                         + "、".join(_esc(f["label"]) for f in diff["missing_required"]) + "</p>")
        confirm_form = _fields_adopt_form(handler, item_id, template_version_id=preview_version_id,
                                          stage="confirm", label="确认采用（只更新草稿）")
        lines.append(f"<div class='review-footer'><span class='hint-line'>取消不改任何值；确认后不动发布版本与原件。</span>"
                     f"<span class='actions'>{confirm_form}</span></div>")
        parts.append("<div class='card'><h2>采用模板对照</h2>" + "".join(lines) + "</div>")
    # 模板管理保持上下文可达（即使已采用模板：冻结四入口侧栏不含模板页，从这里进入管理）。
    management_line = (f"<p class='hint-line'><a href='{KB2_UI_PREFIX}/templates'>管理知识模板</a>："
                       "修改保存为新版本；已有资料继续使用原版本，升级需要人工确认并只作用于草稿。</p>")
    # 当前草稿的发布位置（B2-B08）：按草稿真实归属给出，不把用户带向被替换的历史批次。
    publish_note = ""
    context = db.kb2_item_publish_context(handler.tenant_id, item_id)
    if context and context["draft"]:
        publish_note = (
            f"<p class='hint-line'>当前草稿尚未发布："
            f"<a href='{KB2_UI_PREFIX}/batch/{context['batch_id']}?step=publish'>去当前批次的发布设置</a>"
            "（发布前仍会校验正文核对与必填字段）。</p>")
    parts.append("<div class='card'><h2>模板与版本</h2>" + "".join(adopt_forms)
                 + management_line + publish_note + "</div>")
    # 字段编辑
    field_cards = []
    source_options = view["source_options"]
    for field in view["fields"]:
        field_type = field["type"]
        value = field["value"]
        if field_type == "textarea":
            value_widget = f"<textarea name='value' placeholder='{_esc(field['hint'] or '填写内容')}'>{_esc(value or '')}</textarea>"
        elif field_type == "number":
            value_widget = (f"<input name='value' inputmode='decimal' value='{_esc(value or '')}' placeholder='数字（0 是有效值）'>"
                            f"<input name='unit' value='{_esc(field['unit'])}' placeholder='单位'>")
        elif field_type == "select":
            options = "".join(f"<option value='{_esc(option)}'{' selected' if value == option else ''}>{_esc(option)}</option>"
                              for option in field["options"])
            value_widget = f"<select name='value'><option value=''>{'未选择' if value is None else '清除（未选择）'}</option>{options}</select>"
        elif field_type == "date":
            value_widget = f"<input name='value' type='date' value='{_esc(value or '')}'>"
        elif field_type == "bool":
            selected_true = " selected" if value == "true" else ""
            selected_false = " selected" if value == "false" else ""
            value_widget = ("<select name='value'><option value=''>未选择</option>"
                            f"<option value='true'{selected_true}>是</option>"
                            f"<option value='false'{selected_false}>否</option></select>")
        else:
            value_widget = f"<input name='value' value='{_esc(value or '')}' placeholder='{_esc(field['hint'] or '填写内容')}'>"
        pin = field["source"] or {}
        paragraph_options = "".join(
            f"<option value='{option['paragraph_id']}' data-source='{option['source_id']}'"
            + (" selected" if pin.get("paragraph_id") == option["paragraph_id"] else "")
            + f">{_esc(option['source_title'])} · {_esc(option['location_label'] or '资料级')}</option>"
            for option in source_options)
        source_label = (pin.get("label") or ("人工填写" if pin.get("kind") == "manual" else "未写出来源"))
        field_cards.append(
            f"<div class='para-card'><div class='para-head'><span class='para-no'>{_esc(field['label'])}</span>"
            f"<span class='tag type'>{_esc(field['type_label'])}</span>"
            + ("<span class='tag type'>必填</span>" if field["required"] else "")
            + ("<span class='tag done'>已核对</span>" if field["checked"] else "<span class='tag review'>未核对</span>")
            + ("<span class='tag type'>允许客户问答引用</span>" if field["allow_customer"] else "")
            + f"<span class='src-label'>来源：{_esc(source_label)}</span></div>"
            + f"<form method='post' action='{KB2_UI_PREFIX}/fields/{item_id}/field'>"
            f"<input type='hidden' name='field_key' value='{_esc(field['field_key'])}'>"
            + (f"<p class='hint-line'>{_esc(field['hint'])}</p>" if field["hint"] else "")
            + "<div class='inline-row'>" + value_widget + "</div>"
            + "<div class='inline-row'><select name='source_kind' aria-label='字段来源'>"
            + f"<option value='manual'{' selected' if pin.get('kind') == 'manual' else ''}>人工填写</option>"
            + f"<option value='source'{' selected' if pin.get('kind') == 'source' else ''}>来源段落</option>"
            + "</select><select name='source_paragraph' aria-label='来源段落'>"
            + (f"<option value=''>选择来源段落…</option>{paragraph_options}" if source_options else "<option value=''>（本资料暂无可选段落）</option>")
            + "</select></div>"
            + "<div class='chiprow'>"
            + f"<label class='check-inline'><input type='checkbox' name='checked' value='1'{' checked' if field['checked'] else ''}> <span>已核对</span></label>"
            + f"<label class='check-inline'><input type='checkbox' name='allow_customer' value='1'{' checked' if field['allow_customer'] else ''}> <span>允许客户问答引用</span></label>"
            + "</div>"
            "<div class='review-footer'><span class='hint-line'>改值或来源会撤销该字段的核对。</span>"
            "<span class='actions'><button class='button secondary'>保存字段</button></span></div></form></div>")
    if not field_cards:
        field_cards.append("<p class='muted'>采用模板后，字段会在这里出现。</p>")
    parts.append("<div class='card'><h2>字段</h2>" + "".join(field_cards) + "</div>")
    _ok(handler, "".join(parts), "knowledge_templates", f"灵活字段 · {view['item_title']}")


def get(handler, path, query) -> bool:
    """新的第二批 GET 路由；不属于本模块（含第一批旧视图）返回 False。"""
    if not path.startswith(KB2_UI_PREFIX):
        return False
    match = re.fullmatch(KB2_UI_PREFIX + r"/assets/(knowledge-phase2\.(?:css|js))", path)
    if match:
        serve_asset(handler, match.group(1))
        return True
    match = re.fullmatch(KB2_UI_PREFIX + r"/source/([a-f0-9]{8,64})/file", path)
    if match:
        download_source(handler, match.group(1))
        return True
    if path == KB2_UI_PREFIX:
        view = str(_q1(query, "view") or "")
        if view in KB2_OLD_LIST_VIEWS:
            return False  # 第一批「产品与服务 / 文章与 FAQ / 文件资料」列表保持兼容
        if view == "materials":
            materials_page(handler, query)
        else:
            topics_page(handler, query)
        return True
    if path == KB2_UI_PREFIX + "/add":
        add_page(handler, query)
        return True
    if path == KB2_UI_PREFIX + "/topic/new":
        _topic_new_page(handler, query)
        return True
    match = re.fullmatch(KB2_UI_PREFIX + r"/topic/([a-f0-9]{8,64})/edit", path)
    if match:
        topic_edit_page(handler, query, match.group(1))
        return True
    match = re.fullmatch(KB2_UI_PREFIX + r"/topic/([a-f0-9]{8,64})", path)
    if match:
        topic_detail_page(handler, query, match.group(1))
        return True
    match = re.fullmatch(KB2_UI_PREFIX + r"/batch/([a-f0-9]{8,64})", path)
    if match:
        step = 3 if str(_q1(query, "step") or "") == "publish" else 2
        batch_page(handler, query, match.group(1), step=step)
        return True
    match = re.fullmatch(KB2_UI_PREFIX + r"/material/([a-f0-9]{8,64})", path)
    if match:
        material_page(handler, query, match.group(1))
        return True
    if path == KB2_UI_PREFIX + "/templates":
        templates_page(handler, query)
        return True
    match = re.fullmatch(KB2_UI_PREFIX + r"/templates/([a-f0-9]{8,64})", path)
    if match:
        template_edit_page(handler, query, match.group(1))
        return True
    match = re.fullmatch(KB2_UI_PREFIX + r"/fields/([a-f0-9]{8,64})", path)
    if match:
        fields_page(handler, query, match.group(1))
        return True
    return False


# ── POST 处理 ─────────────────────────────────────────────────────────────

def _form_text(form, key, default="") -> str:
    value = form.get(key, default)
    if isinstance(value, bytes):
        value = value.decode("utf-8", errors="replace")
    return str(value if value is not None else default)


def _topic_next(handler, form, topic_id: str) -> str:
    return _safe_next(form.get("next"), f"{KB2_UI_PREFIX}/topic/{topic_id}")


def _expiry_from_form(form):
    """第二批界面的到期表单口径：日期只填日期 → 北京时间当日 23:59:59；日期+时间 → 北京时间精确时刻；
    全空 → 长期有效（None）；只填时间没有日期 → 明确报错（不能静默当长期有效）。
    归一化仍走既有 normalize_knowledge_expiry（无时区标注按北京时间解释；旧存储格式兼容不变）。"""
    date = _form_text(form, "expiry_date").strip()
    time_value = _form_text(form, "expiry_time").strip()
    if not date and not time_value:
        return None
    if not date:
        raise ValueError("到期时间需要先选择日期：只填时间不能保存（留空表示长期有效）。")
    if not time_value:
        return f"{date}T23:59:59"
    return f"{date}T{time_value}"


def _add_post(handler, form) -> None:
    files = [(name, filename, content) for name, filename, content
             in (form.get("__files__") or []) if name == "source_file" and content]
    selected_tab = "file" if files else ("link" if _form_text(form, "link_url").strip()
                                         else ("manual" if _form_text(form, "manual_text").strip() else "file"))
    values = {
        "tab": selected_tab, "file_title": _form_text(form, "file_title").strip(),
        "url": _form_text(form, "link_url").strip(), "link_title": _form_text(form, "link_title").strip(),
        "text": _form_text(form, "manual_text"), "manual_title": _form_text(form, "manual_title").strip(),
        "filename": ", ".join(filename for _, filename, _ in files),
    }
    entries = [{"kind": "file", "filename": filename, "data": content,
                "title": values["file_title"]} for _, filename, content in files]
    if values["url"]:
        entries.append({"kind": "link", "url": values["url"], "title": values["link_title"]})
    if values["text"].strip():
        entries.append({"kind": "manual", "text": values["text"], "title": values["manual_title"]})
    if not entries:
        add_page(handler, {"tab": [selected_tab]}, error="请先选择文件、登记网页地址或添加手动文字。", values=values)
        return
    try:
        batch_id = handler.db.kb2_create_batch(handler.tenant_id, handler.user_id, entries, process_now=False)
    except (ValueError, PermissionError) as error:
        add_page(handler, {"tab": [selected_tab]}, error=str(error), values=values)
        return
    _redirect(handler, f"{KB2_UI_PREFIX}/batch/{batch_id}",
              notice="已添加：原件已在后台保存并处理；下面可以开始检查整理结果。")


def _batch_post(handler, path, form) -> bool:
    db = handler.db
    match = re.fullmatch(KB2_UI_PREFIX + r"/batch/([a-f0-9]{8,64})/(.+)", path)
    if not match:
        return False
    batch_id, action = match.groups()
    batch = db.kb2_get_batch(handler.tenant_id, batch_id)
    if not batch:
        handler.send_error(404)
        return True
    back = f"{KB2_UI_PREFIX}/batch/{batch_id}"

    def source_view(source_id):
        return next((source for source in batch["sources"] if source["id"] == source_id), None)

    try:
        if action == "paragraphs":
            source = source_view(_form_text(form, "source_id"))
            if not source:
                raise ValueError("资料不存在或不属于该批次")
            updated = 0
            for paragraph in source["paragraphs"]:
                paragraph_id = paragraph["id"]
                new_text = _form_text(form, f"p_{paragraph_id}_text", paragraph["text"])
                checked = f"p_{paragraph_id}_checked" in form
                excluded = f"p_{paragraph_id}_excluded" in form
                if new_text == paragraph["text"] and checked == paragraph["checked"] and excluded == paragraph["excluded"]:
                    continue
                db.kb2_update_paragraph(handler.tenant_id, handler.user_id, paragraph_id,
                                        text=new_text, checked=checked, excluded=excluded)
                updated += 1
            _redirect(handler, back, notice=f"已保存核对结果（更新 {updated} 段）。" if updated else "没有需要保存的改动。")
            return True
        if action == "paragraph-add":
            source = source_view(_form_text(form, "source_id"))
            if not source:
                raise ValueError("资料不存在或不属于该批次")
            db.kb2_add_manual_paragraph(handler.tenant_id, handler.user_id, source["id"], _form_text(form, "text"))
            _redirect(handler, back, notice="已添加人工正文（发布前需要核对）。")
            return True
        if action == "field":
            source = source_view(_form_text(form, "source_id"))
            if not source:
                raise ValueError("资料不存在或不属于该批次")
            paragraph_id = _form_text(form, "paragraph_id").strip() or None
            db.kb2_set_source_field(
                handler.tenant_id, handler.user_id, source["id"],
                label=_form_text(form, "label"), value=_form_text(form, "value"),
                unit=_form_text(form, "unit"), paragraph_id=paragraph_id)
            _redirect(handler, back, notice="字段已保存（来源固定到资料与位置）。")
            return True
        if action == "suggestions/build":
            suggestions = db.kb2_build_topic_suggestions(handler.tenant_id, handler.user_id, batch_id)
            pending = len([item for item in suggestions if item["status"] == "pending"])
            _redirect(handler, back,
                      notice=f"已生成系统建议：待确认 {pending} 条（现有主题文字命中 + 依据资料标题/标题段落的新主题候选；确认前不会建立主题）。")
            return True
        if action == "suggestion-add":
            name = _form_text(form, "name").strip()
            db.kb2_add_topic_suggestion(handler.tenant_id, handler.user_id, batch_id, name)
            _redirect(handler, back, notice=f"已添加建议「{name}」：确认后才建立主题。")
            return True
        if action == "confirm-topics":
            result = db.kb2_confirm_batch_topics(handler.tenant_id, handler.user_id, batch_id)
            created = "、".join(topic["title"] for topic in result["topics_created"]) or "无新主题"
            _redirect(handler, back, notice=f"已确认主题与关联：新建立 {created}；新增关联 {result['relations_created']} 条。")
            return True
        if action == "settings":
            usage = _form_text(form, "usage") or "internal"
            ai = "ai_authorized" in form
            expires = _expiry_from_form(form)
            if _form_text(form, "action") == "publish":
                result = db.kb2_publish_batch(
                    handler.tenant_id, handler.user_id, batch_id, usage=usage, ai_authorized=ai,
                    expires_at=expires, confirm_ai=_form_text(form, "confirm_ai") == "1")
                if result["failed"]:
                    reasons = "；".join(entry["error"] for entry in result["failed"][:3])
                    _redirect(handler, f"{back}?step=publish",
                              notice=f"部分已发布（发布 {len(result['published'])}、归档 {len(result['archived'])}、跳过 {len(result['skipped'])}）。",
                              error=f"未通过：{reasons}")
                else:
                    _redirect(handler, back,
                              notice=f"已发布：正文发布 {len(result['published'])} 件、归档 {len(result['archived'])} 件、跳过 {len(result['skipped'])} 件。")
                return True
            db.kb2_save_batch_settings(handler.tenant_id, handler.user_id, batch_id,
                                       usage=usage, ai_authorized=ai, expires_at=expires)
            _redirect(handler, back, notice="已保存发布设置（草稿，可稍后发布）。")
            return True
        match_retry = re.fullmatch(r"source/([a-f0-9]{8,64})/retry", action)
        if match_retry:
            db.kb2_retry_source(handler.tenant_id, handler.user_id, match_retry.group(1))
            _redirect(handler, back, notice="已重新处理该资料。")
            return True
        match_suggestion = re.fullmatch(r"suggestion/([a-f0-9]{8,64})/(rename|merge|reject|sources)", action)
        if match_suggestion:
            suggestion_id, sub = match_suggestion.groups()
            if sub == "rename":
                db.kb2_rename_topic_suggestion(handler.tenant_id, handler.user_id, suggestion_id,
                                               _form_text(form, "name"))
                _redirect(handler, back, notice="建议已改名（确认前不建立主题）。")
            elif sub == "merge":
                db.kb2_merge_topic_suggestion(handler.tenant_id, handler.user_id, suggestion_id,
                                              _form_text(form, "topic_id"))
                _redirect(handler, back, notice="建议已合并到已有主题（确认后建立关联）。")
            elif sub == "reject":
                db.kb2_reject_topic_suggestion(handler.tenant_id, handler.user_id, suggestion_id)
                _redirect(handler, back, notice="建议已拒绝：不会被自动复活。")
            else:
                raw_ids = form.get("source_ids")
                if raw_ids is None:
                    source_ids = []
                elif isinstance(raw_ids, list):
                    source_ids = [_form_text({"v": item}, "v") for item in raw_ids]
                else:
                    source_ids = [_form_text({"v": raw_ids}, "v")]
                db.kb2_set_suggestion_sources(handler.tenant_id, handler.user_id, suggestion_id, source_ids)
                _redirect(handler, back, notice="已保存建议的资料范围。")
            return True
        return False
    except (ValueError, PermissionError) as error:
        _redirect(handler, back, error=str(error))
        return True


def _topic_post(handler, path, form) -> bool:
    db = handler.db
    if path == KB2_UI_PREFIX + "/topic/new":
        try:
            topic_id = db.kb2_create_topic(handler.tenant_id, handler.user_id,
                                           _form_text(form, "title"),
                                           _form_text(form, "group_id").strip() or None)
        except (ValueError, PermissionError) as error:
            _redirect(handler, KB2_UI_PREFIX, error=str(error))
            return True
        _redirect(handler, f"{KB2_UI_PREFIX}/topic/{topic_id}", notice="主题已建立（草稿）：编辑正文并逐段核对后发布。")
        return True
    match = re.fullmatch(KB2_UI_PREFIX + r"/topic/([a-f0-9]{8,64})/(.+)", path)
    if not match:
        return False
    topic_id, action = match.groups()
    back = _topic_next(handler, form, topic_id)
    try:
        if action == "save":
            paragraphs = []
            index = 0
            while f"p_{index}_text" in form:
                if f"p_{index}_remove" in form:
                    index += 1
                    continue
                source = None
                source_raw = _form_text(form, f"p_{index}_source").strip()
                if source_raw:
                    try:
                        source = json.loads(source_raw)
                    except ValueError:
                        source = None
                paragraphs.append({
                    "id": _form_text(form, f"p_{index}_id") or None,
                    "kind": _form_text(form, f"p_{index}_kind") or "intro",
                    "text": _form_text(form, f"p_{index}_text"),
                    "origin": _form_text(form, f"p_{index}_origin") or "manual",
                    "checked": f"p_{index}_checked" in form,
                    "source": source,
                })
                index += 1
            save_kwargs: dict = {"paragraphs": paragraphs}
            if "usage" in form:
                # 编辑页的「用途、AI 与到期」卡片整体提交：三者一起生效（避免半套字段静默重置）。
                save_kwargs["usage"] = "external" if _form_text(form, "usage") == "external" else "internal"
                save_kwargs["ai_authorized"] = ("ai_authorized" in form)
                save_kwargs["expires_at"] = _expiry_from_form(form)
            db.kb2_save_topic_draft(handler.tenant_id, handler.user_id, topic_id, **save_kwargs)
            renew_pid = _form_text(form, "renew_paragraph").strip()
            if renew_pid:
                # 成员显式重新核对某段的来源（保存已提交的草稿后执行；失败保留已保存草稿并说明）。
                try:
                    result = db.kb2_renew_topic_paragraph_source(
                        handler.tenant_id, handler.user_id, topic_id, renew_pid)
                except (ValueError, PermissionError) as error:
                    _redirect(handler, back, error="草稿已保存；重新核对来源未完成：" + str(error))
                else:
                    _redirect(handler, back,
                              notice=f"草稿已保存；已重新核对来源（现钉住资料版本 v{result['version_number']}）。")
                return True
            _redirect(handler, back, notice="草稿已保存（不影响已发布版本）。")
            return True
        if action == "paragraph-add":
            topic = db.kb2_get_topic(handler.tenant_id, topic_id)
            if not topic:
                raise ValueError("主题不存在或不属于本企业")
            import secrets as _secrets
            paragraphs = [dict(paragraph) for paragraph in topic["content"]["paragraphs"]]
            paragraphs.append({
                "id": _secrets.token_hex(8), "kind": _form_text(form, "kind") or "point",
                "text": _form_text(form, "text"), "origin": "manual", "checked": False,
                "source": {"kind": "manual"},
            })
            db.kb2_save_topic_draft(handler.tenant_id, handler.user_id, topic_id, paragraphs=paragraphs)
            _redirect(handler, back, notice="已添加人工撰写段落（待核对）。")
            return True
        if action == "build-draft":
            result = db.kb2_build_topic_draft(handler.tenant_id, handler.user_id, topic_id)
            _redirect(handler, back,
                      notice=(f"已按关联资料整理初稿：新增 {result['added']} 段（待核对），跳过重复 {result['skipped']} 段；未覆盖人工内容。"
                              if result["added"] else "没有可追加的已核对来源段落（初稿只使用已核对且未排除的段落）。"))
            return True
        if action == "publish":
            version = db.kb2_publish_topic_draft(
                handler.tenant_id, handler.user_id, topic_id,
                confirm_ai=_form_text(form, "confirm_ai") == "1")
            _redirect(handler, back, notice=f"主题已发布（版本 v{version}）。")
            return True
        if action == "disable":
            db.kb2_set_topic_disabled(handler.tenant_id, handler.user_id, topic_id)
            _redirect(handler, back, notice="主题已停用：版本记录与关联保留。")
            return True
        if action == "enable":
            db.kb2_set_topic_disabled(handler.tenant_id, handler.user_id, topic_id, disabled=False)
            _redirect(handler, back, notice="主题已恢复。")
            return True
        if action == "relation-add":
            material = _form_text(form, "material")
            kind, _, material_id = material.partition("::")
            if not kind or not material_id:
                raise ValueError("请选择要关联的资料")
            db.kb2_set_relation(handler.tenant_id, handler.user_id, topic_id,
                                {"kind": kind, "id": material_id}, linked=True)
            _redirect(handler, back, notice="已建立关联（不改变资料本身、不授权 AI）。")
            return True
        if action == "relation-remove":
            db.kb2_set_relation(handler.tenant_id, handler.user_id, topic_id,
                                {"kind": _form_text(form, "material_kind"), "id": _form_text(form, "material_id")},
                                linked=False)
            _redirect(handler, back, notice="已移除关联（资料与历史保留）。")
            return True
        if action == "relation-refresh":
            # 成员显式重新钉住关联快照到资料当前版本（来源更新后不静默换版）。
            result = db.kb2_refresh_relation_pin(
                handler.tenant_id, handler.user_id, topic_id,
                {"kind": _form_text(form, "material_kind"), "id": _form_text(form, "material_id")})
            if result["updated"]:
                _redirect(handler, back,
                          notice=f"已重新钉住：现为资料版本 v{result['current']['pinned_version_number'] or '—'}（不会静默换版）。")
            else:
                _redirect(handler, back, notice="关联快照已是最新，无需更新。")
            return True
        if action == "link-add":
            db.kb2_set_topic_link(handler.tenant_id, handler.user_id, topic_id,
                                  _form_text(form, "related_topic_id"), linked=True)
            _redirect(handler, back, notice="已添加相关主题（双向可见；不改变内容、发布状态与授权）。")
            return True
        if action == "link-remove":
            db.kb2_set_topic_link(handler.tenant_id, handler.user_id, topic_id,
                                  _form_text(form, "related_topic_id"), linked=False)
            _redirect(handler, back, notice="已移除相关主题（只移除关系，不动内容）。")
            return True
        if action == "version-restore":
            db.kb2_restore_topic_version(handler.tenant_id, handler.user_id, topic_id,
                                         int(_form_text(form, "version") or 0))
            _redirect(handler, f"{KB2_UI_PREFIX}/topic/{topic_id}/edit",
                      notice="已恢复为草稿（注明恢复来源；不覆盖历史、不影响线上版本）。")
            return True
        if action == "move":
            db.kb2_move_topic(handler.tenant_id, handler.user_id, topic_id,
                              _form_text(form, "group_id").strip() or None)
            _redirect(handler, back, notice="已调整主题分组。")
            return True
        return False
    except (ValueError, PermissionError) as error:
        _redirect(handler, back, error=str(error))
        return True


def _group_post(handler, path, form) -> bool:
    db = handler.db
    back = _safe_next(form.get("next"), KB2_UI_PREFIX)
    try:
        if path == KB2_UI_PREFIX + "/group/new":
            db.kb2_create_group(handler.tenant_id, handler.user_id, _form_text(form, "name"))
            _redirect(handler, back, notice="分组已建立（只辅助导航）。")
            return True
        if path == KB2_UI_PREFIX + "/group/rename":
            db.kb2_rename_group(handler.tenant_id, handler.user_id,
                                _form_text(form, "group_id"), _form_text(form, "name"))
            _redirect(handler, back, notice="分组已改名。")
            return True
        if path == KB2_UI_PREFIX + "/group/delete":
            db.kb2_delete_group(handler.tenant_id, handler.user_id, _form_text(form, "group_id"))
            _redirect(handler, back, notice="分组已删除：主题与关联保留为未分组。")
            return True
    except (ValueError, PermissionError) as error:
        _redirect(handler, back, error=str(error))
        return True
    return False


def _templates_post(handler, path, form) -> bool:
    db = handler.db
    back = _safe_next(form.get("next"), f"{KB2_UI_PREFIX}/templates")
    try:
        if path == KB2_UI_PREFIX + "/templates/new":
            version_id = db.kb2_save_template(
                handler.tenant_id, handler.user_id, None, _template_form_fields(form),
                name=_form_text(form, "name"))
            templates = db.kb2_list_templates(handler.tenant_id)
            template_id = next((t["id"] for t in templates if t["latest_version_id"] == version_id), None)
            _redirect(handler, f"{KB2_UI_PREFIX}/templates/{template_id}" if template_id else back,
                      notice="模板已保存（v1）：已有资料不会自动改变，采用时需人工确认。")
            return True
        match = re.fullmatch(KB2_UI_PREFIX + r"/templates/([a-f0-9]{8,64})/save", path)
        if match:
            db.kb2_save_template(
                handler.tenant_id, handler.user_id, match.group(1), _template_form_fields(form),
                name=_form_text(form, "name"))
            _redirect(handler, f"{KB2_UI_PREFIX}/templates/{match.group(1)}",
                      notice="已保存为新模板版本（旧版本不可变；已有资料沿用原版本）。")
            return True
    except (ValueError, PermissionError) as error:
        _redirect(handler, back, error=str(error))
        return True
    return False


def _fields_post(handler, path, form) -> bool:
    db = handler.db
    match = re.fullmatch(KB2_UI_PREFIX + r"/fields/([a-f0-9]{8,64})/(adopt|field)", path)
    if not match:
        return False
    item_id, action = match.groups()
    back = f"{KB2_UI_PREFIX}/fields/{item_id}"
    try:
        if action == "adopt":
            template_version_id = _form_text(form, "template_version_id").strip()
            if not template_version_id:
                raise ValueError("请选择要采用的模板版本")
            if _form_text(form, "stage") == "confirm":
                result = db.kb2_adopt_template(handler.tenant_id, handler.user_id, item_id,
                                               template_version_id, confirmed=True)
                diff = result["diff"]
                _redirect(handler, back,
                          notice=(f"已采用模板「{diff['template_name']}」v{diff['template_version']}（只更新草稿）："
                                  f"新增 {len(diff['added'])}、移除 {len(diff['removed'])}、"
                                  f"改类型 {len(diff['type_changed'])}；旧值保留在历史。"))
            else:
                # 只预览对照，不改数据。
                diff = db.kb2_adopt_template(handler.tenant_id, handler.user_id, item_id,
                                             template_version_id)["diff"]
                _redirect(handler, f"{back}?preview={template_version_id}",
                          notice=(f"已生成采用对照（{diff['template_name']} v{diff['template_version']}）："
                                  "确认后才更新草稿。"))
            return True
        if action == "field":
            field_key = _form_text(form, "field_key").strip()
            source_kind = _form_text(form, "source_kind")
            if source_kind == "source":
                paragraph_id = _form_text(form, "source_paragraph").strip()
                if not paragraph_id:
                    raise ValueError("请选择字段的来源段落，或改选「人工填写」")
                paragraph = db._kb2_require_paragraph(handler.tenant_id, paragraph_id)
                source = {"kind": "source", "source_id": paragraph["source_id"], "paragraph_id": paragraph_id}
            else:
                source = {"kind": "manual"}
            db.kb2_set_item_field(
                handler.tenant_id, handler.user_id, item_id, field_key,
                value=_form_text(form, "value"),
                unit=_form_text(form, "unit"),
                checked=("checked" in form),
                allow_customer=("allow_customer" in form),
                source=source)
            _redirect(handler, back, notice="字段已保存（来源与核对状态已更新；发布后进入发布快照）。")
            return True
    except (ValueError, PermissionError) as error:
        _redirect(handler, back, error=str(error))
        return True
    return False


def _item_post(handler, path, form) -> bool:
    db = handler.db
    match = re.fullmatch(KB2_UI_PREFIX + r"/item/([a-f0-9]{8,64})/replace-file", path)
    if not match:
        return False
    item_id = match.group(1)
    files = [(name, filename, content) for name, filename, content
             in (form.get("__files__") or []) if name == "replace_file" and content]
    if not files:
        _redirect(handler, f"{KB2_UI_PREFIX}?view=materials", error="请选择要更新的文件。")
        return True
    _, filename, content = files[0]
    try:
        result = db.kb2_replace_file(handler.tenant_id, handler.user_id, item_id, filename, content)
    except (ValueError, PermissionError) as error:
        _redirect(handler, f"{KB2_UI_PREFIX}?view=materials", error=str(error))
        return True
    diff = result["diff"] or {"counts": {}}
    counts = diff.get("counts", {})
    _redirect(handler, f"{KB2_UI_PREFIX}/batch/{result['batch_id']}",
              notice=(f"已登记新原件并生成新草稿（资料 ID 不变，线上 v{result['baseline_version']} 在核对发布前继续有效）。"
                      f"对照：新增 {counts.get('added', 0)}、变更 {counts.get('changed', 0)}、"
                      f"移除 {counts.get('removed', 0)}、未变 {counts.get('unchanged', 0)}。请逐段核对后发布新版。"))
    return True


def post(handler, path, form) -> bool:
    """新的第二批 POST 路由；不在本模块的路径返回 False（旧入口保持兼容）。"""
    if not re.match(r"^/admin/knowledge(/|$)", path):
        return False
    if not (path.startswith(KB2_UI_PREFIX + "/add")
            or path.startswith(KB2_UI_PREFIX + "/batch/")
            or path.startswith(KB2_UI_PREFIX + "/topic/")
            or path.startswith(KB2_UI_PREFIX + "/group/")
            or path.startswith(KB2_UI_PREFIX + "/templates")
            or path.startswith(KB2_UI_PREFIX + "/fields/")
            or path.startswith(KB2_UI_PREFIX + "/item/")):
        return False
    if not handler._kb_can_manage():
        handler._kb_forbidden()
        return True
    if path == KB2_UI_PREFIX + "/add":
        _add_post(handler, form)
        return True
    if path.startswith(KB2_UI_PREFIX + "/batch/"):
        return _batch_post(handler, path, form)
    if path.startswith(KB2_UI_PREFIX + "/topic/"):
        return _topic_post(handler, path, form)
    if path.startswith(KB2_UI_PREFIX + "/group/"):
        return _group_post(handler, path, form)
    if path.startswith(KB2_UI_PREFIX + "/templates"):
        return _templates_post(handler, path, form)
    if path.startswith(KB2_UI_PREFIX + "/fields/"):
        return _fields_post(handler, path, form)
    if path.startswith(KB2_UI_PREFIX + "/item/"):
        return _item_post(handler, path, form)
    return False
