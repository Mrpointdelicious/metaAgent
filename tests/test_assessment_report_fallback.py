import asyncio
from types import SimpleNamespace

import meta_agent.agent.runtime as runtime_module
from meta_agent.agent.runtime import SingleAgentRuntime


class _Result:
    def __init__(self, outputs, status="succeeded"):
        self.outputs = outputs
        self.status = status


def _ctx(*, enabled=True, results=None):
    return SimpleNamespace(
        query="生成最新的量表报表",
        settings=SimpleNamespace(assessment_reports_enabled=enabled),
        record=SimpleNamespace(run_id="run_fallback_test", results=results or {}),
    )


def test_assessment_report_fallback_runs_when_model_stops_early(monkeypatch):
    calls = []

    async def fake_invoke_tool(ctx, name, args):
        calls.append((name, args))
        if name == "get_assessment_result_analysis":
            ctx.record.results["analysis"] = _Result(
                {"tool_name": name, "assessment_ref": "assessment_old_with_score"}
            )
        elif name == "generate_assessment_single_report":
            ctx.record.results["report"] = _Result(
                {"tool_name": name, "artifact": {"url": "http://example.invalid/report.png"}},
                status="partial",
            )
        return {"status": "success"}

    monkeypatch.setattr(runtime_module, "invoke_tool", fake_invoke_tool)
    monkeypatch.setattr(runtime_module, "tool_allowed", lambda ctx, name: True)

    runtime = object.__new__(SingleAgentRuntime)
    answer = asyncio.run(runtime._ensure_assessment_report(_ctx(), "暂时生成不了报表。"))

    assert answer == "暂时生成不了报表。"
    assert calls == [
        ("get_assessment_result_analysis", {"selector": "latest_usable"}),
        (
            "generate_assessment_single_report",
            {"selector": "assessment_ref", "assessment_ref": "assessment_old_with_score"},
        ),
    ]


def test_assessment_report_fallback_skips_when_artifact_exists(monkeypatch):
    calls = []

    async def fake_invoke_tool(ctx, name, args):
        calls.append(name)
        return {}

    monkeypatch.setattr(runtime_module, "invoke_tool", fake_invoke_tool)
    monkeypatch.setattr(runtime_module, "tool_allowed", lambda ctx, name: True)

    results = {
        "done": _Result(
            {
                "tool_name": "generate_assessment_single_report",
                "artifact": {"url": "http://example.invalid/report.png"},
            }
        )
    }
    runtime = object.__new__(SingleAgentRuntime)
    asyncio.run(runtime._ensure_assessment_report(_ctx(results=results), "已生成。"))

    assert calls == []


def test_assessment_report_fallback_skips_when_disabled(monkeypatch):
    calls = []

    async def fake_invoke_tool(ctx, name, args):
        calls.append(name)
        return {}

    monkeypatch.setattr(runtime_module, "invoke_tool", fake_invoke_tool)
    monkeypatch.setattr(runtime_module, "tool_allowed", lambda ctx, name: True)

    runtime = object.__new__(SingleAgentRuntime)
    asyncio.run(runtime._ensure_assessment_report(_ctx(enabled=False), "能力未开启。"))

    assert calls == []
