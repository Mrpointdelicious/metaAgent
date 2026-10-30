"""
创建日期：2026-10-30
文件功能：适配分支业务能力并保留可信范围边界。
Business-login boundary. Install a verified provider on app.state.

The provider must verify business sessions with their issuer and enforce tenant,
role and space grants. Request inputs and identity headers are never proof.
Patient v2 authorization stays mandatory until the issuer supports a replacement.
"""

from dataclasses import replace
from typing import Protocol

from fastapi import HTTPException, Request

from meta_agent.api.security import ClientIdentity
from meta_agent.contracts import DomainError
from meta_agent.orchestration.identity import TrustedScope


class BusinessIdentityProvider(Protocol):
    async def authenticate(self, request: Request, client: ClientIdentity) -> TrustedScope: ...


async def business_scope(request: Request) -> TrustedScope:
    provider = getattr(request.app.state, "business_identity_provider", None)
    if provider is None:
        raise HTTPException(503, {"code": "business_identity_unavailable"})
    client = getattr(request.state, "client", ClientIdentity())
    scope = await provider.authenticate(request, client)
    if not isinstance(scope, TrustedScope) or not scope.end_user_id or not scope.tenant_id:
        raise HTTPException(403, {"code": "business_identity_invalid"})
    scope = replace(
        scope,
        client_name=client.name,
        excluded_tools=scope.excluded_tools | client.excluded_tools,
    )
    if scope.patient_identity is not None:
        try:
            backend = request.app.state.container.ai_webapi_client
            scope = await backend.resolve_patient_identity(scope)
        except DomainError as exc:
            raise HTTPException(403, {"code": exc.code, "message": exc.message}) from exc
    return scope
