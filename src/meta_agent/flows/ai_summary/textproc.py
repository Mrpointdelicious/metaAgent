"""
创建日期：2026-11-16
文件功能：AI纪要总结的文本前处理，对齐 Dify 工作流里的两个代码节点。

节点一（时间归一）：把对话记录里的 timeStamp 时间戳、以及正文里
「起止时间：HH:MM:SS-HH:MM:SS」统一换算成北京时间（UTC+8）。
节点二（对话整合）：判断本轮输入有没有医疗语义，再与历史累积文本拼接，
按句清洗掉「嗯嗯/哈哈/谢谢」这类敷衍短语，并统计是否超过 6 句（用于分段）。

这里只做纯函数，不碰 IO，方便单测；取数在 flows/ai_summary/__init__.py 里。
"""

from __future__ import annotations

import json
import re
from datetime import date, datetime, timedelta, timezone
from typing import Any

BEIJING_TZ = timezone(timedelta(hours=8))
UTC_TZ = timezone(timedelta(hours=0))

# 超过这个句数说明一段对话该切分了（Dify 里 exceed_six 标记）。
SPLIT_SENTENCE_LIMIT = 6


# --------------------------------------------------------------------------- #
# 节点一：时间归一
# --------------------------------------------------------------------------- #


def convert_ts(ts: Any) -> Any:
    """把秒级/毫秒级时间戳转成北京时间字符串；无法解析时原样返回。"""
    try:
        ts_str = str(ts).strip()
        ts_int = int(ts_str)
    except Exception:
        return ts

    # 13 位及以上按毫秒处理
    if len(ts_str) >= 13:
        ts_int = ts_int / 1000

    try:
        return datetime.fromtimestamp(ts_int, tz=BEIJING_TZ).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return ts


def convert_text_time(time_str: str, base_date: date | None = None) -> str:
    """把 'HH:MM:SS-HH:MM:SS' 这样的大模型输出时间（按 UTC）换算成北京时间。"""
    match = re.match(r"(\d{2}:\d{2}:\d{2})-(\d{2}:\d{2}:\d{2})", time_str)
    if not match:
        return time_str  # 格式不匹配原样返回

    start_time, end_time = match.groups()
    if base_date is None:
        base_date = date.today()

    try:
        start_dt = datetime.combine(base_date, datetime.strptime(start_time, "%H:%M:%S").time())
        end_dt = datetime.combine(base_date, datetime.strptime(end_time, "%H:%M:%S").time())
        start_bj = start_dt.replace(tzinfo=UTC_TZ).astimezone(BEIJING_TZ)
        end_bj = end_dt.replace(tzinfo=UTC_TZ).astimezone(BEIJING_TZ)
        return f"{start_bj.strftime('%H:%M:%S')}-{end_bj.strftime('%H:%M:%S')}"
    except Exception:
        return time_str


_TIME_RANGE = re.compile(r"(起止时间：)(\d{2}:\d{2}:\d{2}-\d{2}:\d{2}:\d{2})")


def process_text_times(text: str) -> str:
    """把正文里的「起止时间：…」替换成北京时间。"""
    return _TIME_RANGE.sub(lambda m: m.group(1) + convert_text_time(m.group(2)), text)


def normalize_records(raw: Any) -> list[dict[str, Any]]:
    """把对话记录（JSON 文本 / dict / list）规整成记录列表并换算时间。

    兼容三种上游形态：
      1. 网关/工作流直接给的数组——元素可以是对象，也可以是对象的 JSON 字符串：
         [{"role":"Doctor","timeStamp":1794804254821,"from":"潘x","message":"…"}, …]
      2. 对话记录接口的返回：{"code":200,"message":"…","data":[{...}]}；
      3. data 里每项仍是 JSON 字符串。
    解析不了的项退化成 {"part": 原文}，不让整条链路失败。
    """
    payload = raw
    if isinstance(raw, str):
        try:
            payload = json.loads(raw)
        except ValueError:
            return []
    if isinstance(payload, dict):
        payload = payload.get("data", [])
    if not isinstance(payload, list):
        return []

    records: list[dict[str, Any]] = []
    for item in payload:
        obj: Any = item
        if isinstance(item, str):
            try:
                obj = json.loads(item)
            except ValueError:
                records.append({"part": item})
                continue
        if not isinstance(obj, dict):
            records.append({"part": str(obj)})
            continue
        obj = dict(obj)
        if "timeStamp" in obj:
            obj["timeStamp"] = convert_ts(obj["timeStamp"])
        if isinstance(obj.get("part"), str):
            obj["part"] = process_text_times(obj["part"])
        records.append(obj)
    return records


# --------------------------------------------------------------------------- #
# 记录 → 对话文本（支持 role/from/message 形态）
# --------------------------------------------------------------------------- #

