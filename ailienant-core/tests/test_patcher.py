# ailienant-core/tests/test_patcher.py
#
# Phase 2.22 DoD: pytest tests/test_patcher.py -v → 0 failures.
#
# Coverage:
#   apply_search_replace:
#     1. Exact match — correct substitution
#     2. CRLF content + LF search block — normalized pass succeeds
#     3. Failure: search block not found → PatchError
#     4. Failure: search block matches multiple times → PatchError (ambiguous)

import pytest

from core.exceptions import PatchError
from core.patcher import apply_search_replace


def test_exact_match_replaces_correctly() -> None:
    """Exact match: search found once → correct substitution returned."""
    content = "def foo():\n    return 1\n"
    result = apply_search_replace(content, "return 1", "return 42")
    assert result == "def foo():\n    return 42\n"


def test_crlf_content_matches_lf_search_block() -> None:
    """CRLF vs LF: normalized pass must succeed when line endings differ."""
    content = "line1\r\nline2\r\nline3\r\n"
    search = "line1\nline2\nline3"
    replace = "lineA\nlineB\nlineC"
    result = apply_search_replace(content, search, replace)
    assert "lineA" in result
    assert "lineB" in result
    assert "lineC" in result
    assert "line1" not in result


def test_raises_patch_error_when_search_not_found() -> None:
    """Not found: PatchError raised with diagnostic 'not found' message."""
    content = "hello world\n"
    with pytest.raises(PatchError, match="not found"):
        apply_search_replace(content, "goodbye world", "hi world")


def test_raises_patch_error_when_search_matches_multiple_times() -> None:
    """Ambiguous: PatchError raised when search block appears more than once."""
    content = "x = 1\nx = 1\n"
    with pytest.raises(PatchError, match="Ambiguous"):
        apply_search_replace(content, "x = 1", "x = 99")
