"""Orchestrate one replay: fetch the recorded packet, send it, compare, capture.

The only trusted inputs are what the worker recorded at report time and what the
target answers now.  Callers can choose *which* step to replay and what to
highlight; they cannot substitute the packet text for a recorded step, which is
what keeps a screenshot from becoming a claim the target never supported.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import Engine, select

from axiom.modules.artifacts.service import ArtifactService
from axiom.modules.redteam.service import RedTeamService
# Import the submodules by their own names: the package ``__init__`` re-exports a
# *function* called ``compare``, and ``from ... import compare`` would bind that
# function over the module we need here.
import axiom.modules.replay.compare as compare_module
import axiom.modules.replay.render as render_module
import axiom.modules.replay.sender as sender_module
from axiom.modules.replay.packet import PacketError, parse_request, parse_response
from axiom.modules.reports import KIND_PACKET, ReportEvidenceService
from axiom.modules.tasks.service import TaskService


class ReplayError(ValueError):
    """A caller-fixable problem: bad step, missing reproduction, blocked target."""

    def __init__(self, message: str, *, hint: str = "", status: int = 400):
        super().__init__(message)
        self.message = message
        self.hint = hint
        self.status = status


@dataclass
class StepView:
    index: int
    title: str
    request: str
    response: str
    proves: str


@dataclass
class ReplayOutcome:
    result_id: str
    step: int
    step_title: str
    sent: bool
    request_text: str
    live: sender_module.SendResult
    comparison: compare_module.Comparison
    artifact_id: str = ""
    evidence_id: str = ""
    image_base64: str = ""
    screenshot_error: str = ""
    notes: list[str] = field(default_factory=list)

    def to_payload(self) -> dict[str, Any]:
        return {
            "result_id": self.result_id,
            "step": self.step,
            "step_title": self.step_title,
            "sent": self.sent,
            "request_text": self.request_text,
            "response": self.live.to_payload(),
            "compare": self.comparison.to_payload(),
            "artifact_id": self.artifact_id,
            "evidence_id": self.evidence_id,
            "screenshot_error": self.screenshot_error,
            "notes": self.notes,
        }


class ReplayService:
    def __init__(
        self,
        engine: Engine,
        *,
        tasks: TaskService,
        red_team: RedTeamService,
        evidence: ReportEvidenceService,
    ):
        self.engine = engine
        self.tasks = tasks
        self.red_team = red_team
        self.evidence = evidence

    # ── reading ──────────────────────────────────────────────────────────────

    def steps(self, task_id: str, result_id: str) -> list[StepView]:
        result = self._result(task_id, result_id)
        reproduction = result.reproduction
        if reproduction is None or not reproduction.steps:
            raise ReplayError(
                "该成果没有结构化复现，无法重放",
                hint="只有带 reproduction 的成果可以复核；请让 Explore 重新上报",
            )
        return [
            StepView(index=index, title=step.title, request=step.request,
                     response=step.response, proves=step.proves)
            for index, step in enumerate(reproduction.steps, start=1)
        ]

    def _result(self, task_id: str, result_id: str):
        results = self.red_team.list_results(task_id)
        for item in results:
            if item.id == result_id:
                return item
        raise ReplayError(f"找不到成果 {result_id}", status=404)

    # ── executing ────────────────────────────────────────────────────────────

    def replay(
        self,
        *,
        task_id: str,
        result_id: str = "",
        step: int = 0,
        packet_text: str = "",
        highlights: list[str] | None = None,
        asserts: list[str] | None = None,
        caption: str = "",
        follow_redirects: bool = False,
        allow_delete: bool = False,
        allow_private: bool = False,
        capture: bool = True,
        attach: bool = False,
        actor: str = "axiom.replay",
    ) -> ReplayOutcome:
        """Send once.  ``result_id`` pins the packet to what was reported."""
        if result_id:
            if step < 1:
                raise ReplayError("重放已上报成果时必须指定 --step（从 1 开始）")
            views = self.steps(task_id, result_id)
            if step > len(views):
                raise ReplayError(
                    f"step {step} 越界，该成果只有 {len(views)} 步",
                    hint=f"有效范围 1..{len(views)}",
                )
            recorded = views[step - 1]
            request_text = recorded.request
            title = f"{result_id} · 第 {step} 步 · {recorded.title}"
        else:
            request_text = (packet_text or "").strip()
            if not request_text:
                raise ReplayError("自由重放需要提供报文（--packet-file 或 stdin）")
            recorded = None
            title = "自由重放"
            step = step or 0

        try:
            packet = parse_request(request_text)
        except PacketError as exc:
            raise ReplayError(str(exc)) from exc

        scope = self._scope(task_id)
        try:
            live = sender_module.send(
                packet,
                scope=scope,
                follow_redirects=follow_redirects,
                allow_delete=allow_delete,
                allow_private=allow_private,
            )
        except sender_module.ReplayBlocked as exc:
            raise ReplayError(exc.reason, hint=exc.hint, status=403) from exc

        recorded_status: int | None = None
        recorded_body = ""
        recorded_content_type = ""
        if recorded is not None:
            try:
                parsed = parse_response(recorded.response)
            except PacketError:
                parsed = None
            if parsed is not None:
                recorded_status = parsed.status_code
                recorded_body = parsed.body
                recorded_content_type = parsed.content_type()

        comparison = compare_module.compare(
            recorded_body,
            recorded_status,
            recorded_content_type,
            live,
            extra_asserts=asserts or [],
        )

        outcome = ReplayOutcome(
            result_id=result_id,
            step=step,
            step_title=recorded.title if recorded is not None else "",
            sent=live.sent,
            request_text=packet.to_text(),
            live=live,
            comparison=comparison,
        )

        if capture and live.sent:
            self._capture(
                outcome,
                title=title,
                caption=caption,
                highlights=highlights or [],
                task_id=task_id,
                attach=attach and bool(result_id),
                actor=actor,
            )
        return outcome

    def _capture(
        self,
        outcome: ReplayOutcome,
        *,
        title: str,
        caption: str,
        highlights: list[str],
        task_id: str,
        attach: bool,
        actor: str,
    ) -> None:
        payload = render_module.render_input_for(
            packet_text=outcome.request_text,
            live=outcome.live,
            comparison=outcome.comparison,
            title=title,
            caption=caption,
            highlights=highlights,
            source_label=self._source_label(),
        )
        try:
            png = render_module.screenshot(
                payload,
                chromium=self._chromium_setting(),
            )
        except render_module.RenderError as exc:
            # The replay itself succeeded; the picture is a nice-to-have.
            outcome.screenshot_error = str(exc)
            return

        import base64

        outcome.image_base64 = base64.b64encode(png).decode("ascii")
        if not attach:
            return

        digest = hashlib.sha256(png).hexdigest()[:8]
        filename = f"replay-{outcome.result_id}-s{outcome.step}-{digest}.png"
        # ``kind=packet`` matters: the review gate counts only packet shots as proof
        # that a step's response was actually seen, and a replay *is* that proof.
        record = self.evidence.capture(
            task_id=task_id,
            subject_type="redteam_result",
            subject_id=outcome.result_id,
            filename=filename,
            content=png,
            step_index=outcome.step,
            caption=caption or f"{outcome.result_id} 第 {outcome.step} 步重放截图",
            created_by=actor,
            kind=KIND_PACKET,
        )
        outcome.artifact_id = str(record.get("artifact_id") or "")
        outcome.evidence_id = outcome.artifact_id

    # ── helpers ──────────────────────────────────────────────────────────────

    def _scope(self, task_id: str) -> dict[str, Any] | None:
        task = self.tasks.get(task_id)
        if task is None:
            raise ReplayError(f"找不到任务 {task_id}", status=404)
        scope = task.scope.model_dump(mode="json") if task.scope is not None else None
        if not scope or not scope.get("enabled", False):
            return None
        return scope

    @staticmethod
    def _chromium_setting() -> str:
        import os

        return os.environ.get("AXIOM_REPLAY_CHROME", "").strip()

    @staticmethod
    def _source_label() -> str:
        import os
        import socket

        host = os.environ.get("AXIOM_REPLAY_SOURCE_LABEL", "").strip()
        if host:
            return host
        try:
            address = socket.gethostbyname(socket.gethostname())
        except OSError:
            address = ""
        return f"{address} · axiom-replay" if address else "axiom-replay"


__all__ = ["ReplayError", "ReplayOutcome", "ReplayService", "StepView"]
