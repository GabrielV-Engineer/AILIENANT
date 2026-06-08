# Tech Debt Backlog — Continuous Registry Protocol

## The Rule

If you discover a strict-mode error, vulnerability, or typing debt **outside the scope of the
current ticket/subfase**, you MUST:

1. **STOP** — do NOT fix it in-place.
2. **ADD an entry** to this file using the format below (reproduction command + file + context).
3. **CONTINUE** with the current task.

This ensures every fix is atomic, auditable, and in the correct topological order. In-place fixes
of out-of-scope debt create invisible changes that break reviewers' ability to verify the diff.

---

## Entry Format

```
### DEBT-NNN — Short description
- **Date:** YYYY-MM-DD
- **Reproduce:** exact shell command that surfaces the error
- **File(s):** affected path(s) and line numbers if known
- **Error:** mypy error code or free-text description
- **Blocked by:** external dependency / phase prerequisite (if any)
- **Phase:** which Phase 8 subfase will address this
- **Notes:** context for future reader
```

---

## Open Entries

### DEBT-019 — api/websocket_manager.py: async request-buffer leak (orphaned late responses)

- **Date:** 2026-06-08
- **Reproduce:** N/A (lifecycle/concurrency gap, not a type or test error). Surfaced when Phase 8.0.6
  typed the two buffers and brought their lifecycle under review.
- **File(s):** `api/websocket_manager.py` — `self._hitl_responses: Dict[str, Dict[str, Any]]` (keyed
  by `approval_id`) and `self._patch_ack_results: Dict[str, Dict[str, Any]]` (keyed by `patch_id`).
- **Lifecycle:** populated when an inbound response/ack arrives (`_hitl_responses[approval_id] = {...}`
  ~line 840; `_patch_ack_results[patch_id] = result` ~line 745); consumed/`pop`ped only by the waiter
  (`wait_*` methods ~lines 796 / 736).
- **Leak (race):** if the waiter has already torn down — `asyncio.wait_for` timeout, task
  cancellation, or the IDE closing / network flicker mid-request — a *late-arriving* response/ack is
  still stored with no consumer left to pop it. `disconnect(client_id)` (~line 184) reaps
  `_inbound_tokens` / `_inbound_refill_at` but **not** these two buffers → orphaned entries accumulate
  `O(H)` (H = interaction requests) over a long-running local IDE server session. Silent memory growth
  that can eventually stall the event loop.
- **Phase:** Dedicated WebSocket-lifetime hardening sub-phase (post-8.0.x). **Behavioral change — out
  of scope for the typing campaign;** recorded per the Continuous Registry Protocol, not fixed in 8.0.6.
- **Proposed fix:** (a) guard the store so a response/ack is only buffered when a waiter is still
  pending (`approval_id in _hitl_pending` / `patch_id in _patch_acks`), dropping orphans; and/or
  (b) maintain a `client_id → {pending approval_ids, patch_ids}` index and reap those buffers inside
  `disconnect()`. Add a regression test that simulates a timed-out/cancelled waiter followed by a late
  resolve and asserts the buffer is empty.

---

### DEBT-018 — brain/memory.py: networkx GraphRAG has no memory bound (heap-overhead risk)

- **Date:** 2026-06-08
- **Reproduce:** N/A (capability/scalability gap, not a type or runtime error). Surfaced when Phase
  8.0.5 unsilenced `brain.memory` and brought the networkx usage under review.
- **File(s):** `brain/memory.py` — the PPR / batch-PPR graph builders that do `import networkx as nx`
  then `nx.DiGraph()` (around lines 108 and 158).
- **Risk:** networkx is pure-Python with a dict-of-dict-of-dict backing store. Spatial complexity is
  `O(V + E)`, but the per-node/per-edge heap overhead on the Python heap is large. In long-lived
  VS Code sessions, a GraphRAG graph that accumulates a node per indexed file with **no eviction or
  teardown** can balloon RAM and stall the asyncio event loop (the IT-director concern raised during
  8.0.5 review).
