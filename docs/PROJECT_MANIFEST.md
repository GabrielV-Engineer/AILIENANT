# AILIENANT: Project Manifest & Master Roadmap

> **Source of Truth.** This document is the executable WBS of the project. Architectural decision history lives in `SCHEMA_EVOLUTION.MD` and `DEV_JOURNAL.md`. Only the active contract remains here.
> For granular audit of completed steps per sub-phase, see `docs/DEV_JOURNAL.md` (Phase 8.x) and `docs/DEV_JOURNAL_ARCHIVE.md` (Phase 0–7.19).

---

## Status Dashboard

| Division | Status | Closed | Next action |
|---|---|---|---|
| 8.0 mypy --strict Campaign | ✅ CLOSED | 2026-06-08 | — |
| 8.1 Operational Stabilization | ✅ CLOSED | 2026-06-08 | — |
| 8.2 Resilience & Observability | ✅ CLOSED | 2026-06-19 | — |
| 8.2.6 Cold-Start / Warm-up Mode | ⬜ PENDING | — | 8.2.6.1 corpus-presence routing |
| 8.3 Benchmark Harness | ✅ CLOSED | 2026-06-13 | — |
| 8.4 MCP Hardening | ✅ CLOSED | 2026-06-11 | — |
| 8.5 External Gateway | ✅ CLOSED | 2026-06-13 | — |
| 8.6 Phase 8 Checkpoint Gate | ⬜ PENDING | — | 8.6 Phase 8 gate (8.2 done) |
| 8.7 Analyst Tri-Brain | ✅ CLOSED | 2026-06-11 | — |
| 8.8 Tool Parity Matrix | ✅ CLOSED | 2026-06-14 | — |
| 8.9 Portable Workspace Home | ✅ CLOSED | 2026-06-14 | — |
| 8.10 Debt Reduction + 8.2 + 8.6 | ⬜ PENDING | — | 8.10.4 execute Division 8.6 |
| 8.11 7-Mode Permission System | ⬜ PENDING | — | ADR + mode resolver |
| 8.12 Five-Layer Context Pipeline | ⬜ PENDING | — | context_pipeline.py |
| Phase 10 Documentation | ✅ CLOSED | 2026-06-11 | — |
| Phase 11 Dashboard Enterprise Redesign | ⬜ PENDING | — | 11.0 Design system |
| Phase 12 Human Evaluation Execution | ⬜ PENDING | — | 12.1 Corpus curation |
| Phase 13 Pre-Launch Innovation Sprint | ⬜ PENDING | — | 13.1 Prompt caching |
| Phase 14 Portfolio Level Release | ⬜ PENDING | — | 14.1 Dockerization |

---

## Phase Map (Quick Reference)

| Phase | Title | Status |
|---|---|---|
| 0 | Foundation, Structure & State Contracts | ✅ |
| 1 | Base Engine & Transport Plumbing | ✅ |
| 2A | Inference & Routing (2.0–2.1) | ✅ |
| 2B | I/O & Memory Stabilization (2.2–2.11) | ✅ |
| 2C | Runtime Anti-Entropy (2.12–2.15) | ✅ |
| 2D | Base Agent Layer (2.16–2.22) | ✅ |
| 3 | Evolutionary Memory System (GraphRAG) | ✅ |
| 4 | Agent Architecture & Mode Selector | ✅ |
| 5 | MCP Ecosystem, Permissions & Tool RAG | ✅ |
| 6 | Resilience, Sandboxing & Security (Enterprise Refactor) | ✅ |
| 7 | VS Code Extension (Frontend TS/React) | ✅ |
| 7.10 | Cognitive Transparency & Connective Integration | ✅ |
| 7.11 | VS Code Native Mesh Execution | ✅ |
| 7.12 | UX/State Stabilization & Context Injection Pathing | ✅ |
| 7.13 | The Enterprise Spinal Cord (Event-Driven Telemetry, Reactive Memory & Self-Healing) | ✅ |
| 7.14 | UI/UX Transformation to Enterprise Agent (Zero-Bubble & Full-Cognition) | ✅ |
| 7.15 | Agentic Core Remediation (Engine Re-Spine, RBAC Enforcement) | ✅ |
| 7.16 | Host-Delegated Tokenization & Rich Diff Rendering (DEBT-006) | ✅ |
| 7.17 | Streaming-AST Progressive Render (Hydration & Debounce Buffer) | ✅ |
| 7.18 | Six-Technique Enterprise Hardening Sweep | ✅ |
| 7.19 | Agentic Execution Cell & Persistent Audit Trail | ✅ |
| 8 | Testing, Refinement & Graceful Degradation | 🟡 Active |
| 8.2.6 | Cold-Start / Warm-up Workspace Mode (5 sub-phases) | ⬜ |
| 8.10 | Debt Reduction + Complete 8.2 + 8.6 (8 sub-phases) | ⬜ |
| 8.11 | 7-Mode Permission System | ⬜ |
| 8.12 | Five-Layer Context Compression Pipeline | ⬜ |
| 9 | Native Thinking (Real-Time Reasoning Stream) | ✅ |
| 10 | Professional Documentation & Public Presence | ✅ |
| 11 | Web Dashboard Enterprise Redesign (9 sub-phases) | ⬜ |
| 12 | Human Evaluation Execution | ⬜ |
| 13 | Pre-Launch Innovation Sprint | ⬜ |
| 14 | Portfolio Level (Standout Release) | ⬜ |

**Legend:** ✅ Closed · 🟡 Active · ⬜ Pending

---

## Manifest Conventions

- Every work item carries a `[x]` / `[ ]` checkbox and a reference to the target file when applicable.
- When a capability extends in a later phase, use **Ref:** `<phase>` instead of duplicating the spec.
- Historical architectural decisions (`[ARCH-PIVOT v3]`, `[ARCH-FINAL]`, etc.) do **not** appear in the body; they live in `SCHEMA_EVOLUTION.MD`.
- Each phase ends with a **Checkpoint Gate** validation (DoD criteria).
- **Absorption 7.13 → Phase 8:** Phase 7.13 absorbs the observability requirements originally planned for Phase 8. Phase 8 must NOT re-create log sinks or audit files — it only builds on the 7.13.3 telemetry channel.
- **Completed task items (Phase 0–7.19):** one-line title only. Full execution history → `docs/DEV_JOURNAL_ARCHIVE.md`.

---

## PHASE 0 — Foundation, Structure & State Contracts

> The immutable foundation. Defines data sovereignty, bicephalic cognitive flow, and entropic environment shielding.

- [x] **0.1. Monorepo Architecture & Resilience Layers.** `/ailienant-core` (FastAPI/LangGraph), `/ailienant-extension` (VS Code/TS), `/docs`; VFS Middleware Layer — backend never queries disk directly for active files. See DEV_JOURNAL_ARCHIVE.
- [x] **0.2. Bicephalic Neural Schema (Pydantic/TypedDict).** `AIlienantGraphState` with SQLite persistence; `immutable_wbs`; `ContextMeter (CSS)` hybrid routing; OCC `document_version_id` headers. See DEV_JOURNAL_ARCHIVE.
- [x] **0.3. Shielded API Contracts (I/O — VFS Ready).** `REST POST /task/submit` + `WebSocket WS /ws/v1/stream/{id}` with `VRAM_OOM_FALLBACK` / `HITL_ASYMMETRIC_FRICTION` streaming protocol. See DEV_JOURNAL_ARCHIVE.
- [x] **0.4. Cognitive Bicephalia, RBAC & XML Sandboxing.** 4-node power topology (Planner/Orchestrator/Logic/Analyst); XML `<file_content>` boundary delimiters; RBAC mode enforcement. See DEV_JOURNAL_ARCHIVE.

---

## PHASE 1 — Base Engine & Transport Plumbing

> Communication infrastructure. Goal: zero latency and absolute conversation state persistence.

- [x] **1.0. AI Engine Foundation (Spec-Driven Development).** `MissionSpecification` master contract; LiteLLM `localhost:4000` gateway; `python-dotenv` config isolation. See DEV_JOURNAL_ARCHIVE.
- [x] **1.1. Frontend — Entropy Extractor (Payload Builder).** `PathResolver`/`WorkspaceHash`; `manual_attachments`; dirty buffers capture; `document_version_id`; `POST /api/v1/task/submit`. See DEV_JOURNAL_ARCHIVE.
- [x] **1.2. Intent Interceptor & Static Routing (Shift-Left AST).** `IntentRouter` in `ailienant-extension/src/core/IntentRouter.ts` — regex + VS Code AST for instant local codemods (<5 ms). See DEV_JOURNAL_ARCHIVE.
- [x] **1.3. Backend — VFS Middleware & Ingestion.** `core/vfs_middleware.py` singleton; `vfs.read()` RAM-first proxy; `core/task_service.py` entropy layer; unified `main.py` (HTTP + WebSocket). See DEV_JOURNAL_ARCHIVE.
- [x] **1.3.1. Context Firewall (Shift-Left Filter Engine).** Layer 1 Git/Ignore; Layer 2 binary block; Layer 3 anti-OOM heuristics (>500 KB / minified). See DEV_JOURNAL_ARCHIVE.
- [x] **1.3.2. Safe Crawler (Symlink Loop Protection).** `InodeSet` per-inode dedup; `max_depth=5` configurable scan. See DEV_JOURNAL_ARCHIVE.
- [x] **1.4. Bidirectional WebSocket Manager.** `TOKEN_CHUNK` / `TELEMETRY_UPDATE` / `GRAPH_MUTATION` async emission; `PLANNER_MODE_TOGGLE` protocol; HITL bidirectional channel `HITL_APPROVAL_REQUIRED` ↔ `HITL_RESPONSE`. See DEV_JOURNAL_ARCHIVE.
- [x] **1.5. Optimistic Concurrency Control (OCC) Gatekeeper.** Extension intercepts `GRAPH_MUTATION`; validates `document_version_id`; rejects with `CONCURRENCY_CONFLICT` on mismatch. See DEV_JOURNAL_ARCHIVE.
- [x] **1.6. Sovereign Internal Gateway (LiteLLM Integration).** Local LiteLLM proxy; alias routing (`small`/`medium`/`big`); autodiscovery `GET /api/v1/models/available`; Zero-Touch config bootstrap; local engine auto-detection. See DEV_JOURNAL_ARCHIVE.
- [x] **1.7. VFS AST Engine (Tree-sitter).** `tree-sitter` integrated into `vfs_manager`; AST generated and cached per file at index time. See DEV_JOURNAL_ARCHIVE.
- [x] **1.8. State & Catalog Tables in SQLite (`core/db.py`).** `session_state` table; `tool_registry` table (prereq for Tool RAG — Phase 5.2). See DEV_JOURNAL_ARCHIVE.

---

## PHASE 2 — Inference Engine, Core Stabilization & Agent Layer

> Central nervous system. LangGraph orchestration, hardware-level memory management, secure hybrid routing, and agent swarm construction.

