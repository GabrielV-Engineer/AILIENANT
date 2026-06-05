# core/memory/trajectory_memory.py
"""Phase 3.0.1 — Successful State Vectorization Engine (Trajectory Memory).

Persists successful mission trajectories to LanceDB after exit-code-0
completions and retrieves semantically similar past trajectories to augment
PlannerAgent context.

Blocking LanceDB operations run inside asyncio.to_thread.
Embedding generation uses litellm.aembedding() (already async).

Phase 3.0.2 will wire memorize_trajectory() into brain/guardrails.py
(validate_output → END path).
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from datetime import datetime, timezone
from typing import Any, Dict, List

import lancedb  # type: ignore[import-untyped]
import pyarrow as pa

import litellm

from shared.config import LANCEDB_PATH, LITELLM_PROXY_API_KEY, LITELLM_PROXY_BASE_URL, MODEL_EMBEDDING

logger = logging.getLogger("TRAJECTORY_MEMORY")

_EMBEDDING_DIM: int = int(os.getenv("AILIENANT_EMBEDDING_DIM", "1536"))
_TABLE_NAME: str = "trajectory_episodes"
_TOP_K: int = 3
_HNSW_MIN_ROWS: int = 256   # IVF training minimum with num_partitions=1

# Strict allowlist: SHA-256 hex (64 chars) or human-readable IDs.
# Prevents SQL injection in the native .where() predicate.
_SAFE_ID_RE: re.Pattern[str] = re.compile(r"^[a-zA-Z0-9_-]{1,128}$")

_TRAJECTORY_SCHEMA: pa.Schema = pa.schema([
    pa.field("task_id",     pa.utf8()),
    pa.field("project_id",  pa.utf8()),
    pa.field("user_input",  pa.utf8()),
    pa.field("outcome",     pa.utf8()),
    pa.field("wbs_summary", pa.utf8()),   # JSON-encoded list of WBSStep dicts
    pa.field("vector",      pa.list_(pa.float32(), list_size=_EMBEDDING_DIM)),
    pa.field("created_at",  pa.utf8()),   # ISO-8601 UTC timestamp
])


def format_trajectories_for_prompt(trajectories: List[Dict[str, Any]]) -> str:
    """Convert retrieved trajectory dicts into a prompt-injectable context block."""
    lines: List[str] = ["## Relevant Past Trajectories (Episodic Memory):"]
    for i, t in enumerate(trajectories, 1):
        lines.append(f"\n### Past Trajectory #{i}")
        lines.append(f"- **Goal:** {str(t.get('user_input', ''))[:200]}")
        lines.append(f"- **Outcome:** {str(t.get('outcome', ''))[:200]}")
        lines.append(f"- **Execution Plan:** {str(t.get('wbs_summary', ''))[:400]}")
    return "\n".join(lines)


class TrajectoryMemoryManager:
    """Async LanceDB-backed trajectory store.

    Stateless — safe to share across concurrent LangGraph fan-out invocations.
    """

    def __init__(self, lancedb_path: str = LANCEDB_PATH) -> None:
        self._lancedb_path = lancedb_path

    # ── Public API ────────────────────────────────────────────────────

    async def memorize_trajectory(
        self,
        state: Dict[str, Any],
        success_flag: bool,
    ) -> None:
        """Persist a successful mission trajectory to LanceDB.

        No-op if success_flag is False or mission_spec is absent.

        Called from brain/guardrails.py (validate_output → END) in Phase 3.0.2:
            await _traj_mgr.memorize_trajectory(state, success_flag=True)
        """
        if not success_flag:
            return

        mission_spec = state.get("mission_spec")
        if mission_spec is None:
            logger.debug("TrajectoryMemory: no mission_spec — skipping.")
            return

        task_id: str = state.get("task_id") or ""
        project_id: str = state.get("project_id") or ""
        user_input: str = state.get("user_input") or ""
        tasks = list(getattr(mission_spec, "tasks", []) or [])
        outcome: str = getattr(mission_spec, "outcome", "") or ""

        fingerprint = (
            f"[GOAL] {user_input} "
            f"[OUTCOME] {outcome} "
            f"[TASKS] {' '.join(f'{t.action}:{t.target_file}' for t in tasks)}"
        )
        wbs_json = json.dumps(
            [
                {
                    "action": t.action,
                    "target_file": t.target_file,
                    "description": t.description,
                }
                for t in tasks
            ]
        )

        try:
            vector = await _get_embedding(fingerprint)
        except Exception as embed_err:
            logger.warning("TrajectoryMemory: embedding failed (non-fatal): %s", embed_err)
            return

        record: Dict[str, Any] = {
            "task_id":     task_id,
            "project_id":  project_id,
            "user_input":  user_input,
            "outcome":     outcome,
            "wbs_summary": wbs_json,
            "vector":      vector,
            "created_at":  datetime.now(timezone.utc).isoformat(),
        }

        try:
            await asyncio.to_thread(self._write_record, record)
            logger.info(
                "TrajectoryMemory: persisted task_id=%s project=%s", task_id, project_id
            )
        except Exception as write_err:
            logger.warning("TrajectoryMemory: write failed (non-fatal): %s", write_err)

    async def search(
        self,
        user_input: str,
        project_id: str = "",
        k: int = _TOP_K,
    ) -> List[Dict[str, Any]]:
        """Retrieve top-k semantically similar past trajectories.

        Filters by project_id when provided (pre-filter pushdown via DataFusion).
        Returns [] on any failure (non-fatal).
        """
        if not user_input.strip():
            return []

        try:
            vector = await _get_embedding(user_input)
        except Exception as embed_err:
            logger.warning("TrajectoryMemory.search: embed failed (non-fatal): %s", embed_err)
            return []

        try:
            return await asyncio.to_thread(self._query_records, vector, project_id, k)
        except Exception as query_err:
            logger.warning("TrajectoryMemory.search: query failed (non-fatal): %s", query_err)
            return []

    # ── Blocking helpers (asyncio.to_thread) ──────────────────────────

    def _write_record(self, record: Dict[str, Any]) -> None:
        db = lancedb.connect(self._lancedb_path)
        if _TABLE_NAME in db.table_names():
            tbl = db.open_table(_TABLE_NAME)
        else:
            tbl = db.create_table(_TABLE_NAME, schema=_TRAJECTORY_SCHEMA)

        tbl.add([record])

        try:
            tbl.create_index(
                vector_column_name="vector",
                index_type="IVF_HNSW_SQ",
                metric="cosine",
                num_partitions=1,
                m=20,
                ef_construction=300,
                replace=True,
            )
        except Exception as idx_err:
            logger.debug(
                "HNSW index deferred (table likely too small, need %d rows): %s",
                _HNSW_MIN_ROWS,
                idx_err,
            )

    def _query_records(
        self,
        vector: List[float],
        project_id: str,
        k: int,
    ) -> List[Dict[str, Any]]:
        db = lancedb.connect(self._lancedb_path)
        if _TABLE_NAME not in db.table_names():
            return []

        tbl = db.open_table(_TABLE_NAME)
        query = tbl.search(vector).metric("cosine").limit(k)

        # Pre-filter pushdown: DataFusion applies the predicate during HNSW
        # traversal, guaranteeing true O(log N) latency and full recall within
        # the project domain. Skipped if project_id is empty or fails the
        # allowlist check — never inject unsanitized input.
        if project_id and _SAFE_ID_RE.match(project_id):
            query = query.where(f"project_id = '{project_id}'")
        elif project_id:
            logger.warning(
                "TrajectoryMemory: project_id %r failed sanitization — filter skipped.",
                project_id,
            )

        rows: List[Any] = query.to_list()
        return [
            {
                "task_id":     r.get("task_id", ""),
                "user_input":  r.get("user_input", ""),
                "outcome":     r.get("outcome", ""),
                "wbs_summary": r.get("wbs_summary", ""),
            }
            for r in rows
        ]


# ── Module-level helpers ───────────────────────────────────────────────────


async def _get_embedding(text: str) -> List[float]:
    """Call embedding model via LiteLLM proxy. Async — does NOT block event loop."""
    resp = await litellm.aembedding(
        model=MODEL_EMBEDDING,
        input=[text],
        api_key=LITELLM_PROXY_API_KEY,
        api_base=LITELLM_PROXY_BASE_URL,
    )
    data: Any = resp.data[0]
    embedding: List[float] = (
        data["embedding"] if isinstance(data, dict) else data.embedding
    )
    return embedding
