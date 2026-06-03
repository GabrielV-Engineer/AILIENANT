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
        _mission(), {"src/x.py": "@@ -1 +1 @@\n-old\n+new"}, []
    )
    assert "not yet enabled" not in summary


def test_summary_still_renders_proposed_diffs() -> None:
    # Honesty fix must not regress the proposal render: the diff fence and the
    # file path still appear.
    summary = TaskService._format_coding_summary(
        _mission(), {"src/x.py": "@@ -1 +1 @@\n-old\n+new"}, []
    )
    assert "```diff" in summary
    assert "src/x.py" in summary


def test_summary_empty_patches_branch_unchanged() -> None:
    summary = TaskService._format_coding_summary(_mission(), {}, [])
    assert "no concrete edits" in summary
