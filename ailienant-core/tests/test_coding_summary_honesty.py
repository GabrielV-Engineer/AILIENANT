"""The turn-end summary must report what actually happened, not a prediction.

``_format_coding_summary`` used to render the proposal turn BEFORE the
permission gate decided DENY/HITL/ALLOW — the whole WBS ran to completion
inside the graph before the first approval card ever appeared, so its copy
had to guess an outcome ("Proposed N file change(s) — review the diff below
and authorize") that hadn't happened yet. 13.0.9 moved approval per-step and
in-graph (brain/apply_gate.py), so by the time this function runs every step
has already been generated, decided, and (if approved) applied — it now
reports the outcome, sourced from ``applied_files_log``, instead of a
promise. It must also never claim the old "use Ctrl+Z to undo" affordance: no
editor is ever opened for these files (PatchActuator.ts never calls
showTextDocument) and the actuator saves immediately, so there is no undo
stack for the keystroke to reach.
"""
from __future__ import annotations

from types import SimpleNamespace

from core.task_service import TaskService


def _mission(step_count: int = 2) -> SimpleNamespace:
    # The formatter reads ``.outcome`` and ``.tasks`` via getattr; a namespace
    # suffices. ``tasks`` is load-bearing: a plan with zero steps is reported
    # differently from one that simply applied nothing.
    return SimpleNamespace(
        outcome="Refactor the parser.",
        tasks=[object()] * step_count,
    )


def _completed(path: str) -> dict:
    return {"file_path": path, "command": None, "status": "completed", "step_number": 1}


def _rejected(path: str) -> dict:
    return {"file_path": path, "command": None, "status": "rejected", "step_number": 2}


def _failed(path: str) -> dict:
    return {"file_path": path, "command": None, "status": "failed", "step_number": 3}


def _revision_requested(path: str) -> dict:
    return {"file_path": path, "command": None, "status": "revision_requested", "step_number": 4}


def _completed_command(cmd: str) -> dict:
    return {"file_path": None, "command": cmd, "status": "completed", "step_number": 5}


def test_summary_never_claims_ctrl_z_undo() -> None:
    """The specific dishonesty this was originally written to catch, updated
    for the current false claim: no editor is opened for these writes, so
    Ctrl+Z genuinely cannot revert them — the summary must not say it can."""
    summary = TaskService._format_coding_summary(_mission(), [_completed("src/x.py")], [])
    assert "Ctrl+Z" not in summary
    assert "not yet enabled" not in summary


def test_summary_points_to_local_history_for_applied_changes() -> None:
    summary = TaskService._format_coding_summary(_mission(), [_completed("src/x.py")], [])
    assert "Local History" in summary


def test_summary_reports_applied_file_count() -> None:
    summary = TaskService._format_coding_summary(
        _mission(), [_completed("a.py"), _completed("b.py")], [],
    )
    assert "Applied 2 file changes to disk" in summary


def test_summary_singular_wording_for_one_file() -> None:
    summary = TaskService._format_coding_summary(_mission(), [_completed("a.py")], [])
    assert "Applied 1 file change to disk" in summary
    assert "1 file changes" not in summary


def test_summary_reports_rejected_steps() -> None:
    summary = TaskService._format_coding_summary(
        _mission(), [_completed("a.py"), _rejected("b.py")], [],
    )
    assert "1 declined" in summary


def test_summary_reports_failed_steps() -> None:
    summary = TaskService._format_coding_summary(_mission(), [_failed("a.py")], [])
    assert "1 not applied" in summary


def test_summary_reports_steps_still_under_revision() -> None:
    summary = TaskService._format_coding_summary(_mission(), [_revision_requested("a.py")], [])
    assert "1 still under revision" in summary


def test_summary_reports_successful_commands_distinctly_from_files() -> None:
    summary = TaskService._format_coding_summary(
        _mission(), [_completed("a.py"), _completed_command("pytest -q")], [],
    )
    assert "Applied 1 file change to disk" in summary
    assert "ran 1 command successfully" in summary


def test_summary_combines_multiple_outcomes_in_one_turn() -> None:
    log = [
        _completed("a.py"), _completed("b.py"), _rejected("c.py"),
        _failed("d.py"), _revision_requested("e.py"),
    ]
    summary = TaskService._format_coding_summary(_mission(), log, [])
    assert "Applied 2 file changes to disk" in summary
    assert "1 declined" in summary
    assert "1 not applied" in summary
    assert "1 still under revision" in summary


def test_summary_empty_log_reports_no_concrete_edits() -> None:
    """A real plan that produced no applied entries (e.g. a read_file-only plan)
    still points to the Plan panel."""
    summary = TaskService._format_coding_summary(_mission(), [], [])
    assert "no concrete edits" in summary
    assert "Plan panel" in summary


