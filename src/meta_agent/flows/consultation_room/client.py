"""
创建日期：2026-11-03
文件功能：在原有问诊取数流程中共享 HTTP 调用预算、并发限制和取消。
"""

import asyncio

from meta_agent.contracts import DomainError
from meta_agent.orchestration.capabilities import CLINICAL_TASKS, require_for_run


class ConsultationClient:
    """Keep the current business adapters while sharing admission and cancellation."""

    def __init__(self, ctx, capability=None):
        # ``capability`` is required by the dev authorization chain. The restored
        # ai_consultation history adapter still constructs this client without one;
        # keep that legacy call site working without weakening explicit grants.
        if capability is not None:
            if capability not in CLINICAL_TASKS.values() or capability in {
                "clinical.basic",
                "clinical.end",
            }:
                raise DomainError(
                    "resource_denied", "此能力不允许读取就诊业务数据。", outcome="unsupported"
                )
            require_for_run(ctx, capability)
        self.ctx = ctx
        self.capability = capability

    def encounter_summary(self):
        if self.capability is not None:
            require_for_run(self.ctx, self.capability)
        return self.ctx.scope.resource_grant.encounter_summary.get_secret_value()

    async def _request(self, method, *args, **kwargs):
        ctx = self.ctx
        async with (
            ctx.tool_slot(),
            asyncio.timeout(min(ctx.remaining, ctx.settings.tool_timeout_seconds)),
        ):
            if self.capability is not None:
                require_for_run(ctx, self.capability)
            response = await getattr(ctx.backend.client, method)(*args, **kwargs)
            if self.capability is not None:
                require_for_run(ctx, self.capability)
            if len(response.content) > 2_000_000:
                raise DomainError(
                    "response_too_large", "问诊服务返回内容过大。", outcome="unavailable"
                )
            # Login responses contain tokens and must never enter evidence/history.
            if method == "get" and response.is_success:
                await ctx.repository.save_evidence(
                    ctx.scope.scope_hash,
                    "consultation.read",
                    ctx.record.request_id,
                    {"body": response.text},
                    "consultation-legacy-read-v1",
                )
            return response

    async def get(self, *args, **kwargs):
        return await self._request("get", *args, **kwargs)

    async def post(self, *args, **kwargs):
        return await self._request("post", *args, **kwargs)


async def fetch_details(ctx, client, url, token, ids):
    from .medical_record import _get

    details = {}
    for value in dict.fromkeys(ids):
        response = await _get(
            client,
            url,
            token=token,
            params={"Id": value},
            step="recommendation_detail",
            label="推荐详情查询",
        )
        body = response.json()
        if not isinstance(body, dict) or not isinstance(body.get("data"), dict):
            raise DomainError("invalid_contract", "推荐详情返回格式无效。")
        details[str(value)] = body["data"]
    return details
