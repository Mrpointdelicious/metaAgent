"""
创建日期：2026-11-18
文件功能：AI问诊流程的挂号分支 —— 直连在线医生名单，按 0/1/多 三态下发。

移植自 Dify「AI诊室(测试)」的「在线医生 / 在线医生数 / 识别姓名 / 挂号指令」
节点群，口径与 ``medical_tools/adapters/platform/doctors.py``（房间外挂号，
GET /api/users，严格按 spaceId 过滤）一致：

    无医生   → 引导去 AI 诊室做 AI 问诊
    1 个     → 自动下发 [DoctorRecommend:]手机号 并播报已挂号
    多个     → 报名单请患者选择；再次选择时按姓名匹配（识别姓名）

[DoctorRecommend:] 的下发通道与 domains/doctors.py、场景指令一致：作为独立
action_ready 事件（携带 evidence / action_id / 去重），不拼进 [Answer:] 文本。
"""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING, Any

import httpx

from meta_agent.contracts import fingerprint
from meta_agent.medical_tools.adapters.platform.doctors import (
    build_envelope,
    fetch_doctors,
)

if TYPE_CHECKING:
    from meta_agent.application.context import RunContext

logger = logging.getLogger(__name__)

CLASS3_LABEL = "人员/医生相关咨询"


# -------------------------------------------
# 选医生表达识别（Dify「识别姓名」节点）
# -------------------------------------------

_CHOICE_KEYWORDS = [
    "选", "选择", "要", "想要", "我想要", "我想选", "想选", "想挂号", "挂号",
    "看", "找", "请", "就诊", "预约", "需要", "联系", "求", "点",
]


def extract_doctor_name(text: str) -> str:
    """识别用户输入中表达选择医生的姓名或「姓名+医生」；未识别返回空串。"""
    text = str(text or "").strip()
    if not text:
        return ""
    kw_pattern = "|".join(map(re.escape, sorted(_CHOICE_KEYWORDS, key=len, reverse=True)))
    pattern = re.compile(
        rf"(?:{kw_pattern})?\s*([\u4e00-\u9fa5]{{2,4}}医生|[\u4e00-\u9fa5]{{2,4}})",
        re.UNICODE,
    )
    match = pattern.search(text)
    if match:
        return match.group(1)
    fallback = re.compile(r"([\u4e00-\u9fa5]{2,4}医生|[\u4e00-\u9fa5]{2,4})$")
    match2 = fallback.search(text)
    return match2.group(1) if match2 else ""


# -------------------------------------------
# 三态播报（口径照搬 Dify 各 answer 节点）
# -------------------------------------------


def plain_booking_answer(data: dict[str, Any]) -> str:
    """未指名医生时的播报（Dify「无医生在线 / 一个医生在线 / 多个医生在线」）。"""
    result = str(data.get("result") or "")
    names = str(data.get("doctorNames") or "")
    command = str(data.get("command") or "")
    if result == "无医生":
        return (
            "[Answer:]当前无在线医生，您可以去到AI诊室描述您的症状进行AI问诊，"
            "如果你想去AI诊室可输入 - 带我去AI诊室"
        )
    if result == "一个医生":
        return f"{command}[Answer:]当前只有{names}在线，已为您挂号{names}"
    return f"[Answer:]当前有多个在线医生，分别是{names}，请选择您想选择的医生"


def named_booking_answer(
    name: str, names: list[str], phones: list[str]
) -> tuple[str, str]:
    """指名医生后的匹配播报（Dify「条件分支 36」0/1/2/else 四态）。

    返回 (status, answer)：status ∈ 0(无医生) / 1(所指在线) / 2(仅1人但非所指) /
    3(多人但未找到所指)。
    """
    if not names:
        return "0", "[Answer:]所有医生都离线，您可以描述您的症状进行AI问诊"
    if name in names:
        index = names.index(name)
        phone = phones[index] if index < len(phones) else ""
        command = f"[DoctorRecommend:]{phone}" if phone else ""
        return "1", f"{command}[Answer:]当前{name}在线，已为您挂号{name}"
    if len(names) == 1:
        command = f"[DoctorRecommend:]{phones[0]}" if phones else ""
        return "2", (
            f"{command}[Answer:]当前{name}已离线，当前只有{names[0]}在线，"
            f"已为您挂号{names[0]}"
        )
    return "3", (
        f"[Answer:]当前{name}已离线，目前还有{'、'.join(names)}在线，请选择你想挂号的医生"
    )


