# tests/test_phase7_18_checkpoint_gate.py
"""Six-Technique Enterprise Hardening Sweep — backend Checkpoint Gate.

Single E2E certification that the six hardening pillars hold together against their
**shipped** entry points. Test-only: it imports and invokes production code,
asserting the one load-bearing invariant per gate row — it does not re-run the
dedicated suites, and it modifies no production logic. Mirrors the sibling
``test_phase7_15_checkpoint_gate.py`` gate.

Async cases run via ``asyncio.run`` (no anyio-backend dependency).

Gate rows certified here (backend-assertable):
  EXLOOP1 run_command dispatch reaches self-heal   EXLOOP2 budget concedes + honest deferral
  DIAG1   structured, capped diagnostics           REC1    hybrid recency ordering
  RF1     response_format strip+repair+memo         FS1    elided style skeleton in the prompt
  CACHE1  AST-hash cache hit/miss                    OCC1    reducers merge w/o loss + live anchor
  MCTS-DEFER  MCTS confined to the ReAct cell, not the single-shot spine (DEBT-009 closed)

Host-certified (out of pytest scope, per the 7.15 §5.2 precedent): the stale
``base_hash`` **rejection** itself runs in the VS Code ``applyEdit`` bridge — the
Python ``core/write_pipeline.py`` performs no disk I/O and delegates the write +
stale guard to the host, which returns ``stale_files`` / emits
``client_concurrency_conflict``. OCC1 here certifies the Python half: the reducer
merges concurrent fan-out without loss, and the anchor the host compares is sound.
"""
from __future__ import annotations

import ast
import asyncio
from contextlib import ExitStack
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest.mock import AsyncMock, MagicMock, patch

import core.sandbox as sb
from agents.coder import _build_style_block, content_hash, run_coder_node
from agents.prompts import STYLE_EXEMPLAR_HEADER
from agents.recency import compute_recency_score
from brain.engine import route_after_coder
from brain.retry_policy import CORRECTION_MAX_ATTEMPTS
from brain.state import (
    MissionSpecification,
    VFSFile,
    WBSStep,
    _merge_generated_code,
)
from core.ast_engine import extract_skeleton
from core.response_cache import SemanticResponseCache
from core.sandbox import SandboxResult
from tools.llm_gateway import LLMGateway, _RESPONSE_FORMAT_UNSUPPORTED
from tools.validation.diagnostics import _DIAG_CAP, format_diagnostics, parse_mypy

_PKG_ROOT = Path(__file__).resolve().parent.parent


# ── EXLOOP1 — a failed run_command reaches the existing self-heal edge ─────────


def test_exloop1_run_command_failure_routes_to_error_correction() -> None:
    # A non-zero sandbox exit must emit the reflexion-mimicking heal delta and the
    # existing conditional edge must carry it into self-healing — not a dead branch.
    adapter = _StubAdapter(
        [SandboxResult(exit_code=1, stdout="x.py:1: error: boom [misc]", stderr="")]
    )
    with _bind_adapter(adapter):
        result = asyncio.run(run_coder_node(_run_command_state("mypy .")))
    assert result.get("healing_required") is True
    assert route_after_coder(result) == "error_correction"


# ── EXLOOP2 — the budget concedes, and a missing adapter defers honestly ───────


def test_exloop2_budget_concedes_and_no_adapter_defers() -> None:
    # At the correction budget the loop forwards instead of spinning on heal.
    adapter = _StubAdapter(
        [SandboxResult(exit_code=1, stdout="x.py:1: error: e [misc]", stderr="")]
    )
    with _bind_adapter(adapter):
        at_budget = asyncio.run(
            run_coder_node(
                _run_command_state("mypy .", correction_attempts=CORRECTION_MAX_ATTEMPTS)
            )
        )
    assert not at_budget.get("healing_required")
    assert route_after_coder(at_budget) == "contract_guard"  # forward, not heal

    # With no resolved adapter the honest deferral is preserved (never a false pass).
    with _bind_adapter(None):
        deferred = asyncio.run(run_coder_node(_run_command_state("pytest -q")))
    assert not deferred.get("healing_required")
    assert any(
        f.startswith("EXECUTE_TIER_DEFERRED:") for f in deferred.get("security_flags", [])
    )


# ── DIAG1 — diagnostics are structured + capped, never raw stdout ──────────────


