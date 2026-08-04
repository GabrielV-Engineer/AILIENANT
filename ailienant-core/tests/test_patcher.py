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
from hypothesis import assume, given
from hypothesis import strategies as st

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


# --- Property-based tests -----------------------------------------------------
#
# Disjoint alphabets for the surrounding text ("abc") vs. the anchor text
# ("xyz") guarantee the anchor can never appear inside or straddle the
# surrounding text, so occurrence counts are deterministic by construction
# rather than relying on Hypothesis to stumble onto a clean example.

_SURROUNDING = st.text(alphabet="abc", max_size=5)
_ANCHOR = st.text(alphabet="xyz", min_size=1, max_size=5)


@given(prefix=_SURROUNDING, suffix=_SURROUNDING, search=_ANCHOR, replace=_ANCHOR)
def test_apply_then_revert_round_trips(
    prefix: str, suffix: str, search: str, replace: str
) -> None:
    """Applying a patch and then applying its inverse returns the original content."""
    assume(search != replace)
    content = prefix + search + suffix
    patched = apply_search_replace(content, search, replace)
    reverted = apply_search_replace(patched, replace, search)
    assert reverted == content


@given(search=_ANCHOR, filler=_SURROUNDING, replace=st.text(alphabet="xyz", max_size=5))
def test_ambiguous_search_always_raises(search: str, filler: str, replace: str) -> None:
    """A search block occurring 2+ times is always rejected, never silently resolved."""
    content = search + filler + search
    with pytest.raises(PatchError, match="Ambiguous"):
        apply_search_replace(content, search, replace)
