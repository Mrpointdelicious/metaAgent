"""
创建日期：2026-09-12
文件功能：提供不读取本地密钥的原生链路合成夹具和可控后端。
"""

import asyncio
from contextlib import asynccontextmanager
from copy import deepcopy

from meta_agent.application.service import ApplicationRequest
from meta_agent.contracts import IntentDecision
from meta_agent.infrastructure.container import create_container
from meta_agent.orchestration.identity import TrustedScope
from meta_agent.tools.demo import demo_response
from tests.helpers.settings import AppTestSettings


class SeedPlanner:
    def __init__(self, goals):
        self.goals = goals

    async def parse(self, query, memory, budget):
        return IntentDecision(decision="execute", goals=deepcopy(self.goals))


class Backend:
    def __init__(self):
        self.calls = []
        self.overrides = {}
        self.delays = {}
        self.gates = {}
        self.entered = {}
        self.cancelled = []
        self.artifact_ok = True

    async def post(self, endpoint, payload):
        self.calls.append((endpoint, deepcopy(payload)))
        self.entered.setdefault(endpoint, asyncio.Event()).set()
        try:
            if endpoint in self.gates:
                await self.gates[endpoint].wait()
            if endpoint in self.delays:
                await asyncio.sleep(self.delays[endpoint])
            if endpoint in self.overrides:
                value = self.overrides[endpoint]
                if isinstance(value, Exception):
                    raise value
                return deepcopy(value(payload) if callable(value) else value)
            return demo_response(endpoint, payload)
        except asyncio.CancelledError:
            self.cancelled.append(endpoint)
            raise

    async def artifact_available(self, url):
        return self.artifact_ok


def request(query, rid="r1", *, scope=None, conversation="c1"):
    return ApplicationRequest(
        query,
        scope or TrustedScope("tenant", "actor", "461", space_id="space", scene_version=7),
        conversation,
        rid,
    )


@asynccontextmanager
async def runtime(**settings):
    config = AppTestSettings(
        app_env="test",
        dry_run=True,
        persistence_backend="memory",
        planner_mode="deterministic",
        **settings,
    )
    container = await create_container(config)
    backend = Backend()
    container.application.backend = backend
    try:
        yield container, backend
    finally:
        await container.close()


def answers(record):
    return "\n".join(e.payload["text"] for e in record.events if e.type == "answer_part")


def scene_response(payload):
    return {
        "status": 200,
        "data": {
            "spaceId": payload["spaceId"],
            "version": 7,
            "commands": [
                {
                    "order": i + 1,
                    "sourceText": text,
                    "status": "matched",
                    "result": f"[ClickPoint:]{i}",
                    "intentType": "click_point",
                }
                for i, text in enumerate(payload["target"].split("，"))
            ],
        },
    }


def report_response(payload):
    return {
        "tool_name": "generate_irego_single_session_report",
        "contract_version": "1.6.0",
        "request_id": "synthetic",
        "status": "success",
        "meta": {},
        "data": {
            "artifact_ref": "artifact-synthetic",
            "image_url": "https://images.example.test/report.png",
            "expires_at": "2099-01-01T00:00:00+00:00",
        },
    }
