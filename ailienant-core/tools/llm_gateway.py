# ailienant-core/tools/llm_gateway.py

import logging
import re
import uuid
from typing import Optional

import litellm
from litellm import ModelResponse

from shared.config import MODEL_MEDIUM, get_litellm_config

logger = logging.getLogger("LLM_GATEWAY")

# Silence litellm's verbose default logging; our gateway owns the log surface.
litellm.suppress_debug_info = True

# Matches optional leading/trailing whitespace and markdown code fences (```json ... ``` or ``` ... ```).
_MD_FENCE_RE = re.compile(r"^\s*```(?:json)?\s*\n?(.*?)\n?```\s*$", re.DOTALL)


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
