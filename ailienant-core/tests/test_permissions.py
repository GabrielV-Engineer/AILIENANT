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

# ASK_ALL is the sole mode that gates READ_ONLY tier through HITL; every other
# mode (including all legacy aliases) auto-admits reads.
_READ_ALLOW_SESSIONS = [s for s in _ALL_SESSIONS if s is not SessionPermissionMode.ASK_ALL]


@pytest.mark.parametrize("session", _READ_ALLOW_SESSIONS)
@pytest.mark.parametrize("identity", _ALL_IDENTITIES)
def test_evaluate_action_read_only_allows_outside_ask_all(
    session: SessionPermissionMode, identity: object
) -> None:
    """READ_ONLY tier is ALLOW in every mode except ASK_ALL, for any identity."""
    decision = evaluate_action(
        session, ToolPrivilegeTier.READ_ONLY, identity.permission_mode  # type: ignore[attr-defined]
    )
    assert decision is PermissionDecision.ALLOW


@pytest.mark.parametrize("identity", _ALL_IDENTITIES)
def test_evaluate_action_ask_all_gates_reads(identity: object) -> None:
    """ASK_ALL routes even READ_ONLY tier through HITL, regardless of identity."""
    decision = evaluate_action(
        SessionPermissionMode.ASK_ALL,
        ToolPrivilegeTier.READ_ONLY,
        identity.permission_mode,  # type: ignore[attr-defined]
    )
    assert decision is PermissionDecision.HITL


# Canonical 7-mode matrix smoke. A representative cell per mode for a
# mutation-capable identity; the exhaustive 7x3 sweep is the Division checkpoint
# gate. ALLOW=a, HITL=h, DENY=d.
_A = PermissionDecision.ALLOW
_H = PermissionDecision.HITL
_D = PermissionDecision.DENY
_CANONICAL_ROWS = [
    (SessionPermissionMode.FULL_AUTO, _A, _A, _A, _A),
    (SessionPermissionMode.STANDARD, _A, _A, _A, _H),
    (SessionPermissionMode.CAUTIOUS, _A, _H, _H, _H),
    (SessionPermissionMode.ASK_EXECUTE, _A, _H, _H, _D),
    (SessionPermissionMode.ASK_ALL, _H, _H, _H, _H),
    (SessionPermissionMode.READ_ONLY, _A, _D, _D, _D),
    (SessionPermissionMode.PLAN_ONLY, _A, _D, _D, _D),
]


@pytest.mark.parametrize("mode, ro, wr, ex, dg", _CANONICAL_ROWS)
def test_evaluate_action_canonical_matrix_rows(
    mode: SessionPermissionMode,
    ro: PermissionDecision,
    wr: PermissionDecision,
    ex: PermissionDecision,
    dg: PermissionDecision,
) -> None:
    """Each canonical mode resolves its (tier -> decision) row for a coder identity."""
    coder = PermissionMode.EDIT_EXECUTE_RBW
    assert evaluate_action(mode, ToolPrivilegeTier.READ_ONLY, coder) is ro
    assert evaluate_action(mode, ToolPrivilegeTier.WRITE, coder) is wr
    assert evaluate_action(mode, ToolPrivilegeTier.EXECUTE, coder) is ex
    assert evaluate_action(mode, ToolPrivilegeTier.DANGEROUS, coder) is dg


@pytest.mark.parametrize(
    "legacy, canonical",
    [
        (SessionPermissionMode.DEFAULT, SessionPermissionMode.CAUTIOUS),
        (SessionPermissionMode.AUTO, SessionPermissionMode.STANDARD),
        (SessionPermissionMode.PLAN, SessionPermissionMode.PLAN_ONLY),
    ],
)
@pytest.mark.parametrize("tier", list(ToolPrivilegeTier))
def test_evaluate_action_legacy_matches_canonical_target(
    legacy: SessionPermissionMode,
    canonical: SessionPermissionMode,
    tier: ToolPrivilegeTier,
) -> None:
    """A deprecated legacy mode resolves identically to its migration target."""
    coder = PermissionMode.EDIT_EXECUTE_RBW
    assert evaluate_action(legacy, tier, coder) is evaluate_action(canonical, tier, coder)


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