- [x] **2.0. PlannerAgent & Conditional Routing Logic (MoE Hybrid + Model Cascading).** TCI<30% → Local; TCI>30% + CSS<40% → Cloud; cascading from ultrafast to flagship. See DEV_JOURNAL_ARCHIVE.
- [x] **2.0.1. Advanced LangGraph Topology (MapReduce for High-TCI).** Fan-out concurrent CoderAgent clones at TCI>80%; Fan-in reducer. See DEV_JOURNAL_ARCHIVE.
- [x] **2.1. 3D Routing Matrix & Tokenization.** O(M) heuristic in `routing_engine.py`; `tiktoken` precision; Vision Bypass for multimodal payloads. See DEV_JOURNAL_ARCHIVE.
- [x] **2.1.5. Dynamic Concurrency (Fan-Out / Fan-In).** Relay State Machine (sequential, Local); Team Swarms (parallel, Cloud); Reducer node for TypedDict merge. See DEV_JOURNAL_ARCHIVE.
- [x] **2.2–2.11. I/O, Memory & Inference Engine Stabilization.** Tiered model caching; SQLite WAL mode; WAL checkpointer; graceful shutdown flush; ProcessPoolExecutor; tiered checkpointing L1/L2; GraphRAG PPR; lazy workspace indexing; state compression (StateSummarizer); I/O debouncing. See DEV_JOURNAL_ARCHIVE.
- [x] **2.12–2.15. Runtime Anti-Entropy.** Dynamic thresholding for branch switches; output parser guardrails; backpressure; memory janitor. See DEV_JOURNAL_ARCHIVE.
- [x] **2.16–2.22. Base Agent Layer.** WorkspaceLifecycleManager; SessionStateManager; `create_subprocess_exec` migration; parallel TaskManager; Shadow Planner multi-turn HITL; background task DAG; workspace overview. See DEV_JOURNAL_ARCHIVE.

---

## PHASE 3 — Evolutionary Memory System (Hybrid GraphRAG)

> Code graph constructed from dependency analysis + semantic vector search, updated reactively.

- [x] **3.1–3.7. Hybrid GraphRAG (PPR + Skeleton Prompting + LanceDB).** `core/memory/semantic_memory.py` + `graphrag_extractor.py`; networkx graph; LanceDB vector store; PPR compute in ProcessPoolExecutor; skeleton prompting; MCP adapter; anti-entropy daemon. See DEV_JOURNAL_ARCHIVE.

---

## PHASE 4 — Agent Architecture & Mode Selector

> Eight-role CoderAgent, policy engine, circuit breaker, workspace lifecycle, chaos gate.

- [x] **4.1–4.5. Agent Policy Engine & Validators.** OrchestratorAgent; 8-role CoderAgent (`ROLE_REGISTRY`); AnalystAgent (SOUL.md); Pydantic validators; circuit breaker; WorkspaceLifecycleManager; Chaos Crucible gate. See DEV_JOURNAL_ARCHIVE.

---

## PHASE 5 — MCP Ecosystem, Permission Engine & Tool RAG

> Composable permission system, Tool RAG store, perception/mutation tools, sandboxed bash, adversarial gate.

- [x] **5.1–5.7. Permission Engine, Tool RAG & Execution Tools.** Permission engine (`rbwe_guard`/`evaluate_action`); Tool RAG store; perception tools (AST inspect, symbol references, data flow trace, doc parser, web fetch); mutation tools (atomic patch, batch edit, file write); `SandboxBashTool`; adversarial gate. See DEV_JOURNAL_ARCHIVE.

---

## PHASE 6 — Resilience, Sandboxing & Security (Enterprise Refactor) ✅

> Zero-Trust execution layer for agents: real host isolation, FinOps emergency brake, SOC2-compatible audit log, graceful OOM/node-crash recovery.

### Binding Architectural Decisions

- **[ADR-001] Pluggable Sandbox with Graceful Degradation.** Adapter resolved once at startup: `DOCKER` (default, 2 s probe) → `NATIVE_HITL` (HITL-before-spawn fallback) → `WASM` (opt-in, pure-compute). Active tier is session-global and immutable.
- **[ADR-002] Wasm Scope Guard.** `wasmtime` restricted to stateless pure-compute; any import outside `wasi_snapshot_preview1` raises `WasmScopeError`.
- **[ADR-003] Canonical HITL Channel Reuse.** No new approval transport; all friction reuses `vfs_manager.request_human_approval(...)` from Phase 1.4/2.27.
- **[ADR-004] Strictly Additive State Growth.** 6 new channels are scalar-overwrite with safe defaults; Phase 5.7 checkpoints deserialize without changes.

### Phase Tasks

- [x] **6.1.1. DockerSandboxAdapter.** `core/sandbox.py`; kernel-side timeout (`timeout --foreground`); `--read-only` rootfs + `/work` tmpfs + `--network none`; long-lived container reuse. See DEV_JOURNAL_ARCHIVE.
- [x] **6.1.2. NativeHITLSandboxAdapter.** Every invocation emits HITL approval before spawn; rejection → `SandboxResult(exit_code=-1, stderr="[hitl_denied]")`; timeout → DLQ enqueue. See DEV_JOURNAL_ARCHIVE.
- [x] **6.1.3. WasmSandboxAdapter (opt-in pure-compute).** `wasmtime-py`; fuel-metered (5M instructions); `_inspect_module_scope` scope guard; temp-file I/O isolation. See DEV_JOURNAL_ARCHIVE.
- [x] **6.1.4. Startup Resolution.** `resolve_default_adapter()` in FastAPI lifespan; result persisted as `ACTIVE_TIER`/`ACTIVE_ADAPTER` globals. See DEV_JOURNAL_ARCHIVE.
- [x] **6.2. HITL Bridge & Asymmetric Friction.** All EXECUTE/DANGEROUS tools dispatch via `ACTIVE_ADAPTER.execute(...)`; `DANGEROUS_COMMANDS_REGEX` intercept retained as pre-adapter choke-point. See DEV_JOURNAL_ARCHIVE.
- [x] **6.3. OOM Cascade & Inference Resilience.** `ContextWindowExceededError` + CUDA OOM cascade: VRAM purge → `oom_fallback_active` → trimmed cloud fallback. **Ref:** 7.13.7 for centralized abstraction. See DEV_JOURNAL_ARCHIVE.
- [x] **6.4. ACID Atomic Transactions & Resume API (`core/dead_letter.py`).** DLQ table; `dead_letter_decorator` on 4 engine entry points; `POST /api/v1/task/resume/{task_id}`; `GET /api/v1/dlq/pending`. See DEV_JOURNAL_ARCHIVE.
- [x] **6.5. FinOps Cost Circuit Breaker & Graph Health Monitor (`core/supervisor.py`).** Sync ledger → state; hard-kill / HITL-soft-gate / token-spike / audit-chain-verify triggers; 6 new state channels. See DEV_JOURNAL_ARCHIVE.
- [x] **6.6. FinOps Ledger & Token Accounting.** Session-scoped cost accumulation; real-time `token_ledger`; SOC2 audit chain (`hitl_audit_chain_head`). See DEV_JOURNAL_ARCHIVE.
- [x] **6.7. Secret Scrubber & Privacy Filter.** `SecretsScrubberFilter` on all telemetry; PII patterns configurable. See DEV_JOURNAL_ARCHIVE.
- [x] **6.8–6.9. Resilience Hardening & Additional Entrypoints.** `supervisor_node` wired into LangGraph; DLQ decorator extended; OOM routing refined. See DEV_JOURNAL_ARCHIVE.
- [x] **6.10. Phase 6 Checkpoint Gate.** `tests/test_phase6_checkpoint_gate.py` — DoD: `pytest` green, `mypy --strict core/sandbox.py` exit 0, Docker/Wasm/NativeHITL adapter round-trips asserted. See DEV_JOURNAL_ARCHIVE.

---

## PHASE 7 — VS Code Extension (Frontend TypeScript/React) ✅

> Enterprise code agent UI: Zero-Bubble canvas, streaming diff engine, full HITL mesh, tool chips.

- [x] **7.1–7.9.B. Full Frontend Buildout.** Entire VS Code extension architecture: workspace panel, NattCanvas chat, staging area, inline editor mutations (SEARCH/REPLACE), GraphRAG code graph overlay, HITL approval card, mode switcher, tool chips, streaming token rendering, session persistence, runtime widget (Docker probe + status panel). See DEV_JOURNAL_ARCHIVE.

---

## PHASE 7.10 — Cognitive Transparency & Connective Integration ✅

> Identity sovereignty, token batch streaming, analyst context injection, envelope-tolerant JSON, checkpoint gate.

- [x] **7.10.1–7.10.5.** Persona/identity (ADR-701); token throttle (ADR-702); analyst context-injection + budget + uuid-tag XML sandboxing (ADR-703); structured-JSON envelope tolerant parsing (ADR-704); security posture (ADR-705); Phase 7.10 checkpoint gate. See DEV_JOURNAL_ARCHIVE.

---

## PHASE 7.11 — VS Code Native Mesh Execution ✅

> Inline editor mutations, WebView rehydration, abort mesh, @mentions, markdown parser, tool chips, HITL notifications, time-travel debugging.

- [x] **7.11.1–7.11.8.** Inline mutation manager (SEARCH/REPLACE in VS Code API); WebView rehydration from WAL; abort mesh (VS Code mesh + ADR-706); @mention parser + markdown streaming parser; tool chips UI; HITL notification toast; time-travel (Rewind) debugging via checkpoint browser. See DEV_JOURNAL_ARCHIVE.

---

## PHASE 7.12 — UX/State Stabilization & Context Injection Pathing ✅

> UX hardening, mypy namespace fix, E2E lifecycle stabilization.

- [x] **7.12–7.12.9.** UX stabilization pass; mypy namespace package fix; E2E task lifecycle hardening; context injection path correctness. See DEV_JOURNAL_ARCHIVE.

---

## PHASE 7.13 — The Enterprise Spinal Cord ✅

> Paradigm shift Pull → Push. Event-driven architecture: live IDE telemetry, reactive GraphRAG, manual Dreaming, self-healing, stream resilience, Planner UI, OOM routing, VFS unification.