def test_diag1_diagnostics_are_structured_and_capped() -> None:
    errs = parse_mypy("main.py:42: error: Incompatible return value [return-value]")
    assert errs and errs[0].line == 42
    rendered = format_diagnostics(errs)
    assert "42" in rendered and "return-value" in rendered
    assert "Traceback" not in rendered  # distilled, not a raw trace

    flood = "\n".join(f"f.py:{i}: error: msg{i} [misc]" for i in range(5000))
    assert len(format_diagnostics(parse_mypy(flood))) <= _DIAG_CAP


# ── REC1 — hybrid recency: hot-but-old > cold-but-old; fresh > stale ───────────


def test_rec1_recency_orders_hot_and_fresh_above_cold_and_stale() -> None:
    now = datetime(2026, 6, 4, 12, 0, 0).timestamp()
    old_iso = datetime.fromtimestamp(now - 10 * 86_400).isoformat()
    fresh_iso = datetime.fromtimestamp(now - 60).isoformat()

    hot_old = compute_recency_score([old_iso], [], access_count=5, now=now)
    cold_old = compute_recency_score([old_iso], [], access_count=0, now=now)
    assert hot_old > cold_old  # the frequency term rescues a hot-but-old file

    fresh = compute_recency_score([fresh_iso], [], access_count=0, now=now)
    stale = compute_recency_score([old_iso], [], access_count=0, now=now)
    assert fresh > stale  # a fresh index beats a stale one at equal access


# ── RF1 — a response_format-rejecting backend recovers and is memoized ─────────


def test_rf1_response_format_degrades_strips_and_memoizes() -> None:
    model = "gate-provider/gate-model"
    _RESPONSE_FORMAT_UNSUPPORTED.discard(model)
    mock = AsyncMock(side_effect=_rf_rejecting)
    try:
        with patch("litellm.acompletion", new=mock):
            resp = asyncio.run(
                LLMGateway.ainvoke(
                    messages=[{"role": "user", "content": "hi"}],
                    model=model,
                    response_format={"type": "json_object"},
                )
            )
        assert resp is not None  # recovered via the param-less retry
        assert mock.await_count == 2  # failed attempt + one stripped retry
        assert model in _RESPONSE_FORMAT_UNSUPPORTED  # learned for the session
    finally:
        _RESPONSE_FORMAT_UNSUPPORTED.discard(model)


# ── FS1 — the coder prompt carries an elided same-language skeleton ────────────


def test_fs1_style_block_carries_elided_skeleton() -> None:
    block = _build_style_block("agents/widget.py", [("agents/helper.py", _PY_FUNC)])
    assert block.startswith(STYLE_EXEMPLAR_HEADER)
    assert "def greet(name: str) -> str:" in block  # signature + type hints kept
    assert "secret_body_token" not in block  # body elided, no logic leaked

    skeleton = extract_skeleton(_PY_FUNC, "python")
    assert "..." in skeleton and "secret_body_token" not in skeleton


# ── CACHE1 — identical context hits; a one-byte edit misses ────────────────────


def test_cache1_ast_hash_cache_hit_and_miss() -> None:
    cache = SemanticResponseCache()
    key = cache.build_key(
        intent="edit|m.py|add hints",
        context=[("m.py", "def f(): pass")],
        project_id="p",
        model="ailienant/big",
    )
    cache.store(key, '{"edits": []}', ["m.py"])
    assert cache.probe(key) == '{"edits": []}'  # identical intent + context → hit

    edited = cache.build_key(
        intent="edit|m.py|add hints",
        context=[("m.py", "def f(): passX")],  # one byte added
        project_id="p",
        model="ailienant/big",
    )
    assert key != edited
    assert cache.probe(edited) is None  # different content-hash → miss


# ── OCC1 — reducers merge fan-out without loss; the stale anchor is live ───────


def test_occ1_reducers_merge_without_loss_and_anchor_is_edit_sensitive() -> None:
    # Concurrent Send() fan-out to different paths: both writes survive the merge.
    merged = _merge_generated_code({"a.py": _vfs("v2")}, {"b.py": _vfs("v1")})
    assert set(merged) == {"a.py", "b.py"}

    # Same path, concurrent: the lexicographically later document_version_id wins,
    # regardless of argument order — a slower branch never clobbers a newer edit.
    newer, older = _vfs("v9"), _vfs("v1")
    assert _merge_generated_code({"a.py": older}, {"a.py": newer})["a.py"].document_version_id == "v9"
    assert _merge_generated_code({"a.py": newer}, {"a.py": older})["a.py"].document_version_id == "v9"

    # The stale-guard anchor the host compares: stable for identical pre-edit text,
    # different after a one-byte edit (so the host can detect a drifted file).
    assert content_hash("def f(): pass") == content_hash("def f(): pass")
    assert content_hash("def f(): pass") != content_hash("def f(): passX")


