from typing import Optional
import typer
from cli.agent_cli.core import app, make_client, run_command
from cli.agent_cli.output import format_success

@app.command()
@run_command
def pay(
    agent_name: str = typer.Argument(..., help="Recipient agent name"),
    amount: str = typer.Argument(..., help="Amount to send"),
    memo: Optional[str] = typer.Option(None, "--memo", help="Payment memo"),
    urgent: bool = typer.Option(False, "--urgent", help="Urgent payment"),
    idempotency_key: Optional[str] = typer.Option(None, "--idempotency-key"),
):
    """Send a hub-routed payment to another agent."""
    client = make_client()
    body = {
        "to_agent_name": agent_name,
        "amount": amount,
        "urgency": "urgent" if urgent else "normal",
    }
    if memo:
        body["memo"] = memo
    data = client.post("/v2/payments/hub-routing", json=body, idempotency_key=idempotency_key)
    print(format_success(data))

@app.command()
@run_command
def payment(
    payment_id: str = typer.Argument(..., help="Payment ID to look up"),
):
    """Look up a payment by ID."""
    client = make_client()
    data = client.get(f"/v2/payments/{payment_id}")
    print(format_success(data))

@app.command()
@run_command
def history(
    limit: int = typer.Option(20, "--limit", help="Number of results"),
    offset: int = typer.Option(0, "--offset", help="Pagination offset"),
    direction: Optional[str] = typer.Option(None, "--direction", help="Filter: in or out"),
):
    """Get payment history."""
    client = make_client()
    params = {"limit": limit, "offset": offset}
    if direction:
        params["direction"] = direction
    data = client.get("/v2/payments/history", params=params)
    print(format_success(data))
