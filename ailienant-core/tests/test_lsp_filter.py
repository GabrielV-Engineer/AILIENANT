# tests/test_lsp_filter.py
"""Phase 3.4.4 — LSP subprocess pipe (Layer 2). Subprocess mocked throughout."""
from __future__ import annotations

import asyncio
import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from tools.validation.lsp_filter import validate_lsp


def _fake_proc(returncode: int, stdout: bytes = b"", stderr: bytes = b"") -> Any:
    """Fake asyncio subprocess: .communicate() AsyncMock + .returncode int."""
    proc = MagicMock()
    proc.communicate = AsyncMock(return_value=(stdout, stderr))
    proc.returncode = returncode
    return proc


# ---------- ruff (Python) ----------

@pytest.mark.anyio
async def test_ruff_clean_passes() -> None:
    proc = _fake_proc(returncode=0, stdout=b"[]")
    with patch(
        "tools.validation.lsp_filter.asyncio.create_subprocess_exec",
        new=AsyncMock(return_value=proc),
    ):
        result = await validate_lsp("x = 1\n", "ok.py")
    assert result.is_valid is True


@pytest.mark.anyio
async def test_ruff_violation_fails() -> None:
    diags = [{
        "code": "F401",
        "message": "`os` imported but unused",
        "location": {"row": 1, "column": 1},
    }]
    proc = _fake_proc(returncode=1, stdout=json.dumps(diags).encode("utf-8"))
    with patch(
        "tools.validation.lsp_filter.asyncio.create_subprocess_exec",
        new=AsyncMock(return_value=proc),
    ):
        result = await validate_lsp("import os\n", "bad.py")
    assert result.is_valid is False
    assert result.errors[0].layer == "LSP"
    assert "F401" in result.errors[0].message
    assert result.prune_reason is not None
    assert "bad.py" in result.prune_reason


@pytest.mark.anyio
async def test_ruff_filenotfound_passes_gracefully() -> None:
    """No python executable -> graceful pass-through (do NOT prune)."""
    with patch(
        "tools.validation.lsp_filter.asyncio.create_subprocess_exec",
        new=AsyncMock(side_effect=FileNotFoundError("python not on PATH")),
    ):
        result = await validate_lsp("x = 1\n", "x.py")
    assert result.is_valid is True


@pytest.mark.anyio
async def test_ruff_timeout_passes_gracefully() -> None:
    """Linter timeout -> graceful pass-through (no false prune)."""
    with patch(
        "tools.validation.lsp_filter.asyncio.wait_for",
        new=AsyncMock(side_effect=asyncio.TimeoutError()),
    ):
        with patch(
            "tools.validation.lsp_filter.asyncio.create_subprocess_exec",
            new=AsyncMock(return_value=_fake_proc(0)),
        ):
            result = await validate_lsp("x = 1\n", "x.py", timeout=0.001)
    assert result.is_valid is True


@pytest.mark.anyio
async def test_ruff_nonzero_with_no_diags_passes() -> None:
    """Nonzero exit but unparseable / empty stdout -> conservative pass."""
    proc = _fake_proc(returncode=2, stdout=b"not json")
    with patch(
        "tools.validation.lsp_filter.asyncio.create_subprocess_exec",
        new=AsyncMock(return_value=proc),
    ):
        result = await validate_lsp("x = 1\n", "x.py")
    assert result.is_valid is True


# ---------- eslint (TS) — graceful degradation ----------

@pytest.mark.anyio
async def test_eslint_filenotfound_passes_gracefully() -> None:
    """No npx on PATH -> graceful pass-through."""
    with patch(
        "tools.validation.lsp_filter.asyncio.create_subprocess_exec",
        new=AsyncMock(side_effect=FileNotFoundError("npx not found")),
    ):
        result = await validate_lsp("const x = 1;\n", "a.ts")
    assert result.is_valid is True


@pytest.mark.anyio
async def test_eslint_violation_fails() -> None:
    eslint_output = [{
        "filePath": "a.ts",
        "messages": [{
            "ruleId": "no-unused-vars",
            "message": "'x' is assigned a value but never used.",
            "line": 1, "column": 7,
        }],
    }]
    proc = _fake_proc(returncode=1, stdout=json.dumps(eslint_output).encode("utf-8"))
    with patch(
        "tools.validation.lsp_filter.asyncio.create_subprocess_exec",
        new=AsyncMock(return_value=proc),
    ):
        result = await validate_lsp("const x = 1;\n", "a.ts")
    assert result.is_valid is False
    assert result.errors[0].layer == "LSP"
    assert "no-unused-vars" in result.errors[0].message


@pytest.mark.anyio
async def test_eslint_npx_module_missing_passes_gracefully() -> None:
    """npx reports missing package -> infra absence, not a code defect."""
    proc = _fake_proc(
        returncode=1,
        stdout=b"[]",
        stderr=b"npx: could not determine executable to run",
    )
    with patch(
        "tools.validation.lsp_filter.asyncio.create_subprocess_exec",
        new=AsyncMock(return_value=proc),
    ):
        result = await validate_lsp("const x = 1;\n", "a.ts")
    assert result.is_valid is True


# ---------- Pass-through ----------

@pytest.mark.anyio
async def test_unsupported_extension_skips_subprocess() -> None:
    """validate_lsp on an unknown extension must not spawn a subprocess."""
    with patch(
        "tools.validation.lsp_filter.asyncio.create_subprocess_exec",
        new=AsyncMock(side_effect=AssertionError("should NOT be called")),
    ):
        result = await validate_lsp("anything", "notes.md")
    assert result.is_valid is True
