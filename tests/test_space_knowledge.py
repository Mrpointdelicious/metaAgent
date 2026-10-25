"""
创建日期：2026-10-22
文件功能：验证空间检索隔离、场景归属、提示词上下文及 Dify 请求节点。
"""

import asyncio
import json
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest

from meta_agent.agent.tools import available_tools, build_tools
from meta_agent.config import Settings
from meta_agent.contracts import DomainError
from meta_agent.domains.portfolio import PortfolioKnowledgeAdapter
from meta_agent.domains.space_context import current_space_context, space_catalog
from meta_agent.orchestration.identity import trusted_scope_from_inputs
from meta_agent.prompts.contracts import SpacePromptInputs
from meta_agent.prompts.service import PromptService
from tools.integrate_space_dify import build_graph

SPACE = "上海中医药大学附属岳阳医院"


@pytest.mark.parametrize("key", ["sceneId", "scene_id"])
def test_current_scene_is_validated_and_kept_separate_from_scene_version(key):
    scope = trusted_scope_from_inputs(
        {"spaceId": SPACE, key: "运动评估室", "sceneVersion": 7}, "actor", "tenant"
    )
    assert scope.scene_id == "运动评估室" and scope.scene_version == 7
    assert current_space_context(scope.space_id, scope.scene_id)["floor"] == 4
    prompts = PromptService()
    rendered = prompts.render(
        prompts.bind(),
        "space.context",
        SpacePromptInputs(current_space=current_space_context(scope.space_id, scope.scene_id)),
    )
    assert json.loads(rendered.messages[1][1])["current_space"]["scene_id"] == "运动评估室"


@pytest.mark.parametrize(
    "inputs",
    [
        {"sceneId": "运动评估室"},
        {"spaceId": "另一家医院", "sceneId": "运动评估室"},
        {"spaceId": SPACE, "sceneId": "不存在的六楼手术室"},
        {"spaceId": SPACE, "sceneId": 123},
    ],
)
def test_invalid_or_cross_space_scenes_are_rejected_at_native_entry(client, auth_headers, inputs):
    response = client.post(
        "/v1/chat",
        headers=auth_headers,
        json={"request_id": "invalid-scene", "user": "actor", "query": "你好", "inputs": inputs},
    )
    assert response.status_code == 422


def test_space_isolates_conversations_but_room_changes_stay_in_the_same_hospital():
    def scope(space, scene=None):
        return trusted_scope_from_inputs({"spaceId": space, "sceneId": scene}, "actor", "tenant")

    assert scope(SPACE, "运动评估室").scope_hash == scope(SPACE, "接待大厅").scope_hash
    assert scope(SPACE).thread_id("same-conversation") != scope("另一家医院").thread_id(
        "same-conversation"
    )


def test_reusing_request_id_with_a_different_current_scene_is_a_conflict(client, auth_headers):
    payload = {
        "request_id": "scene-idempotency",
        "user": "actor",
        "query": "你好",
        "response_mode": "blocking",
        "inputs": {"spaceId": SPACE, "sceneId": "接待大厅"},
    }
    assert client.post("/v1/chat", headers=auth_headers, json=payload).status_code == 200
    payload["inputs"]["sceneId"] = "运动评估室"
    assert client.post("/v1/chat", headers=auth_headers, json=payload).status_code == 409


