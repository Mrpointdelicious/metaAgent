"""
创建日期：2026-10-18
文件功能：保留已禁用的旧 MaxKB 接口，鉴权后明确返回禁用状态。
"""

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from meta_agent.api.security import require_service_identity

router = APIRouter(prefix="/v1/knowledge", dependencies=[Depends(require_service_identity)])


class KnowledgeQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: str = Field(min_length=1, max_length=4000)
    tenant_id: str = Field(min_length=1, max_length=100)
    space_id: str = Field(min_length=1, max_length=160)
    domain: Literal["help", "health", "product"] | None = None
    knowledge_types: list[str] | None = Field(default=None, min_length=1, max_length=7)
    top_k: int | None = Field(default=None, ge=1, le=6)


@router.post("/retrieve")
async def retrieve(payload: KnowledgeQuery):
    raise HTTPException(503, "maxkb_disabled")


@router.get("/sources/{code}")
async def source(code: str, space_id: str, tenant_id: str):
    raise HTTPException(503, "maxkb_disabled")
