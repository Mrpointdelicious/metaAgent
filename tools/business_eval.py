"""
创建日期：2026-10-11
文件功能：真实单Agent业务评测，使用冻结合成记录、本地实时检索和输入侧证据观测。

No patient DB, private corpus, production service or persisted credential is used.
Automated checks are smoke checks, not independent human or clinical judgments.
"""

import argparse
import asyncio
import hashlib
import json
import re
import time
from contextlib import ExitStack
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from langchain_core.messages import ToolMessage

from meta_agent.agent import tools as agent_tools
from meta_agent.agent.runtime import SingleAgentRuntime
from meta_agent.application.service import ApplicationRequest
from meta_agent.config import Settings
from meta_agent.contracts import DomainError
from meta_agent.domains import knowledge
from meta_agent.infrastructure.container import create_container
from meta_agent.llm.models import create_planner_model
from meta_agent.orchestration.identity import TrustedScope

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "fixtures/business_eval"
PATIENT = "900001"
TENANT = "business-eval"


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_frozen(directory=None):
    directory = Path(directory or FIXTURES)
    pins = json.loads((directory / "frozen.sha256.json").read_text(encoding="utf-8"))
    for name, digest in pins.items():
        if sha(directory / name) != digest:
            raise ValueError(f"Frozen evaluation input changed: {name}")
    manifest = json.loads((directory / "cases.json").read_text(encoding="utf-8"))
    knowledge.Corpus.model_validate_json((directory / "corpus.json").read_bytes())
    if not manifest["synthetic_only"] or manifest["clinical_approval"]:
        raise ValueError("Only synthetic nonclinical evaluation is supported")
    return manifest, pins


def envelope(name, data, status="success", message=None):
    version = "patient-layers-1.0.0" if name in agent_tools.LAYER_TOOLS else "iretour-1.0.0"
    return {
        "tool_name": name,
        "contract_version": version,
        "request_id": "synthetic-business-v1",
        "status": status,
        "data": data,
        "meta": {"watermark": "synthetic-business-v1", "source_completeness": "synthetic"},
        "patient_message": message or "自建合成记录，仅用于非临床工程评测。",
    }


def session(ref="iretour_s_v1_eval_latest", *, placeholder=False, unknown_unit=False):
    day, measured = {
        "iretour_s_v1_eval_latest": ("10", 0.64),
        "iretour_s_v1_eval_previous": ("08", 0.48),
        "iretour_s_v1_eval_older": ("06", 0.40),
    }[ref]
    value = 0 if placeholder else measured
    return {
        "session": {"session_ref": ref},
        "time": {
            "execution_ended_at": f"2026-10-{day}T10:00:00+08:00",
            "training_state": "completed",
        },
        "activity": {"canonical_code": "synthetic_walk", "display_name": "合成步行训练"},
        "plan": {"duration_seconds": 120, "state": "planned", "not_actual_result": True},
        "result": {
            "result_state": "summary_available",
            "metrics": [
                {
                    "metric_code": "speed",
                    "display_name": "步速",
                    "raw_value": value,
                    "normalized_value": None if placeholder else value,
                    "unit": None if unknown_unit else "m/s",
                    "unit_status": "unknown" if unknown_unit else "confirmed",
                    "trend_eligible": not unknown_unit,
                    "value_status": "placeholder" if placeholder else "measured",
                    "patient_message": "0为占位，没有有效实测步速"
                    if placeholder
                    else "合成实测记录",
                }
            ],
        },
        "quality": {
            "usable": not placeholder,
            "patient_message": "占位数值不能解释为实测或行走能力"
            if placeholder
            else "合成记录可读取",
            "explanation_boundary": "单次数值不构成个体康复或疗效结论。",
        },
    }


