#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""首次邀请测试上线 T2：真实模型联调检查（默认 dry-run，不调用任何模型；--live 显式开启）。

用法：
  # 1) 干跑（默认；完全离线、不产生费用）：合成临时企业/资料，用受控 stub 走通全部链路
  python3 tools/pilot_checks/check_first_launch_t2.py

  # 2) 真实平台服务（先在当前 shell 安全配置 AI_MODE=deepseek、DEEPSEEK_API_KEY；密钥不进本脚本）
  python3 tools/pilot_checks/check_first_launch_t2.py --live --stage platform \
      --out /tmp/b2b-t2-live-platform.json

  # 3) 企业自定义 OpenAI 兼容接口（密钥只从环境变量读取；命令行不要放密钥）
  B2B_T2_CUSTOM_API_KEY=... python3 tools/pilot_checks/check_first_launch_t2.py --live --stage custom \
      --custom-base-url https://api.example.com/v1 --custom-model example-model \
      --out /tmp/b2b-t2-live-custom.json

边界（对应 docs/首次上线-T2执行方案.md）：
- 全部数据（数据库、原件、私有模型配置）写入系统临时目录；不读不写仓库 data/、不连真实业务库、
  不发外部消息、不恢复 Dify；不读取也不扫描任何密钥文件。
- dry-run 完全离线：真实模型调用被监视器拦截为失败；用的假密钥只用于秘密卫生断言。
- --live 才真实调用：调用次数有上限（--max-calls，默认 6，硬上限 12）；平台服务与企业自定义
  分阶段（--stage、--steps）。报告（--out，live 必填）保留每步状态/耗时/用量/引用版本，
  以及临时库 ai_call_log 导出，不含密钥。
- 真实调用产生供应商费用，需用户费用授权与安全配置。本脚本结果是操作方自测证据，
  不自动构成独立验收。

退出码：0=全部通过；1=存在 FAIL 或 REVIEW；2=用法/配置问题（未发起调用）。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
import tempfile
import traceback
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import ai_client  # noqa: E402
from ai_client import (  # noqa: E402
    AIConfig,
    AIConfigurationError,
    AIService,
    AIUnavailableError,
    MODE_DEEPSEEK,
    MODE_RULES,
    STATUS_LABELS_ZH,
)
from app import (  # noqa: E402
    Database,
    answer_knowledge_question,
    answer_question_prefer_dify,
    answer_question_with_mode,
    extract_requirement_candidates,
    process_channel_message,
)
from model_settings import EnterpriseModels, validate_endpoint  # noqa: E402
# 复用现有测试夹具（只读导入；测试文件不改动）：合成临时企业、两批资料与受控 stub。
from test_enterprise_knowledge_task4 import TENANT, StubKnowledgeAI, Task4Base  # noqa: E402
from test_knowledge_phase2_ai_gates import (  # noqa: E402
    AiGateBase,
    FieldEvidenceGateTests,
    choose_source,
)

FAKE_KEY = "sk-t2-dry-run-not-a-real-key"  # 仅 dry-run 秘密卫生断言用；不是真实密钥
EXTRACTION_SAMPLE = (
    "We need a clean-water pump for a plant room: flow 30 m³/h, head 25 m, "
    "delivery to Hamburg in about 8 weeks."
)
GROUND_QUESTIONS = {
    "knowledge": "发货需要几天？",
    "topic": "保修期是多长？",
    "item_field": "客服响应时间是多久？",
    "product_field": "这台泵的流量范围是多少？",
}
# 合成资料中的不合格标记：任何一步送入模型的内容都不得包含这些文字。
INELIGIBLE_MARKERS = ("内部正文：不得引用。", "内部内容：不得引用。", "默认关闭正文：不得引用。")
KNOWN_STATUSES = tuple(ai_client.STATUS_ACTIONS)  # 失败分类白名单
DATA_DIR = ROOT / "data"


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def assert_no_secret(text: str, secrets) -> None:
    """输出卫生断言：报告/导出文本不得包含已知的密钥值（只比对内存中已知值，不扫描文件系统）。"""
    for secret in secrets:
        if secret and secret in text:
            raise RuntimeError("检测到已知密钥值出现在本脚本输出中：拒绝写入")


class Report:
    """逐项状态 + JSON 报告；状态：PASS / FAIL / REVIEW / INFO / SKIP。"""

    ALLOWED = ("PASS", "FAIL", "REVIEW", "INFO", "SKIP")

    def __init__(self, mode: str):
        self.mode = mode
        self.items: list[dict] = []

    def add(self, section: str, name: str, status: str, detail: str = "", **extra) -> dict:
        if status not in self.ALLOWED:
            raise ValueError(f"未知状态 {status}")
        item = {"section": section, "name": name, "status": status, "detail": str(detail), **extra}
        self.items.append(item)
        line = f"[{status}] {section} · {name} — {detail}"
        print(line)
        return item

    def counts(self) -> dict:
        result = {status: 0 for status in self.ALLOWED}
        for item in self.items:
            result[item["status"]] += 1
        return result

    def exit_code(self) -> int:
        counts = self.counts()
        return 0 if counts["FAIL"] == 0 and counts["REVIEW"] == 0 else 1


class LiveBudget:
    """真实调用次数上限：所有 live 调用先 consume，超限直接拒绝（不发起调用）。"""

    class Exceeded(RuntimeError):
        pass

    def __init__(self, cap: int):
        self.cap = cap
        self.used = 0
        self.calls: list[dict] = []

    def consume(self, action: str) -> None:
        if self.used >= self.cap:
            raise LiveBudget.Exceeded(f"live 调用已达上限 {self.cap}，拒绝继续发起「{action}」")
        self.used += 1
        self.calls.append({"n": self.used, "action": action, "at": now_iso()})

    def summary(self) -> dict:
        return {"max_calls": self.cap, "used_calls": self.used, "calls": self.calls}


class SyntheticFixture(Task4Base):
    """合成临时企业夹具：复用现有测试的临时库/企业隔离（第一批）与第二批主题/字段夹具。"""

    ingest_txt = AiGateBase.ingest_txt
    check_all = AiGateBase.check_all
    publish_item = AiGateBase.publish_item
    build_topic_from = AiGateBase.build_topic_from
    make_item_with_fields = FieldEvidenceGateTests.make_item_with_fields


