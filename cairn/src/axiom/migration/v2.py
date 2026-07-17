from __future__ import annotations

import hashlib
import ipaddress
import json
import re
import sqlite3
from collections import defaultdict
from pathlib import Path

from pydantic import BaseModel, Field

from axiom.infrastructure.database.engine import create_sqlite_engine
from axiom.infrastructure.database.metadata import (
    artifacts,
    credentials,
    facts,
    id_counters,
    intent_sources,
    intents,
    metadata,
    project_knowledge,
    projects,
    task_hints,
    tasks,
    vulnerabilities,
    dispatch_workers,
    runtime_settings,
)
from axiom.modules.common.time import utcnow
from axiom.modules.tasks.origin import parse_origin
from axiom.modules.settings.schemas import RuntimeConfig
from axiom.shared.keyring import KeyringError, decrypt_secret


V2_SCHEMA_REVISION = "51313e2f15db"


class MigrationReport(BaseModel):
    projects: int = 0
    tasks: int = 0
    facts: int = 0
    intents: int = 0
    hints: int = 0
    knowledge: int = 0
    vulnerabilities: int = 0
    credentials: int = 0
    assets: int = 0
    workers: int = 0
    runtime_settings: int = 0
    warnings: list[str] = Field(default_factory=list)


def migrate_legacy_database(
    source_path: str | Path,
    target_path: str | Path,
    *,
    data_dir: str | Path,
    mozzie_token: str = "",
) -> MigrationReport:
    source_path = Path(source_path).expanduser().resolve()
    target_path = Path(target_path).expanduser().resolve()
    data_dir = Path(data_dir).expanduser().resolve()
    if not source_path.is_file():
        raise ValueError(f"Legacy database not found: {source_path}")
    if target_path.exists() and target_path.stat().st_size > 0:
        raise ValueError(f"Target database must be empty or absent: {target_path}")

    source = sqlite3.connect(f"file:{source_path}?mode=ro", uri=True)
    source.row_factory = sqlite3.Row
    engine = create_sqlite_engine(target_path)
    metadata.create_all(engine)
    report = MigrationReport()
    now = utcnow()

    try:
        groups = _rows(source, "project_groups")
        legacy_tasks = _legacy_task_rows(source, report=report, now=now)
        parent_ids = {row["id"] for row in groups}
        parent_ids.update(row.get("parent_project_id") or "legacy-orphans" for row in legacy_tasks)
        all_project_map = _build_id_map(parent_ids, _project_id)
        group_map = {row["id"]: all_project_map[row["id"]] for row in groups}
        task_map = _build_task_id_map(legacy_tasks)
        task_group = {
            row["id"]: all_project_map[row.get("parent_project_id") or "legacy-orphans"]
            for row in legacy_tasks
        }
        facts_by_task: dict[str, list[dict]] = defaultdict(list)
        for row in _rows(source, "facts"):
            facts_by_task[row["project_id"]].append(row)

        with engine.begin() as conn:
            for row in groups:
                conn.execute(
                    projects.insert().values(
                        id=group_map[row["id"]],
                        title=row.get("title") or row["id"],
                        description=row.get("description") or "",
                        status="archived" if row.get("status") == "archived" else "active",
                        created_at=row.get("created_at") or now,
                        updated_at=row.get("created_at") or now,
                    )
                )
                report.projects += 1

            # Preserve orphan tasks by creating a deterministic fallback Project.
            known_projects = set(group_map.values())
            for project_id in set(task_group.values()) - known_projects:
                conn.execute(
                    projects.insert().values(
                        id=project_id,
                        title="Migrated legacy project",
                        description="Created for orphan legacy tasks",
                        status="archived",
                        created_at=now,
                        updated_at=now,
                    )
                )
                report.projects += 1

            for row in legacy_tasks:
                origin_text = _fact_description(facts_by_task[row["id"]], "origin")
                goal = _fact_description(facts_by_task[row["id"]], "goal") or "Legacy task goal"
                try:
                    origin = _parse_legacy_origin(origin_text)
                    scope_enabled = bool(row.get("task_scope_enabled", 0))
                except ValueError:
                    origin = None
                    scope_enabled = False
                    report.warnings.append(f"Invalid legacy origin retained as text: {row['id']}")
                old_status = row.get("status") or "stopped"
                status = "completed" if old_status == "completed" else "stopped"
                scope_json = row.get("task_scope_json") or '{"targets":[],"exclude":[],"notes":""}'
                conn.execute(
                    tasks.insert().values(
                        id=task_map[row["id"]],
                        project_id=task_group[row["id"]],
                        title=row.get("title") or row["id"],
                        origin_host=origin.host if origin else "legacy.invalid",
                        origin_scheme=origin.scheme if origin else None,
                        origin_port=origin.port if origin else None,
                        origin_input=origin_text or "legacy origin unavailable",
                        goal=goal,
                        status=status,
                        phase="none",
                        bootstrap_enabled=bool(row.get("bootstrap_enabled", 1)),
                        guard_enabled=bool(row.get("guard_enabled", 0)),
                        external_report_enabled=bool(row.get("kk_report_enabled", 0)),
                        scope_enabled=scope_enabled,
                        scope_json=scope_json,
                        worker_pool_json=row.get("worker_pool") or "[]",
                        legacy_readonly=True,
                        created_at=row.get("created_at") or now,
                        updated_at=row.get("created_at") or now,
                        completed_at=row.get("created_at") if status == "completed" else None,
                    )
                )
                report.tasks += 1

            for old_task_id, rows in facts_by_task.items():
                if old_task_id not in task_map:
                    continue
                created_at = _task_created(legacy_tasks, old_task_id, now)
                for row in rows:
                    conn.execute(
                        facts.insert().values(
                            id=row["id"],
                            task_id=task_map[old_task_id],
                            description=row.get("description") or "",
                            archived=bool(row.get("archived", 0)),
                            created_at=created_at,
                        )
                    )
                    report.facts += 1

            legacy_intents = _rows(source, "intents")
            migrated_intents: set[tuple[str, str]] = set()
            for row in legacy_intents:
                if row["project_id"] not in task_map:
                    continue
                task_fact_ids = {fact["id"] for fact in facts_by_task[row["project_id"]]}
                to_fact_id = row.get("to_fact_id")
                if to_fact_id not in task_fact_ids:
                    if to_fact_id:
                        report.warnings.append(
                            f"Intent {row['id']} references missing Fact {to_fact_id}; reference cleared"
                        )
                    to_fact_id = None
                completed = bool(row.get("concluded_at"))
                conn.execute(
                    intents.insert().values(
                        id=row["id"],
                        task_id=task_map[row["project_id"]],
                        to_fact_id=to_fact_id,
                        description=row.get("description") or "",
                        status="completed" if completed else "cancelled",
                        creator=row.get("creator") or "legacy",
                        worker=row.get("worker"),
                        worker_pool_json=row.get("worker_pool") or "[]",
                        targets_json="[]",
                        execute_timeout_seconds=row.get("execute_timeout_seconds"),
                        conclude_timeout_seconds=row.get("conclude_timeout_seconds"),
                        last_heartbeat_at=row.get("last_heartbeat_at"),
                        created_at=row.get("created_at") or now,
                        concluded_at=row.get("concluded_at"),
                        archived=bool(row.get("archived", 0)),
                    )
                )
                migrated_intents.add((row["project_id"], row["id"]))
                report.intents += 1

            for row in _rows(source, "intent_sources"):
                source_key = (row["project_id"], row["intent_id"])
                task_fact_ids = {fact["id"] for fact in facts_by_task.get(row["project_id"], [])}
                if source_key in migrated_intents and row["fact_id"] in task_fact_ids:
                    conn.execute(
                        intent_sources.insert().values(
                            task_id=task_map[row["project_id"]],
                            intent_id=row["intent_id"],
                            fact_id=row["fact_id"],
                        )
                    )
                else:
                    report.warnings.append(
                        f"Orphan intent source skipped: {row.get('project_id')}/{row.get('intent_id')}/{row.get('fact_id')}"
                    )

            for row in _rows(source, "hints"):
                if row["project_id"] not in task_map:
                    continue
                conn.execute(
                    task_hints.insert().values(
                        id=row["id"],
                        task_id=task_map[row["project_id"]],
                        content=row.get("content") or "",
                        creator=row.get("creator") or "legacy",
                        created_at=row.get("created_at") or now,
                    )
                )
                report.hints += 1

            knowledge_counts: defaultdict[str, int] = defaultdict(int)
            for row in _rows(source, "blackboard_items"):
                old_group = row.get("group_id")
                if not old_group and row.get("project_id"):
                    old_group = next(
                        (task.get("parent_project_id") for task in legacy_tasks if task["id"] == row["project_id"]),
                        None,
                    )
                project_id = group_map.get(old_group or "")
                if not project_id:
                    continue
                knowledge_counts[project_id] += 1
                kid = f"k{knowledge_counts[project_id]:03d}"
                conn.execute(
                    project_knowledge.insert().values(
                        id=kid,
                        project_id=project_id,
                        title=row.get("title") or "",
                        content=row.get("content") or "",
                        hint=row.get("hint") or "",
                        created_by=row.get("creator") or "legacy",
                        updated_by=row.get("updated_by") or row.get("creator") or "legacy",
                        created_at=row.get("created_at") or now,
                        updated_at=row.get("updated_at") or row.get("created_at") or now,
                    )
                )
                report.knowledge += 1

            for row in _rows(source, "vulns"):
                if row["project_id"] not in task_map:
                    continue
                conn.execute(
                    vulnerabilities.insert().values(
                        id=row["id"],
                        task_id=task_map[row["project_id"]],
                        title=row.get("title") or "Legacy vulnerability",
                        type=row.get("type") or "其他",
                        severity=_severity(row.get("severity")),
                        description=row.get("description") or "",
                        prerequisites=row.get("prerequisites") or "",
                        impact=row.get("impact") or "Legacy impact unavailable",
                        reproduction_steps_json=row.get("reproduction_steps_json") or "[]",
                        remediation=row.get("recommendation") or "",
                        curl_command="",
                        evidence_json=row.get("evidence_json") or "[]",
                        affected_target=row.get("affected_scope") or "",
                        status=_vuln_status(row.get("status")),
                        source=row.get("source") or "legacy",
                        reported_by=row.get("reported_by") or "legacy",
                        created_at=row.get("created_at") or now,
                        updated_at=row.get("created_at") or now,
                    )
                )
                report.vulnerabilities += 1

            for row in _rows(source, "credentials"):
                project_id = group_map.get(row.get("group_id") or "")
                if not project_id:
                    continue
                conn.execute(
                    credentials.insert().values(
                        id=str(row.get("id") or f"legacy-{report.credentials + 1}"),
                        project_id=project_id,
                        label=row.get("label") or "legacy credential",
                        auth_type=row.get("auth_type") or "unknown",
                        credentials_json=row.get("credentials_json") or "{}",
                        domains_json=row.get("domains_json") or "[]",
                        notes="Migrated plaintext credential",
                        active=bool(row.get("active", 1)),
                        created_at=row.get("captured_at") or now,
                        updated_at=row.get("captured_at") or now,
                    )
                )
                report.credentials += 1

            artifact_counts = _archive_assets(
                conn,
                source,
                data_dir=data_dir,
                task_map=task_map,
                task_group=task_group,
                report=report,
                now=now,
            )
            _migrate_runtime_configuration(
                conn,
                source,
                report=report,
                now=now,
                mozzie_token=mozzie_token,
            )
            _initialize_counters(
                conn,
                project_map=all_project_map,
                task_map=task_map,
                facts_by_task=facts_by_task,
                legacy_intents=legacy_intents,
                legacy_hints=_rows(source, "hints"),
                legacy_vulnerabilities=_rows(source, "vulns"),
                legacy_credentials=_rows(source, "credentials"),
                knowledge_counts=knowledge_counts,
                artifact_counts=artifact_counts,
            )
            _stamp_schema(conn)
    finally:
        source.close()
    return report


