"""
创建日期：2026-11-16
文件功能：AI推荐处方（新建处方）独立流程，与 Dify「元宇宙实时问诊AI(新建处方)」对齐。

链路：
  1. 二次判断（LLM）：本轮输入有没有医学信息 / 地点 / 时间，都没有就直接回空结果；
  2. 整合对话：会话变量 talk + " 。" + 本轮 query（等价 Dify「整合对话」节点）；
  3. 结构化抽取（LLM）：疾病（仅康复相关）、机器人类型、训练模式、判断依据、
     地点、时间、量表；
  4. 合并参数：游戏库随机挑游戏、设备映射、疾病个数判定，并回写会话变量；
  5. 按疾病查处方（缺 Type 时可选自动新建，见 settings.prescription_auto_create），
     知识检索只用于补齐评估 / 治疗 / 用药内容；
  6. 输出 {"online_prescriptions": [...], "offline_prescriptions": [...], "summary": "..."}。

写入边界：只有 prescription_auto_create 打开、且该疾病确实缺 Type 时才会调
AddPrescription；默认关闭，第一次上线先按默认跑一轮确认推荐结果再开写入。
"""

from __future__ import annotations

import json
import logging
import random
from typing import TYPE_CHECKING, Any

from meta_agent.contracts import DomainError
from meta_agent.flows.consultation_room.client import ConsultationClient
from meta_agent.flows.consultation_room.medical_record import (
    _extract_content,
    _login,
    current_time,
)
from meta_agent.flows.fixed_task import FixedTaskFlow
from meta_agent.orchestration.capabilities import denial

from . import knowledge, webyun
from .params import disease_list, merge_params
from .prompts import build_extract_prompt, build_gate_prompt, build_location_prompt
from .render import (
    EMPTY_RESULT,
    combine,
    multi_output,
    recommend_offline,
    recommend_online,
    single_output,
    split_online_offline,
)

if TYPE_CHECKING:
    from meta_agent.application.context import RunContext

logger = logging.getLogger(__name__)

EMPTY_ANSWER = json.dumps(EMPTY_RESULT, ensure_ascii=False)

# dry_run 下用的演示处方，让离线测试台也能看到完整输出结构（不发任何网络请求）。
_DEMO_ITEMS: tuple[dict[str, Any], ...] = (
    {"Id": 1, "Name": "脑卒中线上通用处方", "Type": 0},
    {"Id": 2, "Name": "脑卒中评估处方", "Type": 2},
    {"Id": 3, "Name": "脑卒中线下机器人处方", "Type": 1},
)

_DEMO_SCHEDULE = {
    "2026/11/17": [{"Time": "09:00-10:00", "Used": 1}, {"Time": "14:00-15:00", "Used": 0}],
    "2026/11/18": [{"Time": "10:00-11:00", "Used": 0}],
}


def _demo_details() -> dict[str, dict[str, Any]]:
    return {
        "1": {
            "Id": 1,
            "Name": "脑卒中线上通用处方",
            "Indication": "脑卒中",
            "Type": 0,
            "Prescription": "线上通用康复处方",
            "EvaluationList": [{"GameName": "ADL量表-Barthel指数"}],
            "TrainList": [],
            "ScheduleInfo": None,
        },
        "2": {
            "Id": 2,
            "Name": "脑卒中评估处方",
            "Indication": "脑卒中",
            "Type": 2,
            "Prescription": "线上评估处方",
            "EvaluationList": [{"GameName": "简易智力状态评估表"}],
            "TrainList": [],
            "ScheduleInfo": None,
        },
        "3": {
            "Id": 3,
            "Name": "脑卒中线下机器人处方",
            "Indication": "脑卒中",
            "Type": 1,
            "Prescription": "线下机器人训练处方",
            "EvaluationList": [],
            "TrainList": [{"GameName": "梦幻森林", "GameFunc": "行走游戏"}],
            "ScheduleInfo": [
                {"TenantName": "上海金矢机器人", "Schedule": _DEMO_SCHEDULE},
                {"TenantName": "康复医院（演示）", "Schedule": _DEMO_SCHEDULE},
            ],
        },
    }


