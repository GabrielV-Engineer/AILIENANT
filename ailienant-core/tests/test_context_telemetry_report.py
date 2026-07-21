"""Tests for the context-utilization telemetry aggregator.

Hermetic: fixture log strings, no real telemetry and no agent-runtime imports.
Covers parsing (valid records, noise, truncated/malformed lines), the exact
event boundary, the non-trivial-includes-event denominator rule, the
GO/NO-GO/INSUFFICIENT_DATA thresholds, and rotated-file reading.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

from core.benchmark.context_telemetry_report import (
    ContextRecord,
    build_gate_report,
    parse_context_records,
    read_telemetry_logs,
)

_TS = "2026-07-20 15:00:00,123"


def _summarizer_line(session: str, total: int, budget: int, turns: int) -> str:
    ratio = (total / budget) if budget else 0.0
    return (
        f"{_TS} CONTEXT | session={session} | source=summarizer | ratio={ratio:.4f} "
        f"| total_tokens={total} | token_budget={budget} | turns={turns} | duration_s=0.0000"
    )


def _pipeline_line(
    session: str, turns: int, l4_evicted: int, l5_truncated: bool, total: int = 500, budget: int = 8192
) -> str:
    ratio = (total / budget) if budget else 0.0
    return (
        f"{_TS} CONTEXT | session={session} | source=pipeline | ratio={ratio:.4f} "
        f"| total_tokens={total} | token_budget={budget} | turns={turns} | duration_s=0.0000 "
        f"| l1=100 | l2=80 | l3=60 | l4=140 | l5=20 | l4_evicted={l4_evicted} | l5_truncated={l5_truncated}"
    )


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

def test_parse_extracts_only_context_records() -> None:
    text = "\n".join(
        [
            _summarizer_line("s1", total=7000, budget=8192, turns=12),
            f"{_TS} NODE | session=s1 | from=summarize_history | to=planner_agent | reason=x",
            f"{_TS} WS | dir=out | event=server_token_chunk | session=s1 | data=hi",
            _pipeline_line("s2", turns=0, l4_evicted=3, l5_truncated=False),
            "",
        ]
    )
    records = parse_context_records(text)
    assert len(records) == 2
    assert records[0].source == "summarizer" and records[0].total_tokens == 7000
    assert records[1].source == "pipeline" and records[1].l4_evicted == 3
    assert records[1].l5_truncated is False
    # Summarizer records carry no layer fields.
    assert records[0].l4_evicted is None and records[0].l5_truncated is None


def test_parse_skips_truncated_and_malformed_lines() -> None:
    truncated = f"{_TS} CONTEXT | session=s3 | source=summarizer | ratio=0.90 | total_tokens=73…"
    malformed = f"{_TS} CONTEXT | session=s4 | source=summarizer | total_tokens=abc | token_budget=100 | turns=9"
    good = _summarizer_line("s5", total=9000, budget=10000, turns=20)
    records = parse_context_records("\n".join([truncated, malformed, good]))
    assert [r.session for r in records] == ["s5"]


# ---------------------------------------------------------------------------
# Exact event boundary
# ---------------------------------------------------------------------------

def _report_of(*lines: str, **kw: Any) -> Any:
    kw.setdefault("threshold_ratio", 0.80)
    kw.setdefault("keep_last_n", 5)
    return build_gate_report(parse_context_records("\n".join(lines)), **kw)


def test_summarizer_event_boundary_is_exact_not_rounded() -> None:
    # int(0.80 * 1000) == 800. total == 800 did NOT fire; total == 801 did.
    on_threshold = _summarizer_line("edge_no", total=800, budget=1000, turns=20)
    over_threshold = _summarizer_line("edge_yes", total=801, budget=1000, turns=20)
    recs = parse_context_records("\n".join([on_threshold, over_threshold]))
    report = build_gate_report(recs, threshold_ratio=0.80, keep_last_n=5, min_sessions=1)
    # Two non-trivial sessions; only the strictly-over one is an event.
    assert report.nontrivial_sessions == 2
    assert report.sessions_with_event == 1


def test_zero_budget_short_path_record_is_never_an_event() -> None:
    # The no-op short-history record emits total_tokens=0, token_budget=0.
    line = _summarizer_line("short", total=0, budget=0, turns=3)
    report = build_gate_report(parse_context_records(line), threshold_ratio=0.80, keep_last_n=5, min_sessions=1)
    assert report.sessions_with_event == 0
    assert report.nontrivial_sessions == 0  # turns=3 <= 5 and no event → trivial


# ---------------------------------------------------------------------------
# Denominator rule: non-trivial includes any session with an event
# ---------------------------------------------------------------------------

def test_pipeline_eviction_with_zero_turns_is_counted() -> None:
    # A genuine eviction on the single-shot analyst path reports turns=0; it must
    # still count as a non-trivial session with an event, not be dropped.
    line = _pipeline_line("analyst", turns=0, l4_evicted=2, l5_truncated=False)
    report = build_gate_report(parse_context_records(line), threshold_ratio=0.80, keep_last_n=5, min_sessions=1)
    assert report.nontrivial_sessions == 1
    assert report.sessions_with_event == 1
    assert report.event_session_fraction == 1.0


def test_trivial_sessions_excluded_from_denominator() -> None:
    # Short, event-free sessions do not dilute the denominator.
    lines = [_summarizer_line(f"short{i}", total=10, budget=8192, turns=4) for i in range(5)]
    report = build_gate_report(
        parse_context_records("\n".join(lines)), threshold_ratio=0.80, keep_last_n=5, min_sessions=1
    )
    assert report.total_sessions == 5
    assert report.nontrivial_sessions == 0


# ---------------------------------------------------------------------------
# GO / NO-GO / INSUFFICIENT_DATA
# ---------------------------------------------------------------------------

def _nontrivial_no_event(session: str) -> str:
    return _summarizer_line(session, total=100, budget=8192, turns=20)  # long but under threshold


def _nontrivial_event(session: str) -> str:
    return _summarizer_line(session, total=8000, budget=8192, turns=20)  # fired compaction


def test_insufficient_data_below_min_sessions() -> None:
    lines = [_nontrivial_event(f"s{i}") for i in range(4)]
    report = _report_of(*lines, min_sessions=10)
    assert report.recommendation == "INSUFFICIENT_DATA"


def test_go_when_fraction_meets_bar() -> None:
    events = [_nontrivial_event(f"e{i}") for i in range(4)]
    quiet = [_nontrivial_no_event(f"q{i}") for i in range(8)]
    report = _report_of(*events, *quiet, bar=0.25, min_sessions=10)
    assert report.nontrivial_sessions == 12
    assert report.sessions_with_event == 4
    assert report.recommendation == "GO"  # 4/12 = 0.333 >= 0.25


def test_no_go_when_fraction_below_bar() -> None:
    events = [_nontrivial_event("e0")]
    quiet = [_nontrivial_no_event(f"q{i}") for i in range(11)]
    report = _report_of(*events, *quiet, bar=0.25, min_sessions=10)
    assert report.nontrivial_sessions == 12
    assert report.sessions_with_event == 1
    assert report.recommendation == "NO-GO"  # 1/12 = 0.083 < 0.25


# ---------------------------------------------------------------------------
# Rotation reading
# ---------------------------------------------------------------------------

def test_read_missing_log_returns_empty(tmp_path: Path) -> None:
    assert read_telemetry_logs(tmp_path / ".ailienant_telemetry.log") == ""


def test_read_concatenates_base_and_rotations(tmp_path: Path) -> None:
    base = tmp_path / ".ailienant_telemetry.log"
    base.write_text(_summarizer_line("base", total=9000, budget=10000, turns=20), encoding="utf-8")
    (tmp_path / ".ailienant_telemetry.log.1").write_text(
        _summarizer_line("rot1", total=9000, budget=10000, turns=20), encoding="utf-8"
    )
    text = read_telemetry_logs(base)
    sessions = {r.session for r in parse_context_records(text)}
    assert sessions == {"base", "rot1"}
