# ailienant-core/tests/test_apply_gate_prereqs.py
#
# 13.0.9 W0 — the six prerequisite fixes for in-graph incremental approval.
# Each of these is a silent-failure class: nothing raises, nothing logs loudly,
# the feature just quietly does the wrong thing. Regression-guard each one
# directly rather than relying on the larger apply-gate feature to exercise it.

from typing import Any, Dict, List
from unittest.mock import patch

import pytest
from langgraph.errors import GraphInterrupt

pytestmark = pytest.mark.anyio


# ─── 1 — reflexion_guard must let a pause propagate, never convert it to a heal ──


async def test_reflexion_guard_lets_graph_interrupt_propagate() -> None:
    """A GraphInterrupt raised inside a reflexion-wrapped node (coder_agent today,
    apply_commit tomorrow) must reach LangGraph's own suspend/resume machinery
    untouched — not be swallowed into a healing_required delta, which would
    silently destroy the pause and route to self-healing instead of asking the
    user for approval."""
    from brain.engine import reflexion_guard

    async def _raises_interrupt(state: Dict[str, Any]) -> Dict[str, Any]:
        raise GraphInterrupt()

    wrapped = reflexion_guard("coder_agent")(_raises_interrupt)

    with pytest.raises(GraphInterrupt):
        await wrapped({"task_id": "t1", "correction_attempts": 0})


async def test_reflexion_guard_still_traps_a_real_exception() -> None:
    """Non-interrupt exceptions must still convert to a healing signal — the fix
    must not widen into swallowing GraphInterrupt's handling for everything."""
    from brain.engine import reflexion_guard

    async def _raises_value_error(state: Dict[str, Any]) -> Dict[str, Any]:
        raise ValueError("boom")

    wrapped = reflexion_guard("coder_agent")(_raises_value_error)

    result = await wrapped({"task_id": "t1", "correction_attempts": 0})
    assert result["healing_required"] is True
    assert result["failed_node"] == "coder_agent"


# ─── 2 — dead_letter_decorator must not DLQ a pause ──────────────────────────


async def test_dead_letter_decorator_lets_graph_interrupt_propagate() -> None:
    """A HITL pause is not a crash — it must reach LangGraph untouched, and must
    NOT write a dead_letter_tasks row or force-promote L1->L2 on every single
    interrupt (pre-existing agentic_cell pollution this also fixes)."""
    from core.dead_letter import dead_letter_decorator

    async def _raises_interrupt(state: Dict[str, Any]) -> Dict[str, Any]:
        raise GraphInterrupt()

    wrapped = dead_letter_decorator("apply_commit")(_raises_interrupt)

    with patch("core.dead_letter.save_dead_letter") as mock_save, \
         patch("brain.checkpoint.checkpoint_manager.promote") as mock_promote:
        with pytest.raises(GraphInterrupt):
            await wrapped({"task_id": "t1"})
        mock_save.assert_not_called()
        mock_promote.assert_not_called()


async def test_dead_letter_decorator_still_dlqs_a_real_exception() -> None:
    from core.dead_letter import dead_letter_decorator

    async def _raises_value_error(state: Dict[str, Any]) -> Dict[str, Any]:
        raise ValueError("boom")

    wrapped = dead_letter_decorator("apply_commit")(_raises_value_error)

    with patch("core.dead_letter.save_dead_letter") as mock_save:
        with pytest.raises(ValueError):
            await wrapped({"task_id": "t1"})
        mock_save.assert_called_once()


# ─── 3 — modified_content must survive both hops: main.py and core/hitl.py ───


def test_resume_approval_dict_forwards_modified_content() -> None:
    from api.ws_contracts import HITLResponsePayload
    from main import _resume_approval_dict

    data = HITLResponsePayload(
        approval_id="appr-1", approved=True,
        modified_content="edited body",
    )
    result = _resume_approval_dict(data)
    assert result["modified_content"] == "edited body"


def test_resume_approval_dict_modified_content_defaults_none() -> None:
    from api.ws_contracts import HITLResponsePayload
    from main import _resume_approval_dict

    data = HITLResponsePayload(approval_id="appr-1", approved=True)
    result = _resume_approval_dict(data)
    assert result["modified_content"] is None


def test_request_graph_approval_normalizes_modified_content() -> None:
    """The resume value's modified_content must survive request_graph_approval's
    own normalization — fixing only main.py::_resume_approval_dict is not
    sufficient, since this second hop drops it independently."""
    from core.hitl import request_graph_approval

    with patch(
        "core.hitl.interrupt",
        return_value={"approved": True, "comment": None, "modified_content": "edited body"},
    ):
        result = request_graph_approval(
            session_id="s1", action_description="Apply?", request_kind="FILE_WRITE",
        )
    assert result == {
        "approved": True, "comment": None, "modified_content": "edited body",
    }


