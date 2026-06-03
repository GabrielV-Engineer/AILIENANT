# alienant-core/agents/planner.py

import logging
import os as _os
import uuid
from typing import Optional, Any

from langchain_core.runnables import RunnableConfig

# We import our gateway and strict contracts
from tools.llm_gateway import LLMGateway
from shared.config import MODEL_MEDIUM, MODEL_BIG  # noqa: F401 — MEDIUM retained for backward refs
from brain.state import MissionSpecification, WBSStep, ContextMeter
from shared.rbac import PLANNER_IDENTITY
from agents.prompts import build_safe_prompt
from agents.workspace_context import build_workspace_overview 
from core.utils import is_polyglot_file
from core.rules import rule_manager
from core.memory.graphrag_extractor import GraphRAGDynamicExtractor
from core.memory.trajectory_memory import TrajectoryMemoryManager, format_trajectories_for_prompt
from core.memory.semantic_memory import SemanticMemoryManager
from core.resource_manager import ResourceBroker 
from core.memory.context_auditor import (
    audit_task_complexity,
    derive_routing_decision,
    RiskLevel,
)
from brain.retry_policy import PLANNER_MAX_RETRIES

# Bounded planner retry budget on Pydantic ValidationError. Distinct from the
# MICRO_SWARM Coder's MAX_RETRIES (different agent, different gate); both budgets
# are sourced from the central retry policy.
MAX_PLANNER_RETRIES: int = PLANNER_MAX_RETRIES

# Configuración del logger para este nodo específico
logger = logging.getLogger("PLANNER_NODE")

