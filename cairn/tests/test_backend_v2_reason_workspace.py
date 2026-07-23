from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import func, select, update

from axiom.api.v1.router import router
from axiom.infrastructure.database.engine import create_sqlite_engine
from axiom.infrastructure.database.metadata import (
    artifacts,
    facts,
    intent_sources,
    intents,
    metadata,
    reason_workspaces,
    todo_sources,
    todos,
)
from axiom.modules.common.time import utcnow
from axiom.modules.projects.schemas import CreateProject
from axiom.modules.projects.service import ProjectService
from axiom.modules.reason_workspace.schemas import (
    DraftIntent,
    DraftTodo,
    DraftTodoUpdate,
    WorkspaceDraft,
)
from axiom.modules.reason_workspace.service import ReasonWorkspaceService
from axiom.modules.tasks.schemas import CreateTask, TaskScope
from axiom.modules.tasks.lifecycle import TaskLifecycleService
from axiom.modules.tasks.service import TaskService
from axiom.modules.todos.schemas import TodoSource


def make_workspace_service(tmp_path):
    engine = create_sqlite_engine(tmp_path / "axiom-v2.db")
    metadata.create_all(engine)
    project = ProjectService(engine).create(CreateProject(title="Demo"))
    task = TaskService(engine).create(
        CreateTask(project_id=project.id, title="Task", origin="api.example.com", goal="finish")
    )
    TaskLifecycleService(engine).claim_reason(task.id, worker="reasoner")
    return engine, task, ReasonWorkspaceService(engine)


def complete_bootstrap(engine, task_id: str, *, with_artifact: bool = False) -> None:
    now = utcnow()
    with engine.begin() as conn:
        conn.execute(
            facts.insert().values(
                id="f001",
                task_id=task_id,
                description="Bootstrap attack-surface summary",
                created_at=now,
            )
        )
        conn.execute(
            update(intents)
            .where(intents.c.task_id == task_id, intents.c.id == "i001")
            .values(status="completed", to_fact_id="f001", concluded_at=now)
        )
        if with_artifact:
            conn.execute(
                artifacts.insert().values(
                    id="a001",
                    task_id=task_id,
                    intent_id="i001",
                    artifact_type="initial_leads",
                    path=f"/tmp/{task_id}-initial-leads.md",
                    summary="Bootstrap leads",
                    content_hash="a" * 64,
                    size=1,
                    created_by="test",
                    created_at=now,
                )
            )


def test_reason_workspace_commits_todos_and_intents_atomically(tmp_path) -> None:
    engine, task, service = make_workspace_service(tmp_path)
    workspace = service.open(task.id, worker="reasoner", context_revision=1)
    service.add_todo(
        workspace.id,
        DraftTodo(
            ref="t1",
            title="Verify JWT forgery",
            category="authentication",
            theme="jwt",
            priority=80,
            sources=[TodoSource(type="origin", id="origin")],
        ),
    )
    service.propose_intent(
        workspace.id,
        DraftIntent(
            todo_ref="t1",
            from_facts=["origin"],
            description="Verify JWT algorithm confusion",
            targets=["api.example.com"],
            execute_seconds=240,
            conclude_seconds=90,
        ),
    )

    result = service.commit(workspace.id)

    assert result.workspace.status == "committed"
    assert result.created_todo_ids == ["td001"]
    assert result.created_intent_ids == ["i002"]
    with engine.connect() as conn:
        todo = conn.execute(select(todos)).mappings().one()
        intent_rows = conn.execute(select(intents).order_by(intents.c.id)).mappings().all()
    assert todo["status"] == "active"
    assert intent_rows[1]["todo_id"] == "td001"
    assert json.loads(intent_rows[1]["targets_json"]) == ["api.example.com"]
    assert intent_rows[1]["execute_timeout_seconds"] == 240
    assert intent_rows[1]["conclude_timeout_seconds"] == 90


def test_reason_workspace_keeps_todo_only_plan_in_reason_phase(tmp_path) -> None:
    engine, task, service = make_workspace_service(tmp_path)
    workspace = service.open(task.id, worker="reasoner", context_revision=1)
    service.add_todo(
        workspace.id,
        DraftTodo(
            ref="t1",
            title="继续验证权限边界",
            category="authorization",
            sources=[TodoSource(type="origin", id="origin")],
        ),
    )

    result = service.commit(workspace.id)

    assert result.created_intent_ids == []
    assert TaskService(engine).get(task.id).phase == "reason"