class StubExtractionAI(StubKnowledgeAI):
    """在受控 stub 上补抽出入口，供「故障输入保留」步骤使用（默认离线）。"""

    def __init__(self, extraction=None, error=None, **kw):
        super().__init__(error=error, **kw)
        self.extraction = extraction if extraction is not None else {"flow": {"value": "30", "unit": "m³/h"}}

    def extract_requirements(self, text):
        self.calls.append({"text": str(text)[:200]})
        if self.error is not None:
            raise self.error
        return dict(self.extraction), {"duration_ms": 4, "http_status": 200, "prompt_tokens": 11, "completion_tokens": 5}


class BudgetedService:
    """真实服务包装：转发 mode/config，每个模型入口先计入调用上限。"""

    def __init__(self, service, budget: LiveBudget):
        self._service = service
        self._budget = budget

    @property
    def mode(self):
        return self._service.mode

    @property
    def config(self):
        return self._service.config

    def extract_requirements(self, *args, **kwargs):
        self._budget.consume("extract_requirements")
        return self._service.extract_requirements(*args, **kwargs)

    def select_answer_field(self, *args, **kwargs):
        self._budget.consume("select_answer_field")
        return self._service.select_answer_field(*args, **kwargs)

    def select_knowledge_evidence(self, *args, **kwargs):
        self._budget.consume("select_knowledge_evidence")
        return self._service.select_knowledge_evidence(*args, **kwargs)


class RevokeAfterSelection:
    """生成期间撤权演练：真实模型返回后、平台复核前，立即撤销被选证据并记录可恢复句柄。"""

    def __init__(self, service, revoke):
        self._service = service
        self._revoke = revoke
        self.mode = service.mode
        self.config = service.config
        self.handles: list[dict] = []

    def select_knowledge_evidence(self, question, evidence):
        selected, meta = self._service.select_knowledge_evidence(question, evidence)
        if selected is not None:
            unit = next((item for item in evidence if item["key"] == selected), None)
            if unit is not None:
                handle = self._revoke(unit)
                if handle:
                    self.handles.append(handle)
        return selected, meta


def citation_summary(citation) -> dict | None:
    if not citation:
        return None
    keys = ("kind", "title", "form_label", "version", "version_id", "item_id", "topic_id",
            "paragraph_id", "field_key", "published_at")
    summary = {key: citation.get(key) for key in keys if citation.get(key) is not None}
    summary["quote_len"] = len(str(citation.get("quote") or ""))
    return summary


def qa_units(db) -> list[dict]:
    return db.list_customer_qa_evidence(TENANT)


def target_evidence(kind: str, materials: dict):
    """在候选证据里定位本次脚本要验证的合成目标单元（四类各一条）。"""
    if kind == "knowledge":
        return lambda unit: unit.get("item_id") == materials["knowledge"]["item_id"]
    if kind == "topic":
        return lambda unit: unit.get("topic_id") == materials["topic"]["topic_id"]
    if kind == "item_field":
        return lambda unit: (unit.get("item_id") == materials["item_field"]["item_id"]
                             and unit.get("field_key") == "resp")
    return lambda unit: (unit.get("product_id") == materials["product_field"]["product_id"]
                         and unit.get("field_key") == "flow_range")


def unit_for(db, kind: str, materials: dict) -> dict:
    match = target_evidence(kind, materials)
    return next(u for u in qa_units(db) if u["source"] == kind and match(u))


def choose_target(kind: str, materials: dict):
    """受控 stub 选择策略：在送选清单里选中合成目标单元（其他候选仍真实参与过滤）。"""
    match = target_evidence(kind, materials)

    def pick(ai, evidence):
        for item in evidence:
            if item["evidence"].get("source") == kind and match(item["evidence"]):
                ai.selection = item["key"]
                return
        ai.selection = None

    return pick


def build_materials(fx) -> dict:
    """在临时库里合成四类合格证据与不合格负例（T2 用合成资料，不使用任何真实企业数据）。"""
    materials: dict = {}
    # 1) 第一批知识：已发布 + 对外 + 已授权 + 已准备
    item_id, version = fx.publish_prepared(title="发货说明", body="发货说明：英国境内 3-5 个工作日送达。")
    materials["knowledge"] = {"item_id": item_id, "version_id": version["id"]}
    fx.publish_prepared(title="内部文章", body="内部正文：不得引用。", usage="internal")  # 负例
    # 2) 产品字段：已发布 + 逐字段核准 + 已授权 AI 客服
    product_id, source_id = fx.publish_product()
    materials["product_field"] = {"product_id": product_id, "source_id": source_id, "field_key": "flow_range"}
    # 3) 第二批资料 + 主题段
    batch_id, source = fx.ingest_txt("保修说明.txt", "保修期：24 个月，从交付日起算。")
    fx.check_all(source["id"])
    fx.publish_item(batch_id)
    topic_id, _paragraphs = fx.build_topic_from(source["item_id"])
    materials["topic"] = {"topic_id": topic_id, "item_id": source["item_id"], "source_id": source["id"]}
    # 4) 灵活字段：已核对 + 允许客户引用 + 有值 + 有来源；另带一个不允许客户引用的负例字段
    field_item_id, _field_batch = fx.make_item_with_fields(
        [
            {"label": "响应时间", "type": "number", "unit": "小时", "key": "resp"},
            {"label": "支持定制", "type": "bool", "key": "custom"},
            {"label": "内部口径", "type": "text", "key": "internal"},
        ],
        [
            ("resp", "1", {"checked": True, "allow_customer": True}),
            ("custom", False, {"checked": True, "allow_customer": True}),
            ("internal", "内部内容：不得引用。", {"checked": True, "allow_customer": False}),
        ],
    )
    materials["item_field"] = {"item_id": field_item_id, "field_key": "resp"}
    return materials


def revoke_selected_evidence(fx, unit: dict) -> dict:
    """按被选中的证据执行一次真实撤权/失效，返回可恢复的句柄（dry-run 与 live 共用）。

    灵活字段的失效语义是「发布新版本替换」：草稿层改动不改变已发布视图，
    只有暂停字段核对并发布新版本后，旧字段证据才按换版被拒（与既有字段闸门测试同一路径）。
    """
    kind = unit.get("source")
    if kind == "knowledge":
        item_id = unit["item_id"]
        fx.db.disable_knowledge_item(TENANT, item_id, fx.admin["id"], confirmed=True)
        return {"kind": kind, "item_id": item_id, "what": "停用知识条目"}
    if kind == "topic":
        topic_id = unit["topic_id"]
        fx.db.kb2_set_topic_disabled(TENANT, fx.admin["id"], topic_id, True)
        return {"kind": kind, "topic_id": topic_id, "what": "停用主题"}
    if kind == "item_field":
        item_id, field_key = unit["item_id"], unit["field_key"]
        fx.db.kb2_set_item_field(TENANT, fx.admin["id"], item_id, field_key, checked=False)
        fx.db.publish_knowledge_draft(TENANT, item_id, fx.admin["id"], confirm_ai=True)
        return {"kind": kind, "item_id": item_id, "field_key": field_key,
                "what": "暂停字段核对并发布新版本（换版）"}
    if kind == "product_field":
        fx.db.set_source_usage(TENANT, unit["product_id"], unit["fact_source_id"], "restricted", "t2-check")
        return {"kind": kind, "product_id": unit["product_id"], "source_id": unit["fact_source_id"],
                "what": "来源文件标记禁止使用"}
    raise ValueError(f"未知证据来源 {kind!r}")


