"""Machine-readable benchmark report builder.

Turns a flat list of per-problem, per-arm metrics into a single auditable JSON
document: per-test verdict rows, per-arm aggregates with Wilson confidence
intervals, the two hypothesis verdicts, the TCI-stratified routing study (token
efficiency per complexity bucket), and the ablation deltas that attribute value
to each capability.

The builder is a pure function with no I/O and no wall-clock: the same metrics
always produce a byte-identical document, which is what makes a run reproducible
at a pinned corpus revision. Two rigor guarantees are inherited from the routing
study and applied to the precision hypothesis as well:

* **Anchored, strictly-paired comparisons.** A problem enters a comparison only
  when both compared arms produced a metric, and any complexity gate is decided
  by a single reference arm, so two arms are always measured over the same
  problem set even when a cloud model's complexity score jitters across runs.
* **Honest undefined states.** A proportion over zero resolved problems is
  undefined, not zero or one; a hypothesis with no qualifying data is undefined,
  not satisfied. The precision hypothesis additionally distinguishes a genuine
  uplift-from-nothing from a mutual failure that the naive inequality would
  silently pass.

Serialization is strict: non-finite floats are refused (they are invalid JSON
that a downstream consumer cannot parse) and every list is emitted in a
canonical order independent of the input ordering.
"""
from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from math import sqrt
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

from jsonschema import Draft7Validator  # type: ignore[import-untyped]

from tests.benchmark.arms import AblationArm
from tests.benchmark.metrics import ProblemMetrics
from tests.benchmark.routing_study import (
    TCI_BUCKETS,
    RoutingStudyTable,
    build_routing_study,
)

# Semantic version of the report contract. A breaking change to the document
# shape bumps the major; an additive field bumps the minor.
REPORT_SCHEMA_VERSION = "1.0.0"

# The harness's fixed seed (recorded so a report declares the run it came from).
_DEFAULT_SEED = 42

# Precision-uplift threshold: the full pipeline must resolve at least this
# multiple of the zero-shot control's rate on complex problems.
_H1_UPLIFT_FACTOR = 1.25
# Complexity floor (exclusive) for the precision hypothesis.
_H1_TCI_FLOOR = 60.0

_ROUTING_ARM = AblationArm.G4.value
_BASELINE_ARM = AblationArm.G4_FORCE_CLOUD.value
_CONTROL_ARM = AblationArm.G1.value

# Wilson interval z for a 95% two-sided confidence level.
_WILSON_Z = 1.96

# Canonical arm order for stable group serialization.
_ARM_ORDER: Tuple[str, ...] = tuple(a.value for a in AblationArm)

# The three capability deltas, in fixed report order: (lower, upper, isolates).
_ABLATION_PAIRS: Tuple[Tuple[str, str, str], ...] = (
    (AblationArm.G2.value, AblationArm.G3.value, "graph topology (GraphRAG)"),
    (AblationArm.G3.value, AblationArm.G4.value, "ReAct self-correction loop"),
    (
        AblationArm.G4.value,
        AblationArm.G4_FORCE_CLOUD.value,
        "TCI routing vs forced cloud",
    ),
)


def wilson_interval(
    successes: int, n: int, z: float = _WILSON_Z
) -> Tuple[float, float]:
    """Wilson score interval for a binomial proportion.

    Correct near 0 and 1 and for small n, where the normal approximation is not.
    Returns ``(0.0, 0.0)`` for ``n == 0`` — an undefined proportion signalled by
    the zero count rather than a misleading full-width interval.
    """
    if n <= 0:
        return (0.0, 0.0)
    phat = successes / n
    denom = 1.0 + z * z / n
    centre = (phat + z * z / (2 * n)) / denom
    margin = (z * sqrt((phat * (1.0 - phat) + z * z / (4 * n)) / n)) / denom
    return (max(0.0, centre - margin), min(1.0, centre + margin))


