import asyncio
import time
from typing import Any, Dict, Optional

from fastapi import APIRouter
from shared.hardware import HardwareDetector, HardwareProfile
from core.execution_mode import get_effort_level, set_effort_level

router = APIRouter(prefix="/api/v1/hardware", tags=["hardware"])

_cache: tuple[float, HardwareProfile] | None = None
_CACHE_TTL = 3.0
_cache_lock = asyncio.Lock()


async def _get_profile() -> HardwareProfile:
    """Cache-protected hardware detection. Lock serialises concurrent cache refreshes."""
    global _cache
    async with _cache_lock:
        now = time.monotonic()
        if _cache and (now - _cache[0]) < _CACHE_TTL:
            return _cache[1]
        profile = await asyncio.to_thread(HardwareDetector.detect)
        _cache = (now, profile)
        return profile


@router.get("/profile")
async def get_hardware_profile() -> HardwareProfile:
    return await _get_profile()


def _current_local_model() -> Optional[str]:
    """Bare model string for the active local chat target, or None when no
    BYOM preset resolves one — the cost estimate then falls back to the
    static assumption rather than guessing a model to calibrate against."""
    try:
        from core.config.model_resolver import get_chat_target
        target = get_chat_target("big")
        return target.model if target is not None and target.is_local else None
    except Exception:  # noqa: BLE001 — a resolution fault degrades to the static estimate
        return None


@router.get("/mode")
async def get_effort_mode() -> Dict[str, Any]:
    from tools.llm_gateway import estimate_effort_costs

    return {
        "mode": get_effort_level(),
        "cost_estimates": estimate_effort_costs(_current_local_model()),
    }


@router.post("/mode")
async def set_effort_mode(body: Dict[str, Any]) -> Dict[str, Any]:
    from tools.llm_gateway import estimate_effort_costs

    set_effort_level(str(body.get("mode", "balanced")))
    return {
        "mode": get_effort_level(),
        "cost_estimates": estimate_effort_costs(_current_local_model()),
    }