def _rows(conn: sqlite3.Connection, table: str) -> list[dict]:
    exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    if not exists:
        return []
    return [dict(row) for row in conn.execute(f'SELECT * FROM "{table}"')]


def _legacy_task_rows(source: sqlite3.Connection, *, report: MigrationReport, now: str) -> list[dict]:
    rows = _rows(source, "projects")
    known = {row["id"] for row in rows}
    references: set[str] = set()
    inferred_groups: dict[str, str] = {}
    for table in ("facts", "intents", "hints", "vulns", "assets"):
        for row in _rows(source, table):
            task_id = row.get("project_id")
            if not task_id:
                continue
            references.add(task_id)
            if row.get("group_id"):
                inferred_groups.setdefault(task_id, row["group_id"])
    for task_id in sorted(references - known):
        rows.append(
            {
                "id": task_id,
                "parent_project_id": inferred_groups.get(task_id) or "legacy-orphans",
                "title": f"Recovered legacy task {task_id}",
                "status": "stopped",
                "bootstrap_enabled": 0,
                "created_at": now,
                "kk_report_enabled": 0,
                "worker_pool": "[]",
                "guard_enabled": 0,
                "task_scope_enabled": 0,
                "task_scope_json": '{"targets":[],"exclude":[],"notes":""}',
                "_recovered": True,
            }
        )
        report.warnings.append(f"Recovered deleted legacy Task shell: {task_id}")
    return rows


