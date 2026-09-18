"""
创建日期：2026-09-18
文件功能：IReGo 固定工作流回归——Planner 只给业务请求，内部链固定，依赖错误必须为零。
"""

import asyncio

import pytest

from meta_agent.contracts import Condition, Goal, IntentDecision, IReGoRequest
from tests.helpers.runtime import SeedPlanner, answers, report_response, request, runtime

DEPENDENCY_TEXT = ("任务依赖存在循环或缺失", "目标依赖无法解析", "invalid_dependency")


@pytest.mark.parametrize(
    ("query", "operation", "selector_mode", "selector_count", "need_artifact"),
    [
        ("解读最近训练并生成报告图片", "session", "latest_record", None, True),
        ("为我最近一次训练生成报告图片", "session", "latest_record", None, True),
        ("不要生成报告，只解释最近训练", "session", "latest_record", None, False),
        ("最近4次训练趋势并出图", "trend", "latest_count", 4, True),
        ("查看患者概况", "overview", "latest_record", None, False),
        ("查看训练历史", "history", "latest_record", None, False),
    ],
)
def test_irego_request_cases(query, operation, selector_mode, selector_count, need_artifact):
    """Case A-F：Planner 只产生单个 irego.execute 业务请求，无任何依赖边。"""

    async def check():
        async with runtime() as (c, backend):
            run = await c.application.execute(request(query))
            assert len(run.record.plan.tasks) == 1
            task = run.record.plan.tasks[0]
            assert task.capability == "irego.execute"
            assert task.depends_on == [] and task.guards == [] and task.bindings == []
            parsed = IReGoRequest.model_validate(task.arguments)
            assert parsed.operation == operation
            assert parsed.selector.mode == selector_mode
            assert parsed.selector.count == selector_count
            assert parsed.need_artifact == need_artifact
            text = answers(run.record) + " ".join(
                e.payload.get("text", "") for e in run.record.events
            )
            assert not any(mark in text for mark in DEPENDENCY_TEXT)

    asyncio.run(check())


def test_case_a_full_chain_facts_then_artifact():
    """Case A：基础数据、事实与制品全部落地，答案先于制品可达。"""

    async def check():
        async with runtime() as (c, backend):
            backend.overrides["generate_irego_single_session_report"] = report_response
            run = await c.application.execute(request("解读最近训练并生成报告图片"))
            names = [n for n, _ in backend.calls]
            assert "get_irego_patient_history" in names
            assert "get_irego_session_analysis" in names
            assert "generate_irego_single_session_report" in names
            assert run.record.status == "succeeded"
            assert "0.5 m/s" in answers(run.record)
            result = next(iter(run.record.results.values()))
            assert result.facts and result.evidence_ids
            artifacts = [e for e in run.record.events if e.type == "artifact_ready"]
            assert len(artifacts) == 1
            assert artifacts[0].payload["artifact_ref"] == "artifact-synthetic"
            assert artifacts[0].payload["evidence_id"]

    asyncio.run(check())


def test_case_b_artifact_only_still_builds_base_facts():
    """Case B：只生成报告图片也必须先建立 session 分析与事实。"""

    async def check():
        async with runtime() as (c, backend):
            backend.overrides["generate_irego_single_session_report"] = report_response
            run = await c.application.execute(request("为我最近一次训练生成报告图片"))
            names = [n for n, _ in backend.calls]
            assert "get_irego_session_analysis" in names
            result = next(iter(run.record.results.values()))
            assert result.facts, "基础 Facts 必须建立"
            assert any(e.type == "artifact_ready" for e in run.record.events)

    asyncio.run(check())


def test_case_c_negation_never_calls_report_endpoint():
    """Case C：need_artifact=false 时不得调用报表端点。"""

    async def check():
        async with runtime() as (c, backend):
            run = await c.application.execute(request("不要生成报告，只解释最近训练"))
            assert not any(n.startswith("generate") for n, _ in backend.calls)
            assert not any(e.type == "artifact_ready" for e in run.record.events)
            assert "0.5 m/s" in answers(run.record)

    asyncio.run(check())


def test_report_failure_keeps_base_facts_partial():
    """报告失败不能抹掉基础事实：目标 partial 且事实仍在答案中。"""

    async def check():
        async with runtime() as (c, backend):
            run = await c.application.execute(request("解读最近训练并生成报告图片"))
            assert run.record.status == "partial"
            result = next(iter(run.record.results.values()))
            assert result.facts
            assert "0.5 m/s" in answers(run.record)
            assert not any(e.type == "artifact_ready" for e in run.record.events)

    asyncio.run(check())


def test_llm_declared_dependencies_are_ignored():
    """after_goal_ids 不再生成执行依赖，LLM 无权声明依赖边。"""

    async def check():
        async with runtime() as (c, backend):
            c.application.planner = SeedPlanner(
                [
                    Goal(
                        goal_id="g1",
                        kind="irego",
                        domain="irego",
                        query_span="解读最近训练",
                        after_goal_ids=["g2"],
                        irego=IReGoRequest(operation="session"),
                    )
                ]
            )
            run = await c.application.execute(request("解读最近训练"))
            assert run.record.status == "succeeded"
            task = run.record.plan.tasks[0]
            assert task.depends_on == [] and task.guards == []

    asyncio.run(check())


def test_non_scene_condition_is_conservatively_clarified():
    """非场景目标携带 LLM 条件时保守澄清，不生成 guard 图、不执行副作用。"""

    async def check():
        async with runtime() as (c, backend):
            c.application.planner = SeedPlanner(
                [
                    Goal(
                        goal_id="g1",
                        kind="irego",
                        domain="irego",
                        query_span="解读最近训练",
                        condition=Condition(text="有医生", source_goal_ids=["g1"]),
                        irego=IReGoRequest(operation="session"),
                    )
                ]
            )
            run = await c.application.execute(request("解读最近训练"))
            assert run.record.status == "clarification"
            assert backend.calls == []

    asyncio.run(check())


def test_case_g_complex_query_ten_runs_zero_dependency_errors():
    """Case G：同一复杂查询连续 10 次，依赖类澄清必须为 0。"""

    async def check():
        async with runtime() as (c, backend):
            decisions = []
            for i in range(10):
                run = await c.application.execute(
                    request("解读最近训练并生成报告图片", f"r-{i}", conversation=f"c-{i}")
                )
                text = " ".join(e.payload.get("text", "") for e in run.record.events)
                assert not any(mark in text for mark in DEPENDENCY_TEXT)
                assert not any(r.code == "invalid_dependency" for r in run.record.results.values())
                decisions.append(run.record.decision)
            # 确定性路径输出必须稳定
            dumps = [d.model_dump(mode="json") for d in decisions]
            assert all(d == dumps[0] for d in dumps)

    asyncio.run(check())


def test_planner_output_shape_is_business_level():
    """确定性 Planner 输出 IReGoRequest，而非内部工具步骤。"""

    async def check():
        async with runtime() as (c, backend):
            decision: IntentDecision = await c.application.planner.parse(
                "解读最近训练并生成报告图片", c.application.repository and None, None
            )
            goal = decision.goals[0]
            assert goal.kind == "irego"
            assert goal.irego and goal.irego.operation == "session"
            assert goal.irego.selector.mode == "latest_record"
            assert goal.irego.need_artifact is True
            assert goal.after_goal_ids == [] and goal.condition is None

    asyncio.run(check())