class SyntheticBackend:
    """Finite fixture backend. Unseeded calls fail; no network fallback exists."""

    def __init__(self, fixture):
        self.fixture = fixture
        self.calls = []

    async def post(self, name, payload):
        self.calls.append({"name": name, "payload": deepcopy(payload)})
        if name == "resolve_patient_identity":
            return {
                "contract_version": "1.6.0",
                "status": "unavailable",
                "data": {"binding_status": "unresolved", "project_patient_id": int(PATIENT)},
            }
        if payload.get("system_context", {}).get("user") != PATIENT:
            raise DomainError("eval_scope_mismatch", "合成测试身份不匹配。")
        if name == "get_patient_profile":
            data = {
                "patient_ref": f"project:{PATIENT}",
                "age": 60,
                "self_reported_history": [],
                "source": "synthetic-profile",
            }
        elif name == "get_patient_consultation":
            data = {
                "patient_ref": f"project:{PATIENT}",
                "page": {"page_number": 1, "has_next": False},
                "items": [
                    {
                        "consultation_date": "2026-10-08",
                        "diagnoses": [],
                        "orders": [{"text": "按既有计划训练，出现不适联系主管医生。"}],
                    }
                ],
            }
        elif name == "get_patient_rehab":
            data = {
                "patient_ref": f"project:{PATIENT}",
                "plans": [{"state": "planned"}],
                "iretour": {
                    "availability": "available",
                    "latest_session_ref": "iretour_s_v1_eval_latest",
                },
                "irego": {"availability": "unavailable", "reason": "唯一身份映射未确认"},
            }
        elif name == "get_iretour_session_analysis":
            ref = payload.get("session_ref") or "iretour_s_v1_eval_latest"
            if ref not in {
                "iretour_s_v1_eval_latest",
                "iretour_s_v1_eval_previous",
                "iretour_s_v1_eval_older",
            }:
                raise DomainError("eval_record_unknown", "合成记录不存在。", outcome="unavailable")
            data = session(
                ref,
                placeholder=self.fixture == "placeholder",
                unknown_unit=self.fixture == "unknown_unit",
            )
        elif name == "get_iretour_patient_history":
            page = payload.get("page_number", 1)
            data = {
                "page": {"page_number": page, "has_next": page == 1},
                "items": [
                    {
                        "session_ref": ref,
                        "session_time": f"2026-10-{day}T10:00:00+08:00",
                        "training_state": "completed",
                        "result_state": "summary_available",
                        "activity": {"display_name": "合成步行训练"},
                    }
                    for ref, day in (
                        [("iretour_s_v1_eval_latest", "10"), ("iretour_s_v1_eval_previous", "08")]
                        if page == 1
                        else [("iretour_s_v1_eval_older", "06")]
                        if page == 2
                        else []
                    )
                ],
            }
        elif name == "get_iretour_longitudinal_analysis":
            unknown = self.fixture == "unknown_unit"
            values = (
                [("06", 0.40), ("08", 0.48), ("10", 0.64)]
                if unknown
                else [("08", 0.48), ("10", 0.64)]
            )
            data = {
                "window": {
                    "resolved_count": len(values),
                    "comparable_count": 0 if unknown else len(values),
                },
                "metric_series": [
                    {
                        "metric_code": "speed",
                        "display_name": "步速",
                        "unit": None if unknown else "m/s",
                        "unit_status": "unknown" if unknown else "confirmed",
                        "trend_eligible": not unknown,
                        "direction": "unknown" if unknown else "increased",
                        "points": [
                            {
                                "value": v,
                                "value_status": "measured",
                                "session_time": f"2026-10-{day}T10:00:00+08:00",
                            }
                            for day, v in values
                        ],
                    }
                ],
                "quality": {
                    "patient_message": "单位未知，禁止转换或解释改善程度。"
                    if unknown
                    else "可描述数值变化，不能单凭变化宣布康复或疗效。"
                },
            }
        elif name == "get_iretour_patient_context":
            data = {
                "i_re_tour": {"availability": "available", "counts": {"usable_session_count": 3}}
            }
        else:
            raise DomainError(
                "eval_unseeded_tool", "该工具没有合成测试数据。", outcome="unavailable"
            )
        return envelope(name, data)

    async def artifact_available(self, url):
        return False


def strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for child in value.values():
            yield from strings(child)
    elif isinstance(value, list):
        for child in value:
            yield from strings(child)


