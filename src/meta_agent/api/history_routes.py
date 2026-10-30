"""
创建日期：2026-10-30
文件功能：适配分支业务能力并保留可信范围边界。
History requires verified business identity in addition to service authentication."""

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from meta_agent.api.security import require_service_identity
from meta_agent.application.ingress import business_scope

router = APIRouter(dependencies=[Depends(require_service_identity)])


async def history_scope(request, user):
    scope = await business_scope(request)
    if user is not None and user != scope.end_user_id:
        raise HTTPException(403, {"code": "business_subject_mismatch"})
    return scope


@router.get("/v1/conversations")
async def conversations(
    request: Request,
    user: str | None = None,
    limit: int = Query(20, ge=1, le=100),
    last_id: str | None = None,
):
    scope = await history_scope(request, user)
    rows, more = await request.app.state.container.repository.history_conversations(
        scope.scope_hash,
        limit=limit,
        last_id=last_id,
    )
    return {"limit": limit, "has_more": more, "data": rows}


@router.get("/v1/messages")
async def messages(
    request: Request,
    conversation_id: str = Query(min_length=1, max_length=160),
    user: str | None = None,
    limit: int = Query(20, ge=1, le=100),
    first_id: str | None = None,
):
    scope = await history_scope(request, user)
    rows, more = await request.app.state.container.repository.history_messages(
        scope.scope_hash,
        conversation_id,
        limit=limit,
        first_id=first_id,
    )
    return {"limit": limit, "has_more": more, "data": rows}