# --------------------------------------------------------------------------- #
# Report value objects                                                          #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class VerdictRow:
    """One problem's outcome under one arm."""

    problem_id: str
    arm: str
    verdict: Optional[str]
    tci: Optional[float]
    css: Optional[float]
    tokens_local: float
    tokens_cloud: float
    tokens_total: float
    est_usd: float
    latency_s: float

    def to_dict(self) -> Dict[str, Any]:
        return {
            "problem_id": self.problem_id,
            "arm": self.arm,
            "verdict": self.verdict,
            "tci": self.tci,
            "css": self.css,
            "tokens_local": self.tokens_local,
            "tokens_cloud": self.tokens_cloud,
            "tokens_total": self.tokens_total,
            "est_usd": self.est_usd,
            "latency_s": self.latency_s,
        }


@dataclass(frozen=True)
class GroupAggregate:
    """Per-arm resolution proportion with a Wilson confidence interval."""

    arm: str
    n: int
    resolved: int
    resolve_at_3: float
    wilson_lo: float
    wilson_hi: float
    tokens_total: float
    est_usd: float

    def to_dict(self) -> Dict[str, Any]:
        return {
            "arm": self.arm,
            "n": self.n,
            "resolved": self.resolved,
            "resolve_at_3": self.resolve_at_3,
            "wilson_lo": self.wilson_lo,
            "wilson_hi": self.wilson_hi,
            "tokens_total": self.tokens_total,
            "est_usd": self.est_usd,
        }


@dataclass(frozen=True)
class HypothesisVerdict:
    """A pass/fail/undefined verdict for one experimental hypothesis."""

    name: str
    statement: str
    # True = holds, False = refuted, None = undefined (insufficient data).
    holds: Optional[bool]
    n: int
    resolve_treatment: Optional[float]
    resolve_baseline: Optional[float]
    threshold: Optional[float]
    token_savings_ratio: Optional[float]
    resolve_retention: Optional[float]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "statement": self.statement,
            "holds": self.holds,
            "n": self.n,
            "resolve_treatment": self.resolve_treatment,
            "resolve_baseline": self.resolve_baseline,
            "threshold": self.threshold,
            "token_savings_ratio": self.token_savings_ratio,
            "resolve_retention": self.resolve_retention,
        }


@dataclass(frozen=True)
class AblationDelta:
    """The resolution and token difference isolating one capability."""

    name: str
    lower_arm: str
    upper_arm: str
    isolates: str
    n: int
    delta_resolve_at_3: Optional[float]
    delta_tokens_total: Optional[float]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "lower_arm": self.lower_arm,
            "upper_arm": self.upper_arm,
            "isolates": self.isolates,
            "n": self.n,
            "delta_resolve_at_3": self.delta_resolve_at_3,
            "delta_tokens_total": self.delta_tokens_total,
        }


@dataclass(frozen=True)
class BenchmarkReport:
    """The full machine-readable benchmark report."""

    schema_version: str
    corpus_sha: Optional[str]
    complete: bool
    seed: int
    indexing_time_s: float
    arms: Tuple[str, ...]
    verdicts: Tuple[VerdictRow, ...]
    groups: Tuple[GroupAggregate, ...]
    h1: HypothesisVerdict
    h2: HypothesisVerdict
    routing_study: RoutingStudyTable
    ablation_deltas: Tuple[AblationDelta, ...]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "corpus_sha": self.corpus_sha,
            "complete": self.complete,
            "seed": self.seed,
            "indexing_time_s": self.indexing_time_s,
            "arms": list(self.arms),
            "verdicts": [v.to_dict() for v in self.verdicts],
            "groups": [g.to_dict() for g in self.groups],
            "h1": self.h1.to_dict(),
            "h2": self.h2.to_dict(),
            "routing_study": self.routing_study.to_dict(),
            "ablation_deltas": [d.to_dict() for d in self.ablation_deltas],
        }


# --------------------------------------------------------------------------- #
# Builders                                                                      #
# --------------------------------------------------------------------------- #


def _arm_sort_key(arm: str) -> Tuple[int, str]:
    """Canonical arm order: known arms by matrix position, then unknown by name."""
    try:
        return (_ARM_ORDER.index(arm), "")
    except ValueError:
        return (len(_ARM_ORDER), arm)


