# ailienant-core/tests/test_heuristics.py
#
# Phase 2.22.6 DoD: pytest tests/test_heuristics.py -v -> 0 failures.
#
# Coverage:
#   is_polyglot_file:
#     1-12. Known polyglot extensions return True
#     13-19. Pure-syntax extensions return False
#     20. Case-insensitive extension matching

import pytest

from core.utils import is_polyglot_file


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
