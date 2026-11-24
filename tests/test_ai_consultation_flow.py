"""
创建日期：2026-11-18
文件功能：AI问诊独立流程（对齐 Dify「AI诊室(测试)」真实链路）的专项测试。

覆盖：意图识别移植件、问题分类、错误码提取、疾病扫描、病史解析、
挂号三态播报与指名匹配、知识问答分支（MaxKB 检索 mock）、场景分支。
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

from meta_agent.flows.ai_consultation.booking import (
    extract_doctor_name,
    named_booking_answer,
    plain_booking_answer,
)
from meta_agent.flows.ai_consultation.diagnosis import (
    booking_suffix,
    detect_disease,
    format_patient_profile,
    is_final_triage,
    parse_medical_history,
    parse_patient_profile,
)
from meta_agent.flows.ai_consultation.error_code import extract_error_code
from meta_agent.flows.ai_consultation.intents import classify_intent
from meta_agent.flows.ai_consultation.prompts import build_triage_prompt
from meta_agent.flows.ai_consultation.question_classify import classify_question


class _FakeModel:
    def __init__(self, content: str) -> None:
        self.content = content
        self.prompt = ""

    async def ainvoke(self, prompt, config=None):
        self.prompt = prompt
        return SimpleNamespace(content=self.content)


class _FakeEmitter:
    def __init__(self, record=None) -> None:
        self.events: list[tuple[str, dict]] = []
        self.record = record

    async def emit(self, event, payload, **kwargs):
        self.events.append((event, payload))
        if event == "action_ready" and self.record is not None:
            self.record.action_delivery[payload["action_id"]] = "delivery_unknown"


class _FakeEvidence:
    def __init__(self, evidence_id="ev_booking") -> None:
        self.evidence_id = evidence_id


class _FakeRepository:
    async def save_evidence(self, *args, **kwargs):
        return _FakeEvidence()


async def _async_none() -> None:
    return None


def _ctx(query: str) -> SimpleNamespace:
    record = SimpleNamespace(
        run_id="run_ai_consult",
        request_id="req_ai_consult",
        metrics={},
        goal_statuses={},
        results={},
        action_delivery={},
    )
    emitter = _FakeEmitter(record)
    return SimpleNamespace(
        query=query,
        memory=SimpleNamespace(turns=[]),
        record=record,
        llm_budget=SimpleNamespace(take=_async_none),
        emitter=emitter,
        repository=_FakeRepository(),
        business_inputs={},
        scope=SimpleNamespace(
            space_id="67ecd511-5532-405d-9b70-1cd1c0532eed",
            scope_hash="scope_hash_1",
            role="patient",
            scene_version=0,
        ),
        settings=SimpleNamespace(
            dry_run=True,
            scene_enabled=True,
            scene_actions_enabled=True,
            scene_default_space_id="67ecd511-5532-405d-9b70-1cd1c0532eed",
            doctor_default_space_id="",
            knowledge_sources=[],
            knowledge_context_tokens=4000,
            max_tool_response_bytes=2_000_000,
        ),
    )


# -------------------------------------------
# 意图识别移植件
# -------------------------------------------


def test_intent_scene_jump_returns_command_target():
    intent = classify_intent("带我去三楼")
    assert intent["intent"] == "visit_or_medical_request"
    assert intent["selection_mode"] == "scene"
    assert intent["selection_value"].startswith("[")


def test_intent_ai_consultation_for_symptoms():
    intent = classify_intent("我最近头晕还失眠")
    assert intent["intent"] == "ai_consultation"


def test_intent_booking_for_registration():
    intent = classify_intent("我要挂号")
    assert intent["intent"] in ("visit_or_medical_request", "medical_action_request")
    assert intent["selection_mode"] != "scene"


def test_intent_falls_back_to_qa_for_products():
    intent = classify_intent("irego怎么开机")
    assert intent["intent"] == "qa"


def test_intent_transition_phrase_keeps_consultation_when_in_flow():
    """问诊流程中的「谢谢/好的」是过渡语，不应把意图打回 qa。"""
    history = [{"intent": "ai_consultation"}]
    for phrase in ("谢谢", "好的", "明白了", "试一下"):
        intent = classify_intent(phrase, conversation_history=history)
        assert intent["intent"] == "ai_consultation", phrase
    # 不在问诊流程时，这些词仍按原规则走兜底
    assert classify_intent("谢谢")["intent"] != "ai_consultation"


def test_intent_new_topic_switches_out_of_consultation():
    """真正的新话题（天气/翻译等）应切换出问诊流程，不被粘住。"""
    history = [{"intent": "ai_consultation"}]
    assert classify_intent("今天天气怎么样", conversation_history=history)["intent"] == "天气"
    assert classify_intent("这个词英文怎么说", conversation_history=history)["intent"] == "翻译"


def test_intent_consultation_chain_survives_transition():
    """完整多轮：问诊→短答→过渡语→省略句，意图链不应断。"""
    chain = ["我最近老是失眠", "一周了", "谢谢", "第二个"]
    previous = ""
    for query in chain:
        history = [{"intent": previous}] if previous else []
        intent = classify_intent(query, conversation_history=history)
        assert intent["intent"] == "ai_consultation", (query, intent)
        previous = intent["intent"]


def test_intent_explicit_actions_not_stuck_in_consultation():
    """粘性只作用于续聊短句：明确的新动作/产品提问须放行。"""
    history = [{"intent": "ai_consultation"}]
    booking = classify_intent("那我要挂号", conversation_history=history)
    assert booking["intent"] in ("medical_action_request", "visit_or_medical_request")
    qa = classify_intent("irego怎么开机", conversation_history=history)
    assert qa["intent"] == "qa"


def test_llm_intent_parses_json_and_maps_to_emit():
    import asyncio

    from meta_agent.flows.ai_consultation.llm_intent import classify_intent_llm

    ctx = _ctx("谢谢")
    model = _FakeModel('{"intent":"ai_consultation","confidence":0.91,"reason":"问诊中过渡语"}')
    intent = asyncio.run(classify_intent_llm(ctx, model, "谢谢", "ai_consultation"))
    assert intent["intent"] == "ai_consultation"
    assert intent["confidence"] == "0.91"
    assert intent["reason"] == "问诊中过渡语"
    assert intent["selection_mode"] == ""


def test_llm_intent_tolerates_fence_and_prefix():
    from meta_agent.flows.ai_consultation.llm_intent import _parse_intent_json

    assert _parse_intent_json('```json\n{"intent":"qa"}\n```')["intent"] == "qa"
    assert _parse_intent_json('结果：{"intent":"qa"} 以上')["intent"] == "qa"
    assert _parse_intent_json("不是 JSON") is None


def test_llm_intent_invalid_output_returns_none():
    import asyncio

    from meta_agent.flows.ai_consultation.llm_intent import classify_intent_llm

    ctx = _ctx("谢谢")
    assert asyncio.run(classify_intent_llm(ctx, _FakeModel("无法识别"), "谢谢", "")) is None
    unknown = asyncio.run(
        classify_intent_llm(ctx, _FakeModel('{"intent":"火星意图"}'), "谢谢", "")
    )
    assert unknown is None


# -------------------------------------------
# 问题分类与错误码提取
# -------------------------------------------


def test_question_classify_error_code():
    assert classify_question("E12错误码是什么意思")["label"] == "设备错误码提示"


def test_question_classify_company_and_doctor():
    assert classify_question("你们公司的地址")["label"] == "公司/产品/设备操作"
    assert classify_question("张医生在吗")["label"] == "人员/医生相关咨询"


def test_error_code_extraction():
    assert extract_error_code("E12错误码是什么意思") == "E12"
    assert extract_error_code(" irego怎么开机 ") == ""


# -------------------------------------------
# 分诊辅助
# -------------------------------------------


def test_disease_detection_and_final_marker():
    assert detect_disease("初步判断：脑卒中恢复期。推荐挂号：康复医学科。") is True
    assert detect_disease("初步判断：无明确疾病指征。推荐挂号：无需挂号。") is False
    assert is_final_triage("最可能的原因与初步建议：xxx") is True
    assert is_final_triage("请问近期有发烧吗？") is False


def test_medical_history_parsing():
    body = json.dumps({"data": {"PatientDetails": {"MedicalHistory": "高血压10年"}}})
    assert parse_medical_history(body) == {"has_history": "true", "history_value": "高血压10年"}
    assert parse_medical_history('{"data": null}') == {
        "has_history": "false",
        "history_value": "null",
    }
    assert parse_medical_history("not-json") == {"has_history": "false", "history_value": "null"}


def test_patient_profile_extracts_health_fields():
    body = json.dumps(
        {
            "data": {
                "PatientDetails": {
                    "PatientName": "张三",
                    "Sex": "男",
                    "Age": "68",
                    "MedicalHistory": "高血压10年",
                    "AllergyHistory": "青霉素过敏",
                    "HealthStatus": "",
                    "Phone": "13800000000",
                }
            }
        },
        ensure_ascii=False,
    )
    profile = parse_patient_profile(body)
    assert profile["性别"] == "男"
    assert profile["年龄"] == "68"
    assert profile["既往病史"] == "高血压10年"
    assert profile["过敏史"] == "青霉素过敏"
    assert "健康状况" not in profile  # 空值不纳入
    assert "Phone" not in profile  # 非白名单字段不纳入
    text = format_patient_profile(profile)
    assert "- 性别：男" in text and "- 既往病史：高血压10年" in text
    assert parse_patient_profile("not-json") == {}
    assert parse_patient_profile('{"data": null}') == {}


def test_triage_prompt_injects_patient_and_medical_knowledge():
    with_info = build_triage_prompt(
        has_history=True,
        history_value="高血压10年",
        transcript="用户（本轮）：最近头晕",
        query="最近头晕",
        patient_profile="- 性别：男\n- 年龄：68",
        medical_knowledge="[S1] 高血压指南 · 第3页\n头晕需监测血压",
    )
    assert "【患者信息】" in with_info
    assert "- 性别：男" in with_info
    assert "【医学知识库检索结果】" in with_info
    assert "头晕需监测血压" in with_info
    assert "【资料使用规则】" in with_info
    assert "初步判断：" in with_info  # 终结格式未变

    no_info = build_triage_prompt(
        has_history=False,
        history_value="null",
        transcript="用户（本轮）：你好",
        query="你好",
    )
    assert "（空）" in no_info  # 缺省值兼容旧调用
    assert "最可能的原因与初步建议：" in no_info


def test_triage_flow_uses_patient_and_medical_kb(monkeypatch):
    """分诊链路：病历画像 + health 域知识一起进提示词，命中写 metric。"""
    import meta_agent.flows.ai_consultation.diagnosis as diagnosis

    body = json.dumps(
        {"data": {"PatientDetails": {"Sex": "男", "Age": "68", "MedicalHistory": "高血压10年"}}},
        ensure_ascii=False,
    )

    async def fake_login(client, settings):
        return "token"

    class _Resp:
        text = body

    async def fake_get(client, url, *, token, params, step, label):
        return _Resp()

    async def fake_search(ctx, query, domain="health"):
        assert domain == "health"
        assert "头晕" in query and "高血压10年" in query
        return {
            "status": "ok",
            "context": "[S1] 临床资料\n高血压患者头晕应测血压",
            "sources": [],
            "gaps": [],
            "trace_id": "t",
        }

    monkeypatch.setattr(diagnosis, "_login", fake_login)
    monkeypatch.setattr(diagnosis, "_get", fake_get)
    monkeypatch.setattr(diagnosis, "search_maxkb", fake_search)
    monkeypatch.setattr(diagnosis, "build_transcript", lambda ctx: "用户（本轮）：最近头晕")

    ctx = _ctx("最近头晕")
    ctx.business_inputs = {"patientphone": "13800000000"}
    model = _FakeModel("请问最近血压控制得怎么样？")
    text = asyncio.run(diagnosis.run_triage(ctx, model))
    assert text.startswith("[Answer:]")
    assert "- 年龄：68" in model.prompt
    assert "高血压患者头晕应测血压" in model.prompt
    assert ctx.record.metrics["consultation_medical_kb_hit"] == "true"


def test_triage_flow_degrades_without_medical_kb(monkeypatch):
    """知识检索异常时按无知识继续分诊，不阻断。"""
    import meta_agent.flows.ai_consultation.diagnosis as diagnosis

    async def boom(*args, **kwargs):
        raise RuntimeError("kb down")

    monkeypatch.setattr(diagnosis, "search_maxkb", boom)
    monkeypatch.setattr(diagnosis, "build_transcript", lambda ctx: "用户（本轮）：头疼")
    ctx = _ctx("头疼")  # 无 patientphone → 跳过病史查询
    model = _FakeModel("请问头疼多久了？")
    text = asyncio.run(diagnosis.run_triage(ctx, model))
    assert text.startswith("[Answer:]")
    assert ctx.record.metrics["consultation_medical_kb_hit"] == "false"
    assert "（空）" in model.prompt


def test_booking_suffix_wording():
    none_data = {"result": "无医生"}
    one_data = {"result": "一个医生", "doctorNames": "张三", "command": "[DoctorRecommend:]139"}
    many_data = {"result": "多个医生", "doctorNames": "张三、李四"}
    assert "当前无在线医生" in booking_suffix(none_data, "分诊文本")
    assert booking_suffix(one_data, "分诊文本").startswith("[DoctorRecommend:]139")
    assert "已为您挂号张三" in booking_suffix(one_data, "分诊文本")
    assert "分别是张三、李四" in booking_suffix(many_data, "分诊文本")


# -------------------------------------------
# 挂号播报与指名匹配
# -------------------------------------------


def test_plain_booking_answers():
    assert "带我去AI诊室" in plain_booking_answer({"result": "无医生"})
    one = plain_booking_answer(
        {"result": "一个医生", "doctorNames": "张三", "command": "[DoctorRecommend:]139"}
    )
    assert one.startswith("[DoctorRecommend:]139")
    assert "已为您挂号张三" in one
    many = plain_booking_answer({"result": "多个医生", "doctorNames": "张三、李四"})
    assert "请选择您想选择的医生" in many


def test_named_booking_answers():
    names, phones = ["张三", "李四"], ["139", "138"]
    assert named_booking_answer("张三", names, phones)[0] == "1"
    assert named_booking_answer("张三", names, phones)[1].startswith("[DoctorRecommend:]139")
    assert named_booking_answer("王五", ["赵六"], ["137"])[0] == "2"
    assert named_booking_answer("王五", names, phones)[0] == "3"
    assert named_booking_answer("王五", [], [])[0] == "0"


def test_extract_doctor_name():
    assert "张医生" in extract_doctor_name("我选张医生")


# -------------------------------------------
# 挂号指令下发通道（action_ready，与场景指令一致）
# -------------------------------------------


def test_booking_command_via_action_ready_not_text(monkeypatch):
    """一个医生在线：[DoctorRecommend:] 走 action_ready 独立下发，answer 文本不含指令。"""
    import meta_agent.flows.ai_consultation.booking as booking_mod
    from meta_agent.flows.ai_consultation.booking import run_booking

    envelope = {
        "status": 200,
        "data": {
            "result": "一个医生",
            "doctorNames": "张三",
            "doctorPhones": "13900000000",
            "command": "[DoctorRecommend:]13900000000",
            "count": 1,
        },
    }

    async def fake_fetch(ctx, client, space):
        return envelope

    class _FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

    monkeypatch.setattr(booking_mod.httpx, "AsyncClient", lambda **kwargs: _FakeClient())
    monkeypatch.setattr(booking_mod, "fetch_envelope", fake_fetch)

    ctx = _ctx("我要挂号")
    text = asyncio.run(run_booking(ctx, "我要挂号", named=False))
    actions = [e for e in ctx.emitter.events if e[0] == "action_ready"]
    assert len(actions) == 1
    assert actions[0][1]["command_code"] == "[DoctorRecommend:]13900000000"
    assert actions[0][1]["source_evidence_id"] == "ev_booking"
    assert "[DoctorRecommend:]" not in text
    assert "已为您挂号张三" in text
    assert ctx.record.action_delivery  # 事件发射器已登记去重


def test_booking_command_delivered_once(monkeypatch):
    """同一 run 重复挂号：action_ready 只发一次。"""
    import meta_agent.flows.ai_consultation.booking as booking_mod
    from meta_agent.flows.ai_consultation.booking import run_booking

    async def fake_fetch(ctx, client, space):
        return {
            "status": 200,
            "data": {
                "result": "一个医生",
                "doctorNames": "张三",
                "doctorPhones": "13900000000",
                "command": "[DoctorRecommend:]13900000000",
                "count": 1,
            },
        }

    class _FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

    monkeypatch.setattr(booking_mod.httpx, "AsyncClient", lambda **kwargs: _FakeClient())
    monkeypatch.setattr(booking_mod, "fetch_envelope", fake_fetch)

    ctx = _ctx("我要挂号")
    asyncio.run(run_booking(ctx, "我要挂号", named=False))
    asyncio.run(run_booking(ctx, "我要挂号", named=False))
    actions = [e for e in ctx.emitter.events if e[0] == "action_ready"]
    assert len(actions) == 1


# -------------------------------------------
# 流程分支路由（mock 外部依赖）
# -------------------------------------------


def test_flow_booking_branch(monkeypatch):
    import meta_agent.flows.ai_consultation as consultation_flow

    async def fake_booking(ctx, query, *, named):
        return "[DoctorRecommend:]139[Answer:]已挂号"

    monkeypatch.setattr(consultation_flow, "run_booking", fake_booking)
    ctx = _ctx("我要挂号")
    text = asyncio.run(consultation_flow.Flow(_stub_app())._pipeline(ctx, _FakeModel("")))
    assert text.startswith("[DoctorRecommend:]139")
    assert ctx.record.metrics["consultation_intent"] == "medical_action_request"


def test_flow_passes_previous_intent_to_classifier(monkeypatch):
    """上一轮意图存 memory.variables，本轮无实义续聊句据此续走分诊；
    即便本轮兜底成 qa，也不能冲掉活跃的问诊意图，否则后续省略句全落知识库。"""
    import meta_agent.flows.ai_consultation as consultation_flow

    seen = []

    def fake_classify(query, conversation_history=None):
        seen.append(conversation_history)
        return {"intent": "qa", "confidence": "0.75"}

    async def fake_knowledge(ctx, model):
        return "[Answer:]ok"

    monkeypatch.setattr(consultation_flow, "classify_intent", fake_classify)
    monkeypatch.setattr(consultation_flow, "run_knowledge", fake_knowledge)
    ctx = _ctx("嗯")
    ctx.memory.variables = {"consultation_intent": "ai_consultation"}
    asyncio.run(consultation_flow.Flow(_stub_app())._pipeline(ctx, _FakeModel("")))
    assert seen == [[{"intent": "ai_consultation"}]]
    # 关键：qa 兜底不覆盖活跃问诊态，下一轮仍能续聊
    assert ctx.memory.variables["consultation_intent"] == "ai_consultation"
    assert ctx.record.metrics["consultation_intent_state"] == "ai_consultation"


def test_flow_qa_replaces_non_consultation_state(monkeypatch):
    """非问诊态下的 qa 照常写入，避免历史残留。"""
    import meta_agent.flows.ai_consultation as consultation_flow

    monkeypatch.setattr(
        consultation_flow,
        "classify_intent",
        lambda query, conversation_history=None: {"intent": "qa", "confidence": "0.75"},
    )
    async def fake_knowledge(ctx, model):
        return "[Answer:]ok"

    monkeypatch.setattr(consultation_flow, "run_knowledge", fake_knowledge)
    ctx = _ctx("irego 怎么开机")
    ctx.memory.variables = {"consultation_intent": "qa"}
    asyncio.run(consultation_flow.Flow(_stub_app())._pipeline(ctx, _FakeModel("")))
    assert ctx.memory.variables["consultation_intent"] == "qa"


def test_flow_scene_jump_clears_consultation_state(monkeypatch):
    """场景跳转把用户带离问诊，问诊态显式清空。"""
    import meta_agent.flows.ai_consultation as consultation_flow

    async def fake_scene(ctx, intent):
        return "[Answer:]带路"

    monkeypatch.setattr(consultation_flow, "run_scene", fake_scene)
    ctx = _ctx("带我去三楼")
    ctx.memory.variables = {"consultation_intent": "ai_consultation"}
    asyncio.run(consultation_flow.Flow(_stub_app())._pipeline(ctx, _FakeModel("")))
    assert ctx.memory.variables["consultation_intent"] == ""
    assert ctx.record.metrics["consultation_intent_state"] == "none"


def test_flow_uses_llm_intent_when_enabled(monkeypatch):
    """开启 LLM 意图后按模型语义结果分流，并标记来源便于日志观测。"""
    import meta_agent.flows.ai_consultation as consultation_flow

    async def fake_triage(ctx, model):
        return "[Answer:]继续问诊"

    monkeypatch.setattr(consultation_flow, "run_triage", fake_triage)
    ctx = _ctx("谢谢")
    ctx.settings.consultation_intent_llm = True
    model = _FakeModel('{"intent":"ai_consultation","confidence":0.9,"reason":"问诊中过渡语"}')
    text = asyncio.run(consultation_flow.Flow(_stub_app())._pipeline(ctx, model))
    assert text == "[Answer:]继续问诊"
    assert ctx.record.metrics["consultation_intent_source"] == "llm"
    assert ctx.record.metrics["consultation_intent"] == "ai_consultation"


def test_flow_llm_failure_falls_back_to_rules(monkeypatch):
    """LLM 输出非法时降级规则引擎，并把上一轮意图传进去。"""
    import meta_agent.flows.ai_consultation as consultation_flow

    seen = []

    def fake_classify(query, conversation_history=None):
        seen.append(conversation_history)
        return {"intent": "ai_consultation", "confidence": "0.9"}

    async def fake_triage(ctx, model):
        return "[Answer:]规则兜底"

    monkeypatch.setattr(consultation_flow, "classify_intent", fake_classify)
    monkeypatch.setattr(consultation_flow, "run_triage", fake_triage)
    ctx = _ctx("谢谢")
    ctx.settings.consultation_intent_llm = True
    ctx.memory.variables = {"consultation_intent": "ai_consultation"}
    asyncio.run(consultation_flow.Flow(_stub_app())._pipeline(ctx, _FakeModel("bad output")))
    assert ctx.record.metrics["consultation_intent_source"] == "rules"
    assert seen == [[{"intent": "ai_consultation"}]]


def test_flow_scene_whitelist_bypasses_llm(monkeypatch):
    """场景白名单硬前置：点位来自本地目录，LLM 不参与也不消耗预算。"""
    import meta_agent.flows.ai_consultation as consultation_flow

    calls = []

    async def fake_llm_intent(ctx, model, query, previous_intent):
        calls.append(query)
        return None

    async def fake_scene(ctx, intent):
        return "[Answer:]带路"

    monkeypatch.setattr(consultation_flow, "classify_intent_llm", fake_llm_intent)
    monkeypatch.setattr(consultation_flow, "run_scene", fake_scene)
    ctx = _ctx("带我去三楼")
    ctx.settings.consultation_intent_llm = True
    asyncio.run(consultation_flow.Flow(_stub_app())._pipeline(ctx, _FakeModel("")))
    assert calls == []
    assert ctx.record.metrics["consultation_intent_source"] == "scene_whitelist"


def test_flow_scene_branch_uses_pipeline_delivery(monkeypatch):
    """场景分支走现有 navigate_scene 管线：原话整句交给工具，命令编码由
    action_ready 独立下发，本分支只给确定性话术，不再自行拼命令串。"""
    import meta_agent.flows.ai_consultation as consultation_flow

    captured = {}

    async def fake_invoke_tool(ctx, name, args):
        captured["name"] = name
        captured["target"] = args["target"]
        ctx.record.action_delivery["action_1"] = {"status": "delivered"}
        ctx.record.results["tid"] = SimpleNamespace(
            outputs={"actions": {"ref_1": {"status": "matched", "matchName": "康复训练区"}}}
        )

    monkeypatch.setattr(consultation_flow, "invoke_tool", fake_invoke_tool)
    ctx = _ctx("带我去三楼")
    intent = classify_intent(ctx.query)
    text = asyncio.run(consultation_flow.run_scene(ctx, intent))
    assert captured["name"] == "navigate_scene"
    assert captured["target"] == "带我去三楼"
    assert "带您去康复训练区" in text
    # 场景话术必须带 [Answer:]：非流式响应把 [ScenePoint:]5 拼到前面时，
    # 靠它分段，避免 "[ScenePoint:]5好的，这就带您去二楼。" 指令话术粘连。
    assert text.startswith("[Answer:]")
    assert "[ScenePoint:]" not in text and "[Telepor:]" not in text


def test_flow_scene_branch_falls_back_when_unmatched(monkeypatch):
    """未命中/场景不可用：不给命令串，给兜底话术。"""
    import meta_agent.flows.ai_consultation as consultation_flow

    async def fake_invoke_tool(ctx, name, args):
        return None  # record.action_delivery 保持为空

    monkeypatch.setattr(consultation_flow, "invoke_tool", fake_invoke_tool)
    ctx = _ctx("带我去不存在的楼层")
    intent = classify_intent(ctx.query)
    text = asyncio.run(consultation_flow.run_scene(ctx, intent))
    assert "没有找到要去的场景点位" in text


def test_flow_knowledge_scene_pretry_short_circuits(monkeypatch):
    """qa 分支先试场景动作（Dify 条件分支 16 次序）：管线命中则不走知识问答。"""
    import meta_agent.flows.ai_consultation as consultation_flow

    async def fake_scene_delivery(ctx):
        return "好的，这就带您去康复训练区。"

    def fail_search(*args, **kwargs):
        raise AssertionError("场景命中后不应再检索知识库")

    monkeypatch.setattr(consultation_flow, "scene_delivery", fake_scene_delivery)
    monkeypatch.setattr(consultation_flow, "search_maxkb", fail_search)
    model = _FakeModel("")
    ctx = _ctx("帮我打开减重系统")
    text = asyncio.run(consultation_flow.run_knowledge(ctx, model))
    assert "带您去康复训练区" in text


def test_flow_knowledge_skips_scene_pipeline_for_pure_question(monkeypatch):
    """纯知识问题不进 navigate_scene：无谓的工具调用/审计被轻量规则挡住。"""
    import meta_agent.flows.ai_consultation as consultation_flow

    async def fail_scene(ctx):
        raise AssertionError("知识问题不应触发场景管线")

    async def fake_search(ctx, query, domain="product"):
        return {"status": "no_evidence", "context": "", "sources": [], "gaps": [], "trace_id": "t"}

    monkeypatch.setattr(consultation_flow, "scene_delivery", fail_scene)
    monkeypatch.setattr(consultation_flow, "search_maxkb", fake_search)
    model = _FakeModel("按模型知识回答")
    ctx = _ctx("irego怎么开机")
    text = asyncio.run(consultation_flow.run_knowledge(ctx, model))
    assert text.startswith("[Answer:]")


def test_flow_knowledge_branch_uses_maxkb_result(monkeypatch):
    import meta_agent.flows.ai_consultation as consultation_flow

    async def fake_search(ctx, query, domain="product"):
        assert domain == "product"
        return {
            "status": "ok",
            "context": "[S1] IReGo 操作手册 · 第3页\n开机步骤：……",
            "sources": [{"id": "S1", "title": "IReGo 操作手册", "locator": "第3页", "ref": "r1"}],
            "gaps": [],
            "trace_id": "t1",
        }

    async def fake_scene_delivery(ctx):
        return None

    monkeypatch.setattr(consultation_flow, "search_maxkb", fake_search)
    monkeypatch.setattr(consultation_flow, "scene_delivery", fake_scene_delivery)
    model = _FakeModel("1、按下电源键\n2、等待自检\n继续提问建议：\n- A\n- B\n- C")
    ctx = _ctx("irego怎么开机")
    text = asyncio.run(consultation_flow.run_knowledge(ctx, model))
    assert text.startswith("[Answer:]")
    assert "知识库命中" in model.prompt and "true" in model.prompt
    assert "irego怎么开机" in model.prompt


def test_flow_knowledge_branch_error_code_prompt(monkeypatch):
    import meta_agent.flows.ai_consultation as consultation_flow

    async def fake_search(ctx, query, domain="product"):
        return {
            "status": "ok",
            "context": "错误等级：一级\n描述：急停触发",
            "sources": [],
            "gaps": [],
            "trace_id": "t",
        }

    async def fake_scene_delivery(ctx):
        return None

    monkeypatch.setattr(consultation_flow, "search_maxkb", fake_search)
    monkeypatch.setattr(consultation_flow, "scene_delivery", fake_scene_delivery)
    model = _FakeModel("错误码：E12\n错误等级：一级")
    ctx = _ctx("E12错误码是什么意思")
    asyncio.run(consultation_flow.run_knowledge(ctx, model))
    assert "严格的错误码答复格式化器" in model.prompt
    assert "E12" in model.prompt


def test_flow_knowledge_falls_back_without_space(monkeypatch):
    import meta_agent.flows.ai_consultation as consultation_flow

    async def fake_scene_delivery(ctx):
        return None

    monkeypatch.setattr(consultation_flow, "scene_delivery", fake_scene_delivery)
    model = _FakeModel("使用模型自有知识回答")
    ctx = _ctx(" irego怎么开机 ")
    ctx.scope = SimpleNamespace(space_id="")
    ctx.settings.knowledge_sources = []
    text = asyncio.run(consultation_flow.run_knowledge(ctx, model))
    assert text == "[Answer:]使用模型自有知识回答"
    assert "false" in model.prompt  # 无知识命中，提示词标记 use_knowledge=false


def _stub_app():
    return SimpleNamespace(agent_runtime=SimpleNamespace(model=None))
