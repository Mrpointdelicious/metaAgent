"""
创建日期：2026-09-08
文件功能：查询当前空间在线医生，仅展示可确认的名单，不自动联系或播报电话。
"""

from typing import Any

from meta_agent.application.context import RunContext
from meta_agent.contracts import DomainError, TaskResult, TaskSpec, fingerprint
from meta_agent.domains.facts import FactBuilder
from meta_agent.domains.scene import numeric_envelope


class DoctorAdapter:
    async def execute(self, task: TaskSpec, args: dict[str, Any], ctx: RunContext) -> TaskResult:
        del args
        key = fingerprint(["doctors", ctx.scope.space_id])
        cached = await ctx.repository.get("cache", ctx.scope.scope_hash, key)
        evidence = await ctx.repository.evidence(ctx.scope.scope_hash, cached["evidence_id"]) if cached else None
        if evidence is None:
            body = await ctx.call("search_doctors", {"spaceId": ctx.scope.space_id})
            numeric_envelope(body)
            evidence = await ctx.repository.save_evidence(ctx.scope.scope_hash, "search_doctors",
                ctx.record.request_id, body, "numeric-envelope-v1")
            await ctx.repository.put("cache", ctx.scope.scope_hash, key,
                {"evidence_id": evidence.evidence_id}, ctx.settings.doctor_ttl_seconds)
        data = numeric_envelope(evidence.payload)
        count = data.get("count")
        if type(count) is not int or count < 0 or not isinstance(data.get("doctorNames"), str):
            raise DomainError("invalid_doctor_result", "医生名单返回格式无效。")
        builder = FactBuilder(evidence)
        builder.add("/data/count", "doctor_count", "当前在线医生数", required=True)
        if count > 0:
            builder.add("/data/doctorNames", "doctor_names", "在线医生", required=True)
        return TaskResult(task_id=task.task_id, evidence_ids=[evidence.evidence_id], facts=builder.views,
            message="当前空间没有在线医生。" if count == 0 else "",
            outputs={"has_results": count > 0, "no_results": count == 0, "cache_hit": evidence is not None and cached is not None})
