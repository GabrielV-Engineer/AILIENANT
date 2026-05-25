# ailienant-core/tests/test_restore_conversation.py
"""Phase 7.9.B.20 — session memory rehydration on reopen.

restore_conversation re-seeds short-term chat memory from a persisted transcript
so a reopened session keeps continuity, without clobbering a live conversation.
"""
from __future__ import annotations

import pytest

from core import task_service as ts_mod
from core.task_service import TaskService, _MAX_HISTORY_MESSAGES


@pytest.fixture(autouse=True)
def _clear_memory():
    ts_mod._conversations.clear()
    yield
    ts_mod._conversations.clear()


def _svc() -> TaskService:
    return TaskService()


def test_restore_seeds_when_absent() -> None:
    _svc().restore_conversation(
        "s1",
        [{"role": "user", "content": "hola"}, {"role": "assistant", "content": "soy AILIENANT"}],
    )
    assert ts_mod._conversations["s1"] == [
        {"role": "user", "content": "hola"},
        {"role": "assistant", "content": "soy AILIENANT"},
    ]


def test_restore_is_seed_if_absent_never_clobbers() -> None:
    ts_mod._conversations["s1"] = [{"role": "user", "content": "live turn"}]
    _svc().restore_conversation("s1", [{"role": "user", "content": "stale"}])
    assert ts_mod._conversations["s1"] == [{"role": "user", "content": "live turn"}]


def test_restore_filters_and_bounds() -> None:
    msgs = [{"role": "user", "content": f"m{i}"} for i in range(_MAX_HISTORY_MESSAGES + 10)]
    msgs += [{"role": "system", "content": "drop me"}, {"role": "assistant", "content": ""}]
    _svc().restore_conversation("s2", msgs)
    restored = ts_mod._conversations["s2"]
    assert len(restored) == _MAX_HISTORY_MESSAGES
    assert all(m["role"] in ("user", "assistant") and m["content"] for m in restored)
    assert restored[-1]["content"] == f"m{_MAX_HISTORY_MESSAGES + 9}"


def test_restore_empty_is_noop() -> None:
    _svc().restore_conversation("s3", [])
    assert "s3" not in ts_mod._conversations
