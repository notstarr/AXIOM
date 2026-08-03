from __future__ import annotations

import httpx
from sqlalchemy import Engine, func, select

from axiom.modules.graph.tables import intents
from axiom.modules.reason.tables import reason_runs
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
