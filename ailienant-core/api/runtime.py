# ailienant-core/api/runtime.py
"""Phase 7.9.B.7 — Runtime/Environment REST surface.

GET  /api/v1/runtime/status       — live sandbox tier + Docker daemon probe (5 s cache).
POST /api/v1/runtime/start-docker — platform-specific Docker Desktop launcher.

Security mitigations applied to POST /start-docker:
  S7-A — no user input in subprocess; shell=False (list argv always).
  S7-B — Windows paths resolved via os.environ expansion, NOT env-var strings
          ("%LOCALAPPDATA%" is NOT expanded by the OS when shell=False).
  S7-C — 30 s module-level cooldown serialises boot attempts (multi-launch DoS).
  S7-D — Origin header checked at application layer (CORS is allow_origins=["*"]).
"""
from __future__ import annotations

import asyncio
import logging
import os
import pathlib
import subprocess
import sys
import time
from typing import Any, Dict, Optional

import docker  # type: ignore[import-untyped]
import requests  # type: ignore[import-untyped]  # docker-py dependency (requirements.txt:49)
from fastapi import APIRouter, HTTPException, Request

from core.sandbox import (
    DockerSandboxAdapter,
    get_active_adapter,
    get_active_tier,
    pull_sandbox_image,
)

logger = logging.getLogger("AILIENANT_RUNTIME")

router = APIRouter(prefix="/api/v1/runtime", tags=["runtime"])

# ── constants ─────────────────────────────────────────────────────────────────

_SANDBOX_IMAGE_TAG: str = "ailienant-sandbox:latest"
_CACHE_TTL_S: float = 5.0
_PROBE_TIMEOUT_S: float = 2.0  # info() does more work than ping(); needs headroom
_LAUNCH_COOLDOWN_S: float = 30.0

_MODE_LABELS: Dict[Optional[str], str] = {
    "DOCKER": "Isolated Sandbox (Docker)",
    "WASM": "Wasm Sandbox",
    "NATIVE_HITL": "Host Mode (Unsafe)",
    None: "Uninitialized",
}

# S7-B: resolve Windows install paths via os.environ, not bare env-var strings.
# (shell=False means the OS never sees "%LOCALAPPDATA%" — it would search literally.)
_DOCKER_PATHS_WIN: list[pathlib.Path] = [
    pathlib.Path("C:/Program Files/Docker/Docker/Docker Desktop.exe"),
]
_local_appdata = os.environ.get("LOCALAPPDATA")
if _local_appdata:
    _DOCKER_PATHS_WIN.append(pathlib.Path(_local_appdata) / "Docker" / "Docker Desktop.exe")

# S7-D: Origin values the POST /start-docker endpoint accepts.
# Same-origin SPA requests from the dashboard send no Origin header at all (origin="") — those pass.
# Cross-origin CSRF attacks always include a foreign Origin → rejected.
# Phase 7.9.A.5.1: port is dynamic; read AILIENANT_API_PORT so the frozenset matches.
_API_PORT: int = int(os.environ.get("AILIENANT_API_PORT", "8000"))
_ALLOWED_ORIGINS: frozenset[str] = frozenset({
    f"http://localhost:{_API_PORT}",
    f"http://127.0.0.1:{_API_PORT}",
    "http://localhost",
    "http://127.0.0.1",
    "null",  # VS Code webview sandboxed context occasionally sends this
})

# ── module-level state ────────────────────────────────────────────────────────

_docker_cache: Dict[str, Any] = {}   # {"reachable": bool, "ts": float}
_last_launch_time: float = 0.0       # S7-C cooldown anchor
_pull_in_progress: bool = False      # serialises image pulls (no stacking)


# ── internal helpers ──────────────────────────────────────────────────────────

def _platform() -> str:
    if sys.platform == "win32":
        return "windows"
    if sys.platform == "darwin":
        return "macos"
    return "linux"


