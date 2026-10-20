"""
创建日期：2026-10-02
文件功能：适配 IReTour 独立契约，执行固定查询和报表链并保留来源及跨轮记录边界。
"""

from datetime import datetime, timedelta

from meta_agent.contracts import DomainError, IReTourRequest, TaskResult, utcnow
from meta_agent.domains.facts import FactBuilder, aware_date
from meta_agent.domains.irego import ANALYSIS_ARGS, IReGoWorkflow
from meta_agent.domains.rehab import RehabAdapter, list_field, object_field


class IReTourAdapter(RehabAdapter):
    def project(self, task, evidence):
        body = evidence.payload
        status = {"success": "succeeded", "available": "succeeded"}.get(
            body["status"], body["status"]
        )
        result = TaskResult(
            task_id=task.task_id,
            status=status,
            evidence_ids=[evidence.evidence_id],
            outputs={"record_domain": "iretour", "source_version": evidence.source_version},
        )
        builder = FactBuilder(evidence)
        if body.get("patient_message"):
            builder.add("/patient_message", "patient_message", "说明", required=True)
        if status in {"failed", "unavailable"}:
            result.code = "tool_" + status
            result.message = "IReTour 暂无符合条件的可用数据。"
            result.facts = builder.views
            return result
        data = object_field(body, "data")
        endpoint = evidence.tool_name
        if endpoint == "get_iretour_patient_context":
            domain = object_field(data, "i_re_tour")
            builder.add(
                "/data/i_re_tour/availability", "availability", "IReTour数据可用性", required=True
            )
            for key, label in {
                "session_count": "训练次数",
                "summary_available_count": "有汇总结果次数",
                "usable_session_count": "可分析次数",
                "quality_blocked_count": "质量受限次数",
            }.items():
                if key in object_field(domain, "counts"):
                    builder.add(f"/data/i_re_tour/counts/{key}", key, label)
            builder.quality("/data/i_re_tour/quality")
        elif endpoint == "get_iretour_patient_history":
            page = object_field(data, "page")
            result.outputs["history_page"] = page.get("page_number", 1)
            items = list_field(data, "items")
            for i, item in enumerate(items):
                builder.record_ref = item.get("session_ref")
                for key, label in {
                    "session_time": "时间",
                    "training_state": "训练状态",
                    "result_state": "结果状态",
                    "patient_message": "说明",
                }.items():
                    if key in item:
                        builder.add(
                            f"/data/items/{i}/{key}", key, f"第{i + 1}条{label}", required=True
                        )
                builder.add(
                    f"/data/items/{i}/activity/display_name", "activity", f"第{i + 1}条项目"
                )
            if not items:
                result.message = "当前页没有 IReTour 训练记录。"
        elif endpoint == "get_iretour_session_analysis":
            ref = object_field(data, "session").get("session_ref")
            if not isinstance(ref, str) or not ref:
                raise DomainError("invalid_contract", "IReTour 结果缺少记录引用。")
            times = object_field(data, "time")
            builder.record_ref = ref
            time_key = next(
                (
                    k
                    for k in (
                        "execution_ended_at",
                        "execution_started_at",
                        "source_record_created_at",
                    )
                    if times.get(k)
                ),
                "execution_started_at",
            )
            builder.observed_at = aware_date(times.get(time_key))
            builder.add(f"/data/time/{time_key}", "session_time", "训练时间", required=True)
            builder.add("/data/time/training_state", "training_state", "训练状态", required=True)
            builder.add("/data/activity/display_name", "activity", "训练项目", required=True)
            builder.add("/data/plan/duration_seconds", "planned_duration", "计划时长", unit="s")
            outcome = object_field(data, "result")
            builder.add("/data/result/result_state", "result_state", "结果状态", required=True)
            for i, metric in enumerate(list_field(outcome, "metrics")):
                key = (
                    "normalized_value"
                    if metric.get("normalized_value") is not None
                    else "raw_value"
                )
                builder.add(
                    f"/data/result/metrics/{i}/{key}",
                    metric.get("metric_code") or "metric",
                    metric.get("display_name") or "指标",
                    unit=metric.get("unit") or "unknown",
                    status=metric.get("value_status", "unknown"),
                    uses=["display", "single_session"],
                )
            builder.quality()
            result.outputs.update(
                session_ref=ref,
                session_time=times.get(time_key),
                analysis_evidence=evidence.evidence_id,
            )
        elif endpoint == "get_iretour_longitudinal_analysis":
            window = object_field(data, "window")
            for key, label in {
                "started_at": "窗口开始",
                "ended_at": "窗口结束",
                "requested_count": "请求次数",
                "resolved_count": "实际次数",
                "comparable_count": "可比较次数",
                "is_contiguous": "连续窗口",
            }.items():
                builder.add(f"/data/window/{key}", key, label, required=True)
            for i, series in enumerate(list_field(data, "metric_series")):
                prefix = f"/data/metric_series/{i}"
                label = series.get("display_name") or "指标"
                # 只投影数值变化，IReTour 不套用 IReGo 临床疗效解释。
                for key, suffix in {
                    "direction": "数值方向",
                    "interpretation_status": "解释状态",
                }.items():
                    builder.add(f"{prefix}/{key}", key, f"{label}：{suffix}", required=True)
                for j, point in enumerate(series.get("points") or []):
                    builder.record_ref = point.get("session_ref")
                    builder.observed_at = aware_date(point.get("session_time"))
                    builder.add(
                        f"{prefix}/points/{j}/value",
                        series.get("metric_code") or "metric",
                        f"第{point.get('ordinal', j + 1)}次{label}",
                        unit=series.get("unit") or "unknown",
                        status=point.get("value_status", "unknown"),
                        uses=["display", "trend"],
                    )
            if isinstance(data.get("quality"), dict):
                builder.quality()
            result.outputs["window"] = window
        result.facts = builder.views
        return result


