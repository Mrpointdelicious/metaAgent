"""
创建日期：2026-10-15
文件功能：量表测评子流程。

与处方配置同构（流程一致），仅代码与推荐提示词不同：
  1. 意图识别 LLM（复用 PRESCRIPTION_INTENT_PROMPT）路由到「选择添加」或「推荐处方」；
  2. 不含「推荐处方」→ parse_scale_selection 封装
  prescription_selection 事件，输出 result；
  3. 含「推荐处方」→ 登录取 token → 取对话记录/病历信息 →
  parse_disease_recommendation 提取
     disease（覆盖会话变量 disease）→ GET
     GetPrescriptionListNew（keyword=disease）→
     parse_scale_filter 过滤 Type==0 量表 →
     有剩余量表才走推荐 LLM（SCALE_RECOMMEND_PROMPT）→
     parse_scale_recommendation 调详情接口取
     EvaluationList 封装
     prescription_recommendation 事件，
     并把 scale_ids / scale_names 覆盖到会话变量；
  4. 无剩余量表时固定输出「未找到适配的量表或已全部推荐最匹配的量表…」。
"""

import asyncio
import json
import logging
import re
from typing import Any

from meta_agent.application.context import RunContext
from meta_agent.contracts import DomainError

from .access import ConsultationClient, fetch_details
from .medical_record import (
    MEDICAL_INFO_URL,
    _extract_content,
    _get,
    _get_chat_records,
    _login,
    _phone,
    _to_text,
)
from .prescription import (
    PRESCRIPTION_INTENT_PROMPT,
    PRESCRIPTION_LIST_URL,
    extract_disease,
)
from .prompts import SCALE_RECOMMEND_PROMPT

logger = logging.getLogger(__name__)

SCALE_DETAIL_URL = "http://192.168.8.71:8085/webyun/api/SysPrescription/GetPrescriptionInfoById"

# 无剩余量表时的固定输出。
_NO_SCALE_RESULT = (
    "[Answer:]未找到适配的量表或已全部推荐最匹配的量表，请在系统里新建相应量表或直接添加量表"
)


# -------------------------------------------
# -------------------------------- #
# 解析函数（照搬 Dify 代码节点逻辑）
# -------------------------------------------
# -------------------------------- #


def parse_scale_selection(arg1: Any) -> dict[str, str]:
    """
    量表选择事件：解析分类器输出的「选择添加[Id...]」，封装为 prescription_selection 事件。
    arg1: 分类器输出文本，如「选择添加[125]」「选择添加[620,621]」「选择添加全部[620,621]」
    输出 result: JSON 字符串（prescription_selection 事件）
    """
    text = arg1 if isinstance(arg1, str) else ""

    # 提取方括号里的 Id 列表（兼容「选择添加[...]」「选择添加全部[...]」）
    chosen_ids = []
    m = re.search(r"\[([^\]]*)\]", text)
    if m:
        for part in m.group(1).split(","):
            part = part.strip()
            if part.isdigit():
                chosen_ids.append(int(part))
    # 去重、保序
    chosen_ids = list(dict.fromkeys(chosen_ids))

    if not chosen_ids:
        display = "[Answer:]没有可选择的量表，请重试。"
    elif "全部" in text:
        display = "[Answer:]已选择全部量表，请确认。"
    else:
        display = f"[Answer:]已选择{len(chosen_ids)}个量表，请确认。"

    event = {
        "event": "prescription_selection",
        "display_text": display,
        "payload": {"selected_prescription_ids": chosen_ids},
    }
    return {"result": json.dumps(event, ensure_ascii=False)}


