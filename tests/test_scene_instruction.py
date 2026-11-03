"""
创建日期：2026-10-16
文件功能：验证场景/设备指令的确定性识别（is_command_query）。
"""

import asyncio
from types import SimpleNamespace

import pytest

from meta_agent.domains.scene_instruction import (
    is_command_query,
    is_mixed_command_query,
    is_navigation_question,
)


@pytest.mark.parametrize(
    "query",
    [
        "打开摄像头",
        "打开摄像头。",
        "打开个人中心",
        "去二楼",
        "切换视角",
        "投屏",
        "返回上个场景",
        "关闭XX",
        # 挂号/叫号系列：既有别名源，也有点位名，都要能识别为指令
        "取消挂号",
        "确认挂号",
        "挂号确认",
        "取消叫号",
        "确认叫号",
        "叫号确认",
        "拒绝叫号",
        "放大屏幕",
        # 带礼貌前缀仍是指令
        "帮我打开摄像头",
        "请帮我打开个人中心",
    ],
)
def test_recognizes_commands(query):
    assert is_command_query(query) is True


@pytest.mark.parametrize(
    "query",
    [
        "你好",
        "个人中心是什么",
        "这个按钮干什么用",
        "怎么挂号",
        "帮我介绍一下个人中心",
        "我的训练记录有哪些",
        "帮我看看我的训练记录",
        "为什么看不到摄像头",
        # 单独的挂号 / 我要挂号是预约请求，走在线医生工具，不是场景按钮指令
        "挂号",
        "我要挂号",
        "帮我挂号",
        # 打开/查看对话记录走打开历史对话工具，不是场景指令
        "打开对话记录",
        "查看对话记录",
        "看聊天记录",
    ],
)
def test_does_not_misread_questions_as_commands(query):
    assert is_command_query(query) is False


def test_empty_query_is_not_command():
    assert is_command_query("") is False
    assert is_command_query("   ") is False
    assert is_command_query(None) is False


@pytest.mark.parametrize(
    "query",
    [
        "回入口",
        "去大厅",
        "返回入口",
        "去上一层",
        "下一层",
    ],
)
def test_recognizes_relative_navigation(query):
    assert is_command_query(query) is True


@pytest.mark.parametrize("query", ["入口在哪", "入口有什么", "去二楼"])
def test_relative_navigation_not_misread(query):
    # 入口提问不是指令；去二楼是绝对楼层指令，不属于相对导航。
    from meta_agent.domains.scene_instruction import _relative_nav_direction

    assert _relative_nav_direction(query) is None


def test_floor_of():
    from meta_agent.domains.scene_instruction import _floor_of

    assert _floor_of("一楼 入口") == 1
    assert _floor_of("四楼1号评估室") == 4
    assert _floor_of("十一楼会议室") == 11
    assert _floor_of("大厅") is None


def test_relative_match_entrance_prefers_same_floor():
    from meta_agent.domains.scene_instruction import _relative_match

    points = [
        {"prefix": "[ScenePoint:]", "id": "1", "name": "一楼 入口", "intro": ""},
        {"prefix": "[ScenePoint:]", "id": "2", "name": "二楼 入口", "intro": ""},
    ]
    _, p = _relative_match("回入口", "一楼 入口", points)
    assert p["id"] == "1"


def test_relative_match_floor_up():
    from meta_agent.domains.scene_instruction import _relative_match

    points = [
        {"prefix": "[ScenePoint:]", "id": "1", "name": "二楼 康复大厅", "intro": ""},
        {"prefix": "[ScenePoint:]", "id": "2", "name": "一楼 入口", "intro": ""},
    ]
    _, p = _relative_match("去上一层", "一楼 入口", points)
    assert p["id"] == "1"


def test_match_best_nearby_tiebreak():
    from meta_agent.domains.scene_instruction import _match_best

    points = [
        {"prefix": "[ScenePoint:]", "id": "1", "name": "二楼 评估室", "intro": ""},
        {"prefix": "[ScenePoint:]", "id": "2", "name": "一楼 评估室", "intro": ""},
    ]
    _, p = _match_best("评估室", points, "一楼 入口")
    assert p["id"] == "2"


@pytest.mark.parametrize(
    "query",
    [
        "带我去五楼，并查报告",
        "带我去五楼，并查报告。",
        "打开个人中心，顺便查一下我的训练报告",
        "去二楼，同时帮我看看在线医生",
    ],
)
def test_detects_mixed_command_and_other_intent(query):
    assert is_mixed_command_query(query) is True


