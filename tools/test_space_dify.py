"""Exercise space-scoped retrieval and the current room through the existing published Dify app."""

import argparse
import json
from pathlib import Path

import httpx

from meta_agent.domains.space_context import space_catalog
from tools.test_portfolio_dify import APP_CODE, APP_ID, chat

OUTPUT = Path(".local/space-scene")


def check_context(client, space):
    conversation = None
    results = []
    for scene in ("接待大厅", "数据中心"):
        result = chat(
            client,
            "请依据本轮传入的当前位置，告诉我现在在哪个房间，几楼？",
            conversation,
            inputs={"spaceId": space, "sceneId": scene},
        )
        assert result["agent_status"] in {"succeeded", "partial"}
        assert scene in result["answer"], "Current room did not change within the conversation"
        if conversation:
            assert result["conversation_id"] == conversation
        conversation = result["conversation_id"]
        results.append(result)
    default = chat(client, "请检索医院知识库，二楼有哪些诊疗室？", inputs={})
    assert default["agent_status"] in {"succeeded", "partial"} and default["retrievals"]
    assert all(r["space_id"] == space and r["scene_id"] is None for r in default["retrievals"])
    (OUTPUT / "published-context-checks.json").write_text(
        json.dumps(
            {"room_changes": results, "default_space": default}, ensure_ascii=False, indent=2
        ),
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                "same_conversation_room_change": "passed",
                "default_space": "passed",
            }
        )
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--context", action="store_true", help="Check room changes and default inputs"
    )
    args = parser.parse_args()
    OUTPUT.mkdir(parents=True, exist_ok=True)
    space = next(iter(space_catalog()))
    report = {"app_id": APP_ID, "checks": []}
    with httpx.Client(
        base_url="http://localhost", timeout=180, headers={"X-App-Code": APP_CODE}
    ) as client:
        passport = client.get("/api/passport")
        passport.raise_for_status()
        client.headers["X-App-Passport"] = passport.json()["access_token"]
        if args.context:
            check_context(client, space)
            return
        cases = [
            (
                "hospital-cross-room",
                "接待大厅",
                "我现在在哪个房间？请检索医院知识库，告诉我二楼有哪些诊疗室。",
                "help",
            ),
            (
                "changed-current-room",
                "运动评估室",
                "我现在在哪个房间、几楼？请检索医院知识库，告诉我三楼有哪些房间。",
                "help",
            ),
            (
                "medical-literature",
                "运动评估室",
                "请检索康复医学文献，说明脑卒中立位平衡障碍的评定方法，并给出文献标题和PDF页码。",
                "health",
            ),
        ]
        for name, scene, query, domain in cases:
            result = chat(client, query, inputs={"spaceId": space, "sceneId": scene})
            (OUTPUT / f"published-{name}.json").write_text(
                json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            assert result["agent_status"] in {"succeeded", "partial"}, name
            assert result["retrievals"], name
            assert all(
                r["space_id"] == space and r["scene_id"] == scene for r in result["retrievals"]
            ), name
            selected = [r for r in result["retrievals"] if r["domain"] == domain]
            assert selected and any(r["sources"] for r in selected), name
            if domain == "help":
                assert scene in result["answer"], name
                assert any(
                    source["source_locator"].startswith(("yueyang/", "eval_sources/yueyang/"))
                    for row in selected
                    for source in row["sources"]
                ), name
            else:
                assert any(source.get("page") for row in selected for source in row["sources"])
            summary = {
                "case": name,
                "workflow_run_id": result["workflow_run_id"],
                "status": result["agent_status"],
                "domain": domain,
                "source_count": sum(len(row["sources"]) for row in selected),
            }
            report["checks"].append(summary)
            print(json.dumps(summary), flush=True)
    (OUTPUT / "published-checks.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )


if __name__ == "__main__":
    main()