- [x] **7.13.1. Concurrency & Resource Safety Spine (ADR-714).** `graph_write_lock` per project; `SingleFlightCoordinator`; WS inbound token-bucket rate-limit; abort cascade on session disconnect. See DEV_JOURNAL_ARCHIVE.
- [x] **7.13.2. Privacy & Telemetry Filtering — Dual-Rules + Incognito (ADR-718).** `core/rules.py` hierarchical exclude patterns; Incognito mode status-bar toggle; Layer 0 gate in VFS middleware. See DEV_JOURNAL_ARCHIVE.
- [x] **7.13.3. Live Telemetry Log (ADR-712).** `core/telemetry_log.py` async-safe `QueueHandler+Listener`; `SecretsScrubberFilter`; `RotatingFileHandler` UTF-8 size-bounded; `.ailienant_telemetry.log`. See DEV_JOURNAL_ARCHIVE.
- [x] **7.13.4. IDE Telemetry Bus — Push (ADR-708).** `onDidSave/Rename/Delete` listeners; privacy-gated `client_ide_telemetry` channel; droppable priority class in WS client. See DEV_JOURNAL_ARCHIVE.
- [x] **7.13.5. Reactive GraphRAG (ADR-709).** `ReactiveIndexer.index` with content-hash dedup; `_ReactiveBreaker` per (project, file); delete/rename purge graph + vector; `project_id` correctly wired. See DEV_JOURNAL_ARCHIVE.
- [x] **7.13.6. Manual Dreaming — on-demand "Consolidate Memory" (ADR-710).** `OvernightDaemon` repurposed as on-demand service; `DreamingTrigger.tsx` HUD button + VS Code command; OCC epoch guard (stale snapshot → cancel + abort). See DEV_JOURNAL_ARCHIVE.
- [x] **7.13.7. Self-Healing — ErrorCorrectionAgent + DLQ Resume Surface (ADR-711+ADR-716).** `agents/error_correction.py` + `reflexion_guard` in `dead_letter_decorator`; `brain/retry_policy.py` (centralized budgets); `brain/failure_breaker.py` (cross-turn signature cache); `RecoveryPanel.tsx`. See DEV_JOURNAL_ARCHIVE.
- [x] **7.13.8. Frontend Stream Resilience (ADR-715).** `request_id` dedup on reconnect; Zero-Config stream watchdog; send-queue cap; `isAborting` lifecycle fix; HITL ACK; `document_version_id` seeded at WS open. See DEV_JOURNAL_ARCHIVE.
- [x] **7.13.9. Multi-Turn State Machine & Planner UI (ADR-713).** `surface: 'chat'|'planner'` axis; `ModeSwitcher.tsx`; `PlannerSession.tsx` Socratic form; `planner_mode_active` in submit payload. See DEV_JOURNAL_ARCHIVE.
- [x] **7.13.10. Push-Fed Panels & Surface Sync (ADR-716).** `usePollingWhileVisible` visibility-gated polling; `server_oom_engaged` event routed by `task_id`; `master_toggle`/`profile_change` dead types removed. See DEV_JOURNAL_ARCHIVE.
- [x] **7.13.11. VFS Reader Unification.** `core/vfs_middleware.make_safe_reader` — single canonical VFS reader replacing 3 agent-local readers; named LLM/WAL retry constants. See DEV_JOURNAL_ARCHIVE.
- [x] **7.13.12. Phase 7.13 Checkpoint Gate.** `tests/test_phase7_13_checkpoint_gate.py` (20 tests); DoD: `pytest` 768 passed · `mypy .` 225 OK · `npm run compile` 0. See DEV_JOURNAL_ARCHIVE.

---

## PHASE 7.14 — UI/UX Transformation to Enterprise Agent ✅

> Frontend-only (ADR-721). Zero-Bubble canvas, Elite Diff Engine inline, PlanAcceptancePanel, collapsible staging.

- [x] **7.14.0–7.14.7.** Blueprint lock-in (ADR-721); Zero-Bubble canvas (no inline spinner); Elite Diff Engine (shiki host tokenization, rAF-coalesced render); `PlanAcceptancePanel` (55/45 split); staging area with approve/reject; streaming markdown parser; bundle ceiling sentinel; checkpoint gate. Gate: `npm run compile` 0 · `npm run lint` 0 · backend contract certified by Phase 7.15. See DEV_JOURNAL_ARCHIVE.

---

## PHASE 7.15 — Agentic Core Remediation ✅

> Backend re-spine: `_run_coding_task` now enters the compiled LangGraph engine, unlocking mode routing, Rewind, token streaming, and ideation loop.

- [x] **7.15.0–7.15.7.** Root cause: `_run_coding_task` called planner/coder nodes directly, bypassing compiled graph. Fix: re-route through `alienant_app.ainvoke`. Also: RBAC wiring (`evaluate_action`/`rbwe_guard`); Spanish prompt eradication; `planner_mode_registry` now read; phantom "not yet enabled" removed; checkpoint gate (11 rows). Gate: `mypy .` 0/246 · `pytest` 908 passed. See DEV_JOURNAL_ARCHIVE.

---

## PHASE 7.16 — Host-Delegated Tokenization & Rich Diff Rendering ✅

> shiki grammar engine runs in Node host (no bundle ceiling), emits token AST via IPC to webview renderer. Closes DEBT-006.

- [x] **7.16.0–7.16.3.** ADR-733/734/735; static pipeline (AST contract + host lexer + renderer spans); token span IPC; closing DEBT-006. Gate: `mypy .` 0 · `npm run compile` 0. See DEV_JOURNAL_ARCHIVE.

---

## PHASE 7.17 — Streaming-AST Progressive Render ✅

> Streaming AST hydration with React reconciliation + debounce anti-flicker. Agent token-stream integrated.

- [x] **7.17.0–7.17.2+B.** ADR-736/737/738/739; progressive hydration buffer; rAF-coalesce + memoized CodeLine; dead-sink isolation; response_format drop on thinking turns (DEBT-013 logged). Gate: `mypy .` 0/246 · `pytest` 918 passed. See DEV_JOURNAL_ARCHIVE.

---

## PHASE 7.18 — Six-Technique Enterprise Hardening Sweep ✅

> Closed execution feedback loop: `run_command` now dispatches via sandbox adapter, structural diagnostics, recency heatmap RAG, code-STYLE few-shot, AST-hashed semantic cache, OCC Option-A assertion. Closes DEBT-009 defer precondition.

- [x] **7.18.0–7.18.6.** Closed-loop sandboxed executor (ADR-740); recency heatmap (ADR-741); `response_format` graceful degradation (ADR-742); AST-skeleton few-shot (ADR-743); AST-hashed semantic response cache (ADR-744); MCTS-into-live-loop deferred to cell (ADR-745, DEBT-009); checkpoint gate (ADR-746, 9 rows). Gate: `mypy .` 0/245 · `pytest` 908 passed. See DEV_JOURNAL_ARCHIVE.

---

## PHASE 7.19 — Agentic Execution Cell & Persistent Audit Trail ✅

> Continuous bidirectional PTY ReAct loop as a LangGraph cell coexisting with `run_command`. PTY session, workspace sync, multi-axis governor, Glass-Box telemetry, interactive terminal, checklist + GFM tables. Closes DEBT-009.

- [x] **7.19.0–7.19.8.** `SandboxSession` ABC + PTY multiplexer (ADR-747); workspace sync engine VFS↔sandbox+OCC (ADR-748); agentic ReAct cell with MCTS candidate selection — closes DEBT-009 (ADR-749); multi-axis governor steps/tokens/time (ADR-750); WebSocket Cell Event Dispatcher (ADR-751); shadow-DOM audit widgets + virtualizer (ADR-752); interactive chat PTY + Send/Stop toggle (ADR-753); execution checklist + GFM tables (ADR-754); checkpoint gate (ADR-755, 11 backend rows + FE smoke). Gate: `mypy .` 0/261 · `pytest` 957 passed · `npm run compile` 0. See DEV_JOURNAL_ARCHIVE.

---

## PHASE 8 — Testing, Refinement & Graceful Degradation

> Performance calibration and failure simulation for Enterprise robustness.

### Division 8.0 — mypy --strict Eradication Campaign ✅

> Campaign goal achieved 2026-06-08: `mypy --strict main.py` → 0 · zero `follow_imports=silent` entries in `mypy.ini`.

- [x] **8.0.A — Baseline audit (docs-only).** `PHASE_8_BLUEPRINT.md` + `TECH_DEBT_BACKLOG.md` created; 5 DEBT entries pre-registered; Tier 0 → Tier 7 topological map.
- [x] **8.0.0 — Surface mechanical fixes.** 64 errors in 20 files corrected; DEBT-003/004 closed.
- [x] **8.0.FE — Frontend TypeScript/Pylance gate.** `tsc --noEmit` + `eslint src` + `node esbuild.js` → exit 0 permanent; bundle ceiling sentinel active.
- [x] **8.0.1 — Unsilence low-fan-in leaves.** `shared.hardware`, `agents.analyst`, `tools.patch_tool`, `brain/ideation.py`; 3 `follow_imports=silent` blocks removed (9→6). DEBT-001 closed.
- [x] **8.0.2 — Unsilence `tools.llm_gateway`.** Consumer repairs: `contract_guard.py`, `summarizer.py`, `coder.py`; 1 block removed (6→5). DEBT-015/016 closed.
- [x] **8.0.3 — Unsilence `core.vfs_middleware` + `core.compute_pool`.** 5 downstream dead ignores swept; 2 blocks removed (5→3).
- [x] **8.0.4 — Primary goal achieved: `mypy --strict main.py` → 0.** `tool_rag_select_node` retyped; last `type-var` ignore resolved. DEBT-014 reduced.
- [x] **8.0.5 — Unsilence `brain.memory` + `core.db`.** `[mypy-networkx,networkx.*]` config block added; 2 inline ignores removed; 1 block removed (3→1). DEBT-018 logged.
- [x] **8.0.6 — Unsilence `api.websocket_manager` — last silent module.** 6 `dict` → `Dict[str, Any]` typed; 0 `follow_imports=silent` remaining. DEBT-019 logged.
- [x] **8.0.7 — `brain/engine.py` certified strict-clean.** Transitive — 0 code changes; integrity confirmed.
- [x] **8.0.8 — Campaign closure.** 35 inline ignores audited → 28; config-level cleanup for lancedb/docker/requests. DEBT-020/021/022/023 logged.

---

### Division 8.1 — Operational Stabilization & Enterprise Hardening ✅

> Closed DEBT-019/018/020/021/022/023. All `mypy .` gates remained at 0/248.

- [x] **8.1.A — DEBT-019: WebSocket buffer leak fix.** Guard-at-store in `resolve_*`; `sweep_and_wake` in `disconnect()`; `tests/test_ws_buffer_lifecycle.py` (6 rows). Gates: pytest 930 passed.
- [x] **8.1.B — DEBT-018: NetworkX graph eviction.** `MAX_GRAPH_EDGES=5000` cap; `G.clear()` in `finally` teardown in both networkx builders.
- [x] **8.1.C — DEBT-020: Tree-sitter type ignores.** 7 ignores resolved via `param: Any` + local `node: Any` guard.
- [x] **8.1.D — DEBT-021: io_coalescer type-arg ignores.** 5 ignores resolved: `asyncio.Task[None]`, `Callable[..., Any]`.
- [x] **8.1.E — DEBT-022: WebSocket manager Literal narrowing.** 4 broadcast params narrowed; 1 `cast` in `task_service.py`.
- [x] **8.1.F — DEBT-023: Misc single-site ignores.** 5 ignores closed: main.py middleware, sessions.py checkpoint, resource_manager Resolution cast, llm_gateway on_thinking guard.

---

### Division 8.2 — Resilience & Observability ✅

> Operational resilience and observability. Gates: 8.2.5 DoD-check (no gate — gate is Division 8.6). Builds on 7.13.3 telemetry channel — no new log sinks.

- [x] **8.2.1 — End-to-End Tests (`tests/e2e/`)**
  - Validate the full SSoT stack: Prompt → GraphRAG → LangGraph → MCP → WebSocket Response. **DoD:** one E2E case traverses the compiled graph over real HTTP/WS and returns an applied patch.