- **Phase:** Future dedicated phase (post-8.0.x). **Not in scope for the typing campaign** — recorded
  per the Continuous Registry Protocol, not fixed in 8.0.5.
- **Proposed fix:** bound the in-memory graph — e.g. an LRU eviction policy on the node set, a hard
  cap on subgraph size, and/or explicit `G.clear()` / teardown on session end. Measure heap growth
  across a long session first to size the cap.

---

### DEBT-001 — tools.patch_tool: LangChain @tool decorator stub mismatch — ✅ CLOSED 2026-06-05

- **Date:** 2026-05-31
- **File:** `tools/patch_tool.py:219`
- **Error:** `unused-ignore[misc]` — `# type: ignore[misc]` was added when LangChain's `@tool`
  decorator lacked stubs.
- **Resolution (2026-06-05, Phase 8.0.1):** **CLOSED — stubs landed.** `mypy --strict
  tools/patch_tool.py` confirmed the ignore is now *unused* (upstream langchain-core stubs caught
  up). Removed the `# type: ignore[misc]` comment and the `[mypy-tools.patch_tool] follow_imports =
  silent` block together. `mypy --strict tools/patch_tool.py` → 0; `mypy .` → 0/247.

---

### DEBT-002 — agents/contract_guard.py: MODEL_MEDIUM not explicitly exported

- **Date:** 2026-05-31
- **Reproduce:** `cd ailienant-core && .\venv\Scripts\python -m mypy --strict agents/contract_guard.py`
- **File:** `agents/contract_guard.py:100`
- **Error:** `attr-defined` — `Module "tools.llm_gateway" does not explicitly export attribute
  "MODEL_MEDIUM"`
- **Blocked by:** `tools.llm_gateway` silenced (`follow_imports = silent`). The error is invisible
  under the current `mypy .` gate but surfaces under `--strict`.
- **Phase:** 8.2 (resolves automatically when llm_gateway is unsilenced, IF `MODEL_MEDIUM` is
  added to `__all__` or the import is moved to `shared/config.py`).
- **Notes:** `MODEL_MEDIUM` is defined in `tools/llm_gateway.py` but is not in `__all__`. Two
  remediation options: (a) add `MODEL_MEDIUM` (and `MODEL_SMALL`, `MODEL_BIG`) to `__all__` in
  llm_gateway.py; (b) move the constants to `shared/config.py` (where they may semantically
  belong) and update all callers. Option (b) is cleaner long-term.

---

### DEBT-003 — brain/swarms.py: BaseCheckpointSaver missing type args — ✅ CLOSED 2026-06-05

- **Date:** 2026-05-31
- **Reproduce:** `cd ailienant-core && .\venv\Scripts\python -m mypy --strict brain/swarms.py`
- **File:** `brain/swarms.py:189`
- **Error:** `type-arg` — `Missing type arguments for generic type "BaseCheckpointSaver"`
- **Resolution:** Fixed in Phase 8.0.0. Changed `Optional[BaseCheckpointSaver]` to
  `Optional[BaseCheckpointSaver[Any]]` in `brain/swarms.py:189`.

---

### DEBT-004 — brain/swarms.py: stale unused-ignore comments — ✅ CLOSED 2026-06-05

- **Date:** 2026-05-31
- **Reproduce:** `cd ailienant-core && .\venv\Scripts\python -m mypy --strict brain/swarms.py`
- **File:** `brain/swarms.py:226` (and 7 other lines)
- **Error:** `unused-ignore` — stale `# type: ignore[type-var]` / `# type: ignore[arg-type]`
  comments removed (stubs improved). 4 targeted node registration ignores retained where the
  underlying `NodeInputT` constraint is a real Tier 2/3 issue (DEBT-014 will fix those).
- **Resolution:** Fixed in Phase 8.0.0. Removed 8 stale ignores; 4 minimal targeted ignores
  retained for coder/planner/analyst/tool_rag_select node registrations (Tier 2/3 root cause).

---

### DEBT-005 — Multiple brain/ + agents/ interior nodes: unknown strict debt

