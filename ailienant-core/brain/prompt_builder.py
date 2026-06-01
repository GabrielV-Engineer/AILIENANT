"""
brain/prompt_builder.py — Token-budget-aware context assembler (Butterfly Effect).

Tier 1 Flesh:    active_file + top-N PPR files → full content via VFS firewall.
Tier 2 Skeleton: medium-PPR Python files → AST-stripped to signatures.
Polyglot:        non-Python files promoted to Flesh-First; header-only (50 lines)
                 as fallback if full content exceeds remaining budget.

Token budget = 80% of context_window. Pruning: drop lowest-PPR skeletons first.
Skeleton guard: result must be ≤ 15% of original content by character count.

All heavy imports deferred inside build_context() to avoid circular import at startup.
ASTEngine initialized once in __init__ to reuse its BLAKE2B parse cache across calls.
"""
from __future__ import annotations

import logging
import os
import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, List, Optional, Tuple

if TYPE_CHECKING:
    from brain.state import AIlienantGraphState
    from shared.rbac import AgentIdentity

logger = logging.getLogger("PROMPT_BUILDER")

_SKELETON_MAX_RATIO: float = 0.15  # skeleton must be ≤ 15% of original by character count
_HEADER_LINES: int = 50            # non-Python fallback: first N lines as "header skeleton"


# ── Output contracts ──────────────────────────────────────────────────────────

@dataclass
class ContextBlock:
    file_path: str
    content: str
    ppr_score: float
    tier: str  # "flesh" | "skeleton"


@dataclass
class ContextBundle:
    flesh_blocks: List[ContextBlock] = field(default_factory=list)
    skeleton_blocks: List[ContextBlock] = field(default_factory=list)
    total_tokens: int = 0
    partial_context_prefix: str = ""

    def full_text(self) -> str:
        """Concatenate all blocks in tier order for prompt injection."""
        parts: List[str] = []
        if self.partial_context_prefix:
            parts.append(self.partial_context_prefix)
        for blk in self.flesh_blocks:
            parts.append(f'<file path="{blk.file_path}" tier="flesh">\n{blk.content}\n</file>')
        for blk in self.skeleton_blocks:
            parts.append(f'<file path="{blk.file_path}" tier="skeleton">\n{blk.content}\n</file>')
        return "\n\n".join(parts)


# ── Python skeleton extraction ────────────────────────────────────────────────

def _function_signature(node: object, lines: List[str]) -> str:
    """Return 'def name(args) -> ret: ...' for a function_definition AST node."""
    try:
        body = node.child_by_field_name("body")  # type: ignore[attr-defined]
        # sig_end is the last line before the body block starts
        sig_end = body.start_point[0] - 1 if body else node.start_point[0]  # type: ignore[attr-defined]
        sig = "".join(lines[node.start_point[0]: sig_end + 1]).rstrip().rstrip(":")  # type: ignore[attr-defined]
        return sig + ": ..."
    except Exception:
        # Fallback: just the def line, stripped of its colon
        return lines[node.start_point[0]].rstrip().rstrip(":") + ": ..."  # type: ignore[attr-defined]


