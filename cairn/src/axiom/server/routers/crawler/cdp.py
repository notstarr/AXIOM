"""crawler/cdp.py — CDP tab lifecycle management via the stateless helper."""

from __future__ import annotations

import json
import os
import subprocess
import urllib.request

from axiom.server.db import get_conn

from .constants import CDP_PY, CDP_CTL, CDP_ENDPOINT
from .meta import read_meta, write_meta


def cdp_ctl(*args: str, timeout: float = 30.0) -> tuple[int, str]:
    try:
        proc = subprocess.run(
            [CDP_PY, CDP_CTL, *args],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        return proc.returncode, (proc.stdout or "") + (proc.stderr or "")
    except (subprocess.TimeoutExpired, FileNotFoundError) as e:
        return 127, f"cdp_ctl invocation failed: {e}"


def create_crawler_tab(target_url: str) -> str:
    rc, out = cdp_ctl("new-tab", target_url, timeout=30.0)
    if rc != 0:
        return ""
    for line in out.splitlines():
        if line.startswith("TAB_ID:"):
            return line.split(":", 1)[1].strip()
    return ""


def close_crawler_tab(tab_id: str) -> None:
    if not tab_id:
        return
    cdp_ctl("close-tab", tab_id, timeout=10.0)


def ensure_crawler_tab(meta: dict) -> str:
    project_id = str(meta["project_id"])
    tab_id = str(meta.get("cdp_tab_id") or "")
    if tab_id:
        rc, _ = cdp_ctl("--tab-id", tab_id, "url", timeout=8.0)
        if rc == 0:
            return tab_id
    target_url = str(meta.get("target_url") or "")
    tab_id = create_crawler_tab(target_url) if target_url else ""
    if tab_id:
        meta["cdp_tab_id"] = tab_id
        write_meta(project_id, meta)
    return tab_id
