# ailienant-core/agents/researcher.py
#
# Phase 4.1.1 — ResearcherAgent (The Context Hound).
#
# Read-only LangGraph node that produces a Skeleton Map (signatures + relations +
# file paths) for the PlannerAgent. Follows the planner pattern: deterministic
# retrieval (no LangChain bind_tools / ReAct) + single LLMGateway.ainvoke call.
#
# Glob/Grep tooling intentionally NOT bound here — GraphRAG already covers the
# retrieval intent. If a future sub-phase surfaces a concrete gap, this is where
# `make_glob_tool` / `make_grep_tool` factories would be wired in.

from __future__ import annotations

import logging
import os
import uuid
from typing import Any, Dict, List

from agents.prompts import build_safe_prompt
from shared.config import MODEL_MEDIUM
from shared.rbac import RESEARCHER_IDENTITY
from tools.llm_gateway import LLMGateway

# Deferred imports of memory + VFS modules happen inside the function body to
# mirror the existing convention (see core/memory/graphrag_extractor.py:376 and
# brain/prompt_builder.py:177). This avoids circular imports between
# agents/ -> brain/ -> agents/ during graph construction.

logger = logging.getLogger("RESEARCHER_NODE")

# Module-level toggle. Set AILIENANT_RESEARCHER_DEBUG=0 in production to enable
# the full retrieval + LLM path. Promoted to module scope so tests can
# `patch("agents.researcher.DEBUG_MODE", False)` per the planner convention.
DEBUG_MODE: bool = os.getenv("AILIENANT_RESEARCHER_DEBUG", "1") != "0"

_SKELETON_INSTRUCTION: str = (
    "You are the ResearcherAgent. Output a structured 'Skeleton Map' for the "
    "PlannerAgent: list (a) relevant files with a one-line purpose, (b) public "
    "function and class signatures, (c) cross-module relationships. DO NOT write "
    "implementation code. DO NOT dump full file contents. Respond as compact "
    "markdown sections."
)


async def run_researcher_node(state: Dict[str, Any]) -> Dict[str, Any]:
    """Read-only retrieval node — produces ``state["researcher_skeleton"]``.

    Decision tree:
        1. If ``state["explicit_mentions"]`` has entries, bypass GraphRAG and
           load those files verbatim via ``VFSMiddleware.read``.
        2. Otherwise, run the existing GraphRAG pipeline
           (``SemanticMemoryManager.search_with_paths`` →
           ``GraphRAGDynamicExtractor.deep_parse``) and use its formatted
           context block.
        3. Either way, make ONE ``LLMGateway.ainvoke`` call that asks the model
           to compress the retrieval into a Skeleton Map.

    Failure mode: any retrieval error is logged and downgraded — the LLM call
    still happens (with whatever context was gathered). An LLM-level error
    surfaces via ``state["errors"]`` per the planner convention.
    """
    session_id: str = state.get("task_id") or str(uuid.uuid4())
    user_input: str = state.get("user_input", "")
    workspace_root: str = state.get("workspace_root") or ""
    project_id: str = state.get("project_id") or ""
    explicit_mentions: List[str] = state.get("explicit_mentions") or []

    # ── DEBUG bypass — mirrors planner.DEBUG_MODE for CI / UI smoke tests ──
    if DEBUG_MODE:
        logger.warning("⚠️ RESEARCHER DEBUG MODE: emitting synthetic skeleton.")
        return {
            "researcher_skeleton": (
                "## Skeleton (debug)\n- (synthetic, no retrieval performed)\n"
            )
        }

    # ── Override A: @-mention bypass — read the requested files verbatim ──
    forced_blocks: List[str] = []
    if explicit_mentions:
        # Deferred import: VFSMiddleware is a singleton, instantiation is O(1).
        from core.vfs_middleware import VFSMiddleware

        vfs = VFSMiddleware()  # type: ignore[no-untyped-call]
        for path in explicit_mentions:
            try:
                content = vfs.read(path)
            except FileNotFoundError:
                logger.warning("Researcher: @-mention '%s' not found in VFS.", path)
                continue
            except Exception as err:  # noqa: BLE001 — fail-soft on any I/O issue
                logger.warning("Researcher: VFS read failed for '%s': %s", path, err)
                continue
            # Phase 7.11.4 (ADR-706 §4.5d) — hard-context envelope. The
            # `[HARD CONTEXT: SOURCE FILE …]` prefix signals to the LLM that
            # this material is authoritative user-supplied content and must
            # NOT be filtered/summarised; the surrounding RAG bypass at the
            # caller already guarantees that GraphRAG is skipped on this turn.
            forced_blocks.append(
                f"[HARD CONTEXT: SOURCE FILE {path}]\n```\n{content}\n```"
            )

    # ── Path B: GraphRAG retrieval — only when no @-mentions were supplied ──
    deep_block: str = ""
    if not explicit_mentions:
        try:
            from core.memory.graphrag_extractor import GraphRAGDynamicExtractor
            from core.memory.semantic_memory import SemanticMemoryManager

            sem = SemanticMemoryManager()
            _score, top_k = await sem.search_with_paths(
                user_input=user_input,
                workspace_hash=project_id,
            )
            extractor = GraphRAGDynamicExtractor(project_id=project_id)
            deep = await extractor.deep_parse(
                seed_files=top_k,
                workspace_root=workspace_root,
            )
            deep_block = deep.context_block or ""
            logger.info(
                "Researcher: GraphRAG returned %d parsed files (sem=%.3f).",
                len(deep.parsed_files),
                _score,
            )
        except Exception as err:  # noqa: BLE001 — non-fatal per planner convention
            logger.warning(
                "Researcher: GraphRAG retrieval failed (non-fatal): %s", err
            )

    # ── Compose system prompt + single LLM call ──
    boundary: str = uuid.uuid4().hex
    body_parts: List[str] = list(forced_blocks)
    if deep_block:
        body_parts.append(deep_block)
    context_blob: str = (
        "\n\n".join(body_parts)
        if body_parts
        else f"<{boundary}>No retrieval results.</{boundary}>"
    )

    system_prompt: str = build_safe_prompt(
        agent_identity=RESEARCHER_IDENTITY,
        context_str=context_blob,
        boundary=boundary,
    )
    messages: List[Dict[str, str]] = [
        {"role": "system", "content": system_prompt},
        {
            "role": "user",
            "content": f"User request: {user_input!r}\n\n{_SKELETON_INSTRUCTION}",
        },
    ]

    try:
        response = await LLMGateway.ainvoke(
            messages=messages,
            model=MODEL_MEDIUM,
            temperature=0.0,
            max_tokens=2048,
            session_id=session_id,
        )
        skeleton: str = (response.choices[0].message.content or "").strip()
        logger.info("Researcher: skeleton produced (%d chars).", len(skeleton))
        return {"researcher_skeleton": skeleton}
    except Exception as err:  # noqa: BLE001 — surface to state.errors for retry policy
        logger.error("Researcher: LLM call failed: %s", err)
        return {"errors": [f"Researcher Error: {err}"]}
