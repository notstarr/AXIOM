"""REST surface for the report evidence hook.

Two shapes, one contract.  ``POST .../evidence`` hangs an already-uploaded artifact on a
step; ``POST .../evidence/capture`` does the upload and the hang in one multipart call,
which is what a capture-side tool (the step replayer, a proxy, a person with a browser)
uses so it never has to know about artifacts at all.

``kind`` is the load-bearing field: ``packet`` is the step's real response and is
required once per step by the review gate, ``business`` is the effect behind it.
"""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, File, Form, UploadFile, status
from pydantic import BaseModel, Field

from axiom.api.dependencies import get_container
from axiom.composition import Container
from axiom.modules.reports.evidence import KIND_BUSINESS, SUBJECT_RESULT, SUBJECT_VULNERABILITY


router = APIRouter(tags=["reports"])

EvidenceKind = Literal["packet", "business"]


class ReportEvidenceAttach(BaseModel):
    artifact_id: str
    # 0 = 概览图（挂在报告的证据节）；>0 = 挂到「步骤 N」卡片之后。
    step_index: int = Field(default=0, ge=0, le=200)
    # packet = 该步真实回包（审核闸门要求的）；business = 业务效果图。
    kind: EvidenceKind = KIND_BUSINESS
    caption: str = ""
    created_by: str = "reviewer"


class ReportEvidenceItem(BaseModel):
    artifact_id: str
    step_index: int = 0
    kind: str = KIND_BUSINESS
    caption: str = ""
    display_order: int = 0
    url: str = ""


def _list(container: Container, task_id: str, subject_type: str, subject_id: str) -> list[ReportEvidenceItem]:
    return [
        ReportEvidenceItem(**item)
        for item in container.report_evidence.list(task_id, subject_type, subject_id)
    ]


def _attach(
    container: Container,
    *,
    task_id: str,
    subject_type: str,
    subject_id: str,
    body: ReportEvidenceAttach,
) -> ReportEvidenceItem:
    return ReportEvidenceItem(
        **container.report_evidence.attach(
            task_id=task_id,
            subject_type=subject_type,
            subject_id=subject_id,
            artifact_id=body.artifact_id,
            step_index=body.step_index,
            caption=body.caption,
            created_by=body.created_by,
            kind=body.kind,
        )
    )


async def _capture(
    container: Container,
    *,
    task_id: str,
    subject_type: str,
    subject_id: str,
    file: UploadFile,
    step_index: int,
    kind: str,
    caption: str,
    created_by: str,
) -> ReportEvidenceItem:
    return ReportEvidenceItem(
        **container.report_evidence.capture(
            task_id=task_id,
            subject_type=subject_type,
            subject_id=subject_id,
            filename=file.filename or "shot.png",
            content=await file.read(),
            step_index=step_index,
            caption=caption,
            created_by=created_by,
            kind=kind,
        )
    )


@router.get(
    "/tasks/{task_id}/vulnerabilities/{vulnerability_id}/evidence",
    response_model=list[ReportEvidenceItem],
)
def list_vulnerability_evidence(
    task_id: str,
    vulnerability_id: str,
    container: Container = Depends(get_container),
) -> list[ReportEvidenceItem]:
    return _list(container, task_id, SUBJECT_VULNERABILITY, vulnerability_id)


@router.post(
    "/tasks/{task_id}/vulnerabilities/{vulnerability_id}/evidence",
    response_model=ReportEvidenceItem,
    status_code=status.HTTP_201_CREATED,
)
def attach_vulnerability_evidence(
    task_id: str,
    vulnerability_id: str,
    body: ReportEvidenceAttach,
    container: Container = Depends(get_container),
) -> ReportEvidenceItem:
    return _attach(
        container,
        task_id=task_id,
        subject_type=SUBJECT_VULNERABILITY,
        subject_id=vulnerability_id,
        body=body,
    )


