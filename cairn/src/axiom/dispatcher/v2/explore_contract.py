from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path


class ExploreContractError(ValueError):
    """The durable Explore artifacts are not a complete conclusion."""


_HEADING = re.compile(r"^\s{0,3}#{1,6}\s+(.+?)\s*$")
_PLACEHOLDERS = (
    "待填充",
    "待填写",
    "待补充",
    "待完善",
    "状态：进行中",
    "状态: 进行中",
    "状态：正在执行",
    "状态: 正在执行",
    "运行中，线索将逐步追加",
    "线索将逐步追加",
    "测试进行中",
    "持续更新",
    "尚未完成",
    "结果待定",
    "to be filled",
    "work in progress",
)

_REQUIRED = {
    "findings.md": {
        "本轮摘要": 48,
        "测试范围": 24,
        "证据与结论": 72,
        "漏洞判定": 18,
    },
    "leads.md": {
        "线索摘要": 24,
        "已验证观察": 18,
        "待验证线索": 8,
    },
}


@dataclass(frozen=True)
class ExploreResult:
    findings_summary: str
    verdict: str
    leads_summary: str
    fact: str


def _heading_name(value: str) -> str:
    value = re.sub(r"^[\s\d.、)）(:：-]+", "", value)
    return " ".join(value.strip().split())


def _sections(text: str) -> dict[str, str]:
    sections: dict[str, list[str]] = {}
    current: str | None = None
    for line in text.splitlines():
        match = _HEADING.match(line)
        if match:
            current = _heading_name(match.group(1))
            sections.setdefault(current, [])
            continue
        if current is not None:
            sections[current].append(line)
    return {key: "\n".join(value).strip() for key, value in sections.items()}


def _plain(text: str) -> str:
    text = re.sub(r"```(?:[^\n]*)", "", text)
    text = re.sub(r"!?\[([^\]]+)\]\([^)]*\)", r"\1", text)
    text = text.replace("`", "")
    return " ".join(text.split()).strip()


def _substantive(text: str) -> int:
    return sum(character.isalnum() for character in text)


def _validate_file(path: Path, requirements: dict[str, int]) -> dict[str, str]:
    if not path.is_file() or path.stat().st_size <= 0:
        raise ExploreContractError(f"missing artifact: {path.name}")
    text = path.read_text(encoding="utf-8", errors="replace")[:131072]
    sections = _sections(text)
    lowered = text.casefold()
    for marker in _PLACEHOLDERS:
        if marker.casefold() in lowered:
            raise ExploreContractError(f"unfinished artifact: {path.name} ({marker})")
    for name, minimum in requirements.items():
        content = _plain(sections.get(name, ""))
        if _substantive(content) < minimum:
            raise ExploreContractError(
                f"incomplete artifact: {path.name} section {name}"
            )
    return {name: _plain(sections[name]) for name in requirements}


def validate_explore_artifacts(run_dir: Path) -> ExploreResult:
    findings = _validate_file(run_dir / "findings.md", _REQUIRED["findings.md"])
    leads = _validate_file(run_dir / "leads.md", _REQUIRED["leads.md"])
    if not any(
        marker in findings["证据与结论"]
        for marker in ("请求", "响应", "命令", "输出", "HTTP", "状态码", "端点", "路径")
    ):
        raise ExploreContractError("findings.md evidence section has no concrete evidence")
    fact = (
        f"本轮摘要：{findings['本轮摘要']} "
        f"漏洞判定：{findings['漏洞判定']} "
        f"后续线索：{leads['线索摘要']} "
        "证据索引：findings.md、leads.md。"
    )
    if _substantive(fact) < 120:
        raise ExploreContractError("Explore Fact is too short")
    return ExploreResult(
        findings_summary=findings["本轮摘要"],
        verdict=findings["漏洞判定"],
        leads_summary=leads["线索摘要"],
        fact=fact,
    )
