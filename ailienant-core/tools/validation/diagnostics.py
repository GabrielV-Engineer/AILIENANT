"""Structured diagnostics extraction for the closed-loop executor.

When the coder dispatches a verification command (``pytest``, ``mypy``, ``tsc``)
into the sandbox and it fails, the self-heal loop must re-read the failure. Feeding
back the *raw* stdout/stderr is an anti-pattern: long tracebacks truncate the
context window, waste token budget, and inflate attention cost quadratically in the
re-read length. Instead this module distills the output into a compact list of
``[file, line, code, message]`` diagnostics — local parsing is linear in log size
(microseconds) and slashes the prompt the model re-reads on every correction cycle.

The diagnostic shape is reused from ``tools.validation.result`` (``ValidationError``)
so the executor speaks the same vocabulary the Micro-Isolate pipeline already uses.

Event-loop safety (binding): every public parser is total — it MUST NEVER raise.
A parse failure on the FastAPI/LangGraph loop would take down the worker. Each
specialised parser degrades to ``parse_generic`` on any exception, and
``parse_generic`` is a pure bounded slice that cannot fail. A miss yields a single
generic diagnostic, never an empty deref or an ``IndexError``.
"""
from __future__ import annotations

import logging
import re
from typing import Callable, List

from tools.validation.result import ValidationError

logger = logging.getLogger("DIAGNOSTICS")

# Cap on the rendered diagnostic blob fed back into state — mirrors the
# error_correction trace cap so the self-heal payload stays bounded.
_DIAG_CAP: int = 4000

# Cap on a single message kept from an unrecognized tool's output. The generic
# parser keeps a head+tail window so both the command framing and the final error
# survive truncation.
_GENERIC_MSG_CAP: int = 1200

# mypy default text line:  path:line: error: message  [error-code]
# column is optional (mypy emits `path:line:col:` only with --show-column-numbers).
_MYPY_RE = re.compile(
    r"^(?P<path>[^:\n]+):(?P<line>\d+):(?:(?P<col>\d+):)?\s*"
    r"(?:error|warning):\s*(?P<msg>.*?)\s*(?:\[(?P<code>[\w-]+)\])?$"
)

# pytest short-summary line:  FAILED path::test - ExcClass: message
_PYTEST_SUMMARY_RE = re.compile(
    r"^(?:FAILED|ERROR)\s+(?P<path>[^:\s]+)(?:::[^\s]+)?\s*(?:-\s*(?P<msg>.*))?$"
)

# A `file.py:line:` marker anywhere in a pytest traceback body, used as a fallback
# to attach a line number when the summary line carries none.
_FILE_LINE_RE = re.compile(r"(?P<path>[\w./\\-]+\.\w+):(?P<line>\d+):")


def _safe_int(text: str) -> int | None:
    try:
        return int(text)
    except (TypeError, ValueError):
        return None


def parse_mypy(stdout: str, stderr: str = "") -> List[ValidationError]:
    """Extract mypy diagnostics from text output. Total — never raises."""
    try:
        errors: List[ValidationError] = []
        for line in (stdout or "").splitlines():
            m = _MYPY_RE.match(line.strip())
            if not m:
                continue
            code = m.group("code") or ""
            msg = m.group("msg") or ""
            compound = f"{code}: {msg}".strip(": ").strip() if code else msg.strip()
            errors.append(
                ValidationError(
                    layer="LSP",
                    line=_safe_int(m.group("line")),
                    column=_safe_int(m.group("col")) if m.group("col") else None,
                    message=compound or "mypy diagnostic",
                )
            )
        return errors or parse_generic(stdout, stderr)
    except Exception as exc:  # noqa: BLE001 — total contract; degrade, never raise
        logger.debug("parse_mypy degraded to generic: %s", exc)
        return parse_generic(stdout, stderr)


def parse_pytest(stdout: str, stderr: str = "") -> List[ValidationError]:
    """Extract pytest failures from the short test summary. Total — never raises.

    Only the one-line ``FAILED ... - Exc: msg`` summary entries are kept (pytest
    prints these under ``=== short test summary info ===``); full per-test
    tracebacks are deliberately dropped to keep the re-injected payload small.
    A ``file:line:`` marker found in the body is used to enrich the line number.
    """
    try:
        text = stdout or ""
        # Best-effort line hint: the deepest file:line marker in the body.
        line_hint: int | None = None
        for fm in _FILE_LINE_RE.finditer(text):
            li = _safe_int(fm.group("line"))
            if li is not None:
                line_hint = li

        errors: List[ValidationError] = []
        for line in text.splitlines():
            m = _PYTEST_SUMMARY_RE.match(line.strip())
            if not m:
                continue
            msg = (m.group("msg") or "test failed").strip()
            errors.append(
                ValidationError(
                    layer="LSP",
                    line=line_hint,
                    column=None,
                    message=f"{m.group('path')}: {msg}".strip(),
                )
            )
        return errors or parse_generic(stdout, stderr)
    except Exception as exc:  # noqa: BLE001 — total contract; degrade, never raise
        logger.debug("parse_pytest degraded to generic: %s", exc)
        return parse_generic(stdout, stderr)


def parse_generic(stdout: str, stderr: str = "") -> List[ValidationError]:
    """Fallback for unrecognized tools: one bounded diagnostic. Cannot fail.

    Keeps a head+tail window of the combined output so both the leading framing
    and the trailing error survive the cap.
    """
    combined = f"{stdout or ''}\n{stderr or ''}".strip()
    if not combined:
        return [ValidationError(layer="LSP", message="command failed (no output)")]
    n = len(combined)
    if n > _GENERIC_MSG_CAP:
        head = _GENERIC_MSG_CAP // 2
        tail = _GENERIC_MSG_CAP - head
        combined = (
            combined[:head]
            + f"\n...[TRUNCATED {n - _GENERIC_MSG_CAP} CHARS]...\n"
            + combined[-tail:]
        )
    return [ValidationError(layer="LSP", message=combined)]


def select_parser(command: str) -> Callable[[str, str], List[ValidationError]]:
    """Pick a parser by command substring. Defaults to the generic parser."""
    cmd = (command or "").lower()
    if "pytest" in cmd:
        return parse_pytest
    if "mypy" in cmd or "tsc" in cmd:
        return parse_mypy
    return parse_generic


def format_diagnostics(errors: List[ValidationError], cap: int = _DIAG_CAP) -> str:
    """Render diagnostics as compact ``[file:line] message`` lines, hard-capped.

    Defends against ``None``/empty so a caller can hand it any parse result. The
    output is what lands in ``last_error_trace`` for the self-heal loop.
    """
    if not errors:
        return "[no structured diagnostics extracted]"
    lines: List[str] = []
    for e in errors:
        loc = f":{e.line}" if e.line is not None else ""
        lines.append(f"[{loc.lstrip(':') or '?'}] {e.message}".strip())
    blob = "\n".join(lines)
    if len(blob) > cap:
        return blob[: cap - 1] + "…"
    return blob
