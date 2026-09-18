"""
创建日期：2026-09-12
文件功能：回放冻结关键场景，验证记录锚点、任务偏序、事实和故障隔离。
"""

import asyncio
from copy import deepcopy

import pytest

from meta_agent.contracts import Condition, DomainError, Goal
from meta_agent.tools.demo import demo_response
from tests.helpers.runtime import (
    SeedPlanner,
    answers,
    report_response,
    request,
    runtime,
    scene_response,
)


@pytest.mark.parametrize("query", ["你好", "那上一次呢", "继续", "查询IREMO训练", "不要打开面板"])
def test_no_speculative_tool_calls(query):
    async def check():
        async with runtime() as (c, backend):
            run = await c.application.execute(request(query))
            assert backend.calls == []
            assert run.record.status in {"succeeded", "clarification", "unsupported"}

    asyncio.run(check())


def test_report_negation_preserves_analysis():
    async def check():
        async with runtime() as (c, backend):
            run = await c.application.execute(request("不要生成报告，只解释最近训练"))
            assert run.record.status == "succeeded"
            assert "0.5 m/s" in answers(run.record)
            assert not any("generate" in name for name, _ in backend.calls)

    asyncio.run(check())


def test_latest_incomplete_is_not_replaced_with_older_usable():
    async def check():
        async with runtime() as (c, backend):
            body = demo_response("get_irego_patient_history", {})
            older = deepcopy(body["data"]["items"][0])
            latest = {
                **older,
                "session_ref": None,
                "training_state": "not_completed",
                "patient_message": "最新记录未完成。",
                "session_time": "2026-09-02T10:00:00+08:00",
            }
            body["data"]["items"] = [latest, older]
            backend.overrides["get_irego_patient_history"] = body
            run = await c.application.execute(request("解读最近训练"))
            assert [n for n, _ in backend.calls] == ["get_irego_patient_history"]
            assert "最新记录未完成" in answers(run.record)
            assert "2026-09-01" not in answers(run.record)
            assert run.record.status == "partial"

    asyncio.run(check())


def test_current_report_reuses_reference_without_analysis():
    async def check():
        async with runtime() as (c, backend):
            await c.application.execute(request("解读最近训练"))
            backend.calls.clear()
            backend.overrides["generate_irego_single_session_report"] = report_response
            run = await c.application.execute(request("把刚才那次生成图", "r2"))
            assert [n for n, _ in backend.calls] == ["generate_irego_single_session_report"]
            assert backend.calls[0][1]["session_ref"] == "synthetic-session"
            assert any(e.type == "artifact_ready" for e in run.record.events)
            assert run.record.status == "succeeded"

    asyncio.run(check())


def test_previous_relative_to_anchor_not_latest_usable():
    async def check():
        async with runtime() as (c, backend):
            await c.application.execute(request("解读最近训练"))
            history = demo_response("get_irego_patient_history", {})
            current = history["data"]["items"][0]
            history["data"]["items"] = [
                {**current, "session_ref": "newest", "session_time": "2026-09-03T10:00:00+08:00"},
                current,
                {**current, "session_ref": "previous", "session_time": "2026-08-31T10:00:00+08:00"},
            ]
            backend.overrides["get_irego_patient_history"] = history
            run = await c.application.execute(request("那上一次呢", "r2"))
            assert run.record.status == "succeeded"
            assert backend.calls[-1][1]["session_ref"] == "previous"

    asyncio.run(check())


def test_accepted_and_facts_arrive_before_slow_report():
    async def check():
        async with runtime() as (c, backend):
            backend.gates["generate_irego_single_session_report"] = asyncio.Event()
            backend.overrides["generate_irego_single_session_report"] = report_response
            run = await c.application.start(request("解读最近训练并生成报告图片"))
            first = await asyncio.wait_for(run.emitter.queue.get(), 1)
            assert first.type == "accepted"
            while True:
                event = await asyncio.wait_for(run.emitter.queue.get(), 1)
                if event.type == "answer_part" and "0.5 m/s" in event.payload["text"]:
                    break
            assert not run.task.done()
            assert not any(e.type == "artifact_ready" for e in run.record.events)
            backend.gates["generate_irego_single_session_report"].set()
            await run.task
            assert run.record.status == "succeeded"
            assert run.record.events[-1].type == "completed"

    asyncio.run(check())


