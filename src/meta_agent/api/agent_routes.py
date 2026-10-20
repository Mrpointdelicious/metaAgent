"""
创建日期：2026-10-10
文件功能：提供独立Run创建、作用域访问、SSE回放、取消和幂等动作ACK接口。
"""

import logging

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.routing import APIRoute

from meta_agent.api.agent_schemas import (
    AcceptedResponse,
    ActionAckRequest,
    ActionAckResponse,
    CreatedRunResponse,
    CreateRunRequest,
    ErrorResponse,
    RunSnapshotResponse,
)
from meta_agent.api.security import require_service_identity
from meta_agent.application.service import ApplicationRequest
from meta_agent.contracts import DomainError, fingerprint
from meta_agent.events.agent import AgentOutputAdapter, RunSubscription
from meta_agent.orchestration.identity import trusted_scope_from_inputs
from meta_agent.tools.ai_webapi import BackendCallError

logger = logging.getLogger(__name__)


def error(status: int, code: str, message: str, retryable: bool = False):
    return JSONResponse(
        {"error": {"code": code, "message": message, "retryable": retryable}},
        status_code=status,
    )


class AgentRoute(APIRoute):
    def get_route_handler(self):
        handler = super().get_route_handler()

        async def handle(request):
            try:
                return await handler(request)
            except RequestValidationError:
                return error(422, "invalid_request", "请求字段不合法，请检查必填字段与取值。")
            except HTTPException as exc:
                codes = {401: "unauthenticated", 403: "forbidden", 404: "run_not_found"}
                messages = {
                    401: "请通过可信接入方认证。",
                    403: "无当前作用域访问权限。",
                    404: "运行不存在或已过期。",
                }
                return error(
                    exc.status_code,
                    codes.get(exc.status_code, "invalid_request"),
                    messages.get(exc.status_code, "请求字段不合法。"),
                )
            except DomainError as exc:
                if isinstance(exc, BackendCallError) and exc.code != "identity_unresolved":
                    return error(
                        503, "dependency_unavailable", "依赖服务暂不可用，请稍后重试。", True
                    )
                status = {
                    "idempotency_conflict": 409,
                    "ack_conflict": 409,
                    "action_not_found": 404,
                    "service_busy": 429,
                }.get(exc.code, 422)
                return error(status, exc.code, exc.message, exc.retryable)
            except Exception:
                logger.exception("Agent Run API is unavailable")
                return error(503, "service_unavailable", "服务暂不可用，请稍后重试。", True)

        return handle


router = APIRouter(
    prefix="/v1/agent/runs",
    route_class=AgentRoute,
    dependencies=[Depends(require_service_identity)],
    responses={code: {"model": ErrorResponse} for code in (401, 403, 404, 409, 422, 429, 503)},
)
adapter = AgentOutputAdapter()


def principal(request: Request):
    # These claims must be injected by the authenticated gateway, never user text.
    user = request.headers.get("X-End-User-ID", "").strip()
    tenant = request.headers.get(
        "X-Tenant-ID", request.app.state.container.settings.default_tenant_id
    ).strip()
    role = request.headers.get("X-User-Role", "patient").strip()
    if not user:
        raise HTTPException(401)
    if (
        len(user) > 160
        or not tenant
        or len(tenant) > 160
        or role not in {"patient", "clinician", "operator"}
    ):
        raise HTTPException(422)
    return trusted_scope_from_inputs({"tenant_id": tenant, "role": role}, user, tenant)


async def owned_run(run_id: str, request: Request):
    identity = principal(request)
    run = await request.app.state.container.application.owned_run(identity.principal_hash, run_id)
    if run is None:
        # Do not disclose whether another principal owns this identifier.
        raise HTTPException(404)
    return run


@router.post("", status_code=202, response_model=CreatedRunResponse)
async def create_run(payload: CreateRunRequest, request: Request):
    container = request.app.state.container
    identity = principal(request)
    if len(payload.query) > container.settings.max_query_length:
        raise HTTPException(422)
    context = payload.context.model_dump(mode="json")
    inputs = {
        "tenant_id": identity.tenant_id,
        "role": identity.role,
        "projectPatientId": payload.context.patient_id,
        "space_id": payload.context.space_id,
        "scene_version": payload.context.scene_version,
    }
    canonical = trusted_scope_from_inputs(inputs, identity.end_user_id, identity.tenant_id)
    conversation = (
        payload.conversation_id
        or "conversation_" + fingerprint([canonical.scope_hash, payload.request_id])[:24]
    )
    digest = fingerprint(
        {
            "scope": canonical.scope_hash,
            "query": payload.query,
            "conversation_id": conversation,
            "context": context,
        }
    )
    existing = await container.application.reuse_request(
        identity.principal_hash, payload.request_id, digest
    )
    if existing is not None:
        return creation_response(existing.record)
    # Legacy workflows need robot.dbuser; the public identity remains project.dbuser.
    if payload.context.patient_id and container.settings.orchestration_mode == "legacy":
        mapped = await container.ai_webapi_client.resolve_patient_identity(
            project_patient_id=payload.context.patient_id, phone=None
        )
        inputs["patientId"] = str(mapped["robot_patient_id"])
    scope = trusted_scope_from_inputs(inputs, identity.end_user_id, identity.tenant_id)
    run = await container.application.start(
        ApplicationRequest(
            payload.query,
            scope,
            conversation,
            payload.request_id,
            context=context,
            owner_key=identity.principal_hash,
            public_request_hash=digest,
        )
    )
    return creation_response(run.record)


def creation_response(record):
    return {
        "request_id": record.request_id,
        "run_id": record.run_id,
        "conversation_id": record.conversation_id,
        "status": "accepted",
        "events_url": f"/v1/agent/runs/{record.run_id}/events",
        "snapshot_url": f"/v1/agent/runs/{record.run_id}",
    }


@router.get("/{run_id}", response_model=RunSnapshotResponse)
async def snapshot(run_id: str, request: Request):
    run = await owned_run(run_id, request)
    return adapter.snapshot(await run.emitter.snapshot())


@router.get(
    "/{run_id}/events",
    response_class=StreamingResponse,
    responses={200: {"content": {"text/event-stream": {"schema": {"type": "string"}}}}},
)
async def events(run_id: str, request: Request, after_seq: int | None = Query(default=None, ge=0)):
    run = await owned_run(run_id, request)
    cursor = after_seq
    if cursor is None:
        raw = request.headers.get("Last-Event-ID", "0")
        if not raw.isascii() or not raw.isdigit() or len(raw) > 16:
            raise HTTPException(422)
        cursor = int(raw)
    record = await run.emitter.snapshot()
    if cursor > len(record.events):
        raise DomainError("invalid_cursor", "事件游标超出当前运行范围。")
    return StreamingResponse(
        RunSubscription(run.emitter, adapter, cursor).stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "X-Run-ID": run_id},
    )


@router.post("/{run_id}/cancel", response_model=AcceptedResponse)
async def cancel(run_id: str, request: Request):
    run = await owned_run(run_id, request)
    await request.app.state.container.application.cancel(run.record.scope_key, run_id)
    return {"accepted": True}


@router.post("/{run_id}/actions/{action_id}/ack", response_model=ActionAckResponse)
async def acknowledge(run_id: str, action_id: str, payload: ActionAckRequest, request: Request):
    run = await owned_run(run_id, request)
    return await request.app.state.container.application.acknowledge_action(
        run, action_id, payload.status, payload.reason
    )