def _project_id(value: str) -> str:
    match = re.search(r"(\d+)$", value or "")
    return f"prj_{int(match.group(1)):03d}" if match else f"prj_{hashlib.sha1(value.encode()).hexdigest()[:8]}"


def _task_id(value: str) -> str:
    match = re.search(r"(\d+)$", value or "")
    return f"t_{int(match.group(1)):03d}" if match else f"t_{hashlib.sha1(value.encode()).hexdigest()[:8]}"


def _parse_legacy_origin(value: str):
    try:
        origin = parse_origin(value)
        if _plausible_origin(origin.host):
            return origin
    except ValueError:
        pass
    for token in re.split(r"[\s，,；;]+", value or ""):
        candidate = token.strip("。.!！:：()（）[]【】<>《》")
        if not candidate:
            continue
        try:
            origin = parse_origin(candidate)
            if _plausible_origin(origin.host):
                return origin
        except ValueError:
            continue
    raise ValueError("No valid legacy origin found")


def _plausible_origin(host: str) -> bool:
    if host == "localhost" or "." in host:
        return True
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return False


def _build_id_map(values, mapper) -> dict[str, str]:
    """Preserve readable numeric IDs while deterministically resolving collisions."""

    result: dict[str, str] = {}
    used: set[str] = set()
    for value in sorted(set(values)):
        candidate = mapper(value)
        if candidate in used:
            base = candidate
            digest = hashlib.sha1(value.encode()).hexdigest()
            length = 6
            candidate = f"{base}_{digest[:length]}"
            while candidate in used:
                length += 1
                candidate = f"{base}_{digest[:length]}"
        result[value] = candidate
        used.add(candidate)
    return result


