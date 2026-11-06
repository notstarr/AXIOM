from __future__ import annotations

import json

from pydantic import ValidationError
from sqlalchemy import Connection, func, or_, select, update

from axiom.modules.artifacts.tables import artifacts
from axiom.modules.redteam.schemas import (
    RedTeamChain,
    RedTeamChainCreate,
    RedTeamEvidenceArtifact,
    RedTeamResult,
    RedTeamResultReport,
)
from axiom.modules.redteam.tables import red_team_chains, red_team_results
from axiom.modules.reproduction.schemas import Reproduction
from axiom.modules.tasks.tables import tasks
from axiom.shared.errors import ConflictError
from axiom.shared.ids import allocate_id

# Chains number themselves per Task (ch001), so the display never carries the
# global counter.  Three digits keeps the ids aligned in the console; past 999
# the padding would widen and break the alphabetical tie-break in the lineage
# view, so refuse instead of silently mis-sorting.
CHAIN_ID_WIDTH = 3
CHAIN_ID_LIMIT = 10**CHAIN_ID_WIDTH - 1


def _reproduction_of(row) -> Reproduction | None:
    """Decode the stored reproduction; ``{}`` means none recorded."""
    raw = row.get("reproduction_json")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw or "{}")
        except (TypeError, ValueError):
            return None
    if not isinstance(raw, dict) or not raw:
        return None
    try:
        return Reproduction.model_validate(raw)
    except ValidationError:
        return None


def _json_list(value) -> list[str]:
    if isinstance(value, list):
        return [str(item) for item in value if str(item).strip()]
    if isinstance(value, str) and value:
        try:
            decoded = json.loads(value)
        except (TypeError, ValueError):
            return []
        return _json_list(decoded)
    return []


def _json_object(value) -> dict:
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value:
        try:
            decoded = json.loads(value)
        except (TypeError, ValueError):
            return {}
        return decoded if isinstance(decoded, dict) else {}
    return {}


def _evidence_name(storage_key: str, summary: str = "") -> str:
    """The artifact's own file name, which is what the evidence link shows."""

    key = str(storage_key or "").strip()
    if key:
        return key.rsplit("/", 1)[-1]
    return str(summary or "").strip()


def evidence_names(conn: Connection, task_id: str, refs: list[str]) -> list[str]:
    """把 ``artifact:<id>`` 引用解析成产物原文件名。

    通知卡片与控制台共用同一口径（都显示原文件名而非不透明 id）。
    """

    ids = [
        str(ref).split(":", 1)[1].strip()
        for ref in (refs or [])
        if str(ref).startswith("artifact:")
    ]
    ids = [item for item in ids if item]
    if not ids:
        return []
    rows = conn.execute(
        select(artifacts.c.id, artifacts.c.storage_key, artifacts.c.summary).where(
            artifacts.c.task_id == task_id, artifacts.c.id.in_(ids)
        )
    ).mappings()
    by_id = {
        row["id"]: _evidence_name(row.get("storage_key"), row.get("summary"))
        for row in rows
    }
    return [by_id[item] for item in ids if item in by_id]


def points_progress(conn: Connection, task_id: str) -> tuple[int, int]:
    """红队积分进度：verified 核定分之和 / 目标分。

    Goal 判定（goals.service）用的就是这个口径，通知卡片必须与之一致，
    否则群里的数字会和控制台/完成判定对不上。
    """

    awarded = conn.execute(
        select(func.coalesce(func.sum(red_team_results.c.points_awarded), 0)).where(
            red_team_results.c.task_id == task_id,
            red_team_results.c.status == "verified",
        )
    ).scalar_one()
    raw = conn.execute(
        select(tasks.c.red_team_objective_json).where(tasks.c.id == task_id)
    ).scalar_one_or_none()
    target = 100
    if raw:
        objective = _json_object(raw)
        try:
            target = int(objective.get("target_points") or 100)
        except (TypeError, ValueError):
            target = 100
    return int(awarded or 0), int(target)


