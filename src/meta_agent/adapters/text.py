"""
创建日期：2026-11-03
文件功能：防止模型自然语言输出伪造前端可执行的控制标签。
"""

import re

CONTROL = re.compile(
    r"\[(?:Telepor:|ScenePoint:|ClickPoint:|GamePoint:|DoctorRecommend:|Cweb|web)\]"
)


def model_text(text: str) -> str:
    return CONTROL.sub(lambda match: "［" + match.group()[1:-1] + "］", text)
