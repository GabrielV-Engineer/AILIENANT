# tests/test_hybrid_routing.py
"""Phase 3.4.8 DoD — Hybrid Local/Cloud routing, Circuit Breaker, Supreme Judge, TokenLedger."""
from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agents.mcts_coder import (
    MAX_LOCAL_ATTEMPTS,
    evaluate_node_reward,
    generate_local_variant,
    local_fix_with_retry,
    surgeon_escalation,
)
from brain.mcts.tree import MCTSTree
from brain.state import MissionSpecification, WBSStep
from core.token_ledger import token_ledger
from tools.llm_gateway import LLMGateway, Tier
from tools.validation.result import PipelineResult


def _fake_llm_response(
    content: str,
    prompt_tokens: int = 10,
    completion_tokens: int = 5,
) -> SimpleNamespace:
    """Mimic litellm ModelResponse.choices[0].message.content + .usage."""
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=content))],
        usage=SimpleNamespace(
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
        ),
    )


def _make_mission(outcome: str = "test") -> MissionSpecification:
    return MissionSpecification(
        outcome=outcome,
        scope=["x"],
        constraints=["y"],
        decisions=["z"],
        tasks=[
            WBSStep(
                step_number=1,
                target_role="Refactor",
                action="write_file",
                target_file="foo.py",
                description="d",
            )
        ],
        checks=["c"],
    )


@pytest.fixture(autouse=True)
def _reset_ledger() -> Any:
    token_ledger.reset()
    yield
    token_ledger.reset()


# ---------- TokenLedger direct ----------

def test_token_ledger_records_local_and_cloud() -> None:
    token_ledger.record_local(100, 50)
    token_ledger.record_cloud(20, 10)
    snap = token_ledger.snapshot()
    assert snap["local_tokens"] == 150.0
    assert snap["cloud_tokens"] == 30.0


def test_token_ledger_snapshot_includes_savings() -> None:
    token_ledger.record_local(1000, 0)
    token_ledger.record_cloud(500, 0)
    snap = token_ledger.snapshot()
    # Savings: 1000 * (0.030 - 0.001) / 1000 = 0.029
    assert snap["estimated_savings_usd"] == pytest.approx(0.029, rel=1e-3)
    # Invested: 500 * 0.030 / 1000 = 0.015
    assert snap["estimated_invested_usd"] == pytest.approx(0.015, rel=1e-3)


def test_token_ledger_reset_clears_counts() -> None:
    token_ledger.record_local(50, 25)
    token_ledger.record_cloud(10, 5)
    token_ledger.reset()
    snap = token_ledger.snapshot()
    assert snap["local_tokens"] == 0.0
    assert snap["cloud_tokens"] == 0.0


# ---------- LLMGateway tier kwarg + token accounting ----------

@pytest.mark.anyio
async def test_ainvoke_tier_local_records_to_local() -> None:
    fake = _fake_llm_response("ok", prompt_tokens=42, completion_tokens=8)
    with patch(
        "tools.llm_gateway.litellm.acompletion",
        new=AsyncMock(return_value=fake),
    ):
        await LLMGateway.ainvoke(messages=[{"role": "user", "content": "x"}], tier=Tier.LOCAL)
    snap = token_ledger.snapshot()
    assert snap["local_tokens"] == 50.0   # 42 + 8
    assert snap["cloud_tokens"] == 0.0


@pytest.mark.anyio
async def test_ainvoke_tier_cloud_records_to_cloud() -> None:
    fake = _fake_llm_response("ok", prompt_tokens=20, completion_tokens=15)
    with patch(
        "tools.llm_gateway.litellm.acompletion",
        new=AsyncMock(return_value=fake),
    ):
        await LLMGateway.ainvoke(messages=[{"role": "user", "content": "x"}], tier=Tier.CLOUD)
    snap = token_ledger.snapshot()
    assert snap["cloud_tokens"] == 35.0
    assert snap["local_tokens"] == 0.0


