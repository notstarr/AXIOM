from __future__ import annotations

import json
from pathlib import Path

from axiom.modules.artifacts.tables import artifacts
from axiom.modules.goals.tables import goal_evaluations
from axiom.modules.graph.tables import facts, intent_sources, intents
from axiom.modules.reason.tables import reason_run_sources, reason_runs, reason_workspaces
from axiom.modules.tasks.tables import tasks
from axiom.modules.todos.tables import todo_sources, todos
from axiom.modules.vulnerabilities.tables import vulnerabilities


def _project_and_task(client) -> tuple[dict, dict]:
    project = client.post("/api/v1/projects", json={"title": "归档测试"}).json()
    task = client.post(
        "/api/v1/tasks",
        json={
            "project_id": project["id"],
            "title": "归档链路",
            "origin": "archive.example.com",
        },
    ).json()
    return project, task


def _intent(intent_id: str, task_id: str, todo_id: str, to_fact_id: str, description: str):
    return {
        "id": intent_id,
        "task_id": task_id,
        "todo_id": todo_id,
        "kind": "explore",
        "description": description,
        "status": "completed",
        "creator": "test",
        "worker": None,
        "worker_pool_json": "[]",
        "targets_json": "[]",
        "retry_count": 0,
        "infrastructure_retry_count": 0,
        "last_failure_kind": "",
        "failure_class": "",
        "activity_kind": "",
        "activity_summary": "",
        "activity_tool": "",
        "to_fact_id": to_fact_id,
        "created_at": "2026-07-29T00:00:00Z",
        "concluded_at": "2026-07-29T00:01:00Z",
        "archived": False,
    }


def _seed_lineage(engine, task_id: str) -> None:
    with engine.begin() as conn:
        conn.execute(
            facts.insert(),
            [
                {
                    "id": fact_id,
                    "task_id": task_id,
                    "description": description,
                    "created_at": f"2026-07-29T00:0{index}:00Z",
                    "archived": False,
                }
                for index, (fact_id, description) in enumerate(
                    [
                        ("f001", "Bootstrap output"),
                        ("f002", "Duplicate branch root"),
                        ("f003", "Duplicate branch child"),
                        ("f004", "Visible sibling"),
                    ],
                    start=1,
                )
            ],
        )
        conn.execute(
            intents.update()
            .where(intents.c.task_id == task_id, intents.c.id == "i001")
            .values(
                status="completed",
                to_fact_id="f001",
                concluded_at="2026-07-29T00:01:00Z",
            )
        )
        conn.execute(
            todos.insert(),
            [
                {
                    "id": todo_id,
                    "task_id": task_id,
                    "title": title,
                    "category": "recon",
                    "theme": "archive",
                    "priority": 50,
                    "status": "completed",
                    "created_by": "test",
                    "status_reason": "done",
                    "created_at": "2026-07-29T00:00:00Z",
                    "updated_at": "2026-07-29T00:00:00Z",
                    "completed_at": "2026-07-29T00:00:00Z",
                    "archived": False,
                }
                for todo_id, title in [
                    ("td001", "root todo"),
                    ("td002", "child todo"),
                    ("td003", "sibling todo"),
                ]
            ],
        )
        conn.execute(
            intents.insert(),
            [
                _intent("i002", task_id, "td001", "f002", "duplicate root"),
                _intent("i003", task_id, "td002", "f003", "duplicate child"),
                _intent("i004", task_id, "td003", "f004", "visible sibling"),
            ],
        )
        conn.execute(
            intent_sources.insert(),
            [
                {"task_id": task_id, "intent_id": "i002", "fact_id": "f001"},
                {"task_id": task_id, "intent_id": "i003", "fact_id": "f002"},
                {"task_id": task_id, "intent_id": "i004", "fact_id": "f001"},
            ],
        )
        conn.execute(
            todo_sources.insert(),
            [
                {
                    "task_id": task_id,
                    "todo_id": "td001",
                    "source_type": "fact",
                    "source_id": fact_id,
                }
                for fact_id in ("f001", "f002")
            ]
            + [
                {
                    "task_id": task_id,
                    "todo_id": "td002",
                    "source_type": "fact",
                    "source_id": "f002",
                },
                {
                    "task_id": task_id,
                    "todo_id": "td003",
                    "source_type": "fact",
                    "source_id": "f001",
                },
            ],
        )
        conn.execute(
            artifacts.insert().values(
                id="a001",
                task_id=task_id,
                intent_id="i003",
                artifact_type="explore.findings",
                storage_key=f"tasks/{task_id}/artifacts/i003/findings.md",
                summary="duplicate findings",
                content_hash="hash",
                size=10,
                created_by="test",
                created_at="2026-07-29T00:00:00Z",
                archived=False,
            )
        )
        conn.execute(
            vulnerabilities.insert().values(
                id="v001",
                task_id=task_id,
                intent_id="i003",
                title="duplicate vulnerability",
                type="test",
                severity="medium",
                description="duplicate",
                prerequisites="",
                impact="none",
                reproduction_steps_json="[]",
                remediation="",
                curl_command="",
                evidence_json="[]",
                affected_target="archive.example.com",
                status="重复",
                status_reason="duplicate",
                source="explore",
                reported_by="worker",
                created_at="2026-07-29T00:00:00Z",
                updated_at="2026-07-29T00:00:00Z",
                archived=False,
                format_version=2,
            )
        )


