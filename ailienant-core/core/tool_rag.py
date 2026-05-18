"""Phase 5.2 — Tool RAG: just-in-time tool-schema injection.

See docs/PHASE_5_BLUEPRINT.md §3 for the architectural contract.

ToolRAGStore owns a RAM-resident, schemas-only LanceDB instance backed by a
tempfile.mkdtemp() directory that is purged at process exit. It is independent
from the file-PPR LanceDB used by brain.prompt_builder.

select_tools() returns at most TOOL_RAG_TOP_K (= 5) schemas per call, honours
the Phase 4 §3.2 RBAC matrix + the Phase 5 §2.2 session matrix, guarantees at
least one READ_ONLY survivor when one is available, and is deterministic for
identical (intent, active_role, session_mode) triples.

The 70% mean prompt-size reduction (TOOL_RAG_MIN_REDUCTION) is enforced by the
Phase 5.7 Checkpoint Gate; Phase 5.2 only proves the metric is computed and
appended to permission_audit_log on every selection.
"""

from __future__ import annotations

import atexit
import logging
import os
import shutil
import tempfile
from dataclasses import dataclass, field
from typing import (
    Any,
    Awaitable,
    Callable,
    Dict,
    FrozenSet,
    List,
    Optional,
)

import lancedb  # type: ignore[import-untyped]
import pyarrow as pa  # type: ignore[import-untyped]

from core.permissions import SessionPermissionMode, ToolPrivilegeTier

logger = logging.getLogger("TOOL_RAG")


# =====================================================================
# 1. CONSTANTS
# =====================================================================

TOOL_RAG_TOP_K: int = 5
"""Hard cap on schemas returned per select_tools() call (blueprint §5.1)."""

TOOL_RAG_MIN_REDUCTION: float = 0.70
"""Phase 5.7 Checkpoint Gate target — mean prompt-size reduction across intents."""

MCP_HANDSHAKE_TIMEOUT_SEC: float = 5.0
"""Re-exported here so callers can introspect the bootstrap deadline."""

_DEFAULT_EMBEDDING_DIM: int = int(os.getenv("AILIENANT_EMBEDDING_DIM", "1536"))
_LANCE_TABLE_NAME: str = "tool_schemas"


# =====================================================================
# 2. ToolSchema dataclass
# =====================================================================


@dataclass(frozen=True)
class ToolSchema:
    """In-memory representation of one tool's metadata + (optional) embedding.

    `allowed_roles` is a frozenset for hashability; on the LanceDB row it is
    persisted as a comma-flanked string (",core_dev,qa_tester,") so a future
    migration to .where() filtering remains possible without a schema change.
    """

    name: str
    description: str
    json_schema: str
    privilege_tier: ToolPrivilegeTier
    allowed_roles: FrozenSet[str] = field(default_factory=frozenset)
    embedding: Optional[List[float]] = None


def _roles_to_csv(roles: FrozenSet[str]) -> str:
    """Encode a roles set as ',role1,role2,' for substring search ergonomics."""
    if not roles:
        return ","
    return "," + ",".join(sorted(roles)) + ","


def _roles_from_csv(csv: str) -> FrozenSet[str]:
    return frozenset(r for r in csv.strip(",").split(",") if r)


# =====================================================================
# 3. Default embedding callable (litellm proxy)
# =====================================================================


async def _default_embed_fn(text: str) -> List[float]:
    """Embed a single string via the LiteLLM proxy at MODEL_EMBEDDING.

    Mirrors the call pattern in core/memory/semantic_memory.py — same model
    alias, same proxy. Imported lazily to avoid the litellm cost at module load.
    """
    from shared.config import (
        LITELLM_PROXY_API_KEY,
        LITELLM_PROXY_BASE_URL,
        MODEL_EMBEDDING,
    )

    import litellm

    resp = await litellm.aembedding(
        model=MODEL_EMBEDDING,
        input=[text],
        api_base=LITELLM_PROXY_BASE_URL,
        api_key=LITELLM_PROXY_API_KEY,
    )
    return list(resp["data"][0]["embedding"])


