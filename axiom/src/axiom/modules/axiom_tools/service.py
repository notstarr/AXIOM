from __future__ import annotations

import hashlib
import json
import os
import re
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy import Engine, func, select

from axiom.modules.audit.schemas import RecordAuditEvent
from axiom.modules.audit.service import AuditService
from axiom.modules.audit.tables import audit_events
from axiom.modules.axiom_tools.repository import AxiomToolRepository
from axiom.modules.axiom_tools.schemas import (
    AxiomTool,
    AxiomToolAuditCommand,
    AxiomToolBinding,
    AxiomToolBindingSelection,
    AxiomToolScanResult,
    UpdateAxiomTool,
    WorkerAxiomToolBindings,
    WorkerAxiomToolBindingsView,
    WorkerOverview,
)
from axiom.modules.graph.tables import intents
from axiom.modules.phase_capabilities.service import PhaseCapabilityService
from axiom.modules.reason.tables import reason_runs
from axiom.modules.tasks.tables import tasks
from axiom.modules.vulnerability_reviews.tables import vulnerability_review_runs
from axiom.modules.workers.pi_config import (
    PiWorkerConfig,
    canonicalize_pi_worker_config,
)
from axiom.modules.workers.repository import WorkerRepository
from axiom.shared.clock import parse_utc, utcnow
from axiom.shared.database import transaction
from axiom.shared.errors import ConflictError, LeaseInactiveError, NotFoundError


PHASES = ("bootstrap", "reason", "explore", "review")
_KEY = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_RESERVED_TOOL_NAMES = frozenset({"read", "write", "edit", "bash", "grep", "find", "ls"})


@dataclass(slots=True)
class _Candidate:
    key: str
    title: str
    version: str
    description: str
    source: str
    package_path: Path
    entrypoint: str
    manifest: dict[str, Any]
    input_schema: dict[str, Any]
    supported_phases: list[str]
    risk_tags: list[str]
    package_hash: str
    status: str = "ready"
    status_error: str = ""