def _archive(client, task_id: str, root_type: str, root_id: str) -> dict:
    preview = client.post(
        f"/api/v1/tasks/{task_id}/archives/preview",
        json={"root_type": root_type, "root_id": root_id},
    )
    assert preview.status_code == 200, preview.text
    body = preview.json()
    assert body["blockers"] == []
    created = client.post(
        f"/api/v1/tasks/{task_id}/archives",
        json={
            "root_type": root_type,
            "root_id": root_id,
            "expected_closure_hash": body["closure_hash"],
            "reason": "重复链路",
            "actor": "tester",
        },
    )
    assert created.status_code == 201, created.text
    return created.json()


def test_fact_archive_cascades_downstream_but_preserves_upstream_and_sibling(
    client, engine
) -> None:
    _project, task = _project_and_task(client)
    _seed_lineage(engine, task["id"])

    batch = _archive(client, task["id"], "fact", "f002")
    members = {(item["entity_type"], item["entity_id"]) for item in batch["members"]}
    assert {
        ("fact", "f002"),
        ("fact", "f003"),
        ("intent", "i002"),
        ("intent", "i003"),
        ("todo", "td001"),
        ("todo", "td002"),
        ("artifact", "a001"),
        ("vulnerability", "v001"),
    } <= members
    assert ("fact", "f001") not in members
    assert ("intent", "i001") not in members
    assert ("intent", "i004") not in members
    assert ("fact", "f004") not in members

    graph = client.get(f"/api/v1/tasks/{task['id']}/graph").json()
    assert {item["id"] for item in graph["facts"]} == {"origin", "goal", "f001", "f004"}
    assert {item["id"] for item in graph["intents"]} == {"i001", "i004"}
    assert client.get(f"/api/v1/tasks/{task['id']}/artifacts").json() == []
    assert client.get(f"/api/v1/tasks/{task['id']}/vulnerabilities").json() == []


def test_bootstrap_and_active_intent_block_archive(client, engine) -> None:
    _project, task = _project_and_task(client)
    _seed_lineage(engine, task["id"])

    bootstrap = client.post(
        f"/api/v1/tasks/{task['id']}/archives/preview",
        json={"root_type": "fact", "root_id": "f001"},
    ).json()
    assert any("Bootstrap Intent" in item for item in bootstrap["blockers"])

    with engine.begin() as conn:
        conn.execute(
            intents.update()
            .where(intents.c.task_id == task["id"], intents.c.id == "i003")
            .values(status="running")
        )
    active = client.post(
        f"/api/v1/tasks/{task['id']}/archives/preview",
        json={"root_type": "fact", "root_id": "f002"},
    ).json()
    assert any("i003" in item for item in active["blockers"])
    rejected = client.post(
        f"/api/v1/tasks/{task['id']}/archives",
        json={
            "root_type": "fact",
            "root_id": "f002",
            "expected_closure_hash": active["closure_hash"],
            "actor": "tester",
        },
    )
    assert rejected.status_code == 409


