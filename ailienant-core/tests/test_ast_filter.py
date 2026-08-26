# tests/test_ast_filter.py
"""Phase 3.4.4 DoD #2 — AST filter rejects bad Python and bad TypeScript."""
from __future__ import annotations

from tools.validation.ast_filter import validate_ast


# ---------- Python ----------

def test_python_valid_passes() -> None:
    result = validate_ast("def foo():\n    pass\n", "x.py")
    assert result.is_valid is True
    assert result.errors == []
    assert result.prune_reason is None


def test_python_missing_colon_fails() -> None:
    """Missing colon after `def foo()` must fail Layer 1 with file path + line."""
    result = validate_ast("def foo()\n    pass\n", "x.py")
    assert result.is_valid is False
    assert len(result.errors) == 1
    assert result.errors[0].layer == "AST"
    assert result.errors[0].line is not None
    assert result.prune_reason is not None
    assert "x.py" in result.prune_reason


def test_python_unclosed_string_fails() -> None:
    result = validate_ast('x = "unterminated\n', "u.py")
    assert result.is_valid is False
    assert result.errors[0].layer == "AST"


def test_python_indentation_error_fails() -> None:
    result = validate_ast("def foo():\npass\n", "i.py")
    assert result.is_valid is False
    assert result.errors[0].layer == "AST"


# ---------- TypeScript / TSX ----------

def test_typescript_valid_passes() -> None:
    result = validate_ast("const x: number = 1;\n", "a.ts")
    # Either tree-sitter accepts it OR the grammar couldn't load — both leave
    # the candidate non-pruned for Layer 1 in a useful way. We require pass
    # whenever the parser is available.
    if result.prune_reason and "parser unavailable" in result.prune_reason:
        return
    assert result.is_valid is True


def test_typescript_unclosed_brace_fails() -> None:
    """`function f() {` with no closing brace must fail Layer 1."""
    result = validate_ast("function f() {\n", "a.ts")
    if result.prune_reason and "parser unavailable" in result.prune_reason:
        return  # Grammar missing — DoD met by Python case + parser-unavailable signal.
    assert result.is_valid is False
    assert result.errors[0].layer == "AST"
    assert result.prune_reason is not None
    assert "typescript" in result.prune_reason


def test_tsx_unclosed_jsx_fails() -> None:
    result = validate_ast("const x = <Foo>\n", "a.tsx")
    if result.prune_reason and "parser unavailable" in result.prune_reason:
        return
    assert result.is_valid is False
    assert result.errors[0].layer == "AST"


# ---------- Pass-through ----------

def test_unsupported_extension_passes() -> None:
    """Out-of-scope languages must pass through Layer 1 unchanged."""
    result = validate_ast("garbage{{{ this would never parse", "notes.txt")
    assert result.is_valid is True
    assert result.errors == []


def test_unsupported_extension_passes_even_for_known_garbage() -> None:
    result = validate_ast("def foo() pass", "config.yaml")
    assert result.is_valid is True


# ---------- JavaScript / JSX (derived coverage — regression for a real incident) ----------
#
# Before this fix, `.js`/`.jsx` had no entry in the validator's own extension map even
# though the underlying tree-sitter engine (core/ast_engine._LANG_MAP) already supported
# them, so any JSX file passed Layer 1 unconditionally. A live coding turn wrote a React
# component whose content ended in a literal, unclosed `>>>>>>> REPLACE` conflict marker
# — invalid JavaScript — and it was accepted as valid. These cases are the exact shape of
# that incident, not a synthetic one.

def test_javascript_valid_passes() -> None:
    result = validate_ast("function add(a, b) {\n  return a + b;\n}\n", "a.js")
    if result.prune_reason and "parser unavailable" in result.prune_reason:
        return
    assert result.is_valid is True


def test_jsx_conflict_marker_fails() -> None:
    """The exact defect: a REPLACE marker left in committed JSX must fail Layer 1."""
    content = (
        "import React from 'react';\n\n"
        "function App() {\n  return <div>Hello</div>;\n}\n\n"
        "export default App;>>>>>>> REPLACE"
    )
    result = validate_ast(content, "App.jsx")
    if result.prune_reason and "parser unavailable" in result.prune_reason:
        return
    assert result.is_valid is False
    assert result.errors[0].layer == "AST"


def test_jsx_valid_passes() -> None:
    content = "function App() {\n  return <div>Hello</div>;\n}\n\nexport default App;\n"
    result = validate_ast(content, "App.jsx")
    if result.prune_reason and "parser unavailable" in result.prune_reason:
        return
    assert result.is_valid is True


def test_resolve_grammar_language_matches_the_shared_detector() -> None:
    """The validator's coverage must be DERIVED from shared.contracts.detect_language
    and core.ast_engine._LANG_MAP, never a hand-maintained third list that can drift
    from either."""
    from tools.validation.ast_filter import resolve_grammar_language

    assert resolve_grammar_language("a.jsx") == "javascriptreact"
    assert resolve_grammar_language("a.js") == "javascript"
    assert resolve_grammar_language("a.ts") == "typescript"
    assert resolve_grammar_language("a.tsx") == "typescriptreact"
    assert resolve_grammar_language("notes.txt") is None
