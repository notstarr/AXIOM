from __future__ import annotations

import fcntl
import json
import logging
import os
import re
from collections import defaultdict
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from axiom.modules.artifacts.repository import BOOTSTRAP_ARTIFACT_TYPES
from axiom.modules.artifacts.schemas import Artifact
from axiom.modules.vulnerabilities.schemas import StoredExploreVulnerability, Vulnerability
from axiom.shared.errors import ConflictError


logger = logging.getLogger(__name__)


FINISH_MARKER = "FINISH"
_FINISH_FENCE = re.compile(
    r"(?:^|\n)```(?:[A-Za-z0-9_.+-]+)?[ \t]*\n"
    r"[ \t]*FINISH[ \t]*\n```[ \t]*$"
)
BOOTSTRAP_FILES = (
    "asset_inventory.md",
    "initial_leads.md",
)
# jsfinder 每轮/每目标产出独立文件（jsfinder_result-<host>-<path>-<rand4>.json），
# 所以这里按规则发现全部匹配文件，而不是只认一个固定文件名。
JSFINDER_GLOB = "jsfinder_result*.json"
JSFINDER_PREFIX = "jsfinder_result-"
JSFINDER_SUFFIX = ".json"
_SAFE_SEGMENT = re.compile(r"^[A-Za-z0-9_.-]+$")
EXPLORE_FINDINGS_FILE = "findings.md"
EXPLORE_LEADS_FILE = "leads.md"
EXPLORE_MAX_EVIDENCE_FILES = 48
REVIEW_REPORT_FILE = "review.md"  # 旧格式，仅过渡期兼容
REVIEW_VULN_REPORT_PREFIX = "vuln_report-"
REVIEW_MAX_EVIDENCE_FILES = 48


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
    results.extend(
        _collect_jsfinder_results(
            (root, root / "workspace"),
            root=root,
            artifact_type="bootstrap.jsfinder",
            summary="JSFinder 能力树",
            registered={item.relative_name for item in results},
        )
    )
    results.extend(_collect_bootstrap_extras(root, registered={item.relative_name for item in results}))
    results.extend(_collect_bootstrap_evidence(root, registered={item.relative_name for item in results}))
    return results


BOOTSTRAP_EXTRA_MAX_FILES = 20
BOOTSTRAP_EXTRA_MAX_BYTES = 10 * 1024 * 1024
BOOTSTRAP_MAX_EVIDENCE_FILES = 48
# worker 留证习惯不统一：技能脚本写 `{run_dir}/evidence/`，提示词约定写
# `{run_dir}/artifacts/evidence/`。两处都收，登记名统一为 evidence/<rel>。
BOOTSTRAP_EVIDENCE_DIRS = ("evidence", "artifacts/evidence")
_BOOTSTRAP_SYSTEM_FILES = frozenset(
    {
        "bootstrap_artifacts.json",
        "auth.json",
        "models.json",
        "models-store.json",
        "phase-capabilities.json",
        "explore_result.json",
    }
) | set(BOOTSTRAP_ARTIFACT_TYPES)


def _collect_bootstrap_extras(root: Path, *, registered: set[str]) -> list[StageFile]:
    """保留白名单之外的全部一级常规文件为 bootstrap.file。

    worker 常在核心三件套之外产出有价值的额外材料（如 api_endpoints_all.txt、
    jsdump/ 摘要）；此前它们不进白名单、随完成清理物理消失。护栏：
    单文件 ≤10MB、总数 ≤20、文件名必须安全段、子目录与系统文件跳过。
    """
    extras: list[StageFile] = []
    skipped: list[str] = []
    try:
        entries = sorted(root.iterdir(), key=lambda item: item.name)
    except OSError:
        return extras
    for path in entries:
        if len(extras) >= BOOTSTRAP_EXTRA_MAX_FILES:
            logger.warning("Bootstrap 额外产物超过 %d 个，余者丢弃：%s", BOOTSTRAP_EXTRA_MAX_FILES, path.name)
            break
        if path.name in registered or path.name in _BOOTSTRAP_SYSTEM_FILES:
            continue
        if path.is_dir():
            skipped.append(path.name + "/")
            continue
        if not path.is_file() or path.is_symlink():
            continue
        if not _SAFE_SEGMENT.match(path.name):
            skipped.append(path.name)
            continue
        try:
            size = path.stat().st_size
        except OSError:
            continue
        if size == 0:
            continue
        if size <= _PLACEHOLDER_MAX_BYTES and _is_placeholder_content(path):
            logger.warning("Bootstrap 额外产物是空占位内容，已丢弃：%s", path.name)
            continue
        if size > BOOTSTRAP_EXTRA_MAX_BYTES:
            logger.warning("Bootstrap 额外产物超 10MB，已丢弃：%s（%d bytes）", path.name, size)
            continue
        extras.append(
            StageFile(
                relative_name=path.name,
                path=path,
                artifact_type="bootstrap.file",
                summary=path.name,
            )
        )
    if skipped:
        logger.warning("Bootstrap 额外产物中跳过非安全/目录项：%s", "、".join(skipped))
    return extras


