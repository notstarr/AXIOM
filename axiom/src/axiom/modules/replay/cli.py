"""``axiom replay`` — send a packet, look at it, photograph it.

Three commands, matching the reviewer's three steps:

* ``send``   — really send the packet; print a URL that renders the exchange.
* ``shot``   — send, open that URL with playwright, and save the PNG.
* ``attach`` — hang the PNG on a step of a reviewed subject.

``shot`` and ``attach`` are separate on purpose.  A review sometimes needs a business
screenshot too (the admin page, the data, the shell), and that picture comes from the
reviewer's own browser — so ``attach`` accepts any file, and ``shot`` is only the path
that produces a packet image.  Both end in the same ``kind`` slot, which is what the
report and the review gate read.

Everything talks to the loopback console rather than the database: the console owns the
send path and there must not be a second implementation that skips the private-network
and scope checks.
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
        raise click.ClickException(message + (f"\n提示：{hint}" if hint else ""))
    return response.json() if response.content else {}


def _read_packet(packet_file: Path) -> str:
    if str(packet_file) == "-":
        import sys

        return sys.stdin.read()
    return packet_file.read_text(encoding="utf-8")


def _send_payload(
    *,
    task_id: str,
    result_id: str,
    step: int,
    packet: str,
    highlights: tuple[str, ...],
    follow: bool,
    allow_delete: bool,
    allow_private: bool,
    compare_with_recorded: bool,
) -> dict:
    payload = {
        "task_id": task_id,
        "result_id": result_id,
        "step": step,
        "packet": packet,
        "highlights": list(highlights),
        "follow_redirects": follow,
        "allow_delete": allow_delete,
        "allow_private": allow_private,
        "compare_with_recorded": compare_with_recorded,
    }
    return payload


def _print_result(result: dict, *, as_json: bool) -> None:
    if as_json:
        click.echo(json.dumps(result, ensure_ascii=False))
        return
    compare = result.get("compare") or {}
    labels = {"match": "一致", "mismatch": "不一致", "inconclusive": "无法比对"}
    click.echo(f"状态码：{result.get('status_code')} {result.get('reason') or ''}".rstrip())
    click.echo(f"耗时：{result.get('elapsed_ms')} ms")
    if compare.get("status"):
        click.echo(f"比对：{labels.get(compare['status'], compare['status'])}")
        for check in compare.get("checks") or []:
            mark = "✓" if check["ok"] is True else ("✗" if check["ok"] is False else "-")
            click.echo(f"  {mark} {check['name']}: {check['expected']} → {check['actual']}")
        for note in compare.get("notes") or []:
            click.echo(f"  · {note}")


def register_replay_commands(main: click.Group) -> None:
    @main.group("replay")
    def replay_group() -> None:
        """Send a recorded packet again, render it, and hang the picture on a step."""

    @replay_group.command("serve")
    @click.option("--host", default="127.0.0.1", show_default=True)
    @click.option("--port", default=8890, show_default=True, type=int)
    @click.option("--data-dir", type=click.Path(path_type=Path), required=True)
    @click.option("--log-level", default="info", show_default=True)
    def replay_serve(host: str, port: int, data_dir: Path, log_level: str) -> None:
        """Start the loopback-only replay page (never expose this port publicly)."""

        import uvicorn

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
        click.echo(f"replay page: http://{host}:{port}/x/<key>?t={token}")
        uvicorn.run(
            create_replay_app(
                container=container,
                token=token,
                data_dir=resolved,
                public_base=os.environ.get("AXIOM_REPLAY_PUBLIC_BASE", ""),
            ),
            host=host,
            port=port,
            log_level=log_level,
        )

    @replay_group.command("token")
    def replay_token() -> None:
        """Print the token the send endpoint requires."""
        token = _token()
        if not token:
            raise click.ClickException("未找到 token 文件；服务首次启动时会生成")
        click.echo(token)

    def _common(function):
        function = click.option("--task-id", required=True, envvar="AXIOM_TASK_ID")(function)
        function = click.option("--highlight", "highlights", multiple=True)(function)
        function = click.option("--follow", is_flag=True, help="跟随重定向（默认否）")(function)
        function = click.option("--allow-delete", is_flag=True)(function)
        function = click.option("--allow-private", is_flag=True, help="仅测试用：放行私网目标")(function)
        function = click.option(
            "--compare",
            "compare_with_recorded",
            is_flag=True,
            help="与上报的响应逐项比对（默认不比对：审核者是照步骤自己复现一遍）",
        )(function)
        function = click.option("--json", "as_json", is_flag=True, help="输出机器可读 JSON")(function)
        return function

    @replay_group.command("send")
    @click.option("--result", "result_id", default="", help="红队成果 ID（报文取自上报记录）")
    @click.option("--step", type=click.IntRange(min=0), default=0, help="重放已上报成果时必填（从 1 开始）")
    @click.option(
        "--packet-file",
        type=click.Path(path_type=Path, dir_okay=False, allow_dash=True),
        default="-",
        help="报文或 curl 命令；`-` 从 stdin 读",
    )
    @_common
    def replay_send(
        result_id: str,
        step: int,
        packet_file: Path,
        task_id: str,
        highlights: tuple[str, ...],
        follow: bool,
        allow_delete: bool,
        allow_private: bool,
        compare_with_recorded: bool,
        as_json: bool,
    ) -> None:
        """Send it for real and print the URL that renders the exchange."""
        packet = "" if result_id else _read_packet(packet_file)
        result = _call(
            "POST",
            "/api/send",
            _send_payload(
                task_id=task_id,
                result_id=result_id,
                step=step,
                packet=packet,
                highlights=highlights,
                follow=follow,
                allow_delete=allow_delete,
                allow_private=allow_private,
                compare_with_recorded=compare_with_recorded,
            ),
        )
        if as_json:
            click.echo(json.dumps(result, ensure_ascii=False))
            return
        _print_result(result, as_json=False)
        click.echo()
        click.echo(result["url"])
        click.echo(f"（{result['expires_in_seconds']} 秒后过期）")

    @replay_group.command("shot")
    @click.option("--result", "result_id", default="")
    @click.option("--step", type=click.IntRange(min=0), default=0, help="重放已上报成果时必填（从 1 开始）")
    @click.option(
        "--packet-file",
        type=click.Path(path_type=Path, dir_okay=False, allow_dash=True),
        default="-",
    )
    @click.option("--output", type=click.Path(path_type=Path, dir_okay=False), required=True)
    @click.option("--caption", default="")
    @_common
    def replay_shot(
        result_id: str,
        step: int,
        packet_file: Path,
        output: Path,
        caption: str,
        task_id: str,
        highlights: tuple[str, ...],
        follow: bool,
        allow_delete: bool,
        allow_private: bool,
        compare_with_recorded: bool,
        as_json: bool,
    ) -> None:
        """Send it, render the page with playwright, and save the PNG.

        This is the packet-image path.  ``axiom review evidence attach --kind packet``
        hangs the result on the step.
        """
        from axiom.modules.replay.render import RenderError, RenderInput, screenshot
        from axiom.modules.replay.compare import Check, Comparison

        packet = "" if result_id else _read_packet(packet_file)
        result = _call(
            "POST",
            "/api/send",
            _send_payload(
                task_id=task_id,
                result_id=result_id,
                step=step,
                packet=packet,
                highlights=highlights,
                follow=follow,
                allow_delete=allow_delete,
                allow_private=allow_private,
                compare_with_recorded=compare_with_recorded,
            ),
        )

        head = [f"HTTP/1.1 {result.get('status_code')} {result.get('reason') or ''}".rstrip()]
        head.extend(f"{name}: {value}" for name, value in (result.get("headers") or {}).items())
        joined = "\n".join(head)
        body = result.get("body") or ""
        response_text = f"{joined}\n\n{body}" if body else joined

        raw_compare = result.get("compare") or {}
        comparison = None
        # 只有显式 --compare 才把比对结论画上。默认的 "inconclusive" 是一条噪音：
        # 审核者是照步骤自己复现，本来就不该拿上报的报文逐字对比。
        if raw_compare.get("status") and compare_with_recorded:
            comparison = Comparison(
                status=str(raw_compare["status"]),
                checks=[
                    Check(
                        name=str(item.get("name") or ""),
                        expected=str(item.get("expected") or ""),
                        actual=str(item.get("actual") or ""),
                        ok=item.get("ok"),
                    )
                    for item in raw_compare.get("checks") or []
                ],
                notes=[str(item) for item in raw_compare.get("notes") or []],
            )

        # 左栏永远是服务端实际发出的那份报文：--result 时它取自入库的复现，
        # 自由发包时就是 stdin 那一份。用「本地变量优先」会让 --result 模式下
        # 左栏直接空白（本地 packet 是空串），截图就成了半张纸。
        payload = RenderInput(
            packet_text=result.get("request_text") or packet or "",
            response_text=response_text,
            title=caption or "HTTP",
            caption="",
            source_label="axiom-replay",
            elapsed_ms=result.get("elapsed_ms"),
            highlights=list(highlights),
            comparison=comparison,
            body_truncated=bool(result.get("body_truncated")),
            scheme_note=f"实际使用 {result.get('scheme_used')}" if result.get("scheme_used") else "",
        )
        try:
            png = screenshot(payload, chromium=os.environ.get("AXIOM_REPLAY_CHROME", "").strip())
        except RenderError as exc:
            raise click.ClickException(str(exc)) from exc
        output.write_bytes(png)
        if as_json:
            click.echo(json.dumps({**result, "output": str(output), "bytes": len(png)}, ensure_ascii=False))
            return
        _print_result(result, as_json=False)
        click.echo(f"图片：{output}（{len(png)} 字节）")

    @replay_group.command("attach")
    @click.option("--step", type=click.IntRange(min=1), required=True)
    @click.option(
        "--kind",
        type=click.Choice(("packet", "business")),
        default="packet",
        show_default=True,
        help="packet=该步真实回包（每步必需）；business=业务效果图（可选）",
    )
    @click.option("--file", "file_path", type=click.Path(path_type=Path, exists=True, dir_okay=False), required=True)
    @click.option("--caption", default="")
    @click.option("--subject", "subject_id", required=True, help="成果 xXXX 或漏洞 vXXX")
    @_common
    def replay_attach(
        step: int,
        kind: str,
        file_path: Path,
        caption: str,
        subject_id: str,
        task_id: str,
        highlights: tuple[str, ...],
        follow: bool,
        allow_delete: bool,
        allow_private: bool,
        compare_with_recorded: bool,
        as_json: bool,
    ) -> None:
        """Upload a picture and hang it on step N of the subject."""
        from axiom.modules.reports import KIND_BUSINESS, KIND_PACKET

        content = file_path.read_bytes()
        if not content:
            raise click.ClickException("文件为空")
        subject_type = "redteam_result" if subject_id.startswith("x") else "vulnerability"
        url = f"{_base_url()}/api/evidence"
        headers = {"authorization": f"Bearer {_token()}"}
        files = {"file": (file_path.name, content, "image/png")}
        data = {
            "task_id": task_id,
            "subject_type": subject_type,
            "subject_id": subject_id,
            "step": str(step),
            "kind": kind,
            "caption": caption,
        }
        try:
            with httpx.Client(timeout=120, trust_env=False) as client:
                response = client.post(url, headers=headers, files=files, data=data)
        except httpx.HTTPError as exc:
            raise click.ClickException(f"重放服务不可用：{exc}") from exc
        if response.is_error:
            try:
                body = response.json()
            except ValueError:
                body = {}
            raise click.ClickException(str(body.get("message") or response.text))
        record = response.json()
        if as_json:
            click.echo(json.dumps(record, ensure_ascii=False))
        else:
            click.echo(f"已挂到 {subject_id} 第 {step} 步（{record.get('artifact_id')}，kind={kind}）")


__all__ = ["register_replay_commands"]
