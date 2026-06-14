# tests/test_phase8_9_checkpoint_gate.py
"""Portable Workspace Home — Division Checkpoint Gate.

Test-only certification that the workspace-home invariants hold against their
shipped entry points. It imports and invokes production code, asserting one
load-bearing invariant per row; it modifies no production logic and follows the
sibling-gate convention.

Rows certified here:
  HOME1     global stores resolve under the application home
  BIND1     bind_project creates the per-project GraphRAG dir; accessor returns it
  UNBOUND1  unbound access falls back without raising and never escapes the home
  OVERRIDE1 the explicit GraphRAG override beats the bound path
  PID1      project_id_for is raw-path SHA-256 (parity with the editor contract)
  INSTR1    AILIENANT.md present -> injected (capped); absent -> empty
  PLAN1     planning writes a navigable, traversal-safe plan file
  PORT1     path handling is pathlib-based with no shell-out
"""
from __future__ import annotations

import hashlib
import importlib
from pathlib import Path

import pytest

import core.storage_paths as storage_paths
from core.project_instructions import get_project_instructions
from core.state_manager import _plan_md_path, dump_plan_to_markdown
from brain.state import MissionSpecification, WBSStep


def _reset_binding() -> None:
    storage_paths._bound_project_id = None  # type: ignore[attr-defined]


def _spec() -> MissionSpecification:
    return MissionSpecification(
        outcome="Ship the thing",
        scope=["module a", "module b"],
        constraints=[],
        decisions=[],
        checks=[],
        tasks=[
            WBSStep(
                step_number=1,
                target_role="core_dev",
                action="edit_file",
                target_file="src/x.py",
                description="Implement x",
            )
        ],
    )


# ── HOME1 ─────────────────────────────────────────────────────────────────────

def test_home1_global_stores_under_application_home() -> None:
    import shared.config as config

    home = str(config.AILIENANT_HOME)
    assert config.DB_CATALOG_PATH.startswith(home)
    assert config.LANCEDB_PATH.startswith(home)
    assert config.MCTS_DB_PATH.startswith(home)


# ── BIND1 ─────────────────────────────────────────────────────────────────────

def test_bind1_binds_and_creates_per_project_dir(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AILIENANT_GRAPHRAG_LANCEDB", raising=False)
    _reset_binding()
    project_id = storage_paths.bind_project("/some/workspace/root")
    resolved = storage_paths.graphrag_lancedb_path()
    assert project_id in resolved
    assert resolved.endswith("lancedb")
    assert Path(resolved).is_dir()
    _reset_binding()


# ── UNBOUND1 ──────────────────────────────────────────────────────────────────

def test_unbound1_fallback_is_safe(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AILIENANT_GRAPHRAG_LANCEDB", raising=False)
    _reset_binding()
    resolved = storage_paths.graphrag_lancedb_path()  # must not raise
    home = str(storage_paths.AILIENANT_HOME)
    assert resolved.startswith(home)
    assert "_unbound" in resolved


# ── OVERRIDE1 ─────────────────────────────────────────────────────────────────

def test_override1_env_beats_bound_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _reset_binding()
    storage_paths.bind_project("/another/workspace")
    override = tmp_path / "forced_lancedb"
    monkeypatch.setenv("AILIENANT_GRAPHRAG_LANCEDB", str(override))
    assert storage_paths.graphrag_lancedb_path() == str(override)
    # Explicit-id accessor honors the override too.
    assert storage_paths.graphrag_lancedb_path_for("deadbeef") == str(override)
    _reset_binding()


# ── PID1 ──────────────────────────────────────────────────────────────────────

def test_pid1_project_id_is_raw_sha256() -> None:
    sample = "/home/user/proj"
    expected = hashlib.sha256(sample.encode("utf-8")).hexdigest()
    assert storage_paths.project_id_for(sample) == expected
    # Golden vector: pins the editor↔backend identity contract.
    assert expected == "7d73bf4fdeae2f4951c4c30c31818b272a36061af1a9153e5ab4575436ffc407"


# ── INSTR1 ────────────────────────────────────────────────────────────────────

def test_instr1_present_injected_absent_empty(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    (ws / ".ailienant").mkdir(parents=True)

    # Absent → empty, zero tokens.
    assert get_project_instructions("pid", str(ws), "sid") == ""

    # Present → injected, with the heading.
    (ws / ".ailienant" / "AILIENANT.md").write_text(
        "Always add type hints.", encoding="utf-8"
    )
    out = get_project_instructions("pid", str(ws), "sid")
    assert "Project Instructions" in out
    assert "Always add type hints." in out

    # Oversized → head-sliced (capped). Many short lines so the VFS minification
    # guard (max line length) does not reject the file before we cap it.
    big = "Always add type hints everywhere in this module.\n" * 1000
    (ws / ".ailienant" / "AILIENANT.md").write_text(big, encoding="utf-8")
    capped = get_project_instructions("pid", str(ws), "sid", max_chars=8000)
    assert len(capped) < 9000
    assert "truncated" in capped


# ── PLAN1 ─────────────────────────────────────────────────────────────────────

def test_plan1_writes_traversal_safe_plan(tmp_path: Path) -> None:
    ok = dump_plan_to_markdown(_spec(), str(tmp_path), "task-1/../escape")
    assert ok is True
    target = _plan_md_path(str(tmp_path), "task-1/../escape")
    # Stem is confined under .ailienant/plans: path separators are stripped, so the
    # filename cannot escape its directory (residual dots are inert without a sep).
    assert target.parent == tmp_path / ".ailienant" / "plans"
    assert "/" not in target.name and "\\" not in target.name
    resolved_plans = (tmp_path / ".ailienant" / "plans").resolve()
    assert str(target.resolve()).startswith(str(resolved_plans))
    assert target.is_file()
    body = target.read_text(encoding="utf-8")
    assert "Ship the thing" in body
    assert "Implement x" in body

    # No-op when the mission is absent.
    assert dump_plan_to_markdown(None, str(tmp_path), "t2") is False


# ── PORT1 ─────────────────────────────────────────────────────────────────────

def test_port1_pathlib_no_shellout() -> None:
    for mod_name in ("core.storage_paths", "core.state_manager", "core.project_instructions"):
        module = importlib.import_module(mod_name)
        source = Path(module.__file__).read_text(encoding="utf-8")  # type: ignore[arg-type]
        assert "os.system" not in source
        assert "Path(" in source
