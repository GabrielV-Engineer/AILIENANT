"""Phase 7.9.B.6 — Dashboard segments backend: telemetry read path + MCP guard.

Covers the new read-only telemetry helpers (secret masking S1, pagination
clamp S4, ReDoS-safe truncation S5, OFFSET hard cap S6) and the MCP command
allowlist guard (S2). Async cases run via ``asyncio.run`` (no pytest-asyncio).
"""
import asyncio
import time
from typing import Any

import pytest

from api import mcp_servers as mcp_api
from api.mcp_servers import _POLICY_ERROR
from core import telemetry


# --------------------------------------------------------------------------
# Telemetry read path
# --------------------------------------------------------------------------

def _init_telemetry(tmp_path: Any) -> None:
    telemetry.shutdown_telemetry_db()
    telemetry.init_telemetry_db(tmp_path / "telemetry_test.sqlite")


def test_read_helpers_empty_when_db_uninitialised() -> None:
    telemetry.shutdown_telemetry_db()  # force _conn = None
    assert telemetry.recent_routing_decisions() == []
    assert telemetry.recent_oom_events() == []


def test_routing_read_returns_rows(tmp_path: Any) -> None:
    _init_telemetry(tmp_path)
    try:
        telemetry.log_routing_decision("sess1", "planner", "coder", "normal flow", css=0.5, tci=0.2)
        rows = telemetry.recent_routing_decisions()
        assert len(rows) == 1
        row = rows[0]
        assert row["source_node"] == "planner"
        assert row["target_node"] == "coder"
        assert {"id", "timestamp", "reason", "css_score", "tci_score"} <= set(row)
    finally:
        telemetry.shutdown_telemetry_db()


def test_routing_reason_is_secret_masked(tmp_path: Any) -> None:
    """S1: a planted secret in `reason` must be redacted before it leaves the server."""
    _init_telemetry(tmp_path)
    try:
        telemetry.log_routing_decision(
            "sess1", "planner", "coder",
            "user pasted password = hunter2supersecret and key sk-ABCDEFGH12345678",
        )
        reason = telemetry.recent_routing_decisions()[0]["reason"] or ""
        assert "hunter2supersecret" not in reason
        assert "sk-ABCDEFGH12345678" not in reason
        assert "REDACTED" in reason
    finally:
        telemetry.shutdown_telemetry_db()


def test_mask_is_redos_safe_on_huge_input() -> None:
    """S5: masking a 1 MB string returns quickly (input is truncated first)."""
    huge = "a" * 1_000_000 + " password = leak"
    start = time.perf_counter()
    masked = telemetry._mask_sensitive(huge)
    elapsed = time.perf_counter() - start
    assert elapsed < 1.0
    assert masked is not None and len(masked) <= telemetry._MASK_INPUT_CAP + 1  # +1 for the "…"


def test_pagination_is_clamped() -> None:
    """S4+S6: limit clamps to 1..200, offset clamps to 0..hard-cap."""
    assert telemetry._clamp_pagination(99_999, 99_999) == (telemetry._LIMIT_MAX, telemetry._OFFSET_HARD_CAP)
    assert telemetry._clamp_pagination(-5, -5) == (1, 0)
    assert telemetry._clamp_pagination(50, 0) == (50, 0)


# --------------------------------------------------------------------------
# MCP command allowlist (S2)
# --------------------------------------------------------------------------

def test_mcp_allowlist_accepts_known_launchers() -> None:
    # Should not raise.
    mcp_api._validate_mcp_command("stdio:///usr/bin/python")
    mcp_api._validate_mcp_command("stdio:///opt/node/bin/node?arg=server.js")
    mcp_api._validate_mcp_command("stdio:///bare/path/npx")


def test_mcp_allowlist_rejects_non_allowlisted() -> None:
    with pytest.raises(ValueError, match=_POLICY_ERROR):
        mcp_api._validate_mcp_command("stdio:///bin/bash")


def test_mcp_allowlist_rejects_path_traversal_even_if_basename_ok() -> None:
    with pytest.raises(ValueError, match=_POLICY_ERROR):
        mcp_api._validate_mcp_command("stdio:///../../bin/python")


def test_mcp_test_endpoint_rejects_non_allowlisted_with_generic_error() -> None:
    result = asyncio.run(mcp_api.test_server({"uri": "stdio:///bin/bash"}))
    assert result["reachable"] is False
    assert result["tool_count"] == 0
    assert result["error"] == _POLICY_ERROR  # generic — no payload echoed


def test_mcp_save_endpoint_rejects_non_allowlisted() -> None:
    result = asyncio.run(mcp_api.save_server({"name": "evil", "uri": "stdio:///bin/bash"}))
    assert result["ok"] is False
    assert result["error"] == _POLICY_ERROR
