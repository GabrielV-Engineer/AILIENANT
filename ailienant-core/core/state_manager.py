# core/state_manager.py
"""Phase 3.6 — Cognitive State Fast-Boot.

Serialises active WBS + context metrics to <workspace>/.ailienant/AGENTS.md so the
PlannerAgent can skip the expensive LanceDB embedding call on cold start.

Public API:
    dump_state_to_markdown(state, workspace_root) -> bool
    load_state_from_markdown(workspace_root, max_age_seconds) -> Optional[CachedAgentState]
    record_merge_event(workspace_root, merged_paths) -> bool
"""
from __future__ import annotations

import logging
import os
import re
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

from brain.state import ContextMeter, MissionSpecification

logger = logging.getLogger("STATE_MANAGER")

_AGENTS_MD_FILENAME: str = "AGENTS.md"
_AILIENANT_DIR: str = ".ailienant"
_PLANS_SUBDIR: str = "plans"
_DEFAULT_MAX_AGE_SECONDS: int = 3600  # 1 hour

# Confines a task id to a safe filename stem (defense against path traversal in a
# value that ultimately reaches the filesystem).
_SAFE_STEM_RE: re.Pattern[str] = re.compile(r"[^A-Za-z0-9._-]")

# Sentinel delimiters for machine-parseable JSON embedded in Markdown.
_JSON_START: str = "<!-- MACHINE_DATA_JSON\n"
_JSON_END: str = "\n-->"


# ── Data model ────────────────────────────────────────────────────────────────

class CachedAgentState(BaseModel):
    """Serialised planner state written to .ailienant/AGENTS.md for fast-boot."""

    mission_spec: Optional[MissionSpecification] = None
    context_metrics: Optional[ContextMeter] = None
    top_k_files: List[str] = Field(default_factory=list)
    task_id: str = ""
    generated_at: str = ""
    last_merge_at: Optional[str] = None
    last_merged_paths: List[str] = Field(default_factory=list)


# ── Internal helpers ───────────────────────────────────────────────────────────

def _agents_md_path(workspace_root: str) -> Path:
    return Path(workspace_root) / _AILIENANT_DIR / _AGENTS_MD_FILENAME


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _build_agents_md(cached: CachedAgentState) -> str:
    """Render CachedAgentState as human-readable Markdown with embedded machine JSON."""
    lines: List[str] = [
        "<!-- AILIENANT Fast-Boot Checkpoint -->",
        f"<!-- task_id: {cached.task_id} -->",
        f"<!-- generated_at: {cached.generated_at} -->",
        "",
        "## Active Mission",
        "",
    ]

    spec = cached.mission_spec
    if spec:
        lines += [
            f"**Outcome:** {spec.outcome}",
            "",
            "**Scope:**",
        ]
        for s in spec.scope:
            lines.append(f"- {s}")
        lines += ["", "**WBS Tasks:**", ""]
        lines += [
            "| Step | Role | Action | Target | Status |",
            "|------|------|--------|--------|--------|",
        ]
        for task in spec.tasks:
            lines.append(
                f"| {task.step_number} | {task.target_role} | {task.action}"
                f" | {task.target_file} | {task.status} |"
            )
    else:
        lines.append("*(no active mission)*")

    ctx = cached.context_metrics
    if ctx:
        lines += [
            "",
            "## Context Snapshot",
            "",
            f"- **TCI:** {ctx.task_complexity_index:.1f}",
            f"- **CSS:** {ctx.css_total:.1f}",
            f"- **Routing:** {ctx.routing_decision}",
            f"- **Red Alert:** {ctx.is_red_alert}",
            f"- **Semantic Similarity:** {ctx.semantic_similarity:.4f}",
        ]

    if cached.last_merge_at:
        lines += [
            "",
            "## Last MCTS Merge",
            "",
            f"- **At:** {cached.last_merge_at}",
            f"- **Files:** {', '.join(cached.last_merged_paths) or '(none)'}",
        ]

    # Embed machine-parseable JSON — sanitise --> to avoid breaking the HTML comment.
    raw_json: str = cached.model_dump_json()
    safe_json: str = raw_json.replace("-->", "-- >")
    lines += ["", _JSON_START + safe_json + _JSON_END]

    return "\n".join(lines)


def _write_agents_md(target: Path, content: str) -> None:
    """Atomic write using tempfile + os.replace (same pattern as core/rules.py)."""
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp_path: str = ""
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            delete=False,
            dir=str(target.parent),
            prefix=".__ailienant_tmp_",
            suffix=".md",
        ) as tmp:
            tmp.write(content)
            tmp_path = tmp.name
        os.replace(tmp_path, str(target))
    except Exception:
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass
        raise


def _parse_cached_state(text: str) -> Optional[CachedAgentState]:
    """Extract and parse the embedded machine JSON from AGENTS.md content."""
    start_idx: int = text.find(_JSON_START)
    if start_idx == -1:
        return None
    json_begin: int = start_idx + len(_JSON_START)
    end_idx: int = text.find(_JSON_END, json_begin)
    if end_idx == -1:
        return None
    raw_json: str = text[json_begin:end_idx]
    # Reverse the --> sanitisation applied during write.
    raw_json = raw_json.replace("-- >", "-->")
    return CachedAgentState.model_validate_json(raw_json)


# ── Public API ─────────────────────────────────────────────────────────────────

