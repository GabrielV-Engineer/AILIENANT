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
### DEBT-NNN [TIER · Schedule] — Short description
- **Date:** YYYY-MM-DD
- **Reproduce:** exact shell command that surfaces the error
- **File(s):** affected path(s) and line numbers if known
- **Error:** mypy error code or free-text description
- **Blocked by:** external dependency / phase prerequisite (if any)
- **Phase:** which Phase 8 subfase will address this
- **Notes:** context for future reader
```

---

## Tier Definitions

```
[HIGH]      Bounded correctness failures, reliability risks, or security concerns —
            mitigation exists but root cause is unresolved.
[MEDIUM]    Architecture debt, performance issues, test infrastructure gaps, or feature gaps
            whose absence is user-visible but not safety-relevant.
[LOW]       Type hygiene, test polish, UX convenience, deferred features with no urgency.
[DECISION]  Architectural decision record — not a defect; no fix planned.
```

Note: `[CRITICAL]` (active security exposure with no mitigation) is reserved for future use.
No currently-open entry meets that threshold.

## Schedule-State Legend

```
Locked      Assigned to a specific not-yet-shipped sub-phase (e.g., 8.8.5).
Floating    Named with a vague post-X timing; no concrete sub-phase ticket yet.
Unscheduled No phase assigned, or the named phase already shipped without closing this entry.
Blocked     Has an external dependency (upstream stubs, CI lane, coordinated migration).
Decision    Not a defect — see [DECISION] tier.
```

---

## Open Items Dashboard

| ID | Title (short) | Tier | Type | Target Phase | Schedule |
|---|---|---|---|---|---|
| DEBT-036 | Oracle executes on host (no sandbox) | HIGH | Security/Safety | 8.10.5 | Locked |
| DEBT-013 | Thinking-stream drops JSON-mode | HIGH | Reliability | 8.10.5 | Locked |
| DEBT-057 | Non-native-thinking models produce empty ThoughtBox — appear pre-scripted | MEDIUM | UX gap | Phase 11.5 | Locked |
| DEBT-058 | Submitted prompt not preserved during task execution (lost in long sessions) | MEDIUM | UX gap | Phase 11.6 | Locked |
| DEBT-059 | Chat UI has no compaction strategy for long sessions (DOM grows unboundedly) | MEDIUM | FE Architecture | Phase 11.7 + 8.12 | Locked |
| DEBT-066 | No runtime LLM tool-dispatch loop — registered orchestrator/coder/analyst tools never invoked (factories ready) | HIGH | Cognitive activation | Agency phase | Floating |
| DEBT-044 | ValidateWBSDependenciesTool detects ordering violations only, not true DAG cycles | MEDIUM | Correctness gap | post-8.8.4 | Floating |
| DEBT-045 | BudgetEstimatorTool uses fixed heuristic, not calibrated from session history | LOW | Accuracy gap | post-8.8.4 | Floating |
| DEBT-047 | generate_docstring is line-anchored, not a signature-aware Google/Numpy renderer | LOW | Feature gap | post-8.8.5 | Floating |
| DEBT-048 | RunBenchmarkTool skips task_service.register_active_task — task_id not visible via check_task_status | MEDIUM | Integration gap | post-8.8.6 | Floating |
| DEBT-049 | SkillInvokeTool passes embed_fn=None — semantic skill auto-matching disabled | LOW | Feature gap | post-8.8.6 | Floating |
| DEBT-050 | RunBenchmarkTool does not charge ledger.consume_budget — internal benchmarks unbudgeted | MEDIUM | Correctness gap | post-8.8.6 | Floating |
| DEBT-051 | list_tasks cross-role visibility — orchestrator sees all tasks regardless of originating role | LOW | Feature gap | post-8.8.6 | Floating |
| DEBT-052 | resolve_active_skills may execute synchronous LanceDB queries inside async def | LOW | Performance | DB-layer async migration | Floating |
| DEBT-053 | TaskStopTool uses SIGTERM only — no SIGKILL escalation after timeout | LOW | Reliability | post-8.8.6 | Floating |
| DEBT-054 | todo_write / agent_todos channel unbound — no cognitive node wiring | LOW | Integration gap | future integration sprint | Floating |
| DEBT-041 | GrepTool sequential scan (no content index) | MEDIUM | Performance | 8.8.x | Floating |
| DEBT-039 | Benchmark report artifacts no retention | MEDIUM | Reliability | post-8.5/8.8 | Floating |
| DEBT-035 | MultiPL-E TypeScript execution unsupported | MEDIUM | Feature gap | post-8.5/8.8 | Floating |
| DEBT-024 | HITL inline-diff O(N) transport | MEDIUM | Performance | future perf sub-phase | Floating |
| DEBT-037 | G2 retrieval isolation uses mock.patch | LOW | Test architecture | post-8.5/8.8 | Floating |
| DEBT-033 | config.json key_ref round-trip | LOW | UX gap | 8.4.x or later | Floating |
| DEBT-032 | Coder-side skill injection | LOW | Feature gap | 8.4.x or 8.8+ | Floating |
| DEBT-027 | MCP servers not auto-connected at launch | LOW | Feature gap | dedicated slice | Floating |
| DEBT-025 | Docker PTY no daemon integration test | LOW | Test coverage | 7.19 Docker pass | Blocked |
| DEBT-014 | brain/swarms.py NodeInputT 3 residual ignores | LOW | Type hygiene | LangGraph stubs | Blocked |
| DEBT-012 | Diff highlighting disables word-level diff | LOW | UX polish | 7.16.x/7.17 | Floating |
| DEBT-011 | tracemalloc heap-baseline test broken | LOW | Test design | Phase 8 slice | Floating |
| DEBT-007 | Auto-accept pays full HITL round-trip | LOW | Performance | Phase 11 | Floating |
| DEBT-005 | interior brain/agents strict-mode debt | LOW | Type hygiene | — | Unscheduled |
| DEBT-010 | OCC version-vectors: decision record | DECISION | Architecture | N/A | Decision |

---

## Open Entries

---

**HIGH**

---

### DEBT-036 [HIGH · Locked] — BenchmarkOracle executes candidate patches on the host (no sandbox isolation)

- **Date:** 2026-06-12
- **Reproduce:** call `BenchmarkOracle.run_oracle(problem, candidate_patch)` with live LLM output — the patched files are written to a host `TemporaryDirectory` and run via `SubprocessPythonExecutor` (inherits the parent process environment).
- **File(s):** `ailienant-core/tests/benchmark/oracle.py` (`BenchmarkOracle.run_oracle`, `SubprocessPythonExecutor`).
- **Error:** not a runtime defect — a **declared MVP trade-off (CLAUDE.md §7.2)**. The hermetic gate uses trusted golden/wrong fixtures; the AST pre-flight (`_check_patch_safety`) limits the blast radius for this MVP path. A fully isolated oracle (Docker sandbox + read-only corpus mount) is the Enterprise target.
- **Blocked by:** requires a sandbox tier that allows writing the corpus snapshot into a container-local temp dir (the current `SandboxCodegenExecutor` writes to the Docker ro mount's host side, which is sufficient for codegen but not for multi-file oracle isolation).
- **Phase:** standalone benchmark-runtime hardening slice (before the definitive ablation sweep, post-8.5/8.8 when the system is feature-complete).
- **Notes:** logged at 8.3.2 ship per CLAUDE.md §7.3. AST pre-flight (`_BLOCKED_IMPORTS` + Level-1 reflexivity blocklist) is the in-place mitigation.

### DEBT-013 [HIGH · Floating] — Thinking-stream coding turns drop hard JSON-mode (`response_format`)

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

---

**MEDIUM**

---

### DEBT-057 [MEDIUM · Locked] — Non-native-thinking models produce an empty ThoughtBox and appear pre-scripted

- **Date:** 2026-06-14
- **Reproduce:** Select a model that does not support native extended thinking (any non-Anthropic/non-DeepSeek-R1 model). Submit a complex prompt. The ThoughtBox is empty; the agent's response appears as a fixed, pre-scripted answer with no visible reasoning trace, making the AI look unintelligent.
- **File(s):** `ailienant-core/tools/llm_gateway.py` (`_supports_native_thinking`, `acomplete_with_thinking`); `ailienant-core/agents/planner.py` / `agents/coder.py` (system prompt builders); `ailienant-extension/src/webview/components/ThoughtBox.tsx`.
- **Error:** UX/capability gap. Fix: when native thinking is unavailable, inject a reasoning scaffold into the system prompt (`"Before answering, reason step-by-step inside <thinking>…</thinking>"`); stream the `<thinking>` block content via existing `broadcast_thinking_chunk` (identical contract, no new WS type); strip the tags before emitting the final answer token stream. FE: add `[Simulated]` vs `[Native]` tag to ThoughtBox header; add Reasoning Mode toggle (`Native` / `Verbose` / `Compact`).
- **Blocked by:** nothing — fully self-contained.
- **Phase:** Phase 11.5.
- **Notes:** confirmed in live testing session 2026-06-14.

### DEBT-058 [MEDIUM · Locked] — Submitted prompt not preserved during task execution; lost in long sessions

- **Date:** 2026-06-14
- **Reproduce:** Submit a long prompt. The input clears immediately. Scroll up in a session with 20+ messages to find the original prompt — difficult to locate. No sticky indicator of what task the AI is currently working on.
- **File(s):** `ailienant-extension/src/webview/components/NattCanvas.tsx`; `ailienant-extension/src/store/workspaceStore.ts`.
- **Error:** UX gap. Fix: Add `activeTaskPrompt: string | null` + `activeTaskId: string | null` to `workspaceStore.ts`; set on submit, clear on `TASK_COMPLETE`/`ERROR`. New `ActiveTaskHeader.tsx` component: sticky card above the message list showing the current task prompt (compressed, expandable), animated "Working…" indicator, elapsed time, Cancel affordance. Auto-collapses on completion; user-dismissible. No backend change — uses existing WS events.
- **Blocked by:** nothing.
- **Phase:** Phase 11.6.
- **Notes:** analogous to Claude Code's in-flight task header. Confirmed need in live testing session 2026-06-14.

### DEBT-059 [MEDIUM · Locked] — Chat UI has no compaction strategy for long sessions (DOM grows unboundedly)

- **Date:** 2026-06-14
- **Reproduce:** Run a session with 60+ messages. NattCanvas DOM grows unboundedly, causing sluggish rendering and memory pressure in the VS Code webview process. Also mirrors the backend context-window constraint for local models — frequent compaction events would be useful but no FE receiver exists.
- **File(s):** `ailienant-extension/src/webview/components/NattCanvas.tsx`; new `SessionSummaryCard.tsx`; `ailienant-extension/src/store/workspaceStore.ts`; `ailienant-core/api/websocket_manager.py` (new event type); `ailienant-core/brain/context_pipeline.py` (Division 8.12 emission hook).
- **Error:** FE architecture gap + backend integration gap. Fix (two-part): (a) Backend — when `ContextPipeline` (Division 8.12) evicts Layer 4 entries, emit `{"type": "STATE_COMPACTED", "summary": "...", "turns_compressed": N}` over WS. (b) Frontend — when message count exceeds `MESSAGE_COMPACTION_THRESHOLD` (default 40) AND a `STATE_COMPACTED` event arrives, replace messages before the compaction point with a collapsible `SessionSummaryCard` (header: "N messages summarized", body: StateSummarizer output text carried in the event). Messages after the point remain fully rendered.
- **Blocked by:** Division 8.12 `STATE_COMPACTED` event contract (8.12.3).
- **Phase:** Phase 11.7 (FE) + Division 8.12 (backend hook).
- **Notes:** analogous to Claude Code's `/compact` auto-compact. Addresses both DOM memory pressure AND context-window viability for local model sessions. Confirmed need 2026-06-14.

### DEBT-066 [HIGH · Floating] — No runtime LLM tool-dispatch loop activates the registered tools

- **Date:** 2026-06-15
- **Reproduce:** The orchestrator/coder/analyst tool *classes* register their schemas in the `ToolRAGStore` and (as of 8.10.2) have state/session/search-injecting factories, but no production code path runs an LLM tool-calling loop (no ReAct / `bind_tools` / `ToolNode`) that constructs and dispatches them. Only `execute_tracked_tool` dispatches a live tool, and it wires `sandbox_bash` exclusively. The agents make deterministic `LLMGateway.ainvoke` calls; the registered tools are retrievable but never invoked.
- **File(s):** wiring target is a new cognitive dispatch surface (extend `core/task_service.py::execute_tracked_tool` or add a tool-calling node in `brain/`); consumes the 8.10.2 factories (`tools/agent_tools.py::build_orchestrator_tools`, `tools/coder_tools.py::make_coder_execute_tools`, `tools/analyst_tools.py::make_web_search_tool` / `make_dependency_audit_tool`).
- **Error:** not a runtime defect — a **declared trade-off (CLAUDE.md §11.2)**. 8.10.2 made tool construction correct-when-wired (state/session/search injected, HITL gates ready); the missing piece is the activation loop that lets a model select and call them.
- **Blocked by:** nothing structural; needs the dispatch-loop design (deterministic-engine-compatible) — a focused intelligence-phase effort, not a rebuild.
- **Phase:** later intelligence / Agency phase.
- **Notes:** logged at 8.10.2 ship per CLAUDE.md §11.3. Supersedes the dispatch half of the former DEBT-043/046/042 and the DEBT-054 channel-wiring concern.

### DEBT-044 [MEDIUM · Floating] — ValidateWBSDependenciesTool detects forward-reference ordering violations only, not true DAG cycles

- **Date:** 2026-06-14
- **Reproduce:** Construct a `MissionSpecification` with two mutually-dependent steps where file A is consumed by step 2 (which produces file B) and file B is consumed by step 1 (which produces file A). The tool reports the step with the smaller index as a forward-reference, but cannot report the full cycle because `WBSStep` carries no `depends_on` field — dependency edges must be inferred from `target_file`/`action` patterns alone.
- **File(s):** `ailienant-core/tools/planner_tools.py` (`ValidateWBSDependenciesTool._arun`, Pass 2).
- **Error:** not a runtime defect — a **declared trade-off (CLAUDE.md §11.2)**. The dominant planning error class (forward-reference ordering) is caught; true cross-file cycles require schema surgery on `WBSStep`.
- **Blocked by:** additive `depends_on: Optional[List[int]] = None` field on `WBSStep` (`brain/state.py`) + migration in `docs/SCHEMA_EVOLUTION.MD`.
- **Phase:** post-8.8.4 — schedule after `WBSStep` schema extension.
- **Notes:** logged at 8.8.4 ship per CLAUDE.md §11.3.

### DEBT-047 [LOW · Floating] — generate_docstring is line-anchored, not a signature-aware renderer

- **Date:** 2026-06-14
- **Reproduce:** Call `generate_docstring` on a multi-line `def`/`class`. It inserts a `"""TODO: document <name>."""` stub as the first body statement; it does not synthesize param/return sections from the signature, and it deliberately SKIPs single-line definitions (`def f(): return 1`).
- **File(s):** `ailienant-core/tools/coder_tools.py` (`DocstringGeneratorTool._arun`).
- **Error:** not a defect — a **declared trade-off (CLAUDE.md §11.2)**. AST-anchored insertion + `_validate_python_syntax` keeps it safe and deterministic; a richer Google/Numpy renderer is deferred.
- **Blocked by:** nothing — a self-contained enhancement.
- **Phase:** post-8.8.5.
- **Notes:** logged at 8.8.5 ship per CLAUDE.md §11.3.

### DEBT-048 [MEDIUM · Floating] — RunBenchmarkTool skips task_service.register_active_task

- **Date:** 2026-06-14
- **Reproduce:** Submit a benchmark via `run_benchmark` tool and then poll `check_task_status` with the returned task_id. `check_task_status` routes through `task_service.get_task_status()` which only knows about tasks registered via `register_active_task`. Since the tool bypasses that call, it returns `{"status": "unknown"}`. `get_benchmark_report` still works (it reads the artifact file directly).
- **File(s):** `ailienant-core/tools/gateway_tools.py` (`RunBenchmarkTool._arun`).
- **Error:** declared trade-off (CLAUDE.md §11.2). `task_service` is a singleton wired at lifespan startup — tools have no dependency-injection path to it currently.
- **Blocked by:** a shared benchmark/task lifecycle service that exposes `register_active_task` via a module-level accessor or injection point.
- **Phase:** post-8.8.6.
- **Notes:** logged at 8.8.6 ship per CLAUDE.md §11.3.

### DEBT-049 [LOW · Floating] — SkillInvokeTool embed_fn=None disables semantic auto-matching

- **Date:** 2026-06-14
- **Reproduce:** Call `skill_invoke` with only `user_input` (no `skill_id`). Without an embedder, `resolve_active_skills` falls back to the explicit-ID path and returns an empty list (no skills auto-matched by semantic similarity).
- **File(s):** `ailienant-core/tools/gateway_tools.py` (`SkillInvokeTool._arun`).
- **Error:** declared trade-off. A shared embedder injection point does not yet exist at tool-init time.
- **Blocked by:** a graph-level embedder factory that can be passed to skill-related tools at construction.
- **Phase:** post-8.8.6.
- **Notes:** logged at 8.8.6 ship per CLAUDE.md §11.3.

### DEBT-050 [MEDIUM · Floating] — RunBenchmarkTool does not charge ledger.consume_budget

- **Date:** 2026-06-14
- **Reproduce:** Internal agents invoking `run_benchmark` tool bypass the `ledger.consume_budget()` call that the external gateway handler applies. Benchmark compute cost is unaccounted for in the token ledger.
- **File(s):** `ailienant-core/tools/gateway_tools.py` (`RunBenchmarkTool._arun`); compare `gateway/handlers.py` (`handle_run_benchmark`).
- **Error:** declared trade-off. A cross-cutting budget interceptor (pre-action hook on EXECUTE-tier tools) would close this uniformly.
- **Phase:** post-8.8.6.
- **Notes:** logged at 8.8.6 ship per CLAUDE.md §11.3.

### DEBT-051 [LOW · Floating] — task_list cross-role visibility (orchestrator sees all tasks)

- **Date:** 2026-06-14
- **Reproduce:** Multiple roles (e.g. qa_tester, devops_infra) use `task_create`; the orchestrator's `task_list` shows all of them with no owner attribution.
- **File(s):** `ailienant-core/tools/execution_tools.py` (`BackgroundTaskManager.list_tasks`), `ailienant-core/tools/gateway_tools.py` (`TaskListTool._arun`).
- **Error:** not a defect — the orchestrator is a cognitive superuser in the current model. A future `owner_role` field on registry entries would allow scope-filtered listing.
- **Phase:** post-8.8.6.
- **Notes:** logged at 8.8.6 ship per CLAUDE.md §11.3.

### DEBT-052 [LOW · Floating] — resolve_active_skills may execute synchronous LanceDB queries

- **Date:** 2026-06-14
- **Reproduce:** Call `skill_invoke` with a valid skill_id. `resolve_active_skills` is `async def` but internally calls `catalog_db.get_skill()` / `list_enabled_skills_for_scope()` which may be synchronous LanceDB queries. If synchronous, they block the FastAPI event loop during the `await`.
- **File(s):** `ailienant-core/core/skill_resolver.py`, `ailienant-core/tools/gateway_tools.py`.
- **Error:** pre-existing substrate concern, not introduced by this wave.
- **Blocked by:** DB-layer async migration.
- **Phase:** DB-layer async migration.
- **Notes:** logged at 8.8.6 ship per CLAUDE.md §11.3.

### DEBT-053 [LOW · Floating] — TaskStopTool uses SIGTERM only, no SIGKILL escalation

- **Date:** 2026-06-14
- **Reproduce:** Use `task_stop` on a process that traps SIGTERM. The process ignores the signal and keeps running; `_registry["status"]` is "cancelled" but the PID is still alive.
- **File(s):** `ailienant-core/tools/execution_tools.py` (`BackgroundTaskManager.stop`).
- **Error:** declared trade-off. A kill-after-timeout pattern (SIGTERM → wait N seconds → SIGKILL / TerminateProcess) is the correct fix.
- **Phase:** post-8.8.6.
- **Notes:** logged at 8.8.6 ship per CLAUDE.md §11.3.

### DEBT-054 [LOW · Floating] — `todo_write` / `agent_todos` channel have no runtime call site

- **Date:** 2026-06-14
- **Reproduce:** `TodoWriteTool._arun` returns a well-formed JSON string keyed `agent_todos`; `brain/state.py` has the channel and reducer. However, no graph node calls this tool or routes its output through the `_merge_todos` reducer at runtime — the channel is invisible in the UI.
- **File(s):** `ailienant-core/tools/universal_tools.py`, `ailienant-core/brain/state.py`.
- **Error:** declared trade-off (CLAUDE.md §11.2). Division 8.8 is schema-registration-surface only; no `register_*_tools` has a runtime call site. Wiring `todo_write` into a cognitive node so the TODO list renders in the extension sidebar belongs in a dedicated integration phase.
- **Phase:** future integration sprint.
- **Notes:** logged at 8.8.7 ship per CLAUDE.md §11.3.

### DEBT-041 [MEDIUM · Floating] — GrepTool reads catalog-only files sequentially without a content index

- **Date:** 2026-06-13
- **Reproduce:** `GrepTool._arun` iterates `path_provider()` and calls `content_reader(path)` per file via the firewalled `read_safe` reader. The mandatory O(max_matches) short-circuit limits total matches, but on a large workspace every pre-filter file still incurs a disk read until a match is found. No inverted index exists.
- **File(s):** `ailienant-core/tools/researcher_tools.py` (`GrepTool._scan`).
- **Error:** not a runtime defect — a **declared MVP trade-off (CLAUDE.md §11.2)**. The `asyncio.to_thread` offload and the short-circuit guarantee the event loop is never blocked and latency is O(L) in the match cap. The residual is latency on very large workspaces with sparse matches.
- **Blocked by:** nothing structural; the enterprise fix adds an inverted content index and a ReDoS-bounded regex evaluator.
- **Phase:** Wave 2 / Analyst quality-lens (8.8.2), where search tooling becomes load-bearing for the Analyst.
- **Notes:** logged at 8.8.1 ship per CLAUDE.md §11.3.

### DEBT-039 [MEDIUM · Floating] — Benchmark report artifacts have no retention policy

- **Date:** 2026-06-13
- **Reproduce:** trigger `run_benchmark` repeatedly — each run writes a `~/.ailienant/benchmark/<task_id>.json` that is never pruned.
- **File(s):** `ailienant-core/core/benchmark_service.py` (`BENCHMARK_DIR`, `run_benchmark`).
- **Error:** not a runtime defect — unbounded disk growth over time. The single-flight cap bounds the *rate* of growth, not the total.
- **Blocked by:** nothing structural; needs a retention policy decision (cap by count, age-prune, or LRU eviction on write).
- **Phase:** standalone eval-surface hardening slice, post-8.5/8.8.
- **Notes:** logged at 8.5.5 ship per CLAUDE.md §7.3.

### DEBT-035 [MEDIUM · Floating] — MultiPL-E TypeScript execution needs a Node-capable sandbox runtime

- **Date:** 2026-06-12
- **Reproduce:** run a TypeScript codegen problem through `SandboxCodegenExecutor.run(program, Language.TYPESCRIPT, …)` — it returns `ExecOutcome(passed=False, exit_code=-2, stderr="[unsupported_runtime: ...]")` instead of executing.
- **File(s):** `ailienant-core/tests/benchmark/executors.py` (`SandboxCodegenExecutor`); `ailienant-core/core/sandbox.py` (`_DOCKERFILE_TEXT`, `python:3.13-slim`).
- **Error:** not a defect — a **declared MVP trade-off (CLAUDE.md §7.2)**. The shared sandbox image is Python-only (no Node/tsc), so MultiPL-E TS cannot be executed in-container. 8.3.1 ships the full TS *adapter* (loader, prompt, extraction, assembly, Pass@1 wiring); only the TS *execution backend* is deferred. Python (HumanEval) Pass@1 is real.
- **Blocked by:** nothing technical — needs a Node-capable sandbox tier (extend the image with Node/tsx, or a vetted host-Node executor behind explicit opt-in) without compromising the locked Docker security profile.
- **Phase:** a standalone benchmark-runtime slice (before the definitive cross-language run, post-8.5/8.8).
- **Notes:** logged at 8.3.1 ship per CLAUDE.md §7.3. Until then TS Pass@1 is `unsupported_runtime`; the 8.3.1 DoD is met on the Python subset.

### DEBT-024 [MEDIUM · Floating] — HITL inline-diff transport ships full file content (O(N)) instead of a unified diff (O(Δ))

- **Date:** 2026-06-08
- **Files:**
  - `ailienant-core/api/ws_contracts.py` — `ProposedFile.new_content` (full post-edit content).
  - `ailienant-core/core/task_service.py` — HITL branch builds `proposed_files` from `pending_contents`.
  - `ailienant-extension/src/core/PatchActuator.ts` — `preview()` / `apply()` build `PatchedFileDiff` with full `old_content` + `new_content`.
- **Error:** not a type error — a space/latency trade-off. The pre-apply inline-diff approval (and the existing post-apply `RENDER_DIFF`) transport **full file content per file** over the WebSocket. For a large file this is O(N) on the wire and the client-side `diffLines(old, new)` is O(N) work; a multi-thousand-line file risks WS-buffer pressure and a main-thread/event-loop stall during diffing.
- **Blocked by:** nothing — but it must convert **both** the pre-apply (this fix) and the post-apply (`PatchActuator.apply` → `RENDER_DIFF`) paths together, since they share `PatchedFileDiff`/`DiffBlockShape`.
- **Phase:** future performance/transport sub-phase (own ticket — do not smuggle into a feature fix).
- **Notes:** declared MVP during the ASK-mode inline-approval fix (one-atomic-event design). Bounded by the existing `DIFF_RENDER_LINE_CAP=400` *mount* cap in `DiffBlock.tsx`, but the **wire payload** is still uncapped. Target design: compute the unified diff server-side with `difflib`, transport unified diffs only (O(Δ)), and reconstruct both sides in the host via the already-present `diff` lib (`applyPatch`). Safe to defer under the bounded-file-size assumption.

---

**LOW**

---

### DEBT-045 [LOW · Floating] — BudgetEstimatorTool uses a fixed per-action token heuristic, not a calibrated model

- **Date:** 2026-06-14
- **Reproduce:** `BudgetEstimatorTool._arun` computes `estimated_cost_usd` from static base-token constants (`write_file=1000`, `edit_file=800`, `read_file=200`, `run_command=100`) plus `len(description)//4`. These constants were chosen as conservative approximations of the cloud rate; no session-history calibration is performed.
- **File(s):** `ailienant-core/tools/planner_tools.py` (`BudgetEstimatorTool._arun`, `_ACTION_BASE_TOKENS`).
- **Error:** not a runtime defect — a **declared trade-off (CLAUDE.md §11.2)**. The advisory is shift-left (fires before `oom_fallback`) and produces `confidence="low"` to signal the approximation to consumers.
- **Blocked by:** requires session-history telemetry (actual token counts per action type stored in `TokenLedger` or a side table).
- **Phase:** post-8.8.4 — calibration pass after session telemetry is available.
- **Notes:** logged at 8.8.4 ship per CLAUDE.md §11.3.

