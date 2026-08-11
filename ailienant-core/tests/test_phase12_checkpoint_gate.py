# tests/test_phase12_checkpoint_gate.py
"""Pre-Launch Innovation Sprint — Phase 12 Checkpoint Gate.

Test-only certification that Phase 12's cross-cutting invariants hold together
against their shipped entry points, following the sibling-gate convention
(one row per load-bearing guarantee, re-invoking production code rather than
re-deriving it, never re-running a sub-phase's own unit suite). It modifies no
production logic. Heavy engines (LLM calls, LanceDB, Docker) are never spun up —
each row either calls a pure/sync production function directly, drives a real
but cheap code path (tiny fixture files for the AST parser), or performs a
source-level structural check where driving the full async node would require
disproportionate state scaffolding for what the row certifies.

Rows certified here:
  PC1     the system-message HEAD (12.1) is byte-identical across repeated
          calls with the same agent identity and carries no per-turn nonce
  PC2     the boundary declaration is fresh per turn and never leaks into the
          static HEAD it is appended alongside
  TS1     tool state-promotion (12.3) is allowlisted and checks the parse-size
          ceiling BEFORE ever calling json.loads
  SB1     container leases are keyed by (mount root, session) (12.6), not a
          single global slot
  SB2     pool exhaustion refuses to share a lease across mount roots — it
          fails closed rather than executing against the wrong project
  CG1     the coder's grounding pre-pass (12.7) is tier-filtered to READ_ONLY
          before any tool schema is ever selected
  CG2     a HITL-approved deferred tool call resolves the SAME tool by exact
          name — never by re-running the intent-ranked selector
  GR1     deep_parse's read/parse cap (12.11) bounds the parsed-file count but
          never shrinks target_files, so coverage_ratio reports truncation
          rather than hiding it
  DC1     the deleted dead context-assembler (12.12) has zero surviving
          references — its module, its sole caller, and the state-channel
          property that only that caller read
  CH1     symbol_chunk_embeddings (12.13) is an additive table, distinct from
          the pre-existing workspace_embeddings table, and both are covered by
          the janitor's GC sweep
  CH2     chunk-vector reuse is keyed on a sha256 of the chunk's own text —
          two chunks at different (qualified_name, start_line) with identical
          bodies collide on purpose; two different bodies never do
  OS1     the agentic cell's orphan-session sweep (12.14) actually closes an
          orphaned session, and main.py's WS-disconnect hook is wired to call it
  RG1     the autouse response_cache-clearing fixture (12.14/DEBT-153) exists
          and is registered autouse in conftest.py
  CI1     both CI gate workflows (12.15) parse as valid YAML and declare a job
  LANG1   no Spanish survives in the Phase-12-touched production files
          (CLAUDE.md §13.3) — regression guard for the E3 translation pass
"""
from __future__ import annotations

import inspect
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict

import pytest

# =====================================================================
# PC1 / PC2 — cacheable system-prompt prefix (12.1)
# =====================================================================


def test_pc1_static_identity_prompt_byte_identical_and_nonce_free() -> None:
    from agents.prompts import build_static_identity_prompt
    from shared.rbac import PLANNER_IDENTITY

    first = build_static_identity_prompt(agent_identity=PLANNER_IDENTITY)
    second = build_static_identity_prompt(agent_identity=PLANNER_IDENTITY)
    assert first == second, "the system-message HEAD must be byte-identical across calls"
    # A byte-identical HEAD is only cacheable if it never embeds a per-call
    # random boundary token — that value must live exclusively in the TAIL
    # produced by build_boundary_declaration().
    assert "SECURE DELIMITER FOR THIS TURN" not in first


def test_pc2_boundary_declaration_fresh_per_turn_and_never_in_head() -> None:
    from agents.prompts import build_boundary_declaration, build_static_identity_prompt
    from shared.rbac import PLANNER_IDENTITY

    decl_a = build_boundary_declaration("aaaa1111aaaa1111aaaa1111aaaa1111")
    decl_b = build_boundary_declaration("bbbb2222bbbb2222bbbb2222bbbb2222")
    assert decl_a != decl_b, "the boundary declaration must vary per turn"

    head = build_static_identity_prompt(agent_identity=PLANNER_IDENTITY)
    assert "aaaa1111aaaa1111aaaa1111aaaa1111" not in head
    assert "bbbb2222bbbb2222bbbb2222bbbb2222" not in head


# =====================================================================
# TS1 — allowlisted tool state-promotion (12.3)
# =====================================================================


