#!/usr/bin/env python3
"""AXIOM miniapp discovery, decompile orchestration, and static extraction.

This script is intentionally model-friendly: workers can run one command and get
stable artifacts that guide Reqable-backed traffic verification instead of
guessing headers, paths, or parameters.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import string
import struct
import sys
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlparse

TEXT_EXTENSIONS = {
    ".js", ".json", ".wxml", ".wxss", ".html", ".htm", ".txt", ".map",
    ".ts", ".vue", ".css", ".xml", ".config", ".conf", ".md", ".yaml", ".yml",
}
MEDIA_EXTENSIONS = {
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".bmp", ".ico",
    ".mp3", ".wav", ".m4a", ".aac", ".ogg", ".mp4", ".mov", ".avi", ".webm",
    ".ttf", ".otf", ".woff", ".woff2", ".eot",
}
IGNORE_DIRS = {"node_modules", ".git", "__MACOSX", ".idea", ".vscode"}
MAX_FILE_BYTES = 2_500_000
DEFAULT_OUT_ROOT = Path(os.environ.get("AXIOM_MINIAPP_OUT_DIR", "output/miniapp"))
SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_RULES = SCRIPT_DIR / "miniapp_regex_rules.json"

DEFAULT_DISCOVERY_ROOTS = [
    Path("/tmp/axiom-files"),
    Path.cwd(),
    Path.home() / "Library/Containers/com.tencent.xinWeChat/Data/.wxapplet/packages",
    Path.home() / "Library/Containers/com.tencent.xinWeChat/Data/Documents/app_data/radium/Applet/packages",
    Path.home() / "Library/Containers/com.tencent.xinWeChat/Data/Documents/app_data/radium/users",
    Path.home() / "Library/Containers/com.tencent.xinWeChat/Data/Library/Application Support/com.tencent.xinWeChat",
    Path.home() / "Library/Application Support/com.tencent.xinWeChat",
    Path.home() / "Library/Application Support/微信开发者工具/User Data/Default/Weapp",
    Path.home() / "Library/Application Support/微信开发者工具/User Data/Default/WeappPackage",
]

URL_RE = re.compile(r"https?://[^\s'\"`<>\\)\]}]+", re.I)
HOST_RE = re.compile(r"(?<![\w.-])(?:[a-z0-9-]+\.)+[a-z]{2,}(?::\d{2,5})?(?![\w.-])", re.I)
WX_REQUEST_RE = re.compile(r"wx\.request\s*\(|\.request\s*\(|request\s*:\s*function", re.I)
CLIENT_APP_CODE_RE = re.compile(r"(?:client/app[^'\"`\s]*[?&]code=|code=)([A-Za-z][A-Za-z0-9_]*(?:/[A-Za-z0-9_.-]+)+)", re.I)
STRING_CODE_RE = re.compile(r"['\"]([A-Za-z][A-Za-z0-9_]*(?:/[A-Za-z0-9_.-]+)+)['\"]")
STORAGE_RE = re.compile(r"wx\.(?:get|set|remove)Storage(?:Sync)?\s*\(\s*['\"]([^'\"]+)['\"]", re.I)
FALSE_HOST_TLDS = {
    "request", "stringify", "parse", "getstoragesync", "setstoragesync", "removestoragesync",
    "requestpayment", "login", "navigateto", "redirectto", "switchtab", "then", "catch",
    "map", "filter", "reduce", "push", "split", "join", "indexof", "length",
    "png", "jpg", "jpeg", "gif", "webp", "svg", "bmp", "ico", "mp3", "wav", "m4a",
    "mp4", "mov", "avi", "webm", "ttf", "otf", "woff", "woff2", "eot",
}
FALSE_HOST_PREFIXES = ("wx.", "json.", "console.", "this.", "that.", "app.", "page.")
COMMON_HOST_TLDS = {
    "com", "cn", "net", "org", "io", "co", "gov", "edu", "app", "dev", "me", "top",
    "xyz", "shop", "store", "cloud", "vip", "cc", "info", "biz", "online", "site",
    "tech", "ai", "hk", "tw", "sg", "jp", "kr", "uk", "us", "de", "fr", "ru",
}


@dataclass
class MiniappCandidate:
    id: str
    kind: str
    source_path: str
    name: str = ""
    appid: str = ""
    version: str = ""
    hash: str = ""
    package_files: list[str] = field(default_factory=list)
    decompiled_path: str = ""
    confidence: int = 0
    evidence: list[str] = field(default_factory=list)


@dataclass
class RegexFinding:
    rule_id: str
    category: str
    severity: str
    value: str
    file: str
    line: int
    context: str
    description: str = ""


@dataclass
class ApiCandidate:
    kind: str
    value: str
    file: str
    line: int
    host: str = ""
    context: str = ""


@dataclass
class MediaResource:
    kind: str
    path: str
    size: int
    extension: str


@dataclass
class MiniappIdentity:
    candidate_id: str
    kind: str
    appid: str
    version: str
    name: str
    display_name: str
    description: str
    developer: str
    source_path: str
    package_files: list[str]
    decompiled_path: str
    confidence: int
    already_decompiled: bool
    metadata_source: list[str]
    page_titles: list[str]
    hosts: list[str]
    urls: list[str]
    business_codes: list[str]
    notes: list[str]


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def read_text(path: Path) -> str:
    try:
        data = path.read_bytes()
    except OSError:
        return ""
    if len(data) > MAX_FILE_BYTES:
        data = data[:MAX_FILE_BYTES]
    for encoding in ("utf-8", "utf-16", "gb18030"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="ignore")


def safe_rel(path: Path, root: Path) -> str:
    try:
        return str(path.relative_to(root))
    except ValueError:
        return str(path)


def slugify(value: str, fallback: str = "miniapp") -> str:
    text = re.sub(r"[^A-Za-z0-9_.-]+", "-", value.strip()).strip(".-")
    return text[:80] or fallback


def context_line(line: str, max_len: int = 260) -> str:
    text = " ".join(line.strip().split())
    return text if len(text) <= max_len else text[: max_len - 3] + "..."


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def tree_fingerprint(root: Path) -> str:
    digest = hashlib.sha256()
    files = []
    for path in root.rglob("*"):
        if not path.is_file() or any(part in IGNORE_DIRS for part in path.parts):
            continue
        if not (is_text_candidate(path) or path.suffix.lower() in MEDIA_EXTENSIONS or path.suffix.lower() == ".wxapkg"):
            continue
        files.append(path)
    for path in sorted(files)[:5000]:
        try:
            stat = path.stat()
        except OSError:
            continue
        digest.update(safe_rel(path, root).encode("utf-8", errors="ignore"))
        digest.update(str(stat.st_size).encode())
        if stat.st_size <= MAX_FILE_BYTES:
            digest.update(path.read_bytes())
    return digest.hexdigest()


def is_text_candidate(path: Path) -> bool:
    return path.suffix.lower() in TEXT_EXTENSIONS or path.name in {"app.json", "project.config.json", "sitemap.json", "app-service.js"}


def iter_files(root: Path) -> Iterable[Path]:
    for path in root.rglob("*"):
        if path.is_file() and not any(part in IGNORE_DIRS for part in path.parts):
            yield path


def iter_source_files(root: Path) -> Iterable[Path]:
    for path in iter_files(root):
        if is_text_candidate(path):
            yield path


def printable_strings(path: Path, min_len: int = 5) -> list[str]:
    data = path.read_bytes()
    allowed = set(bytes(string.printable, "ascii"))
    out: list[str] = []
    buf = bytearray()
    for byte in data:
        if byte in allowed and byte not in {0x0b, 0x0c}:
            buf.append(byte)
        else:
            if len(buf) >= min_len:
                out.append(buf.decode("utf-8", errors="ignore"))
            buf.clear()
    if len(buf) >= min_len:
        out.append(buf.decode("utf-8", errors="ignore"))
    return out


def load_profile(path: str | None) -> dict[str, Any]:
    if not path:
        return {}
    profile_path = Path(path).expanduser()
    if not profile_path.exists():
        raise SystemExit(f"profile not found: {profile_path}")
    return read_json(profile_path)


def load_rules(path: str | None) -> list[dict[str, Any]]:
    rule_path = Path(path).expanduser() if path else DEFAULT_RULES
    if not rule_path.exists():
        raise SystemExit(f"regex rule file not found: {rule_path}")
    raw = read_json(rule_path)
    rules = raw.get("rules", raw) if isinstance(raw, dict) else raw
    if not isinstance(rules, list):
        raise SystemExit(f"invalid regex rules: {rule_path}")
    for rule in rules:
        try:
            re.compile(str(rule["regex"]), re.I)
        except Exception as exc:
            raise SystemExit(f"invalid regex rule {rule.get('id')}: {exc}") from exc
    return rules


def parse_json_maybe(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        data = read_json(path)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def extract_manifest_from_dir(root: Path) -> tuple[str, str, str, list[str]]:
    evidence: list[str] = []
    appid = ""
    name = ""
    version = ""
    for cfg_name in ("project.config.json", "project.private.config.json"):
        cfg = parse_json_maybe(root / cfg_name)
        if cfg:
            evidence.append(cfg_name)
            appid = appid or str(cfg.get("appid") or cfg.get("appId") or "")
            name = name or str(cfg.get("projectname") or cfg.get("name") or root.name)
            version = version or str(cfg.get("version") or "")
    app_json = parse_json_maybe(root / "app.json")
    if app_json:
        evidence.append("app.json")
        if not name and isinstance(app_json.get("window"), dict):
            name = str(app_json["window"].get("navigationBarTitleText") or "")
        if isinstance(app_json.get("pages"), list):
            evidence.append(f"pages:{len(app_json['pages'])}")
    for file_name in ("app-service.js", "app.js"):
        file = root / file_name
        if file.exists():
            evidence.append(file_name)
            text = read_text(file)
            if not appid:
                match = re.search(r"wx[a-f0-9]{16}", text, re.I)
                if match:
                    appid = match.group(0)
    if not name:
        name = root.name
    return appid, name, version, evidence


def package_appid_hint(path: Path) -> str:
    text = "/".join(path.parts[-8:])
    match = re.search(r"wx[a-f0-9]{16}", text, re.I)
    return match.group(0) if match else ""


def candidate_id(kind: str, source: Path, appid: str, digest: str) -> str:
    base = appid or source.stem or source.name
    suffix = digest[:8] if digest else hashlib.sha1(str(source).encode()).hexdigest()[:8]
    return f"{kind}:{slugify(base)}:{suffix}"


def inspect_candidate(path: Path) -> MiniappCandidate | None:
    try:
        resolved = path.expanduser().resolve()
    except OSError:
        return None
    if not resolved.exists():
        return None
    if resolved.is_file() and resolved.suffix.lower() == ".wxapkg":
        digest = sha256_file(resolved)
        appid = package_appid_hint(resolved)
        return MiniappCandidate(
            id=candidate_id("package", resolved, appid, digest),
            kind="package",
            source_path=str(resolved),
            name=resolved.stem,
            appid=appid,
            hash=digest,
            package_files=[str(resolved)],
            confidence=80 if appid else 60,
            evidence=["wxapkg"],
        )
    if not resolved.is_dir():
        return None

    appid, name, version, evidence = extract_manifest_from_dir(resolved)
    wxapkgs = sorted(str(item) for item in resolved.rglob("*.wxapkg") if item.is_file())[:100]
    has_sources = bool(evidence) or any((resolved / item).exists() for item in ("pages", "components", "utils"))
    if has_sources:
        digest = tree_fingerprint(resolved)
        return MiniappCandidate(
            id=candidate_id("decompiled", resolved, appid, digest),
            kind="decompiled",
            source_path=str(resolved),
            name=name,
            appid=appid,
            version=version,
            hash=digest,
            package_files=wxapkgs,
            decompiled_path=str(resolved),
            confidence=95 if appid else 75,
            evidence=evidence or ["source-like-dir"],
        )
    if wxapkgs:
        digest = hashlib.sha256("\n".join(wxapkgs).encode()).hexdigest()
        appid_hint = package_appid_hint(resolved)
        return MiniappCandidate(
            id=candidate_id("package-dir", resolved, appid_hint, digest),
            kind="package_dir",
            source_path=str(resolved),
            name=resolved.name,
            appid=appid_hint,
            hash=digest,
            package_files=wxapkgs,
            confidence=70,
            evidence=[f"wxapkg:{len(wxapkgs)}"],
        )
    return None


def expand_roots(roots: list[str] | None) -> list[Path]:
    values = [Path(item).expanduser() for item in roots or []]
    if not values:
        values = DEFAULT_DISCOVERY_ROOTS
    expanded: list[Path] = []
    for root in values:
        if not root.exists():
            continue
        expanded.append(root)
        if root.is_dir() and "com.tencent.xinWeChat" in str(root):
            expanded.extend(path for path in root.glob("**/WeApp/**") if path.is_dir())
    seen = set()
    out = []
    for item in expanded:
        try:
            resolved = item.resolve()
        except OSError:
            continue
        if resolved in seen:
            continue
        seen.add(resolved)
        out.append(resolved)
    return out


def discover_miniapps(roots: list[str] | None = None, max_depth: int = 8, limit: int = 200) -> list[MiniappCandidate]:
    candidates: dict[str, MiniappCandidate] = {}
    for root in expand_roots(roots):
        direct = inspect_candidate(root)
        if direct:
            candidates[direct.id] = direct
        if not root.is_dir():
            continue
        base_depth = len(root.parts)
        for path in root.rglob("*"):
            if len(path.parts) - base_depth > max_depth:
                continue
            if any(part in IGNORE_DIRS for part in path.parts):
                continue
            if path.is_file() and path.suffix.lower() != ".wxapkg":
                continue
            if path.is_dir() and not (
                (path / "app.json").exists()
                or (path / "project.config.json").exists()
                or (path / "app-service.js").exists()
                or any(path.glob("*.wxapkg"))
            ):
                continue
            item = inspect_candidate(path)
            if item:
                candidates[item.id] = item
                if len(candidates) >= limit:
                    return sort_candidates(candidates.values())
    return sort_candidates(candidates.values())


def sort_candidates(candidates: Iterable[MiniappCandidate]) -> list[MiniappCandidate]:
    return sorted(candidates, key=lambda item: (-item.confidence, item.appid or item.name, item.source_path))


def manifest_matches(out_dir: Path, source_hash: str) -> bool:
    manifest = out_dir / "manifest.json"
    if not manifest.exists() or not source_hash:
        return False
    try:
        data = read_json(manifest)
    except Exception:
        return False
    return data.get("hash") == source_hash and (out_dir / "decompiled").exists()


def copy_source_dir(source: Path, dest: Path) -> None:
    if dest.exists():
        shutil.rmtree(dest)
    ignore = shutil.ignore_patterns(".git", "node_modules", "__MACOSX")
    shutil.copytree(source, dest, ignore=ignore)


def is_decrypted_wxapkg(data: bytes) -> bool:
    return len(data) >= 14 and data[0] == 0xBE and data[13] == 0xED


def decrypt_wxapkg_data(data: bytes, wxid: str) -> bytes:
    """Decrypt a PC WeChat wxapkg using the common wxid-derived scheme.

    This mirrors the public algorithm used by open miniapp tools: PBKDF2-HMAC-SHA1
    over the wxid, AES-CBC for the first encrypted block, then XOR for the tail.
    """
    if not re.fullmatch(r"wx[0-9a-fA-F]{16}", wxid or ""):
        raise ValueError("missing valid wxid/appid for encrypted wxapkg")
    if len(data) < 1030:
        raise ValueError("encrypted wxapkg is too small")
    try:
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    except Exception as exc:  # pragma: no cover - dependency is declared by AXIOM
        raise ValueError("cryptography package is required for encrypted wxapkg") from exc

    key = hashlib.pbkdf2_hmac("sha1", wxid.encode(), b"saltiest", 1000, 32)
    cipher = Cipher(algorithms.AES(key), modes.CBC(b"the iv: 16 bytes"))
    decryptor = cipher.decryptor()
    first_block = decryptor.update(data[6:1030]) + decryptor.finalize()
    xor_key = wxid.encode()[-2] if len(wxid) >= 2 else 0x66
    tail = bytes(byte ^ xor_key for byte in data[1030:])
    return first_block[:1023] + tail


@dataclass
class WxapkgEntry:
    name: str
    offset: int
    size: int


def parse_wxapkg_entries(data: bytes) -> list[WxapkgEntry]:
    if not is_decrypted_wxapkg(data):
        raise ValueError("wxapkg header is not decrypted or is invalid")
    if len(data) < 18:
        raise ValueError("wxapkg is too small")
    first_mark, _info1, index_len, _body_len, last_mark = struct.unpack_from(">BIIIB", data, 0)
    if first_mark != 0xBE or last_mark != 0xED:
        raise ValueError("wxapkg magic marker mismatch")
    file_count = struct.unpack_from(">I", data, 14)[0]
    if file_count > 102400:
        raise ValueError(f"wxapkg file count too large: {file_count}")
    pos = 18
    index_end = 18 + index_len
    alternate_index_end = 14 + index_len
    max_index_end = max(index_end, alternate_index_end)
    if max_index_end > len(data):
        raise ValueError("wxapkg index exceeds package length")
    entries: list[WxapkgEntry] = []
    for _ in range(file_count):
        if pos + 4 > index_end:
            raise ValueError("wxapkg index truncated at name length")
        name_len = struct.unpack_from(">I", data, pos)[0]
        pos += 4
        if name_len == 0 or name_len > 4096 or pos + name_len + 8 > max_index_end:
            raise ValueError(f"invalid wxapkg filename length: {name_len}")
        raw_name = data[pos : pos + name_len]
        pos += name_len
        offset, size = struct.unpack_from(">II", data, pos)
        pos += 8
        if offset + size > len(data):
            raise ValueError(f"wxapkg file range exceeds package length: {raw_name!r}")
        name = raw_name.decode("utf-8", errors="replace")
        entries.append(WxapkgEntry(name=name, offset=int(offset), size=int(size)))
    if pos not in {index_end, alternate_index_end}:
        raise ValueError(f"wxapkg index length mismatch: read={pos}, expected={index_end} or {alternate_index_end}")
    return entries


def safe_extract_path(root: Path, package_name: str) -> Path | None:
    normalized = package_name.replace("\\", "/").lstrip("/")
    if not normalized or normalized.endswith("/"):
        return None
    parts = Path(normalized).parts
    if any(part in {"", ".", ".."} for part in parts):
        return None
    target = (root / Path(*parts)).resolve()
    root_resolved = root.resolve()
    if target == root_resolved or root_resolved not in target.parents:
        return None
    return target


def unpack_wxapkg_file(package: Path, dest: Path, wxid: str = "") -> tuple[bool, list[str], int]:
    notes: list[str] = []
    try:
        data = package.read_bytes()
    except OSError as exc:
        return False, [f"failed to read {package.name}: {exc}"], 0
    original_decrypted = is_decrypted_wxapkg(data)
    if not original_decrypted:
        try:
            data = decrypt_wxapkg_data(data, wxid)
            notes.append(f"decrypted wxapkg {package.name} using wxid/appid {wxid}")
        except Exception as exc:
            return False, [f"wxapkg unpack skipped for {package.name}: encrypted or invalid package ({exc})"], 0
    try:
        entries = parse_wxapkg_entries(data)
    except Exception as exc:
        return False, [f"wxapkg unpack failed for {package.name}: {exc}"], 0

    written = 0
    skipped = 0
    for entry in entries:
        target = safe_extract_path(dest, entry.name)
        if target is None:
            skipped += 1
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data[entry.offset : entry.offset + entry.size])
        written += 1
    state = "decrypted+unpacked" if not original_decrypted else "unpacked"
    notes.append(f"{state} {package.name}: {written} files written, {skipped} unsafe entries skipped")
    return written > 0, notes, written


def preview_wxapkg_file(package: Path, wxid: str = "", max_files: int = 80) -> tuple[dict[str, str], list[str]]:
    notes: list[str] = []
    try:
        data = package.read_bytes()
    except OSError as exc:
        return {}, [f"failed to read {package.name}: {exc}"]
    if not is_decrypted_wxapkg(data):
        try:
            data = decrypt_wxapkg_data(data, wxid)
            notes.append(f"decrypted preview {package.name} using wxid/appid {wxid}")
        except Exception as exc:
            return {}, [f"preview skipped for {package.name}: encrypted or invalid package ({exc})"]
    try:
        entries = parse_wxapkg_entries(data)
    except Exception as exc:
        return {}, [f"preview unpack index failed for {package.name}: {exc}"]

    wanted: dict[str, str] = {}
    priority_names = {
        "app-config.json", "app.json", "ext.json", "project.config.json", "plugin.json",
        "app-service.js", "common.app.js", "config/config.js", "utils/request.js",
    }
    selected: list[WxapkgEntry] = []
    for entry in entries:
        normalized = entry.name.replace("\\", "/").lstrip("/")
        if normalized in priority_names or normalized.endswith(("app-config.json", "app.json", "ext.json", "project.config.json", "plugin.json")):
            selected.append(entry)
    for entry in entries:
        if len(selected) >= max_files:
            break
        normalized = entry.name.replace("\\", "/").lstrip("/")
        if entry in selected:
            continue
        if normalized.endswith(('.js', '.json')) and any(key in normalized.lower() for key in ("config", "request", "api", "service", "app")):
            selected.append(entry)
    for entry in selected[:max_files]:
        normalized = entry.name.replace("\\", "/").lstrip("/")
        raw = data[entry.offset : entry.offset + min(entry.size, MAX_FILE_BYTES)]
        try:
            wanted[normalized] = raw.decode("utf-8")
        except UnicodeDecodeError:
            wanted[normalized] = raw.decode("utf-8", errors="ignore")
    notes.append(f"previewed {package.name}: {len(wanted)} metadata/source files from {len(entries)} entries")
    return wanted, notes


def unique_append(values: list[str], value: str, limit: int = 100) -> None:
    value = value.strip()
    if value and value not in values and len(values) < limit:
        values.append(value)


def collect_page_titles_from_config(data: dict[str, Any]) -> list[str]:
    titles: list[str] = []
    window = data.get("window")
    if isinstance(window, dict):
        unique_append(titles, str(window.get("navigationBarTitleText") or ""), 80)
    page = data.get("page")
    if isinstance(page, dict):
        for page_config in page.values():
            if isinstance(page_config, dict):
                win = page_config.get("window")
                if isinstance(win, dict):
                    unique_append(titles, str(win.get("navigationBarTitleText") or ""), 80)
    tab_bar = data.get("tabBar") or data.get("tabbar")
    if isinstance(tab_bar, dict):
        items = tab_bar.get("list")
        if isinstance(items, list):
            for item in items:
                if isinstance(item, dict):
                    unique_append(titles, str(item.get("text") or ""), 80)
    return titles


def infer_display_name(candidate: MiniappCandidate, texts: dict[str, str], page_titles: list[str]) -> tuple[str, list[str]]:
    sources: list[str] = []
    for file_name in ("project.config.json", "app.json", "app-config.json"):
        text = texts.get(file_name)
        if not text:
            continue
        try:
            data = json.loads(text)
        except Exception:
            continue
        for key in ("projectname", "name", "appName", "appname", "nickName", "nickname"):
            value = data.get(key) if isinstance(data, dict) else None
            if isinstance(value, str) and value.strip():
                sources.append(f"{file_name}.{key}")
                return value.strip(), sources
    brand_titles = [title for title in page_titles if re.fullmatch(r"[A-Z][A-Z0-9-]{1,15}", title)]
    if brand_titles:
        sources.append("page.window.navigationBarTitleText.brand_like")
        return Counter(brand_titles).most_common(1)[0][0], sources
    meaningful_titles = [title for title in page_titles if title and title not in {"首页", "我的", "分类", "商城", "搜索"}]
    if meaningful_titles:
        counter = Counter(page_titles)
        best = sorted(meaningful_titles, key=lambda item: (-counter[item], len(item)))[0]
        sources.append("page.window.navigationBarTitleText")
        return best, sources
    if page_titles:
        sources.append("page.window.navigationBarTitleText")
        return page_titles[0], sources
    return candidate.name, sources


def collect_identity_from_texts(texts: dict[str, str], profile: dict[str, Any] | None = None) -> dict[str, Any]:
    hosts: list[str] = []
    urls: list[str] = []
    codes: list[str] = []
    page_titles: list[str] = []
    descriptions: list[str] = []
    developers: list[str] = []
    for file_name, text in texts.items():
        if file_name.endswith(".json"):
            try:
                data = json.loads(text)
            except Exception:
                data = None
            if isinstance(data, dict):
                for title in collect_page_titles_from_config(data):
                    unique_append(page_titles, title, 120)
                for key in ("description", "desc", "intro", "signature"):
                    value = data.get(key)
                    if isinstance(value, str):
                        unique_append(descriptions, value, 20)
                for key in ("developer", "company", "subject", "principal", "serviceProvider"):
                    value = data.get(key)
                    if isinstance(value, str):
                        unique_append(developers, value, 20)
        for line in text.splitlines():
            for url in URL_RE.findall(line):
                unique_append(urls, url, 120)
                host = host_from_value(url)
                if host and is_probable_host(host) and not is_noise_host(host):
                    unique_append(hosts, host, 80)
            for code in CLIENT_APP_CODE_RE.findall(line) + STRING_CODE_RE.findall(line):
                if is_business_api_code(code):
                    unique_append(codes, code, 160)
    if profile:
        for host in profile.get("hosts", []) if isinstance(profile.get("hosts"), list) else []:
            if isinstance(host, str):
                unique_append(hosts, host, 80)
    return {
        "hosts": hosts,
        "urls": urls,
        "business_codes": codes,
        "page_titles": page_titles,
        "description": descriptions[0] if descriptions else "",
        "developer": developers[0] if developers else "",
    }


def identity_for_candidate(candidate: MiniappCandidate, profile: dict[str, Any] | None = None) -> MiniappIdentity:
    notes: list[str] = []
    texts: dict[str, str] = {}
    metadata_source: list[str] = []
    already_decompiled = candidate.kind == "decompiled"
    if already_decompiled:
        root = Path(candidate.decompiled_path or candidate.source_path)
        for name in ("app-config.json", "app.json", "project.config.json", "ext.json", "plugin.json", "app-service.js"):
            path = root / name
            if path.exists() and path.is_file():
                texts[name] = read_text(path)
                metadata_source.append(str(path))
        notes.append("used existing decompiled/source-like directory for identity")
    else:
        packages = [Path(item) for item in candidate.package_files]
        if not packages and Path(candidate.source_path).suffix.lower() == ".wxapkg":
            packages = [Path(candidate.source_path)]
        packages = sorted(packages, key=lambda item: (0 if item.name == "__APP__.wxapkg" else 1, item.name))[:3]
        for package in packages:
            preview, package_notes = preview_wxapkg_file(package, candidate.appid or package_appid_hint(package))
            notes.extend(package_notes)
            for key, value in preview.items():
                texts.setdefault(key, value)
            if preview:
                metadata_source.append(str(package))
    collected = collect_identity_from_texts(texts, profile)
    display_name, name_sources = infer_display_name(candidate, texts, collected["page_titles"])
    metadata_source.extend(name_sources)
    confidence = candidate.confidence
    if collected["hosts"]:
        confidence += 8
    if collected["business_codes"]:
        confidence += 8
    if display_name and display_name != candidate.name:
        confidence += 5
    if not collected["developer"]:
        notes.append("developer is not present in local package metadata; needs external appid lookup if required")
    if not collected["description"]:
        notes.append("description is not present in local package metadata; inferred identity uses titles/domains/API clues")
    return MiniappIdentity(
        candidate_id=candidate.id,
        kind=candidate.kind,
        appid=candidate.appid,
        version=candidate.version,
        name=candidate.name,
        display_name=display_name,
        description=collected["description"],
        developer=collected["developer"],
        source_path=candidate.source_path,
        package_files=candidate.package_files,
        decompiled_path=candidate.decompiled_path,
        confidence=min(confidence, 100),
        already_decompiled=already_decompiled,
        metadata_source=metadata_source,
        page_titles=collected["page_titles"],
        hosts=collected["hosts"],
        urls=collected["urls"],
        business_codes=collected["business_codes"],
        notes=notes,
    )


def write_identity_markdown(path: Path, identities: list[MiniappIdentity]) -> None:
    lines = ["# Miniapp Identity Report", ""]
    for index, item in enumerate(identities, 1):
        lines.extend([
            f"## {index}. {item.display_name or item.name or item.appid or item.kind}",
            f"- AppID: `{item.appid}`",
            f"- Kind: `{item.kind}`",
            f"- Already decompiled: `{item.already_decompiled}`",
            f"- Confidence: `{item.confidence}`",
            f"- Source: `{item.source_path}`",
            f"- Description: `{item.description or 'not available offline'}`",
            f"- Developer: `{item.developer or 'not available offline'}`",
            "- Hosts: " + (", ".join(f"`{host}`" for host in item.hosts[:12]) or "none"),
            "- Page titles: " + (", ".join(f"`{title}`" for title in item.page_titles[:16]) or "none"),
            "- Business codes: " + (", ".join(f"`{code}`" for code in item.business_codes[:20]) or "none"),
            "",
        ])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")


def render_strings_fallback(packages: list[Path], dest: Path) -> list[str]:
    notes: list[str] = []
    dest.mkdir(parents=True, exist_ok=True)
    strings_dir = dest / "__strings__"
    raw_dir = dest.parent / "raw"
    strings_dir.mkdir(parents=True, exist_ok=True)
    raw_dir.mkdir(parents=True, exist_ok=True)
    for pkg in packages:
        shutil.copy2(pkg, raw_dir / pkg.name)
        strings = printable_strings(pkg)
        target = strings_dir / f"{pkg.stem}.strings.txt"
        target.write_text("\n".join(strings) + "\n", encoding="utf-8", errors="ignore")
        notes.append(f"fallback strings extracted from {pkg.name}: {len(strings)} strings")
    return notes


def decompile_input(source: Path, out_dir: Path, force: bool = False) -> tuple[Path, dict[str, Any], list[str]]:
    source = source.expanduser().resolve()
    out_dir = out_dir.expanduser().resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    candidate = inspect_candidate(source)
    if not candidate:
        raise SystemExit(f"not a miniapp package or decompiled directory: {source}")
    decompiled = out_dir / "decompiled"
    notes: list[str] = []
    source_hash = candidate.hash
    reused = False
    engine = "source-copy"

    if not force and manifest_matches(out_dir, source_hash):
        reused = True
        notes.append("reused existing decompiled directory because source hash matches manifest")
    elif candidate.kind == "decompiled":
        copy_source_dir(source, decompiled)
        notes.append("copied existing decompiled/source-like directory")
    else:
        if decompiled.exists():
            shutil.rmtree(decompiled)
        decompiled.mkdir(parents=True, exist_ok=True)
        packages = [Path(item) for item in candidate.package_files] or ([source] if source.suffix.lower() == ".wxapkg" else [])
        unpacked_count = 0
        failed_packages: list[Path] = []
        for package in packages:
            ok, package_notes, written = unpack_wxapkg_file(package, decompiled, candidate.appid or package_appid_hint(package))
            notes.extend(package_notes)
            if ok:
                unpacked_count += written
            else:
                failed_packages.append(package)
        if unpacked_count:
            engine = "wxapkg-unpack"
            if failed_packages:
                notes.extend(render_strings_fallback(failed_packages, decompiled))
                notes.append(f"strings fallback used for {len(failed_packages)} package(s) that could not be unpacked")
        else:
            engine = "strings-fallback"
            notes.extend(render_strings_fallback(packages, decompiled))

    appid, name, version, evidence = extract_manifest_from_dir(decompiled)
    manifest = {
        "appid": appid or candidate.appid,
        "name": name if name != "decompiled" else candidate.name,
        "version": version or candidate.version,
        "source_path": str(source),
        "hash": source_hash,
        "kind": candidate.kind,
        "package_files": candidate.package_files,
        "decompiled_path": str(decompiled),
        "engine": engine,
        "reused": reused,
        "evidence": evidence or candidate.evidence,
        "notes": notes,
    }
    write_json(out_dir / "manifest.json", manifest)
    return decompiled, manifest, notes


def host_from_value(value: str) -> str:
    if value.startswith(("http://", "https://")):
        try:
            return urlparse(value).netloc.lower()
        except ValueError:
            return ""
    if "." in value and "/" not in value:
        return value.lower()
    return ""


def is_probable_host(value: str, profile_hosts: set[str] | None = None) -> bool:
    host = value.lower().strip().strip(".,;:)]}")
    if not host or "." not in host:
        return False
    if profile_hosts and (host in profile_hosts or any(host.endswith("." + item) for item in profile_hosts)):
        return True
    if host.startswith(FALSE_HOST_PREFIXES):
        return False
    tld = host.rsplit(".", 1)[-1]
    if tld in FALSE_HOST_TLDS or len(tld) > 12:
        return False
    if tld not in COMMON_HOST_TLDS:
        return False
    return True


def is_business_api_code(value: str) -> bool:
    lowered = value.strip().strip("'\"").lower()
    if not lowered or "/" not in lowered:
        return False
    first_segment = lowered.split("/", 1)[0]
    if first_segment in {"text", "icon", "icons", "image", "images", "font", "fonts", "style", "styles"}:
        return False
    route_prefixes = (
        "pages/", "page/", "components/", "component/", "views/", "view/", "assets/",
        "images/", "image/", "static/", "utils/", "common/", "subpackages/", "subpackage/", "package/",
        "miniprogram_npm/", "node_modules/", "icons/", "icon/",
    )
    if lowered.startswith(route_prefixes):
        return False
    if "." in lowered.rsplit("/", 1)[-1]:
        return False
    return True


def is_noise_host(host: str) -> bool:
    lowered = host.lower()
    if lowered in {"www.w3.org", "w3.org"}:
        return True
    if lowered.startswith(("2fwww.", "logger.", "t.logger.", "n.logger.", "j.logger.", "t.toast.")):
        return True
    if any(part in lowered for part in (".logger.", ".toast.")):
        return True
    return False


def compile_rules(rules: list[dict[str, Any]]) -> list[tuple[dict[str, Any], re.Pattern[str]]]:
    return [(rule, re.compile(str(rule["regex"]), re.I | re.M)) for rule in rules]


def add_api_candidate(items: list[ApiCandidate], seen: set[tuple[str, str, str, int]], kind: str, value: str, file: Path, root: Path, line_no: int, line: str, host: str = "") -> None:
    value = value.strip().strip("'\"")
    if not value:
        return
    key = (kind, value, str(file), line_no)
    if key in seen:
        return
    seen.add(key)
    items.append(ApiCandidate(kind=kind, value=value, file=safe_rel(file, root), line=line_no, host=host, context=context_line(line)))


def scan_directory(root: Path, profile: dict[str, Any], rules: list[dict[str, Any]]) -> dict[str, Any]:
    profile_hosts = {str(host).lower() for host in profile.get("hosts", []) if isinstance(host, str)}
    compiled_rules = compile_rules(rules)
    regex_findings: list[RegexFinding] = []
    api_candidates: list[ApiCandidate] = []
    media_resources: list[MediaResource] = []
    storage_keys: list[RegexFinding] = []
    host_counter: Counter[str] = Counter()
    category_counter: Counter[str] = Counter()
    seen_regex: set[tuple[str, str, str, int]] = set()
    seen_api: set[tuple[str, str, str, int]] = set()

    for file in iter_files(root):
        suffix = file.suffix.lower()
        if suffix in MEDIA_EXTENSIONS:
            media_resources.append(MediaResource(kind="media", path=safe_rel(file, root), size=file.stat().st_size, extension=suffix))
            continue
        if not is_text_candidate(file):
            continue
        text = read_text(file)
        if not text:
            continue
        rel = safe_rel(file, root)
        for line_no, line in enumerate(text.splitlines(), 1):
            context = context_line(line)
            for rule, regex in compiled_rules:
                for match in regex.finditer(line):
                    value = match.group(1) if match.groups() else match.group(0)
                    value = str(value).strip().strip("'\"` ,;)")
                    if not value or len(value) > 1000:
                        continue
                    category = str(rule.get("category", "misc"))
                    if category == "host" and not is_probable_host(value, profile_hosts):
                        continue
                    if category == "api" and not is_business_api_code(value):
                        continue
                    key = (str(rule.get("id")), value, rel, line_no)
                    if key in seen_regex:
                        continue
                    seen_regex.add(key)
                    category_counter[category] += 1
                    finding = RegexFinding(
                        rule_id=str(rule.get("id")),
                        category=category,
                        severity=str(rule.get("severity", "info")),
                        value=value,
                        file=rel,
                        line=line_no,
                        context=context,
                        description=str(rule.get("description", "")),
                    )
                    regex_findings.append(finding)
                    if category in {"url", "host", "route", "api"}:
                        host = host_from_value(value)
                        if host:
                            host_counter[host] += 1
                        add_api_candidate(api_candidates, seen_api, category, value, file, root, line_no, line, host)
                    if category == "storage":
                        storage_keys.append(finding)
            for url in URL_RE.findall(line):
                host = host_from_value(url)
                if host:
                    host_counter[host] += 1
                add_api_candidate(api_candidates, seen_api, "url", url, file, root, line_no, line, host)
            for host in HOST_RE.findall(line):
                host_l = host.lower()
                if is_probable_host(host_l, profile_hosts):
                    host_counter[host_l] += 1
                    add_api_candidate(api_candidates, seen_api, "host", host_l, file, root, line_no, line, host_l)
            for code in CLIENT_APP_CODE_RE.findall(line) + STRING_CODE_RE.findall(line):
                if len(code) >= 4 and not code.startswith("http") and is_business_api_code(code):
                    add_api_candidate(api_candidates, seen_api, "api_code", code, file, root, line_no, line)
            for match in STORAGE_RE.finditer(line):
                value = match.group(1)
                finding = RegexFinding("wx_storage_key", "storage", "info", value, rel, line_no, context, "WeChat storage key")
                storage_keys.append(finding)
            if WX_REQUEST_RE.search(line):
                add_api_candidate(api_candidates, seen_api, "wx_request", "wx.request", file, root, line_no, line)

    sensitive = [item for item in regex_findings if item.category in {"credential", "secret", "token", "personal", "database", "cloud", "private_key", "payment"}]
    return {
        "regex_findings": regex_findings,
        "api_candidates": api_candidates,
        "sensitive_findings": sensitive,
        "storage_keys": storage_keys,
        "media_resources": media_resources,
        "host_counter": host_counter,
        "category_counter": category_counter,
    }


def business_groups(api_candidates: list[ApiCandidate]) -> dict[str, list[dict[str, Any]]]:
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    mapping = {
        "auth_user": ("User", "Login", "Member", "Account", "Auth", "Phone"),
        "order_pay": ("Order", "Pay", "Payment", "Refund"),
        "cart_goods": ("Cart", "Goods", "Product", "Sku", "Shop"),
        "coupon_point": ("Coupon", "Point", "Integral", "Activity"),
        "address_profile": ("Address", "Profile", "Location"),
    }
    for item in api_candidates:
        value_l = item.value.lower()
        placed = False
        for group, keywords in mapping.items():
            if any(keyword.lower() in value_l for keyword in keywords):
                groups[group].append(asdict(item))
                placed = True
        if not placed and item.kind in {"url", "host"}:
            groups["network"].append(asdict(item))
    return dict(groups)


def write_summary(out_dir: Path, manifest: dict[str, Any], scan: dict[str, Any], notes: list[str]) -> None:
    api_candidates: list[ApiCandidate] = scan["api_candidates"]
    sensitive: list[RegexFinding] = scan["sensitive_findings"]
    media: list[MediaResource] = scan["media_resources"]
    host_counter: Counter[str] = scan["host_counter"]
    category_counter: Counter[str] = scan["category_counter"]
    lines = [
        "# Miniapp Static Analysis Summary",
        "",
        "## Target",
        f"- Name: `{manifest.get('name') or ''}`",
        f"- AppID: `{manifest.get('appid') or ''}`",
        f"- Version: `{manifest.get('version') or ''}`",
        f"- Source: `{manifest.get('source_path') or ''}`",
        f"- Engine: `{manifest.get('engine') or ''}`",
        f"- Reused: `{manifest.get('reused')}`",
        "",
        "## Notes",
    ]
    lines.extend(f"- {note.splitlines()[0][:240]}" for note in notes[:20])
    if not notes:
        lines.append("- No decompile warnings")
    lines.extend(["", "## Top Hosts"])
    filtered_hosts = [(host, count) for host, count in host_counter.most_common(50) if not is_noise_host(host)]
    lines.extend(f"- `{host}`: {count}" for host, count in filtered_hosts[:20])
    if not filtered_hosts:
        lines.append("- No hosts found")
    lines.extend(["", "## Finding Categories"])
    lines.extend(f"- `{category}`: {count}" for category, count in category_counter.most_common())
    if not category_counter:
        lines.append("- No regex findings")
    lines.extend(["", "## First API Candidates"])
    for item in api_candidates[:50]:
        lines.append(f"- `{item.kind}` `{item.value}` at `{item.file}:{item.line}`")
    if not api_candidates:
        lines.append("- No API candidates found")
    lines.extend(["", "## First Sensitive Findings"])
    for item in sensitive[:40]:
        lines.append(f"- `{item.category}` `{item.value}` at `{item.file}:{item.line}`")
    if not sensitive:
        lines.append("- No sensitive findings found")
    lines.extend(["", "## Media Resources", f"- Count: {len(media)}", "", "## Next Step"])
    lines.append(
        "Open `traffic_workflow.md`, use Reqable live capture records to recover a real request, "
        "then replay and mutate that request. Do not guess headers or body fields from static code alone."
    )
    (out_dir / "summary.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_traffic_workflow(out_dir: Path, profile: dict[str, Any], scan: dict[str, Any]) -> None:
    api_candidates: list[ApiCandidate] = scan["api_candidates"]
    host_counter: Counter[str] = scan["host_counter"]
    hosts = [host for host, _ in host_counter.most_common(30) if not is_noise_host(host)]
    for host in profile.get("hosts", []):
        if isinstance(host, str) and host not in hosts:
            hosts.insert(0, host)
    codes: list[str] = []
    paths: list[str] = []
    for item in api_candidates:
        if item.kind == "api_code" and item.value not in codes:
            codes.append(item.value)
        if item.kind == "url" and "/" in item.value and item.value not in paths:
            paths.append(item.value)
    lines = [
        "# Reqable 小程序真实流量验证流程",
        "",
        "静态分析只提供 Host、Path 和业务 Code 线索，不足以证明接口行为或漏洞。",
        "必须优先从 Reqable 取得真实请求，再进行重放和单变量变异。",
        "",
        "## 1. 确认有真实流量",
        "- 先调用 `capture_live_filter` 查询现有记录，不要先凭静态代码构造请求。",
        "- 如果没有目标流量，调用 `capture_live_set_enabled(enabled=true)` 开始捕获，",
        "  然后真实操作小程序触发对应业务流程，再重新查询。",
        "- 不要为了查询流量调用 `proxy_set`；是否修改系统代理应由人工或明确任务决定。",
        "",
        "## 2. 使用静态线索筛选 Reqable 记录",
        "- Host 使用 `capture_live_filter` 的 `host` 过滤器。",
        "- Path、接口 Code 或其他关键字使用 `keyword` 过滤器。",
        "- 先宽后窄；没有结果时分别查询 Host、Path 和业务 Code，不要一次叠加过多条件。",
        "",
        "### Hosts",
    ]
    if hosts:
        lines.extend(f"- `{host}`" for host in hosts[:12])
    else:
        lines.append("- No host candidates")
    lines.append("\n### Paths / URLs")
    if paths:
        lines.extend(f"- `{path}`" for path in paths[:40])
    else:
        lines.append("- No path candidates")
    lines.append("\n### Business Code Keywords")
    if codes:
        lines.extend(f"- `{code}`" for code in codes[:60])
    else:
        lines.append("- No code candidates")
    lines.extend([
        "",
        "## 3. 读取并重放真实请求",
        "1. 对候选记录调用 `capture_live_get_by_id(id)`，检查完整 URL、方法、请求头、Cookie、请求体和响应。",
        "2. 对确认的记录调用 `capture_live_generate_curl(id)`，获取保留真实请求字段的 cURL。",
        "3. Explore Worker 使用 `bash` 执行该 cURL；不要把生成的命令直接当作已验证结果。",
        "4. 每次只修改一个身份、对象 ID、状态、数量或业务参数，并比较响应差异。",
        "5. 保留 Cookie、X-TOKEN、Client、Referer、User-Agent、Content-Type、project/version 和编码方式。",
        "6. 如需在 Reqable 界面继续人工调试，可选用 `capture_live_compose(id)`；这不是自动验证的必需步骤。",
        "",
        "## 4. 结论纪律",
        "- 登录失效、请求头缺失、参数不完整、服务繁忙或空响应，不得记为已测试成功。",
        "- 找不到真实请求时，记录流量缺口并继续触发业务流程，不要猜造 Header 或 Body。",
        "- 静态敏感信息必须结合真实可达接口和实际影响验证后，才能作为漏洞证据。",
    ])
    workflow = "\n".join(lines) + "\n"
    (out_dir / "traffic_workflow.md").write_text(workflow, encoding="utf-8")

    # Keep a non-Yak compatibility pointer for older consumers that still look up
    # this filename. New prompts and payloads use traffic_workflow.md.
    legacy = [
        "# Compatibility pointer",
        "",
        "Reqable is the active traffic backend. Read `traffic_workflow.md`.",
    ]
    (out_dir / "yak_workflow.md").write_text("\n".join(legacy) + "\n", encoding="utf-8")


def analyze_decompiled(root: Path, out_dir: Path, profile: dict[str, Any], rules: list[dict[str, Any]], manifest: dict[str, Any] | None = None, notes: list[str] | None = None) -> dict[str, Any]:
    root = root.expanduser().resolve()
    out_dir = out_dir.expanduser().resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    if manifest is None:
        appid, name, version, evidence = extract_manifest_from_dir(root)
        manifest = {
            "appid": appid,
            "name": name,
            "version": version,
            "source_path": str(root),
            "hash": tree_fingerprint(root),
            "kind": "decompiled",
            "package_files": [],
            "decompiled_path": str(root),
            "engine": "analyze-only",
            "reused": True,
            "evidence": evidence,
            "notes": notes or [],
        }
        write_json(out_dir / "manifest.json", manifest)
    scan = scan_directory(root, profile, rules)
    write_json(out_dir / "regex_findings.json", [asdict(item) for item in scan["regex_findings"]])
    write_json(out_dir / "api_candidates.json", [asdict(item) for item in scan["api_candidates"]])
    write_json(out_dir / "sensitive_findings.json", [asdict(item) for item in scan["sensitive_findings"]])
    write_json(out_dir / "storage_keys.json", [asdict(item) for item in scan["storage_keys"]])
    write_json(out_dir / "media_resources.json", [asdict(item) for item in scan["media_resources"]])
    write_json(out_dir / "business_groups.json", business_groups(scan["api_candidates"]))
    write_summary(out_dir, manifest, scan, notes or manifest.get("notes", []))
    write_traffic_workflow(out_dir, profile, scan)
    return {
        "output_dir": str(out_dir),
        "decompiled_path": str(root),
        "manifest": str(out_dir / "manifest.json"),
        "summary": str(out_dir / "summary.md"),
        "traffic_workflow": str(out_dir / "traffic_workflow.md"),
        "yak_workflow": str(out_dir / "yak_workflow.md"),
        "api_candidates": len(scan["api_candidates"]),
        "regex_findings": len(scan["regex_findings"]),
        "sensitive_findings": len(scan["sensitive_findings"]),
        "media_resources": len(scan["media_resources"]),
        "top_hosts": [(host, count) for host, count in scan["host_counter"].most_common(30) if not is_noise_host(host)][:10],
        "files": {
            "regex_findings": str(out_dir / "regex_findings.json"),
            "api_candidates": str(out_dir / "api_candidates.json"),
            "sensitive_findings": str(out_dir / "sensitive_findings.json"),
            "storage_keys": str(out_dir / "storage_keys.json"),
            "media_resources": str(out_dir / "media_resources.json"),
            "business_groups": str(out_dir / "business_groups.json"),
        },
    }


def default_out_for(source: Path, out_root: Path) -> Path:
    item = inspect_candidate(source)
    name = item.appid or item.name if item else source.stem
    digest = (item.hash if item else hashlib.sha1(str(source).encode()).hexdigest())[:8]
    return out_root / f"{slugify(name)}-{digest}"


def state_paths(out_root: Path) -> dict[str, Path]:
    return {
        "status_json": out_root / "status.json",
        "status_md": out_root / "status.md",
        "probe_json": out_root / "probe.json",
        "probe_md": out_root / "probe.md",
    }


def target_id_for_candidate(candidate: MiniappCandidate) -> str:
    source = Path(candidate.source_path)
    if candidate.kind == "decompiled" and source.name == "decompiled" and (source.parent / "manifest.json").exists():
        return slugify(source.parent.name)
    prefix = "ready" if candidate.kind == "decompiled" else "pkg"
    name = candidate.appid or candidate.name or source.stem or source.name
    version = source.name if candidate.kind == "package_dir" else candidate.version
    parts = [prefix, name]
    if version:
        parts.append(version)
    if candidate.hash:
        parts.append(candidate.hash[:8])
    return slugify("-".join(parts))


def filter_top_level_decompiled(candidates: list[MiniappCandidate]) -> list[MiniappCandidate]:
    decompiled = [item for item in candidates if item.kind == "decompiled"]
    decompiled.sort(key=lambda item: (len(Path(item.source_path).parts), item.source_path))
    kept: list[MiniappCandidate] = []
    kept_paths: list[Path] = []
    for item in decompiled:
        path = Path(item.source_path).resolve()
        if any(parent == path or parent in path.parents for parent in kept_paths):
            continue
        kept.append(item)
        kept_paths.append(path)
    return kept


def filter_package_groups(candidates: list[MiniappCandidate]) -> list[MiniappCandidate]:
    package_dirs = [item for item in candidates if item.kind == "package_dir"]
    package_dir_paths = {Path(item.source_path).resolve() for item in package_dirs}
    packages = [
        item for item in candidates
        if item.kind == "package" and Path(item.source_path).resolve().parent not in package_dir_paths
    ]
    return sorted(package_dirs + packages, key=lambda item: (item.appid or item.name, item.source_path))


def candidate_output_paths(candidate: MiniappCandidate) -> dict[str, str]:
    source = Path(candidate.source_path)
    output_dir = (
        source.parent
        if candidate.kind == "decompiled" and source.name == "decompiled"
        else None
    )
    traffic_workflow = output_dir / "traffic_workflow.md" if output_dir else None
    legacy_workflow = output_dir / "yak_workflow.md" if output_dir else None
    workflow = (
        traffic_workflow
        if traffic_workflow and traffic_workflow.exists()
        else legacy_workflow
    )
    if output_dir and (output_dir / "summary.md").exists() and workflow and workflow.exists():
        return {
            "output_dir": str(output_dir),
            "summary": str(output_dir / "summary.md"),
            "traffic_workflow": str(workflow),
            "yak_workflow": (
                str(legacy_workflow)
                if legacy_workflow and legacy_workflow.exists()
                else ""
            ),
            "api_candidates": str(output_dir / "api_candidates.json"),
            "sensitive_findings": str(output_dir / "sensitive_findings.json"),
        }
    return {
        "output_dir": "",
        "summary": "",
        "traffic_workflow": "",
        "yak_workflow": "",
        "api_candidates": "",
        "sensitive_findings": "",
    }


def ready_target_from_candidate(candidate: MiniappCandidate) -> dict[str, Any]:
    identity = identity_for_candidate(candidate)
    paths = candidate_output_paths(candidate)
    return {
        "target_id": target_id_for_candidate(candidate),
        "display_name": identity.display_name,
        "appid": candidate.appid,
        "kind": candidate.kind,
        "source_path": candidate.source_path,
        "decompiled_path": candidate.decompiled_path or candidate.source_path,
        "is_analyzed": bool(paths["summary"] and paths["traffic_workflow"]),
        **paths,
    }


def package_target_from_candidate(candidate: MiniappCandidate) -> dict[str, Any]:
    source = Path(candidate.source_path)
    main_package = ""
    for package in candidate.package_files:
        if Path(package).name == "__APP__.wxapkg":
            main_package = package
            break
    if not main_package and candidate.package_files:
        main_package = candidate.package_files[0]
    if not main_package and candidate.kind == "package":
        main_package = candidate.source_path
    return {
        "target_id": target_id_for_candidate(candidate),
        "appid": candidate.appid,
        "kind": candidate.kind,
        "version_dir": candidate.source_path if candidate.kind == "package_dir" else str(source.parent),
        "source_path": candidate.source_path,
        "package_count": len(candidate.package_files) or (1 if candidate.kind == "package" else 0),
        "main_package": main_package,
        "package_files": candidate.package_files,
        "hash": candidate.hash,
    }


def build_status(root: list[str] | None, max_depth: int, limit: int, out_root: Path) -> dict[str, Any]:
    candidates = discover_miniapps(root, max_depth, limit)
    ready_targets = [ready_target_from_candidate(item) for item in filter_top_level_decompiled(candidates)]
    package_targets = [package_target_from_candidate(item) for item in filter_package_groups(candidates)]
    next_action = "use_ready_target" if any(item["is_analyzed"] for item in ready_targets) else "run_probe"
    if not ready_targets and not package_targets:
        next_action = "no_targets"
    return {
        "out_root": str(out_root.resolve()),
        "next_action": next_action,
        "ready_targets": ready_targets,
        "package_targets": package_targets,
    }


def write_status_markdown(path: Path, data: dict[str, Any]) -> None:
    lines = ["# Miniapp Status", "", f"- Next action: `{data.get('next_action')}`", ""]
    lines.append("## Ready Targets")
    for item in data.get("ready_targets", []):
        lines.append(f"- `{item['target_id']}` `{item.get('display_name') or item.get('appid')}` analyzed=`{item.get('is_analyzed')}` summary=`{item.get('summary')}`")
    if not data.get("ready_targets"):
        lines.append("- None")
    lines.append("")
    lines.append("## Package Targets")
    for item in data.get("package_targets", []):
        lines.append(f"- `{item['target_id']}` appid=`{item.get('appid')}` packages=`{item.get('package_count')}` source=`{item.get('source_path')}`")
    if not data.get("package_targets"):
        lines.append("- None")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def build_probe(status: dict[str, Any], profile: dict[str, Any] | None = None) -> dict[str, Any]:
    targets: list[dict[str, Any]] = []
    for item in status.get("package_targets", []):
        candidate = inspect_candidate(Path(item["source_path"]))
        if not candidate:
            continue
        identity = identity_for_candidate(candidate, profile)
        identity_data = asdict(identity)
        identity_data["target_id"] = item["target_id"]
        identity_data["package_count"] = item.get("package_count", 0)
        identity_data["version_dir"] = item.get("version_dir", "")
        targets.append(identity_data)
    targets.sort(key=lambda item: (-int(item.get("confidence", 0)), item.get("display_name") or item.get("appid") or item.get("target_id")))
    return {"out_root": status.get("out_root", str(DEFAULT_OUT_ROOT)), "count": len(targets), "targets": targets}


def write_probe_markdown(path: Path, data: dict[str, Any]) -> None:
    lines = ["# Miniapp Probe", ""]
    for index, item in enumerate(data.get("targets", []), 1):
        lines.extend([
            f"## {index}. {item.get('display_name') or item.get('appid') or item.get('target_id')}",
            f"- Target ID: `{item.get('target_id')}`",
            f"- AppID: `{item.get('appid')}`",
            f"- Confidence: `{item.get('confidence')}`",
            f"- Packages: `{item.get('package_count')}`",
            f"- Version dir: `{item.get('version_dir')}`",
            f"- Developer: `{item.get('developer') or 'not available offline'}`",
            f"- Description: `{item.get('description') or 'not available offline'}`",
            "- Hosts: " + (", ".join(f"`{host}`" for host in item.get("hosts", [])[:12]) or "none"),
            "- Page titles: " + (", ".join(f"`{title}`" for title in item.get("page_titles", [])[:16]) or "none"),
            "- Business codes: " + (", ".join(f"`{code}`" for code in item.get("business_codes", [])[:20]) or "none"),
            "",
        ])
    if not data.get("targets"):
        lines.append("- No package targets found")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def load_state_file(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        data = read_json(path)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def find_target_record(target_id: str, out_root: Path, root: list[str] | None, max_depth: int, limit: int, profile: dict[str, Any]) -> dict[str, Any]:
    paths = state_paths(out_root)
    status = load_state_file(paths["status_json"])
    if not status:
        status = build_status(root, max_depth, limit, out_root)
        write_json(paths["status_json"], status)
        write_status_markdown(paths["status_md"], status)
    for item in status.get("ready_targets", []) + status.get("package_targets", []):
        if item.get("target_id") == target_id:
            return item
    probe = load_state_file(paths["probe_json"])
    if not probe:
        probe = build_probe(status, profile)
        write_json(paths["probe_json"], probe)
        write_probe_markdown(paths["probe_md"], probe)
    for item in probe.get("targets", []):
        if item.get("target_id") == target_id:
            return item
    known = [item.get("target_id") for item in status.get("ready_targets", []) + status.get("package_targets", [])]
    known.extend(item.get("target_id") for item in probe.get("targets", []))
    raise SystemExit(f"target_id not found: {target_id}; known targets: {', '.join([str(item) for item in known if item])}")


def print_json(data: Any) -> None:
    print(json.dumps(data, ensure_ascii=False, indent=2))


def cmd_list(args: argparse.Namespace) -> int:
    candidates = discover_miniapps(args.root, args.max_depth, args.limit)
    data = {"count": len(candidates), "miniapps": [asdict(item) for item in candidates]}
    if args.out:
        write_json(Path(args.out).expanduser(), data)
    print_json(data)
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    out_root = Path(args.out_dir).expanduser() if args.out_dir else DEFAULT_OUT_ROOT
    paths = state_paths(out_root)
    data = build_status(args.root, args.max_depth, args.limit, out_root)
    write_json(paths["status_json"], data)
    write_status_markdown(paths["status_md"], data)
    print_json(data)
    return 0


def cmd_probe(args: argparse.Namespace) -> int:
    out_root = Path(args.out_dir).expanduser() if args.out_dir else DEFAULT_OUT_ROOT
    paths = state_paths(out_root)
    profile = load_profile(args.profile)
    status = load_state_file(paths["status_json"])
    if not status or args.refresh:
        status = build_status(args.root, args.max_depth, args.limit, out_root)
        write_json(paths["status_json"], status)
        write_status_markdown(paths["status_md"], status)
    data = build_probe(status, profile)
    write_json(paths["probe_json"], data)
    write_probe_markdown(paths["probe_md"], data)
    print_json(data)
    return 0


def cmd_identify(args: argparse.Namespace) -> int:
    profile = load_profile(args.profile)
    candidates = discover_miniapps(args.root, args.max_depth, args.limit)
    if args.only_unpacked:
        candidates = [item for item in candidates if item.kind != "decompiled"]
    package_dir_paths = {Path(item.source_path).resolve() for item in candidates if item.kind == "package_dir"}
    if package_dir_paths:
        candidates = [
            item for item in candidates
            if item.kind != "package" or Path(item.source_path).resolve().parent not in package_dir_paths
        ]
    identities = [identity_for_candidate(candidate, profile) for candidate in candidates]
    identities.sort(key=lambda item: (-item.confidence, item.display_name or item.name, item.appid, item.source_path))
    data = {"count": len(identities), "miniapps": [asdict(item) for item in identities]}
    if args.out:
        write_json(Path(args.out).expanduser(), data)
    if args.markdown:
        write_identity_markdown(Path(args.markdown).expanduser(), identities)
    print_json(data)
    return 0


def cmd_decompile(args: argparse.Namespace) -> int:
    source = Path(args.input)
    out_dir = Path(args.out).expanduser() if args.out else default_out_for(source.expanduser().resolve(), DEFAULT_OUT_ROOT)
    decompiled, manifest, notes = decompile_input(source, out_dir, args.force)
    print_json({"output_dir": str(out_dir.resolve()), "decompiled_path": str(decompiled), "manifest": manifest, "notes": notes})
    return 0


def cmd_analyze(args: argparse.Namespace) -> int:
    profile = load_profile(args.profile)
    rules = load_rules(args.rules)
    root = Path(args.input)
    out_dir = Path(args.out).expanduser() if args.out else default_out_for(root.expanduser().resolve(), DEFAULT_OUT_ROOT)
    result = analyze_decompiled(root, out_dir, profile, rules)
    print_json(result)
    return 0


def cmd_all(args: argparse.Namespace) -> int:
    profile = load_profile(args.profile)
    rules = load_rules(args.rules)
    source: Path | None = Path(args.input).expanduser().resolve() if args.input else None
    if not source:
        candidates = discover_miniapps(args.root, args.max_depth, args.limit)
        if not candidates:
            raise SystemExit("no miniapp candidates found; pass --input /path/to/pkg.wxapkg_or_decompiled_dir")
        source = Path(candidates[0].source_path)
    out_dir = Path(args.out).expanduser() if args.out else default_out_for(source, DEFAULT_OUT_ROOT)
    decompiled, manifest, notes = decompile_input(source, out_dir, args.force)
    result = analyze_decompiled(decompiled, out_dir, profile, rules, manifest=manifest, notes=notes)
    print_json(result)
    return 0


def cmd_prepare(args: argparse.Namespace) -> int:
    out_root = Path(args.out_dir).expanduser() if args.out_dir else DEFAULT_OUT_ROOT
    profile = load_profile(args.profile)
    rules = load_rules(args.rules)
    target = find_target_record(args.target_id, out_root, args.root, args.max_depth, args.limit, profile)
    source_path = target.get("source_path") or target.get("version_dir") or target.get("decompiled_path")
    if not source_path:
        raise SystemExit(f"target has no source path: {args.target_id}")
    source = Path(str(source_path)).expanduser().resolve()
    output_dir = Path(args.out).expanduser() if args.out else out_root / slugify(args.target_id)
    decompiled, manifest, notes = decompile_input(source, output_dir, args.force)
    result = analyze_decompiled(decompiled, output_dir, profile, rules, manifest=manifest, notes=notes)
    result["target_id"] = args.target_id
    result["target"] = target
    print_json(result)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Discover, decompile, and analyze WeChat miniapps for AXIOM workers")
    sub = parser.add_subparsers(dest="command")

    p_status = sub.add_parser("status", help="scan miniapp state and write output/miniapp/status.*")
    p_status.add_argument("--root", action="append", help="root directory to scan; can be repeated")
    p_status.add_argument("--max-depth", type=int, default=8)
    p_status.add_argument("--limit", type=int, default=300)
    p_status.add_argument("--out-dir", help="state/output root; default output/miniapp")
    p_status.set_defaults(func=cmd_status)

    p_probe = sub.add_parser("probe", help="identify package targets from status without full analysis")
    p_probe.add_argument("--root", action="append", help="root directory to scan when status is missing or --refresh is used")
    p_probe.add_argument("--max-depth", type=int, default=8)
    p_probe.add_argument("--limit", type=int, default=300)
    p_probe.add_argument("--profile", help="optional target profile JSON")
    p_probe.add_argument("--out-dir", help="state/output root; default output/miniapp")
    p_probe.add_argument("--refresh", action="store_true", help="rebuild status before probing")
    p_probe.set_defaults(func=cmd_probe)

    p_prepare = sub.add_parser("prepare", help="prepare a target by target_id from status/probe")
    p_prepare.add_argument("--target-id", required=True)
    p_prepare.add_argument("--root", action="append", help="root directory to scan when state files are missing")
    p_prepare.add_argument("--max-depth", type=int, default=8)
    p_prepare.add_argument("--limit", type=int, default=300)
    p_prepare.add_argument("--profile", help="optional target profile JSON")
    p_prepare.add_argument("--rules", help="regex rule pack JSON")
    p_prepare.add_argument("--out-dir", help="state/output root; default output/miniapp")
    p_prepare.add_argument("--out", help="target output directory; default output/miniapp/<target-id>")
    p_prepare.add_argument("--force", action="store_true", help="ignore reusable output manifest")
    p_prepare.set_defaults(func=cmd_prepare)

    p_list = sub.add_parser("list", help="list miniapp packages and decompiled folders")
    p_list.add_argument("--root", action="append", help="root directory to scan; can be repeated")
    p_list.add_argument("--max-depth", type=int, default=8)
    p_list.add_argument("--limit", type=int, default=200)
    p_list.add_argument("--out", help="optional JSON output file")
    p_list.set_defaults(func=cmd_list)

    p_ident = sub.add_parser("identify", help="identify miniapp packages without full analysis")
    p_ident.add_argument("--root", action="append", help="root directory to scan; can be repeated")
    p_ident.add_argument("--max-depth", type=int, default=8)
    p_ident.add_argument("--limit", type=int, default=200)
    p_ident.add_argument("--profile", help="optional target profile JSON")
    p_ident.add_argument("--only-unpacked", action="store_true", help="skip already decompiled/source-like directories")
    p_ident.add_argument("--out", help="optional JSON output file")
    p_ident.add_argument("--markdown", help="optional Markdown report output file")
    p_ident.set_defaults(func=cmd_identify)

    p_dec = sub.add_parser("decompile", help="decompile or normalize a miniapp package/source directory")
    p_dec.add_argument("--input", required=True)
    p_dec.add_argument("--out", help="output directory; default is /tmp/axiom-miniapp/<name-hash>")
    p_dec.add_argument("--force", action="store_true", help="ignore reusable output manifest")
    p_dec.set_defaults(func=cmd_decompile)

    p_an = sub.add_parser("analyze", help="run regex extraction on a decompiled directory")
    p_an.add_argument("--input", required=True)
    p_an.add_argument("--out", help="output directory")
    p_an.add_argument("--profile", help="optional target profile JSON")
    p_an.add_argument("--rules", help="regex rule pack JSON")
    p_an.set_defaults(func=cmd_analyze)

    p_all = sub.add_parser("all", help="discover/decompile/analyze and produce all worker artifacts")
    p_all.add_argument("--input", help="package or decompiled directory; if omitted, uses best discovered candidate")
    p_all.add_argument("--root", action="append", help="discovery root when --input is omitted; can be repeated")
    p_all.add_argument("--max-depth", type=int, default=8)
    p_all.add_argument("--limit", type=int, default=200)
    p_all.add_argument("--out", help="output directory")
    p_all.add_argument("--profile", help="optional target profile JSON")
    p_all.add_argument("--rules", help="regex rule pack JSON")
    p_all.add_argument("--force", action="store_true", help="ignore reusable output manifest")
    p_all.set_defaults(func=cmd_all)
    return parser


def legacy_to_all(argv: list[str]) -> list[str]:
    if argv and argv[0] not in {"status", "probe", "prepare", "list", "identify", "decompile", "analyze", "all", "-h", "--help"}:
        return ["all"] + argv
    return argv


def main(argv: list[str]) -> int:
    parser = build_parser()
    args = parser.parse_args(legacy_to_all(argv))
    if not hasattr(args, "func"):
        parser.print_help()
        return 2
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
