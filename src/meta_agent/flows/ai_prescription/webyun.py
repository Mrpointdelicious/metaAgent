"""
创建日期：2026-11-16
文件功能：AI推荐处方直连 webyun 的三个接口（对应 Dify 的「处方列表 / 处方列表2 /
处方列表3 / 新建处方 / 单疾病输出里的详情查询」HTTP 与代码节点）。

接口：
  GET  /webyun/api/SysPrescription/GetPrescriptionList?keyword=<疾病>
  GET  /webyun/api/SysPrescription/GetPrescriptionInfoById?Id=<处方 Id>
  POST /webyun/api/SysPrescription/AddPrescription

单条请求失败不抛给上层：Dify 每个节点都挂了 fail-branch，这里等价处理为
返回空结果 + 一条 warning 日志，由调用方按「没查到处方」继续走。
"""

from __future__ import annotations

import json
import logging
from typing import Any

import httpx

from meta_agent.application.context import RunContext
from meta_agent.contracts import DomainError
from meta_agent.observability.trace import CURRENT_RUN_ID

logger = logging.getLogger(__name__)

LIST_PATH = "/webyun/api/SysPrescription/GetPrescriptionList"
DETAIL_PATH = "/webyun/api/SysPrescription/GetPrescriptionInfoById"
ADD_PATH = "/webyun/api/SysPrescription/AddPrescription"

# 处方类型：0 线上通用、1 线下、2 评估、3 治疗、4 用药。
ALL_TYPES = ("0", "1", "2", "3", "4")

# 量表名 → 线上通用处方的 Type 编号（与原节点映射表一致）。
SCALE_TYPE_MAP = {
    "AD8认知障碍自评表": 1,
    "ADL量表-Barthel指数": 2,
    "ADL日常生活能力量表": 3,
    "Beck抑郁量表": 4,
    "GAD-7广泛性焦虑量表": 5,
    "Rivermead行为记忆量表记录表": 6,
    "汉密尔顿抑郁量表": 7,
    "简易认知状态评价量表": 8,
    "简易智力状态评估表": 9,
    "老年人认知功能筛查量表": 10,
    "蒙特利尔认知评估量表记录表": 11,
    "认知障碍分级评定表": 12,
}


def _warn(event: str, **fields: Any) -> None:
    logger.warning(
        "prescription api %s",
        event,
        extra={
            "run_id": CURRENT_RUN_ID.get(),
            "event_type": f"prescription_{event}",
            "fields": fields,
        },
    )


def missing_types(items: Any) -> list[str]:
    """列表里已有的 Type → 还缺哪些 Type（顺序固定 0..4）。"""
    present = {
        item.get("Type")
        for item in (items or [])
        if isinstance(item, dict) and isinstance(item.get("Type"), int)
    }
    return [value for value in ALL_TYPES if int(value) not in present]


async def list_prescriptions(
    ctx: RunContext, client: Any, token: str, keyword: str
) -> list[dict[str, Any]]:
    """按疾病关键词查处方列表；失败返回空列表。"""
    url = f"{ctx.settings.prescription_api_base}{LIST_PATH}"
    try:
        response = await client.get(
            url,
            headers={"Authorization": f"Bearer {token}"},
            params={"keyword": keyword},
            timeout=15.0,
        )
        response.raise_for_status()
        body = response.json()
    except (httpx.HTTPError, DomainError, ValueError) as exc:
        _warn("list_failed", keyword=keyword, error=type(exc).__name__)
        return []

    items = body.get("data") if isinstance(body, dict) else None
    return [item for item in items or [] if isinstance(item, dict)]


async def prescription_detail(
    ctx: RunContext, client: Any, token: str, prescription_id: Any
) -> dict[str, Any]:
    """处方详情；失败或空数据返回 {}。"""
    if not prescription_id:
        return {}
    url = f"{ctx.settings.prescription_api_base}{DETAIL_PATH}"
    try:
        response = await client.get(
            url,
            headers={"Authorization": f"Bearer {token}"},
            params={"Id": str(prescription_id)},
            timeout=15.0,
        )
        response.raise_for_status()
        body = response.json()
    except (httpx.HTTPError, DomainError, ValueError) as exc:
        _warn("detail_failed", prescription_id=prescription_id, error=type(exc).__name__)
        return {}

    data = body.get("data") if isinstance(body, dict) else None
    if not isinstance(data, dict) or not any(data.values()):
        return {}
    return {
        "Id": prescription_id,
        "Name": data.get("Name") or f"处方{prescription_id}",
        "Indication": data.get("Indication", ""),
        "Type": data.get("Type"),
        "Prescription": data.get("Prescription", ""),
        "TrainList": data.get("TrainList") or [],
        "EvaluationList": data.get("EvaluationList") or [],
        "ScheduleInfo": data.get("ScheduleInfo"),
    }