def test_ts1_promote_tool_state_allowlisted_and_parse_ceiling_guarded() -> None:
    from core.tool_dispatch import promote_tool_state
    from shared.config import MAX_JSON_PARSE_CHARS

    # Non-allowlisted tool name — never promoted, regardless of payload shape.
    assert promote_tool_state("not_a_promoted_tool", '{"agent_todos": []}') is None

    # Oversized payload for an allowlisted tool: the size check must reject it
    # BEFORE any json.loads call — proven by feeding deliberately invalid JSON
    # padded past the ceiling; a function that parsed first would raise inside
    # json.loads instead of returning None cleanly.
    oversized_invalid_json = "{" + ("x" * (MAX_JSON_PARSE_CHARS + 1))
    assert promote_tool_state("todo_write", oversized_invalid_json) is None

    # Valid, allowlisted, in-bounds payload promotes cleanly.
    raw = '{"agent_todos": [{"content": "Add the retry guard", "status": "pending", "active_form": "Adding the retry guard"}]}'
    result = promote_tool_state("todo_write", raw)
    assert result is not None
    assert result["agent_todos"][0]["content"] == "Add the retry guard"


# =====================================================================
# SB1 / SB2 — sandbox container pool (12.6)
# =====================================================================


def test_sb1_lease_key_scoped_to_mount_root_and_session() -> None:
    from core.sandbox import _lease_key

    key_a = _lease_key("/workspace/project_a", "session-1")
    key_b = _lease_key("/workspace/project_a", "session-1")
    key_c = _lease_key("/workspace/project_a", "session-2")
    key_d = _lease_key("/workspace/project_b", "session-1")

    assert key_a == key_b, "the same (mount root, session) must always key to the same lease"
    assert key_a != key_c, "different sessions against the same mount must not collide"
    assert key_a != key_d, "different mount roots must not collide"


def test_sb2_pool_exhaustion_fails_closed_across_mount_roots() -> None:
    from core.sandbox import SandboxResourceExhausted, _ContainerLease, _ContainerPool

    adapter_stub = SimpleNamespace(_emit_lifecycle=lambda *args, **kwargs: None)
    pool = _ContainerPool(adapter=adapter_stub)  # type: ignore[arg-type]
    # Seed one existing lease mounted at project A only.
    lease_a = _ContainerLease(container=SimpleNamespace(), mount_root="/workspace/project_a")
    pool._leases[("/workspace/project_a", "__shared__")] = lease_a

    # A caller against project B must never silently borrow project A's
    # container — that would execute a command against the wrong project.
    with pytest.raises(SandboxResourceExhausted):
        pool._share_or_raise_locked("/workspace/project_b")

    # Same-root sharing is still the accepted degrade path.
    shared = pool._share_or_raise_locked("/workspace/project_a")
    assert shared is lease_a
    assert shared.refcount == 2


# =====================================================================
# CG1 / CG2 — coder tool-calling completion (12.7)
# =====================================================================


def test_cg1_grounding_admitted_only_when_read_only_actually_allows() -> None:
    from agents.coder import _grounding_admitted
    from core.permissions import SessionPermissionMode

    # ASK_ALL resolves READ_ONLY to HITL, and the grounding pre-pass never
    # wires an approval channel — it must decline rather than gather nothing.
    assert _grounding_admitted(SessionPermissionMode.ASK_ALL) is False
    # Every other canonical mode admits READ_ONLY unconditionally.
    assert _grounding_admitted(SessionPermissionMode.STANDARD) is True
    assert _grounding_admitted(SessionPermissionMode.FULL_AUTO) is True


def test_cg1_needs_grounding_fires_only_for_thin_context() -> None:
    from agents.coder import _needs_grounding
    from brain.state import WBSStep

    step = WBSStep(
        step_number=1, action="edit_file", target_file="a.py",
        description="add a guard", target_role="core_dev",
    )
    # No current content (new file) -> needs grounding.
    assert _needs_grounding(step, None, [("a.py", "some snippet")], {}) is True
    # Content present but RAG came back empty -> needs grounding.
    assert _needs_grounding(step, "def f(): ...", [("a.py", "")], {}) is True
    # A prior validation failure forces a fresh grounding pass.
    assert _needs_grounding(
        step, "def f(): ...", [("a.py", "snippet")], {"validation_feedback": "boom"}
    ) is True
    # Already grounded (content + real snippet + no retry) -> skip.
    assert _needs_grounding(step, "def f(): ...", [("a.py", "snippet")], {}) is False


def test_cg2_pending_tool_call_resolves_by_exact_name_never_reranked() -> None:
    from brain.agentic_cell import run_agentic_cell_node

    source = inspect.getsource(run_agentic_cell_node)
    assert "tool_rag_store.all_schemas()" in source, (
        "an approved deferred tool call must resolve against the full schema "
        "catalog by exact name, not a fresh intent-ranked select_tools() search"
    )
    assert "s.name == tool_name" in source, (
        "resolution must match the SAME tool name the operator approved, not "
        "whatever the ranker currently prefers"
    )


