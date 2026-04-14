from typing import Optional
import typer
from sthrip.cli.core import app, make_client, run_command
from sthrip.cli.output import format_success


@app.command("escrow-create")
@run_command
def escrow_create(
    seller: str = typer.Argument(..., help="Seller agent name"),
    amount: str = typer.Argument(..., help="Escrow amount in XMR"),
    description: str = typer.Option("", "--desc", "-d", help="Work description"),
    accept_hours: int = typer.Option(24, "--accept-hours", help="Hours for seller to accept"),
    delivery_hours: int = typer.Option(48, "--delivery-hours", help="Hours for seller to deliver"),
    review_hours: int = typer.Option(24, "--review-hours", help="Hours for buyer to review"),
):
    """Create a new escrow deal (as buyer)."""
    client = make_client()
    body = {
        "seller_agent_name": seller,
        "amount": amount,
        "description": description,
        "accept_timeout_hours": accept_hours,
        "delivery_timeout_hours": delivery_hours,
        "review_timeout_hours": review_hours,
    }
    data = client.post("/v2/escrow", json=body)
    print(format_success(data))


@app.command("escrow-accept")
@run_command
def escrow_accept(
    escrow_id: str = typer.Argument(..., help="Escrow ID to accept"),
):
    """Accept an escrow deal (as seller)."""
    client = make_client()
    data = client.post(f"/v2/escrow/{escrow_id}/accept")
    print(format_success(data))


@app.command("escrow-deliver")
@run_command
def escrow_deliver(
    escrow_id: str = typer.Argument(..., help="Escrow ID to mark delivered"),
):
    """Mark escrow work as delivered (as seller)."""
    client = make_client()
    data = client.post(f"/v2/escrow/{escrow_id}/deliver")
    print(format_success(data))


@app.command("escrow-release")
@run_command
def escrow_release(
    escrow_id: str = typer.Argument(..., help="Escrow ID"),
    amount: str = typer.Argument(..., help="Amount to release to seller"),
):
    """Release escrow funds to seller (as buyer). Partial release supported."""
    client = make_client()
    data = client.post(f"/v2/escrow/{escrow_id}/release", json={"release_amount": amount})
    print(format_success(data))


@app.command("milestone-deliver")
@run_command
def milestone_deliver(
    escrow_id: str = typer.Argument(..., help="Escrow ID"),
    milestone: int = typer.Argument(..., help="Milestone sequence number"),
):
    """Mark a milestone as delivered (seller)."""
    client = make_client()
    data = client.post(f"/v2/escrow/{escrow_id}/milestones/{milestone}/deliver")
    print(format_success(data))


@app.command("milestone-release")
@run_command
def milestone_release(
    escrow_id: str = typer.Argument(..., help="Escrow ID"),
    milestone: int = typer.Argument(..., help="Milestone sequence number"),
    amount: str = typer.Argument(..., help="Amount to release"),
):
    """Release funds for a milestone (buyer)."""
    client = make_client()
    data = client.post(
        f"/v2/escrow/{escrow_id}/milestones/{milestone}/release",
        json={"release_amount": amount},
    )
    print(format_success(data))


@app.command("escrow-cancel")
@run_command
def escrow_cancel(
    escrow_id: str = typer.Argument(..., help="Escrow ID to cancel"),
):
    """Cancel escrow before seller accepts (as buyer). Full refund."""
    client = make_client()
    data = client.post(f"/v2/escrow/{escrow_id}/cancel")
    print(format_success(data))


@app.command("escrow")
@run_command
def escrow_get(
    escrow_id: str = typer.Argument(..., help="Escrow ID"),
):
    """Get escrow deal details."""
    client = make_client()
    data = client.get(f"/v2/escrow/{escrow_id}")
    print(format_success(data))


@app.command("escrows")
@run_command
def escrow_list(
    role: Optional[str] = typer.Option(None, "--role", help="Filter: buyer, seller, or all"),
    status: Optional[str] = typer.Option(None, "--status", help="Filter by status"),
    limit: int = typer.Option(20, "--limit", help="Number of results"),
    offset: int = typer.Option(0, "--offset", help="Pagination offset"),
):
    """List escrow deals."""
    client = make_client()
    params = {"limit": limit, "offset": offset}
    if role:
        params["role"] = role
    if status:
        params["status"] = status
    data = client.get("/v2/escrow", params=params)
    print(format_success(data))