def _offline_content(params: dict[str, Any], rng: Any) -> list[dict[str, Any]]:
    """线下处方的游戏清单：每个训练模式一条，训练时长 20–30 分钟随机。"""

    def split(value: Any) -> list[str]:
        if isinstance(value, str):
            return [item.strip() for item in value.split(",") if item.strip()]
        if isinstance(value, list):
            return [str(item).strip() for item in value if str(item).strip()]
        return []

    funcs = split(params.get("GameFunc"))
    icons = split(params.get("GameIcon"))
    names = split(params.get("GameName"))
    device = params.get("DeviceType") or ""
    serial = params.get("SerialNumber") or f"{device}-H000000000"

    if funcs and len(funcs) == len(icons) == len(names):
        pairs = zip(funcs, icons, names, strict=True)
    else:
        # 参数对不上就退回固定一条，和原节点的兜底一致。
        pairs = [("行走游戏", "dForest", "梦幻森林")]

    return [
        {
            "GameFunc": func,
            "GameIcon": icon,
            "GameName": name,
            "DepartName": "无",
            "DeviceType": device,
            "TenantName": "上海金矢机器人",
            "SettingTime": rng.randint(20, 30),
            "SerialNumber": serial,
        }
        for func, icon, name in pairs
    ]


def build_payloads(
    params: dict[str, Any],
    need: list[str],
    content: dict[str, str],
    rng: Any,
) -> list[dict[str, Any]]:
    """按缺失的 Type 组装 AddPrescription 请求体（与原代码分支一一对应）。"""
    disease = params.get("disease") or "未知"
    scale = params.get("scale") or ""
    payloads: list[dict[str, Any]] = []

    if "1" in need and params.get("DeviceType") and params.get("GameName"):
        payloads.append(
            {
                "Name": f"{disease}线下处方",
                "Indication": str(disease),
                "Prescription": f"这是一个新建的{disease}线下处方，训练内容由AI分析推荐而来",
                "Type": 1,
                "Content": json.dumps(_offline_content(params, rng), ensure_ascii=False),
            }
        )

    def online_item(label: str, type_code: int, text: str) -> dict[str, Any]:
        return {
            "Name": f"{disease}{label}处方",
            "Indication": str(disease),
            "Prescription": f"自动推荐的{disease}{label}内容",
            "Type": type_code,
            "Content": json.dumps([{"Room": "线上门诊", "ScaleName": text}], ensure_ascii=False),
        }

    if "0" in need and scale:
        payloads.append(
            {
                "Name": f"{disease}线上处方",
                "Indication": str(disease),
                "Prescription": f"这是一个新建的{disease}线上处方，训练内容由AI分析推荐而来",
                "Type": 0,
                "Content": json.dumps(
                    [
                        {
                            "Room": "线上门诊",
                            "ScaleName": scale,
                            "Type": SCALE_TYPE_MAP.get(scale),
                        }
                    ],
                    ensure_ascii=False,
                ),
            }
        )

    for type_code, key, label in ((2, "Evaluation", "评估"), (3, "Treatment", "治疗")):
        text = (content.get(key) or "").strip()
        if str(type_code) in need and text not in ("", "无", "暂无"):
            payloads.append(online_item(label, type_code, text))

    drug = (content.get("Drug") or "").strip()
    if "4" in need and drug not in ("", "无", "暂无"):
        payloads.append(online_item("用药", 4, drug))

    return payloads


async def add_prescriptions(
    ctx: RunContext,
    client: Any,
    token: str,
    phone: str,
    payloads: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """逐条新建处方；单条失败只记录错误，不影响其它 Type。"""
    url = f"{ctx.settings.prescription_api_base}{ADD_PATH}"
    headers = {
        "Authorization": f"Bearer {token}",
        "X-Phone-Number": phone,
        "Content-Type": "application/json",
    }
    results: list[dict[str, Any]] = []
    for payload in payloads:
        try:
            response = await client.post(url, headers=headers, json=payload, timeout=15.0)
            response.raise_for_status()
            results.append({"Type": payload["Type"], "body": response.json()})
        except (httpx.HTTPError, DomainError, ValueError) as exc:
            _warn("create_failed", type_code=payload.get("Type"), error=type(exc).__name__)
            results.append({"Type": payload["Type"], "error": type(exc).__name__})
    return results
