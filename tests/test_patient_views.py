"""
创建日期：2026-10-11
文件功能：验证患者契约、原文完整性、证据范围和当前能力状态。
"""

import asyncio
import json
from copy import deepcopy

import pytest
from langchain_core.messages import AIMessage, ToolMessage

from meta_agent.agent.context import compact_result
from meta_agent.contracts import DomainError, fingerprint
from meta_agent.domains.knowledge_citations import audit_remote_answer
from meta_agent.domains.patient_views import business_context, project_patient
from meta_agent.orchestration.identity import TrustedScope
from tests.helpers.runtime import answers, request, runtime
from tests.test_remote_knowledge import context
from tests.test_single_agent import activate, tool_call
from tools.business_eval import load_frozen
from tools.contract_fixtures import FIXTURES, ContractBackend

SCOPE = TrustedScope("business-eval", "reviewer", project_patient_id="900001")
PAYLOAD = {"system_context": {"user": "900001"}}


@pytest.mark.parametrize(
    "fixture,state", [("rx_empty", "empty"), ("rx_null", "null"), ("rx_present", "present")]
)
def test_field_states_preserve_actual_source_and_scope(fixture, state):
    async def check():
        async with runtime() as (c, _):
            raw = await ContractBackend(fixture).post("get_patient_consultation", PAYLOAD)
            original = deepcopy(raw)
            ev = await c.repository.save_evidence(
                SCOPE.scope_hash, "get_patient_consultation", "r", raw, raw["contract_version"]
            )
            body, facts = project_patient("get_patient_consultation", ev)
            assert body["interpretation"]["field_states"][0]["state"] == state
            assert "data" not in body
            assert raw == original and fingerprint(ev.payload) == ev.raw_payload_hash
            if state == "present":
                assert any(v.fact.value == "合成训练处方甲" for v in facts)
                assert any("不得自行增加组数" in str(v.fact.value) for v in facts)
                assert all(v.fact.path.startswith("/data/medical_records/0/") for v in facts)

    asyncio.run(check())


def test_omitted_optional_prescriptions_are_unknown_not_empty():
    async def check():
        async with runtime() as (c, _):
            raw = await ContractBackend("rx_empty").post("get_patient_consultation", PAYLOAD)
            del raw["data"]["medical_records"][0]["prescriptions"]
            ev = await c.repository.save_evidence(
                SCOPE.scope_hash, "get_patient_consultation", "r", raw, "v1"
            )
            body, _ = project_patient("get_patient_consultation", ev)
            assert body["interpretation"]["field_states"][0]["state"] == "not_returned"

    asyncio.run(check())


def test_malformed_response_is_not_delivered_as_absent_patient_data():
    async def check():
        async with runtime(business_grounding_enabled=True) as (c, _):
            c.application.backend = ContractBackend("malformed")
            activate(
                c,
                [tool_call("get_patient_consultation"), AIMessage("接口异常，不能判断记录内容。")],
            )
            run = await c.application.execute(request("查处方", scope=SCOPE))
            result = next(
                r
                for r in run.record.results.values()
                if r.outputs["tool_name"] == "get_patient_consultation"
            )
            assert result.code == "invalid_patient_contract" and result.status == "unavailable"
            assert not result.facts
            memory = await c.repository.conversation(SCOPE.thread_id("c1"))
            assert "invalid-list-type" not in json.dumps(memory.agent_messages)

    asyncio.run(check())


def test_clinical_long_text_never_loses_negative_tail_as_positive_prefix():
    text = "本条记录的合成详细说明。" * 300 + "不得自行增加训练强度。"
    body = {
        "tool_name": "get_patient_consultation",
        "evidence_id": "test",
        "business_view": True,
        "status": "succeeded",
        "facts": [{"label": "医嘱", "value": text}],
        "interpretation": {"limits": ["原文不完整不能解释"]},
    }
    compact = compact_result(body, 512)
    encoded = json.dumps(compact, ensure_ascii=False)
    assert text in encoded or "本条记录的合成详细说明" not in encoded
    assert compact["context_truncated"] is True
    assert body["facts"][0]["value"] == text


def test_rehab_does_not_interpret_unknown_plan_flags_as_adherence():
    async def check():
        async with runtime() as (c, _):
            raw = await ContractBackend("flag_conflict").post("get_patient_rehab", PAYLOAD)
            ev = await c.repository.save_evidence(
                SCOPE.scope_hash, "get_patient_rehab", "r", raw, "v1"
            )
            body, facts = project_patient("get_patient_rehab", ev)
            assert any(v.fact.value == 1 and "未定义" in v.label for v in facts)
            assert any("不能说训练按计划进行" in s for s in body["interpretation"]["limits"])

    asyncio.run(check())


