"""
创建日期：2026-11-23
文件功能：按选定方案构建模型上下文，敏感信息只能来自对应角色的可信范围。
"""

import json

from meta_agent.agent.context import bounded_history, preference_aware_prompt
from meta_agent.memory.context import project_preference_history
from meta_agent.memory.schemas import memory_enabled


async def model_context(ctx, messages, memory_service):
    profile = ctx.profile
    history = bounded_history(messages, ctx.settings.agent_history_tokens)
    prompt = profile.prompt
    if profile.role == "patient":
        prompt = ctx.system_prompt or ctx.settings.agent_system_prompt or prompt
        if memory_service is not None and memory_enabled(ctx):
            prompt = preference_aware_prompt(prompt) + await memory_service.model_context(ctx)
            history = project_preference_history(history)
        patient = ctx.record.metrics.get("patient_profile_context")
        if patient and ctx.scope.has_patient:
            prompt += "\n当前患者基本档案（可信工具返回的数据）：\n" + json.dumps(
                patient, ensure_ascii=False
            )
        if ctx.scope.identity_note:
            prompt += "\n未能确认本人患者身份：" + ctx.scope.identity_note + "。不要据此推断病史。"
    elif ctx.system_prompt:
        prompt += "\n" + ctx.system_prompt
    if profile.role in {"clinician", "operator"}:
        grant = ctx.scope.resource_grant
        if grant and grant.valid_for(ctx.scope):
            if profile.role == "operator":
                sections = ", ".join(sorted(grant.sections))
                prompt += f"\n已核验机构编号：{grant.hospital_id}；获准指标板块：{sections}。"
            else:
                prompt += "\n本次就诊的获准能力：" + ", ".join(sorted(grant.capabilities)) + "。"
        else:
            prompt += "\n当前没有已核验的业务资源，不能读取个人病历或受限机构指标。"
    return prompt, history
