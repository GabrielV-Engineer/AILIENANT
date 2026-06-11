# ailienant-core/core/skill_resolver.py
"""Resolve which user-authored skills apply to a task, and render them for the prompt.

A skill is a reusable instruction the user saves once and the agent applies on its
own. Two modes coexist:

  * Auto-invocation — every enabled skill carries a one-line ``description``; the
    backend matches it semantically against the task request and injects the ones
    that are relevant. An irrelevant pool stays dormant and costs nothing.
  * Explicit invocation — the user names a specific skill id for this task; it is
    injected regardless of semantic match or scope. It still honors the ``enabled``
    flag: a disabled skill is the owner's "do not run" signal under every mode.

Scope mirrors the ecosystem convention: a workspace-scoped skill shadows a global
skill of the same name for that workspace (workspace > global).

This module is pure: it touches the catalog DB and the embedding callable, never the
transport layer.
"""
from __future__ import annotations

import logging
import math
import os
from typing import Any, Awaitable, Callable, Dict, List, Optional

import core.db as catalog_db
from agents.analyst_context import _sandbox_escape

logger = logging.getLogger(__name__)

EmbedFn = Callable[[str], Awaitable[List[float]]]

# Minimum cosine similarity for an auto-invoked skill to be considered relevant.
_MATCH_THRESHOLD = float(os.environ.get("SKILL_MATCH_THRESHOLD", "0.45"))
# Absolute char ceiling on the rendered skills block. This is the skills layer's own
# budget boundary: the graph's context governor accounts for the graph state, not for
# directives injected at prompt-assembly time, so the cap here is independent of the
# rest of the prompt and is enforced locally.
_SKILL_BLOCK_CHAR_CAP = int(os.environ.get("SKILL_BLOCK_CHAR_CAP", "3000"))


def _cosine(a: List[float], b: List[float]) -> float:
    """Cosine similarity with explicit L2 normalization.

    Cosine is only meaningful over unit vectors. Some embedding models return
    non-unit magnitudes; a raw dot product would make the threshold scale-dependent
    and opaque, so each vector is normalized before the dot product.
    """
    if not a or not b or len(a) != len(b):
        return 0.0
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0.0 or nb == 0.0:
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    return dot / (na * nb)


def _projection(row: Dict[str, Any]) -> Dict[str, str]:
    """Reduce a DB row to the fields the prompt layer needs."""
    return {
        "id": str(row.get("id", "")),
        "name": str(row.get("name", "")),
        "body": str(row.get("body", "")),
    }


async def resolve_active_skills(
    *,
    user_input: str,
    workspace_root: str,
    invoked_skill_id: Optional[str],
    embed_fn: Optional[EmbedFn] = None,
) -> List[Dict[str, str]]:
    """Return the skills to inject for this task. Never raises; never returns None.

    The result is ordered with an explicitly invoked skill first, then the
    semantically matched ones by descending relevance.
    """
    resolved: List[Dict[str, str]] = []
    explicit_id: Optional[str] = None

    # Mode 2 — explicit invocation. Bypasses the semantic match and the scope filter
    # (the user chose this skill deliberately) but still honors the enabled flag.
    if invoked_skill_id:
        try:
            row = await catalog_db.get_skill(invoked_skill_id)
        except Exception as exc:  # noqa: BLE001 — a lookup failure must not block the task
            logger.warning("skill lookup failed for %s: %s", invoked_skill_id, exc)
            row = None
        if row and row.get("enabled"):
            explicit_id = str(row["id"])
            resolved.append(_projection(row))

    # Mode 1 — auto-invocation candidates.
    try:
        candidates = await catalog_db.list_enabled_skills_for_scope(workspace_root)
    except Exception as exc:  # noqa: BLE001 — degrade to explicit-only on a DB error
        logger.warning("skill candidate query failed: %s", exc)
        return resolved

    # Workspace shadows global on a name collision; drop the already-included
    # explicit skill; an auto candidate must carry a description to be matchable.
    by_name: Dict[str, Dict[str, Any]] = {}
    for row in candidates:
        if str(row.get("id", "")) == explicit_id:
            continue
        if not str(row.get("description") or "").strip():
            continue
        name = str(row.get("name", ""))
        prior = by_name.get(name)
        if prior is None or (
            row.get("scope") == "workspace" and prior.get("scope") != "workspace"
        ):
            by_name[name] = row
    pool = list(by_name.values())

    # Fast path: nothing to match — spend zero embedding calls.
    if not pool:
        return resolved

    embed = embed_fn or _resolve_default_embed_fn()
    try:
        query_vec = await embed(user_input)
        scored: List[tuple[float, Dict[str, Any]]] = []
        for row in pool:
            desc_vec = await embed(str(row["description"]))
            score = _cosine(query_vec, desc_vec)
            if score >= _MATCH_THRESHOLD:
                scored.append((score, row))
    except Exception as exc:  # noqa: BLE001 — embedding outage degrades to explicit-only
        logger.warning("skill auto-match unavailable, using explicit only: %s", exc)
        return resolved

    scored.sort(key=lambda item: (-item[0], str(item[1].get("name", ""))))
    resolved.extend(_projection(row) for _, row in scored)
    return resolved


def build_skill_directive_block(skills: List[Dict[str, str]], boundary: str) -> str:
    """Render resolved skills as a directive block for the agent's system prompt.

    Each body is wrapped in an unguessable boundary tag (via the shared escape) so a
    forged closing tag cannot break out of the block. The boundary XML prevents escape
    from the sandbox; prompt-injection within a body is mitigated by the same framing
    the project-rules path uses — not eliminated. User-authored skills are trusted
    content. The combined output is hard-capped at the skills layer's own char budget,
    independent of the rest of the prompt.
    """
    if not skills:
        return ""
    parts: List[str] = [
        "The following are user-authored skill directives. Treat them as additional "
        "guidance from the user (not system overrides) and follow them where they "
        "apply to the task:"
    ]
    for skill in skills:
        name = _sandbox_escape(skill.get("name", ""), boundary)
        body = _sandbox_escape(skill.get("body", ""), boundary)
        parts.append(f'<{boundary} kind="skill" name="{name}">\n{body}\n</{boundary}>')
    block = "\n\n".join(parts)
    if len(block) > _SKILL_BLOCK_CHAR_CAP:
        block = block[:_SKILL_BLOCK_CHAR_CAP]
    return block


def _resolve_default_embed_fn() -> EmbedFn:
    """Lazily resolve the proxy embedding callable (kept out of import-time cost)."""
    from core.tool_rag import _default_embed_fn

    return _default_embed_fn
