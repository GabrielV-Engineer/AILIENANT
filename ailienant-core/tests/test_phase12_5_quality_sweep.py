"""Phase 12.5 — Quality & Polish Debt Sweep.

Focused regression coverage for the parts of the sweep not already exercised by
a sibling suite:
  - DEBT-079 — cross-restart HITL resume recovers the real prompt + thinking
    config from graph state instead of hardcoded defaults, and a legacy
    checkpoint (channels absent) still falls back safely.
  - DEBT-045 — the action_token_usage telemetry round-trip and
    BudgetEstimatorTool's calibrated-vs-static confidence grading.
  - DEBT-052 — the bounded description-embedding cache's hit/eviction behavior
    (INVALID-closure + the two real fixes get their own regression lock here;
    tests/test_skill_resolver.py and test_phase12_3_integration_debts.py cover
    the resolver's existing contract).

DEBT-047 (docstring renderer) and DEBT-012 (diff word-highlight) have their
coverage in tests/test_phase8_8_5_coder_arsenal.py and
ailienant-extension/src/test/diffBlock.test.ts respectively — sibling-file
convention keeps each debt's tests next to the feature it belongs to.
"""
from __future__ import annotations

from types import SimpleNamespace
from typing import Any, Dict, List
from unittest.mock import AsyncMock, patch

import pytest

import core.telemetry as tele
from core.task_service import TaskPayload, TaskService

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


# =====================================================================
# DEBT-079 — cross-restart HITL resume
# =====================================================================


def _fake_snapshot(values: Dict[str, Any], interrupt_value: Any = "approve?") -> Any:
    """A minimal stand-in for langgraph's StateSnapshot — only `.values` and
    `.interrupts` are read by rehydrate_paused_interrupt."""
    interrupt = SimpleNamespace(value=interrupt_value)
    return SimpleNamespace(values=values, interrupts=[interrupt])


async def _run_rehydrate(values: Dict[str, Any]) -> TaskService:
    """Drive rehydrate_paused_interrupt with hybrid_checkpointer/alienant_app faked
    out and the WS emit stubbed, then return the TaskService for assertion."""
    svc = TaskService()
    snapshot = _fake_snapshot(values)

    with patch("brain.checkpoint.hybrid_checkpointer.get_tuple", return_value=None), \
         patch("brain.checkpoint.hybrid_checkpointer.arecover", new=AsyncMock()), \
         patch("brain.engine.alienant_app.aget_state", new=AsyncMock(return_value=snapshot)), \
         patch.object(TaskService, "_emit_interrupt_card", new=AsyncMock()):
        surfaced = await svc.rehydrate_paused_interrupt("sess-1")
    assert surfaced is True
    return svc


async def test_debt079_rehydrate_recovers_prompt_and_thinking_config() -> None:
    """The reconstructed TaskPayload must carry the ORIGINAL prompt and the
    checkpointed reasoning-mode config — not the pre-fix hardcoded
    task_prompt="" / defaults regardless of what was actually running."""
    svc = await _run_rehydrate({
        "execution_mode": "MICRO_SWARM",
        "user_input": "refactor the auth module",
        "enable_native_thinking": False,
        "thinking_budget_tokens": 8192,
    })
    payload, exec_mode = svc._paused_tasks["sess-1"]
    assert isinstance(payload, TaskPayload)
    assert payload.task_prompt == "refactor the auth module"
    assert payload.enable_native_thinking is False
    assert payload.thinking_budget_tokens == 8192
    assert exec_mode == "MICRO_SWARM"


async def test_debt079_legacy_checkpoint_falls_back_to_defaults() -> None:
    """A checkpoint written before the two new channels existed deserializes
    them as absent — the recovered payload must fall back to the exact
    pre-fix literal defaults (True / 4096), never crash or silently zero out."""
    svc = await _run_rehydrate({
        "execution_mode": "SEQUENTIAL",
        "user_input": "some earlier prompt",
        # enable_native_thinking / thinking_budget_tokens deliberately absent.
    })
    payload, _exec_mode = svc._paused_tasks["sess-1"]
    assert payload.task_prompt == "some earlier prompt"
    assert payload.enable_native_thinking is True
    assert payload.thinking_budget_tokens == 4096


async def test_debt079_append_history_no_longer_writes_empty_user_message() -> None:
    """Regression lock for the real bug DEBT-079 turned out to be: the shared
    post-resume path calls _append_history(session_id, "user",
    payload.task_prompt) — with the pre-fix empty task_prompt that wrote a
    blank bubble into the persisted transcript. Prove the recovered prompt
    would no longer be blank on that call."""
    svc = await _run_rehydrate({
        "execution_mode": "SEQUENTIAL",
        "user_input": "the real original prompt",
    })
    payload, _ = svc._paused_tasks["sess-1"]
    assert payload.task_prompt != ""
    assert payload.task_prompt == "the real original prompt"


# =====================================================================
# DEBT-045 — action-token calibration
# =====================================================================


