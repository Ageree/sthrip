from typing import Optional
import typer
from sthrip.cli.core import app, make_client, run_command
from sthrip.cli.output import format_success

me_app = typer.Typer(add_completion=False, pretty_exceptions_enable=False)
app.add_typer(me_app, name="me")

@me_app.callback(invoke_without_command=True)
@run_command
def me_info(ctx: typer.Context):
    """Get current agent info."""
    if ctx.invoked_subcommand is not None:
        return
    client = make_client()
    data = client.get("/v2/me")
    print(format_success(data))

@me_app.command("update")
@run_command
def me_update(
    webhook_url: Optional[str] = typer.Option(None, "--webhook-url"),
    privacy: Optional[str] = typer.Option(None, "--privacy"),
):
    """Update agent settings."""
    client = make_client()
    body = {}
    if webhook_url is not None:
        body["webhook_url"] = webhook_url
    if privacy is not None:
        body["privacy_level"] = privacy
    data = client.patch("/v2/me/settings", json=body)
    print(format_success(data))

@app.command("rate-limit")
@run_command
def rate_limit():
    """Get current rate limit status."""
    client = make_client()
    data = client.get("/v2/me/rate-limit")
    print(format_success(data))