- **Date:** 2026-05-31
- **Reproduce:** `cd ailienant-core && .\venv\Scripts\python -m mypy --strict brain/engine.py 2>&1 | head -60`
- **Files:** `brain/engine.py`, `brain/ideation.py`, `brain/guardrails.py`,
  `brain/intent_router.py`, `agents/coder.py`
- **Error:** Various — `type-arg`, `no-any-return`, `no-untyped-def` (full count hidden behind
  silenced transitive deps; cannot be measured accurately until Phases 8.1–8.5 remove the wall)
- **Blocked by:** Silenced modules (`tools.llm_gateway`, `agents.analyst`, `core.db`, etc.)
  masking the true error count.
- **Phase:** 8.4 and 8.7 (assess after each unsilencing step)
- **Notes:** Do NOT attempt to fix these preemptively. Run an exploratory `mypy --strict` after
  each Phase 8.1–8.5 step to update this entry's error count. The topological order guarantees
  these errors only become actionable after their upstream deps are clean.

---

### DEBT-006 — Inline diff has no syntax highlighting (shiki deferred)

> **Note on numbering:** this is the item the 7.14.2 directive referred to as "DEBT-003"; that id was
> already taken by the `brain/swarms.py` entry above, so it is filed here as DEBT-006. Treat
> "DEBT-003 (shiki)" as an alias for this entry.

- **Date:** 2026-06-01
- **Reproduce:** `cd ailienant-extension && node esbuild.js --production; (Get-Item dist/workspace.js).Length`
  (with shiki re-added inline, the bundle exceeds the 550 KB ceiling).
- **File(s):** `src/workspace/components/DiffBlock.tsx` (diff cells render as themed monospace, no
  token spans).
- **Error:** Feature gap, not a type error. The Elite Diff Engine ships without VS Code-identical
  syntax tokenization. Measured: shiki's JS regex engine (~160 KB minified) + the smallest usable
  grammar `tsx` (~172 KB) exceed the bundle headroom; `react-diff-viewer-continued` alone already
  brings the base to ~537 KB.
- **Blocked by:** The IIFE webview bundle cannot lazily code-split (esbuild `iife` has no
  `splitting`). Reintroducing shiki requires runtime asset externalization (Mechanism A:
  `external:[shiki]` + ship assets to `media/` + `webview.asWebviewUri` loader) or a worker — both
  deferred as out-of-scope CSP/plumbing risk for 7.14.2.
- **Phase:** A future 7.14.x or Phase 11 polish slice.
- **Resolution (2026-06-05):** **CLOSED — token layer shipped & gated.** Phase 7.16 moved the grammar
  engine to the host (7.16.1) and the webview now paints the host AST as scope-colored `<span>`s —
  diffs (`DiffBlock.tsx` per-line `renderContent`) and chat code blocks (`MarkdownRenderer.tsx` via the
  stream-end tokenize round-trip), styled only with `--vscode-*` CSS vars (`scopeColor.ts`). The
  webview gained **zero** grammar deps and `dist/workspace.js` stayed under the 550 KB ceiling
  (548.2 KB). The **7.16.3 checkpoint gate** (`src/test/phase7_16_checkpoint_gate.test.ts`, 10/10)
  asserts the ceiling held + no webview leak + engine host-side + scope→CSS-var theme-flip +
  highlighting renders; `esbuild.js::assertWebviewBundleUnderCeiling()` makes the ceiling a permanent
  build gate. Spawned **DEBT-012** (the `disableWordDiff` trade-off).
- **Notes:** The dormant shiki contract (no-WASM JS engine, fine-grained core, lazy-load) is preserved
  in `docs/PHASE_7_14_0_STACK_CONTRACT.md` §3 for whenever this is picked up. ADR-722's *theming*
  half is already honored — diff colors bind to `--vscode-diffEditor-*` CSS vars today; only the
  token layer is missing.