def _verdict_rows(metrics: List[ProblemMetrics]) -> Tuple[VerdictRow, ...]:
    rows = [
        VerdictRow(
            problem_id=m.problem_id,
            arm=m.arm,
            verdict=m.verdict,
            tci=m.tci,
            css=m.css,
            tokens_local=m.tokens_local,
            tokens_cloud=m.tokens_cloud,
            tokens_total=m.tokens_local + m.tokens_cloud,
            est_usd=m.est_usd,
            latency_s=m.latency_s,
        )
        for m in metrics
    ]
    rows.sort(key=lambda r: (r.problem_id, _arm_sort_key(r.arm), r.arm))
    return tuple(rows)


def _group_aggregates(metrics: List[ProblemMetrics]) -> Tuple[GroupAggregate, ...]:
    """One aggregate per arm. ``n`` counts only scored problems (verdict set)."""
    by_arm: Dict[str, List[ProblemMetrics]] = {}
    for m in metrics:
        by_arm.setdefault(m.arm, []).append(m)

    aggregates: List[GroupAggregate] = []
    for arm in sorted(by_arm, key=_arm_sort_key):
        arm_metrics = by_arm[arm]
        scored = [m for m in arm_metrics if m.verdict is not None]
        n = len(scored)
        resolved = sum(1 for m in scored if m.verdict == "passed")
        resolve_at_3 = (resolved / n) if n else 0.0
        lo, hi = wilson_interval(resolved, n)
        aggregates.append(
            GroupAggregate(
                arm=arm,
                n=n,
                resolved=resolved,
                resolve_at_3=resolve_at_3,
                wilson_lo=lo,
                wilson_hi=hi,
                tokens_total=sum(m.tokens_local + m.tokens_cloud for m in arm_metrics),
                est_usd=sum(m.est_usd for m in arm_metrics),
            )
        )
    return tuple(aggregates)


def _by_problem(metrics: List[ProblemMetrics]) -> Dict[str, Dict[str, ProblemMetrics]]:
    grouped: Dict[str, Dict[str, ProblemMetrics]] = {}
    for m in metrics:
        grouped.setdefault(m.problem_id, {})[m.arm] = m
    return grouped


def _resolved(metric: ProblemMetrics) -> int:
    return 1 if metric.verdict == "passed" else 0


def _h1_verdict(metrics: List[ProblemMetrics]) -> HypothesisVerdict:
    """Precision uplift on complex problems: Resolve@3(G4) >= 1.25 x Resolve@3(G1).

    Inclusion is anchored to the treatment arm's complexity and strictly paired:
    a problem counts only when both G1 and G4 produced a metric and the G4
    complexity exceeds the floor. The verdict never relies on the bare
    inequality at zero — when the control resolves nothing, an uplift is True
    only if the treatment resolves something, and a mutual failure is a refuted
    hypothesis, not a vacuous pass.
    """
    statement = (
        f"Resolve@3({_ROUTING_ARM}) >= {_H1_UPLIFT_FACTOR:g} "
        f"x Resolve@3({_CONTROL_ARM}) on TCI > {_H1_TCI_FLOOR:g}"
    )

    resolved_t = 0
    resolved_c = 0
    n = 0
    for arms in _by_problem(metrics).values():
        treatment = arms.get(_ROUTING_ARM)
        control = arms.get(_CONTROL_ARM)
        if treatment is None or control is None:
            continue
        if treatment.tci is None or treatment.tci <= _H1_TCI_FLOOR:
            continue
        n += 1
        resolved_t += _resolved(treatment)
        resolved_c += _resolved(control)

    if n == 0:
        return HypothesisVerdict(
            name="H1",
            statement=statement,
            holds=None,
            n=0,
            resolve_treatment=None,
            resolve_baseline=None,
            threshold=None,
            token_savings_ratio=None,
            resolve_retention=None,
        )

    resolve_t = resolved_t / n
    resolve_c = resolved_c / n
    if resolve_c == 0.0:
        holds = resolve_t > 0.0
    else:
        holds = resolve_t >= _H1_UPLIFT_FACTOR * resolve_c

    return HypothesisVerdict(
        name="H1",
        statement=statement,
        holds=holds,
        n=n,
        resolve_treatment=resolve_t,
        resolve_baseline=resolve_c,
        threshold=_H1_UPLIFT_FACTOR * resolve_c,
        token_savings_ratio=None,
        resolve_retention=None,
    )


