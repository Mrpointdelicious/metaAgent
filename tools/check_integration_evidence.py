"""Recheck saved demo source mappings and citation membership, never clinical correctness."""

import argparse
import hashlib
import json
import re
from pathlib import Path

REF = re.compile(r"[a-zA-Z0-9_-]+@[a-zA-Z0-9_.-]+#[a-zA-Z0-9_-]+")


def valid_local_snippet(snippet, corpus):
    for doc in corpus["documents"]:
        for section in doc["sections"]:
            if (
                snippet.get("doc_ref")
                == f"{doc['doc_id']}@{doc['version']}#{section['section_id']}"
            ):
                return (
                    doc["approved"]
                    and bool(doc["approved_by"].strip())
                    and snippet.get("title") == doc["title"]
                    and snippet.get("source") == doc["source"]
                    and snippet.get("section") == section["title"]
                    and snippet.get("text") == section["text"]
                )
    return False


def main(args):
    report = json.loads(args.cases.read_text(encoding="utf-8"))
    corpus = json.loads(args.corpus.read_text(encoding="utf-8"))
    docs = {d["code"]: d for d in corpus["documents"]}
    local_corpus = json.loads(args.local_corpus.read_text(encoding="utf-8"))
    checked = []
    occurrence_count = 0
    for case in report["cases"]:
        if case["kind"] != "real_model_tcp":
            checked.append(
                {
                    "case": case["case"],
                    "protocol_status_matches": case["actual_http"] == case["expected_http"],
                }
            )
            continue
        snippets = [
            s
            for ev in case["evidence"] + case.get("history_evidence", [])
            if ev["tool_name"] == "knowledge.search.remote_rag"
            for s in ev["payload"]["snippets"]
        ]
        failures = []
        local_snippets = [
            s
            for ev in case["evidence"] + case.get("history_evidence", [])
            if ev["tool_name"] == "knowledge.search"
            for s in ev["payload"]["snippets"]
        ]
        failures.extend(
            s["doc_ref"] for s in local_snippets if not valid_local_snippet(s, local_corpus)
        )
        for s in snippets:
            code = s["source"].removeprefix("repo://demo/")
            doc = docs.get(code)
            section = (
                next((x for x in doc["sections"] if x["section_id"] == s["section_id"]), None)
                if doc
                else None
            )
            if (
                not doc
                or not section
                or not all(
                    [
                        s["document_version"] == doc["version"],
                        s["title"] == doc["title"],
                        s["source"] == doc["source"],
                        s["section"] == section["title"],
                        (s["start"], s["end"]) == (section["start"], section["end"]),
                        s["text"] == doc["text"][s["start"] : s["end"]],
                        s["page"] is None,
                        s["doc_ref"] == f"{code}@{doc['version']}#{section['section_id']}",
                        s["text_sha256"] == hashlib.sha256(s["text"].encode()).hexdigest(),
                    ]
                )
            ):
                failures.append(s["doc_ref"])
        cited = REF.findall(case["answer"])
        occurrence_count += len(cited)
        invalid = sorted(set(cited) - {s["doc_ref"] for s in snippets + local_snippets})
        required = {
            "E01": {"protocol-a@v1#outcomes"},
            "E02": {
                f"protocol-{doc}@v1#{section}"
                for doc in "ab"
                for section in ("population", "intervention", "outcomes")
            },
            "E09-agent": {"protocol-a@v1#outcomes", "protocol-b@v1#outcomes"},
            "E10": {"protocol-a@v1#outcomes"},
            "E10-expired": {"protocol-a@v1#outcomes"},
        }.get(case["case"], set())
        checked.append(
            {
                "case": case["case"],
                "source_mapping_failures": failures,
                "invalid_citations": invalid,
                "cited_refs": sorted(set(cited)),
                "local_source_count": len(local_snippets),
                "required_refs_covered": required.issubset({s["doc_ref"] for s in snippets}),
                "semantic_support": "requires technical review; not evaluated by this script",
                "run_status": case["actual_status"],
            }
        )
    result = {
        "scope": "source mapping/citation membership/protocol only; not accuracy",
        "citation_occurrences": occurrence_count,
        "checks": checked,
    }
    target = args.cases.parent / "automated-checks.json"
    target.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    assert all(
        not c.get("source_mapping_failures")
        and not c.get("invalid_citations")
        and c.get("protocol_status_matches", True)
        and c.get("required_refs_covered", True)
        for c in checked
    ), "Integration evidence checks failed; inspect automated-checks.json"
    print(
        f"Source/citation/protocol checks passed; {occurrence_count} citation occurrences. "
        "Semantic support not assessed."
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--cases", type=Path, required=True)
    parser.add_argument("--corpus", type=Path, required=True)
    parser.add_argument(
        "--local-corpus",
        type=Path,
        default=Path(__file__).resolve().parents[1] / "fixtures/business_eval/local_rollback.json",
    )
    main(parser.parse_args())