# =====================================================================
# GR1 — deep_parse cap never shrinks target_files (12.11)
# =====================================================================


def test_gr1_deep_parse_cap_bounds_parsed_files_not_target_files(tmp_path: Path) -> None:
    from core.memory.graphrag_extractor import GraphRAGDynamicExtractor

    files = []
    for i in range(3):
        f = tmp_path / f"mod_{i}.py"
        f.write_text(f"def func_{i}():\n    return {i}\n", encoding="utf-8")
        files.append(str(f))

    extractor = GraphRAGDynamicExtractor(project_id="gate-test")
    result = extractor._deep_parse_sync(
        target_files=files,
        workspace_root=str(tmp_path),
        max_files=1,
        token_ceiling=100_000,
    )

    assert len(result.target_files) == 3, (
        "the pre-cap neighbor set must survive uncapped so coverage_ratio's "
        "denominator reflects the real scope, not the truncated read"
    )
    assert len(result.parsed_files) <= 1, "the read/parse loop itself must respect max_files"
    assert result.truncated is True
    assert result.coverage_ratio == pytest.approx(len(result.parsed_files) / 3)


# =====================================================================
# DC1 — dead context-assembler fully deleted (12.12)
# =====================================================================


def test_dc1_deleted_context_assembler_has_no_surviving_references() -> None:
    import importlib.util

    assert importlib.util.find_spec("brain.prompt_builder") is None
    assert importlib.util.find_spec("brain.orchestrator") is None

    from core.indexer import LazyIndexer

    assert not hasattr(LazyIndexer, "progress_percentage"), (
        "progress_percentage was a dead duplicate of the IDE progress-bar "
        "formula computed independently in api/websocket_manager.py"
    )


# =====================================================================
# CH1 / CH2 — symbol-level chunk embeddings (12.13)
# =====================================================================


def test_ch1_chunk_table_additive_and_covered_by_janitor_gc() -> None:
    from core.janitor import _SYMBOL_CHUNKS_TABLE, _VECTOR_TABLES, _WORKSPACE_EMBEDDINGS_TABLE
    from core.memory.semantic_memory import _CHUNK_TABLE_NAME, _TABLE_NAME

    assert _TABLE_NAME != _CHUNK_TABLE_NAME, "the chunk table must be a distinct, additive table"
    assert _WORKSPACE_EMBEDDINGS_TABLE == _TABLE_NAME
    assert _SYMBOL_CHUNKS_TABLE == _CHUNK_TABLE_NAME
    assert _TABLE_NAME in _VECTOR_TABLES and _CHUNK_TABLE_NAME in _VECTOR_TABLES, (
        "vector GC must sweep both tables or a deleted file's chunk rows "
        "keep contributing stale retrieval evidence indefinitely"
    )


def test_ch2_chunk_reuse_keyed_on_text_hash_not_position() -> None:
    from core.memory.semantic_memory import SemanticMemoryManager

    # Two functions whose BODIES are byte-identical but which sit at different
    # (qualified_name, start_line) — the exact case a positional reuse key
    # would treat as distinct and a text-hash key must treat as the same chunk.
    body = "\n".join(f"    line_{i} = {i}" for i in range(20)) + "\n    return line_0"
    body_line_count = body.count("\n") + 1
    content = "def alpha():\n" + body + "\n\ndef beta():\n" + body + "\n"

    alpha_body_char_index = content.index(body)
    beta_body_char_index = content.index(body, alpha_body_char_index + 1)
    alpha_start_line = content[:alpha_body_char_index].count("\n") + 1
    beta_start_line = content[:beta_body_char_index].count("\n") + 1

    sym_alpha = SimpleNamespace(
        kind="function", qualified_name="mod.alpha",
        start_line=alpha_start_line, end_line=alpha_start_line + body_line_count - 1,
    )
    sym_beta = SimpleNamespace(
        kind="function", qualified_name="mod.beta",
        start_line=beta_start_line, end_line=beta_start_line + body_line_count - 1,
    )

    chunks = SemanticMemoryManager._build_chunks(content, [sym_alpha, sym_beta])
    assert len(chunks) == 2
    by_name: Dict[str, Any] = {c["qualified_name"]: c for c in chunks}

    # Identical bodies at different (qualified_name, start_line) collide on the
    # SAME content_hash — a positional key would never do this and would
    # falsely re-embed both on any unrelated shift elsewhere in the file.
    assert by_name["mod.alpha"]["content_hash"] == by_name["mod.beta"]["content_hash"]
    assert by_name["mod.alpha"]["chunk_text"] == by_name["mod.beta"]["chunk_text"]

    # A genuinely different body must never collide.
    other_body = "\n".join(f"    other_{i} = {i}" for i in range(20)) + "\n    return other_0"
    other_content = "def gamma():\n" + other_body + "\n"
    sym_gamma = SimpleNamespace(
        kind="function", qualified_name="mod.gamma", start_line=1,
        end_line=other_body.count("\n") + 2,
    )
    other_chunks = SemanticMemoryManager._build_chunks(other_content, [sym_gamma])
    assert len(other_chunks) == 1
    assert other_chunks[0]["content_hash"] != by_name["mod.alpha"]["content_hash"]