def _build_task_id_map(rows: list[dict]) -> dict[str, str]:
    actual = [row["id"] for row in rows if not row.get("_recovered")]
    result = _build_id_map(actual, _task_id)
    used = set(result.values())
    for row in sorted((item for item in rows if item.get("_recovered")), key=lambda item: item["id"]):
        digest = hashlib.sha1(row["id"].encode()).hexdigest()[:8]
        candidate = f"t_legacy_{digest}"
        if candidate in used:
            candidate = f"{candidate}_{len(used)}"
        result[row["id"]] = candidate
        used.add(candidate)
    return result


def _fact_description(rows: list[dict], fact_id: str) -> str:
    return next((row.get("description") or "" for row in rows if row["id"] == fact_id), "")


def _task_created(tasks_rows: list[dict], task_id: str, default: str) -> str:
    return next((row.get("created_at") or default for row in tasks_rows if row["id"] == task_id), default)


def _severity(value: str | None) -> str:
    normalized = (value or "").strip().lower()
    return {
        "严重": "critical",
        "critical": "critical",
        "高危": "high",
        "high": "high",
        "中危": "medium",
        "medium": "medium",
        "低危": "low",
        "low": "low",
        "info": "info",
    }.get(normalized, "medium")


def _vuln_status(value: str | None) -> str:
    return value if value in {"待验证", "已验证", "忽略", "重复"} else "待验证"


def _archive_assets(conn, source, *, data_dir, task_map, task_group, report, now):
    grouped: defaultdict[str, list[dict]] = defaultdict(list)
    artifact_counts: defaultdict[str, int] = defaultdict(int)
    for row in _rows(source, "assets"):
        old_task = row.get("project_id")
        if old_task in task_map:
            grouped[old_task].append(row)
            report.assets += 1
        else:
            report.warnings.append(f"Legacy asset skipped without Task identity: {row.get('id')}")
    for old_task, rows in grouped.items():
        task_id = task_map[old_task]
        project_id = task_group[old_task]
        if not project_id:
            report.warnings.append(f"Legacy assets have no Project container: {old_task}")
            continue
        artifact_counts[task_id] += 1
        artifact_id = f"a{artifact_counts[task_id]:03d}"
        directory = data_dir / "projects" / project_id / task_id / "legacy"
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / "legacy_asset_inventory.json"
        content = json.dumps(rows, ensure_ascii=False, indent=2)
        path.write_text(content, encoding="utf-8")
        conn.execute(
            artifacts.insert().values(
                id=artifact_id,
                task_id=task_id,
                artifact_type="legacy_asset_inventory",
                path=str(path),
                summary=f"Archived {len(rows)} legacy asset rows",
                content_hash=hashlib.sha256(content.encode()).hexdigest(),
                size=path.stat().st_size,
                created_by="migration.v2",
                created_at=now,
            )
        )
    return artifact_counts