def test_debt045_action_token_roundtrip(tmp_path: Any) -> None:
    tele.init_telemetry_db(str(tmp_path / "telemetry.sqlite"))
    try:
        for tokens in (900, 1000, 1100, 1200, 800):
            tele.log_action_tokens("write_file", tokens, project_id="proj-a")
        stats = tele.action_token_stats("write_file")
        other = tele.action_token_stats("edit_file")
    finally:
        tele.shutdown_telemetry_db()
    assert stats["count"] == 5
    assert stats["median_tokens"] == 1000.0
    assert other["count"] == 0
    assert other["median_tokens"] == 0.0


def test_debt045_uninitialized_db_is_a_safe_noop() -> None:
    tele.shutdown_telemetry_db()  # ensure _conn is None regardless of test order
    tele.log_action_tokens("write_file", 500)  # must not raise
    stats = tele.action_token_stats("write_file")
    assert stats == {"count": 0, "median_tokens": 0.0}


async def test_debt045_estimator_prefers_calibrated_median_above_sample_floor(tmp_path: Any) -> None:
    """Below core.telemetry._ACTION_MIN_SAMPLES the estimator must use the
    static _ACTION_BASE_TOKENS heuristic; once enough real samples exist for
    an action, it switches to the observed median and raises confidence."""
    from brain.state import MissionSpecification, WBSStep
    from tools.planner_tools import BudgetEstimatorTool, _ACTION_BASE_TOKENS

    tele.init_telemetry_db(str(tmp_path / "telemetry.sqlite"))
    try:
        mission = MissionSpecification(
            outcome="done", scope=["a.py"], constraints=[], decisions=[],
            tasks=[
                WBSStep(step_number=1, target_role="core_dev", action="write_file",
                         target_file="a.py", description="d", status="pending"),
            ],
            checks=["c"],
        )
        state: Dict[str, Any] = {
            "session_max_budget_usd": 5.0, "accumulated_session_cost": 0.0,
            "mission_spec": mission,
        }
        tool = BudgetEstimatorTool(state=state)

        # Below the sample floor: static heuristic, "low" confidence.
        import json as _json
        out_low = _json.loads(await tool._arun(include_breakdown=True))
        assert out_low["confidence"] == "low"
        assert out_low["breakdown"][0]["estimated_tokens"] == (
            _ACTION_BASE_TOKENS["write_file"] + len("d") // 4
        )

        # Calibrate write_file with enough real samples that the median (2000)
        # is unambiguously different from the static constant.
        for _ in range(6):
            tele.log_action_tokens("write_file", 2000)

        out_high = _json.loads(await tool._arun(include_breakdown=True))
        assert out_high["confidence"] == "high"
        assert out_high["breakdown"][0]["calibrated"] is True
        assert out_high["breakdown"][0]["estimated_tokens"] == 2000 + len("d") // 4
    finally:
        tele.shutdown_telemetry_db()


# =====================================================================
# DEBT-052 — bounded description-embedding cache
# =====================================================================


async def test_debt052_cache_hit_skips_the_embed_call() -> None:
    from core.skill_resolver import _DescriptionEmbedCache

    cache = _DescriptionEmbedCache(maxsize=8)
    embed_calls: List[str] = []

    async def _embed(text: str) -> List[float]:
        embed_calls.append(text)
        return [1.0, 0.0]

    # First lookup misses and populates; second lookup for the SAME content
    # must not call the embedder again.
    assert cache.get("candidate skill") is None
    vec = await _embed("candidate skill")
    cache.put("candidate skill", vec)
    assert cache.get("candidate skill") == vec
    assert embed_calls == ["candidate skill"]  # only the one deliberate call above


def test_debt052_cache_evicts_oldest_at_maxsize() -> None:
    from core.skill_resolver import _DescriptionEmbedCache

    cache = _DescriptionEmbedCache(maxsize=2)
    cache.put("a", [1.0])
    cache.put("b", [2.0])
    cache.put("c", [3.0])  # evicts "a" (least recently used)
    assert cache.get("a") is None
    assert cache.get("b") == [2.0]
    assert cache.get("c") == [3.0]


async def test_debt052_resolve_active_skills_embeds_description_once_across_calls(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The real regression this closure fixes: an unchanged skill description
    must be embedded once, not once per task/turn."""
    from core import db as catalog_db
    from core import skill_resolver

    monkeypatch.setattr(catalog_db, "DB_CATALOG_PATH", str(tmp_path / "catalog.sqlite"))
    await catalog_db.init_db()
    await catalog_db.upsert_skill("s1", "Match", "body", description="candidate skill")

    embed_calls: List[str] = []

    async def _fake_embed(text: str) -> List[float]:
        embed_calls.append(text)
        return [1.0, 0.0]

    cache = skill_resolver._DescriptionEmbedCache()
    for _ in range(3):
        result = await skill_resolver.resolve_active_skills(
            user_input="query", workspace_root="/ws", invoked_skill_id=None,
            embed_fn=_fake_embed, embed_cache=cache,
        )
        assert [s["name"] for s in result] == ["Match"]

    # The query vector embeds every call (3), but "candidate skill" embeds once.
    assert embed_calls.count("candidate skill") == 1
    assert embed_calls.count("query") == 3