- **Audit cross-ref:** the Phase 7.15 pre-checkpoint audit's "code blocks render as plain white text"
  complaint maps to THIS entry — it is the known, deliberate shiki deferral, not a new defect. The 7.15
  remediation does **not** re-open it (see Fase 7.15 in `PROJECT_MANIFEST.md`); highlighting stays here
  until the bundle/externalization plumbing is funded.
- **Funded by:** **Phase 7.16** (Host-Delegated Tokenization) is the remediation — it takes the
  host-side escape hatch this entry already names: a grammar engine (shiki/textmate) runs in the **Node
  Extension Host** (no bundle ceiling) and ships a token AST over IPC to the dumb webview, so
  `dist/workspace.js` gains no parsing deps. This entry moves to **Closed** when **7.16.2** ships the
  webview AST renderer. The *streaming* half (progressive highlight without flicker) is owned by
  **Phase 7.17**, not this entry.

### DEBT-007 — Auto-accept low-risk edits pays a full HITL round-trip (shift-left candidate)

- **Date:** 2026-06-02
- **Reproduce:** N/A (latency, not an error). With auto-accept ON, every low-risk approval still flows
  backend → WS `server_hitl_approval_request` → webview → `HITL_RESPONSE` → host → WS
  `client_hitl_response` → backend before the edit applies.
- **File(s):** `ailienant-extension/src/workspace/Workspace.tsx` (auto-accept gate in the
  `server_hitl_approval_request` handler); `ailienant-extension/src/workspace/workspaceStore.ts`
  (`autoAcceptLowRisk`).
- **Error:** Per-step network RTT for actions the user pre-authorized. `O(1)` per step but one full
  round-trip each — avoidable.
- **Blocked by:** A client→host→backend channel that carries the auto-accept preference (none exists
  today; the setting is webview-local). Adding it is out of scope for the UI slice that introduced the
  toggle.
- **Phase:** A future shift-left optimization (Phase 11 or a later 7.14.x): the backend reads the
  auto-accept setting and, for low-risk edits, **omits emitting the approval event altogether** — the
  edit applies server-side with no round-trip. Trade-off accepted now for implementation simplicity
  (reuse `useHitlResponder`'s wire message, zero new Python logic).
- **Notes:** The conservative risk gate (any medium/high metric forces the manual card) must be
  preserved if/when this moves server-side.

### DEBT-008 — Coding turns stream node-level narration, not LLM tokens (graph agents don't stream)

- **Date:** 2026-06-02
- **Reproduce:** N/A (UX/perception, not an error). After the Engine Re-Spine, a coding turn drives the
  compiled graph via `astream(stream_mode="values")`; live progress is **node-level narration**
  (`NarrationGate` + `broadcast_pipeline_step`), but the proposed-diff summary still arrives in one
  block — the chat path (`_stream_chat_answer`) streams token-by-token, the coding path does not.
- **File(s):** `ailienant-core/agents/planner.py` (`run_planner_node`), `ailienant-core/agents/coder.py`
  (`run_coder_node`) — both `ainvoke` the model and return complete results; `ailienant-core/core/task_service.py`
  (`_run_coding_task` consumes the final graph state).
- **Error:** Feature gap, not a type error. True token-by-token streaming from inside the graph would
  require the agent nodes themselves to stream their LLM deltas, which they do not.
- **Blocked by:** Nothing technical — it is a **deliberate scope cut** of the Re-Spine to keep that
  change foundational and low-risk, and to avoid touching every agent + the gateway in the same PR
  (event-loop / regression risk).
- **Phase:** Owned by **Phase 7.17** (WBS **7.17.0-B**, ADR-739). **CLOSED 2026-06-05** — but the
  resolution is *thinking*, not the answer tokens: `stream_mode="messages"` was rejected (the nodes use
  the LiteLLM gateway directly, not a LangChain chat model, so that mode captures nothing). Instead the
  nodes stream the model's **native reasoning** to the Thought Box during inference via a dedicated
  `config.configurable["stream_thinking"]` sink (twin of the `narrate` seam), through the new
  `LLMGateway.acomplete_with_thinking`; the structured JSON answer is still buffered → parsed → diffed.
  So the *freeze* is gone (reasoning fills the gap) while the answer diff remains a single block by
  design — structured JSON can't be shown token-by-token usefully. **Distinct from DEBT-006** (frontend
  syntax-highlighting). The residual (true answer-token streaming, blocked by the `response_format`
  drop) is tracked as **DEBT-013**.
