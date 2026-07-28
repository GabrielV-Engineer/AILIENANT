# ailienant-core/tests/test_heuristics.py
#
# Coverage:
#   is_polyglot_file:
#     1-12. Known polyglot extensions return True
#     13-19. Pure-syntax extensions return False
#     20. Case-insensitive extension matching
#   filter_relevant_snippets: cross-project RAG bleed guard (Item B)

import pytest

from core.utils import is_polyglot_file, filter_relevant_snippets


# ---------------------------------------------------------------------------
# Polyglot extensions — must return True
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("path", [
    "templates/index.html",
    "App.vue",
    "Component.svelte",
    "Button.jsx",
    "Form.tsx",
    "base.jinja2",
    "email.jinja",
    "partials/header.j2",
    "README.md",
    "content.mdx",
    "layouts/app.erb",
    "page.ejs",
    "views/user.blade.php",
])
def test_is_polyglot_true(path: str) -> None:
    assert is_polyglot_file(path) is True


# ---------------------------------------------------------------------------
# Pure-syntax extensions — must return False
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("path", [
    "main.py",
    "utils.ts",
    "server.go",
    "App.java",
    "config.json",
    "styles.css",
    "script.js",
])
def test_is_polyglot_false(path: str) -> None:
    assert is_polyglot_file(path) is False


# ---------------------------------------------------------------------------
# Case-insensitivity
# ---------------------------------------------------------------------------


def test_is_polyglot_case_insensitive() -> None:
    assert is_polyglot_file("Page.HTML") is True
    assert is_polyglot_file("App.VUE") is True
    assert is_polyglot_file("Component.SVELTE") is True


# ---------------------------------------------------------------------------
# filter_relevant_snippets — cross-project RAG bleed guard (Item B)
#
# A workspace root spanning two unrelated projects (e.g. one folder containing
# both an existing app and a brand-new one) shares one project_id / vector
# index, so a semantic search scoped to the new project can legitimately
# surface top-k matches from the other. The filter approximates the missing
# per-project boundary with the anchor file's top-level path segment.
# ---------------------------------------------------------------------------


def test_filter_drops_unrelated_top_level_project() -> None:
    snippets = [
        ("GameData/player.py", "class Player: ..."),
        ("App_transcription/audio.py", "def transcribe(): ..."),
    ]
    result = filter_relevant_snippets(snippets, "GameData/score.py")
    assert result == [("GameData/player.py", "class Player: ...")]


def test_filter_keeps_same_top_level_snippets() -> None:
    snippets = [
        ("GameData/player.py", "class Player: ..."),
        ("GameData/utils/vectors.py", "def dot(a, b): ..."),
    ]
    result = filter_relevant_snippets(snippets, "GameData/score.py")
    assert result == snippets


def test_filter_no_directory_component_is_a_noop() -> None:
    """A flat single-file workspace has nothing to compare against — never filter."""
    snippets = [("other_project/thing.py", "...")]
    result = filter_relevant_snippets(snippets, "main.py")
    assert result == snippets


def test_filter_never_drops_an_explicit_mention() -> None:
    """An explicit user reference always wins over the relevance heuristic —
    never hide a file the task explicitly names."""
    snippets = [("App_transcription/audio.py", "def transcribe(): ...")]
    result = filter_relevant_snippets(
        snippets, "GameData/score.py", explicit_mentions=["App_transcription/audio.py"],
    )
    assert result == snippets


def test_filter_empty_snippets_returns_empty() -> None:
    assert filter_relevant_snippets([], "GameData/score.py") == []


def test_filter_handles_windows_backslash_paths() -> None:
    snippets = [
        ("GameData\\player.py", "class Player: ..."),
        ("App_transcription\\audio.py", "def transcribe(): ..."),
    ]
    result = filter_relevant_snippets(snippets, "GameData\\score.py")
    assert result == [("GameData\\player.py", "class Player: ...")]
