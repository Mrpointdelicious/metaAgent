"""
创建日期：2026-08-29
文件功能：Dify chat-messages 兼容接口、作用域限定状态与取消接口。
"""

import asyncio
import logging
from dataclasses import replace
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

from meta_agent.adapters.dify import (
    DifyEventAdapter,
    DifyEventFactory,
    empty_metadata,
    encode_ping,
)
from meta_agent.api.schemas import DifyChatRequest, HealthResponse, RunAccessRequest
from meta_agent.api.security import require_service_identity
from meta_agent.application.service import ApplicationRequest, ApplicationRun
from meta_agent.contracts import DomainError, RunRecord, fingerprint, identifier
from meta_agent.orchestration.identity import trusted_scope_from_inputs

router = APIRouter()

logger = logging.getLogger(__name__)


async def scope_for(inputs: dict[str, Any], user: str, request: Request):
    try:
        inputs = dict(inputs)
        project_id = inputs.get("projectPatientId")
        if project_id is None:
            project_id = inputs.get("project_patient_id")
        # 患者/医生登录后都只传自己的手机号 requesterphone（谁用传谁）。
        # 医生号解析不到患者属正常（医生无患者档案）；角色由 inputs.identity 标注。
        identity = inputs.get("identity")
        if identity in ("医生", "clinician", "doctor"):
            inputs["role"] = "clinician"
        elif identity in ("患者", "patient"):
            inputs["role"] = "patient"
        phone = inputs.get("phone") or inputs.get("requesterphone")
        # 医生（clinician）的 requesterphone 是登录人自己的手机号，不是患者。拿它去解析
        # 患者身份必然失败并注入误导性的「未解析到患者身份」，会让 agent 把挂号等非患者类
        # 操作也误判成身份异常。医生要查某位患者时走可信注入的 patientId / projectPatientId。
        if inputs.get("role") == "clinician":
            phone = None
        identity_note = None
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
                try:
                    identity = await request.app.state.container.ai_webapi_client.resolve_patient_identity(
                        project_patient_id=project_id, phone=phone
                    )
                except DomainError as exc:
                    # 查无患者 / 映射失败不阻断本次请求：以「无患者身份」继续。
                    # 指令、知识问答、挂号等不依赖患者身份的操作照常；患者类工具因无
                    # project 身份被 tool_allowed 禁用，由 agent 按「无患者」作答。
                    identity = None
                    identity_note = exc.message or "未找到对应患者，请核对手机号或患者信息。"
                else:
                    identity_note = None
                if identity is not None:
                    inputs["patientId"] = str(identity["robot_patient_id"])
                    inputs["projectPatientId"] = str(identity["project_patient_id"])
                    inputs["iretourPatientId"] = str(identity["project_patient_id"])
        scope = trusted_scope_from_inputs(
            inputs, user, request.app.state.container.settings.default_tenant_id
        )
        if identity_note:
            scope = replace(scope, identity_note=identity_note)
        return scope
    except DomainError as exc:
        raise HTTPException(
            422, {"code": exc.code, "message": exc.message, "status": 422}
        ) from exc
    except ValueError as exc:
        raise HTTPException(
            422, {"code": "invalid_param", "message": str(exc), "status": 422}
        ) from exc


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


def dify_message_response(record: RunRecord) -> JSONResponse:
    """组装 Dify chat-messages 阻塞响应。"""
    answer = "\n".join(
        e.payload["text"]
        for e in record.events
        if e.type in {"answer_part", "clarification"}
    )
    body = DifyEventFactory(record.run_id, record.conversation_id).message(answer)
    body["metadata"] = empty_metadata()
    return JSONResponse(body)


async def stream_dify(run: ApplicationRun, request: Request):
    adapter = DifyEventAdapter(run.record)
    exhausted = False
    try:
        while True:
            try:
                event = await asyncio.wait_for(run.emitter.queue.get(), timeout=15.0)
            except asyncio.TimeoutError:
                yield encode_ping()
                continue
            if event is None:
                exhausted = True
                break
            frame = adapter.encode(event)
            if frame:
                yield frame
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