- **Notes:** The `NarrationGate` is honored trivially — thinking rides `server_thinking_chunk`, a
  different channel than `server_pipeline_step`, so it never charges the gate. FastAPI event-loop
  protection is honored by the `_ThinkingStreamer` 60 ms coalescer (no WS frame per token).

### DEBT-013 — Thinking-stream coding turns drop hard JSON-mode (`response_format`)

- **Date:** 2026-06-05
- **Reproduce:** N/A (reliability trade-off, not an error). On a reasoning-capable model with native
  thinking ON, `acomplete_with_thinking` takes the streaming branch, which **cannot** pass
  `response_format={"type":"json_object"}` (no `astream*` gateway method supports it). The planner/coder
  answer is therefore prompt-enforced JSON recovered via `_sanitize_json_response`, not provider-enforced
  JSON-mode.
- **File(s):** `ailienant-core/tools/llm_gateway.py` (`acomplete_with_thinking` streaming branch),
  `ailienant-core/agents/coder.py` / `agents/planner.py` (callers).
- **Error:** Not a defect — a **declared trade-off per CLAUDE.md §7.** Blast-radius is bounded: only
  thinking-capable + thinking-ON turns take this path; every other turn keeps the exact
  `ainvoke(response_format=json)` call (zero regression). Residual risk: marginally higher parse-failure
  odds on those turns, already absorbed as **soft errors** (planner actor-critic retry; coder
  step-failed → `error_correction`), and the fence-strip + the 7.18.2/ADR-742 adaptive sanitizer
  recover the JSON.
- **Blocked by:** Nothing technical — a deliberate scope cut to keep 7.17.0-B low-risk.
- **Phase:** Spawned by **7.17.0-B (ADR-739)**. Enterprise refactor candidate: a gateway path that
  streams reasoning **and** keeps `response_format` for providers that support streaming structured
  outputs (e.g. OpenAI), falling back to the sanitizer only where unsupported.
- **Notes:** Re-open if telemetry shows a material rise in planner/coder parse failures on reasoning
  models; otherwise the soft-error handling makes this low-priority.

### DEBT-014 — brain/swarms.py: NodeInputT add_node type-var — ⚠️ REDUCED (3 residual ignores) 2026-06-08

- **Date:** 2026-06-05
- **Root cause:** LangGraph's `add_node` binds `NodeInputT` with `bound=StateLike`
  (`TypedDictLikeV1 | TypedDictLikeV2 | DataclassLike | BaseModel`, per
  `langgraph/typing.py:45`). A node function typed `(state: Dict[str, Any]) -> ...` infers
  `NodeInputT = dict[str, Any]`, which is **not** a TypedDict and violates the bound →
  `type-var` error at the `add_node` call site.
- **Partial resolution (2026-06-08, Phase 8.0.4):** `tool_rag_select_node` (the node defined
  locally in `swarms.py`) was retyped `(state: AIlienantGraphState)`. `AIlienantGraphState` IS a
  TypedDict → satisfies the bound → `swarms.py:155` no longer needs an ignore (removed). This
  closed the strict/non-strict discrepancy that was the last `mypy --strict main.py` residual.
  **`mypy --strict main.py` → 0 as of 8.0.4.**
- **Residual (3 ignores still required):** `swarms.py:156` (`run_coder_node`), `:218`
  (`run_planner_node`), `:227` (`run_analyst_node`), and `ideation.py:215` (`run_analyst_node`)
  retain `# type: ignore[type-var]`. These are **USED** (suppress real errors) under both `mypy .`
  and `mypy --strict` — they cause no `unused-ignore`, so all gates stay green.
