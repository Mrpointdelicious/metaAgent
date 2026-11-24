"""
创建日期：2026-11-16
文件功能：验证 AI推荐处方（新建处方）链路：参数合并、处方类型补全、排班时段、
         单/多疾病输出契约，以及 dry_run 下的端到端行为。
"""

import asyncio
import json
import random
from dataclasses import replace
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import meta_agent.flows.ai_prescription as prescription
from meta_agent.flows.ai_prescription import params as params_mod
from meta_agent.flows.ai_prescription import render, slots, webyun
from tests.helpers.identity import clinical_flow_scope

EXTRACTION = {
    "疾病": "脑卒中",
    "机器人类型": "下肢机器人",
    "训练模式": ["行走游戏", "平衡游戏"],
    "疾病判断依据": {"脑卒中": "患者自述走路不稳需要步态训练"},
    "地点": "上海",
    "时间": "2026-11-17 下午",
    "量表": "ADL量表-Barthel指数",
}


class _Memory:
    def __init__(self):
        self.vars = {}
        self.turns = []

    def get_variable(self, key, default=None):
        return self.vars.get(key, default)

    def set_variable(self, key, value):
        self.vars[key] = value


class _Budget:
    async def take(self):  # noqa: D401 - 测试桩
        return None


class _Model:
    def __init__(self, gate=True, extraction=None):
        self.gate = gate
        self.extraction = extraction if extraction is not None else EXTRACTION
        self.prompts = []

    async def ainvoke(self, prompt, config=None):  # noqa: ARG002
        self.prompts.append(prompt)
        if "has_medical_location_time" in prompt:
            payload = {"has_medical_location_time": self.gate}
            return SimpleNamespace(content=json.dumps(payload))
        if "医疗信息结构化抽取与疾病推断助手" in prompt:
            return SimpleNamespace(content=json.dumps(self.extraction, ensure_ascii=False))
        if "固定地点" in prompt:
            return SimpleNamespace(content="上海金矢机器人")
        return SimpleNamespace(content="")


def _ctx(query="患者脑卒中后走路不稳，想在上海做康复训练，明天下午有号吗？"):
    return SimpleNamespace(
        query=query,
        scope=clinical_flow_scope("ai_prescription"),
        record=SimpleNamespace(run_id="run_rx", metrics={}, goal_statuses={}, status="running"),
        memory=_Memory(),
        settings=SimpleNamespace(
            dry_run=True,
            prescription_auto_create=False,
            prescription_api_base="http://127.0.0.1:9",
            webyun_login_phone="",
            knowledge_corpus_path="missing-corpus.json",
        ),
        business_inputs={},
        llm_budget=_Budget(),
    )


# ------------------------- 参数合并 -------------------------


def test_merge_params_maps_games_and_device():
    merged = params_mod.merge_params(EXTRACTION, "", random.Random(0))

    assert merged["disease"] == "脑卒中"
    assert merged["is_multi_disease"] == 1
    assert merged["DeviceType"] == "iReGo"
    assert merged["SerialNumber"] == "iReGo-H000000000"
    assert merged["GameFunc"] == "行走游戏,平衡游戏"
    # 游戏名/图标来自游戏库，数量与训练模式对齐
    assert len(merged["GameName"].split(",")) == 2
    assert len(merged["GameIcon"].split(",")) == 2
    stored = json.loads(merged["params_json"])
    assert stored["disease"] == "脑卒中" and stored["scale"] == "ADL量表-Barthel指数"


def test_merge_params_prefers_new_values_and_falls_back_to_old():
    old = json.dumps({"疾病": "脊髓损伤", "地点": "北京", "量表": "ADL量表-Barthel指数"})
    merged = params_mod.merge_params({"疾病": "脑卒中"}, old, random.Random(0))

    assert merged["disease"] == "脑卒中"
    assert merged["location"] == "北京"
    assert merged["scale"] == "ADL量表-Barthel指数"
    # 训练模式没给过 → 没有设备
    assert merged["DeviceType"] is None


def test_merge_params_multi_disease_counts_names():
    merged = params_mod.merge_params(
        {"疾病": ["脑卒中", "脊髓损伤"], "训练模式": ["单臂游戏"]}, "", random.Random(0)
    )

    assert merged["disease"] == "脑卒中, 脊髓损伤"
    assert merged["is_multi_disease"] == 2
    assert merged["DeviceType"] == "iReMo"


