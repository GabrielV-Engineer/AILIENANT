"""Transactional candidate tournament over a shared physical work surface.

Picks the best of several competing edits by pushing each candidate onto the shared
surface, running a verify command for a structured verdict, rolling the surface back to a
clean base between candidates, and selecting the UCB1 winner via the contained MCTS tree.
The evaluation is transactional because the surface is physical and shared: a naive loop
would leave Candidate A's mutations in place while Candidate B is verified.

The reward is each candidate's *own* verdict (exit code + diagnostic volume), so no extra
model or judge calls are made. This module is the reusable engine behind two orchestration
shapes: the agentic cell's competing-fix governance and the dispatch layer's
generate-and-filter / tournament patterns (``run_tournament_from_dispatch``).
"""
from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple

from brain.state import VFSFile
from brain.subagent_contracts import DispatchBatchResult, SubagentResultEnvelope

logger = logging.getLogger("SUBAGENT_TOURNAMENT")


def _verdict_reward(exit_code: int, diagnostics: str) -> float:
    """Map a structured verdict to an MCTS reward in [-1.0, 1.0].

    Exit 0 with no diagnostics is the apex (+1.0); a non-zero exit is graded down by how
    much diagnostic text it produced, so a near-miss outranks a wall of errors. This is the
    caller's *own* verdict — no extra LLM/judge calls — which keeps branch evaluation cheap.
    """
    if exit_code == 0:
        return 1.0 if not diagnostics.strip() else 0.5
    # Non-zero: scale -0.2 .. -1.0 by diagnostic volume (longer = worse).
    penalty = min(len(diagnostics), 4000) / 4000.0
    return -0.2 - 0.8 * penalty


def _vfs_to_view(vfs_files: Dict[str, VFSFile]) -> Dict[str, str]:
    """blob-hash view (path -> blob_hash) for MCTS node bookkeeping."""
    return {path: vf.blob_hash for path, vf in vfs_files.items()}


def _content_to_vfs(content_by_path: Dict[str, str], blob_store: Any) -> Dict[str, VFSFile]:
    """Materialize {path: content} into {path: VFSFile} backed by the blob store."""
    from agents.coder import content_hash

    out: Dict[str, VFSFile] = {}
    for path, content in content_by_path.items():
        out[path] = VFSFile(
            blob_hash=blob_store.put(content),
            document_version_id=content_hash(content),
            is_dirty=True,
        )
    return out


# =====================================================================
# Contained MCTS branch governance (transactional surface restoration)
# =====================================================================

async def run_tournament(
    *,
    surface: Any,
    clean_base_content: Dict[str, str],
    candidates: List[Dict[str, str]],
    verify_command: str,
    run_verify: Callable[[str], Awaitable[Tuple[int, str]]],
    blob_store: Any,
    mission_state: Any,
) -> Tuple[int, Dict[str, str]]:
    """Pick the best of >=2 competing edits over the *shared* persistent surface.

    Each candidate is a full {path: content} working set. The surface is physical and
    shared, so candidates cannot be evaluated naively — Candidate A's run would leave the
    files mutated when Candidate B is verified. The evaluation is therefore transactional:

        for each candidate i:
            push candidate i -> surface
            run the verify command -> structured verdict -> reward
            roll the surface back to the clean base   (undo i before evaluating i+1)
        select the UCB1 winner
        restore the surface to the winner

    Returns ``(winner_index, winner_content)``. Reward is the candidate's own verdict, so no
    extra model/judge calls are made.
    """
    from brain.mcts.tree import MCTSTree
    from core.workspace_sync import push_vfs_to_surface

    base_vfs = _content_to_vfs(clean_base_content, blob_store)
    base_version_ids = {p: vf.document_version_id for p, vf in base_vfs.items()}

    tree = MCTSTree(root_state=mission_state, root_vfs_view=_vfs_to_view(base_vfs))
    child_ids: List[str] = []

    for index, candidate in enumerate(candidates):
        cand_vfs = _content_to_vfs(candidate, blob_store)
        cand_version_ids = {p: vf.document_version_id for p, vf in cand_vfs.items()}

        await push_vfs_to_surface(surface, cand_vfs, blob_store, cand_version_ids)
        exit_code, diagnostics = await run_verify(verify_command)
        reward = _verdict_reward(exit_code, diagnostics)

        child = tree.expand(
            tree.root_id,
            action=f"candidate_{index}",
            new_vfs_view=_vfs_to_view(cand_vfs),
            child_mission_state=mission_state,
        )
        child.reward = reward
        child.visits = 1
        child.total_value = reward
        child_ids.append(child.node_id)

        # Roll the physical surface back to the clean base before the next candidate.
        await push_vfs_to_surface(surface, base_vfs, blob_store, base_version_ids)

    # UCB1 needs the parent visited; backpropagate one visit per evaluated candidate.
    tree.get_node(tree.root_id).visits = len(child_ids)
    best_id = tree.select_best_child(tree.root_id)
    winner_index = child_ids.index(best_id) if best_id in child_ids else 0
    winner_content = candidates[winner_index]

    # Restore the surface to the winning candidate's condition.
    winner_vfs = _content_to_vfs(winner_content, blob_store)
    winner_version_ids = {p: vf.document_version_id for p, vf in winner_vfs.items()}
    await push_vfs_to_surface(surface, winner_vfs, blob_store, winner_version_ids)

    return winner_index, winner_content


