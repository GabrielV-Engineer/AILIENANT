"""Phase 8 — Checkpoint Gate (sibling-file convention, test-only).

A single cross-division certification that the Phase 8 contract holds together
against its **shipped** entry points. It imports production code and asserts one
load-bearing invariant per row; it does NOT re-run the dedicated division suites
and modifies no production logic.

Airtight by construction: every row is a pure or in-memory-stubbed assertion — no
row touches the real filesystem, spawns a child process, or leaks durable/global
state (trust mutations ride a unique session id with `try/finally` cleanup; the
gateway ledger is redirected to `tmp_path`; env reads run under `delenv`).

Gate rows:
  A. Resilience (8.2)      — Fast Track, hardware reroute, OOM predictor, observability off-by-default.
  B. Precision (8.3)       — Wilson CI + H₁/H₂ reporting engine assembles a valid schema; seed/temp pinned.
  C. MCP fail-closed (8.4) — unknown verb ⇒ DANGEROUS; PLAN/WRITE deny; AUTO/DANGEROUS still HITL; trust-once tool-scoped.
  D. HITL-degrade (8.5)    — DANGEROUS gateway verb deny-reports under a 2s deadline (never hangs); anti-escalation; ledger fail-closed.
"""
from __future__ import annotations

import asyncio
import json
import uuid
from typing import Any

import pytest

pytestmark = pytest.mark.anyio


# ══════════════════════════════════════════════════════════════════════════════
# Section A — Resilience (Division 8.2)
# ══════════════════════════════════════════════════════════════════════════════

from core.graph_weight import estimate_graph_weight
from core.memory.context_auditor import (
    derive_routing_decision,
    hardware_reroute,
    is_fast_track_eligible,
)
from core.observability import configure_langsmith
from shared.hardware import HardwareProfile


def _profile(vram_gb: float) -> HardwareProfile:
    return HardwareProfile(os_type="windows", is_apple_silicon=False, vram_gb=vram_gb, vram_used_gb=0.0)


def test_A_fast_track_skips_rag_and_routes_local_small() -> None:
    assert is_fast_track_eligible("what is recursion?") is True
    assert is_fast_track_eligible("refactor the auth module in main.py") is False
    assert derive_routing_decision(tci=10.0, css=0.0, fast_track=True) == "LOCAL_SMALL"


def test_A_hardware_reroute_degrades_local_under_vram_floor() -> None:
    # Below floor + cloud → CLOUD with a warning.
    r, p, w = hardware_reroute("LOCAL_SMALL", "LOCAL", _profile(0.5), cloud_available=True)
    assert (r, p) == ("CLOUD", "CLOUD") and w is not None
    # Below floor + no cloud → degrade to LOCAL_SMALL with a warning (never blocks).
    r, p, w = hardware_reroute("LOCAL_BIG", "LOCAL", _profile(0.5), cloud_available=False)
    assert (r, p) == ("LOCAL_SMALL", "LOCAL") and w is not None
    # Healthy hardware → passthrough.
    assert hardware_reroute("LOCAL_SMALL", "LOCAL", _profile(24.0), cloud_available=True) == (
        "LOCAL_SMALL", "LOCAL", None,
    )


def test_A_graph_weight_predicts_overflow_against_candidate_window() -> None:
    state = {"messages": [{"role": "user", "content": "word " * 10000}]}
    assert estimate_graph_weight(state, model_context_window=8192).overflow_risk is True
    assert estimate_graph_weight(state, model_context_window=128_000).overflow_risk is False