def _parse_last_ids(raw):
    """把 last_ids 解析成 Id 字符串集合，兼容 JSON 数组 / 逗号顿号分隔 / 单个值。"""
    if raw is None:
        return set()
    if isinstance(raw, (list, tuple)):
        return {str(x).strip() for x in raw if str(x).strip()}
    if isinstance(raw, (int, float)):
        return {str(raw).strip()}
    s = (str(raw) or "").strip()
    if not s:
        return set()
    if s.startswith("["):
        try:
            parsed = json.loads(s)
            if isinstance(parsed, list):
                return {str(x).strip() for x in parsed if str(x).strip()}
        except Exception:
            pass
    return {x.strip() for x in s.replace("，", ",").replace("、", ",").split(",") if x.strip()}


def parse_scale_filter(arg: Any, last_ids: Any = "") -> dict[str, Any]:
    """
    过滤处方列表：剔除 Type != 0 的处方，其余结构保持与输入一致。
    输出 result：过滤后的完整 JSON 字符串（code/message/data 结构与输入相同，
    data 仅保留 Type==0、且未被上次推荐过的处方）。用字符串输出以避免嵌套对象过深。
    输出 count：本轮可推荐（未在上次推荐过）的 Type==0 处方数量（整数），供提示词据此生成推荐数量。
    输出 start_index：推荐序号起始值 = 上次推荐数量 + 1（首次/无历史为 1），供提示词续排序号。
    last_ids：上次已推荐处方的 Id 列表（JSON 数组或逗号分隔），按
    Id 精确匹配后从本轮候选里剔除，避免重复推荐。
    """
    root = json.loads(arg) if isinstance(arg, str) else arg
    if not isinstance(root, dict):
        return {"result": json.dumps(root, ensure_ascii=False), "count": 0, "start_index": 1}

    # 兼容完整响应（body 是嵌套字符串）与 body 层
    body = root.get("body", root)
    if isinstance(body, str):
        try:
            body = json.loads(body)
        except Exception:
            return {"result": json.dumps(root, ensure_ascii=False), "count": 0, "start_index": 1}

    # 新结构：data 是对象，Type==0（量表）在
    # online_prescriptions；旧结构 data 直接是列表
    data = body.get("data") if isinstance(body, dict) else None
    if isinstance(data, dict):
        raw = data.get("online_prescriptions", [])
    else:
        raw = data if isinstance(data, list) else []

    excluded = _parse_last_ids(last_ids)

    items = []
    for it in raw:
        if not isinstance(it, dict) or it.get("Type") != 0:
            continue
        # 重新推荐：按 Id 精确匹配，剔除上次已推荐过的量表
        pid = str(it.get("Id", "")).strip()
        if pid and pid in excluded:
            continue
        items.append(it)

    count = len(items)

    if isinstance(body, dict):
        body["data"] = items

    # 若输入为完整响应且 body 原本是字符串，还原为字符串，保持结构一致
    if "body" in root:
        root["body"] = (
            json.dumps(body, ensure_ascii=False) if isinstance(root["body"], str) else body
        )

    start_index = len(excluded) + 1 if excluded else 1

    return {
        "result": json.dumps(root, ensure_ascii=False),
        "count": count,
        "start_index": start_index,
    }


# -------------------------------------------
# -------------------------------- #
# 推荐结果封装（照搬 Dify 代码节点，含详情接口调用）
# -------------------------------------------
# -------------------------------- #


def _to_int(v):
    """尽量转成 int，失败则保留原值。"""
    try:
        return int(v)
    except (TypeError, ValueError):
        return v


def _norm(name):
    """归一化量表名称：去空白与常见分隔符号（括号、横杠、下划线、冒号、斜杠、顿号等），用于容错匹配。"""
    return re.sub(r"[\s()（）\[\]【】\-—_—、，。．:：;；·・/\\|]+", "", (name or ""))


def _parse_last_ids_list(raw):
    """
    把 last_ids 解析成有序的 Id 字符串列表（去重、保序），兼容 JSON
    数组 / 逗号顿号分隔 / 单个值。
    """
    result = []
    seen = set()

    def add(x):
        s = str(x).strip()
        if s and s not in seen:
            seen.add(s)
            result.append(s)

    if raw is None:
        return result
    if isinstance(raw, (list, tuple)):
        for x in raw:
            add(x)
        return result
    if isinstance(raw, (int, float)):
        add(raw)
        return result
    s = (str(raw) or "").strip()
    if not s:
        return result
    if s.startswith("["):
        try:
            parsed = json.loads(s)
            if isinstance(parsed, list):
                for x in parsed:
                    add(x)
                return result
        except Exception:
            pass
    for part in s.replace("，", ",").replace("、", ",").split(","):
        add(part)
    return result


