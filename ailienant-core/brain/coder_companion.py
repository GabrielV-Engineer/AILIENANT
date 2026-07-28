# ailienant-core/brain/coder_companion.py
#
# Coder Companion — structured post-turn explanation.
#
# Background task pipeline: asynchronously synthesizes a structured explanation
# (objective / decisions / patterns / bottlenecks / errors / follow-ups) after the
# Coder produces a patch set. Explanation emits over WS, rendered alongside the
# diff-approval UI. Best-effort, fire-and-forget — never blocks route_after_coder.

import asyncio
import logging
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Set, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from shared.config import MINI_JUDGE_MODEL
from tools.llm_gateway import LLMGateway
from core.vfs_middleware import make_safe_reader

logger = logging.getLogger("CODER_COMPANION")

# Strong reference set: prevents GC from destroying broadcast tasks mid-flight.
_companion_background_tasks: Set[asyncio.Task[None]] = set()
_companion_semaphore = asyncio.Semaphore(3)

_MAX_TOKENS_BY_VERBOSITY = {"minimal": 220, "normal": 420, "deep": 800}
# A local judge model needs far more than a cloud call's latency budget (the gateway
# itself grants local calls up to 300s, see llm_gateway._LOCAL_LLM_TIMEOUT_S) — 12s
# was calibrated for a cloud round-trip and reliably starves a local completion before
# it can finish. Kept as two named constants (rather than one bumped flat value) so a
# fast cloud judge still gets a fast unavailable-fallback instead of waiting needlessly.
_COMPANION_LLM_TIMEOUT_CLOUD_S = 12.0
_COMPANION_LLM_TIMEOUT_LOCAL_S = 45.0
_MAX_CONCURRENT_COMPANIONS = 3
_MAX_DIFF_CHARS_PER_FILE = 4000
_MAX_FILES_IN_PAYLOAD = 8


@dataclass(frozen=True)
class CompanionAnalysisRequest:
    """Frozen request signature for caching and passing to LLM."""
    session_id: str
    task_id: str
    attempt_ordinal: int
    task_description: str
    pending_patches: Dict[str, str]
    pending_contents: Dict[str, str]
    file_context: Dict[str, str]
    relevant_errors: List[str]
    security_flags: List[str]
    verbosity: Literal["minimal", "normal", "deep"]


class CompanionDecision(BaseModel):
    """A single design decision and its rationale."""
    name: str
    rationale: str
    risk: Optional[str] = None
    tradeoff: Optional[str] = None


class CompanionAnalysis(BaseModel):
    """Structured explanation output. Always degrades gracefully on failure."""
    model_config = ConfigDict(extra="ignore")

    objective: str
    decisions: List[CompanionDecision] = Field(default_factory=list)
    patterns_applied: List[str] = Field(default_factory=list)
    bottlenecks: List[str] = Field(default_factory=list)
    security_notes: List[str] = Field(default_factory=list)
    errors_found: List[str] = Field(default_factory=list)
    follow_ups: List[str] = Field(default_factory=list)
    reasoning_summary: Optional[str] = None
    degraded: bool = False


def _resolve_verbosity(state: Dict[str, Any]) -> Literal["minimal", "normal", "deep"]:
    """Heuristic: resolve verbosity from structural state signals."""
    errors = state.get("errors") or []
    security_flags = state.get("security_flags") or []
    pending_patches = state.get("pending_patches") or {}

    if errors or security_flags:
        return "deep"
    if len(pending_patches) > 3:
        return "deep"
    if len(pending_patches) == 1 and not errors:
        return "minimal"
    return "normal"


def _companion_budget_available(state: Dict[str, Any]) -> bool:
    """Soft pre-check: skip if already over budget ceiling."""
    current = state.get("current_cost_usd", 0.0)
    ceiling = state.get("max_budget_usd", float("inf"))
    return current < ceiling


async def _companion_gpu_slot_available() -> bool:
    """Non-blocking VRAM-lock probe. Never queue the Companion behind the user's real work.

    A cloud-tier judge model never contends for local VRAM, so it is always admitted.
    A local-tier model must yield instantly rather than wait on the GPU lock the user's
    coding turn holds — an explanation nobody may read must never delay the real task.
    """
    # MVP: unconditionally admit. A local-tier non-blocking GPUResourceManager probe
    # (yield rather than queue when the lock is held) is tracked as DEBT-121.
    return True


