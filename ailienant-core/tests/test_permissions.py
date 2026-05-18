# ailienant-core/tests/test_permissions.py
#
# Phase 5.1 smoke test — 3-axis permission matrix + RBWE guard.
# Mirrors the style of test_routing.py: pure-function parametrized tests, no
# fixtures, no monkeypatching. The formal 11-test gate (test_permission_axes.py
# + test_rbwe_enforcement.py) lands at Phase 5.7 per PHASE_5_BLUEPRINT §7.
#
# DoD: pytest ailienant-core/tests/test_permissions.py -v must pass with 0 failures.

from typing import Any, Dict

import pytest

from core.permissions import (
    PermissionDecision,
    PermissionDeniedError,
    SessionPermissionMode,
    ToolPrivilegeTier,
    evaluate_action,
    rbwe_guard,
)
from shared.rbac import (
    LOGIC_IDENTITY,
    PLANNER_IDENTITY,
    RESEARCHER_IDENTITY,
    PermissionMode,
)


# ---------------------------------------------------------------------------
# evaluate_action — 3-axis matrix
# ---------------------------------------------------------------------------

_NON_READ_TIERS = [
    ToolPrivilegeTier.WRITE,
    ToolPrivilegeTier.EXECUTE,
    ToolPrivilegeTier.DANGEROUS,
]
_ALL_SESSIONS = list(SessionPermissionMode)
_ALL_IDENTITIES = [PLANNER_IDENTITY, LOGIC_IDENTITY, RESEARCHER_IDENTITY]


@pytest.mark.parametrize("session", _ALL_SESSIONS)
@pytest.mark.parametrize("identity", _ALL_IDENTITIES)
def test_evaluate_action_read_only_always_allows(
    session: SessionPermissionMode, identity: object
) -> None:
    """READ_ONLY tier is ALLOW regardless of session mode or agent identity."""
    decision = evaluate_action(
        session, ToolPrivilegeTier.READ_ONLY, identity.permission_mode  # type: ignore[attr-defined]
    )
    assert decision is PermissionDecision.ALLOW


@pytest.mark.parametrize("tier", _NON_READ_TIERS)
@pytest.mark.parametrize("identity", _ALL_IDENTITIES)
def test_evaluate_action_plan_session_denies_writes(
    tier: ToolPrivilegeTier, identity: object
) -> None:
    """SessionPermissionMode.PLAN blocks every non-READ_ONLY tier for every identity."""
    decision = evaluate_action(
        SessionPermissionMode.PLAN, tier, identity.permission_mode  # type: ignore[attr-defined]
    )
    assert decision is PermissionDecision.DENY


def test_evaluate_action_auto_session_blocks_dangerous() -> None:
    """SessionPermissionMode.AUTO must NEVER auto-approve DANGEROUS — always HITL."""
    decision = evaluate_action(
        SessionPermissionMode.AUTO,
        ToolPrivilegeTier.DANGEROUS,
        PermissionMode.EDIT_EXECUTE_RBW,
    )
    assert decision is PermissionDecision.HITL


@pytest.mark.parametrize("tier", [ToolPrivilegeTier.WRITE, ToolPrivilegeTier.EXECUTE])
def test_evaluate_action_auto_session_allows_write_execute(tier: ToolPrivilegeTier) -> None:
    """AUTO is uninterrupted execution for WRITE/EXECUTE (DANGEROUS still HITL)."""
    decision = evaluate_action(SessionPermissionMode.AUTO, tier, PermissionMode.EDIT_EXECUTE_RBW)
    assert decision is PermissionDecision.ALLOW


@pytest.mark.parametrize("tier", _NON_READ_TIERS)
def test_evaluate_action_default_session_hitl_on_writes(tier: ToolPrivilegeTier) -> None:
    """DEFAULT session gates every non-READ_ONLY tier through HITL for a mutating identity."""
    decision = evaluate_action(SessionPermissionMode.DEFAULT, tier, PermissionMode.EDIT_EXECUTE_RBW)
    assert decision is PermissionDecision.HITL