# ── MCTS-DEFER — MCTS confined to the ReAct cell, not the single-shot spine ────


def _module_imports(rel: str) -> List[str]:
    tree = ast.parse((_PKG_ROOT / rel).read_text(encoding="utf-8"))
    imported: List[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.append(node.module or "")
    return imported


def test_mcts_defer_no_live_loop_import_into_brain_mcts() -> None:
    # DEBT-009 closed: the agentic ReAct cell (brain/agentic_cell.py) is MCTS's sanctioned
    # live home, where it governs competing fix candidates using the structured verdict as
    # the reward. The enduring invariant is that the *cheap single-shot spine* — the
    # planner-mediated coder and the graph wiring — stays MCTS-free, so a trivial step never
    # pays tree-search cost. Judged on the AST, so a comment never trips the gate.
    for rel in ("brain/engine.py", "agents/coder.py"):
        imported = _module_imports(rel)
        assert not any(
            name.startswith("brain.mcts") or "mcts_coder" in name for name in imported
        ), f"{rel} wires the offline MCTS tree into the single-shot spine"


# ── helpers ────────────────────────────────────────────────────────────────────


_PY_FUNC = (
    "def greet(name: str) -> str:\n"
    '    """Return a friendly greeting."""\n'
    '    secret_body_token = "computed " + name\n'
    "    return secret_body_token\n"
)


class _StubAdapter:
    """Deterministic sandbox adapter: returns canned results, never spawns a process.

    The last result repeats once the queue drains, so a perpetually-failing command
    is modelled with a single entry. Mirrors the executor suite's stub.
    """

    def __init__(self, results: List[SandboxResult]) -> None:
        self._results = list(results)
        self.calls: List[str] = []

    async def execute(
        self,
        command: str,
        *,
        timeout_s: float,
        cwd: str,
        env_whitelist: Dict[str, str],
        session_id: Optional[str] = None,
    ) -> SandboxResult:
        self.calls.append(command)
        if len(self._results) > 1:
            return self._results.pop(0)
        return self._results[0]


def _bind_adapter(adapter: Optional["_StubAdapter"]) -> ExitStack:
    """Bind the active sandbox tier (over conftest's _DirectAdapter) and isolate the
    WS notify the run_command branch reaches. Returns an entered ExitStack; using it
    in a ``with`` ties teardown to the block."""
    stack = ExitStack()
    stack.enter_context(patch.object(sb, "ACTIVE_ADAPTER", adapter))
    stack.enter_context(
        patch(
            "api.websocket_manager.vfs_manager.emit_graph_mutation",
            new=AsyncMock(return_value=None),
        )
    )
    return stack


def _run_command_state(command: str, **overrides: Any) -> Dict[str, Any]:
    # For a run_command step the WBS schema overloads target_file to hold the command.
    step = WBSStep(
        step_number=1,
        target_role="core_dev",  # type: ignore[arg-type]
        action="run_command",  # type: ignore[arg-type]
        target_file=command,
        description="Run the project verification.",
        status="pending",  # type: ignore[arg-type]
    )
    mission = MissionSpecification(
        outcome="Gate.",
        scope=["main.py"],
        constraints=["-"],
        decisions=["-"],
        tasks=[step],
        checks=["-"],
    )
    state: Dict[str, Any] = {
        "task_id": "gate-7-18",
        "mission_spec": mission,
        "current_step_id": 1,
        "retry_count": 0,
        "correction_attempts": 0,
        "errors": [],
        "security_flags": [],
        "validation_feedback": None,
        "session_permission_mode": "AUTO",
        "workspace_root": "",
        "project_id": "",
    }
    state.update(overrides)
    return state


def _vfs(version: str) -> VFSFile:
    """A minimal VFSFile carrying just the OCC version the reducer compares."""
    return VFSFile(blob_hash="0" * 32, document_version_id=version)


def _mock_response() -> MagicMock:
    resp = MagicMock()
    resp.usage = None  # skip the gateway's token accounting
    resp.choices = [MagicMock()]
    return resp


def _rf_rejecting(**kwargs: Any) -> MagicMock:
    """Stub litellm: 400 when response_format is present, succeed when it is absent."""
    if "response_format" in kwargs:
        raise Exception(
            "BadRequestError: 'response_format' is not supported by this model"
        )
    return _mock_response()
