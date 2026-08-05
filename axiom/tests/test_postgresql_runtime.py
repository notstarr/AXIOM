from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy.exc import DBAPIError

from axiom.shared.database import create_database_engine, run_transaction
from axiom.shared.maintenance import read_maintenance, write_maintenance


class _RetryableDatabaseError(Exception):
    sqlstate = "40001"


class _PermanentDatabaseError(Exception):
    sqlstate = "23505"


def _dbapi_error(original: Exception) -> DBAPIError:
    return DBAPIError(statement="SELECT 1", params={}, orig=original)


def test_run_transaction_retries_serialization_failures(monkeypatch) -> None:
    attempts = 0

    class FakeContext:
        def __enter__(self):
            return object()

        def __exit__(self, exc_type, exc, traceback):
            return False

    class FakeEngine:
        def begin(self):
            return FakeContext()

    def operation(_connection):
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise _dbapi_error(_RetryableDatabaseError("retry"))
        return "ok"

    monkeypatch.setattr("axiom.shared.database.time.sleep", lambda _seconds: None)
    monkeypatch.setattr("axiom.shared.database.random.uniform", lambda _start, _end: 0)

    assert run_transaction(FakeEngine(), operation) == "ok"
    assert attempts == 3


def test_run_transaction_does_not_retry_nontransactional_errors(monkeypatch) -> None:
    attempts = 0

    class FakeContext:
        def __enter__(self):
            return object()

        def __exit__(self, exc_type, exc, traceback):
            return False

    class FakeEngine:
        def begin(self):
            return FakeContext()

    def operation(_connection):
        nonlocal attempts
        attempts += 1
        raise _dbapi_error(_PermanentDatabaseError("permanent"))

    monkeypatch.setattr("axiom.shared.database.time.sleep", lambda _seconds: None)
    with pytest.raises(DBAPIError):
        run_transaction(FakeEngine(), operation)
    assert attempts == 1


def test_runtime_engine_rejects_sqlite_urls() -> None:
    with pytest.raises(ValueError, match="requires a PostgreSQL"):
        create_database_engine("sqlite+pysqlite:///:memory:")


def test_maintenance_state_is_atomic_and_invalid_files_fail_closed(tmp_path: Path) -> None:
    normal = read_maintenance(tmp_path)
    assert normal.state == "normal"

    drained = write_maintenance(
        tmp_path,
        state="drain",
        actor="pytest",
        reason="migration rehearsal",
    )
    assert drained.state == "drain"
    assert read_maintenance(tmp_path) == drained

    (tmp_path / "maintenance.json").write_text("{", encoding="utf-8")
    failed = read_maintenance(tmp_path)
    assert failed.state == "frozen"
    assert failed.actor == "system.fail-closed"