- **Why not fixed:** two approaches were tried and rejected in 8.0.4:
  1. **Retype signatures to `AIlienantGraphState`** — cascades to **63 `arg-type` errors across 19
     files**: every direct caller (production `agents/logic.py:27` + ~18 test files) passes a plain
     `dict`, which is not assignable to a TypedDict param. Too invasive; churns the test suite.
  2. **`input_schema=AIlienantGraphState` on the `add_node` call** — mypy reports `Cannot infer
     value of type parameter "NodeInputT"` because it cannot reconcile a `Dict[str, Any]`-typed
     action with `StateNode[AIlienantGraphState, ...]`. Does not work.
- **Proposed enterprise refactor (deferred):** when the agent-node call sites are themselves
  hardened (a dedicated phase), retype `run_coder_node` / `run_planner_node` / `run_analyst_node`
  to `AIlienantGraphState` **and** migrate all ~19 direct callers (tests + `logic.py`) to construct
  a typed state (or a small `cast` helper). Alternatively, adopt it when LangGraph ships a stub that
  accepts `Mapping[str, Any]` for `NodeInputT`. Until then the 3 ignores are the correct, minimal,
  gate-green suppression.
- **Notes:** The enforced gate (`mypy .`) and the campaign gate (`mypy --strict main.py`) are both
  **0** with these ignores in place. This is no longer a strict-gate blocker — only a code-cleanliness
  residual.

---

### DEBT-015 — agents/contract_guard.py: MODEL_MEDIUM import — ✅ CLOSED 2026-06-05

- **Date:** 2026-06-05
- **File(s):** `agents/contract_guard.py:100`
- **Error:** `attr-defined` — `Module "tools.llm_gateway" does not explicitly export attribute
  "MODEL_MEDIUM"`. `MODEL_MEDIUM` is defined in `shared.config` and imported into `llm_gateway`
  without an explicit re-export; `contract_guard` was pulling it from `llm_gateway` instead of
  the canonical source.
- **Resolution (2026-06-05, Phase 8.0.2):** Changed the deferred import in `contract_guard.py:100`
  to `from shared.config import MODEL_MEDIUM` directly. `mypy --strict agents/contract_guard.py` → 0.

---

### DEBT-016 — brain/summarizer.py: strict-mode type-arg — ✅ CLOSED 2026-06-05

- **Date:** 2026-06-05
- **File(s):** `brain/summarizer.py:34` (bare `dict`).
- **Error:** `type-arg` × 1 (`run_summarize_node` signature: `state: dict`).
- **Partial resolution (2026-06-05, Phase 8.0.1):** `brain/ideation.py` portion (8 × type-arg) was
  self-contained (not gated by `agents.analyst`) — fixed in 8.0.1. Only `summarizer.py` remained.
- **Resolution (2026-06-05, Phase 8.0.2):** **CLOSED.** `run_summarize_node(state: Dict[str, Any])
  -> Dict[str, Any]` + added `Any` to the `typing` import. `mypy --strict brain/summarizer.py` → 0.

---

### DEBT-009 — MCTS variant-search is offline-only (not wired into the live coder loop)

- **Date:** 2026-06-03
- **Reproduce:** N/A (capability gap, not an error). `brain/mcts/` (UCB1 tree, reward eval) + `agents/mcts_coder.py` exist and run only in the parallel dreaming daemon; the live Phase-4 LangGraph coder loop (`run_coder_node`) is single-shot-per-step and never enters MCTS.
- **File(s):** `brain/mcts/tree.py`, `agents/mcts_coder.py`, `brain/engine.py` (no edge into the MCTS search from the live path).
- **Error:** Feature gap. Surfacing UCB1 variant-search inline would multiply LLM calls per step, collide with the 7.18.0 correction budgets, and risk latency/cost regression on the exact loop 7.18.0 makes load-bearing.
- **Blocked by:** **Precondition = Phase 7.18.0 — now SHIPPED (2026-06-04).** MCTS needs a per-variant reward signal; the *structured test/typecheck verdict* introduced by 7.18.0 (`tools/validation/diagnostics.py` → a `healing_required` delta carrying compact `[file,line,code,msg]` diagnostics) is exactly that signal and is now available. The precondition is satisfied; MCTS-live remains deferred by choice (highest risk, lowest marginal value) — it should only be attempted after 7.18.0 *stabilizes* in real turns, and never coupled onto the same edge as another risky change.
- **Phase:** Deferred by **7.18.5 (ADR-745)**. If pursued, scope as a follow-up phase gated behind 7.18.0's green checkpoint; the 7.18.6 gate row **MCTS-DEFER** pins the offline boundary (no live-loop import edge into `brain/mcts`) so an accidental wiring trips the gate.
- **Notes:** Highest risk, lowest marginal value of the six audited techniques. Not a defect — a deliberate sequencing decision.

