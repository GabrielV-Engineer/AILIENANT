# tests/test_response_cache.py
"""AST-Hashed Semantic Response Cache — unit + integration tests.

DoD:
 - identical intent + unchanged context → cache hit (gateway not invoked)
 - one byte edit → miss → re-invoked
 - dirty-buffered planner turns bypass the cache
 - LRU evicts under cap; reverse index stays bounded (no leak)
 - TTL expiry returns None
 - active invalidation drops only the targeted entries
"""
from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from core.response_cache import SemanticResponseCache


# ── helpers ──────────────────────────────────────────────────────────────────


def _fresh(
    max_entries: int = 256,
    ttl_s: float = 1800.0,
    t: float = 0.0,
) -> tuple[SemanticResponseCache, list[float]]:
    """A cache with an injected clock so time is deterministic."""
    clock: list[float] = [t]
    return SemanticResponseCache(max_entries=max_entries, ttl_s=ttl_s, time_fn=lambda: clock[0]), clock


def _fake_llm_response(content: str) -> Any:
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content))])


# ── 1. Unit — hit ─────────────────────────────────────────────────────────────


def test_cache_hit_returns_stored_value() -> None:
    cache, _ = _fresh()
    key = cache.build_key(
        intent="edit|main.py|add type hints",
        context=[("main.py", "def foo(): pass")],
        project_id="proj1",
        model="ailienant/big",
    )
    cache.store(key, '{"edits": []}', ["main.py"])
    assert cache.probe(key) == '{"edits": []}'


# ── 2. Unit — one-byte edit → miss ────────────────────────────────────────────


def test_one_byte_edit_produces_miss() -> None:
    cache, _ = _fresh()
    original = "def foo(): pass"
    modified = "def foo(): passX"  # one byte added

    key_a = cache.build_key(
        intent="edit|main.py|add type hints",
        context=[("main.py", original)],
        project_id="proj1",
        model="ailienant/big",
    )
    key_b = cache.build_key(
        intent="edit|main.py|add type hints",
        context=[("main.py", modified)],
        project_id="proj1",
        model="ailienant/big",
    )
    cache.store(key_a, '{"edits": []}', ["main.py"])
    assert key_a != key_b
    assert cache.probe(key_b) is None  # miss — different content-hash


# ── 3. Unit — project / model scoping ─────────────────────────────────────────


def test_different_project_ids_yield_distinct_keys() -> None:
    cache, _ = _fresh()
    ctx = [("main.py", "def foo(): pass")]
    k1 = cache.build_key(intent="i", context=ctx, project_id="proj-A", model="big")
    k2 = cache.build_key(intent="i", context=ctx, project_id="proj-B", model="big")
    k3 = cache.build_key(intent="i", context=ctx, project_id="proj-A", model="small")
    assert k1 != k2
    assert k1 != k3


# ── 4. Unit — LRU cap + reverse-index GC (Architect comment #1) ──────────────


def test_lru_eviction_does_not_leak_reverse_index() -> None:
    """LRU eviction must scrub the evicted key from _paths / _key_paths."""
    cache, _ = _fresh(max_entries=2)
    k1 = cache.build_key(intent="a", context=[("f1.py", "a")], project_id="p", model="m")
    k2 = cache.build_key(intent="b", context=[("f2.py", "b")], project_id="p", model="m")
    k3 = cache.build_key(intent="c", context=[("f3.py", "c")], project_id="p", model="m")

    cache.store(k1, "resp1", ["f1.py"])
    cache.store(k2, "resp2", ["f2.py"])
    cache.store(k3, "resp3", ["f3.py"])  # triggers eviction of k1 (LRU)

    # k1 is gone from the forward cache
    assert cache.probe(k1) is None
    # f1.py must NOT appear in the reverse index (the leak the Architect flagged)
    assert "f1.py" not in cache._paths
    # k1 must NOT appear in _key_paths either
    assert k1 not in cache._key_paths
    # k2 and k3 still live
    assert cache.probe(k2) == "resp2"
    assert cache.probe(k3) == "resp3"


# ── 5. Unit — TTL expiry ──────────────────────────────────────────────────────


def test_ttl_expiry_returns_none() -> None:
    cache, clock = _fresh(ttl_s=60.0)
    key = cache.build_key(
        intent="i", context=[("f.py", "x")], project_id="p", model="m"
    )
    cache.store(key, "old", ["f.py"])
    clock[0] = 61.0  # advance past TTL
    assert cache.probe(key) is None


# ── 6. Unit — active invalidation spares unrelated entries ────────────────────


def test_active_invalidation_drops_only_targeted_entries() -> None:
    cache, _ = _fresh()
    k_a = cache.build_key(intent="i", context=[("a.py", "a")], project_id="p", model="m")
    k_b = cache.build_key(intent="i", context=[("b.py", "b")], project_id="p", model="m")
    cache.store(k_a, "resp_a", ["a.py"])
    cache.store(k_b, "resp_b", ["b.py"])

    cache.invalidate_path("a.py")

    assert cache.probe(k_a) is None          # evicted
    assert cache.probe(k_b) == "resp_b"      # unaffected
    assert "a.py" not in cache._paths        # reverse index cleaned


# ── 7. Integration — coder hit: gateway not invoked twice ─────────────────────


