# tests/test_mission_spec_coercion.py
"""Phase 7.12 DoD — MissionSpecification / WBSStep hallucination coercion.

The Planner LLM intermittently violates the (immutable) MissionSpecification
contract: it injects objects where ``List[str]`` is required and arbitrary role
strings where a ``Literal`` is required. Before-validators coerce these instead of
raising a ValidationError (which would burn a planner retry). The contract itself
is unchanged — these tests prove malformed input now *validates* to clean data.
"""
from __future__ import annotations

from brain.state import MissionSpecification, WBSStep


def _valid_task() -> dict:
    return {
        "step_number": 1,
        "target_role": "core_dev",
        "action": "edit_file",
        "target_file": "src/x.py",
        "description": "do the thing",
    }


# ── C1 — dict element inside a List[str] field is flattened ──────────────────


def test_dict_in_scope_is_flattened() -> None:
    spec = MissionSpecification.model_validate({
        "outcome": "ship it",
        "scope": [{"file": "src/x.py", "reason": "edit here"}, "src/y.py"],
        "constraints": ["no new deps"],
        "decisions": ["use stdlib"],
        "tasks": [_valid_task()],
        "checks": ["pytest green"],
    })
    assert all(isinstance(s, str) for s in spec.scope)
    assert "file: src/x.py" in spec.scope[0]
    assert spec.scope[1] == "src/y.py"


# ── C2 — bare scalar/dict (not a list) is wrapped into a one-element list ─────


def test_scalar_field_wrapped_into_list() -> None:
    spec = MissionSpecification.model_validate({
        "outcome": "ship it",
        "scope": "single string scope",
        "constraints": {"limit": "O(n)"},
        "decisions": ["x"],
        "tasks": [_valid_task()],
        "checks": ["x"],
    })
    assert spec.scope == ["single string scope"]
    assert spec.constraints == ["limit: O(n)"]


# ── C3 — out-of-vocabulary target_role coerces to the safe default ───────────


def test_unknown_target_role_coerced_to_default() -> None:
    step = WBSStep.model_validate({
        "step_number": 1,
        "target_role": "wizard",            # hallucinated, not in the Literal
        "action": "edit_file",
        "target_file": "src/x.py",
        "description": "y",
    })
    assert step.target_role == "core_dev"


# ── C4 — legacy role still migrates (regression guard for Phase 4.1.4) ────────


def test_legacy_role_still_migrates() -> None:
    step = WBSStep.model_validate({
        "step_number": 1,
        "target_role": "Refactor",
        "action": "edit_file",
        "target_file": "src/x.py",
        "description": "y",
    })
    assert step.target_role == "architect_refactor"


# ── C5 — a clean, already-valid spec is untouched (idempotency) ──────────────


def test_valid_spec_untouched() -> None:
    payload = {
        "outcome": "ship it",
        "scope": ["src/x.py"],
        "constraints": ["no new deps"],
        "decisions": ["use stdlib"],
        "tasks": [_valid_task()],
        "checks": ["pytest green"],
    }
    spec = MissionSpecification.model_validate(payload)
    assert spec.scope == ["src/x.py"]
    assert spec.tasks[0].target_role == "core_dev"