_ROLE_LABELS = {"doctor": "医生", "patient": "患者", "医生": "医生", "患者": "患者"}

# 记录里可能承载正文、发言人、角色的字段，按优先级取第一个非空的。
_TEXT_KEYS = ("message", "part", "text", "content")
_SPEAKER_KEYS = ("from", "speaker", "name", "sender")


def record_text(record: dict[str, Any]) -> str:
    """取出这条记录的正文。"""
    for key in _TEXT_KEYS:
        value = record.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def record_is_dialogue(record: dict[str, Any]) -> bool:
    """是否是「谁说了什么」的结构化对话记录（而不是整段 part 文本）。"""
    if not record_text(record):
        return False
    return any(record.get(key) for key in ("role", *_SPEAKER_KEYS))


def record_speaker(record: dict[str, Any]) -> str:
    for key in _SPEAKER_KEYS:
        value = record.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return record_role(record) or "未知"


def record_role(record: dict[str, Any]) -> str:
    role = str(record.get("role") or "").strip()
    return _ROLE_LABELS.get(role.lower(), role)


def render_transcript(records: list[dict[str, Any]]) -> str:
    """把结构化记录渲染成对话文本，每行带上时间和角色，供 LLM 判断发言人与时间区间。

    例：[2026-11-16 12:44:14] 潘x（医生）：我膝盖疼了两周，走路多了更疼。
    """
    lines: list[str] = []
    for record in records:
        text = record_text(record)
        if not text:
            continue
        speaker = record_speaker(record)
        role = record_role(record)
        label = f"{speaker}（{role}）" if role and role != speaker else speaker
        stamp = str(record.get("timeStamp") or "").strip()
        prefix = f"[{stamp}] " if stamp else ""
        lines.append(f"{prefix}{label}：{text}")
    return "\n".join(lines)


