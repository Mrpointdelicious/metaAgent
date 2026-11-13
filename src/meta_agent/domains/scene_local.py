"""
创建日期：2026-11-13
文件功能：场景指令本地解析器（直连 webyun 点位库，不经过 AI_WebApi）。

与 metaAgent-feature-runtime-cutover 的 scene_instruction.py 使用同一套匹配内核
（前缀剥离 / 同义词 / 中文数字归一 / 打分 / 最低分门槛 / 拆句），但输出改为本分支
既有的 commands-array-v1 封套，因此 SceneCommandResolver → SceneAdapter → dispatch
的 evidence / action_ref / 版本校验链完全不用改：

    {
      "status": 200,
      "data": {
        "spaceId": "<当前空间>",
        "version": 138,
        "commands": [
          {"order": 1, "sourceText": "带我回四楼", "status": "matched",
           "result": "[ScenePoint:]23", "candidates": [], ...}
        ]
      }
    }

契约要点：未匹配 / 歧义的 result 必须是 None（SceneAdapter.resolve_response 会拒绝
非 matched 携带任何编码），且 commands 的 sourceText 必须能按原话顺序完整对齐
（SceneIntentParser.spans 会逐段校验）。
"""

import logging
import re
from typing import Any

import httpx

from meta_agent.config import Settings

LOGGER = logging.getLogger(__name__)

# 直连 webyun 点位库（与 feature-runtime-cutover 保持一致；可由 settings 覆盖）。
DEFAULT_VERSION_URL = "http://192.168.8.160/back/version"
DEFAULT_QUERY_URL = "http://192.168.8.160/back/query"
DEFAULT_SPACE_ID = "67ecd511-5532-405d-9b70-1cd1c0532eed"
# 上游 apikey / apitoken 只从配置读取（META_AGENT__SCENE_BACKEND_API_KEY / API_TOKEN），
# 不在代码里保留任何默认凭据。
REQUEST_TIMEOUT = 15

# 4 类点位的 SQL 模板（{0} = version_id）。
SQL_TEMPLATES = [
    (
        "SELECT jsonb_agg(jsonb_build_object('id', elem.id, 'name', elem.elem->>'Name', "
        "'intro', elem.elem->>'PointExplain')) AS result FROM meta.meta_space_data, "
        "LATERAL (SELECT e.*, row_number() OVER () - 1 AS id FROM "
        "jsonb_array_elements(data_content->'TeleportConfig'->'ChannelsConfig') "
        "WITH ORDINALITY e(elem)) AS elem WHERE version_id = {0};"
    ),
    (
        "SELECT jsonb_agg(jsonb_build_object('id', elem.id, 'name', elem.elem->>'Name', "
        "'intro', elem.elem->>'PointExplain')) AS result FROM meta.meta_space_data, "
        "LATERAL (SELECT e.*, row_number() OVER () - 1 AS id FROM "
        "jsonb_array_elements(data_content->'ScenePointCofig'->'Config') "
        "WITH ORDINALITY e(elem)) AS elem WHERE version_id = {0};"
    ),
    (
        "SELECT jsonb_agg(jsonb_build_object('id', elem.id, 'name', elem.elem->>'des')) "
        "AS result FROM meta.meta_space_data, "
        "LATERAL (SELECT e.*, row_number() OVER () - 1 AS id FROM "
        "jsonb_array_elements(data_content->'CommandList') WITH ORDINALITY e(elem)) "
        "AS elem WHERE version_id = {0};"
    ),
    (
        "SELECT jsonb_agg(jsonb_build_object('id', elem.id, 'name', elem.elem->>'des')) "
        "AS result FROM meta.meta_space_data, "
        "LATERAL (SELECT e.*, row_number() OVER () - 1 AS id FROM "
        "jsonb_array_elements(data_content->'FastTaskConfig'->'GameRoomConfig') "
        "WITH ORDINALITY e(elem)) AS elem WHERE version_id = {0};"
    ),
]
RESULT_PREFIXES = ["[Telepor:]", "[ScenePoint:]", "[ClickPoint:]", "[GamePoint:]"]
PREFIX_TO_TYPE = {
    "[Telepor:]": "telepor",
    "[ScenePoint:]": "scene_point",
    "[ClickPoint:]": "click_point",
    "[GamePoint:]": "game_point",
}

