"""Real TCP + real configured model acceptance, using only self-authored demo text.

No model/tool scripts, no patient identities, no existing servers/DBs are contacted.
Use --allow-existing-model only after explicit authorization to load model settings.
"""

import argparse
import asyncio
import hashlib
import json
import os
import secrets
import socket
import subprocess
import tempfile
from datetime import datetime
from pathlib import Path
from uuid import uuid4
from zoneinfo import ZoneInfo

import httpx
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from meta_agent.config import Settings
from meta_agent.orchestration.identity import TrustedScope

ROOT = Path(__file__).resolve().parents[1]


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


async def wait_ready(url, process=None):
    async with httpx.AsyncClient(trust_env=False) as client:
        for _ in range(100):
            if process and process.poll() is not None:
                raise RuntimeError("Owned demo service exited; inspect rag-service.log")
            try:
                if (await client.get(url, timeout=1)).status_code == 200:
                    return
            except httpx.RequestError:
                pass
            await asyncio.sleep(0.1)
    raise RuntimeError("Owned service did not become ready")


async def main(args):
    if not args.allow_existing_model:
        raise SystemExit("Explicit --allow-existing-model is required for real model acceptance")
    from meta_agent.app import create_app

    existing = Settings()  # Authorized model configuration is used only in memory.
    if not existing.deepseek_api_key.get_secret_value():
        raise SystemExit("BLOCKED: model credential is not configured; no mock substitution")
    rag_root = args.rag_repo.resolve()
    corpus_path = rag_root / "fixtures/integration_demo/corpus.json"
    pin = hashlib.sha256(corpus_path.read_bytes()).hexdigest()
    output = args.output.resolve()
    if output.exists() and any(output.iterdir()):
        raise SystemExit("Output must be empty; previous acceptance records are never overwritten")
    output.mkdir(parents=True, exist_ok=True)
    token, agent_token = secrets.token_urlsafe(40), secrets.token_urlsafe(40)
    rag_port, agent_port, fault_port = free_port(), free_port(), free_port()
    rag_url = f"http://127.0.0.1:{rag_port}"
    agent_url = f"http://127.0.0.1:{agent_port}"
    endpoint = "/v1/experimental/demo/retrieve"
    env = {
        **os.environ,
        "RAG_DEMO_TOKEN": token,
        "RAG_DEMO_CORPUS_SHA256": pin,
        "RAG_DEMO_CORPUS_PATH": str(corpus_path),
        "PYTHONIOENCODING": "utf-8",
    }
    log = (output / "rag-service.log").open("w", encoding="utf-8")
    rag_process = subprocess.Popen(
        [
            str(rag_root / ".venv/Scripts/python.exe"),
            str(rag_root / "tools/demo_server.py"),
            "--port",
            str(rag_port),
        ],
        cwd=rag_root,
        env=env,
        stdout=log,
        stderr=subprocess.STDOUT,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    settings = Settings(
        _env_file=None,
        app_env="test",
        persistence_backend="memory",
        dry_run=True,
        service_bearer_token=agent_token,
        orchestration_mode="agent",
        prompt_bundle=args.prompt_bundle,
        business_grounding_enabled=args.business_grounding,
        deepseek_api_key=existing.deepseek_api_key,
        deepseek_base_url=existing.deepseek_base_url,
        agent_model=existing.agent_model or existing.planner_model,
        planner_model=existing.planner_model,
        request_timeout_seconds=180,
        agent_model_timeout_seconds=90,
        planner_max_tokens=2048,
        knowledge_backend="remote_rag",
        knowledge_rag_url=rag_url + endpoint,
        knowledge_rag_token=token,
        knowledge_rag_corpus_sha256=pin,
        doctors_enabled=False,
        scene_enabled=False,
    )
    app = create_app(settings)
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=agent_port, log_level="warning")
    )
    server_task = asyncio.create_task(server.serve())
    fault = FastAPI()
    fault_mode = {"value": "tamper"}

    @fault.post(endpoint)
    async def injected(request: Request):
        payload = await request.json()  # Read before delay, even when the client times out.
        if fault_mode["value"] == "timeout":
            await asyncio.sleep(2)
        async with httpx.AsyncClient(trust_env=False) as client:
            response = await client.post(
                rag_url + endpoint,
                json=payload,
                headers={
                    k: v
                    for k, v in request.headers.items()
                    if k.lower()
                    in {
                        "authorization",
                        "x-request-id",
                        "x-tenant-id",
                        "x-actor-role",
                        "x-patient-context",
                    }
                },
            )
        body = response.json()
        if fault_mode["value"] == "tamper" and body.get("evidence"):
            body["evidence"][0]["title"] = "被篡改的标题"
        return JSONResponse(body, status_code=response.status_code)

    fault_server = uvicorn.Server(
        uvicorn.Config(fault, host="127.0.0.1", port=fault_port, log_level="warning")
    )
    fault_task = asyncio.create_task(fault_server.serve())
    cases = []

    def save():
        report = {
            "generated_at": datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(),
            "execution": (
                "real model + separate real RAG process + TCP /v1/chat; controlled failures labeled"
            ),
            "model": settings.agent_model,
            "prompt_bundle": settings.prompt_bundle,
            "business_grounding_enabled": settings.business_grounding_enabled,
            "corpus_sha256": pin,
            "clinical_approval": False,
            "cases": cases,
        }
        (output / "cases.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    async def chat(case_id, query, expected, conversation=None, inputs=None):
        rid = "integration-" + uuid4().hex
        body = {
            "request_id": rid,
            "conversation_id": conversation or case_id + uuid4().hex,
            "user": "integration-reviewer",
            "inputs": inputs or {"tenantId": "integration-demo", "role": "operator"},
            "query": query,
            "response_mode": "blocking",
        }
        async with httpx.AsyncClient(trust_env=False, timeout=190) as client:
            response = await client.post(
                agent_url + "/v1/chat",
                json=body,
                headers={"Authorization": f"Bearer {agent_token}"},
            )
        snapshot = response.json()
        scope = TrustedScope(
            body["inputs"]["tenantId"], "integration-reviewer", role=body["inputs"]["role"]
        )
        record = await app.state.container.repository.run(
            scope.scope_hash,
            snapshot["run_id"],
        )
        evidence = []
        for result in record.results.values():
            for eid in result.evidence_ids:
                ev = await app.state.container.repository.evidence(record.scope_key, eid)
                if ev:
                    evidence.append(ev.model_dump(mode="json"))
        answer = "\n".join(e.payload["text"] for e in record.events if e.type == "answer_part")
        from langchain_core.messages import ToolMessage, messages_from_dict

        memory = await app.state.container.repository.conversation(
            scope.thread_id(body["conversation_id"])
        )
        history_evidence = []
        for message in messages_from_dict(memory.agent_messages):
            if not isinstance(message, ToolMessage) or not isinstance(message.content, str):
                continue
            try:
                payload = json.loads(message.content)
            except ValueError:
                continue
            eid = payload.get("evidence_id") if isinstance(payload, dict) else None
            if eid and eid not in {e["evidence_id"] for e in evidence + history_evidence}:
                ev = await app.state.container.repository.evidence(scope.scope_hash, eid)
                if ev:
                    history_evidence.append(ev.model_dump(mode="json"))
        row = {
            "case": case_id,
            "kind": "real_model_tcp",
            "input": body,
            "expected": expected,
            "http_status": response.status_code,
            "actual_status": record.status,
            "answer": answer,
            "trace": record.metrics,
            "tool_results": [r.model_dump(mode="json") for r in record.results.values()],
            "evidence": evidence,
            "history_evidence": history_evidence,
            "snapshot": snapshot,
            "result": "REVIEW_REQUIRED",
            "failure_reason": None,
        }
        cases.append(row)
        save()
        print(
            json.dumps(
                {
                    "case": case_id,
                    "status": record.status,
                    "tools": len(record.results),
                    "evidence": len(evidence),
                },
                ensure_ascii=False,
            ),
            flush=True,
        )
        return row, body["conversation_id"]

    headers = {
        "Authorization": f"Bearer {token}",
        "X-Tenant-ID": "integration-demo",
        "X-Actor-Role": "operator",
        "X-Patient-Context": "false",
    }
    base = {
        "query": "康复研究方案",
        "scope_key": "integration-demo",
        "corpus_version": settings.knowledge_rag_corpus_version,
    }
    try:
        await wait_ready(rag_url + "/health/live", rag_process)
        await wait_ready(agent_url + "/health/live")
        row, conv = await chat(
            "E01",
            "请检索合成康复研究方案甲，说明其计划主要结局，并引用原文来源。仅做技术演示。",
            "真实 RAG / 正确原文引用 / 不把计划说成结果",
        )
        await chat(
            "E02",
            "比较合成康复研究方案甲和方案乙在研究人群、干预方式和主要结局上的异同。请检索这三方面原文并逐项引用。说明没有哪些结果。",
            "两来源三维度六段证据；不足则说明",
        )
        await chat(
            "E03",
            "请检索 zznonexistentquasar 的知识。找不到证据就明确说明，不使用其他主题资料。",
            "no_evidence / 不编造来源",
        )
        await chat(
            "E04",
            "我没有提供患者身份或测量记录。我的实际步行速度是多少？能否从这两份合成研究方案推算？",
            "不补造患者数值；明确缺少实测输入",
        )
        old = settings.knowledge_rag_url
        settings.knowledge_rag_url = f"http://127.0.0.1:{free_port()}" + endpoint
        await chat(
            "E05-connection",
            "请检索合成康复研究方案甲的主要结局，并给出依据。",
            "真实连接失败；不声称检索成功",
        )
        settings.knowledge_rag_url = f"http://127.0.0.1:{fault_port}" + endpoint
        fault_mode["value"] = "timeout"
        settings.knowledge_rag_timeout_seconds = 0.25
        await chat(
            "E05-timeout",
            "请检索合成康复研究方案乙的主要结局，并给出依据。",
            "可控延迟注入 / rag_timeout / 不假成功",
        )
        settings.knowledge_rag_timeout_seconds = 5
        fault_mode["value"] = "tamper"
        await chat(
            "E07",
            "请检索合成康复研究方案甲的主要结局，并标明依据。",
            "真实检索响应经过可控标题篡改 / 签名拒绝 / 无 Evidence",
        )
        settings.knowledge_rag_url = old
        await chat(
            "E06-agent",
            "请检索合成康复研究方案甲的主要结局并引用来源。",
            "跨租户在客户端拒绝；服务端独立探针也拒绝",
            inputs={"tenantId": "other", "role": "operator"},
        )
        settings.knowledge_backend = "local_bm25"
        with tempfile.TemporaryDirectory(prefix="metaagent-rag-demo-") as folder:
            path = Path(folder) / "local.json"
            path.write_text(
                json.dumps(
                    {
                        "schema_version": "1.0",
                        "documents": [
                            {
                                "doc_id": "local-demo",
                                "domain": "help",
                                "version": "v1",
                                "title": "合成本地面板说明",
                                "source": "synthetic://local-demo",
                                "approved": True,
                                "approved_by": "technical-demo-author",
                                "sections": [
                                    {
                                        "section_id": "panel",
                                        "title": "面板使用方法",
                                        "text": "本地演示面板用于查看合成记录。",
                                    }
                                ],
                            }
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            settings.knowledge_corpus_path = str(path)
            await chat(
                "E08",
                "请检索面板使用方法，告诉我本地演示面板用途。",
                "关闭 remote 后 local BM25 可用 / 无 RAG 外连",
            )
        settings.knowledge_backend = "remote_rag"
        settings.knowledge_rag_top_k = 1
        await chat(
            "E09-agent",
            "请检索方案甲和方案乙的主要结局并比较，证据不足就明确说不足。",
            "每次最多一段；保持引用真实，不能强行补完整比较",
        )
        settings.knowledge_rag_top_k = 6
        await chat(
            "E10",
            "继续刚才方案甲的资料，主要结局是哪一项？引用同一文献，并区分计划与实测。",
            "多轮有效引用 / 原 Evidence 未过期可复用",
            conv,
        )
        scope = TrustedScope("integration-demo", "integration-reviewer", role="operator")
        for ev in row["evidence"]:
            await app.state.container.repository.delete(
                "evidence", scope.scope_hash, ev["evidence_id"]
            )
        await chat(
            "E10-expired",
            "方案甲的主要结局是什么？请重新确认资料并引用，区分计划与实测。",
            "旧 Evidence 已移除，须重查或拒绝引用；不得复用失效 Evidence",
            conv,
        )
        # Direct real service probes complement the natural-language cases.
        async with httpx.AsyncClient(trust_env=False, timeout=10) as client:
            for cid, body, change, expected in [
                ("E06-cross-tenant", base, {"X-Tenant-ID": "other"}, 403),
                ("E06-patient", base, {"X-Actor-Role": "patient"}, 403),
                ("E06-unapproved-scope", {**base, "scope_key": "medical-v3"}, {}, 403),
                ("E06-version", {**base, "corpus_version": "medical-v3"}, {}, 409),
                ("E06-unauthorized", base, {"Authorization": "Bearer wrong"}, 401),
                ("E09-top-k", {**base, "top_k": 99}, {}, 422),
                ("E09-budget", {**base, "max_chars": 600, "top_k": 6}, {}, 200),
            ]:
                response = await client.post(
                    rag_url + endpoint, json=body, headers={**headers, **change}
                )
                result = response.json()
                cases.append(
                    {
                        "case": cid,
                        "kind": "real_service_tcp",
                        "input": body,
                        "header_changes": {k: v for k, v in change.items() if k != "Authorization"},
                        "expected_http": expected,
                        "actual_http": response.status_code,
                        "service_response": result,
                        "result": "PASS" if response.status_code == expected else "FAIL",
                    }
                )
                save()
        print("saved cases.json; technical and answer-support review still required", flush=True)
    finally:
        save()
        server.should_exit = True
        fault_server.should_exit = True
        await asyncio.gather(server_task, fault_task, return_exceptions=True)
        rag_process.terminate()
        try:
            rag_process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            rag_process.kill()
            rag_process.wait(timeout=5)
        log.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--rag-repo", type=Path, required=True)
    parser.add_argument(
        "--output", type=Path, default=ROOT / "docs/acceptance/integration-2026-10-11"
    )
    parser.add_argument("--allow-existing-model", action="store_true")
    parser.add_argument("--prompt-bundle", default="builtin-v1")
    parser.add_argument("--business-grounding", action="store_true")
    asyncio.run(main(parser.parse_args()))
