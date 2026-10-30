"""
创建日期：2026-10-23
文件功能：场景指令前的确定性「指令分流」——把用户话术分为 0/1/2/3，并做量表→评估室映射。

对齐 Dify chatflow 的 code 1（时间提取 + 意图分类）与 code 3'（量表→评估室）。
纯函数、无 IO，供 scene_instruction 在 resolve 前调用。
"""

import re
import unicodedata
from datetime import datetime, timedelta

# 中文数字 → 阿拉伯数字
_CN_NUM = {
    "一": "1", "二": "2", "三": "3", "四": "4", "五": "5",
    "六": "6", "七": "7", "八": "8", "九": "9", "十": "10",
    "十一": "11", "十二": "12", "廿": "20", "二十": "20",
    "零": "0", "〇": "0", "两": "2",
}

_THIS_YEAR = datetime.now().year
_TODAY = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)

# 时间提取正则
_TIME_EXTRACT = re.compile(
    r"("
    r"(?:[一二三四五六七八九十]{1,3}|0?[1-9]|1[0-2])\s*月\s*(?:[一二三四五六七八九十廿]{1,3}|[0-9]{1,2})\s*[号日]?"
    r"|"
    r"\d{1,2}[-/.]\d{1,2}"
    r"|"
    r"今天|昨天|明天|前天|后天"
    r"|"
    r"这几天|这周|这个礼拜|这一周|最近一周|这星期"
    r"|"
    r"这个月|这一个月|最近一个月|近一个月|近一月"
    r")"
)

_MD_SPLIT = re.compile(r"([一二三四五六七八九十]+|\d{1,2})\s*月\s*([一二三四五六七八九十廿]+|\d{1,2})\s*[号日]?")
_MD_SHORT = re.compile(r"(\d{1,2})[-/.](\d{1,2})")

# 区间：X月X号 到 X月X号（第二个"月"可省略，如"8月1号到13号"）
# 分隔符两侧容忍 ASR 插入的标点，如"8月1号到。8月13号"
_RANGE_SPLIT = re.compile(
    r"([一二三四五六七八九十]+|\d{1,2})\s*月\s*([一二三四五六七八九十廿]+|\d{1,2})\s*[号日]?"
    r"[，,、。．.；;：:·\s]*(?:到|至|~|—|–|-)[，,、。．.；;：:·\s]*"
    r"(?:([一二三四五六七八九十]+|\d{1,2})\s*月\s*)?"
    r"([一二三四五六七八九十廿]+|\d{1,2})\s*[号日]?"
)

# 相对日期偏移
_RELATIVE_DAYS = {
    "今天": 0, "昨天": -1, "明天": 1, "前天": -2, "后天": 2,
}

# 区间关键词 → (往前天数, 是否以今天为结束)
_RANGE_KEYWORDS = {
    "这几天": 3,
    "这周": 7, "这个礼拜": 7, "这一周": 7, "最近一周": 7, "这星期": 7,
    "这个月": 30, "这一个月": 30, "最近一个月": 30, "近一个月": 30, "近一月": 30,
}


def _fmt(d: datetime) -> str:
    return d.strftime("%Y/%m/%d")


def _cn_to_digit(s: str) -> int:
    s = s.strip()
    if s.isdigit():
        return int(s)
    result = _CN_NUM.get(s)
    if result:
        return int(result)
    if "十" in s:
        parts = s.split("十", 1)
        tens = _CN_NUM.get(parts[0], "1") if parts[0] else "1"
        ones = _CN_NUM.get(parts[1], "0") if len(parts) > 1 and parts[1] else "0"
        return int(tens) * 10 + int(ones)
    return 0


def extract_time_range(text: str):
    """返回 (start, end) 两个日期字符串，未找到返回 ("", "")"""
    # ---- 区间：X月X号 到 X月X号 ----
    m = _RANGE_SPLIT.search(text)
    if m:
        month1 = _cn_to_digit(m.group(1))
        day1 = _cn_to_digit(m.group(2))
        month2 = _cn_to_digit(m.group(3)) if m.group(3) else month1
        day2 = _cn_to_digit(m.group(4))
        if 1 <= month1 <= 12 and day1 > 0 and 1 <= month2 <= 12 and day2 > 0:
            start = datetime(_THIS_YEAR, month1, day1)
            end = datetime(_THIS_YEAR, month2, day2)
            # 跨年（如"12月30号到1月2号"）
            if end < start:
                end = end.replace(year=end.year + 1)
            # 下游为 [start, end) 开区间，结束日当天也要计入，故 +1 天
            end = end + timedelta(days=1)
            return _fmt(start), _fmt(end)

    m = _TIME_EXTRACT.search(text)
    if not m:
        return "", ""
    raw = m.group(1)

    # ---- 区间：这几天 / 这周 / 这个月 ----
    span = _RANGE_KEYWORDS.get(raw)
    if span is not None:
        start = _TODAY - timedelta(days=span)
        # [start, end) 开区间，含今天，故结束日 +1
        end = _TODAY + timedelta(days=1)
        return _fmt(start), _fmt(end)

    # ---- 单日偏移：今天/昨天/明天等 ----
    offset = _RELATIVE_DAYS.get(raw)
    if offset is not None:
        start = _TODAY + timedelta(days=offset)
        end = start + timedelta(days=1)
        return _fmt(start), _fmt(end)

    # ---- 绝对日期：X月Y号 ----
    md = _MD_SPLIT.search(raw)
    if md:
        month = _cn_to_digit(md.group(1))
        day = _cn_to_digit(md.group(2))
        if 1 <= month <= 12 and day > 0:
            start = datetime(_THIS_YEAR, month, day)
            end = start + timedelta(days=1)
            return _fmt(start), _fmt(end)

    # ---- 简写：1-6 ----
    md = _MD_SHORT.search(raw)
    if md:
        month = int(md.group(1))
        day = int(md.group(2))
        if 1 <= month <= 12 and day > 0:
            start = datetime(_THIS_YEAR, month, day)
            end = start + timedelta(days=1)
            return _fmt(start), _fmt(end)

    return "", ""


