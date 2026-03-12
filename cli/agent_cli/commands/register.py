from typing import Optional
import typer
from cli.agent_cli.core import app, make_client, run_command
from cli.agent_cli.config import save_config
from cli.agent_cli.output import format_success

@app.command()
@run_command
def register(
    name: str = typer.Argument(..., help="Agent name (3-255 chars, alphanumeric + -_)"),
    webhook_url: Optional[str] = typer.Option(None, "--webhook-url", help="Webhook URL"),
    privacy: str = typer.Option("medium", "--privacy", help="Privacy level"),
):
    """Register a new agent and save credentials."""
    client = make_client(require_auth=False)
    body = {"agent_name": name, "privacy_level": privacy}
    if webhook_url:
        body["webhook_url"] = webhook_url
    data = client.post("/v2/agents/register", json=body)
    save_config({"api_key": data.get("api_key", ""), "agent_name": name})
    print(format_success(data))
