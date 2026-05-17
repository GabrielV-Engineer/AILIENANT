# tests/test_analyst_agent.py
"""Phase 4.1.5 DoD — AnalystAgent SOUL.md persona integration.

Five tests:
  A.  Hot-reload — SoulManager invalidates its cache when SOUL.md's mtime advances.
  B.  Fallback — missing file → built-in 🐜 + Socratic default prompt.
  B2. R6 — directory misconfiguration falls back gracefully (no IsADirectoryError).
  C.  R1 — Analyst node returns no phantom state keys and is strictly ReadOnly.
  D.  Foreign-import fence — planner/coder/orchestrator/researcher MUST NEVER
      import brain.personality (cognitive isolation per blueprint §3.4).
"""
from __future__ import annotations

import logging
import os
import time
from pathlib import Path
from typing import Any, Dict, List
from unittest.mock import AsyncMock, patch

import pytest


# ── Test A — hot-reload ──────────────────────────────────────────────────────


def test_soul_manager_hot_reloads_on_mtime_change(tmp_path: Path) -> None:
    soul_file = tmp_path / "SOUL.md"
    soul_file.write_text("version one 🐜", encoding="utf-8")

    from brain.personality import SoulManager

    mgr = SoulManager(path=soul_file)

    first = mgr.get_prompt()
    assert "version one" in first

    # Force mtime advance — Windows FAT-like filesystems have 2-second mtime
    # resolution, so we sleep a touch + explicitly bump utime as belt-and-braces.
    time.sleep(0.05)
    soul_file.write_text("version two 🐜🐜", encoding="utf-8")
    new_mtime = time.time() + 1.0
    os.utime(soul_file, (new_mtime, new_mtime))

    second = mgr.get_prompt()
    assert "version two" in second

    # Third call without changes — must serve from cache (same content).
    third = mgr.get_prompt()
    assert third == second


# ── Test B — missing-file fallback ───────────────────────────────────────────


def test_soul_manager_returns_default_fallback_when_missing(tmp_path: Path) -> None:
    from brain.personality import SoulManager

    mgr = SoulManager(path=tmp_path / "does_not_exist.md")
    prompt = mgr.get_prompt()

    assert "🐜" in prompt, "fallback must contain the canonical ant emoji"
    assert "Socratic" in prompt, "fallback must identify the persona as Socratic"

    # No spurious caching of empty content — two calls return the same fallback.
    assert mgr.get_prompt() == prompt


# ── Test B2 — R6 directory-misconfiguration guard ────────────────────────────


def test_soul_manager_falls_back_when_path_is_a_directory(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    from brain.personality import SoulManager

    # tmp_path is itself a directory — simulates AILIENANT_SOUL_PATH with
    # a trailing slash, which would otherwise raise IsADirectoryError on read.
    mgr = SoulManager(path=tmp_path)

    with caplog.at_level(logging.WARNING, logger="PERSONALITY_MANAGER"):
        prompt = mgr.get_prompt()

    assert "🐜" in prompt
    # Operator-friendly diagnostic must fire and mention "DIRECTORY".
    assert any(
        "DIRECTORY" in record.getMessage() for record in caplog.records
    ), f"Expected DIRECTORY warning, got: {[r.getMessage() for r in caplog.records]}"


# ── Test C — R1 state-key contract + ReadOnly policy ─────────────────────────


@pytest.mark.anyio
async def test_analyst_agent_read_only_policy_and_no_phantom_state_keys() -> None:
    """run_analyst_node must not mutate vfs_buffer / pending_patches / generated_code,
    must not leak the soul_prompt sentinel into messages, and every returned key
    must be a declared AIlienantGraphState field."""
    sentinel = "SOUL_SENTINEL_NEVER_LEAK_THIS_STRING"

    state: Dict[str, Any] = {
        "task_id": "analyst-test",
        "user_input": "I want to add a new endpoint.",
        "messages": [],
        "errors": [],
        "security_flags": [],
        "hitl_pending": False,
        "shared_understanding_reached": False,
    }

    with patch(
        "agents.analyst.soul_manager.get_prompt", return_value=sentinel
    ), patch(
        "api.websocket_manager.vfs_manager.broadcast_token",
        new=AsyncMock(return_value=None),
    ):
        from agents.analyst import run_analyst_node

        result = await run_analyst_node(state)

    # ReadOnly policy: writer-channel keys must NOT appear in the result dict.
    assert "vfs_buffer" not in result
    assert "pending_patches" not in result
    assert "generated_code" not in result

    # R1 — every returned key must be a declared AIlienantGraphState field.
    allowed_state_keys = {
        "messages",
        "hitl_pending",
        "shared_understanding_reached",
        "errors",
        "security_flags",
    }
    extras = set(result.keys()) - allowed_state_keys
    assert not extras, f"Analyst leaked non-state keys: {extras}"

    # Soul prompt sentinel must NOT appear in any returned message content.
    surfaced = "\n".join(m.get("content", "") for m in result.get("messages", []))
    assert sentinel not in surfaced, (
        "soul_prompt is ephemeral — it must NEVER reach state.messages."
    )


# ── Test D — foreign-import fence (cognitive isolation) ──────────────────────


def test_soul_manager_not_imported_by_logic_agents() -> None:
    """Planner, Coder, Orchestrator, Researcher MUST NEVER import brain.personality.

    Static source audit — catches an accidental future import that would breach
    the cognitive-isolation fence (blueprint §3.4).
    """
    project_root = Path(__file__).resolve().parent.parent  # ailienant-core/
    logic_agents: List[Path] = [
        project_root / "agents" / "planner.py",
        project_root / "agents" / "coder.py",
        project_root / "agents" / "orchestrator.py",
        project_root / "agents" / "researcher.py",
    ]
    forbidden_patterns = ("from brain.personality", "import brain.personality")

    breaches: List[str] = []
    for agent_file in logic_agents:
        assert agent_file.is_file(), f"expected logic agent missing: {agent_file}"
        source = agent_file.read_text(encoding="utf-8")
        for pattern in forbidden_patterns:
            if pattern in source:
                breaches.append(f"{agent_file.name}: contains '{pattern}'")

    assert not breaches, (
        "Cognitive-isolation fence breached — logic agents must not import "
        f"brain.personality. Breaches: {breaches}"
    )