def test_page_capability_requires_valid_scoped_unexpired_evidence():
    async def check():
        async with runtime() as (c, _):
            ctx = context(c, SCOPE)
            raw = {"status": "success", "data": {"page": {"has_next": False, "page_number": 2}}}
            ev = await c.repository.save_evidence(
                SCOPE.scope_hash, "get_iretour_patient_history", "r", raw, "v1"
            )
            message = ToolMessage(
                content=json.dumps(
                    {"tool_name": "get_iretour_patient_history", "evidence_id": ev.evidence_id}
                ),
                tool_call_id="t",
            )
            first = await business_context(ctx, [message], {"search_knowledge"})
            assert first["pages"]["get_iretour_patient_history"]["has_next"] is False
            assert first["iretour_single_report_enabled"] is False
            assert first["patient_queries_available"] == []
            available = await business_context(ctx, [], {"get_patient_rehab", "search_knowledge"})
            assert available["patient_queries_available"] == ["get_patient_rehab"]
            assert (await business_context(ctx, [], {"generate_iretour_single_session_report"}))[
                "iretour_single_report_enabled"
            ]
            await c.repository.delete("evidence", SCOPE.scope_hash, ev.evidence_id)
            assert not (await business_context(ctx, [message], set()))["pages"]
            ctx.scope = TrustedScope("other", "reviewer", project_patient_id="900001")
            assert not (await business_context(ctx, [message], set()))["pages"]

    asyncio.run(check())


def test_contract_fixtures_are_frozen_and_mark_robustness_separately():
    manifest, pins = load_frozen(FIXTURES)
    assert len(manifest["cases"]) == 28
    assert len([c for c in manifest["cases"] if c["case_id"].startswith("C")]) == 16
    assert "source-pins.json" in pins and "partial_corpus.json" in pins
    assert manifest["clinical_approval"] is False


def test_required_shape_cannot_be_silently_defaulted_to_no_records():
    async def check():
        async with runtime() as (c, _):
            raw = await ContractBackend("rx_empty").post("get_patient_consultation", PAYLOAD)
            del raw["data"]["medical_records"]
            ev = await c.repository.save_evidence(
                SCOPE.scope_hash, "get_patient_consultation", "r", raw, "v1"
            )
            with pytest.raises(DomainError, match="接口结构"):
                project_patient("get_patient_consultation", ev)

    asyncio.run(check())


def test_capability_summary_matches_tools_removed_at_last_model_budget():
    async def check():
        async with runtime(business_grounding_enabled=True, agent_max_model_calls=2) as (c, _):
            c.application.backend = ContractBackend("rx_empty")
            activate(
                c,
                [tool_call("get_patient_consultation"), AIMessage("根据本次已返回的医嘱回答。")],
            )
            run = await c.application.execute(request("查医嘱", scope=SCOPE))
            state = run.record.metrics["business_contexts"][-1]
            assert run.record.metrics["agent_tool_sets"][-1]["names"] == []
            assert not state["knowledge_tool_available"]
            assert state["patient_queries_available"] == []
            assert not state["iretour_single_report_enabled"]

    asyncio.run(check())


def test_local_opt_in_audit_rejects_expired_citations_and_context_lists_only_fresh_refs():
    async def check():
        async with runtime(business_grounding_enabled=True) as (c, _):
            ctx = context(c, SCOPE)
            ev = await c.repository.save_evidence(
                SCOPE.scope_hash,
                "knowledge.search",
                "r",
                {"snippets": [{"doc_ref": "doc@v1#s"}]},
                "v1",
            )
            message = ToolMessage(
                content=json.dumps(
                    {"tool_name": "search_knowledge", "evidence_id": ev.evidence_id}
                ),
                tool_call_id="t",
            )
            assert (await audit_remote_answer("依据[doc@v1#s]", [message], ctx))[0] == ["doc@v1#s"]
            assert (await business_context(ctx, [message], set()))["knowledge_current_refs"] == [
                "doc@v1#s"
            ]
            await c.repository.delete("evidence", SCOPE.scope_hash, ev.evidence_id)
            with pytest.raises(DomainError, match="停止输出"):
                await audit_remote_answer("旧依据仍有效[doc@v1#s]", [message], ctx)
            expired = ToolMessage(
                content=json.dumps({"tool_name": "search_knowledge", "status": "expired"}),
                tool_call_id="old",
            )
            state = await business_context(ctx, [message, expired], set())
            assert state["knowledge_current_refs"] == []
            assert state["expired_knowledge_in_history"]

    asyncio.run(check())


def test_local_stale_draft_never_reaches_answer_events():
    async def check():
        scope = TrustedScope("business-eval", "reviewer")
        async with runtime(
            business_grounding_enabled=True, knowledge_corpus_path=str(FIXTURES / "corpus.json")
        ) as (c, _):
            activate(
                c,
                [
                    tool_call("search_knowledge", {"query": "步速定义", "domain": "health"}),
                    AIMessage("步速依据[tour-speed@v1#definition]。"),
                ],
            )
            first = await c.application.execute(request("步速定义", scope=scope))
            for result in first.record.results.values():
                for eid in result.evidence_ids:
                    await c.repository.delete("evidence", scope.scope_hash, eid)
            activate(c, [AIMessage("已重新核实，旧依据仍有效[tour-speed@v1#definition]。")])
            second = await c.application.execute(request("再核实", "r2", scope=scope))
            assert second.record.status == "failed"
            assert "旧依据仍有效" not in answers(second.record)
            assert second.record.metrics["knowledge_citation_audit"]["status"] == "failed"

    asyncio.run(check())
