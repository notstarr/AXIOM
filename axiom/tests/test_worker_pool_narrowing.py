"""Retry-time worker-pool narrowing.

When an attempt dies from an infrastructure fault, retrying with the same pool
tends to re-select the same worker and burns the whole retry budget on one
broken backend.  Narrowing the pool is what keeps a single bad worker from
parking a work item.

The invariant these tests exist to protect: **the narrowed pool is never
empty**.  The dispatcher reads an empty pool as "any capable worker", so
persisting ``[]`` would silently re-open the item to the worker that just
failed.
"""

from __future__ import annotations

from axiom.modules.dispatch.pool import narrow_pool_after_failure, narrow_worker_pool


# ------------------------------------------------------------------ pure rules


def test_drops_the_worker_that_just_failed() -> None:
    assert narrow_worker_pool(["a", "b", "c"], "a", None) == ["b", "c"]


def test_keeps_a_single_worker_pool_unchanged() -> None:
    """With no alternative, retrying the same worker is the correct behaviour."""

    assert narrow_worker_pool(["a"], "a", ["a", "b"]) == ["a"]


def test_materializes_an_empty_pool_from_capable_workers() -> None:
    """An empty pool means "any worker", so the failure must still be pinned."""

    assert narrow_worker_pool([], "a", ["a", "b"]) == ["b"]


def test_never_returns_an_empty_pool() -> None:
    # The only candidate failed and there is no fallback: keep it rather than
    # persisting [], which the dispatcher would read as "any worker".
    assert narrow_worker_pool(["a"], "a", ["a"]) == ["a"]
    assert narrow_worker_pool([], "a", ["a"]) == ["a"]
    assert narrow_worker_pool(["a"], "a", []) == ["a"]


def test_ignores_a_missing_failed_worker() -> None:
    assert narrow_worker_pool(["a", "b"], None, ["a", "b"]) == ["a", "b"]
    assert narrow_worker_pool(["a", "b"], "", ["a", "b"]) == ["a", "b"]


def test_ignores_an_unknown_failed_worker() -> None:
    """A worker that is not in the pool leaves it untouched."""

    assert narrow_worker_pool(["a", "b"], "zzz", None) == ["a", "b"]


def test_preserves_order_and_drops_blanks() -> None:
    assert narrow_worker_pool(["b", "", "a", "c"], "a", None) == ["b", "c"]


# --------------------------------------------------------------- db integration


def _register(client, name: str, capabilities: list[str]) -> None:
    response = client.put(
        f"/api/v1/workers/{name}",
        json={"capabilities": capabilities, "max_running": 2},
    )
    assert response.status_code == 200, response.text


def test_narrow_pool_after_failure_materializes_and_persists(client, engine) -> None:
    from sqlalchemy import select

    from axiom.modules.graph.tables import intents

    project = client.post("/api/v1/projects", json={"title": "池收窄"}).json()
    task = client.post(
        "/api/v1/tasks",
        json={
            "project_id": project["id"],
            "title": "池收窄任务",
            "origin": "pool.example.com",
            # No explicit pool: the Intent inherits an empty ("any worker") pool.
        },
    ).json()
    _register(client, "pool-a", ["explore"])
    _register(client, "pool-b", ["explore"])

    with engine.begin() as conn:
        current = conn.execute(
            select(intents.c.worker_pool_json).where(
                intents.c.task_id == task["id"], intents.c.id == "i001"
            )
        ).scalar_one()
        assert current in ("[]", ""), current

        pool = narrow_pool_after_failure(
            conn,
            table=intents,
            run_id="i001",
            task_id=task["id"],
            pool_json=current,
            failed_worker="pool-a",
            kind="explore",
            now="2026-10-08T00:00:00Z",
        )
        assert pool, "narrowed pool must never be empty"
        assert "pool-a" not in pool
        assert pool == ["pool-b"]

        persisted = conn.execute(
            select(intents.c.worker_pool_json).where(
                intents.c.task_id == task["id"], intents.c.id == "i001"
            )
        ).scalar_one()
        assert "pool-a" not in persisted
        assert "pool-b" in persisted


def test_narrow_pool_after_failure_keeps_single_worker(client, engine) -> None:
    from sqlalchemy import select

    from axiom.modules.graph.tables import intents

    project = client.post("/api/v1/projects", json={"title": "单池"}).json()
    task = client.post(
        "/api/v1/tasks",
        json={
            "project_id": project["id"],
            "title": "单池任务",
            "origin": "pool.example.com",
            "worker_pool": ["pool-a"],
        },
    ).json()
    _register(client, "pool-a", ["explore"])

    with engine.begin() as conn:
        current = conn.execute(
            select(intents.c.worker_pool_json).where(
                intents.c.task_id == task["id"], intents.c.id == "i001"
            )
        ).scalar_one()
        pool = narrow_pool_after_failure(
            conn,
            table=intents,
            run_id="i001",
            task_id=task["id"],
            pool_json=current,
            failed_worker="pool-a",
            kind="explore",
            now="2026-10-08T00:00:00Z",
        )
        # No alternative exists, so the pool must stay as it was.
        assert pool == ["pool-a"]
