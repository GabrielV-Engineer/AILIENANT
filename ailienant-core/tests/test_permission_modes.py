"""Division checkpoint gate — authoritative 7×4 permission-matrix lock.

This is the test-only sibling gate that freezes the canonical permission surface.
Where ``test_permissions.py`` carries a representative *smoke* of the matrix, this
file asserts every cell exhaustively against an **independent** contract table
transcribed from ``docs/SCHEMA_EVOLUTION.MD §23`` — so a change to the production
``_DECISION_MATRIX`` that is not mirrored here fails the gate, and vice-versa.

The gate locks five invariants:
  1. the full 7-mode × 4-tier decision matrix for a mutation-capable identity,
  2. "ASK" product language == ``PermissionDecision.HITL``,
  3. the identity floor (non-``EDIT_EXECUTE_RBW`` identities are DENY for any
     non-READ_ONLY tier, in every mode),
  4. legacy-alias migration equivalence (``DEFAULT≡CAUTIOUS`` / ``AUTO≡STANDARD`` /
     ``PLAN≡PLAN_ONLY``) across all tiers,
  5. wire-value round-tripping through ``session_mode_from_channel``.
"""

from __future__ import annotations

from typing import Dict, List, Tuple

import pytest

from core.permissions import (
    PermissionDecision,
    SessionPermissionMode,
    ToolPrivilegeTier,
    _VALID_SESSION_MODE_VALUES,
    evaluate_action,
    session_mode_from_channel,
)
from shared.rbac import PermissionMode

# Mutation-capable identity — the only identity that reaches the full matrix; every
# other identity is clamped by the floor (locked separately below).
_CODER = PermissionMode.EDIT_EXECUTE_RBW

_ALLOW = PermissionDecision.ALLOW
_HITL = PermissionDecision.HITL  # "ASK" in product language.
_DENY = PermissionDecision.DENY

_M = SessionPermissionMode
_T = ToolPrivilegeTier


# ---------------------------------------------------------------------------
# 1. Exhaustive 7×4 matrix — independent contract table (SCHEMA_EVOLUTION §23)
# ---------------------------------------------------------------------------
#
# Transcribed by hand from the §23 table, deliberately NOT imported from
# permissions._DECISION_MATRIX. The redundancy is the point: the source matrix
# and this contract must agree, so neither can drift unnoticed.
_CONTRACT_MATRIX: Dict[SessionPermissionMode, Dict[ToolPrivilegeTier, PermissionDecision]] = {
    _M.FULL_AUTO:   {_T.READ_ONLY: _ALLOW, _T.WRITE: _ALLOW, _T.EXECUTE: _ALLOW, _T.DANGEROUS: _ALLOW},
    _M.STANDARD:    {_T.READ_ONLY: _ALLOW, _T.WRITE: _ALLOW, _T.EXECUTE: _ALLOW, _T.DANGEROUS: _HITL},
    _M.CAUTIOUS:    {_T.READ_ONLY: _ALLOW, _T.WRITE: _HITL,  _T.EXECUTE: _HITL,  _T.DANGEROUS: _HITL},
    _M.ASK_EXECUTE: {_T.READ_ONLY: _ALLOW, _T.WRITE: _HITL,  _T.EXECUTE: _HITL,  _T.DANGEROUS: _DENY},
    _M.ASK_ALL:     {_T.READ_ONLY: _HITL,  _T.WRITE: _HITL,  _T.EXECUTE: _HITL,  _T.DANGEROUS: _HITL},
    _M.READ_ONLY:   {_T.READ_ONLY: _ALLOW, _T.WRITE: _DENY,  _T.EXECUTE: _DENY,  _T.DANGEROUS: _DENY},
    _M.PLAN_ONLY:   {_T.READ_ONLY: _ALLOW, _T.WRITE: _DENY,  _T.EXECUTE: _DENY,  _T.DANGEROUS: _DENY},
}

# Flatten to one parametrize case per cell (7 × 4 = 28).
_MATRIX_CELLS: List[Tuple[SessionPermissionMode, ToolPrivilegeTier, PermissionDecision]] = [
    (mode, tier, expected)
    for mode, row in _CONTRACT_MATRIX.items()
    for tier, expected in row.items()
]


def test_contract_table_covers_every_canonical_mode_and_tier() -> None:
    """The contract table is complete: all 7 canonical modes × all 4 tiers."""
    canonical_modes = {
        m for m in SessionPermissionMode if m.value in {
            "full_auto", "standard", "cautious", "ask_execute",
            "ask_all", "read_only", "plan_only",
        }
    }
    assert set(_CONTRACT_MATRIX) == canonical_modes
    for row in _CONTRACT_MATRIX.values():
        assert set(row) == set(ToolPrivilegeTier)
    assert len(_MATRIX_CELLS) == 28


@pytest.mark.parametrize("mode, tier, expected", _MATRIX_CELLS)
def test_matrix_cell_matches_contract(
    mode: SessionPermissionMode,
    tier: ToolPrivilegeTier,
    expected: PermissionDecision,
) -> None:
    """Every (mode, tier) cell resolves to the §23-published decision for a coder."""
    assert evaluate_action(mode, tier, _CODER) is expected