# 动作/礼貌前缀。按长度倒序取最长匹配，避免「请带我去」被「请」截短。
# 不剥离「取消」「确认」：它们是反义语义标记（取消挂号 / 确认挂号）。
PREFIX_WORDS = [
    "请帮我打开", "请带我去", "请带我", "麻烦你帮我", "麻烦帮我", "请帮我",
    "帮我打开", "我想要去", "我需要去", "我想要回", "我需要回",
    "我想去", "我要去", "我想回", "我要回", "我想要", "我需要",
    "带我回", "带我到", "带我去", "带我过去", "传送到",
    "要去", "想去", "要回", "想回",
    "打开", "关闭", "切换", "前往", "进入", "回到", "返回", "退回", "后退",
    "传送", "来一局", "开始玩", "带我", "帮我", "麻烦", "请",
    "去", "到", "回", "上", "玩",
]
_PREFIX_WORDS_BY_LEN = sorted(PREFIX_WORDS, key=len, reverse=True)

# 句尾口语尾巴：「我想去四楼看看」→「四楼」。
_TARGET_TAIL_WORDS = (
    "看看", "瞧瞧", "转转", "逛逛", "走走", "一下",
    "那儿", "那里", "那边", "这边", "吧", "呗", "啊",
)

SYNONYMS = [
    ("点开", "打开"), ("开启", "打开"),
    ("关掉", "关闭"), ("关上", "关闭"), ("退出", "关闭"),
    ("退出", "结束"), ("终止", "结束"), ("停止", "结束"),
    ("转换", "切换"), ("换", "切换"),
    ("返回", "退回"), ("后退", "退回"), ("进去", "进入"),
]
ACTION_ALIASES = [
    ("取消挂号", "挂号返回"),
    ("取消叫号", "拒绝叫号"),
    ("挂号确认", "确认挂号"),
    ("叫号确认", "确认叫号"),
    ("挂号取消", "挂号返回"),
    ("叫号取消", "拒绝叫号"),
    ("放大屏幕", "屏幕放大"),
    ("视角切换", "切换视角"),
    # 训练报告/就诊记录 与 问诊记录 同页。
    ("打开训练报告", "问诊记录"),
    ("打开就诊记录", "问诊记录"),
]

# 拆动作连词/标点。被丢掉的连接词必须在 SceneIntentParser.connector 白名单里，
# 否则 spans() 会因「指令遗漏了原话内容」抛 command_alignment。
_SPLIT_RE = re.compile(r"[，,、；;]|并且|然后|接着|同时|顺便|以及|还有|并")

# 拼音兜底是可选能力：生产镜像未装 pypinyin 时整条分支恒返回 0，
# 文本规则自身已覆盖常用口语，有无该包结果必须一致。
try:
    from pypinyin import lazy_pinyin as _pypinyin

    def _to_pinyin(s: str) -> str:
        try:
            return "".join(_pypinyin(s or "")).lower()
        except Exception:
            return ""
except Exception:
    def _to_pinyin(s: str) -> str:
        return ""


# ==================== 上游访问 ====================

async def _post(client: httpx.AsyncClient, url: str, payload: dict[str, Any], settings: Settings):
    key = settings.scene_backend_api_key
    token = settings.scene_backend_api_token.get_secret_value()
    if not key or not token:
        LOGGER.warning(
            "scene backend credentials missing: set "
            "META_AGENT__SCENE_BACKEND_API_KEY / META_AGENT__SCENE_BACKEND_API_TOKEN"
        )
    response = await client.post(
        url,
        json=payload,
        headers={
            "apikey": key,
            "apitoken": token,
            "Content-Type": "application/json",
        },
        timeout=settings.scene_backend_timeout_seconds,
    )
    response.raise_for_status()
    return response.json()


async def fetch_points(
    client: httpx.AsyncClient, settings: Settings, space_id: str
) -> tuple[int, list[dict[str, Any]]]:
    """取当前空间版本与 4 类点位；单类查询失败不阻塞整体。"""
    doc = await _post(client, settings.scene_backend_version_url, {"spaceId": space_id}, settings)
    raw_version = doc.get("data") if isinstance(doc, dict) else None
    if raw_version is None:
        raise RuntimeError("场景版本接口未返回有效 version")
    version = int(raw_version)
    points: list[dict[str, Any]] = []
    for sql, prefix in zip(SQL_TEMPLATES, RESULT_PREFIXES, strict=True):
        try:
            doc = await _post(
                client,
                settings.scene_backend_query_url,
                {"sql": sql.format(version)},
                settings,
            )
            data = (doc or {}).get("data") if isinstance(doc, dict) else None
            arr = data.get("result") if isinstance(data, dict) else None
            if not isinstance(arr, list):
                continue
            for item in arr:
                if not isinstance(item, dict):
                    continue
                name = str(item.get("name") or "").strip()
                if not name:
                    continue
                pid = item.get("id")
                points.append(
                    {
                        "prefix": prefix,
                        "id": str(int(pid)) if isinstance(pid, (int, float)) else str(pid or ""),
                        "name": name,
                        "intro": str(item.get("intro") or "").strip(),
                    }
                )
        except Exception:
            continue
    return version, points