def _strip_index(s):
    """去掉名称前可能带有的「1.」「1、」「1）」等序号前缀，便于跨轮重新编号。"""
    m = re.match(r"^\s*\d+\s*[.、)）:：]\s*", s)
    return s[m.end() :] if m else s


def _fetch_detail(pid, base, token, detail_cache=None):
    return (detail_cache or {}).get(str(pid), {})


def parse_scale_recommendation(
    arg1: Any, arg2: Any, arg3: Any, arg4: Any = "", arg5: Any = "", detail_cache=None
) -> dict[str, Any]:
    """
    量表测评推荐事件：按提示词推荐的量表（用「量表名称」匹配处方
    Name）调用接口获取 EvaluationList，封装为推荐事件。
    arg1: 量表列表 JSON（提取量表处方.py 的 result，data
    里为 Type==0 的处方，含 Id、Name 等）
    arg2: LLM 生成的量表推荐文本（含「量表名称:」）
    arg3: token（裸 JWT 字符串，或 {"token":"..."}）
    arg4: 上次已推荐量表的 Id 列表 last_ids（JSON 数组或逗号分隔，可为空）；本次会在此基础上累加
    arg5: 上次已推荐量表的名称列表 last_names（JSON
    数组或逗号分隔，可为空）；与 arg4 顺序一一对应，本次在此基础上累加
    输出 result: JSON 字符串；scale_ids:
    array[number]（累计推荐过的量表 Id 列表 = 上次 +
    本次）；scale_names:
    array[string]（累计推荐过的量表名称列表，带「序号. 」前缀，与
    scale_ids 顺序一一对应）
    """
    # 1. 解析量表列表，取 Type==0 的处方
    root = arg1
    if isinstance(arg1, str):
        try:
            root = json.loads(arg1)
        except Exception:
            root = None
    if isinstance(root, dict) and "result" in root and isinstance(root["result"], str):
        try:
            root = json.loads(root["result"])
        except Exception:
            root = None

    items = []
    if isinstance(root, dict):
        body = root.get("body", root)
        if isinstance(body, str):
            try:
                body = json.loads(body)
            except Exception:
                body = None
        if isinstance(body, dict) and isinstance(body.get("data"), list):
            items = [it for it in body["data"] if isinstance(it, dict) and it.get("Type") == 0]

    # 2. 解析 token
    token = arg3 if isinstance(arg3, str) else ""
    if token.startswith("{"):
        try:
            obj = json.loads(token)
            token = obj.get("token", "") if isinstance(obj, dict) else token
        except Exception:
            pass

    # 3. 从 text 按「推荐{序号}」分块解析「量表名称」
    text = arg2 if isinstance(arg2, str) else ""

    def extract_value(line, marker):
        for m in (marker + ":", marker + "："):
            idx = line.find(m)
            if idx != -1:
                return line[idx + len(m) :].strip()
        return ""

    blocks = {}  # 序号 -> 量表名称
    cur_idx = None
    for line in text.split("\n"):
        s = line.strip()
        m = re.match(r"推荐\s*(\d+)", s)
        if m:
            cur_idx = int(m.group(1))
            blocks.setdefault(cur_idx, "")
            continue
        n = extract_value(s, "量表名称")
        if n and cur_idx is not None:
            blocks[cur_idx] = n

    # 兜底：若文本没出现「推荐{序号}」编号，按出现顺序依次编号
    if not blocks:
        seq = 1
        for line in text.split("\n"):
            s = line.strip()
            n = extract_value(s, "量表名称")
            if n:
                blocks[seq] = n
                seq += 1

    # 4. 建立 量表名称 -> 量表
    # 映射，并做模糊匹配（归一化去掉括号/横杠/下划线等，避免 LLM
    # 复制名称时丢符号导致匹配失败）。
    #    量表名可能来自 Name 字段，也可能来自 Details
    # 数组里的字符串（如 "ADL日常生活能力量表"），两者都纳入映射。
    name_to_item = {}
    norm_to_item = {}

    def _add_alias(alias, it):
        alias = (alias or "").strip()
        if not alias:
            return
        name_to_item.setdefault(alias, it)
        norm_to_item.setdefault(_norm(alias), it)

    for it in items:
        _add_alias(it.get("Name"), it)
        det = it.get("Details")
        if isinstance(det, list):
            for d in det:
                if isinstance(d, str):
                    _add_alias(d, it)

    def find_item(name):
        name = (name or "").strip()
        if not name:
            return None
        # 1) 原文精确
        if name in name_to_item:
            return name_to_item[name]
        # 2) 归一化后精确（处理 "ADL日常生活能力量表" vs "ADL(日常生活能力量表)" 这类差异）
        nkey = _norm(name)
        if nkey and nkey in norm_to_item:
            return norm_to_item[nkey]
        # 3) 归一化后双向包含（短名对长名，如 "Barthel指数" vs "ADL量表-Barthel指数"）
        if nkey:
            for nnm, it in norm_to_item.items():
                if nnm and (nkey in nnm or nnm in nkey):
                    return it
        return None

    # 5. 逐个调用接口获取 EvaluationList -> Details
    prescriptions = []
    # 累计推荐 Id：先带上上次已推荐（last_ids），本次新推荐再追加，避免下一轮把历史量表又推出来
    prev_ids = _parse_last_ids_list(arg4)
    scale_ids = [_to_int(x) for x in prev_ids]
    seen_ids = {str(x).strip() for x in prev_ids}
    # 累计推荐名称：与 Id 一一对应地累加（last_names），先剥掉旧序号，最后统一重新编号
    prev_names = [_strip_index(x) for x in _parse_last_ids_list(arg5)]
    scale_names = list(prev_names)
    base = SCALE_DETAIL_URL
    # 先按累计 scale_ids 顺序补回上次已推荐的量表，逐个调接口取详情，
    # 使 payload.prescriptions 与 scale_ids
    # 一一对应（第一次 N 个，第二次累计 2N 个）。
    for _i, _pid in enumerate(scale_ids):
        _d = _fetch_detail(_pid, base, token, detail_cache)
        prescriptions.append(
            {
                "display_index": _i + 1,
                "Id": _pid,
                "Name": _d.get("Name") or (scale_names[_i] if _i < len(scale_names) else ""),
                "Indication": _d.get("Indication", ""),
                "Prescription": _d.get("Prescription", ""),
                "Details": _d.get("EvaluationList", []) or [],
            }
        )

    for idx in sorted(blocks.keys()):
        name = blocks[idx]
        it = find_item(name)
        if it is None:
            continue  # 量表名称没匹配到处方，跳过

        pid = it.get("Id")
        pid_str = str(pid).strip()
        if pid_str in seen_ids:
            continue  # 已在累计列表里（防御性去重）
        scale_ids.append(_to_int(pid))
        seen_ids.add(pid_str)

        d = _fetch_detail(pid, base, token, detail_cache)

        # Details 来自接口返回的 EvaluationList
        details = d.get("EvaluationList", []) or []

        resolved_name = d.get("Name", it.get("Name", ""))
        prescriptions.append(
            {
                "display_index": len(scale_ids),
                "Id": pid,
                "Name": resolved_name,
                "Indication": d.get("Indication", ""),
                "Prescription": d.get("Prescription", ""),
                "Details": details,
            }
        )
        scale_names.append(resolved_name)

    # 6. 组装输出
    event = {
        "event": "prescription_recommendation",
        "display_text": "[Answer:]" + text,
        "payload": {"prescriptions": prescriptions},
    }
    return {
        "result": json.dumps(event, ensure_ascii=False),
        "scale_ids": scale_ids,
        "scale_names": [f"{i + 1}. {n}" for i, n in enumerate(scale_names)],
    }