@pytest.mark.anyio
async def test_ainvoke_without_tier_classifies_model_big_as_cloud() -> None:
    from shared.config import MODEL_BIG
    fake = _fake_llm_response("ok", prompt_tokens=5, completion_tokens=2)
    with patch(
        "tools.llm_gateway.litellm.acompletion",
        new=AsyncMock(return_value=fake),
    ):
        await LLMGateway.ainvoke(
            messages=[{"role": "user", "content": "x"}],
            model=MODEL_BIG,
        )
    snap = token_ledger.snapshot()
    assert snap["cloud_tokens"] == 7.0
    assert snap["local_tokens"] == 0.0


@pytest.mark.anyio
async def test_ainvoke_tier_overrides_explicit_model() -> None:
    """When both `tier` and `model` are passed, `tier` wins."""
    from shared.config import MODEL_SMALL
    fake = _fake_llm_response("ok", prompt_tokens=3, completion_tokens=1)
    call_capture: dict[str, Any] = {}

    async def _capture(**kwargs: Any) -> SimpleNamespace:
        call_capture.update(kwargs)
        return fake

    with patch("tools.llm_gateway.litellm.acompletion", new=_capture):
        await LLMGateway.ainvoke(
            messages=[{"role": "user", "content": "x"}],
            model=MODEL_SMALL,
            tier=Tier.CLOUD,
        )
    from shared.config import MODEL_BIG
    assert call_capture["model"] == MODEL_BIG
    snap = token_ledger.snapshot()
    assert snap["cloud_tokens"] == 4.0


# ---------- Local Fixer Loop ----------

@pytest.mark.anyio
async def test_local_fix_with_retry_passes_on_first_attempt() -> None:
    """If validate_delta passes immediately, no LLM call is made."""
    tree = MCTSTree(root_state=_make_mission(), root_vfs_view={})
    node = tree.expand(tree.root_id, "a", {}, _make_mission())
    pipeline_ok = PipelineResult(passed=True)
    mock_ainvoke = AsyncMock(return_value=_fake_llm_response("unused"))
    with patch(
        "agents.mcts_coder.validate_delta",
        new=AsyncMock(return_value=pipeline_ok),
    ):
        with patch("tools.llm_gateway.LLMGateway.ainvoke", new=mock_ainvoke):
            content, result = await local_fix_with_retry("x", "foo.py", node)
    assert result.passed is True
    assert node.error_streak == 0
    mock_ainvoke.assert_not_called()


@pytest.mark.anyio
async def test_local_fix_with_retry_retries_then_passes_on_second_attempt() -> None:
    """First validate fails → 1 fixer LLM call → second validate passes."""
    tree = MCTSTree(root_state=_make_mission(), root_vfs_view={})
    node = tree.expand(tree.root_id, "a", {}, _make_mission())
    fail = PipelineResult(passed=False, failed_layer="AST", prune_reason="missing colon")
    ok = PipelineResult(passed=True)
    mock_validate = AsyncMock(side_effect=[fail, ok])
    mock_ainvoke = AsyncMock(return_value=_fake_llm_response("fixed code"))
    with patch("agents.mcts_coder.validate_delta", new=mock_validate):
        with patch("tools.llm_gateway.LLMGateway.ainvoke", new=mock_ainvoke):
            content, result = await local_fix_with_retry("x", "foo.py", node)
    assert result.passed is True
    assert node.error_streak == 0  # reset on success
    # Exactly one fixer call (Tier.LOCAL).
    assert mock_ainvoke.call_count == 1
    assert mock_ainvoke.call_args.kwargs["tier"] == Tier.LOCAL


