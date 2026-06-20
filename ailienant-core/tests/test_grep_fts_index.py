"""GrepTool content-index pre-filter (DEBT-041).

Three contracts:
  - ``_extract_fts_literal`` lifts only a safe, guaranteed-contiguous literal
    (>=3 chars), bailing to None on alternation / optional chars / short runs.
  - The FTS5 trigram line index narrows a catalog path set to a SUPERSET of the
    true matches: an indexed hit is kept, an indexed non-hit is dropped, and an
    un-indexed (index-lag) file is never dropped.
  - GrepTool, given a narrow_provider, still finds every true match (no miss),
    and honours the wall-clock scan deadline.
"""
from __future__ import annotations

import asyncio
from typing import Any, Dict, List, Optional

import pytest

from core import db as catalog_db
from tools.researcher_tools import (
    GrepTool,
    _extract_fts_literal,
    make_fts_narrow_provider,
)


def _isolate_catalog(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> str:
    db = str(tmp_path / "catalog_test.sqlite")
    monkeypatch.setattr(catalog_db, "DB_CATALOG_PATH", db)
    return db


# ── _extract_fts_literal ──────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "pattern,expected",
    [
        ("getUserById", "getUserById"),       # plain literal kept whole
        (r"colou?r", "colo"),                 # optional 'u' excluded; "colo" remains
        (r"foo.*bar", "foo"),                 # longest run before a metachar
        ("a|b", None),                        # alternation → unsafe → None
        ("ab", None),                         # < 3 chars → None
        (r"\.py$", None),                     # escaped/short → None
        (r"def\s+handler", "handler"),        # longest safe run wins
    ],
)
def test_extract_fts_literal(pattern: str, expected: Optional[str]) -> None:
    assert _extract_fts_literal(pattern) == expected


# ── FTS5 narrowing superset invariant ─────────────────────────────────────────


@pytest.mark.skipif(
    not catalog_db._fts5_available(), reason="SQLite build lacks FTS5/trigram"
)
def test_fts_narrow_catalog_is_a_superset(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    _isolate_catalog(tmp_path, monkeypatch)

    async def _run() -> Optional[List[str]]:
        await catalog_db.init_db()
        # Two indexed files: one contains the literal, one does not.
        await catalog_db.index_file_lines("hit.py", "value = compute_total()\n", "proj")
        await catalog_db.index_file_lines("miss.py", "value = 1\n", "proj")
        # A third catalog path is NOT indexed (index lag) — must never be dropped.
        catalog = ["hit.py", "miss.py", "lagging.py"]
        return await catalog_db.fts_narrow_catalog("proj", "compute", catalog)

    narrowed = asyncio.run(_run())
    assert narrowed is not None
    assert "hit.py" in narrowed          # indexed + matches the literal
    assert "lagging.py" in narrowed      # un-indexed → always a candidate
    assert "miss.py" not in narrowed     # indexed + cannot contain "compute"


@pytest.mark.skipif(
    not catalog_db._fts5_available(), reason="SQLite build lacks FTS5/trigram"
)
def test_fts_narrow_catalog_full_scans_on_short_literal(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    _isolate_catalog(tmp_path, monkeypatch)

    async def _run() -> Optional[List[str]]:
        await catalog_db.init_db()
        return await catalog_db.fts_narrow_catalog("proj", "ab", ["x.py"])

    assert asyncio.run(_run()) is None  # < 3 chars → caller must full-scan


# ── GrepTool no-miss with narrowing ───────────────────────────────────────────


@pytest.mark.anyio
async def test_grep_with_narrowing_never_misses_unindexed_match(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A live RAM file the index has never seen must still be searched and matched."""
    if not catalog_db._fts5_available():
        pytest.skip("SQLite build lacks FTS5/trigram")
    _isolate_catalog(tmp_path, monkeypatch)
    await catalog_db.init_db()
    # Only "indexed.py" is in the line index; "fresh.py" is a brand-new RAM buffer.
    await catalog_db.index_file_lines("indexed.py", "x = 1\n", "proj")

    buffers: Dict[str, str] = {
        "indexed.py": "x = 1\n",
        "fresh.py": "result = compute_total(items)\n",  # matches, but un-indexed
    }

    async def _paths() -> List[str]:
        return ["indexed.py", "fresh.py"]

    def _reader(path: str) -> Optional[str]:
        return buffers.get(path)

    class _FakeVfs:
        def snapshot_paths(self) -> List[str]:
            return []  # no RAM override claimed; fresh.py rides the index-lag path

    narrow = make_fts_narrow_provider("proj", vfs=_FakeVfs())
    tool = GrepTool(path_provider=_paths, content_reader=_reader, narrow_provider=narrow)

    result = await tool._arun(pattern="compute_total")
    assert "fresh.py:1:" in result  # narrowing kept the un-indexed file → match found


@pytest.mark.anyio
async def test_grep_scan_deadline_returns_without_hanging(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Past the wall-clock deadline the scan aborts and returns (no hang)."""
    import tools.researcher_tools as rt

    monkeypatch.setattr(rt, "_GREP_SCAN_DEADLINE_S", -1.0)  # deadline already elapsed

    async def _paths() -> List[str]:
        return ["a.py", "b.py"]

    def _reader(path: str) -> Optional[str]:  # pragma: no cover — never reached
        raise AssertionError("scan must abort before reading any file past the deadline")

    tool = GrepTool(path_provider=_paths, content_reader=_reader)
    result = await tool._arun(pattern="anything")
    assert "No matches" in result


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"
