import typer
from sthrip.cli.core import app, make_client, run_command
from sthrip.cli.config import save_config
from sthrip.cli.output import format_success

@app.command("rotate-key")
@run_command
def rotate_key():
    """Rotate API key and update local credentials."""
    client = make_client()
    data = client.post("/v2/me/rotate-key")
    new_key = data.get("api_key", "")
    if new_key:
        save_config({"api_key": new_key})
    print(format_success(data))
