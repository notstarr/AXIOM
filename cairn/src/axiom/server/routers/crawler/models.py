"""crawler/models.py — Pydantic models for the crawler API."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel


class CrawlerWorkerConfig(BaseModel):
    pi_model: str = ""
    pi_base_url: str = ""
    pi_api_key: str = ""
    pi_provider_api: str = "openai-completions"
    pi_context_window: str = "128000"


class CrawlerTestRequest(BaseModel):
    pi_base_url: str
    pi_model: str
    pi_api_key: str = ""
    pi_provider_api: str = "openai-completions"


class CrawlerTestResponse(BaseModel):
    ok: bool
    message: str


class CrawlerModelsResponse(BaseModel):
    models: list[dict[str, Any]]
    error: str = ""


class CrawlerStartRequest(BaseModel):
    target_url: str

    def model_post_init(self, _ctx):
        self.target_url = self.target_url.strip()
        if not self.target_url:
            raise ValueError("target_url must not be empty")


class CrawlerStatus(BaseModel):
    project_id: str
    status: str = "idle"
    phase: str = "idle"
    round: int = 0
    target_url: str = ""
    output_dir: str = ""
    pid: int | None = None
    supervisor_id: str | None = None
    phase_pid: int | None = None
    started_at: str | None = None
    phase_started_at: str | None = None
    last_flush_at: str | None = None
    active_window_seconds: int = 0
    flush_window_seconds: int = 0
    total_timeout_seconds: int = 0
    stop_requested: bool = False
    sitemap_exists: bool = False
    api_list_exists: bool = False
    api_page_map_exists: bool = False
