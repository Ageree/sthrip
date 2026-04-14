import json as json_mod
from typing import Optional

import typer

from sthrip.cli.core import app, make_client, run_command
from sthrip.cli.config import save_config
from sthrip.cli.output import format_success


def _parse_pricing(raw: str) -> dict:
    """Parse pricing as JSON string or key=value pairs.

    Accepts either a JSON object string like '{"per_request": "0.01"}'
    or comma-separated key=value pairs like 'per_request=0.01,per_token=0.001'.
    """
    raw = raw.strip()
    if raw.startswith("{"):
        return json_mod.loads(raw)
    result = {}
    for pair in raw.split(","):
        pair = pair.strip()
        if "=" not in pair:
            raise typer.BadParameter(
                f"Invalid pricing pair '{pair}'. Use key=value format or a JSON string."
            )
        key, value = pair.split("=", 1)
        result[key.strip()] = value.strip()
    return result


@app.command()
@run_command
def register(
    name: str = typer.Argument(..., help="Agent name (3-255 chars, alphanumeric + -_)"),
    webhook_url: Optional[str] = typer.Option(None, "--webhook-url", help="Webhook URL"),
    privacy: str = typer.Option("medium", "--privacy", help="Privacy level"),
    capabilities: Optional[str] = typer.Option(
        None, "--capabilities", help="Comma-separated capabilities (e.g. 'translation,code-review')",
    ),
    description: Optional[str] = typer.Option(
        None, "--description", help="Agent description for marketplace",
    ),
    pricing: Optional[str] = typer.Option(
        None, "--pricing", help="Pricing as JSON or key=value pairs (e.g. 'per_request=0.01')",
    ),
    accepts_escrow: Optional[bool] = typer.Option(
        None, "--accepts-escrow/--no-escrow", help="Whether agent accepts escrow payments",
    ),
):
    """Register a new agent and save credentials."""
    client = make_client(require_auth=False)
    body = {"agent_name": name, "privacy_level": privacy}
    if webhook_url:
        body["webhook_url"] = webhook_url
    if capabilities is not None:
        body["capabilities"] = [c.strip() for c in capabilities.split(",") if c.strip()]
    if description is not None:
        body["description"] = description
    if pricing is not None:
        body["pricing"] = _parse_pricing(pricing)
    if accepts_escrow is not None:
        body["accepts_escrow"] = accepts_escrow
    data = client.post("/v2/agents/register", json=body)
    save_config({"api_key": data.get("api_key", ""), "agent_name": name})
    print(format_success(data))
