# ailienant-core/core/project_init.py
"""On-demand `AILIENANT.md` drafting ("/init").

A one-shot pass, fired only by an explicit user action (VS Code command / HUD
button), that reads a bounded workspace overview and asks the LLM to draft the
project's `AILIENANT.md` — stack, conventions, and always/never notes — so a
freshly-provisioned but still-empty template stops being empty.

Deliberately mirrors ``brain/daemon.py``'s ("Dreaming") shape — budget gate,
read-only overview, one LLM call outside any lock, an optimistic-concurrency
guard before commit — since that is the only existing precedent in this
codebase for "user-triggered, one-shot, reads the workspace, writes an
artifact." It is NOT built on top of ``OvernightDaemon`` itself: that class is
coupled to ``SemanticMemoryManager.semantic_upsert``, a different write target
with different semantics (an internal memory note vs. a user-facing,
git-tracked file). Sharing the *shape*, not the class, keeps each writer
single-purpose.

Never generates ``.ailienant/.ailienant.json``: that file already has three
writers (the user, ``core/rules.py``'s implicit-rejection distillation, and
``core/config/profile.py``), and a fourth writer that *infers* behavioral
rules would be materially riskier than drafting prose — a bad rule silently
changes agent behavior, a bad paragraph is just text the user edits.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, Optional

from agents.workspace_context import build_workspace_overview
from core.state_manager import write_markdown_atomic
from shared.config import MODEL_MEDIUM, session_budget_usd

logger = logging.getLogger("PROJECT_INIT")

# Bounds the draft the same way Dreaming bounds its consolidation note — a
# short, dense document, not an essay.
_MAX_DRAFT_TOKENS: int = 1500

# Wider than agents/workspace_context.py's own 2048-char default: that default
# is tuned for a per-turn prompt injection, but a one-shot onboarding pass can
# afford to look at more of the workspace to ground its conventions.
_OVERVIEW_BUDGET: int = 8000
_OVERVIEW_MAX_FILES: int = 200

_SYSTEM_PROMPT: str = (
    "You are AILIENANT's project-onboarding pass. You are read-only: you NEVER "
    "edit files yourself, you only draft text for a human to review. Given a "
    "workspace overview (a folder tree and root manifest contents), infer the "
    "tech stack and frameworks in use, test/build commands (only if visible in "
    "a manifest's scripts/config), and any coding conventions evident in the "
    "material. Output ONLY the body of a Markdown file using exactly these "
    "three section headers, each with short bullet points grounded in what you "
    "were shown — never invent a command or convention you have no evidence "
    "for; a short or empty section is correct when the material does not "
    "support more:\n\n"
    "## Stack & Conventions\n\n## Always\n\n## Never\n"
)

# Candidate read locations, same priority order as core/project_instructions.py's
# own resolver, so /init detects and targets whichever file the rest of the
# system already treats as authoritative.
_CANDIDATES: tuple[str, ...] = (".ailienant/AILIENANT.md", "AILIENANT.md")
_GENERATED_SUFFIX: str = ".generated.md"

# Detects the pristine ailienant-extension/src/workspace_provisioning.ts
# template (and any equally-empty hand-cleared file) without importing or
# duplicating its TypeScript source — this only needs to recognize "nothing
# substantive here yet," not reproduce the template's exact prose.
_HTML_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)
_HEADING_RE = re.compile(r"^#{1,6}\s*.*$", re.MULTILINE)
_EMPTY_BULLET_RE = re.compile(r"^-\s*$", re.MULTILINE)


@dataclass(frozen=True)
class ProjectInitResult:
    """Outcome of one /init pass — drives the WS completion event and tests."""

    status: str  # "written" | "refused_budget" | "aborted_stale" | "skipped_empty"
    path: str
    chars: int


def _is_effectively_empty(text: str) -> bool:
    """True when `text` carries no user-authored substance.

    Strips HTML comments, Markdown headings, and empty bullet markers (`- `);
    what remains must be whitespace-only for the file to count as untouched.
    Errs conservative — a partially-filled template counts as non-empty, so a
    real edit is never mistaken for the pristine template.
    """
    stripped = _HTML_COMMENT_RE.sub("", text)
    stripped = _HEADING_RE.sub("", stripped)
    stripped = _EMPTY_BULLET_RE.sub("", stripped)
    return not stripped.strip()


def _resolve_target(workspace_root: str) -> Path:
    """Return the path /init should write to.

    Reuses whichever candidate `core/project_instructions.py` would have read
    (`.ailienant/AILIENANT.md` preferred, flat `AILIENANT.md` fallback). A
    candidate that already exists and is effectively empty is written in
    place. A candidate with real user content is never touched — the sibling
    ``<name>.generated.md`` is targeted instead. Absent either candidate,
    targets the `.ailienant/` location (the extension's own default).
    """
    root = Path(workspace_root)
    for rel in _CANDIDATES:
        candidate = root / rel
        try:
            if not candidate.is_file():
                continue
            text = candidate.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if _is_effectively_empty(text):
            return candidate
        return candidate.with_name(candidate.stem + _GENERATED_SUFFIX)
    return root / ".ailienant" / "AILIENANT.md"


async def _default_invoke(
    messages: list[Dict[str, Any]], *, session_id: str
) -> Any:
    from tools.llm_gateway import LLMGateway  # deferred — keep module import light

    return await LLMGateway.ainvoke(
        messages, model=MODEL_MEDIUM, max_tokens=_MAX_DRAFT_TOKENS, session_id=session_id
    )


async def run_project_init(
    project_id: str,
    *,
    workspace_root: str,
    session_id: str,
    overview_fn: Callable[..., str] = build_workspace_overview,
    budget_fn: Optional[Callable[[], Dict[str, float]]] = None,
    llm_invoke: Optional[Callable[..., Awaitable[Any]]] = None,
    stale_check: Optional[Callable[[], bool]] = None,
) -> ProjectInitResult:
    """Draft `AILIENANT.md` from a bounded workspace overview. Safe to cancel
    at any await point. Every *business* outcome — over budget, an empty
    overview, no usable LLM content, or a stale snapshot — degrades to a typed
    :class:`ProjectInitResult` rather than raising; an actual LLM/transport
    exception still propagates to the caller uncaught, exactly like
    ``OvernightDaemon.run_consolidation`` (the caller, ``main.py``'s trigger
    wrapper, is the layer that logs and swallows it).

    ``budget_fn``/``llm_invoke``/``overview_fn``/``stale_check`` are injectable
    test seams; production omits them and the real implementations run
    unchanged (same pattern as ``brain/daemon.py``).
    """
    if budget_fn is not None:
        snap = budget_fn()
    else:
        from core.token_ledger import token_ledger

        snap = token_ledger.snapshot()
    budget = session_budget_usd()
    if snap.get("estimated_invested_usd", 0.0) > budget:
        logger.warning(
            "ProjectInit refused: session spend $%.4f over ceiling $%.2f (project=%s).",
            snap.get("estimated_invested_usd", 0.0), budget, project_id,
        )
        return ProjectInitResult("refused_budget", "", 0)

    overview = overview_fn(
        workspace_root, budget=_OVERVIEW_BUDGET, max_files=_OVERVIEW_MAX_FILES
    )
    if not overview:
        logger.info("ProjectInit skipped: empty workspace overview (project=%s).", project_id)
        return ProjectInitResult("skipped_empty", "", 0)

    messages: list[Dict[str, Any]] = [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {"role": "user", "content": f"Workspace overview:\n{overview}"},
    ]

    invoke = llm_invoke or _default_invoke
    resp = await invoke(messages, session_id=session_id)
    try:
        content = resp.choices[0].message.content or ""
    except (AttributeError, IndexError, TypeError):
        content = ""
    content = content.strip()
    if not content:
        logger.info("ProjectInit produced no content (project=%s).", project_id)
        return ProjectInitResult("skipped_empty", "", 0)

    # OCC commit guard — mirrors Dreaming: a concurrent change invalidates the
    # snapshot this draft was grounded in, so it must not land silently.
    if stale_check is not None and stale_check():
        logger.info("ProjectInit aborted: snapshot invalidated mid-run (project=%s).", project_id)
        return ProjectInitResult("aborted_stale", "", 0)

    target = _resolve_target(workspace_root)
    write_markdown_atomic(target, content if content.endswith("\n") else content + "\n")
    logger.info(
        "ProjectInit wrote %d chars to %s (project=%s).", len(content), target, project_id
    )
    return ProjectInitResult("written", str(target), len(content))
