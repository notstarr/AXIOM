from __future__ import annotations

import logging
import os
import threading
from pathlib import Path

import click

from axiom.dispatcher.http import HttpControlPlane
from axiom.dispatcher.runner import AxiomRunner


def _load_service_token() -> str:
    """读取机器控制面 service token：优先环境变量，其次 ~/.local/share/axiom-v3/service_token。"""
    env_token = os.environ.get("AXIOM_SERVICE_TOKEN", "").strip()
    if env_token:
        return env_token
    try:
        token_file = (
            Path(os.environ.get("AXIOM_DATA_DIR", "~/.local/share/axiom-v3")).expanduser()
            / "service_token"
        )
        if token_file.exists():
            return token_file.read_text("utf-8").strip()
    except Exception:
        pass
    return ""


def register_runner_command(main: click.Group) -> None:
    @main.command("runner")
    @click.option("--api-url", default="http://127.0.0.1:8899", show_default=True)
    @click.option("--service-token", default="", help="Optional internal API token.")
    @click.option("--interval", default=None, type=float, help="Override runtime settings.")
    @click.option("--max-parallel", default=None, type=int, help="Override runtime settings.")
    @click.option("--lease-timeout", default=None, type=int, help="Override runtime settings.")
    @click.option("--once", is_flag=True, help="Run one reconciliation and wait for its claims.")
    def runner_command(
        api_url: str,
        service_token: str,
        interval: float | None,
        max_parallel: int | None,
        lease_timeout: int | None,
        once: bool,
    ) -> None:
        """Run the API-only AXIOM worker runner."""
        logging.basicConfig(level=logging.INFO)
        if not service_token:
            service_token = _load_service_token()
        control = HttpControlPlane(api_url, service_token=service_token)
        runner = AxiomRunner(
            control,
            max_parallel=max_parallel,
            lease_timeout_seconds=lease_timeout,
        )
        stop_event = threading.Event()
        try:
            if once:
                runner.run_once()
                runner.wait_for_idle()
            else:
                runner.run_forever(stop_event=stop_event, interval_seconds=interval)
        except KeyboardInterrupt:
            stop_event.set()
        finally:
            runner.close(wait=True)
            control.close()
