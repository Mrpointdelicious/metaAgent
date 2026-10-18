"""Few live connectivity probes, not a recall-quality benchmark."""

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import httpx

ROOT = Path(__file__).resolve().parents[1]
LOCAL = ROOT / ".local/maxkb-dev"
CASES = {
    "sop": "请检索知识库：电脑端如何跳跃和奔跑？请引用来源。",
    "scene": "请根据场景文档说明岳阳医院二楼是什么中心，并引用来源。",
    "pdf": "请根据2025年脑卒中智能康复技术应用专家共识说明条目数量，并引用文献与页码。",
}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--case", choices=["all", "sop", "scene", "pdf", "scope"], default="all")
    args = parser.parse_args()
    access = json.loads((LOCAL / "access.json").read_text(encoding="utf8"))
    path = LOCAL / "smoke.json"
    report = json.loads(path.read_text(encoding="utf8")) if path.exists() else {"attempts": []}
    failed = False
    with httpx.Client(trust_env=False, timeout=65) as client:
        for name in (*CASES, "scope"):
            if args.case not in ("all", name):
                continue
            record = {
                "case": name,
                "at": datetime.now(UTC).isoformat(),
                "release": access["release"],
            }
            inputs = {
                "tenantId": access["tenant_id"],
                "space_id": access["space_id"],
                "role": "operator",
            }
            if name == "scope":
                inputs["space_id"] = "unauthorized-other-space"
            try:
                response = client.post(
                    access["url"],
                    headers={
                        "Authorization": "Bearer " + access["service_token"],
                    },
                    json={
                        "query": CASES.get(name, CASES["sop"]),
                        "inputs": inputs,
                        "user": "maxkb-dev-smoke",
                        "conversation_id": "smoke-" + uuid4().hex,
                        "response_mode": "blocking",
                    },
                )
                record["http_status"] = response.status_code
                record["elapsed_ms"] = round(response.elapsed.total_seconds() * 1000, 2)
                if name == "scope":
                    assert response.status_code == 403
                else:
                    response.raise_for_status()
                    body = response.json()
                    meta = body["metadata"]
                    trace, citations = meta["knowledge_retrieval"], meta["citations"]
                    assert trace["backend"] == "maxkb" and trace["response_verified"]
                    assert trace["space_id"] == access["space_id"]
                    assert trace["release"] == access["release"]
                    assert trace["answer_mode"] == "llm" and citations
                    assert all(c["space_id"] == access["space_id"] for c in citations)
                    assert any("[" + c["doc_ref"] + "]" in body["answer"] for c in citations)
                    if name == "pdf":
                        assert any(c["page"] is not None for c in citations)
                    record.update(answer=body["answer"], retrieval=trace, citations=citations)
                record["passed"] = True
            except (httpx.HTTPError, AssertionError, KeyError, ValueError) as exc:
                record.update(passed=False, error=type(exc).__name__)
                if "response" in locals() and response.status_code >= 400:
                    record["detail"] = response.text[:120]
                failed = True
            report["attempts"].append(record)
            path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf8")
            print(
                json.dumps(
                    {
                        k: v
                        for k, v in record.items()
                        if k not in {"answer", "retrieval", "citations"}
                    },
                    ensure_ascii=False,
                )
            )
    if failed:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