def _h2_verdict(table: RoutingStudyTable) -> HypothesisVerdict:
    """Cost efficiency from the routing study's overall stratum.

    Holds only when the overall savings and retention thresholds are both met.
    When the baseline retains nothing (or spends nothing) a threshold is
    undefined and the verdict is undefined — never a false pass.
    """
    overall = table.overall
    if overall.meets_savings is None or overall.meets_retention is None:
        holds: Optional[bool] = None
    else:
        holds = overall.meets_savings and overall.meets_retention

    statement = (
        f"Resolve@3({_ROUTING_ARM}) >= 0.95 x Resolve@3({_BASELINE_ARM}) "
        f"and tokens({_ROUTING_ARM}) <= 0.60 x tokens({_BASELINE_ARM})"
    )
    return HypothesisVerdict(
        name="H2",
        statement=statement,
        holds=holds,
        n=overall.routing.n,
        resolve_treatment=overall.routing.resolve_at_3,
        resolve_baseline=overall.baseline.resolve_at_3,
        threshold=None,
        token_savings_ratio=overall.token_savings_ratio,
        resolve_retention=overall.resolve_retention,
    )


def _ablation_deltas(metrics: List[ProblemMetrics]) -> Tuple[AblationDelta, ...]:
    """The three capability deltas, each over the two arms' paired-scored set."""
    by_problem = _by_problem(metrics)
    deltas: List[AblationDelta] = []
    for lower, upper, isolates in _ABLATION_PAIRS:
        resolved_lower = 0
        resolved_upper = 0
        tokens_lower = 0.0
        tokens_upper = 0.0
        n = 0
        for arms in by_problem.values():
            lo_m = arms.get(lower)
            up_m = arms.get(upper)
            if lo_m is None or up_m is None:
                continue
            if lo_m.verdict is None or up_m.verdict is None:
                continue
            n += 1
            resolved_lower += _resolved(lo_m)
            resolved_upper += _resolved(up_m)
            tokens_lower += lo_m.tokens_local + lo_m.tokens_cloud
            tokens_upper += up_m.tokens_local + up_m.tokens_cloud

        if n == 0:
            delta_resolve: Optional[float] = None
            delta_tokens: Optional[float] = None
        else:
            delta_resolve = (resolved_upper / n) - (resolved_lower / n)
            delta_tokens = tokens_upper - tokens_lower

        deltas.append(
            AblationDelta(
                name=f"{lower}->{upper}",
                lower_arm=lower,
                upper_arm=upper,
                isolates=isolates,
                n=n,
                delta_resolve_at_3=delta_resolve,
                delta_tokens_total=delta_tokens,
            )
        )
    return tuple(deltas)


def build_report(
    metrics: List[ProblemMetrics],
    *,
    corpus_sha: Optional[str],
    complete: bool,
    indexing_time_s: float = 0.0,
    seed: int = _DEFAULT_SEED,
) -> BenchmarkReport:
    """Aggregate flat per-arm metrics into a deterministic benchmark report."""
    arms_present = tuple(sorted({m.arm for m in metrics}, key=_arm_sort_key))
    routing_study = build_routing_study(
        metrics, routing_arm=_ROUTING_ARM, baseline_arm=_BASELINE_ARM
    )
    return BenchmarkReport(
        schema_version=REPORT_SCHEMA_VERSION,
        corpus_sha=corpus_sha,
        complete=complete,
        seed=seed,
        indexing_time_s=indexing_time_s,
        arms=arms_present,
        verdicts=_verdict_rows(metrics),
        groups=_group_aggregates(metrics),
        h1=_h1_verdict(metrics),
        h2=_h2_verdict(routing_study),
        routing_study=routing_study,
        ablation_deltas=_ablation_deltas(metrics),
    )


