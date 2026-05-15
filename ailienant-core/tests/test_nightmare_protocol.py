# tests/test_nightmare_protocol.py
"""Phase 3.4.3a DoD — Nightmare Protocol evaluator."""
from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from agents.analyst import NightmareEvaluation, evaluate_nightmare


def _fake_llm_response(content: str) -> SimpleNamespace:
    """Mimic litellm ModelResponse.choices[0].message.content structure."""
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=content))]
    )


@pytest.fixture
def workspace(tmp_path):
    """tmp_path workspace with a .ailienant.json rules file."""
    (tmp_path / ".ailienant.json").write_text(
        json.dumps({"rules": ["No global installs"]}),
        encoding="utf-8",
    )
    from core.rules import RuleManager
    RuleManager().reset()
    return str(tmp_path)


@pytest.mark.anyio
async def test_nightmare_clean_delta_returns_high_reward(workspace) -> None:
    fake = _fake_llm_response(json.dumps({"reward": 0.9, "violated_rules": []}))
    with patch(
        "tools.llm_gateway.LLMGateway.ainvoke",
        new=AsyncMock(return_value=fake),
    ):
        result = await evaluate_nightmare("def foo(): pass", workspace)
    assert isinstance(result, NightmareEvaluation)
    assert result.reward == pytest.approx(0.9)
    assert result.violated_rules == []


@pytest.mark.anyio
async def test_nightmare_violation_returns_zero(workspace) -> None:
    fake = _fake_llm_response(
        json.dumps({"reward": 0.0, "violated_rules": ["No global installs"]})
    )
    with patch(
        "tools.llm_gateway.LLMGateway.ainvoke",
        new=AsyncMock(return_value=fake),
    ):
        result = await evaluate_nightmare("pip install -g something", workspace)
    assert result.reward == 0.0
    assert result.violated_rules == ["No global installs"]


@pytest.mark.anyio
async def test_nightmare_llm_failure_returns_failsafe_zero(workspace) -> None:
    with patch(
        "tools.llm_gateway.LLMGateway.ainvoke",
        new=AsyncMock(side_effect=RuntimeError("boom")),
    ):
        result = await evaluate_nightmare("x = 1", workspace)
    assert result.reward == 0.0
    assert result.violated_rules == ["LLM_EVAL_FAILED"]


@pytest.mark.anyio
async def test_nightmare_reward_clamped_to_unit_interval(workspace) -> None:
    fake_high = _fake_llm_response(json.dumps({"reward": 1.5, "violated_rules": []}))
    with patch(
        "tools.llm_gateway.LLMGateway.ainvoke",
        new=AsyncMock(return_value=fake_high),
    ):
        r1 = await evaluate_nightmare("x", workspace)
    assert r1.reward == 1.0

    fake_low = _fake_llm_response(json.dumps({"reward": -0.3, "violated_rules": []}))
    with patch(
        "tools.llm_gateway.LLMGateway.ainvoke",
        new=AsyncMock(return_value=fake_low),
    ):
        r2 = await evaluate_nightmare("y", workspace)
    assert r2.reward == 0.0


@pytest.mark.anyio
async def test_nightmare_invalid_json_returns_failsafe(workspace) -> None:
    fake = _fake_llm_response("not json at all")
    with patch(
        "tools.llm_gateway.LLMGateway.ainvoke",
        new=AsyncMock(return_value=fake),
    ):
        result = await evaluate_nightmare("z", workspace)
    assert result.reward == 0.0
    assert result.violated_rules == ["LLM_EVAL_FAILED"]
