# tests/test_analyst_brains.py
"""Analyst "three-brain" tutor + model selector — robustness DoD.

Covers the five binding hardening mandates plus the model-selector wiring:

  1. Idempotent, loop-safe docs-RAG ingestion (single build under concurrency;
     the event loop stays responsive while a build runs).
  2. Chunk-level packing: no partial chunk, the pinned active-file hard-cap
     prevents context overflow, and the 60% soft-cap keeps the lower brains
     from being starved by a dense central-brain result.
  3. Background rebuild: cooperative cancellation on rapid re-trigger and an
     error boundary that never leaves a permanent hung-empty state.
  4. README freshness: small READMEs verbatim, large ones head-sliced then
     digested, the reactive rebuild debounced against a save storm.
  5. Sparse-preset directional model fallback.

  + The selected answer tier flows into generation and shrinks the context
    budget without touching retrieval.
"""
from __future__ import annotations

import asyncio
import contextlib
from types import SimpleNamespace
from typing import Any, AsyncIterator, List

import pytest

from agents.analyst_context import (
    ContextChunk, ContextBudgetManager, assemble_analyst_context,
    _ANALYST_BUDGET_BY_TIER,
)
from tools.token_counter import PrecisionTokenCounter

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


# ── Mandate 5: directional model fallback ──────────────────────────────

def test_directional_order_endpoints() -> None:
    from core.config.model_resolver import _directional_order
    assert _directional_order("small") == ["small", "medium", "big", "cloud"]
    assert _directional_order("cloud") == ["cloud", "big", "medium", "small"]


def test_get_chat_target_sparse_preset_resolves(monkeypatch: pytest.MonkeyPatch) -> None:
    import core.config.model_resolver as mr
    # Preset defines only medium + cloud (small/big null).
    targets = {
        "medium": SimpleNamespace(provider="openai", model="m/medium"),
        "cloud": SimpleNamespace(provider="openai", model="m/cloud"),
    }
    monkeypatch.setattr(mr, "_cached", targets)
    # Missing 'small' steps up to 'medium'; missing 'big' steps to the nearest (cloud).
    assert mr.get_chat_target("small").model == "m/medium"   # type: ignore[union-attr]
    assert mr.get_chat_target("big").model == "m/cloud"      # type: ignore[union-attr]
    assert mr.get_chat_target("cloud").model == "m/cloud"    # type: ignore[union-attr]


# ── Mandate 2: chunk-level packing, pinned hard-cap, anti-starvation ───

def test_pack_never_emits_partial_chunk() -> None:
    chunks = [
        ContextChunk(body="# Codex\nIDENTITY_TOK", brain="codex", label="codex"),
        ContextChunk(body="=== PROJECT README ===\n" + "readline\n" * 80,
                     brain="readme", label="readme"),
    ]
    block, _ = ContextBudgetManager(30).pack(chunks)        # tiny budget
    assert "IDENTITY_TOK" in block                          # CODEX pinned survives
    # The README either appears whole (header + body) or not at all — never half.
    assert ("=== PROJECT README ===" in block) == ("readline" in block)


def test_pack_pinned_files_hardcapped_no_overflow() -> None:
    budget = 600
    files = [ContextChunk(body="FILETOK " * 400, brain="file", label=f"f{i}")
             for i in range(15)]
    codex = ContextChunk(body="IDENTITY_TOK", brain="codex", label="codex")
    block, dropped = ContextBudgetManager(budget).pack([codex, *files])
    assert PrecisionTokenCounter.count(block) <= budget     # never exceeds the window
    assert dropped                                          # most huge files dropped
    assert "IDENTITY_TOK" in block


def test_pack_soft_cap_prevents_starvation() -> None:
    # Each GraphRAG chunk is ~600 tokens; three of them (~1800) would, uncapped,
    # consume the whole 1500-token budget and starve docs + readme. The 60%
    # soft-cap (~900) bounds GraphRAG to one chunk, leaving room for the rest.
    graph = [ContextChunk(body="GRAGTOK " * 150, brain="graphrag", label=f"g{i}")
             for i in range(3)]
    docs = [ContextChunk(body="DOCSTOK " * 75, brain="docs", label="d0")]
    readme = [ContextChunk(body="RDMETOK " * 75, brain="readme", label="r0")]
    block, _ = ContextBudgetManager(1500).pack([*graph, *docs, *readme])
    assert "GRAGTOK" in block        # central brain present
    assert "DOCSTOK" in block        # docs not starved
    assert "RDMETOK" in block        # readme not starved


# ── Mandate 1: idempotent + loop-safe ingestion ───────────────────────

async def test_docs_index_single_build_under_concurrency(monkeypatch: pytest.MonkeyPatch) -> None:
    import core.memory.docs_index as di
    calls = {"n": 0}
    fresh = {"v": False}

    async def fake_build() -> None:
        calls["n"] += 1
        await asyncio.sleep(0.05)
        fresh["v"] = True

    monkeypatch.setattr(di, "_index_is_fresh", lambda: fresh["v"])
    monkeypatch.setattr(di, "_build_index", fake_build)
    await asyncio.gather(*[di.ensure_docs_index() for _ in range(8)])
    assert calls["n"] == 1           # concurrent first-uses collapse to one build


