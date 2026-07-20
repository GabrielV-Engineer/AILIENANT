# alienant-core/agents/planner.py

import json
import logging
import os
import uuid
from typing import Optional, Any, cast

from langchain_core.runnables import RunnableConfig

# We import our gateway and strict contracts
from tools.llm_gateway import LLMGateway
from shared.config import MODEL_MEDIUM, MODEL_BIG  # noqa: F401 — MEDIUM retained for backward refs
from brain.state import MissionSpecification, WBSStep, ContextMeter
from shared.rbac import PLANNER_IDENTITY
from agents.prompts import build_safe_prompt
from brain.agent_context import (
    AMNESIA_ALERT,
    build_agent_context,
    resolve_context_budget,
)
from brain.context_pipeline import ContextBudgetError
from agents.workspace_context import build_workspace_overview
from core.utils import is_polyglot_file
from core.rules import rule_manager
from core.project_instructions import get_project_instructions
from core.memory.trajectory_memory import TrajectoryMemoryManager, format_trajectories_for_prompt
from core.resource_manager import ResourceBroker
from brain.retry_policy import PLANNER_MAX_RETRIES
from tools.planner_tools import BudgetEstimatorTool, ValidateWBSDependenciesTool

# Bounded planner retry budget on Pydantic ValidationError. Distinct from the
# MICRO_SWARM Coder's MAX_RETRIES (different agent, different gate); both budgets
# are sourced from the central retry policy.
MAX_PLANNER_RETRIES: int = PLANNER_MAX_RETRIES

# Scope discipline injected into the planner instruction. Without it the model
# treats every file it sees in the injected context as a backlog to edit and
# sprawls into unrelated documents.
_SCOPE_DISCIPLINE_DIRECTIVE: str = (
    "SCOPE DISCIPLINE (MANDATORY):\n"
    "- Propose changes ONLY to files the user explicitly named, or that are strictly "
    "necessary to fulfill the literal request. If the request names a single new file, "
    "the WBS must touch ONLY that file.\n"
    "- The injected IDE/workspace/deep context is READ-ONLY reference to help you "
    "understand the project. It is NOT a list of files to modify. Seeing a file in "
    "context is NEVER a reason to edit it.\n"
    "- Do NOT invent documentation updates, refactors, READMEs, tests, or edits to any "
    "file the user did not ask about. Stay minimal: the smallest WBS that satisfies the "
    "request is the correct one.\n\n"
)

_WBS_SEED_DIRECTIVE: str = (
    "EXISTING PLAN AS SEED:\n"
    "- If the user requirement is ALREADY an enumerated or bulleted task list (a plan "
    "the user wrote out), treat that list as the WBS seed: map each item to a sequential "
    "task in the given order, preserving the user's intent and wording.\n"
    "- You MAY refine it — merge redundant items, split a step that needs several actions, "
    "reorder for correct dependencies, or add a missing prerequisite — but honor the "
    "user's structure; do NOT discard their list to invent an unrelated one.\n\n"
)

# Configuración del logger para este nodo específico
logger = logging.getLogger("PLANNER_NODE")

# promoted to module-level so tests can patch it.
# default OFF — the real LLM path now runs (BYOM-aware ainvoke).
# Set AILIENANT_PLANNER_DEBUG=1 to force the synthetic stub (CI/UI smoke tests).
DEBUG_MODE: bool = os.getenv("AILIENANT_PLANNER_DEBUG", "0") != "0"

_POLYGLOT_WARNING = (
    " [!] POLYGLOT FILE DETECTED: {target_file}. "
    "You MUST use the 'patch_file' tool for any modifications. "
    "Full file rewrites are strictly forbidden to prevent corrupting mixed syntax."
)


def _inject_polyglot_constraints(tasks: list[WBSStep]) -> list[WBSStep]:
    """Return a new task list with polyglot-file constraints appended to step descriptions."""
    result = []
    for step in tasks:
        if step.target_file and is_polyglot_file(step.target_file):
            step = step.model_copy(update={
                "description": step.description
                    + _POLYGLOT_WARNING.format(target_file=step.target_file)
            })
        result.append(step)
    return result


