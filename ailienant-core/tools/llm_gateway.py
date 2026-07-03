# ailienant-core/tools/llm_gateway.py

import asyncio
import json
import logging
import os
import re
import time
import uuid
import weakref
from enum import Enum
from typing import (
    TYPE_CHECKING, Any, AsyncIterator, Awaitable, Callable, Dict, List, Optional, Type, cast,
)

if TYPE_CHECKING:
    from tools.stream_delta import StreamDelta

import httpx
import litellm
from litellm import CustomStreamWrapper, ModelResponse
from litellm.exceptions import APIConnectionError, ContextWindowExceededError
from pydantic import BaseModel

from brain.retry_policy import LLM_MAX_TRANSPORT_RETRIES
from shared.config import (
    MODEL_SMALL,
    MODEL_MEDIUM,
    MODEL_BIG,
    LITELLM_PROXY_BASE_URL,
    LLM_MAX_CONCURRENCY,
    get_litellm_config,
    check_cloud_availability,
)

logger = logging.getLogger("LLM_GATEWAY")

# Silence litellm's verbose default logging; our gateway owns the log surface.
litellm.suppress_debug_info = True

# Matches optional leading/trailing whitespace and markdown code fences (```json ... ``` or ``` ... ```).
_MD_FENCE_RE = re.compile(r"^\s*```(?:json)?\s*\n?(.*?)\n?```\s*$", re.DOTALL)


def _loads_or_slice(text: str) -> Any:
    """Parse JSON, tolerating conversational prose around the object (ADR-704 Step A/B).

    Tries ``json.loads`` directly; on failure slices the outermost ``{…}``/``[…]`` span
    (drops leading/trailing prose) and retries. Returns ``None`` when unparseable.
    """
    try:
        return json.loads(text)
    except (json.JSONDecodeError, ValueError):
        pass
    spans: List[tuple[int, int]] = []
    for opener, closer in (("{", "}"), ("[", "]")):
        start = text.find(opener)
        end = text.rfind(closer)
        if start != -1 and end != -1 and end > start:
            spans.append((start, end))
    if not spans:
        return None
    start, end = min(spans, key=lambda se: se[0])  # earliest opener = outermost wrapper
    try:
        return json.loads(text[start:end + 1])
    except (json.JSONDecodeError, ValueError):
        return None


def _find_superset_node(node: Any, required: "set[str]") -> Optional[Dict[str, Any]]:
    """Return the first dict in the tree whose key set is a superset of ``required``.

    Walks dict values and list items recursively (ADR-704 Step C). An empty ``required``
    set matches the first dict encountered (nothing to unwrap).
    """
    if isinstance(node, dict):
        if required.issubset(node.keys()):
            return node
        for value in node.values():
            found = _find_superset_node(value, required)
            if found is not None:
                return found
    elif isinstance(node, list):
        for item in node:
            found = _find_superset_node(item, required)
            if found is not None:
                return found
    return None


# ── response_format graceful degradation ────────────────────────────────────
# Some local backends 400 on the json-mode param. We don't guess by is_local —
# we learn: a model that rejects response_format is memoed here, and subsequent
# calls strip the param pre-emptively so an incompatible backend pays the failed
# round-trip at most once per session. Capable backends never error → never
# memoed → keep native JSON mode. Bounded so a churn of model names can't grow
# the set without limit.
_RESPONSE_FORMAT_UNSUPPORTED: set[str] = set()
_RESPONSE_FORMAT_MEMO_CAP: int = 128


def _is_response_format_error(exc: Exception) -> bool:
    """True when the backend's error text names the json-mode param it rejected."""
    return "response_format" in str(exc).lower()


def _remember_rf_unsupported(model: str) -> None:
    """Memo a model that rejected response_format (bounded; skip add when full)."""
    if model and len(_RESPONSE_FORMAT_UNSUPPORTED) < _RESPONSE_FORMAT_MEMO_CAP:
        _RESPONSE_FORMAT_UNSUPPORTED.add(model)


# ─ OOM Cascade & Inference Resilience ──────────────────────────
# ainvoke() is the single LLM chokepoint. When a local model exhausts its
# context window or VRAM, the OOM-class exception is trapped at the
# litellm.acompletion call site, the local KV cache is purged, the message
# payload is trimmed, and the prompt is re-emitted to a cloud Haiku-class model
# WITHIN THE SAME TURN — see _oom_cascade(). Blueprint §4.
_OOM_CUDA_RE = re.compile(r"cuda|out of memory", re.IGNORECASE)
# Messages retained when trimming the payload for the cloud re-emit. Mirrors the
# StateSummarizer's own failure fallback (brain/summarizer.py KEEP_LAST_N).
_OOM_FALLBACK_KEEP_LAST_N: int = 6

# Generous budget for local models (Ollama on CPU/low-VRAM can be slow for
# structured JSON).  Cloud calls keep the caller-supplied default (60 s).
_LOCAL_LLM_TIMEOUT_S: float = 300.0


def _looks_like_oom(exc: Exception) -> bool:
    """True when an APIConnectionError message reveals a CUDA / VRAM OOM."""
    return bool(_OOM_CUDA_RE.search(str(exc)))