def test_report_failure_keeps_other_goals_and_facts():
    async def check():
        async with runtime() as (c, backend):
            run = await c.application.execute(request("查询患者信息，解读最近训练并生成报告图片"))
            assert run.record.status == "partial"
            assert run.record.goal_statuses == {"g1": "succeeded", "g2": "partial"}
            text = answers(run.record)
            assert "合成演示资料" in text and "0.5 m/s" in text
            assert not any(e.type == "artifact_ready" for e in run.record.events)

    asyncio.run(check())


@pytest.mark.parametrize("count", [3, 6])
def test_action_occurrences_are_not_deduplicated_or_capped_at_five(count):
    async def check():
        async with runtime(scene_actions_enabled=True) as (c, backend):
            backend.overrides["navigate_scene"] = scene_response
            query = "，".join(
                ["打开面板", "关闭面板", "打开面板", "关闭面板", "打开面板", "关闭面板"][:count]
            )
            run = await c.application.execute(request(query))
            actions = [e for e in run.record.events if e.type == "action_ready"]
            assert len(actions) == count
            assert [e.payload["command_order"] for e in actions] == list(range(1, count + 1))
            assert len({e.payload["action_id"] for e in actions}) == count
            assert len(backend.calls) == 1
            assert run.record.status == "succeeded"
            again = await c.application.execute(request(query))
            assert again.reused and len(backend.calls) == 1
            assert again.emitter.queue.empty()

    asyncio.run(check())


def test_ambiguous_middle_action_does_not_block_independent_last():
    async def check():
        async with runtime(scene_actions_enabled=True) as (c, backend):

            def body(payload):
                data = scene_response(payload)
                data["data"]["commands"][1].update(status="ambiguous", result=None)
                return data

            backend.overrides["navigate_scene"] = body
            run = await c.application.execute(request("打开面板，关闭面板，打开面板"))
            assert run.record.goal_statuses == {
                "g1": "succeeded",
                "g2": "clarification",
                "g3": "succeeded",
            }
            assert [e.goal_id for e in run.record.events if e.type == "action_ready"] == [
                "g1",
                "g3",
            ]
            assert run.record.status == "partial"

    asyncio.run(check())


@pytest.mark.parametrize("change", ["version", "destination"])
def test_scene_snapshot_change_stops_stale_actions(change):
    async def check():
        async with runtime(scene_actions_enabled=True) as (c, backend):

            def body(payload):
                data = scene_response(payload)
                if change == "version":
                    data["data"]["version"] = 8
                else:
                    data["data"]["commands"][0].update(intentType="telepor", result="[Telepor:]1")
                return data

            backend.overrides["navigate_scene"] = body
            run = await c.application.execute(request("打开面板，关闭面板"))
            assert len([e for e in run.record.events if e.type == "action_ready"]) == (
                0 if change == "version" else 1
            )
            assert run.record.goal_statuses["g2"] == "clarification"

    asyncio.run(check())


@pytest.mark.parametrize("count", [0, 1])
def test_guard_uses_doctor_query_result(count):
    async def check():
        async with runtime(scene_actions_enabled=True) as (c, backend):
            query = "查询医生，如果有医生就打开面板"
            c.application.planner = SeedPlanner(
                [
                    Goal(
                        goal_id="g1", kind="doctor_query", domain="doctors", query_span="查询医生"
                    ),
                    Goal(
                        goal_id="g2",
                        kind="scene_action",
                        domain="scene",
                        query_span="打开面板",
                        output="action",
                        condition=Condition(text="有医生", source_goal_ids=["g1"]),
                    ),
                ]
            )
            backend.overrides["navigate_scene"] = scene_response
            backend.overrides["search_doctors"] = {
                "status": 200,
                "data": {
                    "count": count,
                    "doctorNames": "合成医生" if count else "",
                    "command": "[DoctorRecommend:]123",
                },
            }
            run = await c.application.execute(request(query))
            assert len([e for e in run.record.events if e.type == "action_ready"]) == count
            assert "DoctorRecommend" not in answers(run.record)
            if not count:
                assert [n for n, _ in backend.calls] == ["search_doctors"]

    asyncio.run(check())