def _initialize_counters(
    conn,
    *,
    project_map,
    task_map,
    facts_by_task,
    legacy_intents,
    legacy_hints,
    legacy_vulnerabilities,
    legacy_credentials,
    knowledge_counts,
    artifact_counts,
):
    conn.execute(
        id_counters.insert(),
        [
            {"scope": "global", "kind": "project", "next_value": _next_prefixed_numeric(project_map.values(), "prj_")},
            {"scope": "global", "kind": "task", "next_value": _next_prefixed_numeric(task_map.values(), "t_")},
            {
                "scope": "global",
                "kind": "credential",
                "next_value": _next_prefixed_numeric(
                    (row.get("id") for row in legacy_credentials), "cred_"
                ),
            },
            {"scope": "global", "kind": "reason_workspace", "next_value": 1},
        ],
    )
    intents_by_task: defaultdict[str, list[str]] = defaultdict(list)
    for row in legacy_intents:
        if row["project_id"] in task_map:
            intents_by_task[row["project_id"]].append(row["id"])
    hints_by_task: defaultdict[str, list[str]] = defaultdict(list)
    for row in legacy_hints:
        if row.get("project_id") in task_map:
            hints_by_task[row["project_id"]].append(row["id"])
    vulnerabilities_by_task: defaultdict[str, list[str]] = defaultdict(list)
    for row in legacy_vulnerabilities:
        if row.get("project_id") in task_map:
            vulnerabilities_by_task[row["project_id"]].append(row["id"])
    for project_id in project_map.values():
        conn.execute(
            id_counters.insert().values(
                scope=project_id,
                kind="knowledge",
                next_value=knowledge_counts.get(project_id, 0) + 1,
            )
        )
    for old_task, new_task in task_map.items():
        conn.execute(
            id_counters.insert(),
            [
                {"scope": new_task, "kind": "fact", "next_value": _next_prefixed_numeric((row["id"] for row in facts_by_task.get(old_task, [])), "f")},
                {"scope": new_task, "kind": "intent", "next_value": _next_prefixed_numeric(intents_by_task.get(old_task, []), "i")},
                {"scope": new_task, "kind": "hint", "next_value": _next_prefixed_numeric(hints_by_task.get(old_task, []), "h")},
                {"scope": new_task, "kind": "todo", "next_value": 1},
                {"scope": new_task, "kind": "artifact", "next_value": artifact_counts.get(new_task, 0) + 1},
                {"scope": new_task, "kind": "vulnerability", "next_value": _next_prefixed_numeric(vulnerabilities_by_task.get(old_task, []), "v")},
            ],
        )


def _next_numeric(values) -> int:
    numbers = []
    for value in values:
        match = re.search(r"(\d+)$", str(value))
        if match:
            numbers.append(int(match.group(1)))
    return max(numbers, default=0) + 1


def _next_prefixed_numeric(values, prefix: str) -> int:
    pattern = re.compile(rf"^{re.escape(prefix)}(\d+)$")
    numbers = []
    for value in values:
        match = pattern.match(str(value))
        if match:
            numbers.append(int(match.group(1)))
    return max(numbers, default=0) + 1


