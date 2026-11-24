"""
创建日期：2026-11-16
文件功能：验证 AI医嘱生成的分支链路（记录数组 → 条件判断 → LLM → 引号规范化）。
"""

import asyncio
import json
from types import SimpleNamespace

from meta_agent.flows import ai_medical_order

RAW_RECORDS = [
    {"role": "Patient", "timeStamp": 1793085016130, "from": "医生1", "message": "有吗？"},
    {"role": "Patient", "timeStamp": 1793085162805, "from": "医生1", "message": "听得到声音吗？"},
    {"role": "Patient", "timeStamp": 1793085283680, "from": "医生1", "message": "脑卒中"},
]
RAW_QUERY = json.dumps(
    [json.dumps(record, ensure_ascii=False) for record in RAW_RECORDS], ensure_ascii=False
)


class _Model:
    def __init__(self, content: str) -> None:
        self.content = content
        self.prompt = ""
        self.calls = 0

    async def ainvoke(self, prompt, config=None):
        self.calls += 1
        self.prompt = prompt
        return SimpleNamespace(content=self.content)


class _Budget:
    async def take(self) -> None:
        return None


class _Emitter:
    def __init__(self) -> None:
        self.events = []

    async def emit(self, event, payload, goal_id=None):
        self.events.append((event, payload))


def _ctx(query: str) -> SimpleNamespace:
    return SimpleNamespace(
        query=query,
        record=SimpleNamespace(
            run_id="run_order", metrics={}, goal_statuses={}, status="succeeded"
        ),
        llm_budget=_Budget(),
        emitter=_Emitter(),
        memory=SimpleNamespace(turns=[], previous_category=""),
    )


def test_conversation_text_keeps_role_label_and_speaker_name():
    """role 决定医生/患者，from 是名字——两者可能不一致，按 Dify 原逻辑分开渲染。"""
    text = ai_medical_order.conversation_text(RAW_QUERY)
    assert text.splitlines()[0] == "患者(医生1)：有吗？"
    assert text.splitlines()[-1] == "患者(医生1)：脑卒中"
    assert ai_medical_order.has_dialogue_roles(text)


def test_conversation_text_handles_non_record_inputs():
    # 空输入、空数组、纯文本
    assert ai_medical_order.conversation_text("") == ""
    assert ai_medical_order.conversation_text("[]") == ""
    assert ai_medical_order.conversation_text("你好") == "你好"
    # 是 JSON 但不是数组 → 按空处理
    assert ai_medical_order.conversation_text('{"a": 1}') == ""
    # 元素解析不了就跳过，不影响别的
    mixed = json.dumps(
        ["不是 JSON", json.dumps({"role": "Doctor", "from": "王x", "message": "复诊"})]
    )
    assert ai_medical_order.conversation_text(mixed) == "医生(王x)：复诊"


def test_no_dialogue_returns_fixed_no_record_json():
    """既没有医生也没有患者时不调用模型，直接给「无问诊记录」。"""
    model = _Model("不应被调用")
    ctx = _ctx("你好")
    result = json.loads(asyncio.run(ai_medical_order.run_medical_order(ctx, ctx.query, model)))

    assert result == {
        "ChiefComplaint": "无问诊记录",
        "Diagnosis": "无问诊记录",
        "Management": "无问诊记录",
    }
    assert model.calls == 0
    assert ctx.record.metrics["medical_order_source"] == "no_record"


def test_unknown_role_only_is_treated_as_no_record():
    """角色都识别不出来时，文本里没有「医生/患者」，同样走固定输出。"""
    query = json.dumps([json.dumps({"role": "Bot", "from": "system", "message": "在的"})])
    model = _Model("不应被调用")
    ctx = _ctx(query)
    result = json.loads(asyncio.run(ai_medical_order.run_medical_order(ctx, query, model)))

    assert result["Diagnosis"] == "无问诊记录"
    assert model.calls == 0


