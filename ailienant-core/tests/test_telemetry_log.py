"""Live telemetry file sink — `.ailienant_telemetry.log` (core/telemetry_log.py).

Covers the ADR-712 binding guarantees:
  - secrets are scrubbed before they reach disk;
  - the file is written UTF-8 (survives emoji / non-ASCII paths under cp1252);
  - the RotatingFileHandler is size-bounded (rotation fires);
  - calls before configuration neither raise nor write;
  - core.telemetry.log_routing_decision mirrors a node transition into the file.

Because writes drain asynchronously on the QueueListener thread, every read-back
is preceded by shutdown_telemetry_log() (the teardown fixture) to flush+join.
"""
from pathlib import Path
from typing import Generator

import pytest

import core.telemetry_log as tlog


@pytest.fixture(autouse=True)
def _isolate_sink() -> Generator[None, None, None]:
    """Reset the module-global sink (listener + queue) between tests."""
    tlog.shutdown_telemetry_log()
    yield
    tlog.shutdown_telemetry_log()


def _read_log(root: Path) -> str:
    return (root / tlog._LOG_FILENAME).read_text(encoding="utf-8")


def test_secrets_scrubbed(tmp_path: Path) -> None:
    """An API key / Bearer token is REDACTED before it lands in the file."""
    tlog.configure_telemetry_log(str(tmp_path))
    secret = "sk-ant-api03-ABCDEFGHIJKLMNOPQRSTUVWXYZ012345"
    bearer = "Bearer abcdefghijklmnopqrstuvwxyz0123456789"
    tlog.log_ws_payload("out", "server_token_chunk", "sess-1", f"{secret} {bearer}")
    tlog.shutdown_telemetry_log()  # flush the listener before reading

    content = _read_log(tmp_path)
    assert secret not in content
    assert "abcdefghijklmnopqrstuvwxyz0123456789" not in content
    assert "REDACTED:" in content


def test_utf8_encoding(tmp_path: Path) -> None:
    """Emoji / accented paths round-trip without UnicodeEncodeError."""
    tlog.configure_telemetry_log(str(tmp_path))
    assert tlog._file_handler is not None
    assert tlog._file_handler.encoding == "utf-8"

    tlog.log_indexing_event("sess-1", "upsert", "café/🚀_módulo.py")
    tlog.shutdown_telemetry_log()

    content = _read_log(tmp_path)
    assert "café/🚀_módulo.py" in content


def test_rotation_bound(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A tiny maxBytes forces rotation — proves the GAP7 size cap is honoured."""
    monkeypatch.setattr(tlog, "_MAX_BYTES", 512)
    tlog.configure_telemetry_log(str(tmp_path))
    for i in range(200):
        tlog.log_node_transition(f"sess-{i}", "graph", "coder_agent", "node_enter padding-payload")
    tlog.shutdown_telemetry_log()

    # backupCount rotation produces .1/.2/.3 siblings once the cap is exceeded.
    rotated = list(tmp_path.glob(tlog._LOG_FILENAME + ".*"))
    assert rotated, "expected at least one rotated backup file"
    assert (tmp_path / tlog._LOG_FILENAME).stat().st_size <= tlog._MAX_BYTES * 4


def test_noop_before_configure(tmp_path: Path) -> None:
    """Logging before configure() neither raises nor writes any file."""
    # No configure_telemetry_log call — the sink has no draining listener.
    tlog.log_ws_payload("in", "client_file_update", "sess-1", "noise")
    tlog.log_node_transition("sess-1", "graph", "planner_agent", "node_enter")
    assert not (tmp_path / tlog._LOG_FILENAME).exists()


def test_node_transition_mirrors(tmp_path: Path) -> None:
    """core.telemetry.log_routing_decision mirrors a NODE line into the sink."""
    from core import telemetry

    tlog.configure_telemetry_log(str(tmp_path))
    telemetry.log_routing_decision(
        session_id="sess-1",
        source="summarize_history",
        target="planner_agent",
        reason="planner_mode_active=False",
    )
    tlog.shutdown_telemetry_log()

    content = _read_log(tmp_path)
    assert "NODE" in content
    assert "to=planner_agent" in content
    assert "from=summarize_history" in content
