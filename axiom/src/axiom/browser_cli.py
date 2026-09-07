"""Browser session snapshot CLI: log in once, inject everywhere.

会话快照是 Playwright storage_state JSON（含 HttpOnly Cookie 与
LocalStorage），保存于 `<data-dir>/browser-sessions/<name>.json`。
Runner 通过 AXIOM_BROWSER_SESSION 引用名称，在为每个 Intent 创建
独立上下文时注入该快照，实现"登录一次、多 Intent 并发复用"。
"""
from __future__ import annotations

import json
from pathlib import Path

import click


def _session_dir(data_dir: Path) -> Path:
    return data_dir.expanduser().resolve() / "browser-sessions"


@click.group("browser-session")
def browser_session_commands() -> None:
    """Prepare and manage browser session snapshots."""


@browser_session_commands.command("save")
@click.option("--name", required=True, help="会话名称（任务里通过 AXIOM_BROWSER_SESSION 引用）")
@click.option("--url", required=True, help="打开并登录的目标地址")
@click.option("--data-dir", type=click.Path(path_type=Path), required=True)
def session_save(name: str, url: str, data_dir: Path) -> None:
    """Open a visible Chrome for Testing window; log in, then press Enter to export."""
    if not name.replace("-", "").replace("_", "").isalnum():
        raise click.ClickException("会话名称仅允许字母、数字、连字符与下划线")
    directory = _session_dir(data_dir)
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / f"{name}.json"
    binary = BrowserBinaryResolver.resolve()
    if not binary:
        raise click.ClickException(
            "未找到 Chrome/Chromium；请先执行 playwright install chromium 或配置 CHROME_BIN"
        )
    from playwright.sync_api import sync_playwright

    profile = directory / ".login-profile"
    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            user_data_dir=str(profile),
            executable_path=binary,
            headless=False,
            viewport={"width": 1280, "height": 800},
        )
        try:
            page = context.pages[0] if context.pages else context.new_page()
            page.goto(url)
            click.echo(f"请在打开的浏览器窗口完成登录：{url}")
            input("登录完成后按回车导出会话快照... ")
            state = context.storage_state()
        finally:
            context.close()
    target.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")
    target.chmod(0o600)
    click.echo(
        f"已保存会话「{name}」→ {target} "
        f"(cookies={len(state.get('cookies') or [])}, "
        f"origins={len(state.get('origins') or [])})"
    )


@browser_session_commands.command("list")
@click.option("--data-dir", type=click.Path(path_type=Path), required=True)
def session_list(data_dir: Path) -> None:
    """List saved session snapshots."""
    directory = _session_dir(data_dir)
    files = sorted(directory.glob("*.json"))
    if not files:
        click.echo("（暂无会话快照）")
        return
    for item in files:
        size_kb = item.stat().st_size / 1024
        click.echo(f"{item.stem}\t{size_kb:.1f} KB\t{item}")


@browser_session_commands.command("remove")
@click.option("--name", required=True)
@click.option("--data-dir", type=click.Path(path_type=Path), required=True)
def session_remove(name: str, data_dir: Path) -> None:
    """Delete a session snapshot."""
    target = _session_dir(data_dir) / f"{name}.json"
    if target.is_file():
        target.unlink()
        click.echo(f"已删除会话「{name}」")
    else:
        click.echo(f"会话「{name}」不存在")


class BrowserBinaryResolver:
    """Reuse the broker's Chrome resolution rules for the login window."""

    @staticmethod
    def resolve() -> str | None:
        from axiom.dispatcher.browser_broker import BrowserBrokerServer

        return BrowserBrokerServer._chrome_binary()


__all__ = ["register_browser_session_commands"]


def register_browser_session_commands(group: click.Group) -> None:
    group.add_command(browser_session_commands)
