"""
创建日期：2026-10-11
文件功能：验证业务评测判分、输入侧观测及状态/知识裁剪回归，不计为真实模型验收。
"""

import asyncio
import json
from copy import deepcopy

import pytest
from langchain_core.messages import AIMessage, ToolMessage

from meta_agent.agent.context import compact_result, fresh_history
from meta_agent.context.budget import estimate_tokens
from meta_agent.contracts import DomainError
from tests.helpers.runtime import request, runtime
from tests.test_remote_knowledge import context
from tests.test_single_agent import activate, tool_call
from tools.business_eval import (
    SyntheticBackend,
    VisibilityObserver,
    load_frozen,
    score_turn,
    session,
)


def knowledge_body(texts):
    return {
        "tool_name": "search_knowledge",
        "status": "succeeded",
        "evidence_id": "evidence-test",
        "facts": [
            {"label": f"资料{i}", "value": text, "unit": None, "value_status": "valid"}
            for i, text in enumerate(texts)
        ],
        "data": {"doc_refs": [f"doc-{i}@v1#section" for i in range(len(texts))]},
    }


def test_knowledge_budget_never_turns_a_negative_tail_into_a_positive_prefix():
    text = "这是合成背景说明。" * 150 + "该方案未提供任何试验结果，不能据此声称康复。"
    body = knowledge_body([text])
    compact = compact_result(body, 512)
    encoded = json.dumps(compact, ensure_ascii=False)
    assert "摘要截断" not in encoded
    assert text in encoded or "这是合成背景说明" not in encoded
    assert estimate_tokens(compact) <= 512
    assert compact.get("context_truncated") is True
    assert compact["status"] != "succeeded" if not compact.get("facts") else True
    assert body["facts"][0]["value"] == text


def test_knowledge_budget_keeps_refs_paired_with_whole_excerpts():
    texts = [f"合成来源{i}。" * 30 + f"来源{i}没有临床疗效数据。" for i in range(6)]
    compact = compact_result(knowledge_body(texts), 1000)
    assert estimate_tokens(compact) <= 1000
    assert compact.get("context_truncated") is True
    facts = compact.get("facts", [])
    refs = compact.get("data", {}).get("doc_refs", [])
    assert len(facts) == len(refs)
    for fact, ref in zip(facts, refs, strict=True):
        index = int(ref.split("@")[0].split("-")[-1])
        assert fact["value"] == texts[index]


def test_expired_knowledge_keeps_only_query_hints_and_no_stale_excerpt():
    async def check():
        async with runtime() as (c, _):
            ctx = context(c)
            ev = await c.repository.save_evidence(
                ctx.scope.scope_hash, "knowledge.search", "request", {"snippets": []}, "v1"
            )
            await c.repository.delete("evidence", ctx.scope.scope_hash, ev.evidence_id)
            args = {"query": "合成步速定义", "domain": "health"}
            old = ToolMessage(
                content=json.dumps(
                    {
                        "tool_name": "search_knowledge",
                        "evidence_id": ev.evidence_id,
                        "facts": [{"value": "旧的解释"}],
                        "data": {"arguments": args},
                    }
                ),
                tool_call_id="test",
            )
            result = json.loads((await fresh_history([old], ctx))[0].content)
            assert result["status"] == "expired"
            assert result["query_hint"] == args
            assert "旧的解释" not in json.dumps(result, ensure_ascii=False)

    asyncio.run(check())


def test_distinct_knowledge_queries_do_not_erase_a_success(tmp_path):
    corpus = {
        "schema_version": "1.0",
        "documents": [
            {
                "doc_id": "sample",
                "title": "资料",
                "domain": "health",
                "version": "v1",
                "source": "repo://test",
                "approved": True,
                "approved_by": "test",
                "sections": [{"section_id": "s", "title": "定义", "text": "合成步速资料。"}],
            }
        ],
    }
    path = tmp_path / "corpus.json"
    path.write_text(json.dumps(corpus, ensure_ascii=False), encoding="utf-8")

    async def check():
        async with runtime(knowledge_corpus_path=str(path)) as (c, _):
            activate(
                c,
                [
                    tool_call("search_knowledge", {"query": "步速", "domain": "health"}),
                    tool_call("search_knowledge", {"query": "未知", "domain": "product"}, "call-2"),
                    AIMessage("已找到步速资料，另外一项没有资料。"),
                ],
            )
            run = await c.application.execute(request("查资料"))
            assert run.record.status == "partial"
            results = list(run.record.results.values())
            assert results[0].outputs["arguments"]["domain"] == "health"
            assert results[1].outputs["arguments"]["domain"] == "product"
            assert [r.status for r in results] == ["succeeded", "unavailable"]

    asyncio.run(check())