_PLACEHOLDER_MAX_BYTES = 64
_PLACEHOLDER_CONTENT = frozenset(
    {"", "{}", "[]", "null", "none", "nil", "n/a", "na", "-", "not found", "404 not found"}
)


def _is_placeholder_content(path: Path) -> bool:
    """`{}`、`[]` 这类占位内容对复盘没有价值，只会污染产物中心。"""
    try:
        text = path.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError):
        return False
    return text.casefold() in _PLACEHOLDER_CONTENT


def _collect_jsfinder_results(
    bases: tuple[Path, ...],
    *,
    root: Path,
    artifact_type: str,
    summary: str,
    registered: set[str],
) -> list[StageFile]:
    """登记 jsfinder 能力树产物（bootstrap / explore 共用）。

    脚本默认把结果写在运行目录根；不登记就会随 lease 清理消失，
    其它 worker 也看不见。一次运行一个文件，全部收。
    """
    results: list[StageFile] = []
    seen: set[str] = set()
    for base in bases:
        if not base.is_dir() or base.is_symlink():
            continue
        for path in sorted(base.glob(JSFINDER_GLOB)):
            filename = path.name
            if filename in seen or filename in registered:
                continue
            seen.add(filename)
            if path.is_symlink():
                continue
            try:
                _require_regular_output(path, root)
                _require_non_empty(path)
                size = path.stat().st_size
            except (StageOutputError, OSError):
                logger.warning("jsfinder 产物无效，已跳过：%s", filename)
                continue
            if size > BOOTSTRAP_EXTRA_MAX_BYTES:
                logger.warning("jsfinder 产物超 10MB，已丢弃：%s（%d bytes）", filename, size)
                continue
            if size <= _PLACEHOLDER_MAX_BYTES and _is_placeholder_content(path):
                logger.warning("jsfinder 产物是空占位内容，已丢弃：%s", filename)
                continue
            tag = (
                filename[len(JSFINDER_PREFIX):-len(JSFINDER_SUFFIX)]
                if filename.startswith(JSFINDER_PREFIX) and filename.endswith(JSFINDER_SUFFIX)
                else ""
            )
            results.append(
                StageFile(
                    relative_name=filename,
                    path=path,
                    artifact_type=artifact_type,
                    summary=f"{summary} · {tag}" if tag else summary,
                )
            )
    return results


def _collect_bootstrap_evidence(root: Path, *, registered: set[str]) -> list[StageFile]:
    """登记 Bootstrap 留证文件。

    证据在 lease 释放时会被保留到任务证据目录，但只有完成时登记成 artifact
    才会进产物中心；此前整个 evidence/ 目录被当作子目录跳过，证据因此不可见。
    """
    collected: dict[str, StageFile] = {}
    skipped: list[str] = []
    for directory in BOOTSTRAP_EVIDENCE_DIRS:
        evidence_root = root / directory
        if not evidence_root.is_dir() or evidence_root.is_symlink():
            continue
        for current_root, directory_names, file_names in os.walk(
            evidence_root,
            topdown=True,
            followlinks=False,
        ):
            current = Path(current_root)
            directory_names.sort()
            file_names.sort()
            retained: list[str] = []
            for name in directory_names:
                path = current / name
                relative = path.relative_to(root)
                if _safe_explore_relative(relative) and not path.is_symlink():
                    retained.append(name)
                else:
                    skipped.append(relative.as_posix() + "/")
            directory_names[:] = retained
            for name in file_names:
                path = current / name
                relative = Path("evidence") / path.relative_to(evidence_root)
                relative_name = relative.as_posix()
                if relative_name in collected or relative_name in registered:
                    continue
                if not _safe_explore_relative(relative) or path.is_symlink():
                    skipped.append(relative_name)
                    continue
                try:
                    _require_regular_output(path, root)
                    if path.stat().st_size <= 0:
                        skipped.append(relative_name)
                        continue
                    if path.stat().st_size <= _PLACEHOLDER_MAX_BYTES and _is_placeholder_content(path):
                        skipped.append(relative_name)
                        continue
                except (StageOutputError, OSError):
                    skipped.append(relative_name)
                    continue
                collected[relative_name] = StageFile(
                    relative_name=relative_name,
                    path=path,
                    artifact_type="bootstrap.evidence",
                    summary=f"Bootstrap 证据：{relative_name}",
                )
    if skipped:
        logger.warning(
            "Bootstrap 证据目录跳过无效项 run_dir=%s count=%d samples=%r",
            root,
            len(skipped),
            skipped[:5],
        )
    items = sorted(collected.values(), key=lambda item: item.relative_name)
    if len(items) > BOOTSTRAP_MAX_EVIDENCE_FILES:
        logger.warning(
            "Bootstrap 证据超过 %d 个，余者不登记：%r",
            BOOTSTRAP_MAX_EVIDENCE_FILES,
            [item.relative_name for item in items[BOOTSTRAP_MAX_EVIDENCE_FILES:]],
        )
        items = items[:BOOTSTRAP_MAX_EVIDENCE_FILES]
    return items


