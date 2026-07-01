"""Dead-code detection: file-level zero-resolved-in-degree, non-entrypoint scan.

Exercises ``compute_dead_code_sync`` directly against seeded edges/indexed-file
tuples (no live DB), plus the allowlist loader against a real ``tmp_path``
workspace, the bounded/jailed content reader, and the analyst-tool wiring:
- hardcoded entrypoint exclusion (FastAPI decorators, pytest files, main.py,
  tool-registration call sites) — filename-only signals never need content,
- the JSON allowlist extends the exclusion via workspace-relative globs,
- true-orphan detection, and external/unresolvable targets never becoming
  candidates,
- the allowlist config fails open to the hardcoded set on any absent/malformed
  file,
- content reads happen only for narrowed candidates (event-loop hygiene proof)
  and are size-capped (OOM guard),
- the DeadCodeDetectionTool degrades to an error payload rather than raising,
  and is retrievable by the analyst role after registration.
"""
from __future__ import annotations

import hashlib
import json
import struct
from pathlib import Path
from typing import Callable, Dict, List, Optional
from unittest.mock import AsyncMock, patch

import pytest

from core.dead_code import (
    _bounded_jailed_read,
    _load_allowlist_patterns,
    compute_dead_code,
    compute_dead_code_sync,
)
from core.tool_rag import ToolRAGStore
from tools.analyst_tools import DeadCodeDetectionTool, register_analyst_tools

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _reader(mapping: Dict[str, str]) -> Callable[[str], Optional[str]]:
    return lambda path: mapping.get(path)


def _isolated_store(tmp_path: Path) -> ToolRAGStore:
    """Deterministic SHA256 fake embeddings — no network, dim=8 (mirrors 8.8.2)."""

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
        store_path=str(tmp_path / "tool_rag_dead_code"),
        embedding_dim=8,
        register_atexit_cleanup=False,
    )


# ── Hardcoded-entrypoint exclusion ───────────────────────────────────────────


def test_fastapi_route_file_excluded() -> None:
    indexed = ("/ws/api/routes.py",)
    contents = {"/ws/api/routes.py": '@router.get("/x")\ndef handler(): ...\n'}
    result = compute_dead_code_sync((), indexed, "/ws", content_reader=_reader(contents))
    assert result == []


def test_fastapi_app_websocket_excluded() -> None:
    indexed = ("/ws/api/ws_routes.py",)
    contents = {"/ws/api/ws_routes.py": '@app.websocket("/api/v1/ws/{client_id}")\n'}
    result = compute_dead_code_sync((), indexed, "/ws", content_reader=_reader(contents))
    assert result == []


def test_pytest_file_excluded_by_filename() -> None:
    indexed = ("/ws/tests/test_foo.py",)
    # No content marker at all — filename glob alone must suffice.
    result = compute_dead_code_sync((), indexed, "/ws", content_reader=_reader({}))
    assert result == []


def test_conftest_excluded_by_filename() -> None:
    indexed = ("/ws/tests/conftest.py",)
    result = compute_dead_code_sync((), indexed, "/ws", content_reader=_reader({}))
    assert result == []


def test_dunder_main_guard_excluded() -> None:
    indexed = ("/ws/scripts/run.py",)
    contents = {"/ws/scripts/run.py": 'if __name__ == "__main__":\n    main()\n'}
    result = compute_dead_code_sync((), indexed, "/ws", content_reader=_reader(contents))
    assert result == []


def test_tool_registration_call_excluded() -> None:
    indexed = ("/ws/tools/setup.py",)
    contents = {"/ws/tools/setup.py": "register_analyst_tools(store)\n"}
    result = compute_dead_code_sync((), indexed, "/ws", content_reader=_reader(contents))
    assert result == []


def test_main_py_basename_excluded() -> None:
    indexed = ("/ws/main.py",)
    # No content marker present at all — the filename-only basename rule must fire.
    result = compute_dead_code_sync((), indexed, "/ws", content_reader=_reader({}))
    assert result == []


# ── JSON allowlist extension ──────────────────────────────────────────────────


def test_json_allowlist_excludes_matching_orphan() -> None:
    indexed = ("/ws/jobs/scheduled_task.py",)
    result = compute_dead_code_sync(
        (), indexed, "/ws", ("jobs/*.py",), content_reader=_reader({})
    )
    assert result == []


def test_json_allowlist_non_matching_pattern_still_flags() -> None:
    indexed = ("/ws/jobs/scheduled_task.py",)
    result = compute_dead_code_sync(
        (), indexed, "/ws", ("other/*.py",), content_reader=_reader({})
    )
    assert result == [{"file": "jobs/scheduled_task.py", "in_degree": 0}]


def test_allowlist_glob_double_star_matches_nested_path() -> None:
    indexed = ("/ws/legacy/sub/old.py",)
    result = compute_dead_code_sync(
        (), indexed, "/ws", ("legacy/**",), content_reader=_reader({})
    )
    assert result == []


