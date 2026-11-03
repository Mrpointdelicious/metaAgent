"""
创建日期：2026-10-15
文件功能：处方配置子流程。

流程（与 Dify 原工作流一致）：
  1. 意图识别 LLM（PRESCRIPTION_INTENT_PROMPT）把用户输入路由到「选择添加」或「推荐处方」；
  2. 不含「推荐处方」→ parse_prescription_selection 封装 prescription_selection 事件，输出 result；
  3. 含「推荐处方」→ 登录取 token → 取对话记录（对话内容）→ 取病历信息 →
     parse_disease_recommendation 提取 disease（覆盖会话变量 disease）→
     GET GetPrescriptionListNew（keyword=disease）→ parse_prescription_filter 过滤线下处方 →
     current_time → 有剩余处方才走推荐 LLM（PRESCRIPTION_RECOMMEND_PROMPT）→
     parse_prescription_recommendation 调详情接口封装 prescription_recommendation 事件，
     并把 prescription_ids / prescription_names / prescription_time_slots 覆盖到会话变量；
  4. 无剩余处方时固定输出「未找到适配的处方或已全部推荐最匹配的处方…」。
"""

import asyncio
import difflib
import json
import logging
import random
import re
import ssl
import urllib.request
from typing import Any

from meta_agent.application.context import RunContext
from meta_agent.config import get_settings
from meta_agent.contracts import DomainError

from .medical_record import (
    MEDICAL_INFO_URL,
    _extract_content,
    _get,
    _get_chat_records,
    _login,
    _phone,
    _to_text,
    current_time,
)
from .prompts import PRESCRIPTION_INTENT_PROMPT, PRESCRIPTION_RECOMMEND_PROMPT

logger = logging.getLogger(__name__)

PRESCRIPTION_LIST_URL = "http://192.168.8.71:8085/webyun/api/SysPrescription/GetPrescriptionListNew"
PRESCRIPTION_DETAIL_URL = "http://192.168.8.71:8085/webyun/api/SysPrescription/GetPrescriptionInfoById"

# 无剩余线下处方时的固定输出。
_NO_PRESCRIPTION_RESULT = (
    "[Answer:]未找到适配的处方或已全部推荐最匹配的处方，请在系统里新建相应处方或直接添加处方"
)


# --------------------------------------------------------------------------- #
# 解析函数（照搬 Dify 代码节点逻辑）
# --------------------------------------------------------------------------- #

