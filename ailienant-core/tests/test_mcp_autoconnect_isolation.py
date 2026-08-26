# ailienant-core/tests/test_mcp_autoconnect_isolation.py
"""DEBT-202 — tests must never autoconnect to the real, persistent MCP catalog.

`tests/conftest.py::_isolate_mcp_autoconnect` (autouse) patches
`autoconnect_enabled_mcp_servers` at both of its call sites so no test — in
particular anything building `TestClient(main.app)` — ever reads the real
`~/.ailienant/catalog.sqlite` or tries to connect a real, possibly
network-dependent MCP server during its lifespan. Before this fixture existed,
that real connection attempt could fail and trip a genuine anyio task-group
teardown bug in the `mcp` SDK, surfacing as a `RuntimeError: Attempted to exit
a cancel scope...` in whichever unrelated test happened to run next.

These tests prove the guard is actually active (not just present) by spying on
`core.db.list_mcp_servers` — the one function every real code path underneath
`autoconnect_enabled_mcp_servers` must call to read the catalog. If the guard
were absent, both call sites below would invoke it for real.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest


@pytest.mark.anyio
async def test_main_lifespan_call_site_is_isolated() -> None:
    """main.py's own binding (`from tools.mcp_adapter import ...` at module load
    time) must be patched directly — patching tools.mcp_adapter's attribute
    alone would never reach it, since the import already copied the reference."""
    import main

    with patch("core.db.list_mcp_servers", new=AsyncMock(return_value=[])) as spy:
        result = await main.autoconnect_enabled_mcp_servers()

    assert result == 0
    spy.assert_not_called()


@pytest.mark.anyio
async def test_task_service_call_site_is_isolated() -> None:
    """core/task_service.py re-imports the name fresh inside its own function
    body on every call, so patching the shared tools.mcp_adapter attribute is
    what reaches this call site."""
    from tools import mcp_adapter

    with patch("core.db.list_mcp_servers", new=AsyncMock(return_value=[])) as spy:
        result = await mcp_adapter.autoconnect_enabled_mcp_servers({})

    assert result == 0
    spy.assert_not_called()


@pytest.fixture()
def anyio_backend() -> str:
    return "asyncio"
