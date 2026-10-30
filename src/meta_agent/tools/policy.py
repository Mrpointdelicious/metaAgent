"""
创建日期：2026-10-30
文件功能：统一兼容工具名的接入方拒绝策略，防止通过别名绕过限制。
"""

ALIASES = {
    "search_online_doctors": "search_doctors",
    "resolve_scene_instruction": "navigate_scene",
}


def tool_is_excluded(scope, name):
    canonical = ALIASES.get(name, name)
    excluded = {ALIASES.get(item, item) for item in getattr(scope, "excluded_tools", ())}
    if name == "resolve_doctor_selection":
        return name in excluded or "search_doctors" in excluded
    return canonical in excluded