# promoted to module-level so tests can patch it.
# default OFF — the real LLM path now runs (BYOM-aware ainvoke).
# Set AILIENANT_PLANNER_DEBUG=1 to force the synthetic stub (CI/UI smoke tests).
DEBUG_MODE: bool = _os.getenv("AILIENANT_PLANNER_DEBUG", "0") != "0"

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

        # Extraemos parallel_tasks para High-TCI: todos los pasos son candidatos al fan-out.
        parallel_tasks = mock_mission.tasks if tci > 80.0 else []
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
    # We built the System Prompt using the strict role of the Planner
    system_prompt_text = build_safe_prompt(
        agent_identity=PLANNER_IDENTITY, context_str=context_str, boundary=boundary
    )

    _rules = rule_manager.get_combined_rules(state.get("workspace_root", ""))
    if _rules:
        system_prompt_text += f"\n\n{_rules}"

    # ── Trajectory Memory Injection ────────────────────────
    _traj_mgr = TrajectoryMemoryManager()
    _past_trajectories = await _traj_mgr.search(
        user_input=user_input,
        project_id=state.get("project_id") or "",
    )
    if _past_trajectories:
        system_prompt_text += f"\n\n{format_trajectories_for_prompt(_past_trajectories)}"
        logger.info(
            "TrajectoryMemory: injected %d past trajectories into planner context.",
            len(_past_trajectories),
        )
    # ────────────────────────────────────────────────────────────────────

    # ── Semantic-Guided Deep Context Extraction ─────────────────────────
    # Single embedding call returns Top-K file paths + similarity score.
    # deep_parse: 1-degree SQLite neighbor expansion → VFS read → Tree-sitter (in thread).
    # CSS is fully recomputed here; block is subsumed.
    if updated_context_metrics is not None:
        try:
            # ── Fast-Boot: skip LanceDB embedding when AGENTS.md is fresh ─
            from core.state_manager import load_state_from_markdown
            _ws_root_fb: str = state.get("workspace_root", "")
            _fast_boot = load_state_from_markdown(_ws_root_fb) if _ws_root_fb else None

            _sem_score: float
            _top_k_files: list[str]
            _extractor = GraphRAGDynamicExtractor(project_id=state.get("project_id") or "")

            if _fast_boot is not None:
                logger.info("Phase 3.6 Fast-Boot: using cached context, skipping LanceDB search.")
                _sem_score = (
                    _fast_boot.context_metrics.semantic_similarity
                    if _fast_boot.context_metrics else 0.0
                )
                _top_k_files = _fast_boot.top_k_files
            else:
                _sem_mgr = SemanticMemoryManager()
                _sem_score, _top_k_files = await _sem_mgr.search_with_paths(
                    user_input=user_input,
                    workspace_hash=state.get("project_id") or "",
                )
            # ─────────────────────────────────────────────────────────────────────────
            _deep_result = await _extractor.deep_parse(
                seed_files=_top_k_files,
                workspace_root=state.get("workspace_root", ""),
            )
            _new_css: float = min(100.0, max(0.0, (
                0.5 * _sem_score
                + 0.3 * _deep_result.coverage_ratio
                + 0.2 * updated_context_metrics.recency_score
            ) * 100.0))
            updated_context_metrics = updated_context_metrics.model_copy(
                update={
                    "semantic_similarity": _sem_score,
                    "graph_coverage": _deep_result.coverage_ratio,
                    "css_total": _new_css,
                    "is_red_alert": _new_css < 40.0,
                }
            )
            css = _new_css
            if _deep_result.context_block:
                system_prompt_text += f"\n\n{_deep_result.context_block}"
            logger.info(
                "Phase 3.2: sem=%.4f graph=%.3f css=%.1f files_parsed=%d/%d",
                _sem_score, _deep_result.coverage_ratio, _new_css,
                len(_deep_result.parsed_files), len(_deep_result.target_files),
            )
        except Exception as _ctx_err:
            logger.warning("Phase 3.2: context extraction failed (non-fatal): %s", _ctx_err)
    # ────────────────────────────────────────────────────────────────────────────────────

    # ── Context Meter Cascade (Early Exit + Mini-Judge) ──────────────────
    _cascade_routing: str = "LOCAL_SMALL"   # conservative safe default
    _cascade_provider: str = "LOCAL"        # safe default

    try:
        # Initialize ContextMeter on first invocation (context_metrics absent from state).
        if updated_context_metrics is None:
            updated_context_metrics = ContextMeter(
                semantic_similarity=0.0,
                graph_coverage=0.0,
                recency_score=0.5,
                css_total=css,
                task_complexity_index=tci,
                routing_decision="LOCAL_SMALL",  # placeholder; overwritten below
                is_red_alert=css < 40.0,
            )

        if updated_context_metrics.is_red_alert:
            # O(1) early exit: context gap → bypass Mini-Judge, force CLOUD.
            _cascade_routing = "CLOUD"
            _cascade_provider = "CLOUD"
            logger.warning(
                "Phase 3.3: RED ALERT (CSS=%.1f) — Mini-Judge bypassed, routing=CLOUD.",
                updated_context_metrics.css_total,
            )
        else:
            _risk: RiskLevel = await audit_task_complexity(user_input, session_id=session_id)
            _math_routing: str = derive_routing_decision(
                tci, updated_context_metrics.css_total
            )

            if _risk == RiskLevel.HIGH:
                _cascade_routing = "CLOUD"
                _cascade_provider = "CLOUD"
                if _math_routing != "CLOUD":
                    logger.warning(
                        "VETO: Semantic risk detected, overriding %s to CLOUD",
                        _math_routing,
                    )
                tci = 100.0
                updated_context_metrics = updated_context_metrics.model_copy(
                    update={"task_complexity_index": 100.0}
                )
                logger.info("Phase 3.3.3: MiniJudge=HIGH → TCI=100.0, routing=CLOUD.")

            elif _risk == RiskLevel.MEDIUM:
                if _math_routing == "LOCAL_SMALL":
                    _cascade_routing = "LOCAL_BIG"
                    logger.warning(
                        "VETO: Semantic risk detected, overriding LOCAL_SMALL to LOCAL_BIG"
                    )
                else:
                    _cascade_routing = _math_routing
                _cascade_provider = "CLOUD" if _cascade_routing == "CLOUD" else "LOCAL"
                tci = max(tci, 75.0)
                updated_context_metrics = updated_context_metrics.model_copy(
                    update={"task_complexity_index": tci}
                )
                logger.info(
                    "Phase 3.3.3: MiniJudge=MEDIUM → TCI=%.1f, routing=%s.",
                    tci, _cascade_routing,
                )

            else:
                _cascade_routing = _math_routing
                _cascade_provider = "CLOUD" if _cascade_routing == "CLOUD" else "LOCAL"
                logger.info(
                    "Phase 3.3.3: MiniJudge=NONE → routing=%s (math defer).",
                    _cascade_routing,
                )

        updated_context_metrics = updated_context_metrics.model_copy(
            update={"routing_decision": _cascade_routing}
        )
        logger.info(
            "Phase 3.3: cascade done — routing=%s provider=%s css=%.1f tci=%.1f",
            _cascade_routing, _cascade_provider, updated_context_metrics.css_total, tci,
        )
    except Exception as _cascade_err:
        logger.warning("cascade failed (non-fatal): %s", _cascade_err)
    # ────────────────────────────────────────────────────────────────────────────────────

    await _emit("routing_decision")  # routing/cascade resolved.

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

    # Human instruction: Here we mentally force the model to respect the SDD contract.
    instruction = (
        f"User requirement: '{user_input}'.\n\n"
        f"IDE context (The files are encapsulated under the secure label) <{boundary}>):\n"
        f"{context_str}\n"
        f"{skeleton_block}"
        "You are the Architect (The Planner). You are PROHIBITED from writing implementation code.\n"
        "Your only task is to generate a complete and logical technical specification (MissionSpecification)."
        "Strictly define the Outcome, Scope, Constraints, Decisions, and sequential Tasks (WBS)"
        "assigning a valid target_role to each task, and the QA validation checks.\n\n"
        # explicit type discipline. The LLM intermittently emits objects
        # where strings belong, and arbitrary role strings; spell out the contract.
        "STRICT TYPE RULES:\n"
        "- Every element of 'scope', 'constraints', 'decisions' and 'checks' MUST be a "
        "plain string. NEVER an object/dict — write a sentence, not '{\"file\": \"x\"}'.\n"
        "- Each task's 'target_role' MUST be exactly ONE of: core_dev, architect_refactor, "
        "devops_infra, secops, qa_tester, doc_manager, vcs_manager, data_ml_engineer.\n"
        "- Each task's 'action' MUST be one of: read_file, write_file, edit_file, run_command.\n\n"
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

    # =====================================================================
    # 4. INVOCATION OF THE LLM ENGINE (With Forced Pydantic Validation)
    # =====================================================================
    logger.info("⏳ Esperando Especificación Técnica (SDD) del LLM...")
    await _emit("drafting_spec")  # about to draft the MissionSpecification.

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
    retry_count: int = 0
    last_validation_err: str = ""
    mission_plan: Optional[MissionSpecification] = None

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
                response = await LLMGateway.ainvoke(
                    messages=messages,
                    model=decision.effective_model,
                    temperature=0.0,
                    response_format={"type": "json_object"},
                    session_id=session_id,
                )
                raw_content = response.choices[0].message.content or ""
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
                await _emit("plan_validated")  # the critic accepted the draft.
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

    # For High-TCI, all WBSSteps are candidates for MapReduce fan-out.
    parallel_tasks = mission_plan.tasks if tci > 80.0 else []

    result = {
        "mission_spec": mission_plan,
        "parallel_tasks": parallel_tasks,
        "tci": tci,
        "css": css,
        "context_metrics": updated_context_metrics,
        "provider": _cascade_provider,
        "planner_retry_count": retry_count,  # 0 on first-shot success
    }
    if state.get("immutable_wbs") is None:
        result["immutable_wbs"] = mission_plan
        logger.info("PlannerAgent: immutable_wbs frozen (first turn, LLM mode).")

    # flush cognitive state to .ailienant/AGENTS.md ────────────
    try:
        from core.state_manager import dump_state_to_markdown
        _state_for_dump = dict(state) | result
        _state_for_dump["_top_k_files_cache"] = locals().get("_top_k_files", [])
        dump_state_to_markdown(_state_for_dump, state.get("workspace_root", ""))
    except Exception as _dump_err:
        logger.debug("state dump skipped: %s", _dump_err)
    # ─────────────────────────────────────────────────────────────────────────

    return result
