"""
创建日期：2026-10-30
文件功能：在同一Agent循环内区分知识提问、场景动作、患者查询及混合请求。
"""

import re
import unicodedata
from dataclasses import dataclass

SCENE_TOOLS = frozenset({"navigate_scene", "resolve_scene_instruction"})
KNOWLEDGE_TOOLS = frozenset({"search_knowledge"})
_QUESTIONS = ("介绍", "解释", "说明", "是什么", "怎么", "如何", "在哪", "哪里", "功能", "干什么")
_HISTORY = ("对话记录", "聊天记录", "对话历史", "历史对话", "聊天历史")
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
    "我的报告",
)
_COMMAND = re.compile(
    r"^(?:(?:请|帮我|麻烦|我要|我想要|我想|给我)\s*)*"
    r"(?:打开|关闭|切换|去|前往|带我去|回到|返回|回|进入|退出|传送|放大|缩小|确认|取消|投屏)"
)
_NEGATION = re.compile(
    r"不要|不许|禁止|别(?:打开|关闭|去|切换)|如果|能否|可不可以|能不能|(?:可以|能).*[吗么][?？]?$"
)


@dataclass(frozen=True)
class BusinessRoute:
    knowledge: bool = False
    navigation_question: bool = False
    scene: bool = False
    history: bool = False
    patient: bool = False
    uncertain: bool = False

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
                "用户在问路而非要求移动；检索help/product资料说明路线，不编造电梯、楼梯或点位。"
            )
        elif self.knowledge:
            hints.append("介绍、说明、是什么、怎么用等问题先检索知识资料；依据原文和真实来源回答。")
        if self.scene:
            hints.append(
                "明确场景动作使用navigate_scene；工具只接受本轮原话，最终指令由AI_WebApi校验。"
                "resolve_scene_instruction仅作匹配预览，不交付动作。"
            )
        if self.history:
            hints.append("历史对话使用open_chat_history；只能读取当前可信会话，不拼接链接。")
        if self.patient:
            hints.append("具体病历、诊断、医嘱和训练报告使用患者工具，不用通用知识代替患者事实。")
        if self.scene and (self.knowledge or self.patient or self.history):
            hints.append("本轮包含多个请求，分别调用对应工具，不遗漏其他问题。")
        if self.uncertain:
            hints.append("否定、条件或能力询问不等于请求执行；先回答或澄清，不交付场景动作。")
        return "\n".join(hints)


def classify_business_query(query: str) -> BusinessRoute:
    text = unicodedata.normalize("NFKC", query).strip().lower()
    flags = dict.fromkeys(
        ("knowledge", "navigation_question", "scene", "history", "patient", "uncertain"), False
    )
    # Clause splitting is only a routing hint; executable boundaries come from AI_WebApi.
    for clause in re.split(
        r"[，,；;。]|然后|接着|顺便|另外|并且|同时|并(?=告诉|介绍|解释|说明|查|看|打开|关闭|去)"
        r"|再(?=查|看|介绍|打开|关闭|去)",
        text,
    ):
        clause = clause.strip()
        if not clause:
            continue
        history = any(word in clause for word in _HISTORY)
        question = any(word in clause for word in _QUESTIONS if word != "功能") or (
            "功能" in clause and not _COMMAND.search(clause)
        )
        patient = any(word in clause for word in _PATIENT)
        navigation = question and bool(
            re.search(r"楼|层|入口|大厅|房间|中心|区域|评估室|训练室", clause)
        )
        uncertain = bool(_NEGATION.search(clause))
        scene = bool(_COMMAND.search(clause)) and not (history or question or patient or uncertain)
        flags["history"] |= history
        flags["knowledge"] |= question and not history
        flags["navigation_question"] |= navigation
        flags["patient"] |= patient
        flags["scene"] |= scene
        flags["uncertain"] |= uncertain
    return BusinessRoute(**flags)
