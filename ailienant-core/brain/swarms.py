"""Phase 4.3 stage-2 — Mode-locked graph topologies (MICRO_SWARM, FULL_SWARM).

Builders are exported so :mod:`brain.intent_router` can compile or reuse the
graph it needs at dispatch time. The MICRO_SWARM graph is compiled once at
import (``_MICRO_SWARM_APP``) per the LangGraph idiom — no per-request
recompile cost. FULL_SWARM is built per-request so the caller can inject a
checkpointer (production: SQLite WAL singleton; tests: ``MemorySaver()``).

Blueprint references:
    * §2.3 MICRO_SWARM topology — CoderAgent + SyntaxGate + StyleGate, bounded
      exclusively by the Circuit Breaker (``error_streak`` channel).
    * §2.4 FULL_SWARM topology — Researcher → Planner → Orchestrator →
      micro_swarm → Analyst.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph

from brain.nodes.circuit_breaker import evaluate_circuit_breaker
from brain.state import AIlienantGraphState

# Blueprint §4.1 — bounded ceiling for the OUTER orchestrator loop. Surfaced
# here only as a documented constant; MICRO_SWARM intentionally does NOT
# consult it (retry_count is Orchestrator-scope, see _route_after_circuit_breaker).
MAX_RETRIES: int = 2


# =====================================================================
# MICRO_SWARM
# =====================================================================


def _route_after_syntax(state: Dict[str, Any]) -> str:
    if state.get("syntax_gate_status") == "pass":
        return "style_gate"
    return "circuit_breaker_check"


def _route_after_style(state: Dict[str, Any]) -> str:
    """Give-Up Gate latch wins; otherwise pass means END, fail means break-circuit."""
    if state.get("style_bypass_active"):
        return END
    if int(state.get("consecutive_style_failures", 0)) == 0:
        return END
    return "circuit_breaker_check"


async def _circuit_breaker_node(state: Dict[str, Any]) -> Dict[str, Any]:
    """Bump ``error_streak`` then evaluate the Circuit Breaker policy.

    ``error_streak`` is the SINGLE counter governing MICRO_SWARM lifecycle.
    ``retry_count`` is intentionally NOT read here — that channel belongs to
    the Orchestrator (outer task-level loop across WBS steps) and reading it
    would couple inner-loop semantics to outer-loop state.
    """
    new_streak = int(state.get("error_streak", 0)) + 1
    deltas: Dict[str, Any] = {"error_streak": new_streak}
    deltas.update(evaluate_circuit_breaker({**state, "error_streak": new_streak}))
    return deltas


def _route_after_circuit_breaker(state: Dict[str, Any]) -> str:
    """END only when the Cloud Surgeon shot has been spent; otherwise retry.

    Two arrival shapes:
      * pre-trip (streak < 3): no tier swap happened; loop back for another shot.
      * post-trip + post-Surgeon-fail: ``CLOUD_SURGEON_EXHAUSTED`` in flags → END.
      * post-trip first time: Surgeon was just swapped in; loop back once.
    """
    flags = state.get("security_flags", []) or []
    if "CLOUD_SURGEON_EXHAUSTED" in flags:
        return END
    return "coder_agent"


def build_micro_swarm() -> Any:
    """Compile the MICRO_SWARM topology. Safe to call multiple times."""
    from agents.coder import run_coder_node
    from validators.gates import style_gate_node, syntax_gate_node

    g = StateGraph(AIlienantGraphState)
    g.add_node("coder_agent", run_coder_node)              # type: ignore[type-var]
    g.add_node("syntax_gate", syntax_gate_node)            # type: ignore[type-var]
    g.add_node("style_gate", style_gate_node)              # type: ignore[type-var]
    g.add_node("circuit_breaker_check", _circuit_breaker_node)  # type: ignore[type-var]

    g.add_edge(START, "coder_agent")
    g.add_edge("coder_agent", "syntax_gate")
    g.add_conditional_edges(
        "syntax_gate", _route_after_syntax, ["style_gate", "circuit_breaker_check"]
    )
    g.add_conditional_edges(
        "style_gate", _route_after_style, [END, "circuit_breaker_check"]
    )
    g.add_conditional_edges(
        "circuit_breaker_check", _route_after_circuit_breaker, ["coder_agent", END]
    )

    return g.compile()


# Compiled once at import; FULL_SWARM embeds this as a native sub-graph node.
_MICRO_SWARM_APP = build_micro_swarm()


# =====================================================================
# FULL_SWARM
# =====================================================================


def build_full_swarm(
    checkpointer: Optional[BaseCheckpointSaver] = None,
    interrupt_before: Optional[List[str]] = None,
) -> Any:
    """Compile FULL_SWARM with a caller-supplied checkpointer.

    Production callers pass :data:`brain.checkpoint.checkpoint_manager`
    (HybridCheckpointer / SQLite WAL). Unit tests pass
    ``langgraph.checkpoint.memory.MemorySaver()`` or ``None``.

    Phase 4.5 — optional ``interrupt_before`` list is forwarded to
    ``StateGraph.compile()`` so tests (and future HITL paths) can pause the
    graph before a named node, persist via the checkpointer, and resume with
    the same ``thread_id``.

    Topology: ``verify_environment → researcher → planner → orchestrator
    → micro_swarm (sub-graph) → analyst → END``. The Coder-side middleware
    (contract_guard / finops / apply_patch / validate_output) still lives in
    the legacy ``alienant_app`` graph and is NOT duplicated here; embedding it
    in the mode-locked variant is the next-PR's job.
    """
    from agents.analyst import run_analyst_node
    from agents.orchestrator import run_orchestrator_node
    from agents.planner import run_planner_node
    from agents.researcher import run_researcher_node
    from validators.environment import verify_environment_node

    g = StateGraph(AIlienantGraphState)
    g.add_node("verify_environment", verify_environment_node)  # type: ignore[type-var]
    g.add_node("researcher_agent", run_researcher_node)        # type: ignore[type-var]
    g.add_node("planner_agent", run_planner_node)              # type: ignore[type-var]
    g.add_node("orchestrator_agent", run_orchestrator_node)    # type: ignore[type-var]
    # Pass the compiled MICRO_SWARM directly as a sub-graph node. LangGraph
    # natively routes state between parent and child without re-applying the
    # parent's reducers on the returned dict — this avoids the O(2^N)
    # ``messages`` duplication that a wrapper function returning
    # ``await _APP.ainvoke(state)`` would trigger (the parent's operator.add
    # reducer would re-append the entire child history on every iteration).
    g.add_node("micro_swarm", _MICRO_SWARM_APP)                # type: ignore[arg-type]
    g.add_node("analyst_agent", run_analyst_node)              # type: ignore[type-var]

    g.add_edge(START, "verify_environment")
    g.add_edge("verify_environment", "researcher_agent")
    g.add_edge("researcher_agent", "planner_agent")
    g.add_edge("planner_agent", "orchestrator_agent")
    g.add_edge("orchestrator_agent", "micro_swarm")
    g.add_edge("micro_swarm", "analyst_agent")
    g.add_edge("analyst_agent", END)

    compile_kwargs: Dict[str, Any] = {}
    if checkpointer is not None:
        compile_kwargs["checkpointer"] = checkpointer
    if interrupt_before is not None:
        compile_kwargs["interrupt_before"] = interrupt_before
    return g.compile(**compile_kwargs)
