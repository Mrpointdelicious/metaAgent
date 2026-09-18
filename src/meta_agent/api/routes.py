"""
创建日期：2026-08-29
文件功能：原生阻塞/SSE、作用域限定状态与取消接口；Dify仅保留未启用入口。
"""

import asyncio
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

from meta_agent.api.schemas import HealthResponse, NativeChatRequest, RunAccessRequest
from meta_agent.api.security import require_service_identity
from meta_agent.application.service import ApplicationRequest, ApplicationRun
from meta_agent.contracts import DomainError, RunRecord, fingerprint
from meta_agent.events.stream import NativeEventAdapter
from meta_agent.orchestration.identity import trusted_scope_from_inputs

router = APIRouter()


def scope_for(payload: RunAccessRequest, request: Request):
    try:
        return trusted_scope_from_inputs(
            payload.inputs, payload.user, request.app.state.container.settings.default_tenant_id
        )
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


def snapshot(record: RunRecord, reused: bool = True) -> dict[str, Any]:
    # Snapshots expose previous progress as data, never as replayable action events.
    events = [e.model_dump(mode="json") for e in record.events if e.type != "action_ready"]
    return {
        "run_id": record.run_id,
        "request_id": record.request_id,
        "conversation_id": record.conversation_id,
        "status": record.status,
        "reused": reused,
        "goal_statuses": record.goal_statuses,
        "task_statuses": {tid: r.status for tid, r in record.results.items()},
        "events": events,
        "action_delivery": record.action_delivery,
        "metrics": record.metrics,
    }


@router.post("/v1/chat", dependencies=[Depends(require_service_identity)])
async def chat(payload: NativeChatRequest, request: Request):
    container = request.app.state.container
    if len(payload.query) > container.settings.max_query_length:
        raise HTTPException(422, "query 超过本轮长度限制")
    scope = scope_for(payload, request)
    conversation = (
        payload.conversation_id
        or "conversation_" + fingerprint([scope.scope_hash, payload.request_id])[:24]
    )
    try:
        run = await container.application.start(
            ApplicationRequest(payload.query, scope, conversation, payload.request_id)
        )
    except DomainError as exc:
        raise HTTPException(
            409 if exc.code == "idempotency_conflict" else 422,
            {"code": exc.code, "message": exc.message},
        ) from exc
    if run.reused:
        return JSONResponse(
            snapshot(run.record), status_code=202 if run.record.status == "running" else 200
        )
    if payload.response_mode == "streaming":
        return StreamingResponse(
            stream(run, request),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
                "X-Run-ID": run.record.run_id,
            },
        )
    try:
        await asyncio.shield(run.task)
    except asyncio.CancelledError:
        await container.application.cancel(scope.scope_hash, run.record.run_id)
        raise
    body = snapshot(run.record, False)
    body["events"] = [e.model_dump(mode="json") for e in run.record.events]
    # Blocking delivery has no transport acknowledgement; keep delivery_unknown.
    return JSONResponse(body)


async def stream(run: ApplicationRun, request: Request):
    adapter = NativeEventAdapter()
    exhausted = False
    try:
        while True:
            event = await run.emitter.queue.get()
            if event is None:
                exhausted = True
                break
            yield adapter.encode(event)
            await run.emitter.mark_dispatched(event)
    finally:
        if not exhausted:
            # A generator disconnect cancels the same application run, including child tools.
            async def terminate():
                await request.app.state.container.application.cancel(
                    run.record.scope_key, run.record.run_id
                )
                await run.emitter.disconnect()

            await asyncio.shield(terminate())


@router.post("/v1/runs/{run_id}/status", dependencies=[Depends(require_service_identity)])
async def run_status(run_id: str, payload: RunAccessRequest, request: Request):
    scope = scope_for(payload, request)
    record = await request.app.state.container.repository.run(scope.scope_hash, run_id)
    if record is None:
        raise HTTPException(404, "运行不存在或不属于当前作用域")
    return snapshot(record)


@router.post("/v1/runs/{run_id}/cancel", dependencies=[Depends(require_service_identity)])
async def cancel(run_id: str, payload: RunAccessRequest, request: Request):
    scope = scope_for(payload, request)
    record = await request.app.state.container.application.cancel(scope.scope_hash, run_id)
    if record is None:
        raise HTTPException(404, "运行不存在或不属于当前作用域")
    return snapshot(record)


@router.post("/compat/dify/v1/chat-messages", dependencies=[Depends(require_service_identity)])
@router.post("/compat/dify/v1/workflows/run", dependencies=[Depends(require_service_identity)])
async def dify_placeholder():
    raise HTTPException(
        501, {"code": "adapter_not_enabled", "message": "Dify适配尚未启用，请使用原生/v1/chat。"}
    )


@router.get("/health/live", response_model=HealthResponse)
async def live(request: Request):
    settings = request.app.state.container.settings
    return HealthResponse(
        status="ok",
        service=settings.service_name,
        version=settings.service_version,
        checks={"process": "alive"},
    )


@router.get("/health/ready", response_model=HealthResponse)
async def ready(request: Request):
    container = request.app.state.container
    try:
        await container.ready()
    except Exception as exc:
        raise HTTPException(503, "持久化或实例租约暂不可用") from exc
    return HealthResponse(
        status="ok",
        service=container.settings.service_name,
        version=container.settings.service_version,
        checks={
            "graph": "compiled",
            "persistence": container.persistence_status,
            "configuration": "valid",
        },
    )


@router.get("/health/dependencies", response_model=HealthResponse)
async def dependencies(request: Request):
    container = request.app.state.container
    reachable, description = await container.ai_webapi_client.ping()
    return HealthResponse(
        status="ok" if reachable else "degraded",
        service=container.settings.service_name,
        version=container.settings.service_version,
        checks={"ai_webapi": description},
    )
