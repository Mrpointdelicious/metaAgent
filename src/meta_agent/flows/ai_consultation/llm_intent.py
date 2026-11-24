"""
创建日期：2026-11-19
文件功能：AI问诊意图识别的 LLM 语义分类（规则引擎 intents.py 的准确化补充）。

纯关键词规则对「谢谢」这类过渡语、省略句和近似说法容易误判。本模块用一次
模型调用做语义分类，输出与 intents.emit() 完全兼容的结构；模型不可用、
预算耗尽或输出非法时返回 None，由调用方降级到规则引擎，保证可用性。

场景跳转不在这里判定：点位 ID 来自本地白名单/目录（intents.detect_scene_jump），
必须由规则硬前置，避免 LLM 编造场景编码。
"""

from __future__ import annotations

import json
import logging
import re
from typing import TYPE_CHECKING, Any

from meta_agent.adapters.text import model_text

from .intents import emit

if TYPE_CHECKING:
    from meta_agent.application.context import RunContext

logger = logging.getLogger(__name__)

INTENT_SYSTEM_PROMPT = """你是医疗健康服务的意图分类器。只输出一个 JSON 对象，不要解释或多余文字。

【可选意图】
- ai_consultation：症状、病情询问、健康建议，或分诊中回答追问（如「一周了」「第二个」）。
- medical_action_request：明确挂号、看医生、预约（如「我要挂号」）。「头疼怎么办」属问诊，非挂号。
- visit_or_medical_request：明确要去某场景/地点/楼层（如「带我去三楼」）。
- qa：产品设备使用、闲聊、天气、翻译等与医疗问诊无关的问题（如「irego怎么开机」）。

【判断规则】
1. 上一轮是问诊、本轮是简短回答或感谢（如「嗯」「好的」「谢谢」「第二个」），仍判问诊，保持连续。
2. 上一轮是问诊、本轮提出明确新问题或动作（如「我要挂号」「irego怎么开机」），按新意图判断。
3. 场景跳转让位给本地点位白名单；确实表达「去某场景」才输出 visit_or_medical_request。

【输出格式】
{"intent":"意图名","confidence":0.0到1.0的小数,"reason":"不超过20字的中文理由"}

【示例】
无|我最近老是失眠 → {"intent":"ai_consultation","confidence":0.95,"reason":"描述失眠症状"}
问诊|谢谢 → {"intent":"ai_consultation","confidence":0.9,"reason":"问诊中过渡语"}
问诊|第二个 → {"intent":"ai_consultation","confidence":0.9,"reason":"问诊中省略回答"}
问诊|那我要挂号 → {"intent":"medical_action_request","confidence":0.95,"reason":"明确挂号动作"}
问诊|irego怎么开机 → {"intent":"qa","confidence":0.9,"reason":"产品使用问题"}
无|带我去三楼 → {"intent":"visit_or_medical_request","confidence":0.9,"reason":"场景跳转"}

【上一轮意图】{previous_intent}
【本轮输入】{query}"""

_VALID_INTENTS = frozenset(
    {
        "ai_consultation",
        "medical_action_request",
        "visit_or_medical_request",
        "qa",
    }
)


def _parse_intent_json(text: str) -> dict[str, Any] | None:
    """从模型输出里稳健提取 JSON 对象（容忍 ```json 围栏和前后缀文字）。"""
    if not text:
        return None
    cleaned = re.sub(r"^\s*```(?:json)?\s*|\s*```\s*$", "", text.strip(), flags=re.IGNORECASE)
    data: Any = None
    try:
        data = json.loads(cleaned)
    except (ValueError, TypeError):
        match = re.search(r"\{[^{}]*\}", cleaned, flags=re.DOTALL)
        if not match:
            return None
        try:
            data = json.loads(match.group(0))
        except (ValueError, TypeError):
            return None
    return data if isinstance(data, dict) else None


async def classify_intent_llm(
    ctx: RunContext, model: Any, query: str, previous_intent: str
) -> dict[str, Any] | None:
    """用模型做语义意图分类。返回 emit() 兼容结构；失败返回 None 由调用方降级。"""
    # 提示词内含 JSON 示例花括号，不能用 str.format（会被当占位符），改用 replace。
    prompt = INTENT_SYSTEM_PROMPT.replace("{previous_intent}", previous_intent or "无").replace(
        "{query}", query
    )
    try:
        await ctx.llm_budget.take()
        response = await model.ainvoke(
            prompt,
            config={
                "tags": ["ai_consultation", "intent"],
                "metadata": {"component": "ai_consultation_intent"},
            },
        )
    except Exception as exc:  # 预算耗尽、超时、模型不可用：一律降级规则
        logger.info(
            "ai_consultation llm intent unavailable, fallback to rules",
            extra={
                "run_id": getattr(getattr(ctx, "record", None), "run_id", ""),
                "event_type": "consultation_intent_llm_fallback",
                "fields": {"error": type(exc).__name__},
            },
        )
        return None

    text = model_text(response.content if hasattr(response, "content") else str(response)).strip()
    payload = _parse_intent_json(text)
    if payload is None:
        logger.info(
            "ai_consultation llm intent returned invalid json, fallback to rules",
            extra={
                "run_id": getattr(getattr(ctx, "record", None), "run_id", ""),
                "event_type": "consultation_intent_llm_fallback",
                "fields": {"reason": "invalid_json"},
            },
        )
        return None

    name = str(payload.get("intent") or "").strip()
    if name not in _VALID_INTENTS:
        return None

    try:
        confidence = float(payload.get("confidence", 0.8))
    except (TypeError, ValueError):
        confidence = 0.8
    confidence = min(1.0, max(0.0, confidence))
    reason = str(payload.get("reason") or "LLM 意图识别")[:50]

    scene = name == "visit_or_medical_request"
    return emit(
        name,
        reason,
        confidence,
        sel_target="jump" if scene else "",
        sel_mode="scene" if scene else "",
        sel_value=query if scene else "",
    )