def parse_prescription_selection(arg1: Any) -> dict[str, str]:
    """
    处方选择事件：解析分类器输出的「选择添加[Id...]」，封装为 prescription_selection 事件。
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
        display = "[Answer:]没有可选择的处方，请重试。"
    elif "全部" in text:
        display = "[Answer:]已选择全部处方，请确认。"
    else:
        display = f"[Answer:]已选择{len(chosen_ids)}个处方，请确认。"

    event = {
        "event": "prescription_selection",
        "display_text": display,
        "payload": {"selected_prescription_ids": chosen_ids},
    }
    return {"result": json.dumps(event, ensure_ascii=False)}


# --------------------------------------------------------------------------- #
# 疾病提取（照搬 Dify 代码节点：直连 DeepSeek，硬编码 key）
# --------------------------------------------------------------------------- #

DEEPSEEK_API_URL = "https://api.deepseek.com/chat/completions"
DEEPSEEK_MODEL = "deepseek-chat"

# 处方 Indication 词表（硬编码）：从 dbprescription.json 的 Indication 字段去重提取。
# 长词在前，保证匹配时优先命中更具体的病名。
PRESCRIPTION_INDICATIONS = sorted([
    "中枢损伤后肢体运动功能障碍者", "评估各类患者日常生活自理能力", "哮喘等慢性呼吸系统疾病",
    "外伤后平衡功能障碍人群", "综合评估功能障碍者生活", "各类急慢性疼痛患者",
    "脑瘫等肢体痉挛患者", "髌股关节疼痛综合征", "脑损伤致失语患者", "肩术后功能评估",
    "腰椎间盘突出症", "膝关节骨关节炎", "运动功能障碍者", "运动后气短患者",
    "关节置换术后", "左膝关节胀痛", "挛缩瘢痕评估", "筛查吞咽障碍",
    "类风湿关节炎", "脑卒中后遗症", "评估运动耐力", "半月板损伤", "坐骨神经痛",
    "手功能障碍", "紧张性头痛", "腰间盘突出", "运动受限者", "阿兹海默症",
    "颈肩腰腿痛", "骨关节疾病", "骨质疏松症", "关节损伤", "呛咳人群", "帕金森病",
    "心肺疾病", "术后增生", "睡眠障碍", "社交能力", "肢体瘫痪", "脊髓损伤",
    "腰肌劳损", "膝关节炎", "运动损伤", "韧带拉伤", "韧带损伤", "骨关节炎",
    "骨折术后", "骨质增生", "偏头痛", "关节炎", "帕金森", "慢阻肺", "抑郁症",
    "滑膜炎", "肌少症", "肌无力", "肩周炎", "肩损伤", "肾结石", "脑卒中",
    "腿骨折", "颈肩痛", "下肢", "偏瘫", "僵硬", "卒中", "外伤", "心肺",
    "感冒", "粘连", "脚疼", "过敏", "骨折",
], key=len, reverse=True)


def _normalize(s):
    s = (s or "").strip()
    s = re.sub(r"[\s,，、/;；。．.]+", "", s)
    return s


def _extract_raw_disease(arg):
    """从病历信息接口响应里提取疾病片段（取 MedicalHistory 第一个逗号前）。"""
    root = arg
    if isinstance(arg, str):
        try:
            root = json.loads(arg)
        except Exception:
            return ""
    if not isinstance(root, dict):
        return ""

    body = root.get("body", root)
    if isinstance(body, str):
        try:
            body = json.loads(body)
        except Exception:
            return ""
    if not isinstance(body, dict):
        return ""

    data = body.get("data")
    patient = data.get("PatientDetails") if isinstance(data, dict) else None
    disease = patient.get("MedicalHistory", "") if isinstance(patient, dict) else ""

    if isinstance(disease, str):
        disease = disease.split("，")[0].split(",")[0].strip()

    return disease or ""


def _call_deepseek(priority_text, candidates):
    """调用 DeepSeek，按优先级从候选标准病名里选出最匹配的一个。返回选中的病名（或空串）。"""
    if not candidates:
        return ""

    system = (
        "你是医疗术语标准化助手。你会收到三段信息：医生的要求、医患对话内容、病历疾病描述，"
        "以及一个候选列表（含病种、症状、评估/评定适用人群等）。请严格按以下优先级从候选列表里选出最匹配的一项："
        "1) 医生的要求：只要医生提出了明确要求（疾病、症状、评估/评定/量表需求等），就无条件以医生的要求为准，忽略其它信息。"
        "例如「要脑卒中处方」→「脑卒中」；「做日常生活自理能力评估」→「评估各类患者日常生活自理能力」。"
        "2) 医患对话内容：若医生没有明确要求，就根据对话里的疾病/症状/评估需求判断，不要被病历描述覆盖（对话是当前最新信息）。"
        "3) 病历疾病描述：只有当医生的要求和对话内容里都没有有效信息时，才用病历描述判断。"
        "硬性要求：输出必须是候选列表里的某一项，绝对禁止输出列表之外的任何词。"
        "即使关联很弱，也必须挑一个最相近的。只输出该项，不要任何解释、标点或多余文字。"
    )
    user = f"{priority_text}\n候选病名列表：{'、'.join(candidates)}"

    body = {
        "model": DEEPSEEK_MODEL,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "temperature": 0,
        "max_tokens": 64,
    }

    req = urllib.request.Request(
        DEEPSEEK_API_URL,
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Authorization": "Bearer " + get_settings().prescription_deepseek_api_key.get_secret_value(),
            "Content-Type": "application/json",
        },
        method="POST",
    )

    # Dify 沙箱精简容器常缺 CA 证书，这里跳过 SSL 验证（仅本机内网调 DeepSeek 用）
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE

    try:
        with urllib.request.urlopen(req, timeout=20, context=ctx) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        return "__DEEPSEEK_ERROR__: " + str(e)

    try:
        return (data["choices"][0]["message"]["content"] or "").strip()
    except Exception as e:
        return "__DEEPSEEK_ERROR__: " + str(e)


def _align_answer(answer, terms):
    """把 DeepSeek 返回的病名对齐到词表里真实存在的病名（防止 AI 改字）。"""
    a = _normalize(answer)
    if not a or a in ("无", "无匹配", "无相关", "无合适", "无适合"):
        return ""

    for t in terms:                       # 精确
        if t == a:
            return t
    for t in terms:                       # 子串
        if t in a or a in t:
            return t
    return ""


def _fallback_closest(text, terms):
    """兜底：从词表里选与描述最相近的病名，保证必有输出。"""
    q = _normalize(text)
    if not q or not terms:
        return ""
    return max(terms, key=lambda t: difflib.SequenceMatcher(None, q, _normalize(t)).ratio())


# 「重新推荐」意图词：命中这些词时，直接复用上次输出的疾病，不重新提取
_RE_RECOMMEND_WORDS = ("重新推荐", "重新匹配", "再推荐", "重新生成", "换一批")


def _is_re_recommend(text):
    """判断是否表达「重新推荐」意图（精确等于意图词）。"""
    return (text or "").strip() in _RE_RECOMMEND_WORDS


def parse_disease_recommendation(
    arg: Any, conversation: str, doctor_request: str, last_disease: str = ""
) -> dict[str, str]:
    """
    按优先级判断疾病：医生的要求 > 医患对话内容 > 病历 MedicalHistory，
    最终适配到处方 Indication 标准病名。
    arg:            病历信息接口响应（HTTP 响应 JSON，内含 MedicalHistory）
    conversation:   医患对话内容（字符串，可为空）
    doctor_request: 医生的要求（字符串，可为空）
    last_disease:   上次输出的疾病（会话变量传入，可为空）；命中「重新推荐」意图时直接复用
    输出：
      disease: 最相近的 Indication 标准病名
    """
    doctor = (doctor_request or "").strip()
    conv = (conversation or "").strip()

    # 「重新推荐」：仅当有上次疾病时直接复用；否则回退到正常提取
    last = (last_disease or "").strip()
    if last and (_is_re_recommend(conv) or _is_re_recommend(doctor)):
        return {"disease": last}

    raw = _extract_raw_disease(arg)

    if not raw and not doctor and not conv:
        return {"disease": ""}

    priority_text = (
        f"医生的要求：{doctor or '（无）'}\n"
        f"医患对话内容：{conv or '（无）'}\n"
        f"病历疾病描述：{raw or '（无）'}"
    )

    answer = _call_deepseek(priority_text, PRESCRIPTION_INDICATIONS)
    if answer.startswith("__DEEPSEEK_ERROR__:"):
        return {"disease": answer}

    matched = _align_answer(answer, PRESCRIPTION_INDICATIONS)
    if not matched:
        matched = _fallback_closest(raw or conv or doctor, PRESCRIPTION_INDICATIONS)

    return {"disease": matched}


# --------------------------------------------------------------------------- #
# 处方列表过滤（照搬 Dify 代码节点）
# --------------------------------------------------------------------------- #

def _empty() -> dict[str, Any]:
    return {
        "result": json.dumps({"data": []}, ensure_ascii=False),
        "hospitals": json.dumps([], ensure_ascii=False),
        "count": 0,
        "start_index": 1,
    }


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


def parse_prescription_filter(arg: Any, last_ids: Any = "") -> dict[str, Any]:
    """
    过滤并压缩处方列表：仅保留 Type==1（线下）处方，并拆分输出：
      - result：处方列表 {"data":[{Id,Type,Name}]}，已剔除上次推荐过的处方（缩进格式，方便 LLM 读取）。
      - hospitals：空闲机构名称列表（JSON 字符串数组），所有处方共用同一份。
      - count：本轮可推荐（未在上次推荐过）的线下处方数量。
      - start_index：推荐序号起始值 = 上次推荐数量 + 1（首次/无历史为 1），供提示词续排序号。
    已占用机构（occupied_slots 非空）不参与推荐，直接丢弃。
    last_ids：上次已推荐处方的 Id 列表（JSON 数组或逗号分隔），按 Id 精确匹配后从本轮候选里剔除，避免重复推荐。
    """
    root = json.loads(arg) if isinstance(arg, str) else arg
    if not isinstance(root, dict):
        return _empty()

    # 兼容完整响应（body 为嵌套字符串）与 body 层直接作为输入
    body = root.get("body", root)
    if isinstance(body, str):
        try:
            body = json.loads(body)
        except Exception:
            body = {}

    data = body.get("data") if isinstance(body, dict) else {}
    # 新结构：data 是对象，线下处方在 offline_prescriptions；旧结构 data 直接是列表
    items = data.get("offline_prescriptions", []) if isinstance(data, dict) else data
    if not isinstance(items, list):
        items = []

    excluded = _parse_last_ids(last_ids)

    out = []
    hospital_names = []
    seen = set()
    for it in items:
        if not isinstance(it, dict) or it.get("Type") != 1:
            continue

        # 重新推荐：按 Id 精确匹配，剔除上次已推荐过的处方
        pid = str(it.get("Id", "")).strip()
        if pid and pid in excluded:
            continue

        out.append({
            "Id": it.get("Id"),
            "Type": it.get("Type"),
            "Name": it.get("Name", ""),
        })

        # 新结构：Hospitals = [{"Name":..., "occupied_slots":[...]}]
        # 只有 occupied_slots 为空（[] 或 {}）才表示设备空闲、可推荐
        for h in it.get("Hospitals", []):
            if not isinstance(h, dict):
                continue
            name = h.get("Name", "")
            slots = h.get("occupied_slots")
            if isinstance(slots, (dict, list)) and not slots:
                if name and name not in seen:
                    seen.add(name)
                    hospital_names.append(name)

    start_index = len(excluded) + 1 if excluded else 1

    return {
        "result": json.dumps({"data": out}, ensure_ascii=False, indent=2),
        "hospitals": json.dumps(hospital_names, ensure_ascii=False),
        "count": len(out),
        "start_index": start_index,
    }


# --------------------------------------------------------------------------- #
# 推荐结果封装（照搬 Dify 代码节点，含详情接口调用）
# --------------------------------------------------------------------------- #

def _to_int(v):
    """尽量转成 int，失败则保留原值。"""
    try:
        return int(v)
    except (TypeError, ValueError):
        return v


def _parse_last_ids_list(raw):
    """把 last_ids 解析成有序的字符串列表（去重、保序），兼容 JSON 数组 / 逗号顿号分隔 / 单个值。"""
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
    return s[m.end():] if m else s


def _parse_last_slots(raw):
    """把 last_time_slots 解析成有序的 dict 列表（与 last_ids 顺序对齐），兼容 JSON 数组 / 单个对象；非法项用 {} 占位。"""
    if raw is None:
        return []
    if isinstance(raw, str):
        s = raw.strip()
        if not s:
            return []
        try:
            raw = json.loads(s)
        except Exception:
            return []
    if isinstance(raw, list):
        return [x if isinstance(x, dict) else {} for x in raw]
    if isinstance(raw, dict):
        return [raw]
    return []


def _fetch_detail(pid, base, token):
    """调用处方详情接口，返回 data 字典；失败或无 data 时返回 {}。"""
    detail = None
    try:
        url = f"{base}?Id={pid}"
        req = urllib.request.Request(url, headers={"Authorization": "Bearer " + token})
        with urllib.request.urlopen(req, timeout=10) as resp:
            detail = json.loads(resp.read().decode("utf-8"))
    except Exception:
        detail = None
    if isinstance(detail, dict) and isinstance(detail.get("data"), dict):
        return detail["data"]
    return {}


def _normalize_imgs(raw):
    """把 GameImg 统一成字符串列表。"""
    imgs = raw or []
    if isinstance(imgs, str):
        imgs = [imgs]
    return [str(x) for x in imgs if x]


def _build_details(d, fallback_imgs):
    """按 TrainList 组装 Details；详情缺图时用 fallback_imgs 兜底。"""
    details = []
    for i, t in enumerate(d.get("TrainList") or []):
        if isinstance(t, dict):
            img = t.get("GameImg") or ""
            if not img and i < len(fallback_imgs):
                img = fallback_imgs[i]
            details.append({
                "DeviceType": t.get("DeviceType", ""),
                "GameName": t.get("GameName", ""),
                "GameFunc": t.get("GameFunc", ""),
                "GameIcon": t.get("GameIcon", ""),
                "GameImg": img,
                "SettingTime": t.get("SettingTime", 0),
            })
    if not details and fallback_imgs:
        details = [{
            "DeviceType": "", "GameName": "", "GameFunc": "",
            "GameIcon": "", "GameImg": img, "SettingTime": 0,
        } for img in fallback_imgs]
    return details


def parse_prescription_recommendation(
    arg1: Any,
    arg2: Any,
    arg3: Any,
    arg4: Any,
    arg5: Any,
    arg6: Any = "",
    arg7: Any = "",
    arg8: Any = "",
) -> dict[str, Any]:
    """
    线下处方推荐事件：按提示词推荐的处方（用「训练项目」= 处方 Name 匹配）调用接口获取详情，封装为推荐事件。
    arg1: 线下处方列表 JSON（{"data":[...]}，data 里 Type==1 的处方含 Id、Name，不含机构信息）
    arg2: 当前日期（预留，未参与解析）
    arg3: LLM 生成的训练处方推荐文本（含「训练项目:」「训练地点:」「时间安排:」）
    arg4: token（裸 JWT 字符串）
    arg5: 空闲机构名称列表（JSON 字符串数组）
    arg6: 上次已推荐处方的 Id 列表 last_ids（JSON 数组或逗号分隔，可为空）；本次会在此基础上累加
    arg7: 上次已推荐处方的名称列表 last_names（JSON 数组或逗号分隔，可为空）；与 arg6 顺序一一对应，本次在此基础上累加
    arg8: 上次已推荐处方的推荐时间档期列表 last_time_slots（JSON 数组，元素为 {"Hospital":..., "Time":...}，与 arg6 顺序一一对应，可为空）；本次在此基础上累加
    输出 result: JSON 字符串；prescription_ids: array[number]（累计推荐过的处方 Id 列表 = 上次 + 本次）；prescription_names: array[string]（累计推荐过的处方名称列表，带「序号. 」前缀，与 prescription_ids 顺序一一对应）；prescription_time_slots: array[object]（累计推荐过的处方时间档期列表，与 prescription_ids 顺序一一对应）
    """
    # 1. 解析处方列表，取 Type==1 的线下处方
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
    if isinstance(root, dict) and isinstance(root.get("data"), list):
        items = [it for it in root["data"] if isinstance(it, dict) and it.get("Type") == 1]

    # 2. 解析 token
    token = arg4 if isinstance(arg4, str) else ""
    if token.startswith("{"):
        try:
            obj = json.loads(token)
            token = obj.get("token", "") if isinstance(obj, dict) else token
        except Exception:
            pass

    # 3. 解析空闲机构名称列表
    hospital_names = []
    if isinstance(arg5, str):
        try:
            _h = json.loads(arg5)
            if isinstance(_h, list):
                hospital_names = [x for x in _h if isinstance(x, str)]
        except Exception:
            pass
    elif isinstance(arg5, list):
        hospital_names = [x for x in arg5 if isinstance(x, str)]

    hospitals = [{"Name": name, "occupied_slots": []} for name in hospital_names]
    if len(hospitals) > 3:
        hospitals = random.sample(hospitals, 3)

    # 4. 从 text 按「推荐{序号}」分块解析「训练项目」「训练地点」「时间安排」
    text = arg3 if isinstance(arg3, str) else ""

    def extract_value(line, marker):
        for m in (marker + ":", marker + "："):
            idx = line.find(m)
            if idx != -1:
                return line[idx + len(m):].strip()
        return ""

    blocks = {}   # 序号 -> {"Name","Hospital","Time"}
    cur_idx = None
    for line in text.split("\n"):
        s = line.strip()
        m = re.match(r"推荐\s*(\d+)", s)
        if m:
            cur_idx = int(m.group(1))
            blocks.setdefault(cur_idx, {"Name": "", "Hospital": "", "Time": ""})
            continue
        n = extract_value(s, "训练项目")
        if n:
            if cur_idx is not None:
                blocks[cur_idx]["Name"] = n
            continue
        h = extract_value(s, "训练地点")
        if h:
            if cur_idx is not None:
                blocks[cur_idx]["Hospital"] = h
            continue
        t = extract_value(s, "时间安排")
        if t:
            if cur_idx is not None:
                blocks[cur_idx]["Time"] = t

    # 兜底：若文本没出现「推荐{序号}」编号，按出现顺序依次编号
    if not blocks:
        seq = 1
        pending_name = ""
        pending_h = ""
        for line in text.split("\n"):
            s = line.strip()
            n = extract_value(s, "训练项目")
            if n:
                pending_name = n
                continue
            h = extract_value(s, "训练地点")
            if h:
                pending_h = h
                continue
            t = extract_value(s, "时间安排")
            if t:
                blocks[seq] = {"Name": pending_name, "Hospital": pending_h, "Time": t}
                seq += 1

    # 5. 按 Name 匹配处方：用「消耗式」列表，支持同名处方（同名时逐个匹配到不同处方，避免重复输出同一个）
    available_items = list(items)

    def find_and_consume(name):
        nm = (name or "").strip()
        if not nm:
            return None
        # 先精确匹配，再包含匹配；每次命中后从池里移除，保证同名处方不会被重复选中
        for i, it in enumerate(available_items):
            if (it.get("Name") or "").strip() == nm:
                return available_items.pop(i)
        for i, it in enumerate(available_items):
            n2 = (it.get("Name") or "").strip()
            if nm and n2 and (nm in n2 or n2 in nm):
                return available_items.pop(i)
        return None

    # 6. 只输出提示词推荐到的处方，逐个调用接口获取详情
    prescriptions = []
    # 累计推荐 Id：先带上上次已推荐（last_ids），本次新推荐再追加，避免下一轮把历史处方又推出来
    prev_ids = _parse_last_ids_list(arg6)
    prescription_ids = [_to_int(x) for x in prev_ids]
    seen_ids = {str(x).strip() for x in prev_ids}
    # 累计推荐名称：与 Id 一一对应地累加（last_names），先剥掉旧序号，最后统一重新编号
    prev_names = [_strip_index(x) for x in _parse_last_ids_list(arg7)]
    prescription_names = list(prev_names)
    # 累计推荐时间档期：与 Id 一一对应地累加（last_time_slots），跨轮保留每个处方的推荐时间/地点
    prescription_time_slots = _parse_last_slots(arg8)
    base = PRESCRIPTION_DETAIL_URL
    # 先按累计 prescription_ids 顺序补回上次已推荐的处方，逐个调接口取详情，
    # 使 payload.prescriptions 与 prescription_ids 一一对应（第一次 N 个，第二次累计 2N 个）。
    for _i, _pid in enumerate(prescription_ids):
        _d = _fetch_detail(_pid, base, token)
        _slot = prescription_time_slots[_i] if _i < len(prescription_time_slots) else {}
        prescriptions.append({
            "display_index": _i + 1,
            "Id": _pid,
            "Name": _d.get("Name") or (prescription_names[_i] if _i < len(prescription_names) else ""),
            "Indication": _d.get("Indication", ""),
            "Prescription": _d.get("Prescription", ""),
            "Hospitals": hospitals,
            "Details": _build_details(_d, []),
            "time_slot_recommendation": [_slot] if (_slot.get("Hospital") or _slot.get("Time")) else [],
        })

    for idx in sorted(blocks.keys()):
        b = blocks[idx]
        it = find_and_consume(b.get("Name", ""))
        if it is None:
            continue  # 训练项目名称没匹配到处方，跳过

        pid = it.get("Id")
        pid_str = str(pid).strip()
        if pid_str in seen_ids:
            continue  # 已在累计列表里（防御性去重）
        prescription_ids.append(_to_int(pid))
        seen_ids.add(pid_str)

        d = _fetch_detail(pid, base, token)

        # arg1 里已带 GameImg（来自列表接口），详情接口缺图时用它兜底，保证 GameImg 不为空
        fallback_imgs = _normalize_imgs(it.get("GameImg"))

        details = _build_details(d, fallback_imgs)

        slot = {"Hospital": b.get("Hospital", ""), "Time": b.get("Time", "")}
        own_time_slot = [slot] if (slot.get("Hospital") or slot.get("Time")) else []

        resolved_name = d.get("Name", it.get("Name", ""))
        prescriptions.append({
            "display_index": len(prescription_ids),
            "Id": pid,
            "Name": resolved_name,
            "Indication": d.get("Indication", ""),
            "Prescription": d.get("Prescription", ""),
            "Hospitals": hospitals,
            "Details": details,
            "time_slot_recommendation": own_time_slot,
        })
        prescription_names.append(resolved_name)
        prescription_time_slots.append(slot)

    # 7. 组装输出
    event = {
        "event": "prescription_recommendation",
        "display_text": "[Answer:]" + text,
        "payload": {"prescriptions": prescriptions},
    }
    return {
        "result": json.dumps(event, ensure_ascii=False),
        "prescription_ids": prescription_ids,
        "prescription_names": [f"{i + 1}. {n}" for i, n in enumerate(prescription_names)],
        "prescription_time_slots": prescription_time_slots,
    }


# --------------------------------------------------------------------------- #
# 提示词组装与编排
# --------------------------------------------------------------------------- #

def _build_intent_prompt(query: str, names_text: str, ids_text: str) -> str:
    return (
        PRESCRIPTION_INTENT_PROMPT.replace("{{#sys.query#}}", query)
        .replace("{{#conversation.prescription_names#}}", names_text)
        .replace("{{#conversation.prescription#}}", ids_text)
    )


def _build_recommend_prompt(
    result: str,
    count: int,
    hospitals: str,
    disease: str,
    conversation: str,
    query: str,
    last_ids: str,
    start_index: int,
    date_time: str,
) -> str:
    count_str = str(count)
    start_str = str(start_index)

    # 先替换成对出现的「{{var}}{{#node.field#}}」组合，避免与 node 引用之间残留。
    prompt = (
        PRESCRIPTION_RECOMMEND_PROMPT.replace("{{result}}{{#1788330722900.result#}}", result)
        .replace("{{count}}{{#1788330722900.count#}}", count_str)
        .replace("{{hospitals}}{{#1788330722900.hospitals#}}", hospitals)
        .replace("{{disease}}{{#1788326057678.disease#}}", disease)
        .replace("{{conversation}}{{#17891032893480.body#}}", conversation)
        .replace("{{start_index}}{{#1788330722900.start_index#}}", start_str)
        .replace("{{dateTime}}{{#17883334080060.dateTime#}}", date_time)
    )
    # 再替换正文里独立出现的占位符。
    prompt = (
        prompt.replace("{{#sys.query#}}", query)
        .replace("{{last_ids}}", last_ids)
        .replace("{{result}}", result)
        .replace("{{count}}", count_str)
        .replace("{{hospitals}}", hospitals)
        .replace("{{disease}}", disease)
        .replace("{{conversation}}", conversation)
        .replace("{{start_index}}", start_str)
        .replace("{{dateTime}}", date_time)
    )
    return prompt


async def _run_recommendation(ctx: RunContext, query: str, model: Any) -> str:
    """推荐处方链路：登录 → 对话/病历 → 提取疾病 → 处方列表 → 过滤 → LLM 推荐 → 封装事件。"""
    client = ctx.backend.client

    token = await _login(client)

    # 对话内容（可选）：手机号缺失或接口不可用都不阻断，告警后按空继续。
    request_phone = _phone(ctx, "requesterphone", "phone")
    conversation = ""
    if not request_phone:
        logger.warning(
            "prescription conversation skipped, request phone missing",
            extra={
                "run_id": ctx.record.run_id,
                "event_type": "prescription_conversation_skipped_missing_phone",
            },
        )
    else:
        try:
            conversation = await _get_chat_records(client, token, request_phone)
        except DomainError as exc:
            logger.warning(
                "prescription conversation unavailable, continuing with empty",
                extra={
                    "run_id": ctx.record.run_id,
                    "event_type": "prescription_conversation_unavailable_continuing",
                    "fields": {"code": exc.code},
                },
            )

    # 病历信息（可选）：提取疾病用，缺失/不可用都按空继续。
    patient_phone = _phone(ctx, "patientphone", "phone")
    medical_text = ""
    if not patient_phone:
        logger.warning(
            "prescription medical info skipped, patient phone missing",
            extra={
                "run_id": ctx.record.run_id,
                "event_type": "prescription_medical_skipped_missing_phone",
            },
        )
    else:
        try:
            medical_resp = await _get(
                client,
                MEDICAL_INFO_URL,
                token=token,
                params={"Phone": patient_phone},
                step="prescription_medical",
                label="病历信息查询",
            )
            medical_text = medical_resp.text
        except DomainError as exc:
            logger.warning(
                "prescription medical info unavailable, continuing with empty",
                extra={
                    "run_id": ctx.record.run_id,
                    "event_type": "prescription_medical_unavailable_continuing",
                    "fields": {"code": exc.code},
                },
            )

    # 提取疾病（直连 DeepSeek，同步阻塞，放入线程池），覆盖会话变量 disease。
    last_disease = ctx.memory.get_variable("disease") or ""
    disease_result = await asyncio.to_thread(
        parse_disease_recommendation, medical_text, conversation, query, last_disease
    )
    disease = disease_result.get("disease", "")
    ctx.memory.set_variable("disease", disease)

    # 处方列表接口。
    list_resp = await _get(
        client,
        PRESCRIPTION_LIST_URL,
        token=token,
        params={"keyword": disease},
        step="prescription_list",
        label="处方列表查询",
    )

    last_ids = ctx.memory.get_variable("prescription_ids")
    filtered = parse_prescription_filter(list_resp.text, last_ids)
    result = filtered["result"]
    hospitals = filtered["hospitals"]
    count = filtered["count"]
    start_index = filtered["start_index"]

    ctx.record.metrics["prescription_offline_count"] = count

    # 无剩余线下处方（result 不含 Id）→ 固定输出，不进推荐 LLM。
    if count <= 0:
        logger.info(
            "no available prescription, returning fixed result",
            extra={
                "run_id": ctx.record.run_id,
                "event_type": "prescription_no_available",
                "fields": {"count": count},
            },
        )
        return _NO_PRESCRIPTION_RESULT

    date_time = current_time()["dateTime"]

    last_names = ctx.memory.get_variable("prescription_names")
    last_time_slots = ctx.memory.get_variable("prescription_time_slots")

    prompt = _build_recommend_prompt(
        result,
        count,
        hospitals,
        disease,
        conversation,
        query,
        _to_text(last_ids),
        start_index,
        date_time,
    )

    await ctx.llm_budget.take()
    rec_resp = await model.ainvoke(
        prompt,
        config={
            "tags": ["consultation_room", "prescription", "recommend"],
            "metadata": {"component": "consultation_room_prescription_recommend"},
        },
    )
    rec_text = _extract_content(rec_resp)

    final = await asyncio.to_thread(
        parse_prescription_recommendation,
        result,
        date_time,
        rec_text,
        token,
        hospitals,
        last_ids,
        last_names,
        last_time_slots,
    )

    # 累计推荐结果覆盖会话变量，供下一轮「选择添加」/「重新推荐」复用。
    ctx.memory.set_variable("prescription_ids", final["prescription_ids"])
    ctx.memory.set_variable("prescription_names", final["prescription_names"])
    ctx.memory.set_variable("prescription_time_slots", final["prescription_time_slots"])

    return final["result"]


async def run_prescription(ctx: RunContext, query: str, model: Any) -> str:
    """执行处方配置完整链路，返回最终 answer 文本（事件 JSON 或固定文案）。"""
    if model is None:
        raise DomainError(
            "prescription_model_unavailable", "处方配置模型暂不可用。", outcome="unavailable"
        )

    # 1. 意图识别：选择添加 / 推荐处方。
    names_text = _to_text(ctx.memory.get_variable("prescription_names"))
    ids_text = _to_text(ctx.memory.get_variable("prescription_ids"))

    intent_prompt = _build_intent_prompt(query, names_text, ids_text)

    await ctx.llm_budget.take()
    intent_resp = await model.ainvoke(
        intent_prompt,
        config={
            "tags": ["consultation_room", "prescription", "intent"],
            "metadata": {"component": "consultation_room_prescription_intent"},
        },
    )
    intent_text = _extract_content(intent_resp)

    ctx.record.metrics["prescription_intent"] = (
        "recommend" if "推荐处方" in intent_text else "select"
    )

    if "推荐处方" in intent_text:
        return await _run_recommendation(ctx, query, model)

    return parse_prescription_selection(intent_text)["result"]
