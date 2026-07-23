from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from axiom.app import create_app
from axiom.shared.database import create_sqlite_engine, metadata
from axiom.shared import model_registry as _model_registry  # noqa: F401


@pytest.fixture()
def engine(tmp_path):
    engine = create_sqlite_engine(tmp_path / "test.db")
    metadata.create_all(engine)
    try:
        yield engine
    finally:
        engine.dispose()


@pytest.fixture()
def data_dir(tmp_path):
    return tmp_path / "data"


@pytest.fixture()
def client(engine, data_dir):
    with TestClient(create_app(engine=engine, data_dir=data_dir)) as test_client:
        yield test_client
