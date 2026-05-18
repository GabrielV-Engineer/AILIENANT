# ailienant-core/tests/test_phase5_7_checkpoint_gate.py
#
# Phase 5.7 Checkpoint Gate — Adversarial E2E audit suite.
#
# Seven mocked-boundary tests prove the Phase 5.1–5.6 systemic guarantees
# hold under attack. No production code (core/, tools/, brain/) is touched
# by this module — per the Zero-Trust Immutability directive, the suite
# only IMPORTS and INVOKES existing entry points.
#
#   A1/A2 — RBWE rejects WRITE-tier tools targeting unread files (0 writes).
#   B1    — Tool RAG selection yields >=70% JSON-payload reduction.
#   C1/C2 — AST validation blocks unparseable patches before vfs_write.
#   D1    — DANGEROUS_COMMANDS_REGEX intercepts before subprocess spawn.
#   D2    — AskUserQuestionTool populates state['pending_hitl_request'].

from __future__ import annotations

import hashlib
import struct
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock, patch

import pytest

from core.permissions import (
    PermissionDeniedError,
    SessionPermissionMode,
    ToolPrivilegeTier,
    rbwe_guard,
)
from core.tool_rag import TOOL_RAG_TOP_K, ToolRAGStore
from tools.control_tools import AskUserQuestionTool, register_control_tools
from tools.execution_tools import SandboxBashTool, register_execution_tools
from tools.mutation_tools import (
    AtomicCodePatchTool,
    FileWriteTool,
    register_mutation_tools,
)
from tools.perception_tools import register_perception_tools


# =====================================================================
# Shared helper — deterministic SHA256-based fake embeddings.
# Copied (not imported) from tests/test_execution_tools.py:244-261.
# =====================================================================


def _isolated_store(tmp_path: Path) -> ToolRAGStore:
    async def fake_embed(text: str) -> List[float]:
        digest = hashlib.sha256(text.encode("utf-8")).digest()
        floats: List[float] = []
        for i in range(8):
            chunk = digest[(i * 4) % len(digest) : (i * 4) % len(digest) + 4]
            if len(chunk) < 4:
                chunk = (chunk + b"\x00\x00\x00\x00")[:4]
            (val,) = struct.unpack("<f", chunk)
            floats.append(max(-1e3, min(1e3, val)))
        return floats

    return ToolRAGStore(
        embed_fn=fake_embed,
        store_path=str(tmp_path / "tool_rag"),
        embedding_dim=8,
        register_atexit_cleanup=False,
    )


# =====================================================================
# Task A — RBWE / Zero-Trust containment
# =====================================================================


@pytest.mark.anyio
async def test_rbwe_blocks_atomic_code_patch_on_unread_file() -> None:
    """A1: AtomicCodePatchTool against an unread path -> guard raises, 0 writes."""
    state: Dict[str, Any] = {"read_files_state": {}}
    vfs_write_mock = MagicMock()
    tool = AtomicCodePatchTool(
        vfs_read=lambda _p: "def x():\n    return 1\n",
        vfs_write=vfs_write_mock,
    )

    with pytest.raises(PermissionDeniedError) as excinfo:
        rbwe_guard(
            tool_name="atomic_code_patch",
            tool_tier=ToolPrivilegeTier.WRITE,
            target_path="/critical/sys.py",
            state=state,
        )
        # Orchestrator would dispatch _arun here; we MUST NOT reach this line.
        await tool._arun(
            file_path="/critical/sys.py",
            search_block="def x():\n    return 1",
            replace_block="def x():\n    return 2",
        )

    msg = str(excinfo.value)
    assert "RBWE" in msg or "never read" in msg
    assert excinfo.value.tool_name == "atomic_code_patch"
    assert excinfo.value.target_path == "/critical/sys.py"
    assert vfs_write_mock.call_count == 0


@pytest.mark.anyio
async def test_rbwe_blocks_file_write_on_unread_file() -> None:
    """A2: FileWriteTool against an unread path -> guard raises, 0 writes."""
    state: Dict[str, Any] = {"read_files_state": {}}
    vfs_write_mock = MagicMock()
    tool = FileWriteTool(
        vfs_read=lambda _p: None,
        vfs_write=vfs_write_mock,
    )

    with pytest.raises(PermissionDeniedError) as excinfo:
        rbwe_guard(
            tool_name="file_write",
            tool_tier=ToolPrivilegeTier.WRITE,
            target_path="/critical/sys.py",
            state=state,
        )
        await tool._arun(
            file_path="/critical/sys.py",
            content="def x():\n    return 2\n",
        )

    msg = str(excinfo.value)
    assert "RBWE" in msg or "never read" in msg
    assert vfs_write_mock.call_count == 0


# =====================================================================
# Task B — Tool RAG Financial Audit (>=70% payload reduction)
# =====================================================================


