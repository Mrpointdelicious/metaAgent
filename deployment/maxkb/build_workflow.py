"""
创建日期：2026-11-10
文件功能：离线生成 MaxKB 原生工作流配置，不作为 MaxKB 工具或服务运行。
"""

import argparse
import json
from pathlib import Path
from uuid import UUID

FLOORS = {"1": "一", "2": "二", "3": "三", "4": "四", "5": "五"}
ANY_FLOOR = r"(?is).*(?:[一二三四五六七八九十百零两0-9]+\s*[楼层]|\d+\s*[fF]).*"
UI_QUESTION = r"(?is).*(?:按钮|按键|麦克风|键盘|跳跃|奔跑|视角|设置|屏幕|界面|社交岛).*"


def build(bindings):
    """Bind approved knowledge IDs to one space and emit an editable native workflow."""
    space = bindings["space_id"]
    if not isinstance(space, str) or not space.strip():
        raise ValueError("space_id is required")
    for group in ("scene_knowledge_ids", "help_knowledge_ids"):
        if not bindings.get(group):
            raise ValueError(f"{group} must contain approved knowledge IDs")
        for value in bindings[group]:
            UUID(value)
    model = bindings.get("reranker_model_id")
    if model:
        UUID(model)
    nodes, edges = [], []

    def node(key, kind, data, fields=(), name=None):
        nodes.append(
            {
                "id": key,
                "type": kind,
                "x": 360 + len(nodes) % 5 * 420,
                "y": 300 + len(nodes) // 5 * 520,
                "properties": {
                    "stepName": name or key,
                    "width": 340,
                    "height": 300,
                    "condition": "OR",
                    "config": {
                        "fields": [{"label": f, "value": f} for f in fields],
                        "globalFields": [],
                    },
                    "node_data": data,
                },
            }
        )
        return nodes[-1]["properties"]

    def edge(source, target, branch=None):
        edges.append(
            {
                "id": f"edge-{len(edges)}",
                "type": "app-edge",
                "sourceNodeId": source,
                "targetNodeId": target,
                "sourceAnchorId": f"{source}{'_' + branch if branch else ''}_right",
                "targetAnchorId": f"{target}_left",
            }
        )

    def check(field, compare, value):
        return {"field": field, "compare": compare, "value": value}

    def condition(key, branches):
        props = node(
            key,
            "condition-node",
            {
                "branch": [
                    {
                        "id": bid,
                        "type": "ELSE" if not rows else "IF",
                        "condition": mode,
                        "conditions": rows,
                    }
                    for bid, mode, rows in branches
                ]
            },
            ("branch_name",),
        )
        props["branch_condition_list"] = [
            {"index": i, "id": branch[0], "height": 100} for i, branch in enumerate(branches)
        ]

    def assign(key, field, value=None, reference=None):
        variable = {
            "name": field,
            "fields": ["global", field],
            "type": "string",
            "source": "referencing" if reference else "custom",
        }
        variable.update({"reference": reference} if reference else {"value": value})
        node(key, "variable-assign-node", {"variable_list": [variable]})

    def published(key, group):
        node(
            key,
            "search-document-node",
            {
                "knowledge_id_list": bindings[group],
                "search_mode": "custom",
                "search_scope_type": "custom",
                "search_condition_type": "AND",
                "search_condition_list": [
                    {"key": "release", "compare": "eq", "value": "published"},
                    {"key": "space_id", "compare": "eq", "value": space},
                ],
            },
            ("document_list", "knowledge_list"),
        )

    base = node(
        "base-node",
        "base-node",
        {
            "name": bindings.get("name", "MetaAgent native knowledge search"),
            "desc": (
                "Native tags, knowledge search and reranking; knowledge-search-v1 evidence output."
            ),
            "prologue": "",
        },
    )
    inputs = [
        ("domain", "string"),
        ("space_id", "string"),
        ("max_context_tokens", "number"),
        ("request_id", "string"),
    ]
    base["api_input_field_list"] = [
        {"field": field, "label": field, "type": kind, "required": True} for field, kind in inputs
    ]
    base["user_input_field_list"] = []
    start = node("start-node", "start-node", {}, ("question",))
    start["config"]["globalFields"] = [
        {"label": field, "value": field}
        for field in [
            *(f for f, _ in inputs),
            "chat_id",
            "document_scope",
            *("floor_" + f for f in FLOORS),
        ]
    ]
    condition(
        "scope-guard",
        [
            (
                "allowed",
                "and",
                [
                    check(["global", "space_id"], "eq", space),
                    check(["global", "domain"], "regex", "help|product"),
                ],
            ),
            ("denied", "and", []),
        ],
    )
    node(
        "denied",
        "reply-node",
        {
            "reply_type": "custom",
            "is_result": True,
            "content": '{"error":{"code":"knowledge_access_denied","retryable":false},'
            '"request_id":{{ global.request_id|tojson }}}',
        },
        ("answer",),
    )
    edge("start-node", "scope-guard")
    edge("scope-guard", "denied", "denied")
    condition(
        "topic",
        [
            ("floor", "and", [check(["start-node", "question"], "regex", ANY_FLOOR)]),
            ("help", "and", [check(["start-node", "question"], "regex", UI_QUESTION)]),
            ("scene", "and", []),
        ],
    )
    edge("scope-guard", "topic", "allowed")
    published("scene-published", "scene_knowledge_ids")
    published("help-published", "help_knowledge_ids")
    edge("topic", "scene-published", "floor")
    edge("topic", "scene-published", "scene")
    edge("topic", "help-published", "help")
    assign("help-scope", "document_scope", reference=["help-published", "document_list"])
    edge("help-published", "help-scope")
    edge("help-scope", "search")

    # Explicit native regex conditions avoid a classifier LLM for ordinary floor questions.
    node(
        "reset-floors",
        "variable-assign-node",
        {
            "variable_list": [
                {
                    "name": "floor_" + f,
                    "fields": ["global", "floor_" + f],
                    "type": "string",
                    "source": "custom",
                    "value": "__none__",
                }
                for f in FLOORS
            ]
        },
    )
    edge("scene-published", "reset-floors")
    for index, (number, chinese) in enumerate(FLOORS.items()):
        key = "floor-" + number
        following = "floor-" + str(index + 2) if index < len(FLOORS) - 1 else "has-floor"
        regex = (
            rf"(?is).*(?<![零一二三四五六七八九十百0-9])"
            rf"(?:{number}\s*(?:楼|层|f)|{chinese}\s*[楼层]).*"
        )
        condition(
            key,
            [
                ("yes", "and", [check(["start-node", "question"], "regex", regex)]),
                ("no", "and", []),
            ],
        )
        assign("set-" + number, "floor_" + number, number)
        edge(key, "set-" + number, "yes")
        edge(key, following, "no")
        edge("set-" + number, following)
    edge("reset-floors", "floor-1")
    condition(
        "has-floor",
        [
            ("yes", "or", [check(["global", "floor_" + f], "not_eq", "__none__") for f in FLOORS]),
            ("no", "and", []),
        ],
    )
    node(
        "floor-documents",
        "search-document-node",
        {
            "knowledge_id_list": [],
            "search_mode": "custom",
            "search_scope_type": "referencing",
            "search_scope_source": "document",
            "search_scope_reference": ["scene-published", "document_list"],
            "search_condition_type": "OR",
            "search_condition_list": [
                {"key": "floor", "compare": "eq", "value": "{{ global.floor_" + f + " }}"}
                for f in FLOORS
            ],
        },
        ("document_list", "knowledge_list"),
    )
    edge("has-floor", "floor-documents", "yes")
    assign("floor-scope", "document_scope", reference=["floor-documents", "document_list"])
    edge("floor-documents", "floor-scope")
    edge("floor-scope", "search")
    unresolved = (
        r"(?is).*(?:这里|那里|这层|本层|当前楼层|"
        r"[一二三四五六七八九十百零两0-9]+\s*[楼层]|\d+\s*f).*"
    )
    condition(
        "missing-target",
        [
            ("gap", "and", [check(["start-node", "question"], "regex", unresolved)]),
            ("general", "and", []),
        ],
    )
    edge("has-floor", "missing-target", "no")
    node(
        "target-gap",
        "reply-node",
        {
            "reply_type": "custom",
            "is_result": True,
            "content": '{{ {"status":"no_evidence","context":"","sources":[], '
            '"gaps":["请明确楼层，或该楼层尚无已发布资料。"],'
            '"trace_id":global.chat_id}|tojson }}',
        },
        ("answer",),
    )
    edge("missing-target", "target-gap", "gap")
    assign("general-scope", "document_scope", reference=["scene-published", "document_list"])
    edge("missing-target", "general-scope", "general")
    edge("general-scope", "search")
    node(
        "search",
        "search-knowledge-node",
        {
            "knowledge_id_list": [],
            "search_scope_type": "referencing",
            "search_scope_source": "document",
            "search_scope_reference": ["global", "document_scope"],
            "knowledge_setting": {
                "top_n": 12,
                "similarity": bindings.get("similarity", 0.25),
                "search_mode": "blend",
                "max_paragraph_char_number": 12000,
            },
            "question_reference_address": ["start-node", "question"],
            "show_knowledge": False,
        },
        ("paragraph_list", "data"),
    )
    if model:
        node(
            "evidence",
            "reranker-node",
            {
                "reranker_setting": {
                    "top_n": 4,
                    "similarity": bindings.get("rerank_similarity", 0.1),
                    "max_paragraph_char_number": 12000,
                },
                "question_reference_address": ["start-node", "question"],
                "reranker_model_id": model,
                "reranker_model_id_type": "custom",
                "reranker_reference_list": [["search", "paragraph_list"]],
                "show_knowledge": False,
            },
            ("result_list",),
            name="Evidence",
        )
    else:
        node(
            "evidence",
            "variable-assign-node",
            {
                "variable_list": [
                    {
                        "name": "raw_evidence",
                        "fields": ["global", "raw_evidence"],
                        "source": "referencing",
                        "reference": ["search", "paragraph_list"],
                        "type": "json",
                    }
                ]
            },
        )
    reply = Path(__file__).with_name("knowledge_reply.jinja").read_text(encoding="utf-8")
    if not model:
        start["config"]["globalFields"].append({"label": "raw_evidence", "value": "raw_evidence"})
        reply = reply.replace("Evidence.result_list", "global.raw_evidence")
    node(
        "reply",
        "reply-node",
        {"reply_type": "custom", "is_result": True, "content": reply},
        ("answer",),
    )
    edge("search", "evidence")
    edge("evidence", "reply")
    return {"nodes": nodes, "edges": edges}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bindings", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    flow = build(json.loads(args.bindings.read_text(encoding="utf-8")))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(flow, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Native workflow written: {len(flow['nodes'])} nodes, {len(flow['edges'])} edges")


if __name__ == "__main__":
    main()
