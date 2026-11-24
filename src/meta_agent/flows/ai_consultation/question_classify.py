"""
创建日期：2026-11-18
文件功能：AI诊室（AI问诊）真实链路移植件 —— 移植自 Dify「AI诊室(测试)」工作流代码节点。
"""

import re

# ===== 常量 =====
LABELS = {
    "CLASS_1": "公司/产品/设备操作",
    "CLASS_2": "设备错误码提示",
    "CLASS_3": "人员/医生相关咨询",
}
PRIORITY = ("CLASS_2", "CLASS_1")

ERROR_KW = [
    "错误码","报错","告警","报警","报警码","告警码","报错码", "错误", "报错",
    "故障","故障代码","下位机","协议","错误等级","error","err"
]
ERROR_RE = re.compile(r"(^|[^a-z0-9])(E\d{2,4}|Err\d{1,4})(?![a-z0-9])", re.I)

PRODUCT_NAMES = [
    "iremo","irego","iretour","旅途","irecog","步道","多功能智能康复步道","智能认知康复训练器","上肢康复机器人", "下肢康复机器人", "康复",
    "上肢机器人", "下肢机器人", "上肢", "下肢", "认知机器人", "认知康复机器人","机器", "欢乐盖大楼", "跑酷猫", "汪汪天使", "打地鼠", "太空飞行", "宇宙拼图", "VR",
]

CLASS1_KW = [
    "公司","位置","地址","合作单位","明星产品","愿景","理念","研发成果","售后","服务","服务保障","使命",
    "简介","功能","优势","特点","临床","适用","适用人群","医学原理","技术优势","适合",
    "模板", "计划", "报告", "介绍",
    "急停","急停按钮","硬件","支撑腿","固定","安装","调试","开机","关机",
    "训练","添加","登录","用户","选择","整体硬件", "系统", "减重",
    "步骤","操作","使用方法","安全检查","准备工作","软件","流程",
]

# === 新增：人员/医生相关关键词（触发 CLASS_3） ===
DOCTOR_KW = [
    "医生","专家","主任","教授","护士","医师",
    "挂号","预约","选择某人","选某人","选医生","挂某人",
    "我想挂号","挂一下","约某人","约医生"
]

# ===== 规范化 =====
def to_halfwidth(s: str) -> str:
    out = []
    for ch in s:
        code = ord(ch)
        if code == 0x3000:
            out.append(" ")
        elif 0xFF01 <= code <= 0xFF5E:
            out.append(chr(code - 0xFEE0))
        else:
            out.append(ch)
    return "".join(out)

def normalize(text: str) -> str:
    return to_halfwidth(text or "").lower()

def includes_any(text: str, words) -> str | None:
    for w in words:
        if w and w in text:
            return w
    return None

# ===== 预处理：整句标准化 =====
def preprocess(query: str) -> str:
    q = (query or "").strip()

    FULLMATCH_RULES = [
        (re.compile(r"^(上肢智能康复机器人|上肢康复智能机器人|上肢智能机器人|上肢康复机器人|上肢机器人|上肢|iremo|iReMo)$", re.I),
         "上肢智能康复机器人iReMo"),
        (re.compile(r"^(下肢智能康复机器人|下肢康复智能机器人|下肢智能机器人|下肢康复机器人|下肢机器人|下肢|irego|iReGo)$", re.I),
         "下肢智能康复机器人iReGo"),
        (re.compile(r"^(认知智能康复训练器|认知智能机器人|认知康复机器人|认知机器人|irecog|iReCog)$", re.I),
         "认知智能康复机器人iReCog"),
        (re.compile(r"^(步道|多功能智能康复步道|步道机器人|iReTour)$", re.I),
         "“旅途”多功能智能康复步道iReTour"),
    ]

    for pat, canon in FULLMATCH_RULES:
        if pat.fullmatch(q):
            return canon

    return query or ""

# ===== 分类 =====
def classify(query: str) -> dict[str, str]:
    q = (query or "").strip()
    if not q:
        return {"label": LABELS["CLASS_1"], "reason": "空查询（归类为 CLASS_1）"}

    qn = normalize(q)

    # === 优先判断：错误码 ===
    if ERROR_RE.search(qn):
        return {"label": LABELS["CLASS_2"], "reason": "命中错误码格式(E/Err)"}
    hit_err_kw = includes_any(qn, ERROR_KW)
    if hit_err_kw:
        return {"label": LABELS["CLASS_2"], "reason": f"命中错误码关键词:{hit_err_kw}"}

    # === 新增：人员/医生类型 ===
    hit_doc_kw = includes_any(qn, DOCTOR_KW)
    if hit_doc_kw:
        return {"label": LABELS["CLASS_3"], "reason": f"命中人员/医生关键词:{hit_doc_kw}"}

    # === CLASS_1：公司/产品/操作 ===
    hit_prod = includes_any(qn, PRODUCT_NAMES)
    hit_kw = includes_any(qn, CLASS1_KW)
    if hit_prod or hit_kw:
        return {"label": LABELS["CLASS_1"], "reason": f"命中公司/产品/操作:{hit_prod or hit_kw}"}

    # === 默认 ===
    return {"label": LABELS["CLASS_1"], "reason": "兜底（非错误/非人员）"}


def classify_question(query: str) -> dict:
    """问题分类入口：返回 {"label": <中文名>, "reason": ..., "processed_query": ...}。

    label ∈ CLASS_1（公司/产品/设备操作）/ CLASS_2（设备错误码提示）/
    CLASS_3（人员/医生相关咨询）。
    """
    processed = preprocess(query)
    result = classify(processed)
    result["processed_query"] = processed
    return result
