# ailienant-core/agents/planner.py

import asyncio
import json
import logging
import os
import uuid
from typing import Optional, Any, cast

from langchain_core.runnables import RunnableConfig

# tools.llm_gateway (and core.memory.trajectory_memory, below) both pull in
# litellm's provider-registry surface (~2.6s to import) — deferred into
# run_planner_node at their point of use, matching this codebase's established
# "keep module import light" convention, so a cold process boot never pays
# this cost until a real planner turn actually runs.
from shared.config import MODEL_MEDIUM, MODEL_BIG  # noqa: F401 — MEDIUM retained for backward refs
from brain.state import MissionSpecification, WBSStep, ContextMeter
from shared.rbac import PLANNER_IDENTITY
from agents.prompts import build_boundary_declaration, build_static_identity_prompt
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
from core.resource_manager import ResourceBroker
from brain.retry_policy import PLANNER_MAX_RETRIES
from tools.planner_tools import BudgetEstimatorTool, ValidateWBSDependenciesTool

# Bounded planner retry budget on Pydantic ValidationError. Distinct from the
# MICRO_SWARM Coder's MAX_RETRIES (different agent, different gate); both budgets
# are sourced from the central retry policy.
MAX_PLANNER_RETRIES: int = PLANNER_MAX_RETRIES

# Token ceiling for the pre-draft plan-of-attack reasoning pass (non-native models).
# Small on purpose: this is a conceptual narrative shown while the user waits, not the
# plan itself — the strict WBS draft that follows carries the real output budget.
_PLANNER_REASONING_MAX_TOKENS: int = 512

# Output ceiling for the strict WBS draft itself (the MissionSpecification JSON —
# outcome/scope/constraints/decisions/tasks/checks). Distinct from the reasoning
# ceiling above. The gateway default (4096, applied whenever no max_tokens is passed)
# budgets a narrow, single-file request comfortably, but a broad "build an MVP"
# request needs a WBS with many tasks and correspondingly more JSON — a flat ceiling
# there produces exactly the shallow/truncated plans this phase corrects (audited
# 2026-07-28 live-test sweep). Scale by the request's length (a longer, more detailed
# ask usually specifies a broader build) rather than bumping the default globally,
# bounded by half the resolved model's real context window.
_PLANNER_DRAFT_MIN_MAX_TOKENS: int = 4096
_PLANNER_DRAFT_MAX_MAX_TOKENS: int = 16384


def _resolve_planner_draft_max_tokens(user_input: str, budget: int) -> int:
    """Derive the WBS draft's output-token ceiling from request breadth.

    Never raises — any unexpected input degrades to the historical flat default.
    """
    try:
        scaled = _PLANNER_DRAFT_MIN_MAX_TOKENS + len(user_input or "") * 6
        # The real context window is the HARD ceiling — it must win even over the
        # historical flat floor, or a small-window local model would be asked for
        # more completion tokens than its window has room for. Only within that
        # hard ceiling do we prefer at least the flat floor.
        hard_ceiling = min(_PLANNER_DRAFT_MAX_MAX_TOKENS, budget // 2)
        floor = min(_PLANNER_DRAFT_MIN_MAX_TOKENS, hard_ceiling)
        return int(max(floor, min(scaled, hard_ceiling)))
    except Exception:  # noqa: BLE001 — a budget-derivation fault must not block planning
        logger.debug("planner: max_tokens scaling failed; falling back to flat default", exc_info=True)
        return _PLANNER_DRAFT_MIN_MAX_TOKENS

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
    "file the user did not ask about.\n"
    "- Scale the WBS to the request's real breadth: a narrow or single-file request gets "
    "the smallest WBS that satisfies it — do not pad it with unrequested steps. A broad "
    "request (e.g. 'build an MVP', 'implement the whole feature') gets as many steps as "
    "the artifact genuinely needs to be complete and working — do not compress a "
    "multi-module build into one or two steps just to appear minimal. Under-building a "
    "broad request is as much a violation of this directive as over-building a narrow "
    "one.\n\n"
)

