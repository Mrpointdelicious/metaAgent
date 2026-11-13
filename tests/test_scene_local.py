"""
创建日期：2026-11-13
文件功能：验证场景指令本地解析器（直连点位库、不经过 AI_WebApi）的契约与话术回归。

点位夹具 tests/fixtures/scene_points.json 取自真实空间 version=138，
共 110 个点位（传送 20 / 场景 36 / 点击 50 / 游戏 4）。
话术集与 metaAgent-feature-runtime-cutover 的实测集一致，共 348 条。
"""

import asyncio
import json
import re
from collections import defaultdict
from pathlib import Path

import httpx
import pytest

from meta_agent.agent.tools import invoke_tool
from meta_agent.domains import scene_local
from meta_agent.domains.scene import COMMAND, numeric_envelope, source_text
from meta_agent.domains.scene_instruction import SceneIntentParser
from meta_agent.domains.scene_local import (
    MIN_MATCH_SCORE,
    build_commands,
    envelope,
    resolve_local_scene,
)
from meta_agent.tools.ai_webapi import AIWebApiClient
from tests.helpers.integration import context
from tests.helpers.runtime import runtime
from tests.helpers.settings import AppTestSettings

POINTS = json.loads(
    (Path(__file__).parent / "fixtures" / "scene_points.json").read_text(encoding="utf-8")
)
BY_PREFIX = defaultdict(list)
for _point in POINTS:
    BY_PREFIX[_point["prefix"]].append(_point)
SCENE = BY_PREFIX["[ScenePoint:]"]
CLICK = BY_PREFIX["[ClickPoint:]"]
TELEPOR = BY_PREFIX["[Telepor:]"]
GAME = BY_PREFIX["[GamePoint:]"]
BY_CODE = {p["prefix"] + p["id"]: p for p in POINTS}


def build_cases():
    groups = {}
    groups["G1 历史报障话术"] = [
        "带我去四楼并介绍五楼并打开对话记录",
        "带我回四楼", "带我回社交岛", "我想去四楼", "我想回四楼",
        "回上一个场景", "返回上一个场景", "退回上一个场景",
    ]
    floors = ["一楼", "二楼", "三楼", "四楼", "五楼"]
    templates = [
        "去{}", "到{}", "带我去{}", "请带我去{}", "带我到{}", "带我回{}",
        "我想去{}", "我想回{}", "我要去{}", "我要回{}", "回{}", "回到{}",
        "返回{}", "退回{}", "前往{}", "进入{}", "我想去{}看看",
    ]
    groups["G2 楼层简称口语"] = [t.format(f) for f in floors for t in templates]
    groups["G2 楼层简称口语"] += [
        "去4楼", "去5楼", "上四楼", "四楼", "五楼",
        "去四楼评估室", "去四楼1号评估室", "去二楼问诊1号房间",
    ]
    groups["G3 场景点位全名"] = [f"去{p['name']}" for p in SCENE] + [
        f"带我去{p['name']}" for p in SCENE
    ]
    groups["G4 点击点位原样"] = [p["name"] for p in CLICK] + [
        f"打开{p['name'].removeprefix('打开').removeprefix('关闭')}" for p in CLICK
    ]
    groups["G5 传送与游戏点位"] = [f"去{p['name']}" for p in TELEPOR] + [
        f"传送到{p['name']}" for p in TELEPOR[:5]
    ] + [f"打开{p['name']}" for p in GAME] + [f"玩{p['name']}" for p in GAME]
    groups["G6 上个场景系列"] = [
        "退回上个场景", "退回上一个场景", "返回上个场景", "回上个场景",
        "上一个场景", "回到上一个场景", "返回上一个场景", "回上一个场景",
        "退回上一场景", "后退一个场景", "回到上个场景",
    ]
    groups["G7 挂号叫号系列"] = [
        "患者确认挂号", "患者挂号返回", "确认挂号", "取消挂号",
        "挂号确认", "挂号取消", "患者确认叫号", "患者拒绝叫号",
        "确认叫号", "取消叫号", "医生叫号",
    ]
    groups["G8 多指令"] = [
        "带我去四楼并打开个人中心",
        "去五楼，然后打开训练报告",
        "打开个人中心并关闭摄像头",
        "带我去四楼并介绍五楼并打开对话记录",
        "去二楼，然后去四楼",
        "打开个人中心、关闭投屏并去五楼",
        "带我去社交岛并且打开设备管理",
        "去三楼训练区，顺便打开摄像头",
    ]
    groups["G9 负例与问句"] = [
        "五楼有什么", "介绍一下五楼", "怎么去四楼", "四楼是干什么的",
        "今天天气怎么样", "随便聊聊", "打开地图说明书", "打开地图123",
        "打开史", "你好", "谢谢你", "我要喝水",
    ]
    return groups


