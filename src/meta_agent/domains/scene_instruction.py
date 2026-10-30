"""
创建日期：2026-10-10
文件功能：场景指令直连版——绕过 .NET，直接连 192.168.8.160/back/*，
解析用户话术为场景指令编码（504-scene-v2 契约）。

匹配逻辑对齐 AI_WebApi AiRehabToolsController.ExecutePipelineAsync
（ScoreMatch / ScoreMatchBest / PinyinScore + 单条唯一最佳匹配，无歧义）。

与参考实现（Dify 直连版 main()）的差异仅两点：
  1. I/O 由 requests 换成 httpx 异步（复用运行中的 AsyncClient）；
  2. user_query 来自输入 query，space_id 来自前端 inputs 的 space_id（空时回退 DEFAULT_SPACE_ID）。
其余打分顺序、前缀剥离、同义词、拼音兜底、25 字段契约均逐行一致。
"""

import json
import re
from datetime import datetime
from typing import Any
from urllib.parse import quote

import httpx

from meta_agent.application.context import RunContext
from meta_agent.contracts import DomainError, TaskResult, TaskSpec
from meta_agent.domains.chat_history import is_chat_history_query
from meta_agent.domains.instruction_classify import classify_instruction, extract_time_range, scale_to_room

VERSION_URL = "http://192.168.8.160/back/version"
QUERY_URL = "http://192.168.8.160/back/query"
API_KEY = "7a4453a4e27b91cbf3e28d554d2127e8"
API_TOKEN = "kfxsb15PUvaaj6rP"
TIMEOUT = 15  # 秒

# 量表/训练意图分流用到的问诊记录与医嘱接口（ga-robot，无需鉴权）。
SYS_MEDICAL_BASE = "http://www.ga-robot.com/webyun/api/SysMedical/"
RECORDS_URL = SYS_MEDICAL_BASE + "GetOnlineConsultationRecords"
ORDER_URL = SYS_MEDICAL_BASE + "GetSingleDoctorOrderInfo"

# 无问诊/无量表时的兜底文案。
NO_TRAINING_TASK_MSG = "暂无线上训练任务，您可去挂号进行就诊安排计划。"

# result 2（查报告/病历/问诊记录）用到的登录与查询接口。
LOGIN_URL = "http://www.ga-robot.com/webyun/api/Login/Login"
LOGIN_PHONE = "15932910402"
LOGIN_PASSWORD = "25d55ad283aa400af464c76d713c07ad"
GET_USER_ROLE_URL = "http://192.168.8.71:8085/webyun/api/SysUserInfo/GetUserIdAndRole"
GET_ALL_PLANS_URL = "http://192.168.8.71:8085/webyun/api/SysPlan/GetAllPlans"
GET_CONSULT_RECORDS_URL = "http://192.168.8.71:8085/webyun/api/SysMedical/GetOnlineConsultationRecordsByDoctor"
RECIPE_BASE = "http://192.168.8.160/cloud/recipe"

# 默认空间 ID（space_id 为空时使用）
DEFAULT_SPACE_ID = "67ecd511-5532-405d-9b70-1cd1c0532eed"

# 4 类点位的 SQL 模板（{0} = version_id），与 C# SqlTemplates 逐字一致
SQL_TEMPLATES = [
    "SELECT jsonb_agg(jsonb_build_object('id', elem.id, 'name', elem.elem->>'Name', 'intro', elem.elem->>'PointExplain')) AS result FROM meta.meta_space_data, LATERAL (SELECT e.*, row_number() OVER () - 1 AS id FROM jsonb_array_elements(data_content->'TeleportConfig'->'ChannelsConfig') WITH ORDINALITY e(elem)) AS elem WHERE version_id = {0};",
    "SELECT jsonb_agg(jsonb_build_object('id', elem.id, 'name', elem.elem->>'Name', 'intro', elem.elem->>'PointExplain')) AS result FROM meta.meta_space_data, LATERAL (SELECT e.*, row_number() OVER () - 1 AS id FROM jsonb_array_elements(data_content->'ScenePointCofig'->'Config') WITH ORDINALITY e(elem)) AS elem WHERE version_id = {0};",
    "SELECT jsonb_agg(jsonb_build_object('id', elem.id, 'name', elem.elem->>'des')) AS result FROM meta.meta_space_data, LATERAL (SELECT e.*, row_number() OVER () - 1 AS id FROM jsonb_array_elements(data_content->'CommandList') WITH ORDINALITY e(elem)) AS elem WHERE version_id = {0};",
    "SELECT jsonb_agg(jsonb_build_object('id', elem.id, 'name', elem.elem->>'des')) AS result FROM meta.meta_space_data, LATERAL (SELECT e.*, row_number() OVER () - 1 AS id FROM jsonb_array_elements(data_content->'FastTaskConfig'->'GameRoomConfig') WITH ORDINALITY e(elem)) AS elem WHERE version_id = {0};",
]

# 前缀 → 结果指令前缀（与 C# ResultPrefixes 一致）
RESULT_PREFIXES = ["[Telepor:]", "[ScenePoint:]", "[ClickPoint:]", "[GamePoint:]"]

# 前缀 → intent_type（与 C# switch 一致）
PREFIX_TO_TYPE = {
    "[Telepor:]": "telepor",
    "[ScenePoint:]": "scene_point",
    "[ClickPoint:]": "click_point",
    "[GamePoint:]": "game_point",
}

# 前缀剥离词（顺序与 C# PrefixWords 一致，靠前优先）
# 注意：不剥离“取消”“确认”。它们不是通用动作动词，而是反义语义标记。
PREFIX_WORDS = ["打开", "关闭", "去", "前往", "进入", "回到", "退回", "带我去", "带我", "帮我"]

