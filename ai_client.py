#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""AI 调用边界：显式模式（本地规则 / DeepSeek 模型辅助）下的统一模型调用与严格校验。

边界约束（对应 docs/技术架构草案.md §10、docs/MVP-开发需求文档.md M3/M5 与 ADR-04）：

- 模式显式：``AI_MODE=rules``（默认，本地演示）或 ``deepseek``；deepseek 模式缺 API Key
  时直接拒绝启动，绝不静默落回规则结果（避免把未联调误认为已联调）。
- 模型只做两件事：把买方自由文本整理为**候选**字段值；判断资料问题对应哪项**已核准、
  已发布**字段。数值、答案文本与引用一律由后端从发布快照构造，模型输出不是核准事实。
- 服务器端调用：有限超时、有限输出长度；错误归类为内部状态（可重试 / 需配置 / 需排查），
  由调用方写入调用日志（提供方、模型、状态、耗时、用量）；不记录密钥与买家消息内容。

本模块只含纯逻辑与一个使用标准库 urllib 的最小 HTTP 客户端；业务合并、日志落库与
对外话术留在 app.py，避免把业务状态放进 AI 边界。
"""

from __future__ import annotations

import json
import math
import os
import re
import socket
import time
import urllib.error
import urllib.request
from urllib.parse import urlsplit

MODE_RULES = "rules"
MODE_DEEPSEEK = "deepseek"
MODES = (MODE_RULES, MODE_DEEPSEEK)

PROVIDER_DEEPSEEK = "deepseek"
DEEPSEEK_DEFAULT_BASE_URL = "https://api.deepseek.com"
DEEPSEEK_DEFAULT_MODEL = "deepseek-flash"
DEEPSEEK_DEFAULT_TIMEOUT_SECONDS = 20.0
DEEPSEEK_TIMEOUT_LIMIT_SECONDS = 120.0
# 输出上限默认（未显式设置时按实际通道取值）：官方通道 1024（2026-10-13 真实调用证据：低思考
# 在 512 下会 finish_reason=length 截断）；第三方兼容端点沿用原 512。显式 max_tokens 与
# DEEPSEEK_MAX_TOKENS 一律尊重，不静默改写（费用上限不会被忽略）。
DEEPSEEK_DEFAULT_MAX_TOKENS = 512
DEEPSEEK_OFFICIAL_DEFAULT_MAX_TOKENS = 1024
DEEPSEEK_MAX_TOKENS_LIMIT = 8192
DEEPSEEK_CHAT_PATH = "/chat/completions"
# 送给模型的买方文本上限（控制输入规模；超出部分截断）。
DEEPSEEK_MAX_INPUT_CHARS = 4000
# 官方 DeepSeek 通道（平台 deepseek，或企业自定义指向官方端点）的固定请求参数。
# 用户 2026-10-13 明确：模型固定 deepseek-flash、思考保持开启（不关闭）、推理强度 low
# （官方支持 low/high/max，默认档为 high）；环境变量、直接构造、运行期修改与旧企业配置
# 都只能得到这一组值，不能切到其他模型或关闭/调整思考。
DEEPSEEK_OFFICIAL_HOST = "api.deepseek.com"
DEEPSEEK_THINKING_TYPE = "enabled"
DEEPSEEK_REASONING_EFFORT = "low"

# 内部状态分类：ok 表示成功；其余为失败分类，action 给出处理建议。
STATUS_OK = "ok"
STATUS_TIMEOUT = "timeout"
STATUS_CONNECTION_ERROR = "connection_error"
STATUS_RATE_LIMITED = "rate_limited"
STATUS_SERVER_ERROR = "server_error"
STATUS_AUTH_FAILED = "auth_failed"
STATUS_INSUFFICIENT_BALANCE = "insufficient_balance"
STATUS_INVALID_REQUEST = "invalid_request"
STATUS_HTTP_ERROR = "http_error"
STATUS_INVALID_OUTPUT = "invalid_output"

ACTION_NONE = "none"
ACTION_RETRY = "retry"
ACTION_CONFIGURE = "configure"
ACTION_INVESTIGATE = "investigate"

# 失败分类 → 处理建议（可重试 / 需配置 / 需排查）。
STATUS_ACTIONS = {
    STATUS_OK: ACTION_NONE,
    STATUS_TIMEOUT: ACTION_RETRY,
    STATUS_CONNECTION_ERROR: ACTION_RETRY,
    STATUS_RATE_LIMITED: ACTION_RETRY,
    STATUS_SERVER_ERROR: ACTION_RETRY,
    STATUS_INVALID_OUTPUT: ACTION_RETRY,
    STATUS_AUTH_FAILED: ACTION_CONFIGURE,
    STATUS_INSUFFICIENT_BALANCE: ACTION_CONFIGURE,
    STATUS_INVALID_REQUEST: ACTION_INVESTIGATE,
    STATUS_HTTP_ERROR: ACTION_INVESTIGATE,
}

# 管理概览「模型接入」卡片展示用（内部界面，非买家端）。
STATUS_LABELS_ZH = {
    STATUS_OK: "成功",
    STATUS_TIMEOUT: "超时 · 可重试",
    STATUS_CONNECTION_ERROR: "连接失败 · 可重试",
    STATUS_RATE_LIMITED: "限流 · 可重试",
    STATUS_SERVER_ERROR: "服务端错误 · 可重试",
    STATUS_INVALID_OUTPUT: "输出无法解析 · 可重试",
    STATUS_AUTH_FAILED: "认证失败 · 需配置（检查 DEEPSEEK_API_KEY）",
    STATUS_INSUFFICIENT_BALANCE: "余额不足 · 需配置（DeepSeek 账户充值后重试）",
    STATUS_INVALID_REQUEST: "请求被拒绝 · 需排查",
    STATUS_HTTP_ERROR: "HTTP 错误 · 需排查",
}


class AIConfigurationError(RuntimeError):
    """配置问题：模式、密钥或参数不允许启动/调用（明确失败，不落回规则结果）。"""


class AIUnavailableError(RuntimeError):
    """一次模型调用失败，携带内部状态分类与耗时（不携带密钥或消息内容）。"""

    def __init__(self, kind: str, message: str = "", http_status: int | None = None, duration_ms: int | None = None):
        super().__init__(message or kind)
        self.kind = kind
        self.http_status = http_status
        self.duration_ms = duration_ms

    @property
    def action(self) -> str:
        return STATUS_ACTIONS.get(self.kind, ACTION_INVESTIGATE)


def classify_http_status(code: int) -> str:
    """把 DeepSeek 错误码（官方文档 400/401/402/422/429 及 5xx）映射为内部状态。"""
    if code == 401:
        return STATUS_AUTH_FAILED
    if code == 402:
        return STATUS_INSUFFICIENT_BALANCE
    if code == 429:
        return STATUS_RATE_LIMITED
    if 500 <= code <= 599:
        return STATUS_SERVER_ERROR
    if code in (400, 422):
        return STATUS_INVALID_REQUEST
    return STATUS_HTTP_ERROR


def is_official_deepseek_endpoint(base_url: str) -> bool:
    """base_url 是否指向官方 DeepSeek API 端点（api.deepseek.com）。

    企业自定义只有在指向官方端点时才套用平台同一组固定参数（模型 flash、思考开启、
    强度 low）；其他第三方 OpenAI 兼容端点保留原协议，不注入 DeepSeek 专用字段。
    """
    try:
        host = urlsplit(str(base_url or "")).hostname or ""
    except ValueError:
        return False
    return host.lower() == DEEPSEEK_OFFICIAL_HOST


class AIConfig:
    """一次运行期的 AI 配置。默认 rules；deepseek 需要显式密钥（只经环境变量读取）。"""

    def __init__(
        self,
        mode: str = MODE_RULES,
        provider: str = PROVIDER_DEEPSEEK,
        model: str = DEEPSEEK_DEFAULT_MODEL,
        base_url: str = DEEPSEEK_DEFAULT_BASE_URL,
        api_key: str = "",
        timeout_seconds: float = DEEPSEEK_DEFAULT_TIMEOUT_SECONDS,
        max_tokens: int | None = None,
    ):
        self.mode = mode
        self.provider = provider
        self.base_url = base_url
        self.api_key = api_key
        self.timeout_seconds = timeout_seconds
        self.max_tokens = max_tokens
        self._model = model

    @property
    def uses_official_deepseek(self) -> bool:
        """请求是否走官方 DeepSeek 通道：平台 deepseek 模式，或企业自定义指向官方端点。"""
        return self.provider == PROVIDER_DEEPSEEK or is_official_deepseek_endpoint(self.base_url)

    @property
    def model(self) -> str:
        """官方通道读取即固定 deepseek-flash（环境变量/直接构造/运行期修改都不能切换）。"""
        if self.uses_official_deepseek:
            return DEEPSEEK_DEFAULT_MODEL
        return self._model

    @model.setter
    def model(self, value: str) -> None:
        self._model = value

    def __repr__(self) -> str:  # 防止密钥随 repr 进入日志或测试输出
        return (
            f"AIConfig(mode={self.mode!r}, provider={self.provider!r}, model={self.model!r}, "
            f"base_url={self.base_url!r}, api_key={'***' if self.api_key else ''}, "
            f"timeout_seconds={self.timeout_seconds!r}, max_tokens={self.max_tokens!r})"
        )

    @classmethod
    def from_env(cls, environ=None, mode_override: str | None = None) -> "AIConfig":
        env = os.environ if environ is None else environ
        raw_mode = mode_override if mode_override is not None else env.get("AI_MODE", "")
        mode = str(raw_mode or MODE_RULES).strip().lower()
        if mode not in MODES:
            raise AIConfigurationError(f"AI_MODE 只支持 rules 或 deepseek，当前是 {mode!r}。")
        config = cls(mode=mode)
        if mode != MODE_DEEPSEEK:
            return config

        api_key = str(env.get("DEEPSEEK_API_KEY", "") or "").strip()
        if not api_key:
            raise AIConfigurationError(
                "AI_MODE=deepseek 但未设置 DEEPSEEK_API_KEY：为避免规则结果被误认为模型联调结果，"
                "应用拒绝启动。请先在本机配置密钥（见 README「启用 DeepSeek 模型辅助」），或改回 AI_MODE=rules。"
            )
        config.api_key = api_key

        base_url = str(env.get("DEEPSEEK_BASE_URL", "") or "").strip() or DEEPSEEK_DEFAULT_BASE_URL
        if not re.match(r"^https?://[^\s/]+", base_url):
            raise AIConfigurationError("DEEPSEEK_BASE_URL 需要是合法的 http(s) 地址（仅测试/代理场景覆盖默认值）。")
        config.base_url = base_url.rstrip("/")

        # DEEPSEEK_MODEL 仅保留兼容读取：官方通道读取模型时固定为 deepseek-flash，
        # 环境变量不能切换到其他模型（用户 2026-10-13 明确）。
        model = str(env.get("DEEPSEEK_MODEL", "") or "").strip()
        config.model = model or DEEPSEEK_DEFAULT_MODEL

        timeout_raw = str(env.get("DEEPSEEK_TIMEOUT_SECONDS", "") or "").strip()
        if timeout_raw:
            try:
                timeout = float(timeout_raw)
            except ValueError:
                raise AIConfigurationError("DEEPSEEK_TIMEOUT_SECONDS 需要是数字（秒）。") from None
            if not 0 < timeout <= DEEPSEEK_TIMEOUT_LIMIT_SECONDS:
                raise AIConfigurationError(f"DEEPSEEK_TIMEOUT_SECONDS 需要在 0–{DEEPSEEK_TIMEOUT_LIMIT_SECONDS:g} 秒之间。")
            config.timeout_seconds = timeout

        tokens_raw = str(env.get("DEEPSEEK_MAX_TOKENS", "") or "").strip()
        if tokens_raw:
            try:
                tokens = int(tokens_raw)
            except ValueError:
                raise AIConfigurationError("DEEPSEEK_MAX_TOKENS 需要是整数。") from None
            if not 1 <= tokens <= DEEPSEEK_MAX_TOKENS_LIMIT:
                raise AIConfigurationError(f"DEEPSEEK_MAX_TOKENS 需要在 1–{DEEPSEEK_MAX_TOKENS_LIMIT} 之间。")
            config.max_tokens = tokens
        return config


EXTRACTION_SYSTEM_PROMPT = """You extract pump requirements from ONE buyer message for an industrial pump presales platform. The buyer will review and edit your output before anything is confirmed, so only extract what the message states explicitly.
Return ONLY one JSON object shaped as {"values": {"<key>": {"value": "<number or text>", "unit": "<unit>"}}}. No commentary outside the JSON.
Allowed keys and formats:
- "category": pump type mentioned by the buyer (text)
- "use_case": the application or use the buyer mentions (short text)
- "flow": requested flow as a plain decimal number string; unit must be "m³/h"
- "head": requested head as a plain decimal number string; unit must be "m"
- "media": pumped liquid; use "clean water" for clean/fresh/drinking water, "chemical" for acid/alkali/chemical service; omit if the liquid is unclear
- "temperature": liquid temperature as a plain decimal number string; unit must be "°C"
- "quantity": number of units as a plain decimal number string; unit must be "units"
- "region": destination (short text)
- "delivery": requested delivery timing (short text)
Hard rules:
- Include a key only when the message states it explicitly; never guess or copy typical values.
- If the message gives a number for flow, head or temperature but no unit, omit that key (never assume a unit).
- Omit any key whose value or unit you are not sure about.
- Never output other keys. Never add explanations."""

FIELD_SELECTION_SYSTEM_PROMPT = """You route ONE buyer question about an industrial pump to a single approved record field.
You will receive the question and the list of published, approved fields for the current product. Choose at most one field whose recorded value directly answers what is asked. Never answer from your own knowledge and never invent values.
Return ONLY one JSON object: {"field": "<field key exactly as listed>"} or {"field": "none"}.
Rules:
- Choose a field only when the question asks about that field's recorded value; questions may be in any language, match meaning rather than keywords.
- Choose "none" for prices, stock, delivery timing, certificates, warranty, spare parts, service, model comparisons, or anything requiring judgement about the buyer's own application.
- Never output other keys. Never add explanations."""


def build_field_selection_user_content(question: str, allowed_fields: list[dict[str, str]]) -> str:
    lines = [
        f"- {field['key']}: {field.get('label_zh', '')} / {field.get('label_en', '')}"
        for field in allowed_fields
    ]
    return (
        "Buyer question:\n" + str(question or "").strip()[:2000] + "\n\n"
        + "Published approved fields for this product:\n" + "\n".join(lines)
    )


# ---- M3 抽取结果的严格校验（白名单字段、单位与结构；无效/含糊值一律丢弃）----------------

EXTRACTION_FIELD_KEYS = (
    "category", "use_case", "flow", "head", "media", "temperature", "quantity", "region", "delivery",
)

# 介质只接受能归入两类可解释口径的表述；其余（如 seawater/污水）无法被现有匹配规则解释，丢弃。
_CLEAN_WATER_TOKENS = (
    "清水", "净水", "clean water", "cleanwater", "fresh water", "freshwater", "clear water",
    "potable", "drinking water", "domestic water", "tap water",
)
_CHEMICAL_TOKENS = ("酸", "碱", "化工", "化学", "chemical", "acid", "alkali", "caustic", "corrosive")

_FLOW_UNIT_FORMS = {"m³/h", "m3/h", "m3h", "m³/hr", "m3/hr", "m³h"}
_HEAD_UNIT_FORMS = {"m", "meter", "meters", "metre", "metres"}
_TEMP_UNIT_FORMS = {"°c", "℃", "c", "celsius"}
_QUANTITY_UNIT_FORMS = {"units", "unit", "pcs", "pc", "pieces", "台", "套"}

_MAX_TEXT_FIELD_CHARS = 200


def _number_value(raw: object) -> float | None:
    """把模型返回的数值（数字或纯十进制字符串）解析为 float；拒绝布尔、NaN/Inf。"""
    if isinstance(raw, bool):
        return None
    if isinstance(raw, (int, float)):
        number = float(raw)
    elif isinstance(raw, str):
        text = raw.strip().replace(",", ".")
        if not text or not re.fullmatch(r"-?\d+(?:\.\d+)?", text):
            return None
        number = float(text)
    else:
        return None
    if not math.isfinite(number):
        return None
    return number


def _format_number(number: float) -> str:
    text = f"{number:.6f}".rstrip("0").rstrip(".")
    return text if text not in ("", "-", "-0") else "0"


def _normalize_bounded_number(
    item: dict, forms: set, unit: str, minimum: float, maximum: float,
    allow_negative: bool = False, require_unit: bool = True,
):
    number = _number_value(item.get("value"))
    if number is None:
        return None
    if allow_negative:
        if not minimum <= number <= maximum:
            return None
    elif not 0 < number <= maximum:
        return None
    raw_unit = str(item.get("unit") or "").strip().lower().replace(" ", "")
    if not raw_unit:
        # 单位缺失：测量类字段（流量/扬程/温度）必须由模型给出受支持的非空单位，缺单位
        # 即丢弃、保持未知——不补标准单位，避免把「50」这类模型猜测变成确定参数
        # （买家原话未给单位时可能是 L/s、gpm 或 ft，补 m³/h 会把猜测写死）。
        if require_unit:
            return None
        return _format_number(number), unit
    if raw_unit not in forms:
        return None  # 无法确认的单位不猜测、不换算，直接丢弃
    return _format_number(number), unit


def _normalize_flow(item: dict):
    return _normalize_bounded_number(item, _FLOW_UNIT_FORMS, "m³/h", 0, 1_000_000)


def _normalize_head(item: dict):
    return _normalize_bounded_number(item, _HEAD_UNIT_FORMS, "m", 0, 100_000)


def _normalize_temperature(item: dict):
    return _normalize_bounded_number(item, _TEMP_UNIT_FORMS, "°C", -273.15, 1000, allow_negative=True)


def _normalize_quantity(item: dict):
    # 数量是计数（台/套/pcs/units）：空单位不构成技术单位猜测，按计数语义接受并归一为
    # "units"；非空时仍必须是受支持的计数写法，其他单位（如 kg）一律丢弃。
    return _normalize_bounded_number(item, _QUANTITY_UNIT_FORMS, "units", 0, 10_000_000, require_unit=False)


def _normalize_category(item: dict):
    raw = item.get("value")
    if not isinstance(raw, str):
        return None
    text = raw.strip()
    if not text or not re.search(r"泵|pump", text, flags=re.IGNORECASE):
        return None
    return "水泵", ""


def _normalize_media(item: dict):
    raw = item.get("value")
    if not isinstance(raw, str):
        return None
    text = raw.strip().lower()
    if not text:
        return None
    if any(token in text for token in _CLEAN_WATER_TOKENS):
        return "清水", ""
    if any(token in text for token in _CHEMICAL_TOKENS):
        return "化工介质", ""
    return None


def _normalize_short_text(item: dict):
    raw = item.get("value")
    if not isinstance(raw, str):
        return None
    text = re.sub(r"\s+", " ", raw).strip()
    if not text or len(text) > _MAX_TEXT_FIELD_CHARS or any(ord(ch) < 32 for ch in text):
        return None
    return text, ""


_EXTRACTION_NORMALIZERS = {
    "category": _normalize_category,
    "use_case": _normalize_short_text,
    "flow": _normalize_flow,
    "head": _normalize_head,
    "media": _normalize_media,
    "temperature": _normalize_temperature,
    "quantity": _normalize_quantity,
    "region": _normalize_short_text,
    "delivery": _normalize_short_text,
}


def normalize_extraction_payload(payload: object) -> dict:
    """校验模型抽取输出：只保留白名单字段且通过单位/结构校验的候选值。

    顶层结构不是 ``{"values": {...}}`` 视为模型输出不可用（调用失败）；单条值无效则丢弃该条。
    流量/扬程/温度必须带受支持的非空单位，缺单位即丢弃（不补默认单位——买家原话未给
    单位时，补单位会把模型猜测变成确定参数）；数量按计数语义处理（空单位接受为
    ``units``，非空须为受支持的计数写法）。
    返回 ``{key: {"value": str, "unit": str}}``，不包含任何确认状态。
    """
    if not isinstance(payload, dict) or not isinstance(payload.get("values"), dict):
        raise AIUnavailableError(STATUS_INVALID_OUTPUT, "模型输出缺少 values 对象")
    extracted: dict = {}
    for key, item in payload["values"].items():
        if key not in EXTRACTION_FIELD_KEYS or not isinstance(item, dict):
            continue  # 白名单之外的字段直接丢弃
        normalized = _EXTRACTION_NORMALIZERS[key](item)
        if normalized is not None:
            extracted[key] = {"value": normalized[0], "unit": normalized[1]}
    return extracted


KNOWLEDGE_EVIDENCE_SYSTEM_PROMPT = """You route ONE customer question to at most one excerpt of a company's published, authorized knowledge.
You will receive the question and the list of allowed excerpts. Choose the single excerpt whose text directly and explicitly answers what is asked. You never write, paraphrase or extend the answer: the platform quotes the chosen excerpt verbatim and attaches its true version citation.
Return ONLY one JSON object: {"evidence": "<key exactly as listed>"} or {"evidence": "none"}.
Rules:
- Choose an excerpt only when it clearly and directly answers the question; questions may be in any language, match meaning rather than keywords.
- Choose "none" when no excerpt states the answer, or when answering would require judgement, promises, prices, stock, delivery or certificates that the excerpts do not state explicitly.
- Never use knowledge from outside the listed excerpts. Never output other keys. Never add explanations."""


def build_knowledge_evidence_user_content(question: str, evidence: list[dict[str, str]]) -> str:
    lines = []
    for item in evidence:
        label = item.get("label") or ""
        version = item.get("version_label") or ""
        lines.append(f"- {item['key']} · {label} · {version}\n  {item.get('text', '')}")
    return (
        "Customer question:\n" + str(question or "").strip()[:2000] + "\n\n"
        + "Published authorized knowledge excerpts:\n" + "\n".join(lines)
    )


def validate_knowledge_evidence_selection(payload: object, allowed_keys: set) -> str | None:
    """校验证据选择输出：命中白名单返回证据键，none/白名单之外返回 None（按资料不足处理）。

    结构缺失或类型不对视为模型输出不可用（调用失败）；可解析但不在候选内的选择
    按「没有足够依据」处理——绝不生成答案，也不视为故障。
    """
    if not isinstance(payload, dict) or "evidence" not in payload:
        raise AIUnavailableError(STATUS_INVALID_OUTPUT, "模型输出缺少 evidence 字段")
    value = payload.get("evidence")
    if value is None:
        raise AIUnavailableError(STATUS_INVALID_OUTPUT, "模型 evidence 为空")
    if not isinstance(value, str):
        raise AIUnavailableError(STATUS_INVALID_OUTPUT, "模型 evidence 不是字符串")
    text = value.strip()
    if not text or text.lower() == "none":
        return None
    if text in allowed_keys:
        return text
    return None


def validate_field_selection(payload: object, allowed_keys: set) -> str | None:
    """校验字段定位输出：命中白名单返回字段名，none/白名单之外返回 None（转人工核实）。

    结构缺失或字段类型不对视为模型输出不可用（调用失败）；可解析但不在本产品
    已核准、已发布字段内的选择按「没有可用依据」处理，不视为故障、也绝不编造答案。
    """
    if not isinstance(payload, dict) or "field" not in payload:
        raise AIUnavailableError(STATUS_INVALID_OUTPUT, "模型输出缺少 field 字段")
    field = payload.get("field")
    if field is None:
        raise AIUnavailableError(STATUS_INVALID_OUTPUT, "模型 field 为空")
    if not isinstance(field, str):
        raise AIUnavailableError(STATUS_INVALID_OUTPUT, "模型 field 不是字符串")
    text = field.strip()
    if not text or text.lower() == "none":
        return None
    if text in allowed_keys:
        return text
    return None


def _parse_completion(payload: dict) -> tuple[dict, int | None, int | None]:
    """从 Chat Completions 响应中取出模型 JSON 内容与用量；结构不符按 invalid_output 处理。"""
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        raise AIUnavailableError(STATUS_INVALID_OUTPUT, "响应缺少 choices")
    choice = choices[0]
    finish_reason = choice.get("finish_reason")
    if finish_reason != "stop":
        # 截断（length）等非正常结束不得当作成功答案：按输出不可用（可重试）处理。
        detail = "输出被 max_tokens 截断" if finish_reason == "length" else f"结束原因 {finish_reason!r}"
        raise AIUnavailableError(STATUS_INVALID_OUTPUT, f"响应未正常结束（{detail}）")
    message = choice.get("message")
    # 只读取最终 content；思考模式的 reasoning_content 不进入业务值、日志或后续请求。
    content = message.get("content") if isinstance(message, dict) else None
    if not isinstance(content, str) or not content.strip():
        raise AIUnavailableError(STATUS_INVALID_OUTPUT, "响应内容为空")
    try:
        model_output = json.loads(content)
    except json.JSONDecodeError:
        raise AIUnavailableError(STATUS_INVALID_OUTPUT, "响应内容不是合法 JSON") from None
    if not isinstance(model_output, dict):
        raise AIUnavailableError(STATUS_INVALID_OUTPUT, "响应内容不是 JSON 对象")
    usage = payload.get("usage")
    if not isinstance(usage, dict):
        usage = {}
    prompt_tokens = usage.get("prompt_tokens")
    if not isinstance(prompt_tokens, int):
        prompt_tokens = None
    completion_tokens = usage.get("completion_tokens")
    if not isinstance(completion_tokens, int):
        completion_tokens = None
    return model_output, prompt_tokens, completion_tokens


class DeepSeekChatClient:
    """最小 DeepSeek Chat Completions 客户端（标准库 urllib，无第三方依赖）。

    端点与请求字段以官方文档为准：POST {base_url}/chat/completions，
    Bearer 鉴权；官方通道固定请求参数（deepseek-flash、思考开启、强度 low）并限制
    输出长度，保证延迟与费用有界。
    """

    def __init__(self, config: AIConfig):
        if config.mode != MODE_DEEPSEEK:
            raise AIConfigurationError("DeepSeek 客户端只能在 AI_MODE=deepseek 下创建。")
        if not config.api_key:
            raise AIConfigurationError("DeepSeek 客户端缺少 API Key（AI_MODE=deepseek 必须显式配置）。")
        self.config = config

    def _endpoint(self) -> str:
        return self.config.base_url.rstrip("/") + DEEPSEEK_CHAT_PATH

    def call_json(self, system_prompt: str, user_content: str) -> tuple[dict, dict]:
        """调用一次并返回 (模型 JSON dict, meta)。meta 含 duration_ms/http_status/tokens。"""
        official = self.config.uses_official_deepseek
        max_tokens = self.config.max_tokens
        if max_tokens is None:
            # 未显式设置时按实际通道取默认：官方 1024（低思考 512 会真实截断），第三方沿用 512。
            max_tokens = DEEPSEEK_OFFICIAL_DEFAULT_MAX_TOKENS if official else DEEPSEEK_DEFAULT_MAX_TOKENS
        body = {
            "model": self.config.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_content},
            ],
            "max_tokens": max_tokens,
            "response_format": {"type": "json_object"},
            "stream": False,
        }
        if official:
            # 官方通道固定参数（用户 2026-10-13 明确）：思考保持开启、强度 low（官方默认 high）。
            # 思考模式忽略 temperature/presence_penalty/frequency_penalty，因此不再发送 temperature。
            body["thinking"] = {"type": DEEPSEEK_THINKING_TYPE}
            body["reasoning_effort"] = DEEPSEEK_REASONING_EFFORT
        else:
            # 第三方 OpenAI 兼容端点保留原协议：不注入 DeepSeek 专用字段，保留原有温度设置。
            body["temperature"] = 0.2
        request = urllib.request.Request(
            self._endpoint(),
            data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
                "Authorization": f"Bearer {self.config.api_key}",
            },
            method="POST",
        )
        started = time.monotonic()
        try:
            if self.config.provider == "custom":
                from model_settings import custom_urlopen
                open_request = custom_urlopen
            else:
                open_request = urllib.request.urlopen
            with open_request(request, timeout=self.config.timeout_seconds) as response:
                raw = response.read()
                http_status = int(response.status)
        except urllib.error.HTTPError as error:
            raise AIUnavailableError(
                classify_http_status(int(error.code)),
                f"DeepSeek 返回 HTTP {error.code}",
                http_status=int(error.code),
                duration_ms=int((time.monotonic() - started) * 1000),
            ) from None
        except (socket.timeout, TimeoutError):
            raise AIUnavailableError(
                STATUS_TIMEOUT,
                "DeepSeek 调用超时",
                duration_ms=int((time.monotonic() - started) * 1000),
            ) from None
        except urllib.error.URLError as error:
            reason = getattr(error, "reason", None)
            kind = STATUS_TIMEOUT if isinstance(reason, (socket.timeout, TimeoutError)) else STATUS_CONNECTION_ERROR
            raise AIUnavailableError(
                kind,
                "DeepSeek 连接失败",
                duration_ms=int((time.monotonic() - started) * 1000),
            ) from None
        except OSError:
            raise AIUnavailableError(
                STATUS_CONNECTION_ERROR,
                "DeepSeek 连接失败",
                duration_ms=int((time.monotonic() - started) * 1000),
            ) from None

        duration_ms = int((time.monotonic() - started) * 1000)
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            raise AIUnavailableError(STATUS_INVALID_OUTPUT, "响应不是 UTF-8 文本", http_status=http_status, duration_ms=duration_ms) from None
        text = text.strip()
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            # 非流式请求在推理开始前可能持续返回空行（官方 keep-alive 机制）；从首个 { 起再解析一次。
            brace = text.find("{")
            payload = None
            if brace > 0:
                try:
                    payload = json.loads(text[brace:])
                except json.JSONDecodeError:
                    payload = None
            if not isinstance(payload, dict):
                raise AIUnavailableError(STATUS_INVALID_OUTPUT, "响应不是合法 JSON", http_status=http_status, duration_ms=duration_ms) from None
        try:
            model_output, prompt_tokens, completion_tokens = _parse_completion(payload)
        except AIUnavailableError as error:
            error.http_status = http_status
            error.duration_ms = duration_ms
            raise
        meta = {
            "duration_ms": duration_ms,
            "http_status": http_status,
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
        }
        return model_output, meta


class AIService:
    """按显式模式提供两个任务的入口；rules 模式下不进行任何外部调用。"""

    def __init__(self, config: AIConfig):
        self.config = config
        self.client = DeepSeekChatClient(config) if config.mode == MODE_DEEPSEEK else None

    @property
    def mode(self) -> str:
        return self.config.mode

    def extract_requirements(self, text: str) -> tuple[dict, dict]:
        """M3：把一条买方原文整理为白名单内的候选值（结构/单位校验后）。"""
        if self.client is None:
            raise AIConfigurationError("当前不是 deepseek 模式，不能调用模型（规则模式请走本地规则提取）。")
        payload, meta = self.client.call_json(EXTRACTION_SYSTEM_PROMPT, str(text or "")[:DEEPSEEK_MAX_INPUT_CHARS])
        return normalize_extraction_payload(payload), meta

    def select_answer_field(self, question: str, allowed_fields: list[dict[str, str]]) -> tuple[str | None, dict]:
        """M5：判断问题对应哪项已核准、已发布字段；答案文本与引用不经过模型。"""
        if self.client is None:
            raise AIConfigurationError("当前不是 deepseek 模式，不能调用模型（规则模式请走本地规则问答）。")
        allowed_keys = {field["key"] for field in allowed_fields}
        user_content = build_field_selection_user_content(question, allowed_fields)
        payload, meta = self.client.call_json(FIELD_SELECTION_SYSTEM_PROMPT, user_content)
        return validate_field_selection(payload, allowed_keys), meta

    def select_knowledge_evidence(self, question: str, evidence: list[dict[str, str]]) -> tuple[str | None, dict]:
        """M5b：从已授权知识证据里选择至多一条回答依据；引用原文与版本由服务端生成。"""
        if self.client is None:
            raise AIConfigurationError("当前不是 deepseek 模式，不能调用模型（规则模式不生成问答答复）。")
        allowed_keys = {item["key"] for item in evidence}
        user_content = build_knowledge_evidence_user_content(question, evidence)
        payload, meta = self.client.call_json(KNOWLEDGE_EVIDENCE_SYSTEM_PROMPT, user_content)
        return validate_knowledge_evidence_selection(payload, allowed_keys), meta
