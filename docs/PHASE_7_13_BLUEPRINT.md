# PHASE 7.13 — Master Architectural Blueprint (The Enterprise Spinal Cord: Event-Driven Telemetry, Reactive Memory & Self-Healing)

> **Mandatory read** during every Phase 7.13 task. Survives session compactions: re-derive intent from this document. Any deviation from the binding decisions tagged **[ADR-7xx]** requires an explicit blueprint amendment in the same PR.

## Context

Phases 7.10–7.12 + 9 made AILIENANT *feel* like a product: cognitive transparency, native thinking, a context-aware analyst, an inviolable identity, and a stabilized WebView lifecycle. But the system is still fundamentally a **Pull model** — a walkie-talkie chat that only reacts when the user speaks. The IDE's reality (saves, renames, deletes, idle time) never reaches the brain on its own; the GraphRAG memory is a stale per-session snapshot; heavily engineered backend features (Planner Manual Mode, the Dreaming daemon) are orphaned because no UI or trigger reaches them; tool/schema/API failures can surface raw to the user; and there is no file-based observability channel to watch the system breathe during development.

Phase 7.13 is the **paradigm shift to a Push / Event-Driven architecture** — the "spinal cord" that connects IDE reality to the agent's cognition in real time, makes memory evolutionary, resurrects the orphaned features, adds an autonomous self-healing loop, and opens a permanent telemetry channel.

### The audit (current state vs. directive premise — verified in code)

The directive assumes greenfield; the codebase shows most pillars are *partially* built. Every 7.13 task therefore **extends/wires** rather than rebuilds, honoring the Zero-Deduplication rule.

