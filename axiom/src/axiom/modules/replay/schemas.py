"""Wire shapes for the replay service.

Kept separate from the CLI so the 8890 page, the CLI, and any future caller all speak
the same contract.  ``result_id`` + ``step`` and a free-form ``packet`` are mutually
exclusive inputs for different jobs: verifying a reported result vs. probing during a
review.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator


ReplayAction = Literal["verify", "shot", "send"]


class ReplayRequest(BaseModel):
    action: ReplayAction = "verify"
    task_id: str
    result_id: str = ""
    step: int = Field(default=0, ge=0, le=200)
    # Only used by ``send``; a reported result always replays its recorded packet.
    packet: str = ""
    highlights: list[str] = Field(default_factory=list, max_length=20)
    asserts: list[str] = Field(default_factory=list, max_length=20)
    caption: str = ""
    follow_redirects: bool = False
    allow_delete: bool = False
    allow_private: bool = False

    @model_validator(mode="after")
    def require_an_input(self) -> "ReplayRequest":
        if self.result_id:
            if self.step < 1:
                raise ValueError("重放已上报成果时必须指定 step（从 1 开始）")
            return self
        if not self.packet.strip():
            raise ValueError("自由重放需要 packet")
        return self

    @property
    def capture(self) -> bool:
        return self.action in {"shot", "send"}

    @property
    def attach(self) -> bool:
        # A free-form packet is not a step of anything, so it has no step to hang on.
        return self.action == "shot" and bool(self.result_id)


class StepInfo(BaseModel):
    index: int
    title: str
    request: str
    response: str
    proves: str


class ResultInfo(BaseModel):
    id: str
    title: str
    status: str
    target_asset: str
    step_count: int
    steps: list[StepInfo] = Field(default_factory=list)


class TaskInfo(BaseModel):
    id: str
    title: str


class ReplayResponse(BaseModel):
    ok: bool = True
    request_text: str = ""
    step_title: str = ""
    sent: bool = False
    response: dict[str, Any] = Field(default_factory=dict)
    compare: dict[str, Any] = Field(default_factory=dict)
    artifact_id: str = ""
    evidence_id: str = ""
    screenshot_error: str = ""
    image_base64: str = ""
    notes: list[str] = Field(default_factory=list)


class ErrorResponse(BaseModel):
    ok: bool = False
    message: str
    hint: str = ""


__all__ = [
    "ErrorResponse",
    "ReplayAction",
    "ReplayRequest",
    "ReplayResponse",
    "ResultInfo",
    "StepInfo",
    "TaskInfo",
]