@pytest.mark.parametrize("tier", _NON_READ_TIERS)
@pytest.mark.parametrize("session", _ALL_SESSIONS)
def test_evaluate_action_planner_identity_locked(
    tier: ToolPrivilegeTier, session: SessionPermissionMode
) -> None:
    """PLAN_ONLY identity (Planner) is DENY for every non-READ_ONLY tier in every session."""
    decision = evaluate_action(session, tier, PermissionMode.PLAN_ONLY)
    assert decision is PermissionDecision.DENY


@pytest.mark.parametrize("tier", _NON_READ_TIERS)
@pytest.mark.parametrize("session", _ALL_SESSIONS)
def test_evaluate_action_read_only_identity_locked(
    tier: ToolPrivilegeTier, session: SessionPermissionMode
) -> None:
    """READ_ONLY identity (Researcher/Analyst) is DENY for every non-READ_ONLY tier."""
    decision = evaluate_action(session, tier, PermissionMode.READ_ONLY)
    assert decision is PermissionDecision.DENY


def test_evaluate_action_is_cached() -> None:
    """evaluate_action must be lru_cached for O(1) repeated lookups."""
    evaluate_action.cache_clear()
    args = (
        SessionPermissionMode.DEFAULT,
        ToolPrivilegeTier.WRITE,
        PermissionMode.EDIT_EXECUTE_RBW,
    )
    evaluate_action(*args)
    evaluate_action(*args)
    info = evaluate_action.cache_info()
    assert info.hits == 1
    assert info.misses == 1


# ---------------------------------------------------------------------------
# rbwe_guard — Read-Before-Write enforcement
# ---------------------------------------------------------------------------


def test_rbwe_guard_allows_read_only() -> None:
    """READ_ONLY tier bypasses the guard even when target is unknown."""
    state: Dict[str, Any] = {"read_files_state": {}}
    rbwe_guard("FileReadTool", ToolPrivilegeTier.READ_ONLY, "never/read.py", state)


def test_rbwe_guard_allows_when_target_is_none() -> None:
    """Mutating tier without a concrete target_path (e.g. bare bash) bypasses the guard."""
    state: Dict[str, Any] = {"read_files_state": {}}
    rbwe_guard("SandboxBashTool", ToolPrivilegeTier.EXECUTE, None, state)


def test_rbwe_guard_rejects_unread_write() -> None:
    """WRITE tier on an unread target raises PermissionDeniedError with provenance."""
    state: Dict[str, Any] = {"read_files_state": {}}
    with pytest.raises(PermissionDeniedError) as exc:
        rbwe_guard("FileWriteTool", ToolPrivilegeTier.WRITE, "src/foo.py", state)
    assert exc.value.tool_name == "FileWriteTool"
    assert exc.value.target_path == "src/foo.py"
    assert "FileReadTool" in exc.value.reason  # corrective hint included


def test_rbwe_guard_allows_after_read() -> None:
    """WRITE tier targeting a path present in read_files_state passes silently."""
    state: Dict[str, Any] = {"read_files_state": {"src/foo.py": object()}}
    rbwe_guard("FileWriteTool", ToolPrivilegeTier.WRITE, "src/foo.py", state)


@pytest.mark.parametrize(
    "tier", [ToolPrivilegeTier.EXECUTE, ToolPrivilegeTier.DANGEROUS]
)
def test_rbwe_guard_rejects_execute_and_dangerous(tier: ToolPrivilegeTier) -> None:
    """EXECUTE and DANGEROUS tiers are also subject to RBWE when target_path is set."""
    state: Dict[str, Any] = {"read_files_state": {}}
    with pytest.raises(PermissionDeniedError):
        rbwe_guard("SomeTool", tier, "/etc/shadow", state)
