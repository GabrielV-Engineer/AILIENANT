"""Server-side unified-diff transport (DEBT-024).

The HITL approval ships an O(Δ) unified diff instead of full file content. These
tests pin the producer's two invariants:
  - line endings are normalized to LF so a CRLF working copy cannot desync the
    host's applyPatch reconstruction (R1);
  - the diff is self-consistent: applied to the old side it reproduces the new
    side byte-for-byte (the round-trip the host performs with `applyPatch`).
"""
from __future__ import annotations

from core.blob_storage import _apply_unified_diff
from core.task_service import _normalize_eol, compute_unified_diff


def test_unified_diff_normalizes_crlf() -> None:
    old = "a\r\nb\r\nc\r\n"
    new = "a\r\nB\r\nc\r\n"
    diff = compute_unified_diff(old, new, "f.py")
    assert "\r" not in diff  # CRLF collapsed before diffing


def test_unified_diff_round_trips_on_edit() -> None:
    old = "def f():\n    return 1\n"
    new = "def f():\n    return 2\n"
    diff = compute_unified_diff(old, new, "calc.py")
    assert _apply_unified_diff(_normalize_eol(old), diff) == _normalize_eol(new)


def test_unified_diff_round_trips_on_create() -> None:
    new = "line one\nline two\n"
    diff = compute_unified_diff("", new, "new.py")
    assert _apply_unified_diff("", diff) == new


def test_unified_diff_empty_on_no_change() -> None:
    same = "x = 1\n"
    assert compute_unified_diff(same, same, "f.py") == ""