def test_overlapping_archive_batches_restore_only_uncovered_members(client, engine) -> None:
    _project, task = _project_and_task(client)
    _seed_lineage(engine, task["id"])

    parent = _archive(client, task["id"], "fact", "f002")
    child = _archive(client, task["id"], "intent", "i003")
    parent_detail = client.get(
        f"/api/v1/tasks/{task['id']}/archives/{parent['id']}"
    ).json()
    assert parent_detail["restored_count"] > 0
    assert parent_detail["retained_archived_count"] > 0

    restored = client.post(
        f"/api/v1/tasks/{task['id']}/archives/{parent['id']}/restore",
        json={"actor": "tester"},
    )
    assert restored.status_code == 200, restored.text
    assert restored.json()["retained_archived_count"] >= 2

    graph = client.get(f"/api/v1/tasks/{task['id']}/graph").json()
    assert "f002" in {item["id"] for item in graph["facts"]}
    assert "i002" in {item["id"] for item in graph["intents"]}
    assert "f003" not in {item["id"] for item in graph["facts"]}
    assert "i003" not in {item["id"] for item in graph["intents"]}

    final = client.post(
        f"/api/v1/tasks/{task['id']}/archives/{child['id']}/restore",
        json={"actor": "tester"},
    )
    assert final.status_code == 200
    graph = client.get(f"/api/v1/tasks/{task['id']}/graph").json()
    assert "f003" in {item["id"] for item in graph["facts"]}
    assert "i003" in {item["id"] for item in graph["intents"]}


def test_archived_entries_are_removed_from_new_context_and_expand(client, engine) -> None:
    _project, task = _project_and_task(client)
    _seed_lineage(engine, task["id"])
    _archive(client, task["id"], "fact", "f002")

    for ref in ("fact:f002", "intent:i002", "todo:td001", "artifact:a001", "vulnerability:v001"):
        expanded = client.post(
            f"/api/v1/tasks/{task['id']}/context/expand",
            json={"refs": [ref]},
        )
        assert expanded.status_code == 404, ref

    revision = client.post(
        f"/api/v1/tasks/{task['id']}/context",
        json={"phase": "reason"},
    )
    assert revision.status_code == 200, revision.text
    graph_path = Path(revision.json()["manifest_path"]).with_name("graph.json")
    context_graph = json.loads(graph_path.read_text())
    assert "f002" not in {item["id"] for item in context_graph["facts"]}
    assert "i002" not in {item["id"] for item in context_graph["intents"]}


def test_archive_rejects_a_stale_closure_hash(client, engine) -> None:
    _project, task = _project_and_task(client)
    _seed_lineage(engine, task["id"])
    preview = client.post(
        f"/api/v1/tasks/{task['id']}/archives/preview",
        json={"root_type": "fact", "root_id": "f002"},
    ).json()

    with engine.begin() as conn:
        conn.execute(
            intents.update()
            .where(intents.c.task_id == task["id"], intents.c.id == "i003")
            .values(status="failed")
        )

    response = client.post(
        f"/api/v1/tasks/{task['id']}/archives",
        json={
            "root_type": "fact",
            "root_id": "f002",
            "expected_closure_hash": preview["closure_hash"],
            "actor": "tester",
        },
    )
    assert response.status_code == 409
    assert "closure changed" in response.json()["error"]["message"]