### DEBT-010 — OCC version-vectors on the graph state dict: rejected in favor of existing reducers (decision record)

- **Date:** 2026-06-03
- **Reproduce:** N/A (architecture decision, not an error). Architect upgrade #5 requested strict version-vector OCC on the LangGraph state dict (reject-and-retry, idempotent nodes).
- **File(s):** `brain/state.py:241-289` (per-file `document_version_id` OCC), `brain/state.py:458-459` (LangGraph reducers: `operator.add`, last-writer-wins `merge`); `agents/coder.py` (emitted `base_hash` stale-guard).
- **Error:** Not a defect — a **conflict surfaced per CLAUDE.md §3.** OCC already exists at the file granularity that governs mutation safety, and the graph state is managed by **reducers** that *merge* the concurrent `Send()` fan-out a version-vector model would *abort* — opposite strategies for the same contention. A parallel OCC layer would either duplicate the guarantee or serialize (break) the SWARM fan-out.
- **Blocked by:** N/A — **resolved as Option A (Pivot):** the intent (zero state-corruption under concurrency) is treated as already satisfied; the 7.18.6 gate row **OCC1** *asserts* the existing reducer + `base_hash` guarantee rather than adding a mechanism.
- **Phase:** Decision recorded under **7.18 (ADR-746)**. Re-open **only** if a demonstrated corruption bug proves reducers insufficient (then Option B targeted idempotency for async MCP writes, or — last resort — Option C full version-vector re-spine).
- **Notes:** A genuine future risk this entry flags: once 7.18 wires execute-tier dispatch, **async MCP tool calls** mutating state mid-node could warrant Option B (targeted execute-tier write idempotency) — a small hardening, not a global OCC rewrite.

### DEBT-011 — test_v3_tracemalloc heap-baseline ceiling is structurally broken (red in the Phase 3.7 gate)

- **Date:** 2026-06-04
- **Reproduce:** `cd ailienant-core && .\venv\Scripts\python -m pytest tests/test_phase3_checkpoint_gate.py::test_v3_tracemalloc_50_node_lifecycle_returns_to_baseline -q`
- **File(s):** `tests/test_phase3_checkpoint_gate.py:418-452` (`test_v3_tracemalloc_50_node_lifecycle_returns_to_baseline`).
- **Error:** Pre-existing test-design defect, not a product bug. The test takes the `tracemalloc`
  baseline snapshot **immediately after** `tracemalloc.start()`, so `baseline_bytes ≈ 0`; the ceiling
  `int(baseline_bytes * 0.05) + 65_536` therefore collapses to a fixed ~64 KB. Allocating 50
  `MCTSNode` + 50 `MissionSpecification` (Pydantic v2) objects retains ~210-240 KB (validator/core-schema
  caches + node dict), so the assertion `delta_bytes < ceiling` always fails (observed 212-237 KB).
  The `prune_branch` + `del tree` cleanup does not (and cannot) reclaim Pydantic's process-wide schema
  caches, so the "returns to baseline" premise is unmeetable as written.
- **Blocked by:** None — fixable now, but **out of scope for 7.18.1** (recency); deliberately deferred
  per the Continuous Registry Protocol rather than fixed in-place. Verified failing on the stashed
  pre-7.18.1 tree, so it is **not** a regression from the recency work.
