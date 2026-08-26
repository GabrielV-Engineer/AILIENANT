# ailienant-core/brain/apply_gate.py
"""Incremental per-step approval (13.0.9).

Replaces the post-graph replay that used to live in ``core/task_service.py``
(a single pass over the WHOLE WBS's accumulated ``pending_patches`` dict,
after every step had already run to completion) with two graph nodes that
gate ONE step at a time, inside the graph:

    supervisor_node -> apply_patch (PREPARE, no interrupt)
                     -> apply_commit (GATE, interrupt-first)
                     -> validate_output

``apply_patch`` (PREPARE) computes everything deterministic and side-effect-
free about this step's proposed change — diffs, risk labels, the permission
verdict, the cumulative blast radius, pre_patch hooks — and commits the result
to ``pending_apply``. It never interrupts.

``apply_commit`` (GATE) is interrupt-first: its FIRST side-effectful action is
either a native ``interrupt()`` (via ``core.hitl.request_graph_approval``, for
a HITL-tier decision) or nothing at all (ALLOW/auto-accept). Everything before
that first action is pure, idempotent, checkpoint-committed-already logic, so
a resume replays byte-identically — the same invariant ``brain/drift_monitor.py``
established for ``drift_compute``/``drift_gate`` and ``brain/finops.py`` for its
own budget interrupt. See DEBT-185 (docs/TECH_DEBT_BACKLOG.md) for why this
split is not optional: interrupting mid-generation would re-run the LLM on
resume and apply the operator's decision to different bytes than the ones
they actually saw.

Confirmed live bugs this closes (docs/DEV_JOURNAL.md 13.0.9):
  - approving files 1-3 then requesting changes on file 4 used to discard all
    three (the old code's revise branch returned before `patches_to_apply =
    accepted` was ever assigned) — now impossible, since each step's approved
    write already landed on disk before the next step's card ever appears.
  - a step was marked "completed" the instant the coder generated it, before
    any human ever saw the diff — the checklist was lying. Statuses here are
    only ever written after the actual decision (or a genuine apply-time
    fault) is known.
  - a plain reject silently dropped the file with no retry and no self-heal
    signal — now a terminal `rejected` status with a recorded error.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from langchain_core.runnables import RunnableConfig

from agents.orchestrator import _mark_step_status
from api.ws_contracts import ProposedFile
from brain.retry_policy import APPLY_REJECT_MAX_ATTEMPTS
from brain.state import WBSStep, is_terminal
from core.hitl import request_graph_approval
from core.permissions import (
    PermissionDecision,
    SessionPermissionMode,
    ToolPrivilegeTier,
    evaluate_action,
    gate_execute_action,
    risk_intercept_guard,
    scan_risk_patterns,
    session_mode_from_channel,
)
from core.storage_paths import is_ailienant_internal_path
from shared.rbac import PermissionMode

logger = logging.getLogger("APPLY_GATE")

# The last N characters of stdout/stderr carried into last_execution_context
# for the self-heal companion (brain/coder_companion.py) — a compile/test
# failure's actionable detail is almost always at the END of the output, and
# capping per-stream keeps a runaway command from crowding out the fixed-size
# fields (command, exit_code) once everything is joined into one scope_summary.
_EXEC_TAIL_CHARS = 800


def _target_step(state: Dict[str, Any]) -> Optional[WBSStep]:
    """Resolve the WBS step this super-step is currently dispatched to, or
    None if there's nothing to prepare/commit (mission missing, step id
    unresolved, or the step no longer exists in the plan)."""
    mission = state.get("mission_spec")
    step_id = state.get("current_step_id")
    if mission is None or step_id is None:
        return None
    return next((t for t in mission.tasks if t.step_number == step_id), None)


def _added_lines(diff: str) -> str:
    """The '+' side of a unified diff, minus the '+++' file header — the
    content this edit introduces. Risk is judged on added lines only, so an
    unchanged region that merely mentions a secret token never blocks
    auto-accept (mirrors the pre-13.0.9 behavior exactly)."""
    return "".join(
        ln[1:]
        for ln in diff.splitlines(keepends=True)
        if ln.startswith("+") and not ln.startswith("+++")
    )


async def run_apply_prepare_node(
    state: Dict[str, Any], config: Optional[RunnableConfig] = None
) -> Dict[str, Any]:
    """PREPARE — deterministic, side-effect-free (besides advisory hooks and
    the blast-radius mapper, both fail-open/fail-closed by design, never an
    interrupt). Commits ``pending_apply`` for ``apply_commit`` to act on."""
    step = _target_step(state)
    if step is None or is_terminal(step):
        return {}

    session_id: str = state.get("task_id", "")
    step_key = str(step.step_number)
    paths: List[str] = list((state.get("pending_step_files") or {}).get(step_key, []))
    command: Optional[str] = (state.get("pending_step_command") or {}).get(step_key)

    if not paths and not command:
        # Orphaned-in_progress backstop (found on the second design audit
        # pass): a step whose generation produced no artifact would otherwise
        # sit here forever — prepare and commit both return {} on an empty
        # envelope, route_after_validation's stall guard is the only thing
        # standing between that and a silently-never-advancing loop. Fail it
        # honestly instead. (read_file steps never reach here — coder.py marks
        # them `completed` directly, so the is_terminal check above already
        # returned.)
        return {
            "mission_spec": _mark_step_status(state["mission_spec"], step.step_number, "failed"),
            "current_step_id": step.step_number,
            "errors": [
                f"CoderAgent step #{step.step_number}: reached the apply gate with "
                "neither a file nor a command to apply — nothing was generated."
            ],
        }

    session_mode = session_mode_from_channel(state.get("session_permission_mode"))

    if command is not None:
        return await _prepare_command(state, step, command, session_id, session_mode)
    return await _prepare_files(state, step, paths, session_id, session_mode)


async def _prepare_command(
    state: Dict[str, Any], step: WBSStep, command: str, session_id: str,
    session_mode: SessionPermissionMode,
) -> Dict[str, Any]:
    """Compute the permission verdict for a staged run_command step — the
    EXACT pair SandboxBashTool._arun uses (core/permissions.py), so a command
    routed through the apply gate gets the identical guard SandboxBashTool
    gets, closing the gap where an EXECUTE-tier HITL verdict used to fall
    straight through coder.py to a real shell spawn with no card at all."""
    verdict = gate_execute_action(session_mode)
    verdict, risk_labels = risk_intercept_guard(command, verdict, session_mode)

    pending_apply: Dict[str, Any] = {
        "step_number": step.step_number,
        "kind": "RISK_INTERCEPT" if risk_labels else "COMMAND_EXECUTE",
        "decision": verdict.value,
        "command": command,
        "files": [],
        "risk_labels": risk_labels,
        "auto_accept": False,
        "attempt": int((state.get("apply_attempts") or {}).get(str(step.step_number), 0)),
    }
    if verdict is PermissionDecision.HITL:
        # Mirrors ExecutionChecklist's status glyph: the human hasn't decided
        # yet, and — unlike ALLOW/DENY — this status is real ahead of the
        # interrupt, since prepare already knows a card is coming.
        mission = _mark_step_status(state["mission_spec"], step.step_number, "awaiting_approval")
    else:
        mission = state["mission_spec"]
    return {"mission_spec": mission, "pending_apply": pending_apply, "current_step_id": step.step_number}


async def _prepare_files(
    state: Dict[str, Any], step: WBSStep, paths: List[str], session_id: str,
    session_mode: SessionPermissionMode,
) -> Dict[str, Any]:
    from core.task_service import compute_unified_diff  # deferred — avoids a brain->core.task_service cycle
    from core.vfs_middleware import make_safe_reader

    mission = state["mission_spec"]
    contents: Dict[str, str] = dict(state.get("pending_contents") or {})
    errors: List[str] = []

    # Refuse to mutate AILIENANT's own runtime artifacts (unchanged from the
    # pre-13.0.9 whole-turn check, now scoped to this step's own paths).
    internal = [p for p in paths if is_ailienant_internal_path(p)]
    paths = [p for p in paths if p not in internal]
    if internal:
        errors.append(
            "Skipped AILIENANT's own runtime files (they cannot be moved): "
            + ", ".join(f"`{p}`" for p in sorted(internal))
        )
    if not paths:
        return {
            "mission_spec": _mark_step_status(mission, step.step_number, "failed"),
            "current_step_id": step.step_number,
            "errors": errors or [
                f"CoderAgent step #{step.step_number}: no applicable file changes."
            ],
        }

    old_reader = make_safe_reader(
        state.get("project_id"), (state.get("workspace_root") or "") or None,
        session_id, vfs=None,
    )
    diffs: Dict[str, str] = {
        p: compute_unified_diff(old_reader(p) or "", contents.get(p, ""), p) for p in paths
    }
    risk_labels = sorted({
        label for diff in diffs.values() for label in scan_risk_patterns(_added_lines(diff))
    })

    verdict = evaluate_action(session_mode, ToolPrivilegeTier.WRITE, PermissionMode.EDIT_EXECUTE_RBW)
    auto_accept = bool(state.get("auto_accept_low_risk")) and not risk_labels and verdict is PermissionDecision.HITL

    # Cumulative blast radius: applied_files_log (already-landed steps this
    # turn) union this step's own files — a per-step-only scope would let a
    # 20-file mission across 20 steps never trip a 10-file threshold, the
    # exact silent-weakening the plan flagged as unacceptable.
    from core.blast_radius import DEFAULT_DEPTH, compute_blast_radius
    from shared.config import BLAST_RADIUS_THRESHOLD_FILES

    already_touched = [
        str(entry.get("file_path"))
        for entry in (state.get("applied_files_log") or [])
        if entry.get("status") == "completed" and entry.get("file_path")
    ]
    try:
        affected = await compute_blast_radius(
            state.get("project_id") or "",
            sorted(set(already_touched) | set(paths)),
            depth=DEFAULT_DEPTH,
            workspace_root=state.get("workspace_root") or "",
        )
    except Exception:  # noqa: BLE001 — a mapper fault must never crash the apply path
        logger.warning("Blast-radius mapper failed (advisory, skipped)", exc_info=True)
        affected = []
    blast_radius_exceeded = len(affected) > BLAST_RADIUS_THRESHOLD_FILES

    if blast_radius_exceeded:
        verdict = PermissionDecision.HITL
        auto_accept = False

    if verdict is PermissionDecision.ALLOW or auto_accept:
        # pre_patch veto is fail-closed and runs whether or not a human will
        # ever see a card — an Auto-mode write must still honor it.
        from core.task_service import run_patch_hooks  # deferred — see module docstring

        pre_ok, pre_msgs = await run_patch_hooks(session_id, "pre_patch")
        if not pre_ok:
            return {
                "mission_spec": _mark_step_status(mission, step.step_number, "failed"),
                "current_step_id": step.step_number,
                "errors": errors + [
                    "Changes not applied — a pre_patch hook vetoed the write."
                    + (" " + "; ".join(pre_msgs[:3]) if pre_msgs else "")
                ],
            }

    pending_apply: Dict[str, Any] = {
        "step_number": step.step_number,
        "kind": "FILE_WRITE",
        "decision": verdict.value,
        "command": None,
        "files": [
            {
                "file_path": p,
                "unified_diff": diffs[p],
                "base_hash": (state.get("pending_base_hash") or {}).get(p),
            }
            for p in paths
        ],
        "risk_labels": risk_labels,
        "auto_accept": auto_accept,
        "blast_radius_files": affected if blast_radius_exceeded else [],
        "attempt": int((state.get("apply_attempts") or {}).get(str(step.step_number), 0)),
    }
    if verdict is PermissionDecision.HITL and not auto_accept:
        mission = _mark_step_status(mission, step.step_number, "awaiting_approval")
    result: Dict[str, Any] = {
        "mission_spec": mission, "pending_apply": pending_apply, "current_step_id": step.step_number,
    }
    if errors:
        result["errors"] = errors
    return result


async def run_apply_commit_node(
    state: Dict[str, Any], config: Optional[RunnableConfig] = None
) -> Dict[str, Any]:
    """GATE — interrupt-first. Everything before the interrupt (or the
    ALLOW/auto-accept no-interrupt path) is pure and replay-stable; everything
    after it is the actual side effect (disk write or command execution),
    which never re-runs on a resume because ``applied_step_ids`` is checked
    FIRST, before anything else."""
    env = state.get("pending_apply")
    if not env:
        return {}

    step_number = int(env["step_number"])
    if step_number in (state.get("applied_step_ids") or []):
        # Idempotency (charter §5.3): a double-delivered resume (a retried
        # client_hitl_response, a reconnect racing the ack) must never re-run
        # the write or re-execute the command a second time.
        return {"pending_apply": None}

    session_id: str = state.get("task_id", "")
    decision = PermissionDecision(env["decision"])
    mission = state["mission_spec"]

    if decision is PermissionDecision.DENY:
        return _terminal_result(state, step_number, "failed", [
            f"CoderAgent step #{step_number}: DENIED — plan mode is read-only; not applied."
        ])

    verdict: Dict[str, Any] = {"approved": True, "comment": None, "modified_content": None}
    if decision is PermissionDecision.HITL and not env.get("auto_accept"):
        kind = env["kind"]
        if kind == "FILE_WRITE":
            action_desc = (
                f"Apply change to {env['files'][0]['file_path']}"
                if len(env["files"]) == 1
                else f"Apply {len(env['files'])} file change(s) for step #{step_number}"
            )
            proposed_files = [
                ProposedFile(
                    file_path=f["file_path"], unified_diff=f["unified_diff"], base_hash=f["base_hash"],
                )
                for f in env["files"]
            ]
            if env.get("blast_radius_files"):
                action_desc += (
                    f" — impacts {len(env['blast_radius_files'])} dependent file(s), "
                    "exceeding the blast-radius review threshold."
                )
            proposed_content = None
        else:
            action_desc = f"COMMAND_EXECUTE: {env['command']}"
            proposed_files = None
            proposed_content = env["command"]

        # THE INTERRUPT. First side-effectful action in this node — everything
        # above is pure/idempotent, so a resume replays it byte-identically
        # and only re-reaches this call, which then returns the resume value
        # instead of raising. See core/hitl.py::request_graph_approval.
        verdict = request_graph_approval(
            session_id=session_id,
            action_description=action_desc,
            proposed_content=proposed_content,
            request_kind=kind,
            proposed_files=proposed_files,
            risk_patterns_matched=env.get("risk_labels") or None,
        )

    if not verdict.get("approved"):
        return await _handle_declined(state, step_number, env, verdict)

    if env["kind"] == "COMMAND_EXECUTE" or env["kind"] == "RISK_INTERCEPT":
        return await _commit_command(state, step_number, env, session_id)
    return await _commit_files(state, step_number, env, session_id, verdict, mission, config)


async def _handle_declined(
    state: Dict[str, Any], step_number: int, env: Dict[str, Any], verdict: Dict[str, Any],
) -> Dict[str, Any]:
    comment = verdict.get("comment")
    attempt = int(env.get("attempt", 0))
    if comment and attempt < APPLY_REJECT_MAX_ATTEMPTS:
        # Request changes: bounded re-dispatch, with the human's feedback
        # threaded back to the coder for the next attempt (agents/coder.py
        # reads apply_feedback and asserts step_number == current_step_id).
        return {
            "mission_spec": _mark_step_status(state["mission_spec"], step_number, "revision_requested"),
            "apply_feedback": {"step_number": step_number, "comment": comment, "attempt": attempt + 1},
            "apply_attempts": {str(step_number): attempt + 1},
            "pending_apply": None,
            "current_step_id": step_number,
        }
    reason = (
        f"revision requested but the {APPLY_REJECT_MAX_ATTEMPTS}-attempt limit was reached"
        if comment else "declined"
    )
    return _terminal_result(state, step_number, "rejected", [
        f"CoderAgent step #{step_number}: {reason} — not applied."
    ])


async def _commit_command(
    state: Dict[str, Any], step_number: int, env: Dict[str, Any], session_id: str,
) -> Dict[str, Any]:
    from tools.execution_tools import render_guarded_command_result, run_guarded_command

    # session_permission_mode=None deliberately skips run_guarded_command's own
    # permission-gate+HITL block entirely — that decision was already made
    # (and, if HITL, already interrupted on) above. This still runs
    # _match_dangerous as the fail-closed floor and does the actual dispatch.
    result = await run_guarded_command(
        env["command"], session_id=session_id, session_permission_mode=None,
    )
    rendered = render_guarded_command_result(result)

    if result.exit_code == 0:
        return _terminal_result(
            state, step_number, "completed", [], applied_command=env["command"],
        )

    # Non-zero exit → the self-heal path, relocated from agents/coder.py's old
    # in-node run_command branch verbatim (diagnostics parsing, the
    # give-up-after-N-attempts concession, the reflexion-shaped healing delta)
    # — only the trigger moved (from a raw record_execution call to the
    # apply-gate's own guarded execution).
    from brain.failure_breaker import normalize_signature
    from brain.retry_policy import resolve_correction_ceiling
    from tools.validation.diagnostics import format_diagnostics, select_parser

    parser = select_parser(env["command"])
    diagnostics = format_diagnostics(parser(result.stdout, result.stderr))
    attempts = int(state.get("correction_attempts", 0))
    correction_ceiling = resolve_correction_ceiling(state.get("effort_level"))
    failed_mission = _mark_step_status(state["mission_spec"], step_number, "failed")
    log_entry = {
        "file_path": None, "command": env["command"], "status": "failed",
        "exit_code": result.exit_code, "step_number": step_number,
    }

    if attempts >= correction_ceiling:
        return {
            "mission_spec": failed_mission,
            "current_step_id": step_number,
            "applied_step_ids": [step_number],
            "applied_files_log": [log_entry],
            "pending_apply": None,
            "errors": [
                f"CoderAgent step #{step_number}: '{env['command']}' still failing "
                f"after {attempts} correction attempts:\n{diagnostics}\n\n{rendered}"
            ],
        }
    return {
        "mission_spec": failed_mission,
        "healing_required": True,
        "correction_attempts": attempts + 1,
        "last_error_trace": diagnostics,
        "failed_node": "apply_commit",
        "failure_signature": normalize_signature("apply_commit", "VerifyFailure", env["command"]),
        "last_execution_context": {
            "command": env["command"],
            "exit_code": result.exit_code,
            "stdout_tail": (result.stdout or "")[-_EXEC_TAIL_CHARS:],
            "stderr_tail": (result.stderr or "")[-_EXEC_TAIL_CHARS:],
        },
        "current_step_id": step_number,
        "applied_step_ids": [step_number],
        "applied_files_log": [log_entry],
        "pending_apply": None,
    }


def _validate_generated_files(contents: Dict[str, str]) -> Optional[str]:
    """Structurally validate pending file content. Returns a diagnostic, or ``None``.

    Runs on the in-memory overlay, BEFORE `apply_patch_set` touches the disk, so a
    file that does not parse is never written. Files whose type no grammar covers
    pass through — `validate_ast` reports that as unverified rather than clean.

    Never raises: a fault in the validator itself must not block an otherwise valid
    apply, or a broken parser would take the whole product down with it.
    """
    from tools.validation.ast_filter import validate_ast

    problems: list[str] = []
    for path, content in contents.items():
        if not content:
            continue
        try:
            result = validate_ast(content, path)
        except Exception:  # noqa: BLE001 — a validator fault must never block a valid apply
            logger.warning("apply_commit: AST validation errored for %s; skipping", path, exc_info=True)
            continue
        if not result.is_valid:
            detail = result.prune_reason or "structural error"
            first = result.errors[0] if result.errors else None
            where = f":{first.line}" if first is not None and first.line else ""
            problems.append(f"{path}{where}: {detail}")
    if not problems:
        return None
    return "The generated code does not parse:\n" + "\n".join(f"- {p}" for p in problems)


async def _lint_generated_files(contents: Dict[str, str]) -> Optional[str]:
    """Lint pending file content (ruff for Python, eslint for TS/TSX). Returns
    a diagnostic, or ``None``.

    Only reached for the ``balanced``/``deep`` Effort Budget levels — the
    syntax gate above is the unconditional correctness floor; this is the
    tier-scaled depth on top of it (charter §11: an MVP scoped to the two
    languages `tools/validation/lsp_filter.py` already covers, not every
    language `validate_ast` accepts — declared, not silent).

    Never raises: a linter/subprocess fault must degrade to passing, not block
    an otherwise-valid apply — the same graceful-degradation contract
    `validate_lsp` itself already documents for a missing linter or timeout.
    """
    from tools.validation.lsp_filter import validate_lsp

    problems: list[str] = []
    for path, content in contents.items():
        if not content:
            continue
        try:
            result = await validate_lsp(content, path)
        except Exception:  # noqa: BLE001 — a linter fault must never block a valid apply
            logger.warning("apply_commit: lint validation errored for %s; skipping", path, exc_info=True)
            continue
        if not result.is_valid:
            detail = result.prune_reason or "lint error"
            first = result.errors[0] if result.errors else None
            where = f":{first.line}" if first is not None and first.line else ""
            problems.append(f"{path}{where}: {detail}")
    if not problems:
        return None
    return "The generated code has lint errors:\n" + "\n".join(f"- {p}" for p in problems)


async def _commit_files(
    state: Dict[str, Any], step_number: int, env: Dict[str, Any], session_id: str,
    verdict: Dict[str, Any], mission: Any, config: Optional[RunnableConfig],
) -> Dict[str, Any]:
    from agents.coder import content_hash  # deferred — mirrors coder.py's own hash helper
    from core.task_service import _diff_line_delta, run_patch_hooks  # deferred — see module docstring
    from core.write_pipeline import apply_patch_set

    files = env["files"]
    diff_by_path = {f["file_path"]: f["unified_diff"] for f in files}
    single_modified = verdict.get("modified_content") if len(files) == 1 else None
    contents = {
        f["file_path"]: (single_modified if single_modified else state["pending_contents"].get(f["file_path"], ""))
        for f in files
    }
    base_hashes = {f["file_path"]: f["base_hash"] for f in files if f["base_hash"]}

    # Structural gate on the overlay, before anything reaches disk. Routes a
    # non-parsing patch into the SAME self-heal contract a failing command uses
    # below, so the coder gets the parser's own diagnostic and re-drafts, bounded
    # by the effort-resolved correction ceiling. Without this the main graph
    # never inspected generated code at all and a file containing a literal
    # conflict marker committed cleanly.
    # The GATE itself (whether the code parses) is unconditional — a
    # correctness floor, never an effort tier. What effort actually scales is
    # whether a failure gets a self-heal RETRY at all: Light fails a syntax
    # error outright (ceiling 0), Balanced/Deep get the full correction budget.
    syntax_error = _validate_generated_files(contents)
    if syntax_error is not None:
        from brain.failure_breaker import normalize_signature
        from brain.retry_policy import resolve_correction_ceiling

        attempts = int(state.get("correction_attempts", 0))
        correction_ceiling = resolve_correction_ceiling(state.get("effort_level"))
        logger.warning(
            "apply_commit: step #%d rejected by the syntax gate (attempt %d/%d): %s",
            step_number, attempts + 1, correction_ceiling, syntax_error,
        )
        if attempts >= correction_ceiling:
            return _terminal_result(state, step_number, "failed", [
                f"CoderAgent step #{step_number}: the generated code still does not "
                f"parse after {attempts} correction attempts — nothing was written.\n"
                f"{syntax_error}"
            ])
        return {
            "mission_spec": _mark_step_status(mission, step_number, "failed"),
            "healing_required": True,
            "correction_attempts": attempts + 1,
            "last_error_trace": syntax_error,
            "failed_node": "apply_commit",
            "failure_signature": normalize_signature(
                "apply_commit", "SyntaxError", ",".join(sorted(contents)),
            ),
            "current_step_id": step_number,
            "pending_apply": None,
        }

    # Effort Budget: lint/LSP depth on top of the always-on syntax floor.
    # Light skips this entirely (0 self-heal attempts makes the check moot
    # anyway); Balanced/Deep run it and share the same self-heal ceiling a
    # syntax failure uses above.
    from brain.retry_policy import normalize_effort_level

    if normalize_effort_level(state.get("effort_level")) in ("balanced", "deep"):
        lint_error = await _lint_generated_files(contents)
        if lint_error is not None:
            from brain.failure_breaker import normalize_signature
            from brain.retry_policy import resolve_correction_ceiling

            attempts = int(state.get("correction_attempts", 0))
            correction_ceiling = resolve_correction_ceiling(state.get("effort_level"))
            logger.warning(
                "apply_commit: step #%d rejected by the lint gate (attempt %d/%d): %s",
                step_number, attempts + 1, correction_ceiling, lint_error,
            )
            if attempts >= correction_ceiling:
                return _terminal_result(state, step_number, "failed", [
                    f"CoderAgent step #{step_number}: the generated code still has "
                    f"lint errors after {attempts} correction attempts.\n{lint_error}"
                ])
            return {
                "mission_spec": _mark_step_status(mission, step_number, "failed"),
                "healing_required": True,
                "correction_attempts": attempts + 1,
                "last_error_trace": lint_error,
                "failed_node": "apply_commit",
                "failure_signature": normalize_signature(
                    "apply_commit", "LintError", ",".join(sorted(contents)),
                ),
                "current_step_id": step_number,
                "pending_apply": None,
            }

    if env["decision"] == PermissionDecision.HITL.value and not env.get("auto_accept"):
        # A human-approved HITL write hasn't run pre_patch yet — prepare only
        # ran it for the ALLOW/auto-accept branch (which must veto BEFORE the
        # write, not after an approval that may never come).
        pre_ok, pre_msgs = await run_patch_hooks(session_id, "pre_patch")
        if not pre_ok:
            return _terminal_result(state, step_number, "failed", [
                "Changes not applied — a pre_patch hook vetoed the write."
                + (" " + "; ".join(pre_msgs[:3]) if pre_msgs else "")
            ])

    res = await apply_patch_set(session_id, contents, base_hashes)
    if not res.get("ok"):
        stale = res.get("stale_files")
        if stale:
            errors = [
                "Not applied — these files changed since the proposal: "
                + ", ".join(f"`{p}`" for p in stale)
                + ". Re-run the request to regenerate against the current code."
            ]
        else:
            errors = [f"Could not apply the changes: {res.get('error') or 'unknown error'}"]
        return _terminal_result(state, step_number, "failed", errors)

    applied = res.get("applied_files") or list(contents)
    log_entries = [
        {"file_path": p, "command": None, "status": "completed", "step_number": step_number}
        for p in applied
    ]

    # Timeline diff marker — one per file that actually landed, mirroring the
    # pre-13.0.9 turn-end emission exactly, just now per-step instead of
    # per-turn. Same off-state DI seam as `narrate`/`stream_thinking`.
    push_activity = (config or {}).get("configurable", {}).get("push_activity")
    if push_activity is not None:
        for p in applied:
            try:
                await push_activity("diff", target=p, metric=_diff_line_delta(diff_by_path.get(p, "")), ref=p)
            except Exception:  # noqa: BLE001 — narration must never block the apply
                logger.debug("apply_commit: diff activity emit skipped for %s", p, exc_info=True)

    _, post_msgs = await run_patch_hooks(session_id, "post_patch")

    result: Dict[str, Any] = {
        "mission_spec": _mark_step_status(mission, step_number, "completed"),
        "current_step_id": step_number,
        "applied_step_ids": [step_number],
        "applied_files_log": log_entries,
        "pending_apply": None,
        # Re-anchor the base hash to the content just written — removes a
        # read-freshness race if a LATER step also touches one of these
        # files: its own prepare pass reads pending_base_hash fresh rather
        # than depending on the VFS reader having observed this write yet.
        "pending_base_hash": {p: content_hash(contents[p]) for p in applied},
    }
    if post_msgs:
        result["errors"] = [f"post_patch hook notes: {'; '.join(post_msgs[:3])}"]
    return result


def _terminal_result(
    state: Dict[str, Any], step_number: int, status: str, errors: List[str],
    applied_command: Optional[str] = None,
) -> Dict[str, Any]:
    log_entry = {
        "file_path": None, "command": applied_command, "status": status, "step_number": step_number,
    }
    result: Dict[str, Any] = {
        "mission_spec": _mark_step_status(state["mission_spec"], step_number, status),
        "current_step_id": step_number,
        "applied_step_ids": [step_number],
        "applied_files_log": [log_entry],
        "pending_apply": None,
    }
    if errors:
        result["errors"] = errors
    return result
