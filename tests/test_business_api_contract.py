"""
创建日期：2026-11-07
文件功能：以业务实际字段形状验证空间别名、无版本导航和单次动作输出。
"""

import asyncio
import json
from dataclasses import replace

import httpx
import pytest
from langchain_core.messages import AIMessage
from pydantic import Field

from meta_agent.agent.runtime import SingleAgentRuntime
from meta_agent.app import create_app
from meta_agent.application.conversations import owner_key
from meta_agent.contracts import DomainError
from meta_agent.orchestration.identity import trusted_scope_from_inputs
from tests.helpers.runtime import runtime
from tests.test_scene_instruction import scene_body
from tests.test_single_agent import ScriptedModel, tool_call


def business_payload(**changes):
    return {
        "query": "带我去三楼。",
        "response_mode": "streaming",
        "conversation_id": "",
        "user": "business-user-001",
        "inputs": {
            "spaceid": "space",
            "apikey": "synthetic-legacy-key",
            "apitoken": "synthetic-legacy-token",
            "companykb": "synthetic-company",
            "maxkapp": "synthetic-app",
            "maxkbauth": "synthetic-maxkb-auth",
            "timestamp": "1700.01",
            "identity": "医生",
            "pos": "二楼 问诊中心",
            "requesterphone": "requester-account",
        },
        "files": [],
        **changes,
    }


class ToolSelectionModel(ScriptedModel):
    tool_sets: list[set[str]] = Field(default_factory=list)

    def bind_tools(self, tools, **kwargs):
        self.tool_sets.append({tool.name for tool in tools})
        return self


@pytest.mark.parametrize("alias", ["spaceid", "spaceId", "space_id"])
def test_space_aliases_preserve_scope_and_public_history(client, auth_headers, alias):
    payload = business_payload(query="你好", response_mode="blocking")
    payload["inputs"][alias] = payload["inputs"].pop("spaceid")
    response = client.post("/v1/chat-messages", headers=auth_headers, json=payload)
    assert response.status_code == 200
    scope = trusted_scope_from_inputs(payload["inputs"], payload["user"], "default")
    assert scope.space_id == "space" and scope.role == "clinician"
    history = client.get(
        "/v1/conversations", headers=auth_headers, params={"user": payload["user"]}
    )
    assert history.status_code == 200
    row = history.json()["data"][0]
    assert row["id"] == response.json()["conversation_id"]
    assert row["inputs"]["space_id"] == "space"
    assert row["inputs"][alias] == "space"
    assert set(row["inputs"]) <= {"spaceid", "spaceId", "space_id", "pos", "timestamp"}
    assert "synthetic-legacy" not in response.text + history.text
    assert "synthetic-maxkb-auth" not in response.text + history.text


def test_blank_aliases_do_not_hide_space_and_conflicts_are_rejected(client, auth_headers):
    payload = business_payload(query="你好", response_mode="blocking")
    payload["inputs"].update(space_id="  ", spaceId=None)
    assert trusted_scope_from_inputs(payload["inputs"], "actor", "default").space_id == "space"
    assert client.post("/v1/chat-messages", headers=auth_headers, json=payload).status_code == 200
    payload["inputs"]["spaceId"] = "another-space"
    assert client.post("/v1/chat-messages", headers=auth_headers, json=payload).status_code == 422
    payload["inputs"]["spaceId"] = 123
    assert client.post("/v1/chat-messages", headers=auth_headers, json=payload).status_code == 422


@pytest.mark.parametrize("endpoint", ["/v1/chat-messages", "/compat/dify/v1/chat-messages"])
@pytest.mark.parametrize("mode", ["streaming", "blocking"])
def test_business_navigation_without_version_delivers_one_action(endpoint, mode):
    async def check():
        async with runtime(
            scene_actions_enabled=True,
            service_bearer_token="primary-test",
            service_bearer_token_secondary="secondary-test",
        ) as (container, backend):
            payload = business_payload(response_mode=mode)
            model = ToolSelectionModel(
                messages=iter(
                    [
                        tool_call("navigate_scene", {"target": payload["query"]}),
                        AIMessage("已发出前往三楼的指令，请留意画面变化。"),
                    ]
                )
            )
            container.application.agent_runtime = SingleAgentRuntime(model)
            backend.overrides["navigate_scene"] = scene_body(
                [payload["query"]], version=19, codes=["[Telepor:]2"]
            )
            app = create_app(container.settings)
            app.state.container = container
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://test",
                headers={"Authorization": "Bearer secondary-test"},
            ) as client:
                response = await client.post(endpoint, json=payload)
            assert response.status_code == 200
            if mode == "streaming":
                frames = [
                    json.loads(s[6:]) for s in response.text.splitlines() if s.startswith("data: ")
                ]
                messages = [f for f in frames if f["event"] == "message"]
                answer = "".join(f["answer"] for f in messages)
                actions = [f for f in messages if f.get("action_id")]
                assert len(actions) == 1 and actions[0]["command_order"] == 1
                assert frames[-1]["event"] == "message_end"
                body = frames[-1]
            else:
                body = response.json()
                answer = body["answer"]
            assert answer.count("[Telepor:]2") == 1
            assert "[timestamp=1700.01]" in answer and "[Answer:]" in answer
            assert body["metadata"]["status"] == "succeeded"
            delivery = "dispatched" if mode == "streaming" else "delivery_unknown"
            assert list(body["metadata"]["action_delivery"].values()) == [delivery]
            assert backend.calls == [
                ("navigate_scene", {"target": payload["query"], "spaceId": "space"})
            ]
            assert model.tool_sets and all("navigate_scene" in ts for ts in model.tool_sets)
            assert all("resolve_scene_instruction" not in ts for ts in model.tool_sets)
            scope = trusted_scope_from_inputs(payload["inputs"], payload["user"], "default")
            meta = await container.repository.conversation_meta(
                owner_key(replace(scope, client_id="secondary")), body["conversation_id"]
            )
            assert meta["inputs"]["space_id"] == "space"
            assert "synthetic-legacy" not in json.dumps(meta)

    asyncio.run(check())


def test_upstream_navigation_failure_is_terminal_without_action():
    async def check():
        async with runtime(scene_actions_enabled=True, service_bearer_token="test") as (c, backend):
            payload = business_payload()
            c.application.agent_runtime = SingleAgentRuntime(
                ScriptedModel(
                    messages=iter(
                        [
                            tool_call("navigate_scene", {"target": payload["query"]}),
                            AIMessage("暂时没能发出导航指令，请稍后再试。"),
                        ]
                    )
                )
            )
            backend.overrides["navigate_scene"] = DomainError("http_502", "合成上游故障。")
            app = create_app(c.settings)
            app.state.container = c
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://test",
                headers={"Authorization": "Bearer test"},
            ) as client:
                response = await client.post("/v1/chat-messages", json=payload)
            frames = [
                json.loads(s[6:]) for s in response.text.splitlines() if s.startswith("data: ")
            ]
            assert response.status_code == 200 and frames[-1]["event"] == "message_end"
            assert frames[-1]["metadata"]["status"] == "unavailable"
            assert not frames[-1]["metadata"]["action_delivery"]
            assert "[Telepor:]" not in response.text and "[ClickPoint:]" not in response.text

    asyncio.run(check())
