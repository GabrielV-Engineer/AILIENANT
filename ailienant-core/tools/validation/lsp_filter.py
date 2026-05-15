# tools/validation/lsp_filter.py
"""Phase 3.4.4 — Layer 2 CLI-based linter pipe (subprocess only, no JSON-RPC).

Python  ->  `python -m ruff check --stdin-filename {path} -` (ruff installed via requirements)
TS/TSX  ->  `npx eslint --stdin --stdin-filename {path} --format json` (graceful no-op if missing)

Graceful degradation: missing linters / Node / timeouts return is_valid=True with
a warning log. A legitimate code candidate is never pruned for an infrastructure
absence — only for real diagnostics the linter explicitly emits.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from typing import Any, Dict, List, Optional

from tools.validation.result import ValidationError, ValidationResult

logger = logging.getLogger("LSP_FILTER")

_PY_EXTS = frozenset({".py"})
_TS_EXTS = frozenset({".ts", ".tsx"})


async def validate_lsp(
    content: str,
    file_path: str,
    timeout: float = 5.0,
) -> ValidationResult:
    """Run the appropriate linter for `file_path`. Pass-through on unsupported."""
    ext: str = os.path.splitext(file_path)[1].lower()
    if ext in _PY_EXTS:
        return await _ruff_check(content, file_path, timeout)
    if ext in _TS_EXTS:
        return await _eslint_check(content, file_path, timeout)
    return ValidationResult(is_valid=True)


async def _ruff_check(
    content: str,
    file_path: str,
    timeout: float,
) -> ValidationResult:
    try:
        proc = await asyncio.create_subprocess_exec(
            sys.executable, "-m", "ruff", "check",
            "--stdin-filename", file_path,
            "--output-format", "json",
            "--no-cache",
            "-",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout_bytes, _stderr_bytes = await asyncio.wait_for(
            proc.communicate(input=content.encode("utf-8")),
            timeout=timeout,
        )
    except FileNotFoundError:
        logger.warning("LSP(ruff): python executable not found; passing through")
        return ValidationResult(is_valid=True)
    except asyncio.TimeoutError:
        logger.warning("LSP(ruff): timed out after %ss; passing through", timeout)
        return ValidationResult(is_valid=True)

    if proc.returncode == 0:
        return ValidationResult(is_valid=True)

    diags: List[Dict[str, Any]] = _parse_ruff_json(stdout_bytes)
    errors: List[ValidationError] = []
    for d in diags:
        loc: Dict[str, Any] = d.get("location") or {}
        code: str = str(d.get("code") or "")
        msg: str = str(d.get("message") or "")
        compound: str = f"{code}: {msg}".strip(": ").strip()
        errors.append(ValidationError(
            layer="LSP",
            line=loc.get("row") if isinstance(loc.get("row"), int) else None,
            column=loc.get("column") if isinstance(loc.get("column"), int) else None,
            message=compound or "ruff diagnostic",
        ))

    if not errors:
        # Non-zero exit but no parseable diags — treat as pass to avoid false prunes.
        logger.warning(
            "LSP(ruff): exit=%s but no JSON diags; passing through", proc.returncode,
        )
        return ValidationResult(is_valid=True)

    first: ValidationError = errors[0]
    return ValidationResult(
        is_valid=False,
        errors=errors,
        prune_reason=f"LSP(ruff): {file_path}:{first.line}: {first.message}",
    )


def _parse_ruff_json(stdout_bytes: bytes) -> List[Dict[str, Any]]:
    """Defensive JSON parsing for ruff's stdout."""
    try:
        decoded: str = stdout_bytes.decode("utf-8") or "[]"
        parsed: Any = json.loads(decoded)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return []
    if not isinstance(parsed, list):
        return []
    return [d for d in parsed if isinstance(d, dict)]


async def _eslint_check(
    content: str,
    file_path: str,
    timeout: float,
) -> ValidationResult:
    """Best-effort npx eslint --stdin. No Node / no eslint = graceful pass-through."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "npx", "--no-install", "eslint",
            "--stdin",
            "--stdin-filename", file_path,
            "--format", "json",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout_bytes, stderr_bytes = await asyncio.wait_for(
            proc.communicate(input=content.encode("utf-8")),
            timeout=timeout,
        )
    except FileNotFoundError:
        logger.warning("LSP(eslint): npx not found; passing through")
        return ValidationResult(is_valid=True)
    except asyncio.TimeoutError:
        logger.warning("LSP(eslint): timed out after %ss; passing through", timeout)
        return ValidationResult(is_valid=True)

    if proc.returncode == 0:
        return ValidationResult(is_valid=True)

    # npx returns 1 when the package isn't installed; that's an infra absence, not a bug.
    stderr_text: str = stderr_bytes.decode("utf-8", errors="replace")
    if "could not determine executable" in stderr_text.lower() \
       or "command not found" in stderr_text.lower():
        logger.warning("LSP(eslint): not installed (npx stderr); passing through")
        return ValidationResult(is_valid=True)

    errors: List[ValidationError] = _parse_eslint_json(stdout_bytes)
    if not errors:
        logger.warning(
            "LSP(eslint): exit=%s but no parseable diags; passing through",
            proc.returncode,
        )
        return ValidationResult(is_valid=True)

    first: ValidationError = errors[0]
    return ValidationResult(
        is_valid=False,
        errors=errors,
        prune_reason=f"LSP(eslint): {file_path}:{first.line}: {first.message}",
    )


def _parse_eslint_json(stdout_bytes: bytes) -> List[ValidationError]:
    """ESLint JSON: list of files, each with `messages` list of diagnostics."""
    try:
        decoded: str = stdout_bytes.decode("utf-8") or "[]"
        parsed: Any = json.loads(decoded)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return []
    if not isinstance(parsed, list):
        return []
    errors: List[ValidationError] = []
    for file_report in parsed:
        if not isinstance(file_report, dict):
            continue
        for m in file_report.get("messages", []):
            if not isinstance(m, dict):
                continue
            line: Optional[int] = m.get("line") if isinstance(m.get("line"), int) else None
            col: Optional[int] = m.get("column") if isinstance(m.get("column"), int) else None
            rule_id: str = str(m.get("ruleId") or "")
            msg: str = str(m.get("message") or "")
            compound: str = f"{rule_id}: {msg}".strip(": ").strip()
            errors.append(ValidationError(
                layer="LSP", line=line, column=col,
                message=compound or "eslint diagnostic",
            ))
    return errors
