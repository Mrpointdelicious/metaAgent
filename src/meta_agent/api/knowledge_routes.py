"""
创建日期：2026-10-18
文件功能：提供无需回答模型的轻量检索和受认证的原文下载接口。
"""

from typing import Literal

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field

from meta_agent.api.security import require_service_identity
from meta_agent.domains.maxkb import knowledge_domain

router = APIRouter(prefix="/v1/knowledge", dependencies=[Depends(require_service_identity)])


class KnowledgeQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: str = Field(min_length=1, max_length=4000)
    tenant_id: str = Field(min_length=1, max_length=100)
    space_id: str = Field(min_length=1, max_length=160)
    domain: Literal["help", "health", "product"] | None = None
    knowledge_types: list[str] | None = Field(default=None, min_length=1, max_length=7)
    top_k: int | None = Field(default=None, ge=1, le=6)


def workflow_for(request):
    workflow = request.app.state.container.maxkb_workflow
    if workflow is None:
        raise HTTPException(503, "maxkb_disabled")
    return workflow


@router.post("/retrieve")
async def retrieve(payload: KnowledgeQuery, request: Request):
    if not payload.query.strip():
        raise HTTPException(422, "maxkb_query_required")
    workflow = workflow_for(request)
    result = await workflow.retrieve(
        {
            "query": payload.query,
            "tenant_id": payload.tenant_id,
            "space_id": payload.space_id,
            "role": "operator",
        },
        domain=payload.domain or knowledge_domain(payload.query),
        types=payload.knowledge_types,
        top_k=payload.top_k,
    )
    return result.model_dump(mode="json", exclude={"signature"})


@router.get("/sources/{code}")
async def source(code: str, space_id: str, tenant_id: str, request: Request):
    workflow = workflow_for(request)
    try:
        path, name = await workflow.retriever.source_file(
            code,
            {"space_id": space_id, "tenant_id": tenant_id, "role": "operator"},
        )
    except (TimeoutError, httpx.TimeoutException):
        raise HTTPException(504, "maxkb_source_timeout") from None
    except (httpx.RequestError, OSError, ValueError, KeyError, RuntimeError):
        raise HTTPException(503, "maxkb_source_unavailable") from None
    return FileResponse(path, filename=name)