### DEBT-037 [LOW · Floating] — G2 retrieval isolation uses mock.patch, not a production DI seam

- **Date:** 2026-06-12
- **Reproduce:** inspect `tests/benchmark/strategies.py:VectorOnlyRetrievalStrategy.patches()` — it returns `[mock.patch(GRAPH_SEAM, _no_graph)]`, a process-global monkey-patch scoped to one task run.
- **File(s):** `ailienant-core/tests/benchmark/strategies.py`, `ailienant-core/tests/benchmark/arms.py`.
- **Error:** not a runtime defect — a **declared MVP trade-off (CLAUDE.md §7.2)**. The patch-based mechanism is the harness-boundary approach established at 8.3.0; no production code is changed. The enterprise alternative is a `RetrievalStrategy` injected via a DI seam in `GraphRAGDynamicExtractor` so the strategy boundary is visible in production profiling and A/B testing, not only in benchmark runs.
- **Blocked by:** requires a production DI refactor of `GraphRAGDynamicExtractor` and its callers (planner + researcher). Out of scope for the 8.3.x measurement track.
- **Phase:** standalone retrieval-DI slice, post-8.5/8.8 when the ablation sweep is complete and the architecture is stable.
- **Notes:** logged at 8.3.3 ship per CLAUDE.md §7.3. The patch-based mechanism is hermetically verified by `test_ablation_verdicts.py` gate tests.