class VisibilityObserver:
    def __init__(self):
        self.snapshots = {}
        self.evidence = {}

    async def __call__(self, ctx, messages):
        if ctx.settings.app_env != "test" or ctx.scope.tenant_id != TENANT:
            raise ValueError("Business eval observer only accepts isolated synthetic scope")
        entries = []
        for message in messages:
            if not isinstance(message, ToolMessage) or not isinstance(message.content, str):
                continue
            try:
                payload = json.loads(message.content)
            except ValueError:
                continue
            eid = payload.get("evidence_id") if isinstance(payload, dict) else None
            if not isinstance(payload, dict):
                continue
            ev = await ctx.repository.evidence(ctx.scope.scope_hash, eid) if eid else None
            if ev is not None:
                self.evidence[eid] = ev.model_dump(mode="json")
            stored = ev.payload if ev else self.evidence.get(eid, {}).get("payload", {})
            visible_strings = list(strings(payload))
            excerpts = [
                {
                    "doc_ref": s["doc_ref"],
                    "text_sha256": hashlib.sha256(s["text"].encode()).hexdigest(),
                    "full_text_visible": s["text"] in visible_strings,
                    "reference_visible": any(s["doc_ref"] in v for v in visible_strings),
                }
                for s in stored.get("snippets", [])
            ]
            entries.append(
                {
                    "evidence_id": eid,
                    "evidence_valid": ev is not None,
                    "tool_name": payload.get("tool_name"),
                    "payload": payload,
                    "knowledge_excerpts": excerpts,
                }
            )
        self.snapshots.setdefault(ctx.record.run_id, []).append(
            {
                "model_call": ctx.llm_budget.calls,
                "tool_messages": entries,
            }
        )


def score_turn(expected, row):
    answer = row["answer"]
    names = [r["outputs"].get("tool_name") for r in row["tool_results"]]
    checks = [
        {"kind": "answer_pattern", "expected": p, "passed": bool(re.search(p, answer, re.I))}
        for p in expected["required_patterns"]
    ]
    checks += [
        {"kind": "required_tool", "expected": n, "passed": n in names}
        for n in expected["required_tools"]
    ]
    if expected.get("tool_alternatives"):
        checks.append(
            {
                "kind": "tool_dependency_alternative",
                "passed": any(
                    all(n in names for n in group) for group in expected["tool_alternatives"]
                ),
            }
        )
    checks += [
        {"kind": "forbidden_tool", "expected": n, "passed": n not in names}
        for n in expected["forbidden_tools"]
    ]
    checks.append(
        {
            "kind": "no_action_artifact",
            "passed": not any(
                e["type"] == "action_ready" or not expected.get("allow_artifact")
                for e in row["action_artifact_events"]
            ),
        }
    )
    checks += [
        {
            "kind": "forbidden_backend",
            "expected": name,
            "passed": not any(c["name"] == name for c in row["backend_calls"]),
        }
        for name in expected.get("forbidden_backend", [])
    ]
    checks.append({"kind": "nonempty_answer", "passed": bool(answer.strip())})
    # Status is measured independently: expected negative outcomes may be unavailable.
    checks.append(
        {
            "kind": "status",
            "passed": row["actual_status"] in expected["allowed_statuses"]
            or expected["knowledge"] == "attempt"
            and row["actual_status"] == "unavailable",
        }
    )
    calls = sum(n == "search_knowledge" for n in names)
    if expected["knowledge"] in {"required", "attempt"}:
        checks.append({"kind": "knowledge_call", "passed": calls > 0})
    if expected["knowledge"] in {"reuse", "unnecessary"}:
        checks.append({"kind": "avoid_knowledge_call", "passed": calls == 0})
    returned = {s["doc_ref"] for e in row["knowledge_evidence"] for s in e["payload"]["snippets"]}
    visible = {
        e["doc_ref"]
        for snapshot in row["model_inputs"][-1:]
        for msg in snapshot["tool_messages"]
        for e in msg["knowledge_excerpts"]
        if msg.get("evidence_valid", True)
        if e["full_text_visible"] and e["reference_visible"]
    }
    for ref in expected["required_refs"]:
        checks.extend(
            [
                {"kind": "returned_ref", "expected": ref, "passed": ref in returned},
                {"kind": "visible_ref", "expected": ref, "passed": ref in visible},
                {
                    "kind": "answer_ref",
                    "expected": ref,
                    "passed": bool(
                        re.search(r"(?<![\w@#-])" + re.escape(ref) + r"(?![\w@#-])", answer)
                    ),
                },
            ]
        )
    if row["case_id"] == "B08" and row["turn"] == 4:
        checks.append(
            {
                "kind": "no_page_after_end",
                "passed": not any(
                    c["name"] == "get_iretour_patient_history" for c in row["backend_calls"]
                ),
            }
        )
    return {
        "checks": checks,
        "passed": all(c["passed"] for c in checks),
        "returned_refs": sorted(returned),
        "visible_refs": sorted(visible),
        "human_review": "NOT_ASSESSED",
        "clinical_review": "NOT_ASSESSED",
        "scope": "automated smoke checks only; not entailment or clinical validity",
    }


