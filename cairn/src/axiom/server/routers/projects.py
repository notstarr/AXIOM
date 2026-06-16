import json
import os as _os
import uuid

from fastapi import APIRouter, HTTPException, Query, UploadFile

from axiom.server.db import get_conn
from axiom.server.models import (
    BlackboardItem,
    BlackboardBatchStatusUpdateRequest,
    CompleteRequest,
    CreateBlackboardItemRequest,
    CreateProjectGroupRequest,
    CreateProjectRequest,
    CreateVulnerabilityRequest,
    Fact,
    Hint,
    HeartbeatRequest,
    Intent,
    ProjectDetail,
    ProjectGroupSummary,
    ProjectMeta,
    ProjectSummary,
    TaskEvent,
    TaskEventCreateRequest,
    ReopenRequest,
    ReopenResponse,
    ReasonClaimRequest,
    UpdateProjectGroupRequest,
    UpdateProjectTitleRequest,
    UpdateProjectStatusRequest,
    UpdateBlackboardItemRequest,
    Vulnerability,
)
from axiom.server.services import (
    build_blackboard_items,
    build_blackboard_items_for_group,
    build_vulns,
    build_intents,
    check_project_completed,
    check_project_active,
    check_project_hint_writable,
    clear_project_reason,
    expire_reason_leases,
    expire_workers,
    get_completion_intent_or_409,
    get_project_group_or_404,
    get_project_or_404,
    intent_to_model,
    next_fact_id,
    next_hint_id,
    next_intent_id,
    next_project_id,
    next_blackboard_item_id,
    next_blackboard_item_id_for_group,
    next_project_group_id,
    next_vuln_id,
    project_meta_from_row,
    project_reason_from_row,
    utcnow,
    validate_facts_exist,
    validate_goal_not_in_sources,
)

router = APIRouter(tags=["projects"])


def _parse_scope(raw: str | None):
    from axiom.server.models import ProjectScope

    if not raw:
        return ProjectScope()
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return ProjectScope()
    if not isinstance(payload, dict):
        return ProjectScope()
    return ProjectScope.model_validate(payload)


def _project_group_summary_from_row(row) -> ProjectGroupSummary:
    return ProjectGroupSummary(
        id=row["id"],
        title=row["title"],
        description=row["description"],
        scope=_parse_scope(row["scope_json"]),
        status=row["status"],
        created_at=row["created_at"],
        task_count=row["task_count"],
        active_task_count=row["active_task_count"],
        stopped_task_count=row["stopped_task_count"],
        completed_task_count=row["completed_task_count"],
        vuln_count=row["vuln_count"],
        blackboard_count=row["blackboard_count"],
    )


def _default_project_group_id(conn) -> str:
    row = conn.execute("SELECT id FROM project_groups ORDER BY created_at, id LIMIT 1").fetchone()
    if row is not None:
        return row["id"]
    group_id = next_project_group_id(conn)
    now = utcnow()
    conn.execute(
        """
        INSERT INTO project_groups (id, title, description, status, created_at)
        VALUES (?, '默认项目', '自动创建的默认项目', 'active', ?)
        """,
        (group_id, now),
    )
    return group_id


