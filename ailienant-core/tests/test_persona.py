# tests/test_persona.py
"""Phase 7.10.1 DoD — Identity Sovereignty regression guard (ADR-701).

Seven tests:
  P1. identity_clause_contains_required_hardenings — clause forbids all known model names.
  P2. compose_prepends_identity_first — body always follows the clause.
  P3. compose_with_empty_body — no crash on empty body.
  P4. compose_is_idempotent — LangGraph cycle cannot double-inject the clause.
  P5. main_chat_prompt_contains_identity_clause — _CHAT_SYSTEM_PROMPT uses the clause.
  P6. soul_default_contains_identity_clause — SoulManager fallback uses the clause.
  P7. custom_soul_md_also_receives_identity_clause — custom SOUL.md gets clause prepended.
"""
from __future__ import annotations

from pathlib import Path

from shared.persona import AILIENANT_IDENTITY, compose


# ── P1 — identity clause contains required hardenings ────────────────────────


def test_identity_clause_contains_required_hardenings() -> None:
    assert "AILIENANT" in AILIENANT_IDENTITY
    assert "NEVER" in AILIENANT_IDENTITY
    assert "Qwen" in AILIENANT_IDENTITY
    assert "Llama" in AILIENANT_IDENTITY
    assert "GPT" in AILIENANT_IDENTITY
    assert "Claude" in AILIENANT_IDENTITY


# ── P2 — compose() prepends identity first ───────────────────────────────────


def test_compose_prepends_identity_first() -> None:
    result = compose("Socratic copilot body")
    assert result.startswith(AILIENANT_IDENTITY)
    assert "Socratic copilot body" in result
    # Identity clause appears before the body.
    assert result.index(AILIENANT_IDENTITY) < result.index("Socratic copilot body")


# ── P3 — compose() handles empty body without crashing ───────────────────────


def test_compose_with_empty_body() -> None:
    result = compose("")
    assert result.startswith(AILIENANT_IDENTITY)
    assert "\n\n" in result


# ── P4 — compose() is idempotent (LangGraph cycle safety) ────────────────────


def test_compose_is_idempotent() -> None:
    first_pass = compose("Socratic copilot")
    second_pass = compose(first_pass)
    assert second_pass == first_pass, (
        "compose() must not double-inject the identity clause in state cycles"
    )
    assert second_pass.count("You are AILIENANT") == 1


# ── P5 — main-chat system prompt contains the identity clause ────────────────


def test_main_chat_prompt_contains_identity_clause() -> None:
    from core.task_service import _CHAT_SYSTEM_PROMPT  # type: ignore[attr-defined]

    assert _CHAT_SYSTEM_PROMPT.startswith(AILIENANT_IDENTITY), (
        "_CHAT_SYSTEM_PROMPT must begin with the ADR-701 identity clause"
    )


# ── P6 — analyst soul default contains the identity clause ───────────────────


def test_soul_default_contains_identity_clause(tmp_path: Path) -> None:
    from brain.personality import SoulManager

    mgr = SoulManager(path=tmp_path / "does_not_exist.md")
    prompt = mgr.get_prompt()
    assert prompt.startswith(AILIENANT_IDENTITY), (
        "SoulManager fallback must prepend the ADR-701 identity clause"
    )


# ── P7 — custom SOUL.md also receives the identity clause ────────────────────


def test_custom_soul_md_also_receives_identity_clause(tmp_path: Path) -> None:
    from brain.personality import SoulManager

    custom_body = "A custom operator persona that says nothing about identity."
    soul_file = tmp_path / "SOUL.md"
    soul_file.write_text(custom_body, encoding="utf-8")

    mgr = SoulManager(path=soul_file)
    prompt = mgr.get_prompt()

    assert prompt.startswith(AILIENANT_IDENTITY), (
        "Custom SOUL.md prompts must also start with the ADR-701 identity clause"
    )
    assert custom_body in prompt, (
        "Custom SOUL.md body must still appear in the composed prompt"
    )
    assert prompt.count("You are AILIENANT") == 1, (
        "Identity clause must appear exactly once — no double-injection"
    )
