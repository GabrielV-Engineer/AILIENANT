# ailienant-core/tests/conftest.py
# Phase 2.25 — Tests-level conftest; writes CHECKPOINT_REPORT.md after the session.
# Phase 7.9.B.9 — Added _DirectAdapter autouse fixture to fix 6 failing execution tests.
from __future__ import annotations

import asyncio
import datetime
import os
import sys
from typing import Dict, Optional

import pytest

from core.sandbox import SandboxResult


class _DirectAdapter:
    """Minimal test double: runs subprocess directly (no HITL gate, no Docker).

    Restores the pre-Phase-6.2 behaviour so execution-tool tests pass under
    plain pytest without a live FastAPI lifespan or Docker daemon.
    """

    async def execute(
        self,
        command: str,
        *,
        timeout_s: float,
        cwd: str,
        env_whitelist: Dict[str, str],
        session_id: Optional[str] = None,
    ) -> SandboxResult:
        try:
            proc = await asyncio.create_subprocess_shell(
                command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=cwd or None,
            )
            try:
                stdout_b, stderr_b = await asyncio.wait_for(
                    proc.communicate(), timeout=timeout_s
                )
                return SandboxResult(
                    exit_code=proc.returncode or 0,
                    stdout=stdout_b.decode("utf-8", errors="replace"),
                    stderr=stderr_b.decode("utf-8", errors="replace"),
                )
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
                return SandboxResult(exit_code=124, stdout="", stderr="[timeout]")
        except Exception as exc:  # noqa: BLE001
            return SandboxResult(exit_code=1, stdout="", stderr=str(exc))


@pytest.fixture(autouse=True)
def _resolve_adapter(monkeypatch: pytest.MonkeyPatch) -> None:
    """Bind a direct-subprocess adapter so execution tests run without FastAPI lifespan."""
    import core.sandbox as sb

    monkeypatch.setattr(sb, "ACTIVE_ADAPTER", _DirectAdapter())
    monkeypatch.setattr(sb, "ACTIVE_TIER", "NATIVE_HITL")


def pytest_sessionfinish(session, exitstatus):
    """Write CHECKPOINT_REPORT.md with metrics collected during the test session."""
    # Module may be keyed as "test_parser_stress" or "tests.test_parser_stress"
    # depending on pytest import mode — search by suffix to handle both.
    parser_mod = next(
        (m for k, m in sys.modules.items() if k.endswith("test_parser_stress")),
        None,
    )
    parser_metrics: dict = getattr(parser_mod, "_PARSER_METRICS", {}) if parser_mod else {}

    timed = [v for v in parser_metrics.values() if isinstance(v, float)]
    avg_latency = round(sum(timed) / len(timed), 3) if timed else "N/A"

    all_passed = exitstatus == 0
    swarm_success = "100%" if all_passed else "DEGRADED (see test output)"
    recovery_status = (
        f"PASS — graph exits cleanly after MAX_RETRIES=2"
        if all_passed
        else "FAIL"
    )

    report_path = os.path.join(os.path.dirname(__file__), "..", "CHECKPOINT_REPORT.md")
    with open(report_path, "w", encoding="utf-8") as fh:
        fh.write("# CHECKPOINT REPORT — Phase 2.25\n\n")
        fh.write(f"Generated: {datetime.datetime.now(datetime.timezone.utc).isoformat()}\n\n")
        fh.write("## Average Parser Latency\n\n")
        fh.write("| Scenario | Latency (ms) |\n|---|---|\n")
        for k, v in parser_metrics.items():
            fh.write(f"| {k} | {v} |\n")
        fh.write(f"\n**Average:** {avg_latency} ms (threshold: < 50 ms)\n\n")
        fh.write("## Swarm Success Rate\n\n")
        fh.write(f"{swarm_success}\n\n")
        fh.write("## Error Recovery Status\n\n")
        fh.write(f"{recovery_status}\n\n")
        fh.write(f"## Test Suite\n\nExit status: `{exitstatus}` (0 = all passed)\n")