# ── True-orphan detection ─────────────────────────────────────────────────────


def test_true_orphan_flagged() -> None:
    indexed = ("/ws/pkg/orphan.py",)
    result = compute_dead_code_sync((), indexed, "/ws", content_reader=_reader({}))
    assert result == [{"file": "pkg/orphan.py", "in_degree": 0}]


def test_non_orphan_not_flagged() -> None:
    edges = (("/ws/pkg/app.py", "/ws/pkg/lib.py"),)
    indexed = ("/ws/pkg/lib.py", "/ws/pkg/app.py")
    result = compute_dead_code_sync(edges, indexed, "/ws", content_reader=_reader({}))
    assert "pkg/lib.py" not in [c["file"] for c in result]


# ── Allowlist relativization ──────────────────────────────────────────────────


def test_allowlist_relativizes_absolute_indexed_path_before_matching() -> None:
    # An indexed file is always stored absolute; the allowlist pattern is a
    # workspace-relative glob. If relativization were skipped, this pattern
    # would never match the absolute path and the file would stay flagged.
    indexed = ("/ws/jobs/scheduled_task.py",)
    result = compute_dead_code_sync(
        (), indexed, "/ws", ("jobs/*.py",), content_reader=_reader({})
    )
    assert result == []


# ── External-target exclusion ────────────────────────────────────────────────


def test_external_only_target_not_flagged() -> None:
    # "fastapi" never appears in indexed_files and is not suffix-resolvable —
    # it must never be iterated as a dead-code candidate at all.
    edges = (("/ws/app.py", "fastapi"),)
    indexed = ("/ws/app.py",)
    result = compute_dead_code_sync(edges, indexed, "/ws", content_reader=_reader({}))
    assert "fastapi" not in [c["file"] for c in result]


def test_python_dotted_module_target_resolves_and_is_not_orphan() -> None:
    # The concrete case the raw/unresolved dashboard computation gets wrong:
    # brain.state must resolve to brain/state.py, giving it in-degree 1.
    edges = (("/ws/pkg/app.py", "brain.state"),)
    indexed = ("/ws/pkg/brain/state.py", "/ws/pkg/app.py")
    result = compute_dead_code_sync(edges, indexed, "/ws", content_reader=_reader({}))
    assert "pkg/brain/state.py" not in [c["file"] for c in result]


# ── I/O narrowing (event-loop hygiene proof) ─────────────────────────────────


def test_content_reader_only_called_for_narrowed_candidates() -> None:
    calls: List[str] = []

    def recording_reader(path: str) -> Optional[str]:
        calls.append(path)
        return None

    edges = (("/ws/pkg/app.py", "/ws/pkg/lib.py"),)
    indexed = ("/ws/pkg/app.py", "/ws/pkg/lib.py", "/ws/pkg/orphan.py")
    compute_dead_code_sync(edges, indexed, "/ws", content_reader=recording_reader)

    assert "/ws/pkg/lib.py" not in calls  # non-zero in-degree — never opened
    assert "/ws/pkg/orphan.py" in calls  # zero in-degree, no other gate matched


def test_content_reader_not_called_for_filename_entrypoint() -> None:
    calls: List[str] = []

    def recording_reader(path: str) -> Optional[str]:
        calls.append(path)
        return None

    indexed = ("/ws/tests/test_foo.py",)
    compute_dead_code_sync((), indexed, "/ws", content_reader=recording_reader)
    assert calls == []


def test_content_reader_not_called_for_allowlisted_file() -> None:
    calls: List[str] = []

    def recording_reader(path: str) -> Optional[str]:
        calls.append(path)
        return None

    indexed = ("/ws/jobs/scheduled_task.py",)
    compute_dead_code_sync((), indexed, "/ws", ("jobs/*.py",), content_reader=recording_reader)
    assert calls == []


# ── Bounded/jailed content reader (OOM + zero-trust) ─────────────────────────


def test_bounded_jailed_read_returns_content_within_cap(tmp_path: Path) -> None:
    f = tmp_path / "small.py"
    f.write_text("x = 1\n", encoding="utf-8")
    assert _bounded_jailed_read(str(f), str(tmp_path)) == "x = 1\n"


def test_bounded_jailed_read_oversized_file_returns_none(tmp_path: Path) -> None:
    import core.dead_code as dc

    f = tmp_path / "big.py"
    f.write_text("x" * (dc._CONTENT_MAX_BYTES + 1), encoding="utf-8")
    assert _bounded_jailed_read(str(f), str(tmp_path)) is None


def test_bounded_jailed_read_jail_escape_returns_none(tmp_path: Path) -> None:
    outside = tmp_path.parent / "outside_dead_code_test.py"
    outside.write_text("x = 1\n", encoding="utf-8")
    try:
        assert _bounded_jailed_read(str(outside), str(tmp_path)) is None
    finally:
        outside.unlink(missing_ok=True)