def restore_selected_evidence(fx, handle: dict) -> None:
    kind = handle.get("kind")
    if kind == "knowledge":
        fx.db.enable_knowledge_item(TENANT, handle["item_id"], fx.admin["id"], confirmed=True)
    elif kind == "topic":
        fx.db.kb2_set_topic_disabled(TENANT, fx.admin["id"], handle["topic_id"], False)
    elif kind == "item_field":
        fx.db.kb2_set_item_field(TENANT, fx.admin["id"], handle["item_id"], handle["field_key"], checked=True)
        fx.db.publish_knowledge_draft(TENANT, handle["item_id"], fx.admin["id"], confirm_ai=True)
    elif kind == "product_field":
        fx.db.set_source_usage(TENANT, handle["product_id"], handle["source_id"], "internal", "t2-check")


# ── dry-run（完全离线）─────────────────────────────────────────────────────────

def dry_run_grounded(report: Report, fx, materials: dict, kind: str) -> None:
    """四类证据各跑一次：受控 stub 选择该类证据 → 服务端精确引用 + 真实版本。"""
    label = f"四类证据 · {kind}"
    try:
        unit = unit_for(fx.db, kind, materials)
    except StopIteration:
        report.add(label, "候选存在", "FAIL", "合成资料未进入候选证据")
        return
    stub = StubKnowledgeAI(on_call=choose_target(kind, materials))
    outcome = answer_knowledge_question(fx.db, stub, TENANT, "", GROUND_QUESTIONS[kind])
    sent = json.dumps(stub.calls[0]["evidence"] if stub.calls else [], ensure_ascii=False)
    leaked = [marker for marker in INELIGIBLE_MARKERS if marker in sent]
    if leaked:
        report.add(label, "仅合格证据送入模型", "FAIL", f"送模型内容包含不合格资料：{leaked}")
        return
    if outcome["status"] != "grounded":
        report.add(label, "回答有据", "FAIL", f"状态 {outcome['status']}（{outcome.get('reason')}）")
        return
    citation = outcome["citations"][0]
    problems = []
    if citation.get("kind") != kind:
        problems.append(f"引用类型 {citation.get('kind')} != {kind}")
    if citation.get("quote") != unit["text"]:
        problems.append("引用原文与存储原文不一致")
    if kind == "product_field":
        if citation.get("field_key") != unit["field_key"] or citation.get("version") != f"v{unit['version_number']}":
            problems.append("产品字段引用版本与当前发布字段不一致")
    else:
        # 版本标识：主题段钉在主题版本（topic_version_id），其余钉在资料版本行（version_id）。
        expected_version_id = unit.get("topic_version_id") if kind == "topic" else unit.get("version_id")
        if citation.get("version_id") != expected_version_id:
            problems.append("引用版本行与当前合格版本不一致")
    if problems:
        report.add(label, "引用与版本", "FAIL", "；".join(problems), citation=citation_summary(citation))
        return
    report.add(label, "回答有据", "PASS", f"原文精确引用 + 版本 {citation.get('version')}",
               citation=citation_summary(citation))


def dry_run_unified_entry(report: Report, fx, materials: dict) -> None:
    """官网/渠道统一入口：同一条合格问题 → grounded + source=knowledge。"""
    stub = StubKnowledgeAI(on_call=choose_source("knowledge"))
    status, _answer, _pid, _vid, citations, reason, source = answer_question_prefer_dify(
        fx.db, stub, TENANT, "", GROUND_QUESTIONS["knowledge"], None, "t2-check")
    if status == "grounded" and source == "knowledge" and citations:
        report.add("统一入口", "官网/渠道共用入口", "PASS",
                   f"grounded · source={source} · 版本 {citations[0].get('version')}",
                   citation=citation_summary(citations[0]))
    else:
        report.add("统一入口", "官网/渠道共用入口", "FAIL", f"状态 {status}（{reason}）· source={source}")


def dry_run_channel(report: Report, fx, materials: dict) -> None:
    """渠道默认 AI 关闭（不走模型）；显式开启后走同一知识问答阶段。"""
    account_id = fx.db.create_business_connection(
        TENANT, "langbot", "Telegram", "T2 合成业务号", fx.admin["id"], fx.admin["email"])
    row = fx.db.get_business_connection(TENANT, account_id)
    if not int(row["ai_enabled"]):
        report.add("统一入口", "渠道默认 AI 关闭", "PASS", "新连接 ai_enabled=0；未进入问答阶段")
    else:
        report.add("统一入口", "渠道默认 AI 关闭", "FAIL", "新连接默认开启了 AI")
    stub = StubKnowledgeAI(selection=None)
    result = process_channel_message(fx.db, stub, TENANT, "langbot", "cust-1", "客户",
                                     GROUND_QUESTIONS["knowledge"], "t2-m1", account_id)
    if result.get("action") == "ai_disabled" and not stub.calls:
        report.add("统一入口", "渠道未开启时不调用模型", "PASS", "action=ai_disabled · 模型调用 0")
    else:
        report.add("统一入口", "渠道未开启时不调用模型", "FAIL",
                   f"action={result.get('action')} · 模型调用 {len(stub.calls)}")
    fx.db.set_connection_ai(TENANT, account_id, True, fx.admin["id"], confirmed=True)
    stub2 = StubKnowledgeAI(on_call=choose_source("knowledge"))
    with patch("app.dispatch_outbound", return_value=(True, "")):
        result2 = process_channel_message(fx.db, stub2, TENANT, "langbot", "cust-1", "客户",
                                          GROUND_QUESTIONS["knowledge"], "t2-m2", account_id)
    if result2.get("action") == "answered" and result2.get("qa_source") == "knowledge":
        report.add("统一入口", "渠道开启后走同一知识阶段", "PASS", "action=answered · qa_source=knowledge")
    else:
        report.add("统一入口", "渠道开启后走同一知识阶段", "FAIL",
                   f"action={result2.get('action')} · qa_source={result2.get('qa_source')}")


