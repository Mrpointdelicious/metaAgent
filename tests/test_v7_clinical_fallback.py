"""
创建日期：2026-11-25
文件功能：验证免校验模式下的临床业务入参回退、encounter_summary 判空及 AI 分诊手机号兜底。
"""

import asyncio
import time
from types import SimpleNamespace

from langchain_core.messages import AIMessage

from meta_agent.application.context import RunContext
from meta_agent.context.budget import LLMBudget
from meta_agent.contracts import ConversationState, RunRecord
from meta_agent.flows.ai_consultation import diagnosis
from meta_agent.flows.consultation_room.client import ConsultationClient
from meta_agent.orchestration.identity import TrustedScope
from meta_agent.orchestration.resources import ResourceGrant
from tests.helpers.settings import AppTestSettings


def _scope(*, role: str = "clinician", grant: ResourceGrant | None = None) -> TrustedScope:
    return TrustedScope(
        tenant_id="tenant-1",
        end_user_id="u-1",
        patient_identity=None,
        role=role,
        space_id="space-1",
        consultation_id="consult-1" if grant else "",
        resource_grant=grant,
    )


def _ctx(
    *,
    disable_policy: bool,
    grant: ResourceGrant | None = None,
    fallback: dict | None = None,
    flow_inputs: dict | None = None,
    role: str = "clinician",
) -> RunContext:
    settings = AppTestSettings(disable_capability_policy=disable_policy)
    return RunContext(
        query="测试",
        scope=_scope(role=role, grant=grant),
        record=RunRecord(
            run_id="run_test",
            request_id="req_test",
            request_hash="hash_test",
            scope_key="scope_test",
            conversation_id="conv_test",
        ),
        memory=ConversationState(),
        settings=settings,
        repository=SimpleNamespace(),
        emitter=SimpleNamespace(),
        backend=SimpleNamespace(),
        llm_budget=LLMBudget(4),
        tool_limiter=asyncio.Semaphore(4),
        flow_inputs=flow_inputs or {},
        fallback_business_inputs=fallback or {},
    )


def test_business_inputs_fallback_only_when_capability_policy_disabled():
    strict_ctx = _ctx(
        disable_policy=False,
        fallback={"patientphone": "0003h3", "requesterphone": "XWWZYX"},
        flow_inputs={"token": "tok-1"},
    )
    assert strict_ctx.business_inputs == {}

    open_ctx = _ctx(
        disable_policy=True,
        fallback={"patientphone": "0003h3", "requesterphone": "XWWZYX"},
        flow_inputs={"token": "tok-1"},
    )
    assert open_ctx.business_inputs == {
        "patientphone": "0003h3",
        "requesterphone": "XWWZYX",
        "token": "tok-1",
    }


def test_business_inputs_prefers_valid_resource_grant_over_fallback():
    grant = ResourceGrant(
        tenant_id="tenant-1",
        user="u-1",
        role="clinician",
        client_id="primary",
        expires_at=int(time.time()) + 300,
        capabilities=frozenset({"clinical.record"}),
        consultation_id="consult-1",
        patient_ref="p-1",
        patient_phone="13900001111",
        requester_phone="13800002222",
        encounter_summary="患者自述左膝疼痛",
    )
    ctx = _ctx(
        disable_policy=True,
        grant=grant,
        fallback={"patientphone": "0003h3", "requesterphone": "XWWZYX"},
        flow_inputs={"token": "tok-1"},
    )
    assert ctx.business_inputs["patientphone"] == "13900001111"
    assert ctx.business_inputs["requesterphone"] == "13800002222"
    assert ctx.business_inputs["token"] == "tok-1"


def test_encounter_summary_returns_empty_string_when_grant_missing_under_disabled_policy():
    ctx = _ctx(disable_policy=True, grant=None, fallback={"patientphone": "0003h3"})
    client = ConsultationClient(ctx, "clinical.record")
    assert client.encounter_summary() == ""


def test_ai_consultation_diagnosis_falls_back_to_requesterphone(monkeypatch):
    captured: list[dict] = []

    async def fake_login(client, settings):
        return "tok"

    async def fake_get(client, url, *, token, params, step, label):
        captured.append(params)
        return SimpleNamespace(text='{"data": {"PatientDetails": {"PatientName": "五五"}}}')

    class FakeModel:
        async def ainvoke(self, prompt, config=None):
            return AIMessage("你好，五五。能具体说说哪里不舒服吗？")

    monkeypatch.setattr(diagnosis, "_login", fake_login)
    monkeypatch.setattr(diagnosis, "_get", fake_get)

    ctx = _ctx(
        disable_policy=True,
        role="patient",
        fallback={"requesterphone": "0003h3"},
    )
    ctx.settings.consultation_medical_kb = False
    answer = asyncio.run(diagnosis.run_triage(ctx, FakeModel()))
    assert captured == [{"Phone": "0003h3"}]
    assert "五五" in answer