- **Phase:** A future test-hardening slice (Phase 8 / gate maintenance).
- **Notes:** Remediation options: (a) call `gc.collect()` before *both* snapshots and warm Pydantic by
  constructing one `MissionSpecification` before the baseline so its schema cache is already resident;
  (b) replace the baseline-relative ceiling with a fixed absolute budget sized for 50 nodes (e.g.
  ~512 KB) since the real claim is "bounded growth," not "returns to zero"; (c) measure only the
  `MCTSTree`/RAM-VFS delta via `snapshot.filter_traces` on `brain/mcts/tree.py` + `core/vfs_middleware.py`
  rather than the whole-process `"filename"` sum. Option (b)+(c) together best preserve the original
  intent (heap-leak guard on the MCTS lifecycle) without the brittle near-zero baseline.

### DEBT-012 — diff highlighting disables word-level diff (no intra-line token slicing)

- **Date:** 2026-06-05
- **Reproduce:** Apply an edit that changes part of a line; the line shows full-line syntax color but
  no word-level add/remove shading (the per-word green/red highlight).
- **File(s):** `src/workspace/components/DiffBlock.tsx` (`disableWordDiff={true}` + the per-line
  `renderContent` content→tokens map).
- **Error:** Not a defect — a **declared trade-off (CLAUDE.md §7.2)** taken to ship 7.16.2 syntax
  highlighting. `react-diff-viewer-continued` calls `renderContent(source)` per *word fragment* when
  word-diff is on, which would break the per-line token mapping (the host emits tokens row-aligned to
  full lines, not fragments). Disabling word-diff yields clean full-line syntax color at the cost of
  intra-line word shading. Line-level add/remove backgrounds (the `--vscode-diffEditor-*` palette) are
  unaffected, so the diff still reads correctly.
- **Blocked by:** None — needs a word-diff-aware token slicer: intersect the viewer's per-fragment
  `DiffInformation` offsets with the line's `ASTToken` runs so each fragment carries only its slice of
  scopes. Non-trivial offset math; out of scope for 7.16.2 (static highlight first).
- **Phase:** A future 7.16.x / 7.17 polish slice (best folded into the 7.17 streaming-render work,
  which already owns the token-reconciliation path).
- **Notes:** Alternative: keep word-diff and run a second, fragment-level tokenization pass keyed by
  `(line, fragmentRange)` — heavier; the offset-intersection approach reuses the existing host AST.

---

## Closed Entries

*(Move entries here when their Phase has been executed and verified.)*

- **DEBT-006 — Inline diff / chat code had no syntax highlighting (shiki deferred)** — **CLOSED 2026-06-05**
  by Phase 7.16 (host-delegated tokenization). Engine moved to the host; webview paints scope-colored
  spans with `--vscode-*` CSS vars and zero grammar deps; `dist/workspace.js` 548.2 KB < 550 KB ceiling.
  Verified by the 7.16.3 checkpoint gate (10/10) + a permanent esbuild ceiling guard. Full record under
  the DEBT-006 entry above. Spawned DEBT-012 (`disableWordDiff` trade-off, still open).

---

## Appendix: Reproduction Quick-Reference

```powershell
# Run from: C:\Proyectos\Proyect_Ailienant\ailienant-core

# DEBT-001
.\venv\Scripts\python -m mypy --strict tools/patch_tool.py

# DEBT-002
.\venv\Scripts\python -m mypy --strict agents/contract_guard.py

# DEBT-003 + DEBT-004
.\venv\Scripts\python -m mypy --strict brain/swarms.py

# DEBT-005 (exploratory — count changes over time)
.\venv\Scripts\python -m mypy --strict brain/engine.py 2>&1 | grep "error:" | wc -l

# DEBT-011 (pre-existing red gate test — heap baseline ceiling)
.\venv\Scripts\python -m pytest tests/test_phase3_checkpoint_gate.py::test_v3_tracemalloc_50_node_lifecycle_returns_to_baseline -q
```
