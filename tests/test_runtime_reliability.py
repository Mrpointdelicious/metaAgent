"""
创建日期：2026-09-12
文件功能：验证原生流断线、并发去重、缓存刷新、趋势边界及外部响应校验。
"""

import asyncio
from types import SimpleNamespace

import httpx
import pytest

from meta_agent.api.routes import stream
from meta_agent.contracts import DomainError, Goal
from meta_agent.tools.ai_webapi import AIWebApiClient
from meta_agent.tools.demo import demo_response
from tests.helpers.runtime import SeedPlanner, answers, request, runtime, scene_response
from tests.helpers.settings import AppTestSettings


def test_concurrent_duplicate_requests_start_once():
    async def check():
        async with runtime() as (c, backend):
            backend.gates["get_multisource_patient_context"] = asyncio.Event()
            runs = await asyncio.gather(
                *[c.application.start(request("查询患者信息")) for _ in range(20)]
            )
            assert len({run.record.run_id for run in runs}) == 1
            assert sum(not run.reused for run in runs) == 1
            backend.gates["get_multisource_patient_context"].set()
            await next(run.task for run in runs if run.task)
            assert len(backend.calls) == 1

    asyncio.run(check())


def test_cancel_before_runner_starts_reaches_terminal_state():
    async def check():
        async with runtime() as (c, backend):
            req = request("查询患者信息")
            run = await c.application.start(req)
            await c.application.cancel(req.scope.scope_hash, run.record.run_id)
            assert run.record.status == "cancelled"
            assert run.record.events[-1].type == "completed"
            assert not c.application.active and not backend.calls

    asyncio.run(check())


def test_memory_retains_six_turns_and_latest_record():
    async def check():
        async with runtime() as (c, _):
            req = request("解读最近训练")
            await c.application.execute(req)
            for i in range(8):
                await c.application.execute(request("你好", f"greet-{i}"))
            memory = await c.repository.conversation(req.scope.thread_id(req.conversation_id))
            assert len(memory.turns) == 6
            assert memory.current_record.session_ref == "synthetic-session"
            assert all(t["assistant"] for t in memory.turns)

    asyncio.run(check())


def test_stream_disconnect_cancels_tools_and_marks_delivery_unknown():
    async def check():
        async with runtime(scene_actions_enabled=True) as (c, backend):
            backend.overrides["navigate_scene"] = scene_response
            backend.gates["get_multisource_patient_context"] = asyncio.Event()
            run = await c.application.start(request("打开面板，查询患者信息"))
            iterator = stream(
                run, SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(container=c)))
            )
            async for chunk in iterator:
                if "event: action_ready" in chunk:
                    break
            await iterator.aclose()
            assert run.record.status == "cancelled"
            assert set(run.record.action_delivery.values()) == {"delivery_unknown"}
            assert "get_multisource_patient_context" in backend.cancelled

    asyncio.run(check())


def test_refresh_replaces_normal_profile_cache():
    async def check():
        async with runtime() as (c, backend):
            await c.application.execute(request("查询患者信息"))
            body = demo_response("get_multisource_patient_context", {})
            body["data"]["profile_brief"]["facts"]["说明"] = "合成更新资料"
            body["meta"]["watermark"] = "updated"
            backend.overrides["get_multisource_patient_context"] = body
            refreshed = await c.application.execute(request("刷新患者信息", "r2"))
            assert "合成更新资料" in answers(refreshed.record)
            cached = await c.application.execute(request("查询患者信息", "r3"))
            assert "合成更新资料" in answers(cached.record)
            assert len(backend.calls) == 2

    asyncio.run(check())


