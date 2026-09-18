"""
创建日期：2026-09-18
文件功能：IReGo 固定领域工作流。Planner 只提交高层业务请求（operation/selector/need_artifact），
数据获取、session_ref 传递、Evidence/Fact 投影与制品生成全部由代码固定，模型无权编排内部步骤。
"""

from typing import Any

from pydantic import ValidationError

from meta_agent.application.context import RunContext
from meta_agent.contracts import (
    DomainError,
    IReGoRequest,
    TaskResult,
    TaskSpec,
    fingerprint,
    utcnow,
)
from meta_agent.domains.facts import aware_date
from meta_agent.domains.rehab import RehabAdapter

ANALYSIS_ARGS = {"detail_level": "standard", "quality_detail": "summary"}
REPORT_STATUS = {"success", "available", "partial"}


class IReGoWorkflow:
    """上层 Scheduler 只看到一个 irego.execute 任务；内部为固定执行链。"""

    def __init__(self, adapter: RehabAdapter) -> None:
        self.adapter = adapter

    async def execute(self, task: TaskSpec, args: dict[str, Any], ctx: RunContext) -> TaskResult:
        try:
            request = IReGoRequest.model_validate(args)
        except ValidationError as exc:
            raise DomainError("invalid_arguments", "iReGo 业务请求不符合契约。") from exc
        if request.operation == "overview":
            return await self._overview(task, request, ctx)
        if request.operation == "history":
            return await self._history(task, request, ctx)
        if request.operation == "session":
            return await self._session(task, request, ctx)
        return await self._trend(task, request, ctx)

    async def _read(self, ctx: RunContext, endpoint: str, payload: dict[str, Any]):
        """读步骤：传输错误重试一次。报表端点不经过该路径（制品不重试）。"""
        try:
            return await self.adapter.fetch(ctx, endpoint, payload)
        except DomainError as exc:
            if not exc.retryable:
                raise
            return await self.adapter.fetch(ctx, endpoint, payload)

    async def _resolve(self, task: TaskSpec, selector, ctx: RunContext) -> TaskResult:
        try:
            return await self.adapter.resolve(task, selector, ctx)
        except DomainError as exc:
            if not exc.retryable:
                raise
            return await self.adapter.resolve(task, selector, ctx)

    async def _overview(self, task: TaskSpec, request: IReGoRequest, ctx: RunContext) -> TaskResult:
        endpoint = "get_multisource_patient_context"
        payload = {"projection_level": "compact", "force_refresh": request.force_refresh}
        cache_key = fingerprint([endpoint, {**payload, "force_refresh": False}])
        if not request.force_refresh:
            cached = await ctx.repository.get("cache", ctx.scope.scope_hash, cache_key)
            if cached:
                evidence = await ctx.repository.evidence(
                    ctx.scope.scope_hash, cached["evidence_id"]
                )
                if evidence:
                    result = self.adapter.project(task, evidence)
                    result.outputs["cache_hit"] = True
                    return result
        evidence = await self._read(ctx, endpoint, payload)
        result = self.adapter.project(task, evidence)
        if result.status == "succeeded":
            await ctx.repository.put(
                "cache",
                ctx.scope.scope_hash,
                cache_key,
                {"evidence_id": evidence.evidence_id},
                ctx.settings.cache_ttl_seconds,
            )
        return result

    async def _history(self, task: TaskSpec, request: IReGoRequest, ctx: RunContext) -> TaskResult:
        page = request.selector.count if request.selector.mode == "ordinal" else 1
        evidence = await self._read(ctx, "get_irego_patient_history", {"page_number": page or 1})
        return self.adapter.project(task, evidence)

    async def _session(self, task: TaskSpec, request: IReGoRequest, ctx: RunContext) -> TaskResult:
        selector = request.selector.model_copy(deep=True)
        if selector.mode == "none":
            selector.mode = "latest_record"
        anchor = ctx.memory.current_record
        if selector.mode == "current_ref":
            if not anchor:
                raise DomainError("anchor_missing", "请明确当前训练记录。", outcome="clarification")
            evidence = await ctx.repository.evidence(ctx.scope.scope_hash, anchor.evidence_id)
            if (
                evidence is not None
                and evidence.source_version == anchor.source_version
                and evidence.tool_name == "get_irego_session_analysis"
            ):
                # 新鲜锚点：复用已持久化分析证据建立本轮事实，不重复调用分析接口。
                result = self.adapter.project(task, evidence)
                result.outputs["session_ref"] = anchor.session_ref
                result.outputs["session_time"] = anchor.session_time
                return await self._finish(task, request, result, ctx)
            # 锚点过期或证据缺失：以同一 session_ref 重新分析，resolve 内部校验一致性。
        if selector.mode in {"current_ref", "latest_usable"}:
            result = await self._resolve(task, selector, ctx)
            return await self._finish(task, request, result, ctx)
        # latest_record / previous_record：经历史发现定位稳定记录。
        result = await self._resolve(task, selector, ctx)
        ref = result.outputs.get("session_ref")
        if result.status not in {"succeeded", "partial"} or not isinstance(ref, str) or not ref:
            return result
        evidence = await self._read(
            ctx,
            "get_irego_session_analysis",
            {"selector": "session_ref", "session_ref": ref, **ANALYSIS_ARGS},
        )
        result = self.adapter.project(task, evidence)
        if result.status in {"succeeded", "partial"} and result.outputs.get("session_ref") != ref:
            raise DomainError("record_mismatch", "工具返回了不同记录，本次结果不用于回答。")
        return await self._finish(task, request, result, ctx)

    async def _trend(self, task: TaskSpec, request: IReGoRequest, ctx: RunContext) -> TaskResult:
        selector = request.selector
        if selector.mode not in {"none", "latest_count", "date_range"}:
            raise DomainError(
                "unsupported_window",
                "当前趋势接口仅支持连续窗口，请明确范围。",
                outcome="clarification",
            )
        args: dict[str, Any] = {"report_count": selector.count or 4}
        if selector.mode == "date_range":
            if not selector.start or not selector.end:
                raise DomainError(
                    "incomplete_window",
                    "请补充完整开始和结束日期。",
                    outcome="clarification",
                )
            args.update(
                selection_mode="date_range",
                start_date=selector.start,
                end_date=selector.end,
            )
        evidence = await self._read(
            ctx,
            "get_irego_longitudinal_analysis",
            {**args, "selector": "latest_contiguous", "project_scope": "all", **ANALYSIS_ARGS},
        )
        result = self.adapter.project(task, evidence)
        return await self._finish(
            task,
            request,
            result,
            ctx,
            endpoint="generate_irego_longitudinal_report",
            report_payload=args,
        )

    async def _finish(
        self,
        task: TaskSpec,
        request: IReGoRequest,
        result: TaskResult,
        ctx: RunContext,
        *,
        endpoint: str = "generate_irego_single_session_report",
        report_payload: dict[str, Any] | None = None,
    ) -> TaskResult:
        # 渐进回答：基础数据与事实先于报告制品可达。
        if ctx.on_result and result.status in {"succeeded", "partial"}:
            ctx.record.results[task.task_id] = result
            await ctx.on_result(task, result, ctx)
        if not request.need_artifact or result.status not in {"succeeded", "partial"}:
            return result
        if report_payload is None:
            ref = result.outputs.get("session_ref")
            if not isinstance(ref, str) or not ref:
                return result
            report_payload = {"session_ref": ref}
        try:
            report_evidence = await self.adapter.fetch(ctx, endpoint, report_payload)
            artifact = self._artifact(report_evidence)
            if (
                artifact["expires_at"] is not None
                and aware_date(artifact["expires_at"]) <= utcnow()
            ) or not await ctx.backend.artifact_available(artifact["url"]):
                raise DomainError(
                    "artifact_unavailable", "报告暂不可访问或已过期。", outcome="unavailable"
                )
        except DomainError as exc:
            # 报告失败不得抹掉已经建立的基础事实。
            if result.status == "succeeded":
                result.status = "partial"
            result.code = exc.code
            result.message = exc.message
            result.retryable = False
            return result
        result.outputs["artifact"] = artifact
        result.evidence_ids.append(report_evidence.evidence_id)
        result.message = "报告已生成。"
        return result

    @staticmethod
    def _artifact(evidence) -> dict[str, Any]:
        body = evidence.payload
        if body.get("status") not in REPORT_STATUS:
            raise DomainError(f"tool_{body.get('status')}", "工具无法提供该结果。")
        data = body.get("data")
        if not isinstance(data, dict):
            raise DomainError("invalid_artifact", "报表结果缺少制品引用或地址。")
        ref, url = data.get("artifact_ref"), data.get("image_url")
        if not isinstance(ref, str) or not ref or not isinstance(url, str) or not url:
            raise DomainError("invalid_artifact", "报表结果缺少制品引用或地址。")
        expiry = aware_date(data.get("expires_at"))
        return {
            "artifact_ref": ref,
            "url": url,
            "expires_at": expiry.isoformat() if expiry else None,
            "evidence_id": evidence.evidence_id,
        }