### DEBT-033 [LOW · Floating] — config.json ↔ MCP secret-store `key_ref` round-trip (fresh-machine import prompt)

- **Date:** 2026-06-11
- **Reproduce:** export `.ailienant/config.json` on a machine with installed credentialed servers, then import it on a fresh machine — the server rows reconcile but their secrets do not travel (correctly: secrets never enter the JSON), and there is no flow that re-prompts for the missing credential. The dashboard also has no import/export buttons surfacing `core/mcp_config.py`.
- **Error:** not a defect — a deferred polish slice. 8.4.6 landed the secret VALUE store + connect-time env injection (closing the load-bearing half of the former DEBT-031), and `core/mcp_config.py` already redacts on export + emits `key_ref` placeholders. What remains is wiring the placeholder round-trip to the new `mcp_secrets` store: on import, detect a `key_ref` whose value is absent locally and prompt for it; expose import/export in the dashboard.
- **Blocked by:** nothing.
- **Phase:** a standalone config-portability slice (8.4.x or a later polish pass).
- **Notes:** logged at 8.4.6 ship per CLAUDE.md §7.3. Export already prevents credential leakage (userinfo redaction + no secret in JSON), so this is a usability gap, not a security one. Substrate is the backend-mask store (`core/config/mcp_secrets.py`), consistent with the ADR-757 amendment.

