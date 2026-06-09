"""Concurrent-write reducers on the shared graph state.

Under SWARM fan-out the MapReduce router sends one CoderAgent per WBS task in a
single super-step. Each writes back the scalar fields of the step it handled. A
scalar channel with no reducer cannot merge those concurrent writes and LangGraph
raises INVALID_CONCURRENT_GRAPH_UPDATE ("can receive only one value per step").

These tests pin the ``target_role`` reducer's merge semantics AND that the channel
is actually annotated with it — the wiring is what prevents the crash, so both must
hold together.
"""
from __future__ import annotations

from typing import Annotated, get_args, get_origin, get_type_hints

from brain.state import AIlienantGraphState, _resolve_target_role


def test_resolve_target_role_keeps_latest_non_none() -> None:
    """Two concurrent real roles merge to the latest (right-biased), no raise."""
    assert _resolve_target_role("core_dev", "secops") == "secops"


def test_resolve_target_role_none_never_clobbers() -> None:
    """None is 'no opinion' — a concurrent reset must not erase a real role."""
    assert _resolve_target_role("core_dev", None) == "core_dev"
    assert _resolve_target_role(None, "qa_tester") == "qa_tester"
    assert _resolve_target_role(None, None) is None


def test_target_role_channel_is_reducer_annotated() -> None:
    """The state channel must carry the reducer, or fan-out writes raise."""
    hints = get_type_hints(AIlienantGraphState, include_extras=True)
    target_role = hints["target_role"]
    assert get_origin(target_role) is Annotated
    assert _resolve_target_role in get_args(target_role)