@pytest.mark.anyio
async def test_circuit_breaker_fires_at_three_failures() -> None:
    """When validate_delta fails 4 times (initial + 3 attempts), error_streak hits 3."""
    tree = MCTSTree(root_state=_make_mission(), root_vfs_view={})
    node = tree.expand(tree.root_id, "a", {}, _make_mission())
    fail = PipelineResult(passed=False, failed_layer="AST", prune_reason="bad")
    mock_validate = AsyncMock(return_value=fail)
    mock_ainvoke = AsyncMock(return_value=_fake_llm_response("still bad"))
    with patch("agents.mcts_coder.validate_delta", new=mock_validate):
        with patch("tools.llm_gateway.LLMGateway.ainvoke", new=mock_ainvoke):
            content, result = await local_fix_with_retry("x", "foo.py", node)
    assert result.passed is False
    assert node.error_streak == MAX_LOCAL_ATTEMPTS
    # All fixer calls used Tier.LOCAL (no cloud during the local loop).
    for call in mock_ainvoke.call_args_list:
        assert call.kwargs["tier"] == Tier.LOCAL


# ---------- Surgeon escalation ----------

@pytest.mark.anyio
async def test_surgeon_uses_tier_cloud_and_resets_on_success() -> None:
    """Circuit Breaker escalates with tier=CLOUD; error_streak resets if surgeon succeeds."""
    tree = MCTSTree(root_state=_make_mission(), root_vfs_view={})
    node = tree.expand(tree.root_id, "a", {}, _make_mission())
    node.error_streak = MAX_LOCAL_ATTEMPTS

    mock_ainvoke = AsyncMock(return_value=_fake_llm_response("surgeon-fixed"))
    ok = PipelineResult(passed=True)
    with patch("tools.llm_gateway.LLMGateway.ainvoke", new=mock_ainvoke):
        with patch("agents.mcts_coder.validate_delta", new=AsyncMock(return_value=ok)):
            fixed = await surgeon_escalation("bad code", "foo.py", "stuck error", node)
    assert fixed == "surgeon-fixed"
    assert mock_ainvoke.call_args.kwargs["tier"] == Tier.CLOUD
    assert node.error_streak == 0  # reset on successful surgery


# ---------- generate_local_variant ----------

@pytest.mark.anyio
async def test_generate_local_variant_uses_tier_local() -> None:
    mock_ainvoke = AsyncMock(return_value=_fake_llm_response("def foo(): pass"))
    with patch("tools.llm_gateway.LLMGateway.ainvoke", new=mock_ainvoke):
        result = await generate_local_variant("orig", "foo.py")
    assert result == "def foo(): pass"
    assert mock_ainvoke.call_args.kwargs["tier"] == Tier.LOCAL


# ---------- evaluate_node_reward orchestration ----------

@pytest.mark.anyio
async def test_evaluate_node_reward_calls_supreme_judge_only_after_local_passes() -> None:
    """Happy path: local passes immediately, Supreme Judge (cloud) returns reward."""
    tree = MCTSTree(root_state=_make_mission(), root_vfs_view={})
    node = tree.expand(tree.root_id, "a", {}, _make_mission())
    ok = PipelineResult(passed=True)
    mock_supreme = AsyncMock(return_value=MagicMock(reward=0.85))
    with patch("agents.mcts_coder.validate_delta", new=AsyncMock(return_value=ok)):
        with patch("agents.mcts_coder.supreme_judge_evaluate", new=mock_supreme):
            reward = await evaluate_node_reward("x", "foo.py", "/ws", node)
    assert reward == 0.85
    mock_supreme.assert_called_once()


@pytest.mark.anyio
async def test_evaluate_node_reward_returns_minus_one_when_surgeon_also_fails() -> None:
    """Local fails 3x → surgeon fixes nothing → reward=-1.0 with NO Supreme Judge call."""
    tree = MCTSTree(root_state=_make_mission(), root_vfs_view={})
    node = tree.expand(tree.root_id, "a", {}, _make_mission())
    fail = PipelineResult(passed=False, failed_layer="AST", prune_reason="bad")
    mock_supreme = AsyncMock(return_value=MagicMock(reward=0.9))
    mock_ainvoke = AsyncMock(return_value=_fake_llm_response("still bad"))
    with patch("agents.mcts_coder.validate_delta", new=AsyncMock(return_value=fail)):
        with patch("agents.mcts_coder.supreme_judge_evaluate", new=mock_supreme):
            with patch("tools.llm_gateway.LLMGateway.ainvoke", new=mock_ainvoke):
                reward = await evaluate_node_reward("x", "foo.py", "/ws", node)
    assert reward == -1.0
    mock_supreme.assert_not_called()   # Cloud judge skipped when local broken