### DEBT-032 [LOW · Floating] — Coder-side skill injection (planner-only shipped in 8.4.5)

- **Date:** 2026-06-11
- **Reproduce:** submit a task with a saved skill active — the skill directive block appears in the planner system prompt (and therefore in the `mission_spec` the coder receives) but is **not** re-injected into the coder's own system prompt. For skills that express a coding style or pattern constraint, the planner-mediated injection is sufficient; for skills that must survive across multi-step coder turns or whose instruction is structurally important to coder generation, a coder-side injection would be more robust.
- **File(s):** `ailienant-core/agents/planner.py` (injection seam, 8.4.5) — the coder (`agents/coder.py`) has no equivalent injection.
- **Error:** not a defect — a **declared MVP trade-off (CLAUDE.md §7.2)**. The planner bakes skill directives into the `mission_spec` that the coder receives, so one seam shapes the whole task with zero TypedDict churn and the lowest blast radius for 8.4.5.
- **Blocked by:** nothing; requires reading `state.get("active_skills")` in the coder's system-prompt construction and calling `build_skill_directive_block` there (same seam pattern as the planner).
- **Phase:** a standalone coder-side injection slice (8.4.x or a bundled polish pass).
- **Notes:** logged at 8.4.5 ship as required by CLAUDE.md §7.3 (every MVP must surface a tracked follow-up). The planner-only path covers the vast majority of skill-directive use cases.