def test_same_query_successful_retry_is_not_permanently_partial(monkeypatch):
    from meta_agent.contracts import TaskResult
    from meta_agent.domains.knowledge import KnowledgeAdapter

    calls = []

    async def flaky(self, task, args, ctx):
        calls.append(args)
        if len(calls) == 1:
            raise DomainError("rag_timeout", "受控超时", outcome="unavailable", retryable=True)
        return TaskResult(task_id=task.task_id)

    monkeypatch.setattr(KnowledgeAdapter, "execute", flaky)

    async def check():
        async with runtime() as (c, _):
            args = {"query": "合成步速", "domain": "health"}
            activate(
                c,
                [
                    tool_call("search_knowledge", args),
                    tool_call("search_knowledge", args, "call-2"),
                    AIMessage("重试成功。"),
                ],
            )
            run = await c.application.execute(request("查资料"))
            assert run.record.status == "succeeded"
            assert any(r.code == "rag_timeout" for r in run.record.results.values())

    asyncio.run(check())


@pytest.mark.parametrize(
    "ref_text,valid",
    [
        ("[tour-speed@v1#definition]", True),
        ("（tour-speed@v1#definition）", True),
        ("`tour-speed@v1#definition`", True),
        ("tour-speed@v1#definition-extra", False),
    ],
)
def test_citation_formats_do_not_match_other_ids(ref_text, valid):
    expected = {
        "required_patterns": [],
        "required_tools": [],
        "forbidden_tools": [],
        "allowed_statuses": ["succeeded"],
        "knowledge": "optional",
        "required_refs": ["tour-speed@v1#definition"],
    }
    row = {
        "answer": ref_text,
        "tool_results": [],
        "actual_status": "succeeded",
        "action_artifact_events": [],
        "knowledge_evidence": [],
        "model_inputs": [],
        "case_id": "test",
        "turn": 1,
    }
    checks = score_turn(expected, row)["checks"]
    assert next(c["passed"] for c in checks if c["kind"] == "answer_ref") == valid
    assert score_turn(expected, row)["passed"] is False  # Citation text alone is insufficient.


def test_frozen_pilot_has_12_cases_and_no_clinical_approval():
    manifest, _ = load_frozen()
    assert len(manifest["cases"]) == len({c["case_id"] for c in manifest["cases"]}) == 12
    assert manifest["clinical_approval"] is False


def test_local_rollback_source_is_validated_against_its_own_corpus():
    from tools.check_integration_evidence import valid_local_snippet

    corpus = {
        "documents": [
            {
                "doc_id": "local",
                "version": "v1",
                "title": "本地资料",
                "source": "repo://local",
                "approved": True,
                "approved_by": "test",
                "sections": [{"section_id": "s", "title": "章节", "text": "原文"}],
            }
        ]
    }
    snippet = {
        "doc_ref": "local@v1#s",
        "title": "本地资料",
        "source": "repo://local",
        "section": "章节",
        "text": "原文",
    }
    assert valid_local_snippet(snippet, corpus)
    for field in ["doc_ref", "title", "source", "section", "text"]:
        assert not valid_local_snippet({**snippet, field: "tampered"}, corpus)


