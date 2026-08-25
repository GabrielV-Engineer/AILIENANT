# tests/test_litellm_leak_guard.py
"""DEBT-201 — the litellm patch-leakage guard (`tests/conftest.py`).

A live incident saw a cascade of real `litellm.exceptions.BadRequestError`
failures followed by one flaky e2e failure three tests later — the shape of a
test that patched `litellm.aembedding`/`acompletion` and did not restore it,
corrupting whichever test ran next. It never reproduced in isolation, so the
exact leaking test was never identified; `_guard_litellm_patch_leakage`
instead self-heals any such leak and logs the offending test's nodeid.

These tests exercise `_litellm_leak_guard`, the plain context manager the
autouse fixture wraps, directly — proving the restore-and-log behavior
without depending on pytest's own fixture ordering or on ever reproducing
the original leak.
"""
from __future__ import annotations

import logging

import litellm
import pytest

from tests.conftest import _litellm_leak_guard


def test_restores_a_leaked_aembedding_and_logs_the_offending_test(
    caplog: pytest.LogCaptureFixture,
) -> None:
    real = litellm.aembedding
    with caplog.at_level(logging.ERROR, logger="AILIENANT_TEST_ISOLATION"):
        with _litellm_leak_guard():
            # Simulate a test that patches litellm directly and forgets to
            # restore it (the exact leak DEBT-201 could never pin down).
            litellm.aembedding = lambda *a, **k: None  # type: ignore[assignment]

    assert litellm.aembedding is real
    assert "litellm.aembedding leaked" in caplog.text


def test_restores_a_leaked_acompletion_and_logs_the_offending_test(
    caplog: pytest.LogCaptureFixture,
) -> None:
    real = litellm.acompletion
    with caplog.at_level(logging.ERROR, logger="AILIENANT_TEST_ISOLATION"):
        with _litellm_leak_guard():
            litellm.acompletion = lambda *a, **k: None  # type: ignore[assignment]

    assert litellm.acompletion is real
    assert "litellm.acompletion leaked" in caplog.text


def test_well_behaved_patch_restored_within_the_block_is_silent(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A test that patches and restores litellm itself (the normal case, e.g.
    a `with patch(...)` block or the `monkeypatch` fixture) must never trip
    the guard or log anything."""
    real_aembedding = litellm.aembedding
    real_acompletion = litellm.acompletion

    with caplog.at_level(logging.ERROR, logger="AILIENANT_TEST_ISOLATION"):
        with _litellm_leak_guard():
            fake = lambda *a, **k: None  # noqa: E731
            litellm.aembedding = fake
            litellm.acompletion = fake
            litellm.aembedding = real_aembedding
            litellm.acompletion = real_acompletion

    assert litellm.aembedding is real_aembedding
    assert litellm.acompletion is real_acompletion
    assert caplog.text == ""


def test_nothing_patched_is_silent(caplog: pytest.LogCaptureFixture) -> None:
    real_aembedding = litellm.aembedding
    real_acompletion = litellm.acompletion

    with caplog.at_level(logging.ERROR, logger="AILIENANT_TEST_ISOLATION"):
        with _litellm_leak_guard():
            pass

    assert litellm.aembedding is real_aembedding
    assert litellm.acompletion is real_acompletion
    assert caplog.text == ""