# -------------------------------------------
# 挂号分支执行
# -------------------------------------------


async def fetch_envelope(
    ctx: RunContext, client: httpx.AsyncClient, space_id: str
) -> dict[str, Any]:
    """取在线医生封套（result / doctorNames / doctorPhones / command / count）。"""
    names, ids, phones = await fetch_doctors(client, ctx.settings, space_id)
    return build_envelope(names, ids, phones)


# -------------------------------------------
# 指令交付（与 domains/doctors.py、场景指令同一通道）
# -------------------------------------------


async def deliver_recommendation(
    ctx: RunContext, answer: str, envelope: dict[str, Any]
) -> str:
    """把 answer 开头的 [DoctorRecommend:] 拆出，作为 action_ready 独立下发。

    返回剥离指令后的 [Answer:] 文本。非流式响应由 api/routes.py 把事件里的
    command_code 拼回 answer 前面，流式响应由前端消费独立 message 帧——两条
    通路都与 domains/doctors.py 一致，故文本里不再保留指令。
    """
    match = re.match(r"^(\[DoctorRecommend:\][^\s\[]+)(.*)$", answer, re.S)
    if not match:
        return answer
    command, rest = match.group(1), match.group(2)
    action_id = "action_" + fingerprint([ctx.record.run_id, command])[:24]
    if action_id in ctx.record.action_delivery:
        # 同一 run 已交付过（重复挂号/指名重选），不重复发事件。
        return rest.lstrip()
    evidence = await ctx.repository.save_evidence(
        ctx.scope.scope_hash,
        "search_doctors",
        str(ctx.record.request_id),
        envelope,
        "numeric-envelope-v1",
    )
    await ctx.emitter.emit(
        "action_ready",
        {
            "action_id": action_id,
            "command_code": command,
            "profile": "native",
            "delivery_status": "ready",
            "source_evidence_id": evidence.evidence_id,
            "source_space_id": ctx.scope.space_id or "",
            "scene_version": getattr(ctx.scope, "scene_version", 0) or 0,
            "command_order": 1,
        },
        task_id=f"consultation_booking_{ctx.record.run_id}",
    )
    logger.info(
        "ai_consultation doctor recommendation delivered",
        extra={
            "run_id": ctx.record.run_id,
            "event_type": "consultation_doctor_recommendation",
            "fields": {"action_id": action_id},
        },
    )
    return rest.lstrip()


async def run_booking(ctx: RunContext, query: str, *, named: bool) -> str:
    """挂号分支完整链路，返回下发给前端的文本（[DoctorRecommend:] + [Answer:]）。"""
    space = (
        getattr(ctx.scope, "space_id", "")
        or ctx.settings.doctor_default_space_id
        or ctx.settings.scene_default_space_id
    )
    name = extract_doctor_name(query) if named else ""
    try:
        async with httpx.AsyncClient(trust_env=False, timeout=20) as client:
            envelope = await fetch_envelope(ctx, client, space)
            data = envelope["data"]
    except Exception as exc:  # 上游不可用按降级口径，不阻断主流程
        logger.warning(
            "ai_consultation doctor list unavailable",
            extra={
                "run_id": ctx.record.run_id,
                "event_type": "consultation_doctors_unavailable",
                "fields": {"error": type(exc).__name__},
            },
        )
        return "[Answer:]医生名单服务暂时不可用，请稍后再试，或描述您的症状进行AI问诊。"

    names = [n for n in str(data.get("doctorNames") or "").split("、") if n]
    phones = [p for p in str(data.get("doctorPhones") or "").split(",") if p]
    if named and name:
        status, answer = named_booking_answer(name, names, phones)
    else:
        status = str(data.get("result") or "")
        answer = plain_booking_answer(data)
    ctx.record.metrics["consultation_booking_status"] = status
    return await deliver_recommendation(ctx, answer, envelope)
