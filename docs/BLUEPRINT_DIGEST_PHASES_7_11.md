# Blueprint Digest — Phases 7 to 11

> **What this is.** The single consolidated record of every architectural blueprint written for Phases 7–11.
> It replaces twelve per-phase files that were deleted on 2026-11-04 (table below). Each section keeps the
> binding decisions (ADR numbers preserved, so code comments, journal entries and the manifest can still be
> grepped against them), the key numbers, the corrections made during implementation, the anti-patterns,
> and the gates that certified each phase.
>
> **How to use it.** Read §0 first — it is the distilled set of rules that recur across every phase and
> still govern new work. Then jump to the phase you are touching. Detailed per-sub-phase scope still lives in
> `docs/PROJECT_MANIFEST.md`; completion history lives in `docs/DEV_JOURNAL.md` / `DEV_JOURNAL_ARCHIVE.md`;
> wire/state contracts live in `docs/SCHEMA_EVOLUTION.MD`. Where this digest and those disagree, they win.
>
> **Binding status.** Every phase below is closed except **Division 8.16** (8.16.0 shipped; 8.16.1–8.16.4
> waiting on a telemetry GO/NO-GO verdict). §9 of this digest is the record for that decision gate — record
> the verdict there.

## Source files replaced

| Deleted file | Actually covered | Section here |
|---|---|---|
| `PHASE_7_BLUEPRINT.md` | 7.10 Cognitive Transparency + 7.11 VS Code Native Mesh (+ ADR-707 Native Thinking amendment) | §1 |
| `PHASE_7_13_BLUEPRINT.md` | 7.13 Enterprise Spinal Cord (Push / event-driven) | §2 |
| `PHASE_7_14_BLUEPRINT.md` | 7.14 Zero-Bubble UI + successor note on 7.15–7.17 | §3 |
| `PHASE_7_18_BLUEPRINT.md` | 7.18 Six-Technique Hardening Sweep | §4 |
| `PHASE_7_19_BLUEPRINT.md` | 7.19 Agentic Execution Cell & PTY | §5 |
| `PHASE_8_BLUEPRINT.md` | 8.0 strict-typing eradication campaign | §6 |
| `PHASE_8_BENCHMARK_MCP_BLUEPRINT.md` | 8.3 Benchmark, 8.4 MCP, 8.5 Gateway (umbrella), 8.6 Gate | §7 |
| `PHASE_8_5_BLUEPRINT.md` | 8.5 External Capability Gateway (locked decisions D1–D8) | §7.3 |
| `PHASE_8.13_BLUEPRINT.md` | 8.13 Polyglot Devcontainer Execution Layer | §8 |
| `PHASE_8_15_BLUEPRINT.md` | 8.15 Dynamic Subagent Dispatch | §10 |
| `PHASE_8_16_BLUEPRINT.md` | 8.16 Importance-Aware Session Memory (decision gate) | §9 |
| `PHASE_11_BLUEPRINT.md` | 11.11 Agent Output Quality + 11.12 Stack Guidance | §11 |

---

## 0. Cross-cutting lessons (the rules that kept recurring)

These came up in phase after phase. They are the most useful thing to carry into new work.

1. **Audit before building — the codebase is usually more mature than the brief.** 7.13, 7.14, 7.18 and
   8.15 all opened with "build X from scratch" and found X mostly built. 7.14 found ~20 of 25 elite-IDE
   techniques already shipped; 7.18 found 5 of 6 agent techniques strong. The work is wiring, not
   rebuilding. Rebuilding mature systems is pure regression risk.
2. **Reuse over rebuild; extend, never fork.** Every blueprint carries a "reuse" table. Examples: the
   uuid4 boundary-tag sandbox (planner → analyst → skills), `io_coalescer`'s debounce shape (token
   batcher, telemetry bus, rate limiter), `token_ledger`'s lock pattern (graph write lock), the
   `gateway/ledger.py` reserve/commit/refund *discipline* (dispatch ledger), `ASTEngine`'s content hash
   (response cache). The permission engine (`classify_tool_privilege` / `evaluate_action`) must never be
   forked — consumer MCP, provider gateway and subagent dispatch all share it.
3. **Surface conflicts, don't silently build them** (Charter §4). 7.18's "version-vector OCC" request
   contradicted LangGraph reducers and was resolved as "assert the existing guarantee" (Option A). 8.15
   rejected adopting `deepagents`. 8.16 rejected its own manifest criterion as unmeasurable.
4. **Verify a premise in code before acting on it.** Repeated mis-attributions were caught only by reading
   code: 7.13's "dashboard panels are dead" (they fetch real endpoints), 7.13's "mcts_coder needs the graph
   lock" (it never touches `core/db.py`), 8.0.7's "unsilencing mypy fixes import cycles" (static setting,
   no runtime effect), 11.11's three wrong root causes (§11.2).
5. **Default-off / topology-identical rollout for risky graph changes.** 8.15 is behind
   `AILIENANT_ENABLE_DYNAMIC_DISPATCH`, checked at graph *construction*, so a disabled deployment compiles
   the exact pre-existing node/edge set. New edges wrap existing targets; they never replace them.
6. **Strict machine-parsed contracts never get reasoning scaffolds.** Coder SEARCH/REPLACE and any JSON
   contract stay free of `<thinking>` (DEBT-013 recurrence class). If you want narration, run a second
   free-form pass (11.11 Item A, planner pre-draft pass).
7. **Untrusted vs trusted execution are opposite threat models.** The locked Docker cage (no network,
   read-only, non-root) runs untrusted model output; the devcontainer runs the user's own trusted project.
   Never route one into the other (8.13).
8. **Never hang; degrade.** External callers with no human get a structured deny-report (8.5 D5);
   devcontainer build timeouts degrade (8.13); the gateway fails fast with "open VS Code" when the host is
   down (8.5 D1a); streams have watchdogs (7.13 ADR-715).
9. **Budgets: scale result-bearing ones by complexity, keep judge/telemetry/compression ones tight**
   (11.11 §4). No flat global bumps. Derive limits from the resolved model's real context window.
10. **Gates follow the sibling convention**: `tests/test_phaseX_checkpoint_gate.py`, test-only, one
    load-bearing assert per row, real entry points, structure asserts via `ast` (not docstring substrings).
    Frontend-only rows are certified by `npm run compile` + manual smoke.
11. **Contracts are additive-only.** Every new WS field, state channel, ACK/requestId field defaulted
    safely so pre-existing checkpoints deserialize unchanged.
12. **Cognitive-isolation fence (from Phase 4.1.5).** Only `agents/analyst.py` imports `brain.personality`.
    Reaffirmed for the `ErrorCorrectionAgent` (7.13) and enforced by an audit test (ISO1).

---

## 1. Phase 7.10 + 7.11 — Cognitive Transparency, Connective Integration & VS Code Native Mesh

**Status:** CLOSED (7.10.1–7.10.5, 7.11.1–7.11.8). ADRs 701–707.

### 1.1 Why
After 7.9.B the system worked mechanically but did not feel like one product. Four verified symptoms:
1. **Identity leakage** — chat and analyst prompts never forbade naming the backing model ("I am Qwen").
2. **No cognitive transparency** — the coding path emitted one `planner_agent` ping, then minutes of silence.
3. **Context-blind analyst** — `context_paths` was in the contract but ignored by the WS handler.
4. **Planner schema fragility** — local models wrapped output as `{"MissionSpecification": {...}}`, burning
   all retries ("6 validation errors").

