# DEV_JOURNAL — Active Phase 8 Engineering Log

Phase 0–7.19 history: see `docs/DEV_JOURNAL_ARCHIVE.md`.
Template (max ~12 lines per entry):

```
## [Phase]: [Short title] — YYYY-MM-DD
**Status:** COMPLETE | **Gates:** mypy 0/N · pytest N passed [· pyright 0 · npm compile 0]
- Shipped: [one sentence]
- Key decision: [one sentence — only if architecturally non-obvious; omit otherwise]
- Deferred: DEBT-N — [one sentence] (omit if none)
```

---

## 8.2.6.2: Skip embedding on an empty store — 2026-06-22
**Status:** COMPLETE | **Gates:** mypy 0/363 · pyright 0 · pytest 1669 passed
- Shipped: `search_with_paths` and `search_snippets` short-circuit via `is_corpus_empty(workspace_hash)` before `_get_embedding`, eliminating one embedding backend round-trip per turn on a cold workspace (behavior-preserving — the query path returns empty on an empty store either way); 3 new hermetic tests assert zero embeds on the cold path and a 2-embed regression guard on a populated corpus.
- Key decision: folded `search_snippets` (live-chat GraphRAG-injection path) into the same fix rather than deferring an identical optimization the DoD named only for `search_with_paths`.

## 8.2.6.1: Corpus-presence probe + empty-vs-low-coverage routing — 2026-06-22
**Status:** COMPLETE | **Gates:** mypy 0/363 · pyright 0 · pytest 1666 passed
- Shipped: `SemanticMemoryManager.is_corpus_empty(workspace_hash)` (30 s TTL, write-invalidated); `derive_routing_decision` gains `corpus_empty=False` additive param that skips the `css<40 → CLOUD` red-alert floor on empty workspaces; Researcher node probes and threads the flag; test_corpus_presence.py (5 rows) + 3 new routing assertions; Boy-Scout: pre-existing pyright `.metric` stub errors closed.
- Key decision: target file corrected from `agents/planner.py` (stale manifest reference, pre-DEBT-069) to `agents/researcher.py` (actual routing/cascade owner post-consolidation); §19 of SCHEMA_EVOLUTION and this entry reflect the live architecture.

## 8.10.14: Native LangGraph Suspend & Resume HITL — DEBT-070 — 2026-06-22
**Status:** COMPLETE | **Gates:** mypy 0/362 · pytest full green (gate 5 + finops/drift/cell suites migrated)
- Shipped: `core/hitl.py` substrate (`request_graph_approval` → `interrupt()`, `extract_pending_interrupt` via `aget_state`); in-graph HITL now suspends the graph and frees the runtime instead of pinning a coroutine. FinOps → single-node interrupt (committed-state gate); DriftMonitor → split `drift_compute`(commits the gate decision)→`drift_gate`(interrupt-first); agentic cell → defer the HITL-gated command to an interrupt-first exec-approval phase (no side effect replayed, command runs once). `task_service` detects the pause post-`astream` and `resume_graph` re-enters with `Command(resume=…)`; the WS `client_hitl_response` routes graph-paused sessions to resume.
- Key decision: a node that calls `interrupt()` commits no pre-interrupt writes and `astream` swallows `GraphInterrupt` (ends naturally) — so the interrupt-decision must come from a prior committed node (drift split) and detection is post-loop via state, never via `except`. Non-graph HITL (MCP, post-graph file-write apply loop) intentionally stays on the `request_human_approval` event channel.
- Deferred: DEBT-072 — pending-interrupt restart-durability (`recover()` must restore L2 pending writes).

## 8.10.13: Post-8.10.12 hardening — skeleton ceiling + state lifecycle — 2026-06-22
**Status:** COMPLETE | **Gates:** mypy 0/360 · pytest full green (gate 3 + suites)
- Shipped: explicit `_SKELETON_MAX_CHARS` truncation guard on the Researcher's skeleton output (defense-in-depth above `max_tokens=2048`); the Planner now clears the consumed `researcher_skeleton` from state so it no longer serializes into downstream coder / agentic-cell checkpoints.
- Key decision: a 3-point risk review found the state-bloat/OCC concern overstated (last-value channels overwrite; researcher→planner is sequential; `summarizer.py` already windows messages) and skeleton saturation already bounded by max_tokens — so only the one real bloat kernel (consumed skeleton lingering downstream) + an explicit ceiling were actioned. `mission_spec` is not pruned (the Coder needs it).
- Deferred: DEBT-071 — codebase-wide LangGraph `add_node` / langchain `args_schema` pyright errors (mypy gate clean; a dedicated typing slice).

## 8.10.12: Researcher node promotion + retrieval/routing consolidation — DEBT-069 — 2026-06-21
**Status:** COMPLETE | **Gates:** mypy 0/359 · pytest 1650 passed / 2 skipped (gate 6 + ~17 migrated)
- Shipped: promoted the Researcher to a first-class graph node (`researcher_agent`, spliced before `planner_agent`) with a bounded READ_ONLY `ToolDispatcher` grounding loop (`build_researcher_tools`); relocated all retrieval + the Context Meter Cascade + hardware reroute from the Planner to the Researcher, which now emits the routing signal (`context_metrics`/`css`/`tci`/`provider`/`routing_warning`) + a dense AST skeleton. The Planner is now a pure WBS engine that consumes that signal.
- Key decision: full single-shot SRP consolidation (user-directed) — the routing-spine math was relocated verbatim (same thresholds/order) so behavior is identical; the Planner keeps a defensive cold-default `context_metrics` so a Researcher bypass never propagates None. SCHEMA_EVOLUTION.MD §19 records the producer move.
- Deferred: none (DEBT-069 closed; Orchestrator dispatch-wiring remains permanently void per the DEBT-068 resolution).

## 8.10.11: Mutating-tier dispatch HITL routing — DEBT-068 — 2026-06-21
**Status:** COMPLETE | **Gates:** mypy 0/358 · pytest 9 passed (gate) · full suite 1644 passed / 2 skipped
- Shipped: `ToolDispatcher.dispatch` gains an injectable `approval_fn` (deny-with-report when absent/denied/raising) plus a `make_websocket_approval_fn` factory; the agentic cell's `run_terminal` now routes EXECUTE→HITL through the approval card via `_admit_execute` instead of treating HITL as ALLOW; `request_human_approval` default deadline raised 300s→86400s.
- Key decision: scope re-shaped to where mutation actually happens — Orchestrator (no LLM/reasoner) and Planner (PLAN-only, READ_ONLY) carry no dispatchable mutating surface, so HITL was proven on the existing coder ReAct loop (agentic cell) rather than bolting a parallel loop onto `coder.py`.
- Deferred: DEBT-069 — Researcher node promotion + its dispatch loop (8.10.12); DEBT-070 — replace async-sleep HITL waits with native LangGraph Suspend & Resume interrupts.

