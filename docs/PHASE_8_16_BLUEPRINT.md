# Division 8.16 Blueprint — Importance-Aware Session Memory

**Status:** 8.16.0 instrumentation SHIPPED — Decision Gate **PROVISIONAL**, pending real telemetry accrual.
**Evaluated against:** the existing `THRESHOLD_RATIO=0.80` floor already enforced by `run_summarize_node` (`brain/summarizer.py`).
**Decision:** Not yet GO or NO-GO. 8.16.0 ships the `CONTEXT`-category telemetry instrumentation and a synthetic long-session corpus generator, but the binding signal is real dogfood usage data, which has not yet accrued (see §2). 8.16.1-8.16.4 remain neither built nor struck until a real verdict is recorded.

---

## 1. Rationale

Division 8.16 uplifts the existing `brain/summarizer.py::StateSummarizer` — a live, LLM-backed compaction node (`THRESHOLD_RATIO=0.80`, `KEEP_LAST_N=5`, wired via `on_state_compacted`) — rather than building a parallel compaction mechanism. Before committing to importance-aware retention, graduated thresholds, or a session-boundary summary artifact (8.16.1-8.16.3), 8.16.0 is a telemetry-only spike: instrument the two functions that already decide when context gets compacted, observe real utilization, and only build the rest of the division if the data justifies it.

**Hard gate (from the manifest):** if median utilization across representative sessions never approaches `THRESHOLD_RATIO=0.80` by a material margin, this division is NO-GO — 8.16.1-8.16.4 are struck from the WBS rather than built speculatively.

## 2. Decision Gate: GO/NO-GO

**Binding-source correction:** synthetic corpora exist to keep this decision from depending solely on production data volume — they are not a substitute for real data when it's available. A synthetic median is entirely an artifact of the generator's own parameters (seed count, turn counts, context windows); treating it as the deciding number would let whoever chose those parameters decide the division's fate. The process:

1. 8.16.0 ships the instrumentation (`core/telemetry_log.py::log_context_utilization`, wired into both `run_summarize_node` and `ContextPipeline.assemble()`). This alone does not close the gate.
2. Real `CONTEXT`-category lines accumulate in `.ailienant_telemetry.log` over a period of actual usage.
3. The **real** `source="summarizer"` ratios (`total_tokens/token_budget`, the same comparison `run_summarize_node` already makes internally against `THRESHOLD_RATIO`) become the primary signal once enough volume exists.
4. The synthetic corpus (below) is supporting characterization only — proof the instrumentation fires correctly, and a sense of what parameter regime would be needed to approach 0.80.
5. Until real volume is representative, the verdict stays **PROVISIONAL**.

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
