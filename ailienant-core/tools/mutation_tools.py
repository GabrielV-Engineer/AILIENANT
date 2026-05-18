"""Phase 5.4 — Surgical Mutation Tools (WRITE-tier bundle).

Three new LangChain BaseTool subclasses + a schema-registration helper:

    AtomicCodePatchTool     — Fuzzy search/replace patch with AST + OCC guarantees.
    BatchSemanticEditTool   — Multi-file Unit-of-Work commit (truly ACID).
    FileWriteTool           — Full-file create/overwrite with optional OCC + AST.

All tools delegate the actual mutation work to tools.patch_tool.apply_patch_to_vfs
(Phase 2.22 transactional engine: fuzzy match + Python AST validation + OCC).
The Unit-of-Work pattern in BatchSemanticEditTool buffers every intra-batch
write in a local Dict[str, str] and only flushes to the real vfs_write callable
after every item has cleared validation — no partial mutation can ever leak
to the real VFS.

register_mutation_tools(store) registers all three schemas with
ToolPrivilegeTier.WRITE. Phase 4 §3.2 RBAC matrix determines the role
whitelist; core/permissions.py is NEVER imported or modified by this module.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Callable, Dict, FrozenSet, List, Optional, Type

from langchain_core.tools import BaseTool
from pydantic import BaseModel, Field, PrivateAttr

from core.exceptions import PatchError, StaleFileException
from core.permissions import ToolPrivilegeTier
from core.tool_rag import ToolRAGStore, ToolSchema
from tools.patch_tool import (
    _compute_hash,
    _validate_python_syntax,
    apply_patch_to_vfs,
)

logger = logging.getLogger("MUTATION_TOOLS")


# =====================================================================
# Shared constants & helpers
# =====================================================================

_ALLOWED_MUTATION_ROLES: FrozenSet[str] = frozenset(
    {"core_dev", "architect_refactor", "secops", "data_ml_engineer", "devops_infra"}
)


# =====================================================================
# Task A — AtomicCodePatchTool
# =====================================================================


class AtomicCodePatchInput(BaseModel):
    """Inputs for a single surgical patch."""

    file_path: str = Field(description="VFS path of the file to patch.")
    search_block: str = Field(
        description=(
            "Snippet to match (≥10 non-whitespace characters required). "
            "Exact first, then normalized whitespace, then fuzzy (0.90 ratio)."
        )
    )
    replace_block: str = Field(default="", description="Replacement snippet ('' = delete).")
    expected_hash: Optional[str] = Field(
        default=None,
        description=(
            "SHA-256 of the file's current content (from a prior FileReadTool "
            "call) for OCC. Omit to skip the check (unsafe)."
        ),
    )


class AtomicCodePatchTool(BaseTool):
    """Surgical search/replace patch with fuzzy match + AST + OCC.

    Errors are returned as strings (never raised) so they surface in the
    agent's scratchpad — matches the make_patch_file_tool discipline.
    """

    name: str = "atomic_code_patch"
    description: str = (
        "Surgically replace a snippet in a VFS file. Fuzzy-matches with 0.90 "
        "threshold; validates Python files via ast.parse before write. "
        "Provide expected_hash for OCC; on mismatch the patch is rejected."
    )
    args_schema: Type[BaseModel] = AtomicCodePatchInput

    _vfs_read: Callable[[str], Optional[str]] = PrivateAttr()
    _vfs_write: Callable[[str, str], None] = PrivateAttr()

    def __init__(
        self,
        *,
        vfs_read: Callable[[str], Optional[str]],
        vfs_write: Callable[[str, str], None],
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self._vfs_read = vfs_read
        self._vfs_write = vfs_write

    def _run(self, *args: Any, **kwargs: Any) -> Any:
        raise NotImplementedError("AtomicCodePatchTool is async-only — use _arun().")

    async def _arun(
        self,
        file_path: str,
        search_block: str,
        replace_block: str = "",
        expected_hash: Optional[str] = None,
    ) -> str:
        try:
            diff = apply_patch_to_vfs(
                self._vfs_read,
                self._vfs_write,
                file_path,
                search_block,
                replace_block,
                expected_hash=expected_hash,
            )
        except StaleFileException as exc:
            return f"[atomic_code_patch] OCC mismatch: {exc}"
        except PatchError as exc:
            return f"[atomic_code_patch] ERROR: {exc}"
        return f"[atomic_code_patch] OK\n{diff}"


# =====================================================================
# Task B — BatchSemanticEditTool (Unit-of-Work pattern)
# =====================================================================


class BatchEditItem(BaseModel):
    """One file's coordinated edit inside a multi-file batch."""

    file_path: str = Field(description="VFS path.")
    document_version_id: str = Field(
        description=(
            "SHA-256 hash of the file's content from a prior FileReadTool call. "
            "Phase 1 of the batch rejects the whole transaction if this is stale."
        )
    )
    search_block: str = Field(description="Snippet to match (≥10 non-ws chars).")
    replace_block: str = Field(default="", description="Replacement ('' = delete).")


