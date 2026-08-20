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
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Set, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from shared.config import MINI_JUDGE_MODEL
from tools.llm_gateway import LLMGateway
from core.vfs_middleware import make_safe_reader

logger = logging.getLogger("CODER_COMPANION")

CompanionScope = Literal["coding", "ideation", "planning", "healing"]

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
# Token-hygiene cap for a non-coding scope's pre-rendered summary (§5.5) —
# mirrors _MAX_DIFF_CHARS_PER_FILE's role for the coding path. Each scope
# builder truncates to this BEFORE the summary ever reaches this dataclass, so
# a single request can never balloon regardless of how much state is behind it.
_MAX_SCOPE_SUMMARY_CHARS = 4000
# Defense-in-depth backstop on total companion emissions per task, shared across
# every scope. Each real decision point is already topology-bounded on its own
# (_GRILL_MAX_ROUNDS, CORRECTION_MAX_ATTEMPTS, one planning pass), so this
# ceiling is deliberately generous — it exists to catch a cyclic-graph bug, not
# to shape normal use. Never explicitly cleared: one small int per task_id for
# the process lifetime, the same bounded-growth tradeoff ACTIVITY_CAP accepts.
_MAX_COMPANION_EMISSIONS_PER_TASK = 20
_companion_emission_counts: Dict[str, int] = {}


def _companion_emission_admitted(task_id: str) -> bool:
    """True and reserves a slot if this task is still under its emission
    ceiling; False (no slot reserved) once it's been reached."""
    count = _companion_emission_counts.get(task_id, 0)
    if count >= _MAX_COMPANION_EMISSIONS_PER_TASK:
        return False
    _companion_emission_counts[task_id] = count + 1
    return True


@dataclass(frozen=True)
class CompanionAnalysisRequest:
    """Frozen request signature for caching and passing to LLM.

    ``scope``/``scope_summary`` (13.0.7) generalize this beyond the coding path:
    a non-coding scope builder pre-renders its own decision-point slice — never
    the full accumulated turn state — into ``scope_summary``, already truncated
    to ``_MAX_SCOPE_SUMMARY_CHARS``, and leaves the coding-only fields at their
    empty defaults. The coding scope keeps using those fields exactly as before;
    ``scope_summary`` stays empty for it.
    """
    session_id: str
    task_id: str
    attempt_ordinal: int
    task_description: str
    verbosity: Literal["minimal", "normal", "deep"]
    scope: CompanionScope = "coding"
    scope_summary: str = ""
    pending_patches: Dict[str, str] = field(default_factory=dict)
    pending_contents: Dict[str, str] = field(default_factory=dict)
    file_context: Dict[str, str] = field(default_factory=dict)
    relevant_errors: List[str] = field(default_factory=list)
    security_flags: List[str] = field(default_factory=list)


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


async def _companion_gpu_slot_available(session_id: str) -> bool:
    """Non-blocking VRAM-lock probe. Never queue the Companion behind the user's real work.

    A cloud-tier judge model never contends for local VRAM, so it is always admitted.
    A local-tier model must yield instantly rather than wait on the GPU lock the user's
    coding turn holds — an explanation nobody may read must never delay the real task.

    Reads GPUResourceManager.snapshot() (an atomic, non-blocking read under the
    manager's own mutex) rather than try_acquire_now/acquire_lock — the companion
    must observe the lock, never take it; taking it would be the exact contention
    this probe exists to prevent.
    """
    try:
        from core.config.model_resolver import get_chat_target
        from core.resource_manager import GPUResourceManager

        target = get_chat_target(_resolve_judge_tier())
        if target is None or not target.is_local:
            return True  # cloud-tier judge never contends for local VRAM

        mgr = await GPUResourceManager.get()
        snapshot = await mgr.snapshot()
        holder = snapshot.get("locked_by_session_id")
        return holder is None or holder == session_id
    except Exception as exc:  # noqa: BLE001 — fail-open: a probe fault must never
        # suppress the card, but a local-tier VRAM probe failing silently could mean
        # this task has been contending for VRAM with the user's real coding turn for
        # some time without anyone noticing — unlike the sibling timeout-resolution
        # helper (whose fault is benign, a shorter deadline), this warrants WARNING,
        # not debug. Bounded to once per coding turn, so this cannot become log spam.
        logger.warning(
            "coder_companion: GPU-slot probe failed — admitting unconditionally: %s",
            exc, exc_info=True,
        )
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