# Counter-bias for unconstrained "build X" requests. Absent this, an ambiguous request
# is filled by the model's own training prior — historically a generic web stack
# (Django/React) even when it plainly does not fit the artifact class (a game, a CLI
# tool, a data pipeline). Deliberately a PROCEDURE, not a catalog of named frameworks:
# a hardcoded stack list would cost real tokens on every planner call (including trivial
# ones), goes stale as the framework landscape moves, and would override the model's own
# (more current) knowledge with a hand-maintained one. This asks the model to reason about
# fit, record what it decided, and never end up in a state where the decision and the plan
# disagree (audited 2026-07-28 live-test sweep; widened after a follow-up audit found the
# first version too narrow and its decision unable to reach either code generator — see
# MissionSpecification.to_context_block in brain/state.py for the propagation half).
_STACK_GUIDANCE_DIRECTIVE: str = (
    "STACK CHOICE:\n"
    "1. If the user named a stack or framework, use it — the rest of this section applies "
    "only when they did not.\n"
    "2. If the workspace overview or an existing manifest/lockfile already shows a stack in "
    "use, extend it rather than introducing a second, competing one.\n"
    "3. Otherwise, before writing any task: (a) name the artifact class being requested in "
    "your own words — do not force it into a category you happen to know well; (b) name the "
    "constraints that genuinely narrow the choice (where it must run, whether it is "
    "interactive/realtime/batch, anything the user stated outright); (c) choose the "
    "technology a practitioner building THAT artifact class would reach for by default — "
    "not the most popular technology in general. A game needs a game engine or a "
    "game-appropriate library, not a web CRUD framework; a CLI tool needs an "
    "argument-parsing library, not a server framework; a data pipeline needs data tooling, "
    "not a UI framework — but reason to the answer, do not just match these examples.\n"
    "4. Record the outcome as the FIRST entry of 'decisions', in the exact form "
    "'Stack: <specific named technologies> — <one-sentence reason tied to the artifact "
    "class>'. A vague answer like 'a game engine' with no name is not acceptable.\n"
    "5. Every task's target_file must be consistent with the chosen stack — its language, "
    "file extensions, and conventional project layout. Do not choose a stack in 'decisions' "
    "and then write tasks for a different one.\n"
    "6. If the artifact class is genuinely ambiguous even after reasoning about it, still "
    "choose and still record it as in step 4, additionally naming the ambiguity and the "
    "alternative you rejected in that same entry.\n"
    "7. A generic web stack is correct only when the artifact genuinely is a web "
    "application — never as a default filled in because nothing more specific was named.\n\n"
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

# Logger scoped to this node
logger = logging.getLogger("PLANNER_NODE")

# promoted to module-level so tests can patch it.
# default OFF — the real LLM path now runs (BYOM-aware ainvoke).
# Set AILIENANT_PLANNER_DEBUG=1 to force the synthetic stub (CI/UI smoke tests).
DEBUG_MODE: bool = os.getenv("AILIENANT_PLANNER_DEBUG", "0") != "0"

_POLYGLOT_WARNING = (
    "[!] POLYGLOT FILE DETECTED: {target_file}. "
    "You MUST use the 'patch_file' tool for any modifications. "
    "Full file rewrites are strictly forbidden to prevent corrupting mixed syntax."
)


def _inject_polyglot_constraints(tasks: list[WBSStep]) -> list[WBSStep]:
    """Return a new task list with a polyglot-file tool-usage directive attached
    to each affected step's ``agent_notes`` — coder-only, never appended to the
    human-facing ``description`` the checklist renders."""
    result = []
    for step in tasks:
        if step.target_file and is_polyglot_file(step.target_file):
            warning = _POLYGLOT_WARNING.format(target_file=step.target_file)
            notes = f"{step.agent_notes}\n{warning}" if step.agent_notes else warning
            step = step.model_copy(update={"agent_notes": notes})
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
    logger.info("🧠 PlannerAgent starting architectural analysis of the mission...")
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
            "⚠️ DEBUG MODE ACTIVE: generating a synthetic SDD contract (LLM bypassed). TCI=%.1f CSS=%.1f",
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
        context_str = f"<{boundary}>No dirty buffers or active IDE context detected.</{boundary}>"
    elif dirty_buffers:
        for buf in dirty_buffers:
            # dict vs Pydantic object compatibility
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
    # are never silently dropped.
    #
    # Identity is nonce-free and byte-identical across every planner call (see
    # build_static_identity_prompt) — this is the cacheable prefix. The per-turn
    # `boundary` nonce still seals the sandbox end-to-end exactly as before; it is
    # declared once via build_boundary_declaration() and appended, unconditionally,
    # as the LAST fragment of the system message in both the success and degrade
    # paths below (see the try/except after the ContextPipeline call) — never
    # folded into a budget-guarded layer, so it can never be silently trimmed away.
    _l1_identity = build_static_identity_prompt(agent_identity=PLANNER_IDENTITY)
    _boundary_decl = build_boundary_declaration(boundary)

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
    from core.memory.trajectory_memory import TrajectoryMemoryManager, format_trajectories_for_prompt

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
        # _boundary_decl is intentionally OUTSIDE the pipeline's budget-guarded
        # layers (see build_boundary_declaration's docstring) — appended here,
        # unconditionally, so it is never subject to L3 tail-eviction.
        system_prompt_text = f"{_agent_ctx.foundation_block}\n\n{_boundary_decl}"
        _ide_context_block = _agent_ctx.execution_block
    except ContextBudgetError:
        # L1-L3 alone exhaust the window. Never silently drop pinned context: degrade
        # to identity-only and make the model aware of its partial amnesia so it cannot
        # invent rules/style/Git policy it can no longer see. Plain assignment — never a
        # re-entrant build, so this cannot loop even if identity alone exceeds budget.
        # The boundary declaration still survives — dropping rules/style/skills under
        # pressure is an acceptable quality tradeoff (the model is warned via
        # AMNESIA_ALERT); dropping the sandbox seal while untrusted content remains
        # in the user turn would be a security regression, not a quality one.
        logger.warning(
            "Planner context budget exhausted by L1-L3 (budget=%d); degrading to "
            "identity-only prompt with an explicit context-loss alert.",
            _budget, exc_info=True,
        )
        system_prompt_text = f"{_l1_identity}\n\n{_boundary_decl}"
        _ide_context_block = f"{context_str}\n\n{AMNESIA_ALERT}"

    # Human instruction: Here we mentally force the model to respect the SDD contract.
    instruction = (
        f"User requirement: '{user_input}'.\n\n"
        # Bare reference only — no axiom/declaration language here (SEAL2): the
        # sandbox rule and which tag is authoritative are declared exclusively in
        # the system message via build_boundary_declaration(), a trusted-only
        # channel untrusted content can never write to (see that function's
        # docstring). Restating the rule here, in the same message role as the
        # untrusted content it wraps, would let injected text forge a competing
        # declaration with no structural way for the model to prefer the real one.
        f"IDE context (see the <{boundary}> tags below):\n"
        f"{_ide_context_block}\n"
        "You are the Architect (The Planner). You are PROHIBITED from writing implementation code.\n"
        "Your only task is to generate a complete and logical technical specification (MissionSpecification)."
        "Strictly define the Outcome, Scope, Constraints, Decisions, and sequential Tasks (WBS)"
        "assigning a valid target_role to each task, and the QA validation checks.\n\n"
        + _SCOPE_DISCIPLINE_DIRECTIVE
        + _WBS_SEED_DIRECTIVE
        + _STACK_GUIDANCE_DIRECTIVE +
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
    from tools.llm_gateway import LLMGateway

    logger.info("⏳ Awaiting the technical specification (SDD) from the LLM...")
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

        # Live plan-of-attack reasoning, streamed to the Thought Box while the user
        # waits — ONLY for non-native models. A native model already surfaces its own
        # reasoning on a separate channel during the strict JSON draft below, so a second
        # pass would double the trace and the cost. A non-native model's strict JSON draft
        # cannot safely carry a reasoning preamble (it corrupts the machine-parsed output),
        # so the reasoning runs here as a separate, free-form completion. Best-effort: a
        # sink or generation fault never blocks planning; a real abort propagates.
        if _on_thinking is not None and _thinking_on:
            _r_model = decision.effective_model
            _r_tier = _r_model.split("/", 1)[1] if _r_model.startswith("ailienant/") else "big"
            _r_tier = _r_tier if _r_tier in ("small", "medium", "big") else "big"
            from core.config.model_resolver import get_chat_target  # deferred — load order
            from tools.llm_gateway import _supports_native_thinking

            _r_target = get_chat_target(_r_tier)
            _r_native = _r_target is not None and _supports_native_thinking(_r_target.model)
            if not _r_native:
                _reasoning_user = (
                    f"User requirement: '{user_input}'.\n\n"
                    # Bare reference only — see the SEAL2 comment above (~L491):
                    # sandbox semantics live exclusively in the system message.
                    f"Relevant project context (see the <{boundary}> tags below):\n"
                    f"{_ide_context_block}\n\n"
                    "Before the formal specification is written, think out loud about your "
                    "plan of attack: the goal, the key files and the order you would touch "
                    "them, the main risks or edge cases, and how you will verify success. "
                    "Write concise, conceptual prose — no code, no JSON, no file dumps."
                )
                _reasoning_messages = [
                    {"role": "system", "content": system_prompt_text},
                    {"role": "user", "content": _reasoning_user},
                ]
                _sink_live = True
                try:
                    async for _d in LLMGateway.astream_reasoning(
                        _reasoning_messages,
                        tier=_r_tier,
                        temperature=0.0,
                        max_tokens=_PLANNER_REASONING_MAX_TOKENS,
                        session_id=session_id,
                        thinking_budget_tokens=_thinking_budget,
                        free_form_answer=True,
                    ):
                        # The entire free-form output is reasoning for the Thought Box, so
                        # forward both 'thinking' and 'text' deltas as they arrive (live).
                        if _sink_live and _d.text:
                            try:
                                await _on_thinking(_d.text, _d.source)
                            except asyncio.CancelledError:
                                raise
                            except Exception as _sink_exc:  # noqa: BLE001 — best-effort stream
                                logger.debug(
                                    "planner reasoning sink failed; latching off: %s", _sink_exc
                                )
                                _sink_live = False
                except asyncio.CancelledError:
                    raise
                except Exception:  # noqa: BLE001 — reasoning is best-effort; never block the draft
                    logger.debug("planner reasoning pass failed (non-fatal)", exc_info=True)

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
                        max_tokens=_resolve_planner_draft_max_tokens(user_input, _budget),
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
    logger.info("✅ Technical specification generated and strictly validated.")

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

    # Companion decision point: the plan just committed. Consumes only THIS
    # just-drafted MissionSpecification's own summary fields — never blocks
    # the graph.
    from brain.coder_companion import schedule_agent_companion, build_planning_companion_request
    schedule_agent_companion(
        state, "planning", 0,
        lambda: build_planning_companion_request(
            session_id=session_id, task_id=session_id, mission_plan=mission_plan,
        ),
    )

    # Optionally open a dynamic-dispatch fan-out (no-op unless the feature is enabled
    # and a plan is emitted). On emission the graph routes planner → dispatch subgraph
    # and returns to drift_compute; otherwise the pre-existing edge is taken unchanged.
    from brain.dispatch_emitter import maybe_emit_dispatch
    result.update(await maybe_emit_dispatch(state, config, return_node="drift_compute"))

    return result