# =====================================================================
# 4. ToolRAGStore
# =====================================================================


EmbedFn = Callable[[str], Awaitable[List[float]]]


class ToolRAGStore:
    """RAM-resident (process-tmpfile-backed) LanceDB store of tool schemas."""

    def __init__(
        self,
        *,
        embed_fn: Optional[EmbedFn] = None,
        store_path: Optional[str] = None,
        embedding_dim: int = _DEFAULT_EMBEDDING_DIM,
        register_atexit_cleanup: bool = True,
    ) -> None:
        self._embed_fn: EmbedFn = embed_fn or _default_embed_fn
        self._embedding_dim: int = embedding_dim

        if store_path is None:
            store_path = tempfile.mkdtemp(prefix="ailienant_tool_rag_")
            if register_atexit_cleanup:
                atexit.register(shutil.rmtree, store_path, ignore_errors=True)
        self._store_path: str = store_path

        self._db = lancedb.connect(self._store_path)
        self._schema = pa.schema(
            [
                pa.field("name", pa.string()),
                pa.field("description", pa.string()),
                pa.field("json_schema", pa.string()),
                pa.field("privilege_tier", pa.string()),
                pa.field("allowed_roles_csv", pa.string()),
                pa.field("vector", pa.list_(pa.float32(), embedding_dim)),
            ]
        )

        existing_tables = (
            self._db.table_names()
            if hasattr(self._db, "table_names") and not hasattr(self._db, "list_tables")
            else self._db.list_tables()
        )
        if _LANCE_TABLE_NAME in existing_tables:
            self._table = self._db.open_table(_LANCE_TABLE_NAME)
        else:
            self._table = self._db.create_table(_LANCE_TABLE_NAME, schema=self._schema)

    # -------- public API ----------------------------------------------------

    async def register_schema(self, schema: ToolSchema) -> None:
        """Insert or replace a tool schema. Idempotent on `schema.name`."""
        embedding = schema.embedding
        if embedding is None:
            embedding = await self._embed_fn(schema.description)
        if len(embedding) != self._embedding_dim:
            raise ValueError(
                f"register_schema: embedding dim mismatch for {schema.name!r} "
                f"(got {len(embedding)}, expected {self._embedding_dim})"
            )

        # Idempotent UPSERT: delete-then-insert is the safest path on lancedb 0.x.
        # Use parameterised-style escaping; names are ASCII identifiers in
        # practice but quote-escape defensively.
        escaped_name = schema.name.replace("'", "''")
        try:
            self._table.delete(f"name = '{escaped_name}'")
        except Exception:  # noqa: BLE001 — delete on non-existent row is benign
            pass

        row = {
            "name": schema.name,
            "description": schema.description,
            "json_schema": schema.json_schema,
            "privilege_tier": schema.privilege_tier.value,
            "allowed_roles_csv": _roles_to_csv(schema.allowed_roles),
            "vector": list(embedding),
        }
        self._table.add([row])

    def all_schemas(self) -> List[ToolSchema]:
        """Snapshot of every registered schema (embedding stripped)."""
        rows = self._table.to_arrow().to_pylist()
        return [self._row_to_schema(r, include_embedding=False) for r in rows]

    async def select_tools(
        self,
        intent: str,
        *,
        k: int = TOOL_RAG_TOP_K,
        active_role: str,
        session_mode: SessionPermissionMode,
    ) -> List[ToolSchema]:
        """Return ≤ k schemas filtered by RBAC + session matrix, ranked by intent."""
        # 1. Full catalog into RAM (catalog is small — Flag A in plan).
        all_rows: List[Dict[str, Any]] = list(self._table.to_arrow().to_pylist())
        if not all_rows:
            return []

        # 2. RBAC pre-filter.
        rbac_rows = [
            r for r in all_rows if active_role in _roles_from_csv(r["allowed_roles_csv"])
        ]
        # 3. Session-mode pre-filter (PLAN allows only READ_ONLY).
        if session_mode is SessionPermissionMode.PLAN:
            rbac_rows = [
                r
                for r in rbac_rows
                if r["privilege_tier"] == ToolPrivilegeTier.READ_ONLY.value
            ]
        if not rbac_rows:
            return []
        surviving_names = {r["name"] for r in rbac_rows}

        # 4. Embed intent and run a vector search (over-fetch then intersect).
        query_vector = await self._embed_fn(intent)
        search_limit = max(k * 4, 32)
        search_rows = (
            self._table.search(list(query_vector)).limit(search_limit).to_list()
        )
        # Intersect with the RBAC/session survivors.
        ranked_rows = [r for r in search_rows if r["name"] in surviving_names]

        # If vector search returned nothing in the survivor set, fall back to
        # alphabetical order over the survivors (still deterministic).
        if not ranked_rows:
            ranked_rows = sorted(rbac_rows, key=lambda r: r["name"])
            # Fabricate a neutral distance so the sort key below works.
            for r in ranked_rows:
                r.setdefault("_distance", 0.0)

        # 5. Deterministic sort (Flag B in plan): ascending _distance, then name.
        ranked_rows.sort(key=lambda r: (float(r.get("_distance", 0.0)), r["name"]))

        # Truncate to k.
        top = ranked_rows[:k]

        # 6. READ_ONLY guarantee — swap in the best READ_ONLY survivor if absent.
        if not any(
            r["privilege_tier"] == ToolPrivilegeTier.READ_ONLY.value for r in top
        ):
            read_only_survivors = [
                r
                for r in ranked_rows
                if r["privilege_tier"] == ToolPrivilegeTier.READ_ONLY.value
            ]
            if read_only_survivors:
                # ranked_rows is already sorted, so read_only_survivors[0] is best.
                best_read_only = read_only_survivors[0]
                # Replace the worst-scoring (last) non-read-only in top.
                top = top[:-1] + [best_read_only]
                # Re-sort to preserve determinism.
                top.sort(key=lambda r: (float(r.get("_distance", 0.0)), r["name"]))

        return [self._row_to_schema(r, include_embedding=False) for r in top]

    def clear(self) -> None:
        """Drop and recreate the underlying table. Test helper."""
        existing_tables = (
            self._db.table_names()
            if hasattr(self._db, "table_names") and not hasattr(self._db, "list_tables")
            else self._db.list_tables()
        )
        if _LANCE_TABLE_NAME in existing_tables:
            self._db.drop_table(_LANCE_TABLE_NAME)
        self._table = self._db.create_table(_LANCE_TABLE_NAME, schema=self._schema)

    # -------- static metrics helper ----------------------------------------

    @staticmethod
    def prompt_size_metrics(
        eager: List[ToolSchema], selected: List[ToolSchema]
    ) -> Dict[str, float]:
        """Compute {eager_size, selected_size, reduction_ratio} over JSON schemas."""
        eager_size = float(sum(len(s.json_schema) for s in eager))
        selected_size = float(sum(len(s.json_schema) for s in selected))
        if eager_size <= 0.0:
            reduction = 0.0
        else:
            reduction = max(0.0, 1.0 - (selected_size / eager_size))
        return {
            "eager_size": eager_size,
            "selected_size": selected_size,
            "reduction_ratio": reduction,
        }

    # -------- internals ----------------------------------------------------

    def _row_to_schema(
        self, row: Dict[str, Any], *, include_embedding: bool
    ) -> ToolSchema:
        return ToolSchema(
            name=row["name"],
            description=row["description"],
            json_schema=row["json_schema"],
            privilege_tier=ToolPrivilegeTier(row["privilege_tier"]),
            allowed_roles=_roles_from_csv(row["allowed_roles_csv"]),
            embedding=list(row["vector"]) if include_embedding else None,
        )


# =====================================================================
# 5. Module-level singleton (convenience handle for non-test callers)
# =====================================================================

tool_rag_store: ToolRAGStore = ToolRAGStore()