@pytest.mark.parametrize("status", ["unavailable", "failed"])
def test_http_success_business_failure_is_not_no_training(status):
    async def check():
        async with runtime() as (c, backend):
            body = demo_response("get_multisource_patient_context", {})
            body.update(status=status, data={}, patient_message="合成业务暂不可用。")
            backend.overrides["get_multisource_patient_context"] = body
            run = await c.application.execute(request("查询患者信息"))
            assert next(iter(run.record.results.values())).code == "tool_" + status
            assert "合成业务暂不可用" in answers(run.record)
            assert "没有训练" not in answers(run.record)

    asyncio.run(check())


@pytest.mark.parametrize("expired", [True, False])
def test_expired_or_unreachable_artifact_never_emitted(expired):
    async def check():
        async with runtime() as (c, backend):
            body = report_response({})
            if expired:
                body["data"]["expires_at"] = "2000-01-01T00:00:00+00:00"
            else:
                backend.artifact_ok = False
            backend.overrides["generate_irego_single_session_report"] = body
            run = await c.application.execute(request("生成最近训练报告图片"))
            assert not any(e.type == "artifact_ready" for e in run.record.events)
            assert any(r.code == "artifact_unavailable" for r in run.record.results.values())

    asyncio.run(check())


def test_zero_missing_unknown_unit_remain_distinct():
    async def check():
        async with runtime() as (c, backend):
            body = demo_response("get_irego_session_analysis", {})
            metric = body["data"]["report_blocks"][0]["metrics"][0]
            metric.update(normalized_value=0, unit_status="unknown")
            body["data"]["overview"]["total_training_duration"] = None
            backend.overrides["get_irego_session_analysis"] = body
            run = await c.application.execute(request("解读最近训练"))
            text = answers(run.record)
            assert "0（单位未知）" in text and "训练总时长：缺失" in text
            assert "0 m/s" not in text

    asyncio.run(check())


def test_read_retry_once_artifact_never_retried():
    async def check():
        async with runtime() as (c, backend):
            backend.overrides["get_multisource_patient_context"] = DomainError(
                "http_503", "合成暂时故障", retryable=True
            )
            await c.application.execute(request("查询患者信息"))
            assert len(backend.calls) == 2
            backend.calls.clear()
            backend.overrides["generate_irego_single_session_report"] = DomainError(
                "http_503", "合成暂时故障", retryable=True
            )
            await c.application.execute(request("生成最近训练报告图片", "r2"))
            assert len([n for n, _ in backend.calls if n.startswith("generate")]) == 1

    asyncio.run(check())


def test_cancel_stops_inflight_tools_and_completes_status():
    async def check():
        async with runtime() as (c, backend):
            endpoint = "get_multisource_patient_context"
            backend.gates[endpoint] = asyncio.Event()
            backend.entered[endpoint] = asyncio.Event()
            req = request("查询患者信息")
            run = await c.application.start(req)
            await asyncio.wait_for(backend.entered[endpoint].wait(), 1)
            await c.application.cancel(req.scope.scope_hash, run.record.run_id)
            assert endpoint in backend.cancelled
            assert run.record.status == "cancelled"
            assert run.record.events[-1].type == "completed"
            assert all(r.status == "cancelled" for r in run.record.results.values())

    asyncio.run(check())


def test_total_deadline_includes_request_queue():
    async def check():
        async with runtime(request_timeout_seconds=0.1, max_concurrent_requests=1) as (c, backend):
            await c.application.request_limiter.acquire()
            run = await c.application.start(request("查询患者信息"))
            await run.task
            c.application.request_limiter.release()
            assert not backend.calls and run.record.status == "failed"
            assert run.record.events[-1].type == "completed"

    asyncio.run(check())


def test_checkpoint_contains_only_ids_and_stage():
    async def check():
        async with runtime() as (c, backend):
            run = await c.application.execute(request("查询患者信息"))
            config = {"configurable": {"thread_id": run.record.run_id}}
            async for snapshot in c.graph.aget_state_history(config):
                assert set(snapshot.values) <= {"run_id", "stage"}
                assert "合成演示资料" not in str(snapshot.values)

    asyncio.run(check())