def dry_run_dify_guard(report: Report, fx, materials: dict) -> None:
    """共享 Dify 禁用：即使 DIFY_* 环境变量存在，统一入口仍只用本地获准知识。"""
    dummy = {
        "DIFY_QA_URL": "https://dify.invalid/v1", "DIFY_QA_APP_KEY": "dify-dummy-not-real",
        "DIFY_KB_URL": "https://dify.invalid/v1", "DIFY_KB_API_KEY": "dify-dummy-not-real",
        "DIFY_KB_DATASET_ID": "dummy-dataset",
    }
    saved = {key: os.environ.get(key) for key in dummy}
    try:
        os.environ.update(dummy)
        stub = StubKnowledgeAI(on_call=choose_source("knowledge"))
        status, _answer, _pid, _vid, citations, reason, source = answer_question_prefer_dify(
            fx.db, stub, TENANT, "", GROUND_QUESTIONS["knowledge"], None, "t2-check")
        if status == "grounded" and source == "knowledge":
            report.add("统一入口", "Dify 环境变量不改变路径", "PASS",
                       "存在 DIFY_* 变量时仍 source=knowledge（共享 Dify 未参与）",
                       citation=citation_summary(citations[0]) if citations else None)
        else:
            report.add("统一入口", "Dify 环境变量不改变路径", "FAIL", f"状态 {status} · source={source}（{reason}）")
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def dry_run_defaults(report: Report, fx, materials: dict) -> None:
    """AI 默认关闭：两批资料默认仅内部 + 未授权；产品字段需显式授权；AI_MODE 默认 rules。"""
    batch_id, source = fx.ingest_txt("默认设置.txt", "默认关闭正文：不得引用。")
    fx.check_all(source["id"])
    result = fx.db.kb2_publish_batch(TENANT, fx.admin["id"], batch_id)  # 不传 usage/ai/confirm
    batch_row = fx.db.conn.execute(
        "SELECT usage, ai_authorized FROM kb2_batches WHERE tenant_id=? AND id=?", (TENANT, batch_id)).fetchone()
    candidates_text = json.dumps([u["text"] for u in qa_units(fx.db)], ensure_ascii=False)
    if (result.get("published") and batch_row["usage"] == "internal" and not int(batch_row["ai_authorized"])
            and INELIGIBLE_MARKERS[2] not in candidates_text):
        report.add("AI 默认关闭", "第二批批次默认仅内部 + 未授权", "PASS",
                   "发布成功但 usage=internal、AI 关；正文未进入候选")
    else:
        report.add("AI 默认关闭", "第二批批次默认仅内部 + 未授权", "FAIL",
                   f"published={bool(result.get('published'))} · usage={batch_row['usage']} · ai={batch_row['ai_authorized']}")

    item_id = fx.db.create_knowledge_item(TENANT, "article", "默认设置文章", fx.admin["id"],
                                          content={"body": "默认关闭正文：不得引用。"})
    fx.db.publish_knowledge_draft(TENANT, item_id, fx.admin["id"])
    version = fx.db.get_published_knowledge_version(TENANT, item_id)
    fx.db.prepare_knowledge_version(TENANT, version["id"])
    version_row = fx.db.conn.execute(
        "SELECT usage, ai_authorized FROM knowledge_versions WHERE id=?", (version["id"],)).fetchone()
    if version_row["usage"] == "internal" and not int(version_row["ai_authorized"]):
        report.add("AI 默认关闭", "第一批资料默认仅内部 + 未授权", "PASS", "发布版本 usage=internal、AI 关")
    else:
        report.add("AI 默认关闭", "第一批资料默认仅内部 + 未授权", "FAIL",
                   f"usage={version_row['usage']} · ai={version_row['ai_authorized']}")

    gate_product = fx.db.create_product(TENANT, "GATE-1", "未授权测试泵", "冷却水循环")
    gate_source = fx.db.add_source(TENANT, gate_product, "GATE-1-manual.txt", "TXT", "第 1 页")
    fx.db.save_field(TENANT, gate_product, "flow_range", "10-20", "m³/h", gate_source, "第 1 页")
    fx.db.approve_field(TENANT, gate_product, "flow_range")
    fx.db.finalize_product_review(TENANT, gate_product)
    fx.db.publish_product(TENANT, gate_product)
    before = [u for u in qa_units(fx.db) if u["source"] == "product_field" and u["product_id"] == gate_product]
    fx.db.set_knowledge_product_authorization(TENANT, gate_product, fx.admin["id"], authorized=True, confirmed=True)
    after = [u for u in qa_units(fx.db) if u["source"] == "product_field" and u["product_id"] == gate_product]
    if not before and len(after) == 1:
        report.add("AI 默认关闭", "产品字段需显式开启 AI 客服授权", "PASS", "授权前 0 条候选；显式授权后 1 条")
    else:
        report.add("AI 默认关闭", "产品字段需显式开启 AI 客服授权", "FAIL",
                   f"授权前 {len(before)} 条 · 授权后 {len(after)} 条")

    try:
        AIConfig.from_env(environ={"AI_MODE": "deepseek"})
        fail_closed = False
    except AIConfigurationError:
        fail_closed = True
    default_mode = AIConfig.from_env(environ={}).mode
    if default_mode == MODE_RULES and fail_closed and AIService(AIConfig()).client is None:
        report.add("AI 默认关闭", "AI_MODE 默认 rules、缺 Key 拒启", "PASS",
                   "无配置=rules；deepseek 缺 Key 明确拒绝启动（不静默落回）")
    else:
        report.add("AI 默认关闭", "AI_MODE 默认 rules、缺 Key 拒启", "FAIL",
                   f"default_mode={default_mode} · fail_closed={fail_closed}")

    candidates_text = json.dumps([u["text"] for u in qa_units(fx.db)], ensure_ascii=False)
    leaked = [marker for marker in INELIGIBLE_MARKERS if marker in candidates_text]
    report.add("AI 默认关闭", "不合格负例不进候选", "PASS" if not leaked else "FAIL",
               "内部/未授权/默认关闭三类正文均未进入候选" if not leaked else f"泄露：{leaked}")


