# ailienant-core/tools/llm_gateway.py

import logging
import re
import time
import uuid
from enum import Enum
from typing import Any, Optional

import httpx
import litellm
from litellm import ModelResponse

from shared.config import (
    MODEL_SMALL,
    MODEL_MEDIUM,
    MODEL_BIG,
    LITELLM_PROXY_BASE_URL,
    get_litellm_config,
    check_cloud_availability,
)

logger = logging.getLogger("LLM_GATEWAY")

# Silence litellm's verbose default logging; our gateway owns the log surface.
litellm.suppress_debug_info = True

# Matches optional leading/trailing whitespace and markdown code fences (```json ... ``` or ``` ... ```).
_MD_FENCE_RE = re.compile(r"^\s*```(?:json)?\s*\n?(.*?)\n?```\s*$", re.DOTALL)


class NoAvailableProviderError(RuntimeError):
    """Raised when all LLM tiers (LOCAL + CLOUD) are unreachable and no fallback exists."""


class TaskPriority(str, Enum):
    """Routing outcome from RoutingEngine.get_optimal_provider(), used to select model tier."""
    LOCAL = "LOCAL"
    CLOUD = "CLOUD"
    HUMAN_REQUIRED = "HUMAN_REQUIRED"


_PRIORITY_MODEL_MAP: dict[TaskPriority, str] = {
    TaskPriority.LOCAL: MODEL_SMALL,
    TaskPriority.CLOUD: MODEL_BIG,
}

# -------------------------------------------------------------------------
# Heartbeat cache: {url: (is_alive, expiry_monotonic_time)}
# Avoids hammering endpoints on every routing decision.
# -------------------------------------------------------------------------
_heartbeat_cache: dict[str, tuple[bool, float]] = {}
_HEARTBEAT_TTL: float = 60.0

# Known cloud API health-check endpoints (lightweight, no auth required)
_CLOUD_HEALTH_URLS: list[str] = [
    "https://api.openai.com",
    "https://api.anthropic.com",
]


