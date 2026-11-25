"""
创建日期：2026-11-18
文件功能：AI问诊流程的 AI 分诊分支 —— 病史取数 + 分诊对话 + 初步判断后自动挂号。

移植自 Dify「AI诊室(测试)」的「获取病史 / 提取病史 / LLM 6 / LLM 7 /
代码执行 18(20) / HTTP 请求 8」节点群：

    1. webyun 登录（与问诊房间同一服务账号，带重试）；
    2. GET GetUserMedicalInfomationNew?Phone=patientphone → 病历信息；
    3. 提取 MedicalHistory → 有病史走「围绕病史追问」提示词，无病史走
       「自然交流起步」提示词（两者都以固定格式终结：初步判断/最可能的原因）；
    4. 输出含「初步判断 / 最可能的原因与初步建议」且命中康复疾病关键词表时，
       查在线医生并拼接挂号情况（无医生 / 1 个自动挂号 / 多个让选择）。
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any

from meta_agent.adapters.text import model_text  # noqa: F401
from meta_agent.flows.consultation_room.client import ConsultationClient
from meta_agent.flows.consultation_room.medical_record import (
    MEDICAL_INFO_URL,
    _extract_content,
    _get,
    _login,
    _phone,
)
from meta_agent.flows.consultation_room.transcript import build_transcript

from .booking import deliver_recommendation, fetch_envelope
from .knowledge import search_maxkb
from .prompts import build_triage_prompt

if TYPE_CHECKING:
    from meta_agent.application.context import RunContext

logger = logging.getLogger(__name__)

# 康复疾病关键词表（Dify「代码执行 18 / 20」；只检查「初步判断：」之后的部分）。
DISEASE_KEYWORDS = [
    # 原有疾病
    "骨质疏松", "脑卒中早期", "脑卒中重症", "脑卒中",
    "脑卒中患者立位平衡障碍", "帕金森", "膝骨关节炎", "髋关节置换",
    "肩周炎", "腰椎间盘突出", "颈椎病", "肌少症", "糖尿病足",
    "COPD", "冠心病术后康复", "衰弱综合征", "跌倒风险", "腰椎疾病",
    "慢性疼痛", "脊髓损伤", "周围神经损伤", "周围神经病变",
    # 神经性疾病
    "多发性硬化", "格林巴利综合征", "脊髓小脑性共济失调", "面瘫", "脑瘫",
    "脊髓炎", "路易体痴呆", "阿尔茨海默病", "重症肌无力",
    "周围神经炎", "运动神经元病", "亨廷顿舞蹈症",
    # 骨折/骨损伤
    "骨折", "髋部骨折", "脊柱骨折", "椎体骨折", "肱骨骨折",
    "桡骨骨折", "胫腓骨骨折", "股骨骨折", "骨盆骨折", "踝部骨折",
    "肩胛骨骨折", "骨折术后", "骨不连", "骨延迟愈合",
    # 其他康复疾病/术后
    "截肢", "截瘫", "脑外伤", "脊髓损伤后遗症",
]

# 分诊终结输出的两个标志（LLM 6 / LLM 7 各自的固定格式第一行）。
FINAL_MARKERS = ("初步判断", "最可能的原因与初步建议")


# 患者画像字段（病历接口 PatientDetails → 提示词可读的中文标签）。
# 只列分诊真正用得上的健康相关字段，值非空才纳入，避免把空字段塞给模型。
PATIENT_PROFILE_FIELDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("姓名", ("PatientName", "Name", "UserName")),
    ("性别", ("Sex", "Gender", "PatientSex")),
    ("年龄", ("Age", "PatientAge")),
    ("出生日期", ("BirthDate", "Birthday")),
    ("身高", ("Height",)),
    ("体重", ("Weight",)),
    ("健康状况", ("HealthStatus", "HealthCondition")),
    ("既往病史", ("MedicalHistory", "PastHistory", "PreviousHistory")),
    ("过敏史", ("AllergyHistory", "Allergies", "Allergy")),
    ("用药情况", ("Medication", "CurrentMedication", "MedicationHistory")),
    ("家族史", ("FamilyHistory",)),
)


def _first_value(details: dict[str, Any], keys: tuple[str, ...]) -> str:
    """按候选键顺序取第一个非空字符串值。"""
    for key in keys:
        value = details.get(key)
        if value is None:
            continue
        text = str(value).strip()
        if text and text.lower() != "null":
            return text
    return ""


def parse_patient_profile(body: str) -> dict[str, str]:
    """从病历接口响应提取患者健康画像（Dify「获取病史」节点的完整利用）。

    返回 {中文标签: 值}，只含非空字段；解析失败返回空 dict。分诊提示词据此
    把患者真实信息（年龄/性别/病史/过敏史等）纳入判断，而非仅凭主诉臆测。
    """
    try:
        payload = json.loads(body)
    except Exception:
        return {}
    data = payload.get("data") if isinstance(payload, dict) else None
    details = data.get("PatientDetails") if isinstance(data, dict) else None
    if not isinstance(details, dict):
        return {}
    profile: dict[str, str] = {}
    for label, keys in PATIENT_PROFILE_FIELDS:
        value = _first_value(details, keys)
        if value:
            profile[label] = value
    return profile


def format_patient_profile(profile: dict[str, str]) -> str:
    """把画像 dict 渲染成提示词文本；空画像返回空串。"""
    return "\n".join(f"- {label}：{value}" for label, value in profile.items())


def parse_medical_history(body: str) -> dict[str, str]:
    """从病历接口响应提取 MedicalHistory（Dify「提取病史」节点）。"""
    try:
        payload = json.loads(body)
    except Exception:
        return {"has_history": "false", "history_value": "null"}

    data = payload.get("data") if isinstance(payload, dict) else None
    details = data.get("PatientDetails") if isinstance(data, dict) else None
    medical_history = details.get("MedicalHistory", "") if isinstance(details, dict) else ""

    history_str = str(medical_history or "").strip()
    return {
        "has_history": "true" if history_str else "false",
        "history_value": history_str if history_str else "null",
    }


def detect_disease(text: str) -> bool:
    """「初步判断：」之后是否命中康复疾病关键词表（Dify「代码执行 18/20」）。"""
    if "初步判断：" in text:
        text_main = text.split("初步判断：", 1)[1]
        for stop in ["。", "\n", ";", "；"]:
            if stop in text_main:
                text_main = text_main.split(stop, 1)[0]
                break
    else:
        text_main = text
    text_main = text_main.replace(" ", "").replace("\u3000", "")
    return any(keyword in text_main for keyword in DISEASE_KEYWORDS)


def is_final_triage(text: str) -> bool:
    """分诊输出是否已到达终结格式（出现任一固定标志行）。"""
    return any(marker in text for marker in FINAL_MARKERS)


def booking_suffix(data: dict[str, Any], triage_text: str) -> str:
    """按医生在线情况拼接挂号情况（Dify「直接回复 15/16/17」口径）。"""
    result = str(data.get("result") or "")
    names = str(data.get("doctorNames") or "")
    command = str(data.get("command") or "")
    if result == "无医生":
        return (
            f"[Answer:]{triage_text} 挂号情况：当前无在线医生，"
            "您可以继续描述您的症状进行AI问诊"
        )
    if result == "一个医生":
        return (
            f"{command}[Answer:]{triage_text} "
            f"挂号情况：当前只有{names}在线，已为您挂号{names}"
        )
    return (
        f"[Answer:]{triage_text} "
        f"挂号情况：当前有多个在线医生，分别是{names}，请选择您想选择的医生"
    )


async def run_triage(ctx: RunContext, model: Any) -> str:
    """AI 分诊完整链路，返回下发给前端的文本（[Answer:] 前缀 + 可能的挂号指令）。"""
    if model is None:
        from meta_agent.contracts import DomainError

        raise DomainError(
            "consultation_model_unavailable", "分诊模型暂不可用。", outcome="unavailable"
        )

    client = ConsultationClient(ctx)
    token = ""
    history_value = "null"
    patient_profile: dict[str, str] = {}
    phone = _phone(ctx, "patientphone", "phone", "requesterphone")
    if phone:
        try:
            token = await _login(client, ctx.settings)
            resp = await _get(
                client,
                MEDICAL_INFO_URL,
                token=token,
                params={"Phone": phone},
                step="consultation_history",
                label="病史查询",
            )
            history_value = parse_medical_history(resp.text)["history_value"]
            patient_profile = parse_patient_profile(resp.text)
        except Exception as exc:  # 病史不可用按无病史分诊继续，不阻断
            logger.warning(
                "ai_consultation history unavailable, continuing without",
                extra={
                    "run_id": ctx.record.run_id,
                    "event_type": "consultation_history_unavailable",
                    "fields": {"error": type(exc).__name__},
                },
            )
    else:
        logger.warning(
            "ai_consultation history skipped, patient phone missing",
            extra={
                "run_id": ctx.record.run_id,
                "event_type": "consultation_history_missing_phone",
            },
        )

    # 医学知识检索（health 域 = clinical_kb + rehab_kb）：把患者本轮主诉、
    # 已有病史一起作为检索词，让分诊回答有循证依据。检索失败/无命中按空继续，
    # 模型退回患者信息 + 医学常识作答，不阻断分诊。
    medical_context = ""
    if getattr(ctx.settings, "consultation_medical_kb", True):
        search_terms = " ".join(
            part
            for part in (ctx.query, patient_profile.get("既往病史", ""))
            if part and part != "null"
        )
        try:
            result = await search_maxkb(ctx, search_terms, domain="health")
            kb_context = str((result or {}).get("context") or "")
            if kb_context.strip() and str((result or {}).get("status")) != "no_evidence":
                medical_context = kb_context
        except Exception as exc:
            logger.warning(
                "ai_consultation medical knowledge unavailable, continuing without",
                extra={
                    "run_id": ctx.record.run_id,
                    "event_type": "consultation_medical_kb_unavailable",
                    "fields": {"error": type(exc).__name__},
                },
            )
    ctx.record.metrics["consultation_medical_kb_hit"] = "true" if medical_context else "false"

    prompt = build_triage_prompt(
        has_history=history_value != "null",
        history_value=history_value,
        transcript=build_transcript(ctx),
        query=ctx.query,
        patient_profile=format_patient_profile(patient_profile),
        medical_knowledge=medical_context,
    )
    await ctx.llm_budget.take()
    response = await model.ainvoke(
        prompt,
        config={
            "tags": ["ai_consultation", "triage"],
            "metadata": {"component": "ai_consultation_triage"},
        },
    )
    text = _extract_content(response).strip()
    if not text:
        from meta_agent.contracts import DomainError

        raise DomainError(
            "consultation_empty", "暂未生成分诊回复。", outcome="unavailable"
        )

    if not is_final_triage(text):
        return "[Answer:]" + text

    if not detect_disease(text):
        return "[Answer:]" + text

    space = (
        getattr(ctx.scope, "space_id", "")
        or ctx.settings.doctor_default_space_id
        or ctx.settings.scene_default_space_id
    )
    try:
        import httpx

        async with httpx.AsyncClient(trust_env=False, timeout=20) as client:
            envelope = await fetch_envelope(ctx, client, space)
            data = envelope["data"]
    except Exception as exc:
        logger.warning(
            "ai_consultation booking lookup unavailable, skipping appendix",
            extra={
                "run_id": ctx.record.run_id,
                "event_type": "consultation_doctors_unavailable",
                "fields": {"error": type(exc).__name__},
            },
        )
        return "[Answer:]" + text

    return await deliver_recommendation(ctx, booking_suffix(data, text), envelope)