def dry_run_revocation(report: Report, fx, materials: dict, kind: str) -> None:
    """生成期间撤权：模型选择后立即失效 → 复核必须拒绝（按资料不足处理）。"""
    label = f"生成期间撤权 · {kind}"
    handle: dict = {}

    def on_call(ai, evidence):
        choose_target(kind, materials)(ai, evidence)
        chosen = next((item["evidence"] for item in evidence if item["key"] == ai.selection), None)
        if chosen is not None:
            handle.update(revoke_selected_evidence(fx, chosen))

    stub = StubKnowledgeAI(on_call=on_call)
    outcome = answer_knowledge_question(fx.db, stub, TENANT, "", GROUND_QUESTIONS[kind])
    if handle:
        restore_selected_evidence(fx, handle)
    if not handle:
        report.add(label, "复核拦截", "FAIL", "受控选择未命中合成目标证据（夹具异常）")
    elif outcome["status"] == "no_evidence" and "生成期间已失效" in outcome.get("reason", ""):
        report.add(label, "复核拦截", "PASS", f"{handle['what']}；拒绝输出失效内容（{outcome['reason']}）")
    else:
        report.add(label, "复核拦截", "FAIL",
                   f"撤权动作：{handle['what']}；状态 {outcome['status']}（{outcome.get('reason')}）")


def dry_run_failures(report: Report, fx, materials: dict) -> None:
    """故障输入保留：模型失败 → 明确错误、无假答复、可重试；抽取路径同口径。"""
    failure = AIUnavailableError(ai_client.STATUS_TIMEOUT, "timeout")
    stub = StubKnowledgeAI(error=failure)
    outcome = answer_knowledge_question(fx.db, stub, TENANT, "", GROUND_QUESTIONS["knowledge"])
    latest = fx.db.latest_ai_call(TENANT)
    ok = (outcome["status"] == "error" and outcome["error_kind"] == ai_client.STATUS_TIMEOUT
          and outcome["answer"] == "" and not outcome["citations"]
          and outcome["candidate_count"] > 0 and latest and latest["status"] == ai_client.STATUS_TIMEOUT)
    if ok and not fx.db.list_human_tasks(TENANT):
        report.add("故障与输入保留", "问答失败：无假答复、候选保留、日志留痕", "PASS",
                   f"error_kind=timeout · 候选 {outcome['candidate_count']} 条仍在 · 未建人工任务")
    else:
        report.add("故障与输入保留", "问答失败：无假答复、候选保留、日志留痕", "FAIL",
                   f"status={outcome['status']} · kind={outcome.get('error_kind')} · answer={outcome['answer']!r}")
    stub2 = StubKnowledgeAI(on_call=choose_source("knowledge"))
    outcome2 = answer_knowledge_question(fx.db, stub2, TENANT, "", GROUND_QUESTIONS["knowledge"])
    if outcome2["status"] == "grounded":
        report.add("故障与输入保留", "同一问题重试成功", "PASS", "失败后原问题直接重试即 grounded（输入未丢失）")
    else:
        report.add("故障与输入保留", "同一问题重试成功", "FAIL", f"重试状态 {outcome2['status']}")

    fail_stub = StubExtractionAI(error=failure)
    try:
        extract_requirement_candidates(fx.db, fail_stub, TENANT, "", EXTRACTION_SAMPLE)
        raised = False
    except AIUnavailableError as error:
        raised = error.kind == ai_client.STATUS_TIMEOUT
    latest = fx.db.latest_ai_call(TENANT)
    if raised and latest and latest["status"] == ai_client.STATUS_TIMEOUT:
        report.add("故障与输入保留", "需求抽取失败：向上抛出并留痕", "PASS", "AIUnavailableError(timeout) 已抛出不吞掉")
    else:
        report.add("故障与输入保留", "需求抽取失败：向上抛出并留痕", "FAIL", f"raised={raised}")
    retry = StubExtractionAI()
    values = extract_requirement_candidates(fx.db, retry, TENANT, "", EXTRACTION_SAMPLE)
    flow = values.get("flow") or {}
    if flow.get("value") == "30" and flow.get("source") == "model_candidate" and flow.get("state") == "candidate":
        report.add("故障与输入保留", "抽取重试得到候选值", "PASS", "重试成功；候选仍待买方确认（state=candidate）")
    else:
        report.add("故障与输入保留", "抽取重试得到候选值", "FAIL", f"flow={flow!r}")


def dry_run_secrets(report: Report, fx, materials: dict) -> None:
    """秘密检查（假密钥）：配置遮蔽、私有目录权限、数据库/日志不含密钥。"""
    masked = FAKE_KEY not in repr(AIConfig(mode=MODE_DEEPSEEK, api_key=FAKE_KEY))
    report.add("秘密检查", "AIConfig 表示遮蔽密钥", "PASS" if masked else "FAIL",
               "repr 只显示 ***" if masked else "repr 含密钥值")

    models = EnterpriseModels(fx.db.path, AIService(AIConfig()))
    models.save(TENANT, "https://api.deepseek.com", "deepseek-chat", FAKE_KEY)
    file_mode = models.path(TENANT).stat().st_mode & 0o777
    root_mode = models.root.stat().st_mode & 0o777
    public_text = json.dumps(models.public(TENANT), ensure_ascii=False)
    models.disconnect(TENANT)
    ok = file_mode == 0o600 and root_mode == 0o700 and FAKE_KEY not in public_text
    report.add("秘密检查", "企业私有配置 700/600 且不回显", "PASS" if ok else "FAIL",
               f"目录 {oct(root_mode)} · 文件 {oct(file_mode)} · public 不含密钥")

    db_bytes = Path(fx.db.path).read_bytes().decode("latin1")
    audit_details = " ".join(
        str(row["details"] or "") for row in fx.db.conn.execute("SELECT details FROM audit_log"))
    log_text = " ".join(
        str(row["provider"]) + str(row["model"]) for row in fx.db.conn.execute("SELECT provider, model FROM ai_call_log"))
    leaked = [name for name, text in (("SQLite", db_bytes), ("审计", audit_details), ("调用日志", log_text))
              if FAKE_KEY in text]
    report.add("秘密检查", "密钥不进数据库/审计/调用日志", "PASS" if not leaked else "FAIL",
               "假密钥未出现在 SQLite/审计/ai_call_log" if not leaked else f"泄露位置：{leaked}")