def test_pending_intent_is_not_claimable_while_archived_and_is_claimable_after_restore(
    client,
    engine,
) -> None:
    _project, task = _project_and_task(client)
    _seed_lineage(engine, task["id"])
    client.put(
        "/api/v1/workers/worker-a",
        json={"capabilities": ["explore"], "max_running": 1},
    )
    with engine.begin() as conn:
        conn.execute(
            intents.update()
            .where(intents.c.task_id == task["id"], intents.c.id == "i003")
            .values(status="pending", to_fact_id=None, concluded_at=None)
        )
    batch = _archive(client, task["id"], "intent", "i003")

    endpoint = f"/api/v1/dispatch/tasks/{task['id']}/intents/i003/claim"
    blocked = client.post(endpoint, json={"worker_name": "worker-a"})
    assert blocked.status_code == 409

    restored = client.post(
        f"/api/v1/tasks/{task['id']}/archives/{batch['id']}/restore",
        json={"actor": "tester"},
    )
    assert restored.status_code == 200, restored.text
    claimed = client.post(endpoint, json={"worker_name": "worker-a"})
    assert claimed.status_code == 200, claimed.text


def _seed_active_reason_run(
    engine,
    task_id: str,
    *,
    source_fact_id: str,
    draft: dict | None = None,
) -> tuple[str, str]:
    reason_run_id = f"rr_archive_{task_id}"
    workspace_id = f"rw_archive_{task_id}"
    now = "2026-07-29T00:10:00Z"
    with engine.begin() as conn:
        conn.execute(
            reason_runs.insert().values(
                id=reason_run_id,
                task_id=task_id,
                status="running",
                trigger="test.archive",
                trigger_payload_json="{}",
                input_fingerprint="archive-test",
                worker_pool_json="[]",
                worker="worker-a",
                settings_version=1,
                execute_timeout_seconds=300,
                conclude_timeout_seconds=120,
                explore_execute_min_seconds=60,
                explore_execute_max_seconds=3600,
                explore_conclude_min_seconds=180,
                explore_conclude_max_seconds=600,
                lease_id="lease_archive_test",
                claimed_at=now,
                last_heartbeat_at=now,
                retry_count=0,
                infrastructure_retry_count=0,
                last_failure_kind="",
                failure_class="",
                activity_kind="reasoning",
                activity_summary="testing",
                activity_tool="",
                activity_started_at=now,
                activity_updated_at=now,
                created_at=now,
                updated_at=now,
            )
        )
        conn.execute(
            reason_run_sources.insert().values(
                task_id=task_id,
                reason_run_id=reason_run_id,
                fact_id=source_fact_id,
            )
        )
        conn.execute(
            reason_workspaces.insert().values(
                id=workspace_id,
                task_id=task_id,
                intent_id=None,
                reason_run_id=reason_run_id,
                lease_id="lease_archive_test",
                status="open",
                context_revision=1,
                draft_json=json.dumps(draft or {}),
                worker="worker-a",
                failure_reason="",
                result_json="{}",
                created_at=now,
                updated_at=now,
            )
        )
    return reason_run_id, workspace_id


def test_archive_cancels_reason_run_when_source_or_workspace_is_invalidated(
    client,
    engine,
) -> None:
    for source_fact_id, draft in (
        ("f002", {}),
        (
            "f004",
            {
                "todo_updates": [
                    {"todo_id": "td001", "status": "completed", "source_adds": []}
                ]
            },
        ),
    ):
        _project, task = _project_and_task(client)
        _seed_lineage(engine, task["id"])
        reason_run_id, workspace_id = _seed_active_reason_run(
            engine,
            task["id"],
            source_fact_id=source_fact_id,
            draft=draft,
        )

        _archive(client, task["id"], "fact", "f002")
        with engine.connect() as conn:
            run = conn.execute(
                reason_runs.select().where(reason_runs.c.id == reason_run_id)
            ).mappings().one()
            workspace = conn.execute(
                reason_workspaces.select().where(reason_workspaces.c.id == workspace_id)
            ).mappings().one()
        assert run["status"] == "cancelled"
        assert run["lease_id"] is None
        assert run["last_failure_kind"] == "archive_context_invalidated"
        assert workspace["status"] == "abandoned"
        assert workspace["failure_reason"] == "archive_context_invalidated"