@pytest.mark.parametrize(
    "query",
    [
        # 纯多动作指令：各部分都是指令，不是混合意图
        "带我去五楼，然后去二楼",
        "打开摄像头，然后投屏",
        # 单一指令或单一提问
        "带我去五楼",
        "查报告",
        "帮我看看我的训练记录",
    ],
)
def test_does_not_misread_pure_commands_as_mixed(query):
    assert is_mixed_command_query(query) is False


# ---- 问路/导航提问（应走企业知识库，而不是下场景指令或凭常识编路线） ----


@pytest.mark.parametrize(
    "query,expected",
    [
        ("怎么去三楼", True),
        ("怎么去三楼？", True),
        ("三楼怎么走", True),
        ("怎么到二楼", True),
        ("如何到达评估室", True),
        ("怎么上三楼", True),
        ("怎么去大厅", True),
        ("去三楼", False),  # 指令，不是问路
        ("怎么走", False),  # 无导航目标
        ("电梯怎么用", False),  # 使用说明，不是问路
        ("三楼有什么", False),  # 内容提问，不是问路
        ("你好", False),
    ],
)
def test_is_navigation_question(query, expected):
    assert is_navigation_question(query) is expected


def test_navigation_question_is_not_command():
    # 问路是提问，不是场景指令；两者不能同时判 True。
    assert is_navigation_question("怎么去三楼") is True
    assert is_command_query("怎么去三楼") is False
    assert is_command_query("去三楼") is True
    assert is_navigation_question("去三楼") is False


# ---- 裸确认/指代指令（「需要打开」→ 结合上一轮找回「打开摄像头」） ----


@pytest.mark.parametrize(
    "query,expected",
    [
        ("需要打开", True),
        ("需要", True),
        ("好的", True),
        ("可以", True),
        ("打开吧", True),
        ("打开摄像头", False),  # 有具体目标，不是裸确认
        ("需要打开摄像头", False),
        ("打开个人中心", False),
        ("帮我挂号", False),  # 挂号是预约请求，不是场景指令
    ],
)
def test_bare_followup(query, expected):
    from meta_agent.domains.scene_instruction import _bare_followup

    assert _bare_followup(query) is expected


@pytest.mark.parametrize(
    "query,expected",
    [
        ("需要打开", "打开"),
        ("好的打开", "打开"),
        ("打开吧", "打开"),
        ("打开摄像头", "打开"),
        ("需要", None),  # 纯确认没有动词
        ("好的", None),
    ],
)
def test_followup_verb(query, expected):
    from meta_agent.domains.scene_instruction import _followup_verb

    assert _followup_verb(query) == expected


def test_recognizes_bare_followup_as_command_but_not_pure_confirmation():
    # 「需要打开」含动词 → 指令意图；纯「需要」「好的」不含动词 → 不主动判定为指令。
    assert is_command_query("需要打开") is True
    assert is_command_query("打开吧") is True
    assert is_command_query("需要") is False
    assert is_command_query("好的") is False


@pytest.mark.parametrize(
    "query,expected",
    [
        ("带我去三楼", True),
        ("带我去三楼吧", True),
        ("带我过去", True),
        ("帮我挂号", False),  # 挂号是预约请求，不是场景指令
    ],
)
def test_recognizes_navigation_command_with_target(query, expected):
    # 带我去/带我 + 目标 是导航指令；但「可以带我去吗」含疑问词仍判 False（走问路/确认恢复）。
    assert is_command_query(query) is expected


@pytest.mark.parametrize(
    "query,expected",
    [
        ("可以带我去吗", "带我去"),
        ("可用带我去嘛", "带我去"),
        ("带我去吧", "带我去"),
        ("需要打开", "打开"),
        ("好的", None),
    ],
)
def test_followup_verb_navigation(query, expected):
    from meta_agent.domains.scene_instruction import _followup_verb

    assert _followup_verb(query) == expected


def test_bare_followup_navigation_confirmation():
    from meta_agent.domains.scene_instruction import _bare_followup

    assert _bare_followup("可以带我去吗") is True
    assert _bare_followup("可用带我去嘛") is True


def test_recover_navigation_target_from_previous_turn():
    from meta_agent.domains.scene_instruction import _recover_followup_command

    turns = [
        {
            "user": "怎么去三楼",
            "assistant": "在元宇宙中的上海岳阳医院，进到医院后直接说“带我去三楼”即可。",
        }
    ]
    assert _recover_followup_command("可以带我去吗", turns) == "带我去三楼"
    assert _recover_followup_command("可用带我去嘛", turns) == "带我去三楼"
    # 纯确认（无动词）也能从上一轮找回最后一次提到的「带我去三楼」。
    assert _recover_followup_command("好的", turns) == "带我去三楼"