- [x] **8.2.2 — Fast Track + Observability**
  - Builds on the 7.13.3 telemetry channel (`telemetry_log` / `.ailienant_telemetry.log`); creates NO new sink. The Fast Track is the **TCI-0 pre-RAG path** inside `resolve_provider`/`derive_routing_decision` — not a parallel bypass. LangSmith traces over the existing channel. **DoD:** a trivial query skips GraphRAG via the existing routing engine; zero new sinks.
- [x] **8.2.3 — Hardware Fallbacks (Graceful Degradation)**
  - VRAM threshold is config, not constant (reconcile `<16GB` vs `<1GB` from Phase 10.3); bypass to Cloud on insufficient VRAM. **DoD:** VRAM below the configured threshold routes to Cloud without crash.
  - [x] **8.2.3.1 — Graph Weight Calculator (Context OOM Predictor)**
    - Algorithm that calculates State size (Tokens × Model) *before* executing the prompt — feeds the hardware semaphore from 7.5.3.
- [x] **8.2.4 — Hardware Stress Simulator (Chaos Engineering)**
  - Script that artificially consumes RAM/VRAM and validates that `hardware_profiler` triggers real fallbacks (pause indexing, switch to Cloud). **DoD:** synthetic pressure triggers the fallback observable in telemetry.
- [x] **8.2.5 — DoD-check** *(not the gate)* — resilience smoke green.

---

### Division 8.2.6 — Cold-Start / Warm-up Workspace Mode ⬜

> A cold or tiny, unstructured workspace currently behaves as the *most expensive* path: an empty corpus drives `CSS ≤ 20` → RED ALERT → forced CLOUD on every turn (Mini-Judge bypassed), plus a wasted embedding per turn and a full crawl on 1–5 file folders. Root cause is **routing conflating "no corpus to retrieve from" with "rich corpus but low coverage."** This division makes a cold/tiny workspace stay local-first and cheap, and lets a local endpoint dropping mid-session degrade gracefully. It is the **concrete realization of the empty-corpus discrimination that 8.2.2 (Fast Track / TCI-0 pre-RAG path) anticipated** — cross-reference, not duplication; 8.2.2 stays open for its LangSmith/observability scope. **Out of scope (rejected as over-engineering):** per-tier embeddings, a full capability registry (context-window/vision/tools), a persistent capability cache, moving the native-thinking allowlist into the registry.

- [ ] **8.2.6.1 — Corpus-presence probe + empty-vs-low-coverage routing.**
  [Add `SemanticMemoryManager.is_corpus_empty(workspace_hash)` — a cheap LanceDB table/row-count check, short-TTL cached, invalidated on index write. Extend `derive_routing_decision(tci, css, corpus_empty=False)` (additive optional param) to skip the `css<40 → CLOUD` red-alert floor when the corpus is empty, routing by TCI bands alone. In `agents/planner.py`: `is_red_alert = (css < 40.0) and not corpus_empty`; pass `corpus_empty` into the routing call. CSS stays truthful in telemetry — only the escalation decision changes. **DoD:** empty corpus + `tci<30` → `LOCAL_SMALL`; **regression guard:** non-empty corpus + `css<40` still → CLOUD. Target files: `core/memory/semantic_memory.py`, `core/memory/context_auditor.py`, `agents/planner.py`.]

- [ ] **8.2.6.2 — Skip embedding on an empty store.**
  [`SemanticMemoryManager.search_with_paths` short-circuits `return (0.0, [], [])` via `is_corpus_empty(workspace_hash)` before calling `_get_embedding`. Behavior is identical (an empty store returns `[]` anyway) but saves one embedding backend call per planner turn on a cold workspace. **DoD:** zero `_get_embedding` calls on a cold workspace (mock-asserted). Target file: `core/memory/semantic_memory.py`.]

- [ ] **8.2.6.3 — Warm-up indexing gate.**
  [`_WARMUP_MIN_FILES` constant (default 5): in `indexer._run`, when `0 < total < _WARMUP_MIN_FILES`, defer the full crawl, mark complete, and fire the complete event (log warm-up mode). Reactive indexing (7.13.5) still indexes real files on save as the project grows; the next `client_workspace_init` that sees `≥ _WARMUP_MIN_FILES` runs the full crawl. Safe because the crawl is recoverable and reactive coverage backfills. **DoD:** sub-threshold defers; threshold runs. Target file: `core/indexer.py`.]

- [ ] **8.2.6.4 — Mid-session local-endpoint failover.**
  [In the BYOM call path (`tools/llm_gateway.py`, `acomplete_byom`/`astream_byom`): on a non-OOM `APIConnectionError` where the resolved target `is_local`, resolve the next available target via the existing `model_resolver._directional_order(tier)` and retry **once** (guard with a `_failover_attempted` flag — no loops). If the directional neighbour is Cloud with no key configured, the fallback fails cleanly and the original error surfaces — never swallowed (CLAUDE.md §5.2). Inference retry is idempotent (read-only, no state mutation — §5.3). **DoD:** simulated local drop falls back to the next tier; a second failure re-raises without looping. Target file: `tools/llm_gateway.py`.]

- [ ] **8.2.6.5 — Division 8.2.6 Checkpoint Gate.**
  [`tests/test_phase8_2_6_warmup_gate.py` (sibling-file convention): `is_corpus_empty` True on a fresh store / False after a write; empty corpus + `tci<30` → LOCAL_SMALL with `is_red_alert` False; the non-empty `css<40` → CLOUD regression guard; `search_with_paths` makes zero embed calls on a cold store; warm-up defer vs run at the threshold; B4 single-retry then re-raise. **DoD:** `mypy .` 0 · `pytest` green. No FE surface, so `npm run compile` is not required (a "Warm-up mode" HUD badge is an optional Phase 11 follow-up). **SCHEMA_EVOLUTION note (record at implementation, not now):** `derive_routing_decision` gains an additive optional `corpus_empty` param (default `False`) — backward compatible, no contract break.]

---

### Division 8.3 — Precision Benchmarking & Ablation Study ✅

> Empirical moat proof: architectural uplift (H₁) and routing efficiency (H₂). Pass@1 (HumanEval/MultiPL-E) vs Resolve@k (k≤3 ReAct cycles). Factorial 2×2 design; Wilson CI; `seed=42`/`temp=0`.

- [x] **8.3.0 — Blueprint + harness scaffold (`tests/benchmark/`).** `arms.py`, `metrics.py`, `hygiene.py`, `runner.py`, `problems.py`; measurement hygiene (embeddings preflight, cache off, telemetry as TCI/CSS source of truth). Gates: test_harness_scaffold 7/7 · mypy 0/299 · pyright 0 · suite 1233 passed.
- [x] **8.3.1 — Codegen adapter — HumanEval (Python) + MultiPL-E (TypeScript).** Pass@1 model-solo baseline; `SandboxCodegenExecutor` + `SubprocessPythonExecutor`; frozen hand-authored subset. DEBT-035 (TS runtime). Gates: test_codegen_pass1 8/8 · suite 1241 passed.
- [x] **8.3.2 — Multi-file benchmark — frozen corpus + BenchmarkOracle.** `tests/benchmark/corpus/v1/` (3 modules, 3 problems); `BenchmarkOracle` with Resolve@k; `asyncio.Event` on `LazyIndexer`; AST pre-flight safety. DEBT-036. Gates: test_oracle_resolve_k 12/12 · suite 1253 passed.
- [x] **8.3.3 — Ablation harness G1–G4 + G4-force-cloud.** `strategies.py`; G2 vector-only via `mock.patch`; G4_FORCE_CLOUD override of `derive_routing_decision`; `_graph_task_runner`; snapshot-then-clear drain. DEBT-037. Gates: test_ablation_verdicts 8/8 · suite 1261 passed.
- [x] **8.3.4 — Routing study H₂.** TCI-bucket × tokens × Resolve@3 cross-tabulation; anchored bucketing; strict pairing; `_prepare_run` refactor. Gates: test_routing_study 9/9 · suite clean.
- [x] **8.3.5 — Report generator.** `BenchmarkReport`; Wilson CI; `REPORT_SCHEMA` Draft-07; `write_report` atomic (`NamedTemporaryFile` + `os.replace`); 7 zero-trust findings closed. Gates: test_report 13/13 · test_reproducibility 3/3 · benchmark 60/60 · suite 1286 passed.
- [x] **8.3.6 — DoD-check — reproducibility.** `test_reproducibility.py` (3 tests): pinned SHA, deterministic JSON serialization, `seed=42`/`temp=0` contract declared.

---

### Division 8.4 — MCP & Model-Provider Ecosystem Hardening ✅

> Harden against fail-open security; curated registry; HITL live on real MCP tools. Symmetric permission model — no permission-engine fork.

- [x] **8.4.0 — Blueprint (ADR-757).** Covered by `docs/PHASE_8_BENCHMARK_MCP_BLUEPRINT.md`.
- [x] **8.4.1 — `classify_tool_privilege()` — security fix (closes DEBT-026).** Fail-closed precedence: curated catalog > verb heuristic > DANGEROUS. Gates: mypy 0/264 · pytest 1063 passed.
- [x] **8.4.2 — Curated MCP registry.** `core/mcp_registry.py` — `RegulatedServer` frozen dataclass; 4 servers; `tool_tiers` map. Gates: mypy 0/270 · pytest 1095 passed.
- [x] **8.4.3 — Import/export `.ailienant/config.json`.** Portable format (no secrets, `key_ref` only); `_redact_uri_credentials`; HTTP 422 on `McpConfigError`; allowlist guard on import. DEBT-031 logged. Gates: mypy 0/272 · pytest 1103 passed.
- [x] **8.4.4 — Auto-connect MCP servers + dispatch guard wiring (closes DEBT-027).** Multi-session `ClientSession` registry; bootstrap idempotent; `evaluate_action` dispatch-guard via injected kwargs; `autoconnect_enabled_mcp_servers` in lifespan. Gates: mypy 0/273 · pytest 1116 passed.
- [x] **8.4.5 — Skills execution wiring (closes DEBT-028 skills half).** Dual-mode resolver (cosine ≥0.45 Mode-1, explicit Mode-2); `build_skill_directive_block` boundary-sandboxed; schema migration. DEBT-032 logged. Gates: mypy 0 · npm compile 0 · pytest green.
- [x] **8.4.6 — Browse Registry UX (closes DEBT-031).** `mcp_secrets.py` atomic 0600 writes; `serialize_registry(installed_names)`; `_build_stdio_params` with `shutil.which` (Windows `npx.cmd`); close-first on re-install; tier-badge cards. DEBT-033 logged. Gates: pytest 59 focused + suite green · mypy 0/280 · tsc 0 · eslint 0.
- [x] **8.4.7 — DoD-check: HITL live on real MCP tool (closes DEBT-029).** ContextVar ambient injection (`_task_session_id`/`_task_session_mode`); trust-once valve (`_session_trust`); lazy `vfs_manager` channel default; `MCP_TOOL_CALL` FE binding. Gates: test_mcp_dispatch_guard 15/15 · mypy 0/280 · tsc 0 · eslint 0.
- [x] **8.4.8 — BYOM Provider Registry & Intuitive Config (closes DEBT-030).** `core/config/provider_registry.py` SSoT; 6 new providers (Gemini, DeepSeek, Mistral, Qwen, Moonshot, Zhipu); native routing avoids `_ensure_v1` trap. Gates: mypy 0/266 · pyright 0 · npm compile 0.
- [x] **8.4.9 — BYOM Model Availability, Preset UX & Model Health Check.** Model cache persisted per endpoint; `POST /test` with `endpoint_id`; OpenRouter native routing; `Model Browser` modal; `POST /ping` health check. Gates: mypy 0/267 · pyright 0 · npm compile 0.