# --------------------------------------------------------------------------- #
# Schema + serialization                                                        #
# --------------------------------------------------------------------------- #

_NULLABLE_NUMBER = {"type": ["number", "null"]}
_NULLABLE_BOOL = {"type": ["boolean", "null"]}


def _object(required: List[str], properties: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": required,
        "properties": properties,
    }


_STRATUM_CELL_SCHEMA = _object(
    [
        "arm",
        "bucket",
        "n",
        "resolved",
        "resolve_at_3",
        "tokens_total",
        "tokens_local",
        "tokens_cloud",
        "est_usd",
        "tokens_per_resolved",
    ],
    {
        "arm": {"type": "string"},
        "bucket": {"type": "string"},
        "n": {"type": "integer"},
        "resolved": {"type": "integer"},
        "resolve_at_3": {"type": "number"},
        "tokens_total": {"type": "number"},
        "tokens_local": {"type": "number"},
        "tokens_cloud": {"type": "number"},
        "est_usd": {"type": "number"},
        "tokens_per_resolved": _NULLABLE_NUMBER,
    },
)

_H2_STRATUM_SCHEMA = _object(
    [
        "bucket",
        "routing",
        "baseline",
        "token_savings_ratio",
        "resolve_retention",
        "meets_savings",
        "meets_retention",
    ],
    {
        "bucket": {"type": "string"},
        "routing": {"$ref": "#/definitions/stratum_cell"},
        "baseline": {"$ref": "#/definitions/stratum_cell"},
        "token_savings_ratio": _NULLABLE_NUMBER,
        "resolve_retention": _NULLABLE_NUMBER,
        "meets_savings": _NULLABLE_BOOL,
        "meets_retention": _NULLABLE_BOOL,
    },
)

REPORT_SCHEMA: Dict[str, Any] = {
    "$schema": "http://json-schema.org/draft-07/schema#",
    "title": "AILIENANT benchmark report",
    "type": "object",
    "additionalProperties": False,
    "definitions": {
        "stratum_cell": _STRATUM_CELL_SCHEMA,
        "h2_stratum": _H2_STRATUM_SCHEMA,
    },
    "required": [
        "schema_version",
        "corpus_sha",
        "complete",
        "seed",
        "indexing_time_s",
        "arms",
        "verdicts",
        "groups",
        "h1",
        "h2",
        "routing_study",
        "ablation_deltas",
    ],
    "properties": {
        "schema_version": {"type": "string"},
        "corpus_sha": {"type": ["string", "null"]},
        "complete": {"type": "boolean"},
        "seed": {"type": "integer"},
        "indexing_time_s": {"type": "number"},
        "arms": {"type": "array", "items": {"type": "string"}},
        "verdicts": {
            "type": "array",
            "items": _object(
                [
                    "problem_id",
                    "arm",
                    "verdict",
                    "tci",
                    "css",
                    "tokens_local",
                    "tokens_cloud",
                    "tokens_total",
                    "est_usd",
                    "latency_s",
                ],
                {
                    "problem_id": {"type": "string"},
                    "arm": {"type": "string"},
                    "verdict": {
                        "type": ["string", "null"],
                        "enum": ["passed", "failed", None],
                    },
                    "tci": _NULLABLE_NUMBER,
                    "css": _NULLABLE_NUMBER,
                    "tokens_local": {"type": "number"},
                    "tokens_cloud": {"type": "number"},
                    "tokens_total": {"type": "number"},
                    "est_usd": {"type": "number"},
                    "latency_s": {"type": "number"},
                },
            ),
        },
        "groups": {
            "type": "array",
            "items": _object(
                [
                    "arm",
                    "n",
                    "resolved",
                    "resolve_at_3",
                    "wilson_lo",
                    "wilson_hi",
                    "tokens_total",
                    "est_usd",
                ],
                {
                    "arm": {"type": "string"},
                    "n": {"type": "integer"},
                    "resolved": {"type": "integer"},
                    "resolve_at_3": {"type": "number"},
                    "wilson_lo": {"type": "number"},
                    "wilson_hi": {"type": "number"},
                    "tokens_total": {"type": "number"},
                    "est_usd": {"type": "number"},
                },
            ),
        },
        "h1": {"$ref": "#/definitions/hypothesis"},
        "h2": {"$ref": "#/definitions/hypothesis"},
        "routing_study": _object(
            [
                "routing_arm",
                "baseline_arm",
                "strata",
                "overall",
                "dropped_no_tci",
                "dropped_unpaired",
            ],
            {
                "routing_arm": {"type": "string"},
                "baseline_arm": {"type": "string"},
                "strata": {
                    "type": "array",
                    "items": {"$ref": "#/definitions/h2_stratum"},
                },
                "overall": {"$ref": "#/definitions/h2_stratum"},
                "dropped_no_tci": {"type": "integer"},
                "dropped_unpaired": {"type": "integer"},
            },
        ),
        "ablation_deltas": {
            "type": "array",
            "items": _object(
                [
                    "name",
                    "lower_arm",
                    "upper_arm",
                    "isolates",
                    "n",
                    "delta_resolve_at_3",
                    "delta_tokens_total",
                ],
                {
                    "name": {"type": "string"},
                    "lower_arm": {"type": "string"},
                    "upper_arm": {"type": "string"},
                    "isolates": {"type": "string"},
                    "n": {"type": "integer"},
                    "delta_resolve_at_3": _NULLABLE_NUMBER,
                    "delta_tokens_total": _NULLABLE_NUMBER,
                },
            ),
        },
    },
}