def _extract_python_skeleton(content: str, tree: object) -> Optional[str]:
    """Strip function/method bodies to '...', keep imports + class headers +
    docstrings + module-level constants.

    Returns None if:
    - tree is None (parse failed)
    - extraction raises an unhandled exception
    - result exceeds _SKELETON_MAX_RATIO of original content by character count
      (in which case a minimal imports-only fallback is attempted)
    """
    if tree is None:
        return None
    try:
        lines = content.splitlines(keepends=True)
        out: List[str] = []

        for node in tree.root_node.children:  # type: ignore[attr-defined]
            nt = node.type
            sl = node.start_point[0]
            el = node.end_point[0]

            if nt in ("import_statement", "import_from_statement", "import_from"):
                out.extend(lines[sl: el + 1])

            elif nt == "class_definition":
                out.extend(lines[sl: sl + 1])  # class header line only
                body = node.child_by_field_name("body")
                if body:
                    for child in body.children:
                        if child.type == "expression_statement":
                            # First expression in class body = docstring position
                            expr = child.children[0] if child.children else None
                            if expr and expr.type in ("string", "concatenated_string"):
                                out.extend(lines[child.start_point[0]: child.end_point[0] + 1])
                            break
                        elif child.type == "function_definition":
                            out.append(_function_signature(child, lines) + "\n")

            elif nt == "function_definition":
                out.append(_function_signature(node, lines) + "\n")

            elif nt in ("expression_statement", "assignment"):
                # Keep module-level constants and simple assignments
                raw = "".join(lines[sl: el + 1]).strip()
                if raw and "=" in raw:
                    out.extend(lines[sl: el + 1])

        skeleton = "".join(out)

        # Character-count guard (cheap heuristic before calling tiktoken)
        if len(skeleton) > len(content) * _SKELETON_MAX_RATIO:
            # Aggressive fallback: imports + bare def/class header lines only
            out = []
            for node in tree.root_node.children:  # type: ignore[attr-defined]
                nt = node.type
                sl = node.start_point[0]
                if nt in ("import_statement", "import_from_statement", "import_from"):
                    out.extend(lines[sl: node.end_point[0] + 1])
                elif nt in ("function_definition", "class_definition"):
                    out.append(lines[sl])
            skeleton = "".join(out)

        return skeleton if skeleton.strip() else None

    except Exception as exc:
        logger.debug("Skeleton extraction failed: %s", exc)
        return None


# ── PromptBuilder ─────────────────────────────────────────────────────────────

