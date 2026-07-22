# ailienant-core/agents/researcher.py
#
# ResearcherAgent (The Context Hound) — first-class retrieval + routing node.
#
# Owns the full information-retrieval domain: a bounded READ_ONLY tool-dispatch loop
# (glob/grep/AST/GraphRAG/dependents), GraphRAG deep-context extraction, @-mention
# bypass, recency, the Context Meter Cascade (CSS → routing), and hardware-aware
# reroute. It emits the routing signal (`context_metrics`, `css`, `tci`, `provider`,
# `routing_warning`) plus a dense `researcher_skeleton` that the PlannerAgent consumes
# as its structural view of the codebase — the Planner performs no retrieval of its own.

from __future__ import annotations

import logging
import os
import time
import uuid
from typing import Any, AsyncIterator, Dict, List, Optional, Tuple

from langchain_core.runnables import RunnableConfig

from agents.prompts import build_safe_prompt
from agents.recency import compute_recency_score, session_heatmap
from brain.state import ContextMeter
from core.graph_weight import estimate_graph_weight
from core.memory.context_auditor import (
    RiskLevel,
    audit_task_complexity,
    derive_routing_decision,
    hardware_reroute,
    is_fast_track_eligible,
)
from shared.config import MODEL_MEDIUM, check_cloud_availability
from shared.rbac import RESEARCHER_IDENTITY
from tools.llm_gateway import LLMGateway

logger = logging.getLogger("RESEARCHER_NODE")

# Live by default (set AILIENANT_RESEARCHER_DEBUG=1 to force the synthetic stub used
# by CI / UI smoke tests). Promoted to module scope so tests can patch it.
DEBUG_MODE: bool = os.getenv("AILIENANT_RESEARCHER_DEBUG", "0") != "0"

# Minimum semantic similarity (0–1) for the deep-context block to be folded into the
# skeleton's source material. Below this floor a retrieved file is noise.
_DEEP_CONTEXT_MIN_SIM: float = 0.20

# Upper bound on reason→call→observe cycles the grounding loop spends before it must
# commit to the skeleton. Bounded so a chatty model cannot stall the node.
_RESEARCHER_TOOL_MAX_ITERS: int = 3

# Hard ceiling (chars) on the skeleton handed to the Planner — a defensive cap above
# the ~2048-token generation bound, so an oversized buffer can never saturate the
# Planner's context window. ~16000 chars ≈ ~4k tokens; normal skeletons pass untouched.
_SKELETON_MAX_CHARS: int = 16000

_SKELETON_INSTRUCTION: str = (
    "You are the ResearcherAgent. Output a dense, technical 'Skeleton Map' that is "
    "the PlannerAgent's primary structural view of the codebase. It MUST contain: "
    "(a) every relevant file by exact workspace-relative path with a one-line purpose; "
    "(b) the full public function and class signatures (names, parameters, return "
    "types) — not prose summaries; (c) cross-module relationships (imports, call edges, "
    "and dependents). DO NOT write implementation code, DO NOT dump full file bodies, "
    "and DO NOT replace signatures with vague descriptions. Respond as compact markdown "
    "sections; precision beats brevity — a vague skeleton starves the planner."
)


def _collect_buffer_mtimes(workspace_root: str, paths: list[str]) -> list[float]:
    """Best-effort epoch mtimes for the active/dirty IDE buffers.

    Feeds the recency time-decay term so a file the user just edited reads as fresh
    even when its LanceDB ``indexed_at`` is stale. Resolves relative paths against the
    workspace root; any path that is unsaved, virtual, or otherwise not on disk is
    silently skipped — recency must never break the turn.
    """
    mtimes: list[float] = []
    for path in paths:
        if not path:
            continue
        candidate = path if os.path.isabs(path) else os.path.join(workspace_root, path)
        try:
            mtimes.append(os.stat(candidate).st_mtime)
        except OSError:
            continue
    return mtimes


