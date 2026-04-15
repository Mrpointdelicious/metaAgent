from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


RiskLevel = Literal["low", "medium", "high"]


class TimeRange(BaseModel):
    start: datetime
    end: datetime
    label: str


class EvidenceItem(BaseModel):
    source: str
    message: str
    related_ids: list[int] = Field(default_factory=list)