### DEBT-027 [LOW · Floating] — MCP servers testable but not auto-connected at task launch

- **Date:** 2026-06-10
- **Updated:** 2026-06-13 — Confirmed still open. `bootstrap_mcp_session` is not called anywhere in `core/task_service.py`. Comment in `api/mcp_servers.py:11` explicitly notes: "Auto-connecting saved servers at task time is a tracked follow-up." Reclassified from "Phase: 8.4.4" to Floating (8.4.4 shipped other MCP work but not this auto-connect wiring).
- **Reproduce:** `POST /api/v1/mcp/test` probes a server successfully, but starting a new task does not open sessions to `enabled` servers — their tools are absent from the task's `ToolRAGStore` selection.
- **File(s):** `ailienant-core/api/mcp_servers.py` (test endpoint exists, no auto-connect hook); `ailienant-core/tools/mcp_adapter.py::bootstrap_mcp_session` (not invoked at task launch); `ailienant-core/core/task_service.py` (task entry, no MCP bootstrap pass).
- **Error:** coverage/wiring gap — a configured-and-enabled MCP server contributes no tools until manually bootstrapped.
- **Blocked by:** none.
- **Phase:** dedicated auto-connect wiring slice (Floating — assign to next 8.x window).
- **Notes:** The `autoconnect_enabled_mcp_servers()` function in `tools/mcp_adapter.py` is correct and ready; only the invocation at task startup in `core/task_service.py` is missing.

