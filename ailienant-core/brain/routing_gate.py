# ailienant-core/brain/routing_gate.py
#
# 13.1.10 — Reviewable Model Route.
#
# The router (agents/researcher.py's CSS/TCI cascade) already picks a model
# tier for the turn before this node runs; the Glass-Box Timeline's lane badge
# (13.1.9) already shows what it picked. This node is what lets the operator
# actually CONFIRM or override that pick, once per turn, before the planner
# drafts — rather than only ever finding out after the fact.
#
# Spliced between `researcher_agent` and `planner_agent` in every topology
# (brain/engine.py): every path to the planner already runs through the
# researcher first, so this is the single choke point that sees a computed
# `context_metrics.routing_decision` no matter which route got here.

from __future__ import annotations

import json
import logging
from typing import Any, Dict, Optional

from langchain_core.runnables import RunnableConfig
from langgraph.graph import END

from core.permissions import SessionPermissionMode, session_mode_from_channel

logger = logging.getLogger("ROUTING_GATE")

MODEL_ROUTE_REVIEW_KIND: str = "MODEL_ROUTE_REVIEW"

# The router's own decision, keyed on the same vocabulary
# core/memory/context_auditor.py::derive_routing_decision returns and
# resolve_model_alias_for_routing consumes. Kept here as the fallback only —
# never restated as a second ladder (§5.7); the real ladder lives in
# resolve_model_alias_for_routing's own `mapping` dict.
_DEFAULT_ROUTING_DECISION: str = "LOCAL_BIG"  # mirrors resolve_model_alias_for_routing's own MODEL_BIG default

# The 3-button frontend UI's Ask/Plan modes are the only ones this gate
# suspends for; every other session mode — Auto (STANDARD) foremost, but also
# any mode not currently reachable from the UI — bypasses it and simply
# writes the router's own pick. See core/permissions.py's
# _FRONTEND_MODE_TO_SESSION for the full frontend->mode mapping; revisit this
# set if a future mode becomes reachable from the UI and should also gate.
_GATE_MODES = frozenset({SessionPermissionMode.CAUTIOUS, SessionPermissionMode.PLAN_ONLY})


async def _resolve_route_review(
    task_id: str,
    routing_decision: str,
    tci: Optional[float],
    css: Optional[float],
    config: Optional[RunnableConfig],
) -> Dict[str, Any]:
    """Suspend for the operator's decision on the router's pick.

    Mirrors ``brain/ideation.py::_resolve_brief_review``: an injected
    ``model_route_review_fn`` seam so the node stays unit-testable outside a
    live graph run, else the real suspend on the approval channel every other
    HITL card already uses. The structured TCI/CSS/decision triple rides
    ``proposed_content`` as JSON — no new wire field, `request_graph_approval`
    already carries exactly the accept/override/cancel shape this needs.
    """
    review_fn = (config or {}).get("configurable", {}).get("model_route_review_fn")
    if review_fn is not None:
        return dict(await review_fn(routing_decision, tci, css) or {})
    from core.hitl import request_graph_approval  # deferred — avoids import cycle

    return request_graph_approval(
        session_id=task_id,
        action_description=(
            "Routing selected a model for this turn. Accept it, or pick a "
            "different tier from the active preset."
        ),
        proposed_content=json.dumps({
            "routing_decision": routing_decision, "tci": tci, "css": css,
        }),
        request_kind=MODEL_ROUTE_REVIEW_KIND,
    )


async def run_model_route_node(
    state: Dict[str, Any], config: Optional[RunnableConfig] = None,
) -> Dict[str, Any]:
    """LangGraph node: confirm or override the router's model pick for this turn.

    Single-phase, unlike `run_synthesis_node`'s defer-then-interrupt-first
    split (brain/ideation.py) — that split exists because drafting the brief
    is an expensive, non-deterministic MODEL_BIG call that must not silently
    re-run on every resume. Resolving a routing decision here is a pure
    re-read of `context_metrics`, already sitting in state: replaying this
    node from the top on resume reproduces the exact same value every time,
    so there is no hazard in resolving and interrupting within one
    invocation.

    AUTO (and any session mode outside `_GATE_MODES`) never suspends: it
    writes `confirmed_routing_decision` from the router's own pick and
    returns immediately — the Glass-Box Timeline lane badge (13.1.9) remains
    the only surface for the model choice in that mode, consistent with what
    AUTO is for.
    """
    session_mode = session_mode_from_channel(state.get("session_permission_mode"))
    context_meter = state.get("context_metrics")
    routing_decision = str(
        getattr(context_meter, "routing_decision", None) or _DEFAULT_ROUTING_DECISION
    )
    tci = getattr(context_meter, "task_complexity_index", None)
    css = getattr(context_meter, "css_total", None)
    task_id = str(state.get("task_id", ""))

    if session_mode not in _GATE_MODES:
        logger.info(
            "run_model_route_node: session_mode=%s bypasses review — confirming %s.",
            session_mode.value, routing_decision,
        )
        return {"confirmed_routing_decision": routing_decision}

    decision = await _resolve_route_review(task_id, routing_decision, tci, css, config)
    if not decision.get("approved"):
        logger.info("run_model_route_node: route review cancelled — ending the turn.")
        return {"confirmed_routing_decision": None, "hitl_pending": True}

    override = decision.get("modified_content")
    final_decision = str(override).strip() if override and str(override).strip() else routing_decision
    logger.info(
        "run_model_route_node: route confirmed (%s%s).",
        final_decision, " — overridden" if final_decision != routing_decision else "",
    )
    return {"confirmed_routing_decision": final_decision, "hitl_pending": False}


def route_after_model_route(state: Dict[str, Any]) -> str:
    """hitl_pending=True (the operator cancelled) -> END; otherwise -> planner_agent."""
    if state.get("hitl_pending"):
        logger.info("route_after_model_route: hitl_pending → END.")
        return END
    return "planner_agent"