async def _probe_docker(force: bool = False) -> bool:
    """Deep Docker daemon health check, cached for _CACHE_TTL_S seconds.

    Uses client.info() rather than client.ping(): a degraded WSL2 / engine
    backend keeps answering ping() at the HTTP API layer even when the engine
    itself is broken. info() queries real engine state, so a degraded daemon
    makes it hang (caught by the 2 s timeout) or raise APIError.

    force=True bypasses the cache so a healthy→degraded transition is never
    trapped behind a stale True (frontend "Force Retry" + post-mutation refresh).
    """
    global _docker_cache
    now = time.monotonic()
    if not force:
        cached_ts: float = _docker_cache.get("ts", 0.0)  # type: ignore[assignment]
        if cached_ts and (now - cached_ts) < _CACHE_TTL_S:
            return bool(_docker_cache["reachable"])
    try:
        client = await asyncio.to_thread(docker.from_env)
        await asyncio.wait_for(asyncio.to_thread(client.info), timeout=_PROBE_TIMEOUT_S)
        _docker_cache = {"reachable": True, "ts": now}
        return True
    except (docker.errors.APIError, requests.exceptions.ConnectionError, TimeoutError) as exc:
        # Engine degraded (broken WSL2, daemon mid-shutdown) — treat as DOWN.
        # asyncio.wait_for raises asyncio.TimeoutError, which IS TimeoutError on 3.11+.
        logger.warning("[runtime] Docker engine degraded/unreachable: %s", type(exc).__name__)
        _docker_cache = {"reachable": False, "ts": now}
        return False
    except Exception as exc:  # noqa: BLE001 — any other failure → DOWN, never propagate
        logger.warning("[runtime] Docker probe failed: %s", type(exc).__name__)
        _docker_cache = {"reachable": False, "ts": now}
        return False


async def _check_image_exists(reachable: bool) -> bool:
    """Return True only when the daemon is up AND the sandbox image is present."""
    if not reachable:
        return False
    try:
        client = await asyncio.to_thread(docker.from_env)
        await asyncio.to_thread(client.images.get, _SANDBOX_IMAGE_TAG)
        return True
    except Exception:  # noqa: BLE001
        return False


def _check_container_running() -> bool:
    """Inspect the adapter's live container reference (no I/O if unstarted)."""
    adapter = get_active_adapter()
    if not isinstance(adapter, DockerSandboxAdapter):
        return False
    container = adapter._container  # noqa: SLF001 — private but same package
    if container is None:
        return False
    try:
        container.reload()
        return str(getattr(container, "status", "")) == "running"
    except Exception:  # noqa: BLE001
        return False


# ── endpoints ─────────────────────────────────────────────────────────────────

@router.get("/status")
async def get_runtime_status(force: bool = False) -> Dict[str, object]:
    """Live sandbox tier + Docker daemon reachability (5 s cache).

    force=true (query) bypasses the reachability cache — wired to the frontend
    "Force Retry" escape hatch so a stuck state can be re-probed on demand.
    """
    tier = get_active_tier()
    reachable = await _probe_docker(force=force)
    image = await _check_image_exists(reachable)
    running = _check_container_running()
    return {
        "tier": tier,
        "docker_reachable": reachable,
        "image_exists": image,
        "container_running": running,
        "mode_label": _MODE_LABELS.get(tier, "Unknown"),
    }


