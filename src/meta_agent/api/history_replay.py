"""Serve historical messages alongside a running single-writer MetaAgent."""

import os
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response

from meta_agent.api.schemas import RunAccessRequest
from meta_agent.application.report_history import ReportReadUnavailable, historical_images, read_history_image
from meta_agent.config import get_settings
from meta_agent.orchestration.identity import trusted_scope_from_inputs
from meta_agent.tools.ai_webapi import AIWebApiClient


def create_app(settings=None, upstream=None, history_transport=None, image_transport=None):
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app):
        origin = httpx.URL(upstream or os.environ["REPORT_HISTORY_UPSTREAM"])
        if origin.scheme not in {"http", "https"} or not origin.host or origin.path != "/" or origin.userinfo or origin.query:
            raise ValueError("REPORT_HISTORY_UPSTREAM must be a fixed HTTP origin")
        async with httpx.AsyncClient(base_url=origin, transport=history_transport,
                                     trust_env=False, follow_redirects=False, timeout=30) as history:
            app.state.history = history
            app.state.images = AIWebApiClient(settings, image_transport)
            try:
                yield
            finally:
                await app.state.images.close()

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(CORSMiddleware, allow_origins=settings.cors_allow_origins,
                       allow_methods=["GET", "POST"], allow_headers=["*"])

    @app.get("/health")
    async def health():
        return {"status": "ok"}

    async def read(request, params, content=None):
        # Only the running history service verifies the original caller and selects visible turns.
        headers = {k: request.headers[k] for k in ("authorization", "content-type") if k in request.headers}
        try:
            return await request.app.state.history.request(
                request.method, request.url.path, params=params, headers=headers, content=content
            )
        except httpx.HTTPError as exc:
            raise HTTPException(502, "历史消息服务暂不可用") from exc

    async def replay(request, payload, original):
        headers = {"Cache-Control": "private, no-store"}
        if original.status_code != 200:
            headers.update({k: original.headers[k] for k in ("content-type", "www-authenticate", "retry-after") if k in original.headers})
            return Response(original.content, status_code=original.status_code, headers=headers)
        data = original.json()
        rows = data.get("data", [])
        ref = request.query_params.get("artifact_ref")
        if ref is not None:
            scope = trusted_scope_from_inputs(
                payload.inputs, payload.user, settings.default_tenant_id,
                require_identity_token=settings.patient_identity_require_token,
                infer_from_requesterphone=settings.patient_identity_infer_from_requesterphone,
            )
            # GET without a role lets the original service infer it from the conversation.
            # Reuse that service's patient view before treating an inferred viewer as a patient.
            if request.method == "GET" and not {"identity", "role"}.intersection(payload.inputs):
                patient_params = history_params(request).merge({"role": "patient"})
                checked = await read(request, patient_params)
                patient_rows = {row["id"]: row.get("answer") for row in checked.json().get("data", [])} if checked.status_code == 200 else {}
                rows = [row for row in rows if patient_rows.get(row["id"]) == row.get("answer")]
            try:
                image = await read_history_image(rows, ref, request.query_params.get("message_id"), scope,
                                                 request.app.state.images, request.query_params["conversation_id"])
            except ReportReadUnavailable:
                return Response(status_code=503, headers=headers)
            if image is None:
                return Response(status_code=404, headers=headers)
            return Response(image, media_type="image/png", headers={**headers, "X-Content-Type-Options": "nosniff"})
        historical_images(rows, request.app.state.images.report_url)
        return JSONResponse(data, headers=headers)

    def history_params(request):
        return httpx.QueryParams([(key, value) for key, value in request.query_params.multi_items()
                                 if key not in {"artifact_ref", "message_id"}])

    @app.get("/v1/messages")
    async def get_messages(request: Request, user: str, conversation_id: str):
        params = request.query_params
        inputs = {k: params[k] for k in ("role", "identity", "tenant_id", "space_id") if k in params}
        return await replay(request, RunAccessRequest(user=user, inputs=inputs), await read(request, history_params(request)))

    @app.post("/v1/messages/query")
    async def post_messages(request: Request, payload: RunAccessRequest, conversation_id: str):
        original = await read(request, history_params(request), await request.body())
        return await replay(request, payload, original)

    return app


app = create_app()
