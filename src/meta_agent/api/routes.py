"""
创建日期：2026-08-29
文件功能：原生与交互聊天阻塞/SSE入口，以及作用域限定的状态与取消接口。
"""

import asyncio
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

from meta_agent.api.schemas import (
    ChatRequest,
    DifyChatRequest,
    HealthResponse,
    NativeChatRequest,
    RunAccessRequest,
)
from meta_agent.api.security import require_service_identity
from meta_agent.application.service import ApplicationRequest, ApplicationRun
from meta_agent.contracts import DomainError, RunRecord, fingerprint
from meta_agent.events.dify import DifyEventAdapter
from meta_agent.events.stream import NativeEventAdapter
from meta_agent.orchestration.identity import trusted_scope_from_inputs

router = APIRouter()


async def scope_for(payload: RunAccessRequest, request: Request):
    try:
        inputs = dict(payload.inputs)
        project_id = inputs.get("projectPatientId")
        if project_id is None:
            project_id = inputs.get("project_patient_id")
        phones = [
            inputs[name]
            for name in ("patientPhone", "patientphone", "patient_phone")
            if inputs.get(name) not in (None, "")
        ]
        if phones and any(value != phones[0] for value in phones[1:]):
            raise ValueError("患者手机号别名不一致")
        phone = phones[0] if phones else None
        if project_id is not None or phone is not None:
            if any(
                inputs.get(k) not in (None, "")
                for k in ("patientId", "patient_id", "robotDbUserId")
            ):
                raise ValueError("原始患者编号与身份映射输入不可同时提供")
            if (
                phone is not None
                or request.app.state.container.settings.orchestration_mode == "legacy"
            ):
                identity = (
                    await request.app.state.container.ai_webapi_client.resolve_patient_identity(
                        project_patient_id=project_id, phone=phone
                    )
                )
                inputs["patientId"] = str(identity["robot_patient_id"])
                inputs["projectPatientId"] = str(identity["project_patient_id"])
                inputs["iretourPatientId"] = str(identity["project_patient_id"])
        return trusted_scope_from_inputs(
            inputs, payload.user, request.app.state.container.settings.default_tenant_id
        )
    except DomainError as exc:
        raise HTTPException(422, {"code": exc.code, "message": exc.message}) from exc
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


async def start_chat(payload: NativeChatRequest, request: Request):
    container = request.app.state.container
    if len(payload.query) > container.settings.max_query_length:
        raise HTTPException(422, "query 超过本轮长度限制")
    scope = await scope_for(payload, request)
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
    return run, scope


@router.post("/v1/chat", dependencies=[Depends(require_service_identity)])
async def chat(payload: ChatRequest, request: Request):
    if isinstance(payload, DifyChatRequest):
        return await dify_chat(payload, request)
    run, scope = await start_chat(payload, request)
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
        await request.app.state.container.application.cancel(scope.scope_hash, run.record.run_id)
        raise
    body = snapshot(run.record, False)
    body["events"] = [e.model_dump(mode="json") for e in run.record.events]
    # Blocking delivery has no transport acknowledgement; keep delivery_unknown.
    return JSONResponse(body)


@router.post("/v1/chat-messages", dependencies=[Depends(require_service_identity)])
@router.post("/compat/dify/v1/chat-messages", dependencies=[Depends(require_service_identity)])
async def dify_chat(payload: DifyChatRequest, request: Request):
    run, scope = await start_chat(payload, request)
    adapter = DifyEventAdapter()
    if run.reused:
        return JSONResponse(
            adapter.message(run.record, reused=True),
            status_code=202 if run.record.status == "running" else 200,
        )
    if payload.response_mode == "streaming":
        return StreamingResponse(
            dify_stream(run, request),
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
        await request.app.state.container.application.cancel(scope.scope_hash, run.record.run_id)
        raise
    return JSONResponse(adapter.message(run.record))


async def dify_stream(run: ApplicationRun, request: Request):
    adapter = DifyEventAdapter()
    exhausted = False
    try:
        while True:
            try:
                event = await asyncio.wait_for(run.emitter.queue.get(), timeout=15)
            except TimeoutError:
                yield "event: ping\n\n"
                continue
            if event is None:
                exhausted = True
                break
        # Native answer revisions replace prior text; Dify messages append text.
        # Publish the settled answer once, after draining progress and tool events.
        for frame in adapter.frames(run.record):
            yield adapter.encode(frame)
    finally:
        if not exhausted:

            async def terminate():
                await request.app.state.container.application.cancel(
                    run.record.scope_key, run.record.run_id
                )
                await run.emitter.disconnect()

            await asyncio.shield(terminate())


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
    scope = await scope_for(payload, request)
    record = await request.app.state.container.repository.run(scope.scope_hash, run_id)
    if record is None:
        raise HTTPException(404, "运行不存在或不属于当前作用域")
    return snapshot(record)


@router.post("/v1/runs/{run_id}/cancel", dependencies=[Depends(require_service_identity)])
async def cancel(run_id: str, payload: RunAccessRequest, request: Request):
    scope = await scope_for(payload, request)
    record = await request.app.state.container.application.cancel(scope.scope_hash, run_id)
    if record is None:
        raise HTTPException(404, "运行不存在或不属于当前作用域")
    return snapshot(record)


@router.post("/compat/dify/v1/workflows/run", dependencies=[Depends(require_service_identity)])
async def dify_workflow_placeholder():
    raise HTTPException(
        501,
        {"code": "adapter_not_enabled", "message": "Dify Workflow适配尚未启用，请使用聊天接口。"},
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