async def _gather_tool_grounding(
    state: Dict[str, Any],
    config: Optional[RunnableConfig],
    task_id: str,
) -> Tuple[str, List[Dict[str, Any]]]:
    """Run a bounded READ_ONLY tool loop to ground the skeleton in the user's real code.

    The researcher may call its retrieval tools (glob, grep, workspace_structure,
    query_graphrag, get_dependents) before composing the skeleton, so it maps concrete
    findings rather than guessing. Every call is gated through the same permission
    matrix as the rest of the system; the researcher's tools are all READ_ONLY, so the
    gate is friction-free here. Best-effort: skipped when there is no workspace to
    inspect, and any failure degrades to no grounding (the node must never crash).

    Returns the grounding text block (possibly empty) and the trace of executed tool
    calls (name + args per entry) for the state delta.
    """
    if not (state.get("workspace_root") or state.get("project_id")):
        return "", []
    try:
        from core.permissions import session_mode_from_channel
        from core.tool_dispatch import ToolCall, ToolDispatcher, make_gateway_reasoner
        from shared.rbac import PermissionMode
        from tools.researcher_tools import build_researcher_tools

        tools = build_researcher_tools(state)
        dispatcher = ToolDispatcher(
            tools,
            active_role="researcher",
            session_mode=session_mode_from_channel(state.get("session_permission_mode")),
            state=state,
            agent_permission=PermissionMode.READ_ONLY,
        )
        configurable = (config or {}).get("configurable", {})
        reasoner = configurable.get("researcher_tool_reasoner") or make_gateway_reasoner(
            tools, session_id=task_id
        )
        seed = (
            "Before composing the Skeleton Map, you MAY call READ_ONLY retrieval tools "
            "(glob, grep, workspace_structure, query_graphrag, get_dependents) to ground "
            "it in the user's real code. Call only what helps; emit {} to skip."
        )
        loop_messages: List[Dict[str, Any]] = [{"role": "user", "content": seed}]
        trace: List[ToolCall] = []
        await dispatcher.run_loop(
            loop_messages, reasoner, max_iters=_RESEARCHER_TOOL_MAX_ITERS, trace=trace
        )
        observations = [
            str(m.get("content", ""))
            for m in loop_messages
            if m.get("role") == "system"
            and str(m.get("content", "")).startswith("[tool observations]")
        ]
        block = (
            "## Read-only diagnostics gathered for this skeleton\n"
            + "\n\n".join(observations)
            if observations
            else ""
        )
        return block, [{"name": c.name, "args": c.args} for c in trace]
    except Exception as exc:  # noqa: BLE001 — researcher must never crash the graph
        logger.warning(
            "Researcher tool grounding failed [%s: %s]", type(exc).__name__, exc
        )
        return "", []


def _cold_context_meter(state: Dict[str, Any], css: float, tci: float) -> ContextMeter:
    """Safe-default ContextMeter for the cold path / error fallback.

    No retrieval ran, so recency leans on edit-freshness of the open/dirty buffers
    plus any in-session access history. Mirrors the cascade's cold-path init so a
    bypass or failure never propagates a None routing signal downstream.
    """
    ide_context = state.get("ide_context", {})
    dirty_buffers = (
        ide_context.get("dirty_buffers", [])
        if isinstance(ide_context, dict)
        else getattr(ide_context, "dirty_buffers", [])
    )
    _active_path = state.get("active_file_path", "")
    _cold_buffer_paths: list[str] = ([_active_path] if _active_path else []) + [
        (b.get("path") if isinstance(b, dict) else b.path) for b in dirty_buffers
    ]
    _cold_project_id: str = state.get("project_id") or ""
    _cold_recency: float = compute_recency_score(
        indexed_at_iso=[],
        buffer_mtimes=_collect_buffer_mtimes(
            state.get("workspace_root", ""), _cold_buffer_paths
        ),
        access_count=sum(
            session_heatmap.count(_cold_project_id, p) for p in _cold_buffer_paths
        ),
        now=time.time(),
    )
    return ContextMeter(
        semantic_similarity=0.0,
        graph_coverage=0.0,
        recency_score=_cold_recency,
        css_total=css,
        task_complexity_index=tci,
        routing_decision="LOCAL_SMALL",
        is_red_alert=css < 40.0,
    )


