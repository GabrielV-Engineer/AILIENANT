"""Glass-Box Timeline activity channel — the un-throttled, ordered event stream.

Covers the two new backend surfaces: the pure label→kind classifier that feeds the
channel from the single `_narrate` choke point, and the `broadcast_activity_event`
emitter (a typed `server_activity_event`, distinct from the throttled pipeline step).
"""
from __future__ import annotations

from typing import Any, List, Optional, Tuple
from unittest.mock import AsyncMock, patch

import pytest

from core.task_service import _classify_activity

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


# --------------------------------------------------------------------------- #
# _classify_activity — raw node label → (kind, target, metric)
# --------------------------------------------------------------------------- #


def test_classify_free_text_action_verbs() -> None:
    assert _classify_activity("reading fibonacci.py") == ("read", "fibonacci.py", None)
    assert _classify_activity("editing app.py") == ("edit", "app.py", None)
    assert _classify_activity("writing gui.py") == ("edit", "gui.py", None)
    assert _classify_activity("running pytest -q") == ("command", "pytest -q", None)
    assert _classify_activity("verified mypy .") == ("command", "mypy .", None)
    assert _classify_activity("self-healing coder_agent") == ("heal", "coder_agent", None)
    assert _classify_activity("recovered coder_agent") == ("heal", "coder_agent", None)


def test_classify_phase_tokens() -> None:
    assert _classify_activity("context_gather") == ("understanding", None, None)
    assert _classify_activity("synthesizing_intent") == ("understanding", None, None)
    assert _classify_activity("drafting_spec") == ("planning", None, None)
    assert _classify_activity("handoff_to_planner") == ("planning", None, None)
    assert _classify_activity("critic_review") == ("reviewing", None, None)
    assert _classify_activity("plan_validated") == ("reviewing", None, None)
    assert _classify_activity("critic_rejected → replanning (1/3)") == (
        "reviewing", None, "replanning",
    )


def test_classify_unknown_returns_none() -> None:
    # A label with no timeline equivalent flows only to the legacy pipeline-step channel.
    assert _classify_activity("some_new_internal_token") == (None, None, None)
    assert _classify_activity("") == (None, None, None)


def test_classify_kinds_are_all_in_the_contract_enum() -> None:
    # Every kind the classifier can produce must be a member of ActivityKind, or the
    # payload would fail contract validation at the edge (no raw token ever escapes).
    from api.ws_contracts import ActivityEventPayload

    import typing
    allowed = set(typing.get_args(ActivityEventPayload.model_fields["kind"].annotation))
    labels = [
        "reading x", "editing x", "writing x", "running x", "verified x",
        "giving up on x after 3 attempts", "self-healing x", "recovered x",
        "could not auto-fix x", "retrieving context",
        "context_gather", "synthesizing_intent", "handoff_to_planner",
        "drafting_spec", "critic_review", "unwrapping_schema", "plan_validated",
        "plan_budget_overage_advisory", "critic_rejected → replanning (1/3)",
    ]
    for lbl in labels:
        kind, _, _ = _classify_activity(lbl)
        assert kind is None or kind in allowed, f"{lbl!r} → {kind!r} not in enum"


# --------------------------------------------------------------------------- #
# broadcast_activity_event — a typed server_activity_event, un-gated
# --------------------------------------------------------------------------- #


async def test_broadcast_activity_event_shape() -> None:
    from api.websocket_manager import vfs_manager
    from api.ws_contracts import ServerActivityEvent

    sent: List[Any] = []

    async def _capture(client_id: str, event: Any) -> None:
        sent.append(event)

    with patch.object(vfs_manager, "send_personal_message", new=AsyncMock(side_effect=_capture)):
        await vfs_manager.broadcast_activity_event(
            "sess-1", seq=0, ts=1.0, kind="read", target="fibonacci.py",
        )

    assert len(sent) == 1
    ev = sent[0]
    assert isinstance(ev, ServerActivityEvent)
    assert ev.event_type == "server_activity_event"
    assert ev.data.seq == 0
    assert ev.data.kind == "read"
    assert ev.data.target == "fibonacci.py"
    assert ev.data.session_id == "sess-1"