def explore_files(
    run_dir: str | Path,
    *,
    emit_warnings: bool = True,
) -> list[StageFile]:
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

    results.extend(
        _collect_jsfinder_results(
            (run_root,),
            root=run_root,
            artifact_type="explore.jsfinder",
            summary="Explore JSFinder 能力树",
            registered={item.relative_name for item in results},
        )
    )

    evidence, skipped = _collect_optional_explore_evidence(root, required=set(required))
    if len(evidence) > EXPLORE_MAX_EVIDENCE_FILES:
        overflow = evidence[EXPLORE_MAX_EVIDENCE_FILES:]
        evidence = evidence[:EXPLORE_MAX_EVIDENCE_FILES]
        skipped["artifact_limit"].extend(item.relative_name for item in overflow)
    results.extend(evidence)
    if emit_warnings and skipped:
        _log_skipped_explore_outputs(run_root, skipped, registered=len(evidence))
    return results


def review_files(run_dir: str | Path, *, emit_warnings: bool = True) -> list[StageFile]:
    run_root = Path(run_dir).resolve()
    root = run_root / "artifacts"
    if not root.is_dir() or root.is_symlink():
        raise StageOutputError("Reviewer 必须在 artifacts/ 中写入产物（漏洞报告与/或证据）")

    # 审核报告废除：改为「每个「已验证」漏洞一份漏洞报告」。
    # 允许零份（本轮无有效漏洞是合法结果），裁决本身存在漏洞记录的 status + reason 里。
    allowed_root_names: set[str] = set()
    results: list[StageFile] = []
    for path in sorted(root.iterdir(), key=lambda item: item.name):
        if not path.is_file():
            continue
        name = path.name
        if name.startswith(REVIEW_VULN_REPORT_PREFIX) and name.endswith(".md"):
            # 新：漏洞报告（每漏洞一份）。文件名必须是纯 ASCII，
            # 否则存储/引用链路会出问题（提示词里已要求 <slug> 只用 [A-Za-z0-9-]）。
            if not _safe_explore_relative(Path(name)):
                if emit_warnings:
                    logger.warning("漏洞报告文件名非法（须纯 ASCII），已跳过：%s", name)
                continue
            try:
                _require_regular_output(path, root)
                _require_non_empty(path)
            except StageOutputError:
                if emit_warnings:
                    logger.warning("审核漏洞报告无效，已跳过：%s", name)
                continue
            vuln_id = name[len(REVIEW_VULN_REPORT_PREFIX):-len(".md")].split("-")[0]
            results.append(
                StageFile(
                    relative_name=name,
                    path=path,
                    artifact_type="review.vuln_report",
                    summary=f"漏洞报告 · {vuln_id}" if vuln_id else "漏洞报告",
                )
            )
            allowed_root_names.add(name)
        elif name == REVIEW_REPORT_FILE:
            # 过渡期兼容：旧的 review.md 仍收（不再强制要求）
            try:
                _require_regular_output(path, root)
                _require_non_empty(path)
            except StageOutputError:
                continue
            results.append(
                StageFile(
                    relative_name=name,
                    path=path,
                    artifact_type="review.report",
                    summary="Reviewer 漏洞审核记录（旧格式）",
                )
            )
            allowed_root_names.add(name)
    evidence, skipped = _collect_optional_evidence(
        root,
        required=allowed_root_names,
        artifact_type="review.evidence",
        summary_prefix="Reviewer 复测证据",
    )
    if len(evidence) > REVIEW_MAX_EVIDENCE_FILES:
        overflow = evidence[REVIEW_MAX_EVIDENCE_FILES:]
        evidence = evidence[:REVIEW_MAX_EVIDENCE_FILES]
        skipped["artifact_limit"].extend(item.relative_name for item in overflow)
    results.extend(evidence)
    if emit_warnings and skipped:
        _log_skipped_optional_outputs(
            "review",
            run_root,
            skipped,
            registered=len(evidence),
        )
    return results


def _collect_optional_explore_evidence(
    root: Path,
    *,
    required: set[str],
) -> tuple[list[StageFile], dict[str, list[str]]]:
    return _collect_optional_evidence(
        root,
        required=required,
        artifact_type="explore.evidence",
        summary_prefix="Explore 证据",
    )


