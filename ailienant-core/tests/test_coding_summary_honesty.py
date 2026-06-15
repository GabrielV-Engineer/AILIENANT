"""The proposal summary must not claim disk application is disabled.

``_format_coding_summary`` renders the proposal turn BEFORE the permission gate
decides DENY / HITL / ALLOW, so its copy must be mode-neutral and truthful. The
bug it guards: the summary asserted "Applying changes to disk is not yet
enabled" while the very same flow applies via ``apply_patch_set`` and reports
"✓ Applied N file(s) to disk" — a lie to the operator.
"""
from __future__ import annotations

from types import SimpleNamespace

from core.task_service import TaskService


def _mission() -> SimpleNamespace:
    # The formatter only reads ``.outcome`` via getattr; a namespace suffices.
    return SimpleNamespace(outcome="Refactor the parser.")


def test_summary_does_not_claim_apply_disabled() -> None:
    summary = TaskService._format_coding_summary(
        _mission(), {"src/x.py": "@@ -1 +1 @@\n-old\n+new"}, [], plan_surface=False
    )
    assert "not yet enabled" not in summary


def test_summary_points_to_the_plan_panel_without_embedding_diffs() -> None:
    # In plan mode the chat bubble is a pointer to the rich Plan surface — the
    # diffs (and the full WBS) render there, not flattened into chat prose. This
    # keeps the bubble small regardless of plan size; the honesty guarantee above
    # is unchanged (the copy still never claims apply is disabled).
    summary = TaskService._format_coding_summary(
        _mission(), {"src/x.py": "@@ -1 +1 @@\n-old\n+new"}, [], plan_surface=True
    )
    assert "Plan panel" in summary
    assert "```diff" not in summary


def test_summary_ask_mode_points_to_the_inline_diff_not_the_panel() -> None:
    # In Ask/Auto the Plan panel does not render — the diff is shown inline in the
    # chat — so the pointer must NOT send the user to a non-existent panel.
    summary = TaskService._format_coding_summary(
        _mission(), {"src/x.py": "@@ -1 +1 @@\n-old\n+new"}, [], plan_surface=False
    )
    assert "Plan panel" not in summary
    assert "review the diff" in summary


def test_summary_auto_mode_announces_apply_not_authorize() -> None:
    # Auto mode applies without an authorize step, so the pointer must announce the
    # apply rather than ask the user to review/authorize a diff that never appears.
    summary = TaskService._format_coding_summary(
        _mission(), {"src/x.py": "@@ -1 +1 @@\n-old\n+new"}, [],
        plan_surface=False, auto_apply=True,
    )
    assert "Applying" in summary
    assert "authorize" not in summary
    assert "Plan panel" not in summary


def test_summary_empty_patches_branch_unchanged() -> None:
    summary = TaskService._format_coding_summary(_mission(), {}, [], plan_surface=True)
    assert "no concrete edits" in summary
