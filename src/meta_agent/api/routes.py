"""
创建日期：2026-08-29
文件功能：原生阻塞/SSE、作用域限定状态与取消接口；Dify仅保留未启用入口。
"""

import asyncio

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

from meta_agent.api.schemas import HealthResponse, NativeChatRequest, RunAccessRequest
from meta_agent.api.security import require_service_identity
from meta_agent.application.service import ApplicationRequest, ApplicationRun
from meta_agent.contracts import DomainError, fingerprint
from meta_agent.events.delivery import EventDelivery
from meta_agent.events.native import SnapshotPurpose
from meta_agent.orchestration.identity import trusted_scope_from_inputs

router = APIRouter()


async def scope_for(payload: RunAccessRequest, request: Request):
    try:
        inputs = dict(payload.inputs)
        project_id = inputs.get("projectPatientId")
        if project_id is None:
            project_id = inputs.get("project_patient_id")
        phone = inputs.get("patientPhone")
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


@router.post("/v1/chat", dependencies=[Depends(require_service_identity)])
async def chat(payload: NativeChatRequest, request: Request):
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
    if run.reused:
        return JSONResponse(
            container.output_adapter.snapshot(run.record, purpose=SnapshotPurpose.REUSED),
            status_code=202 if run.record.status == "running" else 200,
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
    return JSONResponse(
        container.output_adapter.snapshot(run.record, purpose=SnapshotPurpose.BLOCKING)
    )


def stream(run: ApplicationRun, request: Request):
    container = request.app.state.container
    return EventDelivery(
        run.emitter,
        container.output_adapter,
        lambda: container.application.cancel(run.record.scope_key, run.record.run_id),
    ).stream()


@router.post("/v1/runs/{run_id}/status", dependencies=[Depends(require_service_identity)])
async def run_status(run_id: str, payload: RunAccessRequest, request: Request):
    scope = await scope_for(payload, request)
    record = await request.app.state.container.repository.run(scope.scope_hash, run_id)
    if record is None:
        raise HTTPException(404, "运行不存在或不属于当前作用域")
    return request.app.state.container.output_adapter.snapshot(
        record, purpose=SnapshotPurpose.STATUS
    )


@router.post("/v1/runs/{run_id}/cancel", dependencies=[Depends(require_service_identity)])
async def cancel(run_id: str, payload: RunAccessRequest, request: Request):
    scope = await scope_for(payload, request)
    record = await request.app.state.container.application.cancel(scope.scope_hash, run_id)
    if record is None:
        raise HTTPException(404, "运行不存在或不属于当前作用域")
    return request.app.state.container.output_adapter.snapshot(
        record, purpose=SnapshotPurpose.CANCEL
    )


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
