"""Check the deployed request validation and real RAG adapter independently of model credentials."""

import json
import subprocess
from pathlib import Path

from meta_agent.domains.space_context import space_catalog

REMOTE = r"""
import asyncio,json,sys
from types import SimpleNamespace
from uuid import uuid4
import httpx
from langgraph.store.memory import InMemoryStore
from meta_agent.config import Settings
from meta_agent.contracts import TaskSpec,DomainError
from meta_agent.domains.portfolio import PortfolioKnowledgeAdapter
from meta_agent.infrastructure.repository import Repository
from meta_agent.orchestration.identity import trusted_scope_from_inputs

payload=json.load(sys.stdin)
async def main():
    settings=Settings()
    rows=[]
    for scene,domain,query in payload['cases']:
        scope=trusted_scope_from_inputs({'spaceId':payload['space'],'sceneId':scene},
            'space-adapter-check','local-dify-demo')
        ctx=SimpleNamespace(settings=settings,scope=scope,
            repository=Repository(InMemoryStore(),settings),
            record=SimpleNamespace(request_id='space-check-'+uuid4().hex,metrics={}))
        task=TaskSpec(task_id='knowledge',goal_ids=['query'],capability='knowledge.search',
            idempotency_key=uuid4().hex)
        result=await PortfolioKnowledgeAdapter().execute(task,{'query':query,'domain':domain},ctx)
        sources=result.outputs.get('citations',[])
        assert result.status in {'succeeded','partial'} and sources, domain
        if domain=='help':
            assert any('二楼' in view.fact.value for view in result.facts)
        else: assert any(source['page'] for source in sources)
        rows.append({'scene_id':scene,'domain':domain,'status':result.status,
            'source_count':len(sources),'citations':sources,
            'retrieval':ctx.record.metrics['knowledge_retrieval']})
    scopes=[row['retrieval'][0]['kb_ids'] for row in rows if row['domain']=='help']
    assert len(scopes)==2 and scopes[0]==scopes[1]
    ctx.scope=trusted_scope_from_inputs({'spaceId':'unknown-space'},'space-check','local-dify-demo')
    try: await PortfolioKnowledgeAdapter().execute(task,{'query':'check','domain':'help'},ctx)
    except DomainError as exc: assert exc.code=='knowledge_space_unavailable'
    else: raise AssertionError('unknown_space_fell_back_to_default_kb')
    async with httpx.AsyncClient(base_url='http://127.0.0.1:8080') as client:
        for inputs in [
            {'spaceId':payload['space'],'sceneId':'not-an-existing-room'},
            {'spaceId':'other-hospital','sceneId':payload['cases'][0][0]},
            {'sceneId':payload['cases'][0][0]},
        ]:
            response=await client.post('/v1/chat',headers={
                'Authorization':'Bearer '+settings.service_bearer_token},
                json={'request_id':uuid4().hex,'user':'space-check','query':'hello','inputs':inputs})
            assert response.status_code==422
    return {'adapter_checks':rows,'invalid_scene_rejections':3,'unknown_space_rejected':True}
print('SPACE_RUNTIME_RESULT='+json.dumps(asyncio.run(main()),ensure_ascii=True))
"""


def main():
    space = next(iter(space_catalog()))
    scenes = list(space_catalog()[space]["scenes"])
    payload = {
        "space": space,
        "cases": [
            ("接待大厅", "help", "岳阳医院二楼有哪些诊疗室？"),
            ("运动评估室", "help", "岳阳医院二楼有哪些诊疗室？"),
            (scenes[0], "health", "脑卒中立位平衡障碍的评定方法"),
        ],
    }
    completed = subprocess.run(
        ["docker", "exec", "-i", "meta_agent", "python", "-c", REMOTE],
        input=json.dumps(payload),
        capture_output=True,
        encoding="utf-8",
        timeout=180,
    )
    if completed.returncode:
        raise RuntimeError(completed.stderr[-2000:])
    for line in completed.stdout.splitlines():
        if line.startswith("SPACE_RUNTIME_RESULT="):
            result = json.loads(line.split("=", 1)[1])
            output = Path(".local/space-scene/runtime-checks.json")
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
            print(
                json.dumps(
                    {
                        "adapter_checks": [
                            {k: row[k] for k in ("domain", "status", "source_count")}
                            for row in result["adapter_checks"]
                        ],
                        "invalid_scene_rejections": result["invalid_scene_rejections"],
                        "unknown_space_rejected": result["unknown_space_rejected"],
                    }
                )
            )
            return
    raise RuntimeError("Runtime did not return validation results")


if __name__ == "__main__":
    main()