def build_ideation_companion_request(
    session_id: str,
    task_id: str,
    attempt_ordinal: int,
    task_description: str,
    question_batch: List[Dict[str, Any]],
    resolved_answers: Dict[str, Any],
) -> CompanionAnalysisRequest:
    """Companion request for one grill round closing (agents/analyst.py's ask
    phase). Consumes ONLY that round's own batch + answers — both already
    round-local, never the accumulated `tool_dispatch_trace` — so this never
    grows across rounds the way re-sending an append-only state list would.
    """
    lines: List[str] = []
    for q in question_batch:
        qid = q.get("id", "")
        header = q.get("header", "")
        question = q.get("question", "")
        answer = resolved_answers.get(qid)
        lines.append(f"- [{header}] {question}\n  Answer: {answer if answer else '(no answer)'}")
    summary = "\n".join(lines)[:_MAX_SCOPE_SUMMARY_CHARS]

    return CompanionAnalysisRequest(
        session_id=session_id,
        task_id=task_id,
        attempt_ordinal=attempt_ordinal,
        task_description=task_description,
        verbosity="minimal",
        scope="ideation",
        scope_summary=summary,
    )


def build_planning_companion_request(
    session_id: str,
    task_id: str,
    mission_plan: Any,
) -> CompanionAnalysisRequest:
    """Companion request for the plan committing (agents/planner.py, after the
    validated MissionSpecification is assembled). Consumes only the just-drafted
    plan's own summary fields, never prior turns' plans."""
    task_lines = [
        f"- step {getattr(t, 'step_number', '?')}: {getattr(t, 'action', '')} "
        f"{getattr(t, 'target_file', '')} — {getattr(t, 'description', '')}"
        for t in list(getattr(mission_plan, "tasks", []))[:_MAX_FILES_IN_PAYLOAD]
    ]
    summary = "\n".join([
        f"Outcome: {getattr(mission_plan, 'outcome', '')}",
        f"Constraints: {'; '.join(list(getattr(mission_plan, 'constraints', []))[:8])}",
        "Tasks:",
        *task_lines,
    ])[:_MAX_SCOPE_SUMMARY_CHARS]

    return CompanionAnalysisRequest(
        session_id=session_id,
        task_id=task_id,
        attempt_ordinal=0,
        task_description=str(getattr(mission_plan, "outcome", "")),
        verbosity="normal",
        scope="planning",
        scope_summary=summary,
    )


def build_healing_companion_request(
    session_id: str,
    task_id: str,
    attempt_ordinal: int,
    failed_node: str,
    diagnosis: str,
    healed: bool,
) -> CompanionAnalysisRequest:
    """Companion request for error correction resolving (agents/error_correction.py),
    whether it healed the fault or conceded. Consumes only this attempt's own
    diagnosis text, never the accumulated error history."""
    summary = (
        f"Failed node: {failed_node}\n"
        f"Outcome: {'healed' if healed else 'could not auto-fix'}\n"
        f"Diagnosis: {diagnosis}"
    )[:_MAX_SCOPE_SUMMARY_CHARS]

    return CompanionAnalysisRequest(
        session_id=session_id,
        task_id=task_id,
        attempt_ordinal=attempt_ordinal,
        task_description=f"Self-healing {failed_node}",
        verbosity="minimal",
        scope="healing",
        scope_summary=summary,
    )