class AxiomToolService:
    def __init__(
        self,
        engine: Engine,
        data_dir: Path,
        *,
        skills,
        phase_capabilities: PhaseCapabilityService,
    ):
        self.engine = engine
        self.repository = AxiomToolRepository()
        self.workers = WorkerRepository()
        self.audit = AuditService(engine)
        self.skills = skills
        self.phase_capabilities = phase_capabilities
        self.managed_root = (data_dir / "axiom-tools").resolve()
        self.bundled_root = (
            Path(__file__).parents[2] / "dispatcher" / "axiom_tools" / "bundled"
        ).resolve()
        self.managed_root.mkdir(parents=True, exist_ok=True)

    def scan(self) -> AxiomToolScanResult:
        scanned_at = utcnow()
        candidates: list[_Candidate] = []
        reserved = set(_RESERVED_TOOL_NAMES)
        if self.skills is not None:
            reserved.update(skill.name for skill in self.skills.list())
        for source, root in (("bundled", self.bundled_root), ("managed", self.managed_root)):
            if not root.is_dir():
                continue
            for package in sorted(root.iterdir(), key=lambda item: item.name.casefold()):
                if package.name.startswith("."):
                    continue
                if not package.is_dir() and not package.is_symlink():
                    continue
                candidates.append(self._read_candidate(package, source=source, reserved=reserved))

        grouped: dict[str, list[_Candidate]] = defaultdict(list)
        for candidate in candidates:
            grouped[candidate.key].append(candidate)
        selected: dict[str, _Candidate] = {}
        for key, matches in grouped.items():
            candidate = matches[0]
            if len(matches) > 1:
                paths = ", ".join(str(item.package_path) for item in matches)
                candidate.status = "invalid"
                candidate.status_error = f"重复 AXIOM Tool key：{key}；候选目录：{paths}"
            selected[key] = candidate

        with transaction(self.engine) as conn:
            existing = {row["key"]: row for row in self.repository.list_rows(conn)}
            for key, candidate in selected.items():
                old = existing.get(key)
                values = {
                    "key": key,
                    "title": candidate.title,
                    "version": candidate.version,
                    "description": candidate.description,
                    "source": candidate.source,
                    "package_path": str(candidate.package_path),
                    "entrypoint": candidate.entrypoint,
                    "manifest_json": json.dumps(candidate.manifest, ensure_ascii=False),
                    "input_schema_json": json.dumps(candidate.input_schema, ensure_ascii=False),
                    "supported_phases_json": json.dumps(candidate.supported_phases),
                    "risk_tags_json": json.dumps(candidate.risk_tags, ensure_ascii=False),
                    "package_hash": candidate.package_hash,
                    "status": candidate.status,
                    "status_error": candidate.status_error,
                    "enabled": bool(old["enabled"]) if old is not None else True,
                    "discovered_at": scanned_at,
                    "created_at": old["created_at"] if old is not None else scanned_at,
                    "updated_at": scanned_at,
                }
                self.repository.upsert(conn, values)
            self.repository.mark_missing(conn, set(existing) - set(selected), scanned_at)

        items = self.list()
        return AxiomToolScanResult(
            scanned_at=scanned_at,
            ready=sum(item.status == "ready" for item in items),
            invalid=sum(item.status == "invalid" for item in items),
            missing=sum(item.status == "missing" for item in items),
            items=items,
        )

    def list(self) -> list[AxiomTool]:
        with self.engine.connect() as conn:
            rows = self.repository.list_rows(conn)
            workers_by_name = {
                worker.name: worker for worker in self.workers.list(conn)
            }
            usage = self._usage(conn)
        matrix = self.phase_capabilities.matrix()
        records = {
            (item.capability_kind, item.capability_key): item for item in matrix.items
        }
        result: list[AxiomTool] = []
        for row in rows:
            record = records.get(("axiom_tool", row["key"]))
            scopes = list(record.scopes) if record else []
            # The old response shape is still useful to existing clients.  It
            # is now only a projection of stage-level bindings, never a source
            # of runtime truth.
            compatibility_bindings: list[AxiomToolBinding] = []
            for scope in scopes:
                phases = PHASES if scope == "common" else (scope,)
                projected = [
                    worker
                    for worker in workers_by_name.values()
                    if any(phase in worker.capabilities for phase in phases)
                ]
                if not projected:
                    compatibility_bindings.append(
                        AxiomToolBinding(scope=scope, phase="" if scope == "common" else scope)
                    )
                for worker in projected:
                    phase = scope if scope != "common" else "common"
                    effective = all(
                        self._binding_effective(
                            worker,
                            row,
                            item_phase,
                        )[0]
                        for item_phase in phases
                        if item_phase in worker.capabilities
                    )
                    reason = next(
                        (
                            self._binding_effective(worker, row, item_phase)[1]
                            for item_phase in phases
                            if item_phase in worker.capabilities
                            and self._binding_effective(worker, row, item_phase)[1]
                        ),
                        "",
                    )
                    compatibility_bindings.append(
                        AxiomToolBinding(
                            scope=scope,
                            worker_name=worker.name,
                            phase=phase,
                            effective=effective,
                            inactive_reason=reason,
                        )
                    )
            item = self.repository.model(row, bindings=compatibility_bindings)
            stats = usage.get(item.key, {})
            result.append(
                item.model_copy(
                    update={
                        "scopes": scopes,
                        "call_count": stats.get("call_count", 0),
                        "last_used_at": stats.get("last_used_at", ""),
                        "last_load_error": stats.get("last_load_error", ""),
                    }
                )
            )
        return result

    def get(self, key: str) -> AxiomTool:
        key = self._key(key)
        for item in self.list():
            if item.key == key:
                return item
        raise NotFoundError(f"AXIOM Tool not found: {key}")

    def update(self, key: str, command: UpdateAxiomTool) -> AxiomTool:
        key = self._key(key)
        with self.engine.begin() as conn:
            if not self.repository.set_enabled(conn, key, command.enabled, utcnow()):
                raise NotFoundError(f"AXIOM Tool not found: {key}")
        return self.get(key)

    def worker_bindings(self, worker_name: str) -> WorkerAxiomToolBindingsView:
        with self.engine.connect() as conn:
            worker = self.workers.get(conn, worker_name)
            if worker is None:
                raise NotFoundError(f"Worker not found: {worker_name}")
            tool_rows = {row["key"]: row for row in self.repository.list_rows(conn)}
        matrix = self.phase_capabilities.matrix()
        by_key = {
            item.capability_key: item
            for item in matrix.items
            if item.capability_kind == "axiom_tool"
        }
        configured = {phase: [] for phase in PHASES}
        effective = {phase: [] for phase in PHASES}
        reasons: dict[str, str] = {}
        for key, item in by_key.items():
            for phase in PHASES:
                if phase in item.scopes or "common" in item.scopes:
                    configured[phase].append(key)
                    active, reason = self._binding_effective(
                        worker,
                        tool_rows.get(key),
                        phase,
                    )
                    if active:
                        effective[phase].append(key)
                    elif reason:
                        reasons[f"{phase}:{key}"] = reason
        for phase in PHASES:
            configured[phase].sort()
            effective[phase].sort()
        return WorkerAxiomToolBindingsView(
            worker_name=worker_name,
            **configured,
            effective=WorkerAxiomToolBindings(**effective),
            inactive_reasons=reasons,
        )

    def save_worker_bindings(
        self,
        worker_name: str,
        command: WorkerAxiomToolBindings,
    ) -> WorkerAxiomToolBindingsView:
        del command
        with self.engine.connect() as conn:
            if self.workers.get(conn, worker_name) is None:
                raise NotFoundError(f"Worker not found: {worker_name}")
        raise ConflictError(
            "具体模型后端不再持有 AXIOM Tool 绑定；请使用阶段能力接口"
        )

    def save_tool_bindings(
        self,
        key: str,
        command: AxiomToolBindingSelection,
    ) -> AxiomTool:
        key = self._key(key)
        unique: dict[str, AxiomToolBinding] = {}
        for binding in command.bindings:
            scope = binding.scope or binding.phase
            if not scope:
                continue
            unique[scope] = binding.model_copy(update={"scope": scope})
        bindings = list(unique.values())
        with transaction(self.engine) as conn:
            row = self.repository.get_row(conn, key)
            if row is None:
                raise NotFoundError(f"AXIOM Tool not found: {key}")
            for binding in bindings:
                scope = binding.scope or binding.phase
                if scope == "common":
                    phases = PHASES
                else:
                    phases = (scope,)
                for phase in phases:
                    self._require_bindable(row, key=key, phase=phase)
            scopes = list(dict.fromkeys(binding.scope for binding in bindings if binding.scope))
            self.phase_capabilities.repository.replace_capability(
                conn,
                capability_kind="axiom_tool",
                capability_key=key,
                scopes=scopes,
                now=utcnow(),
            )
        return self.get(key)

    def effective_tools(self, worker_name: str, phase: str) -> list[AxiomTool]:
        if phase not in PHASES:
            return []
        with self.engine.connect() as conn:
            worker = self.workers.get(conn, worker_name)
            if worker is None:
                raise NotFoundError(f"Worker not found: {worker_name}")
            tool_rows = {row["key"]: row for row in self.repository.list_rows(conn)}
        result: list[AxiomTool] = []
        for key in self.phase_capabilities.effective_keys(phase=phase, kind="axiom_tool"):
            row = tool_rows.get(key)
            active, _reason = self._binding_effective(worker, row, phase)
            if active and row is not None:
                result.append(self.repository.model(row))
        return sorted(result, key=lambda item: item.key)

    def snapshot_for_lease(
        self,
        *,
        worker_name: str,
        phase: str,
        run_dir: str | Path,
    ) -> tuple[list[str], list[str], str | None]:
        snapshot = self.phase_capabilities.snapshot_for_lease(
            worker_name=worker_name,
            phase=phase,
            run_dir=run_dir,
        )
        return (
            snapshot.axiom_tool_keys,
            snapshot.axiom_tool_extensions,
            snapshot.axiom_tool_snapshot_path,
        )

    def worker_overview(self) -> list[WorkerOverview]:
        with self.engine.connect() as conn:
            workers_list = self.workers.list(conn)
            active_counts = self._active_counts(conn)
        matrix = self.phase_capabilities.matrix()
        axiom_by_phase = {
            phase: {
                item.capability_key
                for item in matrix.items
                if item.capability_kind == "axiom_tool"
                and ("common" in item.scopes or phase in item.scopes)
            }
            for phase in PHASES
        }
        mcp_by_phase = {
            phase: {
                item.capability_key
                for item in matrix.items
                if item.capability_kind == "mcp_tool"
                and ("common" in item.scopes or phase in item.scopes)
            }
            for phase in PHASES
        }
        result: list[WorkerOverview] = []
        now = parse_utc(utcnow())
        for worker in workers_list:
            config = (
                PiWorkerConfig.model_validate(
                    canonicalize_pi_worker_config(worker.config)
                )
                if worker.worker_type == "pi"
                else None
            )
            if worker.disabled:
                connection_state = "disabled"
            elif worker.cooldown_until and parse_utc(worker.cooldown_until) > now:
                connection_state = "cooldown"
            else:
                connection_state = "ready"
            result.append(
                WorkerOverview(
                    name=worker.name,
                    worker_type=worker.worker_type,
                    capabilities=worker.capabilities,
                    max_running=worker.max_running,
                    running=active_counts.get(worker.name, 0),
                    priority=worker.priority,
                    disabled=worker.disabled,
                    cooldown_until=worker.cooldown_until,
                    connection_state=connection_state,
                    config=self._public_worker_config(worker, config),
                    model=config.model if config else "",
                    context_window=config.context_window if config else 0,
                    thinking_enabled=config.thinking_enabled if config else False,
                    thinking_level=config.effective_thinking_level if config else "off",
                    axiom_tool_counts={
                        phase: (
                            len(axiom_by_phase[phase])
                            if phase in worker.capabilities
                            else 0
                        )
                        for phase in PHASES
                    },
                    mcp_tool_count=len(
                        set().union(
                            *(
                                mcp_by_phase[phase]
                                for phase in worker.capabilities
                                if phase in mcp_by_phase
                            )
                        )
                    ),
                )
            )
        return result

    def record_audit(self, command: AxiomToolAuditCommand) -> None:
        with transaction(self.engine) as conn:
            project_id = conn.execute(
                select(tasks.c.project_id).where(tasks.c.id == command.task_id)
            ).scalar_one_or_none()
            if project_id is None:
                raise NotFoundError(f"Task not found: {command.task_id}")
            if command.intent_id:
                active = conn.execute(
                    select(intents.c.id).where(
                        intents.c.task_id == command.task_id,
                        intents.c.id == command.intent_id,
                        intents.c.lease_id == command.lease_id,
                        intents.c.worker == command.worker,
                        intents.c.status.in_(["claimed", "running", "concluding"]),
                    )
                ).scalar_one_or_none()
            elif command.reason_run_id:
                active = conn.execute(
                    select(reason_runs.c.id).where(
                        reason_runs.c.task_id == command.task_id,
                        reason_runs.c.id == command.reason_run_id,
                        reason_runs.c.lease_id == command.lease_id,
                        reason_runs.c.worker == command.worker,
                        reason_runs.c.status.in_(["claimed", "running", "concluding"]),
                    )
                ).scalar_one_or_none()
            elif command.review_run_id:
                active = conn.execute(
                    select(vulnerability_review_runs.c.id).where(
                        vulnerability_review_runs.c.task_id == command.task_id,
                        vulnerability_review_runs.c.id == command.review_run_id,
                        vulnerability_review_runs.c.lease_id == command.lease_id,
                        vulnerability_review_runs.c.worker == command.worker,
                        vulnerability_review_runs.c.status.in_(
                            ["claimed", "running", "concluding"]
                        ),
                    )
                ).scalar_one_or_none()
            else:
                raise LeaseInactiveError(
                    "AXIOM Tool audit requires an Intent, ReasonRun or ReviewRun"
                )
            if active is None:
                raise LeaseInactiveError("AXIOM Tool audit lease is no longer active")
            level = "error" if command.outcome in {"error", "load_error"} else "info"
            self.audit.record_in(
                conn,
                RecordAuditEvent(
                    project_id=project_id,
                    task_id=command.task_id,
                    intent_id=command.intent_id or None,
                    reason_run_id=command.reason_run_id or None,
                    review_run_id=command.review_run_id or None,
                    worker=command.worker,
                    event_type=(
                        "axiom_tool.load_failed"
                        if command.event_kind == "load_error"
                        else "axiom_tool.executed"
                    ),
                    level=level,
                    message=(
                        f"AXIOM Tool {command.tool_key} 加载失败"
                        if command.event_kind == "load_error"
                        else f"AXIOM Tool {command.tool_key} 执行{command.outcome}"
                    ),
                    payload={
                        "category": "axiom_tool",
                        "tool_key": command.tool_key,
                        "phase": command.phase,
                        "lease_id": command.lease_id,
                        "outcome": command.outcome,
                        "duration_ms": command.duration_ms,
                        **({"error": command.error} if command.error else {}),
                    },
                ),
            )

    def _read_candidate(self, package: Path, *, source: str, reserved: set[str]) -> _Candidate:
        fallback_key = package.name if _KEY.fullmatch(package.name) else f"invalid_{hashlib.sha256(str(package).encode()).hexdigest()[:12]}"
        invalid = self._invalid_candidate(fallback_key, package, source)
        try:
            if package.is_symlink():
                raise ValueError("工具包目录禁止使用符号链接")
            self._reject_symlinks(package)
            manifest_path = package / "tool.json"
            if not manifest_path.is_file():
                raise ValueError("缺少 tool.json")
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            if not isinstance(manifest, dict):
                raise ValueError("tool.json 根节点必须是对象")
            if manifest.get("schema_version") != 1:
                raise ValueError("tool.json schema_version 必须为 1")
            key = str(manifest.get("key") or "").strip()
            if not _KEY.fullmatch(key):
                raise ValueError("key 必须为小写字母开头的字母、数字或下划线")
            invalid.key = key
            if key != package.name:
                raise ValueError("工具包目录名必须与 key 一致")
            if key in reserved or key.startswith("mcp__"):
                raise ValueError(f"工具名 {key} 与 Pi 原生 Tool、Skill 或 MCP 保留名冲突")
            title = str(manifest.get("title") or "").strip()
            version = str(manifest.get("version") or "").strip()
            description = str(manifest.get("description") or "").strip()
            entrypoint = str(manifest.get("entrypoint") or "").strip()
            if not title or not version or not description or not entrypoint:
                raise ValueError("title、version、description、entrypoint 均不能为空")
            entry_relative = Path(entrypoint)
            if entry_relative.is_absolute() or ".." in entry_relative.parts:
                raise ValueError("entrypoint 不能越界")
            entry_path = (package / entry_relative).resolve(strict=True)
            package_resolved = package.resolve(strict=True)
            if entry_path == package_resolved or package_resolved not in entry_path.parents:
                raise ValueError("entrypoint 越出工具包目录")
            if not entry_path.is_file() or entry_path.is_symlink():
                raise ValueError("entrypoint 必须是普通文件且不能是符号链接")
            parameters = manifest.get("parameters", {})
            self._validate_input_schema(parameters)
            phases = manifest.get("supported_phases")
            if not isinstance(phases, list) or not phases:
                raise ValueError("supported_phases 至少包含一个阶段")
            phases = list(dict.fromkeys(str(item).strip() for item in phases))
            if any(phase not in PHASES for phase in phases):
                raise ValueError(
                    "supported_phases 只能包含 bootstrap、reason、explore、review"
                )
            risk_tags = manifest.get("risk_tags", [])
            guidelines = manifest.get("prompt_guidelines", [])
            if not isinstance(risk_tags, list) or not all(isinstance(item, str) for item in risk_tags):
                raise ValueError("risk_tags 必须是字符串数组")
            if not isinstance(guidelines, list) or not all(isinstance(item, str) for item in guidelines):
                raise ValueError("prompt_guidelines 必须是字符串数组")
            return _Candidate(
                key=key,
                title=title,
                version=version,
                description=description,
                source=source,
                package_path=package.resolve(),
                entrypoint=entrypoint,
                manifest=manifest,
                input_schema=parameters,
                supported_phases=phases,
                risk_tags=list(dict.fromkeys(item.strip() for item in risk_tags if item.strip())),
                package_hash=self._package_hash(package),
            )
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            invalid.status_error = str(exc)
            return invalid

    @staticmethod
    def _invalid_candidate(key: str, package: Path, source: str) -> _Candidate:
        return _Candidate(
            key=key,
            title=package.name,
            version="",
            description="",
            source=source,
            package_path=package.absolute(),
            entrypoint="",
            manifest={},
            input_schema={},
            supported_phases=[],
            risk_tags=[],
            package_hash="",
            status="invalid",
        )

    @staticmethod
    def _reject_symlinks(package: Path) -> None:
        for root, directories, files in os.walk(package, followlinks=False):
            for name in [*directories, *files]:
                if (Path(root) / name).is_symlink():
                    raise ValueError("工具包内禁止使用符号链接")

    @staticmethod
    def _validate_input_schema(schema: object) -> None:
        """Validate the stable subset of JSON Schema Pi exposes to a Tool.

        AXIOM deliberately does not execute a package while scanning.  A full
        JSON-Schema dependency would add a second validator with subtly
        different draft semantics, so the catalog validates the contract Pi
        needs for tool registration: an object root, object properties,
        string required names, and no required property outside properties.
        Nested schemas are checked recursively for the same basic shape.
        """

        if not isinstance(schema, dict) or schema.get("type") != "object":
            raise ValueError("parameters 必须是 type=object 的 JSON Schema 对象")
        properties = schema.get("properties", {})
        if not isinstance(properties, dict):
            raise ValueError("parameters.properties 必须是对象")
        required = schema.get("required", [])
        if not isinstance(required, list) or not all(
            isinstance(item, str) and item.strip() for item in required
        ):
            raise ValueError("parameters.required 必须是字符串数组")
        unknown_required = set(required) - set(properties)
        if unknown_required:
            raise ValueError(
                "parameters.required 包含未声明属性："
                + ", ".join(sorted(unknown_required))
            )
        additional = schema.get("additionalProperties", True)
        if not isinstance(additional, (bool, dict)):
            raise ValueError("parameters.additionalProperties 必须是布尔值或 Schema 对象")
        for name, property_schema in properties.items():
            if not isinstance(name, str) or not name.strip():
                raise ValueError("parameters.properties 的属性名必须是非空字符串")
            if not isinstance(property_schema, dict):
                raise ValueError(f"parameters.properties.{name} 必须是 Schema 对象")
            nested_type = property_schema.get("type")
            if nested_type == "object":
                AxiomToolService._validate_input_schema(property_schema)
            elif nested_type == "array":
                items = property_schema.get("items")
                if items is not None and not isinstance(items, dict):
                    raise ValueError(f"parameters.properties.{name}.items 必须是 Schema 对象")
            elif nested_type is not None and not isinstance(nested_type, (str, list)):
                raise ValueError(f"parameters.properties.{name}.type 无效")

    @staticmethod
    def _package_hash(package: Path) -> str:
        digest = hashlib.sha256()
        for path in sorted(package.rglob("*")):
            if not path.is_file():
                continue
            relative = path.relative_to(package).as_posix()
            digest.update(relative.encode("utf-8"))
            digest.update(b"\0")
            digest.update(path.read_bytes())
            digest.update(b"\0")
        return digest.hexdigest()

    @staticmethod
    def _key(value: str) -> str:
        key = value.strip()
        if not _KEY.fullmatch(key):
            raise ConflictError("Invalid AXIOM Tool key")
        return key

    @staticmethod
    def _require_bindable(row, *, key: str, phase: str) -> None:
        if row is None:
            raise NotFoundError(f"AXIOM Tool not found: {key}")
        if row["status"] != "ready":
            raise ConflictError(f"AXIOM Tool {key} is not ready")
        if not row["enabled"]:
            raise ConflictError(f"AXIOM Tool {key} is disabled")
        phases = json.loads(row["supported_phases_json"] or "[]")
        if phase not in phases:
            raise ConflictError(f"AXIOM Tool {key} does not support {phase}")

    @staticmethod
    def _binding_effective(worker, tool_row, phase: str) -> tuple[bool, str]:
        if worker is None:
            return False, "Worker 不存在"
        if worker.disabled:
            return False, "Worker 已禁用"
        if phase not in worker.capabilities:
            return False, f"Worker 未参与 {phase} 阶段"
        if tool_row is None:
            return False, "工具目录记录不存在"
        if tool_row["status"] != "ready":
            return False, f"工具状态为 {tool_row['status']}"
        if not tool_row["enabled"]:
            return False, "工具已全局停用"
        if phase not in json.loads(tool_row["supported_phases_json"] or "[]"):
            return False, "工具不支持该阶段"
        return True, ""

    @staticmethod
    def _usage(conn) -> dict[str, dict[str, object]]:
        result: dict[str, dict[str, object]] = defaultdict(
            lambda: {"call_count": 0, "last_used_at": "", "last_load_error": ""}
        )
        rows = conn.execute(
            select(audit_events.c.event_type, audit_events.c.payload_json, audit_events.c.created_at)
            .where(audit_events.c.event_type.like("axiom_tool.%"))
            .order_by(audit_events.c.created_at)
        ).mappings()
        for row in rows:
            try:
                payload = json.loads(row["payload_json"] or "{}")
            except json.JSONDecodeError:
                continue
            key = str(payload.get("tool_key") or "")
            if not key:
                continue
            if row["event_type"] == "axiom_tool.executed":
                result[key]["call_count"] = int(result[key]["call_count"]) + 1
                result[key]["last_used_at"] = row["created_at"]
            elif row["event_type"] == "axiom_tool.load_failed":
                result[key]["last_load_error"] = str(payload.get("error") or "加载失败")
        return dict(result)

    @staticmethod
    def _active_counts(conn) -> dict[str, int]:
        result: dict[str, int] = defaultdict(int)
        statuses = ["claimed", "running", "concluding"]
        for table in (intents, reason_runs, vulnerability_review_runs):
            for row in conn.execute(
                select(table.c.worker, func.count().label("count"))
                .where(table.c.worker.is_not(None), table.c.status.in_(statuses))
                .group_by(table.c.worker)
            ).mappings():
                result[row["worker"]] += int(row["count"])
        return dict(result)

    @staticmethod
    def _public_worker_config(worker, config: PiWorkerConfig | None) -> dict[str, Any]:
        """Return the small, secret-free configuration used by the Worker UI.

        The ordinary Worker endpoint remains backwards compatible for the
        existing settings editor.  The new overview endpoint must not expose
        API keys or arbitrary environment variables just to render a card.
        """

        if config is None:
            return {"worker_type": worker.worker_type}
        return {
            "binary": config.binary,
            "provider": config.provider,
            "model": config.model,
            "base_url": config.base_url,
            "provider_api": config.provider_api,
            "context_window": config.context_window,
            "thinking_enabled": config.thinking_enabled,
            "thinking_level": config.effective_thinking_level,
            "tools": list(config.tools),
            "skill_paths": list(config.skill_paths),
            "mcp_enabled": bool(
                isinstance(worker.config.get("mcp"), dict)
                and worker.config["mcp"].get("enabled")
            ),
        }
