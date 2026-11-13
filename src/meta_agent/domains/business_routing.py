"""
创建日期：2026-10-30
文件功能：在同一Agent循环内区分知识提问、场景动作、患者查询及混合请求。
"""

import re
import unicodedata
from dataclasses import dataclass

from meta_agent.domains.instruction_classify import classify_instruction

SCENE_TOOLS = frozenset({"navigate_scene", "resolve_scene_instruction"})
KNOWLEDGE_TOOLS = frozenset({"search_knowledge"})
_QUESTIONS = (
    "介绍", "解释", "说明", "是什么", "什么是", "怎么", "如何", "在哪", "哪里", "功能", "干什么",
)
_HISTORY = ("对话记录", "聊天记录", "对话历史", "历史对话", "聊天历史")
# 医院运营数据（query_hospital_operations 的领域）：与患者个人数据是两套权限，
# 患者身份问这些时不能编数字，也不能指路去某个楼层看大屏。
_HOSPITAL_OPS = (
    "运营数据",
    "运营报表",
    "运营分析",
    "门诊量",
    "门诊人次",
    "住院量",
    "出院人次",
    "床位使用",
    "床位周转",
    "患者流量",
    "营收",
    "经营数据",
    "科室绩效",
)
_PATIENT = (
    "病历",
    "病史",
    "诊断",
    "医嘱",
    "用药",
    "训练记录",
    "训练史",
    "训练报告",
    "测评报告",
    "评估报告",
    "我的报告",
    # 「下肢报告」「上肢报告」「康复报告」这类按部位/类别叫的报告，用户不会说全称，
    # 用「报告」兜住；「打开+报告」仍按界面指令口径走场景，见 _UI_OPEN 分支。
    "报告",
    "评估结果",
    "测评结果",
)
# 与 scene_local.PREFIX_WORDS 的导航/动作前缀保持同口径。单字新增项（到/上/玩）
# 要求后面至少还有 2 个字符的目标，避免「到」「上」「玩」这类残句被当成场景指令。
_COMMAND = re.compile(
    r"^(?:(?:请|帮我|麻烦|我要|我想要|我想|给我)\s*)*"
    r"(?:打开|关闭|切换|带(?:我)?(?:去|到|回)|来一局|开始玩|"
    r"去|前往|回到|返回|退回|后退|回|进入|退出|传送|放大|缩小|确认|取消|投屏|"
    r"到(?=.{2,})|上(?=.{2,})|玩(?=.{2,}))"
)
_NEGATION = re.compile(
    r"不要|不许|禁止|别(?:打开|关闭|去|切换)|如果|能否|可不可以|能不能|(?:可以|能).*[吗么][?？]?$"
)
# 「打开/关闭/切换/放大/缩小/投屏 + 记录/报告」按提示词口径是打开页面的界面指令
# （走 navigate_scene），不是患者数据查询；历史对话（_HISTORY）仍优先于此口径。
_UI_OPEN = re.compile(
    r"^(?:(?:请|帮我|麻烦|我要|我想要|我想|给我)\s*)?"
    r"(?:打开|关闭|切换|放大|缩小|投屏)"
)


# 「查」「好」「看看」这类极短回复是承接上一轮的省略追问，本身不含业务关键词；
# 若按空路由处理，模型拿不到落点会直接自由作答（实测 steps 0）。
_FOLLOWUP = re.compile(
    r"^(?:查|查一下|查下|查查|看看吧?|看一下|看下|要|好|行|嗯|对|是|可以|继续|"
    r"那就查|那就看|帮我查|帮我看看|帮我查查)[吧呀啊哦。！!？?~～\s]*$"
)


# 「含时间 → 记录查询」是上游 Dify 链路的口径，那条链路里闲聊已被前置过滤；
# 在通用路由里还要再确认这句确实在问记录，否则「今天天气怎么样」会被当成查患者数据。
_RECORD_LIKE = re.compile(
    r"记录|报告|病历|病史|诊断|医嘱|用药|测评|评估|量表|训练|练了|练过|数据|档案|"
    r"查一下|查查|帮我查|查看|看看|看一下|调取|统计|几次|多久|多长时间"
)