def _trim_for_fallback(messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Deterministically shrink the payload before the cloud re-emit.

    Keeps a leading ``system`` message (if present) plus the last
    ``_OOM_FALLBACK_KEEP_LAST_N`` messages. No LLM call — the LLM-backed
    StateSummarizer routes to the *local* model, the exact tier that just
    OOM'd, so invoking it here would risk a re-OOM recursion.
    """
    if len(messages) <= _OOM_FALLBACK_KEEP_LAST_N:
        return messages
    head: List[Dict[str, Any]] = (
        [messages[0]] if messages and messages[0].get("role") == "system" else []
    )
    return head + messages[-_OOM_FALLBACK_KEEP_LAST_N:]


async def _oom_cascade(
    messages: List[Dict[str, Any]],
    failed_model: str,
    *,
    reason: str,
    kwargs: Dict[str, Any],
    trace_id: str,
    state: Optional[Dict[str, Any]] = None,
) -> ModelResponse:
    """OOM rescue: purge VRAM → mark state → trim context → re-emit to cloud.

    Single-turn recovery. Sequential, NOT recursive: a second
    OOM on the cloud model propagates out of ainvoke() naturally — the
    double-fault → DLQ path.
    """
    logger.warning(
        "OOM cascade engaged [trace=%s] failed_model=%s reason=%s",
        trace_id, failed_model, reason,
    )

    # 1. Purge the local KV cache / VRAM (argless signature).
    from core.lifecycle_manager import lifecycle_manager
    await lifecycle_manager.release_vram_on_mode_switch()

    # 2. Mark graph state — best-effort; ainvoke() is often called without state.
    if state is not None:
        state["oom_fallback_active"] = True
        state.setdefault("security_flags", []).append(
            f"OOM_FALLBACK_ENGAGED:{reason}"
        )

    # 3. Trim the payload to cut token density before the re-emit.
    trimmed = _trim_for_fallback(messages)

    # 4. Re-emit to the cloud fallback model (env-configurable).
    fallback_model = os.getenv(
        "AILIENANT_OOM_CLOUD_FALLBACK_MODEL", "claude-haiku-4-5-20251001"
    )
    logger.warning(
        "OOM cascade re-emitting to cloud fallback [trace=%s] model=%s",
        trace_id, fallback_model,
    )

    # Surface the swap to the IDE. Best-effort: the rescue must never fail on a
    # transport hiccup, and `state` is often absent. Broadcasts are keyed by
    # task_id, like the rest of the brain layer. (`except Exception` lets a
    # CancelledError propagate, since it derives from BaseException.)
    if state is not None:
        task_id = str(state.get("task_id", "") or "")
        if task_id:
            try:
                from api.websocket_manager import vfs_manager  # deferred — avoids circular import
                await vfs_manager.broadcast_oom_engaged(
                    task_id, failed_model=failed_model, fallback_model=fallback_model,
                )
            except Exception as exc:  # noqa: BLE001 — UI surfacing is non-fatal
                logger.debug("OOM engaged broadcast failed (non-fatal): %s", exc)

    _t0 = time.perf_counter()
    response: ModelResponse = cast(ModelResponse, await litellm.acompletion(
        **{**kwargs, "model": fallback_model, "messages": trimmed}
    ))
    swap_latency_ms = (time.perf_counter() - _t0) * 1000.0

    # 5. Ledger — the rescue is a cloud call.
    try:
        from core.token_ledger import token_ledger
        usage = getattr(response, "usage", None)
        if usage is not None:
            token_ledger.record_cloud(
                int(getattr(usage, "prompt_tokens", 0) or 0),
                int(getattr(usage, "completion_tokens", 0) or 0),
            )
    except Exception as exc:  # noqa: BLE001 — token accounting is non-fatal
        logger.debug("OOM cascade token accounting failed (non-fatal): %s", exc)

    # 6. Telemetry — record the rescue swap (Phase 6.8, formalises 6.3).
    try:
        from core import telemetry
        tokens_at_failure = litellm.token_counter(
            model=failed_model, messages=messages
        )
        await telemetry.log_oom_event(
            reason=reason, original_model=failed_model,
            fallback_model=fallback_model, tokens_at_failure=tokens_at_failure,
            swap_latency_ms=swap_latency_ms, state=state,
        )
    except Exception as exc:  # noqa: BLE001 — telemetry is non-fatal
        logger.debug("OOM telemetry write failed (non-fatal): %s", exc)

    return response


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

# protocol-friendly alias for the routing tiers.
Tier = TaskPriority


def _classify_model_as_tier(model_name: str) -> TaskPriority:
    """Best-effort: classify a raw model name into LOCAL vs CLOUD for the ledger."""
    if model_name == MODEL_BIG:
        return TaskPriority.CLOUD
    return TaskPriority.LOCAL


# Native Thinking capability gate.
# Substrings (lower-cased) that identify a model exposing native reasoning
# tokens. Anthropic Extended Thinking surfaces them via LiteLLM's normalized
# ``delta.reasoning_content``; open reasoning models (DeepSeek-R1, QwQ) do the
# same. Kept as a substring allowlist (not a Literal) so new reasoning models
# can be added without a schema bump; anything not matched falls back to flat
# text streaming with zero regression.
_NATIVE_THINKING_MODEL_HINTS: tuple[str, ...] = (
    "claude-3-7",
    "claude-sonnet-4",
    "claude-opus-4",
    "deepseek-r1",
    "deepseek-reasoner",
    "qwq",
    "o1",
    "o3",
)


def _supports_native_thinking(model_name: str) -> bool:
    """True when the model id looks like it emits native reasoning tokens.

    Best-effort, substring-based (see ``_NATIVE_THINKING_MODEL_HINTS``). A false
    negative is harmless — the stream simply never emits thinking deltas and the
    frontend shows no Thought Box. A false positive is also harmless: providers
    that reject the ``thinking`` param raise, and the orchestration layer falls
    back to the flat-text path.
    """
    lowered = (model_name or "").lower()
    return any(hint in lowered for hint in _NATIVE_THINKING_MODEL_HINTS)


# Streaming structured-output capability gate.
# Providers that honour ``response_format`` *while streaming* (OpenAI-style JSON
# mode on a streamed completion). Default-deny: anything not listed keeps the
# prompt-enforced + sanitizer path, so a provider that would reject the param on
# a stream (Anthropic has no response_format; some local/reasoner builds 400 on
# it) is never sent it. This is the single tuning point — add a provider only
# once its streaming JSON mode is verified.
_STREAMING_STRUCTURED_PROVIDERS: frozenset[str] = frozenset({"openai"})


def _supports_streaming_structured_output(target: Any) -> bool:
    """True when the resolved target's provider streams ``response_format``.

    Conservative by construction (see ``_STREAMING_STRUCTURED_PROVIDERS``): a
    false negative simply keeps the existing sanitizer fallback (zero
    regression); we never gamble a streamed structured call on an unverified
    provider.
    """
    provider = getattr(target, "provider", "") or ""
    return provider.lower() in _STREAMING_STRUCTURED_PROVIDERS

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

# -------------------------------------------------------------------------
# Outbound-concurrency gate: bound how many gateway calls are in-flight to the
# proxy at once so a fan-out is admission-controlled here, not discovered as a
# provider-side rate-limit rejection. The gateway is a static namespace with no
# instance to hold state, so the semaphore lives at module scope. An
# asyncio.Semaphore binds to the loop that first uses it, and the test suite
# spins many independent event loops (one per asyncio.run), so the gate is keyed
# per running loop; a WeakKeyDictionary drops each entry when its loop is
# garbage-collected, so no manual reset is ever needed.
# -------------------------------------------------------------------------
_llm_semaphores: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Semaphore]" = (
    weakref.WeakKeyDictionary()
)


def _llm_semaphore() -> asyncio.Semaphore:
    """Return the outbound-concurrency gate bound to the running event loop.

    Lazily created per loop with the configured ceiling. Creation is race-free:
    get_running_loop() -> lookup -> insert runs with no ``await`` in between, so
    concurrent coroutines on the same loop cannot interleave mid-function and
    every one observes the single shared semaphore.
    """
    loop = asyncio.get_running_loop()
    sem = _llm_semaphores.get(loop)
    if sem is None:
        sem = asyncio.Semaphore(LLM_MAX_CONCURRENCY)
        _llm_semaphores[loop] = sem
    return sem


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
    def _extract_nested_schema_target(
        raw_str: str, schema_class: Type[BaseModel]
    ) -> Dict[str, Any]:
        """ AST-aware recursive envelope unwrapper.

        Local/BYOM models routinely wrap structured output: a markdown fence,
        conversational prose around the object, or a top-level envelope key such as
        ``{"MissionSpecification": {…}}`` or ``{"json": {"result": {…}}}``. A flat
        single-key lookup fails on all of these. This finds the *real* schema object by
        recursively walking the parsed tree and returning the first dict whose key set is
        a superset of ``schema_class``'s REQUIRED fields.

        Centralized beside ``_sanitize_json_response`` so every structured agent call can
        reuse it (planner, Mini-Judge, …). Never raises — the caller feeds the returned
        dict to ``model_validate`` and lets Pydantic surface a native ``ValidationError``.

        Returns:
            The unwrapped object dict; the base parsed dict when no node matches (so the
            caller's validation still fails loudly); or ``{}`` when the text is unparseable.
        """
        cleaned = LLMGateway._sanitize_json_response(raw_str)

        # Step B — parse; on failure, slice the outermost JSON span to drop prose, retry.
        parsed: Any = _loads_or_slice(cleaned)
        if parsed is None:
            return {}

        # Step C — find the node whose keys ⊇ the schema's required fields.
        required: set[str] = {
            name for name, field in schema_class.model_fields.items() if field.is_required()
        }
        target = _find_superset_node(parsed, required)
        if target is not None:
            return target

        # Step D — no match: return the base dict so Pydantic raises natively; else {}.
        return parsed if isinstance(parsed, dict) else {}

    @staticmethod
    def invoke(
        messages: list[dict[str, Any]],
        model: str = MODEL_MEDIUM,
        temperature: float = 0.0,
        response_format: Optional[dict[str, Any]] = None,
        max_tokens: int = 4096,
        timeout: float = 60.0,
        session_id: Optional[str] = None,
    ) -> ModelResponse:
        """Synchronous LLM call. Prefer ainvoke() inside async contexts.

        DANGER: Bypass concurrency throttle — this synchronous path is NOT gated
        by the outbound-concurrency semaphore (an asyncio primitive cannot guard
        blocking code). Do NOT wrap it in ``asyncio.to_thread(LLMGateway.invoke,
        ...)`` for fan-out: that smuggles it back onto the event loop while
        bypassing the gate, so real concurrency to the provider silently exceeds
        the ceiling. Use the async entry points (ainvoke / astream*) for any
        concurrent work.
        """
        trace_id = session_id or str(uuid.uuid4())
        cfg = get_litellm_config()
        logger.debug(
            "LLM invoke — model=%s base_url=%s trace=%s", model, cfg["base_url"], trace_id
        )
        kwargs: dict[str, Any] = dict(
            model=model,
            messages=messages,
            temperature=temperature,
            max_tokens=max_tokens,
            timeout=timeout,
            max_retries=LLM_MAX_TRANSPORT_RETRIES,
            metadata={"session_id": trace_id},
            extra_headers={"X-Ailienant-Trace-ID": trace_id},
            **cfg,
        )
        if response_format and kwargs["model"] not in _RESPONSE_FORMAT_UNSUPPORTED:
            kwargs["response_format"] = response_format
        try:
            return cast(ModelResponse, litellm.completion(**kwargs))
        except Exception as e:
            if "response_format" in kwargs and _is_response_format_error(e):
                logger.warning(
                    "Backend rejected response_format; stripping + retrying once [trace=%s]",
                    trace_id,
                )
                _remember_rf_unsupported(kwargs["model"])
                kwargs.pop("response_format", None)
                return cast(ModelResponse, litellm.completion(**kwargs))
            logger.error("LLM invoke failed [trace=%s]: %s", trace_id, e)
            raise

    @staticmethod
    async def ainvoke(
        messages: list[dict[str, Any]],
        model: str = MODEL_MEDIUM,
        tier: Optional[TaskPriority] = None,
        temperature: float = 0.0,
        response_format: Optional[dict[str, Any]] = None,
        max_tokens: int = 4096,
        timeout: float = 60.0,
        session_id: Optional[str] = None,
        state: Optional[Dict[str, Any]] = None,
    ) -> ModelResponse:
        """Async LLM call — non-blocking on the FastAPI event loop.

        — pass `tier=Tier.LOCAL` or `tier=Tier.CLOUD` to route via
        the priority map (overrides `model`). If `tier` is None, `model` is used
        directly and the tier is inferred from the model name for accounting.

        — `state` is the optional LangGraph state dict. When supplied,
        an OOM-class failure mutates it (`oom_fallback_active`, `security_flags`)
        before re-emitting to the cloud fallback model; see `_oom_cascade`.
        """
        trace_id = session_id or str(uuid.uuid4())
        effective_model: str = (
            _PRIORITY_MODEL_MAP[tier] if tier is not None else model
        )

        # — BYOM-aware routing. Resolve `ailienant/*` tier aliases to
        # the active preset's concrete model and call it directly (no proxy). Falls
        # back to the LiteLLM proxy when no preset is active (back-compat). This is
        # the single chokepoint that un-stubs the planner + mini-judge + coder.
        byom_kwargs: Optional[dict[str, Any]] = None
        _effective_timeout = timeout  # default; overridden below for a resolved local target
        if effective_model.startswith("ailienant/"):
            from core.config.model_resolver import get_chat_target
            _alias_tier = effective_model.split("/", 1)[1]
            _target = get_chat_target(
                _alias_tier if _alias_tier in ("small", "medium", "big") else "medium"
            )
            if _target is not None:
                _effective_timeout = _LOCAL_LLM_TIMEOUT_S if _target.is_local else timeout
                byom_kwargs = {"model": _target.model}
                if _target.api_base:
                    byom_kwargs["api_base"] = _target.api_base
                if _target.api_key:
                    byom_kwargs["api_key"] = _target.api_key

        if byom_kwargs is not None:
            logger.debug(
                "LLM ainvoke (BYOM) — alias=%s model=%s trace=%s",
                effective_model, byom_kwargs["model"], trace_id,
            )
            kwargs: dict[str, Any] = dict(
                messages=messages,
                temperature=temperature,
                max_tokens=max_tokens,
                timeout=_effective_timeout,
                max_retries=LLM_MAX_TRANSPORT_RETRIES,
                metadata={"session_id": trace_id},
                extra_headers={"X-Ailienant-Trace-ID": trace_id},
                **byom_kwargs,
            )
        else:
            cfg = get_litellm_config()
            logger.debug(
                "LLM ainvoke — model=%s tier=%s base_url=%s trace=%s",
                effective_model, tier, cfg["base_url"], trace_id,
            )
            kwargs = dict(
                model=effective_model,
                messages=messages,
                temperature=temperature,
                max_tokens=max_tokens,
                timeout=timeout,
                max_retries=LLM_MAX_TRANSPORT_RETRIES,
                metadata={"session_id": trace_id},
                extra_headers={"X-Ailienant-Trace-ID": trace_id},
                **cfg,
            )
        if response_format and kwargs["model"] not in _RESPONSE_FORMAT_UNSUPPORTED:
            kwargs["response_format"] = response_format
        # Admission control: hold one concurrency slot for the whole network op
        # (including the inline response_format retry and any OOM cascade), then
        # release it before the post-hoc token accounting below.
        sem = _llm_semaphore()
        if sem.locked():
            logger.debug("LLM gateway at concurrency ceiling; ainvoke queued [trace=%s]", trace_id)
        async with sem:
            try:
                response: ModelResponse = cast(ModelResponse, await litellm.acompletion(**kwargs))
            except ContextWindowExceededError:
                # Context window exhausted → OOM cascade to a cloud fallback model.
                return await _oom_cascade(
                    messages, effective_model, reason="context_overflow",
                    kwargs=kwargs, trace_id=trace_id, state=state,
                )
            except APIConnectionError as exc:
                # A CUDA / VRAM OOM surfaces as a connection error → cascade.
                if _looks_like_oom(exc):
                    return await _oom_cascade(
                        messages, effective_model, reason="cuda_oom",
                        kwargs=kwargs, trace_id=trace_id, state=state,
                    )
                logger.error("LLM ainvoke failed [trace=%s]: %s", trace_id, exc)
                raise
            except Exception as e:
                if "response_format" in kwargs and _is_response_format_error(e):
                    logger.warning(
                        "Backend rejected response_format; stripping + retrying once [trace=%s]",
                        trace_id,
                    )
                    _remember_rf_unsupported(kwargs["model"])
                    kwargs.pop("response_format", None)
                    response = cast(ModelResponse, await litellm.acompletion(**kwargs))
                else:
                    logger.error("LLM ainvoke failed [trace=%s]: %s", trace_id, e)
                    raise

        # — record token usage to the global ledger by tier.
        try:
            from core.token_ledger import token_ledger
            usage = getattr(response, "usage", None)
            if usage is not None:
                prompt_tokens: int = int(getattr(usage, "prompt_tokens", 0) or 0)
                completion_tokens: int = int(getattr(usage, "completion_tokens", 0) or 0)
                resolved_tier: TaskPriority = (
                    tier if tier is not None else _classify_model_as_tier(effective_model)
                )
                if resolved_tier == TaskPriority.CLOUD:
                    token_ledger.record_cloud(prompt_tokens, completion_tokens)
                else:
                    token_ledger.record_local(prompt_tokens, completion_tokens)
        except Exception as exc:
            logger.debug("Token accounting failed (non-fatal): %s", exc)

        return response

    @staticmethod
    async def acomplete_with_thinking(
        messages: list[dict[str, Any]],
        model: str = MODEL_MEDIUM,
        temperature: float = 0.0,
        response_format: Optional[dict[str, Any]] = None,
        max_tokens: int = 4096,
        timeout: float = 60.0,
        session_id: Optional[str] = None,
        state: Optional[Dict[str, Any]] = None,
        *,
        on_thinking: Optional[Callable[[str], Awaitable[None]]] = None,
        enable_thinking: bool = False,
        thinking_budget_tokens: int = 4096,
    ) -> str:
        """Structured completion that streams native reasoning while it works.

        A single entry point with two branches so the caller's code is identical
        regardless of model:

        - **Streaming branch** (taken only when a reasoning sink is wired AND the
          active model emits native reasoning tokens): consume the thinking-aware
          stream, push each reasoning delta to ``on_thinking`` (best-effort), and
          accumulate the answer tokens into an in-memory buffer that is returned.
        - **Fallback branch** (every other case — no sink, thinking disabled, or a
          non-reasoning model): delegate to :meth:`ainvoke`, preserving
          ``response_format``, the OOM cascade, and response-cache compatibility.
          Behaviour here is byte-identical to a direct ``ainvoke`` call.

        Streaming is *best-effort*; generation is *mission-critical*. A failure in
        the reasoning sink (e.g. a closed WebSocket) is swallowed and the sink is
        latched off for the rest of the call — the answer buffer keeps filling, so
        the structured result is always returned intact. ``CancelledError`` (a real
        abort) is never swallowed.

        Because the streaming branch cannot pass ``response_format`` (the streaming
        APIs don't support it), a reasoning model may wrap its JSON in markdown
        fences; when the caller asked for JSON the buffered answer is run through
        :meth:`_sanitize_json_response` before returning so the downstream parser
        never trips on a fence.
        """
        # Derive the BYOM tier from the alias, mirroring ainvoke's resolution.
        _alias_tier = model.split("/", 1)[1] if model.startswith("ailienant/") else "medium"
        tier = _alias_tier if _alias_tier in ("small", "medium", "big") else "medium"

        target: Any = None
        want_stream = on_thinking is not None and enable_thinking
        if want_stream:
            from core.config.model_resolver import get_chat_target
            target = get_chat_target(tier)
            want_stream = target is not None and _supports_native_thinking(target.model)

        if not want_stream:
            resp = await LLMGateway.ainvoke(
                messages=messages,
                model=model,
                temperature=temperature,
                response_format=response_format,
                max_tokens=max_tokens,
                timeout=timeout,
                session_id=session_id,
                state=state,
            )
            return resp.choices[0].message.content or ""

        # Streaming branch. on_thinking is guaranteed non-None by want_stream.
        if on_thinking is None:
            return ""  # unreachable by construction
        assert target is not None  # narrowed: want_stream required a resolved target
        sink: Callable[[str], Awaitable[None]] = on_thinking
        buffer: List[str] = []
        sink_live = True
        # Preserve provider-enforced JSON mode on the stream where the provider
        # supports it; elsewhere it stays None and the sanitizer below recovers
        # the JSON (DEBT-013).
        stream_response_format = (
            response_format
            if response_format and _supports_streaming_structured_output(target)
            else None
        )
        async for delta in LLMGateway.astream_byom_thinking(
            messages,
            tier=tier,
            temperature=temperature,
            max_tokens=max_tokens,
            timeout=timeout,
            session_id=session_id,
            enable_thinking=True,
            thinking_budget_tokens=thinking_budget_tokens,
            response_format=stream_response_format,
        ):
            if delta.kind == "thinking":
                # Best-effort: a dead sink must never abort generation.
                if sink_live:
                    try:
                        await sink(delta.text)
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:  # noqa: BLE001 — streaming is best-effort
                        logger.debug("thinking sink failed; latching off: %s", exc)
                        sink_live = False
            else:  # "text" — the answer channel (mission-critical)
                buffer.append(delta.text)

        answer = "".join(buffer)
        # Dropping response_format reintroduces ```json fences on reasoning models;
        # strip them so the downstream parser sees clean JSON.
        if response_format:
            answer = LLMGateway._sanitize_json_response(answer)
        return answer

    @staticmethod
    async def astream(
        messages: list[dict[str, Any]],
        model: str = MODEL_MEDIUM,
        temperature: float = 0.0,
        max_tokens: int = 4096,
        timeout: float = 60.0,
        session_id: Optional[str] = None,
    ) -> AsyncIterator[str]:
        """Async streaming LLM call — yields token delta strings for WebSocket broadcast.

        Wired to transport/throttler.py via throttled_stream() in Phase 4's
        WebSocket token handler. Each yielded string is a non-empty token delta
        suitable for direct broadcast via vfs_manager.broadcast_token().
        """
        trace_id = session_id or str(uuid.uuid4())
        cfg = get_litellm_config()
        logger.debug(
            "LLM astream — model=%s base_url=%s trace=%s", model, cfg["base_url"], trace_id
        )
        kwargs: dict[str, Any] = dict(
            model=model,
            messages=messages,
            temperature=temperature,
            max_tokens=max_tokens,
            timeout=timeout,
            stream=True,
            max_retries=LLM_MAX_TRANSPORT_RETRIES,
            metadata={"session_id": trace_id},
            extra_headers={"X-Ailienant-Trace-ID": trace_id},
            **cfg,
        )
        # Hold one concurrency slot for the full stream lifetime — an open stream
        # keeps a live provider connection, so it is genuinely in-flight until the
        # last chunk (see the module gate note on true in-flight accounting).
        sem = _llm_semaphore()
        if sem.locked():
            logger.debug("LLM gateway at concurrency ceiling; astream queued [trace=%s]", trace_id)
        async with sem:
            try:
                response = cast(CustomStreamWrapper, await litellm.acompletion(**kwargs))
                async for chunk in response:
                    delta = chunk.choices[0].delta.content or ""
                    if delta:
                        yield delta
            except Exception as e:
                logger.error("LLM astream failed [trace=%s]: %s", trace_id, e)
                raise

    # -------------------------------------------------------------------------
    # Direct BYOM calls (proxy-free)
    # -------------------------------------------------------------------------
    # These bypass get_litellm_config() / the LiteLLM proxy and call the active
    # BYOM preset's model directly via its resolved api_base/api_key. Used by the
    # live main chat (astream_byom) and the Natt analyst (acomplete_byom).

    @staticmethod
    def _byom_kwargs(target: Any, messages: list[dict[str, Any]], **opts: Any) -> dict[str, Any]:
        kwargs: dict[str, Any] = {"model": target.model, "messages": messages, **opts}
        if target.api_base:
            kwargs["api_base"] = target.api_base
        if target.api_key:
            kwargs["api_key"] = target.api_key
        return kwargs

    @staticmethod
    async def acomplete_byom(
        messages: list[dict[str, Any]],
        tier: str = "medium",
        temperature: float = 0.4,
        max_tokens: int = 1024,
        timeout: float = 60.0,
        session_id: Optional[str] = None,
    ) -> str:
        """Non-streaming completion against the active BYOM chat model (direct).

        On a non-OOM transport drop of a *local* endpoint, fails over once to the
        next callable target on the capability ladder (see ``get_failover_target``).
        The retry is bounded to a single attempt: a second failure re-raises.
        """
        from core.config.model_resolver import get_chat_target, get_failover_target  # deferred — load order
        target = get_chat_target(tier)
        if target is None:
            raise NoAvailableProviderError("No active BYOM chat model — activate a preset.")
        trace_id = session_id or str(uuid.uuid4())
        _effective_timeout = _LOCAL_LLM_TIMEOUT_S if target.is_local else timeout
        attempted_failover = False
        # One concurrency slot spans the whole call, including the bounded local
        # failover retry (a single logical op holds a single slot).
        sem = _llm_semaphore()
        if sem.locked():
            logger.debug("LLM gateway at concurrency ceiling; acomplete_byom queued [trace=%s]", trace_id)
        async with sem:
            while True:
                kwargs = LLMGateway._byom_kwargs(
                    target, messages, temperature=temperature, max_tokens=max_tokens,
                    timeout=_effective_timeout, max_retries=LLM_MAX_TRANSPORT_RETRIES,
                )
                logger.debug("BYOM acomplete — model=%s base=%s trace=%s", target.model, target.api_base, trace_id)
                try:
                    resp: ModelResponse = cast(ModelResponse, await litellm.acompletion(**kwargs))
                    return resp.choices[0].message.content or ""
                except APIConnectionError as exc:
                    if attempted_failover or not target.is_local or _looks_like_oom(exc):
                        raise  # OOM, cloud drop, or already retried — surface it
                    nxt = get_failover_target(tier, exclude_model=target.model)
                    if nxt is None:
                        raise  # nothing to fall back to — original drop surfaces
                    logger.warning(
                        "BYOM local endpoint dropped [model=%s trace=%s]; failing over to %s",
                        target.model, trace_id, nxt.model,
                    )
                    target = nxt
                    _effective_timeout = _LOCAL_LLM_TIMEOUT_S if target.is_local else timeout
                    attempted_failover = True

    @staticmethod
    async def astream_byom(
        messages: list[dict[str, Any]],
        tier: str = "medium",
        temperature: float = 0.4,
        max_tokens: int = 1024,
        timeout: float = 60.0,
        session_id: Optional[str] = None,
    ) -> AsyncIterator[str]:
        """Streaming completion against the active BYOM chat model (direct).

        Yields non-empty token-delta strings for WebSocket broadcast. Raises
        NoAvailableProviderError when no BYOM preset is active.

        (FinOps integrity) — opt into LiteLLM's
        ``stream_options={"include_usage": True}`` so the final chunk carries
        a ``usage`` object with prompt/completion token counts. The accounting
        block lives in a ``finally`` so it ALWAYS runs — completion path AND
        abort path (CancelledError propagates through the ``async for``, but
        the finally still flushes whatever tokens were observed). Providers
        that don't emit usage (some local model builds) record zeros, which is
        a no-op by the ledger contract; the abort path is unaffected either
        way (token accounting NEVER blocks cancel propagation).
        """
        from core.config.model_resolver import get_chat_target, get_failover_target  # deferred — load order
        target = get_chat_target(tier)
        if target is None:
            raise NoAvailableProviderError("No active BYOM chat model — activate a preset.")
        trace_id = session_id or str(uuid.uuid4())
        _effective_timeout = _LOCAL_LLM_TIMEOUT_S if target.is_local else timeout
        prompt_tokens: int = 0
        completion_tokens: int = 0
        # Hold one concurrency slot for the full stream lifetime; the token
        # accounting finally runs inside the gate so an abort flushes tokens and
        # then frees the slot in that order.
        sem = _llm_semaphore()
        if sem.locked():
            logger.debug("LLM gateway at concurrency ceiling; astream_byom queued [trace=%s]", trace_id)
        async with sem:
            try:
                # Bounded single failover on the INITIAL connect only — a partially
                # streamed answer cannot be re-rolled, so the retry must land before the
                # first yield. A non-OOM transport drop of a local endpoint falls over
                # once to the next callable ladder target; a second failure re-raises.
                attempted_failover = False
                while True:
                    kwargs = LLMGateway._byom_kwargs(
                        target, messages, temperature=temperature, max_tokens=max_tokens,
                        timeout=_effective_timeout, stream=True, max_retries=LLM_MAX_TRANSPORT_RETRIES,
                    )
                    kwargs.setdefault("stream_options", {"include_usage": True})
                    logger.debug("BYOM astream — model=%s base=%s trace=%s", target.model, target.api_base, trace_id)
                    try:
                        response = cast(CustomStreamWrapper, await litellm.acompletion(**kwargs))
                        break
                    except APIConnectionError as exc:
                        if attempted_failover or not target.is_local or _looks_like_oom(exc):
                            raise  # OOM, cloud drop, or already retried — surface it
                        nxt = get_failover_target(tier, exclude_model=target.model)
                        if nxt is None:
                            raise  # nothing to fall back to — original drop surfaces
                        logger.warning(
                            "BYOM local endpoint dropped [model=%s trace=%s]; failing over to %s",
                            target.model, trace_id, nxt.model,
                        )
                        target = nxt
                        _effective_timeout = _LOCAL_LLM_TIMEOUT_S if target.is_local else timeout
                        attempted_failover = True
                async for chunk in response:
                    # Final-chunk shape (include_usage): `usage` populated, `choices`
                    # may be empty. Pre-final chunks: `usage=None`, content in choices[0].delta.
                    usage = getattr(chunk, "usage", None)
                    if usage is not None:
                        prompt_tokens = int(
                            getattr(usage, "prompt_tokens", prompt_tokens) or prompt_tokens
                        )
                        completion_tokens = int(
                            getattr(usage, "completion_tokens", completion_tokens) or completion_tokens
                        )
                    choices = getattr(chunk, "choices", None) or []
                    if choices:
                        delta = (getattr(choices[0], "delta", None) and choices[0].delta.content) or ""
                        if delta:
                            yield delta
            finally:
                # ALWAYS record — completion OR abort path. Zero-token cases are
                # no-ops in the ledger contract (verified in core/token_ledger.py).
                try:
                    from core.token_ledger import token_ledger
                    resolved_tier = _classify_model_as_tier(target.model)
                    if resolved_tier == TaskPriority.CLOUD:
                        token_ledger.record_cloud(prompt_tokens, completion_tokens)
                    else:
                        token_ledger.record_local(prompt_tokens, completion_tokens)
                except Exception as exc:  # noqa: BLE001 — never block stream-end on accounting
                    logger.debug("Stream token accounting failed (non-fatal): %s", exc)

    # -------------------------------------------------------------------------
    # — Native Thinking streaming (proxy-free BYOM)
    # -------------------------------------------------------------------------

    @staticmethod
    async def astream_byom_thinking(
        messages: list[dict[str, Any]],
        tier: str = "medium",
        temperature: float = 0.4,
        max_tokens: int = 1024,
        timeout: float = 60.0,
        session_id: Optional[str] = None,
        *,
        enable_thinking: bool = True,
        thinking_budget_tokens: int = 4096,
        response_format: Optional[dict[str, Any]] = None,
    ) -> AsyncIterator["StreamDelta"]:
        """Thinking-aware streaming completion against the active BYOM chat model.

        Bifurcates each upstream chunk into ``StreamDelta`` values tagged
        ``"thinking"`` (native reasoning tokens) or ``"text"`` (answer tokens).
        The legacy ``astream_byom`` is deliberately left untouched and remains
        the flat-text fallback path; callers select this method only when the
        user has Native Thinking enabled (Phase 9 plan §1C).

        ``thinking`` config is appended ONLY when ``enable_thinking`` is true AND
        ``_supports_native_thinking`` recognises the active model. Otherwise the
        param is omitted entirely → the provider streams plain text and this
        generator simply never yields a ``"thinking"`` delta (zero regression).

        The ``finally`` token-accounting block mirrors ``astream_byom`` verbatim:
        thinking tokens are billed inside ``usage.completion_tokens`` on the
        final chunk, so the ledger stays correct with no special handling, and
        accounting still flushes on the abort (CancelledError) path.
        """
        from tools.stream_delta import StreamDelta  # local — keep module a leaf
        from core.config.model_resolver import get_chat_target  # deferred — load order

        target = get_chat_target(tier)
        if target is None:
            raise NoAvailableProviderError("No active BYOM chat model — activate a preset.")
        trace_id = session_id or str(uuid.uuid4())
        _effective_timeout = _LOCAL_LLM_TIMEOUT_S if target.is_local else timeout
        kwargs = LLMGateway._byom_kwargs(
            target, messages, temperature=temperature, max_tokens=max_tokens,
            timeout=_effective_timeout, stream=True, max_retries=LLM_MAX_TRANSPORT_RETRIES,
        )
        kwargs.setdefault("stream_options", {"include_usage": True})
        thinking_on = bool(enable_thinking) and _supports_native_thinking(target.model)
        if thinking_on:
            # LiteLLM normalises Anthropic's ``thinking`` blocks (and open
            # reasoning models' equivalents) into ``delta.reasoning_content``.
            kwargs["thinking"] = {"type": "enabled", "budget_tokens": thinking_budget_tokens}
        # Provider-enforced JSON mode on the stream, only when the caller asked
        # for it and the model has not already proven it rejects the param.
        if response_format and target.model not in _RESPONSE_FORMAT_UNSUPPORTED:
            kwargs["response_format"] = response_format
        logger.debug(
            "BYOM astream(thinking=%s) — model=%s base=%s trace=%s",
            thinking_on, target.model, target.api_base, trace_id,
        )
        prompt_tokens: int = 0
        completion_tokens: int = 0
        # Hold one concurrency slot for the full stream lifetime; the token
        # accounting finally runs inside the gate so an abort flushes tokens and
        # then frees the slot in that order.
        sem = _llm_semaphore()
        if sem.locked():
            logger.debug("LLM gateway at concurrency ceiling; astream_byom_thinking queued [trace=%s]", trace_id)
        async with sem:
            try:
                try:
                    response = cast(CustomStreamWrapper, await litellm.acompletion(**kwargs))
                except Exception as exc:
                    # Mirror ainvoke's self-healing: a backend that rejects
                    # response_format is memoed and retried once without it, before
                    # any chunk is consumed (so a stream is never restarted mid-flight).
                    if "response_format" in kwargs and _is_response_format_error(exc):
                        logger.warning(
                            "Backend rejected streamed response_format; stripping + retrying once [trace=%s]",
                            trace_id,
                        )
                        _remember_rf_unsupported(kwargs["model"])
                        kwargs.pop("response_format", None)
                        response = cast(CustomStreamWrapper, await litellm.acompletion(**kwargs))
                    else:
                        raise
                async for chunk in response:
                    usage = getattr(chunk, "usage", None)
                    if usage is not None:
                        prompt_tokens = int(
                            getattr(usage, "prompt_tokens", prompt_tokens) or prompt_tokens
                        )
                        completion_tokens = int(
                            getattr(usage, "completion_tokens", completion_tokens) or completion_tokens
                        )
                    choices = getattr(chunk, "choices", None) or []
                    if not choices:
                        continue
                    delta_obj = getattr(choices[0], "delta", None)
                    if delta_obj is None:
                        continue
                    # Reasoning channel first (it precedes the answer in practice).
                    reasoning = getattr(delta_obj, "reasoning_content", None) or ""
                    if reasoning:
                        yield StreamDelta("thinking", reasoning)
                    content = getattr(delta_obj, "content", None) or ""
                    if content:
                        yield StreamDelta("text", content)
            finally:
                # ALWAYS record — completion OR abort path. Identical contract to
                # astream_byom; thinking tokens are inside completion_tokens.
                try:
                    from core.token_ledger import token_ledger
                    resolved_tier = _classify_model_as_tier(target.model)
                    if resolved_tier == TaskPriority.CLOUD:
                        token_ledger.record_cloud(prompt_tokens, completion_tokens)
                    else:
                        token_ledger.record_local(prompt_tokens, completion_tokens)
                except Exception as exc:  # noqa: BLE001 — never block stream-end on accounting
                    logger.debug("Stream token accounting failed (non-fatal): %s", exc)

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
        messages: list[dict[str, Any]],
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