@router.get("/project-groups", response_model=list[ProjectGroupSummary])
def list_project_groups():
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT g.*,
                (SELECT COUNT(*) FROM projects p WHERE p.parent_project_id = g.id) AS task_count,
                (SELECT COUNT(*) FROM projects p WHERE p.parent_project_id = g.id AND p.status = 'active') AS active_task_count,
                (SELECT COUNT(*) FROM projects p WHERE p.parent_project_id = g.id AND p.status = 'stopped') AS stopped_task_count,
                (SELECT COUNT(*) FROM projects p WHERE p.parent_project_id = g.id AND p.status = 'completed') AS completed_task_count,
                (SELECT COUNT(*) FROM vulns v JOIN projects p ON p.id = v.project_id WHERE p.parent_project_id = g.id) AS vuln_count,
                (SELECT COUNT(*) FROM blackboard_items b WHERE b.group_id = g.id) AS blackboard_count
            FROM project_groups g
            ORDER BY g.created_at, g.id
            """
        ).fetchall()
        return [
            _project_group_summary_from_row(row)
            for row in rows
        ]


@router.post("/project-groups", response_model=ProjectGroupSummary, status_code=201)
def create_project_group(body: CreateProjectGroupRequest):
    with get_conn() as conn:
        group_id = next_project_group_id(conn)
        now = utcnow()
        conn.execute(
            """
            INSERT INTO project_groups (id, title, description, scope_json, status, created_at)
            VALUES (?, ?, ?, ?, 'active', ?)
            """,
            (
                group_id,
                body.title,
                body.description,
                body.scope.model_dump_json(),
                now,
            ),
        )
        row = conn.execute(
            """
            SELECT g.*,
                0 AS task_count,
                0 AS active_task_count,
                0 AS stopped_task_count,
                0 AS completed_task_count,
                0 AS vuln_count,
                0 AS blackboard_count
            FROM project_groups g
            WHERE g.id = ?
            """,
            (group_id,),
        ).fetchone()
        assert row is not None
        return _project_group_summary_from_row(row)


@router.put("/project-groups/{group_id}", response_model=ProjectGroupSummary)
def update_project_group(group_id: str, body: UpdateProjectGroupRequest):
    with get_conn() as conn:
        get_project_group_or_404(conn, group_id)
        conn.execute(
            "UPDATE project_groups SET title = ?, description = ?, scope_json = ?, status = ? WHERE id = ?",
            (body.title, body.description, body.scope.model_dump_json(), body.status, group_id),
        )
        row = conn.execute(
            """
            SELECT g.*,
                (SELECT COUNT(*) FROM projects p WHERE p.parent_project_id = g.id) AS task_count,
                (SELECT COUNT(*) FROM projects p WHERE p.parent_project_id = g.id AND p.status = 'active') AS active_task_count,
                (SELECT COUNT(*) FROM projects p WHERE p.parent_project_id = g.id AND p.status = 'stopped') AS stopped_task_count,
                (SELECT COUNT(*) FROM projects p WHERE p.parent_project_id = g.id AND p.status = 'completed') AS completed_task_count,
                (SELECT COUNT(*) FROM vulns v JOIN projects p ON p.id = v.project_id WHERE p.parent_project_id = g.id) AS vuln_count,
                (SELECT COUNT(*) FROM blackboard_items b WHERE b.group_id = g.id) AS blackboard_count
            FROM project_groups g
            WHERE g.id = ?
            """,
            (group_id,),
        ).fetchone()
        assert row is not None
        return _project_group_summary_from_row(row)


@router.get("/projects", response_model=list[ProjectSummary])
def list_projects():
    with get_conn() as conn:
        expire_workers(conn)
        expire_reason_leases(conn)
        rows = conn.execute("""
            SELECT p.*,
                (SELECT COUNT(*) FROM facts WHERE project_id = p.id) AS fact_count,
                (SELECT COUNT(*) FROM intents WHERE project_id = p.id) AS intent_count,
                (SELECT COUNT(*) FROM intents WHERE project_id = p.id AND concluded_at IS NULL AND worker IS NOT NULL) AS working_intent_count,
                (SELECT COUNT(*) FROM intents WHERE project_id = p.id AND concluded_at IS NULL AND worker IS NULL) AS unclaimed_intent_count,
                (SELECT COUNT(*) FROM hints WHERE project_id = p.id) AS hint_count
            FROM projects p
            ORDER BY p.created_at
        """).fetchall()
        return [
            ProjectSummary(
                id=row["id"],
                parent_project_id=row["parent_project_id"],
                title=row["title"],
                status=row["status"],
                bootstrap_enabled=bool(row["bootstrap_enabled"]),
                created_at=row["created_at"],
                reason=project_reason_from_row(row),
                fact_count=row["fact_count"],
                intent_count=row["intent_count"],
                working_intent_count=row["working_intent_count"],
                unclaimed_intent_count=row["unclaimed_intent_count"],
                hint_count=row["hint_count"],
            )
            for row in rows
        ]


@router.post("/projects", response_model=ProjectDetail, status_code=201)
def create_project(body: CreateProjectRequest):
    with get_conn() as conn:
        pid = next_project_id(conn)
        now = utcnow()
        parent_project_id = body.parent_project_id or _default_project_group_id(conn)
        get_project_group_or_404(conn, parent_project_id)

        conn.execute(
            "INSERT INTO projects (id, parent_project_id, title, status, bootstrap_enabled, depth_mode, created_at) VALUES (?, ?, ?, 'active', ?, ?, ?)",
            (pid, parent_project_id, body.title, body.bootstrap_enabled, 1 if body.depth_mode else 0, now),
        )
        conn.execute(
            "INSERT INTO facts (id, project_id, description) VALUES (?, ?, ?)",
            ("origin", pid, body.origin),
        )
        conn.execute(
            "INSERT INTO facts (id, project_id, description) VALUES (?, ?, ?)",
            ("goal", pid, body.goal),
        )

        hints = []
        if body.hints:
            for h in body.hints:
                hid = next_hint_id(conn, pid)
                conn.execute(
                    "INSERT INTO hints (id, project_id, content, creator, created_at) VALUES (?, ?, ?, ?, ?)",
                    (hid, pid, h.content, h.creator, now),
                )
                hints.append(Hint(id=hid, content=h.content, creator=h.creator, created_at=now))

        return ProjectDetail(
            project=ProjectMeta(
                id=pid,
                parent_project_id=parent_project_id,
                title=body.title,
                status="active",
                bootstrap_enabled=body.bootstrap_enabled,
                depth_mode=body.depth_mode,
                created_at=now,
                reason=None,
            ),
            facts=[
                Fact(id="origin", description=body.origin),
                Fact(id="goal", description=body.goal),
            ],
            intents=[],
            hints=hints,
            blackboard=[],
        )


@router.get("/projects/{project_id}", response_model=ProjectDetail)
def get_project(project_id: str):
    with get_conn() as conn:
        expire_workers(conn, project_id)
        expire_reason_leases(conn, project_id)
        row = get_project_or_404(conn, project_id)

        facts = conn.execute(
            "SELECT * FROM facts WHERE project_id = ?", (project_id,)
        ).fetchall()
        hints = conn.execute(
            "SELECT * FROM hints WHERE project_id = ? ORDER BY created_at",
            (project_id,),
        ).fetchall()

        group_id = row["parent_project_id"] or ""
        return ProjectDetail(
            project=project_meta_from_row(row),
            facts=[Fact(**dict(f)) for f in facts],
            intents=build_intents(conn, project_id),
            hints=[Hint(**dict(h)) for h in hints],
            vulns=build_vulns(conn, project_id),
            blackboard=build_blackboard_items_for_group(conn, group_id) if group_id else [],
        )


@router.get("/projects/{project_id}/events", response_model=list[TaskEvent])
def list_project_events(project_id: str, after_id: int = Query(default=0, ge=0), limit: int = Query(default=200, ge=1, le=1000)):
    with get_conn() as conn:
        get_project_or_404(conn, project_id)
        rows = conn.execute(
            """
            SELECT id, project_id, task_type, phase, worker, level, message, payload_json, created_at
            FROM task_events
            WHERE project_id = ? AND id > ?
            ORDER BY id ASC
            LIMIT ?
            """,
            (project_id, after_id, limit),
        ).fetchall()
        events: list[TaskEvent] = []
        for row in rows:
            payload = {}
            raw_payload = row["payload_json"] or "{}"
            try:
                parsed = json.loads(raw_payload)
                if isinstance(parsed, dict):
                    payload = parsed
            except json.JSONDecodeError:
                payload = {"raw_payload": raw_payload}
            events.append(
                TaskEvent(
                    id=row["id"],
                    project_id=row["project_id"],
                    task_type=row["task_type"],
                    phase=row["phase"],
                    worker=row["worker"],
                    level=row["level"],
                    message=row["message"],
                    payload=payload,
                    created_at=row["created_at"],
                )
            )
        return events


@router.post("/projects/{project_id}/events", response_model=TaskEvent, status_code=201)
def create_project_event(project_id: str, body: TaskEventCreateRequest):
    with get_conn() as conn:
        get_project_or_404(conn, project_id)
        now = utcnow()
        payload_json = json.dumps(body.payload, ensure_ascii=False, separators=(",", ":"))
        cur = conn.execute(
            """
            INSERT INTO task_events (project_id, task_type, phase, worker, level, message, payload_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                project_id,
                body.task_type,
                body.phase,
                body.worker,
                body.level,
                body.message,
                payload_json,
                now,
            ),
        )
        event_id = int(cur.lastrowid)
        return TaskEvent(
            id=event_id,
            project_id=project_id,
            task_type=body.task_type,
            phase=body.phase,
            worker=body.worker,
            level=body.level,
            message=body.message,
            payload=body.payload,
            created_at=now,
        )


