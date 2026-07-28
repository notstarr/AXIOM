from __future__ import annotations

import fcntl
import json
import os
import re
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from axiom.modules.artifacts.schemas import Artifact
from axiom.modules.vulnerabilities.schemas import StoredExploreVulnerability, Vulnerability
from axiom.shared.errors import ConflictError


FINISH_MARKER = "FINISH"
_FINISH_FENCE = re.compile(
    r"(?:^|\n)```(?:[A-Za-z0-9_.+-]+)?[ \t]*\n"
    r"[ \t]*FINISH[ \t]*\n```[ \t]*$"
)
BOOTSTRAP_FILES = (
    "asset_inventory.md",
    "initial_leads.md",
    "findings.md",
)
_SAFE_SEGMENT = re.compile(r"^[A-Za-z0-9_.-]+$")
EXPLORE_FINDINGS_FILE = "findings.md"
EXPLORE_LEADS_FILE = "leads.md"


class StageOutputError(ConflictError):
    """A worker declared completion before its durable outputs were ready."""


@dataclass(frozen=True, slots=True)
class StageFile:
    relative_name: str
    path: Path
    artifact_type: str
    summary: str


class ExploreState(BaseModel):
    vulnerabilities: list[StoredExploreVulnerability] = Field(default_factory=list, max_length=20)
    artifact_ids: list[str] = Field(default_factory=list)
    vulnerability_ids: list[str] = Field(default_factory=list)
    completed: bool = False


def require_finish(value: str) -> None:
    if value.strip() != FINISH_MARKER:
        raise StageOutputError("最终响应必须仅包含 FINISH")


def require_explore_finish(value: str) -> str | None:
    """Accept harmless Explore marker formatting while keeping the marker terminal."""
    text = value.strip()
    if text == FINISH_MARKER:
        return None
    if text == f"`{FINISH_MARKER}`":
        return "inline_code"

    fenced = _FINISH_FENCE.search(text)
    if fenced is not None:
        return "code_fence" if fenced.start() == 0 else "trailing_code_fence"

    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if lines and lines[-1] == FINISH_MARKER:
        return "trailing_line"
    if lines and lines[-1] == f"`{FINISH_MARKER}`":
        return "trailing_inline_code"
    raise StageOutputError("最终响应必须以独立的 FINISH 结束")


def bootstrap_files(run_dir: str | Path) -> list[StageFile]:
    root = Path(run_dir).resolve()
    results: list[StageFile] = []
    labels = {
        "asset_inventory.md": ("bootstrap.asset_inventory", "Bootstrap 资产清单"),
        "initial_leads.md": ("bootstrap.initial_leads", "Bootstrap 初始线索"),
        "findings.md": ("bootstrap.findings", "Bootstrap 已确认问题"),
    }
    errors: list[str] = []
    for filename in BOOTSTRAP_FILES:
        path = root / filename
        try:
            _require_regular_output(path, root)
            _require_non_empty(path)
        except StageOutputError as exc:
            errors.append(str(exc))
            continue
        artifact_type, summary = labels[filename]
        results.append(
            StageFile(
                relative_name=filename,
                path=path,
                artifact_type=artifact_type,
                summary=summary,
            )
        )
    if errors:
        raise StageOutputError("Bootstrap 产物未就绪：" + "；".join(errors))
    return results


def explore_files(run_dir: str | Path) -> list[StageFile]:
    run_root = Path(run_dir).resolve()
    root = run_root / "artifacts"
    if not root.is_dir() or root.is_symlink():
        raise StageOutputError("Explore 必须在 artifacts/ 中写入 findings.md 和 leads.md")

    results: list[StageFile] = []
    required = {
        EXPLORE_FINDINGS_FILE: ("explore.findings", "Explore 已确认事实"),
        EXPLORE_LEADS_FILE: ("explore.leads", "Explore 待验证线索"),
    }
    errors: list[str] = []
    for filename, (artifact_type, summary) in required.items():
        path = root / filename
        try:
            _require_regular_output(path, root)
            _require_non_empty(path)
        except StageOutputError as exc:
            errors.append(str(exc))
            continue
        results.append(
            StageFile(
                relative_name=filename,
                path=path,
                artifact_type=artifact_type,
                summary=summary,
            )
        )
    if errors:
        raise StageOutputError("Explore 核心产物未就绪：" + "；".join(errors))

    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root)
        for segment in relative.parts:
            if not _SAFE_SEGMENT.fullmatch(segment) or segment.startswith("."):
                raise StageOutputError(f"Explore 产物路径不安全：{relative.as_posix()}")
        if path.is_symlink():
            raise StageOutputError(f"阶段产物不能是符号链接：{relative.as_posix()}")
        if relative.as_posix() in required:
            continue
        if relative.parts[0] != "evidence":
            raise StageOutputError(
                f"Explore 只允许 findings.md、leads.md 和 evidence/**：{relative.as_posix()}"
            )
        if path.is_dir():
            continue
        _require_regular_output(path, root)
        _require_non_empty(path)
        relative_name = relative.as_posix()
        results.append(
            StageFile(
                relative_name=relative_name,
                path=path,
                artifact_type="explore.evidence",
                summary=f"Explore 证据：{relative_name}",
            )
        )
    if len(results) > 50:
        raise StageOutputError("Explore 单次最多注册 50 个 Artifact")
    return results