@router.post("/v1/chat-messages", dependencies=[Depends(require_service_identity)])
@router.post("/v1/chat", dependencies=[Depends(require_service_identity)])
@router.post("/compat/dify/v1/chat-messages", dependencies=[Depends(require_service_identity)])
async def chat_messages(payload: DifyChatRequest, request: Request):
    container = request.app.state.container
    if len(payload.query) > container.settings.max_query_length:
        raise HTTPException(
            422, {"code": "invalid_param", "message": "query 超过本轮长度限制", "status": 422}
        )
    inputs = dict(payload.inputs)
    scope = await scope_for(inputs, payload.user, request)
    request_id = identifier("req")
    conversation_id = (
        payload.conversation_id
        or "conversation_" + fingerprint([scope.scope_hash, request_id])[:24]
    )
    try:
        run = await container.application.start(
            ApplicationRequest(
                payload.query, scope, conversation_id, request_id, raw_inputs=inputs,
                client=request.state.client,
            )
        )
    except DomainError as exc:
        status = 409 if exc.code == "idempotency_conflict" else 422
        raise HTTPException(
            status, {"code": exc.code, "message": exc.message, "status": status}
        ) from exc
    # 记录每次请求的完整入参（含 inputs 全部键值），便于按 run_id 排查。
    logger.info(
        "chat request received",
        extra={
            "run_id": run.record.run_id,
            "event_type": "chat_request",
            "fields": {
                "user": payload.user,
                "query": payload.query[:200],
                "response_mode": payload.response_mode,
                "conversation_id": conversation_id,
                "inputs": dict(payload.inputs),
                "space_id": scope.space_id,
            },
        },
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
    if run.task is not None:
        try:
            await asyncio.shield(run.task)
        except asyncio.CancelledError:
            await container.application.cancel(scope.scope_hash, run.record.run_id)
            raise
    return dify_message_response(run.record)


@router.get("/v1/conversations", dependencies=[Depends(require_service_identity)])
async def list_conversations(
    user: str, request: Request, last_id: str = "", limit: int = 20
):
    container = request.app.state.container
    limit = max(1, min(limit, 100))
    metas = await container.repository.list_conversations(user, limit=1000)
    metas.sort(key=lambda m: m.get("updated_at", 0), reverse=True)
    if last_id:
        for index, meta in enumerate(metas):
            if meta.get("conversation_id") == last_id:
                metas = metas[index + 1 :]
                break
        else:
            metas = []
    has_more = len(metas) > limit
    data = [
        {
            "id": meta.get("conversation_id", ""),
            "name": meta.get("name", ""),
            "inputs": meta.get("inputs", {}),
            "status": meta.get("status", "normal"),
            "introduction": "",
            "created_at": int(meta.get("created_at", 0)),
            "updated_at": int(meta.get("updated_at", 0)),
        }
        for meta in metas[:limit]
    ]
    return {"limit": limit, "has_more": has_more, "data": data}


@router.get("/v1/messages", dependencies=[Depends(require_service_identity)])
async def list_messages(
    user: str,
    conversation_id: str,
    request: Request,
    limit: int = 20,
    first_id: str = "",
    last_id: str = "",
):
    container = request.app.state.container
    limit = max(1, min(limit, 100))
    meta = await container.repository.conversation_meta(user, conversation_id)
    if meta is None:
        raise HTTPException(
            404, {"code": "conversation_not_exists", "message": "会话不存在。", "status": 404}
        )
    thread_id = meta.get("thread_id", "")
    memory = await container.repository.conversation(thread_id) if thread_id else None
    turns = list(memory.turns if memory else [])

    # 每条消息：稳定 id（用完整列表下标，不随 limit 变化）+ 递增 created_at。
    # 旧数据无 per-turn created_at（历史只存 {user, assistant}），用会话创建时间 + 下标合成递增时间戳，
    # 保证排序与分页游标稳定。
    base_ts = int(meta.get("created_at", 0)) or 0
    all_messages = []
    for index, turn in enumerate(turns):
        user = str(turn.get("user", ""))
        created_at = (
            int(turn["created_at"])
            if isinstance(turn.get("created_at"), (int, float))
            else base_ts + index
        )
        all_messages.append(
            {
                "id": "msg_" + fingerprint([thread_id, index, user])[:24],
                "conversation_id": conversation_id,
                "inputs": meta.get("inputs", {}),
                "query": user,
                "answer": str(turn.get("assistant", "")),
                "message_files": [],
                "feedback": None,
                "retriever_resources": [],
                "created_at": created_at,
            }
        )

    # 游标分页（Dify 语义，turns 按时间升序追加）：
    #   first_id → 返回该消息之前（更早）的一页；last_id → 返回该消息之后（更新）的一页。
    #   默认返回最新 limit 条，升序（最早在前）。
    start = len(all_messages)
    if first_id or last_id:
        for i, m in enumerate(all_messages):
            if first_id and m["id"] == first_id:
                start = i
                break
            if last_id and m["id"] == last_id:
                start = i + 1
                break
    window = all_messages[:start]
    has_more = len(window) > limit
    data = window[-limit:]
    return {"limit": limit, "has_more": has_more, "data": data}


@router.post("/v1/runs/{run_id}/status", dependencies=[Depends(require_service_identity)])
async def run_status(run_id: str, payload: RunAccessRequest, request: Request):
    scope = await scope_for(payload.inputs, payload.user, request)
    record = await request.app.state.container.repository.run(scope.scope_hash, run_id)
    if record is None:
        raise HTTPException(404, "运行不存在或不属于当前作用域")
    return snapshot(record)


@router.post("/v1/runs/{run_id}/cancel", dependencies=[Depends(require_service_identity)])
async def cancel(run_id: str, payload: RunAccessRequest, request: Request):
    scope = await scope_for(payload.inputs, payload.user, request)
    record = await request.app.state.container.application.cancel(scope.scope_hash, run_id)
    if record is None:
        raise HTTPException(404, "运行不存在或不属于当前作用域")
    return snapshot(record)


@router.post("/compat/dify/v1/workflows/run", dependencies=[Depends(require_service_identity)])
async def dify_placeholder():
    raise HTTPException(
        501, {"code": "adapter_not_enabled", "message": "workflows/run 适配尚未启用，请使用 /v1/chat-messages。"}
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
