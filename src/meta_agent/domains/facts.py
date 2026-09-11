"""
创建日期：2026-09-08
文件功能：将工具真实字段投影为带来源、单位和质量边界的不可拆分事实。
"""

from datetime import datetime
from typing import Any

from meta_agent.contracts import Fact, FactView, fingerprint
from meta_agent.infrastructure.repository import EvidenceEnvelope, resolve_pointer


def pointer_part(value: str) -> str:
    return value.replace("~", "~0").replace("/", "~1")


def aware_date(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed if parsed.tzinfo is not None else None
    except ValueError:
        return None


class FactBuilder:
    def __init__(self, evidence: EvidenceEnvelope, record_ref: str | None = None,
                 observed_at: Any = None) -> None:
        self.evidence, self.record_ref = evidence, record_ref
        self.observed_at = aware_date(observed_at)
        self.views: list[FactView] = []

    def add(self, path: str, key: str, label: str, *, unit: str | None = None,
            status: str = "valid", required: bool = False, topic: str = "general",
            uses: list[str] | None = None) -> None:
        exists, value = resolve_pointer(self.evidence.payload, path)
        if not exists or value is None:
            status = "missing"
            value = None
        if not isinstance(value, (str, int, float, bool, type(None))):
            return
        if status not in {"valid", "missing", "invalid", "unknown", "not_comparable"}:
            status = "unknown"
        fact = Fact(fact_id="fact_" + fingerprint([self.evidence.evidence_id, path])[:24],
            evidence_id=self.evidence.evidence_id, path=path, semantic_key=key,
            value=value, unit=unit, value_status=status, observed_at=self.observed_at,
            record_ref=self.record_ref, source_version=self.evidence.source_version,
            allowed_uses=uses or ["display"])
        self.views.append(FactView(fact=fact, label=label, topic=topic, required=required))

    def quality(self, prefix: str = "/data/quality") -> None:
        exists, quality = resolve_pointer(self.evidence.payload, prefix)
        if not exists or not isinstance(quality, dict):
            self.add(prefix + "/overall_status", "quality", "数据质量", required=True)
            return
        for field, label in [("overall_status", "数据质量"), ("trend_eligibility", "趋势比较资格"),
                             ("patient_message", "质量说明"), ("explanation_boundary", "解释范围")]:
            if field in quality:
                self.add(f"{prefix}/{field}", field, label, required=True, topic="quality")
        for index, issue in enumerate(quality.get("issues") or []):
            if isinstance(issue, dict) and issue.get("patient_message"):
                self.add(f"{prefix}/issues/{index}/patient_message", "quality_issue", "数据限制",
                         required=True, topic="quality")

    def metrics(self, blocks: list[dict[str, Any]], prefix: str = "/data/report_blocks") -> None:
        for i, block in enumerate(blocks):
            topic = str(block.get("rehab_content") or "general")
            block_label = str(block.get("display_name") or "训练")
            for j, metric in enumerate(block.get("metrics") or []):
                if not isinstance(metric, dict):
                    continue
                field = "normalized_value" if metric.get("normalized_value") is not None else "raw_value"
                unit = metric.get("unit") if metric.get("unit_status") == "confirmed" else "unknown"
                self.add(f"{prefix}/{i}/metrics/{j}/{field}",
                    str(metric.get("metric_code") or "unknown_metric"),
                    f"{block_label}：{metric.get('display_name') or '指标'}",
                    unit=unit or "unknown", status=str(metric.get("value_status") or "unknown"),
                    topic=topic, uses=["display", "single_session"])
