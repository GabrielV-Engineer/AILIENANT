# ailienant-core/tools/llm_gateway.py

import asyncio
import json
import logging
import os
import re
import time
import uuid
import weakref
from collections import deque
from enum import Enum
from typing import (
    TYPE_CHECKING, Any, AsyncIterator, Awaitable, Callable, Dict, List, Optional, Type, TypedDict, cast,
)

if TYPE_CHECKING:
    from tools.stream_delta import StreamDelta

import httpx
import litellm
from litellm import CustomStreamWrapper, ModelResponse
from litellm.exceptions import APIConnectionError, ContextWindowExceededError, UnsupportedParamsError
from pydantic import BaseModel

from brain.retry_policy import LLM_MAX_TRANSPORT_RETRIES
from shared.config import (
    MODEL_SMALL,
    MODEL_MEDIUM,
    MODEL_BIG,
    LITELLM_PROXY_BASE_URL,
    LLM_MAX_CONCURRENCY,
    VISION_MAX_IMAGES_PER_CALL,
    VISION_MAX_TOTAL_BASE64_CHARS,
    get_litellm_config,
    check_cloud_availability,
)


class _ActionKwarg(TypedDict, total=False):
    """Precisely-typed optional-kwarg carrier for the `action` tag.

    A plain ``Dict[str, str]`` unpacked via ``**`` is opaque to mypy — it cannot
    verify which keyword slot it lands in, so it conservatively rejects the
    splat against every other keyword parameter in the call. A ``total=False``
    TypedDict is a documented mypy-understood exception: ``**extra`` unpacks
    precisely onto the single named ``action`` parameter it declares, exactly
    like passing the keyword directly, while `{}` (the untagged case) supplies
    none of it — see the call sites in ``ainvoke``/``astream_byom``/
    ``astream_byom_thinking``/``astream_reasoning``/``acomplete_with_thinking``.
    """
    action: str


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


def _find_node_with_any_key(node: Any, keys: "set[str]") -> Optional[Dict[str, Any]]:
    """Return the first dict in the tree sharing at least one key with ``keys``.

    The all-optional counterpart to :func:`_find_superset_node`: with no required
    fields to anchor on, "carries a field this schema declares" is the weakest
    evidence that a node is the intended object rather than its envelope.
    """
    if isinstance(node, dict):
        if keys & node.keys():
            return node
        for value in node.values():
            found = _find_node_with_any_key(value, keys)
            if found is not None:
                return found
    elif isinstance(node, list):
        for item in node:
            found = _find_node_with_any_key(item, keys)
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


# ── native-thinking param graceful degradation ──────────────────────────────
# A runtime capability probe (core.config.model_resolver) can correctly report that
# a MODEL supports reasoning while litellm's transport for that provider still
# rejects the `thinking` kwarg outright — observed with Ollama's own `/api/show`
# listing `thinking` in gemma4's capabilities while litellm's `ollama_chat` custom
# provider does not forward an OpenAI/Anthropic-shaped `thinking` param at all.
# Same self-heal contract as _RESPONSE_FORMAT_UNSUPPORTED: memoed per model, the
# failed round-trip is paid at most once per session.
_THINKING_PARAM_UNSUPPORTED: set[str] = set()
_THINKING_PARAM_MEMO_CAP: int = 128


def _remember_thinking_unsupported(model: str) -> None:
    """Memo a model whose provider transport rejected the `thinking` kwarg."""
    if model and len(_THINKING_PARAM_UNSUPPORTED) < _THINKING_PARAM_MEMO_CAP:
        _THINKING_PARAM_UNSUPPORTED.add(model)


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

def _env_timeout_s(name: str, default: float) -> float:
    """Local mirror of ``shared.config._env_float`` (module-private there, and a
    single float read doesn't warrant a cross-module import of a leading-
    underscore helper). Never raises — a malformed override falls back silently.
    """
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


# Local-model timeout (DEBT-191). Was a single flat constant (300s) — wrong
# by construction: a fixed ceiling can never fit every deployment's hardware,
# because the actual constraint is real generation *speed*, which varies by
# an order of magnitude or more across CPU/low-VRAM setups (observed directly:
# ~2-3 tokens/sec on constrained hardware). A flat 300s budget covers barely
# 600-900 tokens at that speed — nowhere near a demanding structured-output
# call's real ceiling (e.g. the planner's MissionSpecification draft, sized up
# to several thousand output tokens for a broad request) — so every attempt
# degraded into an empty, unparseable response instead of a legibly slow one,
# no matter how many retries ran. Scaling the budget by the call's own
# `max_tokens` fixes the guessing-one-number problem structurally: a small
# call (a mini-judge classification) still times out quickly; a large one (a
# full plan draft) gets proportionally more room. All knobs are
# env-overridable for a deployment that needs to tune further. Cloud calls are
# untouched — they keep the caller-supplied default (60s).
_LOCAL_LLM_SECONDS_PER_TOKEN: float = _env_timeout_s("AILIENANT_LOCAL_LLM_SECONDS_PER_TOKEN", 0.5)
_LOCAL_LLM_TIMEOUT_CUSHION_S: float = _env_timeout_s("AILIENANT_LOCAL_LLM_TIMEOUT_CUSHION_S", 60.0)
_LOCAL_LLM_TIMEOUT_FLOOR_S: float = _env_timeout_s("AILIENANT_LOCAL_LLM_TIMEOUT_FLOOR_S", 300.0)

# Extra headroom folded into the `num_ctx` a local Ollama call requests, beyond
# the measured prompt + declared max_tokens — chat-template overhead and special
# tokens are real but not counted by `PrecisionTokenCounter`'s own estimate.
_NUM_CTX_CALL_MARGIN: int = int(os.getenv("AILIENANT_NUM_CTX_CALL_MARGIN", "256"))

