"""Structured-diagnostics parser — total, bounded, reusable.

The closed-loop executor feeds the model distilled ``[file, line, code, msg]``
diagnostics instead of raw stdout. These tests pin two guarantees that matter on
the live event loop: the parsers extract real line numbers from pytest/mypy
output, and they are *total* — malformed, empty, or binary input degrades to a
safe generic diagnostic and never raises (a crash here would take down the
FastAPI/LangGraph worker thread).
"""
from __future__ import annotations

from tools.validation.diagnostics import (
    format_diagnostics,
    parse_generic,
    parse_mypy,
    parse_pytest,
    select_parser,
)
from tools.validation.result import ValidationError


# --------------------------------------------------------------------------- #
# mypy
# --------------------------------------------------------------------------- #


def test_parse_mypy_extracts_line_and_code() -> None:
    out = (
        "app/models.py:17: error: Incompatible return value type "
        '(got "int", expected "str")  [return-value]\n'
        "Found 1 error in 1 file (checked 3 source files)\n"
    )
    errors = parse_mypy(out)
    assert any(e.line == 17 for e in errors)
    assert any("return-value" in e.message for e in errors)


def test_parse_mypy_handles_column_form() -> None:
    out = "x.py:5:9: error: Name 'foo' is not defined  [name-defined]\n"
    errors = parse_mypy(out)
    assert errors[0].line == 5
    assert errors[0].column == 9


# --------------------------------------------------------------------------- #
# pytest
# --------------------------------------------------------------------------- #


def test_parse_pytest_extracts_failed_summary() -> None:
    out = (
        "=== short test summary info ===\n"
        "FAILED tests/test_x.py::test_one - AssertionError: 1 != 2\n"
        "FAILED tests/test_y.py::test_two - ValueError: bad\n"
    )
    errors = parse_pytest(out)
    msgs = " ".join(e.message for e in errors)
    assert "test_x.py" in msgs and "AssertionError" in msgs
    assert "test_y.py" in msgs


def test_parse_pytest_enriches_line_from_body() -> None:
    out = (
        "tests/test_x.py:88: in test_one\n"
        "    assert add(1, 1) == 3\n"
        "FAILED tests/test_x.py::test_one - assert 2 == 3\n"
    )
    errors = parse_pytest(out)
    assert any(e.line == 88 for e in errors)


# --------------------------------------------------------------------------- #
# generic + selector
# --------------------------------------------------------------------------- #


def test_parse_generic_bounds_message() -> None:
    huge = "x" * 5000
    errors = parse_generic(huge, "")
    assert len(errors) == 1
    assert "TRUNCATED" in errors[0].message
    assert len(errors[0].message) < 2000


def test_parse_generic_empty_is_safe() -> None:
    errors = parse_generic("", "")
    assert len(errors) == 1
    assert errors[0].message


def test_select_parser_routes_by_command() -> None:
    assert select_parser("pytest -q tests/") is parse_pytest
    assert select_parser("python -m mypy .") is parse_mypy
    assert select_parser("npx tsc --noEmit") is parse_mypy
    assert select_parser("make build") is parse_generic
    assert select_parser("") is parse_generic


# --------------------------------------------------------------------------- #
# Totality / crash-safety contract
# --------------------------------------------------------------------------- #


def test_parsers_never_raise_on_garbage() -> None:
    garbage_inputs = [
        "\x00\x01\x02 binary noise \xff\xfe",
        "::::::::",
        "FAILED\nFAILED\nFAILED",
        ":\n:\n:\n",
        "x.py:notanumber: error: msg [code]",
        "\n\n\n",
    ]
    for g in garbage_inputs:
        # None of these may raise — each must yield at least one diagnostic.
        assert parse_mypy(g) and isinstance(parse_mypy(g)[0], ValidationError)
        assert parse_pytest(g) and isinstance(parse_pytest(g)[0], ValidationError)
        assert parse_generic(g) and isinstance(parse_generic(g)[0], ValidationError)


def test_format_diagnostics_empty_is_safe() -> None:
    assert format_diagnostics([]) == "[no structured diagnostics extracted]"


def test_format_diagnostics_respects_cap() -> None:
    errors = [ValidationError(layer="LSP", line=i, message="m" * 200) for i in range(100)]
    rendered = format_diagnostics(errors, cap=500)
    assert len(rendered) <= 500


def test_format_diagnostics_renders_line() -> None:
    errors = [ValidationError(layer="LSP", line=12, message="boom")]
    rendered = format_diagnostics(errors)
    assert "12" in rendered and "boom" in rendered