class IReTourWorkflow(IReGoWorkflow):
    def __init__(self):
        super().__init__(IReTourAdapter())

    async def execute(self, task, args, ctx):
        request = IReTourRequest.model_validate(args)
        if request.operation == "overview":
            evidence = await self._read(
                ctx, "get_iretour_patient_context", {"projection_level": "compact"}
            )
            return self.adapter.project(task, evidence)
        if request.operation == "history":
            page = request.selector.count if request.selector.mode == "ordinal" else 1
            evidence = await self._read(
                ctx, "get_iretour_patient_history", {"page_number": page or 1}
            )
            return self.adapter.project(task, evidence)
        if request.operation == "trend":
            return await self._trend(task, request, ctx)
        return await self._session(task, request, ctx)

    async def _session(self, task, request, ctx):
        mode = request.selector.mode
        anchor = ctx.memory.current_record
        if mode in {"current_ref", "previous_record"} and (
            not anchor or anchor.domain != "iretour"
        ):
            raise DomainError(
                "anchor_missing", "请先定位 IReTour 训练记录。", outcome="clarification"
            )
        if mode == "current_ref":
            evidence = await ctx.repository.evidence(ctx.scope.scope_hash, anchor.evidence_id)
            if (
                evidence
                and evidence.tool_name == "get_iretour_session_analysis"
                and evidence.source_version == anchor.source_version
            ):
                return await self._finish_tour(
                    task, request, self.adapter.project(task, evidence), ctx
                )
            ref = anchor.session_ref
        elif mode == "latest_usable":
            ref = None
        elif mode in {"none", "latest_record", "previous_record", "ordinal"}:
            candidates = []
            ref = None
            for page in range(1, 4):
                evidence = await self._read(
                    ctx, "get_iretour_patient_history", {"page_number": page, "page_size": 50}
                )
                if evidence.payload["status"] not in {"success", "available", "partial"}:
                    return self.adapter.project(task, evidence)
                data = object_field(evidence.payload, "data")
                candidates.extend(
                    item
                    for item in list_field(data, "items")
                    if not ((when := aware_date(item.get("session_time"))) and when > utcnow())
                )
                index = (request.selector.count or 1) - 1 if mode == "ordinal" else 0
                if mode == "previous_record":
                    position = next(
                        (
                            i
                            for i, item in enumerate(candidates)
                            if item.get("session_ref") == anchor.session_ref
                        ),
                        None,
                    )
                    index = position + 1 if position is not None else len(candidates)
                if index < len(candidates):
                    ref = candidates[index].get("session_ref")
                    break
                if not object_field(data, "page").get("has_next"):
                    break
            if not ref:
                raise DomainError(
                    "record_not_found", "未找到可分析的 IReTour 记录。", outcome="unavailable"
                )
        else:
            raise DomainError(
                "unsupported_selector", "请明确单次训练记录。", outcome="clarification"
            )
        evidence = await self._read(
            ctx,
            "get_iretour_session_analysis",
            {
                "selector": "session_ref" if ref else "latest_usable",
                **ANALYSIS_ARGS,
                **({"session_ref": ref} if ref else {}),
            },
        )
        result = self.adapter.project(task, evidence)
        if (
            ref
            and result.status in {"succeeded", "partial"}
            and result.outputs.get("session_ref") != ref
        ):
            raise DomainError("record_mismatch", "IReTour 返回了不同的训练记录。")
        return await self._finish_tour(task, request, result, ctx)

    async def _finish_tour(self, task, request, result, ctx):
        return await self._finish(
            task, request, result, ctx, endpoint="generate_iretour_single_session_report"
        )

    async def _trend(self, task, request, ctx):
        selector = request.selector
        args = {"activity_scope": request.activity_scope}
        if selector.mode in {"none", "latest_count"}:
            count = selector.count or 4
            minimum = 3 if request.need_artifact else 2
            if not minimum <= count <= 20:
                raise DomainError(
                    "invalid_window",
                    f"IReTour 本次请求需要{minimum}到20次训练。",
                    outcome="clarification",
                )
            args.update(selection_mode="latest_count", report_count=count)
        elif selector.mode == "recent_ordinal_range":
            start, end = selector.start_ordinal, selector.end_ordinal
            minimum = 3 if request.need_artifact else 2
            if start is None or end is None or not minimum <= abs(end - start) + 1 <= 20:
                raise DomainError(
                    "invalid_window", "请提供有效的连续训练序号范围。", outcome="clarification"
                )
            args.update(selection_mode="recent_ordinal_range", start_ordinal=start, end_ordinal=end)
        elif selector.mode == "date_range":
            try:
                start, end = (
                    datetime.fromisoformat(selector.start),
                    datetime.fromisoformat(selector.end),
                )
                valid = start.tzinfo is not None and end.tzinfo is not None and start <= end
                if not valid or (end - start) > timedelta(days=180):
                    raise ValueError
            except (ValueError, TypeError) as exc:
                raise DomainError(
                    "invalid_window",
                    "请提供含时区且不超过180天的有效日期范围。",
                    outcome="clarification",
                ) from exc
            args.update(
                selection_mode="date_range", start_date=selector.start, end_date=selector.end
            )
        else:
            raise DomainError(
                "invalid_window", "请明确连续训练次数或日期范围。", outcome="clarification"
            )
        evidence = await self._read(
            ctx, "get_iretour_longitudinal_analysis", {**args, **ANALYSIS_ARGS}
        )
        return await self._finish(
            task,
            request,
            self.adapter.project(task, evidence),
            ctx,
            endpoint="generate_iretour_longitudinal_report",
            report_payload=args,
        )