@pytest.mark.anyio
async def test_coder_cache_hit_gateway_called_once_then_skipped() -> None:
    """run_coder_node over an identical state twice must invoke the LLM only once."""
    from core.vfs_middleware import VFSReadResult
    from brain.state import MissionSpecification, WBSStep
    from core.response_cache import response_cache

    response_cache.clear()

    step = WBSStep(
        step_number=1,
        target_role="core_dev",
        action="edit_file",
        target_file="main.py",
        description="Add type hints.",
        status="pending",
    )
    mission = MissionSpecification(
        outcome=".", scope=["main.py"], constraints=["-"], decisions=["-"],
        tasks=[step], checks=["-"],
    )
    state: dict[str, Any] = {
        "task_id": "cache-test", "mission_spec": mission, "current_step_id": 1,
        "retry_count": 0, "errors": [], "security_flags": [],
        "validation_feedback": None, "project_id": "testproj",
    }

    mock_ainvoke = AsyncMock(return_value=_fake_llm_response('{"edits": []}'))

    with patch("api.websocket_manager.vfs_manager.emit_graph_mutation", new=AsyncMock()), \
         patch("core.memory.semantic_memory.SemanticMemoryManager.search_snippets",
               new=AsyncMock(return_value=[])), \
         patch("core.vfs_middleware.VFSMiddleware.read_safe",
               return_value=VFSReadResult(content="def foo(): return 1\n")), \
         patch("tools.llm_gateway.LLMGateway.ainvoke", mock_ainvoke):
        from agents.coder import run_coder_node
        await run_coder_node(dict(state))   # first — miss → LLM called
        await run_coder_node(dict(state))   # second — hit → LLM NOT called

    assert mock_ainvoke.call_count == 1

    # Third call with different file content → miss again
    mock_ainvoke.reset_mock()
    with patch("api.websocket_manager.vfs_manager.emit_graph_mutation", new=AsyncMock()), \
         patch("core.memory.semantic_memory.SemanticMemoryManager.search_snippets",
               new=AsyncMock(return_value=[])), \
         patch("core.vfs_middleware.VFSMiddleware.read_safe",
               return_value=VFSReadResult(content="def foo(): return 2\n")), \
         patch("tools.llm_gateway.LLMGateway.ainvoke", mock_ainvoke):
        await run_coder_node(dict(state))

    assert mock_ainvoke.call_count == 1  # re-invoked on changed content

    response_cache.clear()


# ── 8. Integration — planner dirty-buffer bypass ─────────────────────────────


@pytest.mark.anyio
async def test_planner_dirty_buffer_bypass_and_clean_cache_hit() -> None:
    """Dirty buffers → cache disabled (both calls invoke LLM).
    Clean state  → second call is a cache hit (LLM called once)."""
    from contextlib import ExitStack
    from unittest.mock import MagicMock as MM
    from core.response_cache import response_cache
    from brain.state import MissionSpecification, WBSStep

    response_cache.clear()

    def _broker_decision() -> MM:  # type: ignore[return]
        d = MM(); d.cancelled = False; d.effective_model = "ailienant/big"; d.holds_lock = False
        return d

    def _make_response(content: str) -> MM:  # type: ignore[return]
        r = MM(); r.choices = [MM(message=MM(content=content))]
        return r

    mission_json = MissionSpecification(
        outcome="Test.", scope=["s.py"], constraints=["-"], decisions=["-"],
        tasks=[WBSStep(step_number=1, target_role="core_dev", action="read_file",
                       target_file="s.py", description="stub")],
        checks=["-"],
    ).model_dump_json()

    mock_ainvoke = AsyncMock(return_value=_make_response(mission_json))
    mock_acquire = AsyncMock(return_value=_broker_decision())

    # The planner extracts dirty_buffers from ide_context (not the top-level key).
    dirty_ide = {"dirty_buffers": [{"path": "s.py", "content": "x = 1"}]}
    dirty_state: dict[str, Any] = {
        "task_id": "plan-dirty", "user_input": "Add a feature.",
        "workspace_root": "/ws", "project_id": "ptest",
        "context_metrics": None, "mission_spec": None, "immutable_wbs": None,
        "errors": [], "retry_count": 0, "current_cost_usd": 0.0,
        "max_budget_usd": 10.0, "vfs_buffer": {}, "terminal_output": "",
        "parallel_tasks": [], "tci": 45.0, "css": 78.5, "provider": "LOCAL",
        "current_step_id": None,
        "dirty_buffers": [],
        "ide_context": dirty_ide,  # <-- dirty buffers live here
        "researcher_skeleton": None,
    }
    clean_state = {**dirty_state, "ide_context": {}}  # no dirty buffers

    from agents.planner import run_planner_node

    with ExitStack() as stack:
        stack.enter_context(patch("agents.planner.DEBUG_MODE", False))
        traj_cls = stack.enter_context(patch("agents.planner.TrajectoryMemoryManager"))
        stack.enter_context(patch("agents.planner.LLMGateway.ainvoke", mock_ainvoke))
        stack.enter_context(patch("agents.planner.ResourceBroker.acquire_or_resolve", mock_acquire))
        stack.enter_context(patch("agents.planner.ResourceBroker.release", AsyncMock()))
        traj_cls.return_value.search = AsyncMock(return_value=[])

        # Dirty: two identical turns both invoke the LLM (bypass)
        await run_planner_node(dict(dirty_state))
        await run_planner_node(dict(dirty_state))
        assert mock_ainvoke.call_count == 2

        # Clean: first turn is a miss; second is a hit
        mock_ainvoke.reset_mock()
        await run_planner_node(dict(clean_state))   # miss → LLM
        await run_planner_node(dict(clean_state))   # hit  → LLM NOT called
        assert mock_ainvoke.call_count == 1

    response_cache.clear()