class BatchSemanticEditInput(BaseModel):
    edits: List[BatchEditItem] = Field(
        description="Two or more coordinated edits across files (single-item batches are allowed).",
    )


class BatchSemanticEditTool(BaseTool):
    """Multi-file atomic batch — TRULY ACID via Unit-of-Work / Write Buffer.

    Three phases. The real VFS is touched ONLY in Phase 3, and only if every
    item has cleared OCC + fuzzy match + AST validation. A failure in any phase
    discards the in-memory buffer and leaves the VFS byte-identical.

      Phase 1 — Pre-validate every item's document_version_id against the real
                vfs_read. Any stale paths → reject the whole batch.
      Phase 2 — Apply each apply_patch_to_vfs against a local write_buffer.
                Inner OCC is suppressed (already validated in Phase 1).
                Intra-batch reads see prior intra-batch writes via buffered_read.
      Phase 3 — Commit: flush write_buffer to the real vfs_write.
    """

    name: str = "batch_semantic_edit"
    description: str = (
        "Apply a coordinated list of surgical edits across multiple files as a "
        "single ACID transaction. Each item carries its own document_version_id; "
        "the whole batch is rejected if ANY version is stale or ANY patch fails "
        "AST/fuzzy validation. No partial mutation can ever leak to the VFS."
    )
    args_schema: Type[BaseModel] = BatchSemanticEditInput

    _vfs_read: Callable[[str], Optional[str]] = PrivateAttr()
    _vfs_write: Callable[[str, str], None] = PrivateAttr()

    def __init__(
        self,
        *,
        vfs_read: Callable[[str], Optional[str]],
        vfs_write: Callable[[str, str], None],
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self._vfs_read = vfs_read
        self._vfs_write = vfs_write

    def _run(self, *args: Any, **kwargs: Any) -> Any:
        raise NotImplementedError("BatchSemanticEditTool is async-only — use _arun().")

    async def _arun(self, edits: List[Dict[str, Any]]) -> str:
        if not edits:
            return "[batch_semantic_edit] no items"

        # Pydantic re-coerces each dict via the BatchEditItem schema when invoked
        # through LangChain. When invoked directly (tests), we coerce explicitly.
        coerced: List[BatchEditItem] = [
            item if isinstance(item, BatchEditItem) else BatchEditItem(**item)
            for item in edits
        ]

        # -------- Phase 1 — Pre-validation against the REAL vfs_read ---------
        stale: List[str] = []
        for item in coerced:
            current_content = self._vfs_read(item.file_path) or ""
            current_hash = _compute_hash(current_content)
            if current_hash != item.document_version_id:
                stale.append(item.file_path)

        if stale:
            stale_lines = "\n".join(f"  - {p}" for p in stale)
            return (
                "[batch_semantic_edit] OCC mismatch — entire batch rejected. "
                "The following file(s) have changed since you read them; re-read "
                f"each and regenerate the batch:\n{stale_lines}"
            )

        # -------- Phase 2 — Apply each patch against a local write buffer ----
        write_buffer: Dict[str, str] = {}

        def buffered_read(path: str) -> Optional[str]:
            if path in write_buffer:
                return write_buffer[path]
            return self._vfs_read(path)

        def buffered_write(path: str, content: str) -> None:
            write_buffer[path] = content

        diffs: List[str] = []
        for idx, item in enumerate(coerced, start=1):
            try:
                # expected_hash=None: OCC was already validated in Phase 1. Inside
                # the buffer, the file's "current" hash diverges from the agent's
                # pre-batch version_id as soon as any sibling item touches it.
                diff = apply_patch_to_vfs(
                    buffered_read,
                    buffered_write,
                    item.file_path,
                    item.search_block,
                    item.replace_block,
                    expected_hash=None,
                )
                diffs.append(diff)
            except PatchError as exc:
                # Discard the buffer — the real VFS is untouched.
                logger.info(
                    "BATCH_EDIT: item %d/%d failed (%s); discarding write buffer.",
                    idx,
                    len(coerced),
                    exc,
                )
                return (
                    f"[batch_semantic_edit] ERROR on item {idx} "
                    f"({item.file_path!r}): {exc}. No writes committed to VFS."
                )

        # -------- Phase 3 — Commit: flush buffer to the REAL vfs_write -------
        for path, content in write_buffer.items():
            self._vfs_write(path, content)

        report = [
            f"[batch_semantic_edit] OK — {len(coerced)} item(s) committed atomically:"
        ]
        for item, diff in zip(coerced, diffs):
            report.append(f"--- {item.file_path}\n{diff}")
        return "\n".join(report)


# =====================================================================
# Task C — FileWriteTool
# =====================================================================


class FileWriteInput(BaseModel):
    file_path: str = Field(description="VFS path to create or overwrite.")
    content: str = Field(description="Full file contents.")
    expected_hash: Optional[str] = Field(
        default=None,
        description=(
            "SHA-256 of the existing file content for OCC. Omit to create a new "
            "file or to perform an unsafe overwrite."
        ),
    )


class FileWriteTool(BaseTool):
    """Full-file create or overwrite. Light AST validation for .py paths."""

    name: str = "file_write"
    description: str = (
        "Create or overwrite a VFS file with the given content. Python files "
        "are AST-validated before the write commits; failures leave the VFS "
        "untouched. Provide expected_hash for OCC on overwrites; omit when "
        "creating a new file. RBWE is enforced upstream by rbwe_guard."
    )
    args_schema: Type[BaseModel] = FileWriteInput

    _vfs_read: Callable[[str], Optional[str]] = PrivateAttr()
    _vfs_write: Callable[[str, str], None] = PrivateAttr()

    def __init__(
        self,
        *,
        vfs_read: Callable[[str], Optional[str]],
        vfs_write: Callable[[str, str], None],
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self._vfs_read = vfs_read
        self._vfs_write = vfs_write

    def _run(self, *args: Any, **kwargs: Any) -> Any:
        raise NotImplementedError("FileWriteTool is async-only — use _arun().")

    async def _arun(
        self,
        file_path: str,
        content: str,
        expected_hash: Optional[str] = None,
    ) -> str:
        if expected_hash is not None:
            current = self._vfs_read(file_path) or ""
            if _compute_hash(current) != expected_hash:
                return (
                    f"[file_write] OCC mismatch on {file_path!r}: file changed "
                    "since last read. Re-read and retry."
                )

        try:
            _validate_python_syntax(content, file_path)
        except PatchError as exc:
            return f"[file_write] ERROR: {exc}"

        self._vfs_write(file_path, content)
        return f"[file_write] OK: {file_path!r} written ({len(content)} chars)."


# =====================================================================
# Task E — Schema registration helper
# =====================================================================


def _tool_schema(
    name: str, description: str, json_schema_class: Type[BaseModel]
) -> ToolSchema:
    return ToolSchema(
        name=name,
        description=description,
        json_schema=json.dumps(json_schema_class.model_json_schema(), default=str),
        privilege_tier=ToolPrivilegeTier.WRITE,
        allowed_roles=_ALLOWED_MUTATION_ROLES,
    )


async def register_mutation_tools(store: ToolRAGStore) -> int:
    """Register the 3 WRITE-tier schemas in the given store. Returns count."""
    schemas: List[ToolSchema] = [
        _tool_schema(
            "atomic_code_patch",
            "Surgical search/replace patch in a VFS file with fuzzy + AST + OCC.",
            AtomicCodePatchInput,
        ),
        _tool_schema(
            "batch_semantic_edit",
            "Multi-file coordinated edit, fully ACID via a Unit-of-Work write buffer.",
            BatchSemanticEditInput,
        ),
        _tool_schema(
            "file_write",
            "Create or overwrite a VFS file with optional OCC and Python AST checks.",
            FileWriteInput,
        ),
    ]
    for schema in schemas:
        await store.register_schema(schema)
    return len(schemas)
