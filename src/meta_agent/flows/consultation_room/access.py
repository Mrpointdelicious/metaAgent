"""
创建日期：2026-11-03
文件功能：问诊参与关系的运行期授权边界及共享预算的临床 HTTP 访问。
"""

import asyncio

from pydantic import Field, ValidationError

from meta_agent.contracts import DomainError, StrictModel

CLINICAL_CATEGORIES = frozenset({"病历填写", "邀请医生", "处方配置", "量表测评", "医嘱填写"})


class ConsultationAccess(StrictModel):
    """Only a server-side identity provider may create this runtime-only grant."""

    authorization_scope: str
    consultation_context: str
    patient_ref: str = Field(min_length=1, max_length=256)
    requester_phone: str = Field(pattern=r"^[0-9+]{6,20}$")
    patient_phone: str = Field(pattern=r"^[0-9+]{6,20}$")
    allowed_categories: frozenset[str]


async def read_access(provider, scope, category, timeout):
    try:
        async with asyncio.timeout(timeout):
            return ConsultationAccess.model_validate(await provider.authorize(scope, category))
    except (ValidationError, TimeoutError) as exc:
        raise DomainError(
            "consultation_authorization_unavailable",
            "问诊参与关系暂时无法确认。",
            outcome="unavailable",
        ) from exc


async def authorize(ctx, category, application):
    provider = application.consultation_identity_provider
    if provider is None:
        raise DomainError(
            "consultation_authorization_unavailable",
            "当前尚未接入问诊参与关系校验，暂不能读取患者资料或生成问诊草稿。",
            outcome="unavailable",
        )
    access = await read_access(
        provider, ctx.scope, category, min(ctx.remaining, ctx.settings.tool_timeout_seconds)
    )
    if (
        access.authorization_scope != ctx.scope.scope_hash
        or access.consultation_context != ctx.scope.consultation_context
        or category not in access.allowed_categories
        or access.patient_ref != ctx.scope.consultation_patient_ref
        or (ctx.scope.has_patient and access.patient_ref != ctx.scope.patient_ref)
    ):
        raise DomainError(
            "consultation_forbidden", "当前用户未获本次问诊操作授权。", outcome="unavailable"
        )
    ctx.consultation_access = access


class ConsultationClient:
    """Keep the current business adapters while sharing admission and cancellation."""

    def __init__(self, ctx):
        if ctx.consultation_access is None:
            raise DomainError(
                "consultation_forbidden", "请先确认问诊参与关系。", outcome="unavailable"
            )
        self.ctx = ctx

    async def _request(self, method, *args, **kwargs):
        ctx = self.ctx
        async with (
            ctx.tool_slot(),
            asyncio.timeout(min(ctx.remaining, ctx.settings.tool_timeout_seconds)),
        ):
            response = await getattr(ctx.backend.client, method)(*args, **kwargs)
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
