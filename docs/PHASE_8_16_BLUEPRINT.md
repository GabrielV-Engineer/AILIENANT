# Division 8.16 Blueprint — Importance-Aware Session Memory

**Status:** 8.16.0 instrumentation + gate-resolution mechanism SHIPPED — Decision Gate **PROVISIONAL**, pending real telemetry accrual.
**Evaluated against:** a **compaction-event-frequency** criterion (§2) — NOT a utilization average, which is structurally unresolvable (the summarizer thermostat suppresses it; see §2).
**Decision:** Not yet GO or NO-GO. 8.16.0 ships the `CONTEXT`-category telemetry instrumentation, a synthetic corpus generator, and the `core/benchmark/context_telemetry_report.py` aggregator that reads real telemetry against the corrected criterion. The binding signal is real dogfood usage data, which has not yet accrued (cold start — no log exists yet). 8.16.1-8.16.4 remain neither built nor struck until a real verdict is recorded.

---

## 1. Rationale

Division 8.16 uplifts the existing `brain/summarizer.py::StateSummarizer` — a live, LLM-backed compaction node (`THRESHOLD_RATIO=0.80`, `KEEP_LAST_N=5`, wired via `on_state_compacted`) — rather than building a parallel compaction mechanism. Before committing to importance-aware retention, graduated thresholds, or a session-boundary summary artifact (8.16.1-8.16.3), 8.16.0 is a telemetry-only spike: instrument the two functions that already decide when context gets compacted, observe real utilization, and only build the rest of the division if the data justifies it.