---

### Division 8.5 — External Capability Gateway — [ADR-759] 🟡

> External agents (Claude Code, Codex) execute AILIENANT safely. MCP multi-tool stdio server adapting `/api/v1/task/submit` + WS + token. Symmetric permission model — no fork.

- [x] **8.5.0 — Blueprint (ADR-759, rescoped).** D1-D8 decisions locked: loopback EXECUTE, in-process READ_ONLY, host-discovery via `run.json`, durable ledger, conservative posture, deny-report HITL-degrade (D5), poll-pair JSON-RPC (D6), semver (D7), symmetric perms (D8). Gates: docs-only.
- [x] **8.5.1 — Gateway framework.** `gateway/` package (stdio MCP server); `catalog.py` SSoT (7 capabilities + schemas); `core/config/host_discovery.py` (`write_run_state` 0600, `probe_host_alive` async TCP, `resolve_host_or_error`); lifespan hook in `main.py`. Gates: mypy 0/286 · pytest 1192 · test_gateway_framework 15/15.
- [x] **8.5.2 — Tier governance.** `gateway/ledger.py` durable JSON per-caller token-bucket + budget (dedicated `.lock` filelock, clock-skew hardened, fail-closed); `gateway/governance.py` (`authorize_invocation`, `resolve_internal_task_mode` anti-escalation, `register_gateway_privileges`). Gates: mypy 0/289 · pytest 1209 · test_gateway_governance 17/17.
- [x] **8.5.3 — HITL-degrade deny-report.** Structured deny envelope (`status/reason/capability/tier/would_have_required/message`); `_denied()` helper; structurally no `await` (never hangs); `asyncio.wait_for(timeout=2.0)` safety test. Gates: mypy 0/290 · test_gateway_hitl_degrade 3/3.
- [x] **8.5.4 — Capability catalog v1.** `gateway/handlers.py` — in-process READ_ONLY (`query_memory`/`get_dependents`/`get_workspace_graph`) + loopback EXECUTE (`run_task` with `INTERNAL_TASK_MODE`=DEFAULT anti-escalation); `check_task_status` poll companion; race submit→register closed; `CAPABILITY_HANDLERS` dict DI. Gates: mypy 0/292 · pyright 0 · test_gateway_catalog_v1 14/14 · gateway 49/49.
- [x] **8.5.5 — Eval surface (run_benchmark + get_report).** `core/benchmark_service.py` (LFI-hardened `_resolve_artifact`, single-flight `_inflight`, durable artifact-file completion signal, pay-upfront refund); 2 loopback host endpoints. 8 zero-trust findings closed. DEBT-038/039. Gates: mypy 0/317 · pyright 0 · test_gateway_eval_surface 17/17 · suite 1303 passed.
- [x] **8.5.6 — Versioning + auth ergonomics + integration docs.** `PROTOCOL_VERSION` 1.0.0 single-sourced in `catalog.py` + advertised per-tool in `list_tools()._meta` (with `schema_version`/`deprecated`; null sunset keys omitted); `Capability` deprecation mechanism (N=2-minor support window); safe masked boot line (never logs the token); `docs/GATEWAY_INTEGRATION.md` (launch/auth/ceilings/catalog/envelopes/versioning). **DoD:** surface declares its version; integration docs exist. Gates: mypy 0/318 · pyright 0 · suite 1308 passed.
- [x] **8.5.7 — DoD-check** — an external caller lists the catalog, runs a READ_ONLY verb, and is denied+reported on a DANGEROUS verb without hanging. ✅ `test_gateway_dod.py` (3 rows): catalog discovery + `_meta` version, READ_ONLY `ok`, DANGEROUS → `requires_human_approval` deny-report under `asyncio.wait_for` (proves non-hang). **División 8.5 CERRADA** (el gate de fase sigue siendo 8.6).

---

### Division 8.6 — Phase 8 Checkpoint Gate — [ADR-760] ⬜

> Single phase gate (sibling-file convention): re-certifies resilience + precision (H₁/H₂ harness runs) + MCP privilege fail-closed + external HITL-degrade. **DoD:** `pytest` green · `mypy .` 0 · gate green · `npm run compile` 0.

---

### Division 8.7 — Analyst Tri-Brain & Model Selector ✅

> Converts Natt from a single-source engine (GraphRAG only) to a complete knowledge tutor with three graduated context sources and a configurable response model selector.

- [x] **8.7.0 — Brain 1: GraphRAG (central, no code change).** `SemanticMemoryManager.search_snippets()` — existing source, model-independent, full budget preserved.
- [x] **8.7.1 — Brain 2: Workspace README (`core/readme_digest.py`).** Verbatim ≤5 KB; semantic digest with 7 s debounce; SHA-256 cache; reactive invalidation via `client_ide_telemetry`.
- [x] **8.7.2 — Brain 3: AILIENANT docs-RAG (`core/memory/docs_index.py`).** Corpus: `HowItWorks.md`/`HowToUseIt.md`/`README.md`; reserved namespace (`sha256("__ailienant_product_docs__")`); idempotence triple: `asyncio.Lock` + `filelock.FileLock` + double-check inside lock.
- [x] **8.7.3 — ContextBudgetManager + per-tier budgets.** `ContextChunk` + `ContextBudgetManager.pack()` in `agents/analyst_context.py`; 5-tier retention ladder; 60% hard-cap; soft-cap + backfill; per-tier token budgets.
- [x] **8.7.4 — Directional preset fallback (`core/config/model_resolver.py`).** `_directional_order(tier)`: `small` scales up → cloud; `cloud` descends → small; never crashes on sparse preset.
- [x] **8.7.5 — Analyst model selector in Natt HUD (frontend).** `analystTier` in `workspaceStore.ts`; `AnalystModelPicker` in `NattCanvas.tsx`; `model_tier` in `NATT_MESSAGE` payload.
- [x] **8.7.6 — Division 8.7 Checkpoint Gate.** `tests/test_analyst_brains.py` — 14 tests. Gates: pytest 1117 passed · mypy 0/276 · npm compile 0 · npm lint 0.

---

### Division 8.8 — Tool Parity Matrix (Agent Arsenal) — [ADR-761] ⬜

> Six agents are unevenly equipped: 14 schemas registered today; Researcher and Analyst are the most under-equipped relative to their missions; Planner operates with zero formal tools. This division builds the **56-tool parity matrix** — a `tool × agent` assignment with precise `allowed_roles`. The unlock is **Wave 0** (`ToolSearchTool` over the existing `ToolRAGStore`) so 56 tools never blow the prompt budget. No changes to `AIlienantGraphState`/`MissionSpecification` (SCHEMA_EVOLUTION.MD). Permission engine (`classify_tool_privilege`/`evaluate_action`) reused — forking PROHIBITED.

> **Honest inventory count:** (A) 13 real registered tools requiring `allowed_roles` wire-in only. (B) 4 with backing functions requiring schema formalization (`GetDependentsTool`, `GraphRAGQueryTool`, `ASTValidateTool`, `FileReadTool` paginated). (C) ~29 genuinely net-new. **Substrate correction:** `ContextMeter*` tools from source analysis assume a `ContextMeter` class with `css_total`/`TCI` that does NOT exist; real substrate is `TokenLedger` (`core/token_ledger.py`) — re-channeled as `TokenLedgerReadTool`/`BudgetEstimatorTool`.

- [x] **8.8.0 — Wave 0: Infra gate (`ToolSearchTool` / `DeferredToolLoader`) — GATE FOR THE ENTIRE DIVISION.**
  - Relevance-based tool retrieval over the existing `ToolRAGStore`, auto-triggered at ~10% of context budget; below the threshold, tools load eagerly; above, the agent retrieves by query. Nothing else in 8.8 ships until this is green.
  - **DoD:** with 56 schemas registered, a cold prompt respects `TOOL_RAG_MIN_REDUCTION=0.70` and the correct tool is retrievable by query.

- [x] **8.8.1 — Wave 1: Researcher Arsenal (READ_ONLY).** *(≈5 net-new · 5 wire-in)*
  - Wire-in `researcher` in `allowed_roles`: `inspect_ast_node`, `get_symbol_references`, `trace_data_flow`, `document_parser`, `FileReadTool` paginated.
  - Net-new: `GlobTool`/`GrepTool` over VFS RAM-first (no direct FS), `WorkspaceStructureTool` (relevance-filtered tree), `GraphRAGQueryTool` (formalizes `deep_parse`), `GetDependentsTool` (formalizes `get_dependents`).
  - **DoD:** Researcher retrieves and executes each read tool under its role; none writes.

- [x] **8.8.2 — Wave 2: Analyst Quality Lens (READ_ONLY).** *(≈6 net-new · 4 wire-in)*
  - Wire-in `analyst`: `inspect_ast_node`, `trace_data_flow`, `get_symbol_references`, `web_fetch`.
  - Net-new: `RunLinterTool` (wraps `tools/validation/lsp_filter.py`), `ComplexityAnalysisTool` (McCabe CC, nesting depth), `DependencyAuditTool` (manifests vs CVE via MCP), `CodeDiffTool` (dirty-buffer vs VFS), `WebSearchTool` (MCP brave-search), `TokenLedgerReadTool` (re-channeled over `TokenLedger`, not `ContextMeter`).
  - **DoD:** Analyst explains cyclomatic complexity, traces data flow, and compares versions — all without touching code.

- [x] **8.8.3 — Wave 3: Orchestrator Introspection (deterministic).** *(2 net-new · 3 wire-in)*
  - Net-new: `GetWBSStatusTool` (reads `mission_spec.tasks`), `EmitHITLRequestTool` (audited `HITL_APPROVAL_REQUIRED` emission).
  - Wire-in Orchestrator: `ask_user_question`, `toggle_plan_mode`, `read_token_ledger`.
  - Amendment (§4 pivot): `GetTokenLedgerTool` dropped — it duplicated 8.8.2's `read_token_ledger` (`TokenLedgerReadTool`, which already formalizes `token_ledger.snapshot()`). Resolved by additively wiring `orchestrator` into `read_token_ledger.allowed_roles`. Net-new 3→2, wire-in 2→3; surface unchanged (5 tools).
  - **DoD:** Orchestrator operations are audited tools (not direct state access), safely invocable by the 8.5 external gateway.

- [x] **8.8.4 — Wave 3b: Planner Pre-Commit Verification (READ_ONLY — the moat).** *(≈2 net-new · 3 wire-in)*
  - Wire-in `planner`: `WorkspaceStructureTool`, `GetDependentsTool`, `InspectASTNodeTool`.
  - Net-new: `ValidateWBSDependenciesTool` (detects circular dependencies between `WBSStep` + steps referencing out-of-scope files — AILIENANT using its own GraphRAG to verify its own plan), `BudgetEstimatorTool` (plan draft token cost vs `TokenLedger` budget — shift-left of `oom_fallback`).
  - **DoD:** a `MissionSpecification` draft with a circular dependency is rejected pre-commit; the Planner's first attempt self-corrects instead of burning `MAX_PLANNER_RETRIES`.

