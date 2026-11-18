"""
创建日期：2026-11-16
文件功能：验证 AI实时分析双模式链路（初始评估 / 实时对话分析，对齐 Dify 初始评估.yml）。
"""

import asyncio
import json
from types import SimpleNamespace

import pytest

import meta_agent.flows.ai_realtime_analysis as ra
from meta_agent.contracts import DomainError
from meta_agent.flows.ai_realtime_analysis import (
    ONLINE_CONSULTATION_RECORDS_URL,
    is_initial_assessment,
    run_analysis,
    wrap_analysis_event,
)


class Memory:
    def __init__(self):
        self.vars = {}

    def get_variable(self, key, default=None):
        return self.vars.get(key, default)

    def set_variable(self, key, value):
        self.vars[key] = value


class Budget:
    async def take(self):
        pass


class Model:
    def __init__(self, content):
        self.content = content
        self.prompts = []

    async def ainvoke(self, prompt, config=None):
        self.prompts.append(prompt)
        return SimpleNamespace(content=self.content)


def make_ctx(inputs=None):
    return SimpleNamespace(
        run_id="r1",
        metrics={},
        memory=Memory(),
        llm_budget=Budget(),
        business_inputs=inputs or {},
        record=SimpleNamespace(run_id="r1", metrics={}, goal_statuses={}, status="running"),
        settings=SimpleNamespace(dry_run=True),
    )


def test_query_keyword_routes_to_initial_assessment():
    assert is_initial_assessment("初始评估")
    assert is_initial_assessment("请帮我做初始评估")
    assert not is_initial_assessment("分析一下患者最近的变化")
    assert not is_initial_assessment("")


def test_json_query_routes_and_extracts_message():
    """前端真实报文：JSON 字符串 query，InitialEvaluation=null 表示对话消息。"""
    query = json.dumps(
        {
            "InitialEvaluation": None,
            "role": "Doctor",
            "timeStamp": 1794880913087,
            "from": "医生1",
            "message": "我给他说一声，为什么要去现场你们去调这种东西呢？",
        },
        ensure_ascii=False,
    )
    assert not is_initial_assessment(query)
    assert ra.parse_query_payload(query)["from"] == "医生1"
    assert (
        ra.extract_dialogue_text(query)
        == "医生1：我给他说一声，为什么要去现场你们去调这种东西呢？"
    )


def test_json_query_initial_evaluation_truthy_routes_to_initial():
    query = json.dumps({"InitialEvaluation": True, "role": "Doctor", "message": ""})
    assert is_initial_assessment(query)


def test_text_query_extraction_is_passthrough():
    assert ra.parse_query_payload(" plain text ") is None
    assert ra.extract_dialogue_text("患者说最近走路更稳了") == "患者说最近走路更稳了"


def test_json_query_without_message_yields_empty_dialogue():
    query = json.dumps({"InitialEvaluation": None, "from": "医生1", "message": ""})
    assert ra.parse_query_payload(query) is not None
    assert ra.extract_dialogue_text(query) == ""


def test_event_wrapper_matches_dify_contract():
    event = json.loads(wrap_analysis_event("分析正文"))
    assert event["event"] == "analysis_record"
    assert event["display_text"] == "[Answer:]分析正文\n\n"
    assert event["payload"] == {}


def test_initial_assessment_fetches_four_sources_and_caches_context(monkeypatch):
    calls = []

    async def fake_login(client, settings):
        return "token-1"

    async def fake_get(client, url, *, token, params, step, label):
        calls.append((url, dict(params)))
        body = {
            ra.MEDICAL_INFO_URL: json.dumps(
                {"data": {"PatientDetails": {"Sex": "男"}}}, ensure_ascii=False
            ),
            ra.USER_ROLE_URL: json.dumps(
                {"data": {"UserId": "7", "TenantId": "3"}}, ensure_ascii=False
            ),
            ra.TEMPLATE_CONTENT_URL: json.dumps({"data": {"Age": 58}}, ensure_ascii=False),
            ONLINE_CONSULTATION_RECORDS_URL: json.dumps(
                {"data": [{"Date": "2026/11/01"}]}, ensure_ascii=False
            ),
        }[url]
        return SimpleNamespace(text=body)

    monkeypatch.setattr(ra, "_login", fake_login)
    monkeypatch.setattr(ra, "_get", fake_get)

    model = Model("【患者基本信息】\n性别：男")
    ctx = make_ctx({"patientphone": "15800000005"})
    answer = asyncio.run(run_analysis(ctx, "初始评估", model))

    event = json.loads(answer)
    assert event["event"] == "analysis_record"
    assert event["display_text"].startswith("[Answer:]【患者基本信息】")

    assert (url for url, _ in calls) is not None
    assert len(calls) == 4
    assert dict(calls[3][1]) == {"Phone": "15800000005"}

    # 提示词里带三路数据（病历/模板/就诊记录；角色响应只用于取 UserId），会话变量已缓存。
    prompt = model.prompts[0]
    assert '"Sex"' in prompt and "男" in prompt
    assert '"Age"' in prompt
    assert '{"data": [{"Date": "2026/11/01"}]}' in prompt
    assert ctx.memory.get_variable("patient_context") == "【患者基本信息】\n性别：男"
    assert ctx.record.metrics["analysis_mode"] == "initial"