def test_recover_followup_command_from_previous_offer():
    from meta_agent.domains.scene_instruction import _recover_followup_command

    turns = [
        {
            "user": "怎么打开摄像头",
            "assistant": "点击屏幕左上角功能区里的“摄像头”按钮，就能打开摄像头。需要我现在帮你打开吗？",
        }
    ]
    assert _recover_followup_command("需要打开", turns) == "打开摄像头"
    assert _recover_followup_command("需要", turns) == "打开摄像头"  # 纯确认也能找回


def test_recover_followup_command_returns_none_without_context():
    from meta_agent.domains.scene_instruction import _recover_followup_command

    assert _recover_followup_command("需要打开", []) is None
    assert _recover_followup_command("需要打开", None) is None
    assert _recover_followup_command("需要打开", [{"user": "你好", "assistant": "你好"}]) is None


def test_adapter_recovers_followup_target():
    async def check():
        from meta_agent.domains.scene_instruction import SceneInstructionAdapter

        class FakeResp:
            def __init__(self, data):
                self._data = data

            def raise_for_status(self):
                pass

            def json(self):
                return self._data

        class FakeClient:
            async def post(self, url, json=None, headers=None, timeout=None):
                payload = json or {}
                if "spaceId" in payload:
                    return FakeResp({"data": 1})
                if "CommandList" in payload.get("sql", ""):
                    return FakeResp({"data": {"result": [{"id": 7, "name": "摄像头", "intro": ""}]}})
                return FakeResp({"data": {"result": []}})

        async def _fake_save_evidence(*a, **k):
            return SimpleNamespace(evidence_id="e1")

        ctx = SimpleNamespace(
            query="需要打开",
            scope=SimpleNamespace(space_id="space", position=None, scope_hash="hash"),
            backend=SimpleNamespace(client=FakeClient()),
            repository=SimpleNamespace(save_evidence=_fake_save_evidence),
            record=SimpleNamespace(request_id="req1"),
            memory=SimpleNamespace(
                turns=[
                    {
                        "user": "怎么打开摄像头",
                        "assistant": "点击…摄像头…就能打开摄像头。需要我现在帮你打开吗？",
                    }
                ]
            ),
        )
        result = await SceneInstructionAdapter().execute(SimpleNamespace(task_id="t1"), {}, ctx)
        assert result.status == "succeeded"
        assert result.outputs["command"] == "[ClickPoint:]7"

    asyncio.run(check())


def test_adapter_recovers_navigation_target():
    # 端到端：「可以带我去吗」（省略目标）→ 从上一轮找回「带我去三楼」→ 匹配到三楼场景点。
    async def check():
        from meta_agent.domains.scene_instruction import SceneInstructionAdapter

        class FakeResp:
            def __init__(self, data):
                self._data = data

            def raise_for_status(self):
                pass

            def json(self):
                return self._data

        class FakeClient:
            async def post(self, url, json=None, headers=None, timeout=None):
                payload = json or {}
                if "spaceId" in payload:
                    return FakeResp({"data": 1})
                if "TeleportConfig" in payload.get("sql", ""):
                    return FakeResp({"data": {"result": [{"id": 3, "name": "三楼", "intro": ""}]}})
                return FakeResp({"data": {"result": []}})

        async def _fake_save_evidence(*a, **k):
            return SimpleNamespace(evidence_id="e1")

        ctx = SimpleNamespace(
            query="可以带我去吗",
            scope=SimpleNamespace(space_id="space", position=None, scope_hash="hash"),
            backend=SimpleNamespace(client=FakeClient()),
            repository=SimpleNamespace(save_evidence=_fake_save_evidence),
            record=SimpleNamespace(request_id="req1"),
            memory=SimpleNamespace(
                turns=[
                    {
                        "user": "怎么去三楼",
                        "assistant": "在元宇宙中的上海岳阳医院，进到医院后直接说“带我去三楼”即可。",
                    }
                ]
            ),
        )
        result = await SceneInstructionAdapter().execute(SimpleNamespace(task_id="t1"), {}, ctx)
        assert result.status == "succeeded"
        assert result.outputs["command"] == "[Telepor:]3"

    asyncio.run(check())