- [x] **8.8.5 — Wave 4: Role-Specific Coder Tools (leverages `ROLE_REGISTRY`).** *(≈10 net-new · 5 formalize)*
  - Formalize: `atomic_code_patch`, `batch_semantic_edit`, `file_write`, `sandbox_bash`, `ASTValidateTool` (wraps `validate_ast`).
  - Net-new by exclusive role: `RunTestsTool` (qa_tester), `GitStageTool`/`GitCommitTool`/`GitDiffTool` (vcs_manager, conventional commits), `DocstringGeneratorTool` (doc_manager), `LinterAutoFixTool` (secops/qa_tester, `--fix` with diff before apply), `DependencyInstallTool` (devops_infra, EXECUTE in sandbox), `EnvFileGuardTool` (devops_infra, intercepts `.env` mutations → HITL), `DataPipelineRunTool` (data_ml_engineer), `SecurityAuditTool` (secops, OWASP on the diff).
  - `allowed_roles` of each tool mirrors the `allowed_tools` of the role in `agents/roles.py`.
  - **DoD:** a role cannot invoke a tool outside its set; each exclusive tool responds only to its role(s).

- [x] **8.8.6 — Wave 5: Gateway/Benchmark — DEPENDS ON Division 8.5.** *(≈6 net-new · 2 extend)*
  - **Resolved conflict (Pivot):** 8.5 already owns `run_task` (8.5.4), `run_benchmark`/`get_report` (8.5.5), and `list_tools()` (8.5.1) as **external gateway verbs**. This Wave **formalizes-as-internal-tool** the same substrate — one benchmark runner, one catalog, one task cycle — **never a fork**. If 8.5 is still active when this Wave starts, this Wave builds the shared substrate and 8.5 consumes it.
  - Net-new: `RunBenchmarkTool`, `GetBenchmarkReportTool`, `ListCapabilitiesTool`, `SkillInvokeTool` (wraps the 8.4.5 skill resolver). Task V2: `task_create`/`task_get` extend; `TaskListTool`/`TaskStopTool` net-new.
  - **DoD:** benchmark/catalog tools call the same substrate functions as the 8.5 verbs (no duplicated runner).

- [x] **8.8.7 — Wave 6: Universal Tools (all roles).** *(≈1 net-new · 1 cross-listed)*
  - `ToolSearchTool` (cross-listed from Wave 0, available to all roles) + `TodoWriteTool` net-new over `AIlienantGraphState`.
  - Amendment (§4 pivot): `agent_todos: Annotated[List[Dict[str, Any]], _merge_todos]` added additively to `AIlienantGraphState`; reducer tests `right is not None` (not truthiness) so an explicit `[]` clears the panel — the anti-immortal-TODO invariant. Cross-reference `docs/SCHEMA_EVOLUTION.MD §14`.
  - **DoD:** any agent retrieves tools by query and writes its TODO list to state.

- [x] **8.8.8 — Division 8.8 Checkpoint Gate.**
  - `tests/test_phase8_8_tool_parity_gate.py` (sibling-file convention): every registered schema resolves through `ToolRAGStore`; `allowed_roles` enforced (a role cannot invoke an out-of-set tool); Wave 0 reduction ≥70%; ISO row asserts that `agents/roles.py` role contracts do not degrade.
  - **DoD:** `pytest` green · `mypy .` 0 · `ruff` clean · `npm run compile` 0 (only if any FE surface exposes a tool).

---

## Division 8.9 — Portable Workspace Home (`.ailienant/` Provisioning) ✅

> AILIENANT manages its own persistence home like Claude Code manages `~/.claude/`. Relocate global runtime stores from the CWD to a stable `~/.ailienant/` home; partition only the GraphRAG semantic store per-project. Add a freeform `AILIENANT.md` project-instructions channel, renderable WBS plans, and zero-friction workspace provisioning. Hybrid storage (Option C): catalog SQLite / MCTS / gateway ledger / global LanceDB tables stay global (already isolated by `project_id`/`workspace_hash` columns); only `workspace_embeddings` goes per-project.

- [x] **8.9.1 — Storage-Path Foundation.**
  Relocate global-store defaults in `shared/config.py` to `~/.ailienant/` (`catalog.sqlite`, `lancedb/`, `mcts.sqlite`); ensure home exists at import. New `core/storage_paths.py`: canonical `project_id_for`, `bind_project(workspace_root)`, `graphrag_lancedb_path()` (per-project `~/.ailienant/projects/<id>/lancedb/` + `_unbound` fallback). Bind on `client_workspace_init` (`main.py:1154`). **DoD:** globals resolve under home; bind creates the per-project lancedb dir; unbound fallback safe; env overrides win.
- [x] **8.9.2 — Wire GraphRAG + migrate CWD stores.**
  `semantic_memory.SemanticMemoryManager` defaults its lancedb path to `storage_paths.graphrag_lancedb_path()`; `docs_index`/`trajectory_memory` stay global. `checkpointing`/`janitor` use `MCTS_DB_PATH`; `gateway/handlers` + `janitor` import the shared `project_id_for`. One-time best-effort migration of legacy CWD stores into the home. **DoD:** CWD clean; GraphRAG under per-project home; existing data migrated or re-indexed.
- [x] **8.9.3 — `AILIENANT.md` Project-Instructions Injection.**
  Read `<ws>/.ailienant/AILIENANT.md` (fallback `<ws>/AILIENANT.md`) via `make_safe_reader`, token-capped (digest if large), inject alongside the rules block in the prompt pipeline. **DoD:** present → injected (capped); absent → prompt unchanged, zero tokens.
- [x] **8.9.4 — Renderable `plans/` WBS Export.**
  `dump_plan_to_markdown(spec, workspace_root, task_id)` writes a navigable `<ws>/.ailienant/plans/<task_id>.md` after planning; atomic, non-fatal. **DoD:** a planning turn leaves a readable plan file.
- [x] **8.9.5 — Extension Auto-Provision + `.gitignore`.**
  On first workspace open, idempotently create `<ws>/.ailienant/` + starter `AILIENANT.md` and append a marked `.gitignore` block (ignore runtime/cache, keep `AILIENANT.md` trackable) via `vscode.workspace.fs`. **DoD:** once-only provisioning; re-activation is a no-op.
- [x] **8.9.6 — Division 8.9 Checkpoint Gate.**
  `tests/test_phase8_9_checkpoint_gate.py` (sibling convention): global defaults under home; `bind_project`/`graphrag_lancedb_path` per-project + unbound fallback; env override precedence; `project_id_for` golden vector matches the extension contract; `AILIENANT.md` present→injected/absent→no-op; planning writes `plans/<task_id>.md`; cross-platform path safety. **DoD:** `pytest` green · `mypy .` 0 · `npm run compile` 0 · `npm run lint` 0.

---

## Division 8.10 — Aggressive Debt Reduction & Path to 8.2 + 8.6 ⬜

> Closes the full open DEBT backlog aggressively before enterprise initiatives begin. Ordered so that 8.2 and 8.6 complete cleanly mid-phase. Eight sub-phases.

- [x] **8.10.0 — Emergency FE Regressions (unblock daily use first)**
  - DEBT-055 (HIGH): Natt/Analyst pane scroll — `.ws-natt-body` is a `1fr` grid track that lacked `min-height: 0`, so it grew past the viewport instead of scrolling. Files: `ailienant-extension/src/workspace/workspace.css`. (Main chat `.ws-messages` was already correct.)
  - DEBT-056 (MEDIUM): Composer auto-resize — `scrollHeight`-driven `useLayoutEffect` hook (`min-height: 2.5rem; max-height: 12rem`) for the main composer (PromptBar, also the Socratic planner input) and the Analyst input (NattPromptBar). Files: `src/workspace/hooks/useAutoResizeTextarea.ts`, `PromptBar.tsx`, `NattPromptBar.tsx`, `workspace.css`.
  - DEBT-060 (HIGH): Diff-authorize card duplicated on tab switch with no diff — `server_plan_document` re-injected its summary bubble on every panel reveal because the host re-post guard matched only the `"Drafted a plan"` phrasing, not the Ask/Auto `"Proposed N file change(s)…"` pointer. Made the webview handler idempotent by summary content + content-based host guard. Files: `Workspace.tsx`, `providers/workspace_panel.ts`.
  - DEBT-061 (MEDIUM): Pipeline execution trace collapsed to a 1px box line — redesigned `.ws-thinking` from a bordered widget into an inline, borderless muted trace that reads as part of the conversation. Files: `workspace.css`, `components/PipelineProgress.tsx`.
  - DEBT-062 (MEDIUM): Telemetry HUD follow-up — fixed the height regression DEBT-056 introduced (composer and telemetry card share `--hud-rest-height`, equal at rest, composer-only growth); merged the OCC ring and the context meter into one split donut (left OCC, right lavender deepening with occupancy); resolved the per-model context window via litellm metadata (no flat 200k); backtick-wrapped apply-result paths so `*_telemetry.log` stops being italicized. Files: `TelemetryHUD.tsx`, `workspace.css`, `theme.css`, `providers/workspace_panel.ts`, `core/task_service.py`, `api/sessions.py`.
  - **DoD:** `npm run compile` 0 (tsc/eslint 0 errors) · `mypy .` 0/340 · `pytest` 1494 passed · manual smoke: composer & HUD equal at rest, long prompt grows composer only; split ring shows OCC + lavender context; stale-file notice renders the path intact.
  - Deferred to `docs/TECH_DEBT_BACKLOG.md`: DEBT-063 (plan executes out of WBS order), DEBT-064 (agent organizes its own `.ailienant/` runtime files → OCC stale-apply), DEBT-065 (Auto-mode summary says "authorize" though it auto-applies).

- [x] **8.10.1 — Deployment readiness & critical path fixes**
  - DEBT-034 (HIGH): normalize `project_id` hash — apply `os.path.normcase(os.path.normpath(...))` in `core/storage_paths.py:project_id_for` and the byte-for-byte equivalent in `extension/src/core/PathResolver.ts:computeProjectId` (Node `path` module + trailing-sep strip except root); triggers one-time lazy re-index on next workspace open.
  - DEBT-038 (MEDIUM): relocate benchmark harness from `tests/benchmark/` to `core/benchmark/`; update all `tests.benchmark.*` imports in gate files.
  - DEBT-040 (MEDIUM · Locked): close stale `tool_search` role resolution via Explicit State Augmentation — the per-step role rides in the `Send` payload (router sets `active_role = step.target_role`); `_resolve_active_role` is config-first; the ambient `_task_active_role` ContextVar is removed (no per-step staleness, no cross-WS leak).
  - **DoD:** `mypy .` 0 · `pytest` green · `npm run compile` 0.