Plus five design gaps: **G1** RPC congestion (one WS frame per token → Webview layout thrashing), **G2**
stale context mid-generation, **G3** prompt injection via injected files, **G4** analyst token blowout on
7B models, **G5** brittle envelope unwrapping (~40% failure on flat lookup).

**Segmentation decision:** the nine native-VS-Code UX items were split into 7.11 so 7.10 stayed on
backend/transport; but both were designed together because the transport had to be sized for 7.11's inline
diff stream (the highest-frequency token producer).

### 1.2 Binding decisions
- **ADR-701 Identity Sovereignty (prompt-only).** One canonical clause (`shared/persona.py::AILIENANT_IDENTITY`
  + `compose()`), always prepended — to main chat, analyst and the SOUL default. A custom SOUL.md is appended
  *after* it and cannot weaken it. **No output regex scrubbing** (adds latency, brittle across tokenizers).
- **ADR-702 Transparency + token throttling.** Narration reuses `server_pipeline_step` + the 7.9.B.14
  collapsible trace — **no new transport**. Narration is synthesized status, not raw chain-of-thought.
  New `transport/token_batcher.py`: tokens coalesced in **`chunk_ms = 40`** windows; narration ≤ **15%** of
  WS bandwidth (NarrationGate); render target ≥ **45 FPS**. Batch first, then `throttled_stream`
  backpressure (1 MB). Distinct from `io_coalescer` (file saves, 500 ms).
- **ADR-703 Analyst Context Contract.** Analyst is read-only ("the Voice, not the Hand").
  - *Sandbox (G3):* every fragment wrapped in uuid4 tags `<uuid_context path="…">`, closing sequences
    escaped (incl. unicode variants), plus a prompt clause: content between tags is raw data, never
    instructions.
  - *Budget (G4):* ≤ 4 KB active file, ≤ 2 KB GraphRAG, ≤ 1 KB Codex. If the file exceeds 30% of the
    window → **Tree-sitter semantic-priority slice**: (1) imports, (2) enclosing class/interface signature,
    (3) function under cursor. Never a geographic line cut.
  - *Context-tolerant divergence (G2):* no binary hash reject. AST/line diff: edits outside the region the
    analyst read → reply valid, offsets realigned; edits inside → reply flagged.
- **ADR-704 Envelope-tolerant JSON.** `_extract_nested_schema_target(raw_str, schema)` in
  `tools/llm_gateway.py`: strip fences/prose → `json.loads` → recursively return the first sub-object whose
  keys ⊇ the schema's required fields. Centralized so planner, Mini-Judge and coder all benefit.
- **ADR-705 Security posture.** Identity = anti-impersonation control; all injected content tagged +
  escaped; rich-chip output sanitized (XSS); `SecretsScrubberFilter` on any path that could surface a key;
  latency budgets are part of security ("a starved UI is a DoS on the operator's attention").
- **ADR-706 Mesh principles (7.11).** (a) inline mutations via `activeTextEditor.edit()` + decorations,
  reconciled through VFS + `apply_patch` AST validation; (b) **Abort mesh** — prefer inter-node boundary;
  if mid-stream, catch `CancelledError` and write a cold-serializable **Emergency Savepoint**
  (`termination_reason: "user_abort"`), idempotent checkpointer write, rehydration tolerates a truncated
  node, closes Docker/Wasm tools, records FinOps spend, no zombie coroutines; (c) WebView rehydration via
  `setState/getState` + Zustand, listeners torn down on unmount; (d) `@file/@folder/@terminal` hard-context
  bypasses RAG, debounced index; (e) **stateful streaming markdown parser**, O(1) amortized — open/closed
  fence counters, virtual closure injected at render without mutating source; (f) native HITL toasts when
  the panel is closed; (g) time-travel = re-send `thread_id` + `checkpoint_id`.
- **ADR-707 Native Thinking (amends ADR-702).** When the model exposes native reasoning (Anthropic
  Extended Thinking; DeepSeek-R1/QwQ via LiteLLM `reasoning_content`), raw deltas may stream on a **new
  dedicated event `server_thinking_chunk`** (`ThinkingChunkPayload{session_id, delta, token_count}`) — never
  on `server_pipeline_step`. Gateway bifurcates with `StreamDelta{kind: thinking|text}`
  (`tools/stream_delta.py`); `astream_byom_thinking` is additive. Toggle ON by default,
  `thinking_budget_tokens` default 4096, only sent if `_supports_native_thinking(model)`. Thinking coalesced
  at `chunk_ms=60`, exempt from the 15% narration budget. **Display-only:** own `Message.thinking` field,
  sanitized render, excluded from `PERSIST_TRANSCRIPT`, never fed back to agents.

### 1.3 Gate (7.10.5)
ID1 identity across ≥2 models · TR1 narration before answer, 40 ms batches · TR2 narration ≤15% frames ·
AN1 file awareness · AN2 Codex self-knowledge (`docs/AILIENANT_CODEX.md`) · AN3 injection neutralized ·
AN4 slice fidelity · AN5 tolerant divergence · PL1 envelope variants incl. `{"json":{"MissionSpecification":…}}`
· DB1 dashboard round-trips · REG (≥584 tests at start).

### 1.4 Anti-patterns
New transport for narration · importing `brain.personality` outside the analyst · regex model-name
scrubbing · analyst mutating files · unbounded context injection · one frame per token · string-concatenating
raw file text into prompts · geographic slicing · binary hash rejection · O(N²) markdown re-scan · abort
without a cold savepoint · unsanitized chip output · blocking the WS receive loop.

---

## 2. Phase 7.13 — The Enterprise Spinal Cord (Push / Event-Driven)

**Status:** CLOSED (7.13.1–7.13.12). Gate: `test_phase7_13_checkpoint_gate.py` (20 tests), 768 pytest,
mypy 225 OK. ADRs 708–718 (717 intentionally unused).

### 2.1 Why
The system was a Pull model: IDE saves/renames/deletes never reached the brain; GraphRAG was a stale
per-session snapshot; built features were orphaned (Planner Manual Mode, Dreaming daemon, DLQ resume API);
raw errors could reach the user; no file-based observability. 7.13 is the shift to Push. It also retrofitted
code owned by closed Phases 0–6 (see §2.4) — manifest tasks carry `Ref: 7.13.x` back-pointers.

**Build order** (v2): safety spine → privacy gate → telemetry log (the verification instrument) → bus →
reactive index → manual Dreaming → self-healing → client resilience → Planner UI → dashboard sync → dedup
→ gate.

### 2.2 Binding decisions
- **ADR-708 IDE Telemetry Bus.** A silent `client_ide_telemetry` channel (`file_saved/renamed/deleted/
  active_changed`) on the **existing** WebSocket — no second socket. Fire-and-forget, no toasts, off the
  receive loop, reuses the 150 ms debounce in `ide_sync.ts`. Wired the orphan `client_file_delete` sender.
  **Priority class** in `ws_client.ts`: chat/agent-status frames have absolute priority; telemetry is
  droppable/deferrable; `_pendingSends` capped.
