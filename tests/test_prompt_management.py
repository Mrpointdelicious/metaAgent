"""
创建日期：2026-10-09
文件功能：验证提示词迁移等价、严格输入、运行版本绑定与调用追踪。
"""

import asyncio
import hashlib
import json
from dataclasses import FrozenInstanceError
from datetime import datetime

import pytest
from langchain_core.messages import AIMessage
from pydantic import ValidationError

from meta_agent.context.budget import LLMBudget
from meta_agent.contracts import ConversationState, IntentDecision
from meta_agent.events.native import SnapshotPurpose
from meta_agent.infrastructure.container import create_container
from meta_agent.planning.parser import StructuredIntentPlanner
from meta_agent.prompts import catalog
from meta_agent.prompts.contracts import (
    AgentPromptInputs,
    EmptyPromptInputs,
    FactSelectionPromptInputs,
    IntentPromptInputs,
)
from meta_agent.prompts.service import PromptService
from tests.helpers.runtime import request, runtime
from tests.helpers.settings import AppTestSettings
from tests.test_single_agent import SCOPE, activate, tool_call


@pytest.mark.parametrize(
    "profile,exhausted,digest",
    [
        (None, False, "39d94bcbcc2c992b25e9c1f3bd9eadf8f5bdf779697706f72bea253a66eb7072"),
        (
            {"note": "保留 {now} 与 {{字段}}"},
            True,
            "6975b94aff92106c9ce2dfe82b050e1a3ce6a24977acb62097d6e48135df2db8",
        ),
    ],
)
def test_agent_messages_match_pre_migration_golden(profile, exhausted, digest):
    # Hashes captured from the original inline text and original concatenation order.
    prompts = PromptService()
    rendered = prompts.render(
        prompts.bind(),
        "rehab.agent",
        AgentPromptInputs(
            now=datetime.fromisoformat("2026-10-09T08:00:00+00:00"),
            patient_profile=profile,
            budget_exhausted=exhausted,
        ),
    )
    assert hashlib.sha256(rendered.system_message.content.encode()).hexdigest() == digest
    assert [role for role, _ in rendered.messages] == ["system"]
    assert ("budget_exhausted@v1" in rendered.fragments) == exhausted
    assert ("patient_profile@v1" in rendered.fragments) == bool(profile)
    assert "保留" not in json.dumps(rendered.usage(1), ensure_ascii=False)


def test_intent_and_fact_messages_preserve_roles_and_literal_data():
    prompts = PromptService()
    binding = prompts.bind()
    inputs = IntentPromptInputs(
        query="保留 {query}",
        recent_turns=[],
        record_candidates=[],
        record_domain=None,
        history_domain=None,
        history_page=None,
        pending_goals=[],
        patient_brief="{patient}",
    )
    rendered = prompts.render(binding, "intent.parse", inputs)
    assert hashlib.sha256(rendered.system_message.content.encode()).hexdigest() == (
        "a89493171820efd1d147ce644ef0a62390692d5ca82d22b3e94a777fa5bba19c"
    )
    assert json.loads(rendered.messages[1][1]) == inputs.model_dump()
    assert rendered.messages[1][0] == "human"
    fact = prompts.render(
        binding,
        "answer.fact_select",
        FactSelectionPromptInputs(
            query="查询",
            facts=[{"value": "{value}"}],
        ),
    )
    assert fact.messages == (
        ("system", "只选择相关事实编号，禁止创建或改写事实。"),
        ("human", '{"query": "查询", "facts": [{"value": "{value}"}]}'),
    )
    repair = prompts.render(binding, "intent.repair", EmptyPromptInputs())
    assert repair.messages == (("human", "结构不合法，请完整按给定Schema重新生成。"),)


def test_prompt_binding_and_inputs_fail_explicitly():
    prompts = PromptService()
    binding = prompts.bind()
    with pytest.raises(FrozenInstanceError):
        binding.bundle = "changed"
    with pytest.raises(ValueError, match="Unknown prompt bundle"):
        prompts.bind("builtin-v999")
    with pytest.raises(ValueError, match="Unknown prompt ID"):
        prompts.render(binding, "unknown", EmptyPromptInputs())
    with pytest.raises(TypeError, match="AgentPromptInputs"):
        prompts.render(binding, "rehab.agent", EmptyPromptInputs())
    with pytest.raises(ValidationError):
        AgentPromptInputs.model_validate({"patient_profile": {}})
    with pytest.raises(ValidationError):
        EmptyPromptInputs.model_validate({"template_path": "unregistered.md"})


