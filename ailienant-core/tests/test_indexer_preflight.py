# ailienant-core/tests/test_indexer_preflight.py
"""Phase 7.9.B.17 — robust Ollama embed-model presence match.

Ollama's /api/tags reports tagged names (e.g. "nomic-embed-text:latest"), so a
naive equality check falsely reported a freshly-pulled model as missing. The
match must be tag- and case-insensitive with either-direction prefix.
"""
from __future__ import annotations

from core.indexer import _ollama_model_present


def test_pulled_model_matches_tagged_name() -> None:
    assert _ollama_model_present({"nomic-embed-text:latest"}, "nomic-embed-text")


def test_match_is_case_insensitive() -> None:
    assert _ollama_model_present({"Nomic-Embed-Text:latest"}, "nomic-embed-text")


def test_exact_untagged_match() -> None:
    assert _ollama_model_present({"mxbai-embed-large"}, "mxbai-embed-large")


def test_absent_model_returns_false() -> None:
    assert not _ollama_model_present({"llama3:latest", "phi3:mini"}, "nomic-embed-text")


def test_empty_want_returns_false() -> None:
    assert not _ollama_model_present({"nomic-embed-text:latest"}, "")