# =====================================================================
# OS1 — orphaned agentic-cell session sweep wired (12.14)
# =====================================================================


class _AsyncNoop:
    """Callable async stub that records whether it was invoked."""

    def __init__(self) -> None:
        self.called = False

    async def __call__(self, *args: Any, **kwargs: Any) -> None:
        self.called = True


def test_os1_sweep_orphaned_sessions_closes_a_real_orphan() -> None:
    import asyncio

    import brain.agentic_cell as ac

    async def _run() -> None:
        # _session_registry is a process-wide module global several other test
        # files (agentic-cell lifecycle / dispatcher / PTY suites) write into
        # directly with no autouse reset — a full-suite run can reach this test
        # with residual entries already present. Snapshot-clear-restore keeps
        # this row's swept-count assertion exact regardless of run order,
        # without touching the production registry's lifecycle semantics.
        saved = dict(ac._session_registry)
        ac._session_registry.clear()
        try:
            fake_session = SimpleNamespace(close=_AsyncNoop())
            fake_cell = SimpleNamespace(collector=None, session=fake_session)
            ac._session_registry["orphan-task"] = fake_cell  # type: ignore[assignment]

            swept = await ac.sweep_orphaned_sessions(live_task_ids=[])
            assert swept == 1
            assert "orphan-task" not in ac._session_registry
            assert fake_session.close.called
        finally:
            ac._session_registry.clear()
            ac._session_registry.update(saved)

    asyncio.run(_run())


def test_os1_main_wires_the_ws_disconnect_hook_to_the_sweep() -> None:
    main_source = (Path(__file__).parent.parent / "main.py").read_text(encoding="utf-8")
    assert "from brain.agentic_cell import sweep_orphaned_sessions" in main_source
    assert "_register_session_cleanup_hook(_sweep_orphaned_cells_on_disconnect)" in main_source


# =====================================================================
# RG1 — autouse response_cache reset fixture (12.14 / DEBT-153)
# =====================================================================


def test_rg1_response_cache_autouse_fixture_registered() -> None:
    conftest_source = (Path(__file__).parent / "conftest.py").read_text(encoding="utf-8")
    assert "@pytest.fixture(autouse=True)" in conftest_source
    assert "def _reset_response_cache" in conftest_source
    assert "response_cache.clear()" in conftest_source


# =====================================================================
# CI1 — both gate workflows parse and declare a job (12.15)
# =====================================================================


def test_ci1_backend_and_frontend_workflows_parse_and_declare_jobs() -> None:
    yaml = pytest.importorskip("yaml")
    repo_root = Path(__file__).parent.parent.parent
    backend = yaml.safe_load(
        (repo_root / ".github" / "workflows" / "backend-gate.yml").read_text(encoding="utf-8")
    )
    frontend = yaml.safe_load(
        (repo_root / ".github" / "workflows" / "frontend-gate.yml").read_text(encoding="utf-8")
    )
    assert backend["jobs"], "backend-gate.yml must declare at least one job"
    assert frontend["jobs"], "frontend-gate.yml must declare at least one job"


# =====================================================================
# LANG1 — no Spanish survives in the files this phase translated (§13.3)
# =====================================================================

_LANG1_BACKEND_FILES = (
    "main.py",
    "shared/token_counter.py",
    "agents/planner.py",
    "core/task_service.py",
    "brain/state.py",
    "api/api_contracts.py",
)
_LANG1_FRONTEND_FILES = (
    "../ailienant-extension/src/editor/vfs_reader.ts",
    "../ailienant-extension/src/api/api_client.ts",
    "../ailienant-extension/src/api/ws_client.ts",
    "../ailienant-extension/src/brain/session.ts",
)
_LANG1_ACCENT_CHARS = "áéíóúñÁÉÍÓÚÑ"


def test_lang1_no_spanish_in_translated_production_files() -> None:
    core_root = Path(__file__).parent.parent
    offenders = []
    for rel in (*_LANG1_BACKEND_FILES, *_LANG1_FRONTEND_FILES):
        path = core_root / rel
        text = path.read_text(encoding="utf-8")
        for lineno, line in enumerate(text.splitlines(), start=1):
            if any(ch in line for ch in _LANG1_ACCENT_CHARS):
                offenders.append(f"{rel}:{lineno}: {line.strip()[:80]}")
    assert not offenders, "Spanish reintroduced into production code:\n" + "\n".join(offenders)