def _build_companion_request(state: Dict[str, Any], attempt_ordinal: int) -> CompanionAnalysisRequest:
    """Assemble request from state fields only (no invented data)."""
    session_id = state.get("task_id", "")
    task_id = state.get("task_id", "")
    mission_spec = state.get("mission_spec")
    task_description = ""
    if mission_spec and mission_spec.tasks:
        step_id = state.get("current_step_id")
        task = next((t for t in mission_spec.tasks if t.step_number == step_id), None)
        if task:
            task_description = task.description

    pending_patches = state.get("pending_patches") or {}
    pending_contents = state.get("pending_contents") or {}
    relevant_errors = state.get("errors") or []
    security_flags = state.get("security_flags") or []

    verbosity = _resolve_verbosity(state)

    # Truncate file context to the token budget before it enters an LLM prompt.
    file_context = {}
    workspace_root = state.get("workspace_root") or ""
    project_id = state.get("project_id") or ""
    session_id_for_vfs = session_id
    try:
        vfs_reader = make_safe_reader(project_id, workspace_root, session_id_for_vfs)
        for filepath in list(pending_patches.keys())[:_MAX_FILES_IN_PAYLOAD]:
            try:
                content = vfs_reader(filepath)
                if content:
                    file_context[filepath] = content[:_MAX_DIFF_CHARS_PER_FILE]
            except Exception as e:
                logger.debug("coder_companion: vfs_reader failed for %s — %s", filepath, e)
    except Exception as e:
        logger.debug("coder_companion: make_safe_reader failed — %s", e)

    return CompanionAnalysisRequest(
        session_id=session_id,
        task_id=task_id,
        attempt_ordinal=attempt_ordinal,
        task_description=task_description,
        pending_patches={k: v for k, v in list(pending_patches.items())[:_MAX_FILES_IN_PAYLOAD]},
        pending_contents={k: v for k, v in list(pending_contents.items())[:_MAX_FILES_IN_PAYLOAD]},
        file_context=file_context,
        relevant_errors=relevant_errors,
        security_flags=security_flags,
        verbosity=verbosity,
    )


def _build_companion_system_prompt(verbosity: Literal["minimal", "normal", "deep"]) -> str:
    """System prompt with verbosity-gated field expansion."""
    base = (
        "You are an expert code reviewer analyzing a patch (SEARCH/REPLACE edits) "
        "the Coder just produced. Your job is to explain the patch in structured terms: "
        "what it achieves, why those decisions were made, what patterns apply, "
        "where bottlenecks or edge cases exist, and what follow-up work remains. "
        "\n\n"
        "RULES:\n"
        "- Explain, do not judge. Do not suggest alternative solutions or improvements.\n"
        "- Output ONLY a JSON object. No preamble, no markdown, no trailing text.\n"
        "- Be concise. Each list item ≤25 words, full sentences.\n"
        "- Populate `security_notes` if security_flags are non-empty; keep it separate from errors_found.\n"
    )

    if verbosity == "deep":
        base += (
            "- For a complex patch: populate all fields (decisions, patterns_applied, bottlenecks, "
            "errors_found, follow_ups).\n"
        )
    elif verbosity == "minimal":
        base += (
            "- For a simple patch: populate only `objective` and `security_notes` (if present); "
            "leave other lists empty.\n"
        )
    else:  # normal
        base += (
            "- For a moderate patch: populate `objective`, `decisions`, and `security_notes` (if present); "
            "keep other lists short.\n"
        )

    return base


def _build_companion_user_payload(request: CompanionAnalysisRequest) -> str:
    """Assemble user message with token-hygiene truncation."""
    lines = [
        f"Task: {request.task_description}",
        f"Verbosity: {request.verbosity}",
        "",
        "FILES CHANGED:",
    ]

    for i, (filepath, diff) in enumerate(request.pending_patches.items()):
        if i >= _MAX_FILES_IN_PAYLOAD:
            lines.append(f"... +{len(request.pending_patches) - i} more files, truncated")
            break
        lines.append(f"\n### {filepath}")
        truncated_diff = diff[:_MAX_DIFF_CHARS_PER_FILE]
        if len(diff) > _MAX_DIFF_CHARS_PER_FILE:
            truncated_diff += f"\n... truncated ({len(diff)} chars total)"
        lines.append(truncated_diff)

    if request.file_context:
        lines.append("\nPRE-PATCH CONTEXT (samples):")
        for filepath, context in request.file_context.items():
            lines.append(f"\n### {filepath}")
            lines.append(context[:_MAX_DIFF_CHARS_PER_FILE])

    if request.relevant_errors:
        lines.append("\nERRORS DURING EXECUTION:")
        for err in request.relevant_errors:
            lines.append(f"- {err}")

    if request.security_flags:
        lines.append("\nSECURITY FLAGS:")
        for flag in request.security_flags:
            lines.append(f"- {flag}")

    return "\n".join(lines)


