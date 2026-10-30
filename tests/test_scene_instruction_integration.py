"""
创建日期：2026-10-30
文件功能：验证服务端指令来源、多动作、版本、幂等和交付状态隔离。
"""

import asyncio
from dataclasses import replace

import pytest

from meta_agent.agent.tools import invoke_tool, spec
from meta_agent.domains.scene import SceneAdapter
from meta_agent.events.dify import DifyEventAdapter
from tests.helpers.integration import context
from tests.helpers.runtime import runtime


def scene_body(spans, statuses=None, *, version=7, codes=None):
    statuses = statuses or ["matched"] * len(spans)
    return {
        "status": 200,
        "data": {
            "spaceId": "space",
            "version": version,
            "commands": [
                {
                    "order": i + 1,
                    "sourceText": text,
                    "status": status,
                    "result": (codes[i] if codes else f"[ClickPoint:]{i}")
                    if status == "matched"
                    else None,
                    "candidates": [{"name": "A", "score": 0.9}, {"name": "B", "score": 0.9}]
                    if status == "ambiguous"
                    else [],
                }
                for i, (text, status) in enumerate(zip(spans, statuses, strict=True))
            ],
        },
    }


@pytest.mark.parametrize("phrase", ["去二楼", "打开个人中心", "切换视角", "回到入口", "去上一层"])
def test_scene_phrases_use_fixed_backend_without_sql_or_local_command_ids(phrase):
    async def check():
        async with runtime(scene_actions_enabled=True) as (container, backend):
            backend.overrides["navigate_scene"] = scene_body([phrase])
            ctx = context(container, phrase)
            result = await invoke_tool(ctx, "navigate_scene", {"target": phrase})
            assert result["status"] == "succeeded"
            assert backend.calls == [
                (
                    "navigate_scene",
                    {
                        "target": phrase,
                        "spaceId": "space",
                        "expectedSceneVersion": 7,
                    },
                )
            ]
            assert len(ctx.record.action_delivery) == 1
            assert set(ctx.record.action_delivery.values()) == {"delivery_unknown"}
            before = dict(ctx.record.action_delivery)
            DifyEventAdapter().message(ctx.record)
            assert ctx.record.action_delivery == before

    asyncio.run(check())


def test_multi_action_spans_come_from_server_and_have_distinct_idempotent_delivery_ids():
    async def check():
        async with runtime(scene_actions_enabled=True) as (container, backend):
            query = "打开个人中心然后切换视角"
            backend.overrides["navigate_scene"] = scene_body(["打开个人中心", "切换视角"])
            ctx = context(container, query)
            result = await invoke_tool(ctx, "navigate_scene", {"target": query})
            assert result["status"] == "succeeded"
            actions = [event for event in ctx.record.events if event.type == "action_ready"]
            assert [event.payload["command_order"] for event in actions] == [1, 2]
            assert len({event.payload["action_id"] for event in actions}) == 2
            stored = next(
                result for result in ctx.record.results.values() if result.outputs.get("actions")
            )
            gid, ref = next(iter(stored.outputs["action_ref"].items()))
            outcome = await SceneAdapter().dispatch(
                spec(ctx, "navigate_scene", "retry", "scene.dispatch", {}, [gid]),
                ref,
                ctx,
            )
            assert outcome.code == "delivery_unknown" and len(ctx.record.action_delivery) == 2

    asyncio.run(check())


def test_teleport_invalidates_following_action_until_scene_refresh():
    async def check():
        async with runtime(scene_actions_enabled=True) as (container, backend):
            query = "去二楼然后打开训练页面"
            backend.overrides["navigate_scene"] = scene_body(
                ["去二楼", "打开训练页面"], codes=["[Telepor:]1", "[ClickPoint:]2"]
            )
            ctx = context(container, query)
            result = await invoke_tool(ctx, "navigate_scene", {"target": query})
            assert result["status"] == "partial" and len(ctx.record.action_delivery) == 1
            assert result["data"]["dispatches"][1]["code"] == "scene_context_changed"

    asyncio.run(check())


@pytest.mark.parametrize("status", ["ambiguous", "unmatched"])
def test_equal_candidates_and_missing_points_never_become_executable(status):
    async def check():
        async with runtime(scene_actions_enabled=True) as (container, backend):
            phrase = "打开不存在的点位"
            backend.overrides["navigate_scene"] = scene_body([phrase], [status])
            ctx = context(container, phrase)
            result = await invoke_tool(ctx, "navigate_scene", {"target": phrase})
            assert result["status"] == "partial" and not ctx.record.action_delivery
            assert result["data"]["dispatches"][0]["code"] == status

    asyncio.run(check())


@pytest.mark.parametrize(
    "version,spans,expected",
    [
        (8, ["打开个人中心"], "scene_version_mismatch"),
        (7, [], "scene_catalog_unavailable"),
        (7, ["关闭个人中心"], "command_alignment"),
    ],
)
def test_stale_empty_and_forged_scene_results_fail_closed(version, spans, expected):
    async def check():
        async with runtime(scene_actions_enabled=True) as (container, backend):
            backend.overrides["navigate_scene"] = scene_body(spans, version=version)
            ctx = context(container, "打开个人中心")
            result = await invoke_tool(ctx, "navigate_scene", {"target": ctx.query})
            if expected == "scene_catalog_unavailable":
                assert next(iter(ctx.record.results.values())).code == expected
            else:
                assert result["code"] == expected
            assert not ctx.record.action_delivery

    asyncio.run(check())


def test_resolution_is_read_only_and_dispatch_rechecks_evidence_and_version():
    async def check():
        async with runtime(scene_actions_enabled=True) as (container, backend):
            ctx = context(container, "打开个人中心")
            backend.overrides["navigate_scene"] = scene_body([ctx.query])
            result = await invoke_tool(ctx, "resolve_scene_instruction", {"target": ctx.query})
            assert result["status"] == "succeeded" and not ctx.record.action_delivery
            stored = next(iter(ctx.record.results.values()))
            gid, ref = next(iter(stored.outputs["action_ref"].items()))
            ctx.scope = replace(ctx.scope, scene_version=8)
            with pytest.raises(Exception, match="刷新当前场景版本"):
                await SceneAdapter().dispatch(
                    spec(ctx, "navigate_scene", "d", "scene.dispatch", {}, [gid]), ref, ctx
                )
            assert not ctx.record.action_delivery

    asyncio.run(check())


def test_matched_command_does_not_become_delivered_when_transport_fails():
    from meta_agent.contracts import DomainError

    async def check():
        async with runtime(scene_actions_enabled=True) as (container, backend):
            ctx = context(container, "打开个人中心")
            backend.overrides["navigate_scene"] = scene_body([ctx.query])
            emit = ctx.emitter.emit

            async def fail_delivery(kind, *args, **kwargs):
                if kind == "action_ready":
                    raise DomainError("delivery_failed", "合成交付失败。")
                return await emit(kind, *args, **kwargs)

            ctx.emitter.emit = fail_delivery
            result = await invoke_tool(ctx, "navigate_scene", {"target": ctx.query})
            assert result["status"] == "partial" and not ctx.record.action_delivery
            assert next(iter(result["data"]["actions"].values()))["status"] == "matched"
            assert result["data"]["dispatches"][0]["code"] == "delivery_failed"

    asyncio.run(check())