async def test_docs_index_build_keeps_loop_responsive(monkeypatch: pytest.MonkeyPatch) -> None:
    import core.memory.docs_index as di
    fresh = {"v": False}

    async def fake_build() -> None:
        await asyncio.sleep(0.2)
        fresh["v"] = True

    monkeypatch.setattr(di, "_index_is_fresh", lambda: fresh["v"])
    monkeypatch.setattr(di, "_build_index", fake_build)
    ticks = {"n": 0}

    async def ticker() -> None:
        for _ in range(20):
            await asyncio.sleep(0.01)
            ticks["n"] += 1

    await asyncio.gather(di.ensure_docs_index(), ticker())
    assert ticks["n"] >= 5           # the loop kept running during the build


# ── Mandate 3: rebuild cancellation + error boundary ──────────────────

async def test_rebuild_cancels_previous(monkeypatch: pytest.MonkeyPatch) -> None:
    import core.memory.docs_index as di

    async def slow_ensure(*, force: bool = False) -> None:
        await asyncio.sleep(1.0)

    monkeypatch.setattr(di, "ensure_docs_index", slow_ensure)
    di._current_rebuild_task = None
    di._rebuild_in_flight = False

    di.request_rebuild()
    t1 = di._current_rebuild_task
    await asyncio.sleep(0)
    di.request_rebuild()
    t2 = di._current_rebuild_task
    await asyncio.sleep(0.03)

    assert t1 is not t2
    assert t1 is not None and t1.cancelled()
    assert di._rebuild_in_flight is True
    assert t2 is not None
    t2.cancel()
    with contextlib.suppress(asyncio.CancelledError, Exception):
        await t2


async def test_rebuild_error_boundary_resets_state(monkeypatch: pytest.MonkeyPatch) -> None:
    import core.memory.docs_index as di

    async def boom(*, force: bool = False) -> None:
        raise RuntimeError("lancedb I/O down")

    monkeypatch.setattr(di, "ensure_docs_index", boom)
    di._current_rebuild_task = None
    di._rebuild_in_flight = False

    di.request_rebuild()
    task = di._current_rebuild_task
    assert task is not None
    await task                       # must NOT raise (error swallowed)
    assert di._rebuild_in_flight is False
    assert di._current_rebuild_task is None


# ── Mandate 4: README freshness, debounce, efficiency ─────────────────

def test_readme_small_used_verbatim() -> None:
    import core.readme_digest as rd
    out = rd.get_readme_brain("/proj", lambda _p: "# Hi\nA small readme.")
    assert out == "# Hi\nA small readme."


def test_readme_none_when_absent() -> None:
    import core.readme_digest as rd
    assert rd.get_readme_brain("/proj", lambda _p: None) is None


async def test_readme_large_head_slice_then_debounced_digest(monkeypatch: pytest.MonkeyPatch) -> None:
    import core.readme_digest as rd
    big = "# Title\n" + ("paragraph line of text\n" * 2000)   # > 5 KB
    builds = {"n": 0}

    async def fake_digest(_text: str) -> str:
        builds["n"] += 1
        return "DIGEST_RESULT"

    monkeypatch.setattr(rd, "_build_digest", fake_digest)
    monkeypatch.setattr(rd, "_DEBOUNCE_S", 0.05)
    rd._digest_cache.clear()
    rd._pending.clear()

    out = rd.get_readme_brain("/proj", lambda _p: big)
    assert out is not None and out.startswith("# Title")      # head-slice
    assert "DIGEST_RESULT" not in out                         # digest not ready yet

    for _ in range(10):                                       # save storm
        rd.schedule_digest("/proj", lambda _p: big)
    await asyncio.sleep(0.2)
    assert builds["n"] == 1                                   # debounced to ONE build
    assert rd.get_readme_brain("/proj", lambda _p: big) == "DIGEST_RESULT"


# ── e2e: tier shrinks the budget but not retrieval; tier reaches the model ──

async def test_smaller_tier_trims_context() -> None:
    rag = [("a.py", "CODE " * 800)]
    docs = [("HowToUseIt.md", "HELP " * 800)]
    small = await assemble_analyst_context(
        [], None, "s", rag_snippets=rag, docs_snippets=docs, tier="small")
    cloud = await assemble_analyst_context(
        [], None, "s", rag_snippets=rag, docs_snippets=docs, tier="cloud")
    small_tok = PrecisionTokenCounter.count(small)
    cloud_tok = PrecisionTokenCounter.count(cloud)
    assert small_tok <= cloud_tok
    assert small_tok <= _ANALYST_BUDGET_BY_TIER["small"]


async def test_generate_reply_forwards_tier(monkeypatch: pytest.MonkeyPatch) -> None:
    from agents import analyst
    captured: dict[str, Any] = {}

    async def fake_astream(messages: List[dict], tier: str = "medium",
                           session_id: str = "") -> AsyncIterator[str]:
        captured["tier"] = tier
        for _ in ():                 # async generator that yields nothing
            yield ""

    monkeypatch.setattr("tools.llm_gateway.LLMGateway.astream_byom", fake_astream)
    out: List[str] = []
    async for chunk in analyst.generate_analyst_reply_stream("hi", tier="big"):
        out.append(chunk)
    assert captured["tier"] == "big"