def _build_companion_system_prompt(
    verbosity: Literal["minimal", "normal", "deep"],
    scope: CompanionScope = "coding",
) -> str:
    """System prompt with verbosity-gated field expansion."""
    if scope != "coding":
        _scope_noun = {
            "ideation": "a round of clarifying questions the Analyst just asked and the operator just answered",
            "planning": "the technical plan the Planner just committed",
            "healing": "a self-healing correction attempt the system just resolved",
        }[scope]
        return (
            f"You are explaining, to the developer watching, {_scope_noun}. "
            "Your job is to explain WHY, in structured terms: what it achieves, "
            "what decisions were made, and what — if anything — the developer "
            "should follow up on.\n\n"
            "RULES:\n"
            "- Explain, do not judge. Do not suggest alternative approaches.\n"
            "- Output ONLY a JSON object. No preamble, no markdown, no trailing text.\n"
            "- Be concise. Each list item ≤25 words, full sentences.\n"
            "- Populate `objective` always; populate `follow_ups` only if something "
            "genuinely needs the developer's attention. Leave other lists empty — "
            "they describe a code patch, which this decision point has none of.\n"
        )
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
    if request.scope != "coding":
        # scope_summary is already truncated to _MAX_SCOPE_SUMMARY_CHARS by the
        # scope builder that produced it — nothing further to cap here.
        return (
            f"Task: {request.task_description}\n"
            f"Verbosity: {request.verbosity}\n\n"
            f"{request.scope_summary}"
        )

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


def _resolve_judge_tier() -> str:
    """The BYOM tier MINI_JUDGE_MODEL resolves to (small/medium/big).

    Defaults to "medium" for a non-``ailienant/`` alias or an unrecognized tier
    suffix — matches the fallback ``_call_analyst_llm`` always used before this
    was extracted into a shared helper.
    """
    if MINI_JUDGE_MODEL.startswith("ailienant/"):
        _alias_tier = MINI_JUDGE_MODEL.split("/", 1)[1]
        if _alias_tier in ("small", "medium", "big"):
            return _alias_tier
    return "medium"


def _resolve_companion_llm_timeout(tier: str) -> float:
    """Tier-aware deadline shared by the structured analysis and narration calls.

    A local judge target needs far more than a cloud round-trip's budget (the
    gateway itself grants local calls up to 300s, see
    llm_gateway._LOCAL_LLM_TIMEOUT_S) — 12s reliably starves a local completion
    before it can finish. Best-effort: an unresolved alias (no BYOM preset active
    yet) falls back to the cloud deadline.
    """
    try:
        from core.config.model_resolver import get_chat_target
        _target = get_chat_target(tier)
        if _target is not None and _target.is_local:
            return _COMPANION_LLM_TIMEOUT_LOCAL_S
    except Exception:  # noqa: BLE001 — resolution is advisory; keep the cloud default on any fault
        logger.debug("coder_companion: judge-tier resolution failed; using cloud deadline", exc_info=True)
    return _COMPANION_LLM_TIMEOUT_CLOUD_S


async def _call_analyst_llm(request: CompanionAnalysisRequest) -> CompanionAnalysis:
    """Best-effort LLM call. Never raises — returns degraded=True on any failure."""
    from core.response_cache import response_cache  # deferred — avoid circular import

    system_prompt = _build_companion_system_prompt(request.verbosity, request.scope)
    user_payload = _build_companion_user_payload(request)

    # scope-namespaced so a planning/ideation/healing cache key never collides
    # with the coding path's (or each other's) — pending_patches is empty for
    # every non-coding scope, so scope_summary stands in as its context slice.
    context: List[tuple[str, str]] = (
        [(f, request.pending_patches[f]) for f in sorted(request.pending_patches)]
        if request.scope == "coding"
        else [(str(request.scope), request.scope_summary)]
    )
    cache_key = response_cache.build_key(
        intent=f"coder_companion:{request.scope}:{request.task_id}:{request.attempt_ordinal}",
        context=context,
        project_id=request.session_id,
        model=MINI_JUDGE_MODEL,
    )
    cached = response_cache.probe(cache_key)
    if cached is not None:
        return _parse_companion_json(cached)

    llm_timeout = _resolve_companion_llm_timeout(_resolve_judge_tier())

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
    if not _companion_emission_admitted(task_id):
        logger.info("coder_companion: emission cap reached for task=%s — skipping", task_id)
        return
    analysis: CompanionAnalysis

    try:
        if not _companion_budget_available(state):
            logger.debug("coder_companion: skipped — over budget ceiling")
            analysis = CompanionAnalysis(objective="Explanation unavailable.", degraded=True)
        else:
            async with _companion_semaphore:  # bounds SWARM fan-out bursts
                if not await _companion_gpu_slot_available(session_id):
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
            emission_id=f"{task_id}:coding:{attempt_ordinal}",
            scope="coding",
            analysis=analysis,
        )
    except Exception as exc:  # noqa: BLE001 — a broadcast fault must not crash this
        # fire-and-forget background task; the card simply falls back to its own
        # client-side safety-net timeout in that (rare) case.
        logger.debug("coder_companion: broadcast failed — %s", exc, exc_info=True)


