"""ErrorCorrectionAgent — the self-healing reflexion unit.

A cold, surgical engineering tool. When a coding step raises (a tool failure, a
schema hallucination, an API crash), this agent reads the traceback, reads the
offending source file, and proposes the smallest corrective patch. The patch is
NEVER written to disk here — it is emitted into the standard ``pending_patches`` /
``pending_contents`` / ``pending_base_hash`` channels so it flows through the existing
``request_human_approval`` (HITL) + write-pipeline path like any other edit.

Two call shapes share one implementation:
  * ``run_error_correction_node(state)`` — a LangGraph node reading the diagnostic
    fields the reflexion guard wrote into state.
  * ``attempt_correction(...)`` — a direct helper for the manual task-service coder
    loop, which orchestrates steps outside the compiled graph.

Cognitive isolation (binding): this module MUST NOT import ``brain.personality``. It
is a logic agent behind the Phase 4.1.5 fence — no persona, no empathy, no apologies
that would waste tokens and add latency inside the loop. Enforced by the ISO1 audit.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import traceback as _traceback
from typing import Any, Awaitable, Callable, Dict, List, Optional

from pydantic import BaseModel

from brain.failure_breaker import failure_breaker, normalize_signature
from brain.retry_policy import CORRECTION_MAX_ATTEMPTS

logger = logging.getLogger("ERROR_CORRECTION")

# Injectable LLM call (system, user_payload) -> raw JSON string. Mirrors the
# ContractGuardNode seam so unit tests can substitute a deterministic stub.
LLMInvoker = Callable[[str, Dict[str, Any]], Awaitable[str]]

_TRACE_CAP: int = 4000          # cap traceback text fed back into the loop / state
_FILE_SLICE_CAP: int = 16000    # cap offending-file content sent to the model (OOM guard)
# Extract source paths from CPython traceback frames: `  File "x.py", line N, ...`.
_TB_FILE_RE = re.compile(r'File "([^"]+)", line \d+')


class CorrectionProposal(BaseModel):
    """The model's structured verdict. An empty ``filepath`` means 'no safe fix'."""

    diagnosis: str = ""
    filepath: str = ""
    new_content: str = ""


class CorrectionResult(BaseModel):
    """Outcome of one correction attempt, ready to fold into graph state."""

    healed: bool
    diagnosis: str = ""
    # Channel deltas — empty when no fix was produced.
    pending_patches: Dict[str, str] = {}
    pending_contents: Dict[str, str] = {}
    pending_base_hash: Dict[str, str] = {}


def _content_hash(text: str) -> str:
    """SHA-256 over newline-normalized text — matches agents.coder.content_hash so
    the write-pipeline stale-file guard agrees on the pre-edit anchor."""
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def candidate_files_from_traceback(
    traceback_text: str, workspace_root: Optional[str]
) -> List[str]:
    """Pull in-workspace source paths out of a traceback, newest frame first.

    Frames outside ``workspace_root`` (stdlib, site-packages) are dropped — the agent
    may only ever propose edits to the user's own code. Order is reversed so the
    deepest (most proximate) frame is tried first.
    """
    paths: List[str] = []
    seen: set[str] = set()
    for match in _TB_FILE_RE.finditer(traceback_text):
        raw = os.path.normpath(match.group(1))
        if raw in seen:
            continue
        seen.add(raw)
        if workspace_root:
            try:
                rel = os.path.relpath(raw, workspace_root)
            except ValueError:
                continue  # different drive on Windows — not our code
            if rel.startswith("..") or os.path.isabs(rel):
                continue  # outside the workspace
        paths.append(raw)
    paths.reverse()
    return paths