def test_trend_ineligible_preserves_window_and_rejects_claim():
    async def check():
        async with runtime() as (c, backend):
            body = {
                "tool_name": "get_irego_longitudinal_analysis",
                "contract_version": "1.6.0",
                "request_id": "fixture",
                "status": "partial",
                "meta": {},
                "data": {
                    "window": {
                        "requested_report_count": 4,
                        "resolved_report_count": 2,
                        "started_at": "2026-09-01T10:00:00+08:00",
                        "ended_at": "2026-09-02T10:00:00+08:00",
                        "comparison_mode": "not_comparable",
                        "is_contiguous": True,
                    },
                    "quality": {"explanation_boundary": "合成记录条件不同，不可比较。"},
                    "projects": [
                        {
                            "lanes": [
                                {
                                    "metric_series": [
                                        {
                                            "comparison_eligible": False,
                                            "comparison_limitations": ["合成训练条件不同"],
                                            "interpretation": {
                                                "patient_message": "不应输出的改善断言"
                                            },
                                            "points": [],
                                        }
                                    ]
                                }
                            ]
                        }
                    ],
                },
            }
            backend.overrides["get_irego_longitudinal_analysis"] = body
            run = await c.application.execute(request("解读最近四次训练趋势"))
            text = answers(run.record)
            assert "请求记录数：4" in text and "窗口记录数：2" in text
            assert "不可比较" in text and "不应输出的改善断言" not in text
            assert backend.calls[0][1]["selector"] == "latest_contiguous"

    asyncio.run(check())


def test_model_cannot_authorize_quoted_action():
    async def check():
        async with runtime(scene_actions_enabled=True) as (c, backend):
            c.application.planner = SeedPlanner(
                [
                    Goal(
                        goal_id="g1",
                        kind="scene_action",
                        domain="scene",
                        query_span="打开面板",
                        output="action",
                    )
                ]
            )
            run = await c.application.execute(request("他说“打开面板”，这是什么意思"))
            assert not backend.calls and run.record.status == "clarification"

    asyncio.run(check())


def test_expired_anchor_is_revalidated_as_same_record():
    async def check():
        async with runtime() as (c, backend):
            req = request("解读最近训练")
            await c.application.execute(req)
            memory = await c.repository.conversation(req.scope.thread_id(req.conversation_id))
            await c.repository.delete(
                "evidence", req.scope.scope_hash, memory.current_record.evidence_id
            )
            backend.calls.clear()
            await c.application.execute(request("把刚才那次生成图", "r2"))
            assert backend.calls[0][0] == "get_irego_session_analysis"
            assert backend.calls[0][1]["selector"] == "session_ref"
            assert backend.calls[0][1]["session_ref"] == "synthetic-session"
            assert not any(n == "get_irego_patient_history" for n, _ in backend.calls)

    asyncio.run(check())


@pytest.mark.parametrize("kind", ["http", "json", "size"])
def test_http_client_rejects_invalid_responses(kind):
    async def check():
        def handler(request):
            if kind == "http":
                return httpx.Response(503)
            if kind == "json":
                return httpx.Response(200, content=b"not json")
            return httpx.Response(200, content=b"x" * 5000)

        client = AIWebApiClient(
            AppTestSettings(dry_run=False, max_tool_response_bytes=4096),
            httpx.MockTransport(handler),
        )
        try:
            with pytest.raises(DomainError) as failure:
                await client.post("get_irego_patient_history", {})
            assert (
                failure.value.code
                == {"http": "http_503", "json": "invalid_json", "size": "response_too_large"}[kind]
            )
            assert failure.value.retryable == (kind == "http")
        finally:
            await client.close()

    asyncio.run(check())


def test_artifact_origin_and_content_type_validation():
    async def check():
        seen = []

        def handler(request):
            seen.append(request)
            if request.url.path.endswith("redirect"):
                return httpx.Response(302, headers={"location": "http://unknown.example.test/x"})
            if request.url.path.endswith("html"):
                return httpx.Response(200, headers={"content-type": "text/html"})
            return httpx.Response(200, headers={"content-type": "image/png"})

        client = AIWebApiClient(
            AppTestSettings(
                ai_webapi_base_url="http://backend.test/api",
                ai_webapi_bearer_token="synthetic-secret",
            ),
            httpx.MockTransport(handler),
        )
        try:
            assert not await client.artifact_available("http://unknown.test/image")
            assert not seen
            assert not await client.artifact_available("http://backend.test/redirect")
            assert not await client.artifact_available("http://backend.test/html")
            assert await client.artifact_available("http://backend.test/image")
            assert all("authorization" not in req.headers for req in seen)
        finally:
            await client.close()

    asyncio.run(check())