def _talk(ctx: RunContext) -> str:
    """会话里累积的对话文本；会话变量为空时用历史轮次兜底。"""
    stored = str(ctx.memory.get_variable("talk", "") or "").strip()
    if stored:
        return stored
    parts: list[str] = []
    for turn in (ctx.memory.turns or [])[-6:]:
        for key in ("user", "assistant"):
            text = str(turn.get(key) or "").strip()
            if text:
                parts.append(text)
    return " 。".join(parts)


def _parse_object(text: str) -> dict[str, Any]:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned[cleaned.find("\n") + 1 :]
        if cleaned.endswith("```"):
            cleaned = cleaned[: cleaned.rfind("```")]
    try:
        payload = json.loads(cleaned.strip())
    except (TypeError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _parse_gate(text: str) -> bool:
    """二次判断结果：优先按 JSON 取，取不到就沿用「包含 true」的口径。"""
    payload = _parse_object(text)
    if "has_medical_location_time" in payload:
        return bool(payload["has_medical_location_time"])
    return "true" in text.lower()


async def _call_model(ctx: RunContext, model: Any, prompt: str, tag: str) -> str:
    await ctx.llm_budget.take()
    response = await model.ainvoke(
        prompt,
        config={"tags": ["ai_prescription", tag], "metadata": {"component": "ai_prescription"}},
    )
    return _extract_content(response).strip()


async def _prescriptions(
    ctx: RunContext,
    client: Any,
    token: str,
    phone: str,
    disease: str,
    params: dict[str, Any],
    content: dict[str, str],
    rng: random.Random,
) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    """按疾病查处方；缺 Type 且允许写入时补齐后重查，返回 (列表, 详情字典)。"""
    if ctx.settings.dry_run:
        ctx.record.metrics["prescription_source"] = "demo"
        return [dict(item) for item in _DEMO_ITEMS], _demo_details()

    items = await webyun.list_prescriptions(ctx, client, token, disease)
    need = webyun.missing_types(items)
    ctx.record.metrics["prescription_missing_types"] = ",".join(need)

    client_identity = getattr(ctx, "client", None)
    if (
        need
        and ctx.settings.prescription_auto_create
        and denial(
            ctx.scope,
            ctx.settings,
            "clinical.prescription.create",
            excluded=client_identity.excluded_tools if client_identity else (),
        )
        is None
    ):
        payloads = webyun.build_payloads(params, need, content, rng)
        results = await webyun.add_prescriptions(ctx, client, token, phone, payloads)
        created = sum(1 for item in results if "body" in item)
        ctx.record.metrics["prescription_created"] = created
        logger.info(
            "prescriptions created",
            extra={
                "run_id": ctx.record.run_id,
                "event_type": "prescription_created",
                "fields": {"disease": disease, "created": created, "need": ",".join(need)},
            },
        )
        if payloads:
            items = await webyun.list_prescriptions(ctx, client, token, disease)
    elif need:
        ctx.record.metrics["prescription_created"] = 0
        logger.info(
            "prescription creation skipped",
            extra={
                "run_id": ctx.record.run_id,
                "event_type": "prescription_create_skipped",
                "fields": {"disease": disease, "need": ",".join(need)},
            },
        )

    details: dict[str, dict[str, Any]] = {}
    for item in items:
        identifier = item.get("Id")
        if not identifier:
            continue
        detail = await webyun.prescription_detail(ctx, client, token, identifier)
        if detail:
            details[str(identifier)] = detail
    ctx.record.metrics["prescription_items"] = len(items)
    return items, details


async def _nearest_hospitals(
    ctx: RunContext,
    model: Any,
    location: str | None,
    details: dict[str, dict[str, Any]],
) -> str | None:
    """LLM 从候选机构里挑最近的三个；没地点或模型失败就保留原地点。"""
    if not location or location == "null" or model is None:
        return location

    tenants: list[str] = []
    for detail in details.values():
        for hospital in detail.get("ScheduleInfo") or []:
            name = hospital.get("TenantName")
            if name and name not in tenants:
                tenants.append(name)
    if not tenants:
        return location

    try:
        text = await _call_model(ctx, model, build_location_prompt(location, tenants), "location")
    except Exception:  # noqa: BLE001 - 距离筛选失败不该中断整条处方链路
        logger.warning(
            "nearest hospital selection failed",
            extra={"run_id": ctx.record.run_id, "event_type": "prescription_location_failed"},
        )
        return location
    return text or location


async def run_prescription(ctx: RunContext, query: str, model: Any) -> str:
    """执行 AI推荐处方链路，返回三字段 JSON 文本。"""
    if model is None:
        raise DomainError(
            "prescription_model_unavailable", "处方推荐模型暂不可用。", outcome="unavailable"
        )

    today = current_time()["dateTime"]
    history = _talk(ctx)
    talk = f"{history} 。{query}" if history else query
    rng = random.Random()

    gate_text = await _call_model(ctx, model, build_gate_prompt(query), "gate")
    if not _parse_gate(gate_text):
        ctx.record.metrics["prescription_mode"] = "no_signal"
        logger.info(
            "prescription skipped, no medical/location/time signal",
            extra={"run_id": ctx.record.run_id, "event_type": "prescription_no_signal"},
        )
        return EMPTY_ANSWER

    extraction = await _call_model(ctx, model, build_extract_prompt(query, talk, today), "extract")
    params = merge_params(extraction, ctx.memory.get_variable("params_json", ""), rng)
    ctx.memory.set_variable("talk", talk)
    ctx.memory.set_variable("params_json", params["params_json"])
    ctx.record.metrics["prescription_disease"] = params["disease"]
    ctx.record.metrics["prescription_multi"] = params["is_multi_disease"]

    if params["is_multi_disease"] == 0:
        ctx.record.metrics["prescription_mode"] = "no_disease"
        logger.info(
            "prescription skipped, no rehab disease",
            extra={"run_id": ctx.record.run_id, "event_type": "prescription_no_disease"},
        )
        return EMPTY_ANSWER

    client = ConsultationClient(ctx, "clinical.prescription")
    token = (ctx.business_inputs.get("token") or "").strip()
    if not token and not ctx.settings.dry_run:
        token = await _login(client, ctx.settings)
    phone = ctx.business_inputs.get("requesterphone") or ctx.business_inputs.get("patientphone")

    is_multi = params["is_multi_disease"] >= 2
    diseases = (
        disease_list(params["disease"], params["disease_reasons"])
        if is_multi
        else [params["disease"]]
    )
    ctx.record.metrics["prescription_mode"] = "multi" if is_multi else "single"

    all_items: list[dict[str, Any]] = []
    all_details: dict[str, dict[str, Any]] = {}
    for disease in diseases:
        content, source = knowledge.content_for(ctx, disease)
        ctx.record.metrics[f"prescription_knowledge_{disease}"] = source
        items, details = await _prescriptions(
            ctx, client, token, phone, disease, params, content, rng
        )
        all_items.extend(items)
        all_details.update(details)

    location = await _nearest_hospitals(ctx, model, params.get("location"), all_details)
    online_pairs, offline_pairs = split_online_offline(combine(all_items, all_details))

    online = recommend_online(online_pairs)
    offline = recommend_offline(offline_pairs, location, params.get("time"), rng)

    if is_multi:
        result = multi_output(online, offline, diseases, params["disease_reasons"])
    else:
        result = single_output(online, offline, params["disease_reasons"])

    ctx.record.metrics["prescription_online"] = len(result["online_prescriptions"])
    ctx.record.metrics["prescription_offline"] = len(result["offline_prescriptions"])
    logger.info(
        "prescription flow finished",
        extra={
            "run_id": ctx.record.run_id,
            "event_type": "prescription_finished",
            "fields": {
                "mode": ctx.record.metrics["prescription_mode"],
                "online": len(result["online_prescriptions"]),
                "offline": len(result["offline_prescriptions"]),
            },
        },
    )
    return json.dumps(result, ensure_ascii=False)


class Flow(FixedTaskFlow):
    branch = "ai_prescription"
    task_name = "处方推荐"
    metrics_key = "prescription_done"

    async def _pipeline(self, ctx: RunContext, model: Any) -> str:
        return await run_prescription(ctx, ctx.query, model)
