from __future__ import annotations

import os

import pytest
from alembic import command
from fastapi.testclient import TestClient
from sqlalchemy import create_engine

from axiom.app import create_app
from axiom.cli import alembic_config
from axiom.shared import model_registry as _model_registry  # noqa: F401
from axiom.shared.database import create_database_engine, metadata


TEST_DATABASE_URL = os.getenv(
    "AXIOM_TEST_DATABASE_URL",
    "postgresql+psycopg://axiom_app:axiom-app-test@127.0.0.1:55433/axiom_test",
)
TEST_MIGRATOR_URL = os.getenv(
    "AXIOM_TEST_MIGRATOR_URL",
    "postgresql+psycopg://axiom_migrator:axiom-migrator-test@127.0.0.1:55433/axiom_test",
)


@pytest.fixture(scope="session", autouse=True)
def postgresql_schema():
    command.upgrade(alembic_config(TEST_MIGRATOR_URL), "postgresql@head")
    yield


def _truncate_database() -> None:
    admin = create_engine(TEST_MIGRATOR_URL, pool_pre_ping=True)
    try:
        tables = ", ".join(
            f'"{table.schema}"."{table.name}"' for table in reversed(metadata.sorted_tables)
        )
        with admin.begin() as conn:
            conn.exec_driver_sql("SET LOCAL ROLE axiom_owner")
            conn.exec_driver_sql(f"TRUNCATE TABLE {tables} CASCADE")
    finally:
        admin.dispose()


@pytest.fixture()
def engine():
    _truncate_database()
    engine = create_database_engine(TEST_DATABASE_URL, application_name="axiom-pytest")
    try:
        yield engine
    finally:
        engine.dispose()
        _truncate_database()


@pytest.fixture()
def data_dir(tmp_path):
    return tmp_path / "data"


@pytest.fixture()
def client(engine, data_dir):
    with TestClient(create_app(engine=engine, data_dir=data_dir)) as test_client:
        yield test_client
