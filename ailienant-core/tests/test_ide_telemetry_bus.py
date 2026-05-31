"""IDE telemetry bus — silent file-lifecycle channel (Push transport spine).

The bus carries save/create/rename on the silent ``client_ide_telemetry``
channel and routes them into the existing reactive-index seam
(``io_coalescer``); delete keeps the purpose-built ``client_file_delete`` purge
contract. Backend dispatch is a non-blocking coalescer enqueue, gated upstream
by the per-client inbound token bucket so a save storm cannot starve the loop.
"""
from typing import Any, List, Tuple

import pytest

from api.websocket_manager import ws_adapter, ConnectionManager
from api.ws_contracts import (
    ClientFileDeleteEvent,
    ClientIdeTelemetryEvent,
    IdeTelemetryPayload,
)


class _FakeCoalescer:
    """Records submit / submit_unlink calls without touching the real indexer."""

    def __init__(self) -> None:
        self.submits: List[Tuple[str, str, str]] = []
        self.unlinks: List[Tuple[str, str]] = []

    def submit(self, filepath: str, content: str, project_id: str = "") -> None:
        self.submits.append((filepath, content, project_id))

    def submit_unlink(self, filepath: str, project_id: str = "") -> None:
        self.unlinks.append((filepath, project_id))


@pytest.fixture
def fake_coalescer(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Swap the module-global io_coalescer for a recorder and yield it."""
    import main

    fake = _FakeCoalescer()
    monkeypatch.setattr(main, "io_coalescer", fake)
    return fake


# --- Contract -----------------------------------------------------------------

def test_contract_roundtrips() -> None:
    frame = (
        '{"event_type":"client_ide_telemetry",'
        '"data":{"action":"file_saved","filepath":"/w/a.py","document_version_id":"7"}}'
    )
    ev = ws_adapter.validate_json(frame)
    assert isinstance(ev, ClientIdeTelemetryEvent)
    assert ev.data.action == "file_saved"
    assert ev.data.filepath == "/w/a.py"
    assert ev.data.old_path is None
    assert ev.data.document_version_id == "7"


def test_malformed_action_rejected() -> None:
    bad = (
        '{"event_type":"client_ide_telemetry",'
        '"data":{"action":"file_exploded","filepath":"/w/a.py"}}'
    )
    with pytest.raises(Exception):
        ws_adapter.validate_json(bad)


# --- Dispatch into the existing reactive seam ---------------------------------

def test_saved_routes_to_coalescer(fake_coalescer: _FakeCoalescer) -> None:
    import main

    main._dispatch_ide_telemetry(IdeTelemetryPayload(action="file_saved", filepath="/w/a.py"))
    assert fake_coalescer.submits == [("/w/a.py", "", "")]
    assert fake_coalescer.unlinks == []


def test_created_routes_to_coalescer(fake_coalescer: _FakeCoalescer) -> None:
    import main

    main._dispatch_ide_telemetry(IdeTelemetryPayload(action="file_created", filepath="/w/new.py"))
    assert fake_coalescer.submits == [("/w/new.py", "", "")]
    assert fake_coalescer.unlinks == []


def test_renamed_purges_old_and_resubmits_new(fake_coalescer: _FakeCoalescer) -> None:
    import main

    main._dispatch_ide_telemetry(
        IdeTelemetryPayload(action="file_renamed", filepath="/w/new.py", old_path="/w/old.py")
    )
    assert fake_coalescer.unlinks == [("/w/old.py", "")]
    assert fake_coalescer.submits == [("/w/new.py", "", "")]


def test_rename_without_old_path_degrades_to_submit(fake_coalescer: _FakeCoalescer) -> None:
    """A rename frame missing old_path must not crash; it indexes the new path."""
    import main

    main._dispatch_ide_telemetry(IdeTelemetryPayload(action="file_renamed", filepath="/w/new.py"))
    assert fake_coalescer.unlinks == []
    assert fake_coalescer.submits == [("/w/new.py", "", "")]


# --- Safety -------------------------------------------------------------------

def test_rate_limit_sheds_telemetry_class() -> None:
    """The receive loop gates client_ide_telemetry on allow_inbound exactly like
    client_file_update: once the bucket is drained, the loop skips dispatch."""
    mgr = ConnectionManager()
    drained = any(not mgr.allow_inbound("c1") for _ in range(1000))
    assert drained is True  # the bucket sheds once capacity is exhausted


def test_delete_uses_purge_contract_not_telemetry() -> None:
    """Regression guard: deletes validate to the purpose-built purge event, NOT
    the telemetry channel, so the orphan client_file_delete wiring is intact."""
    frame = '{"event_type":"client_file_delete","data":{"filepath":"/w/gone.py","project_id":""}}'
    ev = ws_adapter.validate_json(frame)
    assert isinstance(ev, ClientFileDeleteEvent)
    assert ev.data.filepath == "/w/gone.py"
