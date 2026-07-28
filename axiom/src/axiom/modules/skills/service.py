from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import yaml

from axiom.modules.audit.service import AuditService
from axiom.modules.skills.schemas import (
    CreateSkill,
    SaveSkill,
    SetSkillEnabled,
    Skill,
    SkillContent,
)
from axiom.shared.errors import ConflictError, NotFoundError


_SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_STATE_FILENAME = ".skill-state.json"


class SkillService:
    """Expose shared Skill folders; workers receive paths rather than copied content."""

    def __init__(self, data_dir: Path, engine=None):
        self.engine = engine
        self.managed_root = (data_dir / "skills").resolve()
        self.bundled_root = (Path(__file__).parents[2] / "dispatcher" / "skills").resolve()
        self.managed_root.mkdir(parents=True, exist_ok=True)

    def directories(self) -> list[str]:
        return [str(path) for path in (self.managed_root, self.bundled_root) if path.is_dir()]

    def active_paths(self) -> list[str]:
        """Return shared paths for enabled Skills, without copying their files."""
        return [str(Path(skill.path).parent) for skill in self.list() if skill.enabled]

    def list(self) -> list[Skill]:
        result: dict[str, Skill] = {}
        state = self._load_state()
        for source, root in (("managed", self.managed_root), ("bundled", self.bundled_root)):
            if not root.is_dir():
                continue
            for skill_file in sorted(root.glob("*/SKILL.md")):
                name = skill_file.parent.name
                if name in result:
                    continue
                title, description = _metadata(skill_file)
                result[name] = Skill(
                    name=name,
                    title=title,
                    description=description,
                    path=str(skill_file),
                    source=source,
                    content=skill_file.read_text(encoding="utf-8"),
                    enabled=state.get(name, True),
                    builtin=source == "bundled",
                )
        return sorted(result.values(), key=lambda item: item.name.casefold())

    def get(self, name: str) -> SkillContent:
        name = self._name(name)
        for item in self.list():
            if item.name == name:
                return SkillContent.model_validate(item.model_dump())
        raise NotFoundError(f"Skill not found: {name}")

    def save(self, name: str, command: SaveSkill) -> SkillContent:
        name = self._name(name)
        directory = self.managed_root / name
        existing_content = self._existing_content(name)
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / "SKILL.md"
        temporary = directory / ".SKILL.md.tmp"
        content = command.content
        if command.description is not None:
            content = _with_frontmatter(
                name,
                command.description,
                command.content,
                existing_content=existing_content,
            )
        else:
            content = _collapse_duplicate_frontmatter(content)
        temporary.write_text(content, encoding="utf-8")
        temporary.replace(path)
        if command.enabled is not None:
            self._set_enabled(name, command.enabled)
        return self.get(name)

    def set_enabled(self, name: str, command: SetSkillEnabled) -> SkillContent:
        skill = self.get(name)
        self._set_enabled(skill.name, command.enabled)
        return self.get(skill.name)

    def create(self, command: CreateSkill) -> SkillContent:
        name = self._name(command.name)
        path = self.managed_root / name / "SKILL.md"
        if path.exists():
            raise ConflictError(f"Skill already exists: {name}")
        return self.save(
            name,
            SaveSkill(
                description=command.description or name,
                content=command.content or "请填写 Skill 内容",
            ),
        )

    def files(self, name: str) -> list[dict[str, object]]:
        skill = self.get(name)
        root = Path(skill.path).parent
        return [
            {"path": str(path.relative_to(root)), "size": path.stat().st_size}
            for path in sorted(root.rglob("*"))
            if path.is_file()
        ]

    def file(self, name: str, relative_path: str) -> dict[str, str]:
        path = self._safe_file(name, relative_path)
        if not path.is_file():
            raise NotFoundError(f"Skill file not found: {relative_path}")
        return {"path": relative_path, "content": path.read_text(encoding="utf-8")}

    def save_file(self, name: str, relative_path: str, content: str) -> dict[str, str]:
        path = self._safe_file(name, relative_path)
        root = self._skill_root(name)
        if root == (self.bundled_root / name).resolve():
            if Path(relative_path).parts != ("SKILL.md",):
                raise ConflictError(
                    "Bundled Skill files are read-only; create a managed override first"
                )
            saved = self.save(name, SaveSkill(content=content))
            return {"path": relative_path, "content": saved.content}
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.tmp")
        temporary.write_text(content.rstrip() + "\n", encoding="utf-8")
        temporary.replace(path)
        return {"path": relative_path, "content": path.read_text(encoding="utf-8")}

    def create_file(self, name: str, relative_path: str, content: str) -> dict[str, str]:
        self._require_managed_root(name)
        path = self._safe_file(name, relative_path)
        if path.exists():
            raise ConflictError(f"Skill file already exists: {relative_path}")
        return self.save_file(name, relative_path, content)

    def delete_file(self, name: str, relative_path: str) -> None:
        self._require_managed_root(name)
        path = self._safe_file(name, relative_path)
        if path.name == "SKILL.md":
            raise ConflictError("SKILL.md cannot be deleted")
        if not path.is_file():
            raise NotFoundError(f"Skill file not found: {relative_path}")
        path.unlink()

    def usage(self) -> dict[str, list[dict[str, object]]]:
        if self.engine is None:
            return {"items": []}
        events = AuditService(self.engine).list(limit=2_000)
        result: dict[str, dict[str, object]] = {}
        for event in events:
            payload = event.payload or {}
            names = payload.get("skills_used") or payload.get("skill") or payload.get("skill_name")
            names = names if isinstance(names, list) else [names]
            for name in names:
                if not name:
                    continue
                item = result.setdefault(str(name), {"skill": str(name), "calls": 0, "workers": set(), "phases": set(), "last_used_at": event.created_at})
                item["calls"] += 1
                if event.worker:
                    item["workers"].add(event.worker)
                phase = payload.get("phase")
                if phase:
                    item["phases"].add(str(phase))
        items = []
        for item in result.values():
            items.append({**item, "workers": sorted(item["workers"]), "phases": sorted(item["phases"])})
        items.sort(key=lambda value: (-value["calls"], value["skill"]))
        return {"items": items}

    def delete(self, name: str) -> None:
        name = self._name(name)
        directory = self.managed_root / name
        path = directory / "SKILL.md"
        if not path.is_file():
            raise NotFoundError(f"Managed Skill not found: {name}")
        path.unlink()
        try:
            directory.rmdir()
        except OSError:
            pass

    def _load_state(self) -> dict[str, bool]:
        try:
            raw = json.loads(
                (self.managed_root / _STATE_FILENAME).read_text(encoding="utf-8")
            )
        except (OSError, json.JSONDecodeError):
            return {}
        if not isinstance(raw, dict):
            return {}
        return {str(name): bool(enabled) for name, enabled in raw.items()}

    def _set_enabled(self, name: str, enabled: bool) -> None:
        state = self._load_state()
        state[name] = bool(enabled)
        temporary = self.managed_root / f".{_STATE_FILENAME}.tmp"
        temporary.write_text(
            json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        temporary.replace(self.managed_root / _STATE_FILENAME)

    @staticmethod
    def _name(value: str) -> str:
        value = value.strip()
        if not _SAFE_NAME.fullmatch(value):
            raise ConflictError("Invalid Skill name")
        return value

    def _safe_file(self, name: str, relative_path: str) -> Path:
        self._name(name)
        relative = Path(relative_path)
        if not relative_path.strip() or relative.is_absolute() or ".." in relative.parts:
            raise ConflictError("Invalid Skill file path")
        root = self._skill_root(name)
        path = (root / relative).resolve()
        if not root.is_relative_to(self.managed_root) and not root.is_relative_to(
            self.bundled_root
        ):
            raise ConflictError("Invalid Skill file path")
        if root not in path.parents and path != root:
            raise ConflictError("Invalid Skill file path")
        return path

    def _skill_root(self, name: str) -> Path:
        name = self._name(name)
        for root in (self.managed_root, self.bundled_root):
            directory = (root / name).resolve()
            if (directory / "SKILL.md").is_file():
                return directory
        raise NotFoundError(f"Skill not found: {name}")

    def _existing_content(self, name: str) -> str:
        for root in (self.managed_root, self.bundled_root):
            path = root / name / "SKILL.md"
            if path.is_file():
                return path.read_text(encoding="utf-8", errors="replace")
        return ""

    def _require_managed_root(self, name: str) -> Path:
        root = self._skill_root(name)
        if not root.is_relative_to(self.managed_root):
            raise ConflictError("Bundled Skill files are read-only")
        return root


def _metadata(path: Path) -> tuple[str, str]:
    content = path.read_text(encoding="utf-8", errors="replace")
    layers, body = _leading_frontmatter(content)
    frontmatter = _merge_frontmatter(layers)
    lines = body.splitlines()
    title = next(
        (line.lstrip("# ").strip() for line in lines if line.strip().startswith("#")),
        "",
    )
    title = title or str(frontmatter.get("name") or path.parent.name)
    description = str(frontmatter.get("description") or "").strip()
    if not description:
        description = next((line.strip() for line in lines if line.strip()), "")
    return title or path.parent.name, description[:300]


def _with_frontmatter(
    name: str,
    description: str,
    content: str,
    *,
    existing_content: str = "",
) -> str:
    existing_layers, _ = _leading_frontmatter(existing_content)
    incoming_layers, body = _leading_frontmatter(content)
    metadata = _merge_frontmatter(existing_layers)
    metadata.update(_merge_frontmatter(incoming_layers))
    metadata.update({"name": name, "description": description.strip()})
    return _render_skill_document(metadata, body)


def _collapse_duplicate_frontmatter(content: str) -> str:
    layers, body = _leading_frontmatter(content)
    if len(layers) < 2:
        return content
    return _render_skill_document(_merge_frontmatter(layers), body)


def _leading_frontmatter(content: str) -> tuple[list[dict[str, Any]], str]:
    remaining = content.lstrip("\ufeff")
    layers: list[dict[str, Any]] = []
    while True:
        lines = remaining.splitlines(keepends=True)
        if not lines or lines[0].strip() != "---":
            break
        try:
            end = next(
                index for index, line in enumerate(lines[1:], 1) if line.strip() == "---"
            )
        except StopIteration:
            break
        try:
            parsed = yaml.safe_load("".join(lines[1:end]))
        except yaml.YAMLError:
            break
        if not isinstance(parsed, dict):
            break
        layers.append(dict(parsed))
        remaining = "".join(lines[end + 1 :]).lstrip("\r\n")
    return layers, remaining


def _merge_frontmatter(layers: list[dict[str, Any]]) -> dict[str, Any]:
    merged: dict[str, Any] = {}
    # The outermost layer is the newest one produced by the old double-wrap bug.
    # Merge from inner to outer so its name/description wins while useful inner
    # metadata such as requirements is retained.
    for layer in reversed(layers):
        merged.update(layer)
    return merged


def _render_skill_document(metadata: dict[str, Any], body: str) -> str:
    frontmatter = yaml.safe_dump(
        metadata,
        allow_unicode=True,
        sort_keys=False,
        default_flow_style=False,
    ).strip()
    normalized_body = body.strip()
    return f"---\n{frontmatter}\n---\n\n{normalized_body}\n"
