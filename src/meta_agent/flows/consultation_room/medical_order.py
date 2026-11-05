"""
创建日期：2026-10-15
文件功能：医嘱填写子流程。

流程（与 Dify 原工作流一致）：
  1. POST Login/Login（复用 medical_record._login）→ parse_token 提取 token；
  2. GET SysUserInfo/GetUserMedicalInfomationNew（Bearer token + Phone=patientphone）
     → parse_disease 提取疾病（PatientDetails.MedicalHistory 首个逗号前）；
  3. GET /api/chat-records（Bearer token + phone=requestphone）→ 对话内容；
  4. 用 MEDICAL_ORDER_PROMPT 调用 LLM，生成结构化「康复医嘱」文本；
  5. parse_medical_order 把文本解析为 medical_advice_fill 事件，返回 result JSON 字符串。
"""

import json
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
from .prompts import MEDICAL_ORDER_PROMPT

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# 解析函数（照搬 Dify 代码节点逻辑）
# --------------------------------------------------------------------------- #


def parse_medical_order(arg: Any) -> dict[str, str]:
    """
    将康复医嘱文本解析为结构化「医嘱填写」事件。
    输入 arg: 康复医嘱文本（含【主诉】【诊断】【处理】三部分）
    输出 result: JSON 字符串
    {"event":"medical_advice_fill","display_text":"[Answer:]"+原文,
     "payload":{"ChiefComplaint":...,"Diagnosis":...,"Management":...}}
    """
    text = arg if isinstance(arg, str) else ""

    def extract(start_marker, end_markers):
        idx = text.find(start_marker)
        if idx == -1:
            return ""
        start = idx + len(start_marker)
        end = len(text)
        for m in end_markers:
            j = text.find(m, start)
            if j != -1 and j < end:
                end = j
        return text[start:end].strip()

    chief = extract("【主诉】", ["【诊断】", "【处理】"])
    diagnosis = extract("【诊断】", ["【处理】"])
    management = extract("【处理】", ["[AI生成草稿]"])

    event = {
        "event": "medical_advice_fill",
        "display_text": "[Answer:]" + text + "\n\n",
        "payload": {
            "ChiefComplaint": chief,
            "Diagnosis": diagnosis,
            "Management": management,
        },
    }
    return {"result": json.dumps(event, ensure_ascii=False)}


# --------------------------------------------------------------------------- #
# 提示词组装与编排
# --------------------------------------------------------------------------- #


def _build_prompt(disease: str, chat_body: str, query: str) -> str:
    return (
        MEDICAL_ORDER_PROMPT.replace("{{#17888371559050.disease#}}", disease)
        .replace("{{#1788837210080.body#}}", chat_body)
        .replace("{{#sys.query#}}", query)
    )


async def run_medical_order(ctx: RunContext, query: str, model: Any) -> str:
    """执行医嘱填写完整链路，返回最终 result JSON 字符串。"""
    if model is None:
        raise DomainError(
            "medical_order_model_unavailable", "医嘱填写模型暂不可用。", outcome="unavailable"
        )

    client = ConsultationClient(ctx)
    token = await _login(client, ctx.settings)

    # 患者病情是可选输入：手机号缺失或病历接口不可用都不阻断，告警后按空继续。
    patient_phone = _phone(ctx, "patientphone", "phone")
    disease = ""
    if not patient_phone:
        logger.warning(
            "medical order disease skipped, patient phone missing",
            extra={
                "run_id": ctx.record.run_id,
                "event_type": "medical_order_disease_skipped_missing_phone",
            },
        )
    else:
        try:
            medical_resp = await _get(
                client,
                MEDICAL_INFO_URL,
                token=token,
                params={"Phone": patient_phone},
                step="medical_order_medical",
                label="病历信息查询",
            )
            disease = parse_disease(medical_resp.text).get("disease", "")
        except DomainError as exc:
            logger.warning(
                "medical order disease unavailable, continuing with empty",
                extra={
                    "run_id": ctx.record.run_id,
                    "event_type": "medical_order_disease_unavailable_continuing",
                    "fields": {"code": exc.code},
                },
            )

    # 对话内容是可选输入：手机号缺失或接口不可用都不阻断，告警后按空继续。
    request_phone = _phone(ctx, "requesterphone", "phone")
    chat_body = ""
    if not request_phone:
        logger.warning(
            "medical order chat records skipped, request phone missing",
            extra={
                "run_id": ctx.record.run_id,
                "event_type": "medical_order_chat_skipped_missing_phone",
            },
        )
    else:
        try:
            chat_body = await _get_chat_records(client, token, request_phone)
        except DomainError as exc:
            logger.warning(
                "medical order chat records unavailable, continuing with empty",
                extra={
                    "run_id": ctx.record.run_id,
                    "event_type": "medical_order_chat_unavailable_continuing",
                    "fields": {"code": exc.code},
                },
            )

    prompt = _build_prompt(disease, chat_body, query)

    await ctx.llm_budget.take()
    response = await model.ainvoke(
        prompt,
        config={
            "tags": ["consultation_room", "medical_order"],
            "metadata": {"component": "consultation_room_medical_order"},
        },
    )
    advice_text = _extract_content(response)

    return parse_medical_order(advice_text)["result"]
