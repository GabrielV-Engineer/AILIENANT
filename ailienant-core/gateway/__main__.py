"""Entry point for ``python -m gateway`` — runs the gateway over stdio.

The server boots standalone and answers ``list_tools`` from the static catalog with
no connection to the host. A live host is required only when an EXECUTE-tier verb
later needs the loopback substrate.
"""
from __future__ import annotations

import asyncio
import logging
import os

from mcp.server.stdio import stdio_server

from gateway.catalog import PROTOCOL_VERSION
from gateway.server import build_gateway_server

logger = logging.getLogger("GATEWAY")


async def _run() -> None:
    # A token-derived caller gets its own durable ceilings; without one, calls fall
    # into the shared anonymous pool. The mask is a pure boolean check — the token
    # value is never read into the log.
    auth_mode = "Token-Secured" if os.environ.get("AILIENANT_GATEWAY_TOKEN") else "Anonymous (Shared Pool)"
    logger.info("AILIENANT gateway online — protocol v%s, auth: %s", PROTOCOL_VERSION, auth_mode)
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