def test_business_evidence_hash_and_source_text_are_independently_checked():
    from meta_agent.contracts import fingerprint
    from tools.check_business_eval import evidence_errors

    corpus = {
        "documents": [
            {
                "doc_id": "sample",
                "version": "v1",
                "title": "标题",
                "source": "repo://sample",
                "approved": True,
                "approved_by": "test",
                "sections": [{"section_id": "s", "title": "章节", "text": "没有结果"}],
            }
        ]
    }
    payload = {
        "snippets": [
            {
                "doc_ref": "sample@v1#s",
                "title": "标题",
                "source": "repo://sample",
                "section": "章节",
                "text": "没有结果",
            }
        ]
    }
    ev = {"payload": payload, "raw_payload_hash": fingerprint(payload)}
    assert not evidence_errors(ev, corpus)
    bad = deepcopy(ev)
    bad["payload"]["snippets"][0]["text"] = "有结果"
    assert "payload_hash_mismatch" in evidence_errors(bad, corpus)
    bad["raw_payload_hash"] = fingerprint(bad["payload"])
    assert evidence_errors(bad, corpus) == ["source_mapping:sample@v1#s"]


def test_coverage_uses_final_model_input_not_discarded_earlier_messages():
    expected = {
        "required_patterns": [],
        "required_tools": [],
        "forbidden_tools": [],
        "allowed_statuses": ["succeeded"],
        "knowledge": "optional",
        "required_refs": ["tour-speed@v1#definition"],
    }
    row = {
        "answer": "[tour-speed@v1#definition]",
        "tool_results": [],
        "actual_status": "succeeded",
        "action_artifact_events": [],
        "knowledge_evidence": [],
        "model_inputs": [
            {
                "tool_messages": [
                    {
                        "knowledge_excerpts": [
                            {
                                "doc_ref": "tour-speed@v1#definition",
                                "full_text_visible": True,
                                "reference_visible": True,
                            }
                        ]
                    }
                ]
            },
            {"tool_messages": []},
        ],
        "case_id": "test",
        "turn": 1,
    }
    assert score_turn(expected, row)["visible_refs"] == []


def test_unknown_units_apply_to_all_accessible_records_and_real_zero_is_distinct():
    for ref in [
        "iretour_s_v1_eval_latest",
        "iretour_s_v1_eval_previous",
        "iretour_s_v1_eval_older",
    ]:
        metric = session(ref, unknown_unit=True)["result"]["metrics"][0]
        assert metric["unit"] is None and metric["trend_eligible"] is False
    assert session(placeholder=True)["result"]["metrics"][0]["value_status"] == "placeholder"
    measured = deepcopy(session()["result"]["metrics"][0])
    measured["raw_value"] = measured["normalized_value"] = 0
    assert measured["value_status"] == "measured"


def test_synthetic_backend_has_no_network_or_unseeded_data_fallback():
    async def check():
        backend = SyntheticBackend("measured")
        with pytest.raises(DomainError, match="合成测试身份"):
            await backend.post("get_patient_profile", {"system_context": {"user": "other"}})
        with pytest.raises(DomainError, match="没有合成测试数据"):
            await backend.post("unexpected", {"system_context": {"user": "900001"}})

    asyncio.run(check())


@pytest.mark.parametrize(
    "markup",
    [
        '<｜｜DSML｜｜ calls><｜｜DSML｜｜ invoke name="search_knowledge">',
        '<tool_call>{"name":"search_knowledge"}</tool_call>',
    ],
)
def test_model_tool_markup_is_neither_displayed_nor_executed(markup):
    async def check():
        async with runtime() as (c, backend):
            activate(c, [AIMessage(markup)])
            run = await c.application.execute(request("解释资料"))
            assert run.record.status == "unavailable"
            assert not backend.calls
            assert not any(markup in e.payload.get("text", "") for e in run.record.events)

    asyncio.run(check())


def test_grounding_versions_are_pinned_and_original_prompt_stays_unchanged():
    from meta_agent.contracts import utcnow
    from meta_agent.prompts.contracts import AgentPromptInputs
    from meta_agent.prompts.service import PromptService

    prompts = PromptService()
    old = prompts.render(prompts.bind("builtin-v1"), "rehab.agent", AgentPromptInputs(now=utcnow()))
    new = prompts.render(
        prompts.bind("business-grounded-v3"), "rehab.agent", AgentPromptInputs(now=utcnow())
    )
    assert "business_grounding@v3" in new.fragments
    assert "business_grounding@v3" not in old.fragments
    assert new.template_hash != old.template_hash
    latest = prompts.render(
        prompts.bind("business-grounded-v4"), "rehab.agent", AgentPromptInputs(now=utcnow())
    )
    assert "business_grounding@v4" in latest.fragments
    assert latest.template_hash != new.template_hash