def test_summary_names_a_stepless_plan_instead_of_blaming_the_edits() -> None:
    """The reported incident: the planner returned zero steps, and the turn said
    only that it "produced no concrete edits" — describing the symptom while
    hiding the cause."""
    summary = TaskService._format_coding_summary(_mission(step_count=0), [], [])
    assert "no steps" in summary


def test_summary_reports_a_plan_mode_turn_as_a_success() -> None:
    """Stopping after the plan IS the correct outcome of a read-only turn;
    reporting it as an absence of edits describes success in failure language."""
    summary = TaskService._format_coding_summary(
        _mission(step_count=3), [], [], plan_only=True
    )
    assert "Plan ready" in summary and "3 steps" in summary
    assert "no concrete edits" not in summary


def test_summary_never_swallows_errors_when_nothing_was_applied() -> None:
    """The empty-log branch used to return BEFORE the notes were appended, so the
    one path with nothing to show was also the only one that withheld why."""
    summary = TaskService._format_coding_summary(
        _mission(), [], ["Orchestrator: missing mission_spec or empty tasks."]
    )
    assert "Orchestrator: missing mission_spec or empty tasks." in summary
    assert "Plan panel" in summary


def test_summary_all_steps_rejected_reports_no_changes_applied() -> None:
    summary = TaskService._format_coding_summary(_mission(), [_rejected("a.py")], [])
    assert "Local History" not in summary  # nothing landed on disk to revert


def test_summary_surfaces_user_facing_errors_as_notes() -> None:
    summary = TaskService._format_coding_summary(
        _mission(), [_completed("a.py")], ["Skipped AILIENANT's own runtime files (they cannot be moved): `x.log`"],
    )
    assert "_Notes:_" in summary
    assert "Skipped AILIENANT's own runtime files" in summary


def test_summary_hides_internal_self_heal_diagnostics_from_notes() -> None:
    summary = TaskService._format_coding_summary(
        _mission(), [_completed("a.py")], ["self-heal could not correct coder_agent: no readable offending file"],
    )
    assert "self-heal could not correct" not in summary


# ── Acceptance checks (13.1.3, §8.7/C2) ─────────────────────────────────────
#
# `check_results` (brain/checks_gate.py) folds the plan's OWN acceptance
# criteria into this same honesty discipline: "N files applied" standing
# alone, when a check has since proven the work incomplete, is exactly the
# silent-success failure this whole function exists to prevent.


def _check(status: str, text: str = "Pytest exits 0.") -> dict:
    return {"check": text, "command": "pytest", "status": status}


def test_summary_defaults_to_unchanged_when_no_checks_ran() -> None:
    """Backward compatibility: a plan with no checks, or a turn that never
    reached run_checks_node, must render exactly as before."""
    summary = TaskService._format_coding_summary(_mission(), [_completed("a.py")], [])
    assert "acceptance check" not in summary.lower()


def test_summary_leads_with_a_failed_check_ahead_of_the_apply_summary() -> None:
    """The single most important fact about a turn — a failed acceptance
    check — must be the FIRST thing a user reads, never buried after 'Applied
    N file changes to disk.' where a skim would read it as a clean success."""
    summary = TaskService._format_coding_summary(
        _mission(), [_completed("a.py")], [], [_check("failed")],
    )
    assert summary.startswith("1 acceptance check FAILED")
    assert "NOT verified complete" in summary
    assert "Applied 1 file change" in summary  # still reports what happened, just not first


def test_summary_reports_all_checks_passed() -> None:
    summary = TaskService._format_coding_summary(
        _mission(), [_completed("a.py")], [], [_check("passed"), _check("passed", "npm run build succeeds")],
    )
    assert "2 acceptance checks passed" in summary
    assert "FAILED" not in summary


def test_summary_reports_unverified_checks_honestly() -> None:
    summary = TaskService._format_coding_summary(
        _mission(), [_completed("a.py")], [],
        [_check("passed"), _check("unverified", "Verify props are wired correctly.")],
    )
    assert "1 acceptance check passed" in summary
    assert "1 could not be automatically verified" in summary


def test_summary_a_failed_check_still_reaches_the_notes_via_errors() -> None:
    """The failed-check headline and the detailed error note are complementary,
    not exclusive — the headline for a skim, the note for the detail."""
    summary = TaskService._format_coding_summary(
        _mission(), [_completed("a.py")],
        ["Acceptance check failed — 'Pytest exits 0.' (pytest): 2 failed, 1 passed"],
        [_check("failed")],
    )
    assert "acceptance check FAILED" in summary
    assert "_Notes:_" in summary
    assert "2 failed, 1 passed" in summary
