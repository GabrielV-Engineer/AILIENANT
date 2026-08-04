# Debugging Runbook

> Where to look when a real install misbehaves. Every surface listed here already exists in the
> codebase — this document is a triage map, not a new subsystem. Linked from
> [DEVELOPERS.md](../DEVELOPERS.md)'s Testing & quality gates section.

---

## 1. "What command did the agent just run, and how did it exit?" — the exec log ring

**Source:** `ailienant-core/core/exec_log.py`

An in-memory, bounded ring (`deque(maxlen=200)`) of the most recent sandbox executions. Deliberately
non-persistent — a live tail should not survive a restart, and keeping it in memory avoids write
amplification from a task that execs dozens of times. Every entry is secret-masked and
character-capped (`_OUTPUT_CAP = 2_000`, `_COMMAND_CAP = 1_000`) before it ever enters the ring.

This module is also the **sole masking site** for the Glass-Box Timeline's execution-detail channel
(`record_execution` feeds the turn-scoped `ActivitySink` the exact same masked/capped output the
dashboard uses), so the WebSocket path and the dashboard path can never diverge on what is safe to
show.

**How to query it:**
- `GET /api/v1/runtime/exec-log?tail=50&since=<seq>` (`core/exec_log.py::recent_exec_log`)
- Dashboard: Runtime panel's exec-log tail view.

**When to use it:** an agent run behaved unexpectedly and you need to know exactly what shell
commands it issued, in what order, with what exit codes — without re-running the session.

---

## 2. "What happened during this turn, step by step?" — the Glass-Box Timeline

**Source:** `ailienant-extension/src/workspace/utils/timelineBuilder.ts` +
`ailienant-extension/src/workspace/components/AgentTimeline.tsx`

The backend streams one turn as two kinds of frames: lightweight, sequence-ordered
`server_activity_event` markers (the spine) and heavier bodies on their own existing channels
(reasoning deltas, diff blocks, execution detail) correlated back to a marker by `ref`. WebSocket
delivery is ordered per-channel but **not** guaranteed ordered across channels, so a body can arrive
before or after its marker — `timelineBuilder.ts`'s upsert functions are keyed by identity (the
marker's `ref`, or a synthetic `seq:<n>` key) specifically so either arrival order produces the same
final rendering.

**How to use it:** open the session's Workspace panel and inspect the timeline directly — it is the
live, ordered reconstruction of what the agent did. For a user-reported run you weren't watching
live, the timeline rebuilds identically from the same WS frame history on reconnect/reload.

**When to use it:** you need the full narrative of a turn — what was reasoned, what tools ran, what
diffs were proposed — not just the exec log's command-level slice.

---

## 3. "Was every HITL approval genuinely resolved, and is the record intact?" — the audit chain

**Source:** `ailienant-core/core/audit.py`

Every Human-in-the-Loop resolution (approved / rejected / timeout) appends one immutable,
blake2b-chained row to `hitl_audit_log`:

```
chain_hash = blake2b(prev_chain_hash ‖ audit_id ‖ session_id ‖ request_kind
                     ‖ action_description ‖ proposed_content_hash
                     ‖ resolution ‖ resolved_at)
```

Any out-of-band mutation of a historical row breaks every subsequent link. `proposed_content` is
secrets-scrubbed before it is ever stored or hashed — no raw key enters the ledger.

**How to query it:**
- `GET /api/v1/audit/log?session_id=<id>` — the raw chain for a session.
- `GET /api/v1/audit/stats?project_id=<id>` — aggregate approval/rejection counts.
- `GET /api/v1/audit/verify` — re-walks the chain and reports whether it's intact
  (`core/audit.py::verify_chain`; a mismatch raises `AuditChainBrokenError`, caught by the
  `dead_letter_decorator` and recorded as a recoverable DLQ episode rather than silently lost).

**When to use it:** a user disputes whether they approved an action, or you suspect tampering with
the approval history — this is the tamper-evident source of truth, not the chat transcript.

---

## 4. "What's the system's actual behavior over time?" — telemetry tables

**Source:** `ailienant-core/core/telemetry.py`, `ailienant-core/core/task_service.py`

Two persisted tables answer different questions:

| Table | Answers | Read via |
|---|---|---|
| `request_latency` (one row per task) | "Is the system getting slower? What's the p50/p95/p99 for a given action?" | `core/telemetry.py::latency_percentiles` |
| `container_lifecycle` | "What sandbox containers spun up/down, and when?" | `core/telemetry.py::recent_container_events`, exposed at `GET /api/v1/runtime/lifecycle` |

Both grow unbounded today (no retention/GC wired in yet — DEBT-120); reads are windowed/clamped, so
this is safe to query on a long-running install, just not yet self-pruning.

Also useful: `core/telemetry.py::recent_routing_decisions` (which model tier a request routed to and
why) and `core/telemetry.py::recent_oom_events` (VRAM/context-window fallback history) — both cover
"why did the system pick this behavior," a different question than "what did the agent do."

**When to use it:** a performance regression report, or "is this a one-off or a pattern" — these
tables have the historical signal the exec log and timeline (both turn-scoped) don't.

---

## 5. Sandbox reliability (PTY sessions, pool exhaustion, daemon hangs)

**Source:** `ailienant-core/core/sandbox.py`, `ailienant-core/brain/agentic_cell.py`

12.14 closed a cluster of sandbox reliability defects (orphaned PTY sessions surviving idle-TTL
reaping, a hijacked exec socket leaking a thread on daemon hang, pool exhaustion degrading to
uncoordinated container sharing). The regression suites added for that closure are the fastest way
to understand or reproduce a sandbox-tier issue today:

- `ailienant-core/tests/test_sandbox_pool_resilience.py` — daemon-hang, hijacked-socket, and
  pool-queueing scenarios (`HANG*`, `PTY*`, `QUEUE*` cases).
- `ailienant-core/tests/test_agentic_cell_lifecycle.py` — orphaned-session teardown on WS disconnect
  and LangGraph run-lifecycle events (`LIFE*` cases).

**When to use it:** a user reports a stuck sandbox session, an "out of containers" error, or a
session that seems to survive after its owning task should have ended.

---

## What this runbook is not

It is not a substitute for reading the source of the module in question, and it does not cover every
subsystem — it covers the surfaces a real user-reported "something went wrong" report is most likely
to need. If you find yourself repeatedly grepping for the same thing to answer a support question,
that's a signal to add a section here, not to keep grepping.
