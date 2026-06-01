"""Reactive incremental indexing: content-hash idempotency, circuit breaker, purge.

Exercises ``ReactiveIndexer`` and ``_ReactiveBreaker`` directly with the heavy
collaborators (process pool, LanceDB, SQLite, VFS) mocked, so the behavioural
contract is asserted without a live backend:
- a byte-identical re-save is a no-op (no AST dispatch, no re-embed),
- a real change re-indexes and stores the new hash under the real project_id,
- the per-key breaker opens after a failure streak, sheds, half-opens after the
  cooldown, resets on success, isolates poison-pill files, and is cleared on purge,
- delete purges both the graph and the vector,
- an empty (telemetry) body is resolved from the VFS before indexing.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional
from unittest.mock import AsyncMock

import pytest

import core.indexer as idx
from core.indexer import ReactiveIndexer, _ReactiveBreaker, _FAIL_THRESHOLD
from shared.contracts import IndexingResult

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


# ── Fakes ───────────────────────────────────────────────────────────────────


def _ok_result(file_path: str, imports: Optional[List[str]] = None) -> IndexingResult:
    return IndexingResult(
        file_path=file_path,
        symbol_count=1,
        language_id="python",
        success=True,
        imports=imports or [],
    )


class _FakeClock:
    """Manually advanced monotonic clock for deterministic breaker tests."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, secs: float) -> None:
        self.now += secs


def _patch_db(
    monkeypatch: pytest.MonkeyPatch,
    *,
    stored_hash: Optional[str] = None,
) -> Dict[str, Any]:
    """Stub the core.db calls the indexer holds references to. Returns spies."""
    spies: Dict[str, Any] = {
        "upsert_indexed_file": AsyncMock(),
        "upsert_dependencies": AsyncMock(),
        "purge_file_nodes": AsyncMock(),
        "get_indexed_hash": AsyncMock(return_value=stored_hash),
    }
    for name, mock in spies.items():
        monkeypatch.setattr(idx, name, mock)
    return spies


def _patch_pool(monkeypatch: pytest.MonkeyPatch, result: Any) -> AsyncMock:
    """Stub compute_pool.run to return (or raise) ``result`` without a real process."""
    run = AsyncMock()
    if isinstance(result, Exception):
        run.side_effect = result
    else:
        run.return_value = result
    monkeypatch.setattr(idx.compute_pool, "run", run)
    return run


def _patch_semantic(
    monkeypatch: pytest.MonkeyPatch, *, upsert_ok: bool = True
) -> Dict[str, AsyncMock]:
    """Stub the deferred SemanticMemoryManager import with spies on upsert/delete."""
    import core.memory.semantic_memory as sem

    upsert = AsyncMock(return_value=upsert_ok)
    delete = AsyncMock()

    class _FakeManager:
        def __init__(self, *a: Any, **k: Any) -> None:
            pass

        semantic_upsert = upsert
        semantic_delete = delete

    monkeypatch.setattr(sem, "SemanticMemoryManager", _FakeManager)
    return {"upsert": upsert, "delete": delete}


def _patch_vfs(
    monkeypatch: pytest.MonkeyPatch, *, content: Optional[str], ok: bool = True
) -> None:
    """Stub the deferred VFSMiddleware import so empty-body saves resolve content."""
    import core.vfs_middleware as vfsmod

    class _Res:
        def __init__(self) -> None:
            self.ok = ok
            self.content = content
            self.error = None if ok else "vfs-miss"

    class _FakeVFS:
        def __init__(self, *a: Any, **k: Any) -> None:
            pass

        def read_safe(self, *a: Any, **k: Any) -> Any:
            return _Res()

    monkeypatch.setattr(vfsmod, "VFSMiddleware", _FakeVFS)


def _noop() -> None:
    pass


# ── Idempotency ───────────────────────────────────────────────────────────────


async def test_unchanged_content_is_skipped(monkeypatch: pytest.MonkeyPatch) -> None:
    """A save whose hash matches the stored hash neither parses nor re-embeds."""
    content = "def f():\n    return 1\n" * 20  # enough to be a real file
    import hashlib

    digest = hashlib.sha256(content.encode("utf-8", errors="replace")).hexdigest()
    _patch_db(monkeypatch, stored_hash=digest)
    run = _patch_pool(monkeypatch, _ok_result("f.py"))
    sem = _patch_semantic(monkeypatch)

    await ReactiveIndexer().index("f.py", content, "proj", "/ws", _noop)

    run.assert_not_called()
    sem["upsert"].assert_not_called()


