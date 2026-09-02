"""
创建日期：2026-08-29
文件功能：提供健康检查、Dify Chatflow 与 Workflow 兼容的阻塞和 SSE 路由。
"""

import logging
from collections.abc import AsyncIterator
from typing import Any
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import JSONResponse, StreamingResponse

from meta_agent.adapters.dify import DifyEventFactory, encode_ping, encode_sse
from meta_agent.adapters.legacy_protocol import LegacyProtocol
from meta_agent.api.schemas import DifyChatRequest, DifyWorkflowRequest, HealthResponse
from meta_agent.api.security import require_service_identity
from meta_agent.graph.state import AgentState
from meta_agent.orchestration.identity import TrustedScope, trusted_scope_from_inputs

logger = logging.getLogger(__name__)
router = APIRouter()


def _new_identifier(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex}"


def _validate_query_length(query: str, maximum: int) -> None:
    if len(query) > maximum:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"query 长度不能超过 {maximum} 个字符",
        )


def _scope_or_422(
    inputs: dict[str, Any],
    user: str,
    default_tenant_id: str,
) -> TrustedScope:
    try:
        return trusted_scope_from_inputs(inputs, user, default_tenant_id)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc


def _initial_state(
    query: str,
    scope: TrustedScope,
    conversation_id: str,
    run_id: str,
) -> AgentState:
    return AgentState(
        query=query,
        tenant_id=scope.tenant_id,
        end_user_id=scope.end_user_id,
        patient_id=scope.patient_id,
        scope_hash=scope.scope_hash,
        conversation_id=conversation_id,
        run_id=run_id,
    )


def _graph_config(scope: TrustedScope, conversation_id: str) -> dict[str, Any]:
    return {"configurable": {"thread_id": scope.thread_id(conversation_id)}}


def _legacy_messages(state: dict[str, Any]) -> list[str]:
    messages = [LegacyProtocol.mode(command) for command in state.get("mode_commands") or []]
    messages.append(LegacyProtocol.answer(str(state.get("response_text") or "")))
    messages.extend(LegacyProtocol.image(url) for url in state.get("image_urls") or [])
    return messages


async def _invoke_graph(
    request: Request,
    state: AgentState,
    config: dict[str, Any],
) -> dict[str, Any]:
    container = request.app.state.container
    thread_id = str(config["configurable"]["thread_id"])
    async with container.request_limiter, container.thread_locks.hold(thread_id):
        return await container.graph.ainvoke(state, config=config)


async def _stream_graph(
    request: Request,
    initial_state: AgentState,
    config: dict[str, Any],
    event_factory: DifyEventFactory,
) -> AsyncIterator[str]:
    container = request.app.state.container
    thread_id = str(config["configurable"]["thread_id"])
    aggregate: dict[str, Any] = dict(initial_state)
    yield encode_ping()
    yield encode_sse(event_factory.workflow_started())
    try:
        async with container.request_limiter, container.thread_locks.hold(thread_id):
            async for update in container.graph.astream(
                initial_state,
                config=config,
                stream_mode="updates",
            ):
                if not isinstance(update, dict):
                    continue
                for node_name, node_update in update.items():
                    if isinstance(node_update, dict):
                        aggregate.update(node_update)
                    yield encode_sse(event_factory.node_finished(str(node_name)))
                    if node_name == "compose_response":
                        for message in _legacy_messages(aggregate):
                            yield encode_sse(event_factory.message(message))
        metadata = {"result_ref": aggregate.get("result_ref", "")}
        yield encode_sse(event_factory.message_end(metadata))
        yield encode_sse(
            event_factory.workflow_finished(
                "succeeded",
                {
                    "result": aggregate.get("response_text", ""),
                    "result_ref": aggregate.get("result_ref", ""),
                    "image_urls": aggregate.get("image_urls", []),
                },
            )
        )
    except Exception:
        logger.exception(
            "agent stream failed",
            extra={"run_id": initial_state["run_id"], "event_type": "stream_error"},
        )
        safe_message = "当前请求处理失败，请稍后重试。"
        yield encode_sse(event_factory.error(safe_message))
        yield encode_sse(event_factory.workflow_finished("failed", {}, safe_message))