# -------------------------------------------
# -------------------------------- #
# 提示词组装与编排
# -------------------------------------------
# -------------------------------- #


def _build_intent_prompt(query: str, names_text: str, ids_text: str) -> str:
    return (
        PRESCRIPTION_INTENT_PROMPT.replace("{{#sys.query#}}", query)
        .replace("{{#conversation.prescription_names#}}", names_text)
        .replace("{{#conversation.prescription#}}", ids_text)
    )


def _build_scale_recommend_prompt(
    scale_list: str,
    count: int,
    patient_condition: str,
    query: str,
    last_ids: str,
    start_index: int,
) -> str:
    count_str = str(count)
    start_str = str(start_index)

    # 先替换成对出现的「{{var}}{{#node.field#}}」组合。
    prompt = (
        SCALE_RECOMMEND_PROMPT.replace("{{scale_list}}{{#17888367374850.result#}}", scale_list)
        .replace("{{count}}{{#17888367374850.count#}}", count_str)
        .replace("{{patient_condition}}{{#17888367213310.disease#}}", patient_condition)
        .replace("{{conversation}}{{#sys.query#}}", query)
        .replace("{{last_ids}}{{#conversation.scale#}}", last_ids)
        .replace("{{start_index}}{{#17888367374850.start_index#}}", start_str)
    )
    # 再替换正文里独立出现的占位符。
    prompt = (
        prompt.replace("{{scale_list}}", scale_list)
        .replace("{{count}}", count_str)
        .replace("{{patient_condition}}", patient_condition)
        .replace("{{conversation}}", query)
        .replace("{{last_ids}}", last_ids)
        .replace("{{start_index}}", start_str)
    )
    return prompt


