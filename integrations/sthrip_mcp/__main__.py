"""Entry point: python -m sthrip_mcp [--sse] [--port PORT] [--host HOST]."""

import argparse

from .server import create_server


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Sthrip MCP Server — universal payment interface for AI agents",
    )
    parser.add_argument(
        "--sse",
        action="store_true",
        help="Use SSE transport instead of stdio (for remote agents)",
    )
    parser.add_argument(
        "--streamable-http",
        action="store_true",
        help="Use Streamable HTTP transport (recommended for production)",
    )
    parser.add_argument("--host", default="127.0.0.1", help="Host to bind (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8080, help="Port to bind (default: 8080)")

    args = parser.parse_args()
    mcp = create_server()

    if args.streamable_http:
        mcp.run(transport="streamable-http", host=args.host, port=args.port)
    elif args.sse:
        mcp.run(transport="sse", host=args.host, port=args.port)
    else:
        mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
