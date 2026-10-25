"""Update the existing Dify app from reviewable request source and the RAG scene snapshot."""

import argparse
import hashlib
import json
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path

from meta_agent.domains.space_context import space_catalog
from tools.integrate_portfolio_dify import remote

OUTPUT = Path(".local/space-scene")
SOURCE = Path(__file__).resolve().parents[1] / "workflows/dify/prepare_request.py"


def build_graph(graph: dict) -> dict:
    graph = deepcopy(graph)
    catalog = space_catalog()
    nodes = {node["id"]: node for node in graph["nodes"]}
    start = nodes["start"]["data"]
    variables = [v for v in start["variables"] if v["variable"] not in {"spaceId", "sceneId"}]
    variables.extend(
        [
            {
                "variable": "spaceId",
                "label": "空间 ID",
                "type": "select",
                "required": False,
                "default": next(iter(catalog)),
                "options": list(catalog),
                "max_length": 160,
                "description": "用于选择医院知识库。",
            },
            {
                "variable": "sceneId",
                "label": "当前房间 / 场景",
                "type": "text-input",
                "required": False,
                "default": None,
                "max_length": 160,
                "description": "填写已发布的场景原始名称，服务端校验归属；只表示当前位置。",
            },
        ]
    )
    start["variables"] = variables
    start["title"] = "用户问题与当前场景"
    prepare = nodes["prepare"]["data"]
    code = SOURCE.read_text(encoding="utf-8")
    compile(code, str(SOURCE), "exec")
    prepare["code"] = code
    prepare["title"] = "准备可信请求并校验场景"
    prepare["desc"] = "医院知识使用spaceId；医疗文献全局检索；场景由服务端校验当前发布目录。"
    prepare["variables"] = [
        v for v in prepare["variables"] if v["variable"] not in {"space_id", "scene_id"}
    ] + [
        {"variable": "space_id", "value_selector": ["start", "spaceId"]},
        {"variable": "scene_id", "value_selector": ["start", "sceneId"]},
    ]
    nodes["invoke"]["data"]["title"] = "调用 MetaAgent"
    nodes["invoke"]["data"]["desc"] = (
        "医疗文献不依赖spaceId；医院知识按spaceId选择，sceneId提供当前位置。"
    )
    return graph


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["update", "publish", "test-draft"])
    parser.add_argument("--scene", default="接待大厅")
    parser.add_argument("--query", default="我现在在哪里？请检索医院知识库，说明二楼有哪些诊疗室。")
    args = parser.parse_args()
    OUTPUT.mkdir(parents=True, exist_ok=True)
    if args.action == "update":
        before = remote("inspect")
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S")
        (OUTPUT / f"workflow-before-{stamp}.json").write_text(
            json.dumps(before, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        expected = hashlib.sha256(json.dumps(before["graph"], sort_keys=True).encode()).hexdigest()
        graph = build_graph(before["graph"])
        result = remote("update", graph=graph, expected_hash=expected)
        (OUTPUT / "workflow-after.json").write_text(
            json.dumps(graph, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    elif args.action == "test-draft":
        result = remote(
            "test-draft",
            query=args.query,
            inputs={"spaceId": next(iter(space_catalog())), "sceneId": args.scene},
        )
        (OUTPUT / "draft-response.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        from tools.test_portfolio_dify import lookup

        workflow = result.get("workflow", {})
        assert workflow.get("status") == "succeeded", "Dify workflow failed"
        agent = lookup(workflow["id"])
        assert agent["agent_status"] in {"succeeded", "partial"}, "MetaAgent run failed"
        assert agent["retrievals"], "No current knowledge retrieval in the draft run"
        assert all(
            row.get("domain") == "health"
            or (
                row.get("space_id") == next(iter(space_catalog()))
                and row.get("scene_id") == args.scene
            )
            for row in agent["retrievals"]
        ), "Draft did not preserve the trusted space/scene context"
        print(
            json.dumps(
                {"workflow_status": workflow["status"], "agent_status": agent["agent_status"]}
            )
        )
        return
    else:
        result = remote(
            "publish",
            marked_name="全局医疗检索与已发布场景",
            marked_comment="医疗文献独立于 spaceId；医院资料按空间检索，场景动态校验 RAG 发布目录",
        )
    print(json.dumps(result, ensure_ascii=True))


if __name__ == "__main__":
    main()
