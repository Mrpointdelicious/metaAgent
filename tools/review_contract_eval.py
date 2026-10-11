"""Derived route review and lexical alerts; never overwrite raw trials or claim entailment."""

import argparse
import hashlib
import json
import re
from collections import Counter
from pathlib import Path

RUBRIC = {
    "version": "contract-route-review-v2",
    "reason": "C09 disabled report needs no session read; C10 may use a queried history anchor.",
    "frozen_inputs_unchanged": True,
    "semantics": "NOT_ASSESSED; lexical alerts require review, not automatic error labels",
}
ALERTS = {
    "absence_wording": r"没有.{0,6}处方|无处方|未开.{0,3}处方",
    "plan_wording": r"(?:确实|正在|已经|均|都).{0,5}按.{0,6}计划|与.{0,8}计划.{0,5}(?:一致|符合)",
    "internal_fields": r"session_ref|has_next|trend_eligible|value_status|\bnull\b",
}


def review_row(row):
    checks = row["automated"]["checks"]
    corrected = [
        c
        for c in checks
        if not (
            row["case_id"] in {"C09", "C10"}
            and c["kind"] == "required_tool"
            and c.get("expected") == "get_iretour_session_analysis"
        )
    ]
    if row["case_id"] == "C10":
        corrected.append(
            {
                "kind": "successful_synthetic_artifact",
                "passed": any(e["type"] == "artifact_ready" for e in row["action_artifact_events"]),
            }
        )
    calls = [r for r in row["tool_results"] if r["outputs"].get("tool_name") == "search_knowledge"]
    return {
        "case_id": row["case_id"],
        "repeat": row["repeat"],
        "turn": row["turn"],
        "original_smoke": row["automated"]["passed"],
        "route_corrected_smoke": all(c["passed"] for c in corrected),
        "route_checks": corrected,
        "lexical_alerts": {
            name: re.findall(pattern, row["answer"], re.I)
            for name, pattern in ALERTS.items()
            if re.search(pattern, row["answer"], re.I)
        },
        "knowledge_calls": len(calls),
        "c13_extra_knowledge_calls": max(0, len(calls) - 1) if row["case_id"] == "C13" else None,
        "semantic_review": "NOT_ASSESSED",
        "clinical_review": "NOT_ASSESSED",
    }


def main(args):
    if args.output.exists():
        raise SystemExit("Derived review output exists; select a new file")
    reports = {}
    for path in args.results:
        report = json.loads(path.read_text(encoding="utf-8"))
        rows = [review_row(r) for r in report["rows"]]
        reports[str(path)] = {
            "rows": rows,
            "turns": len(rows),
            "original_pass": sum(r["original_smoke"] for r in rows),
            "route_corrected_pass": sum(r["route_corrected_smoke"] for r in rows),
            "lexical_alert_counts": dict(Counter(k for r in rows for k in r["lexical_alerts"])),
            "c13_extra_calls": sum(r["c13_extra_knowledge_calls"] or 0 for r in rows),
        }
    result = {
        "rubric": RUBRIC,
        "reviewer_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "reports": reports,
    }
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({p: {k: v for k, v in r.items() if k != "rows"} for p, r in reports.items()}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--results", type=Path, nargs="+", required=True)
    parser.add_argument("--output", type=Path, required=True)
    main(parser.parse_args())
