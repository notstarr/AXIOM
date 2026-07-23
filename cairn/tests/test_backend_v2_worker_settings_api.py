from __future__ import annotations

from axiom.api.v1.dispatcher import WorkerConnectionRequest, _worker_connection
from axiom.infrastructure.database.engine import create_sqlite_engine
from axiom.infrastructure.database.metadata import metadata
from axiom.modules.workers.schemas import UpsertWorker
from axiom.modules.workers.service import WorkerService


def test_worker_connection_reads_migrated_nested_env(tmp_path) -> None:
    engine = create_sqlite_engine(tmp_path / "v2.db")
    metadata.create_all(engine)
    WorkerService(engine).upsert(
        UpsertWorker(
            name="worker-a",
            capabilities=["bootstrap", "reason", "explore"],
            max_running=1,
            config={
                "env": {
                    "PI_BASE_URL": "https://models.example/v1",
                    "PI_API_KEY": "plain-key",
                    "PI_MODEL": "model-a",
                    "PI_PROVIDER_API": "openai-responses",
                }
            },
        )
    )

    resolved = _worker_connection(WorkerConnectionRequest(), "worker-a", engine)

    assert resolved.base_url == "https://models.example/v1"
    assert resolved.api_key == "plain-key"
    assert resolved.model == "model-a"