def test_merge_params_unknown_disease_is_zero():
    merged = params_mod.merge_params({"疾病": None}, "", random.Random(0))
    assert merged["disease"] == "未知"
    assert merged["is_multi_disease"] == 0


def test_parse_json_like_tolerates_fence_and_unquoted_values():
    assert params_mod.parse_json_like('```json\n{"疾病": "脑卒中"}\n```') == {"疾病": "脑卒中"}
    assert params_mod.parse_json_like('{"疾病": 脑卒中}') == {"疾病": "脑卒中"}
    assert params_mod.parse_json_like("不是 JSON") == {}


# ------------------------- 处方类型与请求体 -------------------------


def test_missing_types_lists_absent_codes():
    assert webyun.missing_types([]) == ["0", "1", "2", "3", "4"]
    assert webyun.missing_types([{"Type": 0}, {"Type": 2}]) == ["1", "3", "4"]


def test_build_payloads_covers_offline_and_online_types():
    merged = params_mod.merge_params(EXTRACTION, "", random.Random(0))
    content = {"Evaluation": "ADL评定", "Treatment": "步行训练", "Drug": "", "Device": "x"}
    payloads = webyun.build_payloads(merged, ["0", "1", "2", "3", "4"], content, random.Random(0))

    by_type = {item["Type"]: item for item in payloads}
    assert set(by_type) == {0, 1, 2, 3}
    assert by_type[1]["Name"] == "脑卒中线下处方"
    offline = json.loads(by_type[1]["Content"])
    assert offline[0]["DeviceType"] == "iReGo"
    assert 20 <= offline[0]["SettingTime"] <= 30
    assert json.loads(by_type[0]["Content"])[0]["ScaleName"] == "ADL量表-Barthel指数"
    assert json.loads(by_type[3]["Content"])[0]["ScaleName"] == "步行训练"


def test_build_payloads_skips_offline_without_game():
    merged = params_mod.merge_params({"疾病": "脑卒中"}, "", random.Random(0))
    payloads = webyun.build_payloads(merged, ["1"], {}, random.Random(0))
    assert payloads == []


# ------------------------- 排班时段 -------------------------


def test_available_slots_fills_default_slots_and_filters_date():
    schedule = {"2026/11/17": [{"Time": "09:00-10:00", "Used": 1}]}
    found = slots.available_slots(schedule, "2026/11/17", "上午")

    assert "2026/11/17 09:00-10:00" not in found
    assert "2026/11/17 10:00-11:00" in found
    # 下午的时段被「上午」过滤掉
    assert not any("14:00" in item for item in found)


def test_pick_slot_falls_back_to_target_date_when_full():
    # 全部时段占满 → 按目标日期 + 时段兜底；目标日期是「未来某天」时原样返回。
    target = (datetime.now() + timedelta(days=2)).strftime("%Y/%m/%d")
    schedule = {
        target: [{"Time": slot["Time"], "Used": 1} for slot in slots.DEFAULT_TIME_SLOTS],
    }
    picked = slots.pick_slot(schedule, target, "下午", random.Random(0))
    assert picked == f"{target} 14:00-15:00"


def test_pick_slot_bumps_today_to_tomorrow_when_full():
    # 目标日期是「今天」且时段全部占满/已结束 → 推到明天兜底。
    today = datetime.now().strftime("%Y/%m/%d")
    schedule = {
        today: [{"Time": slot["Time"], "Used": 1} for slot in slots.DEFAULT_TIME_SLOTS],
    }
    picked = slots.pick_slot(schedule, today, "下午", random.Random(0))
    tomorrow = (datetime.now() + timedelta(days=1)).strftime("%Y/%m/%d")
    assert picked == f"{tomorrow} 14:00-15:00"


# ------------------------- 输出契约 -------------------------


def _online(name, indication="脑卒中", type_code=0):
    return {
        "Id": name,
        "Name": name,
        "Indication": indication,
        "Prescription": "",
        "Details": [],
        "Type": type_code,
    }


def _offline(name, indication="脑卒中"):
    return {
        "Id": name,
        "Name": name,
        "Indication": indication,
        "Prescription": "",
        "Hospitals": [],
        "Details": [],
        "time_slot_recommendation": [
            {"Hospital": "上海金矢机器人", "Time": "2026/11/17 14:00-15:00"}
        ],
        "Type": 1,
    }


