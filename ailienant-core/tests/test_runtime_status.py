"""Phase 7.9.B.7 — Runtime/Environment REST surface tests.

Covers:
  - GET /api/v1/runtime/status shape (tier, reachability, mode_label).
  - _probe_docker: timeout and import-error paths return False (internal catch).
  - POST /api/v1/runtime/start-docker:
      S7-C: cooldown blocks rapid second call.
      S7-C: already-running guard (no spawn).
      S7-A/B: generic error message on Popen failure (OS detail not echoed).
      S7-D: CSRF Origin check rejects foreign origin / allows vscode-webview://.

All async cases use asyncio.run (no pytest-asyncio dependency).
"""
from __future__ import annotations

import asyncio
import time
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

import api.runtime as runtime_mod
from api.runtime import (
    _probe_docker,
    get_runtime_status,
    start_docker,
)


# ── helpers ───────────────────────────────────────────────────────────────────

def _reset_module_state() -> None:
    """Clear cached state between tests."""
    runtime_mod._docker_cache = {}
    runtime_mod._last_launch_time = 0.0


def _make_request(origin: str = "") -> Any:
    """Create a minimal mock of fastapi.Request with the given Origin header.

    Deliberately does NOT assign req.headers = dict(...), because that would
    replace the MagicMock with a real dict whose .get attribute is read-only.
    Instead we configure .get on the MagicMock directly.
    """
    req = MagicMock()
    headers_data = {"origin": origin} if origin else {}
    req.headers.get = lambda key, default="": headers_data.get(key, default)
    return req


# ── GET /status ───────────────────────────────────────────────────────────────

def test_status_shape_when_sandbox_uninitialised() -> None:
    """All expected keys are present; tier=None and docker_reachable=False when unstarted."""
    _reset_module_state()
    with (
        patch("api.runtime.get_active_tier", return_value=None),
        patch("api.runtime._probe_docker", new_callable=AsyncMock, return_value=False),
        patch("api.runtime._check_image_exists", new_callable=AsyncMock, return_value=False),
        patch("api.runtime._check_container_running", return_value=False),
    ):
        result = asyncio.run(get_runtime_status())
    assert {"tier", "docker_reachable", "image_exists", "container_running", "mode_label"} <= set(result)
    assert result["tier"] is None
    assert result["docker_reachable"] is False
    assert result["mode_label"] == "Uninitialized"


def test_status_docker_tier_with_mock_ping() -> None:
    """DOCKER tier + successful ping → docker_reachable=True, correct mode_label."""
    _reset_module_state()
    with (
        patch("api.runtime.get_active_tier", return_value="DOCKER"),
        patch("api.runtime._probe_docker", new_callable=AsyncMock, return_value=True),
        patch("api.runtime._check_image_exists", new_callable=AsyncMock, return_value=True),
        patch("api.runtime._check_container_running", return_value=False),
    ):
        result = asyncio.run(get_runtime_status())
    assert result["tier"] == "DOCKER"
    assert result["docker_reachable"] is True
    assert result["mode_label"] == "Isolated Sandbox (Docker)"


def test_status_docker_reachable_false_propagates() -> None:
    """When _probe_docker returns False (e.g. timeout internally), status reflects it."""
    _reset_module_state()
    with (
        patch("api.runtime.get_active_tier", return_value="DOCKER"),
        patch("api.runtime._probe_docker", new_callable=AsyncMock, return_value=False),
        patch("api.runtime._check_image_exists", new_callable=AsyncMock, return_value=False),
        patch("api.runtime._check_container_running", return_value=False),
    ):
        result = asyncio.run(get_runtime_status())
    assert result["docker_reachable"] is False
    assert result["image_exists"] is False


def test_probe_docker_returns_false_on_exception() -> None:
    """_probe_docker catches any exception from the daemon ping and returns False."""
    _reset_module_state()
    with patch("api.runtime.docker") as mock_docker:
        mock_docker.from_env.side_effect = Exception("daemon unreachable")
        result = asyncio.run(_probe_docker())
    assert result is False


# ── POST /start-docker — S7-C (cooldown + already-running) ───────────────────

def test_start_docker_skips_if_already_running() -> None:
    """S7-C: if Docker is already running, return early without spawning."""
    _reset_module_state()
    with (
        patch("api.runtime._probe_docker", new_callable=AsyncMock, return_value=True),
        patch("subprocess.Popen") as mock_popen,
    ):
        result = asyncio.run(start_docker(_make_request()))
    assert result["launched"] is False
    assert "already running" in str(result["message"]).lower()
    mock_popen.assert_not_called()


def test_start_docker_cooldown_blocks_second_call() -> None:
    """S7-C: a second call within _LAUNCH_COOLDOWN_S is rejected without spawning."""
    _reset_module_state()
    runtime_mod._last_launch_time = time.monotonic()  # simulate recent launch
    with (
        patch("api.runtime._probe_docker", new_callable=AsyncMock, return_value=False),
        patch("subprocess.Popen") as mock_popen,
    ):
        result = asyncio.run(start_docker(_make_request()))
    assert result["launched"] is False
    assert "in progress" in str(result["message"]).lower()
    mock_popen.assert_not_called()


# ── POST /start-docker — S7-A/B (generic error) ──────────────────────────────

def test_start_docker_generic_error_on_popen_failure() -> None:
    """S7-A/B: OS error detail must NOT be echoed; only the generic message is returned."""
    _reset_module_state()
    secret_detail = "secret/internal/path/Docker Desktop.exe not found"
    with (
        patch("api.runtime._probe_docker", new_callable=AsyncMock, return_value=False),
        patch("api.runtime._platform", return_value="macos"),
        patch("subprocess.Popen", side_effect=FileNotFoundError(secret_detail)),
    ):
        result = asyncio.run(start_docker(_make_request()))
    assert result["launched"] is False
    assert secret_detail not in str(result["message"]), "OS error detail must NOT be echoed to client"
    assert "manually" in str(result["message"]).lower()


# ── POST /start-docker — S7-D (CSRF Origin check) ────────────────────────────

def test_start_docker_csrf_rejects_foreign_origin() -> None:
    """S7-D: a POST from a foreign Origin raises HTTP 403."""
    _reset_module_state()
    req = _make_request(origin="https://evil.example.com")
    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(start_docker(req))
    assert exc_info.value.status_code == 403


def test_start_docker_csrf_allows_vscode_webview() -> None:
    """S7-D: vscode-webview:// origin is allowed (no 403 raised)."""
    _reset_module_state()
    req = _make_request(origin="vscode-webview://abc123def456")
    with (
        patch("api.runtime._probe_docker", new_callable=AsyncMock, return_value=False),
        patch("api.runtime._platform", return_value="linux"),
        patch("subprocess.Popen"),
    ):
        result = asyncio.run(start_docker(req))
    assert "launched" in result


def test_start_docker_csrf_allows_no_origin_header() -> None:
    """S7-D: absent Origin header (same-origin SPA) is always allowed."""
    _reset_module_state()
    req = _make_request(origin="")  # no Origin header
    with (
        patch("api.runtime._probe_docker", new_callable=AsyncMock, return_value=False),
        patch("api.runtime._platform", return_value="linux"),
        patch("subprocess.Popen"),
    ):
        result = asyncio.run(start_docker(req))
    assert "launched" in result