def test_request_graph_approval_bare_resume_value_still_has_modified_content_key() -> None:
    """A bare truthy/falsy resume value (fail-safe path) must still produce the
    modified_content key so a caller can uniformly do resp.get('modified_content')
    without a KeyError depending on which resume shape arrived."""
    from core.hitl import request_graph_approval

    with patch("core.hitl.interrupt", return_value=True):
        result = request_graph_approval(
            session_id="s1", action_description="Apply?", request_kind="FILE_WRITE",
        )
    assert result == {"approved": True, "comment": None, "modified_content": None}


# ─── 4 — _emit_interrupt_card must forward proposed_files ────────────────────


async def test_emit_interrupt_card_forwards_proposed_files() -> None:
    """Was hardcoded proposed_files=None — the only path a native interrupt()
    reaches the frontend by, so every FILE_WRITE interrupt rendered an approval
    card with no diff at all."""
    from core.task_service import TaskService

    ts = TaskService()  # type: ignore[no-untyped-call]
    sent: List[Any] = []

    async def _capture(session_id: str, event: Any) -> None:
        sent.append(event)

    proposed = [
        {"file_path": "a.py", "unified_diff": "@@ -1 +1 @@\n-old\n+new", "base_hash": "abc123"},
    ]
    with patch("core.task_service.vfs_manager.send_personal_message", new=_capture):
        await ts._emit_interrupt_card(
            "sess-1",
            {
                "session_id": "sess-1",
                "approval_id": "appr-1",
                "action_description": "Apply change to a.py",
                "request_kind": "FILE_WRITE",
                "proposed_files": proposed,
            },
        )

    data = sent[0].data
    assert data.proposed_files is not None
    assert len(data.proposed_files) == 1
    assert data.proposed_files[0].file_path == "a.py"
    assert data.proposed_files[0].unified_diff == "@@ -1 +1 @@\n-old\n+new"
    assert data.proposed_files[0].base_hash == "abc123"


async def test_emit_interrupt_card_proposed_files_none_when_absent() -> None:
    """A non-FILE_WRITE interrupt (e.g. a clarification) must still degrade to
    None, not raise, when the payload never carried proposed_files."""
    from core.task_service import TaskService

    ts = TaskService()  # type: ignore[no-untyped-call]
    sent: List[Any] = []

    async def _capture(session_id: str, event: Any) -> None:
        sent.append(event)

    with patch("core.task_service.vfs_manager.send_personal_message", new=_capture):
        await ts._emit_interrupt_card(
            "sess-1",
            {
                "session_id": "sess-1",
                "approval_id": "appr-1",
                "action_description": "Approve budget overage?",
                "request_kind": "BUDGET_OVERFLOW",
            },
        )

    assert sent[0].data.proposed_files is None


# ─── 5 — recursion_limit is set explicitly, not left to LangGraph's default ──


def test_graph_recursion_limit_constant_is_generous_and_env_overridable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import importlib
    import shared.config as config_mod

    # Default, no env override.
    monkeypatch.delenv("AILIENANT_GRAPH_RECURSION_LIMIT", raising=False)
    importlib.reload(config_mod)
    assert config_mod.GRAPH_RECURSION_LIMIT >= 100

    # Env override respected.
    monkeypatch.setenv("AILIENANT_GRAPH_RECURSION_LIMIT", "300")
    importlib.reload(config_mod)
    assert config_mod.GRAPH_RECURSION_LIMIT == 300

    # A malformed override never wedges the ceiling below LangGraph's own default.
    monkeypatch.setenv("AILIENANT_GRAPH_RECURSION_LIMIT", "1")
    importlib.reload(config_mod)
    assert config_mod.GRAPH_RECURSION_LIMIT == 25

    # Restore real state for any test that imports the module after this one.
    monkeypatch.delenv("AILIENANT_GRAPH_RECURSION_LIMIT", raising=False)
    importlib.reload(config_mod)


def test_task_service_run_config_carries_the_recursion_limit() -> None:
    """Guard against the wiring silently regressing back to an implicit default —
    grep the source for the actual key rather than driving a full graph run."""
    import inspect
    import core.task_service as ts_mod

    src = inspect.getsource(ts_mod)
    assert '"recursion_limit": GRAPH_RECURSION_LIMIT' in src, (
        "cfg passed to alienant_app.astream() must set recursion_limit explicitly"
    )