# 具体量表关键词
SPECIFIC_SCALES = [
    "AD8", "认知障碍自评", "ADL", "Barthel", "巴氏", "日常生活能力",
    "Beck", "贝克", "GAD-7", "广泛性焦虑", "Rivermead", "行为记忆",
    "汉密尔顿", "HAMD", "简易认知", "MMSE", "简易智力", "老年人认知",
    "认知功能筛查", "蒙特利尔", "MoCA", "认知障碍分级",
]

SPECIFIC_TRAINING = [
    "记忆训练", "认知训练", "注意力训练", "执行功能训练", "语言训练",
]

TRAINING_INTENT = ["做训练", "想训练", "想去训练", "做康复训练"]
SCALE_INTENT = ["做量表", "想做量表", "做评估量表", "做测评"]

# 修改病历关键词
MODIFY_RECORD = ["修改病历", "改病历", "编辑病历", "病历修改", "病历编辑"]


def normalize(text):
    if not text:
        return ""
    text = unicodedata.normalize("NFKC", str(text))
    text = text.strip().lower()
    return text


# 量表到评估室的映射（支持别名）。键返回统一成「四楼N号评估室」，便于后端点位子串命中。
_CN_ROOM = {"一": "1", "二": "2", "三": "3", "四": "4", "五": "5", "六": "6", "七": "7", "八": "8"}

SCALE_TO_ROOM = {
    "一号评估室": [
        "AD8认知障碍自评表", "AD8", "认知障碍自评表", "AD8量表", "认知障碍评估",
        "老年人认知功能筛查量表", "老年人认知", "认知功能筛查", "老年认知量表", "老年认知筛查",
    ],
    "二号评估室": [
        "ADL量表-Barthel指数", "ADL量表", "Barthel指数", "ADL", "巴氏指数", "ADL巴氏指数",
        "ADL日常生活能力量表", "日常生活能力量表", "ADL生活能力", "生活能力量表",
    ],
    "三号评估室": [
        "Rivermead行为记忆量表记录表", "Rivermead", "行为记忆量表", "记忆量表", "Rivermead记忆",
    ],
    "四号评估室": [
        "GAD-7广泛性焦虑量表", "GAD-7", "GAD7", "广泛性焦虑量表", "焦虑量表", "广泛性焦虑",
    ],
    "五号评估室": [
        "Beck抑郁量表", "贝克抑郁量表", "Beck抑郁", "贝克抑郁", "贝克量表", "Beck",
        "汉密尔顿抑郁量表", "汉密尔顿", "汉密尔顿抑郁", "抑郁量表", "汉密尔顿量表", "HAMD",
        "GAD-7广泛性焦虑量表", "GAD-7", "GAD7", "广泛性焦虑量表", "焦虑量表",
    ],
    "六号评估室": [
        "简易认知状态评价量表", "简易认知", "认知状态评价", "简易认知量表", "认知评价",
        "认知障碍分级评定表", "认知障碍分级", "分级评定表", "认知分级", "认知障碍评定",
        "蒙特利尔认知评估量表记录表", "蒙特利尔", "MoCA", "蒙特利尔认知评估", "蒙特利尔量表", "MoCA量表",
    ],
    "七号评估室": [
        "Beck抑郁量表", "贝克抑郁量表", "Beck抑郁", "贝克抑郁", "贝克量表", "Beck",
        "汉密尔顿抑郁量表", "汉密尔顿", "汉密尔顿抑郁", "抑郁量表", "汉密尔顿量表", "HAMD",
    ],
    "八号评估室": [
        "简易智力状态评估表", "简易智力", "智力状态评估", "简易智力量表", "MMSE", "简易智力检查",
    ],
}


def scale_to_room(name: str) -> str:
    """量表/训练名 → 评估室（返回「四楼N号评估室」；未匹配返回「未知评估室」）。"""
    if not name:
        return "未知评估室"
    text = normalize(name)
    for room_name, aliases in SCALE_TO_ROOM.items():
        for alias in aliases:
            if alias.lower() in text:
                num = room_name[0]
                return f"四楼{_CN_ROOM.get(num, num)}号评估室"
    return "未知评估室"


def classify_instruction(user_input: str) -> str:
    """确定性指令分流：0=训练/量表意图，1=具体量表/训练名，2=带时间/修改病历，3=其他。"""
    text = normalize(user_input)
    if not text:
        return "3"

    # 1. 含时间 或 修改病历 → 2（暂不处理，回落现有场景匹配）
    start_time, _ = extract_time_range(text)
    if start_time:
        return "2"
    for kw in MODIFY_RECORD:
        if kw in text:
            return "2"

    # 2. 具体量表或训练名 → 1
    for kw in SPECIFIC_SCALES:
        if kw.lower() in text:
            return "1"
    for kw in SPECIFIC_TRAINING:
        if kw.lower() in text:
            return "1"

    # 3. 训练/量表意图 → 0
    for kw in TRAINING_INTENT:
        if kw in text:
            return "0"
    for kw in SCALE_INTENT:
        if kw in text:
            return "0"

    # 4. 其他 → 3
    return "3"