def _migrate_runtime_configuration(conn, source, *, report, now, mozzie_token: str) -> None:
    settings_rows = _rows(source, "settings")
    settings = settings_rows[0] if settings_rows else {}
    judge_rows = _rows(source, "judge_settings")
    judge = judge_rows[0] if judge_rows else {}
    defaults = RuntimeConfig()

    def positive(name: str, default: int) -> int:
        try:
            value = int(settings.get(name) or default)
        except (TypeError, ValueError):
            value = default
        return max(1, value)

    max_workers = positive("dispatch_runtime_max_workers", defaults.max_workers)
    max_task_workers = min(
        max_workers,
        positive("dispatch_runtime_max_project_workers", defaults.max_task_workers),
    )
    guard_key = _legacy_provider_key(source, "judge", report)
    config = RuntimeConfig(
        interval_seconds=positive("dispatch_runtime_interval", defaults.interval_seconds),
        max_workers=max_workers,
        max_running_tasks=positive(
            "dispatch_runtime_max_running_projects", defaults.max_running_tasks
        ),
        max_task_workers=max_task_workers,
        bootstrap_limit=positive("bootstrap_worker_count", defaults.bootstrap_limit),
        reason_limit=positive("reason_worker_count", defaults.reason_limit),
        explore_limit=positive("explore_worker_count", defaults.explore_limit),
        bootstrap_execute_seconds=positive(
            "dispatch_bootstrap_timeout", defaults.bootstrap_execute_seconds
        ),
        bootstrap_conclude_seconds=positive(
            "dispatch_bootstrap_conclude_timeout", defaults.bootstrap_conclude_seconds
        ),
        reason_execute_seconds=positive("reason_timeout", defaults.reason_execute_seconds),
        explore_execute_seconds=positive("intent_timeout", defaults.explore_execute_seconds),
        explore_conclude_seconds=positive(
            "dispatch_explore_conclude_timeout", defaults.explore_conclude_seconds
        ),
        mozzie_token=mozzie_token.strip(),
        guard_mode=str(judge.get("mode") or defaults.guard_mode),
        guard_model_base_url=str(judge.get("base_url") or defaults.guard_model_base_url),
        guard_model_name=str(judge.get("model") or defaults.guard_model_name),
        guard_api_key=guard_key,
        guard_timeout_seconds=max(
            1, int(judge.get("timeout_seconds") or defaults.guard_timeout_seconds)
        ),
        guard_whitelist_tools=_json_list(
            judge.get("whitelist_tools_json"), defaults.guard_whitelist_tools
        ),
        guard_whitelist_bash_verbs=_json_list(
            judge.get("whitelist_bash_verbs_json"), defaults.guard_whitelist_bash_verbs
        ),
        guard_hard_block_rules=_json_value(
            judge.get("hard_block_rules_json"), defaults.guard_hard_block_rules
        ),
        crawler_enabled=False,
    )
    conn.execute(
        runtime_settings.insert().values(
            id="dispatcher",
            config_json=config.model_dump_json(),
            version=max(1, int(settings.get("dispatch_config_version") or 1)),
            updated_by="migration.v2",
            updated_at=settings.get("dispatch_config_updated_at") or now,
        )
    )
    report.runtime_settings = 1

    pi_tools = [
        row.get("name")
        for row in _rows(source, "pi_tools")
        if row.get("enabled") and row.get("name")
    ]
    for row in _rows(source, "dispatch_workers"):
        capabilities = [
            item
            for item in _json_list(row.get("task_types_json"), [])
            if item in {"bootstrap", "reason", "explore"}
        ]
        if not capabilities:
            continue
        env = _json_value(row.get("env_json"), {})
        conn.execute(
            dispatch_workers.insert().values(
                name=row["name"],
                worker_type=row.get("worker_type") or "pi",
                capabilities_json=json.dumps(capabilities),
                max_running=max(1, int(row.get("max_running") or 1)),
                priority=max(0, int(row.get("priority") or 0)),
                config_json=json.dumps(
                    {"env": env, "pi_tools": pi_tools}, ensure_ascii=False
                ),
                disabled=bool(row.get("disabled", 0)),
                updated_at=row.get("updated_at") or now,
            )
        )
        report.workers += 1


def _legacy_provider_key(source, provider: str, report: MigrationReport) -> str:
    row = next(
        (
            item
            for item in _rows(source, "provider_keys")
            if item.get("provider") == provider and item.get("is_active")
        ),
        None,
    )
    if not row or not row.get("key_ciphertext"):
        return ""
    try:
        return decrypt_secret(row["key_ciphertext"])
    except KeyringError as exc:
        report.warnings.append(f"Could not decrypt legacy provider key {provider}: {exc}")
        return ""


def _json_value(raw, default):
    if raw is None or raw == "":
        return default
    try:
        return json.loads(raw) if isinstance(raw, str) else raw
    except (TypeError, json.JSONDecodeError):
        return default


def _json_list(raw, default: list) -> list:
    value = _json_value(raw, default)
    return value if isinstance(value, list) else list(default)


def _stamp_schema(conn) -> None:
    conn.exec_driver_sql("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL PRIMARY KEY)")
    conn.exec_driver_sql(
        "INSERT INTO alembic_version (version_num) VALUES (?)",
        (V2_SCHEMA_REVISION,),
    )