async def run_case(case, args, model_fields, repeat, on_row=None):
    directory = Path(getattr(args, "fixtures", None) or FIXTURES)
    settings = Settings(
        _env_file=None,
        app_env="test",
        orchestration_mode="agent",
        persistence_backend="memory",
        dry_run=True,
        knowledge_backend="local_bm25",
        knowledge_corpus_path=str(directory / case.get("corpus", "corpus.json")),
        business_grounding_enabled=getattr(args, "business_grounding", False),
        iretour_reports_enabled=case.get("reports_enabled", False),
        prompt_bundle=args.prompt_bundle,
        doctors_enabled=False,
        scene_actions_enabled=False,
        request_timeout_seconds=60,
        agent_model_timeout_seconds=30,
        agent_max_model_calls=8,
        max_tool_calls=16,
        tool_timeout_seconds=15,
        agent_tool_output_tokens=3000,
        agent_history_tokens=8000,
        planner_temperature=0,
        planner_max_tokens=2048,
        evidence_ttl_seconds=1800,
        **model_fields,
    )
    container = await create_container(settings)
    if case.get("backend") == "contract":
        from tools.contract_fixtures import ContractBackend

        backend = ContractBackend(case["fixture"])
    else:
        backend = SyntheticBackend(case["fixture"])
    observer = VisibilityObserver()
    container.application.backend = backend
    model = create_planner_model(
        settings.model_copy(
            update={
                "planner_model": settings.agent_model or settings.planner_model,
                "planner_timeout_seconds": settings.agent_model_timeout_seconds,
                "planner_max_retries": 0,
            }
        )
    )
    container.application.agent_runtime = SingleAgentRuntime(
        model,
        checkpointer=container.repository.checkpointer,
        store=container.repository.store,
        model_input_observer=observer,
    )
    scope = TrustedScope(
        TENANT,
        "synthetic-reviewer",
        role="patient",
        project_patient_id=PATIENT if case.get("patient", True) else None,
    )
    candidates, rows = [], []
    original_rank, original_allowed = knowledge.rank, agent_tools.tool_allowed

    def observed_rank(query, sections):
        hits = original_rank(query, sections)
        candidates.append(
            {
                "query": query,
                "refs": [
                    f"{sections[i][0].doc_id}@{sections[i][0].version}#{sections[i][1].section_id}"
                    for _, i in hits
                ],
            }
        )
        return hits

    async def timeout_knowledge(self, task, arguments, ctx):
        raise DomainError(
            "rag_timeout", "受控注入：知识查询超时。", outcome="unavailable", retryable=True
        )

    try:
        with ExitStack() as patches:
            patches.enter_context(patch.object(knowledge, "rank", observed_rank))
            if args.mode == "M0" or case.get("disable_knowledge"):
                patches.enter_context(
                    patch.object(
                        agent_tools,
                        "tool_allowed",
                        lambda ctx, name: (
                            name != "search_knowledge" and original_allowed(ctx, name)
                        ),
                    )
                )
            if case.get("fault") == "timeout":
                patches.enter_context(
                    patch.object(knowledge.KnowledgeAdapter, "execute", timeout_knowledge)
                )
            conversation = "business-" + uuid4().hex
            for index, expected in enumerate(case["turns"], 1):
                if expected.get("transition") == "expire_knowledge":
                    for eid, ev in list(observer.evidence.items()):
                        if ev["payload"].get("snippets"):
                            await container.repository.delete("evidence", scope.scope_hash, eid)
                start_calls, start_candidates = len(backend.calls), len(candidates)
                started = time.monotonic()
                run = await container.application.execute(
                    ApplicationRequest(
                        expected["query"], scope, conversation, "business-" + uuid4().hex
                    )
                )
                record = run.record
                evidence = []
                for result in record.results.values():
                    for eid in result.evidence_ids:
                        ev = await container.repository.evidence(scope.scope_hash, eid)
                        if ev:
                            evidence.append(ev.model_dump(mode="json"))
                known = {}
                for eid, ev in observer.evidence.items():
                    if ev["payload"].get("snippets") and await container.repository.evidence(
                        scope.scope_hash, eid
                    ):
                        known[eid] = ev
                row = {
                    "case_id": case["case_id"],
                    "turn": index,
                    "repeat": repeat,
                    "mode": args.mode,
                    "corpus_file": case.get("corpus", "corpus.json"),
                    "knowledge_disabled": args.mode == "M0" or case.get("disable_knowledge", False),
                    "query": expected["query"],
                    "expected": expected,
                    "run_id": record.run_id,
                    "actual_status": record.status,
                    "answer": "\n".join(
                        e.payload["text"] for e in record.events if e.type == "answer_part"
                    ),
                    "latency_ms": round((time.monotonic() - started) * 1000, 2),
                    "trace": record.metrics,
                    "tool_results": [r.model_dump(mode="json") for r in record.results.values()],
                    "backend_calls": backend.calls[start_calls:],
                    "candidate_queries": candidates[start_candidates:],
                    "evidence": evidence,
                    "knowledge_evidence": list(known.values()),
                    "model_inputs": observer.snapshots.get(record.run_id, []),
                    "failure_events": [
                        e.model_dump(mode="json") for e in record.events if e.type == "task_failed"
                    ],
                    "action_artifact_events": [
                        e.model_dump(mode="json")
                        for e in record.events
                        if e.type in {"action_ready", "artifact_ready"}
                    ],
                }
                row["automated"] = score_turn(expected, row)
                rows.append(row)
                if on_row is not None:
                    on_row(row)
                print(
                    json.dumps(
                        {
                            "case": case["case_id"],
                            "turn": index,
                            "repeat": repeat,
                            "status": record.status,
                            "checks_pass": row["automated"]["passed"],
                            "ms": row["latency_ms"],
                        }
                    ),
                    flush=True,
                )
    finally:
        await container.close()
    return rows


