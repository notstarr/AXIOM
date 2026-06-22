"""crawler/constants.py — shared constants and paths."""

from __future__ import annotations

import os
import pathlib

# Prompt template paths
_PROMPTS_DIR = (
    pathlib.Path(__file__).resolve().parents[3]
    / "dispatcher" / "prompts" / "other"
)
CRAWLER_PROMPT_PATH = _PROMPTS_DIR / "Crawler.md"
CRAWLER_EXECUTE_PROMPT_PATH = _PROMPTS_DIR / "CrawlerExecute.md"
CRAWLER_FLUSH_PROMPT_PATH = _PROMPTS_DIR / "CrawlerFlush.md"

# Runtime directories
CRAWLER_SESSION_DIR = "/tmp/axiom-crawler"
CRAWLER_TRAFFIC_FILE = pathlib.Path("/tmp/proxy_flows.jsonl")

# Tools available to the pi agent
CRAWL_TOOLS = "bash,read_file,write_file,list_dir"
FLUSH_TOOLS = "bash,read_file,write_file,list_dir"

# Crawler state view file mapping: filename -> (table, root_key)
CRAWLER_VIEW_FILES = {
    "site_map.yaml": ("crawler_pages", "pages"),
    "api_inventory.yaml": ("crawler_apis", "apis"),
    "page_api_map.yaml": ("crawler_page_maps", "mappings"),
}

# CDP helper paths — resolved relative to the project, not hardcoded.
_PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[5]
_CDP_CTL_REL = pathlib.Path("cairn/src/axiom/dispatcher/skills/browser-control/scripts/cdp_ctl.py")
_VENV_PY = pathlib.Path(os.environ.get("VIRTUAL_ENV", "")) / "bin" / "python"

CDP_PY = str(_VENV_PY) if _VENV_PY.exists() else os.environ.get("CDP_PY", "python3")
CDP_CTL = str(_PROJECT_ROOT / _CDP_CTL_REL)
CDP_ENDPOINT = os.environ.get("CDP_ENDPOINT", "http://localhost:9223")