async def _call_analyst_llm(request: CompanionAnalysisRequest) -> CompanionAnalysis:
    """Best-effort LLM call. Never raises — returns degraded=True on any failure."""
    from core.response_cache import response_cache  # deferred — avoid circular import

    system_prompt = _build_companion_system_prompt(request.verbosity)
    user_payload = _build_companion_user_payload(request)

    cache_key = response_cache.build_key(
        intent=f"coder_companion:{request.task_id}:{request.attempt_ordinal}",
        context=[(f, request.pending_patches[f]) for f in sorted(request.pending_patches)],
        project_id=request.session_id,
        model=MINI_JUDGE_MODEL,
    )
    cached = response_cache.probe(cache_key)
    if cached is not None:
        return _parse_companion_json(cached)

    # Resolve whether the judge alias points at a local target BEFORE picking the
    # deadline: a local model routinely needs tens of seconds, while a cloud model's
    # round-trip is comparable to the old flat 12s. Best-effort — an unresolved alias
    # (no BYOM preset active yet) falls back to the cloud deadline, matching prior
    # behavior exactly.
    llm_timeout = _COMPANION_LLM_TIMEOUT_CLOUD_S
    if MINI_JUDGE_MODEL.startswith("ailienant/"):
        try:
            from core.config.model_resolver import get_chat_target
            _alias_tier = MINI_JUDGE_MODEL.split("/", 1)[1]
            _target = get_chat_target(_alias_tier if _alias_tier in ("small", "medium", "big") else "medium")
            if _target is not None and _target.is_local:
                llm_timeout = _COMPANION_LLM_TIMEOUT_LOCAL_S
        except Exception:  # noqa: BLE001 — resolution is advisory; keep the cloud default on any fault
            logger.debug("coder_companion: judge-tier resolution failed; using cloud deadline", exc_info=True)

    try:
        response = await asyncio.wait_for(
            LLMGateway.ainvoke(
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_payload},
                ],
                model=MINI_JUDGE_MODEL,
                temperature=0.0,
                response_format={"type": "json_object"},
                max_tokens=_MAX_TOKENS_BY_VERBOSITY[request.verbosity],
                session_id=request.session_id,
            ),
            timeout=llm_timeout,
        )
        if response and response.choices:
            raw_content = response.choices[0].message.content
        else:
            return CompanionAnalysis(objective="Explanation unavailable.", degraded=True)
    except Exception as exc:  # noqa: BLE001 — best-effort explainer; timeout/provider/network fault
        logger.debug("coder_companion: LLM call failed — %s", exc, exc_info=True)
        return CompanionAnalysis(objective="Explanation unavailable.", degraded=True)

    if raw_content:
        response_cache.store(cache_key, raw_content, paths=list(request.pending_patches.keys()))
    return _parse_companion_json(raw_content or "")


def _parse_companion_json(raw_content: str) -> CompanionAnalysis:
    """Parse LLM output, degrading gracefully on malformed or semantically-empty output."""
    try:
        data = LLMGateway._extract_nested_schema_target(raw_content, CompanionAnalysis)
        analysis = CompanionAnalysis.model_validate(data)
    except (ValidationError, ValueError, KeyError) as exc:
        logger.debug("coder_companion: malformed LLM output — %s", exc, exc_info=True)
        return CompanionAnalysis(objective="Explanation unavailable.", degraded=True)

    if not analysis.objective.strip():
        return CompanionAnalysis(objective="Explanation unavailable.", degraded=True)
    return analysis


def schedule_coder_companion(state: Dict[str, Any], attempt_ordinal: int) -> None:
    """Fire-and-forget entry point. Synchronous, never awaited. Called from run_coder_node."""
    task = asyncio.create_task(_run_coder_companion(state, attempt_ordinal))
    _companion_background_tasks.add(task)
    task.add_done_callback(_companion_background_tasks.discard)


async def _run_coder_companion(state: Dict[str, Any], attempt_ordinal: int) -> None:
    """Background task body. Must never raise into the event loop — a fault here must
    not affect the coder's checkpoint or the graph's control flow.

    Every exit path (budget-skip, VRAM-skip, LLM success/degrade, or an unexpected
    fault) converges on exactly ONE broadcast below, so the frontend card always
    receives a terminal signal instead of waiting on a producer that silently never
    emits — it can wait for this event instead of racing an independent local clock.
    """
    session_id = state.get("task_id", "")
    task_id = state.get("task_id", "")
    analysis: CompanionAnalysis

    try:
        if not _companion_budget_available(state):
            logger.debug("coder_companion: skipped — over budget ceiling")
            analysis = CompanionAnalysis(objective="Explanation unavailable.", degraded=True)
        else:
            async with _companion_semaphore:  # bounds SWARM fan-out bursts
                if not await _companion_gpu_slot_available():
                    logger.debug("coder_companion: skipped — local tier VRAM lock busy")
                    analysis = CompanionAnalysis(objective="Explanation unavailable.", degraded=True)
                else:
                    request = _build_companion_request(state, attempt_ordinal)
                    analysis = await _call_analyst_llm(request)
    except Exception as exc:  # noqa: BLE001 — outer safety net: a best-effort explainer
        # must never propagate a fault, from anywhere in this pipeline, to its caller.
        logger.warning("coder_companion: pipeline failed for task=%s — %s", task_id, exc, exc_info=True)
        analysis = CompanionAnalysis(objective="Explanation unavailable.", degraded=True)

    try:
        # Import here to avoid circular dependency.
        from api.websocket_manager import vfs_manager

        await vfs_manager.broadcast_coder_companion(
            session_id=session_id,
            task_id=task_id,
            correlation_id=f"{task_id}:{attempt_ordinal}",
            analysis=analysis,
        )
    except Exception as exc:  # noqa: BLE001 — a broadcast fault must not crash this
        # fire-and-forget background task; the card simply falls back to its own
        # client-side safety-net timeout in that (rare) case.
        logger.debug("coder_companion: broadcast failed — %s", exc, exc_info=True)