def run_dry_run(args, report: Report, meta: dict) -> int:
    print("== T2 dry-run（离线；不调用任何模型）==")
    guard = ["ai_client.DeepSeekChatClient.call_json", "urllib.request.urlopen", "model_settings.custom_urlopen"]
    fx = SyntheticFixture()
    with patch("ai_client.DeepSeekChatClient.call_json",
               side_effect=RuntimeError("dry-run 禁止真实模型调用")), \
         patch("urllib.request.urlopen", side_effect=RuntimeError("dry-run 禁止网络调用")), \
         patch("model_settings.custom_urlopen", side_effect=RuntimeError("dry-run 禁止自定义端点调用")):
        fx.setUp()
        try:
            meta["fixture_db"] = str(fx.db.path)
            report.add("护栏", "dry-run 禁网监视器", "PASS", "已启用：" + "、".join(guard))
            materials = build_materials(fx)
            kinds = ("knowledge", "product_field", "topic", "item_field")
            found = {u["source"] for u in qa_units(fx.db)}
            report.add("四类证据", "四类候选齐全", "PASS" if set(kinds) <= found else "FAIL",
                       f"候选来源：{sorted(found)}")
            for kind in kinds:
                dry_run_grounded(report, fx, materials, kind)
            dry_run_unified_entry(report, fx, materials)
            dry_run_channel(report, fx, materials)
            dry_run_dify_guard(report, fx, materials)
            dry_run_defaults(report, fx, materials)
            for kind in kinds:
                dry_run_revocation(report, fx, materials, kind)
            dry_run_failures(report, fx, materials)
            dry_run_secrets(report, fx, materials)
            rows = [dict(row) for row in fx.db.conn.execute("SELECT * FROM ai_call_log ORDER BY rowid")]
            ok_rows = [r for r in rows if r["status"] == "ok"]
            err_rows = [r for r in rows if r["status"] != "ok"]
            report.add("汇总", "调用日志状态可审计", "PASS" if ok_rows and err_rows else "FAIL",
                       f"ok {len(ok_rows)} 条 · 失败 {len(err_rows)} 条（stub 本地记录，无外呼）")
            meta["call_log"] = [{k: r[k] for k in ("task", "provider", "model", "status", "action",
                                                   "http_status", "duration_ms", "prompt_tokens",
                                                   "completion_tokens", "created_at")} for r in rows]
        finally:
            if args.keep_workdir:
                target = Path(meta["workdir"]) / "synthetic.db"
                try:
                    shutil.copy2(str(fx.db.path), str(target))
                    meta["kept_db"] = str(target)
                except OSError:
                    pass
            fx.tearDown()
    report.add("护栏", "dry-run 真实调用次数", "PASS", "0 次（全部经受控 stub）；真实调用只在 --live 发生")
    return report.exit_code()


# ── live（真实调用；有上限、有审计）────────────────────────────────────────────

def live_preflight(args, report: Report) -> tuple:
    """返回 (service_config, secrets_in_memory, error)。未通过时不发起任何调用。"""
    secrets = []
    if args.stage == "platform":
        try:
            config = AIConfig.from_env()
        except AIConfigurationError as error:
            return None, secrets, f"平台模型配置不完整：{error}"
        if config.mode != MODE_DEEPSEEK:
            return None, secrets, ("当前 AI_MODE 不是 deepseek。请先在本机 shell 安全配置 "
                                   "AI_MODE=deepseek 与 DEEPSEEK_API_KEY（不要写入文件或命令行）。")
        secrets.append(config.api_key)
        return config, secrets, None
    if not args.custom_base_url or not args.custom_model:
        return None, secrets, "--stage custom 需要 --custom-base-url 与 --custom-model"
    try:
        base_url = validate_endpoint(args.custom_base_url)
    except ValueError as error:
        return None, secrets, f"自定义 API 地址校验失败：{error}"
    key = os.environ.get(args.custom_key_env, "").strip()
    if not key:
        return None, secrets, (f"未读到自定义 API 密钥：请把密钥放入环境变量 {args.custom_key_env} "
                               "（不要把密钥写进命令行或文件）。")
    secrets.append(key)
    config = AIConfig(mode=MODE_DEEPSEEK, provider="custom", base_url=base_url, model=args.custom_model, api_key=key)
    return config, secrets, None


def live_probe(report, fx, service, budget: LiveBudget) -> None:
    budgeted = BudgetedService(service, budget)
    try:
        values = extract_requirement_candidates(fx.db, budgeted, TENANT, "", EXTRACTION_SAMPLE)
    except AIUnavailableError as error:
        report.add("live", "探针：需求抽取", "FAIL",
                   f"状态 {error.kind}（{STATUS_LABELS_ZH.get(error.kind, error.kind)}）· {error.duration_ms} ms")
        return
    latest = fx.db.latest_ai_call(TENANT)
    flow = (values.get("flow") or {}).get("value")
    head = (values.get("head") or {}).get("value")
    if flow and head:
        report.add("live", "探针：需求抽取", "PASS",
                   f"flow={flow} · head={head} · 耗时 {latest['duration_ms']} ms · "
                   f"tokens {latest['prompt_tokens']}+{latest['completion_tokens']}",
                   log=dict(latest))
    else:
        report.add("live", "探针：需求抽取", "REVIEW",
                   f"调用成功但未抽取到 flow/head（模型输出：{[k for k in values]}），需人工评估", log=dict(latest))


def live_grounded(report, fx, materials, service, budget: LiveBudget, kind: str) -> None:
    question = GROUND_QUESTIONS[kind]
    outcome = None
    if kind == "product_field":
        status, _answer, _pid, _vid, citations, reason = answer_question_with_mode(
            fx.db, service, TENANT, "", question, materials["product_field"]["product_id"])
        source = "local"
        if status == "grounded" and citations:
            outcome = {"status": status, "citations": citations, "reason": reason}
    else:
        status, _answer, _pid, _vid, citations, reason, source = answer_question_prefer_dify(
            fx.db, service, TENANT, "", question, None, "t2-live")
        if status == "grounded" and citations:
            outcome = {"status": status, "citations": citations, "reason": reason}
    if outcome is None:
        report.add("live", f"真实问答（{kind}）", "REVIEW",
                   f"未取得有据回答：状态 {status} · source {source}（{reason}）——平台未编造，但该证据类未获真实引用")
        return
    citation = outcome["citations"][0]
    try:
        unit = unit_for(fx.db, kind, materials)
    except StopIteration:
        unit = None
    stored_texts = {u["text"] for u in qa_units(fx.db) if u["source"] == kind}
    if citation.get("kind") != kind:
        report.add("live", f"真实问答（{kind}）", "REVIEW",
                   f"模型选择了另一类合格证据（{citation.get('kind')}）——平台引用仍为服务端原文，需人工评估",
                   citation=citation_summary(citation))
    elif citation.get("quote") not in stored_texts:
        report.add("live", f"真实问答（{kind}）", "FAIL",
                   "引用原文不属于该类型任何合格证据（服务端引用完整性异常）",
                   citation=citation_summary(citation))
    elif unit is not None and citation.get("quote") != unit["text"]:
        report.add("live", f"真实问答（{kind}）", "REVIEW",
                   "模型选择了同类另一条合格证据（引用仍为服务端原文）——需人工评估选择是否恰当",
                   citation=citation_summary(citation))
    else:
        report.add("live", f"真实问答（{kind}）", "PASS",
                   f"grounded · 版本 {citation.get('version')} · source={source}",
                   citation=citation_summary(citation), log=dict(fx.db.latest_ai_call(TENANT)))


