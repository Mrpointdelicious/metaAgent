"""
创建日期：2026-10-22
文件功能：仅向容器回环地址和可信服务身份提供 Prompt 部署状态与排空控制。
"""

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import StrictBool

from meta_agent.api.security import require_service_identity
from meta_agent.contracts import StrictModel


async def loopback_only(request: Request) -> None:
    if request.client is None or request.client.host not in {"127.0.0.1", "::1"}:
        raise HTTPException(403, "部署控制仅允许容器内回环访问")


router = APIRouter(
    prefix="/_ops/prompt-deployment",
    dependencies=[Depends(loopback_only), Depends(require_service_identity)],
    include_in_schema=False,
)


class DrainUpdate(StrictModel):
    draining: StrictBool


async def status(request: Request) -> dict:
    container = request.app.state.container
    await container.ready()
    binding = container.prompts.bind(container.settings.prompt_bundle)
    return {
        "ready": True,
        "bundle": binding.bundle,
        "bundle_hash": binding.bundle_hash,
        "releases": container.prompts.releases(),
        "active_runs": len(container.application.active),
        "starting_runs": container.application.starting_runs,
        "draining": container.application.draining,
    }


@router.get("")
async def inspect(request: Request):
    return await status(request)


@router.post("")
async def update(payload: DrainUpdate, request: Request):
    # Validate readiness before changing the admission flag.
    await status(request)
    request.app.state.container.application.draining = payload.draining
    return await status(request)
