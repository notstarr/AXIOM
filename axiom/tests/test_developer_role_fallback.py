"""Gateway developer-role capability: probe, persistence and Pi wiring.

Pi sends the system prompt as OpenAI's ``developer`` role whenever the model
advertises reasoning.  Gateways that only know the older ``system`` role reject
that with an opaque 400, so AXIOM probes the capability once per Worker, stores
the verdict, and only downgrades on a confirmed refusal.
"""

from __future__ import annotations

import httpx
import pytest
from pydantic import ValidationError

from axiom.dispatcher.pi import PiBootstrapExecutor
from axiom.dispatcher.runner import AxiomRunner
from axiom.modules.workers.pi_config import PiWorkerConfig, probe_developer_role
from axiom.modules.workers.repository import WorkerRepository
from axiom.modules.workers.schemas import UpsertWorker, Worker


PROBE_URL = "https://gateway.example/v1/chat/completions"


class FakeGateway:
    """Stand in for ``httpx.post`` and record the role of every probe."""

    def __init__(self, *, developer: bool | None, system: bool | None) -> None:
        self.developer = developer
        self.system = system
        self.roles: list[str] = []

    def __call__(self, url, *, headers=None, json=None, timeout=None, proxy=None):  # noqa: A002
        role = json["messages"][0]["role"]
        self.roles.append(role)
        accepted = self.developer if role == "developer" else self.system
        if accepted is None:
            raise httpx.ConnectError("gateway unreachable", request=httpx.Request("POST", url))
        return httpx.Response(
            status_code=200 if accepted else 400,
            json={"ok": accepted},
            request=httpx.Request("POST", url),
        )


def _probe(gateway: FakeGateway, monkeypatch: pytest.MonkeyPatch) -> bool | None:
    monkeypatch.setattr(httpx, "post", gateway)
    return probe_developer_role("https://gateway.example", "key", "auto", "openai-completions")


def _worker(config: dict, *, verdict: bool | None = None) -> Worker:
    """Build a Worker.

    ``config`` carries the Pi settings; ``verdict`` is the persisted column
    (what the Runner reads) — they are separate inputs on purpose.
    """

    return Worker(
        name="worker-a",
        worker_type="pi",
        capabilities=["explore"],
        max_running=1,
        priority=0,
        config=config,
        disabled=False,
        developer_role_supported=verdict,
        created_at="2026-10-08T00:00:00Z",
        updated_at="2026-10-08T00:00:00Z",
    )


def _models_payload(config: dict, *, verdict: bool | None = None) -> dict:
    executor = PiBootstrapExecutor(_worker(config, verdict=verdict))
    payload = executor._models_payload()
    assert payload is not None
    return payload


# --------------------------------------------------------------------------- probe


def test_probe_reports_support_when_developer_is_accepted(monkeypatch) -> None:
    gateway = FakeGateway(developer=True, system=True)
    assert _probe(gateway, monkeypatch) is True
    # Supporting the new role costs exactly one request.
    assert gateway.roles == ["developer"]


def test_probe_reports_refusal_only_when_system_still_works(monkeypatch) -> None:
    gateway = FakeGateway(developer=False, system=True)
    assert _probe(gateway, monkeypatch) is False
    assert gateway.roles == ["developer", "system"]


def test_probe_is_inconclusive_when_both_roles_fail(monkeypatch) -> None:
    """A gateway outage must never be recorded as a role refusal."""

    gateway = FakeGateway(developer=False, system=False)
    assert _probe(gateway, monkeypatch) is None
    assert gateway.roles == ["developer", "system"]


def test_probe_is_inconclusive_on_transport_failure(monkeypatch) -> None:
    gateway = FakeGateway(developer=None, system=None)
    assert _probe(gateway, monkeypatch) is None
    assert gateway.roles == ["developer"]


def test_probe_skips_anthropic_and_responses_apis() -> None:
    # No HTTP call is attempted for APIs that have no developer role.
    assert probe_developer_role("https://x", "k", "m", "anthropic-messages") is None
    assert probe_developer_role("https://x", "k", "m", "openai-responses") is None
    assert probe_developer_role("", "k", "m", "openai-completions") is None
    assert probe_developer_role("https://x", "k", "", "openai-completions") is None


