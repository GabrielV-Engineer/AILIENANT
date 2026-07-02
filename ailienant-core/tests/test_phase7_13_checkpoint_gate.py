# tests/test_phase7_13_checkpoint_gate.py
"""Enterprise Spinal Cord — unified Push-model Checkpoint Gate.

Single E2E certification that the event-driven Push pillars hold together against
their **shipped** entry points. Test-only: it imports and invokes production code,
asserting the one load-bearing invariant per gate row — it does not re-run the
dedicated suites, and it modifies no production logic. Mirrors the sibling
``test_phase7_10_checkpoint_gate.py`` gate.

Async cases run via ``asyncio.run`` (no anyio-backend dependency).

Gate rows certified here (backend-assertable):
  SC  silent telemetry → reactive index seam      PR1 Dual-Rules privacy gate
  CC1 per-project graph write lock                 RL1 inbound rate-limit shedding
  SF1 reactive single-flight coalescing            CN1 task/daemon cancel on shutdown
  DR1 manual-only Dreaming + stale abort           AL1 self-heal in budget → DLQ
  ISO1 cognitive-isolation fence                   FR1 stream watchdog (local/cloud)
  FR2 correlation-id dedup (bounded)               FR3 abort ACK surfaces failure
  OR2 dead-letter resume round-trip                OR3 planner toggle reaches backend
  TL1 telemetry log scrubs secrets                 DD1 single VFS reader + named retries

Frontend-only rows are out of pytest scope — certified by ``npm run compile`` + the
manual smoke (§5.2): PR2 (Incognito halts the bus at the source in ide_sync.ts — no
backend hook exists), OR1 (the Planner Manual-Mode React form), DB1 (the HTTP-served
dashboard panels; their backend endpoints are covered by test_dashboard_segments /
test_runtime_status). REG (full pytest + mypy + npm) is the suite-level DoD, not a case.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, Generator, List, Optional, Tuple

import pytest
from unittest.mock import AsyncMock, patch

import main
from agents.error_correction import ErrorCorrectionAgent, attempt_correction
from api.websocket_manager import ConnectionManager, ws_adapter
from api.ws_contracts import (
    ClientFileDeleteEvent,
    ClientPlannerModeToggleEvent,
    IdeTelemetryPayload,
    ServerAbortAckEvent,
)
from brain.daemon import OvernightDaemon, overnight_daemon
from brain.engine import route_after_summarize
from brain.failure_breaker import failure_breaker
from brain.retry_policy import (
    CORRECTION_MAX_ATTEMPTS,
    LLM_MAX_TRANSPORT_RETRIES,
    WAL_CHECKPOINT_MAX_RETRIES,
)
from core import dead_letter, db as catalog_db
from core.config.byom_config import (
    BYOMConfig,
    ModelTarget,
    _WATCHDOG_CLOUD_MS,
    _WATCHDOG_LOCAL_MS,
    stream_watchdog_ms,
)
from core.dead_letter import (
    get_pending_dlqs,
    init_dlq_table,
    mark_dlq_resolved,
    save_dead_letter,
)
from core.indexer import SingleFlightCoordinator
from core.rules import RuleManager, rule_manager
from core.task_service import TaskService
from core.vfs_middleware import make_safe_reader

_PKG_ROOT = Path(__file__).resolve().parent.parent


# ── SC1/SC2 — silent telemetry routes into the reactive-index seam ────────────


class _FakeCoalescer:
    """Records submit / submit_unlink without touching the real indexer."""

    def __init__(self) -> None:
        self.submits: List[Tuple[str, str, str]] = []
        self.unlinks: List[Tuple[str, str]] = []

    def submit(self, filepath: str, content: str, project_id: str = "") -> None:
        self.submits.append((filepath, content, project_id))

    def submit_unlink(self, filepath: str, project_id: str = "") -> None:
        self.unlinks.append((filepath, project_id))


@pytest.fixture
def fake_coalescer(monkeypatch: pytest.MonkeyPatch) -> _FakeCoalescer:
    fake = _FakeCoalescer()
    monkeypatch.setattr(main, "io_coalescer", fake)
    return fake


def test_sc_save_and_rename_route_to_reactive_index(fake_coalescer: _FakeCoalescer) -> None:
    # Dispatch is fire-and-forget into the coalescer — no broadcast, no toast path.
    main._dispatch_ide_telemetry(
        IdeTelemetryPayload(action="file_saved", filepath="/w/a.py"), "proj"
    )
    main._dispatch_ide_telemetry(
        IdeTelemetryPayload(action="file_renamed", filepath="/w/new.py", old_path="/w/old.py"),
        "proj",
    )
    assert ("/w/a.py", "", "proj") in fake_coalescer.submits     # one file re-indexed
    assert ("/w/new.py", "", "proj") in fake_coalescer.submits   # rename migrates the row
    assert ("/w/old.py", "proj") in fake_coalescer.unlinks       # old path purged


def test_sc_delete_uses_purge_contract_not_telemetry() -> None:
    # Regression guard: deletes ride the purpose-built purge event, not the telemetry channel.
    frame = '{"event_type":"client_file_delete","data":{"filepath":"/w/gone.py","project_id":""}}'
    ev = ws_adapter.validate_json(frame)
    assert isinstance(ev, ClientFileDeleteEvent)
    assert ev.data.filepath == "/w/gone.py"


# ── PR1 — the Dual-Rules privacy gate never pushes an excluded file ───────────


@pytest.fixture
def _reset_rules() -> Generator[None, None, None]:
    rule_manager.reset()
    RuleManager._instance = None
    yield
    rule_manager.reset()
    RuleManager._instance = None


def test_pr1_excluded_file_is_never_readable(tmp_path: Path, _reset_rules: None) -> None:
    cfg_dir = tmp_path / ".ailienant"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    (cfg_dir / ".ailienant.json").write_text(
        json.dumps({"exclude_patterns": ["**/*.env"]}), encoding="utf-8"
    )
    secret = tmp_path / "config.env"
    secret.write_text("API_KEY=s3cr3t", encoding="utf-8")
    normal = tmp_path / "main.py"
    normal.write_text("x = 1\n", encoding="utf-8")

    read = make_safe_reader("proj-1", str(tmp_path), "sess-1")
    assert read(str(secret)) is None         # excluded → withheld from the brain
    assert read(str(normal)) == "x = 1\n"     # in-scope source → flows through


# ── CC1 — per-project graph-write lock identity ───────────────────────────────


def test_cc1_graph_lock_is_per_project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(catalog_db, "DB_CATALOG_PATH", str(tmp_path / "catalog.sqlite"))

    async def _run() -> None:
        same_a = catalog_db.graph_write_lock("projA")
        same_b = catalog_db.graph_write_lock("projA")
        other = catalog_db.graph_write_lock("projB")
        assert same_a is same_b          # one serialization point per project
        assert same_a is not other       # distinct projects never contend

    asyncio.run(_run())


# ── RL1 — inbound flood is rate-limited (the loop is not swamped) ─────────────


def test_rl1_inbound_flood_is_shed() -> None:
    mgr = ConnectionManager()
    drained = any(not mgr.allow_inbound("c-flood") for _ in range(1000))
    assert drained is True


# ── SF1 — rapid saves of one file coalesce to a single trailing run ───────────


def test_sf1_single_flight_coalesces_to_one_trailing_run() -> None:
    order: List[str] = []
    gate = asyncio.Event()

    async def _run() -> None:
        coord = SingleFlightCoordinator()

        async def first() -> None:
            order.append("start1")
            await gate.wait()
            order.append("end1")

        async def superseded() -> None:
            order.append("superseded")

        async def newest() -> None:
            order.append("newest")

        t1 = asyncio.create_task(coord.run("k", lambda: first()))
        await asyncio.sleep(0.01)
        await coord.run("k", lambda: superseded())   # coalesced away
        await coord.run("k", lambda: newest())       # newest wins the trailing slot
        assert order == ["start1"]
        gate.set()
        await t1
        assert order == ["start1", "end1", "newest"]

    asyncio.run(_run())


# ── CN1 — daemon / maintenance / generation tasks cancel cleanly ──────────────


def test_cn1_daemon_and_maintenance_stop_without_orphans() -> None:
    async def _run() -> None:
        from core.db_maintenance import WALCheckpointer

        d = OvernightDaemon()
        d.start()
        assert d._running is True
        await d.stop()
        assert d._running is False

        # The WAL worker spawns a real task; stop() must cancel it mid-sleep, no orphan.
        wal = WALCheckpointer(SimpleNamespace(is_writing=False, conn=None), interval_s=3600.0)  # pyright: ignore[reportArgumentType] — SimpleNamespace test double for HybridCheckpointer
        wal.start()
        await wal.stop()

    asyncio.run(_run())


def test_cn1_abort_session_cancels_registered_task() -> None:
    async def _run() -> None:
        ts = TaskService()
        started = asyncio.Event()

        async def _runner() -> None:
            started.set()
            try:
                await asyncio.sleep(5.0)
            except asyncio.CancelledError:
                # The runner absorbs its own cancellation by design and returns;
                # the test never installs an outer except that would swallow the signal.
                return

        task = asyncio.create_task(_runner())
        await started.wait()
        ts.register_active_task("sess-cn1", task)
        assert ts.abort_session("sess-cn1") is True
        await asyncio.wait_for(task, timeout=1.0)
        assert "sess-cn1" not in ts._active_tasks   # done-callback auto-pops the registry

    asyncio.run(_run())


# ── DR1 — Dreaming is manual-only; a save mid-run aborts without writing ───────


def test_dr1_no_idle_loop_exists() -> None:
    assert not hasattr(OvernightDaemon, "_loop")   # no heartbeat / timer wake
    assert overnight_daemon._running is False


def test_dr1_stale_snapshot_aborts_without_commit(monkeypatch: pytest.MonkeyPatch) -> None:
    import brain.daemon as daemon_mod

    lock_state: Dict[str, bool] = {"held": False}

    class _SpyLock:
        async def __aenter__(self) -> "_SpyLock":
            lock_state["held"] = True
            return self

        async def __aexit__(self, *exc: Any) -> bool:
            lock_state["held"] = False
            return False

    monkeypatch.setattr(daemon_mod, "graph_write_lock", lambda pid="": _SpyLock())

    writes: List[str] = []

    class _Semantic:
        async def semantic_upsert(self, file_path: str, content: str, workspace_hash: str) -> bool:
            writes.append(file_path)
            return True

    async def _invoke(messages: List[Dict[str, Any]], **kwargs: Any) -> Any:
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="note"))]
        )

    daemon = OvernightDaemon(
        semantic=_Semantic(),
        overview_fn=lambda root: "Workspace root: demo\nsrc/app.py",
        budget_fn=lambda: {"estimated_invested_usd": 0.0},
        llm_invoke=_invoke,
    )

    async def _run() -> None:
        result = await daemon.run_consolidation(
            "proj", None, workspace_root="/ws", session_id="dream:c1",
            stale_check=lambda: True,
        )
        assert result.status == "aborted_stale"
        assert writes == []   # the snapshot diverged → the commit was skipped

    asyncio.run(_run())


# ── AL1 — self-heal inside the budget, then concede to the DLQ ────────────────


_OFFENDER = "C:\\ws\\pkg\\mod.py"
_ORIGINAL = "def f():\n    return undefined_name\n"
_FIXED = "def f():\n    return 1\n"


def test_al1_self_heals_within_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        ErrorCorrectionAgent,
        "_read_offending_file",
        staticmethod(lambda path, state: _ORIGINAL),
    )

    async def _stub_invoke(system: str, payload: Dict[str, Any]) -> str:
        return json.dumps({"diagnosis": "fix", "filepath": _OFFENDER, "new_content": _FIXED})

    agent = ErrorCorrectionAgent(llm_invoker=_stub_invoke)

    async def _run() -> None:
        failure_breaker._state.clear()
        try:
            raise ValueError("boom")
        except ValueError as exc:
            state: Dict[str, Any] = {"workspace_root": "C:\\ws", "correction_attempts": 0}
            result = await attempt_correction(
                exc, state, failed_node="coder_agent",
                extra_candidates=[_OFFENDER], agent=agent,
            )
        assert result is not None and result.healed is True
        # The fix flows through the HITL channels, never written here directly.
        assert result.pending_patches.get(_OFFENDER) == _FIXED
        failure_breaker._state.clear()

    asyncio.run(_run())


def test_al1_over_budget_concedes_to_dlq(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        ErrorCorrectionAgent,
        "_read_offending_file",
        staticmethod(lambda path, state: _ORIGINAL),
    )

    async def _run() -> None:
        failure_breaker._state.clear()
        try:
            raise ValueError("boom")
        except ValueError as exc:
            state: Dict[str, Any] = {
                "workspace_root": "C:\\ws",
                "correction_attempts": CORRECTION_MAX_ATTEMPTS,
            }
            result = await attempt_correction(
                exc, state, failed_node="coder_agent", extra_candidates=[_OFFENDER],
            )
        # Budget exhausted → None (never raises); the caller routes to the dead-letter queue.
        assert result is None
        failure_breaker._state.clear()

    asyncio.run(_run())


# ── ISO1 — the self-healing agent stays behind the cognitive-isolation fence ──


def test_iso1_error_correction_has_no_personality_import() -> None:
    # Parse the AST so the fence is judged on real import statements, not docstring prose
    # (the module's own docstring explains *why* it must not import brain.personality).
    import ast

    tree = ast.parse(
        (_PKG_ROOT / "agents" / "error_correction.py").read_text(encoding="utf-8")
    )
    imported: List[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.append(node.module or "")
    assert not any(name.startswith("brain.personality") for name in imported)


# ── FR1 — the stream watchdog window is local-vs-cloud aware ──────────────────


def test_fr1_watchdog_is_local_vs_cloud_aware() -> None:
    local_cfg = BYOMConfig(
        chat_models={"big": ModelTarget(model="ollama/llama3.1", provider="ollama", is_local=True)}
    )
    cloud_cfg = BYOMConfig(
        chat_models={"big": ModelTarget(model="gpt-4o", provider="openai", is_local=False)}
    )
    with patch("core.config.byom_config.load_byom_config", return_value=local_cfg):
        assert stream_watchdog_ms() == _WATCHDOG_LOCAL_MS
    with patch("core.config.byom_config.load_byom_config", return_value=cloud_cfg):
        assert stream_watchdog_ms() == _WATCHDOG_CLOUD_MS


# ── FR2 — reconnect dedups by correlation id; the dedup set is bounded ────────


def test_fr2_correlation_id_dedups_and_is_bounded() -> None:
    main._recent_request_ids.clear()
    rid = "req-" + "a" * 8
    assert main._is_duplicate_request(rid) is False    # first sighting
    assert main._is_duplicate_request(rid) is True     # the reconnect replay is a duplicate

    for i in range(main._RECENT_REQUEST_CAP + 50):
        main._is_duplicate_request(f"req-{i}")
    assert len(main._recent_request_ids) <= main._RECENT_REQUEST_CAP
    main._recent_request_ids.clear()


# ── FR3 — Stop surfaces an ACK (never a silent failure) ───────────────────────


def test_fr3_abort_ack_carries_signalled_flag() -> None:
    mgr = ConnectionManager()
    sent = AsyncMock()

    async def _run() -> None:
        with patch.object(mgr, "send_personal_message", sent):
            await mgr.broadcast_abort_ack("sess-X", signalled=False)

    asyncio.run(_run())

    sent.assert_awaited_once()
    assert sent.await_args is not None
    envelope = sent.await_args.args[1]
    assert isinstance(envelope, ServerAbortAckEvent)
    assert envelope.data.session_id == "sess-X"
    assert envelope.data.signalled is False


# ── OR2 — the dead-letter resume lifecycle round-trips ────────────────────────


def test_or2_dead_letter_resume_round_trips(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(dead_letter, "DB_CATALOG_PATH", str(tmp_path / "catalog.sqlite"))

    async def _run() -> Tuple[str, List[Any], List[Any]]:
        await init_dlq_table()   # the isolated DB is NOT auto-migrated — create it explicitly
        episode_id = await save_dead_letter(
            task_id="task-r", thread_id="task-r", failed_node="apply_patch",
            exc=RuntimeError("crash"), state={"task_id": "task-r"},
        )
        before = await get_pending_dlqs("task-r")
        await mark_dlq_resolved(episode_id)
        after = await get_pending_dlqs("task-r")
        return episode_id, before, after

    episode_id, before, after = asyncio.run(_run())
    assert len(before) == 1 and before[0].episode_id == episode_id
    assert after == []   # a resolved episode never resurfaces as pending


# ── OR3 — the Planner toggle reaches the backend routing decision ─────────────


def test_or3_planner_toggle_reaches_backend() -> None:
    frame = '{"event_type":"client_planner_mode_toggle","data":{"active":true}}'
    ev = ws_adapter.validate_json(frame)
    assert isinstance(ev, ClientPlannerModeToggleEvent)
    assert ev.data.active is True

    # The registry the WS handler writes is a module-level seam on main.
    assert isinstance(main.planner_mode_registry, dict)
    # And the toggled flag steers the post-summarize route.
    assert route_after_summarize({"planner_mode_active": True}) == "ideation_loop"
    assert route_after_summarize({"planner_mode_active": False}) == "planner_agent"


# ── TL1 — the live telemetry log scrubs secrets before disk ───────────────────


def test_tl1_telemetry_log_scrubs_secrets(tmp_path: Path) -> None:
    import core.telemetry_log as tlog

    tlog.shutdown_telemetry_log()
    try:
        tlog.configure_telemetry_log(str(tmp_path))
        secret = "sk-ant-api03-ABCDEFGHIJKLMNOPQRSTUVWXYZ012345"
        tlog.log_ws_payload("out", "server_token_chunk", "sess-1", f"key={secret}")
        tlog.shutdown_telemetry_log()   # flush the listener before reading
        content = (tmp_path / tlog._LOG_FILENAME).read_text(encoding="utf-8")
        assert secret not in content     # redacted before it reached disk
        assert "sess-1" in content       # the transition itself is still audited
    finally:
        tlog.shutdown_telemetry_log()


# ── DD1 — one VFS reader factory; retry budgets are named constants ───────────


def test_dd1_single_vfs_reader_and_named_retries() -> None:
    assert callable(make_safe_reader)

    for rel in ("coder.py", "analyst_context.py", "error_correction.py"):
        src = (_PKG_ROOT / "agents" / rel).read_text(encoding="utf-8")
        assert "make_safe_reader" in src     # routes through the shared factory
        assert "read_safe(" not in src        # no bespoke firewalled reader remains

    pb = (_PKG_ROOT / "brain" / "prompt_builder.py").read_text(encoding="utf-8")
    assert "read_safe(" not in pb

    # The scattered litellm / WAL retry literals now live in one module.
    assert LLM_MAX_TRANSPORT_RETRIES == 2
    assert WAL_CHECKPOINT_MAX_RETRIES == 3