@dataclass(frozen=True)
class BusinessRoute:
    knowledge: bool = False
    navigation_question: bool = False
    scene: bool = False
    history: bool = False
    patient: bool = False
    uncertain: bool = False
    record_query: bool = False
    followup: bool = False
    hospital_ops: bool = False

    def allows(self, tool_name):
        # Routing can narrow permissions, never grant them.
        if tool_name in SCENE_TOOLS:
            return self.scene or not (
                self.knowledge
                or self.navigation_question
                or self.history
                or self.patient
                or self.uncertain
            )
        return not (tool_name in KNOWLEDGE_TOOLS and self.scene and not self.knowledge)

    def guidance(self):
        hints = []
        if self.navigation_question:
            hints.append(
                "用户在问路而非要求移动；检索product资料（场景库）说明路线，不编造电梯、楼梯或点位。"
            )
        elif self.knowledge:
            hints.append("介绍、说明、是什么、怎么用等问题先检索知识资料；依据原文和真实来源回答。")
        if self.scene:
            hints.append(
                "明确场景动作使用navigate_scene；工具只接受本轮原话，最终指令由本地场景目录校验。"
                "resolve_scene_instruction仅作匹配预览，不交付动作。"
            )
        if self.history:
            hints.append("历史对话使用open_chat_history；只能读取当前可信会话，不拼接链接。")
        if self.patient:
            if self.record_query:
                hints.append(
                    "带时间的记录/报告请求是查询患者数据：训练记录/训练报告用康复历史工具"
                    "（get_irego_patient_history），量表测评用 get_assessment_patient_history，"
                    "病历/问诊用患者就诊工具；按用户原话时间（如今天）过滤，查不到如实说明，不编造记录。"
                )
            else:
                hints.append(
                    "具体病历、诊断、医嘱和各类报告使用患者工具，不用通用知识代替患者事实："
                    "训练/康复报告与训练记录用 get_irego_patient_history，"
                    "量表测评结果用 get_assessment_patient_history，病历/问诊记录用患者就诊工具；"
                    "按用户说的部位或类别（如下肢）在结果里筛选，查不到如实说明。"
                )
        if self.scene and (self.knowledge or self.patient or self.history):
            hints.append("本轮包含多个请求，分别调用对应工具，不遗漏其他问题。")
        if self.uncertain:
            hints.append("否定、条件或能力询问不等于请求执行；先回答或澄清，不交付场景动作。")
        if self.followup:
            hints.append(
                "本轮只是承接上一轮的省略追问：沿用上一轮已确认的对象直接调用对应工具"
                "（记录/报告类用患者工具），不要反问、不要重述操作菜单。"
            )
        if self.hospital_ops:
            hints.append(
                "本轮要的是医院运营数据（门诊量/住院量/床位/流量等）：先用机构目录工具"
                "（query_hospital_directory）确认是哪家机构并拿到编号，再调用"
                "query_hospital_operations 查询；机构不明确时问清是哪家医院，"
                "不要编造数字，也不要指引去某个楼层看大屏。"
            )
        return "\n".join(hints)


def classify_business_query(query: str) -> BusinessRoute:
    text = unicodedata.normalize("NFKC", query).strip().lower()
    flags = dict.fromkeys(
        (
            "knowledge",
            "navigation_question",
            "scene",
            "history",
            "patient",
            "uncertain",
            "record_query",
            "followup",
            "hospital_ops",
        ),
        False,
    )
    # Clause splitting is only a routing hint; executable boundaries come from the local resolver.
    for clause in re.split(
        r"[，,；;。]|然后|接着|顺便|另外|并且|同时|并(?=告诉|介绍|解释|说明|查|看|打开|关闭|去)"
        r"|再(?=查|看|介绍|打开|关闭|去)",
        text,
    ):
        clause = clause.strip()
        if not clause:
            continue
        history = any(word in clause for word in _HISTORY)
        hospital_ops = any(word in clause for word in _HOSPITAL_OPS)
        question = any(word in clause for word in _QUESTIONS if word != "功能") or (
            "功能" in clause and not _COMMAND.search(clause)
        )
        patient_words = any(word in clause for word in _PATIENT)
        uncertain = bool(_NEGATION.search(clause))
        # 指令分流（对齐 Dify chatflow code 1 / feature-runtime-cutover）：
        # 量表/训练意图（"0"/"1"）是场景指令——量表→评估室映射由 scene_local 完成；
        # 带时间或「修改病历」（"2"）是患者数据查询，不是场景跳转。
        intent = classify_instruction(clause)
        record = False
        if intent == "2" and _RECORD_LIKE.search(clause):
            # 「打开/查看今天的训练记录」含时间 → 查数据，即使动词是「打开」。
            patient = True
            scene = False
            record = True
        elif _UI_OPEN.search(clause) and patient_words:
            # 「打开+记录/报告」是打开页面的界面指令（提示词口径），不是查数据；
            # 历史对话与疑问仍优先。
            patient = False
            scene = not (history or question or uncertain)
        else:
            patient = patient_words
            scene = (
                bool(_COMMAND.search(clause)) or intent in ("0", "1")
            ) and not (history or question or patient or uncertain or hospital_ops)
        navigation = question and bool(
            re.search(r"楼|层|入口|大厅|房间|中心|区域|评估室|训练室", clause)
        )
        flags["history"] |= history
        flags["knowledge"] |= question and not history
        flags["navigation_question"] |= navigation
        flags["patient"] |= patient
        flags["scene"] |= scene
        flags["uncertain"] |= uncertain
        flags["record_query"] |= record
        flags["hospital_ops"] |= hospital_ops
    if not any(flags.values()) and _FOLLOWUP.match(text):
        # 整句没有任何业务关键词，只是「查」「好」这类承接词：标记为省略追问，
        # 让模型沿用上一轮对象，而不是当成闲聊或重新问一遍。
        flags["followup"] = True
    return BusinessRoute(**flags)
