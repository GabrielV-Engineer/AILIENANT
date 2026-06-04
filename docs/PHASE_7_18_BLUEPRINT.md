# PHASE 7.18 — Master Architectural Blueprint (Six-Technique Enterprise Hardening Sweep)

> **Mandatory read** during every Phase 7.18 task. Survives session compactions: re-derive intent from this document. Any deviation from the binding decisions tagged **[ADR-74x]** requires an explicit blueprint amendment in the same PR.
>
> **Backend track.** 7.18 lives in `ailienant-core/` and **does** touch the Python contract (unlike the frontend-only 7.14/7.16 tracks — it is the correct posture for a capability-hardening track). It is the **next backend phase, sequenced BEFORE 7.16.1** begins; the 7.16/7.17 frontend/host track is orthogonal and can proceed in parallel on the extension.

## Context

A Systems-Architect review asked whether AILIENANT actually applies the six techniques that let Cursor / Claude Code / Codex behave like senior engineers — System Prompt, Contextual RAG, Chain-of-Thought, Few-Shot, Tool Use / Function Calling, Feedback Loop — and whether a new phase is warranted before 7.16.1.

A grounded audit (three parallel exploration sweeps + direct file reads, per CLAUDE.md §3 Strategic Auditor) established that **the project is already far more mature than a "still an MVP" brief assumes**: **five of the six techniques are STRONG and wired in production.** Therefore 7.18 is scoped as a **targeted hardening sweep — one headline gap + four small/medium closes + one defer decision** — never a rebuild of mature systems (that is pure regression risk). The governing rule is **reuse over rebuild**: the hard parts (sandbox tiers, execution tools, the self-heal machinery, retry budgets, the tree-sitter AST engine, the reducer-based OCC) already exist; net-new code is confined to the wiring.

### The audit (current state vs. brief premise — verified in code)

