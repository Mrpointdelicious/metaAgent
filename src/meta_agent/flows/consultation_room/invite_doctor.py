"""
创建日期：2026-10-14
文件功能：邀请医生子流程。

流程（与 Dify 原工作流一致）：
  1. POST Login/Login（复用 medical_record._login）→ parse_token 提取 token；
  2. GET /api/users/all（Bearer token + inviterPhone=requestphone）→ 在线医生列表 body；
  3. GET SysUserInfo/GetUserMedicalInfomationNew（Bearer token + Phone=patientphone）
     → parse_disease 提取病症（PatientDetails.MedicalHistory 首个逗号前）；
  4. 有在线医生 → LLM（医生列表 + 病症）生成推荐文本；无在线医生 → 跳过 LLM；
  5. parse_doctor_recommendation 把（医生列表 + 推荐文本）封装为 doctor_recommendation 事件，
     并产出 doctor_ids / doctor_names 两个会话变量；
  6. 返回 result JSON 字符串作为 answer。
"""

import json
import logging
from typing import Any

from meta_agent.application.context import RunContext
from meta_agent.contracts import DomainError

from .medical_record import MEDICAL_INFO_URL, _extract_content, _get, _login, _phone
from .prompts import DOCTOR_RECOMMEND_PROMPT

logger = logging.getLogger(__name__)

DOCTORS_ALL_URL = "http://192.168.8.71:4000/api/users/all"


# --------------------------------------------------------------------------- #
# 解析函数（照搬 Dify 代码节点逻辑）
# --------------------------------------------------------------------------- #

def parse_disease(arg: Any) -> dict[str, str]:
    """从病历信息接口响应中提取「疾病」字段（PatientDetails.MedicalHistory 首个逗号前）。"""
    root = arg
    if isinstance(arg, str):
        try:
            root = json.loads(arg)
        except Exception:
            return {"disease": ""}
    if not isinstance(root, dict):
        return {"disease": ""}

    body = root.get("body", root)
    if isinstance(body, str):
        try:
            body = json.loads(body)
        except Exception:
            return {"disease": ""}
    if not isinstance(body, dict):
        return {"disease": ""}

    data = body.get("data")
    patient = data.get("PatientDetails") if isinstance(data, dict) else None
    disease = patient.get("MedicalHistory", "") if isinstance(patient, dict) else ""

    if isinstance(disease, str):
        disease = disease.split("，")[0].split(",")[0].strip()

    return {"disease": disease or ""}


def _to_int(value: Any) -> Any:
    """尽量转成 int，失败则保留原值。"""
    try:
        return int(value)
    except (TypeError, ValueError):
        return value


def _extract_doctors(arg1: Any) -> list[dict]:
    """从在线医生列表响应中提取 data 数组（与 parse_doctor_recommendation 同源）。"""
    root = arg1
    if isinstance(arg1, str):
        try:
            root = json.loads(arg1)
        except Exception:
            root = None

    doctors: list[dict] = []
    if isinstance(root, dict):
        body = root.get("body", root)
        if isinstance(body, str):
            try:
                body = json.loads(body)
            except Exception:
                body = None
        if isinstance(body, dict) and isinstance(body.get("data"), list):
            doctors = body["data"]
    return doctors


def parse_doctor_recommendation(arg1: Any, arg2: Any) -> dict[str, Any]:
    """
    将医生推荐结果封装为「医生推荐」事件。
    arg1: 在线医生列表 JSON（含 data 数组，每位医生有 id/name/phone/tenantName 等）
    arg2: LLM 生成的医生推荐文本
    输出：
      - result: JSON 字符串（事件本体）
      - has_doctor: string，"true"/"false"，表示是否有在线医生
      - doctor_ids: array[number]，在线医生 id 列表，无医生时为 []
      - doctor_names: array[string]，在线医生姓名列表，无医生时为 []
    """
    doctors = _extract_doctors(arg1)

    if doctors:
        # 有在线医生：从列表映射结构化字段，并收集医生 id
        payload_doctors = []
        doctor_ids = []
        doctor_names = []
        for i, d in enumerate(doctors):
            payload_doctors.append({
                "display_index": i + 1,
                "doctorId": d.get("id"),
                "name": d.get("name", ""),
                "phone": d.get("phone", ""),
                "tenantName": d.get("tenantName", ""),
                "status": 0,
            })
            doctor_ids.append(_to_int(d.get("id")))
            doctor_names.append(d.get("name", ""))
        event = {
            "event": "doctor_recommendation",
            "display_text": "[Answer:]" + (arg2 if isinstance(arg2, str) else "") + "\n",
            "payload": {"doctors": payload_doctors},
        }
        has_doctor = True
    else:
        # 无在线医生：固定兜底文案，payload 为空
        event = {
            "event": "doctor_recommendation",
            "display_text": "[Answer:]当前没有在线医生。建议您稍后再试，或我可以先为您发送离线邀请，待医生上线后通知。\n",
            "payload": {},
        }
        has_doctor = False
        doctor_ids = []
        doctor_names = []

    return {
        "result": json.dumps(event, ensure_ascii=False),
        "has_doctor": "true" if has_doctor else "false",
        "doctor_ids": doctor_ids,
        "doctor_names": doctor_names,
    }


