# tests/test_envelope_unwrap.py
"""Phase 7.10.4 DoD — envelope-tolerant structured JSON (ADR-704 / G5).

LLMGateway._extract_nested_schema_target must recover the real schema object from the
envelopes local/BYOM models routinely emit: markdown fences, conversational prose, and
top-level / nested wrapper keys. Eight cases drive the contract:

  E1. markdown-fenced object        E5. invalid JSON → {} (graceful)
  E2. top-level key wrapper         E6. already-flat object unchanged
  E3. conversational prose          E7. no-match → base dict (Pydantic fails natively)
  E4. deeply nested wrapper         E8. real MissionSpecification envelope round-trips
"""
from __future__ import annotations

import json

import pytest
from pydantic import BaseModel

from brain.state import MissionSpecification, WBSStep
from tools.llm_gateway import LLMGateway

_unwrap = LLMGateway._extract_nested_schema_target


class _Probe(BaseModel):
    alpha: int
    beta: str


# ── E1 — markdown-fenced object ──────────────────────────────────────────────


def test_unwrap_markdown_fenced() -> None:
    raw = '```json\n{"alpha": 1, "beta": "x"}\n```'
    assert _unwrap(raw, _Probe) == {"alpha": 1, "beta": "x"}


# ── E2 — top-level envelope key ──────────────────────────────────────────────


def test_unwrap_top_level_key_wrapper() -> None:
    raw = '{"Envelope": {"alpha": 1, "beta": "x"}}'
    assert _unwrap(raw, _Probe) == {"alpha": 1, "beta": "x"}


# ── E3 — conversational prose around the object ──────────────────────────────


def test_unwrap_strips_conversational_prose() -> None:
    raw = 'Sure! Here is the result:\n{"alpha": 2, "beta": "y"}\nHope that helps.'
    assert _unwrap(raw, _Probe) == {"alpha": 2, "beta": "y"}


# ── E4 — deeply nested wrapper ───────────────────────────────────────────────


def test_unwrap_deeply_nested() -> None:
    raw = '{"json": {"result": {"alpha": 3, "beta": "z"}}}'
    assert _unwrap(raw, _Probe) == {"alpha": 3, "beta": "z"}


# ── E5 — invalid JSON degrades gracefully ────────────────────────────────────


def test_unwrap_invalid_json_returns_empty() -> None:
    extracted = _unwrap("this is not json at all", _Probe)
    assert extracted == {}
    # The caller feeds {} to model_validate, which raises a native ValidationError.
    with pytest.raises(Exception):
        _Probe.model_validate(extracted)


# ── E6 — already-flat object is returned unchanged ───────────────────────────


def test_unwrap_flat_object_unchanged() -> None:
    assert _unwrap('{"alpha": 4, "beta": "w"}', _Probe) == {"alpha": 4, "beta": "w"}


# ── E7 — no match → base dict so Pydantic still fails loudly ─────────────────


def test_unwrap_no_match_returns_base_dict() -> None:
    extracted = _unwrap('{"foo": 1, "bar": 2}', _Probe)
    assert extracted == {"foo": 1, "bar": 2}
    with pytest.raises(Exception):
        _Probe.model_validate(extracted)


# ── E8 — real enveloped MissionSpecification round-trips ─────────────────────


def test_unwrap_real_mission_specification_envelope() -> None:
    mission = MissionSpecification(
        outcome="Add an endpoint.",
        scope=["api.py"],
        constraints=["no new deps"],
        decisions=["use FastAPI"],
        tasks=[WBSStep(step_number=1, target_role="core_dev", action="edit_file",
                       target_file="api.py", description="add route")],
        checks=["tests green"],
    )
    enveloped = json.dumps({"MissionSpecification": mission.model_dump()})

    extracted = _unwrap(enveloped, MissionSpecification)
    # The unwrapped dict validates back into the model without raising.
    revalidated = MissionSpecification.model_validate(extracted)
    assert revalidated.outcome == "Add an endpoint."
    assert len(revalidated.tasks) == 1


# ─────────────────────────────────────────────────────────────────────────
# All-optional schemas. `required` is empty, and every dict is a superset of
# the empty set, so the plain walk matches the OUTERMOST node — validating a
# foreign envelope into a fully-defaulted instance without raising. For a
# schema whose default carries meaning (an empty GrillQuestionBatch means "no
# further questions"), that turns a malformed answer into a confident wrong one.
# ─────────────────────────────────────────────────────────────────────────


class _AllOptional(BaseModel):
    questions: list[str] = []


def test_all_optional_schema_prefers_a_node_carrying_a_declared_field() -> None:
    """A nested wrapper whose inner node declares `questions` must be unwrapped
    to that node, not matched at the envelope."""
    enveloped = json.dumps({
        "interview_goal": "gather requirements",
        "sections": [{"section_name": "Purpose", "questions": ["What is the goal?"]}],
    })

    extracted = _unwrap(enveloped, _AllOptional)

    assert extracted.get("questions") == ["What is the goal?"]
    assert _AllOptional.model_validate(extracted).questions == ["What is the goal?"]


def test_all_optional_schema_still_unwraps_a_flat_object() -> None:
    """The declared-field preference must not disturb the already-correct case."""
    flat = json.dumps({"questions": ["a", "b"]})

    assert _unwrap(flat, _AllOptional) == {"questions": ["a", "b"]}


def test_all_optional_schema_falls_back_when_no_declared_field_exists() -> None:
    """A response carrying nothing this schema declares still returns a dict, so
    the caller's own validation (not this helper) decides what to do with it."""
    foreign = json.dumps({"totally": "unrelated"})

    assert _unwrap(foreign, _AllOptional) == {"totally": "unrelated"}