async def _run_recommendation(ctx: RunContext, query: str, model: Any) -> str:
    """量表推荐链路：登录 → 对话/病历 → 提取疾病 → 量表列表 → 过滤 → LLM 推荐 → 封装事件。"""
    client = ConsultationClient(ctx)

    token = await _login(client, ctx.settings)

    # 对话内容（供疾病提取用，可选）：缺失/不可用都按空继续。
    request_phone = _phone(ctx, "requesterphone", "phone")
    conversation = ""
    if not request_phone:
        logger.warning(
            "scale conversation skipped, request phone missing",
            extra={
                "run_id": ctx.record.run_id,
                "event_type": "scale_conversation_skipped_missing_phone",
            },
        )
    else:
        try:
            conversation = await _get_chat_records(client, token, request_phone)
        except DomainError as exc:
            logger.warning(
                "scale conversation unavailable, continuing with empty",
                extra={
                    "run_id": ctx.record.run_id,
                    "event_type": "scale_conversation_unavailable_continuing",
                    "fields": {"code": exc.code},
                },
            )

    # 病历信息（可选）：提取疾病用，缺失/不可用都按空继续。
    patient_phone = _phone(ctx, "patientphone", "phone")
    medical_text = ""
    if not patient_phone:
        logger.warning(
            "scale medical info skipped, patient phone missing",
            extra={
                "run_id": ctx.record.run_id,
                "event_type": "scale_medical_skipped_missing_phone",
            },
        )
    else:
        try:
            medical_resp = await _get(
                client,
                MEDICAL_INFO_URL,
                token=token,
                params={"Phone": patient_phone},
                step="scale_medical",
                label="病历信息查询",
            )
            medical_text = medical_resp.text
        except DomainError as exc:
            logger.warning(
                "scale medical info unavailable, continuing with empty",
                extra={
                    "run_id": ctx.record.run_id,
                    "event_type": "scale_medical_unavailable_continuing",
                    "fields": {"code": exc.code},
                },
            )

    # 提取疾病（直连 DeepSeek，同步阻塞，放入线程池），覆盖会话变量 disease。
    last_disease = ctx.memory.get_variable("disease") or ""
    disease_result = await extract_disease(
        ctx, model, medical_text, conversation, query, last_disease
    )
    disease = disease_result.get("disease", "")
    ctx.memory.set_variable("disease", disease)

    # 量表列表接口（与处方共用 GetPrescriptionListNew，keyword=disease）。
    list_resp = await _get(
        client,
        PRESCRIPTION_LIST_URL,
        token=token,
        params={"keyword": disease},
        step="scale_list",
        label="量表列表查询",
    )

    last_ids = ctx.memory.get_variable("scale_ids")
    filtered = parse_scale_filter(list_resp.text, last_ids)
    result = filtered["result"]
    count = filtered["count"]
    start_index = filtered["start_index"]

    ctx.record.metrics["scale_online_count"] = count

    # 无剩余量表（result 不含 Id）→ 固定输出，不进推荐 LLM。
    if count <= 0:
        logger.info(
            "no available scale, returning fixed result",
            extra={
                "run_id": ctx.record.run_id,
                "event_type": "scale_no_available",
                "fields": {"count": count},
            },
        )
        return _NO_SCALE_RESULT

    prompt = _build_scale_recommend_prompt(
        result, count, disease, query, _to_text(last_ids), start_index
    )

    await ctx.llm_budget.take()
    rec_resp = await model.ainvoke(
        prompt,
        config={
            "tags": ["consultation_room", "scale_assessment", "recommend"],
            "metadata": {"component": "consultation_room_scale_assessment_recommend"},
        },
    )
    rec_text = _extract_content(rec_resp)

    last_names = ctx.memory.get_variable("scale_names")
    final = await asyncio.to_thread(
        parse_scale_recommendation, result, rec_text, token, last_ids, last_names
    )

    detail_cache = await fetch_details(ctx, client, SCALE_DETAIL_URL, token, final["scale_ids"])
    final = parse_scale_recommendation(
        result, rec_text, token, last_ids, last_names, detail_cache=detail_cache
    )

    # 累计推荐结果覆盖会话变量，供下一轮「选择添加」/「重新推荐」复用。
    ctx.memory.set_variable("scale_ids", final["scale_ids"])
    ctx.memory.set_variable("scale_names", final["scale_names"])

    return final["result"]


