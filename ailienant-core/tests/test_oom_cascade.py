"""Phase 6.8 — OOM Cascade telemetry & resilience suite (formalises Phase 6.3).

Covers the four scenarios of the OOM rescue path in ``tools/llm_gateway.py``:
context-window overflow, CUDA / VRAM OOM, double-fault propagation, and the
``oom_fallback_events`` telemetry row. Async cases run via ``asyncio.run`` so the
suite needs no ``pytest-asyncio`` dependency (mirrors ``test_audit_chain.py``).
``litellm.acompletion`` is monkeypatched with a stateful stub.
"""
import asyncio
import sqlite3
from typing import Any, List, Optional

import litellm
import pytest
from litellm import ModelResponse
from litellm.exceptions import APIConnectionError, ContextWindowExceededError

from core import telemetry
from shared.config import MODEL_MEDIUM
from tools.llm_gateway import LLMGateway, _looks_like_oom


class _Acompletion:
    """Stateful ``litellm.acompletion`` stub.

    ``raise_seq`` is consumed one entry per call: an exception entry is raised,
    a ``None`` entry returns a fresh ``ModelResponse``. Calls beyond the
    sequence also return a ``ModelResponse``.
    """

    def __init__(self, raise_seq: List[Optional[BaseException]]) -> None:
        self.raise_seq = list(raise_seq)
        self.calls = 0

    async def __call__(self, **kwargs: Any) -> ModelResponse:
        idx = self.calls
        self.calls += 1
        exc = self.raise_seq[idx] if idx < len(self.raise_seq) else None
        if exc is not None:
            raise exc
        return ModelResponse()


def _ctx_err() -> ContextWindowExceededError:
    return ContextWindowExceededError(
        message="context window exceeded",
        model=MODEL_MEDIUM,
        llm_provider="ollama",
    )


def _cuda_err() -> APIConnectionError:
    return APIConnectionError(
        message="CUDA error: out of memory on device 0",
        llm_provider="ollama",
        model=MODEL_MEDIUM,
    )


def test_looks_like_oom_regex() -> None:
    """``_looks_like_oom`` matches CUDA / VRAM phrasing, not generic failures."""
    assert _looks_like_oom(_cuda_err()) is True
    assert _looks_like_oom(RuntimeError("connection reset by peer")) is False


def test_context_overflow_cascade(monkeypatch: pytest.MonkeyPatch) -> None:
    """ContextWindowExceededError → cascade re-emits to cloud, state marked."""
    stub = _Acompletion([_ctx_err(), None])
    monkeypatch.setattr(litellm, "acompletion", stub)
    state: dict[str, Any] = {"task_id": "t-ctx"}

    resp = asyncio.run(
        LLMGateway.ainvoke([{"role": "user", "content": "hi"}], state=state)
    )

    assert isinstance(resp, ModelResponse)
    assert stub.calls == 2  # local OOM + cloud rescue
    assert state["oom_fallback_active"] is True
    assert "OOM_FALLBACK_ENGAGED:context_overflow" in state["security_flags"]


def test_cuda_oom_cascade(monkeypatch: pytest.MonkeyPatch) -> None:
    """APIConnectionError matching /cuda|out of memory/ → cascade fires."""
    stub = _Acompletion([_cuda_err(), None])
    monkeypatch.setattr(litellm, "acompletion", stub)
    state: dict[str, Any] = {"task_id": "t-cuda"}

    resp = asyncio.run(
        LLMGateway.ainvoke([{"role": "user", "content": "hi"}], state=state)
    )

    assert isinstance(resp, ModelResponse)
    assert stub.calls == 2
    assert state["oom_fallback_active"] is True
    assert "OOM_FALLBACK_ENGAGED:cuda_oom" in state["security_flags"]


def test_double_fault_propagates(monkeypatch: pytest.MonkeyPatch) -> None:
    """Local AND cloud OOM → the second exception propagates out of ainvoke.

    The cascade is sequential, not recursive: a second OOM on the cloud model
    leaves ``_oom_cascade`` uncaught — the ``dead_letter_decorator`` (Phase 6.4)
    is the intended catcher, not exercised here.
    """
    stub = _Acompletion([_ctx_err(), _ctx_err()])
    monkeypatch.setattr(litellm, "acompletion", stub)

    with pytest.raises(ContextWindowExceededError):
        asyncio.run(
            LLMGateway.ainvoke(
                [{"role": "user", "content": "hi"}], state={"task_id": "t-dbl"}
            )
        )
    assert stub.calls == 2


def test_oom_telemetry_row(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """A successful cascade writes exactly one ``oom_fallback_events`` row."""
    db = str(tmp_path / "telem.sqlite")
    telemetry.init_telemetry_db(db)
    try:
        stub = _Acompletion([_ctx_err(), None])
        monkeypatch.setattr(litellm, "acompletion", stub)
        asyncio.run(
            LLMGateway.ainvoke(
                [{"role": "user", "content": "hello"}],
                state={"task_id": "sess-tele"},
            )
        )
        conn = sqlite3.connect(db)
        rows = conn.execute(
            "SELECT session_id, event, reason, original_model, fallback_model, "
            "tokens_at_failure, swap_latency_ms FROM oom_fallback_events"
        ).fetchall()
        conn.close()
    finally:
        telemetry.shutdown_telemetry_db()

    assert len(rows) == 1
    session_id, event, reason, original, fallback, tokens, latency = rows[0]
    assert session_id == "sess-tele"
    assert event == "oom_fallback"
    assert reason == "context_overflow"
    assert original == MODEL_MEDIUM
    assert fallback  # env-configured cloud fallback model, non-empty
    assert tokens >= 0
    assert latency >= 0.0