# ---------------------------------------------------------------------------
# 2. ASK == HITL — the "Ask" product rows never resolve to a distinct outcome
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "mode, tier",
    [
        (_M.CAUTIOUS, _T.WRITE),
        (_M.CAUTIOUS, _T.EXECUTE),
        (_M.ASK_EXECUTE, _T.WRITE),
        (_M.ASK_EXECUTE, _T.EXECUTE),
        (_M.ASK_ALL, _T.READ_ONLY),
        (_M.ASK_ALL, _T.DANGEROUS),
    ],
)
def test_ask_rows_are_hitl(mode: SessionPermissionMode, tier: ToolPrivilegeTier) -> None:
    """"Ask before …" maps onto HITL, not a separate decision value."""
    assert evaluate_action(mode, tier, _CODER) is PermissionDecision.HITL


# ---------------------------------------------------------------------------
# 3. Identity floor — non-coder identities are DENY for any non-READ_ONLY tier
# ---------------------------------------------------------------------------

_NON_READ_TIERS = [_T.WRITE, _T.EXECUTE, _T.DANGEROUS]
_FLOORED_IDENTITIES = [PermissionMode.PLAN_ONLY, PermissionMode.READ_ONLY]
_ALL_CANONICAL_MODES = list(_CONTRACT_MATRIX)


@pytest.mark.parametrize("identity", _FLOORED_IDENTITIES)
@pytest.mark.parametrize("mode", _ALL_CANONICAL_MODES)
@pytest.mark.parametrize("tier", _NON_READ_TIERS)
def test_identity_floor_denies_non_read_tiers(
    identity: PermissionMode,
    mode: SessionPermissionMode,
    tier: ToolPrivilegeTier,
) -> None:
    """A non-EDIT_EXECUTE_RBW identity can never exceed READ_ONLY, in any mode."""
    assert evaluate_action(mode, tier, identity) is PermissionDecision.DENY


@pytest.mark.parametrize("identity", _FLOORED_IDENTITIES)
@pytest.mark.parametrize("mode", _ALL_CANONICAL_MODES)
def test_identity_floor_clears_for_read_only_tier(
    identity: PermissionMode, mode: SessionPermissionMode
) -> None:
    """READ_ONLY tier clears the floor, so its per-mode posture is still honored
    (ALLOW everywhere except ASK_ALL, which gates reads through HITL)."""
    expected = PermissionDecision.HITL if mode is _M.ASK_ALL else PermissionDecision.ALLOW
    assert evaluate_action(mode, _T.READ_ONLY, identity) is expected


# ---------------------------------------------------------------------------
# 4. Legacy-alias migration equivalence — across all 4 tiers
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "legacy, canonical",
    [
        (_M.DEFAULT, _M.CAUTIOUS),
        (_M.AUTO, _M.STANDARD),
        (_M.PLAN, _M.PLAN_ONLY),
    ],
)
@pytest.mark.parametrize("tier", list(ToolPrivilegeTier))
def test_legacy_alias_resolves_to_canonical(
    legacy: SessionPermissionMode,
    canonical: SessionPermissionMode,
    tier: ToolPrivilegeTier,
) -> None:
    """Each deprecated alias produces the exact decision of its canonical target."""
    assert evaluate_action(legacy, tier, _CODER) is evaluate_action(canonical, tier, _CODER)


def test_legacy_auto_never_auto_approves_dangerous() -> None:
    """The pre-7-mode invariant survives: legacy AUTO (→STANDARD) keeps DANGEROUS at HITL,
    never ALLOW — only the new opt-in FULL_AUTO auto-runs irreversible operations."""
    assert evaluate_action(_M.AUTO, _T.DANGEROUS, _CODER) is PermissionDecision.HITL
    assert evaluate_action(_M.FULL_AUTO, _T.DANGEROUS, _CODER) is PermissionDecision.ALLOW


# ---------------------------------------------------------------------------
# 5. Wire-value round-trip — every member is an accepted channel value
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("mode", list(SessionPermissionMode))
def test_every_mode_value_is_accepted_wire_value(mode: SessionPermissionMode) -> None:
    """No member can be persisted to the channel and then rejected on resume."""
    assert mode.value in _VALID_SESSION_MODE_VALUES


@pytest.mark.parametrize("mode", list(SessionPermissionMode))
def test_session_mode_from_channel_round_trips(mode: SessionPermissionMode) -> None:
    """A persisted mode value reloads to the same member, case-insensitively."""
    assert session_mode_from_channel(mode.value) is mode
    assert session_mode_from_channel(mode.value.upper()) is mode


def test_session_mode_from_channel_falls_back_on_garbage() -> None:
    """An unrecognized channel value resolves to a safe (non-escalating) default,
    never raising — a typo must not crash resume or escalate privileges."""
    fallback = session_mode_from_channel("not-a-real-mode")
    assert fallback in set(SessionPermissionMode)
    # The fallback must not be a permissive auto-run posture.
    assert fallback not in {_M.FULL_AUTO, _M.STANDARD, _M.AUTO}
