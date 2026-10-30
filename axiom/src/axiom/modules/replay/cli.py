"""``axiom replay`` — the reviewer's side of the replay console.

Two audiences, one service:

* An AI reviewer inside a ReviewRun calls ``verify``/``shot`` through the local 8890
  console, so the screenshots and the evidence attachment happen server-side where
  the packets actually leave.
* A human on the same box runs ``serve`` to bring that console up.

Everything routes through 8890 rather than touching the database directly, because
the console owns the send/capture path and there must not be a second implementation
that skips the private-network and scope checks.
"""

from __future__ import annotations

import base64
import json
import os
from pathlib import Path

import click
import httpx


DEFAULT_URL = "http://127.0.0.1:8890"
TOKEN_FILE = Path("/var/lib/axiom/replay-token")


def _base_url() -> str:
    return os.environ.get("AXIOM_REPLAY_URL", DEFAULT_URL).rstrip("/")


def _token() -> str:
    configured = os.environ.get("AXIOM_REPLAY_TOKEN", "").strip()
    if configured:
        return configured
    try:
        return TOKEN_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _call(method: str, path: str, payload: dict | None = None) -> dict:
    headers = {"content-type": "application/json"}
    token = _token()
    if not token:
        raise click.ClickException(
            "未找到重放器 token：请设置 AXIOM_REPLAY_TOKEN，或 sudo cat /var/lib/axiom/replay-token"
        )
    headers["authorization"] = f"Bearer {token}"
    try:
        with httpx.Client(base_url=_base_url(), headers=headers, timeout=180, trust_env=False) as client:
            response = client.request(method, path, json=payload)
    except httpx.HTTPError as exc:
        raise click.ClickException(
            f"重放服务不可用（{_base_url()}）：{exc}；启动它：systemctl start axiom-replay"
        ) from exc
    if response.is_error:
        try:
            body = response.json()
        except ValueError:
            body = {}
        message = str(body.get("message") or response.text or "").strip()
        hint = str(body.get("hint") or "").strip()
        raise click.ClickException(f"{message}" + (f"\n提示：{hint}" if hint else ""))
    return response.json() if response.content else {}