class RedTeamRepository:
    def create_chain(
        self,
        conn: Connection,
        *,
        task_id: str,
        goal_cycle_id: str | None,
        command: RedTeamChainCreate,
        created_by: str,
        now: str,
    ) -> RedTeamChain:
        chain_id = allocate_id(
            conn,
            scope=task_id,
            kind="red_team_chain",
            prefix="ch",
            width=CHAIN_ID_WIDTH,
        )
        if len(chain_id) > 2 + CHAIN_ID_WIDTH:
            raise ConflictError(
                f"任务 {task_id} 的攻击链已达 {CHAIN_ID_LIMIT} 条上限，请先归档或合并"
            )
        values = {
            "id": chain_id,
            "task_id": task_id,
            "goal_cycle_id": goal_cycle_id,
            "title": command.title,
            "initial_principal_json": command.initial_principal.model_dump(mode="json"),
            "target_capabilities_json": command.target_capabilities,
            "target_assets_json": command.target_assets,
            "status": "open",
            "status_reason": "",
            "current_state_json": {
                "principal": command.initial_principal.model_dump(mode="json"),
                "capabilities": [],
                "assets": [],
            },
            "source_fact_ids_json": command.source_fact_ids,
            "source_intent_ids_json": [],
            "created_by": created_by,
            "created_at": now,
            "updated_at": now,
        }
        conn.execute(red_team_chains.insert().values(**values))
        return self._chain(values)

    def get_chain(self, conn: Connection, task_id: str, chain_id: str) -> RedTeamChain | None:
        row = conn.execute(
            select(red_team_chains).where(
                red_team_chains.c.task_id == task_id,
                red_team_chains.c.id == chain_id,
            )
        ).mappings().one_or_none()
        return self._chain(row) if row is not None else None

    def list_chains(self, conn: Connection, task_id: str) -> list[RedTeamChain]:
        rows = conn.execute(
            select(red_team_chains)
            .where(red_team_chains.c.task_id == task_id)
            .order_by(red_team_chains.c.created_at, red_team_chains.c.id)
        ).mappings()
        return [self._chain(row) for row in rows]

    def update_chain(
        self,
        conn: Connection,
        *,
        task_id: str,
        chain_id: str,
        values: dict,
    ) -> RedTeamChain | None:
        result = conn.execute(
            update(red_team_chains)
            .where(
                red_team_chains.c.task_id == task_id,
                red_team_chains.c.id == chain_id,
            )
            .values(**values)
        )
        if not result.rowcount:
            return None
        return self.get_chain(conn, task_id, chain_id)

    def get_result(self, conn: Connection, task_id: str, result_id: str) -> RedTeamResult | None:
        row = conn.execute(
            select(red_team_results).where(
                red_team_results.c.task_id == task_id,
                red_team_results.c.id == result_id,
            )
        ).mappings().one_or_none()
        if row is None:
            return None
        return self._with_evidence(conn, [self._result(row)])[0]

    def list_results(
        self,
        conn: Connection,
        task_id: str,
        *,
        chain_id: str | None = None,
    ) -> list[RedTeamResult]:
        query = select(red_team_results).where(red_team_results.c.task_id == task_id)
        if chain_id:
            query = query.where(red_team_results.c.chain_id == chain_id)
        rows = conn.execute(query.order_by(red_team_results.c.created_at, red_team_results.c.id)).mappings()
        return self._with_evidence(conn, [self._result(row) for row in rows])

    def list_results_page(
        self,
        conn: Connection,
        *,
        task_id: str | None = None,
        chain_id: str | None = None,
        status: str | None = None,
        result_type: str | None = None,
        search: str = "",
        limit: int,
        offset: int,
    ) -> tuple[list[RedTeamResult], int, int]:
        """Cross-task Result listing for the management console (重要事实·成果视图).

        Returns ``(items, total, awarded_points)``. ``awarded_points`` covers the
        whole filtered set, not the requested page — the console shows it as the
        核定 score and must not shrink when the user pages through results.
        """

        conditions = []
        if task_id:
            conditions.append(red_team_results.c.task_id == task_id)
        if chain_id:
            conditions.append(red_team_results.c.chain_id == chain_id)
        if status:
            conditions.append(red_team_results.c.status == status)
        if result_type:
            conditions.append(red_team_results.c.result_type == result_type)
        if search:
            like = f"%{search}%"
            conditions.append(
                or_(
                    red_team_results.c.title.ilike(like),
                    red_team_results.c.description.ilike(like),
                    red_team_results.c.principal.ilike(like),
                )
            )
        total = conn.execute(
            select(func.count()).select_from(red_team_results).where(*conditions)
        ).scalar_one()
        # 核定分合计必须覆盖整个筛选集，不能由前端对本页求和：分页后那会变成
        # 「本页核定多少分」。只统计 verified，口径与 points_progress 一致。
        awarded = conn.execute(
            select(
                func.coalesce(func.sum(red_team_results.c.points_awarded), 0)
            ).where(
                *conditions,
                red_team_results.c.status == "verified",
            )
        ).scalar_one()
        rows = conn.execute(
            select(red_team_results)
            .where(*conditions)
            .order_by(red_team_results.c.created_at.desc(), red_team_results.c.id.desc())
            .offset(offset)
            .limit(limit)
        ).mappings()
        return (
            self._with_evidence(conn, [self._result(row) for row in rows]),
            int(total),
            int(awarded or 0),
        )

    # 取数逻辑以模块级函数为唯一实现（通知与评审都能直接用连接调用），
    # 这里只做薄委托，避免两份口径漂移。

    def evidence_names(self, conn: Connection, task_id: str, refs: list[str]) -> list[str]:
        return evidence_names(conn, task_id, refs)

    def points_progress(self, conn: Connection, task_id: str) -> tuple[int, int]:
        return points_progress(conn, task_id)

    def _with_evidence(self, conn: Connection, results: list[RedTeamResult]) -> list[RedTeamResult]:
        """Resolve ``artifact:<id>`` references to their file names.

        Artifact ids are scoped to a Task, so the lookup key is the pair.
        References that no longer resolve keep only the raw ref (the console
        then falls back to showing the id instead of a file name).
        """

        wanted: dict[str, set[str]] = {}
        for item in results:
            for ref in item.evidence_refs:
                if ref.startswith("artifact:"):
                    artifact_id = ref.split(":", 1)[1].strip()
                    if artifact_id:
                        wanted.setdefault(item.task_id, set()).add(artifact_id)
        if not wanted:
            return results
        lookup: dict[tuple[str, str], dict] = {}
        for task_id, ids in wanted.items():
            rows = conn.execute(
                select(
                    artifacts.c.task_id,
                    artifacts.c.id,
                    artifacts.c.intent_id,
                    artifacts.c.artifact_type,
                    artifacts.c.storage_key,
                    artifacts.c.summary,
                    artifacts.c.size,
                ).where(artifacts.c.task_id == task_id, artifacts.c.id.in_(sorted(ids)))
            ).mappings()
            for row in rows:
                lookup[(row["task_id"], row["id"])] = dict(row)
        hydrated: list[RedTeamResult] = []
        for item in results:
            resolved: list[RedTeamEvidenceArtifact] = []
            for ref in item.evidence_refs:
                artifact_id = ref.split(":", 1)[1].strip() if ref.startswith("artifact:") else ""
                row = lookup.get((item.task_id, artifact_id))
                if row is None:
                    continue
                resolved.append(
                    RedTeamEvidenceArtifact(
                        id=row["id"],
                        name=_evidence_name(row.get("storage_key"), row.get("summary")),
                        size=int(row.get("size") or 0),
                        artifact_type=row.get("artifact_type") or "",
                        intent_id=row.get("intent_id"),
                    )
                )
            hydrated.append(item.model_copy(update={"evidence_artifacts": resolved}))
        return hydrated

    def list_chains_page(
        self,
        conn: Connection,
        *,
        task_id: str | None = None,
        status: str | None = None,
        search: str = "",
        limit: int,
        offset: int,
    ) -> tuple[list[RedTeamChain], int]:
        """Cross-task Chain listing for the management console (成果·攻击链视图)."""

        conditions = []
        if task_id:
            conditions.append(red_team_chains.c.task_id == task_id)
        if status:
            conditions.append(red_team_chains.c.status == status)
        if search:
            conditions.append(red_team_chains.c.title.ilike(f"%{search}%"))
        total = conn.execute(
            select(func.count()).select_from(red_team_chains).where(*conditions)
        ).scalar_one()
        rows = conn.execute(
            select(red_team_chains)
            .where(*conditions)
            .order_by(red_team_chains.c.created_at.desc(), red_team_chains.c.id.desc())
            .offset(offset)
            .limit(limit)
        ).mappings()
        return [self._chain(row) for row in rows], int(total)

    def create_result(
        self,
        conn: Connection,
        *,
        task_id: str,
        intent_id: str,
        command: RedTeamResultReport,
        reported_by: str,
        now: str,
    ) -> RedTeamResult:
        # 成果编号按任务独立（x001、x002…），与漏洞的 v001 同一套口径，
        # 这样每条任务内都能从 1 开始读，不会出现跨任务的全局大序号。
        result_id = allocate_id(conn, scope=task_id, kind="red_team_result", prefix="x", width=3)
        values = {
            "id": result_id,
            "task_id": task_id,
            "chain_id": command.chain_id,
            "intent_id": intent_id,
            "result_type": command.result_type,
            "title": command.title,
            "description": command.description,
            "principal": command.principal,
            "tenant": command.tenant,
            "capabilities_before_json": command.capabilities_before,
            "capabilities_gained_json": command.capabilities_gained,
            "capabilities_after_json": command.capabilities_after,
            "target_asset": command.target_asset,
            "data_target_id": command.data_target_id,
            "sample_count": command.sample_count,
            "sample_bytes": command.sample_bytes,
            "data_fields_json": command.data_fields,
            "data_summary": command.data_summary,
            "credential_ref": command.credential_ref,
            "evidence_refs_json": command.evidence_refs,
            # 本环的复现过程；攻击链由图谱在读取时装配，不落在这里。
            "reproduction_json": command.reproduction.model_dump(mode="json"),
            # The control plane immediately queues a Reviewer; there is no
            # externally observable window in which an unqueued Result is
            # considered complete.
            "status": "reviewing",
            "reported_by": reported_by,
            "points_declared": command.points_declared,
            "points_awarded": 0,
            "reviewed_by": None,
            "review_reason": "",
            "created_at": now,
            "updated_at": now,
        }
        conn.execute(red_team_results.insert().values(**values))
        return self._with_evidence(conn, [self._result(values)])[0]

    def update_description(
        self,
        conn: Connection,
        *,
        task_id: str,
        result_id: str,
        values: dict,
        now: str,
    ) -> bool:
        """写审核者改写的描述类字段。

        与 ``update_result`` 分开是刻意的：那条路的语义是「人工改了实质内容 →
        判定失效」，会把 status 复位成 reviewing 并重新排队审核；审核者在自己的
        审核期里改描述不能触发这件事，否则它会把自己踢出队列。这里只碰描述列，
        状态、分数、裁决理由一概不动。
        """
        if not values:
            return False
        payload = {**values, "updated_at": now}
        result = conn.execute(
            update(red_team_results)
            .where(
                red_team_results.c.task_id == task_id,
                red_team_results.c.id == result_id,
            )
            .values(**payload)
        )
        return bool(result.rowcount)

    def update_result(
        self,
        conn: Connection,
        *,
        task_id: str,
        result_id: str,
        body,
        now: str,
    ) -> dict:
        """写入人工可改的字段，返回被改动的字段名（供重审判定）。"""
        fields = body.model_fields_set
        if not fields:
            return {}
        values: dict = {}
        for name in fields:
            value = getattr(body, name)
            if name == "reproduction":
                values["reproduction_json"] = (
                    value.model_dump(mode="json") if value is not None else {}
                )
            elif name in {"data_fields"}:
                values[f"{name}_json"] = list(value or [])
            elif name in {"sample_count", "sample_bytes", "data_target_id",
                          "credential_ref", "data_summary"}:
                values[name] = value
            elif name in {"title", "description", "principal", "tenant",
                          "target_asset", "result_type", "points_declared"}:
                values[name] = value
        values["updated_at"] = now
        conn.execute(
            update(red_team_results)
            .where(
                red_team_results.c.task_id == task_id,
                red_team_results.c.id == result_id,
            )
            .values(**values)
        )
        return {name for name in fields}

    def set_result_status(
        self,
        conn: Connection,
        *,
        task_id: str,
        result_id: str,
        status: str,
        reviewed_by: str,
        review_reason: str,
        points_awarded: int | None = None,
        now: str,
    ) -> RedTeamResult | None:
        values = {
            "status": status,
            "reviewed_by": reviewed_by,
            "review_reason": review_reason,
            "updated_at": now,
        }
        if points_awarded is not None:
            values["points_awarded"] = int(points_awarded)
        result = conn.execute(
            update(red_team_results)
            .where(
                red_team_results.c.task_id == task_id,
                red_team_results.c.id == result_id,
            )
            .values(**values)
        )
        if not result.rowcount:
            return None
        return self.get_result(conn, task_id, result_id)

    @staticmethod
    def _chain(row) -> RedTeamChain:
        return RedTeamChain(
            id=row["id"],
            task_id=row["task_id"],
            goal_cycle_id=row.get("goal_cycle_id"),
            title=row["title"],
            initial_principal=_json_object(row.get("initial_principal_json")),
            target_capabilities=_json_list(row.get("target_capabilities_json")),
            target_assets=_json_list(row.get("target_assets_json")),
            status=row["status"],
            status_reason=row.get("status_reason") or "",
            current_state=_json_object(row.get("current_state_json")),
            source_fact_ids=_json_list(row.get("source_fact_ids_json")),
            source_intent_ids=_json_list(row.get("source_intent_ids_json")),
            created_by=row["created_by"],
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
        )

    @staticmethod
    def _result(row) -> RedTeamResult:
        return RedTeamResult(
            id=row["id"],
            task_id=row["task_id"],
            chain_id=row["chain_id"],
            intent_id=row["intent_id"],
            result_type=row["result_type"],
            title=row["title"],
            description=row["description"],
            principal=row["principal"],
            tenant=row.get("tenant") or "",
            capabilities_before=_json_list(row.get("capabilities_before_json")),
            capabilities_gained=_json_list(row.get("capabilities_gained_json")),
            capabilities_after=_json_list(row.get("capabilities_after_json")),
            target_asset=row.get("target_asset") or "",
            data_target_id=row.get("data_target_id"),
            sample_count=row.get("sample_count"),
            sample_bytes=row.get("sample_bytes"),
            data_fields=_json_list(row.get("data_fields_json")),
            data_summary=row.get("data_summary") or "",
            credential_ref=row.get("credential_ref"),
            evidence_refs=_json_list(row.get("evidence_refs_json")),
            reproduction=_reproduction_of(row),
            status=row["status"],
            reported_by=row["reported_by"],
            points_declared=int(row.get("points_declared") or 0),
            points_awarded=int(row.get("points_awarded") or 0),
            reviewed_by=row.get("reviewed_by"),
            review_reason=row.get("review_reason") or "",
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
        )