# 同义词展开（顺序与 C# SynonymWords 一致）
SYNONYMS = [
    ("点开", "打开"), ("开启", "打开"),
    ("关掉", "关闭"), ("关上", "关闭"), ("退出", "关闭"),
    ("退出", "结束"), ("终止", "结束"), ("停止", "结束"),
    ("转换", "切换"), ("换", "切换"),
    ("返回", "退回"), ("后退", "退回"), ("进去", "进入"),
]

# 动作别名：用户话术 → 点位名片段（整段替换，非前缀替换）。
ACTION_ALIASES = [
    ("取消挂号", "挂号返回"),
    ("取消叫号", "拒绝叫号"),
    ("挂号确认", "确认挂号"),
    ("叫号确认", "确认叫号"),
    ("挂号取消", "挂号返回"),
    ("叫号取消", "拒绝叫号"),
    ("放大屏幕", "屏幕放大"),
    ("视角切换", "切换视角"),
]

# ---- 拆动作连词/标点（把一句多动作拆成多个 source_text）----
_SPLIT_RE = re.compile(r"[，,、；;]|并且|然后|接着|同时|顺便|以及|还有|并")

# ---- 输出契约常量 ----
TOOL_KIND = "navigation"
CONTRACT_VERSION = "504-scene-v2"

# 拼音兜底：对应 C# NPinyin.Pinyin.GetPinyin + NormalizePinyin；Python 用 pypinyin 等价替代
try:
    from pypinyin import lazy_pinyin as _pypinyin

    def _to_pinyin(s):
        try:
            return "".join(_pypinyin(s or "")).lower()
        except Exception:
            return ""
except Exception:
    def _to_pinyin(s):
        return ""


# ==================== 上游访问（异步） ====================

async def _post(client: httpx.AsyncClient, url: str, payload: dict[str, Any]) -> Any:
    """POST 上游接口，带 apikey/apitoken 头，返回解析后的 JSON。"""
    resp = await client.post(
        url,
        json=payload,
        headers={"apikey": API_KEY, "apitoken": API_TOKEN, "Content-Type": "application/json"},
        timeout=TIMEOUT,
    )
    resp.raise_for_status()
    return resp.json()


async def _resolve_version(client: httpx.AsyncClient, space_id: str) -> int:
    """取当前 space 的 version_id（对应 C# ResolveVersionAsync）。"""
    doc = await _post(client, VERSION_URL, {"spaceId": space_id})
    data = doc.get("data") if isinstance(doc, dict) else None
    version = int(data) if data is not None else None
    if version is None:
        raise RuntimeError("场景版本接口未返回有效 version")
    return version


async def _query_points(client: httpx.AsyncClient, version: int) -> list[dict[str, Any]]:
    """逐个 SQL 查点位，返回统一结构的点列表（对应 C# 4 类循环）。"""
    points: list[dict[str, Any]] = []
    for sql, prefix in zip(SQL_TEMPLATES, RESULT_PREFIXES):
        try:
            doc = await _post(client, QUERY_URL, {"sql": sql.format(version)})
            data = (doc or {}).get("data") if isinstance(doc, dict) else None
            arr = data.get("result") if isinstance(data, dict) else None
            if not isinstance(arr, list):
                continue
            for item in arr:
                if not isinstance(item, dict):
                    continue
                name = (item.get("name") or "").strip()
                if not name:
                    continue
                pid = item.get("id")
                points.append({
                    "prefix": prefix,
                    "id": str(int(pid)) if isinstance(pid, (int, float)) else str(pid or ""),
                    "name": name,
                    "intro": (item.get("intro") or "").strip(),
                })
        except Exception:
            # 某一类点位查询失败不阻塞整体，与 C# 逐类 try/catch 一致
            continue
    return points


# ==================== 匹配打分（依据 C# ScoreMatch 系列） ====================

def _strip_prefix(s: str) -> str:
    for w in PREFIX_WORDS:
        if s.startswith(w) and len(s) > len(w):
            return s[len(w):]
    return s


def _expand_synonyms(target: str):
    yield target
    for frm, to in SYNONYMS:
        if target.startswith(frm) and len(target) > len(frm):
            yield to + target[len(frm):]
    for frm, to in ACTION_ALIASES:
        if frm in target:
            yield target.replace(frm, to)


# ==================== 归一化：中文数字 / 同义词 ====================