# ==================== 匹配打分 ====================

def _strip_prefix(s: str) -> str:
    """剥掉最长命中的动作/礼貌前缀，再去掉句尾口语尾巴。"""
    for word in _PREFIX_WORDS_BY_LEN:
        if s.startswith(word) and len(s) > len(word):
            s = s[len(word):]
            break
    for tail in _TARGET_TAIL_WORDS:
        if s.endswith(tail) and len(s) > len(tail):
            s = s[: -len(tail)]
            break
    return s


# 目录里的点位名是「退回上个场景」；「回上一个场景」「返回上一场景」
# 「后退一个场景」等变体靠前缀剥离无法精确命中，直接给出规范名。
_SCENE_BACK_RE = re.compile(
    r"^\s*(?:请|麻烦|帮我|我想|我要|想要|要)?\s*"
    r"(?:退回|返回|回到|后退|回|去)?\s*"
    r"(?:上一个|上一|一个|上个)(?:场景|页面|画面)\s*$"
)


def _expand_synonyms(target: str):
    yield target
    for frm, to in SYNONYMS:
        if target.startswith(frm) and len(target) > len(frm):
            yield to + target[len(frm):]
    for frm, to in ACTION_ALIASES:
        if frm in target:
            yield target.replace(frm, to)
    if _SCENE_BACK_RE.match(target or ""):
        yield "退回上个场景"
        yield "上个场景"


_CN_DIGITS = {
    "零": 0, "一": 1, "二": 2, "两": 2, "三": 3,
    "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9,
}


def _cn_to_arabic(text: str) -> str:
    def _conv(s: str) -> str:
        section, number = 0, 0
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


_SYNONYM_NORM = [("房间", "室"), ("房", "室"), ("手动", "人工")]


def _normalize(text: str) -> str:
    s = _cn_to_arabic(text or "")
    for frm, to in _SYNONYM_NORM:
        s = s.replace(frm, to)
    return s


_FLOOR_RE = re.compile(r"([0-9]+)\s*楼")


def _floor_of(text: str) -> int | None:
    m = _FLOOR_RE.search(_normalize(text or ""))
    return int(m.group(1)) if m else None


def _normalize_pinyin(s: str) -> str:
    """保留小写 a-z 与数字。必须保留数字：否则所有 N楼 的拼音都退化成 lou。"""
    return "".join(ch for ch in (s or "").lower() if "a" <= ch <= "z" or "0" <= ch <= "9")


def _pinyin_score(left: str, right: str) -> float:
    pa = _normalize_pinyin(_to_pinyin(left))
    pb = _normalize_pinyin(_to_pinyin(right))
    if not pa or not pb:
        return 0.0
    if pa == pb:
        return 0.7
    if len(pa) >= 4 and pa in pb:
        return 0.65
    if len(pb) >= 4 and pb in pa:
        return 0.65
    return 0.0


_PUNCT = "。！？!?…．."


def _score_match(target: str, name: str, intro: str) -> float:
    candidate = (target or "").strip().strip(_PUNCT)
    point = (name or "").strip()
    if not candidate or not point:
        return 0.0
    best = _score_pair(candidate, point, intro)
    cn, pn = _normalize(candidate), _normalize(point)
    if cn != candidate or pn != point:
        best = max(best, _score_pair(cn, pn, intro))
    return best


def _score_pair(candidate: str, point: str, intro: str) -> float:
    cl, pl = candidate.lower(), point.lower()
    if cl == pl:
        return 1.0
    # 单字候选不能靠「是点位名的子串」拿 0.9（「回」会命中「退回上个场景」）。
    if len(cl) >= 2 and cl in pl:
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
    return max((_score_match(v, name, intro) for v in _expand_synonyms(target)), default=0.0)


def _proximity_bonus(point: dict[str, Any], current_pos: str | None) -> float:
    if not current_pos:
        return 0.0
    cur = _floor_of(current_pos)
    return 0.01 if cur is not None and _floor_of(point.get("name") or "") == cur else 0.0


# 最低匹配分。原实现 score>0 即命中，0.4/0.5/0.6/0.65 这些弱匹配会把问句
# 误当成可执行指令；低于此分一律拒绝。
MIN_MATCH_SCORE = 0.75