def initialize_explore_state(run_dir: str | Path) -> Path:
    path = explore_state_path(run_dir)
    with explore_state_guard(run_dir):
        _reject_state_symlink(path)
        if not path.exists():
            _write_explore_state(path, ExploreState())
    return path


def load_explore_state(run_dir: str | Path) -> ExploreState:
    path = explore_state_path(run_dir)
    _reject_state_symlink(path)
    if not path.is_file():
        return ExploreState()
    try:
        return ExploreState.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise StageOutputError("Explore 内部漏洞状态文件无效") from exc


def append_explore_vulnerability(
    run_dir: str | Path,
    report: StoredExploreVulnerability,
) -> tuple[ExploreState, bool]:
    with explore_state_guard(run_dir):
        return append_explore_vulnerability_locked(run_dir, report)


def append_explore_vulnerability_locked(
    run_dir: str | Path,
    report: StoredExploreVulnerability,
) -> tuple[ExploreState, bool]:
    """Append while the caller holds ``explore_state_guard``."""
    path = explore_state_path(run_dir)
    state = load_explore_state(run_dir)
    if state.completed:
        raise StageOutputError("Explore 已完成，不能继续上报漏洞")
    fingerprint = _vulnerability_fingerprint(report)
    reports = list(state.vulnerabilities)
    if any(_vulnerability_fingerprint(item) == fingerprint for item in reports):
        return state, True
    if len(reports) >= 20:
        raise StageOutputError("Explore 单次最多上报 20 个漏洞")
    reports.append(report)
    updated = state.model_copy(update={"vulnerabilities": reports})
    _write_explore_state(path, updated)
    return updated, False


def write_bootstrap_index(run_dir: str | Path, artifacts: list[Artifact]) -> Path:
    by_type = {item.artifact_type: item for item in artifacts}
    payload = {
        "completed": True,
        "artifacts": {
            filename: {
                "id": by_type[artifact_type].id,
                "type": artifact_type,
                "storage_key": by_type[artifact_type].storage_key,
                "content_hash": by_type[artifact_type].content_hash,
                "size": by_type[artifact_type].size,
            }
            for filename, (artifact_type, _summary) in {
                "asset_inventory.md": ("bootstrap.asset_inventory", ""),
                "initial_leads.md": ("bootstrap.initial_leads", ""),
                "findings.md": ("bootstrap.findings", ""),
            }.items()
        },
    }
    path = Path(run_dir).resolve() / "bootstrap_artifacts.json"
    _atomic_json(path, payload)
    return path


def finalize_explore_state(
    run_dir: str | Path,
    *,
    artifacts: list[Artifact],
    vulnerabilities: list[Vulnerability],
) -> Path:
    path = explore_state_path(run_dir)
    state = load_explore_state(run_dir)
    completed = state.model_copy(
        update={
            "artifact_ids": [item.id for item in artifacts],
            "vulnerability_ids": [item.id for item in vulnerabilities],
            "completed": True,
        }
    )
    _write_explore_state(path, completed)
    return path


def explore_state_path(run_dir: str | Path) -> Path:
    return Path(run_dir).resolve() / "explore_result.json"


@contextmanager
def explore_state_guard(run_dir: str | Path) -> Iterator[None]:
    path = explore_state_path(run_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_suffix(path.suffix + ".lock")
    if lock_path.is_symlink():
        raise StageOutputError("Explore 内部状态锁不能是符号链接")
    with lock_path.open("a+b") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        yield


def _write_explore_state(path: Path, state: ExploreState) -> None:
    _reject_state_symlink(path)
    _atomic_json(path, state.model_dump(mode="json"))


def _atomic_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def _require_regular_output(path: Path, root: Path) -> None:
    if path.is_symlink():
        raise StageOutputError(f"阶段产物不能是符号链接：{path.name}")
    try:
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise StageOutputError(f"阶段产物不存在：{path.name}") from exc
    root = root.resolve()
    if resolved != root and root not in resolved.parents:
        raise StageOutputError(f"阶段产物越过运行目录：{path.name}")
    if not resolved.is_file():
        raise StageOutputError(f"阶段产物不是普通文件：{path.name}")


def _require_non_empty(path: Path) -> None:
    try:
        if path.stat().st_size <= 0:
            raise StageOutputError(f"阶段产物为空：{path.name}")
    except OSError as exc:
        raise StageOutputError(f"无法读取阶段产物：{path.name}") from exc


def _vulnerability_fingerprint(
    report: StoredExploreVulnerability,
) -> tuple[str, str, str, str]:
    return (
        report.title.casefold(),
        report.type.casefold(),
        report.severity,
        report.affected_target.casefold(),
    )


def _reject_state_symlink(path: Path) -> None:
    if path.is_symlink():
        raise StageOutputError("Explore 内部状态文件不能是符号链接")