def test_reason_workspace_keeps_explore_phase_when_existing_intents_remain(tmp_path) -> None:
    engine, task, service = make_workspace_service(tmp_path)
    with engine.begin() as conn:
        conn.execute(
            intents.insert().values(
                id="i002",
                task_id=task.id,
                description="existing queued work",
                status="pending",
                creator="previous-reason",
                worker_pool_json="[]",
                targets_json='["api.example.com"]',
                execute_timeout_seconds=240,
                conclude_timeout_seconds=90,
                created_at=utcnow(),
            )
        )
    workspace = service.open(task.id, worker="reasoner", context_revision=1)

    result = service.commit(workspace.id)

    assert result.created_intent_ids == []
    assert TaskService(engine).get(task.id).phase == "explore"


def test_reason_workspace_scope_failure_does_not_pollute_intent_draft(tmp_path) -> None:
    engine, task, service = make_workspace_service(tmp_path)
    workspace = service.open(task.id, worker="reasoner", context_revision=1)
    service.add_todo(
        workspace.id,
        DraftTodo(
            ref="t1",
            title="Out of scope idea",
            category="other",
            sources=[TodoSource(type="origin", id="origin")],
        ),
    )
    with pytest.raises(ValueError, match="outside Task Scope"):
        service.propose_intent(
            workspace.id,
            DraftIntent(
                todo_ref="t1",
                from_facts=["origin"],
                description="Do not dispatch this",
                targets=["other.example.com"],
                execute_seconds=240,
                conclude_seconds=90,
            ),
        )

    with engine.connect() as conn:
        assert conn.execute(select(func.count()).select_from(todos)).scalar_one() == 0
        assert conn.execute(select(func.count()).select_from(intents)).scalar_one() == 1
        row = conn.execute(select(reason_workspaces)).mappings().one()
    assert row["status"] == "open"
    assert json.loads(row["draft_json"])["intent_proposals"] == []


def test_reason_workspace_normalizes_scope_ports_and_rejects_duplicate_drafts(tmp_path) -> None:
    engine, task, service = make_workspace_service(tmp_path)
    TaskService(engine).update_scope(
        task.id,
        TaskScope(enabled=True, targets=["127.0.0.1:18990"]),
    )
    workspace = service.open(task.id, worker="reasoner", context_revision=1)
    service.add_todo(
        workspace.id,
        DraftTodo(
            ref="t1",
            title="Verify local acceptance endpoints",
            category="recon",
            sources=[TodoSource(type="origin", id="origin")],
        ),
    )
    first = DraftIntent(
        todo_ref="t1",
        from_facts=["origin"],
        description="Verify the two local GET endpoints",
        targets=["127.0.0.1:18990"],
        execute_seconds=30,
        conclude_seconds=30,
    )
    service.propose_intent(workspace.id, first)

    with pytest.raises(ValueError, match="Duplicate Intent proposal"):
        service.propose_intent(
            workspace.id,
            first.model_copy(update={"targets": ["http://127.0.0.1:18990"]}),
        )

    result = service.commit(workspace.id)
    assert result.created_intent_ids == ["i002"]
    with engine.connect() as conn:
        row = conn.execute(
            select(intents).where(intents.c.task_id == task.id, intents.c.id == "i002")
        ).mappings().one()
    assert json.loads(row["targets_json"]) == ["127.0.0.1"]


def test_reason_workspace_can_only_commit_once(tmp_path) -> None:
    _engine, task, service = make_workspace_service(tmp_path)
    workspace = service.open(task.id, worker="reasoner", context_revision=1)
    service.commit(workspace.id)

    with pytest.raises(ValueError, match="already committed"):
        service.commit(workspace.id)


def test_reason_intent_requires_timeouts_and_one_todo_reference() -> None:
    with pytest.raises(Exception):
        DraftIntent(
            todo_ref="t1",
            todo_id="td001",
            from_facts=["origin"],
            description="invalid",
            targets=["api.example.com"],
            execute_seconds=0,
            conclude_seconds=90,
        )


def test_reason_intent_keeps_timeout_metadata_out_of_description() -> None:
    with pytest.raises(ValueError, match="must not repeat execute/conclude budgets"):
        DraftIntent(
            todo_ref="t1",
            from_facts=["origin"],
            description="验证接口访问控制；预算：execute 300s，conclude 90s",
            targets=["api.example.com"],
            execute_seconds=300,
            conclude_seconds=90,
        )