# ------------------------------------------------------------------ models.json


def _pi_config(**overrides) -> dict:
    config = {
        "base_url": "https://gateway.example",
        "api_key": "key",
        "model": "auto",
        "thinking_enabled": True,
    }
    config.update(overrides)
    return config


def test_models_payload_keeps_developer_by_default() -> None:
    model = _models_payload(_pi_config())["providers"]["axiom"]["models"][0]
    assert "compat" not in model


def test_models_payload_keeps_developer_when_probe_confirms_support() -> None:
    config = _pi_config(developer_role_supported=True)
    model = _models_payload(config)["providers"]["axiom"]["models"][0]
    assert "compat" not in model


def test_models_payload_pins_system_when_probe_confirmed_refusal() -> None:
    config = _pi_config(developer_role_supported=False)
    model = _models_payload(config)["providers"]["axiom"]["models"][0]
    assert model["compat"] == {"supportsDeveloperRole": False}


def test_models_payload_reads_the_worker_column_not_only_the_config() -> None:
    """The probed verdict is a Worker column; config_json never carries it.

    Production stores it via ``set_capability``, so a payload built only from
    ``worker.config`` would silently keep Pi on ``developer``.
    """

    model = _models_payload(_pi_config(), verdict=False)["providers"]["axiom"]["models"][0]
    assert model["compat"] == {"supportsDeveloperRole": False}


def test_models_payload_keeps_developer_when_the_column_confirms_support() -> None:
    model = _models_payload(_pi_config(), verdict=True)["providers"]["axiom"]["models"][0]
    assert "compat" not in model


def test_models_payload_keeps_developer_when_the_column_is_unprobed() -> None:
    model = _models_payload(_pi_config(), verdict=None)["providers"]["axiom"]["models"][0]
    assert "compat" not in model


# ------------------------------------------------------------------- repository


def _upsert(repository: WorkerRepository, conn, config: dict) -> Worker:
    return repository.upsert(
        conn,
        name="worker-a",
        command=UpsertWorker(
            worker_type="pi",
            capabilities=["explore"],
            max_running=1,
            priority=0,
            config=config,
        ),
        now="2026-10-08T00:00:00Z",
    )


def test_upsert_resets_verdict_when_the_provider_changes(engine) -> None:
    repository = WorkerRepository()
    with engine.begin() as conn:
        _upsert(repository, conn, _pi_config())
        repository.set_capability(
            conn, name="worker-a", developer_role_supported=False, now="2026-10-08T00:00:01Z"
        )

        # Same provider: the learned verdict survives a save round-trip.
        kept = _upsert(repository, conn, _pi_config(api_key="key"))
        assert kept.developer_role_supported is False

        # A different gateway invalidates it.
        moved = _upsert(repository, conn, _pi_config(base_url="https://other.example"))
        assert moved.developer_role_supported is None


def test_upsert_resets_verdict_when_the_model_changes(engine) -> None:
    repository = WorkerRepository()
    with engine.begin() as conn:
        _upsert(repository, conn, _pi_config())
        repository.set_capability(
            conn, name="worker-a", developer_role_supported=True, now="2026-10-08T00:00:01Z"
        )
        rotated = _upsert(repository, conn, _pi_config(model="auto-v2"))
        assert rotated.developer_role_supported is None


def test_upsert_keeps_verdict_across_unrelated_edits(engine) -> None:
    repository = WorkerRepository()
    with engine.begin() as conn:
        _upsert(repository, conn, _pi_config())
        repository.set_capability(
            conn, name="worker-a", developer_role_supported=False, now="2026-10-08T00:00:01Z"
        )
        edited = repository.upsert(
            conn,
            name="worker-a",
            command=UpsertWorker(
                worker_type="pi",
                capabilities=["explore"],
                max_running=8,
                priority=3,
                config=_pi_config(),
                disabled=True,
            ),
            now="2026-10-08T00:00:02Z",
        )
        assert edited.developer_role_supported is False


