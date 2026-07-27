from __future__ import annotations

import json

from sqlalchemy import Connection, func, select, update

from axiom.modules.external_reports.schemas import ExternalReportAttempt
from axiom.modules.external_reports.tables import external_report_attempts
from axiom.shared.ids import allocate_id


class ExternalReportRepository:
    def next_attempt_no(self, conn: Connection, *, task_id: str, vulnerability_id: str) -> int:
        current = conn.execute(
            select(func.max(external_report_attempts.c.attempt_no)).where(
                external_report_attempts.c.task_id == task_id,
                external_report_attempts.c.vulnerability_id == vulnerability_id,
            )
        ).scalar_one()
        return int(current or 0) + 1

    def create_running(
        self,
        conn: Connection,
        *,
        task_id: str,
        vulnerability_id: str,
        attempt_no: int,
        endpoint: str,
        payload: dict,
        now: str,
    ) -> ExternalReportAttempt:
        values = {
            "id": allocate_id(conn, scope=task_id, kind="external_report", prefix="er"),
            "task_id": task_id,
            "vulnerability_id": vulnerability_id,
            "attempt_no": attempt_no,
            "status": "running",
            "endpoint": endpoint,
            "payload_json": json.dumps(payload, ensure_ascii=False),
            "response_status": None,
            "response_body": "",
            "error": "",
            "created_at": now,
            "updated_at": now,
            "completed_at": None,
        }
        conn.execute(external_report_attempts.insert().values(**values))
        return self._model(values)

    def get(self, conn: Connection, attempt_id: str) -> ExternalReportAttempt | None:
        row = conn.execute(
            select(external_report_attempts).where(external_report_attempts.c.id == attempt_id)
        ).mappings().one_or_none()
        return self._model(row) if row is not None else None

    def list(self, conn: Connection, *, task_id: str, vulnerability_id: str) -> list[ExternalReportAttempt]:
        rows = conn.execute(
            select(external_report_attempts)
            .where(
                external_report_attempts.c.task_id == task_id,
                external_report_attempts.c.vulnerability_id == vulnerability_id,
            )
            .order_by(external_report_attempts.c.attempt_no.desc())
        ).mappings()
        return [self._model(row) for row in rows]

    def finish(
        self,
        conn: Connection,
        *,
        attempt_id: str,
        status: str,
        response_status: int | None,
        response_body: str,
        error: str,
        now: str,
    ) -> ExternalReportAttempt | None:
        result = conn.execute(
            update(external_report_attempts)
            .where(
                external_report_attempts.c.id == attempt_id,
                external_report_attempts.c.status == "running",
            )
            .values(
                status=status,
                response_status=response_status,
                response_body=response_body,
                error=error,
                updated_at=now,
                completed_at=now,
            )
        )
        return self.get(conn, attempt_id) if result.rowcount else None

    @staticmethod
    def _model(row) -> ExternalReportAttempt:
        return ExternalReportAttempt(
            id=row["id"],
            task_id=row["task_id"],
            vulnerability_id=row["vulnerability_id"],
            attempt_no=row["attempt_no"],
            status=row["status"],
            endpoint=row["endpoint"],
            payload=json.loads(row["payload_json"]),
            response_status=row["response_status"],
            response_body=row["response_body"],
            error=row["error"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            completed_at=row["completed_at"],
        )