### DEBT-025 [LOW · Blocked] — Docker persistent-PTY backend has no daemon integration test

- **Date:** 2026-06-09
- **Files:**
  - `ailienant-core/core/sandbox.py` — `_DockerPtyBackend` (`exec_create`/`exec_start(socket=True, tty=True)` persistent shell) and `DockerSandboxAdapter.open_session`.
- **Error:** not a type error — a coverage gap. The directed suite (`tests/test_phase7_19_0_pty_session.py`) verifies the session contract through a stub backend and the real Unix `openpty` backend (Unix-only, skipped on Windows). The **Docker** session backend's real exec-socket framing (raw-stream `tty=True` semantics, socket detach on container stop, `exec_inspect` exit-code reap) is exercised only structurally via the shared `_PtySession` machinery — no test attaches to a live `ailienant-sandbox-daemon` container.
- **Blocked by:** a Docker daemon in CI (the broader sandbox-integration gap; `test_execution_tools` Docker failures are already environmental per project notes).
- **Phase:** the Phase 7.19 Docker-session integration pass (or whichever sub-phase wires the dispatcher onto a live container), once a daemon-backed CI lane exists.
- **Notes:** declared MVP during 7.19.0. The host PTY path (Native Direct) — the tier the 7.19.2 dispatcher will actually drive first — is fully covered (stub + real openpty). The Docker backend is implemented for parity but unverified end-to-end against a container; treat its first live use as integration-test-gated.

### DEBT-014 [LOW · Blocked] — brain/swarms.py: NodeInputT add_node type-var — ⚠️ REDUCED (3 residual ignores) 2026-06-08

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

### DEBT-012 [LOW · Floating] — diff highlighting disables word-level diff (no intra-line token slicing)

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

### DEBT-011 [LOW · Floating] — test_v3_tracemalloc heap-baseline ceiling is structurally broken (red in the Phase 3.7 gate)

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
  per the Continuous Registry Protocol rather than fixed in-place.
- **Phase:** A future test-hardening slice (Phase 8 / gate maintenance).
- **Notes:** Remediation options: (a) call `gc.collect()` before *both* snapshots and warm Pydantic by
  constructing one `MissionSpecification` before the baseline so its schema cache is already resident;
  (b) replace the baseline-relative ceiling with a fixed absolute budget sized for 50 nodes (e.g.
  ~512 KB) since the real claim is "bounded growth," not "returns to zero"; (c) measure only the
  `MCTSTree`/RAM-VFS delta via `snapshot.filter_traces` on `brain/mcts/tree.py` + `core/vfs_middleware.py`
  rather than the whole-process `"filename"` sum. Option (b)+(c) together best preserve the original
  intent (heap-leak guard on the MCTS lifecycle) without the brittle near-zero baseline.

### DEBT-007 [LOW · Floating] — Auto-accept low-risk edits pays a full HITL round-trip (shift-left candidate)

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
  today; the setting is webview-local).
- **Phase:** A future shift-left optimization (Phase 11 or a later 7.14.x): the backend reads the
  auto-accept setting and, for low-risk edits, **omits emitting the approval event altogether** — the
  edit applies server-side with no round-trip.
- **Notes:** The conservative risk gate (any medium/high metric forces the manual card) must be
  preserved if/when this moves server-side.

### DEBT-005 [LOW · Unscheduled] — Multiple brain/ + agents/ interior nodes: unknown strict debt

- **Date:** 2026-05-31
- **Updated:** 2026-06-13 — Phases 8.4 and 8.7 shipped. Verification confirms **4 errors remain**
  in `brain/engine.py` under `--strict`. Entry reclassified from Floating (8.4/8.7) to **Unscheduled**.
- **Reproduce:** `cd ailienant-core && .\venv\Scripts\python -m mypy --strict brain/engine.py 2>&1 | grep "error:" | wc -l`
  (returns 4 as of 2026-06-13)
- **Files:** `brain/engine.py`, `brain/ideation.py`, `brain/guardrails.py`,
  `brain/intent_router.py`, `agents/coder.py`
- **Error:** Various — `type-arg`, `no-any-return`, `no-untyped-def`. 4 confirmed in `brain/engine.py`;
  full count in remaining files not yet measured.
- **Blocked by:** Nothing structural — upstream silenced modules were unsilenced in 8.1–8.7.
  The errors are now directly measurable and fixable; they simply lack a scheduled phase.
- **Phase:** Unscheduled — assign to the next available typing-hardening sub-phase.
- **Notes:** Do NOT fix preemptively in unrelated tickets. Assign to a dedicated typing-hardening
  slice once 8.8 core work stabilizes.

---

**DECISION RECORDS**

---

### DEBT-010 [DECISION] — OCC version-vectors on the graph state dict: rejected in favor of existing reducers (decision record)

- **Date:** 2026-06-03
- **Reproduce:** N/A (architecture decision, not an error). Architect upgrade #5 requested strict version-vector OCC on the LangGraph state dict (reject-and-retry, idempotent nodes).
- **File(s):** `brain/state.py:241-289` (per-file `document_version_id` OCC), `brain/state.py:458-459` (LangGraph reducers: `operator.add`, last-writer-wins `merge`); `agents/coder.py` (emitted `base_hash` stale-guard).
- **Error:** Not a defect — a **conflict surfaced per CLAUDE.md §3.** OCC already exists at the file granularity that governs mutation safety, and the graph state is managed by **reducers** that *merge* the concurrent `Send()` fan-out a version-vector model would *abort* — opposite strategies for the same contention. A parallel OCC layer would either duplicate the guarantee or serialize (break) the SWARM fan-out.
- **Blocked by:** N/A — **resolved as Option A (Pivot):** the intent (zero state-corruption under concurrency) is treated as already satisfied; the 7.18.6 gate row **OCC1** *asserts* the existing reducer + `base_hash` guarantee rather than adding a mechanism.
- **Phase:** Decision recorded under **7.18 (ADR-746)**. Re-open **only** if a demonstrated corruption bug proves reducers insufficient.
- **Notes:** A genuine future risk: once 7.18 wires execute-tier dispatch, **async MCP tool calls** mutating state mid-node could warrant Option B (targeted execute-tier write idempotency) — a small hardening, not a global OCC rewrite.

---

## Closed Entries

