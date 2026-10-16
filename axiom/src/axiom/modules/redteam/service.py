from __future__ import annotations

import json
from sqlalchemy import Engine, exists, select, update

from axiom.modules.artifacts.tables import artifacts
from axiom.modules.audit.schemas import RecordAuditEvent
from axiom.modules.audit.service import AuditService
from axiom.modules.graph.tables import intents
from axiom.modules.graph.tables import facts
from axiom.modules.redteam.repository import RedTeamRepository
from axiom.modules.redteam.schemas import (
    ACTIONABLE_CHAIN_STATUSES,
    LEGACY_CHAIN_STATUS_MAP,
    RedTeamChain,
    RedTeamChainCreate,
    RedTeamResult,
    RedTeamResultReport,
    RedTeamResultReview,
)
from axiom.modules.redteam.tables import red_team_chains
from axiom.modules.tasks.tables import tasks
from axiom.shared.clock import utcnow
from axiom.shared.database import run_transaction
from axiom.shared.errors import ConflictError, LeaseInactiveError, NotFoundError


def _is_planner_role(created_by: str) -> bool:
    """Accept a ``reason``/``plan`` role with an optional ``worker.`` prefix.

    Worker CLI callers pass role-qualified names such as ``worker.reason``
    (the API default) or ``reason.<pool>``; both must be accepted while
    Explore callers stay rejected.
    """
    role = (created_by or "").strip().lower()
    if role.startswith("worker."):
        role = role[len("worker.") :]
    return role.startswith("reason") or role.startswith("plan")


def normalize_chain_status(value: str | None) -> str:
    normalized = str(value or "").strip()
    return LEGACY_CHAIN_STATUS_MAP.get(normalized, normalized)


def actionable_chain_ids(conn, task_id: str) -> list[str]:
    """Chains Reason should keep planning, shaped like actionable Todos.

    An open or partial Chain with no in-flight Explore Intent is the red-team
    wake signal.  Blocked, completed and abandoned chains wait for an explicit
    Reason/Plan status change.
    """
    open_explore = ("pending", "claimed", "running", "concluding")
    chain_has_open_explore = exists(
        select(intents.c.id).where(
            intents.c.task_id == task_id,
            intents.c.chain_id == red_team_chains.c.id,
            intents.c.kind == "explore",
            intents.c.status.in_(open_explore),
            intents.c.archived.is_(False),
        )
    )
    rows = conn.execute(
        select(red_team_chains.c.id, red_team_chains.c.status)
        .where(
            red_team_chains.c.task_id == task_id,
            ~chain_has_open_explore,
        )
        .order_by(red_team_chains.c.created_at, red_team_chains.c.id)
    ).mappings()
    return [
        row["id"]
        for row in rows
        if normalize_chain_status(row["status"]) in ACTIONABLE_CHAIN_STATUSES
    ]


def bound_chain_id(row) -> str | None:
    """Prefer the Intent column; fall back to legacy plan metadata."""
    column = row.get("chain_id") if hasattr(row, "get") else None
    if isinstance(column, str) and column.strip():
        return column.strip()
    return _chain_id_from_metadata(row.get("plan_metadata_json") if hasattr(row, "get") else None)


def _chain_id_from_metadata(value) -> str | None:
    """Read the bound RedTeam Chain from an Intent's plan metadata."""

    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (TypeError, ValueError):
            return None
    if not isinstance(value, dict):
        return None
    chain_id = value.get("chain_id")
    return chain_id.strip() if isinstance(chain_id, str) and chain_id.strip() else None