def dump_state_to_markdown(state: Dict[str, Any], workspace_root: str) -> bool:
    """Flush active planner state to .ailienant/AGENTS.md atomically.

    Accepts a raw LangGraph state dict (or the merged dict(state)|result from the
    planner node). Non-fatal: logs a warning and returns False on any failure.
    """
    if not workspace_root:
        return False
    try:
        spec: Optional[MissionSpecification] = state.get("mission_spec")
        ctx: Optional[ContextMeter] = state.get("context_metrics")
        top_k: List[str] = state.get("_top_k_files_cache", [])

        cached = CachedAgentState(
            mission_spec=spec,
            context_metrics=ctx,
            top_k_files=top_k,
            task_id=str(state.get("task_id") or ""),
            generated_at=_iso_now(),
        )
        target: Path = _agents_md_path(workspace_root)
        _write_agents_md(target, _build_agents_md(cached))
        logger.info("Phase 3.6: state dumped to %s", target)
        return True
    except Exception as exc:
        logger.warning("Phase 3.6: dump_state_to_markdown failed (non-fatal): %s", exc)
        return False


def _plan_md_path(workspace_root: str, task_id: str) -> Path:
    stem = _SAFE_STEM_RE.sub("_", task_id.strip()) or "plan"
    return Path(workspace_root) / _AILIENANT_DIR / _PLANS_SUBDIR / f"{stem}.md"


def _build_plan_md(spec: MissionSpecification, task_id: str) -> str:
    """Render a mission as a navigable, human-readable Markdown plan.

    Distinct from AGENTS.md (the machine fast-boot cache): this view carries no
    embedded JSON and includes each step's full description so a reader can follow
    the agent's intended work in the editor preview.
    """
    lines: List[str] = [
        f"# Plan — {task_id or 'current task'}",
        "",
        f"_Generated {_iso_now()}_",
        "",
        f"**Outcome:** {spec.outcome}",
        "",
        "## Scope",
        "",
    ]
    for item in spec.scope:
        lines.append(f"- {item}")
    if not spec.scope:
        lines.append("- *(no scope recorded)*")

    lines += ["", "## Work Breakdown", ""]
    if spec.tasks:
        for task in spec.tasks:
            iteration = " · iterative" if getattr(task, "requires_iteration", False) else ""
            lines += [
                f"### Step {task.step_number} — {task.target_role} · {task.action}{iteration}",
                "",
                f"**Target:** `{task.target_file}`",
                "",
                task.description or "*(no description)*",
                "",
            ]
    else:
        lines.append("*(no tasks)*")

    return "\n".join(lines).rstrip() + "\n"


def dump_plan_to_markdown(
    spec: Optional[MissionSpecification],
    workspace_root: str,
    task_id: str,
) -> bool:
    """Write a navigable plan to .ailienant/plans/<task_id>.md atomically.

    Non-fatal: logs a warning and returns False on any failure (a plan-export
    error must never interrupt planning). No-op when the mission or workspace is
    absent.
    """
    if not workspace_root or spec is None:
        return False
    try:
        target: Path = _plan_md_path(workspace_root, task_id)
        _write_agents_md(target, _build_plan_md(spec, task_id))
        logger.info("Plan exported to %s", target)
        return True
    except Exception as exc:  # noqa: BLE001 — plan export must never crash planning
        logger.warning("dump_plan_to_markdown failed (non-fatal): %s", exc)
        return False


def load_state_from_markdown(
    workspace_root: str,
    max_age_seconds: int = _DEFAULT_MAX_AGE_SECONDS,
) -> Optional[CachedAgentState]:
    """Load cached agent state from .ailienant/AGENTS.md if it exists and is fresh.

    Returns None if the file is missing, stale, or cannot be parsed.
    """
    if not workspace_root:
        return None
    target: Path = _agents_md_path(workspace_root)
    if not target.exists():
        return None
    try:
        mtime: float = os.path.getmtime(str(target))
        if time.time() - mtime > max_age_seconds:
            logger.debug("Phase 3.6: AGENTS.md is stale — skipping fast-boot.")
            return None
        text: str = target.read_text(encoding="utf-8")
        cached: Optional[CachedAgentState] = _parse_cached_state(text)
        if cached is None:
            logger.debug("Phase 3.6: AGENTS.md parse failed — skipping fast-boot.")
        return cached
    except Exception as exc:
        logger.debug("Phase 3.6: load_state_from_markdown failed (non-fatal): %s", exc)
        return None


def record_merge_event(workspace_root: str, merged_paths: List[str]) -> bool:
    """Update AGENTS.md with MCTS merge metadata after a successful apply_merge().

    No-op if AGENTS.md doesn't exist yet (planner hasn't run). Non-fatal.
    """
    if not workspace_root:
        return False
    target: Path = _agents_md_path(workspace_root)
    if not target.exists():
        return False
    try:
        text: str = target.read_text(encoding="utf-8")
        cached: Optional[CachedAgentState] = _parse_cached_state(text)
        if cached is None:
            return False
        updated = cached.model_copy(update={
            "last_merge_at": _iso_now(),
            "last_merged_paths": list(merged_paths),
        })
        _write_agents_md(target, _build_agents_md(updated))
        logger.info("Phase 3.6: merge event recorded in AGENTS.md (%d paths).", len(merged_paths))
        return True
    except Exception as exc:
        logger.warning("Phase 3.6: record_merge_event failed (non-fatal): %s", exc)
        return False