async def run_researcher_node(
    state: Dict[str, Any], config: Optional[RunnableConfig] = None
) -> Dict[str, Any]:
    """Read-only retrieval + routing node — produces the skeleton and the routing signal.

    Pipeline:
        1. Bounded READ_ONLY tool-grounding loop (skipped on fast_track / @-mention).
        2. Retrieval: @-mention bypass OR fast-boot + GraphRAG deep-context + recency.
        3. Context Meter Cascade (CSS → red-alert → mini-judge → routing) + hardware reroute.
        4. ONE LLM call compressing the gathered context into a dense Skeleton Map.

    Emits ``researcher_skeleton`` plus the routing channels (``context_metrics``,
    ``css``, ``tci``, ``provider``, ``routing_warning``) consumed by the Planner and the
    downstream routers. Failure modes degrade — the node never crashes the graph and
    always emits a non-None ``context_metrics``.
    """
    session_id: str = state.get("task_id") or str(uuid.uuid4())
    user_input: str = state.get("user_input", "")
    workspace_root: str = state.get("workspace_root") or ""
    project_id: str = state.get("project_id") or ""
    explicit_mentions: List[str] = state.get("explicit_mentions") or []
    # Retrieval seams are injectable so a benchmark can degrade them explicitly;
    # production omits these keys and the real bound methods below run unchanged.
    _configurable = (config or {}).get("configurable", {})
    _retrieval_fn = _configurable.get("planner_retrieval_fn")
    _graph_fn = _configurable.get("graph_fn")

    # Read TCI and CSS from state; fall back to the carried ContextMeter fields.
    tci: float = state.get("tci", 0.0)
    css: float = state.get("css", 100.0)
    metrics = state.get("context_metrics")
    if metrics is not None and tci == 0.0:
        tci = getattr(metrics, "task_complexity_index", 0.0)
    if metrics is not None and css == 100.0:
        css = getattr(metrics, "css_total", 100.0)
    updated_context_metrics = metrics  # default: pass through unchanged

    _fast_track: bool = is_fast_track_eligible(user_input)

    # IDE buffers feed the recency term during retrieval.
    ide_context = state.get("ide_context", {})
    dirty_buffers = (
        ide_context.get("dirty_buffers", [])
        if isinstance(ide_context, dict)
        else getattr(ide_context, "dirty_buffers", [])
    )
    _active_path = state.get("active_file_path", "")

    # ── DEBUG bypass — synthetic skeleton + synthetic routing signal ──
    if DEBUG_MODE:
        logger.warning("⚠️ RESEARCHER DEBUG MODE: emitting synthetic skeleton + metrics.")
        debug_metrics = updated_context_metrics or _cold_context_meter(state, css, tci)
        return {
            "researcher_skeleton": (
                "## Skeleton (debug)\n- (synthetic, no retrieval performed)\n"
            ),
            "context_metrics": debug_metrics,
            "css": css,
            "tci": tci,
            "provider": "LOCAL",
            "routing_warning": None,
        }

    # ── Override A: @-mention bypass — read the requested files verbatim ──
    forced_blocks: List[str] = []
    if explicit_mentions:
        from core.vfs_middleware import VFSMiddleware

        vfs = VFSMiddleware()
        for path in explicit_mentions:
            try:
                content = vfs.read(path)
            except FileNotFoundError:
                logger.warning("Researcher: @-mention '%s' not found in VFS.", path)
                continue
            except Exception as err:  # noqa: BLE001 — fail-soft on any I/O issue
                logger.warning("Researcher: VFS read failed for '%s': %s", path, err)
                continue
            forced_blocks.append(
                f"[HARD CONTEXT: SOURCE FILE {path}]\n```\n{content}\n```"
            )

    # ── Bounded READ_ONLY tool grounding (skip on fast_track / @-mention bypass) ──
    grounding_block: str = ""
    dispatch_trace: List[Dict[str, Any]] = []
    if not _fast_track and not explicit_mentions:
        grounding_block, dispatch_trace = await _gather_tool_grounding(
            state, config, session_id
        )

    # Ensure a ContextMeter exists before retrieval so the CSS update has a base to
    # model_copy. The Researcher runs first in the graph, so unlike the old Planner
    # path it must retrieve on a cold turn too — gating retrieval on a pre-existing
    # metric would leave the first turn with an empty skeleton.
    if updated_context_metrics is None:
        updated_context_metrics = _cold_context_meter(state, css, tci)

    # ── Semantic-Guided Deep Context Extraction (relocated from the Planner) ──
    # Single embedding call returns Top-K file paths + similarity score.
    # deep_parse: 1-degree SQLite neighbor expansion → VFS read → Tree-sitter (in thread).
    # CSS is fully recomputed here.
    _deep_context_block: str = ""
    _top_k_files: list[str] = []
    # Distinguishes a cold/empty workspace (nothing to retrieve) from a rich-but-low-
    # coverage one: an empty corpus must not trip the red-alert CLOUD floor. Defaults
    # to False (treat as non-empty) so the fast-track / @-mention paths — which skip
    # retrieval — and any probe failure keep the conservative escalation behavior.
    _corpus_empty: bool = False
    if not _fast_track and not explicit_mentions:
        try:
            from core.memory.graphrag_extractor import GraphRAGDynamicExtractor
            from core.memory.semantic_memory import SemanticMemoryManager

            # ── Fast-Boot: skip LanceDB embedding when AGENTS.md is fresh ─
            from core.state_manager import load_state_from_markdown
            _ws_root_fb: str = state.get("workspace_root", "")
            _fast_boot = load_state_from_markdown(_ws_root_fb) if _ws_root_fb else None

            _sem_score: float
            _indexed_at: list[str]
            _extractor = GraphRAGDynamicExtractor(project_id=state.get("project_id") or "")
            # Single manager instance, reused for retrieval and the corpus-presence
            # probe below (no-arg ctor resolves the bound project's LanceDB partition).
            _sem_mgr = SemanticMemoryManager()

            if _fast_boot is not None:
                logger.info("Fast-Boot: using cached context, skipping LanceDB search.")
                _sem_score = (
                    _fast_boot.context_metrics.semantic_similarity
                    if _fast_boot.context_metrics else 0.0
                )
                _top_k_files = _fast_boot.top_k_files
                # Cache path carries no indexed_at; recency leans on buffer mtimes
                # and the session heatmap instead.
                _indexed_at = []
            else:
                _retrieval = _retrieval_fn or _sem_mgr.search_with_paths
                _sem_score, _top_k_files, _indexed_at = await _retrieval(
                    user_input=user_input,
                    workspace_hash=state.get("project_id") or "",
                )
            # ─────────────────────────────────────────────────────────────────────────
            _deep_parse = _graph_fn or _extractor.deep_parse
            _deep_result = await _deep_parse(
                seed_files=_top_k_files,
                workspace_root=state.get("workspace_root", ""),
            )
            # ── Session-Heatmap Recency ──────────────────────────────────
            _project_id: str = state.get("project_id") or ""
            # Cheap, short-TTL cached probe: did this workspace index anything? An
            # empty corpus makes a low CSS a cold-start artifact, not a coverage gap,
            # so the red-alert escalation below must not fire on it.
            _corpus_empty = await _sem_mgr.is_corpus_empty(_project_id)
            session_heatmap.bump(_project_id, _top_k_files)
            _buffer_paths: list[str] = (
                [_active_path] if _active_path else []
            ) + [
                (b.get("path") if isinstance(b, dict) else b.path)
                for b in dirty_buffers
            ]
            _access_count: int = sum(
                session_heatmap.count(_project_id, p) for p in _top_k_files
            )
            _recency: float = compute_recency_score(
                indexed_at_iso=_indexed_at,
                buffer_mtimes=_collect_buffer_mtimes(
                    state.get("workspace_root", ""), _buffer_paths
                ),
                access_count=_access_count,
                now=time.time(),
            )
            _new_css: float = min(100.0, max(0.0, (
                0.5 * _sem_score
                + 0.3 * _deep_result.coverage_ratio
                + 0.2 * _recency
            ) * 100.0))
            updated_context_metrics = updated_context_metrics.model_copy(
                update={
                    "semantic_similarity": _sem_score,
                    "graph_coverage": _deep_result.coverage_ratio,
                    "recency_score": _recency,
                    "css_total": _new_css,
                    "is_red_alert": (_new_css < 40.0) and not _corpus_empty,
                }
            )
            css = _new_css
            # Gate the injection on relevance: a block built from low-similarity
            # retrievals is noise that derails scope. The metric above is unaffected.
            if _deep_result.context_block and _sem_score >= _DEEP_CONTEXT_MIN_SIM:
                _deep_context_block = _deep_result.context_block
            elif _deep_result.context_block:
                logger.info(
                    "Deep-context suppressed: sem=%.4f < floor=%.2f (low relevance).",
                    _sem_score, _DEEP_CONTEXT_MIN_SIM,
                )
            logger.info(
                "Researcher retrieval: sem=%.4f graph=%.3f css=%.1f files_parsed=%d/%d",
                _sem_score, _deep_result.coverage_ratio, _new_css,
                len(_deep_result.parsed_files), len(_deep_result.target_files),
            )
        except Exception as _ctx_err:
            logger.warning("Researcher: context extraction failed (non-fatal): %s", _ctx_err)
    # ────────────────────────────────────────────────────────────────────────────────────

    # ── Context Meter Cascade (Early Exit + Mini-Judge) ──────────────────
    _cascade_routing: str = "LOCAL_SMALL"   # conservative safe default
    _cascade_provider: str = "LOCAL"        # safe default
    _routing_warning: Optional[str] = None  # set only when hardware degrades routing

    try:
        if _fast_track:
            # Trivial query: GraphRAG was skipped, so CSS was never recomputed. Pin it
            # high by decree — a self-contained query has sufficient context — so the
            # uncomputed CSS=0 cannot trip the red-alert gate and abort the turn.
            css = 100.0
            if updated_context_metrics is not None:
                updated_context_metrics = updated_context_metrics.model_copy(
                    update={"css_total": 100.0, "is_red_alert": False}
                )

        # Initialize ContextMeter on first invocation (context_metrics absent from state).
        if updated_context_metrics is None:
            updated_context_metrics = _cold_context_meter(state, css, tci)

        if _fast_track:
            # Pre-RAG fast path: route LOCAL_SMALL and bypass the Mini-Judge LLM call
            # entirely — a trivial query should not pay a classification round-trip.
            _cascade_routing = derive_routing_decision(tci, css, fast_track=True)
            _cascade_provider = "LOCAL"
            from core.telemetry_log import log_node_transition
            log_node_transition(session_id, "researcher", "fast_track", "fast_track_pre_rag")
            logger.info(
                "Fast Track: trivial query → routing=%s (RAG skipped, Mini-Judge bypassed).",
                _cascade_routing,
            )
        elif updated_context_metrics.is_red_alert:
            # O(1) early exit: context gap → bypass Mini-Judge, force CLOUD.
            _cascade_routing = "CLOUD"
            _cascade_provider = "CLOUD"
            logger.warning(
                "RED ALERT (CSS=%.1f) — Mini-Judge bypassed, routing=CLOUD.",
                updated_context_metrics.css_total,
            )
        else:
            _risk: RiskLevel = await audit_task_complexity(user_input, session_id=session_id)
            _math_routing: str = derive_routing_decision(
                tci, updated_context_metrics.css_total, corpus_empty=_corpus_empty
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
                logger.info("MiniJudge=HIGH → TCI=100.0, routing=CLOUD.")

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
                    "MiniJudge=MEDIUM → TCI=%.1f, routing=%s.",
                    tci, _cascade_routing,
                )

            else:
                _cascade_routing = _math_routing
                _cascade_provider = "CLOUD" if _cascade_routing == "CLOUD" else "LOCAL"
                logger.info(
                    "MiniJudge=NONE → routing=%s (math defer).",
                    _cascade_routing,
                )

        updated_context_metrics = updated_context_metrics.model_copy(
            update={"routing_decision": _cascade_routing}
        )
        logger.info(
            "Cascade done — routing=%s provider=%s css=%.1f tci=%.1f",
            _cascade_routing, _cascade_provider, updated_context_metrics.css_total, tci,
        )
    except Exception as _cascade_err:
        logger.warning("cascade failed (non-fatal): %s", _cascade_err)
    # ────────────────────────────────────────────────────────────────────────────────────

    # ── Hardware-aware graceful degradation (post-cascade) ───────────────
    # A LOCAL_* decision that the host cannot run safely — VRAM below the cloud
    # floor, or a state predicted to overflow the candidate local context window —
    # is rerouted to cloud (or, with no cloud configured, degraded to LOCAL_SMALL
    # with a user-facing warning). Hardware reality overrides the cheap-path
    # preference. Non-fatal: any failure leaves the cascade decision intact.
    try:
        _hw_profile = state.get("hardware_profile")
        _overflow_risk = False
        if _cascade_routing.startswith("LOCAL"):
            _llm_profile = state.get("active_llm_profile")
            _ctx_window = int(getattr(_llm_profile, "context_window", 0) or 0)
            if _ctx_window > 0:
                _weight = estimate_graph_weight(
                    state, model_context_window=_ctx_window
                )
                _overflow_risk = _weight.overflow_risk
        _new_routing, _new_provider, _hw_warning = hardware_reroute(
            _cascade_routing,
            _cascade_provider,
            _hw_profile,
            cloud_available=check_cloud_availability(),
            overflow_risk=_overflow_risk,
        )
        if _hw_warning is not None:
            _cascade_routing, _cascade_provider = _new_routing, _new_provider
            _routing_warning = _hw_warning
            if updated_context_metrics is not None:
                updated_context_metrics = updated_context_metrics.model_copy(
                    update={"routing_decision": _cascade_routing}
                )
            from core.telemetry import log_routing_decision
            log_routing_decision(
                session_id,
                "researcher",
                _cascade_routing,
                reason="vram_floor_reroute",
                hw=(getattr(_hw_profile, "gpu_name", None) or None),
                project_id=project_id,
            )
            logger.warning("Hardware reroute: %s", _hw_warning)
    except Exception as _hw_err:
        logger.warning("hardware reroute failed (non-fatal): %s", _hw_err)
    # ────────────────────────────────────────────────────────────────────────────────────

    # ── Compose the skeleton's source material + single LLM call ──
    boundary: str = uuid.uuid4().hex
    body_parts: List[str] = list(forced_blocks)
    if _deep_context_block:
        body_parts.append(_deep_context_block)
    if grounding_block:
        body_parts.append(grounding_block)
    context_blob: str = (
        "\n\n".join(body_parts)
        if body_parts
        else f"<{boundary}>No retrieval results.</{boundary}>"
    )

    system_prompt: str = build_safe_prompt(
        agent_identity=RESEARCHER_IDENTITY,
        context_str=context_blob,
        boundary=boundary,
    )
    messages: List[Dict[str, str]] = [
        {"role": "system", "content": system_prompt},
        {
            "role": "user",
            "content": f"User request: {user_input!r}\n\n{_SKELETON_INSTRUCTION}",
        },
    ]

    skeleton: str = ""
    errors: List[str] = []
    try:
        response = await LLMGateway.ainvoke(
            messages=messages,
            model=MODEL_MEDIUM,
            temperature=0.0,
            max_tokens=2048,
            session_id=session_id,
        )
        skeleton = (response.choices[0].message.content or "").strip()
        # Hard ceiling on the buffer handed to the Planner. max_tokens above is the
        # primary bound; this is a defensive post-generation cap so a pathological or
        # future-reconfigured generation can never saturate the Planner's context.
        if len(skeleton) > _SKELETON_MAX_CHARS:
            logger.warning(
                "Researcher: skeleton exceeded ceiling (%d > %d chars) — truncating.",
                len(skeleton), _SKELETON_MAX_CHARS,
            )
            skeleton = skeleton[:_SKELETON_MAX_CHARS] + "\n…[skeleton truncated]"
        logger.info("Researcher: skeleton produced (%d chars).", len(skeleton))
    except Exception as err:  # noqa: BLE001 — surface to state.errors for retry policy
        logger.error("Researcher: LLM call failed: %s", err)
        errors.append(f"Researcher Error: {err}")

    # ── Assemble result + defensive null-guard on the routing signal ──
    if updated_context_metrics is None:
        updated_context_metrics = _cold_context_meter(state, css, tci)
    result: Dict[str, Any] = {
        "researcher_skeleton": skeleton,
        "context_metrics": updated_context_metrics,
        "css": css,
        "tci": tci,
        "provider": _cascade_provider,
        "routing_warning": _routing_warning,
    }
    if dispatch_trace:
        result["tool_dispatch_trace"] = dispatch_trace
    if errors:
        result["errors"] = errors

    # ── Flush cognitive state to .ailienant/AGENTS.md (fast-boot snapshot) ──
    try:
        from core.state_manager import dump_state_to_markdown
        _state_for_dump = dict(state) | result
        _state_for_dump["_top_k_files_cache"] = _top_k_files
        dump_state_to_markdown(_state_for_dump, workspace_root)
    except Exception as _dump_err:
        logger.debug("Researcher state dump skipped: %s", _dump_err)

    # Optionally open a dynamic-dispatch fan-out before handing off to the Planner
    # (no-op unless enabled and a plan is emitted). On emission the graph routes
    # researcher → dispatch subgraph and returns to planner_agent.
    from brain.dispatch_emitter import maybe_emit_dispatch
    result.update(await maybe_emit_dispatch(state, config, return_node="planner_agent"))

    return result
