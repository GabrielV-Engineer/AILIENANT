"""brain/checks_gate.py — Executes the plan's own acceptance criteria at turn end.

The planner generates `MissionSpecification.checks` as its own definition of
done ("Pytest exits 0", "the module imports", "FeatureCard receives props
correctly"). Project-wide, the only prior consumer of this field was a logger
counting how many there were — never run, never verified, never even shown to
the coder while it wrote the code. A live incident made the cost of that
concrete: the model's own generated checks named the exact defect that shipped
("Verify that FeatureCard and Testimonial are correctly utilized... via
props"), and nothing on the path ever asked the question the plan itself had
already written down.

Not every check is mechanically executable — the props example above requires
semantic code review, not a shell command. This node recognises a narrow,
explicit set of common CI-style checks (pytest, the project's own lint/type
gates, npm test/build/lint) and runs ONLY those, through the exact same
guarded-command path (`tools.execution_tools.run_guarded_command`) every other
command execution in the system already goes through — the same permission
gate, the same dangerous-pattern interception, no bypass for being "just a
check." Every other check is reported `unverified`, never silently dropped and
never counted as passing — declared MVP scope (charter §11), not an oversight:
turning arbitrary free-text acceptance criteria into safely executable
commands in general is a much larger problem than this pass takes on.
"""
from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional, Pattern, Tuple

logger = logging.getLogger("CHECKS_GATE")

# Recognised checks, ordered narrowest-first so a more specific pattern (e.g.
# "npm run lint") is not shadowed by "npm run build"'s own bare "npm" mention.
# Deliberately a short, explicit, auditable list — the same "no hidden catalog"
# principle agents/planner.py's own STACK CHOICE directive follows — rather than
# an attempt at general natural-language-to-command translation, which would
# risk running something the check text never actually asked for.
_KNOWN_CHECK_PATTERNS: Tuple[Tuple[Pattern[str], str], ...] = (
    (re.compile(r"\bpytest\b", re.I), "pytest"),
    (re.compile(r"\bmypy\b", re.I), "mypy ."),
    (re.compile(r"\bruff\s+check\b|\bruff\b", re.I), "ruff check ."),
    (re.compile(r"\bnpm\s+run\s+lint\b|\beslint\b", re.I), "npm run lint"),
    (re.compile(r"\bnpm\s+run\s+build\b", re.I), "npm run build"),
    (re.compile(r"\bnpm\s+(run\s+)?test\b", re.I), "npm test"),
)

# Bounded, matching CORRECTION_MAX_ATTEMPTS's own philosophy — this is a
# read-only verification pass at turn end, not a self-heal loop, so there is
# no retry: a single guarded run per matched check.
_CHECK_TIMEOUT_SEC: float = 120.0


def match_executable_command(check_text: str) -> Optional[str]:
    """Return the command a recognised check maps to, or ``None``.

    Pure and side-effect-free so the matching logic is trivially unit-testable
    without touching the guarded-command machinery at all.
    """
    for pattern, command in _KNOWN_CHECK_PATTERNS:
        if pattern.search(check_text or ""):
            return command
    return None


async def run_checks_node(state: Dict[str, Any]) -> Dict[str, Any]:
    """LangGraph terminal node: execute the mechanically executable subset of
    ``mission_spec.checks``, honestly, before the turn is reported complete.

    Runs once, after every WBS step has reached a terminal status (wired at
    ``route_after_validation``'s ``wbs_all_steps_terminal`` branch) — never on
    a stalled or aborted turn, since there is nothing to verify yet. Returns a
    ``check_results`` state delta (one entry per check, ``passed`` /
    ``failed`` / ``unverified``) and folds any ``failed`` verdict into
    ``errors`` so the existing turn-end summary (``core/task_service.py::
    _format_coding_summary``) can never present a failed check as a quiet
    success — the same discipline the per-step apply gate already applies to
    a denied or rejected write.

    Never raises: a fault in this node must not turn an otherwise-successful
    turn into a hard crash — it degrades to reporting the affected checks
    unverified.
    """
    mission = state.get("mission_spec")
    checks: List[str] = list(getattr(mission, "checks", None) or [])
    if not checks:
        return {}

    from tools.execution_tools import (
        _GUARD_REFUSED_EXIT_CODE,
        render_guarded_command_result,
        run_guarded_command,
    )

    session_id = str(state.get("task_id", ""))
    session_permission_mode = state.get("session_permission_mode")
    results: List[Dict[str, Any]] = []
    new_errors: List[str] = []

    for check_text in checks:
        command = match_executable_command(check_text)
        if command is None:
            results.append({"check": check_text, "command": None, "status": "unverified"})
            continue
        try:
            outcome = await run_guarded_command(
                command,
                timeout_sec=_CHECK_TIMEOUT_SEC,
                working_dir=state.get("workspace_root"),
                session_id=session_id,
                session_permission_mode=session_permission_mode,
            )
        except Exception:  # noqa: BLE001 — a guard/execution fault degrades to unverified
            logger.warning(
                "checks_gate: executing %r for check %r failed", command, check_text, exc_info=True,
            )
            results.append({"check": check_text, "command": command, "status": "unverified"})
            continue

        if outcome.exit_code == 0:
            results.append({"check": check_text, "command": command, "status": "passed"})
        elif outcome.exit_code == _GUARD_REFUSED_EXIT_CODE:
            # The guard chain (permission gate, dangerous-pattern interceptor)
            # refused to even attempt the command — this says nothing about
            # whether the check's own condition holds, so it is honestly
            # "we could not determine this," not "we determined it failed."
            results.append({"check": check_text, "command": command, "status": "unverified"})
        else:
            detail = render_guarded_command_result(outcome)
            results.append({
                "check": check_text, "command": command, "status": "failed", "detail": detail,
            })
            new_errors.append(f"Acceptance check failed — {check_text!r} ({command}): {detail[:300]}")

    logger.info(
        "checks_gate: %d check(s) — %d passed, %d failed, %d unverified",
        len(results),
        sum(1 for r in results if r["status"] == "passed"),
        sum(1 for r in results if r["status"] == "failed"),
        sum(1 for r in results if r["status"] == "unverified"),
    )
    delta: Dict[str, Any] = {"check_results": results}
    if new_errors:
        delta["errors"] = new_errors
    return delta