def test_reason_workspace_v1_api_flow(tmp_path) -> None:
    engine, task, _service = make_workspace_service(tmp_path)
    app = FastAPI()
    app.state.v2_engine = engine
    app.state.service_token = "service-token"
    app.include_router(router)

    with TestClient(app) as client:
        assert client.post(
            f"/api/v1/tasks/{task.id}/reason/workspaces",
            json={"worker": "reasoner", "context_revision": 1},
        ).status_code == 401
        headers = {"Authorization": "Bearer service-token"}
        opened = client.post(
            f"/api/v1/tasks/{task.id}/reason/workspaces",
            json={"worker": "reasoner", "context_revision": 1},
            headers=headers,
        )
        assert opened.status_code == 201
        workspace_id = opened.json()["id"]
        assert client.post(
            f"/api/v1/reason-workspaces/{workspace_id}/todos",
            json={
                "ref": "t1",
                "title": "Verify JWT",
                "category": "authentication",
                "theme": "jwt",
                "priority": 80,
                "sources": [{"type": "origin", "id": "origin"}],
            },
            headers=headers,
        ).status_code == 200
        assert client.post(
            f"/api/v1/reason-workspaces/{workspace_id}/intents",
            json={
                "todo_ref": "t1",
                "from_facts": ["origin"],
                "description": "Verify JWT algorithm confusion",
                "targets": ["api.example.com"],
                "execute_seconds": 240,
                "conclude_seconds": 90,
            },
            headers=headers,
        ).status_code == 200
        committed = client.post(
            f"/api/v1/reason-workspaces/{workspace_id}/commit",
            headers=headers,
        )
        assert committed.status_code == 200
        assert committed.json()["created_todo_ids"] == ["td001"]
        assert committed.json()["created_intent_ids"] == ["i002"]


def test_todo_source_is_validated_when_added_not_at_commit(tmp_path) -> None:
    _engine, task, service = make_workspace_service(tmp_path)
    workspace = service.open(task.id, worker="reasoner", context_revision=1)

    with pytest.raises(ValueError, match="Todo source not found: hint:h001"):
        service.add_todo(
            workspace.id,
            DraftTodo(
                ref="bad-source",
                title="Invalid source",
                category="other",
                sources=[TodoSource(type="hint", id="h001")],
            ),
        )
    assert service.get(workspace.id).draft.todo_adds == []


def test_intent_references_are_validated_when_proposed(tmp_path) -> None:
    _engine, task, service = make_workspace_service(tmp_path)
    workspace = service.open(task.id, worker="reasoner", context_revision=1)

    with pytest.raises(ValueError, match="Todo draft ref not found"):
        service.propose_intent(
            workspace.id,
            DraftIntent(
                todo_ref="missing",
                from_facts=["origin"],
                description="Invalid plan",
                targets=["api.example.com"],
                execute_seconds=30,
                conclude_seconds=30,
            ),
        )
    assert service.get(workspace.id).draft.intent_proposals == []


def test_reason_submit_commits_complete_plan_atomically(tmp_path) -> None:
    _engine, task, service = make_workspace_service(tmp_path)
    workspace = service.open(task.id, worker="reasoner", context_revision=1)
    result = service.submit(
        workspace.id,
        WorkspaceDraft(
            todo_adds=[
                DraftTodo(
                    ref="jwt",
                    title="Verify JWT forgery",
                    category="authentication",
                    sources=[TodoSource(type="origin", id="origin")],
                )
            ],
            intent_proposals=[
                DraftIntent(
                    todo_ref="jwt",
                    from_facts=["origin"],
                    description="Verify JWT forgery",
                    targets=["api.example.com"],
                    execute_seconds=120,
                    conclude_seconds=60,
                )
            ],
        ),
    )

    assert result.workspace.status == "committed"
    assert result.workspace.draft.todo_adds[0].ref == "jwt"
    assert result.created_todo_ids == ["td001"]
    assert result.created_intent_ids == ["i002"]


def test_concurrent_todo_adds_do_not_lose_draft_entries(tmp_path) -> None:
    _engine, task, service = make_workspace_service(tmp_path)
    workspace = service.open(task.id, worker="reasoner", context_revision=1)

    def add(ref: str) -> None:
        service.add_todo(
            workspace.id,
            DraftTodo(
                ref=ref,
                title=f"Todo {ref}",
                category="recon",
                sources=[TodoSource(type="origin", id="origin")],
            ),
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(add, ["one", "two"]))

    refs = {item.ref for item in service.get(workspace.id).draft.todo_adds}
    assert refs == {"one", "two"}