- **ADR-709 Reactive incremental indexing.** On save, re-index only that file under lock + per-file
  single-flight `(filepath, project_id)`; `semantic_upsert` + graph-node refresh; **content-hash idempotent
  unified entry** shared by human saves and agent `apply_patch` (prevents duplicate LanceDB nodes); circuit
  breaker; delete → purge, rename → migrate. No full re-crawl on save.
- **ADR-710 Manual Dreaming (rewritten — idle trigger abandoned).** Consolidation only on an explicit action
  (dashboard button + VS Code command → `client_dreaming_run`). Rationale: an idle timer overloads the CPU
  during builds, races returning typists, and burns tokens unattended — *the user owns when tokens are
  spent*. **Amendment — targeted Dreaming:** optional `focus_area` (Architecture & Patterns / Refactoring &
  Tech Debt / Bug Fixes / free text; `None` = whole workspace). `OvernightDaemon` became a stateless
  on-demand service `run_consolidation(project_id, focus_area=None)`: corpus from
  `build_workspace_overview`, LLM call **outside** the graph lock, final `semantic_upsert` **under** it.
  Race guard = monotonic per-project save epoch in `main.py` (a save mid-run bumps it and cancels the task;
  daemon re-checks before commit → `aborted_stale`). Over-budget → `refused_budget` before any LLM call.
- **ADR-711 Self-healing `ErrorCorrectionAgent`.** Reflexion node: tool failure / schema hallucination / API
  crash → read traceback → read the offending file → propose fix → retry, **≤3**, then DLQ. Unifies scattered
  retry budgets (guardrail=2, planner=2, MCTS=3); failure-signature cache as a cross-turn breaker. Never
  imports `brain.personality`; patches go through `apply_patch` + HITL only.
- **ADR-712 Live telemetry log.** `.ailienant_telemetry.log` at workspace root (`core/telemetry_log.py`):
  `SecretsScrubberFilter` mandatory, `.gitignore`'d, explicit UTF-8 (cp1252 lesson), `RotatingFileHandler`.
  **It absorbs Phase 8 observability** — later phases build on it, never re-create it (Charter §12).
- **ADR-713 Multi-turn mode machine + Planner surface.** `Workspace.tsx` → Chat / Planner / Dreaming modes;
  Manual Mode renders a locked multi-turn form (`PlannerSession.tsx`), never dumped into chat; wired the
  orphan `client_planner_mode_toggle`.
- **ADR-714 Concurrency & resource safety.** Per-project `asyncio.Lock` serializing all
  `dependency_graph`/LanceDB writes (`upsert_dependencies` **and** `purge_file_nodes`); taken by the
  daemon and the GraphRAG extractor — **not** `agents/mcts_coder.py` (it never touches `core/db.py`).
  Per-session background-task registry cascade-cancels indexer + daemon on disconnect (extends the existing
  `cleanup_session` hook + `active_tasks` drain). Inbound per-client WS token-bucket rate limit.
- **ADR-715 Frontend stream resilience.** Correlation/request IDs (server-side dedup on reconnect), client
  stream watchdog, reliable send queue + re-attach, `isAborting` never survives teardown, ABORT/HITL ACKs,
  `document_version_id` seeded at startup, capped buffers, StagingArea stale-patch refresh. All fields
  additive.
- **ADR-716 Orphan recovery.** Verify-or-wire-or-delete, gated by a user-approved inventory. DLQ resume
  surface (`/task/resume` + `/dlq/pending`).
  **Correction:** the dashboard is HTTP-served (same-origin `fetch`, no WS, no host bridge); panels unmount
  on tab switch, so there was no polling leak. WS "push-fed panels" would have been over-engineering →
  gate DB1 amended to **visibility-gated polling**. Dead FE types `client_master_toggle` /
  `client_profile_change` deleted (backend handlers kept). OOM toast wired via new `server_oom_engaged`.
- **ADR-718 Centralized privacy filtering.** Telemetry exclusion uses only the Dual-Rules resolver
  (`./.ailienant/.ailienant.json` local deep-merged over `~/.ailienant/.ailienant.json`; `core/rules.py::
  RuleManager`) — no new ignore files. Gate fires before anything leaves the IDE. Status-bar **Incognito**
  toggle pauses the bus instantly.

### 2.3 Gate rows
SC1 silent telemetry · SC2 reactive single-file index · PR1 excluded files never pushed · PR2 Incognito
(frontend) · CC1 graph lock · RL1 rate limit · SF1 single-flight · CN1 task cancel · DR1 manual-only Dreaming
+ save-mid-run abort · AL1 self-heal ≤3 then DLQ · ISO1 no personality import (AST check, not substring) ·
FR1 watchdog · FR2 no duplicate generation on reconnect · FR3 abort ACK · OR1 Planner form (frontend) · OR2
DLQ resume · OR3 Planner toggle · DB1 dashboard (frontend) · TL1 log · DD1 dedup (VFS reader unified into
`core.vfs_middleware.make_safe_reader`).

### 2.4 Retrofits into closed Phases 0–6
| Prior phase | Friction under Push | Fix |
|---|---|---|
| 0–1 FastAPI/WS | zombie background tasks on disconnect | cascade-cancel per session (7.13.1) |
| 2A–2D LLM gateway | scattered retries; failures could corrupt WS state | unified retry abstraction → DLQ (7.13.7/11) |
| 3 GraphRAG | two writers (human save + agent patch) | one content-hash idempotent entry (7.13.5) |
| 4 MCTS/Planner | graph readers vs DELETE/INSERT | readers take the per-project lock (7.13.1/6) |
| 5–6 VFS | duplicate readers in `coder.py` + `analyst.py` | single factory honoring Dual-Rules (7.13.11) |

---

## 3. Phase 7.14 — Zero-Bubble UI Transformation (+ 7.15–7.17 context)

**Status:** CLOSED (7.14.0–7.14.7). Frontend-only. ADRs 720–726 (719 unused as boundary marker).

### 3.1 Why
The panel read as a chatbot (bubbles, plain `<pre>` code, no diffs). Audit: ~20/25 elite-IDE techniques
already existed. Scope = 2 net-new epics (canvas, diff) + 3 enhancements + strategic gaps.

### 3.2 Binding decisions
- **ADR-720 Zero-Bubble canvas.** No per-message border/radius/role background/`max-width`; 100% width;
  1px hairline separators; role label only; dual-density typography (prose airy, code compact).
- **ADR-721 Diffs from the existing edit payload, computed client-side.** Host enriches
  `server_apply_workspace_edit` (PatchActuator already reads the old doc text) and posts a webview message
  `RENDER_DIFF {patch_id, file_path, old_content, new_content, status}`; `jsdiff` in the webview. **No
  Python contract change, no `server_chat_diff` event.**
  **Amendment A:** additive read-only `GET /api/v1/sessions/{thread_id}/context` →
  `{context_window, context_used_tokens, context_pct}` for the context meter. The cumulative ledger was
  rejected as a proxy: it is monotonic (Σ all turns) while the live window is pruned, so it would pin at
  100% in long sessions.