def register_replay_commands(main: click.Group) -> None:
    @main.group("replay")
    def replay_group() -> None:
        """Replay a recorded packet against the real target and capture the answer."""

    @replay_group.command("serve")
    @click.option("--host", default="127.0.0.1", show_default=True)
    @click.option("--port", default=8890, show_default=True, type=int)
    @click.option("--data-dir", type=click.Path(path_type=Path), required=True)
    @click.option("--log-level", default="info", show_default=True)
    def replay_serve(host: str, port: int, data_dir: Path, log_level: str) -> None:
        """Start the loopback-only replay console (never expose this port publicly)."""

        import uvicorn

        from axiom.app import create_app  # noqa: F401  (ensures module wiring is valid)
        from axiom.composition import Container
        from axiom.modules.replay.web import create_replay_app, read_or_create_token
        from axiom.shared.database import create_database_engine, database_url_from_env

        if host not in {"127.0.0.1", "::1", "localhost"}:
            raise click.ClickException(
                "重放器只能监听本机：它没有 AXIOM 的登录体系，绑定到外部地址等于对公网开放"
            )

        resolved = data_dir.expanduser().resolve()
        engine = create_database_engine(database_url_from_env(), application_name="axiom-replay")
        container = Container.build(engine, data_dir=resolved)
        token = read_or_create_token(resolved)
        click.echo(f"replay console: http://{host}:{port}/?t={token}")
        uvicorn.run(
            create_replay_app(container=container, token=token, data_dir=resolved),
            host=host,
            port=port,
            log_level=log_level,
        )

    @replay_group.command("token")
    def replay_token() -> None:
        """Print the console token (read it from the 0600 file)."""
        token = _token()
        if not token:
            raise click.ClickException("未找到 token 文件；服务首次启动时会生成")
        click.echo(token)

    @replay_group.command("list")
    @click.argument("task_id")
    def replay_list(task_id: str) -> None:
        """List the red-team results of a task and the steps each one can replay."""
        results = _call("GET", f"/api/results?task_id={task_id}")
        if not results:
            click.echo("该任务没有红队成果")
            return
        for item in results:
            click.echo(f"{item['id']}  [{item['status']}]  {item['title']}")
            if not item.get("steps"):
                click.echo("    无结构化复现，无法重放")
                continue
            for step in item["steps"]:
                click.echo(f"    {step['index']}. {step['title']}")

    def _run(
        action: str,
        task_id: str,
        result_id: str,
        step: int,
        packet: str,
        highlights: tuple[str, ...],
        asserts: tuple[str, ...],
        caption: str,
        follow: bool,
        allow_delete: bool,
        allow_private: bool,
        output: Path | None,
        quiet: bool,
    ) -> None:
        payload = {
            "action": action,
            "task_id": task_id,
            "result_id": result_id,
            "step": step,
            "packet": packet,
            "highlights": list(highlights),
            "asserts": list(asserts),
            "caption": caption,
            "follow_redirects": follow,
            "allow_delete": allow_delete,
            "allow_private": allow_private,
        }
        result = _call("POST", "/api/replay", payload)
        image = result.pop("image_base64", "")
        if output is not None and image:
            output.write_bytes(base64.b64decode(image))
            result["saved_to"] = str(output)
            result.pop("image_base64", None)
        elif image:
            result["screenshot_bytes"] = len(base64.b64decode(image))
        if quiet:
            click.echo(json.dumps(result, ensure_ascii=False))
            return
        compare_payload = result.get("compare") or {}
        labels = {"match": "一致", "mismatch": "不一致", "inconclusive": "无法比对"}
        click.echo(f"发包：{'成功' if result.get('sent') else '失败'}  {result.get('request_text','').splitlines()[0] if result.get('request_text') else ''}")
        if compare_payload.get("status"):
            click.echo(f"比对：{labels.get(compare_payload['status'], compare_payload['status'])}")
            for check in compare_payload.get("checks", []):
                mark = "✓" if check["ok"] is True else ("✗" if check["ok"] is False else "-")
                click.echo(f"  {mark} {check['name']}: {check['expected']} → {check['actual']}")
            for note in compare_payload.get("notes", []):
                click.echo(f"  · {note}")
        if result.get("artifact_id"):
            click.echo(f"证据：已挂到 {result_id} 第 {step} 步（{result['artifact_id']}）")
        if result.get("screenshot_error"):
            click.echo(f"截图未生成：{result['screenshot_error']}")
        if output is not None and image:
            click.echo(f"图片：{output}")

    def _common_options(function):
        function = click.option("--task-id", required=True, envvar="AXIOM_TASK_ID")(function)
        function = click.option("--highlight", "highlights", multiple=True)(function)
        function = click.option("--assert", "asserts", multiple=True)(function)
        function = click.option("--caption", default="")(function)
        function = click.option("--follow", is_flag=True, help="跟随重定向（默认否）")(function)
        function = click.option("--allow-delete", is_flag=True)(function)
        function = click.option("--allow-private", is_flag=True, help="仅测试用：放行私网目标")(function)
        function = click.option("--json", "as_json", is_flag=True, help="输出机器可读 JSON")(function)
        return function

    @replay_group.command("verify")
    @click.option("--result", "result_id", required=True)
    @click.option("--step", type=click.IntRange(min=1), required=True)
    @_common_options
    def replay_verify(
        result_id: str,
        step: int,
        task_id: str,
        highlights: tuple[str, ...],
        asserts: tuple[str, ...],
        caption: str,
        follow: bool,
        allow_delete: bool,
        allow_private: bool,
        as_json: bool,
    ) -> None:
        """Send the recorded packet again and report whether the answer matches."""
        _run("verify", task_id, result_id, step, "", highlights, asserts, caption,
             follow, allow_delete, allow_private, None, as_json)

    @replay_group.command("shot")
    @click.option("--result", "result_id", required=True)
    @click.option("--step", type=click.IntRange(min=1), required=True)
    @click.option("--output", type=click.Path(path_type=Path, dir_okay=False))
    @_common_options
    def replay_shot(
        result_id: str,
        step: int,
        output: Path | None,
        task_id: str,
        highlights: tuple[str, ...],
        asserts: tuple[str, ...],
        caption: str,
        follow: bool,
        allow_delete: bool,
        allow_private: bool,
        as_json: bool,
    ) -> None:
        """Send the packet, capture the exchange, and hang it on the result's step."""
        _run("shot", task_id, result_id, step, "", highlights, asserts, caption,
             follow, allow_delete, allow_private, output, as_json)

    @replay_group.command("send")
    @click.option(
        "--packet-file",
        type=click.Path(path_type=Path, dir_okay=False, allow_dash=True),
        default="-",
        show_default=True,
        help="报文文件；`-` 从 stdin 读（heredoc）",
    )
    @click.option("--output", type=click.Path(path_type=Path, dir_okay=False))
    @_common_options
    def replay_send(
        packet_file: Path,
        output: Path | None,
        task_id: str,
        highlights: tuple[str, ...],
        asserts: tuple[str, ...],
        caption: str,
        follow: bool,
        allow_delete: bool,
        allow_private: bool,
        as_json: bool,
    ) -> None:
        """Send an arbitrary packet (free-form probing during a review)."""
        if str(packet_file) == "-":
            import sys

            packet = sys.stdin.read()
        else:
            packet = packet_file.read_text(encoding="utf-8")
        _run("send", task_id, "", 0, packet, highlights, asserts, caption,
             follow, allow_delete, allow_private, output, as_json)


__all__ = ["register_replay_commands"]
