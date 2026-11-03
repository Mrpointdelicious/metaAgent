"""
创建日期：2026-08-29
文件功能：原生与交互聊天阻塞/SSE入口，以及作用域限定的状态与取消接口。
"""

import asyncio
from dataclasses import replace
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

from meta_agent.adapters.dify import DifyEventAdapter
from meta_agent.api.schemas import (
    ChatRequest,
    DifyChatRequest,
    HealthResponse,
    NativeChatRequest,
    RunAccessRequest,
)
from meta_agent.api.security import require_service_identity
from meta_agent.application.conversations import owner_key
from meta_agent.application.service import ApplicationRequest, ApplicationRun
from meta_agent.contracts import DomainError, RunRecord
from meta_agent.events.dify import DifyEventAdapter as SettledDifyAdapter
from meta_agent.events.stream import NativeEventAdapter
from meta_agent.orchestration.identity import trusted_scope_from_inputs

router = APIRouter()


async def scope_for(payload: RunAccessRequest, request: Request):
    try:
        scope = trusted_scope_from_inputs(
            dict(payload.inputs),
            payload.user,
            request.app.state.container.settings.default_tenant_id,
        )
        if scope.patient_identity is not None:
            scope = await request.app.state.container.ai_webapi_client.resolve_patient_identity(
                scope
            )
        client = getattr(getattr(request, "state", None), "client", None)
        if client:
            scope = replace(scope, client_id=client.name)
        return scope
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
    conversation = payload.conversation_id
    try:
        run = await container.application.start(
            ApplicationRequest(
                payload.query,
                scope,
                conversation,
                payload.request_id,
                raw_inputs=dict(payload.inputs),
                client=getattr(getattr(request, "state", None), "client", None),
                protocol="dify" if isinstance(payload, DifyChatRequest) else "native",
            )
        )
    except DomainError as exc:
        raise HTTPException(
            409
            if exc.code == "idempotency_conflict"
            else 404
            if exc.code == "conversation_not_found"
            else 422,
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
    adapter = SettledDifyAdapter()
    if run.reused:
        return JSONResponse(
            adapter.message(run.record, reused=True),
            status_code=202 if run.record.status == "running" else 200,
        )
    if payload.response_mode == "streaming":
        return StreamingResponse(
            stream_dify(run, request),
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
    body = adapter.message(run.record)
    commands = [e.payload["command_code"] for e in run.record.events if e.type == "action_ready"]
    if commands:
        body["answer"] = "".join(commands) + body["answer"]
    return JSONResponse(body)


async def stream_dify(run: ApplicationRun, request: Request):
    adapter = DifyEventAdapter(run.record)
    application = request.app.state.container.application
    incremental = (
        bool(application.agent_runtime) or run.record.metrics.get("branch") == "consultation_room"
    )
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
            # Planned answers can replace earlier revisions. Publish their settled
            # projection once; consultation/agent answers are final event payloads.
            if incremental:
                frame = adapter.encode(event)
                if frame:
                    yield frame
                await run.emitter.mark_dispatched(event)
        if not incremental:
            settled = SettledDifyAdapter()
            for frame in settled.frames(run.record):
                yield settled.encode(frame)
    finally:
        if not exhausted:

            async def terminate():
                await request.app.state.container.application.cancel(
                    run.record.scope_key, run.record.run_id
                )
                await run.emitter.disconnect()

            await asyncio.shield(terminate())


dify_stream = stream_dify


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


async def conversation_history(payload, request, conversation_id="", limit=20, cursor=""):
    scope = await scope_for(payload, request)
    repository = request.app.state.container.repository
    selector = request.app.state.container.application.conversations
    limit = max(1, min(limit, 100))
    if not conversation_id:
        metas = await repository.list_conversations(owner_key(scope), limit=1000)
        visible = []
        for meta in metas:
            binding = await selector.get(scope, meta["conversation_id"])
            if binding and await can_read_conversation(binding, scope, request):
                visible.append({**meta, "agent_kind": binding.agent_kind})
        visible.sort(key=lambda m: m["updated_at"], reverse=True)
        if cursor:
            index = next((i for i, m in enumerate(visible) if m["conversation_id"] == cursor), None)
            visible = visible[index + 1 :] if index is not None else []
        return {
            "limit": limit,
            "has_more": len(visible) > limit,
            "data": [
                {
                    "id": m["conversation_id"],
                    "name": m["name"],
                    "inputs": m["inputs"],
                    "status": "normal",
                    "agent_kind": m["agent_kind"],
                    "created_at": int(m["created_at"]),
                    "updated_at": int(m["updated_at"]),
                }
                for m in visible[:limit]
            ],
        }
    binding = await selector.get(scope, conversation_id)
    if not binding or not await can_read_conversation(binding, scope, request):
        raise HTTPException(404, "会话不存在或不属于当前作用域")
    scope = replace(
        scope, agent_kind=binding.agent_kind, consultation_context=binding.consultation_context
    )
    memory = await repository.conversation(scope.thread_id(conversation_id))
    rows = [
        {
            "id": f"{conversation_id}:{i}",
            "conversation_id": conversation_id,
            "query": turn["user"],
            "answer": turn["assistant"],
            "inputs": {},
            "created_at": turn.get("created_at", int(binding.created_at) + i),
            "feedback": None,
            "retriever_resources": [],
            "message_files": [],
        }
        for i, turn in enumerate(memory.turns)
    ]
    rows.reverse()
    if cursor:
        index = next((i for i, row in enumerate(rows) if row["id"] == cursor), None)
        rows = rows[index + 1 :] if index is not None else []
    return {"limit": limit, "has_more": len(rows) > limit, "data": rows[:limit]}


async def can_read_conversation(binding, scope, request):
    if binding.authorization_scope != scope.scope_hash:
        return False
    if binding.agent_kind != "consultation_room" or not binding.authorized_patient_binding:
        return True
    from pydantic import ValidationError

    from meta_agent.flows.consultation_room.access import ConsultationAccess

    provider = request.app.state.container.application.consultation_identity_provider
    if provider is None:
        return False
    selected = replace(
        scope,
        agent_kind=binding.agent_kind,
        position=binding.position,
        consultation_id=binding.consultation_id,
        consultation_context=binding.consultation_context,
        consultation_patient_ref=binding.authorized_patient_binding,
    )
    try:
        async with asyncio.timeout(15):
            access = ConsultationAccess.model_validate(
                await provider.authorize(selected, "history")
            )
        return (
            access.authorization_scope == scope.scope_hash
            and access.consultation_context == binding.consultation_context
            and access.patient_ref == binding.authorized_patient_binding
            and "history" in access.allowed_categories
        )
    except (DomainError, ValidationError, TimeoutError):
        return False


@router.get("/v1/conversations", dependencies=[Depends(require_service_identity)])
async def list_conversations(
    user: str,
    request: Request,
    last_id: str = "",
    limit: int = 20,
    tenant_id: str = "",
    role: str = "patient",
):
    inputs = {"role": role}
    if tenant_id:
        inputs["tenant_id"] = tenant_id
    return await conversation_history(
        RunAccessRequest(user=user, inputs=inputs), request, limit=limit, cursor=last_id
    )


@router.get("/v1/messages", dependencies=[Depends(require_service_identity)])
async def list_messages(
    user: str,
    conversation_id: str,
    request: Request,
    limit: int = 20,
    first_id: str = "",
    last_id: str = "",
    tenant_id: str = "",
    role: str = "patient",
):
    inputs = {"role": role}
    if tenant_id:
        inputs["tenant_id"] = tenant_id
    return await conversation_history(
        RunAccessRequest(user=user, inputs=inputs),
        request,
        conversation_id,
        limit,
        first_id or last_id,
    )


@router.post("/v1/conversations/query", dependencies=[Depends(require_service_identity)])
async def query_conversations(payload: RunAccessRequest, request: Request, limit: int = 20):
    return await conversation_history(payload, request, limit=limit)


@router.post("/v1/messages/query", dependencies=[Depends(require_service_identity)])
async def query_messages(
    payload: RunAccessRequest,
    request: Request,
    conversation_id: str,
    limit: int = 20,
    first_id: str = "",
):
    return await conversation_history(payload, request, conversation_id, limit, first_id)


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