@router.get("/projects/{project_id}/vulns", response_model=list[Vulnerability])
def list_project_vulns(project_id: str):
    with get_conn() as conn:
        get_project_or_404(conn, project_id)
        return build_vulns(conn, project_id)


@router.post("/projects/{project_id}/vulns", response_model=Vulnerability, status_code=201)
def create_project_vuln(project_id: str, body: CreateVulnerabilityRequest):
    with get_conn() as conn:
        now = utcnow()
        vuln_id = next_vuln_id(conn, project_id)
        evidence_json = json.dumps(body.evidence, ensure_ascii=False, separators=(",", ":"))
        steps_json = json.dumps(body.reproduction_steps, ensure_ascii=False, separators=(",", ":"))
        conn.execute(
            """
            INSERT INTO vulns (
                id, project_id, title, description, prerequisites, impact,
                evidence_json, reproduction_steps_json,
                status, source, reported_by, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                vuln_id,
                project_id,
                body.title,
                body.description,
                body.prerequisites,
                body.impact,
                evidence_json,
                steps_json,
                body.status,
                body.source,
                body.reported_by,
                now,
            ),
        )
        return Vulnerability(
            id=vuln_id,
            title=body.title,
            description=body.description,
            prerequisites=body.prerequisites,
            impact=body.impact,
            evidence=body.evidence,
            reproduction_steps=body.reproduction_steps,
            status=body.status,
            source=body.source,
            reported_by=body.reported_by,
            created_at=now,
        )


@router.put("/projects/{project_id}/vulns/{vuln_id}", response_model=Vulnerability)
def update_project_vuln(project_id: str, vuln_id: str, body: CreateVulnerabilityRequest):
    now = utcnow()
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM vulns WHERE id = ? AND project_id = ?", (vuln_id, project_id)).fetchone()
        if not row:
            raise HTTPException(404, "Vulnerability not found")
        evidence_json = json.dumps(body.evidence, ensure_ascii=False, separators=(",", ":"))
        steps_json = json.dumps(body.reproduction_steps, ensure_ascii=False, separators=(",", ":"))
        conn.execute("""
            UPDATE vulns SET title=?, description=?, prerequisites=?, impact=?,
            evidence_json=?, reproduction_steps_json=?, status=?
            WHERE id=? AND project_id=?
        """, (body.title, body.description, body.prerequisites, body.impact,
              evidence_json, steps_json, body.status,
              vuln_id, project_id))
    return Vulnerability(id=vuln_id, title=body.title, description=body.description, prerequisites=body.prerequisites,
                         impact=body.impact, evidence=body.evidence,
                         reproduction_steps=body.reproduction_steps, status=body.status, source=body.source,
                         reported_by=body.reported_by, created_at=row["created_at"])


@router.delete("/projects/{project_id}/vulns/{vuln_id}", status_code=204)
def delete_project_vuln(project_id: str, vuln_id: str):
    with get_conn() as conn:
        row = conn.execute("SELECT 1 FROM vulns WHERE id = ? AND project_id = ?", (vuln_id, project_id)).fetchone()
        if not row:
            raise HTTPException(404, "Vulnerability not found")
        conn.execute("DELETE FROM vulns WHERE id = ? AND project_id = ?", (vuln_id, project_id))


# --- Group-level blackboard ---


@router.get("/project-groups/{group_id}/blackboard", response_model=list[BlackboardItem])
def list_group_blackboard_items(group_id: str):
    with get_conn() as conn:
        get_project_group_or_404(conn, group_id)
        return build_blackboard_items_for_group(conn, group_id)


@router.post("/project-groups/{group_id}/blackboard", response_model=BlackboardItem, status_code=201)
def create_group_blackboard_item(group_id: str, body: CreateBlackboardItemRequest):
    with get_conn() as conn:
        get_project_group_or_404(conn, group_id)
        item_id = next_blackboard_item_id_for_group(conn, group_id)
        now = utcnow()
        dummy_pid = conn.execute(
            "SELECT id FROM projects WHERE parent_project_id = ? LIMIT 1",
            (group_id,),
        ).fetchone()
        if dummy_pid is None:
            dummy_pid = conn.execute("SELECT id FROM projects LIMIT 1").fetchone()
        fallback_pid = dummy_pid["id"] if dummy_pid else "proj_000"
        conn.execute(
            "INSERT INTO blackboard_items (id, project_id, group_id, title, content, hint, category, status, priority,"
            " related_fact_id, related_intent_id, related_vuln_id, creator, updated_by, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (item_id, fallback_pid, group_id, body.title, body.content, body.hint,
             body.category, body.status, body.priority,
             body.related_fact_id, body.related_intent_id, body.related_vuln_id,
             body.creator, body.creator, now, now),
        )
        return BlackboardItem(
            id=item_id, title=body.title, content=body.content, hint=body.hint,
            category=body.category, status=body.status, priority=body.priority,
            related_fact_id=body.related_fact_id, related_intent_id=body.related_intent_id,
            related_vuln_id=body.related_vuln_id, creator=body.creator, updated_by=body.creator,
            created_at=now, updated_at=now,
        )


@router.put("/project-groups/{group_id}/blackboard/{item_id}", response_model=BlackboardItem)
def update_group_blackboard_item(group_id: str, item_id: str, body: UpdateBlackboardItemRequest):
    with get_conn() as conn:
        get_project_group_or_404(conn, group_id)
        row = conn.execute(
            "SELECT * FROM blackboard_items WHERE id = ? AND group_id = ?",
            (item_id, group_id),
        ).fetchone()
        if row is None:
            raise HTTPException(404, "Blackboard item not found")
        now = utcnow()
        conn.execute(
            "UPDATE blackboard_items SET title = ?, content = ?, hint = ?, category = ?, status = ?, priority = ?,"
            " related_fact_id = ?, related_intent_id = ?, related_vuln_id = ?, updated_by = ?, updated_at = ?"
            " WHERE id = ? AND group_id = ?",
            (body.title, body.content, body.hint, body.category, body.status, body.priority,
             body.related_fact_id, body.related_intent_id, body.related_vuln_id,
             body.updated_by, now, item_id, group_id),
        )
        return BlackboardItem(
            id=item_id, title=body.title, content=body.content, hint=body.hint,
            category=body.category, status=body.status, priority=body.priority,
            related_fact_id=body.related_fact_id, related_intent_id=body.related_intent_id,
            related_vuln_id=body.related_vuln_id, creator=row["creator"], updated_by=body.updated_by,
            created_at=row["created_at"], updated_at=now,
        )


@router.delete("/project-groups/{group_id}/blackboard/{item_id}", status_code=204)
def delete_group_blackboard_item(group_id: str, item_id: str):
    with get_conn() as conn:
        get_project_group_or_404(conn, group_id)
        conn.execute(
            "DELETE FROM blackboard_items WHERE id = ? AND group_id = ?",
            (item_id, group_id),
        )


# --- Task-level blackboard (legacy) ---

@router.get("/projects/{project_id}/blackboard", response_model=list[BlackboardItem])
def list_project_blackboard_items(project_id: str):
    with get_conn() as conn:
        get_project_or_404(conn, project_id)
        return build_blackboard_items(conn, project_id)


@router.post("/projects/{project_id}/blackboard", response_model=BlackboardItem, status_code=201)
def create_project_blackboard_item(project_id: str, body: CreateBlackboardItemRequest):
    with get_conn() as conn:
        check_project_hint_writable(conn, project_id)
        now = utcnow()
        item_id = next_blackboard_item_id(conn, project_id)
        conn.execute(
            """
            INSERT INTO blackboard_items (
                id, project_id, title, content, hint, category, status, priority,
                related_fact_id, related_intent_id, related_vuln_id,
                creator, updated_by, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                item_id,
                project_id,
                body.title,
                body.content,
                body.hint,
                body.category,
                body.status,
                body.priority,
                body.related_fact_id,
                body.related_intent_id,
                body.related_vuln_id,
                body.creator,
                body.creator,
                now,
                now,
            ),
        )
        return BlackboardItem(
            id=item_id,
            title=body.title,
            content=body.content,
            hint=body.hint,
            category=body.category,
            status=body.status,
            priority=body.priority,
            related_fact_id=body.related_fact_id,
            related_intent_id=body.related_intent_id,
            related_vuln_id=body.related_vuln_id,
            creator=body.creator,
            updated_by=body.creator,
            created_at=now,
            updated_at=now,
        )