**Hard gate:** if context compaction/eviction essentially never occurs in real sessions, this division is NO-GO — 8.16.1-8.16.4 are struck from the WBS rather than built speculatively. (The manifest's original wording — "median utilization approaches `THRESHOLD_RATIO=0.80`" — is corrected in §2; it is structurally unresolvable.)

## 2. Decision Gate: GO/NO-GO

**The original criterion is structurally unresolvable — corrected here.** The manifest specified "median utilization across representative sessions approaches `THRESHOLD_RATIO=0.80`." That can essentially never legitimately return GO, because the summarizer is a **thermostat**: `_run_summarize_node_core` compacts exactly when `total_tokens > 0.80 × context_window`, replacing history with `[summary(≤512 tok), *recent(5)]` and resetting utilization to ~0.15. The per-turn ratio is therefore a sawtooth bounded in ~[0.15, 0.80]; its median sits ~0.40-0.45 for any healthy session regardless of usage intensity. The metric is suppressed by the very mechanism it judges — a GO reading would signal the summarizer is *broken*, not that the division is warranted.

**Corrected binding criterion — compaction-event frequency.** What the division actually cares about is whether the loss 8.16.1 prevents (a decision/error summarized away) *occurs often enough to matter*. That is a frequency/event question the shipped telemetry answers directly:

> **Event** = a `source="summarizer"` record that actually fired compaction — reconstructed exactly as `total_tokens > int(THRESHOLD_RATIO × token_budget)` (from the raw counts, not the display-rounded `ratio` field) — **or** a `source="pipeline"` record with `l4_evicted > 0` or `l5_truncated = True`. A **non-trivial session** has `max(turns) > KEEP_LAST_N` **or** already has ≥1 event (so a pipeline eviction whose `turns` reads 0 on the single-shot analyst path is never dropped from the denominator). **GO** if ≥ a material fraction (default **25%**) of non-trivial sessions fire ≥1 event, given an adequate sample (default ≥ **10** non-trivial sessions). **NO-GO** if events are vanishingly rare at adequate sample. **INSUFFICIENT_DATA** otherwise — keep collecting.

This metric is not thermostat-confounded (it counts whether the loss occurs at all, not the suppressed steady-state level) and needs no new instrumentation. Reading it is one command:
`python -m core.benchmark.context_telemetry_report <workspace>/.ailienant_telemetry.log` → prints the distribution + a GO / NO-GO / INSUFFICIENT_DATA recommendation. The pure parser/aggregator (`core/benchmark/context_telemetry_report.py`) reads the base log + its rotated siblings, defensively skips malformed/truncated lines, and applies exactly the criterion above.

**Raw-data caveat:** `turns` is not cumulative session length — it sawtooths with the thermostat, and for `source="pipeline"` it is the post-eviction L4 chunk count (a different quantity from the summarizer's message count). The gate never sums or medians `turns`; it uses per-session `max(turns)` only as a triviality floor plus the exact per-record event test.

Synthetic corpora (below) are supporting characterization only — proof the instrumentation fires end-to-end, never the deciding number. Until real volume is representative, the verdict stays **PROVISIONAL**.

### Synthetic corpus characterization (supporting data, not binding)

`core/benchmark/session_corpus.py::generate_corpus` driven through `_run_summarize_node_core` directly (`tests/test_context_telemetry.py::test_synthetic_corpus_range_characterization`), seeds `0-9` × turn counts `[10, 30, 60, 120]` × context windows `[4096, 8192, 32768, 128000]` (160 samples):

| Statistic | Value |
|---|---|
| n_samples | 160 |
| median ratio | 0.0926 |
| mean ratio | 0.2668 |
| min ratio | 0.0025 |
| max ratio | 1.4333 |

The synthetic median sits well below `THRESHOLD_RATIO=0.80` — but this is expected and uninformative on its own: the generator's word-salad messages are far shorter and less repetitive than real architectural/debugging conversation, and the parameter sweep deliberately spans small sessions (10 turns) to large (120 turns) rather than modeling any particular real usage distribution. The max (1.43, i.e. already past threshold) shows the instrumentation correctly detects over-threshold sessions when turn count and window are both large/small respectively — proving the mechanism works across the full range, not characterizing what real sessions actually look like.

**Conclusion of this pass:** instrumentation verified working end-to-end (real per-turn ratios computed and logged correctly, including the boundary case that exceeds 1.0). No verdict yet — revisit once real `CONTEXT` telemetry has accrued over a representative dogfood period.

### Dogfood protocol — how to obtain the binding data

1. Launch the backend + extension and connect a workspace (arms the sink → `<workspace_root>/.ailienant_telemetry.log`).
2. Perform ≥ `min_sessions` (default 10) real **edit-intent coding** tasks, including a few long multi-turn ones likely to cross the compaction threshold or trigger L4 eviction. Note: only edit-intent tasks enter the graph and emit `source="summarizer"` records; the Natt analyst-chat pane emits only `source="pipeline"` records; pure chat emits neither.
3. Aggregate (or copy the log aside) before ~20 MB of telemetry accrues — the sink rotates at 4×5 MB, so a very long passive-collection window would drop the oldest sessions. A bounded dogfood sprint is well under this.
4. Run `python -m core.benchmark.context_telemetry_report <workspace>/.ailienant_telemetry.log`.
5. Record the resulting GO / NO-GO in §2, flip the Status from PROVISIONAL, and: on **GO** unblock 8.16.1; on **NO-GO** strike 8.16.1-8.16.4 from the manifest (spec preserved via `~~struck~~` annotations, not deleted).

## 3. Telemetry record shape (reference)

`core/telemetry_log.py::log_context_utilization(session_id, source, total_tokens, token_budget, turn_count, duration_s, l1_tokens=None, ..., l5_truncated=None)`, emitted under the `CONTEXT` category on the existing `.ailienant_telemetry.log` sink (no new sink, no new file). `source` is `"summarizer"` (from `run_summarize_node`, layer fields absent) or `"pipeline"` (from `ContextPipeline.assemble()`, all layer fields populated). See `docs/SCHEMA_EVOLUTION.MD §33` for the `session_start_time` state channel `duration_s` derives from.

## 4. Known limitations (carried into any future 8.16.x work reading this data)

1. `duration_s` is `0.0` for every `source="pipeline"` record from the analyst-context path (`agents/analyst_context.py`) — `session_start_time` was deliberately not threaded through that path (three external callers, one of which — the Natt analyst-chat pane — has no session concept to supply it).
2. `duration_s` under-counts (resets to a fresh start) for any session whose first turn after a server restart lands on a cold L1 checkpoint — the session-start resolver deliberately has no L2/SQLite fallback, to avoid adding a per-turn database read.
3. `turn_count` for `source="pipeline"` records is the post-eviction L4 chunk count, not a raw session-length count — do not compare it directly against the summarizer's `turn_count` (`len(messages)`, pre-eviction).

## 5. Amendments (applied at 8.16.0 implementation)

- The manifest's literal `tests/benchmark/` reference for the Division 8.3 harness is stale — DEBT-038 relocated the actual reusable harness modules to `core/benchmark/` (Phase 8.10.1); `tests/benchmark/` now holds gate tests only. The new `core/benchmark/session_corpus.py` generator was added there accordingly.
- `session_start_time` (the field `duration_s` derives from) is a newly-declared additive `AIlienantGraphState` channel (`docs/SCHEMA_EVOLUTION.MD §33`), not an undeclared transient dict key — required because `brain/engine.py` compiles the graph as `StateGraph(AIlienantGraphState)`, so only declared TypedDict fields survive a checkpoint round-trip across turns.
- The summarizer's telemetry wiring uses a rename-and-wrap-with-shared-sink pattern (`_run_summarize_node_core` + a thin `run_summarize_node` wrapper) rather than instrumenting each of its 5 return points individually, specifically to avoid computing the `tiktoken` token estimate twice on a hot path that runs on every graph turn.
- The Decision-Gate criterion was corrected from median-utilization-vs-0.80 (structurally suppressed by the summarizer thermostat) to a compaction-event-frequency criterion (§2), and `core/benchmark/context_telemetry_report.py` was added to read real telemetry against it. The report tool reconstructs the summarizer firing condition exactly from `total_tokens`/`token_budget` (not the display-rounded `ratio`), and treats any session with a real event as non-trivial so a zero-`turns` pipeline eviction is never dropped from the denominator. The pipe-delimited log format it must string-parse is logged as `DEBT-109` (the Enterprise target is a typed JSONL telemetry contract).
