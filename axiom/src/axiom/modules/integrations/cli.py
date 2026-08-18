from typing import Any
import click

from axiom.cli import main
from axiom.shared.dingtalk import DingTalkConfig, DingTalkNotifier


@main.group("dingtalk")
def dingtalk_group() -> None:
    """DingTalk notification and webhook integration."""


@dingtalk_group.command("test")
@click.option("--url", default=None, help="Override DingTalk Webhook URL.")
@click.option("--secret", default=None, help="Override DingTalk Secret.")
def dingtalk_test(url: str | None, secret: str | None) -> None:
    """Send a test notification to DingTalk."""
    if url:
        notifier = DingTalkNotifier(DingTalkConfig(webhook_url=url, secret=secret, enabled=True))
    else:
        from axiom.shared.dingtalk import dingtalk_notifier
        notifier = dingtalk_notifier

    if not notifier.is_enabled:
        click.secho("[!] DingTalk is not configured or disabled.", fg="yellow")
        click.echo("Please set AXIOM_DINGTALK_WEBHOOK_URL or configure via Settings page.")
        return

    click.echo("[*] Sending test notification to DingTalk...")
    ok = notifier.send_markdown(
        "AXIOM CLI 连通性测试",
        "### 🔔 AXIOM 钉钉 CLI 测试成功\n\n系统通知管道工作正常！"
    )
    if ok:
        click.secho("[✓] Test notification sent successfully!", fg="green")
    else:
        click.secho("[x] Failed to send notification. Check logs for details.", fg="red")