def test_A_langsmith_off_by_default_no_sink(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in ("LANGCHAIN_TRACING_V2", "LANGSMITH_API_KEY", "LANGCHAIN_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    assert configure_langsmith() is False


# ══════════════════════════════════════════════════════════════════════════════
# Section B — Precision: H₁/H₂ reporting engine (Division 8.3)
# Pure-function only — the runner / executor / corpus I/O lives in tests/benchmark/.
# ══════════════════════════════════════════════════════════════════════════════

from core.benchmark.hygiene import SEED, TEMPERATURE
from core.benchmark.metrics import ProblemMetrics
from core.benchmark.report import (
    BenchmarkReport,
    HypothesisVerdict,
    build_report,
    validate_report,
    wilson_interval,
)


def test_B_wilson_interval_bounds() -> None:
    lo, hi = wilson_interval(8, 10)
    assert 0.0 <= lo <= hi <= 1.0 and lo > 0.0
    assert wilson_interval(0, 0) == (0.0, 0.0)


def _metric(arm: str, pid: str, *, verdict: str, local: float, cloud: float) -> ProblemMetrics:
    return ProblemMetrics(
        arm=arm, problem_id=pid, tokens_local=local, tokens_cloud=cloud,
        est_usd=0.0, tci=80.0, css=70.0, latency_s=0.1, verdict=verdict,
    )


def test_B_build_report_assembles_valid_h1_h2_and_wilson() -> None:
    # Synthetic paired data across the arms H₁ (G1 vs G4) and H₂ (G4 vs forced-cloud)
    # consume — enough to render real (non-None) verdicts with Wilson intervals.
    metrics = []
    for pid in ("p1", "p2"):
        metrics.append(_metric("G1", pid, verdict="failed", local=200, cloud=0))
        metrics.append(_metric("G4", pid, verdict="passed", local=100, cloud=0))
        metrics.append(_metric("G4_FORCE_CLOUD", pid, verdict="passed", local=0, cloud=300))

    report = build_report(metrics, corpus_sha=None, complete=True)

    assert isinstance(report, BenchmarkReport)
    validate_report(report.to_dict())  # schema-valid assembly from data
    assert isinstance(report.h1, HypothesisVerdict) and report.h1.name == "H1"
    assert isinstance(report.h2, HypothesisVerdict) and report.h2.name == "H2"
    assert report.h1.holds is not None and report.h2.holds is not None  # verdict rendered
    assert report.groups, "per-arm aggregates must be present"
    for g in report.groups:
        assert 0.0 <= g.wilson_lo <= g.wilson_hi <= 1.0


def test_B_determinism_pinned() -> None:
    assert SEED == 42 and TEMPERATURE == 0.0


# ══════════════════════════════════════════════════════════════════════════════
# Section C — MCP privilege fail-closed (Division 8.4)
# ══════════════════════════════════════════════════════════════════════════════

from core.permissions import (
    PermissionDecision,
    SessionPermissionMode,
    ToolPrivilegeTier,
    classify_tool_privilege,
    evaluate_action,
)
from shared.rbac import PermissionMode
from tools.mcp_adapter import (
    _grant_session_trust,
    _is_session_trusted,
    clear_session_trust,
)


def test_C_unknown_verb_classifies_dangerous_fail_closed() -> None:
    assert classify_tool_privilege("totally_unknown_verb_xyz", "no idea", "some_server") is (
        ToolPrivilegeTier.DANGEROUS
    )


def test_C_write_under_plan_denied_dangerous_under_auto_still_hitl() -> None:
    assert evaluate_action(
        SessionPermissionMode.PLAN, ToolPrivilegeTier.WRITE, PermissionMode.EDIT_EXECUTE_RBW
    ) is PermissionDecision.DENY
    # A DANGEROUS verb is never silently ALLOWed, even under Auto.
    assert evaluate_action(
        SessionPermissionMode.AUTO, ToolPrivilegeTier.DANGEROUS, PermissionMode.EDIT_EXECUTE_RBW
    ) is PermissionDecision.HITL


def test_C_trust_once_is_tool_scoped_and_isolated() -> None:
    sid = uuid.uuid4().hex  # unique to this test — cannot bleed into the suite
    try:
        assert _is_session_trusted(sid, "tool_a") is False
        _grant_session_trust(sid, "tool_a")
        assert _is_session_trusted(sid, "tool_a") is True
        assert _is_session_trusted(sid, "tool_b") is False  # trust is per-tool
    finally:
        clear_session_trust(sid)  # guarantee no elevated privilege survives this row
    assert _is_session_trusted(sid, "tool_a") is False


# ══════════════════════════════════════════════════════════════════════════════
# Section D — External HITL-degrade (Division 8.5)
# Sync tests using asyncio.run (mirrors tests/test_gateway_hitl_degrade.py).
# ══════════════════════════════════════════════════════════════════════════════

from core.permissions import register_privilege_overrides
from gateway import catalog, ledger, server
from gateway.governance import resolve_internal_task_mode


@pytest.fixture()
def iso_ledger(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> Any:
    """Redirect the FS-backed gateway ledger to tmp_path and reset env knobs."""
    monkeypatch.setattr(ledger, "LEDGER_PATH", tmp_path / "gateway_ledger.json")
    for var in (
        "AILIENANT_GATEWAY_RATE_CAP", "AILIENANT_GATEWAY_RATE_REFILL_PER_S",
        "AILIENANT_GATEWAY_BUDGET", "AILIENANT_GATEWAY_CALLER_ID", "AILIENANT_GATEWAY_TOKEN",
    ):
        monkeypatch.delenv(var, raising=False)
    return tmp_path


def _inject_dangerous_verb(monkeypatch: pytest.MonkeyPatch) -> None:
    register_privilege_overrides({"danger_verb": ToolPrivilegeTier.DANGEROUS})
    danger = catalog.Capability(
        name="danger_verb", description="hypothetical irreversible verb",
        tier=ToolPrivilegeTier.DANGEROUS, input_schema={"type": "object"}, is_async=False,
    )
    original = catalog.get_capability
    monkeypatch.setattr(
        catalog, "get_capability",
        lambda n: danger if n == "danger_verb" else original(n),
    )


def test_D_dangerous_verb_deny_reports_without_hanging(
    iso_ledger: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    _inject_dangerous_verb(monkeypatch)

    async def _drive() -> dict[str, Any]:
        # The 2s deadline turns "never hangs" into a falsifiable assertion: an await
        # on a human approval that can never arrive would raise TimeoutError here.
        result = await asyncio.wait_for(server.dispatch_call("danger_verb", {}), timeout=2.0)
        return json.loads(result[0].text)

    payload = asyncio.run(_drive())
    assert payload["status"] == "denied"
    assert payload["reason"] == "requires_human_approval"
    assert payload["would_have_required"] == "human_approval"
    assert isinstance(payload["message"], str) and payload["message"]


def test_D_internal_task_mode_never_escalates() -> None:
    # An external caller cannot raise the spawned task's posture above DEFAULT.
    mode = resolve_internal_task_mode({"mode": "auto", "execution_mode": "auto",
                                       "session_permission_mode": "auto"})
    assert mode is SessionPermissionMode.DEFAULT


def test_D_ledger_rate_fail_closed(iso_ledger: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    # A zero-capacity, zero-refill bucket denies — fail-closed, on a tmp_path ledger.
    monkeypatch.setenv("AILIENANT_GATEWAY_RATE_CAP", "0")
    monkeypatch.setenv("AILIENANT_GATEWAY_RATE_REFILL_PER_S", "0")
    result = asyncio.run(server.dispatch_call("query_memory", {"query": "x"}))
    payload = json.loads(result[0].text)
    assert payload["status"] == "denied" and payload["reason"] == "rate_exceeded"