## 8.10.10: WBS contract correctness — DEBT-044 · DEBT-051 — 2026-06-20
**Status:** COMPLETE | **Gates:** mypy 0/357 · pytest 13 passed (gate) · full suite green
- Shipped: DEBT-044 — `WBSStep.depends_on: Optional[List[int]]` added additively; `ValidateWBSDependenciesTool` Pass 5 (Kahn's BFS) detects cycles and invalid references as blocking issues. DEBT-051 — `BackgroundTaskManager.create()` stamps `owner_role`; `list_tasks(caller_role)` filters non-orchestrator callers to their own tasks; orchestrator retains full visibility.
- Key decision: `owner_role`/`caller_role` flow as explicit tool input fields (additive, zero state-coupling) rather than via DI seam; sufficient for DoD and preserves backward-compatible defaults.

## 8.10.9: Infrastructure & UX quality — DEBT-011 · DEBT-037 · DEBT-033 — 2026-06-20
**Status:** COMPLETE | **Gates:** mypy 0/356 · pytest 1622 passed (2 skipped) · pyright 0 (changed files) · tsc 0 · eslint 0 (changed files)
- Shipped: DEBT-011 — heap-lifecycle test now self-calibrates (two-pass residual + `_HEAP_HEADROOM_RATIO`) instead of a fixed, unportable byte ceiling; green in isolation and in-suite. DEBT-037 — ablation retrieval degradation moved from `mock.patch` of internal class methods to dependency-injected callables (`graph_fn`/`planner_retrieval_fn`/`coder_retrieval_fn`) folded into `config["configurable"]`; agents fall back to their real methods when absent, so production is unchanged. DEBT-033 — extension gains an MCP config-import view with a credential dialog driven by the backend `needs_secret` signal (reuses the registry credential-store path).
- Key decision: retrieval DI is keyed off the existing config seam the runner already uses, so the deterministic core gains an explicit injection point with zero behavioral change rather than a benchmark-only patch.
- Deferred: none.

## 8.10.8: Runtime tool-dispatch activation (substrate + Analyst) + DEBT-032 — 2026-06-20
**Status:** COMPLETE | **Gates:** mypy 0/356 · pytest 1622 passed (2 skipped) · ruff clean · pyright 0 (changed files)
- Shipped: DEBT-066 — `core/tool_dispatch.py` (role-agnostic `ToolDispatcher`, `parse_tool_call_envelope`, `make_gateway_reasoner`) generalizing the agentic-cell prompt-enforced-JSON pattern; gated through `evaluate_action`; self-correcting (bad JSON / unknown tool → feedback turn, never a crash). Wired live on the Analyst via `build_analyst_tools(state)` + a bounded pre-grill loop in `run_analyst_node`; executed calls recorded on the additive `tool_dispatch_trace` channel. DEBT-032 — coder mirrors the planner skill-directive seam.
- Key decision: scope bounded to substrate + one READ_ONLY node (Analyst) so activation lands with zero mutation blast radius; prompt-enforced JSON chosen because the gateway returns text (no `bind_tools`).
- Deferred: DEBT-068 — wire Coder/Planner/Orchestrator + HITL routing (Researcher needs node promotion first) → 8.10.11.

## 8.10.7: Pre-launch gap audit (docs-only) — 2026-06-20
**Status:** COMPLETE | **Gates:** docs-only (no code, no type/test gates)
- Shipped: `DEVELOPERS.md` honest list updated — MCP adapter wiring marked shipped with floating deferrals (DEBT-029, DEBT-066) called out; tool catalog corrected from "16 of ~56" to ~50 built across six waves with DEBT-066 as the remaining cognitive-activation gap; Researcher and Orchestrator sections reflect built tool bundles and wired factories; prompt caching noted as planned for the pre-launch innovation sprint.

---

## 8.10.6: MEDIUM debts (four) + carve-out of Division 8.13 — 2026-06-20
**Status:** COMPLETE | **Gates:** mypy 0/354 · pytest 1603 passed (2 skipped) · npm compile 0 · npm lint 0
- Shipped: DEBT-024 — O(Δ) HITL transport (`ProposedFile.unified_diff`, `new_content` deprecated `Optional=None` §10-safe; server reads old via the VFS reader, EOL-normalizes, emits a `difflib` diff; host reconstructs via `applyPatch`, drift→stale). DEBT-041 — FTS5 **trigram** `file_lines` index (stdlib, feature-detected) populated by `LazyIndexer`; `GrepTool` superset-narrows the catalog (RAM ∪ FTS-hits ∪ index-lag) then regex-confirms, with a per-line cap + scan deadline. DEBT-048/050 — `get_task_service()`/`reset_task_service()` accessor; `RunBenchmarkTool` charges `ledger.consume_budget` upfront and `register_active_task(task_id)` with refund/release compensation. DEBT-053 — `BackgroundTaskManager.stop` async SIGTERM→5 s grace→SIGKILL/`taskkill /T /F`.
- Key decision: DEBT-035 re-scoped out (devcontainer overengineering/runtime-bias) → new **Division 8.13** (polyglot devcontainer execution layer, `docs/PHASE_8.13_BLUEPRINT.md`); split-by-trust keeps the untrusted oracle's locked Docker cage while the trusted agent execution moves to the extension-owned devcontainer adapter (§4 manifest amendment).
- Deferred: DEBT-035 → Division 8.13 (planned; adapter not yet implemented).

## 8.10.5: HIGH-tier architectural debts (DEBT-036 + DEBT-013) — 2026-06-19
**Status:** COMPLETE | **Gates:** mypy 0/352 · pytest 1586 passed (2 skipped)
- Shipped: DEBT-036 — additive `CodegenExecutor.run_workspace` moves oracle workspace materialization into the executor; the live path now isolates in the Docker sandbox (corpus+patch under the adapter mount, `python3 __oracle_main__.py` run by `cwd` so no host `sys.path` leaks) while the hermetic gate keeps the host subprocess. DEBT-013 — capability-gated streaming structured output: `astream_byom_thinking` preserves `response_format` for allow-listed providers (`{openai}`), self-healing degrade-once on rejection, sanitizer the universal fallback.
- Key decision: source the oracle workspace root from `DockerSandboxAdapter.host_workspace` (the single mount authority) rather than oracle-injection; harden the live path with `PYTHONDONTWRITEBYTECODE=1` (no root-owned `__pycache__`) and a strictly lexical pre-I/O path-traversal guard.

## 8.10.4: Division 8.6 — Phase 8 Checkpoint Gate (ADR-760) — 2026-06-19
**Status:** COMPLETE | **Gates:** mypy 0/351 · pytest 1574 passed (2 skipped) · npm compile 0
- Shipped: `tests/test_phase8_checkpoint_gate.py` (13 rows, test-only) re-certifying the cross-division Phase 8 contract against shipped entry points — A: resilience (8.2 fast-track/reroute/OOM-predictor/observability), B: H₁/H₂+Wilson reporting engine (8.3, pure-function), C: MCP fail-closed (8.4 unknown-verb⇒DANGEROUS, PLAN/WRITE deny, AUTO/DANGEROUS still HITL, trust-once tool-scoped), D: gateway HITL-degrade (8.5 deny-report under a 2s deadline, anti-escalation, ledger fail-closed).
- Key decision: the gate certifies the benchmark *reporting engine* (`build_report`), not the runner I/O — airtight & O(ms); every row pure/in-memory with isolated cleanup (unique `sid`+`try/finally`, `tmp_path` ledger, env `delenv`).

## 8.10.3: Division 8.2 — Resilience & Observability — 2026-06-19
**Status:** COMPLETE | **Gates:** mypy 0/350 · pytest 1561 passed (2 skipped)
- Shipped: Fast Track (`is_fast_track_eligible` pre-RAG skip → LOCAL_SMALL, CSS pinned so no false red-alert); env-gated `configure_langsmith()` (no new sink); config-driven VRAM gates + `hardware_reroute` (LOCAL_* below floor / predicted overflow → cloud, else LOCAL_SMALL+warning surfaced via `state["routing_warning"]` + `TelemetryPayload`); `core/graph_weight.py` OOM predictor judged against the candidate local window; chaos stress sim (synthetic profile injection); real HTTP/WS E2E that returns an applied patch.
- Key decision: E2E seals the cognitive engine at the `alienant_app.astream` boundary (Gateway pattern) and keeps the transport + write-pipeline + ack loop real; sync TestClient on its portal thread avoids the event-loop deadlock.
- Deferred: DEBT-067 — real RAM/VRAM stress-allocation script (the chaos sim uses synthetic injection).

## 8.10.2: Integration Wiring Sprint — DEBT-043 / 046 / 042 / 028 — 2026-06-15
**Status:** COMPLETE | **Gates:** mypy 0/343 · pytest 1527 passed (2 skipped)
- Shipped: DEBT-043 — `make_get_wbs_status_tool` / `make_emit_hitl_request_tool` + `build_orchestrator_tools(state)` bind the audited orchestrator tools to live graph state. DEBT-046 — `_gated_exec` + `_GatedExecTool` base + `make_coder_execute_tools(state)` thread `session_id`/`session_permission_mode` so EXECUTE-tier coder commands route through `evaluate_action` → `request_human_approval` (trust-once honored; `guard_env_file` excluded). DEBT-042 — `make_brave_search_fn()` lazily resolves the brave-search MCP session and is resilience-wrapped; analyst factories inject it. DEBT-028 — `_run_patch_hooks` runs `pre_patch`/`post_patch` commands through the sandbox adapter around the single `apply_patch_set` commit.
- Key decision: Scope = "correct-when-wired" (mirrors DEBT-040) — three tool classes are schema-registration-only with no LLM dispatch loop, so the factories make construction correct the moment dispatch lands; hooks delegate their ceiling to the adapter's own `timeout_s` (kills+reaps) rather than an outer `wait_for` that would orphan the child, and `pre_patch` fails closed on non-zero/timeout/no-adapter.
- Deferred: DEBT-066 — new HIGH "Cognitive Tool Activation": no runtime LLM tool-dispatch loop invokes the registered tools (factories ready); scheduled for a later intelligence/Agency phase.

## 8.10.1: Deployment Readiness — DEBT-034 / 038 / 040 — 2026-06-15
**Status:** COMPLETE | **Gates:** mypy 0/343 · pytest 1506 passed (2 skipped) · tsc 0 · eslint 0 errors
- Shipped: DEBT-034 — `project_id_for` now hashes `os.path.normcase(os.path.normpath(...))` and `PathResolver.computeProjectId` mirrors it (Node `path.win32/posix.normalize` + a trailing-separator strip that preserves the disk/UNC/POSIX root), so casing/separator/trailing-slash variants of one workspace key the same index (one-time lazy re-index on next open). DEBT-038 — relocated the 11-module benchmark harness (+ `corpus/` and `datasets/` fixtures) from `tests/benchmark/` to a shippable `core/benchmark/` package and repointed all imports; `run_benchmark` no longer reaches into the test tree. DEBT-040 — Explicit State Augmentation: the router writes `active_role = step.target_role` onto each `Send` payload, `_resolve_active_role` is config-first, and the ambient `_task_active_role` ContextVar was removed entirely.
- Key decision: the per-step role rides in immutable graph state (the `Send` payload), not an ambient ContextVar — thread-isolated with no cross-WS leakage; the router was the real gap (it inherited the task-initial role and never re-set it per step). The TS path replicates Python's trailing-slash rule explicitly because `path.normalize` keeps a non-root trailing separator that `normpath` strips.
- Deferred: none new. DEBT-040 residual logged in backlog — the agent-callable `tool_search` dispatch stays unwired (DEBT-043/046/054 cluster); this makes per-step selection correct now and resolution correct when that dispatch lands.

## 8.10.x: Deferred Backlog Fixes — DEBT-064 / 063 / 065 — 2026-06-14
**Status:** COMPLETE | **Gates:** mypy 0/341 · pytest 1499 passed · tsc 0 · eslint 0 errors
- Shipped: DEBT-064 — AILIENANT no longer surfaces/moves its own runtime files: `_build_tree` filters them at the enumeration source, `_run_coding_task` drops internal paths from the patch set (with a user note), and the VFS firewall ignores `.ailienant_telemetry.log*`; shared `is_ailienant_internal_path` (core/storage_paths.py). DEBT-063 — `planner.parallel_tasks=[]` forces sequential RELAY execution (WBS steps have only implicit step_number ordering, so the `tci>80` blanket SWARM fan-out was unsafe). DEBT-065 — `_format_coding_summary` gains a backward-compatible `auto_apply` branch ("Applying…" vs "review/authorize").
- Key decision: the telemetry log isn't a code file (not in `_EXT_LANG`), so it reached the agent via the workspace tree's raw `os.walk`, not the indexed catalog — the fix is the tree + write-layer guard, not a catalog filter. The VFS ignore is scoped to the log only (not `.ailienant/`) so the user-authored `.ailienant/AILIENANT.md` instructions stay readable.
- Deferred: none new. SWARM dispatch left dormant for a future explicit-dependency-DAG (the only way to safely re-introduce parallelism).

## 8.10.0b: FE Regression Follow-up — HUD Height + Context Ring — 2026-06-14
**Status:** COMPLETE | **Gates:** tsc 0 · eslint 0 errors · mypy 0/340 · pytest 1494 passed
- Shipped (DEBT-062): fixed the HUD height regression DEBT-056 introduced (composer + telemetry share `--hud-rest-height`, equal at rest via `flex-end`); merged the OCC ring and context meter into one split donut (`OccContextRing` — left OCC palette, right `--accent-context` lavender deepening with occupancy); per-model context window resolved from litellm `get_model_info` instead of the flat 200k default; apply-result paths backtick-wrapped so `*_telemetry.log` no longer renders italic.
- Key decision: only the HUD height was a true regression from my change; the context window/plan-order/telemetry-log-apply issues were pre-existing — fixed the regression + the requested ring redesign now and logged the rest rather than fold backend orchestration into an FE pass.
- Deferred: DEBT-063 (plan executes out of WBS order), DEBT-064 (agent organizes its own `.ailienant/` runtime files → OCC stale-apply; root cause of the live test failure), DEBT-065 (Auto-mode "authorize" wording). Live used-tokens may still read 0 (coding-task path may not populate the L1 `messages` channel) — diagnostic logged in `compute_context_occupancy` pending a runtime trace.

## 8.10.0: Emergency FE Regressions — 2026-06-14
**Status:** COMPLETE | **Gates:** tsc 0 · eslint 0 errors (2 pre-existing warnings untouched) · esbuild 0
- Shipped: Natt-pane scroll (`min-height:0` on the `.ws-natt-body` grid track); `scrollHeight` composer auto-resize via a shared `useAutoResizeTextarea` (`useLayoutEffect`) hook wired into PromptBar + NattPromptBar; diff-authorize card no longer duplicates on tab-switch (idempotent `server_plan_document` + content-based host re-post guard); pipeline trace redesigned from a bordered box to an inline borderless trace.
- Key decision: DEBT-060's duplicate bubble was the host re-posting the latest plan on every panel reveal under a guard that matched only the plan-surface `"Drafted a plan"` phrasing; fixed by making the webview summary-append idempotent by content (charter §5.3) with a content-based host guard as defense in depth — not by suppressing the panel restore.
- Deferred: original WBS paths were stale (`src/webview/`, `PlannerSession.tsx` — neither exists); the real surface is `src/workspace/`. (DEBT-055/056 scroll+resize; DEBT-060 diff card; DEBT-061 pipeline trace — renumbered from a collision with existing backlog IDs.)

## 8.9: Portable Workspace Home (`.ailienant/` Provisioning) — 2026-06-14
**Status:** COMPLETE | **Gates:** mypy 0/340 · pytest 1494 passed · tsc 0 · eslint 0 errors
- Shipped: global stores relocated from CWD to `~/.ailienant/` via `shared.config` home defaults; new `core/storage_paths.py` partitions only the GraphRAG semantic store per project (`projects/<id>/lancedb/`, bound on `client_workspace_init`); freeform `AILIENANT.md` instructions injected into planner+coder prompts; navigable `dump_plan_to_markdown` plan export; extension first-run provisioning of `.ailienant/` + starter `AILIENANT.md` + marked `.gitignore` block; `test_phase8_9_checkpoint_gate.py` (8 rows).
- Key decision: hybrid storage (Option C) — the catalog/MCTS/ledger and the `ailienant_product_docs`/trajectory LanceDB tables stay global because they are shared across projects (isolated by `project_id`/`workspace_hash` column); only `workspace_embeddings` is physically per-project, so out-of-process/dashboard consumers resolve it from an explicit `graphrag_lancedb_path_for(project_id)`.

## 8.8.8: Division 8.8 Checkpoint Gate — 2026-06-14
**Status:** COMPLETE | **Gates:** mypy 0/339 · pytest 1486 passed · ruff clean
- Shipped: `tests/test_phase8_8_tool_parity_gate.py` — 5 tests (R1a integrity · R1b READ_ONLY retrievability · R2 RBAC negative · R3 reduction ≥70% at full catalog scale · R4 ISO role-contract snapshot) certifying all 12 `register_*_tools` over an isolated store.

## 8.8.7: Wave 6 Universal Tools — 2026-06-14
**Status:** COMPLETE | **Gates:** mypy 0/339 · pytest 1486 passed · ruff clean
- Shipped: `tools/universal_tools.py` (`TodoWriteTool` / `todo_write`, READ_ONLY, ALL_ROLES 12-role universe); `brain/state.py` additive `agent_todos` channel; `ALL_ROLES` constant in `control_tools.py`; `tool_search` cross-listed to all 12 roles in `meta_tools.py`; 28 tests.
- Key decision: `_merge_todos` reducer tests `right is not None` (not truthiness) so an explicit `[]` clears the panel and TODO immortality is impossible.
- Deferred: DEBT-054 — `todo_write` / `agent_todos` channel have no runtime call site; wiring into a cognitive node deferred.

## 8.8.6: Wave 5 Gateway/Benchmark Tools — 2026-06-14
**Status:** COMPLETE | **Gates:** mypy 0/334 · pytest 33 new passed · full suite 1465 passed
- Shipped: 6 net-new RBAC-gated tools in `tools/gateway_tools.py` (`run_benchmark`, `get_benchmark_report`, `list_capabilities`, `skill_invoke`, `task_list`, `task_stop`) wrapping the 8.5 benchmark substrate, gateway catalog, and skill resolver; `task_create`/`task_get` extended to include `orchestrator` role (Task V2).
- Key decision: `BackgroundTaskManager` extended with `_procs` dict, `list_tasks()`, and `stop()` (cancel-wins race guard + `finally`-block pop to prevent zombie references); `GetBenchmarkReportTool` uses `asyncio.to_thread` for disk I/O; `_cleanup_benchmark` is a named function (not a lambda) for proper `exc_info` logging.
- Deferred: DEBT-048 — `RunBenchmarkTool` skips `task_service.register_active_task` (task_id not visible via check_task_status); DEBT-049 — `SkillInvokeTool` `embed_fn=None` (explicit only); DEBT-050 — unbudgeted internal benchmark invocations; DEBT-051 — task_list cross-role visibility; DEBT-052 — potential sync DB calls under `resolve_active_skills`; DEBT-053 — SIGTERM only, no SIGKILL escalation.

## 8.8.5: Wave 4 Role-Specific Coder Tools — 2026-06-14
**Status:** COMPLETE | **Gates:** mypy 0/332 · pytest 27 new passed · full suite 1432 passed (1 latent 8.8.4 defect fixed)
- Shipped: 10 net-new role-exclusive coder tools + `ASTValidateTool` in new `tools/coder_tools.py` (thin wrappers over the sandbox adapter / `validate_ast` / patch engine), and re-mirrored the 4 formalize tools' `allowed_roles` to `agents/roles.py` per capability (`mutation_tools` split into 3 per-tool sets; `sandbox_bash` given its own `_SANDBOX_BASH_ROLES`).
- Key decision: Zero-Trust Bash — a shared `_safe_arg` guard rejects flag injection, path traversal, and absolute paths before `shlex.quote`; `--` is an extra layer only for GNU-getopt CLIs, never relied on for python/pip; `git_diff` is EXECUTE (it spawns) and `guard_env_file` is DANGEROUS/content-hash-idempotent. Net behavior delta: `core_dev`/`secops` lose `sandbox_bash`, `vcs_manager` gains it.
- Fixed: latent 8.8.4 `UnboundLocalError` — `_bud` was bound inside the planning branch, unbound on the cache-hit / dirty-buffer bypass path; hoisted above the branch.
- Deferred: DEBT-046 — EXECUTE/DANGEROUS wrappers rely on tier-gating, not `sandbox_bash`'s interactive HITL-card plumbing; DEBT-047 — `generate_docstring` is line-anchored, not a signature-aware renderer.

---

## 8.8.4: Wave 3b Planner Pre-Commit Verification (deterministic) — 2026-06-14
**Status:** COMPLETE | **Gates:** mypy 0/330 · pytest 27 new passed · regression 112 passed (planner + perception + 8.8.0–8.8.3)
- Shipped: 2 net-new Planner tools (`validate_wbs_dependencies`, `estimate_plan_budget`) in new `tools/planner_tools.py` + Planner wire-in on `workspace_structure`, `get_dependents` (researcher_tools) and `inspect_ast_node` (perception_tools); deterministic pre-commit hook in `agents/planner.py` raises `ValueError` on ordering violations, feeding the existing `MAX_PLANNER_RETRIES` loop with structured per-step/file feedback.
- Key decision: forward-reference detection scoped to files the plan explicitly creates (`write_file` steps only) — pre-existing files assumed present to avoid false positives; `BudgetEstimatorTool` is advisory (never raises, stored via LangGraph `result` dict, not in-place state mutation per Fix 1).
- Deferred: DEBT-044 — true DAG cycle detection requires `depends_on` on `WBSStep` (schema migration deferred); DEBT-045 — `BudgetEstimatorTool` heuristic not calibrated from session history.

---

## 8.8.3: Wave 3 Orchestrator Introspection (deterministic) — 2026-06-13
**Status:** COMPLETE | **Gates:** mypy 0/328 · pytest 19 new passed · regression 77 passed (control + tool_rag + 5.7 + 8.8.0 + 8.8.1 + 8.8.2)
- Shipped: 2 net-new orchestrator tools (`get_wbs_status`, `emit_hitl_request`) in new `tools/orchestrator_tools.py` + orchestrator wire-in on `ask_user_question`, `toggle_plan_mode` (control_tools) and `read_token_ledger` (analyst_tools), via additive `allowed_roles` parametrization of both `_control_schema` and `_tool_schema`.
- Key decision: §4 pivot — `GetTokenLedgerTool` dropped as a duplicate of 8.8.2's `read_token_ledger`; orchestrator wired into that schema instead (2 net-new · 3 wire-in). `emit_hitl_request` idempotency rests on a deterministic `blake2b(flag)` id, not the audit-only `hitl_approval_requests` channel (survives a dropped checkpointer turn); LLM-controlled flag fields are colon/newline-sanitized; `get_wbs_status` guards `tasks` via `getattr(..., None) or []` against a TypeError crash.
- Deferred: DEBT-043 — orchestrator tools register but are not yet bound into the live graph node (state-injecting factories + tool-set binding deferred to a graph-wiring sprint).

## 8.8.2: Wave 2 Analyst Quality Lens (READ_ONLY) — 2026-06-13
**Status:** COMPLETE | **Gates:** mypy 0/326 · pytest 28 new passed · regression 68 passed (perception + tool_rag + 5.7 + 8.8.0 + 8.8.1)
- Shipped: 6 net-new analyst tools (run_linter, analyze_complexity, audit_dependencies, diff_changes, web_search, read_token_ledger) + analyst wire-in on 4 perception tools (inspect_ast_node, get_symbol_references, trace_data_flow, web_fetch); `_jailed_disk_read` workspace-jail helper; `VFSMiddleware.read_ram_only()`.
- Key decision: All disk reads confined by `_jailed_disk_read` (pathlib.resolve().is_relative_to); CodeDiffTool uses itertools.islice over difflib.unified_diff for O(min(N, 300)) memory; ComplexityAnalysisTool catches both SyntaxError and RecursionError; DependencyAuditTool uses .get() on both package.json dep keys to prevent KeyError.
- Deferred: DEBT-042 — WebSearchTool and DependencyAuditTool search_fn injection unwired (brave-search MCP wiring deferred to integration sprint).

## 8.8.1: Wave 1 Researcher Arsenal (READ_ONLY) — 2026-06-13
**Status:** COMPLETE | **Gates:** mypy 0/324 · pytest 16 new passed · regression 52 passed (perception + tool_rag + 5.7 + 8.8.0)
- Shipped: 5 net-new researcher tools (glob, grep, workspace_structure, query_graphrag, get_dependents) + read_file schema formalization + researcher wire-in on 4 perception tools; shared `tools/quarantine.py`; lock-safe `VFSMiddleware.snapshot_paths()`; `core.db.list_indexed_files`.
- Key decision: Role namespace is flat — `"researcher"` in `allowed_roles` uses the same string-membership predicate as the 8 coder sub-roles; GrepTool short-circuit is O(max_matches) with `asyncio.to_thread` offload; path provider canonicalizes both VFS and catalog paths via `normcase`+`normpath` before set-union to prevent casing/separator collisions on Windows.
- Deferred: DEBT-041 — GrepTool sequential catalog scan; inverted index + ReDoS-bounded matcher deferred to 8.8.2.

## 8.0.1: Unsilence shared.hardware + agents.analyst + tools.patch_tool — 2026-06-05
**Status:** COMPLETE | **Gates:** mypy 0/247 · pytest 924 passed
- Shipped: Removed `follow_imports=silent` for 3 leaf modules; fixed 12 type errors (unused-ignore in psutil/pynvml stubs, `Set[Task[Any]]`/`Dict[str,Any]` in analyst, bare `dict` in ideation/swarms).
- Key decision: Corrected `brain/ideation.py` errors in the same pass since they were not blocked by `agents.analyst` as the blueprint claimed.
- Deferred: DEBT-001 closed (LangChain `@tool` stubs arrived).

## 8.0.2: Unsilence tools.llm_gateway — Repair Consumers — 2026-06-05
**Status:** COMPLETE | **Gates:** mypy 0/247 · pytest 924 passed
- Shipped: Removed llm_gateway silence; fixed 3 consumers — `contract_guard.py` re-routed `MODEL_MEDIUM` import, `summarizer.py` dict→`Dict[str,Any]`, `coder.py` 5 type-arg fixes.
- Deferred: DEBT-014 updated (NodeInputT strict/non-strict discrepancy in swarms.py:155 persists).

## 8.0.3: Unsilence core.vfs_middleware + core.compute_pool — 2026-06-05
**Status:** COMPLETE | **Gates:** mypy 0/247 · pytest 924 passed
- Shipped: Typed `VFSMiddleware.__new__` return; `pathspec.PathSpec[Any]`; `FrozenSet[str]` — unlocking 5 downstream `no-untyped-call` ignores in indexer/researcher/task_service/graphrag_extractor that were removed in the same pass.

## 8.0.4: mypy --strict main.py → 0 (primary campaign goal) — 2026-06-08
**Status:** COMPLETE | **Gates:** mypy 0/247 · pytest 924 passed
- Shipped: `tool_rag_select_node` retyped to `AIlienantGraphState` (satisfies LangGraph `NodeInputT` bound); eliminated the last `type-var` ignore at swarms.py:155.
- Deferred: DEBT-014 reduced to 3 retained ignores (coder/planner/analyst nodes — retyping cascades to 63 `arg-type` errors in 19 callers; deferred to a dedicated migration).

## 8.0.5: Unsilence brain.memory + core.db — 2026-06-08
**Status:** COMPLETE | **Gates:** mypy 0/247 · pytest 924 passed
- Shipped: Both modules were already strict-clean; added `[mypy-networkx,networkx.*] ignore_missing_imports=True` (full top-level + submodule glob required) and removed 2 inline ignores from `brain/memory.py`.
- Deferred: DEBT-018 logged — networkx graphs in GraphRAG have no eviction; O(V+E) RAM growth in long VS Code sessions.

## 8.0.6: Unsilence api.websocket_manager — Last Silent Module — 2026-06-08
**Status:** COMPLETE | **Gates:** mypy 0/247 · pytest 924 passed
- Shipped: Typed `_hitl_responses` and `_patch_ack_results` as `Dict[str, Dict[str, Any]]` (dict invariance ensures JSON-serializable keys at the socket layer); zero `follow_imports=silent` blocks remaining.
- Deferred: DEBT-019 logged — `_hitl_responses` / `_patch_ack_results` accumulate orphaned entries when the waiter times out or cancels; `disconnect()` does not sweep them.

## 8.0.7: brain/engine.py Certified Strict-Clean — 2026-06-08
**Status:** COMPLETE | **Gates:** mypy 0/247 · pytest 924 passed
- Shipped: Verified `brain/engine.py` passes `mypy --strict` with zero code changes (errors were transitive through already-fixed modules); campaign integrity confirmed.

## 8.0.8: Campaign Closure — Ignore Audit + Config-Level Cleanup — 2026-06-08
**Status:** COMPLETE | **Gates:** mypy 0/247 · pytest 924 passed
- Shipped: Audited all 35 remaining inline ignores; retired 7 stale ones; moved lancedb/docker/requests library suppression from inline to `mypy.ini` blocks.
- Deferred: DEBT-020 (7 tree-sitter `import` ignores), DEBT-021 (5 io_coalescer ignores), DEBT-022 (4 broadcast Literal ignores), DEBT-023 (5 misc single-site ignores).

---

## 8.1.A: DEBT-019 — WebSocket Buffer Leak Fix — 2026-06-08
**Status:** COMPLETE | **Gates:** mypy 0/248 · pytest 930 passed
- Shipped: Guard-at-store in `resolve_hitl_response`/`resolve_patch_ack` rejects stale waiters; `sweep_and_wake` called in `disconnect()` purges orphaned entries and unblocks any surviving waiters; `tests/test_ws_buffer_lifecycle.py` (6 rows).

## 8.1.B: DEBT-018 — NetworkX Graph Eviction — 2026-06-08
**Status:** COMPLETE | **Gates:** mypy 0/248 · pytest 932 passed
- Shipped: `MAX_GRAPH_EDGES=5000` hard cap in both networkx builders; `G.clear()` in `finally` teardown on session close.

## 8.1.C: DEBT-020 — Tree-sitter Type Ignores — 2026-06-08
**Status:** COMPLETE | **Gates:** mypy 0/248 · pytest 932 passed
- Shipped: 7 tree-sitter `# type: ignore[import]` resolved via `param: Any` annotations and a local `node: Any` guard variable.

## 8.1.D: DEBT-021 — io_coalescer Type-Arg Ignores — 2026-06-08
**Status:** COMPLETE | **Gates:** mypy 0/248 · pytest 932 passed
- Shipped: 5 ignores resolved by annotating `asyncio.Task[None]` and `Callable[..., Any]` at the two coalescer call sites.

## 8.1.E: DEBT-022 — WebSocket Manager Literal Narrowing — 2026-06-08
**Status:** COMPLETE | **Gates:** mypy 0/248 · pytest 932 passed
- Shipped: 4 broadcast param ignores resolved with `Literal` type narrowing in `websocket_manager.py`; 1 `cast` in `task_service.py`.

## 8.1.F: DEBT-023 — Misc Single-Site Ignores — 2026-06-08
**Status:** COMPLETE | **Gates:** mypy 0/248 · pytest 932 passed
- Shipped: 5 remaining ignores closed: main.py middleware cast, sessions.py checkpoint cast, resource_manager `Resolution` cast, llm_gateway `on_thinking` guard annotation.

---

## 8.2: Mode Flow Fix — Plan Panel + submitWithMode Race — 2026-06-08
**Status:** COMPLETE | **Gates:** npm check-types 0 · npm lint 0
- Shipped: Plan panel now gated to `planner_mode_active=true`; `PlanAcceptancePanel` (55/45 split); `submitWithMode` avoids async `setMode` race by bundling mode into the submit payload; stale plan cleared on new task.

## 8.3: CoderAgent SEARCH/REPLACE Format — 2026-06-08
**Status:** COMPLETE | **Gates:** mypy 0/248 · pytest 21 target tests passed
- Shipped: Replaced fragile code-in-JSON edits with structured SEARCH/REPLACE blocks (`### EDIT` / `<<<SEARCH` / `======` / `>>>REPLACE`); `_clean_block` strips leading/trailing newlines.

## 7.19 Planning: Agentic Cell WBS + Blueprint — 2026-06-08
**Status:** COMPLETE | **Gates:** docs-only
- Shipped: `docs/PHASE_7_19_BLUEPRINT.md` created; 9-sub-phase WBS locked (7.19.0–7.19.8); ADR-750 governor spec ratified.

## 8.4: ASK Mode — Proposed Files in HITL Payload + Inline Diff — 2026-06-08
**Status:** COMPLETE | **Gates:** mypy 0/248 · npm compile 0
- Shipped: `proposed_files` field in `HITLApprovalRequestPayload`; `PatchActuator.preview()` renders unified diff inline in the chat approval card before write; atomic React `useTransition` commit.
- Deferred: DEBT-024 logged — diff inline rendering performance on large changesets.

## 8.5: WebSocket Multiplexing — Single O(1) Socket — 2026-06-09
**Status:** COMPLETE | **Gates:** mypy 0/248 · npm compile 0 · pytest 947 passed
- Shipped: `_aliases` registry maps session IDs to a single connection; `ws_client.ts` demultiplexer dispatches by `session_id`; `SessionManager.forSession` factory-cache; re-announce on reconnect.
- Key decision: Single WS per window (not per session) eliminates thundering-herd reconnect storms on VS Code reload.

## 8.6: Post-MUX Sanitation — HITL + Plan Mode Fixes — 2026-06-09
**Status:** COMPLETE | **Gates:** mypy 0/249 · npm compile 0 · pytest 952 passed
- Shipped: HITL card moved to main chat (not NattCanvas); request-changes loop re-submits without a new session; `timeout_s=None` for interactive HITL; `_resolve_target_role` reducer for parallel coders; 3 plan-mode bugs fixed (stale plan clear, keep-planning, INVALID_CONCURRENT state).

## 8.7: Planner Scope Discipline — 2026-06-09
**Status:** COMPLETE | **Gates:** mypy 0/250 · npm compile 0 · pytest 957 passed
- Shipped: `_SCOPE_DISCIPLINE_DIRECTIVE` constant injected into planner system prompt; `_DEEP_CONTEXT_MIN_SIM=0.20` semantic gate filters low-relevance context before injection; per-file sequential approval loop; collapsible diffs (`collapsed=true` by default).

---

## División 8.7: Analyst Tri-Brain — 2026-06-11
**Status:** COMPLETE | **Gates:** mypy 0/276 · npm compile 0 · npm lint 0 · pytest 1117 passed · test_analyst_brains 14/14
- Shipped: `docs_index.py` (idempotent `asyncio.Lock+filelock`, `search_ailienant_docs`); `readme_digest.py` (debounced 7 s, SHA-256 change-detect cache); `ContextBudgetManager` (5-tier escalator, 60% hard-cap, backfill); directional model fallback; `AnalystModelPicker` FE.
- Key decision: Docs index uses filelock over asyncio lock alone so parallel host processes don't corrupt the shared index file.

## Phase 10 / Docs: License + Developer Documentation — 2026-06-11
**Status:** COMPLETE | **Gates:** docs-only
- Shipped: AGPL-3.0 dual license; `CLA.md`; 7 language READMEs; `HowItWorks.md`, `HowToUseIt.md`, `DEVELOPERS.md`, `CONTRIBUTING.md`; `assets/` directory with icon and logo variants.

---

## 8.4.0+8.4.1: classify_tool_privilege() — Fail-Closed MCP Tier Assignment — 2026-06-10
**Status:** COMPLETE | **Gates:** mypy 0/264 · pytest 1063 passed
- Shipped: `classify_tool_privilege()` with fail-closed precedence (curated catalog > verb heuristic > DANGEROUS default); camelCase `_TOKEN_SPLIT`; severity-max aggregation; `_PRIVILEGE_CATALOG` seam for overrides.

## 8.4.2: Curated MCP Registry — 2026-06-10
**Status:** COMPLETE | **Gates:** mypy 0/270 · pytest 1095 passed
- Shipped: `core/mcp_registry.py` — `RegulatedServer` frozen dataclass, 4 built-in server entries, `tool_tiers` map; `init_registry()` + `register_privilege_overrides()` seam.

## 8.4.3: MCP Config Import/Export — 2026-06-10
**Status:** COMPLETE | **Gates:** mypy 0/272 · pytest 1103 passed
- Shipped: `.ailienant/config.json` portable format (no secrets, `key_ref` only); `_redact_uri_credentials` regex; HTTP 422 on `McpConfigError`; allowlist guard on import; case-insensitive server-name reconcile.

## 8.4.4: Auto-Connect MCP Servers — 2026-06-11
**Status:** COMPLETE | **Gates:** mypy 0/273 · pytest 1116 passed
- Shipped: Multi-session `ClientSession` registry keyed by `server_name`; idempotent bootstrap; `evaluate_action` dispatch-guard via injected kwargs in `_arun`; `autoconnect_enabled_mcp_servers` in lifespan.
- Deferred: DEBT-027 closed.

## 8.4.5: Skills Execution Wiring — 2026-06-11
**Status:** COMPLETE | **Gates:** mypy 0 · npm compile 0 · pytest full green
- Shipped: Dual-mode resolver (cosine ≥0.45 Mode-1, explicit Mode-2); `build_skill_directive_block` with uuid4 boundary sandboxing; schema migration adding `description`/`enabled`/`scope` columns.
- Deferred: DEBT-028 (skills half) closed; DEBT-032 logged (coder-side skill invocation).

## 8.4.6: Browse Registry UX — 2026-06-11
**Status:** COMPLETE | **Gates:** pytest 59 focused + suite green · mypy 0/280 · tsc 0 · eslint 0
- Shipped: `mcp_secrets.py` atomic 0600 writes + masked re-submission guard; `serialize_registry(installed_names)`; `_build_stdio_params` with `shutil.which` for Windows `npx.cmd`; close-first on re-install; frontend tier-badge cards.
- Deferred: DEBT-031 closed; DEBT-033 logged (`key_ref` round-trip not verified end-to-end).

## 8.4.7: HITL Live on Real MCP Tool — 2026-06-11
**Status:** COMPLETE | **Gates:** mypy 0/280 · tsc 0 · eslint 0 · test_mcp_dispatch_guard 15/15
- Shipped: `ContextVar` ambient session injection (`_task_session_id`/`_task_session_mode`); trust-once valve (`_session_trust` dict); lazy `vfs_manager` channel closure default; `MCP_TOOL_CALL` frontend binding.
- Deferred: DEBT-029 closed. Division 8.4 CLOSED.

---

## 8.5.0: External Gateway Blueprint — 2026-06-11
**Status:** COMPLETE | **Gates:** docs-only
- Shipped: D1-D8 architectural decisions ratified (loopback EXECUTE, in-process READ_ONLY, host discovery via run.json, durable ledger, conservative posture, deny-report HITL-degrade, poll-pair, semver, symmetric perms).

## 8.5.1: Gateway Framework — stdio MCP Server + Host Discovery — 2026-06-11
**Status:** COMPLETE | **Gates:** mypy 0/286 · pytest 1192 passed · test_gateway_framework 15/15
- Shipped: `gateway/` package — `catalog.py` SSoT, `server.py` low-level dispatch with `dispatch_call` seam, `__main__.py` standalone stdio entry; `host_discovery.py` (`write_run_state` 0600, `probe_host_alive` async TCP, `resolve_host_or_error`).

## 8.5.2: Tier Governance — Durable Ledger + Anti-Escalation — 2026-06-12
**Status:** COMPLETE | **Gates:** mypy 0/289 · pytest 1209 passed · test_gateway_governance 17/17
- Shipped: `gateway/ledger.py` — durable JSON per-caller token-bucket + budget, dedicated `.lock` filelock, clock-skew hardened; `gateway/governance.py` — `authorize_invocation`, `resolve_internal_task_mode` anti-escalation, `register_gateway_privileges`.

## 8.5.3: HITL-Degrade Deny-Report — 2026-06-12
**Status:** COMPLETE | **Gates:** mypy 0/290 · pytest 1212 passed · test_gateway_hitl_degrade 3/3
- Shipped: Structured deny envelope (`status/reason/capability/tier/would_have_required/message`); `_denied()` delegates to `_envelope`; no `await` in the deny path by construction (structurally never hangs).

## 8.5.4: Capability Catalog v1 — In-Process + Loopback Handlers — 2026-06-12
**Status:** COMPLETE | **Gates:** mypy 0/292 · pyright 0 · test_gateway_catalog_v1 14/14 · gateway suites 49/49
- Shipped: `gateway/handlers.py` — `CAPABILITY_HANDLERS` dict with in-process READ_ONLY and loopback EXECUTE handlers; `get_task_status`; race between `submit` and `register` closed.

## 8.5.5: Eval Surface — run_benchmark + get_report — 2026-06-13
**Status:** COMPLETE | **Gates:** mypy 0/317 · pyright 0 · test_gateway_eval_surface 17/17 · gateway 46/46 · suite 1303 passed
- Shipped: `core/benchmark_service.py` — LFI-hardened `_resolve_artifact` (uuid4-only regex + `is_relative_to` confinement), single-flight `_inflight` with done-callback release, durable artifact-file completion signal, pay-upfront refund on failure.
- Key decision: `_inflight` released via done-callback (not inside `run_benchmark`) so a benchmark fault cannot leak the slot — canonical pattern for all future single-flight operations (Engineering Invariant 5.1).

## 8.5.6+8.5.7: Versioning + Auth Ergonomics + Integration Docs + DoD — 2026-06-13
**Status:** COMPLETE | **Gates:** mypy 0/318 · pyright 0 · test_gateway_dod 3/3 · gateway 68/68 · suite 1308 passed
- Shipped: `PROTOCOL_VERSION` 1.0.0 single-sourced in `catalog.py` + advertised per-tool in `list_tools()._meta`; `Capability` deprecation mechanism (null sunset keys omitted from the wire); safe masked boot line (boolean token check, never logs the value); `docs/GATEWAY_INTEGRATION.md`; DoD gate (catalog discovery, READ_ONLY `ok`, DANGEROUS → `requires_human_approval` deny-report under `asyncio.wait_for` proving non-hang). **División 8.5 CLOSED.**
- Key decision: surface version advertised in `list_tools()._meta` (not a new introspection verb) — keeps the v1 catalog frozen at 7 while satisfying D7's "declares its version."

---

## 8.3.0: Benchmark Harness Scaffold — 2026-06-12
**Status:** COMPLETE | **Gates:** mypy 0/299 · pyright 0 · test_harness_scaffold 7/7 · suite 1233 passed
- Shipped: `tests/benchmark/` package — `arms.py`, `metrics.py`, `hygiene.py`, `runner.py`, `problems.py`; hermetic gate `test_harness_scaffold.py`.

## 8.3.1: Pass@1 Adapter — HumanEval/MultiPL-E — 2026-06-12
**Status:** COMPLETE | **Gates:** mypy 0/302 · pyright 0 · test_codegen_pass1 8/8 · suite 1241 passed
- Shipped: `SandboxCodegenExecutor` + `SubprocessPythonExecutor`; robust `extract_code` with fence-first then heuristic fallback.
- Deferred: DEBT-035 logged (TypeScript runtime executor not yet implemented).

## 8.3.2: BenchmarkOracle — Resolve@k + Corpus v1 — 2026-06-12
**Status:** COMPLETE | **Gates:** mypy 0/308 · pyright 0 · test_oracle_resolve_k 12/12 · suite 1253 passed
- Shipped: `BenchmarkOracle` with Resolve@k metric; `tests/benchmark/corpus/v1/` multi-file problem corpus; `asyncio.Event` on `LazyIndexer`; AST pre-flight safety check.
- Deferred: DEBT-036 logged (corpus v2 language expansion).

## 8.3.3: Ablation Harness — G1–G4 + FORCE_CLOUD — 2026-06-12
**Status:** COMPLETE | **Gates:** mypy 0/310 · pyright 0 · test_ablation_verdicts 8/8 · suite 1261 passed
- Shipped: Strategy objects for G1–G4 + G4_FORCE_CLOUD configurations; `_graph_task_runner` via `ainvoke`; snapshot-then-clear drain pattern; path normalization for cross-platform corpus.
- Deferred: DEBT-037 logged (baseline calibration run against live model).

## 8.3.4: Routing Study H₂ — TCI-Bucket × Tokens × Resolve@3 — 2026-06-12
**Status:** COMPLETE | **Gates:** mypy 0/312 · pyright 0 · test_routing_study 9/9 · suite clean
- Shipped: TCI-bucket × token-count × Resolve@3 cross-tabulation; anchored bucketing; strict pairing (same problem per strategy); `_prepare_run` refactor for composability.

## 8.3.5+8.3.6: Report Generator + Reproducibility DoD — 2026-06-13
**Status:** COMPLETE | **Gates:** mypy 0/315 · pyright 0 · test_report 13/13 · test_reproducibility 3/3 · tests/benchmark 60/60 · suite 1286 passed
- Shipped: `BenchmarkReport` dataclass; Wilson CI intervals; `REPORT_SCHEMA` Draft-07 JSON Schema; `write_report` atomic (`NamedTemporaryFile` + `os.replace`); reproducibility DoD (seeded RNG, pinned corpus hash, deterministic ordering). Division 8.3 CLOSED.

---

## Docs Revision: 5-Agent Roster + GraphRAG Context + Icon — 2026-06-12
**Status:** COMPLETE | **Gates:** docs-only
- Shipped: 5-agent team roster across all 7 language READMEs; GraphRAG ~70% prompt-reduction claim added; dynamic port note; `icon-color.svg` referenced in all READMEs.

---

## Docs Revision: Manifest & Journal Restructure — 2026-06-13
**Status:** COMPLETE | **Gates:** docs-only
- Shipped: `CLAUDE.md §14` strict entry template + interaction protocol; `docs/DEV_JOURNAL_ARCHIVE.md` (compressed Phase 0–7.19 history, one entry per sub-phase); `docs/DEV_JOURNAL.md` rewritten to strict 12-line English template (Phase 8.x only); `docs/PROJECT_MANIFEST.md` restructured (Status Dashboard, Phase Map fixed, embedded Status blocks stripped from Phase 0–7.19 items, translated to English); `README.md` + `DEVELOPERS.md` reference archive.
- Key decision: Archive boundary set at Division 8.0 — Phase 7.19 entries (closed 2026-06-09/10) go to archive; Hito 8.x milestone entries (June 8–9) stay in active journal as they occur in the Phase 8 era.

## 8.8.0: Wave 0 infra gate — DeferredToolLoader + tool_search — 2026-06-13
**Status:** COMPLETE | **Gates:** mypy 0/321 · pytest 7 new (74 in sweep) passed · pyright baseline
- Shipped: `DeferredToolLoader` (eager-vs-deferred policy over `ToolRAGStore`, ~10%-of-budget char threshold) + `tool_search` discovery tool (READ_ONLY, all roles); `tool_rag_select_node` now consults the loader; ambient `_task_active_role` ContextVar added. Gate proves ≥70% reduction at 56 synthetic schemas + retrievability by query.
- Key decision: role resolution is config-first (`RunnableConfig`) with the ContextVar as a declared MVP fallback; `tool_search` returns names+descriptions + a shift-left instruction (discovery, not direct-load) so full schemas never re-inflate the deferred prompt; deferred set built as `k-1`+append to guarantee `≤k` with no drop branch.
- Deferred: DEBT-040 — `tool_search` ContextVar role fallback is stale across per-step transitions; robust `config.configurable` threading scheduled for 8.8.5.