- [x] **8.10.2 — Integration wiring sprint**
  - DEBT-043 (MEDIUM): bind orchestrator tools into the live graph node — create `make_get_wbs_status_tool` / `make_emit_hitl_request_tool` factories in `tools/agent_tools.py`; wire into `agents/orchestrator.py` tool set.
  - DEBT-046 (MEDIUM): thread `session_id`/`session_permission_mode` into coder tool factories; EXECUTE-tier coder tools surface the HITL approval card (mirrors `sandbox_bash`).
  - DEBT-042 (MEDIUM): wire `WebSearchTool._search_fn` + `DependencyAuditTool._search_fn` to the brave-search MCP session handle via `bootstrap_mcp_session` lifecycle propagation.
  - DEBT-028 hooks (MEDIUM): execute stored `pre_patch`/`post_patch` hooks around task mutations in `core/task_service.py`.
  - **DoD:** `mypy .` 0 · `pytest` green.

- [x] **8.10.3 — Execute Division 8.2: Resilience & Observability**
  Drives all five pending 8.2 sub-tasks:
  - 8.2.1: E2E tests — full SSoT stack over real HTTP/WS returning an applied patch.
  - 8.2.2: Fast Track + LangSmith observability (no new log sink; builds on 7.13.3).
  - 8.2.3: Hardware fallbacks — VRAM threshold as config; Cloud reroute on insufficient VRAM. 8.2.3.1: Graph Weight Calculator predicts State size before prompt execution.
  - 8.2.4: Hardware Stress Simulator — chaos script triggering real `hardware_profiler` fallbacks observable in telemetry.
  - 8.2.5: DoD-check — resilience smoke green.
  - **DoD:** `mypy .` 0 · `pytest` green.

- [ ] **8.10.4 — Execute Division 8.6: Phase 8 Checkpoint Gate**
  Sibling gate re-certifying H₁/H₂ harness, MCP privilege fail-closed, HITL-degrade, and resilience. **DoD:** `pytest` green · `mypy .` 0 · gate green · `npm run compile` 0.

- [ ] **8.10.5 — HIGH-tier architectural debts**
  - DEBT-036 (HIGH): route `BenchmarkOracle` code execution through the sandbox adapter (Docker tier) for corpus isolation; replace `SubprocessPythonExecutor` direct host execution.
  - DEBT-013 (HIGH): add a gateway streaming branch that keeps `response_format` for providers supporting streaming structured output (OpenAI style); fall back to ADR-742 adaptive sanitizer only where unsupported.
  - **DoD:** `mypy .` 0 · `pytest` green.

- [ ] **8.10.6 — MEDIUM performance & correctness debts**
  - DEBT-024 (MEDIUM): compute unified diff server-side in `task_service.py`; transport O(Δ) patch; client reconstructs both sides via existing `applyPatch`. Shared `PatchedFileDiff`/`DiffBlockShape` contract updated.
  - DEBT-035 (MEDIUM): TypeScript sandbox execution — extend Docker image with `node:20-slim`; `SandboxCodegenExecutor` routes `Language.TYPESCRIPT` to Node tier.
  - DEBT-041 (MEDIUM): GrepTool inverted-content-index — async index at index time; `GrepTool._scan` becomes O(matches) not O(files); ReDoS-bounded regex evaluator.
  - DEBT-048 + DEBT-050 (MEDIUM): `RunBenchmarkTool` registers with `task_service.register_active_task` and charges `ledger.consume_budget()`.
  - DEBT-053 (LOW→pre-release): `TaskStopTool` SIGTERM → wait 5 s → SIGKILL escalation.
  - **DoD:** `mypy .` 0 · `pytest` green.

- [ ] **8.10.7 — Pre-launch gap audit (docs-only)**
  Update `DEVELOPERS.md` honest list to reflect completions (56-tool catalog, MCP wiring, orchestrator/researcher nodes), remaining deferrals (Wasm default, full MCTS, autonomous dreaming, auth), and planned implementations (prompt caching → Phase 13.1). **DoD:** honest list accurate; no code changes.

---

### Division 8.11 — 7-Mode Permission System ⬜

> Extend the current 3-mode `session_mode` (DEFAULT / PLAN / READ_ONLY) to a 7-mode execution permission matrix, modeled on Claude Code's Allow/Ask/Deny granularity adapted to AILIENANT's privilege-tier model. ADR to be assigned.

| Mode | Name | Behavior |
|------|------|----------|
| 1 | FULL_AUTO | No HITL for any tier; all tools execute immediately |
| 2 | STANDARD | HITL for DANGEROUS only (current DEFAULT) |
| 3 | CAUTIOUS | HITL for EXECUTE + DANGEROUS; READ_ONLY auto-admitted |
| 4 | ASK_EXECUTE | Ask before any non-READ_ONLY tool; deny DANGEROUS |
| 5 | ASK_ALL | Ask before every tool call (including READ_ONLY) |
| 6 | READ_ONLY | Only READ_ONLY tier admitted; EXECUTE/DANGEROUS blocked |
| 7 | PLAN_ONLY | Planning only; no execution (current PLAN) |

- [ ] **8.11.1 — ADR + `session_mode` enum extension.**
  Additive extension: `session_mode` gains 4 new enum values; DEFAULT maps to STANDARD (2); PLAN maps to PLAN_ONLY (7); READ_ONLY maps to READ_ONLY (6). No existing checkpoint breaks. SCHEMA_EVOLUTION.MD versioned entry. Files: `brain/state.py`, `docs/SCHEMA_EVOLUTION.MD`.
- [ ] **8.11.2 — `evaluate_action` resolver rewrite.**
  `gateway/governance.py:evaluate_action` maps `(mode, tier) → ALLOW | ASK | DENY` per the 7×3 matrix. Files: `gateway/governance.py`, `core/task_service.py` (ambient mode propagation).
- [ ] **8.11.3 — Frontend mode switcher (7 modes).**
  Extend `ModeSwitcher.tsx` to surface all 7 modes with descriptions. Persist selection in `workspaceStore`. Files: `ailienant-extension/src/webview/components/ModeSwitcher.tsx`.
- [ ] **8.11.4 — Division 8.11 Checkpoint Gate.**
  `tests/test_permission_modes.py`: 7 × 3 tier matrix asserted (ALLOW/ASK/DENY per mode per tier). **DoD:** `mypy .` 0 · `pytest` green · `npm run compile` 0.

---

### Division 8.12 — Five-Layer Context Compression Pipeline ⬜

> Formalize context management into a typed 5-layer pipeline preventing silent mid-task context loss. Inspired by Claude Code's 6-layer context window architecture. Emits `STATE_COMPACTED` WS events consumed by Phase 11.7 chat compaction. ADR to be assigned.

| Layer | Name | Content | Persistence | Budget |
|---|---|---|---|---|
| 1 | Foundation | System prompt, role identity, AILIENANT.md, tool schemas | Static startup, never evicted | 20% |
| 2 | Project | README digest, GraphRAG project summary, rules | Session-persistent, reloaded on workspace change | 15% |
| 3 | Memory | StateSummarizer output, checkpoint deltas, dreaming digest | Rolling, oldest evicted on overflow | 20% |
| 4 | Conversation | Recent turns, WBS status, HITL decisions | FIFO window, explicit eviction | 30% |
| 5 | Execution | Tool results, diffs, benchmark reports (on-demand) | Volatile per-turn, not persisted | 15% |

- [ ] **8.12.1 — `brain/context_pipeline.py` + assembler.**
  `ContextLayer` ABC + `ContextPipeline` assembler; Layer 4 FIFO eviction emits `STATE_COMPACTED` event over WS when entries are dropped. Existing `brain/summarizer.py` becomes Layer 3's compression backend (no rename). Existing `agents/analyst_context.py:ContextBudgetManager` becomes Layer 4's budget.
- [ ] **8.12.2 — Agent integration.**
  `agents/planner.py` / `agents/coder.py` consume `ContextPipeline` instead of ad-hoc injection. Guarantee: a task exceeding 100K tokens never silently truncates Layers 1–3.
- [ ] **8.12.3 — WS `STATE_COMPACTED` event contract.**
  `api/websocket_manager.py`: new event type `{"type": "STATE_COMPACTED", "summary": "...", "turns_compressed": N}`; consumed by Phase 11.7 `SessionSummaryCard` frontend (not yet shipped — wired in Phase 11).
- [ ] **8.12.4 — Division 8.12 Checkpoint Gate.**
  `tests/test_context_pipeline.py`: Layer 1–3 are never evicted; Layer 4 FIFO eviction fires when the budget is exceeded; `STATE_COMPACTED` event is emitted. **DoD:** `mypy .` 0 · `pytest` green.

---

## PHASE 9 — Native Thinking (Real-Time Reasoning Stream) ✅

> Real-time native model reasoning exposed in a collapsible Thought Box (Claude Extended Thinking / open reasoning models via `reasoning_content`). Strictly transport/orchestration/UI layers — `agents/` untouched.

- [x] **9.1. Gateway bifurcation (transport).** `tools/stream_delta.py` + `astream_byom_thinking`; `_supports_native_thinking` capability gate (Anthropic / DeepSeek-R1 / QwQ).
- [x] **9.2. Dedicated WS contract + payload.** `ThinkingChunkPayload` + `ServerThinkingChunkEvent`; `TaskPayload.enable_native_thinking`; `broadcast_thinking_chunk`.
- [x] **9.3. Orchestration demux.** `_stream_with_thinking` routes reasoning → Thought Box (60 ms) and response → bubble (40 ms); reasoning exempt from 15% NarrationGate.
- [x] **9.4. UI + state (React/Zustand).** Native Thinking toggle persisted; `ThoughtBox.tsx` (collapsible accordion + live timing); `thinkingReducer.ts` pure reducers; reasoning excluded from `PERSIST_TRANSCRIPT`.
- [x] **9.5. Phase 9 Checkpoint Gate.** `tests/test_native_thinking.py` (7) + `src/test/nativeThinking.test.ts` (7). Gates: pytest 665 passed · mypy clean (202 files) · npm compile 0 · Mocha 50 passing.

---

## PHASE 10 — Professional Documentation & Public Presence ✅

> Enterprise-quality public presence: multi-language README, accessible guides, standardized contributing guide, deep technical developer README. Documentation + licensing + manifest only — no code changes.

- [x] **10.0. Scope note.** Docs + licensing + manifest; zero code mutations. Gamification discarded; MCP/Skills/hardware ecosystem referenced to 8.4/8.2.
- [x] **10.1. Licensing foundation.** `LICENSE` (AGPL-3.0 verbatim) + `LICENSING.md` (dual license: Community AGPL vs Commercial/Enterprise) + `CLA.md`.
- [x] **10.2. Public README (English) + branding.** `README.md` enterprise landing; `assets/` with `logo.svg` + `icon-color.svg`; previous technical README migrated to `DEVELOPERS.md`.
- [x] **10.3. Translation set (7 languages).** `README.{es,fr,zh,hi,ru,it}.md` + `README.md` (en); cross-language navigation bar.
- [x] **10.4. User guides.** `HowToUseIt.md` (step-by-step manual) + `HowItWorks.md` (architecture explainer with diagrams).
- [x] **10.5. Developer docs + contribution.** `DEVELOPERS.md` (module map, critical path pseudocode, security model, Repository Layout, honest list of not-yet-implemented) + `CONTRIBUTING.md` (setup, Exit 0 gates, Conventional Commits, timeless/English comment policy, CLA gate).
- [x] **10.6. Phase 10 Checkpoint Gate.** DoD (docs-only) verified: LICENSE/LICENSING.md/CLA.md present and linked; 7 README variants present with correct cross-language bars; no broken relative links; Repository Layout in `DEVELOPERS.md`; diff is exclusively `.md` + `assets/` + `LICENSE`.