_CN_DIGITS = {"零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
              "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}


def _cn_to_arabic(text: str) -> str:
    """连续中文数字（零~九、十、百、千）转阿拉伯数字，其余原样。"""
    def _conv(s):
        section = 0
        number = 0
        for ch in s:
            if ch in _CN_DIGITS:
                number = _CN_DIGITS[ch]
            elif ch == "十":
                section += (number or 1) * 10
                number = 0
            elif ch == "百":
                section += (number or 1) * 100
                number = 0
            elif ch == "千":
                section += (number or 1) * 1000
                number = 0
        return str(section + number)
    return re.sub(r"[零一二两三四五六七八九十百千]+", lambda m: _conv(m.group(0)), text)


_SYNONYM_NORM = [
    ("房间", "室"),
    ("房", "室"),
    ("手动", "人工"),
]


def _normalize(text: str) -> str:
    """归一化：中文数字转阿拉伯数字 + 同义写法统一。"""
    s = _cn_to_arabic(text or "")
    for frm, to in _SYNONYM_NORM:
        s = s.replace(frm, to)
    return s


_FLOOR_RE = re.compile(r"([0-9]+)\s*楼")


def _floor_of(text: str) -> int | None:
    """从点位/位置名里取楼层号（如「四楼1号评估室」→4，「一楼 入口」→1）。"""
    m = _FLOOR_RE.search(_normalize(text or ""))
    if not m:
        return None
    try:
        return int(m.group(1))
    except ValueError:
        return None


# 提问/咨询标记：命中说明用户在问问题而非下指令，避免把“帮我介绍一下XX”误判成指令。
_QUESTION_MARKERS = (
    "介绍", "解释", "说明", "是什么", "什么是", "怎么", "如何", "为什么",
    "哪些", "多少", "有没有", "能不能", "可以吗", "能否", "吗",
)

# 强指令动词（需带目标）：打开X、关闭X、去X、切换X…
_COMMAND_VERBS = ("打开", "关闭", "切换", "前往", "进入", "回到", "去")

# 可独立成指令的动词：投屏、返回、退回（无需带目标，单独也能下指令）。
_STANDALONE_VERBS = ("投屏", "返回", "退回")

# 界面按钮指令：确认/取消/拒绝/同意 + 挂号/叫号/取号/签到（如“确认挂号”“取消叫号”）。
# 注意：单独的“挂号”“我要挂号”是预约请求，应走在线医生工具，不算场景按钮指令。
_BUTTON_ACTIONS = ("确认", "取消", "拒绝", "同意")
_BUTTON_TARGETS = ("挂号", "叫号", "取号", "签到")

# 礼貌/铺垫前缀：判定前先剥掉，不影响后面的动词判断。
_POLITE_PREFIXES = ("请帮我", "帮我", "请", "麻烦", "带我", "带我去")

# 裸确认/指代指令：去掉确认词与句末语气词后只剩命令动词或为空（本身不含指令目标）。
# 如「需要打开」→「打开」、「好的」→「」、「打开吧」→「打开」；这类话术需结合上一轮对话找回目标。
_CONFIRM_WORDS = (
    "请帮我", "帮我", "请", "需要", "好的", "可以", "要",
    "嗯", "对", "行", "是的", "好", "麻烦", "可用", "能不能", "可不可以", "能否",
)
_TRAILING_PARTICLES = ("吧", "呗", "嘛", "吗", "啊", "呀", "哦", "呢", "咯")
_TRAILING_PUNCT = "。！？!?…．."
# 指令目标：动词后紧跟的一段中文/字母/数字（如「打开摄像头。」→「摄像头」）。
_TARGET_RE = re.compile(r"[一-鿿A-Za-z0-9]{1,12}")

# 可带目标的指令动词/礼貌前缀，用于找回被省略目标的确认/指代指令；长在前避免
# 「带我去」被「去」截断（如「可以带我去吗」→「带我去」→ 上一轮找回「带我去三楼」）。
_FOLLOWUP_VERBS = ("带我去", "带我") + _COMMAND_VERBS


def _followup_verb(query: str) -> str | None:
    """从裸确认指令里提取命令动词（如「需要打开」→「打开」、「可以带我去」→「带我去」）；无动词返回 None。"""
    q = _normalize((query or "").strip()).strip(_TRAILING_PUNCT)
    for p in _TRAILING_PARTICLES:
        if q.endswith(p):
            q = q[: -len(p)]
            break
    for w in _CONFIRM_WORDS:
        if q.startswith(w):
            q = q[len(w):]
            break
    for v in _FOLLOWUP_VERBS:
        if q.startswith(v):
            return v
    return None


def _bare_followup(query: str) -> bool:
    """判定是否为裸确认/指代指令（不含具体目标，需结合上一轮对话找回）。"""
    q = _normalize((query or "").strip()).strip(_TRAILING_PUNCT)
    if not q:
        return False
    for p in _TRAILING_PARTICLES:
        if q.endswith(p):
            q = q[: -len(p)]
            break
    for w in _CONFIRM_WORDS:
        if q.startswith(w):
            q = q[len(w):]
            break
    return not q or q in _FOLLOWUP_VERBS


def _target_after(text: str, verb: str, start: int = 0) -> str | None:
    """在文本里从 start 起找「verb+目标」，返回目标（剥离句末语气词）；找不到返回 None。"""
    idx = text.find(verb, start)
    while idx >= 0:
        m = _TARGET_RE.match(text, idx + len(verb))
        if m:
            target = m.group(0)
            for p in _TRAILING_PARTICLES:
                if target.endswith(p):
                    target = target[: -len(p)]
            if target:
                return target
        idx = text.find(verb, idx + len(verb))
    return None


def _recover_followup_command(query: str, turns: list[dict[str, Any]] | None) -> str | None:
    """从上一轮对话找回裸确认指令的目标，返回完整指令（如「打开摄像头」）；找不到返回 None。

    优先用当前话术里的动词去上一轮找「动词+目标」；纯确认（无动词）时取上一轮
    最后一次被提及的「动词+目标」。
    """
    verb = _followup_verb(query)
    for turn in reversed(turns or []):
        for text in (turn.get("assistant") or "", turn.get("user") or ""):
            if verb:
                target = _target_after(text, verb)
                if target is not None:
                    return verb + target
                continue
            for v in _FOLLOWUP_VERBS:
                idx = text.rfind(v)
                while idx >= 0:
                    target = _target_after(text, v, idx)
                    if target is not None:
                        return v + target
                    idx = text.rfind(v, 0, idx)
    return None


def is_command_query(query: str) -> bool:
    """确定性判定：用户话术是否更像场景/设备指令（而非提问）。

    与 resolve() 共用同一套指令词表（PREFIX_WORDS / SYNONYMS / ACTION_ALIASES），
    保证“识别”与“解析”口径一致，不靠 LLM 猜测。这里是粗筛，最终仍由
    resolve() 的确定性打分决定是否真的命中某个点位。
    """
    q = _normalize((query or "").strip())
    if not q:
        return False
    # 打开/查看历史对话记录 → 走打开历史对话工具，不是场景指令。
    if is_chat_history_query(q):
        return False
    # 提问/咨询优先：含疑问或介绍类词 → 不是指令。
    if any(m in q for m in _QUESTION_MARKERS):
        return False
    # 量表/训练意图（做量表/做训练/做AD8量表…）→ 走指令分流（量表→评估室→场景点）；
    # 带时间/修改病历（2）→ 走「登录→查报告/病历/问诊记录」。
    if classify_instruction(query) in ("0", "1", "2"):
        return True
    # 相对导航（回入口 / 回大厅 / 去上一层 / 去下一层）→ 指令。
    if _relative_nav_direction(q) is not None:
        return True
    # 动作别名（取消挂号→挂号返回、放大屏幕→屏幕放大…）→ 指令。
    if any(frm in q for frm, _ in ACTION_ALIASES):
        return True
    # 界面按钮指令（确认挂号/取消叫号/拒绝叫号…）→ 指令。
    if any((a + t) in q for a in _BUTTON_ACTIONS for t in _BUTTON_TARGETS):
        return True
    # 「带我去 / 带我」是明确导航指令（带我去三楼 / 带我过去）；单独出现也算指令，
    # 交给 resolve_scene_instruction 结合上一轮找回目标。
    if q.startswith("带我去") or q.startswith("带我"):
        return True
    # 剥掉礼貌前缀后，以强指令动词开头 → 指令。
    stripped = q
    for p in _POLITE_PREFIXES:
        if stripped.startswith(p):
            stripped = stripped[len(p):]
            break
    if any(stripped == v or stripped.startswith(v) for v in _STANDALONE_VERBS):
        return True
    # 裸确认/指代指令（如「需要打开」「打开吧」）：本身不含目标，但仍是场景指令意图，
    # 由 resolve_scene_instruction 结合上一轮对话找回目标。
    if _followup_verb(query) is not None and _bare_followup(query):
        return True
    return any(stripped.startswith(v) and len(stripped) > len(v) for v in _COMMAND_VERBS)


# 问路/导航提问标记：问「怎么去X」「X怎么走」等（与 is_command_query 互补，应走企业知识库）。
_NAV_QUESTION_MARKERS = (
    "怎么去", "怎么走", "怎么到", "怎么到达", "怎么前往",
    "如何走", "如何到", "如何到达", "如何前往",
    "怎样去", "怎样走", "怎样到",
    "咋去", "咋走", "咋到",
    "怎么上", "怎么下",
)
# 导航目标：楼层/房间/区域/位置等，避免把「怎么走」这类无目标话术误判为问路。
_NAV_TARGET_RE = re.compile(r"楼|层|室|区|地方|位置|地点|方向|中心|大厅|入口")


def is_navigation_question(query: str) -> bool:
    """判定用户话术是否在问路（如「怎么去三楼」「二楼怎么走」），应走企业知识库作答。

    与 is_command_query 互补：含疑问词（怎么/如何…）+ 导航目标是问路；
    「去三楼」（无疑问词）是下指令，由 is_command_query 判 True 走场景指令解析。
    """
    q = _normalize((query or "").strip())
    if not q:
        return False
    return _NAV_TARGET_RE.search(q) is not None and any(m in q for m in _NAV_QUESTION_MARKERS)


# 介绍/说明/功能类知识问题标记：应优先走知识库检索，而不是反问或凭通用知识作答。
_KNOWLEDGE_QUESTION_MARKERS = (
    "介绍", "说明", "解释", "是什么", "什么是", "功能", "怎么用", "如何使用", "在哪", "在哪里",
)


def is_knowledge_question(query: str) -> bool:
    """判定用户话术是否为介绍/说明/功能类知识问题（如「介绍左上角」「X是什么」「X功能」）。

    这类问题应优先调用 search_knowledge 检索知识库，而不是反问用户或凭通用知识作答。
    与 is_command_query 互补：含「介绍/说明/是什么」等提问标记的不会判成指令。
    """
    q = _normalize((query or "").strip())
    return bool(q) and any(m in q for m in _KNOWLEDGE_QUESTION_MARKERS)


def is_mixed_command_query(query: str) -> bool:
    """判定用户话术是否同时包含场景指令与其他请求（如“带我去五楼，并查报告”）。

    只有当拆出的多个动作里，至少一个是场景指令、且至少一个不是场景指令时，
    才视为混合意图；纯多动作指令（如“去五楼然后去二楼”）各部分都是指令，
    返回 False，仍交给 resolve_scene_instruction 一次解析。这样路由层才敢让
    agent 对“查报告”这类非指令部分另行调用对应工具，而不是只回指令结果。
    """
    parts = _split_actions(query)
    if len(parts) < 2:
        return False
    command_parts = [p for p in parts if is_command_query(p)]
    other_parts = [p for p in parts if not is_command_query(p)]
    return bool(command_parts) and bool(other_parts)


def _normalize_pinyin(s: str) -> str:
    """只保留小写 a-z（对应 C# NormalizePinyin 的 FormD 分解后取 a-z）。"""
    return "".join(ch for ch in (s or "").lower() if "a" <= ch <= "z")


def _pinyin_score(left: str, right: str) -> float:
    """对应 C# PinyinScore：pa==pb→0.7；pb 含 pa 且 pa≥3→0.65。

    注意：Dify 沙箱若未装 pypinyin，_to_pinyin 退化为空串，此时 ""=="" 会恒成立、
    导致所有候选拿 0.7、第一个点位被误命中。因此这里对空拼音直接返回 0。
    """
    pa = _normalize_pinyin(_to_pinyin(left))
    pb = _normalize_pinyin(_to_pinyin(right))
    if not pa or not pb:
        return 0.0
    if pa == pb:
        return 0.7
    if len(pa) >= 3 and pa in pb:
        return 0.65
    if len(pb) >= 3 and pb in pa:
        return 0.65
    return 0.0


# 句末标点：LLM 常给指令加句号，去掉以免破坏子串匹配
_PUNCT = "。！？!?…．."


def _score_match(target: str, name: str, intro: str) -> float:
    """单个候选的匹配分（0 表示不匹配）。"""
    candidate = (target or "").strip().strip(_PUNCT)
    point = (name or "").strip()
    if not candidate or not point:
        return 0.0
    best = _score_pair(candidate, point, intro)
    cn = _normalize(candidate)
    pn = _normalize(point)
    if cn != candidate or pn != point:
        best = max(best, _score_pair(cn, pn, intro))
    return best


def _score_pair(candidate: str, point: str, intro: str) -> float:
    """对一对候选/点位做原始文本匹配（不含归一化）。"""
    cl, pl = candidate.lower(), point.lower()
    if cl == pl:
        return 1.0
    if cl in pl:
        return 0.9
    stripped = _strip_prefix(candidate)
    if stripped != candidate:
        sl = stripped.lower()
        if sl == pl:
            return 0.85
        if sl in pl:
            return 0.8
        if pl in sl:
            return 0.6
    if pl in cl:
        return 0.5
    if cl in (intro or "").lower():
        return 0.4
    p = _pinyin_score(candidate, point)
    if p > 0:
        return p
    if stripped != candidate:
        return _pinyin_score(stripped, point)
    return 0.0


def _score_match_best(target: str, name: str, intro: str) -> float:
    """取同义词展开后的最高分（对应 C# ScoreMatchBest）。"""
    return max((_score_match(v, name, intro) for v in _expand_synonyms(target)), default=0.0)


def _proximity_bonus(point: dict[str, Any], current_pos: str | None) -> float:
    """同楼层点位给微小加分，用于就近打平（不改变强匹配的胜负）。"""
    if not current_pos:
        return 0.0
    cur = _floor_of(current_pos)
    if cur is None:
        return 0.0
    return 0.01 if _floor_of(point.get("name") or "") == cur else 0.0


def _match_best(target: str, points: list[dict[str, Any]], current_pos: str | None = None):
    """对应 C# Where(s>0).OrderByDescending(s).FirstOrDefault()：唯一最佳匹配，无歧义。

    current_pos 传入时，同楼层候选加 0.01 就近分，仅用于分数相同/极接近时的打平。
    """
    best = None
    for p in points:
        s = _score_match_best(target, p["name"], p["intro"])
        if s > 0:
            s += _proximity_bonus(p, current_pos)
        if s > 0 and (best is None or s > best[0]):
            best = (s, p)
    return best


def _relative_nav_direction(q: str) -> str | None:
    """识别相对导航意图：'entrance'（回入口/大厅）/ 'up' / 'down'；否则 None。"""
    nq = _normalize(q)
    if re.search(r"上\d+层", nq):
        return "up"
    if re.search(r"下\d+层", nq):
        return "down"
    # 入口/大厅需带移动意图，避免把「入口在哪」这类提问误判成指令。
    if ("入口" in nq or "大厅" in nq) and nq.startswith(
        ("回", "去", "前往", "进入", "返回", "回到", "带我去", "带我")
    ):
        return "entrance"
    return None


def _relative_match(target: str, current_pos: str | None, points: list[dict[str, Any]]):
    """基于当前位置的相对导航：回入口/大厅、去上一层/下一层。命中返回 (score, point)，否则 None。"""
    if not current_pos:
        return None
    direction = _relative_nav_direction(target)
    if direction is None:
        return None
    if direction == "entrance":
        cur_floor = _floor_of(current_pos)
        fallback = None
        for p in points:
            name = _normalize(p.get("name") or "")
            if "入口" not in name and "大厅" not in name:
                continue
            if cur_floor is not None and _floor_of(p.get("name") or "") == cur_floor:
                return (0.95, p)  # 同层入口优先
            if fallback is None:
                fallback = p
        return (0.95, fallback) if fallback is not None else None
    cur_floor = _floor_of(current_pos)
    if cur_floor is None:
        return None
    target_floor = cur_floor + (1 if direction == "up" else -1)
    if target_floor < 1:
        return None
    for p in points:
        if _floor_of(p.get("name") or "") == target_floor:
            return (0.95, p)
    return None


# ==================== 输出契约（保留 25 字段） ====================

def _j(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _to_text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _split_actions(query: str) -> list[str]:
    """把用户原话拆成若干动作短语（source_text）。无连词时返回整句。"""
    q = (query or "").strip()
    if not q:
        return []
    parts = [p.strip() for p in _SPLIT_RE.split(q) if p.strip()]
    return parts or [q]


def _build_command(order: int, source_text_: str, best) -> dict[str, Any]:
    """把一个动作归一化为一条 command（按 AI_WebApi 无歧义，仅 matched / unmatched）。"""
    if best is None:
        return {
            "order": order,
            "source_text": source_text_,
            "status": "unmatched",
            "result": "",
            "intent_type": "",
            "match_name": "",
            "score": None,
            "message": "未匹配到当前空间指令",
            "candidates": [],
        }
    score, p = best
    return {
        "order": order,
        "source_text": source_text_,
        "status": "matched",
        "result": p["prefix"] + p["id"],
        "intent_type": PREFIX_TO_TYPE.get(p["prefix"], "unknown"),
        "match_name": p["name"],
        "score": score,
        "message": "",
        "candidates": [],
    }


def _patient_message(matched_count: int, ambiguous_count: int, unmatched_count: int) -> str:
    if matched_count > 0:
        if ambiguous_count > 0 or unmatched_count > 0:
            return "已执行可执行的操作，其余需要确认。"
        return "好的。"
    if ambiguous_count > 0:
        return "请再明确一下您要操作的位置。"
    if unmatched_count > 0:
        return "没有找到对应操作。"
    return "没有可执行的操作。"


def _empty_contract(error: str = "", status_code: str = "") -> dict[str, Any]:
    return {
        "success": "false" if error else "true",
        "status": "failed" if error else "empty",
        "needs_clarification": "false",
        "clarification_question": "",
        "result": _j({"space_id": "", "version": "", "commands": []}),
        "image_url": "",
        "artifact_ref": "",
        "patient_message": "场景指令服务暂时不可用。" if error else "未解析到可执行的场景操作。",
        "status_code": str(status_code or ""),
        "error": str(error or "")[:500],
        "command": "",
        "tool_kind": TOOL_KIND,
        "contract_version": CONTRACT_VERSION,
        "query_context": "",
        "has_instruction": "false",
        "match_name": "",
        "intent_type": "unknown",
        "doctor_count": "",
        "doctor_names": "",
        "commands_json": "[]",
        "matched_count": "0",
        "ambiguous_count": "0",
        "unmatched_count": "0",
        "space_id": "",
        "version": "",
    }


# ==================== 主入口（异步） ====================

async def resolve(
    user_query: str,
    space_id: str,
    client: httpx.AsyncClient,
    current_pos: str | None = None,
) -> dict[str, Any]:
    """场景指令直连解析：返回 504-scene-v2 的 25 字段契约（与原 main() 一致）。

    current_pos 为前端传入的当前位置（pos），用于就近打平与相对导航（回入口/上下一层）。
    """
    query = _to_text(user_query)
    if not query:
        return _empty_contract("用户指令为空")

    sid = _to_text(space_id) or DEFAULT_SPACE_ID

    try:
        version = await _resolve_version(client, sid)
        points = await _query_points(client, version)
    except httpx.TimeoutException:
        return _empty_contract("timeout")
    except httpx.HTTPStatusError as exc:
        return _empty_contract("HTTP " + str(exc.response.status_code))
    except httpx.RequestError:
        return _empty_contract("connection_error")
    except Exception as exc:  # 版本接口返回无效等
        return _empty_contract(str(exc))

    commands = []
    for i, action in enumerate(_split_actions(query), 1):
        best = _match_best(action, points, current_pos)
        if best is None:
            best = _relative_match(action, current_pos, points)
        commands.append(_build_command(i, action, best))

    matched = [c for c in commands if c["status"] == "matched"]
    ambiguous = [c for c in commands if c["status"] == "ambiguous"]  # 按 AI_WebApi 恒为空
    unmatched = [c for c in commands if c["status"] == "unmatched"]
    matched_count = len(matched)
    ambiguous_count = len(ambiguous)
    unmatched_count = len(unmatched)
    total_count = len(commands)

    if total_count == 0:
        top_status = "empty"
    elif matched_count == total_count:
        top_status = "available"
    elif matched_count > 0:
        top_status = "partial"
    elif ambiguous_count > 0:
        top_status = "needs_clarification"
    else:
        top_status = "empty"

    version_text = _to_text(version)
    legacy_command = "".join(c["result"] for c in matched)
    first_matched = matched[0] if matched else {}

    result_obj = {
        "space_id": sid,
        "version": version,
        "current_position": _to_text(current_pos),
        "commands": commands,
        "matched_count": matched_count,
        "ambiguous_count": ambiguous_count,
        "unmatched_count": unmatched_count,
    }

    return {
        "success": "true",
        "status": top_status,
        "needs_clarification": "true" if ambiguous_count > 0 else "false",
        "clarification_question": "",
        "result": _j(result_obj),
        "image_url": "",
        "artifact_ref": "",
        "patient_message": _patient_message(matched_count, ambiguous_count, unmatched_count),
        "status_code": "",
        "error": "",
        "command": legacy_command,
        "tool_kind": TOOL_KIND,
        "contract_version": CONTRACT_VERSION,
        "query_context": "",
        "has_instruction": "true" if matched_count > 0 else "false",
        "match_name": _to_text(first_matched.get("match_name")),
        "intent_type": _to_text(first_matched.get("intent_type")) or "unknown",
        "doctor_count": "",
        "doctor_names": "",
        "commands_json": _j(commands),
        "matched_count": str(matched_count),
        "ambiguous_count": str(ambiguous_count),
        "unmatched_count": str(unmatched_count),
        "space_id": sid,
        "version": version_text,
        "current_position": _to_text(current_pos),
    }


def _parse_commands(body: dict[str, Any]) -> list[dict[str, Any]]:
    raw = body.get("commands_json")
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
            return parsed if isinstance(parsed, list) else []
        except Exception:
            return []
    if isinstance(raw, list):
        return raw
    try:
        obj = json.loads(body.get("result") or "{}")
        return obj.get("commands") or []
    except Exception:
        return []


_STATUS = {
    "available": "succeeded",
    "partial": "partial",
    "needs_clarification": "clarification",
    "empty": "unavailable",
    "failed": "failed",
}


async def _latest_prescribed_scale(ctx: RunContext) -> str | None:
    """查患者最近一次问诊的医嘱里的量表名。

    链：GetOnlineConsultationRecords(Phone=requesterphone) → 取 CreateTime 距今天
    最近的 Id → GetSingleDoctorOrderInfo(Id) → 取 data.PrescriptionInfo[] 首个非空 ScaleName。
    """
    raw = ctx.record.raw_inputs or {}
    phone = _to_text(raw.get("requesterphone") or raw.get("phone"))
    if not phone:
        return None
    client = ctx.backend.client
    try:
        resp = await client.get(RECORDS_URL, params={"Phone": phone}, timeout=TIMEOUT)
        resp.raise_for_status()
        records_doc = resp.json()
    except Exception:
        return None
    records = records_doc.get("data") if isinstance(records_doc, dict) else None
    if not isinstance(records, list) or not records:
        return None
    today = datetime.now().date()
    closest_id = None
    min_diff = None
    for rec in records:
        if not isinstance(rec, dict):
            continue
        ts = rec.get("CreateTime")
        if not ts:
            continue
        try:
            d = datetime.strptime(_to_text(ts), "%Y-%m-%d %H:%M:%S").date()
        except Exception:
            continue
        diff = abs((d - today).days)
        if min_diff is None or diff < min_diff:
            min_diff = diff
            closest_id = rec.get("Id")
    if closest_id is None:
        return None
    try:
        resp = await client.get(ORDER_URL, params={"Id": closest_id}, timeout=TIMEOUT)
        resp.raise_for_status()
        order_doc = resp.json()
    except Exception:
        return None
    data = order_doc.get("data") if isinstance(order_doc, dict) else None
    prescriptions = data.get("PrescriptionInfo") if isinstance(data, dict) else None
    if not isinstance(prescriptions, list):
        return None
    for item in prescriptions:
        if isinstance(item, dict) and item.get("ScaleName"):
            return _to_text(item.get("ScaleName"))
    return None


def _fallback_contract(msg: str) -> dict[str, Any]:
    """量表/训练分流无结果时，返回 command 为空、patient_message=msg 的契约。"""
    body = _empty_contract()
    body["patient_message"] = msg
    body["status"] = "empty"
    return body


async def _resolve_scale_instruction(ctx: RunContext, result: str, query: str) -> dict[str, Any]:
    """量表/训练意图 → 评估室 → 场景点（复用现有后端 resolve 做房间→点位匹配）。"""
    if result == "1":
        # 具体量表/训练名：直接映射评估室；训练名不在映射表时退回原话做点位匹配。
        room = scale_to_room(query)
        target = query if room == "未知评估室" else room
    else:  # result == "0"
        scale_name = await _latest_prescribed_scale(ctx)
        if not scale_name:
            return _fallback_contract(NO_TRAINING_TASK_MSG)
        room = scale_to_room(scale_name)
        target = scale_name if room == "未知评估室" else room

    space_id = _to_text(ctx.scope.space_id)
    body = await resolve(target, space_id, ctx.backend.client)
    if not body.get("command"):
        return _fallback_contract(NO_TRAINING_TASK_MSG)
    return body


def _extract_token(doc: Any) -> str:
    """从登录响应里提取 token（兼容顶层 / data 层 / body.data 层）。"""
    if isinstance(doc, str):
        try:
            doc = json.loads(doc)
        except Exception:
            return ""
    if not isinstance(doc, dict):
        return ""
    if doc.get("token"):
        return str(doc["token"])
    data = doc.get("data")
    if isinstance(data, dict):
        if data.get("token"):
            return str(data["token"])
        inner = data.get("data")
        if isinstance(inner, dict) and inner.get("token"):
            return str(inner["token"])
    body = doc.get("body")
    if isinstance(body, str):
        try:
            body = json.loads(body)
        except Exception:
            body = None
    if isinstance(body, dict):
        if body.get("token"):
            return str(body["token"])
        bdata = body.get("data")
        if isinstance(bdata, dict):
            if bdata.get("token"):
                return str(bdata["token"])
            binner = bdata.get("data")
            if isinstance(binner, dict) and binner.get("token"):
                return str(binner["token"])
    return ""


def _web_contract(url: str, msg: str) -> dict[str, Any]:
    """拼 [web] 链接 + 提示语的场景指令契约。"""
    body = _empty_contract()
    body["command"] = "[web]" + url
    body["patient_message"] = msg
    body["status"] = "available"
    body["has_instruction"] = "true"
    return body


async def _login_token(ctx: RunContext) -> str:
    """固定服务账号登录（Phone 固定 LOGIN_PHONE），取 JWT token。"""
    client = ctx.backend.client
    try:
        resp = await client.post(
            LOGIN_URL,
            json={"Phone": LOGIN_PHONE, "Password": LOGIN_PASSWORD},
            timeout=TIMEOUT,
        )
        resp.raise_for_status()
        return _extract_token(resp.json())
    except Exception:
        return ""


async def _user_id_tenant(ctx: RunContext, token: str, phone: str) -> tuple[str, str]:
    client = ctx.backend.client
    try:
        resp = await client.get(
            GET_USER_ROLE_URL,
            params={"Phone": phone},
            headers={"Authorization": f"Bearer {token}"},
            timeout=TIMEOUT,
        )
        resp.raise_for_status()
        doc = resp.json()
    except Exception:
        return "", ""
    data = doc.get("data") if isinstance(doc, dict) else None
    if not isinstance(data, dict):
        return "", ""
    return str(data.get("UserId", "")), str(data.get("TenantId", ""))


async def _query_reports(
    ctx: RunContext, token: str, tenant_id: str, user_id: str, start: str, end: str, phone: str
) -> dict[str, Any]:
    client = ctx.backend.client
    payload = {
        "TenantId": tenant_id, "Page": 1, "PageSize": 100, "OrderType": 1,
        "UserId": user_id, "Keyword": "", "StartTime": start, "EndTime": end,
    }
    try:
        resp = await client.post(
            GET_ALL_PLANS_URL,
            json=payload,
            headers={"Authorization": f"Bearer {token}"},
            timeout=TIMEOUT,
        )
        resp.raise_for_status()
        doc = resp.json()
    except Exception:
        return _fallback_contract(NO_TRAINING_TASK_MSG)
    items = doc.get("data", {}).get("data", []) if isinstance(doc, dict) else []
    if not isinstance(items, list):
        items = []
    valid = [it for it in items if isinstance(it, dict) and it.get("Id") is not None]
    count = len(valid)
    if count == 0:
        return _fallback_contract("该时间段无报告，请联系管理员！")
    if count == 1:
        report_id = str(valid[0].get("Id", ""))
        url = f"{RECIPE_BASE}/12/{phone}/{quote(report_id, safe='=&')}/{token}"
        return _web_contract(url, "当前日期只有一个训练报告，已为您打开报告详情")
    start_s = start.replace("/", "")
    end_s = end.replace("/", "")
    url = f"{RECIPE_BASE}/9/{phone}/{start_s}_{end_s}/{token}"
    return _web_contract(url, "已打开当前日期训练报告列表")


async def _query_consultation_records(
    ctx: RunContext, token: str, tenant_id: str, user_id: str, time_val: str, phone: str
) -> dict[str, Any]:
    client = ctx.backend.client
    payload = {
        "TenantId": tenant_id, "Page": 1, "PageSize": 100, "Type": 1,
        "UserId": user_id, "Keyword": "", "TimeRange": time_val,
    }
    try:
        resp = await client.post(
            GET_CONSULT_RECORDS_URL,
            json=payload,
            headers={"Authorization": f"Bearer {token}"},
            timeout=TIMEOUT,
        )
        resp.raise_for_status()
        doc = resp.json()
    except Exception:
        return _fallback_contract(NO_TRAINING_TASK_MSG)
    doctor_list = doc.get("data", {}).get("DoctorList") if isinstance(doc, dict) else None
    if not isinstance(doctor_list, dict):
        return _fallback_contract("该时间段无问诊记录，请联系管理员！")
    items = doctor_list.get("data", [])
    if not isinstance(items, list):
        items = []
    count = len(items)
    if count == 0:
        return _fallback_contract("该时间段无问诊记录，请联系管理员！")
    if count == 1:
        record_id = str(items[0].get("Id", ""))
        url = f"{RECIPE_BASE}/11/{phone}/{record_id}/{token}"
        return _web_contract(url, "当前日期只有一个问诊记录，已为您打开记录详情")
    url = f"{RECIPE_BASE}/8/{phone}/{time_val}/{token}"
    return _web_contract(url, "已打开当前日期问诊记录列表")


async def _resolve_record_query(ctx: RunContext, query: str) -> dict[str, Any]:
    """result 2：查报告 / 病历 / 问诊记录（带时间筛选），产出 [web] 链接 + 提示语。"""
    raw = ctx.record.raw_inputs or {}
    phone = _to_text(raw.get("requesterphone") or raw.get("phone"))
    if not phone:
        return _fallback_contract(NO_TRAINING_TASK_MSG)
    token = await _login_token(ctx)
    if not token:
        return _fallback_contract(NO_TRAINING_TASK_MSG)
    user_id, tenant_id = await _user_id_tenant(ctx, token, phone)
    start, end = extract_time_range(query)
    time_val = (start.replace("/", "") + "_" + end.replace("/", "")) if start else ""

    if "报告" in query:
        return await _query_reports(ctx, token, tenant_id, user_id, start, end, phone)
    if "病" in query:
        url = f"{RECIPE_BASE}/13/{phone}/0/{token}"
        return _web_contract(url, "已打开修改病历页面，请进行修改！")
    return await _query_consultation_records(ctx, token, tenant_id, user_id, time_val, phone)


class SceneInstructionAdapter:
    """把指令解析结果落证据库，并投影为 TaskResult 供 agent / 后处理层消费。"""

    async def execute(self, task: TaskSpec, args: dict[str, Any], ctx: RunContext) -> TaskResult:
        # user_query 来自输入 query；space_id 取已解析的作用域（兼容 space_id/spaceId/spaceid）。
        query = _to_text(ctx.query)
        if not query:
            raise DomainError("scene_request_mismatch", "请提供要解析的指令内容。", outcome="clarification")
        space_id = _to_text(ctx.scope.space_id)
        # 指令分流：量表/训练意图（0/1）走「量表→评估室→场景点」；
        # 带时间/报告/病历（2）走「登录→查报告/病历/问诊记录」；其余走现有场景匹配。
        result = classify_instruction(query)
        if result == "2":
            body = await _resolve_record_query(ctx, query)
        elif result in ("0", "1"):
            body = await _resolve_scale_instruction(ctx, result, query)
        else:
            body = await resolve(query, space_id, ctx.backend.client, current_pos=ctx.scope.position)
            # 裸确认/指代指令（如上一轮说「打开摄像头，需要我现在帮你打开吗」后用户回「需要打开」）：
            # 本身不含目标、直接解析不到，从上一轮对话找回目标再解析一次。
            if not body.get("command") and _bare_followup(query):
                recovered = _recover_followup_command(query, ctx.memory.turns)
                if recovered:
                    body = await resolve(
                        recovered, space_id, ctx.backend.client, current_pos=ctx.scope.position
                    )
        evidence = await ctx.repository.save_evidence(
            ctx.scope.scope_hash,
            "resolve_scene_instruction",
            ctx.record.request_id,
            body,
            body.get("contract_version") or CONTRACT_VERSION,
        )
        status = _STATUS.get(body.get("status"), "unavailable")
        if body.get("success") == "false":
            status = "failed"
        message = body.get("patient_message") or ""
        return TaskResult(
            task_id=task.task_id,
            status=status,
            code="tool_" + status if status in {"failed", "unavailable"} else "",
            message=message,
            evidence_ids=[evidence.evidence_id],
            outputs={
                "tool_name": "resolve_scene_instruction",
                "commands": _parse_commands(body),
                "command": body.get("command") or "",
                "matched_count": int(body.get("matched_count") or 0),
                "ambiguous_count": int(body.get("ambiguous_count") or 0),
                "unmatched_count": int(body.get("unmatched_count") or 0),
                "intent_type": body.get("intent_type"),
                "match_name": body.get("match_name"),
                "space_id": body.get("space_id"),
                "version": body.get("version"),
                "contract_version": body.get("contract_version"),
            },
        )