def test_consultation_boundary_reaches_model_without_changing_source_evidence():
    from meta_agent.contracts import fingerprint
    from meta_agent.orchestration.identity import TrustedScope

    async def check():
        async with runtime() as (c, _):
            scope = TrustedScope("business-eval", "synthetic-reviewer", project_patient_id="900001")
            c.application.backend = SyntheticBackend("measured")
            activate(
                c,
                [tool_call("get_patient_consultation"), AIMessage("本次记录未提供处方信息。")],
            )
            run = await c.application.execute(request("上次医嘱", scope=scope))
            assert run.record.status == "succeeded"
            checkpoint = await c.repository.checkpointer.aget_tuple(
                {"configurable": {"thread_id": run.record.run_id}}
            )
            messages = checkpoint.checkpoint["channel_values"]["messages"]
            body = json.loads(next(m.content for m in messages if m.type == "tool"))
            assert "字段未返回不等于没有" in body["interpretation_boundary"]
            evidence = await c.repository.evidence(scope.scope_hash, body["evidence_id"])
            assert "interpretation_boundary" not in evidence.payload
            assert "prescriptions" not in evidence.payload["data"]["items"][0]
            assert fingerprint(evidence.payload) == evidence.raw_payload_hash

    asyncio.run(check())


def test_input_observer_records_invalid_messages_instead_of_hiding_them():
    from meta_agent.orchestration.identity import TrustedScope

    async def check():
        async with runtime() as (c, _):
            ctx = context(c, TrustedScope("business-eval", "synthetic-reviewer"))
            observer = VisibilityObserver()
            invalid = ToolMessage(
                content=json.dumps(
                    {
                        "tool_name": "search_knowledge",
                        "evidence_id": "missing",
                        "facts": [{"value": "stale text"}],
                    }
                ),
                tool_call_id="test",
            )
            await observer(ctx, [invalid])
            entry = observer.snapshots[ctx.record.run_id][0]["tool_messages"][0]
            assert entry["evidence_valid"] is False
            assert entry["payload"]["facts"][0]["value"] == "stale text"
            ctx.scope = TrustedScope("other", "actor")
            with pytest.raises(ValueError, match="isolated synthetic"):
                await observer(ctx, [invalid])

    asyncio.run(check())


def test_route_amendment_requires_artifact_and_does_not_score_lexical_alerts_as_semantics():
    from tools.review_contract_eval import review_row

    row = {
        "case_id": "C10",
        "repeat": 1,
        "turn": 1,
        "automated": {
            "checks": [
                {
                    "kind": "required_tool",
                    "expected": "get_iretour_session_analysis",
                    "passed": False,
                }
            ],
            "passed": False,
        },
        "action_artifact_events": [],
        "tool_results": [],
        "answer": "本次未提供处方，不代表患者没有处方。",
    }
    assert not review_row(row)["route_corrected_smoke"]
    row["action_artifact_events"] = [{"type": "artifact_ready"}]
    reviewed = review_row(row)
    assert reviewed["route_corrected_smoke"]
    assert reviewed["lexical_alerts"]["absence_wording"]
    assert reviewed["semantic_review"] == "NOT_ASSESSED"
    assert row["automated"]["passed"] is False


def test_patient_fact_audit_rejects_invented_absence_and_changed_source_version():
    from tools.check_business_eval import patient_fact_errors

    evidence = {"payload": {"data": {}}, "source_version": "original"}
    fact = {
        "path": "/data/diagnosis",
        "value": "无疾病",
        "value_status": "valid",
        "source_version": "other",
    }
    assert set(patient_fact_errors(fact, evidence)) == {
        "fact_value_mismatch",
        "fact_absence_status_mismatch",
        "fact_source_version_mismatch",
    }
