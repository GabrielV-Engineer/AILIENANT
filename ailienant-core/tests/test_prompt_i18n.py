"""Language-mirroring directive reaches BOTH prompt-assembly skeletons.

The model is told to answer in the language of the user's request. The defect
this guards was a missing mirror instruction: with none, an English prompt
produced Spanish-leaked identifiers (``def transcribir_audio``). Two distinct
skeletons feed the LLM — ``build_safe_prompt`` (planner/researcher, via
``BASE_SYSTEM_PROMPT``) and ``build_coder_system_prompt`` (the coder, via
``_BASE_CODER_PROMPT``) — so the directive must appear in both, the
XML-sandbox quarantine axiom must survive intact and stay subordinate to
nothing, and the old Spanish context header must not bias the model.
"""
from __future__ import annotations

from agents.prompts import build_safe_prompt
from agents.roles import LANGUAGE_MIRROR_DIRECTIVE, build_coder_system_prompt
from shared.rbac import PLANNER_IDENTITY

_MIRROR_PHRASE = "Mirror the language"
_QUARANTINE_SENTINEL = "COGNITIVE QUARANTINE"


def test_directive_in_planner_system_prompt() -> None:
    prompt = build_safe_prompt(
        PLANNER_IDENTITY, context_str="", boundary="test_boundary"
    )
    assert _MIRROR_PHRASE in prompt
    assert LANGUAGE_MIRROR_DIRECTIVE in prompt


def test_directive_in_coder_system_prompt() -> None:
    # The coder is the agent that actually emitted Spanish identifiers; its
    # skeleton is separate and must carry the directive on its own.
    prompt = build_coder_system_prompt("core_dev")
    assert _MIRROR_PHRASE in prompt
    assert LANGUAGE_MIRROR_DIRECTIVE in prompt


def test_sandbox_shield_intact_and_outranks_mirror() -> None:
    # The quarantine axiom must remain present AND appear after the mirror
    # directive, so the directive can never be read as overriding the shield.
    prompt = build_safe_prompt(
        PLANNER_IDENTITY, context_str="", boundary="test_boundary"
    )
    assert _QUARANTINE_SENTINEL in prompt
    assert "<test_boundary>" in prompt
    assert prompt.index(_MIRROR_PHRASE) < prompt.index(_QUARANTINE_SENTINEL)


def test_no_spanish_context_header_leak() -> None:
    # The old Spanish header biased the model toward Spanish output; it must be
    # gone (replaced by the English equivalent).
    prompt = build_safe_prompt(
        PLANNER_IDENTITY, context_str="", boundary="test_boundary"
    )
    assert "CONTEXTO ACTIVO" not in prompt
    assert "ACTIVE CONTEXT" in prompt
