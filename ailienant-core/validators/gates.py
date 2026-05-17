# ailienant-core/validators/gates.py
"""Phase 4.2 — Deterministic gate nodes for LangGraph (no LLM, zero tokens).

  * :func:`syntax_gate_node` — wraps :func:`ast.parse`; writes ``syntax_gate_status``.
  * :func:`style_gate_node`  — wraps ``ruff check --stdin`` via subprocess; tracks
    ``consecutive_style_failures``; inline Give-Up Gate (latches
    ``style_bypass_active`` when ``consecutive_style_failures >= STYLE_BYPASS_THRESHOLD``).

Pure-function helpers (:func:`validate_syntax`, :func:`validate_style`) are
exported so unit tests can exercise the validation logic without state-channel
plumbing. Both gate nodes honour the R1 state-key contract — they return ONLY
keys that exist as fields on :class:`AIlienantGraphState`.
"""
from __future__ import annotations

import ast
import asyncio
import logging
import sys
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger("VALIDATORS_GATES")

# Blueprint §4.1 — Give-Up Gate ceiling.
STYLE_BYPASS_THRESHOLD: int = 2

# R8 — subprocess deadlock guard.
_RUFF_TIMEOUT_S: float = 10.0


# ── Pure-function helpers ────────────────────────────────────────────────────


def validate_syntax(code: str) -> Tuple[bool, Optional[str]]:
    """Return ``(passed, error_message)``. ``(True, None)`` on success."""
    if not code.strip():
        return True, None
    try:
        ast.parse(code)
        return True, None
    except SyntaxError as e:
        return False, f"SyntaxError: {e.msg} (line {e.lineno}, col {e.offset})"


async def validate_style(
    code: str,
    interpreter: Optional[str] = None,
    timeout: float = _RUFF_TIMEOUT_S,
) -> Tuple[bool, Optional[str]]:
    """Pipe *code* through ``python -m ruff check --stdin``. Pure async function.

    R9 — graceful degradation when ruff is missing or interpreter is bogus.
    R8 — hard timeout + ``proc.kill()`` if the child stalls.
    """
    py = interpreter or sys.executable
    try:
        proc = await asyncio.create_subprocess_exec(
            py,
            "-m",
            "ruff",
            "check",
            "--no-cache",
            "--stdin-filename",
            "stdin.py",
            "-",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError:
        return False, f"interpreter not found: {py}"

    try:
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(code.encode("utf-8")),
            timeout=timeout,
        )
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return False, f"ruff timed out after {timeout}s"

    if proc.returncode == 0:
        return True, None

    combined = (stdout + stderr).decode("utf-8", errors="replace").strip()
    # Distinguish "ruff missing" from "real style violation" for R9.
    if "no module named ruff" in combined.lower():
        return False, f"ruff module not available at {py}"
    return False, combined or f"ruff exit code {proc.returncode}"


# ── LangGraph node wrappers ──────────────────────────────────────────────────


def _extract_code(state: Dict[str, Any]) -> str:
    """Read code-under-validation from state (Phase 4.2 TRANSITIONAL).

    The ``code_under_validation`` field is a unit-test isolation convenience
    that duplicates content already present in ``state["vfs_buffer"]`` and
    ``state["pending_patches"]``. Every LangGraph checkpoint persists this
    duplicate to SQLite WAL + LanceDB — O(N) state bloat per patch.

    TODO(phase-4.3): replace this with resolution from vfs_buffer (blob_storage
    lookup) or pending_patches (in-memory diff apply). Remove the
    ``code_under_validation`` field from AIlienantGraphState as part of the
    same PR. Tracked in docs/PROJECT_MANIFEST.md Tech Debt section.
    """
    return str(state.get("code_under_validation") or "")


async def syntax_gate_node(state: Dict[str, Any]) -> Dict[str, Any]:
    """Phase 4.2 — ``ast.parse`` the code under validation.

    State-key contract: returns ``syntax_gate_status`` (always) and
    ``errors`` (only on fail). No phantom keys.
    """
    code = _extract_code(state)
    if not code:
        return {"syntax_gate_status": "pending"}

    passed, err = validate_syntax(code)
    if passed:
        return {"syntax_gate_status": "pass"}

    return {
        "syntax_gate_status": "fail",
        "errors": [err or "syntax error"],
    }


async def style_gate_node(state: Dict[str, Any]) -> Dict[str, Any]:
    """Phase 4.2 — ruff check + inline Give-Up Gate.

    Behaviour matrix:
        * No code under validation     → no state mutation (empty return).
        * Style pass                   → ``consecutive_style_failures`` reset to 0.
        * Syntax has not yet passed    → ``errors`` only (not a style-only failure;
                                         Give-Up Gate does NOT trip).
        * Style-only fail (syntax pass)→ increment ``consecutive_style_failures``.
                                         If new count ≥ ``STYLE_BYPASS_THRESHOLD`` (= 2),
                                         latch ``style_bypass_active=True`` and emit
                                         the ``STYLE_BYPASS_ACTIVATED`` security flag
                                         for downstream Analyst handoff.

    R1 state-key contract: returns ONLY ``consecutive_style_failures``,
    ``style_bypass_active``, ``errors``, ``security_flags`` — all declared on
    :class:`AIlienantGraphState`. The defunct ``style_gate_status`` field
    (deferred per blueprint §1) is NOT in the schema so it is NOT returned.
    """
    code = _extract_code(state)
    if not code:
        return {}

    interpreter = state.get("venv_interpreter_path")
    interpreter_arg = interpreter if isinstance(interpreter, str) else None
    passed, err = await validate_style(code, interpreter=interpreter_arg)

    if passed:
        return {"consecutive_style_failures": 0}

    # Style failed. If syntax has not been verified or is currently failing,
    # do NOT trip the Give-Up Gate (this is not a style-only failure).
    syntax_status = state.get("syntax_gate_status", "pending")
    if syntax_status != "pass":
        return {"errors": [err or "style error"]}

    current = int(state.get("consecutive_style_failures", 0))
    new_count = current + 1

    result: Dict[str, Any] = {
        "consecutive_style_failures": new_count,
        "errors": [err or "style error"],
    }

    if new_count >= STYLE_BYPASS_THRESHOLD:
        result["style_bypass_active"] = True
        result["security_flags"] = ["STYLE_BYPASS_ACTIVATED"]
        logger.warning(
            "Give-Up Gate tripped: consecutive_style_failures=%d ≥ threshold=%d. "
            "Latching style_bypass_active=True for downstream Analyst handoff.",
            new_count,
            STYLE_BYPASS_THRESHOLD,
        )

    return result