async def test_changed_content_reindexes_and_stores_hash(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Different content → AST dispatch + embed + the new hash is persisted."""
    content = "x = 1\n" * 50
    spies = _patch_db(monkeypatch, stored_hash="stale-hash")
    run = _patch_pool(monkeypatch, _ok_result("f.py", imports=["os"]))
    sem = _patch_semantic(monkeypatch)
    deps_changed = {"n": 0}

    await ReactiveIndexer().index(
        "f.py", content, "proj", "/ws", lambda: deps_changed.__setitem__("n", 1)
    )

    run.assert_awaited_once()
    sem["upsert"].assert_awaited_once()
    # Stored under the REAL project_id, with the freshly-computed hash.
    import hashlib

    digest = hashlib.sha256(content.encode("utf-8", errors="replace")).hexdigest()
    spies["upsert_indexed_file"].assert_awaited_once_with(
        "f.py", "proj", content_hash=digest
    )
    spies["upsert_dependencies"].assert_awaited_once_with("f.py", ["os"], "proj")
    assert deps_changed["n"] == 1  # on_deps_changed fired (PPR scheduled)


async def test_project_id_threaded_to_embed(monkeypatch: pytest.MonkeyPatch) -> None:
    """Vector upsert runs under the real project_id, never the empty orphan partition."""
    _patch_db(monkeypatch, stored_hash=None)
    _patch_pool(monkeypatch, _ok_result("f.py"))
    sem = _patch_semantic(monkeypatch)

    await ReactiveIndexer().index("f.py", "y = 2\n" * 50, "realproj", "/ws", _noop)

    _, kwargs = sem["upsert"].call_args
    assert kwargs["workspace_hash"] == "realproj"


# ── Empty body → VFS resolution ────────────────────────────────────────────────


async def test_empty_body_resolved_from_vfs(monkeypatch: pytest.MonkeyPatch) -> None:
    """A telemetry save (content="") pulls the freshest bytes from the VFS."""
    _patch_db(monkeypatch, stored_hash=None)
    run = _patch_pool(monkeypatch, _ok_result("f.py"))
    _patch_semantic(monkeypatch)
    _patch_vfs(monkeypatch, content="resolved = True\n" * 50, ok=True)

    await ReactiveIndexer().index("f.py", "", "proj", "/ws", _noop)

    run.assert_awaited_once()


async def test_empty_body_vfs_miss_is_noop(monkeypatch: pytest.MonkeyPatch) -> None:
    """If the VFS cannot resolve the body, nothing is indexed."""
    _patch_db(monkeypatch, stored_hash=None)
    run = _patch_pool(monkeypatch, _ok_result("f.py"))
    _patch_semantic(monkeypatch)
    _patch_vfs(monkeypatch, content=None, ok=False)

    await ReactiveIndexer().index("f.py", "", "proj", "/ws", _noop)

    run.assert_not_called()


# ── Circuit breaker ─────────────────────────────────────────────────────────────


def test_breaker_opens_after_threshold_and_sheds() -> None:
    clock = _FakeClock()
    b = _ReactiveBreaker(clock)
    key = "proj\x00f.py"

    for _ in range(_FAIL_THRESHOLD):
        assert b.allow(key)
        b.record_failure(key)

    # Now OPEN: shed until the cooldown elapses.
    assert not b.allow(key)
    clock.advance(idx._COOLDOWN_S - 0.1)
    assert not b.allow(key)
    # Cooldown elapsed → one half-open trial is permitted.
    clock.advance(0.2)
    assert b.allow(key)


def test_breaker_success_resets_state() -> None:
    clock = _FakeClock()
    b = _ReactiveBreaker(clock)
    key = "proj\x00f.py"
    for _ in range(_FAIL_THRESHOLD):
        b.record_failure(key)
    assert not b.allow(key)
    b.record_success(key)
    assert b.allow(key)  # CLOSED again, zero residual state


def test_breaker_isolates_distinct_keys() -> None:
    clock = _FakeClock()
    b = _ReactiveBreaker(clock)
    bad, good = "proj\x00bad.py", "proj\x00good.py"
    for _ in range(_FAIL_THRESHOLD):
        b.record_failure(bad)
    assert not b.allow(bad)
    assert b.allow(good)  # a poison-pill file never trips a healthy one


def test_breaker_failed_halfopen_restarts_cooldown() -> None:
    clock = _FakeClock()
    b = _ReactiveBreaker(clock)
    key = "proj\x00f.py"
    for _ in range(_FAIL_THRESHOLD):
        b.record_failure(key)
    clock.advance(idx._COOLDOWN_S + 1)
    assert b.allow(key)          # half-open trial
    b.record_failure(key)        # trial failed → re-open
    assert not b.allow(key)      # shedding again from the new opened_at


async def test_embed_failure_trips_breaker(monkeypatch: pytest.MonkeyPatch) -> None:
    """A downed embedding backend (upsert returns False) feeds the breaker."""
    _patch_db(monkeypatch, stored_hash=None)
    _patch_pool(monkeypatch, _ok_result("f.py"))
    _patch_semantic(monkeypatch, upsert_ok=False)

    ri = ReactiveIndexer()
    for _ in range(_FAIL_THRESHOLD):
        await ri.index("f.py", "z = 3\n" * 50, "proj", "/ws", _noop)

    # Breaker is now OPEN for this key → the next attempt is shed (no pool dispatch).
    run2 = _patch_pool(monkeypatch, _ok_result("f.py"))
    await ri.index("f.py", "z = 3\n" * 50, "proj", "/ws", _noop)
    run2.assert_not_called()


# ── Purge ───────────────────────────────────────────────────────────────────────


async def test_purge_removes_graph_and_vector(monkeypatch: pytest.MonkeyPatch) -> None:
    spies = _patch_db(monkeypatch)
    sem = _patch_semantic(monkeypatch)

    await ReactiveIndexer().purge("gone.py", "proj", "/ws")

    spies["purge_file_nodes"].assert_awaited_once_with("gone.py", "proj")
    sem["delete"].assert_awaited_once()
    _, kwargs = sem["delete"].call_args
    assert kwargs["workspace_hash"] == "proj"


async def test_purge_clears_breaker_state(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_db(monkeypatch)
    _patch_semantic(monkeypatch)
    ri = ReactiveIndexer()
    key = "proj\x00gone.py"
    for _ in range(_FAIL_THRESHOLD):
        ri._breaker.record_failure(key)
    assert not ri._breaker.allow(key)

    await ri.purge("gone.py", "proj", "/ws")

    assert ri._breaker.allow(key)  # breaker residue evicted with the file
