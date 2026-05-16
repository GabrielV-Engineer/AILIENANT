# tests/test_analyst_distillation.py
"""Phase 3.4.7 DoD — Rule Distillation + atomic append to local .ailienant.json."""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from agents.analyst import distill_rejection_to_rule
from core.rules import rule_manager


def _fake_llm_response(content: str) -> SimpleNamespace:
    """Mimic litellm ModelResponse.choices[0].message.content shape."""
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=content))]
    )


@pytest.fixture(autouse=True)
def _isolate_home(tmp_path, monkeypatch):
    """Sandbox Path.home() + reset RuleManager singleton between tests."""
    fake_home = tmp_path / "_home_"
    fake_home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: fake_home)
    rule_manager.reset()
    yield fake_home
    rule_manager.reset()


# ---------- distill_rejection_to_rule (LLM path) ----------

@pytest.mark.anyio
async def test_distill_returns_rule_from_llm() -> None:
    fake = _fake_llm_response(json.dumps({"rule": "Use list comprehensions"}))
    with patch(
        "tools.llm_gateway.LLMGateway.ainvoke",
        new=AsyncMock(return_value=fake),
    ):
        result = await distill_rejection_to_rule(
            "for x in items: out.append(x*2)",
            "out = [x*2 for x in items]",
        )
    assert result == "Use list comprehensions"


@pytest.mark.anyio
async def test_distill_returns_none_when_llm_says_null() -> None:
    fake = _fake_llm_response(json.dumps({"rule": None}))
    with patch(
        "tools.llm_gateway.LLMGateway.ainvoke",
        new=AsyncMock(return_value=fake),
    ):
        result = await distill_rejection_to_rule("foo", "bar")
    assert result is None


@pytest.mark.anyio
async def test_distill_returns_none_when_codes_identical() -> None:
    mock_ainvoke = AsyncMock(return_value=_fake_llm_response('{"rule": "X"}'))
    with patch("tools.llm_gateway.LLMGateway.ainvoke", new=mock_ainvoke):
        result = await distill_rejection_to_rule("same code", "same code")
    assert result is None
    mock_ainvoke.assert_not_called()


@pytest.mark.anyio
async def test_distill_returns_none_on_llm_failure() -> None:
    with patch(
        "tools.llm_gateway.LLMGateway.ainvoke",
        new=AsyncMock(side_effect=RuntimeError("network down")),
    ):
        result = await distill_rejection_to_rule("a", "b")
    assert result is None  # swallows the exception


@pytest.mark.anyio
async def test_distill_returns_none_on_invalid_json() -> None:
    fake = _fake_llm_response("not json at all")
    with patch(
        "tools.llm_gateway.LLMGateway.ainvoke",
        new=AsyncMock(return_value=fake),
    ):
        result = await distill_rejection_to_rule("a", "b")
    assert result is None


@pytest.mark.anyio
async def test_distill_strips_whitespace_and_drops_empty_rule() -> None:
    fake = _fake_llm_response(json.dumps({"rule": "   "}))
    with patch(
        "tools.llm_gateway.LLMGateway.ainvoke",
        new=AsyncMock(return_value=fake),
    ):
        result = await distill_rejection_to_rule("a", "b")
    assert result is None


# ---------- RuleManager.append_local_rule (file write) ----------

def test_append_local_rule_creates_file(tmp_path: Path) -> None:
    """When no .ailienant.json exists, the writer creates the dir+file."""
    target = tmp_path / ".ailienant" / ".ailienant.json"
    assert not target.exists()
    changed = rule_manager.append_local_rule(str(tmp_path), "RuleX")
    assert changed is True
    assert target.is_file()
    data = json.loads(target.read_text(encoding="utf-8"))
    assert data == {"rules": ["RuleX"]}


