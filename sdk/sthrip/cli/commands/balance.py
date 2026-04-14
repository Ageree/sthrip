from typing import Optional
import typer
from sthrip.cli.core import app, make_client, run_command
from sthrip.cli.output import format_success

@app.command()
@run_command
def balance():
    """Get current hub balance."""
    client = make_client()
    data = client.get("/v2/balance")
    print(format_success(data))

@app.command()
@run_command
def deposit(
    amount: Optional[str] = typer.Option(None, "--amount", help="Amount (required in ledger mode)"),
    idempotency_key: Optional[str] = typer.Option(None, "--idempotency-key"),
):
    """Request deposit address or credit balance."""
    client = make_client()
    body = {}
    if amount:
        body["amount"] = amount
    data = client.post("/v2/balance/deposit", json=body, idempotency_key=idempotency_key)
    print(format_success(data))

@app.command()
@run_command
def withdraw(
    address: str = typer.Argument(..., help="Destination Monero address"),
    amount: str = typer.Argument(..., help="Amount to withdraw"),
    idempotency_key: Optional[str] = typer.Option(None, "--idempotency-key"),
):
    """Withdraw XMR to an external address."""
    client = make_client()
    data = client.post(
        "/v2/balance/withdraw",
        json={"address": address, "amount": amount},
        idempotency_key=idempotency_key,
    )
    print(format_success(data))

@app.command()
@run_command
def deposits():
    """List deposit transactions."""
    client = make_client()
    data = client.get("/v2/balance/deposits")
    print(format_success(data))
