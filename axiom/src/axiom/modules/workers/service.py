from __future__ import annotations

import httpx
from collections import defaultdict
from sqlalchemy import Engine, func, select

from axiom.modules.graph.tables import intents
from axiom.modules.reason.tables import reason_runs
from axiom.modules.vulnerability_reviews.tables import vulnerability_review_runs
from axiom.modules.phase_capabilities.schemas import PhaseBackend, WorkerPhase
from axiom.modules.workers.pi_config import PiWorkerConfig, canonicalize_pi_worker_config
from axiom.modules.workers.repository import WorkerRepository
from axiom.modules.workers.schemas import (
    UpsertWorker,
    Worker,
    WorkerConnectionRequest,
    WorkerConnectionResult,
    WorkerModel,
    WorkerModelsResult,
)
from axiom.shared.clock import utcnow
from axiom.shared.clock import parse_utc
from axiom.shared.database import immediate_transaction
from axiom.shared.errors import ConflictError, NotFoundError


class WorkerService:
    def __init__(self, engine: Engine):
        self.engine = engine
        self.repository = WorkerRepository()

    def upsert(self, name: str, command: UpsertWorker) -> Worker:
        name = self._normalize_name(name)
        if command.worker_type == "pi":
            command = command.model_copy(
                update={"config": canonicalize_pi_worker_config(command.config)}
            )
        with self.engine.begin() as conn:
            return self._normalize_worker(
                self.repository.upsert(conn, name=name, command=command, now=utcnow())
            )

    def get(self, name: str) -> Worker:
        with self.engine.connect() as conn:
            worker = self.repository.get(conn, name)
        if worker is None:
            raise NotFoundError(f"Worker not found: {name}")
        return self._normalize_worker(worker)

    def list(self) -> list[Worker]:
        with self.engine.connect() as conn:
            return [self._normalize_worker(worker) for worker in self.repository.list(conn)]

    def phase_overview(self, phase: str | None = None) -> list[WorkerPhase]:
        phases = [self._phase(phase)] if phase else ["bootstrap", "reason", "explore", "review"]
        with self.engine.connect() as conn:
            workers = [self._normalize_worker(item) for item in self.repository.list(conn)]
            running_by_worker: dict[str, int] = defaultdict(int)
            running_by_worker_phase: dict[tuple[str, str], int] = defaultdict(int)
            for row in conn.execute(
                select(intents.c.worker, intents.c.kind, func.count().label("count"))
                .where(
                    intents.c.worker.is_not(None),
                    intents.c.status.in_(["claimed", "running", "concluding"]),
                )
                .group_by(intents.c.worker, intents.c.kind)
            ).mappings():
                worker_name = str(row["worker"])
                count = int(row["count"])
                running_by_worker[worker_name] += count
                running_by_worker_phase[(worker_name, str(row["kind"]))] += count
            for row in conn.execute(
                select(reason_runs.c.worker, func.count().label("count"))
                .where(
                    reason_runs.c.worker.is_not(None),
                    reason_runs.c.status.in_(["claimed", "running", "concluding"]),
                )
                .group_by(reason_runs.c.worker)
            ).mappings():
                worker_name = str(row["worker"])
                count = int(row["count"])
                running_by_worker[worker_name] += count
                running_by_worker_phase[(worker_name, "reason")] += count
            for row in conn.execute(
                select(vulnerability_review_runs.c.worker, func.count().label("count"))
                .where(
                    vulnerability_review_runs.c.worker.is_not(None),
                    vulnerability_review_runs.c.status.in_(["claimed", "running", "concluding"]),
                )
                .group_by(vulnerability_review_runs.c.worker)
            ).mappings():
                worker_name = str(row["worker"])
                count = int(row["count"])
                running_by_worker[worker_name] += count
                running_by_worker_phase[(worker_name, "review")] += count
        now = parse_utc(utcnow())
        result: list[WorkerPhase] = []
        titles = {
            "bootstrap": "Bootstrap Worker",
            "reason": "Reason Worker",
            "explore": "Explore Worker",
            "review": "Reviewer Worker",
        }
        for item_phase in phases:
            backends: list[PhaseBackend] = []
            for worker in workers:
                if item_phase not in worker.capabilities:
                    continue
                config = (
                    PiWorkerConfig.model_validate(
                        canonicalize_pi_worker_config(worker.config)
                    )
                    if worker.worker_type == "pi"
                    else None
                )
                shared_running = running_by_worker.get(worker.name, 0)
                running = running_by_worker_phase.get((worker.name, item_phase), 0)
                if worker.disabled:
                    state = "disabled"
                elif worker.cooldown_until and parse_utc(worker.cooldown_until) > now:
                    state = "cooldown"
                else:
                    state = "ready"
                backends.append(
                    PhaseBackend(
                        name=worker.name,
                        worker_type=worker.worker_type,
                        model=config.model if config else "",
                        context_window=config.context_window if config else 0,
                        thinking_enabled=config.thinking_enabled if config else False,
                        thinking_level=config.effective_thinking_level if config else "off",
                        max_running=worker.max_running,
                        running=running,
                        shared_running=shared_running,
                        available=(
                            max(0, worker.max_running - shared_running)
                            if state == "ready"
                            else 0
                        ),
                        priority=worker.priority,
                        disabled=worker.disabled,
                        connection_state=state,
                        capabilities=worker.capabilities,
                    )
                )
            backends.sort(key=lambda item: (item.priority, item.name))
            result.append(
                WorkerPhase(
                    phase=item_phase,
                    title=titles[item_phase],
                    backends=backends,
                    running=sum(item.running for item in backends),
                    capacity=sum(item.max_running for item in backends if not item.disabled),
                    available=sum(item.available for item in backends),
                )
            )
        return result

    def set_phase_backends(self, phase: str, backend_names: list[str]) -> WorkerPhase:
        phase = self._phase(phase)
        selected = set(backend_names)
        with immediate_transaction(self.engine) as conn:
            workers = self.repository.list(conn)
            known = {worker.name for worker in workers}
            missing = selected - known
            if missing:
                raise NotFoundError(
                    "Worker backends not found: " + ", ".join(sorted(missing))
                )
            now = utcnow()
            for worker in workers:
                capabilities = [item for item in worker.capabilities if item != phase]
                if worker.name in selected:
                    capabilities.append(phase)
                self.repository.upsert(
                    conn,
                    name=worker.name,
                    command=UpsertWorker(
                        worker_type=worker.worker_type,
                        capabilities=capabilities,
                        max_running=worker.max_running,
                        priority=worker.priority,
                        config=worker.config,
                        disabled=worker.disabled,
                    ),
                    now=now,
                )
        return self.phase_overview(phase)[0]

    def set_cooldown(self, name: str, cooldown_until: str | None) -> Worker:
        with self.engine.begin() as conn:
            worker = self.repository.set_cooldown(
                conn,
                name=name,
                cooldown_until=cooldown_until,
                now=utcnow(),
            )
        if worker is None:
            raise NotFoundError(f"Worker not found: {name}")
        return self._normalize_worker(worker)

    def delete(self, name: str) -> None:
        with self.engine.begin() as conn:
            worker = self.repository.get(conn, name)
            if worker is None:
                raise NotFoundError(f"Worker not found: {name}")
            active = conn.execute(
                select(func.count())
                .select_from(intents)
                .where(
                    intents.c.worker == name,
                    intents.c.status.in_(["claimed", "running", "concluding"]),
                )
            ).scalar_one()
            if active:
                raise ConflictError("Worker with active Intents cannot be deleted")
            active_reason_runs = conn.execute(
                select(func.count())
                .select_from(reason_runs)
                .where(
                    reason_runs.c.worker == name,
                    reason_runs.c.status.in_(["claimed", "running", "concluding"]),
                )
            ).scalar_one()
            if active_reason_runs:
                raise ConflictError("Worker with active ReasonRuns cannot be deleted")
            active_review_runs = conn.execute(
                select(func.count())
                .select_from(vulnerability_review_runs)
                .where(
                    vulnerability_review_runs.c.worker == name,
                    vulnerability_review_runs.c.status.in_(
                        ["claimed", "running", "concluding"]
                    ),
                )
            ).scalar_one()
            if active_review_runs:
                raise ConflictError("Worker with active Vulnerability ReviewRuns cannot be deleted")
            self.repository.delete(conn, name)

    def list_models(
        self,
        name: str,
        *,
        base_url: str = "",
        api_key: str = "",
    ) -> WorkerModelsResult:
        connection = self._connection(
            name,
            WorkerConnectionRequest(base_url=base_url, api_key=api_key),
        )
        if not connection.base_url:
            return WorkerModelsResult(error="base_url 未配置")
        try:
            response = httpx.get(
                connection.base_url.rstrip("/") + "/models",
                headers=self._authorization(connection.api_key),
                timeout=10,
            )
        except httpx.HTTPError as exc:
            return WorkerModelsResult(error=f"请求失败：{exc}")
        if response.status_code != 200:
            return WorkerModelsResult(
                error=f"HTTP {response.status_code}: {response.text[:200]}"
            )
        try:
            payload = response.json()
        except ValueError:
            return WorkerModelsResult(error="响应不是合法 JSON")
        raw_models = payload.get("data", []) if isinstance(payload, dict) else payload
        if not isinstance(raw_models, list):
            return WorkerModelsResult(error="响应中没有模型列表")
        result: list[WorkerModel] = []
        seen: set[str] = set()
        for item in raw_models:
            if isinstance(item, str):
                model_id = item.strip()
                context_window = 200_000
            elif isinstance(item, dict):
                model_id = str(item.get("id") or item.get("name") or "").strip()
                context_window = item.get("context_length") or item.get("context_window") or 200_000
            else:
                continue
            if not model_id or model_id in seen:
                continue
            try:
                context_window = max(1, int(context_window))
            except (TypeError, ValueError):
                context_window = 200_000
            seen.add(model_id)
            result.append(
                WorkerModel(id=model_id, name=model_id, context_window=context_window)
            )
        return WorkerModelsResult(models=result)

    def test_connection(
        self,
        name: str,
        command: WorkerConnectionRequest,
    ) -> WorkerConnectionResult:
        connection = self._connection(name, command)
        if not connection.base_url or not connection.model:
            return WorkerConnectionResult(ok=False, message="base_url 和 model 必填")
        responses_api = connection.provider_api in {"responses", "openai-responses"}
        endpoint = "/responses" if responses_api else "/chat/completions"
        payload = (
            {"model": connection.model, "input": "ping", "max_output_tokens": 8}
            if responses_api
            else {
                "model": connection.model,
                "messages": [{"role": "user", "content": "ping"}],
                "max_tokens": 8,
                "temperature": 0,
            }
        )
        headers = {"Content-Type": "application/json", **self._authorization(connection.api_key)}
        try:
            response = httpx.post(
                connection.base_url.rstrip("/") + endpoint,
                headers=headers,
                json=payload,
                timeout=15,
            )
        except httpx.HTTPError as exc:
            return WorkerConnectionResult(ok=False, message=f"连接失败：{exc}")
        if 200 <= response.status_code < 300:
            return WorkerConnectionResult(ok=True, message="连接成功，模型可用")
        return WorkerConnectionResult(
            ok=False,
            message=f"HTTP {response.status_code}: {response.text[:500]}",
        )

    def _connection(
        self,
        name: str,
        command: WorkerConnectionRequest,
    ) -> WorkerConnectionRequest:
        try:
            worker = self.get(name)
        except NotFoundError:
            if command.base_url:
                return command.model_copy(
                    update={"provider_api": command.provider_api or "openai-completions"}
                )
            raise
        if worker.worker_type != "pi":
            raise ConflictError(f"Worker does not use the Pi adapter: {name}")
        config = PiWorkerConfig.model_validate(worker.config)
        return WorkerConnectionRequest(
            base_url=command.base_url.strip() or config.base_url,
            api_key=command.api_key.strip() or config.api_key,
            model=command.model.strip() or config.model,
            provider_api=command.provider_api.strip() or config.provider_api,
        )

    @staticmethod
    def _authorization(api_key: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {api_key}"} if api_key else {}

    @staticmethod
    def _normalize_name(name: str) -> str:
        value = name.strip()
        if not value:
            raise ConflictError("Worker name must not be empty")
        if len(value) > 128:
            raise ConflictError("Worker name is too long")
        return value

    @staticmethod
    def _phase(value: str | None) -> str:
        phase = str(value or "").strip().lower()
        if phase not in {"bootstrap", "reason", "explore", "review"}:
            raise ConflictError(f"Invalid Worker phase: {phase}")
        return phase

    @staticmethod
    def _normalize_worker(worker: Worker) -> Worker:
        """Expose legacy Pi Workers in the same shape used by new leases.

        Existing rows are intentionally not rewritten during a read.  They
        still receive the split thinking controls in API responses and in the
        Runner's next lease, while an explicit save performs the durable
        canonicalization through ``upsert``.
        """

        if worker.worker_type != "pi":
            return worker
        return worker.model_copy(
            update={"config": canonicalize_pi_worker_config(worker.config)}
        )