CASES = build_cases()
NEGATIVE_QUERIES = CASES["G9 负例与问句"]
# 多意图句里含知识问题/历史对话工具的部分本来就不该执行，允许部分命中。
PARTIAL_OK = {"带我去四楼并介绍五楼并打开对话记录"}


def _state(commands):
    hits = [c for c in commands if c["status"] == "matched"]
    return "全命中" if len(hits) == len(commands) else ("部分命中" if hits else "未命中")


def test_real_catalog_covers_all_348_phrases():
    """除负例与含知识问题的多意图句外，全量话术必须命中且不低于最低分。"""
    failures = []
    summary = {}
    for group, queries in CASES.items():
        states = defaultdict(int)
        for query in queries:
            commands = build_commands(query, POINTS)
            state = _state(commands)
            states[state] += 1
            if group == "G9 负例与问句":
                if state != "未命中":
                    failures.append(f"{group} | {query} | 负例被误执行: {commands}")
            elif query in PARTIAL_OK:
                if state == "未命中":
                    failures.append(f"{group} | {query} | 首段导航未命中")
            elif state != "全命中":
                failures.append(f"{group} | {query} | {state}: {commands}")
            for command in commands:
                if command["status"] == "matched" and command["score"] < MIN_MATCH_SCORE:
                    failures.append(f"{group} | {query} | 低分命中 {command['score']}")
        summary[group] = dict(states)
    assert len(POINTS) == 110
    assert sum(len(q) for q in CASES.values()) == 348
    assert not failures, "匹配回归失败:\n" + "\n".join(failures[:20])
    assert summary["G9 负例与问句"] == {"未命中": 12}


def test_floor_phrases_never_land_on_the_wrong_floor():
    """曾因拼音退化成 lou 全部错跑到二楼；这里逐条校验落点楼层。"""
    digits = {"一": "1", "二": "2", "两": "2", "三": "3", "四": "4",
              "五": "5", "六": "6", "七": "7", "八": "8", "九": "9"}
    wrong = []
    for query in CASES["G2 楼层简称口语"]:
        match = re.search(r"([一二两三四五六七八九0-9])楼", query)
        if not match:
            continue
        want = int(digits.get(match.group(1), match.group(1)))
        for command in build_commands(query, POINTS):
            if command["status"] != "matched":
                continue
            name = BY_CODE[command["result"]]["name"]
            got = scene_local._floor_of(name)
            if got != want:
                wrong.append(f"{query} -> {command['result']} {name} (期望{want}楼, 实际{got})")
    assert not wrong, "楼层错配:\n" + "\n".join(wrong[:20])


def test_every_point_name_recalls_itself():
    """110 个点位用各自原名都能召回自己，目录本身无缺口。"""
    missed = [
        p["name"]
        for p in POINTS
        if not any(
            c["status"] == "matched" and c["result"] == p["prefix"] + p["id"]
            for c in build_commands(p["name"], POINTS)
        )
    ]
    assert not missed, f"点位名无法召回自己: {missed[:10]}"


def test_reported_failure_phrases_now_resolve():
    """用户实际报障的话术逐条落到正确点位。"""
    expected = {
        "带我回四楼": "[ScenePoint:]23",
        "带我回社交岛": "[Telepor:]0",
        "我想去四楼": "[ScenePoint:]23",
        "我想回四楼": "[ScenePoint:]23",
        "到四楼": "[ScenePoint:]23",
        "上四楼": "[ScenePoint:]23",
        "请带我去四楼": "[ScenePoint:]23",
        "玩记忆速配": "[GamePoint:]0",
    }
    for query, code in expected.items():
        commands = build_commands(query, POINTS)
        assert commands[0]["status"] == "matched", query
        assert commands[0]["result"] == code, (query, commands)


@pytest.mark.parametrize("query", CASES["G6 上个场景系列"])
def test_history_back_phrases_hit_click_point_33(query):
    """真实目录有 [ClickPoint:]33「退回上个场景」，不再硬性拒绝。"""
    commands = build_commands(query, POINTS)
    assert commands[0]["status"] == "matched", query
    assert commands[0]["result"] == "[ClickPoint:]33", (query, commands)


@pytest.mark.parametrize("query", NEGATIVE_QUERIES)
def test_negative_phrases_produce_no_executable_command(query):
    commands = build_commands(query, POINTS)
    assert all(c["status"] == "unmatched" and c["result"] is None for c in commands), query