*(Entries here are compact summaries. Full resolution notes are in git history and in the entry's Resolution block before it was moved here.)*

- **DEBT-043 — Orchestrator tools unbound to live state** — **RESOLVED 2026-06-15** (8.10.2). Added `make_get_wbs_status_tool` / `make_emit_hitl_request_tool` + `build_orchestrator_tools(state)` in `tools/agent_tools.py` — the canonical path that constructs the audited tools bound to the live graph state. The deterministic node's flag contract is left intact (§10); invocation moves to [DEBT-066].
- **DEBT-046 — Coder EXECUTE wrappers lack the interactive HITL card** — **RESOLVED 2026-06-15** (8.10.2). New `_gated_exec` + `_GatedExecTool` base + `make_coder_execute_tools(state)` thread `session_id`/`session_permission_mode` so EXECUTE-tier commands route through `evaluate_action` → `request_human_approval` (mirrors the MCP gate), honoring the trust-once valve; `guard_env_file` excluded (own gate). Additive — unfactoried construction unchanged.
- **DEBT-042 — Analyst search_fn unwired** — **RESOLVED 2026-06-15** (8.10.2). `tools/mcp_adapter.py::make_brave_search_fn()` resolves the brave-search session lazily and is resilience-wrapped (`wait_for` + broad except → degradation string, never raises); `make_web_search_tool` / `make_dependency_audit_tool` inject it by default. CVE/web search go live the moment the session connects.
- **DEBT-028 (hooks half) — Hooks persisted but never executed** — **RESOLVED 2026-06-15** (8.10.2). `TaskService._run_patch_hooks` runs enabled `pre_patch`/`post_patch` commands through the sandbox adapter around the single `apply_patch_set` commit; the ceiling is delegated to the adapter's `timeout_s` (kills+reaps — no outer `wait_for` orphan). `pre_patch` non-zero/timeout/no-adapter fails-closed (vetoes); `post_patch` is advisory; every fault is non-fatal + logged. (Skills half closed earlier in 8.4.5.)
- **DEBT-034 — Gateway `project_id` hashing is path-format-fragile** — **RESOLVED 2026-06-15** (8.10.1). `project_id_for` (core/storage_paths.py) now hashes `os.path.normcase(os.path.normpath(workspace_root))`; `PathResolver.computeProjectId` mirrors it byte-for-byte via Node `path.win32/posix.normalize` + a trailing-separator strip that preserves the disk/UNC/POSIX root (a naive regex strip would corrupt `C:\`→`C:`). One-time lazy re-index on next workspace open.
- **DEBT-038 — Production benchmark service imports the test tree** — **RESOLVED 2026-06-15** (8.10.1). Relocated the 11 harness modules (+ `corpus/` and `datasets/` fixtures) from `tests/benchmark/` to a shippable `core/benchmark/` package; repointed all `tests.benchmark.*` imports to `core.benchmark.*` (harness, 7 test files, `benchmark_service.py`, `test_gateway_eval_surface.py`). Reverse-dependency guard: zero `from tests` imports remain under `core/benchmark/`. `report.schema.json` stays in `tests/benchmark/` (read via the test's own `__file__`).
- **DEBT-040 — `tool_search` role resolution stale across per-step transitions** — **RESOLVED 2026-06-15** (8.10.1) via Explicit State Augmentation. Root cause: the router never re-set `active_role` per step — it inherited the task-initial value. The `Send` payload now carries `active_role = step.target_role` (engine.py, both SWARM and RELAY sites), so the wired tool-selection path is per-step-correct; `_resolve_active_role` is config-first and the ambient `_task_active_role` ContextVar was removed entirely (def + task_service set/reset), eliminating staleness and cross-WS leakage. Residual: the agent-callable `tool_search` dispatch itself is still unwired (the DEBT-043/046/054 cluster) — this makes selection correct now and resolution correct when that dispatch lands.
- **DEBT-064 — Agent organizes its own runtime files → OCC stale-apply** — **RESOLVED 2026-06-14** (8.10.x). The telemetry log isn't a code file, so it reached the agent via the workspace tree (`_build_tree`, a raw `os.walk` that lists hidden files); the "move" was a patch through `apply_patch_set`. Fixed at the source (filter the tree) + a write-layer guard dropping internal paths from the patch set + a VFS read-block on `.ailienant_telemetry.log*`. `is_ailienant_internal_path` (core/storage_paths.py) exempts the user-authored `.ailienant/AILIENANT.md`.
- **DEBT-063 — Plan executes out of WBS order** — **RESOLVED 2026-06-14** (8.10.x). WBS steps carry only implicit `step_number` ordering (no dependency DAG), so the `tci>80` blanket SWARM fan-out (`planner.parallel_tasks`) ran dependent steps out of order. Set `parallel_tasks=[]` → always sequential RELAY; SWARM dispatch left dormant for a future explicit-dependency DAG.
- **DEBT-065 — Auto-mode summary wording** — **RESOLVED 2026-06-14** (8.10.x). `_format_coding_summary` took only `plan_surface`; added a backward-compatible `auto_apply` branch so Auto reads "Applying N file change(s) directly…" instead of "review the diff and authorize."
- **DEBT-055 — Chat scroll regression** — **RESOLVED 2026-06-14** (8.10.0). The real defect was the Natt/Analyst pane: `.ws-natt-body` is a `1fr` grid track missing `min-height: 0`. The main chat list was already correct.
- **DEBT-056 — Text HUD fixed height (no auto-resize)** — **RESOLVED 2026-06-14** (8.10.0). Shared `useAutoResizeTextarea` (`useLayoutEffect`) hook on PromptBar + NattPromptBar; bounds in CSS (`min-height: 2.5rem; max-height: 12rem`). Introduced a HUD height regression, fixed under DEBT-062.
- **DEBT-060 — Diff-authorize card duplicated on tab switch with no diff** — **RESOLVED 2026-06-14** (8.10.0). `server_plan_document` re-injected its summary on every panel reveal; made the webview handler idempotent by summary content + a content-based host re-post guard. (Renumbered from a collision with the existing DEBT-057.)
- **DEBT-061 — Pipeline execution trace collapsed to a 1px box line** — **RESOLVED 2026-06-14** (8.10.0). Redesigned `.ws-thinking` from a bordered widget into an inline borderless trace. (Renumbered from a collision with the existing DEBT-058.)
- **DEBT-062 — Telemetry HUD height regression + context-window indicator** — **RESOLVED 2026-06-14** (8.10.0). Shared `--hud-rest-height` aligns composer + telemetry card; merged OCC ring and context meter into one split donut (`OccContextRing`); per-model window resolved via litellm `get_model_info`; apply-result paths backtick-wrapped. Live used-tokens read may still need a runtime trace (diagnostic logged).
- **DEBT-001 — tools.patch_tool: LangChain @tool decorator stub mismatch** — **CLOSED 2026-06-05** (Phase 8.0.1). Removed stale `# type: ignore[misc]` on `tools/patch_tool.py:219` after langchain-core stubs caught up. `mypy --strict tools/patch_tool.py` → 0.

- **DEBT-002 — agents/contract_guard.py: MODEL_MEDIUM not explicitly exported** — **CLOSED 2026-06-13** (verified post-Phase 8.0.2). `mypy --strict agents/contract_guard.py` → 0. The attr-defined error was resolved when `contract_guard.py:100` was changed to import `MODEL_MEDIUM` from `shared.config` directly (same fix as DEBT-015).

- **DEBT-003 — brain/swarms.py: BaseCheckpointSaver missing type args** — **CLOSED 2026-06-05** (Phase 8.0.0). `Optional[BaseCheckpointSaver]` → `Optional[BaseCheckpointSaver[Any]]` in `brain/swarms.py:189`.

- **DEBT-004 — brain/swarms.py: stale unused-ignore comments** — **CLOSED 2026-06-05** (Phase 8.0.0). Removed 8 stale `type: ignore` comments; 4 minimal targeted ignores retained for DEBT-014.

- **DEBT-006 — Inline diff / chat code had no syntax highlighting (shiki deferred)** — **CLOSED 2026-06-05** by Phase 7.16 (host-delegated tokenization). Engine moved to the host; webview paints scope-colored spans with `--vscode-*` CSS vars and zero grammar deps; `dist/workspace.js` 548.2 KB < 550 KB ceiling. Verified by the 7.16.3 checkpoint gate (10/10) + a permanent esbuild ceiling guard. Spawned DEBT-012 (`disableWordDiff` trade-off, still open).

- **DEBT-008 — Coding turns stream node-level narration, not LLM tokens** — **CLOSED 2026-06-05** (Phase 7.17.0-B / ADR-739). Thinking tokens stream via `acomplete_with_thinking` to Thought Box. Structured JSON answer buffers by design; residual tracked as DEBT-013.

- **DEBT-009 — MCTS variant-search is offline-only** — **CLOSED 2026-06-09** (Phase 7.19.2 / ADR-749). MCTS wired into the ReAct agentic cell (`brain/agentic_cell.py`) for multi-candidate fix paths; linear spine stays MCTS-free (MCTS-DEFER gate row). Multi-axis governor added in 7.19.3 (ADR-750).

- **DEBT-015 — agents/contract_guard.py: MODEL_MEDIUM import** — **CLOSED 2026-06-05** (Phase 8.0.2). Import redirected from `tools.llm_gateway` to `shared.config`. `mypy --strict agents/contract_guard.py` → 0.

- **DEBT-016 — brain/summarizer.py: strict-mode type-arg** — **CLOSED 2026-06-05** (Phase 8.0.2). `run_summarize_node` typed as `(state: Dict[str, Any]) -> Dict[str, Any]`. `mypy --strict brain/summarizer.py` → 0.

- **DEBT-018 — brain/memory.py: networkx GraphRAG has no memory bound** — **RESOLVED 2026-06-08** (Phase 8.1.B). `MAX_GRAPH_EDGES = 5000` cap-and-skip guard; deterministic `finally` teardown on both PPR builders. Regression: `test_oversized_graph_is_skipped_gracefully` + `test_at_cap_boundary_still_computes`.

- **DEBT-019 — api/websocket_manager.py: async request-buffer leak** — **RESOLVED 2026-06-08** (Phase 8.1.A). Guard-at-store drops late orphan responses; `disconnect()` wakes suspended waiters in O(1). Regression: `tests/test_ws_buffer_lifecycle.py` (6 cases).

- **DEBT-020 — tree-sitter stubs incomplete (6 × attr-defined, 1 × union-attr)** — **RESOLVED 2026-06-08** (Phase 8.1.C). `node: Any` / `tree: Any` retyping in `brain/prompt_builder.py`; local-variable narrowing guard in `brain/memory.py`. 7 ignores eliminated.

- **DEBT-021 — core/io_coalescer.py: bare Callable missing type parameters** — **RESOLVED 2026-06-08** (Phase 8.1.D). `Optional[Callable]` → `Optional[Callable[..., Any]]`; `asyncio.Task` → `asyncio.Task[None]`. 5 `type-arg` errors eliminated.

- **DEBT-022 — api/websocket_manager.py: 4 × arg-type on enum literals** — **RESOLVED 2026-06-08** (Phase 8.1.E). 4 broadcast method params narrowed from `str` to `Literal[...]` types; one cascading caller required `cast(Literal["success","error"], ...)`.

- **DEBT-023 — Miscellaneous single-site strict suppressions (5 ignores)** — **RESOLVED 2026-06-08** (Phase 8.1.F). `_require_token` typed; `DirtyBuffer` cast; `tup.checkpoint` cast; `Resolution` cast; `on_thinking` None guard. All 5 ignores eliminated.

- **DEBT-026 — MCP-discovered tools hardcoded to READ_ONLY (privilege fail-open)** — **RESOLVED 2026-06-10** (Phase 8.4.1). `classify_tool_privilege()` added to `core/permissions.py` (catalog > verb heuristic > DANGEROUS fail-closed). Dispatch-time trust-once valve tracked as DEBT-029 (also now resolved).

- **DEBT-029 — MCP tool dispatch consults no permission guard + no trust-once valve** — **RESOLVED 2026-06-11** (Phase 8.4.7). `McpToolAdapter._arun` resolves session context from ambient ContextVars; `_session_trust` dict per `(session_id, tool_name)`; FE `request_kind="MCP_TOOL_CALL"` card. 15 dispatch-guard tests green.

- **DEBT-030 — BYOM dashboard: no Google preset + `_ensure_v1` mangles native cloud endpoints** — **RESOLVED 2026-06-10** (Phase 8.4.8 + 8.4.9). `core/config/provider_registry.py` single source of truth for 12+ providers. Re-test 404 and OpenRouter double-`/v1` fixed in 8.4.9. `tested_models` cached per-endpoint. 20 new tests green.

- **DEBT-031 — MCP secret-value store + connect-time env injection** — **RESOLVED 2026-06-11** (Phase 8.4.6, load-bearing half). `core/config/mcp_secrets.py` backend-masked secret store (`0600`, atomic); env injection via `_build_stdio_params` at connect time. Config portability remainder tracked as DEBT-033.

---

## Appendix: Reproduction Quick-Reference

```powershell
# Run from: C:\Proyectos\Proyect_Ailienant\ailienant-core

# DEBT-005 (exploratory — count changes over time; 4 errors confirmed 2026-06-13)
.\venv\Scripts\python -m mypy --strict brain/engine.py 2>&1 | grep "error:" | wc -l

# DEBT-011 (pre-existing red gate test — heap baseline ceiling)
.\venv\Scripts\python -m pytest tests/test_phase3_checkpoint_gate.py::test_v3_tracemalloc_50_node_lifecycle_returns_to_baseline -q
```
