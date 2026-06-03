# tests/test_phase7_15_checkpoint_gate.py
"""Agentic Core Remediation — backend Checkpoint Gate.

Single E2E certification that the corrective backend pillars hold together
against their **shipped** entry points. Test-only: it imports and invokes
production code, asserting the one load-bearing invariant per gate row — it does
not re-run the dedicated suites, and it modifies no production logic. Mirrors the
sibling ``test_phase7_13_checkpoint_gate.py`` gate.

Async cases run via ``asyncio.run`` (no anyio-backend dependency).

Gate rows certified here (backend-assertable):
  RS1  live code path runs the compiled graph    RS2  planner toggle steers routing
  RS3  toggle registry is a real seam            RB1  RBAC matrix gates the write edge
  RB2  frontend mode → session policy            EX1  execute-tier gate (PLAN/DEFAULT/AUTO)
  EX2  run_command is honest (failed+deferred)   I18N1 language mirror reaches both prompts
  HON1 summary never claims apply disabled       OBS1 narration rides the isolation seam
  RP1  plan reaches the webview as structure

Frontend-only rows are out of pytest scope — certified by ``npm run compile`` + a
manual smoke (per the 7.13/7.14 §5.2 precedent): the host forwarding
``execution_mode`` and the ``OPEN_FILE`` → ``showTextDocument`` try/catch handler
in ``workspace_panel.ts``, and the ``PlanPanel.tsx`` render. Their backend
contract is covered by RP1 (the ``ServerPlanDocumentEvent`` shape round-trips).
REG (full pytest + mypy + npm run compile) is the suite-level DoD, not a case.

Manual smoke (frontend rows): Plan mode → an edit attempt is refused read-only;
Ask → HITL card; Auto → "⚡ Auto-applying…". A finalized plan renders the docked
Plan panel and the pointer bubble together (no flicker); clicking a target_file
that does not exist yet → a warning toast, host stays alive; an existing one opens
in the editor.
"""
from __future__ import annotations

import ast
import asyncio
from pathlib import Path
from typing import Any, Dict, List, Optional

import main
from agents.roles import LANGUAGE_MIRROR_DIRECTIVE, build_coder_system_prompt
from api.websocket_manager import ws_adapter
from api.ws_contracts import ServerPlanDocumentEvent
from brain.engine import route_after_summarize
from brain.state import MissionSpecification, WBSStep
from core.permissions import (
    PermissionDecision,
    SessionPermissionMode,
    ToolPrivilegeTier,
    evaluate_action,
    gate_execute_action,
    session_mode_from_frontend,
)
from core.task_service import TaskService
from shared.rbac import PermissionMode

_PKG_ROOT = Path(__file__).resolve().parent.parent
_CODER = PermissionMode.EDIT_EXECUTE_RBW


# ── RS1 — the live coding path drives the compiled graph, not direct nodes ────


def test_rs1_coding_path_runs_compiled_graph_not_direct_nodes() -> None:
    # The re-spine routes _run_coding_task through alienant_app.astream so the
    # mode router, ideation loop and checkpointer all engage. A regression to
    # calling the node functions directly would silently kill all four at once.
    src = (_PKG_ROOT / "core" / "task_service.py").read_text(encoding="utf-8")
    assert "alienant_app.astream(" in src      # the engine, not a shortcut

    tree = ast.parse(src)
    direct_calls = {
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id in {"run_coder_node", "run_planner_node"}
    }
    assert not direct_calls   # the bypass that disabled routing/checkpoints is gone


# ── RS2 — the planner toggle steers the post-summarize route ──────────────────


def test_rs2_planner_toggle_steers_routing() -> None:
    assert route_after_summarize({"planner_mode_active": True}) == "ideation_loop"
    assert route_after_summarize({"planner_mode_active": False}) == "planner_agent"
    # Absent flag must fall to autonomous planning, never the interactive loop.
    assert route_after_summarize({}) == "planner_agent"


# ── RS3 — the toggle registry is a real module-level seam ─────────────────────


def test_rs3_planner_registry_is_a_live_seam() -> None:
    assert isinstance(main.planner_mode_registry, dict)


# ── RB1 — the RBAC matrix gates the write edge correctly ──────────────────────


def test_rb1_rbac_matrix_gates_the_write_edge() -> None:
    write = ToolPrivilegeTier.WRITE
    assert evaluate_action(SessionPermissionMode.PLAN, write, _CODER) is PermissionDecision.DENY
    assert evaluate_action(SessionPermissionMode.DEFAULT, write, _CODER) is PermissionDecision.HITL
    assert evaluate_action(SessionPermissionMode.AUTO, write, _CODER) is PermissionDecision.ALLOW
    # A read is always free regardless of mode (the floor the gate never blocks).
    assert (
        evaluate_action(SessionPermissionMode.PLAN, ToolPrivilegeTier.READ_ONLY, _CODER)
        is PermissionDecision.ALLOW
    )


# ── RB2 — the frontend selector maps onto the session policy ──────────────────


