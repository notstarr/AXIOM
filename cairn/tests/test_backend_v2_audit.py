from __future__ import annotations

from sqlalchemy import func, select

from axiom.infrastructure.database.engine import create_sqlite_engine
from axiom.infrastructure.database.metadata import audit_events, metadata
from axiom.modules.audit.schemas import RecordAuditEvent
from axiom.modules.audit.service import AuditService


def test_audit_events_are_append_only_and_filterable(tmp_path) -> None:
    engine = create_sqlite_engine(tmp_path / "axiom-v2.db")
    metadata.create_all(engine)
    service = AuditService(engine)
    first = service.record(
        RecordAuditEvent(
            project_id="prj_001",
            task_id="t_001",
            intent_id="i001",
            worker="worker-a",
            event_type="intent.started",
            message="Intent started",
            payload={"attempt": 1},
        )
    )
    second = service.record(
        RecordAuditEvent(
            project_id="prj_001",
            task_id="t_002",
            event_type="task.stopped",
            message="Task stopped",
        )
    )

    assert service.list(task_id="t_001")[0].payload == {"attempt": 1}
    assert {event.id for event in service.list(project_id="prj_001")} == {first.id, second.id}
    with engine.connect() as conn:
        assert conn.execute(select(func.count()).select_from(audit_events)).scalar_one() == 2
