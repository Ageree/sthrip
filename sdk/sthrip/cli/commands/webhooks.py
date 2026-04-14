import typer
from sthrip.cli.core import app, make_client, run_command
from sthrip.cli.output import format_success

webhooks_app = typer.Typer(add_completion=False, pretty_exceptions_enable=False)
app.add_typer(webhooks_app, name="webhooks")

@webhooks_app.command("list")
@run_command
def webhooks_list():
    """List recent webhook events."""
    client = make_client()
    data = client.get("/v2/webhooks/events")
    print(format_success(data))

@webhooks_app.command("retry")
@run_command
def webhooks_retry(
    event_id: str = typer.Argument(..., help="Webhook event ID to retry"),
):
    """Retry a failed webhook delivery."""
    client = make_client()
    data = client.post(f"/v2/webhooks/events/{event_id}/retry")
    print(format_success(data))
