"""
创建日期：2026-10-09
文件功能：校验提示词变量并按固定版本渲染模型消息，保留来源元数据。
"""

import json

from pydantic import BaseModel

from meta_agent.prompts.catalog import load_catalog
from meta_agent.prompts.contracts import AgentPromptInputs, PromptBinding, RenderedPrompt


class PromptService:
    def __init__(self) -> None:
        self._catalog = load_catalog()

    def bind(self, bundle: str = "builtin-v1") -> PromptBinding:
        try:
            return self._catalog[bundle]
        except KeyError as exc:
            raise ValueError(f"Unknown prompt bundle: {bundle}") from exc

    def render(
        self, binding: PromptBinding, prompt_id: str, variables: BaseModel
    ) -> RenderedPrompt:
        definition = binding.definition(prompt_id)
        if type(variables) is not definition.input_model:
            raise TypeError(f"{prompt_id} requires {definition.input_model.__name__}")
        values = definition.input_model.model_validate(variables.model_dump())
        names = [name for name, _ in definition.components]
        used = names[:1]
        if isinstance(values, AgentPromptInputs):
            text = definition.text("agent_system")
            text += definition.text("agent_time").replace("{now}", values.now.isoformat())
            used.append("agent_time")
            if values.patient_profile:
                text += definition.text("patient_profile") + json.dumps(
                    values.patient_profile, ensure_ascii=False
                )
                used.append("patient_profile")
            if values.budget_exhausted:
                text += definition.text("budget_exhausted")
                used.append("budget_exhausted")
            messages = (("system", text),)
        elif prompt_id == "intent.repair":
            messages = (("human", definition.text("intent_repair")),)
        else:
            messages = (
                ("system", definition.text(names[0])),
                ("human", json.dumps(values.model_dump(mode="json"), ensure_ascii=False)),
            )
        return RenderedPrompt(
            messages,
            prompt_id,
            definition.version,
            definition.template_hash,
            tuple(f"{name}@{dict(definition.component_versions)[name]}" for name in used),
        )
