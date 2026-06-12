# ailienant-core/tests/test_gateway_catalog_v1.py
#
# Capability Catalog v1 tests for the External Capability Gateway: the READ_ONLY
# memory/graph verbs answer in-process (keyed by the workspace project id), run_task
# submits to the live host under the conservative posture over loopback, and
# check_task_status reads lifecycle state back. Backends and loopback seams are
# monkeypatched — no real database, socket, or running host.

from __future__ import annotations

import asyncio
import hashlib
import json
from types import SimpleNamespace
from typing import Any, Dict, Iterator

import pytest

from core.config.host_discovery import HostCoords, HostNotRunningError
from core.permissions import session_mode_from_frontend
from core.task_service import TaskService
from gateway import governance, handlers, ledger, server


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def iso_ledger(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> Any:
    """Point the ledger at an isolated tmp file and reset env knobs to defaults."""
    path = tmp_path / "gateway_ledger.json"
    monkeypatch.setattr(ledger, "LEDGER_PATH", path)
    for var in (
        "AILIENANT_GATEWAY_RATE_CAP",
        "AILIENANT_GATEWAY_RATE_REFILL_PER_S",
        "AILIENANT_GATEWAY_BUDGET",
        "AILIENANT_GATEWAY_CALLER_ID",
        "AILIENANT_GATEWAY_TOKEN",
    ):
        monkeypatch.delenv(var, raising=False)
    return path


@pytest.fixture()
def registered() -> Iterator[None]:
    """Bind the real capability handlers, then restore the registry on teardown.

    Registration mutates a module global; snapshot/restore keeps it from leaking into
    tests that assert on unwired verbs.
    """
    saved = dict(server._HANDLERS)
    for name, handler in handlers.CAPABILITY_HANDLERS.items():
        server.register_handler(name, handler)
    try:
        yield
    finally:
        server._HANDLERS.clear()
        server._HANDLERS.update(saved)


def _dispatch(name: str, args: Dict[str, Any]) -> Dict[str, Any]:
    result = asyncio.run(server.dispatch_call(name, args))
    return json.loads(result[0].text)  # type: ignore[no-any-return]


_WS = "/abs/workspace"
_PID = hashlib.sha256(_WS.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# project_id derivation (mirrors the editor exactly)
# ---------------------------------------------------------------------------


def test_project_id_for_is_raw_sha256_of_path() -> None:
    assert handlers.project_id_for(_WS) == hashlib.sha256(_WS.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# READ_ONLY verbs respond in-process, keyed by project_id (DoD)
# ---------------------------------------------------------------------------


def test_query_memory_responds_and_keys_on_project_id(
    iso_ledger: Any, registered: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: Dict[str, Any] = {}

    async def _fake_snippets(self: Any, user_input: str, workspace_hash: str = "", k: int = 5) -> Any:
        seen["workspace_hash"] = workspace_hash
        seen["query"] = user_input
        return [("a.py", "snippet-a"), ("b.py", "snippet-b")]

    import core.memory.semantic_memory as sm

    monkeypatch.setattr(sm.SemanticMemoryManager, "search_snippets", _fake_snippets)

    payload = _dispatch("query_memory", {"query": "where is X", "workspace_root": _WS})
    assert payload["status"] == "ok"
    assert payload["result"] == [
        {"file_path": "a.py", "snippet": "snippet-a"},
        {"file_path": "b.py", "snippet": "snippet-b"},
    ]
    assert seen["workspace_hash"] == _PID  # derived from workspace_root, not passed raw


def test_get_dependents_responds_and_keys_on_project_id(
    iso_ledger: Any, registered: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: Dict[str, Any] = {}

    async def _fake_dependents(target: str, project_id: str = "") -> Any:
        seen["project_id"] = project_id
        seen["target"] = target
        return ["caller_a.py", "caller_b.py"]

    import core.db as core_db

    monkeypatch.setattr(core_db, "get_dependents", _fake_dependents)

    payload = _dispatch("get_dependents", {"symbol": "pkg.mod.fn", "workspace_root": _WS})
    assert payload["status"] == "ok"
    assert payload["result"] == ["caller_a.py", "caller_b.py"]
    assert seen["project_id"] == _PID
    assert seen["target"] == "pkg.mod.fn"


def test_get_workspace_graph_responds_and_maps_edges(
    iso_ledger: Any, registered: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def _fake_edges(project_id: str = "") -> Any:
        assert project_id == _PID
        return [("a.py", "b.py", "high", 0.9), ("b.py", "c.py", None, None)]

    import core.db as core_db

    monkeypatch.setattr(core_db, "get_graph_edges_enriched", _fake_edges)

    payload = _dispatch("get_workspace_graph", {"workspace_root": _WS})
    assert payload["status"] == "ok"
    assert payload["result"] == [
        {"source": "a.py", "target": "b.py", "confidence": "high", "score": 0.9},
        {"source": "b.py", "target": "c.py", "confidence": None, "score": None},
    ]


def test_read_only_verb_rejects_missing_required_arg(
    iso_ledger: Any, registered: None
) -> None:
    payload = _dispatch("query_memory", {"workspace_root": _WS})  # no "query"
    assert payload["status"] == "error"
    assert payload["reason"] == "invalid_arguments"
    assert payload["missing"] == ["query"]
    assert payload["capability"] == "query_memory"


# ---------------------------------------------------------------------------
# run_task: submits under the conservative posture (DoD)
# ---------------------------------------------------------------------------


def test_run_task_submits_under_conservative_mode(
    iso_ledger: Any, registered: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: Dict[str, Any] = {}

    async def _fake_resolve() -> HostCoords:
        return HostCoords(port=9999, token="tok", pid=1)

    async def _fake_submit(coords: HostCoords, task_id: str, payload: Dict[str, Any]) -> None:
        captured["task_id"] = task_id
        captured["payload"] = payload

    monkeypatch.setattr(handlers, "resolve_host_or_error", _fake_resolve)
    monkeypatch.setattr(handlers, "_submit_task_loopback", _fake_submit)

    payload = _dispatch("run_task", {"prompt": "fix the bug", "workspace_root": _WS})
    assert payload["status"] == "ok"
    assert payload["result"]["status"] == "submitted"
    assert payload["result"]["task_id"] == captured["task_id"]
    # The spawned task runs under the conservative posture — never silent AUTO.
    assert captured["payload"]["execution_mode"] == "ask_before_edits"
    assert (
        session_mode_from_frontend(captured["payload"]["execution_mode"])
        is governance.INTERNAL_TASK_MODE
    )
    assert captured["payload"]["project_id"] == _PID
    assert captured["payload"]["dirty_buffers"] == []


def test_run_task_fails_fast_when_host_down(
    iso_ledger: Any, registered: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def _no_host() -> HostCoords:
        raise HostNotRunningError("not running")

    monkeypatch.setattr(handlers, "resolve_host_or_error", _no_host)

    payload = _dispatch("run_task", {"prompt": "x", "workspace_root": _WS})
    assert payload["status"] == "error"
    assert payload["reason"] == "host_unavailable"
    assert "VS Code" in payload["message"]


# ---------------------------------------------------------------------------
# check_task_status: passthrough of the host's lifecycle read
# ---------------------------------------------------------------------------


def test_check_task_status_passthrough(
    iso_ledger: Any, registered: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def _fake_resolve() -> HostCoords:
        return HostCoords(port=9999, token=None, pid=1)

    async def _fake_status(coords: HostCoords, task_id: str) -> Any:
        return {"status": "running", "session_id": task_id}

    monkeypatch.setattr(handlers, "resolve_host_or_error", _fake_resolve)
    monkeypatch.setattr(handlers, "_get_status_loopback", _fake_status)

    payload = _dispatch("check_task_status", {"task_id": "t-42"})
    assert payload["status"] == "ok"
    assert payload["result"] == {"status": "running", "session_id": "t-42"}


# ---------------------------------------------------------------------------
# Host read-endpoint: get_task_status over existing lifecycle state
# ---------------------------------------------------------------------------


def _bare_service() -> TaskService:
    svc = TaskService.__new__(TaskService)  # bypass the heavy constructor
    svc._active_tasks = {}
    return svc


def test_get_task_status_running() -> None:
    svc = _bare_service()
    svc._active_tasks["t1"] = SimpleNamespace(done=lambda: False)  # type: ignore[assignment]
    assert svc.get_task_status("t1") == {"status": "running", "session_id": "t1"}


def test_get_task_status_completed(monkeypatch: pytest.MonkeyPatch) -> None:
    svc = _bare_service()
    import brain.checkpoint as cp

    rows = [SimpleNamespace(promoted_at=10.0, termination_reason=None)]
    monkeypatch.setattr(cp.hybrid_checkpointer, "list_checkpoints", lambda _t: rows)
    status = svc.get_task_status("t2")
    assert status["status"] == "completed"
    assert status["checkpoints"] == 1
    assert status["last_at"] == 10.0


def test_get_task_status_aborted(monkeypatch: pytest.MonkeyPatch) -> None:
    svc = _bare_service()
    import brain.checkpoint as cp

    rows = [SimpleNamespace(promoted_at=11.0, termination_reason="user_abort")]
    monkeypatch.setattr(cp.hybrid_checkpointer, "list_checkpoints", lambda _t: rows)
    assert svc.get_task_status("t3")["status"] == "aborted"


def test_get_task_status_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    svc = _bare_service()
    import brain.checkpoint as cp

    monkeypatch.setattr(cp.hybrid_checkpointer, "list_checkpoints", lambda _t: [])
    assert svc.get_task_status("nope")["status"] == "unknown"


# ---------------------------------------------------------------------------
# Submit→register race: the runner is known the instant the ack returns
# ---------------------------------------------------------------------------


def test_submit_registers_task_synchronously(monkeypatch: pytest.MonkeyPatch) -> None:
    import main

    async def _scenario() -> None:
        async def _blocker(*_a: Any, **_kw: Any) -> Dict[str, Any]:
            await asyncio.Event().wait()  # never completes — the runner stays in flight
            return {}

        monkeypatch.setattr(main.task_service, "process_task", _blocker)
        payload = main.TaskPayload(task_prompt="hi", dirty_buffers=[])
        try:
            ack = await main.submit_task(payload, x_task_id="t-race")
            assert ack["status"] == "accepted"
            # Registered synchronously — no await between submit and this read.
            assert "t-race" in main.task_service._active_tasks
            assert main.task_service.get_task_status("t-race")["status"] == "running"
        finally:
            main.task_service.abort_session("t-race")

    asyncio.run(_scenario())


# ---------------------------------------------------------------------------
# Handler robustness: a raising handler degrades to a structured error
# ---------------------------------------------------------------------------


def test_handler_error_is_structured(
    iso_ledger: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def _boom(_args: Dict[str, Any]) -> Any:
        raise ValueError("kaboom")

    saved = dict(server._HANDLERS)
    server.register_handler("query_memory", _boom)
    try:
        payload = _dispatch("query_memory", {"query": "x", "workspace_root": _WS})
        assert payload["status"] == "error"
        assert payload["reason"] == "handler_error"
        assert "kaboom" in payload["detail"]
    finally:
        server._HANDLERS.clear()
        server._HANDLERS.update(saved)