# --------------------------------------------------------------------------- #
# 结果封装与编排
# --------------------------------------------------------------------------- #

# 在线医生列表接口不可用时的固定输出。
_DOCTORS_UNAVAILABLE_RESULT = json.dumps(
    {
        "event": "doctor_recommendation",
        "display_text": "[Answer:]获取在线医生信息异常，请联系管理员",
        "payload": {},
    },
    ensure_ascii=False,
)


def _build_prompt(doctor_list: str, disease: str) -> str:
    return DOCTOR_RECOMMEND_PROMPT.replace(
        "{{#1789444010244.doctor_list#}}", doctor_list
    ).replace("{{#17888359554260.disease#}}", disease)


async def run_invite_doctor(ctx: RunContext, model: Any) -> str:
    """执行邀请医生完整链路，返回最终 result JSON 字符串。"""
    client = ctx.backend.client

    token = await _login(client)

    request_phone = _phone(ctx, "requesterphone", "phone")
    if not request_phone:
        raise DomainError(
            "request_phone_missing", "缺少请求手机号，无法查询在线医生。", outcome="clarification"
        )

    # 在线医生列表接口不可用时不阻断流程，下发固定异常事件。
    try:
        doctors_resp = await _get(
            client,
            DOCTORS_ALL_URL,
            token=token,
            params={"inviterPhone": request_phone},
            step="invite_doctor_doctors",
            label="在线医生列表查询",
        )
    except DomainError as exc:
        logger.warning(
            "doctor list unavailable, returning fixed error event",
            extra={
                "run_id": ctx.record.run_id,
                "event_type": "invite_doctor_doctors_unavailable",
                "fields": {"code": exc.code},
            },
        )
        ctx.record.metrics["invite_doctor_doctors_unavailable"] = True
        return _DOCTORS_UNAVAILABLE_RESULT

    doctors = _extract_doctors(doctors_resp.text)

    # 病症是可选输入：患者手机号缺失或病历接口不可用都不阻断推荐，告警后按空继续。
    patient_phone = _phone(ctx, "patientphone", "phone")
    disease = ""
    if not patient_phone:
        logger.warning(
            "disease skipped, patient phone missing",
            extra={
                "run_id": ctx.record.run_id,
                "event_type": "invite_doctor_disease_skipped",
            },
        )
    else:
        try:
            medical_resp = await _get(
                client,
                MEDICAL_INFO_URL,
                token=token,
                params={"Phone": patient_phone},
                step="invite_doctor_medical",
                label="病历信息查询",
            )
            disease = parse_disease(medical_resp.text).get("disease", "")
        except DomainError as exc:
            logger.warning(
                "disease unavailable, continuing with empty disease",
                extra={
                    "run_id": ctx.record.run_id,
                    "event_type": "invite_doctor_disease_unavailable",
                    "fields": {"code": exc.code},
                },
            )

    # 有在线医生才走 LLM 推荐；无医生时 arg2 为空，parse_doctor_recommendation 走兜底文案。
    if doctors:
        if model is None:
            raise DomainError(
                "invite_doctor_model_unavailable", "医生推荐模型暂不可用。", outcome="unavailable"
            )
        prompt = _build_prompt(json.dumps(doctors, ensure_ascii=False), disease)
        await ctx.llm_budget.take()
        response = await model.ainvoke(
            prompt,
            config={
                "tags": ["consultation_room", "invite_doctor"],
                "metadata": {"component": "consultation_room_invite_doctor"},
            },
        )
        llm_text = _extract_content(response)
    else:
        llm_text = ""

    final = parse_doctor_recommendation(doctors_resp.text, llm_text)

    ctx.record.metrics["invite_doctor_online_count"] = len(doctors)
    # 新建/覆盖两个会话变量，供后续分支复用。
    ctx.memory.set_variable("doctor_ids", final["doctor_ids"])
    ctx.memory.set_variable("doctor_names", final["doctor_names"])

    return final["result"]