def test_startup_rejects_unavailable_prompt_bundle():
    async def check():
        with pytest.raises(ValueError, match="Unknown prompt bundle"):
            await create_container(AppTestSettings(prompt_bundle="missing"))

    asyncio.run(check())


def test_catalog_rejects_missing_resources_and_unknown_versions(monkeypatch, tmp_path):
    with monkeypatch.context() as patch:
        patch.setattr(catalog, "files", lambda _: tmp_path)
        with pytest.raises(FileNotFoundError):
            PromptService()
    monkeypatch.setattr(catalog, "BUNDLES", {"broken": (("rehab.agent", "v999"),)})
    with pytest.raises(ValueError, match="Unknown prompt version"):
        PromptService()


def test_catalog_can_release_new_version_without_changing_existing_binding(monkeypatch, tmp_path):
    original = PromptService()
    old_binding = original.bind()
    source = catalog.files("meta_agent.prompts").joinpath("templates")
    destination = tmp_path / "templates"
    destination.mkdir()
    for resource in source.iterdir():
        (destination / resource.name).write_text(
            resource.read_text(encoding="utf-8"), encoding="utf-8"
        )
    (destination / "agent_system.v2.md").write_text("新版固定规则。", encoding="utf-8")
    manifest = dict(catalog.MANIFEST)
    inputs, schema, fragments = manifest["rehab.agent", "v1"]
    manifest["rehab.agent", "v2"] = (
        inputs,
        schema,
        tuple((name, "v2" if name == "agent_system" else version) for name, version in fragments),
    )
    monkeypatch.setattr(catalog, "MANIFEST", manifest)
    monkeypatch.setattr(
        catalog,
        "BUNDLES",
        {
            **catalog.BUNDLES,
            "builtin-v2": (("rehab.agent", "v2"),),
        },
    )
    monkeypatch.setattr(catalog, "files", lambda _: tmp_path)
    updated = PromptService()
    values = AgentPromptInputs(now=datetime.fromisoformat("2026-10-09T08:00:00+00:00"))
    old = updated.render(old_binding, "rehab.agent", values)
    new = updated.render(updated.bind("builtin-v2"), "rehab.agent", values)
    assert old.messages == original.render(old_binding, "rehab.agent", values).messages
    assert new.system_message.content.startswith("新版固定规则。")
    assert old.template_hash != new.template_hash
    assert new.fragments == ("agent_system@v2", "agent_time@v1")


def test_planner_retry_reuses_binding_and_records_both_instructions():
    class Model:
        def __init__(self):
            self.calls = []

        def with_structured_output(self, schema, **kwargs):
            assert schema is IntentDecision
            return self

        async def ainvoke(self, messages):
            self.calls.append(list(messages))
            if len(self.calls) == 1:
                return None
            return IntentDecision(decision="respond")

    async def check():
        prompts, model, usage = PromptService(), Model(), []
        binding = prompts.bind()
        planner = StructuredIntentPlanner(model, AppTestSettings(prompt_bundle="changed"), prompts)
        result = await planner.parse(
            "你好",
            ConversationState(),
            LLMBudget(),
            prompt_binding=binding,
            prompt_usages=usage,
        )
        assert result.decision == "respond"
        assert model.calls[0] == model.calls[1][:2]
        assert len(model.calls[1]) == 3
        assert [(u["call"], u["prompt_id"]) for u in usage] == [
            (1, "intent.parse"),
            (2, "intent.parse"),
            (2, "intent.repair"),
        ]

    asyncio.run(check())


def test_agent_pins_bundle_and_keeps_profile_out_of_metrics():
    async def check():
        async with runtime() as (container, backend):
            activate(
                container, [tool_call("get_patient_consultation"), AIMessage("已有就诊记录。")]
            )
            run = await container.application.start(request("病史", scope=SCOPE))
            container.settings.prompt_bundle = "next-unpublished-version"
            await run.task
            assert run.record.status == "succeeded"
            assert run.context.patient_profile_context
            assert "patient_profile_context" not in run.record.metrics
            assert run.record.metrics["prompt_bundle"] == "builtin-v1"
            usages = run.record.metrics["prompt_usages"]
            assert [u["call"] for u in usages] == [1, 2]
            assert len({u["template_hash"] for u in usages}) == 1
            assert all(u["prompt_id"] == "rehab.agent" for u in usages)
            assert len(run.record.metrics["agent_tool_sets"]) == 2
            # Even old persisted metrics are redacted by the shared projection.
            run.record.metrics["patient_profile_context"] = {"private": "fixture"}
            snapshot = container.output_adapter.snapshot(run.record, purpose=SnapshotPurpose.STATUS)
            assert "patient_profile_context" not in snapshot["metrics"]

    asyncio.run(check())
