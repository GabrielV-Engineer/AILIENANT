# tests/test_workspace_context.py
"""Phase 7.12 — workspace-shape overview hard-bound guarantees (Issues 4 & 8)."""
from __future__ import annotations

from pathlib import Path

from agents.workspace_context import build_workspace_overview


def test_empty_on_missing_root() -> None:
    assert build_workspace_overview("") == ""
    assert build_workspace_overview("/no/such/path/zzz") == ""


def test_manifests_injected(tmp_path: Path) -> None:
    (tmp_path / "README.md").write_text("# Hello Project", encoding="utf-8")
    (tmp_path / "pyproject.toml").write_text("[project]\nname='x'", encoding="utf-8")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.py").write_text("print(1)", encoding="utf-8")

    out = build_workspace_overview(str(tmp_path))
    assert "README.md" in out
    assert "Hello Project" in out
    assert "pyproject.toml" in out
    assert "main.py" in out


def test_noise_dirs_pruned(tmp_path: Path) -> None:
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "junk.js").write_text("x", encoding="utf-8")
    (tmp_path / "app.py").write_text("x", encoding="utf-8")

    out = build_workspace_overview(str(tmp_path))
    assert "app.py" in out
    assert "node_modules" not in out


def test_max_files_truncation(tmp_path: Path) -> None:
    for i in range(50):
        (tmp_path / f"f{i}.txt").write_text("x", encoding="utf-8")
    out = build_workspace_overview(str(tmp_path), max_files=10)
    assert "truncated at 10 files" in out


def test_budget_hard_cap(tmp_path: Path) -> None:
    (tmp_path / "README.md").write_text("A" * 5000, encoding="utf-8")
    out = build_workspace_overview(str(tmp_path), budget=512)
    assert len(out) <= 512