def test_generated_commands_pass_the_existing_contract_chain():
    """本地生成的 commands 必须能过 spans 对齐与 SceneAdapter 的静态校验。"""
    parser = SceneIntentParser()
    failures = []
    for queries in CASES.values():
        for query in queries:
            commands = build_commands(query, POINTS)
            try:
                spans = parser.spans(query, commands)
                assert len(spans) == len(commands)
            except Exception as exc:
                failures.append(f"spans | {query} | {exc}")
            body = envelope("space", 138, commands)
            assert numeric_envelope(body)["version"] == 138
            for index, command in enumerate(commands, 1):
                if type(command["order"]) is not int or command["order"] != index:
                    failures.append(f"order | {query} | {command}")
                if command["status"] not in {"matched", "ambiguous", "unmatched"}:
                    failures.append(f"status | {query} | {command}")
                if command["status"] == "matched":
                    if not COMMAND.fullmatch(command["result"]):
                        failures.append(f"code | {query} | {command['result']}")
                elif command["result"] is not None:
                    failures.append(f"result must be None | {query} | {command}")
                if source_text(command["sourceText"]) == "":
                    failures.append(f"empty sourceText | {query}")
    assert not failures, "契约校验失败:\n" + "\n".join(failures[:20])


def test_pinyin_is_optional_and_does_not_change_results(monkeypatch):
    """生产镜像未装 pypinyin；有无该包，348 条结果必须完全一致。"""
    baseline = {
        query: [(c["status"], c["result"]) for c in build_commands(query, POINTS)]
        for queries in CASES.values()
        for query in queries
    }
    monkeypatch.setattr(scene_local, "_to_pinyin", lambda _s: "")
    for queries in CASES.values():
        for query in queries:
            assert [
                (c["status"], c["result"]) for c in build_commands(query, POINTS)
            ] == baseline[query], query


def _mock_transport(points):
    """模拟 /back/version 与 /back/query，记录所有请求 URL。"""
    seen = []
    sql_keys = [
        ("TeleportConfig", "[Telepor:]"),
        ("ScenePointCofig", "[ScenePoint:]"),
        ("CommandList", "[ClickPoint:]"),
        ("FastTaskConfig", "[GamePoint:]"),
    ]

    def handler(request):
        seen.append(str(request.url))
        if request.url.path == "/back/version":
            return httpx.Response(200, json={"data": 138})
        if request.url.path == "/back/query":
            sql = json.loads(request.content)["sql"]
            for key, prefix in sql_keys:
                if key in sql:
                    rows = [
                        {"id": int(p["id"]), "name": p["name"], "intro": p["intro"]}
                        for p in points
                        if p["prefix"] == prefix
                    ]
                    return httpx.Response(200, json={"data": {"result": rows}})
            return httpx.Response(200, json={"data": {"result": None}})
        return httpx.Response(404, json={})

    return httpx.MockTransport(handler), seen


def test_backend_uses_local_catalog_and_never_calls_webapi():
    """scene_local_enabled 默认开启：navigate_scene 只打 /back/*，不打 .NET 端点。"""

    async def check():
        transport, seen = _mock_transport(POINTS)
        settings = AppTestSettings(
            dry_run=False,
            ai_webapi_base_url="http://webapi.test/api/ai/patients/tools",
            ai_webapi_rehab_base_url="http://webapi.test/api/ai/rehab/tools",
        )
        assert settings.scene_local_enabled is True
        client = AIWebApiClient(settings, transport=transport)
        try:
            body = await client.post("navigate_scene", {"target": "带我回四楼", "spaceId": "space"})
        finally:
            await client.close()
        assert body["status"] == 200
        assert body["data"]["spaceId"] == "space"
        assert body["data"]["version"] == 138
        assert body["data"]["commands"][0]["result"] == "[ScenePoint:]23"
        assert seen and all("/back/" in url for url in seen), seen
        assert not any("/api/ai/rehab/tools" in url for url in seen), seen

    asyncio.run(check())


def test_scene_local_disabled_falls_back_to_webapi():
    """回滚开关：设为 false 时恢复请求 .NET 的 navigate_scene 端点。"""

    async def check():
        seen = []
        canned = envelope("space", 7, build_commands("去四楼", POINTS))

        def handler(request):
            seen.append(str(request.url))
            return httpx.Response(200, json=canned)

        settings = AppTestSettings(
            dry_run=False,
            scene_local_enabled=False,
            ai_webapi_rehab_base_url="http://webapi.test/api/ai/rehab/tools",
        )
        client = AIWebApiClient(settings, transport=httpx.MockTransport(handler))
        try:
            body = await client.post("navigate_scene", {"target": "去四楼", "spaceId": "space"})
        finally:
            await client.close()
        assert body == canned
        assert any(url.endswith("/api/ai/rehab/tools/navigate_scene") for url in seen), seen
        assert not any("/back/" in url for url in seen), seen

    asyncio.run(check())


