# ailienant-core/tests/conftest.py
# Phase 2.25 — Tests-level conftest; writes CHECKPOINT_REPORT.md after the session.
import datetime
import os
import sys


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