def _default_candidate_from_envelope(env: SubagentResultEnvelope) -> Optional[Dict[str, str]]:
    """Default envelope -> {path: content} extraction — a *convention*, not a guarantee.

    Treats every string-valued field of ``structured_result`` as a ``path -> content`` pair
    (``int``/``float``/``bool``/``list_str`` fields carry metadata, not file bodies, and are
    dropped). This is only correct when the subagent's response schema is deliberately
    path-keyed: a schema mixing a body field with a prose field (e.g. ``{"a.py": ...,
    "explanation": ...}``) would materialize a file literally named ``explanation``. Any real
    dispatch pattern whose schema is not path-keyed must pass an explicit ``candidate_extractor``
    to :func:`run_tournament_from_dispatch` rather than rely on this default.

    Returns ``None`` for a non-``ok`` envelope or one carrying no usable string map (caller skips).
    """
    if env.status != "ok" or not env.structured_result:
        return None
    patch = {k: v for k, v in env.structured_result.items() if isinstance(v, str)}
    return patch or None


async def run_tournament_from_dispatch(
    batch_result: "DispatchBatchResult | Dict[str, Any]",
    *,
    surface: Any,
    clean_base_content: Dict[str, str],
    verify_command: str,
    run_verify: Callable[[str], Awaitable[Tuple[int, str]]],
    blob_store: Any,
    mission_state: Any,
    candidate_extractor: Optional[
        Callable[[SubagentResultEnvelope], Optional[Dict[str, str]]]
    ] = None,
) -> Tuple[int, Dict[str, str], Optional[str]]:
    """Adapt a dispatch batch of subagent-proposed edits into the tournament and run it.

    Each envelope is mapped to a ``{path: content}`` candidate working set via
    ``candidate_extractor`` (or :func:`_default_candidate_from_envelope` when ``None`` — see its
    caveat: the default assumes a path-keyed schema). An extractor returning ``None``/empty is
    skipped. The transactional push/verify/rollback and the UCB1 selection are delegated verbatim
    to :func:`run_tournament` — this adapter never duplicates that logic, and a caller-supplied
    ``candidate_extractor`` that raises is deliberately *not* caught (a programmer error surfaces).

    Returns ``(winner_index, winner_content, winner_task_id)`` so a caller can populate
    ``DispatchBatchResult.winner_task_id`` (left ``None`` by the synthesis node). ``winner_index``
    is relative to the *filtered* candidate list, not the raw envelope list — ``winner_task_id`` is
    the unambiguous identifier.

    Isolation caveat: :func:`run_tournament`'s surface rollback restores base file *contents* but
    does not delete a candidate's *newly introduced* paths (``push_vfs_to_surface`` only writes).
    Candidates should therefore share ``clean_base_content``'s path set; a candidate introducing an
    out-of-base path is still evaluated but logged as a contamination risk. Full delete-not-in-base
    isolation is deferred (DEBT-104).

    Raises ``ValueError`` (fail-fast) when the batch yields no usable candidate edit.
    """
    batch = (
        DispatchBatchResult.model_validate(batch_result)
        if isinstance(batch_result, dict)
        else batch_result
    )
    extract = candidate_extractor or _default_candidate_from_envelope
    base_paths = set(clean_base_content)

    candidates: List[Dict[str, str]] = []
    candidate_task_ids: List[str] = []
    for env in batch.results:
        patch = extract(env)
        if not patch:
            continue
        out_of_base = sorted(set(patch) - base_paths)
        if out_of_base:
            # Observability only: the surface rollback cannot delete these, so they can
            # contaminate sibling candidates and the winner restore (DEBT-104).
            logger.warning(
                "run_tournament_from_dispatch: task %r introduces path(s) outside the clean "
                "base %s — not isolated by surface rollback (DEBT-104)",
                env.task_id, out_of_base,
            )
        candidates.append(patch)
        candidate_task_ids.append(env.task_id)

    if not candidates:
        raise ValueError(
            "run_tournament_from_dispatch: no usable candidate edits in batch "
            f"{batch.batch_id!r} (pattern={batch.pattern!r})"
        )

    winner_index, winner_content = await run_tournament(
        surface=surface,
        clean_base_content=clean_base_content,
        candidates=candidates,
        verify_command=verify_command,
        run_verify=run_verify,
        blob_store=blob_store,
        mission_state=mission_state,
    )
    return winner_index, winner_content, candidate_task_ids[winner_index]
