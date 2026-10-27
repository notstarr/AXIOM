"""REST surface for the report evidence hook.

No UI this round: the console renders whatever is attached, and the reviewer (or an
operator with curl) attaches pictures here.  Kept deliberately small — attach, list,
detach — so the eventual UI has nothing to reconcile.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, status
from pydantic import BaseModel, Field

from axiom.api.dependencies import get_container
from axiom.composition import Container
from axiom.modules.reports.evidence import SUBJECT_RESULT, SUBJECT_VULNERABILITY


router = APIRouter(tags=["reports"])


class ReportEvidenceAttach(BaseModel):
    artifact_id: str
    # 0 = 概览图（挂在报告的证据节）；>0 = 挂到「步骤 N」卡片之后。
    step_index: int = Field(default=0, ge=0, le=200)
    caption: str = ""
    created_by: str = "reviewer"


class ReportEvidenceItem(BaseModel):
    artifact_id: str
    step_index: int = 0
    caption: str = ""
    display_order: int = 0
    url: str = ""


def _list(container: Container, task_id: str, subject_type: str, subject_id: str) -> list[ReportEvidenceItem]:
    return [
        ReportEvidenceItem(**item)
        for item in container.report_evidence.list(task_id, subject_type, subject_id)
    ]


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
    return ReportEvidenceItem(
        **container.report_evidence.attach(
            task_id=task_id,
            subject_type=SUBJECT_VULNERABILITY,
            subject_id=vulnerability_id,
            artifact_id=body.artifact_id,
            step_index=body.step_index,
            caption=body.caption,
            created_by=body.created_by,
        )
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
    return ReportEvidenceItem(
        **container.report_evidence.attach(
            task_id=task_id,
            subject_type=SUBJECT_RESULT,
            subject_id=result_id,
            artifact_id=body.artifact_id,
            step_index=body.step_index,
            caption=body.caption,
            created_by=body.created_by,
        )
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