# Adaptive calibration on top of the static formula above. The assumed
# 0.5s/token rate is still a guess — a smaller one than a flat 300s ceiling,
# but a guess nonetheless. Once a model has actually completed a few local
# calls, its OWN observed generation speed is a better estimate than any
# assumption. Deliberately layered, not a replacement: the static formula
# above remains the seed for a model with no history yet (the highest-stakes
# call — e.g. the very first large structured-generation request on unfamiliar
# hardware — has nothing to calibrate from until it survives once), and the
# floor stays load-bearing even after calibration kicks in.
#
# Recorded per resolved model string (not globally): a single deployment can
# have genuinely different-speed models behind different tiers (a 3B "small"
# and a bigger "big"), and collapsing them into one rate would misjudge one or
# the other. A bounded window of the most recent (completion_tokens,
# wall_clock_seconds) samples is kept per model; the rate is total tokens over
# total duration across the window (NOT an average of per-call ratios — a
# duration-weighted combination is the statistically correct way to merge
# heterogeneously-sized samples, and is naturally more robust to one noisy
# small-call ratio than an unweighted mean would be).
#
# A non-streaming call's wall-clock time also includes prompt-eval and model
# load time, not pure generation time — this makes the measured "seconds per
# completion token" somewhat pessimistic for a large-prompt call, which is the
# safe direction to be wrong in for a timeout budget. `_LOCAL_RATE_SAFETY_MARGIN`
# additionally inflates the measured rate before use, so normal run-to-run
# variance (a colder cache, thermal throttling, a bigger prompt than any
# sample seen so far) doesn't turn an average into a call that undershoots.
_LOCAL_RATE_WINDOW: int = 10
_LOCAL_RATE_MIN_SAMPLES: int = 2
# A handful of tiny completions (a mini-judge classification, a few tokens
# each) is NOT enough evidence to estimate a per-token rate — their wall-clock
# time is dominated by fixed per-request overhead (connection, prompt-eval,
# model warm-up), not steady-state generation, so a small-completion-only
# window wildly overestimates seconds/token (measured directly: two 3-token
# samples produced a >3-hour timeout estimate). Require a real minimum of
# accumulated OUTPUT before trusting the measurement — not just a sample
# count — so the estimate is actually dominated by generation time.
_LOCAL_RATE_MIN_TOTAL_TOKENS: int = 200
_LOCAL_RATE_SAFETY_MARGIN: float = _env_timeout_s("AILIENANT_LOCAL_RATE_SAFETY_MARGIN", 1.3)

_local_model_completions: Dict[str, "deque[tuple[int, float]]"] = {}


def _record_local_completion(model: str, completion_tokens: int, duration_s: float) -> None:
    """Feed one completed local call's (tokens, wall-clock time) into that
    model's rolling calibration window. Best-effort — a degenerate sample
    (no tokens, non-positive duration) is simply skipped, never raised.
    """
    if completion_tokens <= 0 or duration_s <= 0:
        return
    history = _local_model_completions.setdefault(model, deque(maxlen=_LOCAL_RATE_WINDOW))
    history.append((completion_tokens, duration_s))


def _measured_local_seconds_per_token(model: str) -> Optional[float]:
    """This model's own observed generation rate, or None with too few (or
    no) samples — or too little accumulated output — to trust over the
    static assumption."""
    history = _local_model_completions.get(model)
    if history is None or len(history) < _LOCAL_RATE_MIN_SAMPLES:
        return None
    total_tokens = sum(tokens for tokens, _ in history)
    total_duration = sum(duration for _, duration in history)
    if total_tokens < _LOCAL_RATE_MIN_TOTAL_TOKENS:
        return None
    return total_duration / total_tokens


def resolve_local_timeout(max_tokens: int, model: Optional[str] = None) -> float:
    """Local-tier request timeout, scaled to the call's own output ceiling.

    Prefers this model's own measured generation rate once enough completed
    calls have calibrated it; falls back to the conservative static
    assumption otherwise. Never below `_LOCAL_LLM_TIMEOUT_FLOOR_S` either way
    — calibration can only make the estimate more accurate, never remove the
    safety net.

    Public (not module-private) because `brain/agentic_cell.py` calls this too
    — the ReAct cell's own per-turn budget (`AGENTIC_CELL_MAX_ELAPSED_S`) must
    never be smaller than what a single local LLM call inside one iteration
    can legitimately need, or the governor's ceiling contradicts its own
    single operation's floor (DEBT-191 follow-up).
    """
    measured = _measured_local_seconds_per_token(model) if model else None
    seconds_per_token = (
        measured * _LOCAL_RATE_SAFETY_MARGIN if measured is not None else _LOCAL_LLM_SECONDS_PER_TOKEN
    )
    return max(_LOCAL_LLM_TIMEOUT_FLOOR_S, max_tokens * seconds_per_token + _LOCAL_LLM_TIMEOUT_CUSHION_S)


# A representative generation size for the Effort Budget's cost estimate — not
# a real ceiling, just the token count used to convert a seconds-per-token
# rate into a human-readable "how long does one extra call cost" figure. Set
# to _CODER_MIN_MAX_TOKENS-scale (a typical single-file generation), since
# both the self-heal retry and a `checks` command re-run are themselves
# roughly one-file-sized local calls.
_EFFORT_COST_REPRESENTATIVE_TOKENS: int = 4096

# How many EXTRA local calls each level typically costs beyond the generation
# every level already pays for — the same "typical extra calls" framing this
# codebase already uses for a coarse UI estimate (compare DEVELOPERS.md's own
# "~4 extra local calls" language for a deeper pipeline). Deliberately a
# labeled range, not a false-precision single number: a self-heal round or a
# `checks` command may or may not actually fire on a given turn.
_EFFORT_EXTRA_CALLS: Dict[str, str] = {
    "light": "0",
    "balanced": "0-1",
    "deep": "1-3",
}