@router.post(
    "/tasks/{task_id}/vulnerabilities/{vulnerability_id}/evidence/capture",
    response_model=ReportEvidenceItem,
    status_code=status.HTTP_201_CREATED,
)
async def capture_vulnerability_evidence(
    task_id: str,
    vulnerability_id: str,
    file: UploadFile = File(...),
    step_index: int = Form(0),
    kind: EvidenceKind = Form(KIND_BUSINESS),
    caption: str = Form(""),
    created_by: str = Form("reviewer"),
    container: Container = Depends(get_container),
) -> ReportEvidenceItem:
    return await _capture(
        container,
        task_id=task_id,
        subject_type=SUBJECT_VULNERABILITY,
        subject_id=vulnerability_id,
        file=file,
        step_index=step_index,
        kind=kind,
        caption=caption,
        created_by=created_by,
    )


@router.delete(
    "/tasks/{task_id}/vulnerabilities/{vulnerability_id}/evidence/{artifact_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
def detach_vulnerability_evidence(
    task_id: str,
    vulnerability_id: str,
    artifact_id: str,
    container: Container = Depends(get_container),
) -> None:
    container.report_evidence.detach(
        task_id=task_id,
        subject_type=SUBJECT_VULNERABILITY,
        subject_id=vulnerability_id,
        artifact_id=artifact_id,
    )


@router.get(
    "/tasks/{task_id}/red-team/results/{result_id}/evidence",
    response_model=list[ReportEvidenceItem],
)
def list_result_evidence(
    task_id: str,
    result_id: str,
    container: Container = Depends(get_container),
) -> list[ReportEvidenceItem]:
    return _list(container, task_id, SUBJECT_RESULT, result_id)


@router.post(
    "/tasks/{task_id}/red-team/results/{result_id}/evidence",
    response_model=ReportEvidenceItem,
    status_code=status.HTTP_201_CREATED,
)
def attach_result_evidence(
    task_id: str,
    result_id: str,
    body: ReportEvidenceAttach,
    container: Container = Depends(get_container),
) -> ReportEvidenceItem:
    return _attach(
        container,
        task_id=task_id,
        subject_type=SUBJECT_RESULT,
        subject_id=result_id,
        body=body,
    )


@router.post(
    "/tasks/{task_id}/red-team/results/{result_id}/evidence/capture",
    response_model=ReportEvidenceItem,
    status_code=status.HTTP_201_CREATED,
)
async def capture_result_evidence(
    task_id: str,
    result_id: str,
    file: UploadFile = File(...),
    step_index: int = Form(0),
    kind: EvidenceKind = Form(KIND_BUSINESS),
    caption: str = Form(""),
    created_by: str = Form("reviewer"),
    container: Container = Depends(get_container),
) -> ReportEvidenceItem:
    return await _capture(
        container,
        task_id=task_id,
        subject_type=SUBJECT_RESULT,
        subject_id=result_id,
        file=file,
        step_index=step_index,
        kind=kind,
        caption=caption,
        created_by=created_by,
    )


@router.delete(
    "/tasks/{task_id}/red-team/results/{result_id}/evidence/{artifact_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
def detach_result_evidence(
    task_id: str,
    result_id: str,
    artifact_id: str,
    container: Container = Depends(get_container),
) -> None:
    container.report_evidence.detach(
        task_id=task_id,
        subject_type=SUBJECT_RESULT,
        subject_id=result_id,
        artifact_id=artifact_id,
    )


# ------------------------------------------------------------------------- report views
#
# The console renders whatever ``to_dict`` produces, so the two views below are the
# only shape it needs to know about.  Deliberately separate from the domain endpoints:
# those return the stored record (which the reviewer also reads), these return the
# rendered document (which only humans read).


@router.get("/tasks/{task_id}/vulnerabilities/{vulnerability_id}/report")
def vulnerability_report(
    task_id: str,
    vulnerability_id: str,
    container: Container = Depends(get_container),
) -> dict:
    return container.reports.vulnerability_doc(task_id, vulnerability_id)


@router.get("/tasks/{task_id}/red-team/results/{result_id}/report")
def result_report(
    task_id: str,
    result_id: str,
    container: Container = Depends(get_container),
) -> dict:
    return container.reports.result_doc(task_id, result_id)