def schedule_agent_companion(
    state: Dict[str, Any],
    scope: CompanionScope,
    attempt_ordinal: int,
    build_request: Callable[[], CompanionAnalysisRequest],
) -> None:
    """Fire-and-forget entry point for a non-coding decision point (a grill round
    closing, a plan committing, error correction resolving). Mirrors
    `schedule_coder_companion`'s lifecycle and governance exactly — same
    background-task strong-ref set, same budget/VRAM/semaphore admission — but
    takes a zero-arg request builder instead of building one from `state` itself:
    each scope's request shape differs too much from the coding path's
    patch-shaped one to share a single builder.
    """
    task = asyncio.create_task(_run_agent_companion(state, scope, attempt_ordinal, build_request))
    _companion_background_tasks.add(task)
    task.add_done_callback(_companion_background_tasks.discard)


async def _run_agent_companion(
    state: Dict[str, Any],
    scope: CompanionScope,
    attempt_ordinal: int,
    build_request: Callable[[], CompanionAnalysisRequest],
) -> None:
    """Background task body for a non-coding companion emission. Same
    never-raise contract and broadcast-on-every-exit-path guarantee as
    `_run_coder_companion` — see that function's docstring for the rationale.
    """
    session_id = state.get("task_id", "")
    task_id = state.get("task_id", "")
    emission_id = f"{task_id}:{scope}:{attempt_ordinal}"
    if not _companion_emission_admitted(task_id):
        logger.info("coder_companion: emission cap reached for task=%s — skipping (%s)", task_id, scope)
        return
    analysis: CompanionAnalysis

    try:
        if not _companion_budget_available(state):
            logger.debug("coder_companion: skipped (%s) — over budget ceiling", scope)
            analysis = CompanionAnalysis(objective="Explanation unavailable.", degraded=True)
        else:
            async with _companion_semaphore:  # bounds SWARM fan-out bursts
                if not await _companion_gpu_slot_available(session_id):
                    logger.debug("coder_companion: skipped (%s) — local tier VRAM lock busy", scope)
                    analysis = CompanionAnalysis(objective="Explanation unavailable.", degraded=True)
                else:
                    request = build_request()
                    analysis = await _call_analyst_llm(request)
    except Exception as exc:  # noqa: BLE001 — outer safety net: a best-effort explainer
        # must never propagate a fault, from anywhere in this pipeline, to its caller.
        logger.warning(
            "coder_companion: pipeline failed for task=%s scope=%s — %s", task_id, scope, exc, exc_info=True,
        )
        analysis = CompanionAnalysis(objective="Explanation unavailable.", degraded=True)

    try:
        # Import here to avoid circular dependency.
        from api.websocket_manager import vfs_manager

        await vfs_manager.broadcast_coder_companion(
            session_id=session_id,
            task_id=task_id,
            correlation_id=f"{task_id}:{attempt_ordinal}",
            emission_id=emission_id,
            scope=scope,
            analysis=analysis,
        )
    except Exception as exc:  # noqa: BLE001 — a broadcast fault must not crash this
        # fire-and-forget background task; the card simply falls back to its own
        # client-side safety-net timeout in that (rare) case.
        logger.debug("coder_companion: broadcast failed — %s", exc, exc_info=True)
