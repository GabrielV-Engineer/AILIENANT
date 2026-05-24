# ailienant-core/tests/test_model_resolver.py
"""Phase 7.9.B.17 — chat-target resolution + Ollama route normalization.

get_chat_target must rewrite `ollama/<m>` to `ollama_chat/<m>` (litellm chat
endpoint, applies the model template) at read time, so already-persisted presets
are fixed without a re-apply. Non-ollama targets are untouched. Tier fallback
still works and is normalized too.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

from core.config import model_resolver
from core.config.byom_config import ModelTarget


def _cfg(chat_models: dict) -> SimpleNamespace:
    return SimpleNamespace(chat_models=chat_models)


def _ollama(model: str) -> ModelTarget:
    return ModelTarget(
        model=model, provider="ollama",
        api_base="http://localhost:11434", api_key="", is_local=True,
    )


def test_get_chat_target_normalizes_ollama_to_ollama_chat() -> None:
    targets = {"medium": _ollama("ollama/llama3.1")}
    with patch("core.config.model_resolver.load_byom_config", return_value=_cfg(targets)):
        model_resolver.refresh()
        t = model_resolver.get_chat_target("medium")
    model_resolver.refresh()
    assert t is not None
    assert t.model == "ollama_chat/llama3.1"
    assert t.provider == "ollama"


def test_get_chat_target_leaves_non_ollama_untouched() -> None:
    targets = {
        "medium": ModelTarget(
            model="gpt-4o", provider="openai", api_base=None, api_key="sk-x", is_local=False
        )
    }
    with patch("core.config.model_resolver.load_byom_config", return_value=_cfg(targets)):
        model_resolver.refresh()
        t = model_resolver.get_chat_target("medium")
    model_resolver.refresh()
    assert t is not None
    assert t.model == "gpt-4o"


def test_get_chat_target_falls_back_across_tiers_and_normalizes() -> None:
    # Request "big" but only "small" exists → fallback + normalize.
    targets = {"small": _ollama("ollama/phi3")}
    with patch("core.config.model_resolver.load_byom_config", return_value=_cfg(targets)):
        model_resolver.refresh()
        t = model_resolver.get_chat_target("big")
    model_resolver.refresh()
    assert t is not None
    assert t.model == "ollama_chat/phi3"


def test_get_chat_target_none_when_no_preset() -> None:
    with patch("core.config.model_resolver.load_byom_config", return_value=_cfg({})):
        model_resolver.refresh()
        t = model_resolver.get_chat_target("medium")
    model_resolver.refresh()
    assert t is None