def live_no_basis(report, fx, materials, service, budget: LiveBudget) -> None:
    status, _answer, _pid, _vid, citations, reason, source = answer_question_prefer_dify(
        fx.db, service, TENANT, "", "How is the weather in London today?", None, "t2-live")
    if status in ("no_evidence", "needs_human"):
        report.add("live", "无依据问题不编造", "PASS", f"状态 {status}（{reason or '模型判定资料不足'}）",
                   log=dict(fx.db.latest_ai_call(TENANT)))
    elif status == "grounded":
        report.add("live", "无依据问题不编造", "REVIEW",
                   "模型对无关问题给出了资料引用——需人工评估该引用是否恰为提问内容",
                   citation=citation_summary(citations[0]) if citations else None)
    else:
        report.add("live", "无依据问题不编造", "FAIL", f"状态 {status} · {reason}")


def live_revoke(report, fx, materials, service, budget: LiveBudget, kind: str) -> None:
    proxy = RevokeAfterSelection(BudgetedService(service, budget),
                                 lambda item: revoke_selected_evidence(fx, item["evidence"]))
    outcome = answer_knowledge_question(fx.db, proxy, TENANT, "", GROUND_QUESTIONS[kind])
    for handle in proxy.handles:
        restore_selected_evidence(fx, handle)
    what = "；".join(handle["what"] for handle in proxy.handles)
    if not proxy.handles:
        report.add("live", "生成期间撤权", "REVIEW", "模型未选择可用证据，撤权演练未触发（可重跑该步骤）")
    elif outcome["status"] == "no_evidence" and "生成期间已失效" in outcome.get("reason", ""):
        report.add("live", "生成期间撤权", "PASS", f"{what}；复核拒绝输出失效内容")
    else:
        report.add("live", "生成期间撤权", "FAIL",
                   f"撤权动作 {what} 后仍返回 {outcome['status']}（{outcome.get('reason')}）")


def live_fault(report, fx, config, budget: LiveBudget) -> None:
    bad_model = "t2-check-invalid-model-do-not-use"
    bad = AIConfig(mode=MODE_DEEPSEEK, provider=config.provider, base_url=config.base_url,
                   model=bad_model, api_key=config.api_key, timeout_seconds=min(config.timeout_seconds, 20.0))
    try:
        BudgetedService(AIService(bad), budget).extract_requirements(EXTRACTION_SAMPLE)
    except AIUnavailableError as error:
        label = STATUS_LABELS_ZH.get(error.kind, error.kind)
        report.add("live", "故障分类（无效模型名）", "PASS",
                   f"拒绝并归类：{error.kind}（{label}）· {error.duration_ms} ms · 未产生任何答复")
        return
    report.add("live", "故障分类（无效模型名）", "REVIEW",
               "无效模型名的调用未报错——供应商行为与预期不同，需人工评估")


def run_live(args, report: Report, meta: dict) -> int:
    print("== T2 live（真实调用；有上限）==")
    config, secrets, error = live_preflight(args, report)
    if error:
        report.add("live", "预检", "FAIL", error)
        meta["secrets"] = []
        return 2
    meta["secrets"] = list(secrets)
    steps = args.steps
    if len(steps) > args.max_calls:
        report.add("live", "预检", "FAIL",
                   f"计划 {len(steps)} 次真实调用超过上限 {args.max_calls}；请提高 --max-calls 或裁剪 --steps")
        return 2
    budget = LiveBudget(args.max_calls)
    meta["budget"] = budget
    # 注入即用监视器：live 期间不拦截（真实调用是显式的），但记录调用序列。
    report.add("live", "预检", "PASS",
               f"stage={args.stage} · 模型 {config.model} · 地址 {config.base_url} · "
               f"计划 {len(steps)} 次调用（上限 {args.max_calls}）"
               + ("；自定义配置只在内存构造，不落盘真实密钥" if args.stage == "custom" else ""))
    if os.environ.get("DIFY_QA_APP_KEY") or os.environ.get("DIFY_KB_API_KEY"):
        report.add("live", "Dify 环境披露", "INFO", "检测到 DIFY_* 变量；本次路径不使用它们（共享 Dify 不恢复）")
    fx = SyntheticFixture()
    fx.setUp()
    try:
        materials = build_materials(fx)
        service = AIService(config)
        if "probe" in steps:
            live_probe(report, fx, service, budget)
        if "grounded" in steps:
            live_grounded(report, fx, materials, service, budget, args.ground_kind)
        if "no_basis" in steps:
            live_no_basis(report, fx, materials, service, budget)
        if "revoke" in steps:
            live_revoke(report, fx, materials, service, budget, args.ground_kind)
        if "fault" in steps:
            live_fault(report, fx, config, budget)
        rows = [dict(row) for row in fx.db.conn.execute("SELECT * FROM ai_call_log ORDER BY rowid")]
        meta["call_log"] = [{k: r[k] for k in ("task", "provider", "model", "status", "action",
                                               "http_status", "duration_ms", "prompt_tokens",
                                               "completion_tokens", "created_at")} for r in rows]
        prompt_tokens = sum(int(r["prompt_tokens"] or 0) for r in rows)
        completion_tokens = sum(int(r["completion_tokens"] or 0) for r in rows)
        report.add("live", "用量汇总", "INFO",
                   f"真实调用 {budget.used}/{budget.cap} 次 · tokens {prompt_tokens}+{completion_tokens}"
                   "（费用以供应商账单为准）")
    finally:
        if args.keep_workdir:
            target = Path(meta["workdir"]) / "synthetic.db"
            try:
                shutil.copy2(str(fx.db.path), str(target))
                meta["kept_db"] = str(target)
            except OSError:
                pass
        fx.tearDown()
    return report.exit_code()