def test_local_envelope_flows_through_scene_adapter_and_dispatch():
    """本地生成的封套能被既有 evidence / action_ref / dispatch 链完整消费。"""

    async def check():
        async with runtime(scene_actions_enabled=True) as (container, backend):
            phrase = "带我回四楼"
            backend.overrides["navigate_scene"] = envelope(
                "space", 7, build_commands(phrase, POINTS)
            )
            ctx = context(container, phrase)
            result = await invoke_tool(ctx, "navigate_scene", {"target": phrase})
            assert result["status"] == "succeeded", result
            assert len(ctx.record.action_delivery) == 1
            assert backend.calls[0][1]["target"] == phrase

    asyncio.run(check())


def test_resolve_local_scene_without_target_returns_empty_commands():
    async def check():
        transport, _seen = _mock_transport(POINTS)
        settings = AppTestSettings(dry_run=False)
        async with httpx.AsyncClient(transport=transport) as client:
            body = await resolve_local_scene(client, settings, {"target": "", "spaceId": "space"})
        assert body["data"]["commands"] == []
        assert body["data"]["version"] == 138

    asyncio.run(check())


def test_scale_instruction_maps_to_assessment_room():
    """量表→评估室映射（Dify code 3'）：量表名命中对应评估室点位。"""

    async def check():
        transport, _seen = _mock_transport(POINTS)
        settings = AppTestSettings(dry_run=False)
        async with httpx.AsyncClient(transport=transport) as client:
            body = await resolve_local_scene(
                client, settings, {"target": "我想做ADL量表", "spaceId": "space"}
            )
        commands = body["data"]["commands"]
        assert commands, body
        matched = [c for c in commands if c["status"] == "matched"]
        assert matched, body
        assert any("四楼2号评估室" in c["matchName"] for c in matched), body
        assert any("我想做ADL量表" in c["sourceText"] or c["sourceText"] for c in matched), body

    asyncio.run(check())


@pytest.mark.parametrize(
    "query",
    [
        "我想做ADL量表",
        "做ADL",
        "我想做巴氏指数评估",
        "做日常生活能力量表",
        "我想做MMSE",
        "做蒙特利尔认知评估",
    ],
)
def test_scale_phrases_resolve_to_expected_rooms(query):
    commands = build_commands(_scale_target(query), POINTS)
    assert commands, query
    names = " ".join(c["matchName"] for c in commands)
    rooms = "四楼1号评估室 四楼2号评估室 四楼6号评估室 四楼8号评估室"
    assert any(room in names for room in rooms.split()), (query, names)


def _scale_target(query: str) -> str:
    from meta_agent.domains.instruction_classify import classify_instruction, scale_to_room

    assert classify_instruction(query) == "1", query
    room = scale_to_room(query)
    assert room != "未知评估室", query
    return room


def test_time_record_query_returns_empty_commands():
    """带时间的记录请求（Dify code 1 的 "2"）不是场景跳转：返回空命令。"""

    async def check():
        transport, _seen = _mock_transport(POINTS)
        settings = AppTestSettings(dry_run=False)
        async with httpx.AsyncClient(transport=transport) as client:
            for query in ["查看今天的训练记录", "打开今天的训练记录", "修改病历"]:
                body = await resolve_local_scene(
                    client, settings, {"target": query, "spaceId": "space"}
                )
                assert body["data"]["commands"] == [], (query, body)

    asyncio.run(check())


def test_scale_mapping_survives_source_span_alignment():
    """回归：量表映射改写 target 后，sourceText 必须回填原话。

    上一版实测「我想做ADL量表」返回「系统没认出来」：映射后的 commands
    sourceText 是「四楼2号评估室」，而 SceneIntentParser.spans() 要求
    sourceText 能在本轮原话里定位，找不到就抛 command_alignment。
    """

    async def check():
        transport, _seen = _mock_transport(POINTS)
        settings = AppTestSettings(dry_run=False)
        async with httpx.AsyncClient(transport=transport) as client:
            body = await resolve_local_scene(
                client, settings, {"target": "我想做ADL量表", "spaceId": "space"}
            )
        commands = body["data"]["commands"]
        assert commands and commands[0]["status"] == "matched", body
        assert "四楼2号评估室" in commands[0]["matchName"], body
        # 对齐校验：不抛错且覆盖全部原话
        spans = SceneIntentParser().spans("我想做ADL量表", commands)
        assert len(spans) == len(commands)

    asyncio.run(check())
