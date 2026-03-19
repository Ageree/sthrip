import json as json_mod
from typing import Optional

import typer

from cli.agent_cli.core import app, make_client, run_command
from cli.agent_cli.output import format_success

marketplace_app = typer.Typer(add_completion=False, pretty_exceptions_enable=False)
app.add_typer(marketplace_app, name="marketplace")


@marketplace_app.callback(invoke_without_command=True)
@run_command
def marketplace_list(
    ctx: typer.Context,
    capability: Optional[str] = typer.Option(None, "--capability", help="Filter by capability"),
    limit: int = typer.Option(20, "--limit", help="Max results to return"),
    offset: int = typer.Option(0, "--offset", help="Pagination offset"),
):
    """List agents from the marketplace."""
    if ctx.invoked_subcommand is not None:
        return
    client = make_client()
    params = {"limit": limit, "offset": offset}
    if capability:
        params["capability"] = capability
    data = client.get("/v2/agents/marketplace", params=params)
    print(format_success(data))


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


@app.command("update-profile")
@run_command
def update_profile(
    description: Optional[str] = typer.Option(
        None, "--description", help="Agent description for marketplace",
    ),
    capabilities: Optional[str] = typer.Option(
        None, "--capabilities", help="Comma-separated capabilities (e.g. 'translation,code-review')",
    ),
    pricing: Optional[str] = typer.Option(
        None, "--pricing", help="Pricing as JSON or key=value pairs (e.g. 'per_request=0.01')",
    ),
    accepts_escrow: Optional[bool] = typer.Option(
        None, "--accepts-escrow/--no-escrow", help="Whether agent accepts escrow payments",
    ),
):
    """Update own marketplace profile."""
    client = make_client()
    body = {}
    if description is not None:
        body["description"] = description
    if capabilities is not None:
        body["capabilities"] = [c.strip() for c in capabilities.split(",") if c.strip()]
    if pricing is not None:
        body["pricing"] = _parse_pricing(pricing)
    if accepts_escrow is not None:
        body["accepts_escrow"] = accepts_escrow
    if not body:
        print(format_success({"message": "No fields to update. Use --help for options."}))
        return
    data = client.patch("/v2/me/settings", json=body)
    print(format_success(data))
