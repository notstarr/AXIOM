#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def fetch(base: str, path: str) -> Any:
    request = urllib.request.Request(base.rstrip("/") + path, headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        payload = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"GET {path} returned {exc.code}: {payload[:500]}") from exc


def fetch_result(base: str, path: str) -> Any:
    """Capture deterministic read errors such as archived-task 409 responses."""

    try:
        return fetch(base, path)
    except RuntimeError as exc:
        return {"__error__": str(exc)}


def paged(base: str, path: str) -> Any:
    separator = "&" if "?" in path else "?"
    first = fetch(base, f"{path}{separator}limit=200&offset=0")
    if not isinstance(first, dict) or "items" not in first:
        return first
    items = list(first.get("items") or [])
    total = int(first.get("total") or len(items))
    while len(items) < total:
        page = fetch(base, f"{path}{separator}limit=200&offset={len(items)}")
        batch = list(page.get("items") or [])
        if not batch:
            break
        items.extend(batch)
    result = dict(first)
    result["items"] = items
    result["total"] = total
    result["limit"] = len(items)
    result["offset"] = 0
    return result


def paged_result(base: str, path: str) -> Any:
    try:
        return paged(base, path)
    except RuntimeError as exc:
        return {"__error__": str(exc)}


ISO_TIMESTAMP = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$")
SKILL_PATH = re.compile(r"(?:^|/)(?:dispatcher/)?skills/([^/]+)/SKILL\.md$")


def normalize_string(value: str) -> str:
    if match := SKILL_PATH.search(value):
        return f"<AXIOM_SKILLS>/{match.group(1)}/SKILL.md"
    if ISO_TIMESTAMP.fullmatch(value):
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    return value


def normalize(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: normalize(value[key]) for key in sorted(value)}
    if isinstance(value, list):
        return [normalize(item) for item in value]
    if isinstance(value, str):
        return normalize_string(value)
    return value


def main() -> None:
    parser = argparse.ArgumentParser(description="Export a normalized read-only AXIOM API snapshot")
    parser.add_argument("--api-url", required=True)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()

    snapshot: dict[str, Any] = {
        "projects": fetch(args.api_url, "/api/v1/projects"),
        "tasks": fetch(args.api_url, "/api/v1/tasks"),
        "dashboard": fetch(args.api_url, "/api/v1/dashboard/summary"),
        "vulnerabilities": paged(args.api_url, "/api/v1/vulnerabilities?include_archived=true"),
        "workers": fetch(args.api_url, "/api/v1/workers"),
        "worker_overview": fetch(args.api_url, "/api/v1/workers/overview"),
        "phase_capabilities": fetch(args.api_url, "/api/v1/phase-capabilities"),
        "axiom_tools": fetch(args.api_url, "/api/v1/axiom-tools"),
        "mcp_servers": fetch(args.api_url, "/api/v1/mcp/servers"),
        "skills": fetch(args.api_url, "/api/v1/skills"),
    }
    task_details: dict[str, Any] = {}
    tasks = snapshot["tasks"] if isinstance(snapshot["tasks"], list) else []
    for task in tasks:
        task_id = str(task["id"])
        quoted = urllib.parse.quote(task_id, safe="")
        task_details[task_id] = {
            "task": fetch_result(args.api_url, f"/api/v1/tasks/{quoted}"),
            "graph": fetch_result(args.api_url, f"/api/v1/tasks/{quoted}/graph"),
            "goal_cycles": fetch_result(args.api_url, f"/api/v1/tasks/{quoted}/goal-cycles"),
            "goal_evaluations": fetch_result(
                args.api_url, f"/api/v1/tasks/{quoted}/goal-evaluations"
            ),
            "todos": fetch_result(args.api_url, f"/api/v1/tasks/{quoted}/todos"),
            "vulnerabilities": fetch_result(
                args.api_url, f"/api/v1/tasks/{quoted}/vulnerabilities"
            ),
            "artifacts": paged_result(args.api_url, f"/api/v1/tasks/{quoted}/artifacts"),
            "archives_active": fetch_result(
                args.api_url, f"/api/v1/tasks/{quoted}/archives?status=active"
            ),
            "archives_restored": fetch_result(
                args.api_url,
                f"/api/v1/tasks/{quoted}/archives?status=restored",
            ),
        }
    snapshot["task_details"] = task_details
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(normalize(snapshot), ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
