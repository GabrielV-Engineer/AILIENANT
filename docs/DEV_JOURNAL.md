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
