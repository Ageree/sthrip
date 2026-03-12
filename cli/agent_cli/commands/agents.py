from typing import Optional
import typer
from cli.agent_cli.core import app, make_client, run_command
from cli.agent_cli.output import format_success

agents_app = typer.Typer(add_completion=False, pretty_exceptions_enable=False)
app.add_typer(agents_app, name="agents")

@agents_app.command("list")
@run_command
def agents_list(
    verified: bool = typer.Option(False, "--verified", help="Only verified agents"),
    tier: Optional[str] = typer.Option(None, "--tier", help="Filter by tier"),
    min_trust_score: Optional[float] = typer.Option(None, "--min-trust-score"),
    limit: int = typer.Option(20, "--limit"),
    offset: int = typer.Option(0, "--offset"),
):
    """Discover agents with filters."""
    client = make_client()
    params = {"limit": limit, "offset": offset}
    if verified:
        params["verified_only"] = "true"
    if tier:
        params["tier"] = tier
    if min_trust_score is not None:
        params["min_trust_score"] = min_trust_score
    data = client.get("/v2/agents", params=params)
    print(format_success(data))

@agents_app.command("get")
@run_command
def agents_get(
    name: str = typer.Argument(..., help="Agent name to look up"),
):
    """Get public agent profile."""
    client = make_client()
    data = client.get(f"/v2/agents/{name}")
    print(format_success(data))

@app.command()
@run_command
def leaderboard():
    """Top agents by trust score."""
    client = make_client()
    data = client.get("/v2/leaderboard")
    print(format_success(data))
