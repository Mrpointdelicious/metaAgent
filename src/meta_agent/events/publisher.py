"""
创建日期：2026-10-09
文件功能：统一类型化事件发布、回答修订、制品与动作去重和完成事件。
"""

import asyncio

from pydantic import Field

from meta_agent.contracts import (
    ActionPayload,
    AnswerPayload,
    ArtifactPayload,
    ClarificationPayload,
    FailurePayload,
    OutboundEvent,
    OutcomeStatus,
    ProgressPayload,
    StrictModel,
    TextPayload,
    fingerprint,
)
from meta_agent.events import messages
from meta_agent.events.stream import EventEmitter


class AnswerContent(TextPayload):
    fact_ids: list[str] = Field(default_factory=list, max_length=100)
    doc_refs: list[str] = Field(default_factory=list, max_length=20)


class RunOutcome(StrictModel):
    outcome: OutcomeStatus
    goal_statuses: dict[str, OutcomeStatus]
    task_statuses: dict[str, OutcomeStatus]


class RunEventPublisher:
    def __init__(self, emitter: EventEmitter) -> None:
        self.emitter = emitter
        self.lock = asyncio.Lock()
        self.answers: dict[str | None, OutboundEvent] = {}
        self._answer_hashes: dict[str | None, str] = {}
        self._artifacts: set[str] = set()
        self._actions: dict[str, OutboundEvent] = {}
        self._completed: OutboundEvent | None = None
        for event in emitter.record.events:
            self._remember(event)

    def _remember(self, event: OutboundEvent) -> None:
        if event.type == "answer_part":
            content = AnswerContent.model_validate(
                {
                    key: event.payload.get(key, [] if key != "text" else "")
                    for key in ("text", "fact_ids", "doc_refs")
                }
            )
            self.answers[event.goal_id] = event
            self._answer_hashes[event.goal_id] = fingerprint(content.model_dump(mode="json"))
        elif event.type == "artifact_ready":
            self._artifacts.add(event.payload["artifact_ref"])
        elif event.type == "action_ready":
            self._actions[event.payload["action_id"]] = event
        elif event.type == "completed":
            self._completed = event

    async def accepted(self, text: str = messages.ACCEPTED) -> OutboundEvent:
        async with self.lock:
            return await self.emitter.emit("accepted", TextPayload(text=text).model_dump())

    async def progress(
        self,
        stage: str,
        text: str | None = None,
        *,
        task_id: str | None = None,
        goal_id: str | None = None,
    ) -> OutboundEvent:
        payload = ProgressPayload(
            stage=stage, text=text if text is not None else messages.PROGRESS[stage]
        )
        async with self.lock:
            return await self.emitter.emit(
                "progress", payload.model_dump(), task_id=task_id, goal_id=goal_id
            )

    async def answer(
        self, payload: AnswerContent, *, goal_id: str | None = None, task_id: str | None = None
    ) -> OutboundEvent | None:
        async with self.lock:
            digest = fingerprint(payload.model_dump(mode="json"))
            if self._answer_hashes.get(goal_id) == digest:
                return None
            previous = self.answers.get(goal_id)
            answer = AnswerPayload(
                **payload.model_dump(),
                revision=previous.payload["revision"] + 1 if previous else 1,
                replaces=previous.event_id if previous else None,
            )
            event = await self.emitter.emit(
                "answer_part", answer.model_dump(mode="json"), goal_id=goal_id, task_id=task_id
            )
            self._remember(event)
            return event

    async def artifact(
        self, payload: ArtifactPayload, *, task_id: str | None = None, goal_id: str | None = None
    ) -> OutboundEvent | None:
        async with self.lock:
            if payload.artifact_ref in self._artifacts:
                return None
            event = await self.emitter.emit(
                "artifact_ready", payload.model_dump(mode="json"), task_id=task_id, goal_id=goal_id
            )
            self._remember(event)
            return event

    async def action(self, payload: ActionPayload, *, task_id: str, goal_id: str) -> OutboundEvent:
        async with self.lock:
            if payload.action_id in self._actions:
                return self._actions[payload.action_id]
            event = await self.emitter.emit(
                "action_ready", payload.model_dump(mode="json"), task_id=task_id, goal_id=goal_id
            )
            self._remember(event)
            return event

    async def clarification(
        self,
        payload: ClarificationPayload,
        *,
        task_id: str | None = None,
        goal_id: str | None = None,
    ) -> OutboundEvent:
        async with self.lock:
            return await self.emitter.emit(
                "clarification", payload.model_dump(mode="json"), task_id=task_id, goal_id=goal_id
            )

    async def failure(
        self, payload: FailurePayload, *, task_id: str | None = None, goal_id: str | None = None
    ) -> OutboundEvent:
        async with self.lock:
            return await self.emitter.emit(
                "task_failed", payload.model_dump(mode="json"), task_id=task_id, goal_id=goal_id
            )

    async def completed(self, outcome: RunOutcome) -> OutboundEvent:
        async with self.lock:
            if self._completed:
                return self._completed
            event = await self.emitter.emit("completed", outcome.model_dump(mode="json"))
            self._remember(event)
            return event