# ---------------------------------------------------------------- config shape


def test_config_round_trips_the_verdict() -> None:
    assert PiWorkerConfig.model_validate({}).developer_role_supported is None
    assert PiWorkerConfig.model_validate({"developer_role_supported": False}).developer_role_supported is False
    assert PiWorkerConfig.model_validate({"developer_role_supported": True}).developer_role_supported is True


def test_config_rejects_non_boolean_verdict() -> None:
    with pytest.raises(ValidationError):
        PiWorkerConfig.model_validate({"developer_role_supported": "maybe"})


# ------------------------------------------------------------------ runner gate


class FakeControlPlane:
    """Minimal control plane exposing only what the capability gate touches."""

    def __init__(self, worker: Worker) -> None:
        self.worker = worker
        self.persisted: list[tuple[str, bool | None]] = []

    def set_worker_capability(self, name: str, verdict: bool | None) -> None:
        self.persisted.append((name, verdict))


def _runner(control_plane: FakeControlPlane) -> AxiomRunner:
    return AxiomRunner(control_plane, executor_factory=lambda worker, work: object())


def _patch_probe(monkeypatch, verdict: bool | None) -> list[str]:
    from axiom.dispatcher import runner as runner_module

    seen: list[str] = []

    def fake_probe(base_url, api_key, model, provider_api, *, proxy=None, timeout=None):
        seen.append(base_url)
        return verdict

    monkeypatch.setattr(runner_module, "probe_developer_role", fake_probe)
    return seen


def test_runner_probes_unknown_worker_and_persists_verdict(monkeypatch) -> None:
    worker = _worker(_pi_config())
    control_plane = FakeControlPlane(worker)
    seen = _patch_probe(monkeypatch, False)

    result = _runner(control_plane)._probe_worker_capability(worker)

    assert result.developer_role_supported is False
    assert control_plane.persisted == [("worker-a", False)]
    assert seen == ["https://gateway.example"]


def test_runner_skips_probe_when_verdict_is_already_known(monkeypatch) -> None:
    """A stored verdict must not cost an HTTP round-trip on every Lease."""

    worker = _worker(_pi_config(), verdict=True)
    control_plane = FakeControlPlane(worker)
    seen = _patch_probe(monkeypatch, False)

    result = _runner(control_plane)._probe_worker_capability(worker)

    assert result.developer_role_supported is True
    assert seen == []
    assert control_plane.persisted == []


def test_runner_does_not_persist_an_inconclusive_verdict(monkeypatch) -> None:
    worker = _worker(_pi_config())
    control_plane = FakeControlPlane(worker)
    _patch_probe(monkeypatch, None)

    result = _runner(control_plane)._probe_worker_capability(worker)

    assert result.developer_role_supported is None
    assert control_plane.persisted == []


def test_runner_skips_workers_without_a_usable_provider(monkeypatch) -> None:
    seen = _patch_probe(monkeypatch, False)
    control_plane = FakeControlPlane(_worker({}))

    for config in (
        {"model": "auto"},  # no base_url
        {"base_url": "https://gateway.example"},  # no model
    ):
        result = _runner(control_plane)._probe_worker_capability(_worker(config))
        assert result.developer_role_supported is None

    assert seen == []


def test_runner_skips_non_pi_workers(monkeypatch) -> None:
    seen = _patch_probe(monkeypatch, False)
    worker = _worker(_pi_config()).model_copy(update={"worker_type": "other"})
    result = _runner(FakeControlPlane(worker))._probe_worker_capability(worker)

    assert result.developer_role_supported is None
    assert seen == []


def test_runner_survives_a_control_plane_that_cannot_persist(monkeypatch) -> None:
    """Losing the write-back must not change the verdict used for this Lease."""

    _patch_probe(monkeypatch, False)
    control_plane = FakeControlPlane(_worker(_pi_config()))

    def boom(name, verdict):
        raise RuntimeError("control plane down")

    control_plane.set_worker_capability = boom  # type: ignore[method-assign]
    result = _runner(control_plane)._probe_worker_capability(control_plane.worker)

    assert result.developer_role_supported is False
