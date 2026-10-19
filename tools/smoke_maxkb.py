"""Probe a few native retrieval/rerank samples and one scope boundary."""

import argparse
import json
import os
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import httpx

ROOT = Path(__file__).resolve().parents[1]
LOCAL = ROOT / ".local/maxkb-dev"
CASES = {
    "sop": ("请检索知识库：电脑端如何跳跃和奔跑？请引用来源。", ["sop_kb"]),
    "scene": (
        "请根据场景文档说明岳阳医院二楼是什么中心，并引用来源。",
        ["scene_kb", "hospital_kb"],
    ),
    "pdf": (
        "请根据2025年脑卒中智能康复技术应用专家共识说明条目数量，并引用文献与页码。",
        ["clinical_kb", "rehab_kb"],
    ),
    "md": (
        "岳阳医院元宇宙医生端登录后，如何使用工作台？请根据医生端使用说明回答。",
        ["sop_kb", "technical_kb"],
    ),
}


def check(client, access, name, *, answer=False):
    record = {"case": name, "at": datetime.now(UTC).isoformat(), "release": access["release"]}
    query, types = CASES.get(name, CASES["sop"])
    space = "unauthorized-other-space" if name == "scope" else access["space_id"]
    headers = {"Authorization": "Bearer " + access["service_token"]}
    payload = {
        "query": query,
        "tenant_id": access["tenant_id"],
        "space_id": space,
        "knowledge_types": types,
        "top_k": 3,
    }
    url = access["retrieve_url"]
    if answer:
        url = access["url"]
        payload = {
            "query": query,
            "inputs": {"tenantId": access["tenant_id"], "space_id": space, "role": "operator"},
            "user": "maxkb-dev-smoke",
            "conversation_id": "smoke-" + uuid4().hex,
            "response_mode": "blocking",
        }
    response = client.post(url, headers=headers, json=payload)
    record.update(
        http_status=response.status_code,
        elapsed_ms=round(response.elapsed.total_seconds() * 1000, 2),
    )
    if name == "scope":
        assert response.status_code == 403
        return record
    response.raise_for_status()
    body = response.json()
    if answer:
        trace, evidence = body["metadata"]["knowledge_retrieval"], body["metadata"]["citations"]
        assert trace["integration"] == "native" and trace["response_verified"]
        assert trace["release"] == access["release"]
        assert any("[" + e["doc_ref"] + "]" in body["answer"] for e in evidence)
        record["answer"] = body["answer"]
    else:
        trace, evidence = body, body["evidence"]
        assert body["stages"]["implementation"] == "maxkb-v2.10.7-lts-native"
        assert body["scope_key"] == access["tenant_id"]
        assert body["corpus_version"] == access["release"]
        assert body["selected_count"] == len(evidence)
    assert trace["backend"] == "maxkb" and trace["status"] in {"success", "partial"}
    assert evidence and all(e["space_id"] == access["space_id"] for e in evidence)
    if name == "pdf":
        assert any(isinstance(e["page"], int) and e["page"] >= 1 for e in evidence)
    if name == "md":
        assert any(e["heading_path"] and e["title"].lower().endswith(".md") for e in evidence)
    assert trace["rerank_applied"] is (name in {"pdf", "md"})
    # Do not forward a service token to an arbitrary URL returned by a backend.
    base = (
        url.split("/compat/dify/", 1)[0] if answer else url.removesuffix("/v1/knowledge/retrieve")
    )
    source = evidence[0]["source"]
    assert source.startswith(base + "/v1/knowledge/sources/")
    with client.stream("GET", source, headers=headers) as downloaded:
        downloaded.raise_for_status()
        assert next(downloaded.iter_bytes(), b"")
    record.update(
        status=trace["status"],
        rerank_applied=trace["rerank_applied"],
        degradation=trace["degradation"],
        citations=[{k: v for k, v in e.items() if k not in {"text", "score"}} for e in evidence],
    )
    return record


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--case", choices=["all", *CASES, "scope"], default="all")
    parser.add_argument("--answer", action="store_true", help="Explicitly test configured answers")
    args = parser.parse_args()
    access = json.loads((LOCAL / "access.json").read_text(encoding="utf8"))
    if args.answer and access.get("answer_mode") == "disabled":
        parser.error("Enable llm/extractive answers and refresh access.json before --answer")
    path = LOCAL / "native-smoke.json"
    report = {
        "integration": "native",
        "mode": "answer" if args.answer else "retrieval",
        "samples": [],
    }
    failed = False
    with httpx.Client(trust_env=False, follow_redirects=False, timeout=65) as client:
        for name in (*CASES, "scope"):
            if args.case not in ("all", name):
                continue
            try:
                record = {**check(client, access, name, answer=args.answer), "passed": True}
            except (httpx.HTTPError, AssertionError, KeyError, ValueError) as exc:
                # Never copy a native/provider error body into logs or a report.
                record = {"case": name, "passed": False, "error": type(exc).__name__}
                if isinstance(exc, httpx.HTTPStatusError):
                    record["http_status"] = exc.response.status_code
                failed = True
            report["samples"].append(record)
            print(
                json.dumps(
                    {k: v for k, v in record.items() if k not in {"answer", "citations"}},
                    ensure_ascii=False,
                )
            )
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf8")
    os.chmod(temporary, 0o600)
    temporary.replace(path)
    if failed:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
