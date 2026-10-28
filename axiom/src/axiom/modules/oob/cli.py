"""``axiom oob`` — one shared out-of-band bucket for every worker.

Workers run as separate processes and cannot reach each other's memory, so the
CLI is the shared handle: it talks to the API over the machine control plane
and the bucket itself is the API process's singleton.  That is also what makes
"only one at a time" enforceable — there is exactly one place that can hold it.

``start`` is idempotent.  The skill's rule is "if one is already open, use it",
and the cheapest way to honour that is for ``start`` to return the live URL
instead of failing.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import click
import httpx


def _service_token(explicit: str) -> str:
    if explicit.strip():
        return explicit.strip()
    for name in ("AXIOM_SERVICE_TOKEN",):
        value = os.environ.get(name, "").strip()
        if value:
            return value
    try:
        data_dir = Path(os.environ.get("AXIOM_DATA_DIR", "~/.local/share/axiom-v3")).expanduser()
        token_file = data_dir / "service_token"
        if token_file.exists():
            return token_file.read_text("utf-8").strip()
    except Exception:  # noqa: BLE001 - a missing token just yields 401 below
        pass
    return ""


def _client(api_url: str, token: str) -> httpx.Client:
    base = api_url.rstrip("/")
    if not base.endswith("/api/v1"):
        base += "/api/v1"
    headers = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return httpx.Client(base_url=base, headers=headers, timeout=15, trust_env=False)


def _call(api_url: str, token: str, method: str, path: str, **kwargs):
    try:
        with _client(api_url, token) as client:
            response = client.request(method, path, **kwargs)
    except httpx.HTTPError as exc:
        raise click.ClickException(f"无法连接 AXIOM API（{api_url}）：{exc}") from exc
    if response.status_code == 401:
        raise click.ClickException(
            "AXIOM API 拒绝访问：缺少或无效的 service token。"
            "请设置 AXIOM_SERVICE_TOKEN 或 AXIOM_DATA_DIR。"
        )
    if response.status_code >= 400:
        raise click.ClickException(f"API {response.status_code}：{response.text[:300]}")
    return response.json()


def _fmt_remaining(seconds: int) -> str:
    return f"{seconds // 60} 分 {seconds % 60} 秒"


def _display_zone():
    """Which zone to print times in.

    The service runs with ``TZ=UTC`` from its env file, so "local" here is UTC
    and an operator reading ``date`` (CST) would have to do arithmetic.  Render
    in ``AXIOM_OOB_TZ`` (default Asia/Shanghai, where this deployment lives) and
    label the zone once, rather than silently printing UTC.
    """

    import os
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

    name = os.environ.get("AXIOM_OOB_TZ", "Asia/Shanghai").strip() or "Asia/Shanghai"
    try:
        return ZoneInfo(name), name
    except (ZoneInfoNotFoundError, ValueError):
        return None, "UTC"


def _local(value: str) -> str:
    """ISO timestamp → ``HH:MM:SS`` in the display zone; unparseable input as-is."""

    from datetime import datetime, timezone

    zone, _ = _display_zone()
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return value
    if zone is not None:
        parsed = parsed.astimezone(zone)
    else:
        parsed = parsed.astimezone(timezone.utc)
    return parsed.strftime("%H:%M:%S")


def _print_state(state: dict) -> None:
    if not state.get("running"):
        click.echo("OOB 未开启。用 `axiom oob start` 打开（默认 10 分钟）。")
        return
    click.echo(f"URL        {state['url']}")
    click.echo(f"剩余       {_fmt_remaining(state['remaining_seconds'])}")
    click.echo(f"到期       {_local(state['expires_at'])}")
    click.echo(f"命中       {state['hit_count']} 次 / {state['unique_count']} 条")
    _, zone_name = _display_zone()
    click.echo(f"时区       {zone_name}")


def _print_hits(hits: list[dict]) -> None:
    if not hits:
        click.echo("（还没有命中）")
        return
    click.echo(f"{'时间':<20} {'次数':>4}  {'来源':<16} {'方法':<6} 标签 / 路径")
    for row in hits:
        label = row["label"] or "-"
        path = row["path"]
        click.echo(
            f"{_local(row['last_at']):<20} {row['count']:>4}  "
            f"{(row['client'] or '-'):<16} {row['method']:<6} {label}  {path}"
        )
        if row["query"]:
            click.echo(f"{'':<20} ?{row['query'][:120]}")
        if row["user_agent"]:
            click.echo(f"{'':<20} UA: {row['user_agent'][:100]}")


def register_oob_commands(main: click.Group) -> None:
    @main.group("oob")
    def oob_commands() -> None:
        """共享的带外回连捕获口（同一时刻只开一个，默认 10 分钟后自毁）。"""

    base_option = click.option(
        "--api-url",
        default=lambda: os.environ.get("AXIOM_SERVER", "http://127.0.0.1:8889"),
        show_default="AXIOM_SERVER 或 http://127.0.0.1:8889",
        help="AXIOM API 地址。",
    )
    token_option = click.option(
        "--service-token",
        default="",
        help="机器控制面 token；默认读 AXIOM_SERVICE_TOKEN 或 data-dir/service_token。",
    )

    @oob_commands.command("start")
    @base_option
    @token_option
    @click.option("--ttl", default=600, show_default=True, help="存活秒数，上限 3600。")
    @click.option("--base-url", default="", help="覆盖对外可达的基地址（如 https://1.2.3.4:8888）。")
    @click.option("--quiet", is_flag=True, help="只打印 URL，便于 shell 取用。")
    def oob_start(api_url: str, service_token: str, ttl: int, base_url: str, quiet: bool) -> None:
        """开启（或延用已开启的）捕获口。可重复执行。"""

        params = {"ttl_seconds": ttl}
        if base_url:
            params["base_url"] = base_url
        state = _call(api_url, _service_token(service_token), "POST", "/oob/start", params=params)
        if quiet:
            click.echo(state["url"])
            return
        _print_state(state)

    @oob_commands.command("status")
    @base_option
    @token_option
    def oob_status(api_url: str, service_token: str) -> None:
        """查看是否开启、URL、剩余时间、命中数。"""

        _print_state(_call(api_url, _service_token(service_token), "GET", "/oob"))

    @oob_commands.command("list")
    @base_option
    @token_option
    @click.option("--label", default="", help="只看标签匹配的命中（子串匹配）。")
    @click.option("--limit", default=50, show_default=True, help="最多显示条数。")
    @click.option("--json", "as_json", is_flag=True, help="输出原始 JSON。")
    def oob_list(api_url: str, service_token: str, label: str, limit: int, as_json: bool) -> None:
        """列出捕获到的回连。"""

        params: dict[str, object] = {"limit": limit}
        if label:
            params["label"] = label
        payload = _call(api_url, _service_token(service_token), "GET", "/oob/hits", params=params)
        if as_json:
            click.echo(json.dumps(payload, ensure_ascii=False, indent=2))
            return
        if not payload.get("running"):
            _print_state(payload)
            return
        # 过滤时把总数换成这个标签下的数，否则「命中 6 次」和下面两行对不上。
        hits = payload.get("hits") or []
        scope = f"（标签含 {label}）" if label else ""
        click.echo(f"URL        {payload['url']}")
        click.echo(f"剩余       {_fmt_remaining(payload['remaining_seconds'])}")
        filtered = label and payload.get("hit_count") != sum(r["count"] for r in hits)
        if filtered:
            click.echo(f"命中       {sum(r['count'] for r in hits)} 次 / {len(hits)} 条{scope}")
            click.echo(f"全局       {payload['hit_count']} 次 / {payload['unique_count']} 条")
        else:
            click.echo(f"命中       {payload['hit_count']} 次 / {payload['unique_count']} 条{scope}")
        click.echo()
        _print_hits(hits)

    @oob_commands.command("stop")
    @base_option
    @token_option
    def oob_stop(api_url: str, service_token: str) -> None:
        """立即关闭并清空全部记录。"""

        result = _call(api_url, _service_token(service_token), "POST", "/oob/stop")
        if result.get("stopped"):
            click.echo(f"已关闭，清空 {result.get('cleared', 0)} 条记录。")
        else:
            click.echo("本来就没有开启。")