@pytest.mark.anyio
async def test_evaluate_node_reward_supreme_judge_succeeds_after_surgeon() -> None:
    """Local fails 3x → surgeon repairs → Supreme Judge runs and returns reward."""
    tree = MCTSTree(root_state=_make_mission(), root_vfs_view={})
    node = tree.expand(tree.root_id, "a", {}, _make_mission())
    fail = PipelineResult(passed=False, failed_layer="AST", prune_reason="bad")
    ok = PipelineResult(passed=True)
    # validate_delta calls during evaluate_node_reward:
    #   1 initial + 3 fixer-retries = 4 fails (local_fix_with_retry exhausts attempts)
    #   1 inside surgeon_escalation post-check = pass
    #   1 in evaluate_node_reward after surgeon returns = pass
    mock_validate = AsyncMock(side_effect=[fail, fail, fail, fail, ok, ok])
    mock_supreme = AsyncMock(return_value=MagicMock(reward=0.6))
    mock_ainvoke = AsyncMock(return_value=_fake_llm_response("better code"))
    with patch("agents.mcts_coder.validate_delta", new=mock_validate):
        with patch("agents.mcts_coder.supreme_judge_evaluate", new=mock_supreme):
            with patch("tools.llm_gateway.LLMGateway.ainvoke", new=mock_ainvoke):
                reward = await evaluate_node_reward("x", "foo.py", "/ws", node)
    assert reward == 0.6
    mock_supreme.assert_called_once()
    # Check: 3 local fixer calls + 1 cloud surgeon call.
    tiers_used = [c.kwargs["tier"] for c in mock_ainvoke.call_args_list]
    assert tiers_used.count(Tier.LOCAL) == MAX_LOCAL_ATTEMPTS
    assert tiers_used.count(Tier.CLOUD) == 1


# ---------- Supreme Judge tier verification ----------

@pytest.mark.anyio
async def test_supreme_judge_uses_cloud_tier(tmp_path) -> None:
    """supreme_judge_evaluate must call ainvoke with tier=Tier.CLOUD."""
    import json
    (tmp_path / ".ailienant.json").write_text(json.dumps({"rules": ["R"]}))
    from core.rules import RuleManager
    RuleManager().reset()

    from agents.analyst import supreme_judge_evaluate
    mock_ainvoke = AsyncMock(return_value=_fake_llm_response(
        json.dumps({"reward": 0.5, "violated_rules": []}),
    ))
    with patch("tools.llm_gateway.LLMGateway.ainvoke", new=mock_ainvoke):
        result = await supreme_judge_evaluate("code", str(tmp_path))
    assert result.reward == 0.5
    assert mock_ainvoke.call_args.kwargs["tier"] == Tier.CLOUD


# ---------- HTTP endpoint ----------

def test_http_telemetry_tokens_endpoint() -> None:
    from fastapi.testclient import TestClient
    from main import app

    token_ledger.record_local(200, 100)
    token_ledger.record_cloud(50, 25)
    with TestClient(app) as client:
        resp = client.get("/api/v1/telemetry/tokens")
    assert resp.status_code == 200
    body = resp.json()
    assert set(body.keys()) >= {
        "local_tokens", "cloud_tokens", "estimated_savings_usd", "estimated_invested_usd",
    }
    assert body["local_tokens"] == 300.0
    assert body["cloud_tokens"] == 75.0