class ErrorCorrectionAgent:
    """Async reflexion agent. Reads a traceback + offending file, proposes a fix."""

    def __init__(self, llm_invoker: Optional[LLMInvoker] = None) -> None:
        self._llm_invoker: Optional[LLMInvoker] = llm_invoker

    async def _default_llm_invoker(
        self, system: str, user_payload: Dict[str, Any]
    ) -> str:
        """Default call via the project LLMGateway. Deferred import avoids a circular
        dependency at module load (same idiom as contract_guard/drift_monitor)."""
        from tools.llm_gateway import LLMGateway
        from shared.config import MODEL_MEDIUM

        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": json.dumps(user_payload, default=str)},
        ]
        response = await LLMGateway.ainvoke(
            messages=messages,
            model=MODEL_MEDIUM,
            temperature=0.0,
            response_format={"type": "json_object"},
        )
        return str(response.choices[0].message.content or "")

    @staticmethod
    def _read_offending_file(
        path: str, state: Dict[str, Any]
    ) -> Optional[str]:
        """Firewalled read of a candidate file via the shared VFS middleware. Returns
        None when the file is excluded/ignored/binary/too-large/unreadable."""
        from core.vfs_middleware import make_safe_reader

        read = make_safe_reader(
            state.get("project_id"),
            state.get("workspace_root"),
            state.get("task_id"),
        )
        return read(path)

    async def propose_fix(
        self,
        *,
        traceback_text: str,
        candidate_files: List[str],
        state: Dict[str, Any],
    ) -> CorrectionResult:
        """Read the first usable candidate file and ask the model for a minimal fix.

        Defensive by construction: a read miss, an LLM error, an unparseable response,
        or an empty/foreign ``filepath`` all resolve to ``healed=False`` rather than
        raising — a saturated event loop must never let an LLM failure escape here.
        """
        offending_path: Optional[str] = None
        original: Optional[str] = None
        for path in candidate_files:
            content = self._read_offending_file(path, state)
            if content is not None:
                offending_path, original = path, content
                break

        if offending_path is None or original is None:
            logger.info("Correction: no readable candidate file; conceding.")
            return CorrectionResult(healed=False, diagnosis="no readable offending file")

        from agents.prompts import ERROR_CORRECTION_SYSTEM_PROMPT

        invoker: LLMInvoker = self._llm_invoker or self._default_llm_invoker
        try:
            raw = await invoker(
                ERROR_CORRECTION_SYSTEM_PROMPT,
                {
                    "traceback": traceback_text[:_TRACE_CAP],
                    "candidate_paths": [offending_path],
                    "offending_file": {
                        "path": offending_path,
                        "content": original[:_FILE_SLICE_CAP],
                    },
                },
            )
            proposal = CorrectionProposal.model_validate(json.loads(raw))
        except Exception as exc:  # noqa: BLE001 — network/parse/validation → concede
            logger.warning("Correction LLM/parse failed; conceding: %s", exc)
            return CorrectionResult(healed=False, diagnosis=f"correction unavailable: {exc}")

        # A safe agent declines rather than guessing — empty filepath/content = no fix.
        if not proposal.filepath or not proposal.new_content:
            return CorrectionResult(healed=False, diagnosis=proposal.diagnosis)
        if os.path.normpath(proposal.filepath) != offending_path:
            logger.warning(
                "Correction proposed a foreign path (%s != %s); rejecting.",
                proposal.filepath, offending_path,
            )
            return CorrectionResult(healed=False, diagnosis=proposal.diagnosis)
        if proposal.new_content == original:
            return CorrectionResult(healed=False, diagnosis=proposal.diagnosis)

        return CorrectionResult(
            healed=True,
            diagnosis=proposal.diagnosis,
            pending_patches={offending_path: proposal.new_content},
            pending_contents={offending_path: proposal.new_content},
            pending_base_hash={offending_path: _content_hash(original)},
        )


_default_agent = ErrorCorrectionAgent()


async def attempt_correction(
    exc: BaseException,
    state: Dict[str, Any],
    *,
    failed_node: str,
    extra_candidates: Optional[List[str]] = None,
    agent: Optional[ErrorCorrectionAgent] = None,
) -> Optional[CorrectionResult]:
    """Bounded, breaker-gated correction for direct (non-graph) call sites.

    Returns a healed ``CorrectionResult`` when a fix was produced, or ``None`` when
    the budget/breaker forbids another attempt or no fix was found — the caller then
    falls back to its own dead-letter / error path. Never raises.
    """
    tb_text = "".join(
        _traceback.format_exception(type(exc), exc, exc.__traceback__)
    )
    signature = normalize_signature(failed_node, type(exc).__name__, str(exc))

    attempts = int(state.get("correction_attempts", 0))
    if attempts >= CORRECTION_MAX_ATTEMPTS or not failure_breaker.allow(signature):
        logger.info(
            "Correction skipped [node=%s]: attempts=%d breaker_open=%s",
            failed_node, attempts, not failure_breaker.allow(signature),
        )
        return None

    candidates = candidate_files_from_traceback(tb_text, state.get("workspace_root"))
    for path in extra_candidates or []:
        norm = os.path.normpath(path)
        if norm not in candidates:
            candidates.append(norm)

    result = await (agent or _default_agent).propose_fix(
        traceback_text=tb_text, candidate_files=candidates, state=state
    )
    if result.healed:
        failure_breaker.record_success(signature)
        return result
    failure_breaker.record_failure(signature)
    return None


async def run_error_correction_node(state: Dict[str, Any]) -> Dict[str, Any]:
    """LangGraph node: read the diagnostic fields the reflexion guard stored, attempt
    one bounded correction, and emit channel deltas. Always clears ``healing_required``
    so the graph routes forward whether or not a fix was found."""
    if not state.get("healing_required"):
        return {}

    tb_text: str = str(state.get("last_error_trace") or "")
    failed_node: str = str(state.get("failed_node") or "unknown")
    signature: str = str(state.get("failure_signature") or "")

    candidates = candidate_files_from_traceback(tb_text, state.get("workspace_root"))
    mission = state.get("mission_spec")
    step_id = state.get("current_step_id")
    if mission is not None and step_id is not None:
        for task in getattr(mission, "tasks", []):
            if getattr(task, "step_number", None) == step_id:
                target = getattr(task, "target_file", None)
                if target and os.path.normpath(target) not in candidates:
                    candidates.append(os.path.normpath(target))

    result = await _default_agent.propose_fix(
        traceback_text=tb_text, candidate_files=candidates, state=state
    )

    if result.healed:
        if signature:
            failure_breaker.record_success(signature)
        logger.info("Correction healed [node=%s]: %s", failed_node, result.diagnosis)
        return {
            "healing_required": False,
            "pending_patches": result.pending_patches,
            "pending_contents": result.pending_contents,
            "pending_base_hash": result.pending_base_hash,
            "validation_feedback": f"Auto-correction applied: {result.diagnosis}",
        }

    if signature:
        failure_breaker.record_failure(signature)
    logger.info("Correction conceded [node=%s]: %s", failed_node, result.diagnosis)
    return {
        "healing_required": False,
        "errors": [f"self-heal could not correct {failed_node}: {result.diagnosis}"],
    }
