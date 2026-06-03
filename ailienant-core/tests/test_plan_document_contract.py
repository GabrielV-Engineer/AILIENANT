"""Rich Plan side-panel contract (ADR-732).

A finalized plan reaches the IDE as a single structured ``server_plan_document``
message — the full MissionSpecification plus a one-line chat pointer — instead of
a markdown-flattened bubble that dropped every field but ``outcome``. These tests
pin three properties:

  1. A MissionSpecification projects cleanly onto ``PlanDocumentPayload`` (no
     structure lost: scope / constraints / decisions / WBS / checks survive).
  2. ``ServerPlanDocumentEvent`` round-trips through the same ``TypeAdapter`` the
     websocket manager uses to serialize every outbound message.
  3. The chat summary is now a *pointer* — it no longer embeds the raw WBS/diffs
     (those live in the panel), so it stays small regardless of plan size.
"""
from __future__ import annotations

from api.ws_contracts import PlanDocumentPayload, ServerPlanDocumentEvent
from api.websocket_manager import ws_adapter
from brain.state import MissionSpecification, WBSStep
from core.task_service import TaskService


def _mission(n_steps: int = 3) -> MissionSpecification:
    return MissionSpecification(
        outcome="Ship the rich plan surface.",
        scope=["src/app.py", "src/util.py"],
        constraints=["No external deps."],
        decisions=["Reuse the existing webview."],
        tasks=[
            WBSStep(
                step_number=i,
                target_role="core_dev",
                action="edit_file",
                target_file=f"src/module_{i}.py",
                description=f"Implement step {i}.",
                status="pending",
            )
            for i in range(1, n_steps + 1)
        ],
        checks=["Pytest exits 0."],
        ubiquitous_language={"plan": "a finalized MissionSpecification"},
    )


def test_mission_projects_onto_payload_without_losing_structure() -> None:
    payload = TaskService._build_plan_payload(_mission(), summary="pointer")

    assert payload.summary == "pointer"
    assert payload.outcome == "Ship the rich plan surface."
    assert payload.scope == ["src/app.py", "src/util.py"]
    assert payload.constraints == ["No external deps."]
    assert payload.decisions == ["Reuse the existing webview."]
    assert payload.checks == ["Pytest exits 0."]
    assert payload.ubiquitous_language == {"plan": "a finalized MissionSpecification"}
    # The WBS survives as serialized rows (the panel renders step/role/action/file).
    assert len(payload.tasks) == 3
    assert payload.tasks[0]["target_file"] == "src/module_1.py"
    assert payload.tasks[0]["action"] == "edit_file"


def test_event_round_trips_through_the_ws_adapter() -> None:
    event = ServerPlanDocumentEvent(
        data=TaskService._build_plan_payload(_mission(), summary="see the Plan panel")
    )

    raw = ws_adapter.dump_json(event)
    restored = ws_adapter.validate_json(raw)

    assert isinstance(restored, ServerPlanDocumentEvent)
    assert restored.event_type == "server_plan_document"
    assert restored.data.summary == "see the Plan panel"
    assert len(restored.data.tasks) == 3


def test_summary_is_a_pointer_not_an_embedded_wbs() -> None:
    # A large plan must not bloat the chat bubble — the WBS/diffs live in the panel.
    mission = _mission(n_steps=50)
    patches = {f"src/module_{i}.py": f"@@ diff {i} @@" for i in range(1, 51)}

    summary = TaskService._format_coding_summary(mission, patches, errors=[])

    assert "Plan panel" in summary
    assert "```diff" not in summary  # diffs are not re-flattened into chat prose
    assert "@@ diff" not in summary
    # Pointer stays short regardless of a 50-step plan.
    assert len(summary) < 200


def test_empty_plan_projects_with_safe_defaults() -> None:
    # Ideation-stub plans can carry an empty WBS — the payload must not choke.
    mission = MissionSpecification(
        outcome="",
        scope=[],
        constraints=[],
        decisions=[],
        tasks=[],
        checks=[],
    )
    payload = TaskService._build_plan_payload(mission, summary="empty")

    assert payload.tasks == []
    assert payload.outcome == ""
    assert payload.ubiquitous_language == {}
