"""
创建日期：2026-10-21
文件功能：备份并更新指定 Dify 工作流的知识来源展示，通过 Dify 服务发布和测试。

Uses existing accounts, API tokens and workflow services; never prints credentials.
Backups and raw responses stay in the ignored local output directory.
"""

import argparse
import hashlib
import json
import subprocess
from pathlib import Path

APP_ID = "747e56ab-5f99-45ac-b511-a2b960f249c5"
OUTPUT = Path(".local/portfolio-dify")

REMOTE = r"""
import hashlib,json,sys
from app import app
from extensions.ext_database import db
from sqlalchemy import select
from sqlalchemy.orm import Session
from models import App
from models.account import Account
from models.workflow import Workflow
from services.workflow_service import WorkflowService
from libs.datetime_utils import naive_utc_now

args=json.load(sys.stdin)
with app.app_context():
    model=db.session.get(App,args['app_id'])
    if model is None:raise RuntimeError('target_app_missing')
    account=db.session.get(Account,model.created_by)
    account.set_tenant_id(model.tenant_id)
    service=WorkflowService()
    draft=service.get_draft_workflow(app_model=model)
    if args['action']=='inspect':
        result={'app_id':model.id,'workflow_id':model.workflow_id,'draft_id':draft.id,
                'graph':draft.graph_dict,'features':draft.features_dict}
    elif args['action']=='update':
        actual=hashlib.sha256(json.dumps(draft.graph_dict,sort_keys=True).encode()).hexdigest()
        if actual!=args['expected_hash']:raise RuntimeError('workflow_changed_since_backup')
        saved=service.sync_draft_workflow(app_model=model,graph=args['graph'],features=draft.features_dict,
            unique_hash=draft.unique_hash,account=account,environment_variables=draft.environment_variables,
            conversation_variables=draft.conversation_variables)
        result={'draft_id':saved.id,'status':'saved'}
    elif args['action']=='publish':
        with Session(db.engine) as session:
            published=service.publish_workflow(session=session,app_model=model,account=account,
                marked_name=args.get('marked_name','RAG知识库联调'),
                marked_comment=args.get('marked_comment','接入现有版本知识库，展示可核对的文献标题和PDF页码'))
            saved=session.get(App,model.id)
            saved.workflow_id=published.id
            saved.updated_by=account.id
            saved.updated_at=naive_utc_now()
            session.commit()
            result={'workflow_id':published.id,'status':'published'}
    elif args['action']=='test-draft':
        from services.app_generate_service import AppGenerateService
        from core.app.entities.app_invoke_entities import InvokeFrom
        with app.test_request_context('/'):
            result=AppGenerateService.generate(app_model=model,user=account,
                args={'inputs':args.get('inputs',{}),'query':args['query'],'files':[]},
                invoke_from=InvokeFrom.DEBUGGER,streaming=True)
            if not isinstance(result,dict):
                events=[]
                for chunk in result:
                    if isinstance(chunk,dict):events.append(chunk)
                    elif isinstance(chunk,(str,bytes)):
                        if isinstance(chunk,bytes):chunk=chunk.decode()
                        for line in chunk.splitlines():
                            if line.startswith('data: '):events.append(json.loads(line[6:]))
                    if (events and events[-1].get('event') in
                            ('workflow_finished','message_end','error')):
                        break
                result={'events':events,'answer':''.join(e.get('answer','') for e in events
                    if e.get('event') in ('message','agent_message')),
                    'workflow':next((e.get('data') for e in reversed(events)
                        if e.get('event')=='workflow_finished'),{}),
                    'metadata':next((e.get('metadata') for e in reversed(events)
                        if e.get('event')=='message_end'),{})}
                if not result['answer']:
                    result['answer']=(result['workflow'].get('outputs') or {}).get('answer','')
    else:raise RuntimeError('unsupported_action')
    print('DIFY_INTEGRATION_RESULT='+json.dumps(result,ensure_ascii=False,default=str))
"""

SOURCE_FOOTER = """    # Render verified retrieval provenance separately from model-authored claims.
    source_lines = []
    seen_sources = set()
    for retrieval in (data.get("metrics") or {}).get("knowledge_retrieval", []):
        for source in retrieval.get("sources", []):
            key = (source.get("version_id"), source.get("page"))
            if key in seen_sources:
                continue
            seen_sources.add(key)
            title = str(source.get("title") or "文献").replace("\\n", " ")
            title = title.replace("<", "＜").replace(">", "＞")
            title = title.replace("[", "［").replace("]", "］")
            page = source.get("page")
            location = "PDF 第 " + str(page) + " 页" if page else "原文分段"
            source_lines.append("- " + title + "（" + location + "）")
    if source_lines:
        answer += "\\n\\n**本次检索资料**\\n\\n" + "\\n".join(source_lines[:12])

"""


def remote(action, **values):
    completed = subprocess.run(
        ["docker", "exec", "-i", "docker-api-1", "python", "-c", REMOTE],
        input=json.dumps({"action": action, "app_id": APP_ID, **values}),
        capture_output=True,
        encoding="utf-8",
        timeout=240,
    )
    if completed.returncode:
        raise RuntimeError(completed.stderr[-2500:])
    marker = "DIFY_INTEGRATION_RESULT="
    for line in reversed(completed.stdout.splitlines()):
        if line.startswith(marker):
            return json.loads(line[len(marker) :])
    raise RuntimeError("Dify service did not return a result")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["update", "publish", "test-draft"])
    parser.add_argument(
        "--query", default="请检索知识库，说明脑卒中立位平衡障碍的评定方法，并标明文献和PDF页码。"
    )
    args = parser.parse_args()
    OUTPUT.mkdir(parents=True, exist_ok=True)
    if args.action == "update":
        before = remote("inspect")
        backup = OUTPUT / "workflow-before.json"
        if not backup.exists():
            backup.write_text(json.dumps(before, ensure_ascii=False, indent=2), encoding="utf-8")
        graph = before["graph"]
        expected = hashlib.sha256(json.dumps(graph, sort_keys=True).encode()).hexdigest()
        node = next(n for n in graph["nodes"] if n["id"] == "present")
        code = node["data"]["code"]
        if "# Render verified retrieval provenance" not in code:
            anchor = '    status = str(data.get("status") or "unavailable")'
            if code.count(anchor) != 1:
                raise RuntimeError("Unexpected presentation code; review before editing")
            node["data"]["code"] = code.replace(anchor, SOURCE_FOOTER + anchor)
        node["data"]["desc"] = "回答后展示知识服务返回的真实文献标题和PDF页码。"
        invoke = next(n for n in graph["nodes"] if n["id"] == "invoke")
        invoke["data"]["desc"] = "MetaAgent search_knowledge 已接入现有 rag-portfolio 知识库。"
        compile(node["data"]["code"], "dify-present", "exec")
        result = remote("update", graph=graph, expected_hash=expected)
        (OUTPUT / "workflow-after.json").write_text(
            json.dumps(graph, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    elif args.action == "test-draft":
        result = remote("test-draft", query=args.query)
        (OUTPUT / "draft-response.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        assert result.get("workflow", {}).get("status") == "succeeded"
        assert "本次检索资料" in result.get("answer", ""), (
            "No live knowledge provenance in draft answer"
        )
        print(
            json.dumps(
                {
                    "workflow_run_id": result["workflow"]["id"],
                    "status": "succeeded",
                    "has_source_footer": True,
                },
                ensure_ascii=False,
            )
        )
        return
    else:
        result = remote("publish")
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