# The hypothesis sub-schema is shared by h1 and h2 via $ref.
REPORT_SCHEMA["definitions"]["hypothesis"] = _object(
    [
        "name",
        "statement",
        "holds",
        "n",
        "resolve_treatment",
        "resolve_baseline",
        "threshold",
        "token_savings_ratio",
        "resolve_retention",
    ],
    {
        "name": {"type": "string"},
        "statement": {"type": "string"},
        "holds": _NULLABLE_BOOL,
        "n": {"type": "integer"},
        "resolve_treatment": _NULLABLE_NUMBER,
        "resolve_baseline": _NULLABLE_NUMBER,
        "threshold": _NULLABLE_NUMBER,
        "token_savings_ratio": _NULLABLE_NUMBER,
        "resolve_retention": _NULLABLE_NUMBER,
    },
)


def validate_report(obj: Dict[str, Any]) -> None:
    """Raise if ``obj`` is not a valid benchmark report document.

    Validates the schema itself first (catching a malformed schema) and then the
    document against it.
    """
    Draft7Validator.check_schema(REPORT_SCHEMA)
    Draft7Validator(REPORT_SCHEMA).validate(obj)


def serialize_report(obj: Dict[str, Any]) -> str:
    """Validate and serialize a report dict to canonical, strict JSON.

    ``allow_nan=False`` rejects non-finite floats: a leaked ``Infinity``/``NaN``
    is valid Python ``json`` output but invalid JSON that a downstream consumer
    cannot parse, so it is refused at the source rather than written.
    """
    validate_report(obj)
    return json.dumps(obj, allow_nan=False, sort_keys=True, indent=2) + "\n"


def write_report(report: Union[BenchmarkReport, Dict[str, Any]], path: Path) -> Path:
    """Atomically write a validated report to ``path``.

    The document is serialized (and so validated and finiteness-checked) before
    any filesystem mutation, so a rejected report leaves nothing behind. The
    temp file is created in the destination's own directory to keep the rename
    on a single filesystem (a cross-device ``os.replace`` raises ``EXDEV``), and
    its handle is closed before the rename (a still-open temp file blocks
    ``os.replace`` on Windows).
    """
    obj = report.to_dict() if isinstance(report, BenchmarkReport) else report
    payload = serialize_report(obj)

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=str(path.parent),
        prefix=path.name + ".",
        suffix=".tmp",
        delete=False,
    )
    tmp_path = Path(handle.name)
    try:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
        handle.close()
        os.replace(str(tmp_path), str(path))
    except BaseException:
        try:
            handle.close()
        except OSError:
            pass
        tmp_path.unlink(missing_ok=True)
        raise
    return path
