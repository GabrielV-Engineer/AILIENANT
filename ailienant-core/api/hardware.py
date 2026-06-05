import asyncio
import time
from typing import Any, Dict

from fastapi import APIRouter
from shared.hardware import HardwareDetector, HardwareProfile
from core.execution_mode import get_mode, set_mode

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


@router.get("/mode")
async def get_execution_mode() -> Dict[str, Any]:
    profile = await _get_profile()
    return {"mode": get_mode(), "suggested": profile.suggested_mode}


@router.post("/mode")
async def set_execution_mode(body: Dict[str, Any]) -> Dict[str, Any]:
    mode = body.get("mode", "AUTO")
    set_mode(mode)
    profile = await _get_profile()
    return {"mode": get_mode(), "suggested": profile.suggested_mode}
