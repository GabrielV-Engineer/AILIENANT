"""Shared fixtures for the end-to-end suite.

The E2E cases exercise the real ASGI app over real HTTP/WebSocket. They run as
synchronous tests so Starlette's TestClient drives the app on its own blocking
portal thread (a separate event loop), letting background graph/transport
coroutines progress while the test thread drives the socket — no shared loop to
deadlock.
"""
from __future__ import annotations

import pytest
from starlette.testclient import TestClient


@pytest.fixture()
def e2e_client():
    """A TestClient bound to the real application, with lifespan started."""
    import main

    with TestClient(main.app) as client:
        yield client
