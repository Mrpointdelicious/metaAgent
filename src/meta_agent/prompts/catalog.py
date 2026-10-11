"""
创建日期：2026-10-09
文件功能：从随包模板加载经过注册的不可变提示词版本。
"""

from importlib.resources import files
from types import MappingProxyType

from meta_agent.contracts import IntentDecision, fingerprint
from meta_agent.prompts.contracts import (
    AgentPromptInputs,
    EmptyPromptInputs,
    FactSelection,
    FactSelectionPromptInputs,
    FrontendPromptInputs,
    IntentPromptInputs,
    PromptBinding,
    PromptDefinition,
)

MANIFEST = MappingProxyType(
    {
        ("rehab.agent", "business-v9"): (
            AgentPromptInputs,
            None,
            (
                ("agent_system", "v1"),
                ("business_grounding", "v9"),
                ("business_context", "v1"),
                ("agent_time", "v1"),
                ("patient_profile", "v1"),
                ("budget_exhausted", "v1"),
            ),
        ),
        ("rehab.agent", "business-v8"): (
            AgentPromptInputs,
            None,
            (
                ("agent_system", "v1"),
                ("business_grounding", "v8"),
                ("business_context", "v1"),
                ("agent_time", "v1"),
                ("patient_profile", "v1"),
                ("budget_exhausted", "v1"),
            ),
        ),
        ("rehab.agent", "business-v7"): (
            AgentPromptInputs,
            None,
            (
                ("agent_system", "v1"),
                ("business_grounding", "v7"),
                ("business_context", "v1"),
                ("agent_time", "v1"),
                ("patient_profile", "v1"),
                ("budget_exhausted", "v1"),
            ),
        ),
        ("rehab.agent", "business-v6"): (
            AgentPromptInputs,
            None,
            (
                ("agent_system", "v1"),
                ("business_grounding", "v6"),
                ("business_context", "v1"),
                ("agent_time", "v1"),
                ("patient_profile", "v1"),
                ("budget_exhausted", "v1"),
            ),
        ),
        ("rehab.agent", "business-v5"): (
            AgentPromptInputs,
            None,
            (
                ("agent_system", "v1"),
                ("business_grounding", "v5"),
                ("business_context", "v1"),
                ("agent_time", "v1"),
                ("patient_profile", "v1"),
                ("budget_exhausted", "v1"),
            ),
        ),
        ("rehab.agent", "v1"): (
            AgentPromptInputs,
            None,
            (
                ("agent_system", "v1"),
                ("agent_time", "v1"),
                ("patient_profile", "v1"),
                ("budget_exhausted", "v1"),
            ),
        ),
        ("rehab.agent", "business-v2"): (
            AgentPromptInputs,
            None,
            (
                ("agent_system", "v1"),
                ("business_grounding", "v2"),
                ("agent_time", "v1"),
                ("patient_profile", "v1"),
                ("budget_exhausted", "v1"),
            ),
        ),
        ("intent.parse", "v1"): (IntentPromptInputs, IntentDecision, (("intent_system", "v1"),)),
        ("rehab.agent", "business-v3"): (
            AgentPromptInputs,
            None,
            (
                ("agent_system", "v1"),
                ("business_grounding", "v3"),
                ("agent_time", "v1"),
                ("patient_profile", "v1"),
                ("budget_exhausted", "v1"),
            ),
        ),
        ("intent.repair", "v1"): (EmptyPromptInputs, None, (("intent_repair", "v1"),)),
        ("rehab.agent", "business-v4"): (
            AgentPromptInputs,
            None,
            (
                ("agent_system", "v1"),
                ("business_grounding", "v4"),
                ("agent_time", "v1"),
                ("patient_profile", "v1"),
                ("budget_exhausted", "v1"),
            ),
        ),
        ("frontend.context", "v1"): (FrontendPromptInputs, None, (("frontend_context", "v1"),)),
        ("answer.fact_select", "v1"): (
            FactSelectionPromptInputs,
            FactSelection,
            (("fact_select_system", "v1"),),
        ),
    }
)

BUNDLES = MappingProxyType(
    {
        "business-grounded-v9": (
            ("rehab.agent", "business-v9"),
            ("intent.parse", "v1"),
            ("intent.repair", "v1"),
            ("answer.fact_select", "v1"),
            ("frontend.context", "v1"),
        ),
        "business-grounded-v8": (
            ("rehab.agent", "business-v8"),
            ("intent.parse", "v1"),
            ("intent.repair", "v1"),
            ("answer.fact_select", "v1"),
            ("frontend.context", "v1"),
        ),
        "business-grounded-v7": (
            ("rehab.agent", "business-v7"),
            ("intent.parse", "v1"),
            ("intent.repair", "v1"),
            ("answer.fact_select", "v1"),
            ("frontend.context", "v1"),
        ),
        "business-grounded-v6": (
            ("rehab.agent", "business-v6"),
            ("intent.parse", "v1"),
            ("intent.repair", "v1"),
            ("answer.fact_select", "v1"),
            ("frontend.context", "v1"),
        ),
        "business-grounded-v5": (
            ("rehab.agent", "business-v5"),
            ("intent.parse", "v1"),
            ("intent.repair", "v1"),
            ("answer.fact_select", "v1"),
            ("frontend.context", "v1"),
        ),
        "builtin-v1": (
            ("rehab.agent", "v1"),
            ("intent.parse", "v1"),
            ("intent.repair", "v1"),
            ("answer.fact_select", "v1"),
            ("frontend.context", "v1"),
        ),
        "business-grounded-v2": (
            ("rehab.agent", "business-v2"),
            ("intent.parse", "v1"),
            ("intent.repair", "v1"),
            ("answer.fact_select", "v1"),
            ("frontend.context", "v1"),
        ),
        "business-grounded-v3": (
            ("rehab.agent", "business-v3"),
            ("intent.parse", "v1"),
            ("intent.repair", "v1"),
            ("answer.fact_select", "v1"),
            ("frontend.context", "v1"),
        ),
        "business-grounded-v4": (
            ("rehab.agent", "business-v4"),
            ("intent.parse", "v1"),
            ("intent.repair", "v1"),
            ("answer.fact_select", "v1"),
            ("frontend.context", "v1"),
        ),
    }
)


def load_catalog() -> MappingProxyType:
    definitions = {}
    root = files("meta_agent.prompts").joinpath("templates")
    for (prompt_id, version), (inputs, output, versions) in MANIFEST.items():
        components = tuple(
            (name, root.joinpath(f"{name}.{fragment_version}.md").read_text(encoding="utf-8"))
            for name, fragment_version in versions
        )
        if any(not text.strip() for _, text in components):
            raise ValueError(f"Empty prompt resource: {prompt_id}")
        if dict(components).get("agent_time", "{now}").count("{now}") != 1:
            raise ValueError("Agent time template requires one {now} placeholder")
        definitions[prompt_id, version] = PromptDefinition(
            prompt_id,
            version,
            inputs,
            output,
            components,
            versions,
            fingerprint(
                [
                    components,
                    versions,
                    inputs.model_json_schema(),
                    output.model_json_schema() if output else None,
                ]
            ),
        )
    catalog = {}
    for bundle, references in BUNDLES.items():
        if len({prompt_id for prompt_id, _ in references}) != len(references):
            raise ValueError(f"Duplicate prompt ID in bundle: {bundle}")
        try:
            bound = tuple(definitions[reference] for reference in references)
        except KeyError as exc:
            raise ValueError(f"Unknown prompt version in bundle: {bundle}") from exc
        catalog[bundle] = PromptBinding(bundle, bound)
    return MappingProxyType(catalog)