@router.put("/projects/{project_id}/blackboard/{item_id}", response_model=BlackboardItem)
def update_project_blackboard_item(project_id: str, item_id: str, body: UpdateBlackboardItemRequest):
    with get_conn() as conn:
        check_project_hint_writable(conn, project_id)
        now = utcnow()
        updated = conn.execute(
            """
            UPDATE blackboard_items
            SET title = ?,
                content = ?,
                hint = ?,
                category = ?,
                status = ?,
                priority = ?,
                related_fact_id = ?,
                related_intent_id = ?,
                related_vuln_id = ?,
                updated_by = ?,
                updated_at = ?
            WHERE id = ? AND project_id = ?
            """,
            (
                body.title,
                body.content,
                body.hint,
                body.category,
                body.status,
                body.priority,
                body.related_fact_id,
                body.related_intent_id,
                body.related_vuln_id,
                body.updated_by,
                now,
                item_id,
                project_id,
            ),
        )
        if updated.rowcount == 0:
            raise HTTPException(404, "Blackboard item not found")
        row = conn.execute(
            "SELECT * FROM blackboard_items WHERE id = ? AND project_id = ?",
            (item_id, project_id),
        ).fetchone()
        assert row is not None
        return BlackboardItem(
            id=row["id"],
            title=row["title"],
            content=row["content"],
            category=row["category"],
            status=row["status"],
            priority=row["priority"] or "medium",
            related_fact_id=row["related_fact_id"],
            related_intent_id=row["related_intent_id"],
            related_vuln_id=row["related_vuln_id"],
            creator=row["creator"],
            updated_by=row["updated_by"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )


@router.post("/projects/{project_id}/blackboard/batch/status", response_model=list[BlackboardItem])
def batch_update_blackboard_item_status(project_id: str, body: BlackboardBatchStatusUpdateRequest):
    with get_conn() as conn:
        check_project_hint_writable(conn, project_id)
        now = utcnow()
        for item_id in body.ids:
            conn.execute(
                """
                UPDATE blackboard_items
                SET status = ?,
                    updated_by = ?,
                    updated_at = ?
                WHERE id = ? AND project_id = ?
                """,
                (body.status, body.updated_by, now, item_id, project_id),
            )
        return build_blackboard_items(conn, project_id)


@router.delete("/projects/{project_id}/blackboard/{item_id}", status_code=204)
def delete_project_blackboard_item(project_id: str, item_id: str):
    with get_conn() as conn:
        check_project_hint_writable(conn, project_id)
        deleted = conn.execute(
            "DELETE FROM blackboard_items WHERE id = ? AND project_id = ?",
            (item_id, project_id),
        )
        if deleted.rowcount == 0:
            raise HTTPException(404, "Blackboard item not found")


@router.delete("/projects/{project_id}", status_code=204)
def delete_project(project_id: str):
    with get_conn() as conn:
        get_project_or_404(conn, project_id)
        conn.execute("DELETE FROM projects WHERE id = ?", (project_id,))


@router.put("/projects/{project_id}/title", response_model=ProjectMeta)
def update_project_title(project_id: str, body: UpdateProjectTitleRequest):
    with get_conn() as conn:
        get_project_or_404(conn, project_id)
        conn.execute(
            "UPDATE projects SET title = ? WHERE id = ?",
            (body.title, project_id),
        )
        updated = conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
        return project_meta_from_row(updated)


@router.put("/projects/{project_id}/status", response_model=ProjectMeta)
def update_project_status(project_id: str, body: UpdateProjectStatusRequest):
    with get_conn() as conn:
        expire_reason_leases(conn, project_id)
        row = get_project_or_404(conn, project_id)
        current_status = row["status"]
        if current_status == "completed":
            raise HTTPException(409, "Completed projects cannot change status")
        if current_status == body.status:
            return project_meta_from_row(row)

        conn.execute(
            "UPDATE projects SET status = ? WHERE id = ?",
            (body.status, project_id),
        )
        if body.status == "stopped":
            conn.execute(
                "UPDATE intents SET worker = NULL WHERE project_id = ? AND concluded_at IS NULL",
                (project_id,),
            )
            clear_project_reason(conn, project_id)
        updated = conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
        return project_meta_from_row(updated)


@router.post("/projects/{project_id}/reason/claim", response_model=ProjectMeta)
def claim_project_reason(project_id: str, body: ReasonClaimRequest):
    with get_conn() as conn:
        check_project_active(conn, project_id)
        expire_reason_leases(conn, project_id)
        row = get_project_or_404(conn, project_id)
        current_worker = row["reason_worker"]
        if current_worker is not None and current_worker != body.worker:
            raise HTTPException(409, f"Project reason is currently claimed by {current_worker}")
        if current_worker == body.worker:
            return project_meta_from_row(row)

        now = utcnow()
        conn.execute(
            """
            UPDATE projects
            SET reason_worker = ?,
                reason_trigger = ?,
                reason_started_at = ?,
                reason_last_heartbeat_at = ?
            WHERE id = ?
            """,
            (body.worker, body.trigger, now, now, project_id),
        )
        updated = conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
        return project_meta_from_row(updated)


@router.post("/projects/{project_id}/reason/heartbeat", response_model=ProjectMeta)
def heartbeat_project_reason(project_id: str, body: HeartbeatRequest):
    with get_conn() as conn:
        check_project_active(conn, project_id)
        expire_reason_leases(conn, project_id)
        row = get_project_or_404(conn, project_id)
        current_worker = row["reason_worker"]
        if current_worker is None:
            raise HTTPException(409, "Project reason is not currently claimed")
        if current_worker != body.worker:
            raise HTTPException(409, f"Project reason is currently claimed by {current_worker}")

        now = utcnow()
        conn.execute(
            "UPDATE projects SET reason_last_heartbeat_at = ? WHERE id = ?",
            (now, project_id),
        )
        updated = conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
        return project_meta_from_row(updated)


@router.post("/projects/{project_id}/reason/release", response_model=ProjectMeta)
def release_project_reason(project_id: str, body: HeartbeatRequest):
    with get_conn() as conn:
        check_project_active(conn, project_id)
        expire_reason_leases(conn, project_id)
        row = get_project_or_404(conn, project_id)
        current_worker = row["reason_worker"]
        if current_worker is None:
            return project_meta_from_row(row)
        if current_worker != body.worker:
            raise HTTPException(409, f"Project reason is currently claimed by {current_worker}")

        clear_project_reason(conn, project_id)
        updated = conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
        return project_meta_from_row(updated)


@router.post("/projects/{project_id}/complete", response_model=Intent)
def complete_project(project_id: str, body: CompleteRequest):
    with get_conn() as conn:
        check_project_active(conn, project_id)
        expire_reason_leases(conn, project_id)
        validate_facts_exist(conn, project_id, body.from_)
        validate_goal_not_in_sources(body.from_)

        now = utcnow()
        iid = next_intent_id(conn, project_id)

        conn.execute(
            "INSERT INTO intents (id, project_id, to_fact_id, description, creator, worker, last_heartbeat_at, created_at, concluded_at) VALUES (?, ?, 'goal', ?, ?, ?, ?, ?, ?)",
            (iid, project_id, body.description, body.worker, body.worker, now, now, now),
        )
        for fid in body.from_:
            conn.execute(
                "INSERT INTO intent_sources (intent_id, project_id, fact_id) VALUES (?, ?, ?)",
                (iid, project_id, fid),
            )
        conn.execute(
            """
            UPDATE projects
            SET status = 'completed',
                reason_worker = NULL,
                reason_trigger = NULL,
                reason_started_at = NULL,
                reason_last_heartbeat_at = NULL
            WHERE id = ?
            """,
            (project_id,),
        )

        return Intent(
            id=iid,
            **{"from": body.from_},
            to="goal",
            description=body.description,
            creator=body.worker,
            worker=body.worker,
            last_heartbeat_at=now,
            created_at=now,
            concluded_at=now,
        )


@router.post("/projects/{project_id}/reopen", response_model=ReopenResponse)
def reopen_project(project_id: str, body: ReopenRequest):
    with get_conn() as conn:
        expire_reason_leases(conn, project_id)
        check_project_completed(conn, project_id)
        completion = get_completion_intent_or_409(conn, project_id)

        source_rows = conn.execute(
            "SELECT fact_id FROM intent_sources WHERE intent_id = ? AND project_id = ? ORDER BY rowid",
            (completion["id"], project_id),
        ).fetchall()
        source_ids = [row["fact_id"] for row in source_rows]
        if not source_ids:
            raise HTTPException(409, "Completion intent is missing its source facts")

        now = utcnow()
        fact_id = next_fact_id(conn, project_id)
        intent_id = next_intent_id(conn, project_id)
        description = body.description
        creator = body.creator

        conn.execute(
            "DELETE FROM intents WHERE id = ? AND project_id = ?",
            (completion["id"], project_id),
        )
        conn.execute(
            "INSERT INTO facts (id, project_id, description) VALUES (?, ?, ?)",
            (fact_id, project_id, description),
        )
        conn.execute(
            "INSERT INTO intents (id, project_id, to_fact_id, description, creator, worker, last_heartbeat_at, created_at, concluded_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (intent_id, project_id, fact_id, "external_feedback", creator, creator, now, now, now),
        )
        for source_id in source_ids:
            conn.execute(
                "INSERT INTO intent_sources (intent_id, project_id, fact_id) VALUES (?, ?, ?)",
                (intent_id, project_id, source_id),
            )
        clear_project_reason(conn, project_id)
        conn.execute(
            "UPDATE projects SET status = 'active' WHERE id = ?",
            (project_id,),
        )

        updated_project = conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
        updated_intent = conn.execute(
            "SELECT * FROM intents WHERE id = ? AND project_id = ?",
            (intent_id, project_id),
        ).fetchone()
        assert updated_project is not None
        assert updated_intent is not None
        return ReopenResponse(
            project=project_meta_from_row(updated_project),
            fact=Fact(id=fact_id, description=description),
            intent=intent_to_model(conn, updated_intent, project_id),
        )


@router.get("/projects/{project_id}/tool-executions")
def get_tool_executions(project_id: str, since: str = "", limit: int = 200):
    """Return tool executions for a project, ordered by time.

    Use ?since=<id> for incremental polling — only returns records
    created after the given ID.  Default limit 200 prevents OOM on
    very large projects.
    """
    with get_conn() as conn:
        if since:
            rows = conn.execute(
                """
                SELECT id, tool, kind, argv_json, status, returncode,
                       stdout, stderr, duration_ms, created_at, finished_at
                FROM tool_executions
                WHERE project_id = ? AND rowid > (
                    SELECT rowid FROM tool_executions WHERE id = ? AND project_id = ?
                )
                ORDER BY created_at ASC
                LIMIT ?
                """,
                (project_id, since, project_id, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT id, tool, kind, argv_json, status, returncode,
                       stdout, stderr, duration_ms, created_at, finished_at
                FROM tool_executions
                WHERE project_id = ?
                ORDER BY created_at ASC
                LIMIT ?
                """,
                (project_id, limit),
            ).fetchall()
    return {
        "project_id": project_id,
        "items": [
            {
                "id": row["id"],
                "tool": row["tool"],
                "kind": row["kind"],
                "argv": json.loads(row["argv_json"] or "[]"),
                "status": row["status"],
                "returncode": row["returncode"],
                "stdout": row["stdout"],
                "stderr": row["stderr"],
                "duration_ms": row["duration_ms"],
                "created_at": row["created_at"],
                "finished_at": row["finished_at"],
            }
            for row in rows
        ],
    }


@router.post("/upload-har")
async def upload_har(file: UploadFile):
    """Upload a HAR file. Saved to /tmp/axiom-uploads/, path returned for origin."""
    if not file.filename or not file.filename.endswith(('.har', '.json', '.txt')):
        raise HTTPException(400, "Only .har / .json / .txt accepted")
    upload_dir = "/tmp/axiom-uploads"
    _os.makedirs(upload_dir, exist_ok=True)
    safe_name = f"{uuid.uuid4().hex[:12]}_{file.filename}"
    path = _os.path.join(upload_dir, safe_name)
    content = await file.read()
    with open(path, "wb") as f:
        f.write(content)
    return {"path": path, "size": len(content)}