def test_oversized_file_falls_back_to_filename_only_detection(tmp_path: Path) -> None:
    import core.dead_code as dc

    big = tmp_path / "big_orphan.py"
    big.write_text("x" * (dc._CONTENT_MAX_BYTES + 1), encoding="utf-8")
    indexed = (str(big),)
    reader = lambda path: _bounded_jailed_read(path, str(tmp_path))  # noqa: E731
    result = compute_dead_code_sync((), indexed, str(tmp_path), content_reader=reader)
    # Oversized => content=None => no marker found => still flagged (accepted,
    # documented edge case; filename didn't match an entrypoint pattern either).
    assert result == [{"file": "big_orphan.py", "in_degree": 0}]


# ── Allowlist config: fail-open behavior ─────────────────────────────────────


def test_allowlist_absent_file_fail_open(tmp_path: Path) -> None:
    assert _load_allowlist_patterns(None, str(tmp_path), None) == []


def test_allowlist_malformed_json_fail_open(tmp_path: Path) -> None:
    ailienant_dir = tmp_path / ".ailienant"
    ailienant_dir.mkdir()
    (ailienant_dir / "dead-code-allowlist.json").write_text("{not json", encoding="utf-8")
    assert _load_allowlist_patterns(None, str(tmp_path), None) == []


def test_allowlist_wrong_shape_fail_open(tmp_path: Path) -> None:
    ailienant_dir = tmp_path / ".ailienant"
    ailienant_dir.mkdir()
    (ailienant_dir / "dead-code-allowlist.json").write_text(
        json.dumps({"patterns": ["a"]}), encoding="utf-8"
    )
    assert _load_allowlist_patterns(None, str(tmp_path), None) == []


def test_allowlist_non_string_entries_rejected(tmp_path: Path) -> None:
    ailienant_dir = tmp_path / ".ailienant"
    ailienant_dir.mkdir()
    (ailienant_dir / "dead-code-allowlist.json").write_text(
        json.dumps(["ok/*.py", 42]), encoding="utf-8"
    )
    assert _load_allowlist_patterns(None, str(tmp_path), None) == []


def test_allowlist_valid_file_loaded(tmp_path: Path) -> None:
    ailienant_dir = tmp_path / ".ailienant"
    ailienant_dir.mkdir()
    (ailienant_dir / "dead-code-allowlist.json").write_text(
        json.dumps(["jobs/*.py", "legacy/**"]), encoding="utf-8"
    )
    assert _load_allowlist_patterns(None, str(tmp_path), None) == ["jobs/*.py", "legacy/**"]


# ── Async wrapper ─────────────────────────────────────────────────────────────


async def test_async_wrapper_fetches_and_delegates(monkeypatch: pytest.MonkeyPatch) -> None:
    import core.dead_code as dc

    monkeypatch.setattr(dc.catalog_db, "get_all_edges", AsyncMock(return_value=[]))
    monkeypatch.setattr(
        dc.catalog_db, "list_indexed_files", AsyncMock(return_value=["/ws/pkg/orphan.py"])
    )
    result = await compute_dead_code("proj", "/ws", content_reader=_reader({}))
    assert result == [{"file": "pkg/orphan.py", "in_degree": 0}]


# ── Tool layer ────────────────────────────────────────────────────────────────


async def test_detect_dead_code_tool_returns_json() -> None:
    tool = DeadCodeDetectionTool(project_id="proj", workspace_root="/ws")
    with patch(
        "core.dead_code.compute_dead_code",
        new=AsyncMock(return_value=[{"file": "pkg/orphan.py", "in_degree": 0}]),
    ):
        raw = await tool._arun()
    payload = json.loads(raw)
    assert payload == {"candidates": [{"file": "pkg/orphan.py", "in_degree": 0}], "count": 1}


async def test_detect_dead_code_tool_fails_open_on_exception() -> None:
    tool = DeadCodeDetectionTool(project_id="proj", workspace_root="/ws")
    with patch(
        "core.dead_code.compute_dead_code", new=AsyncMock(side_effect=RuntimeError("graph boom"))
    ):
        raw = await tool._arun()
    payload = json.loads(raw)
    assert payload["candidates"] == []
    assert "error" in payload


async def test_register_analyst_tools_includes_detect_dead_code(tmp_path: Path) -> None:
    store = _isolated_store(tmp_path)
    await register_analyst_tools(store)

    schemas = {s.name: s for s in store.all_schemas()}
    assert "detect_dead_code" in schemas
    assert "analyst" in schemas["detect_dead_code"].allowed_roles

    from core.permissions import ToolPrivilegeTier

    assert schemas["detect_dead_code"].privilege_tier == ToolPrivilegeTier.READ_ONLY
