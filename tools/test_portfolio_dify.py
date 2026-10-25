"""
创建日期：2026-10-21
文件功能：从 Dify 已发布聊天入口验证知识检索、连续追问、英文来源和可选服务故障。

Uses the application's existing public web passport flow. Tokens are kept in memory;
raw responses remain local. --fault briefly stops only the new RAG API and restores it.
"""

import argparse
import json
import subprocess
import time
from pathlib import Path
from uuid import UUID

import httpx

APP_ID = "747e56ab-5f99-45ac-b511-a2b960f249c5"
APP_CODE = "j1keGUrMVpPrYUoq"
OUTPUT = Path(".local/portfolio-dify")


def lookup(workflow_run_id):
    rid = str(UUID(workflow_run_id))
    query = (
        "select json_build_object('workflow_id',r.workflow_id,'invoke',n.outputs::json)::text "
        "from workflow_node_executions n join workflow_runs r on r.id=n.workflow_run_id "
        f"where r.app_id='{APP_ID}' and r.id='{rid}' and n.node_id='invoke' "
        "order by n.created_at desc limit 1"
    )
    result = subprocess.run(
        [
            "docker",
            "exec",
            "docker-db_postgres-1",
            "psql",
            "-U",
            "postgres",
            "-d",
            "dify",
            "-At",
            "-c",
            query,
        ],
        capture_output=True,
        encoding="utf-8",
        check=True,
    )
    raw = json.loads(result.stdout)
    body = json.loads(raw["invoke"]["body"])
    return {
        "workflow_id": raw["workflow_id"],
        "agent_status": body.get("status"),
        "agent_run_id": body.get("run_id"),
        "prompt_bundle": body.get("metrics", {}).get("prompt_bundle"),
        "prompt_bundle_hash": body.get("metrics", {}).get("prompt_bundle_hash"),
        "task_statuses": body.get("task_statuses"),
        "retrievals": body.get("metrics", {}).get("knowledge_retrieval", []),
    }


def chat(client, query, conversation_id=None, inputs=None):
    payload = {"inputs": inputs or {}, "query": query, "response_mode": "streaming", "files": []}
    if conversation_id:
        payload["conversation_id"] = conversation_id
    events = []
    with client.stream("POST", "/api/chat-messages", json=payload) as response:
        response.raise_for_status()
        for line in response.iter_lines():
            if line.startswith("data: "):
                event = json.loads(line[6:])
                events.append(event)
                if event.get("event") in {"error", "message_end", "workflow_finished"}:
                    break
    finished = next((e["data"] for e in events if e.get("event") == "workflow_finished"), {})
    assert finished.get("status") == "succeeded", "Dify workflow did not succeed"
    answer = "".join(e.get("answer", "") for e in events if e.get("event") == "message")
    if not answer:
        answer = finished.get("outputs", {}).get("answer", "")
    conversation = next((e["conversation_id"] for e in events if e.get("conversation_id")), None)
    rid = next(e["workflow_run_id"] for e in events if e.get("workflow_run_id"))
    return {
        "workflow_run_id": rid,
        "conversation_id": conversation,
        "answer": answer,
        "events": events,
        **lookup(rid),
    }


def check_sources(result):
    assert result["agent_status"] in {"succeeded", "partial"}
    assert result["retrievals"], "MetaAgent did not retrieve current RAG evidence"
    sources = [source for r in result["retrievals"] for source in r["sources"]]
    assert sources and all(
        source.get("content_hash")
        and (source.get("page") or (source.get("section") and source.get("source_locator")))
        for source in sources
    )
    assert "本次检索资料" in result["answer"]
    if any(source.get("page") for source in sources):
        assert "PDF 第" in result["answer"]
    return sources


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fault", action="store_true")
    args = parser.parse_args()
    OUTPUT.mkdir(parents=True, exist_ok=True)
    report = {"app_id": APP_ID, "checks": []}
    with httpx.Client(
        base_url="http://localhost", timeout=180, headers={"X-App-Code": APP_CODE}
    ) as client:
        passport = client.get("/api/passport")
        passport.raise_for_status()
        client.headers["X-App-Passport"] = passport.json()["access_token"]
        cases = [
            ("balance", "请检索知识库，说明脑卒中立位平衡障碍的评定方法，并标明文献和PDF页码。"),
            ("followup", "刚才提到的PASS主要适用于哪个阶段？请依据现行知识库重新核实并标明页码。"),
            (
                "english",
                "请检索 Effect of wearable exoskeleton on post-stroke gait 这篇系统综述，"
                "概括研究主题和结论边界，并给出原文PDF页码。",
            ),
        ]
        conversation = None
        previous_traces = set()
        for name, query in cases:
            print("Testing published workflow:", name, flush=True)
            result = chat(client, query, conversation if name == "followup" else None)
            sources = check_sources(result)
            traces = {r["trace_id"] for r in result["retrievals"]}
            if name == "followup":
                assert traces.isdisjoint(previous_traces), "Knowledge result reused across turns"
            if name == "english":
                assert any("exoskeleton" in s["title"].lower() for s in sources)
            conversation, previous_traces = result["conversation_id"], traces
            (OUTPUT / (name + ".json")).write_text(
                json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            report["checks"].append(
                {
                    "case": name,
                    "workflow_run_id": result["workflow_run_id"],
                    "workflow_id": result["workflow_id"],
                    "agent_status": result["agent_status"],
                    "trace_ids": sorted(traces),
                    "sources": len(sources),
                    "status": "passed",
                }
            )
        if args.fault:
            print("Testing unavailable RAG service; restoring it in finally", flush=True)
            subprocess.run(
                ["docker", "stop", "rag-portfolio-api-1"], check=True, capture_output=True
            )
            try:
                result = chat(
                    client,
                    "请用知识库检索立位平衡指南并给出原文页码；"
                    "没有检索结果就说明服务不可用，不要凭常识补全。",
                )
                assert not result["retrievals"] and "本次检索资料" not in result["answer"]
                assert any(status == "unavailable" for status in result["task_statuses"].values())
                (OUTPUT / "unavailable.json").write_text(
                    json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
                )
                report["checks"].append(
                    {
                        "case": "rag_unavailable",
                        "status": "passed",
                        "workflow_run_id": result["workflow_run_id"],
                        "agent_status": result["agent_status"],
                    }
                )
            finally:
                subprocess.run(
                    ["docker", "start", "rag-portfolio-api-1"], check=True, capture_output=True
                )
                for _ in range(30):
                    try:
                        if (
                            httpx.get("http://127.0.0.1:5052/health/ready", timeout=2).status_code
                            == 200
                        ):
                            break
                    except httpx.HTTPError:
                        pass
                    time.sleep(1)
                else:
                    raise RuntimeError("RAG API did not recover")
                report["rag_restored"] = True
    (OUTPUT / "verification.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