def _match_best(target: str, points: list[dict[str, Any]], current_pos: str | None = None):
    best = None
    for p in points:
        raw = _score_match_best(target, p["name"], p["intro"])
        if raw < MIN_MATCH_SCORE:
            continue
        score = raw + _proximity_bonus(p, current_pos)
        if best is None or score > best[0]:
            best = (score, p)
    return best


def _relative_nav_direction(q: str) -> str | None:
    nq = _normalize(q)
    if re.search(r"上\d+层", nq):
        return "up"
    if re.search(r"下\d+层", nq):
        return "down"
    if ("入口" in nq or "大厅" in nq) and nq.startswith(
        ("回", "去", "前往", "进入", "返回", "回到", "带我去", "带我")
    ):
        return "entrance"
    return None


def _relative_match(target: str, current_pos: str | None, points: list[dict[str, Any]]):
    """基于当前位置的相对导航：回入口/大厅、上/下一层。"""
    if not current_pos:
        return None
    direction = _relative_nav_direction(target)
    if direction is None:
        return None
    if direction == "entrance":
        cur_floor, fallback = _floor_of(current_pos), None
        for p in points:
            name = _normalize(p.get("name") or "")
            if "入口" not in name and "大厅" not in name:
                continue
            if cur_floor is not None and _floor_of(p.get("name") or "") == cur_floor:
                return (0.95, p)
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


# ==================== 封套构造 ====================

def _split_actions(query: str) -> list[str]:
    q = (query or "").strip()
    if not q:
        return []
    return [p.strip() for p in _SPLIT_RE.split(q) if p.strip()] or [q]


def _build_command(order: int, source: str, best) -> dict[str, Any]:
    """构造一条 commands-array-v1 指令。

    非 matched 时 result 必须是 None：SceneAdapter.resolve_response 会拒绝
    「不明确的指令携带可执行编码」。
    """
    if best is None:
        return {
            "order": order,
            "sourceText": source,
            "status": "unmatched",
            "result": None,
            "message": "未匹配到当前空间指令",
            "candidates": [],
        }
    score, point = best
    return {
        "order": order,
        "sourceText": source,
        "status": "matched",
        "result": point["prefix"] + point["id"],
        "matchName": point["name"],
        "intentType": PREFIX_TO_TYPE.get(point["prefix"], "unknown"),
        "score": round(score, 4),
        "message": "",
        "candidates": [],
    }


def build_commands(
    query: str, points: list[dict[str, Any]], current_pos: str | None = None
) -> list[dict[str, Any]]:
    """纯函数：把一句（可能含多动作）原话解析成 commands 数组。"""
    commands = []
    for order, action in enumerate(_split_actions(query), 1):
        best = _match_best(action, points, current_pos)
        if best is None:
            best = _relative_match(action, current_pos, points)
        commands.append(_build_command(order, action, best))
    return commands


def envelope(space_id: str, version: int, commands: list[dict[str, Any]]) -> dict[str, Any]:
    """组装 AI_WebApi 同构封套，供 SceneAdapter / numeric_envelope 消费。"""
    return {"status": 200, "data": {"spaceId": space_id, "version": version, "commands": commands}}


async def resolve_local_scene(
    client: httpx.AsyncClient, settings: Settings, payload: dict[str, Any]
) -> dict[str, Any]:
    """navigate_scene 的本地实现：直连取点位 + 本地匹配，返回同构封套。"""
    from meta_agent.domains.instruction_classify import classify_instruction, scale_to_room

    space_id = str(payload.get("spaceId") or settings.scene_default_space_id)
    target = str(payload.get("target") or "").strip()
    current_pos = payload.get("pos") or payload.get("currentPos") or None
    version, points = await fetch_points(client, settings, space_id)
    if not target:
        return envelope(space_id, version, [])
    # 指令分流（对齐 Dify chatflow code 3' / feature-runtime-cutover）：
    # 具体量表名 → 映射到对应评估室点位（如 ADL → 四楼2号评估室）；
    # 带时间/修改病历的记录请求 → 不是场景跳转，返回空命令交回患者数据工具链。
    original = target
    intent = classify_instruction(target)
    if intent == "2":
        return envelope(space_id, version, [])
    if intent == "1":
        room = scale_to_room(target)
        if room != "未知评估室":
            target = room
    commands = build_commands(target, points, str(current_pos) if current_pos else None)
    if target != original:
        # 量表映射改写了目标，但 sourceText 必须回填用户原话：上层
        # SceneIntentParser.spans() 要求每条 sourceText 能在本轮原话里定位，
        # 否则 command_alignment 校验失败，指令无法交付。
        for command in commands:
            command["sourceText"] = original
    return envelope(space_id, version, commands)
