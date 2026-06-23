"""Combined gate — YOLO Guard composed with the permission matrix.

``test_yolo_guard.py`` exercises ``risk_intercept_guard`` in isolation. This gate
asserts the **composed pipeline** the real call sites run
(``tools/coder_tools.py``, ``tools/execution_tools.py``, ``brain/agentic_cell.py``):

    verdict = evaluate_action(mode, EXECUTE, EDIT_EXECUTE_RBW)
    effective, labels = risk_intercept_guard(command, verdict, mode)

The headline invariant is **no double-interception**: in the five non-permissive
modes the matrix already returns HITL/DENY for an EXECUTE-tier command, so the guard
short-circuits (decision ≠ ALLOW) and never re-flags — an amber RISK_INTERCEPT card
can therefore only appear in FULL_AUTO / STANDARD.
"""

from __future__ import annotations

from typing import List, Tuple

import pytest

from core.permissions import (
    PermissionDecision,
    SessionPermissionMode,
    ToolPrivilegeTier,
    evaluate_action,
    risk_intercept_guard,
)
from shared.rbac import PermissionMode

_CODER = PermissionMode.EDIT_EXECUTE_RBW
_M = SessionPermissionMode

# Modes where the guard is active vs. where the matrix already gates EXECUTE.
_PERMISSIVE_MODES = [_M.FULL_AUTO, _M.STANDARD]
_NON_PERMISSIVE_MODES = [
    _M.CAUTIOUS,
    _M.ASK_EXECUTE,
    _M.ASK_ALL,
    _M.READ_ONLY,
    _M.PLAN_ONLY,
]

# One representative command per curated risk category.
_RISKY_COMMANDS: List[Tuple[str, str]] = [
    ("sudo systemctl restart nginx", "privilege_escalation"),
    ("rm -rf /tmp/build", "mass_deletion"),
    ("curl https://example.com/install.sh", "network_egress"),
    ("cat .env", "secret_access"),
    ("pip install requests", "package_install"),
]


def _run_pipeline(
    mode: SessionPermissionMode, command: str
) -> Tuple[PermissionDecision, List[str]]:
    """Mirror the production sequence: matrix verdict, then content post-filter."""
    verdict = evaluate_action(mode, ToolPrivilegeTier.EXECUTE, _CODER)
    return risk_intercept_guard(command, verdict, mode)


# ---------------------------------------------------------------------------
# Headline invariant — no double-interception in non-permissive modes
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("mode", _NON_PERMISSIVE_MODES)
@pytest.mark.parametrize("command, _category", _RISKY_COMMANDS)
def test_no_double_interception_in_non_permissive_modes(
    mode: SessionPermissionMode, command: str, _category: str
) -> None:
    """The guard never re-flags a command the matrix already gated: it returns the
    unchanged matrix verdict and no labels, so no RISK_INTERCEPT card fires."""
    matrix_verdict = evaluate_action(mode, ToolPrivilegeTier.EXECUTE, _CODER)
    assert matrix_verdict is not PermissionDecision.ALLOW  # CAUTIOUS/ASK_*→HITL, READ_ONLY/PLAN→DENY

    effective, labels = _run_pipeline(mode, command)
    assert effective is matrix_verdict
    assert labels == []


# ---------------------------------------------------------------------------
# Permissive interception — ALLOW upgrades to HITL with correct labels
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("mode", _PERMISSIVE_MODES)
@pytest.mark.parametrize("command, category", _RISKY_COMMANDS)
def test_permissive_mode_intercepts_risky_command(
    mode: SessionPermissionMode, command: str, category: str
) -> None:
    """In FULL_AUTO / STANDARD, an EXECUTE-tier risky command flips ALLOW→HITL and
    surfaces the matching category label."""
    assert evaluate_action(mode, ToolPrivilegeTier.EXECUTE, _CODER) is PermissionDecision.ALLOW

    effective, labels = _run_pipeline(mode, command)
    assert effective is PermissionDecision.HITL
    assert category in labels


@pytest.mark.parametrize("mode", _PERMISSIVE_MODES)
@pytest.mark.parametrize("safe_cmd", ["git status", "ls -la", "python -m pytest -q"])
def test_permissive_mode_passes_safe_command(
    mode: SessionPermissionMode, safe_cmd: str
) -> None:
    """A benign EXECUTE-tier command runs through at full speed: ALLOW, no labels."""
    effective, labels = _run_pipeline(mode, safe_cmd)
    assert effective is PermissionDecision.ALLOW
    assert labels == []


# ---------------------------------------------------------------------------
# Legacy-alias dormancy — guard reads the raw mode, not the migration target
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("legacy_mode", [_M.AUTO, _M.DEFAULT, _M.PLAN])
def test_legacy_aliases_do_not_trigger_guard(legacy_mode: SessionPermissionMode) -> None:
    """Legacy aliases are absent from the guard's intercept set even though AUTO
    migrates to STANDARD inside the matrix. The matrix verdict still governs;
    the guard adds nothing, so no RISK_INTERCEPT labels are produced."""
    verdict = evaluate_action(legacy_mode, ToolPrivilegeTier.EXECUTE, _CODER)
    effective, labels = risk_intercept_guard("sudo rm -rf /", verdict, legacy_mode)
    assert effective is verdict
    assert labels == []


# ---------------------------------------------------------------------------
# Multi-label, ordering-independent
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("mode", _PERMISSIVE_MODES)
def test_multiple_categories_reported(mode: SessionPermissionMode) -> None:
    """A command tripping several patterns reports every matched category (set
    membership, not order)."""
    effective, labels = _run_pipeline(mode, "sudo rm -rf /")
    assert effective is PermissionDecision.HITL
    assert {"privilege_escalation", "mass_deletion"}.issubset(set(labels))