def test_goal_evaluation_and_goal_edge_hide_archived_evidence(client, engine) -> None:
    _project, task = _project_and_task(client)
    _seed_lineage(engine, task["id"])
    with engine.begin() as conn:
        conn.execute(
            goal_evaluations.insert().values(
                id="ge_archive_test",
                task_id=task["id"],
                intent_id=None,
                reason_run_id="rr_history",
                workspace_id=None,
                achieved=True,
                reason="historical assessment",
                evidence_refs_json='["fact:f002"]',
                evaluator="reason",
                created_at="2026-07-29T00:20:00Z",
            )
        )
    _archive(client, task["id"], "fact", "f002")

    graph = client.get(f"/api/v1/tasks/{task['id']}/graph").json()
    assert graph["goal_from_fact_ids"] == []
    rejected = client.post(
        f"/api/v1/tasks/{task['id']}/goal-evaluations",
        json={
            "achieved": False,
            "reason": "must not reuse archived evidence",
            "evidence_refs": ["fact:f002"],
            "evaluator": "human",
        },
    )
    assert rejected.status_code == 409

    revision = client.post(
        f"/api/v1/tasks/{task['id']}/context",
        json={"phase": "reason"},
    ).json()
    context_dir = Path(revision["manifest_path"]).parent
    manifest = json.loads(
        Path(revision["manifest_path"]).read_text(encoding="utf-8")
    )
    assert "goal_evaluations" not in manifest["indexes"]
    assert "goal_cycles" not in manifest["indexes"]
    assert not (context_dir / "goal_evaluations.json").exists()
    assert not (context_dir / "goal_cycles.json").exists()
    expanded = client.post(
        f"/api/v1/tasks/{task['id']}/context/expand",
        json={"refs": ["goal_evaluation:ge_archive_test"]},
    )
    assert expanded.status_code == 404


def test_partially_archived_todo_stays_visible_with_only_visible_sources(
    client,
    engine,
) -> None:
    _project, task = _project_and_task(client)
    _seed_lineage(engine, task["id"])
    with engine.begin() as conn:
        conn.execute(
            facts.insert().values(
                id="f005",
                task_id=task["id"],
                description="visible second branch",
                created_at="2026-07-29T00:05:00Z",
                archived=False,
            )
        )
        conn.execute(
            intents.insert().values(
                **_intent("i005", task["id"], "td001", "f005", "visible second branch")
            )
        )
        conn.execute(
            intent_sources.insert().values(
                task_id=task["id"],
                intent_id="i005",
                fact_id="f001",
            )
        )
    _archive(client, task["id"], "fact", "f002")

    todos_response = client.get(f"/api/v1/tasks/{task['id']}/todos").json()
    assert "td001" in {item["id"] for item in todos_response}
    expanded = client.post(
        f"/api/v1/tasks/{task['id']}/context/expand",
        json={"refs": ["todo:td001"]},
    )
    assert expanded.status_code == 200, expanded.text
    sources = expanded.json()["entries"]["todo:td001"]["sources"]
    assert sources == [{"type": "fact", "id": "f001"}]


def test_archived_artifact_remains_available_only_through_archive_view(
    client,
    engine,
    data_dir,
) -> None:
    _project, task = _project_and_task(client)
    _seed_lineage(engine, task["id"])
    path = data_dir / f"tasks/{task['id']}/artifacts/i003/findings.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("# archived findings\n", encoding="utf-8")
    batch = _archive(client, task["id"], "intent", "i003")

    assert client.get(
        f"/api/v1/tasks/{task['id']}/artifacts/a001/content"
    ).status_code == 404
    content = client.get(
        f"/api/v1/tasks/{task['id']}/archives/{batch['id']}/artifacts/a001/content"
    )
    assert content.status_code == 200, content.text
    assert content.json()["content"] == "# archived findings\n"
    download = client.get(
        f"/api/v1/tasks/{task['id']}/archives/{batch['id']}/artifacts/a001/download"
    )
    assert download.status_code == 200
    assert download.content == b"# archived findings\n"