def test_initial_assessment_continues_when_phone_missing(monkeypatch):
    async def fake_login(client, settings):
        return "token-1"

    async def fail_get(*args, **kwargs):
        raise AssertionError("should not fetch without phone")

    monkeypatch.setattr(ra, "_login", fake_login)
    monkeypatch.setattr(ra, "_get", fail_get)

    model = Model("【患者基本信息】\n性别：")
    ctx = make_ctx({})
    answer = asyncio.run(run_analysis(ctx, "初始评估", model))
    assert json.loads(answer)["event"] == "analysis_record"
    assert "（无）" in model.prompts[0]


def test_dialogue_analysis_uses_cached_context(monkeypatch):
    async def fail_login(*args, **kwargs):
        raise AssertionError("dialogue mode must not fetch")

    monkeypatch.setattr(ra, "_login", fail_login)

    model = Model("实时对话分析建议\n患者新述无新增症状。更新建议：①观察②复评")
    ctx = make_ctx({})
    ctx.memory.set_variable("patient_context", "【患者基本信息】\n性别：男")

    answer = asyncio.run(run_analysis(ctx, "患者说最近走路更稳了", model))
    assert json.loads(answer)["event"] == "analysis_record"
    assert "患者说最近走路更稳了" in model.prompts[0]
    assert "【患者基本信息】" in model.prompts[0]
    assert ctx.record.metrics["analysis_mode"] == "dialogue"
    assert ctx.record.metrics["analysis_context_available"] is True
    assert ctx.record.metrics["analysis_query_format"] == "text"


def test_dialogue_analysis_with_json_query(monkeypatch):
    """JSON 报文对话消息：提示词里应是提取后的「说话人：message」，而非整段 JSON。"""
    async def fail_login(*args, **kwargs):
        raise AssertionError("dialogue mode must not fetch")

    monkeypatch.setattr(ra, "_login", fail_login)

    model = Model("实时对话分析建议\n患者新述无新增症状。更新建议：①观察②复评")
    ctx = make_ctx({"patientphone": "15800000005"})
    query = json.dumps(
        {
            "InitialEvaluation": None,
            "role": "Doctor",
            "timeStamp": 1794880913087,
            "from": "医生1",
            "message": "我给他说一声，为什么要去现场你们去调这种东西呢？",
        },
        ensure_ascii=False,
    )
    answer = asyncio.run(run_analysis(ctx, query, model))
    assert json.loads(answer)["event"] == "analysis_record"
    prompt = model.prompts[0]
    assert "医生1：我给他说一声" in prompt
    assert '"InitialEvaluation"' not in prompt
    assert ctx.record.metrics["analysis_mode"] == "dialogue"
    assert ctx.record.metrics["analysis_query_format"] == "json"


def test_dialogue_analysis_without_context_still_runs(monkeypatch):
    async def fail_login(*args, **kwargs):
        raise AssertionError("dialogue mode must not fetch")

    monkeypatch.setattr(ra, "_login", fail_login)

    model = Model("实时对话分析建议\n暂无对话内容可分析")
    ctx = make_ctx({})
    answer = asyncio.run(run_analysis(ctx, "实时分析", model))
    assert json.loads(answer)["event"] == "analysis_record"
    assert "（无）" in model.prompts[0]


def test_empty_model_output_raises_unavailable(monkeypatch):
    async def fail_login(*args, **kwargs):
        return "token-1"

    monkeypatch.setattr(ra, "_login", fail_login)

    ctx = make_ctx({})
    with pytest.raises(DomainError):
        asyncio.run(run_analysis(ctx, "实时分析", Model("")))