def test_append_local_rule_preserves_profile_fields(tmp_path: Path) -> None:
    """Existing IntelligenceProfileConfig keys must survive a rule write."""
    target = tmp_path / ".ailienant" / ".ailienant.json"
    target.parent.mkdir()
    target.write_text(
        json.dumps({"master_enabled": True, "profile": "Hybrid"}),
        encoding="utf-8",
    )
    changed = rule_manager.append_local_rule(str(tmp_path), "NewRule")
    assert changed is True
    data: dict[str, Any] = json.loads(target.read_text(encoding="utf-8"))
    assert data["master_enabled"] is True
    assert data["profile"] == "Hybrid"
    assert data["rules"] == ["NewRule"]


def test_append_local_rule_dedupes_existing(tmp_path: Path) -> None:
    target = tmp_path / ".ailienant" / ".ailienant.json"
    target.parent.mkdir()
    target.write_text(json.dumps({"rules": ["A"]}), encoding="utf-8")
    before_mtime = target.stat().st_mtime
    changed = rule_manager.append_local_rule(str(tmp_path), "A")
    assert changed is False
    # File untouched.
    after_mtime = target.stat().st_mtime
    assert before_mtime == after_mtime
    data = json.loads(target.read_text(encoding="utf-8"))
    assert data == {"rules": ["A"]}


def test_append_local_rule_appends_to_existing_list(tmp_path: Path) -> None:
    target = tmp_path / ".ailienant" / ".ailienant.json"
    target.parent.mkdir()
    target.write_text(json.dumps({"rules": ["A"]}), encoding="utf-8")
    changed = rule_manager.append_local_rule(str(tmp_path), "B")
    assert changed is True
    data = json.loads(target.read_text(encoding="utf-8"))
    assert data["rules"] == ["A", "B"]


def test_append_local_rule_handles_dict_rules_shape(tmp_path: Path) -> None:
    """If existing rules are a dict, the new rule lands in the 'distilled' list."""
    target = tmp_path / ".ailienant" / ".ailienant.json"
    target.parent.mkdir()
    target.write_text(
        json.dumps({"rules": {"deps": ["x"]}}),
        encoding="utf-8",
    )
    changed = rule_manager.append_local_rule(str(tmp_path), "DistilledRule")
    assert changed is True
    data = json.loads(target.read_text(encoding="utf-8"))
    assert data["rules"]["deps"] == ["x"]
    assert data["rules"]["distilled"] == ["DistilledRule"]


def test_append_local_rule_invalidates_cache(tmp_path: Path) -> None:
    """After append, get_combined_rules must surface the new rule on next call."""
    rule_manager.append_local_rule(str(tmp_path), "FreshRule")
    out = rule_manager.get_combined_rules(str(tmp_path))
    assert "- FreshRule" in out


def test_append_local_rule_rejects_empty_inputs(tmp_path: Path) -> None:
    assert rule_manager.append_local_rule("", "rule") is False
    assert rule_manager.append_local_rule(str(tmp_path), "") is False
    assert rule_manager.append_local_rule(str(tmp_path), "   ") is False


# ---------- End-to-end (DoD anchor) ----------

@pytest.mark.anyio
async def test_e2e_distill_then_persist_rule(tmp_path: Path) -> None:
    """DoD anchor: LLM distills a rule that's then written to local .ailienant.json."""
    fake = _fake_llm_response(json.dumps({"rule": "Type-annotate all public functions"}))
    with patch(
        "tools.llm_gateway.LLMGateway.ainvoke",
        new=AsyncMock(return_value=fake),
    ):
        rule = await distill_rejection_to_rule(
            "def add(a, b): return a + b",
            "def add(a: int, b: int) -> int: return a + b",
        )
    assert rule == "Type-annotate all public functions"
    appended = rule_manager.append_local_rule(str(tmp_path), rule)
    assert appended is True
    target = tmp_path / ".ailienant" / ".ailienant.json"
    data = json.loads(target.read_text(encoding="utf-8"))
    assert data["rules"] == ["Type-annotate all public functions"]