class LLMGateway:
    """
    Unified client for all agent LLM calls.

    Routes every request through the local LiteLLM proxy (localhost:4000),
    which handles provider translation, fallbacks, and API key management.
    Agents pass abstract model aliases (ailienant/small, /medium, /big);
    the proxy resolves them to real models without touching application code.
    """

    @staticmethod
    def _sanitize_json_response(content: str) -> str:
        """Strip markdown code fences and surrounding whitespace from an LLM response.

        Some models wrap JSON output in ```json ... ``` regardless of response_format.
        This normalises the string so model_validate_json never sees the fences.
        """
        match = _MD_FENCE_RE.match(content)
        return match.group(1).strip() if match else content.strip()

    @staticmethod
    def invoke(
        messages: list[dict],
        model: str = MODEL_MEDIUM,
        temperature: float = 0.0,
        response_format: Optional[dict] = None,
        max_tokens: int = 4096,
        timeout: float = 60.0,
        session_id: Optional[str] = None,
    ) -> ModelResponse:
        """Synchronous LLM call. Prefer ainvoke() inside async contexts."""
        trace_id = session_id or str(uuid.uuid4())
        cfg = get_litellm_config()
        logger.debug(
            "LLM invoke — model=%s base_url=%s trace=%s", model, cfg["base_url"], trace_id
        )
        try:
            kwargs: dict = dict(
                model=model,
                messages=messages,
                temperature=temperature,
                max_tokens=max_tokens,
                timeout=timeout,
                max_retries=2,
                metadata={"session_id": trace_id},
                extra_headers={"X-Ailienant-Trace-ID": trace_id},
                **cfg,
            )
            if response_format:
                kwargs["response_format"] = response_format
            return litellm.completion(**kwargs)
        except Exception as e:
            logger.error("LLM invoke failed [trace=%s]: %s", trace_id, e)
            raise

    @staticmethod
    async def ainvoke(
        messages: list[dict],
        model: str = MODEL_MEDIUM,
        temperature: float = 0.0,
        response_format: Optional[dict] = None,
        max_tokens: int = 4096,
        timeout: float = 60.0,
        session_id: Optional[str] = None,
    ) -> ModelResponse:
        """Async LLM call — non-blocking on the FastAPI event loop."""
        trace_id = session_id or str(uuid.uuid4())
        cfg = get_litellm_config()
        logger.debug(
            "LLM ainvoke — model=%s base_url=%s trace=%s", model, cfg["base_url"], trace_id
        )
        try:
            kwargs: dict = dict(
                model=model,
                messages=messages,
                temperature=temperature,
                max_tokens=max_tokens,
                timeout=timeout,
                max_retries=2,
                metadata={"session_id": trace_id},
                extra_headers={"X-Ailienant-Trace-ID": trace_id},
                **cfg,
            )
            if response_format:
                kwargs["response_format"] = response_format
            return await litellm.acompletion(**kwargs)
        except Exception as e:
            logger.error("LLM ainvoke failed [trace=%s]: %s", trace_id, e)
            raise

    @staticmethod
    async def heartbeat(url: str) -> bool:
        """Async HEAD request to *url* with a 5s timeout, result cached for 60s.

        Returns True if the server responds with any status < 500 (including auth
        errors like 401/403 — the server is reachable, just requires credentials).
        Returns False on any network error or timeout.
        """
        now = time.monotonic()
        cached = _heartbeat_cache.get(url)
        if cached is not None and now < cached[1]:
            return cached[0]

        alive = False
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.head(url)
                alive = resp.status_code < 500
        except Exception:
            alive = False

        _heartbeat_cache[url] = (alive, now + _HEARTBEAT_TTL)
        logger.debug("heartbeat %s → alive=%s (cached %ds)", url, alive, int(_HEARTBEAT_TTL))
        return alive

    @staticmethod
    async def get_active_tiers() -> set[str]:
        """Discover which LLM tiers are currently reachable.

        Returns a set containing any of: {"LOCAL", "CLOUD"}.
        An empty set means no tier is available — callers should raise
        NoAvailableProviderError rather than proceeding with a doomed request.

        Strategy:
        - LOCAL: probe the LiteLLM proxy (LITELLM_PROXY_BASE_URL)
        - CLOUD: fast env-var pre-check (check_cloud_availability) THEN heartbeat
                 at least one cloud endpoint — avoids network I/O if no keys are set.
        """
        active: set[str] = set()

        if await LLMGateway.heartbeat(LITELLM_PROXY_BASE_URL):
            active.add("LOCAL")

        if check_cloud_availability():
            for url in _CLOUD_HEALTH_URLS:
                if await LLMGateway.heartbeat(url):
                    active.add("CLOUD")
                    break  # One reachable cloud endpoint is sufficient

        logger.info("Active LLM tiers: %s", active or "NONE")
        return active

    @staticmethod
    async def ainvoke_by_priority(
        priority: TaskPriority,
        messages: list[dict],
        session_id: Optional[str] = None,
        **kwargs: Any,
    ) -> ModelResponse:
        """Select model tier by TaskPriority and delegate to ainvoke().

        Raises ValueError for HUMAN_REQUIRED so the caller can route to the HITL gate
        instead of accidentally firing an LLM call with no valid model.

        For LOCAL priority, injects Ollama keep_alive via extra_body — agents should
        call vfs_manager.broadcast_model_warmup() before this if a warmup may occur.
        """
        if priority == TaskPriority.HUMAN_REQUIRED:
            raise ValueError("HUMAN_REQUIRED: routing deferred to HITL gate — no LLM call made")
        model = _PRIORITY_MODEL_MAP[priority]
        if priority == TaskPriority.LOCAL:
            from brain.routing_engine import RoutingEngine
            keep_alive = RoutingEngine.get_keep_alive(model)
            kwargs["extra_body"] = {**kwargs.get("extra_body", {}), "keep_alive": keep_alive}
        return await LLMGateway.ainvoke(
            messages=messages, model=model, session_id=session_id, **kwargs
        )