- **ADR-722 Render stack.** `jsdiff` + `react-diff-viewer-continued` + `shiki`; hatching on unbalanced hunks;
  colors bound to `var(--vscode-diffEditor-*)`. Shiki dynamically imported, fine-grained core; highlight on
  block-complete, diff on edit-arrival — never per token. Bundle-ceiling sentinel in `esbuild.js`.
- **ADR-723 Ghost telemetry.** Status dots on tool chips, live action log while thinking, live token
  footer (only the live counter was genuinely new).
- **ADR-724 Inline per-diff HITL.** Accept/Reject/Comment under each diff via the existing `HITL_RESPONSE`;
  comment opens a nested input that **does not clear the composer draft**; `Ctrl+Enter`/`Esc` on focus.
  Approval is per-patch, not per-hunk (per-hunk IDs need backend — deferred).
- **ADR-725** Inline Revert reuses `BRANCH_FROM_CHECKPOINT`; minor @-mention polish only.
- **ADR-726** In scope: context-budget meter + auto-accept toggle. Deferred to Phase 11+: parallel
  multi-thread runs, cross-session context refs, dual GUI/CLI mode.

### 3.3 Successor track — why 7.15 existed
A pre-gate audit found the UI surfaced affordances the backend didn't honor. **Single root cause:**
`core/task_service.py::_run_coding_task` called planner/coder nodes directly instead of through the
compiled LangGraph app — disabling mode routing, the ideation loop, the checkpointer (no Rewind) and token
streaming at once. Also: RBAC built but unwired, Spanish prompts, a stale "applying changes is not yet
enabled" string. Fixed in **7.15 (ADR-727..732)**, which *does* change the Python contract. **Lock:**
7.14.7 could not close until 7.15.7 was green. Chat syntax highlighting (DEBT-006) moved to **7.16**
(host-side shiki tokenization) and **7.17** (streaming render, rAF coalescing) — ADR-733..739. (Those
phases had no separate blueprint file.)

---

## 4. Phase 7.18 — Six-Technique Enterprise Hardening Sweep

**Status:** CLOSED (7.18.0–7.18.6). Backend. ADRs 740–746. Gate: `test_phase7_18_checkpoint_gate.py`.

### 4.1 Audit
System Prompt ✅ · Contextual RAG ✅ (recency was a hardcoded `0.5`) · Chain-of-Thought ✅ · Tool Use ✅
(`response_format` sent unconditionally) · Few-Shot 🟡 (no code-style exemplars) · **Feedback Loop 🟡 —
the headline gap:** sandbox + execute-tier tools existed, but a `run_command` WBS step dead-ended as
`EXECUTE_TIER_DEFERRED`. No write → run → capture → fix loop.

### 4.2 Binding decisions
- **ADR-740 Closed-loop sandboxed executor.** `run_command` dispatches through `get_active_adapter()`.
  Output parsed into **structured diagnostics** `[file, line, code, message]` (reusing `ValidationError` /
  `lsp_filter` JSON; extended to mypy and pytest summaries) — raw traces blow attention cost. Failure
  returns a delta that mimics `reflexion_guard` (`healing_required`, compact capped `last_error_trace`,
  `failure_signature`, `correction_attempts+1`) so the **existing** `route_after_coder →
  error_correction` path re-drafts. No new loop or budgets. `EXECUTE_TIER_DEFERRED` only when the adapter is
  `None`. **Highest risk:** `candidate_files_from_traceback` only parses CPython tracebacks → thread the
  step's `target_file` through `extra_candidates`; tests must assert a correction actually fires.
- **ADR-741 Session-heatmap recency.** `recency = 0.7·time_decay + 0.3·access_frequency` (exponential decay
  on `indexed_at`/mtime + bounded in-session access counter). CSS formula
  `0.5·Sem + 0.3·Graph + 0.2·Time` unchanged.
- **ADR-742 `response_format` degradation.** Strip for local targets and/or retry once without it; the
  existing 3-layer JSON repair is the floor. No new repairer.
- **ADR-743 AST-skeleton few-shot.** 2–3 same-language nearby functions distilled to signature + types +
  docstring, body → `...` (>70% token cut), via the tree-sitter `ast_engine` (not stdlib `ast`), framed
  "match conventions, do not copy logic", separate from the topology RAG block. Best-effort `""`, byte-capped.
- **ADR-744 Semantic response cache.** LRU (size + TTL) keyed by intent hash + AST hash of context +
  `project_id` + model id; deterministic calls only (`temperature=0`); dirty buffers bypass; eviction via
  `ASTEngine.invalidate`. Must be **off** during benchmarks (§7.1).
- **ADR-745 MCTS-into-live-loop deferred** (DEBT-009) until 7.18.0's verdict existed as its reward signal —
  later closed inside the 7.19 agentic cell.
- **ADR-746 Gate.** Rows EXLOOP1/2, DIAG1, REC1, RF1, FS1, CACHE1, OCC1, MCTS-DEFER.

### 4.3 The OCC conflict (resolved by Option A)
The request for version-vector OCC on graph state contradicted the design: OCC already exists per file
(`document_version_id`, `base_hash` stale-guard), and graph state uses **reducers** (`operator.add`,
last-writer-wins merge) precisely to **merge** concurrent `Send()` fan-out writes. Version vectors would
**abort** what reducers merge. Decision: no new mechanism; gate row OCC1 asserts the existing guarantee.

---

## 5. Phase 7.19 — Agentic Execution Cell & Persistent Audit Trail

**Status:** CLOSED (7.19.0–7.19.8). ADRs 747–755. Gate: `test_phase7_19_checkpoint_gate.py`.

**Goal:** go from 7.18's batch, planner-mediated loop (one buffered command, `tty=False`, no stdin) to a
continuous LLM-driven **ReAct loop over a live bidirectional terminal** — as a bounded cell *inside*
LangGraph, not a replacement for the graph or MCTS. "Glass-box": granular WS events (`tool_call_start`,
`pty_chunk`, `ast_mutation_diff`) instead of a 3-minute spinner.

**Locked decisions:**
1. Native-direct governance = **hybrid**: strict allowlist for standard tooling + **session-level approval**
   for unknown commands. Not per-command HITL (would make the loop unusable).
2. The cell **coexists with `run_command`**: a router sends trivial single-step ops through the simple path,
   multi-step debugging through the cell (`WBSStep.requires_iteration`).
3. **Bidirectional PTY from day one** — a process stuck on `[Y/n]` with one-way `subprocess.run` would block
   the event loop.

**Conflicts handled:** every cell iteration emits a checkpoint + trajectory record (keeps Rewind); the cell
sets `bypass_cache` per iteration (7.18.4 cache assumes single-shot); MCTS candidate selection lives in the
cell (closes DEBT-009).

**WBS:** 7.19.0 `SandboxSession` + PTY multiplexer (747) · 7.19.1 VFS↔sandbox sync with OCC (748) · 7.19.2
ReAct cell node + 3 tools (749) · 7.19.3 multi-axis governor — steps ∧ tokens ∧ time (750) · 7.19.4 typed WS
telemetry + O(1) routing (751) · 7.19.5 audit widgets, virtualized >1000 lines (752) · 7.19.6 xterm.js PTY +
Send/Stop toggle on the same button (753) · 7.19.7 progressive ☐→✅ checklist + GFM rendering (754) · 7.19.8
gate (755).