def test_rb2_frontend_mode_maps_to_session_policy() -> None:
    assert session_mode_from_frontend("automatic") is SessionPermissionMode.AUTO
    assert session_mode_from_frontend("ask_before_edits") is SessionPermissionMode.DEFAULT
    assert session_mode_from_frontend("plan_mode") is SessionPermissionMode.PLAN
    # Unknown → None so the caller falls back to the settings seed, never a guess.
    assert session_mode_from_frontend("bogus") is None


# ── EX1 — the execute-tier choke point denies in Plan, cards in Ask ───────────


def test_ex1_execute_tier_gate_decisions() -> None:
    assert gate_execute_action(SessionPermissionMode.PLAN) is PermissionDecision.DENY
    assert gate_execute_action(SessionPermissionMode.DEFAULT) is PermissionDecision.HITL
    assert gate_execute_action(SessionPermissionMode.AUTO) is PermissionDecision.ALLOW


# ── EX2 — a run_command step is honest: failed-and-deferred, never a lie ───────


def test_ex2_run_command_is_failed_not_falsely_completed() -> None:
    # The coder has no live execute edge; marking run_command "completed" would
    # lie that a command ran. Guard the honest path: failed + EXECUTE_TIER_DEFERRED.
    src = (_PKG_ROOT / "agents" / "coder.py").read_text(encoding="utf-8")
    assert 'EXECUTE_TIER_DEFERRED' in src
    assert '"failed"' in src   # the deferred branch surfaces a failed status


# ── I18N1 — the language-mirror directive reaches the coder prompt skeleton ───


def test_i18n1_language_mirror_reaches_the_coder_prompt() -> None:
    # The defect produced Spanish identifiers on English prompts; the mirror
    # directive must be present in the actually-built coder system prompt.
    prompt = build_coder_system_prompt("coder")
    assert LANGUAGE_MIRROR_DIRECTIVE in prompt


# ── HON1 — the chat summary never claims disk-write is disabled ───────────────


def test_hon1_summary_does_not_claim_apply_disabled() -> None:
    mission = _mission(outcome="ship it")
    summary = TaskService._format_coding_summary(mission, {"src/x.py": "patch"}, [])
    assert "not yet enabled" not in summary       # the old contradictory lie is gone
    assert "Plan panel" in summary                # points to the rich surface
    assert "```diff" not in summary               # diffs live in the panel, not chat


# ── OBS1 — narration rides the injected seam; the agent never imports transport ─


def test_obs1_narration_stays_behind_the_isolation_fence() -> None:
    # Both cognitive nodes narrate through the task_service-injected narrate emitter
    # carried on config.configurable (NOT graph state — a callable is not msgpack-
    # serializable and the checkpointer freezes the whole state), without importing
    # the transport layer at module scope.
    for rel in ("coder.py", "error_correction.py"):
        src = (_PKG_ROOT / "agents" / rel).read_text(encoding="utf-8")
        assert '.get("configurable", {}).get("narrate")' in src
        # The callable must never be sourced from serializable graph state again.
        assert 'state.get("narrate")' not in src

    # error_correction stays fully behind the fence: no api.* import at all
    # (judged on the AST, not docstring prose).
    tree = ast.parse(
        (_PKG_ROOT / "agents" / "error_correction.py").read_text(encoding="utf-8")
    )
    imported: List[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.append(node.module or "")
    assert not any(name.startswith("api.") for name in imported)


# ── RP1 — the finalized plan reaches the webview as structure, in one message ──


def test_rp1_plan_projects_to_structured_payload_and_round_trips() -> None:
    mission = _mission(
        outcome="add the OPEN_FILE handler",
        scope=["src/providers/workspace_panel.ts"],
        constraints=["no second WebviewPanel"],
        decisions=["dock inside the existing webview"],
        tasks=[
            WBSStep(
                step_number=1,
                target_role="core_dev",
                action="edit_file",
                target_file="src/providers/workspace_panel.ts",
                description="add the OPEN_FILE case",
            )
        ],
        checks=["npm run compile is clean"],
    )
    summary = "Drafted a plan — see the Plan panel."
    payload = TaskService._build_plan_payload(mission, summary)

    assert payload.summary == summary
    assert payload.outcome == "add the OPEN_FILE handler"
    assert payload.scope == ["src/providers/workspace_panel.ts"]
    assert payload.constraints and payload.decisions and payload.checks
    assert payload.tasks[0]["target_file"] == "src/providers/workspace_panel.ts"

    # The event survives the WebSocket adapter under the discriminated union.
    event = ServerPlanDocumentEvent(data=payload)
    restored = ws_adapter.validate_json(ws_adapter.dump_json(event))
    assert isinstance(restored, ServerPlanDocumentEvent)
    assert restored.event_type == "server_plan_document"
    assert restored.data.outcome == "add the OPEN_FILE handler"


# ── helpers ───────────────────────────────────────────────────────────────────


def _mission(
    *,
    outcome: str = "",
    scope: Optional[List[str]] = None,
    constraints: Optional[List[str]] = None,
    decisions: Optional[List[str]] = None,
    tasks: Optional[List[WBSStep]] = None,
    checks: Optional[List[str]] = None,
) -> MissionSpecification:
    return MissionSpecification(
        outcome=outcome,
        scope=scope or [],
        constraints=constraints or [],
        decisions=decisions or [],
        tasks=tasks or [],
        checks=checks or [],
    )