def test_first_reason_derives_bootstrap_fact_from_artifact_and_rejects_origin_dispatch(
    tmp_path,
) -> None:
    engine, task, service = make_workspace_service(tmp_path)
    complete_bootstrap(engine, task.id, with_artifact=True)
    workspace = service.open(task.id, worker="reasoner", context_revision=1)

    bad_plan = WorkspaceDraft(
        todo_adds=[
            DraftTodo(
                ref="lead",
                title="Verify Bootstrap lead",
                category="recon",
                sources=[TodoSource(type="artifact", id="a001")],
            )
        ],
        intent_proposals=[
            DraftIntent(
                todo_ref="lead",
                from_facts=["origin"],
                description="Verify the lead",
                targets=["api.example.com"],
                execute_seconds=120,
                conclude_seconds=60,
            )
        ],
    )
    with pytest.raises(ValueError, match="unrelated Facts: origin"):
        service.submit(workspace.id, bad_plan)

    result = service.submit(
        workspace.id,
        bad_plan.model_copy(
            update={
                "intent_proposals": [
                    bad_plan.intent_proposals[0].model_copy(
                        update={"from_facts": ["f001"]}
                    )
                ]
            }
        ),
    )
    assert result.created_intent_ids == ["i002"]
    with engine.connect() as conn:
        persisted_sources = set(
            conn.execute(
                select(todo_sources.c.source_type, todo_sources.c.source_id).where(
                    todo_sources.c.task_id == task.id,
                    todo_sources.c.todo_id == "td001",
                )
            ).all()
        )
        edge_sources = list(
            conn.execute(
                select(intent_sources.c.fact_id).where(
                    intent_sources.c.task_id == task.id,
                    intent_sources.c.intent_id == "i002",
                )
            ).scalars()
        )
    assert persisted_sources == {("artifact", "a001"), ("fact", "f001")}
    assert edge_sources == ["f001"]


def test_later_reason_can_dispatch_old_todo_from_old_fact_and_append_new_evidence(
    tmp_path,
) -> None:
    engine, task, service = make_workspace_service(tmp_path)
    complete_bootstrap(engine, task.id, with_artifact=True)
    first = service.open(task.id, worker="reasoner", context_revision=1)
    first_result = service.submit(
        first.id,
        WorkspaceDraft(
            todo_adds=[
                DraftTodo(
                    ref="deferred",
                    title="Deferred Bootstrap lead",
                    category="authorization",
                    sources=[TodoSource(type="artifact", id="a001")],
                )
            ]
        ),
    )
    todo_id = first_result.created_todo_ids[0]
    with engine.begin() as conn:
        conn.execute(
            facts.insert().values(
                id="f002",
                task_id=task.id,
                description="A later Explore result",
                created_at=utcnow(),
            )
        )

    second = service.open(task.id, worker="reasoner", context_revision=2)
    result = service.submit(
        second.id,
        WorkspaceDraft(
            todo_updates=[
                DraftTodoUpdate(
                    todo_id=todo_id,
                    source_adds=[TodoSource(type="fact", id="f002")],
                )
            ],
            intent_proposals=[
                DraftIntent(
                    todo_id=todo_id,
                    from_facts=["f001"],
                    description="Continue the deferred Bootstrap lead",
                    targets=["api.example.com"],
                    execute_seconds=120,
                    conclude_seconds=60,
                )
            ],
        ),
    )
    assert result.created_intent_ids == ["i002"]
    with engine.connect() as conn:
        persisted_sources = set(
            conn.execute(
                select(todo_sources.c.source_type, todo_sources.c.source_id).where(
                    todo_sources.c.task_id == task.id,
                    todo_sources.c.todo_id == todo_id,
                )
            ).all()
        )
        edge_sources = list(
            conn.execute(
                select(intent_sources.c.fact_id).where(
                    intent_sources.c.task_id == task.id,
                    intent_sources.c.intent_id == "i002",
                )
            ).scalars()
        )
    assert ("fact", "f001") in persisted_sources
    assert ("fact", "f002") in persisted_sources
    assert edge_sources == ["f001"]


def test_later_reason_rejects_fact_not_attached_to_todo(tmp_path) -> None:
    engine, task, service = make_workspace_service(tmp_path)
    complete_bootstrap(engine, task.id)
    first = service.open(task.id, worker="reasoner", context_revision=1)
    todo_id = service.submit(
        first.id,
        WorkspaceDraft(
            todo_adds=[
                DraftTodo(
                    ref="old",
                    title="Old evidence",
                    category="other",
                    sources=[TodoSource(type="fact", id="f001")],
                )
            ]
        ),
    ).created_todo_ids[0]
    with engine.begin() as conn:
        conn.execute(
            facts.insert().values(
                id="f002",
                task_id=task.id,
                description="Unrelated evidence",
                created_at=utcnow(),
            )
        )
    second = service.open(task.id, worker="reasoner", context_revision=2)
    with pytest.raises(ValueError, match="unrelated Facts: f002"):
        service.submit(
            second.id,
            WorkspaceDraft(
                intent_proposals=[
                    DraftIntent(
                        todo_id=todo_id,
                        from_facts=["f002"],
                        description="Incorrectly jump to unrelated evidence",
                        targets=["api.example.com"],
                        execute_seconds=120,
                        conclude_seconds=60,
                    )
                ]
            ),
        )
