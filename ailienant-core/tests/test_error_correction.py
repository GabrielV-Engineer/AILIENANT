"""Unit tests for the self-healing ErrorCorrectionAgent + reflexion plumbing.

Covers: a mock error recovers within the budget; the agent concedes (never raises)
on no-fix / LLM failure / foreign-path proposals; the in-turn budget and the
cross-turn failure-signature breaker short-circuit; the graph node clears
healing_required; and the cognitive-isolation fence (no brain.personality import).
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, List

import pytest

from agents.error_correction import (
    ErrorCorrectionAgent,
    attempt_correction,
    candidate_files_from_traceback,
    run_error_correction_node,
)
from brain.failure_breaker import failure_breaker, normalize_signature
from brain.retry_policy import CORRECTION_MAX_ATTEMPTS, FAILURE_SIGNATURE_THRESHOLD

_OFFENDER = "C:\\ws\\pkg\\mod.py"
_ORIGINAL = "def f():\n    return undefined_name\n"
_FIXED = "def f():\n    return 1\n"

# candidate_files_from_traceback() filters frames by os.path.relpath() against
# workspace_root, which is only meaningful for paths in the *host's own*
# format (relpath can't parse a foreign OS's drive-letter/separator convention).
# _OFFENDER above is a Windows-style literal used elsewhere purely as an opaque
# string/dict key, but the two tests that actually exercise the relpath-based
# workspace filter need a real, host-appropriate path pair to test the real
# behavior on every platform, not just Windows.
_WS_ROOT = os.path.join(os.path.abspath(os.sep), "ws")
_WS_OFFENDER = os.path.join(_WS_ROOT, "pkg", "mod.py")
_WS_STDLIB_FRAME = os.path.join(os.path.abspath(os.sep), "stdlib", "json.py")


@pytest.fixture(autouse=True)
def _clear_breaker() -> Any:
    """Reset the process-wide breaker so tests do not leak signature state."""
    failure_breaker._state.clear()
    yield
    failure_breaker._state.clear()


def _stub_invoker(filepath: str, new_content: str, diagnosis: str = "fix") -> Any:
    async def _invoke(system: str, payload: Dict[str, Any]) -> str:
        return json.dumps(
            {"diagnosis": diagnosis, "filepath": filepath, "new_content": new_content}
        )
    return _invoke


def _patch_read(monkeypatch: Any, content: str | None) -> None:
    monkeypatch.setattr(
        ErrorCorrectionAgent,
        "_read_offending_file",
        staticmethod(lambda path, state: content),
    )


# ── propose_fix happy path ───────────────────────────────────────────────────


@pytest.mark.anyio
async def test_recovers_and_emits_hitl_patch(monkeypatch: Any) -> None:
    _patch_read(monkeypatch, _ORIGINAL)
    agent = ErrorCorrectionAgent(llm_invoker=_stub_invoker(_OFFENDER, _FIXED))
    result = await agent.propose_fix(
        traceback_text="NameError", candidate_files=[_OFFENDER], state={}
    )
    assert result.healed
    # Patch flows through the standard HITL channels (never a direct disk write).
    assert result.pending_contents[_OFFENDER] == _FIXED
    assert _OFFENDER in result.pending_patches
    assert result.pending_base_hash[_OFFENDER]  # pre-edit stale-guard anchor present


@pytest.mark.anyio
async def test_concedes_on_no_fix(monkeypatch: Any) -> None:
    _patch_read(monkeypatch, _ORIGINAL)
    agent = ErrorCorrectionAgent(llm_invoker=_stub_invoker("", ""))  # declines
    result = await agent.propose_fix(
        traceback_text="NameError", candidate_files=[_OFFENDER], state={}
    )
    assert not result.healed
    assert not result.pending_contents


@pytest.mark.anyio
async def test_rejects_foreign_path(monkeypatch: Any) -> None:
    _patch_read(monkeypatch, _ORIGINAL)
    agent = ErrorCorrectionAgent(
        llm_invoker=_stub_invoker("C:\\ws\\other.py", _FIXED)
    )
    result = await agent.propose_fix(
        traceback_text="NameError", candidate_files=[_OFFENDER], state={}
    )
    assert not result.healed  # model may only edit the file it was given


@pytest.mark.anyio
async def test_llm_failure_is_conceded_not_raised(monkeypatch: Any) -> None:
    _patch_read(monkeypatch, _ORIGINAL)

    async def _boom(system: str, payload: Dict[str, Any]) -> str:
        raise RuntimeError("gateway down")

    agent = ErrorCorrectionAgent(llm_invoker=_boom)
    result = await agent.propose_fix(
        traceback_text="NameError", candidate_files=[_OFFENDER], state={}
    )
    assert not result.healed  # a saturated loop must never let the failure escape


@pytest.mark.anyio
async def test_unreadable_file_conceded(monkeypatch: Any) -> None:
    _patch_read(monkeypatch, None)  # excluded / binary / too-large / missing
    agent = ErrorCorrectionAgent(llm_invoker=_stub_invoker(_OFFENDER, _FIXED))
    result = await agent.propose_fix(
        traceback_text="NameError", candidate_files=[_OFFENDER], state={}
    )
    assert not result.healed


# ── attempt_correction: budget + breaker ─────────────────────────────────────


@pytest.mark.anyio
async def test_attempt_correction_recovers(monkeypatch: Any) -> None:
    _patch_read(monkeypatch, _ORIGINAL)
    agent = ErrorCorrectionAgent(llm_invoker=_stub_invoker(_OFFENDER, _FIXED))
    result = await attempt_correction(
        ValueError("boom"), {"workspace_root": "C:\\ws"},
        failed_node="coder_agent", extra_candidates=[_OFFENDER], agent=agent,
    )
    assert result is not None and result.healed


@pytest.mark.anyio
async def test_attempt_correction_budget_exhausted(monkeypatch: Any) -> None:
    _patch_read(monkeypatch, _ORIGINAL)
    agent = ErrorCorrectionAgent(llm_invoker=_stub_invoker(_OFFENDER, _FIXED))
    state = {"workspace_root": "C:\\ws", "correction_attempts": CORRECTION_MAX_ATTEMPTS}
    result = await attempt_correction(
        ValueError("boom"), state,
        failed_node="coder_agent", extra_candidates=[_OFFENDER], agent=agent,
    )
    assert result is None  # in-turn budget spent → defer to DLQ


@pytest.mark.anyio
async def test_signature_breaker_short_circuits(monkeypatch: Any) -> None:
    _patch_read(monkeypatch, _ORIGINAL)
    sig = normalize_signature("coder_agent", "ValueError", "boom")
    for _ in range(FAILURE_SIGNATURE_THRESHOLD):
        failure_breaker.record_failure(sig)
    agent = ErrorCorrectionAgent(llm_invoker=_stub_invoker(_OFFENDER, _FIXED))
    result = await attempt_correction(
        ValueError("boom"), {"workspace_root": "C:\\ws"},
        failed_node="coder_agent", extra_candidates=[_OFFENDER], agent=agent,
    )
    assert result is None  # known-unfixable signature → no LLM spend


# ── graph node ───────────────────────────────────────────────────────────────


@pytest.mark.anyio
async def test_run_node_always_clears_healing_required(monkeypatch: Any) -> None:
    _patch_read(monkeypatch, _ORIGINAL)
    monkeypatch.setattr(
        "agents.error_correction._default_agent",
        ErrorCorrectionAgent(llm_invoker=_stub_invoker(_WS_OFFENDER, _FIXED)),
    )
    state: Dict[str, Any] = {
        "healing_required": True,
        "last_error_trace": f'File "{_WS_OFFENDER}", line 2, in f\nNameError',
        "failed_node": "coder_agent",
        "failure_signature": normalize_signature("coder_agent", "NameError", "x"),
        "workspace_root": _WS_ROOT,
    }
    out = await run_error_correction_node(state)
    assert out["healing_required"] is False
    assert out["pending_contents"][os.path.normpath(_WS_OFFENDER)] == _FIXED


@pytest.mark.anyio
async def test_run_node_noop_without_signal() -> None:
    assert await run_error_correction_node({"healing_required": False}) == {}


# ── traceback parsing ────────────────────────────────────────────────────────


def test_candidate_files_filters_to_workspace() -> None:
    tb = (
        f'File "{_WS_STDLIB_FRAME}", line 10, in loads\n'
        f'File "{_WS_OFFENDER}", line 2, in f\n'
    )
    out: List[str] = candidate_files_from_traceback(tb, _WS_ROOT)
    assert out == [os.path.normpath(_WS_OFFENDER)]  # stdlib frame dropped


# ── cognitive-isolation fence ────────────────────────────────────────────────


def test_no_personality_import() -> None:
    src = (Path(__file__).resolve().parent.parent / "agents" / "error_correction.py").read_text(
        encoding="utf-8"
    )
    # Match the import statement forms (the docstring legitimately names the fence).
    assert "from brain.personality" not in src
    assert "\nimport brain.personality" not in src
