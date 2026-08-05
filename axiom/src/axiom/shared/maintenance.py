from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Literal

from axiom.shared.clock import utcnow


MaintenanceState = Literal["normal", "drain", "frozen"]
MAINTENANCE_FILENAME = "maintenance.json"


@dataclass(frozen=True, slots=True)
class MaintenanceStatus:
    state: MaintenanceState = "normal"
    actor: str = "system"
    reason: str = ""
    updated_at: str = ""


def maintenance_path(data_dir: str | Path) -> Path:
    return Path(data_dir).expanduser().resolve() / MAINTENANCE_FILENAME


def read_maintenance(data_dir: str | Path) -> MaintenanceStatus:
    path = maintenance_path(data_dir)
    if not path.exists():
        return MaintenanceStatus()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        state = str(payload.get("state") or "normal")
        if state not in {"normal", "drain", "frozen"}:
            raise ValueError("invalid maintenance state")
        return MaintenanceStatus(
            state=state,  # type: ignore[arg-type]
            actor=str(payload.get("actor") or "system"),
            reason=str(payload.get("reason") or ""),
            updated_at=str(payload.get("updated_at") or ""),
        )
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return MaintenanceStatus(
            state="frozen",
            actor="system.fail-closed",
            reason="maintenance state file is invalid",
            updated_at="",
        )


def write_maintenance(
    data_dir: str | Path,
    *,
    state: MaintenanceState,
    actor: str,
    reason: str = "",
) -> MaintenanceStatus:
    if state not in {"normal", "drain", "frozen"}:
        raise ValueError(f"invalid maintenance state: {state}")
    root = Path(data_dir).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    status = MaintenanceStatus(
        state=state,
        actor=actor.strip() or "system",
        reason=reason.strip(),
        updated_at=utcnow(),
    )
    descriptor, temporary_name = tempfile.mkstemp(prefix="maintenance-", dir=root)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(asdict(status), handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, maintenance_path(root))
    finally:
        temporary.unlink(missing_ok=True)
    return status
