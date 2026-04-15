from typing import Literal

from pydantic import BaseModel

from config import LLMProvider


TaskType = Literal["single_review", "risk_screen", "weekly_report", "unsupported"]


class OrchestratorRequest(BaseModel):
    task_type: TaskType | None = None
    patient_id: int | None = None
    plan_id: int | None = None
    therapist_id: int | None = None
    days: int | None = None
    top_k: int = 10
    raw_text: str | None = None
    use_agent_sdk: bool | None = None
    llm_provider: LLMProvider | None = None
    llm_model: str | None = None
    llm_base_url: str | None = None


class OrchestratorResponse(BaseModel):
    task_type: TaskType
    execution_mode: str
    llm_provider: LLMProvider | None = None
    llm_model: str | None = None
    structured_output: dict | list | None
    final_text: str
