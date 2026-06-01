# PHASE 7.13 — Master Architectural Blueprint (The Enterprise Spinal Cord: Event-Driven Telemetry, Reactive Memory & Self-Healing)

> **Mandatory read** during every Phase 7.13 task. Survives session compactions: re-derive intent from this document. Any deviation from the binding decisions tagged **[ADR-7xx]** requires an explicit blueprint amendment in the same PR.
>
> **v2 (build-order reorder + hardening).** v1 captured the five directive pillars as 7.13.0–7.13.9. A second, deeper audit found backend↔frontend wiring gaps, orphaned backend actions with no UI trigger, real-world interruption points, and the mechanical safeguards a Push system needs — folded in, **in build/creation order**, as 7.13.0–7.13.12. New/amended decisions: **ADR-714/715/716/718** added; **ADR-710 rewritten** (Dreaming is now manual, not idle-triggered).

## Context

Phases 7.10–7.12 + 9 made AILIENANT *feel* like a product: cognitive transparency, native thinking, a context-aware analyst, an inviolable identity, and a stabilized WebView lifecycle. But the system is still fundamentally a **Pull model** — a walkie-talkie chat that only reacts when the user speaks. The IDE's reality (saves, renames, deletes) never reaches the brain on its own; the GraphRAG memory is a stale per-session snapshot; heavily engineered backend features (Planner Manual Mode, the Dreaming daemon, the DLQ resume API) are orphaned because no UI or trigger reaches them; tool/schema/API failures can surface raw to the user; and there is no file-based observability channel to watch the system breathe during development.

Phase 7.13 is the **paradigm shift to a Push / Event-Driven architecture** — the "spinal cord" that connects IDE reality to the agent's cognition in real time, makes memory evolutionary, resurrects the orphaned features, adds an autonomous self-healing loop, and opens a permanent telemetry channel. **The Push model does not live in a vacuum:** it silently couples to code owned by the already-`[x]` backend phases (0–6), exposing race conditions, leaks, and duplication that were invisible under the synchronous/pull scheme. Those retrofits are catalogued in §9 (Backend Integration Matrix) and owned by specific 7.13 sub-phases.

### Build order (v2)

Foundations → privacy gate → instrument → ingest → react → (manual) consolidate → heal → make the client durable → expose surfaces → clean up → gate. The numbering 7.13.0–7.13.12 reflects creation order, not discovery order. The safety spine (7.13.1) and the privacy gate (7.13.2) come **first** because every later Push feature stresses them; the telemetry log (7.13.3) is built early as the **verification instrument** for everything after it.

### The audit (current state vs. directive premise — verified in code)

The directive assumes greenfield; the codebase shows most pillars are *partially* built. Every 7.13 task therefore **extends/wires** rather than rebuilds, honoring the Zero-Deduplication rule.