| # | Technique | Verdict | The real 7.18 work |
|---|---|---|---|
| 1 | **System Prompt** | ✅ STRONG | Per-agent personas + output contracts + XML-UUID injection sandbox ([prompts.py](../ailienant-core/agents/prompts.py)); reusable directive constants (`LANGUAGE_MIRROR_DIRECTIVE`, `_GRILL_DIRECTIVE`, `ROLE_REGISTRY` 8 roles); SOUL hot-reload; dynamic `{css}`/`planning_strategy` injection. **No work — out of scope.** |
| 2 | **Contextual RAG** | ✅ STRONG (1 dead term) | LanceDB + SQLite dep-graph + PPR, hybrid CSS `(0.5·Sem)+(0.3·Graph)+(0.2·Time)`, active-file/dirty-buffer VFS, multi-tier token budgets, semantic slicing. **Gap:** recency is a hardcoded `0.5` placeholder ([planner.py:332](../ailienant-core/agents/planner.py#L332)) → the third CSS factor is dead weight. **7.18.1.** |
| 3 | **Chain-of-Thought** | ✅ STRONG | Socratic ideation→synthesis→planner Actor-Critic (`MAX_PLANNER_RETRIES`, P(E)³), narrated `critic_review`/`critic_rejected`/`plan_validated`, native thinking (ADR-707). **No work — out of scope.** |
| 4 | **Tool Use** | ✅ STRONG (1 fragility) | Pydantic `MissionSpecification`/`WBSStep` + `response_format=json_object` + 3-layer JSON repair, Tool RAG, atomic patch (fuzzy+AST+OCC). **Gap:** `response_format` sent unconditionally ([llm_gateway.py:374](../ailienant-core/tools/llm_gateway.py#L374),[:459](../ailienant-core/tools/llm_gateway.py#L459)) — local backends may 400. **7.18.2.** |
| 5 | **Few-Shot** | 🟡 PARTIAL | Format exemplars, GraphRAG snippet injection ([coder.py:41](../ailienant-core/agents/coder.py#L41)), episodic top-3 trajectory memory in the planner prompt. **Gap:** no **code-STYLE** exemplars — RAG injects topology, not framed house-style patterns. **7.18.3.** |
| 6 | **Feedback Loop** | 🟡 STRONG-but-OPEN (headline) | Exception-driven self-heal fully wired (`reflexion_guard`→`error_correction`→HITL→re-inject, failure-signature breaker, DLQ). Sandboxed EXECUTE-tier tools exist AND route through `core.sandbox.get_active_adapter()` ([execution_tools.py:13](../ailienant-core/tools/execution_tools.py#L13)). **Gap:** the autonomous coder loop never consumes them — a `run_command` WBS step dead-ends as `EXECUTE_TIER_DEFERRED` ([coder.py:133-160](../ailienant-core/agents/coder.py#L133)). **No closed loop of: write → run tests/typecheck in sandbox → capture → re-inject → re-draft.** *This is the single thing separating AILIENANT from Cursor/Claude Code.* **7.18.0 (headline).** |

### Architect's review (5 upgrades) — folded after verifying each against the code

| # | Upgrade | Verdict after code-check | Lands |
|---|---|---|---|
| 1 | **Structured error parsing** (`[file,line,code,msg]`, not raw stdout; O(N) local, avoids O(T²) attention blowup) | ✅ SOUND + **partially exists** — `ValidationError`/`ValidationResult` ([result.py](../ailienant-core/tools/validation/result.py)); `lsp_filter` already runs ruff/eslint `--format json`. Reuse, don't invent a format. | 7.18.0 |
| 2 | **Session heatmap recency** = `0.7·time_decay + 0.3·access_frequency` (in-session counter, O(1)) | ✅ SOUND, net-new. Better than pure mtime on legacy code. | 7.18.1 |
| 3 | **AST-skeleton few-shot** (signature + type hints + docstring, body→`...`; >70% token cut) | ✅ SOUND + **reuse the engine** — [`ast_engine.py`](../ailienant-core/core/ast_engine.py) is tree-sitter (20+ langs, content-hash cache), **not** stdlib `ast` (Python-only). | 7.18.3 |
| 4 | **AST-hashed semantic LLM cache** (O(1) lookup vs O(N) network; ~40% API-cost cut) | ✅ SOUND, net-new but **half-built** — `ASTEngine` (ast_engine.py:113-153) is already a blake2b content-hash tree cache. Extend from caching **trees** to caching **responses**. | 7.18.4 |
| 5 | **OCC version-vectors on the LangGraph state dict** (reject-and-retry) | ⚠️ **CONFLICT** — OCC already exists (`document_version_id` per file, state.py:241-289) **and** the graph uses **reducers** (`operator.add`, last-writer-wins `merge`, state.py:458-459) that **merge** the concurrent fan-out a version-vector model would **abort**. Opposite strategies. **Surfaced as §3 conflict — not silently built.** | §3 (below) |

### Grounding (existing infrastructure to reuse, verified)

- **Sandbox** — `core/sandbox.py` ships Docker/Wasm/NativeHITL tiers (Phase 6.1) bound to `ACTIVE_ADAPTER` at lifespan startup; `get_active_adapter()` is the read seam.
- **Execute-tier tools** — `tools/execution_tools.py` (Phase 6.2) already routes `sandbox_bash`/`check_type_integrity` through the active adapter, with HITL friction + `DANGEROUS_COMMANDS_REGEX` + `gate_execute_action`.
- **Structured diagnostics** — `tools/validation/result.py` (`ValidationError{layer,line,column,message}`/`ValidationResult`) + `tools/validation/lsp_filter.py` (ruff/eslint `--format json`, graceful degradation).
- **Self-heal machinery** — `reflexion_guard` + `route_after_coder` + `run_error_correction_node` ([engine.py](../ailienant-core/brain/engine.py)), bounded by `CORRECTION_MAX_ATTEMPTS`/`FAILURE_SIGNATURE_THRESHOLD` ([retry_policy.py](../ailienant-core/brain/retry_policy.py)) + the process-wide `failure_breaker`; DLQ persistence in `core/dead_letter.py`.
- **AST engine** — `core/ast_engine.py` `ASTEngine`: tree-sitter, 20+ languages, blake2b content-hash-keyed tree cache with `parse`/`get`/`invalidate`.
- **RAG retrieval** — `SemanticMemoryManager().search_snippets(...)` returns `(file_path, snippet)` pairs with an `indexed_at` ISO field in the LanceDB schema; the coder already calls it in `_build_rag_block` ([coder.py:41](../ailienant-core/agents/coder.py#L41)).
- **JSON repair** — `LLMGateway._sanitize_json_response` / `_extract_nested_schema_target`: the proven floor for local backends.
- **OCC** — per-file `document_version_id` ([state.py:241-289](../ailienant-core/brain/state.py#L241)) + the coder's emitted `base_hash` stale-guard + LangGraph reducers for concurrent fan-out.

This blueprint is the contract that future 7.18 PRs must conform to.

---

## 1. Scope boundary (what 7.18 owns)

| Owns | Does NOT own |
|---|---|
| Closing the **Feedback** loop: dispatch `run_command`/post-edit verify into the existing sandbox, parse structured diagnostics, re-inject through the existing self-heal path. | The self-heal machinery itself (reuse, never re-implement); the sandbox adapters; the execution tools. |
| **RAG** recency upgrade (hybrid time+frequency). | The CSS formula weights or the `ContextMeter` schema (unchanged). |
| **Tool Use** `response_format` degradation. | The 3-layer JSON repair (reuse as the fallback floor). |
| **Few-Shot** AST-skeleton style exemplars. | The tree-sitter engine (reuse) or the topology RAG block (kept distinct). |
| **Semantic response cache** atop the existing AST-hash primitive. | A new cache subsystem (extend `ASTEngine`, don't fork it). |
| The **defer decision** for MCTS-live + the **§3 OCC conflict** resolution. | Wiring MCTS into the live loop; any global version-vector OCC re-spine. |
| Techniques 1 (System Prompt) & 3 (Chain-of-Thought) — explicitly **out of scope** (already STRONG; do not "improve" working machinery under cover of this sweep). | |

**Timeless code (CLAUDE.md):** no phase numbers / milestone tags in source comments. Bounded/defensive throughout (OOM caps on the cache, context-window caps on exemplars, cp1252-safe parsing, graceful degradation when an adapter/linter/grammar is absent).

---

## 2. WBS — Sub-phases (ordered by leverage)

### 7.18.0 — Closed-Loop Sandboxed Executor (Technique 6) — HEADLINE — **[ADR-740]**

Make the coder run the project's verification in the existing sandbox and self-correct on real, **structured** failure output.

- Replace the dead `run_command` branch ([coder.py:133-160](../ailienant-core/agents/coder.py#L133)): dispatch through the already-wired sandbox path (`get_active_adapter().execute(...)`, reusing `SandboxBashTool`/`CheckTypeIntegrityTool`). A post-edit auto-verify step is optional and additive.
- **Structured diagnostics, not raw stdout (architect #1):** parse tool output into `[file, line, error_code, short_message]` — reuse `ValidationError`/`ValidationResult` and the `lsp_filter` JSON pattern; extend it to `mypy` (`--output-json`/regex) and `pytest` (short test-summary, not the full traceback). Raw traces truncate context and blow attention cost (O(T²)); local parsing is O(N) microseconds.
- On exit≠0 return a delta that **mimics `reflexion_guard`** (`healing_required=True`, `last_error_trace=<compact structured diagnostics, capped>`, `failed_node`, `failure_signature=normalize_signature(...)`, `correction_attempts+1`) so the existing `route_after_coder → run_error_correction_node` path re-injects and re-drafts. **No new loop, no new budgets.**
- **Net-new (minimal):** a thin verify node (`agents/verifier.py::run_verify_node`, or folded into the coder) + a diagnostic parser + one conditional edge into the existing `error_correction` target.
- Preserve the honesty contract: keep `EXECUTE_TIER_DEFERRED` as the fallback **only** when `get_active_adapter() is None`.
- **DoD:** with a deterministic stub adapter (canned `SandboxResult`, no real subprocess in CI) returning exit≠0-then-0, a `run_command` step drives exactly one correction cycle then completes; a perpetually-failing command stops at the budget; `adapter is None` still yields honest-deferred. `mypy .` exit 0 + targeted pytest. The existing `run_command`-deferral test (and the 7.15 gate row **EX2**) is revised to the new contract.

> **Highest-risk integration point.** `candidate_files_from_traceback` ([error_correction.py:92](../ailienant-core/agents/error_correction.py#L92)) only parses **CPython tracebacks**; pytest/mypy/tsc output yields no candidate, so `propose_fix` would concede and the loop would "run and capture" but **silently never re-draft**. Mitigation: thread the step's `target_file` through the existing `extra_candidates` seam (`run_error_correction_node` already appends it at [error_correction.py:289](../ailienant-core/agents/error_correction.py#L289)). **Every 7.18.0 test must assert a correction attempt actually fires on a captured non-zero exit — not merely that the command ran.**

### 7.18.1 — Session-Heatmap Recency (Technique 2, architect #2) — **[ADR-741]**

Replace the `recency_score=0.5` placeholder with a hybrid signal so a hot-but-old file isn't penalized as stale.

- `recency_score = 0.7·time_decay + 0.3·access_frequency`. **time_decay:** exponential decay over the `indexed_at` ISO field + live mtime of active/dirty buffers. **access_frequency:** an in-session per-file retrieval counter (dict/LRU keyed by `project_id+path`), normalized to [0,1]; O(1) read.
- **Net-new:** a pure helper `recency_score(indexed_at, access_count, now) -> float` + a session-scoped, size-bounded access counter. Wire into the `ContextMeter` init at [planner.py:332](../ailienant-core/agents/planner.py#L332). CSS formula and schema unchanged. No second DB query.
- **DoD:** a hot-but-old file beats a cold-but-old file (frequency term fires); fresh > stale; empty inputs → safe default (no div-by-zero); unparseable ISO → skipped not raised; counter bounded. **Invert** the stale assertion at [test_phase3_checkpoint_gate.py:12](../ailienant-core/tests/test_phase3_checkpoint_gate.py#L12) ("time-decay is NOT in production code"). `mypy .` exit 0.

### 7.18.2 — `response_format` Graceful Degradation (Technique 4) — **[ADR-742]**

Stop a local backend from hard-failing on the JSON-mode param.

- **Net-new (only the detect/strip):** at [llm_gateway.py:374](../ailienant-core/tools/llm_gateway.py#L374) and [:459](../ailienant-core/tools/llm_gateway.py#L459), pre-emptively strip `response_format` for known-local targets (the BYOM path already computes `is_local`) and/or catch a `response_format`-named error and re-emit once. The response then flows through the **existing** `_sanitize_json_response`/`_extract_nested_schema_target` repair — **do not add a new JSON repairer.**
- **DoD:** a stub backend that raises on `response_format` succeeds via strip+repair; a cloud backend is unchanged with no extra round-trip. `mypy .` exit 0 + both-branch pytest.

### 7.18.3 — AST-Skeleton Code-STYLE Few-Shot (Technique 5, architect #3) — **[ADR-743]**

Frame real project code as style patterns — as distilled skeletons, not whole functions.

- **Distill, don't dump:** extract only signature + type hints + docstring, body → `...`. **Reuse `ast_engine.py`** (tree-sitter, polyglot, cached), **not** stdlib `ast`. A skeleton extractor walks the parsed tree for function/method/class nodes and emits header + doc.
- **Net-new:** a style-exemplar selector that filters the `(file_path, snippet)` pairs `search_snippets(...)` already returns to 2-3 same-language nearby functions, runs each through the distiller, and frames them under an explicit "Match the conventions of these existing functions (naming, error handling, docstrings, type hints) — do not copy their logic" header, **distinct** from the topology RAG block. Framing constant lives in [prompts.py](../ailienant-core/agents/prompts.py).
- **Best-effort:** `""` on any failure (mirror [coder.py:50](../ailienant-core/agents/coder.py#L50)); a tree-sitter miss for an exotic language degrades to `""`; cap exemplar bytes.
- **DoD:** for a known language the coder prompt carries the style header + ≥1 same-language **skeleton** (body elided to `...`, signature/docstring present); empty/exotic project → `""` no exception; skeleton materially smaller than source. `mypy .` exit 0 + prompt-assembly unit test.

### 7.18.4 — AST-Hashed Semantic Response Cache (Technique 4, architect #4) — **[ADR-744]**

Skip the LLM round-trip when the same intent meets unchanged context (~40% API-cost cut, seconds→ms on repeats).

- **Extend the existing primitive.** `ASTEngine` (ast_engine.py:113-153) is already a blake2b content-hash tree cache. Add a sibling response cache keyed by `hash(prompt_intent) + AST-hash(context files)`; probe before a planner/coder LLM call, store on miss.
- **Net-new:** a bounded in-memory LRU `SemanticResponseCache` (size + TTL caps for OOM safety) + probe/store hooks at the two call sites. Active eviction reuses `ASTEngine.invalidate(path)` on the reactive-index save hook.
- **Correctness guards:** cache only deterministic calls (`temperature=0.0`, which planner/coder already use); fold dirty/unsaved-buffer hashes into the key (or bypass); key includes `project_id` and model id (no cross-project / cross-model bleed).
- **DoD:** identical intent + unchanged AST-hash → cache hit (gateway not invoked, asserted via mock call-count); a one-byte context edit → miss → re-invoked; dirty-buffer turns bypass; LRU evicts under cap. `mypy .` exit 0 + targeted pytest.

### 7.18.5 — MCTS-into-Live-Loop: **DEFER** (decision row) — **[ADR-745]**

- `brain/mcts/` + `agents/mcts_coder.py` exist but are **offline-only** (parallel dreaming). Wiring UCB1 variant-search into the live single-shot coder loop multiplies LLM calls per step, collides with the freshly-wired correction budgets from 7.18.0, and risks latency/cost regression on the exact loop 7.18.0 makes load-bearing — highest risk, lowest marginal value.
- Its natural reward signal is *exactly* the structured verdict 7.18.0 introduces, so MCTS-live is strictly better attempted **after** 7.18.0 ships and stabilizes.
- **Deliverable:** this ADR + one `TECH_DEBT_BACKLOG.md` row recording the defer and its precondition (7.18.0's verification signal as the MCTS reward source). **No source changes.** If pursued, scope as a follow-up gated behind 7.18.0's green checkpoint.

### 7.18.6 — Checkpoint Gate Fase 7.18 — **[ADR-746]**

- **Net-new (test-only):** `tests/test_phase7_18_checkpoint_gate.py`, mirroring the sibling-gate convention (`test_phase{3,5_7,6,7_10,7_13,7_15}_checkpoint_gate.py`): imports + invokes real entry points; one load-bearing assert per row; async via `asyncio.run`; fence/structure asserts via `ast` (the ISO1 precedent — judge statements, not docstring prose); **modifies no production logic.**
  - **EXLOOP1** — `run_command` + active stub adapter dispatches; exit≠0 → `healing_required` reaches `error_correction`.
  - **EXLOOP2** — loop respects `CORRECTION_MAX_ATTEMPTS`; `adapter is None` → honest-deferred fallback.
  - **DIAG1** — a failed verify yields **structured** `[file,line,code,msg]` diagnostics (not a raw trace) in `last_error_trace`, size-capped.
  - **REC1** — heatmap recency: a hot-but-old file outscores a cold-but-old file; fresh > stale holds.
  - **RF1** — `response_format`-rejecting backend succeeds via strip+repair; cloud unchanged.
  - **FS1** — coder prompt carries the style header + a same-language **skeleton** exemplar (body elided).
  - **CACHE1** — identical intent + unchanged AST-hash → cache hit (gateway not invoked); a context edit → miss → re-invoked.
  - **OCC1** — (architect #5, Option A) concurrent fan-out writes to a reduced state key **merge** without loss; a stale `base_hash` is **rejected** at the write edge — proving the existing OCC/reducer guarantee rather than adding a parallel mechanism.
  - **MCTS-DEFER** — asserts no live-loop import edge into `brain/mcts` (a future accidental wiring trips the gate).
- **DoD:** full `pytest` green + `mypy .` exit 0 + the gate file green. The §1 LOCK-IN of this blueprint expires when this row is marked `[x]`.

---

## 3. CLAUDE.md §3 Conflict — Architect Upgrade #5 (OCC version-vectors)

The architect asked for "strict OCC using version vectors on the LangGraph state dict; on conflict, reject and pull-and-retry (idempotent nodes)." Per CLAUDE.md §3 this is **surfaced, not silently built**, because it clashes with the shipped concurrency architecture:

- OCC **already exists** at the granularity that governs mutation safety: `document_version_id` per `FileArtifact` ([state.py:241-289](../ailienant-core/brain/state.py#L241)) + the coder's emitted per-file `base_hash` stale-guard for the VS Code `applyEdit` bridge.
- The **graph state** dict is managed by **LangGraph reducers** (`operator.add` for cost deltas, last-writer-wins `merge` for file maps; state.py:458-459), *not* reject-and-retry. Reducers exist precisely because parallel `Send()` fan-out writes the same keys concurrently — the reducer **merges** them deterministically.
- These are **opposite strategies for the same contention.** A version-vector OCC layer would **abort** the concurrent writes the reducers are built to **merge** — either duplicating the existing guarantee or fighting it (serializing the SWARM fan-out the architecture depends on).

**Resolution (CLAUDE.md §3 options):**
- **Option A — Pivot (CHOSEN/recommended):** treat #5's intent (zero state-corruption under concurrency) as **already satisfied** by reducers + `document_version_id` + `base_hash`. Ship gate row **`OCC1`** (7.18.6) that *asserts* the invariant rather than adding a parallel mechanism. Zero new concurrency machinery.
- **Option B — Manifest update (targeted):** if the real concern is **async MCP tool calls** mutating state mid-node (a genuine risk once 7.18 wires execute-tier dispatch), scope a small item to make the execute-tier write path idempotent and assert it — not a global version-vector rewrite.
- **Option C — Refactor (not recommended):** replace reducers with version-vector OCC across all nodes (every node becomes checkout-verify-commit-or-retry). A foundational re-spine of the concurrency model, high-risk, undoes the SWARM merge design. Only if a demonstrated corruption bug proves reducers insufficient.

---

## 4. Reuse scorecard

**Reused:** sandbox tiers, execution tools + HITL friction, `ValidationResult`/`lsp_filter` diagnostic pattern, `reflexion_guard`, `route_after_coder`, `run_error_correction_node`, `failure_breaker`, retry budgets, `_sanitize_json_response`/`_extract_nested_schema_target`, `search_snippets`, the `ast_engine` tree-sitter dissector + content-hash cache, the reducers + `document_version_id`/`base_hash` OCC, the `_emit` narration seam, the `indexed_at` field.

**Net-new (total):** one verify node + diagnostic parser + one edge (7.18.0); one hybrid recency helper + session access-counter (7.18.1); one strip/detect branch (7.18.2); one AST-skeleton distiller + style selector + constant (7.18.3); one `SemanticResponseCache` LRU + two probe/store hooks (7.18.4); one gate file (7.18.6).

**Conflict surfaced, not built:** architect #5 OCC (see §3).

---

## 5. Verification (DoD — all exit 0)

Backend (`cd ailienant-core`, `./venv/Scripts/python`):
1. `mypy .` → 0 after every sub-phase (no `--strict` regression; new code is strict-clean).
2. `pytest` → green, incl. the revised `run_command` test, the inverted recency assertion, and the new `test_phase7_18_checkpoint_gate.py`.
3. **Behavioral:** a Plan→Accept→code turn whose WBS includes a `run_command` verify step shows the command dispatched into the sandbox, a non-zero exit narrated as a critic/heal cycle, a re-draft, then green — all within `CORRECTION_MAX_ATTEMPTS`; with no adapter, the step honestly reports deferred.

Frontend: none (backend-only phase; 7.16.1 proceeds independently afterward).

---

## ADR Ledger

| ADR | Sub-phase | Binding decision |
|---|---|---|
| **740** | 7.18.0 | Closed-loop sandboxed executor; structured diagnostics; re-inject via the existing self-heal path; honest-deferred only when no adapter. |
| **741** | 7.18.1 | Hybrid recency `0.7·time_decay + 0.3·access_frequency`; in-session bounded counter; CSS formula/schema unchanged. |
| **742** | 7.18.2 | Strip/detect `response_format` for local backends; degrade to the existing 3-layer JSON repair; no new repairer. |
| **743** | 7.18.3 | AST-skeleton style exemplars via the tree-sitter engine; distinct from the topology RAG block; best-effort, byte-capped. |
| **744** | 7.18.4 | Semantic response cache atop the `ASTEngine` content-hash primitive; deterministic-only; project/model-scoped keys; OOM-bounded LRU. |
| **745** | 7.18.5 | DEFER MCTS-into-live-loop; precondition = 7.18.0's verification signal as reward; tech-debt row registered. |
| **746** | 7.18.6 | Checkpoint Gate Fase 7.18; sibling-gate file; OCC1 asserts the existing reducer/`base_hash` guarantee (§3 Option A). |
