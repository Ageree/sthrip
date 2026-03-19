"""Entry point — imports core + all command modules, re-exports app."""
from cli.agent_cli.core import app  # noqa: F401

from cli.agent_cli.commands import register as _register  # noqa: F401
from cli.agent_cli.commands import balance as _balance  # noqa: F401
from cli.agent_cli.commands import payments as _payments  # noqa: F401
from cli.agent_cli.commands import agents as _agents  # noqa: F401
from cli.agent_cli.commands import me as _me  # noqa: F401
from cli.agent_cli.commands import keys as _keys  # noqa: F401
from cli.agent_cli.commands import webhooks as _webhooks  # noqa: F401
from cli.agent_cli.commands import health as _health  # noqa: F401
from cli.agent_cli.commands import escrow as _escrow  # noqa: F401