---

## PHASE 11 — Web Dashboard Enterprise Redesign ⬜

> Elevate the web dashboard from MVP (11 functional panels, basic CSS) to an enterprise-grade observability console with full project-context awareness and a flagship GraphRAG visualization. Depends on Division 8.12 `STATE_COMPACTED` event contract. Nine sub-phases.

- [ ] **11.0 — Design System & Navigation.**
  Enterprise component library (design tokens, spacing scale, typography, color system); sidebar nav with grouped panels (Monitoring / Configuration / Operations); keyboard shortcuts; collapsible sidebar; responsive layout. Foundation inherited by all redesigned panels.
- [ ] **11.1 — Project Context Disambiguation (critical — precedes all panel redesigns).**
  Active project selector (project name + path) pinned to the top bar; `project_id` propagated to all polling hooks (`HardwarePanel`, `TelemetryPanel`, `OverviewPanel`, `AuditPanel`, `RuntimePanel`, `RecoveryPanel`); global config panels (`BYOMPanel`, `ExtensionsPanel`, `RulesPanel`) show active project badge; backend dashboard endpoints gain `?project_id=` filter param. **DoD:** switching projects re-scopes all widget data.
- [ ] **11.2 — GraphRAG Knowledge Visualization (flagship — highest priority).**
  Full enterprise visualization for `MemoryManagement` panel demonstrating AILIENANT's cognitive depth.
  - *Force-directed graph (D3.js / Cytoscape.js):* node types with distinct shapes + colors (`file`=circle, `function/method`=diamond, `class`=hexagon, `module`=square, `external dep`=triangle); node sizes scaled by PPR score; **god nodes** (top-K centrality) highlighted with gold ring + star badge; **community clusters** (Louvain) as colored halos with click-to-filter; edge types (imports/calls/inherits) as distinct line styles with legend toggle; cross-community nodes get multi-color rings; click-to-inspect side panel (symbol, file, code snippet via VFS, PPR rank, community, degree, last indexed); graph search with matched-node pulse animation.
  - *Vector map layer (2D projection):* UMAP/t-SNE of LanceDB embeddings as density heatmap; each point = a doc chunk colored by cluster; hover shows chunk text preview, source file, and embedding distance; clicking a region zooms the graph layer to that cluster's files.
  - *Doc chunk browser (list layer):* paginated list of all indexed chunks (content preview, source file, vector ID, last-access timestamp — recency of RAG retrieval); sortable by recency / PPR score / embedding norm; Purge button with HITL confirmation for stale eviction.
- [ ] **11.3 — Real-time Monitoring Panels Redesign.**
  `TelemetryPanel`: live sparkline charts (token cost, routing-decision pie, latency P50/P95) — all scoped to active project. `HardwarePanel`: animated radial VRAM/RAM gauges with configurable alarm thresholds + 60-second VRAM timeline. `OverviewPanel`: project-scoped KPI cards (session cost, tasks today, MCP servers connected, HITL approvals pending). `RuntimePanel`: Docker lifecycle Gantt timeline, adapter tier switcher, live container log stream.
- [ ] **11.4 — BYOM & Extensions Polish.**
  BYOM: model browser with benchmark Pass@1 scores (from Division 8.3 reports), cost-per-token badges, quick-connect CTA, health-check status. Extensions: skill cards with usage stats, semantic search over the 56-tool catalog, installed vs available MCP servers with one-click install from the curated registry.
- [ ] **11.5 — Verbal Reasoning Fallback for Non-Native-Thinking Models (closes DEBT-057).**
  When native thinking is unavailable, `tools/llm_gateway.py` injects a reasoning scaffold into the system prompt (`<thinking>…</thinking>` prefix); the block is streamed to ThoughtBox via the existing `broadcast_thinking_chunk` path and stripped from the final answer. FE: Reasoning Mode toggle (`Native` / `Verbose` / `Compact`); ThoughtBox header shows `[Simulated]` vs `[Native]` tag. Files: `tools/llm_gateway.py`, `agents/planner.py`, `agents/coder.py`, `ThoughtBox.tsx`.
- [ ] **11.6 — Active Task Header / Prompt Preservation (closes DEBT-058).**
  Submitted prompt stays as a sticky card pinned above the message list while the AI responds; collapses to 1-line summary on `TASK_COMPLETE`; user-dismissible. New `ActiveTaskHeader.tsx` with animated "Working…" + elapsed-time indicator + Cancel affordance. `workspaceStore.ts`: `activeTaskPrompt` / `activeTaskId` state. No backend change; uses existing WS events. Files: `workspaceStore.ts`, new `ActiveTaskHeader.tsx`, `NattCanvas.tsx`.
- [ ] **11.7 — Chat Compaction for Long Sessions (closes DEBT-059).**
  When message list exceeds `MESSAGE_COMPACTION_THRESHOLD` (default 40) AND the backend emits a `STATE_COMPACTED` WS event (from Division 8.12 Layer 4 eviction), replace messages before the compaction point with a collapsible `SessionSummaryCard` (header: "N messages summarized", body: `StateSummarizer` output). Messages after the point remain fully rendered. No new backend endpoint — event payload carries the summary text. Files: `NattCanvas.tsx`, new `SessionSummaryCard.tsx`, `workspaceStore.ts`.
- [ ] **11.8 — Dashboard Checkpoint Gate.** `npm run compile` 0 · `npm run lint` 0 · Playwright smoke: all panels load; project context selector re-scopes data on switch; GraphRAG graph renders with ≥1 node + god-node badge; vector map heatmap visible; `ActiveTaskHeader` appears on submit and clears on completion; ThoughtBox shows `[Simulated]` tag for a non-thinking model.

---

## PHASE 12 — Human Evaluation Execution ⬜

> Run the Division 8.3 benchmark harness end-to-end with a curated corpus and human judges, producing exact accuracy percentages with statistical confidence bounds — the "moat proof" document backing every quality claim in the portfolio.

**Output metrics (exact percentages + Wilson CI):**
- **Pass@1**: % correct on first attempt. **Resolve@k** (k≤3): % correct within k retries.
- **Ablation delta**: G1 (full arch) vs G2 (vector-only) vs G3 (no RAG) vs G4 (no HITL) — exact ΔPass@1 per component.
- **Human judge scores**: correctness (0/1), code quality (1–5), intent alignment (1–5), HITL appropriateness (1–5) — averaged per problem per arm.
- **H₁/H₂ verdicts** with Wilson CI: e.g. "Pass@1 = 74% ± 6%; ΔG1-G2 = +18 pp (p<0.05)".
- Final artifact: `docs/HUMAN_EVAL_REPORT.md` — structured, versioned, public-ready.

- [ ] **12.1 — Problem Set Curation.** Freeze 30–50 real-world multi-file problems with known-correct solutions, difficulty tags (easy/medium/hard), Python + TS language split. Each problem attempted by all 4 arms under identical conditions.
- [ ] **12.2 — Blind Evaluation Protocol.** Judge assignment rubric (correctness, code quality, intent alignment, HITL appropriateness); arm identity hidden at scoring time; Cohen's κ inter-rater reliability check before final scoring.
- [ ] **12.3 — Data Collection & Report.** Run `run_benchmark` via gateway for G1–G4 arms; collect judge scores; pipe into `BenchmarkReport` with Wilson CI; compare H₁/H₂. Output: `docs/HUMAN_EVAL_REPORT.md`.
- [ ] **12.4 — Human Eval Gate.** Report artifact exists; Wilson CI columns populated; H₁/H₂ verdict rendered. **DoD:** `get_report` returns a valid, non-empty report.

---

## PHASE 13 — Pre-Launch Innovation Sprint ⬜

> The "Phase 9 spirit" — a focused innovation wave immediately before the final launch deploying the highest-ROI features not yet in the system.

- [ ] **13.1 — Provider-Native Prompt Caching (~90% input-token discount).**
  Structure LangGraph message assembly so the stable high-volume prefix (system prompt → tool/MCP schemas → GraphRAG context) is byte-identical and front-loaded across coder/planner iterations; tag `cache_control` breakpoints for Anthropic/OpenAI providers; measure per-session savings in telemetry. Files: `tools/llm_gateway.py`, `agents/planner.py`, `agents/coder.py`. **DoD:** tokens-saved metric > 0 in session telemetry.
- [ ] **13.2 — WBSStep `depends_on` Schema Extension (closes DEBT-044).**
  Add `depends_on: Optional[List[int]] = None` to `WBSStep`; update `ValidateWBSDependenciesTool` to detect true DAG cycles; add SCHEMA_EVOLUTION.MD §15 entry.
- [ ] **13.3 — Remaining Integration DEBTs Sprint.**
  Close DEBT-049 (`SkillInvokeTool` embedder injection via graph-level factory), DEBT-054 (`agent_todos` channel runtime wiring into a cognitive node), DEBT-051 (`task_list` owner-scoped visibility for non-orchestrator roles).
- [ ] **13.4 — Pre-Launch Innovation Gate.** Prompt caching tokens-saved metric > 0; DEBT-049/054/051 closed; `pytest` green · `mypy .` 0 · `npm run compile` 0.

---

## PHASE 14 — Portfolio Level (Standout Release) ⬜

> Final preparation to showcase the tool. Content migrated from old Phase 11.

- [ ] **14.1. Full Dockerization.** `Dockerfile` + `docker-compose.yml` to launch the full architecture (LanceDB + Backend) with a single command.
- [ ] **14.2. Binary Packaging (Zero-Friction Install).** **PyInstaller / Nuitka:** compile `/ailienant-core` (FastAPI + LanceDB + Tree-sitter) into a per-OS binary (`.exe` / macOS / Linux). **VS Code Extension Bundling:** the TS extension unpacks and executes the local binary in background on install. The user needs no Python, Docker, or Node installed.
- [ ] **14.3. Visual Documentation.** `README.md` final with real architecture diagrams.
- [ ] **14.4. Autonomous Demo.** Recording where TestAgent + LogicAgent + AnalystAgent solve a cyclic bug unattended.
- [ ] **14.5. Final Checkpoint Gate.** Zero-Friction Install E2E validation + project closure.

---

## Appendix — Pivot History

Historical architectural decisions (`[ARCH-PIVOT v3]` Dynamic Concurrency, `[ARCH-FINAL]` Tiered Caching, `[ARCH-FINAL]` Tiered Checkpointing, removal and reintroduction of `immutable_wbs`, etc.) are consolidated in `docs/SCHEMA_EVOLUTION.MD`. This manifest maintains only the **active contract** so that "what is left?" remains answerable in a single read.

For granular audit of completed steps per sub-phase, see `docs/DEV_JOURNAL.md` (Phase 8.x active log) and `docs/DEV_JOURNAL_ARCHIVE.md` (Phase 0–7.19 history).
