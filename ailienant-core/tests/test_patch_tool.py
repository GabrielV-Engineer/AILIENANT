# ailienant-core/tests/test_patch_tool.py
#
# Phase 2.22.1 & 2.22.2 DoD: pytest tests/test_patch_tool.py -v -> 0 failures.
#
# Coverage:
#   AtomicPatchInput (Pydantic schema):
#     1. Reject empty search_block
#     2. Reject short search_block (< 10 chars)
#     3. Reject whitespace-only search_block
#     4. Accept valid inputs
#   _fuzzy_find_and_replace (Context Anchoring Engine):
#     5. Succeeds on single-character LLM typo (ratio > 0.90)
#     6. Raises PatchError when ratio below threshold
#   _validate_python_syntax (AST Boundary Check):
#     7. Passes for valid Python
#     8. Raises PatchError on broken Python (unclosed paren)
#     9. Skipped for non-.py files

import pytest
from pydantic import ValidationError

from core.exceptions import PatchError
from tools.patch_tool import (
    AtomicPatchInput,
    _fuzzy_find_and_replace,
    _validate_python_syntax,
)


# ---------------------------------------------------------------------------
# AtomicPatchInput — Pydantic schema validation
# ---------------------------------------------------------------------------


def test_atomic_patch_input_rejects_empty_search_block() -> None:
    with pytest.raises(ValidationError):
        AtomicPatchInput(file_path="foo.py", search_block="")


def test_atomic_patch_input_rejects_short_search_block() -> None:
    with pytest.raises(ValidationError):
        AtomicPatchInput(file_path="foo.py", search_block="short")  # 5 chars


def test_atomic_patch_input_rejects_whitespace_only_search_block() -> None:
    with pytest.raises(ValidationError):
        AtomicPatchInput(file_path="foo.py", search_block="   \n  ")


def test_atomic_patch_input_accepts_valid_inputs() -> None:
    obj = AtomicPatchInput(
        file_path="foo.py",
        search_block="def foo():\n    pass\n",
        replace_block="def foo():\n    return 42\n",
    )
    assert obj.replace_block == "def foo():\n    return 42\n"
    assert obj.ast_context_node is None


# ---------------------------------------------------------------------------
# _fuzzy_find_and_replace — Context Anchoring Engine
# ---------------------------------------------------------------------------


def test_fuzzy_match_succeeds_on_single_character_typo() -> None:
    """LLM typo: 'calcualte' instead of 'calculate' — ratio exceeds 0.90."""
    content = "def calculate_total(items):\n    return sum(items)\n"
    search = "def calcualte_total(items):\n    return sum(items)\n"  # transposed letters
    replace = "def calculate_total(items):\n    return sum(i.price for i in items)\n"
    result = _fuzzy_find_and_replace(content, search, replace)
    assert "i.price" in result
    assert "calcualte" not in result


def test_fuzzy_match_raises_patch_error_when_ratio_below_threshold() -> None:
    content = "def foo():\n    return 1\n"
    search = "class CompletelyDifferentThing:\n    def unrelated_method(self):\n        pass\n"
    with pytest.raises(PatchError, match="ratio"):
        _fuzzy_find_and_replace(content, search, "replacement")


# ---------------------------------------------------------------------------
# _validate_python_syntax — AST Boundary Check
# ---------------------------------------------------------------------------


def test_ast_validation_passes_for_valid_python() -> None:
    _validate_python_syntax("def foo():\n    return 42\n", "module.py")


def test_ast_validation_raises_patch_error_on_broken_python() -> None:
    broken = "def foo():\n    return (\n"  # unclosed parenthesis
    with pytest.raises(PatchError, match="AST Validation Failed"):
        _validate_python_syntax(broken, "module.py")


def test_ast_validation_skipped_for_non_python_files() -> None:
    _validate_python_syntax("{{{{{{{{ not python }}}}}}}}", "template.html")
