"""
创建日期：2026-10-09
文件功能：集中定义接收请求和运行进度的默认展示文本。
"""

ACCEPTED = "收到，我来处理。"
PROGRESS = {
    "agent": "正在结合记录回答。",
    "tool": "正在查询相关信息。",
    "planning": "正在确认本轮目标。",
    "resolving_context": "正在确认当前选择的训练记录。",
    "generating_artifact": "正在生成报告。",
    "composing": "正在整理回答。",
}