async def run_scale_assessment(ctx: RunContext, query: str, model: Any) -> str:
    """执行量表测评完整链路，返回最终 answer 文本（事件 JSON 或固定文案）。"""
    if model is None:
        raise DomainError(
            "scale_assessment_model_unavailable", "量表测评模型暂不可用。", outcome="unavailable"
        )

    # 1. 意图识别：选择添加 / 推荐处方。
    names_text = _to_text(ctx.memory.get_variable("scale_names"))
    ids_text = _to_text(ctx.memory.get_variable("scale_ids"))

    intent_prompt = _build_intent_prompt(query, names_text, ids_text)

    await ctx.llm_budget.take()
    intent_resp = await model.ainvoke(
        intent_prompt,
        config={
            "tags": ["consultation_room", "scale_assessment", "intent"],
            "metadata": {"component": "consultation_room_scale_assessment_intent"},
        },
    )
    intent_text = _extract_content(intent_resp)

    ctx.record.metrics["scale_intent"] = "recommend" if "推荐处方" in intent_text else "select"

    if "推荐处方" in intent_text:
        return await _run_recommendation(ctx, query, model)

    result = parse_scale_selection(intent_text)["result"]
    selected = json.loads(result)["payload"]["selected_prescription_ids"]
    candidates = {
        str(value) for value in _parse_last_ids_list(ctx.memory.get_variable("scale_ids"))
    }
    if any(str(value) not in candidates for value in selected):
        raise DomainError(
            "selection_not_recommended", "请从本次问诊已推荐的量表中选择。", outcome="clarification"
        )
    return result
