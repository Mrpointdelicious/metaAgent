"""
创建日期：2026-11-16
文件功能：问诊内评估分析子流程：读取患者病情与对话记录，生成数据解读草稿。

沿用医嘱子流程的取数方式（webyun 登录 → 病历 → 对话记录），不依赖患者 v2 身份，
因为问诊场景只带 patientphone / requesterphone。生成内容仅为草稿，不写任何临床系统。
"""

import logging
from typing import Any

from meta_agent.application.context import RunContext
from meta_agent.contracts import DomainError

from .client import ConsultationClient
from .invite_doctor import parse_disease
from .medical_record import (
    MEDICAL_INFO_URL,
    _extract_content,
    _get,
    _get_chat_records,
    _login,
    _phone,
)
from .prompts import ANALYSIS_PROMPT

logger = logging.getLogger(__name__)


def _build_prompt(disease: str, chat_body: str, query: str) -> str:
    return (
        ANALYSIS_PROMPT.replace("{{disease}}", disease or "未提供")
        .replace("{{chat}}", chat_body or "未提供")
        .replace("{{query}}", query)
    )


async def run_analysis(ctx: RunContext, query: str, model: Any) -> str:
    """执行评估分析的取数与生成链路，返回可读的分析草稿文本。"""
    if model is None:
        raise DomainError(
            "analysis_model_unavailable", "评估分析模型暂不可用。", outcome="unavailable"
        )

    client = ConsultationClient(ctx)
    token = await _login(client, ctx.settings)

    # 病情与对话都是可选输入：缺失或接口失败只告警，不阻断；两者都空才明确不可用。
    disease = ""
    patient_phone = _phone(ctx, "patientphone", "phone")
    if patient_phone:
        try:
            medical_resp = await _get(
                client,
                MEDICAL_INFO_URL,
                token=token,
                params={"Phone": patient_phone},
                step="analysis_medical",
                label="病历信息查询",
            )
            disease = parse_disease(medical_resp.text).get("disease", "")
        except DomainError as exc:
            logger.warning(
                "analysis disease unavailable, continuing",
                extra={
                    "run_id": ctx.record.run_id,
                    "event_type": "consultation_analysis_disease_unavailable",
                    "fields": {"code": exc.code},
                },
            )

    chat_body = ""
    request_phone = _phone(ctx, "requesterphone", "phone")
    if request_phone:
        try:
            chat_body = await _get_chat_records(client, token, request_phone)
        except DomainError as exc:
            logger.warning(
                "analysis chat records unavailable, continuing",
                extra={
                    "run_id": ctx.record.run_id,
                    "event_type": "consultation_analysis_chat_unavailable",
                    "fields": {"code": exc.code},
                },
            )

    if not disease and not chat_body:
        raise DomainError(
            "analysis_inputs_unavailable",
            "没有可用于分析的病历或对话记录。",
            outcome="unavailable",
        )

    await ctx.llm_budget.take()
    response = await model.ainvoke(
        _build_prompt(disease, chat_body, query),
        config={
            "tags": ["consultation_room", "analysis"],
            "metadata": {"component": "consultation_room_analysis"},
        },
    )
    text = _extract_content(response).strip()
    if not text:
        raise DomainError("analysis_empty", "暂未生成有效分析内容。", outcome="unavailable")
    return text
