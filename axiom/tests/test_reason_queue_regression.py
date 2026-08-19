"""Regression test for the single-task Reason queue feature (live DB).

Exercises against a throwaway scratch task (t_reg_*) using the real services:

  R1 claim fix        - claim of a pending ReasonRun succeeds (was AttributeError -> HTTP 500)
  R2 reconcile fix    - queued pending run is created while another run is in-flight
                         (was UnboundLocalError -> reconcile HTTP 500)
  R3 schema fix       - DB allows one in-flight + one queued pending run per task
                         (was uq_reason_runs_one_open_per_task unique violation)
  R4 serialization    - claim of the queued run is rejected while in-flight exists
  R5 queue limit      - reason_queue_limit caps the number of queued pending runs
  R6 dedup            - repeated reconcile with identical input does not create duplicates
  R7 late-binding     - start_run refreshes source_fact_ids to the latest unplanned facts
  R8 disabled worker  - claim returns a clean ConflictError instead of AttributeError

Run:  .venv/bin/python tests/test_reason_queue_regression.py
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

from sqlalchemy import text, update

from axiom.shared.database import create_database_engine, database_url_from_env
from axiom.composition import Container
from axiom.shared.clock import utcnow
from axiom.shared.errors import ConflictError
from axiom.modules.workers.tables import workers

WORKER = "deepseek-v4-flash"

_results: list[tuple[str, bool, str]] = []


def check(step: str, ok: bool, detail: str = "") -> None:
    _results.append((step, ok, detail))
    tag = "PASS" if ok else "FAIL"
    print(f"[{tag}] {step}" + (f"  ({detail})" if detail else ""))


def main() -> int:
    engine = create_database_engine(database_url_from_env())
    container = Container.build(
        engine,
        data_dir=Path(os.path.expanduser("~/.local/share/axiom-v3")),
        allow_startup_writes=False,
    )
    task_id = f"t_reg_{int(time.time())}"
    now = utcnow()
    worker_was_enabled = True
    old_queue_limit = 3
    owned: list[tuple[str, str]] = []  # (run_id, lease_id) leases I hold

    def set_worker_disabled(disabled: bool) -> None:
        with engine.begin() as conn:
            conn.execute(
                update(workers)
                .where(workers.c.name == WORKER)
                .values(disabled=disabled, updated_at=utcnow())
            )

    try:
        with engine.connect() as conn:
            project_id = conn.execute(
                text("select project_id from axiom.tasks where id='t_175'")
            ).scalar()
            scope_json = conn.execute(
                text("select scope_json from axiom.tasks where id='t_175'")
            ).scalar()
            row = conn.execute(
                text("select disabled from axiom.workers where name=:w"), {"w": WORKER}
            ).first()
            worker_was_enabled = not bool(row[0]) if row else True

        settings_before = container.settings.get()
        old_queue_limit = settings_before.config.reason_queue_limit

        # ---- setup: scratch task + completed bootstrap + one fact ----
        with engine.begin() as conn:
            conn.execute(
                text(
                    "insert into axiom.tasks (id, project_id, title, origin_host, origin_scheme,"
                    " origin_input, goal, status, phase, scope_json, worker_pool_json,"
                    " guard_enabled, external_report_enabled, legacy_readonly, created_at, updated_at)"
                    " values (:id, :pid, :title, 'regression', 'https', 'regression',"
                    " 'regression', 'running', 'waiting', :scope, :pool, false, false, false,"
                    " :now, :now)"
                ),
                {"id": task_id, "pid": project_id, "title": "reason-queue-regression",
                 "pool": json.dumps([WORKER]), "scope": json.dumps(scope_json or {}), "now": now},
            )
            conn.execute(
                text(
                    "insert into axiom.intents (id, task_id, kind, description, priority, status,"
                    " creator, worker_pool_json, targets_json, retry_count, infrastructure_retry_count,"
                    " activity_kind, activity_summary, activity_tool, last_failure_kind, failure_class,"
                    " human_intervention_required, human_intervention_reason, assessment_status,"
                    " assessment_reason, created_at, archived) values (:id, :tid, 'bootstrap',"
                    " 'regression bootstrap', 50, 'completed', 'regression', '[]', '[]', 0, 0, '',"
                    " '', '', '', '', false, '', 'completed', '', :now, false)"
                ),
                {"id": task_id + "_b1", "tid": task_id, "now": now},
            )
            conn.execute(
                text(
                    "insert into axiom.facts (task_id, id, description, created_at, archived)"
                    " values (:tid, 'f_reg_1', 'fact 1', :now, false)"
                ),
                {"tid": task_id, "now": now},
            )

        # ---- R8: claim while worker disabled must be a clean ConflictError ----
        set_worker_disabled(True)
        created = container.reason.reconcile_task(task_id)
        check("R2a: reconcile creates first run", created is not None, f"run={created}")

        with engine.connect() as conn:
            pending = container.reason.repository.pending_runs_for_task(conn, task_id)
        check("R6a: exactly one pending run after first reconcile", len(pending) == 1,
              f"pending={len(pending)}")
        run1 = pending[0].id

        try:
            container.dispatch.claim_reason_run(
                task_id=task_id, reason_run_id=run1, worker_name=WORKER
            )
            check("R8: claim with disabled worker raised clean ConflictError", False,
                  "claim unexpectedly succeeded")
        except ConflictError as exc:
            check("R8: claim with disabled worker raised clean ConflictError",
                  "disabled" in str(exc).lower(), str(exc))
        except AttributeError as exc:
            check("R8: claim with disabled worker raised clean ConflictError", False,
                  f"AttributeError again: {exc}")
        except Exception as exc:
            check("R8: claim with disabled worker raised clean ConflictError", False,
                  f"{type(exc).__name__}: {exc}")

        # ---- R1: re-enable worker, claim + start first run ----
        set_worker_disabled(False)
        lease1 = None
        for _ in range(20):
            try:
                claimed = container.dispatch.claim_reason_run(
                    task_id=task_id, reason_run_id=run1, worker_name=WORKER
                )
                lease1 = claimed.lease_id
                owned.append((run1, lease1))
                break
            except ConflictError:
                time.sleep(0.2)
        if lease1 is None:
            # The live runner may have sniped the claim; verify it was claimed anyway.
            with engine.connect() as conn:
                row = container.reason.repository.get_run(conn, task_id, run1)
            if row is not None and row.status == "claimed" and row.worker == WORKER:
                check("R1: claim succeeds (runner sniped, run claimed by worker)", True,
                      f"status={row.status}")
                lease1 = row.lease_id or ""
            else:
                check("R1: claim succeeds", False,
                      f"claim failed 20x; run status={row.status if row else '?'}")
                raise SystemExit(1)
        else:
            started = container.reason.start_run(task_id, run1, lease_id=lease1)
            check("R1: claim succeeds", started.worker_name == WORKER, f"run={run1}")

        with engine.connect() as conn:
            row = container.reason.repository.get_run(conn, task_id, run1)
            assert row is not None and row.status == "running"
            check("R2b: run1 reached running", True, f"status={row.status}")

        # ---- R2/R3: insert late fact, reconcile -> queued pending run ----
        with engine.begin() as conn:
            conn.execute(
                text(
                    "insert into axiom.facts (task_id, id, description, created_at, archived)"
                    " values (:tid, 'f_reg_2', 'fact 2 (late)', :now, false)"
                ),
                {"tid": task_id, "now": utcnow()},
            )
        queued = container.reason.reconcile_task(task_id)
        check("R2c: queued run created while in-flight", queued is not None and queued != run1,
              f"queued={queued}")

        with engine.connect() as conn:
            pending = container.reason.repository.pending_runs_for_task(conn, task_id)
            in_flight = container.reason.repository.in_flight_for_task(conn, task_id)
        check("R3a: one in-flight + one pending (schema allows multiple open)",
              in_flight is not None and in_flight.id == run1 and len(pending) == 1,
              f"in_flight={in_flight.id if in_flight else None} pending={len(pending)}")
        run2 = pending[0].id
        with engine.connect() as conn:
            row2 = container.reason.repository.get_run(conn, task_id, run2)
        check("R2d: queued run marked as queued", row2 is not None
              and bool(row2.trigger_payload.get("queued")) is True, f"trigger={row2.trigger}")

        # ---- R6b: repeated reconcile with same input must not duplicate ----
        again = container.reason.reconcile_task(task_id)
        with engine.connect() as conn:
            pending = container.reason.repository.pending_runs_for_task(conn, task_id)
        check("R6b: no duplicate pending after repeated reconcile", len(pending) == 1,
              f"pending={len(pending)} again->{again}")

        # ---- R4: claim of queued run rejected while in-flight ----
        try:
            container.dispatch.claim_reason_run(
                task_id=task_id, reason_run_id=run2, worker_name=WORKER
            )
            check("R4: queued run claim rejected while in-flight", False, "claim succeeded")
        except ConflictError as exc:
            check("R4: queued run claim rejected while in-flight", True, str(exc))
        except Exception as exc:
            check("R4: queued run claim rejected while in-flight", False,
                  f"{type(exc).__name__}: {exc}")

        # ---- R5: queue_limit=1 caps pending at 1 ----
        current = container.settings.get()
        container.settings.update(
            current.config.model_copy(update={"reason_queue_limit": 1}),
            actor="regression-test",
            expected_version=current.version,
        )
        with engine.begin() as conn:
            conn.execute(
                text(
                    "insert into axiom.facts (task_id, id, description, created_at, archived)"
                    " values (:tid, 'f_reg_3', 'fact 3', :now, false)"
                ),
                {"tid": task_id, "now": utcnow()},
            )
        container.reason.reconcile_task(task_id)
        with engine.connect() as conn:
            pending = container.reason.repository.pending_runs_for_task(conn, task_id)
        check("R5: queue_limit=1 caps queued pending at 1", len(pending) == 1,
              f"pending={len(pending)}")
        current = container.settings.get()
        container.settings.update(
            current.config.model_copy(update={"reason_queue_limit": old_queue_limit}),
            actor="regression-test",
            expected_version=current.version,
        )

        # ---- R7: free the in-flight slot, claim + start queued run, check late-binding ----
        with engine.begin() as conn:
            released = container.reason.repository.release(
                conn,
                task_id=task_id,
                run_id=run1,
                lease_id=lease1,
                failure_kind="regression_terminal",
                failure_class="execution",
                next_retry_at=None,
                terminal=True,
                now=utcnow(),
            )
            check("R7a: terminal release of run1 frees in-flight slot", released,
                  f"run={run1}")

        lease2 = None
        for _ in range(20):
            try:
                claimed = container.dispatch.claim_reason_run(
                    task_id=task_id, reason_run_id=run2, worker_name=WORKER
                )
                lease2 = claimed.lease_id
                owned.append((run2, lease2))
                break
            except ConflictError:
                time.sleep(0.2)
        if lease2 is None:
            with engine.connect() as conn:
                row2 = container.reason.repository.get_run(conn, task_id, run2)
            check("R7b: queued run becomes claimable after in-flight ends",
                  row2 is not None and row2.status == "claimed" and row2.worker == WORKER,
                  f"status={row2.status if row2 else '?'}")
            lease2 = row2.lease_id or ""
        else:
            container.reason.start_run(task_id, run2, lease_id=lease2)
            check("R7b: queued run becomes claimable after in-flight ends", True)

        with engine.connect() as conn:
            sources = container.reason.repository.source_ids(conn, task_id, run2)
        check("R7c: late-binding refreshed source_fact_ids",
              "f_reg_3" in sources and "f_reg_1" in sources,
              f"sources={sources}")

        with engine.connect() as conn:
            row2 = container.reason.repository.get_run(conn, task_id, run2)
        check("R2e: queued run started after in-flight completion",
              row2 is not None and row2.status == "running", f"status={row2.status}")

    finally:
        # ---- cleanup: restore worker/settings, terminal-release leases, archive task ----
        try:
            set_worker_disabled(not worker_was_enabled)
        except Exception:
            pass
        try:
            current = container.settings.get()
            container.settings.update(
                current.config.model_copy(update={"reason_queue_limit": old_queue_limit}),
                actor="regression-test",
                expected_version=current.version,
            )
        except Exception:
            pass
        for run_id, lease_id in owned:
            try:
                with engine.begin() as conn:
                    container.reason.repository.release(
                        conn,
                        task_id=task_id,
                        run_id=run_id,
                        lease_id=lease_id,
                        failure_kind="regression_cleanup",
                        failure_class="execution",
                        next_retry_at=None,
                        terminal=True,
                        now=utcnow(),
                    )
            except Exception:
                pass
        try:
            with engine.begin() as conn:
                conn.execute(
                    text(
                        "update axiom.tasks set archived=true, status='stopped', phase='none'"
                        " where id=:id"
                    ),
                    {"id": task_id},
                )
        except Exception:
            pass

    print()
    failed = [r for r in _results if not r[1]]
    for step, ok, detail in _results:
        print(("FAIL" if not ok else "PASS") + "  " + step + (f"  ({detail})" if detail else ""))
    print()
    if failed:
        print(f"REG RESULT: {len(_results) - len(failed)}/{len(_results)} passed")
        return 1
    print(f"REG RESULT: ALL {len(_results)} PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
