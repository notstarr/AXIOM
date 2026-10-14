from __future__ import annotations

import hashlib
import json
import os
import re
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy import Engine, select

from axiom.modules.axiom_tools.repository import AxiomToolRepository
from axiom.modules.mcp.tables import mcp_servers, mcp_tools
from axiom.modules.mcp.schema_order import public_input_schema
from axiom.modules.phase_capabilities.builtins import builtin_tool_payloads
from axiom.modules.phase_capabilities.repository import PhaseCapabilityRepository
from axiom.modules.phase_capabilities.schemas import (
    CapabilityScopeValues,
    CapabilitySelection,
    PhaseCapabilitiesResponse,
    PhaseCapabilitiesUpdate,
    PhaseCapabilityRecord,
)
from axiom.modules.skills.service import MANDATORY_COMMON_SKILLS, SkillService
from axiom.shared.clock import utcnow
from axiom.shared.database import transaction
from axiom.shared.errors import ConflictError, NotFoundError


PHASES = ("bootstrap", "reason", "explore", "plan", "review")
SCOPES = ("common", *PHASES)
KINDS = ("skill", "mcp_tool", "axiom_tool")
_DEFAULTS_MARKER = ".phase-capabilities-v1-initialized"
_BROWSER_BOOTSTRAP_MARKER = ".phase-capabilities-v2-browser-bootstrap"
_MCP_NAME_PART = re.compile(r"[^a-z0-9_]+")
_DEFAULT_SKILL_SCOPES = {
    "vuln-review": ("review",),
    "browser-control": ("bootstrap", "explore"),
    "redteam-scoring": ("explore",),
    "redteam-review": ("review",),
}


@dataclass(frozen=True, slots=True)
class LeaseCapabilitySnapshot:
    skill_directories: list[str]
    mcp_tool_names: list[str]
    mcp_environment: dict[str, str]
    axiom_tool_keys: list[str]
    axiom_tool_extensions: list[str]
    axiom_tool_snapshot_path: str | None
    capability_snapshot_path: str
    mcp_mode: str = "legacy"