**Governor cost bound:** `Cost_total = Σ (C_in·T_in⁽ⁱ⁾ + C_out·T_out⁽ⁱ⁾)` with `N ≤ N_max`,
`Σ tokens ≤ token_cap`, `elapsed ≤ time_cap`; any axis tripping concedes gracefully.

---

## 6. Phase 8.0 — Strict-Typing Eradication Campaign

**Status:** CLOSED 2026-06-08. Objective met: `mypy --strict main.py` → 0 and **zero**
`follow_imports = silent` modules, while `mypy .` (the enforced gate) stayed green throughout (0/247,
pytest 924/0).

**Method:** bottom-up topological unsilencing (Tier 0 leaves → Tier 7 `main.py`). Each sub-phase added a
new strict gate without breaking `mypy .`; annotation-only changes, zero logic changes.

| Sub | Outcome |
|---|---|
| 8.0.0 | 64 surface errors in 20 files (bare `dict` → `Dict[str, Any]` ×35, 15 stale ignores, `no-any-return` casts). DEBT-003/004 closed. |
| 8.0.1 | Unsilenced `shared.hardware`, `agents.analyst`, `tools.patch_tool` (+ `brain/ideation.py`). DEBT-001 closed. |
| 8.0.2 | Unsilenced `tools.llm_gateway` (already clean — the wall only hid consumer errors). `MODEL_MEDIUM` imported from `shared.config`. DEBT-015/016 closed. |
| 8.0.3 | Unsilenced `core.vfs_middleware` + `core.compute_pool`; typed `VFSMiddleware.__new__` killed 5 dead ignores. |
| 8.0.4 | Retyped `tool_rag_select_node(state: AIlienantGraphState)` → last residual gone. |
| 8.0.5 | `core.db` already clean; `brain.memory` needed `[mypy-networkx,networkx.*]` (both names required). DEBT-018 (unbounded networkx graph RAM) logged. |
| 8.0.6 | `api.websocket_manager` — last wall. DEBT-019 (late HITL/ack responses leak in request buffers) logged. |
| 8.0.7 | `brain/engine.py` certified. Moving its deferred imports to the top was **declined**: they avoid real runtime cycles; mypy silencing has no runtime effect. |
| 8.0.8 | Untyped third-party ignores promoted to config blocks (lancedb, docker, requests). 28 inline ignores remain, all used and tracked (DEBT-014, 020–023). |

**Lessons:** walls usually hid *consumer* errors, not the module's own; **DEBT-014** — retyping
`run_coder_node`/`run_planner_node`/`run_analyst_node` to the TypedDict cascades to 63 errors in 19 files,
so 3 `type-var` ignores stay. Frontend (8.0.FE) has no campaign — `tsc` already strict; gate is
`npm run compile`.

**Continuous Registry Protocol (still binding):** a strict-mode error, vulnerability or typing debt found
outside the current scope is logged in `TECH_DEBT_BACKLOG.md` immediately, not fixed in place.

---

## 7. Phase 8.3 / 8.4 / 8.5 / 8.6 — Benchmark, MCP Ecosystem, External Gateway

**Status:** all CLOSED (8.6 gate closed 2026-06-19). ADRs 756–760.

### 7.1 ADR-756 — Precision benchmarking & ablation (8.3)
- **Two distinct metrics.** **Pass@1** for plain codegen (at `temperature=0`, Pass@k collapses to Pass@1).
  **Resolve@k** for multi-file refactors: solved within k≤3 *dependent* self-correction cycles (valid at
  temp 0 because context changes each cycle). In arms with no ReAct loop, Resolve@3 = Resolve@1.
- **Hypotheses (2×2, one axis each, stated multiplicatively):**
  - **H₁ precision** (model fixed, TCI>60, both pinned to CLOUD): `Resolve@3(G4) ≥ 1.25 × Resolve@3(G1)`.
  - **H₂ cost** (pipeline fixed): `Resolve@3(TCI-routing) ≥ 0.95 × Resolve@3(G4-force-cloud)` and
    `tokens ≤ 0.60 ×` force-cloud.
- **Arms:** G1 zero-shot · G2 vector-only RAG (net-new **Strategy injected from the harness**, never a
  `benchmark_mode` flag in production) · G3 GraphRAG + TCI, ReAct off (`requires_iteration=False`) · G4 full
  · G4-force-cloud (harness override of `resolve_provider`). G2↔G3 = graph value, G3↔G4 = ReAct value,
  G4↔force-cloud = routing value.
- **Statistics:** n ≥ 30 *distinct problems* per arm; Wilson CIs; reruns only where cloud variance shows;
  `seed=42`, temp 0 (declared caveat: not bitwise deterministic on cloud/quantized models).
- **Token Efficiency Ratio** stratified by TCI bucket `[0,40) / [40,75) / [75,100]` (raw ratio flatters
  systems that only solve the easy ones).
- **Guards:** `benchmark_budget_usd` cap aborts cleanly with a partial report; **frozen corpus** at a
  pinned SHA + golden patches; `BenchmarkOracle.run_oracle(snapshot, patch) -> Verdict` is the sole arbiter.
- **Index hygiene (mandatory):** pre-index and block until `is_complete` (assert `get_dependents` non-empty);
  build once per corpus, time it separately (`indexing_time_s`); embedding pre-flight hard-aborts; **response
  cache OFF** (a cache hit falsifies token accounting).
- Ablation runs in-process via `process_task()`; 2–3 E2E cases over real HTTP/WS. Output: `report.json`
  (verdicts, aggregates, CIs, H₁/H₂ verdicts, stratified ratios, ablation deltas). Harness later relocated to
  `core/benchmark/` (DEBT-038); `tests/benchmark/` holds gate tests only.

### 7.2 ADR-757 — MCP config SSoT + privilege classification (8.4)
- **SSoT:** SQLite `mcp_servers` table stays the truth; `.ailienant/config.json` is a serializable
  projection. Import = idempotent upsert by name. Secrets never in JSON (`key_ref` placeholder); export
  redacts URL userinfo.
- **`classify_tool_privilege(tool, description, server)` — fail-closed** (fixed DEBT-026, where every MCP
  tool was hardcoded READ_ONLY). Precedence: curated catalog > verb heuristic > **DANGEROUS default**.
  Heuristic: get/list/read/search/fetch/describe → READ_ONLY; create/update/write/push/add/set → WRITE;
  exec/run/invoke/spawn → EXECUTE; delete/drop/force/merge/reset/purge → DANGEROUS. Catalog pins e.g.
  `github.merge_pull_request` DANGEROUS, `postgres.query` READ_ONLY / `postgres.execute` EXECUTE.
