import os
import typer
from cli.agent_cli.core import app, make_client, run_command
from cli.agent_cli.config import CREDENTIALS_PATH, load_config, resolve_base_url
from cli.agent_cli.output import format_success

config_app = typer.Typer(add_completion=False, pretty_exceptions_enable=False)
app.add_typer(config_app, name="config")

@app.command()
@run_command
def health():
    """Check API health."""
    client = make_client(require_auth=False)
    data = client.get("/health")
    print(format_success(data))

@config_app.command("show")
@run_command
def config_show():
    """Show resolved configuration (no secrets exposed)."""
    env_key = os.environ.get("STHRIP_API_KEY")
    file_config = load_config()
    file_key = file_config.get("api_key")

    if env_key:
        source = "env"
    elif file_key:
        source = "file"
    else:
        source = "none"

    data = {
        "base_url": resolve_base_url(),
        "credentials_file": CREDENTIALS_PATH,
        "api_key_source": source,
        "agent_name": file_config.get("agent_name"),
    }
    print(format_success(data))
