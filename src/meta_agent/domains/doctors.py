"""
创建日期：2026-09-08
文件功能：查询当前空间在线医生，仅展示可确认的名单，不自动联系或播报电话。

挂号指令（[DoctorRecommend:]手机号）的下发口径（2026-11-17）：
  仅当「本轮明确表达挂号意图」且「在线医生恰好 1 位」且「该医生有手机号」
  且「请求人不是医生身份」时，作为独立 action_ready 事件下发；
  只问名单、多医生、无手机号、医生身份一律不发（避免一问名单就自动呼出）。

名单来源（2026-11-17 确认）：本域是「房间外挂号」，走 GET /api/users（在线接诊医生）；
房间内邀请医生走 GET /api/users/all（在房间中的医生），见 consultation_room 子流程。
"""

import re
from typing import Any

from meta_agent.application.context import RunContext
from meta_agent.contracts import DomainError, TaskResult, TaskSpec, fingerprint
from meta_agent.domains.facts import FactBuilder
from meta_agent.domains.scene import numeric_envelope

# 明确挂号意图：挂号 / 挂（个|一个|…）号 / 挂某某的号 / 预约 / 就他 / 选第 N 个。
_BOOKING = re.compile(
    r"挂号|挂(?:个|一个|一下|上)?号|挂[^，。；！？,.!?]{1,10}?的?号"
    r"|预约(?:挂号|医生)?|约(?:个|一个)?号"
    r"|就(?:他|她|这位|这个|该医生)|选(?:他|她|这位|这个|第)|第[一二三四五六七八九十0-9]+(?:个|位)"
)
# 否定/撤销表达不算挂号意图（不要挂号、别预约、取消挂号）。
_BOOKING_NEGATED = re.compile(r"(?:不|别|无需|不用|不要|取消)(?:想|要|用|再)?(?:挂|预约)")


def booking_intent(query: str) -> bool:
    """本轮是否明确表达「要挂号」；否定表达（不要挂号/取消）返回 False。"""
    text = (query or "").strip()
    if not text or _BOOKING_NEGATED.search(text):
        return False
    return bool(_BOOKING.search(text))


def recommendation_command(ctx: RunContext, data: dict[str, Any]) -> str:
    """按挂号口径决定是否下发 [DoctorRecommend:]手机号；不满足时返回空串。"""
    if getattr(ctx.scope, "role", "") == "clinician":
        return ""
    if not booking_intent(ctx.query):
        return ""
    if data.get("count") != 1:
        return ""
    phones = [p.strip() for p in str(data.get("doctorPhones") or "").split(",") if p.strip()]
    if len(phones) != 1:
        return ""
    return f"[DoctorRecommend:]{phones[0]}"


class DoctorAdapter:
    async def select(self, task, args, ctx):
        selection = args["selection"].strip()
        if selection not in ctx.query:
            raise DomainError(
                "doctor_request_mismatch", "请使用本轮明确选择。", outcome="clarification"
            )
        key = fingerprint(["doctors", ctx.scope.space_id])
        cached = await ctx.repository.get("cache", ctx.scope.scope_hash, key)
        evidence = (
            await ctx.repository.evidence(ctx.scope.scope_hash, cached["evidence_id"])
            if cached
            else None
        )
        if evidence is None:
            raise DomainError(
                "doctor_list_required", "请先查询当前空间在线医生。", outcome="clarification"
            )
        data = numeric_envelope(evidence.payload)
        names = [name.strip() for name in data["doctorNames"].split("、") if name.strip()]
        if len(names) != data["count"]:
            raise DomainError("invalid_doctor_result", "医生名单无法逐项确认。")
        ordinal = re.fullmatch(r"(?:第|选择|选)?([一二三四五六七八九十]|\d+)(?:个|位)?", selection)
        if ordinal:
            value = ordinal[1]
            index = int(value) if value.isdigit() else "一二三四五六七八九十".index(value) + 1
            selected = [index - 1] if 1 <= index <= len(names) else []
        else:
            selected = [i for i, name in enumerate(names) if name == selection]
        if len(selected) != 1:
            return TaskResult(
                task_id=task.task_id,
                status="clarification",
                code="doctor_ambiguous",
                message="请从当前名单明确选择一位医生。",
            )
        return TaskResult(
            task_id=task.task_id,
            evidence_ids=[evidence.evidence_id],
            outputs={
                "doctor_name": names[selected[0]],
                "selected_index": selected[0] + 1,
                "registration_status": "not_requested",
            },
        )

    async def execute(self, task: TaskSpec, args: dict[str, Any], ctx: RunContext) -> TaskResult:
        del args
        key = fingerprint(["doctors", ctx.scope.space_id])
        cached = await ctx.repository.get("cache", ctx.scope.scope_hash, key)
        evidence = (
            await ctx.repository.evidence(ctx.scope.scope_hash, cached["evidence_id"])
            if cached
            else None
        )
        if evidence is None:
            # 房间外挂号只认 spaceId（/api/users 严格按空间过滤），不传 inviterPhone：
            # 那是 /api/users/all 房间内在场口径的参数。
            body = await ctx.call("search_doctors", {"spaceId": ctx.scope.space_id})
            numeric_envelope(body)
            evidence = await ctx.repository.save_evidence(
                ctx.scope.scope_hash,
                "search_doctors",
                ctx.record.request_id,
                body,
                "numeric-envelope-v1",
            )
            await ctx.repository.put(
                "cache",
                ctx.scope.scope_hash,
                key,
                {"evidence_id": evidence.evidence_id},
                ctx.settings.doctor_ttl_seconds,
            )
        data = numeric_envelope(evidence.payload)
        count = data.get("count")
        if type(count) is not int or count < 0 or not isinstance(data.get("doctorNames"), str):
            raise DomainError("invalid_doctor_result", "医生名单返回格式无效。")
        builder = FactBuilder(evidence)
        builder.add("/data/count", "doctor_count", "当前在线医生数", required=True)
        if count > 0:
            builder.add("/data/doctorNames", "doctor_names", "在线医生", required=True)
        command = recommendation_command(ctx, data)
        action_id = ""
        if command:
            # 与场景指令同一通道：独立 message 先于 [Answer:] 下发，前端据此弹挂号卡。
            action_id = "action_" + fingerprint([ctx.record.run_id, command])[:24]
            if action_id not in ctx.record.action_delivery:
                await ctx.emitter.emit(
                    "action_ready",
                    {
                        "action_id": action_id,
                        "command_code": command,
                        "profile": "native",
                        "delivery_status": "ready",
                        "source_evidence_id": evidence.evidence_id,
                        "source_space_id": ctx.scope.space_id or "",
                        # 医师推荐不属于场景点位，沿用当前场景版本（无场景时为 0）。
                        "scene_version": ctx.scope.scene_version or 0,
                        "command_order": 1,
                    },
                    task_id=task.task_id,
                    goal_id=task.goal_ids[0],
                )
        outputs: dict[str, Any] = {
            "has_results": count > 0,
            "no_results": count == 0,
            "cache_hit": evidence is not None and cached is not None,
        }
        if command:
            outputs.update(command=command, action_id=action_id)
        return TaskResult(
            task_id=task.task_id,
            evidence_ids=[evidence.evidence_id],
            facts=builder.views,
            message="当前空间没有在线医生。" if count == 0 else "",
            outputs=outputs,
        )
