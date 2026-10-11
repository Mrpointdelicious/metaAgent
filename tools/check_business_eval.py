"""
创建日期：2026-10-11
文件功能：离线核对合成业务评测的来源完整性、证据覆盖和调用指标，不评判临床正确性。
"""

import argparse
import json
import math
from collections import Counter
from pathlib import Path

from meta_agent.contracts import fingerprint
from meta_agent.domains.knowledge_citations import REF
from meta_agent.infrastructure.repository import resolve_pointer

ROOT = Path(__file__).resolve().parents[1]


def evidence_errors(evidence, corpus):
    errors = []
    if fingerprint(evidence["payload"]) != evidence["raw_payload_hash"]:
        errors.append("payload_hash_mismatch")
    documents = {doc["doc_id"]: doc for doc in corpus["documents"]}
    for snippet in evidence["payload"].get("snippets", []):
        ref = snippet["doc_ref"]
        doc_id, rest = ref.split("@", 1)
        version, sid = rest.split("#", 1)
        doc = documents.get(doc_id)
        section = (
            next((s for s in doc["sections"] if s["section_id"] == sid), None) if doc else None
        )
        if (
            not doc
            or not section
            or not (
                doc["approved"]
                and doc["approved_by"].strip()
                and doc["version"] == version
                and snippet["title"] == doc["title"]
                and snippet["source"] == doc["source"]
                and snippet["section"] == section["title"]
                and snippet["text"] == section["text"]
            )
        ):
            errors.append(f"source_mapping:{ref}")
    return errors


def percentile(values, p):
    return sorted(values)[max(0, math.ceil(len(values) * p) - 1)] if values else None


def patient_fact_errors(fact, evidence):
    if evidence is None:
        return ["missing_fact_evidence"]
    exists, value = resolve_pointer(evidence["payload"], fact["path"])
    errors = []
    if fact["value"] != (value if exists else None):
        errors.append("fact_value_mismatch")
    if fact["source_version"] != evidence["source_version"]:
        errors.append("fact_source_version_mismatch")
    if (not exists or value is None) and fact["value_status"] != "missing":
        errors.append("fact_absence_status_mismatch")
    return errors


def analyze(report, corpus):
    rows = report["rows"]
    evidence = {
        ev["evidence_id"]: ev for r in rows for ev in r["evidence"] + r["knowledge_evidence"]
    }
    errors = {
        eid: problems for eid, ev in evidence.items() if (problems := evidence_errors(ev, corpus))
    }
    wrong_backend_scope = []
    coverage = []
    stale_visible = []
    invalid_answer_refs = []
    fact_errors = []
    fact_count = 0
    for row in rows:
        for tool in row["tool_results"]:
            if tool["outputs"].get("tool_name") in {
                "get_patient_consultation",
                "get_patient_rehab",
            }:
                for view in tool["facts"]:
                    fact_count += 1
                    fact = view["fact"]
                    if problems := patient_fact_errors(fact, evidence.get(fact["evidence_id"])):
                        fact_errors.append([row["case_id"], fact["path"], problems])
        allowed = {
            s["doc_ref"] for ev in row["knowledge_evidence"] for s in ev["payload"]["snippets"]
        }
        invalid = sorted(set(REF.findall(row["answer"])) - allowed)
        if invalid:
            invalid_answer_refs.append([row["case_id"], row["repeat"], row["turn"], invalid])
        for call in row["backend_calls"]:
            body = call["payload"]
            if call["name"] == "resolve_patient_identity":
                valid = body.get("project_patient_id") == 900001
            else:
                valid = body.get("system_context", {}).get("user") == "900001"
            if not valid:
                wrong_backend_scope.append(
                    [row["case_id"], row["repeat"], row["turn"], call["name"]]
                )
        needed = set(row["expected"]["required_refs"])
        if needed:
            candidates = {ref for q in row["candidate_queries"] for ref in q["refs"]}
            coverage.append(
                {
                    "case_id": row["case_id"],
                    "repeat": row["repeat"],
                    "turn": row["turn"],
                    "needed": len(needed),
                    "candidate": len(needed & candidates)
                    if row["expected"]["knowledge"] != "reuse"
                    else None,
                    "returned": len(needed & set(row["automated"]["returned_refs"])),
                    "visible": len(needed & set(row["automated"]["visible_refs"])),
                }
            )
        for snapshot in row["model_inputs"][-1:]:
            for message in snapshot["tool_messages"]:
                if message.get("evidence_valid") is False and any(
                    e["full_text_visible"] for e in message["knowledge_excerpts"]
                ):
                    stale_visible.append(
                        [row["case_id"], row["repeat"], row["turn"], message["evidence_id"]]
                    )
    tools = [r for row in rows for r in row["tool_results"]]
    knowledge = [r for r in tools if r["outputs"].get("tool_name") == "search_knowledge"]
    completed = report["summary"]["case_runs"]
    return {
        "mode": report["mode"],
        "prompt_bundle": report["prompt_bundle"],
        "turns": len(rows),
        "case_runs": len(completed),
        "automated_pass_turns": sum(r["automated"]["passed"] for r in rows),
        "automated_pass_case_runs": sum(r["all_turn_checks_pass"] for r in completed),
        "source_evidence_count": len(evidence),
        "source_errors": errors,
        "wrong_backend_scope": wrong_backend_scope,
        "stale_knowledge_visible": stale_visible,
        "invalid_answer_references": invalid_answer_refs,
        "patient_fact_occurrences": fact_count,
        "patient_fact_errors": fact_errors,
        "stale_observation": "measured"
        if all(
            "evidence_valid" in m
            for r in rows
            for s in r["model_inputs"]
            for m in s["tool_messages"]
        )
        else "legacy observer only recorded valid evidence",
        "coverage": coverage,
        "tool_calls": len(tools),
        "backend_calls": sum(len(r["backend_calls"]) for r in rows),
        "model_calls": sum(r["trace"].get("llm_calls", 0) for r in rows),
        "knowledge_calls": len(knowledge),
        "no_hit_calls": sum(r["code"] == "no_knowledge_evidence" for r in knowledge),
        "knowledge_error_codes": dict(Counter(r["code"] for r in knowledge if r["code"])),
        "status_counts": dict(Counter(r["actual_status"] for r in rows)),
        "latency_ms": {
            "p50": percentile([r["latency_ms"] for r in rows], 0.5),
            "p95": percentile([r["latency_ms"] for r in rows], 0.95),
            "max": max((r["latency_ms"] for r in rows), default=None),
            "small_sample_descriptive_only": True,
        },
        "independent_human_review": "NOT_ASSESSED",
        "clinical_quality": "NOT_ASSESSED",
        "answer_entailment": "requires claim review; not established by pattern checks",
    }


def main(args):
    corpus = json.loads(args.corpus.read_text(encoding="utf-8"))
    result = {
        str(p): analyze(json.loads(p.read_text(encoding="utf-8")), corpus) for p in args.results
    }
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    for path, summary in result.items():
        if (
            summary["source_errors"]
            or summary["wrong_backend_scope"]
            or summary["stale_knowledge_visible"]
            or summary["invalid_answer_references"]
            or summary["patient_fact_errors"]
        ):
            raise SystemExit(f"Evidence audit failed: {path}; inspect saved audit")
    print("Evidence hashes, local source mappings and synthetic backend scope checks passed.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--results", type=Path, nargs="+", required=True)
    parser.add_argument("--corpus", type=Path, default=ROOT / "fixtures/business_eval/corpus.json")
    parser.add_argument("--output", type=Path, required=True)
    main(parser.parse_args())