def _collect_optional_evidence(
    root: Path,
    *,
    required: set[str],
    artifact_type: str,
    summary_prefix: str,
) -> tuple[list[StageFile], dict[str, list[str]]]:
    skipped: dict[str, list[str]] = defaultdict(list)
    try:
        root_entries = sorted(root.iterdir(), key=lambda item: item.name)
    except OSError as exc:
        raise StageOutputError("无法读取 Explore Artifact 目录") from exc

    for path in root_entries:
        if path.name in required or path.name == "evidence":
            continue
        skipped["unsupported_root_entry"].append(path.name)

    evidence_root = root / "evidence"
    if not evidence_root.exists() and not evidence_root.is_symlink():
        return [], skipped
    if evidence_root.is_symlink():
        skipped["symlink"].append("evidence")
        return [], skipped
    if not evidence_root.is_dir():
        skipped["not_directory"].append("evidence")
        return [], skipped

    evidence: list[StageFile] = []
    for current_root, directory_names, file_names in os.walk(
        evidence_root,
        topdown=True,
        followlinks=False,
    ):
        current = Path(current_root)
        directory_names.sort()
        file_names.sort()

        retained_directories: list[str] = []
        for directory_name in directory_names:
            path = current / directory_name
            relative = path.relative_to(root)
            if not _safe_explore_relative(relative):
                skipped["unsafe_path"].append(relative.as_posix())
            elif path.is_symlink():
                skipped["symlink"].append(relative.as_posix())
            else:
                retained_directories.append(directory_name)
        directory_names[:] = retained_directories

        for file_name in file_names:
            path = current / file_name
            relative = path.relative_to(root)
            relative_name = relative.as_posix()
            if not _safe_explore_relative(relative):
                skipped["unsafe_path"].append(relative_name)
                continue
            if path.is_symlink():
                skipped["symlink"].append(relative_name)
                continue
            try:
                _require_regular_output(path, root)
            except StageOutputError:
                skipped["not_regular_or_outside"].append(relative_name)
                continue
            try:
                if path.stat().st_size <= 0:
                    skipped["empty"].append(relative_name)
                    continue
            except OSError:
                skipped["unreadable"].append(relative_name)
                continue
            evidence.append(
                StageFile(
                    relative_name=relative_name,
                    path=path,
                    artifact_type=artifact_type,
                    summary=f"{summary_prefix}：{relative_name}",
                )
            )

    evidence.sort(key=lambda item: item.relative_name)
    return evidence, skipped


def _safe_explore_relative(relative: Path) -> bool:
    return all(
        bool(_SAFE_SEGMENT.fullmatch(segment)) and not segment.startswith(".")
        for segment in relative.parts
    )


def _log_skipped_explore_outputs(
    run_root: Path,
    skipped: dict[str, list[str]],
    *,
    registered: int,
) -> None:
    _log_skipped_optional_outputs("explore", run_root, skipped, registered=registered)


def _log_skipped_optional_outputs(
    stage: str,
    run_root: Path,
    skipped: dict[str, list[str]],
    *,
    registered: int,
) -> None:
    total = 0
    for reason, relative_names in sorted(skipped.items()):
        if not relative_names:
            continue
        total += len(relative_names)
        logger.warning(
            "%s_optional_artifacts_skipped run_dir=%s reason=%s count=%s samples=%r",
            stage,
            run_root,
            reason,
            len(relative_names),
            relative_names[:5],
        )
    logger.warning(
        "%s_optional_artifact_summary run_dir=%s evidence_registered=%s skipped=%s",
        stage,
        run_root,
        registered,
        total,
    )


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
    # 同类型可能有多份（jsfinder 每轮/每目标一个文件）。只保留"最后一份"会让
    # 其余产物在清单里静默消失（DB 行还在，但下游读不到）。
    by_type_multi: dict[str, list[Artifact]] = {}
    for item in artifacts:
        by_type_multi.setdefault(item.artifact_type, []).append(item)

    def _entry(item: Artifact) -> dict[str, object]:
        return {
            "id": item.id,
            "type": item.artifact_type,
            "storage_key": item.storage_key,
            "content_hash": item.content_hash,
            "size": item.size,
            "summary": item.summary,
        }
    payload = {
        "completed": True,
        # 兼容旧读法：文件名 -> 该类型的最后一份
        "artifacts": {
            filename: _entry(by_type[artifact_type])
            for filename, artifact_type in {
                "asset_inventory.md": "bootstrap.asset_inventory",
                "initial_leads.md": "bootstrap.initial_leads",
                "jsfinder_result.json": "bootstrap.jsfinder",
            }.items()
            if artifact_type in by_type
        },
        # 新读法：类型 -> 全部（同类型多份不再被压成一条）
        "artifacts_multi": {
            artifact_type: [_entry(item) for item in items]
            for artifact_type, items in by_type_multi.items()
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