def test_archive_compatibility_endpoints_create_and_restore_batches(client, engine) -> None:
    _project, task = _project_and_task(client)
    _seed_lineage(engine, task["id"])

    archived = client.patch(
        f"/api/v1/tasks/{task['id']}/facts/f002",
        json={"archived": True},
    )
    assert archived.status_code == 200, archived.text
    assert archived.json()["archived"] is True
    restored = client.patch(
        f"/api/v1/tasks/{task['id']}/facts/f002",
        json={"archived": False},
    )
    assert restored.status_code == 200, restored.text
    assert restored.json()["archived"] is False

    toggled = client.post(f"/api/v1/tasks/{task['id']}/intents/i003/toggle-archive")
    assert toggled.status_code == 200, toggled.text
    assert toggled.json()["archived"] is True
    toggled_back = client.post(
        f"/api/v1/tasks/{task['id']}/intents/i003/toggle-archive"
    )
    assert toggled_back.status_code == 200, toggled_back.text
    assert toggled_back.json()["archived"] is False

    deleted = client.delete(f"/api/v1/tasks/{task['id']}/intents/i003")
    assert deleted.status_code == 204
    batches = client.get(f"/api/v1/tasks/{task['id']}/archives?status=active").json()
    assert any(item["root_type"] == "intent" and item["root_id"] == "i003" for item in batches)


def test_artifact_and_vulnerability_delete_endpoints_use_archive_batches(
    client,
    engine,
) -> None:
    _project, task = _project_and_task(client)
    _seed_lineage(engine, task["id"])

    assert client.delete(f"/api/v1/tasks/{task['id']}/artifacts/a001").status_code == 204
    assert client.delete(
        f"/api/v1/tasks/{task['id']}/vulnerabilities/v001"
    ).status_code == 204
    batches = client.get(f"/api/v1/tasks/{task['id']}/archives?status=active").json()
    roots = {(item["root_type"], item["root_id"]) for item in batches}
    assert ("artifact", "a001") in roots
    assert ("vulnerability", "v001") in roots


def test_compat_restore_requires_batch_selection_when_same_root_is_archived_twice(
    client,
    engine,
) -> None:
    _project, task = _project_and_task(client)
    _seed_lineage(engine, task["id"])
    _archive(client, task["id"], "intent", "i003")
    _archive(client, task["id"], "intent", "i003")

    response = client.patch(
        f"/api/v1/tasks/{task['id']}/intents/i003",
        json={"archived": False},
    )
    assert response.status_code == 409
    assert "Multiple archive batches" in response.json()["error"]["message"]


def test_legacy_readonly_task_can_read_archives_but_cannot_mutate_them(client, engine) -> None:
    _project, task = _project_and_task(client)
    _seed_lineage(engine, task["id"])
    batch = _archive(client, task["id"], "intent", "i003")
    with engine.begin() as conn:
        conn.execute(
            tasks.update().where(tasks.c.id == task["id"]).values(legacy_readonly=True)
        )

    assert client.get(f"/api/v1/tasks/{task['id']}/archives").status_code == 200
    assert client.get(
        f"/api/v1/tasks/{task['id']}/archives/{batch['id']}"
    ).status_code == 200
    assert client.post(
        f"/api/v1/tasks/{task['id']}/archives/{batch['id']}/restore",
        json={"actor": "tester"},
    ).status_code == 409
    assert client.post(
        f"/api/v1/tasks/{task['id']}/archives/preview",
        json={"root_type": "intent", "root_id": "i004"},
    ).status_code == 409