@pytest.mark.anyio
async def test_tool_rag_selection_yields_70pct_payload_reduction(
    tmp_path: Path,
) -> None:
    """B1: full Phase-5 schema set; QA intent; reduction_ratio MUST be >=0.70.

    Per locked D5 guardrail: if this asserts at <0.70, the corrective action
    is to compress verbose `description=` strings in the tool Pydantic schemas
    — NOT to lower this threshold or shrink TOOL_RAG_TOP_K.
    """
    store = _isolated_store(tmp_path)
    await register_perception_tools(store)
    await register_mutation_tools(store)
    await register_execution_tools(store)
    await register_control_tools(store)

    eager = store.all_schemas()
    assert len(eager) >= 14, f"expected >=14 schemas across Phase 5; got {len(eager)}"

    selected = await store.select_tools(
        intent="Run the test suite and check linting",
        k=TOOL_RAG_TOP_K,
        active_role="core_dev",
        session_mode=SessionPermissionMode.DEFAULT,
    )
    selected_names = {s.name for s in selected}
    assert selected_names & {"sandbox_bash", "check_type_integrity"}, (
        "QA-intent selection should surface at least one of "
        "{sandbox_bash, check_type_integrity}; got "
        f"{selected_names!r}"
    )

    metrics = ToolRAGStore.prompt_size_metrics(eager, selected)
    assert metrics["reduction_ratio"] >= 0.70, (
        "Phase 5 financial guarantee not met: "
        f"reduction_ratio={metrics['reduction_ratio']:.3f}, "
        f"eager_size={metrics['eager_size']:.0f}, "
        f"selected_size={metrics['selected_size']:.0f}. "
        "Remedy: compress tool `description=` strings (D5 guardrail) — "
        "do NOT lower this assertion."
    )


# =====================================================================
# Task C — AST Malicious Patch Containment
# =====================================================================


@pytest.mark.anyio
async def test_atomic_patch_ast_failure_blocks_vfs_write() -> None:
    """C1: replace_block produces unparseable code -> ERROR returned, 0 writes."""
    storage: Dict[str, str] = {"core/sys.py": "def x():\n    return 1\n"}

    def vfs_read(path: str) -> Optional[str]:
        return storage.get(path)

    vfs_write_mock = MagicMock()
    tool = AtomicCodePatchTool(vfs_read=vfs_read, vfs_write=vfs_write_mock)

    out = await tool._arun(
        file_path="core/sys.py",
        search_block="def x():\n    return 1",
        replace_block="def x():\n    return (",  # unclosed paren — ast.parse rejects
    )

    assert "ERROR" in out
    assert "AST" in out or "SyntaxError" in out
    assert vfs_write_mock.call_count == 0
    assert storage["core/sys.py"] == "def x():\n    return 1\n"  # untouched


@pytest.mark.anyio
async def test_file_write_ast_failure_blocks_vfs_write() -> None:
    """C2: FileWriteTool content fails AST -> ERROR returned, 0 writes."""
    vfs_write_mock = MagicMock()
    tool = FileWriteTool(
        vfs_read=lambda _p: None,
        vfs_write=vfs_write_mock,
    )

    out = await tool._arun(
        file_path="broken.py",
        content="def broken(:\n    return\n",  # syntactically invalid
    )

    assert "ERROR" in out
    assert "AST" in out or "SyntaxError" in out
    assert vfs_write_mock.call_count == 0


# =====================================================================
# Task D — HITL Asymmetric Friction (combined E2E flow)
# =====================================================================


@pytest.mark.anyio
async def test_sandbox_bash_dangerous_command_never_spawns_subprocess() -> None:
    """D1: rm -rf is regex-intercepted; asyncio.create_subprocess_shell never fires."""
    tool = SandboxBashTool()
    spawn_calls: List[Any] = []

    async def _exploding_spawn(*args: Any, **kwargs: Any) -> Any:
        spawn_calls.append((args, kwargs))
        raise AssertionError("Subprocess MUST NOT spawn on DANGEROUS pattern.")

    with patch(
        "tools.execution_tools.asyncio.create_subprocess_shell",
        side_effect=_exploding_spawn,
    ):
        out = await tool._arun(command="rm -rf node_modules")

    assert "DANGEROUS_COMMAND_INTERCEPTED" in out
    assert "ask_user_question" in out  # sentinel advises the next move
    assert spawn_calls == []


@pytest.mark.anyio
async def test_ask_user_question_populates_pending_hitl_request() -> None:
    """D2: continues D1's flow — HITL escalation populates pending_hitl_request."""
    state: Dict[str, Any] = {}
    tool = AskUserQuestionTool(state=state)

    out = await tool._arun(
        question="Approve `rm -rf node_modules` to reset the workspace?",
        context="Sandbox bash refused; awaiting human override.",
    )

    assert "HITL_PENDING:" in out
    req = state["pending_hitl_request"]
    assert req["kind"] == "ASK_USER_QUESTION"
    assert len(req["request_id"]) == 32  # uuid4().hex
    assert req["question"].startswith("Approve")
    # Sentinel string and state entry share the same request_id.
    assert out.endswith(req["request_id"])


# =====================================================================
# anyio backend constraint
# =====================================================================


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"