@router.post("/start-docker")
async def start_docker(request: Request) -> Dict[str, object]:
    """Launch Docker Desktop (or the Docker service) on the host machine.

    S7-A: fixed argv array, shell=False.
    S7-B: env-var paths resolved in Python.
    S7-C: 30 s cooldown; blocks if a launch is already in progress.
    S7-D: Origin header guard against cross-origin CSRF.
    """
    global _last_launch_time

    # S7-D — application-layer CSRF guard.
    origin = request.headers.get("origin", "")
    if origin and not (
        origin.startswith("vscode-webview://") or origin in _ALLOWED_ORIGINS
    ):
        raise HTTPException(status_code=403, detail="Forbidden")

    # Already running — no spawn needed.
    if await _probe_docker():
        return {
            "launched": False,
            "platform": _platform(),
            "message": "Docker daemon is already running.",
        }

    # S7-C — cooldown: serialise boot attempts; prevent multi-launch DoS.
    now = time.monotonic()
    if (now - _last_launch_time) < _LAUNCH_COOLDOWN_S:
        return {
            "launched": False,
            "platform": _platform(),
            "message": "Docker launch already in progress. Please wait 30 s.",
        }
    _last_launch_time = now  # stamp BEFORE Popen so a failed attempt still engages cooldown

    platform = _platform()
    try:
        if platform == "windows":
            # S7-A+B: iterate pre-resolved pathlib.Path objects; shell=False (list argv).
            for path in _DOCKER_PATHS_WIN:
                if path.is_file():
                    subprocess.Popen([str(path)])  # noqa: S603
                    logger.info("[runtime] start-docker launched on windows: %s", path.name)
                    return {
                        "launched": True,
                        "platform": platform,
                        "message": "Docker Desktop is launching. Please wait up to 30 s.",
                    }
            logger.warning("[runtime] start-docker: Docker Desktop not found at expected paths.")
            return {
                "launched": False,
                "platform": platform,
                "message": "Docker Desktop not found. Please start it manually.",
            }
        elif platform == "macos":
            subprocess.Popen(["open", "-a", "Docker"])  # noqa: S603
        else:  # linux
            subprocess.Popen(["systemctl", "--user", "start", "docker"])  # noqa: S603
        logger.info("[runtime] start-docker launched on %s.", platform)
        return {
            "launched": True,
            "platform": platform,
            "message": "Docker is launching. Please wait up to 30 s.",
        }
    except Exception:  # noqa: BLE001
        logger.exception("[runtime] start-docker failed on %s.", platform)
        return {
            "launched": False,
            "platform": platform,
            "message": "Could not launch Docker. Please start it manually.",
        }


@router.post("/pull-image")
async def pull_image(request: Request) -> Dict[str, object]:
    """Pull the pre-built sandbox image from the public registry (zero-config).

    Blocking SDK call offloaded via asyncio.to_thread (pull can take minutes).
    Structured errors let the client distinguish no-connection / not-found /
    disk-full. Generic surface: no raw OS/registry strings echoed to the client.
    """
    global _pull_in_progress

    # S7-D — same application-layer CSRF guard as start-docker.
    origin = request.headers.get("origin", "")
    if origin and not (
        origin.startswith("vscode-webview://") or origin in _ALLOWED_ORIGINS
    ):
        raise HTTPException(status_code=403, detail="Forbidden")

    if _pull_in_progress:
        return {
            "pulled": False,
            "error": "in_progress",
            "message": "A download is already in progress. Please wait.",
        }

    if not await _probe_docker():
        return {
            "pulled": False,
            "error": "docker_down",
            "message": "Docker daemon is not reachable. Start Docker first.",
        }

    _pull_in_progress = True
    try:
        await pull_sandbox_image()
        logger.info("[runtime] sandbox image pulled and tagged %s", _SANDBOX_IMAGE_TAG)
        return {
            "pulled": True,
            "image": _SANDBOX_IMAGE_TAG,
            "message": "Sandbox image downloaded successfully.",
        }
    except docker.errors.NotFound:
        logger.warning("[runtime] pull-image: image not found on registry.")
        return {
            "pulled": False,
            "error": "image_not_found",
            "message": "Sandbox image was not found on the registry.",
        }
    except requests.exceptions.ConnectionError:
        logger.warning("[runtime] pull-image: no connection to registry.")
        return {
            "pulled": False,
            "error": "no_connection",
            "message": "No internet connection to the image registry.",
        }
    except docker.errors.APIError as exc:
        detail = str(exc).lower()
        if "no space" in detail or "disk" in detail:
            logger.error("[runtime] pull-image: disk full.")
            return {
                "pulled": False,
                "error": "disk_full",
                "message": "Not enough disk space to download the image.",
            }
        logger.exception("[runtime] pull-image: registry/API error.")
        return {
            "pulled": False,
            "error": "registry_error",
            "message": "Registry error while downloading the image.",
        }
    except Exception:  # noqa: BLE001
        logger.exception("[runtime] pull-image: unexpected failure.")
        return {
            "pulled": False,
            "error": "unknown",
            "message": "Could not download the sandbox image. See the manual fallback.",
        }
    finally:
        _pull_in_progress = False
