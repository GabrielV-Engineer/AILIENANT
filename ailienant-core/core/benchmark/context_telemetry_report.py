"""Context-utilization telemetry aggregator.

Reads the flat, pipe-delimited telemetry sink (``<workspace_root>/.ailienant_telemetry.log``
plus its rotated ``.1``/``.2``/``.3`` siblings), extracts the ``CONTEXT`` records
emitted by ``core.telemetry_log.log_context_utilization``, and answers one
question: **does context compaction/eviction actually occur in real sessions
often enough to justify importance-aware retention work?**

The metric is a per-session event frequency, not a utilization average — the
summarizer is a controller that holds utilization below its own threshold, so an
average would be structurally suppressed. An *event* is a summarizer record that
actually crossed the compaction threshold (reconstructed exactly from the raw
token counts, not the display-rounded ratio) or a pipeline record that actually
evicted/truncated. The report tallies the fraction of non-trivial sessions with
at least one event and recommends GO / NO-GO / INSUFFICIENT_DATA against a bar.

Pure and hermetic: the parse/aggregate functions import nothing from the agent
runtime, so they are cheap to unit-test against fixture strings. The CLI is the
only place that resolves the live threshold/keep-window constants.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional

# The category token the sink writes for these records, and the truncation marker
# the sink appends to any over-long line (both single-sourced from the emitter's
# observable contract, not re-derived).
_CATEGORY = "CONTEXT"
_ROTATION_SUFFIXES = (".1", ".2", ".3")


@dataclass(frozen=True)
class ContextRecord:
    """One parsed context-utilization sample from the telemetry sink."""

    session: str
    source: str  # "summarizer" | "pipeline"
    ratio: float
    total_tokens: int
    token_budget: int
    turns: int
    l4_evicted: Optional[int]     # pipeline-only; None for summarizer records
    l5_truncated: Optional[bool]  # pipeline-only; None for summarizer records


@dataclass(frozen=True)
class GateReport:
    """Aggregate verdict over a corpus of ContextRecords."""

    total_records: int
    summarizer_records: int
    pipeline_records: int
    total_sessions: int
    nontrivial_sessions: int
    sessions_with_event: int
    event_session_fraction: float
    recommendation: str  # "GO" | "NO-GO" | "INSUFFICIENT_DATA"
    threshold_ratio: float
    bar: float
    min_sessions: int


def read_telemetry_logs(log_path: Path) -> str:
    """Return the concatenated text of a telemetry log and its rotated siblings.

    A missing base file yields ``""`` (a fresh install has no log yet). Each file
    is read independently so one unreadable rotation never sinks the rest; records
    are grouped by session downstream, so file/read order does not matter.
    """
    parts: List[str] = []
    for candidate in (log_path, *(log_path.with_name(log_path.name + s) for s in _ROTATION_SUFFIXES)):
        try:
            parts.append(candidate.read_text(encoding="utf-8"))
        except OSError:
            continue  # absent rotation or transient read error — skip this file only
    return "\n".join(parts)


def parse_context_records(text: str) -> List[ContextRecord]:
    """Parse every well-formed CONTEXT line in ``text`` into a ContextRecord.

    Lines that are not CONTEXT records (WS/NODE/INDEX telemetry, blank lines) are
    skipped. A CONTEXT line whose fields are truncated or malformed is skipped by
    catching only the narrow errors bad *data* can raise (ValueError from a numeric
    conversion, KeyError from a missing required field). A programming error in this
    parser (NameError/AttributeError/...) is deliberately NOT caught, so it surfaces
    rather than masquerading as "malformed data".
    """
    records: List[ContextRecord] = []
    for raw in text.splitlines():
        segments = raw.split(" | ")
        head = segments[0].split()
        if not head or head[-1] != _CATEGORY:
            continue
        fields: Dict[str, str] = {}
        for seg in segments[1:]:
            key, sep, value = seg.partition("=")
            if sep:
                fields[key.strip()] = value
        try:
            records.append(
                ContextRecord(
                    session=fields["session"],
                    source=fields["source"],
                    ratio=float(fields.get("ratio", "0") or "0"),
                    total_tokens=int(fields["total_tokens"]),
                    token_budget=int(fields["token_budget"]),
                    turns=int(fields["turns"]),
                    l4_evicted=int(fields["l4_evicted"]) if "l4_evicted" in fields else None,
                    l5_truncated=(fields["l5_truncated"] == "True") if "l5_truncated" in fields else None,
                )
            )
        except (ValueError, KeyError):
            continue  # truncated/malformed data line — skip, never abort the sweep
    return records


def _is_event(rec: ContextRecord, threshold_ratio: float) -> bool:
    """True if this record represents a real compaction/eviction event.

    Summarizer: reconstruct the exact firing condition the node uses
    (``total_tokens > int(threshold_ratio * context_window)``) from the raw counts,
    not the display-rounded ratio — a record landing exactly on the integer
    threshold did NOT fire and must not count. A zero budget (the short-history
    no-op record) can never be an event.
    Pipeline: an actual FIFO eviction or tail-truncation happened.
    """
    if rec.source == "summarizer":
        return rec.total_tokens > int(threshold_ratio * rec.token_budget)
    if rec.source == "pipeline":
        return (rec.l4_evicted or 0) > 0 or rec.l5_truncated is True
    return False


def build_gate_report(
    records: List[ContextRecord],
    *,
    threshold_ratio: float,
    keep_last_n: int,
    bar: float = 0.25,
    min_sessions: int = 10,
) -> GateReport:
    """Aggregate records into a GO / NO-GO / INSUFFICIENT_DATA recommendation.

    A session is *non-trivial* if it was long enough to be a compaction candidate
    (``max(turns) > keep_last_n``) OR it already shows an event — the latter clause
    keeps a genuine pipeline eviction whose ``turns`` reads 0 (single-shot analyst
    path) from being dropped from the denominator. GO when at least ``bar`` of the
    non-trivial sessions show an event, given an adequate sample.
    """
    by_session: Dict[str, List[ContextRecord]] = defaultdict(list)
    for rec in records:
        by_session[rec.session].append(rec)

    nontrivial = 0
    with_event = 0
    for recs in by_session.values():
        has_event = any(_is_event(r, threshold_ratio) for r in recs)
        max_turns = max((r.turns for r in recs), default=0)
        if max_turns > keep_last_n or has_event:
            nontrivial += 1
            if has_event:
                with_event += 1

    fraction = (with_event / nontrivial) if nontrivial else 0.0
    if nontrivial < min_sessions:
        recommendation = "INSUFFICIENT_DATA"
    elif fraction >= bar:
        recommendation = "GO"
    else:
        recommendation = "NO-GO"

    return GateReport(
        total_records=len(records),
        summarizer_records=sum(1 for r in records if r.source == "summarizer"),
        pipeline_records=sum(1 for r in records if r.source == "pipeline"),
        total_sessions=len(by_session),
        nontrivial_sessions=nontrivial,
        sessions_with_event=with_event,
        event_session_fraction=fraction,
        recommendation=recommendation,
        threshold_ratio=threshold_ratio,
        bar=bar,
        min_sessions=min_sessions,
    )


def format_report(report: GateReport) -> str:
    """Render a GateReport as a human-readable multi-line summary."""
    return "\n".join(
        (
            "Context-utilization gate report",
            f"  records            : {report.total_records} "
            f"(summarizer={report.summarizer_records}, pipeline={report.pipeline_records})",
            f"  sessions           : {report.total_sessions} "
            f"(non-trivial={report.nontrivial_sessions})",
            f"  sessions w/ event  : {report.sessions_with_event}",
            f"  event fraction     : {report.event_session_fraction:.3f} "
            f"(bar={report.bar:.2f}, min_sessions={report.min_sessions})",
            f"  threshold_ratio    : {report.threshold_ratio:.2f}",
            f"  RECOMMENDATION     : {report.recommendation}",
        )
    )


if __name__ == "__main__":  # pragma: no cover - thin CLI wrapper
    import sys

    # The live compaction threshold and retention window are single-sourced from the
    # summarizer; resolved here (not in the pure functions) so the parse/aggregate
    # path stays free of the agent-runtime import graph.
    from brain.summarizer import KEEP_LAST_N, THRESHOLD_RATIO

    target = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(".ailienant_telemetry.log")
    parsed = parse_context_records(read_telemetry_logs(target))
    print(format_report(build_gate_report(parsed, threshold_ratio=THRESHOLD_RATIO, keep_last_n=KEEP_LAST_N)))
