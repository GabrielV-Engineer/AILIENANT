"""Directed suite for the extracted transactional candidate tournament.

Covers the extraction contract: the re-export shim keeps ``select_candidate_via_mcts``
importable from ``brain.agentic_cell`` with byte-identical behavior, and the new
``run_tournament_from_dispatch`` adapter maps a ``DispatchBatchResult`` of subagent-proposed
edits into the same transactional UCB1 engine — including its default vs. pluggable extraction,
the surface-isolation contamination warning, and the degenerate-count edge cases.

Async cases run inside ``asyncio.run`` so the stub surface lives on one event loop across the
push/verify/rollback sequence.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Dict, List, Optional, Tuple

import pytest

import brain.agentic_cell as ac
import brain.subagent_tournament as st
from brain.subagent_contracts import DispatchBatchResult, SubagentResultEnvelope
from brain.subagent_tournament import run_tournament, run_tournament_from_dispatch
from core.blob_storage import ContentAddressableStorage
from core.workspace_sync import SyncSurface, _raw_sha256


# ── Stubs ─────────────────────────────────────────────────────────────────────


class StubSyncSurface(SyncSurface):
    """In-memory work surface that records every write (for rollback assertions)."""

    def __init__(self, initial: Optional[Dict[str, bytes]] = None) -> None:
        self._files: Dict[str, bytes] = dict(initial or {})
        self.write_log: List[Tuple[str, bytes]] = []

    async def write_file(self, rel_path: str, content: bytes) -> None:
        self._files[rel_path] = content
        self.write_log.append((rel_path, content))

    async def read_file(self, rel_path: str) -> Optional[bytes]:
        return self._files.get(rel_path)

    async def get_file_hashes(self) -> Dict[str, str]:
        return {p: _raw_sha256(c) for p, c in self._files.items()}


def _verify_good(surface: StubSyncSurface) -> "object":
    async def run_verify(_cmd: str) -> Tuple[int, str]:
        content = (await surface.read_file("a.py") or b"").decode("utf-8")
        return (0, "") if "good" in content else (1, "FAILED")

    return run_verify


def _envelope(task_id: str, result: Optional[Dict[str, object]], status: str = "ok") -> SubagentResultEnvelope:
    return SubagentResultEnvelope(
        task_id=task_id, status=status, structured_result=result, raw_digest=""  # type: ignore[arg-type]
    )


def _batch(results: List[SubagentResultEnvelope]) -> DispatchBatchResult:
    return DispatchBatchResult(
        batch_id="b0", pattern="tournament", results=results, total_cost_usd=0.0
    )


# ── Extraction / shim identity ──────────────────────────────────────────────────


def test_shim_reexports_run_tournament() -> None:
    """agentic_cell keeps select_candidate_via_mcts as the run_tournament re-export."""
    assert ac.select_candidate_via_mcts is run_tournament
    assert callable(ac.select_candidate_via_mcts)
    # _content_to_vfs is re-imported for the cell's own use, kept single-sourced.
    assert ac._content_to_vfs is st._content_to_vfs


# ── Relocated body behaves identically ──────────────────────────────────────────


def test_run_tournament_selects_best_verdict() -> None:
    """>=2 candidates → UCB1 over the structured verdict picks the passing one."""
    store = ContentAddressableStorage()
    surface = StubSyncSurface()
    candidates = [{"a.py": "BROKEN\n"}, {"a.py": "good\n"}]

    async def body() -> Tuple[int, Dict[str, str]]:
        return await run_tournament(
            surface=surface,
            clean_base_content={"a.py": "BASE\n"},
            candidates=candidates,
            verify_command="pytest",
            run_verify=_verify_good(surface),  # type: ignore[arg-type]
            blob_store=store,
            mission_state=None,
        )

    winner_index, winner = asyncio.run(body())
    assert winner_index == 1
    assert winner["a.py"] == "good\n"
    assert surface._files["a.py"].decode("utf-8") == "good\n"  # ends at the winner


# ── Adapter: default extraction ─────────────────────────────────────────────────


def test_from_dispatch_default_extraction_picks_winner() -> None:
    """ok envelopes whose structured_result is path-keyed → tournament, winner_task_id resolved."""
    store = ContentAddressableStorage()
    surface = StubSyncSurface()
    batch = _batch([
        _envelope("t0", {"a.py": "BROKEN\n"}),
        _envelope("t1", {"a.py": "good\n"}),
    ])

    async def body() -> Tuple[int, Dict[str, str], Optional[str]]:
        return await run_tournament_from_dispatch(
            batch,
            surface=surface,
            clean_base_content={"a.py": "BASE\n"},
            verify_command="pytest",
            run_verify=_verify_good(surface),  # type: ignore[arg-type]
            blob_store=store,
            mission_state=None,
        )

    winner_index, winner, winner_task_id = asyncio.run(body())
    assert winner_index == 1
    assert winner["a.py"] == "good\n"
    assert winner_task_id == "t1"


def test_from_dispatch_skips_non_ok_and_empty() -> None:
    """error / no-string-map envelopes are filtered before the tournament runs."""
    store = ContentAddressableStorage()
    surface = StubSyncSurface()
    batch = _batch([
        _envelope("err", {"a.py": "good\n"}, status="error"),   # non-ok → skipped
        _envelope("meta", {"score": 5}),                        # no str field → skipped
        _envelope("t1", {"a.py": "good\n"}),                    # the only survivor
    ])

    async def body() -> Tuple[int, Dict[str, str], Optional[str]]:
        return await run_tournament_from_dispatch(
            batch,
            surface=surface,
            clean_base_content={"a.py": "BASE\n"},
            verify_command="pytest",
            run_verify=_verify_good(surface),  # type: ignore[arg-type]
            blob_store=store,
            mission_state=None,
        )

    winner_index, winner, winner_task_id = asyncio.run(body())
    assert winner_index == 0          # single survivor is at filtered index 0
    assert winner_task_id == "t1"
    assert winner["a.py"] == "good\n"


def test_from_dispatch_accepts_dict_batch() -> None:
    """A state-stored model_dump() dict is normalized via model_validate."""
    store = ContentAddressableStorage()
    surface = StubSyncSurface()
    batch = _batch([_envelope("t0", {"a.py": "good\n"})]).model_dump()

    async def body() -> Tuple[int, Dict[str, str], Optional[str]]:
        return await run_tournament_from_dispatch(
            batch,
            surface=surface,
            clean_base_content={"a.py": "BASE\n"},
            verify_command="pytest",
            run_verify=_verify_good(surface),  # type: ignore[arg-type]
            blob_store=store,
            mission_state=None,
        )

    _idx, _winner, winner_task_id = asyncio.run(body())
    assert winner_task_id == "t0"


# ── Adapter: pluggable extractor (F1) ───────────────────────────────────────────


def test_from_dispatch_custom_extractor_honored() -> None:
    """A semantic (non-path-keyed) schema is mapped by a caller-supplied extractor."""
    store = ContentAddressableStorage()
    surface = StubSyncSurface()
    batch = _batch([
        _envelope("t0", {"patch_path": "a.py", "patch_body": "BROKEN\n", "note": "x"}),
        _envelope("t1", {"patch_path": "a.py", "patch_body": "good\n", "note": "y"}),
    ])

    def extractor(env: SubagentResultEnvelope) -> Optional[Dict[str, str]]:
        sr = env.structured_result or {}
        path, body = sr.get("patch_path"), sr.get("patch_body")
        if isinstance(path, str) and isinstance(body, str):
            return {path: body}
        return None

    async def body() -> Tuple[int, Dict[str, str], Optional[str]]:
        return await run_tournament_from_dispatch(
            batch,
            surface=surface,
            clean_base_content={"a.py": "BASE\n"},
            verify_command="pytest",
            run_verify=_verify_good(surface),  # type: ignore[arg-type]
            blob_store=store,
            mission_state=None,
            candidate_extractor=extractor,
        )

    _idx, winner, winner_task_id = asyncio.run(body())
    assert winner["a.py"] == "good\n"
    assert winner_task_id == "t1"


# ── Adapter: edge cases (F5 / F7) ───────────────────────────────────────────────


def test_from_dispatch_empty_batch_raises() -> None:
    """No usable candidate → fail-fast ValueError (never an empty-list tournament)."""
    batch = _batch([_envelope("err", {"a.py": "x\n"}, status="error")])

    async def body() -> object:
        return await run_tournament_from_dispatch(
            batch,
            surface=StubSyncSurface(),
            clean_base_content={"a.py": "BASE\n"},
            verify_command="pytest",
            run_verify=_verify_good(StubSyncSurface()),  # type: ignore[arg-type]
            blob_store=ContentAddressableStorage(),
            mission_state=None,
        )

    try:
        asyncio.run(body())
    except ValueError as exc:
        assert "no usable candidate" in str(exc)
    else:  # pragma: no cover - guard
        raise AssertionError("expected ValueError on an all-filtered batch")


def test_from_dispatch_warns_on_out_of_base_path(caplog: pytest.LogCaptureFixture) -> None:
    """A candidate introducing a path outside the clean base is evaluated but logged (DEBT-104)."""
    store = ContentAddressableStorage()
    surface = StubSyncSurface()
    # Single survivor introduces new.py, absent from the base → contamination warning.
    batch = _batch([_envelope("t0", {"a.py": "good\n", "new.py": "extra\n"})])

    async def body() -> Tuple[int, Dict[str, str], Optional[str]]:
        return await run_tournament_from_dispatch(
            batch,
            surface=surface,
            clean_base_content={"a.py": "BASE\n"},
            verify_command="pytest",
            run_verify=_verify_good(surface),  # type: ignore[arg-type]
            blob_store=store,
            mission_state=None,
        )

    with caplog.at_level(logging.WARNING, logger="SUBAGENT_TOURNAMENT"):
        _idx, _winner, winner_task_id = asyncio.run(body())

    assert winner_task_id == "t0"  # still evaluated, not rejected
    assert any("new.py" in rec.message and "DEBT-104" in rec.message for rec in caplog.records)
