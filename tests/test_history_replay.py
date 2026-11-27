"""
创建日期：2026-11-26
文件功能：验证历史消息代理的报告图片读取、鉴权和错误响应。
"""

import httpx
from fastapi.testclient import TestClient

from meta_agent.api.history_replay import create_app
from meta_agent.config import Settings


REF = "irego_scope_v2_" + "f" * 64
BASE = "http://reports:15043/aiwebapi/ai/patients/report-artifacts/" + REF + "/content"
OLD = "patient_artifact_v2.payload." + "x" * 43
DIRECT = BASE.replace("/aiwebapi/", "/api/")
PNG = bytes.fromhex("89504e470d0a1a0a") + b"fixture"


def test_relay_only_refreshes_visible_patient_turns_and_preserves_errors():
    import json

    seen = []
    def history(req):
        if req.headers.get("authorization") != "Bearer caller":
            return httpx.Response(401, headers={"WWW-Authenticate": "Bearer"})
        if req.url.params.get("conversation_id") == "missing":
            return httpx.Response(404, json={"detail": "missing"})
        if req.url.params.get("role") == "patient" and req.url.params["conversation_id"] == "clinician":
            return httpx.Response(404)
        return httpx.Response(200, json={"has_more": True, "limit": 1, "data": [
            {"id": "c:7", "answer": "[Cweb]" + BASE + "?access_grant=" + OLD, "query": "q"}
        ]})

    def images(req):
        assert req.url.path == "/api/ai/patients/report-artifacts/" + REF + "/content"
        assert req.headers["authorization"] == "Bearer internal"
        seen.append(json.loads(req.content))
        if seen[-1]["system_context"]["user"] == "outage":
            return httpx.Response(503)
        return httpx.Response(200, content=PNG, headers={"Content-Type": "image/png"})

    app = create_app(Settings(patient_identity_require_token=False, ai_webapi_bearer_token="internal"),
                     "http://original", httpx.MockTransport(history), httpx.MockTransport(images))
    with TestClient(app) as client:
        args = {"conversation_id": "c", "user": "viewer", "first_id": "c:8", "limit": 1}
        headers = {"Authorization": "Bearer caller"}
        assert client.get("/v1/messages", params=args).status_code == 401
        assert client.get("/v1/messages", params={**args, "conversation_id": "missing"}, headers=headers).status_code == 404
        result = client.get("/v1/messages", params=args, headers=headers)
        assert result.json() == {"has_more": True, "limit": 1, "data": [{"id": "c:7", "answer": "[Cweb]" + DIRECT, "query": "q", "report_artifacts": [REF]}]}
        assert "no-store" in result.headers["cache-control"]
        lookup = {**args, "artifact_ref": REF, "message_id": "c:7"}
        assert client.get("/v1/messages", params=lookup, headers=headers).content == PNG
        assert client.get("/v1/messages", params={**lookup, "message_id": "c:404"}, headers=headers).status_code == 404
        assert client.get("/v1/messages", params={**lookup, "conversation_id": "clinician"}, headers=headers).status_code == 404
        posted = client.post("/v1/messages/query", params=lookup, headers=headers,
                             json={"user": "viewer", "inputs": {"role": "patient"}})
        assert posted.content == PNG
        assert [p["system_context"]["user"] for p in seen] == ["viewer", "viewer"]
        assert all(p["original_grant"] == OLD for p in seen)
        assert client.get("/v1/messages", params={**lookup, "user":"outage"}, headers=headers).status_code == 503
        assert client.get("/v1/messages", params={**args, "user":"outage"}, headers=headers).status_code == 200
        assert client.post("/v1/chat-messages").status_code == 404