def _parse_stamp(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text or text.isdigit():
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(text[:19], fmt)
        except ValueError:
            continue
    return None


def record_span(records: list[dict[str, Any]]) -> str:
    """这批记录的时间区间（HH:MM:SS-HH:MM:SS）；没有可用时间戳时返回空串。"""
    stamps = [
        parsed for parsed in (_parse_stamp(item.get("timeStamp")) for item in records) if parsed
    ]
    if not stamps:
        return ""
    return f"{min(stamps).strftime('%H:%M:%S')}-{max(stamps).strftime('%H:%M:%S')}"


# --------------------------------------------------------------------------- #
# 节点二：对话整合（语义判断 + 拼接清洗）
# --------------------------------------------------------------------------- #

_TRAILING_PATTERNS = (
    r"(啊+)$",
    r"(嗯+)$",
    r"(哈+)$",
    r"(呵+)$",
    r"(嘿+)$",
    r"(哦+)$",
    r"(诶+)$",
    r"(呃+)$",
    r"(唉+)$",
    r"(额+)$",
    r"(嗯哼+)$",
    r"(嘿嘿+)$",
    r"(呵呵+)$",
    r"(哈哈+)$",
    r"(拜拜+)$",
    r"(再见+)$",
    r"(nice+)$",
    r"(good+)$",
    r"(great+)$",
    r"(辛苦+了*)$",
    r"(谢谢+)$",
    r"(bye+)$",
    r"([~…．.。！!？?,，、]*)$",  # 尾部的标点
)


def remove_trailing_meaningless(text: str) -> str:
    """移除句尾无实际语义的敷衍短语（如啊啊啊、嗯嗯嗯、哈哈哈等）。"""
    seg = re.sub(r"(呃{3,})", "", text, flags=re.IGNORECASE)
    while seg:
        original = seg
        seg = seg.rstrip()
        for pattern in _TRAILING_PATTERNS:
            trimmed = re.sub(pattern, "", seg, flags=re.IGNORECASE)
            if len(trimmed) < len(seg):
                seg = trimmed
                break
        if seg == original:
            break
    return seg


# 医患场景下这些整句等于没有信息量
_MEANINGLESS_PATTERNS = (
    r"^嗯+$",
    r"^啊+$",
    r"^呵+.*$",
    r"^哈+$",
    r"^哦+$",
    r"^恩+$",
    r"^呃+$",
    r"^唉+$",
    r"^额+$",
    r"^[~…．.。！!？?,，、]*$",
    r"^好+$",
    r"^可以+$",
    r"^ok+$",
    r"^yes+$",
    r"^no+$",
    r"^没事+$",
    r"^好的+$",
    r"^知道+$",
    r"^了解$",
    r"^明白$",
    r"^收到$",
    r"^谢谢+$",
    r"^辛苦+了*$",
    r"^再见+$",
    r"^拜拜+$",
    r"^bye+$",
    r"^随便+$",
    r"^看你+$",
    r"^你说+$",
    r"^不知道+$",
    r"^不清楚+$",
    r"^无$",
    r"^无语$",
    r"^……+$",
    r"^嗯哼+$",
    r"^诶+$",
    r"^hi+$",
    r"^hello+$",
    r"^beautiful+$",
    r"^nice+$",
    r"^good+$",
    r"^great+$",
    r"^呵呵+$",
    r"^哈哈+$",
    r"^嘿嘿+$",
    r"^[0-9a-zA-Z]{1,8}$",
    r"^只是.*$",
    r"^没事+。*$",
    r"^没事儿+。*$",
    r"^没什么+。*$",
)

# 出现这些词就当作有真实诉求，不再按敷衍句丢弃
_MEANING_KEYWORDS = (
    "发烧",
    "咳嗽",
    "感冒",
    "疼",
    "痛",
    "出血",
    "不舒服",
    "难受",
    "怎么办",
    "怎么治",
    "原因",
    "症状",
    "怎么缓解",
    "如何处理",
    "用药",
    "检查",
    "吃什么",
    "注意事项",
    "能不能",
    "正常吗",
    "怀孕",
    "月经",
    "过敏",
    "头晕",
    "恶心",
    "呕吐",
    "不适",
    "需要看医生吗",
    "挂什么科",
    "怀疑",
    "手术",
    "体检",
    "咳",
    "痒",
    "咽喉",
    "嗓子",
    "肚子",
    "脚",
    "腿",
    "胸口",
    "嗓子痛",
    "晕",
    "头痛",
    "最近",
    "一天",
    "晚上",
    "白天",
)

_PUNCTUATION = (
    r"[\s\u3000\uff01\uff0c\uff1f\uff1a\uff1b\uff08\uff09\uff0e"
    r"\u201c\u201d\u2018\u2019\u3002\uff0c\uff0e"
    r"\.\,\!\?\:\;\'\"\(\)\[\]\{\}\<\>\@\#\$\%\^\&\*\-\_\=\+\|\/\\~…·—。，、；：“”‘’（）《》？]"
)


def is_semantic_valid(value: Any) -> bool:
    """判断输入在医患问诊场景下是否具备实际语义内容。"""
    if isinstance(value, list):
        return any(is_semantic_valid(item) for item in value)
    if not isinstance(value, str):
        return False
    if not value.strip():
        return False

    if not re.sub(_PUNCTUATION, "", value):
        return False

    for pattern in _MEANINGLESS_PATTERNS:
        if re.fullmatch(pattern, value, re.IGNORECASE):
            return any(keyword in value for keyword in _MEANING_KEYWORDS)
    return True


def _concat_text(value: Any) -> str:
    if isinstance(value, list):
        return "".join(
            str(item).strip() for item in value if item is not None and str(item).strip()
        )
    if isinstance(value, str):
        return value
    return ""


def _clean_sentences(text: str) -> str:
    """按句切分后逐句去掉敷衍尾巴，再拼回去。"""
    parts = re.split(r"(。|！|!|？|\?)", text)
    cleaned: list[str] = []
    for index in range(0, len(parts) - 1, 2):
        sentence = re.sub(r"(呃{3,})", "", parts[index], flags=re.IGNORECASE)
        sentence = remove_trailing_meaningless(sentence)
        if sentence:
            cleaned.append(sentence + parts[index + 1])

    # 结尾可能没有标点，单独处理
    if len(parts) % 2 == 1 and parts[-1].strip():
        tail = re.sub(r"(呃{3,})", "", parts[-1], flags=re.IGNORECASE)
        tail = remove_trailing_meaningless(tail)
        if tail:
            cleaned.append(tail)
    return "".join(cleaned).strip()


def integrate(arg1: Any, arg2: Any) -> dict[str, Any]:
    """对话整合：arg1 是本轮新增，arg2 是截至当前的历史累积。

    返回 result（拼接清洗后的完整对话）、arg1_text（本轮原文）、exceed_six
    （句数是否超过阈值，供调用方决定要不要分段）。
    """
    arg1_str = _concat_text(arg1)
    arg2_str = _concat_text(arg2)
    arg1_text = arg1_str.strip()

    if is_semantic_valid(arg1):
        if arg2_str.strip():
            combined = arg2_str.strip().rstrip("。") + "。" + arg1_str.strip()
        else:
            combined = arg1_str.strip()
    else:
        combined = arg2_str.strip()

    result = _clean_sentences(combined)
    sentences = [item.strip() for item in re.split(r"[。！!？\?]", result) if item.strip()]
    return {
        "result": result,
        "arg1_text": arg1_text,
        "exceed_six": 1 if len(sentences) > SPLIT_SENTENCE_LIMIT else 0,
    }