def test_new_graph_and_context_remove_dangling_refs_from_historical_archive_flags(
    client,
    engine,
) -> None:
    _project, task = _project_and_task(client)
    _seed_lineage(engine, task["id"])
    with engine.begin() as conn:
        conn.execute(
            facts.update()
            .where(facts.c.task_id == task["id"], facts.c.id.in_(["f002", "f003"]))
            .values(archived=True)
        )

    graph = client.get(f"/api/v1/tasks/{task['id']}/graph").json()
    visible_intent = next(item for item in graph["intents"] if item["id"] == "i003")
    assert visible_intent["from_facts"] == []
    assert visible_intent["to_fact_id"] is None

    with engine.begin() as conn:
        conn.execute(
            intents.update()
            .where(intents.c.task_id == task["id"], intents.c.id == "i003")
            .values(archived=True)
        )
    revision = client.post(
        f"/api/v1/tasks/{task['id']}/context",
        json={"phase": "reason"},
    ).json()
    context_dir = Path(revision["manifest_path"]).parent
    artifacts_index = json.loads(
        (context_dir / "artifacts.json").read_text(encoding="utf-8")
    )
    todos_index = json.loads((context_dir / "todos.json").read_text(encoding="utf-8"))
    vulnerabilities_index = json.loads(
        (context_dir / "vulnerabilities.json").read_text(encoding="utf-8")
    )
    artifact_detail = json.loads(
        (context_dir / artifacts_index[0]["detail"]).read_text(encoding="utf-8")
    )
    vulnerability_detail = json.loads(
        (context_dir / vulnerabilities_index[0]["detail"]).read_text(encoding="utf-8")
    )
    todo_entry = next(item for item in todos_index if item["id"] == "td002")
    todo_detail = json.loads(
        (context_dir / todo_entry["detail"]).read_text(encoding="utf-8")
    )
    assert artifact_detail["intent_id"] is None
    assert vulnerability_detail["intent_id"] is None
    assert todo_detail["sources"] == []
    assert vulnerabilities_index[0]["intent_id"] is None


def test_archived_domain_records_cannot_be_edited(client, engine) -> None:
    _project, task = _project_and_task(client)
    _seed_lineage(engine, task["id"])
    _archive(client, task["id"], "fact", "f002")

    attempts = [
        client.patch(
            f"/api/v1/tasks/{task['id']}/facts/f002",
            json={"description": "must not change"},
        ),
        client.patch(
            f"/api/v1/tasks/{task['id']}/intents/i002",
            json={"description": "must not change"},
        ),
        client.patch(
            f"/api/v1/tasks/{task['id']}/todos/td001",
            json={"status": "active", "actor": "tester"},
        ),
        client.patch(
            f"/api/v1/tasks/{task['id']}/vulnerabilities/v001",
            json={"status": "待验证"},
        ),
    ]
    assert [response.status_code for response in attempts] == [409, 409, 409, 409]


def test_archived_pending_intent_keeps_its_state_during_task_updates_and_completion(
    client,
    engine,
) -> None:
    _project, task = _project_and_task(client)
    _seed_lineage(engine, task["id"])
    with engine.begin() as conn:
        conn.execute(
            intents.update()
            .where(intents.c.task_id == task["id"], intents.c.id == "i003")
            .values(
                status="pending",
                to_fact_id=None,
                concluded_at=None,
                worker_pool_json='["worker-a"]',
            )
        )
    batch = _archive(client, task["id"], "intent", "i003")

    updated = client.patch(
        f"/api/v1/tasks/{task['id']}",
        json={"worker_pool": ["worker-b"]},
    )
    assert updated.status_code == 200, updated.text
    completed = client.post(
        f"/api/v1/tasks/{task['id']}/goal-evaluations",
        json={
            "achieved": True,
            "reason": "complete without touching archived pending work",
            "evidence_refs": ["fact:f001"],
            "evaluator": "human",
        },
    )
    assert completed.status_code == 201, completed.text

    with engine.connect() as conn:
        archived_intent = conn.execute(
            intents.select().where(
                intents.c.task_id == task["id"],
                intents.c.id == "i003",
            )
        ).mappings().one()
    assert archived_intent["status"] == "pending"
    assert json.loads(archived_intent["worker_pool_json"]) == ["worker-a"]

    restored = client.post(
        f"/api/v1/tasks/{task['id']}/archives/{batch['id']}/restore",
        json={"actor": "tester"},
    )
    assert restored.status_code == 200, restored.text
    visible = client.get(f"/api/v1/tasks/{task['id']}/intents/i003").json()
    assert visible["status"] == "pending"
    assert visible["worker_pool"] == ["worker-a"]


def test_archive_preview_confirm_button_uses_boolean_blocker_condition() -> None:
    index = (
        Path(__file__).parents[1] / "src" / "axiom" / "static" / "index.html"
    ).read_text(encoding="utf-8")

    assert "archivePreview.blockers?.length > 0" in index
    assert "|| archivePreview.blockers?.length\"" not in index