| Pillar | Already exists (reuse) | Real gap (the 7.13 work) |
|---|---|---|
| File watchers | `onDidChangeActiveTextEditor` / `onDidChangeTextDocument` → `client_file_update`, debounced 150 ms ([ide_sync.ts:67-70](../ailienant-extension/src/ide_sync.ts#L67)); [core/io_coalescer.py](../ailienant-core/core/io_coalescer.py) debounces saves in a 500 ms window (`_MASS_THRESHOLD=100`, `_UNLINK_SENTINEL`) | **Missing** `onDidSaveTextDocument`, `onDidRenameFiles`, `onDidDeleteFiles`; **no** silent telemetry channel; the `client_file_delete` sender is an **orphan** (handler exists, no FE emitter) |
| Reactive GraphRAG | Lazy/batch per-session indexer ([core/indexer.py](../ailienant-core/core/indexer.py)); per-file `semantic_upsert` deletes+reinserts by `(workspace_hash, file_path)` | **No** save-triggered single-file re-index; no per-file debounce/single-flight; no content-hash-idempotent unified entry shared by agent writes + human saves |
| Concurrency safety | `core/token_ledger.py` `threading.Lock` pattern; `_ppr_tasks` per-project cancel precedent ([main.py:661](../ailienant-core/main.py#L661)); `active_tasks` drain + `cleanup_session` hook ([main.py:285/1045](../ailienant-core/main.py#L285)) | **No `asyncio.Lock`** on `dependency_graph`/LanceDB writes (`core/db.py::upsert_dependencies`/`purge_file_nodes` — race window); **no** inbound WS rate limit; **no** per-session cascade-cancel of background indexer/daemon tasks |
| Dreaming | `OvernightDaemon` heartbeat stub ([brain/daemon.py](../ailienant-core/brain/daemon.py)); workspace-context consolidation helpers | **Never started** by `main.py`; **no UI/command trigger** — the orphan `dreaming_toggle` reaches nothing |
| Orphanage / Planner | Planner **Manual Mode** + Socratic `ideation_loop` exist ([agents/planner.py](../ailienant-core/agents/planner.py), [brain/ideation.py](../ailienant-core/brain/ideation.py)); WS toggle event type exists in `config.ts` | Frontend has **no Planner UI**; `client_planner_mode_toggle` is an **orphan** (type defined, no sender) |
| Dashboard | overview/telemetry/memory/byom/staging panels wired; **Hardware/Runtime/Rules/Audit panels fetch real endpoints** (re-audit + `project_runtime_docker_widget` memory) | The genuine orphans are `client_master_toggle`/`client_profile_change` (dead types), the OOM-telemetry view, the `ContextOverlay` terminal stub; and mount-poll panels leak polling-cleanup (convert to Push-fed) |
| Self-healing | Validation/guardrail retry, `MAX_RETRIES=2` ([brain/guardrails.py](../ailienant-core/brain/guardrails.py)); DLQ ([core/dead_letter.py](../ailienant-core/core/dead_letter.py)) + resume API ([main.py](../ailienant-core/main.py) `resume_task()`, `/dlq/pending`); MCTS surgeon escalation | **No** `ErrorCorrectionAgent` (traceback → read → fix → retry); scattered retry budgets; the `/task/resume` + `/dlq/pending` APIs are **orphans** (no UI) |
| Telemetry log | Routing/OOM telemetry is **SQLite-resident** ([core/telemetry.py](../ailienant-core/core/telemetry.py)) | **No** `.ailienant_telemetry.log` file sink; no `RotatingFileHandler` bound |
| Frontend resilience | OCC `document_version_id` / 7.7 Delta State Sync (`_fileVersions`, `FILE_VERSION_CHANGED`); abort savepoint ([brain/checkpoint.py](../ailienant-core/brain/checkpoint.py)) | **No** request-ID on `SUBMIT_TASK`, no `ABORT_MESH`/HITL ACK, no stream watchdog, uncapped `_pendingSends`, `isAborting` survives teardown, `document_version_id` never seeded at startup |
| Privacy gate | §7.1.2 Privacy Gate (`src/ide_sync.ts` `isFileBlocked()` + `FileSystemWatcher`); §3.4.6 Dual-Rules Resolver (`core/rules.py::RuleManager`, local-over-global merge) | Telemetry push has **no exclusion gate** before files leave the IDE; no Incognito kill-switch |
| Dedup | Centralized `LLMGateway`; single telemetry sink | VFS reader wrapper duplicated in **both** `agents/coder.py` (`_make_vfs_reader`) **and** `agents/analyst.py`; retry budgets scattered |

### Grounding (existing infrastructure to reuse, verified)

- §3.4.6 **Dual-Rules Resolver:** `./.ailienant/.ailienant.json` (Local) **>** `~/.ailienant/.ailienant.json` (Global); deep-merge dicts + concat/dedup lists; local override. Impl: `core/rules.py` `RuleManager` (`append_local_rule()` atomic). The single source of truth for telemetry exclusion (ADR-718) — **no new ignore files**.
- §7.1.2 **Privacy Gate:** `src/ide_sync.ts` already parses `.ailienantignore` via `FileSystemWatcher` (hot-reload), `isFileBlocked()` glob filter → `FILE_BLOCKED` disables submit. §1.3.1 Context Firewall parses `.gitignore`+`.ailienantignore` with `pathspec` (backend VFS). Extended (not replaced) by ADR-718.
- [core/io_coalescer.py](../ailienant-core/core/io_coalescer.py) debounces file-save events in a 500 ms window (`_MASS_THRESHOLD=100`, `_UNLINK_SENTINEL`) — the reuse shape for the telemetry bus debounce (ADR-708), the reactive single-flight (ADR-714), and the inbound rate-limit mass-threshold.
- [core/token_ledger.py](../ailienant-core/core/token_ledger.py) `threading.Lock` pattern — the reuse shape for the per-project `asyncio.Lock` (ADR-714). The `_ppr_tasks` per-project cancel at [main.py:661](../ailienant-core/main.py#L661) is the precedent for the background-task registry.
- `active_tasks` drain on shutdown + `register_session_cleanup_hook(task_service.cleanup_session)` on disconnect ([main.py:285/1045](../ailienant-core/main.py#L285)) — **already wired**; ADR-714 EXTENDS them to cascade-cancel the background indexer + daemon, it does not build a new lifecycle.
- [transport/throttler.py](../ailienant-core/transport/throttler.py) `throttled_stream` (1 MB backpressure) + [transport/token_batcher.py](../ailienant-core/transport/token_batcher.py) (40 ms coalesce, NarrationGate 15%) — the composition point for the telemetry priority class (ADR-708).
- `SemanticMemoryManager.semantic_upsert` deletes+reinserts a single `(workspace_hash, file_path)` row — the reuse point for incremental re-index (ADR-709). The indexer sets low scheduler priority (nice / `BELOW_NORMAL`) and has crash-resume.
- **OCC `document_version_id`** flows through `TaskPayload` and the 7.7 Delta State Sync — the divergence-discipline anchor for the Dreaming/indexing race (ADR-710) and the startup-seed fix (ADR-715), mirroring ADR-703.
- `OvernightDaemon` (`brain/daemon.py`) is a started-nowhere heartbeat — resurrected and driven by an **explicit user action** (ADR-710, rewritten); bounds reuse `finops_gate` + the token ledger; DLQ-wrapped (`core/dead_letter.py`).
- `core/dead_letter.py` + the `resume_task()` / `/dlq/pending` APIs — the reuse points for the self-healing DLQ redirect and the resume surface (ADR-711 + ADR-716).
- The `ideation_loop` Socratic sub-graph + the `client_planner_mode_toggle` WS event type already exist — the frontend Planner surface consumes them (ADR-713).
- `SecretsScrubberFilter` (Phase 6.7, `shared/logging_filters.py`) — mandatory on the telemetry-log path (ADR-712). The 7.12.9 Fix 4 UTF-8 `reconfigure` lesson is the encoding precedent.
- The Phase 4.1.5 **cognitive-isolation fence** (only the analyst imports `brain.personality`) governs the new `ErrorCorrectionAgent` (ADR-711).
- `request_human_approval` (300 s + audit) + `apply_patch` + HITL — the mandatory disk-write path for the `ErrorCorrectionAgent` and Dreaming.

This blueprint is the contract that future 7.13 PRs must conform to.

---

## 1. Scope boundary (what 7.13 owns)

| Owns | Does NOT own |
|---|---|
| The concurrency & resource-safety spine (per-project graph/LanceDB write lock, single-flight, inbound WS rate limit, per-session background-task cancel); the Dual-Rules telemetry-exclusion gate + Incognito toggle; IDE lifecycle watchers (save/rename/delete) on the silent telemetry channel + priority class; reactive single-file GraphRAG re-index (content-hash-idempotent unified entry); **manual** "Consolidate Memory" Dreaming with race discipline; the `ErrorCorrectionAgent` self-healing node + DLQ resume surface; frontend stream resilience & lifecycle re-attach; the multi-turn state machine + Planner Manual-Mode surface; dashboard surface-sync (verify-or-wire-or-delete, gated) + Push-fed panels; the `.ailienant_telemetry.log` sink; the dedup sweep. | New cognition/agent *contracts* (`AIlienantGraphState`, `MissionSpecification` field sets stay immutable — additive coercers/ACK/requestId fields only). New ignore-file formats (reuse the Dual-Rules resolver). New MCP marketplace or onboarding (Phase 10). Binary packaging (Phase 11). |

---

## 2. Binding Decisions (ADRs)

> Contiguous **708 → 718**. ADR-710 is **rewritten** (manual Dreaming supersedes the idle trigger). ADR-714/715/716/718 are **new**. ADR-717 is intentionally unused.

### [ADR-708] IDE Telemetry Bus — silent push channel + priority class

A **secondary, silent** message protocol rides the **existing** WebSocket — **no second socket** (port overload, auth duplication, reconnect complexity). New client→server events `client_ide_telemetry` carry a `kind` (`file_saved` / `file_renamed` / `file_deleted` / `active_changed`) and are **fire-and-forget**: they NEVER raise toasts, never interrupt the chat stream, and are dispatched off the WS receive-loop (the 7.9.B.17 off-loop pattern). Frontend watchers extend `src/ide_sync.ts` — reuse the existing 150 ms debounce; do **not** register a second change-watcher. The previously **orphaned `client_file_delete` sender is wired here**. Every push first passes the ADR-718 exclusion gate. **The bus arms no timer** — Dreaming is manual (ADR-710); the bus feeds reactive indexing (ADR-709) and Push-fed panels (ADR-716).

**Backpressure / Head-of-Line Blocking (binding).** Telemetry and interactive chat share one socket, so a large-file save or a rapid edit burst could delay the agent's answer tokens. A **priority class** in `src/api/ws_client.ts` gives `chat_token` / `agent_status` frames **absolute priority**; telemetry frames are **droppable or deferrable** when the channel is congested. `_pendingSends` is **capped** (flood guard on long disconnect). This composes with `throttled_stream` (batch/priority first, then backpressure-guard). A starved answer stream is a denial-of-service against the operator's attention (mirrors ADR-705).

### [ADR-709] Reactive Incremental Indexing

On `file_saved`, re-index **only that file** in the background under the ADR-714 **lock + single-flight**: reuse `SemanticMemoryManager.semantic_upsert` (delete+reinsert by `(workspace_hash, file_path)`) and refresh the file's `dependency_graph` node. A **per-file single-flight** per `(filepath, project_id)` collapses save storms; the write is **idempotent by content-hash** through a **unified entry point** so the agent's `apply_patch` writes and human saves share one path (GAP9). A reactive-index **circuit breaker** (GAP6) trips on repeated failure. On `file_deleted` → purge the vector-store row + graph node (`purge_file_nodes`); on `file_renamed` → migrate them. **No full re-crawl on save** (that is the per-session `ClientWorkspaceInitEvent` path only). The orphan **Memory Janitor** may be wired as the GC counterpart. Memory is an evolutionary live-video, not a stale snapshot.

### [ADR-710] Manual Dreaming — explicit "Consolidate Memory" action (REWRITTEN, supersedes the idle trigger)

The 5-min inactivity trigger is **abandoned.** Consolidation runs **only** on an explicit user action: a **WebDashboard button** + a **VS Code command** ("Trigger Dreaming / Consolidate Memory"), routed via a new `client_dreaming_run` event to the `OvernightDaemon` started in the `main.py` lifespan. It runs under the ADR-714 **shared lock + cancellation token**, with a **save-mid-run abort guard** anchored on `document_version_id` (the ADR-703 pattern: if a `file_saved` lands mid-run, abort the write transaction + invalidate the snapshot), FinOps/token bounds (`finops_gate` + token ledger), DLQ-wrap (`core/dead_letter.py`), and low scheduler priority. The daemon **never mutates files without HITL**. It replaces the orphan/UI-less `dreaming_toggle`.

*Rationale:* an idle timer waking GraphRAG+LLM during a heavy build or local-model run overloads the CPU, races resuming typists, and burns tokens unattended. **The user owns when resources and tokens are spent.**

*Anti-patterns:* any timer-based / idle auto-trigger; consolidating without the shared lock; running while the user is mid-edit on the same files.

> **Amendment — Targeted Dreaming (`focus_area`).** `client_dreaming_run` carries an optional
> `focus_area: Optional[str]`. When set (HUD presets "Architecture and Patterns" / "Refactoring and
> Technical Debt" / "Bug Fixes", or free-text "Other"), the daemon injects the theme into the
> consolidation system prompt so the LLM prioritizes restructuring toward that concern — a scoped pass
> spends fewer tokens than a whole-workspace sweep. `None` = "Auto" (whole workspace). `OvernightDaemon`
> is **repurposed**: the Phase 3.4.3a MCTS heartbeat loop is removed; the class is now a stateless
> on-demand consolidation service exposing `run_consolidation(project_id, focus_area=None, …)`. The pass
> reuses `agents/workspace_context.build_workspace_overview` for its corpus, calls `LLMGateway.ainvoke`
> **outside** the per-project `graph_write_lock`, and persists the result as a semantic-memory note via
> `SemanticMemoryManager.semantic_upsert` **under** the lock (final write only). The OCC race guard is a
> monotonic per-project save epoch in `main.py`: a `client_file_update`/`client_ide_telemetry` mid-run
> bumps it (invalidating the snapshot) **and** cancels the in-flight task; the daemon re-checks the
> snapshot before committing (`aborted_stale`). An already-over-budget session **refuses** the pass
> (`refused_budget`) before any LLM call.

### [ADR-711] Self-Healing Loop / `ErrorCorrectionAgent`

A LangGraph reflexion/fallback node (`brain/engine.py`). On a tool-execution failure, a schema hallucination, or an API crash, the graph **must not** return a raw error to the user. It routes to an internal `ErrorCorrectionAgent` (`agents/error_correction.py`) that **reads the traceback → uses read tools on the offending file → proposes a fix → retries the execution, up to 3 times** before conceding gracefully. It reuses `LLMGateway` and **composes with — does not duplicate** — the existing guardrail retry and the DLQ; the scattered retry budgets (guardrail=2, planner=2, MCTS=3, orchestrator) are unified under a common abstraction. A **failure-signature cache** acts as a cross-turn circuit breaker (GAP8). After the bounded retries, the payload/task is redirected to `core/dead_letter.py` — **a saturated event loop must never let an LLM failure corrupt WS state.**

**Strict cognitive isolation (binding).** The agent is a cold, surgical engineering tool. It **never imports `brain.personality`** (no empathy/apologies that waste tokens and add latency inside the loop — honors the Phase 4.1.5 fence). Its proposed patches **must** pass through the existing `apply_patch` + HITL flow before touching disk; it is **never** granted unsupervised direct write.

### [ADR-712] Live Telemetry Log sink

A dedicated logger writes WS payloads, GraphRAG indexing events, and LangGraph node transitions to **`.ailienant_telemetry.log`** at the workspace root. It is the human/agent verification loop during 7.13 (and absorbs Phase 8's observability requirement — see Roadmap Impact), built **early** so the rest of 7.13 can be verified against it.

**Security & robustness (binding).** The file is an audit surface — it logs code snippets and prompts. (1) `SecretsScrubberFilter` (Phase 6.7) is **mandatory** on the path — secrets never land in the log. (2) `.ailienant_telemetry.log` is added to `.gitignore` **immediately**, so neither the user nor an agent can accidentally commit it to GitHub. (3) The Python writer forces **explicit UTF-8 encoding** (the 7.12.9 Fix 4 Windows lesson) to survive emoji / non-ASCII characters in file paths under cp1252. (4) A **`RotatingFileHandler`** with a hard max size (GAP7) so the sink cannot exhaust disk.

### [ADR-713] Frontend Multi-Turn State Machine + Planner Surface

`Workspace.tsx` is refactored into a **mode state machine** with distinct modes (Chat / Planner / Dreaming Dashboard) instead of a single chat view. When the Planner requests a manual Q&A session (Manual Mode), the frontend renders an **interactive form or a locked multi-turn prompt** — never dumping the ideation context into the standard chat. It consumes the **existing** ideation WS events + the `client_planner_mode_toggle` toggle (**whose orphan sender is wired here**) and persists through `workspaceStore.ts`. No backend cognition contract changes (`MissionSpecification`, `AIlienantGraphState` stay immutable).

### [ADR-714] Concurrency & Resource Safety (NEW)

A per-project `asyncio.Lock` **serializes all `dependency_graph`/LanceDB writes** — the agent, the reactive index, and manual Dreaming all share it; it guards **both** `upsert_dependencies` **and** `purge_file_nodes` (and ideally the LanceDB write + PPR recompute), reusing the `token_ledger` lock pattern. **Single-flight** per `(filepath, project_id)` collapses redundant re-index of rapid saves. A **per-session background-task registry** cascade-cancels the background indexer + daemon tasks on disconnect/shutdown — built by **extending** the already-wired `cleanup_session` hook + `active_tasks` drain ([main.py:285/1045](../ailienant-core/main.py#L285)) and the `_ppr_tasks` cancel precedent ([main.py:661](../ailienant-core/main.py#L661)), not a new lifecycle. An **inbound per-client WS token-bucket rate limit** (reusing the `io_coalescer` mass-threshold) stops a save flood from swamping the event loop. The telemetry log is **size-rotated** (GAP7). **The graph-reader path that takes the lock is the daemon consolidation + GraphRAG extractor — NOT `agents/mcts_coder.py`, which does not touch `core/db.py`.**

*Anti-patterns:* unlocked DELETE+INSERT; the daemon writing the graph without the shared lock; unbounded inbound events; orphaned background tasks on disconnect.

### [ADR-715] Frontend Stream Resilience & Lifecycle Re-attach (NEW)

Every long-running client→server request carries a **correlation/request ID** for idempotent server-side dedup (no duplicate generations on reconnect). Streams have a **client watchdog** that clears a stuck `isStreaming` / tool-chip / natt state if `server_stream_end` never arrives. Sends are queued reliably and **re-attached on reconnect**; `_pendingSends` is bounded. Transient UI flags (`isAborting`) **never survive teardown** (cleared on rehydrate). `ABORT_MESH` and HITL responses are **ACK'd** (Stop with the WS down surfaces an error; a HITL response from a hidden/torn-down webview is not orphaned). `document_version_id` is **seeded at startup** (so the first delta is not always treated as a change). In-flight tool chips are persisted; the tool-output array and the `InlineMutationManager` promise chain are **capped**; StagingArea gains a stale-patch refresh path. All new fields (ACK, requestId) are **additive** in `api/ws_contracts.py`.

*Anti-patterns:* fire-and-forget abort; an infinite "Streaming…" spinner; duplicate generations on reconnect; uncapped client buffers.

### [ADR-716] Orphan Recovery & Push-Fed Surfaces (NEW)

Backend capabilities reachable today only via API get a UI or are deleted, **gated by a user-approved inventory**. Crucially — correcting the v1 mis-audit — the Hardware/Runtime/Rules/Audit panels **fetch real endpoints**; they are **verified, not blind-deleted**. Mount-poll panels (Hardware/Runtime) migrate to **telemetry-bus subscriptions** (killing the polling-cleanup leaks). The dead-letter resume path (`/task/resume` + `/dlq/pending`) becomes a first-class **resume surface** (the cross-session complement to in-turn self-healing). Orphan WS events (`planner_mode_toggle`, `file_delete`, `master_toggle`, `profile_change`) are wired or their dead types removed.

*Anti-patterns:* deleting a panel without the approved inventory; leaving orphan endpoints/events; per-panel polling in a Push world.

### [ADR-718] Centralized Privacy & Telemetry Filtering — Dual-Rules + Incognito (NEW)

Telemetry exclusion adheres strictly to §3.4.6: **no new `.ailienantignore`-style files, no per-subfolder config pollution.** A single hierarchical source of truth — `./.ailienant/.ailienant.json` (local) deep-merged over `~/.ailienant/.ailienant.json` (global) — is read by both the TS watcher (extend the §7.1.2 Privacy Gate in `src/ide_sync.ts`) and the Python engines (`core/rules.py::RuleManager`, consumed by reactive index / Dreaming / analyst context). This gate fires **before** anything leaves the IDE — a `.env`/confidential file is never pushed to the brain. A **VS Code status-bar "Incognito Mode"** toggle instantly pauses the push bus without editing JSON, for tactical privacy.

*Anti-patterns:* generating ignore files per directory; a second/divergent exclusion list; pushing a file the resolver excludes; an Incognito state the bus ignores.

---

## 3. Per-pillar design (build order)

### 3.1 — Concurrency & Resource Safety Spine (7.13.1)
Per-project `asyncio.Lock` around `core/db.py::upsert_dependencies` + `purge_file_nodes` (reuse the `token_ledger` lock pattern); shared daemon↔indexer lock; single-flight per `(filepath, project_id)` in `core/indexer.py`; **extend** the existing `cleanup_session` hook + `active_tasks` drain in `main.py`/`core/task_service.py` to cascade-cancel the background indexer + daemon; inbound per-client token-bucket in `api/websocket_manager.py` (reuse `io_coalescer` mass-threshold). The lock is taken by the daemon + `core/memory/graphrag_extractor.py`, not `agents/mcts_coder.py` (ADR-714).

### 3.2 — Privacy & Telemetry Filtering: Dual-Rules + Incognito (7.13.2)
`core/rules.py::RuleManager` resolves the hierarchical `.ailienant/.ailienant.json` for Python engines; extend `src/ide_sync.ts`'s §7.1.2 Privacy Gate to honor the resolved exclusion patterns; new status-bar Incognito toggle in `extension.ts` pauses the bus; `core/vfs_middleware.py` consumes the shared resolver (ADR-718).

### 3.3 — Live Telemetry Log (7.13.3)
`core/telemetry_log.py` sink → `.ailienant_telemetry.log`; `RotatingFileHandler`, `SecretsScrubberFilter`, explicit UTF-8, `.gitignore`. Wired from `api/websocket_manager.py` + `brain/engine.py`. Built early as the verification instrument (ADR-712).

### 3.4 — Spinal Cord: IDE Telemetry Bus (7.13.4)
Extend `src/ide_sync.ts` (`onDidSave/Rename/Delete`) on the existing 150 ms debounce; wire the orphan `client_file_delete` sender; every push passes the 7.13.2 gate. Silent `client_ide_telemetry` channel + priority class in `src/api/ws_client.ts` + `_pendingSends` cap; off-loop dispatch in `main.py` honoring the 7.13.1 rate limit (ADR-708).

### 3.5 — Reactive GraphRAG (7.13.5)
`core/indexer.py` `reindex_one(path)` under the 7.13.1 lock + single-flight; `semantic_upsert` + graph-node refresh; delete/rename purge/migrate (consumes `client_file_delete`); circuit breaker; content-hash-idempotent unified entry shared by `apply_patch` + human saves (ADR-709).

### 3.6 — Manual Dreaming (7.13.6)
`brain/daemon.py` `OvernightDaemon` started in the `main.py` lifespan, fired **only** by `client_dreaming_run` (WebDashboard button + VS Code command); shared lock + cancellation token; save-mid-run abort via `document_version_id`; FinOps bounds; DLQ-wrap; low priority. Consolidation reuses `agents/workspace_context.py` (ADR-710, rewritten).

### 3.7 — Self-Healing + DLQ Resume (7.13.7)
New `agents/error_correction.py` + reflexion node in `brain/engine.py`; unifies retry budgets; failure-signature cache; isolation + HITL discipline; redirect to `core/dead_letter.py` after bounded retries; wire the orphan `/task/resume` + `/dlq/pending` into a resume surface (ADR-711 + ADR-716).

### 3.8 — Frontend Stream Resilience (7.13.8)
Correlation/request IDs, stream watchdog, reliable send queue + reconnect re-attach, clear `isAborting` on rehydrate, ABORT/HITL ACK, seed `document_version_id` at startup, persist tool chips, cap buffers, StagingArea stale-patch refresh; additive fields in `api/ws_contracts.py` (ADR-715).

### 3.9 — Orphanage Recovery I: Planner UI (7.13.9)
`Workspace.tsx` → mode machine; new `PlannerSession.tsx` locked multi-turn form; wire the orphan `client_planner_mode_toggle` sender; consume existing ideation events (ADR-713).

### 3.10 — Orphanage Recovery II: Surface Sync & Push-Fed Panels (7.13.10)
Gated inventory first (verify, don't blind-delete); convert mount-poll panels to telemetry-bus subscriptions; wire/decide `client_master_toggle`/`client_profile_change`, OOM-telemetry view, `ContextOverlay` terminal stub (ADR-716).

### 3.11 — Zero-Deduplication Sweep (7.13.11)
Consolidate the VFS readers in **both** `agents/coder.py` **and** `agents/analyst.py` into one `VFSMiddleware` factory honoring the Dual-Rules resolver + current buffer version; finalize the unified retry/backoff abstraction (consumed by 7.13.7).

---

## 4. File inventory

### 4.1 New files
| Path | Task | Purpose |
|---|---|---|
| `ailienant-core/core/telemetry_log.py` | 7.13.3 | `.ailienant_telemetry.log` sink (scrubbed, rotated, UTF-8) |
| `ailienant-core/agents/error_correction.py` | 7.13.7 | `ErrorCorrectionAgent` — traceback read → fix → retry ≤3 |
| `ailienant-extension/src/workspace/components/PlannerSession.tsx` | 7.13.9 | locked multi-turn Manual-Mode form |
| `ailienant-extension/src/workspace/components/ModeMenu.tsx` | 7.13.9 | mode-machine switcher |
| `ailienant-core/tests/test_concurrency_safety.py` | 7.13.1 | graph write lock; single-flight; per-session cancel; inbound rate limit |
| `ailienant-core/tests/test_privacy_telemetry_gate.py` | 7.13.2 | Dual-Rules exclusion; Incognito pauses the bus |
| `ailienant-core/tests/test_telemetry_log.py` | 7.13.3 | secrets scrubbed; UTF-8; rotation bound |
| `ailienant-core/tests/test_ide_telemetry_bus.py` | 7.13.4 | silent dispatch, priority/drop under congestion |
| `ailienant-core/tests/test_reactive_index.py` | 7.13.5 | single-file upsert; delete/rename purge; content-hash idempotency |
| `ailienant-core/tests/test_manual_dreaming.py` | 7.13.6 | fires only on action; save-mid-run aborts; lock held |
| `ailienant-core/tests/test_error_correction.py` | 7.13.7 | mock error recovers ≤3; no `personality` import; HITL path; DLQ redirect |

### 4.2 Modified files
| Path | Change | Task |
|---|---|---|
| [core/db.py](../ailienant-core/core/db.py) | per-project `asyncio.Lock` around `upsert_dependencies` + `purge_file_nodes` | 7.13.1 |
| [core/indexer.py](../ailienant-core/core/indexer.py) | single-flight; `reindex_one`; content-hash unified entry; circuit breaker | 7.13.1 / 7.13.5 |
| [core/io_coalescer.py](../ailienant-core/core/io_coalescer.py) | reuse mass-threshold for inbound rate limit / single-flight | 7.13.1 |
| [core/task_service.py](../ailienant-core/core/task_service.py) · [main.py](../ailienant-core/main.py) | extend `cleanup_session` + `active_tasks` to cascade-cancel indexer/daemon; lifespan start daemon; off-loop telemetry dispatch; `client_dreaming_run` route | 7.13.1 / 7.13.4 / 7.13.6 |
| [api/websocket_manager.py](../ailienant-core/api/websocket_manager.py) | inbound per-client token-bucket; telemetry-log wiring | 7.13.1 / 7.13.3 |
| [core/telemetry.py](../ailienant-core/core/telemetry.py) · [brain/engine.py](../ailienant-core/brain/engine.py) · [main.py](../ailienant-core/main.py) | telemetry-log mirror (forensic-first); node-entry instrumentation; `configure_telemetry_log` at `client_workspace_init` + `shutdown_telemetry_log` in lifespan (ADR-712 amendment — `main.py`/`brain/engine.py` added to the 7.13.3 file set) | 7.13.3 |
| [core/rules.py](../ailienant-core/core/rules.py) · [core/vfs_middleware.py](../ailienant-core/core/vfs_middleware.py) | Dual-Rules exclusion resolver consumption | 7.13.2 / 7.13.11 |
| [src/ide_sync.ts](../ailienant-extension/src/ide_sync.ts) | save/rename/delete listeners; wire `client_file_delete`; extend Privacy Gate with resolved exclusions | 7.13.2 / 7.13.4 |
| `ailienant-extension/src/extension.ts` | Incognito status-bar toggle; "Trigger Dreaming" command | 7.13.2 / 7.13.6 |
| [src/api/ws_client.ts](../ailienant-extension/src/api/ws_client.ts) | telemetry priority class; droppable frames; `_pendingSends` cap; correlation IDs; reliable queue + re-attach | 7.13.4 / 7.13.8 |
| [api/ws_contracts.py](../ailienant-core/api/ws_contracts.py) | additive `client_ide_telemetry`, `client_dreaming_run`, ACK/requestId fields | 7.13.4 / 7.13.6 / 7.13.8 |
| [brain/daemon.py](../ailienant-core/brain/daemon.py) · `agents/workspace_context.py` | manual consolidation + race discipline under shared lock | 7.13.6 |
| [core/memory/graphrag_extractor.py](../ailienant-core/core/memory/graphrag_extractor.py) | acquire the per-project lock on graph reads | 7.13.1 / 7.13.6 |
| [brain/engine.py](../ailienant-core/brain/engine.py) · [brain/guardrails.py](../ailienant-core/brain/guardrails.py) · [tools/llm_gateway.py](../ailienant-core/tools/llm_gateway.py) · [core/dead_letter.py](../ailienant-core/core/dead_letter.py) | reflexion node; unified retry abstraction; DLQ redirect | 7.13.7 / 7.13.11 |
| `src/workspace/Workspace.tsx` · `workspaceStore.ts` · `InlineMutationManager.ts` · `HITLInterventionCard.tsx` · `StagingArea.tsx` · `workspace_panel.ts` | mode machine; stream watchdog; ACK; clear `isAborting`; seed version; cap buffers | 7.13.8 / 7.13.9 |
| dashboard panels (Hardware/Runtime/Rules/Audit) · `ContextOverlay.tsx` | verify-or-wire-or-delete per approved inventory; mount-poll → bus subscription; resume surface | 7.13.10 |
| [agents/coder.py](../ailienant-core/agents/coder.py) · [agents/analyst.py](../ailienant-core/agents/analyst.py) | consolidate VFS reader into one factory | 7.13.11 |
| `.gitignore` | add `.ailienant_telemetry.log` | 7.13.3 |

### 4.3 Reused (not modified)
`io_coalescer` debounce/mass-threshold · `token_ledger` lock pattern · `_ppr_tasks` cancel precedent · `throttled_stream` + `token_batcher` · `semantic_upsert` + indexer low-priority worker · OCC `document_version_id` / Delta State Sync · `ideation_loop` · `finops_gate` + token ledger · `SecretsScrubberFilter` · `apply_patch` + HITL · `request_human_approval` · the 7.9.B.17 off-loop dispatch · §3.4.6 `RuleManager` · §7.1.2 Privacy Gate.

---

## 5. Verification Plan

### 5.1 — 7.13.12 Checkpoint Gate

> **✅ GATE CLOSED (7.13.12).** All backend-assertable rows are certified by
> `ailienant-core/tests/test_phase7_13_checkpoint_gate.py` (20 tests) against the shipped
> entry points. The frontend-only rows **PR2** (Incognito halts the bus in `ide_sync.ts` — no
> backend hook exists), **OR1** (the Planner Manual-Mode React form), and **DB1** (the
> HTTP-served dashboard panels) are out of pytest scope and certified by `npm run compile` +
> the §5.2 inventory + the manual smoke. DoD green: `pytest` **768 passed**, `mypy .` **225
> OK**, `mypy --strict --follow-imports=silent` on the new file **0 errors**, `npm run
> compile` **0 errors**. **Phase 7.13 is closed; the §1 LOCK-IN expires.**

| # | Test | Assertion |
|---|---|---|
| SC1 | Silent telemetry | save/rename/delete emit `client_ide_telemetry` with **no** toast and **no** chat interruption |
| SC2 | Reactive index | one `file_saved` re-indexes **only** that file (verifiable in `.ailienant_telemetry.log`); delete/rename purge/migrate the row |
| PR1 | Privacy gate | a `.env` / excluded file is **never** pushed to the brain (Dual-Rules resolver) |
| PR2 | Incognito | the status-bar toggle halts the push bus instantly (no JSON edit) |
| CC1 | Graph lock | no phantom deps under concurrent reactive re-index + Dreaming (the per-project lock holds across `upsert_dependencies`/`purge_file_nodes`) |
| RL1 | Rate limit | an inbound save flood is rate-limited; the event loop is not swamped; answer latency unaffected |
| SF1 | Single-flight | rapid saves of one file coalesce to a single index pass |
| CN1 | Task cancel | background indexer + daemon tasks are cancelled on disconnect/shutdown (no orphans) |
| DR1 | Manual Dreaming | Dreaming fires **only** from the explicit action (no idle wake); a save mid-run aborts the txn + invalidates the snapshot |
| AL1 | Self-healing | an intentional mock error recovers via `ErrorCorrectionAgent` in ≤3 attempts, no raw error to the user; patches went through HITL; after the budget it redirects to the DLQ |
| ISO1 | Isolation | `agents/error_correction.py` never imports `brain.personality` (audit test) |
| FR1 | Stream watchdog | a hung "Streaming…" auto-clears via the watchdog when `server_stream_end` never arrives |
| FR2 | Correlation ID | reconnect mid-`SUBMIT_TASK` produces **no** duplicate generation (server-side dedup) |
| FR3 | Abort ACK | Stop with the WS down surfaces an error (ABORT_MESH ACK), never a silent failure |
| OR1 | Planner UI | Manual Mode renders an interactive locked multi-turn form, not standard chat |
| OR2 | DLQ resume | the dead-letter resume UI round-trips `/task/resume` + `/dlq/pending` |
| OR3 | Planner toggle | the Planner mode toggle reaches the backend (`client_planner_mode_toggle` wired) |
| DB1 | Dashboard | no panel shows "No data"; the genuine orphans are wired or deleted per the approved inventory; mount-poll panels are **visibility-gated** (amended from "Push-fed" per §5.2 / ADR-716 — the dashboard is HTTP-only, panels unmount on tab-switch, no leak) |
| TL1 | Telemetry log | WS payloads + node transitions logged, secrets scrubbed, UTF-8, bounded/rotated, `.gitignore`'d |
| DD1 | Dedup | no duplicate VFS readers (`coder.py` + `analyst.py`); retry budgets unified |
| REG | Regression | full pytest green (≥ 675 at 7.13 start); `npm run compile` 0 errors; `mypy --strict` 0 on new/modified files |

### 5.2 — Dashboard surface-sync inventory (filled during 7.13.10, user-approved)
| Control / metric | Panel / file | Expected endpoint / signal | Status (live/stub/dead) | Decision |
|---|---|---|---|---|
| Hardware profile | `HardwarePanel` | `GET /api/v1/hardware/profile` | live, mount-poll 3s | **visibility-gated poll** |
| Execution mode get/set | `HardwarePanel` | `GET/POST /api/v1/hardware/mode` | live, on-demand | verified |
| Runtime status | `RuntimePanel` | `GET /api/v1/runtime/status` | live, mount-poll 5s | **visibility-gated poll** |
| start-docker / pull-image | `RuntimePanel` | `POST /api/v1/runtime/*` | live, on-demand | verified |
| Soul / settings / reject | `RulesPanel` | `GET/POST /api/v1/system/*`, `/telemetry/reject` | live, on-mount | verified |
| Audit log / verify / stats | `AuditPanel` | `GET /api/v1/audit/*` | live, on-mount + paging | verified |
| `client_master_toggle` | `config.ts` type | backend WS handler live; no FE emitter / no host forward | dead (FE) | **delete dead FE type** |
| `client_profile_change` | `config.ts` type | backend WS handler live; no FE emitter / no host forward | dead (FE) | **delete dead FE type** |
| `OOM_ENGAGED` toast | `Workspace.tsx` | consumed (toast) but never emitted; `oom_fallback_active` only in graph state | dead (consumer) | **wire** (new `server_oom_engaged`) |
| Terminal context | `ContextOverlay` | manual paste → `ATTACH_CONTEXT kind:'terminal'` | live (manual by design — no VS Code API exposes terminal output) | verified |

**Architectural correction (ADR-716):** the dashboard is a backend-served HTML page
(`/dashboard/`) using same-origin HTTP `fetch` — no WebSocket, no host bridge. Panels are
conditionally rendered, so they unmount on tab-switch and tear down their intervals; the
"polling-cleanup leak" does **not** exist. A WS "telemetry-bus subscription" would require a
new dashboard WS subsystem + a periodic backend hardware/runtime emitter — over-engineering
for two correctly-behaving pollers. **Gate DB1 amended:** mount-poll panels are
*visibility-gated* (poll only while the dashboard is visible) rather than WS-Push-fed. The
dead `master_toggle`/`profile_change` FE types are deleted; their backend WS handlers are
retained (additive/harmless, flagged as a future backend cleanup).

**Manual smoke:** save a file → `.ailienant_telemetry.log` shows one incremental re-index, no toast. Open an excluded `.env` → no push; toggle Incognito → the bus halts. Click "Consolidate Memory" → Dreaming runs once; save mid-run → it aborts cleanly; no idle wake ever. Trigger a mock tool error → the agent self-heals silently, then DLQs after the budget. Kill the WS mid-stream → the spinner clears; reconnect → no duplicate generation. Toggle Planner Manual Mode → an interactive form appears. Open the dashboard → every panel shows real data or is gone.

---

## 6. Roadmap Impact

| Future phase | Risk | Mitigation in this blueprint |
|---|---|---|
| Phase 8 (Pruebas / Observabilidad) | re-spec a separate log sink / audit file | **7.13 absorbs Phase 8 observability** (ADR-712 + manifest Convenciones note); Phase 8 builds *on* `.ailienant_telemetry.log`, never re-creates it |
| Phase 10 (Onboarding / Antena) | the live-state panel needs node transitions | reuses the 7.13.3 node-transition telemetry + the 7.13.9 mode machine + the 7.13.10 Push-fed panels |
| Cognitive-isolation fence (4.1.5) | a self-healing agent could tempt a `brain.personality` import | ADR-711 reaffirms the fence; `ISO1` audit test stays green |
| `SCHEMA_EVOLUTION.MD` | new telemetry / divergence / ACK / requestId fields | all additive, scalar/optional with safe defaults; no removals/renames |
| 7.11 transport load | telemetry floods the WS shared with the inline diff-stream | ADR-708 priority class + `throttled_stream` size the channel; telemetry is droppable + `_pendingSends` capped |
| Completed phases 0–6 | the Push model silently mutates `[x]` backend code | §9 Backend Integration Matrix + manifest `**Ref:** 7.13.x` back-pointers make every retrofit traceable without un-checking those phases |

---

## 7. Anti-patterns (do **not** do this)

- ❌ Open a second WebSocket for telemetry. Ride the existing socket with a silent channel + priority class.
- ❌ Run an unlocked DELETE+INSERT on `dependency_graph`. The per-project `asyncio.Lock` guards both `upsert_dependencies` and `purge_file_nodes`.
- ❌ Let the daemon write the graph without the shared lock, or take the lock in `agents/mcts_coder.py` (it doesn't touch `core/db.py`).
- ❌ Leave inbound telemetry unbounded, or background indexer/daemon tasks un-cancelled on disconnect.
- ❌ Generate per-directory ignore files or a second exclusion list. Reuse the §3.4.6 Dual-Rules resolver. Push a file the resolver excludes, or ignore the Incognito state.
- ❌ Arm any idle/timer-based Dreaming trigger. Consolidation is **manual only** — explicit action → `client_dreaming_run`.
- ❌ Consolidate (Dream) over a graph a save just mutated. Abort the txn + invalidate the snapshot on divergence.
- ❌ Full workspace re-crawl on every save. Incremental single-file `semantic_upsert` under the lock + single-flight; content-hash idempotent.
- ❌ Raise toasts / interrupt the chat on background telemetry. Fire-and-forget, off-loop.
- ❌ Let telemetry frames starve `chat_token`. Telemetry is droppable; chat has absolute priority; `_pendingSends` is capped.
- ❌ Return a raw tool/schema/API error to the user. Route through the `ErrorCorrectionAgent`, then the DLQ.
- ❌ Import `brain.personality` into the `ErrorCorrectionAgent`. It is a cold engineering tool. Grant it no unsupervised disk writes — `apply_patch` + HITL only.
- ❌ Fire-and-forget abort, or an infinite "Streaming…" spinner, or duplicate generations on reconnect, or uncapped client buffers. Use ACKs, the watchdog, correlation IDs, and bounded queues.
- ❌ Write an unbounded / unrotated telemetry log, or one without `SecretsScrubberFilter` / explicit UTF-8, or commit it to git.
- ❌ Dump Planner Manual-Mode Q&A into the standard chat. Render the locked multi-turn form.
- ❌ Blind-delete an already-wired dashboard panel, or leave a panel showing "No data", or keep per-panel polling in a Push world. Verify-or-wire-or-delete per the approved inventory; convert mount-poll panels to bus subscriptions.
- ❌ Duplicate the VFS reader across `coder.py`/`analyst.py` / leave divergent retry caps. Consolidate.

---

## 8. Glossary

- **Spinal Cord** — the event-driven bus connecting IDE lifecycle events to the agent's cognition in real time (ADR-708).
- **Safety spine** — the concurrency & resource-safety foundation: per-project graph write lock, single-flight, inbound rate limit, per-session task cancel (ADR-714).
- **Silent telemetry channel** — the secondary, toast-free, droppable message protocol on the existing WebSocket.
- **Priority class** — the WS scheduling rule giving chat/answer frames absolute precedence over telemetry (ADR-708).
- **Dual-Rules resolver** — the §3.4.6 hierarchical `.ailienant/.ailienant.json` (local over global) single source of truth for telemetry exclusion (ADR-718).
- **Incognito Mode** — the VS Code status-bar toggle that instantly pauses the push bus without editing JSON (ADR-718).
- **Reactive indexing** — save-triggered single-file `semantic_upsert` + graph refresh under the lock + single-flight, content-hash idempotent (ADR-709).
- **Manual Dreaming** — the explicit, user-triggered (`client_dreaming_run`) workspace-context consolidation pass; **no idle timer** (ADR-710, rewritten).
- **Race discipline** — aborting a consolidation whose underlying graph a concurrent save invalidated, anchored on `document_version_id` (ADR-710).
- **Single-flight** — one in-flight index per `(filepath, project_id)`; rapid saves coalesce (ADR-714).
- **`ErrorCorrectionAgent`** — the self-healing reflexion node: traceback → read → fix → retry ≤3, then DLQ (ADR-711).
- **Failure-signature cache** — the cross-turn circuit breaker keyed on a normalized error signature (GAP8, ADR-711).
- **Telemetry log** — `.ailienant_telemetry.log`, the scrubbed/rotated/UTF-8 file sink for WS payloads + node transitions (ADR-712).
- **Correlation/request ID** — the per-request idempotency key enabling server-side dedup on reconnect (ADR-715).
- **Stream watchdog** — the client timeout that clears a stuck `isStreaming`/tool/natt state (ADR-715).
- **Mode machine** — the `Workspace.tsx` Chat / Planner / Dreaming-Dashboard state machine (ADR-713).
- **Verify-or-wire-or-delete** — the dashboard policy: every control is verified live, connected to a real endpoint, or removed — gated by a user-approved inventory; already-wired panels are not blind-deleted (ADR-716).
- **Push-fed panel** — a dashboard panel that subscribes to the telemetry bus instead of mount-polling (ADR-716).
- **Cognitive-isolation fence** — the Phase 4.1.5 rule that only the analyst imports the persona module; reaffirmed for the `ErrorCorrectionAgent`.
- **Orphan** — a backend capability (API/WS event) with no UI trigger, or a frontend type with no sender (ADR-716).

---

## 9. Backend Integration Matrix (Phases 0–6 → 7.13)

Phase 7.13 does **not** live in a vacuum: introducing the Push model silently couples it to code owned by the already-`[x]` backend phases. Each prior phase was complete *under a synchronous/pull scheme*; the event-driven flow exposes hidden friction (race conditions, leaks, duplication). These retrofits are owned by the 7.13 sub-phase noted, but **explicitly modify earlier-phase files** — the manifest's affected tasks carry a `**Ref:** 7.13.x` back-pointer so the `[x]` phases are not silently mutated.

| Prior phase | Forgotten aspect (friction under Push) | Required modification | Owned by |
|---|---|---|---|
| **0 & 1** — FastAPI/WS plumbing | **Orphan task lifecycle** — on a sudden client disconnect, background indexer/Dreaming tasks run on as zombies. *(Partly mitigated: `active_tasks` drains on shutdown + `cleanup_session` hook on disconnect already exist at `main.py:285/1045`.)* | **Extend** the existing cleanup-hook/registry (`main.py` lifespan + WS manager + `core/task_service.py`) to **cascade-cancel the background indexer + daemon tasks per session** (not just the abort-mesh `active_tasks`). | **7.13.1** |
| **2A–2D** — Inference / LLM gateway | **Dispersed retry + panic handling** — local retry logic in `tools/llm_gateway.py` + base agents; under a saturated event loop an LLM failure can throw a generic exception that freezes the telemetry pipeline / corrupts WS state. | **Decouple + unify** retries into one centralized exception abstraction wired to the DLQ: after the bounded retries, redirect the payload/task to `core/dead_letter.py` — never corrupt the WebSocket. | **7.13.7** (abstraction) + **7.13.11** (consolidation) |
| **3** — GraphRAG / LanceDB | **Dual write path** — Push gives two real-time writers: the **human** (FileWatcher save) and the **AI** (`apply_patch`). Two channels → duplicated LanceDB nodes / timing inconsistencies. | `core/indexer.py` enforces **one idempotent, content-hash entry point**; whether the change comes from the FileWatcher or the agent's virtual buffer, hash → if present, skip re-index. | **7.13.5** (GAP9) |
| **4** — MCTS / Planner | **Graph mutual-exclusion** — the Dreaming graph-reader path reads/expands the dependency graph while reactive index (7.13.5) + manual Dreaming (7.13.6) run `DELETE`/`INSERT` (`upsert_dependencies`/`purge_file_nodes`) → phantom reads / subgraph corruption. *(Note: `agents/mcts_coder.py` does **not** touch `core/db.py`; the readers are the daemon + GraphRAG extractor.)* | The **graph-reader path (daemon consolidation + GraphRAG extractor) must acquire the per-project `asyncio.Lock`** from 7.13.1; no consolidation/expansion may run while the graph is being restructured. | **7.13.1** (lock) + **7.13.6** (Dreaming honors it) |
| **5 & 6** — VFS / consistency | **Duplicate VFS readers** — `coder.py` and `analyst.py` each instantiate their own readers; under the Dual-Rules privacy filter (§3.4.6) divergent readers may ignore protected files or read stale buffers. | Replace local readers with the centralized **`VFSMiddleware` factory**; the single reader honors the Dual-Rules resolver + current buffer version. | **7.13.11** (incl. `analyst.py`, not just `coder.py`) |

---

*End of Phase 7.13 Blueprint (v2). The next compaction should still be able to re-derive intent from this single document.*
