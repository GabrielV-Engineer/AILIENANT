"""Native LangGraph Suspend & Resume HITL substrate.

In-graph human approvals use LangGraph's ``interrupt()`` / ``Command(resume=…)`` so an
awaiting approval *checkpoints the graph and frees the runtime* instead of pinning a
coroutine on an ``asyncio.Event`` until a wall-clock deadline.

Resume correlation is LangGraph-native: a ``Command(resume=…)`` is matched to the
pending interrupt by the interrupt's deterministic position within the task — never by
any id this module emits. The ``approval_id`` in the payload is therefore *cosmetic*: it
correlates the frontend approval card and the inbound ``client_hitl_response`` only, so
no float/index non-determinism on a node replay can orphan a resume.

This substrate is for HITL that runs *inside a graph node*. Non-graph HITL (the MCP
adapter, the post-graph file-write apply loop) stays on the ``request_human_approval``
event channel — ``interrupt()`` only works within a running graph execution.
"""
from __future__ import annotations

import logging
import uuid
from typing import Any, Dict, List, Optional

from langchain_core.runnables import RunnableConfig
from langgraph.types import interrupt

logger = logging.getLogger("HITL")


def request_graph_approval(
    *,
    session_id: str,
    action_description: str,
    proposed_content: Optional[str] = None,
    request_kind: str,
    proposed_files: Optional[List[Any]] = None,
    risk_patterns_matched: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Suspend the running graph for a human approval via native ``interrupt()``.

    First execution raises ``GraphInterrupt`` (the LangGraph engine catches it,
    checkpoints, and pauses the run — the ``astream`` generator ends naturally). On
    resume the same call returns the ``Command(resume=…)`` value, normalized to
    ``{"approved": bool, "comment": str | None, "modified_content": str | None}``.

    Must be called from within a graph node during execution (``interrupt()`` reads the
    active run context); calling it outside a graph run raises.
    """
    payload: Dict[str, Any] = {
        "session_id": session_id,
        "approval_id": uuid.uuid4().hex,  # cosmetic — card correlation only, not resume-matching
        "action_description": action_description,
        "proposed_content": proposed_content,
        "request_kind": request_kind,
        "proposed_files": proposed_files,
        "risk_patterns_matched": risk_patterns_matched,
    }
    resumed: Any = interrupt(payload)
    if isinstance(resumed, dict):
        return {
            "approved": bool(resumed.get("approved")),
            "comment": resumed.get("comment"),
            # Edit-before-apply from the HITL card's edit mode. Must be forwarded
            # here too — normalizing it away is exactly as destructive as dropping
            # it upstream in main.py::_resume_approval_dict; both links in the
            # chain have to carry it or a FILE_WRITE edit silently reverts to the
            # original proposed content.
            "modified_content": resumed.get("modified_content"),
        }
    # A bare truthy/falsey resume value is tolerated (fail-safe: unknown → not approved).
    return {"approved": bool(resumed), "comment": None, "modified_content": None}


def request_graph_clarification(
    *,
    session_id: str,
    question: Optional[str] = None,
    context: Optional[str] = None,
    suggested_options: Optional[List[str]] = None,
    questions: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """Suspend the running graph to ask the human a clarifying question.

    Same native-``interrupt()`` substrate as :func:`request_graph_approval`, but
    shaped for a question/answer exchange instead of a binary approval: the
    frontend renders ``question``/``context``/``suggested_options`` (single-question
    shape), or ``questions`` (the multi-question batch extension, DEBT-172) — the
    two are independent and either may be populated. The resumed run gets back
    whatever free-text answer or picked option the human supplied, normalized to
    ``{"answer": str | None, "selected_option": str | None, "answers": list | None}``.

    Must be called from within a graph node during execution (``interrupt()`` reads
    the active run context); calling it outside a graph run raises.
    """
    payload: Dict[str, Any] = {
        "session_id": session_id,
        "request_id": uuid.uuid4().hex,  # cosmetic — card correlation only, not resume-matching
        "request_kind": "CLARIFICATION_NEEDED",
        "question": question,
        "context": context,
        "suggested_options": list(suggested_options) if suggested_options else [],
    }
    if questions:
        payload["questions"] = questions
    resumed: Any = interrupt(payload)
    if isinstance(resumed, dict):
        return {
            "answer": resumed.get("answer"),
            "selected_option": resumed.get("selected_option"),
            "answers": resumed.get("answers"),
        }
    if isinstance(resumed, str):
        return {"answer": resumed, "selected_option": None, "answers": None}
    return {"answer": None, "selected_option": None, "answers": None}


def _collect_interrupts(snapshot: Any) -> List[Any]:
    """Gather pending ``Interrupt`` objects from a ``StateSnapshot`` (top-level + per-task)."""
    found: List[Any] = []
    top = getattr(snapshot, "interrupts", None)
    if top:
        found.extend(list(top))
    for task in getattr(snapshot, "tasks", None) or []:
        task_interrupts = getattr(task, "interrupts", None)
        if task_interrupts:
            found.extend(list(task_interrupts))
    return found


async def extract_pending_interrupt(cfg: RunnableConfig) -> Optional[Dict[str, Any]]:
    """Return the pending interrupt payload for a thread, or ``None`` if not paused.

    Reads via the **async** state accessor (``aget_state``); the interrupt value is the
    dict ``request_graph_approval`` handed to ``interrupt()``. Deferred import of the
    compiled graph avoids an import cycle at module load.
    """
    from brain.engine import alienant_app  # deferred — avoids import cycle

    snapshot = await alienant_app.aget_state(cfg)
    interrupts = _collect_interrupts(snapshot)
    if not interrupts:
        return None
    value = getattr(interrupts[0], "value", None)
    return value if isinstance(value, dict) else {"value": value}