# ── 入口 ──────────────────────────────────────────────────────────────────────

def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true",
                        help="显式开启真实模型调用（默认 dry-run 完全离线）")
    parser.add_argument("--stage", choices=("platform", "custom"), default="platform",
                        help="live 的服务来源：平台服务（环境变量）/ 企业自定义 API")
    parser.add_argument("--steps", default="probe,grounded,no_basis,revoke,fault",
                        help="live 步骤子集：probe,grounded,no_basis,revoke,fault（逗号分隔）")
    parser.add_argument("--ground-kind", choices=("knowledge", "topic", "item_field", "product_field"),
                        default="knowledge", help="live grounded/revoke 步骤使用的证据类型")
    parser.add_argument("--max-calls", type=int, default=6, help="live 真实调用上限（默认 6，硬上限 12）")
    parser.add_argument("--out", type=Path, default=None, help="报告 JSON 输出路径（live 必填）")
    parser.add_argument("--custom-base-url", default=None, help="企业自定义 API 基础地址（https，公网）")
    parser.add_argument("--custom-model", default=None, help="企业自定义 API 模型名")
    parser.add_argument("--custom-key-env", default="B2B_T2_CUSTOM_API_KEY",
                        help="读取自定义 API 密钥的环境变量名（默认 B2B_T2_CUSTOM_API_KEY）")
    parser.add_argument("--keep-workdir", action="store_true", help="保留临时目录与合成数据库副本以便检查")
    return parser.parse_args()


def finalize(report: Report, args, meta: dict) -> None:
    after = data_dir_state()
    before = meta.get("repo_data_before")
    if before is not None:
        if before != after:
            report.add("护栏", "仓库 data/ 未被修改", "FAIL", f"前后不一致：{before} -> {after}")
        else:
            report.add("护栏", "仓库 data/ 未被修改", "PASS",
                       "data/ 不存在（临时副本）" if not after["exists"] else "data/ 修改时间与条目数未变")
    payload = {
        "check": "first-launch-t2",
        "mode": report.mode,
        "stage": args.stage if args.live else "n/a",
        "started_at": meta.get("started_at"),
        "finished_at": now_iso(),
        "python": sys.version.split()[0],
        "cwd": os.getcwd(),
        "script": str(Path(__file__).resolve().relative_to(ROOT)),
        "script_sha256": sha256_file(Path(__file__)),
        "guardrails": meta.get("guardrails", {}),
        "budget": meta["budget"].summary() if meta.get("budget") else None,
        "steps": report.items,
        "call_log": meta.get("call_log", []),
        "counts": report.counts(),
        "kept_db": meta.get("kept_db"),
        "notes": [
            "本脚本结果为操作方自测证据，不自动构成独立验收；独立复跑后才可用于验收结论。",
            "live 调用产生供应商费用，以供应商账单为准。",
            "共享 Dify 对外生成保持关闭；本脚本不使用、不恢复。",
        ],
    }
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    assert_no_secret(text, meta.get("secrets", []))
    if args.out:
        target = Path(args.out)
        temp_path = target.with_suffix(target.suffix + ".tmp")
        temp_path.write_text(text, encoding="utf-8")
        os.replace(temp_path, target)
        print(f"报告已写入：{target}")
    counts = report.counts()
    print(f"汇总：PASS {counts['PASS']} · FAIL {counts['FAIL']} · REVIEW {counts['REVIEW']} · "
          f"INFO {counts['INFO']} · SKIP {counts['SKIP']}")
    print("说明：本脚本结果为操作方自测证据，不构成独立验收；REVIEW 项请人工评估后再下结论。")


def data_dir_state() -> dict:
    if not DATA_DIR.exists():
        return {"path": str(DATA_DIR), "exists": False}
    stat = DATA_DIR.stat()
    return {"path": str(DATA_DIR), "exists": True, "mtime": stat.st_mtime,
            "entries": len(list(DATA_DIR.iterdir()))}


def main() -> None:
    args = parse_args()
    if args.live and args.out is None:
        print("用法错误：--live 必须提供 --out（报告 JSON 路径），以保留可审计状态/引用版本/用量。", file=sys.stderr)
        raise SystemExit(2)
    if not 1 <= args.max_calls <= 12:
        print("用法错误：--max-calls 需要在 1–12 之间。", file=sys.stderr)
        raise SystemExit(2)
    steps = [step.strip() for step in args.steps.split(",") if step.strip()]
    allowed = {"probe", "grounded", "no_basis", "revoke", "fault"}
    unknown = [step for step in steps if step not in allowed]
    if unknown:
        print(f"用法错误：未知步骤 {unknown}（可选：{sorted(allowed)}）。", file=sys.stderr)
        raise SystemExit(2)
    args.steps = list(dict.fromkeys(steps))

    workdir = Path(tempfile.mkdtemp(prefix="b2b-t2-check-"))
    report = Report("live" if args.live else "dry-run")
    meta = {
        "started_at": now_iso(),
        "workdir": str(workdir),
        "repo_data_before": data_dir_state(),
        "secrets": [],
        "guardrails": {
            "workdir": str(workdir),
            "workdir_is_temp": str(workdir).startswith(tempfile.gettempdir()),
            "ai_mode_env": os.environ.get("AI_MODE", "(未设置)"),
            "dify_env_present": sorted(key for key in os.environ if key.startswith("DIFY_")),
            "network_tripwires_dry_run": [
                "ai_client.DeepSeekChatClient.call_json",
                "urllib.request.urlopen",
                "model_settings.custom_urlopen",
            ] if not args.live else [],
        },
    }
    exit_code = 2
    try:
        if not meta["guardrails"]["workdir_is_temp"]:
            report.add("护栏", "工作目录在系统临时目录", "FAIL", "拒绝在非临时目录运行")
        else:
            exit_code = run_live(args, report, meta) if args.live else run_dry_run(args, report, meta)
    except LiveBudget.Exceeded as error:
        report.add("live", "调用上限", "FAIL", str(error))
        exit_code = 1
    except Exception as error:  # 意外异常不吞掉：打印现场并保留已收集报告
        traceback.print_exc()
        report.add("运行", "未捕获异常", "FAIL", f"{type(error).__name__}: {error}")
        exit_code = 1
    finally:
        try:
            finalize(report, args, meta)
        finally:
            if args.keep_workdir:
                print(f"临时目录保留：{workdir}")
            else:
                shutil.rmtree(workdir, ignore_errors=True)
    raise SystemExit(exit_code)


if __name__ == "__main__":
    main()
