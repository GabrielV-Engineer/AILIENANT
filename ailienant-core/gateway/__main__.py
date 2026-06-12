"""Entry point for ``python -m gateway`` — runs the gateway over stdio.

The server boots standalone and answers ``list_tools`` from the static catalog with
no connection to the host. A live host is required only when an EXECUTE-tier verb
later needs the loopback substrate.
"""
from __future__ import annotations

import asyncio

from mcp.server.stdio import stdio_server

from gateway.server import build_gateway_server


async def _run() -> None:
    server = build_gateway_server()
    async with stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            server.create_initialization_options(),
        )


def main() -> None:
    asyncio.run(_run())


if __name__ == "__main__":
    main()