def test_single_output_trims_and_summarizes():
    online = [_online(f"线上{i}") for i in range(8)]
    offline = [_offline(f"线下{i}") for i in range(10)]

    result = render.single_output(online, offline, {"脑卒中": "走路不稳"})

    assert len(result["online_prescriptions"]) == 6
    assert len(result["offline_prescriptions"]) == 8
    assert result["summary"].startswith("根据走路不稳推荐相关处方。")
    assert "推荐地点：上海金矢机器人" in result["summary"]


def test_multi_output_allocates_per_disease_and_merges():
    online = [_online("脑卒中线上处方", "脑卒中"), _online("脊髓损伤线上处方", "脊髓损伤")]
    offline = [_offline("脑卒中线下处方", "脑卒中")]
    reasons = {"脑卒中": "走路不稳", "脊髓损伤": "双下肢无力"}

    result = render.multi_output(online, offline, ["脑卒中", "脊髓损伤"], reasons)

    online_ids = {item["Id"] for item in result["online_prescriptions"]}
    assert online_ids == {"脑卒中线上处方", "脊髓损伤线上处方"}
    assert "针对脊髓损伤" in result["summary"] or "双下肢无力" in result["summary"]


# ------------------------- 端到端 -------------------------


def test_flow_returns_three_field_json_in_dry_run():
    ctx = _ctx()
    answer = asyncio.run(prescription.run_prescription(ctx, ctx.query, _Model()))

    payload = json.loads(answer)
    assert set(payload) == {"online_prescriptions", "offline_prescriptions", "summary"}
    assert len(payload["online_prescriptions"]) == 2
    assert payload["offline_prescriptions"][0]["time_slot_recommendation"][0]["Hospital"]
    assert ctx.record.metrics["prescription_mode"] == "single"
    # 会话变量按 Dify 语义累积
    assert ctx.memory.get_variable("talk")
    assert json.loads(ctx.memory.get_variable("params_json"))["disease"] == "脑卒中"


def test_flow_short_circuits_without_medical_signal():
    ctx = _ctx("你好呀")
    answer = asyncio.run(prescription.run_prescription(ctx, ctx.query, _Model(gate=False)))

    empty = {"online_prescriptions": [], "offline_prescriptions": [], "summary": ""}
    assert json.loads(answer) == empty
    assert ctx.record.metrics["prescription_mode"] == "no_signal"


def test_flow_short_circuits_when_no_rehab_disease():
    ctx = _ctx()
    model = _Model(extraction={"疾病": None, "地点": "上海"})
    answer = asyncio.run(prescription.run_prescription(ctx, ctx.query, model))

    assert json.loads(answer)["summary"] == ""
    assert ctx.record.metrics["prescription_mode"] == "no_disease"


def test_flow_requires_model():
    ctx = _ctx()
    with pytest.raises(Exception) as excinfo:
        asyncio.run(prescription.run_prescription(ctx, ctx.query, None))
    assert "model_unavailable" in str(excinfo.value) or "模型" in str(excinfo.value)


@pytest.mark.parametrize(
    ("enabled", "granted", "writes"), [(True, True, 1), (True, False, 0), (False, True, 0)]
)
def test_prescription_template_creation_requires_toggle_and_encounter_grant(
    monkeypatch, enabled, granted, writes
):
    ctx = _ctx()
    ctx.settings.dry_run = False
    ctx.settings.prescription_auto_create = enabled
    grant = ctx.scope.resource_grant
    if granted:
        grant = grant.model_copy(
            update={"capabilities": grant.capabilities | {"clinical.prescription.create"}}
        )
        ctx.scope = replace(ctx.scope, resource_grant=grant)
    lookup = AsyncMock(return_value=[])
    create = AsyncMock(return_value=[{"body": {}}])
    monkeypatch.setattr(webyun, "list_prescriptions", lookup)
    monkeypatch.setattr(webyun, "build_payloads", lambda *args: [{"Type": "2"}])
    monkeypatch.setattr(webyun, "add_prescriptions", create)
    asyncio.run(
        prescription._prescriptions(
            ctx,
            object(),
            "test",
            grant.requester_phone.get_secret_value(),
            "示例",
            {},
            {},
            random.Random(1),
        )
    )
    assert create.await_count == writes
    assert lookup.await_count == 1 + writes
    assert ctx.record.metrics["prescription_created"] == writes