- **Amendments:**
  - Secret substrate = **backend-mask** (`core/config/mcp_secrets.py`, `0600`, masked on read, mirrors
    BYOM), not VS Code SecretStorage (the dashboard is a fetch-only webview that cannot reach it).
  - **8.4.4 multi-session registry:** `_sessions` / `_exit_stacks` keyed by server; idempotent bootstrap;
    one exit stack per server; `shutdown_mcp_sessions()` in lifespan; auto-connect at startup;
    `evaluate_action` gate in `_arun` (READ_ONLY short-circuits; DANGEROUS overrides AUTO);
    `MCP_HITL_TIMEOUT_SEC` default 120 s. DEBT-027 closed.
  - **8.4.5 Skills:** `core/skill_resolver.py`; semantic match (L2-normalized cosine) or explicit
    `invoked_skill_id`; workspace scope beats global; `enabled=0` is absolute; injected into the planner only
    (coder side = DEBT-032), boundary-sandboxed, `_SKILL_BLOCK_CHAR_CAP` 3000; UI chip replaces raw paste.
  - **8.4.6 Browse registry:** connect-time env injection on top of the SDK default environment (not
    `os.environ.copy()`); `shutil.which` resolution, `shell=True` forbidden; args via `urlencode`; reinstall
    closes first; install restricted to curated servers; tier badges shown before install. DEBT-031 closed.
  - **8.4.7 Live gate:** LangChain calls `_arun` without session kwargs, so the gate was dead → fixed with
    **ContextVars** (`_task_session_id`, `_task_session_mode`) set before `astream()` and reset in
    `finally`. **Trust-once valve** per session+tool, cleared at task end. FE card renders
    `request_kind == "MCP_TOOL_CALL"`. DEBT-029 closed.

### 7.3 ADR-759 — External Capability Gateway (8.5; supersedes single-tool ADR-758)
A multi-tool **stdio MCP server** (`ailienant-core/gateway/`) letting external agents (Claude Code, Codex)
drive AILIENANT. Pure adapter: never alters `AIlienantGraphState`, `ContextMeter` or `MissionSpecification`.
Core problem: MCP is request/response with no human; AILIENANT is long-running, streaming and HITL-gated.