def test_llm_output_quotes_are_normalized_to_english():
    """模型常照着提示词里的中文引号输出，后处理要统一成英文双引号，保证能直接解析。"""
    model = _Model(
        "{\n“ChiefComplaint”: “脑卒中后右侧肢体无力”，\n"
        "‘Diagnosis’: ‘脑卒中’，\n"
        "“Management”: “康复训练；阿司匹林100mg每日一次”\n}"
    )
    ctx = _ctx(RAW_QUERY)
    result = json.loads(asyncio.run(ai_medical_order.run_medical_order(ctx, ctx.query, model)))

    assert result["ChiefComplaint"] == "脑卒中后右侧肢体无力"
    assert result["Diagnosis"] == "脑卒中"
    assert "阿司匹林" in result["Management"]
    assert ctx.record.metrics["medical_order_source"] == "llm"


def test_output_is_plain_three_field_json_without_data_wrapper():
    """answer 就是医嘱对象本身，不再套一层 {"data": …}。"""
    model = _Model(
        '{"ChiefComplaint": "脑卒中", "Diagnosis": "脑卒中", '
        '"Management": "立即就医评估；监测生命体征；保持呼吸道通畅；避免自行用药；'
        '遵医嘱进行影像学检查及相应治疗。"}'
    )
    ctx = _ctx(RAW_QUERY)
    answer = asyncio.run(ai_medical_order.run_medical_order(ctx, ctx.query, model))
    payload = json.loads(answer)

    assert "data" not in payload
    assert list(payload) == ["ChiefComplaint", "Diagnosis", "Management"]
    assert payload == {
        "ChiefComplaint": "脑卒中",
        "Diagnosis": "脑卒中",
        "Management": "立即就医评估；监测生命体征；保持呼吸道通畅；避免自行用药；"
        "遵医嘱进行影像学检查及相应治疗。",
    }
    assert ctx.record.metrics["medical_order_json"] == "ok"


def test_missing_fields_are_filled_and_extra_keys_kept():
    """模型漏字段时补空串、多余字段保留，键序固定为三个主字段在前。"""
    model = _Model('{"Diagnosis": "脑卒中", "Note": "随访"}')
    ctx = _ctx(RAW_QUERY)
    payload = json.loads(asyncio.run(ai_medical_order.run_medical_order(ctx, ctx.query, model)))

    assert list(payload) == ["ChiefComplaint", "Diagnosis", "Management", "Note"]
    assert payload["ChiefComplaint"] == ""
    assert payload["Management"] == ""
    assert payload["Note"] == "随访"


def test_unparsable_output_falls_back_to_raw_text():
    """模型给出非 JSON 时如实返回文本并打标，不编造字段、也不包成假 JSON。"""
    model = _Model("建议完善膝关节评估。")
    ctx = _ctx(RAW_QUERY)
    answer = asyncio.run(ai_medical_order.run_medical_order(ctx, ctx.query, model))

    assert answer == "建议完善膝关节评估。"
    assert ctx.record.metrics["medical_order_json"] == "raw"


def test_flow_emits_plain_json_answer_with_conversation_in_prompt():
    model = _Model('{"ChiefComplaint": "脑卒中", "Diagnosis": "脑卒中", "Management": "康复训练"}')
    ctx = _ctx(RAW_QUERY)
    flow = ai_medical_order.Flow(SimpleNamespace(agent_runtime=SimpleNamespace(model=model)))

    asyncio.run(flow.execute(ctx))

    # 提示词里带的是渲染后的对话，不是原始 JSON 串
    assert "患者(医生1)：脑卒中" in model.prompt
    assert '"role"' not in model.prompt
    event, payload = ctx.emitter.events[0]
    assert event == "answer_part"
    assert json.loads(payload["text"])["Diagnosis"] == "脑卒中"
    assert ctx.record.goal_statuses["ai_medical_order:医嘱生成"] == "succeeded"


def test_markdown_fence_is_stripped_before_quoting():
    model = _Model(
        '```json\n{"ChiefComplaint": "脑卒中", "Diagnosis": "脑卒中", "Management": "训练"}\n```'
    )
    ctx = _ctx(RAW_QUERY)
    result = json.loads(asyncio.run(ai_medical_order.run_medical_order(ctx, ctx.query, model)))
    assert result["Diagnosis"] == "脑卒中"