@pytest.mark.parametrize("domain", ["health", "help", "product"])
def test_trusted_space_selects_kb_and_scene_never_becomes_a_retrieval_filter(monkeypatch, domain):
    async def check():
        ids = {name: [str(uuid4())] for name in ("health", "help", "product")}
        settings = Settings(
            _env_file=None,
            portfolio_enabled=True,
            portfolio_token="health-reader",
            portfolio_help_token="hospital-reader",
            portfolio_kb_ids=[str(uuid4())],
            portfolio_space_kb_ids={SPACE: ids},
        )
        calls = []

        def handle(request):
            calls.append(json.loads(request.content))
            expected = "health-reader" if domain == "health" else "hospital-reader"
            assert request.headers["Authorization"] == "Bearer " + expected
            return httpx.Response(
                200,
                json={
                    "evidence": [],
                    "notes": [],
                    "trace_id": str(uuid4()),
                    "used_context_tokens": 0,
                },
            )

        client = httpx.AsyncClient(transport=httpx.MockTransport(handle))
        try:
            monkeypatch.setattr(
                "meta_agent.domains.portfolio.httpx.AsyncClient",
                lambda **kw: type(client)(transport=httpx.MockTransport(handle)),
            )
            ctx = SimpleNamespace(
                settings=settings,
                scope=SimpleNamespace(space_id=SPACE, scene_id="接待大厅"),
                record=SimpleNamespace(metrics={}),
            )
            result = await PortfolioKnowledgeAdapter().execute(
                SimpleNamespace(task_id="knowledge"),
                {"query": "二楼有哪些诊疗室", "domain": domain, "kb_ids": ["caller-override"]},
                ctx,
            )
            assert result.status == "unavailable"
            assert len(calls) == 1 and calls[0]["kb_ids"] == (
                settings.portfolio_kb_ids if domain == "health" else ids[domain]
            )
            assert calls[0]["queries"] == [{"q": "二楼有哪些诊疗室"}]
            assert "scene_id" not in calls[0] and "sceneId" not in calls[0]
            ctx.scope.space_id = "未绑定医院"
            if domain == "health":
                # A new query in any hospital keeps the same global medical scope.
                ctx.record.metrics = {}
                await PortfolioKnowledgeAdapter().execute(
                    SimpleNamespace(task_id="knowledge"), {"query": "查询", "domain": domain}, ctx
                )
                assert calls[-1]["kb_ids"] == settings.portfolio_kb_ids
                return
            with pytest.raises(DomainError) as rejected:
                await PortfolioKnowledgeAdapter().execute(
                    SimpleNamespace(task_id="knowledge"), {"query": "查询", "domain": domain}, ctx
                )
            assert rejected.value.code == "knowledge_space_unavailable"
            assert len(calls) == 1  # Never falls back to the globally configured KB.
        finally:
            await client.aclose()

    asyncio.run(check())


def test_space_tool_schema_supports_hospital_help_without_exposing_identity():
    settings = Settings(_env_file=None, portfolio_enabled=True, portfolio_space_kb_ids={SPACE: {}})
    ctx = SimpleNamespace(settings=settings, frontend_context={}, query="二楼有哪些房间")
    tool = next(t for t in build_tools() if t.name == "search_knowledge")
    selected = available_tools(ctx, [tool])[0]
    properties = selected.args_schema.model_json_schema()["properties"]
    assert properties["domain"]["enum"] == ["health", "product", "help"]
    assert not {"spaceId", "sceneId", "kb_ids", "patientId"} & properties.keys()


def test_workflow_uses_reviewed_source_and_native_request_contract():
    minimal = {
        "nodes": [
            {"id": "start", "data": {"variables": []}},
            {"id": "prepare", "data": {"variables": []}},
            {"id": "invoke", "data": {}},
        ]
    }
    graph = build_graph(minimal)
    namespace = {}
    exec(compile(graph["nodes"][1]["data"]["code"], "dify-prepare", "exec"), namespace)
    values = dict(
        query="  二楼有哪些房间  ",
        conversation_id="conv",
        user_id="actor",
        app_id="app",
        workflow_run_id="run",
        project_patient_id="3799",
        space_id=SPACE,
        scene_id="接待大厅",
    )
    request = json.loads(namespace["main"](**values)["request_body"])
    assert set(request) == {
        "request_id",
        "conversation_id",
        "user",
        "inputs",
        "query",
        "response_mode",
    }
    assert request["inputs"]["spaceId"] == SPACE and request["inputs"]["sceneId"] == "接待大厅"
    assert request["query"] == "二楼有哪些房间" and request["response_mode"] == "blocking"
    for updates in (
        {"space_id": "", "scene_id": "接待大厅"},
        {"project_patient_id": "0"},
    ):
        with pytest.raises(ValueError):
            namespace["main"](**{**values, **updates})
    assert build_graph(graph) == graph
    assert graph["nodes"][0]["data"]["variables"][1]["type"] == "text-input"
    assert "options" not in graph["nodes"][0]["data"]["variables"][1]
    assert graph["nodes"][0]["data"]["variables"][1]["default"] is None