async def main(args):
    if not args.allow_existing_model:
        raise SystemExit(
            "Use --allow-existing-model after user authorization; no mock model substitution"
        )
    directory = Path(getattr(args, "fixtures", None) or FIXTURES)
    manifest, pins = load_frozen(directory)
    output = args.output.resolve()
    if output.exists() and any(output.iterdir()):
        raise SystemExit("Output must be empty; previous runs are never overwritten")
    output.mkdir(parents=True, exist_ok=True)
    existing = Settings()  # Only approved model fields are copied; never dump Settings.
    if not existing.deepseek_api_key.get_secret_value():
        raise SystemExit("BLOCKED: configured model credential missing")
    fields = {
        "deepseek_api_key": existing.deepseek_api_key,
        "deepseek_base_url": existing.deepseek_base_url,
        "agent_model": existing.agent_model or existing.planner_model,
        "planner_model": existing.planner_model,
    }
    case_ids = set(args.cases.split(",")) if args.cases else None
    selected = [c for c in manifest["cases"] if case_ids is None or c["case_id"] in case_ids]
    if not selected or case_ids and case_ids - {c["case_id"] for c in selected}:
        raise SystemExit("Unknown or empty case selection")
    report = {
        "generated_at": datetime.now().astimezone().isoformat(),
        "schema_version": "business-eval-1",
        "mode": args.mode,
        "model": fields["agent_model"],
        "prompt_bundle": args.prompt_bundle,
        "business_grounding_enabled": getattr(args, "business_grounding", False),
        "fixture_version": manifest["fixture_version"],
        "execution": {
            "model": "real",
            "patient_backend": "synthetic_in_process",
            "retrieval": "disabled" if args.mode == "M0" else "real_local_bm25",
            "remote_patient_rag": "NOT_RUN",
            "clinical_review": "NOT_ASSESSED",
        },
        "fixture_pins": pins,
        "source_hashes": {
            name: sha(ROOT / name)
            for name in [
                "src/meta_agent/agent/runtime.py",
                "src/meta_agent/agent/context.py",
                "src/meta_agent/agent/tools.py",
                "src/meta_agent/domains/knowledge.py",
                "src/meta_agent/prompts/catalog.py",
                "src/meta_agent/prompts/service.py",
                "src/meta_agent/prompts/templates/business_grounding.v3.md",
                "src/meta_agent/prompts/templates/business_grounding.v4.md",
                "tools/business_eval.py",
            ]
        },
        "budgets": {
            "request_seconds": 60,
            "model_seconds": 30,
            "model_calls": 8,
            "backend_tool_calls": 16,
            "tool_seconds": 15,
            "tool_output_estimated_tokens": 3000,
            "history_estimated_tokens": 8000,
        },
        "case_selection": [c["case_id"] for c in selected],
        "repeats": args.repeats,
        "rows": [],
        "human_review": "NOT_ASSESSED",
        "failure": None,
    }

    def save():
        rows = report["rows"]
        report["summary"] = {
            "turns": len(rows),
            "automated_check_pass_turns": sum(r["automated"]["passed"] for r in rows),
            "not_business_or_clinical_accuracy": True,
            "case_runs": [
                {
                    "case_id": c["case_id"],
                    "repeat": repeat,
                    "all_turn_checks_pass": all(
                        r["automated"]["passed"]
                        for r in rows
                        if r["case_id"] == c["case_id"] and r["repeat"] == repeat
                    ),
                }
                for repeat in range(1, args.repeats + 1)
                for c in selected
                if any(r["case_id"] == c["case_id"] and r["repeat"] == repeat for r in rows)
            ],
        }
        (output / "results.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    save()
    (output / "runner-source.txt").write_text(
        Path(__file__).read_text(encoding="utf-8"), encoding="utf-8"
    )
    (output / "frozen-inputs.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    if any(c.get("backend") == "contract" for c in selected):
        source = ROOT / "tools/contract_fixtures.py"
        report["source_hashes"]["tools/contract_fixtures.py"] = sha(source)
        (output / "provider-source.txt").write_text(
            source.read_text(encoding="utf-8"), encoding="utf-8"
        )
    for source in [
        "src/meta_agent/domains/patient_views.py",
        "src/meta_agent/prompts/templates/business_grounding.v5.md",
        "src/meta_agent/prompts/templates/business_grounding.v6.md",
        "src/meta_agent/prompts/templates/business_grounding.v7.md",
        "src/meta_agent/prompts/templates/business_grounding.v8.md",
        "src/meta_agent/prompts/templates/business_grounding.v9.md",
        "src/meta_agent/domains/knowledge_citations.py",
        "src/meta_agent/prompts/templates/business_context.v1.md",
        "src/meta_agent/prompts/contracts.py",
        "src/meta_agent/config.py",
    ]:
        if (ROOT / source).is_file():
            report["source_hashes"][source] = sha(ROOT / source)

    def on_row(row):
        report["rows"].append(row)
        save()

    try:
        for repeat in range(1, args.repeats + 1):
            for case in selected:
                await run_case(case, args, fields, repeat, on_row)
    except Exception as exc:
        report["failure"] = type(exc).__name__  # Provider exception strings can contain secrets.
        save()
        raise SystemExit(
            f"Evaluation failed: {type(exc).__name__}; completed cases retained"
        ) from None
    print(json.dumps(report["summary"], ensure_ascii=False), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--allow-existing-model", action="store_true")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--mode", choices=["M0", "M1"], default="M1")
    parser.add_argument("--prompt-bundle", default="builtin-v1")
    parser.add_argument("--cases")
    parser.add_argument("--fixtures", type=Path)
    parser.add_argument("--business-grounding", action="store_true")
    parser.add_argument("--repeats", type=int, choices=range(1, 4), default=1)
    asyncio.run(main(parser.parse_args()))