| Pillar | Already exists (reuse) | Real gap (the 7.13 work) |
|---|---|---|
| File watchers | `onDidChangeActiveTextEditor` / `onDidChangeTextDocument` → `client_file_update`, debounced 150 ms ([ide_sync.ts:67-70](../ailienant-extension/src/ide_sync.ts#L67)); [core/io_coalescer.py](../ailienant-core/core/io_coalescer.py) debounces saves in a 500 ms window | **Missing** `onDidSaveTextDocument`, `onDidRenameFiles`, `onDidDeleteFiles`; **no** silent telemetry channel — all events share one socket |
| Reactive GraphRAG | Lazy/batch per-session indexer ([core/indexer.py](../ailienant-core/core/indexer.py)); per-file `semantic_upsert` deletes+reinserts by `(workspace_hash, file_path)` | **No** save-triggered single-file re-index; no per-file debounce pipeline |
| Dreaming | `OvernightDaemon` heartbeat stub ([brain/daemon.py](../ailienant-core/brain/daemon.py)); MCTS repair helpers in `agents/mcts_coder.py` | **Never started** by `main.py`; **no** idle trigger |
| Orphanage / Planner | Planner **Manual Mode** + Socratic `ideation_loop` exist ([agents/planner.py](../ailienant-core/agents/planner.py), [brain/ideation.py](../ailienant-core/brain/ideation.py)); WS toggle event wired | Frontend has **no Planner UI** — `plan_mode` just routes through the standard chat |
| Dashboard | overview/telemetry/memory/byom/staging panels wired | Hardware/Runtime/Rules/Audit panels **stubbed** (UI, no fetch); dead Terminal-context stub |
| Agentic loop | Validation/guardrail retry, `MAX_RETRIES=2` ([brain/guardrails.py](../ailienant-core/brain/guardrails.py)); DLQ ([core/dead_letter.py](../ailienant-core/core/dead_letter.py)); MCTS surgeon escalation | **No** `ErrorCorrectionAgent` that reads a stack trace → reads the file → proposes a fix → retries |
| Telemetry log | Routing/OOM telemetry is **SQLite-resident** ([core/telemetry.py](../ailienant-core/core/telemetry.py)) | **No** `.ailienant_telemetry.log` file sink for WS payloads / node transitions / indexing events |
| Dedup | Centralized `LLMGateway`; single telemetry sink | VFS reader wrapper duplicated (`_make_vfs_reader` in [agents/coder.py](../ailienant-core/agents/coder.py)); retry budgets scattered across 4 modules with divergent caps |

### Grounding (existing infrastructure to reuse, verified)

- [core/io_coalescer.py](../ailienant-core/core/io_coalescer.py) already debounces file-save events in a 500 ms window — the reuse shape for both the telemetry bus debounce (ADR-708) and the reactive single-file index debounce (ADR-709).
- [transport/throttler.py](../ailienant-core/transport/throttler.py) `throttled_stream` is backpressure-only (pauses when the asyncio write buffer exceeds 1 MB) — the composition point for the telemetry priority class (ADR-708).
- `SemanticMemoryManager.semantic_upsert` already deletes+reinserts a single `(workspace_hash, file_path)` row — the reuse point for incremental re-index (ADR-709). The indexer already sets low scheduler priority (nice / `BELOW_NORMAL`).
- **OCC `document_version_id`** flows through `TaskPayload` and the 7.7 Delta State Sync (`_fileVersions`, `FILE_VERSION_CHANGED`) — the divergence-discipline anchor for the Dreaming/indexing race (ADR-710), mirroring ADR-703.
- `OvernightDaemon` (`brain/daemon.py`) is a started-nowhere heartbeat — resurrected and armed off the telemetry bus (ADR-710); bounds reuse `finops_gate` + the token ledger.
- The `ideation_loop` Socratic sub-graph + the `client_planner_mode_toggle` WS event already exist — the frontend Planner surface consumes them (ADR-713) with no new cognition contract.
- `SecretsScrubberFilter` (Phase 6.7, `shared/logging_filters.py`) — mandatory on the telemetry-log path (ADR-712). The 7.12.9 Fix 4 UTF-8 `reconfigure` lesson is the encoding precedent.
- The Phase 4.1.5 **cognitive-isolation fence** (only the analyst imports `brain.personality`) governs the new `ErrorCorrectionAgent` (ADR-711).

This blueprint is the contract that future 7.13 PRs must conform to.

---

## 1. Scope boundary (what 7.13 owns)

| Owns | Does NOT own |
|---|---|
| IDE lifecycle watchers (save/rename/delete); the silent telemetry WS channel + priority class; reactive single-file GraphRAG re-index; idle-triggered Dreaming with race discipline; frontend multi-turn state machine + Planner Manual-Mode surface; dashboard wire-or-delete sync; the `ErrorCorrectionAgent` self-healing node; the `.ailienant_telemetry.log` sink; the dedup sweep. | New cognition/agent *contracts* (`AIlienantGraphState`, `MissionSpecification` field sets stay immutable — additive coercers/prompt text only). New MCP marketplace or onboarding (Phase 10). Binary packaging (Phase 11). |

---

## 2. Binding Decisions (ADRs)

### [ADR-708] IDE Telemetry Bus — silent push channel + priority class

A **secondary, silent** message protocol rides the **existing** WebSocket — **no second socket** (port overload, auth duplication, reconnect complexity). New client→server events `client_ide_telemetry` carry a `kind` (`file_saved` / `file_renamed` / `file_deleted` / `active_changed`) and are **fire-and-forget**: they NEVER raise toasts, never interrupt the chat stream, and are dispatched off the WS receive-loop (the 7.9.B.17 off-loop pattern). Frontend watchers extend `src/ide_sync.ts` — reuse the existing 150 ms debounce; do **not** register a second change-watcher. The debounce shape mirrors `io_coalescer`.

**Backpressure / Head-of-Line Blocking (binding).** Telemetry and interactive chat share one socket, so a large-file save or a rapid edit burst could delay the agent's answer tokens. A **priority class** in `src/api/ws_client.ts` gives `chat_token` / `agent_status` frames **absolute priority**; telemetry frames are **droppable or deferrable** when the channel is congested. This composes with `throttled_stream` (batch/priority first, then backpressure-guard). A starved answer stream is a denial-of-service against the operator's attention (mirrors ADR-705).

### [ADR-709] Reactive Incremental Indexing

On `file_saved`, re-index **only that file** in the background: reuse `SemanticMemoryManager.semantic_upsert` (delete+reinsert by `(workspace_hash, file_path)`) and refresh the file's `dependency_graph` node. A **per-file debounce** (~500 ms, `io_coalescer` shape) collapses save storms. The write is **idempotent** and runs at **low scheduler priority** (reuse the indexer's nice / `BELOW_NORMAL`). On `file_deleted` → purge the vector-store row + graph node; on `file_renamed` → migrate them. **No full re-crawl on save** (that is the per-session `ClientWorkspaceInitEvent` path only). Memory is an evolutionary live-video, not a stale snapshot.

### [ADR-710] Idle-Triggered Dreaming + race discipline

An **idle timer** (default 5 min) on the telemetry bus arms the resurrected `OvernightDaemon`, started in the `main.py` lifespan. After the last `file_saved`/interaction, the daemon runs a workspace-context consolidation pass **unprompted**. New activity **cancels/resets** the timer. Hard **token/time bounds** reuse `finops_gate` + the token ledger; the daemon **never mutates files without HITL**.

**Race discipline (binding).** If a `file_saved` lands while the daemon is consolidating, the daemon **aborts its write transaction, invalidates the current snapshot, and resets the 5-min timer** — context-divergence discipline anchored on `document_version_id` (the ADR-703 pattern). Never consolidate over a graph the save just mutated.

### [ADR-711] Self-Healing Loop / `ErrorCorrectionAgent`

A LangGraph reflexion/fallback node (`brain/engine.py`). On a tool-execution failure, a schema hallucination, or an API crash, the graph **must not** return a raw error to the user. It routes to an internal `ErrorCorrectionAgent` (`agents/error_correction.py`) that **reads the traceback → uses read tools on the offending file → proposes a fix → retries the execution, up to 3 times** before conceding gracefully. It reuses `LLMGateway` and **composes with — does not duplicate** — the existing guardrail retry and the DLQ; the scattered retry budgets (guardrail=2, planner=2, MCTS=3, orchestrator) are unified under a common abstraction.

**Strict cognitive isolation (binding).** The agent is a cold, surgical engineering tool. It **never imports `brain.personality`** (no empathy/apologies that waste tokens and add latency inside the loop — honors the Phase 4.1.5 fence). Its proposed patches **must** pass through the existing `apply_patch` + HITL flow before touching disk; it is **never** granted unsupervised direct write.

### [ADR-712] Live Telemetry Log sink

A dedicated logger writes WS payloads, GraphRAG indexing events, and LangGraph node transitions to **`.ailienant_telemetry.log`** at the workspace root. It is the human/agent verification loop during 7.13 (and absorbs Phase 8's observability requirement — see Roadmap Impact). The sink is **bounded + rotated** (max size) so it cannot exhaust disk.

**Security & robustness (binding).** The file is an audit surface — it logs code snippets and prompts. (1) `SecretsScrubberFilter` (Phase 6.7) is **mandatory** on the path — secrets never land in the log. (2) `.ailienant_telemetry.log` is added to `.gitignore` **immediately**, so neither the user nor an agent can accidentally commit it to GitHub. (3) The Python writer forces **explicit UTF-8 encoding** (the 7.12.9 Fix 4 Windows lesson) to survive emoji / non-ASCII characters in file paths under cp1252.

### [ADR-713] Frontend Multi-Turn State Machine + Planner Surface

`Workspace.tsx` is refactored into a **mode state machine** with distinct modes (Chat / Planner / Dreaming Dashboard) instead of a single chat view. When the Planner requests a manual Q&A session (Manual Mode), the frontend renders an **interactive form or a locked multi-turn prompt** — never dumping the ideation context into the standard chat. It consumes the **existing** ideation WS events + the `client_planner_mode_toggle` toggle and persists through `workspaceStore.ts`. No backend cognition contract changes (`MissionSpecification`, `AIlienantGraphState` stay immutable).

---

## 3. Per-pillar design

### 3.1 — Spinal Cord (7.13.1)
Extend `src/ide_sync.ts` with the three missing lifecycle listeners on the existing debounce. Add additive `client_ide_telemetry` schemas to `api/ws_contracts.py`. `main.py` dispatches telemetry off the receive-loop into the reactive indexer (3.2) and the Dreaming idle timer (3.3). `src/api/ws_client.ts` gains the priority class (ADR-708).

### 3.2 — Reactive GraphRAG (7.13.2)
`core/indexer.py` gains a `reindex_one(path)` path that calls `semantic_upsert` + graph-node refresh under a per-file `io_coalescer` debounce; delete/rename purge/migrate. Driven by the bus from 3.1 (ADR-709).

### 3.3 — Dreaming (7.13.3)
`brain/daemon.py` `OvernightDaemon` is armed by an idle timer fed by the bus; started in the `main.py` lifespan; bounded via `finops_gate`; race-disciplined via `document_version_id` (ADR-710). Consolidation reuses `agents/workspace_context.py`.

### 3.4 — Orphanage / Planner UI (7.13.4)
`Workspace.tsx` → mode machine; new `PlannerSession.tsx` renders the locked multi-turn Manual-Mode form, consuming existing ideation events (ADR-713).

### 3.5 — Dashboard sync (7.13.5)
**Gating step first:** produce the wire-or-delete inventory table (control → expected endpoint → status) for user approval; persist it in §6 of this blueprint. Then wire approved panels / delete approved dead UI.

### 3.6 — Self-healing (7.13.6)
New `agents/error_correction.py` + reflexion node in `brain/engine.py`; unifies retry budgets; isolation + HITL discipline (ADR-711).

### 3.7 — Telemetry log (7.13.7)
New `core/telemetry_log.py` sink wired from `api/websocket_manager.py` + `brain/engine.py`; scrubbed, bounded, UTF-8, `.gitignore`'d (ADR-712).

### 3.8 — Dedup sweep (7.13.8)
Consolidate the VFS reader into a `VFSMiddleware` factory; unify retry/backoff under the 7.13.6 abstraction.

---

## 4. File inventory

### 4.1 New files
| Path | Task | Purpose |
|---|---|---|
| `ailienant-core/agents/error_correction.py` | 7.13.6 | `ErrorCorrectionAgent` — traceback read → fix → retry ≤3 |
| `ailienant-core/core/telemetry_log.py` | 7.13.7 | `.ailienant_telemetry.log` sink (scrubbed, bounded, UTF-8) |
| `ailienant-extension/src/workspace/components/PlannerSession.tsx` | 7.13.4 | locked multi-turn Manual-Mode form |
| `ailienant-core/tests/test_ide_telemetry_bus.py` | 7.13.1 | silent dispatch, priority/drop under congestion |
| `ailienant-core/tests/test_reactive_index.py` | 7.13.2 | single-file upsert; delete/rename purge; debounce |
| `ailienant-core/tests/test_dreaming_idle.py` | 7.13.3 | idle arm/cancel; save-mid-dream abort+reset |
| `ailienant-core/tests/test_error_correction.py` | 7.13.6 | mock error recovers ≤3; no `personality` import; HITL path |
| `ailienant-core/tests/test_telemetry_log.py` | 7.13.7 | secrets scrubbed; UTF-8; rotation bound |

### 4.2 Modified files
| Path | Change |
|---|---|
| [src/ide_sync.ts](../ailienant-extension/src/ide_sync.ts) | add save/rename/delete listeners on existing debounce |
| [src/api/ws_client.ts](../ailienant-extension/src/api/ws_client.ts) | telemetry priority class; droppable telemetry frames |
| [api/ws_contracts.py](../ailienant-core/api/ws_contracts.py) | additive `client_ide_telemetry` events |
| [main.py](../ailienant-core/main.py) | off-loop telemetry dispatch; start `OvernightDaemon` in lifespan |
| [core/indexer.py](../ailienant-core/core/indexer.py) | `reindex_one` incremental path |
| [brain/daemon.py](../ailienant-core/brain/daemon.py) | idle-armed consolidation + race discipline |
| [brain/engine.py](../ailienant-core/brain/engine.py) | reflexion node wiring; node-transition telemetry |
| [brain/guardrails.py](../ailienant-core/brain/guardrails.py) · [core/dead_letter.py](../ailienant-core/core/dead_letter.py) | compose with unified retry abstraction |
| [src/workspace/Workspace.tsx](../ailienant-extension/src/workspace/Workspace.tsx) · `workspaceStore.ts` · `ModeMenu.tsx` | mode state machine |
| dashboard panels (Hardware/Runtime/Rules/Audit) · `ContextOverlay.tsx` | wire-or-delete per approved inventory |
| [core/vfs_middleware.py](../ailienant-core/core/vfs_middleware.py) · [agents/coder.py](../ailienant-core/agents/coder.py) | consolidate VFS reader factory |
| `.gitignore` | add `.ailienant_telemetry.log` |

### 4.3 Reused (not modified)
`io_coalescer` debounce shape · `throttled_stream` (composes after the priority class) · `semantic_upsert` + indexer low-priority worker · OCC `document_version_id` / Delta State Sync · `ideation_loop` + `client_planner_mode_toggle` · `finops_gate` + token ledger · `SecretsScrubberFilter` · `apply_patch` + HITL · the 7.9.B.17 off-loop dispatch.

---

## 5. Verification Plan

### 5.1 — 7.13.9 Checkpoint Gate
| # | Test | Assertion |
|---|---|---|
| SC1 | Silent telemetry | save/rename/delete emit `client_ide_telemetry` with **no** toast and **no** chat interruption |
| SC2 | Reactive index | one `file_saved` re-indexes **only** that file (verifiable in `.ailienant_telemetry.log`); delete/rename purge/migrate the row |
| BP1 | Backpressure | under a save burst, `chat_token` frames keep priority; telemetry frames drop/defer, answer latency unaffected |
| DR1 | Idle dreaming | 5-min idle arms Dreaming; new activity cancels it; a save mid-consolidation aborts the txn + resets the timer |
| OR1 | Planner UI | Manual Mode renders an interactive locked multi-turn form, not standard chat |
| DB1 | Dashboard | no panel shows "No data"; orphans are wired or deleted per the approved inventory |
| AL1 | Self-healing | an intentional mock error recovers via `ErrorCorrectionAgent` in ≤3 attempts, no raw error to the user; patches went through HITL |
| ISO1 | Isolation | `agents/error_correction.py` never imports `brain.personality` (audit test) |
| TL1 | Telemetry log | WS payloads + node transitions logged, secrets scrubbed, UTF-8, bounded/rotated, `.gitignore`'d |
| DD1 | Dedup | no duplicate VFS readers; retry budgets unified |
| REG | Regression | full pytest green (≥ 675 at 7.13 start); `npm run compile` 0 errors; `mypy --strict` 0 on new/modified files |

### 5.2 — Dashboard wire-or-delete inventory (filled during 7.13.5, user-approved)
| Control / metric | Panel | Expected endpoint | Status (live/stub/dead) | Decision |
|---|---|---|---|---|
| _(to be enumerated and approved before any frontend mutation)_ | | | | |

**Manual smoke:** save a file → watch `.ailienant_telemetry.log` show one incremental re-index, no toast. Idle 5 min → Dreaming consolidates; type → it cancels. Toggle Planner Manual Mode → an interactive form appears. Trigger a mock tool error → the agent self-heals silently. Open the dashboard → every panel shows real data or is gone.

---

## 6. Roadmap Impact

| Future phase | Risk | Mitigation in this blueprint |
|---|---|---|
| Phase 8 (Pruebas / Observabilidad) | re-spec a separate log sink / audit file | **7.13 absorbs Phase 8 observability** (ADR-712 + manifest Convenciones note); Phase 8 builds *on* `.ailienant_telemetry.log`, never re-creates it |
| Phase 10 (Onboarding / Antena) | the live-state panel needs node transitions | reuses the 7.13.7 node-transition telemetry + the 7.13.4 mode machine |
| Cognitive-isolation fence (4.1.5) | a self-healing agent could tempt a `brain.personality` import | ADR-711 reaffirms the fence; `ISO1` audit test stays green |
| `SCHEMA_EVOLUTION.MD` | new telemetry / divergence fields | all additive, scalar/optional with safe defaults; no removals/renames |
| 7.11 transport load | telemetry floods the WS shared with the inline diff-stream | ADR-708 priority class + `throttled_stream` size the channel; telemetry is droppable |

---

## 7. Anti-patterns (do **not** do this)

- ❌ Open a second WebSocket for telemetry. Ride the existing socket with a silent channel + priority class.
- ❌ Full workspace re-crawl on every save. Incremental single-file `semantic_upsert` only.
- ❌ Raise toasts / interrupt the chat on background telemetry. Fire-and-forget, off-loop.
- ❌ Let telemetry frames starve `chat_token`. Telemetry is droppable; chat has absolute priority.
- ❌ Consolidate (Dream) over a graph a save just mutated. Abort the txn + reset the timer on divergence.
- ❌ Return a raw tool/schema/API error to the user. Route through the `ErrorCorrectionAgent`.
- ❌ Import `brain.personality` into the `ErrorCorrectionAgent`. It is a cold engineering tool.
- ❌ Grant the `ErrorCorrectionAgent` unsupervised disk writes. Patches go through `apply_patch` + HITL.
- ❌ Write an unbounded / unrotated telemetry log, or one without `SecretsScrubberFilter`, or commit it to git.
- ❌ Write the log without explicit UTF-8 (cp1252 crash on emoji/non-ASCII paths).
- ❌ Dump Planner Manual-Mode Q&A into the standard chat. Render the locked multi-turn form.
- ❌ Leave dashboard panels showing "No data". Wire or delete — per the user-approved inventory.
- ❌ Delete a dashboard panel without the approved inventory table. Approval gates deletion.
- ❌ Duplicate the VFS reader / leave divergent retry caps. Consolidate.

---

## 8. Glossary

- **Spinal Cord** — the event-driven bus connecting IDE lifecycle events to the agent's cognition in real time (ADR-708).
- **Silent telemetry channel** — the secondary, toast-free, droppable message protocol on the existing WebSocket.
- **Priority class** — the WS scheduling rule giving chat/answer frames absolute precedence over telemetry (ADR-708).
- **Reactive indexing** — save-triggered single-file `semantic_upsert` + graph refresh (ADR-709).
- **Dreaming** — the idle-time, unprompted workspace-context consolidation pass run by the `OvernightDaemon` (ADR-710).
- **Race discipline** — aborting a consolidation whose underlying graph a concurrent save invalidated, anchored on `document_version_id` (ADR-710).
- **`ErrorCorrectionAgent`** — the self-healing reflexion node: traceback → read → fix → retry ≤3 (ADR-711).
- **Telemetry log** — `.ailienant_telemetry.log`, the scrubbed/bounded/UTF-8 file sink for WS payloads + node transitions (ADR-712).
- **Mode machine** — the `Workspace.tsx` Chat / Planner / Dreaming-Dashboard state machine (ADR-713).
- **Wire-or-delete** — the dashboard policy: every control is connected to a real endpoint or removed, gated by a user-approved inventory.
- **Cognitive-isolation fence** — the Phase 4.1.5 rule that only the analyst imports the persona module; reaffirmed for the `ErrorCorrectionAgent`.

---

*End of Phase 7.13 Blueprint. The next compaction should still be able to re-derive intent from this single document.*