async def run_planner_node(
    state: dict[str, Any], config: Optional[RunnableConfig] = None
) -> dict[str, Any]:
    """
    LangGraph Node: The Strategist (The Architect & SDD Enforcer).

    Mission::
        Analyzes the user's requirements and the IDE (VFS) context to
        generate a strict Macro-Contract (MissionSpecification). Does not execute code.

    Args:
        state (dict): The current global state (AIlienantGraphState).

    Returns:
        dict: A dictionary with a partial update of the status.
              Specifically, it updates the 'mission_spec' key and optionally 'errors'.
    """
    logger.info("🧠 PlannerAgent iniciando análisis arquitectónico de la misión...")
    # Prefer task_id (AIlienantGraphState) then session_id (loose dict); fall back to uuid4.
    session_id: str = (
        state.get("task_id") or state.get("session_id") or str(uuid.uuid4())
    )

    # granular sub-step narration. task_service injects an async emitter on
    # config.configurable["narrate"] (kept off graph state so the checkpointer
    # never tries to serialize a callable); the planner stays decoupled from the
    # transport layer (never imports vfs_manager) — cognitive-isolation fence intact.
    _narrate = (config or {}).get("configurable", {}).get("narrate")
    # Reasoning sink (Thought Box) + native-thinking prefs, same off-state seam.
    _on_thinking = (config or {}).get("configurable", {}).get("stream_thinking")
    _thinking_on = bool((config or {}).get("configurable", {}).get("enable_native_thinking"))
    _thinking_budget = int((config or {}).get("configurable", {}).get("thinking_budget_tokens") or 4096)
    # Retrieval seams are injectable so a benchmark can degrade them explicitly;
    # production omits these keys and the real bound methods below run unchanged.
    _configurable = (config or {}).get("configurable", {})
    _planner_retrieval_fn = _configurable.get("planner_retrieval_fn")
    _graph_fn = _configurable.get("graph_fn")

    async def _emit(node_name: str) -> None:
        if _narrate is not None:
            await _narrate(node_name)

    # =====================================================================
    # 0. SIMULATION MODE (Short Circuit for UI/Backend Testing)
    # =====================================================================
    # DEBUG_MODE is now a module-level constant (see top of file). Set env var
    # AILIENANT_PLANNER_DEBUG=0 in production to enable the full LLM path.

    # We read TCI and CSS from the state. We prefer top-level shortcuts;
    # If they are not present yet, we navigate to context_metrics as a safe fallback..
    tci: float = state.get("tci", 0.0)
    css: float = state.get("css", 100.0)
    metrics = state.get("context_metrics")
    if metrics is not None and tci == 0.0:
        tci = getattr(metrics, "task_complexity_index", 0.0)
    if metrics is not None and css == 100.0:
        css = getattr(metrics, "css_total", 100.0)

    # replaced by semantic-guided deep parse (production path below).
    updated_context_metrics = metrics  # default: pass through unchanged

    if DEBUG_MODE:
        logger.warning(
            "⚠️ MODO DEBUG ACTIVO: Generando contrato SDD sintético (Bypass de LLM). TCI=%.1f CSS=%.1f",
            tci,
            css,
        )

        # For High-TCI (>80), we generated two independent tasks to exercise the fan-out.
        if tci > 80.0:
            tasks = [
                WBSStep(
                    step_number=1,
                    target_role="architect_refactor",
                    action="read_file",
                    target_file="main.py",
                    description="Paso paralelo A: leer archivo principal.",
                    status="pending",
                ),
                WBSStep(
                    step_number=2,
                    target_role="qa_tester",
                    action="read_file",
                    target_file="requirements.txt",
                    description="Paso paralelo B: auditar dependencias.",
                    status="pending",
                ),
            ]
            logger.info("High-TCI detected: %d parallel tasks generated.", len(tasks))
        else:
            tasks = [
                WBSStep(
                    step_number=1,
                    target_role="architect_refactor",
                    action="read_file",
                    target_file="main.py",
                    description="Read main file to validate IDE connection.",
                    status="pending",
                )
            ]

        tasks = _inject_polyglot_constraints(tasks) 

        mock_mission = MissionSpecification(
            outcome="Initial analysis completed in a synthetic form.",
            scope=["main.py"],
            constraints=["Without external dependencies."],
            decisions=["Use DEBUG mode to validate graph routing."],
            tasks=tasks,
            checks=["The file was read without throwing any exceptions."],
        )

        # WBS steps carry only implicit step_number ordering (no dependency graph),
        # so parallel fan-out would run dependent steps out of order. Execute
        # sequentially via RELAY; SWARM dispatch stays dormant until an explicit
        # dependency DAG exists.
        parallel_tasks: list[Any] = []
        result: dict[str, Any] = {
            "mission_spec": mock_mission,
            "parallel_tasks": parallel_tasks,
            "tci": tci,
            "css": css,
            "context_metrics": updated_context_metrics,
        }
        # Freeze the baseline on the first turn (immutable_wbs absent or None).
        # DriftMonitor will compare future re-plans against this anchor.
        if state.get("immutable_wbs") is None:
            result["immutable_wbs"] = mock_mission
            logger.info("PlannerAgent: immutable_wbs frozen (first turn, DEBUG mode).")
        return result

    # =====================================================================
    # 1. CONTEXT EXTRACTION (User Input and IDE)
    # =====================================================================
    user_input = state.get("user_input", "")
    # We safely extract the buffers (it may come as a dict or object depending on the serializer)
    ide_context = state.get("ide_context", {})

    # Compatibility: If ide_context is a Pydantic model, we use .dict(); if it is already a dict, we use it directly.
    dirty_buffers = (
        ide_context.get("dirty_buffers", [])
        if isinstance(ide_context, dict)
        else getattr(ide_context, "dirty_buffers", [])
    )

    # =====================================================================
    # 2. SECURITY BLOCK: INVISIBLE SANDBOXING (Defense against Prompt Injection)
    # =====================================================================
    # We generate an ephemeral cryptographic lock to isolate the user code from the system instructions.
    boundary = uuid.uuid4().hex

    context_str = ""

    # the ACTIVE FILE the user is looking at, injected FIRST
    # and prominently labeled so the Planner anchors on the open tab instead of
    # hallucinating from the stale LanceDB/GraphRAG index. The active tab may be
    # SAVED (so absent from dirty_buffers); content is hard-capped client-side.
    _active_path = state.get("active_file_path", "")
    _active_content = state.get("active_file_content", "")
    if _active_content:
        context_str += (
            f'<{boundary} kind="active_file" path="{_active_path}">\n'
            f"=== ACTIVE FILE (user is viewing this now): {_active_path} ===\n"
            f"{_active_content}\n</{boundary}>\n\n"
        )

    if not dirty_buffers and not _active_content:
        context_str = f"<{boundary}>No se detectaron archivos sucios ni contexto activo en el IDE.</{boundary}>"
    elif dirty_buffers:
        for buf in dirty_buffers:
            # Compatibilidad dict vs Pydantic object
            filepath = buf.get("path") if isinstance(buf, dict) else buf.path
            content = buf.get("content") if isinstance(buf, dict) else buf.content

            context_str += (
                f'<{boundary} filepath="{filepath}">\n{content}\n</{boundary}>\n\n'
            )

    # inject workspace SHAPE (depth-limited tree +
    # root manifests) so the Planner is no longer blind to project structure.
    # Wrapped in the same ephemeral boundary as raw data (never instructions).
    _ws_overview = build_workspace_overview(state.get("workspace_root", ""))
    if _ws_overview:
        context_str += (
            f'<{boundary} kind="workspace_overview">\n{_ws_overview}\n</{boundary}>\n\n'
        )

    # =====================================================================
    # 3. PROMPT CONSTRUCTION (RBAC and Spec-Driven Development)
    # =====================================================================
    # The durable instruction context (identity, rules, project instructions,
    # skills, memory) and the volatile IDE context are mapped onto the five-layer
    # ContextPipeline below. Identity carries the cognitive-quarantine framing but
    # NOT the volatile file content — that is routed to the Execution layer (L5) so
    # it is trimmed first under budget pressure, while identity/rules/memory (L1-L3)
    # are never silently dropped. The SAME `boundary` nonce threads through both the
    # L1 framing and the L5 chunk so the prompt-injection seal stays end-to-end.
    _l1_identity = build_safe_prompt(
        agent_identity=PLANNER_IDENTITY,
        context_str=(
            f"<{boundary}>IDE context is provided in the user turn below, under this "
            f"same secure boundary.</{boundary}>"
        ),
        boundary=boundary,
    )

    _rules = rule_manager.get_combined_rules(state.get("workspace_root", ""))

    # Freeform project instructions (AILIENANT.md) — standing prose guidance that
    # complements the machine-checkable .ailienant.json rules above.
    _project_instructions = get_project_instructions(
        state.get("project_id") or "",
        state.get("workspace_root", ""),
        state.get("task_id", ""),
    )

    # ── User Skill Injection ───────────────────────────────
    # Skills the user saved and either explicitly invoked or that matched this task
    # semantically. Resolved upstream and threaded on the loose state dict; wrapped in
    # the same ephemeral boundary as other injected directives.
    _skill_block = ""
    _skills = state.get("active_skills") or []
    if _skills:
        from core.skill_resolver import build_skill_directive_block

        _skill_block = build_skill_directive_block(_skills, boundary) or ""

    # ── Trajectory Memory Injection ────────────────────────
    _traj_mgr = TrajectoryMemoryManager()
    _past_trajectories = await _traj_mgr.search(
        user_input=user_input,
        project_id=state.get("project_id") or "",
    )
    _trajectory_block = ""
    if _past_trajectories:
        _trajectory_block = format_trajectories_for_prompt(_past_trajectories)
        logger.info(
            "TrajectoryMemory: injected %d past trajectories into planner context.",
            len(_past_trajectories),
        )
    # ────────────────────────────────────────────────────────────────────

    # ── Routing signal (produced upstream by the Researcher node) ──
    # The Researcher owns all retrieval + the Context Meter Cascade + hardware reroute
    # and emits context_metrics / css / tci / provider / routing_warning on state. The
    # Planner is a pure analytical engine that consumes the resolved signal. Keep a
    # defensive fallback so a Researcher bypass/error can never propagate a None metric
    # into the routing consumers or the cache key below.
    if updated_context_metrics is None:
        updated_context_metrics = ContextMeter(
            semantic_similarity=0.0,
            graph_coverage=0.0,
            recency_score=0.0,
            css_total=css,
            task_complexity_index=tci,
            routing_decision="LOCAL_SMALL",
            is_red_alert=css < 40.0,
        )

    # consume the Researcher's Skeleton Map when available.
    # Injected as a sandboxed block so the LLM treats it as inert data (matches the
    # XML-boundary discipline used for dirty buffers throughout this module).
    researcher_skeleton: str = state.get("researcher_skeleton") or ""
    skeleton_block: str = ""
    if researcher_skeleton:
        skeleton_block = (
            f"\n\n<{boundary} role=\"researcher_skeleton\">\n"
            f"{researcher_skeleton}\n"
            f"</{boundary}>\n"
        )
        logger.info(
            "Planner: consuming researcher_skeleton (%d chars).",
            len(researcher_skeleton),
        )

    # ── Budget-guarded assembly (five-layer ContextPipeline) ──
    # L1 identity+rules / L2 project-instructions+skills / L3 trajectory+skeleton are
    # the durable instruction context (never silently truncated). The volatile IDE
    # context (active file, dirty buffers, workspace overview) is the Execution layer
    # (L5) — trimmed first when the window is tight. on_compacted is omitted: a
    # single-shot planner turn carries no running conversation list, so L4 stays empty.
    _budget = resolve_context_budget(state)
    try:
        _agent_ctx = await build_agent_context(
            total_token_budget=_budget,
            foundation=[_l1_identity, _rules],
            project=[_project_instructions, _skill_block],
            memory=[_trajectory_block, skeleton_block],
            execution=[context_str],
            session_id=state.get("task_id", ""),
            session_start_time=state.get("session_start_time"),
        )
        system_prompt_text = _agent_ctx.foundation_block
        _ide_context_block = _agent_ctx.execution_block
    except ContextBudgetError:
        # L1-L3 alone exhaust the window. Never silently drop pinned context: degrade
        # to identity-only and make the model aware of its partial amnesia so it cannot
        # invent rules/style/Git policy it can no longer see. Plain assignment — never a
        # re-entrant build, so this cannot loop even if identity alone exceeds budget.
        logger.warning(
            "Planner context budget exhausted by L1-L3 (budget=%d); degrading to "
            "identity-only prompt with an explicit context-loss alert.",
            _budget, exc_info=True,
        )
        system_prompt_text = _l1_identity
        _ide_context_block = f"{context_str}\n\n{AMNESIA_ALERT}"

    # Human instruction: Here we mentally force the model to respect the SDD contract.
    instruction = (
        f"User requirement: '{user_input}'.\n\n"
        f"IDE context (The files are encapsulated under the secure label) <{boundary}>):\n"
        f"{_ide_context_block}\n"
        "You are the Architect (The Planner). You are PROHIBITED from writing implementation code.\n"
        "Your only task is to generate a complete and logical technical specification (MissionSpecification)."
        "Strictly define the Outcome, Scope, Constraints, Decisions, and sequential Tasks (WBS)"
        "assigning a valid target_role to each task, and the QA validation checks.\n\n"
        + _SCOPE_DISCIPLINE_DIRECTIVE
        + _WBS_SEED_DIRECTIVE +
        # explicit type discipline. The LLM intermittently emits objects
        # where strings belong, and arbitrary role strings; spell out the contract.
        "STRICT TYPE RULES:\n"
        "- Every element of 'scope', 'constraints', 'decisions' and 'checks' MUST be a "
        "plain string. NEVER an object/dict — write a sentence, not '{\"file\": \"x\"}'.\n"
        "- Each task's 'target_role' MUST be exactly ONE of: core_dev, architect_refactor, "
        "devops_infra, secops, qa_tester, doc_manager, vcs_manager, data_ml_engineer.\n"
        "- Each task's 'action' MUST be one of: read_file, write_file, edit_file, run_command.\n"
        "- Set 'requires_iteration': true on a task ONLY when it needs an autonomous "
        "run-read-edit-rerun loop to converge — e.g. fix failing tests, debug a stack trace, "
        "or iterate a build until it passes. Leave it false (or omit it) for trivial "
        "single-shot edits and one-off commands; those take the faster, cheaper path.\n\n"
        # flat-JSON contract to stop envelope wrapping.
        "CRITICAL FORMATTING RULE: Return ONLY the raw JSON object. DO NOT wrap it in any "
        "top-level key such as 'response', 'mission', 'result', or 'MissionSpecification'. "
        "No prose, no markdown fences. Emit exactly these top-level fields: "
        "outcome, scope, constraints, decisions, tasks, checks. "
        'Example shape: {"outcome": "...", "scope": ["..."], "constraints": ["..."], '
        '"decisions": ["..."], "tasks": [{"step_number": 1, "target_role": "core_dev", '
        '"action": "edit_file", "target_file": "src/x.py", "description": "..."}], '
        '"checks": ["..."]}.'
    )

    messages = [
        {"role": "system", "content": system_prompt_text},
        {"role": "user", "content": instruction},
    ]

    # Semantic response cache identity. A deterministic planner draft over an
    # unchanged context is served from memory; the whole assembled prompt folds
    # into the key, so any edit to the active file (or retrieved context) misses.
    # Bypass entirely when the user has unsaved buffers — a dirty turn must always
    # re-plan against the live edits, never a stale cached spec.
    from core.response_cache import response_cache

    # Build the response-cache key from boundary-free, stable inputs only.
    # system_prompt_text and instruction both embed an ephemeral uuid4 boundary
    # nonce that changes every call, so they can never produce a repeatable key.
    # Key off: user_input, active file content, and the Researcher's skeleton + CSS
    # (all deterministic for identical turns over unchanged files / retrieval).
    cache_enabled: bool = not dirty_buffers
    planner_cache_key: str = ""
    planner_cache_paths: list[str] = []
    if cache_enabled:
        cache_ctx: list[tuple[str, str]] = [("<user_input>", user_input)]
        if _active_path and _active_content:
            cache_ctx.append((_active_path, _active_content))
        if researcher_skeleton:
            cache_ctx.append(("<researcher_skeleton>", researcher_skeleton))
        cache_ctx.append(("<css>", f"{updated_context_metrics.css_total:.1f}"))
        # Fold the resolved token budget into the key: the same inputs produce a
        # different budget-trimmed prompt under a different context window (local↔cloud
        # reroute), so a budget-blind key could serve a stale trim.
        cache_ctx.append(("<budget>", str(_budget)))
        planner_cache_key = response_cache.build_key(
            intent=user_input,
            context=cache_ctx,
            project_id=state.get("project_id") or "",
            model=MODEL_BIG,
        )
        planner_cache_paths = [_active_path] if _active_path else []

    # =====================================================================
    # 4. INVOCATION OF THE LLM ENGINE (With Forced Pydantic Validation)
    # =====================================================================
    logger.info("⏳ Esperando Especificación Técnica (SDD) del LLM...")
    await _emit("drafting_spec")  # about to draft the MissionSpecification.

    retry_count: int = 0
    mission_plan: Optional[MissionSpecification] = None

    # Probe the response cache BEFORE acquiring the VRAM lock, so a hit pays no
    # GPU/lock cost. A poisoned or stale entry can never block planning — it falls
    # straight through to the live LLM path below.
    if cache_enabled:
        cached_plan = response_cache.probe(planner_cache_key)
        if cached_plan is not None:
            try:
                _cached_extracted = LLMGateway._extract_nested_schema_target(
                    cached_plan, MissionSpecification
                )
                mission_plan = MissionSpecification.model_validate(_cached_extracted)
                mission_plan = mission_plan.model_copy(update={
                    "tasks": _inject_polyglot_constraints(list(mission_plan.tasks))
                })
                logger.info(
                    "PlannerAgent: served MissionSpecification from semantic response cache."
                )
            except Exception as _cache_err:  # noqa: BLE001 — a poisoned entry must not block planning
                logger.debug("Planner cache entry unusable, re-planning live: %s", _cache_err)
                mission_plan = None

    # Budget estimate from the pre-commit BudgetEstimatorTool. Hoisted above the
    # planning branch so every return path — including the cache-hit / dirty-buffer
    # bypass that skips the branch entirely — has it bound before the result dict.
    _bud: Optional[dict] = None

    if mission_plan is None:
        # mandates Big/cloud model for the Planner.
        # ResourceBroker still arbitrates the VRAM lock; we just request BIG by default.
        decision = await ResourceBroker.acquire_or_resolve(state, model=MODEL_BIG)
        if decision.cancelled:
            return {"errors": ["Planner cancelled by user during VRAM contention."]}

        # Actor-Critic reflection loop: the Pydantic schema IS the critic. Each draft is
        # validated against MissionSpecification; on rejection the exact errors are fed
        # back and the actor re-drafts. Bounded by MAX_PLANNER_RETRIES, this drives the
        # single-shot error rate P(E) down to ~P(E)^n. The narration below frames the
        # cycle (review → rejected → replanning → validated) so it is legible in the log.
        await _emit("critic_review")
        last_validation_err: str = ""

        try:
            while retry_count <= MAX_PLANNER_RETRIES:
                if retry_count > 0 and last_validation_err:
                    # The critic rejected the prior draft — narrate the re-plan attempt.
                    await _emit(f"critic_rejected → replanning ({retry_count}/{MAX_PLANNER_RETRIES})")
                    corrective: str = (
                        f"\n\nYour previous attempt failed schema validation with these errors:\n"
                        f"{last_validation_err}\n"
                        f"Fix them and emit ONLY the raw JSON object for MissionSpecification. "
                        f"DO NOT wrap it in any top-level key (e.g. 'response', 'mission', "
                        f"'MissionSpecification'), do not add prose or markdown fences, and do "
                        f"not omit required fields."
                    )
                    messages[-1] = {
                        **messages[-1],
                        "content": messages[-1]["content"] + corrective,
                    }

                try:
                    # Streams native reasoning to the Thought Box while drafting;
                    # the structured plan JSON is buffered and validated as before.
                    # Falls back to a plain JSON-mode ainvoke on non-reasoning
                    # models (or when thinking is off) with zero behaviour change.
                    raw_content = await LLMGateway.acomplete_with_thinking(
                        messages=messages,
                        model=decision.effective_model,
                        temperature=0.0,
                        response_format={"type": "json_object"},
                        session_id=session_id,
                        on_thinking=_on_thinking,
                        enable_thinking=_thinking_on,
                        thinking_budget_tokens=_thinking_budget,
                    )
                    # unwrap envelopes (markdown / prose /
                    # top-level key) before validation so a wrapped-but-valid plan no longer
                    # burns a retry. No-match returns the base dict → Pydantic still fails loudly.
                    await _emit("unwrapping_schema")
                    extracted = LLMGateway._extract_nested_schema_target(
                        raw_content, MissionSpecification
                    )
                    mission_plan = MissionSpecification.model_validate(extracted)
                    mission_plan = mission_plan.model_copy(update={
                        "tasks": _inject_polyglot_constraints(list(mission_plan.tasks))
                    })
                    # Hard gate: WBS dependency / scope validation. Raises ValueError on
                    # blocking issues so the existing except block feeds structured feedback
                    # to the LLM and increments retry_count — no extra control-flow needed.
                    _wbs_val = ValidateWBSDependenciesTool(state=state)
                    _val = json.loads(await _wbs_val._arun())
                    if not _val.get("valid", True):
                        _issue_lines = [
                            f"{i['type']}: step {i.get('step_number', '?')},"
                            f" file={i.get('target_file', '?')}"
                            + (
                                f" (producer at step {i['first_producer']})"
                                if i.get("first_producer")
                                else ""
                            )
                            for i in _val.get("issues", [])[:5]
                        ]
                        raise ValueError(
                            "WBS pre-commit dependency check failed — fix the following"
                            " then re-emit the plan:\n" + "\n".join(_issue_lines)
                        )
                    # Advisory gate: budget estimate. Stored via the return dict (not
                    # in-place mutation) so LangGraph reducers persist it. Advisory only
                    # — a budget overage is logged but does not consume a retry.
                    _bud_tool = BudgetEstimatorTool(state=state)
                    _bud = cast(dict, json.loads(await _bud_tool._arun()))
                    if not _bud.get("fits_within_budget", True):
                        await _emit("plan_budget_overage_advisory")
                        logger.warning(
                            "plan_budget_advisory: est=$%.4f remaining=$%.4f margin=$%.4f",
                            _bud["estimated_cost_usd"],
                            _bud["remaining_budget_usd"],
                            _bud["margin_usd"],
                        )
                    await _emit("plan_validated")  # the critic accepted the draft.
                    # Cache the validated raw draft for future identical turns.
                    if cache_enabled and raw_content:
                        response_cache.store(
                            planner_cache_key, raw_content, planner_cache_paths
                        )
                    break
                except Exception as parse_err:  # noqa: BLE001 — Pydantic ValidationError + sanitiser errors
                    last_validation_err = str(parse_err)
                    logger.warning(
                        "Planner retry %d/%d — schema validation failed: %s",
                        retry_count + 1,
                        MAX_PLANNER_RETRIES + 1,
                        last_validation_err,
                    )
                    retry_count += 1

            if mission_plan is None:
                logger.error(
                    "Planner: exhausted %d attempts on schema validation.",
                    MAX_PLANNER_RETRIES + 1,
                )
                return {
                    "errors": [
                        f"Planner Error - schema validation exhausted "
                        f"{MAX_PLANNER_RETRIES + 1} attempts: {last_validation_err}"
                    ],
                    "planner_retry_count": retry_count,
                }
        finally:
            if decision.holds_lock:
                await ResourceBroker.release(state.get("task_id", ""))

    # =====================================================================
    # 5. AUDIT AND UPDATE OF THE GLOBAL STATUS
    # =====================================================================
    logger.info("✅ Especificación Técnica generada y validada estrictamente.")

    # structured logging instead of raw print() to stdout.
    # The previous emoji print() block crashed the node on Windows cp1252 consoles
    # ('charmap' codec can't encode '\U0001f4cb'); the logger honours the UTF-8
    # stream reconfigured at startup and keeps the trace out of stdout.
    logger.info(
        "📋 MISSION SPECIFICATION (SDD) — outcome=%s | constraints=%d | tasks=%d | checks=%d",
        mission_plan.outcome,
        len(mission_plan.constraints),
        len(mission_plan.tasks),
        len(mission_plan.checks),
    )

    # Carry the Socratic glossary (when the ideation handoff produced one) into the
    # plan's ubiquitous_language so the domain terms settled during the dialogue
    # survive into the final MissionSpecification. Planner-drafted terms win on key
    # collision; the dialogue glossary only fills gaps.
    _gloss = state.get("ideation_glossary")
    if isinstance(_gloss, dict) and _gloss:
        merged = {**{str(k): str(v) for k, v in _gloss.items()}, **mission_plan.ubiquitous_language}
        mission_plan = mission_plan.model_copy(update={"ubiquitous_language": merged})

    # WBS steps carry only implicit step_number ordering (no dependency graph), so
    # parallel fan-out would run dependent steps out of order. Sequential RELAY
    # execution preserves the displayed plan order; SWARM dispatch stays dormant
    # until an explicit dependency DAG exists.
    parallel_tasks = []

    # The routing signal (tci / css / context_metrics / provider / routing_warning) is
    # produced by the Researcher node and already on state; the Planner is a pure
    # consumer and does not re-emit it.
    result = {
        "mission_spec": mission_plan,
        "parallel_tasks": parallel_tasks,
        "planner_retry_count": retry_count,  # 0 on first-shot success
        "budget_estimate": _bud,  # None if loop exhausted; Dict after clean draft
        # The Planner is the sole reader of researcher_skeleton; clear it once consumed
        # so the (potentially large) skeleton stops serializing into every downstream
        # coder / agentic-cell super-step checkpoint. A new turn re-runs the Researcher.
        "researcher_skeleton": None,
    }
    if state.get("immutable_wbs") is None:
        result["immutable_wbs"] = mission_plan
        logger.info("PlannerAgent: immutable_wbs frozen (first turn, LLM mode).")

    # Export a navigable plan the user can open in the editor preview. The cognitive
    # fast-boot snapshot (dump_state_to_markdown) is owned by the Researcher node.
    try:
        from core.state_manager import dump_plan_to_markdown
        _ws_root = state.get("workspace_root", "")
        dump_plan_to_markdown(mission_plan, _ws_root, str(state.get("task_id") or ""))
    except Exception as _dump_err:
        logger.debug("plan dump skipped: %s", _dump_err)
    # ─────────────────────────────────────────────────────────────────────────

    # Optionally open a dynamic-dispatch fan-out (no-op unless the feature is enabled
    # and a plan is emitted). On emission the graph routes planner → dispatch subgraph
    # and returns to drift_compute; otherwise the pre-existing edge is taken unchanged.
    from brain.dispatch_emitter import maybe_emit_dispatch
    result.update(await maybe_emit_dispatch(state, config, return_node="drift_compute"))

    return result
