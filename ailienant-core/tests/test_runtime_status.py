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

import docker  # type: ignore[import-untyped]
import pytest
import requests  # type: ignore[import-untyped]
from fastapi import HTTPException

import api.runtime as runtime_mod
from api.runtime import (
    _probe_docker,
    get_runtime_status,
    pull_image,
    start_docker,
)


# ── helpers ───────────────────────────────────────────────────────────────────

def _reset_module_state() -> None:
    """Clear cached state between tests."""
    runtime_mod._docker_cache = {}
    runtime_mod._last_launch_time = 0.0
    runtime_mod._pull_in_progress = False


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
    """_probe_docker catches any exception from the daemon probe and returns False."""
    _reset_module_state()
    with patch("api.runtime.docker") as mock_docker:
        # Restore the real errors module so the granular except tuple holds
        # genuine exception classes (a bare MagicMock there is a TypeError).
        mock_docker.errors = docker.errors
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


# ── _probe_docker — deep health check (7.9.B.8) ──────────────────────────────

def _make_docker_client(info_side_effect: Any = None) -> MagicMock:
    """Mock a docker client whose .info() can raise or return."""
    client = MagicMock()
    if info_side_effect is not None:
        client.info.side_effect = info_side_effect
    else:
        client.info.return_value = {"ServerVersion": "27.0"}
    return client


def test_probe_docker_uses_info_not_ping() -> None:
    """A healthy engine: info() is queried (not ping) and the probe is True."""
    _reset_module_state()
    client = _make_docker_client()
    with patch("api.runtime.docker") as mock_docker:
        mock_docker.from_env.return_value = client
        result = asyncio.run(_probe_docker(force=True))
    assert result is True
    client.info.assert_called_once()
    client.ping.assert_not_called()


def test_probe_docker_apierror_returns_false() -> None:
    """A degraded engine raising APIError on info() → reachable False."""
    _reset_module_state()
    client = _make_docker_client(info_side_effect=docker.errors.APIError("engine broken"))
    with patch("api.runtime.docker") as mock_docker:
        mock_docker.errors = docker.errors
        mock_docker.from_env.return_value = client
        result = asyncio.run(_probe_docker(force=True))
    assert result is False


def test_probe_docker_connection_error_returns_false() -> None:
    """No daemon socket: ConnectionError on info() → reachable False."""
    _reset_module_state()
    client = _make_docker_client(info_side_effect=requests.exceptions.ConnectionError("no socket"))
    with patch("api.runtime.docker") as mock_docker:
        mock_docker.errors = docker.errors
        mock_docker.from_env.return_value = client
        result = asyncio.run(_probe_docker(force=True))
    assert result is False


def test_probe_docker_force_bypasses_cache() -> None:
    """A fresh True cache must NOT trap a now-degraded engine when force=True."""
    _reset_module_state()
    runtime_mod._docker_cache = {"reachable": True, "ts": time.monotonic()}
    client = _make_docker_client(info_side_effect=docker.errors.APIError("engine broken"))
    with patch("api.runtime.docker") as mock_docker:
        mock_docker.errors = docker.errors
        mock_docker.from_env.return_value = client
        result = asyncio.run(_probe_docker(force=True))
    assert result is False  # cache bypassed; live probe wins


def test_status_passes_force_to_probe() -> None:
    """get_runtime_status(force=True) forwards force to the probe."""
    _reset_module_state()
    probe = AsyncMock(return_value=False)
    with (
        patch("api.runtime.get_active_tier", return_value=None),
        patch("api.runtime._probe_docker", probe),
        patch("api.runtime._check_image_exists", new_callable=AsyncMock, return_value=False),
        patch("api.runtime._check_container_running", return_value=False),
    ):
        asyncio.run(get_runtime_status(force=True))
    probe.assert_awaited_once_with(force=True)


# ── POST /pull-image (7.9.B.8) ───────────────────────────────────────────────

def test_pull_image_success() -> None:
    """Daemon up + pull succeeds → pulled True, local image tag returned."""
    _reset_module_state()
    with (
        patch("api.runtime._probe_docker", new_callable=AsyncMock, return_value=True),
        patch("api.runtime.pull_sandbox_image", new_callable=AsyncMock) as mock_pull,
    ):
        result = asyncio.run(pull_image(_make_request()))
    assert result["pulled"] is True
    assert result["image"] == runtime_mod._SANDBOX_IMAGE_TAG
    mock_pull.assert_awaited_once()


def test_pull_image_docker_down() -> None:
    """Daemon unreachable → docker_down error, pull not attempted."""
    _reset_module_state()
    with (
        patch("api.runtime._probe_docker", new_callable=AsyncMock, return_value=False),
        patch("api.runtime.pull_sandbox_image", new_callable=AsyncMock) as mock_pull,
    ):
        result = asyncio.run(pull_image(_make_request()))
    assert result["pulled"] is False
    assert result["error"] == "docker_down"
    mock_pull.assert_not_awaited()


def test_pull_image_not_found() -> None:
    """Image absent from registry → image_not_found error."""
    _reset_module_state()
    with (
        patch("api.runtime._probe_docker", new_callable=AsyncMock, return_value=True),
        patch("api.runtime.pull_sandbox_image", new_callable=AsyncMock,
              side_effect=docker.errors.NotFound("no such image")),
    ):
        result = asyncio.run(pull_image(_make_request()))
    assert result["pulled"] is False
    assert result["error"] == "image_not_found"


def test_pull_image_no_connection() -> None:
    """No internet to the registry → no_connection error."""
    _reset_module_state()
    with (
        patch("api.runtime._probe_docker", new_callable=AsyncMock, return_value=True),
        patch("api.runtime.pull_sandbox_image", new_callable=AsyncMock,
              side_effect=requests.exceptions.ConnectionError("dns fail")),
    ):
        result = asyncio.run(pull_image(_make_request()))
    assert result["pulled"] is False
    assert result["error"] == "no_connection"


def test_pull_image_disk_full() -> None:
    """APIError mentioning no space → disk_full error."""
    _reset_module_state()
    with (
        patch("api.runtime._probe_docker", new_callable=AsyncMock, return_value=True),
        patch("api.runtime.pull_sandbox_image", new_callable=AsyncMock,
              side_effect=docker.errors.APIError("no space left on device")),
    ):
        result = asyncio.run(pull_image(_make_request()))
    assert result["pulled"] is False
    assert result["error"] == "disk_full"


def test_pull_image_in_progress() -> None:
    """A second concurrent pull is rejected without attempting another pull."""
    _reset_module_state()
    runtime_mod._pull_in_progress = True
    with (
        patch("api.runtime._probe_docker", new_callable=AsyncMock, return_value=True),
        patch("api.runtime.pull_sandbox_image", new_callable=AsyncMock) as mock_pull,
    ):
        result = asyncio.run(pull_image(_make_request()))
    assert result["pulled"] is False
    assert result["error"] == "in_progress"
    mock_pull.assert_not_awaited()


def test_pull_image_csrf_rejects_foreign_origin() -> None:
    """S7-D: pull-image from a foreign Origin raises HTTP 403."""
    _reset_module_state()
    req = _make_request(origin="https://evil.example.com")
    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(pull_image(req))
    assert exc_info.value.status_code == 403