class PromptBuilder:
    """
    Assembles a token-budget-aware ContextBundle for LLM prompt injection.

    Polyglot strategy (Phase 2.5):
    - Python files in skeleton tier → AST skeleton extraction
    - Non-Python files in skeleton tier → Flesh-First (full content if budget allows);
      header-only (first _HEADER_LINES lines) as fallback; dropped if header also overflows.
    TODO(Phase 3): implement tree-sitter skeleton extraction for JS/TS/Rust/Go.

    Usage:
        bundle = await prompt_builder.build_context(
            active_file="src/main.py",
            project_id="",
            model_name="gpt-4",
            context_window=8192,
        )
        system_prompt = bundle.full_text()
    """

    def __init__(self) -> None:
        from core.ast_engine import ASTEngine
        self._ast = ASTEngine()

    async def build_context(
        self,
        active_file: str,
        project_id: str,
        model_name: str,
        context_window: int,
        top_n_flesh: int = 5,
        top_n_skeleton: int = 10,
    ) -> ContextBundle:
        from core.db import get_top_ppr_files
        from core.vfs_middleware import make_safe_reader
        from tools.token_counter import PrecisionTokenCounter
        from brain.orchestrator import orchestrator_context

        token_budget = int(context_window * 0.80)
        flesh_blocks: List[ContextBlock] = []
        skeleton_blocks: List[ContextBlock] = []
        tokens_used = 0

        # Partial context warning (empty string when indexing is complete)
        prefix = orchestrator_context.get_partial_context_prefix()
        if prefix:
            tokens_used += PrecisionTokenCounter.estimate_with_buffer(prefix, model_name)

        # Firewalled reader for context assembly; returns the file text or None.
        _read = make_safe_reader(project_id, None, None)

        # ── Tier 1 Flesh: active file (always injected first) ────────────
        active_content = _read(active_file)
        if active_content:
            cost = PrecisionTokenCounter.estimate_with_buffer(active_content, model_name)
            flesh_blocks.append(ContextBlock(active_file, active_content, 1.0, "flesh"))
            tokens_used += cost

        # ── Tier 1 Flesh: top-N PPR-ranked files ─────────────────────────
        top_files: List[Tuple[str, float]] = await get_top_ppr_files(
            project_id, top_n_flesh + top_n_skeleton
        )
        candidates = [(p, s) for p, s in top_files if p != active_file]

        for file_path, ppr_score in candidates[:top_n_flesh]:
            if tokens_used >= token_budget:
                break
            content = _read(file_path)
            if not content:
                continue
            cost = PrecisionTokenCounter.estimate_with_buffer(content, model_name)
            if tokens_used + cost > token_budget:
                break
            flesh_blocks.append(ContextBlock(file_path, content, ppr_score, "flesh"))
            tokens_used += cost

        # ── Tier 2 — Polyglot context strategy ───────────────────────────
        # Python:     AST skeleton (signatures only)
        # Non-Python: Flesh-First → full content if budget allows; else 50-line header
        #             Drop entirely if even the header overflows budget.
        # TODO(Phase 3): add tree-sitter skeleton extraction for JS/TS/Rust/Go
        pending: List[ContextBlock] = []

        for file_path, ppr_score in candidates[top_n_flesh: top_n_flesh + top_n_skeleton]:
            content = _read(file_path)
            if not content:
                continue
            ext = os.path.splitext(file_path)[1].lower()

            if ext == ".py":
                # Python: attempt AST skeleton; fall back to full content if extraction fails
                tree = self._ast.parse(file_path, content, "python")
                skeleton = _extract_python_skeleton(content, tree)
                pending.append(ContextBlock(file_path, skeleton or content, ppr_score, "skeleton"))
            else:
                # Non-Python: Flesh-First strategy
                cost_full = PrecisionTokenCounter.estimate_with_buffer(content, model_name)
                if tokens_used + cost_full <= token_budget:
                    # Fits as full flesh block — add immediately, skip skeleton queue
                    flesh_blocks.append(ContextBlock(file_path, content, ppr_score, "flesh"))
                    tokens_used += cost_full
                else:
                    # Too large for flesh — queue a 50-line header as skeleton fallback
                    header = "\n".join(content.splitlines()[:_HEADER_LINES])
                    pending.append(ContextBlock(file_path, header, ppr_score, "skeleton"))

        # Prune pending skeleton/header blocks: add highest-PPR first, drop those that overflow
        for blk in sorted(pending, key=lambda b: b.ppr_score, reverse=True):
            cost = PrecisionTokenCounter.estimate_with_buffer(blk.content, model_name)
            if tokens_used + cost <= token_budget:
                skeleton_blocks.append(blk)
                tokens_used += cost
            # else: silently dropped (lowest-PPR files are naturally last in iteration)

        return ContextBundle(
            flesh_blocks=flesh_blocks,
            skeleton_blocks=skeleton_blocks,
            total_tokens=tokens_used,
            partial_context_prefix=prefix,
        )


# Module-level singleton
prompt_builder = PromptBuilder()


# ── Phase 5.1.1 — Centralized boundary_id ownership ──────────────────────────
# Per PHASE_5_BLUEPRINT §2.4: prompt_builder.build_system_prompt(state) generates
# boundary_id = uuid.uuid4().hex and writes it to state; the axiom string in
# agents.prompts.BASE_SYSTEM_PROMPT interpolates it. Callers that adopt this
# function get a state-owned per-turn boundary; legacy inline uuid4().hex sites
# (e.g. agents/planner.py) are unchanged in this PR and migrate in a follow-up.

def build_system_prompt(
    state: "AIlienantGraphState",
    agent_identity: "AgentIdentity",
    context_str: str = "",
    target_role: Optional[str] = None,
) -> str:
    """Generate a per-turn boundary_id, write it to state, return the System Prompt.

    Delegates final assembly to agents.prompts.build_safe_prompt — this function
    owns only the boundary generation and state mutation.
    """
    from agents.prompts import build_safe_prompt  # local import: avoid circular

    boundary = uuid.uuid4().hex
    state["boundary_id"] = boundary
    return build_safe_prompt(
        agent_identity=agent_identity,
        context_str=context_str,
        boundary=boundary,
        target_role=target_role,
    )