@router.get("/health/live", response_model=HealthResponse)
async def live(request: Request) -> HealthResponse:
    """仅检查进程和 ASGI 事件循环。"""
    settings = request.app.state.container.settings
    return HealthResponse(
        status="ok",
        service=settings.service_name,
        version=settings.service_version,
        checks={"process": "alive"},
    )


@router.get("/health/ready", response_model=HealthResponse)
async def ready(request: Request) -> HealthResponse:
    """检查核心依赖是否已经装配完成。"""
    container = request.app.state.container
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
async def dependencies(request: Request) -> HealthResponse:
    """检查外部 AI_WebApi，不把短暂外部故障转换为容器重启。"""
    container = request.app.state.container
    reachable, description = await container.ai_webapi_client.ping()
    return HealthResponse(
        status="ok" if reachable else "degraded",
        service=container.settings.service_name,
        version=container.settings.service_version,
        checks={"ai_webapi": description},
    )


@router.post(
    "/compat/dify/v1/chat-messages",
    dependencies=[Depends(require_service_identity)],
)
async def send_chat_message(payload: DifyChatRequest, request: Request) -> Any:
    """运行有会话状态的 Chatflow 兼容请求。"""
    settings = request.app.state.container.settings
    _validate_query_length(payload.query, settings.max_query_length)
    scope = _scope_or_422(payload.inputs, payload.user, settings.default_tenant_id)
    conversation_id = payload.conversation_id.strip() or _new_identifier("conversation")
    task_id = _new_identifier("task")
    workflow_run_id = _new_identifier("run")
    state = _initial_state(payload.query, scope, conversation_id, workflow_run_id)
    config = _graph_config(scope, conversation_id)
    events = DifyEventFactory(task_id, workflow_run_id, conversation_id)

    if payload.response_mode == "streaming":
        return StreamingResponse(
            _stream_graph(request, state, config, events),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    result = await _invoke_graph(request, state, config)
    answer = "\n".join(_legacy_messages(result))
    return JSONResponse(
        {
            "event": "message",
            "task_id": task_id,
            "message_id": task_id,
            "conversation_id": conversation_id,
            "mode": "advanced-chat",
            "answer": answer,
            "metadata": {"result_ref": result.get("result_ref", "")},
        }
    )


@router.post(
    "/compat/dify/v1/workflows/run",
    dependencies=[Depends(require_service_identity)],
)
async def run_workflow(payload: DifyWorkflowRequest, request: Request) -> Any:
    """运行无跨调用会话状态的 Workflow 兼容请求。"""
    settings = request.app.state.container.settings
    query = str(payload.inputs.get("query") or payload.inputs.get("sys.query") or "").strip()
    if not query:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Workflow inputs.query 不能为空",
        )
    _validate_query_length(query, settings.max_query_length)
    scope = _scope_or_422(payload.inputs, payload.user, settings.default_tenant_id)
    conversation_id = _new_identifier("workflow")
    task_id = _new_identifier("task")
    workflow_run_id = _new_identifier("run")
    state = _initial_state(query, scope, conversation_id, workflow_run_id)
    config = _graph_config(scope, conversation_id)
    events = DifyEventFactory(task_id, workflow_run_id, conversation_id)

    if payload.response_mode == "streaming":
        return StreamingResponse(
            _stream_graph(request, state, config, events),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    result = await _invoke_graph(request, state, config)
    return JSONResponse(
        {
            "workflow_run_id": workflow_run_id,
            "task_id": task_id,
            "data": {
                "id": workflow_run_id,
                "workflow_id": "meta_agent",
                "status": "succeeded",
                "outputs": {
                    "result": result.get("response_text", ""),
                    "result_ref": result.get("result_ref", ""),
                    "image_urls": result.get("image_urls", []),
                },
            },
        }
    )