def estimate_effort_costs(model: Optional[str] = None) -> Dict[str, Dict[str, Any]]:
    """Rough, honestly-labeled per-effort-level cost estimate for the UI.

    Replaces the old hardware-VRAM lock (a tier was simply unselectable below
    a VRAM floor) with a stated cost instead — Effort Budget levels cost local
    generation TIME, not VRAM headroom, so nothing here should ever be locked;
    the caller just gets told what a level is likely to cost before choosing it.

    Uses this model's own calibrated generation rate once enough completed
    calls exist (see `_measured_local_seconds_per_token`), falling back to the
    same conservative static assumption `resolve_local_timeout` itself uses —
    never a separate, second guess at the model's speed.
    """
    measured = _measured_local_seconds_per_token(model) if model else None
    seconds_per_token = measured if measured is not None else _LOCAL_LLM_SECONDS_PER_TOKEN
    seconds_per_extra_call = seconds_per_token * _EFFORT_COST_REPRESENTATIVE_TOKENS
    return {
        level: {
            "extra_calls": extra_calls,
            "seconds_per_extra_call": round(seconds_per_extra_call, 1),
            "calibrated": measured is not None,
        }
        for level, extra_calls in _EFFORT_EXTRA_CALLS.items()
    }


# Local-target transport retries (DEBT-191 follow-up). `LLM_MAX_TRANSPORT_RETRIES`
# (brain/retry_policy.py) exists for connection blips / transient 5xx against a
# remote provider — retrying re-issues the identical request at the identical
# full timeout (confirmed: litellm hands `timeout`/`max_retries` straight to the
# OpenAI SDK's async retry loop, which does exactly this; `litellm.exceptions.
# Timeout` IS in its default retryable set). For a local target, a timeout means
# the hardware is slow or the endpoint is dead — retrying doesn't fix either
# case, it just re-runs the same slow generation from scratch. Left at 1 (not 0)
# so a genuine transient local blip (a dropped socket, Ollama momentarily busy)
# still gets one retry; this caps the worst case at 2x `resolve_local_timeout(...)`
# instead of 3x. Cloud targets are unaffected — this constant is only read at the
# same `if target.is_local` branches `_effective_timeout` already uses.
_LOCAL_LLM_MAX_RETRIES: int = int(_env_timeout_s("AILIENANT_LOCAL_LLM_MAX_RETRIES", 1.0))

# Per-chunk idle bound for a local streaming call (DEBT-191 follow-up, part 2).
# `_effective_timeout`/`resolve_local_timeout` above bound the WHOLE call, sized
# generously for a slow-but-steady local model — but a call-level bound cannot
# distinguish "still generating, just slow" from "went completely silent" until
# the full multi-minute budget elapses. A stream that stops producing chunks
# entirely (the engine hung, or the process died) is a materially different
# fault and should surface far sooner than the full call timeout. Cloud targets
# are unaffected (only ever read where `target.is_local` gates it).
_LOCAL_STREAM_IDLE_TIMEOUT_S: float = _env_timeout_s("AILIENANT_LOCAL_STREAM_IDLE_TIMEOUT_S", 45.0)


class LocalStreamStalledError(RuntimeError):
    """Raised when a local streaming call goes silent mid-flight — no new
    chunk within `_LOCAL_STREAM_IDLE_TIMEOUT_S` — so a genuine stall becomes
    a visible, logged failure instead of the caller (and the user) waiting on
    the full call timeout, or indefinitely if the engine never recovers."""


async def _iter_with_stall_detection(
    stream: AsyncIterator[Any], *, idle_timeout_s: float, is_local: bool,
) -> AsyncIterator[Any]:
    """Re-yield ``stream``'s items, raising :class:`LocalStreamStalledError`
    if the upstream goes silent for longer than ``idle_timeout_s`` between
    items. A transparent passthrough for a non-local target — a cloud stall
    is already covered by the caller's own `timeout=` and the frontend's
    stream-stall watchdog.
    """
    if not is_local:
        async for item in stream:
            yield item
        return
    aiter = stream.__aiter__()
    while True:
        try:
            item = await asyncio.wait_for(aiter.__anext__(), timeout=idle_timeout_s)
        except StopAsyncIteration:
            return
        except asyncio.TimeoutError as exc:
            raise LocalStreamStalledError(
                f"Local model generation went silent for over {idle_timeout_s:.0f}s "
                "mid-stream — the engine may be overloaded or stuck. Ending this turn."
            ) from exc
        yield item


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
    """Local/cloud/human-required classification used for ledger accounting
    and tier selection — an internal gateway concept, not a wire copy of
    core.memory.context_auditor.derive_routing_decision's four-value tier
    decision (LOCAL_SMALL/LOCAL_MEDIUM/LOCAL_BIG/CLOUD)."""
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