| Decision | Rule |
|---|---|
| **D1** Two paths | EXECUTE verbs loop back to `POST /api/v1/task/submit` through the real `_require_token`; READ_ONLY verbs call core managers in-process. |
| **D1a** Host discovery | Host writes `~/.ailienant/run.json` `{port, loopback_token, pid}` with **`0600`**. It is a hint: gateway checks pid alive + ~2 s connect; if dead, returns "AILIENANT host is not running. Please open VS Code to start the engine." Fail-fast; no headless auto-start. |
| **D2** Identity | Each caller has its own token → `caller_id` (separate from the loopback service token). |
| **D3** Durable ledger | `GatewayCallerLedger` — token-bucket rate + cumulative budget, **persisted to disk** (an in-memory ledger resets on every reconnect). Out-of-band from graph state. Part of the MVP. |
| **D4** Conservative posture | Pinned before `evaluate_action`; never silent AUTO; no self-escalation; built from existing enums (curated EXECUTE verbs pre-authorized at the capability layer; the spawned task's internal DANGEROUS/HITL → deny). |
| **D5** HITL-degrade | Return `{status: "denied", tier, reason, would_have_required: "human_approval"}` immediately; never call `request_human_approval`. |
| **D6** Async | Poll pair: `run_task`/`run_benchmark` return `task_id` + instruction; `check_task_status` polls. No 202, no stream. |
| **D7** Versioning | Semver `protocol_version` + per-capability `schema_version`; N-minor deprecation window. |
| **D8** Symmetric permissions | Gateway capabilities classified on the same table; forking the engine is forbidden. READ_ONLY in-process calls still run classification + ledger. |

**Catalog v1:** `run_task` (EXECUTE, conservative), `check_task_status`, `query_memory`, `get_dependents`,
`get_workspace_graph`, `get_report` (READ_ONLY), `run_benchmark` (EXECUTE, budget-gated). The benchmark is
the first consumer, not the design goal. **Lock:** EXECUTE verbs were blocked until 8.4.1 (fail-closed
classification) shipped.

### 7.4 ADR-760 — Phase 8 checkpoint gate (8.6)
Re-certified resilience, a schema-valid `report.json`, fail-closed MCP privilege (a mutating tool never lands
READ_ONLY) and external HITL-degrade (DANGEROUS verb → deny-report, never hangs).

---

## 8. Division 8.13 — Polyglot Devcontainer Execution Layer

**Status:** CLOSED 2026-06-30 (8.13.1–8.13.6). ADR-762.

**Intent:** stop editing one `python:3.13-slim` image per language. Environment becomes data the user owns
(`devcontainer.json`), built by the user's local Docker daemon via standard tooling — like Codex
(`AGENTS.md`), Cursor background agents and Claude Code's reference devcontainer.

**Split by trust (the locked invariant):**
| Surface | Threat model | Adapter |
|---|---|---|
| Benchmark oracle, codegen Pass@1 | untrusted model output | `DockerSandboxAdapter` — locked cage, **unchanged** |
| Agent `run_command`, user's tests, interactive sessions | trusted project execution | `DevcontainerSandboxAdapter` |

**Architecture:** the extension owns the lifecycle — probe the Dev Containers extension → `devcontainer`
CLI → degrade to Docker/Native tiers. The backend adapter never shells Docker: `execute()` goes over a
host bridge (`WebSocketHostBridge`, injected from `main.py`) to the extension, which runs
`devcontainer exec` and streams output back. `devcontainer up` runs once per workspace, lazy, single-flight,
non-blocking, with timeout degrade.

**Wire contract** (`SCHEMA_EVOLUTION.MD §26`): five additive events, each with a `request_id` (UUID4)
correlation key; env passes as **`env_keys` — names only, never values** (the host resolves values itself).

**Selective HITL fallback:** if the devcontainer is unavailable *before* a command runs, trusted execution
goes to the consent-gated Native tier — never the untrusted cage. Mid-execution failures degrade, never
re-run on the host (idempotency). The `devcontainer.json` scaffold restores isolation.

**Dependency (§9):** the `devcontainer` CLI is a **host prerequisite** (PATH or Dev Containers extension),
not bundled in the `.vsix`; `@devcontainers/cli` is dev-only. DEBT-082 resolved.

**Correction recorded at the gate:** 8.13 did **not** resolve DEBT-035 (TypeScript in the *untrusted*
benchmark lane) — that is the opposite threat model; it stays `unsupported_runtime` until a locked Node
tier is justified.

---

## 9. Division 8.16 — Importance-Aware Session Memory (OPEN — decision gate)

**Status:** 8.16.0 shipped (instrumentation + report tool). Decision gate **PROVISIONAL — no verdict yet**.
8.16.1 (importance-aware retention), 8.16.2 (graduated thresholds), 8.16.3 (session summary artifact in
`.ailienant/sessions/`), 8.16.4 (gate) are neither built nor struck until a real verdict is recorded
**here**.

### 9.1 Rationale
Upgrade the existing `brain/summarizer.py::StateSummarizer` (`THRESHOLD_RATIO=0.80`, `KEEP_LAST_N=5`)
rather than build a parallel compactor — but only if real sessions compact often enough for the loss to
matter. If compaction essentially never happens, the division is NO-GO.

### 9.2 The corrected criterion (binding)
The manifest's original "median utilization approaches 0.80" can never return GO: the summarizer is a
**thermostat** — it compacts at 0.80 and resets utilization to ~0.15, so the ratio sawtooths in
~[0.15, 0.80] with a median ~0.40–0.45 for any healthy session.

**Use compaction-event frequency instead:**
- **Event** = a `source="summarizer"` record where `total_tokens > int(THRESHOLD_RATIO × token_budget)`
  (from raw counts, not the rounded `ratio`), **or** a `source="pipeline"` record with `l4_evicted > 0` or
  `l5_truncated = True`.
- **Non-trivial session** = `max(turns) > KEEP_LAST_N` **or** has ≥1 event.
- **GO** if ≥ **25%** of non-trivial sessions have ≥1 event, with ≥ **10** non-trivial sessions.
  **NO-GO** if events are vanishingly rare at adequate sample. Otherwise **INSUFFICIENT_DATA**.
- Run: `python -m core.benchmark.context_telemetry_report <workspace>/.ailienant_telemetry.log`
  (reads rotated siblings, skips malformed lines).

### 9.3 Data-validity boundary — discard telemetry before 2026-09-01
Two inputs were skewed in opposite directions before that date: `token_budget` overstated on summarizer
records (window probed at the `"big"` tier while `MODEL_SMALL` serves the call → biased to NO-GO), and
`l5_truncated` overstated on pipeline records (static L4/L5 split → biased to GO). Fixed under DEBT-081
(`_SUMMARY_TIER`, `_split_discretionary`). Collect from a log written after the fix.

### 9.4 Dogfood protocol
1. Run backend + extension, connect a workspace (arms `.ailienant_telemetry.log`).
2. Do ≥10 real **edit-intent coding** tasks, some long and multi-turn. Only edit-intent tasks emit
   `source="summarizer"`; the Natt analyst pane emits only `source="pipeline"`; pure chat emits neither.
3. Aggregate before ~20 MB accrues (sink rotates at 4 × 5 MB).
4. Run the report tool.
5. Record GO / NO-GO below. **GO** → unblock 8.16.1. **NO-GO** → strike 8.16.1–8.16.4 in the manifest
   with `~~struck~~` (spec preserved, not deleted).

> **Verdict:** _not yet recorded._

### 9.5 Reference & limitations
Record: `core/telemetry_log.py::log_context_utilization(...)` under category `CONTEXT` on the existing sink
(`summarizer` records have no layer fields; `pipeline` records have all). `session_start_time` is a new
additive state channel (`SCHEMA_EVOLUTION.MD §33`) because only declared TypedDict fields survive
checkpoints. Summarizer wiring uses a rename-and-wrap (`_run_summarize_node_core` + thin wrapper) so the
tiktoken estimate runs once. Synthetic corpus (`core/benchmark/session_corpus.py`, 160 samples: median
0.093, max 1.43) only proves the instrumentation works — it is not a verdict.
Known limits: `duration_s` is 0.0 on the analyst pipeline path and resets after a server restart on a cold
L1 checkpoint; pipeline `turn_count` is post-eviction L4 chunks, not comparable to the summarizer's
message count. The pipe-delimited log format is DEBT-109 (target: typed JSONL).

---

## 10. Division 8.15 — Dynamic Subagent Dispatch

**Status:** CLOSED 2026-07-04 (8.15.0–8.15.6 + 8.15.0.1 gateway throttle). Feature flag
`AILIENANT_ENABLE_DYNAMIC_DISPATCH`, default off.

### 10.1 Decision
Evaluated LangChain "Dynamic Subagents" (`deepagents`, LLM-written JS in QuickJS calling `task()`).
**Rejected** the package and any JS interpreter: (1) it duplicates our task lifecycle, permission matrix and
VFS layer; (2) executing LLM-authored code is a new untrusted boundary; (3) a JS engine is a heavy,
less-portable dependency (scipy precedent). **Taken instead:** deterministic structural fan-out, keeping raw
subagent output out of the parent context, and six named patterns — built natively as **Pydantic-validated
data, never executed code**. No `eval`/`exec` anywhere.

### 10.2 Contracts (`brain/subagent_contracts.py`, `SCHEMA_EVOLUTION.MD §30`)
- `SubagentResponseSchema` — closed field types (`str/int/float/bool/list_str`), 1–8 fields (no free-form
  JSON schema).
- `SubagentTask` — `task_id` (idempotency key), description ≤4000, `subagent_role` (8 dev roles +
  `analyst_readonly`), `context_refs` (VFS paths only, ≤20), `max_iterations` 1–8. **No permission-mode
  field** — a subagent can never request more than its parent turn.
- `DispatchPlan` — `pattern`, tasks 1–32, `synthesis_instruction` ≤2000, `dispatch_depth` 0–2.
- `SubagentResultEnvelope` — status `ok/error/budget_exhausted/denied`, `raw_digest` capped at
  `shared.config.MAX_OBSERVATION_CHARS` (single source; the leaf module must not import `core.tool_dispatch`).
- `DispatchBatchResult` — results, total cost, optional winner.
- State channels (additive, safe defaults): `dispatch_plan`, `dispatch_batch_result`, `dispatch_depth`,
  `subagent_dispatch_trace` (append-only), `_dispatch_results` (`operator.add` from day one), plus
  `_dispatch_consumed`, `dispatch_round_count`, `dispatch_return_node`, `_dispatch_reserved_usd` (§32).

### 10.3 Mechanics (as shipped, after amendments)
- `brain/dispatch.py::build_dispatch_sends` — separate from `route_to_coders`, which is **never edited** (no
  shared code path with SWARM/RELAY). Increments depth on every fan-out.
- Nodes: `dispatch_origin` = **admission gate** (validate → depth/width deny → single-flight budget
  reserve), routed by a pure string router to `dispatch_fanout` (Send-only) or `dispatch_synthesize`;
  `subagent_worker` (returns an envelope, truncates before constructing it); `dispatch_gate` (fan-in — a
  conditional edge on a fanned-out node would fire once per Send); `dispatch_advance` (new pattern rounds);
  `dispatch_synthesize` (terminal per top-level dispatch, clears `dispatch_plan`).
- **`operator.add` channels cannot be cleared** → a consume watermark `_dispatch_consumed`; synthesis only
  digests `results[consumed:]`. Two origins (`researcher_agent`, `planner_agent`) → `dispatch_return_node`
  routes back (planner → `drift_compute`, researcher → `planner_agent`).
- Waves: concurrency cap `AILIENANT_MAX_CONCURRENT_SUBAGENTS` (default 4) enforced by splitting into
  sequential waves — not a semaphore fighting `Send()`.
- Denial envelopes are bounded to `MAX_DISPATCH_WIDTH` (never one per raw task of an over-scoped plan).
- Tournament: `select_candidate_via_mcts` relocated **verbatim** to `brain/subagent_tournament.py` as
  `run_tournament` with a re-export shim; `run_tournament_from_dispatch` adds an optional
  `candidate_extractor`.
- Budget (`brain/dispatch_ledger.py`): **state-channel arithmetic**, not a file ledger — spend already lives
  in `current_cost_usd` / `max_budget_usd`; a second ledger would double-book. Reuses the reserve → commit
  → floor-at-zero refund discipline only. Estimates are lenient (DEBT-105); `finops`/`check_governor` are
  the hard ceiling.
- Context isolation: per-result cap + per-batch digest scaled to the **parent's** tier
  (LOCAL_SMALL/LOCAL_BIG/CLOUD), greedy whole-chunk packing, winner first.

### 10.4 Patterns
classify-and-act (N=1) · fanout-and-synthesize (primary) · adversarial-verification (producer wave then an
`analyst_readonly` critic) · generate-and-filter and tournament (via the MCTS/UCB1 tournament) ·
loop-until-done (bounded by `check_governor`'s steps/tokens/time).

### 10.5 Security
Depth ≤2 × width ≤32 → `MAX_TOTAL_DISPATCH_FANOUT` = 1024, asserted in the gate; code-level re-check denies
(not truncates); budget is the backstop. String fields rendered as data, never as system messages;
`SecretsScrubber` on every digest. No new permission axis: `analyst_readonly` is floor-locked to READ_ONLY
**automatically** by `evaluate_action` (role binding in `shared/rbac.py::resolve_dispatch_permission`).

### 10.6 Regression guards & MVP debts
R1 SWARM untouched · R2 verbatim tournament relocation · R3 edges wrap, never replace · R4 flag checked at
graph construction · R5 legacy checkpoints deserialize · R6 reducer from day one. Gate audit corrections:
there are 12 dispatch channels (not 4); the real MCTS regression proof is the tournament shim tests;
`dispatch_depth > 2` cannot even be constructed under Pydantic. **Deferred:** DEBT-104 (tournament rollback
never deletes new paths; warn-only), DEBT-105 (lenient cost estimate), DEBT-106 (dev roles run tool-less),
DEBT-107 (no production prompt yet decides when to fan out; mechanism + `AILIENANT_DISPATCH_DEBUG` exist).

---

## 11. Phase 11.11 + 11.12 — Agent Output Quality & Narration Depth

**Status:** CLOSED (11.10 mechanical sweep, 11.11, 11.12).

### 11.1 Why
Two live local-model runs ("build an MVP game from this GDD", then "explain the code in detail") showed
behavioral defects with nothing crashing: empty Thought Box during coding, robotic narration, identifiers
from an unrelated project bleeding into game code, a Django/React scaffold for a game, stub files, and a
terse answer to an explicit "explain in detail".

### 11.2 Corrected root causes (do not "fix" the wrong things)
| Symptom | Wrong attribution | Verified cause |
|---|---|---|
| Terse answers | `fast_path.py` 512-token cap | `fast_path` is not on the question path. `_CHAT_SYSTEM_PROMPT` says "concisely". |
| Shallow plans | `_PLANNER_REASONING_MAX_TOKENS = 512` | That budgets the pre-draft narrative; the WBS draft takes the 4096 default. |
| Cross-project bleed | unbounded tree / unscoped RAG | Both bounded. Real cause: **`project_id` granularity** — one workspace root holding two projects indexes both under one hash. |

**Do not modify:** `brain/fast_path.py`, `_PLANNER_REASONING_MAX_TOKENS`, or the tight budgets: analyst
nightmare/supreme 120, intent-reflect 60, rule-distill 80, summarizer 512, researcher→planner 2048, intent
classifier 20, BYOM health 5, companion 220/420/800, dormant `mcts_coder`.

### 11.3 Decisions
- **A — Narration.** The coder gets **no** `<thinking>` scaffold (strict SEARCH/REPLACE). The companion's
  analysis call is also strict JSON, so a **second free-form pass** in `brain/coder_companion.py` uses
  `LLMGateway.astream_reasoning(..., free_form_answer=True)` into the Thought Box. Contracts untouched.
  Inherits fire-and-forget, semaphore, GC-safe task set, tier timeouts (45 s local / 12 s cloud), terminal
  broadcast on every exit. Gated by budget and GPU-slot checks — **skip, never queue**. Deterministic status
  strings remain as anchors. **Invalidating regression:** if patches stop applying cleanly, revert.
- **C — Budgets.** Coder generation and planner WBS draft budgets scale with task complexity, bounded by the
  real context window. No flat bump. Answer depth is a prompt fix: expansive chat prompt when
  `_EXPLAIN_SIGNALS` match.
- **B — Relevance filter** before injection (no cap raise), overview biased to the task's subtree.
  **Explicit user references always win** (`explicit_mentions`, `active_file_path` never filtered).
- **D / 11.12 — Stack choice.** A hardcoded artifact-class → stack catalog was **rejected** (3–8k tokens per
  call, goes stale yearly, overrides fresher model knowledge, doesn't fix the procedural hole).
  `_STACK_GUIDANCE_DIRECTIVE` became a procedure: classify artifact → name constraints → choose what a
  practitioner would → record as `decisions[0]` in the form `Stack: <names> — <reason>` → every
  `target_file` consistent. Two holes fixed: `MissionSpecification.decisions` was write-only (no agent read
  it), and there are **two** code generators. `MissionSpecification.to_context_block()` (decisions before
  constraints, capped) now feeds **both** `agents/coder.py` (inside the budget-guarded list, tail-truncated)
  and `brain/agentic_cell.py::_build_messages` (re-injected every ReAct iteration).
- **E — Proportional scope.** `_SCOPE_DISCIPLINE_DIRECTIVE` is minimal for narrow requests and adequately
  deep for broad build-outs. The clause "injected context is READ-ONLY reference; seeing a file is never a
  reason to edit it" is kept verbatim.
- **Interactions:** C × E together turn stubs into a real MVP (either alone fails). B and E pull in opposite
  directions; the READ-ONLY clause keeps them compatible.

---

## 12. ADR index

| ADR | Phase | Decision |
|---|---|---|
| 701 | 7.10 | Identity sovereignty, prompt-only |
| 702 | 7.10 | Narration over `server_pipeline_step`; 40 ms token batching; ≤15% narration |
| 703 | 7.10 | Analyst context: uuid sandbox, 4/2/1 KB budget, semantic slice, tolerant divergence |
| 704 | 7.10 | Envelope-tolerant recursive JSON extraction |
| 705 | 7.10 | Security posture (XSS, scrubber, latency as security) |
| 706 | 7.11 | Mesh: inline edits, abort savepoint, rehydration, @mentions, O(1) markdown, native HITL, time-travel |
| 707 | 7.10 amd. | Native thinking on `server_thinking_chunk`, display-only |
| 708–716, 718 | 7.13 | Telemetry bus, reactive index, manual Dreaming, self-healing, telemetry log, mode machine, concurrency spine, stream resilience, orphan recovery, Dual-Rules privacy |
| 720–726 | 7.14 | Zero-bubble, client-side diffs (+ context endpoint), render stack, ghost telemetry, inline HITL, inline revert, context meter + auto-accept |
| 727–732 | 7.15 | Agentic core remediation (route through compiled engine) — no blueprint file |
| 733–739 | 7.16/7.17 | Host tokenization + streaming render — no blueprint file |
| 740–746 | 7.18 | Closed loop, recency heatmap, `response_format` degrade, AST few-shot, response cache, MCTS defer, gate |
| 747–755 | 7.19 | Sandbox session/PTY, sync, ReAct cell, governor, telemetry API, audit widgets, xterm PTY, checklist, gate |
| 756 | 8.3 | Benchmark methodology |
| 757 | 8.4 | MCP SSoT + fail-closed privilege |
| 758 | 8.5 | Single-tool MCP server — superseded |
| 759 | 8.5 | External Capability Gateway (D1–D8) |
| 760 | 8.6 | Phase 8 gate |
| 762 | 8.13 | Devcontainer, split by trust |
