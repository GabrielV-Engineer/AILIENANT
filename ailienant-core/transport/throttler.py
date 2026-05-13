# ailienant-core/transport/throttler.py
#
# Phase 2.2.A — WebSocket Backpressure Guard.
#
# Prevents server-side OOM when the IDE client is slow to consume the token stream.
# Wraps an async token generator and pauses it whenever the underlying asyncio
# transport's write buffer exceeds _WRITE_BUFFER_HIGH_WATER bytes.
#
# Usage (Phase 4 token streaming handler in main.py):
#
#   async for token in throttled_stream(LLMGateway.astream(...), websocket):
#       await vfs_manager.broadcast_token(client_id, token)

from __future__ import annotations

import asyncio
import logging
from typing import AsyncIterator

from fastapi import WebSocket

logger = logging.getLogger("STREAM_THROTTLER")

_WRITE_BUFFER_HIGH_WATER: int = 1_048_576  # 1 MB — pause threshold
_THROTTLE_INTERVAL_S: float = 0.05         # 50 ms poll interval when buffer is full


def _get_write_buffer_size(websocket: WebSocket) -> int:
    """Inspect the asyncio transport write-buffer size for a uvicorn WebSocket.

    Traverses the uvicorn-internal connection path to reach the asyncio transport.
    Falls back to 0 (no throttling) if the path is unavailable — e.g., under test
    with a mock WebSocket, or after a uvicorn version bump changes the internal API.

    The accessed path is:
        websocket.scope["awsgi.interfaces"]["websocket"]._connection.transport
    which is stable across uvicorn 0.20–0.30 but is NOT part of the public ASGI spec.
    """
    try:
        transport = (
            websocket.scope["awsgi.interfaces"]["websocket"]
            ._connection
            .transport
        )
        return transport.get_write_buffer_size()
    except (KeyError, AttributeError):
        return 0


async def throttled_stream(
    gen: AsyncIterator[str],
    websocket: WebSocket,
) -> AsyncIterator[str]:
    """Wrap a token-stream generator with write-buffer backpressure control.

    Yields each token from *gen* unchanged. Before yielding, checks the
    WebSocket transport's write-buffer size. If it exceeds
    _WRITE_BUFFER_HIGH_WATER, the generator is paused (asyncio.sleep loop)
    until the buffer drains below the threshold.

    No tokens are dropped — the generator simply stalls until the client
    catches up. This prevents the server's asyncio event loop from
    accumulating unbounded in-memory queues for a slow IDE.
    """
    async for token in gen:
        while True:
            buf_size = _get_write_buffer_size(websocket)
            if buf_size <= _WRITE_BUFFER_HIGH_WATER:
                break
            logger.debug(
                "Throttling: WebSocket write buffer %.1f KB > %.1f KB — pausing stream.",
                buf_size / 1024,
                _WRITE_BUFFER_HIGH_WATER / 1024,
            )
            await asyncio.sleep(_THROTTLE_INTERVAL_S)
        yield token
