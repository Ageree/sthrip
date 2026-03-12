import functools
import sys
from typing import Optional

import typer

from cli.agent_cli.client import StrhipClient, CliError
from cli.agent_cli.config import resolve_api_key, resolve_base_url
from cli.agent_cli.output import format_error

app = typer.Typer(add_completion=False, pretty_exceptions_enable=False)


class _State:
    def __init__(self):
        self.url: Optional[str] = None
        self.timeout: int = 30
        self.debug: bool = False


state = _State()


@app.callback()
def main(
    url: Optional[str] = typer.Option(None, "--url", help="API base URL override"),
    timeout: int = typer.Option(30, "--timeout", help="Request timeout in seconds"),
    debug: bool = typer.Option(False, "--debug", help="Log HTTP details to stderr"),
):
    state.url = url
    state.timeout = timeout
    state.debug = debug


def make_client(require_auth: bool = True) -> StrhipClient:
    api_key = resolve_api_key()
    if require_auth and not api_key:
        print(
            format_error("No API key found. Run 'sthrip register' first or set STHRIP_API_KEY.", 2),
            file=sys.stderr,
        )
        raise typer.Exit(code=2)
    return StrhipClient(
        base_url=resolve_base_url(flag_url=state.url),
        api_key=api_key,
        timeout=state.timeout,
        debug=state.debug,
    )


def run_command(fn):
    """Decorator that catches CliError and prints JSON error to stderr.
    Uses functools.wraps to preserve function signature for Typer."""
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except CliError as e:
            print(format_error(str(e), e.exit_code), file=sys.stderr)
            raise typer.Exit(code=e.exit_code)
    return wrapper
