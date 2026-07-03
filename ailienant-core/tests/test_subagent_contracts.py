# tests/test_subagent_contracts.py
"""Dynamic-dispatch schema DoD — bounds, closed vocabularies, and default-safety.

Every LLM-authored dispatch instruction is a Pydantic-validated closed-vocabulary
object, so a hallucinated shape must fail fast at the boundary. These tests prove
the Pydantic bounds (recursion depth, fan-out width, field counts) reject an
over-scoped or malformed plan, the closed Literal vocabularies reject unknown
values, and the result envelope carries its documented safe defaults.
"""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from shared.config import MAX_OBSERVATION_CHARS
from brain.subagent_contracts import (
    DispatchBatchResult,
    DispatchPlan,
    SubagentResponseField,
    SubagentResponseSchema,
    SubagentResultEnvelope,
    SubagentTask,
)


def _response_schema() -> SubagentResponseSchema:
    return SubagentResponseSchema(
        fields=[SubagentResponseField(name="summary", type="str", description="one line")]
    )


def _task(**overrides: object) -> SubagentTask:
    payload: dict[str, object] = {
        "task_id": "abc123",
        "description": "do the thing",
        "subagent_role": "core_dev",
        "response_schema": _response_schema(),
    }
    payload.update(overrides)
    return SubagentTask(**payload)  # type: ignore[arg-type]


def _plan(**overrides: object) -> DispatchPlan:
    payload: dict[str, object] = {
        "pattern": "fanout_and_synthesize",
        "tasks": [_task()],
        "synthesis_instruction": "merge the results",
    }
    payload.update(overrides)
    return DispatchPlan(**payload)  # type: ignore[arg-type]


# ── D1 — DispatchPlan.dispatch_depth is bounded [0, 2] ───────────────────────


def test_dispatch_depth_accepts_in_bounds() -> None:
    for depth in (0, 1, 2):
        assert _plan(dispatch_depth=depth).dispatch_depth == depth


def test_dispatch_depth_rejects_out_of_bounds() -> None:
    for depth in (-1, 3):
        with pytest.raises(ValidationError):
            _plan(dispatch_depth=depth)


# ── D2 — DispatchPlan.tasks fan-out width is bounded [1, 32] ──────────────────


def test_tasks_accepts_boundary_widths() -> None:
    assert len(_plan(tasks=[_task()]).tasks) == 1
    assert len(_plan(tasks=[_task() for _ in range(32)]).tasks) == 32


def test_tasks_rejects_empty_and_over_width() -> None:
    with pytest.raises(ValidationError):
        _plan(tasks=[])
    with pytest.raises(ValidationError):
        _plan(tasks=[_task() for _ in range(33)])


# ── D3 — SubagentResponseSchema.fields is bounded [1, 8] ──────────────────────


def test_response_schema_fields_bounds() -> None:
    one = [SubagentResponseField(name="a", type="str", description="d")]
    eight = [SubagentResponseField(name=f"f{i}", type="str", description="d") for i in range(8)]
    assert len(SubagentResponseSchema(fields=one).fields) == 1
    assert len(SubagentResponseSchema(fields=eight).fields) == 8
    with pytest.raises(ValidationError):
        SubagentResponseSchema(fields=[])
    with pytest.raises(ValidationError):
        SubagentResponseSchema(
            fields=[SubagentResponseField(name=f"f{i}", type="str", description="d") for i in range(9)]
        )


# ── D4 — SubagentResponseField.type is a closed 5-member vocabulary ──────────


def test_response_field_type_closed_vocab() -> None:
    for t in ("str", "int", "float", "bool", "list_str"):
        assert SubagentResponseField(name="x", type=t, description="d").type == t  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        SubagentResponseField(name="x", type="dict", description="d")  # type: ignore[arg-type]


# ── D5 — SubagentTask: role vocab, bounds, and no session_permission_mode ─────


def test_subagent_role_vocab() -> None:
    assert _task(subagent_role="analyst_readonly").subagent_role == "analyst_readonly"
    with pytest.raises(ValidationError):
        _task(subagent_role="root_admin")


def test_subagent_task_bounds() -> None:
    assert _task(context_refs=[f"vfs://f{i}" for i in range(20)])  # 20 is allowed
    with pytest.raises(ValidationError):
        _task(context_refs=[f"vfs://f{i}" for i in range(21)])
    for bad in (0, 9):
        with pytest.raises(ValidationError):
            _task(max_iterations=bad)
    with pytest.raises(ValidationError):
        _task(description="x" * 4001)


def test_subagent_task_has_no_permission_escalation_field() -> None:
    # Security invariant: a subagent cannot carry a permission mode on the wire.
    assert "session_permission_mode" not in SubagentTask.model_fields


# ── D6 — SubagentResultEnvelope: digest cap, status vocab, safe defaults ──────


def test_raw_digest_capped_at_shared_constant() -> None:
    ok = SubagentResultEnvelope(task_id="t", status="ok", raw_digest="x" * MAX_OBSERVATION_CHARS)
    assert len(ok.raw_digest) == MAX_OBSERVATION_CHARS
    with pytest.raises(ValidationError):
        SubagentResultEnvelope(task_id="t", status="ok", raw_digest="x" * (MAX_OBSERVATION_CHARS + 1))


def test_result_status_closed_vocab() -> None:
    for s in ("ok", "error", "budget_exhausted", "denied"):
        assert SubagentResultEnvelope(task_id="t", status=s, raw_digest="").status == s  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        SubagentResultEnvelope(task_id="t", status="timeout", raw_digest="")  # type: ignore[arg-type]


def test_result_envelope_safe_defaults() -> None:
    env = SubagentResultEnvelope(task_id="t", status="ok", raw_digest="digest")
    assert env.cost_usd == 0.0
    assert env.iterations_used == 0
    assert env.structured_result is None
    assert env.error_message is None


# ── D7 — DispatchBatchResult constructs; winner defaults None ─────────────────


def test_batch_result_winner_defaults_none() -> None:
    batch = DispatchBatchResult(batch_id="b", pattern="tournament", results=[], total_cost_usd=0.0)
    assert batch.winner_task_id is None


# ── D8 — round-trip: DispatchPlan.model_dump() fits the state channel ─────────


def test_plan_model_dump_round_trips() -> None:
    dumped = _plan().model_dump()
    assert isinstance(dumped, dict)
    # A dispatch_plan channel stores exactly this dict; it must re-validate cleanly.
    assert DispatchPlan.model_validate(dumped).pattern == "fanout_and_synthesize"