def _maybe_log_action_tokens(
    action: Optional[str],
    prompt_tokens: int,
    completion_tokens: int,
    project_id: Optional[str] = None,
) -> None:
    """Feed the DEBT-045 calibration substrate — a no-op unless a caller tagged
    the call with a WBS action (today, only the coder's write_file/edit_file
    generation call does). Explicit opt-in, not ambient state: the caller passes
    the tag it already has in hand, so a concurrent Send() fan-out or a
    fire-and-forget side call (e.g. the coder-companion explanation pass) never
    gets attributed to a step it didn't serve. Mirrors the ledger accounting
    blocks this sits beside: best-effort, never raises, never blocks the call.
    """
    if not action:
        return
    try:
        from core.telemetry import log_action_tokens
        log_action_tokens(action, prompt_tokens + completion_tokens, project_id)
    except Exception as exc:  # noqa: BLE001 — calibration telemetry is non-fatal
        logger.debug("Action-token telemetry failed (non-fatal): %s", exc)


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

    OFFLINE FALLBACK ONLY — see :func:`supports_native_thinking` for the
    runtime-probed check every call site should actually use. This substring
    guess is what let a model that genuinely supports reasoning (e.g. Ollama's
    own `gemma4`, which reports `capabilities: ["thinking"]` at `/api/show`)
    go unrecognised for good just because its name was never added to the hint
    list below — the model's own free-form "think out loud" pass ran in its
    place, silently, for every call. Kept only for providers a capability probe
    cannot reach (no `/api/show`-equivalent, or the probe itself fails).

    Best-effort, substring-based. A false negative here just means the offline
    fallback triggers unnecessarily (the stream shows no Thought Box); a false
    positive is also harmless — a provider that rejects the ``thinking`` param
    raises, and the orchestration layer falls back to the flat-text path.
    """
    lowered = (model_name or "").lower()
    return any(hint in lowered for hint in _NATIVE_THINKING_MODEL_HINTS)


async def supports_native_thinking(target: Optional[Any]) -> bool:
    """Whether ``target`` emits native reasoning tokens — ASKED of the runtime,
    not guessed from the model's name.

    Probes :func:`core.config.model_resolver.probe_runtime_capabilities`,
    which reports a real ``capabilities`` list for a reachable Ollama target.
    When that probe genuinely resolved the target's real context length (i.e.
    it actually got an answer), its ``supports_thinking`` verdict is trusted
    outright — a positive OR a negative, both real. The substring heuristic
    above is used ONLY when the probe could not determine anything at all
    (a non-Ollama provider with no equivalent endpoint, an unreachable local
    engine, or a malformed response) — an offline fallback, not the primary
    signal.
    """
    if target is None:
        return False
    from core.config.model_resolver import probe_runtime_capabilities

    caps = await probe_runtime_capabilities(target)
    if caps.context_length is not None:
        return caps.supports_thinking
    return _supports_native_thinking(getattr(target, "model", "") or "")


# Streaming structured-output capability gate.
# Providers that honour ``response_format`` *while streaming* (OpenAI-style JSON
# mode on a streamed completion). Default-deny: anything not listed keeps the
# prompt-enforced + sanitizer path, so a provider that would reject the param on
# a stream (Anthropic has no response_format; some local/reasoner builds 400 on
# it) is never sent it. This is the single tuning point — add a provider only
# once its streaming JSON mode is verified.
_STREAMING_STRUCTURED_PROVIDERS: frozenset[str] = frozenset({"openai"})

# Extra output-token headroom granted to a simulated-reasoning turn so the
# scaffolded ``<thinking>`` block cannot starve the answer (the flat stream
# shares one ``max_tokens`` across reasoning + answer). Capped so a runaway
# reasoner cannot request an unbounded completion.
_SIM_THINK_CAP: int = 4096


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


def _attach_images_to_messages(
    messages: list[dict[str, Any]],
    images: Optional[List[Dict[str, str]]],
    model: str,
    trace_id: str,
) -> list[dict[str, Any]]:
    """Turn the last user message's plain-string content into an OpenAI-style
    multimodal content-block list (DEBT-168).

    Must run against the physically resolved ``model`` (post BYOM-alias
    resolution), never the caller-supplied alias — ``litellm.supports_vision``
    only knows real model names. A local pure-Python check (``litellm`` reads
    its own bundled model-cost map, no network, never raises), so this never
    blocks on I/O; the size/count ceilings are checked via ``len()`` before any
    string is built, so an oversized request is refused in O(1) rather than
    paying for the block construction. Every rejection path is loud (a WARNING
    naming the reason) and returns ``messages`` unchanged — never a silent drop,
    since litellm.supports_vision() itself returns False (not an error) for any
    model absent from its map, which includes most local BYOM targets.
    """
    if not images:
        return messages
    if not litellm.supports_vision(model):
        logger.warning(
            "Vision attachment(s) not sent — model=%s is not recognized as "
            "vision-capable [trace=%s]",
            model, trace_id,
        )
        return messages

    count = len(images)
    total_chars = sum(len(img.get("data") or "") for img in images)
    if count > VISION_MAX_IMAGES_PER_CALL or total_chars > VISION_MAX_TOTAL_BASE64_CHARS:
        logger.warning(
            "Vision attachment(s) refused — count=%d (max %d) total_base64_chars=%d "
            "(max %d) [trace=%s]",
            count, VISION_MAX_IMAGES_PER_CALL, total_chars,
            VISION_MAX_TOTAL_BASE64_CHARS, trace_id,
        )
        return messages

    last_user_idx: Optional[int] = None
    for i in range(len(messages) - 1, -1, -1):
        if messages[i].get("role") == "user":
            last_user_idx = i
            break
    if last_user_idx is None:
        return messages

    original = messages[last_user_idx]
    blocks: List[Dict[str, Any]] = [{"type": "text", "text": original.get("content", "")}]
    for img in images:
        data = img.get("data") or ""
        if not data:
            continue
        mime = img.get("mime") or "image/png"
        blocks.append({"type": "image_url", "image_url": {"url": f"data:{mime};base64,{data}"}})

    new_messages = list(messages)
    new_messages[last_user_idx] = {**original, "content": blocks}
    return new_messages


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
        # An all-optional schema makes `required` empty, and every dict is a
        # superset of the empty set — so the walk would match the outermost
        # envelope and validate into a fully-defaulted instance. For a schema
        # whose default IS a meaningful signal (an empty GrillQuestionBatch means
        # "done asking"), that silently turns a malformed response into a
        # confident wrong answer. Prefer the first node carrying any declared
        # field; fall back to the plain match when the tree has none.
        if target is not None and not required:
            declared = set(schema_class.model_fields)
            if declared and not (set(target) & declared):
                target = _find_node_with_any_key(parsed, declared) or target
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
        action: Optional[str] = None,
        images: Optional[List[Dict[str, str]]] = None,
    ) -> ModelResponse:
        """Async LLM call — non-blocking on the FastAPI event loop.

        — pass `tier=Tier.LOCAL` or `tier=Tier.CLOUD` to route via
        the priority map (overrides `model`). If `tier` is None, `model` is used
        directly and the tier is inferred from the model name for accounting.

        — `state` is the optional LangGraph state dict. When supplied,
        an OOM-class failure mutates it (`oom_fallback_active`, `security_flags`)
        before re-emitting to the cloud fallback model; see `_oom_cascade`.

        — `action` optionally tags the call with the WBS action it serves
        (e.g. "write_file") so DEBT-045's calibration substrate can attribute
        real token usage to that action. Omitted by default; a caller opts in
        explicitly rather than this being inferred from ambient context.

        — `images` optionally attaches vision content blocks (each a
        `{"data": base64_str, "mime": "image/png"}` dict, mirroring
        `ManualAttachment`) to the last user message, gated on the physically
        resolved model actually supporting vision (DEBT-168). A caller on a
        non-vision target is unaffected — `messages` passes through unchanged.
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
        # Authoritative local/cloud signal for the ledger — the physically resolved
        # target, not the alias name. Stays None only when no BYOM preset resolves
        # (back-compat litellm-proxy path), where the alias-name classifier applies.
        resolved_is_local: Optional[bool] = None
        _effective_timeout = timeout  # default; overridden below for a resolved local target
        _effective_max_retries = LLM_MAX_TRANSPORT_RETRIES  # default; reduced below for a local target
        if effective_model.startswith("ailienant/"):
            from core.config.model_resolver import get_chat_target
            _alias_tier = effective_model.split("/", 1)[1]
            _target = get_chat_target(
                _alias_tier if _alias_tier in ("small", "medium", "big", "cloud") else "medium"
            )
            if _target is not None:
                resolved_is_local = _target.is_local
                _effective_timeout = (
                    resolve_local_timeout(max_tokens, _target.model) if _target.is_local else timeout
                )
                _effective_max_retries = (
                    _LOCAL_LLM_MAX_RETRIES if _target.is_local else LLM_MAX_TRANSPORT_RETRIES
                )
                byom_kwargs = {"model": _target.model}
                if _target.api_base:
                    byom_kwargs["api_base"] = _target.api_base
                if _target.api_key:
                    byom_kwargs["api_key"] = _target.api_key
                _num_ctx_kwarg = await LLMGateway._resolve_local_num_ctx_kwarg(
                    _target, messages, max_tokens
                )
                byom_kwargs.update(_num_ctx_kwarg)
                if _target.is_local and "num_ctx" in _num_ctx_kwarg:
                    from core.config.model_resolver import check_local_admission
                    await check_local_admission(_target, _num_ctx_kwarg["num_ctx"])

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
                max_retries=_effective_max_retries,
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
        if images:
            # Must run against kwargs["model"] (the physically resolved target,
            # post BYOM-alias resolution) — the caller-supplied `model`/`tier`
            # alias tells us nothing about vision capability.
            kwargs["messages"] = _attach_images_to_messages(
                kwargs["messages"], images, str(kwargs["model"]), trace_id,
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
            # Timed from inside the semaphore hold, not from admission — this
            # measures the model's own generation speed, not AILIENANT's own
            # concurrency queueing, which would otherwise pollute the DEBT-191
            # calibration below with an unrelated wait.
            _call_started = time.monotonic()
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
                    # Re-armed so a calibration sample below reflects only the
                    # call that actually produced its completion_tokens — not
                    # the wasted first attempt's negotiation round-trip too.
                    _call_started = time.monotonic()
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
                # Accuracy order: a physically resolved target's `is_local` is the
                # ground truth (it reflects what actually served the call and burned
                # local GPU vs cloud API budget), so it wins even over an explicit
                # `tier` — a `tier=CLOUD` alias that BYOM resolves to a local model is
                # local spend. Only when no target resolved do we fall back to the
                # requested tier, then to the alias-name heuristic.
                resolved_tier: TaskPriority
                if resolved_is_local is not None:
                    resolved_tier = (
                        TaskPriority.LOCAL if resolved_is_local else TaskPriority.CLOUD
                    )
                elif tier is not None:
                    resolved_tier = tier
                else:
                    resolved_tier = _classify_model_as_tier(effective_model)
                if resolved_tier == TaskPriority.CLOUD:
                    token_ledger.record_cloud(prompt_tokens, completion_tokens)
                else:
                    token_ledger.record_local(prompt_tokens, completion_tokens)
                    # DEBT-191: feed this completed call's own speed into that
                    # model's rolling calibration window, gated on the
                    # physically resolved target actually being local (not
                    # merely the requested tier — see the accuracy-order
                    # comment above) so a cloud completion never pollutes a
                    # local model's estimate under the wrong key.
                    if resolved_is_local:
                        _record_local_completion(
                            str(kwargs["model"]), completion_tokens,
                            time.monotonic() - _call_started,
                        )
                _maybe_log_action_tokens(
                    action, prompt_tokens, completion_tokens,
                    project_id=state.get("project_id") if state else None,
                )
        except Exception as exc:
            logger.debug("Token accounting failed (non-fatal): %s", exc)

        # `finish_reason == "length"` is the provider's own unambiguous statement
        # that it stopped because it hit the output ceiling, not because the
        # answer was finished — the single most diagnostic signal for a truncated
        # structured response, and until now it was discarded unread along with
        # the rest of the response envelope. A strict-JSON caller downstream just
        # sees an unparseable fragment with no way to tell "cut short" apart from
        # "model ignored the contract", which is exactly the ambiguity that made a
        # live planner failure take a full log-forensics pass to explain.
        try:
            _finish = getattr(response.choices[0], "finish_reason", None)
            if _finish == "length":
                logger.warning(
                    "LLM response TRUNCATED at the output ceiling (finish_reason=length) "
                    "— model=%s max_tokens=%d trace=%s. A structured/JSON caller will "
                    "receive an unparseable fragment; lower the request's scope or raise "
                    "the model's context window.",
                    kwargs.get("model"), max_tokens, trace_id,
                )
            # Output-side telemetry (13.1.3): every existing CONTEXT record
            # measures the input side only — nothing previously recorded what
            # the model actually generated, whether the provider itself says the
            # completion was cut short, or what window it was served under. See
            # log_generation_utilization's own docstring for why this exists.
            from core.telemetry_log import log_generation_utilization
            _usage = getattr(response, "usage", None)
            log_generation_utilization(
                session_id=trace_id,
                model=str(kwargs.get("model", effective_model)),
                prompt_tokens=int(getattr(_usage, "prompt_tokens", 0) or 0) if _usage else 0,
                completion_tokens=int(getattr(_usage, "completion_tokens", 0) or 0) if _usage else 0,
                finish_reason=_finish,
                num_ctx=kwargs.get("num_ctx"),
            )
        except Exception:  # noqa: BLE001 — a diagnostic must never sink a good call
            logger.debug("finish_reason/telemetry probe failed (non-fatal)", exc_info=True)

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
        on_thinking: Optional[Callable[[str, str], Awaitable[None]]] = None,
        enable_thinking: bool = False,
        thinking_budget_tokens: int = 4096,
        action: Optional[str] = None,
    ) -> str:
        """Structured completion that streams reasoning while it works.

        A single entry point with two branches so the caller's code is identical
        regardless of model:

        - **Reasoning branch** (taken whenever a reasoning sink is wired AND
          thinking is enabled): consume :meth:`astream_reasoning` — which picks the
          model's native reasoning stream or a prompt-scaffolded simulated one — push
          each reasoning delta to ``on_thinking(text, source)`` (best-effort), and
          accumulate the answer tokens into an in-memory buffer that is returned.
        - **Fallback branch** (no sink, or thinking disabled): delegate to
          :meth:`ainvoke`, preserving ``response_format``, the OOM cascade, and
          response-cache compatibility. Behaviour is byte-identical to a direct
          ``ainvoke`` call — no scaffold, no wasted tokens.

        Streaming is *best-effort*; generation is *mission-critical*. A failure in
        the reasoning sink (e.g. a closed WebSocket) is swallowed and the sink is
        latched off for the rest of the call — the answer buffer keeps filling, so
        the structured result is always returned intact. ``CancelledError`` (a real
        abort) is never swallowed.

        A ``response_format`` request on a non-native model never shares a
        completion with a reasoning preamble (see :meth:`astream_reasoning`'s
        SAFETY INVARIANT) — it routes through :meth:`ainvoke` internally, which
        forwards ``response_format`` where the provider supports it. The buffered
        answer is still run through :meth:`_sanitize_json_response` as a universal
        backstop (e.g. a native model whose provider doesn't support streamed
        ``response_format``), so the downstream parser never trips on a fence.
        """
        # Derive the BYOM tier from the alias, mirroring ainvoke's resolution.
        _alias_tier = model.split("/", 1)[1] if model.startswith("ailienant/") else "medium"
        tier = _alias_tier if _alias_tier in ("small", "medium", "big", "cloud") else "medium"

        # Forwarded only when the caller actually tagged the call — omitted
        # entirely (not even as action=None) so a test double mocking one of
        # these methods with its own fixed, enumerated signature (no **kwargs
        # catch-all) doesn't break on an untagged call, which is the common
        # case. See _maybe_log_action_tokens for what this ultimately feeds.
        _extra_action: _ActionKwarg = {"action": action} if action else {}

        sink_wired = on_thinking is not None and enable_thinking
        if not sink_wired:
            resp = await LLMGateway.ainvoke(
                messages=messages,
                model=model,
                temperature=temperature,
                response_format=response_format,
                max_tokens=max_tokens,
                timeout=timeout,
                session_id=session_id,
                state=state,
                **_extra_action,
            )
            return resp.choices[0].message.content or ""

        # Reasoning branch. on_thinking is guaranteed non-None by sink_wired.
        assert on_thinking is not None  # narrowed by sink_wired
        sink: Callable[[str, str], Awaitable[None]] = on_thinking
        buffer: List[str] = []
        sink_live = True
        async for delta in LLMGateway.astream_reasoning(
            messages,
            tier=tier,
            temperature=temperature,
            max_tokens=max_tokens,
            timeout=timeout,
            session_id=session_id,
            thinking_budget_tokens=thinking_budget_tokens,
            response_format=response_format,
            **_extra_action,
        ):
            if delta.kind == "thinking":
                # Best-effort: a dead sink must never abort generation.
                if sink_live:
                    try:
                        await sink(delta.text, delta.source)
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:  # noqa: BLE001 — streaming is best-effort
                        logger.debug("thinking sink failed; latching off: %s", exc)
                        sink_live = False
            else:  # "text" — the answer channel (mission-critical)
                buffer.append(delta.text)

        answer = "".join(buffer)
        # Dropping response_format reintroduces ```json fences (and a simulated turn
        # emits reasoning before the JSON); strip so the downstream parser sees
        # clean JSON.
        if response_format:
            answer = LLMGateway._sanitize_json_response(answer)
        return answer

    @staticmethod
    async def astream_reasoning(
        messages: list[dict[str, Any]],
        tier: str = "medium",
        *,
        temperature: float = 0.0,
        max_tokens: int = 4096,
        timeout: float = 60.0,
        session_id: Optional[str] = None,
        thinking_budget_tokens: int = 4096,
        response_format: Optional[dict[str, Any]] = None,
        free_form_answer: bool = False,
        action: Optional[str] = None,
    ) -> AsyncIterator["StreamDelta"]:
        """Reasoning-aware stream — one engine, native or simulated, chosen once.

        Resolves the active BYOM tier and yields :class:`StreamDelta` values tagged
        ``kind`` (``thinking``/``text``) and ``source`` (``native``/``simulated``):

        - **Native** (runtime confirms the target via ``supports_native_thinking``):
          delegate to :meth:`astream_byom_thinking`, which surfaces the model's own
          ``reasoning_content`` — behaviour identical to before. Reasoning is a
          genuinely separate channel here, so it never competes with the answer.
        - **Simulated, strict output** (non-native model, ``response_format`` set):
          see the SAFETY INVARIANT below — never scaffolded.
        - **Simulated, no scaffold** (non-native model, ``free_form_answer`` is
          falsy): plain unscaffolded stream — the default, see below.
        - **Simulated, scaffolded** (non-native model, ``free_form_answer=True``,
          no ``response_format``): inject a code-free reasoning scaffold on a
          *copied* message list (the caller's list is never mutated), stream flat
          text, and split it live with :class:`ThinkingTagDemuxer` so the
          ``<thinking>`` block streams to the reasoning channel and the remainder
          is the answer. The reasoning gets its own token headroom so it never
          starves the answer.

        SAFETY INVARIANT (2026-07-25 incident — a recurrence of DEBT-013's failure
        class on the new simulated path): a non-reasoning model asked to narrate
        free-form reasoning *and* produce strict, machine-parsed output (a JSON
        schema, or a marker-delimited format like the Coder's SEARCH/REPLACE
        contract) in the SAME completion will unreliably corrupt that output —
        observed as silently dropped required JSON fields (`MissionSpecification`)
        and edits that were never emitted at all (the Coder's own "no prose before
        or after" instruction directly contradicts a reasoning preamble). Because
        of this, scaffolding is **off by default** — a caller must explicitly pass
        ``free_form_answer=True`` to prove its answer is free prose/markdown that
        is never machine-parsed (see ``agents/analyst.py`` and
        ``core/task_service.py::_stream_with_thinking`` for the only two current
        callers that do). ``response_format`` being set always overrides
        ``free_form_answer`` — an explicit contradiction from a caller is treated
        as a bug, not honored — and additionally routes through :meth:`ainvoke`
        rather than the flat stream, restoring provider-level JSON enforcement +
        self-heal where supported (the true pre-11.5 behavior for this case, not
        merely "no scaffold"). Do not weaken this default without re-reading the
        incident above; a new caller that doesn't think about this should get the
        safe behavior, not the dangerous one.

        Exactly one path fires per call — native and simulated never mix.
        """
        from tools.stream_delta import StreamDelta
        from tools.thinking_demux import ThinkingTagDemuxer
        from core.config.model_resolver import get_chat_target  # deferred — load order

        target = get_chat_target(tier)
        native = target is not None and await supports_native_thinking(target)
        # See acomplete_with_thinking's identical comment: omitted entirely when
        # untagged so a fixed-signature test double never breaks on this kwarg.
        _extra_action: _ActionKwarg = {"action": action} if action else {}

        if native:
            # Provider-enforced JSON mode on the stream only where supported;
            # elsewhere the caller's sanitizer recovers the JSON (DEBT-013).
            stream_rf = (
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
                response_format=stream_rf,
                **_extra_action,
            ):
                yield delta  # already tagged source="native" by construction
            return

        if response_format:
            # See SAFETY INVARIANT above — never scaffolded, regardless of
            # free_form_answer. ainvoke gives the best available JSON compliance
            # (provider enforcement + self-heal where supported); callers that
            # reach this branch already buffer the full answer downstream, so
            # nothing is lost by not streaming incrementally.
            resp = await LLMGateway.ainvoke(
                messages,
                model=f"ailienant/{tier}",
                temperature=temperature,
                response_format=response_format,
                max_tokens=max_tokens,
                timeout=timeout,
                session_id=session_id,
                **_extra_action,
            )
            yield StreamDelta("text", resp.choices[0].message.content or "", "simulated")
            return

        if not free_form_answer:
            # SAFETY DEFAULT — see the invariant above. Plain unscaffolded stream:
            # no reasoning shown, but the answer is otherwise unaffected and still
            # streams incrementally.
            async for chunk in LLMGateway.astream_byom(
                messages,
                tier=tier,
                temperature=temperature,
                max_tokens=max_tokens,
                timeout=timeout,
                session_id=session_id,
                **_extra_action,
            ):
                yield StreamDelta("text", chunk, "simulated")
            return

        # Scaffolded path — reached only when free_form_answer=True and no
        # response_format (the caller has explicitly declared this is safe).
        scaffolded = LLMGateway._inject_reasoning_scaffold(messages)
        sim_max_tokens = max_tokens + min(thinking_budget_tokens, _SIM_THINK_CAP)
        demux = ThinkingTagDemuxer()
        async for chunk in LLMGateway.astream_byom(
            scaffolded,
            tier=tier,
            temperature=temperature,
            max_tokens=sim_max_tokens,
            timeout=timeout,
            session_id=session_id,
            **_extra_action,
        ):
            reasoning, answer = demux.feed(chunk)
            if reasoning:
                yield StreamDelta("thinking", reasoning, "simulated")
            if answer:
                yield StreamDelta("text", answer, "simulated")
        reasoning, answer = demux.finish()
        if reasoning:
            yield StreamDelta("thinking", reasoning, "simulated")
        if answer:
            yield StreamDelta("text", answer, "simulated")

    @staticmethod
    def _inject_reasoning_scaffold(
        messages: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Return a copy of ``messages`` with a code-free reasoning scaffold added.

        The scaffold elicits a conceptual, narrative ``<thinking>`` block — no code,
        no variable dumps, no answer — for models without native reasoning. Appended
        to the first system message if present, else prepended as a new one. Each
        message dict is shallow-copied so the caller's list (which a retry loop may
        re-invoke) is never mutated.

        Only ever called for a free-form answer with no ``response_format`` (see
        ``astream_reasoning``'s SAFETY INVARIANT) — the scaffold is deliberately
        format-agnostic; it never needs a JSON-specific clause because a strict
        output contract is never combined with this scaffold in the first place.
        """
        scaffold = (
            "Before answering, think out loud inside one <thinking>...</thinking> "
            "block. Narrate a clear first-person train of thought: what you are "
            "weighing, the trade-offs, what you need to check, and how you reach your "
            "conclusion. Write professionally but plainly. Do NOT put code, file "
            "contents, variable dumps, or the final answer inside <thinking> — "
            "reasoning only. After </thinking>, give your final answer."
        )
        out = [dict(m) for m in messages]
        for msg in out:
            if msg.get("role") == "system":
                msg["content"] = f"{msg.get('content', '')}\n\n{scaffold}"
                return out
        out.insert(0, {"role": "system", "content": scaffold})
        return out

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
    async def _resolve_local_num_ctx_kwarg(
        target: Any, messages: list[dict[str, Any]], max_tokens: int,
    ) -> dict[str, int]:
        """``{"num_ctx": N}`` for a local Ollama target, or ``{}`` otherwise.

        Every local call used to be served at Ollama's own silent 4096-token
        default regardless of the model's real capacity or what this specific
        call actually needs — the direct physical cause of a live planner
        failure whose forensics took a full log investigation to explain (a
        prompt plus its requested completion exceeded 4096, Ollama silently
        context-shifted the prompt to make room, and the model drifted off the
        JSON contract it could no longer fully see). Sizing this explicitly
        closes that hole for every BYOM call site, not just the one it was
        first found on.

        Never raises: a probe/resolution fault must degrade to omitting the
        kwarg entirely (today's behaviour), never block or corrupt the call it
        is trying to improve.
        """
        if not getattr(target, "is_local", False):
            return {}
        try:
            from core.config.model_resolver import resolve_num_ctx
            from tools.token_counter import PrecisionTokenCounter

            prompt_text = "\n".join(str(m.get("content", "")) for m in messages)
            prompt_tokens = PrecisionTokenCounter.estimate_with_buffer(prompt_text, target.model)
            num_ctx = await resolve_num_ctx(
                target, min_required=prompt_tokens + max_tokens + _NUM_CTX_CALL_MARGIN,
            )
            return {"num_ctx": num_ctx} if num_ctx is not None else {}
        except Exception:  # noqa: BLE001 — degrade to omitting the kwarg, never block the call
            logger.debug("num_ctx resolution failed (non-fatal)", exc_info=True)
            return {}

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
        _effective_timeout = resolve_local_timeout(max_tokens, target.model) if target.is_local else timeout
        _effective_max_retries = _LOCAL_LLM_MAX_RETRIES if target.is_local else LLM_MAX_TRANSPORT_RETRIES
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
                    timeout=_effective_timeout, max_retries=_effective_max_retries,
                    **await LLMGateway._resolve_local_num_ctx_kwarg(target, messages, max_tokens),
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
                    _effective_timeout = resolve_local_timeout(max_tokens, target.model) if target.is_local else timeout
                    _effective_max_retries = _LOCAL_LLM_MAX_RETRIES if target.is_local else LLM_MAX_TRANSPORT_RETRIES
                    attempted_failover = True

    @staticmethod
    async def astream_byom(
        messages: list[dict[str, Any]],
        tier: str = "medium",
        temperature: float = 0.4,
        max_tokens: int = 1024,
        timeout: float = 60.0,
        session_id: Optional[str] = None,
        action: Optional[str] = None,
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
        _effective_timeout = resolve_local_timeout(max_tokens, target.model) if target.is_local else timeout
        _effective_max_retries = _LOCAL_LLM_MAX_RETRIES if target.is_local else LLM_MAX_TRANSPORT_RETRIES
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
                    _num_ctx_kwarg = await LLMGateway._resolve_local_num_ctx_kwarg(
                        target, messages, max_tokens
                    )
                    if target.is_local and "num_ctx" in _num_ctx_kwarg:
                        from core.config.model_resolver import check_local_admission
                        await check_local_admission(target, _num_ctx_kwarg["num_ctx"])
                    kwargs = LLMGateway._byom_kwargs(
                        target, messages, temperature=temperature, max_tokens=max_tokens,
                        timeout=_effective_timeout, stream=True, max_retries=_effective_max_retries,
                        **_num_ctx_kwarg,
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
                        _effective_timeout = resolve_local_timeout(max_tokens, target.model) if target.is_local else timeout
                        _effective_max_retries = _LOCAL_LLM_MAX_RETRIES if target.is_local else LLM_MAX_TRANSPORT_RETRIES
                        attempted_failover = True
                async for chunk in _iter_with_stall_detection(
                    response, idle_timeout_s=_LOCAL_STREAM_IDLE_TIMEOUT_S, is_local=target.is_local,
                ):
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
                    _maybe_log_action_tokens(action, prompt_tokens, completion_tokens)
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
        action: Optional[str] = None,
    ) -> AsyncIterator["StreamDelta"]:
        """Thinking-aware streaming completion against the active BYOM chat model.

        Bifurcates each upstream chunk into ``StreamDelta`` values tagged
        ``"thinking"`` (native reasoning tokens) or ``"text"`` (answer tokens).
        The legacy ``astream_byom`` is deliberately left untouched and remains
        the flat-text fallback path; callers select this method only when the
        user has Native Thinking enabled.

        ``thinking`` config is appended ONLY when ``enable_thinking`` is true AND
        ``supports_native_thinking`` confirms the active model via the runtime
        probe (falling back to the offline substring guess only when the probe
        itself cannot resolve anything). Otherwise the param is omitted entirely
        → the provider streams plain text and this generator simply never
        yields a ``"thinking"`` delta (zero regression).

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
        _effective_timeout = resolve_local_timeout(max_tokens, target.model) if target.is_local else timeout
        _effective_max_retries = _LOCAL_LLM_MAX_RETRIES if target.is_local else LLM_MAX_TRANSPORT_RETRIES
        thinking_on = (
            bool(enable_thinking)
            and await supports_native_thinking(target)
            and target.model not in _THINKING_PARAM_UNSUPPORTED
        )
        # A native reasoning turn generates thinking_content ON TOP OF the answer,
        # so the window must hold both — sizing num_ctx off max_tokens alone would
        # reproduce this exact bug's shape for the one case this parameter exists.
        _num_ctx_max_tokens = max_tokens + thinking_budget_tokens if thinking_on else max_tokens
        _num_ctx_kwarg = await LLMGateway._resolve_local_num_ctx_kwarg(
            target, messages, _num_ctx_max_tokens
        )
        if target.is_local and "num_ctx" in _num_ctx_kwarg:
            from core.config.model_resolver import check_local_admission
            await check_local_admission(target, _num_ctx_kwarg["num_ctx"])
        kwargs = LLMGateway._byom_kwargs(
            target, messages, temperature=temperature, max_tokens=max_tokens,
            timeout=_effective_timeout, stream=True, max_retries=_effective_max_retries,
            **_num_ctx_kwarg,
        )
        kwargs.setdefault("stream_options", {"include_usage": True})
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
                    elif "thinking" in kwargs and isinstance(exc, UnsupportedParamsError):
                        # A capability probe saying the MODEL can reason is not the
                        # same fact as this litellm provider transport accepting a
                        # `thinking=` kwarg — memo and retry flat, same shape as above.
                        logger.warning(
                            "Backend rejected native `thinking` param; stripping + retrying once [trace=%s]",
                            trace_id,
                        )
                        _remember_thinking_unsupported(kwargs["model"])
                        kwargs.pop("thinking", None)
                        response = cast(CustomStreamWrapper, await litellm.acompletion(**kwargs))
                    else:
                        raise
                async for chunk in _iter_with_stall_detection(
                    response, idle_timeout_s=_LOCAL_STREAM_IDLE_TIMEOUT_S, is_local=target.is_local,
                ):
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
                    _maybe_log_action_tokens(action, prompt_tokens, completion_tokens)
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