class RedTeamService:
    """Control-plane operations for the isolated red-team result model.

    This service deliberately does not call the vulnerability repository.  A
    red-team result is evidence of a goal transition, not a vulnerability row.
    """

    def __init__(self, engine: Engine, *, reviews=None):
        self.engine = engine
        self.repository = RedTeamRepository()
        self.audit = AuditService(engine)
        self.reviews = reviews

    def _task(self, conn, task_id: str):
        row = conn.execute(select(tasks).where(tasks.c.id == task_id)).mappings().one_or_none()
        if row is None:
            raise NotFoundError(f"Task not found: {task_id}")
        if row.get("engagement_mode", "vulnerability") != "red_team":
            raise ConflictError("This operation is only available for red_team tasks")
        return row

    @staticmethod
    def _objective(row) -> dict:
        value = row.get("red_team_objective_json")
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except (TypeError, ValueError):
                return {}
        return value if isinstance(value, dict) else {}

    def create_chain(
        self,
        task_id: str,
        command: RedTeamChainCreate,
        *,
        created_by: str,
        goal_cycle_id: str | None = None,
        intent_id: str | None = None,
    ) -> RedTeamChain:
        def work(conn):
            return self.create_chain_in(
                conn,
                task_id,
                command,
                created_by=created_by,
                goal_cycle_id=goal_cycle_id,
                intent_id=intent_id,
            )

        return run_transaction(self.engine, work)

    def create_chain_in(
        self,
        conn,
        task_id: str,
        command: RedTeamChainCreate,
        *,
        created_by: str,
        goal_cycle_id: str | None = None,
        intent_id: str | None = None,
        now: str | None = None,
    ) -> RedTeamChain:
        """Persist one Chain inside the caller's transaction.

        Reason/Plan Workspace commits reuse this path so a staged Chain is
        created with exactly the same objective, scope and provenance checks
        as the direct API.
        """
        now = now or utcnow()
        if not _is_planner_role(created_by):
            raise ConflictError("Only Reason or Plan may create a RedTeam Chain")
        task = self._task(conn, task_id)
        objective = self._objective(task)
        allowed_capabilities = set(objective.get("target_capabilities", []))
        allowed_assets = set(objective.get("target_assets", []))
        # 未声明结构化目标（空列表）= 不设上限，Reason 可自由声明能力/资产；
        # 显式声明了目标则锁死边界（fail closed）。
        if allowed_capabilities and not set(command.target_capabilities).issubset(allowed_capabilities):
            raise ConflictError("Chain capability target exceeds the task objective")
        if allowed_assets and not set(command.target_assets).issubset(allowed_assets):
            raise ConflictError("Chain asset target exceeds the task objective")
        if command.source_fact_ids:
            found = set(
                conn.execute(
                    select(facts.c.id).where(
                        facts.c.task_id == task_id,
                        facts.c.id.in_(command.source_fact_ids),
                        facts.c.archived.is_(False),
                    )
                ).scalars()
            )
            if found != set(command.source_fact_ids):
                raise ConflictError("Chain contains a Fact outside the task scope")
        if intent_id:
            intent = conn.execute(
                select(intents).where(intents.c.task_id == task_id, intents.c.id == intent_id)
            ).mappings().one_or_none()
            if intent is None or intent["kind"] != "explore":
                raise ConflictError("Chain source Intent is invalid")
        chain = self.repository.create_chain(
            conn,
            task_id=task_id,
            goal_cycle_id=goal_cycle_id,
            command=command,
            created_by=created_by,
            now=now,
        )
        if intent_id:
            chain = self.repository.update_chain(
                conn,
                task_id=task_id,
                chain_id=chain.id,
                values={"source_intent_ids_json": [intent_id], "updated_at": now},
            ) or chain
        self.audit.record_in(conn, RecordAuditEvent(
            project_id=task["project_id"], task_id=task_id, worker=created_by,
            event_type="redteam.chain.created", message="红队攻击链已创建",
            payload={"chain_id": chain.id, "title": chain.title, "created_by": created_by},
        ))
        return chain

    def get_chain(self, task_id: str, chain_id: str) -> RedTeamChain:
        with self.engine.connect() as conn:
            self._task(conn, task_id)
            chain = self.repository.get_chain(conn, task_id, chain_id)
        if chain is None:
            raise NotFoundError(f"RedTeam Chain not found: {chain_id}")
        return chain

    def apply_chain_update_in(
        self,
        conn,
        task_id: str,
        chain_id: str,
        *,
        status: str | None = None,
        status_reason: str | None = None,
        title: str | None = None,
        current_state: dict | None = None,
        actor: str,
        now: str | None = None,
    ) -> RedTeamChain:
        """Apply one worker-staged Chain update inside the caller's transaction.

        Reason and Plan own the operational status, the same way they own Todo
        status.  Points stay on verified Results.
        """
        now = now or utcnow()
        task = self._task(conn, task_id)
        current = self.repository.get_chain(conn, task_id, chain_id)
        if current is None:
            raise NotFoundError(f"RedTeam Chain not found: {chain_id}")
        if status is not None and status not in (
            "open",
            "partial",
            "blocked",
            "completed",
            "abandoned",
        ):
            raise ConflictError(f"Chain status is not a worker update: {status}")
        values: dict = {"updated_at": now}
        if status is not None:
            values["status"] = status
        if status_reason is not None:
            values["status_reason"] = status_reason
        if title is not None:
            values["title"] = title
        if current_state is not None:
            values["current_state_json"] = current_state
        updated = self.repository.update_chain(
            conn, task_id=task_id, chain_id=chain_id, values=values
        )
        if updated is None:
            raise ConflictError("Chain update lost a concurrent race")
        self.audit.record_in(conn, RecordAuditEvent(
            project_id=task["project_id"], task_id=task_id, worker=actor,
            event_type="redteam.chain.updated", message="红队攻击链已更新",
            payload={"chain_id": chain_id, "status": status, "title": title},
        ))
        return updated

    def link_intent_in(
        self,
        conn,
        task_id: str,
        chain_id: str,
        intent_id: str,
        *,
        actor: str,
        now: str | None = None,
    ) -> RedTeamChain:
        """Bind one Explore Intent to a Chain inside the caller's transaction.

        The binding is written on ``intents.chain_id``.  ``source_intent_ids``
        and ``plan_metadata_json.chain_id`` stay filled so older readers keep
        working during the migration.
        """
        now = now or utcnow()
        task = self._task(conn, task_id)
        chain = self.repository.get_chain(conn, task_id, chain_id)
        if chain is None:
            raise NotFoundError(f"RedTeam Chain not found: {chain_id}")
        intent = conn.execute(
            select(intents).where(intents.c.task_id == task_id, intents.c.id == intent_id)
        ).mappings().one_or_none()
        if intent is None or intent["kind"] != "explore":
            raise NotFoundError(f"Explore Intent not found: {intent_id}")
        linked = list(dict.fromkeys([*chain.source_intent_ids, intent_id]))
        updated = self.repository.update_chain(
            conn,
            task_id=task_id,
            chain_id=chain_id,
            values={"source_intent_ids_json": linked, "updated_at": now},
        )
        if updated is None:
            raise ConflictError("Chain link lost a concurrent race")
        metadata = intent.get("plan_metadata_json")
        if isinstance(metadata, str):
            try:
                metadata = json.loads(metadata)
            except (TypeError, ValueError):
                metadata = {}
        metadata = metadata if isinstance(metadata, dict) else {}
        metadata["chain_id"] = chain_id
        conn.execute(
            update(intents)
            .where(intents.c.task_id == task_id, intents.c.id == intent_id)
            .values(chain_id=chain_id, plan_metadata_json=metadata)
        )
        self.audit.record_in(conn, RecordAuditEvent(
            project_id=task["project_id"], task_id=task_id, intent_id=intent_id, worker=actor,
            event_type="redteam.chain.linked", message="红队攻击链已关联 Explore Intent",
            payload={"chain_id": chain_id, "intent_id": intent_id},
        ))
        return updated

    def list_chains(self, task_id: str) -> list[RedTeamChain]:
        with self.engine.connect() as conn:
            self._task(conn, task_id)
            return self.repository.list_chains(conn, task_id)

    def list_results(self, task_id: str, *, chain_id: str | None = None) -> list[RedTeamResult]:
        with self.engine.connect() as conn:
            self._task(conn, task_id)
            return self.repository.list_results(conn, task_id, chain_id=chain_id)

    def list_results_page(
        self,
        *,
        task_id: str | None = None,
        chain_id: str | None = None,
        status: str | None = None,
        result_type: str | None = None,
        search: str = "",
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[RedTeamResult], int]:
        """Cross-task listing for the 重要事实·成果 view (management console)."""

        with self.engine.connect() as conn:
            return self.repository.list_results_page(
                conn,
                task_id=task_id,
                chain_id=chain_id,
                status=status,
                result_type=result_type,
                search=search,
                limit=limit,
                offset=offset,
            )

    def list_chains_page(
        self,
        *,
        task_id: str | None = None,
        status: str | None = None,
        search: str = "",
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[RedTeamChain], int]:
        with self.engine.connect() as conn:
            return self.repository.list_chains_page(
                conn,
                task_id=task_id,
                status=status,
                search=search,
                limit=limit,
                offset=offset,
            )

    def report_result(
        self,
        task_id: str,
        intent_id: str,
        lease_id: str,
        command: RedTeamResultReport,
        *,
        reported_by: str,
    ) -> RedTeamResult:
        def work(conn):
            task = self._task(conn, task_id)
            intent = conn.execute(
                select(intents).where(intents.c.task_id == task_id, intents.c.id == intent_id)
            ).mappings().one_or_none()
            if intent is None or intent["kind"] != "explore":
                raise NotFoundError(f"Explore Intent not found: {intent_id}")
            if intent["lease_id"] != lease_id or intent["status"] not in {"claimed", "running", "concluding"}:
                raise LeaseInactiveError("Explore Intent lease is not active")
            linked_chain_id = bound_chain_id(intent)
            if not linked_chain_id:
                raise ConflictError("Explore Intent is not bound to a Chain")
            if command.chain_id and command.chain_id != linked_chain_id:
                raise ConflictError("Result must be reported to the Intent's bound Chain")
            # Do not rebind `command`: this nested function would then treat the
            # outer parameter as an unassigned local.
            report = command.model_copy(update={"chain_id": linked_chain_id})
            chain = self.repository.get_chain(conn, task_id, linked_chain_id)
            if chain is None:
                raise NotFoundError(f"RedTeam Chain not found: {linked_chain_id}")
            if not report.evidence_refs:
                raise ConflictError("A Result requires at least one Evidence or Artifact reference")
            bound_refs: list[str] = []
            for ref in report.evidence_refs:
                if not ref.startswith("artifact:"):
                    raise ConflictError("Evidence references must use artifact:<id>")
                artifact_id = ref.removeprefix("artifact:")
                artifact = conn.execute(
                    select(artifacts.c.id, artifacts.c.intent_id).where(
                        artifacts.c.task_id == task_id,
                        artifacts.c.id == artifact_id,
                        artifacts.c.archived.is_(False),
                    )
                ).mappings().one_or_none()
                if artifact is None:
                    raise ConflictError(f"Evidence/Artifact is not available: {ref}")
                # 证据归到上报它的 Intent，和漏洞模式一样出现在 iXXX 目录下。
                if not artifact["intent_id"]:
                    conn.execute(
                        update(artifacts)
                        .where(artifacts.c.task_id == task_id, artifacts.c.id == artifact_id)
                        .values(intent_id=intent_id)
                    )
                bound_refs.append(ref)
            report = report.model_copy(update={"evidence_refs": bound_refs})
            objective = self._objective(task)
            if report.result_type == "data":
                if report.sample_count is None or report.sample_bytes is None:
                    raise ConflictError("Data Result requires sample_count and sample_bytes")
                # 任务声明了数据目标时锁死上限与字段白名单；未声明则不做
                # 结构化限制（样本数/字节数仍必填，审核与 Goal 校验兜底）。
                data_targets = [
                    item for item in objective.get("data_targets", [])
                    if isinstance(item, dict) and str(item.get("id") or "").strip()
                ]
                if data_targets:
                    target = next((item for item in data_targets if item.get("id") == report.data_target_id), None)
                    if target is None:
                        raise ConflictError("Data Result must reference a task data target")
                    if (report.sample_count or 0) > int(target.get("max_records", 10)) or (report.sample_bytes or 0) > int(target.get("max_bytes", 1_048_576)):
                        raise ConflictError("Data Result exceeds the task objective limit")
                    if not set(report.data_fields).issubset(set(target.get("allowed_fields", []))):
                        raise ConflictError("Data Result contains fields outside the objective")
            if report.result_type == "credential" and not report.credential_ref:
                raise ConflictError("Credential Result requires a reference, never plaintext credentials")
            if report.result_type in {"permission", "pivot"} and not report.capabilities_gained and not report.target_asset:
                raise ConflictError("Permission/Pivot Result requires a capability or target asset")
            result = self.repository.create_result(
                conn,
                task_id=task_id,
                intent_id=intent_id,
                command=report,
                reported_by=intent.get("worker") or reported_by or "explore",
                now=utcnow(),
            )
            state = dict(chain.current_state)
            capabilities = set(state.get("capabilities") or []) | set(report.capabilities_gained)
            assets = set(state.get("assets") or [])
            if report.target_asset:
                assets.add(report.target_asset)
            state.update({"capabilities": sorted(capabilities), "assets": sorted(assets), "last_result_id": result.id})
            conn.execute(
                update(red_team_chains)
                .where(red_team_chains.c.task_id == task_id, red_team_chains.c.id == report.chain_id)
                .values(status="partial", current_state_json=state, updated_at=utcnow())
            )
            self.audit.record_in(conn, RecordAuditEvent(
                project_id=task["project_id"], task_id=task_id, intent_id=intent_id,
                worker=reported_by or "explore", event_type="redteam.result.reported",
                message="红队成果已上报并进入审核", payload={"result_id": result.id, "chain_id": report.chain_id, "result_type": report.result_type},
            ))
            if self.reviews is None:
                raise ConflictError("Red-team Reviewer service is not configured")
            self.reviews.enqueue_redteam_in(conn, task_id=task_id, result_id=result.id, now=utcnow())
            return result.model_copy(update={"status": "reviewing"})

        return run_transaction(self.engine, work)

    def review_result(self, task_id: str, result_id: str, command: RedTeamResultReview) -> RedTeamResult:
        def work(conn):
            if not command.reviewed_by.startswith("reviewer"):
                raise ConflictError("Only the Reviewer Worker may adjudicate a Result")
            self._task(conn, task_id)
            current = self.repository.get_result(conn, task_id, result_id)
            if current is None:
                raise NotFoundError(f"RedTeam Result not found: {result_id}")
            if current.status not in {"reported", "reviewing", "needs-human"}:
                raise ConflictError("Result is already terminal")
            # 积分核定：verified 必须给出核定分；其余状态一律 0 分
            points_awarded = 0
            if command.status == "verified":
                if command.points_awarded is None or command.points_awarded < 1:
                    raise ConflictError("Verified Result requires awarded points")
                points_awarded = int(command.points_awarded)
            updated = self.repository.set_result_status(
                conn, task_id=task_id, result_id=result_id, status=command.status,
                reviewed_by=command.reviewed_by, review_reason=command.reason,
                points_awarded=points_awarded, now=utcnow()
            )
            assert updated is not None
            task = conn.execute(select(tasks).where(tasks.c.id == task_id)).mappings().one()
            self.audit.record_in(conn, RecordAuditEvent(
                project_id=task["project_id"], task_id=task_id, worker=command.reviewed_by,
                event_type="redteam.result.reviewed", message="红队成果已完成审核",
                payload={"result_id": result_id, "status": command.status, "points_awarded": points_awarded},
            ))
            return updated
        return run_transaction(self.engine, work)
