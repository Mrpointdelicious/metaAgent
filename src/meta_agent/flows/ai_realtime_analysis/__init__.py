"""
创建日期：2026-11-16
文件功能：AI实时分析独立流程（对齐 Dify「初始评估.yml」，advanced-chat，17 节点）。

链路（双模式，由 query 决定）：

模式 A——初始评估（query 含「初始评估」，或 JSON 报文 InitialEvaluation 字段非空）：
  1. webyun 登录（与问诊房间同一服务账号）；
  2. GET GetUserMedicalInfomationNew?Phone=patientphone   → 病历信息；
  3. GET GetUserIdAndRole?Phone=patientphone             → UserId / TenantId；
  4. GET GetMedicalRecordTemplateContent?userId=UserId   → 补充信息；
  5. GET GetOnlineConsultationRecords?Phone=patientphone → 历史就诊记录；
  6. LLM（初始评估生成器提示词）→ 结构化评估文本；
  7. 会话变量 patient_context ← 评估文本（后续实时分析用它做基线）；
  8. 输出 {"event":"analysis_record","display_text":"[Answer:]<文本>\\n\\n","payload":{}}。

模式 B——实时对话分析（其余 query）：
  1. 解析 query：前端真实报文是 JSON 字符串
     {"InitialEvaluation":null,"role":"Doctor","timeStamp":...,"from":"医生1","message":"..."}，
     取 message 作为对话原文（带说话人前缀）；纯文本 query 原样使用；
  2. 读取会话变量 patient_context（初始评估基线，可为空）；
  3. LLM（实时对话分析助手提示词，输入对话原文 + patient_context）→ 50-200 字建议；
  4. 同样的 analysis_record 事件封装。

与 Dify 的差异：HTTP 节点失败不中断（告警后按空继续，仅 LLM 无输出视为不可用），
不会出现「网络异常」兜底回复；phone 缺失时同样按空数据继续。
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any

from meta_agent.contracts import DomainError
from meta_agent.flows.consultation_room.client import ConsultationClient
from meta_agent.flows.consultation_room.medical_record import (
    MEDICAL_INFO_URL,
    TEMPLATE_CONTENT_URL,
    USER_ROLE_URL,
    _extract_content,
    _get,
    _login,
    _phone,
    parse_user_role,
)
from meta_agent.flows.fixed_task import FixedTaskFlow

from .prompts import build_dialogue_prompt, build_initial_prompt

if TYPE_CHECKING:
    from meta_agent.application.context import RunContext

logger = logging.getLogger(__name__)

# 历史就诊记录（Dify「获取问诊记录」节点）。
ONLINE_CONSULTATION_RECORDS_URL = (
    "http://192.168.8.71:8085/webyun/api/SysMedical/GetOnlineConsultationRecords"
)

# 触发初始评估的关键词（Dify「条件分支 2」：sys.query contains 初始评估）。
INITIAL_ASSESSMENT_KEYWORD = "初始评估"

# 前端事件封装（Dify「规划输出」/「代码执行 4」两个节点的同一逻辑）。
_EVENT_NAME = "analysis_record"


def parse_query_payload(query: str) -> dict[str, Any] | None:
    """前端实时分析 query 是 JSON 字符串报文，解析失败按纯文本处理返回 None。"""
    text = (query or "").strip()
    if not (text.startswith("{") and text.endswith("}")):
        return None
    try:
        payload = json.loads(text)
    except ValueError:
        return None
    return payload if isinstance(payload, dict) else None


def is_initial_assessment(query: str) -> bool:
    """Dify「条件分支 2」：query 包含「初始评估」即走初始评估链路；
    JSON 报文中 InitialEvaluation 字段非空同样视为初始评估触发。"""
    if INITIAL_ASSESSMENT_KEYWORD in (query or ""):
        return True
    payload = parse_query_payload(query)
    return bool(payload and payload.get("InitialEvaluation"))


def extract_dialogue_text(query: str) -> str:
    """从 query 提取对话原文：JSON 报文取 message（带说话人前缀），否则原样返回。

    Dify 原版把整段 JSON 塞进提示词的「医患对话内容」，JSON 语法噪声会干扰分析；
    这里提取 message 并保留说话人，语义等价且更干净。
    """
    payload = parse_query_payload(query)
    if not payload:
        return query or ""
    message = str(payload.get("message") or "")
    speaker = str(payload.get("from") or "")
    if not message:
        return ""
    return f"{speaker}：{message}" if speaker else message


def wrap_analysis_event(text: str) -> str:
    """把分析文本封装成 analysis_record 事件的 JSON 字符串。"""
    event = {
        "event": _EVENT_NAME,
        "display_text": "[Answer:]" + (text or "") + "\n\n",
        "payload": {},
    }
    return json.dumps(event, ensure_ascii=False)


async def _initial_assessment(ctx: RunContext, model: Any) -> str:
    """模式 A：四路取数 → LLM 生成初始评估 → 缓存 patient_context。"""
    if model is None:
        raise DomainError(
            "analysis_model_unavailable", "评估分析模型暂不可用。", outcome="unavailable"
        )

    client = ConsultationClient(ctx)
    token = await _login(client, ctx.settings)

    medical_body = template_body = visit_body = ""
    phone = _phone(ctx, "patientphone", "phone")
    if not phone:
        logger.warning(
            "initial assessment skipped, patient phone missing",
            extra={
                "run_id": ctx.record.run_id,
                "event_type": "analysis_initial_missing_phone",
            },
        )
    else:
        async def _fetch(step: str, label: str, url: str, params: dict[str, str]) -> str:
            try:
                resp = await _get(client, url, token=token, params=params, step=step, label=label)
                return resp.text
            except DomainError as exc:
                logger.warning(
                    "initial assessment source unavailable, continuing with empty",
                    extra={
                        "run_id": ctx.record.run_id,
                        "event_type": "analysis_source_unavailable_continuing",
                        "fields": {"code": exc.code, "step": step},
                    },
                )
                return ""

        medical_body = await _fetch(
            "analysis_medical", "病历信息查询", MEDICAL_INFO_URL, {"Phone": phone}
        )
        role_body = await _fetch(
            "analysis_user_role", "用户角色查询", USER_ROLE_URL, {"Phone": phone}
        )
        try:
            user_id = parse_user_role(role_body)["UserId"]
        except ValueError:
            user_id = ""
        template_body = await _fetch(
            "analysis_template", "病历模板查询", TEMPLATE_CONTENT_URL, {"userId": user_id}
        )
        visit_body = await _fetch(
            "analysis_visit_records",
            "历史就诊记录查询",
            ONLINE_CONSULTATION_RECORDS_URL,
            {"Phone": phone},
        )

    await ctx.llm_budget.take()
    response = await model.ainvoke(
        build_initial_prompt(medical_body, template_body, visit_body),
        config={
            "tags": ["ai_realtime_analysis", "initial_assessment"],
            "metadata": {"component": "ai_realtime_analysis_initial"},
        },
    )
    text = _extract_content(response).strip()
    if not text:
        raise DomainError("analysis_empty", "暂未生成有效分析内容。", outcome="unavailable")

    # 会话变量缓存（Dify「变量赋值」：conversation.patient_context over-write）。
    ctx.memory.set_variable("patient_context", text)
    ctx.record.metrics["analysis_mode"] = "initial"
    ctx.record.metrics["analysis_context_cached"] = True
    return text


async def _dialogue_analysis(ctx: RunContext, query: str, model: Any) -> str:
    """模式 B：query 对话原文 + 缓存的初始评估 → 实时对话分析建议。"""
    if model is None:
        raise DomainError(
            "analysis_model_unavailable", "评估分析模型暂不可用。", outcome="unavailable"
        )

    patient_context = ctx.memory.get_variable("patient_context") or ""
    await ctx.llm_budget.take()
    response = await model.ainvoke(
        build_dialogue_prompt(query, patient_context),
        config={
            "tags": ["ai_realtime_analysis", "dialogue_analysis"],
            "metadata": {"component": "ai_realtime_analysis_dialogue"},
        },
    )
    text = _extract_content(response).strip()
    if not text:
        raise DomainError("analysis_empty", "暂未生成有效分析内容。", outcome="unavailable")

    ctx.record.metrics["analysis_mode"] = "dialogue"
    ctx.record.metrics["analysis_context_available"] = bool(patient_context)
    return text


async def run_analysis(ctx: RunContext, query: str, model: Any) -> str:
    """双模式入口：返回 analysis_record 事件的 JSON 字符串（对齐 Dify answer）。"""
    if is_initial_assessment(query):
        text = await _initial_assessment(ctx, model)
    else:
        text = await _dialogue_analysis(ctx, extract_dialogue_text(query), model)
        ctx.record.metrics["analysis_query_format"] = (
            "json" if parse_query_payload(query) else "text"
        )
    return wrap_analysis_event(text)


class Flow(FixedTaskFlow):
    branch = "ai_realtime_analysis"
    task_name = "评估分析"
    metrics_key = "analysis_generated"

    async def _pipeline(self, ctx: RunContext, model: Any) -> str:
        return await run_analysis(ctx, ctx.query, model)
