from __future__ import annotations

import hashlib
import json
import re
import shutil
import threading
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
_SYNC_STATE_FILENAME = ".skill-sync-state.json"
_DELETED_FILES_FILENAME = ".skill-file-deletions.json"
_SKILL_FILE_PREVIEW_LIMIT = 512 * 1024
MANDATORY_COMMON_SKILLS = frozenset({"whatisvul"})


class SkillService:
    """Expose shared Skill folders; workers receive paths rather than copied content."""

    def __init__(self, data_dir: Path, engine=None):
        self.engine = engine
        self.managed_root = (data_dir / "skills").resolve()
        self.bundled_root = (Path(__file__).parents[2] / "dispatcher" / "skills").resolve()
        self.managed_root.mkdir(parents=True, exist_ok=True)
        self._sync_lock = threading.RLock()
        self._list_cache_signature: str | None = None
        self._list_cache: tuple[Skill, ...] | None = None
        self._sync_bundled_overrides()
        self._sync_bundled_support_files()
        self._ensure_mandatory_enabled()

    def directories(self) -> list[str]:
        return [str(path) for path in (self.managed_root, self.bundled_root) if path.is_dir()]

    def active_paths(self) -> list[str]:
        """Return shared paths for enabled Skills, without copying their files."""
        return [str(Path(skill.path).parent) for skill in self.list() if skill.enabled]

    def list(self) -> list[Skill]:
        # Refreshing this endpoint is also the runtime synchronization point:
        # managed copies which were previously synchronized from source are
        # updated from the bundled tree, while explicit user overrides remain
        # untouched.  The package metadata signature lets the common read path
        # return immediately without re-hashing large support files (for
        # example the ffuf dictionaries) on every lease/context request.
        with self._sync_lock:
            signature = _tree_metadata_signature(
                (self.managed_root, self.bundled_root)
            )
            if (
                self._list_cache is not None
                and signature == self._list_cache_signature
            ):
                return list(self._list_cache)

            self._sync_bundled_overrides()
            self._sync_bundled_support_files()
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
                    bundled_path = self.bundled_root / name / "SKILL.md"
                    bundled_hash = _file_hash(bundled_path) if bundled_path.is_file() else ""
                    effective_hash = _tree_hash(skill_file.parent)
                    sync_status, update_available = self._sync_status(
                        name=name,
                        source=source,
                        bundled_hash=bundled_hash,
                    )
                    result[name] = Skill(
                        name=name,
                        title=title,
                        description=description,
                        path=str(skill_file),
                        source=source,
                        content=skill_file.read_text(encoding="utf-8"),
                        enabled=state.get(name, True),
                        builtin=source == "bundled",
                        bundled_hash=bundled_hash,
                        effective_hash=effective_hash,
                        sync_status=sync_status,
                        bundled_update_available=update_available,
                    )
            cached = tuple(sorted(result.values(), key=lambda item: item.name.casefold()))
            self._list_cache = cached
            # Synchronization can create/update managed support files, so the
            # signature must be captured after the list has been materialized.
            self._list_cache_signature = _tree_metadata_signature(
                (self.managed_root, self.bundled_root)
            )
            return list(cached)

    def get(self, name: str) -> SkillContent:
        name = self._name(name)
        for item in self.list():
            if item.name == name:
                return SkillContent.model_validate(item.model_dump())
        raise NotFoundError(f"Skill not found: {name}")

    def save(self, name: str, command: SaveSkill) -> SkillContent:
        name = self._name(name)
        if (
            command.enabled is not None
            and not command.enabled
            and name in MANDATORY_COMMON_SKILLS
        ):
            raise ConflictError(
                f"Skill {name} 是五阶段锁定的通用 Skill，不能停用"
            )
        directory = self.managed_root / name
        existing_content = self._existing_content(name)
        self._materialize_bundled_support_files(name, directory)
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
        self._mark_custom(name)
        if command.enabled is not None:
            self._set_enabled(name, command.enabled)
        return self.get(name)

    def set_enabled(self, name: str, command: SetSkillEnabled) -> SkillContent:
        skill = self.get(name)
        if skill.name in MANDATORY_COMMON_SKILLS and not command.enabled:
            raise ConflictError(f"Skill {skill.name} 是五阶段锁定的通用 Skill，不能停用")
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

    def file(
        self, name: str, relative_path: str, preview_bytes: int | None = None
    ) -> dict[str, object]:
        path = self._safe_file(name, relative_path)
        if not path.is_file():
            raise NotFoundError(f"Skill file not found: {relative_path}")

        size = path.stat().st_size
        if preview_bytes is None:
            content = path.read_text(encoding="utf-8")
            truncated = False
        else:
            limit = min(max(int(preview_bytes), 1), _SKILL_FILE_PREVIEW_LIMIT)
            with path.open("rb") as handle:
                content = handle.read(limit).decode("utf-8", errors="replace")
            truncated = size > limit
            if not truncated:
                content = path.read_text(encoding="utf-8")
        return {
            "path": relative_path,
            "content": content,
            "size": size,
            "truncated": truncated,
        }

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
        self._mark_support_file_custom(name, relative_path)
        self._clear_deleted_file(name, relative_path)
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
        bundled_path = (self.bundled_root / name / relative_path).resolve()
        if bundled_path.is_file():
            self._mark_deleted_file(name, relative_path)
            self._mark_support_file_deleted(name, relative_path)
        else:
            self._clear_support_file_state(name, relative_path)

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
        if name in MANDATORY_COMMON_SKILLS:
            raise ConflictError(f"Skill {name} 是五阶段锁定的通用 Skill，不能删除")
        directory = self.managed_root / name
        path = directory / "SKILL.md"
        if not path.is_file():
            raise NotFoundError(f"Managed Skill not found: {name}")
        shutil.rmtree(directory)
        self._clear_deleted_files(name)
        self._clear_sync_state(name)

    def restore_bundled(self, name: str) -> SkillContent:
        """Drop a managed override and use the current bundled Skill.

        This is intentionally explicit: existing managed files may contain
        user-authored changes and must never be overwritten merely because a
        source checkout changed.
        """

        name = self._name(name)
        bundled = self.bundled_root / name / "SKILL.md"
        if not bundled.is_file():
            raise NotFoundError(f"Bundled Skill not found: {name}")
        managed = self.managed_root / name
        if managed.exists():
            shutil.rmtree(managed)
        self._clear_deleted_files(name)
        self._clear_sync_state(name)
        return self.get(name)

    def _sync_bundled_overrides(self) -> None:
        """Update only managed copies previously known to be auto-synced.

        Before this state file existed, a managed copy could have been edited
        by a user or produced by an older deployment.  If its body differs
        from the bundled body on first observation, classify it conservatively
        as ``custom`` rather than destroying it.  Equal copies become
        ``synced`` and will follow future bundled updates automatically.
        """

        with self._sync_lock:
            if not self.bundled_root.is_dir():
                return
            state = self._load_sync_state()
            changed = False
            bundled_names = {
                path.parent.name for path in self.bundled_root.glob("*/SKILL.md")
            }
            for name in sorted(bundled_names):
                bundled_path = self.bundled_root / name / "SKILL.md"
                managed_path = self.managed_root / name / "SKILL.md"
                if not managed_path.is_file():
                    if name in state:
                        state.pop(name, None)
                        changed = True
                    continue

                bundled_hash = _file_hash(bundled_path)
                managed_hash = _file_hash(managed_path)
                record = state.get(name)
                if not isinstance(record, dict):
                    record = None
                mode = str(record.get("mode")) if record else ""
                previous_bundled_hash = (
                    str(record.get("bundled_hash") or "") if record else ""
                )
                previous_managed_hash = (
                    str(record.get("managed_hash") or "") if record else ""
                )

                if record is None:
                    mode = "synced" if managed_hash == bundled_hash else "custom"
                    state[name] = {
                        "mode": mode,
                        "bundled_hash": bundled_hash,
                        "managed_hash": managed_hash,
                        "support_files": {},
                    }
                    changed = True
                    continue

                support_files = record.get("support_files")
                if not isinstance(support_files, dict):
                    support_files = {}

                if mode == "synced":
                    if managed_hash != previous_managed_hash:
                        state[name] = {
                            "mode": "custom",
                            "bundled_hash": previous_bundled_hash or bundled_hash,
                            "managed_hash": managed_hash,
                            "support_files": support_files,
                        }
                        changed = True
                        continue
                    if bundled_hash != previous_bundled_hash:
                        _atomic_copy(bundled_path, managed_path)
                        managed_hash = bundled_hash
                        state[name] = {
                            "mode": "synced",
                            "bundled_hash": bundled_hash,
                            "managed_hash": managed_hash,
                            "support_files": support_files,
                        }
                        changed = True
                elif mode != "custom":
                    state[name] = {
                        "mode": "custom",
                        "bundled_hash": bundled_hash,
                        "managed_hash": managed_hash,
                        "support_files": support_files,
                    }
                    changed = True

            for name in set(state) - bundled_names:
                # Managed-only Skills are user-created and remain custom, but an
                # abandoned record should not survive deletion of the override.
                if not (self.managed_root / name / "SKILL.md").is_file():
                    state.pop(name, None)
                    changed = True
            if changed:
                self._write_sync_state(state)

    def _sync_status(self, *, name: str, source: str, bundled_hash: str) -> tuple[str, bool]:
        if source == "bundled":
            return "bundled", False
        record = self._load_sync_state().get(name)
        if not isinstance(record, dict):
            return "custom", False
        support_files = record.get("support_files")
        if not isinstance(support_files, dict):
            support_files = {}
        custom_support = any(
            isinstance(item, dict)
            and str(item.get("mode")) in {"custom", "deleted"}
            for item in support_files.values()
        ) or bool(self._load_deleted_files().get(name))
        available = bool(
            bundled_hash
            and str(record.get("bundled_hash") or "")
            and bundled_hash != str(record.get("bundled_hash"))
        )
        for relative_path, item in support_files.items():
            if not isinstance(item, dict) or str(item.get("mode")) not in {
                "custom",
                "deleted",
            }:
                continue
            current_bundled_hash = _file_hash(
                self.bundled_root / name / relative_path
            )
            previous_bundled_hash = str(item.get("bundled_hash") or "")
            if current_bundled_hash and current_bundled_hash != previous_bundled_hash:
                available = True
                break
        if str(record.get("mode")) == "synced" and not custom_support:
            return "synced", False
        return ("bundled_update_available" if available else "custom"), available

    def _mark_custom(self, name: str) -> None:
        with self._sync_lock:
            bundled = self.bundled_root / name / "SKILL.md"
            managed = self.managed_root / name / "SKILL.md"
            state = self._load_sync_state()
            current = state.get(name)
            support_files = (
                current.get("support_files", {}) if isinstance(current, dict) else {}
            )
            state[name] = {
                "mode": "custom",
                "bundled_hash": _file_hash(bundled) if bundled.is_file() else "",
                "managed_hash": _file_hash(managed) if managed.is_file() else "",
                "support_files": support_files,
            }
            self._write_sync_state(state)

    def _mark_support_file_custom(self, name: str, relative_path: str) -> None:
        self._set_support_file_state(name, relative_path, mode="custom")

    def _mark_support_file_deleted(self, name: str, relative_path: str) -> None:
        self._set_support_file_state(name, relative_path, mode="deleted")

    def _set_support_file_state(
        self, name: str, relative_path: str, *, mode: str
    ) -> None:
        with self._sync_lock:
            relative_path = Path(relative_path).as_posix()
            state = self._load_sync_state()
            record = state.get(name)
            if not isinstance(record, dict):
                bundled_skill = self.bundled_root / name / "SKILL.md"
                managed_skill = self.managed_root / name / "SKILL.md"
                bundled_hash = _file_hash(bundled_skill)
                managed_hash = _file_hash(managed_skill)
                record = {
                    "mode": "synced" if bundled_hash == managed_hash else "custom",
                    "bundled_hash": bundled_hash,
                    "managed_hash": managed_hash,
                    "support_files": {},
                }
            support_files = record.get("support_files")
            if not isinstance(support_files, dict):
                support_files = {}
            bundled = self.bundled_root / name / relative_path
            managed = self.managed_root / name / relative_path
            support_files[relative_path] = {
                "mode": mode,
                "bundled_hash": _file_hash(bundled),
                "managed_hash": _file_hash(managed),
            }
            record["support_files"] = support_files
            state[name] = record
            self._write_sync_state(state)

    def _clear_support_file_state(self, name: str, relative_path: str) -> None:
        with self._sync_lock:
            relative_path = Path(relative_path).as_posix()
            state = self._load_sync_state()
            record = state.get(name)
            if not isinstance(record, dict):
                return
            support_files = record.get("support_files")
            if not isinstance(support_files, dict) or relative_path not in support_files:
                return
            support_files.pop(relative_path, None)
            record["support_files"] = support_files
            state[name] = record
            self._write_sync_state(state)

    def _clear_sync_state(self, name: str) -> None:
        with self._sync_lock:
            state = self._load_sync_state()
            if name in state:
                state.pop(name, None)
                self._write_sync_state(state)

    def _load_sync_state(self) -> dict[str, dict[str, Any]]:
        try:
            raw = json.loads(
                (self.managed_root / _SYNC_STATE_FILENAME).read_text(encoding="utf-8")
            )
        except (OSError, json.JSONDecodeError):
            return {}
        if not isinstance(raw, dict):
            return {}
        return {
            str(name): value
            for name, value in raw.items()
            if isinstance(value, dict)
        }

    def _write_sync_state(self, state: dict[str, dict[str, Any]]) -> None:
        path = self.managed_root / _SYNC_STATE_FILENAME
        if not state:
            path.unlink(missing_ok=True)
            return
        temporary = self.managed_root / f".{_SYNC_STATE_FILENAME}.tmp"
        temporary.write_text(
            json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        temporary.replace(path)

    def _sync_bundled_support_files(self) -> None:
        """Synchronize bundled auxiliary files without overwriting user edits.

        Each managed support file keeps its own last-known bundled and managed
        hashes. Files that still match the previous managed hash follow source
        updates automatically. Direct edits, explicit deletions, and managed-
        only files become custom and remain untouched.
        """

        with self._sync_lock:
            if not self.bundled_root.is_dir() or not self.managed_root.is_dir():
                return
            state = self._load_sync_state()
            deleted_files = self._load_deleted_files()
            changed = False
            for bundled_skill in sorted(self.bundled_root.glob("*/SKILL.md")):
                name = bundled_skill.parent.name
                bundled_directory = bundled_skill.parent
                managed_directory = self.managed_root / name
                managed_skill = managed_directory / "SKILL.md"
                if not managed_skill.is_file():
                    continue

                record = state.get(name)
                if not isinstance(record, dict):
                    bundled_hash = _file_hash(bundled_skill)
                    managed_hash = _file_hash(managed_skill)
                    record = {
                        "mode": "synced" if bundled_hash == managed_hash else "custom",
                        "bundled_hash": bundled_hash,
                        "managed_hash": managed_hash,
                        "support_files": {},
                    }
                    changed = True
                support_files = record.get("support_files")
                if not isinstance(support_files, dict):
                    support_files = {}
                    changed = True

                bundled_files = {
                    relative: source
                    for relative, source in _skill_support_files(bundled_directory)
                }
                deleted_for_skill = deleted_files.get(name, set())
                for relative_path, source in bundled_files.items():
                    target = managed_directory / relative_path
                    bundled_hash = _file_hash(source)
                    item = support_files.get(relative_path)
                    if relative_path in deleted_for_skill:
                        deleted_item = {
                            "mode": "deleted",
                            "bundled_hash": (
                                str(item.get("bundled_hash") or bundled_hash)
                                if isinstance(item, dict)
                                else bundled_hash
                            ),
                            "managed_hash": "",
                        }
                        if item != deleted_item:
                            support_files[relative_path] = deleted_item
                            changed = True
                        continue

                    if not target.is_file():
                        _atomic_copy(source, target)
                        support_files[relative_path] = {
                            "mode": "synced",
                            "bundled_hash": bundled_hash,
                            "managed_hash": bundled_hash,
                        }
                        changed = True
                        continue

                    managed_hash = _file_hash(target)
                    if not isinstance(item, dict):
                        support_files[relative_path] = {
                            "mode": "synced" if managed_hash == bundled_hash else "custom",
                            "bundled_hash": bundled_hash,
                            "managed_hash": managed_hash,
                        }
                        changed = True
                        continue

                    mode = str(item.get("mode") or "")
                    previous_bundled_hash = str(item.get("bundled_hash") or "")
                    previous_managed_hash = str(item.get("managed_hash") or "")
                    if managed_hash == bundled_hash:
                        normalized = {
                            "mode": "synced",
                            "bundled_hash": bundled_hash,
                            "managed_hash": managed_hash,
                        }
                        if item != normalized:
                            support_files[relative_path] = normalized
                            changed = True
                        continue
                    if mode == "synced":
                        if managed_hash != previous_managed_hash:
                            support_files[relative_path] = {
                                "mode": "custom",
                                "bundled_hash": previous_bundled_hash or bundled_hash,
                                "managed_hash": managed_hash,
                            }
                            changed = True
                        elif bundled_hash != previous_bundled_hash:
                            _atomic_copy(source, target)
                            support_files[relative_path] = {
                                "mode": "synced",
                                "bundled_hash": bundled_hash,
                                "managed_hash": bundled_hash,
                            }
                            changed = True
                    elif mode != "custom":
                        support_files[relative_path] = {
                            "mode": "custom",
                            "bundled_hash": previous_bundled_hash or bundled_hash,
                            "managed_hash": managed_hash,
                        }
                        changed = True
                    elif managed_hash != previous_managed_hash:
                        item["managed_hash"] = managed_hash
                        support_files[relative_path] = item
                        changed = True

                for relative_path in set(support_files) - set(bundled_files):
                    item = support_files.get(relative_path)
                    target = managed_directory / relative_path
                    if not target.is_file():
                        support_files.pop(relative_path, None)
                        changed = True
                        continue
                    if not isinstance(item, dict):
                        support_files[relative_path] = {
                            "mode": "custom",
                            "bundled_hash": "",
                            "managed_hash": _file_hash(target),
                        }
                        changed = True
                        continue
                    if str(item.get("mode")) == "synced":
                        managed_hash = _file_hash(target)
                        if managed_hash == str(item.get("managed_hash") or ""):
                            target.unlink()
                            support_files.pop(relative_path, None)
                        else:
                            support_files[relative_path] = {
                                "mode": "custom",
                                "bundled_hash": "",
                                "managed_hash": managed_hash,
                            }
                        changed = True

                for relative_path, target in _skill_support_files(managed_directory):
                    if relative_path in bundled_files or relative_path in support_files:
                        continue
                    support_files[relative_path] = {
                        "mode": "custom",
                        "bundled_hash": "",
                        "managed_hash": _file_hash(target),
                    }
                    changed = True

                record["support_files"] = support_files
                state[name] = record
            if changed:
                self._write_sync_state(state)

    def _materialize_bundled_support_files(
        self, name: str, directory: Path
    ) -> None:
        """Keep auxiliary files when a bundled Skill gets a managed SKILL.md override."""

        directory.mkdir(parents=True, exist_ok=True)
        bundled = self.bundled_root / name
        if not (bundled / "SKILL.md").is_file():
            return
        deleted_files = self._load_deleted_files().get(name, set())
        for source in sorted(bundled.rglob("*")):
            relative = source.relative_to(bundled)
            if (
                relative.as_posix() in deleted_files
                or relative == Path("SKILL.md")
                or "__pycache__" in relative.parts
                or source.suffix in {".pyc", ".pyo"}
                or source.name == ".DS_Store"
                or source.is_symlink()
            ):
                continue
            target = directory / relative
            if source.is_dir():
                target.mkdir(parents=True, exist_ok=True)
            elif source.is_file() and not target.exists():
                target.parent.mkdir(parents=True, exist_ok=True)
                temporary = target.with_name(f".{target.name}.copy.tmp")
                shutil.copy2(source, temporary)
                temporary.replace(target)

    def _load_deleted_files(self) -> dict[str, set[str]]:
        try:
            raw = json.loads(
                (self.managed_root / _DELETED_FILES_FILENAME).read_text(
                    encoding="utf-8"
                )
            )
        except (OSError, json.JSONDecodeError):
            return {}
        if not isinstance(raw, dict):
            return {}
        result: dict[str, set[str]] = {}
        for name, paths in raw.items():
            if isinstance(paths, list):
                result[str(name)] = {str(path) for path in paths if str(path).strip()}
        return result

    def _write_deleted_files(self, deleted: dict[str, set[str]]) -> None:
        cleaned = {
            name: sorted(paths)
            for name, paths in deleted.items()
            if paths
        }
        state_path = self.managed_root / _DELETED_FILES_FILENAME
        if not cleaned:
            state_path.unlink(missing_ok=True)
            return
        temporary = self.managed_root / f".{_DELETED_FILES_FILENAME}.tmp"
        temporary.write_text(
            json.dumps(cleaned, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        temporary.replace(state_path)

    def _mark_deleted_file(self, name: str, relative_path: str) -> None:
        deleted = self._load_deleted_files()
        deleted.setdefault(name, set()).add(Path(relative_path).as_posix())
        self._write_deleted_files(deleted)

    def _clear_deleted_file(self, name: str, relative_path: str) -> None:
        deleted = self._load_deleted_files()
        paths = deleted.get(name)
        if not paths:
            return
        paths.discard(Path(relative_path).as_posix())
        if not paths:
            deleted.pop(name, None)
        self._write_deleted_files(deleted)

    def _clear_deleted_files(self, name: str) -> None:
        deleted = self._load_deleted_files()
        if name in deleted:
            deleted.pop(name, None)
            self._write_deleted_files(deleted)

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
        self._write_state(state)

    def _ensure_mandatory_enabled(self) -> None:
        """Repair legacy state that disabled a now-mandatory shared Skill."""

        available = {
            path.parent.name
            for root in (self.managed_root, self.bundled_root)
            if root.is_dir()
            for path in root.glob("*/SKILL.md")
        }
        mandatory = MANDATORY_COMMON_SKILLS & available
        if not mandatory:
            return
        state = self._load_state()
        changed = False
        for name in mandatory:
            if state.get(name) is False:
                state[name] = True
                changed = True
        if changed:
            self._write_state(state)

    def _write_state(self, state: dict[str, bool]) -> None:
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


def _file_hash(path: Path) -> str:
    if not path.is_file():
        return ""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()



def _tree_metadata_signature(roots: tuple[Path, ...]) -> str:
    """Return a cheap signature for Skill package metadata, not file contents."""

    digest = hashlib.sha256()
    seen: set[Path] = set()
    ignored_suffixes = {".pyc", ".pyo", ".tmp"}
    for root in roots:
        root = root.resolve()
        if not root.is_dir() or root in seen:
            continue
        seen.add(root)
        for path in sorted(root.rglob("*")):
            try:
                relative = path.relative_to(root)
                if (
                    path.is_symlink()
                    or "__pycache__" in relative.parts
                    or path.name == ".DS_Store"
                    or path.suffix in ignored_suffixes
                    or not path.is_file()
                ):
                    continue
                stat = path.stat()
            except OSError:
                # A concurrently changed support file will be observed by the
                # next call and must not make the request fail.
                continue
            digest.update(str(root).encode("utf-8"))
            digest.update(b"\0")
            digest.update(relative.as_posix().encode("utf-8"))
            digest.update(b"\0")
            digest.update(str(stat.st_size).encode("ascii"))
            digest.update(b"\0")
            digest.update(str(stat.st_mtime_ns).encode("ascii"))
            digest.update(b"\0")
            digest.update(str(stat.st_ctime_ns).encode("ascii"))
            digest.update(b"\0")
    return digest.hexdigest()

def _tree_hash(root: Path) -> str:
    """Hash a Skill directory deterministically without executing its files."""

    if not root.is_dir():
        return ""
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root)
        if (
            not path.is_file()
            or path.is_symlink()
            or "__pycache__" in relative.parts
            or path.suffix in {".pyc", ".pyo"}
            or path.name == ".DS_Store"
        ):
            continue
        digest.update(relative.as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def _skill_support_files(root: Path) -> list[tuple[str, Path]]:
    if not root.is_dir():
        return []
    result: list[tuple[str, Path]] = []
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root)
        if (
            not path.is_file()
            or path.is_symlink()
            or relative == Path("SKILL.md")
            or "__pycache__" in relative.parts
            or path.suffix in {".pyc", ".pyo"}
            or path.name == ".DS_Store"
            or path.name.endswith((".copy.tmp", ".sync.tmp", ".tmp"))
        ):
            continue
        result.append((relative.as_posix(), path))
    return result


def _atomic_copy(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.sync.tmp")
    shutil.copy2(source, temporary)
    temporary.replace(target)


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