class PhaseCapabilityService:
    """One capability matrix shared by the logical stage Workers."""

    def __init__(
        self,
        engine: Engine,
        data_dir: Path,
        *,
        skills: SkillService,
        initialize_defaults: bool = True,
    ):
        self.engine = engine
        self.skills = skills
        self.repository = PhaseCapabilityRepository()
        self.axiom_tools = AxiomToolRepository()
        self.data_dir = data_dir.resolve()
        if initialize_defaults:
            self._initialize_existing_skills_once()

    def matrix(self, *, skill_records=None) -> PhaseCapabilitiesResponse:
        # A lease snapshot may already have loaded the Skill catalog outside
        # the database connection. Reuse it so large Skill packages are not
        # scanned repeatedly while a PostgreSQL transaction is open.
        if skill_records is None:
            skill_records = self.skills.list()
        with self.engine.connect() as conn:
            bindings = self.repository.list_rows(conn)
            axiom_rows = self.axiom_tools.list_rows(conn)
            mcp_rows = conn.execute(
                select(
                    mcp_tools,
                    mcp_servers.c.name.label("server_name"),
                    mcp_servers.c.disabled.label("server_disabled"),
                    mcp_servers.c.discovery_status.label("server_status"),
                ).join(mcp_servers, mcp_servers.c.id == mcp_tools.c.server_id)
            ).mappings().all()
        scopes = {
            scope: CapabilityScopeValues().model_dump(mode="json") for scope in SCOPES
        }
        scopes_by_capability: dict[tuple[str, str], list[str]] = defaultdict(list)
        for row in bindings:
            scopes[row["scope"]][row["capability_kind"]].append(row["capability_key"])
            scopes_by_capability[(row["capability_kind"], row["capability_key"])].append(
                row["scope"]
            )
        items: list[PhaseCapabilityRecord] = []
        for skill in skill_records:
            item_scopes = self._ordered_scopes(scopes_by_capability[("skill", skill.name)])
            skill_ready = Path(skill.path).is_file()
            inactive = {
                phase: (
                    "Skill 文件不存在"
                    if not skill_ready
                    else "Skill 已全局停用"
                )
                for phase in PHASES
                if self._assigned(item_scopes, phase)
                and (not skill.enabled or not skill_ready)
            }
            items.append(
                PhaseCapabilityRecord(
                    capability_kind="skill",
                    capability_key=skill.name,
                    title=skill.title,
                    description=skill.description,
                    enabled=skill.enabled,
                    ready=skill_ready,
                    status="ready" if skill_ready else "missing",
                    scopes=item_scopes,
                    locked_scopes=(
                        ["common"] if skill.name in MANDATORY_COMMON_SKILLS else []
                    ),
                    inactive_reasons=inactive,
                    metadata={"source": skill.source, "path": skill.path},
                )
            )
        for row in axiom_rows:
            item_scopes = self._ordered_scopes(
                scopes_by_capability[("axiom_tool", row["key"])]
            )
            supported = json.loads(row["supported_phases_json"] or "[]")
            inactive: dict[str, str] = {}
            for phase in PHASES:
                if not self._assigned(item_scopes, phase):
                    continue
                reason = self._axiom_inactive_reason(row, phase)
                if reason:
                    inactive[phase] = reason
            items.append(
                PhaseCapabilityRecord(
                    capability_kind="axiom_tool",
                    capability_key=row["key"],
                    title=row["title"],
                    description=row["description"],
                    enabled=bool(row["enabled"]),
                    ready=row["status"] == "ready",
                    status=row["status"],
                    supported_phases=supported,
                    scopes=item_scopes,
                    inactive_reasons=inactive,
                    metadata={
                        "version": row["version"],
                        "package_hash": row["package_hash"],
                        "risk_tags": json.loads(row["risk_tags_json"] or "[]"),
                    },
                )
            )
        for row in mcp_rows:
            item_scopes = self._ordered_scopes(
                scopes_by_capability[("mcp_tool", row["id"])]
            )
            enabled = bool(row["enabled"]) and not bool(row["server_disabled"])
            ready = enabled and row["server_status"] == "ready"
            inactive: dict[str, str] = {}
            for phase in PHASES:
                if not self._assigned(item_scopes, phase):
                    continue
                if row["server_disabled"]:
                    inactive[phase] = "MCP Server 已停用"
                elif not row["enabled"]:
                    inactive[phase] = "MCP Tool 已全局停用"
                elif row["server_status"] != "ready":
                    inactive[phase] = f"MCP Server 状态为 {row['server_status']}"
            items.append(
                PhaseCapabilityRecord(
                    capability_kind="mcp_tool",
                    capability_key=row["id"],
                    title=row["title"] or row["name"],
                    description=row["description"],
                    enabled=enabled,
                    ready=ready,
                    status=(
                        "disabled" if row["server_disabled"] else row["server_status"]
                    ),
                    server_id=row["server_id"],
                    server_name=row["server_name"],
                    scopes=item_scopes,
                    inactive_reasons=inactive,
                    metadata={"tool_name": row["name"]},
                )
            )
        items.sort(
            key=lambda item: (
                item.capability_kind,
                item.title.casefold(),
                item.capability_key,
            )
        )
        return PhaseCapabilitiesResponse(
            scopes={key: CapabilityScopeValues(**value) for key, value in scopes.items()},
            items=items,
            builtin_tools=builtin_tool_payloads(),
        )

    def save_matrix(self, command: PhaseCapabilitiesUpdate) -> PhaseCapabilitiesResponse:
        requested = command.model_dump(mode="json")
        for kind in KINDS:
            common_keys = set(requested["common"][kind])
            for phase in PHASES:
                requested[phase][kind] = [
                    key for key in requested[phase][kind] if key not in common_keys
                ]
        missing_locked = MANDATORY_COMMON_SKILLS - set(
            requested["common"]["skill"]
        )
        if missing_locked:
            raise ConflictError(
                "以下 Skill 是锁定的通用能力，不能取消："
                + ", ".join(sorted(missing_locked))
            )
        current = self._binding_sets()
        with transaction(self.engine) as conn:
            for scope in SCOPES:
                values = requested[scope]
                for kind in KINDS:
                    keys = list(values[kind])
                    existing = current.get((scope, kind), set())
                    for key in set(keys) - existing:
                        self._require_bindable(conn, kind=kind, key=key, scope=scope)
                    self.repository.replace_scope(
                        conn,
                        scope=scope,
                        capability_kind=kind,
                        capability_keys=keys,
                        now=utcnow(),
                    )
            self._ensure_mandatory_locked(conn)
        return self.matrix()

    def save_capability(
        self,
        kind: str,
        key: str,
        command: CapabilitySelection,
    ) -> PhaseCapabilitiesResponse:
        kind = self._kind(kind)
        key = key.strip()
        if not key:
            raise ConflictError("Capability key must not be empty")
        scopes = list(command.scopes)
        if kind == "skill" and key in MANDATORY_COMMON_SKILLS and "common" not in scopes:
            raise ConflictError(f"Skill {key} 是锁定的通用能力")
        with transaction(self.engine) as conn:
            existing = {
                row["scope"]
                for row in self.repository.list_rows(
                    conn, capability_kind=kind, capability_key=key
                )
            }
            for scope in set(scopes) - existing:
                self._require_bindable(conn, kind=kind, key=key, scope=scope)
            self.repository.replace_capability(
                conn,
                capability_kind=kind,
                capability_key=key,
                scopes=scopes,
                now=utcnow(),
            )
            self._ensure_mandatory_locked(conn)
        return self.matrix()

    def replace_scope(self, *, kind: str, scope: str, keys: list[str]) -> None:
        """Compatibility helper for retired Worker-specific binding endpoints."""

        kind = self._kind(kind)
        scope = self._scope(scope)
        keys = list(dict.fromkeys(key.strip() for key in keys if key.strip()))
        with transaction(self.engine) as conn:
            existing = {
                row["capability_key"]
                for row in self.repository.list_rows(
                    conn, scope=scope, capability_kind=kind
                )
            }
            for key in set(keys) - existing:
                self._require_bindable(conn, kind=kind, key=key, scope=scope)
            self.repository.replace_scope(
                conn,
                scope=scope,
                capability_kind=kind,
                capability_keys=keys,
                now=utcnow(),
            )
            self._ensure_mandatory_locked(conn)

    def scopes_for(self, *, kind: str, key: str) -> list[str]:
        with self.engine.connect() as conn:
            rows = self.repository.list_rows(
                conn, capability_kind=self._kind(kind), capability_key=key
            )
        return self._ordered_scopes([row["scope"] for row in rows])

    def effective_keys(self, *, phase: str, kind: str) -> list[str]:
        phase = self._phase(phase)
        kind = self._kind(kind)
        with self.engine.connect() as conn:
            rows = self.repository.list_rows(conn, capability_kind=kind)
            requested = sorted(
                {
                    row["capability_key"]
                    for row in rows
                    if row["scope"] in {"common", phase}
                }
            )
            if kind != "skill":
                return [
                    key
                    for key in requested
                    if self._inactive_reason(conn, kind=kind, key=key, phase=phase) == ""
                ]

        # Do not perform Skill filesystem discovery while the connection above
        # is still checked out. SkillService.list() is cached, but a package
        # refresh may still need to inspect large support files.
        skill_map = {skill.name: skill for skill in self.skills.list()}
        return [
            key
            for key in requested
            if (
                (skill := skill_map.get(key)) is not None
                and skill.enabled
                and Path(skill.path).is_file()
            )
        ]

    def snapshot_for_lease(
        self,
        *,
        worker_name: str,
        phase: str,
        run_dir: str | Path,
    ) -> LeaseCapabilitySnapshot:
        phase = self._phase(phase)
        directory = Path(run_dir).resolve()
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / "phase-capabilities.json"
        if path.is_file():
            return self._snapshot_from_file(path, worker_name=worker_name, phase=phase)

        # Load the Skill catalog once, before opening any database connection.
        # The catalog may contain very large dictionaries and should never hold
        # an idle PostgreSQL transaction open while it is being synchronized.
        skill_records = self.skills.list()
        matrix = self.matrix(skill_records=skill_records)
        item_map = {
            (item.capability_kind, item.capability_key): item for item in matrix.items
        }

        def effective_matrix_keys(kind: str) -> list[str]:
            return sorted(
                item.capability_key
                for item in matrix.items
                if item.capability_kind == kind
                and self._assigned(item.scopes, phase)
                and phase not in item.inactive_reasons
            )

        skill_keys = effective_matrix_keys("skill")
        mcp_keys = effective_matrix_keys("mcp_tool")
        axiom_keys = effective_matrix_keys("axiom_tool")
        skill_map = {skill.name: skill for skill in skill_records}
        skill_items: list[dict[str, Any]] = []
        for key in skill_keys:
            skill = skill_map.get(key)
            if skill is None:
                continue
            skill_path = Path(skill.path)
            skill_items.append(
                {
                    "key": key,
                    "scope": self._effective_scope(item_map[("skill", key)].scopes, phase),
                    "directory": str(skill_path.parent),
                    # Skill packages can include references, scripts, profiles,
                    # and other files that are just as relevant to execution as
                    # SKILL.md.  The Skill service already computes a stable
                    # directory hash after synchronizing the effective package;
                    # persist that value so a new Lease records attachment-only
                    # updates too.
                    "content_hash": skill.effective_hash
                    or self._file_hash(skill_path),
                }
            )

        with self.engine.connect() as conn:
            mcp_rows = self._selected_mcp_rows(conn, mcp_keys)
            axiom_rows = [
                row
                for key in axiom_keys
                if (row := self.axiom_tools.get_row(conn, key)) is not None
            ]
        server_configs: dict[str, dict[str, Any]] = {}
        snapshot_servers: dict[str, dict[str, Any]] = {}
        mcp_items: list[dict[str, Any]] = []
        allowed_mcp_tools: list[str] = []
        mcp_mode = os.environ.get("AXIOM_PI_MCP_MODE", "pi_native").strip().lower() or "pi_native"
        if mcp_mode not in {"pi_native", "legacy"}:
            raise ConflictError(f"AXIOM_PI_MCP_MODE 无效：{mcp_mode}")
        native_names = self._native_mcp_names(mcp_rows) if mcp_mode == "pi_native" else {}
        for row in mcp_rows:
            legacy_name = self.mcp_runtime_tool_name(row["server_name"], row["name"], row["id"])
            allowed_name = native_names[row["id"]] if mcp_mode == "pi_native" else legacy_name
            allowed_mcp_tools.append(allowed_name)
            runtime_config = self._mcp_runtime_config(row)
            native_server_name = native_names.get(f"server:{row['server_id']}", row["server_name"])
            server_key = native_server_name if mcp_mode == "pi_native" else row["server_name"]
            server_configs[server_key] = runtime_config
            server_entry = snapshot_servers.setdefault(server_key, {
                "id": row["server_id"],
                "name": row["server_name"],
                "native_name": native_server_name,
                "transport": row["transport"],
                "config": runtime_config,
                "config_hash": self._json_hash(runtime_config),
                "tool_exposure": {},
            })
            server_entry["tool_exposure"][row["name"]] = "direct"
            input_schema = public_input_schema(json.loads(row["input_schema_json"] or "{}"))
            output_schema = json.loads(row["output_schema_json"] or "{}")
            mcp_items.append(
                {
                    "key": row["id"],
                    "scope": self._effective_scope(
                        item_map[("mcp_tool", row["id"])].scopes, phase
                    ),
                    "server_id": row["server_id"],
                    "server_name": row["server_name"],
                    "native_server_name": native_server_name,
                    "tool_name": row["name"],
                    "active_name": allowed_name,
                    "native_name": allowed_name,
                    "legacy_name": legacy_name,
                    "title": row["title"] or row["name"],
                    "description": row["description"],
                    "input_schema": input_schema,
                    "output_schema": output_schema,
                    "annotations": json.loads(row["annotations_json"] or "{}"),
                    "updated_at": row["updated_at"],
                    "schema_hash": self._json_hash(
                        {"input": input_schema, "output": output_schema}
                    ),
                }
            )

        axiom_items = [
            {
                "key": row["key"],
                "scope": self._effective_scope(
                    item_map[("axiom_tool", row["key"])].scopes, phase
                ),
                "version": row["version"],
                "package_hash": row["package_hash"],
                "package_path": row["package_path"],
                "entrypoint": row["entrypoint"],
                "input_schema": json.loads(row["input_schema_json"] or "{}"),
            }
            for row in axiom_rows
        ]
        payload = {
            "schema_version": 3,
            "worker_backend": worker_name,
            "phase": phase,
            "mcp_mode": mcp_mode,
            "created_at": utcnow(),
            "builtin_tools": builtin_tool_payloads(phase=phase),
            "skills": skill_items,
            "mcp_tools": mcp_items,
            "mcp_servers": list(snapshot_servers.values()),
            "axiom_tools": axiom_items,
        }
        self._write_json(path, payload)
        axiom_path = directory / "axiom-tools.json"
        if axiom_items:
            self._write_json(
                axiom_path,
                {
                    "schema_version": 1,
                    "worker": worker_name,
                    "phase": phase,
                    "tools": axiom_items,
                },
            )
        environment = self._mcp_environment(server_configs, allowed_mcp_tools, mode=mcp_mode)
        return LeaseCapabilitySnapshot(
            skill_directories=[item["directory"] for item in skill_items],
            mcp_tool_names=allowed_mcp_tools,
            mcp_environment=environment,
            axiom_tool_keys=[item["key"] for item in axiom_items],
            axiom_tool_extensions=[
                str(Path(item["package_path"]) / item["entrypoint"])
                for item in axiom_items
            ],
            axiom_tool_snapshot_path=str(axiom_path) if axiom_items else None,
            capability_snapshot_path=str(path),
            mcp_mode=mcp_mode,
        )

    def _snapshot_from_file(
        self,
        path: Path,
        *,
        worker_name: str,
        phase: str,
    ) -> LeaseCapabilitySnapshot:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ConflictError(f"Lease capability snapshot is unreadable: {path}") from exc
        if (
            not isinstance(payload, dict)
            or payload.get("schema_version") not in {1, 2, 3}
            or payload.get("worker_backend") != worker_name
            or payload.get("phase") != phase
        ):
            raise ConflictError("Lease capability snapshot does not match the active Lease")
        skills = payload.get("skills") if isinstance(payload.get("skills"), list) else []
        mcp_items = payload.get("mcp_tools") if isinstance(payload.get("mcp_tools"), list) else []
        axiom_items = payload.get("axiom_tools") if isinstance(payload.get("axiom_tools"), list) else []
        schema_version = int(payload.get("schema_version") or 1)
        mcp_mode = str(payload.get("mcp_mode") or "legacy").strip().lower()
        if mcp_mode not in {"pi_native", "legacy"}:
            raise ConflictError(f"Lease capability snapshot has invalid MCP mode: {mcp_mode}")
        if schema_version < 3:
            # Pre-native snapshots never carried an execution mode and must
            # stay on the legacy adapter during the drain window.
            mcp_mode = "legacy"
        snapshot_servers = payload.get("mcp_servers")
        server_configs: dict[str, dict[str, Any]] = {}
        if isinstance(snapshot_servers, list):
            for item in snapshot_servers:
                if not isinstance(item, dict):
                    continue
                name = str(
                    (item.get("native_name") if mcp_mode == "pi_native" else item.get("name"))
                    or item.get("native_name")
                    or ""
                )
                config = item.get("config")
                if name and isinstance(config, dict):
                    server_configs[name] = config
        # Lease snapshots created before phase-capability server configs were
        # persisted are still readable.  New snapshots never take this path,
        # so a restart cannot silently replace their MCP credentials/config.
        if not server_configs and mcp_items and schema_version >= 3:
            raise ConflictError(
                "Lease capability snapshot is missing immutable MCP server configuration"
            )
        if not server_configs and mcp_items:
            with self.engine.connect() as conn:
                selected_rows = self._selected_mcp_rows(
                    conn,
                    [
                        str(item.get("key") or "")
                        for item in mcp_items
                        if isinstance(item, dict)
                    ],
                )
            server_configs = {
                row["server_name"]: self._mcp_runtime_config(row, allow_sse=True)
                for row in selected_rows
            }
        allowed = [
            str(
                (item.get("native_name") if mcp_mode == "pi_native" else item.get("active_name"))
                or item.get("active_name")
                or ""
            )
            for item in mcp_items
            if isinstance(item, dict)
            and str(
                (item.get("native_name") if mcp_mode == "pi_native" else item.get("active_name"))
                or item.get("active_name")
                or ""
            )
        ]
        axiom_path = path.with_name("axiom-tools.json")
        return LeaseCapabilitySnapshot(
            skill_directories=[
                str(item.get("directory") or "")
                for item in skills
                if isinstance(item, dict) and str(item.get("directory") or "")
            ],
            mcp_tool_names=allowed,
            mcp_environment=self._mcp_environment(server_configs, allowed, mode=mcp_mode),
            axiom_tool_keys=[
                str(item.get("key") or "")
                for item in axiom_items
                if isinstance(item, dict) and str(item.get("key") or "")
            ],
            axiom_tool_extensions=[
                str(Path(str(item.get("package_path") or "")) / str(item.get("entrypoint") or ""))
                for item in axiom_items
                if isinstance(item, dict)
                and str(item.get("package_path") or "")
                and str(item.get("entrypoint") or "")
            ],
            axiom_tool_snapshot_path=str(axiom_path) if axiom_items else None,
            capability_snapshot_path=str(path),
            mcp_mode=mcp_mode,
        )

    def _initialize_existing_skills_once(self) -> None:
        marker = self.data_dir / _DEFAULTS_MARKER
        browser_marker = self.data_dir / _BROWSER_BOOTSTRAP_MARKER
        skill_records = self.skills.list()
        with transaction(self.engine) as conn:
            if not marker.exists():
                now = utcnow()
                for skill in skill_records:
                    if not skill.enabled:
                        continue
                    if self.repository.list_rows(
                        conn, capability_kind="skill", capability_key=skill.name
                    ):
                        continue
                    for scope in _DEFAULT_SKILL_SCOPES.get(skill.name, ("common",)):
                        self.repository.ensure(
                            conn,
                            scope=scope,
                            capability_kind="skill",
                            capability_key=skill.name,
                            now=now,
                        )

            # Existing installations were initialized before Bootstrap could
            # use the native Browser Broker.  Add only the new Bootstrap
            # binding once, preserving any user choice about the Explore
            # binding and avoiding a startup-time re-enable after the marker.
            if not browser_marker.exists():
                browser_skill = next(
                    (skill for skill in skill_records if skill.name == "browser-control"),
                    None,
                )
                if (
                    browser_skill is not None
                    and browser_skill.enabled
                    and Path(browser_skill.path).is_file()
                ):
                    self.repository.ensure(
                        conn,
                        scope="bootstrap",
                        capability_kind="skill",
                        capability_key="browser-control",
                        now=utcnow(),
                    )
            self._ensure_mandatory_locked(conn, skill_records=skill_records)
        if not marker.exists():
            marker.parent.mkdir(parents=True, exist_ok=True)
            temporary = marker.with_name(f".{marker.name}.tmp")
            temporary.write_text(utcnow() + "\n", encoding="utf-8")
            temporary.replace(marker)
        if not browser_marker.exists():
            browser_marker.parent.mkdir(parents=True, exist_ok=True)
            temporary = browser_marker.with_name(f".{browser_marker.name}.tmp")
            temporary.write_text(utcnow() + "\n", encoding="utf-8")
            temporary.replace(browser_marker)

    def _ensure_mandatory_locked(self, conn, *, skill_records=None) -> None:
        now = utcnow()
        if skill_records is None:
            skill_records = self.skills.list()
        available = {skill.name for skill in skill_records}
        for name in MANDATORY_COMMON_SKILLS & available:
            self.repository.ensure(
                conn,
                scope="common",
                capability_kind="skill",
                capability_key=name,
                now=now,
            )

    def _binding_sets(self) -> dict[tuple[str, str], set[str]]:
        with self.engine.connect() as conn:
            rows = self.repository.list_rows(conn)
        result: dict[tuple[str, str], set[str]] = defaultdict(set)
        for row in rows:
            result[(row["scope"], row["capability_kind"])].add(row["capability_key"])
        return result

    def _require_bindable(self, conn, *, kind: str, key: str, scope: str) -> None:
        phases = PHASES if scope == "common" else (scope,)
        if kind == "skill":
            skill = next((item for item in self.skills.list() if item.name == key), None)
            if skill is None:
                raise NotFoundError(f"Skill not found: {key}")
            if not skill.enabled:
                raise ConflictError(f"Skill {key} is disabled")
            if not Path(skill.path).is_file():
                raise ConflictError(f"Skill {key} is missing")
            return
        if kind == "mcp_tool":
            row = conn.execute(
                select(
                    mcp_tools,
                    mcp_servers.c.disabled.label("server_disabled"),
                    mcp_servers.c.discovery_status,
                )
                .join(mcp_servers, mcp_servers.c.id == mcp_tools.c.server_id)
                .where(mcp_tools.c.id == key)
            ).mappings().one_or_none()
            if row is None:
                raise NotFoundError(f"MCP Tool not found: {key}")
            if row["server_disabled"] or not row["enabled"]:
                raise ConflictError(f"MCP Tool {key} is disabled")
            if row["discovery_status"] != "ready":
                raise ConflictError(
                    f"MCP Tool {key} 所属 Server 尚未就绪：{row['discovery_status']}"
                )
            return
        row = self.axiom_tools.get_row(conn, key)
        if row is None:
            raise NotFoundError(f"AXIOM Tool not found: {key}")
        if scope == "common":
            supported = set(json.loads(row["supported_phases_json"] or "[]"))
            missing = [phase for phase in PHASES if phase not in supported]
            if missing:
                raise ConflictError(
                    f"AXIOM Tool {key} 不能设为通用能力；通用能力必须支持五个阶段，"
                    f"当前缺少：{', '.join(missing)}"
                )
        for phase in phases:
            reason = self._axiom_inactive_reason(row, phase)
            if reason:
                raise ConflictError(f"AXIOM Tool {key} cannot bind to {scope}: {reason}")

    def _inactive_reason(self, conn, *, kind: str, key: str, phase: str) -> str:
        if kind == "skill":
            skill = next((item for item in self.skills.list() if item.name == key), None)
            if skill is None:
                return "Skill 不存在"
            if not skill.enabled:
                return "Skill 已全局停用"
            return "" if Path(skill.path).is_file() else "Skill 文件不存在"
        if kind == "mcp_tool":
            row = conn.execute(
                select(
                    mcp_tools.c.enabled,
                    mcp_servers.c.disabled.label("server_disabled"),
                    mcp_servers.c.discovery_status,
                )
                .join(mcp_servers, mcp_servers.c.id == mcp_tools.c.server_id)
                .where(mcp_tools.c.id == key)
            ).mappings().one_or_none()
            if row is None:
                return "MCP Tool 不存在"
            if row["server_disabled"]:
                return "MCP Server 已停用"
            if not row["enabled"]:
                return "MCP Tool 已全局停用"
            return "" if row["discovery_status"] == "ready" else (
                f"MCP Server 状态为 {row['discovery_status']}"
            )
        row = self.axiom_tools.get_row(conn, key)
        return self._axiom_inactive_reason(row, phase) if row is not None else "AXIOM Tool 不存在"

    @staticmethod
    def _axiom_inactive_reason(row, phase: str) -> str:
        if row["status"] != "ready":
            return f"工具状态为 {row['status']}"
        if not row["enabled"]:
            return "工具已全局停用"
        if phase not in json.loads(row["supported_phases_json"] or "[]"):
            return "工具不支持该阶段"
        return ""

    @staticmethod
    def _selected_mcp_rows(conn, keys: list[str]):
        keys = [key for key in keys if key]
        if not keys:
            return []
        return conn.execute(
            select(
                mcp_tools,
                mcp_servers.c.name.label("server_name"),
                mcp_servers.c.command,
                mcp_servers.c.args_json,
                mcp_servers.c.env_json,
                mcp_servers.c.url,
                mcp_servers.c.transport,
                mcp_servers.c.raw_config_json,
            )
            .join(mcp_servers, mcp_servers.c.id == mcp_tools.c.server_id)
            .where(
                mcp_tools.c.id.in_(keys),
                mcp_tools.c.enabled.is_(True),
                mcp_servers.c.disabled.is_(False),
                mcp_servers.c.discovery_status == "ready",
            )
            .order_by(mcp_servers.c.name, mcp_tools.c.name)
        ).mappings().all()

    @staticmethod
    def mcp_runtime_tool_name(server_name: str, tool_name: str, tool_id: str) -> str:
        def part(value: str) -> str:
            normalized = _MCP_NAME_PART.sub("_", value.strip().lower()).strip("_")
            return (normalized or "tool")[:36]

        suffix = hashlib.sha256(tool_id.encode("utf-8")).hexdigest()[:8]
        return f"mcp__{part(server_name)}__{part(tool_name)}__{suffix}"

    @staticmethod
    def _native_mcp_names(rows) -> dict[str, str]:
        """Build the exact Pi 1.1 MCP tool names for this snapshot.

        Pi sanitizes the complete ``mcp__<server>__<tool>`` name and appends
        the first eight SHA-256 hex digits of ``server + NUL + tool`` when a
        sanitized name collides or exceeds 64 characters.  Keeping that logic
        here makes the immutable Lease snapshot agree with the names Pi will
        actually register; a mismatch is therefore detected before a Worker
        can run.  Server names are intentionally left intact: Pi performs the
        same sanitization after ``registerMcpServer`` receives them.
        """

        rows = list(rows)
        result: dict[str, str] = {}
        server_ids_by_name: dict[str, str] = {}
        server_by_id: dict[str, str] = {}
        for row in rows:
            server_id = str(row["server_id"])
            server_name = str(row["server_name"])
            previous = server_ids_by_name.get(server_name)
            if previous is not None and previous != server_id:
                raise ConflictError(
                    f"MCP Server 名称与 Pi 原生注册冲突：{server_name}"
                )
            server_ids_by_name[server_name] = server_id
            server_by_id[server_id] = server_name

        def pi_base(server_name: str, tool_name: str) -> str:
            return re.sub(
                r"[^A-Za-z0-9_]", "_", f"mcp__{server_name}__{tool_name}"
            )

        bases = [
            pi_base(server_by_id[str(row["server_id"])], str(row["name"]))
            for row in rows
        ]
        base_counts: dict[str, int] = defaultdict(int)
        for base in bases:
            base_counts[base] += 1

        for row, base in zip(rows, bases, strict=True):
            server_name = server_by_id[str(row["server_id"])]
            tool_name = str(row["name"])
            if len(base) <= 64 and base_counts[base] == 1:
                native_name = base
            else:
                suffix = hashlib.sha256(
                    f"{server_name}\0{tool_name}".encode("utf-8")
                ).hexdigest()[:8]
                native_name = f"{base[:55]}_{suffix}"
            result[str(row["id"])] = native_name
            result[f"server:{row['server_id']}"] = server_name
        return result

    @staticmethod
    def _mcp_runtime_config(row, *, allow_sse: bool = False) -> dict[str, Any]:
        transport = str(row["transport"] or "").strip().lower()
        url = str(row["url"] or "").strip()
        is_sse = transport == "sse" or url.lower().endswith("/sse")
        if is_sse and not allow_sse:
            raise ConflictError(
                f"MCP Server {row.get('server_name', row.get('name', 'unknown'))} 仍使用 legacy SSE，不能切换到 Pi 原生 MCP"
            )
        if row["command"]:
            if transport and transport != "stdio":
                raise ConflictError(
                    f"MCP Server {row.get('server_name', row.get('name', 'unknown'))} 使用了不支持的 transport：{transport}"
                )
            try:
                args = json.loads(row["args_json"] or "[]")
                env = json.loads(row["env_json"] or "{}")
            except (TypeError, json.JSONDecodeError) as exc:
                raise ConflictError("MCP Server stdio 配置不是合法 JSON") from exc
            if not isinstance(args, list) or not isinstance(env, dict):
                raise ConflictError("MCP Server stdio args/env 类型无效")
            return {
                "command": row["command"],
                "args": args,
                "env": {
                    str(key): str(value)
                    for key, value in env.items()
                },
            }
        if is_sse and allow_sse:
            config: dict[str, Any] = {"url": url, "transport": "sse"}
        elif transport not in {"", "http", "streamable_http", "streamable-http"}:
            raise ConflictError(
                f"MCP Server {row.get('server_name', row.get('name', 'unknown'))} 使用了不支持的 transport：{transport}"
            )
        else:
            config = {"url": url, "type": "http"}
        try:
            raw = json.loads(row["raw_config_json"] or "{}")
        except (TypeError, json.JSONDecodeError) as exc:
            raise ConflictError("MCP Server HTTP 配置不是合法 JSON") from exc
        if not isinstance(raw, dict):
            raise ConflictError("MCP Server HTTP raw_config 类型无效")
        if "headers" in raw and not isinstance(raw["headers"], dict):
            raise ConflictError("MCP Server HTTP headers 类型无效")
        if isinstance(raw, dict) and isinstance(raw.get("headers"), dict) and raw.get("headers"):
            config["headers"] = {
                str(key): str(value) for key, value in raw["headers"].items()
            }
        return config

    @staticmethod
    def _mcp_environment(
        server_configs: dict[str, dict[str, Any]],
        allowed_tools: list[str],
        *,
        mode: str = "legacy",
    ) -> dict[str, str]:
        enabled = bool(server_configs and allowed_tools)
        # Native Pi MCP reads the immutable snapshot directly.  Do not copy
        # server credentials into an environment variable where an unrelated
        # extension could observe them; the legacy adapter still receives the
        # historical JSON payload during its drain window.
        config_payload = (
            {"mcpServers": server_configs if enabled else {}}
            if mode == "legacy"
            else {"mcpServers": {}}
        )
        return {
            "PI_MCP_ENABLED": "true" if enabled else "false",
            "AXIOM_MCP_MODE": mode if enabled else "legacy",
            "PI_MCP_CONFIG_JSON": json.dumps(
                config_payload,
                ensure_ascii=False,
                separators=(",", ":"),
            ),
            "PI_MCP_ALLOWED_SERVERS": ",".join(server_configs) if enabled else "",
            "PI_MCP_ALLOWED_SERVERS_JSON": json.dumps(
                list(server_configs) if enabled else [],
                ensure_ascii=False,
                separators=(",", ":"),
            ),
            "PI_MCP_ALLOWED_TOOLS": ",".join(allowed_tools) if enabled else "",
            # AXIOM registers only the selected concrete MCP Tools itself.
            # Clear the community adapter's optional direct-tool override so a
            # stale model-backend environment cannot expose sibling tools.
            "MCP_DIRECT_TOOLS": "",
        }

    @staticmethod
    def _effective_scope(scopes: list[str], phase: str) -> str:
        return "common" if "common" in scopes else phase

    @staticmethod
    def _assigned(scopes: list[str], phase: str) -> bool:
        return "common" in scopes or phase in scopes

    @staticmethod
    def _ordered_scopes(scopes: list[str]) -> list[str]:
        values = set(scopes)
        return [scope for scope in SCOPES if scope in values]

    @staticmethod
    def _phase(value: str) -> str:
        value = value.strip().lower()
        if value not in PHASES:
            raise ConflictError(f"Invalid Worker phase: {value}")
        return value

    @staticmethod
    def _scope(value: str) -> str:
        value = value.strip().lower()
        if value not in SCOPES:
            raise ConflictError(f"Invalid capability scope: {value}")
        return value

    @staticmethod
    def _kind(value: str) -> str:
        value = value.strip().lower()
        if value not in KINDS:
            raise ConflictError(f"Invalid capability kind: {value}")
        return value

    @staticmethod
    def _write_json(path: Path, payload: dict[str, Any]) -> None:
        temporary = path.with_name(f".{path.name}.tmp")
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        temporary.replace(path)

    @staticmethod
    def _file_hash(path: Path) -> str:
        try:
            return hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError:
            return ""

    @staticmethod
    def _json_hash(value: Any) -> str:
        return hashlib.sha256(
            json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
                "utf-8"
            )
        ).hexdigest()
